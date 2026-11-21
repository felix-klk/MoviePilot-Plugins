"""MoviePilot V3 entry point for the JAV Subtitle Assistant."""

import os
import shutil
from datetime import datetime
from itertools import chain, islice
from pathlib import Path
from stat import S_ISREG
from subprocess import SubprocessError
from threading import Event, Lock
from time import monotonic, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from app.sdk import scheduler as scheduler_sdk
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .media import SAMPLE, VIDEOS, has_chinese_embedded, has_chinese_sidecar, video_number, write_subtitle
from .subtitlecat import SubtitleCatProvider


# Form rows follow the GPL-3.0 configuration layout in 海拉鲁字幕大师 V3.
def _col(component: str, props: dict, md: int = 12) -> dict:
    return {"component": "VCol", "props": {"cols": 12, "md": md},
            "content": [{"component": component, "props": props}]}


def _row(*columns: dict) -> dict:
    return {"component": "VRow", "content": list(columns)}


class JavSubtitleAssistant(_PluginBase):
    plugin_name = "JAV字幕助手"
    plugin_desc = "扫描本地整理目录，从 Subtitle Cat 下载缺少的中文字幕。"
    plugin_icon = ""
    plugin_version = "1.1.0"
    plugin_author = "felix-klk"
    author_url = "https://github.com/felix-klk"
    plugin_config_prefix = "javsubtitleassistant_"
    plugin_order = 50
    auth_level = 1

    DEFAULTS = {"enabled": False, "folders": "", "cron": "0 * * * *",
                "onlyonce": False, "online_use_proxy": False}
    MANUAL_JOB = "scan_once"
    MAX_SEARCHES = 40
    MAX_CHECKS = 100
    MAX_CACHE = 8000
    SCAN_BUDGET_SECONDS = 180

    def __init__(self):
        super().__init__()
        self._folders = ()
        self._trigger = None
        self._stopped = Event()
        self._stopped.set()
        self._scan_lock = Lock()
        self._once_scheduled = False
        self._configuration_error = ""
        self._use_proxy = False

    def init_plugin(self, config: dict | None = None) -> None:
        """Read local paths and validate the schedule before exposing it to MoviePilot."""
        self.stop_service()
        values = self.DEFAULTS | (config or {})
        self._configuration_error = ""
        self._trigger = None
        if not values["enabled"]:
            return
        folders = dict.fromkeys(Path(line.strip()) for line in str(values["folders"]).splitlines()
                                if line.strip())
        self._folders = tuple(folder for folder in folders if folder.is_absolute())
        if not self._folders or len(self._folders) != len(folders):
            self._configuration_error = "请填写 MoviePilot 可访问的绝对目录，每行一个。"
            logger.warning(f"JAV字幕助手：{self._configuration_error}")
            return
        try:
            self._trigger = CronTrigger.from_crontab(str(values["cron"]), timezone=ZoneInfo(settings.TZ))
        except (ValueError, ZoneInfoNotFoundError) as error:
            self._configuration_error = f"定时表达式或时区无效。{error}"
            logger.warning(f"JAV字幕助手：{self._configuration_error}")
            return
        self._use_proxy = bool(values["online_use_proxy"])
        self._stopped = Event()
        if values["onlyonce"]:
            add = getattr(scheduler_sdk, "add_plugin_once_job", None)
            if add and add(type(self).__name__, self.MANUAL_JOB, self.scan_now, "JAV字幕助手立即扫描", delay_seconds=3):
                self._once_scheduled = True
                values["onlyonce"] = False
                self.update_config(values)
            else:
                self._configuration_error = "立即扫描未能加入调度，请从「服务」运行扫描任务。"

    def get_state(self) -> bool:
        return not self._stopped.is_set()

    @staticmethod
    def get_command() -> list:
        return []

    def get_api(self) -> list:
        return []

    def get_form(self) -> tuple[list, dict]:
        return [{"component": "VForm", "content": [
            _row(_col("VSwitch", {"model": "enabled", "label": "启用插件"}, 6),
                 _col("VSwitch", {"model": "onlyonce", "label": "保存后立即扫描"}, 6)),
            _row(_col("VTextarea", {
                "model": "folders", "label": "转移后的本地目录", "rows": 3,
                "hint": "填写 MoviePilot 内能访问的绝对路径，每行一个。", "persistent-hint": True,
            })),
            _row(_col("VTextField", {"model": "cron", "label": "定时扫描（Cron）"}, 6),
                 _col("VSwitch", {"model": "online_use_proxy", "label": "Subtitle Cat 使用 MoviePilot 系统代理"}, 6)),
        ]}], self.DEFAULTS.copy()

    def get_page(self) -> list[dict]:
        """Show the scan outcome without requiring access to server logs."""
        if self._scan_lock.locked() or self._once_scheduled:
            title, message, level = "扫描中", "扫描完成后，重新打开详情查看结果。", "info"
        elif self._configuration_error:
            title, message, level = "设置需要检查", self._configuration_error, "warning"
        elif not self.get_state():
            title, message, level = "插件未启用", "在设置里填写目录，勾选「启用插件」和「保存后立即扫描」。", "info"
        else:
            report = self.get_data("last_scan")
            if isinstance(report, dict):
                title = f"最近一次扫描 · {report['finished']}"
                message = (
                    f"发现 {report['videos']} 个视频，已有中文 {report['existing']}，"
                    f"未识别番号 {report['unidentified']}，联网 {report['searched']}，"
                    f"写入 {report['saved']}，未找到 {report['missing']}，"
                    f"暂缓 {report['cached']}，失败 {report['failed']}。"
                )
                level = "warning" if report["failed"] else "success"
                return [
                    {"component": "VAlert", "props": {
                        "title": title, "text": message, "type": level, "variant": "tonal",
                    }},
                    {"component": "VList", "content": [
                        {"component": "VListItem", "props": {"title": item[0], "subtitle": item[1]}}
                        for item in report["details"]
                    ]},
                ]
            title, message, level = "尚未扫描", "在设置里勾选「保存后立即扫描」，或者从服务列表运行。", "info"
        return [{"component": "VAlert", "props": {
            "title": title, "text": message, "type": level, "variant": "tonal",
        }}]

    def get_service(self) -> list[dict]:
        """Register the scan using MoviePilot's scheduler."""
        if not self.get_state():
            return []
        return [{
            "id": "JavSubtitleAssistant.Scan",
            "name": "JAV字幕助手 · 扫描本地目录",
            "trigger": self._trigger,
            "func": self.scan,
            "kwargs": {},
        }]

    def stop_service(self) -> None:
        self._stopped.set()
        if self._once_scheduled:
            remove = getattr(scheduler_sdk, "remove_plugin_once_job", None)
            if remove:
                remove(type(self).__name__, self.MANUAL_JOB)
            self._once_scheduled = False

    def _videos(self, folders: tuple[Path, ...], stopped: Event, on_error):
        """Walk the configured folders without following nested directory symlinks."""
        seen = set()
        for folder in folders:
            try:
                root = folder.resolve(strict=True)
                if not root.is_dir():
                    raise NotADirectoryError(str(folder))
            except (OSError, RuntimeError) as error:
                on_error(str(folder), error)
                continue
            for directory, subdirs, files in os.walk(
                    root, followlinks=False, onerror=lambda error: on_error(error.filename or str(root), error)):
                if stopped.is_set():
                    return
                subdirs.sort()
                for name in sorted(files):
                    video = Path(directory) / name
                    if video.suffix.lower() not in VIDEOS or SAMPLE.search(video.stem):
                        continue
                    if video not in seen:
                        seen.add(video)
                        yield video

    def scan_now(self) -> None:
        """Retry eligible files when requested from the settings page."""
        self._once_scheduled = False
        self.scan(force=True)

    def scan(self, *, force: bool = False) -> None:
        """Scan one batch and download at most one Chinese subtitle per video."""
        stopped, folders = self._stopped, self._folders
        if stopped.is_set() or not self._scan_lock.acquire(blocking=False):
            return
        try:
            self._configuration_error = ""
            now = time()
            deadline = monotonic() + self.SCAN_BUDGET_SECONDS
            report = dict.fromkeys(("videos", "existing", "unidentified", "searched",
                                    "saved", "missing", "cached", "failed"), 0)
            details = []

            def add_detail(name: str, reason: str) -> None:
                if len(details) < 15:
                    details.append([name, reason])

            def log_failure(name: str, error: Exception) -> None:
                report["failed"] += 1
                add_detail(name, str(error))
                logger.warning(f"JAV字幕助手：{name}，{error}")

            state = self.get_data("scan_state")
            if not isinstance(state, dict):
                state = {}
            stored = state.get("checked")
            cache = {
                key: record for key, record in (stored.items() if isinstance(stored, dict) else ())
                if isinstance(record, list) and len(record) == 3
                and isinstance(record[2], (int, float)) and record[2] > now
            }
            ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
            if not ffprobe:
                logger.warning("JAV字幕助手：找不到 ffprobe，将跳过无法检查内嵌字幕的视频")
            videos = list(self._videos(folders, stopped, log_failure))
            report["videos"] = len(videos)
            provider = SubtitleCatProvider(use_proxy=self._use_proxy)
            cursor = state.get("cursor")
            start = next((index + 1 for index, video in enumerate(videos) if str(video) == cursor), 0)
            checked = 0
            last = ""
            for video in chain(islice(videos, start, None), islice(videos, start)):
                if (stopped.is_set() or monotonic() >= deadline or report["searched"] >= self.MAX_SEARCHES
                        or checked >= self.MAX_CHECKS):
                    break
                last = str(video)
                try:
                    number = video_number(video)
                    if not number:
                        report["unidentified"] += 1
                        add_detail(video.name, "未识别番号")
                        continue
                    stat = video.stat()
                    if not S_ISREG(stat.st_mode):
                        continue
                    stamp = [stat.st_size, stat.st_mtime_ns]
                    key = os.path.normcase(str(video))
                    if not force and stamp == cache.get(key, [])[:2]:
                        report["cached"] += 1
                        continue
                except OSError as error:
                    log_failure(video.name, error)
                    continue
                try:
                    checked += 1
                    existing = has_chinese_sidecar(video)
                    if not existing and video.suffix.lower() != ".strm":
                        if not ffprobe:
                            raise OSError("ffprobe 不可用，无法确认内嵌字幕")
                        existing = has_chinese_embedded(video, ffprobe, ffmpeg)
                    if existing:
                        report["existing"] += 1
                    else:
                        report["searched"] += 1
                        result = provider.search_and_download(number, video)
                        if stopped.is_set():
                            break
                        current = video.stat()
                        if stamp != [current.st_size, current.st_mtime_ns]:
                            continue
                        if not result:
                            report["missing"] += 1
                            add_detail(video.name, f"{number} · Subtitle Cat 暂无匹配中文字幕")
                        elif has_chinese_sidecar(video):
                            report["existing"] += 1
                        elif not stopped.is_set():
                            destination = write_subtitle(video, *result)
                            if destination:
                                logger.info(f"JAV字幕助手：已保存 {destination.name}")
                                report["saved"] += 1
                                add_detail(video.name, f"已保存 {destination.name}")
                    cache[key] = [*stamp, now + 86400]
                except (OSError, ValueError, SubprocessError) as error:
                    log_failure(video.name, error)
                    cache[key] = [*stamp, now + 3600]
            if not stopped.is_set():
                records = sorted(cache.items(), key=lambda item: item[1][2])[-self.MAX_CACHE:]
                self.save_data("scan_state", {"checked": dict(records), "cursor": last})
                self.save_data("last_scan", {**report, "details": details,
                    "finished": datetime.now(ZoneInfo(settings.TZ)).strftime("%Y-%m-%d %H:%M:%S")})
            logger.info(f"JAV字幕助手：扫描结束，联网 {report['searched']}，写入 {report['saved']}，失败 {report['failed']}")
        finally:
            self._scan_lock.release()
