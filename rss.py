"""RSS 订阅源。

每个订阅源继承 `RssSource`，实现两个方法即可接入：
    - fetch_items()          抓取并解析，返回 List[RssItem]
    - download_url(torrent_id)  由种子 id 拼出 .torrent 下载地址
然后用 @register 注册。main.py 会自动遍历所有已注册的源。

标题/季数/集数等易碎的解析逻辑集中在本文件顶部的辅助函数里。
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import feedparser
import opencc
import requests

from config import PROXIES
from logger import log

_converter = opencc.OpenCC("t2s")  # 繁体 -> 简体

# 中文数字 -> 季数
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_SEASON_RE = re.compile(r"第([一二三四五六七八九十]+)季")


def extract_season(text):
    """从原始标题中识别季数，识别不到按第 1 季。"""
    m = _SEASON_RE.search(text)
    if not m:
        return 1
    return _CN_NUM.get(m.group(1), 1)


def strip_season(title):
    """去掉标题里的『第X季』字样。"""
    return _SEASON_RE.sub("", title)


def extract_episode(raw_title):
    """解析集数。

    整数集返回 int；带小数的（如 11.5 这类分集）返回 float；
    特别篇返回 -1；无法识别返回 -2。
    """
    cleaned = re.sub(r"\[[^\]]*\]", "", raw_title)  # 去掉所有 [ ... ] 块
    tail = cleaned[cleaned.rfind("-") + 1:].strip()
    try:
        return int(tail)
    except ValueError:
        pass
    try:
        return float(tail)  # 支持 11.5 这类小数集
    except ValueError:
        return -1 if "特别篇" in raw_title else -2


# 倒推只在"一个 cour（约 12 话）之内"可靠。超过这个集数，说明这番已经跨季度播出
# （连续 2cour 或分割 2cour），"从首播连续周更"的假设不再成立，倒推容易落到空档季度，
# 这时直接用当集季度更稳。
_ONE_COUR = 12


def estimate_premiere(release_time, episode, season):
    """用当前集倒推首播日期：周更番第 N 集约在首播后 (N-1) 周。

    这样即使程序在某番已经播到第 11 话才第一次抓到，也能把它归到真正的首播季度，
    而不是被算进当前季度。

    倒推只在满足以下条件时才做，否则退回用当集发布时间：
    - 只对第一季：第二季集数可能接着上一季连续编号（第二季第一集就是第 15 集），不可靠。
    - 只在一个 cour 内（episode<=12）：分割 2cour 的后半（如从第 13 集起）倒推会落到中间空档季度。
    - episode>=1：特别篇/未知（episode<1）无法倒推。
    """
    if season == 1 and 1 <= episode <= _ONE_COUR:
        return release_time - timedelta(weeks=episode - 1)
    return release_time


def extract_quarter(release_time):
    """按发布时间归入番剧季度，如 24A(冬)/24B(春)/24C(夏)/24D(秋)。"""
    year, month = release_time.year, release_time.month
    if month in (12, 1, 2):
        if month == 12:
            year += 1
        quarter = "A"
    elif month in (3, 4, 5):
        quarter = "B"
    elif month in (6, 7, 8):
        quarter = "C"
    else:  # 9, 10, 11
        quarter = "D"
    return f"{str(year)[2:]}{quarter}"


@dataclass
class RssItem:
    """一条标准化后的订阅条目。"""
    torrent_id: str
    anime_title: str
    episode: int
    season: int
    release_time: datetime
    quarter: str
    rss_group: str
    torrent_from: str


class RssSource:
    name = ""          # 展示用名称
    rss_group = ""     # 入库的分组标识
    torrent_from = ""  # 种子来源站点（决定下载地址的拼法）

    def fetch_items(self):
        raise NotImplementedError

    def download_url(self, torrent_id):
        raise NotImplementedError


SOURCES = []
_SOURCE_BY_FROM = {}


def register(source):
    SOURCES.append(source)
    _SOURCE_BY_FROM[source.torrent_from] = source
    return source


def torrent_download_url(torrent_from, torrent_id):
    """给定来源与种子 id，返回下载地址；来源未知返回 None。"""
    source = _SOURCE_BY_FROM.get(torrent_from)
    return source.download_url(torrent_id) if source else None


class NyaaSource(RssSource):
    """nyaa 系订阅源基类。

    适用于标题形如『[组名] 罗马名 / 中文名 - 集数 [标签...]』的上传者
    （ANi、Lilith-Raws 等格式基本一致）。子类只需设置
    `name` / `rss_group` / `RSS_URL` 三个属性即可。
    """
    torrent_from = "nyaa"
    RSS_URL = ""

    def download_url(self, torrent_id):
        return f"https://nyaa.si/download/{torrent_id}.torrent"

    def fetch_items(self):
        log.info(f"尝试获取订阅 - {self.name}")
        try:
            resp = requests.get(self.RSS_URL, proxies=PROXIES, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error(f"获取订阅错误 - {self.name}: {e}")
            return []

        feed = feedparser.parse(resp.content)
        if feed.bozo:
            log.error(f"Feed解析错误或源站问题 - {self.name}")
            return []
        log.info(f"Feed解析成功 - {self.name}")

        items = []
        for entry in feed.entries:
            item = self._parse_entry(entry)
            if item is not None:
                items.append(item)
        return items

    def _parse_entry(self, entry):
        """把一条 RSS entry 解析成 RssItem；解析失败返回 None（跳过该条）。"""
        try:
            raw_title = entry.title

            # 种子 id：取链接最后一段去掉扩展名，如 .../download/123456.torrent -> 123456
            link = entry.link
            torrent_id = link[link.rfind("/") + 1:]
            if "." in torrent_id:
                torrent_id = torrent_id[: torrent_id.find(".")]

            # 番剧名：取标题中作品名部分
            if "/" in raw_title:
                title = raw_title[raw_title.find("/") + 1:]
                title = title[: title.find("-")].replace(" ", "")
            else:
                title = raw_title[: raw_title.find("-") - 1]
                title = title[title.find("]") + 1:].replace(" ", "")

            anime_title = strip_season(_converter.convert(title))
            season = extract_season(raw_title)
            episode = extract_episode(raw_title)
            if episode == -2:
                log.error(f"集数转化错误 - {anime_title} - {raw_title}")

            published = entry.get("published")
            if not published:
                log.warning(f"缺少发布时间，跳过 - {raw_title}")
                return None
            release_time = datetime.strptime(
                published, "%a, %d %b %Y %H:%M:%S %z"
            ).replace(tzinfo=None)

            return RssItem(
                torrent_id=torrent_id,
                anime_title=anime_title,
                episode=episode,
                season=season,
                release_time=release_time,
                quarter=extract_quarter(estimate_premiere(release_time, episode, season)),
                rss_group=self.rss_group,
                torrent_from=self.torrent_from,
            )
        except Exception as e:
            log.error(f"解析条目失败: {e} - {entry.get('title', '未知标题')}")
            return None


class AniSource(NyaaSource):
    name = "ANi"
    rss_group = "ANI"
    RSS_URL = "https://nyaa.si/?page=rss&u=ANiTorrent"


class LilithSource(NyaaSource):
    """示例：第二个 nyaa 上传者。标题格式与 ANi 基本一致，直接复用 NyaaSource 解析。"""
    name = "Lilith-Raws"
    rss_group = "LILITH"
    RSS_URL = "https://nyaa.si/?page=rss&u=Lilith-Raws"


register(AniSource())
# 想启用第二个源时，取消下一行注释（会开始抓取并下载 Lilith-Raws 的番剧）：
# register(LilithSource())
