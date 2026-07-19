"""Mikan（蜜柑计划）全站发现源（P2）。

用途：发现 ANi 收不到的番。抓 Mikan 的 Classic 全站 feed，产出 source_kind='other'
的标准条目——主流程会把它们登记为『待人工确认』，默认不下载。

Mikan Classic 是所有字幕组混合，噪声大；可用 MIKAN_SUBGROUPS 白名单收窄。
info_hash 从剧集页链接（/Home/Episode/<hash>）直接取，与 nyaa 精确对齐去重。
"""
import logging
import re
from datetime import datetime

import feedparser
import httpx

from config import MIKAN_RSS_URL, MIKAN_SUBGROUPS, PROXY
from sources.base import ParsedItem, Source
from sources.parse import candidate_names, estimate_premiere, extract_quarter, parse_title

log = logging.getLogger("autorss")


def _hash_from_link(link: str) -> str:
    # https://mikanani.me/Home/Episode/<40hex>
    return link.rstrip("/").rsplit("/", 1)[-1].strip().lower()


def _enclosure(entry) -> str:
    for enc in entry.get("enclosures", []) or []:
        if enc.get("href"):
            return enc["href"]
    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure" and link.get("href"):
            return link["href"]
    return ""


def _is_batch(title: str) -> bool:
    """整理/搬运/合集这类批量帖（标题超长或含这些词）——直接丢。"""
    return len(title) > 90 or any(m in title for m in ("整理", "搬运", "合集"))


class MikanSource(Source):
    name = "Mikan"
    source_kind = "other"
    RSS_URL = MIKAN_RSS_URL

    async def fetch(self) -> list[ParsedItem]:
        kwargs = {"timeout": 30, "follow_redirects": True}
        if PROXY:
            kwargs["proxy"] = PROXY
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.get(self.RSS_URL)
            resp.raise_for_status()
            content = resp.content

        feed = feedparser.parse(content)
        items = []
        for entry in feed.entries:
            item = self._parse(entry)
            if item is not None:
                items.append(item)
        return items

    def _parse(self, entry) -> ParsedItem | None:
        try:
            raw_title = entry.title
            info_hash = _hash_from_link(entry.get("link", ""))
            if not re.fullmatch(r"[0-9a-f]{40}", info_hash):
                return None  # 必须是 40 位 hex，才能与 nyaa 的 hash 精确对齐去重

            if _is_batch(raw_title):
                return None  # 批量/合集帖

            group, anime_title, season, episode = parse_title(raw_title)
            # 白名单：子串匹配，兼顾联合发布（如 "喵萌奶茶屋&LoliHouse"）
            if MIKAN_SUBGROUPS and not any(g in group for g in MIKAN_SUBGROUPS):
                return None
            if not anime_title:
                return None
            download_url = _enclosure(entry)
            if not download_url:
                return None

            release_time = None
            pp = entry.get("published_parsed")
            if pp:
                release_time = datetime(*pp[:6])
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
                download_url=download_url,
                source=group or "Mikan",
                site="mikan",
                source_kind="other",
                search_names=candidate_names(raw_title),
            )
        except Exception as e:
            log.error("Mikan 解析失败: %s - %s", e, entry.get("title", "?"))
            return None
