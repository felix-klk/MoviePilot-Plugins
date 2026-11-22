import asyncio
from base64 import b64decode
from collections import OrderedDict
from datetime import datetime
import hashlib
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

import app.plugins
from app.sdk.media import MetaInfoPath


ROOT = Path(__file__).resolve().parents[3]
app.plugins.__path__.insert(0, str(ROOT / "plugins.v3"))

from app.plugins.javsubtitleassistant import JavSubtitleAssistant
from app.plugins.javsubtitleassistant.auto_transfer.auto_transfer_models import build_auto_transfer_task, public_auto_transfer_task
from app.plugins.javsubtitleassistant.catalog.local_media_catalog import LOCAL_CACHE_VERSION
from app.plugins.javsubtitleassistant.matching.subtitle_history import MATCH_HISTORY_CACHE_VERSION
from app.plugins.javsubtitleassistant.api.catalog_api import CatalogApi
from app.plugins.javsubtitleassistant.catalog.local_artwork import local_poster_thumbnail
from app.plugins.javsubtitleassistant.catalog.manual_strm import ManualStrmCatalog
from app.plugins.javsubtitleassistant.config.config_schema import normalize_plugin_config
from app.plugins.javsubtitleassistant.matching.subtitle_language import (
    auto_target_has_chinese_subtitle, required_auto_languages,
)
from app.plugins.javsubtitleassistant.online_subtitles import service
from app.plugins.javsubtitleassistant.online_subtitles.keyword_builder import build_search_keywords
from app.plugins.javsubtitleassistant.online_subtitles.matcher import rank_auto_download_candidates
from app.plugins.javsubtitleassistant.online_subtitles.providers import subtitlecat
from app.plugins.javsubtitleassistant.online_subtitles.throttle import SerialThrottle


def catalog(meta_info_path=None):
    return ManualStrmCatalog(normalize_text=lambda value: str(value or "").strip(),
                             safe_int=lambda value, default=0: int(value) if str(value).isdigit() else default,
                             hash_text=lambda value: hashlib.sha256(value.encode()).hexdigest(),
                             meta_info_path=meta_info_path, logger_warning=lambda *_: None)


class JavForkTests(unittest.TestCase):
    def test_release_has_its_own_federated_frontend(self):
        index = json.loads((ROOT / "package.v3.json").read_bytes())
        plugin = JavSubtitleAssistant
        self.assertEqual(index[plugin.__name__]["version"], plugin.plugin_version)
        self.assertEqual(plugin.plugin_author, index[plugin.__name__]["author"])
        self.assertEqual(plugin.author_url, index[plugin.__name__]["author_url"])
        self.assertEqual(plugin.plugin_config_prefix, "javsubtitleassistant_")
        self.assertEqual(plugin.get_render_mode(), ("vue", "dist/assets"))
        remote = ROOT / "plugins.v3/javsubtitleassistant/dist/assets/remoteEntry.js"
        source = remote.read_text(encoding="utf-8")
        for component in ("./Page", "./Config", "./AppPage"):
            self.assertIn(component, source)

    def test_scanner_uses_local_video_and_number_for_catalog_and_search(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            video = root / "rendered movie.mkv"
            video.touch()
            (root / "movie.nfo").write_text("<movie><title>Local title</title><num>ABC-013</num></movie>",
                                           encoding="utf-8")
            entry, = catalog().scan([str(root)])
            self.assertEqual((entry["jav_number"], Path(entry["path"])), ("ABC-013", video))
            self.assertIn("ABC-013", entry["title"])
            self.assertEqual(build_search_keywords({}, [entry], "auto"), ["ABC-013", "ABC013"])
            self.assertEqual(catalog().entry_for_path(video, [str(root)])["id"], entry["id"])

    def test_dal_number_stays_a_movie_with_moviepilot_filename_parser(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            video = root / "DAL-013.mp4"
            video.touch()
            scanner = catalog(lambda path: MetaInfoPath(path, force_video=True))
            entry, = scanner.scan([str(root)])
            self.assertEqual(
                (entry["jav_number"], entry["media_type"], entry["season"], entry["episode"], entry["target_label"]),
                ("DAL-013", "movie", 0, 0, video.name),
            )
            self.assertEqual(build_search_keywords({}, [entry], "movie"), ["DAL-013", "DAL013"])

    def test_legacy_s01e13_is_removed_from_saved_movie_entries_and_completed_queue_task(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            video = root / "DAL-013.mp4"
            video.touch()
            entry, = catalog().scan([str(root)])
            entry.update(season=1, episode=13, target_label=f"S01E13 · {video.name}")
            now = time.time()
            now_iso = datetime.fromtimestamp(now).isoformat()
            plugin = JavSubtitleAssistant.__new__(JavSubtitleAssistant)
            plugin._manual_strm_enabled = True
            plugin._manual_strm_paths = [str(root)]
            plugin._local_entries_cache = {}
            plugin._entry_map = OrderedDict()
            plugin._auto_transfer_tasks = OrderedDict()
            with patch.object(plugin, "get_data_path", return_value=root):
                local = plugin.services.local_media_catalog()
                local.local_cache_file().write_text(json.dumps({
                    "version": LOCAL_CACHE_VERSION, "complete": True, "loaded_at": now_iso, "entries": [entry],
                }), encoding="utf-8")
                self.assertTrue(local.restore_persisted_local_cache())
                restored_entry, = local.load_local_entries()
                self.assertEqual((restored_entry["season"], restored_entry["episode"], restored_entry["target_label"]),
                                 (0, 0, video.name))

                history = plugin.services.history()
                target = {key: entry[key] for key in ("id", "path", "jav_number", "media_type", "season", "episode")}
                target["label"] = entry["target_label"]
                history.match_history_cache_file().write_text(json.dumps({
                    "version": MATCH_HISTORY_CACHE_VERSION, "loaded_at": now_iso, "items": [{"targets": [target]}],
                }), encoding="utf-8")
                self.assertTrue(history.restore_persisted_match_history_cache())
                restored_target = plugin._match_history_cache["items"][0]["targets"][0]
                self.assertEqual((restored_target["season"], restored_target["episode"], restored_target["label"]),
                                 (0, 0, video.name))

                queue = plugin.services.auto_transfer()._queue
                task = build_auto_transfer_task(
                    task_id="previous-job", entry_key="previous-entry", group_key="previous-group",
                    entry=entry, now=now, safe_int=plugin._safe_int,
                )
                task.update(status="completed", active=False, result={"status": "written"})
                queue._state_file().write_text(json.dumps({"version": 1, "tasks": [task]}), encoding="utf-8")
                queue._load_locked()
                restored_task = plugin._auto_transfer_tasks["previous-job"]
                public = public_auto_transfer_task(restored_task, timestamp_iso=plugin._timestamp_iso)
                self.assertEqual((public["status"], public["target_label"], public["season"], public["episode"]),
                                 ("completed", video.name, 0, 0))
                self.assertEqual(public["result"], {"status": "written"})
                self.assertEqual(restored_task["entry"]["target_label"], video.name)

    def test_local_artwork_reaches_paginated_catalog_and_reads_replaced_images(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            movie = root / "ABC-013"
            movie.mkdir()
            video = movie / "ABC-013.mkv"
            video.touch()
            poster = movie / "poster.jpg"
            Image.new("RGB", (540, 810), "red").save(poster)
            entry, = catalog().scan([str(root)])
            plugin = JavSubtitleAssistant.__new__(JavSubtitleAssistant)
            plugin._manual_strm_paths = [str(root)]
            plugin._local_entries_cache = {"entries": [entry]}
            media_catalog = plugin.services.local_media_catalog()
            req = SimpleNamespace(query_params={"page": "1", "page_size": "1"})
            with (
                patch.object(plugin.services, "local_media_catalog", return_value=media_catalog),
                patch.object(plugin.services, "media_metadata", return_value=SimpleNamespace(enrich_media=dict)),
                patch.object(media_catalog, "load_local_entries", return_value=[entry]),
            ):
                data = asyncio.run(CatalogApi(plugin).search(req))["data"]
                item, = data["medias"]
                self.assertNotIn("_artwork_entries", item)
                first = item["poster_thumb_url"]
                self.assertTrue(first.startswith("data:image/jpeg;base64,"))
                image_bytes = b64decode(first.partition(",")[2])
                with Image.open(BytesIO(image_bytes)) as image:
                    self.assertEqual(image.size, (216, 324))
                Image.new("RGB", (520, 800), "blue").save(poster)
                second = asyncio.run(CatalogApi(plugin).search(req))["data"]["medias"][0]["poster_thumb_url"]
                self.assertNotEqual(first, second)

    def test_artwork_uses_numbered_files_in_shared_folder_and_rejects_parent_paths(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            folder = root / "library"
            folder.mkdir()
            video = folder / "ABC-013.mkv"
            video.touch()
            (folder / "ABC-014.mkv").touch()
            entries = [(str(video), "ABC-013")]
            Image.new("RGB", (30, 40), "blue").save(folder / "poster.jpg")
            Image.new("RGB", (30, 40), "red").save(root / "outside.png")
            nfo = video.with_suffix(".nfo")
            nfo.write_text('<movie><thumb aspect="poster">../outside.png</thumb></movie>', encoding="utf8")
            self.assertEqual(local_poster_thumbnail(entries, [str(folder)]), "")
            (folder / "ABC-013-poster.JPG").write_bytes((root / "outside.png").read_bytes())
            self.assertTrue(local_poster_thumbnail(entries, [str(folder)]).startswith("data:image/jpeg;base64,"))
            (folder / "ABC-013-poster.JPG").unlink()
            Image.new("RGB", (32, 42), "green").save(folder / "custom.webp")
            nfo.write_text('<movie><art><poster>custom.webp</poster></art></movie>', encoding="utf8")
            self.assertTrue(local_poster_thumbnail(entries, [str(folder)]).startswith("data:image/jpeg;base64,"))
            self.assertEqual(local_poster_thumbnail(entries, [str(root / "missing")]), "")

    def test_exact_number_and_part_from_subtitlecat(self):
        query = """<table class="sub-table">
            <tr><td><a href="/subs/1/ABC-1234-CD2.html">ABC-1234-CD2</a> (translated from Chinese)</td></tr>
            <tr><td><a href="/subs/1/ABC-123-CD1.html">ABC-123-CD1</a> (translated from Chinese)</td></tr>
            <tr><td><a href="/subs/1/ABC-123-CD2.html">ABC-123-CD2</a> (translated from Chinese)</td></tr>
            </table>"""
        client = SimpleNamespace(timeout=8, get_text=lambda url, **_: (200, query, url))
        provider = subtitlecat.SubtitleCatProvider(client)
        target = {"jav_number": "ABC-123", "basename": "ABC-123-CD2", "media_type": "movie", "title": "Local title"}
        with patch.object(subtitlecat, "subtitlecat_gate", SerialThrottle(0)):
            result, = provider.search_all(["ABC-123"], [target], "auto")
            unsplit_target = {**target, "basename": "ABC-123"}
            self.assertEqual(provider.search_all(["ABC-123"], [unsplit_target], "auto"), [])
            manual = provider.search_all(["ABC-123"], [unsplit_target], "movie")
            self.assertEqual({item.identity_status for item in manual}, {"weak"})
        self.assertEqual((result.title, result.identity_status), ("ABC-123-CD2", "strong"))

    def test_subtitlecat_jav_results_do_not_require_episodes(self):
        query = """<table class="sub-table">
            <tr><td><a href="subs/1803/MNGS-088-subtitlenexus-sakura-2606-en_3_0.html">MNGS-088-subtitlenexus-sakura-2606-en_3_0</a> (translated from English)</td></tr>
            <tr><td><a href="subs/1790/MNGS-088.zh-cn%28by%20transub%29.html">MNGS-088.zh-cn(by transub)</a> (translated from Chinese)</td></tr>
            <tr><td><a href="subs/1789/4k688.com%40MNGS-088.ja.whisperjav.html">4k688.com@MNGS-088.ja.whisperjav</a> (translated from Japanese)</td></tr>
            </table>"""
        client = SimpleNamespace(timeout=8, get_text=lambda url, **_: (200, query, url))
        provider = subtitlecat.SubtitleCatProvider(client)
        target = {"jav_number": "MNGS-088", "basename": "MNGS-088", "media_type": "movie"}
        with patch.object(subtitlecat, "subtitlecat_gate", SerialThrottle(0)):
            results = provider.search_all(["MNGS-088"], [target], "episode")
        self.assertEqual([item.language for item in results], ["English", "Chinese", "Japanese"])
        ranked = rank_auto_download_candidates([item.to_dict() for item in results], [target], ["chi", "cht"], strict=True)
        self.assertEqual([item["language"] for item in ranked], ["Chinese"])

    def test_only_subtitlecat_and_fixed_auto_policy_after_upgrade(self):
        config = normalize_plugin_config({"online_providers": ["subhd", "subdl"],
                                          "auto_subtitle_language_priority": ["eng"],
                                          "auto_transfer_subtitle_strategy": "ai_source_only",
                                          "auto_multi_subtitle_mode": "all",
                                          "timeline_vad_mode": "rms",
                                          "rar_dependency_mode": "mapped_binary"})
        self.assertEqual(config["online_providers"], ["subtitlecat"])
        self.assertEqual(config["auto_subtitle_language_priority"], ["chi", "cht"])
        self.assertTrue(config["auto_subtitle_language_strict"])
        self.assertEqual(config["auto_transfer_subtitle_strategy"], "online_source_only")
        self.assertEqual(config["auto_multi_subtitle_mode"], "best")
        self.assertEqual(config["timeline_vad_mode"], "rms")
        self.assertEqual(config["rar_dependency_mode"], "mapped_binary")
        online = service.OnlineSubtitleSearchService()
        try:
            links = online.manual_links(["MNGS-088"])
            self.assertEqual([item["provider"] for item in links], ["subtitlecat"])
            self.assertEqual(links[0]["links"][0]["url"], "https://www.subtitlecat.com/index.php?search=MNGS-088")
        finally:
            online.fetcher.close()

    def test_any_chinese_stops_auto_processing_even_with_bilingual_preference(self):
        inventory = SimpleNamespace(embedded_subtitle_tracks_for_target=lambda _: [{"language": "cht"}])
        self.assertTrue(auto_target_has_chinese_subtitle({}, {}, subtitle_inventory=inventory,
                                                        language_priority=["chi&eng"], strict=True))
        self.assertTrue(auto_target_has_chinese_subtitle({}, {"subtitles": [{"language_suffix": "chi"}]},
                                                        subtitle_inventory=inventory, language_priority=["chi&eng"]))
        self.assertIn("chi", required_auto_languages(["chi&eng"]))
        self.assertNotIn("eng", required_auto_languages(["eng"]))

    def test_only_a_trailing_c_marker_skips_before_probing(self):
        checked = []
        inventory = SimpleNamespace(embedded_subtitle_tracks_for_target=lambda entry: checked.append(entry) or [])
        for marked in (
            {"filename": "JUFE-626-破解-C.mp4"},
            {"path": "/library/JUFE-626.1080p-c.MKV"},
            {"basename": "JUFE-626.1080p-c"},
        ):
            with self.subTest(marked=marked):
                self.assertTrue(auto_target_has_chinese_subtitle(marked, {}, subtitle_inventory=inventory))
                self.assertTrue(auto_target_has_chinese_subtitle({}, marked, subtitle_inventory=inventory))
        self.assertEqual(checked, [])
        for name in (
            "JUFE-626-破解-C-1080p.mp4",
            "JUFE-626-破解-C2.mp4",
            "/library-C/JUFE-626.mp4",
            "JUFE-626.mp4",
        ):
            with self.subTest(name=name):
                self.assertFalse(auto_target_has_chinese_subtitle({}, {"path": name}, subtitle_inventory=inventory))
        self.assertEqual(len(checked), 4)

    def test_marked_video_skips_auto_search_and_optional_emby_check(self):
        entry = {"filename": "JUFE-626-破解-C.mp4", "path": "/local/JUFE-626-破解-C.mp4"}
        plugin = JavSubtitleAssistant.__new__(JavSubtitleAssistant)
        runner = plugin.services.auto_transfer()
        with (
            patch.object(runner, "_target_from_entry", return_value={"path": entry["path"]}),
            patch.object(plugin.services, "online_subtitles", side_effect=AssertionError("unexpected online search")),
            patch.object(plugin.services, "emby_refresh", side_effect=AssertionError("unexpected Emby access")),
        ):
            self.assertEqual(runner.auto_process_transfer_entry(entry)["status"], "skipped")
            self.assertEqual(runner._check_emby_subtitles_for_task({"entry": entry})["status"], "skipped")

    def test_shared_download_gate_serializes_all_callers_and_waits_after_finish(self):
        events = []
        def download(result, **_):
            events.append(("start", time.monotonic()))
            time.sleep(.02)
            events.append(("end", time.monotonic()))
            return "test.srt", b"content"

        original = service.OnlineSubtitleSearchService.__new__(service.OnlineSubtitleSearchService)
        original.providers = {"subtitlecat": SimpleNamespace(download=download)}
        original.fetcher = SimpleNamespace(close=lambda: None)
        with patch.object(service, "download_gate", SerialThrottle(.04)), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(original.download, [{"provider": "subtitlecat"}]) for _ in range(2)]
            for future in futures:
                future.result(timeout=5)
        self.assertEqual([event for event, _ in events], ["start", "end", "start", "end"])
        self.assertGreaterEqual(events[2][1] - events[1][1], .035)

    def test_site_backoff_prevents_requests_during_cooldown(self):
        gate = SerialThrottle(0)
        with gate.slot():
            gate.back_off(429, "1200")
        with self.assertRaisesRegex(ValueError, "暂停请求"):
            with gate.slot():
                self.fail("a cooldown request was admitted")


if __name__ == "__main__":
    unittest.main()
