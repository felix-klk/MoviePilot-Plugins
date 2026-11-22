from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ..online_subtitles.catalog import AVAILABLE_ONLINE_PROVIDER_IDS
from ..online_subtitles.matcher import (
    AUTO_DOWNLOAD_ATTEMPT_LIMIT,
    AUTO_SEASON_ATTEMPT_LIMIT,
    _episode_from_text,
    _season_matches_text,
    auto_download_attempts,
    rank_auto_download_candidates,
)
from ..online_subtitles.models import OnlineSubtitleResult, SubHDWechatVerificationRequired
from .auto_transfer_models import WAITING_VERIFICATION

from .auto_transfer_processor import AutoTransferProcessor, AutoTransferProcessorCollaborators
from .auto_transfer_queue import AutoTransferQueue
from .auto_transfer_rate_limit import AutoTransferRateLimiter
from .auto_transfer_season import AutoTransferSeasonCache, AutoTransferSeasonCollaborators
from .auto_transfer_write import AutoTransferWriteCollaborators, AutoTransferWriteStrategy
from ..matching.subtitle_language import (
    auto_subtitle_sort_key,
    auto_target_has_chinese_subtitle as language_auto_target_has_chinese_subtitle,
    is_chinese_language_suffix,
    language_profile_from_text,
    required_auto_languages,
    target_satisfies_auto_subtitle_policy,
)


def _result_matches_preview_targets(
    item: Dict[str, Any], targets: List[Dict[str, Any]], require_season_evidence: bool,
) -> bool:
    season, _ = _episode_from_text(item["title"]) or (0, 0)
    return not require_season_evidence or bool(item["season"] or season) or _season_matches_text(item["title"], targets)



@dataclass(frozen=True)
class AutoTransferCollaborators:
    online_service_factory: Optional[Callable[[], Any]] = None
    target_from_entry: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
    extract_subtitle_files: Optional[Callable[..., List[Dict[str, Any]]]] = None
    write_operations_to_disk: Optional[Callable[..., Tuple[List[Dict[str, Any]], int, int]]] = None
    submit_autosub_for_entries: Optional[Callable[..., Dict[str, Any]]] = None
    prepare_online_ai_subtitle_overrides: Optional[Callable[..., Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]] = None
    auto_media_for_entry: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
    is_chinese_transfer_media: Optional[Callable[[Dict[str, Any]], Tuple[bool, str]]] = None
    normalize_online_download_name: Optional[Callable[..., str]] = None
    detect_language_profile: Optional[Callable[[str, bytes], Dict[str, Any]]] = None
    auto_subtitle_sort_key: Optional[Callable[[Dict[str, Any]], Tuple[int, int, int, str]]] = None
    auto_target_has_chinese_subtitle: Optional[Callable[[Dict[str, Any], Dict[str, Any]], bool]] = None
    check_online_rate_limit: Optional[Callable[[Iterable[str]], None]] = None
    wait_online_rate_limit: Optional[Callable[..., None]] = None
    logger: Any = None
    threading_module: Any = None
    time_module: Any = None
    http_exception: Any = None

    @classmethod
    def from_owner(
        cls,
        owner: Any,
        *,
        logger: Any,
        threading_module: Any,
        time_module: Any,
        http_exception: Any,
    ) -> "AutoTransferCollaborators":
        return cls(
            online_service_factory=owner._online_service,
            target_from_entry=owner._target_from_entry,
            extract_subtitle_files=lambda upload_name, raw_bytes, session_dir: owner._upload_session_service_for_path(
                session_dir.parent
            ).extract_online_subtitle_files(upload_name, raw_bytes, session_dir),
            write_operations_to_disk=lambda **kwargs: owner._subtitle_writer().write_operations_to_disk(**kwargs),
            submit_autosub_for_entries=lambda *args, **kwargs: owner._autosub_bridge().submit_autosub_for_entries(
                *args,
                **kwargs,
            ),
            prepare_online_ai_subtitle_overrides=lambda *args, **kwargs: owner._online_ai_service().prepare_online_ai_subtitle_overrides(
                *args,
                **kwargs,
            ),
            auto_media_for_entry=lambda entry: owner._media_metadata_service().auto_media_for_entry(entry),
            is_chinese_transfer_media=lambda entry: owner._media_metadata_service().is_chinese_transfer_media(entry),
            normalize_online_download_name=lambda name, content, result: owner._upload_session_service().normalize_online_download_name(
                name,
                content,
                result,
            ),
            detect_language_profile=owner._detect_language_profile,
            auto_subtitle_sort_key=lambda item: auto_subtitle_sort_key(
                item,
                language_priority=list(getattr(owner, "_auto_subtitle_language_priority", None) or owner._default_auto_language_priority),
                format_priority=list(getattr(owner, "_auto_subtitle_format_priority", None) or owner._default_auto_format_priority),
            ),
            auto_target_has_chinese_subtitle=lambda entry, target: language_auto_target_has_chinese_subtitle(
                entry,
                target,
                subtitle_inventory=owner._subtitle_inventory(),
                is_chinese_language_suffix_func=is_chinese_language_suffix,
                language_priority=getattr(owner, "_auto_subtitle_language_priority", None),
                strict=bool(getattr(owner, "_auto_subtitle_language_strict", False)),
            ),
            check_online_rate_limit=owner._check_online_rate_limit,
            logger=logger,
            threading_module=threading_module,
            time_module=time_module,
            http_exception=http_exception,
        )


class AutoTransferService:
    def __init__(
        self,
        owner: Any,
        *,
        logger: Any,
        threading_module: Any,
        time_module: Any,
        http_exception: Any,
        collaborators: Optional[AutoTransferCollaborators] = None,
    ) -> None:
        self._owner = owner
        self._collaborators = collaborators or AutoTransferCollaborators()
        self._logger = self._collaborators.logger or logger
        self._threading = self._collaborators.threading_module or threading_module
        self._time = self._collaborators.time_module or time_module
        self._http_exception = self._collaborators.http_exception or http_exception
        self._queue = AutoTransferQueue(
            owner,
            time_module=self._time,
            logger=self._logger,
            ensure_worker=lambda: self.ensure_transfer_auto_worker(),
            rate_status=self.auto_transfer_rate_status,
        )
        self._rate_limiter = AutoTransferRateLimiter(owner, time_module=self._time)
        self._processor = AutoTransferProcessor(
            owner,
            logger=self._logger,
            http_exception=self._http_exception,
            collaborators=AutoTransferProcessorCollaborators(
                target_from_entry=lambda entry: self._target_from_entry(entry),
                auto_target_has_chinese_subtitle=lambda entry, target: self._auto_target_has_chinese_subtitle(entry, target),
                chinese_transfer_media_evidence=lambda entry: self._chinese_transfer_media_evidence(entry),
                media_for_transfer_entry=lambda entry: self._media_for_transfer_entry(entry),
                search_providers=lambda: self.auto_search_providers(),
                search_keywords_for_entry=lambda entry, target: self.auto_search_keywords_for_entry(entry, target),
                check_online_rate_limit=lambda providers: self._check_online_rate_limit(providers),
                wait_online_rate_limit=lambda providers, task_ids=None: self._wait_online_rate_limit(providers, task_ids=task_ids),
                online_service=lambda: self._online_service(),
                online_download_name=lambda name, content, result: self._online_download_name(name, content, result),
                extract_subtitle_files=lambda upload_name, raw_bytes, session_dir: self._extract_subtitle_files(
                    upload_name,
                    raw_bytes,
                    session_dir,
                ),
                write_prepared_uploads_for_entries=lambda **kwargs: self.auto_write_prepared_uploads_for_entries(**kwargs),
                submit_autosub_for_entries=lambda *args, **kwargs: self._submit_autosub_for_entries(*args, **kwargs),
                call_search_write_subtitle=lambda *args, **kwargs: self._call_auto_search_write_subtitle(*args, **kwargs),
                submit_ai_for_entry=lambda entry, target, reason: self.auto_submit_ai_for_entry(entry, target, reason),
            ),
        )
        self._season_cache = AutoTransferSeasonCache(
            owner,
            logger=self._logger,
            collaborators=AutoTransferSeasonCollaborators(
                target_from_entry=lambda entry: self._target_from_entry(entry),
                auto_target_has_chinese_subtitle=lambda entry, target: self._auto_target_has_chinese_subtitle(entry, target),
                chinese_transfer_media_evidence=lambda entry: self._chinese_transfer_media_evidence(entry),
                media_for_transfer_entry=lambda entry: self._media_for_transfer_entry(entry),
                task_result_key=lambda entry: self._auto_task_result_key(entry),
                search_providers=lambda: self.auto_search_providers(),
                wait_online_rate_limit=lambda providers, task_ids=None: self._wait_online_rate_limit(providers, task_ids=task_ids),
                online_service=lambda: self._online_service(),
                online_download_name=lambda name, content, result: self._online_download_name(name, content, result),
                extract_subtitle_files=lambda upload_name, raw_bytes, session_dir: self._extract_subtitle_files(
                    upload_name,
                    raw_bytes,
                    session_dir,
                ),
                write_prepared_uploads_for_entries=lambda **kwargs: self.auto_write_prepared_uploads_for_entries(**kwargs),
                store_cache=lambda entries, prepared_uploads, selected_result: self.store_auto_season_package_cache(
                    entries,
                    prepared_uploads,
                    selected_result,
                ),
                load_cache=lambda entry: self.load_auto_season_package_cache(entry),
                write_from_cache=lambda entries: self.auto_write_from_season_cache(entries),
                search_write_package=lambda entries, **kwargs: self.auto_search_write_season_package(entries, **kwargs),
                process_entry=lambda entry, **kwargs: self.auto_process_transfer_entry(entry, **kwargs),
            ),
        )
        self._write_strategy = AutoTransferWriteStrategy(
            owner,
            collaborators=AutoTransferWriteCollaborators(
                target_from_entry=lambda entry: self._target_from_entry(entry),
                detect_language_profile=lambda file_name, raw_bytes: self._detect_language_profile(file_name, raw_bytes),
                subtitle_preference_sort_key=self._collaborators.auto_subtitle_sort_key,
                write_operations_to_disk=lambda **kwargs: self._write_operations_to_disk(**kwargs),
                prepare_online_ai_subtitle_overrides=lambda **kwargs: self._prepare_online_ai_subtitle_overrides(**kwargs),
                submit_autosub_for_entries=lambda *args, **kwargs: self._submit_autosub_for_entries(*args, **kwargs),
                select_subtitle_items=lambda prepared_uploads, targets: self.select_auto_subtitle_items(
                    prepared_uploads,
                    targets,
                ),
            ),
        )

    def _callback(self, name: str, fallback_name: str) -> Callable[..., Any]:
        callback = getattr(self._collaborators, name)
        return callback or getattr(self._owner, fallback_name)

    def _online_service(self) -> Any:
        return self._callback("online_service_factory", "_online_service")()

    def _target_from_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        return self._callback("target_from_entry", "_target_from_entry")(entry)

    def _check_online_rate_limit(self, providers: Iterable[str]) -> None:
        self._callback("check_online_rate_limit", "_check_online_rate_limit")(providers)

    def _wait_online_rate_limit(self, providers: Iterable[str], task_ids: Optional[List[str]] = None) -> None:
        if self._collaborators.wait_online_rate_limit:
            self._collaborators.wait_online_rate_limit(providers, task_ids=task_ids)
            return
        self.auto_wait_online_rate_limit(providers, task_ids=task_ids)

    def _online_download_name(self, name: str, content: bytes, result: Dict[str, Any]) -> str:
        callback = self._collaborators.normalize_online_download_name
        if callback:
            return callback(name, content, result)
        return self._owner._upload_session_service().normalize_online_download_name(name, content, result)

    def _extract_subtitle_files(self, upload_name: str, raw_bytes: bytes, session_dir: Path) -> List[Dict[str, Any]]:
        return self._callback("extract_subtitle_files", "_extract_subtitle_files")(upload_name, raw_bytes, session_dir)

    def _auto_target_has_chinese_subtitle(self, entry: Dict[str, Any], target: Dict[str, Any]) -> bool:
        return self._callback("auto_target_has_chinese_subtitle", "_auto_target_has_chinese_subtitle")(entry, target)

    def _media_for_transfer_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        callback = self._collaborators.auto_media_for_entry
        if callback:
            return callback(entry)
        return self._owner._media_metadata_service().auto_media_for_entry(entry)

    def _chinese_transfer_media_evidence(self, entry: Dict[str, Any]) -> Tuple[bool, str]:
        callback = self._collaborators.is_chinese_transfer_media
        if callback:
            return callback(entry)
        return self._owner._media_metadata_service().is_chinese_transfer_media(entry)

    def _auto_search_write_subtitle(self, entry: Dict[str, Any], target: Dict[str, Any]) -> Dict[str, Any]:
        return self.auto_search_write_subtitle(entry, target)

    def _call_auto_search_write_subtitle(
        self,
        entry: Dict[str, Any],
        target: Dict[str, Any],
        *,
        queue_rate_limited: bool = False,
        task_ids: Optional[List[str]] = None,
        blocked_providers: Optional[Dict[str, SubHDWechatVerificationRequired]] = None,
    ) -> Dict[str, Any]:
        try:
            return self.auto_search_write_subtitle(
                entry,
                target,
                queue_rate_limited=queue_rate_limited,
                task_ids=task_ids,
                blocked_providers=blocked_providers,
            )
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise
            return self._auto_search_write_subtitle(entry, target)

    def _submit_autosub_for_entries(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self._callback("submit_autosub_for_entries", "_submit_autosub_for_entries")(*args, **kwargs)

    def _detect_language_profile(self, file_name: str, raw_bytes: bytes) -> Dict[str, Any]:
        return self._callback("detect_language_profile", "_detect_language_profile")(file_name, raw_bytes)

    def _subtitle_preference_sort_key(self, item: Dict[str, Any]) -> Tuple[int, int, int, str]:
        return self._write_strategy.subtitle_preference_sort_key(item)

    def _write_operations_to_disk(self, **kwargs: Any) -> Tuple[List[Dict[str, Any]], int, int]:
        return self._callback("write_operations_to_disk", "_write_operations_to_disk")(**kwargs)

    def _prepare_online_ai_subtitle_overrides(self, *args: Any, **kwargs: Any):
        return self._callback("prepare_online_ai_subtitle_overrides", "_prepare_online_ai_subtitle_overrides")(*args, **kwargs)

    def _auto_task_result_key(self, entry: Dict[str, Any]) -> str:
        owner = self._owner
        return owner._normalize_text(entry.get("id")) or self.auto_transfer_entry_key(entry)

    def _auto_season_cache_key(self, entry: Dict[str, Any]) -> str:
        return self._season_cache.cache_key(entry)

    def _auto_season_cache_dir(self, cache_key: str) -> Path:
        return self._season_cache.cache_dir(cache_key)

    def restore(self, *, resume: bool) -> None:
        self._queue.restore(resume=resume)

    def stop(self) -> None:
        self._queue.stop()

    def transfer_auto_key(self, entry: Dict[str, Any]) -> str:
        return self._queue.transfer_key(entry)

    def claim_transfer_auto_entries(self, entries: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
        return self._queue.claim_entries(entries)

    def auto_transfer_entry_key(self, entry: Dict[str, Any]) -> str:
        return self._queue.entry_key(entry)

    def auto_transfer_group_key(self, entry: Dict[str, Any]) -> str:
        return self._queue.group_key(entry)

    def trim_auto_transfer_tasks_locked(self) -> None:
        self._queue.trim_locked()

    def enqueue_transfer_auto_entries(self, entries: List[Dict[str, Any]]) -> Tuple[int, int]:
        return self._queue.enqueue_entries(entries)

    def enqueue_transfer_auto_entries_with_outcomes(self, entries: List[Dict[str, Any]]) -> List[str]:
        return self._queue.enqueue_entries_with_outcomes(entries)

    def enqueue_missing_directory_entries(self, entries: List[Dict[str, Any]]) -> None:
        if not self.auto_search_providers():
            return
        owner = self._owner
        missing = []
        for entry in entries:
            entry = {**entry, "_directory_auto_search": True}
            target = self._target_from_entry(entry)
            if not target.get("writable", True) or self._processor.target_skip_reason(entry, target):
                continue
            if getattr(owner, "_auto_check_emby_subtitles", False):
                result = self._check_emby_subtitles_for_task({"entry": entry})
                if result and result["status"] == "skipped":
                    continue
            media = owner.services.media_metadata().enrich_media(entry)
            if media.get("tmdb_id"):
                media["media_source"] = "themoviedb"
                media["media_id"] = str(media["tmdb_id"])
            missing.append(media)
        if missing:
            self.enqueue_transfer_auto_entries(missing)

    def handle_transfer_complete(self, event: Any) -> Dict[str, Any]:
        owner = self._owner
        event_data = getattr(event, "event_data", None) or {}
        if not isinstance(event_data, dict):
            return {"entries": [], "queued": 0, "skipped": 0}
        entries = owner.services.target_resolver().entries_from_transfer_event(event_data)
        if not entries:
            self._logger.info("[NeoSubtitleMaster] 入库事件未解析到本地视频目标，跳过自动字幕搜索")
            return {"entries": [], "queued": 0, "skipped": 0}
        owner.services.local_media_catalog().merge_local_entries_cache(entries)
        queued, skipped = self.enqueue_transfer_auto_entries(entries)
        if skipped:
            self._logger.info("[NeoSubtitleMaster] 入库自动字幕处理去重跳过重复目标 count=%s", skipped)
        if queued:
            self._logger.info("[NeoSubtitleMaster] 入库自动字幕任务已入队 count=%s", queued)
        return {"entries": entries, "queued": queued, "skipped": skipped}

    def ensure_transfer_auto_worker(self) -> None:
        owner = self._owner
        with owner._transfer_auto_lock:
            if getattr(owner, "_auto_transfer_stopping", False):
                return
            worker = owner._auto_transfer_worker
            if worker and worker.is_alive():
                return
            worker = self._threading.Thread(
                target=self.auto_transfer_queue_loop,
                name="NeoSubtitleMasterTransferQueue",
                daemon=True,
            )
            owner._auto_transfer_worker = worker
            worker.start()

    def update_auto_transfer_task(self, task_id: str, **updates: Any) -> None:
        self._queue.update_task(task_id, **updates)

    def retry_auto_transfer_task(self, task_id: str, *, force_low_confidence: bool = False) -> bool:
        return self._queue.retry_task(task_id, force_low_confidence=force_low_confidence)

    def can_follow_up_bilingual_subtitle(self, entry: Dict[str, Any]) -> bool:
        owner = self._owner
        return (
            entry.get("media_type") == "tv"
            and owner._safe_int(entry.get("episode"), 0) > 0
            and required_auto_languages(owner._auto_subtitle_language_priority) is not None
            and (entry.get("_directory_auto_search")
                 or owner._normalize_auto_transfer_subtitle_strategy(owner._auto_transfer_subtitle_strategy) != "ai_source_only")
            and bool(self.auto_search_providers())
        )

    def cancel_bilingual_followup(self, task_id: str) -> bool:
        return self._queue.cancel_bilingual_followup(task_id)

    def clear_auto_transfer_history(self) -> int:
        return self._queue.clear_history()

    def claim_next_auto_transfer_batch(self) -> Tuple[List[Dict[str, Any]], float]:
        return self._queue.claim_next_batch()

    def auto_wait_online_rate_limit(self, providers: Iterable[str], task_ids: Optional[List[str]] = None) -> None:
        self._rate_limiter.wait(providers, task_ids=task_ids)

    def auto_transfer_rate_status(self) -> Dict[str, Any]:
        return self._rate_limiter.status(self.auto_search_providers())

    def auto_transfer_queue_summary(self) -> Dict[str, Any]:
        return self._queue.summary()

    def auto_transfer_queue_snapshot(self, limit: int = 100) -> Dict[str, Any]:
        return self._queue.snapshot(limit=limit)

    def auto_transfer_queue_loop(self) -> None:
        owner = self._owner
        while True:
            batch, wait_seconds = owner._claim_next_auto_transfer_batch()
            if wait_seconds < 0:
                return
            if not batch:
                owner._auto_transfer_wakeup.wait(timeout=max(0.2, wait_seconds))
                continue
            try:
                owner._process_transfer_auto_task_batch(batch)
            except Exception as exc:
                self._logger.warning("[NeoSubtitleMaster] 入库自动字幕队列批次失败: %s", exc)
                for task in batch:
                    owner._update_auto_transfer_task(
                        task["id"],
                        status="failed",
                        message=f"入库自动字幕处理异常: {exc}",
                    )

    def auto_search_keywords_for_entry(self, entry: Dict[str, Any], target: Dict[str, Any]) -> List[str]:
        return self._processor.search_keywords_for_entry(entry, target)


    def auto_search_providers(self) -> List[str]:
        owner = self._owner
        return [item for item in (owner._online_provider_ids or []) if item in AVAILABLE_ONLINE_PROVIDER_IDS]

    def preview_online_download(
        self,
        entries: List[Dict[str, Any]],
        results: List[OnlineSubtitleResult],
        scope: str,
    ) -> Dict[str, Any]:
        owner = self._owner
        providers = self.auto_search_providers()
        priority = getattr(owner, "_auto_subtitle_language_priority", None)
        strict = bool(getattr(owner, "_auto_subtitle_language_strict", False))
        strategy = owner._normalize_auto_transfer_subtitle_strategy(owner._auto_transfer_subtitle_strategy)
        available = [item.to_dict() for item in results if item.provider in providers]
        skipped = []
        groups: Dict[str, List[Dict[str, Any]]] = {}
        require_season_evidence = len({
            self.auto_transfer_group_key(entry) for entry in entries if entry.get("media_type") == "tv"
        }) > 1

        for entry in entries:
            target = self._target_from_entry(entry)
            reason = ""
            if not target.get("writable", True):
                reason = "目标不可写"
            if not reason and owner._auto_check_emby_subtitles:
                check = self._check_emby_subtitles_for_task({"entry": entry})
                if check:
                    if check["status"] == "pending" and not check.get("emby_offline_attempts"):
                        reason = "Emby 尚未扫描到该目标的字幕轨道"
                    else:
                        reason = check["reason"]
            if not reason:
                reason = self._processor.target_skip_reason(entry, target)
            if not reason and strategy == "ai_source_only" and not target.get("is_stream"):
                reason = "当前策略仅提交 AI 字幕任务"
            if reason:
                skipped.append({"target_id": target["id"], "target": target["label"], "reason": reason})
                continue
            groups.setdefault(self.auto_transfer_group_key(entry), []).append(target)

        plans = []
        for targets in groups.values():
            season_scope = targets[0].get("media_type") == "tv" and (len(targets) > 1 or scope in {"season", "batch"})
            rejections: Dict[str, int] = {}
            if len(available) < len(results):
                rejections["字幕源未启用自动入库"] = len(results) - len(available)
            candidates = [
                item for item in available
                if targets[0].get("media_type") != "tv"
                or _result_matches_preview_targets(item, targets, require_season_evidence)
            ]
            if len(candidates) < len(available):
                rejections["季集不匹配或未确认"] = len(available) - len(candidates)
            candidates = rank_auto_download_candidates(
                candidates, targets, priority,
                strict=strict, min_score=owner._auto_search_min_score, rejections=rejections,
            )
            attempt_limit = AUTO_SEASON_ATTEMPT_LIMIT if season_scope else AUTO_DOWNLOAD_ATTEMPT_LIMIT
            attempts = auto_download_attempts(candidates, attempt_limit)
            label = targets[0]["label"]
            if len(targets) > 1:
                label = f"第 {targets[0].get('season') or 0} 季 · {len(targets)} 个目标"
            plans.append({
                "target_ids": [target["id"] for target in targets],
                "target": label,
                "scope": "season" if season_scope else "single",
                "candidates": [
                    {key: item[key] for key in ("provider", "result_id", "title", "match_detail", "auto_rank")}
                    for item in attempts
                ],
                "candidate_count": len(candidates),
                "rejections": rejections,
            })
        return {
            "enabled": bool(owner.get_state() and owner._auto_search_on_transfer),
            "min_score": owner._auto_search_min_score,
            "required_languages": required_auto_languages(priority, strict=strict),
            "plans": plans,
            "skipped": skipped,
        }


    def auto_search_write_subtitle(
        self,
        entry: Dict[str, Any],
        target: Optional[Dict[str, Any]] = None,
        *,
        queue_rate_limited: bool = False,
        task_ids: Optional[List[str]] = None,
        blocked_providers: Optional[Dict[str, SubHDWechatVerificationRequired]] = None,
    ) -> Dict[str, Any]:
        return self._processor.search_write_subtitle(
            entry,
            target,
            queue_rate_limited=queue_rate_limited,
            task_ids=task_ids,
            blocked_providers=blocked_providers,
        )


    def auto_search_and_write_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        return self._processor.search_and_write_entry(entry)


    def auto_submit_ai_for_entry(
        self,
        entry: Dict[str, Any],
        target: Optional[Dict[str, Any]] = None,
        reason: str = "",
    ) -> Dict[str, Any]:
        return self._processor.submit_ai_for_entry(entry, target, reason)


    def auto_process_transfer_entry(
        self,
        entry: Dict[str, Any],
        *,
        queue_rate_limited: bool = False,
        task_ids: Optional[List[str]] = None,
        prior_write_conflict: str = "",
        blocked_providers: Optional[Dict[str, SubHDWechatVerificationRequired]] = None,
    ) -> Dict[str, Any]:
        return self._processor.process_entry(
            entry,
            queue_rate_limited=queue_rate_limited,
            task_ids=task_ids,
            prior_write_conflict=prior_write_conflict,
            blocked_providers=blocked_providers,
        )


    def auto_prepared_items_for_targets(
        self,
        prepared_uploads: List[Dict[str, Any]],
        targets: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        return self._write_strategy.prepared_items_for_targets(prepared_uploads, targets)


    def select_auto_subtitle_items(
        self,
        prepared_uploads: List[Dict[str, Any]],
        targets: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        return self._write_strategy.select_subtitle_items(prepared_uploads, targets)


    def auto_write_prepared_uploads_for_entries(
        self,
        *,
        target_entries: List[Dict[str, Any]],
        prepared_uploads: List[Dict[str, Any]],
        session_dir: Any,
        selected_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self._write_strategy.write_prepared_uploads_for_entries(
            target_entries=target_entries,
            prepared_uploads=prepared_uploads,
            session_dir=session_dir,
            selected_result=selected_result,
        )


    def store_auto_season_package_cache(
        self,
        entries: List[Dict[str, Any]],
        prepared_uploads: List[Dict[str, Any]],
        selected_result: Dict[str, Any],
    ) -> None:
        self._season_cache.store_cache(entries, prepared_uploads, selected_result)


    def load_auto_season_package_cache(self, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._season_cache.load_cache(entry)


    def auto_write_from_season_cache(self, entries: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self._season_cache.write_from_cache(entries)


    def auto_search_write_season_package(
        self,
        entries: List[Dict[str, Any]],
        *,
        task_ids: Optional[List[str]] = None,
        blocked_providers: Optional[Dict[str, SubHDWechatVerificationRequired]] = None,
    ) -> Dict[str, Any]:
        return self._season_cache.search_write_package(
            entries, task_ids=task_ids, blocked_providers=blocked_providers,
        )


    def auto_process_transfer_group(
        self,
        entries: List[Dict[str, Any]],
        task_ids: Optional[List[str]] = None,
        *,
        blocked_providers: Optional[Dict[str, SubHDWechatVerificationRequired]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        return self._season_cache.process_group(
            entries, task_ids=task_ids, blocked_providers=blocked_providers,
        )


    def _check_emby_subtitles_for_task(self, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        owner = self._owner
        entry = task["entry"]
        target = self._target_from_entry(entry)
        if (not entry.get("_directory_auto_search") and not target.get("is_stream")
                and owner._auto_transfer_subtitle_strategy == "ai_source_only"):
            return None

        required = required_auto_languages(
            owner._auto_subtitle_language_priority,
            strict=owner._auto_subtitle_language_strict,
        )
        local_skip = {"status": "skipped", "reason": "目标已有符合语言要求的字幕"}
        if target_satisfies_auto_subtitle_policy(target, required):
            return local_skip
        inventory = owner.services.subtitle_inventory()
        local_tracks = inventory.probe_embedded_subtitle_tracks(entry)
        if local_tracks is not None:
            target["embedded_subtitles"] = local_tracks
            self._logger.info(
                "[NeoSubtitleMaster] 入库字幕检测使用本地轨道 target=%s tracks=%s",
                target.get("label"),
                len(local_tracks),
            )
            return local_skip if target_satisfies_auto_subtitle_policy(target, required) else None

        if owner._auto_skip_chinese_media_on_transfer:
            is_chinese, evidence = self._chinese_transfer_media_evidence(entry)
            if is_chinese:
                return {"status": "skipped", "reason": f"中文资源自动跳过：{evidence}"}

        emby = owner.services.emby_refresh()
        scope = getattr(owner, "_emby_query_libraries", None)
        options = {"scope": scope} if scope is not None else {}
        status, tracks = emby.subtitle_tracks_for_entry(entry, owner._emby_subtitle_path_mappings, **options)
        if status == "offline":
            delays = (60, 300, 900)
            attempt = min(int(task.get("emby_offline_attempts") or 0), len(delays) - 1)
            delay = delays[attempt]
            return {
                "status": "pending",
                "reason": f"等待所选 Emby 恢复，{delay // 60} 分钟后重查",
                "retry_after": delay,
                "emby_offline_attempts": attempt + 1,
            }
        if status == "pending" and not task.get("emby_rechecked"):
            return {"status": "pending", "reason": "等待 Emby 扫描字幕轨道，30 秒后重查"}
        if status != "ready":
            self._logger.info(
                "[NeoSubtitleMaster] Emby 检查未确认，继续自动匹配 target=%s status=%s",
                target.get("label"),
                status,
            )
            return None

        for track in tracks:
            external = track.get("IsExternal")
            if not isinstance(external, bool) or track.get("IsTextSubtitleStream") is False:
                continue
            title = str(track.get("Title") or "")
            if not inventory.embedded_subtitle_track_is_usable(
                track.get("Codec"),
                title,
                {"forced": track.get("IsForced"), "comment": track.get("IsHearingImpaired")},
            ):
                continue
            language = str(track.get("Language") or "").strip()
            display_language = track.get("DisplayLanguage") or ""
            if language.lower() in {"", "und", "chi", "zho", "zh", "chinese"}:
                title_suffix = inventory.embedded_subtitle_language_suffix("", title)
                if title_suffix in {"chi", "cht"} or "&" in title_suffix:
                    language = title_suffix
                elif display_language:
                    language = display_language
            suffix = inventory.embedded_subtitle_language_suffix(language, title)
            if external:
                file_name = str(track.get("Path") or "").replace("\\", "/").rsplit("/", 1)[-1]
                video_stem = str(entry.get("basename") or "")
                if video_stem and file_name.startswith(video_stem + "."):
                    tags = file_name[len(video_stem) + 1:].lower().split(".")[:-1]
                    if set(tags) & {"forced", "foreign", "sdh", "hi"}:
                        continue
                    for tag in reversed(tags):
                        tagged_language = inventory.embedded_subtitle_language_suffix("", tag)
                        if "&" in tagged_language or (suffix == "und" and tagged_language != "und"):
                            suffix = tagged_language

            subtitle = {"language_suffix": suffix}
            if suffix != "und" and target_satisfies_auto_subtitle_policy({"subtitles": [subtitle]}, required):
                kind = "外挂" if external else "内嵌"
                label = language_profile_from_text(suffix)["label"]
                return {"status": "skipped", "reason": f"Emby 已检测到符合要求的{kind}字幕（{label}）"}
        self._logger.info(
            "[NeoSubtitleMaster] Emby 已返回当前版本轨道，未找到符合要求的字幕 target=%s subtitle_tracks=%s",
            target.get("label"),
            len(tracks),
        )
        return None

    def _defer_unavailable_directory(self, task: Dict[str, Any]) -> bool:
        owner = self._owner
        if not getattr(owner, "_manual_strm_enabled", False):
            return False
        reason = owner.services.manual_strm_monitor().wait_reason(task.get("entry") or {})
        if not reason:
            return False
        owner._update_auto_transfer_task(
            task["id"], status="pending", message=reason,
            next_run_ts=self._time.time() + 30, directory_waiting=True,
        )
        return True

    def process_transfer_auto_task_batch(self, tasks: List[Dict[str, Any]]) -> None:
        owner = self._owner
        pending = []
        for task in tasks:
            entry = task.get("entry") or {}
            if self._defer_unavailable_directory(task):
                continue
            if entry.get("_directory_auto_search") and not owner.services.local_media_catalog().filter_existing_local_entries([entry]):
                owner._update_auto_transfer_task(task["id"], status="skipped", message="本地整理目录目标已失效")
                continue
            if task.get("bilingual_retry_since") is not None:
                if not self.can_follow_up_bilingual_subtitle(entry):
                    owner._update_auto_transfer_task(task["id"], status="skipped", message="当前设置不适用双语补搜")
                    continue
                if not owner.services.local_media_catalog().filter_existing_local_entries([entry]):
                    owner._update_auto_transfer_task(task["id"], status="skipped", message="补搜目标已失效")
                    continue
            pending.append(task)
        if getattr(owner, "_auto_check_emby_subtitles", False):
            tasks, pending = pending, []
            for task in tasks:
                result = self._check_emby_subtitles_for_task(task) if isinstance(task.get("entry"), dict) else None
                if not result:
                    pending.append(task)
                elif result["status"] == "pending":
                    offline_attempts = result.get("emby_offline_attempts", 0)
                    owner._update_auto_transfer_task(
                        task["id"],
                        status="pending",
                        message=result["reason"],
                        next_run_ts=self._time.time() + result.get("retry_after", 30),
                        emby_rechecked=bool(task.get("emby_rechecked")) or not offline_attempts,
                        emby_offline_attempts=offline_attempts,
                    )
                else:
                    owner._update_auto_transfer_task(
                        task["id"], status="skipped", message=result["reason"], result=result,
                    )
                    self._logger.info(
                        "[NeoSubtitleMaster] %s target=%s", result["reason"], task.get("target_label"),
                    )

        entries = [
            task["entry"] for task in pending
            if isinstance(task.get("entry"), dict) and task.get("bilingual_retry_since") is None
        ]
        task_ids = [task["id"] for task in pending if task.get("id")]
        is_tv_batch = (
            bool(entries)
            and all(owner._normalize_text(entry.get("media_type")) == "tv" for entry in entries)
            and len({self.auto_transfer_group_key(entry) for entry in entries}) == 1
        )
        blocked_providers: Dict[str, SubHDWechatVerificationRequired] = {}
        if is_tv_batch:
            results = self.auto_process_transfer_group(
                entries, task_ids=task_ids, blocked_providers=blocked_providers,
            )
        else:
            results = {
                self._auto_task_result_key(entry): self.auto_process_transfer_entry(
                    entry,
                    queue_rate_limited=True,
                    task_ids=task_ids,
                    blocked_providers=blocked_providers,
                )
                for entry in entries
            }
        waiting_targets = []
        for task in pending:
            entry = task.get("entry") or {}
            if task.get("bilingual_retry_since") is not None:
                result = self.auto_process_transfer_entry(
                    entry, queue_rate_limited=True, task_ids=[task["id"]], blocked_providers=blocked_providers,
                )
            else:
                result = results.get(self._auto_task_result_key(entry))
            result = result or {
                "status": "failed",
                "reason": "入库自动字幕任务没有返回结果",
            }
            if result.get("status") not in {"written", "completed", "ai_submitted"} and self._defer_unavailable_directory(task):
                continue
            if result.get("status") == WAITING_VERIFICATION:
                owner._update_auto_transfer_task(
                    task["id"], status=WAITING_VERIFICATION, next_run_ts=0, bilingual_retry_since=None,
                    message=result["reason"], result=result,
                )
                waiting_targets.append(result.get("target") or task.get("target_label") or task["id"])
                continue
            if result.get("needs_online_retry") and self.can_follow_up_bilingual_subtitle(entry):
                self._queue.defer_bilingual_followup(task, result)
                self._logger.info("[NeoSubtitleMaster] 已安排双语字幕补搜 target=%s reason=%s",
                                  task.get("target_label"), result.get("reason"))
                continue
            status = result.get("status") if result.get("status") in {"completed", "written", "skipped", "failed", "ai_submitted"} else "completed"
            public_status = "completed" if status in {"written", "ai_submitted"} else status
            owner._update_auto_transfer_task(
                task["id"],
                status=public_status,
                message=result.get("reason") or result.get("status") or public_status,
                result=result,
            )
            self._logger.info(
                "[NeoSubtitleMaster] 入库自动字幕处理完成 target=%s strategy=%s status=%s reason=%s",
                result.get("target") or entry.get("target_label") or entry.get("filename"),
                result.get("strategy"),
                result.get("status"),
                result.get("reason", ""),
            )

        if waiting_targets and "subhd" in blocked_providers and not owner._auto_transfer_stopping:
            try:
                from app.schemas.types import NotificationType

                verification = blocked_providers["subhd"]
                owner.post_message(
                    mtype=NotificationType.Plugin,
                    title="NEO字幕大师 · SubHD 待公众号验证",
                    text="待处理\n" + "\n".join(dict.fromkeys(waiting_targets)) + f"\n\n{verification}",
                )
            except Exception as exc:
                self._logger.warning("[NeoSubtitleMaster] 发送 SubHD 验证通知失败: %s", exc)

    def process_transfer_auto_subtitles(self, entries: List[Dict[str, Any]]) -> None:
        owner = self._owner
        for entry in entries:
            try:
                result = self.auto_process_transfer_entry(entry)
                self._logger.info(
                    "[NeoSubtitleMaster] 入库自动字幕处理完成 target=%s strategy=%s status=%s reason=%s",
                    result.get("target"),
                    result.get("strategy"),
                    result.get("status"),
                    result.get("reason", ""),
                )
            except Exception as exc:
                self._logger.warning(
                    "[NeoSubtitleMaster] 入库自动字幕处理失败 target=%s error=%s",
                    entry.get("target_label") or entry.get("filename"),
                    exc,
                )
