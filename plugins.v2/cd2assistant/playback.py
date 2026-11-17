"""External playback detection and restart-safe ownership of paused uploads.

Detection follows the MoviePilot speedlimiter design (Shurelol/jxxghp),
using MoviePilot configured media servers. No downloader settings are changed.
"""
from __future__ import annotations

import ipaddress
import json
import math
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit


def field(value: Any, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, value.get(name[:1].lower() + name[1:], default))
    return getattr(value, name, default)


def as_bool(value):
    return value.lower() in {"true", "1", "yes"} if isinstance(value, str) else bool(value)


def external_address(endpoint, proxy_networks=()) -> bool | None:
    """None means unknown; never mistake a failed IP parse for an idle server."""
    text = str(endpoint or "").strip()
    if not text:
        return None
    try:
        if "://" in text:
            text = urlsplit(text).hostname or ""
        elif text.startswith("["):
            text = text[1:text.index("]")]
        elif text.count(":") == 1 and "." in text:
            text = text.rsplit(":", 1)[0]
        address = ipaddress.ip_address(text.split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if any(address in network for network in proxy_networks):
            return True
        return not (address.is_private or address.is_loopback or address.is_link_local
                    or address.is_unspecified or address.is_multicast
                    or address in ipaddress.ip_network("100.64.0.0/10"))
    except ValueError:
        return None


def session_list(payload):
    if isinstance(payload, dict):
        for key in ("Items", "Sessions", "Data"):
            value = field(payload, key)
            if value is not None:
                payload = value
                break
    if not isinstance(payload, list) or any(not isinstance(s, dict) for s in payload):
        raise ValueError("媒体服务器返回了无效会话列表")
    return payload


def video_session(session, jellyfin=False, proxy_networks=()) -> bool | None:
    item = field(session, "NowPlayingItem")
    if not item:
        return False
    if str(field(item, "MediaType", "")).lower() != "video":
        return False
    if as_bool(field(field(session, "PlayState", {}), "IsPaused", False)):
        return False
    if jellyfin:
        stamp = field(session, "LastPlaybackCheckIn") or field(session, "LastActivityDate")
        if stamp:
            try:
                parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - parsed).total_seconds() > 120:
                    return False
            except ValueError:
                pass
    return external_address(field(session, "RemoteEndPoint"), proxy_networks)


def detect_external_playback(services, log, proxy_networks=()) -> bool | None:
    """True wins over unknown; False requires successful checks of every server."""
    if not services:
        log.warning("外网播放检测：没有已配置的媒体服务器，保留上传状态")
        return None
    unknown = False
    playing = False
    for name, service in services.items():
        try:
            kind = str(service.type).lower()
            if kind in {"emby", "jellyfin"}:
                path = "emby/Sessions" if kind == "emby" else "Sessions"
                url = "[HOST]" + path + "?api_key=[APIKEY]"
                if kind == "jellyfin":
                    url += "&activeWithinSeconds=120"
                response = service.instance.get_data(url)
                if response is None:
                    raise ValueError("未收到响应")
                try:
                    if response.status_code != 200:
                        raise ValueError(f"HTTP {response.status_code}")
                    sessions = session_list(response.json())
                finally:
                    response.close()
                states = [video_session(s, kind == "jellyfin", proxy_networks) for s in sessions]
            elif kind == "plex":
                plex = service.instance.get_plex()
                if plex is None:
                    raise ValueError("Plex 未连接")
                states = []
                for session in plex.sessions():
                    if str(getattr(session, "TAG", "")).lower() != "video":
                        continue
                    players = getattr(session, "players", None) or [getattr(session, "player", None)]
                    for player in players:
                        status = str(getattr(player, "state", "")).lower()
                        if status in {"paused", "stopped"}:
                            continue
                        if status not in {"playing", "buffering"}:
                            states.append(None)
                            continue
                        # Plex can explicitly identify LAN connections even with public IPv6.
                        local = getattr(player, "local", None)
                        address = getattr(player, "address", None)
                        remote = external_address(address, proxy_networks)
                        if local is not None and as_bool(local) and not (
                            proxy_networks and remote is True and external_address(address) is False
                        ):
                            remote = False
                        states.append(remote)
            else:
                raise ValueError("暂不支持此媒体服务器类型")
            playing |= any(s is True for s in states)
            unknown |= any(s is None for s in states)
        except Exception as error:
            # Do not include exception text: HTTP errors may contain API keys in URLs.
            log.warning("外网播放检测：%s 查询失败或不支持（%s），保留上传状态", name, type(error).__name__)
            unknown = True
    return True if playing else (None if unknown else False)


STATUSES = ("WaitforPreprocessing", "Preprocessing", "Cancelled", "Transfer", "Pause",
            "Finish", "Skipped", "Inqueue", "Ignored", "Error", "FatalError")
ACTIVE = {"WaitforPreprocessing", "Preprocessing", "Transfer", "Inqueue", "Error"}


def upload_status(task):
    numeric = field(task, "statusEnum")
    if isinstance(numeric, int) and numeric != 0:
        return STATUSES[numeric] if 0 <= numeric < len(STATUSES) else "Unknown"
    value = field(task, "status")
    if isinstance(value, str) and value and (numeric != 0 or value in STATUSES):
        return value
    value = field(task, "statusEnum", value)
    return STATUSES[value] if isinstance(value, int) and 0 <= value < len(STATUSES) else "Unknown"


def identity(task):
    return {"path": str(field(task, "destPath", "")), "size": str(field(task, "size", 0))}


class UploadGuard:
    """Persist intent before pausing, and clear ownership only after successful resume."""

    def __init__(self, state, save, log, clock=time.monotonic, *, limits=None, save_limits=None):
        self.state = state if isinstance(state, dict) else {}
        self.save = save
        self.log = log
        self.clock = clock
        self.idle_since = None
        self.limits = limits if isinstance(limits, dict) else {}
        self.save_limits = save_limits
        self.limit_failures = set()

    def pending(self, target):
        return bool(self.state.get(target)) or target in self.limits

    def tick(self, playing, targets, delay=45, enabled=True, limit=0):
        if enabled and playing is not False:
            self.idle_since = None
            if playing is None:
                return
        elif enabled:
            if self.idle_since is None:
                self.idle_since = self.clock()
            if self.clock() - self.idle_since < delay:
                return
        protect = enabled and playing is True
        limited = set()
        for target_id, (name, api) in targets.items():
            if target_id.endswith(":copy"):
                continue
            try:
                if protect and limit:
                    self.apply_limit(target_id, api, limit)
                    limited.add(target_id)
                    if target_id in self.limit_failures:
                        self.log.info("%s：已恢复 CloudDrive2 播放限速", name)
                    self.limit_failures.discard(target_id)
                elif not protect and target_id in self.limits:
                    self.restore_limit(target_id, api)
                    self.log.info("%s：外网播放结束，已处理上传限速恢复", name)
            except Exception as error:
                if protect:
                    if target_id not in self.limit_failures:
                        self.log.warning("%s：CloudDrive2 限速未生效，改为暂停传输，请检查系统设置权限（%s）",
                                         name, type(error).__name__)
                    self.limit_failures.add(target_id)
                else:
                    self.log.warning("%s：CloudDrive2 上传限速恢复失败，将重试（%s）", name, type(error).__name__)
        for target_id, (name, api) in targets.items():
            try:
                if protect and target_id.removesuffix(":copy") not in limited:
                    self.pause(target_id, name, api)
                elif self.state.get(target_id):
                    self.resume(target_id, name, api)
            except Exception as error:
                self.log.warning("CloudDrive2 外网播放控制：%s 操作失败（%s），下次重试", name, type(error).__name__)

    def apply_limit(self, target, api, limit):
        current = api.upload_limit()
        record = self.limits.get(target)
        if 0 < current <= limit:
            return
        original = record["original"] if record and current == record["applied"] else current
        self.limits[target] = {"original": original, "applied": limit}
        self.save_limits(self.limits)
        api.set_upload_limit(limit)
        confirmed = api.upload_limit()
        if not 0 < confirmed <= limit:
            raise RuntimeError("CloudDrive2 未应用上传限速")
        if confirmed != limit:
            self.limits[target]["applied"] = confirmed
            self.save_limits(self.limits)

    def restore_limit(self, target, api):
        record = self.limits[target]
        if api.upload_limit() == record["applied"]:
            api.set_upload_limit(record["original"])
            if api.upload_limit() != record["original"]:
                raise RuntimeError("CloudDrive2 未恢复原上传限速")
        del self.limits[target]
        self.save_limits(self.limits)

    def pause(self, target_id, name, api):
        tasks = api.list_uploads()
        for key, task in tasks.items():
            if upload_status(task) not in ACTIVE:
                continue
            # Durable intent also permits recovery if the response is lost after the server pauses.
            self.state.setdefault(target_id, {})[key] = identity(task)
            self.save(self.state)
            try:
                api.pause(key)
                self.log.info("%s：外网播放，暂停传输任务", name)
            except Exception as error:
                self.log.warning("%s：暂停传输任务失败，将继续处理其他任务（%s）", name, type(error).__name__)

    def resume(self, target_id, name, api):
        tasks = api.list_uploads()
        owned = self.state[target_id]
        for key, expected in list(owned.items()):
            task = tasks.get(key)
            if task is not None and identity(task) == expected:
                status = upload_status(task)
                if status == "Unknown":
                    continue
                if status == "Pause":
                    try:
                        api.resume(key)
                        self.log.info("%s：恢复本插件暂停的传输任务", name)
                    except Exception as error:
                        self.log.warning("%s：恢复传输任务失败，将继续处理其他任务（%s）", name, type(error).__name__)
                        continue
            del owned[key]
            self.save(self.state)
        if not owned:
            self.state.pop(target_id, None)
            self.save(self.state)


class CloudUploadAPI:
    def __init__(self, client, proto, stopping=lambda: False):
        self.client = client
        self.proto = proto
        self.stopping = stopping

    def rpc(self, name, request):
        return self.client.rpc(name, request, stopping=self.stopping)

    def list_uploads(self):
        tasks = {}
        for page in range(10000):
            response = self.rpc("GetUploadFileList", self.proto.GetUploadFileListRequest(
                getAll=False, itemsPerPage=200, pageNumber=page, filter=""))
            rows = field(response, "uploadFiles", [])
            total = int(field(response, "totalCount", 0))
            before = len(tasks)
            for task in rows:
                key = str(field(task, "key", ""))
                if not key:
                    raise ValueError("上传任务缺少 key")
                tasks[key] = task
            if len(tasks) >= total:
                return tasks
            if not rows or len(tasks) == before:
                raise ValueError("上传任务分页不完整，跳过本次控制")
        raise ValueError("上传任务分页超过上限")

    def pause(self, key):
        self.rpc("PauseUploadFiles", self.proto.MultpleUploadFileKeyRequest(keys=[key]))

    def resume(self, key):
        self.rpc("ResumeUploadFiles", self.proto.MultpleUploadFileKeyRequest(keys=[key]))

    def upload_limit(self):
        value = self.rpc("GetSystemSettings", None).maxUploadSpeedKBytesPerSecond
        if not math.isfinite(value) or value < 0:
            raise ValueError("CloudDrive2 返回了无效的上传限速")
        return value

    def set_upload_limit(self, limit):
        self.rpc("SetSystemSettings", self.proto.SystemSettings(maxUploadSpeedKBytesPerSecond=limit))


class CloudCopyAPI(CloudUploadAPI):
    def list_uploads(self):
        tasks = {}
        for task in self.rpc("GetCopyTasks", None).copyTasks:
            if not task.sourcePath or not task.destPath:
                continue
            key = json.dumps([task.sourcePath, task.destPath])
            if task.status in (0, 1, 2):
                status = "Pause" if task.paused else "Transfer"
            else:
                status = {3: "Finish", 4: "FatalError"}.get(task.status, "Unknown")
            tasks[key] = {
                "destPath": task.destPath,
                "size": f"{task.taskMode}:{task.startTime.seconds}:{task.startTime.nanos}",
                "status": status,
            }
        return tasks

    def pause(self, key):
        self._set_paused(key, True)

    def resume(self, key):
        self._set_paused(key, False)

    def _set_paused(self, key, paused):
        source, dest = json.loads(key)
        self.rpc("PauseCopyTask", self.proto.PauseCopyTaskRequest(sourcePath=source, destPath=dest, pause=paused))
