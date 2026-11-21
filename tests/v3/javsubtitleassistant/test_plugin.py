import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.plugins


PROJECT = Path(__file__).resolve().parents[3]
app.plugins.__path__.insert(0, str(PROJECT / "plugins.v3"))

from app.plugins import javsubtitleassistant as plugin
from app.plugins.javsubtitleassistant import media, subtitlecat


CHINESE = "1\n00:00:00,100 --> 00:00:01,000\nHello\n你好，這裡有中文字幕。\n".encode()
ENGLISH = b"1\n00:00:00,100 --> 00:00:01,000\nEnglish only\n"


class JavSubtitleAssistantTests(unittest.TestCase):
    def setUp(self):
        # These unit tests use the real SDK base without starting unrelated host services.
        chain = patch("app.sdk.plugin.base.PluginChain")
        chain.start()
        self.addCleanup(chain.stop)

    def test_market_identity_and_service_lifecycle(self):
        source = json.loads((PROJECT / "package.v3.json").read_text(encoding="utf-8"))
        instance = plugin.JavSubtitleAssistant()
        metadata = source[type(instance).__name__]
        self.assertEqual(metadata["name"], instance.plugin_name)
        self.assertEqual(metadata["version"], instance.plugin_version)
        self.assertEqual(next(iter(metadata["history"])), f"v{instance.plugin_version}")
        self.assertEqual(instance.get_service(), [])
        instance.init_plugin({"enabled": True, "folders": str(PROJECT)})
        self.assertEqual(instance.get_service()[0]["id"], "JavSubtitleAssistant.Scan")
        instance.stop_service()
        self.assertEqual(instance.get_service(), [])

    def test_jav_number_and_local_nfo(self):
        self.assertEqual(list(media.jav_numbers("4k FC2 PPV 1234567 CD1")), ["FC2-1234567"])
        self.assertEqual(list(media.jav_numbers("T28620.1080p")), ["T28-620"])
        self.assertEqual(list(media.jav_numbers("ABC012")), ["ABC-012"])
        self.assertEqual(list(media.jav_numbers("FC234")), ["FC-234"])
        self.assertEqual(list(media.jav_numbers("100323_01")), ["100323-01"])
        self.assertEqual(list(media.jav_numbers("HD.1080p.HEVC-265")), [])
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "organizer title.mkv"
            video.with_suffix(".nfo").write_text("<movie><num>ABC-013</num></movie>", encoding="utf-8")
            self.assertEqual(media.video_number(video), "ABC-013")
            video.with_suffix(".nfo").unlink()
            video.touch()
            (video.parent / "movie.nfo").write_text("<movie><num>ABC-014</num></movie>", encoding="utf-8")
            self.assertEqual(media.video_number(video), "ABC-014")
            (video.parent / "different movie.mkv").touch()
            self.assertEqual(media.video_number(video), "")

    def test_external_chinese_bilingual_and_japanese(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "ABC-001-CD1.mkv"
            (root / "ABC-001-CD2.zh-CN.srt").write_bytes(CHINESE)
            (root / "ABC-001-CD1.ja.srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n今日は映画を見ています。\n", encoding="utf-8",
            )
            other_version = root / "ABC-001-CD1.4K.mkv"
            other_version.touch()
            (root / "ABC-001-CD1.4K.zh-CN.srt").write_bytes(CHINESE)
            self.assertFalse(media.has_chinese_sidecar(video))
            self.assertTrue(media.has_chinese_sidecar(other_version))
            (root / "ABC-001-CD1-forced.srt").write_bytes(CHINESE)
            self.assertTrue(media.has_chinese_sidecar(video))

    def test_legacy_text_encodings_distinguish_japanese_from_chinese(self):
        traditional = "你好，請問你知道這是什麼嗎？".encode("big5")
        japanese = "これは日本語だけの字幕です。今日はいい天気です。".encode("cp932")
        self.assertTrue(media.chinese_text(media.decode_subtitle(traditional)))
        self.assertFalse(media.chinese_text(media.decode_subtitle(japanese)))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is unavailable")
    def test_real_embedded_subtitle_even_when_track_is_not_tagged_chinese(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "ABC-001.mkv"
            subtitle = root / "input.srt"
            for text, expected in ((ENGLISH, False), (CHINESE, True)):
                subtitle.write_bytes(text)
                subprocess.run(
                    ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
                     "-i", "color=black:s=16x16:r=1:d=2", "-i", str(subtitle),
                     "-map", "0:v", "-map", "1:s", "-c:v", "mpeg4", "-c:s", "srt",
                     "-metadata:s:s:0", "language=eng", str(video)],
                    capture_output=True, timeout=15, check=True,
                )
                self.assertEqual(media.has_chinese_embedded(video, "ffprobe", "ffmpeg"), expected)

    def test_subtitlecat_candidate_and_download_validation(self):
        site = subtitlecat.SITE
        detail = f"{site}/subs/10/ABC-123-CD2.html"
        pages = {
            f"{site}/index.php?search=ABC-123": b"""<table class="sub-table">
              <tr><td><a href="subs/12/ABC-1234-CD2.html">ABC-1234-CD2</a></td></tr>
              <tr><td><a href="subs/12/ABC-123-CD1.html">ABC-123-CD1</a></td></tr>
              <tr><td><a href="https://example.org/subs/12/ABC-123-CD2.html">ABC-123-CD2</a></td></tr>
              <tr><td><a href="subs/10/ABC-123-CD2.html">ABC-123-CD2</a> (translated from Japanese)</td></tr>
              </table>""",
            detail: b"""<a id="download_zh-CN" href="/subs/11/ABC-123-CD2-zh-CN.srt">Download</a>
                        <a id="download_zh-TW" href="/subs/11/ABC-123-CD2-zh-TW.srt">Download</a>""",
            f"{site}/subs/11/ABC-123-CD2-zh-CN.srt": b"<html>not a subtitle</html>",
            f"{site}/subs/11/ABC-123-CD2-zh-TW.srt": CHINESE,
        }
        provider = subtitlecat.SubtitleCatProvider()
        with patch.object(provider, "_fetch", side_effect=lambda url, *args: pages[url]):
            self.assertEqual(
                provider.search_and_download("ABC-123", Path("ABC-123-CD2.mkv")),
                ("zh-TW", CHINESE),
            )

    def test_scan_resumes_batch_and_does_not_download_for_existing_chinese(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(1, 4):
                (root / f"ABC-00{index}.strm").write_text("file:///unused/test.mkv", encoding="utf-8")
            chinese = root / "ABC-001.eng.srt"
            chinese.write_bytes(CHINESE)
            (root / "ABC-002.eng.srt").write_bytes(ENGLISH)
            state = {}
            instance = plugin.JavSubtitleAssistant()
            instance.MAX_SEARCHES = 1
            instance.init_plugin({"enabled": True, "folders": str(root)})
            with (patch.object(instance, "get_data", side_effect=state.get),
                  patch.object(instance, "save_data", side_effect=state.__setitem__),
                  patch.object(plugin.SubtitleCatProvider, "search_and_download", return_value=("zh-CN", CHINESE)) as search):
                service = instance.get_service()[0]
                service["func"]()
                service["func"]()
                state.clear()
                service["func"]()
                self.assertEqual([call.args[0] for call in search.call_args_list], ["ABC-002", "ABC-003"])
            self.assertEqual(chinese.read_bytes(), CHINESE)
            self.assertEqual((root / "ABC-002.eng.srt").read_bytes(), ENGLISH)
            self.assertEqual((root / "ABC-002.zh-CN.srt").read_bytes(), CHINESE)
            self.assertEqual((root / "ABC-003.zh-CN.srt").read_bytes(), CHINESE)
            self.assertFalse((root / "ABC-001.zh-CN.srt").exists())
            self.assertIsNone(media.write_subtitle(root / "ABC-002.strm", "zh-CN", b"replacement"))
            self.assertEqual((root / "ABC-002.zh-CN.srt").read_bytes(), CHINESE)

    def test_settings_register_immediate_scan_and_report_missing_directory(self):
        directory = Path(tempfile.gettempdir()) / "javsubtitle-test-unavailable-root"
        instance = plugin.JavSubtitleAssistant()
        state = {}
        with (patch.object(plugin.scheduler_sdk, "add_plugin_once_job", return_value=True) as add,
              patch.object(instance, "update_config") as config,
              patch.object(instance, "get_data", side_effect=state.get),
              patch.object(instance, "save_data", side_effect=state.__setitem__)):
            instance.init_plugin({"enabled": True, "folders": str(directory), "onlyonce": True})
            plugin_id, job, callback, _ = add.call_args.args
            self.assertEqual((plugin_id, job), ("JavSubtitleAssistant", "scan_once"))
            self.assertFalse(config.call_args.args[0]["onlyonce"])
            self.assertEqual(instance.get_page()[0]["props"]["title"], "扫描中")
            callback()
            self.assertEqual(state["last_scan"]["failed"], 1)
            self.assertEqual(instance.get_page()[1]["content"][0]["props"]["title"], str(directory))

    def test_manual_scan_retries_cached_misses(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "ABC-016.strm"
            video.touch()
            instance = plugin.JavSubtitleAssistant()
            instance.init_plugin({"enabled": True, "folders": directory})
            state = {}
            with (patch.object(instance, "get_data", side_effect=state.get),
                  patch.object(instance, "save_data", side_effect=state.__setitem__),
                  patch.object(plugin.SubtitleCatProvider, "search_and_download",
                               side_effect=[None, ("zh-CN", CHINESE)]) as search):
                instance.scan()
                instance.scan()
                self.assertEqual(search.call_count, 1)
                self.assertEqual(state["last_scan"]["cached"], 1)
                instance.scan_now()
            self.assertEqual(search.call_count, 2)
            self.assertEqual(state["last_scan"]["saved"], 1)


if __name__ == "__main__":
    unittest.main()
