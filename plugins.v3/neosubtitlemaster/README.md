# NEO字幕大师

MoviePilot V3 字幕插件，维护者为 [felix-klk](https://github.com/felix-klk)，插件 ID 为 `NeoSubtitleMaster`。

支持在线字幕搜索、手动上传和匹配、智能调轴、Emby 字幕查询、本地目录监控和缺字幕自动补搜。支持设定双语偏好，并在已有合格字幕时跳过对应视频。

## 使用

1. 在 MoviePilot V3 添加 [felix-klk 的插件仓库](https://github.com/felix-klk/MoviePilot-Plugins)，在市场安装「NEO字幕大师」。
2. 在插件设置中启用插件，选择在线字幕源和语言。使用本地目录补搜时，填写 MoviePilot 能访问的绝对路径。
3. 保存后可用「检查已有视频」补查存量，或开启「缺字幕时自动补搜」处理新增视频。

NEO字幕大师使用自己的设置、索引和自动入库队列。首次安装后请在新插件中保存需要的设置。

同时使用 AI字幕生成(联动版)时，若由 NEO 负责入库自动补搜，可将 AutoSubv3 设为后备模式，由 NEO 发起需要的 AI 任务。

已知问题、修复步骤和验收条件见 [NEO字幕大师修复方案](https://github.com/felix-klk/MoviePilot-Plugins/blob/main/docs/NEO%E5%AD%97%E5%B9%95%E5%A4%A7%E5%B8%88%E4%BF%AE%E5%A4%8D%E6%96%B9%E6%A1%88.md)。

## 许可与致谢

基于 [ifsherlock 的海拉鲁字幕大师](https://github.com/ifsherlock/MoviePilot-Plugins) 延续开发，字幕匹配工作参考了 [ChineseSubFinder](https://github.com/ChineseSubFinder/ChineseSubFinder)。部分字幕源接入参考 [ChineseSubFinder-optimization](https://github.com/morningstar-ski/ChineseSubFinder-optimization)。按 [GPL-3.0](LICENSE) 发布，转换词库附带其原始许可证。
