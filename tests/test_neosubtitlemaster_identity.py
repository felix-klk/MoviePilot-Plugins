from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_subtitlemanualupload_cache import load_plugin_module, plugin_submodule


@pytest.mark.parametrize(("dialogue", "suffix"), [
    ("请告诉我你们准备什么时候出发", "chi"),
    ("우리 모두 지금 어디로 가야 하는지 알려 주세요", "kor"),
])
def test_neo_v3_detects_utf8_subtitle_when_preview_ends_inside_character(dialogue, suffix):
    neo, _, _ = load_plugin_module("plugins.v3", "NeoSubtitleMaster")
    language = plugin_submodule(neo, "matching.subtitle_language")
    header = b"1\n00:00:01,000 --> 00:00:04,000\n"
    raw = header + (dialogue.replace(" ", "") * 600).encode("utf-8")
    with pytest.raises(UnicodeDecodeError):
        raw[:16000].decode("utf-8")
    assert language.detect_language_profile("episode.srt", raw, {".srt"})["suffix"] == suffix


def test_neo_v3_japanese_names_do_not_count_as_chinese_dialogue():
    neo, _, _ = load_plugin_module("plugins.v3", "NeoSubtitleMaster")
    language = plugin_submodule(neo, "matching.subtitle_language")
    japanese = "国際連合安全保障理事会\n" * 3 + "緊急会議の開催について報告します。\n" * 24
    chinese = "所有客人已抵達，今晚一起出發。\n" * 20
    assert language.detect_language_profile("episode.srt", japanese.encode(), {".srt"})["suffix"] == "jpn"
    assert language.detect_language_profile("episode.srt", (japanese + chinese).encode(), {".srt"})["suffix"] == "chi&jp"


@pytest.mark.parametrize(("foreign", "label"), [
    ("eng", "英"), ("jp", "日"), ("kr", "韩"),
    ("fre", "法"), ("rus", "俄"), ("spa", "西"), ("ger", "德"), ("por", "葡"), ("ita", "意"),
])
@pytest.mark.parametrize(("chinese", "prefix", "locale"), [("chi", "中", "zh-CN"), ("cht", "繁", "zh-TW")])
def test_neo_v3_bilingual_filename_round_trip(foreign, label, chinese, prefix, locale):
    neo, _, _ = load_plugin_module("plugins.v3", "NeoSubtitleMaster")
    language = plugin_submodule(neo, "matching.subtitle_language")
    writer = plugin_submodule(neo, "matching.subtitle_writer")
    suffix = f"{chinese}&{foreign}"
    named_alias = f"{prefix}{label}双语"
    name = writer.build_destination_name(
        {"basename": "Episode"}, {"language_suffix": f"{foreign}&{chinese}", "ext": ".srt"},
        normalize_text=language.normalize_text, normalize_language_suffix=language.normalize_language_suffix,
    )
    assert name == f"Episode.{named_alias}.{locale}.srt"
    assert language.normalize_language_suffix(named_alias) == suffix
    assert language.language_suffix_from_filename(name, {".srt"})["suffix"] == suffix
    assert language.language_profile_from_text(name)["suffix"] == suffix
    assert language.language_suffix_from_filename(f"Episode.{suffix}.srt", {".srt"})["suffix"] == suffix


CHINESE_LINES = [
    "这件事情必须先告诉他们。",
    "如果你们已经准备好了，我们现在出发。",
    "他说等大家回来以后再一起决定。",
]
ENGLISH_LINES = [
    "We will tell them what happened tomorrow.",
    "Have you finished all of your work?",
    "They are waiting for us at the station.",
]
JAPANESE_LINES = [
    "この部屋にいる皆さんに伝えてください。",
    "今日はとても大切な日です。",
    "もう少ししたら家に帰ります。",
]
KOREAN_LINES = [
    "여기에 있는 모든 사람에게 알려 주세요.",
    "우리 모두 오늘 일을 끝내고 돌아가야 해요.",
    "나는 바로 그쪽으로 가겠다고 말했습니다.",
]
PORTUGUESE_LINES = [
    "Não sei se você já contou o que aconteceu.",
    "Então vamos conversar com eles também.",
    "Ninguém mesmo acredita que isso seja verdade.",
]


def _srt_text(lines):
    return "\n\n".join(
        f"{index}\n00:00:01,000 --> 00:00:05,000\n{line}" for index, line in enumerate(lines, 1)
    ) + "\n"


@pytest.mark.parametrize(("encoding", "lines", "expected"), [
    ("utf-8", CHINESE_LINES, "chi"),
    ("utf-8-sig", KOREAN_LINES, "kor"),
    ("utf-16", JAPANESE_LINES, "jpn"),
    ("utf-16-le", KOREAN_LINES, "kor"),
    ("utf-16-be", CHINESE_LINES, "chi"),
    ("gb18030", CHINESE_LINES, "chi"),
    ("big5", ["我們說過了，請把這個交給他。"] * 4, "cht"),
    ("cp932", JAPANESE_LINES, "jpn"),
    ("cp949", KOREAN_LINES, "kor"),
    ("cp1252", PORTUGUESE_LINES, "por"),
])
def test_neo_v3_subtitle_dialogue_overrides_claimed_bilingual_tag(encoding, lines, expected):
    neo, _, _ = load_plugin_module("plugins.v3", "NeoSubtitleMaster")
    raw = _srt_text(lines * 12).encode(encoding)
    assert neo.NeoSubtitleMaster._detect_language_profile("Show.S01E01.chi&eng.srt", raw)["suffix"] == expected


@pytest.mark.parametrize(("lines", "expected"), [
    (KOREAN_LINES, "chi&kr"),
    (JAPANESE_LINES, "chi&jp"),
    (ENGLISH_LINES, "chi&eng"),
    (PORTUGUESE_LINES, "chi&por"),
])
def test_neo_v3_detects_two_dialogue_languages_beyond_first_sample(lines, expected):
    neo, _, _ = load_plugin_module("plugins.v3", "NeoSubtitleMaster")
    raw = _srt_text(CHINESE_LINES * 180 + lines).encode()
    assert len(_srt_text(CHINESE_LINES * 180).encode()) > 16000
    assert neo.NeoSubtitleMaster._detect_language_profile("episode.srt", raw)["suffix"] == expected


def test_neo_v3_ignores_vtt_notes_and_cue_ids_when_checking_language():
    neo, _, _ = load_plugin_module("plugins.v3", "NeoSubtitleMaster")
    text = "WEBVTT\n\nNOTE\n" + "\n".join(ENGLISH_LINES) + "\n\n"
    text += "\n\n".join(
        f"{english}\n00:00.000 --> 00:04.000\n{chinese}"
        for english, chinese in zip(ENGLISH_LINES, CHINESE_LINES)
    )
    assert neo.NeoSubtitleMaster._detect_language_profile("episode.eng.vtt", text.encode())["suffix"] == "chi"


@pytest.mark.parametrize("raw", [
    b"\xff\xfe\x00", b"<html><body>Authentication failed</body></html>",
    b"1\n00:00:00,000 --> 00:00:01,000\nA12345.",
    _srt_text(["東京"]).encode(),
    _srt_text(["你好\nHello"]).encode(),
])
def test_neo_v3_does_not_use_filename_to_hide_unconfirmed_text(raw):
    neo, _, _ = load_plugin_module("plugins.v3", "NeoSubtitleMaster")
    assert neo.NeoSubtitleMaster._detect_language_profile("episode.chi&eng.srt", raw)["suffix"] == "und"


def test_neo_v3_bitmap_keeps_explicit_language_tag():
    neo, _, _ = load_plugin_module("plugins.v3", "NeoSubtitleMaster")
    raw = b"\x00\x00\x01\xba\x00\x01"
    assert neo.NeoSubtitleMaster._detect_language_profile("episode.chi&eng.sub", raw)["suffix"] == "chi&eng"
    assert neo.NeoSubtitleMaster._detect_language_profile("episode.sub", raw)["suffix"] == "und"


def test_neo_v3_mislabeled_foreign_subtitle_does_not_block_or_pass_bilingual_auto_write(tmp_path):
    neo, _, _ = load_plugin_module("plugins.v3", "NeoSubtitleMaster")
    plugin = neo.NeoSubtitleMaster()
    plugin._auto_subtitle_language_priority = ["chi&eng"]
    plugin._auto_subtitle_language_strict = True
    path = tmp_path / "Show.S01E01.chi&eng.srt"
    path.write_text(_srt_text(KOREAN_LINES), encoding="utf-8")
    inventory = plugin.services.subtitle_inventory()
    subtitle = inventory._subtitle_file_metadata(path, "Show.S01E01")
    assert (subtitle["language_suffix"], subtitle["language_label"]) == ("kor", "韩文")
    language = plugin_submodule(neo, "matching.subtitle_language")
    target = {"id": "ep1", "basename": "Show.S01E01", "season": 1, "episode": 1, "subtitles": [subtitle]}
    assert not language.target_satisfies_auto_subtitle_policy(target, ["chi&eng"])
    prepared = [{"upload_id": "one", "source_name": path.name, "stored_path": str(path), "ext": ".srt"}]
    service = plugin.services.auto_transfer()
    assert service.select_auto_subtitle_items(prepared, [target]) == []
    assert plugin.services.online_ai().online_ai_candidate_items(prepared_uploads=prepared, targets=[target])[0]["language_suffix"] == "kor"
    path.write_bytes(b"uncertain")
    assert plugin.services.online_ai().online_ai_candidate_items(prepared_uploads=prepared, targets=[target]) == []
    path.write_text(_srt_text(CHINESE_LINES + ENGLISH_LINES), encoding="utf-8")
    assert service.select_auto_subtitle_items(prepared, [target])[0]["language_suffix"] == "chi&eng"


@pytest.mark.parametrize("extension", [".smi", ".ttml"])
def test_neo_v3_reads_timed_dialogue_from_structured_subtitles(extension):
    from test_subtitlemanualupload_text_formats import make_sami, make_ttml

    neo, _, _ = load_plugin_module("plugins.v3", "NeoSubtitleMaster")
    text = "這是一段只包含繁體中文的對白，請等我們做好準備。"
    raw = make_sami(text, "big5") if extension == ".smi" else make_ttml(text, "utf-16")
    assert neo.NeoSubtitleMaster._detect_language_profile(f"episode.eng{extension}", raw)["suffix"] == "cht"


def test_neo_v3_emby_fallback_respects_verified_local_subtitle_language(monkeypatch):
    neo, _, _ = load_plugin_module("plugins.v3", "NeoSubtitleMaster")
    plugin = neo.NeoSubtitleMaster()
    plugin._auto_subtitle_language_priority = ["chi&kr"]
    plugin._auto_subtitle_language_strict = True
    plugin._auto_skip_chinese_media_on_transfer = False
    name = "Show.S01E01.chi&kr.srt"
    entry = {"basename": "Show.S01E01"}
    target = {**entry, "subtitles": [{"name": name, "language_suffix": "kor"}]}
    inventory = plugin.services.subtitle_inventory()
    monkeypatch.setattr(inventory, "probe_embedded_subtitle_tracks", lambda entry: None)
    monkeypatch.setattr(plugin.services, "subtitle_inventory", lambda: inventory)
    emby = SimpleNamespace(subtitle_tracks_for_entry=lambda *args, **kwargs: ("ready", [
        {"IsExternal": True, "Codec": "subrip", "Language": "chi", "Path": "/media/" + name}
    ]))
    monkeypatch.setattr(plugin.services, "emby_refresh", lambda: emby)
    service = plugin.services.auto_transfer()
    monkeypatch.setattr(service, "_target_from_entry", lambda entry: target)

    assert service._check_emby_subtitles_for_task({"entry": entry}) is None
    target["subtitles"] = []
    assert service._check_emby_subtitles_for_task({"entry": entry})["status"] == "skipped"


def test_neo_v3_mislabeled_legacy_sidecar_does_not_conflict_with_new_filename(tmp_path):
    neo, _, _ = load_plugin_module("plugins.v3", "NeoSubtitleMaster")
    writer = neo.NeoSubtitleMaster().services.writer()
    video = tmp_path / "Show.S01E01.mkv"
    legacy = video.with_suffix(".chi&kr.srt")
    original = _srt_text(KOREAN_LINES).encode()
    legacy.write_bytes(original)
    source = tmp_path / "downloaded.srt"
    source.write_text(_srt_text(CHINESE_LINES + KOREAN_LINES), encoding="utf-8")
    operation = {
        "source_path": source, "destination_path": video.with_suffix(".中韩双语.zh-CN.srt"),
        "video_path": video, "target_entry": {"id": "ep1"}, "language_suffix": "chi&kr",
    }
    conflicts = {}
    assert writer._confirm_write_conflicts([operation], None, conflicts) == [operation]
    assert conflicts == {} and legacy.read_bytes() == original
    legacy.write_bytes(source.read_bytes())
    assert writer._confirm_write_conflicts([operation], None, conflicts) == []
    assert conflicts == {"ep1": legacy.name}


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


@pytest.mark.parametrize(
    ("page_html", "code"),
    [
        ("<li>在公众号里发送数字 <strong>023456</strong></li>", "023456"),
        (
            '<li hidden>在公众号里发送数字 123456</li>'
            '<li style="display: none">在公众号里发送数字 654321</li>'
            '<script>let copy = "在公众号里发送数字 777777";</script>'
            '<li>在公众号里发送数字 <strong></strong></li>',
            "",
        ),
    ],
)
def test_neo_v3_subhd_reports_wechat_verification_for_manual_download(monkeypatch, page_html, code):
    neo, _, _ = load_plugin_module("plugins.v3", "NeoSubtitleMaster")
    subhd = plugin_submodule(neo, "online_subtitles.providers.subhd")
    detail_url = "https://subhd.tv/a/example?t=private-value"
    down_url = "https://subhd.tv/down/example"
    submitted = []
    closed = []

    def get_text(url, *, referer):
        if url == detail_url:
            return 200, '<a href="/down/example">下载字幕文件</a>', url
        assert url == down_url
        return 200, page_html, url

    def post_json(url, payload, *, referer):
        submitted.append((url, payload, referer))
        raise subhd.OnlineJSONError(403, {"msg": "请先在公众号发送页面上的数字，再点击下载。"})

    def make_client(**options):
        assert options == {"use_proxy": False, "timeout": 5, "browser_recovery": False}
        return SimpleNamespace(get_text=get_text, post_json=post_json, close=lambda: closed.append(True))

    monkeypatch.setattr(subhd, "OnlinePageClient", make_client)
    provider = subhd.SubHDProvider(SimpleNamespace(bypass_url="https://proxy.example", timeout=5))
    with pytest.raises(ValueError, match="SubHD 需要微信公众号验证") as result:
        provider.download({"page_url": detail_url})

    message = str(result.value)
    assert "https://subhd.tv/a/example，" in message and "private-value" not in message
    assert "以浏览器显示的数字为准，验证后下载并上传" in message
    assert ("插件读取到的页面数字为" in message) == bool(code)
    if code:
        assert code in message
    assert submitted == [("https://subhd.tv/api/sub/down", {"sid": "example", "cap": ""}, down_url)]
    assert closed == [True]


def test_neo_wechat_browser_uses_rendered_digits_and_same_page(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, PropertyMock

    neo, _, _ = load_plugin_module("plugins.v3", "NeoSubtitleMaster")
    module = plugin_submodule(neo, "online_subtitles.subhd_wechat")
    plugin = neo.NeoSubtitleMaster()
    notices = []
    monkeypatch.setattr(plugin, "post_message", lambda **msg: notices.append(msg), raising=False)
    monkeypatch.setattr(sys.modules["app.schemas.types"], "NotificationType", SimpleNamespace(Plugin="plugin"), raising=False)
    context = MagicMock()
    detail, down = context.new_page.return_value, MagicMock()
    detail.url = "https://subhd.me/a/example"
    down.url = "https://subhd.me/down/example"
    type(context).pages = PropertyMock(side_effect=[[], [detail, down]] * 4)
    down.get_by_text.return_value.first.is_visible.return_value = False
    lines = down.locator.return_value.filter.return_value
    lines.all_inner_texts.side_effect = [["在公众号里发送数字 获取中…"], ["在公众号里发送数字 012345"], []]
    down.get_by_role.return_value.is_enabled.side_effect = [False, False, False, False, True]
    file = tmp_path / "example.srt"
    file.write_bytes(b"subtitle")
    download = down.expect_download.return_value.__enter__.return_value.value
    download.path.return_value = file
    download.suggested_filename = file.name
    context.storage_state.return_value = {"cookies": [], "origins": []}
    manager = plugin.services.subhd_wechat
    assert manager._download_page(context, detail.url, "subhd.me") == (file.name, b"subtitle")
    assert len(notices) == 1 and "012345" in notices[0]["text"]
    down.get_by_role.return_value.click.assert_called_once()
    down.get_by_text.return_value.first.is_visible.return_value = True
    with pytest.raises(ValueError, match="额外的人机验证"):
        manager._download_page(context, detail.url, "subhd.me")
    down.get_by_role.return_value.click.assert_called_once()
    assert len(notices) == 1
    down.get_by_role.return_value.is_enabled.side_effect = [False, True]
    assert manager._download_page(context, detail.url, "subhd.me", interactive=True) == (file.name, b"subtitle")
    assert len(notices) == 2 and "人工验证" in notices[-1]["title"]
    models = plugin_submodule(neo, "online_subtitles.models")
    with models.automatic_subtitle_download(), pytest.raises(ValueError, match="额外的人机验证"):
        manager._download_page(context, detail.url, "subhd.me", interactive=True)
    assert len(notices) == 2
    assert not models._automatic_download.get()


def test_neo_subhd_uses_configured_entry_and_shared_profile(tmp_path, monkeypatch):
    from contextlib import contextmanager

    neo, _, _ = load_plugin_module("plugins.v3", "NeoSubtitleMaster")
    browser = plugin_submodule(neo, "online_subtitles.subhd_wechat")
    plugin = neo.NeoSubtitleMaster()
    monkeypatch.setattr(plugin, "get_data_path", lambda: tmp_path)
    plugin._online_site_urls = {"subhd": "https://mirror.example"}
    profiles, urls, modes = [], [], []

    @contextmanager
    def open_profile(profile, interactive):
        profiles.append(profile)
        modes.append(interactive)
        yield object()

    def download(context, url, host, interactive):
        urls.append(url)
        assert host == "mirror.example"
        return "episode.srt", b"subtitle"

    monkeypatch.setattr(browser, "_browser", open_profile)
    monkeypatch.setattr(plugin.services.subhd_wechat, "_download_page", download)
    for sid in ("first", "second"):
        service = plugin.services.online_subtitles()
        result = service.download([{"provider": "subhd", "page_url": f"https://subhd.me/a/{sid}"}])
        assert result[0]["content"] == b"subtitle"
    assert urls == ["https://mirror.example/a/first", "https://mirror.example/a/second"]
    assert profiles == [tmp_path / "subhd_browser" / "profile"] * 2
    assert modes == [False, False]

    def challenge(context, url, host, interactive):
        if not interactive:
            raise browser._HumanPrompt("人工验证")
        return "episode.srt", b"subtitle"

    monkeypatch.setenv("DISPLAY", ":1")
    monkeypatch.setattr(plugin.services.subhd_wechat, "_download_page", challenge)
    service.download([{"provider": "subhd", "result_id": "third"}])
    assert modes[2:] == [False, True]
    assert profiles[2:] == profiles[:2]
