# CloudDrive2助手：MoviePilot V3 外网播放暂停上传补丁

基于用户提供的 Cd2Assistant 3.0.1 修改，补丁版本 3.1.2。只修改 V3 的 CloudDrive2助手；CloudDriveDisk 无需替换。

3.1.0 使用插件私有 protobuf 描述符池，修复 `duplicate symbol clouddrive.ProxyType` 加载冲突，并保持 Windows 兼容。插件恢复原来的站点认证等级 2，MoviePilot 会按此等级显示插件。

## 行为

- 开启“启用插件”和“外网播放时暂停上传（忽略内网）”。此新功能默认关闭。
- 读取 MoviePilot 已配置的 Emby、Jellyfin、Plex，无需重复填写媒体服务器密钥。
- 默认每 10 秒查询播放状态；已有媒体服务器 Webhook 接入 MP 时，可提前触发查询。无需 Webhook 也能运行。
- 任意服务器有外网视频正在播放或 Plex 视频缓冲时，暂停配置的所有 CloudDrive2 实例中正在处理、排队、上传或可重试的上传任务。
- 内网 IPv4、私有/链路本地 IPv6、回环地址及 CGNAT 地址忽略；公网 IPv6 正常识别。Plex 明确标记为本地的客户端也忽略。
- 所有外网视频停止或暂停后，默认等待 45 秒再恢复；恢复时间还受轮询周期与请求耗时影响。
- 音频播放不触发。查询失败、地址缺失或无法解析、没有媒体服务器、存在不支持的服务器类型时，不能据此认定“无人播放”。已有外网播放仍可触发暂停。
- 播放期间新加入的上传任务会在下一次检查中暂停，因此不是零延迟的全局上传禁令。
- 暂停前记录任务 key、目标路径、大小；恢复时只处理本功能接管的相同任务。原先手动暂停的任务不会被批量恢复。
- MP 重启保留接管记录，重新检查播放状态后恢复管理。API 请求超时或恢复失败保留记录并重试。CloudDrive2 令牌失效时重新登录一次。

## 从插件商城安装

1. 在 MoviePilot V3 的第三方插件仓库中添加：

   `https://github.com/felix-klk/MoviePilot-Plugins`

2. 刷新插件商城，搜索 **CloudDrive2助手**，选择来自本仓库的 **3.1.2** 版本安装或更新。
3. 打开配置，填写 `名称#地址#API令牌`（权限见下文），开启“启用插件”和“外网播放时暂停上传（忽略内网）”。
4. 默认每 10 秒检查，所有外网播放停止或暂停后等待 45 秒恢复。

本插件与原 CloudDrive2助手使用相同插件 ID，以保留配置。若同时添加其他提供同名插件的仓库，请确认安装或更新来源为本仓库。从其他源更新可能覆盖此功能。

## 实机验证

1. 在 CD2 中准备一个正常上传任务，另准备一个手动暂停任务。
2. 用内网客户端播放：正常上传应继续。
3. 用手机蜂窝网络播放视频：下一次检查后正常上传应暂停；日志出现“外网播放，暂停上传任务”。
4. 两个外网设备同时播放，停止其中一个：上传应继续暂停。
5. 全部外网设备停止或暂停：约 45～55 秒后恢复本功能暂停的任务；原手动暂停任务仍暂停。
6. 外网播放期间添加新上传任务：下一轮检查暂停。
7. 暂停期间重启 MP，再全部停止播放：应在确认无人播放及延迟结束后恢复。

## 关闭与回退

先保持“启用插件”和原 CD2 配置，单独关闭“外网播放时暂停上传”并保存，等待下一次检查恢复接管任务，确认完成后再停用、卸载或还原备份。直接卸载插件不会执行恢复；必要时在 CD2 中手动恢复相应任务。

请在恢复完毕后再删除/修改 CD2 地址或用户名；接管记录按地址及用户名隔离，改变连接身份后不会误操作新实例。

## 注意事项与验证范围

- 本补丁实现的是暂停/恢复上传，不提供指定 KB/s 的带宽限速，不控制下载器。
- 外网判断依赖媒体服务器记录的客户端真实 IP。经过反向代理但服务器只看到代理内网 IP 时，会按内网处理；应先确保服务器能识别真实客户端地址。
- 已经被插件暂停的任务，如果你再手动点一次暂停，CD2 API 不提供暂停来源，插件无法识别这次意图。需要手动接管时，先关闭联动并等它释放任务，再手动暂停。
- 上传任务的暂停生效速度、客户端播放上报是否及时、媒体服务器残留会话，以及 CloudDriveDisk 远程上传模式在长时间暂停后的表现，仍需在实际环境验证。
- 本地验证为核心逻辑测试及使用宿主替身的 V3 插件契约测试，未连接真实 MoviePilot/CloudDrive2。测试不等同于完整实机联调。

## 来源

- 基础插件：用户提供的 `MoviePilot-Plugins-Cd2Assistant_v3.0.1.zip`，原作者 thsrite，保留 GPLv3 LICENSE。
- 播放检测设计参考：Shurelol / jxxghp 的 [speedlimiter](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/plugins.v2/speedlimiter/__init__.py)。本实现按 V3 SDK 重写，保留内网豁免，不修改下载器限速。
- V3 服务发现：[app.sdk.services](https://github.com/jxxghp/MoviePilot/blob/v3/app/sdk/services.py)。
- CloudDrive2：[官方 API](https://www.clouddrive2.com/api/CloudDrive2_gRPC_API_Guide.html)，使用 GetUploadFileList、PauseUploadFiles、ResumeUploadFiles。

运行测试：`python -m unittest discover -s tests -p 'test_cd2assistant*.py' -v`。


## 3.1.0 健康检测与通知

点击插件使用 MoviePilot 原生设置表单，不再进入无数据的详情页。原仪表板与 HomePage API 保留。

开启插件，填写连接配置，设置检测周期（默认每 10 分钟），开启通知并选择消息类型。在 MoviePilot 的通知设置中启用 Telegram 等渠道，并允许该消息类型。插件沿用 MP 通知路由，不需要重复填写 Bot Token。勾选“立即运行一次”并保存可检查当前状态。

上传检测遍历全部分页，按官方 statusEnum 判断 Error（可重试错误）与 FatalError（失败）；暂停、取消、排队均不视为失败。旧关键词不再作为失败通知的前提。相同异常去重并持久化，任务恢复、完成或移除后报告异常已不再出现；检查失败不判为恢复。通知提交到 MP 后的实际投递由 MP 管理。

网盘检查包含 GetSystemInfo 登录/就绪/错误状态、GetAllCloudApis 锁定状态及 GetSubFiles 强制刷新目录。空目录正常；超时、权限不足、资源限制独立报告，不猜测 Cookie 过期。目录读取成功仅证明当次目录访问正常，不代表已测试上传或下载。使用定时只读探测，不创建测试文件；短于检测周期且已消失的错误可能无法捕获。

接口字段已与官方当前 https://www.clouddrive2.com/api/clouddrive.proto 对照。继续采用独立 protobuf 描述符池，保留 Windows 兼容、等级 2 和外网播放暂停上传。


3.1.1 修复 MP V3 抽象方法 get_page 缺失导致无法加载的问题；保留占位实现以使用原生设置入口。显示名称为 CloudDrive2助手 NEO(自用版)，插件 ID 仍为 Cd2Assistant。

## API 令牌

在 CloudDrive2 网页进入「API 令牌 → 创建令牌」，指定有效期和需要的权限。在插件设置填 `名称#地址#API令牌`，例如 `家里#http://127.0.0.1:19798#你的令牌`。每行一台，地址须从 MoviePilot 所在机器或容器可达。CloudDrive2 的默认 gRPC 端口是 19798；若通过支持 gRPC 的 TLS 入口访问，填 `https://你的域名` 或带端口的 HTTPS 地址。

- **监控**：列目录（`allow_list`）、获取云存储信息（`allow_get_cloud_apis`）、查看传输任务（`allow_get_transfer_tasks`）。检查整台服务器时，将令牌的根目录设为 `/`。
- **外网播放暂停/恢复**：再启用修改传输任务（`allow_modify_transfer_tasks`）。
- **可选功能**：仪表板或 HomePage 再启用运行信息和空间信息（`allow_view_runtime_info`、`allow_get_space_info`）。离线下载和重启分别需要 `allow_add_offline_download` 和 `allow_service_control`。

令牌直接用于 Bearer 认证。到期或被撤销后，创建新令牌替换原值即可。更换令牌时保留原实例名和 gRPC 地址，以便接续已暂停的任务。旧的 `名称#地址#用户名#密码` 格式仍可用。从旧格式切换前，若插件暂停了上传，请先关闭「外网播放时暂停上传」，保持插件启用，确认任务恢复后再换令牌。

[CloudDrive2 官方 API 开发者指南](https://www.clouddrive2.com/api/CloudDrive2_gRPC_API_Guide.html)
