"""数据模型（SQLModel）。

Anime   —— 番剧管理的单位，按 (标题, 季) 唯一。if_down=是否自动下载，
           confirmed=非ANi来源需人工确认时为 False。
Torrent —— 每一条种子，按 info_hash 唯一（跨源/跨站的精确去重键）。
"""
from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class Anime(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("title", "season", name="uq_anime_title_season"),)

    id: int | None = Field(default=None, primary_key=True)
    title: str = Field(index=True)
    season: int = Field(default=1)
    quarter: str = Field(default="")           # 如 26C，决定下载文件夹
    if_down: bool = Field(default=True)         # 是否自动下载
    confirmed: bool = Field(default=True)       # 非ANi来源默认 False，等人工确认
    source_kind: str = Field(default="ani")     # 'ani' / 'other'
    created_at: datetime = Field(default_factory=datetime.now)

    # ---- Bangumi 富集（P3，可空；富集失败/未开启则保持空） ----
    bangumi_id: int | None = Field(default=None)   # bgm.tv subject id
    display_name: str | None = Field(default=None)  # 规范番名（展示用，不改 title 身份键）
    air_date: str | None = Field(default=None)      # 真实放送开始日 YYYY-MM-DD
    enriched: bool = Field(default=False)           # 是否已尝试富集（尝试过即 True，不论成败，避免每集重刷）
    merged_into: int | None = Field(default=None)   # 跨源重复时指向保留的那部；管理页隐藏、只用于聚合来源


class Torrent(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("info_hash", name="uq_torrent_info_hash"),)

    id: int | None = Field(default=None, primary_key=True)
    info_hash: str = Field(index=True)          # 40位hex，小写；跨源去重键
    source: str = Field(default="ANI")          # 字幕组/来源
    site: str = Field(default="nyaa")           # 下载站点
    anime_title: str = Field(default="")
    season: int = Field(default=1)
    episode: float = Field(default=-2)          # 支持 .5；-1特别篇 -2未知
    quarter: str = Field(default="")
    status: str = Field(default="pending")      # pending/downloaded/error
    download_url: str = Field(default="")
    release_time: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.now)
