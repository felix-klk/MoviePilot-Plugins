from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_monitor(version: str):
    path = ROOT / version / "subtitlemanualupload/runtime/manual_strm_monitor.py"
    name = f"{version.replace('.', '_')}_manual_strm_monitor_test"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.ManualStrmMonitor


class FakeMetadata:
    def enrich_media(self, entry):
        return {**entry, "tmdb_id": 13, "poster_url": "https://image.tmdb.org/poster.jpg"}


class FakeQueue:
    def __init__(self):
        self.entries = []

    def enqueue_transfer_auto_entries(self, entries):
        self.entries.extend(entries)
        return len(entries), 0

    def auto_search_providers(self):
        return ["subhd"]


def make_owner(tmp_path: Path, strm: Path):
    queue = FakeQueue()
    services = types.SimpleNamespace(
        media_metadata=lambda: FakeMetadata(),
        auto_transfer=lambda: queue,
    )
    owner = types.SimpleNamespace(
        _manual_strm_enabled=True,
        _manual_strm_paths=[str(strm.parent)],
        _auto_search_on_manual_strm=True,
        _subtitle_exts={".srt", ".ass"},
        services=services,
        get_data_path=lambda: tmp_path / "data",
    )
    return owner, queue


def test_monitor_persists_signatures_and_only_requeues_changed_strm(tmp_path):
    for version in ("plugins.v2",):
        root = tmp_path / version
        root.mkdir(parents=True)
        strm = root / "Movie.strm"
        strm.write_text("remote-1", encoding="utf-8")
        owner, queue = make_owner(tmp_path / version, strm)
        monitor = load_monitor(version)(owner, logger=types.SimpleNamespace(info=lambda *args: None, warning=lambda *args: None))
        entry = {"id": "movie", "origin": "manual_strm", "path": str(strm), "media_type": "movie"}

        monitor._queue_changed_entries([entry])
        monitor._queue_changed_entries([entry])
        assert len(queue.entries) == 1
        assert queue.entries[0]["media_source"] == "themoviedb"
        assert queue.entries[0]["media_id"] == "13"

        strm.write_text("remote-2-longer", encoding="utf-8")
        monitor._queue_changed_entries([entry])
        assert len(queue.entries) == 2


def test_monitor_tracks_nfo_subtitle_and_deleted_directory_events(tmp_path):
    for version in ("plugins.v2",):
        owner, _ = make_owner(tmp_path / version, tmp_path / version / "Movie.strm")
        monitor = load_monitor(version)(owner, logger=types.SimpleNamespace(info=lambda *args: None, warning=lambda *args: None))
        deleted_dir = tmp_path / version / "deleted-directory"
        paths = monitor._interesting_paths(
            {
                (1, str(tmp_path / version / "Movie.nfo")),
                (1, str(tmp_path / version / "Movie.chi.srt")),
                (3, str(deleted_dir)),
            }
        )

        assert str(tmp_path / version / "Movie.nfo") in paths
        assert str(tmp_path / version / "Movie.chi.srt") in paths
        assert str(deleted_dir) in paths


def test_v3_directory_baseline_stable_copy_and_subtitle_removal(tmp_path, monkeypatch):
    root = tmp_path / "media"
    nested = root / "Season 01"
    nested.mkdir(parents=True)
    hd = nested / "Show.S01E01.mkv"
    four_k = nested / "Show.S01E01.2160p.mp4"
    subtitle = nested / "Show.S01E01.2160p.zh-CN.srt"
    hd.write_bytes(b"existing")
    entries = lambda paths: [{"path": str(path)} for path in paths]
    queued = []
    owner, _ = make_owner(tmp_path, hd)
    owner._manual_strm_paths = [str(root)]
    owner.services.local_media_catalog = lambda: types.SimpleNamespace(
        apply_manual_strm_changes=lambda paths: {
            "changed_entries": entries(
                Path(path) for path in paths if Path(path).suffix in {".mkv", ".mp4"}
                and Path(path).is_file()
            ),
        },
        manual_entries_for_paths=entries,
    )
    owner.services.auto_transfer = lambda: types.SimpleNamespace(
        enqueue_missing_directory_entries=lambda batch: queued.extend(item["path"] for item in batch)
    )
    monitor = load_monitor("plugins.v3")(
        owner, logger=types.SimpleNamespace(), video_exts={".strm", ".mkv", ".mp4"},
    )
    monkeypatch.setattr(monitor, "_running", lambda: True)

    monitor._poll_root(root)
    assert queued == []
    four_k.write_bytes(b"copy starts")
    monitor._poll_root(root)
    assert queued == []
    four_k.write_bytes(b"copy completed")
    monitor._poll_root(root)
    assert queued == []
    monitor._poll_root(root)
    assert queued == [str(four_k)]

    subtitle.write_text("written by queue", encoding="utf-8")
    monitor._poll_root(root)
    monitor._poll_root(root)
    assert queued == [str(four_k)]

    detached = root.with_name("detached")
    root.rename(detached)
    monitor._poll_root(root)
    assert monitor.wait_reason({"path": str(four_k)}) == "等待本地整理目录恢复"
    assert monitor.status()["offline_roots"] == [str(root)]
    assert monitor.request_scan()
    assert not monitor.request_scan()
    detached.rename(root)
    monitor._poll_root(root)
    assert queued == [str(four_k)]
    monitor._poll_root(root)
    assert set(queued[1:]) == {str(hd), str(four_k)}
    assert not monitor.status()["scanning"]
    assert not monitor.wait_reason({"path": str(four_k)})

    subtitle.unlink()
    monitor._poll_root(root)
    monitor._poll_root(root)
    assert queued.count(str(four_k)) == 3
    assert queued.count(str(hd)) == 1


def test_media_metadata_uses_moviepilot_match_cache_for_missing_identity():
    for version in ("plugins.v2", "plugins.v3"):
        catalog_dir = ROOT / version / "subtitlemanualupload/catalog"
        package_name = f"{version.replace('.', '_')}_metadata_testpkg"
        package = types.ModuleType(package_name)
        package.__path__ = [str(catalog_dir)]
        sys.modules[package_name] = package
        spec = importlib.util.spec_from_file_location(
            f"{package_name}.media_metadata",
            catalog_dir / "media_metadata.py",
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        calls = []

        class FakeChain:
            def match_tmdbinfo(self, **kwargs):
                calls.append(kwargs)
                return {
                    "media_source": "themoviedb",
                    "id": 13,
                    "poster_path": "https://image.tmdb.org/t/p/original/poster.jpg",
                    "title": "阿甘正传",
                    "year": "1994",
                }

            def tmdb_info(self, **kwargs):
                return None

        service = module.MediaMetadataService(
            tmdb_chain_factory=FakeChain,
            media_type_tv="tv",
            media_type_movie="movie",
            tmdb_detail_cache={},
            logger_warning=lambda *args: None,
            normalize_text=lambda value: str(value or "").strip(),
            safe_int=lambda value, default=0: int(value) if str(value or "").isdigit() else default,
            media_type_text_func=lambda value: str(value or ""),
            extract_title_aliases_func=lambda value: [],
            chinese_language_codes=set(),
            chinese_country_codes=set(),
            chinese_region_names=set(),
            chinese_category_pattern=types.SimpleNamespace(search=lambda value: None),
        )

        first = service.enrich_media({"media_type": "movie", "title": "阿甘正传", "year": "1994"})
        second = service.enrich_media({"media_type": "movie", "title": "阿甘正传", "year": "1994"})

        assert first["tmdb_id"] == 13
        assert first["poster_url"].endswith("poster.jpg")
        assert second["tmdb_id"] == 13
        assert len(calls) == 1
