from __future__ import annotations

import asyncio
import re
import hashlib
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel, ConfigDict

from app import schemas
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType

from .cd2client import CloudDriveConnection, proto
from .health import inspect_health
from .playback import CloudUploadAPI, UploadGuard, detect_external_playback


class CloudDriveInfo(BaseModel):
    """CloudDrive2 状态页和 HomePage API 的稳定返回模型。"""

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
    """监控 CloudDrive2 上传任务和云盘登录状态。"""

    plugin_name = "CloudDrive2助手 NEO(自用版)"
    plugin_desc = "监控CloudDrive2，外网播放时暂停上传，停止后自动恢复，忽略内网播放。"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/clouddrive.png"
    plugin_version = "2.1.1"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "cd2assistant_"
    plugin_order = 5
    auth_level = 2

    def __init__(self) -> None:
        """初始化实例状态，避免不同插件实例共享 CloudDrive 客户端。"""
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
        self._clients: Dict[str, CloudDriveConnection] = {}
        self._cd2_url: Dict[str, str] = {}
        self._info: Dict[str, Dict[str, Any]] = {}
        self._scheduler: Optional[BackgroundScheduler] = None
        self._playback_pause = False
        self._playback_interval = 10
        self._playback_resume_delay = 45
        self._health_lock = threading.Lock()
        self._health_alerts = {}
        self._playback_lock = threading.Lock()
        self._playback_stopping = True
        self._upload_guard = None
        self._playback_targets = {}

    def init_plugin(self, config: dict | None = None) -> None:
        """读取配置并建立客户端；周期任务由宿主调度器统一管理。"""
        self.stop_service()
        if self._scheduler is not None or self._clients:
            logger.error("旧 CloudDrive2 资源未完全停止，跳过重新初始化")
            return
        self._info = {}

        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._notify = bool(config.get("notify", False))
        self._msgtype = config.get("msgtype")
        self._onlyonce = bool(config.get("onlyonce", False))
        self._cd2_restart = bool(config.get("cd2_restart", False))
        self._cron = config.get("cron", "*/10 * * * *")
        self._health_alerts = self.get_data("health_alerts") or {}
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
            logger.error("CloudDrive2助手配置错误，请检查配置")
            return

        self.__create_clients()
        self._playback_stopping = False
        if not self._clients:
            return

        recover_uploads = not self._enabled and any(
            self._upload_guard.state.get(target) for target in self._playback_targets
        )
        if self._onlyonce or self._cd2_restart or recover_uploads:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

        if recover_uploads:
            # MP V2 skips get_service() for disabled plugins, so recovery runs here.
            self._scheduler.add_job(
                self.check_playback, "interval", id="recover_uploads",
                seconds=self._playback_interval, max_instances=1, coalesce=True,
            )

        if self._onlyonce:
            logger.info("CloudDrive2助手定时任务，立即运行一次")
            self._scheduler.add_job(
                self.__run_check,
                "date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="CloudDrive2助手定时任务",
            )
            self._onlyonce = False
            self.__update_config()

        if self._cd2_restart:
            logger.info("CloudDrive2重启任务，立即运行一次")
            self._scheduler.add_job(
                self.__run_restart,
                "date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="CloudDrive2重启任务",
            )
            self._cd2_restart = False
            self.__update_config()

        if self._scheduler and self._scheduler.get_jobs():
            self._scheduler.start()

    def __create_clients(self) -> None:
        """按一行一个配置建立 CloudDrive2 客户端，隔离单条坏配置。"""
        for line_number, line in enumerate((self._cd2_confs or "").splitlines(), 1):
            if not line.strip():
                continue
            parts = line.split("#", 3)
            if len(parts) not in (3, 4) or not all(part.strip() for part in parts[:3]):
                logger.error("CloudDrive2 第 %s 行配置格式错误，请检查实例名、地址与认证信息", line_number)
                continue

            name, url = parts[:2]
            username = parts[2] if len(parts) == 4 else None
            try:
                if username is None:
                    name, url = name.strip(), url.strip()
                    client = CloudDriveConnection(url, api_token=parts[2])
                else:
                    client = CloudDriveConnection(url, username, parts[3])
            except Exception:
                logger.error("CloudDrive2 第 %s 行连接配置无效，请检查 gRPC 地址和认证信息", line_number)
                continue

            old_client = self._clients.get(name)
            if old_client:
                old_client.close()
                self._playback_targets = {
                    key: target for key, target in self._playback_targets.items() if target[0] != name
                }
            self._clients[name] = client
            self._cd2_url[name] = url
            identity = username if username is not None else f"token\n{name}"
            target_id = hashlib.sha256(f"{url.rstrip('/')}\n{identity}".encode()).hexdigest()
            self._playback_targets[target_id] = (
                name, CloudUploadAPI(client, proto, lambda: self._playback_stopping))

    def __sync_old_config(self) -> None:
        """把历史单实例配置转换为当前多实例配置。"""
        if self._cd2_confs:
            return
        old_config = self.get_config() or {}
        if not all(old_config.get(key) for key in ("cd2_url", "cd2_username", "cd2_password")):
            return
        self._cd2_confs = (
            f"默认配置1#{old_config['cd2_url']}#"
            f"{old_config['cd2_username']}#{old_config['cd2_password']}"
        )
        self.__update_config()

    def __update_config(self) -> None:
        """持久化会被一次性任务消费的配置状态。"""
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

    async def check(self) -> None:
        await asyncio.to_thread(self.__check_health)

    def __check_health(self):
        if self._playback_stopping or not self._health_lock.acquire(blocking=False):
            return
        try:
            blacklist = {item.strip() for item in self._black_dir.split(',') if item.strip()}
            for name, client in tuple(self._clients.items()):
                username = client._username
                identity = username if username is not None else f"token\n{name}"
                instance = hashlib.sha256((self._cd2_url[name] + '\n' + identity).encode()).hexdigest()
                for scope, complete, issues in inspect_health(client, proto, blacklist, lambda: self._playback_stopping):
                    if self._playback_stopping:
                        return
                    key = instance + ':' + scope
                    previous = self._health_alerts.get(key, {})
                    # Query failure replaces only its own alert; it cannot clear task failures.
                    current = issues if complete else {**previous, **issues}
                    logger.info('%s：%s 检测%s，异常 %s 项', name, scope.split(':')[0],
                                '完成' if complete else '未能确认', len(issues))
                    if not self._notify:
                        continue
                    changed = [value for item, value in current.items() if previous.get(item) != value]
                    recovered = [item for item in previous if item not in current] if complete else []
                    if changed or recovered:
                        text = name + '｜' + ('上传检测' if scope == 'uploads' else '网盘/服务检测')
                        if changed:
                            text += '\n' + '\n\n'.join(changed[:20])
                            if len(changed) > 20:
                                text += f'\n另有 {len(changed) - 20} 项异常，请在 CloudDrive2 查看。'
                        if recovered:
                            text += f'\n{len(recovered)} 项此前异常已不再出现（上传任务可能已恢复、完成或移除）。'
                        if len(text) > 3000:
                            text = text[:2900] + f'\n本次新增或变化异常共 {len(changed)} 项，完整内容请在 CloudDrive2 查看。'
                        try:
                            self.__send_notify(text)
                        except Exception as error:
                            logger.warning('CloudDrive2 通知提交失败（%s），下次重试', type(error).__name__)
                            continue
                    self._health_alerts[key] = current
                    self.save_data('health_alerts', self._health_alerts)
        finally:
            self._health_lock.release()

    @staticmethod
    def __field(value: Any, name: str, default: Any = None) -> Any:
        """读取 CloudDrive2 protobuf 或字典响应中的字段。"""
        if isinstance(value, dict):
            return value.get(name, default)
        return getattr(value, name, default)

    @staticmethod
    def __action_payload(event: Event | None) -> Optional[SimpleNamespace]:
        """读取 MoviePilot V2 的字典事件，只取插件动作需要的字段。"""
        data = getattr(event, "event_data", None)
        if not isinstance(data, dict) or not isinstance(data.get("action"), str):
            return None
        return SimpleNamespace(action=data["action"], arg_str=data.get("arg_str"),
                               channel=data.get("channel"), user=data.get("user"))

    @eventmanager.register(EventType.PluginAction)
    async def restart_cd2(self, event: Event | None = None) -> None:
        """重启指定或全部 CloudDrive2 实例。"""
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
                await asyncio.to_thread(client.rpc, "RestartService")
            except Exception as error:
                logger.error("%s CloudDrive2重启失败：%s", name, error)
                if payload is not None:
                    self.post_message(
                        channel=payload.channel,
                        title=f"{name} CloudDrive2重启失败！",
                        userid=payload.user,
                        text=str(error),
                    )
                continue
            logger.info("%s CloudDrive2重启成功", name)
            if payload is not None:
                self.post_message(
                    channel=payload.channel,
                    title=f"{name} CloudDrive2重启成功！",
                    userid=payload.user,
                )

        if payload is not None and args and not found:
            self.post_message(
                channel=payload.channel,
                title=f"未找到 {args} 配置！",
                userid=payload.user,
            )

    @eventmanager.register(EventType.PluginAction)
    async def add_offline_files(self, event: Event | None = None) -> None:
        """执行 CloudDrive2 离线下载命令。"""
        payload = self.__action_payload(event)
        if event is None or payload is None or payload.action != "cloud_download":
            return
        raw_args = getattr(payload, "arg_str", "")
        if not raw_args:
            logger.error("缺少参数：%s", raw_args)
            return

        args = str(raw_args).replace(" ", "\n")
        cloud_path = self._cloud_path.strip()
        first_line = args.split("\n", 1)[0]
        if first_line.startswith("/"):
            cloud_path = first_line
            args = args[len(first_line):].lstrip("\n")
        if not cloud_path:
            logger.error("请先设置云盘路径")
            if payload.user:
                self.post_message(channel=payload.channel, title="请先设置云盘路径！", userid=payload.user)
            return

        client = next((item for item in self._clients.values() if item), None)
        if not client:
            logger.error("CloudDrive2助手没有可用连接")
            if payload.user:
                self.post_message(channel=payload.channel, title="CloudDrive2连接失败！", userid=payload.user)
            return

        logger.info("获取到离线云盘路径：%s", cloud_path)
        logger.info("开始离线下载：%s", args)
        try:
            result = await asyncio.to_thread(
                client.rpc, "AddOfflineFiles",
                proto.AddOfflineFileRequest(urls=args, toFolder=cloud_path),
            )
        except Exception as error:
            logger.error("离线下载失败：%s", error)
            if payload.user:
                self.post_message(
                    channel=payload.channel,
                    title="离线下载失败！",
                    userid=payload.user,
                    text=f"错误信息：{error}",
                )
            return

        if self.__field(result, "success", False):
            logger.info("离线下载成功")
            if payload.user:
                self.post_message(channel=payload.channel, title=f"{cloud_path} 离线下载成功！", userid=payload.user)
            return

        error_message = self.__field(result, "errorMessage")
        logger.error("离线下载失败：%s", error_message)
        if payload.user:
            self.post_message(
                channel=payload.channel,
                title="离线下载失败！",
                userid=payload.user,
                text=f"错误信息：{error_message}",
            )

    @eventmanager.register(EventType.PluginAction)
    async def cd2_info(self, event: Event | None = None) -> None:
        """通过插件动作消息返回 CloudDrive2 运行状态。"""
        payload = self.__action_payload(event)
        if event is None or payload is None or payload.action != "cd2_info":
            return
        args = str(getattr(payload, "arg_str", "") or "").strip()
        found = False
        for name, client in tuple(self._clients.items()):
            if args and args.casefold() != str(name).casefold():
                continue
            found = True
            try:
                info = await self.__get_cd2_info(event=event, client=client)
                self._info[name] = info
            except Exception as error:
                logger.error("获取 %s CloudDrive2 信息失败：%s", name, error)

        if args and not found:
            self.post_message(channel=payload.channel, title=f"未找到 {args} 配置！", userid=payload.user)

    @staticmethod
    def __message_to_dict(value: Any) -> dict[str, Any]:
        """把 protobuf、字典或文本响应归一为可计算字典。"""
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

    async def __get_cloud_space(self, client: CloudDriveConnection) -> Optional[str]:
        """异步读取各云盘空间，且将目录枚举放进线程边界。"""
        blacklist = {item.strip() for item in self._black_dir.split(",") if item.strip()}
        space_info_text = "\n"
        for path in await asyncio.to_thread(client.listdir):
            if not path or path in blacklist:
                continue
            try:
                result = await asyncio.to_thread(
                    client.rpc, "GetSpaceInfo", proto.FileRequest(path=f"/{path}"),
                )
                values = self.__message_to_dict(result)
                total = self.__convert_bytes(self.__number(values.get("totalSpace")))
                used = self.__convert_bytes(self.__number(values.get("usedSpace")))
                space_info_text += f"{path}：{used}/{total}\n"
            except Exception as error:
                logger.error("获取云盘 %s 空间信息失败：%s", path, error)
        return space_info_text

    async def __get_cd2_info(
        self,
        event: Event | None = None,
        client: CloudDriveConnection | None = None,
    ) -> dict[str, Any]:
        """读取 CloudDrive2 系统状态并保持旧接口字段名。"""
        if not client:
            return {}
        system_info = self.__message_to_dict(
            await asyncio.to_thread(client.rpc, "GetRunningInfo")
        )
        task_count = self.__message_to_dict(
            await asyncio.to_thread(client.rpc, "GetAllTasksCount")
        )
        download_files = self.__message_to_dict(
            await asyncio.to_thread(client.rpc, "GetDownloadFileList")
        )
        upload_files = self.__message_to_dict(
            await asyncio.to_thread(
                client.rpc, "GetUploadFileList",
                proto.GetUploadFileListRequest(itemsPerPage=1, pageNumber=0),
            )
        )
        cloud_space = await self.__get_cloud_space(client)

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
        logger.info("获取CloudDrive2系统信息：\n%s", info)

        if event is not None:
            payload = self.__action_payload(event)
            if payload is not None:
                self.post_message(
                    channel=payload.channel,
                    title="CloudDrive2系统信息",
                    userid=payload.user,
                    text=(
                        f"CPU占用：{info['cpuUsage']}\n"
                        f"内存占用：{info['memUsageKB']}\n"
                        f"运行时间：{info['uptime']}\n"
                        f"打开文件数量：{info['fhTableCount']}\n"
                        f"目录缓存数量：{info['dirCacheCount']}\n"
                        f"临时文件数量：{info['tempFileCount']}\n"
                        f"上传任务数量：{info['upload_count']}\n"
                        f"下载任务数量：{info['download_count']}\n"
                        f"下载速度：{info['download_speed']}\n"
                        f"上传速度：{info['upload_speed']}\n"
                        f"存储空间：{info['cloud_space']}\n"
                    ),
                )
        return info

    async def homepage(self, name: str | None = None) -> CloudDriveInfo:
        """返回指定 CloudDrive2 实例的 HomePage 状态。"""
        for cd2_name, client in self._clients.items():
            if name and str(cd2_name) != name:
                continue
            info = await self.__get_cd2_info(client=client)
            self._info[cd2_name] = info
            return CloudDriveInfo.model_validate(info)
        return CloudDriveInfo()

    async def __refresh_info(self) -> None:
        """刷新详情页和仪表板共用的 CloudDrive2 状态快照。"""
        for name, client in tuple(self._clients.items()):
            try:
                self._info[name] = await self.__get_cd2_info(
                    client=client,
                )
            except Exception as error:
                logger.error("获取 %s CloudDrive2 信息失败：%s", name, error)

    def __refresh_info_sync(self) -> None:
        """从宿主同步页面 ABI 运行异步状态采集。"""
        try:
            asyncio.run(self.__refresh_info())
        except Exception as error:
            logger.error("刷新 CloudDrive2 页面状态失败：%s", error)

    @staticmethod
    def __convert_bytes(size_in_bytes: float) -> str:
        """把字节数转换为适合状态页展示的单位。"""
        units = ("B", "KB", "MB", "GB", "TB", "PB")
        unit_index = 0
        while size_in_bytes >= 1024 and unit_index < len(units) - 1:
            size_in_bytes /= 1024
            unit_index += 1
        return f"{size_in_bytes:.2f} {units[unit_index]}"

    @staticmethod
    def convert_seconds(seconds: float) -> str:
        """把运行秒数转换为中文时长。"""
        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        parts = []
        if days > 0:
            parts.append(f"{int(days)}天")
        if hours > 0:
            parts.append(f"{int(hours)}小时")
        if minutes > 0:
            parts.append(f"{int(minutes)}分钟")
        if seconds > 0 or not parts:
            parts.append(f"{seconds:.0f}秒")
        return "".join(parts)

    def __send_notify(self, message: str) -> None:
        """发送检测通知，并将无效消息类型回退到手动处理。"""
        try:
            message_type = NotificationType[str(self._msgtype)] if self._msgtype else NotificationType.Manual
        except KeyError:
            message_type = NotificationType.Manual
        self.post_message(title="CloudDrive2助手通知", mtype=message_type, text=message)

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册 CloudDrive2 重启、状态和离线下载命令。"""
        return [
            {
                "cmd": "/cd2_restart",
                "event": EventType.PluginAction,
                "desc": "CloudDrive2重启",
                "category": "",
                "data": {"action": "cd2_restart"},
            },
            {
                "cmd": "/cd2_info",
                "event": EventType.PluginAction,
                "desc": "CloudDrive2系统信息",
                "category": "",
                "data": {"action": "cd2_info"},
            },
            {
                "cmd": "/cd",
                "event": EventType.PluginAction,
                "desc": "云下载",
                "category": "",
                "data": {"action": "cloud_download"},
            },
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """注册由宿主 API Key 鉴权的 HomePage 状态接口。"""
        return [
            {
                "path": "/homepage",
                "endpoint": self.homepage,
                "methods": ["GET"],
                "auth": "apikey",
                "response_model": CloudDriveInfo,
                "summary": "HomePage",
                "description": "HomePage自定义api",
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """把周期检测注册到宿主调度器。"""
        services = []
        if self._enabled and self._clients and self._upload_guard and (
            self._playback_pause or self._upload_guard.state
        ):
            services.append({
                "id": "Cd2AssistantPlayback", "name": "CloudDrive2外网播放暂停上传",
                "trigger": "interval", "func": self.check_playback,
                "kwargs": {"seconds": self._playback_interval, "max_instances": 1, "coalesce": True},
            })
        if not self._enabled or not self._cron or not self._clients:
            return services
        try:
            trigger = CronTrigger.from_crontab(self._cron, timezone=settings.TZ)
        except Exception as error:
            logger.error("定时任务配置错误：%s", error)
            return services
        return services + [
            {
                "id": "Cd2Assistant",
                "name": "CloudDrive2助手检测服务",
                "trigger": trigger,
                "func": self.check,
                "kwargs": {"max_instances": 1, "coalesce": True},
            }
        ]

    @staticmethod
    def __bounded_int(value, default, minimum, maximum):
        try:
            return max(minimum, min(maximum, int(value)))
        except (TypeError, ValueError):
            return default

    def check_playback(self) -> None:
        """同步调度任务；锁避免 Webhook、轮询和插件关闭互相交错。"""
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
                    logger.warning("获取媒体服务器失败，保持 CloudDrive2 上传状态")
            if self._playback_stopping:
                return
            self._upload_guard.tick(playing, self._playback_targets,
                                    self._playback_resume_delay, enabled=enabled)
            if not self._enabled and self._scheduler:
                recovery = self._scheduler.get_job("recover_uploads")
                if recovery and not any(self._upload_guard.state.get(target) for target in self._playback_targets):
                    recovery.remove()
        except Exception as error:
            logger.warning("外网播放检查失败（%s），下次重试", type(error).__name__)
        finally:
            self._playback_lock.release()

    @eventmanager.register(EventType.WebhookMessage)
    async def playback_webhook(self, event: Event | None = None) -> None:
        """Webhook 仅加快查询，不直接把单个停止事件视为所有人停止播放。"""
        if not event or not self._enabled or not self._playback_pause:
            return
        event_name = str(self.__field(getattr(event, "event_data", None), "event", "") or "").lower()
        if event_name in {"playback.start", "playback.stop", "playback.pause", "playback.unpause",
                          "playbackstart", "playbackstop", "playbackpause", "playbackunpause",
                          "media.play", "media.stop", "media.pause", "media.resume"}:
            await asyncio.to_thread(self.check_playback)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单及默认配置。"""
        message_options = [
            {"title": item.value, "value": item.name}
            for item in NotificationType
        ]
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            self.__field_control("VSwitch", "enabled", "启用插件", 3),
                            self.__field_control("VSwitch", "notify", "开启通知", 3),
                            self.__field_control("VSwitch", "cd2_restart", "cd2重启一次", 3),
                            self.__field_control("VSwitch", "onlyonce", "立即运行一次", 3),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__field_control("VSwitch", "playback_pause", "外网播放时暂停上传（忽略内网）", 6),
                            self.__field_control("VTextField", "playback_interval", "播放检查间隔（秒，5～300）", 3),
                            self.__field_control("VTextField", "playback_resume_delay", "无人外网播放后恢复延迟（秒）", 3),
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {"type": "info", "variant": "tonal", "text":
                            "读取 MP 已配置的 Emby/Jellyfin/Plex。内网播放不暂停上传；所有外网视频停止或暂停后延迟恢复。"
                            "只恢复本功能暂停的任务。反向代理必须让媒体服务器识别真实客户端 IP。关闭本功能后会在下一次检查恢复其暂停的任务。"},
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
                                            "label": "CloudDrive2 连接配置",
                                            "rows": 2,
                                            "placeholder": "家里#http://127.0.0.1:19798#API令牌",
                                            "hint": "每行一台：名称#地址#API令牌。旧的名称#地址#用户名#密码格式也可用。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__field_control("VCronField", "cron", "检测周期", 4, "5位cron表达式"),
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
                                            "label": "消息类型",
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
                            self.__field_control("VTextField", "black_dir", "cd2黑名单目录", 4, "cd2上添加的本地目录(多个目录用英文逗号分隔)"),
                            self.__field_control("VTextField", "cloud_path", "云下载路径", 4),
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
                                            "text": "检查全部上传任务：Error 为可重试错误，FatalError 为上传失败。暂停、排队、取消不报失败；相同异常只通知一次，恢复后通知。旧版关键词不再限制错误检测。",
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
                                            "text": "检测服务登录与就绪状态、网盘锁定及强制刷新目录。空目录属于正常；超时、权限不足等单独报告，不推断 Cookie 过期。通知使用 MP 已配置渠道（如 Telegram），请在 MP 通知设置中开启对应消息类型。",
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
                                            {"component": "span", "text": "HomePage配置教程请参考："},
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
            "keyword": "",
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
        """构造配置表单中的单字段列。"""
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
        """构造插件详情页和仪表板共用的状态卡片。"""
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
        """渲染单个 CloudDrive2 实例及其状态字段。"""
        values = [
            ("CPU占用", info.get("cpuUsage")),
            ("内存占用", info.get("memUsageKB")),
            ("运行时间", info.get("uptime")),
            ("打开文件数", info.get("fhTableCount")),
            ("缓存目录数", info.get("dirCacheCount")),
            ("临时文件数", info.get("tempFileCount")),
            ("下载任务数", info.get("download_count", 0)),
            ("上传任务数", info.get("upload_count", 0)),
            ("下载速率", info.get("download_speed", "0KB/s")),
            ("上传速率", info.get("upload_speed", "0KB/s")),
            ("存储空间", info.get("cloud_space")),
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

    def get_page(self) -> Optional[List[dict]]:
        # Satisfy the host ABC while its placeholder detection keeps has_page false.
        pass

    def get_dashboard(self, key: str | None = None, **kwargs) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], List[dict]]]:
        """刷新状态后返回仪表板布局。"""
        if not self._clients:
            return {
                "cols": 12,
                "md": 12,
            }, {"refresh": 10, "border": False}, [{"component": "div", "text": "无法连接CloudDrive2"}]
        self.__refresh_info_sync()
        elements = [self.__render_instance(name, self._info.get(name, {})) for name in self._clients]
        return {"cols": 12, "md": 12}, {"refresh": 10, "border": False}, elements

    def stop_service(self) -> None:
        """停止一次性调度器并释放 CloudDrive2 连接。"""
        self._playback_stopping = True
        # The worker uses only synchronous SDK/RPC calls, so joining it does not
        # depend on the host event loop. Keep ownership persisted across restarts.
        with self._health_lock, self._playback_lock:
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
                logger.error("退出插件失败：%s", error)
            else:
                self._scheduler = None

        for name, client in tuple(self._clients.items()):
            try:
                client.close()
            except Exception as error:
                logger.debug("关闭 CloudDrive2 连接失败：%s", error)
                continue
            self._clients.pop(name, None)
            self._cd2_url.pop(name, None)

    def __run_check(self) -> None:
        """为同步 APScheduler 一次性任务桥接异步检测。"""
        try:
            asyncio.run(self.check())
        except Exception as error:
            logger.error("CloudDrive2助手一次性检测失败：%s", error)

    def __run_restart(self) -> None:
        """为同步 APScheduler 一次性任务桥接异步重启。"""
        try:
            asyncio.run(self.restart_cd2())
        except Exception as error:
            logger.error("CloudDrive2助手一次性重启失败：%s", error)
