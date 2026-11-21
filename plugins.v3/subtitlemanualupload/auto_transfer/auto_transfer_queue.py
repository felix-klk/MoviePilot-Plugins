from __future__ import annotations

import json
from collections import OrderedDict
from math import isfinite
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .auto_transfer_models import (
    AUTO_TRANSFER_ACTIVE_STATUSES,
    AUTO_TRANSFER_PUBLIC_STATUSES,
    build_auto_transfer_task,
    is_auto_transfer_task_active,
    is_bilingual_followup_waiting,
    public_auto_transfer_task,
)

_HISTORY_TTL_SECONDS = 3 * 24 * 60 * 60
_BILINGUAL_RETRY_DELAYS = (3600, 6 * 3600, 24 * 3600)


class AutoTransferQueue:
    def __init__(
        self,
        owner: Any,
        *,
        time_module: Any,
        logger: Any,
        ensure_worker: Optional[Callable[[], None]] = None,
        rate_status: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        self._owner = owner
        self._time = time_module
        self._logger = logger
        self._ensure_worker = ensure_worker
        self._rate_status = rate_status or (lambda: {})

    def _state_file(self) -> Path:
        return self._owner.get_data_path() / "auto_transfer_queue.json"

    def _save_locked(self) -> None:
        try:
            path = self._state_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_suffix(".tmp")
            snapshot = {"version": 1, "tasks": list(self._owner._auto_transfer_tasks.values())}
            temp_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            temp_path.replace(path)
        except (OSError, TypeError, ValueError) as exc:
            self._logger.warning("[SubtitleManualUpload] 保存自动入库队列失败: %s", exc)

    def _load_locked(self) -> None:
        try:
            payload = json.loads(self._state_file().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            self._logger.warning("[SubtitleManualUpload] 读取自动入库队列失败: %s", exc)
            return
        if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("tasks"), list):
            self._logger.warning("[SubtitleManualUpload] 自动入库队列存档格式无效")
            return
        for task in payload["tasks"]:
            if not isinstance(task, dict):
                continue
            task_id = task.get("id")
            status = task.get("status")
            if (not isinstance(task_id, str) or not task_id or not isinstance(status, str)
                    or status not in AUTO_TRANSFER_PUBLIC_STATUSES or not isinstance(task.get("entry"), dict)):
                continue
            try:
                timestamps_valid = all(
                    type(task.get(field)) in {int, float} and isfinite(task[field])
                    for field in ("created_ts", "updated_ts", "next_run_ts")
                )
            except OverflowError:
                timestamps_valid = False
            if not timestamps_valid:
                continue
            task["active"] = status in AUTO_TRANSFER_ACTIVE_STATUSES
            self._owner._auto_transfer_tasks[task_id] = task

    def restore(self, *, resume: bool) -> None:
        owner = self._owner
        with owner._transfer_auto_lock:
            if not owner._auto_transfer_restored:
                self._load_locked()
                owner._auto_transfer_restored = True
            changed = self._discard_expired_locked()
            worker = owner._auto_transfer_worker
            if not worker or not worker.is_alive():
                for task in owner._auto_transfer_tasks.values():
                    if task["status"] == "in_progress":
                        task.update(status="pending", active=True, next_run_ts=0, message="等待恢复入库自动字幕处理")
                        changed = True
            if changed:
                self._save_locked()
            owner._auto_transfer_stopping = not resume
            owner._auto_transfer_wakeup.set()
            start_worker = resume and bool(owner._auto_transfer_tasks)
        if start_worker and self._ensure_worker:
            self._ensure_worker()

    def stop(self) -> None:
        owner = self._owner
        with owner._transfer_auto_lock:
            owner._auto_transfer_stopping = True
            owner._auto_transfer_wakeup.set()
            worker = owner._auto_transfer_worker
            if not worker or not worker.is_alive():
                owner._auto_transfer_worker = None

    def transfer_key(self, entry: Dict[str, Any]) -> str:
        owner = self._owner
        path = owner._normalize_text(entry.get("path") or entry.get("relative_path"))
        if path:
            normalized_path = path.lower().replace("\\", "/")
            return owner._hash_text(f"{normalized_path}|{owner._entry_filesystem_signature(entry)}")
        return owner._normalize_text(entry.get("id") or entry.get("target_label") or entry.get("filename"))

    def claim_entries(self, entries: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
        owner = self._owner
        now = self._time.time()
        claimed: List[Dict[str, Any]] = []
        skipped = 0
        with owner._transfer_auto_lock:
            owner._transfer_auto_recent = {
                key: ts
                for key, ts in (owner._transfer_auto_recent or {}).items()
                if now - ts < owner._transfer_auto_dedupe_seconds
            }
            for entry in entries:
                key = self.transfer_key(entry)
                if not key:
                    claimed.append(entry)
                    continue
                if key in owner._transfer_auto_recent:
                    skipped += 1
                    continue
                owner._transfer_auto_recent[key] = now
                claimed.append(entry)
        return claimed, skipped

    def entry_key(self, entry: Dict[str, Any]) -> str:
        owner = self._owner
        return self.transfer_key(entry) or owner._hash_text(
            f"{entry.get('id')}|{entry.get('target_label')}|{entry.get('filename')}"
        )

    def _target_key(self, entry: Dict[str, Any]) -> str:
        path = self._owner._normalize_text(entry.get("path"))
        return path.casefold().replace("\\", "/") or self.entry_key(entry)

    def group_key(self, entry: Dict[str, Any]) -> str:
        owner = self._owner
        if owner._normalize_text(entry.get("media_type")) != "tv":
            return self.entry_key(entry)
        media_key = owner._normalize_text(entry.get("media_key") or entry.get("tmdb_id") or entry.get("title"))
        season = owner._safe_int(entry.get("season"), 0)
        if not media_key or not season:
            return self.entry_key(entry)
        return f"tv|{media_key}|s{season:02d}"

    def _discard_expired_locked(self) -> bool:
        tasks = self._owner._auto_transfer_tasks
        cutoff = self._time.time() - _HISTORY_TTL_SECONDS
        expired = [
            task_id for task_id, task in tasks.items()
            if task["status"] not in AUTO_TRANSFER_ACTIVE_STATUSES and task["updated_ts"] <= cutoff
        ]
        for task_id in expired:
            tasks.pop(task_id)
        return bool(expired)

    def trim_locked(self) -> None:
        if self._discard_expired_locked():
            self._save_locked()

    def enqueue_entries(self, entries: List[Dict[str, Any]]) -> Tuple[int, int]:
        owner = self._owner
        valid_entries = owner.services.local_media_catalog().filter_existing_local_entries(entries)
        if getattr(owner, "_auto_transfer_stopping", False):
            return 0, len(valid_entries) + (len(entries or []) - len(valid_entries))
        claimed, skipped = self.claim_entries(valid_entries)
        if not claimed:
            return 0, skipped + (len(entries or []) - len(valid_entries))
        now = self._time.time()
        queued = 0
        with owner._transfer_auto_lock:
            if owner._auto_transfer_stopping:
                return 0, len(entries or [])
            active_keys = {
                self._target_key(task["entry"])
                for task in (owner._auto_transfer_tasks or OrderedDict()).values()
                if task.get("status") in AUTO_TRANSFER_ACTIVE_STATUSES
            }
            for entry in claimed:
                target_key = self._target_key(entry)
                if target_key in active_keys:
                    skipped += 1
                    continue
                entry_key = self.entry_key(entry)
                task_id = owner._hash_text(f"auto-transfer|{entry_key}|{now}")[:16]
                owner._auto_transfer_tasks[task_id] = build_auto_transfer_task(
                    task_id=task_id,
                    entry_key=entry_key,
                    group_key=self.group_key(entry),
                    entry=entry,
                    now=now,
                    safe_int=owner._safe_int,
                )
                active_keys.add(target_key)
                queued += 1
            if queued:
                self._save_locked()
                owner._auto_transfer_wakeup.set()
        if queued and self._ensure_worker:
            self._ensure_worker()
        return queued, skipped

    def update_task(self, task_id: str, **updates: Any) -> None:
        owner = self._owner
        with owner._transfer_auto_lock:
            task = (owner._auto_transfer_tasks or OrderedDict()).get(task_id)
            if not task:
                return
            if owner._auto_transfer_stopping:
                if task["status"] == "in_progress":
                    task.update(status="pending", next_run_ts=0, message="等待恢复入库自动字幕处理")
                return
            task.update(updates)
            task["updated_ts"] = self._time.time()
            if task.get("status") == "pending":
                task["active"] = True
            if task.get("status") not in AUTO_TRANSFER_ACTIVE_STATUSES:
                task["active"] = False
                task["next_run_ts"] = 0
            owner._auto_transfer_tasks.move_to_end(task_id)
            self._save_locked()
            owner._auto_transfer_wakeup.set()

    def defer_bilingual_followup(self, task: Dict[str, Any], result: Dict[str, Any]) -> None:
        now = self._time.time()
        started = task.get("bilingual_retry_since")
        if started is None:
            started = now
        next_run = next(
            (started + delay for delay in _BILINGUAL_RETRY_DELAYS if started + delay > now),
            now + _BILINGUAL_RETRY_DELAYS[-1],
        )
        self.update_task(
            task["id"], status="pending", result=result, next_run_ts=next_run,
            bilingual_retry_since=started, emby_rechecked=False,
            emby_offline_attempts=0,
            message=f"等待双语字幕补搜：{result.get('reason') or '本轮未补齐双语字幕'}",
        )

    def cancel_bilingual_followup(self, task_id: str) -> bool:
        owner = self._owner
        with owner._transfer_auto_lock:
            task = owner._auto_transfer_tasks.get(task_id)
            if owner._auto_transfer_stopping or not task or not is_bilingual_followup_waiting(task):
                return False
            task.update(
                status="skipped", active=False, next_run_ts=0, updated_ts=self._time.time(),
                message="已取消双语字幕补搜",
            )
            owner._auto_transfer_tasks.move_to_end(task_id)
            self._save_locked()
            owner._auto_transfer_wakeup.set()
            return True

    def retry_task(self, task_id: str, *, force_low_confidence: bool = False) -> bool:
        owner = self._owner
        with owner._transfer_auto_lock:
            if owner._auto_transfer_stopping:
                return False
            self.trim_locked()
            task = (owner._auto_transfer_tasks or OrderedDict()).get(task_id)
            if not task or (task.get("status") not in {"failed", "skipped"} and not is_bilingual_followup_waiting(task)):
                return False
            if force_low_confidence and not (
                task.get("status") == "failed" and "低可信" in str(task.get("message") or "")
            ):
                return False
            entry = owner._json_clone(task.get("entry") or {})
            if not entry:
                return False
            if force_low_confidence:
                entry["_force_timeline_write"] = True
            else:
                entry.pop("_force_timeline_write", None)
            now = self._time.time()
            task.pop("emby_rechecked", None)
            task.pop("emby_offline_attempts", None)
            task.update(
                {
                    "entry": entry,
                    "group_key": task.get("entry_key") if force_low_confidence else task.get("group_key"),
                    "status": "pending",
                    "active": True,
                    "message": "等待重新处理自动入库字幕",
                    "result": {},
                    "updated_ts": now,
                    "next_run_ts": 0,
                    "retry_count": int(task.get("retry_count") or 0) + 1,
                }
            )
            owner._auto_transfer_tasks.move_to_end(task_id)
            self._save_locked()
            owner._auto_transfer_wakeup.set()
        if self._ensure_worker:
            self._ensure_worker()
        return True

    def clear_history(self) -> int:
        owner = self._owner
        with owner._transfer_auto_lock:
            removable = [
                task_id
                for task_id, task in (owner._auto_transfer_tasks or OrderedDict()).items()
                if task.get("status") not in AUTO_TRANSFER_ACTIVE_STATUSES
            ]
            for task_id in removable:
                owner._auto_transfer_tasks.pop(task_id, None)
            if removable:
                self._save_locked()
                owner._auto_transfer_wakeup.set()
        return len(removable)

    def claim_next_batch(self) -> Tuple[List[Dict[str, Any]], float]:
        owner = self._owner
        with owner._transfer_auto_lock:
            owner._auto_transfer_wakeup.clear()
            if getattr(owner, "_auto_transfer_stopping", False):
                owner._auto_transfer_worker = None
                return [], -1
            self.trim_locked()
            tasks = owner._auto_transfer_tasks
            now = self._time.time()
            deadlines = [
                task["updated_ts"] + _HISTORY_TTL_SECONDS for task in tasks.values()
                if task["status"] not in AUTO_TRANSFER_ACTIVE_STATUSES
            ]
            pending = [
                task
                for task in tasks.values()
                if task.get("status") == "pending"
            ]
            if not pending:
                if deadlines:
                    return [], min(3600.0, max(0.0, min(deadlines) - now))
                owner._auto_transfer_worker = None
                return [], -1
            ready = [
                task for task in pending
                if (not task.get("emby_rechecked") and not task.get("emby_offline_attempts")
                    and not is_bilingual_followup_waiting(task))
                or float(task.get("next_run_ts") or 0) <= now
            ]
            if not ready:
                next_run = min([*deadlines, *(float(task["next_run_ts"]) for task in pending)])
                max_wait = 1.0 if any(
                    task.get("emby_rechecked") and not task.get("emby_offline_attempts") for task in pending
                ) else 3600.0
                return [], min(next_run - now, max_wait)
            first = ready[0]
            group_key = owner._normalize_text(first.get("group_key"))
            group = [task for task in ready if owner._normalize_text(task.get("group_key")) == group_key]
            is_tv_group = owner._normalize_text(first.get("media_type")) == "tv"
            if is_tv_group:
                newest = max(float(task.get("created_ts") or 0) for task in group)
                wait_seconds = owner._auto_transfer_queue_debounce_seconds - (self._time.time() - newest)
                if wait_seconds > 0:
                    for task in group:
                        task["next_run_ts"] = self._time.time() + wait_seconds
                        task["message"] = "等待同季入库事件聚合"
                    return [], min(wait_seconds, 1.0)
                batch = group
            else:
                batch = [first]
            now = self._time.time()
            for task in batch:
                task["status"] = "in_progress"
                task["active"] = True
                task["message"] = "入库自动字幕处理中"
                task["updated_ts"] = now
                task["next_run_ts"] = 0
            self._save_locked()
            return [owner._json_clone(task) for task in batch], 0

    def summary(self) -> Dict[str, Any]:
        owner = self._owner
        with owner._transfer_auto_lock:
            self.trim_locked()
            return self._summary_locked()

    def _summary_locked(self) -> Dict[str, Any]:
        owner = self._owner
        tasks = owner._auto_transfer_tasks.values()
        summary = {
            "pending": 0,
            "waiting_bilingual": 0,
            "in_progress": 0,
            "completed": 0,
            "skipped": 0,
            "failed": 0,
            "active": 0,
            "total": len(tasks),
            "worker_alive": bool(owner._auto_transfer_worker and owner._auto_transfer_worker.is_alive()),
        }
        for task in tasks:
            status = task.get("status")
            if status in AUTO_TRANSFER_PUBLIC_STATUSES:
                summary[status] += 1
            if is_bilingual_followup_waiting(task):
                summary["waiting_bilingual"] += 1
            if is_auto_transfer_task_active(task):
                summary["active"] += 1
        return summary

    def snapshot(self, limit: int = 100) -> Dict[str, Any]:
        owner = self._owner
        with owner._transfer_auto_lock:
            self.trim_locked()
            recent = list(owner._auto_transfer_tasks.values())[-limit:]
            tasks = [owner._json_clone(task) for task in recent]
            summary = self._summary_locked()
        pending_position = 0
        public_tasks: List[Dict[str, Any]] = []
        for task in tasks:
            if task.get("status") == "pending":
                pending_position += 1
            public_tasks.append(
                public_auto_transfer_task(
                    task,
                    timestamp_iso=owner._timestamp_iso,
                    queue_position=pending_position,
                )
            )
        cache_items = []
        for item in list((owner._auto_season_package_cache or OrderedDict()).values())[-20:]:
            cache_items.append(
                {
                    "key": item.get("key"),
                    "title": item.get("title"),
                    "season": item.get("season"),
                    "subtitle_count": len(item.get("items") or []),
                    "updated_at": item.get("updated_at", ""),
                    "provider": item.get("provider", ""),
                    "source_title": item.get("source_title", ""),
                }
            )
        return {
            "summary": summary,
            "tasks": public_tasks,
            "rate_limits": self._rate_status(),
            "season_package_cache": cache_items,
            "debounce_seconds": owner._auto_transfer_queue_debounce_seconds,
            "rate_limit_scope": "provider",
        }
