"""Download existing Chinese SRTs from Subtitle Cat."""

import re
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .media import CHINESE_TAG, MAX_SUBTITLE, chinese_text, decode_subtitle, jav_numbers, release_part


SITE = "https://www.subtitlecat.com"
HOSTS = {"subtitlecat.com", "www.subtitlecat.com"}
TIMESTAMP = r"\d{2,}:[0-5]\d:[0-5]\d[,.]\d{3}"
SRT = re.compile(rf"\s*[0-9]+[ \t]*\n[ \t]*{TIMESTAMP}[ \t]+-->[ \t]+{TIMESTAMP}[^\n]*\n[^\n]+")


class SearchPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = False
        self.rows = []
        self.in_table = False
        self.in_first_cell = False
        self.href = ""
        self.title = ""
        self.description = ""
        self.cells = 0
        self.in_link = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table" and "sub-table" in (attrs.get("class") or "").split():
            self.found = self.in_table = True
        if not self.in_table:
            return
        if tag == "tr":
            self.href = self.title = self.description = ""
            self.cells = 0
        elif tag == "td":
            self.cells += 1
            self.in_first_cell = self.cells == 1
        elif tag == "a" and self.in_first_cell and not self.href:
            self.href = attrs.get("href") or ""
            self.in_link = bool(self.href)

    def handle_endtag(self, tag):
        if tag == "a":
            self.in_link = False
        elif tag == "td":
            self.in_first_cell = False
        elif tag == "table":
            self.in_table = False
        elif tag == "tr" and self.in_table and self.href:
            language = re.search(r"\btranslated from\s+([^()]+)", self.description, re.I)
            chinese = bool(language and CHINESE_TAG.search(language[1]))
            self.rows.append((self.href, self.title.strip(), chinese))

    def handle_data(self, data):
        if self.in_link:
            self.title += data
        elif self.in_first_cell:
            self.description += data


class DownloadPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.translated = {}
        self.original = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        href = attrs.get("href") or ""
        if tag == "a" and href:
            link_id = attrs.get("id") or ""
            if link_id in {"download_zh-CN", "download_zh-TW"}:
                self.translated[link_id.removeprefix("download_")] = href
            elif href.endswith("-orig.srt"):
                self.original.append(href)
        # This reads the static filename reference without executing page code.
        reference = re.fullmatch(
            r"\s*translate_from_server_folder\('[^']+',\s*'([^']+-orig\.srt)',\s*'(/subs/[0-9]+/)'\);?\s*",
            attrs.get("onclick") or "",
        )
        if reference:
            self.original.append(reference[2] + reference[1])


def site_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.netloc.lower() in HOSTS


class SiteRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not site_url(newurl):
            raise OSError("Subtitle Cat 返回了站外跳转")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, referer: str = SITE + "/") -> bytes:
    request = Request(url, headers={"User-Agent": "JAVSubtitleAssistant/1.0", "Referer": referer})
    try:
        with build_opener(SiteRedirects()).open(request, timeout=8) as response:
            data = response.read(MAX_SUBTITLE + 1)
    except HTTPException as error:
        raise OSError("Subtitle Cat 返回了不完整的响应") from error
    if len(data) > MAX_SUBTITLE:
        raise OSError("Subtitle Cat 响应超过 2 MiB")
    return data


def subtitle_link(href: str, page: str, suffix: str) -> str:
    if not href:
        return ""
    try:
        url = urljoin(page, href)
        parts = urlsplit(url)
        path = unquote(parts.path)
        if (site_url(url) and not parts.query and not parts.fragment and path.endswith(suffix) and
                re.fullmatch(r"/subs/[0-9]+/[^/\\\x00-\x1f]+", path)):
            return SITE + quote(path, safe="/")
    except ValueError:
        pass
    return ""


def download_links(detail: str, chinese: bool, search_url: str):
    parsed = DownloadPage()
    parsed.feed(fetch(detail, search_url).decode("utf-8", errors="replace"))
    file_stem = unquote(urlsplit(detail).path).rsplit("/", 1)[-1].removesuffix(".html")
    original = detail.removesuffix(".html") + "-orig.srt"
    if chinese and original in {subtitle_link(link, detail, "-orig.srt") for link in parsed.original}:
        traditional = re.search(r"(?:[._-](?:zh-TW|cht|traditional))\b", file_stem, re.I)
        simplified = re.search(r"(?:[._-](?:zh-CN|chs|simplified))\b", file_stem, re.I)
        yield ("zh-TW" if traditional else "zh-CN" if simplified else "zh"), original
    for language in ("zh-CN", "zh-TW"):
        suffix = f"-{language}.srt"
        url = subtitle_link(parsed.translated.get(language, ""), detail, suffix)
        if url and unquote(urlsplit(url).path).rsplit("/", 1)[-1] == file_stem + suffix:
            yield language, url


def find_chinese_subtitle(number: str, video: Path) -> tuple[str, bytes] | None:
    """Require the same JAV number and part, then return one verified Chinese SRT."""
    part = release_part(video.stem)
    aliases = [number, number.replace("-", "")]
    if number.startswith("FC2-"):
        aliases.append(number.replace("FC2-", "FC2-PPV-"))
    checked = set()
    failures = []
    for keyword in dict.fromkeys(aliases):
        search_url = SITE + "/index.php?search=" + quote(keyword, safe="")
        page = SearchPage()
        page.feed(fetch(search_url).decode("utf-8", errors="replace"))
        if not page.found:
            raise OSError("Subtitle Cat 搜索结果缺少字幕表")
        matches = [
            (title, href, chinese) for href, title, chinese in page.rows
            if number in jav_numbers(title) and release_part(title) == part
        ]
        matches.sort(key=lambda row: (not row[0].casefold().startswith(video.stem.casefold()), not row[2]))
        for _, href, chinese in matches:
            detail = subtitle_link(href, search_url, ".html")
            if not detail or detail in checked or len(checked) >= 6:
                continue
            checked.add(detail)
            try:
                for language, url in download_links(detail, chinese, search_url):
                    try:
                        text = decode_subtitle(fetch(url, detail)).replace("\r\n", "\n").replace("\r", "\n")
                        if SRT.match(text) and chinese_text(text):
                            return language, text.encode("utf-8")
                    except (OSError, UnicodeDecodeError) as error:
                        failures.append(error)
            except OSError as error:
                failures.append(error)
        if len(checked) >= 6:
            break
    if failures:
        raise OSError(f"Subtitle Cat 下载失败：{failures[-1]}")
    return None
