from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable


_POLL_SECONDS = 5
_BATCH_SIZE = 20


class ManualStrmMonitor:
    """Maintain a baseline per directory and check changes after two matching observations."""

    def __init__(self, owner: Any, *, logger: Any, video_exts: Iterable[str]) -> None:
        self._owner = owner
        self._logger = logger
        self._video_exts = set(video_exts)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        roots = []
        for raw in self._owner._manual_strm_paths:
            path = Path(raw)
            if path.is_absolute():
                try:
                    roots.append(path.resolve())
                except (OSError, RuntimeError):
                    roots.append(path)
        self._configured_roots = tuple(dict.fromkeys(roots))
        self._baseline: dict[Path, dict[Path, tuple[int, ...]]] = {}
        self._observed: dict[Path, dict[Path, tuple[int, ...]]] = {}
        self._root_ids: dict[Path, tuple[int, int]] = {}
        self._offline: set[Path] = set()
        self._scan_lock = threading.Lock()
        self._scan_roots: dict[Path, set[Path]] = {}
        self._last_error = ""
        self._last_event_at = ""
        self._last_change_count = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not self._owner.get_state() or not self._owner._manual_strm_enabled:
            return
        roots = self._configured_roots
        if not roots:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, args=(roots,), name="SubtitleManualUploadDirectoryMonitor", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop_event.is_set())

    def status(self) -> dict[str, Any]:
        with self._scan_lock:
            scanning = bool(self._scan_roots) and self._running()
        return {
            "enabled": bool(self._owner._manual_strm_enabled),
            "running": self._running(),
            "roots": [str(root) for root in self._configured_roots],
            "offline_roots": sorted(str(root) for root in self._offline.copy()),
            "scanning": scanning,
            "last_error": self._last_error,
            "last_event_at": self._last_event_at,
            "last_change_count": self._last_change_count,
        }

    def request_scan(self) -> bool:
        with self._scan_lock:
            if not self._running() or self._scan_roots:
                return False
            self._scan_roots = {root: set() for root in self._configured_roots}
            return bool(self._scan_roots)

    @staticmethod
    def _root_id(root: Path) -> tuple[int, int]:
        stat = root.stat()
        return stat.st_dev, stat.st_ino

    def wait_reason(self, entry: dict[str, Any]) -> str:
        if not self._owner._manual_strm_enabled:
            return ""
        path = Path(str(entry.get("path") or ""))
        for root in self._configured_roots:
            if not path.is_relative_to(root):
                continue
            root_id = None
            try:
                root_id = self._root_id(root)
                if root in self._offline or self._root_ids.get(root) not in (None, root_id):
                    return "等待本地整理目录恢复"
                stat = path.stat()
                if not stat.st_size:
                    return "等待视频写入稳定"
            except FileNotFoundError:
                saved_id = tuple(entry.get("_directory_root_id") or ())
                if root_id is None or (
                    saved_id and saved_id != root_id and not self._observed.get(root)
                ):
                    return "等待本地整理目录恢复"
            except OSError:
                return "等待本地整理目录恢复"
            else:
                baseline = self._baseline.get(root, {})
                if path in self._observed.get(root, {}) and baseline.get(path) != self._signature(stat):
                    return "等待视频写入稳定"
            break
        return ""

    def _run(self, roots: tuple[Path, ...]) -> None:
        while not self._stop_event.is_set():
            for root in roots:
                if self._stop_event.is_set():
                    return
                try:
                    self._poll_root(root)
                except Exception as exc:
                    self._last_error = str(exc)
                    self._logger.warning("[SubtitleManualUpload] 检查本地整理目录失败 path=%s error=%s", root, exc)
            self._stop_event.wait(_POLL_SECONDS)

    @staticmethod
    def _signature(stat: os.stat_result) -> tuple[int, int, int]:
        return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def _snapshot(self, root: Path) -> dict[Path, tuple[int, ...]]:
        snapshot = {}
        stack = [root]
        junction = getattr(os.path, "isjunction", lambda _: False)
        while stack and not self._stop_event.is_set():
            directory = stack.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if not junction(entry.path):
                            stack.append(Path(entry.path))
                        continue
                    path = Path(entry.path)
                    if self._is_interesting(path) and entry.is_file(follow_symlinks=False):
                        try:
                            stat = entry.stat(follow_symlinks=False)
                            snapshot[path] = self._signature(stat)
                        except FileNotFoundError:
                            continue
        return snapshot

    def _is_interesting(self, path: Path) -> bool:
        ext = path.suffix.lower()
        return ext == ".nfo" or ext in self._video_exts or ext in self._owner._subtitle_exts

    def _poll_root(self, root: Path) -> None:
        try:
            root_id = self._root_id(root)
            current = self._snapshot(root)
            if self._root_id(root) != root_id or (
                not current and self._baseline.get(root) and self._root_ids[root] != root_id
            ):
                raise OSError("目录挂载已变化")
        except OSError:
            self._offline.add(root)
            self._observed.pop(root, None)
            return
        if self._stop_event.is_set():
            return
        videos = {path for path in current if path.suffix.lower() in self._video_exts and current[path][0]}
        catalog = self._owner.services.local_media_catalog()
        baseline = self._baseline.get(root)
        previous = self._observed.get(root)
        self._observed[root] = current
        if baseline is None:
            catalog.apply_manual_strm_changes(str(path) for path in videos)
            self._baseline[root] = current
            self._root_ids[root] = root_id
            self._offline.discard(root)
            return
        if previous is None:
            return
        stable_videos = {path for path in videos if current[path] == previous.get(path)}
        changes = {
            path for path in baseline.keys() | current.keys()
            if current.get(path) == previous.get(path) and current.get(path) != baseline.get(path)
            and (path not in videos or path in stable_videos)
        }
        self._root_ids[root] = root_id
        self._offline.discard(root)
        # Publish the stable observations before the queue worker can claim a task.
        if changes:
            updated = baseline.copy()
            for path in changes:
                if path in current:
                    updated[path] = current[path]
                else:
                    updated.pop(path, None)
            self._baseline[root] = updated
        try:
            self._dispatch(root, changes, videos, stable_videos, current)
        except Exception:
            self._baseline[root] = baseline
            raise

    def _dispatch(
        self, root: Path, changes: set[Path], videos: set[Path],
        stable_videos: set[Path], current: dict[Path, tuple[int, ...]],
    ) -> None:
        catalog = self._owner.services.local_media_catalog()
        candidates = set()
        if changes:
            catalog.apply_manual_strm_changes(str(path) for path in changes)
            candidates.update(changes & videos)
            removed = changes - current.keys()
            removed_videos = {path for path in removed if path.suffix.lower() in self._video_exts}
            candidates.update(self._videos_with_removed_subtitles(removed, videos | removed_videos))
            self._last_change_count = len(changes)
            self._last_event_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        if not self._owner._auto_search_on_manual_strm:
            candidates.clear()

        with self._scan_lock:
            scanned = self._scan_roots.get(root)
        if scanned is not None:
            candidates.update(stable_videos - scanned)
        paths = sorted(candidates & stable_videos)
        for offset in range(0, len(paths), _BATCH_SIZE):
            if self._stop_event.is_set():
                return
            batch = paths[offset:offset + _BATCH_SIZE]
            entries = catalog.manual_entries_for_paths(batch)
            for entry in entries:
                entry["_directory_root_id"] = self._root_ids[root]
            self._owner.services.auto_transfer().enqueue_missing_directory_entries(entries)
            if scanned is not None:
                scanned.update(batch)
        if scanned is not None and videos <= scanned:
            with self._scan_lock:
                self._scan_roots.pop(root, None)

    def _videos_with_removed_subtitles(self, removed: set[Path], videos: set[Path]) -> set[Path]:
        by_name: dict[tuple[Path, str], set[Path]] = {}
        subtitles = [path for path in removed if path.suffix.lower() in self._owner._subtitle_exts]
        affected_dirs = {path.parent for path in subtitles}
        for video in videos:
            if video.parent in affected_dirs:
                by_name.setdefault((video.parent, os.path.normcase(video.stem)), set()).add(video)
        affected = set()
        for path in subtitles:
            name = os.path.normcase(path.stem)
            while name:
                targets = by_name.get((path.parent, name))
                if targets:
                    affected.update(targets)
                    break
                name, separator, _ = name.rpartition(".")
                if not separator:
                    break
        return affected


__all__ = ["ManualStrmMonitor"]
