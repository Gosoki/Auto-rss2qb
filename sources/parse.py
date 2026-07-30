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
# 【量词都带上界】季号不会超过 3 位、中文数字不会超过 4 字。无界的 (\d+)(?:st|nd|rd|th)
# 对长数字串是 O(n²)：实测 8000 位数字要跑 1.37 秒，而标题来自第三方 feed，
# 一条畸形标题就能把常驻采集协程卡住。加上界后是线性，且不损失任何真实标题的识别。
_SEASON_CN_RE = re.compile(r"第\s*([一二三四五六七八九十]{1,4}|\d{1,3})\s*[季期]")  # 第三季/第3期
_SEASON_WORD_RE = re.compile(                            # 3rd Season / Season 3（ANi 罗马音常见）
    r"(\d{1,3})(?:st|nd|rd|th)\s+season|season\s*(\d{1,3})", re.I)
_SEASON_EN_RE = re.compile(r"[Ss](\d{1,2})[Ee]\d")       # S02E07 → 第2季
# 裸季标记 S4 / S2（LoliHouse、喵萌等常写 『番名 S4 / Romaji S4 - 17』）——前后都不许挨字母数字，
# 免得吃到 PS4 / DTS5 / S02E07(由上面那条管) 之类。仅作最后兜底，显式的『第X季/Season N』优先。
_SEASON_BARE_RE = re.compile(r"(?<![A-Za-z0-9])[Ss](\d{1,2})(?![0-9A-Za-z])")
_GROUP_RE = re.compile(r"^[\[【]([^\]】]+)[\]】]")          # [组] 或 【组】
_SLASH_RE = re.compile(r"\s+/\s+")                        # 语言分隔『罗马音 / 中文』，不吃番名内部的裸 /
_HAN_RE = re.compile(r"[一-鿿]")                           # CJK 汉字
_KANA_RE = re.compile(r"[぀-ヿ]")                          # 平/片假名（3040–30FF）＝日文段特征
ONE_COUR = 12

# 批量/合集/蓝光整理帖 或 连续集范围(01-12)——不是周更单集。
# · BDMV/BD Remux/Vol.N/第N巻/TV+SP：蓝光盘/卷/整季合集，非周更单集（不含歧义的『第N季/クール』，那些有周更单集）。
# · EP01-28 与裸 01-12 范围：连续集合集；两侧都要 ≥2 位（真·合集用零填充），才不把『第二季 - 03』的 "2 - 03"
#   误判成范围而丢单集；裸范围两侧还须被【非连字符】的非字母数字包围，才不把 "x264-10bit" 的 "264-10"、
#   或日期 "[2024-05-10]" 的 "05-10" 误判成范围（相邻连字符＝编码残段/日期分段，都不是合集）。
_BATCH_RE = re.compile(
    r"合集|整理|搬运|BD-?RIP|BDMV|BD\s?Remux|\bBatch\b|Vol\.\s*\d+|\bTV\s*\+\s*SP\b|第\s*\d+\s*[巻卷]"
    r"|(?<![A-Za-z])EP\d{1,3}\s*[-~〜]\s*\d{1,3}", re.I)
# 裸集号范围(01-12/01~12)：连续集合集。单独拆出 + 在 is_batch 里守卫：标题能抽出单集集号时(如
# '[组] Show - 24 (最終回 23-24 総集編)' 的 '23-24')多是注解而非合集范围，不当合集静默丢弃整条。
_BARE_RANGE_RE = re.compile(r"(?<![-A-Za-z0-9])\d{2,3}\s*[-~〜]\s*\d{2,3}(?![-A-Za-z0-9])")

# 集数识别（按优先级）：'- 07'/'- 11.5'/'- 07v2' → S02E07 → 第07話/第二十三话 → [07]/[07v2]
# 第1条用负向后顾避免吃到范围 01-12 的第二个数，并容忍 v2 版本后缀；第3条兼中文数字；第4条限 1~3 位避免命中 [2024]
# 完结标记：字幕组常在最终话写 『- 24 END』『- 12 完』『- 13 Fin』，它卡在集号与 [tag] 之间，
# 早先的收尾锚点容不下它 → 整条落 -2（未知集），最终话永远不会被自动下。
_FINALE = r"(?:\s*(?:END|FIN|COMPLETE|完|終|终|エンド))?"
_EP_PATTERNS = [
    re.compile(r"(?<!\d)-\s*(\d{1,4}(?:\.\d+)?)(?:\s*[vV]\d+)?" + _FINALE + r"\s*(?:$|[\[【(（])",
               re.I),
    re.compile(r"[Ss]\d{1,2}[Ee](\d{1,3})"),
    re.compile(r"第\s*([一二三四五六七八九十]+|\d+(?:\.\d+)?)\s*[话話集]"),
    re.compile(r"[\[【](\d{1,3}(?:\.\d+)?)(?:[vV]\d+)?[\]】]"),
]
# 常见视频扩展名后缀（ANi 等种子名带 .mp4/.mkv 结尾，先剥掉再抽集数段，否则 '- 07 .mp4' 的集数段锚不到行尾）
_EXT_RE = re.compile(r"\.(mp4|mkv|avi|ts|flv|rmvb|wmv|mov|m2ts|webm)\s*$", re.I)
# 从番名里剥掉的集数段：锚定到『空格-空格数字(可带 v2)后接括号或行尾』，别吃副标题里的 -2nd
_EP_TAIL = (r"\s-\s*\d{1,4}(?:\.\d+)?(?:\s*[vV]\d+)?" + _FINALE + r"\s*(?:$|[\[【(（])")
_EP_TAIL_RE = re.compile(_EP_TAIL, re.I)        # 预编译（_clean_for_search 用）
# 【必须与 _EP_TAIL_RE 同样带 re.I】它们复用同一个 _EP_TAIL 字符串，而完结标记里有 END/FIN/Complete
# 这些字母：少了 re.I 就会出现"集号认出来了、番名却没洗干净"的半吊子——
# '[ANi] Some Show - 24 Fin' 的集号是 24（_EP_PATTERNS 带 re.I），番名却留成 'SomeShow-24Fin'，
# 而番名是别名键，等于给同一部番造了个新身份。
_STRIP_PATTERNS = [re.compile(p, re.I) for p in (     # 预编译（_clean_name 循环用）
    _EP_TAIL, r"[Ss]\d{1,2}[Ee]\d{1,3}", r"第\s*(?:[一二三四五六七八九十]+|\d+)\s*[话話集]")]
# 标签块 [..]/【..】（含空括号，整体替换）——_clean_name/_clean_for_search/parse_multibracket 共用。
# 勿与 _INNER_BLK(带捕获组、+ 不匹配空括号) 混用：对空括号 [] 的 sub 结果不同。
_TAG_BLK_RE = re.compile(r"[\[【][^\]】]*[\]】]")
# 宣传标记『★07月新番★』（喵萌等）：它后面紧跟的 [ 会因语言分段被切走而失去右括号，
# _TAG_BLK_RE 要求成对故删不掉，残留在番名里污染 别名键 与 bgm 搜索词。
_PROMO_RE = re.compile(r"★[^★]*★")


# 单条种子标题的长度上限。真实标题一般 60~150 字符，300 已是极端；这里给到 512 纯属留余量。
# 为什么必须有这道闸：标题来自第三方 RSS（不可信输入），而本模块多条正则是"先 findall/sub 再逐段
# 处理"的写法，对超长输入是超线性的——一条几百 KB 的畸形标题就能让解析在事件循环里空转很久，
# 而采集是常驻协程，一次卡住就是"好几天没更新了"。截断只影响那条畸形标题的可读性，不影响正常条目。
MAX_TITLE_LEN = 512


def clip_title(raw: str) -> str:
    """把 RSS 给的标题截到 MAX_TITLE_LEN。各 source 的 _parse 一进门就调它（唯一入口）。"""
    raw = raw or ""
    return raw if len(raw) <= MAX_TITLE_LEN else raw[:MAX_TITLE_LEN]


def is_batch(title: str) -> bool:
    """批量/合集/蓝光/连续集范围帖——各源共用，抓到就丢。"""
    if _BATCH_RE.search(title):
        return True
    # 裸集号范围(01-12) 仅在标题【抽不出单集集号】时才判合集：标准合集帖(如 'Show 01-12')抽不到单集→判合集；
    # 而 'Show - 24 (23-24 総集編)' 能抽到第24集→那个 23-24 是注解，不误当合集把单集丢掉。
    if _BARE_RANGE_RE.search(title) and extract_episode(title) < 0:
        return True
    return False


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
    if m:
        return int(m.group(1))
    m = _SEASON_BARE_RE.search(text)    # 再兜底裸 S4（『番名 S4 - 17』）
    return int(m.group(1)) if m else 1


def season_from_name(name: str):
    """从 bgm 规范名/日文名反推季号（bgm 权威，名字里带『第X季/Season N』时用）。"""
    return _find_season(name) if name else None


def strip_season(title: str) -> str:
    return _SEASON_CN_RE.sub("", title)


# 双编号写法：'- 16(88)' —— 括号外是【季内】集号、括号内是【全系列绝对】集号（LoliHouse 系常用）。
# 它是跨源集号归一的现成锚点：offset = 绝对 - 季内，拿到之后就能把别的源（如 ANi 直接写 88）
# 的绝对集号折算回季内集号，避免同一集因两种编号体系被当成两集、各下一份到同一目录。
# 两数都限 1~3 位（真集号不会四位），且绝对号必须【严格大于】季内号——否则 '- 04(2024)' 这种
# 年份括号、'- 12(3)' 这种碟片编号都会被误当双编号，推出一个荒唐的偏移量污染全番的集号折算。
_EP_DUAL_RE = re.compile(r"(?<!\d)-\s*(\d{1,3})(?:\s*[vV]\d+)?\s*\(\s*(\d{1,3})\s*\)(?!\d)")


def extract_episode_abs(text: str) -> int | None:
    """标题带双编号 'NN(MM)' 时返回绝对集号 MM，否则 None。季内集号仍由 extract_episode 给。"""
    m = _EP_DUAL_RE.search(text)
    if not m:
        return None
    rel, absolute = int(m.group(1)), int(m.group(2))
    return absolute if 1 <= rel < absolute <= 999 else None


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
    s = _PROMO_RE.sub("", _EXT_RE.sub("", _TAG_BLK_RE.sub("", name_part)))
    for pat in _STRIP_PATTERNS:
        m = pat.search(s)
        if m:
            s = s[:m.start()]
            break
    # 去掉分段切剩的孤立括号（『★07月新番★[番名』剥掉标记后会留个左括号）
    return s.replace(" ", "").strip().strip("[]【】").strip()


def estimate_premiere(release_time: datetime, episode, season: int) -> datetime:
    """用集数倒推首播日（只对第一季、且一个 cour 内可靠，否则用当集时间）。
    小数集（如 11.5 特别篇）不倒推：它们不在正片周更序列上，倒推会落到错季度、拆散归档目录。"""
    is_special = isinstance(episode, float) and episode != int(episode)
    if season == 1 and not is_special and 1 <= episode <= ONE_COUR:
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
        out = (fmt or "{yy}{q}").format(**ctx)
    except Exception:
        # 模板由用户在设置页手填，str.format 的出错方式远不止 KeyError/IndexError/ValueError：
        # 例如 '{yy.foo}' 抛 AttributeError——漏捕会让【任何渲染季度名的页面】整页打挂。
        # 这里一律回退成原始季度键，宁可显示得朴素也不能崩。
        return quarter
    # 同理 '{yy:>{yyyy}}' 这种嵌套宽度能生成几千字符：它会变成目录名的一段，必须封顶。
    return out[:60] if len(out) > 60 else out


def _is_chinese(s: str) -> bool:
    """判『中文段』：有汉字、无假名——用于多语言标题优先取中文番名。
    ANi 是 罗马音/中文（中文在后），但别的字幕组常把中文放前面，故按内容挑而非按位置。"""
    return bool(_HAN_RE.search(s)) and not _KANA_RE.search(s)


def _group_and_body(raw: str):
    """组名 + 去掉开头 [组] 块后的正文。parse_title/candidate_names/剧场版抓取共用，省重复 _GROUP_RE.match。"""
    m = _GROUP_RE.match(raw)
    return (m.group(1).strip() if m else ""), (raw[m.end():] if m else raw)


def parse_title(raw_title: str):
    """从各家字幕组标题提取 (组名, 番名, 季, 集)。

    多语言标题（形如 [组] A / B - EE [tags]）：番名**优先取中文段**（含汉字、无假名），
    没有中文才回退取末段（ANi 惯例 罗马音/中文，中文在末段）；集数从整体正文抽，不受挑哪段影响。
    繁转简 + 去季名。
    """
    group, body = _group_and_body(raw_title)   # 去开头 [组] 块，免得它混进第一段语言

    if _SLASH_RE.search(body):
        segs = _SLASH_RE.split(body)                 # 各语言段（罗马音/日文/中文，2~3 段）
        name_part = next((s for s in segs if _is_chinese(s)), segs[-1])  # 优先中文段，没有则末段（ANi=中文）
    else:
        name_part = body

    season = extract_season(raw_title)
    episode = extract_episode(body)                  # 集数从整体正文抽：中文段在前时也不丢 '- EE'
    anime_title = strip_season(t2s(_clean_name(name_part)))
    return group, anime_title, season, episode


def _clean_for_search(s: str) -> str:
    """搜 bgm 用的关键词：去标签块与集数段，但**保留内部空格和季标记**
    （罗马音要空格才搜得准；"第二季"/S02 有助于命中正确的季条目）。"""
    # _PROMO_RE 必须一起去掉：★07月新番★ 这类促销串不在标签块里，留着会整段变成 bgm 的搜索词
    # （实测『【喵萌奶茶屋】★07月新番★[猫与龙] - 03』清完只剩 '★07月新番★'，番名反而没了）。
    # _clean_name(157) 与 search_query_names(378) 早就在去它，唯独这条路漏了——parse.py:63 的
    # 注释本来就写着它的存在理由是"污染别名键与 bgm 搜索词"。
    s = _PROMO_RE.sub("", _EXT_RE.sub("", _TAG_BLK_RE.sub("", s)))
    m = _EP_TAIL_RE.search(s)                   # 去 " - 07" 及其后（锚定，不吃 -2nd 副标题）
    if m:
        s = s[:m.start()]
    # 再剥掉首尾【落单】的括号：形如 [猫与龙 / Nekokaburi] 的标题会先被斜杠切成两段，
    # 于是各自剩下半个括号（'[猫与龙' / 'Nekokaburi]'），_TAG_BLK_RE 要成对才认，管不到。
    # 带着半个括号去搜 bgm 是必然搜不中的。
    return s.strip().strip("[]【】()（）〔〕 ")


def candidate_names(raw_title: str) -> list[str]:
    """从标题提取所有可用于搜 bgm 的候选名（日文原名/罗马音/中文，含繁→简）。

    有日文汉字/假名就一并带上（最准）；ANi 一般是 罗马音 + 繁体中文。
    """
    _, body = _group_and_body(raw_title)
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


# ---- 全括号命名的番名回退捕获（沸羊羊/悠哈/GM-Team 等，config 开关控制是否启用）----
_CJK = re.compile(r"[一-鿿぀-ヿ]")
_INNER_BLK = re.compile(r"[\[【]([^\]】]+)[\]】]")
# 明显不是番名的块：分类 / 年份 / 画质 / 编码 / 来源 / 语言 / 纯集号——回退挑名字时跳过它们
_NAME_BLOCK_SKIP = re.compile(
    r"^(?:国漫|国番|日漫|港漫|美漫|新番|完结|補番|补番|\d{4}|\d{1,4}(?:\.\d+)?|"
    r"\d+P|\d+x\d+|4K|x26[45]|H\.?26[45]|HEVC|AVC|AAC|FLAC|OPUS|DDP|"
    r"GB|BIG5|CHT|CHS|BDRIP|BD|WEB-?RIP|WEB-?DL|Baha|B-?Global|CR|Crunchyroll|"
    r"Bilibili|IQIYI|Netflix|ViuTV|NTV|MKV|MP4|TS|SRT|ASS|"
    r"简体?|繁體?|日語?|简繁日?|简日|繁日)$", re.I)


def _is_skip_block(blk: str) -> bool:
    """整块是否为『非番名』的画质/语言/来源块——按空格/下划线拆 token，每个 token 都是 skip 词才算。
    早前用整块锚定匹配，遇 '1080p 简体' / 'x264 CHS' 这类多 token 块会漏判、把它误当番名。"""
    toks = [t for t in re.split(r"[\s_]+", blk.strip()) if t]
    return bool(toks) and all(_NAME_BLOCK_SKIP.match(t) for t in toks)


def parse_multibracket(raw_title: str):
    """全括号命名 [组][番名块][集号][画质…] 的番名回退：挑出番名块 → 拆多语言 → 候选名。

    仅当 parse_title 得空名、且开了 ANIME_MULTIBRACKET_PARSE 时兜底调用（见 nyaa/mikan 源）。
    挑名策略：① 优先含 '/' 的块（中文/罗马音/日文，最明确）；② 退而取第一个『含 CJK 且非标签』的块。
    带信心闸：洗完拿不到 ≥2 字符的合理候选就返回 None——宁可不猜、落『待识别』，也不建垃圾番。
    返回 (anime_title, candidate_names) 或 None。
    """
    m = _GROUP_RE.match(raw_title)
    body = raw_title[m.end():] if m else raw_title
    blocks = _INNER_BLK.findall(body)
    nameblk = next((b for b in blocks if "/" in b), None) or \
        next((b for b in blocks if _CJK.search(b) and not _is_skip_block(b)), None)
    if not nameblk:
        return None

    names: list[str] = []
    for p in re.split(r"\s*/\s*|_", nameblk):        # 中文/罗马音/日文 多用 / 或 _ 分隔
        c = strip_season(_EXT_RE.sub("", _TAG_BLK_RE.sub("", p))).strip()
        if len(c.replace(" ", "")) < 2 or _is_skip_block(c):
            continue
        if c not in names:
            names.append(c)
        if _CJK.search(c):
            simp = t2s(c)                            # bgm 的 name_cn 是简体
            if simp != c and simp not in names:
                names.append(simp)
    if not names:                                    # 信心闸：一个合理候选都没有 → 放弃
        return None
    cjk = [n for n in names if _CJK.search(n)]
    anime_title = (cjk[0] if cjk else names[0]).replace(" ", "")
    return anime_title, names


# ---- 搜种用番名（『补齐该源』用，与搜 bgm 的 candidate_names 分开）----
# 洗完还带括号/促销星 → candidate_names 没洗干净（全括号命名的组），改用 parse_multibracket 的块级候选
_MANGLED_RE = re.compile(r"[\[\]【】★]")
# candidate_names 只锚掉『 - 07』式集号，这些写法会整段留在名字里：第03话 / EP03 / - 03 END
_EP_LEFT_RE = re.compile(
    r"\s*(?:第\s*\d{1,4}\s*[话話集]|\b(?:EP|Episode)\s*\.?\s*\d{1,3}\b|"
    r"[-–]\s*\d{1,3}(?:\.\d+)?\s*(?:END|FIN|完结?)?\s*$)", re.I)


def _split_langs(s: str) -> list[str]:
    """把『中文名/罗马音』这类无空格并列的写法拆开（candidate_names 只拆有空格的 ' / '）。
    仅当两侧语种不同（一侧含 CJK、一侧不含）才拆——同语种的 'Fate/Zero' 是名字本身，拆了反而搜不到。"""
    parts = [p.strip() for p in s.split("/") if p.strip()]
    if len(parts) > 1 and any(_CJK.search(p) for p in parts) and any(not _CJK.search(p) for p in parts):
        return parts
    return [s]


def search_query_names(raw_title: str) -> list[str]:
    """从一条种子标题提炼【去种子站搜同源新种】的关键词（补齐用）。返回去重后的候选，可能为空。

    与 candidate_names（搜 bgm 用）的差别是必须把集号剥干净：带『第03话』『EP03』『- 03 END』的词
    在 nyaa/Mikan 一条都搜不到。全括号命名的组走 parse_multibracket 兜底——这里【不】看
    ANIME_MULTIBRACKET_PARSE 开关：那开关防的是"猜错名字建出垃圾番"，而这里的名字只当搜索词用，
    补齐入库时挂的是既有 anime_id、还要过组名/季号过滤，猜歪最多白发一次请求。
    """
    cands = candidate_names(raw_title)
    if not cands or any(_MANGLED_RE.search(c) for c in cands):
        mb = parse_multibracket(raw_title)      # 全括号命名：番名整块在 [..] 里，candidate_names 洗不出来
        if mb:
            cands = mb[1]
    out: list[str] = []
    for c in cands:
        for part in _split_langs(_PROMO_RE.sub("", c)):
            part = _EP_LEFT_RE.sub("", _TAG_BLK_RE.sub("", part)).strip(" -–—_[]【】()（）★")
            if len(part.replace(" ", "")) >= 2 and part not in out:
                out.append(part)
    return out
