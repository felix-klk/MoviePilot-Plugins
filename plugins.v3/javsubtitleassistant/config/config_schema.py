from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..online.online_subtitle import (
    DEFAULT_ENGINE,
    normalize_online_engine,
    normalize_provider_roots,
)
from ..matching.subtitle_language import (
    DEFAULT_AUTO_FORMAT_PRIORITY,
)

from ..online_subtitles.catalog import (
    DEFAULT_ONLINE_PROVIDER_IDS, AVAILABLE_ONLINE_PROVIDER_IDS,
    PROVIDER_CATALOG,
)


DEFAULT_RAR_TOOL_PATH = "/usr/bin/unar"
RAR_DEPENDENCY_MODES = {"none", "container_install", "mapped_binary"}
AUTO_TRANSFER_SUBTITLE_STRATEGIES = {"online_then_ai_source", "online_source_only", "ai_source_only"}
AUTO_MULTI_SUBTITLE_MODES = {"best", "chinese_all", "all"}
AUTO_TRANSFER_SUBTITLE_STRATEGY_ALIASES = {
    "search_first": "online_then_ai_source",
    "search_only": "online_source_only",
    "ai_only": "ai_source_only",
    "ai_first": "ai_source_only",
}
TIMELINE_VAD_MODES = {"webrtc", "rms"}

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "show_sidebar_nav": True,
    "rar_dependency_mode": "none",
    "rar_tool_path": DEFAULT_RAR_TOOL_PATH,
    "traditional_to_simplified": False,
    "emby_refresh_after_subtitle_write": False,
    "auto_search_on_transfer": False,
    "auto_check_emby_subtitles": False,
    "emby_library_servers": None,
    "emby_query_libraries": None,
    "emby_subtitle_path_mappings": "",
    "auto_skip_chinese_media_on_transfer": False,
    "auto_transfer_subtitle_strategy": "online_source_only",
    "trust_transfer_history_paths": False,
    "manual_strm_enabled": True,
    "manual_strm_paths": [],
    "auto_search_on_manual_strm": True,
    "auto_multi_subtitle_mode": "best",
    "auto_subtitle_legacy_ranking": False,
    "auto_subtitle_language_priority": ["chi", "cht"],
    "auto_subtitle_language_strict": True,
    "auto_subtitle_format_priority": list(DEFAULT_AUTO_FORMAT_PRIORITY),
    "auto_ass_to_srt_for_ai": True,
    "timeline_max_offset_seconds": 120,
    "timeline_min_offset_seconds": 0.2,
    "timeline_vad_mode": "webrtc",
    "timeline_allow_risky_offset": False,
    "online_providers": ["subtitlecat"],
    "online_engine": DEFAULT_ENGINE,
    "online_use_proxy": False,
    "subtitlecat_include_translations": True,
    "ai_link_enabled": False,
}


def normalize_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_emby_query_libraries(value: Any) -> list[dict[str, Any]] | None:
    """Keep missing scope distinct from an explicitly empty selection."""
    if value is None:
        return None
    by_library = {}
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        server = normalize_text(item.get("server"))
        library_id = normalize_text(item.get("id"))
        if not server or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", library_id):
            continue
        paths = item.get("paths")
        by_library[server, library_id] = {
            "server": server,
            "server_id": normalize_text(item.get("server_id")),
            "id": library_id,
            "name": normalize_text(item.get("name")) or library_id,
            "paths": list(dict.fromkeys(
                path.strip() for path in paths if isinstance(path, str) and path.strip()
            )) if isinstance(paths, list) else [],
        }
    return list(by_library.values())


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def normalize_timeline_max_offset(value: Any) -> int:
    seconds = safe_int(value, 120)
    if seconds <= 0:
        return 120
    return min(seconds, 300)


def normalize_timeline_min_offset(value: Any) -> float:
    try:
        seconds = float(value)
    except Exception:
        seconds = 0.2
    if seconds <= 0 or seconds > 1:
        return 0.2
    return seconds


def normalize_timeline_vad_mode(value: Any) -> str:
    mode = normalize_text(value).lower()
    return mode if mode in TIMELINE_VAD_MODES else "webrtc"


def normalize_auto_multi_subtitle_mode(value: Any) -> str:
    mode = normalize_text(value).lower()
    aliases = {
        "preferred": "best",
        "prefer": "best",
        "best_only": "best",
        "chinese": "chinese_all",
        "chinese_and_bilingual": "chinese_all",
        "all_chinese": "chinese_all",
        "everything": "all",
    }
    mode = aliases.get(mode, mode)
    return mode if mode in AUTO_MULTI_SUBTITLE_MODES else "best"


def normalize_rar_dependency_mode(value: Any) -> str:
    mode = normalize_text(value).lower()
    if mode in RAR_DEPENDENCY_MODES:
        return mode
    return "none"


def normalize_auto_transfer_subtitle_strategy(value: Any) -> str:
    strategy = normalize_text(value).lower()
    strategy = AUTO_TRANSFER_SUBTITLE_STRATEGY_ALIASES.get(strategy, strategy)
    if strategy in AUTO_TRANSFER_SUBTITLE_STRATEGIES:
        return strategy
    return "online_source_only"


def normalize_provider_ids(
    value: Any,
    *,
    available_provider_ids: Optional[Iterable[str]] = None,
    default_provider_ids: Optional[Iterable[str]] = None,
) -> List[str]:
    allowed = set(available_provider_ids or AVAILABLE_ONLINE_PROVIDER_IDS)
    defaults = list(default_provider_ids or DEFAULT_ONLINE_PROVIDER_IDS)
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        raw_items = re.split(r"[,，\s]+", value)
    else:
        raw_items = defaults
    providers = []
    for item in raw_items:
        provider_id = normalize_text(item).lower()
        if provider_id in allowed and provider_id not in providers:
            providers.append(provider_id)
    # Explicit empty selections (including retired-only sources) remain disabled.
    return providers


def normalize_online_site_urls(config: Dict[str, Any]) -> Dict[str, str]:
    raw = config.get("online_site_urls") if isinstance(config.get("online_site_urls"), dict) else {}
    roots = {provider_id: raw.get(provider_id) or config.get(f"{provider_id}_url")
             for provider_id in AVAILABLE_ONLINE_PROVIDER_IDS}
    return normalize_provider_roots(roots)


def build_default_config(
    *,
    default_auto_language_priority: Optional[Iterable[str]] = None,
    default_auto_format_priority: Optional[Iterable[str]] = None,
    default_online_provider_ids: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    config["auto_subtitle_language_priority"] = list(DEFAULT_CONFIG["auto_subtitle_language_priority"])
    config["auto_subtitle_format_priority"] = list(default_auto_format_priority or DEFAULT_AUTO_FORMAT_PRIORITY)
    config["online_providers"] = list(default_online_provider_ids or DEFAULT_ONLINE_PROVIDER_IDS)
    return config


def normalize_plugin_config(
    config: Optional[Dict[str, Any]],
    *,
    subtitle_exts: Optional[Iterable[str]] = None,
    default_auto_language_priority: Optional[Iterable[str]] = None,
    default_auto_format_priority: Optional[Iterable[str]] = None,
    available_provider_ids: Optional[Iterable[str]] = None,
    default_provider_ids: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    raw_config = config or {}
    available_provider_list = list(available_provider_ids or AVAILABLE_ONLINE_PROVIDER_IDS)
    default_provider_list = list(default_provider_ids or DEFAULT_ONLINE_PROVIDER_IDS)
    online_provider_ids = normalize_provider_ids(
        raw_config.get("online_providers"),
        available_provider_ids=available_provider_list,
        default_provider_ids=default_provider_list,
    )
    # A previous source selection migrates to SubtitleCat when every selected site was removed.
    if not online_provider_ids and raw_config.get("online_providers"):
        online_provider_ids = default_provider_list
    legacy_proxy_default = "online_proxy_migrated" not in raw_config and raw_config.get("online_use_proxy") is True
    emby_libraries = normalize_emby_query_libraries(raw_config.get("emby_query_libraries"))
    emby_servers = raw_config.get("emby_library_servers")
    if not isinstance(emby_servers, list):
        emby_servers = [library["server"] for library in emby_libraries or []]
    emby_servers = list(dict.fromkeys(
        name.strip() for name in emby_servers if isinstance(name, str) and name.strip()
    ))

    return {
        "enabled": bool(raw_config.get("enabled")),
        "show_sidebar_nav": bool(raw_config.get("show_sidebar_nav", True)),
        "rar_dependency_mode": normalize_rar_dependency_mode(raw_config.get("rar_dependency_mode")),
        "rar_tool_path": normalize_text(raw_config.get("rar_tool_path")) or DEFAULT_RAR_TOOL_PATH,
        "online_providers": online_provider_ids,
        "online_engine": normalize_online_engine(raw_config.get("online_engine")),
        "online_use_proxy": False if legacy_proxy_default else bool(raw_config.get("online_use_proxy", False)),
        "online_site_urls": normalize_online_site_urls(raw_config),
        "subtitlecat_include_translations": bool(raw_config.get("subtitlecat_include_translations", True)),
        "ai_link_enabled": bool(raw_config.get("ai_link_enabled", False)),
        "traditional_to_simplified": bool(raw_config.get("traditional_to_simplified", False)),
        "emby_refresh_after_subtitle_write": bool(raw_config.get("emby_refresh_after_subtitle_write", False)),
        "auto_search_on_transfer": bool(raw_config.get("auto_search_on_transfer", False)),
        "auto_check_emby_subtitles": bool(raw_config.get("auto_check_emby_subtitles", False)),
        "emby_library_servers": emby_servers,
        "emby_query_libraries": emby_libraries,
        "emby_subtitle_path_mappings": normalize_text(raw_config.get("emby_subtitle_path_mappings")),
        "auto_skip_chinese_media_on_transfer": DEFAULT_CONFIG["auto_skip_chinese_media_on_transfer"],
        "auto_transfer_subtitle_strategy": DEFAULT_CONFIG["auto_transfer_subtitle_strategy"],
        "trust_transfer_history_paths": bool(raw_config.get("trust_transfer_history_paths", False)),
        "manual_strm_enabled": bool(raw_config.get("manual_strm_enabled", True)),
        "manual_strm_paths": normalize_manual_strm_paths(raw_config.get("manual_strm_paths")),
        "auto_search_on_manual_strm": bool(raw_config.get("auto_search_on_manual_strm", True)),
        "auto_multi_subtitle_mode": DEFAULT_CONFIG["auto_multi_subtitle_mode"],
        "auto_subtitle_legacy_ranking": DEFAULT_CONFIG["auto_subtitle_legacy_ranking"],
        "auto_subtitle_language_priority": list(DEFAULT_CONFIG["auto_subtitle_language_priority"]),
        "auto_subtitle_language_strict": DEFAULT_CONFIG["auto_subtitle_language_strict"],
        "auto_subtitle_format_priority": list(DEFAULT_CONFIG["auto_subtitle_format_priority"]),
        "auto_ass_to_srt_for_ai": DEFAULT_CONFIG["auto_ass_to_srt_for_ai"],
        "timeline_max_offset_seconds": normalize_timeline_max_offset(raw_config.get("timeline_max_offset_seconds")),
        "timeline_min_offset_seconds": normalize_timeline_min_offset(raw_config.get("timeline_min_offset_seconds")),
        "timeline_vad_mode": normalize_timeline_vad_mode(raw_config.get("timeline_vad_mode")),
        "timeline_allow_risky_offset": bool(raw_config.get("timeline_allow_risky_offset", False)),
    }


def normalize_manual_strm_paths(value: Any) -> List[str]:
    """规范化本地 STRM 根目录，多行保存并去除父子重复目录。"""
    raw_items = value if isinstance(value, list) else str(value or "").splitlines()
    paths: List[str] = []
    for item in raw_items:
        text = normalize_text(item).strip().rstrip("/\\")
        if not text or not re.match(r"^[A-Za-z]:[\\/]", text) and not text.startswith("/"):
            continue
        if text not in paths:
            paths.append(text)
    result: List[str] = []
    for path in sorted(paths, key=lambda item: (len(item), item.lower())):
        normalized = path.replace("\\", "/").rstrip("/")
        if any(normalized == parent or normalized.startswith(parent + "/") for parent in result):
            continue
        result.append(path)
    return result


def _field(component: str, props: Dict[str, Any]) -> Dict[str, Any]:
    return {"component": component, "props": props}


def _col(component: str, props: Dict[str, Any], *, md: Optional[int] = None) -> Dict[str, Any]:
    col_props = {"cols": 12}
    if md is not None:
        col_props["md"] = md
    return {"component": "VCol", "props": col_props, "content": [_field(component, props)]}


def _row(*columns: Dict[str, Any]) -> Dict[str, Any]:
    return {"component": "VRow", "content": list(columns)}


def _alert(text: str) -> Dict[str, Any]:
    return _row(
        {
            "component": "VCol",
            "props": {"cols": 12},
            "content": [
                _field(
                    "VAlert",
                    {
                        "type": "info",
                        "variant": "tonal",
                        "text": text,
                    },
                )
            ],
        }
    )


def build_config_form(
    *,
    default_auto_language_priority: Optional[Iterable[str]] = None,
    default_auto_format_priority: Optional[Iterable[str]] = None,
    default_online_provider_ids: Optional[Iterable[str]] = None,
) -> Tuple[List[dict], Dict[str, Any]]:
    form = [
        {
            "component": "VForm",
            "content": [
                _row(
                    _col("VSwitch", {"model": "enabled", "label": "启用插件"}, md=3),
                    _col("VSwitch", {"model": "show_sidebar_nav", "label": "显示侧边栏入口"}, md=3),
                    _col("VSwitch", {"model": "traditional_to_simplified", "label": "写入前繁体转简体"}, md=3),
                    _col("VSwitch", {"model": "emby_refresh_after_subtitle_write", "label": "写入后扫描 Emby 外挂字幕"}, md=3),
                    _col("VSwitch", {"model": "auto_search_on_transfer", "label": "入库后自动搜索匹配字幕"}, md=3),
                ),
                _row(
                    _col(
                        "VTextField",
                        {
                            "model": "timeline_max_offset_seconds",
                            "label": "智能调轴最大偏移秒数",
                            "type": "number",
                            "placeholder": "120",
                            "min": 1,
                            "max": 300,
                            "suffix": "秒",
                            "hint": "默认 120；不建议超过 120 秒，过大偏移通常意味着字幕错集或错版本。",
                            "persistentHint": True,
                        },
                        md=4,
                    ),
                    _col(
                        "VTextField",
                        {
                            "model": "timeline_min_offset_seconds",
                            "label": "智能调轴最小应用阈值",
                            "placeholder": "0.2",
                        },
                        md=4,
                    ),
                    _col(
                        "VSelect",
                        {
                            "model": "timeline_vad_mode",
                            "label": "音频 VAD 模式",
                            "items": [
                                {"title": "WebRTC VAD（推荐）", "value": "webrtc"},
                                {"title": "RMS 能量阈值（降级）", "value": "rms"},
                            ],
                        },
                        md=4,
                    ),
                ),
                _row(
                    _col(
                        "VSwitch",
                        {
                            "model": "auto_check_emby_subtitles",
                            "label": "自动下载前检查 Emby 字幕",
                            "hint": "本地无法读取内嵌字幕时，查询 MoviePilot 已配置的 Emby。",
                            "persistentHint": True,
                        },
                        md=6,
                    ),
                    _col(
                        "VTextarea",
                        {
                            "model": "emby_subtitle_path_mappings",
                            "label": "Emby 路径映射",
                            "hint": "路径相同可留空。每行填一组，例如 /media => /mnt/media。",
                            "persistentHint": True,
                            "rows": 2,
                        },
                        md=6,
                    ),
                    _col(
                        "VSwitch",
                        {
                            "model": "manual_strm_enabled",
                            "label": "监控本地整理目录",
                            "hint": "开启后展开目录配置，并实时监控本地硬盘上的 .strm 和同目录字幕变化。",
                            "persistentHint": True,
                        },
                        md=6,
                    ),
                    _col(
                        "VTextarea",
                        {
                            "model": "manual_strm_paths",
                            "label": "本地整理目录",
                            "placeholder": "/vol2/1000/raid/2/links2\n/vol1/1000/media-strm",
                            "rows": 3,
                            "autoGrow": True,
                            "hint": "每行一个 MoviePilot 容器内可见的绝对路径；不读取 .strm 内容。",
                            "persistentHint": True,
                        },
                        md=12,
                    ),
                    _col(
                        "VSwitch",
                        {
                            "model": "auto_search_on_manual_strm",
                            "label": "本地视频新增或更新时自动补中文字幕",
                            "hint": "发现本地视频新增或变化时检查字幕；已有中文就跳过，否则尝试补一份。",
                            "persistentHint": True,
                        },
                        md=6,
                    ),
                    _col(
                        "VSwitch",
                        {
                            "model": "trust_transfer_history_paths",
                            "label": "信任整理历史路径",
                            "hint": "CD2、网盘挂载、SMB 等慢路径可开启，刷新资源清单时不逐条访问文件。",
                            "persistentHint": True,
                        },
                        md=6,
                    ),
                ),
                _row(
                    _col(
                        "VSelect",
                        {
                            "model": "online_providers",
                            "label": "自动字幕源",
                            "multiple": True,
                            "chips": True,
                            "items": [{"title": item["name"], "value": item["id"]} for item in PROVIDER_CATALOG],
                        },
                        md=6,
                    ),
                    _col(
                        "VSwitch",
                        {"model": "online_use_proxy", "label": "API 搜索和下载使用 MoviePilot 系统代理（默认关闭）"},
                        md=6,
                    ),
                    _col("VSwitch", {"model": "subtitlecat_include_translations", "label": "接受 SubtitleCat 已有中文字幕译文"}, md=6),
                ),
                _row(
                    _col(
                        "VSelect",
                        {
                            "model": "rar_dependency_mode",
                            "label": "压缩包解压器处理方式",
                            "items": [
                                {"title": "不处理，仅检测", "value": "none"},
                                {"title": "加载插件时尝试容器内安装 unar", "value": "container_install"},
                                {"title": "使用宿主机映射文件", "value": "mapped_binary"},
                            ],
                        },
                        md=6,
                    ),
                    _col(
                        "VTextField",
                        {
                            "model": "rar_tool_path",
                            "label": "容器内映射路径",
                            "placeholder": DEFAULT_RAR_TOOL_PATH,
                        },
                        md=6,
                    ),
                ),
            ],
        }
    ]
    return form, build_default_config(
        default_auto_language_priority=default_auto_language_priority,
        default_auto_format_priority=default_auto_format_priority,
        default_online_provider_ids=default_online_provider_ids,
    )
