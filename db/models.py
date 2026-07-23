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
    pref_source: str | None = Field(default=None)     # 锁定下载源（精确匹配 torrent.source：锁哪个组只下哪个；联合发布如"喵萌&LoliHouse"视作独立源、要单独锁，入库照收）；空=按优先级多源兜底
    pref_keyword: str | None = Field(default=None)    # 版本关键词（大小写不敏感子串命中 raw_title，如 繁日/简日/1080p）：与锁定源叠加、只下命中的版本；空=不限
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
    info_hash: str = Field(index=True)          # 40位hex，小写；跨源去重键
    anime_id: int = Field(default=0, index=True)  # → Anime.id（主键关联，取代按番名匹配）
    source: str = Field(default="")             # 字幕组/来源
    site: str = Field(default="nyaa")           # 下载站点
    anime_title: str = Field(default="")        # 该种子解析出的原始番名（展示/调试）
    raw_title: str = Field(default="")          # 原始种子完整标题（含语言/画质标签，用于区分同集不同版本）
    season: int = Field(default=1)
    episode: float = Field(default=-2)          # 支持 .5；-1特别篇 -2未知
    quarter: str = Field(default="")
    status: str = Field(default="pending")      # 应用侧生命周期：pending/downloading/downloaded/error/skipped
    download_url: str = Field(default="")
    release_time: datetime | None = Field(default=None)
    priority: int = Field(default=0)            # 来源组优先级（缓冲窗口到点时按此选下哪一份）
    created_at: datetime = Field(default_factory=datetime.now)
    # ---- qB 实时状态（后台每 QB_SYNC_INTERVAL 秒从 qBittorrent 同步；未接 qB 时留空/0）----
    qb_state: str = Field(default="")           # qB 原始态：downloading/stalledUP/pausedDL/error…（空=qB 未跟踪）
    qb_progress: float = Field(default=0.0)     # 完成度 0..1
    qb_dlspeed: int = Field(default=0)          # 下载速度 B/s
    qb_size: int = Field(default=0)             # 种子总大小 B
    qb_synced_at: datetime | None = Field(default=None)  # 最近一次从 qB 同步的时间


class Movie(SQLModel, table=True):
    """一部剧场版/OVA（唯一，身份 = bangumi_id）。来源仅 Mikan 季度剧场版/OVA 桶，识别用 bgm。

    与 TV 番剧（Anime）完全分离：不进周更下载流，只在 /movies 页人工审批后逐版本下。
    """
    id: int | None = Field(default=None, primary_key=True)
    bangumi_id: int | None = Field(default=None, index=True)   # 身份键（可空）
    mikan_id: str | None = Field(default=None, index=True)     # Mikan 番组 id（刷新种子 RSS 用）
    mikan_type: str | None = Field(default=None)      # Mikan 桶判定：剧场版 / OVA（列表徽标；『是不是电影』以此为准）
    # ---- 名称 / 归档 ----
    title: str = Field(default="")                    # Mikan/解析名（兜底）
    display_name: str | None = Field(default=None)    # bgm 规范名（UI 显示）
    jp_name: str | None = Field(default=None)         # bgm 日文原名（建下载文件夹用）
    quarter: str = Field(default="")                  # 首播季（bgm 放送日），决定下载文件夹
    # ---- bgm 元数据 ----
    air_date: str | None = Field(default=None)
    air_weekday: int | None = Field(default=None)
    total_episodes: int | None = Field(default=None)
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
    info_hash: str = Field(index=True)          # 40位hex，小写；跨源去重键
    movie_id: int = Field(default=0, index=True)  # → Movie.id
    source: str = Field(default="")             # 字幕组/来源
    site: str = Field(default="mikan")
    raw_title: str = Field(default="")          # 原始种子完整标题（区分版本）
    status: str = Field(default="pending")      # 应用侧生命周期：pending/downloading/downloaded/error/skipped
    download_url: str = Field(default="")
    release_time: datetime | None = Field(default=None)
    priority: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.now)
    # ---- qB 实时状态（同 AnimeTorrent）----
    qb_state: str = Field(default="")
    qb_progress: float = Field(default=0.0)
    qb_dlspeed: int = Field(default=0)
    qb_size: int = Field(default=0)
    qb_synced_at: datetime | None = Field(default=None)
