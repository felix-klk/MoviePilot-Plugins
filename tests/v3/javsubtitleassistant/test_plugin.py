import hashlib
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app.plugins


ROOT = Path(__file__).resolve().parents[3]
app.plugins.__path__.insert(0, str(ROOT / "plugins.v3"))

from app.plugins.javsubtitleassistant import JavSubtitleAssistant
from app.plugins.javsubtitleassistant.catalog.manual_strm import ManualStrmCatalog
from app.plugins.javsubtitleassistant.matching.subtitle_language import (
    auto_target_has_chinese_subtitle, required_auto_languages,
)
from app.plugins.javsubtitleassistant.online_subtitles import service
from app.plugins.javsubtitleassistant.online_subtitles.keyword_builder import build_search_keywords
from app.plugins.javsubtitleassistant.online_subtitles.providers import subtitlecat
from app.plugins.javsubtitleassistant.online_subtitles.throttle import SerialThrottle


def catalog():
    return ManualStrmCatalog(normalize_text=lambda value: str(value or "").strip(),
                             safe_int=lambda value, default=0: int(value) if str(value).isdigit() else default,
                             hash_text=lambda value: hashlib.sha256(value.encode()).hexdigest(),
                             extract_episode_hint=lambda _: {}, meta_info_path=None, logger_warning=lambda *_: None)


class JavForkTests(unittest.TestCase):
    def test_release_has_its_own_federated_frontend(self):
        index = json.loads((ROOT / "package.v3.json").read_bytes())
        plugin = JavSubtitleAssistant
        self.assertEqual(index[plugin.__name__]["version"], plugin.plugin_version)
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
            self.assertEqual((entry["jav_number"], entry["path"]), ("ABC-013", str(video)))
            self.assertIn("ABC-013", entry["title"])
            self.assertEqual(build_search_keywords({}, [entry], "auto"), ["ABC-013", "ABC013"])
            self.assertEqual(catalog().entry_for_path(video, [str(root)])["id"], entry["id"])

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
        self.assertEqual((result.title, result.identity_status), ("ABC-123-CD2", "strong"))

    def test_any_chinese_stops_auto_processing_even_with_bilingual_preference(self):
        inventory = SimpleNamespace(embedded_subtitle_tracks_for_target=lambda _: [{"language": "cht"}])
        self.assertTrue(auto_target_has_chinese_subtitle({}, {}, subtitle_inventory=inventory,
                                                        language_priority=["chi&eng"], strict=True))
        self.assertTrue(auto_target_has_chinese_subtitle({}, {"subtitles": [{"language_suffix": "chi"}]},
                                                        subtitle_inventory=inventory, language_priority=["chi&eng"]))
        self.assertIn("chi", required_auto_languages(["chi&eng"]))
        self.assertNotIn("eng", required_auto_languages(["eng"]))

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
