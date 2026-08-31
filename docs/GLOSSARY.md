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
| `pages/layout.parse_bgm_id` | `core/manual.parse_bgm_id`（唯一实现） | 两份行为逐条相同（实测 7/7 一致），但调用点分家：手动下载走 core 那份、四个 UI 绑定入口走 pages 那份。E-13「收紧 parse_bgm_id 判据」在这个状态下实施必然只改一半 |
| `core/engine.quarter_year`（自己算） | 转出 `sources.parse.quarter_year` | 它曾写 `2000 + int(q[:2])`、docstring 还宣称「只此一份」，而 `format_quarter` 的 `{yyyy}` 另写一份 `"20"+yy`——两份共享同一个「两位年一律当 20xx」的错 |
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

## 四、同名但**不是**同一件事（别互相替换）

> 由 `tests/test_single_definition.py` 的白名单守着：跨文件的模块级重名必须在那里登记并写清理由，
> 新增未登记的重名会当场变红。

| 名字 | 两处的差别 |
|---|---|
| `verify` | `db/backup.py` 问的是「这份备份**打得开吗**」→ `(bool, str)`；`db/transfer.py` 问的是「迁完**行数对得上吗**」→ 不一致项的列表（空=一致） |
| `_parse_date` | **接受的格式与返回类型都不同**。`core/anime` 收 `YYYY-MM-DD` / `YYYY-MM` / `YYYY`（**允许残缺日期**）→ 返回 `date`；`services/enrich` 收 `YYYY-MM-DD` / `YYYY/MM/DD` / `MM/DD/YYYY`（**拒绝残缺日期**）→ 返回 `datetime`。<br>注意方向：给 bgm 数据解析日期的是**严格**那份，所以 bgm 若给出 `"2026"` 这种残缺日期，`air_date` / `quarter` / `air_weekday` 会**整组落空**（真库当前 0 部中招，属潜在路径） |
| `overview` | 番剧侧按季度三桶（订阅/审核/忽略），剧场版侧按年份（电影数/已下）。量纲不同，别拿来互相校验 |

## 五、番剧 / 剧场版的对称实现（有意，不要合并）

`_has_handled_torrents`、`_set_status`、`_terminal_torrent_rows`、`deleted_torrent_rows`、
`excluded_torrent_rows`、`failed_rows`、`source_map` 在 `core/anime.py` 与 `core/movies.py` 各一份——
两条线操作的是不同的表，合并只会多出一层表参数。

`exclude_torrent`、`unexclude_torrent`、`reset_downloading`、`sync_qb_status` 是**门面**：
真实现在 `core/engine.py`，两条线各转一层把自己的表带进去（各自 docstring 都写着「实现见 engine.*」）。
改行为改 engine 那一份；两个包装只负责选表。
