"""订阅源基类与标准条目。

新增一个源只需继承 Source、实现 async fetch()，返回一串 ParsedItem。
主流程只认 ParsedItem，各源的解析差异被隔离在自己的 fetch 里。
"""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ParsedItem:
    info_hash: str          # 40位hex小写，跨源去重键
    raw_title: str
    anime_title: str
    season: int
    episode: float          # 支持 .5；-1特别篇 -2未知
    quarter: str
    release_time: datetime | None
    download_url: str       # 直接可下载 .torrent 的地址
    source: str             # 展示用来源名，如 'ANI'
    site: str               # 下载站点，如 'nyaa'
    source_kind: str = "ani"  # 'ani' 自动下 / 'other' 需人工确认
    search_names: list[str] = field(default_factory=list)  # 候选名（搜 bgm 用）


class Source:
    name = "base"
    source_kind = "ani"

    async def fetch(self) -> list[ParsedItem]:
        raise NotImplementedError
