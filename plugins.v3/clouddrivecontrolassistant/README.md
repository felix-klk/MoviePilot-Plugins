# CloudDrive控制助手

CloudDrive控制助手监控上传故障和网盘健康，通过 MoviePilot 已配置的渠道发送通知。启用外网播放保护后，可暂停 CloudDrive2 的上传和文件夹复制/移动，或给 CloudDrive2 设置上传限速。

## 安装与发行状态

在 MoviePilot 第三方插件仓库添加 [felix-klk/MoviePilot-Plugins](https://github.com/felix-klk/MoviePilot-Plugins)，在商城搜索 **CloudDrive控制助手**，作者 `felix-klk`。插件 ID 为 `CloudDriveControlAssistant`，认证等级为 2。

| MoviePilot | 独立发行版 | GitHub 安装包 |
| --- | --- | --- |
| V2，要求 2.10.0 至 3.0.0（不含） | 2.0.0 | [CloudDriveControlAssistant_v2.0.0](https://github.com/felix-klk/MoviePilot-Plugins/releases/tag/CloudDriveControlAssistant_v2.0.0) |
| V3，要求 3.0.0 及以上 | 3.0.0 | [CloudDriveControlAssistant_v3.0.0](https://github.com/felix-klk/MoviePilot-Plugins/releases/tag/CloudDriveControlAssistant_v3.0.0) |

从 `Cd2Assistant` 切换时，先在旧插件关闭外网播放保护，等待它恢复所接管的任务与限速，再停用旧插件。新插件单独保存设置和任务状态，首次安装需填写地址和 API 令牌。

## CloudDrive2 连接与 API 令牌

在 CloudDrive2 的「API 令牌」页面创建令牌。回到插件设置，启用插件，在「CloudDrive2 连接」填写服务器地址和 API 令牌。地址须能从 MoviePilot 所在机器或容器访问。gRPC 默认端口为 `19798`，支持用 HTTPS 连接提供 gRPC 的 TLS 入口。

首页编辑首个连接。已保存的认证信息留空沿用，其他已保存的连接继续运行。已有账号密码配置可继续使用；如果要切换首个连接到 API 令牌，直接填写新令牌。轮换 API 令牌时保留原实例名和地址，可接续已有接管记录。

| 用途 | CloudDrive2 令牌权限 |
| --- | --- |
| 上传及网盘健康检查 | 列目录 `allow_list`、获取云存储信息 `allow_get_cloud_apis`、查看传输任务 `allow_get_transfer_tasks` |
| 播放时暂停与恢复 | 在上项基础上开启修改传输任务 `allow_modify_transfer_tasks` |
| 播放时限制上传速度 | 再开启获取系统设置 `allow_get_system_settings`、修改系统设置 `allow_modify_system_settings` |
| 查看仪表板或 HomePage | 按需开启运行信息 `allow_view_runtime_info`、空间信息 `allow_get_space_info` |
| 离线下载或重启 CloudDrive2 | 分别开启 `allow_add_offline_download` 或 `allow_service_control` |

监控所有网盘时，将令牌根目录设为 `/`。令牌到期或被撤销后，在设置页替换。权限和接口以 [CloudDrive2 官方 API 指南](https://www.clouddrive2.com/api/CloudDrive2_gRPC_API_Guide.html) 为准。

## 外网播放保护

在「外网播放保护」卡片打开开关。插件复用 MoviePilot 已配置的 Emby、Jellyfin、Plex，默认每 10 秒检查一次。已有的媒体服务器 Webhook 可以提前触发检查。全部外网视频停止或暂停后，默认延迟 45 秒再恢复。

- **暂停上传与复制**覆盖 CloudDrive2 上传和文件夹复制/移动队列，恢复范围限于插件接管的任务。外网播放期间新进入队列的任务会在下一轮检查处理。网盘服务端秒传和未进入这些队列的递归操作受 CloudDrive2 API 限制。
- **限制上传速度**使用 CloudDrive2 的「上传上限（KB/s）」，初次切换默认填入 200。插件保留已有的更低限速，播放结束后恢复自己调整过的设置。设置限速失败会回退到暂停两类队列。
- 内网播放默认豁免。经代理或 VPN 访问时，外网与内网按媒体服务器提供的会话地址区分。仅当代理来源专用于外网时，才在「高级设置 → 播放来源例外」中添加该地址或 CIDR。遇到查询失败或地址不明，且无法确认是否有外网播放时，插件保留当前保护状态。
- 修改连接地址、从密码切换到令牌或卸载前，先保持插件启用，关闭「外网播放保护」并保存，等接管的任务和限速恢复。直接卸载后需要在 CloudDrive2 检查并手动恢复相应任务或设置。

## 健康检查和通知

默认每 10 分钟检查一次，也可在「高级设置」勾选「立即检查一次」后保存。开启「异常通知」，在 MoviePilot 的 Telegram 等通知渠道中允许所选消息类型（默认「手动处理」）。

插件遍历上传队列分页，根据 CloudDrive2 的 `Error` 和 `FatalError` 区分可能重试的异常和上传失败，并检查服务登录和就绪状态、网盘锁定状态、目录能否强制刷新。目录探测只读，可读取的空目录视为正常。通知对持续的相同异常去重，成功查询后才确认异常消失。检测结果反映检查时能查到的状态。

聊天命令为 `/cdcontrol_info`（状态）、`/cdcontrol_restart`（重启）和 `/cdcontrol_download`（离线下载）。HomePage API 使用 `CloudDriveControlAssistant` 插件命名空间。

## 验证

实机验证时保留一个手动暂停的任务，再启动一个上传和一个文件夹复制或移动任务。用内网播放确认任务继续运行，再用手机蜂窝网络播放，确认选择「暂停上传与复制」时两类队列暂停、停止播放并超过恢复延迟后自动恢复，手动暂停的任务继续保持暂停。测试限速模式时比对播放前、播放中、结束后的 CloudDrive2 上限。另可在开启通知后使用「立即检查一次」，核对 MoviePilot 通知日志和所选渠道的投递结果。

基于 thsrite 的 CloudDrive2助手，播放检测参考 [Shurelol / jxxghp 的 speedlimiter](https://github.com/jxxghp/MoviePilot-Plugins/tree/main/plugins.v2/speedlimiter)。项目采用 [GPLv3](LICENSE)。
