"""数据模型（SQLModel）。

TV 番剧与剧场版/OVA 彻底分表、互不相干（各自独立的表 + 独立的 anime.py / movies.py 逻辑）：

Setting     —— 键值配置覆盖（设置页写、运行时读、即时生效）。
SourceGroup —— 订阅的字幕组/源组（feed/策略/优先级/白名单）——只喂 TV 周更番。
Anime       —— 一部 TV 番剧（唯一）。身份 = bangumi_id。含 bgm 元数据。下不下 = confirmed 且未 rejected。
AnimeAlias  —— 番名对照：(标题, 季) → 哪部 TV 番。命中即知是谁，不必再查 bgm。
AnimeTorrent     —— 一条 TV 种子，按 info_hash 唯一；anime_id 关联到 TV 番。含 qB 实时态镜像（qb_*）。
Movie       —— 一部剧场版/OVA（唯一）。来源仅 Mikan 季度剧场版/OVA 桶，识别用 bgm。与 Anime 无关。
MovieTorrent—— 一条剧场版/OVA 种子，按 info_hash 唯一；movie_id 关联到 Movie。含 qB 实时态镜像。
"""
from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class Setting(SQLModel, table=True):
    """键值配置覆盖：设置页写这里，运行时读，改了即时生效（不必重启）。"""
    key: str = Field(primary_key=True)
    value: str = Field(default="")


class SourceGroup(SQLModel, table=True):
    """一个订阅源组（字幕组）。worker 每轮据此重建源；策略/优先级可在 UI 改。"""
    __table_args__ = (UniqueConstraint("name", name="uq_sourcegroup_name"),)

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)              # 展示名，唯一
    site: str = Field(default="nyaa")          # 'nyaa' | 'mikan'
    feed: str = Field(default="")              # nyaa: 用户名或完整RSS URL；mikan: RSS URL
    policy: str = Field(default="auto")        # 'auto' 全下 | 'review' 进待确认队列
    priority: int = Field(default=0)           # 越大越优先（多源同一集选高的）
    subgroups: str = Field(default="")         # 字幕组白名单（逗号分隔，空=全部；子串匹配组名）
    title_filter: str = Field(default="")      # 标题关键词过滤（逗号分隔，空=不限；标题需含其一，如 繁日/简日）
    enabled: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.now)


class Anime(SQLModel, table=True):
    """一部 TV 番剧（唯一）。不同组的不同写法都经 AnimeAlias 指到这一条。剧场版/OVA 不在此，见 Movie。"""
    id: int | None = Field(default=None, primary_key=True)
    bangumi_id: int | None = Field(default=None, index=True)   # 身份键（可空）
    # ---- 名称 / 归档 ----
    title: str = Field(default="")                    # 内部标签（首次见到的解析名）；身份不靠它
    display_name: str | None = Field(default=None)    # 中文规范名（UI 显示）
    jp_name: str | None = Field(default=None)         # 日文原名（建下载文件夹用）
    season: int = Field(default=1)
    quarter: str = Field(default="")                  # 如 26C，决定下载文件夹
    # ---- bgm 元数据 ----
    air_date: str | None = Field(default=None)        # 放送开始日 YYYY-MM-DD
    air_weekday: int | None = Field(default=None)     # 放送星期 0=周一 … 6=周日
    total_episodes: int | None = Field(default=None)  # 总集数
    platform: str | None = Field(default=None)        # 类型：TV / WEB …（剧场版/OVA 归 Movie）
    cover_url: str | None = Field(default=None)       # 封面图 URL
    rating: float | None = Field(default=None)        # bgm 评分（0-10）
    summary: str | None = Field(default=None)         # 简介
    # ---- 制作信息（bgm infobox + 主角声优；纯文本展示，想看全部点 bgm 链接）----
    author: str | None = Field(default=None)          # 原作
    director: str | None = Field(default=None)        # 导演
    music: str | None = Field(default=None)           # 音乐
    cast: str | None = Field(default=None)            # 主角声优：'角色：声优 / …'
    # ---- 下载控制 ----
    confirmed: bool = Field(default=True)             # 确认状态（待确认源默认 False，等人工确认）；确认即自动下
    rejected: bool = Field(default=False)             # 人工拒绝（移出主列表 + 停下载，可在『拒绝』页恢复）
    # 注：『开始使用日超期忽略』复用此二字段编码为 (rejected=True, confirmed=False)——人工拒绝必是 confirmed=True，
    # 故该组合唯一表示"超期忽略"，随开始日可逆、不与人工拒绝混淆，无需额外字段。见 core.anime.apply_start_date_filter
    pref_source: str | None = Field(default=None)     # 锁定下载源（精确匹配 torrent.source：锁哪个组只下哪个；联合发布如"喵萌&LoliHouse"视作独立源、要单独锁，入库照收）；空=按优先级多源兜底
    pref_keyword: str | None = Field(default=None)    # 版本关键词（大小写不敏感子串命中 raw_title，如 繁日/简日/1080p）：与锁定源叠加、只下命中的版本；空=不限
    enrich_tries: int = Field(default=0)              # bgm 未识别番的后台重试次数（满 REENRICH_MAX_TRIES 停自动重试，留手动；手动重识别清零）
    # 跨源集号偏移：某些源用【全系列绝对集号】（第二季第 4 集写成 16），另一些用【季内集号】（写 04）。
    # 集去重键是 (anime_id, episode)，两种写法会被当成两集、各下一份到同一目录。
    # 值＝绝对号 - 季内号，从标题里的双编号写法 '16(88)' 自动推出（见 sources.parse.extract_episode_abs）；
    # 推不出就留 None，此时详情页会标出『疑似同集不同编号』交给人工，绝不瞎猜。
    ep_offset: int | None = Field(default=None)
    last_enrich_at: datetime | None = Field(default=None)  # 上次后台重试 bgm 的时刻（指数退避调度：下次到点=此刻+BASE*2^tries，封顶 MAX）
    # ---- 完结 / 断更（见 core.anime.sweep_finished / sweep_idle）----
    # finished_at：判定"1..total_episodes 全部到手"的时刻。**它本身只是个标记**——
    # 是否据此停止自动下新集由 config.ANIME_FINISH_UNSUB 决定（默认关，只提示不停订）。
    # 【为什么不复用 confirmed/rejected】上面那两个字段已经编码了三种语义（含"超期忽略"的
    # (rejected=True, confirmed=False) 组合），塞第四种会与 apply_start_date_filter 的可逆重算打架；
    # 而且 rejected 会把番挪进『已忽略』——完结的番仍然是订阅中的番，只是不再有新集。
    # 【为什么不做纯派生】派生算得出"现在完不完整"，算不出"什么时候完的"（UI 要显示、通知要去重）。
    finished_at: datetime | None = Field(default=None)
    # 用户点过详情页的『继续订阅』：此后本番不再被自动判完结。
    # 【必需，不是可选】完结判据是【状态式】的（集齐了就恒为真），用户手动清掉 finished_at 后
    # 下一轮巡检会立刻再判一次 —— 没有这一位，那个按钮就是"点了没用"。
    finish_optout: bool = Field(default=False)
    # 上次因『长期没有新种子』提醒过的时刻。不落库的话，每个巡检周期都会为同一部番重发一条通知。
    # 【不需要在新种子入库时清它】sweep_idle 的候选闸本身就要求"最近一条种子早于 cutoff"，
    # 收到新种子的番根本进不了候选；这一列只用来在【同一段静默期内】去重。
    idle_notified_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.now)


class AnimeAlias(SQLModel, table=True):
    """番名对照：某组解析出的 (标题, 季) → 番。命中即知是哪部番，无需再查 bgm。"""
    __tablename__ = "anime_alias"   # 表名带 anime 前缀（与剧场版/TV 分表命名一致）
    __table_args__ = (UniqueConstraint("title", "season", name="uq_alias_title_season"),)

    id: int | None = Field(default=None, primary_key=True)
    title: str = Field(index=True)
    season: int = Field(default=1)
    anime_id: int = Field(index=True)                 # → Anime.id
    created_at: datetime = Field(default_factory=datetime.now)


class AnimeTorrent(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("info_hash", name="uq_animetorrent_info_hash"),)

    id: int | None = Field(default=None, primary_key=True)
    info_hash: str = Field()                    # 40位hex，小写；跨源去重键（唯一约束 uq_ 索引已覆盖等值查找，不再另建普通索引）
    anime_id: int = Field(default=0, index=True)  # → Anime.id（主键关联，取代按番名匹配）
    source: str = Field(default="")             # 字幕组/来源
    site: str = Field(default="nyaa")           # 下载站点
    anime_title: str = Field(default="")        # 该种子解析出的原始番名（展示/调试）
    raw_title: str = Field(default="")          # 原始种子完整标题（含语言/画质标签，用于区分同集不同版本）
    season: int = Field(default=1)
    # 集号词表（四种，**别只记三种**）：
    #   >=1  正片（支持 .5 这种插入话）
    #   0    第0話/前导集 —— 它在周更序列上，`auto_downloadable_ep` 的判据就是 `ep >= 0`，
    #        所以它**会被自动下载**。真库里有 2 行（历史遗留的剧场版误入），
    #        而 `- 00` / `第0话` / `[00]` / `EP00` / `S01E00` 六种写法今天都仍解析成 0。
    #   -1   特别篇   -2 未知/疑似批量  —— 这两类不自动下（见 auto_downloadable_ep）
    # 【`>=0` 与 `>=1` 两个阈值是【有意】不同的，别去"对齐"】
    #   · 能不能自动下：`>= 0`（0 是正片序列上的一集）；
    #   · 算不算"已下 N 集"/完结覆盖：`>= 1`（bgm 的 total_episodes 从 1 数起，
    #     把 0 计进去会让分子比分母多）。
    # flush 的 have_eps **不设阈值**（按 HAVE 状态整批取），所以 0 也参与集去重、不会重复下。
    episode: float = Field(default=-2)
    quarter: str = Field(default="")                  # 入库时的解析快照，【不权威】：
    # 决定实际保存目录的是 Anime.quarter（同名但语义不同）；这里只在番还没识别出来时
    # 作为路径回退用。历史上按种子行的 quarter 归拢，曾把同一部番劈成两个季度卡。

    # 应用侧生命周期（全集见 core.engine 状态词表）：pending 待下 / downloading 交付中 / sent 已交付qB
    # / error 失败 / skipped 同集去重落选 / deleted 人工删过 / excluded 人工排除 / stalled 停滞异常
    status: str = Field(default="pending")
    download_url: str = Field(default="")
    save_path: str = Field(default="")          # 交 qB 时的实际保存路径；改季度/重绑后据此移动或提醒旧位置
    release_time: datetime | None = Field(default=None)
    priority: int = Field(default=0)            # 来源组优先级（缓冲窗口到点时按此选下哪一份）
    # 【index=True 是给"最近入库"那几条排序查询用的】(R21) 仪表盘打开后每 30 秒刷一次
    # `ORDER BY created_at DESC LIMIT 50`，无索引时是 SCAN + 临时 B 树排序，
    # 而 sent 是只增不减的终态、行数随挂机线性增长。真库(1675 行)实测 5.50ms → 0.23ms。
    created_at: datetime = Field(default_factory=datetime.now, index=True)
    # ---- qB 实时状态（后台每 QB_SYNC_INTERVAL 秒从 qBittorrent 同步；未接 qB 时留空/0）----
    qb_state: str = Field(default="")           # qB 原始态：downloading/stalledUP/pausedDL/error…（空=qB 未跟踪）
    qb_progress: float = Field(default=0.0)     # 完成度 0..1
    qb_dlspeed: int = Field(default=0)          # 下载速度 B/s
    qb_size: int = Field(default=0)             # 种子总大小 B
    qb_synced_at: datetime | None = Field(default=None)  # 最近一次从 qB 同步的时间
    qb_progress_at: datetime | None = Field(default=None)  # 进度上次推进的时间；长期不推进→标停滞(异常)判定用
    archived_at: datetime | None = Field(default=None)  # 完成归档时间：已从 qB 移除(留文件)、不再跟踪；空=未归档
    # ---- 暂时性失败的自动重试（只给取种失败/关停中断，见 core.engine.RETRY_BACKOFF_MIN）----
    retry_count: int = Field(default=0)     # 已重试次数；成功即清零，用满退避表就落 error 等人工
    retry_at: datetime | None = Field(default=None)   # 早于此刻不重发（None=不在重试队列里）
    fail_reason: str = Field(default="")    # 最近一次失败原因，详情页展示（成功即清空）


class Movie(SQLModel, table=True):
    """一部剧场版/OVA（唯一，身份 = bangumi_id）。来源仅 Mikan 季度剧场版/OVA 桶，识别用 bgm。

    与 TV 番剧（Anime）完全分离：不进周更下载流，只在 /movies 页人工审批后逐版本下。
    """
    id: int | None = Field(default=None, primary_key=True)
    bangumi_id: int | None = Field(default=None, index=True)   # 身份键（可空）
    # Mikan 番组 id。详情页的『刷新版本』按它重拉 /RSS/Bangumi?bangumiId=<id>
    # （core.movies.refresh_movie_torrents）——BD 常在首映后 6~18 个月才出，
    # 而发现是按整年扫的，没有这个按钮就只能整年重扫。
    mikan_id: str | None = Field(default=None, index=True, unique=True)
    mikan_type: str | None = Field(default=None)      # Mikan 桶判定：剧场版 / OVA（列表徽标；『是不是电影』以此为准）
    # ---- 名称 / 归档 ----
    title: str = Field(default="")                    # Mikan/解析名（兜底）
    display_name: str | None = Field(default=None)    # bgm 规范名（UI 显示）
    jp_name: str | None = Field(default=None)         # bgm 日文原名（建下载文件夹用）
    quarter: str = Field(default="")                  # 首播季键（如 26C，bgm 放送日推出）。
    # 【剧场版实际只用其中的年份】归档目录走 MOVIE_QUARTER_FMT（默认 {yyyy}）、统计与分组按年，
    # 页面上也叫『年份』——与番剧侧的"季度"语义不同，别按 TV 的口径读它。
    # 列名不改（改列要迁移、且它确实存着一个季度键），取年份一律用 core.engine.quarter_year()。
    # ---- bgm 元数据 ----
    air_date: str | None = Field(default=None)
    air_weekday: int | None = Field(default=None)
    total_episodes: int | None = Field(default=None)
    duration: str | None = Field(default=None)        # bgm infobox『片长』（剧场版详情展示；番剧无此列）
    platform: str | None = Field(default=None)        # 剧场版 / OVA / OAD / WEB（bgm 类型，仅展示）
    cover_url: str | None = Field(default=None)
    rating: float | None = Field(default=None)
    summary: str | None = Field(default=None)
    # ---- 制作信息（bgm infobox + 主角声优）----
    author: str | None = Field(default=None)          # 原作
    director: str | None = Field(default=None)        # 导演
    music: str | None = Field(default=None)           # 音乐
    cast: str | None = Field(default=None)            # 主角声优：'角色：声优 / …'
    # ---- 忽略 / 识别 ----（剧场版逐版本人工下，无审批/首选源概念）
    rejected: bool = Field(default=False)             # 人工忽略（移出 /movies，可恢复）
    created_at: datetime = Field(default_factory=datetime.now)


class MovieTorrent(SQLModel, table=True):
    """一条剧场版/OVA 种子，按 info_hash 唯一；movie_id 关联到 Movie。剧场版=一部作品，各条即不同版本。"""
    __table_args__ = (UniqueConstraint("info_hash", name="uq_movietorrent_info_hash"),)

    id: int | None = Field(default=None, primary_key=True)
    info_hash: str = Field()                    # 40位hex，小写；跨源去重键（唯一约束 uq_ 索引已覆盖等值查找，不再另建普通索引）
    movie_id: int = Field(default=0, index=True)  # → Movie.id
    source: str = Field(default="")             # 字幕组/来源
    site: str = Field(default="mikan")
    raw_title: str = Field(default="")          # 原始种子完整标题（区分版本）
    # 应用侧生命周期（全集见 core.engine 状态词表）：pending 待下 / downloading 交付中 / sent 已交付qB
    # / error 失败 / skipped 同集去重落选 / deleted 人工删过 / excluded 人工排除 / stalled 停滞异常
    status: str = Field(default="pending")
    download_url: str = Field(default="")
    save_path: str = Field(default="")          # 交 qB 时的实际保存路径；改季度/重绑后据此移动或提醒旧位置
    release_time: datetime | None = Field(default=None)
    priority: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.now, index=True)   # 同 AnimeTorrent，理由见那里
    # ---- qB 实时状态（同 AnimeTorrent）----
    qb_state: str = Field(default="")
    qb_progress: float = Field(default=0.0)
    qb_dlspeed: int = Field(default=0)
    qb_size: int = Field(default=0)
    qb_synced_at: datetime | None = Field(default=None)
    qb_progress_at: datetime | None = Field(default=None)  # 进度上次推进的时间；长期不推进→标停滞(异常)判定用
    archived_at: datetime | None = Field(default=None)  # 完成归档时间：已从 qB 移除(留文件)、不再跟踪；空=未归档
    # ---- 暂时性失败的自动重试（只给取种失败/关停中断，见 core.engine.RETRY_BACKOFF_MIN）----
    retry_count: int = Field(default=0)     # 已重试次数；成功即清零，用满退避表就落 error 等人工
    retry_at: datetime | None = Field(default=None)   # 早于此刻不重发（None=不在重试队列里）
    fail_reason: str = Field(default="")    # 最近一次失败原因，详情页展示（成功即清空）
