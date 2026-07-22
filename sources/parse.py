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
_SEASON_CN_RE = re.compile(r"第\s*([一二三四五六七八九十]+|\d+)\s*[季期]")  # 第三季/第3期
_SEASON_WORD_RE = re.compile(                            # 3rd Season / Season 3（ANi 罗马音常见）
    r"(\d+)(?:st|nd|rd|th)\s+season|season\s*(\d+)", re.I)
_SEASON_EN_RE = re.compile(r"[Ss](\d{1,2})[Ee]\d")       # S02E07 → 第2季
_GROUP_RE = re.compile(r"^[\[【]([^\]】]+)[\]】]")          # [组] 或 【组】
_SLASH_RE = re.compile(r"\s+/\s+")                        # 语言分隔『罗马音 / 中文』，不吃番名内部的裸 /
ONE_COUR = 12

# 批量/合集/蓝光整理帖 或 连续集范围(01-12)——不是周更单集。
# · BDMV/BD Remux/Vol.N/第N巻/TV+SP：蓝光盘/卷/整季合集，非周更单集（不含歧义的『第N季/クール』，那些有周更单集）。
# · EP01-28 与裸 01-12 范围：连续集合集；两侧都要 ≥2 位（真·合集用零填充），才不把『第二季 - 03』的 "2 - 03"
#   误判成范围而丢单集；裸范围两侧还须被非字母数字包围，才不把 "x264-10bit" 的 "264-10" 误判成范围。
_BATCH_RE = re.compile(
    r"合集|整理|搬运|BD-?RIP|BDMV|BD\s?Remux|\bBatch\b|Vol\.\s*\d+|\bTV\s*\+\s*SP\b|第\s*\d+\s*[巻卷]"
    r"|(?<![A-Za-z])EP\d{1,3}\s*[-~〜]\s*\d{1,3}"
    r"|(?<![A-Za-z0-9])\d{2,3}\s*[-~〜]\s*\d{2,3}(?![A-Za-z0-9])", re.I)

# 集数识别（按优先级）：'- 07'/'- 11.5'/'- 07v2' → S02E07 → 第07話/第二十三话 → [07]/[07v2]
# 第1条用负向后顾避免吃到范围 01-12 的第二个数，并容忍 v2 版本后缀；第3条兼中文数字；第4条限 1~3 位避免命中 [2024]
_EP_PATTERNS = [
    re.compile(r"(?<!\d)-\s*(\d+(?:\.\d+)?)(?:\s*[vV]\d+)?\s*(?:$|[\[【(（])"),
    re.compile(r"[Ss]\d{1,2}[Ee](\d{1,3})"),
    re.compile(r"第\s*([一二三四五六七八九十]+|\d+(?:\.\d+)?)\s*[话話集]"),
    re.compile(r"[\[【](\d{1,3}(?:\.\d+)?)(?:[vV]\d+)?[\]】]"),
]
# 常见视频扩展名后缀（ANi 等种子名带 .mp4/.mkv 结尾，先剥掉再抽集数段，否则 '- 07 .mp4' 的集数段锚不到行尾）
_EXT_RE = re.compile(r"\.(mp4|mkv|avi|ts|flv|rmvb|wmv|mov|m2ts|webm)\s*$", re.I)
# 从番名里剥掉的集数段：锚定到『空格-空格数字(可带 v2)后接括号或行尾』，别吃副标题里的 -2nd
_EP_TAIL = r"\s-\s*\d+(?:\.\d+)?(?:\s*[vV]\d+)?\s*(?:$|[\[【(（])"
_STRIP_PATTERNS = [_EP_TAIL, r"[Ss]\d{1,2}[Ee]\d{1,3}", r"第\s*(?:[一二三四五六七八九十]+|\d+)\s*[话話集]"]


def is_batch(title: str) -> bool:
    """批量/合集/蓝光/连续集范围帖——各源共用，抓到就丢。"""
    return bool(_BATCH_RE.search(title))


def _cn_to_int(s: str) -> int:
    """中文数字→整数，支持 十一=11 / 二十=20 / 二十三=23。识别不了回 1。"""
    if s in _CN_NUM:
        return _CN_NUM[s]
    if "十" in s:
        left, _, right = s.partition("十")
        tens = _CN_NUM.get(left, 1) if left else 1
        ones = _CN_NUM.get(right, 0) if right else 0
        return tens * 10 + ones
    return 1


def _season_num(g: str) -> int:
    return int(g) if g.isdigit() else _cn_to_int(g)


def _find_season(text: str):
    """从『第X季/第X期』或『Nth Season/Season N』抽季号；抽不到回 None。"""
    m = _SEASON_CN_RE.search(text)
    if m:
        return _season_num(m.group(1))
    m = _SEASON_WORD_RE.search(text)
    if m:
        return int(m.group(1) or m.group(2))
    return None


def extract_season(text: str) -> int:
    s = _find_season(text)
    if s is not None:
        return s
    m = _SEASON_EN_RE.search(text)      # 兜底 S02E07
    return int(m.group(1)) if m else 1


def season_from_name(name: str):
    """从 bgm 规范名/日文名反推季号（bgm 权威，名字里带『第X季/Season N』时用）。"""
    return _find_season(name) if name else None


def strip_season(title: str) -> str:
    return _SEASON_CN_RE.sub("", title)


def extract_episode(text: str):
    """整数集→int，小数集(11.5)→float，中文数字(第二十三话)→int，特别篇/OVA→-1，无法识别→-2。"""
    for pat in _EP_PATTERNS:
        m = pat.search(text)
        if m:
            v = m.group(1)
            if v.replace(".", "").isdigit():
                return int(v) if "." not in v else float(v)
            return _cn_to_int(v)   # 中文数字集号（第二十三话）
    return -1 if ("特别篇" in text or "OVA" in text.upper()) else -2


def _clean_name(name_part: str) -> str:
    """去掉 [..]/【..】 标签块、扩展名与集数段，得到干净番名（无空格）。"""
    s = _EXT_RE.sub("", re.sub(r"[\[【][^\]】]*[\]】]", "", name_part))
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


# ABCD ↔ 季节 / 首月（与 extract_quarter 一致：A冬1月 B春4月 C夏7月 D秋10月）
# 季度字母→季名的唯一来源；mikan.season_cn 与 /movies 季度选择器都复用它，避免各处各维护一份。
SEASON_CN = {"A": "冬", "B": "春", "C": "夏", "D": "秋"}
_Q_MONTH = {"A": 1, "B": 4, "C": 7, "D": 10}
_QUARTER_KEY_RE = re.compile(r"(\d{2})([A-D])")


def format_quarter(quarter: str, fmt: str) -> str:
    """把内部季度键(如 '26C')按模板渲染成显示名/文件夹名。

    占位：{yy}=26 {yyyy}=2026 {q}=C {season}=夏 {m}=7。
    解析不出(旧数据/未知/None)或模板写错 → 原样返回，绝不抛异常。
    """
    m = _QUARTER_KEY_RE.fullmatch(quarter or "")
    if not m:
        return quarter or ""
    yy, q = m.group(1), m.group(2)
    ctx = {"yy": yy, "yyyy": f"20{yy}", "q": q,
           "season": SEASON_CN[q], "m": str(_Q_MONTH[q])}
    try:
        return (fmt or "{yy}{q}").format(**ctx)
    except (KeyError, IndexError, ValueError):
        return quarter


def parse_title(raw_title: str):
    """从各家字幕组标题提取 (组名, 番名, 季, 集)。

    番名取自 '/' 之后（有则）或组名括号之后，剥掉标签块与集数段；繁转简 + 去季名。
    """
    m = _GROUP_RE.match(raw_title)
    group = m.group(1).strip() if m else ""

    if _SLASH_RE.search(raw_title):
        name_part = _SLASH_RE.split(raw_title, 1)[1]   # 『罗马音 / 中文』取中文段
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
    s = _EXT_RE.sub("", re.sub(r"[\[【][^\]】]*[\]】]", "", s))   # 去 [..]/【..】 标签块 + 扩展名
    m = re.search(_EP_TAIL, s)                   # 去 " - 07" 及其后（锚定，不吃 -2nd 副标题）
    if m:
        s = s[:m.start()]
    return s.strip()


def candidate_names(raw_title: str) -> list[str]:
    """从标题提取所有可用于搜 bgm 的候选名（日文原名/罗马音/中文，含繁→简）。

    有日文汉字/假名就一并带上（最准）；ANi 一般是 罗马音 + 繁体中文。
    """
    m = _GROUP_RE.match(raw_title)
    body = raw_title[m.end():] if m else raw_title
    if _SLASH_RE.search(body):
        parts = _SLASH_RE.split(body, 1)   # 罗马音段 + 中文段
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
