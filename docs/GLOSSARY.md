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

`set_quarter` 也各一份，但它比上面几个更该分开：番剧那份改的是**季度**（季母有意义，
开了 `ANIME_SEASON_SUBFOLDER` 时还影响 `Season N` 子目录）；剧场版那份只用其中的**年份**
（页面上那一栏就叫「年份」，归档目录走 `MOVIE_QUARTER_FMT`，默认 `{yyyy}`）。
两者存的都是季度键、共用同一套格式校验，但**问用户的问题不一样**——剧场版详情页收的是四位年份，
拼成 `{yy}A` 再存。合并会把两个不同的概念挤进一个函数。

`exclude_torrent`、`unexclude_torrent`、`reset_downloading`、`sync_qb_status` 是**门面**：
真实现在 `core/engine.py`，两条线各转一层把自己的表带进去（各自 docstring 都写着「实现见 engine.*」）。
改行为改 engine 那一份；两个包装只负责选表。


## 六、容易读成"标签就是内容"的两个字段（r17）

| 名字 | 它到底是什么 |
|---|---|
| 备份文件名里的 `scope`（`full` / `meta`） | 是**导出那一刻的配置**推出来的标签（`DB_BACKEND == 'mysql'` 就叫 meta），**不是文件里有什么**。<br>两个方向都会骗人：刚建好还没跑过业务的库标 `full` 而里面一行数据都没有（用户机器上 5 份里有 4 份是这样）；切了 MySQL 之后标 `meta` 的那份里往往还躺着整套（旧的）业务数据——因为导出动作恒为对本地文件整个 `VACUUM INTO`，而 `switch_data_engine` 明写"只切连接，不搬数据"。<br>**要回答"这份救不救得回我的番"，只能用 `backup.has_business_data()` / `describe_content()` 现场数。** |
| `Movie.quarter` | 列名叫季度，**剧场版只用其中的年份**（归档目录 `MOVIE_QUARTER_FMT` 默认 `{yyyy}`，页面上那一栏就叫『年份』）。<br>而它的值由 `quarter_of()` 算出来，那个函数带着**番剧**的规则「12 月归次年冬季」——对季播番是对的（12 月开播实际跨 1–3 月播完），对一次性上映的剧场版是错的。<br>真库 5/70 部因此归档到次年，见 DECISIONS **E-30**。取年份一律走 `core.engine.quarter_year()`。 |

## 七、本轮新增的名字（r17）

| 名字 | 为什么这么叫 |
|---|---|
| `notify._rate_ok` / `_rate_commit` | 【只查】与【记账】拆成两步。桶按**送达**计数（用户口径就是"每小时最多收到几条"），所以 `_rate_commit` 必须在 `await notify()` 返回 True 之后才调。`_state_rate_ok` / `_state_rate_commit` 同理。 |
| `notify._muted_until` / `_fail_streak` | 连续失败熔断，与限流桶**正交**：桶管"发太多"，熔断管"发不出去还一直试"。`notify()` 串在交付主链路上，地址是黑洞时每条要白等满 `NOTIFY_TIMEOUT`。 |
| `backup.describe_content` | 特意不叫 `describe`：这个模块里"描述一份备份"最容易被读成文件名/大小/时间（那些 `list_backups` 已经给了），而它说的是**里面有几部番**。 |
| `anime.sweep_alerts._two_sides` | 番剧与剧场版两边的计数拼成一句话。特意不叫 `_both`——`_both(a, b, unit)` 读不出哪个是哪边。 |
| `base.warn_if_not_a_feed` | 判据不是 `bozo` 而是 `feed.version`。理由（哪几种"200 但不是 RSS"是 `bozo=False`）写在函数 docstring 里，两条解析路径共用这一份。 |

## 八、设计语言：按钮的角色语法（r18）

按钮的样式**只由角色决定**，写法是 `{形态} {round} {dense} {color} {no-caps}`：

| token | 含义 |
|---|---|
| `unelevated` | 实心无阴影 = **主操作**（确认 / 保存 / 绑定） |
| `outline` | 描边 = **触发器**（点开还有下一层：下拉菜单、批量动作） |
| `flat` | 无底色 = **取消与次级** |
| `dense` | **行内**（列表行、元操作行）。**不表示"更小"** |
| `no-caps` | 只在标签含拉丁字母时需要（Quasar 默认转大写，中文不受影响） |

**尺寸只有一个开关**：默认 14px；加 `.classes("btn-sm")` 得到 12px。

⚠️ **不要用 Quasar 的 `size=`**。它渲染成【行内 font-size】，档位只有 `xs8/sm10/md14/lg20/xl24`，
本项目要的 12px 不在表里——曾因此出现 23 个按钮写 `size=sm` 再叠 `.style("font-size:12px")`
把 10 掰回 12，而同一个 `flat dense size=sm` 在两处被掰成 12px 与 14px。
`tests/test_ui_badge_style.py` 的两条守卫盯着这件事：props 组合必须在角色词表里登记，
调用点不许写 `font-size`。

与徽标那一节是**同一条原则**：尺度由全局定，调用点只选角色。
