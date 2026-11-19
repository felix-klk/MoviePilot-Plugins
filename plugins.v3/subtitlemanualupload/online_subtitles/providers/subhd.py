from __future__ import annotations

from datetime import datetime, timezone

from ..captcha.subhd import SubHDSvgCaptchaSolver
from ..clients import *  # noqa: F401,F403
from ..keyword_builder import subhd_search_keywords
from ..language import *  # noqa: F401,F403
from ..matcher import *  # noqa: F401,F403
from ..models import *  # noqa: F401,F403
from ..shared import *  # noqa: F401,F403
from .base import BaseSubtitleProvider, _subhd_manual_url


_SUBHD_LABELS = {
    "官方字幕": "官方字幕",
    "AI校对": "AI校对",
    "AI校對": "AI校对",
    "其他来源": "其他来源",
    "其他來源": "其他来源",
}


def _subtitle_listing_metadata(text: str) -> Dict[str, Dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(text or "", "html.parser")
    metadata: Dict[str, Dict[str, Any]] = {}

    def add_metadata(container, count_node, time_node):
        if container is None:
            return
        links = [
            link for link in container.select('a[href^="/a/"]')
            if re.fullmatch(r"/a/[A-Za-z0-9_-]+", link["href"])
        ]
        ids = {link["href"][3:] for link in links}
        if len(ids) != 1:
            return
        fields = metadata.setdefault(ids.pop(), {})
        for link in links:
            classes = link.get("class", [])
            label = link.get_text(" ", strip=True)
            if "link-secondary" in classes and label in {"未关联", "未關聯"}:
                fields["subhd_unassociated"] = True
            elif label and "link-dark" in classes:
                field = "movie_name" if "align-middle" in classes else "listing_title"
                fields.setdefault(field, label)
        labels = {
            _SUBHD_LABELS[label]
            for badge in container.select("span.rounded.text-white")
            if (label := badge.get_text(strip=True).replace(" ", "")) in _SUBHD_LABELS
        }
        if len(labels) == 1:
            fields["subtitle_type"] = labels.pop()

        count = count_node.get_text(strip=True) if count_node is not None else ""
        if re.fullmatch(r"[0-9]{1,15}|[0-9]{1,3}(?:[,，][0-9]{3}){1,4}", count):
            fields["download_count"] = int(count.replace(",", "").replace("，", ""))
        if time_node is not None:
            value = time_node.get("datetime", "")
            try:
                stamp = datetime.fromisoformat(value)
                if stamp.utcoffset() is not None:
                    fields["uploaded_at"] = stamp.astimezone(timezone.utc).isoformat()
            except (ValueError, OverflowError):
                pass

    for card in soup.select("div.clearfix"):
        icon = card.select_one("svg.bi-download")
        time_node = card.select_one("time[datetime]")
        count_node = icon.find_next_sibling() if icon is not None else None
        add_metadata(card, count_node if count_node is not None and count_node.name == "span" else None, time_node)

    for header in soup.select(".rounded-top > .row"):
        columns = header.find_all("div", recursive=False)
        headers = [column.get_text(strip=True) for column in columns]
        counts = next((i for i, value in enumerate(headers) if value in {"下载次数", "下載次數"}), None)
        uploader = next((i for i, value in enumerate(headers) if value in {"上传人", "上傳人", "上传时间", "上傳時間"}), None)
        if counts is None and uploader is None:
            continue
        for row in header.parent.find_next_siblings("div", class_="row"):
            cells = row.find_all("div", recursive=False)
            if len(cells) == len(columns):
                time_node = cells[uploader].select_one("time[datetime]") if uploader is not None else row.select_one("time[datetime]")
                count_node = cells[counts] if counts is not None else None
                add_metadata(row, count_node, time_node)

    return {sid: fields for sid, fields in metadata.items() if fields}


class SubHDProvider(BaseSubtitleProvider):
    provider_id = "subhd"
    display_name = "SubHD"
    default_root_url = DEFAULT_PROVIDER_ROOTS["subhd"]

    def status(self) -> Dict[str, Any]:
        status = super().status()
        status["message"] = "使用豆瓣 ID 或标题自动搜索，必要时自动处理下载验证码"
        return status

    def manual_url(self, keyword: str) -> str:
        return _subhd_manual_url(self.root_url, keyword)

    def search(self, keyword: str, targets: List[Dict[str, Any]], scope: str) -> List[OnlineSubtitleResult]:
        return self._search_keyword(keyword, targets)

    def search_all(
        self, keywords: List[str], targets: List[Dict[str, Any]], scope: str, *, manual_keyword: str = "",
    ) -> List[OnlineSubtitleResult]:
        title_keywords = subhd_search_keywords(keywords, targets, scope, manual_keyword=manual_keyword)
        errors: List[str] = []

        def attempt(label: str, callback):
            try:
                return callback()
            except CaptchaRequiredError:
                raise
            except Exception as exc:
                message = _compact_error_message(str(exc))
                if message not in errors:
                    errors.append(message)
                logger.warning(
                    "[SubtitleManualUpload] SubHD 搜索步骤失败，继续后续候选 step=%s error=%s",
                    label,
                    message,
                )
                return []

        def search_keywords() -> List[OnlineSubtitleResult]:
            for keyword in title_keywords:
                found = attempt(f"keyword:{keyword}", lambda keyword=keyword: self._search_keyword(keyword, targets))
                if found:
                    return found
            return []

        found = search_keywords()
        if found:
            return found

        douban_id = _first_target_value(targets, "douban_id")
        if douban_id:
            found = attempt(
                "douban-detail",
                lambda: self._search_detail(str(douban_id), targets, "SubHD 豆瓣 ID 查询"),
            )
            if found:
                return found
        fallback = self._fallback_ids_from_douban(keywords, targets)
        fallback_douban = fallback.get("douban_id")
        if fallback_douban and fallback_douban != douban_id:
            found = attempt(
                "douban-fallback",
                lambda: self._search_detail(fallback_douban, targets, "SubHD 豆瓣搜索兜底"),
            )
            if found:
                return found
        fallback_imdb = fallback.get("imdb_id") or _first_target_value(targets, "imdb_id")
        if fallback_imdb:
            resolved = attempt("imdb-resolve", lambda: self._resolve_imdb(fallback_imdb))
            if resolved:
                found = attempt(
                    "imdb-detail",
                    lambda: self._search_detail(resolved, targets, "SubHD IMDb 兜底查询"),
                )
                if found:
                    return found
        if errors:
            raise ValueError(_provider_error_summary(errors, len(title_keywords)))
        return []

    def _search_keyword(self, keyword: str, targets: List[Dict[str, Any]]) -> List[OnlineSubtitleResult]:
        if not keyword:
            return []
        fetch = getattr(self.fetcher, "get_search_text", self.fetcher.get_text)
        status, text, final_url = fetch(self.manual_url(keyword), referer=self.root_url)
        if status >= 400 and status != 404:
            raise ValueError(f"SubHD 搜索请求失败 HTTP {status}，请检查 CF 服务、站点地址及代理配置")
        if status < 400 and text and "<" not in text:
            raise ValueError("SubHD 搜索返回内容不是网页，请检查 CF 服务响应格式")
        if status >= 400 or not text:
            logger.warning(
                "[SubtitleManualUpload] SubHD 标题搜索页面不可用 keyword=%s status=%s",
                keyword,
                status,
            )
            return []
        detail_candidates = self._relevant_detail_candidates(text, keyword, targets)
        direct_count = len(re.findall(r'href=["\']/a/[A-Za-z0-9_-]+["\']', text or "", flags=re.I))
        logger.info(
            "[SubtitleManualUpload] SubHD 标题搜索页面解析完成 keyword=%s status=%s detail_candidates=%s relevant_detail_candidates=%s direct_candidates=%s final_host=%s",
            keyword,
            status,
            len(_extract_unique_matches(r'href=["\']/d/(\d+)["\']', text)),
            len(detail_candidates),
            direct_count,
            _host(final_url),
        )
        results = self._parse_subtitles(
            text,
            final_url,
            targets,
            f"SubHD 标题查询 · {keyword}",
            match_keyword=keyword,
        )
        if results:
            return _dedupe_results(results)[:30]
        for sid in detail_candidates[:4]:
            try:
                results.extend(
                    self._search_detail(
                        sid,
                        targets,
                        f"SubHD 标题查询 · {keyword}",
                        match_keyword=keyword,
                    )
                )
            except CaptchaRequiredError:
                raise
            except Exception as exc:
                logger.warning(
                    "[SubtitleManualUpload] SubHD 详情候选失败，继续下一个 detail_id=%s error=%s",
                    sid,
                    _compact_error_message(str(exc)),
                )
                continue
            if results:
                break
        return _dedupe_results(results)[:30]

    @staticmethod
    def _relevant_detail_candidates(text: str, keyword: str, targets: List[Dict[str, Any]]) -> List[str]:
        candidates: List[str] = []
        seen: set[str] = set()
        pattern = r'<a[^>]+href=["\']/d/(\d+)["\'][^>]*>(.*?)</a>'
        for match in re.finditer(pattern, text or "", flags=re.I | re.S):
            detail_id, raw_label = match.groups()
            label = _strip_tags(raw_label)
            if not label:
                alt_match = re.search(r'\balt=["\']([^"\']+)["\']', raw_label, flags=re.I)
                label = html.unescape(alt_match.group(1)).strip() if alt_match else ""
            assessment = _assess_result_match(title=label, keyword=keyword, targets=targets)
            if detail_id in seen or assessment["identity_status"] == "failed":
                continue
            seen.add(detail_id)
            candidates.append(detail_id)
        return candidates

    def _search_detail(
        self,
        detail_id: str,
        targets: List[Dict[str, Any]],
        query_plan: str,
        *,
        match_keyword: str = "",
    ) -> List[OnlineSubtitleResult]:
        detail_id = str(detail_id or "").strip()
        if not detail_id:
            return []
        page_url = f"{self.root_url}/d/{quote(detail_id)}"
        fetch = getattr(self.fetcher, "get_search_text", self.fetcher.get_text)
        status, text, final_url = fetch(page_url, referer=self.root_url)
        if status >= 400 and status != 404:
            raise ValueError(f"SubHD 详情请求失败 HTTP {status}，请检查 CF 服务、站点地址及代理配置")
        if status >= 400 or not text:
            if status == 404:
                logger.info(
                    "[SubtitleManualUpload] SubHD 未找到豆瓣 ID 对应详情页，跳过 detail_id=%s status=%s",
                    detail_id,
                    status,
                )
            else:
                logger.warning(
                    "[SubtitleManualUpload] SubHD 详情页不可用 detail_id=%s status=%s",
                    detail_id,
                    status,
                )
            return []
        results = self._parse_subtitles(
            text,
            final_url or page_url,
            targets,
            query_plan,
            match_keyword=match_keyword,
        )
        logger.info(
            "[SubtitleManualUpload] SubHD 详情页解析完成 detail_id=%s results=%s query_plan=%s",
            detail_id,
            len(results),
            query_plan,
        )
        return results

    def _parse_subtitles(
        self,
        text: str,
        page_url: str,
        targets: List[Dict[str, Any]],
        query_plan: str,
        *,
        match_keyword: str = "",
    ) -> List[OnlineSubtitleResult]:
        results: List[OnlineSubtitleResult] = []
        target_episode = _target_episode_from_targets(targets)
        listing_metadata = _subtitle_listing_metadata(text)
        for match in re.finditer(r'<a[^>]+href=["\'](/a/([A-Za-z0-9_-]+))["\'][^>]*>(.*?)</a>', text or "", flags=re.I | re.S):
            href, sid, raw_title = match.groups()
            listing = listing_metadata.get(sid, {})
            unassociated = listing.get("subhd_unassociated", False)
            title = listing.get("listing_title") or _strip_tags(raw_title) or f"SubHD 字幕 {sid}"
            if target_episode:
                include, _ = _episode_include_for_title(title, target_episode)
                if not include:
                    continue
            season, episode = _episode_from_text(title) or (0, 0)
            assessment = _assess_result_match(
                title=title, keyword=match_keyword, targets=targets, attrs={"movie_name": listing.get("movie_name")},
            )
            if assessment["identity_status"] == "failed" and targets:
                continue
            match_detail = assessment["match_detail"]
            if unassociated:
                match_detail = " / ".join(filter(None, (match_detail, "SubHD 未关联 · 仅手动选择")))
            window_start = max(0, match.start() - 700)
            window_end = min(len(text or ""), match.end() + 300)
            language_text = f"{title} {_strip_tags((text or '')[window_start:window_end])}"
            page = urljoin(self.root_url, href)
            results.append(
                OnlineSubtitleResult(
                    provider=self.provider_id,
                    provider_label=self.display_name,
                    result_id=sid,
                    title=title,
                    download_count=listing.get("download_count"),
                    uploaded_at=listing.get("uploaded_at", ""),
                    subtitle_type=listing.get("subtitle_type", ""),
                    page_url=page,
                    download_url=f"subhd-page:{page}",
                    language=_guess_language_label(language_text),
                    language_category=_language_category_from_text(language_text),
                    format=_guess_subtitle_format(language_text),
                    season=season,
                    episode=episode,
                    score=assessment["score"] + 10,
                    source=self.display_name,
                    note="通过 SubHD 自动搜索",
                    downloadable=True,
                    query_plan=query_plan,
                    identity_status=assessment["identity_status"],
                    match_detail=match_detail,
                    subhd_unassociated=unassociated,
                )
            )
        return _dedupe_results(results)

    def _resolve_imdb(self, imdb_id: str) -> str:
        imdb = _normalize_imdb_tt(imdb_id)
        if not imdb:
            return ""
        status, text, _ = self.fetcher.get_text(f"{self.root_url}/search/{quote(imdb)}", referer=self.root_url)
        if status >= 400 or not text:
            return ""
        ids = _extract_unique_matches(r'href=["\']/d/(\d+)["\']', text)
        return ids[0] if ids else ""

    def _fallback_ids_from_douban(self, keywords: List[str], targets: List[Dict[str, Any]]) -> Dict[str, str]:
        for keyword in keywords[:2]:
            candidate = _search_douban_subject(
                keyword,
                _target_year_from_targets(targets),
                self.fetcher,
                targets,
            )
            if candidate.get("douban_id"):
                if not candidate.get("imdb_id"):
                    candidate["imdb_id"] = _fetch_douban_imdb_id(candidate["douban_id"], self.fetcher)
                logger.info(
                    "[SubtitleManualUpload] 豆瓣兜底候选通过身份校验 keyword=%s douban_id=%s title=%s year=%s",
                    keyword,
                    candidate.get("douban_id"),
                    candidate.get("title"),
                    candidate.get("year"),
                )
                return candidate
        return {}

    @staticmethod
    def _extract_api_download_url(data: Any) -> str:
        if isinstance(data, str):
            text = html.unescape(data.strip())
            if not text:
                return ""
            if text.startswith(("http://", "https://", "/")):
                return text
            if text.startswith("{") or text.startswith("["):
                try:
                    return SubHDProvider._extract_api_download_url(json.loads(text))
                except Exception:
                    pass
            hrefs = _extract_download_hrefs(text)
            if hrefs:
                return hrefs[0]
            if re.search(r"(?i)(download|down|api|file|zip|rar|7z|srt|ass|ssa|sub|vtt)", text) and not re.search(r"\s", text):
                return text
            return ""
        if isinstance(data, list):
            for item in data:
                found = SubHDProvider._extract_api_download_url(item)
                if found:
                    return found
            return ""
        if not isinstance(data, dict):
            return ""
        for key in (
            "url",
            "download_url",
            "downloadUrl",
            "down_url",
            "downUrl",
            "file_url",
            "fileUrl",
            "link",
            "href",
            "path",
            "location",
            "redirect",
        ):
            found = SubHDProvider._extract_api_download_url(data.get(key))
            if found:
                return found
        for key in ("data", "result", "sub", "subtitle", "file", "download", "resource"):
            found = SubHDProvider._extract_api_download_url(data.get(key))
            if found:
                return found
        for value in data.values():
            found = SubHDProvider._extract_api_download_url(value)
            if found:
                return found
        return ""

    def _download_first_valid_link(self, links: List[str], base_url: str, referer: str) -> Optional[Tuple[str, bytes]]:
        for href in links:
            file_url = urljoin(base_url, html.unescape(href))
            try:
                filename, content, final_url = self.fetcher.get_bytes(file_url, referer=referer)
                if content and not _looks_like_html_bytes(content, filename or final_url):
                    return filename, content
            except CaptchaRequiredError:
                raise
            except Exception as exc:
                logger.info("[SubtitleManualUpload] SubHD 下载候选链接失败 host=%s error=%s", _host(file_url), exc)
        return None

    def download(self, result: Dict[str, Any], captcha_code: str = "") -> Tuple[str, bytes]:
        bypass_url = getattr(self.fetcher, "bypass_url", "")
        if not bypass_url:
            return self._download_session(result, captcha_code)
        # Never replace the shared fetcher: concurrent downloads need separate jars.
        direct = OnlinePageClient(use_proxy=False, timeout=self.fetcher.timeout, browser_recovery=False)
        try:
            logger.info("[SubtitleManualUpload] SubHD 下载开始 mode=direct")
            return SubHDProvider(direct, root_url=self.root_url)._download_session(result, captcha_code)
        except CaptchaRequiredError:
            logger.info("[SubtitleManualUpload] SubHD 直连需要验证，从详情页重新开始 mode=cf")
        finally:
            direct.close()
        remote = OnlinePageClient(use_proxy=False, timeout=self.fetcher.timeout, bypass_url=bypass_url)
        try:
            return SubHDProvider(remote, root_url=self.root_url)._download_session(result, captcha_code)
        finally:
            remote.close()

    def _download_session(self, result: Dict[str, Any], captcha_code: str = "") -> Tuple[str, bytes]:
        page_url = str(result.get("page_url") or result.get("download_url") or "").replace("subhd-page:", "", 1)
        if not page_url:
            raise ValueError("SubHD 缺少下载页面")
        status, text, final_page_url = self.fetcher.get_text(page_url, referer=self.root_url)
        if status >= 400 or not text:
            mode = "cf" if getattr(self.fetcher, "bypass_url", "") else "direct"
            raise ValueError(f"SubHD 详情页不可访问 mode={mode} HTTP {status} host={_host(page_url)}")
        page_url = final_page_url or page_url
        parsed_page = urlparse(page_url)
        root_url = f"{parsed_page.scheme}://{parsed_page.netloc}"
        down_match = re.search(r'href=["\']([^"\']*/down/[A-Za-z0-9_-]+[^"\']*)["\']', text, re.I)
        if down_match:
            down_url = urljoin(root_url, html.unescape(down_match.group(1)))
        else:
            sid_match = re.search(r'\bdata-sid=["\']([A-Za-z0-9_-]+)["\']', text, re.I)
            if not sid_match:
                raise ValueError("SubHD 未找到下载按钮")
            prepare_data = self.fetcher.post_json(
                f"{root_url}/api/sub/prepare-download",
                {"sid": sid_match.group(1)},
                referer=page_url,
            )
            prepared_url = self._extract_api_download_url(prepare_data)
            if not prepare_data.get("success") or "/down/" not in prepared_url:
                raise ValueError("SubHD 下载按钮准备失败")
            down_url = urljoin(root_url, prepared_url)
        sid = Path(urlparse(down_url).path).name
        if not sid:
            raise ValueError("SubHD 下载按钮缺少 sid")
        down_status, down_text, down_final_url = self.fetcher.get_text(down_url, referer=page_url)
        if down_status >= 400 or not down_text:
            mode = "cf" if getattr(self.fetcher, "bypass_url", "") else "direct"
            raise ValueError(f"SubHD 下载确认页不可访问 mode={mode} HTTP {down_status} host={_host(down_url)}")
        down_url = down_final_url or down_url
        parsed_down = urlparse(down_url)
        root_url = f"{parsed_down.scheme}://{parsed_down.netloc}"
        payload = {"sid": sid, "cap": captcha_code or ""}
        api_url = f"{root_url}/api/sub/down"

        def submit():
            try:
                return self.fetcher.post_json(api_url, payload, referer=down_url)
            except OnlineJSONError as exc:
                if exc.status == 403 and exc.payload.get("turnstileRequired") is True:
                    return exc.payload
                raise

        data = submit()
        if data.get("turnstileRequired") is True:
            config_match = re.search(r'\bturnstileConfig\s*=\s*(\{[^\n]+\})\s*;', down_text or "")
            try:
                config = json.loads(config_match.group(1)) if config_match else {}
            except ValueError:
                config = {}
            sitekey = config.get("siteKey")
            if not sitekey:
                raise CaptchaRequiredError("SubHD 要求 Turnstile 验证，但下载页未提供 siteKey；请在站点完成验证后下载。", provider="subhd", verify_url=down_url)
            payload["turnstileToken"] = self.fetcher.solve_turnstile(down_url, sitekey=sitekey, action="download")
            data = submit()
            if data.get("turnstileRequired") is True:
                raise CaptchaRequiredError("SubHD 未接受本次 Turnstile 令牌，请确认服务和下载使用同一代理出口；本次已停止重试。", provider="subhd", verify_url=down_url)
        if data.get("pass") is False and not payload["cap"] and "<svg" in str(data.get("msg") or "").lower():
            payload["cap"] = SubHDSvgCaptchaSolver().solve(str(data.get("msg") or ""))
            data = submit()
        if not data.get("success"):
            raise ValueError("SubHD 下载验证码校验失败或接口未返回下载地址")
        file_url = self._extract_api_download_url(data)
        if not file_url:
            fallback_links = _extract_download_hrefs(down_text or "")
            fallback = self._download_first_valid_link(fallback_links, down_final_url or down_url, down_url)
            if fallback:
                return fallback
            url_value = data.get("url") if isinstance(data, dict) else ""
            logger.warning(
                "[SubtitleManualUpload] SubHD API 未返回下载地址 sid=%s keys=%s success=%s pass=%s url_len=%s url_type=%s msg_hint=%s down_status=%s",
                sid,
                ",".join(sorted(str(key) for key in data.keys())) if isinstance(data, dict) else type(data).__name__,
                data.get("success") if isinstance(data, dict) else "",
                data.get("pass") if isinstance(data, dict) else "",
                len(str(url_value or "")),
                type(url_value).__name__,
                _compact_error_message(str(data.get("msg") or ""))[:80] if isinstance(data, dict) else "",
                down_status,
            )
            raise ValueError("SubHD API 未返回下载地址")
        resolved_url = urljoin(root_url, file_url)
        filename, content, final_url = self.fetcher.get_bytes(resolved_url, referer=down_url)
        if _looks_like_html_bytes(content, filename or final_url):
            text = _decode_bytes(content, filename)
            fallback = self._download_first_valid_link(_extract_download_hrefs(text), final_url or resolved_url, resolved_url)
            if fallback:
                return fallback
            raise ValueError("SubHD 下载地址返回了 HTML 页面，未解析到有效字幕文件")
        return filename or f"subhd-{sid}.zip", content
