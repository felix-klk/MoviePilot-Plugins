from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from test_subtitlemanualupload_cache import load_plugin_module, plugin_submodule


def test_neo_and_legacy_register_as_independent_plugins(monkeypatch):
    legacy, _, _ = load_plugin_module("plugins.v3")
    neo, _, _ = load_plugin_module("plugins.v3", "NeoSubtitleMaster")
    original = legacy.SubtitleManualUpload()
    plugin = neo.NeoSubtitleMaster()
    plugin._enabled = True

    root = Path(__file__).resolve().parents[1]
    catalog = json.loads((root / "package.v3.json").read_text(encoding="utf-8"))
    manifest = catalog[type(plugin).__name__]
    assert manifest["name"] == plugin.plugin_name == "NEO字幕大师"
    assert manifest["author"] == plugin.plugin_author == "felix-klk"
    assert manifest["author_url"] == plugin.author_url == "https://github.com/felix-klk"
    assert manifest["version"] == plugin.plugin_version == "1.0.0"
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


def test_neo_keeps_autosub_task_source_protocol(monkeypatch, tmp_path):
    neo, _, _ = load_plugin_module("plugins.v3", "NeoSubtitleMaster")
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
