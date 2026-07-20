"""数据模型（SQLModel）。

Anime      —— 一部番剧，唯一一条。身份 = bangumi_id（拿不到则各自独立）。
              含 bgm 抓来的元数据 + 下载总开关 if_down。
TitleAlias —— 番名对照：各字幕组解析出的 (标题, 季) → 指向哪部番。命中即知是谁，不必再查 bgm。
Torrent    —— 每条种子，按 info_hash 唯一；用 anime_id 关联到番。
SourceGroup—— 订阅的字幕组/源。
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
    policy: str = Field(default="auto")        # 'auto' 全下 | 'review' 需审核
    priority: int = Field(default=0)           # 越大越优先（多源同一集选高的）
    subgroups: str = Field(default="")         # 字幕组白名单（逗号分隔，空=全部；子串匹配组名）
    title_filter: str = Field(default="")      # 标题关键词过滤（逗号分隔，空=不限；标题需含其一，如 繁日/简日）
    enabled: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.now)


class Anime(SQLModel, table=True):
    """一部番剧（唯一）。不同组的不同写法都经 TitleAlias 指到这一条。"""
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
    platform: str | None = Field(default=None)        # 类型：TV / 剧场版 / OVA / WEB …
    cover_url: str | None = Field(default=None)       # 封面图 URL
    rating: float | None = Field(default=None)        # bgm 评分（0-10）
    summary: str | None = Field(default=None)         # 简介
    # ---- 下载控制 ----
    if_down: bool = Field(default=True)               # (遗留列，已不再读取；下不下 = confirmed 且未 rejected)
    confirmed: bool = Field(default=True)             # 审核状态（审核源默认 False，等人工确认）；确认即自动下
    rejected: bool = Field(default=False)             # 人工拒绝（移出主列表 + 停下载，可在『拒绝』页恢复）
    source_kind: str = Field(default="auto")          # 引入它的策略（'auto'/'review'，徽章用）
    pref_source: str | None = Field(default=None)     # 首选下载源（子串匹配 torrent.source；空=按优先级）
    enriched: bool = Field(default=False)             # 是否已尝试富集
    created_at: datetime = Field(default_factory=datetime.now)


class TitleAlias(SQLModel, table=True):
    """番名对照：某组解析出的 (标题, 季) → 番。命中即知是哪部番，无需再查 bgm。"""
    __table_args__ = (UniqueConstraint("title", "season", name="uq_alias_title_season"),)

    id: int | None = Field(default=None, primary_key=True)
    title: str = Field(index=True)
    season: int = Field(default=1)
    anime_id: int = Field(index=True)                 # → Anime.id
    created_at: datetime = Field(default_factory=datetime.now)


class Torrent(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("info_hash", name="uq_torrent_info_hash"),)

    id: int | None = Field(default=None, primary_key=True)
    info_hash: str = Field(index=True)          # 40位hex，小写；跨源去重键
    anime_id: int = Field(default=0, index=True)  # → Anime.id（主键关联，取代按番名匹配）
    source: str = Field(default="")             # 字幕组/来源
    site: str = Field(default="nyaa")           # 下载站点
    anime_title: str = Field(default="")        # 该种子解析出的原始番名（展示/调试）
    season: int = Field(default=1)
    episode: float = Field(default=-2)          # 支持 .5；-1特别篇 -2未知
    quarter: str = Field(default="")
    status: str = Field(default="pending")      # pending/downloaded/error/skipped/downloading
    download_url: str = Field(default="")
    release_time: datetime | None = Field(default=None)
    priority: int = Field(default=0)            # 来源组优先级（缓冲窗口到点时按此选下哪一份）
    created_at: datetime = Field(default_factory=datetime.now)
