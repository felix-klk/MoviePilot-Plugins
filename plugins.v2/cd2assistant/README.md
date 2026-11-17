# CloudDrive2助手 NEO(自用版) · MoviePilot V2

适用于 MoviePilot 2.10.0 及以上的 V2 版本。在插件市场添加 `https://github.com/felix-klk/MoviePilot-Plugins`，搜索 **CloudDrive2助手 NEO(自用版)** 并安装 **2.1.1**。已经安装 `Cd2Assistant` 时可选择本仓库进行更新，插件 ID 相同，可沿用本机已有配置。手动选历史版本时，V2 选择 `Cd2Assistant_v2.1.1`，V3 选择对应的 3.x 版本。

1. 打开插件设置，启用插件并添加 CloudDrive2 连接。每行使用 `名称#地址#API令牌`，权限见下文。
2. 按需开启「外网播放时暂停上传（忽略内网）」。默认每 10 秒检查 MP 已配置的媒体服务器，所有外网播放停止或暂停后等待 45 秒，仅恢复由插件暂停的任务。
3. 开启「开启通知」，在 MP 的 Telegram 等渠道中允许所选消息类型（默认「手动处理」）。勾选「立即运行一次」并保存可触发检测。周期检测默认每 10 分钟运行。

插件使用 CloudDrive2 gRPC 分页检查上传错误，并检查服务是否就绪、网盘是否锁定及目录能否读取。空目录正常，持续相同的故障只通知一次。目录探测只读，定时检测只报告检查时仍可查到的异常。

## API 令牌

在 CloudDrive2 网页进入「API 令牌 → 创建令牌」，指定有效期和需要的权限。在插件设置填 `名称#地址#API令牌`，例如 `家里#http://127.0.0.1:19798#你的令牌`。每行一台，地址须从 MoviePilot 所在机器或容器可达。CloudDrive2 的默认 gRPC 端口是 19798；若通过支持 gRPC 的 TLS 入口访问，填 `https://你的域名` 或带端口的 HTTPS 地址。

- **监控**：列目录（`allow_list`）、获取云存储信息（`allow_get_cloud_apis`）、查看传输任务（`allow_get_transfer_tasks`）。检查整台服务器时，将令牌的根目录设为 `/`。
- **外网播放暂停/恢复**：再启用修改传输任务（`allow_modify_transfer_tasks`）。
- **可选功能**：仪表板或 HomePage 再启用运行信息和空间信息（`allow_view_runtime_info`、`allow_get_space_info`）。离线下载和重启分别需要 `allow_add_offline_download` 和 `allow_service_control`。

令牌直接用于 Bearer 认证。到期或被撤销后，创建新令牌替换原值即可。更换令牌时保留原实例名和 gRPC 地址，以便接续已暂停的任务。旧的 `名称#地址#用户名#密码` 格式仍可用。从旧格式切换前，若插件暂停了上传，请先关闭「外网播放时暂停上传」，保持插件启用，确认任务恢复后再换令牌。

[CloudDrive2 官方 API 开发者指南](https://www.clouddrive2.com/api/CloudDrive2_gRPC_API_Guide.html)
