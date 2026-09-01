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
    r"合集|BD-?RIP|BDMV|BD\s?Remux|\bBatch\b|Vol\.\s*\d+|\bTV\s*\+\s*SP\b|第\s*\d+\s*[巻卷]"
    # 【EP 区间守卫必须与 _EP_PATTERNS 的 EP 条同步放宽】那条放到 4 位而这条留在 3 位时，
    # `EP1001-1100` 这种百集合集包就从"合集(丢弃)"变成了"第 1001 集(自动下)" ——
    # 放宽集号反而制造了一条会把整包当单集下回来的路。
    # 【EP 与 Episode 两种写法都要收】抽取侧那条是 `(?:EP|Episode)`，这条只认 EP 的话，
    # `Episode 1001~1100` 仍然会被抽成第 1001 集。守卫的覆盖面必须与它要守的那条一样宽。
    r"|(?<![A-Za-z])(?:EP|Episode)\s*\.?\s*\d{1,4}\s*[-~〜]\s*\d{1,4}", re.I)
# 『搬运/整理』只在【剥掉开头 [组] 块之后】的正文里才算合集信号——这两个词同样是组名的常用字
# （天月搬运组、XX整理组…），按整条标题匹配等于把这些组的【每一条单集】都当合集丢掉，
# 表现是"某个组一集都收不到"，而且丢在 _parse 最前面、日志里连条记录都没有。
# BDMV/BDRIP 这些不能挪进来：'[BDMV] 番名' 的关键词本来就在开头那个块里。
_BATCH_BODY_RE = re.compile(r"搬运|搬運|整理")
# 裸集号范围(01-12/01~12)：连续集合集。单独拆出 + 在 is_batch 里守卫：标题能抽出单集集号时(如
# '[组] Show - 24 (最終回 23-24 総集編)' 的 '23-24')多是注解而非合集范围，不当合集静默丢弃整条。
_BARE_RANGE_RE = re.compile(r"(?<![-A-Za-z0-9])\d{2,3}\s*[-~〜]\s*\d{2,3}(?![-A-Za-z0-9])")

# 集数识别（按优先级）：'- 07'/'- 11.5'/'- 07v2' → S02E07 → 第07話/第二十三话 → [07]/[07v2] → EP07
# 第1条用负向后顾避免吃到范围 01-12 的第二个数，并容忍 v2 版本后缀；第3条兼中文数字；第4条限 1~3 位避免命中 [2024]
# 完结标记：字幕组常在最终话写 『- 24 END』『- 12 完』『- 13 Fin』，它卡在集号与 [tag] 之间，
# 早先的收尾锚点容不下它 → 整条落 -2（未知集），最终话永远不会被自动下。
_FINALE = r"(?:\s*(?:END|FIN|COMPLETE|完|終|终|エンド))?"
# 集号右界：行尾 / 标签块起始 / 【当分隔符用的】连字符。
# 第三种是因为『[组] 番名 - 05 - [简日内嵌][AVC 8bit 1080P]』这类写法——集号后面【还有一个分隔符】
# 才接标签块。旧的右界只认 行尾/左括号，这类标题整条落 -2（未识别）：不自动下、堆进"待识别"，
# 而且 _EP_TAIL_RE 同样锚不到 → 番名脏成 '番名-05-'，别名键与 bgm 搜索词一起废掉。
# 连字符后面若紧跟一个【裸集号】(‘- 01 - 12’) 就不认：那是连续集范围(合集)，不是"集号+分隔符"。
# 判据是"数字后面不再跟字母数字"——1080P/720P/4K 这些带字母尾巴的不算裸集号，
# 所以 '- 05 - 1080P AVC' 这种没有标签块的写法照样能认出 05。
_EP_END = r"\s*(?:$|[\[【(（]|[-–—](?!\s*\d{1,3}(?![0-9A-Za-z])))"
# 【四位数字的两类同形物：年份与分辨率】放宽集号到 4 位之后，这两类必须逐处排掉。
# 抽成片段而不是各写各的：本文件里有【三条】同族正则要用到它（括号写法、EP 兜底、双编号锚点），
# 历史上正是"放宽了其中一条、守卫没跟上"反复出问题。end 是各自的收尾锚（括号写法靠 ]】、
# EP 靠"后面不是数字"、双编号靠 )），所以做成参数而不是写死。
#
# 【但三处排的东西【有意不同】，别硬统一】判据是"猜错的代价可不可逆"：
#   · 括号写法 [1080]：裸写在方括号里的 1080 几乎必是分辨率 → 年份+分辨率都排；
#   · EP 兜底 EP1080：没人用 EP 写分辨率，1080 几乎必是集号（海贼王真播过第 1080 集）
#     → 只排年份；分辨率那一半交给 (?![\dpPiI])，它挡的是 1080p/1080i 这种带单位的写法；
#   · 双编号 - 16(1080)：真歧义，而猜错的代价【不可逆】—— _learn_and_normalize_episode
#     只在 ep_offset 为空时学一次，学成 1064 之后全项目【没有任何入口能重置】
#     → 年份+分辨率都排，宁可少学一次。
def _not_year(end: str) -> str:
    return r"(?!(?:19|20)\d{2}%s)" % end


def _not_resolution(end: str) -> str:
    return r"(?!(?:1080|1440|2160)%s)" % end


_EP_PATTERNS = [
    # 左分隔符连 – — 一起认（右界本来就认）：偶有组用长破折号写『番名 – 05 [1080p]』。
    # 收尾锚点仍是 _EP_END，所以副标题里的 '—2nd Season' 照旧吃不到（后面不是 行尾/括号/分隔符）。
    re.compile(r"(?<!\d)[-–—]\s*(\d{1,4}(?:\.\d+)?)(?:\s*[vV]\d+)?" + _FINALE + _EP_END, re.I),
    # 位数放宽到 4 位【且必须带尾部守卫】：海贼王/柯南这类长番写作 S01E1174。
    # 少了 (?!\d) 的话，5 位集号不是"匹配不上"而是【静默截断】——旧代码正是卡在 3 位又没有守卫，
    # S01E1174/1175/1176 三条连续新集全被截成 117，撞成同一个去重键 (番,集)，
    # flush 每键只放行一份 → 三集里两集永远收不到，而界面把它们标成蓝灰「备用项」、
    # tooltip 写「同集已有更优版本」。真库实际发生过（anime#99 的 1150/1388/1617）。
    # 注意下面 _STRIP_PATTERNS 里的同款 SxxE 必须【同步放宽】，否则集号认出来了、
    # 番名却剩个尾巴（S01E1174 剥掉 S01E117 留下 "4"），而番名是别名键。
    re.compile(r"[Ss]\d{1,2}[Ee](\d{1,4})(?!\d)"),
    re.compile(r"第\s*([一二三四五六七八九十]+|\d+(?:\.\d+)?)\s*[话話集]"),
    # 括号写法 [07]/[07v2]/[28END]/[24 END]/[12完]/[1170]。两处历史坑都在这一条上：
    # ① 少了 _FINALE：桜都/北宇治等组把最终话写成 [28END]，整条落 -2（未知集）→ 每部番都差最后一集。
    # ② 位数卡死 3 位：海贼王/柯南 的 [1170] 抽不出集号，整部千集长番进"待识别"。
    # 放宽到 4 位就必须排掉两类同形的东西——负向前瞻只看【整块】，所以 [1080P] 这种带字母尾巴的
    # 本来就匹配不上，需要排的只有裸写的年份与分辨率：
    #   [2023]/[1999] → 发行年（GM-Team 惯例 [国漫][番名][2023][172]，年份在集号前面，先撞上）
    #   [1080]/[1440]/[2160] → 裸写的分辨率（720/480 这些 3 位的旧代码本来就在收，沿用现状不动）
    re.compile(r"[\[【]" + _not_year(r"[\]】]") + _not_resolution(r"[\]】]")
               + r"(\d{1,4}(?:\.\d+)?)(?:\s*[vV]\d+)?" + _FINALE + r"\s*[\]】]", re.I),
    # EP07 / Episode 7 / [EP07]：放最后当兜底（上面四条都认不出才轮到它）。
    # 前面禁字母数字，免得吃到 'Deep 3' 的 ep、'S02EP07' 由第 2 条先管。
    # 【放宽到 4 位时必须把括号写法那套守卫一起带过来】上面那条括号写法放宽时写了
    # `(?!(?:19|20)\d{2}[\]】]|(?:1080|1440|2160)[\]】])` 来排掉同形的年份与分辨率；
    # 这条兜底从 3 位放宽到 4 位时漏了同一道守卫，于是 `EP.1080p` 被当成第 1080 集、
    # `EP.2023` 被当成第 2023 集 —— 而 3 位时代它们都匹配不上（\d{1,3} 后面跟数字被 (?!\d) 否掉），
    # 落 -2『未知集』且 auto_downloadable_ep 拒绝自动下。放宽反而制造了一条【会自动下错东西】的路。
    # 【这条只排年份，不排分辨率】原来两类都排，代价是 EP1080/EP1440/EP2160 这些【真集号】
    # 一律落 -2（海贼王真播过第 1080 集）。而 1080p / 1080i 这种带单位的写法早已被
    # 尾部的 (?![\dpPiI]) 挡住——实测这条守卫自带的 5 条用例里有 4 条是它挡下的，
    # 分辨率那一半白丢真集号。年份那一半留着：EP.2023 这种确实需要它。
    re.compile(r"(?<![A-Za-z0-9])(?:EP|Episode)\s*\.?\s*" + _not_year(r"(?!\d)")
               + r"(\d{1,4})(?![\dpPiI])", re.I),
]
# 常见视频扩展名后缀（ANi 等种子名带 .mp4/.mkv 结尾，先剥掉再抽集数段，否则 '- 07 .mp4' 的集数段锚不到行尾）
_EXT_RE = re.compile(r"\.(mp4|mkv|avi|ts|flv|rmvb|wmv|mov|m2ts|webm)\s*$", re.I)
# 从番名里剥掉的集数段：锚定到『空格-空格数字(可带 v2)后接括号/行尾/分隔连字符』，别吃副标题里的 -2nd
_EP_TAIL = (r"\s[-–—]\s*\d{1,4}(?:\.\d+)?(?:\s*[vV]\d+)?" + _FINALE + _EP_END)
_EP_TAIL_RE = re.compile(_EP_TAIL, re.I)        # 预编译（_clean_for_search 用）
# 【必须与 _EP_TAIL_RE 同样带 re.I】它们复用同一个 _EP_TAIL 字符串，而完结标记里有 END/FIN/Complete
# 这些字母：少了 re.I 就会出现"集号认出来了、番名却没洗干净"的半吊子——
# '[ANi] Some Show - 24 Fin' 的集号是 24（_EP_PATTERNS 带 re.I），番名却留成 'SomeShow-24Fin'，
# 而番名是别名键，等于给同一部番造了个新身份。
# 不带季号的集号写法（第07话 / EP07）——与 _EP_PATTERNS 对齐：集号认得出来，就要从名字里洗得掉。
_EP_MARK = (r"第\s*(?:[一二三四五六七八九十]+|\d+)\s*[话話集]",
            # 【剥离侧【不】带守卫，与同文件的 SxxE 剥离条同口径】那条的理由逐字适用：
            # 抽取要"宁可认不出，也别认错"，剥离要"番名必须干净"——番名经 alias_key 就是番的身份键。
            # 带守卫时 EP1080 认不出集号、名字也不剥，番名变成 'OnePieceEP10801080pCRWEB-DL'，
            # 于是同一部番的 EP1174 进「OnePiece」、EP1080 另建一条垃圾番，候选名还脏到 bgm 搜不到。
            # 认不出集号 ≠ 名字要留着它。
            r"(?<![A-Za-z0-9])EP\s*\.?\s*\d{1,4}(?![\dpPiI])")
_STRIP_PATTERNS = [re.compile(p, re.I) for p in (     # 预编译（_clean_name 循环用）
    # 【剥离侧【不】带尾部守卫，与抽取侧有意不同】抽取要的是"宁可认不出，也别认错"，
    # 所以 SxxE 那条带 (?!\d)；但剥离要的是"番名必须干净"——番名经 alias_key 就是番的身份键，
    # 脏一个字符就等于给同一部番造了个新身份、裂成两部。
    # 带守卫的话，S01E11745（5 位）整条匹配不上、一个字符都不剥，番名变成 'ShowS01E11745'；
    # 而它的集号本来就该落 -2（5 位不是合法集号），两件事互不矛盾：认不出集号 ≠ 名字要留着它。
    _EP_TAIL, r"[Ss]\d{1,2}[Ee]\d+", *_EP_MARK)]
# 搜 bgm 用的同款集号段，但【不含 S02E07】：那条带着季号，留在搜索词里有助于命中正确的季条目
# （_clean_for_search 的既定策略就是"去集号、留季标记"）。
_SEARCH_STRIP_PATTERNS = [re.compile(p, re.I) for p in (_EP_TAIL, *_EP_MARK)]
# 标签块 [..]/【..】（含空括号，整体替换）——_clean_name/_clean_for_search/parse_multibracket 共用。
# 勿与 _INNER_BLK(带捕获组、+ 不匹配空括号) 混用：对空括号 [] 的 sub 结果不同。
_TAG_BLK_RE = re.compile(r"[\[【][^\]】]*[\]】]")
# 宣传标记『★07月新番★』（喵萌等）：它后面紧跟的 [ 会因语言分段被切走而失去右括号，
# _TAG_BLK_RE 要求成对故删不掉，残留在番名里污染 别名键 与 bgm 搜索词。
# 收尾星号可缺：有的组只写单边（'[漫貓字幕組]★04月新番[番名][05]…'）。少了这一支，整条标题
# 洗完只剩 '★04月新番'——它会被当成番名建库，等于每个季度给每个这种组造一部假番。
# 右界同时收在 [ 【 上：不是"吃到下一个星号为止"，免得没有收尾星时把后面的番名块一起吞掉。
_PROMO_RE = re.compile(r"★[^★\[【]*★?")


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
    if _BATCH_BODY_RE.search(_group_and_body(title)[1]):   # 搬运/整理：只看正文，别被组名带沟里
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


# 特典标记。两条判据分工不同，别合并：
# · _SPECIAL_TAG_RE —— 标记【独占一个方括号】。它【压过】后面抽到的数字集号（见 extract_episode）。
#   这是字幕组标注特典的通行写法，误伤面小：`[特別篇]` 只可能是特典标记。
# · _SPECIAL_LOOSE_RE —— 标题里【出现】这些词。它只在【一个数字都抽不到】时当兜底用，
#   把 -2（未知）细化成 -1（特别篇）。宽松是安全的，因为那一支本来就不会自动下载。
_SPECIAL_TAG_RE = re.compile(
    r"[\[【]\s*(?:特[别別]篇|OVA|OAD|SP|Special|映像特典|NCOP|NCED)\s*[\]】]", re.I)
_SPECIAL_LOOSE_RE = re.compile(r"特[别別]篇|\bOVA\b|\bOAD\b", re.I)


def extract_season(text: str) -> int:
    s = _find_season(text)
    if s is not None:
        return s
    m = _SEASON_EN_RE.search(text)      # 兜底 S02E07
    if m:
        return int(m.group(1))
    m = _SEASON_BARE_RE.search(text)    # 再兜底裸 S4（『番名 S4 - 17』）
    return int(m.group(1)) if m else 1


# 罗马数字季标记（『Clevatess II』『幼女戦記Ⅱ』『無職転生Ⅲ』）。全角与半角都收。
# 【只收 II/III/IV，不收 I】季 1 本来就是默认值，认出它一分收益没有，
# 而半角的 "I" 在英文名里是人称代词（"I'm …"），认它只会白白多一类误判。
# 【前后不许挨字母数字】否则会吃到 "AVC"、"IV" 结尾的单词、"S2E11" 里的数字。
# 【已知边界：紧跟汉字的 II 不排除】"ルイII世"（路易二世）会被读成第 2 季。
# 不排除是有意的——日语的季标记本来就写作『第Ⅱ期』，把汉字后缀一并排除会把它挡掉，
# 而两者在这个位置上无法区分。真库 169 个 bgm 名里没有这类，故按"宁可多认"处理；
# 真出现了，纠正入口是详情页的『编辑季度』。
_SEASON_ROMAN_RE = re.compile(r"(?<![A-Za-z0-9])(Ⅳ|Ⅲ|Ⅱ|IV|III|II)(?![A-Za-z0-9])")
_ROMAN_NUM = {"Ⅱ": 2, "Ⅲ": 3, "Ⅳ": 4, "II": 2, "III": 3, "IV": 4}


def season_from_name(name: str):
    """从 bgm 规范名/日文名反推季号（bgm 权威，名字里带『第X季/Season N/Ⅱ』时用）。

    【罗马数字只在这里认，不进 extract_season】本函数的输入只有 bgm 的规范名与日文名，
    是一个干净、可控的集合；而 extract_season 吃的是字幕组的原始标题，那里 II/IV 出现在
    编码参数、组名、作品名里的概率高得多，放进去要先在真库 1650 条标题上扫误报。
    实测：真库 169 个 bgm 名里命中 5 个，其中 4 个与已有 season 一致（等于交叉验证），
    只有 anime#46『Clevatess II』的 season 从错的 1 纠正成 2。
    """
    if not name:
        return None
    sn = _find_season(name)
    if sn is not None:
        return sn
    m = _SEASON_ROMAN_RE.search(name)
    return _ROMAN_NUM[m.group(1)] if m else None


def strip_season(title: str) -> str:
    return _SEASON_CN_RE.sub("", title)


# 双编号写法：'- 16(88)' —— 括号外是【季内】集号、括号内是【全系列绝对】集号（LoliHouse 系常用）。
# 它是跨源集号归一的现成锚点：offset = 绝对 - 季内，拿到之后就能把别的源（如 ANi 直接写 88）
# 的绝对集号折算回季内集号，避免同一集因两种编号体系被当成两集、各下一份到同一目录。
# 【括号内放宽到 4 位，括号外保持 3 位】原注释写的是"两数都限 1~3 位（真集号不会四位）"——
# 那句话已经被本轮自己推翻了：放宽 SxxEnnnn / [1170] 的动机正是海贼王、柯南这类千集长番。
# 而这里是跨源集号归一的【唯一锚点】，卡在 3 位就意味着：恰恰是被放宽支持的那批长番，
# ep_offset 永远学不到（'- 16(1088)' 抽不出 abs），于是同一集的两套编号并存、各下一份到同一目录。
# 真库 anime#27 就是这个下场（ANi 绝对 13–19 + Nix-Raws 季内 1–7，7 集各下了两份）。
#
# 括号【外】仍限 3 位：那是季内集号，一季不会有一千集；放宽它只会多招误匹配。
# 括号【内】放宽到 4 位，但必须补一道年份守卫——原注释点出的 '- 04(2024)' 是真危险：
# 3 位时代它天然匹配不上，4 位之后就会被当成 offset=2020 去污染全番的集号折算。
# '- 12(3)' 那类碟片编号仍由下面 `rel < absolute` 挡住。
_EP_DUAL_RE = re.compile(
    r"(?<!\d)-\s*(\d{1,3})(?:\s*[vV]\d+)?\s*"
    r"\(\s*" + _not_year(r"\s*\)") + _not_resolution(r"\s*\)") + r"(\d{1,4})\s*\)(?!\d)")


def extract_episode_abs(text: str) -> int | None:
    """标题带双编号 'NN(MM)' 时返回绝对集号 MM，否则 None。季内集号仍由 extract_episode 给。"""
    m = _EP_DUAL_RE.search(text)
    if not m:
        return None
    rel, absolute = int(m.group(1)), int(m.group(2))
    # 上界跟着括号内的位数一起放宽（原为 999，与旧的 \d{1,3} 配套）。
    # 年份已由正则的负向前瞻挡掉，这里只保留"绝对号严格大于季内号"这条语义闸。
    return absolute if 1 <= rel < absolute <= 9999 else None


def extract_episode(text: str):
    """整数集→int，小数集(11.5)→float，中文数字(第二十三话)→int，特别篇/OVA→-1，无法识别→-2。"""
    text = _EXT_RE.sub("", text)   # 先剥 .mkv/.mp4：'Show - 05.mkv' 的集号段否则锚不到行尾
    # 【标记独占一个方括号时，它压过后面的数字】`[ANi] 我的英雄學院 FINAL SEASON [特別篇] - 01`
    # 的 `- 01` 是【这一批特别篇里的第 1 个】，不是正片第 1 集。原来的 特别篇/OVA 判据是
    # 写在最后当兜底的：只有一个数字都抽不到时才生效，而这种标题抽得到，于是它被当成正片第 1 集
    # 入库、参与集去重、占住第 1 集的位置，而正片第 1 集来的时候就被去重挡掉了。
    # 真库 anime#95『我的英雄学院 FINAL SEASON』是唯一一部到今天还没识别出 bgm 的番，
    # 卡的就是这里（它名下只有这一条种子，而它被当成了正片）。
    #
    # 【判据为什么收紧到"独占一个方括号"，而不是"标题里含这几个字"】
    # 真库 1660 条标题里两种判法命中的都只有这 1 条，差集为 0 —— 也就是说没有任何数据能告诉我
    # 宽松写法安不安全。而宽松写法的失手方式是明摆着的：番名本身带 OVA/SP 的番
    # （『はたらく細胞!! OVA』这类）会被整部打成特别篇，一集正片都不下。
    # 独占方括号是字幕组标注特典的通行写法，也是唯一有实证的那一种。
    if _SPECIAL_TAG_RE.search(text):
        return -1
    for pat in _EP_PATTERNS:
        m = pat.search(text)
        if m:
            v = m.group(1)
            if v.replace(".", "").isdigit():
                return int(v) if "." not in v else float(v)
            return _cn_to_int(v)   # 中文数字集号（第二十三话）
    return -1 if _SPECIAL_LOOSE_RE.search(text) else -2


# 【番名本身就写成【…】的番】`【我推的孩子】`/`【推しの子】`/`【咒术回战】` —— 那对方括号
# 是官方标题的一部分，不是标签块。_TAG_BLK_RE 会把它整块删掉，于是番名洗成空串，
# 而 sources/base.py 对空番名是【静默丢弃整条】：那部番从某几个组那里一集都收不到，
# 界面和日志零信号（实测：日志一条记录都没有，连计数都不出现）。
# 判据用【纯回退】：先按原样洗一遍，洗不出可用名字时才走解包。
# 【原注释说"正常标题一律走原路径、行为不变""回退只会让它们从丢弃变成能解析"——那是错的】：
# 第一版只判"首块不是标签"，在 1499 条真实标题语料上有 12 条行为改变，
# 而且全部是从"丢弃"变成"**猜错名字**"——后者严重得多（丢弃是零 DB 写入的静默丢包，
# 猜错名字会写下不可逆的 alias 行与 info_hash 占用）。现在的三条判据见下面 _clean_name。
_LEAD_BLK_RE = re.compile(r"^\s*[\[【]([^\]】]+)[\]】]")


# 只是一个季号、没有番名的残渣：'S2' / 'Season 2' / '2nd Season'。
# 【为什么要单独判】strip_season 只认中文季名（第X季/期），英文写法它不动。
# 于是 `【我推的孩子】 S2 - 03` 洗完剩下 'S2'，非空 → 解包回退不触发 →
# 番名就是 'S2'：该组所有这么写的【】番全并进一部叫 S2 的假番里（与 F1 同款损坏）。
# 真番名不会长成这样，所以整段匹配是安全的。
_SEASON_ONLY_RE = re.compile(
    r"(?:S|Season)\s*\d{1,2}|\d{1,2}(?:st|nd|rd|th)\s*Season"
    r"|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]+"                      # 罗马数字季号：'【我推的孩子】Ⅱ' 曾解析出番名 'Ⅱ'
    r"|\d{1,2}\s*[期季部]|Part\s*\d{1,2}", re.I)


def _unwrap_name_block(name_part: str) -> str | None:
    """整段是不是「番名本身写在括号里」的形状？是就返回解包后的文本，否则 None。

    **全项目唯一一份判据**，`_clean_name`（定番名）与 `_clean_for_search`（定搜索词）共用。
    【为什么必须共用】它们是同一件事的两半，而分家过一次、代价很大：
    第 11 轮只给 `_clean_name` 加了这条回退，于是 `【咒术回战】 第二季 - 24` 的番名修好了，
    **搜索词却塌成 `['第二季']`** —— 而 search_names 是 enrich.resolve 的唯一入参，
    两部不同的「第二季」会搜到同一个 bgm 条目、被合并成一部番（不可逆）。

    三条判据（缺一不可，理由见 _clean_name）：
      ① 非标签块恰好一个；② 它是首块；③ 只有一个块，或块外还有正文。
    """
    blocks = [b[1:-1] for b in _TAG_BLK_RE.findall(name_part or "")]
    real = [b for b in blocks if not _is_skip_block(b)]
    outside = _TAG_BLK_RE.sub("", name_part or "").strip()
    if len(real) == 1 and blocks and blocks[0] == real[0] and (len(blocks) == 1 or outside):
        return _LEAD_BLK_RE.sub(r"\1", name_part, count=1)
    return None


def _usable_name(s: str) -> bool:
    """洗出来的这段能当番名用吗——去掉季名后还得剩点东西，且不能是季号/标签一类的残渣。

    【为什么要问 _is_skip_block】本模块早就有一份「这不是番名」的词表（画质/语言/来源/完结…），
    但它以前只在解包回退里被调用，主路径从不问它 —— 于是同一个词写在括号里判成标签、
    写在括号外就成了番名：`【咒术回战】 完结 - 24` 解析出的番名是 `完结`、
    `【我推的孩子】Ⅱ` 是 `Ⅱ`。这类残渣当番名的后果与季号一样：该组所有这么写的番
    共用一个别名、被并成同一部；而 `Ⅱ` 只有一个字符，还会被 candidate_names 的长度闸滤掉，
    bgm 永远救不回来。
    """
    t = strip_season(t2s(s or "")).strip()
    if not t or _SEASON_ONLY_RE.fullmatch(t):
        return False
    # 【纯数字要放行】_NAME_BLOCK_SKIP 里有 `\d{4}` 与 `\d{1,4}` 两条——它们是给**括号块**用的
    # （`[05]`、`[2024]` 这种块几乎必然是集号/年份），可整段番名是纯数字却完全正当：
    # `86`、`1122`。把那条规则原样套到整段名字上，`[ANi] 86 - 05` 的番名会变成空串，
    # 而空番名 = 主流程**静默丢弃整条种子** —— 那部番一集都收不到。
    # （这一条是把 _is_skip_block 接进本函数时漏想的：块级判据的作用域被扩到了整段名字。）
    if t.replace(".", "").isdigit():
        return True
    return not _is_skip_block(t)


def _clean_name(name_part: str) -> str:
    """去掉 [..]/【..】 标签块、扩展名与集数段，得到干净番名（无空格）。

    **洗成空时把开头那个块解包再洗一遍**（纯回退，正常情况下这一支根本不会走到）。
    见上面 _LEAD_BLK_RE 的说明。
    """
    s = _clean_name_once(name_part)
    if _usable_name(s):
        return s
    # 【只解包"整段就是一个番名块"这一种形状】——那正是 D5 要修的 `【我推的孩子】 - 11`。
    # 判据有两条，缺一不可：
    #   ① 非标签块【恰好一个】；② 它就是【首块】。
    # 【为什么不能只看首块】第一版只判了"首块不是标签"，于是任何全括号标题的第一个块都被当成番名：
    #   [诸神字幕组][2024年10月新番][青之箱][05][1080P] → 番名 '2024年10月新番'
    #   [天使动漫论坛][www.tsdm39.com][10月新番][败犬女主太多了][01] → 番名 'www.tsdm39.com'
    # 后果不是"名字难看"：该组当季所有番共用这一个别名、被并成同一部——落进同一个目录，
    # 集去重键 (anime_id, episode) 还会让不同番的同号集互相撞成 skipped，且 info_hash 被占死【不可逆】。
    # 而且它**架空了 ANIME_MULTIBRACKET_PARSE 开关**：全括号命名本该由 parse_multibracket 处理，
    # 那条路径受开关管（默认关，因为猜名可能猜错），这里却无条件先把名字猜了。
    unwrapped = _unwrap_name_block(name_part)
    if unwrapped is not None:
        alt = _clean_name_once(unwrapped)
        if _usable_name(alt):
            return alt
    return s if _usable_name(s) else ""


def _clean_name_once(name_part: str) -> str:
    # 注：_is_skip_block 定义在本文件更下方，运行时才解析名字，不影响。
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


def quarter_of(dt: datetime) -> str:
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


def movie_quarter_of(dt: datetime) -> str:
    """剧场版按【上映那一年】归档，不走 quarter_of 的"12 月归次年冬季"。

    `quarter_of` 那条规则对**番剧**是对的：12 月开播的季播番实际跨 1–3 月播完，
    整季归次年冬季才不会被劈成两个目录。而**剧场版是一次性上映**——
    页面上那一栏就叫「年份」、归档目录走 `MOVIE_QUARTER_FMT`（默认 `{yyyy}`），
    按番剧的规则算就会把 12 月首映的片放进次年。

    真库实证（E-30，2026-09-01 拍板）：70 部里有 5 部这样，
    『剧场版 间谍过家家 代号：白』2023-12-22 → 落进 `…/Movie/2024/`，
    另有『窗边的小豆豆』『青春猪头少年不会梦到红书包女孩』等 4 部同形。

    【仍返回季度键而不是年份】`Movie.quarter` 的列名与类型不动（改列要迁移），
    季母固定 A —— 剧场版不看季，取年份一律走 `core.engine.quarter_year()`。
    """
    return f"{str(dt.year)[2:]}A"


# ABCD ↔ 季节 / 首月（与 quarter_of 一致：A冬1月 B春4月 C夏7月 D秋10月）
# 季度字母→季名的唯一来源；mikan.season_cn 与 /movies 季度选择器都复用它，避免各处各维护一份。
SEASON_CN = {"A": "冬", "B": "春", "C": "夏", "D": "秋"}
_Q_MONTH = {"A": 1, "B": 4, "C": 7, "D": 10}
_QUARTER_KEY_RE = re.compile(r"(\d{2})([A-D])")

# 【两位年的世纪基准】季度键只存两位年（'99D'），世纪【没有存】。历史上全项目一律按 20xx 解释，
# 于是 1999 年首播的番（海贼王、柯南这类长番在 bgm 上的首播日就在上世纪）会：
#   ① 显示成『2099年秋』（{yyyy} 是设置页季度模板下拉的第一项，用户一点即中）；
#   ② 在番剧列表里排到【当季之上的第一位】——分组是 sorted(reverse=True) 的纯字符串比较，
#      '99D' > '26C'，而 pages/anime.py 又让第一组默认展开：打开首页看到的是 1999 年的番，
#      当季那组反而折叠在它下面。真库实测过（anime#99）。
# yy >= 基准 → 19xx，否则 20xx。取 40 ⇒ 有效窗口 1940–2039，覆盖全部有 bgm 条目的动画
# （电视动画始于 1963，最早的动画长片在 1940 年代），并留 13 年余量。
# 【到 2040 年要处理】那时 '40C' 会被解释成 1940 而不是 2040。届时的正解是把键改成四位年，
# 但那要连带迁移 anime/movie/animetorrent 三处的 quarter 列【以及磁盘上已归档的目录名】，
# 不是这一层能解决的——所以这里只做解释、不改键的格式。
_CENTURY_PIVOT = 40


def quarter_year(quarter: str) -> int | None:
    """季度键('26C'/'99D') → 四位年份(2026/1999)；解析不出回 None。

    **"从两位年还原世纪"只此一份**——core.engine.quarter_year 与 format_quarter 的 {yyyy}
    都转调它。历史上这两处各写各的 `2000 + int(...)` / f"20{yy}"，于是同一个 '99D'
    在排序、显示、归档三条路径上可以给出不同的年份。
    """
    m = _QUARTER_KEY_RE.fullmatch(quarter or "")
    if not m:
        return None
    yy = int(m.group(1))
    return (1900 if yy >= _CENTURY_PIVOT else 2000) + yy


def quarter_sort_key(quarter: str) -> tuple:
    """季度键的排序键：(四位年, 季字母)。解析不出的排最后（用 -1 年）。

    直接对季度键做字符串排序是错的：'99D' > '26C'，1999 的番会排到 2026 之前。
    """
    y = quarter_year(quarter)
    return (-1, "") if y is None else (y, (quarter or "")[2:3])


def format_quarter(quarter: str, fmt: str) -> str:
    """把内部季度键(如 '26C')按模板渲染成显示名/文件夹名。

    占位：{yy}=26 {yyyy}=2026 {q}=C {season}=夏 {m}=7。
    解析不出(旧数据/未知/None)或模板写错 → 原样返回，绝不抛异常。
    """
    m = _QUARTER_KEY_RE.fullmatch(quarter or "")
    if not m:
        return quarter or ""
    yy, q = m.group(1), m.group(2)
    ctx = {"yy": yy, "yyyy": str(quarter_year(quarter)), "q": q,
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


def kw_match(kw: str, raw: str) -> bool:
    """版本/标题关键词是否命中种子原名？**大小写不敏感**子串（繁日/简日/1080p 等）。

    【五处共用这一个判据】源组的 title_filter 与 subgroups（nyaa/mikan 的 _parse 各两处）
    与单番的 pref_keyword（core.anime）。此前源组那两处用的是裸 `in`（大小写敏感）：用户在源管理页填 `1080p`
    而该组标题写的是 `1080P`，那个源组每一轮都会被整体过滤成 0 条，日志里只有一行"0 条"，
    而设置页对 pref_keyword 的示例文案用的恰恰是小写的 `1080p`——两处口径相反最容易踩。
    """
    return (kw or "").lower() in (raw or "").lower()


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

    # 【与集数同口径：都从 body 抽，不从 raw_title】raw_title 带着开头那个 [组] 块，
    # 组名里恰好有 'S2'/'Ⅱ' 之类字样时（如 '[Sakurato-S2] 某番'）会被读成第 2 季，
    # 于是该组收的每一部番都被整体挪进第 2 季：季度目录错、跨源集号也对不上。
    # body 已由 _group_and_body 去掉组块，而 '[某组][S2] 某番' 这种真把季号单独成块的写法仍在 body 里。
    season = extract_season(body)
    episode = extract_episode(body)                  # 集数从整体正文抽：中文段在前时也不丢 '- EE'
    anime_title = strip_season(t2s(_clean_name(name_part)))
    if not anime_title and _SLASH_RE.search(body):
        # 选中的语言段被洗空了（如 '【我推的孩子】第二季' —— 块被当标签删掉、只剩季名再被 strip 掉），
        # 而【别的语言段还在】。丢掉整条不如换一段：番名为空 = 主流程静默丢弃整条种子。
        for seg in _SLASH_RE.split(body):
            if seg is name_part:
                continue
            alt = strip_season(t2s(_clean_name(seg)))
            if alt:
                anime_title = alt
                break
    return group, anime_title, season, episode


def _clean_for_search(s: str) -> str:
    """搜 bgm 用的关键词：去标签块与集数段，但**保留内部空格和季标记**
    （罗马音要空格才搜得准；"第二季"/S02 有助于命中正确的季条目）。"""
    # _PROMO_RE 必须一起去掉：★07月新番★ 这类促销串不在标签块里，留着会整段变成 bgm 的搜索词
    # （实测『【喵萌奶茶屋】★07月新番★[猫与龙] - 03』清完只剩 '★07月新番★'，番名反而没了）。
    # _clean_name(157) 与 search_query_names(378) 早就在去它，唯独这条路漏了——parse.py:63 的
    # 注释本来就写着它的存在理由是"污染别名键与 bgm 搜索词"。
    s = _PROMO_RE.sub("", _EXT_RE.sub("", _TAG_BLK_RE.sub("", s)))
    # 去 " - 07" / "第07话" / "EP07" 及其后（都锚定，不吃 -2nd 副标题）。集号没写在独立括号块里、
    # 而是贴在名字后面时（'番名 第05话 [1080p]'），只锚 " - 07" 会把集号一起当成搜索词发给 bgm。
    for pat in _SEARCH_STRIP_PATTERNS:
        m = pat.search(s)
        if m:
            s = s[:m.start()]
            break
    # 再剥掉首尾【落单】的括号：形如 [猫与龙 / Nekokaburi] 的标题会先被斜杠切成两段，
    # 于是各自剩下半个括号（'[猫与龙' / 'Nekokaburi]'），_TAG_BLK_RE 要成对才认，管不到。
    # 带着半个括号去搜 bgm 是必然搜不中的。
    return s.strip().strip("[]【】()（）〔〕 ")


def _search_name_of(seg: str) -> str:
    """一段文本 → 搜 bgm 用的关键词，**含"番名写在括号里"的解包回退**。

    与 _clean_name 共用 _unwrap_name_block：那两条路径必须同进同退，
    否则会出现"番名解析对了、搜索词却是『第二季』"这种最难查的分家（第 12 轮踩过）。
    """
    out = _clean_for_search(seg)
    if _usable_name(out):
        return out
    unwrapped = _unwrap_name_block(seg)
    if unwrapped is not None:
        alt = _clean_for_search(unwrapped)
        if _usable_name(alt):
            return alt
    return out


def candidate_names(raw_title: str) -> list[str]:
    """从标题提取所有可用于搜 bgm 的候选名（日文原名/罗马音/中文，含繁→简）。

    有日文汉字/假名就一并带上（最准）；ANi 一般是 罗马音 + 繁体中文。
    """
    _, body = _group_and_body(raw_title)
    # 全拆，不是只拆一刀：三段式『中文 / 罗马音 / 日文』很常见，只拆一刀会把后两段黏成一个候选
    # （'Saijo no Osewa / 才女のお世话 - 05 -'），既搜不中 bgm，还白白丢掉最准的那个日文原名。
    parts = _SLASH_RE.split(body) if _SLASH_RE.search(body) else [body]

    names: list[str] = []
    for p in parts:
        cleaned = _search_name_of(p)      # 含解包回退，见那里
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
    # '4月新番'/'10月新番'/'四月新番' 是【季度栏目名】不是番名。漏了它的后果比看起来重：
    # 天使动漫等论坛的每条标题都带这个块，且它排在真番名【前面】，于是同一论坛当季所有番会
    # 共用别名 ('4月新番', 1) 被并成同一部假番——落进同一个目录，集号还会互相撞成 skipped。
    # 【年份前缀要一起吃掉】'10月新番' 挡得住，'2024年10月新番' 挡不住——它从行首锚定，
    # 年份一加就落空。而后者正是「番名写在【】里」那条修法明确交给 parse_multibracket 的
    # 那类标题，交过去之后它会犯同样的错：该组当季所有番共用一个别名、挂同一个 anime_id。
    r"^(?:(?:\d{2,4}\s*年\s*)?(?:\d{1,2}|[一二三四五六七八九十]{1,3})\s*月新?番|"
    r"招募\w*|招聘\w*|合集|全集|完結|"
    # 版本/修订类：它们与"完结"同族——写在括号里判成标签、写在括号外就成了番名。
    # `【某番】 修正版 - 03` 曾解析出番名 '修正版'，该组所有这么发的番共用一个别名。
    r"修正版|修正|重制版|重製版|无修版?|無修版?|TV版|剧场版|劇場版|完结撒花|完結撒花|"
    r"国漫|國漫|国番|國番|日漫|港漫|美漫|新番|完结|補番|补番|\d{4}|\d{1,4}(?:\.\d+)?|"
    r"\d+P|\d+x\d+|4K|\d{1,2}\s*bits?|x26[45]|H\.?26[45]|HEVC|AVC|AAC|FLAC|OPUS|DDP|"
    r"GB|BIG5|CHT|CHS|BDRIP|BD|WEB-?RIP|WEB-?DL|Baha|B-?Global|CR|Crunchyroll|"
    r"Bilibili|IQIYI|Netflix|ViuTV|NTV|MKV|MP4|TS|SRT|ASS|"
    # 语言块：简/繁/日… 后面常挂 内嵌/内封/外挂/双语（'简日内嵌'『繁日內嵌』）。逐块列举列不完，
    # 按『语言字 ×1~3 + 可选字幕形式』整体匹配。真番名不会长成这样（'日常' 的 '常' 不在语言字里，
    # 整块匹配不上 → 不会被误跳过）。
    # 【语言字要简繁成对】后缀那一半（体體/语語）本来就成对，唯独首字漏了「簡」——
    # 于是繁体组的 [簡繁內封] / [簡日雙語] / [簡體] 全部逃过跳过闸，而它们的简体版全部命中。
    # 逃过之后那个块会被当成番名：parse_multibracket 挑名时可能挑中它，
    # _clean_name 的解包回退也会把它当番名 —— 结果是建出一部叫「簡繁內封」的假番，
    # 而该组当季所有番共用这一个别名、被并成同一部。
    r"(?:[简簡繁中日英港台][体體语語文]?){1,3}(?:内嵌|內嵌|内封|內封|外挂|外掛|双语|雙語|双字|雙字|字幕)?)$", re.I)


def _is_skip_block(blk: str) -> bool:
    """整块是否为『非番名』的画质/语言/来源块——按空格/下划线拆 token，每个 token 都是 skip 词才算。
    早前用整块锚定匹配，遇 '1080p 简体' / 'x264 CHS' 这类多 token 块会漏判、把它误当番名。"""
    toks = [t for t in re.split(r"[\s_]+", blk.strip()) if t]
    return bool(toks) and all(_NAME_BLOCK_SKIP.match(t) for t in toks)


def parse_multibracket(raw_title: str):
    """全括号命名 [组][番名块][集号][画质…] 的番名回退：挑出番名块 → 拆多语言 → 候选名。

    仅当 parse_title 得空名、且开了 ANIME_MULTIBRACKET_PARSE 时兜底调用（见 nyaa/mikan 源）。
    挑名策略：① 优先【真·多语言并列】的块（一侧含 CJK、一侧不含，判据同 _split_langs）；
    ② 退而取第一个『含 CJK 且非标签』的块；③ 再退取第一个非标签块（纯拉丁名的番靠这一档）。
    带信心闸：洗完拿不到 ≥2 字符的合理候选就返回 None——宁可不猜、落『待识别』，也不建垃圾番。
    返回 (anime_title, candidate_names) 或 None。
    """
    m = _GROUP_RE.match(raw_title)
    body = raw_title[m.end():] if m else raw_title
    blocks = _INNER_BLK.findall(body)
    # ① 优先含 '/' 的块，但【必须是真的多语言并列】——判据复用 _split_langs（一侧含 CJK、一侧不含）。
    #    无条件认 '/' 会把 [Fate/Zero] 拆成 'Fate'，整个 Fate 系列并成一部番；
    #    也会让 [招募/翻译] 这类块挤掉后面真正的番名块。
    nameblk = next((b for b in blocks if len(_split_langs(b)) > 1 and not _is_skip_block(b)), None) or \
        next((b for b in blocks if _CJK.search(b) and not _is_skip_block(b)), None) or \
        next((b for b in blocks if not _is_skip_block(b) and len(b.strip()) >= 2), None)
    if not nameblk:
        return None

    names: list[str] = []
    # 块内再拆多语言：'_' 恒拆（只当分隔符用），'/' 走 _split_langs 的同一判据——
    # 否则挑对了 [Fate/Zero] 这个块，也会在这里被拆回 'Fate'，等于没修。
    for p in [q for seg in nameblk.split("_") for q in _split_langs(seg)]:
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
# 末尾允许再挂一个分隔符（'番名 - 05 -'）：清理跑在 strip 之前，不带上它就只剥掉那根光杆连字符、
# 把 '- 05' 留在搜索词里，去种子站一条都搜不到（补齐该源因此永远空手而归）。
# 【每个 \s* 后面都跟一个【必需】的 token，不要留相邻的可选空白】
# 原写法尾部是 `\s*(?:END|FIN|完结?)?\s*[-–—]?\s*$` —— 三段 \s* 中间夹两个可选组，
# 一段空白可以被这三段以指数级多种方式瓜分。整体匹配失败时正则引擎要把它们全试一遍：
# 实测 "- 12" + 500 个空格 + "x" 单条耗时 230ms，且随长度约立方增长
# （clip_title 只截到 512 字符、**不归一空白**，所以这个上限压不住它）。
# 而这段解析跑在采集主链路上、同步阻塞事件循环：一个这样的 feed 就能把整轮采集拖住。
_EP_LEFT_RE = re.compile(
    r"\s*(?:第\s*\d{1,4}\s*[话話集]|\b(?:EP|Episode)\s*\.?\s*\d{1,3}\b|"
    r"[-–—]\s*\d{1,3}(?:\.\d+)?(?:\s*(?:END|FIN|完结?))?(?:\s*[-–—])?\s*$)", re.I)


def _split_langs(s: str) -> list[str]:
    """把『中文名/罗马音』这类无空格并列的写法拆开（candidate_names 只拆有空格的 ' / '）。

    判据是**每一侧的脚本都纯净**：整段要么只有 CJK、要么只有拉丁。
    【为什么不能只判"两侧语种不同"】那样会把 `Fate/Grand Order -绝对魔兽战线巴比伦尼亚-`
    拆成 `Fate` + 右半段 —— 右半边恰好带中文就判成"多语言并列"了。而拆出来的 `Fate`
    会排在候选第一位，详情页那个『补齐该源』按钮（name_filter=False，不做番名近似）
    就拿它去搜站，把同组的 Fate/Zero、Fate/Apocrypha 等**别的番**的种子按 anime_id 硬挂进来。
    且不可逆：那些 hash 从此被本番占死，真正属于它们的番之后在 hash 去重处静默 return False，
    永远收不到；UI 里的"删除"是改 status 不是删行，hash 永久占用。
    `Fate/Zero`（两侧都是拉丁）以前就被挡住了，漏的是"拉丁名 + 中文右半"这一半——
    而那正是中文字幕组发 Fate 系列时的多数形态。
    """
    parts = [p.strip() for p in s.split("/") if p.strip()]
    if len(parts) < 2:
        return [s]

    def _pure(p: str) -> bool:
        """这一段是不是"纯粹一种脚本"：要么整段没有拉丁字母，要么整段没有 CJK。"""
        return not (_CJK.search(p) and re.search(r"[A-Za-z]", p))

    if (all(_pure(p) for p in parts)
            and any(_CJK.search(p) for p in parts) and any(not _CJK.search(p) for p in parts)):
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
