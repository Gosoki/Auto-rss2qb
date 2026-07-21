"""通用 nyaa 源：一个字幕组一个实例。

feed 可以是 nyaa 用户名（自动拼 RSS）或一条完整 RSS URL（应对按关键词搜的 feed）。
每条种子打上所属组的策略(policy)+优先级(priority)，交给主流程决定下不下、下哪份。
"""
import logging
from datetime import datetime

import feedparser
import httpx

import config
from sources.base import ParsedItem, Source
from sources.parse import candidate_names, estimate_premiere, extract_quarter, is_batch, parse_title

log = logging.getLogger("autorss")


def nyaa_feed_url(feed: str) -> str:
    """用户名 → 拼 RSS；已是 http(s) URL → 原样用。"""
    feed = (feed or "").strip()
    if feed.startswith(("http://", "https://")):
        return feed
    return f"https://nyaa.si/?page=rss&u={feed}&c=1_2"


class NyaaSource(Source):
    site = "nyaa"

    def __init__(self, name: str, rss_url: str, policy: str = "auto", priority: int = 0,
                 subgroups: list | None = None, title_filter: list | None = None):
        self.name = name
        self.rss_url = rss_url
        self.policy = policy
        self.priority = priority
        self.subgroups = subgroups or []      # 字幕组白名单（子串匹配组名，空=全部）
        self.title_filter = title_filter or []  # 标题关键词过滤（标题需含其一，空=不限）

    async def fetch(self) -> list[ParsedItem]:
        async with httpx.AsyncClient(**config.http_client_kwargs(30)) as client:
            resp = await client.get(self.rss_url)
            resp.raise_for_status()
            content = resp.content

        feed = feedparser.parse(content)
        if feed.bozo:
            log.warning("%s Feed 解析异常（bozo），尽力处理已解析条目", self.name)

        items = []
        for entry in feed.entries:
            item = self._parse(entry)
            if item is not None:
                items.append(item)
        return items

    def _parse(self, entry) -> ParsedItem | None:
        try:
            raw_title = entry.title
            info_hash = (entry.get("nyaa_infohash") or "").strip().lower()
            if not info_hash:
                return None  # 没有 hash 无法跨源去重，跳过
            if is_batch(raw_title):
                return None  # 合集/BDRip/连续集范围 整理帖
            if self.title_filter and not any(k in raw_title for k in self.title_filter):
                return None  # 标题不含所需关键词（如按语言 繁日/简日 过滤）

            group, anime_title, season, episode = parse_title(raw_title)
            if self.subgroups and not any(g in group for g in self.subgroups):
                return None  # 不在白名单的字幕组
            if episode == -2:
                log.warning("集数解析失败 - %s", raw_title)

            release_time = None
            published = entry.get("published")
            if published:
                try:
                    release_time = datetime.strptime(
                        published, "%a, %d %b %Y %H:%M:%S %z"
                    ).replace(tzinfo=None)
                except ValueError:
                    pass

            quarter = ""
            if release_time is not None:
                quarter = extract_quarter(estimate_premiere(release_time, episode, season))

            return ParsedItem(
                info_hash=info_hash,
                raw_title=raw_title,
                anime_title=anime_title,
                season=season,
                episode=episode,
                quarter=quarter,
                release_time=release_time,
                download_url=entry.link,   # nyaa 的 link 就是 .torrent 下载地址
                source=(group or self.name),
                site="nyaa",
                source_kind=self.policy,
                priority=self.priority,
                search_names=candidate_names(raw_title),
            )
        except Exception as e:
            log.error("解析条目失败: %s - %s", e, entry.get("title", "?"))
            return None
