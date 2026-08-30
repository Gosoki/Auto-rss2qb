"""Mikan（蜜柑计划）整合：① 全站 RSS 发现源（周更番，MikanSource）；② 季度剧场版/OVA 发现（catalog）。

① RSS 源：抓 Mikan Classic 全站 feed，产出标准条目交主流程（默认『待人工确认』）。噪声大，可在
   『订阅源』页给该组填字幕组白名单收窄（SourceGroup.subgroups）。
   info_hash 从剧集页链接（/Home/Episode/<hash>）取，与 nyaa 精确对齐去重。

② 季度剧场版/OVA：周更番走 RSS，剧场版/OVA 不适合，改用 Mikan 季度浏览页发现：
   /Home/BangumiCoverFlowByDayOfWeek?year=..&seasonStr=..  按放送星期分块 + 末尾『剧场版/OVA』桶。
   拿到番组 id → 详情页取 bgm.tv/subject/<id> + 字幕组 → /RSS/Bangumi?bangumiId=<id> 取全部种子。
   季度/规范名一律由 bgm 定（识别用 bgm）；Mikan 只负责『发现有哪些』+『提供种子』。
"""
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import feedparser
import httpx

import config
from services import fetch
from sources.base import _HEX40_RE as _BASE_HEX40, ParsedItem, RssSource
from sources.parse import SEASON_CN, candidate_names, clip_title, parse_title, quarter_of

log = logging.getLogger("autorss")

# 季度浏览页里『非星期』块的标签关键词 → 视作剧场版/OVA 桶
_MOVIE_LABELS = ("剧场", "劇場", "OVA", "OAD", "OAV", "特别", "スペシャル", "SP")
_DOW_SPLIT_RE = re.compile(r'<div class="sk-bangumi" data-dayofweek="\d+">')
_ROW_LABEL_RE = re.compile(r'id="data-row-\d+"[^>]*>\s*(.*?)\s*</div>', re.S)
_BANGUMI_RE = re.compile(r'/Home/Bangumi/(\d+)"[^>]*?title="([^"]*)"')
_BGM_RE = re.compile(r'bgm\.tv/subject/(\d+)')
_HASH_FROM_LINK_RE = re.compile(r'/Home/Episode/([0-9a-f]{40})')
_HEX40_RE = _BASE_HEX40      # 复用 sources/base 里那一份，别各自留一个拷贝   # info_hash 校验（每条种子热路径，预编译）


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

class MikanSource(RssSource):
    """Mikan 的 RSS 源。除了下面这三样，全部逻辑在 RssSource 里（见那里的说明）。"""

    site = "mikan"
    # Mikan 的 pubDate 【不带时区】（实为北京时间 UTC+8），而 feedparser 一律按 GMT 解释。
    # 这条知识以前存在 core/engine.py 的 _SITE_TZ 字典里——那是全项目唯一一处
    # "新增一个源时漏改了不会报错、只是发布时间整天错"的地方，现在收回源自己身上。
    TZ = timezone(timedelta(hours=8))

    def __init__(self, name: str = "Mikan", rss_url: str = "",
                 policy: str = "review", priority: int = 0, subgroups: list | None = None,
                 title_filter: list | None = None):
        # 只为一件事覆写：rss_url 留空时回落到全站 Classic feed（nyaa 没有这个概念）。
        #
        # 【与 nyaa 的不对称是有意的，但要提醒】nyaa 的空 feed 是个事故（拼不出用户名 → 全站
        # firehose），所以那边直接 raise；而 Mikan 的全站 feed 是一个【正当配置】——
        # 本项目默认种入的 Mikan 组用的就是它，配的是 review 策略（人工确认后才下）。
        # 真正危险的组合是"空 feed + auto 策略"：那等于自动下载整个 Mikan。
        # UI 两道闸已经不让存空 feed 了，这里是给直接改库/老数据留的一句提醒。
        if not rss_url and policy == "auto":
            log.warning("源组『%s』没填 feed，将订阅 Mikan 全站，而策略是【自动下载】——"
                        "这几乎肯定不是你想要的，去『订阅源』页填上 feed 或改成人工审核", name)
        super().__init__(name, rss_url or config.MIKAN_RSS_URL, policy, priority,
                         subgroups, title_filter)

    def _hash_of(self, entry) -> str:
        return _hash_from_link(entry.get("link", ""))

    def _url_of(self, entry) -> str:
        return _enclosure(entry)


# ---------------- ② 季度剧场版/OVA 发现（catalog） ----------------

def make_client() -> httpx.AsyncClient:
    """给编排层用的共享 client（一次发现批量复用连接 + 代理设置）。"""
    # 传 MIKAN_BASE：自建 Mikan 镜像常写成局域网 IP，PROXY_SKIP_INTERNAL 靠它认出来
    return httpx.AsyncClient(**config.http_client_kwargs(url=config.MIKAN_BASE))


def mikan_search_url(query: str) -> str:
    """Mikan 搜索 RSS：/RSS/Search?searchstr=<关键词>。补齐(backfill)用；返回 /Home/Episode/<hash>
    格式，MikanSource._parse / _hash_from_link 直接吃。"""
    return f"{config.MIKAN_BASE}/RSS/Search?searchstr={quote(query)}"


def season_cn(quarter_letter: str) -> str:
    """季度字母 A/B/C/D → Mikan 季名 冬/春/夏/秋。"""
    return SEASON_CN.get(quarter_letter, "")


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
    htm = await fetch.get_text(client, url)
    return _parse_movie_bucket(htm)


async def fetch_detail(client, mikan_id: str) -> int | None:
    """Mikan 番组详情页 → bgm_id（取不到返回 None）。剧场版只需 bgm 精确联动键，不接字幕组白名单。"""
    htm = await fetch.get_text(client, f"{config.MIKAN_BASE}/Home/Bangumi/{mikan_id}")
    bm = _BGM_RE.search(htm)
    return int(bm.group(1)) if bm else None


async def fetch_bangumi_torrents(client, mikan_id: str) -> list[ParsedItem]:
    """某 Mikan 番组的全部种子（各版本/字幕组）→ ParsedItem 列表。

    剧场版/OVA 常无规范集号，episode 允许 -1/-2；不做批量/字幕组过滤（剧场版逐版本人工挑着下）。
    """
    url = f"{config.MIKAN_BASE}/RSS/Bangumi?bangumiId={mikan_id}"
    feed = feedparser.parse(await fetch.get_bytes(client, url))   # 带上限+总超时，理由同 RssSource.fetch
    if feed.bozo:
        # 【与 RssSource.fetch 同口径】feed 结构坏掉（站点改版、返回错误页）时表现同样是"0 条"，
        # 没有这行告警就没人知道为什么。这是本文件的【第三条】RSS 解析路径——
        # 番剧那两条已经合并进 RssSource，它因为语义不同（剧场版不过滤、允许 -1/-2 集号）留在这里，
        # 所以那次合并修好的几件事要手动同步过来。
        log.warning("Mikan 番组 %s 的种子 Feed 解析异常（bozo），尽力处理已解析条目", mikan_id)
    items: list[ParsedItem] = []
    for entry in feed.entries:
        try:
            raw_title = clip_title(entry.title)   # 第三方标题，先截长（理由见 parse.clip_title）
            info_hash = (_hash_from_link(entry.get("link", "")) or "").strip().lower()
            if not _HEX40_RE.fullmatch(info_hash):
                continue
            group, anime_title, season, episode = parse_title(raw_title)
            download_url = _enclosure(entry)
            if not download_url:
                continue
            release_time = None
            pp = entry.get("published_parsed")
            if pp:
                release_time = datetime(*pp[:6])
            quarter = quarter_of(release_time) if release_time else ""
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
                priority=0,          # 剧场版逐版本人工挑，不参与优先级选择（policy 不落 MovieTorrent，故不设）
                search_names=candidate_names(raw_title),
            ))
        except Exception as e:
            # 【兜底自己不能再抛】同 RssSource._parse：条目畸形到连 .get 都没有时，
            # 处理器自己炸掉，异常逃出去掀翻整轮扫描——而这个 except 的全部意义就是别让一条坏条目连累其余。
            try:
                what = str(entry.get("title", "?"))[:80]
            except Exception:
                what = repr(entry)[:80]
            log.error("Mikan 剧场版种子解析失败: %s: %s - %s", type(e).__name__, e, what)
    return items
