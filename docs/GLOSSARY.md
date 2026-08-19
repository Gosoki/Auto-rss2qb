# 术语表

两条线（TV 番剧 / 剧场版·OVA）分表、分模块、互不相干，界面用词因此有分歧。
这份表把**哪些分歧是有意的、哪些是历史遗留**写下来——否则每一轮审计都会重新把它们当成 bug 报一次。

## 一、有意区分（**不要**统一）

| 番剧侧 | 剧场版侧 | 为什么不统一 |
|---|---|---|
| 「待下」 | 「可下载」 | 番剧的 pending 是**后台到点会自动下**的；剧场版的 pending 是**等着你去点**的。同一个 `status="pending"`，但对用户的含义相反：一个是"不用管"，一个是"该你了" |
| 「一集一份」 | 「版本」 | 一部剧场版真的有多个发布版本（不同字幕组 / 分辨率 / BD 重制），逐个人工挑；番剧是同一集选一份最优的。强行统一成「种子」会把这个区别抹掉 |
| 「季度」(26C) | 「年份」(2026) | 同一个 `quarter` 列，但剧场版整条线按年归档（`MOVIE_QUARTER_FMT` 默认 `{yyyy}`）。取年份**一律**用 `core.engine.quarter_year()`，别再手写 `q[:2]` |

## 二、同一件事的两个名字（历史遗留，已消掉）

| 曾经 | 现在 | 消掉的原因 |
|---|---|---|
| `ParsedItem.source_kind` | `ParsedItem.policy` | 它就是 `SourceGroup.policy` 的透传 |
| `_select_downloads` | `_download_candidates` | 原名说的是「选出所有要下的」，实现却是「每集一串候选」——那正是一条 P1 缺陷的认知来源 |
| `core/engine._SITE_TZ` 字典 | `RssSource.TZ` 类属性 | 全项目唯一一处「新增源时漏改**不报错**、只是发布时间整天错」 |
| `config.MIKAN_SUBGROUPS` | `SourceGroup.subgroups` | 死配置键，真正生效的一直是后者 |
| `core/engine._host_is_internal` | `core/ssrf.block_internal_request` | 孤儿函数，且**不钉 IP**——留着会被误当成防线 |
| 三处各写各的「重试识别」 | `core.anime.manual_enrich()` | 详情页清 `enrich_tries`、『待识别』tab 不清，同名按钮两种行为 |

## 三、容易读反的字段

| 字段 | 别按字面读 |
|---|---|
| `AnimeTorrent.quarter` | 入库时的解析快照，**不权威**。决定保存目录的是 `Anime.quarter` |
| `Anime.finished_at` | 只是个**标记**。停不停订由 `config.ANIME_FINISH_UNSUB` 决定 |
| `Anime.finish_optout` | 用户点过『继续订阅』＝此后不再自动判完结。**必需**，否则那个按钮点了没用（判据是状态式的，下一轮立刻重判） |
| `is_subscribed_row` | **不是**完整判据（曾有人按名字这么以为）。完整判据是 `subscribed_where()` |
| `Movie.mikan_id` | Mikan 番组 id，用于『刷新版本』按钮按需重拉该片的种子 RSS |
| `download_anime_torrent` 的返回值 | 三态：`True`=已接受 / `False`=明确拒绝 / `None`=够不着（不可判定），不要当布尔用 |
