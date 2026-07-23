"""Mikan（蜜柑计划）整合：① 全站 RSS 发现源（周更番，MikanSource）；② 季度剧场版/OVA 发现（catalog）。

① RSS 源：抓 Mikan Classic 全站 feed，产出标准条目交主流程（默认『待人工确认』）。噪声大，可用
   MIKAN_SUBGROUPS 白名单收窄。info_hash 从剧集页链接（/Home/Episode/<hash>）取，与 nyaa 精确对齐去重。

② 季度剧场版/OVA：周更番走 RSS，剧场版/OVA 不适合，改用 Mikan 季度浏览页发现：
   /Home/BangumiCoverFlowByDayOfWeek?year=..&seasonStr=..  按放送星期分块 + 末尾『剧场版/OVA』桶。
   拿到番组 id → 详情页取 bgm.tv/subject/<id> + 字幕组 → /RSS/Bangumi?bangumiId=<id> 取全部种子。
   季度/规范名一律由 bgm 定（识别用 bgm）；Mikan 只负责『发现有哪些』+『提供种子』。
"""
import html
import logging
import re
from datetime import datetime
from urllib.parse import quote

import feedparser
import httpx

import config
from sources.base import ParsedItem, Source
from sources.parse import (SEASON_CN, candidate_names, estimate_premiere, extract_quarter,
                           is_batch, parse_multibracket, parse_title)

log = logging.getLogger("autorss")

# 季度浏览页里『非星期』块的标签关键词 → 视作剧场版/OVA 桶
_MOVIE_LABELS = ("剧场", "劇場", "OVA", "OAD", "OAV", "特别", "スペシャル", "SP")
_DOW_SPLIT_RE = re.compile(r'<div class="sk-bangumi" data-dayofweek="\d+">')
_ROW_LABEL_RE = re.compile(r'id="data-row-\d+"[^>]*>\s*(.*?)\s*</div>', re.S)
_BANGUMI_RE = re.compile(r'/Home/Bangumi/(\d+)"[^>]*?title="([^"]*)"')
_BGM_RE = re.compile(r'bgm\.tv/subject/(\d+)')
_HASH_FROM_LINK_RE = re.compile(r'/Home/Episode/([0-9a-f]{40})')


def _hash_from_link(link: str) -> str:
    """从剧集页链接取 info_hash：优先 /Home/Episode/<40hex>，退回取末段。"""
    m = _HASH_FROM_LINK_RE.search(link or "")
    if m:
        return m.group(1)
    return (link or "").rstrip("/").rsplit("/", 1)[-1].strip().lower()


def _enclosure(entry) -> str:
    for enc in entry.get("enclosures", []) or []:
        if enc.get("href"):
            return enc["href"]
    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure" and link.get("href"):
            return link["href"]
    return ""


# ---------------- ① 全站 RSS 发现源 ----------------

class MikanSource(Source):
    site = "mikan"

    def __init__(self, name: str = "Mikan", rss_url: str = "",
                 policy: str = "review", priority: int = 0, subgroups: list | None = None,
                 title_filter: list | None = None):
        self.name = name
        self.rss_url = rss_url or config.MIKAN_RSS_URL
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

            if is_batch(raw_title):
                return None  # 批量/合集帖
            if self.title_filter and not any(k in raw_title for k in self.title_filter):
                return None  # 标题不含所需关键词（如按语言 繁日/简日 过滤）

            group, anime_title, season, episode = parse_title(raw_title)
            search_names = candidate_names(raw_title)
            if not anime_title and config.ANIME_MULTIBRACKET_PARSE:
                mb = parse_multibracket(raw_title)   # 开关开：全括号命名回退捕获番名
                if mb:
                    anime_title, search_names = mb
            # 白名单：子串匹配，兼顾联合发布（如 "喵萌奶茶屋&LoliHouse"）
            if self.subgroups and not any(g in group for g in self.subgroups):
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
                source=group or self.name,
                site="mikan",
                source_kind=self.policy,
                priority=self.priority,
                search_names=search_names,
            )
        except Exception as e:
            log.error("Mikan 解析失败: %s - %s", e, entry.get("title", "?"))
            return None


# ---------------- ② 季度剧场版/OVA 发现（catalog） ----------------

def make_client() -> httpx.AsyncClient:
    """给编排层用的共享 client（一次发现批量复用连接 + 代理设置）。"""
    return httpx.AsyncClient(**config.http_client_kwargs())


def mikan_search_url(query: str) -> str:
    """Mikan 搜索 RSS：/RSS/Search?searchstr=<关键词>。补齐(backfill)用；返回 /Home/Episode/<hash>
    格式，MikanSource._parse / _hash_from_link 直接吃。"""
    return f"{config.MIKAN_BASE}/RSS/Search?searchstr={quote(query)}"


def season_cn(quarter_letter: str) -> str:
    """季度字母 A/B/C/D → Mikan 季名 冬/春/夏/秋。"""
    return SEASON_CN.get(quarter_letter, "")


async def _get_text(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.text


def _parse_movie_bucket(htm: str) -> list[tuple[str, str, str]]:
    """从季度浏览页 HTML 抽剧场版/OVA 块：返回 [(mikan_id, 展示名, 桶标签)]。

    只认标签命中 _MOVIE_LABELS 的块（剧场版/OVA），跳过 7 个星期块。
    """
    out, seen = [], set()
    for blk in _DOW_SPLIT_RE.split(htm)[1:]:
        lm = _ROW_LABEL_RE.search(blk)
        label = html.unescape(lm.group(1)).strip() if lm else ""
        if not any(k in label for k in _MOVIE_LABELS):
            continue
        for m in _BANGUMI_RE.finditer(blk):
            mid = m.group(1)
            if mid not in seen:
                seen.add(mid)
                out.append((mid, html.unescape(m.group(2)).strip(), label))
    return out


async def discover_movie_bucket(client, year: int, season_letter: str) -> list[tuple[str, str, str]]:
    """某季度（year + A/B/C/D）Mikan 剧场版/OVA 桶：[(mikan_id, 展示名, 桶标签)]。"""
    scn = season_cn(season_letter)
    if not scn:
        return []
    url = (f"{config.MIKAN_BASE}/Home/BangumiCoverFlowByDayOfWeek"
           f"?year={year}&seasonStr={quote(scn)}")
    htm = await _get_text(client, url)
    return _parse_movie_bucket(htm)


async def fetch_detail(client, mikan_id: str) -> int | None:
    """Mikan 番组详情页 → bgm_id（取不到返回 None）。剧场版只需 bgm 精确联动键，不接字幕组白名单。"""
    htm = await _get_text(client, f"{config.MIKAN_BASE}/Home/Bangumi/{mikan_id}")
    bm = _BGM_RE.search(htm)
    return int(bm.group(1)) if bm else None


async def fetch_bangumi_torrents(client, mikan_id: str) -> list[ParsedItem]:
    """某 Mikan 番组的全部种子（各版本/字幕组）→ ParsedItem 列表。

    剧场版/OVA 常无规范集号，episode 允许 -1/-2；不做批量/字幕组过滤（剧场版逐版本人工挑着下）。
    """
    url = f"{config.MIKAN_BASE}/RSS/Bangumi?bangumiId={mikan_id}"
    resp = await client.get(url)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items: list[ParsedItem] = []
    for entry in feed.entries:
        try:
            raw_title = entry.title
            info_hash = _hash_from_link(entry.get("link", ""))
            if not re.fullmatch(r"[0-9a-f]{40}", info_hash):
                continue
            group, anime_title, season, episode = parse_title(raw_title)
            download_url = _enclosure(entry)
            if not download_url:
                continue
            release_time = None
            pp = entry.get("published_parsed")
            if pp:
                release_time = datetime(*pp[:6])
            quarter = extract_quarter(release_time) if release_time else ""
            items.append(ParsedItem(
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
                priority=0,          # 剧场版逐版本人工挑，不参与优先级选择（source_kind 不落 MovieTorrent，故不设）
                search_names=candidate_names(raw_title),
            ))
        except Exception as e:
            log.error("Mikan 剧场版种子解析失败: %s - %s", e, entry.get("title", "?"))
    return items
