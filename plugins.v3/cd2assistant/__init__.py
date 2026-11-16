from __future__ import annotations

import asyncio
import inspect
import re
import hashlib
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from clouddrive import Client, CloudDriveClient
from clouddrive.proto import CloudDrive_pb2
from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel, ConfigDict

from app import schemas
from app.plugins import _PluginBase
from app.schemas.event import PluginActionEventData
from app.schemas.types import EventType, MessageType
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.services import MediaServerHelper

from .playback import CloudUploadAPI, UploadGuard, detect_external_playback


class CloudDriveInfo(BaseModel):
    """CloudDrive2 ״̬ҳ�� HomePage API ���ȶ�����ģ�͡�"""

    model_config = ConfigDict(extra="ignore")

    cpuUsage: Optional[str] = None
    memUsageKB: Optional[str] = None
    uptime: Optional[str] = None
    fhTableCount: Optional[int] = None
    dirCacheCount: Optional[int] = None
    tempFileCount: Optional[int] = None
    upload_count: int = 0
    download_count: int = 0
    download_speed: str = "0KB/s"
    upload_speed: str = "0KB/s"
    cloud_space: Optional[str] = None


class Cd2Assistant(_PluginBase):
    """��� CloudDrive2 �ϴ���������̵�¼״̬��"""

    plugin_name = "CloudDrive2����"
    plugin_desc = "���CloudDrive2����������ʱ��ͣ�ϴ���ֹͣ���Զ��ָ��������������š�"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/clouddrive.png"
    plugin_version = "3.0.2"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "cd2assistant_"
    plugin_order = 5
    auth_level = 2

    def __init__(self) -> None:
        """��ʼ��ʵ��״̬�����ⲻͬ���ʵ������ CloudDrive �ͻ��ˡ�"""
        super().__init__()
        self._enabled = False
        self._onlyonce = False
        self._cd2_restart = False
        self._cron: Optional[str] = None
        self._notify = False
        self._msgtype: Optional[str] = None
        self._keyword: Optional[str] = None
        self._black_dir = ""
        self._cloud_path = ""
        self._cd2_confs: Optional[str] = None
        self._cd2_clients: Dict[str, CloudDriveClient] = {}
        self._clients: Dict[str, Client] = {}
        self._cd2_url: Dict[str, str] = {}
        self._info: Dict[str, Dict[str, Any]] = {}
        self._scheduler: Optional[BackgroundScheduler] = None
        self._playback_pause = False
        self._playback_interval = 10
        self._playback_resume_delay = 45
        self._playback_lock = threading.Lock()
        self._playback_stopping = True
        self._upload_guard = None
        self._playback_targets = {}

    def init_plugin(self, config: dict | None = None) -> None:
        """��ȡ���ò������ͻ��ˣ���������������������ͳһ������"""
        self.stop_service()
        if self._scheduler is not None or self._cd2_clients or self._clients:
            logger.error("�� CloudDrive2 ��Դδ��ȫֹͣ���������³�ʼ��")
            return
        self._info = {}

        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._notify = bool(config.get("notify", False))
        self._msgtype = config.get("msgtype")
        self._onlyonce = bool(config.get("onlyonce", False))
        self._cd2_restart = bool(config.get("cd2_restart", False))
        self._cron = config.get("cron")
        self._keyword = config.get("keyword")
        self._cd2_confs = config.get("cd2_confs")
        self._black_dir = config.get("black_dir") or ""
        self._cloud_path = config.get("cloud_path") or ""
        self._playback_pause = bool(config.get("playback_pause", False))
        self._playback_interval = self.__bounded_int(config.get("playback_interval"), 10, 5, 300)
        self._playback_resume_delay = self.__bounded_int(config.get("playback_resume_delay"), 45, 0, 3600)
        self._upload_guard = UploadGuard(self.get_data("playback_owned_uploads") or {},
                                        lambda state: self.save_data("playback_owned_uploads", state), logger)
        self.__sync_old_config()

        if not (self._enabled or self._onlyonce or self._cd2_restart or self._upload_guard.state):
            return
        if not self._cd2_confs:
            logger.error("CloudDrive2�������ô�����������")
            return

        self.__create_clients()
        self._playback_stopping = False
        if not self._cd2_clients:
            return

        if self._onlyonce or self._cd2_restart:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

        if self._onlyonce:
            logger.info("CloudDrive2���ֶ�ʱ������������һ��")
            self._scheduler.add_job(
                self.__run_check,
                "date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="CloudDrive2���ֶ�ʱ����",
            )
            self._onlyonce = False
            self.__update_config()

        if self._cd2_restart:
            logger.info("CloudDrive2����������������һ��")
            self._scheduler.add_job(
                self.__run_restart,
                "date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="CloudDrive2��������",
            )
            self._cd2_restart = False
            self.__update_config()

        if self._scheduler and self._scheduler.get_jobs():
            self._scheduler.start()

    def __create_clients(self) -> None:
        """��һ��һ�����ý��� CloudDrive2 �ͻ��ˣ����뵥�������á�"""
        for line in (self._cd2_confs or "").splitlines():
            parts = line.split("#", 3)
            if len(parts) != 4 or not all(parts[:3]):
                logger.error("CloudDrive2�������ø�ʽ������������%s", line.split("#", 1)[0])
                continue

            name, url, username, password = parts
            try:
                cd2_client = CloudDriveClient(url, username, password)
                client = Client(url, username, password)
            except Exception as error:
                logger.error("CloudDrive2��������ʧ�ܣ��������� %s��%s", name, error)
                continue

            old_cd2_client = self._cd2_clients.get(name)
            old_client = self._clients.get(name)
            if old_cd2_client:
                old_cd2_client.close()
            if old_client:
                old_client.close()
            self._cd2_clients[name] = cd2_client
            self._clients[name] = client
            self._cd2_url[name] = url
            target_id = hashlib.sha256(f"{url.rstrip('/')}\n{username}".encode()).hexdigest()
            self._playback_targets[target_id] = (
                name, CloudUploadAPI(client, CloudDrive_pb2, lambda: self._playback_stopping))

    def __sync_old_config(self) -> None:
        """����ʷ��ʵ������ת��Ϊ��ǰ��ʵ�����á�"""
        if self._cd2_confs:
            return
        old_config = self.get_config() or {}
        if not all(old_config.get(key) for key in ("cd2_url", "cd2_username", "cd2_password")):
            return
        self._cd2_confs = (
            f"Ĭ������1#{old_config['cd2_url']}#"
            f"{old_config['cd2_username']}#{old_config['cd2_password']}"
        )
        self.__update_config()

    def __update_config(self) -> None:
        """�־û��ᱻһ�����������ѵ�����״̬��"""
        self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
                "cd2_restart": self._cd2_restart,
                "cron": self._cron,
                "msgtype": self._msgtype,
                "keyword": self._keyword,
                "notify": self._notify,
                "cd2_confs": self._cd2_confs,
                "black_dir": self._black_dir,
                "cloud_path": self._cloud_path,
                "playback_pause": self._playback_pause,
                "playback_interval": self._playback_interval,
                "playback_resume_delay": self._playback_resume_delay,
            }
        )

    @staticmethod
    async def __await_rpc(method, *args, **kwargs):
        """�ȴ� CloudDrive2 ���첽 RPC��ͬʱ����ȷ���ԵĲ���������"""
        result = method(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    async def __listdir(fs: Any, path: str = "") -> list[str]:
        """�� CloudDrive2 ͬ���ļ�ϵͳ��ѯ�Ƴ������¼�ѭ����"""
        if not fs:
            return []
        result = await asyncio.to_thread(fs.listdir, path)
        return list(result or [])

    async def check(self) -> None:
        """�����������ʵ��������״̬���ϴ�����"""
        for name, cd2_client in tuple(self._cd2_clients.items()):
            try:
                await self.__check_cookie(name, cd2_client)
                await self.__check_task(name, cd2_client)
            except Exception as error:
                logger.error("��� CloudDrive2 ʵ�� %s ʧ�ܣ�%s", name, error)

    async def __check_cookie(self, name: str, cd2_client: CloudDriveClient) -> None:
        """������̸�Ŀ¼�Ƿ�ɶ�����ʶ���¼ʧЧ�ͷ���Ƶ�ء�"""
        logger.info("��ʼ��� %s cookie", name)
        fs = cd2_client.fs
        if not fs:
            logger.error("CloudDrive2����ʧ�ܣ���������")
            return

        blacklist = {item.strip() for item in self._black_dir.split(",") if item.strip()}
        for path in await self.__listdir(fs):
            if not path or path in blacklist:
                continue
            error_msg = None
            try:
                if not await self.__listdir(fs, path):
                    logger.warning("���� %s Ϊ��", path)
                    error_msg = f"���� {path} cookie����"
            except Exception as error:
                logger.error("���� %s cookie���ڣ�%s", path, error)
                error_msg = (
                    f"���� {path} ����Ƶ�ʹ��ߣ����Ժ�����"
                    if "429" in str(error)
                    else f"���� {path} cookie����"
                )
            if self._notify and error_msg:
                self.__send_notify(error_msg)

    async def __check_task(self, name: str, cd2_client: CloudDriveClient) -> None:
        """����ϴ����񲢰��ؼ���֪ͨ�쳣����"""
        logger.info("��ʼ��� %s �ϴ�����", name)
        task_list = await self.__await_rpc(
            cd2_client.upload_tasklist.list,
            page=0,
            page_size=10,
            filter="",
            async_=True,
        )
        if not task_list:
            logger.info("û�з����ϴ�����")
            return

        for task in task_list:
            status = self.__field(task, "status")
            error_message = self.__field(task, "errorMessage", "") or ""
            try:
                matched = bool(self._keyword and re.search(self._keyword, str(error_message)))
            except re.error as error:
                logger.error("CloudDrive2���ؼ������ô���%s", error)
                return
            if status == "FatalError" and matched:
                logger.info("�����쳣�ϴ�����%s", error_message)
                if self._notify:
                    self.__send_notify(str(error_message))
                    break

    @staticmethod
    def __field(value: Any, name: str, default: Any = None) -> Any:
        """��ȡ CloudDrive2 protobuf ���ֵ���Ӧ�е��ֶΡ�"""
        if isinstance(value, dict):
            return value.get(name, default)
        return getattr(value, name, default)

    @staticmethod
    def __action_payload(event: Event | None) -> Optional[PluginActionEventData]:
        """��ȡ��������� typed ���գ���ֱ�������ɱ�ԭʼ payload��"""
        if event is None:
            return None
        snapshot = event.snapshot()
        if not snapshot.valid or not isinstance(snapshot.payload, PluginActionEventData):
            logger.warning("CloudDrive2���ֺ�����Ч��������¼���%s", snapshot.errors)
            return None
        return snapshot.payload

    @eventmanager.register(EventType.PluginAction)
    async def restart_cd2(self, event: Event | None = None) -> None:
        """����ָ����ȫ�� CloudDrive2 ʵ����"""
        payload = self.__action_payload(event)
        if event is not None:
            if payload is None or payload.action != "cd2_restart":
                return
            args = str(getattr(payload, "arg_str", "") or "").strip()
        else:
            args = ""

        found = False
        for name, client in tuple(self._clients.items()):
            if args and args.casefold() != str(name).casefold():
                continue
            found = True
            try:
                await self.__await_rpc(client.RestartService, async_=True)
            except Exception as error:
                logger.error("%s CloudDrive2����ʧ�ܣ�%s", name, error)
                if payload is not None:
                    self.post_message(
                        channel=payload.channel,
                        title=f"{name} CloudDrive2����ʧ�ܣ�",
                        userid=payload.user,
                        text=str(error),
                    )
                continue
            logger.info("%s CloudDrive2�����ɹ�", name)
            if payload is not None:
                self.post_message(
                    channel=payload.channel,
                    title=f"{name} CloudDrive2�����ɹ���",
                    userid=payload.user,
                )

        if payload is not None and args and not found:
            self.post_message(
                channel=payload.channel,
                title=f"δ�ҵ� {args} ���ã�",
                userid=payload.user,
            )

    @eventmanager.register(EventType.PluginAction)
    async def add_offline_files(self, event: Event | None = None) -> None:
        """ִ�� CloudDrive2 �����������"""
        payload = self.__action_payload(event)
        if event is None or payload is None or payload.action != "cloud_download":
            return
        raw_args = getattr(payload, "arg_str", "")
        if not raw_args:
            logger.error("ȱ�ٲ�����%s", raw_args)
            return

        args = str(raw_args).replace(" ", "\n")
        cloud_path = self._cloud_path.strip()
        first_line = args.split("\n", 1)[0]
        if first_line.startswith("/"):
            cloud_path = first_line
            args = args[len(first_line):].lstrip("\n")
        if not cloud_path:
            logger.error("������������·��")
            if payload.user:
                self.post_message(channel=payload.channel, title="������������·����", userid=payload.user)
            return

        client = next((item for item in self._clients.values() if item), None)
        if not client:
            logger.error("CloudDrive2����û�п�������")
            if payload.user:
                self.post_message(channel=payload.channel, title="CloudDrive2����ʧ�ܣ�", userid=payload.user)
            return

        logger.info("��ȡ����������·����%s", cloud_path)
        logger.info("��ʼ�������أ�%s", args)
        try:
            result = await self.__await_rpc(
                client.AddOfflineFiles,
                CloudDrive_pb2.AddOfflineFileRequest(urls=args, toFolder=cloud_path),
                async_=True,
            )
        except Exception as error:
            logger.error("��������ʧ�ܣ�%s", error)
            if payload.user:
                self.post_message(
                    channel=payload.channel,
                    title="��������ʧ�ܣ�",
                    userid=payload.user,
                    text=f"������Ϣ��{error}",
                )
            return

        if self.__field(result, "success", False):
            logger.info("�������سɹ�")
            if payload.user:
                self.post_message(channel=payload.channel, title=f"{cloud_path} �������سɹ���", userid=payload.user)
            return

        error_message = self.__field(result, "errorMessage")
        logger.error("��������ʧ�ܣ�%s", error_message)
        if payload.user:
            self.post_message(
                channel=payload.channel,
                title="��������ʧ�ܣ�",
                userid=payload.user,
                text=f"������Ϣ��{error_message}",
            )

    @eventmanager.register(EventType.PluginAction)
    async def cd2_info(self, event: Event | None = None) -> None:
        """ͨ�����������Ϣ���� CloudDrive2 ����״̬��"""
        payload = self.__action_payload(event)
        if event is None or payload is None or payload.action != "cd2_info":
            return
        args = str(getattr(payload, "arg_str", "") or "").strip()
        found = False
        for name, client in tuple(self._clients.items()):
            if args and args.casefold() != str(name).casefold():
                continue
            found = True
            cd2_client = self._cd2_clients.get(name)
            try:
                info = await self.__get_cd2_info(event=event, client=client, cd2_client=cd2_client)
                self._info[name] = info
            except Exception as error:
                logger.error("��ȡ %s CloudDrive2 ��Ϣʧ�ܣ�%s", name, error)

        if args and not found:
            self.post_message(channel=payload.channel, title=f"δ�ҵ� {args} ���ã�", userid=payload.user)

    @staticmethod
    def __message_to_dict(value: Any) -> dict[str, Any]:
        """�� protobuf���ֵ���ı���Ӧ��һΪ�ɼ����ֵ䡣"""
        if not value:
            return {}
        if isinstance(value, dict):
            return value
        try:
            return MessageToDict(value)
        except (TypeError, ValueError):
            matches = re.findall(r"(\w+): ([\d.]+)", str(value))
            return {key: float(number) for key, number in matches}

    @staticmethod
    def __number(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    async def __get_cloud_space(self, cd2_client: CloudDriveClient | None) -> Optional[str]:
        """�첽��ȡ�����̿ռ䣬�ҽ�Ŀ¼ö�ٷŽ��̱߽߳硣"""
        if not cd2_client or not cd2_client.fs:
            logger.error("CloudDrive2����ʧ�ܣ���������")
            return None

        blacklist = {item.strip() for item in self._black_dir.split(",") if item.strip()}
        space_info_text = "\n"
        for path in await self.__listdir(cd2_client.fs):
            if not path or path in blacklist:
                continue
            try:
                result = await self.__await_rpc(
                    cd2_client.GetSpaceInfo,
                    CloudDrive_pb2.FileRequest(path=path),
                    async_=True,
                )
                values = self.__message_to_dict(result)
                total = self.__convert_bytes(self.__number(values.get("totalSpace")))
                used = self.__convert_bytes(self.__number(values.get("usedSpace")))
                space_info_text += f"{path}��{used}/{total}\n"
            except Exception as error:
                logger.error("��ȡ���� %s �ռ���Ϣʧ�ܣ�%s", path, error)
        return space_info_text

    async def __get_cd2_info(
        self,
        event: Event | None = None,
        client: Client | None = None,
        cd2_client: CloudDriveClient | None = None,
    ) -> dict[str, Any]:
        """��ȡ CloudDrive2 ϵͳ״̬�����־ɽӿ��ֶ�����"""
        if not client or not cd2_client:
            return {}
        system_info = self.__message_to_dict(
            await self.__await_rpc(client.GetRunningInfo, async_=True)
        )
        task_count = self.__message_to_dict(
            await self.__await_rpc(client.GetAllTasksCount, async_=True)
        )
        download_files = self.__message_to_dict(
            await self.__await_rpc(client.GetDownloadFileList, async_=True)
        )
        upload_files = self.__message_to_dict(
            await self.__await_rpc(
                client.GetUploadFileList,
                CloudDrive_pb2.GetUploadFileListRequest(getAll=True),
                async_=True,
            )
        )
        cloud_space = await self.__get_cloud_space(cd2_client)

        cpu_usage = self.__number(system_info.get("cpuUsage"))
        memory_kb = self.__number(system_info.get("memUsageKB"))
        uptime = self.__number(system_info.get("uptime"))
        download_speed = self.__number(download_files.get("globalBytesPerSecond"))
        upload_speed = self.__number(upload_files.get("globalBytesPerSecond"))
        info = {
            "cpuUsage": f"{cpu_usage:.2f}%" if system_info else None,
            "memUsageKB": f"{memory_kb / 1024:.2f}MB" if system_info else None,
            "uptime": self.convert_seconds(uptime) if system_info else None,
            "fhTableCount": int(self.__number(system_info.get("fhTableCount"))) if system_info else None,
            "dirCacheCount": int(self.__number(system_info.get("dirCacheCount"))) if system_info else None,
            "tempFileCount": int(self.__number(system_info.get("tempFileCount"))) if system_info else None,
            "upload_count": int(self.__number(task_count.get("uploadCount"))),
            "download_count": int(self.__number(task_count.get("downloadCount"))),
            "download_speed": f"{download_speed / 1024 / 1024:.2f}MB/s" if download_speed else "0KB/s",
            "upload_speed": f"{upload_speed / 1024 / 1024:.2f}MB/s" if upload_speed else "0KB/s",
            "cloud_space": cloud_space,
        }
        logger.info("��ȡCloudDrive2ϵͳ��Ϣ��\n%s", info)

        if event is not None:
            payload = self.__action_payload(event)
            if payload is not None:
                self.post_message(
                    channel=payload.channel,
                    title="CloudDrive2ϵͳ��Ϣ",
                    userid=payload.user,
                    text=(
                        f"CPUռ�ã�{info['cpuUsage']}\n"
                        f"�ڴ�ռ�ã�{info['memUsageKB']}\n"
                        f"����ʱ�䣺{info['uptime']}\n"
                        f"���ļ�������{info['fhTableCount']}\n"
                        f"Ŀ¼����������{info['dirCacheCount']}\n"
                        f"��ʱ�ļ�������{info['tempFileCount']}\n"
                        f"�ϴ�����������{info['upload_count']}\n"
                        f"��������������{info['download_count']}\n"
                        f"�����ٶȣ�{info['download_speed']}\n"
                        f"�ϴ��ٶȣ�{info['upload_speed']}\n"
                        f"�洢�ռ䣺{info['cloud_space']}\n"
                    ),
                )
        return info

    async def homepage(self, name: str | None = None) -> CloudDriveInfo:
        """����ָ�� CloudDrive2 ʵ���� HomePage ״̬��"""
        for cd2_name, client in self._clients.items():
            if name and str(cd2_name) != name:
                continue
            info = await self.__get_cd2_info(client=client, cd2_client=self._cd2_clients.get(cd2_name))
            self._info[cd2_name] = info
            return CloudDriveInfo.model_validate(info)
        return CloudDriveInfo()

    async def __refresh_info(self) -> None:
        """ˢ������ҳ���Ǳ��干�õ� CloudDrive2 ״̬���ա�"""
        for name, client in tuple(self._clients.items()):
            try:
                self._info[name] = await self.__get_cd2_info(
                    client=client,
                    cd2_client=self._cd2_clients.get(name),
                )
            except Exception as error:
                logger.error("��ȡ %s CloudDrive2 ��Ϣʧ�ܣ�%s", name, error)

    def __refresh_info_sync(self) -> None:
        """������ͬ��ҳ�� ABI �����첽״̬�ɼ���"""
        try:
            asyncio.run(self.__refresh_info())
        except Exception as error:
            logger.error("ˢ�� CloudDrive2 ҳ��״̬ʧ�ܣ�%s", error)

    @staticmethod
    def __convert_bytes(size_in_bytes: float) -> str:
        """���ֽ���ת��Ϊ�ʺ�״̬ҳչʾ�ĵ�λ��"""
        units = ("B", "KB", "MB", "GB", "TB", "PB")
        unit_index = 0
        while size_in_bytes >= 1024 and unit_index < len(units) - 1:
            size_in_bytes /= 1024
            unit_index += 1
        return f"{size_in_bytes:.2f} {units[unit_index]}"

    @staticmethod
    def convert_seconds(seconds: float) -> str:
        """����������ת��Ϊ����ʱ����"""
        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        parts = []
        if days > 0:
            parts.append(f"{int(days)}��")
        if hours > 0:
            parts.append(f"{int(hours)}Сʱ")
        if minutes > 0:
            parts.append(f"{int(minutes)}����")
        if seconds > 0 or not parts:
            parts.append(f"{seconds:.0f}��")
        return "".join(parts)

    def __send_notify(self, message: str) -> None:
        """���ͼ��֪ͨ��������Ч��Ϣ���ͻ��˵��ֶ�������"""
        try:
            message_type = MessageType[str(self._msgtype)] if self._msgtype else MessageType.Manual
        except KeyError:
            message_type = MessageType.Manual
        self.post_message(title="CloudDrive2����֪ͨ", mtype=message_type, text=message)

    def get_state(self) -> bool:
        """���ز���Ƿ����á�"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """ע�� CloudDrive2 ������״̬�������������"""
        return [
            {
                "cmd": "/cd2_restart",
                "event": EventType.PluginAction,
                "desc": "CloudDrive2����",
                "category": "",
                "data": {"action": "cd2_restart"},
            },
            {
                "cmd": "/cd2_info",
                "event": EventType.PluginAction,
                "desc": "CloudDrive2ϵͳ��Ϣ",
                "category": "",
                "data": {"action": "cd2_info"},
            },
            {
                "cmd": "/cd",
                "event": EventType.PluginAction,
                "desc": "������",
                "category": "",
                "data": {"action": "cloud_download"},
            },
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """ע�������� API Key ��Ȩ�� HomePage ״̬�ӿڡ�"""
        return [
            {
                "path": "/homepage",
                "endpoint": self.homepage,
                "methods": ["GET"],
                "auth": "apikey",
                "response_model": CloudDriveInfo,
                "summary": "HomePage",
                "description": "HomePage�Զ���api",
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """�����ڼ��ע�ᵽ������������"""
        services = []
        if self._clients and self._upload_guard and (
            (self._enabled and self._playback_pause) or self._upload_guard.state
        ):
            services.append({
                "id": "Cd2AssistantPlayback", "name": "CloudDrive2����������ͣ�ϴ�",
                "trigger": "interval", "func": self.check_playback,
                "kwargs": {"seconds": self._playback_interval, "max_instances": 1, "coalesce": True},
            })
        if not self._enabled or not self._cron or not self._cd2_clients:
            return services
        try:
            trigger = CronTrigger.from_crontab(self._cron, timezone=settings.TZ)
        except Exception as error:
            logger.error("��ʱ�������ô���%s", error)
            return services
        return services + [
            {
                "id": "Cd2Assistant",
                "name": "CloudDrive2���ּ�����",
                "trigger": trigger,
                "func": self.check,
                "kwargs": {},
            }
        ]

    @staticmethod
    def __bounded_int(value, default, minimum, maximum):
        try:
            return max(minimum, min(maximum, int(value)))
        except (TypeError, ValueError):
            return default

    def check_playback(self) -> None:
        """ͬ���������������� Webhook����ѯ�Ͳ���رջ��ཻ����"""
        if self._playback_stopping or not self._playback_lock.acquire(blocking=False):
            return
        try:
            if self._playback_stopping or self._upload_guard is None:
                return
            enabled = self._enabled and self._playback_pause
            playing = None
            if enabled:
                try:
                    playing = detect_external_playback(MediaServerHelper().get_services(), logger)
                except Exception:
                    logger.warning("��ȡý�������ʧ�ܣ����� CloudDrive2 �ϴ�״̬")
            if self._playback_stopping:
                return
            self._upload_guard.tick(playing, self._playback_targets,
                                    self._playback_resume_delay, enabled=enabled)
        except Exception as error:
            logger.warning("�������ż��ʧ�ܣ�%s�����´�����", type(error).__name__)
        finally:
            self._playback_lock.release()

    @eventmanager.register(EventType.WebhookMessage)
    async def playback_webhook(self, event: Event | None = None) -> None:
        """Webhook ���ӿ��ѯ����ֱ�Ӱѵ���ֹͣ�¼���Ϊ������ֹͣ���š�"""
        if not event or not self._enabled or not self._playback_pause:
            return
        snapshot = event.snapshot()
        if not snapshot.valid:
            return
        event_name = str(getattr(snapshot.payload, "event", "") or "").lower()
        if event_name in {"playback.start", "playback.stop", "playback.pause", "playback.unpause",
                          "playbackstart", "playbackstop", "playbackpause", "playbackunpause",
                          "media.play", "media.stop", "media.pause", "media.resume"}:
            await asyncio.to_thread(self.check_playback)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """���ز�����ñ�����Ĭ�����á�"""
        message_options = [
            {"title": item.value, "value": item.name}
            for item in MessageType
        ]
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            self.__field_control("VSwitch", "enabled", "���ò��", 3),
                            self.__field_control("VSwitch", "notify", "����֪ͨ", 3),
                            self.__field_control("VSwitch", "cd2_restart", "cd2����һ��", 3),
                            self.__field_control("VSwitch", "onlyonce", "��������һ��", 3),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__field_control("VSwitch", "playback_pause", "��������ʱ��ͣ�ϴ�������������", 6),
                            self.__field_control("VTextField", "playback_interval", "���ż�������룬5��300��", 3),
                            self.__field_control("VTextField", "playback_resume_delay", "�����������ź�ָ��ӳ٣��룩", 3),
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {"type": "info", "variant": "tonal", "text":
                            "��ȡ MP �����õ� Emby/Jellyfin/Plex���������Ų���ͣ�ϴ�������������Ƶֹͣ����ͣ���ӳٻָ���"
                            "ֻ�ָ���������ͣ�����񡣷������������ý�������ʶ����ʵ�ͻ��� IP���رձ����ܺ������һ�μ��ָ�����ͣ������"},
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "cd2_confs",
                                            "label": "cd2����",
                                            "rows": 2,
                                            "placeholder": "cd2����1#http://127.0.0.1:19798#admin#123456��һ��һ�����ã�",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__field_control("VCronField", "cron", "�������", 4, "5λcron����ʽ"),
                            self.__field_control("VTextField", "keyword", "���ؼ���", 4),
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": False,
                                            "chips": True,
                                            "model": "msgtype",
                                            "label": "��Ϣ����",
                                            "items": message_options,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__field_control("VTextField", "black_dir", "cd2������Ŀ¼", 4, "cd2�����ӵı���Ŀ¼(���Ŀ¼��Ӣ�Ķ��ŷָ�)"),
                            self.__field_control("VTextField", "cloud_path", "������·��", 4),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "���ڼ��CloudDrive2�ϴ����񣬼���Ƿ����м��ؼ��ʣ�����֪ͨ��",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "���ڼ��CloudDrive2����CK�Ƿ���ڣ�����֪ͨ�����صı���·�������Ӻ���������",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {"type": "success", "variant": "tonal"},
                                        "content": [
                                            {"component": "span", "text": "HomePage���ý̳���ο���"},
                                            {
                                                "component": "a",
                                                "props": {
                                                    "href": "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/docs/Cd2Assistant.md",
                                                    "target": "_blank",
                                                },
                                                "text": "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/docs/Cd2Assistant.md",
                                            },
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": False,
            "onlyonce": False,
            "cd2_restart": False,
            "cron": "*/10 * * * *",
            "keyword": "�˺��쳣",
            "cd2_confs": "",
            "msgtype": "Manual",
            "black_dir": "",
            "cloud_path": "",
            "playback_pause": False,
            "playback_interval": 10,
            "playback_resume_delay": 45,
        }

    @staticmethod
    def __field_control(component: str, model: str, label: str, md: int, placeholder: str | None = None) -> dict:
        """�������ñ����еĵ��ֶ��С�"""
        props = {"model": model, "label": label}
        if placeholder:
            props["placeholder"] = placeholder
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [{"component": component, "props": props}],
        }

    @staticmethod
    def __metric_card(label: str, value: Any) -> dict[str, Any]:
        """����������ҳ���Ǳ��干�õ�״̬��Ƭ��"""
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": 3, "sm": 6},
            "content": [
                {
                    "component": "VCard",
                    "props": {"variant": "tonal"},
                    "content": [
                        {
                            "component": "VCardText",
                            "props": {"class": "d-flex align-center"},
                            "content": [
                                {
                                    "component": "div",
                                    "content": [
                                        {"component": "span", "props": {"class": "text-caption"}, "text": label},
                                        {"component": "span", "props": {"class": "text-h6"}, "text": value},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }

    def __render_instance(self, name: str, info: dict[str, Any]) -> dict[str, Any]:
        """��Ⱦ���� CloudDrive2 ʵ������״̬�ֶΡ�"""
        values = [
            ("CPUռ��", info.get("cpuUsage")),
            ("�ڴ�ռ��", info.get("memUsageKB")),
            ("����ʱ��", info.get("uptime")),
            ("���ļ���", info.get("fhTableCount")),
            ("����Ŀ¼��", info.get("dirCacheCount")),
            ("��ʱ�ļ���", info.get("tempFileCount")),
            ("����������", info.get("download_count", 0)),
            ("�ϴ�������", info.get("upload_count", 0)),
            ("��������", info.get("download_speed", "0KB/s")),
            ("�ϴ�����", info.get("upload_speed", "0KB/s")),
            ("�洢�ռ�", info.get("cloud_space")),
        ]
        cards = [
            {
                "component": "VCol",
                "props": {"cols": 12},
                "content": [{"component": "VCard", "props": {"variant": "tonal"}, "content": [{"component": "VCardText", "text": f"{name} ({self._cd2_url.get(name, '')})"}]}],
            }
        ]
        cards.extend(self.__metric_card(label, value) for label, value in values)
        return {"component": "VRow", "content": cards}

    def get_page(self) -> List[dict]:
        """ˢ��״̬�󷵻ز������ҳ��"""
        self.__refresh_info_sync()
        return [self.__render_instance(name, self._info.get(name, {})) for name in self._clients]

    def get_dashboard(self, key: str | None = None, **kwargs) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], List[dict]]]:
        """ˢ��״̬�󷵻��Ǳ��岼�֡�"""
        if not self._clients:
            return {
                "cols": 12,
                "md": 12,
            }, {"refresh": 10, "border": False}, [{"component": "div", "text": "�޷�����CloudDrive2"}]
        self.__refresh_info_sync()
        elements = [self.__render_instance(name, self._info.get(name, {})) for name in self._clients]
        return {"cols": 12, "md": 12}, {"refresh": 10, "border": False}, elements

    def stop_service(self) -> None:
        """ֹͣһ���Ե��������ͷ� CloudDrive2 ���ӡ�"""
        self._playback_stopping = True
        # The worker uses only synchronous SDK/RPC calls, so joining it does not
        # depend on the host event loop. Keep ownership persisted across restarts.
        with self._playback_lock:
            self._playback_targets = {}
            self.__close_services()

    def __close_services(self) -> None:
        scheduler = self._scheduler
        if scheduler:
            try:
                scheduler.remove_all_jobs()
                if scheduler.running:
                    scheduler.shutdown(wait=False)
            except Exception as error:
                logger.error("�˳����ʧ�ܣ�%s", error)
            else:
                self._scheduler = None

        for clients in (self._cd2_clients, self._clients):
            for name, client in tuple(clients.items()):
                try:
                    client.close()
                except Exception as error:
                    logger.debug("�ر� CloudDrive2 ����ʧ�ܣ�%s", error)
                    continue
                clients.pop(name, None)
        for name in tuple(self._cd2_url):
            if name not in self._cd2_clients and name not in self._clients:
                self._cd2_url.pop(name, None)

    def __run_check(self) -> None:
        """Ϊͬ�� APScheduler һ���������Ž��첽��⡣"""
        try:
            asyncio.run(self.check())
        except Exception as error:
            logger.error("CloudDrive2����һ���Լ��ʧ�ܣ�%s", error)

    def __run_restart(self) -> None:
        """Ϊͬ�� APScheduler һ���������Ž��첽������"""
        try:
            asyncio.run(self.restart_cd2())
        except Exception as error:
            logger.error("CloudDrive2����һ��������ʧ�ܣ�%s", error)
