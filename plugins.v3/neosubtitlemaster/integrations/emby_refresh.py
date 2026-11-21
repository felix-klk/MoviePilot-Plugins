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

    def _configured_instances(
        self, *, emby_only: bool = False, selected_names: set[str] | None = None,
    ) -> dict[str, Any]:
        """Read configured instances across MoviePilot V2 and V3 service APIs."""
        if selected_names is not None and not selected_names:
            return {}
        for module_name in ("app.application.mediaserver", "app.helper.mediaserver"):
            try:
                helper_module = import_module(module_name)
            except ModuleNotFoundError as exc:
                if exc.name not in {module_name, module_name.rsplit(".", 1)[0]}:
                    raise
                continue
            try:
                helper = helper_module.MediaServerHelper()
                services = (helper.get_services(type_filter="emby", name_filters=list(selected_names))
                            if selected_names is not None else helper.get_services()) or {}
            except Exception as exc:
                self._logger.warning("[NeoSubtitleMaster] 读取 MoviePilot 媒体服务器目录失败: %s", exc)
                continue
            instances = {
                name or "default": service.instance
                for name, service in services.items()
                if (selected_names is None or name in selected_names)
                and (not emby_only or getattr(service, "type", None) == "emby")
                and getattr(service, "instance", None) is not None
                and (selected_names is not None or not self._is_inactive(service.instance))
            }
            if instances or selected_names is not None:
                return instances

        if selected_names is not None:
            return {}
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
            self._logger.warning("[NeoSubtitleMaster] 读取 MoviePilot Emby 实例失败: %s", exc)
            return {}

    @staticmethod
    def _is_inactive(instance: Any) -> bool:
        inactive = getattr(instance, "is_inactive", None)
        return bool(inactive()) if callable(inactive) else False

    @staticmethod
    def configured_emby_names() -> list[str]:
        helper = import_module("app.application.mediaserver").MediaServerHelper()
        return sorted(
            (name for name, config in helper.get_configs().items() if config.type == "emby"),
            key=str.casefold,
        )

    def libraries_for_server(self, server: str) -> list[dict[str, Any]]:
        instance = self._configured_instances(emby_only=True, selected_names={server}).get(server)
        if not self._can_query(instance):
            raise RuntimeError("Emby 实例不可用")
        try:
            read = self._reader(instance, self._request_utils())
            libraries = {}
            offset = 0
            while True:
                page = read("Library/VirtualFolders/Query", StartIndex=offset, Limit=100)
                rows = page.get("Items")
                if not isinstance(rows, list):
                    raise ValueError("Emby 媒体库列表格式错误")
                for item in rows:
                    if not isinstance(item, dict):
                        continue
                    library_id = str(item.get("ItemId") or item.get("Id") or "")
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", library_id):
                        continue
                    options = item.get("LibraryOptions") or {}
                    locations = item.get("Locations") or []
                    path_infos = options.get("PathInfos") or []
                    paths = [*locations, *(
                        info.get(field) for info in path_infos if isinstance(info, dict)
                        for field in ("Path", "NetworkPath")
                    )]
                    libraries[library_id] = {
                        "server": server,
                        "server_id": self._server_id(instance),
                        "id": library_id,
                        "name": str(item.get("Name") or library_id),
                        "paths": list(dict.fromkeys(path for path in paths if isinstance(path, str) and path)),
                    }
                offset += len(rows)
                total = page.get("TotalRecordCount")
                if not rows or not isinstance(total, int) or offset >= total:
                    break
            return sorted(libraries.values(), key=lambda item: item["name"].casefold())
        except Exception:
            raise RuntimeError("读取 Emby 媒体库失败") from None

    def refresh_for_videos(
        self, targets: Iterable[dict[str, Any]], *, scope: list[dict] | None = None, mappings: str = "",
    ) -> bool:
        """Prefer Strm Assistant's external subtitle task, then refresh the library if absent."""
        targets = [target for target in targets if target]
        if not targets:
            return False

        matching = {}
        if scope is not None:
            for target in targets:
                path = self._mapped_path(target.get("path"), mappings)
                for name, libraries in self._libraries_for_path(scope, path, require_root=True).items():
                    matching.setdefault(name, []).extend(libraries)
            if not matching:
                return False
        instances = (self._configured_instances(emby_only=True, selected_names=set(matching))
                     if scope is not None else self._configured_instances())
        if not instances:
            self._logger.info("[NeoSubtitleMaster] 未配置可用的 Emby 实例，跳过外挂字幕扫描")
            return False

        try:
            request_utils = self._request_utils()
        except Exception as exc:
            self._logger.warning("[NeoSubtitleMaster] MoviePilot HTTP 客户端不可用: %s", exc)
            return False

        started = False
        for name, instance in instances.items():
            if scope is not None and (
                not self._can_query(instance)
                or not any(self._same_server(instance, item) for item in matching[name])
            ):
                continue
            if self._start_external_track_scan(name, instance, request_utils):
                started = True
        return started

    @staticmethod
    def _media_path(value: Any) -> str:
        return value.strip().replace("\\", "/").rstrip("/") if isinstance(value, str) else ""

    @classmethod
    def _under_root(cls, path: str, root: str) -> bool:
        normalized = cls._media_path(root)
        if not normalized:
            return root.strip() == "/" and path.startswith("/")
        if normalized.startswith("//") or re.match(r"^[A-Za-z]:", normalized):
            path, normalized = path.casefold(), normalized.casefold()
        return path == normalized or path.startswith(normalized + "/")

    @classmethod
    def _libraries_for_path(cls, scope: list[dict], path: str, *, require_root: bool = False) -> dict[str, list[dict]]:
        result: dict[str, list[dict]] = {}
        if not re.match(r"^(?:/|[A-Za-z]:/)", path):
            return result
        for library in scope:
            paths = library["paths"]
            if (not paths and not require_root) or any(cls._under_root(path, root) for root in paths):
                result.setdefault(library["server"], []).append(library)
        return result

    @staticmethod
    def _server_id(instance: Any) -> str:
        value = getattr(instance, "serverid", "")
        return value if isinstance(value, str) else ""

    @classmethod
    def _same_server(cls, instance: Any, library: dict) -> bool:
        expected = library.get("server_id")
        actual = cls._server_id(instance)
        return not expected or not actual or expected == actual

    @classmethod
    def _can_query(cls, instance: Any) -> bool:
        return bool(getattr(instance, "_host", None) and getattr(instance, "_apikey", None)
                    and not cls._is_inactive(instance))

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

    @staticmethod
    def _reader(instance: Any, request: Any) -> Any:
        base_url = instance._host.rstrip("/") + "/emby/"
        headers = {"X-Emby-Token": instance._apikey}
        user_id = getattr(instance, "user", None)
        user = {"UserId": user_id} if user_id else {}

        def read(endpoint: str, **params: Any) -> dict:
            response = request.get_res(
                base_url + endpoint, params={**params, **user}, headers=headers,
                timeout=3, allow_redirects=False,
            )
            if response is None:
                raise ValueError("Emby 查询失败")
            try:
                if not 200 <= response.status_code < 300:
                    raise ValueError("Emby 查询失败")
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Emby 响应格式错误")
                return payload
            finally:
                response.close()
        return read

    def subtitle_tracks_for_entry(
        self, entry: dict, mappings: str = "", *, scope: list[dict] | None = None,
    ) -> tuple[str, list[dict]]:
        """Read tracks for the exact version within the configured scope."""
        path = self._mapped_path(entry.get("path"), mappings)
        if not re.match(r"^(?:/|[A-Za-z]:/)", path):
            return "unavailable", []
        matching = self._libraries_for_path(scope, path) if scope is not None else {}
        if scope is not None and not matching:
            return "out_of_scope", []
        try:
            instances = (self._configured_instances(emby_only=True, selected_names=set(matching))
                         if scope is not None else self._configured_instances(emby_only=True))
            request = self._request_utils() if instances else None
        except Exception:
            return ("offline" if scope is not None else "unavailable"), []

        offline = bool(matching.keys() - instances.keys())
        pending = False
        for name, instance in instances.items():
            if not self._can_query(instance):
                offline = offline or scope is not None
                continue
            read = self._reader(instance, request)

            def items(parent_id: str | None, **params: Any) -> list[dict]:
                if parent_id:
                    params.setdefault("ParentId", parent_id)
                payload = read("Items", Fields="Path,MediaSources,AlternateMediaSources", Limit=8, **params)
                values = payload.get("Items")
                if not isinstance(values, list):
                    raise ValueError("Emby 媒体列表格式错误")
                return [item for item in values if isinstance(item, dict)]

            is_episode = entry.get("media_type") == "tv"
            kind = "Episode" if is_episode else "Movie"
            libraries = matching[name] if scope is not None else [None]
            for library in libraries:
                if library is not None and not self._same_server(instance, library):
                    offline = True
                    continue
                library_id = library["id"] if library is not None else None
                try:
                    matches = items(library_id, Path=path, Recursive=True, IncludeItemTypes=kind)
                    if not matches:
                        matches = self._media_candidates(entry, lambda **params: items(library_id, **params))
                    status, tracks = self._tracks_for_exact_version(matches, path, read)
                except Exception:
                    # HTTP errors can contain the server URL and credentials.
                    offline = offline or scope is not None
                    break
                if status == "ready":
                    return status, tracks
                pending = pending or status == "pending"
        if offline:
            return "offline", []
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
            self._logger.warning("[NeoSubtitleMaster] Emby 实例缺少连接信息 name=%s", name)
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
                self._logger.warning("[NeoSubtitleMaster] 查询 Emby 计划任务失败 name=%s status=%s", name, status)
                return False

            tasks = response.json()
            if isinstance(tasks, dict):
                tasks = tasks.get("Items", [])
            if not isinstance(tasks, list):
                self._logger.warning("[NeoSubtitleMaster] Emby 计划任务响应格式错误 name=%s", name)
                return False

            task = next((
                item for item in tasks
                if isinstance(item, dict) and item.get("Key") in _EXTERNAL_SUBTITLE_SCAN_TASK_KEYS
            ), None)
            if task is None:
                return self._refresh_library(name, base_url, params, request_utils)
            if not task.get("Id"):
                self._logger.warning(
                    "[NeoSubtitleMaster] 找到神医助手外挂字幕扫描任务但缺少任务 ID name=%s key=%s",
                    name,
                    task.get("Key"),
                )
                return False

            task_id = str(task["Id"])
            if task.get("State") == "Running":
                self._logger.info("[NeoSubtitleMaster] 神医助手外挂字幕扫描已在运行 name=%s task_id=%s", name, task_id)
                return True

            response = request_utils.post_res(
                base_url + "emby/ScheduledTasks/Running/" + quote(task_id, safe=""),
                params=params,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            if response is None or not 200 <= response.status_code < 300:
                status = getattr(response, "status_code", None)
                self._logger.warning(
                    "[NeoSubtitleMaster] 启动神医助手外挂字幕扫描失败 name=%s task_id=%s status=%s",
                    name,
                    task_id,
                    status,
                )
                return False

            self._logger.info(
                "[NeoSubtitleMaster] 已启动神医助手外挂字幕扫描 name=%s task_id=%s",
                name,
                task_id,
            )
            return True
        except Exception as exc:
            self._logger.warning("[NeoSubtitleMaster] 调用神医助手外挂字幕扫描失败 name=%s error=%s", name, exc)
            return False

    def _refresh_library(self, name: str, base_url: str, params: dict, request_utils: Any) -> bool:
        """Use Emby's supported full-library refresh only when no dedicated task exists."""
        self._logger.info("[NeoSubtitleMaster] 未找到神医助手外挂字幕任务，改为全库刷新 name=%s", name)
        try:
            response = request_utils.post_res(
                base_url + "emby/Library/Refresh",
                params=params,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            self._logger.warning("[NeoSubtitleMaster] Emby 全库刷新请求异常 name=%s error=%s", name, exc)
            return False
        if response is None or not 200 <= response.status_code < 300:
            status = getattr(response, "status_code", None)
            self._logger.warning("[NeoSubtitleMaster] Emby 全库刷新失败 name=%s status=%s", name, status)
            return False
        self._logger.info("[NeoSubtitleMaster] 已触发 Emby 全库刷新 name=%s", name)
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
