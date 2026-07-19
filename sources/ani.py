"""nyaa 上 ANi 字幕组的全量 RSS 源。

标题形如：[ANi] Romaji / 中文名 - 07 [1080P][Baha][WEB-DL][CHT][MP4]
解析出 番名/季/集/季度，并从 <nyaa:infoHash> 取种子 hash 作去重键。
"""
import logging
import re
from datetime import datetime, timedelta

import feedparser
import httpx

from config import ANI_RSS_URL, PROXY
from sources.base import ParsedItem, Source

log = logging.getLogger("autorss")

try:
    import opencc
    _converter = opencc.OpenCC("t2s")
    def _t2s(text: str) -> str:
        return _converter.convert(text)
except Exception:  # opencc 没装也能跑，只是不做繁转简
    def _t2s(text: str) -> str:
        return text

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_SEASON_RE = re.compile(r"第([一二三四五六七八九十]+)季")
_ONE_COUR = 12  # 倒推首播季度只在一个 cour 内可靠（见 estimate_premiere）


def extract_season(text: str) -> int:
    m = _SEASON_RE.search(text)
    return _CN_NUM.get(m.group(1), 1) if m else 1


def strip_season(title: str) -> str:
    return _SEASON_RE.sub("", title)


def extract_episode(raw_title: str):
    """整数集→int，小数集(11.5)→float，特别篇→-1，无法识别→-2。"""
    cleaned = re.sub(r"\[[^\]]*\]", "", raw_title)
    tail = cleaned[cleaned.rfind("-") + 1:].strip()
    try:
        return int(tail)
    except ValueError:
        pass
    try:
        return float(tail)
    except ValueError:
        return -1 if "特别篇" in raw_title else -2


def estimate_premiere(release_time: datetime, episode, season: int) -> datetime:
    """用集数倒推首播日（周更番第 N 集约在首播后 N-1 周）。

    只对第一季、且在一个 cour 内倒推：第二季集数可能连续编号、跨 cour 有空档，
    倒推都不可靠，这时直接用当集时间。
    """
    if season == 1 and 1 <= episode <= _ONE_COUR:
        return release_time - timedelta(weeks=episode - 1)
    return release_time


def extract_quarter(dt: datetime) -> str:
    """按日期归季度：A冬(12/1/2) B春(3/4/5) C夏(6/7/8) D秋(9/10/11)。"""
    year, month = dt.year, dt.month
    if month in (12, 1, 2):
        if month == 12:
            year += 1
        q = "A"
    elif month in (3, 4, 5):
        q = "B"
    elif month in (6, 7, 8):
        q = "C"
    else:
        q = "D"
    return f"{str(year)[2:]}{q}"


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
                return None  # 没有 hash 无法做跨源去重，跳过

            # 番名：'/' 后面是作品名；无 '/' 则取 '[组名]' 之后。集数以被空格包围的
            # ' - ' 分隔，用 rsplit(' - ') 从右切，避免番名内部的连字符（如 Re-Zero）被误截。
            if "/" in raw_title:
                name_part = raw_title.split("/", 1)[1]
            elif "]" in raw_title:
                name_part = raw_title[raw_title.find("]") + 1:]
            else:
                name_part = raw_title
            title = name_part.rsplit(" - ", 1)[0].replace(" ", "")

            anime_title = strip_season(_t2s(title))
            season = extract_season(raw_title)
            episode = extract_episode(raw_title)
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
            )
        except Exception as e:
            log.error("解析条目失败: %s - %s", e, entry.get("title", "?"))
            return None
