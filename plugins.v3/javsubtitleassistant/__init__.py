"""MoviePilot V3 entry point for the JAV Subtitle Assistant."""

import os
import shutil
from itertools import chain, islice
from pathlib import Path
from subprocess import SubprocessError
from threading import Event, Lock
from time import time
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .media import SAMPLE, VIDEOS, has_chinese_embedded, has_chinese_sidecar, video_number, write_subtitle
from .subtitlecat import find_chinese_subtitle


class JavSubtitleAssistant(_PluginBase):
    plugin_name = "JAV字幕助手"
    plugin_desc = "扫描本地整理目录，从 Subtitle Cat 下载缺少的中文字幕。"
    plugin_icon = ""
    plugin_version = "1.0.0"
    plugin_author = "felix-klk"
    author_url = "https://github.com/felix-klk"
    plugin_config_prefix = "javsubtitleassistant_"
    plugin_order = 50
    auth_level = 1

    DEFAULTS = {"enabled": False, "folders": "", "cron": "0 * * * *"}
    MAX_SEARCHES = 40
    MAX_CHECKS = 100
    MAX_CACHE = 8000

    def __init__(self):
        super().__init__()
        self._folders = ()
        self._trigger = None
        self._stopped = Event()
        self._stopped.set()
        self._scan_lock = Lock()

    def init_plugin(self, config: dict | None = None) -> None:
        """Read local paths and validate the schedule before exposing it to MoviePilot."""
        self.stop_service()
        values = self.DEFAULTS | (config or {})
        if not values["enabled"]:
            return
        folders = dict.fromkeys(Path(line.strip()) for line in str(values["folders"]).splitlines()
                                if line.strip())
        self._folders = tuple(folder for folder in folders if folder.is_absolute())
        if len(self._folders) != len(folders):
            logger.warning("JAV字幕助手：已忽略非绝对目录")
        if not self._folders:
            logger.warning("JAV字幕助手：请填写至少一个整理目录的绝对路径")
            return
        try:
            self._trigger = CronTrigger.from_crontab(str(values["cron"]), timezone=ZoneInfo(settings.TZ))
        except ValueError as error:
            logger.error(f"JAV字幕助手：Cron 表达式无效，{error}")
            return
        self._stopped = Event()

    def get_state(self) -> bool:
        return not self._stopped.is_set()

    @staticmethod
    def get_command() -> list:
        return []

    def get_api(self) -> list:
        return []

    def get_form(self) -> tuple[list, dict]:
        return [
            {
                "component": "VForm",
                "content": [
                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用 JAV字幕助手"}},
                    {"component": "VTextarea", "props": {
                        "model": "folders", "label": "转移后的本地目录",
                        "hint": "填写 MoviePilot 内能访问的绝对路径，每行一个。", "persistent-hint": True,
                        "rows": 3,
                    }},
                    {"component": "VTextField", "props": {
                        "model": "cron", "label": "定时扫描（Cron）",
                        "hint": "默认每小时扫描。保存后也可在服务页面运行。", "persistent-hint": True,
                    }},
                ],
            }
        ], self.DEFAULTS.copy()

    def get_page(self) -> None:
        return None

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

    def _videos(self, folders: tuple[Path, ...], stopped: Event):
        """Walk the configured folders without following nested directory symlinks."""
        seen = set()
        for folder in folders:
            try:
                root = folder.resolve(strict=True)
                if not root.is_dir():
                    raise NotADirectoryError(str(folder))
            except (OSError, RuntimeError) as error:
                logger.warning(f"JAV字幕助手：无法扫描 {folder}，{error}")
                continue
            for directory, subdirs, files in os.walk(root, followlinks=False):
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

    def scan(self) -> None:
        """Scan one batch and download at most one Chinese subtitle per video."""
        stopped, folders = self._stopped, self._folders
        if stopped.is_set() or not self._scan_lock.acquire(blocking=False):
            return
        try:
            now = time()
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
            videos = list(self._videos(folders, stopped))
            cursor = state.get("cursor")
            start = next((index + 1 for index, video in enumerate(videos) if str(video) == cursor), 0)
            checked = searched = saved = failed = 0
            last = ""
            for video in chain(islice(videos, start, None), islice(videos, start)):
                if stopped.is_set() or searched >= self.MAX_SEARCHES or checked >= self.MAX_CHECKS:
                    break
                last = str(video)
                try:
                    checked += 1
                    number = video_number(video)
                    if not number or not video.is_file():
                        continue
                    stat = video.stat()
                    stamp = [stat.st_size, stat.st_mtime_ns]
                    key = os.path.normcase(str(video))
                    if stamp == cache.get(key, [])[:2]:
                        continue
                except OSError as error:
                    logger.warning(f"JAV字幕助手：读取 {video.name} 失败，{error}")
                    failed += 1
                    continue
                try:
                    existing = has_chinese_sidecar(video)
                    if not existing and video.suffix.lower() != ".strm":
                        if not ffprobe:
                            raise OSError("ffprobe 不可用，无法确认内嵌字幕")
                        existing = has_chinese_embedded(video, ffprobe, ffmpeg)
                    if not existing:
                        searched += 1
                        result = find_chinese_subtitle(number, video)
                        if stopped.is_set():
                            break
                        current = video.stat()
                        if stamp != [current.st_size, current.st_mtime_ns]:
                            continue
                        if result and not has_chinese_sidecar(video) and not stopped.is_set():
                            destination = write_subtitle(video, *result)
                            if destination:
                                logger.info(f"JAV字幕助手：已保存 {destination.name}")
                                saved += 1
                    cache[key] = [*stamp, now + 86400]
                except (OSError, ValueError, SubprocessError) as error:
                    failed += 1
                    cache[key] = [*stamp, now + 3600]
                    logger.warning(f"JAV字幕助手：处理 {video.name} 失败，{error}")
            if not stopped.is_set():
                records = sorted(cache.items(), key=lambda item: item[1][2])[-self.MAX_CACHE:]
                self.save_data("scan_state", {"checked": dict(records), "cursor": last})
            logger.info(f"JAV字幕助手：扫描结束，联网搜索 {searched}，写入 {saved}，失败 {failed}")
        finally:
            self._scan_lock.release()
