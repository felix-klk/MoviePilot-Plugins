"""External playback detection and restart-safe ownership of paused uploads.

Detection follows the MoviePilot speedlimiter design (Shurelol/jxxghp),
using the V3 SDK. No downloader settings are changed.
"""
from __future__ import annotations

import ipaddress
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


def external_address(endpoint) -> bool | None:
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
        raise ValueError("ý���������������Ч�Ự�б�")
    return payload


def video_session(session, jellyfin=False) -> bool | None:
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
    return external_address(field(session, "RemoteEndPoint"))


def detect_external_playback(services, log) -> bool | None:
    """True wins over unknown; False requires successful checks of every server."""
    if not services:
        log.warning("�������ż�⣺û�������õ�ý��������������ϴ�״̬")
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
                    raise ValueError("δ�յ���Ӧ")
                try:
                    if response.status_code != 200:
                        raise ValueError(f"HTTP {response.status_code}")
                    sessions = session_list(response.json())
                finally:
                    response.close()
                states = [video_session(s, kind == "jellyfin") for s in sessions]
            elif kind == "plex":
                plex = service.instance.get_plex()
                if plex is None:
                    raise ValueError("Plex δ����")
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
                        states.append(False if local is not None and as_bool(local)
                                      else external_address(getattr(player, "address", None)))
            else:
                raise ValueError("�ݲ�֧�ִ�ý�����������")
            playing |= any(s is True for s in states)
            unknown |= any(s is None for s in states)
        except Exception as error:
            # Do not include exception text: HTTP errors may contain API keys in URLs.
            log.warning("�������ż�⣺%s ��ѯʧ�ܻ�֧�֣�%s���������ϴ�״̬", name, type(error).__name__)
            unknown = True
    return True if playing else (None if unknown else False)


STATUSES = ("WaitforPreprocessing", "Preprocessing", "Cancelled", "Transfer", "Pause",
            "Finish", "Skipped", "Inqueue", "Ignored", "Error", "FatalError")
ACTIVE = {"WaitforPreprocessing", "Preprocessing", "Transfer", "Inqueue", "Error"}


def upload_status(task):
    value = field(task, "status")
    if isinstance(value, str) and value:
        return value
    value = field(task, "statusEnum", value)
    return STATUSES[value] if isinstance(value, int) and 0 <= value < len(STATUSES) else "Unknown"


def identity(task):
    return {"path": str(field(task, "destPath", "")), "size": str(field(task, "size", 0))}


class UploadGuard:
    """Persist intent before pausing, and clear ownership only after successful resume."""

    def __init__(self, state, save, log, clock=time.monotonic):
        self.state = state if isinstance(state, dict) else {}
        self.save = save
        self.log = log
        self.clock = clock
        self.idle_since = None

    def tick(self, playing, targets, delay=45, enabled=True):
        if enabled and playing is not False:
            self.idle_since = None
            if playing is None:
                return
        elif enabled:
            if self.idle_since is None:
                self.idle_since = self.clock()
            if self.clock() - self.idle_since < delay:
                return
        for target_id, (name, api) in targets.items():
            try:
                if enabled and playing:
                    self.pause(target_id, name, api)
                elif self.state.get(target_id):
                    self.resume(target_id, name, api)
            except Exception as error:
                self.log.warning("CloudDrive2 �������ſ��ƣ�%s ����ʧ�ܣ�%s�����´�����", name, type(error).__name__)

    def pause(self, target_id, name, api):
        tasks = api.list_uploads()
        owned = self.state.setdefault(target_id, {})
        for key, task in tasks.items():
            if upload_status(task) not in ACTIVE:
                continue
            # Durable intent also permits recovery if the response is lost after the server pauses.
            owned[key] = identity(task)
            self.save(self.state)
            api.pause(key)
            self.log.info("%s���������ţ���ͣ�ϴ�����", name)

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
                    api.resume(key)
                    self.log.info("%s�����������ѽ������ָ��ϴ�����", name)
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
        if self.stopping():
            raise RuntimeError("�������ֹͣ")
        method = getattr(self.client.stub, name)
        try:
            return method(request, metadata=self.client.metadata, timeout=15)
        except Exception as error:
            code = getattr(error, "code", lambda: None)()
            if getattr(code, "name", "") != "UNAUTHENTICATED":
                raise
            if self.stopping():
                raise RuntimeError("�������ֹͣ") from None
            token = self.client.stub.GetToken(self.proto.GetTokenRequest(
                userName=self.client.username, password=self.client.password), timeout=15)
            if not token.success or not token.token:
                raise RuntimeError("CloudDrive2 ��¼ʧ��") from None
            self.client.metadata[:] = [("authorization", "Bearer " + token.token)]
            return method(request, metadata=self.client.metadata, timeout=15)

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
                    raise ValueError("�ϴ�����ȱ�� key")
                tasks[key] = task
            if len(tasks) >= total:
                return tasks
            if not rows or len(tasks) == before:
                raise ValueError("�ϴ������ҳ���������������ο���")
        raise ValueError("�ϴ������ҳ��������")

    def pause(self, key):
        self.rpc("PauseUploadFiles", self.proto.MultpleUploadFileKeyRequest(keys=[key]))

    def resume(self, key):
        self.rpc("ResumeUploadFiles", self.proto.MultpleUploadFileKeyRequest(keys=[key]))
