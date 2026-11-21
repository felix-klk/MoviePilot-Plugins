# JAV字幕助手

MoviePilot V3 插件，ID 为 JavSubtitleAssistant。基于海拉鲁字幕大师 V3 1.2.39，沿用它的资源卡片、字幕搜索、队列、匹配历史、配置和字幕写入流程。两款插件使用独立的状态和前端入口。

## 使用

1. 在 MoviePilot 的插件市场添加 [插件仓库](https://github.com/felix-klk/MoviePilot-Plugins)，安装或更新 JAV字幕助手。
2. 打开配置并启用插件。在「监控本地整理目录」填入 MoviePilot 可读写的转移后路径，每行一个。原 JAV 版的目录会沿用。
3. 从插件详情或整理分组侧栏打开 JAV字幕助手。使用番号搜索本地资源，点击卡片可以查看视频和匹配字幕。启用本地视频自动补字幕后，启动时会检查已有资源，后续新增和变化进入原有任务队列。

自动模式按番号和分段匹配，已有中文字幕即跳过，自动写入沿用下载字幕的时间轴，手动模式可选择字幕并调轴。

## Subtitle Cat 请求间隔

Subtitle Cat 是默认字幕源。插件内下载串行，上一份字幕下载结束后至少等 8 秒才开始下一份。Subtitle Cat 搜索、详情和字幕请求之间至少等 3 秒。站点返回 HTTP 403、429 或 503 时暂停访问至少 15 分钟，若下载响应要求更久则按站点时间等待。

[海拉鲁字幕大师的 GPL-3.0 实现](https://github.com/felix-klk/MoviePilot-Plugins/tree/bfe965fed80fc1f837d9930927e77ff271212ebf/plugins.v3/subtitlemanualupload)
