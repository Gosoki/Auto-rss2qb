"""Mikan 季度发现（剧场版 / OVA 专用）。

周更番走 RSS（sources/mikan.py、nyaa.py）；剧场版/OVA 不适合 RSS 流，改用 Mikan 的
季度浏览页发现：

  /Home/BangumiCoverFlowByDayOfWeek?year=2026&seasonStr=夏
      按放送星期分块（data-row-0..6 = 周日~周六），末尾额外一块
      data-row-7 = 剧场版（有 OVA 时可能再多一块）。只取这些『非星期』块。

拿到每部的 Mikan 番组 id + 展示名后：
  · /Home/Bangumi/<id>            → bgm.tv/subject/<bgm_id> 精确联动键 + 字幕组
  · /RSS/Bangumi?bangumiId=<id>   → 该番组全部种子（含各版本/字幕组）

季度归属与规范名一律由 bgm 定（识别用 bgm）；Mikan 只负责『发现有哪些剧场版/OVA + 提供种子』。
"""
import html
import logging
import re
from datetime import datetime
from urllib.parse import quote

import feedparser
import httpx

import config
from sources.base import ParsedItem
from sources.parse import candidate_names, extract_quarter, parse_title

log = logging.getLogger("autorss")

_SEASON_CN = {"A": "冬", "B": "春", "C": "夏", "D": "秋"}
# 季度浏览页里『非星期』块的标签关键词 → 视作剧场版/OVA 桶
_MOVIE_LABELS = ("剧场", "劇場", "OVA", "OAD", "OAV", "特别", "スペシャル", "SP")
_DOW_SPLIT_RE = re.compile(r'<div class="sk-bangumi" data-dayofweek="\d+">')
_ROW_LABEL_RE = re.compile(r'id="data-row-\d+"[^>]*>\s*(.*?)\s*</div>', re.S)
_BANGUMI_RE = re.compile(r'/Home/Bangumi/(\d+)"[^>]*?title="([^"]*)"')
_BGM_RE = re.compile(r'bgm\.tv/subject/(\d+)')
_SUBGROUP_SPLIT_RE = re.compile(r'<div class="subgroup-text"\s+id="\d+">')
_GROUP_RE = re.compile(r'/Home/PublishGroup/(\d+)"[^>]*>([^<]+)')
_HASH_FROM_LINK_RE = re.compile(r'/Home/Episode/([0-9a-f]{40})')


def _client_kwargs(timeout: int = 30) -> dict:
    kwargs = {"timeout": timeout, "follow_redirects": True}
    if config.PROXY:
        kwargs["proxy"] = config.PROXY
    return kwargs


def season_cn(quarter_letter: str) -> str:
    """季度字母 A/B/C/D → Mikan 季名 冬/春/夏/秋。"""
    return _SEASON_CN.get(quarter_letter, "")


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


async def fetch_detail(client, mikan_id: str) -> tuple[int | None, list[tuple[int, str]]]:
    """Mikan 番组详情页 → (bgm_id, [(字幕组id, 字幕组名)])。取不到 bgm_id 返回 (None, groups)。"""
    htm = await _get_text(client, f"{config.MIKAN_BASE}/Home/Bangumi/{mikan_id}")
    bm = _BGM_RE.search(htm)
    bgm_id = int(bm.group(1)) if bm else None
    groups, seen = [], set()
    # 只认真实种子区块 <div class="subgroup-text" id="..."> 内的组，避免侧栏/兜底标签污染
    for seg in _SUBGROUP_SPLIT_RE.split(htm)[1:]:
        gm = _GROUP_RE.search(seg)
        if not gm:
            continue
        gid = int(gm.group(1))
        if gid not in seen:
            seen.add(gid)
            groups.append((gid, html.unescape(gm.group(2)).strip()))
    return bgm_id, groups


def _hash_from_link(link: str) -> str:
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


async def fetch_bangumi_torrents(client, mikan_id: str, priority: int = 0,
                                 subgroups: list | None = None) -> list[ParsedItem]:
    """某 Mikan 番组的全部种子（各版本/字幕组）→ ParsedItem 列表。

    剧场版/OVA 常无规范集号，episode 允许 -1/-2；不做批量过滤（剧场版本身可能是单条）。
    """
    url = f"{config.MIKAN_BASE}/RSS/Bangumi?bangumiId={mikan_id}"
    resp = await client.get(url)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    subs = subgroups or []
    items: list[ParsedItem] = []
    for entry in feed.entries:
        try:
            raw_title = entry.title
            info_hash = _hash_from_link(entry.get("link", ""))
            if not re.fullmatch(r"[0-9a-f]{40}", info_hash):
                continue
            group, anime_title, season, episode = parse_title(raw_title)
            if subs and not any(g in group for g in subs):
                continue
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
                source_kind="movie",
                priority=priority,
                search_names=candidate_names(raw_title),
            ))
        except Exception as e:
            log.error("Mikan 剧场版种子解析失败: %s - %s", e, entry.get("title", "?"))
    return items


def make_client() -> httpx.AsyncClient:
    """给编排层用的共享 client（一次发现批量复用连接 + 代理设置）。"""
    return httpx.AsyncClient(**_client_kwargs())
