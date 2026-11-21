from __future__ import annotations

from asyncio import to_thread
from pathlib import Path
from typing import Any, Dict

from fastapi import HTTPException, Request

from ..config.config_schema import host_from_url
from ..timeline.timeline_fixer import check_timeline_fixer_dependencies


class StatusApi:
    def __init__(self, owner: Any):
        self.owner = owner

    def scan_local_directories(self) -> Dict[str, Any]:
        monitor = self.owner.services.manual_strm_monitor()
        status = monitor.status()
        if not self.owner.get_state() or not status["running"]:
            raise HTTPException(status_code=400, detail="请先保存并启用本地整理目录监控")
        if not self.owner.services.auto_transfer().auto_search_providers():
            raise HTTPException(status_code=400, detail="请先保存并启用至少一个在线字幕源")
        started = monitor.request_scan()
        return self.owner._ok({"started": started, "scanning": monitor.status()["scanning"]})

    def status(self) -> Dict[str, Any]:
        owner = self.owner
        services = owner.services
        archive_dependency = services.archive_dependency()
        rar_tool = archive_dependency.rar_tool()
        rar_python = archive_dependency.rar_python_available()
        local_media_catalog = services.local_media_catalog()
        auto_transfer = services.auto_transfer()
        autosub_bridge = services.autosub_bridge()
        return owner._ok(
            {
                "enabled": owner.get_state(),
                "auto_search_on_transfer": bool(owner._auto_search_on_transfer),
                "auto_skip_chinese_media_on_transfer": bool(owner._auto_skip_chinese_media_on_transfer),
                "auto_search_on_manual_strm": bool(owner._auto_search_on_manual_strm),
                "auto_transfer_subtitle_strategy": owner._auto_transfer_subtitle_strategy,
                "traditional_to_simplified": bool(owner._traditional_to_simplified),
                "source": "MoviePilot 本地整理记录 + 本地整理目录监控",
                "index": local_media_catalog.cache_status(),
                "manual_strm_monitor": services.manual_strm_monitor().status(),
                "archive_support": {
                    "zip": True,
                    "rar": bool(rar_tool),
                    "rar_tool": Path(rar_tool).name if rar_tool else "",
                    "rar_tool_path": rar_tool or owner._rar_tool_path,
                    "rar_python": rar_python,
                    "rar_python_package": owner._rar_python_package,
                    "dependency_mode": owner._rar_dependency_mode,
                    "dependency_status": owner._rar_dependency_status,
                },
                "timeline_fixer": {
                    **check_timeline_fixer_dependencies(),
                    "configured_max_offset_seconds": owner._timeline_max_offset_seconds,
                    "configured_min_offset_seconds": owner._timeline_min_offset_seconds,
                    "vad_mode": owner._timeline_vad_mode,
                    "allow_risky_offset": bool(owner._timeline_allow_risky_offset),
                },
                "online_search": {
                    "enabled_providers": owner._online_provider_ids,
                    "assrt_api_configured": bool(owner._assrt_api_key),
                    "assrt_api_host": host_from_url(owner._assrt_api_url),
                    "opensubtitles_api_configured": bool(owner._opensubtitles_api_key),
                    "opensubtitles_api_host": host_from_url(owner._opensubtitles_api_url),
                    "opensubtitles_download_configured": bool(
                        owner._opensubtitles_username and owner._opensubtitles_password
                    ),
                },
                "auto_transfer_queue": auto_transfer.auto_transfer_queue_summary(),
                "ai_subtitle": autosub_bridge.autosub_status(),
            }
        )

    def emby_query_libraries(self, server: str = "") -> Dict[str, Any]:
        service = self.owner.services.emby_refresh()
        try:
            servers = service.configured_emby_names()
            if not server:
                return self.owner._ok({"servers": servers})
            if server not in servers:
                raise HTTPException(status_code=404, detail="该 Emby 服务器未启用或已移除")
            return self.owner._ok({"libraries": service.libraries_for_server(server)})
        except (ImportError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail="无法读取 Emby 媒体库，请检查该连接后重试") from exc

    def refresh_index(self) -> Dict[str, Any]:
        owner = self.owner
        local_media_catalog = owner.services.local_media_catalog()
        local_media_catalog.start_background_cache_refresh()
        cache_status = local_media_catalog.cache_status()
        has_cache = bool((owner._local_entries_cache or {}).get("entries"))
        message = "媒体库资源清单已在后台刷新"
        if has_cache:
            message += "，当前页面先使用已有缓存"
        else:
            message += "，首次刷新完成前列表可能暂时为空"
        return owner._ok(
            {
                "realtime": False,
                "background": True,
                "index": cache_status,
            },
            message=message,
        )

    def auto_transfer_queue(self, request: Request) -> Dict[str, Any]:
        owner = self.owner
        limit = min(max(owner._safe_int(request.query_params.get("limit"), 100), 1), 200)
        return owner._ok(owner.services.auto_transfer().auto_transfer_queue_snapshot(limit=limit))

    async def retry_auto_transfer_task(self, request: Request) -> Dict[str, Any]:
        owner = self.owner
        body = await request.json()
        task_id = owner._normalize_text(body.get("task_id")) if isinstance(body, dict) else ""
        force_low_confidence = bool(body.get("force_low_confidence")) if isinstance(body, dict) else False
        if not task_id:
            raise HTTPException(status_code=400, detail="缺少自动入库任务 ID")
        service = owner.services.auto_transfer()
        if not service.retry_auto_transfer_task(task_id, force_low_confidence=force_low_confidence):
            raise HTTPException(status_code=409, detail="任务不存在、仍在处理中或不允许重试")
        message = "已强制重新处理低可信调轴任务" if force_low_confidence else "已重新加入自动入库队列"
        return owner._ok(service.auto_transfer_queue_snapshot(limit=200), message=message)

    async def clear_auto_transfer_history(self, request: Request) -> Dict[str, Any]:
        owner = self.owner
        service = owner.services.auto_transfer()
        cleared = service.clear_auto_transfer_history()
        return owner._ok(
            {**service.auto_transfer_queue_snapshot(limit=200), "cleared": cleared},
            message=f"已清空 {cleared} 条自动入库历史任务",
        )

    async def enqueue_bilingual_followup(self, request: Request) -> Dict[str, Any]:
        owner = self.owner
        body = await request.json()
        if isinstance(body, dict) and "target_ids" in body:
            return await to_thread(self._enqueue_bilingual_followup_batch, body)
        target_id = owner._normalize_text(body.get("target_id")) if isinstance(body, dict) else ""
        if not target_id:
            raise HTTPException(status_code=400, detail="缺少剧集目标 ID")
        entry = owner.services.local_media_catalog().resolve_targets([target_id]).get(target_id)
        if not entry:
            raise HTTPException(status_code=409, detail="剧集目标已失效，请重新选择")
        service = owner.services.auto_transfer()
        if not owner.get_state() or not service.can_follow_up_bilingual_subtitle(entry):
            raise HTTPException(status_code=422, detail="双语补搜需要已启用的插件、剧集目标、双语语言选项和在线字幕源")
        queued, _ = service.enqueue_transfer_auto_entries([entry])
        if not queued:
            raise HTTPException(status_code=409, detail="已在队列中、刚处理过或目标不可用")
        return owner._ok(service.auto_transfer_queue_snapshot(limit=200), message="已加入双语字幕补搜")

    def _enqueue_bilingual_followup_batch(self, body: Dict[str, Any]) -> Dict[str, Any]:
        owner = self.owner
        raw_ids = body["target_ids"]
        if not isinstance(raw_ids, list) or not raw_ids or any(
            not isinstance(target_id, str) or not target_id.strip() for target_id in raw_ids
        ):
            raise HTTPException(status_code=400, detail="请勾选需要补搜的剧集目标")
        target_ids = list(dict.fromkeys(target_id.strip() for target_id in raw_ids))
        raw_locked_ids = body.get("locked_target_ids", [])
        if not isinstance(raw_locked_ids, list) or any(not isinstance(item, str) for item in raw_locked_ids):
            raise HTTPException(status_code=400, detail="锁定目标列表格式无效")
        locked_ids = {item.strip() for item in raw_locked_ids}
        if not owner.get_state():
            raise HTTPException(status_code=422, detail="请先启用字幕插件")
        service = owner.services.auto_transfer()
        entries = owner.services.local_media_catalog().resolve_targets(target_ids)
        results = []
        pending = []
        for target_id in target_ids:
            entry = entries.get(target_id)
            result = {"target_id": target_id, "label": (entry or {}).get("target_label") or target_id}
            results.append(result)
            if target_id in locked_ids:
                result["status"] = "locked"
            elif not entry:
                result["status"] = "unavailable"
            elif entry.get("writable") is False:
                result["status"] = "read_only"
            elif not service.can_follow_up_bilingual_subtitle(entry):
                result["status"] = "ineligible"
            else:
                pending.append((entry, result))

        if pending:
            outcomes = service.enqueue_transfer_auto_entries_with_outcomes([entry for entry, _ in pending])
            for (_, result), outcome in zip(pending, outcomes, strict=True):
                result["status"] = outcome

        queued = sum(result["status"] == "queued" for result in results)
        already_active = sum(result["status"] == "already_active" for result in results)
        skipped = len(results) - queued - already_active
        parts = [f"已加入 {queued} 个补搜任务"]
        if already_active:
            parts.append(f"{already_active} 个已在队列")
        if skipped:
            parts.append(f"{skipped} 个未加入")
        return owner._ok(
            {
                **service.auto_transfer_queue_snapshot(limit=200),
                "followup_batch": {"queued": queued, "already_active": already_active, "skipped": skipped, "items": results},
            },
            message="，".join(parts),
        )

    async def cancel_bilingual_followup(self, request: Request) -> Dict[str, Any]:
        owner = self.owner
        body = await request.json()
        task_id = owner._normalize_text(body.get("task_id")) if isinstance(body, dict) else ""
        service = owner.services.auto_transfer()
        if not task_id or not service.cancel_bilingual_followup(task_id):
            raise HTTPException(status_code=409, detail="只有等待补搜的任务可以取消")
        return owner._ok(service.auto_transfer_queue_snapshot(limit=200), message="已取消双语字幕补搜")
