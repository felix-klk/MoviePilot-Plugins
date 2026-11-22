"""Use SubHD's visible download flow while the operator confirms in WeChat."""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from app.sdk.logging import logger

from ..upload.upload_session import MAX_UPLOAD_CONTENT_BYTES
from .models import SubHDWechatVerificationRequired, _automatic_download
from .shared import _looks_like_html_bytes


_HOSTS = {"subhd.me", "www.subhd.me", "subhd.tv", "www.subhd.tv"}
_CODE = re.compile(r"公众号\s*[里内中]?\s*发送\s*数字\s*[:：]?\s*([0-9]{6})(?![0-9])")
_VERIFY_SECONDS = 90


def _site_url(url, section, sid=None, configured_host=""):
    parts = urlsplit(url)
    match = re.fullmatch(rf"/{section}/([A-Za-z0-9_-]+)", parts.path)
    return bool(parts.scheme == "https" and parts.hostname and not parts.username and not parts.password
                and parts.netloc in (_HOSTS | {configured_host}) and match
                and (sid is None or match[1] == sid))


@contextmanager
def _browser(profile, interactive):
    # Prefer an installed standard browser. MP containers can also supply the SDK browser.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        executable = next((path for path in (
            shutil.which("chromium"), shutil.which("google-chrome"), shutil.which("chromium-browser"),
            playwright.chromium.executable_path,
            os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe")
            if os.name == "nt" else None,
        ) if path and Path(path).is_file()), None)
        if executable:
            context = playwright.chromium.launch_persistent_context(
                str(profile), executable_path=executable, headless=not interactive, accept_downloads=True,
            )
            try:
                yield context
            finally:
                context.close()
            return

    from cloakbrowser import launch_persistent_context

    context = launch_persistent_context(str(profile), headless=not interactive, accept_downloads=True, stealth_args=False)
    try:
        yield context
    finally:
        context.close()


class SubHDWechatDownload:
    # A plugin reload can briefly overlap a closing browser from the previous instance.
    _profile_lock = threading.Lock()

    def __init__(self, owner):
        self.owner = owner
        self.stopped = threading.Event()

    def _notice(self, title, message):
        logger.warning("[NeoSubtitleMaster] %s %s", title, message)
        try:
            from app.schemas.types import NotificationType
            self.owner.post_message(mtype=NotificationType.Plugin, title=f"NEO字幕大师 · {title}", text=message)
            return True
        except Exception as exc:
            logger.warning("[NeoSubtitleMaster] SubHD 微信验证通知发送失败 (%s)", type(exc).__name__)
            return False

    def download(self, original, configured_host):
        if not _site_url(original.page_url, "a", configured_host=configured_host) or self.stopped.is_set():
            raise original
        while not self._profile_lock.acquire(timeout=0.5):
            if self.stopped.is_set():
                raise original
        try:
            if self.stopped.is_set():
                raise original
            profile = self.owner.get_data_path() / "subhd_browser" / "profile"
            profile.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                with _browser(profile, False) as context:
                    return self._download_page(context, original.page_url, configured_host, False)
            except _HumanPrompt:
                if self.stopped.is_set() or not (os.name == "nt" or os.environ.get("DISPLAY")):
                    raise
            with _browser(profile, True) as context:
                return self._download_page(context, original.page_url, configured_host, True)
        except SubHDWechatVerificationRequired:
            raise
        except Exception as exc:
            reason = str(exc) if isinstance(exc, _PageFailure) else f"插件浏览器未能完成下载（{type(exc).__name__}）"
            failure = SubHDWechatVerificationRequired(
                candidate_id=original.candidate_id, page_url=original.page_url,
                status_hint=f"{reason}。可在插件重试，或打开详情页手动下载并上传。{original.page_url}",
            )
            if not self.stopped.is_set() and not _automatic_download.get():
                failure.notified = self._notice("SubHD 下载待处理", str(failure))
            raise failure from exc
        finally:
            self._profile_lock.release()

    def _download_page(self, context, url, configured_host, interactive=False):
        old_pages = set(context.pages)
        page = context.new_page()
        page.set_default_timeout(12000)
        sid = urlsplit(url).path.rsplit("/", 1)[-1]
        page.goto(url, wait_until="domcontentloaded", timeout=18000)
        if not _site_url(page.url, "a", sid, configured_host):
            raise _PageFailure("SubHD 详情页地址已变化")
        page.get_by_text("下载字幕文件", exact=True).click()
        # The site can prepare the download in a new tab or navigate the detail tab.
        navigation_deadline = time.monotonic() + 15
        while not self.stopped.is_set():
            down = next((tab for tab in context.pages if tab not in old_pages
                         and _site_url(tab.url, "down", sid, configured_host)), None)
            if down is not None:
                page = down
                break
            if time.monotonic() >= navigation_deadline:
                raise _PageFailure("点击下载后未进入对应的 SubHD 确认页")
            page.wait_for_timeout(200)
        page.set_default_timeout(12000)
        button = page.get_by_role("button", name=re.compile("下载字幕文件|重新下载"))
        human_prompt = page.get_by_text(re.compile(r"请先完成(?:下方的)?人机验证"))
        def check_human_prompt():
            if not human_prompt.first.is_visible():
                return False
            if _automatic_download.get():
                raise _PageFailure("SubHD 要求额外的人机验证")
            if not interactive:
                raise _HumanPrompt("SubHD 要求额外的人机验证，需要可交互的显示会话")
            page.bring_to_front()
            self._notice("SubHD 待人工验证", f"请在插件浏览器的原下载页完成人机验证，插件会继续下载。\n{url}")
            end = time.monotonic() + _VERIFY_SECONDS
            while not self.stopped.is_set() and time.monotonic() < end:
                if button.is_visible() and button.is_enabled():
                    return True
                page.wait_for_timeout(400)
            raise _PageFailure("等待人工验证超时或插件已停止")

        button.wait_for(state="visible")
        deadline = time.monotonic() + 15
        last_code = ""
        while not self.stopped.is_set():
            if not _site_url(page.url, "down", sid, configured_host):
                raise _PageFailure("SubHD 验证页已失效")
            if check_human_prompt():
                break
            lines = page.locator("li:visible").filter(has_text="发送数字").all_inner_texts()
            codes = {match[1] for line in lines for match in _CODE.finditer(line)}
            if button.is_enabled() and not lines:
                break
            if button.is_enabled() and last_code:
                break
            if len(codes) == 1:
                code = next(iter(codes))
                if code != last_code:
                    if not last_code:
                        deadline = time.monotonic() + _VERIFY_SECONDS
                    last_code = code
                    self._notice("SubHD 待公众号验证",
                                 f"请在微信公众号「火星研究社」发送 {code}。插件正在原下载页等待放行，随后自动下载。\n{url}")
            if time.monotonic() >= deadline:
                raise _PageFailure("等待微信确认超时，本次数字已停止使用" if last_code else "页面未显示可确认的六位数字")
            page.wait_for_timeout(400)
        if self.stopped.is_set():
            raise _PageFailure("插件已停止等待")

        try:
            with page.expect_download(timeout=25000) as completed:
                button.click()
        except Exception as exc:
            if type(exc).__name__ != "TimeoutError" or not check_human_prompt():
                raise
            with page.expect_download(timeout=25000) as completed:
                button.click()
        download = completed.value
        try:
            file = download.path()
            if not file or not 0 < file.stat().st_size <= MAX_UPLOAD_CONTENT_BYTES:
                raise _PageFailure("SubHD 下载文件为空或超过插件大小限制")
            content = file.read_bytes()
            filename = Path(download.suggested_filename).name
            if _looks_like_html_bytes(content, filename):
                raise _PageFailure("SubHD 下载返回了网页")
            return filename, content
        finally:
            download.delete()

class _PageFailure(ValueError):
    pass


class _HumanPrompt(_PageFailure):
    pass
