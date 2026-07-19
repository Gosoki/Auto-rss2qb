"""nyaa 上 ANi 字幕组的全量 RSS 源。

标题形如：[ANi] Romaji / 中文名 - 07 [1080P][Baha][WEB-DL][CHT][MP4]
从 <nyaa:infoHash> 取种子 hash 作去重键；标题/季度解析交给 sources.parse。
"""
import logging
from datetime import datetime

import feedparser
import httpx

from config import ANI_RSS_URL, PROXY
from sources.base import ParsedItem, Source
from sources.parse import candidate_names, estimate_premiere, extract_quarter, parse_title

log = logging.getLogger("autorss")


class AniSource(Source):
    name = "ANI"
    source_kind = "ani"
    RSS_URL = ANI_RSS_URL

    async def fetch(self) -> list[ParsedItem]:
        kwargs = {"timeout": 30, "follow_redirects": True}
        if PROXY:
            kwargs["proxy"] = PROXY
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.get(self.RSS_URL)
            resp.raise_for_status()
            content = resp.content

        feed = feedparser.parse(content)
        if feed.bozo:
            log.warning("ANi Feed 解析异常（bozo），尽力处理已解析条目")

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

            _group, anime_title, season, episode = parse_title(raw_title)
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
                source="ANI",
                site="nyaa",
                source_kind="ani",
                search_names=candidate_names(raw_title),
            )
        except Exception as e:
            log.error("解析条目失败: %s - %s", e, entry.get("title", "?"))
            return None
