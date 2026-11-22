from __future__ import annotations

import asyncio
from dataclasses import replace
import inspect
import io
import importlib.util
import json
import re
import shutil
import sys
import types
import zipfile
from pathlib import Path

import pytest


def load_plugin_module(generation="plugins.v2", plugin_id="SubtitleManualUpload"):
    root = Path(__file__).resolve().parents[1]
    package_dir = root / generation / plugin_id.lower()

    class HTTPException(Exception):
        def __init__(self, status_code=500, detail=""):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class PluginBase:
        def get_data_path(self):
            return root / ".tmp-test-data"

    class FakeTransferHistory:
        calls = 0
        data = []

        @classmethod
        def list_by_page(cls, **kwargs):
            cls.calls += 1
            return list(cls.data)

    class FakeSiteOper:
        data = []

        def list(self):
            return list(self.data)

    async def run_in_threadpool(func, *args, **kwargs):
        return func(*args, **kwargs)

    modules = {
        "fastapi": types.SimpleNamespace(HTTPException=HTTPException, Request=object),
        "starlette": types.ModuleType("starlette"),
        "starlette.concurrency": types.SimpleNamespace(run_in_threadpool=run_in_threadpool),
        "starlette.datastructures": types.SimpleNamespace(UploadFile=object),
        "app": types.ModuleType("app"),
        "app.core": types.ModuleType("app.core"),
        "app.core.config": types.SimpleNamespace(
            settings=types.SimpleNamespace(TMDB_IMAGE_DOMAIN="image.tmdb.org", RMT_MEDIAEXT={".mkv", ".mp4", ".strm"})
        ),
        "app.core.metainfo": types.SimpleNamespace(MetaInfoPath=lambda path: types.SimpleNamespace()),
        "app.db": types.ModuleType("app.db"),
        "app.db.site_oper": types.SimpleNamespace(SiteOper=FakeSiteOper),
        "app.db.models": types.ModuleType("app.db.models"),
        "app.db.models.transferhistory": types.SimpleNamespace(TransferHistory=FakeTransferHistory),
        "app.log": types.SimpleNamespace(
            logger=types.SimpleNamespace(
                info=lambda *args, **kwargs: None,
                warning=lambda *args, **kwargs: None,
                error=lambda *args, **kwargs: None,
            )
        ),
        "app.plugins": types.SimpleNamespace(_PluginBase=PluginBase),
    }
    if generation == "plugins.v3":
        modules.update({
            "app.sdk": types.ModuleType("app.sdk"),
            "app.schemas": types.ModuleType("app.schemas"),
            "app.schemas.types": types.SimpleNamespace(
                MediaSource=types.SimpleNamespace(
                    TMDB=types.SimpleNamespace(value="themoviedb"),
                    Douban=types.SimpleNamespace(value="douban"),
                ),
            ),
            "app.db.oper": types.ModuleType("app.db.oper"),
            "app.db.oper.transferhistory": types.SimpleNamespace(TransferHistoryOper=FakeTransferHistory),
            "app.sdk.config": modules["app.core.config"],
            "app.sdk.logging": modules["app.log"],
            "app.sdk.media": types.SimpleNamespace(
                MetaInfoPath=modules["app.core.metainfo"].MetaInfoPath,
                build_media_key=lambda *_args, **_kwargs: "",
                resolve_media_identity=lambda *_args, **_kwargs: None,
            ),
        })
    for name, module in modules.items():
        sys.modules[name] = module

    package_name = f"{plugin_id.lower()}_{generation.rsplit('.', 1)[-1]}_cache_testpkg"
    for name in list(sys.modules):
        if name == package_name or name.startswith(f"{package_name}."):
            sys.modules.pop(name, None)

    spec = importlib.util.spec_from_file_location(
        package_name,
        package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    return module, FakeTransferHistory, FakeSiteOper


def make_plugin(module, plugin_id="SubtitleManualUpload"):
    plugin_class = getattr(module, plugin_id)
    plugin = plugin_class.__new__(plugin_class)
    plugin._local_entries_cache = {"loaded_at": None, "entries": [], "media_count": 0, "persisted": False}
    plugin._entry_map = module.OrderedDict()
    plugin._media_index_cache = module.OrderedDict()
    plugin._auto_transfer_tasks = module.OrderedDict()
    plugin._auto_transfer_worker = None
    plugin._auto_transfer_stopping = False
    plugin._auto_season_package_cache = module.OrderedDict()
    plugin_class._embedded_subtitle_probe_cache = module.OrderedDict()
    plugin._cache_refreshing = False
    plugin._cache_ttl_seconds = 1800
    plugin._cache_max_entries = 10
    plugin._entry_map_max_size = 2
    plugin._media_index_cache_max_keys = 20
    plugin._match_history_cache_ttl_seconds = 86400
    plugin._timeline_task_ttl_seconds = 86400
    plugin._match_history_cache = {"loaded_at": None, "signature": "", "items": [], "entry_count": 0, "persisted": False}
    plugin._timeline_tasks = module.OrderedDict()
    plugin._ai_link_enabled = False
    plugin._auto_skip_chinese_media_on_transfer = True
    plugin._auto_transfer_subtitle_strategy = "online_then_ai_source"
    plugin._auto_multi_subtitle_mode = "best"
    # General cache/AI cases allow monolingual subtitles; bilingual cases opt in explicitly.
    plugin._auto_subtitle_language_priority = ["chi", "cht", "eng"]
    plugin._auto_subtitle_format_priority = [".ass", ".srt", ".ssa", ".vtt", ".webvtt", ".sbv", ".sub"]
    plugin._auto_ass_to_srt_for_ai = True
    plugin._auto_search_min_score = 20
    plugin._online_provider_ids = ["assrt"]
    plugin._online_rate_records = {}
    plugin._online_rate_limit_per_minute = 5
    plugin._transfer_auto_dedupe_seconds = 300
    plugin._transfer_auto_recent = {}
    plugin._transfer_auto_lock = module.threading.Lock()
    plugin._tmdb_detail_cache = {}
    plugin._build_entry_from_history = lambda history: dict(history)
    for cache_file in [
        plugin._local_media_catalog().local_cache_file(),
        plugin._subtitle_history().match_history_cache_file(),
    ]:
        if cache_file.exists():
            cache_file.unlink()
    return plugin


def plugin_submodule(module, name):
    return sys.modules[f"{module.__name__}.{name}"]


def test_target_normalizers_are_reexported_from_target_resolver(tmp_path):
    module, _, _ = load_plugin_module()
    target_resolver = plugin_submodule(module, "catalog.target_resolver")
    target_normalizers = plugin_submodule(module, "catalog.target_normalizers")

    for name in [
        "media_type_text",
        "poster_url",
        "history_type_text",
        "number_from_tag",
        "extract_episode_hint",
        "is_local_video_path",
        "event_value",
        "is_stream_path",
        "entry_path_is_valid",
        "entry_filesystem_signature",
        "entry_matches_keyword",
    ]:
        assert callable(getattr(target_resolver, name))
        assert callable(getattr(target_normalizers, name))

    video = tmp_path / "Show.S01E02.mkv"
    video.write_text("video")
    entry = {
        "storage": "local",
        "path": str(video),
        "title": "Example Show",
        "filename": video.name,
        "basename": video.stem,
        "relative_path": "Season 1/Show.S01E02.mkv",
    }

    assert target_resolver.extract_episode_hint(video.name) == {"season": 1, "episode": 2}
    assert target_resolver.extract_episode_hint(video.name) == target_normalizers.extract_episode_hint(video.name)
    assert target_resolver.entry_path_is_valid(entry)
    assert target_resolver.entry_path_is_valid(entry) == target_normalizers.entry_path_is_valid(entry)
    assert target_resolver.entry_filesystem_signature(entry) == target_normalizers.entry_filesystem_signature(entry)
    assert target_resolver.entry_filesystem_signature(entry).startswith("local|")
    assert target_resolver.entry_matches_keyword(entry, "example s01e02")
    assert target_resolver.entry_matches_keyword(entry, "example s01e02") == target_normalizers.entry_matches_keyword(entry, "example s01e02")


def test_suggest_target_rejects_single_target_episode_mismatch():
    module, _, _ = load_plugin_module()
    target_resolver = plugin_submodule(module, "catalog.target_resolver")
    targets = [{"id": "s03e09", "season": 3, "episode": 9}]

    assert target_resolver.suggest_target(
        {"source_name": "My.Adventures.with.Superman.S03E04.en.srt"},
        targets,
        extract_episode_hint=target_resolver.extract_episode_hint,
    ) is None
    assert target_resolver.suggest_target(
        {"source_name": "My.Adventures.with.Superman.S03E09.en.srt"},
        targets,
        extract_episode_hint=target_resolver.extract_episode_hint,
    ) == "s03e09"


def test_auto_fill_does_not_positional_match_explicit_episode_mismatch():
    module, _, _ = load_plugin_module()
    target_resolver = plugin_submodule(module, "catalog.target_resolver")
    preview_items = [{
        "source_name": "My.Adventures.with.Superman.S03E04.en.srt",
        "target_id": None,
    }]
    targets = [{"id": "s03e09", "season": 3, "episode": 9}]

    target_resolver.auto_fill_missing_targets(
        preview_items,
        targets,
        extract_episode_hint=target_resolver.extract_episode_hint,
    )

    assert preview_items[0]["target_id"] is None


def test_media_metadata_helpers_are_reexported_from_target_resolver():
    module, _, _ = load_plugin_module()
    target_resolver = plugin_submodule(module, "catalog.target_resolver")
    media_metadata = plugin_submodule(module, "catalog.media_metadata")
    online_subtitle = plugin_submodule(module, "online.online_subtitle")
    runtime_helpers = plugin_submodule(module, "runtime.runtime_helpers")

    for name in [
        "english_title_from_aliases",
        "tmdb_aliases",
        "tmdb_detail_payload",
        "auto_media_for_entry",
        "is_chinese_transfer_media",
        "MediaMetadataService",
    ]:
        assert hasattr(target_resolver, name)
        assert hasattr(media_metadata, name)

    detail = {
        "original_language": "zh",
        "origin_country": ["CN"],
        "translations": [
            {"iso_639_1": "en", "data": {"title": "The Wandering Earth"}},
            {"iso_639_1": "zh", "data": {"title": "流浪地球"}},
        ],
    }
    kwargs = {"extract_title_aliases_func": online_subtitle.extract_title_aliases}
    assert target_resolver.tmdb_detail_payload(detail, **kwargs) == media_metadata.tmdb_detail_payload(detail, **kwargs)
    assert target_resolver.tmdb_detail_payload(detail, **kwargs)["en_title"] == "The Wandering Earth"

    entry = {"tmdb_id": 123, "media_type": "movie", "title": "流浪地球"}
    transfer_kwargs = {
        "auto_media_for_entry_func": lambda _entry: {"tmdb_id": 123, "original_language": "zh-Hans"},
        "normalize_text": runtime_helpers.normalize_text,
        "chinese_language_codes": module.SubtitleManualUpload._chinese_media_language_codes,
        "chinese_country_codes": module.SubtitleManualUpload._chinese_media_country_codes,
        "chinese_region_names": module.SubtitleManualUpload._chinese_media_region_names,
        "chinese_category_pattern": module.SubtitleManualUpload._chinese_media_category_pattern,
    }
    assert target_resolver.is_chinese_transfer_media(entry, **transfer_kwargs) == media_metadata.is_chinese_transfer_media(
        entry,
        **transfer_kwargs,
    )
    assert target_resolver.is_chinese_transfer_media(entry, **transfer_kwargs) == (True, "TMDB original_language=zh-hans")
    assert issubclass(target_resolver.MediaMetadataService, media_metadata.MediaMetadataService)


def test_subtitle_inventory_is_reexported_from_target_resolver(tmp_path):
    module, _, _ = load_plugin_module()
    target_resolver = plugin_submodule(module, "catalog.target_resolver")
    subtitle_inventory = plugin_submodule(module, "catalog.subtitle_inventory")
    subtitle_language = plugin_submodule(module, "matching.subtitle_language")
    runtime_helpers = plugin_submodule(module, "runtime.runtime_helpers")

    assert issubclass(target_resolver.SubtitleInventory, subtitle_inventory.SubtitleInventory)

    video = tmp_path / "Movie.mkv"
    video.write_text("video", encoding="utf-8")
    subtitle = tmp_path / "Movie.chi.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")
    backup = tmp_path / "Movie.chi.srt.bak"
    backup.write_text("backup", encoding="utf-8")

    def detect_language_profile(name, _raw_bytes):
        suffix = "chi" if "chi" in name else "eng"
        return {"suffix": suffix, "category": "chinese" if suffix == "chi" else "english"}

    class FakeSubprocess:
        PIPE = object()
        TimeoutExpired = TimeoutError

        def __init__(self):
            self.calls = 0

        def run(self, *args, **kwargs):
            self.calls += 1
            return types.SimpleNamespace(
                stdout=json.dumps(
                    {
                        "streams": [
                            {
                                "index": 2,
                                "codec_name": "subrip",
                                "tags": {"language": "zh-Hans", "title": "Chinese Simplified"},
                            }
                        ]
                    }
                )
            )

    fake_subprocess = FakeSubprocess()
    inventory = target_resolver.SubtitleInventory(
        subtitle_exts=module.SubtitleManualUpload._subtitle_exts,
        stream_exts=module.SubtitleManualUpload._stream_exts,
        embedded_text_codecs=module.SubtitleManualUpload._embedded_subtitle_text_codecs,
        embedded_image_codecs=module.SubtitleManualUpload._embedded_subtitle_image_codecs,
        embedded_probe_cache=module.OrderedDict(),
        embedded_probe_cache_max_size=2,
        trust_transfer_history_paths=False,
        normalize_text=runtime_helpers.normalize_text,
        normalize_language_suffix=subtitle_language.normalize_language_suffix,
        detect_language_profile=detect_language_profile,
        is_chinese_language_suffix=subtitle_language.is_chinese_language_suffix,
        safe_int=runtime_helpers.safe_int,
        subtitle_backup_path=lambda path: path.with_name(f"{path.name}.bak"),
        subprocess_module=fake_subprocess,
        logger_warning=lambda *args, **kwargs: None,
    )

    subtitles = inventory.subtitle_files_for_target({"storage": "local", "path": str(video)})
    assert subtitles == [
        {
            "name": "Movie.chi.srt",
            "path": str(subtitle),
            "relative_path": str(subtitle).replace("\\", "/"),
            "ext": ".srt",
            "is_bitmap": False,
            "language_suffix": "chi",
            "language_category": "chinese",
            "backup_path": str(backup),
            "backup_available": True,
            "size": subtitle.stat().st_size,
            "modified_at": subtitles[0]["modified_at"],
        }
    ]

    assert inventory.embedded_subtitle_tracks_for_target({"storage": "local", "path": str(tmp_path / "Movie.strm")}) == []
    assert fake_subprocess.calls == 0

    first_probe = inventory.embedded_subtitle_tracks_for_target({"storage": "local", "path": str(video)})
    second_probe = inventory.embedded_subtitle_tracks_for_target({"storage": "local", "path": str(video)})
    assert first_probe == second_probe
    assert first_probe[0]["language_suffix"] == "chi"
    assert first_probe[0]["is_chinese"] is True
    assert fake_subprocess.calls == 1


def test_local_media_catalog_is_reexported_from_target_resolver(tmp_path):
    module, histories, _ = load_plugin_module()
    plugin = make_plugin(module)
    target_resolver = plugin_submodule(module, "catalog.target_resolver")
    local_media_catalog = plugin_submodule(module, "catalog.local_media_catalog")

    assert issubclass(target_resolver.LocalMediaCatalog, local_media_catalog.LocalMediaCatalog)

    old_time = module.datetime.now() - module.timedelta(seconds=1900)
    stale_path = tmp_path / "stale.mkv"
    stale_path.write_text("video", encoding="utf-8")
    plugin._local_entries_cache = {
        "loaded_at": old_time,
        "entries": [
            {
                "id": "stale",
                "path": str(stale_path),
                "media_key": "movie-stale",
                "media_type": "movie",
                "title": "Stale Movie",
            }
        ],
        "media_count": 1,
        "persisted": True,
    }
    histories.data = [make_history_entry(tmp_path, "fresh", "fresh.mkv", media_key="movie-fresh")]
    catalog = plugin.services.local_media_catalog()
    started = {"value": False}
    catalog.start_background_cache_refresh = lambda: started.update(value=True)

    entries = catalog.load_local_entries(allow_stale=True)

    assert [item["id"] for item in entries] == ["stale"]
    assert started["value"] is True
    assert catalog.cache_status()["stale"] is True

    key = catalog.media_index_cache_key("stale", "movie")
    catalog.media_index_cache_set(key, entries, [{"title": "Stale Movie"}])
    assert catalog.media_index_cache_get(key, entries) == [{"title": "Stale Movie"}]
    plugin._local_entries_cache["loaded_at"] = module.datetime.now()
    assert catalog.media_index_cache_get(key, entries) is None


def test_media_target_resolver_is_reexported_from_target_resolver(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    target_resolver = plugin_submodule(module, "catalog.target_resolver")
    media_target_resolver = plugin_submodule(module, "catalog.media_target_resolver")

    assert issubclass(target_resolver.MediaTargetResolver, media_target_resolver.MediaTargetResolver)
    assert isinstance(plugin.services.target_resolver(), media_target_resolver.MediaTargetResolver)

    video = tmp_path / "Movie.mkv"
    video.write_text("video", encoding="utf-8")
    subtitle = tmp_path / "Movie.chi.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")
    target = plugin.services.target_resolver().target_from_entry(
        {
            "id": "m1",
            "path": str(video),
            "target_label": "Movie.mkv",
            "basename": "Movie",
            "media_type": "movie",
            "title": "Movie",
            "year": "2024",
            "library_name": "MoviePilot 媒体库",
            "relative_path": "Movie.mkv",
            "storage": "local",
            "writable": True,
        }
    )

    assert target["label"] == "Movie.mkv"
    assert target["path"] == str(video)
    assert target["is_stream"] is False
    assert target["has_subtitle"] is True
    assert target["subtitle_count"] == 1
    assert target["subtitles"][0]["language_suffix"] == "chi"


def remember_targets(plugin, module, entries):
    target_resolver = plugin_submodule(module, "catalog.target_resolver")
    runtime_helpers = plugin_submodule(module, "runtime.runtime_helpers")
    target_resolver.TargetEntryCache(
        plugin._entry_map,
        max_size=plugin._entry_map_max_size,
        normalize_text=runtime_helpers.normalize_text,
    ).remember(entries)


def load_session(plugin, module, session_id):
    runtime_helpers = plugin_submodule(module, "runtime.runtime_helpers")
    return plugin._upload_session_service().load_session(session_id, normalize_text=runtime_helpers.normalize_text)


def test_service_wrappers_delegate_to_services_registry():
    module, _, _ = load_plugin_module()
    service_registry = plugin_submodule(module, "runtime.service_registry")
    plugin = module.SubtitleManualUpload.__new__(module.SubtitleManualUpload)

    assert isinstance(plugin.services, service_registry.SubtitleManualUploadServices)
    assert plugin.services is plugin.services
    assert module.SubtitleManualUpload._archive_dependency_service().__class__.__name__ == "ArchiveDependencyService"

    calls = []
    marker = object()

    class FakeServices:
        def upload_session(self):
            calls.append("upload_session")
            return marker

        def writer(self):
            calls.append("writer")
            return marker

        def history(self):
            calls.append("history")
            return marker

        def autosub_bridge(self):
            calls.append("autosub_bridge")
            return marker

        def online_ai(self):
            calls.append("online_ai")
            return marker

        def auto_transfer(self):
            calls.append("auto_transfer")
            return marker

        def target_resolver(self):
            calls.append("target_resolver")
            return marker

        def local_media_catalog(self):
            calls.append("local_media_catalog")
            return marker

        def media_metadata(self):
            calls.append("media_metadata")
            return marker

        def timeline_tasks(self):
            calls.append("timeline_tasks")
            return marker

        def online_subtitles(self):
            calls.append("online_subtitles")
            return marker

    plugin._services_registry = FakeServices()

    assert plugin._upload_session_service() is marker
    assert plugin._subtitle_writer() is marker
    assert plugin._subtitle_history() is marker
    assert plugin._autosub_bridge() is marker
    assert plugin._online_ai_service() is marker
    assert plugin._auto_transfer_service() is marker
    assert plugin._target_resolver() is marker
    assert plugin._local_media_catalog() is marker
    assert plugin._media_metadata_service() is marker
    assert plugin._timeline_task_store() is marker
    assert plugin._online_service() is marker
    assert calls == [
        "upload_session",
        "writer",
        "history",
        "autosub_bridge",
        "online_ai",
        "auto_transfer",
        "target_resolver",
        "local_media_catalog",
        "media_metadata",
        "timeline_tasks",
        "online_subtitles",
    ]


def api_endpoint(plugin, path):
    return next(route["endpoint"] for route in plugin.get_api() if route["path"] == path)


class ServiceOverrideRegistry:
    def __init__(self, base_registry, **services):
        self._base_registry = base_registry
        self._services = services

    def __getattr__(self, name):
        if name in self._services:
            return lambda *args, **kwargs: self._services[name]
        return getattr(self._base_registry, name)


def override_services(plugin, **services):
    plugin._services_registry = ServiceOverrideRegistry(plugin.services, **services)
    return plugin._services_registry


class FakeRequest:
    def __init__(self, body=None, query_params=None):
        self._body = {} if body is None else body
        self.query_params = query_params or {}

    async def json(self):
        return self._body


class FakeFormRequest:
    def __init__(self, form):
        self._form = form

    async def form(self):
        return self._form


class FakeForm:
    def __init__(self, values):
        self._values = dict(values)

    def get(self, key, default=None):
        return self._values.get(key, default)

    def getlist(self, key):
        value = self._values.get(key, [])
        return list(value) if isinstance(value, list) else [value]


def test_plugin_api_route_contract_is_stable():
    module, _, _ = load_plugin_module()
    plugin = module.SubtitleManualUpload.__new__(module.SubtitleManualUpload)

    route_contract = [
        (route["path"], tuple(route["methods"]), route.get("auth"), route.get("summary"))
        for route in plugin.get_api()
    ]

    assert route_contract == [
        ("/status", ("GET",), "bear", "获取字幕匹配插件状态"),
        ("/refresh_index", ("POST",), "bear", "兼容旧版刷新索引入口"),
        ("/search", ("GET",), "bear", "搜索 MoviePilot 本地资源候选"),
        ("/targets", ("GET",), "bear", "读取选中媒体的本地文件目标"),
        ("/match_history", ("GET",), "bear", "读取字幕匹配历史"),
        ("/timeline_tasks", ("POST",), "bear", "查询智能调轴任务状态"),
        ("/timeline_fix_existing", ("POST",), "bear", "对匹配历史中的外挂字幕执行智能调轴"),
        ("/auto_transfer_queue", ("GET",), "bear", "查询入库自动字幕处理队列"),
        ("/auto_transfer_queue/retry", ("POST",), "bear", "重试自动入库字幕任务"),
        ("/auto_transfer_queue/clear_history", ("POST",), "bear", "清空自动入库字幕历史任务"),
        ("/prepare_upload", ("POST",), "bear", "上传字幕并生成匹配预览"),
        ("/apply_upload", ("POST",), "bear", "应用字幕匹配结果并写入目标目录"),
        ("/clear_subtitles", ("POST",), "bear", "清空选中目标视频的外挂字幕"),
        ("/delete_subtitle", ("POST",), "bear", "删除单个已匹配外挂字幕"),
        ("/restore_subtitle_backup", ("POST",), "bear", "恢复智能调轴前的字幕备份"),
        ("/ai_submit", ("POST",), "bear", "提交 AI 字幕生成任务"),
        ("/ai_tasks", ("POST",), "bear", "查询当前资源的 AI 字幕生成任务状态"),
        ("/ai_cancel", ("POST",), "bear", "取消 AI 字幕生成任务"),
        ("/ai_restart", ("POST",), "bear", "重新生成 AI 字幕任务"),
        ("/online_status", ("GET",), "bear", "获取在线字幕源状态"),
        ("/online_manual_links", ("POST",), "bear", "生成在线字幕站手动搜索链接"),
        ("/online_search", ("POST",), "bear", "搜索在线字幕"),
        ("/online_search_provider", ("POST",), "bear", "搜索单个在线字幕源"),
        ("/online_ai_submit", ("POST",), "bear", "提交在线外语字幕到 AI 翻译状态队列"),
        ("/online_download_preview", ("POST",), "bear", "下载在线字幕并生成匹配预览"),
    ]


def test_plugin_api_request_parameters_are_fastapi_requests():
    module, _, _ = load_plugin_module()
    plugin = module.SubtitleManualUpload.__new__(module.SubtitleManualUpload)

    request_routes = [
        route["path"]
        for route in plugin.get_api()
        if "request" in inspect.signature(route["endpoint"]).parameters
    ]

    assert request_routes
    for route in plugin.get_api():
        signature = inspect.signature(route["endpoint"])
        parameter = signature.parameters.get("request")
        if parameter is None:
            continue
        assert parameter.annotation == "Request", route["path"]


class FakeUpload:
    def __init__(self, filename, content):
        self.filename = filename
        self._content = content

    async def read(self):
        return self._content


def make_history_entry(tmp_path, entry_id, filename, **extra):
    path = tmp_path / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("video", encoding="utf-8")
    return {
        "id": entry_id,
        "path": str(path),
        "media_key": extra.pop("media_key", f"movie-{entry_id}"),
        **extra,
    }


def test_local_entries_cache_hits_until_forced_refresh(tmp_path):
    module, histories, _ = load_plugin_module()
    plugin = make_plugin(module)
    histories.calls = 0
    histories.data = [make_history_entry(tmp_path, "a", "a.mkv", media_key="movie-a")]

    catalog = plugin.services.local_media_catalog()
    first = catalog.load_local_entries()
    second = catalog.load_local_entries()
    forced = catalog.load_local_entries(force=True)

    assert first == second == forced
    assert histories.calls == 2
    assert plugin._local_media_catalog().cache_status()["entry_count"] == 1
    assert plugin._local_media_catalog().cache_status()["media_count"] == 1


def test_refresh_local_cache_rebuilds_entries(tmp_path):
    module, histories, _ = load_plugin_module()
    plugin = make_plugin(module)
    histories.calls = 0
    histories.data = [make_history_entry(tmp_path, "a", "a.mkv", media_key="movie-a")]
    plugin.services.local_media_catalog().load_local_entries()

    histories.data = [make_history_entry(tmp_path, "b", "b.mkv", media_key="movie-b")]
    refreshed = plugin._local_media_catalog().refresh_local_cache()

    assert [item["id"] for item in refreshed] == ["b"]
    assert list(plugin._entry_map.keys()) == ["b"]


def test_local_entries_cache_persists_between_plugin_instances(tmp_path):
    module, histories, _ = load_plugin_module()
    plugin = make_plugin(module)
    histories.calls = 0
    histories.data = [make_history_entry(tmp_path, "a", "a.mkv", media_key="movie-a")]

    first = plugin.services.local_media_catalog().load_local_entries()
    plugin2 = module.SubtitleManualUpload.__new__(module.SubtitleManualUpload)
    plugin2._local_entries_cache = {"loaded_at": None, "entries": [], "media_count": 0, "persisted": False}
    plugin2._entry_map = module.OrderedDict()
    plugin2._media_index_cache = module.OrderedDict()
    plugin2._cache_refreshing = False
    plugin2._cache_ttl_seconds = 1800
    plugin2._cache_max_entries = 10
    plugin2._entry_map_max_size = 2
    plugin2._media_index_cache_max_keys = 20
    plugin2._build_entry_from_history = lambda history: dict(history)

    restored = plugin2.services.local_media_catalog().load_local_entries()

    assert restored == first
    assert histories.calls == 1
    assert plugin2._local_media_catalog().cache_status()["persisted"] is True


def test_stale_persisted_cache_returns_before_background_refresh(tmp_path):
    module, histories, _ = load_plugin_module()
    plugin = make_plugin(module)
    old_time = module.datetime.now() - module.timedelta(seconds=1900)
    stale_path = tmp_path / "stale.mkv"
    stale_path.write_text("video", encoding="utf-8")
    plugin._local_entries_cache = {
        "loaded_at": old_time,
        "entries": [{"id": "stale", "path": str(stale_path), "media_key": "movie-stale"}],
        "media_count": 1,
        "persisted": True,
    }
    started = {"value": False}
    catalog = plugin.services.local_media_catalog()
    catalog.start_background_cache_refresh = lambda: started.update(value=True)

    entries = catalog.load_local_entries(allow_stale=True)

    assert [item["id"] for item in entries] == ["stale"]
    assert started["value"] is True


def test_transfer_event_entries_can_merge_into_local_cache(tmp_path):
    module, histories, _ = load_plugin_module()
    plugin = make_plugin(module)
    histories.calls = 0
    video = tmp_path / "Example.Show.S01E02.mkv"
    video.write_text("video")
    meta = types.SimpleNamespace(begin_season=1, begin_episode=2, episode_list=[2])
    mediainfo = types.SimpleNamespace(type="电视剧", title="Example Show", year="2024", tmdb_id=123)
    transferinfo = types.SimpleNamespace(file_list_new=[str(video)])

    entries = plugin.services.target_resolver().entries_from_transfer_event(
        {
            "meta": meta,
            "mediainfo": mediainfo,
            "transferinfo": transferinfo,
        }
    )
    plugin.services.local_media_catalog().merge_local_entries_cache(entries)

    assert len(entries) == 1
    assert entries[0]["media_type"] == "tv"
    assert entries[0]["season"] == 1
    assert entries[0]["episode"] == 2
    assert plugin._local_media_catalog().cache_status()["entry_count"] == 1


def test_entry_map_is_bounded_lru():
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    target_resolver = plugin_submodule(module, "catalog.target_resolver")
    runtime_helpers = plugin_submodule(module, "runtime.runtime_helpers")
    cache = target_resolver.TargetEntryCache(
        plugin._entry_map,
        max_size=plugin._entry_map_max_size,
        normalize_text=runtime_helpers.normalize_text,
    )

    cache.remember([{"id": "a"}, {"id": "b"}, {"id": "c"}])
    cache.remember([{"id": "b"}])
    cache.remember([{"id": "d"}])

    assert list(plugin._entry_map.keys()) == ["b", "d"]


def test_target_has_chinese_subtitle_checks_external_and_embedded_tracks():
    module, _, _ = load_plugin_module()
    subtitle_language = plugin_submodule(module, "matching.subtitle_language")

    assert subtitle_language.target_has_chinese_subtitle({"subtitles": [{"language_suffix": "chi"}]})
    assert subtitle_language.target_has_chinese_subtitle({"embedded_subtitles": [{"language": "zh-Hant"}]})
    assert not subtitle_language.target_has_chinese_subtitle(
        {
            "subtitles": [{"language_suffix": "eng"}],
            "embedded_subtitles": [{"language": "eng"}],
        }
    )

    target = {}
    inventory = types.SimpleNamespace(
        embedded_subtitle_tracks_for_target=lambda _entry: [{"language_suffix": "chi", "is_chinese": True}]
    )

    assert subtitle_language.auto_target_has_chinese_subtitle(
        {"id": "m1"},
        target,
        subtitle_inventory=inventory,
        language_priority=["chi"],
    )
    assert target["embedded_subtitle_count"] == 1


def test_online_download_name_prefers_archive_magic_over_filename_suffix():
    module, _, _ = load_plugin_module()
    language = plugin_submodule(module, "matching.subtitle_language")
    upload_session = plugin_submodule(module, "upload.upload_session")
    cls = module.SubtitleManualUpload

    rar_named_zip = upload_session.normalize_online_download_name(
        "Spider-Man.Into.the.Spider-Verse.2018.1080p.WEB-DL.DD5.1.H264-FGT.zip",
        b"Rar!\x1a\x07\x00subtitle-data",
        {"title": "Spider-Man Into the Spider-Verse"},
        subtitle_exts=cls._subtitle_exts,
        archive_exts=cls._archive_exts,
        normalize_text=language.normalize_text,
        decode_preview_bytes=language.decode_preview_bytes,
    )
    zip_named_unknown = upload_session.normalize_online_download_name(
        "subtitle.bin",
        b"PK\x03\x04subtitle-data",
        {"title": "Spider-Man Into the Spider-Verse"},
        subtitle_exts=cls._subtitle_exts,
        archive_exts=cls._archive_exts,
        normalize_text=language.normalize_text,
        decode_preview_bytes=language.decode_preview_bytes,
    )

    assert rar_named_zip == "Spider-Man.Into.the.Spider-Verse.2018.1080p.WEB-DL.DD5.1.H264-FGT.rar"
    assert zip_named_unknown == "subtitle.zip"


def test_online_download_name_detects_7z_magic():
    module, _, _ = load_plugin_module()
    language = plugin_submodule(module, "matching.subtitle_language")
    upload_session = plugin_submodule(module, "upload.upload_session")
    cls = module.SubtitleManualUpload

    assert upload_session.normalize_online_download_name(
        "download.bin",
        b"7z\xbc\xaf\x27\x1c" + b"payload",
        {"title": "Jack Reacher"},
        subtitle_exts=cls._subtitle_exts,
        archive_exts=cls._archive_exts,
        normalize_text=language.normalize_text,
        decode_preview_bytes=language.decode_preview_bytes,
    ) == "download.7z"


def test_upload_session_write_and_load_round_trips_payload(tmp_path):
    module, _, _ = load_plugin_module()
    runtime_helpers = plugin_submodule(module, "runtime.runtime_helpers")
    plugin = make_plugin(module)
    session_id = f"pytest-{runtime_helpers.hash_text(str(tmp_path))[:8]}"
    payload = {
        "target_ids": ["t1"],
        "prepared_uploads": [{"upload_id": "u1", "source_name": "Movie.chi.srt"}],
    }

    plugin._write_session(session_id, payload)
    session_dir, loaded = load_session(plugin, module, session_id)

    try:
        assert session_dir == plugin.services.upload_session().get_session_root() / session_id
        assert loaded == payload
    finally:
        module.shutil.rmtree(session_dir, ignore_errors=True)


def _zip_payload(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return buffer.getvalue()


def _limited_upload_session_service(module, tmp_path, **limit_overrides):
    upload_session = plugin_submodule(module, "upload.upload_session")
    runtime_helpers = plugin_submodule(module, "runtime.runtime_helpers")
    cls = module.SubtitleManualUpload
    limits = upload_session.ArchiveResourceLimits(**limit_overrides)
    return upload_session.UploadSessionService(
        data_path=tmp_path,
        subtitle_exts=cls._subtitle_exts,
        archive_exts=cls._archive_exts,
        rar_exts=cls._rar_exts,
        sevenzip_exts=cls._sevenzip_exts,
        default_session_hours=cls._default_session_hours,
        hash_text=runtime_helpers.hash_text,
        extract_rar_subtitle_files=lambda *args, **kwargs: [],
        extract_7z_subtitle_files=lambda *args, **kwargs: [],
        resource_limits=limits,
    )


def test_upload_content_size_limit_rejects_single_subtitle(tmp_path):
    module, _, _ = load_plugin_module()
    service = _limited_upload_session_service(module, tmp_path, max_content_bytes=3)

    with pytest.raises(ValueError, match="上传内容大小超过限制"):
        service.extract_subtitle_files("movie.srt", b"1234", tmp_path)


def test_archive_member_count_limit_rejects_zip(tmp_path):
    module, _, _ = load_plugin_module()
    service = _limited_upload_session_service(
        module,
        tmp_path,
        max_content_bytes=1_000_000,
        max_archive_members=2,
    )
    payload = _zip_payload([("a.txt", b""), ("b.txt", b""), ("c.txt", b"")])

    with pytest.raises(ValueError, match="成员数量超过限制"):
        service.extract_subtitle_files("many.zip", payload, tmp_path)


def test_archive_member_size_limit_rejects_zip_subtitle(tmp_path):
    module, _, _ = load_plugin_module()
    service = _limited_upload_session_service(
        module,
        tmp_path,
        max_content_bytes=1_000_000,
        max_archive_member_bytes=3,
    )
    payload = _zip_payload([("movie.srt", b"1234")])

    with pytest.raises(ValueError, match="单文件大小超过限制"):
        service.extract_subtitle_files("sample.zip", payload, tmp_path)


def test_archive_total_size_limit_rejects_zip_subtitles(tmp_path):
    module, _, _ = load_plugin_module()
    service = _limited_upload_session_service(
        module,
        tmp_path,
        max_content_bytes=1_000_000,
        max_archive_member_bytes=100,
        max_archive_total_bytes=5,
    )
    payload = _zip_payload([("a.srt", b"123"), ("b.srt", b"456")])

    with pytest.raises(ValueError, match="总解压大小超过限制"):
        service.extract_subtitle_files("sample.zip", payload, tmp_path)


def test_archive_dependency_service_reports_mapped_binary_missing():
    module, _, _ = load_plugin_module()
    upload_session = plugin_submodule(module, "upload.upload_session")
    statuses = []

    service = upload_session.ArchiveDependencyService(
        rar_dependency_mode="mapped_binary",
        rar_tool_path="/missing/7z",
        rar_python_package="rarfile",
        rar_tools=(),
        sevenzip_tools=(),
        normalize_text=str,
        decode_preview_bytes=lambda raw: raw.decode("utf-8", errors="ignore"),
        logger_info=lambda *args, **kwargs: None,
        logger_warning=lambda *args, **kwargs: None,
        status_setter=lambda state, message: statuses.append((state, message)),
    )

    service.prepare_rar_dependency()

    assert statuses == [("missing", "未检测到映射文件，请把宿主机 unar 映射到容器 /missing/7z")]


def test_archive_subtitle_count_limit_rejects_zip_subtitles(tmp_path):
    module, _, _ = load_plugin_module()
    service = _limited_upload_session_service(
        module,
        tmp_path,
        max_content_bytes=1_000_000,
        max_archive_member_bytes=100,
        max_archive_total_bytes=100,
        max_subtitle_files=1,
    )
    payload = _zip_payload([("a.srt", b"1"), ("b.srt", b"2")])

    with pytest.raises(ValueError, match="字幕文件数量超过限制"):
        service.extract_subtitle_files("sample.zip", payload, tmp_path)


def test_rarfile_resource_limit_error_does_not_fallback(tmp_path):
    module, _, _ = load_plugin_module()
    upload_session = plugin_submodule(module, "upload.upload_session")
    runtime_helpers = plugin_submodule(module, "runtime.runtime_helpers")
    fallback_called = False

    class FakeMember:
        filename = "movie.srt"
        file_size = 4

        def isdir(self):
            return False

    class FakeArchive:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def infolist(self):
            return [FakeMember()]

        def read(self, member):
            return b"1234"

    class FakeRarfileModule:
        @staticmethod
        def RarFile(path):
            return FakeArchive()

    def extract_with_rarfile(source_name, archive_path, session_dir, **kwargs):
        return upload_session.extract_rar_subtitle_files_with_rarfile(
            source_name,
            archive_path,
            session_dir,
            rarfile_module_factory=lambda: FakeRarfileModule,
            rar_python_package="rarfile",
            subtitle_exts={".srt"},
            hash_text=runtime_helpers.hash_text,
            **kwargs,
        )

    def fallback_extract(*args, **kwargs):
        nonlocal fallback_called
        fallback_called = True
        return []

    limits = upload_session.ArchiveResourceLimits(max_archive_member_bytes=3)

    with pytest.raises(upload_session.ArchiveResourceLimitError, match="单文件大小超过限制"):
        upload_session.extract_rar_subtitle_files(
            "sample.rar",
            tmp_path / "sample.rar",
            tmp_path,
            rar_python_available_func=lambda: True,
            extract_with_rarfile=extract_with_rarfile,
            rar_tool_func=lambda: "7z",
            extract_command_archive_subtitle_files_func=fallback_extract,
            rar_python_package="rarfile",
            resource_limits=limits,
        )

    assert not fallback_called


def test_archive_subtitle_extractor_uses_dependency_service_for_7z(tmp_path):
    module, _, _ = load_plugin_module()
    upload_session = plugin_submodule(module, "upload.upload_session")
    calls = []

    class FakeArchiveDependency:
        def sevenzip_tool(self):
            return "unar"

        def list_rar_members(self, archive_path, tool_path):
            calls.append(("list", Path(archive_path).name, tool_path))
            return [
                upload_session.ArchiveMember("nested/Movie.zh.ass", 0, 8),
                upload_session.ArchiveMember("notes.txt", 1),
            ]

        def read_rar_member(self, archive_path, member, tool_path):
            calls.append(("read", member.name, tool_path))
            return b"subtitle"

    extractor = upload_session.ArchiveSubtitleExtractor(
        archive_dependency_service=FakeArchiveDependency(),
        subtitle_exts={".ass"},
        hash_text=lambda value: "archive-upload-id",
        rar_python_package="rarfile",
    )
    archive_path = tmp_path / "sample.7z"
    archive_path.write_bytes(b"7z archive")

    extracted = extractor.extract_7z_subtitle_files("sample.7z", archive_path, tmp_path)

    assert extracted == [
        {
            "upload_id": "archive-upload-id",
            "source_name": "Movie.zh.ass",
            "archive_name": "sample.7z",
            "archive_member": "nested/Movie.zh.ass",
            "stored_path": str(tmp_path / "archive-upload-id.ass"),
            "ext": ".ass",
        }
    ]
    assert (tmp_path / "archive-upload-id.ass").read_bytes() == b"subtitle"
    assert calls == [
        ("list", "sample.7z", "unar"),
        ("read", "nested/Movie.zh.ass", "unar"),
    ]


def test_unar_archive_commands_use_lsar_json_and_stdout_extract(tmp_path):
    module, _, _ = load_plugin_module()
    upload_session = plugin_submodule(module, "upload.upload_session")
    archive_path = tmp_path / "sample.rar"
    archive_path.write_bytes(b"Rar!\x1a\x07\x01\x00")
    unar_path = tmp_path / "unar"
    lsar_path = tmp_path / "lsar"
    unar_path.write_text("", encoding="utf-8")
    lsar_path.write_text("", encoding="utf-8")
    unar_path.chmod(0o755)
    lsar_path.chmod(0o755)
    calls = []

    def run_command(args):
        calls.append(tuple(str(item) for item in args))
        if Path(args[0]).name == "lsar":
            return json.dumps(
                {
                    "lsarContents": [
                        {"XADFileName": "nested/Movie.zh.ass", "XADIndex": 0, "XADFileSize": 8},
                        {"XADFileName": "nested", "XADIsDirectory": True, "XADIndex": 1},
                        {"XADFileName": "notes.txt", "XADIndex": 2},
                    ]
                }
            ).encode("utf-8")
        return b"subtitle"

    members = upload_session.list_rar_members(
        archive_path,
        str(unar_path),
        decode_preview_bytes=lambda raw: raw.decode("utf-8", errors="ignore"),
        run_command=run_command,
    )
    member_bytes = upload_session.read_rar_member(
        archive_path,
        members[0],
        str(unar_path),
        run_command=run_command,
    )

    assert members == [
        upload_session.ArchiveMember("nested/Movie.zh.ass", 0, 8),
        upload_session.ArchiveMember("notes.txt", 2),
    ]
    assert member_bytes == b"subtitle"
    assert calls == [
        (str(lsar_path), "-json", "-no-recursion", str(archive_path)),
        (str(unar_path), "-quiet", "-no-directory", "-no-recursion", "-indexes", "-output-directory", "-", str(archive_path), "0"),
    ]


def test_rar_extraction_prefers_unar_over_rarfile_when_available(tmp_path):
    module, _, _ = load_plugin_module()
    upload_session = plugin_submodule(module, "upload.upload_session")
    archive_path = tmp_path / "sample.rar"
    archive_path.write_bytes(b"Rar!\x1a\x07\x01\x00")
    calls = []

    def extract_with_rarfile(*args, **kwargs):
        calls.append("rarfile")
        return []

    def extract_with_command(source_name, archive_path, session_dir, tool_path, **kwargs):
        calls.append(("command", Path(tool_path).name))
        return [{"source_name": "Movie.zh.ass"}]

    extracted = upload_session.extract_rar_subtitle_files(
        "sample.rar",
        archive_path,
        tmp_path,
        rar_python_available_func=lambda: True,
        extract_with_rarfile=extract_with_rarfile,
        rar_tool_func=lambda: "/usr/bin/unar",
        extract_command_archive_subtitle_files_func=extract_with_command,
        rar_python_package="rarfile",
    )

    assert extracted == [{"source_name": "Movie.zh.ass"}]
    assert calls == [("command", "unar")]


def test_archive_tool_detection_ignores_legacy_7z_configuration(tmp_path, monkeypatch):
    module, _, _ = load_plugin_module()
    upload_session = plugin_submodule(module, "upload.upload_session")
    unar_path = tmp_path / "unar"
    sevenzip_path = tmp_path / "7z"
    unar_path.write_text("", encoding="utf-8")
    sevenzip_path.write_text("", encoding="utf-8")
    unar_path.chmod(0o755)
    sevenzip_path.chmod(0o755)

    def fake_which(tool):
        if tool == "unar":
            return str(unar_path)
        if tool == "7z":
            return str(sevenzip_path)
        return ""

    monkeypatch.setattr(upload_session.shutil, "which", fake_which)
    kwargs = {"normalize_text": lambda value: str(value or "").strip()}

    assert upload_session.find_rar_tool(
        configured_tool_path=str(sevenzip_path),
        rar_tools=("7z", "unar"),
        **kwargs,
    ) == str(unar_path)
    assert upload_session.find_sevenzip_tool(
        configured_tool_path=str(sevenzip_path),
        sevenzip_tools=("7z", "unar"),
        **kwargs,
    ) == str(unar_path)


def test_prepare_upload_uses_upload_session_service(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    video.write_text("video", encoding="utf-8")
    remember_targets(plugin, module,
        [
            {
                "id": "m1",
                "path": str(video),
                "basename": "Movie",
                "filename": "Movie.mkv",
                "target_label": "Movie",
                "storage": "local",
            }
        ]
    )
    setattr(plugin, "_suggest" + "_target", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("preview should call target_resolver.suggest_target directly")
    ))
    setattr(plugin, "_auto_fill" + "_missing_targets", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("preview should call target_resolver.auto_fill_missing_targets directly")
    ))
    setattr(plugin, "_build_destination" + "_name", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("preview should call SubtitleWriter.build_destination_name directly")
    ))
    form = FakeForm(
        {
            "target_ids": module.json.dumps(["m1"]),
            "files": [
                FakeUpload(
                    "Movie.chi.srt",
                    "1\n00:00:01,000 --> 00:00:02,000\n你好\n".encode("utf-8"),
                )
            ],
        }
    )

    prepare_endpoint = next(route["endpoint"] for route in plugin.get_api() if route["path"] == "/prepare_upload")
    response = asyncio.run(prepare_endpoint(FakeFormRequest(form)))

    data = response["data"]
    assert response["success"] is True
    assert data["source"] == "upload"
    assert data["items"][0]["source_name"] == "Movie.chi.srt"
    assert data["items"][0]["target_id"] == "m1"
    session_dir, session_payload = load_session(plugin, module, data["session_id"])
    assert session_payload["source"] == "upload"
    assert Path(session_payload["uploads"][0]["stored_path"]).is_file()
    shutil.rmtree(session_dir, ignore_errors=True)


def test_online_search_endpoint_uses_online_service(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    video.write_text("video", encoding="utf-8")
    remember_targets(plugin, module,
        [
            {
                "id": "m1",
                "path": str(video),
                "basename": "Movie",
                "filename": "Movie.mkv",
                "target_label": "Movie",
                "storage": "local",
            }
        ]
    )
    captured = {}

    class FakeOnlineService:
        def search(self, **kwargs):
            captured["search"] = kwargs
            return {"results": [{"provider": "assrt", "result_id": "r1"}], "messages": []}

        def manual_links(self, keywords, providers=None):
            captured["manual_links"] = {"keywords": keywords, "providers": providers}
            return [{"provider": "assrt", "url": "https://example.invalid/search"}]

    override_services(plugin, online_subtitles=FakeOnlineService())
    search_endpoint = next(route["endpoint"] for route in plugin.get_api() if route["path"] == "/online_search")

    response = asyncio.run(
        search_endpoint(
            FakeRequest(
                {
                    "target_ids": ["m1"],
                    "keyword": "Movie 2024",
                    "providers": ["assrt"],
                }
            )
        )
    )

    assert response["success"] is True
    assert captured["search"]["providers"] == ["assrt"]
    assert captured["search"]["keywords"][0] == "Movie 2024"
    assert response["data"]["results"][0]["result_id"] == "r1"
    assert response["data"]["manual_links"][0]["provider"] == "assrt"


def test_api_apply_upload_uses_subtitle_writer_and_forwards_risky_offset(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    source = tmp_path / "Upload.srt"
    destination = tmp_path / "Movie.chi.srt"
    video.write_text("video", encoding="utf-8")
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")
    target = {
        "id": "m1",
        "path": str(video),
        "basename": "Movie",
        "filename": "Movie.mkv",
        "target_label": "Movie",
        "storage": "local",
    }
    plugin._write_session(
        "apply-writer",
        {
            "uploads": [
                {
                    "upload_id": "u1",
                    "source_name": source.name,
                    "archive_name": "",
                    "stored_path": str(source),
                    "ext": ".srt",
                }
            ],
            "targets": [target],
        },
    )
    setattr(plugin, "_build_write" + "_operations", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("api_apply_upload should call SubtitleWriter.build_write_operations directly")
    ))
    captured = {}

    def fake_fix(video_path, subtitle_path, output_path, **kwargs):
        captured["kwargs"] = kwargs
        output_path.write_bytes(subtitle_path.read_bytes())
        return module.TimelineFixResult(
            enabled=True,
            applied=True,
            reason="timeline adjusted",
            base="audio:webrtc",
            offset_seconds=121.0,
            scale_factor=1.0,
            score=0.9,
            confidence="high",
        )

    module.fix_subtitle_timeline = fake_fix

    apply_endpoint = next(route["endpoint"] for route in plugin.get_api() if route["path"] == "/apply_upload")
    response = asyncio.run(
        apply_endpoint(
            FakeRequest(
                {
                    "session_id": "apply-writer",
                    "items": [
                        {
                            "upload_id": "u1",
                            "target_id": "m1",
                            "ext": ".srt",
                            "language_suffix": "chi",
                        }
                    ],
                    "fix_timeline": True,
                    "allow_risky_offset": True,
                }
            )
        )
    )

    assert response["success"] is True
    assert response["data"]["count"] == 1
    assert destination.exists()
    assert "智能调轴 1 个" in response["message"]
    assert captured["kwargs"]["allow_risky_offset"] is True


@pytest.mark.parametrize("generation", ["plugins.v2", "plugins.v3"])
@pytest.mark.parametrize("include_invalid", [False, True])
def test_online_download_preview_uses_upload_session_service(tmp_path, generation, include_invalid):
    module, _, _ = load_plugin_module(generation)
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    video.write_text("video", encoding="utf-8")
    remember_targets(plugin, module,
        [
            {
                "id": "m1",
                "path": str(video),
                "basename": "Movie",
                "filename": "Movie.mkv",
                "target_label": "Movie",
                "storage": "local",
            }
        ]
    )

    class FakeOnlineService:
        def download(self, selected):
            downloaded = [
                {
                    "provider": selected[0]["provider"],
                    "source_name": "Movie.eng.srt",
                    "content": b"1\n00:00:01,000 --> 00:00:02,000\nHello\n",
                    "result": selected[0],
                }
            ]
            if include_invalid:
                downloaded.insert(0, {
                    "provider": selected[0]["provider"],
                    "source_name": "invalid.ass",
                    "content": b"upstream error",
                    "result": selected[0],
                })
            return downloaded

    override_services(plugin, online_subtitles=FakeOnlineService())
    download_endpoint = next(route["endpoint"] for route in plugin.get_api() if route["path"] == "/online_download_preview")

    response = asyncio.run(
        download_endpoint(
            FakeRequest(
                {
                    "target_ids": ["m1"],
                    "results": [
                        {
                            "provider": "opensubtitles",
                            "result_id": "r1",
                            "title": "Movie English",
                        }
                    ],
                }
            )
        )
    )

    data = response["data"]
    assert response["success"] is True
    assert data["source"] == "online"
    assert data["items"][0]["source_name"] == "Movie.eng.srt"
    assert data["items"][0]["online_source"] == "opensubtitles"
    assert len(data["items"]) == 1
    assert len(data["invalid_files"]) == int(include_invalid)
    if include_invalid:
        assert data["invalid_files"][0]["name"] == "invalid.ass"
        assert data["invalid_files"][0]["reason"]
    session_dir, session_payload = load_session(plugin, module, data["session_id"])
    assert session_payload["source"] == "online"
    assert Path(session_payload["uploads"][0]["stored_path"]).is_file()
    module.shutil.rmtree(session_dir, ignore_errors=True)


def test_v3_online_auto_preview_uses_current_policy_without_downloading(tmp_path, monkeypatch):
    module, _, _ = load_plugin_module("plugins.v3")
    plugin = make_plugin(module)
    plugin._enabled = True
    plugin._auto_search_on_transfer = True
    plugin._auto_skip_chinese_media_on_transfer = False
    plugin._auto_check_emby_subtitles = False
    plugin._online_provider_ids = ["subhd"]
    video = tmp_path / "Pressure.2026.2160p.WEB-DL.strm"
    video.write_text("https://example.invalid/video", encoding="utf-8")
    remember_targets(plugin, module, [{
        "id": "pressure", "title": "Pressure", "path": str(video),
        "basename": video.stem, "filename": video.name, "target_label": video.name,
        "storage": "local", "media_type": "movie",
    }])

    def no_online_service():
        pytest.fail("Preview must not contact a subtitle provider")

    monkeypatch.setattr(plugin.services, "online_subtitles", no_online_service)
    endpoint = next(route["endpoint"] for route in plugin.get_api() if route["path"] == "/online_auto_preview")
    normal = {
        "provider": "subhd", "title": "Pressure.2026.2160p.WEB-DL", "score": 60,
        "language": "简英双语", "identity_status": "strong",
    }
    body = {
        "target_ids": ["pressure"], "scope": "movie",
        "results": [
            {**normal, "result_id": "ai", "subtitle_type": "AI校对", "score": 85},
            {**normal, "result_id": "official", "subtitle_type": "官方字幕", "download_count": 1254},
            {**normal, "result_id": "unassociated", "subtitle_type": "官方字幕", "score": 99,
             "subhd_unassociated": True},
            {**normal, "result_id": "low", "score": 19},
            {**normal, "result_id": "manual", "downloadable": False},
            {**normal, "result_id": "other-provider", "provider": "subtitlecat"},
        ],
    }

    def preview():
        return asyncio.run(endpoint(FakeRequest(body)))["data"]

    result = preview()
    assert result["enabled"] is True
    plan = result["plans"][0]
    assert [item["result_id"] for item in plan["candidates"]] == ["ai", "official"]
    assert plan["candidates"][0]["auto_rank"]["release"] == "片源匹配"
    assert plan["candidates"][0]["auto_rank"]["type_bonus"] == 3
    assert 1 < plan["candidates"][1]["auto_rank"]["download_bonus"] < 1.5
    assert plan["rejections"] == {
        "匹配分低于 20": 1, "仅支持手动下载": 1, "字幕源未启用自动入库": 1, "SubHD 未关联": 1,
    }

    plugin._auto_subtitle_legacy_ranking = True
    result = preview()
    assert "legacy_ranking" not in result
    assert [item["result_id"] for item in result["plans"][0]["candidates"]] == ["ai", "official"]
    plugin._auto_subtitle_legacy_ranking = False

    saved_results = body["results"]
    plugin._online_provider_ids = ["subhd", "subtitlecat"]
    body["results"] = [{**normal, "result_id": f"subhd-{index}"} for index in range(6)] + [
        {**normal, "result_id": "backup", "provider": "subtitlecat"},
    ]
    attempts = preview()["plans"][0]["candidates"]
    assert [item["result_id"] for item in attempts] == ["subhd-0", "subhd-1", "subhd-2", "subhd-3", "backup"]
    assert attempts[-1]["auto_rank"]["fallback"] is True
    assert [item["auto_rank"]["position"] for item in attempts] == [1, 2, 3, 4, 5]
    plugin._online_provider_ids = ["subhd"]
    body["results"] = saved_results

    plugin._auto_subtitle_language_priority = ["chi&jp"]
    result = preview()
    assert result["required_languages"] == ["chi&jp"]
    assert result["plans"][0]["candidates"] == []
    assert result["plans"][0]["rejections"]["字幕语言不符合要求"] == 2

    plugin._auto_subtitle_language_priority = ["chi"]
    plugin._auto_subtitle_language_strict = True
    video.with_suffix(".zh-CN.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\n测试\n", encoding="utf-8")
    plugin.services.subtitle_inventory().invalidate_directory(tmp_path)
    result = preview()
    assert result["plans"] == []
    assert result["skipped"][0]["reason"] == "目标已有符合语言要求的字幕"

    plugin._auto_subtitle_language_priority = ["chi&eng"]
    plugin._entry_map_max_size = 10
    episodes = []
    for season, episode in [(1, 1), (1, 2), (2, 1)]:
        name = f"Pressure.S{season:02d}E{episode:02d}.2160p.WEB-DL"
        path = tmp_path / f"{name}.strm"
        path.write_text("https://example.invalid/video", encoding="utf-8")
        episodes.append({
            "id": name, "title": "Pressure", "path": str(path), "basename": name,
            "filename": path.name, "target_label": name, "storage": "local",
            "media_type": "tv", "season": season, "episode": episode,
        })
    remember_targets(plugin, module, episodes)
    body.update(
        target_ids=[entry["id"] for entry in episodes],
        scope="season",
        results=[
            {**normal, "result_id": "s1", "title": "Pressure.S01.2160p.WEB-DL"},
            {**normal, "result_id": "s2", "title": "Pressure.S02E01.2160p.WEB-DL"},
            {**normal, "result_id": "e3", "title": "Pressure.S01E03.2160p.WEB-DL"},
            {**normal, "result_id": "unconfirmed"},
        ],
    )
    plans = preview()["plans"]
    assert [plan["scope"] for plan in plans] == ["season", "season"]
    assert [[item["result_id"] for item in plan["candidates"]] for plan in plans] == [["s1"], ["s2"]]


def test_7z_archive_extraction_with_unar_tool(tmp_path):
    module, _, _ = load_plugin_module()
    cls = module.SubtitleManualUpload
    upload_session = plugin_submodule(module, "upload.upload_session")
    original_which = upload_session.shutil.which
    original_run = upload_session.subprocess.run
    unar_path = tmp_path / "unar"
    lsar_path = tmp_path / "lsar"
    unar_path.write_text("", encoding="utf-8")
    lsar_path.write_text("", encoding="utf-8")
    unar_path.chmod(0o755)
    lsar_path.chmod(0o755)

    def fake_which(tool):
        if tool == "unar":
            return str(unar_path)
        if tool == "lsar":
            return str(lsar_path)
        return ""

    def fake_run(args, stdout=None, stderr=None, check=None, timeout=None):
        command = Path(args[0]).name
        if command == "lsar":
            output = json.dumps(
                {
                    "lsarContents": [
                        {"XADFileName": "说明.txt", "XADIndex": 0},
                        {"XADFileName": "Jack.Reacher.2012.chs&eng.ass", "XADIndex": 1},
                        {"XADFileName": "Jack.Reacher.2012.eng.ass", "XADIndex": 2},
                    ]
                }
            ).encode()
        elif command == "unar":
            member = args[-1]
            output = f"[Script Info]\n; {member}\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,你好".encode()
        else:
            raise AssertionError(args)
        return types.SimpleNamespace(stdout=output, stderr=b"")

    upload_session.shutil.which = fake_which
    upload_session.subprocess.run = fake_run

    try:
        archive_dependency = upload_session.ArchiveDependencyService(
            rar_dependency_mode="none",
            rar_tool_path="",
            rar_python_package=cls._rar_python_package,
            rar_tools=cls._rar_tools,
            sevenzip_tools=("unar",),
            normalize_text=lambda value: str(value or "").strip(),
            decode_preview_bytes=lambda raw: (raw or b"").decode("utf-8", errors="ignore"),
        )
        extractor = upload_session.ArchiveSubtitleExtractor(
            archive_dependency_service=archive_dependency,
            subtitle_exts=cls._subtitle_exts,
            hash_text=cls._hash_text,
            rar_python_package=cls._rar_python_package,
        )
        service = upload_session.UploadSessionService(
            data_path=tmp_path,
            subtitle_exts=cls._subtitle_exts,
            archive_exts=cls._archive_exts,
            rar_exts=cls._rar_exts,
            sevenzip_exts=cls._sevenzip_exts,
            default_session_hours=cls._default_session_hours,
            hash_text=cls._hash_text,
            extract_rar_subtitle_files=extractor.extract_rar_subtitle_files,
            extract_7z_subtitle_files=extractor.extract_7z_subtitle_files,
            normalize_text=lambda value: str(value or "").strip(),
            decode_preview_bytes=lambda raw: (raw or b"").decode("utf-8", errors="ignore"),
        )
        extracted = service.extract_subtitle_files("sample.7z", b"7z\xbc\xaf\x27\x1c" + b"payload", tmp_path)
    finally:
        upload_session.shutil.which = original_which
        upload_session.subprocess.run = original_run

    assert [item["source_name"] for item in extracted] == [
        "Jack.Reacher.2012.chs&eng.ass",
        "Jack.Reacher.2012.eng.ass",
    ]
    assert all(Path(item["stored_path"]).exists() for item in extracted)
    assert all(item["archive_name"] == "sample.7z" for item in extracted)


def test_language_suffix_supports_bilingual_codes():
    module, _, _ = load_plugin_module()
    language = plugin_submodule(module, "matching.subtitle_language")

    assert language.normalize_language_suffix("chi&eng") == "chi&eng"
    assert language.normalize_language_suffix("zh/en") == "chi&eng"
    assert language.normalize_language_suffix("chi+jpn") == "chi&jp"
    assert language.normalize_language_suffix("chi,kor") == "chi&kr"
    assert language.is_chinese_language_suffix("chi&eng") is True


def test_detect_language_profile_marks_bilingual_subtitles():
    module, _, _ = load_plugin_module()
    language = plugin_submodule(module, "matching.subtitle_language")
    cls = module.SubtitleManualUpload

    chinese = "这是中文字幕文本" * 30
    english = " This is an English subtitle line" * 30
    japanese = "これは日本語字幕です" * 30
    korean = "이것은 한국어 자막입니다" * 30

    assert language.detect_language_profile("movie.zh.en.srt", f"{chinese}{english}".encode(), cls._subtitle_exts)["suffix"] == "chi&eng"
    assert language.detect_language_profile("movie.srt", f"{chinese}{japanese}".encode(), cls._subtitle_exts)["suffix"] == "chi&jp"
    assert language.detect_language_profile("movie.srt", f"{chinese}{korean}".encode(), cls._subtitle_exts)["suffix"] == "chi&kr"


def test_detect_language_profile_prefers_suffix_token_before_subtitle_extension():
    module, _, _ = load_plugin_module()
    language = plugin_submodule(module, "matching.subtitle_language")
    cls = module.SubtitleManualUpload

    name = "Jack.Reacher.Never.Go.Back.2016.1080p.KORSUB.HDRip.x264.AAC2.0-STUTTERSHIT.eng.srt"
    assert language.detect_language_profile(name, b"", cls._subtitle_exts)["suffix"] == "eng"
    assert language.detect_language_profile("Example.Movie.2024.zh.en.ass", b"", cls._subtitle_exts)["suffix"] == "chi&eng"


def test_strm_target_skips_timeline_fixing(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    video = tmp_path / "Movie.strm"
    video.write_text("http://example.invalid/movie.mkv", encoding="utf-8")
    source = tmp_path / "subtitle.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    destination = tmp_path / "Movie.chi.srt"

    results, fixed_count, _ = plugin.services.writer().write_operations_to_disk(
        session_dir=session_dir,
        operations=[
            {
                "upload_info": {"upload_id": "u1", "source_name": "subtitle.srt", "archive_name": ""},
                "target_entry": {"id": "t1", "path": str(video), "basename": "Movie", "storage": "local"},
                "video_path": video,
                "source_path": source,
                "language_suffix": "chi",
                "destination_name": destination.name,
                "destination_path": destination,
            }
        ],
        fix_timeline=True,
    )

    assert destination.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert fixed_count == 0
    assert results[0]["timeline"]["enabled"] is True
    assert results[0]["timeline"]["applied"] is False
    assert results[0]["timeline"]["base"] == "strm"
    task = plugin.services.timeline_tasks().task_for_target_id("t1")
    assert task["status"] == "skipped"
    assert task["timeline"]["base"] == "strm"
    assert plugin.services.timeline_tasks().tasks_for_entries([{"id": "t1"}])["summary"]["skipped"] == 1


def test_timeline_task_store_summary_and_target_mapping(tmp_path):
    module, _, _ = load_plugin_module()
    timeline_tasks = plugin_submodule(module, "timeline.timeline_tasks")
    runtime_helpers = plugin_submodule(module, "runtime.runtime_helpers")
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"

    assert timeline_tasks.timeline_task_summary([]) == {
        "pending": 0,
        "in_progress": 0,
        "completed": 0,
        "skipped": 0,
        "failed": 0,
        "active": 0,
        "total": 0,
    }

    store = timeline_tasks.TimelineTaskStore(
        plugin,
        normalize_text=runtime_helpers.normalize_text,
        cache_loaded_at=runtime_helpers.cache_loaded_at,
        json_clone=runtime_helpers.json_clone,
        timeline_task_ttl_seconds=plugin._timeline_task_ttl_seconds,
        max_tasks=plugin._entry_map_max_size,
    )
    store.set_task(
        {
            "target_entry": {"id": "t1", "path": str(video), "basename": "Movie", "storage": "local"},
            "upload_info": {"source_name": "subtitle.srt"},
            "destination_name": "Movie.chi.srt",
        },
        status="completed",
        message="done",
    )

    data = store.tasks_for_entries([{"id": "t1"}, {"id": "missing"}])

    assert data["summary"]["completed"] == 1
    assert data["task_by_target"]["t1"]["status"] == "completed"
    assert data["task_by_target"]["missing"] is None


def test_write_operations_passes_allow_risky_offset_to_timeline_fixer(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    video = tmp_path / "Movie.mkv"
    source = tmp_path / "subtitle.srt"
    destination = tmp_path / "Movie.chi.srt"
    video.write_text("video", encoding="utf-8")
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    captured = {}

    def fake_fix(video_path, subtitle_path, output_path, **kwargs):
        captured["kwargs"] = kwargs
        output_path.write_bytes(subtitle_path.read_bytes())
        return module.TimelineFixResult(
            enabled=True,
            applied=True,
            reason="timeline adjusted",
            base="audio:webrtc",
            offset_seconds=121.0,
            scale_factor=1.0,
            score=0.8,
        )

    module.fix_subtitle_timeline = fake_fix

    results, fixed_count, _ = plugin.services.writer().write_operations_to_disk(
        session_dir=session_dir,
        operations=[
            {
                "upload_info": {"upload_id": "u1", "source_name": "subtitle.srt", "archive_name": ""},
                "target_entry": {"id": "t1", "path": str(video), "basename": "Movie", "storage": "local"},
                "video_path": video,
                "source_path": source,
                "language_suffix": "chi",
                "destination_name": destination.name,
                "destination_path": destination,
            }
        ],
        fix_timeline=True,
        allow_risky_offset=True,
    )

    assert destination.exists()
    assert fixed_count == 1
    assert results[0]["timeline"]["offset_seconds"] == 121.0
    assert captured["kwargs"]["allow_risky_offset"] is True


def test_write_operations_rejects_low_confidence_timeline_result(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    video = tmp_path / "Movie.mkv"
    source = tmp_path / "subtitle.srt"
    destination = tmp_path / "Movie.chi.srt"
    video.write_text("video", encoding="utf-8")
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")

    def fake_fix(video_path, subtitle_path, output_path, **kwargs):
        output_path.write_bytes(subtitle_path.read_bytes())
        return module.TimelineFixResult(
            enabled=True,
            applied=False,
            reason="timeline alignment rejected",
            base="audio:rms",
            offset_seconds=-30.0,
            scale_factor=1.0,
            score=0.2,
            confidence="low",
            score_margin=0.0,
            risk_flags=["rms_low_precision"],
        )

    module.fix_subtitle_timeline = fake_fix

    try:
        plugin.services.writer().write_operations_to_disk(
            session_dir=session_dir,
            operations=[
                {
                    "upload_info": {"upload_id": "u1", "source_name": "subtitle.srt", "archive_name": ""},
                    "target_entry": {"id": "t1", "path": str(video), "basename": "Movie", "storage": "local"},
                    "video_path": video,
                    "source_path": source,
                    "language_suffix": "chi",
                    "destination_name": destination.name,
                    "destination_path": destination,
                }
            ],
            fix_timeline=True,
        )
    except module.HTTPException as exc:
        assert exc.status_code == 409
        assert "智能调轴低可信" in exc.detail
    else:
        raise AssertionError("low confidence timeline result should block write")
    assert not destination.exists()
    assert plugin.services.timeline_tasks().task_for_target_id("t1")["status"] == "failed"


def test_write_operations_allows_explicit_low_confidence_override(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    video = tmp_path / "Movie.mkv"
    source = tmp_path / "subtitle.srt"
    destination = tmp_path / "Movie.chi.srt"
    video.write_text("video", encoding="utf-8")
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")

    def fake_fix(video_path, subtitle_path, output_path, **kwargs):
        output_path.write_bytes(subtitle_path.read_bytes())
        return module.TimelineFixResult(
            enabled=True,
            applied=False,
            reason="timeline alignment rejected",
            base="audio:rms",
            offset_seconds=-30.0,
            scale_factor=1.0,
            score=0.2,
            confidence="low",
            score_margin=0.0,
            risk_flags=["rms_low_precision"],
        )

    module.fix_subtitle_timeline = fake_fix

    results, _, _ = plugin.services.writer().write_operations_to_disk(
        session_dir=session_dir,
        operations=[
            {
                "upload_info": {"upload_id": "u1", "source_name": "subtitle.srt", "archive_name": ""},
                "target_entry": {"id": "t1", "path": str(video), "basename": "Movie", "storage": "local"},
                "video_path": video,
                "source_path": source,
                "language_suffix": "chi",
                "destination_name": destination.name,
                "destination_path": destination,
            }
        ],
        fix_timeline=True,
        force_low_confidence=True,
    )

    assert destination.exists()
    assert results[0]["timeline"]["confidence"] == "low"
    assert plugin.services.timeline_tasks().task_for_target_id("t1")["status"] == "completed"


def test_low_confidence_timeline_result_blocks_auto_write():
    module, _, _ = load_plugin_module()
    subtitle_writer = plugin_submodule(module, "matching.subtitle_writer")

    result = module.TimelineFixResult(
        enabled=True,
        applied=False,
        reason="timeline alignment rejected",
        base="audio:rms",
        offset_seconds=-119.99,
        scale_factor=1.0,
        score=0.2,
        confidence="low",
        score_margin=0.0,
        risk_flags=["weak_score_margin", "rms_low_precision"],
    )

    assert subtitle_writer.timeline_result_blocks_auto_write(result) is True


def test_offset_below_threshold_with_blocking_risk_still_blocks_auto_write():
    module, _, _ = load_plugin_module()
    subtitle_writer = plugin_submodule(module, "matching.subtitle_writer")

    result = module.TimelineFixResult(
        enabled=True,
        applied=False,
        reason="offset below threshold",
        base="audio:webrtc",
        offset_seconds=0.1,
        scale_factor=1.0,
        score=0.2,
        confidence="rejected",
        score_margin=0.0,
        risk_flags=["local_alignment_unstable"],
    )

    assert subtitle_writer.timeline_result_blocks_auto_write(result) is True


def test_target_payload_marks_strm_resources(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.strm"
    video.write_text("http://example.invalid/movie.mkv", encoding="utf-8")

    target = plugin._target_from_entry(
        {"id": "t1", "path": str(video), "basename": "Movie", "target_label": "Movie", "storage": "local"}
    )

    assert target["path"] == str(video)
    assert target["is_stream"] is True


def test_ai_submit_skips_strm_without_requiring_autosub_plugin(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.strm"
    video.write_text("http://example.invalid/movie.mkv", encoding="utf-8")

    result = plugin.services.autosub_bridge().submit_autosub_for_entries(
        [{"id": "t1", "path": str(video), "basename": "Movie", "target_label": "Movie", "storage": "local"}]
    )

    assert result["added"] == []
    assert result["failed"] == []
    assert result["skipped"][0]["reason"] == "STRM 资源暂不支持 AI 生成字幕"


def test_ai_restart_with_selected_external_subtitle_submits_matched_override(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    subtitle = tmp_path / "Movie.eng.srt"
    video.write_text("video", encoding="utf-8")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    entry = {"id": "t1", "path": str(video), "basename": "Movie", "target_label": "Movie", "storage": "local"}
    captured = {}

    plugin._autosub_plugin = lambda: (types.SimpleNamespace(restart_tasks=lambda **kwargs: None), "")

    def fake_submit(entries, subtitle_overrides=None, **kwargs):
        captured["entries"] = entries
        captured["overrides"] = subtitle_overrides
        captured["submit_kwargs"] = kwargs
        return {"added": [{"path": entries[0]["path"]}], "skipped": [], "failed": [], "targets": [], "tasks": {}}

    bridge = plugin.services.autosub_bridge()
    bridge.submit_autosub_for_entries = fake_submit
    override_services(plugin, autosub_bridge=bridge)

    result = plugin.services.autosub_bridge().restart_autosub_for_entries(
        [entry],
        source_policy="matched_external",
        overwrite_policy="new_variant",
        source_subtitle_path=str(subtitle),
    )

    override = captured["overrides"][str(video)]
    assert result["added"]
    assert captured["entries"] == [entry]
    assert override["subtitle_path"] == str(subtitle)
    assert override["lang"] == "en"
    assert override["source_policy"] == "matched_external"
    assert override["source_name"] == subtitle.name
    assert captured["submit_kwargs"]["trigger"] == "manual"
    assert captured["submit_kwargs"]["source_policy"] == "matched_external"
    assert captured["submit_kwargs"]["overwrite_policy"] == "new_variant"


def test_ai_submit_with_selected_external_subtitle_submits_matched_override(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    subtitle = tmp_path / "Movie.eng.srt"
    video.write_text("video", encoding="utf-8")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    entry = {"id": "t1", "path": str(video), "basename": "Movie", "target_label": "Movie", "storage": "local"}
    remember_targets(plugin, module, [entry])
    captured = {}

    class FakeBridge:
        def selected_external_subtitle_override_for_entries(self, entries, **kwargs):
            captured["selected_kwargs"] = kwargs
            return {
                entries[0]["path"]: {
                    "subtitle_path": kwargs["source_subtitle_path"],
                    "lang": "en",
                    "source_policy": "matched_external",
                    "source_name": Path(kwargs["source_subtitle_path"]).name,
                    "timeline_fixed": False,
                    "overwrite_policy": kwargs["overwrite_policy"],
                }
            }

        def submit_autosub_for_entries(self, entries, subtitle_overrides=None, **kwargs):
            captured["entries"] = entries
            captured["overrides"] = subtitle_overrides
            captured["submit_kwargs"] = kwargs
            return {"added": [{"path": entries[0]["path"]}], "skipped": [], "failed": [], "targets": [], "tasks": {}}

    override_services(plugin, autosub_bridge=FakeBridge())

    response = asyncio.run(
        api_endpoint(plugin, "/ai_submit")(
            FakeRequest(
                {
                    "target_ids": ["t1"],
                    "source_policy": "matched_external",
                    "source_subtitle_path": str(subtitle),
                    "overwrite_policy": "new_variant",
                }
            )
        )
    )

    override = captured["overrides"][str(video)]
    assert response["success"] is True
    assert captured["entries"] == [entry]
    assert override["subtitle_path"] == str(subtitle)
    assert override["lang"] == "en"
    assert override["source_policy"] == "matched_external"
    assert captured["selected_kwargs"]["source_subtitle_path"] == str(subtitle)
    assert captured["submit_kwargs"]["trigger"] == "manual"
    assert captured["submit_kwargs"]["source_policy"] == "matched_external"
    assert captured["submit_kwargs"]["overwrite_policy"] == "new_variant"


def test_ai_submit_with_asr_source_policy_forwards_source_choice(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    video.write_text("video", encoding="utf-8")
    entry = {"id": "t1", "path": str(video), "basename": "Movie", "target_label": "Movie", "storage": "local"}
    remember_targets(plugin, module, [entry])
    captured = {}

    class FakeBridge:
        def submit_autosub_for_entries(self, entries, subtitle_overrides=None, **kwargs):
            captured["entries"] = entries
            captured["overrides"] = subtitle_overrides
            captured["submit_kwargs"] = kwargs
            return {"added": [{"path": entries[0]["path"]}], "skipped": [], "failed": [], "targets": [], "tasks": {}}

    override_services(plugin, autosub_bridge=FakeBridge())

    response = asyncio.run(
        api_endpoint(plugin, "/ai_submit")(
            FakeRequest(
                {
                    "target_ids": ["t1"],
                    "source_policy": "asr",
                    "overwrite_policy": "new_variant",
                }
            )
        )
    )

    assert response["success"] is True
    assert captured["overrides"] is None
    assert captured["submit_kwargs"]["trigger"] == "manual"
    assert captured["submit_kwargs"]["source_policy"] == "asr"
    assert captured["submit_kwargs"]["overwrite_policy"] == "new_variant"


def test_autosub_tasks_for_entries_returns_all_tasks_by_target(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    video.write_text("video", encoding="utf-8")
    entry = {"id": "t1", "path": str(video), "basename": "Movie", "target_label": "Movie", "storage": "local"}
    tasks = [
        {"task_id": "new-asr", "video_file": str(video), "status": "completed", "output_variant": "aiasr"},
        {"task_id": "old-match", "video_file": str(video), "status": "completed", "output_variant": "aimatch"},
        {"task_id": "old-embedded", "video_file": str(video), "status": "failed", "output_variant": "aiembedded"},
    ]
    fake_plugin = types.SimpleNamespace(
        _status_payload=lambda: {"available": True, "enabled": True},
        tasks_payload=lambda **kwargs: {"status": {"available": True}, "tasks": tasks},
    )
    plugin._ai_link_enabled = True
    plugin._autosub_plugin = lambda: (fake_plugin, "")

    result = plugin._autosub_tasks_for_entries([entry])

    assert [task["task_id"] for task in result["tasks"]] == ["new-asr", "old-match", "old-embedded"]
    assert result["task_by_target"]["t1"]["task_id"] == "new-asr"
    assert [task["task_id"] for task in result["tasks_by_target"]["t1"]] == ["new-asr", "old-match", "old-embedded"]
    assert result["summary"]["total"] == 3


def test_ai_restart_forwards_selected_task_ids_without_using_latest(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    video.write_text("video", encoding="utf-8")
    entry = {"id": "t1", "path": str(video), "basename": "Movie", "target_label": "Movie", "storage": "local"}
    captured = {}

    class FakeAutoSub:
        def _status_payload(self):
            return {"available": True, "enabled": True}

        def tasks_payload(self, **kwargs):
            return {
                "status": {"available": True},
                "tasks": [
                    {"task_id": "latest-asr", "video_file": str(video), "status": "completed", "active": False},
                    {"task_id": "old-match", "video_file": str(video), "status": "completed", "active": False},
                ],
            }

        def restart_tasks(self, **kwargs):
            captured.update(kwargs)
            return {"added": [{"task_id": "old-match"}], "skipped": [], "failed": []}

    plugin._ai_link_enabled = True
    plugin._autosub_plugin = lambda: (FakeAutoSub(), "")

    result = plugin.services.autosub_bridge().restart_autosub_for_entries([entry], task_ids=["old-match"])

    assert captured["task_ids"] == ["old-match"]
    assert result["added"] == [{"task_id": "old-match"}]


def test_api_ai_restart_accepts_task_ids_without_target_ids(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    video.write_text("video", encoding="utf-8")
    entry = {"id": "t1", "path": str(video), "basename": "Movie", "target_label": "Movie", "storage": "local"}
    remember_targets(plugin, module, [entry])
    captured = {}

    def fake_restart(entries, **kwargs):
        captured["entries"] = entries
        captured["kwargs"] = kwargs
        return {"added": [{"task_id": "old-match"}], "skipped": [], "failed": [], "tasks": {}}

    class FakeBridge:
        def restart_autosub_for_entries(self, entries, **kwargs):
            return fake_restart(entries, **kwargs)

    override_services(plugin, autosub_bridge=FakeBridge())

    response = asyncio.run(api_endpoint(plugin, "/ai_restart")(FakeRequest({"task_ids": ["old-match"]})))

    assert response["success"] is True
    assert captured["entries"] == [entry]
    assert captured["kwargs"]["task_ids"] == ["old-match"]


def test_ai_restart_filters_task_ids_outside_current_target(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video1 = tmp_path / "Episode1.mkv"
    video2 = tmp_path / "Episode2.mkv"
    video1.write_text("video", encoding="utf-8")
    video2.write_text("video", encoding="utf-8")
    entry1 = {"id": "e1", "path": str(video1), "basename": "Episode1", "target_label": "E01", "storage": "local"}
    entry2 = {"id": "e2", "path": str(video2), "basename": "Episode2", "target_label": "E02", "storage": "local"}
    captured = {}

    class FakeAutoSub:
        def _status_payload(self):
            return {"available": True, "enabled": True}

        def tasks_payload(self, **kwargs):
            return {
                "status": {"available": True},
                "tasks": [
                    {"task_id": "task-e1", "video_file": str(video1), "status": "completed", "active": False},
                    {"task_id": "task-e2", "video_file": str(video2), "status": "completed", "active": False},
                ],
            }

        def restart_tasks(self, **kwargs):
            captured.update(kwargs)
            return {"added": [{"task_id": "task-e1"}], "skipped": [], "failed": []}

    plugin._ai_link_enabled = True
    plugin._autosub_plugin = lambda: (FakeAutoSub(), "")

    result = plugin.services.autosub_bridge().restart_autosub_for_entries([entry1], task_ids=["task-e1", "task-e2"])

    assert captured["task_ids"] == ["task-e1"]
    assert result["skipped"][0]["task_id"] == "task-e2"
    assert "不属于当前可操作目标" in result["skipped"][0]["reason"]


def test_ai_restart_task_ids_for_locked_target_are_skipped(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Locked.mkv"
    video.write_text("video", encoding="utf-8")
    locked_entry = {"id": "locked", "path": str(video), "basename": "Locked", "target_label": "Locked", "storage": "local"}
    remember_targets(plugin, module, [locked_entry])
    called = {"restart": False}

    class FakeAutoSub:
        def _status_payload(self):
            return {"available": True, "enabled": True}

        def tasks_payload(self, **kwargs):
            return {"status": {"available": True}, "tasks": []}

        def restart_tasks(self, **kwargs):
            called["restart"] = True
            return {"added": [], "skipped": [], "failed": []}

    plugin._ai_link_enabled = True
    plugin._autosub_plugin = lambda: (FakeAutoSub(), "")

    response = asyncio.run(
        api_endpoint(plugin, "/ai_restart")(
            FakeRequest({"task_ids": ["locked-task"], "locked_target_ids": ["locked"]})
        )
    )

    assert response["success"] is True
    assert called["restart"] is False
    assert response["data"]["skipped"][0]["task_id"] == "locked-task"


def test_ai_restart_explicit_task_ids_filtered_empty_does_not_fallback(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video1 = tmp_path / "Episode1.mkv"
    video2 = tmp_path / "Episode2.mkv"
    video1.write_text("video", encoding="utf-8")
    video2.write_text("video", encoding="utf-8")
    entry1 = {"id": "e1", "path": str(video1), "basename": "Episode1", "target_label": "E01", "storage": "local"}
    called = {"restart": False}

    class FakeAutoSub:
        def _status_payload(self):
            return {"available": True, "enabled": True}

        def tasks_payload(self, **kwargs):
            return {
                "status": {"available": True},
                "tasks": [
                    {"task_id": "task-e1", "video_file": str(video1), "status": "completed", "active": False},
                    {"task_id": "task-e2", "video_file": str(video2), "status": "completed", "active": False},
                ],
            }

        def restart_tasks(self, **kwargs):
            called["restart"] = True
            return {"added": [], "skipped": [], "failed": []}

    plugin._ai_link_enabled = True
    plugin._autosub_plugin = lambda: (FakeAutoSub(), "")

    result = plugin.services.autosub_bridge().restart_autosub_for_entries([entry1], task_ids=["task-e2"])

    assert called["restart"] is False
    assert result["added"] == []
    assert result["skipped"][0]["task_id"] == "task-e2"
    assert "不属于当前可操作目标" in result["skipped"][0]["reason"]


def test_api_ai_restart_task_ids_with_empty_target_cache_does_not_query_global_tasks(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    called = {"tasks_payload": False, "restart": False}

    class FakeAutoSub:
        def _status_payload(self):
            return {"available": True, "enabled": True}

        def tasks_payload(self, **kwargs):
            called["tasks_payload"] = True
            return {"status": {"available": True}, "tasks": [{"task_id": "global-task", "video_file": str(tmp_path / "Locked.mkv")}]}

        def restart_tasks(self, **kwargs):
            called["restart"] = True
            return {"added": [], "skipped": [], "failed": []}

    plugin._ai_link_enabled = True
    plugin._entry_map.clear()
    plugin._autosub_plugin = lambda: (FakeAutoSub(), "")

    response = asyncio.run(api_endpoint(plugin, "/ai_restart")(FakeRequest({"task_ids": ["global-task"]})))

    assert response["success"] is True
    assert called["tasks_payload"] is False
    assert called["restart"] is False
    assert response["data"]["skipped"][0]["task_id"] == "global-task"


def test_api_ai_restart_all_requested_targets_locked_with_task_ids_does_not_fallback(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video1 = tmp_path / "Episode1.mkv"
    video2 = tmp_path / "Episode2.mkv"
    video1.write_text("video", encoding="utf-8")
    video2.write_text("video", encoding="utf-8")
    remember_targets(plugin, module,
        [
            {"id": "e1", "path": str(video1), "basename": "Episode1", "target_label": "E01", "storage": "local"},
            {"id": "e2", "path": str(video2), "basename": "Episode2", "target_label": "E02", "storage": "local"},
        ]
    )
    called = {"restart": False}

    def fake_restart(entries, **kwargs):
        called["restart"] = True
        return {"added": [], "skipped": [], "failed": [], "tasks": {}}

    class FakeBridge:
        def restart_autosub_for_entries(self, entries, **kwargs):
            return fake_restart(entries, **kwargs)

    override_services(plugin, autosub_bridge=FakeBridge())

    response = asyncio.run(
        api_endpoint(plugin, "/ai_restart")(
            FakeRequest({"target_ids": ["e1"], "task_ids": ["task-e1"], "locked_target_ids": ["e1"]})
        )
    )

    assert response["success"] is True
    assert called["restart"] is False
    skipped = response["data"]["skipped"]
    assert any(item.get("target_id") == "e1" for item in skipped)
    assert any(item.get("task_id") == "task-e1" for item in skipped)


def test_api_ai_restart_mixed_unlocked_and_locked_task_ids(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video1 = tmp_path / "Episode1.mkv"
    video2 = tmp_path / "Episode2.mkv"
    video1.write_text("video", encoding="utf-8")
    video2.write_text("video", encoding="utf-8")
    entry1 = {"id": "e1", "path": str(video1), "basename": "Episode1", "target_label": "E01", "storage": "local"}
    entry2 = {"id": "e2", "path": str(video2), "basename": "Episode2", "target_label": "E02", "storage": "local"}
    remember_targets(plugin, module, [entry1, entry2])
    captured = {}

    class FakeAutoSub:
        def _status_payload(self):
            return {"available": True, "enabled": True}

        def tasks_payload(self, **kwargs):
            return {
                "status": {"available": True},
                "tasks": [
                    {"task_id": "task-e1", "video_file": str(video1), "status": "completed", "active": False},
                    {"task_id": "task-e2", "video_file": str(video2), "status": "completed", "active": False},
                ],
            }

        def restart_tasks(self, **kwargs):
            captured.update(kwargs)
            return {"added": [{"task_id": "task-e1"}], "skipped": [], "failed": []}

    plugin._ai_link_enabled = True
    plugin._autosub_plugin = lambda: (FakeAutoSub(), "")

    response = asyncio.run(
        api_endpoint(plugin, "/ai_restart")(
            FakeRequest(
                {
                    "target_ids": ["e1", "e2"],
                    "task_ids": ["task-e1", "task-e2"],
                    "locked_target_ids": ["e2"],
                }
            )
        )
    )

    assert response["success"] is True
    assert captured["task_ids"] == ["task-e1"]
    skipped = response["data"]["skipped"]
    assert any(item.get("target_id") == "e2" for item in skipped)
    assert any(item.get("task_id") == "task-e2" for item in skipped)


def test_api_ai_restart_stale_target_ids_with_task_ids_returns_400(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    video.write_text("video", encoding="utf-8")
    remember_targets(plugin, module, [{"id": "valid", "path": str(video), "basename": "Movie", "target_label": "Movie", "storage": "local"}])

    try:
        asyncio.run(api_endpoint(plugin, "/ai_restart")(FakeRequest({"target_ids": ["missing"], "task_ids": ["task-1"]})))
    except module.HTTPException as exc:
        assert exc.status_code == 400
        assert "目标视频已失效" in exc.detail
    else:
        raise AssertionError("stale target should be rejected even when task_ids are provided")


def test_api_ai_restart_mixed_stale_target_ids_returns_400(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    video.write_text("video", encoding="utf-8")
    remember_targets(plugin, module, [{"id": "valid", "path": str(video), "basename": "Movie", "target_label": "Movie", "storage": "local"}])

    try:
        asyncio.run(api_endpoint(plugin, "/ai_restart")(FakeRequest({"target_ids": ["valid", "missing"], "task_ids": ["task-1"]})))
    except module.HTTPException as exc:
        assert exc.status_code == 400
        assert "目标视频已失效" in exc.detail
    else:
        raise AssertionError("mixed stale target should be rejected")


def test_ai_restart_rejects_external_subtitle_outside_current_target(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    subtitle = tmp_path / "Other.eng.srt"
    video.write_text("video", encoding="utf-8")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    entry = {"id": "t1", "path": str(video), "basename": "Movie", "target_label": "Movie", "storage": "local"}

    try:
        plugin.services.autosub_bridge().selected_external_subtitle_override_for_entries(
            [entry],
            source_subtitle_path=str(subtitle),
        )
    except module.HTTPException as exc:
        assert exc.status_code == 400
        assert "当前集" in exc.detail
    else:
        raise AssertionError("should reject unrelated subtitle path")


def test_ai_restart_matched_external_filters_explicit_task_ids_before_submit(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video1 = tmp_path / "Episode1.mkv"
    video2 = tmp_path / "Episode2.mkv"
    subtitle = tmp_path / "Episode1.eng.srt"
    video1.write_text("video", encoding="utf-8")
    video2.write_text("video", encoding="utf-8")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    entry1 = {"id": "e1", "path": str(video1), "basename": "Episode1", "target_label": "E01", "storage": "local"}
    called = {"submit": False}

    class FakeAutoSub:
        def _status_payload(self):
            return {"available": True, "enabled": True}

        def tasks_payload(self, **kwargs):
            return {
                "status": {"available": True},
                "tasks": [
                    {"task_id": "task-e1", "video_file": str(video1), "status": "completed", "active": False},
                    {"task_id": "task-e2", "video_file": str(video2), "status": "completed", "active": False},
                ],
            }

        def restart_tasks(self, **kwargs):
            raise AssertionError("matched external path should submit, not restart")

    def fake_submit(*args, **kwargs):
        called["submit"] = True
        return {"added": [], "skipped": [], "failed": [], "targets": [], "tasks": {}}

    plugin._ai_link_enabled = True
    plugin._autosub_plugin = lambda: (FakeAutoSub(), "")
    bridge = plugin.services.autosub_bridge()
    bridge.submit_autosub_for_entries = fake_submit
    override_services(plugin, autosub_bridge=bridge)

    result = plugin.services.autosub_bridge().restart_autosub_for_entries(
        [entry1],
        source_policy="matched_external",
        source_subtitle_path=str(subtitle),
        task_ids=["task-e2"],
    )

    assert called["submit"] is False
    assert result["added"] == []
    assert result["skipped"][0]["task_id"] == "task-e2"


def test_ai_restart_matched_external_backup_replace_becomes_new_variant(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Episode1.mkv"
    subtitle = tmp_path / "Episode1.eng.srt"
    video.write_text("video", encoding="utf-8")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    entry = {"id": "e1", "path": str(video), "basename": "Episode1", "target_label": "E01", "storage": "local"}
    captured = {}

    class FakeAutoSub:
        def _status_payload(self):
            return {"available": True, "enabled": True}

        def tasks_payload(self, **kwargs):
            return {
                "status": {"available": True},
                "tasks": [{"task_id": "task-e1", "video_file": str(video), "status": "completed", "active": False}],
            }

        def restart_tasks(self, **kwargs):
            raise AssertionError("matched external path should submit, not restart")

    def fake_submit(*args, **kwargs):
        captured["submit_kwargs"] = kwargs
        return {"added": [{"task_id": "new-task"}], "skipped": [], "failed": [], "targets": [], "tasks": {}}

    plugin._ai_link_enabled = True
    plugin._autosub_plugin = lambda: (FakeAutoSub(), "")
    bridge = plugin.services.autosub_bridge()
    bridge.submit_autosub_for_entries = fake_submit
    override_services(plugin, autosub_bridge=bridge)

    result = plugin.services.autosub_bridge().restart_autosub_for_entries(
        [entry],
        source_policy="matched_external",
        overwrite_policy="backup_replace",
        source_subtitle_path=str(subtitle),
        source_subtitle_lang="eng",
        task_ids=["task-e1"],
    )

    assert result["added"][0]["task_id"] == "new-task"
    override = captured["submit_kwargs"]["subtitle_overrides"][str(video)]
    assert override["overwrite_policy"] == "new_variant"
    assert captured["submit_kwargs"]["overwrite_policy"] == "new_variant"


def test_ai_tasks_empty_request_includes_tasks_by_target():
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)

    response = asyncio.run(api_endpoint(plugin, "/ai_tasks")(FakeRequest({})))

    assert response["success"] is True
    assert response["data"]["tasks_by_target"] == {}


def test_api_delete_subtitle_only_allows_target_subtitles(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    subtitle = tmp_path / "Movie.chi.srt"
    unrelated = tmp_path / "Other.chi.srt"
    video.write_text("video", encoding="utf-8")
    subtitle.write_text("subtitle", encoding="utf-8")
    unrelated.write_text("other", encoding="utf-8")
    entry = {"id": "t1", "path": str(video), "basename": "Movie", "target_label": "Movie", "storage": "local"}
    remember_targets(plugin, module, [entry])

    delete_endpoint = next(route["endpoint"] for route in plugin.get_api() if route["path"] == "/delete_subtitle")
    response = asyncio.run(delete_endpoint(FakeRequest({"target_id": "t1", "subtitle_path": str(subtitle)})))

    assert response["success"] is True
    assert not subtitle.exists()
    assert unrelated.exists()


def test_api_delete_subtitle_rejects_locked_target(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    subtitle = tmp_path / "Movie.chi.srt"
    video.write_text("video", encoding="utf-8")
    subtitle.write_text("subtitle", encoding="utf-8")
    entry = {"id": "t1", "path": str(video), "basename": "Movie", "target_label": "Movie", "storage": "local"}
    remember_targets(plugin, module, [entry])

    delete_endpoint = next(route["endpoint"] for route in plugin.get_api() if route["path"] == "/delete_subtitle")
    try:
        asyncio.run(delete_endpoint(FakeRequest({"target_id": "t1", "subtitle_path": str(subtitle), "locked_target_ids": ["t1"]})))
    except module.HTTPException as exc:
        assert exc.status_code == 423
        assert "锁定" in exc.detail
    else:
        raise AssertionError("locked target should reject subtitle deletion")

    assert subtitle.exists()


def test_clear_subtitles_skips_locked_targets_and_uses_enumerated_subtitles(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video1 = tmp_path / "Movie1.mkv"
    video2 = tmp_path / "Movie2.mkv"
    sub1 = tmp_path / "Movie1.chi.srt"
    sub2 = tmp_path / "Movie2.chi.srt"
    unrelated = tmp_path / "Other.chi.srt"
    for path in [video1, video2, sub1, sub2, unrelated]:
        path.write_text("data", encoding="utf-8")
    entries = [
        {"id": "t1", "path": str(video1), "basename": "Movie1", "target_label": "Movie1", "storage": "local"},
        {"id": "t2", "path": str(video2), "basename": "Movie2", "target_label": "Movie2", "storage": "local"},
    ]
    remember_targets(plugin, module, entries)

    clear_endpoint = next(route["endpoint"] for route in plugin.get_api() if route["path"] == "/clear_subtitles")
    response = asyncio.run(clear_endpoint(FakeRequest({"target_ids": ["t1", "t2"], "locked_target_ids": ["t2"]})))

    assert response["success"] is True
    assert response["data"]["count"] == 1
    assert not sub1.exists()
    assert sub2.exists()
    assert unrelated.exists()
    assert response["data"]["failed"][0]["target_id"] == "t2"


def test_restore_subtitle_backup_missing_returns_404(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    subtitle = tmp_path / "Movie.chi.srt"
    video.write_text("video", encoding="utf-8")
    subtitle.write_text("subtitle", encoding="utf-8")
    entry = {"id": "t1", "path": str(video), "basename": "Movie", "target_label": "Movie", "storage": "local"}
    remember_targets(plugin, module, [entry])

    restore_endpoint = next(
        route["endpoint"] for route in plugin.get_api() if route["path"] == "/restore_subtitle_backup"
    )
    try:
        asyncio.run(restore_endpoint(FakeRequest({"target_id": "t1", "subtitle_path": str(subtitle)})))
    except module.HTTPException as exc:
        assert exc.status_code == 404
        assert "没有找到调轴前备份" in exc.detail
    else:
        raise AssertionError("missing subtitle backup should return 404")


def test_search_media_candidates_returns_total_with_page_slice(tmp_path):
    module, histories, _ = load_plugin_module()
    plugin = make_plugin(module)
    histories.data = [
        make_history_entry(tmp_path, "a", "a.mkv", media_key="movie-a", media_type="movie", title="A", date="2024-01-01"),
        make_history_entry(tmp_path, "b", "b.mkv", media_key="movie-b", media_type="movie", title="B", date="2024-01-02"),
        make_history_entry(tmp_path, "c", "c.mkv", media_key="movie-c", media_type="movie", title="C", date="2024-01-03"),
    ]
    original_resolver = plugin._target_resolver()

    class NoTargetDetailResolver:
        def merge_seasons(self, entries):
            return original_resolver.merge_seasons(entries)

        def targets_for_media(self, **kwargs):
            raise AssertionError("media list search should not load target details")

    plugin._target_resolver = lambda: NoTargetDetailResolver()

    candidates, total = asyncio.run(
        plugin._local_media_catalog().search_media_candidates(keyword="", media_type="movie", limit=2, offset=1)
    )

    assert total == 3
    assert [item["title"] for item in candidates] == ["B", "A"]


def test_api_search_uses_local_media_catalog_service(tmp_path):
    module, histories, _ = load_plugin_module()
    plugin = make_plugin(module)
    histories.data = [
        make_history_entry(tmp_path, "a", "a.mkv", media_key="movie-a", media_type="movie", title="A", date="2024-01-01"),
    ]
    captured = {}

    class FakeCatalog:
        async def search_media_candidates(self, **kwargs):
            captured.update(kwargs)
            return [{"title": "A"}], 1

    override_services(plugin, local_media_catalog=FakeCatalog())

    search_endpoint = next(route["endpoint"] for route in plugin.get_api() if route["path"] == "/search")
    response = asyncio.run(
        search_endpoint(FakeRequest(query_params={"media_type": "movie", "page_size": "10"}))
    )

    assert response["data"]["total"] == 1
    assert response["data"]["medias"][0]["title"] == "A"
    assert captured["media_type"] == "movie"


def test_api_targets_uses_media_target_resolver_directly(tmp_path):
    module, histories, _ = load_plugin_module()
    plugin = make_plugin(module)
    histories.data = [
        make_history_entry(
            tmp_path,
            "m1",
            "Movie.mkv",
            media_key="movie",
            media_type="movie",
            title="Movie",
            year="2024",
            basename="Movie",
            target_label="Movie.mkv",
        )
    ]
    captured = {}

    class FakeResolver:
        def targets_for_media(self, **kwargs):
            captured.update(kwargs)
            return {
                "targets": [{"basename": "Movie"}],
                "target_count": 1,
                "all_target_count": 1,
                "selected_season": "",
            }

    override_services(plugin, target_resolver=FakeResolver())

    targets_endpoint = next(route["endpoint"] for route in plugin.get_api() if route["path"] == "/targets")
    response = targets_endpoint(
        FakeRequest(query_params={"media_type": "movie", "title": "Movie", "year": "2024"})
    )

    assert response["data"]["target_count"] == 1
    assert response["data"]["targets"][0]["basename"] == "Movie"
    assert captured["media_type"] == "movie"


def test_group_entries_exposes_thumbnail_poster_url():
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entries = [
        {
            "id": "a",
            "path": "/media/a.mkv",
            "media_key": "movie-a",
            "media_type": "movie",
            "title": "A",
            "date": "2024-01-01",
            "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
        }
    ]

    groups = plugin.services.local_media_catalog().group_entries_as_media(entries, 0)

    assert groups[0]["poster_url"] == "https://image.tmdb.org/t/p/w500/poster.jpg"
    assert groups[0]["poster_thumb_url"] == "https://image.tmdb.org/t/p/w185/poster.jpg"


def test_search_media_candidates_reuses_media_index_cache(tmp_path):
    module, histories, _ = load_plugin_module()
    plugin = make_plugin(module)
    histories.data = [
        make_history_entry(tmp_path, "a", "a.mkv", media_key="movie-a", media_type="movie", title="A", date="2024-01-01"),
        make_history_entry(tmp_path, "b", "b.mkv", media_key="movie-b", media_type="movie", title="B", date="2024-01-02"),
        make_history_entry(tmp_path, "c", "c.mkv", media_key="movie-c", media_type="movie", title="C", date="2024-01-03"),
    ]
    calls = {"count": 0}
    catalog = plugin.services.local_media_catalog()
    original_group = catalog.group_entries_as_media

    def counted_group(entries, limit):
        calls["count"] += 1
        return original_group(entries, limit)

    catalog.group_entries_as_media = counted_group
    first, first_total = asyncio.run(catalog.search_media_candidates(keyword="", media_type="movie", limit=2, offset=0))
    second, second_total = asyncio.run(catalog.search_media_candidates(keyword="", media_type="movie", limit=2, offset=2))

    assert first_total == second_total == 3
    assert [item["title"] for item in first] == ["C", "B"]
    assert [item["title"] for item in second] == ["A"]
    assert calls["count"] == 1


def test_match_history_groups_targets_with_subtitles(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video1 = tmp_path / "Show.S01E01.mkv"
    video2 = tmp_path / "Show.S01E02.mkv"
    sub1 = tmp_path / "Show.S01E01.chi.srt"
    sub2 = tmp_path / "Show.S01E02.eng.srt"
    for path in [video1, video2, sub1, sub2]:
        path.write_text("data", encoding="utf-8")
    plugin._local_entries_cache = {
        "loaded_at": module.datetime.now(),
        "entries": [
            {
                "id": "e1",
                "media_key": "tv-show",
                "media_type": "tv",
                "title": "Show",
                "path": str(video1),
                "basename": "Show.S01E01",
                "season": 1,
                "episode": 1,
                "target_label": "S01E01",
                "storage": "local",
            },
            {
                "id": "e2",
                "media_key": "tv-show",
                "media_type": "tv",
                "title": "Show",
                "path": str(video2),
                "basename": "Show.S01E02",
                "season": 1,
                "episode": 2,
                "target_label": "S01E02",
                "storage": "local",
            },
        ],
        "media_count": 1,
        "persisted": False,
    }

    items = plugin.services.history().match_history_items(keyword="", media_type="tv")

    assert len(items) == 1
    assert items[0]["subtitle_count"] == 2
    assert [target["episode"] for target in items[0]["targets"]] == [1, 2]


def test_match_history_cache_reuses_scanned_items_until_invalidated(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    subtitle = tmp_path / "Movie.chi.srt"
    video.write_text("video", encoding="utf-8")
    subtitle.write_text("subtitle", encoding="utf-8")
    plugin._local_entries_cache = {
        "loaded_at": module.datetime.now(),
        "entries": [
            {
                "id": "m1",
                "media_key": "movie",
                "media_type": "movie",
                "title": "Movie",
                "path": str(video),
                "basename": "Movie",
                "target_label": "Movie",
                "storage": "local",
            }
        ],
        "media_count": 1,
        "persisted": False,
    }
    calls = {"count": 0}
    original_inventory = plugin._subtitle_inventory()
    original = original_inventory.subtitle_files_for_target

    def counted_subtitles(entry):
        calls["count"] += 1
        return original(entry)

    override_services(plugin, subtitle_inventory=types.SimpleNamespace(subtitle_files_for_target=counted_subtitles))

    history = plugin.services.history()
    first = history.match_history_items()
    second = history.match_history_items()
    history.invalidate_match_history_cache()
    third = history.match_history_items()

    assert first == second == third
    assert calls["count"] == 1


def test_match_history_rebuilds_only_changed_subtitle_directory(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first_video = first_dir / "First.mkv"
    second_video = second_dir / "Second.mkv"
    first_video.write_text("video", encoding="utf-8")
    second_video.write_text("video", encoding="utf-8")
    (first_dir / "First.chi.srt").write_text("subtitle", encoding="utf-8")
    (second_dir / "Second.chi.srt").write_text("subtitle", encoding="utf-8")
    entries = [
        {"id": "first", "media_key": "first", "media_type": "movie", "title": "First", "path": str(first_video), "basename": "First", "target_label": "First", "storage": "local"},
        {"id": "second", "media_key": "second", "media_type": "movie", "title": "Second", "path": str(second_video), "basename": "Second", "target_label": "Second", "storage": "local"},
    ]
    scans = []

    class Inventory:
        def directory_signatures_for_entries(self, values):
            return {
                str(Path(entry["path"]).parent): str(Path(entry["path"]).parent.stat().st_mtime_ns)
                for entry in values
            }

        def subtitle_files_for_targets(self, values):
            scans.append(sorted(str(Path(entry["path"]).parent) for entry in values))
            return {
                entry["id"]: [{"name": Path(entry["path"]).stem + ".chi.srt", "modified_at": "2026-01-01T00:00:00"}]
                for entry in values
            }

    class Resolver:
        def target_from_entry(self, entry, *, subtitles):
            return {"id": entry["id"], "basename": entry["basename"], "subtitles": subtitles, "season": 0, "episode": 0}

    override_services(plugin, subtitle_inventory=Inventory(), target_resolver=Resolver())
    history = plugin.services.history()
    history.rebuild_match_history_cache(entries=entries)
    history.rebuild_match_history_cache(entries=entries)
    (second_dir / "new.ass").write_text("new", encoding="utf-8")
    history.rebuild_match_history_cache(entries=entries)

    assert len(scans) == 3
    assert sorted(item[0] for item in scans[:2]) == sorted([str(first_dir), str(second_dir)])
    assert scans[2] == [str(second_dir)]


def test_match_history_page_uses_current_snapshot_without_filesystem_scan():
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    now = module.datetime.now()
    plugin._match_history_cache = {
        "loaded_at": now,
        "validated_at": now,
        "signature": "snapshot",
        "items": [
            {
                "id": "movie",
                "media_type": "movie",
                "title": "Movie",
                "targets": [{"id": "m1", "basename": "Movie", "subtitles": []}],
            }
        ],
        "entry_count": 1,
        "persisted": False,
    }
    plugin._entry_filesystem_signature = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("cached page must not inspect the filesystem")
    )

    page = plugin.services.history().match_history_page(page=1, page_size=20)

    assert page["total"] == 1
    assert page["refreshing"] is False
    assert page["items"][0]["targets"][0]["basename"] == "Movie"


def test_match_history_page_starts_only_one_background_refresh_for_missing_snapshot(monkeypatch):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    history_module = plugin_submodule(module, "matching.subtitle_history")
    refresh_started = module.threading.Event()
    allow_refresh_to_finish = module.threading.Event()
    refresh_finished = module.threading.Event()
    calls = {"count": 0}

    def blocked_rebuild(_history, **_kwargs):
        calls["count"] += 1
        refresh_started.set()
        allow_refresh_to_finish.wait(timeout=2)
        refresh_finished.set()
        return []

    monkeypatch.setattr(history_module.SubtitleHistory, "rebuild_match_history_cache", blocked_rebuild)
    endpoint = next(route["endpoint"] for route in plugin.get_api() if route["path"] == "/match_history")

    first = endpoint(FakeRequest(query_params={"page": "1", "page_size": "20"}))
    second = endpoint(FakeRequest(query_params={"page": "1", "page_size": "20"}))

    assert first["data"]["ready"] is False
    assert first["data"]["refreshing"] is True
    assert second["data"]["refreshing"] is True
    assert refresh_started.wait(timeout=1)
    assert calls["count"] == 1

    allow_refresh_to_finish.set()
    assert refresh_finished.wait(timeout=1)


def test_match_history_rebuild_populates_first_snapshot_from_empty_local_cache(tmp_path):
    module, histories, _ = load_plugin_module()
    plugin = make_plugin(module)
    histories.data = [
        make_history_entry(
            tmp_path,
            "m1",
            "Movie.mkv",
            media_key="movie",
            media_type="movie",
            title="Movie",
            storage="local",
        )
    ]
    (tmp_path / "Movie.chi.srt").write_text("subtitle", encoding="utf-8")

    items = plugin.services.history().rebuild_match_history_cache()

    assert [item["id"] for item in items] == ["movie"]
    assert plugin._match_history_cache["loaded_at"] is not None
    assert [entry["id"] for entry in plugin._local_entries_cache["entries"]] == ["m1"]


def test_subtitle_inventory_batches_entries_by_directory(tmp_path, monkeypatch):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    inventory_module = plugin_submodule(module, "catalog.subtitle_inventory")
    video1 = tmp_path / "Show.S01E01.mkv"
    video2 = tmp_path / "Show.S01E02.mkv"
    subtitle1 = tmp_path / "Show.S01E01.chi.srt"
    subtitle2 = tmp_path / "Show.S01E02.chi.srt"
    for path in (video1, video2, subtitle1, subtitle2):
        path.write_text(path.name, encoding="utf-8")

    original_iterdir = inventory_module.Path.iterdir
    calls = {"count": 0}

    def counted_iterdir(path):
        if path == tmp_path:
            calls["count"] += 1
        return original_iterdir(path)

    monkeypatch.setattr(inventory_module.Path, "iterdir", counted_iterdir)
    entries = [
        {"id": "e1", "path": str(video1), "storage": "local"},
        {"id": "e2", "path": str(video2), "storage": "local"},
    ]

    subtitles = plugin._subtitle_inventory().subtitle_files_for_targets(entries)

    assert calls["count"] == 1
    assert [item["name"] for item in subtitles["e1"]] == [subtitle1.name]
    assert [item["name"] for item in subtitles["e2"]] == [subtitle2.name]


def test_match_history_filters_deleted_local_targets(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    subtitle = tmp_path / "Movie.chi.srt"
    video.write_text("video", encoding="utf-8")
    subtitle.write_text("subtitle", encoding="utf-8")
    entry = {
        "id": "m1",
        "media_key": "movie",
        "media_type": "movie",
        "title": "Movie",
        "path": str(video),
        "basename": "Movie",
        "target_label": "Movie",
        "storage": "local",
    }
    plugin._local_entries_cache = {
        "loaded_at": module.datetime.now(),
        "entries": [entry],
        "media_count": 1,
        "persisted": False,
    }
    remember_targets(plugin, module, [entry])

    history = plugin.services.history()
    assert len(history.match_history_items()) == 1

    video.unlink()
    assert history.match_history_items() == []
    assert plugin._local_media_catalog().cache_status()["entry_count"] == 0


def test_match_history_cache_invalidates_when_external_subtitle_changes(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    video.write_text("video", encoding="utf-8")
    plugin._local_entries_cache = {
        "loaded_at": module.datetime.now(),
        "entries": [
            {
                "id": "m1",
                "media_key": "movie",
                "media_type": "movie",
                "title": "Movie",
                "path": str(video),
                "basename": "Movie",
                "target_label": "Movie",
                "storage": "local",
            }
        ],
        "media_count": 1,
        "persisted": False,
    }

    history = plugin.services.history()
    assert history.match_history_items() == []

    (tmp_path / "Movie.chi.srt").write_text("subtitle", encoding="utf-8")
    future = module.time.time() + 10
    module.os.utime(tmp_path, (future, future))

    items = history.match_history_items()
    assert len(items) == 1
    assert items[0]["subtitle_count"] == 1


def test_subtitle_history_service_persists_and_restores_cache(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    history_module = plugin_submodule(module, "matching.subtitle_history")
    target_resolver = plugin_submodule(module, "catalog.target_resolver")
    runtime_helpers = plugin_submodule(module, "runtime.runtime_helpers")
    history = history_module.SubtitleHistory(
        plugin,
        http_exception=module.HTTPException,
        logger=module.logger,
        target_entry_cache=target_resolver.TargetEntryCache(
            plugin._entry_map,
            max_size=plugin._entry_map_max_size,
            normalize_text=runtime_helpers.normalize_text,
        ),
    )
    video = tmp_path / "Movie.mkv"
    video.write_text("video", encoding="utf-8")
    target = {
        "id": "m1",
        "path": str(video),
        "basename": "Movie",
        "target_label": "Movie",
        "storage": "local",
    }
    plugin._match_history_cache = {
        "loaded_at": module.datetime.now(),
        "signature": "sig",
        "items": [{"id": "movie", "targets": [target]}],
        "entry_count": 1,
        "persisted": False,
    }

    history.persist_match_history_cache()
    plugin._match_history_cache = {"loaded_at": None, "signature": "", "items": [], "entry_count": 0, "persisted": False}
    plugin._entry_map.clear()

    assert history.restore_persisted_match_history_cache() is True
    assert plugin._match_history_cache["persisted"] is True
    assert plugin._match_history_cache["items"][0]["targets"][0]["id"] == "m1"
    assert plugin._entry_map["m1"]["path"] == str(video)


def test_subtitle_history_rejects_legacy_cache_version(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    history = plugin.services.history()
    cache_file = history.match_history_cache_file()
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps(
            {
                "version": 2,
                "loaded_at": module.datetime.now().isoformat(timespec="seconds"),
                "validated_at": module.datetime.now().isoformat(timespec="seconds"),
                "signature": "legacy",
                "entry_count": 0,
                "items": [],
            }
        ),
        encoding="utf-8",
    )

    assert history.restore_persisted_match_history_cache() is False


def test_api_match_history_builds_targets_with_media_target_resolver(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    subtitle = tmp_path / "Movie.chi.srt"
    video.write_text("video", encoding="utf-8")
    subtitle.write_text("subtitle", encoding="utf-8")
    plugin._local_entries_cache = {
        "loaded_at": module.datetime.now(),
        "entries": [
            {
                "id": "m1",
                "media_key": "movie",
                "media_type": "movie",
                "title": "Movie",
                "path": str(video),
                "basename": "Movie",
                "target_label": "Movie",
                "storage": "local",
            }
        ],
        "media_count": 1,
        "persisted": False,
    }
    plugin._target_from_entry = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("match history should build targets through MediaTargetResolver")
    )
    plugin.services.history().rebuild_match_history_cache()

    match_history_endpoint = next(
        route["endpoint"] for route in plugin.get_api() if route["path"] == "/match_history"
    )
    response = match_history_endpoint(FakeRequest(query_params={"media_type": "movie"}))

    assert response["data"]["total"] == 1
    assert response["data"]["items"][0]["targets"][0]["basename"] == "Movie"


def test_transfer_auto_dedupe_key_changes_when_same_path_is_reimported(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path)

    claimed, skipped = plugin._claim_transfer_auto_entries([entry])
    assert len(claimed) == 1
    assert skipped == 0

    claimed, skipped = plugin._claim_transfer_auto_entries([entry])
    assert claimed == []
    assert skipped == 1

    Path(entry["path"]).write_text("video reimported with different size", encoding="utf-8")
    claimed, skipped = plugin._claim_transfer_auto_entries([entry])
    assert len(claimed) == 1
    assert skipped == 0


def test_timeline_fix_existing_accepts_all_subtitles_for_target(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    chi = tmp_path / "Movie.chi.srt"
    eng = tmp_path / "Movie.eng.srt"
    video.write_text("video", encoding="utf-8")
    chi.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")
    eng.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    entry = {
        "id": "m1",
        "media_key": "movie",
        "media_type": "movie",
        "title": "Movie",
        "path": str(video),
        "basename": "Movie",
        "target_label": "Movie",
        "storage": "local",
    }
    plugin._local_entries_cache = {
        "loaded_at": module.datetime.now(),
        "entries": [entry],
        "media_count": 1,
        "persisted": False,
    }
    remember_targets(plugin, module, [entry])
    routes = plugin.get_api()
    timeline_api_module = plugin_submodule(module, "api.timeline_api")
    timeline_api_module.check_timeline_fixer_dependencies = lambda: {
        "available": True,
        "ffmpeg": True,
        "ffprobe": True,
        "modules": {},
    }

    class NoopThread:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def start(self):
            return None

    original_thread = timeline_api_module.threading.Thread
    timeline_api_module.threading.Thread = NoopThread
    try:
        timeline_endpoint = next(route["endpoint"] for route in routes if route["path"] == "/timeline_fix_existing")
        response = asyncio.run(timeline_endpoint(FakeRequest({"items": [{"target_id": "m1"}]})))
    finally:
        timeline_api_module.threading.Thread = original_thread

    assert response["success"] is True
    assert response["data"]["accepted"] == 2
    assert response["data"]["summary"]["pending"] == 2
    assert response["data"]["task_by_target"]["m1"]["status"] == "pending"


def setup_online_ai_translate(plugin, module, tmp_path):
    video = tmp_path / "Movie.mkv"
    video.write_text("video", encoding="utf-8")
    entry = {
        "id": "m1",
        "path": str(video),
        "basename": "Movie",
        "filename": "Movie.mkv",
        "target_label": "Movie",
        "storage": "local",
    }
    remember_targets(plugin, module, [entry])

    class FakeOnlineService:
        def download(self, results):
            return [
                {
                    "provider": "opensubtitles",
                    "source_name": "Movie.eng.srt",
                    "content": b"1\n00:00:01,000 --> 00:00:02,000\nHello\n",
                    "result": results[0],
                }
            ]

    captured = {}

    def fake_submit(entries, subtitle_overrides=None, **kwargs):
        captured["overrides"] = subtitle_overrides or {}
        captured["submit_kwargs"] = kwargs
        return {
            "added": [{"path": entries[0]["path"]}],
            "skipped": [],
            "failed": [],
            "targets": [plugin._target_from_entry(entries[0])],
            "tasks": {
                "status": {"available": True},
                "summary": {"total": 1, "active": 1, "pending": 1},
                "tasks": [{"target_id": "m1", "status": "pending"}],
                "task_by_target": {"m1": {"target_id": "m1", "status": "pending"}},
            },
        }

    class FakeAutosubBridge:
        def submit_autosub_for_entries(self, entries, subtitle_overrides=None, **kwargs):
            return fake_submit(entries, subtitle_overrides=subtitle_overrides, **kwargs)

    def fake_fix(video_path, subtitle_path, output_path, **kwargs):
        captured["fix_kwargs"] = kwargs
        output_path.write_bytes(subtitle_path.read_bytes())
        return module.TimelineFixResult(
            enabled=True,
            applied=True,
            reason="test",
            base="audio",
            offset_seconds=1.0,
            scale_factor=1.0,
            score=0.95,
        )

    override_services(plugin, online_subtitles=FakeOnlineService(), autosub_bridge=FakeAutosubBridge())
    setattr(plugin, "_suggest" + "_target", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("online AI should call target_resolver.suggest_target directly")
    ))
    setattr(plugin, "_auto_fill" + "_missing_targets", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("online AI should call target_resolver.auto_fill_missing_targets directly")
    ))
    setattr(plugin, "_build_write" + "_operations", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("online AI should call SubtitleWriter.build_write_operations directly")
    ))
    module.check_timeline_fixer_dependencies = lambda: {
        "available": True,
        "ffmpeg": "ffmpeg",
        "ffprobe": "ffprobe",
        "modules": {"pysubs2": True, "numpy": True},
    }
    module.fix_subtitle_timeline = fake_fix
    return entry, captured


def test_online_ai_translate_downloads_fixes_and_does_not_create_preview(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry, captured = setup_online_ai_translate(plugin, module, tmp_path)
    download_endpoint = next(route["endpoint"] for route in plugin.get_api() if route["path"] == "/online_download_preview")

    response = asyncio.run(
        download_endpoint(
            FakeRequest(
                {
                    "target_ids": ["m1"],
                    "submit_ai_translate": True,
                    "results": [
                        {
                            "provider": "opensubtitles",
                            "result_id": "r1",
                            "language_category": "english",
                            "title": "Movie English",
                        }
                    ],
                }
            )
        )
    )

    data = response["data"]
    assert response["success"] is True
    assert "session_id" not in data
    assert "items" not in data
    assert data["ai_translate"]["added"]
    assert data["tasks"]["task_by_target"]["m1"]["status"] == "pending"
    assert data["timeline_tasks"]["task_by_target"]["m1"]["status"] == "completed"
    assert data["fixed_subtitles"][0]["timeline"]["applied"] is True
    override = captured["overrides"][entry["path"]]
    assert Path(override["subtitle_path"]).exists()
    assert override["lang"] == "en"
    assert override["source_policy"] == "matched_external"
    assert override["timeline_fixed"] is True
    assert captured["submit_kwargs"]["trigger"] == "manual"
    assert captured["submit_kwargs"]["source_policy"] == "matched_external"
    assert captured["submit_kwargs"]["overwrite_policy"] == "new_variant"


def test_online_ai_translate_rejects_low_confidence_timeline_before_submit(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    setup_online_ai_translate(plugin, module, tmp_path)
    called = {"submit": False}

    def fake_submit(*args, **kwargs):
        called["submit"] = True
        return {}

    def fake_fix(video_path, subtitle_path, output_path, **kwargs):
        output_path.write_bytes(subtitle_path.read_bytes())
        return module.TimelineFixResult(
            enabled=True,
            applied=False,
            reason="timeline alignment rejected",
            base="audio:webrtc",
            offset_seconds=10.0,
            scale_factor=1.0,
            score=0.2,
            confidence="rejected",
            score_margin=0.0,
            risk_flags=["local_alignment_unstable"],
        )

    class FakeAutosubBridge:
        def submit_autosub_for_entries(self, *args, **kwargs):
            return fake_submit(*args, **kwargs)

    override_services(plugin, autosub_bridge=FakeAutosubBridge())
    module.fix_subtitle_timeline = fake_fix

    try:
        download_endpoint = next(route["endpoint"] for route in plugin.get_api() if route["path"] == "/online_download_preview")
        asyncio.run(
            download_endpoint(
                FakeRequest(
                    {
                        "target_ids": ["m1"],
                        "submit_ai_translate": True,
                        "results": [
                            {
                                "provider": "opensubtitles",
                                "result_id": "r1",
                                "language_category": "english",
                                "title": "Movie English",
                            }
                        ],
                    }
                )
            )
        )
    except module.HTTPException as exc:
        assert exc.status_code == 409
        assert "智能调轴低可信" in exc.detail
    else:
        raise AssertionError("low confidence timeline result should block AI submit")
    assert called["submit"] is False


def test_online_download_preview_submit_ai_rejects_mixed_stale_targets(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    setup_online_ai_translate(plugin, module, tmp_path)
    called = {"submit": False}

    class FakeOnlineAiService:
        def submit_online_ai_translate(self, *args, **kwargs):
            called["submit"] = True
            return {}

    override_services(plugin, online_ai=FakeOnlineAiService())

    try:
        download_endpoint = next(route["endpoint"] for route in plugin.get_api() if route["path"] == "/online_download_preview")
        asyncio.run(
            download_endpoint(
                FakeRequest(
                    {
                        "target_ids": ["m1", "missing"],
                        "submit_ai_translate": True,
                        "results": [
                            {
                                "provider": "opensubtitles",
                                "result_id": "r1",
                                "language_category": "english",
                                "title": "Movie English",
                            }
                        ],
                    }
                )
            )
        )
    except module.HTTPException as exc:
        assert exc.status_code == 400
        assert "目标视频已失效" in exc.detail
    else:
        raise AssertionError("mixed stale target should be rejected")
    assert called["submit"] is False


def test_online_ai_submit_endpoint_downloads_fixes_and_does_not_create_preview(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry, captured = setup_online_ai_translate(plugin, module, tmp_path)

    response = asyncio.run(
        api_endpoint(plugin, "/online_ai_submit")(
            FakeRequest(
                {
                    "target_ids": ["m1"],
                    "results": [
                        {
                            "provider": "opensubtitles",
                            "result_id": "r1",
                            "language_category": "english",
                            "title": "Movie English",
                        }
                    ],
                }
            )
        )
    )

    data = response["data"]
    assert response["success"] is True
    assert "session_id" not in data
    assert "items" not in data
    assert data["ai_translate"]["added"]
    assert data["tasks"]["task_by_target"]["m1"]["status"] == "pending"
    assert data["timeline_tasks"]["task_by_target"]["m1"]["status"] == "completed"
    assert Path(captured["overrides"][entry["path"]]["subtitle_path"]).exists()
    assert captured["submit_kwargs"]["source_policy"] == "matched_external"


def test_online_ai_submit_endpoint_uses_online_ai_service(tmp_path):
    module, _, _ = load_plugin_module()
    runtime_helpers = plugin_submodule(module, "runtime.runtime_helpers")
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    video.write_text("video", encoding="utf-8")
    entry = {
        "id": "m1",
        "path": str(video),
        "basename": "Movie",
        "filename": "Movie.mkv",
        "target_label": "Movie",
        "storage": "local",
    }
    remember_targets(plugin, module, [entry])
    captured = {}

    class FakeOnlineAiService:
        def submit_online_ai_translate(self, entries, selected_results, allow_risky_offset=False):
            captured["entries"] = entries
            captured["selected_results"] = selected_results
            captured["allow_risky_offset"] = allow_risky_offset
            return runtime_helpers.ok_response({"ai_translate": {"added": [{"path": entries[0]["path"]}]}, "tasks": {}})

    override_services(plugin, online_ai=FakeOnlineAiService())

    response = asyncio.run(
        api_endpoint(plugin, "/online_ai_submit")(
            FakeRequest(
                {
                    "target_ids": ["m1"],
                    "allow_risky_offset": True,
                    "results": [{"provider": "opensubtitles", "result_id": "r1", "language_category": "english"}],
                }
            )
        )
    )

    assert response["success"] is True
    assert captured["entries"] == [entry]
    assert captured["selected_results"][0]["result_id"] == "r1"
    assert captured["allow_risky_offset"] is True


def test_online_download_preview_submit_ai_uses_online_ai_service(tmp_path):
    module, _, _ = load_plugin_module()
    runtime_helpers = plugin_submodule(module, "runtime.runtime_helpers")
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    video.write_text("video", encoding="utf-8")
    entry = {
        "id": "m1",
        "path": str(video),
        "basename": "Movie",
        "filename": "Movie.mkv",
        "target_label": "Movie",
        "storage": "local",
    }
    remember_targets(plugin, module, [entry])
    captured = {}

    class FakeOnlineAiService:
        def submit_online_ai_translate(self, entries, selected_results, allow_risky_offset=False):
            captured["entries"] = entries
            captured["selected_results"] = selected_results
            captured["allow_risky_offset"] = allow_risky_offset
            return runtime_helpers.ok_response({"ai_translate": {"added": [{"path": entries[0]["path"]}]}, "tasks": {}})

    override_services(plugin, online_ai=FakeOnlineAiService())
    download_endpoint = next(route["endpoint"] for route in plugin.get_api() if route["path"] == "/online_download_preview")

    response = asyncio.run(
        download_endpoint(
            FakeRequest(
                {
                    "target_ids": ["m1"],
                    "submit_ai_translate": True,
                    "results": [{"provider": "opensubtitles", "result_id": "r1", "language_category": "english"}],
                }
            )
        )
    )

    assert response["success"] is True
    assert captured["entries"] == [entry]
    assert captured["selected_results"][0]["result_id"] == "r1"
    assert captured["allow_risky_offset"] is False


def test_online_ai_submit_stale_target_returns_400_without_name_error():
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)

    try:
        asyncio.run(
            api_endpoint(plugin, "/online_ai_submit")(
                FakeRequest(
                    {
                        "target_ids": ["missing"],
                        "results": [{"provider": "opensubtitles", "result_id": "r1"}],
                    }
                )
            )
        )
    except module.HTTPException as exc:
        assert exc.status_code == 400
        assert "目标视频已失效" in exc.detail
    else:
        raise AssertionError("stale target should be rejected")


def test_online_ai_submit_mixed_stale_target_returns_400(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    video.write_text("video", encoding="utf-8")
    remember_targets(plugin, module,
        [{"id": "m1", "path": str(video), "basename": "Movie", "target_label": "Movie", "storage": "local"}]
    )

    try:
        asyncio.run(
            api_endpoint(plugin, "/online_ai_submit")(
                FakeRequest(
                    {
                        "target_ids": ["m1", "missing"],
                        "results": [{"provider": "opensubtitles", "result_id": "r1"}],
                    }
                )
            )
        )
    except module.HTTPException as exc:
        assert exc.status_code == 400
        assert "目标视频已失效" in exc.detail
    else:
        raise AssertionError("mixed stale target should be rejected")


def test_online_ai_submit_can_allow_risky_offset_after_manual_confirm(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    _, captured = setup_online_ai_translate(plugin, module, tmp_path)

    response = asyncio.run(
        api_endpoint(plugin, "/online_ai_submit")(
            FakeRequest(
                {
                    "target_ids": ["m1"],
                    "allow_risky_offset": True,
                    "results": [
                        {
                            "provider": "opensubtitles",
                            "result_id": "r1",
                            "language_category": "english",
                            "title": "Movie English",
                        }
                    ],
                }
            )
        )
    )

    assert response["success"] is True
    assert captured["fix_kwargs"]["allow_risky_offset"] is True


def test_online_ai_submit_requires_timeline_fixer_before_download(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    video = tmp_path / "Movie.mkv"
    video.write_text("video", encoding="utf-8")
    remember_targets(plugin, module,
        [
            {
                "id": "m1",
                "path": str(video),
                "basename": "Movie",
                "filename": "Movie.mkv",
                "target_label": "Movie",
                "storage": "local",
            }
        ]
    )
    class DownloadShouldNotRun:
        def download(self, selected):
            raise AssertionError("timeline check should run before download")

    override_services(plugin, online_subtitles=DownloadShouldNotRun())
    module.check_timeline_fixer_dependencies = lambda: {
        "available": False,
        "ffmpeg": "",
        "ffprobe": "ffprobe",
        "modules": {"pysubs2": True},
    }

    try:
        asyncio.run(
            api_endpoint(plugin, "/online_ai_submit")(
                FakeRequest(
                    {
                        "target_ids": ["m1"],
                        "results": [
                            {
                                "provider": "opensubtitles",
                                "result_id": "r1",
                                "language_category": "english",
                                "title": "Movie English",
                            }
                        ],
                    }
                )
            )
        )
    except module.HTTPException as exc:
        assert exc.status_code == 409
        assert "智能调轴不可用" in exc.detail
    else:
        raise AssertionError("timeline dependency failure should block online AI submit")


def test_tmdb_aliases_reuse_online_title_cleaner():
    module, _, _ = load_plugin_module()
    online_subtitle = plugin_submodule(module, "online.online_subtitle")
    runtime_helpers = plugin_submodule(module, "runtime.runtime_helpers")

    aliases = runtime_helpers.tmdb_aliases(
        [
            {"iso_639_1": "tr", "name": "Turkish", "english_name": "Turkish", "data": {"title": "Hayalet Sürücü"}},
            {"iso_639_1": "fi", "name": "suomi", "english_name": "Finnish", "data": {"title": ""}},
            {"iso_639_1": "en", "name": "English", "english_name": "English", "data": {"title": "Ghost Rider"}},
        ],
        extract_title_aliases_func=online_subtitle.extract_title_aliases,
    )

    assert "Hayalet Sürücü" in aliases
    assert "Ghost Rider" in aliases
    assert "Turkish" not in aliases
    assert "suomi" not in aliases
    assert "Finnish" not in aliases


def test_tmdb_detail_payload_prefers_real_english_translation_title():
    module, _, _ = load_plugin_module()
    online_subtitle = plugin_submodule(module, "online.online_subtitle")
    runtime_helpers = plugin_submodule(module, "runtime.runtime_helpers")

    payload = runtime_helpers.tmdb_detail_payload(
        {
            "original_language": "en",
            "origin_country": ["US"],
            "original_title": "The Lord of the Rings: The Return of the King",
            "translations": [
                {
                    "iso_639_1": "de",
                    "name": "Deutsch",
                    "english_name": "German",
                    "data": {"title": "Der Herr der Ringe - Die Rueckkehr des Koenigs"},
                },
                {
                    "iso_639_1": "en",
                    "name": "English",
                    "english_name": "English",
                    "data": {"title": "The Lord of the Rings: The Return of the King"},
                },
            ],
        },
        extract_title_aliases_func=online_subtitle.extract_title_aliases,
    )

    assert payload["en_title"] == "The Lord of the Rings: The Return of the King"
    assert "Der Herr der Ringe - Die Rueckkehr des Koenigs" in payload["tmdb_aliases"]


def make_auto_entry(tmp_path, filename="Movie.mkv", **overrides):
    video = tmp_path / filename
    video.write_text("video", encoding="utf-8")
    entry = {
        "id": filename,
        "media_key": "movie-key",
        "media_type": "movie",
        "title": "Movie",
        "year": "2024",
        "tmdb_id": 123,
        "douban_id": "",
        "path": str(video),
        "basename": video.stem,
        "filename": video.name,
        "storage": "local",
        "library_name": "MoviePilot 入库事件",
        "target_label": video.name,
    }
    entry.update(overrides)
    return entry


def auto_transfer_service_with(plugin, **methods):
    service = plugin._auto_transfer_service()
    collaborator_overrides = {}
    for name, method in methods.items():
        if name in {"prepare_online_ai_subtitle_overrides", "submit_autosub_for_entries"}:
            collaborator_overrides[name] = method
            continue
        setattr(service, name, method)
    if collaborator_overrides:
        service._collaborators = replace(service._collaborators, **collaborator_overrides)
    override_services(plugin, auto_transfer=service)
    return service


@pytest.fixture(params=[
    pytest.param(("plugins.v3", "SubtitleManualUpload"), id="legacy-v3"),
    pytest.param(("plugins.v2", "NeoSubtitleMaster"), id="neo-v2"),
    pytest.param(("plugins.v3", "NeoSubtitleMaster"), id="neo-v3"),
])
def durable_auto_transfer_queue(tmp_path, request):
    generation, plugin_id = request.param
    module, _, _ = load_plugin_module(generation, plugin_id)
    runtime = plugin_submodule(module, "config.config_runtime")
    clock = types.SimpleNamespace(now=1_799_999_000.0)
    clock.time = lambda: clock.now
    state_path = tmp_path / "plugin-data" / "auto_transfer_queue.json"

    def new_queue(resume=True):
        plugin = make_plugin(module, plugin_id)
        plugin.get_data_path = lambda: state_path.parent
        runtime.reset_runtime_state(plugin)
        service = plugin._auto_transfer_service()
        service._queue._time = clock
        worker_starts = []
        service.ensure_transfer_auto_worker = lambda: worker_starts.append(True)
        service.restore(resume=resume)
        return plugin, service, worker_starts

    return new_queue, clock, state_path, runtime


@pytest.mark.parametrize("durable_auto_transfer_queue", [("plugins.v3", "NeoSubtitleMaster")], indirect=True)
@pytest.mark.parametrize("alternate_count", [0, 1, 2])
@pytest.mark.parametrize("challenge_scope", ["season", "auto"])
def test_neo_wechat_verification_pauses_batch_and_keeps_other_sources(
    tmp_path, durable_auto_transfer_queue, monkeypatch, alternate_count, challenge_scope,
):
    new_queue, clock, state_path, _ = durable_auto_transfer_queue
    plugin, service, _ = new_queue()
    module = sys.modules[type(plugin).__module__]
    model = plugin_submodule(module, "online_subtitles.models")
    entries = [
        make_auto_entry(
            tmp_path, filename=f"Dexter.S02E{episode:02}.mkv", id=f"ep{episode}",
            media_key="dexter", media_type="tv", title="Dexter", season=2, episode=episode,
        )
        for episode in (2, 3)
    ]
    plugin._auto_skip_chinese_media_on_transfer = False
    plugin._auto_check_emby_subtitles = False
    plugin._auto_transfer_subtitle_strategy = "online_source_only"
    plugin._auto_subtitle_language_strict = True
    plugin._auto_subtitle_language_priority = ["chi&eng"]
    plugin._auto_transfer_queue_debounce_seconds = 0
    plugin._apply_tmdb_detail = lambda *_: None
    plugin._update_auto_transfer_task = service.update_auto_transfer_task
    notifications = []
    monkeypatch.setattr(sys.modules["app.schemas.types"], "NotificationType", types.SimpleNamespace(Plugin="plugin"), raising=False)
    monkeypatch.setattr(plugin, "post_message", lambda **message: notifications.append(message), raising=False)
    monkeypatch.setattr(service, "auto_search_providers", lambda: ["subhd", "subtitlecat", "assrt"])
    monkeypatch.setattr(service, "_wait_online_rate_limit", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_auto_target_has_chinese_subtitle", lambda *args: False)

    searches, downloads, writes = [], [], []
    subhd_page = "https://subhd.tv/a/example"

    def search(*, providers, scope, targets, **kwargs):
        searches.append((scope, providers))
        results = []
        if "subhd" in providers and scope == challenge_scope:
            results.extend(
                {"provider": "subhd", "result_id": f"subhd-{i}", "title": "Dexter 简英双语", "score": 90 - i}
                for i in (1, 2, 3)
            )
        if "subtitlecat" in providers:
            results.extend(
                {"provider": "subtitlecat", "result_id": f"eng-{i}", "title": f"Dexter_track{i}_[eng]",
                 "translation_source": "original_upload", "score": 100}
                for i in (3, 6)
            )
        if "assrt" in providers and scope == "auto" and targets[0]["episode"] < 2 + alternate_count:
            results.append({"provider": "assrt", "result_id": "bilingual", "title": "Dexter 简英双语", "score": 90})
        return {"results": results}

    def download(selected):
        candidate = selected[0]
        downloads.append(candidate["result_id"])
        if candidate["provider"] == "subhd":
            assert model._automatic_download.get()
            raise model.SubHDWechatVerificationRequired(
                candidate_id=candidate["result_id"], page_url=subhd_page, code="023456",
            )
        assert candidate["provider"] == "assrt"
        return [{"source_name": "Dexter.ass", "content": b"ass", "result": candidate}]

    def write(*, target_entries, **kwargs):
        writes.append(target_entries[0]["id"])
        return {"status": "written", "written_count": 1}

    monkeypatch.setattr(service, "_online_service", lambda: types.SimpleNamespace(search=search, download=download))
    monkeypatch.setattr(service, "_extract_subtitle_files", lambda name, *args: [{"source_name": name}])
    monkeypatch.setattr(service, "auto_write_prepared_uploads_for_entries", write)
    assert service.enqueue_transfer_auto_entries(entries) == (2, 0)
    batch, _ = service.claim_next_auto_transfer_batch()
    assert len(batch) == 2
    service.process_transfer_auto_task_batch(batch)
    assert downloads == ["subhd-1"] + ["bilingual"] * alternate_count
    assert searches[-1] == ("auto", ["subtitlecat", "assrt"])
    snapshot = service.auto_transfer_queue_snapshot()
    assert writes == ["ep2", "ep3"][:alternate_count]
    assert snapshot["summary"]["completed"] == alternate_count, snapshot["tasks"]
    if alternate_count == 2:
        assert notifications == []
        return

    waiting = [task for task in snapshot["tasks"] if task["status"] == "waiting_verification"]
    assert snapshot["summary"]["waiting_verification"] == len(waiting) == 2 - alternate_count
    assert all(task["can_retry"] and task["result"]["verification_url"] == subhd_page for task in waiting)
    assert len(notifications) == 1
    assert all(word in notifications[0]["text"] for word in ("023456", "S02E03", subhd_page))
    assert ("S02E02" in notifications[0]["text"]) == (alternate_count == 0)
    assert "023456" not in state_path.read_text(encoding="utf-8")
    clock.now += 4 * 86400
    restored, restored_service, _ = new_queue()
    assert restored_service.auto_transfer_queue_snapshot()["summary"]["waiting_verification"] == len(waiting)
    assert restored_service.claim_next_auto_transfer_batch()[0] == []
    assert restored_service.enqueue_transfer_auto_entries(entries[alternate_count:]) == (0, len(waiting))
    task_id = waiting[0]["id"]
    assert restored_service.retry_auto_transfer_task(task_id)
    retry, _ = restored_service.claim_next_auto_transfer_batch()
    assert [task["id"] for task in retry] == [task_id]
    monkeypatch.setattr(restored_service, "_auto_target_has_chinese_subtitle", lambda *args: True)
    monkeypatch.setattr(restored_service, "_online_service", lambda: pytest.fail("existing subtitle should skip online search"))
    restored._update_auto_transfer_task = restored_service.update_auto_transfer_task
    restored_service.process_transfer_auto_task_batch(retry)
    assert restored._auto_transfer_tasks[task_id]["status"] == "skipped"
    assert len(notifications) == 1


def test_v3_auto_transfer_queue_restores_history_and_interrupted_work(tmp_path, durable_auto_transfer_queue):
    new_queue, clock, state_path, runtime = durable_auto_transfer_queue
    plugin, service, _ = new_queue()
    entries = [make_auto_entry(tmp_path, filename=f"{name}.mkv") for name in ("Done", "Interrupted", "Pending")]
    assert service.enqueue_transfer_auto_entries(entries) == (3, 0)
    done_id, interrupted_id, pending_id = plugin._auto_transfer_tasks
    service.update_auto_transfer_task(done_id, status="completed", result={"written_count": 1})
    batch, _ = service.claim_next_auto_transfer_batch()
    assert [task["id"] for task in batch] == [interrupted_id]
    service.stop()
    service.update_auto_transfer_task(interrupted_id, status="failed", message="late result from stopped worker")
    assert {task["id"]: task["status"] for task in json.loads(state_path.read_text(encoding="utf-8"))["tasks"]} == {
        done_id: "completed", interrupted_id: "in_progress", pending_id: "pending",
    }

    clock.now += 60
    restored, resumed_service, starts = new_queue()
    assert starts == [True]
    assert {task["id"]: task["status"] for task in resumed_service.auto_transfer_queue_snapshot()["tasks"]} == {
        done_id: "completed", interrupted_id: "pending", pending_id: "pending",
    }
    assert restored._auto_transfer_tasks[done_id]["result"] == {"written_count": 1}
    previous_lock = restored._transfer_auto_lock
    runtime.reset_runtime_state(restored)
    resumed_service.restore(resume=True)
    assert restored._transfer_auto_lock is previous_lock
    batch, _ = resumed_service.claim_next_auto_transfer_batch()
    assert [task["id"] for task in batch] == [interrupted_id]


def test_v3_auto_transfer_history_purges_at_three_days_without_a_page_request(tmp_path, durable_auto_transfer_queue):
    new_queue, clock, state_path, _ = durable_auto_transfer_queue
    plugin, service, _ = new_queue()
    entries = [make_auto_entry(tmp_path, filename=f"{name}.mkv") for name in ("Done", "Failed", "Skipped")]
    assert service.enqueue_transfer_auto_entries(entries) == (3, 0)
    for task_id, status in zip(list(plugin._auto_transfer_tasks), ("completed", "failed", "skipped")):
        service.update_auto_transfer_task(task_id, status=status)

    clock.now += 3 * 24 * 60 * 60 - 1
    _, restored_service, starts = new_queue()
    assert restored_service.claim_next_auto_transfer_batch() == ([], 1.0)
    assert starts == [True]
    assert len(json.loads(state_path.read_text(encoding="utf-8"))["tasks"]) == 3
    clock.now += 1
    assert restored_service.claim_next_auto_transfer_batch() == ([], -1)
    assert json.loads(state_path.read_text(encoding="utf-8"))["tasks"] == []


def test_v3_auto_transfer_expiry_and_manual_clear_keep_pending_task(tmp_path, durable_auto_transfer_queue):
    new_queue, clock, state_path, _ = durable_auto_transfer_queue
    plugin, service, _ = new_queue()
    entries = [make_auto_entry(tmp_path, filename=f"{name}.mkv") for name in ("Old", "New", "Pending")]
    assert service.enqueue_transfer_auto_entries(entries) == (3, 0)
    old_id, new_id, pending_id = plugin._auto_transfer_tasks
    service.update_auto_transfer_task(old_id, status="failed")
    clock.now += 3 * 24 * 60 * 60
    service.update_auto_transfer_task(new_id, status="skipped")

    _, restored_service, starts = new_queue(resume=False)
    assert not starts
    assert set(task["id"] for task in restored_service.auto_transfer_queue_snapshot()["tasks"]) == {new_id, pending_id}
    assert restored_service.clear_auto_transfer_history() == 1
    assert [item["id"] for item in json.loads(state_path.read_text(encoding="utf-8"))["tasks"]] == [pending_id]


def test_v3_directory_scan_checks_each_version_and_rechecks_after_mount_recovers(
    tmp_path, durable_auto_transfer_queue, monkeypatch,
):
    new_queue, clock, _, _ = durable_auto_transfer_queue
    media = tmp_path / "media"
    media.mkdir()
    subtitles = "1\n00:00:01,000 --> 00:00:02,000\n你好\nHello\n"

    def configure(plugin, service):
        plugin._enabled = True
        plugin._manual_strm_enabled = True
        plugin._manual_strm_paths = [str(media)]
        plugin._auto_subtitle_language_priority = ["chi&eng"]
        plugin._auto_check_emby_subtitles = False
        plugin._auto_skip_chinese_media_on_transfer = False
        plugin._auto_transfer_subtitle_strategy = "ai_source_only"
        plugin._auto_transfer_queue_debounce_seconds = 0
        service._time = clock
        override_services(
            plugin, auto_transfer=service,
            media_metadata=types.SimpleNamespace(enrich_media=lambda entry: dict(entry)),
        )

    plugin, service, _ = new_queue()
    configure(plugin, service)
    hd, four_k = [
        make_auto_entry(
            media, filename=name, media_type="tv", season=1, episode=1, origin="manual_strm",
        )
        for name in ("Show.S01E01.mkv", "Show.S01E01.2160p.mkv")
    ]
    Path(hd["path"]).with_suffix(".zh-CN.srt").write_text(subtitles.replace("Hello\n", ""), encoding="utf-8")
    Path(four_k["path"]).with_suffix(".中英双语.zh-CN.srt").write_text(subtitles, encoding="utf-8")
    monitor = plugin.services.manual_strm_monitor()
    monkeypatch.setattr(monitor, "_running", lambda: True)
    monitor._poll_root(media)
    assert not plugin._auto_transfer_tasks
    assert api_endpoint(plugin, "/local_directories/scan")()["data"]["started"]
    monitor._poll_root(media)
    task_id, = plugin._auto_transfer_tasks
    assert plugin._auto_transfer_tasks[task_id]["entry"]["path"] == hd["path"]
    assert service.enqueue_transfer_auto_entries_with_outcomes([{**hd, "id": "transfer-id"}]) == ["already_active"]

    detached = media.with_name("offline")
    media.rename(detached)
    batch, _ = service.claim_next_auto_transfer_batch()
    service.process_transfer_auto_task_batch(batch)
    assert service.claim_next_auto_transfer_batch() == ([], 30)
    assert plugin._auto_transfer_tasks[task_id]["message"] == "等待本地整理目录恢复"

    restored, resumed, _ = new_queue()
    configure(restored, resumed)
    assert resumed.claim_next_auto_transfer_batch() == ([], 30)
    detached.rename(media)
    Path(hd["path"]).with_suffix(".中英双语.zh-CN.srt").write_text(subtitles, encoding="utf-8")
    monkeypatch.setattr(resumed, "auto_search_write_subtitle", lambda *args, **kwargs: pytest.fail("已有合格字幕"))
    clock.now += 30
    batch, _ = resumed.claim_next_auto_transfer_batch()
    resumed.process_transfer_auto_task_batch(batch)
    assert restored._auto_transfer_tasks[task_id]["status"] == "skipped"
    assert "已有符合语言" in restored._auto_transfer_tasks[task_id]["message"]


def test_v3_hdr10_plus_sidecar_skips_online_search_for_its_video_version(tmp_path, monkeypatch):
    module, _, _ = load_plugin_module("plugins.v3")
    plugin = make_plugin(module)
    plugin._auto_subtitle_language_priority = ["chi&eng"]
    plugin._auto_skip_chinese_media_on_transfer = False
    four_k = make_auto_entry(tmp_path, filename="塔尔萨之王 - S04E03 - 2160p - WEB-DL HDR10+.mkv")
    hd = make_auto_entry(tmp_path, filename="塔尔萨之王 - S04E03 - 1080p - WEB-DL.mkv")
    Path(four_k["path"]).with_suffix(".中英双语.zh-CN.ass").write_text(
        "[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,你好 Hello",
        encoding="utf-8",
    )
    Path(hd["path"]).with_suffix(".zh-CN.srt").write_text("你好", encoding="utf-8")
    inventory = plugin.services.subtitle_inventory()
    monkeypatch.setattr(inventory, "embedded_subtitle_tracks_for_target", lambda entry: [])
    override_services(plugin, subtitle_inventory=inventory)
    searched = []

    def search(entry, target, **kwargs):
        searched.append(entry["id"])
        return {"status": "skipped", "needs_online_retry": True}

    service = auto_transfer_service_with(plugin, auto_search_write_subtitle=search)
    assert service.auto_process_transfer_entry(four_k)["reason"] == "目标已有符合语言要求的字幕"
    assert service.auto_process_transfer_entry(hd)["needs_online_retry"]
    assert searched == [hd["id"]]
    assert plugin._detect_language_profile("Tulsa.HDR10+.中英双语.zh-CN.ass", b"")["suffix"] == "chi&eng"
    assert plugin._detect_language_profile("Tulsa.chi&vie.ass", b"")["suffix"] == "chi&vie"


def test_v3_season_write_rechecks_late_sidecar_and_writes_other_targets(tmp_path, monkeypatch):
    module, _, _ = load_plugin_module("plugins.v3")
    plugin = make_plugin(module)
    plugin._auto_subtitle_language_priority = ["chi&eng"]
    plugin._traditional_to_simplified = False
    entries = [make_auto_entry(tmp_path, filename=f"Show.S01E{episode:02d}.strm", id=f"e{episode}",
                              media_type="tv", title="Show", season=1, episode=episode)
               for episode in (1, 2, 3)]
    first, second, blocked = [
        Path(entry["path"]).with_suffix(".中英双语.zh-CN.srt") for entry in entries
    ]
    blocked.mkdir()
    session = tmp_path / "download"
    session.mkdir()
    subtitles = "1\n00:00:01,000 --> 00:00:02,000\n字幕\nA line of English.\n"
    existing = subtitles.replace("字幕", "已有字幕")
    uploads = []
    for entry in entries:
        source = session / f'{entry["basename"]}.chi&eng.srt'
        source.write_text(subtitles, encoding="utf-8")
        uploads.append({"upload_id": entry["id"], "source_name": source.name, "stored_path": str(source), "ext": ".srt"})

    writer_module = plugin_submodule(module, "matching.subtitle_writer")
    normalize = writer_module.normalize_subtitle_encoding

    def copy_existing_during_preparation(source, encoded):
        if not first.exists():
            first.write_text(existing, encoding="utf-8")
        return normalize(source, encoded)

    monkeypatch.setattr(writer_module, "normalize_subtitle_encoding", copy_existing_during_preparation)
    service = auto_transfer_service_with(plugin)
    result = service.auto_write_prepared_uploads_for_entries(
        target_entries=entries, prepared_uploads=uploads, session_dir=session,
    )

    assert result["written_by_target"]["e2"]["output_path"] == str(second)
    assert result["skipped_by_target"] == {"e1": "目标已有符合语言要求的字幕"}
    assert "请在写入预览中确认" in result["conflicts_by_target"]["e3"]
    assert result["missing_target_ids"] == ["e3"]
    assert first.read_text(encoding="utf-8") == existing
    assert second.is_file()
    assert blocked.is_dir()


def test_v3_season_write_conflict_stops_followup_after_single_search_is_exhausted(tmp_path):
    module, _, _ = load_plugin_module("plugins.v3")
    plugin = make_plugin(module)
    plugin._auto_skip_chinese_media_on_transfer = False
    plugin._auto_subtitle_language_priority = ["chi&eng"]
    entry = make_auto_entry(tmp_path, filename="Show.S01E01.strm", media_type="tv", season=1, episode=1)
    conflict = "字幕写入冲突：Show.S01E01.中英双语.zh-CN.ass，请在写入预览中确认"
    searched = []
    service = auto_transfer_service_with(
        plugin,
        auto_write_from_season_cache=lambda entries: {},
        auto_search_write_season_package=lambda entries, task_ids: {"conflicts_by_target": {entry["id"]: conflict}},
        auto_search_write_subtitle=lambda entry, target, **kwargs: (
            searched.append(entry["id"]) or {"status": "skipped", "needs_online_retry": True}
        ),
    )
    result = service.auto_process_transfer_group([entry])[entry["id"]]
    assert searched == [entry["id"]]
    assert result["status"] == "skipped"
    assert result["reason"] == conflict
    assert not result.get("needs_online_retry")


def test_v3_bilingual_followup_survives_restart_and_stops_after_matching(tmp_path, durable_auto_transfer_queue):
    new_queue, clock, state_path, _ = durable_auto_transfer_queue
    plugin, service, _ = new_queue()
    plugin._auto_subtitle_language_priority = ["chi&eng", "chi&jp", "chi"]
    plugin._auto_transfer_queue_debounce_seconds = 0
    entry = make_auto_entry(tmp_path, filename="Show.S01E06.mkv", media_type="tv", season=1, episode=6)
    first_failure = clock.now
    missing = {"status": "skipped", "reason": "未找到双语", "needs_online_retry": True}
    service.auto_process_transfer_group = lambda entries, task_ids, **kwargs: {entry["id"]: missing}
    assert service.enqueue_transfer_auto_entries([entry]) == (1, 0)
    batch, _ = service.claim_next_auto_transfer_batch()
    service.process_transfer_auto_task_batch(batch)
    task_id = batch[0]["id"]

    (tmp_path / "another-file.txt").touch()
    clock.now += 301
    assert service.enqueue_transfer_auto_entries([entry]) == (0, 1)
    service.stop()
    restored, resumed, _ = new_queue()
    restored._auto_subtitle_language_priority = ["chi&eng", "chi&jp"]
    restored._auto_transfer_queue_debounce_seconds = 0
    resumed.auto_process_transfer_group = lambda *args, **kwargs: pytest.fail("补搜应直接搜索单集")
    resumed.auto_process_transfer_entry = lambda *args, **kwargs: missing

    for hours in (1, 6, 24, 48, 72):
        planned_ts = first_failure + hours * 3600
        assert restored._auto_transfer_tasks[task_id]["next_run_ts"] == planned_ts
        clock.now = planned_ts - 1
        assert resumed.claim_next_auto_transfer_batch() == ([], 1.0)
        clock.now = planned_ts
        batch, _ = resumed.claim_next_auto_transfer_batch()
        resumed.process_transfer_auto_task_batch(batch)

    resumed.auto_process_transfer_entry = lambda *args, **kwargs: {"status": "written", "written_count": 1}
    clock.now = first_failure + 96 * 3600
    batch, _ = resumed.claim_next_auto_transfer_batch()
    resumed.process_transfer_auto_task_batch(batch)
    saved = json.loads(state_path.read_text(encoding="utf-8"))["tasks"][0]
    assert saved["status"] == "completed"
    assert saved["next_run_ts"] == 0


def test_v3_batch_followup_only_queues_selected_versions_and_reports_partial_results(
    tmp_path, durable_auto_transfer_queue, monkeypatch,
):
    new_queue, _, state_path, _ = durable_auto_transfer_queue
    plugin, service, _ = new_queue()
    plugin._enabled = True
    plugin._auto_subtitle_language_priority = ["chi&eng"]
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    entries = {
        name: make_auto_entry(
            media_dir, filename=f"Show.S01E{episode:02d}.{name}.mkv",
            id=name, media_type="tv", season=1, episode=episode, writable=name != "read-only",
        )
        for name, episode in [
            ("unselected", 1), ("active", 2), ("1080p", 3), ("2160p", 3),
            ("locked", 4), ("read-only", 5), ("recent", 6), ("removed-file", 7),
        ]
    }
    entries["movie"] = make_auto_entry(media_dir, id="movie")
    Path(entries["removed-file"]["path"]).unlink()
    catalog = plugin.services.local_media_catalog()
    resolve_calls = []

    def resolve_targets(target_ids):
        resolve_calls.append(target_ids)
        return {target_id: entries[target_id] for target_id in target_ids if target_id in entries}

    monkeypatch.setattr(catalog, "resolve_targets", resolve_targets)
    override_services(plugin, auto_transfer=service, local_media_catalog=catalog)
    assert service.enqueue_transfer_auto_entries([entries["active"], entries["recent"]]) == (2, 0)
    active, recent = plugin._auto_transfer_tasks.values()
    service._queue.defer_bilingual_followup(active, {"reason": "未找到双语"})
    original_next_run = active["next_run_ts"]
    service.update_auto_transfer_task(recent["id"], status="completed")

    endpoint = api_endpoint(plugin, "/auto_transfer_queue/followup")
    payload = {
        "target_ids": [
            "active", "1080p", "2160p", "1080p", "locked", "read-only",
            "missing", "movie", "recent", "removed-file",
        ],
        "locked_target_ids": ["locked"],
    }
    response = asyncio.run(endpoint(FakeRequest(payload)))
    assert {item["target_id"]: item["status"] for item in response["data"]["followup_batch"]["items"]} == {
        "active": "already_active", "1080p": "queued", "2160p": "queued", "locked": "locked",
        "read-only": "read_only", "missing": "unavailable", "movie": "ineligible",
        "recent": "recent", "removed-file": "unavailable",
    }
    assert response["message"] == "已加入 2 个补搜任务，1 个已在队列，6 个未加入"
    assert resolve_calls == [list(dict.fromkeys(payload["target_ids"]))]
    assert active["next_run_ts"] == original_next_run
    repeated = asyncio.run(endpoint(FakeRequest({"target_ids": ["1080p", "2160p"]})))
    assert repeated["data"]["followup_batch"]["queued"] == 0
    assert repeated["data"]["followup_batch"]["already_active"] == 2
    saved = json.loads(state_path.read_text(encoding="utf-8"))["tasks"]
    assert {task["entry"]["id"] for task in saved} == {"active", "recent", "1080p", "2160p"}

    single = asyncio.run(endpoint(FakeRequest({"target_id": "unselected"})))
    assert single["message"] == "已加入双语字幕补搜"
    assert "followup_batch" not in single["data"]


def test_v3_batch_followup_keeps_existing_bilingual_and_retries_missing_episode(
    tmp_path, durable_auto_transfer_queue, monkeypatch,
):
    new_queue, clock, _, _ = durable_auto_transfer_queue
    plugin, service, _ = new_queue()
    plugin._enabled = True
    plugin._auto_check_emby_subtitles = False
    plugin._auto_skip_chinese_media_on_transfer = False
    plugin._auto_subtitle_language_priority = ["chi&eng", "chi&jp"]
    plugin._auto_subtitle_language_strict = False
    plugin._auto_transfer_queue_debounce_seconds = 0
    existing, missing = [
        make_auto_entry(tmp_path, filename=f"Show.S01E{episode:02d}.mkv", media_type="tv", season=1, episode=episode)
        for episode in (1, 2)
    ]
    subtitles = "1\n00:00:01,000 --> 00:00:02,000\n你好\nHello\n"
    bilingual = Path(existing["path"]).with_suffix(".中英双语.zh-CN.srt")
    bilingual.write_text(subtitles, encoding="utf-8")
    Path(missing["path"]).with_suffix(".zh-CN.srt").write_text(subtitles.replace("Hello\n", ""), encoding="utf-8")
    catalog = plugin.services.local_media_catalog()
    entries = {entry["id"]: entry for entry in (existing, missing)}
    monkeypatch.setattr(catalog, "resolve_targets", lambda target_ids: {target_id: entries[target_id] for target_id in target_ids})
    override_services(plugin, auto_transfer=service, local_media_catalog=catalog)
    searched = []

    def fake_online_search(entry, target, **kwargs):
        searched.append(entry["episode"])
        return {"status": "skipped", "reason": "未找到双语", "needs_online_retry": True}

    monkeypatch.setattr(service, "auto_search_write_subtitle", fake_online_search)
    response = asyncio.run(api_endpoint(plugin, "/auto_transfer_queue/followup")(
        FakeRequest({"target_ids": list(entries)}),
    ))
    assert response["data"]["followup_batch"]["queued"] == 2
    batch, _ = service.claim_next_auto_transfer_batch()
    service.process_transfer_auto_task_batch(batch)
    tasks = {task["episode"]: task for task in plugin._auto_transfer_tasks.values()}
    assert searched == [2]
    assert tasks[1]["status"] == "skipped"
    assert tasks[1]["message"] == "目标已有符合语言要求的字幕"
    assert tasks[2]["status"] == "pending"
    assert tasks[2]["next_run_ts"] == clock.now + 3600
    assert bilingual.read_text(encoding="utf-8") == subtitles


def test_v3_bilingual_followup_can_run_now_or_be_cancelled(tmp_path, durable_auto_transfer_queue):
    new_queue, clock, _, _ = durable_auto_transfer_queue
    plugin, service, _ = new_queue()
    plugin._auto_subtitle_language_priority = ["chi&eng"]
    plugin._auto_transfer_queue_debounce_seconds = 0
    entry = make_auto_entry(tmp_path, filename="Show.S01E06.mkv", media_type="tv", season=1, episode=6)
    missing = {"status": "skipped", "reason": "未找到双语", "needs_online_retry": True}
    service.auto_process_transfer_group = lambda entries, task_ids, **kwargs: {entry["id"]: missing}
    assert service.enqueue_transfer_auto_entries([entry]) == (1, 0)
    batch, _ = service.claim_next_auto_transfer_batch()
    task_id = batch[0]["id"]
    service.process_transfer_auto_task_batch(batch)
    assert service.auto_transfer_queue_snapshot()["tasks"][0]["waiting_bilingual"]
    assert service.retry_auto_transfer_task(task_id)
    batch, _ = service.claim_next_auto_transfer_batch()
    assert [task["id"] for task in batch] == [task_id]
    service.auto_process_transfer_entry = lambda *args, **kwargs: missing
    service.process_transfer_auto_task_batch(batch)
    assert service.cancel_bilingual_followup(task_id)
    clock.now += 7200
    assert service.claim_next_auto_transfer_batch()[0] == []
    assert service.auto_transfer_queue_snapshot()["tasks"][0]["message"] == "已取消双语字幕补搜"


def test_auto_transfer_queue_enqueues_tv_season_tasks_without_starting_immediately(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entries = [
        make_auto_entry(
            tmp_path,
            filename="Show.S01E01.mkv",
            media_key="tv-key",
            media_type="tv",
            title="Show",
            season=1,
            episode=1,
        ),
        make_auto_entry(
            tmp_path,
            filename="Show.S01E02.mkv",
            media_key="tv-key",
            media_type="tv",
            title="Show",
            season=1,
            episode=2,
        ),
    ]

    service = plugin._auto_transfer_service()
    service.ensure_transfer_auto_worker = lambda: None
    queued, skipped = service.enqueue_transfer_auto_entries(entries)
    snapshot = service.auto_transfer_queue_snapshot()

    assert queued == 2
    assert skipped == 0
    assert snapshot["summary"]["pending"] == 2
    assert len({task["group_key"] for task in snapshot["tasks"]}) == 1


def test_auto_transfer_queue_retry_force_and_clear_history_boundaries(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    service = plugin._auto_transfer_service()
    worker_calls = []
    service._queue._ensure_worker = lambda: worker_calls.append(True)
    entries = [
        make_auto_entry(tmp_path, filename="Movie-A.mkv", id="a"),
        make_auto_entry(tmp_path, filename="Movie-B.mkv", id="b"),
    ]

    queued, skipped = service.enqueue_transfer_auto_entries(entries)
    snapshot = service.auto_transfer_queue_snapshot()
    failed_id = snapshot["tasks"][0]["id"]
    active_id = snapshot["tasks"][1]["id"]
    service.update_auto_transfer_task(failed_id, status="failed", message="智能调轴低可信，已拒绝写入")

    failed_task = next(task for task in service.auto_transfer_queue_snapshot()["tasks"] if task["id"] == failed_id)
    assert queued == 2
    assert skipped == 0
    assert failed_task["next_run_at"] == ""
    assert failed_task["can_retry"] is True
    assert failed_task["can_force_low_confidence"] is True
    assert service.retry_auto_transfer_task(active_id) is False
    assert service.retry_auto_transfer_task(failed_id, force_low_confidence=True) is True

    retried = plugin._auto_transfer_tasks[failed_id]
    assert retried["entry"]["_force_timeline_write"] is True
    assert retried["group_key"] == retried["entry_key"]
    assert retried["retry_count"] == 1
    assert worker_calls

    service.update_auto_transfer_task(failed_id, status="completed", message="强制入库完成")
    assert service.clear_auto_transfer_history() == 1
    remaining_ids = {task["id"] for task in service.auto_transfer_queue_snapshot()["tasks"]}
    assert remaining_ids == {active_id}


def test_auto_transfer_queue_rejects_force_for_non_low_confidence_failure(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    service = plugin._auto_transfer_service()
    service._queue._ensure_worker = lambda: None
    service.enqueue_transfer_auto_entries([make_auto_entry(tmp_path, filename="Movie.mkv")])
    task_id = service.auto_transfer_queue_snapshot()["tasks"][0]["id"]
    service.update_auto_transfer_task(task_id, status="failed", message="网络下载失败")

    assert service.retry_auto_transfer_task(task_id, force_low_confidence=True) is False
    assert plugin._auto_transfer_tasks[task_id]["status"] == "failed"


def test_stop_service_uses_auto_transfer_service_directly():
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    captured = {}

    class FakeAutoTransferService:
        def stop(self):
            captured["stopped"] = True

    override_services(plugin, auto_transfer=FakeAutoTransferService())

    plugin.stop_service()

    assert captured["stopped"] is True


def test_v3_emby_recheck_keeps_other_episodes_moving(tmp_path, monkeypatch):
    module, _, _ = load_plugin_module("plugins.v3")
    plugin = make_plugin(module)
    plugin.get_data_path = lambda: tmp_path / "plugin-data"
    plugin_submodule(module, "config.config_runtime").reset_runtime_state(plugin)
    plugin._auto_check_emby_subtitles = True
    plugin._auto_skip_chinese_media_on_transfer = False
    plugin._auto_transfer_queue_debounce_seconds = 0
    plugin._auto_subtitle_language_strict = True
    plugin._auto_subtitle_language_priority = ["chi", "cht"]
    service = plugin._auto_transfer_service()
    logs = []
    monkeypatch.setattr(service._logger, "info", lambda message, *args: logs.append(message % args))
    service._queue._ensure_worker = lambda: None
    clock = types.SimpleNamespace(time=lambda: 100.0)
    service._time = service._queue._time = clock
    entries = [
        make_auto_entry(
            tmp_path, filename=f"Show.S01E0{i}.strm",
            media_key="tv-key", media_type="tv", season=1, episode=i,
        )
        for i in (1, 2, 3, 4, 5)
    ]
    local_forced = Path(entries[1]["path"]).with_suffix(".zh-CN.forced.srt")
    local_forced.write_text("1\n00:00:01,000 --> 00:00:02,000\n招牌\n", encoding="utf-8")
    target = service._target_from_entry(entries[1])
    assert target["subtitles"][0]["language_suffix"] == "chi"
    assert not service._auto_target_has_chinese_subtitle(entries[1], target)
    searches = []
    emby_checks = []

    def tracks(entry, mappings):
        assert mappings == ""
        emby_checks.append(entry["episode"])
        if entry["episode"] == 4 or (entry["episode"] == 1 and emby_checks.count(1) == 1):
            return "pending", []
        if entry["episode"] == 5:
            return "ready", [{
                "IsExternal": True, "Codec": "ass", "Language": "zh-CN",
                "Path": "/emby/Show.S01E05.中英双语.zh-CN.default.ass",
            }]
        if entry["episode"] in (1, 3):
            return "ready", [{
                "IsExternal": False, "IsForced": False, "Codec": "subrip",
                "Language": "chi", "Title": "Traditional Chinese",
            }]
        return "ready", [
            {"IsExternal": False, "Codec": "subrip", "Language": "eng"},
            {"IsExternal": False, "Codec": "subrip", "Language": "chi", "IsForced": True},
        ]

    def search_group(group, task_ids):
        searches.append([entry["episode"] for entry in group])
        return {entry["id"]: {"status": "written"} for entry in group}

    override_services(
        plugin, auto_transfer=service,
        emby_refresh=types.SimpleNamespace(subtitle_tracks_for_entry=tracks),
    )
    monkeypatch.setattr(service, "auto_process_transfer_group", search_group)
    service.enqueue_transfer_auto_entries(entries[:2])
    batch, _ = service._queue.claim_next_batch()
    service.process_transfer_auto_task_batch(batch)

    assert searches == [[2]]
    assert emby_checks == [1, 2]
    assert any("Emby 已返回当前版本轨道" in log and "S01E02" in log for log in logs)
    waiting = next(task for task in plugin._auto_transfer_tasks.values() if task["episode"] == 1)
    assert waiting["status"] == "pending"
    assert waiting["next_run_ts"] == 130
    assert service._queue.claim_next_batch() == ([], 1.0)

    service.enqueue_transfer_auto_entries(entries[2:3])
    batch, _ = service._queue.claim_next_batch()
    assert [task["episode"] for task in batch] == [3]
    service.process_transfer_auto_task_batch(batch)

    clock.time = lambda: 130
    batch, _ = service._queue.claim_next_batch()
    service.process_transfer_auto_task_batch(batch)
    assert searches == [[2]]
    assert emby_checks == [1, 2, 3, 1]
    assert waiting["status"] == "skipped"
    assert "繁中" in waiting["message"]

    plugin._auto_subtitle_language_priority = ["chi&eng"]
    service.enqueue_transfer_auto_entries(entries[3:])
    batch, _ = service._queue.claim_next_batch()
    service.process_transfer_auto_task_batch(batch)
    last_task = next(task for task in plugin._auto_transfer_tasks.values() if task["episode"] == 5)
    assert last_task["status"] == "skipped"
    assert "外挂字幕（中英双语）" in last_task["message"]

    clock.time = lambda: 160
    batch, _ = service._queue.claim_next_batch()
    service.process_transfer_auto_task_batch(batch)
    assert searches == [[2], [4]]
    assert emby_checks == [1, 2, 3, 1, 4, 5, 4]

    inventory = plugin.services.subtitle_inventory()
    monkeypatch.setattr(inventory, "probe_embedded_subtitle_tracks", lambda entry: [])
    override_services(plugin, auto_transfer=service, subtitle_inventory=inventory)
    local_entry = make_auto_entry(tmp_path, filename="Local.mkv")
    assert service._check_emby_subtitles_for_task({"entry": local_entry}) is None
    assert any("入库字幕检测使用本地轨道" in log and "Local.mkv" in log for log in logs)
    assert emby_checks == [1, 2, 3, 1, 4, 5, 4]


def test_v3_emby_offline_wait_recovers_after_restart(tmp_path, durable_auto_transfer_queue):
    new_queue, clock, _, _ = durable_auto_transfer_queue
    scope = [{"server": "nas", "id": "1", "paths": [str(tmp_path)]}]
    entry = make_auto_entry(tmp_path, filename="Show.S01E01.strm", media_type="tv", season=1, episode=1)

    def configure(plugin, service, answer):
        plugin._auto_check_emby_subtitles = True
        plugin._auto_skip_chinese_media_on_transfer = False
        plugin._auto_subtitle_language_priority = ["chi&eng"]
        plugin._auto_subtitle_language_strict = False
        plugin._auto_transfer_queue_debounce_seconds = 0
        plugin._emby_query_libraries = scope
        plugin._emby_subtitle_path_mappings = ""
        service._time = clock

        def read_tracks(target, mappings, *, scope):
            assert scope == plugin._emby_query_libraries
            return answer

        override_services(plugin, auto_transfer=service, emby_refresh=types.SimpleNamespace(
            subtitle_tracks_for_entry=read_tracks,
        ))

    plugin, service, _ = new_queue()
    configure(plugin, service, ("offline", []))
    assert service.enqueue_transfer_auto_entries([entry]) == (1, 0)
    batch, _ = service.claim_next_auto_transfer_batch()
    task_id = batch[0]["id"]
    service.process_transfer_auto_task_batch(batch)
    assert plugin._auto_transfer_tasks[task_id]["next_run_ts"] == clock.now + 60
    assert service.claim_next_auto_transfer_batch() == ([], 60)

    plugin, service, _ = new_queue()
    configure(plugin, service, ("offline", []))
    assert service.claim_next_auto_transfer_batch() == ([], 60)
    clock.now += 60
    batch, _ = service.claim_next_auto_transfer_batch()
    service.process_transfer_auto_task_batch(batch)
    assert service.claim_next_auto_transfer_batch() == ([], 300)

    configure(plugin, service, ("ready", [{
        "IsExternal": False, "Codec": "ass", "Language": "chi", "Title": "中英双语",
    }]))
    clock.now += 300
    batch, _ = service.claim_next_auto_transfer_batch()
    service.process_transfer_auto_task_batch(batch)
    task = plugin._auto_transfer_tasks[task_id]
    assert task["status"] == "skipped"
    assert "符合要求" in task["message"]
    assert task["next_run_ts"] == 0


def test_auto_transfer_queue_stop_service_marks_pending_queue_stopped(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, filename="Movie.mkv")

    service = plugin._auto_transfer_service()
    service.ensure_transfer_auto_worker = lambda: None
    queued, skipped = service.enqueue_transfer_auto_entries([entry])
    assert queued == 1
    assert skipped == 0

    service.stop()
    snapshot = service.auto_transfer_queue_snapshot()

    assert snapshot["summary"]["pending"] == 0
    assert snapshot["summary"]["skipped"] == 1
    assert snapshot["summary"]["active"] == 0
    assert "服务已停止" in snapshot["tasks"][0]["message"]

    queued, skipped = service.enqueue_transfer_auto_entries([make_auto_entry(tmp_path, filename="Other.mkv")])
    assert queued == 0
    assert skipped == 1


def test_listen_transfer_complete_uses_auto_transfer_service_directly(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    plugin._enabled = True
    plugin._auto_search_on_transfer = True
    captured = {}
    event = types.SimpleNamespace(event_data={"path": str(tmp_path / "Movie.mkv")})

    class FakeAutoTransferService:
        def handle_transfer_complete(self, received_event):
            captured["event"] = received_event
            return {"entries": [], "queued": 1, "skipped": 0}

    override_services(plugin, auto_transfer=FakeAutoTransferService())

    plugin.listen_transfer_complete(event)

    assert captured["event"] is event


def test_auto_transfer_service_handles_transfer_complete_event(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, filename="Movie.mkv")
    captured = {}

    class FakeResolver:
        def entries_from_transfer_event(self, event_data):
            captured["event_data"] = event_data
            return [entry]

    class FakeCatalog:
        def merge_local_entries_cache(self, entries):
            captured["merged"] = entries

        def filter_existing_local_entries(self, entries):
            captured["filtered"] = entries
            return entries

    override_services(plugin, target_resolver=FakeResolver(), local_media_catalog=FakeCatalog())
    service = plugin.services.auto_transfer()
    service.ensure_transfer_auto_worker = lambda: captured.setdefault("worker_started", True)
    event = types.SimpleNamespace(event_data={"path": str(tmp_path / "Movie.mkv")})

    result = service.handle_transfer_complete(event)

    assert captured["event_data"] == event.event_data
    assert captured["merged"] == [entry]
    assert captured["filtered"] == [entry]
    assert captured["worker_started"] is True
    assert result["queued"] == 1
    assert result["skipped"] == 0


def test_api_auto_transfer_queue_uses_auto_transfer_service_directly():
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    captured = {}

    class FakeAutoTransferService:
        def auto_transfer_queue_snapshot(self, limit=100):
            captured["limit"] = limit
            return {"summary": {"total": 0}, "tasks": []}

    override_services(plugin, auto_transfer=FakeAutoTransferService())

    queue_endpoint = next(route["endpoint"] for route in plugin.get_api() if route["path"] == "/auto_transfer_queue")
    response = queue_endpoint(FakeRequest(query_params={"limit": "500"}))

    assert response["success"] is True
    assert captured["limit"] == 200
    assert response["data"]["summary"]["total"] == 0


def test_api_auto_transfer_queue_mutations_use_service_and_return_snapshot():
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    captured = {}

    class FakeAutoTransferService:
        def retry_auto_transfer_task(self, task_id, *, force_low_confidence=False):
            captured["retry"] = (task_id, force_low_confidence)
            return True

        def clear_auto_transfer_history(self):
            captured["cleared"] = True
            return 2

        def auto_transfer_queue_snapshot(self, limit=100):
            captured.setdefault("limits", []).append(limit)
            return {"summary": {"total": 1}, "tasks": [{"id": "task-1"}]}

    override_services(plugin, auto_transfer=FakeAutoTransferService())
    retry_endpoint = api_endpoint(plugin, "/auto_transfer_queue/retry")
    clear_endpoint = api_endpoint(plugin, "/auto_transfer_queue/clear_history")

    retry_response = asyncio.run(
        retry_endpoint(FakeRequest({"task_id": "task-1", "force_low_confidence": True}))
    )
    clear_response = asyncio.run(clear_endpoint(FakeRequest({})))

    assert captured["retry"] == ("task-1", True)
    assert captured["cleared"] is True
    assert captured["limits"] == [200, 200]
    assert retry_response["data"]["tasks"][0]["id"] == "task-1"
    assert clear_response["data"]["cleared"] == 2


def test_auto_transfer_group_prefers_season_package_then_single_episode(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    plugin._auto_skip_chinese_media_on_transfer = False
    entries = [
        make_auto_entry(
            tmp_path,
            filename="Show.S01E01.mkv",
            id="e1",
            media_key="tv-key",
            media_type="tv",
            title="Show",
            season=1,
            episode=1,
        ),
        make_auto_entry(
            tmp_path,
            filename="Show.S01E02.mkv",
            id="e2",
            media_key="tv-key",
            media_type="tv",
            title="Show",
            season=1,
            episode=2,
        ),
    ]
    calls = []
    fake_cache = lambda items: {"status": "skipped", "written_by_target": {}}

    def fake_season(items, task_ids=None):
        calls.append(("season", [item["id"] for item in items], list(task_ids or [])))
        return {
            "status": "written",
            "result": "Show S01 pack",
            "written_by_target": {"e1": {"path": "Show.S01E01.chi.srt"}},
            "candidate_count": 1,
            "search_results": 1,
        }

    def fake_single(entry, target, **kwargs):
        calls.append(("single", entry["id"], kwargs.get("queue_rate_limited")))
        return {"status": "written", "target": target.get("label"), "result": "single episode"}

    service = auto_transfer_service_with(
        plugin,
        auto_write_from_season_cache=fake_cache,
        auto_search_write_season_package=fake_season,
        auto_search_write_subtitle=fake_single,
    )

    results = service.auto_process_transfer_group(entries, task_ids=["t1", "t2"])

    assert calls == [("season", ["e1", "e2"], ["t1", "t2"]), ("single", "e2", True)]
    assert results["e1"]["season_package"] is True
    assert results["e2"]["result"] == "single episode"


def test_auto_transfer_group_accepts_season_ai_by_target(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    plugin._auto_skip_chinese_media_on_transfer = False
    entries = [
        make_auto_entry(tmp_path, filename="Show.S01E01.mkv", id="e1", media_key="tv-key", media_type="tv", title="Show", season=1, episode=1),
        make_auto_entry(tmp_path, filename="Show.S01E02.mkv", id="e2", media_key="tv-key", media_type="tv", title="Show", season=1, episode=2),
    ]
    calls = []
    fake_cache = lambda items: {"status": "skipped", "written_by_target": {}}
    fake_season = lambda items, task_ids=None: {
        "status": "written",
        "result": "Show S01 English pack",
        "written_by_target": {},
        "ai_by_target": {
            "e1": {"target_id": "e1", "subtitle_path": "Show.S01E01.fixed.srt"},
            "e2": {"target_id": "e2", "subtitle_path": "Show.S01E02.fixed.srt"},
        },
        "ai_translate": {"added": [{"path": entries[0]["path"]}, {"path": entries[1]["path"]}]},
        "candidate_count": 1,
        "search_results": 1,
    }

    def fake_single(entry, target, **kwargs):
        calls.append(entry["id"])
        return {"status": "written"}

    service = auto_transfer_service_with(
        plugin,
        auto_write_from_season_cache=fake_cache,
        auto_search_write_season_package=fake_season,
        auto_search_write_subtitle=fake_single,
    )

    results = service.auto_process_transfer_group(entries, task_ids=["t1", "t2"])

    assert calls == []
    assert results["e1"]["status"] == "ai_submitted"
    assert results["e2"]["status"] == "ai_submitted"
    assert results["e1"]["fixed_subtitles"][0]["subtitle_path"].endswith("S01E01.fixed.srt")


def test_auto_season_package_tries_next_candidate_when_coverage_incomplete(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    plugin._online_provider_ids = ["opensubtitles"]
    entries = [
        make_auto_entry(tmp_path, filename="Show.S01E01.mkv", id="e1", media_key="tv-key", media_type="tv", title="Show", season=1, episode=1),
        make_auto_entry(tmp_path, filename="Show.S01E02.mkv", id="e2", media_key="tv-key", media_type="tv", title="Show", season=1, episode=2),
    ]

    class FakeSeasonService:
        def __init__(self):
            self.downloaded = []

        def search(self, **kwargs):
            return {
                "results": [
                    {"provider": "opensubtitles", "result_id": "partial", "title": "partial pack", "score": 90, "downloadable": True},
                    {"provider": "opensubtitles", "result_id": "full", "title": "full pack", "score": 89, "downloadable": True},
                ]
            }

        def download(self, selected):
            self.downloaded.append(selected[0]["result_id"])
            return [{"provider": "opensubtitles", "source_name": f"{selected[0]['result_id']}.zip", "content": b"zip", "result": selected[0]}]

    service = FakeSeasonService()
    plugin._online_service = lambda: service
    plugin._apply_tmdb_detail = lambda target, media: None
    fake_extract = lambda source_name, content, session_dir: [
        {"upload_id": source_name, "source_name": source_name, "stored_path": str(tmp_path / source_name), "ext": ".srt"}
    ]
    fake_store_cache = lambda *args, **kwargs: None

    def fake_write(**kwargs):
        selected_title = kwargs["selected_result"]["title"]
        if selected_title == "partial pack":
            return {
                "status": "written",
                "result": selected_title,
                "written_by_target": {"e1": {"path": "Show.S01E01.chi.srt"}},
                "completed_count": 1,
                "missing_target_ids": ["e2"],
                "coverage_complete": False,
            }
        return {
            "status": "written",
            "result": selected_title,
            "written_by_target": {
                "e1": {"path": "Show.S01E01.chi.srt"},
                "e2": {"path": "Show.S01E02.chi.srt"},
            },
            "completed_count": 2,
            "missing_target_ids": [],
            "coverage_complete": True,
        }

    auto_service = auto_transfer_service_with(
        plugin,
        _media_for_transfer_entry=lambda entry: {"media_type": "tv", "title": "Show", "year": "2024"},
        _extract_subtitle_files=fake_extract,
        store_auto_season_package_cache=fake_store_cache,
        auto_write_prepared_uploads_for_entries=fake_write,
    )

    result = auto_service.auto_search_write_season_package(entries)

    assert service.downloaded == ["partial", "full"]
    assert result["result"] == "full pack"
    assert result["coverage_complete"] is True


def test_levius_season_package_requires_all_twelve_episodes(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    plugin._online_provider_ids = ["opensubtitles"]
    entries = [
        make_auto_entry(
            tmp_path,
            filename=f"Levius.S01E{episode:02d}.mkv",
            id=f"e{episode}",
            media_key="levius-tv-key",
            media_type="tv",
            title="Levius",
            season=1,
            episode=episode,
        )
        for episode in range(1, 13)
    ]

    class FakeSeasonService:
        def __init__(self):
            self.downloaded = []

        def search(self, **kwargs):
            return {
                "results": [
                    {"provider": "opensubtitles", "result_id": "levius-11", "title": "Levius pack 11 episodes", "score": 96, "downloadable": True},
                    {"provider": "opensubtitles", "result_id": "levius-12", "title": "Levius pack 12 episodes", "score": 95, "downloadable": True},
                ]
            }

        def download(self, selected):
            self.downloaded.append(selected[0]["result_id"])
            return [{"provider": "opensubtitles", "source_name": f"{selected[0]['result_id']}.zip", "content": b"zip", "result": selected[0]}]

    service = FakeSeasonService()
    plugin._online_service = lambda: service
    plugin._apply_tmdb_detail = lambda target, media: None
    fake_extract = lambda source_name, content, session_dir: [
        {"upload_id": source_name, "source_name": source_name, "stored_path": str(tmp_path / source_name), "ext": ".srt"}
    ]
    fake_store_cache = lambda *args, **kwargs: None

    def fake_write(**kwargs):
        selected_title = kwargs["selected_result"]["title"]
        if "11 episodes" in selected_title:
            return {
                "status": "written",
                "result": selected_title,
                "written_by_target": {f"e{episode}": {"path": f"Levius.S01E{episode:02d}.chi.srt"} for episode in range(1, 12)},
                "completed_count": 11,
                "missing_target_ids": ["e12"],
                "coverage_complete": False,
            }
        return {
            "status": "written",
            "result": selected_title,
            "written_by_target": {
                f"e{episode}": {"path": f"Levius.S01E{episode:02d}.chi.srt"} for episode in range(1, 13)
            },
            "completed_count": 12,
            "missing_target_ids": [],
            "coverage_complete": True,
        }

    auto_service = auto_transfer_service_with(
        plugin,
        _media_for_transfer_entry=lambda entry: {"media_type": "tv", "title": "Levius", "year": "2019"},
        _extract_subtitle_files=fake_extract,
        store_auto_season_package_cache=fake_store_cache,
        auto_write_prepared_uploads_for_entries=fake_write,
    )

    result = auto_service.auto_search_write_season_package(entries)

    assert service.downloaded == ["levius-11", "levius-12"]
    assert result["result"] == "Levius pack 12 episodes"
    assert result["completed_count"] == 12
    assert result["coverage_complete"] is True
    assert set(result["written_by_target"]) == {f"e{episode}" for episode in range(1, 13)}


def test_auto_season_foreign_srt_submits_ai_as_subtitle_fallback(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entries = [
        make_auto_entry(tmp_path, filename="Show.S01E01.mkv", id="e1", media_key="tv-key", media_type="tv", title="Show", season=1, episode=1),
        make_auto_entry(tmp_path, filename="Show.S01E02.mkv", id="e2", media_key="tv-key", media_type="tv", title="Show", season=1, episode=2),
    ]
    prepared_uploads = []
    for entry in entries:
        subtitle = tmp_path / f"{entry['basename']}.eng.srt"
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
        prepared_uploads.append(
            {
                "upload_id": entry["id"],
                "source_name": subtitle.name,
                "stored_path": str(subtitle),
                "ext": ".srt",
                "target_id": entry["id"],
            }
        )

    captured = {}
    fake_overrides = lambda **kwargs: (
        {entry["path"]: {"subtitle_path": str(tmp_path / f"{entry['basename']}.fixed.srt"), "lang": "en"} for entry in kwargs["target_entries"]},
        [{"target_id": entry["id"], "subtitle_path": str(tmp_path / f"{entry['basename']}.fixed.srt"), "autosub_lang": "en"} for entry in kwargs["target_entries"]],
    )

    def fake_submit(entries_arg, subtitle_overrides=None, **kwargs):
        captured["entries"] = entries_arg
        captured["overrides"] = subtitle_overrides
        captured["submit_kwargs"] = kwargs
        return {
            "added": [{"path": entry["path"]} for entry in entries_arg],
            "skipped": [],
            "failed": [],
        }

    service = auto_transfer_service_with(
        plugin,
        prepare_online_ai_subtitle_overrides=fake_overrides,
        submit_autosub_for_entries=fake_submit,
        _write_operations_to_disk=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("Foreign season package should be submitted to AI instead of written")
        ),
    )

    result = service.auto_write_prepared_uploads_for_entries(
        target_entries=entries,
        prepared_uploads=prepared_uploads,
        session_dir=tmp_path,
        selected_result={"provider": "opensubtitles", "title": "Show English pack"},
    )

    assert result["status"] == "written"
    assert result["ai_count"] == 2
    assert result["coverage_complete"] is True
    assert [entry["id"] for entry in captured["entries"]] == ["e1", "e2"]
    assert set(captured["overrides"]) == {entry["path"] for entry in entries}
    assert captured["submit_kwargs"]["trigger"] == "subtitle_fallback"
    assert captured["submit_kwargs"]["source_policy"] == "matched_external"
    assert captured["submit_kwargs"]["overwrite_policy"] == "new_variant"


def test_auto_transfer_rate_limit_is_tracked_per_provider(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    plugin._online_provider_ids = ["assrt", "opensubtitles"]
    plugin._auto_transfer_tasks["t1"] = {
        "id": "t1",
        "status": "in_progress",
        "active": True,
        "next_run_ts": 0,
        "message": "",
    }
    now = {"value": 1000.0}
    observed = {}
    original_time = module.time.time
    original_sleep = module.time.sleep
    module.time.time = lambda: now["value"]

    def fake_sleep(seconds):
        observed["next_run_ts"] = plugin._auto_transfer_tasks["t1"]["next_run_ts"]
        observed["message"] = plugin._auto_transfer_tasks["t1"]["message"]
        now["value"] += seconds

    module.time.sleep = fake_sleep
    try:
        plugin._online_rate_records = {"assrt": [941.0, 950.0, 960.0, 970.0, 980.0]}
        plugin._auto_wait_online_rate_limit(["assrt"], task_ids=["t1"])
    finally:
        module.time.time = original_time
        module.time.sleep = original_sleep

    assert observed["next_run_ts"] == 1001.0
    assert "assrt" in observed["message"]
    assert len(plugin._online_rate_records["assrt"]) == 5
    assert plugin._auto_transfer_tasks["t1"]["next_run_ts"] == 0


def test_auto_transfer_rate_limit_rejects_malformed_provider_state(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    plugin._online_rate_records = {"assrt": "invalid"}

    with pytest.raises(ValueError, match="assrt"):
        plugin._auto_wait_online_rate_limit(["assrt"])


def test_auto_transfer_skips_chinese_media_by_tmdb_language(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, title="流浪地球")
    service = auto_transfer_service_with(
        plugin,
        _chinese_transfer_media_evidence=lambda entry: (True, "TMDB original_language=zh"),
        auto_search_write_subtitle=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Chinese media should skip search")
        ),
        auto_submit_ai_for_entry=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Chinese media should skip AI")
        ),
    )

    result = service.auto_process_transfer_entry(entry)

    assert result["status"] == "skipped"
    assert "中文资源自动跳过" in result["reason"]


def test_auto_transfer_skips_before_search_when_video_has_embedded_chinese_subtitle(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, title="Anime", media_type="tv", season=4, episode=14)
    plugin._auto_skip_chinese_media_on_transfer = False
    original_run = module.subprocess.run
    module.subprocess.run = lambda *args, **kwargs: types.SimpleNamespace(
        stdout='{"streams":[{"index":2,"codec_name":"subrip","tags":{"language":"chi","title":"Chinese Simplified"}}]}'
    )
    service = auto_transfer_service_with(
        plugin,
        auto_search_write_subtitle=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Embedded Chinese subtitle should skip online search")
        ),
        auto_submit_ai_for_entry=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Embedded Chinese subtitle should skip AI")
        ),
    )

    try:
        result = service.auto_process_transfer_entry(entry)
    finally:
        module.subprocess.run = original_run

    assert result["status"] == "skipped"
    assert result["reason"] == "目标已有符合语言要求的字幕"


def test_auto_transfer_ai_source_only_skips_embedded_chinese_before_ai(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, title="Anime", media_type="tv", season=4, episode=14)
    plugin._auto_skip_chinese_media_on_transfer = False
    plugin._auto_transfer_subtitle_strategy = "ai_source_only"
    original_run = module.subprocess.run
    module.subprocess.run = lambda *args, **kwargs: types.SimpleNamespace(
        stdout='{"streams":[{"index":2,"codec_name":"subrip","tags":{"language":"zho"}}]}'
    )
    service = auto_transfer_service_with(
        plugin,
        auto_submit_ai_for_entry=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Embedded Chinese subtitle should skip ai_source_only")
        ),
    )

    try:
        result = service.auto_process_transfer_entry(entry)
    finally:
        module.subprocess.run = original_run

    assert result["status"] == "skipped"
    assert result["reason"] == "目标已有符合语言要求的字幕"


def test_auto_transfer_samples_unknown_embedded_text_subtitle_content(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, title="Anime", media_type="tv", season=4, episode=14)
    plugin._auto_skip_chinese_media_on_transfer = False
    original_run = module.subprocess.run

    def fake_run(args, *run_args, **run_kwargs):
        if args[0] == "ffprobe":
            return types.SimpleNamespace(
                stdout='{"streams":[{"index":2,"codec_name":"subrip","tags":{"language":"und"}}]}'
            )
        return types.SimpleNamespace(
            stdout=("1\n00:00:01,000 --> 00:00:02,000\n这是一段中文字幕内容，用来确认未知语言内嵌字幕不会漏判。\n" * 25).encode()
        )

    module.subprocess.run = fake_run
    service = auto_transfer_service_with(
        plugin,
        auto_search_write_subtitle=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Sampled Chinese subtitle should skip online search")
        ),
        auto_submit_ai_for_entry=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Sampled Chinese subtitle should skip AI")
        ),
    )

    try:
        result = service.auto_process_transfer_entry(entry)
    finally:
        module.subprocess.run = original_run

    assert result["status"] == "skipped"
    assert result["reason"] == "目标已有符合语言要求的字幕"


def test_auto_transfer_embedded_english_subtitle_still_searches(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, title="Anime", media_type="tv", season=4, episode=14)
    plugin._auto_skip_chinese_media_on_transfer = False
    original_run = module.subprocess.run
    module.subprocess.run = lambda *args, **kwargs: types.SimpleNamespace(
        stdout='{"streams":[{"index":4,"codec_name":"ass","tags":{"language":"eng","title":"English subtitles for Chinese dialogue"}}]}'
    )
    service = auto_transfer_service_with(
        plugin,
        auto_search_write_subtitle=lambda item, target, **kwargs: {
            "status": "written",
            "target": target.get("label"),
            "result": "online chinese subtitle",
        },
        auto_submit_ai_for_entry=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Written online subtitle should not trigger AI")
        ),
    )

    try:
        result = service.auto_process_transfer_entry(entry)
    finally:
        module.subprocess.run = original_run

    assert result["status"] == "written"
    assert result["result"] == "online chinese subtitle"


def test_auto_transfer_embedded_regional_chinese_language_tag_skips(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, title="Anime", media_type="tv", season=4, episode=14)
    plugin._auto_skip_chinese_media_on_transfer = False
    original_run = module.subprocess.run
    module.subprocess.run = lambda *args, **kwargs: types.SimpleNamespace(
        stdout='{"streams":[{"index":2,"codec_name":"ass","tags":{"language":"zh-Hans-CN"}}]}'
    )
    service = auto_transfer_service_with(
        plugin,
        auto_search_write_subtitle=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Regional Chinese language tag should skip online search")
        ),
    )

    try:
        result = service.auto_process_transfer_entry(entry)
    finally:
        module.subprocess.run = original_run

    assert result["status"] == "skipped"
    assert result["reason"] == "目标已有符合语言要求的字幕"


def test_auto_transfer_embedded_chinese_pgs_does_not_skip_online_search(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, title="Anime", media_type="tv", season=4, episode=14)
    plugin._auto_skip_chinese_media_on_transfer = False
    original_run = module.subprocess.run
    module.subprocess.run = lambda *args, **kwargs: types.SimpleNamespace(
        stdout='{"streams":[{"index":5,"codec_name":"hdmv_pgs_subtitle","tags":{"language":"chi","title":"Chinese"}}]}'
    )
    service = auto_transfer_service_with(
        plugin,
        auto_search_write_subtitle=lambda item, target, **kwargs: {
            "status": "written",
            "target": target.get("label"),
            "result": "online chinese subtitle",
        },
    )

    try:
        result = service.auto_process_transfer_entry(entry)
    finally:
        module.subprocess.run = original_run

    assert result["status"] == "written"
    assert result["result"] == "online chinese subtitle"


def test_auto_transfer_embedded_chinese_signs_does_not_skip_online_search(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, title="Anime", media_type="tv", season=4, episode=14)
    plugin._auto_skip_chinese_media_on_transfer = False
    original_run = module.subprocess.run
    module.subprocess.run = lambda *args, **kwargs: types.SimpleNamespace(
        stdout='{"streams":[{"index":2,"codec_name":"ass","tags":{"language":"chi","title":"Chinese Signs/Songs"}}]}'
    )
    service = auto_transfer_service_with(
        plugin,
        auto_search_write_subtitle=lambda item, target, **kwargs: {
            "status": "written",
            "target": target.get("label"),
            "result": "online chinese subtitle",
        },
    )

    try:
        result = service.auto_process_transfer_entry(entry)
    finally:
        module.subprocess.run = original_run

    assert result["status"] == "written"
    assert result["result"] == "online chinese subtitle"


def test_auto_transfer_group_skips_embedded_chinese_target_before_season_search(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    plugin._auto_skip_chinese_media_on_transfer = False
    entries = [
        make_auto_entry(tmp_path, filename="Show.S01E01.mkv", id="e1", media_key="tv-key", media_type="tv", title="Show", season=1, episode=1),
        make_auto_entry(tmp_path, filename="Show.S01E02.mkv", id="e2", media_key="tv-key", media_type="tv", title="Show", season=1, episode=2),
    ]
    original_run = module.subprocess.run

    def fake_run(args, *run_args, **run_kwargs):
        video_path = str(args[-1])
        if video_path.endswith("Show.S01E01.mkv"):
            return types.SimpleNamespace(
                stdout='{"streams":[{"index":2,"codec_name":"subrip","tags":{"language":"zho","title":"Chinese Simplified"}}]}'
            )
        return types.SimpleNamespace(stdout='{"streams":[]}')

    module.subprocess.run = fake_run
    cache_ids = []
    season_ids = []

    def fake_cache(items):
        cache_ids.extend(item["id"] for item in items)
        return {"status": "skipped", "written_by_target": {}}

    def fake_season(items, task_ids=None):
        season_ids.extend(item["id"] for item in items)
        return {
            "status": "skipped",
            "written_by_target": {},
            "ai_by_target": {},
        }

    searched = []
    service = auto_transfer_service_with(
        plugin,
        auto_write_from_season_cache=fake_cache,
        auto_search_write_season_package=fake_season,
        auto_search_write_subtitle=lambda entry, target, **kwargs: searched.append(entry["id"]) or {
            "status": "skipped",
            "target": target.get("label"),
        },
    )

    try:
        results = service.auto_process_transfer_group(entries, task_ids=["t1", "t2"])
    finally:
        module.subprocess.run = original_run

    assert results["e1"]["status"] == "skipped"
    assert results["e1"]["reason"] == "目标已有符合语言要求的字幕"
    assert cache_ids == ["e2"]
    assert season_ids == ["e2"]
    assert searched == ["e2"]


def test_auto_transfer_chinese_title_is_not_skip_evidence_without_tmdb_match(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, title="灰原同学的第二轮青春游戏", media_type="tv", season=1, episode=7)
    service = auto_transfer_service_with(
        plugin,
        _chinese_transfer_media_evidence=lambda entry: (False, "no Chinese TMDB evidence"),
        auto_search_write_subtitle=lambda item, target, **kwargs: {
            "status": "written",
            "target": target.get("label"),
            "result": "Haigakura S01E07",
        },
        auto_submit_ai_for_entry=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Written result should not trigger fallback AI")
        ),
    )

    result = service.auto_process_transfer_entry(entry)

    assert result["status"] == "written"
    assert result["result"] == "Haigakura S01E07"


def test_auto_transfer_online_then_ai_source_falls_back_to_ai_when_search_is_uncertain(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path)
    plugin._auto_skip_chinese_media_on_transfer = False
    service = auto_transfer_service_with(
        plugin,
        auto_search_write_subtitle=lambda item, target, **kwargs: {
            "status": "skipped",
            "target": target.get("label"),
            "reason": "高置信可下载结果数量为 0",
            "search_results": 0,
        },
        auto_submit_ai_for_entry=lambda item, target, reason: {
            "status": "ai_submitted",
            "target": target.get("label"),
            "reason": reason,
        },
    )

    result = service.auto_process_transfer_entry(entry)

    assert result["status"] == "ai_submitted"
    assert result["search"]["search_results"] == 0
    assert result["strategy"] == "online_then_ai_source"


def test_auto_transfer_online_source_only_never_submits_ai_source_fallback(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path)
    plugin._auto_skip_chinese_media_on_transfer = False
    plugin._auto_transfer_subtitle_strategy = "online_source_only"
    service = auto_transfer_service_with(
        plugin,
        auto_search_write_subtitle=lambda item, target, **kwargs: {
            "status": "skipped",
            "target": target.get("label"),
            "reason": "高置信可下载结果数量为 0",
        },
        auto_submit_ai_for_entry=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("search_only should not submit AI")
        ),
    )

    result = service.auto_process_transfer_entry(entry)

    assert result["status"] == "skipped"
    assert result["strategy"] == "online_source_only"


def test_auto_transfer_ai_source_only_never_searches(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path)
    plugin._auto_skip_chinese_media_on_transfer = False
    plugin._auto_transfer_subtitle_strategy = "ai_source_only"
    service = auto_transfer_service_with(
        plugin,
        auto_search_write_subtitle=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("ai_only should not search")
        ),
        auto_submit_ai_for_entry=lambda item, target, reason: {
            "status": "ai_submitted",
            "target": target.get("label"),
            "reason": reason,
        },
    )

    result = service.auto_process_transfer_entry(entry)

    assert result["status"] == "ai_submitted"
    assert result["strategy"] == "ai_source_only"


def test_auto_transfer_legacy_ai_first_maps_to_ai_source_only(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path)
    plugin._auto_skip_chinese_media_on_transfer = False
    plugin._auto_transfer_subtitle_strategy = "ai_first"
    service = auto_transfer_service_with(
        plugin,
        auto_search_write_subtitle=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("ai_first legacy value should migrate to ai_source_only and never search")
        ),
        auto_submit_ai_for_entry=lambda item, target, reason: {
            "status": "ai_submitted",
            "target": target.get("label"),
            "reason": reason,
        },
    )

    result = service.auto_process_transfer_entry(entry)

    assert result["status"] == "ai_submitted"
    assert result["strategy"] == "ai_source_only"


def test_manual_strm_auto_processing_forces_online_only_and_never_submits_ai(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    plugin._auto_skip_chinese_media_on_transfer = False
    plugin._auto_transfer_subtitle_strategy = "ai_source_only"
    entry = make_auto_entry(tmp_path, filename="Movie.strm", origin="manual_strm")
    calls = []
    service = auto_transfer_service_with(
        plugin,
        auto_search_write_subtitle=lambda item, target, **kwargs: calls.append("online") or {
            "status": "skipped",
            "reason": "没有高置信在线字幕",
            "target": target.get("label"),
        },
        auto_submit_ai_for_entry=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("STRM must not submit AI audio generation")
        ),
    )

    result = service.auto_process_transfer_entry(entry)

    assert calls == ["online"]
    assert result["strategy"] == "online_source_only"
    assert result["status"] == "skipped"


def test_auto_transfer_legacy_strategy_aliases_are_migrated():
    module, _, _ = load_plugin_module()
    config_schema = plugin_submodule(module, "config.config_schema")
    aliases = {
        "search_first": "online_then_ai_source",
        "search_only": "online_source_only",
        "ai_only": "ai_source_only",
        "ai_first": "ai_source_only",
    }

    for legacy, expected in aliases.items():
        assert config_schema.normalize_auto_transfer_subtitle_strategy(legacy) == expected


def test_auto_subtitle_preference_config_normalizes_legacy_strings(tmp_path):
    module, _, _ = load_plugin_module()
    config_schema = plugin_submodule(module, "config.config_schema")
    plugin = make_plugin(module)
    saved_configs = []
    plugin.update_config = lambda config: saved_configs.append(config)
    raw_config = {
        "auto_multi_subtitle_mode": "chinese",
        "auto_subtitle_language_priority": "英文,双语,坏值",
        "auto_subtitle_format_priority": "srt,ass,xxx",
        "auto_ass_to_srt_for_ai": False,
    }

    normalized = config_schema.normalize_plugin_config(raw_config, subtitle_exts=module.SubtitleManualUpload._subtitle_exts)
    assert normalized["auto_multi_subtitle_mode"] == "chinese_all"
    assert normalized["auto_subtitle_language_priority"][:5] == ["eng", "chi&eng", "chi&jp", "chi&kr", "other_bilingual"]
    assert normalized["auto_subtitle_format_priority"][:4] == [".srt", ".ass", ".ssa", ".vtt"]
    assert ".sbv" in normalized["auto_subtitle_format_priority"]
    assert normalized["auto_ass_to_srt_for_ai"] is False

    old_transfer_lock = plugin._transfer_auto_lock
    plugin._entry_map["stale"] = {"id": "stale"}
    plugin._media_index_cache["stale"] = {"items": []}
    plugin._match_history_cache = {"loaded_at": "stale", "signature": "stale", "items": [{"id": "stale"}], "entry_count": 1, "persisted": True}
    plugin._timeline_tasks["stale"] = {"status": "running"}
    plugin._transfer_auto_recent = {"stale": 1.0}
    plugin._auto_transfer_tasks["stale"] = {"status": "pending"}
    plugin._auto_transfer_worker = object()
    plugin._auto_transfer_stopping = True
    plugin._auto_season_package_cache["stale"] = {"items": []}
    plugin._cache_refreshing = True
    plugin._cache_refresh_started_at = "stale"
    plugin._cache_refresh_completed_at = "stale"
    plugin._cache_refresh_error = "stale"
    plugin._local_entries_cache = {"loaded_at": "stale", "entries": [{"id": "stale"}], "media_count": 1, "persisted": True}

    plugin.init_plugin(raw_config)

    assert plugin._auto_multi_subtitle_mode == "chinese_all"
    assert plugin._auto_subtitle_language_priority[:5] == ["eng", "chi&eng", "chi&jp", "chi&kr", "other_bilingual"]
    assert plugin._auto_subtitle_format_priority[:4] == [".srt", ".ass", ".ssa", ".vtt"]
    assert ".sbv" in plugin._auto_subtitle_format_priority
    assert plugin._auto_ass_to_srt_for_ai is False
    assert module.SubtitleManualUpload._auto_multi_subtitle_mode == plugin._auto_multi_subtitle_mode
    assert module.SubtitleManualUpload._auto_subtitle_language_priority == plugin._auto_subtitle_language_priority
    assert module.SubtitleManualUpload._auto_subtitle_format_priority == plugin._auto_subtitle_format_priority
    assert module.SubtitleManualUpload._auto_ass_to_srt_for_ai == plugin._auto_ass_to_srt_for_ai
    assert plugin._entry_map == {}
    assert plugin._media_index_cache == {}
    assert plugin._match_history_cache == {
        "version": 3,
        "loaded_at": None,
        "validated_at": None,
        "signature": "",
        "items": [],
        "entry_count": 0,
        "persisted": False,
    }
    assert plugin._timeline_tasks == {}
    assert plugin._transfer_auto_recent == {}
    assert plugin._transfer_auto_lock is not old_transfer_lock
    assert plugin._auto_transfer_tasks == {}
    assert plugin._auto_transfer_worker is None
    assert plugin._auto_transfer_stopping is False
    assert plugin._auto_season_package_cache == {}
    assert plugin._cache_refreshing is False
    assert plugin._cache_refresh_started_at == ""
    assert plugin._cache_refresh_completed_at == ""
    assert plugin._cache_refresh_error == ""
    assert plugin._local_entries_cache == {"loaded_at": None, "entries": [], "media_count": 0, "persisted": False}
    assert saved_configs
    saved_config = saved_configs[-1]
    assert saved_config["auto_multi_subtitle_mode"] == "chinese_all"
    assert saved_config["auto_subtitle_language_priority"] == plugin._auto_subtitle_language_priority
    assert saved_config["auto_subtitle_format_priority"] == plugin._auto_subtitle_format_priority
    assert saved_config["auto_ass_to_srt_for_ai"] is False
    assert saved_config["online_proxy_migrated"] is True
    assert saved_config["assrt_provider_migrated"] is True
    assert saved_config["subhd_url"] == plugin._online_site_urls["subhd"]
    assert saved_config["opensubtitles_url"] == plugin._online_site_urls["opensubtitles"]


def test_strict_language_requires_selection():
    module, _, _ = load_plugin_module()
    config_schema = plugin_submodule(module, "config.config_schema")

    with pytest.raises(ValueError, match="至少选择一种语言"):
        config_schema.normalize_plugin_config({"auto_subtitle_language_strict": True, "auto_subtitle_language_priority": []})
    normalized = config_schema.normalize_plugin_config({"auto_subtitle_language_priority": ["bilingual"]})
    assert normalized["auto_subtitle_language_priority"][:4] == ["chi&eng", "chi&jp", "chi&kr", "other_bilingual"]


def test_config_schema_default_config_covers_config_vue_bound_fields():
    module, _, _ = load_plugin_module()
    config_schema = plugin_submodule(module, "config.config_schema")
    plugin = module.SubtitleManualUpload.__new__(module.SubtitleManualUpload)
    _, default_config = plugin.get_form()
    vue_text = (Path(module.__file__).parent / "src" / "components" / "Config.vue").read_text(encoding="utf-8")
    bound_fields = sorted(set(re.findall(r"localConfig\.(?!value\b)([A-Za-z0-9_]+)", vue_text)))

    assert bound_fields
    assert not [field for field in bound_fields if field not in config_schema.DEFAULT_CONFIG]
    assert not [field for field in bound_fields if field not in default_config]
    for field in bound_fields:
        assert default_config[field] == config_schema.DEFAULT_CONFIG[field]

    assert "online_providers: [...defaultOnlineProviderIds]" in vue_text
    assert "auto_subtitle_language_priority: ['chi&eng', 'chi&jp', 'chi&kr', 'other_bilingual', 'chi', 'cht', 'eng']" in vue_text
    assert "auto_subtitle_format_priority: ['.ass', '.srt', '.ssa', '.vtt']" in vue_text
    assert "timeline_max_offset_seconds: 120" in vue_text
    assert "rar_dependency_mode: 'none'" in vue_text
    assert "opensubtitles_api_url: 'https://api.opensubtitles.com/api/v1'" in vue_text


def test_auto_transfer_existing_chinese_subtitle_skips_all_strategies(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, filename="Movie.mkv")
    (tmp_path / "Movie.chi.srt").write_text("subtitle", encoding="utf-8")
    service = auto_transfer_service_with(
        plugin,
        auto_search_write_subtitle=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("existing subtitle should skip search")
        ),
        auto_submit_ai_for_entry=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("existing subtitle should skip AI")
        ),
    )

    result = service.auto_process_transfer_entry(entry)

    assert result["status"] == "skipped"
    assert result["reason"] == "目标已有符合语言要求的字幕"


def test_auto_transfer_existing_non_chinese_subtitle_still_searches(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, filename="Movie.mkv")
    (tmp_path / "Movie.eng.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    plugin._auto_skip_chinese_media_on_transfer = False
    original_run = module.subprocess.run
    module.subprocess.run = lambda *args, **kwargs: types.SimpleNamespace(stdout='{"streams":[]}')
    service = auto_transfer_service_with(
        plugin,
        auto_search_write_subtitle=lambda item, target, **kwargs: {
            "status": "written",
            "target": target.get("label"),
            "result": "online chinese subtitle",
        },
        auto_submit_ai_for_entry=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Written online subtitle should not trigger AI")
        ),
    )

    try:
        result = service.auto_process_transfer_entry(entry)
    finally:
        module.subprocess.run = original_run

    assert result["status"] == "written"
    assert result["result"] == "online chinese subtitle"


def test_auto_search_write_uses_best_api_candidate_when_multiple_results(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    plugin._online_provider_ids = ["assrt", "opensubtitles"]
    entry = make_auto_entry(tmp_path, filename="Movie.mkv")
    subtitle = tmp_path / "Movie.chi.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")

    class FakeAutoService:
        def __init__(self):
            self.downloaded = []

        def search(self, **kwargs):
            return {
                "results": [
                    {
                        "provider": "opensubtitles",
                        "result_id": "os-1",
                        "title": "Movie 2024 Chinese",
                        "score": 70,
                        "downloadable": True,
                        "language_category": "chinese",
                    },
                    {
                        "provider": "assrt",
                        "result_id": "assrt-1",
                        "title": "Movie 2024 English",
                        "score": 65,
                        "downloadable": True,
                        "language_category": "english",
                    },
                ]
            }

        def download(self, selected):
            self.downloaded = selected
            return [
                {
                    "provider": selected[0]["provider"],
                    "source_name": "Movie.chi.srt",
                    "content": subtitle.read_bytes(),
                    "result": selected[0],
                }
            ]

    online_service = FakeAutoService()
    plugin._online_service = lambda: online_service
    fake_keywords = lambda item, target: ["Movie 2024"]
    fake_extract = lambda source_name, content, session_dir: [
        {
            "upload_id": "u1",
            "source_name": source_name,
            "stored_path": str(subtitle),
            "ext": ".srt",
        }
    ]
    service = auto_transfer_service_with(
        plugin,
        auto_search_keywords_for_entry=fake_keywords,
        _extract_subtitle_files=fake_extract,
        _write_operations_to_disk=lambda **kwargs: ([{"output_name": "Movie.chi.srt"}], 0, 0),
        auto_submit_ai_for_entry=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Chinese subtitle should not trigger AI")
        ),
    )

    result = service.auto_search_write_subtitle(entry)

    assert result["status"] == "written"
    assert result["candidate_count"] == 2
    assert online_service.downloaded[0]["result_id"] == "os-1"


def test_auto_search_write_falls_back_when_best_download_has_no_subtitle(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    plugin._online_provider_ids = ["assrt", "opensubtitles"]
    entry = make_auto_entry(tmp_path, filename="Movie.mkv")
    subtitle = tmp_path / "Movie.chi.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")

    class FakeAutoService:
        def __init__(self):
            self.downloaded_ids = []

        def search(self, **kwargs):
            return {
                "results": [
                    {
                        "provider": "opensubtitles",
                        "result_id": "os-link",
                        "title": "Movie Netdisk Link",
                        "score": 95,
                        "downloadable": True,
                        "language_category": "chinese",
                    },
                    {
                        "provider": "assrt",
                        "result_id": "assrt-sub",
                        "title": "Movie Chinese Subtitle",
                        "score": 90,
                        "downloadable": True,
                        "language_category": "chinese",
                    },
                ]
            }

        def download(self, selected):
            result = selected[0]
            self.downloaded_ids.append(result["result_id"])
            if result["result_id"] == "os-link":
                return [
                    {
                        "provider": result["provider"],
                        "source_name": "download-url.txt",
                        "content": b"https://example.invalid/pan-link",
                        "result": result,
                    }
                ]
            return [
                {
                    "provider": result["provider"],
                    "source_name": "Movie.chi.srt",
                    "content": subtitle.read_bytes(),
                    "result": result,
                }
            ]

    online_service = FakeAutoService()
    plugin._online_service = lambda: online_service
    fake_keywords = lambda item, target: ["Movie 2024"]

    def fake_extract(source_name, content, session_dir):
        if source_name.endswith(".txt"):
            return []
        return [
            {
                "upload_id": "u1",
                "source_name": source_name,
                "stored_path": str(subtitle),
                "ext": ".srt",
            }
        ]

    service = auto_transfer_service_with(
        plugin,
        auto_search_keywords_for_entry=fake_keywords,
        _extract_subtitle_files=fake_extract,
        _write_operations_to_disk=lambda **kwargs: ([{"output_name": "Movie.chi.srt"}], 0, 0),
    )

    result = service.auto_search_write_subtitle(entry)

    assert result["status"] == "written"
    assert result["result"] == "Movie Chinese Subtitle"
    assert online_service.downloaded_ids == ["os-link", "assrt-sub"]


def test_auto_search_write_chinese_subtitle_enables_timeline_fix(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, filename="Movie.mkv")
    subtitle = tmp_path / "Movie.chi.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")

    class FakeAutoService:
        def search(self, **kwargs):
            return {
                "results": [
                    {
                        "provider": "opensubtitles",
                        "result_id": "os-1",
                        "title": "Movie Chinese",
                        "score": 90,
                        "downloadable": True,
                        "language_category": "chinese",
                    }
                ]
            }

        def download(self, selected):
            return [
                {
                    "provider": selected[0]["provider"],
                    "source_name": "Movie.chi.srt",
                    "content": subtitle.read_bytes(),
                    "result": selected[0],
                }
            ]

    observed = {}
    plugin._online_service = lambda: FakeAutoService()
    fake_keywords = lambda item, target: ["Movie 2024"]
    fake_extract = lambda source_name, content, session_dir: [
        {
            "upload_id": "u1",
            "source_name": source_name,
            "stored_path": str(subtitle),
            "ext": ".srt",
        }
    ]
    def fake_write(**kwargs):
        observed["fix_timeline"] = kwargs.get("fix_timeline")
        return [{"output_name": "Movie.chi.srt"}], 1, 0

    service = auto_transfer_service_with(
        plugin,
        auto_search_keywords_for_entry=fake_keywords,
        _extract_subtitle_files=fake_extract,
        _write_operations_to_disk=fake_write,
        submit_autosub_for_entries=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Chinese subtitle should not trigger AI")
        ),
    )

    result = service.auto_search_write_subtitle(entry)

    assert result["status"] == "written"
    assert observed["fix_timeline"] is True


def test_auto_write_passes_force_low_confidence_only_from_target_entry(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, filename="Movie.mkv", _force_timeline_write=True)
    subtitle = tmp_path / "Movie.chi.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")
    observed = {}

    def fake_write(**kwargs):
        observed.update(kwargs)
        return [{"output_name": "Movie.chi.srt"}], 0, 0

    service = auto_transfer_service_with(plugin, _write_operations_to_disk=fake_write)
    result = service.auto_write_prepared_uploads_for_entries(
        target_entries=[entry],
        prepared_uploads=[
            {"upload_id": "u1", "source_name": subtitle.name, "stored_path": str(subtitle), "ext": ".srt"}
        ],
        session_dir=tmp_path,
        selected_result={"provider": "opensubtitles", "title": "Movie Chinese"},
    )

    assert result["status"] == "written"
    assert observed["fix_timeline"] is True
    assert observed["force_low_confidence"] is True


def test_auto_search_write_foreign_srt_submits_ai_without_writing(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, filename="Movie.mkv")
    subtitle = tmp_path / "Movie.eng.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")

    class FakeAutoService:
        def search(self, **kwargs):
            return {
                "results": [
                    {
                        "provider": "opensubtitles",
                        "result_id": "os-1",
                        "title": "Movie English",
                        "score": 90,
                        "downloadable": True,
                        "language_category": "english",
                    }
                ]
            }

        def download(self, selected):
            return [
                {
                    "provider": selected[0]["provider"],
                    "source_name": "Movie.eng.srt",
                    "content": subtitle.read_bytes(),
                    "result": selected[0],
                }
            ]

    captured = {}
    plugin._online_service = lambda: FakeAutoService()
    fake_keywords = lambda item, target: ["Movie 2024"]
    fake_extract = lambda source_name, content, session_dir: [
        {
            "upload_id": "u1",
            "source_name": source_name,
            "stored_path": str(subtitle),
            "ext": ".srt",
        }
    ]
    fake_write = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("Foreign SRT should be submitted to AI instead of written")
    )
    fake_overrides = lambda **kwargs: (
        {entry["path"]: {"subtitle_path": str(subtitle), "lang": "en"}},
        [{"target_id": entry["id"], "subtitle_path": str(subtitle), "autosub_lang": "en"}],
    )

    def fake_submit(entries, subtitle_overrides=None, **kwargs):
        captured["overrides"] = subtitle_overrides
        captured["submit_kwargs"] = kwargs
        return {
            "added": [{"path": entries[0]["path"]}],
            "skipped": [],
            "failed": [],
            "tasks": {"task_by_target": {"e1": {"status": "pending"}}},
        }

    service = auto_transfer_service_with(
        plugin,
        auto_search_keywords_for_entry=fake_keywords,
        _extract_subtitle_files=fake_extract,
        _write_operations_to_disk=fake_write,
        prepare_online_ai_subtitle_overrides=fake_overrides,
        submit_autosub_for_entries=fake_submit,
    )

    result = service.auto_search_write_subtitle(entry)

    assert result["status"] == "ai_submitted"
    assert captured["overrides"][entry["path"]]["lang"] == "en"
    assert captured["submit_kwargs"]["trigger"] == "subtitle_fallback"
    assert captured["submit_kwargs"]["source_policy"] == "matched_external"
    assert captured["submit_kwargs"]["overwrite_policy"] == "new_variant"
    assert result["fixed_subtitles"][0]["autosub_lang"] == "en"


def test_auto_search_write_prefers_chinese_ass_from_multi_subtitle_package(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, filename="Movie.mkv")
    files = {}
    for name, text in {
        "Movie.chi.srt": "1\n00:00:01,000 --> 00:00:02,000\n你好\n",
        "Movie.chi.ass": "[Script Info]\n[V4+ Styles]\n[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,你好\n",
        "Movie.eng.srt": "1\n00:00:01,000 --> 00:00:02,000\nHello\n",
    }.items():
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        files[name] = path

    class FakeAutoService:
        def search(self, **kwargs):
            return {"results": [{"provider": "opensubtitles", "result_id": "os-1", "title": "Movie pack", "score": 90, "downloadable": True}]}

        def download(self, selected):
            return [{"provider": "opensubtitles", "source_name": "Movie.zip", "content": b"zip", "result": selected[0]}]

    plugin._online_service = lambda: FakeAutoService()
    fake_keywords = lambda item, target: ["Movie 2024"]
    fake_extract = lambda source_name, content, session_dir: [
        {"upload_id": name, "source_name": name, "stored_path": str(path), "ext": path.suffix.lower()}
        for name, path in files.items()
    ]
    captured = {}

    def fake_write(**kwargs):
        captured["operations"] = kwargs["operations"]
        return [{"output_name": operation["destination_name"]} for operation in kwargs["operations"]], 1, 0

    service = auto_transfer_service_with(
        plugin,
        auto_search_keywords_for_entry=fake_keywords,
        _extract_subtitle_files=fake_extract,
        _write_operations_to_disk=fake_write,
        submit_autosub_for_entries=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Chinese best match should not trigger AI")
        ),
    )

    result = service.auto_search_write_subtitle(entry)

    assert result["status"] == "written"
    assert [operation["upload_info"]["source_name"] for operation in captured["operations"]] == ["Movie.chi.ass"]
    assert result["written"][0]["output_name"] == "Movie.chi.ass"


@pytest.mark.parametrize("mode", ["best", "chinese_all", "all"])
def test_auto_strict_language_downloads_generic_pack_and_writes_only_selected_pair(tmp_path, mode):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    plugin._auto_subtitle_language_priority = ["chi&eng"]
    plugin._auto_subtitle_language_strict = True
    plugin._auto_multi_subtitle_mode = mode
    entry = make_auto_entry(tmp_path, filename="Movie.mkv")
    files = {}
    for name in ("Movie.chi.jp.srt", "Movie.chi.eng.srt"):
        path = tmp_path / name
        path.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")
        files[name] = path

    class FakeAutoService:
        def __init__(self):
            self.downloaded = []

        def search(self, **kwargs):
            return {"results": [
                {"provider": "assrt", "title": "Movie 中日双语", "score": 99, "downloadable": True},
                {"provider": "assrt", "title": "Movie 双语", "score": 90, "downloadable": True},
            ]}

        def download(self, selected):
            self.downloaded.append(selected[0]["title"])
            return [{"provider": "assrt", "source_name": "Movie.zip", "content": b"zip", "result": selected[0]}]

    online_service = FakeAutoService()
    plugin._online_service = lambda: online_service
    captured = []

    def fake_write(**kwargs):
        captured.extend(kwargs["operations"])
        return [{"output_name": operation["destination_name"]} for operation in kwargs["operations"]], 0, 0

    service = auto_transfer_service_with(
        plugin,
        auto_search_keywords_for_entry=lambda *_: ["Movie 2024"],
        _extract_subtitle_files=lambda *_: [
            {"upload_id": name, "source_name": name, "stored_path": str(path), "ext": ".srt"}
            for name, path in files.items()
        ],
        _write_operations_to_disk=fake_write,
        submit_autosub_for_entries=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("strict mode must not submit AI")),
    )

    result = service.auto_search_write_subtitle(entry)

    assert result["status"] == "written"
    assert online_service.downloaded == ["Movie 双语"]
    assert [operation["upload_info"]["source_name"] for operation in captured] == ["Movie.chi.eng.srt"]


def test_auto_strict_language_does_not_fall_back_to_ai(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    plugin._auto_subtitle_language_priority = ["chi&eng"]
    plugin._auto_subtitle_language_strict = True
    plugin._auto_skip_chinese_media_on_transfer = False
    entry = make_auto_entry(tmp_path, filename="Movie.mkv")
    service = auto_transfer_service_with(
        plugin,
        auto_search_write_subtitle=lambda *args, **kwargs: {"status": "skipped", "reason": "未找到符合语言要求的字幕"},
        auto_submit_ai_for_entry=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("strict mode must not fall back to AI")),
    )

    result = service.auto_process_transfer_entry(entry)

    assert result["status"] == "skipped"
    assert result["reason"] == "未找到符合语言要求的字幕"


def test_auto_write_chinese_all_keeps_chinese_and_bilingual_variants(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    plugin._auto_multi_subtitle_mode = "chinese_all"
    entry = make_auto_entry(tmp_path, filename="Movie.mkv")
    prepared_uploads = []
    for name, text in {
        "Movie.chi.ass": "[Script Info]\n[V4+ Styles]\n[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,你好\n",
        "Movie.cht.srt": "1\n00:00:01,000 --> 00:00:02,000\n你好\n",
        "Movie.chi.eng.srt": "1\n00:00:01,000 --> 00:00:02,000\n你好\nHello\n",
        "Movie.eng.srt": "1\n00:00:01,000 --> 00:00:02,000\nHello\n",
    }.items():
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        prepared_uploads.append({"upload_id": name, "source_name": name, "stored_path": str(path), "ext": path.suffix.lower()})

    captured = {}

    def fake_write(**kwargs):
        captured["operations"] = kwargs["operations"]
        return [{"output_name": operation["destination_name"]} for operation in kwargs["operations"]], 1, 0

    service = auto_transfer_service_with(
        plugin,
        _write_operations_to_disk=fake_write,
        submit_autosub_for_entries=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Chinese-all mode should ignore foreign-only subtitles when Chinese variants exist")
        ),
    )
    setattr(plugin, "_build_write" + "_operations", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("auto write should call SubtitleWriter.build_write_operations directly")
    ))

    result = service.auto_write_prepared_uploads_for_entries(
        target_entries=[entry],
        prepared_uploads=prepared_uploads,
        session_dir=tmp_path,
        selected_result={"provider": "opensubtitles", "title": "Movie pack"},
    )

    assert result["status"] == "written"
    assert [operation["upload_info"]["source_name"] for operation in captured["operations"]] == [
        "Movie.chi.ass",
        "Movie.cht.srt",
        "Movie.chi.eng.srt",
    ]


def test_auto_select_all_keeps_all_language_and_format_variants(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    plugin._auto_multi_subtitle_mode = "all"
    entry = make_auto_entry(tmp_path, filename="Movie.mkv")
    prepared_uploads = []
    for name, text in {
        "Movie.chi.ass": "[Script Info]\n[V4+ Styles]\n[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,你好\n",
        "Movie.cht.srt": "1\n00:00:01,000 --> 00:00:02,000\n你好\n",
        "Movie.eng.ass": "[Script Info]\n[V4+ Styles]\n[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hello\n",
        "Movie.eng.srt": "1\n00:00:01,000 --> 00:00:02,000\nHello\n",
    }.items():
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        prepared_uploads.append({"upload_id": name, "source_name": name, "stored_path": str(path), "ext": path.suffix.lower()})

    setattr(plugin, "_suggest" + "_target", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("auto selection should call target_resolver.suggest_target directly")
    ))
    setattr(plugin, "_auto_fill" + "_missing_targets", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("auto selection should call target_resolver.auto_fill_missing_targets directly")
    ))
    setattr(plugin, "_build_destination" + "_name", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("auto selection should call SubtitleWriter.build_destination_name directly")
    ))
    selected = plugin._select_auto_subtitle_items(prepared_uploads, [plugin._target_from_entry(entry)])

    assert [item["source_name"] for item in selected] == [
        "Movie.chi.ass",
        "Movie.cht.srt",
        "Movie.eng.ass",
        "Movie.eng.srt",
    ]


def test_online_ai_converts_foreign_ass_to_temporary_srt(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, filename="Movie.mkv")
    source = tmp_path / "Movie.eng.ass"
    source.write_text("[Script Info]\n[V4+ Styles]\n[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hello\n", encoding="utf-8")

    class FakeSubs:
        def save(self, output_path, format_=""):
            assert format_ == "srt"
            Path(output_path).write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")

    online_ai_service = plugin.services.online_ai()
    online_ai_service.load_pysubs2_file = lambda path: FakeSubs()

    def fake_timeline(video_path, subtitle_path, output_path, allow_risky_offset=False):
        assert Path(subtitle_path).suffix.lower() == ".srt"
        Path(output_path).write_text(Path(subtitle_path).read_text(encoding="utf-8"), encoding="utf-8")
        return module.TimelineFixResult(
            enabled=True,
            applied=False,
            reason="offset below threshold",
            base="test",
            offset_seconds=0.0,
            scale_factor=1.0,
            score=1.0,
        )

    plugin._run_timeline_fix = fake_timeline

    overrides, fixed = online_ai_service.prepare_online_ai_subtitle_overrides(
        session_dir=tmp_path,
        target_entries=[entry],
        prepared_uploads=[{"upload_id": "u1", "source_name": source.name, "stored_path": str(source), "ext": ".ass"}],
    )

    assert set(overrides) == {entry["path"]}
    assert overrides[entry["path"]]["subtitle_path"].endswith(".srt")
    assert fixed[0]["source_name"].endswith(".srt")
    assert fixed[0]["autosub_lang"] == "en"


def test_online_ai_allows_explicit_low_confidence_override(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, filename="Movie.mkv")
    source = tmp_path / "Movie.eng.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")

    def fake_timeline(video_path, subtitle_path, output_path, allow_risky_offset=False):
        Path(output_path).write_bytes(Path(subtitle_path).read_bytes())
        return module.TimelineFixResult(
            enabled=True,
            applied=False,
            reason="timeline alignment rejected",
            base="audio:rms",
            offset_seconds=-30.0,
            scale_factor=1.0,
            score=0.2,
            confidence="low",
            score_margin=0.0,
            risk_flags=["rms_low_precision"],
        )

    plugin._run_timeline_fix = fake_timeline
    overrides, fixed = plugin.services.online_ai().prepare_online_ai_subtitle_overrides(
        session_dir=tmp_path,
        target_entries=[entry],
        prepared_uploads=[{"upload_id": "u1", "source_name": source.name, "stored_path": str(source), "ext": ".srt"}],
        force_low_confidence=True,
    )

    assert set(overrides) == {entry["path"]}
    assert fixed[0]["timeline"]["confidence"] == "low"


def test_auto_write_foreign_ai_skip_is_not_counted_as_completed(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, filename="Movie.mkv")
    subtitle = tmp_path / "Movie.eng.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    prepared_uploads = [
        {
            "upload_id": "u1",
            "source_name": subtitle.name,
            "stored_path": str(subtitle),
            "ext": ".srt",
        }
    ]
    fake_overrides = lambda **kwargs: (
        {entry["path"]: {"subtitle_path": str(subtitle), "lang": "en"}},
        [{"target_id": entry["id"], "subtitle_path": str(subtitle), "autosub_lang": "en"}],
    )
    fake_submit = lambda *args, **kwargs: {
        "added": [],
        "skipped": [{"path": entry["path"], "reason": "任务已存在"}],
        "failed": [],
        "tasks": {"task_by_target": {}},
    }
    service = auto_transfer_service_with(
        plugin,
        prepare_online_ai_subtitle_overrides=fake_overrides,
        submit_autosub_for_entries=fake_submit,
    )

    result = service.auto_write_prepared_uploads_for_entries(
        target_entries=[entry],
        prepared_uploads=prepared_uploads,
        session_dir=tmp_path,
        selected_result={"provider": "opensubtitles", "title": "Movie English"},
    )

    assert result["status"] == "skipped"
    assert result["ai_count"] == 0
    assert result["completed_count"] == 0
    assert result["ai_by_target"] == {}
    assert "AI 字幕任务未新增" in result["reason"]


def test_online_ai_falls_back_to_srt_when_foreign_ass_conversion_fails(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    entry = make_auto_entry(tmp_path, filename="Movie.mkv")
    ass_source = tmp_path / "Movie.eng.ass"
    srt_source = tmp_path / "Movie.eng.srt"
    ass_source.write_text("[Script Info]\n[V4+ Styles]\n[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hello\n", encoding="utf-8")
    srt_source.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    online_ai_service = plugin.services.online_ai()
    online_ai_service.load_pysubs2_file = lambda path: (_ for _ in ()).throw(RuntimeError("bad ass"))
    observed = {}

    def fake_timeline(video_path, subtitle_path, output_path, allow_risky_offset=False):
        observed["subtitle_path"] = subtitle_path
        Path(output_path).write_text(Path(subtitle_path).read_text(encoding="utf-8"), encoding="utf-8")
        return module.TimelineFixResult(
            enabled=True,
            applied=False,
            reason="offset below threshold",
            base="test",
            offset_seconds=0.0,
            scale_factor=1.0,
            score=1.0,
        )

    plugin._run_timeline_fix = fake_timeline

    overrides, fixed = online_ai_service.prepare_online_ai_subtitle_overrides(
        session_dir=tmp_path,
        target_entries=[entry],
        prepared_uploads=[
            {"upload_id": "ass", "source_name": ass_source.name, "stored_path": str(ass_source), "ext": ".ass"},
            {"upload_id": "srt", "source_name": srt_source.name, "stored_path": str(srt_source), "ext": ".srt"},
        ],
    )

    assert Path(observed["subtitle_path"]) == srt_source
    assert set(overrides) == {entry["path"]}
    assert fixed[0]["source_name"] == "Movie.eng.srt"


def test_auto_search_providers_use_all_enabled_sources():
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    plugin._online_provider_ids = ["subhd", "zimuku", "assrt", "opensubtitles", "subtitlecat", "subdl"]

    assert plugin._auto_search_providers() == ["subhd", "assrt", "opensubtitles", "subtitlecat", "subdl"]


def test_transfer_auto_claim_dedupes_same_video_path(tmp_path):
    module, _, _ = load_plugin_module()
    plugin = make_plugin(module)
    first = make_auto_entry(tmp_path, filename="Movie.mkv")
    duplicate = {**first}
    other = make_auto_entry(tmp_path, filename="Other.mkv")

    claimed, skipped = plugin._claim_transfer_auto_entries([first, duplicate, other])
    claimed_again, skipped_again = plugin._claim_transfer_auto_entries([first])

    assert [item["filename"] for item in claimed] == ["Movie.mkv", "Other.mkv"]
    assert skipped == 1
    assert claimed_again == []
    assert skipped_again == 1
