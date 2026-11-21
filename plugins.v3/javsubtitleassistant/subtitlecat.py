"""Subtitle Cat provider adapted from 海拉鲁字幕大师 V3 1.2.38 (GPL-3.0)."""

import re
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from time import monotonic
from urllib.parse import quote, unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from app.sdk.config import settings

from .media import CHINESE_TAG, MAX_SUBTITLE, chinese_text, decode_subtitle, jav_numbers, release_part


SITE = "https://www.subtitlecat.com"
HOSTS = {"subtitlecat.com", "www.subtitlecat.com"}
TIMESTAMP = r"\d{2,}:[0-5]\d:[0-5]\d[,.]\d{3}"
SRT = re.compile(rf"\s*[0-9]+[ \t]*\n[ \t]*{TIMESTAMP}[ \t]+-->[ \t]+{TIMESTAMP}[^\n]*\n[^\n]+")


def _site_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.netloc.lower() in HOSTS


class _SiteRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _site_url(newurl):
            raise OSError("Subtitle Cat 返回了站外跳转")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _SearchTable(HTMLParser):
    """Read the first cell only: metrics and language counts are not language evidence."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found_table = False
        self.data_rows = 0
        self.rows = []
        self._in_table = False
        self._cell = 0
        self._in_cell = False
        self._href = ""
        self._title = []
        self._text = []
        self._in_anchor = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "table" and "sub-table" in (attributes.get("class") or "").split():
            self.found_table = self._in_table = True
        if not self._in_table:
            return
        if tag == "tr":
            self._cell, self._href, self._title, self._text = 0, "", [], []
        elif tag == "td":
            self._cell += 1
            self._in_cell = self._cell == 1
        elif tag == "a" and self._in_cell and not self._href:
            self._href = attributes.get("href") or ""
            self._in_anchor = True

    def handle_data(self, data):
        if self._in_cell:
            self._text.append(data)
        if self._in_anchor:
            self._title.append(data)

    def handle_endtag(self, tag):
        if tag == "a":
            self._in_anchor = False
        elif tag == "td":
            self._in_cell = False
        elif tag == "tr" and self._in_table:
            self.data_rows += bool(self._cell)
            if self._href:
                self.rows.append((self._href, "".join(self._title).strip(), " ".join(self._text)))
        elif tag == "table":
            self._in_table = False


class _DownloadLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = {}
        self.originals = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "a" and (attributes.get("id") or "").startswith("download_"):
            self.links[attributes["id"].removeprefix("download_")] = attributes.get("href") or ""
        if tag == "a" and (attributes.get("href") or "").endswith("-orig.srt"):
            self.originals.append(attributes["href"])
        # This is a static filename reference, never an instruction to run page JavaScript.
        call = re.fullmatch(
            r"\s*translate_from_server_folder\('[^']+',\s*'([^']+-orig\.srt)',\s*'(/subs/\d+/)'\);?\s*",
            attributes.get("onclick") or "",
        )
        if call:
            self.originals.append(call.group(2) + call.group(1))


class SubtitleCatProvider:
    """Keep Hyrule's search/detail/download flow and match local JAV numbers and parts."""

    MAX_KEYWORDS = 3
    MAX_RESULTS = 20
    MAX_DETAILS = 6
    SEARCH_BUDGET_SECONDS = 25

    def __init__(self, use_proxy: bool = False):
        proxies = getattr(settings, "PROXY", None) if use_proxy else None
        self._opener = build_opener(ProxyHandler(proxies), _SiteRedirects())

    def _fetch(self, url: str, deadline: float, referer: str = SITE + "/") -> bytes:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("Subtitle Cat 搜索超时")
        request = Request(url, headers={"User-Agent": "JAVSubtitleAssistant/1.1", "Referer": referer})
        try:
            with self._opener.open(request, timeout=min(8, remaining)) as response:
                data = response.read(MAX_SUBTITLE + 1)
        except HTTPException as error:
            raise OSError("Subtitle Cat 返回了不完整的响应") from error
        if len(data) > MAX_SUBTITLE:
            raise OSError("Subtitle Cat 响应超过 2 MiB")
        return data

    @staticmethod
    def _safe_url(value: str, base: str, suffix: str) -> str:
        if not value:
            return ""
        try:
            url = urljoin(base, value)
            parsed = urlsplit(url)
            path = unquote(parsed.path)
            if (_site_url(url) and not parsed.query and not parsed.fragment and path.endswith(suffix)
                    and re.fullmatch(r"/subs/\d+/[^/\\\x00-\x1f]+", path)):
                return SITE + quote(path, safe="/-._~")
        except ValueError:
            pass
        return ""

    def _search(self, keyword: str, number: str, video: Path, deadline: float):
        search_url = SITE + "/index.php?search=" + quote(keyword, safe="")
        parser = _SearchTable()
        parser.feed(self._fetch(search_url, deadline).decode("utf-8", errors="replace"))
        if not parser.found_table or (parser.data_rows and not parser.rows):
            raise OSError("Subtitle Cat 搜索页面无法解析")
        part = release_part(video.stem)
        matches = []
        for href, title, description in parser.rows:
            if number not in jav_numbers(title) or release_part(title) != part:
                continue
            page_url = self._safe_url(href, search_url, ".html")
            if not page_url:
                continue
            language = re.search(r"\btranslated from\s+([^()]+)", description, re.I)
            original_chinese = bool(language and CHINESE_TAG.search(language[1]))
            matches.append((page_url, title, original_chinese))
            if len(matches) >= self.MAX_RESULTS:
                break
        matches.sort(key=lambda row: (not row[1].casefold().startswith(video.stem.casefold()), not row[2]))
        return search_url, matches

    def _download(self, page_url: str, original_chinese: bool, search_url: str, deadline: float):
        parser = _DownloadLinks()
        parser.feed(self._fetch(page_url, deadline, search_url).decode("utf-8", errors="replace"))
        name = unquote(urlsplit(page_url).path).rsplit("/", 1)[-1].removesuffix(".html")
        original = page_url.removesuffix(".html") + "-orig.srt"
        downloads = []
        if original_chinese and original in {self._safe_url(href, page_url, "-orig.srt") for href in parser.originals}:
            traditional = re.search(r"(?:[._-](?:zh-TW|cht|traditional))\b", name, re.I)
            simplified = re.search(r"(?:[._-](?:zh-CN|chs|simplified))\b", name, re.I)
            downloads.append(("zh-TW" if traditional else "zh-CN" if simplified else "zh", original))
        for code in ("zh-CN", "zh-TW"):
            suffix = f"-{code}.srt"
            url = self._safe_url(parser.links.get(code, ""), page_url, suffix)
            if url and unquote(urlsplit(url).path).rsplit("/", 1)[-1] == name + suffix:
                downloads.append((code, url))
        error = None
        for code, url in downloads:
            try:
                text = decode_subtitle(self._fetch(url, deadline, page_url)).replace("\r\n", "\n").replace("\r", "\n")
                if SRT.match(text) and chinese_text(text):
                    return code, text.encode("utf-8")
                error = OSError("Subtitle Cat 返回的文件不是有效的中文字幕")
            except (OSError, UnicodeDecodeError) as exc:
                error = exc
        if error:
            raise error
        return None

    def search_and_download(self, number: str, video: Path) -> tuple[str, bytes] | None:
        deadline = monotonic() + self.SEARCH_BUDGET_SECONDS
        aliases = [number, number.replace("-", "")]
        if number.startswith("FC2-"):
            aliases.append(number.replace("FC2-", "FC2-PPV-"))
        seen = set()
        error = None
        for keyword in list(dict.fromkeys(aliases))[:self.MAX_KEYWORDS]:
            search_url, matches = self._search(keyword, number, video, deadline)
            for page_url, _, original_chinese in matches:
                if page_url in seen:
                    continue
                seen.add(page_url)
                try:
                    result = self._download(page_url, original_chinese, search_url, deadline)
                    if result:
                        return result
                except (OSError, UnicodeDecodeError) as exc:
                    error = exc
                if len(seen) >= self.MAX_DETAILS:
                    break
            if len(seen) >= self.MAX_DETAILS:
                break
        if error:
            raise OSError(f"Subtitle Cat 下载失败：{error}") from error
        return None
