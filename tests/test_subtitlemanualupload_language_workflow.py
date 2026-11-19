from __future__ import annotations

import hashlib
import io
import types
import zipfile
from collections import OrderedDict
from pathlib import Path
from unittest.mock import Mock

import pytest

from test_subtitlemanualupload_sup import StubHttpException, make_bitmap_writer, make_upload_service, sup_modules  # noqa: F401


@pytest.fixture
def workflow(sup_modules, tmp_path):
    modules = sup_modules
    writer, owner, _ = make_bitmap_writer(modules, tmp_path)
    owner._traditional_to_simplified = False
    owner._auto_subtitle_language_strict = True
    owner._auto_subtitle_language_priority = ["chi&eng"]
    owner._auto_subtitle_format_priority = [".ass", ".srt"]
    owner._default_auto_language_priority = modules.language.DEFAULT_AUTO_LANGUAGE_PRIORITY
    owner._default_auto_format_priority = modules.language.DEFAULT_AUTO_FORMAT_PRIORITY
    owner._auto_multi_subtitle_mode = "best"
    owner._auto_transfer_subtitle_strategy = "online_then_ai_source"
    owner._auto_skip_chinese_media_on_transfer = False
    owner._subtitle_exts = modules.formats.SUBTITLE_EXTS
    owner._stream_exts = {".strm"}
    owner._timeline_allow_risky_offset = False
    owner._timeline_max_offset_seconds = 120
    owner._timeline_min_offset_seconds = 0.2
    owner._timeline_vad_mode = "webrtc"
    owner._set_timeline_task = Mock()
    owner._timeline_result_blocks_auto_write = lambda result: False
    owner._timeline_rejection_message = lambda result: ""
    owner._target_from_entry = lambda entry: {"label": entry.get("label", "")}
    owner._remove_ext_marks = Mock()
    owner._invalidate_match_history_cache = Mock()
    owner._subtitle_inventory = lambda: types.SimpleNamespace(invalidate_directory=Mock())
    owner._auto_search_min_score = 20
    owner._auto_season_package_cache = OrderedDict()
    owner._safe_int = lambda value, default=0: int(value or default)
    owner._hash_text = lambda value: hashlib.sha256(value.encode()).hexdigest()
    owner.get_data_path = lambda: tmp_path
    owner._apply_tmdb_detail = Mock()
    owner._extract_episode_hint = modules.load("catalog.target_normalizers").extract_episode_hint
    owner._normalize_auto_transfer_subtitle_strategy = modules.load("config.config_schema").normalize_auto_transfer_subtitle_strategy
    def timeline_fix(*, video_path, subtitle_path, output_path, **kwargs):
        import shutil
        shutil.copyfile(subtitle_path, output_path)
        return modules.timeline.TimelineFixResult(
            enabled=True, applied=False, reason="test timeline unchanged", base="audio", offset_seconds=0, scale_factor=1, score=1,
        )
    owner._run_timeline_fix = timeline_fix
    owner._subtitle_writer = lambda: writer
    upload = make_upload_service(modules, tmp_path)
    owner.services.upload_session = lambda: upload
    ai = Mock(side_effect=AssertionError("strict language policy must not submit AI"))
    language = modules.language
    detect = lambda name, raw: language.detect_language_profile(name, raw, modules.formats.SUBTITLE_EXTS)
    inventory = types.SimpleNamespace(embedded_subtitle_tracks_for_target=lambda entry: [])
    has_subtitle = lambda entry, target: language.auto_target_has_chinese_subtitle(
        entry, target, subtitle_inventory=inventory,
        language_priority=owner._auto_subtitle_language_priority, strict=owner._auto_subtitle_language_strict,
    )
    write_module = modules.load("auto_transfer.auto_transfer_write")
    strategy = write_module.AutoTransferWriteStrategy(owner, collaborators=write_module.AutoTransferWriteCollaborators(
        target_from_entry=lambda entry: entry,
        detect_language_profile=detect,
        write_operations_to_disk=writer.write_operations_to_disk,
        prepare_online_ai_subtitle_overrides=ai,
        submit_autosub_for_entries=ai,
        select_subtitle_items=lambda *args: strategy.select_subtitle_items(*args),
    ))
    online = Mock()
    processor_module = modules.load("auto_transfer.auto_transfer_processor")
    processor = processor_module.AutoTransferProcessor(owner, logger=Mock(), http_exception=ValueError,
        collaborators=processor_module.AutoTransferProcessorCollaborators(
            target_from_entry=lambda entry: entry,
            auto_target_has_chinese_subtitle=has_subtitle,
            chinese_transfer_media_evidence=lambda entry: (False, ""),
            media_for_transfer_entry=lambda entry: entry,
            search_providers=lambda: ["assrt"],
            search_keywords_for_entry=lambda *args: ["Show"],
            check_online_rate_limit=Mock(), wait_online_rate_limit=Mock(),
            online_service=lambda: online, online_download_name=lambda name, *_: name,
            extract_subtitle_files=upload.extract_subtitle_files,
            write_prepared_uploads_for_entries=strategy.write_prepared_uploads_for_entries,
            submit_autosub_for_entries=ai,
            call_search_write_subtitle=lambda *args, **kwargs: processor.search_write_subtitle(*args, **kwargs),
            submit_ai_for_entry=ai,
        ))
    season_module = modules.load("auto_transfer.auto_transfer_season")
    season = season_module.AutoTransferSeasonCache(owner, logger=Mock(),
        collaborators=season_module.AutoTransferSeasonCollaborators(
            target_from_entry=lambda entry: entry,
            auto_target_has_chinese_subtitle=has_subtitle,
            chinese_transfer_media_evidence=lambda entry: (False, ""),
            media_for_transfer_entry=lambda entry: entry,
            task_result_key=lambda entry: entry["id"],
            search_providers=lambda: ["assrt"], wait_online_rate_limit=Mock(),
            online_service=lambda: online, online_download_name=lambda name, *_: name,
            extract_subtitle_files=upload.extract_subtitle_files,
            write_prepared_uploads_for_entries=strategy.write_prepared_uploads_for_entries,
            store_cache=lambda *args: season.store_cache(*args),
            load_cache=lambda *args: season.load_cache(*args),
            write_from_cache=lambda *args: season.write_from_cache(*args),
            search_write_package=lambda *args, **kwargs: season.search_write_package(*args, **kwargs),
            process_entry=processor.process_entry,
        ))
    return types.SimpleNamespace(owner=owner, processor=processor, season=season, upload=upload, online=online, ai=ai, modules=modules)


def entry_for(tmp_path, episode=1):
    path = tmp_path / f"Show.S01E{episode:02d}.mkv"
    path.touch()
    return {"id": f"e{episode}", "path": str(path), "basename": path.stem, "filename": path.name,
            "label": path.stem, "title": "Show", "year": "2024", "media_key": "show-key",
            "media_type": "tv", "season": 1, "episode": episode, "storage": "local"}


def subtitle_zip(episodes=(1,), languages=("chi.jp", "chi.eng")):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for episode in episodes:
            for language in languages:
                archive.writestr(f"Show.S01E{episode:02d}.{language}.srt", "1\n00:00:01,000 --> 00:00:02,000\n这是字幕。\nHello there.\n")
    return output.getvalue()


def serve_zip(workflow, content, titles=("Show 双语",)):
    workflow.online.search.return_value = {"results": [
        {"provider": "assrt", "result_id": str(index), "title": title, "score": 95-index, "downloadable": True}
        for index, title in enumerate(titles)
    ]}
    workflow.online.download.side_effect = lambda selected: [{"provider": "assrt", "source_name": "Show.zip", "content": content, "result": selected[0]}]


@pytest.mark.parametrize("sup_modules", ["plugins.v3"], indirect=True)
@pytest.mark.parametrize("is_season", [False, True])
def test_v3_attempts_other_site_within_download_budget(workflow, tmp_path, is_season):
    episodes = (1, 2) if is_season else (1,)
    entries = [entry_for(tmp_path, episode) for episode in episodes]
    title = "Show.S01" if is_season else "Show.S01E01"
    candidate = {"title": title, "score": 80, "language": "简英双语", "identity_status": "strong"}
    workflow.online.search.return_value = {"results": [
        {**candidate, "provider": "subhd", "result_id": f"subhd-{index}"} for index in range(6)
    ] + [{**candidate, "provider": "assrt", "result_id": "backup"}]}

    def download(selected):
        result = selected[0]
        if result["result_id"] == "subhd-0":
            raise ValueError("download failed")
        languages = ("chi.eng",) if result["result_id"] == "backup" else ("chi",)
        return [{
            "provider": result["provider"], "source_name": "Show.zip",
            "content": subtitle_zip(episodes=episodes, languages=languages), "result": result,
        }]

    workflow.online.download.side_effect = download
    result = workflow.season.search_write_package(entries) if is_season else workflow.processor.process_entry(entries[0])
    assert result["status"] == "written", result
    tried = [call.args[0][0]["result_id"] for call in workflow.online.download.call_args_list]
    subhd_limit = 2 if is_season else 4
    assert tried == [*(f"subhd-{index}" for index in range(subhd_limit)), "backup"]
    assert len(list(tmp_path.glob("*.中英双语.zh-CN.srt"))) == len(episodes)
    workflow.ai.assert_not_called()


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("mode", ["best", "chinese_all", "all"])
def test_selected_bilingual_zip_writes_only_required_pair(workflow, tmp_path, mode, strict):
    workflow.owner._auto_subtitle_language_strict = strict
    workflow.owner._auto_subtitle_language_priority = ["chi&eng", "chi"]
    workflow.owner._auto_multi_subtitle_mode = mode
    entry = entry_for(tmp_path)
    entry["subtitles"] = [{"language_suffix": "chi"}]
    serve_zip(workflow, subtitle_zip(languages=("chi.jp", "chi", "chi.eng")), ("Show 中日双语", "Show 双语"))
    result = workflow.processor.process_entry(entry)
    assert result["status"] == "written", result
    tag = "中英双语.zh-CN" if workflow.modules.generation == "plugins.v3" else "chi&eng"
    filename = f"Show.S01E01.{tag}.srt"
    assert [path.name for path in tmp_path.glob("*.srt")] == [filename]
    assert "Hello there." in (tmp_path / filename).read_text(encoding="utf-8")
    assert workflow.online.download.call_args.args[0][0]["title"] == "Show 双语"
    workflow.ai.assert_not_called()


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("strategy", ["online_then_ai_source", "online_source_only", "ai_source_only"])
def test_required_pair_no_match_never_writes_or_submits_ai(workflow, tmp_path, strategy, strict):
    workflow.owner._auto_subtitle_language_strict = strict
    workflow.owner._auto_subtitle_language_priority = ["chi&eng", "chi"]
    workflow.owner._auto_transfer_subtitle_strategy = strategy
    serve_zip(workflow, subtitle_zip(languages=("chi.jp", "chi")))
    result = workflow.processor.process_entry(entry_for(tmp_path))
    assert result["status"] == "skipped"
    assert list(tmp_path.glob("*.srt")) == []
    workflow.ai.assert_not_called()
    if strategy == "ai_source_only":
        workflow.online.search.assert_not_called()
    else:
        assert "语言要求" in result["reason"]


@pytest.mark.parametrize("strict", [False, True])
def test_existing_other_pair_does_not_skip_selected_pair(workflow, tmp_path, strict):
    workflow.owner._auto_subtitle_language_strict = strict
    entry = entry_for(tmp_path)
    entry["subtitles"] = [{"language_suffix": "chi&jp"}]
    serve_zip(workflow, subtitle_zip())
    assert workflow.processor.process_entry(entry)["status"] == "written"
    workflow.online.search.assert_called_once()
    entry["subtitles"].append({"language_suffix": "chi&eng"})
    assert workflow.processor.process_entry(entry)["reason"] == "目标已有符合语言要求的字幕"
    workflow.online.search.assert_called_once()


@pytest.mark.parametrize("strict", [False, True])
def test_season_cache_rechecks_language_after_restart_and_preference_change(workflow, tmp_path, strict):
    workflow.owner._auto_subtitle_language_strict = strict
    entries = [entry_for(tmp_path, 1), entry_for(tmp_path, 2)]
    serve_zip(workflow, subtitle_zip(episodes=(1, 2)))
    workflow.owner._auto_subtitle_language_priority = ["chi&jp"]
    initial = workflow.season.search_write_package(entries)
    assert initial["status"] == "written", initial
    assert initial["coverage_complete"] is True
    assert len(list(tmp_path.glob("*.chi&jp.srt"))) == 2
    workflow.owner._auto_season_package_cache.clear()
    workflow.owner._auto_subtitle_language_priority = ["chi&eng"]
    cached = workflow.season.write_from_cache(entries)
    assert cached["from_cache"] is True
    tag = "中英双语.zh-CN" if workflow.modules.generation == "plugins.v3" else "chi&eng"
    assert len(list(tmp_path.glob(f"*.{tag}.srt"))) == 2
    workflow.owner._auto_subtitle_language_priority = ["chi&kr"]
    unmatched = workflow.season.write_from_cache(entries)
    assert unmatched["status"] == "skipped"
    assert list(tmp_path.glob("*.chi&kr.srt")) == []
    workflow.online.download.assert_called_once()
    workflow.ai.assert_not_called()


def test_language_config_round_trip_and_empty_validation(sup_modules):
    schema = sup_modules.load("config.config_schema")
    runtime = sup_modules.load("config.config_runtime")
    for priority in (["bilingual", "chi", "cht", "eng"], ["chi&eng"], ["jpn"], ["kor"]):
        config = schema.normalize_plugin_config({"auto_subtitle_language_priority": priority, "auto_subtitle_language_strict": True})
        owner = types.SimpleNamespace()
        runtime.apply_runtime_config(owner, config)
        saved = runtime.build_save_config_payload(owner)
        reloaded = schema.normalize_plugin_config(saved)
        assert reloaded["auto_subtitle_language_priority"] == config["auto_subtitle_language_priority"]
        assert reloaded["auto_subtitle_language_strict"] is True
    with pytest.raises(ValueError, match="至少选择一种语言"):
        schema.normalize_plugin_config({"auto_subtitle_language_priority": [], "auto_subtitle_language_strict": True})


@pytest.mark.parametrize("sup_modules", ["plugins.v3"], indirect=True)
def test_v3_legacy_ranking_switch_round_trips(sup_modules):
    schema = sup_modules.load("config.config_schema")
    runtime = sup_modules.load("config.config_runtime")
    assert schema.normalize_plugin_config({})["auto_subtitle_legacy_ranking"] is False
    owner = types.SimpleNamespace()
    runtime.apply_runtime_config(owner, schema.normalize_plugin_config({"auto_subtitle_legacy_ranking": True}))
    saved = runtime.build_save_config_payload(owner)
    assert schema.normalize_plugin_config(saved)["auto_subtitle_legacy_ranking"] is True


@pytest.mark.parametrize("sup_modules", ["plugins.v3"], indirect=True)
@pytest.mark.parametrize(("language", "default", "tag", "detected"), [
    ("chs", False, "zh-CN", "chi"),
    ("cht", False, "zh-TW", "cht"),
    ("eng&chi", True, "中英双语.zh-CN.default", "chi&eng"),
    ("eng&cht", False, "繁英双语.zh-TW", "cht&eng"),
    ("chi", True, "zh-CN.default", "chi"),
])
def test_emby_subtitle_write_preserves_original_format_and_existing_flags(sup_modules, tmp_path, language, default, tag, detected):
    writer, owner, _ = make_bitmap_writer(sup_modules, tmp_path)
    del owner._remove_ext_marks
    owner._traditional_to_simplified = False
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    entry = entry_for(media_dir)
    source = tmp_path / "source.ass"
    source.write_text("[Script Info]\nTitle: sample", encoding="utf-8")
    old = {media_dir / f"Show.S01E01.eng.{flag}.ass": flag.encode() for flag in ("default", "forced")}
    for path, content in old.items():
        path.write_bytes(content)

    upload = {"upload_id": "one", "stored_path": str(source), "source_name": source.name, "ext": ".ass"}
    item = {"upload_id": "one", "target_id": entry["id"], "language_suffix": language, "default_subtitle": default}
    operations = writer.build_write_operations([item], {"one": upload}, {entry["id"]: entry})
    written, _, _ = writer.write_operations_to_disk(session_dir=tmp_path, operations=operations)

    output = media_dir / f"Show.S01E01.{tag}.ass"
    assert written[0]["output_name"] == output.name
    assert output.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert sup_modules.language.language_suffix_from_filename(output.name, {".ass"})["suffix"] == detected
    assert {path: path.read_bytes() for path in old} == old


@pytest.mark.parametrize("sup_modules", ["plugins.v3"], indirect=True)
@pytest.mark.parametrize("overwrite", [False, True])
@pytest.mark.parametrize(("language", "output_tag", "legacy_tag"), [
    ("chi", "zh-CN", "chi.default"),
    ("chi&eng", "中英双语.zh-CN", "中英双语.default"),
    ("chi&eng", "中英双语.zh-CN", "eng&chi"),
])
def test_subtitle_conflicts_require_confirmation_and_keep_variants(sup_modules, tmp_path, language, output_tag, legacy_tag, overwrite):
    writer, owner, _ = make_bitmap_writer(sup_modules, tmp_path)
    owner._traditional_to_simplified = False
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    targets = {entry["id"]: entry for entry in (entry_for(media_dir, 1), entry_for(media_dir, 2))}
    legacy = media_dir / f"Show.S01E02.{legacy_tag}.srt"
    legacy.write_bytes(b"old variant")
    exact = media_dir / f"Show.S01E01.{output_tag}.srt"
    exact.write_bytes(b"same name")
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    source = session_dir / "source.srt"
    source.write_bytes(b"new subtitle")
    upload = {"upload_id": "one", "stored_path": str(source), "source_name": source.name, "ext": ".srt"}
    items = [{"upload_id": "one", "target_id": target_id, "language_suffix": language} for target_id in targets]
    operations = writer.build_write_operations(items, {"one": upload}, targets)
    with pytest.raises(StubHttpException, match="自动写入已跳过"):
        writer.write_operations_to_disk(session_dir=session_dir, operations=operations)
    writer._session_loader = lambda _: (session_dir, {"targets": list(targets.values()), "uploads": [upload]})
    apply_args = {"session_id": "test-session", "items": items, "locked_skipped": []}
    review, _ = writer.apply_upload_session(**apply_args)
    same, other = review["write_conflict"]["rows"]
    assert same["same_names"] == [exact.name]
    new = media_dir / f"Show.S01E02.{output_tag}.srt"
    assert other["other_names"] == [legacy.name]
    assert other["destinations"] == [new.name]
    assert exact.read_bytes() == b"same name"
    assert not new.exists()
    assert source.exists()

    exact.write_bytes(b"changed after the first confirmation")
    choices = {
        same["id"]: {"action": "overwrite", "token": same["token"]},
        other["id"]: {"action": "save", "token": other["token"]},
    }
    stale, _ = writer.apply_upload_session(**apply_args, conflict_choices=choices)
    refreshed = stale["write_conflict"]["rows"][0]
    assert refreshed["token"] != same["token"]
    assert not new.exists()

    choices[same["id"]] = {"action": "overwrite" if overwrite else "skip", "token": refreshed["token"]}
    response, _ = writer.apply_upload_session(**apply_args, conflict_choices=choices)
    written = response["written"]
    assert {item["output_name"] for item in written} == ({exact.name, new.name} if overwrite else {new.name})
    assert new.read_bytes() == b"new subtitle"
    if overwrite:
        assert exact.read_bytes() == b"new subtitle"
        assert Path(written[0]["backup_path"]).read_bytes() == b"changed after the first confirmation"
    else:
        assert exact.read_bytes() == b"changed after the first confirmation"
        assert response["skipped"] == [{"target_id": "e1", "reason": "已选择跳过此字幕"}]
    assert legacy.read_bytes() == b"old variant"


def test_embedded_track_pair_title_is_exact_and_requires_explicit_pair_name(sup_modules):
    module = sup_modules.load("catalog.subtitle_inventory")
    language = sup_modules.language
    inventory = module.SubtitleInventory(
        subtitle_exts={".srt"}, stream_exts={".strm"}, embedded_text_codecs={"ass"},
        embedded_image_codecs={"pgs"}, embedded_probe_cache=OrderedDict(), embedded_probe_cache_max_size=5,
        trust_transfer_history_paths=False, normalize_text=language.normalize_text,
        normalize_language_suffix=language.normalize_language_suffix,
        detect_language_profile=lambda name, raw: language.detect_language_profile(name, raw, {".srt"}),
        is_chinese_language_suffix=language.is_chinese_language_suffix, safe_int=lambda value, default=0: default,
        subtitle_backup_path=lambda path: path, subprocess_module=types.SimpleNamespace(),
        logger_warning=lambda *args, **kwargs: None,
    )
    assert inventory.embedded_subtitle_language_suffix("chi", "中英双语") == "chi&eng"
    assert inventory.embedded_subtitle_language_suffix("eng", "English subtitles for Chinese dialogue") == "eng"
