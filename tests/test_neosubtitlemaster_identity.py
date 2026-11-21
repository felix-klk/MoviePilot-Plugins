from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_subtitlemanualupload_cache import load_plugin_module, plugin_submodule


@pytest.mark.parametrize("generation", ["v2", "v3"])
def test_neo_and_legacy_register_as_independent_plugins(monkeypatch, generation):
    package_dir = f"plugins.{generation}"
    if generation == "v2":
        monkeypatch.setitem(sys.modules, "app.sdk", None)
    legacy, _, _ = load_plugin_module(package_dir)
    neo, _, _ = load_plugin_module(package_dir, "NeoSubtitleMaster")
    original = legacy.SubtitleManualUpload()
    plugin = neo.NeoSubtitleMaster()
    plugin._enabled = True

    root = Path(__file__).resolve().parents[1]
    catalog = json.loads((root / f"package.{generation}.json").read_text(encoding="utf-8"))
    manifest = catalog[type(plugin).__name__]
    assert manifest["name"] == plugin.plugin_name == "NEO字幕大师"
    assert manifest["author"] == plugin.plugin_author == "felix-klk"
    assert manifest["author_url"] == plugin.author_url == "https://github.com/felix-klk"
    frontend_manifest = Path(neo.__file__).with_name("package.json")
    frontend_version = json.loads(frontend_manifest.read_text(encoding="utf-8"))["version"]
    assert manifest["version"] == plugin.plugin_version == frontend_version
    assert f"v{plugin.plugin_version}" in manifest["history"]
    if generation == "v2":
        legacy_catalog = json.loads((root / "package.json").read_text(encoding="utf-8"))
        v3_catalog = json.loads((root / "package.v3.json").read_text(encoding="utf-8"))
        assert legacy_catalog[type(plugin).__name__] == manifest
        assert v3_catalog[type(plugin).__name__]["version"] != plugin.plugin_version
    assert manifest["release"] is True
    assert catalog[type(original).__name__]["version"] == original.plugin_version
    assert plugin.plugin_config_prefix == "neosubtitlemaster_"
    assert plugin.plugin_config_prefix != original.plugin_config_prefix
    assert plugin.get_sidebar_nav()[0]["title"] == plugin.plugin_name
    assert "/local_directories/scan" in {route["path"] for route in plugin.get_api()}

    action_ids = {action["id"] for action in plugin.get_actions()}
    assert action_ids and all(action.startswith("neosubtitlemaster_") for action in action_ids)
    assert action_ids.isdisjoint(action["id"] for action in original.get_actions())
    tools = plugin.get_agent_tools()
    agent_module = plugin_submodule(neo, "automation.agent_tools")
    registered = {"SubtitleManualUpload": original, "NeoSubtitleMaster": plugin}
    monkeypatch.setattr(agent_module, "PluginManager", lambda: SimpleNamespace(running_plugins=registered))
    assert {tool.name for tool in tools}.isdisjoint(tool.name for tool in original.get_agent_tools())
    assert all(tool()._plugin_instance() is plugin for tool in tools)
    registered.pop("NeoSubtitleMaster")
    assert all(tool()._plugin_instance() is None for tool in tools)


@pytest.mark.parametrize("generation", ["plugins.v2", "plugins.v3"])
def test_neo_keeps_autosub_task_source_protocol(monkeypatch, tmp_path, generation):
    neo, _, _ = load_plugin_module(generation, "NeoSubtitleMaster")
    plugin = neo.NeoSubtitleMaster()
    bridge = plugin.services.autosub_bridge()
    selected = tmp_path / "Movie.mkv"
    entry = {"id": "video", "path": str(selected)}
    submitted = []

    def submit_tasks(paths, **options):
        submitted.append((paths, options))
        return {"added": [{"path": paths[0]}]}

    monkeypatch.setattr(plugin, "_autosub_plugin", lambda: (SimpleNamespace(submit_tasks=submit_tasks), ""))
    monkeypatch.setattr(plugin, "_target_from_entry", lambda item: item)
    monkeypatch.setattr(bridge, "autosub_tasks_for_entries", lambda entries: {})

    bridge.submit_autosub_for_entries([entry])
    bridge.submit_autosub_for_entries([entry], subtitle_overrides={str(selected): {"path": "Movie.eng.ass"}})
    assert [paths for paths, _ in submitted] == [[str(selected)], [str(selected)]]
    assert {options["source"] for _, options in submitted} == {"subtitle_manual_upload"}


def test_neo_v2_groups_host_history_and_directory_media_by_tmdb_id(tmp_path):
    neo, history, _ = load_plugin_module("plugins.v2", "NeoSubtitleMaster")
    plugin = neo.NeoSubtitleMaster()
    plugin.get_data_path = lambda: tmp_path / "plugin-data"
    runtime = plugin_submodule(neo, "config.config_runtime")
    runtime.reset_runtime_state(plugin)
    root = tmp_path / "library"
    root.mkdir()
    first = tmp_path / "流人.S06E01.mkv"
    second = root / "流人.S06E02.mkv"
    first.touch()
    second.touch()
    (root / "tvshow.nfo").write_text(
        '<tvshow><title>流人</title><uniqueid type="tmdb">123</uniqueid></tvshow>',
        encoding="utf-8",
    )
    history.data = [
        SimpleNamespace(status=True, dest=str(first), type="电视剧", title="流人", tmdbid=123),
    ]
    plugin._manual_strm_enabled = True
    plugin._manual_strm_paths = [str(root)]
    catalog = plugin.services.local_media_catalog()
    entries = catalog.load_local_entries(force=True)
    assert history.calls == 1
    assert {entry["path"] for entry in entries if entry["media_key"] == "tmdb:123"} == {
        str(first), str(second),
    }
    assert len(catalog.group_entries_as_media(entries, limit=10)) == 1
    entries_from_event = plugin.services.target_resolver().entries_from_transfer_event({
        "mediainfo": SimpleNamespace(tmdb_id=123, type="电视剧", title="流人"),
        "transferinfo": {"file_list_new": [str(second)]},
    })
    assert [entry["media_key"] for entry in entries_from_event] == ["tmdb:123"]


def test_neo_v2_discovers_and_scopes_emby_connections(monkeypatch):
    neo, _, _ = load_plugin_module("plugins.v2", "NeoSubtitleMaster")
    service = neo.NeoSubtitleMaster().services.emby_refresh()
    calls = []
    servers = {name: SimpleNamespace(type="emby", instance=object()) for name in ("Living room", "NAS")}

    def get_services(**filters):
        calls.append(filters)
        return servers

    helper = SimpleNamespace(get_configs=lambda: servers, get_services=get_services)
    monkeypatch.setitem(sys.modules, "app.helper.mediaserver", SimpleNamespace(MediaServerHelper=lambda: helper))
    assert service.configured_emby_names() == ["Living room", "NAS"]
    assert service._configured_instances(emby_only=True, selected_names={"NAS"}) == {"NAS": servers["NAS"].instance}
    assert service._configured_instances(emby_only=True, selected_names=set()) == {}
    assert calls == [{"type_filter": "emby", "name_filters": ["NAS"]}]
