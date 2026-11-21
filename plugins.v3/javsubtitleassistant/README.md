# JAV字幕助手

MoviePilot V3 插件，ID 为 `JavSubtitleAssistant`。在海拉鲁字幕大师 V3 1.2.38 的 Subtitle Cat 搜索和下载实现上适配本地 JAV 文件，按视频番号和分段下载一份缺少的中文字幕。

## 安装和配置

1. 在 MoviePilot 的插件市场设置中添加 [felix-klk/MoviePilot-Plugins](https://github.com/felix-klk/MoviePilot-Plugins)。刷新市场后搜索并安装「JAV字幕助手」。
2. 打开插件配置，填写转移后目录，每行一个 MoviePilot 进程可读写的绝对路径。勾选「启用插件」和「保存后立即扫描」，再保存配置。定时扫描默认每小时运行。
3. 打开插件详情查看发现的视频数、已有中文字幕、写入和失败明细。也可在服务列表手动运行「JAV字幕助手 · 扫描本地目录」。Subtitle Cat 访问失败时可以检查网络，按需要勾选使用 MoviePilot 系统代理。

本地开发时，可以把带有 `package.v3.json` 和 `plugins.v3` 的仓库目录加入 `PLUGIN_LOCAL_REPO_PATHS`。Docker 部署需要先将该仓库目录挂载进 MoviePilot 容器。配置的媒体路径应指向整理后的媒体库。

## 字幕判断

- 对每个视频版本分别判断。已有简体、繁体、粤语或含中文的双语字幕时停止，强制中文字幕同样算已有中文。仅有英文或日文字幕时继续搜索。
- 检查同名外挂字幕和内嵌轨道。内嵌字幕优先参考语言标记和标题，其次通过 FFmpeg 检查文字轨道。未标语言的图片字幕无法确认语种。对 `.strm` 只检查本地外挂字幕。
- 优先用同名 NFO 里的显式番号，随后尝试文件名。单视频目录还会尝试 `movie.nfo` 和目录名。按番号和分段匹配，只下载网站已有的中文 SRT。校验后保存到视频旁，例如 `ABC-123.zh-CN.srt`，已有字幕文件会保留。
- 每轮最多联网搜索 40 部视频，下一轮从上次进度继续。定时扫描暂缓近期检查过的文件，保存后立即扫描可重试暂缓项。

普通视频的内嵌字幕检查使用 MoviePilot 环境里的 `ffprobe` 和 `ffmpeg`。媒体无法探测时，本轮跳过该视频并写入插件日志。

Subtitle Cat 解析与下载流程改编自 ifsherlock 及贡献者的海拉鲁字幕大师，按仓库 GPL-3.0 许可分发。 [海拉鲁字幕大师 V3 1.2.38](https://github.com/felix-klk/MoviePilot-Plugins/commit/ff8d8ed0f623c277de66cf70d76074825eacf1b1) · [MoviePilot V3 插件开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/Plugin_Development.md)
