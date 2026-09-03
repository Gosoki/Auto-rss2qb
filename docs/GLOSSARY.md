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

---

## 九、R21 新增的几个词与纪律

### `db.maintenance(reason, blocked_by=...)` —— 整库维护闸

"切库"与"整库迁移"这两件事会把整个业务库换掉/清空重写。期间 `db.get_session()`
一律抛 `DatabaseBusy`，`is_data_down()` 为真（后台四条循环按停摆跳过本轮）。

**闸为什么装在 `get_session()` 上**：全仓 316 处业务库访问全部走它，
而 `db/transfer.py` / `db/schema.py` / `db/backup.py` 用的是显式引擎、一处都不走它 ——
这一个点既拦得住全部业务读写，又拦不到维护自己。装在别处必然漏。

**`blocked_by` 为什么收在 `maintenance()` 里面**：它挡的是"交付协程跨 await 持着旧库的
整数主键、维护结束之后才回写"那一种（`transfer` 保留主键，两个库里 id=501 是两条毫不相干的
种子）。闸只挡得住维护**期间**的读写。调用方"先查一遍再进维护"的写法中间隔着一个
`await confirm(...)`，窗口大得能开进一整轮交付；放在里面，检查与置位之间没有 await。

### `observation_gap_seconds()` —— 观测断档窗口

"这段空白不能归因于种子"的判定阈值。**必须按 worker 最慢的那一档正常节拍算**
（`QB_IDLE_RECHECK_MIN`），不是活跃节拍（`QB_SYNC_INTERVAL`）。
按错了的后果不是"偶尔误判"，而是**停滞检测整个功能从未生效过**（R21 复现）。

### `_recheck_where()` 与 `_inflight_where()` 必须互斥

`recheck` 的语义就是"被 `_inflight_where` 排除掉、需要额外问一遍的行"。
两条 WHERE 一旦重叠，同一行会在 `rows_all` 里出现两次，并被 `if tid in recheck_ids: continue`
**整个跳过落定** —— 该行永远落不了定，而 `sent ∈ HAVE_STATUSES` 让集去重认定这一集已有一份、
盘上却什么都没有。互斥由 `_recheck_where` 末尾那个 `or_`（= NOT in-flight）在 SQL 里保证。

### `host_allowed()` —— Host 头白名单

挡 DNS 重绑定。浏览器不允许脚本伪造 Host，所以这一条就能根治。
放行：字面 IP / `localhost`·`*.localhost` / `.local`·`.lan`·`.internal`·`.home.arpa` / `WEB_ALLOW_HOSTS` 里的域名。（E-45：`*.localhost` 是守卫一直放行、文档一直漏写的那一条）
**按 IP 访问永远放行** —— 这是域名填错时唯一的自救出路，改这条判据时不能把它堵死。

### 三条一直在复发的形状，R21 又各中了一次

① **广度**：同一个决定应该在 N 处生效，只落了 1 处
（`_sqlite_engine` 的 PRAGMA、`enrich_movie` 的不合并、`_has_unmovable_files`、`http_client_kwargs(url=)`）。

② **作用域**：约束的范围大于验证的范围
（迁移只锁两把轮次锁、页面写入口不受约束；徽标符号守卫只扫字面量）。

③ **假用例**：断言自己传进去的参数、用字符串匹配源码、拿同一条链的产物跟自己比。
R21 里我自己写的守卫就中了两次（索引一致性、分页尺寸），都是靠**当场变异验证**才发现的。

**纪律：新写的守卫必须当场做一次变异验证** —— 把它声称守住的东西破坏掉，确认它真的会红。
没做过变异验证的守卫，有相当概率是假的。

---

## 十、R22 记的几处命名（含一处已改、若干条待你判断）

### 已改：`_state_muted` → `_state_suppressed`（services/notify.py）

本模块里 **mute 这个词根已经被熔断占用了**（`_muted_until`：连续失败 N 次后整个通知模块
静音 M 秒）。两个概念完全不同：

| | 熔断静音 `_muted_until` | 状态型小桶 `_state_suppressed` |
|---|---|---|
| 范围 | **所有** kind 一起不发 | 只压住某一个 kind |
| 触发 | 推送地址是黑洞（连续失败） | 该 kind 一小时内已真送达 12 条 |
| 解除 | 墙钟 300 秒 | 滚动一小时窗口自然过期 |

共用一个词根会让人以为它们是同一套机制的两半 —— 而实际上一个是"地址坏了"，
一个是"你已经被这件事通知够多次了"。

### 待你判断的几个（我没动）

| 名字 | 读起来像 | 实际是 | 我的看法 |
|---|---|---|---|
| `engine.maintenance_blockers()` | "维护会挡住哪些东西" | "**现在不能开始**维护的理由清单" | 建议改 `why_maintenance_cannot_start()` 或 `blocked_from_maintenance()`。语义反了一半，而它是安全闸，读反的代价不小 |
| `worker.loop_error(what, e)` | "循环结构出错了" | "**后台循环**的异常记账出口" | 建议 `log_loop_exception`。现名两个词都可以当形容词读 |
| `db.data_down_reason()` vs `db.maintenance_reason()` | 两个平级的"原因" | 前者**包含**后者（维护也算"此刻不能用"） | 不建议改名，但两处 docstring 要互相点名（已补）。改名会牵动 8 个调用点，收益不抵风险 |
| `_QB_NEEDS_RECHECK` | "需要补查的态" | "需要补查的态，**但两个来源的搭车条件不同**"（missingFiles 搭车、过渡态无条件） | 拆成两个常量更准，但 R21 已经把差异写进 `_recheck_where` 的 docstring，先留着 |
| `existing_hashes(model_cls, hashes)` | 可能被读成"取出全部已有 hash" | "这批里哪些**已经在库里**" | 名字够准，只是第一个参数是表、第二个才是待查集合，调用时容易写反 —— 已由类型天然挡住（传反会 AttributeError） |

## 十一、本轮新增的名字（r27）

| 名字 | 为什么这么叫 |
|---|---|
| `pages/anime._kw_hit(a, kw)` | 番剧表搜索框的匹配判据。特意**不叫 `_match`**——本项目里 `match` 已经被"番名 → 番"的绑定链占满了（`resolve` / `bind_*` / `_merge_*`），再来一个 `_match` 会被读成"把这个词匹配到某部番上"，而它只是个**大小写不敏感的子串命中**，不产生任何绑定。`kw` 与 `Anime.pref_keyword`、`SourceGroup.title_filter` 同一个词根，都是"用户自己填的一段过滤文本"。 |
| `core/anime._binding_looks_wrong_rows(a, rows)` | `binding_looks_wrong(s, a)` 的**判据本体**，只是种子行已经取好了。名字里带 `_rows` 就是在说"差别只在参数形态，判据是同一份"。两个形态并存的理由：单部番的 4 个闸顺手查一次最简单，而全库扫逐部番查是 N+1（真库 99 部实测 120→26ms）。**判据只有这一份实现**，别抄第二遍。 |
| `core/anime._EpRow` | 全库扫时按列投影出来的最小种子行（只有 `episode` / `release_time`）。字段名必须与 `AnimeTorrent` 逐字相同——判据是同一份代码，靠鸭子类型对上。取名 `_EpRow` 而不是 `_TorrentRow`：它**不是**一条种子的全部，只是"判集号归属"要的那两列，叫全名会让人以为可以拿它当种子用。 |
| `config.QB_CATEGORY_ANIME` / `_MOVIE` / `_MANUAL` | qB 分类名。**不叫 `QB_TAG_*`**：本项目的 tag 带的是语义（季度/年份/`Manual`），category 才是归大类的标签。三个键分开而不是一个模板，因为三条投递路径的用户心智本来就是分开的（"番剧下到哪一堆"和"我手点的下到哪一堆"是两件事）。 |

### 这一轮**没有**新增的歧义名

`suspect_wrong_binding` / `suspect_duplicate_anime` 这一对（r26 加的）复查过：
`suspect_` 前缀在本项目里恒定表示"**只读检测、只报不改**"，两处都兑现了。
再加同族函数时沿用这个前缀，别用 `check_` / `detect_`——那两个词在本项目里
已经被"会改状态的闸"（`apply_start_date_filter`、`sweep_*`）占着了。

## 十二、R28 新增/澄清的几个名字

| 名字 | 为什么这么叫 |
|---|---|
| `core/ssrf._SHARED_V4` | RFC 6598 的"共享地址段" 100.64.0.0/10。**不叫 `_CGNAT`**：Tailscale 用它、运营商级 NAT 用它、别的东西也可能用它，按 RFC 的名字命名才不会让人以为"我没用 CGNAT 就跟我无关"。它必须显式写出来，因为 CPython 3.12.4 起把这一段的 `is_private` 改成了 False —— 判据一旦只写"语义"就会随解释器版本漂。 |
| `pages/anime._build_manage_tab` | 番剧表 tab 的**外壳**：先建一次稳定的搜索行，再建可刷新的 `manage_panel`。特意不叫 `manage_tab`（那读起来像"这个 tab 的全部内容"）——它的职责恰恰是"哪些东西**不**跟着刷新走"。 |
| `pages/parse_test._VERDICT_SHADE` | 判定横幅的色号表。取名带 `_SHADE` 是在说：这里存的是**色号**（`text-green-500`），不是色相名 —— 上一版正是把色相拿来拼 `f"text-{color}-400"`，色号写死在拼接里，于是绿那一档与全站的绿不是同一个颜色。 |
| `busy_action` 的键前缀 `mov-` | 剧场版侧的防抖键（`mov-bind:` / `mov-refail:`）。`busy_action` 的去重键是**模块级**的，番剧与剧场版的 id 空间各自独立 —— 不加前缀的话 `bind:7` 会让"番剧 #7"和"剧场版 #7"互相挡住。 |

### 一条**没改**的歧义名（记着）

`sources/parse._EP_END` 读起来像"集号的结束位置"，实际是**右界锚点**（行尾 / 标签块起始 /
当分隔符用的连字符），而且它内部还藏着一条"后面不能紧跟裸集号"的负向前瞻 ——
那条前瞻才是判"这是不是连续集区间"的**第三道**判据（另两道在 `_BATCH_RE` / `_BARE_RANGE_RE`）。
R28 那条 P1 正是因为这三道判据的位数上界不一致。
改名（如 `_EP_RIGHT_BOUND`）会牵动三条同族正则的注释，收益不大；
现已由 `test_batch_guards_are_at_least_as_wide_as_episode_extraction` 把三者绑在一起。

## 十三、R29 的两条命名笔记

| 名字 | 为什么这么叫 |
|---|---|
| `db/backup._peek` 里新增的 `backend` / `mysql_db` | 回答的是「**恢复这份之后**业务库会指向哪」，不是「这份备份是从哪个库导出来的」。两者在本项目里恰好同义（备份恒是对本地 meta 文件的整文件快照），但语义方向不同 —— 页面文案统一按前者写（『→ 本地 SQLite』/『→ host/db』），因为用户在那一刻要做的决定是"要不要恢复它"。 |
| `worker.archive_round` / `worker.sweep_round` | R27 抽出来的两个模块级轮次函数。`_round` 后缀在本项目里恒表示「一整轮、且**全程持着那一轮的锁**」—— 驱动者必须调它、不许绕过去直接调里面的 `engine.*`（R29 补了驱动者级用例钉这一条）。 |

### 这一轮确认过、**没改**的一处

`db/backup.py` 里 `scope`（文件名里的 `full`/`meta`）与新增的 `backend` 是两件事，
容易被读成同一件：前者是**导出那一刻的配置标签**（`_peek` 的 docstring 早写明它会骗人），
后者是**文件里 setting 表的实际值**。页面上两者并排显示、颜色规则不同
（`has_data` 绿/橙看内容，`backend` 蓝灰/橙看是否与当前一致），已在 `_peek` 处写清。

## 十四、R30：一个**从没写下来过**的词表

`AnimeTorrent.episode` 有**四**种取值，而模型注释原来只写了三种：

| 取值 | 含义 | `auto_downloadable_ep`（能不能自动下） | `episode_coverage` / `episode_progress`（算不算已下） | `flush` 的 `have_eps`（参不参与集去重） |
|---|---|---|---|---|
| `>=1` | 正片，含 `.5` 插入话 | ✓ (`ep >= 0`) | ✓ (`>= 1`) | ✓（不设阈值） |
| **`0`** | **第0話 / 前导集** | **✓** | **✗** | ✓ |
| `-1` | 特别篇 | ✗ | ✗ | ✓ |
| `-2` | 未知 / 疑似批量 | ✗ | ✗ | ✓ |

**三个阈值不同是有意的，别去"对齐"**：
- 能不能下用 `>= 0` —— 第 0 集在周更序列上；
- 算不算覆盖用 `>= 1` —— bgm 的 `total_episodes` 从 1 数起，把 0 计进分子会让"已下"超过总数；
- 集去重**不设阈值** —— 否则 0/-1/-2 的行不参与去重，同一条会被重复下。

守卫：`tests/test_parse.py::test_episode_zero_is_a_real_episode_not_an_error_code`
与 `::test_the_model_comment_lists_every_reachable_episode_value`。

| 名字 | 为什么这么叫 |
|---|---|
| `core/anime.suspect_movie_as_anime` | 沿用 `suspect_` 前缀（本项目里恒表示"只读检测、只报不改"）。方向写在名字里：**movie as anime** —— 报的是"一部剧场版在番剧表里也有一条"，而不是反过来。反过来那种（一部 TV 番在剧场版表里）今天不存在，真要出现应当另起一个名字，别把两件事塞进一个函数。 |

## 十五、R33：E-46 落地带来的几个词

| 词 | 指什么 | 别读成 |
|---|---|---|
| **跨主键（PK straddle）** | 一个协程"读了业务库的整数主键 → `await` → 按那个主键写回"。切库横在 await 中间时，写回落进另一个库里同 id 的另一行（`db/transfer.py` 保留主键） | 不是"并发写同一行"——那是锁的事；这是**库换了**，锁救不了 |
| **工作单元** | 一段从第一次读主键到最后一次写回都应被同一道闸看见的代码。它是**闭集**（有限个入口）；"写回点"是开放集（R21→R27 四次数不全的那个） | 不是"一次请求"、也不是"一个 task" |
| **在途登记 `engine.in_flight(label)`** | 只登记、不串行的上下文管理器；`maintenance_blockers()` 把登记着的 label 列成理由。用于**页面驱动**的工作单元（没有轮次锁、也不是我们 create_task 的） | 不是锁。两个不相干的页面操作**可以**同时在途 |
| **`wrapper → _<name>_inner`** | 页面入口的两层写法：外层只做登记，逻辑全在 `_inner`。按公开名取 AST 找到的是**壳** | 守卫要断言函数体时必须经 `conftest.impl_of` 找 `_inner`（R33 已撞过一次：正向守卫静默做空） |
| **`_STRADDLERS` 登记表** | `tests/test_engine_lifecycle.py` 里的{模块: {函数名: 被哪道闸看见}}。值只有五种：`lock:<锁名>` / `delivering` / `sync_busy` / `in_flight` / `caller:A\|B` | 不是文档——它被三条用例核：形状扫描（漏登记红）、真锁拿住问闸（假锁红）、caller 链走到底不绕圈 |
| **有界地等 `wait_until_quiet(30)`** | 维护入口先等最多 30 秒让在途的工作单元收尾，等不到才拒。盖住"一个取种/识别正在半途"这种最常见的忙 | 不是无限等（整轮采集可能几分钟，R22 的理由仍成立），也不是"停"（不取消任何协程） |
| **`background_tasks`** | `main.py` 里存的七个后台协程句柄。今天只是按 asyncio 官方要求持强引用；将来要做"停"从这里下手 | 现在**没有**任何地方取消它们 |


## 十六、E-8：`pending` 的语义重载 —— 已知、代价已接受（2026-09-02 拍板 A：不拆）

`AnimeTorrent.status == "pending"` 承载着五种"还没落盘"的情形，程序按**别的列**分辨它们：

| 它其实是 | 怎么分辨 | 谁在用 |
|---|---|---|
| 新入库、等 flush 放行 | `retry_at IS NULL`，番在 `subscribed_where()` 里 | flush / 详情页『将下载』 |
| 暂时性失败、排队重试 | `retry_at IS NOT NULL` | flush 到点重发 / 详情页『重试中·第N次』 |
| 番待确认 / 已忽略 / 已停订，永远不会自动下 | 番的 `confirmed` / `rejected` / `finished_at` | 详情页三条橙/灰徽标（R31） |
| 特别篇 / 未知集，永远不会自动下 | `episode < 0`（`auto_downloadable_ep`） | 详情页紫徽标 |
| relocate 搬不动、被清回来等重下 | `save_path` 空 + qB 实时态被清 | 下一轮 flush |

**为什么不拆成五个状态词**：状态词表是"逐层包含"的（`TRACKED ⊂ HAVE`、`DOWNLOADABLE`、`MANUAL_TERMINAL`），
9 处写入点、十几处读取点都按这张表判；拆词等于把每一处都改一遍，而分辨它们的信息**本来就在别的列上**。
`pending_breakdown` 把五桶分开数给仪表盘看，这就是"靠注释与分桶"的那一半。

**已经付过的代价**（记在这里免得再付一次）：E-3（qB 消失的终局落什么）与 E-4（relocate 对 -1/-2）
都是这个重载的具体账单 —— 两处都错在"把它清成 pending"之后**以为有路径会再下它**。
新加一条"把 X 清成 pending"的代码之前，先回答：五种里它是哪一种，谁会来把它下回去。

## 十七、E-3：`error` 与 `stalled` 从此各只剩一种语义（2026-09-02 拍板 B）

| 状态 | 唯一语义 | 谁写它 | 之后会怎样 |
|---|---|---|---|
| `error` | **从没交付出去**：取种失败退避用满 / qB 拒收 / 路径越界 / 从 qB 消失且**一个字节都没下到** | 交付路径的 `_fail`、sync 的消失分支（progress == 0） | ∉ HAVE ⇒ 可换源、可『补下』、flush 不自动重试 |
| `stalled` | **交付过、盘上有半成品**：qB 报 error / 长期无推进 / 从 qB 消失但**有过进度** | sync 的三处（每处都写 `fail_reason`，详情页徽标 tooltip 用） | ∈ HAVE ⇒ 集去重挡着、**不进补下、不自动换源**；要人工在详情页『下载』重试或『删除』 |

E-19 立的原则、E-3 把最后一条分叉（"从 qB 消失"）补齐。**已接受的代价**：一条下到 99% 后被用户在 qB 里删掉的种子，
现在停在 stalled 等人工，而不是自动换源再下一份 —— 因为那一份半成品还在目录里，自动换源会往同一个目录再放一份。

## 十八、E-57：『交付中』的占位，只有一个复位点了（R35）

| 词 | 指什么 |
|---|---|
| **占位（placeholder）** | 交付协程进锁时把 `status` 置成 `downloading` 并记下 `prev_status`。归档标记与 qB 实时态**留在行上不动**（E-49），清理挪到交付成功那一刻 |
| **残骸** | 落库是 `downloading`、但 `engine._delivering` 里没有它 —— 本进程没有协程在管这一行 |
| **`engine.sweep_stale_delivering()`** | **唯一**的落定点（async）。先问 qB：里面有它 → 按已交付落定；没有 → 按 `prev_status` 还原；**连不上 → 一行都不动**。qB 关着时不必问 |
| **`worker._stale_sweep_pending`** | "欠着一次清扫"。`init_business_state` 只记账（它是同步的、还跑在线程里，await 不了）；`worker.sweep_leftovers_if_pending()` 在协程里消费，持 `_sweep_lock` |

**别再写第二个复位点**：`reset_downloading` 那三个函数（engine/anime/movies）在 R35 已删。
理由是它同步、问不了 qB，于是对"崩在 `add_to_qb` 之后"的行会给出与清扫**相反**的结论 ——
两个复位点、两种口径，正是本项目第①号缺陷形状。
