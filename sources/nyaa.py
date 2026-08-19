"""通用 nyaa 源：一个字幕组一个实例。

feed 可以是 nyaa 用户名（自动拼 RSS）或一条完整 RSS URL（应对按关键词搜的 feed）。
每条种子打上所属组的策略(policy)+优先级(priority)，交给主流程决定下不下、下哪份。
"""
from datetime import timezone
from urllib.parse import quote

from sources.base import RssSource


def nyaa_feed_url(feed: str) -> str:
    """用户名 → 拼 RSS；已是 http(s) URL → 原样用。

    分类用 1_0（全部动漫）而非 1_2（仅英译）——ANi/Lilith-Raws 等中文字幕组的种子归在
    1_3（非英译），若写死 1_2 这些组的 feed 会拉到 0 条（静默拉空）。1_0 覆盖各语言字幕。
    """
    feed = (feed or "").strip()
    if feed.startswith(("http://", "https://")):
        return feed
    if not feed:
        # 【空用户名 = 全站 firehose】拼出来的是不带 u= 的整站 RSS：几十条/分钟、什么都有。
        # 而源组的默认策略是 auto（自动建番、自动确认、自动下载）——一个不小心存进去的空 feed
        # 就能让 qB 开始下载整个 nyaa。宁可这一轮抓不到，也不能让它变成"订阅全站"。
        raise ValueError("nyaa 源的 feed 不能为空（填用户名或完整 RSS URL）")
    return f"https://nyaa.si/?page=rss&u={feed}&c=1_0"


def nyaa_search_url(query: str) -> str:
    """nyaa 搜索 RSS：按关键词搜全站动漫（c=1_0 覆盖各语言字幕）。补齐(backfill)用；
    返回的 RSS 与用户订阅同格式（含 nyaa_infohash），NyaaSource._parse 直接吃。"""
    return f"https://nyaa.si/?page=rss&q={quote(query)}&c=1_0&f=0"


class NyaaSource(RssSource):
    """nyaa 的 RSS 源。除了下面这三样，全部逻辑在 RssSource 里（见那里的说明）。"""

    site = "nyaa"
    TZ = timezone.utc          # nyaa 的 pubDate 带 -0000，feedparser 归一后就是真 UTC

    def _hash_of(self, entry) -> str:
        return entry.get("nyaa_infohash") or ""

    def _url_of(self, entry) -> str:
        return entry.get("link") or ""      # nyaa 的 link 就是 .torrent 下载地址
