"""共享的标题/季度解析（ANi、Mikan 等 nyaa 系标题都用）。

Mikan 全站字幕组命名五花八门：半角 [组] / 全角 【组】、集数写法有 ' - 07' /
S02E07 / [07] / 第07話 等，都尽量识别，识别不到才退回 -2（未知）。
"""
import re
from datetime import datetime, timedelta

try:
    import opencc
    _converter = opencc.OpenCC("t2s")
    def t2s(text: str) -> str:
        return _converter.convert(text)
except Exception:  # opencc 没装也能跑
    def t2s(text: str) -> str:
        return text

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_SEASON_CN_RE = re.compile(r"第([一二三四五六七八九十]+)季")
_SEASON_EN_RE = re.compile(r"[Ss](\d{1,2})[Ee]\d")       # S02E07 → 第2季
_GROUP_RE = re.compile(r"^[\[【]([^\]】]+)[\]】]")          # [组] 或 【组】
ONE_COUR = 12

# 集数识别（按优先级）：'- 07'/'- 11.5' → S02E07 → 第07話/集 → [07]/[07v2]
_EP_PATTERNS = [
    re.compile(r"-\s*(\d+(?:\.\d+)?)\s*(?:$|[\[【(（])"),
    re.compile(r"[Ss]\d{1,2}[Ee](\d{1,3})"),
    re.compile(r"第\s*(\d+(?:\.\d+)?)\s*[话話集]"),
    re.compile(r"[\[【](\d+(?:\.\d+)?)(?:[vV]\d+)?[\]】]"),
]
# 从番名里剥掉的集数/季段
_STRIP_PATTERNS = [r"\s-\s*\d", r"[Ss]\d{1,2}[Ee]\d{1,3}", r"第\s*\d+\s*[话話集]"]


def extract_season(text: str) -> int:
    m = _SEASON_CN_RE.search(text)
    if m:
        return _CN_NUM.get(m.group(1), 1)
    m = _SEASON_EN_RE.search(text)
    return int(m.group(1)) if m else 1


def strip_season(title: str) -> str:
    return _SEASON_CN_RE.sub("", title)


def extract_episode(text: str):
    """整数集→int，小数集(11.5)→float，特别篇/OVA→-1，无法识别→-2。"""
    for pat in _EP_PATTERNS:
        m = pat.search(text)
        if m:
            v = m.group(1)
            return int(v) if "." not in v else float(v)
    return -1 if ("特别篇" in text or "OVA" in text.upper()) else -2


def _clean_name(name_part: str) -> str:
    """去掉 [..]/【..】 标签块与集数段，得到干净番名（无空格）。"""
    s = re.sub(r"[\[【][^\]】]*[\]】]", "", name_part)
    for pat in _STRIP_PATTERNS:
        m = re.search(pat, s)
        if m:
            s = s[:m.start()]
            break
    return s.replace(" ", "").strip()


def estimate_premiere(release_time: datetime, episode, season: int) -> datetime:
    """用集数倒推首播日（只对第一季、且一个 cour 内可靠，否则用当集时间）。"""
    if season == 1 and 1 <= episode <= ONE_COUR:
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


def parse_title(raw_title: str):
    """从各家字幕组标题提取 (组名, 番名, 季, 集)。

    番名取自 '/' 之后（有则）或组名括号之后，剥掉标签块与集数段；繁转简 + 去季名。
    """
    m = _GROUP_RE.match(raw_title)
    group = m.group(1).strip() if m else ""

    if "/" in raw_title:
        name_part = raw_title.split("/", 1)[1]
    elif m:
        name_part = raw_title[m.end():]   # 组名括号之后（避免误用结尾 tag 的 ]）
    else:
        name_part = raw_title

    season = extract_season(raw_title)
    episode = extract_episode(name_part)
    anime_title = strip_season(t2s(_clean_name(name_part)))
    return group, anime_title, season, episode


def _clean_for_search(s: str) -> str:
    """搜 bgm 用的关键词：去标签块与集数段，但**保留内部空格和季标记**
    （罗马音要空格才搜得准；"第二季"/S02 有助于命中正确的季条目）。"""
    s = re.sub(r"[\[【][^\]】]*[\]】]", "", s)   # 去 [..]/【..】 标签块
    m = re.search(r"\s-\s*\d", s)                # 去 " - 07" 及其后
    if m:
        s = s[:m.start()]
    return s.strip()


def candidate_names(raw_title: str) -> list[str]:
    """从标题提取所有可用于搜 bgm 的候选名（日文原名/罗马音/中文，含繁→简）。

    有日文汉字/假名就一并带上（最准）；ANi 一般是 罗马音 + 繁体中文。
    """
    m = _GROUP_RE.match(raw_title)
    body = raw_title[m.end():] if m else raw_title
    if "/" in body:
        parts = [body.split("/", 1)[0], body.split("/", 1)[1]]
    else:
        parts = [body]

    names: list[str] = []
    for p in parts:
        cleaned = _clean_for_search(p)
        if cleaned:
            names.append(cleaned)
            simp = t2s(cleaned)                  # bgm 的 name_cn 是简体
            if simp != cleaned:
                names.append(simp)

    out: list[str] = []
    for n in names:
        if len(n.replace(" ", "")) >= 2 and n not in out:
            out.append(n)
    return out
