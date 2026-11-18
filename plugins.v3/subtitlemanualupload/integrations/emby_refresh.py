from __future__ import annotations

import re
from importlib import import_module
from typing import Any, Iterable
from urllib.parse import quote


_EXTERNAL_SUBTITLE_SCAN_TASK_KEYS = (
    "ScanExternalTrackTask",
    "ScanExternalSubtitleTask",
)
_REQUEST_TIMEOUT_SECONDS = 15


class EmbyRefreshService:
    """Read subtitle tracks and trigger external subtitle scans on configured Emby servers."""

    def __init__(self, logger: Any) -> None:
        self._logger = logger

    def _configured_instances(self, *, emby_only: bool = False) -> dict[str, Any]:
        """Read configured instances across MoviePilot V2 and V3 service APIs."""
        for module_name in ("app.application.mediaserver", "app.helper.mediaserver"):
            try:
                helper_module = import_module(module_name)
            except ModuleNotFoundError as exc:
                if exc.name not in {module_name, module_name.rsplit(".", 1)[0]}:
                    raise
                continue
            try:
                services = helper_module.MediaServerHelper().get_services() or {}
            except Exception as exc:
                self._logger.warning("[SubtitleManualUpload] 读取 MoviePilot 媒体服务器目录失败: %s", exc)
                continue
            instances = {
                name or "default": service.instance
                for name, service in services.items()
                if (not emby_only or getattr(service, "type", None) == "emby")
                and getattr(service, "instance", None) is not None
                and not self._is_inactive(service.instance)
            }
            if instances:
                return instances

        try:
            try:
                module_manager = import_module("app.sdk.plugin").ModuleManager
            except ModuleNotFoundError as exc:
                if exc.name not in {"app.sdk", "app.sdk.plugin"}:
                    raise
                module_manager = import_module("app.core.module").ModuleManager
            module = module_manager().get_running_module("EmbyModule")
            return dict(module.get_instances()) if module else {}
        except Exception as exc:
            self._logger.warning("[SubtitleManualUpload] 读取 MoviePilot Emby 实例失败: %s", exc)
            return {}

    @staticmethod
    def _is_inactive(instance: Any) -> bool:
        inactive = getattr(instance, "is_inactive", None)
        return bool(inactive()) if callable(inactive) else False

    def refresh_for_videos(self, targets: Iterable[dict[str, Any]]) -> bool:
        """Prefer Strm Assistant's external subtitle task, then refresh the library if absent."""
        if not any(target for target in targets):
            return False

        instances = self._configured_instances()
        if not instances:
            self._logger.info("[SubtitleManualUpload] 未配置可用的 Emby 实例，跳过外挂字幕扫描")
            return False

        try:
            request_utils = self._request_utils()
        except Exception as exc:
            self._logger.warning("[SubtitleManualUpload] MoviePilot HTTP 客户端不可用: %s", exc)
            return False

        started = False
        for name, instance in instances.items():
            if self._start_external_track_scan(name, instance, request_utils):
                started = True
        return started

    @staticmethod
    def _media_path(value: Any) -> str:
        return value.strip().replace("\\", "/").rstrip("/") if isinstance(value, str) else ""

    @classmethod
    def _mapped_path(cls, path: Any, mappings: str) -> str:
        path = cls._media_path(path)
        prefixes = []
        for line in mappings.splitlines():
            local, separator, emby = line.partition("=>")
            local, emby = cls._media_path(local), cls._media_path(emby)
            if separator and local and emby and (path == local or path.startswith(local + "/")):
                prefixes.append((local, emby))
        if prefixes:
            local, emby = max(prefixes, key=lambda pair: len(pair[0]))
            return emby + path[len(local):]
        return path

    def subtitle_tracks_for_entry(self, entry: dict, mappings: str = "") -> tuple[str, list[dict]]:
        """Return ready, pending, or unavailable for this exact media version."""
        path = self._mapped_path(entry.get("path"), mappings)
        if not re.match(r"^(?:/|[A-Za-z]:/)", path):
            return "unavailable", []
        try:
            instances = self._configured_instances(emby_only=True)
            request = self._request_utils() if instances else None
        except Exception:
            return "unavailable", []

        pending = False
        for instance in instances.values():
            host = getattr(instance, "_host", None)
            key = getattr(instance, "_apikey", None)
            if not host or not key or self._is_inactive(instance):
                continue
            base_url = host.rstrip("/") + "/emby/"
            user_id = getattr(instance, "user", None)
            headers = {"X-Emby-Token": key}
            user = {"UserId": user_id} if user_id else {}

            def read(endpoint: str, **params: Any) -> dict:
                response = request.get_res(
                    base_url + endpoint,
                    params={**params, **user},
                    headers=headers,
                    timeout=3,
                    allow_redirects=False,
                )
                if response is None or not 200 <= response.status_code < 300:
                    raise ValueError("Emby 查询失败")
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Emby 响应格式错误")
                return payload

            def items(**params: Any) -> list[dict]:
                payload = read("Items", Fields="Path,MediaSources,AlternateMediaSources", Limit=8, **params)
                values = payload.get("Items")
                if not isinstance(values, list):
                    raise ValueError("Emby 媒体列表格式错误")
                return [item for item in values if isinstance(item, dict)]

            is_episode = entry.get("media_type") == "tv"
            kind = "Episode" if is_episode else "Movie"
            try:
                matches = items(Path=path, Recursive=True, IncludeItemTypes=kind)
                if not matches:
                    matches = self._media_candidates(entry, items)
                status, tracks = self._tracks_for_exact_version(matches, path, read)
            except Exception:
                # HTTP errors can contain the server URL and credentials.
                continue
            if status == "ready":
                return status, tracks
            pending = pending or status == "pending"
        return ("pending" if pending else "unavailable"), []

    @staticmethod
    def _media_candidates(entry: dict, items: Any) -> list[dict]:
        # Emby may index only the primary version's path on the library item.
        tmdb_id = str(entry.get("tmdb_id") or "")
        if not tmdb_id.isdecimal() or tmdb_id == "0":
            return []
        if entry.get("media_type") != "tv":
            return items(AnyProviderIdEquals=f"tmdb.{tmdb_id}", Recursive=True, IncludeItemTypes="Movie")
        season, episode = str(entry.get("season") or ""), str(entry.get("episode") or "")
        if not season.isdecimal() or not episode.isdecimal():
            return []
        series = items(AnyProviderIdEquals=f"tmdb.{tmdb_id}", Recursive=True, IncludeItemTypes="Series")
        return [
            item
            for show in series if show.get("Id")
            for item in items(
                ParentId=show["Id"],
                Recursive=True,
                IncludeItemTypes="Episode",
                ParentIndexNumber=season,
                MinIndexNumber=episode,
            )
            if str(item.get("ParentIndexNumber")) == season and str(item.get("IndexNumber")) == episode
        ]

    @classmethod
    def _tracks_for_exact_version(cls, items: list[dict], path: str, read: Any) -> tuple[str, list[dict]]:
        matches = []
        for item in items:
            item_path = cls._media_path(item.get("Path"))

            def matching_sources(sources: Any) -> list[dict]:
                if not isinstance(sources, list):
                    return []
                sources = [source for source in sources if isinstance(source, dict)]
                exact = [source for source in sources if cls._media_path(source.get("Path")) == path]
                if exact:
                    return exact
                # A STRM source may report its playback URL in place of the STRM file path.
                if item_path == path and path.lower().endswith(".strm") and len(sources) == 1:
                    source_path = cls._media_path(sources[0].get("Path"))
                    if not source_path or "://" in source_path:
                        return sources
                return []

            exact = matching_sources(item.get("MediaSources"))
            if not exact or any(not source.get("MediaStreams") for source in exact):
                item_id = str(item.get("Id") or "")
                if re.fullmatch(r"[A-Za-z0-9_-]+", item_id):
                    playback = read(f"Items/{item_id}/PlaybackInfo")
                    exact = matching_sources(playback.get("MediaSources"))
            matches.extend(exact)

        if len(matches) > 1:
            return "unavailable", []
        streams = matches[0].get("MediaStreams") if matches else None
        if not isinstance(streams, list) or not any(
            isinstance(stream, dict) and stream.get("Type") in {"Video", "Audio", "Subtitle"}
            for stream in streams
        ):
            return "pending", []
        return "ready", [
            stream for stream in streams
            if isinstance(stream, dict) and stream.get("Type") == "Subtitle"
        ]

    def _start_external_track_scan(self, name: str, instance: Any, request_utils: Any) -> bool:
        host = getattr(instance, "_host", None)
        api_key = getattr(instance, "_apikey", None)
        if not host or not api_key:
            self._logger.warning("[SubtitleManualUpload] Emby 实例缺少连接信息 name=%s", name)
            return False

        base_url = host.rstrip("/") + "/"
        params = {"api_key": api_key}
        try:
            response = request_utils.get_res(
                base_url + "emby/ScheduledTasks",
                params=params,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            if response is None or not 200 <= response.status_code < 300:
                status = getattr(response, "status_code", None)
                self._logger.warning("[SubtitleManualUpload] 查询 Emby 计划任务失败 name=%s status=%s", name, status)
                return False

            tasks = response.json()
            if isinstance(tasks, dict):
                tasks = tasks.get("Items", [])
            if not isinstance(tasks, list):
                self._logger.warning("[SubtitleManualUpload] Emby 计划任务响应格式错误 name=%s", name)
                return False

            task = next((
                item for item in tasks
                if isinstance(item, dict) and item.get("Key") in _EXTERNAL_SUBTITLE_SCAN_TASK_KEYS
            ), None)
            if task is None:
                return self._refresh_library(name, base_url, params, request_utils)
            if not task.get("Id"):
                self._logger.warning(
                    "[SubtitleManualUpload] 找到神医助手外挂字幕扫描任务但缺少任务 ID name=%s key=%s",
                    name,
                    task.get("Key"),
                )
                return False

            task_id = str(task["Id"])
            if task.get("State") == "Running":
                self._logger.info("[SubtitleManualUpload] 神医助手外挂字幕扫描已在运行 name=%s task_id=%s", name, task_id)
                return True

            response = request_utils.post_res(
                base_url + "emby/ScheduledTasks/Running/" + quote(task_id, safe=""),
                params=params,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            if response is None or not 200 <= response.status_code < 300:
                status = getattr(response, "status_code", None)
                self._logger.warning(
                    "[SubtitleManualUpload] 启动神医助手外挂字幕扫描失败 name=%s task_id=%s status=%s",
                    name,
                    task_id,
                    status,
                )
                return False

            self._logger.info(
                "[SubtitleManualUpload] 已启动神医助手外挂字幕扫描 name=%s task_id=%s",
                name,
                task_id,
            )
            return True
        except Exception as exc:
            self._logger.warning("[SubtitleManualUpload] 调用神医助手外挂字幕扫描失败 name=%s error=%s", name, exc)
            return False

    def _refresh_library(self, name: str, base_url: str, params: dict, request_utils: Any) -> bool:
        """Use Emby's supported full-library refresh only when no dedicated task exists."""
        self._logger.info("[SubtitleManualUpload] 未找到神医助手外挂字幕任务，改为全库刷新 name=%s", name)
        try:
            response = request_utils.post_res(
                base_url + "emby/Library/Refresh",
                params=params,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            self._logger.warning("[SubtitleManualUpload] Emby 全库刷新请求异常 name=%s error=%s", name, exc)
            return False
        if response is None or not 200 <= response.status_code < 300:
            status = getattr(response, "status_code", None)
            self._logger.warning("[SubtitleManualUpload] Emby 全库刷新失败 name=%s status=%s", name, status)
            return False
        self._logger.info("[SubtitleManualUpload] 已触发 Emby 全库刷新 name=%s", name)
        return True

    @staticmethod
    def _request_utils() -> Any:
        """Load the MoviePilot HTTP wrapper from either supported generation."""
        try:
            module = import_module("app.adapters.network.http")
        except ModuleNotFoundError as exc:
            if exc.name not in {"app.adapters", "app.adapters.network", "app.adapters.network.http"}:
                raise
            module = import_module("app.utils.http")
        return module.RequestUtils()
