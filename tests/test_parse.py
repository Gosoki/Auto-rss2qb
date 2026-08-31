"""sources/parse.py 的表驱动回归。

每一条都对应一次真实修过的 bug 或一条明确的设计承诺；新修 bug 时【先在这里加一行】再改代码。
标注 (R1) 的是 2026-08 第 1 轮审计修的，它们修前的实际错误值写在注释里。
"""
import pytest

from sources.parse import (candidate_names, extract_episode, extract_episode_abs, extract_season,
                           is_batch, parse_multibracket, parse_title, quarter_of, search_query_names)
from datetime import datetime


# (标题, 期望 (组, 番名, 季, 集))
TITLES = [
    # ---- 常规写法 ----
    ("[ANi] 药师少女的独语 - 16(88) [1080P][Baha][WEB-DL][AAC AVC][CHT]", ("ANi", "药师少女的独语", 1, 16)),
    ("[Lilith-Raws] 海賊王 - 1170 [Baha][WEB-DL][1080p]", ("Lilith-Raws", "海贼王", 1, 1170)),
    ("[喵萌奶茶屋] 某番 / Some Show - 07 [1080p][简繁日内封]", ("喵萌奶茶屋", "某番", 1, 7)),
    ("[某组] Show S02E07 [1080p]", ("某组", "Show", 2, 7)),
    # (R14) 长番的 4 位集号：SxxEnnnn 曾卡在 \d{1,3} 且【没有尾部守卫】，
    # 于是不是"匹配不上"而是【静默截断】——S01E1174 → 117。真库 anime#99 三条连续新集
    # (1174/1175/1176) 全被截成 117，撞成同一个去重键，flush 每键只放行一份 → 两集永远收不到。
    ("[Nix-Raws] 海贼王 / ワンピース / One Piece S01E1174 [CR WEB-DL 1080p][简繁内封]",
     ("Nix-Raws", "海贼王", 1, 1174)),
    ("[某组] Show S01E1000 [1080p]", ("某组", "Show", 1, 1000)),
    ("[某组] Show S01E999 [1080p]", ("某组", "Show", 1, 999)),
    # 5 位不是合法集号：集号必须【匹配不上】落 -2（而不是再截一次成 1174），
    # 但**番名仍要洗干净**——番名经 alias_key 就是番的身份键。
    # 第一版这条把 'ShowS01E11745' 写成了期望值，等于把一条回归钉成了"正确行为"：
    # 改前番名是干净的 'Show'（集号错成 117），改后集号对了、名字却脏了。
    # 抽取带尾部守卫、剥离不带，两件事就都对了。
    ("[某组] Show S01E11745 [1080p]", ("某组", "Show", 1, -2)),
    # (R14) EPnnnn 同一类缺陷：柯南写作 EP1150 时，旧代码集号落 -2 且番名被污染成 'DetectiveConanEP1150'
    ("[某组] Detective Conan EP1150 [1080p]", ("某组", "DetectiveConan", 1, 1150)),
    ("[某组] 某番 第07话 [1080P]", ("某组", "某番", 1, 7)),
    ("[某组] 某番 第二十三话 [1080P]", ("某组", "某番", 1, 23)),
    ("[某组] 某番 EP07 [1080p]", ("某组", "某番", 1, 7)),
    ("[某组] 某番 [11.5][1080p]", ("某组", "某番", 1, 11.5)),
    ("[某组] 某番 [07v2][1080p]", ("某组", "某番", 1, 7)),
    # 集号后还有一个分隔符才接标签块（旧右界认不出 → 整条落 -2）
    ("[某组] 某番 - 05 - [简日内嵌][AVC 8bit 1080P]", ("某组", "某番", 1, 5)),
    # 括号里的 23-24 是注解不是合集范围，别把整条当合集丢掉
    ("[某组] Show - 24 (最終回 23-24 総集編) [1080p]", ("某组", "Show", 1, 24)),

    # ---- (R1) 括号写法的完结集：修前全是 -2，桜都/北宇治等组每部番都差最后一集 ----
    ("[桜都字幕组] 葬送的芙莉莲 [28END][1080p][简体内嵌]", ("桜都字幕组", "葬送的芙莉莲", 1, 28)),
    ("[北宇治字幕组] 某某番 [24 END][WebRip][1080p][HEVC_AAC]", ("北宇治字幕组", "某某番", 1, 24)),
    ("[某组] 某番 [12完][1080P]", ("某组", "某番", 1, 12)),
    ("[某组] 某番 [13 Fin][1080P]", ("某组", "某番", 1, 13)),
    # ---- (R1) 括号里的四位集号：修前 -2，海贼王/柯南整部进"待识别" ----
    ("[某组] ONE PIECE [1170][1080p][繁日双语]", ("某组", "ONEPIECE", 1, 1170)),
    # ---- 放宽到 4 位后【不能】误吃的两类同形数字 ----
    ("[某组] 某番 [2024][1080p]", ("某组", "某番", 1, -2)),          # 裸年份
    ("[某组] 某番 [1080p][01]", ("某组", "某番", 1, 1)),             # 分辨率带字母，不受影响
    ("[GM-Team][国漫][某番][2023][172][1080P][HEVC]", ("GM-Team", "", 1, 172)),  # 年份在集号前，先撞上

    # ---- (R1) 季号只从 body 抽，不从 raw_title：组名里带 S2 会把该组所有番挪进第 2 季 ----
    ("[Sakurato-S2] 某番 - 03 [1080p]", ("Sakurato-S2", "某番", 1, 3)),
    ("[某组][S2] 某番 - 03 [1080p]", ("某组", "某番", 2, 3)),        # 季号单独成块仍要认出
    ("[某组] 某番 第二季 - 03 [1080p]", ("某组", "某番", 2, 3)),
]


@pytest.mark.parametrize("raw,expected", TITLES, ids=[t[0][:40] for t in TITLES])
def test_parse_title(raw, expected):
    assert parse_title(raw) == expected


# (标题, 期望番名 或 None)
MULTIBRACKET = [
    # (R1) 季度栏目名不是番名：修前三部不同番共用别名 ('4月新番',1) 被并成一部假番
    ("[天使动漫论坛][4月新番][某番A][01][1080P][简繁外挂]", "某番A"),
    ("[某组][10月新番][某番C][05][1080P]", "某番C"),
    ("[某组][四月新番][某番E][05][1080P]", "某番E"),
    ("[某组][招募翻译][某番D][02][1080P]", "某番D"),
    # (R1) 同语种的 '/' 是名字本身，不是多语言分隔：修前 [Fate/Zero] → 'Fate'，整个系列并成一部
    ("[某组][Fate/Zero][05][1080p]", "Fate/Zero"),
    # 真·多语言并列（一侧 CJK 一侧不含）该拆
    ("[悠哈璃羽字幕社][某番 中文名/RomajiName][03][1080p][CHS]", "某番中文名"),
    # (R1) 纯拉丁名的全括号标题（第三档兜底）
    ("[Sakurato][Some Show][01][AVC-8bit 1080p AAC][CHS]", "SomeShow"),
    ("[GM-Team][国漫][某番][2023][172][1080P][HEVC]", "某番"),
    ("[沸羊羊字幕组][某番][01][1080P][简日双语]", "某番"),
    # 信心闸：一个合理候选都没有 → 宁可不猜，落"待识别"
    ("[某组][1080P][x264][CHS]", None),
]


@pytest.mark.parametrize("raw,expected", MULTIBRACKET, ids=[t[0][:40] for t in MULTIBRACKET])
def test_parse_multibracket(raw, expected):
    got = parse_multibracket(raw)
    assert (got[0] if got else None) == expected


def test_multibracket_candidates_are_usable_search_terms():
    """挑出的候选名要能直接拿去种子站搜——带括号/集号的词一条都搜不到。"""
    for raw, _ in MULTIBRACKET:
        for name in search_query_names(raw):
            assert not any(c in name for c in "[]【】★"), (raw, name)
            assert len(name.replace(" ", "")) >= 2, (raw, name)


@pytest.mark.parametrize("title,batch", [
    ("[某组] 某番 01-12 合集 [1080P]", True),
    ("[某组] 某番 [01-12][1080P]", True),
    ("[天月搬运组] 某番 - 07 [1080P]", False),   # 组名带"搬运"不等于这条是合集
    ("[某组] Show - 24 (最終回 23-24 総集編) [1080p]", False),  # 能抽出单集集号就不当合集
])
def test_is_batch(title, batch):
    assert is_batch(title) is batch


def test_episode_abs_only_for_double_numbering():
    assert extract_episode_abs("[ANi] 某番 - 16(88) [1080P]") == 88
    assert extract_episode_abs("[ANi] 某番 - 16 [1080P]") is None


@pytest.mark.parametrize("dt,q", [
    (datetime(2026, 1, 15), "26A"), (datetime(2026, 2, 28), "26A"),
    (datetime(2026, 4, 1), "26B"), (datetime(2026, 7, 31), "26C"),
    (datetime(2026, 10, 5), "26D"), (datetime(2026, 11, 30), "26D"),
    # 【故意如此，别"修"它】冬季档跨年（12/1/2 同属一档），12 月首播归【次年】的冬档。
    # 这是 quarter_of 与 db/models.py 都写明的口径；改成按自然年会改变已下片的目标目录。
    (datetime(2026, 12, 1), "27A"), (datetime(2026, 12, 31), "27A"),
])
def test_quarter_of(dt, q):
    assert quarter_of(dt) == q


def test_no_title_yields_no_fake_identity():
    """解析不出番名时必须给空串——绝不能返回一个会被当成别名键的垃圾串。"""
    for raw in ("", "   ", "[某组]", "[某组][1080P]"):
        group, name, season, ep = parse_title(raw)
        assert name == "" or len(name) >= 2, raw


@pytest.mark.parametrize("kw,raw,hit", [
    ("1080p", "[组] 番 - 01 [1080P][繁日]", True),    # (R2) 大小写不敏感：源组过滤曾是敏感的，
    ("1080P", "[组] 番 - 01 [1080p][繁日]", True),    # 用户填小写而组写大写 → 该源每轮全灭，日志只有"0 条"
    ("繁日", "[组] 番 - 01 [1080p][繁日]", True),
    ("简日", "[组] 番 - 01 [1080p][繁日]", False),
    ("", "任何标题", True),                           # 空关键词 = 不过滤
    ("x", "", False),
])
def test_kw_match_is_case_insensitive(kw, raw, hit):
    """源组的 title_filter 与单番的 pref_keyword 必须共用这一个判据——
    两处口径相反是最容易踩的坑（设置页对 pref_keyword 的示例文案用的就是小写 1080p）。"""
    from sources.parse import kw_match
    assert kw_match(kw, raw) is hit


def test_subgroup_whitelist_is_case_insensitive():
    """(R5) 字幕组白名单曾是大小写敏感的裸 `in`，而紧邻它的标题关键词早已不敏感。
    用户填 lolihouse 而站上写 LoliHouse → 该源组每轮全灭，日志只有一行"0 条"，没有任何指向。
    这与 R2 判为 P1 的 title_filter 是逐字相同的失效模式，所以两处必须共用同一个判据。"""
    from sources.parse import kw_match
    assert kw_match("lolihouse", "LoliHouse")
    assert kw_match("LoliHouse", "lolihouse")
    assert not kw_match("nekomoe", "LoliHouse")


@pytest.mark.parametrize("name", [
    "Fate/Grand Order -绝对魔兽战线巴比伦尼亚-",
    "Fate/kaleid liner 魔法少女伊莉雅",
    "Fate/strange Fake 伪典",
    "Fate/Zero",
])
def test_slash_inside_a_title_is_not_a_language_separator(name):
    """斜杠属于番名本身时不许拆。

    只判「两侧语种不同」是不够的：`Fate/Grand Order -绝对魔兽战线巴比伦尼亚-` 的右半边
    恰好带中文，于是被当成"中文名/罗马音"拆出一个 `Fate`。而 `Fate` 会排在候选第一位，
    详情页『补齐该源』(name_filter=False，不做番名近似) 拿它去搜站，
    把同组 Fate/Zero、Fate/Apocrypha 等**别的番**的种子按 anime_id 硬挂进来——
    而且不可逆：那些 hash 从此被本番占死，真正属于它们的番永远收不到。
    判据要改成「每一侧的脚本都纯净」。
    """
    from sources.parse import _split_langs
    assert _split_langs(name) == [name]


@pytest.mark.parametrize("name,want", [
    ("进击的巨人/Shingeki no Kyojin", ["进击的巨人", "Shingeki no Kyojin"]),
    ("孤独摇滚/Bocchi the Rock!", ["孤独摇滚", "Bocchi the Rock!"]),
    ("转生史莱姆/Tensei Shitara Slime Datta Ken", ["转生史莱姆", "Tensei Shitara Slime Datta Ken"]),
    ("Steins;Gate/命运石之门", ["Steins;Gate", "命运石之门"]),   # 两侧脚本都纯净＝真并列
])
def test_real_bilingual_pairs_are_still_split(name, want):
    """收紧判据不能误伤真正的『中文名/罗马音』并列——那是这个函数存在的全部理由。"""
    from sources.parse import _split_langs
    assert _split_langs(name) == want


@pytest.mark.parametrize("raw,want_name,want_ep", [
    ("[喵萌奶茶屋&LoliHouse] 【我推的孩子】 / Oshi no Ko - 11 [WebRip 1080p].mkv", "我推的孩子", 11),
    ("[NC-Raws] 【我推的孩子】 - 11 (B-Global 1920x1080).mp4", "我推的孩子", 11),
    ("[Skymoon-Raws] 【推しの子】 - 05 [WebRip][1080p]", "推しの子", 5),
])
def test_title_wrapped_in_brackets_is_not_eaten_as_a_tag(raw, want_name, want_ep):
    """番名本身就写成【…】的番不能被当成标签块删掉。

    `_TAG_BLK_RE` 会把 `【我推的孩子】` 整块删掉 → 番名洗成空串 → sources/base.py
    对空番名是【静默丢弃整条】：那部番从某几个组那里一集都收不到，界面和日志零信号
    （实测：日志一条记录都没有，连计数都不出现）。
    """
    from sources.parse import parse_title
    _, name, _, ep = parse_title(raw)
    assert name == want_name and ep == want_ep


@pytest.mark.parametrize("raw", [
    "[组][1080P][x264][CHS]",
    "[组] [1080p] - 12 [x265]",
])
def test_tag_only_titles_still_yield_no_name(raw):
    """解包回退不能反过来把画质标签认成番名——那会建出一部叫 '1080p' 的番。"""
    from sources.parse import parse_title
    assert parse_title(raw)[1] == ""


def test_episode_regex_has_no_catastrophic_backtracking():
    """标题解析不能被一条畸形标题拖住事件循环。

    `_EP_LEFT_RE` 的尾部曾是 `\\s*(?:END|FIN|完结?)?\\s*[-–—]?\\s*$` —— 三段 \\s* 中间夹两个
    可选组，一段空白能被它们以指数级多种方式瓜分；整体匹配失败时引擎要全试一遍。
    实测 "- 12" + 500 空格 + "x" 单条 230ms，而 clip_title 只截长度、**不归一空白**。
    这段解析跑在采集主链路上、同步阻塞事件循环：一个这样的 feed 就能把整轮采集拖住。
    """
    import time

    from sources.parse import clip_title, search_query_names

    # 【长度要选在 clip_title 截完之后仍带着结尾那个 'x' 的档】600 个空格会被
    # MAX_TITLE_LEN=512 把尾部的 'x' 正好截掉，于是新旧两版正则都是 0ms —— 守卫恒真。
    # 180 个空格截完仍是 192 字符、'x' 还在：实测旧写法 38ms、新写法 0.9ms。
    evil = clip_title("[组] 某番 - 12" + " " * 180 + "x")
    assert evil.endswith("x"), "用例前提不成立：evil 串的触发尾巴被 clip_title 截掉了"
    t0 = time.perf_counter()
    search_query_names(evil)
    cost = time.perf_counter() - t0
    assert cost < 0.005, f"畸形标题解析耗时 {cost * 1000:.1f}ms，正则有回溯问题"


@pytest.mark.parametrize("raw,want", [
    ("[组] 某番 - 12", "某番"),
    ("[组] 某番 - 12 END", "某番"),
    ("[组] 某番 - 12.5", "某番"),
    ("[组] 某番 第03话", "某番"),
    ("[组] 某番 EP03", "某番"),
    ("[组] 某番 - 12 完", "某番"),
])
def test_episode_suffix_stripping_is_unchanged(raw, want):
    """消除回溯不能改变语义——这几种是真实字幕组的集号写法。"""
    from sources.parse import search_query_names
    assert want in " ".join(search_query_names(raw))


@pytest.mark.parametrize("trad,simp", [
    ("簡繁內封", "简繁内封"), ("簡日雙語", "简日双语"), ("簡體", "简体"),
    ("繁體", "繁体"), ("簡中", "简中"),
])
def test_language_blocks_are_recognised_in_both_scripts(trad, simp):
    """语言块的简繁两种写法必须同判——字类里曾有「繁」而漏了「簡」。

    逃过跳过闸的后果不是"少跳一个块"：那个块会被当成番名（parse_multibracket 挑名、
    _clean_name 的解包回退都会），于是建出一部叫「簡繁內封」的假番，
    而该组当季所有番共用这一个别名、被并成同一部——落进同一个目录，集号还互相撞成 skipped。
    """
    from sources.parse import _is_skip_block
    assert _is_skip_block(trad) == _is_skip_block(simp) is True


@pytest.mark.parametrize("name", ["簡単な生活", "日常", "中二病", "英雄王", "台风"])
def test_real_titles_starting_with_a_language_char_are_not_skipped(name):
    """收进「簡」不能误伤以语言字开头的真番名。"""
    from sources.parse import _is_skip_block
    assert _is_skip_block(name) is False


@pytest.mark.parametrize("raw,want_name,want_season", [
    ("[喵萌奶茶屋] 【我推的孩子】第二季 - 03 [1080p]", "我推的孩子", 2),
    ("[喵萌奶茶屋] 【我推的孩子】第2季 - 03 [1080p]", "我推的孩子", 2),
    ("[组] 【葬送的芙莉莲】第三期 - 03 [1080p]", "葬送的芙莉莲", 3),
])
def test_bracket_title_with_a_season_suffix_still_parses(raw, want_name, want_season):
    """`【番名】第二季` 不能被静默丢弃。

    解包回退的判空时机曾在 `strip_season` 【之前】：洗完剩下 `第二季`（非空）→ 回退不触发，
    到调用方那里才被 strip_season 变成空 → 整条种子静默丢弃。
    """
    from sources.parse import parse_title
    _, name, season, _ = parse_title(raw)
    assert (name, season) == (want_name, want_season)


@pytest.mark.parametrize("raw", [
    "[喵萌奶茶屋] 【我推的孩子】 S2 - 03 [1080p]",
    "[组] 【葬送的芙莉莲】 Season 2 - 03 [1080p]",
])
def test_bracket_title_never_yields_a_season_marker_as_the_name(raw):
    """`【番名】 S2` 不能解析出一个叫 `S2` 的番名。

    strip_season 只认中文季名，英文写法它不动 → 洗完剩下 `S2`（非空）→ 回退不触发 →
    番名就是 `S2`：该组所有这么写的【】番全并进一部叫 S2 的假番里，
    落进同一目录、集号互撞、info_hash 被占死。
    """
    from sources.parse import parse_title
    name = parse_title(raw)[1]
    assert name not in ("S2", "Season2", "2ndSeason") and len(name) > 3, f"番名成了季号：{name!r}"


@pytest.mark.parametrize("raw,leaked", [
    ("[某组] 【我推的孩子】Ⅱ - 03 [1080p]", "Ⅱ"),
    ("[某组] 【咒术回战】 完结 - 24", "完结"),
    ("[某组] 【某番】 修正版 - 03", "修正版"),
    ("[某组] 【我推的孩子】 2期 - 03", "2期"),
    ("[组] 【葬送的芙莉莲】 Season 2 - 03 [1080p]", "Season2"),
])
def test_leftover_fragments_never_become_the_anime_name(raw, leaked):
    """季号/完结/版本这类残渣不能单独成为番名。

    本模块早有一份「这不是番名」的词表，但它以前只在解包回退里被调用、主路径从不问它——
    于是同一个词写在括号里判成标签、写在括号外就成了番名：
    `【咒术回战】 完结 - 24` 解析出的番名是 `完结`，该组所有这么发的番共用一个别名、
    被并成同一部（而 `Ⅱ` 只有一个字符，还会被 candidate_names 的长度闸滤掉，bgm 永远救不回来）。
    """
    from sources.parse import parse_title
    name = parse_title(raw)[1]
    assert name != leaked, f"残渣 {leaked!r} 成了番名"
    assert len(name) > 2, f"番名退化成 {name!r}"


@pytest.mark.parametrize("raw,want", [
    ("[组] 劇場版 少女☆歌劇 - 01", "剧场版少女☆歌剧"),
    ("[ANi] 葬送的芙莉莲 - 12 [1080P]", "葬送的芙莉莲"),
    ("[组] IS 无限斯特拉托斯 - 05", "IS无限斯特拉托斯"),
])
def test_widening_the_skip_table_does_not_eat_real_titles(raw, want):
    """收紧判据不能误伤真番名——「剧场版 XX」整段是名字，不是标签。"""
    from sources.parse import parse_title
    assert parse_title(raw)[1] == want


def test_year_prefixed_season_block_is_a_tag_not_a_name():
    """`2024年10月新番` 要和裸 `10月新番` 一样被认成标签。

    月份分支从行首锚定，年份一加就落空——而这类标题正是「番名写在【】里」那条修法
    明确交给 parse_multibracket 的，交过去之后它会犯同样的错。
    """
    from sources.parse import _is_skip_block, parse_multibracket
    assert _is_skip_block("2024年10月新番") is True
    assert parse_multibracket("[X][2024年10月新番][青之箱][05][1080P]")[0] == "青之箱"


@pytest.mark.parametrize("raw,ep", [
    ("[Nix-Raws] 海贼王 / One Piece S01E1174 [CR WEB-DL 1080p]", 1174),
    ("[Nix-Raws] 海贼王 / One Piece S01E1175 [CR WEB-DL 1080p]", 1175),
    ("[Nix-Raws] 海贼王 / One Piece S01E1176 [CR WEB-DL 1080p]", 1176),
])
def test_four_digit_episodes_do_not_collapse(raw, ep):
    """连续的 4 位集号必须解析成【互不相同】的集号。

    这是 R14 的核心失效形状，比"某一集号错了"严重：集去重键是 (anime_id, episode)，
    三集塌成同一个键之后 flush 每键只放行一份，另外两集不是"下错"而是【永远收不到】，
    且详情页把它们标成蓝灰「备用项」、tooltip 写「同集已有更优版本」——用户看到的是一句谎话。
    真库 anime#99 的种子 1150/1388/1617 就是这样。
    """
    assert parse_title(raw)[3] == ep


def test_four_digit_episode_names_stay_clean():
    r"""抽取放宽了，【剥离】也必须同步放宽——否则番名会剩个尾巴。

    S01E1174 若只放宽抽取、剥离仍是 \d{1,3}，番名会被剥成 'OnePiece4'（剥掉 S01E117 留下 4）。
    而番名是别名键(alias_key)，脏一个字符就等于给同一部番造了个新身份、裂成两部。
    """
    assert parse_title("[ANi] One Piece S01E1174 [1080p]")[1] == "OnePiece"
    assert parse_title("[XX] Detective Conan EP1150 [1080p]")[1] == "DetectiveConan"


# ---------------- (R14) 两位年的世纪基准 ----------------

@pytest.mark.parametrize("q,year", [
    ("26C", 2026), ("25D", 2025), ("00A", 2000), ("39D", 2039),
    ("40A", 1940), ("99D", 1999), ("63B", 1963),
    ("", None), (None, None), ("ZZ", None), ("2026C", None),
])
def test_quarter_year_resolves_century(q, year):
    """季度键只存两位年，世纪要靠基准年还原。修前一律当 20xx，'99D' → 2099。"""
    from sources.parse import quarter_year
    assert quarter_year(q) == year


def test_1999_sorts_last_not_first():
    """这是本次修复要挡住的具体症状。

    番剧页按季度分组、`sorted(reverse=True)` 后让【第一组默认展开】。季度键是两位年，
    字符串比较下 '99D' > '26C'，于是打开首页第一眼看到的是 1999 年那部长番、
    当季那组折叠在它下面。真库 anime#99（海贼王，bgm 首播 1999-10-20）实测过。
    """
    from sources.parse import quarter_sort_key
    qs = ["16B", "19A", "25C", "25D", "26A", "26B", "26C", "99D"]
    got = sorted(qs, key=quarter_sort_key, reverse=True)
    assert got[0] == "26C", f"当季应排第一，实际是 {got[0]}"
    assert got[-1] == "99D", f"1999 应排最后，实际是 {got[-1]}"


def test_quarter_display_uses_real_century():
    """{yyyy} 是设置页季度模板下拉的第一项，用户一点即中——不能显示成 2099 年。"""
    from sources.parse import format_quarter
    assert format_quarter("99D", "{yyyy}年{season}") == "1999年秋"
    assert format_quarter("26C", "{yyyy}年{season}") == "2026年夏"
    # 键的格式没变：番剧默认模板是 {yy}{q}，渲染结果与改动前逐字相同
    assert format_quarter("99D", "{yy}{q}") == "99D"
    # 【但"已归档目录不受影响"这句只对番剧成立，别再写成无条件结论】
    # 剧场版的 MOVIE_QUARTER_FMT 默认就是 {yyyy}（config.py），而 core/movies 每次下载都重算
    # save_path 且没有自动 relocate。所以世纪基准这一改，2000 年前的片归档目录会从
    # 2099 变成 1999 —— 方向是【变对】（2099 本来就是错的），但若真有片已经下在 2099 下面，
    # 新集会去 1999、旧文件留在 2099。真库实测：剧场版季度全在 22D–26C，
    # 且 movietorrent 的 save_path 全为空（一条都没下过），实际影响面为零。
    assert format_quarter("99D", "{yyyy}") == "1999"
    assert format_quarter("26C", "{yyyy}") == "2026"


def test_engine_quarter_year_is_the_same_implementation():
    """core.engine.quarter_year 必须与 sources.parse 的同源。

    它的 docstring 自称『只此一份』，而 format_quarter 的 {yyyy} 另写了一份 f"20{yy}"——
    两份共享同一个世纪错，且将来只会修一处。这条用例把"同源"钉住。
    """
    from core import engine
    from sources.parse import quarter_year
    for q in ("99D", "26C", "00A", "40A", "", "ZZ"):
        assert engine.quarter_year(q) == quarter_year(q)


def test_prev_quarter_crosses_year_2000():
    """'00A'(2000冬) 的上一季是 '99D'(1999秋)。修前拼出 '-1D'，谁都解析不出。"""
    from core import engine
    assert engine.prev_quarter("00A") == "99D"
    assert engine.prev_quarter("26A") == "25D"
    assert engine.prev_quarter("26C") == "26B"


@pytest.mark.parametrize("raw,ep", [
    # (R15 回归) EP 从 3 位放宽到 4 位时，漏了括号写法那条早就有的「年份/分辨率」守卫。
    # 3 位时代这些都匹配不上（\d{1,3} 后面跟数字被 (?!\d) 否掉）→ 落 -2『未知集』，
    # 而 auto_downloadable_ep 拒绝自动下 -2；放宽之后它们变成了「第 1080 集」「第 2023 集」，
    # 也就是放宽反而制造了一条【会自动下错东西】的路。
    ("[Sub] Show EP.1080p [x264]", -2),
    ("[Sub] Show EP 1080p", -2),
    ("[Sub] Show EP.2023 [BD]", -2),
    ("[Sub] Show EP1440p", -2),
    ("[Sub] Show EP 2160p", -2),
    # 真集号照常认出，别把守卫扩太宽
    ("[Sub] Show EP1174 [1080p]", 1174),
    ("[Sub] Show EP999", 999),
    ("[Sub] Show EP07 [1080p]", 7),
])
def test_ep_pattern_excludes_years_and_resolutions(raw, ep):
    assert parse_title(raw)[3] == ep


def test_five_digit_episode_gets_unknown_but_a_clean_name():
    """抽取与剥离【有意不同】：抽取宁可认不出，剥离必须洗干净。

    S01E11745 的集号该落 -2（5 位不是合法集号），但番名必须仍是 'Show' ——
    番名经 alias_key 就是番的身份键，脏一个字符等于给同一部番造了个新身份。
    第一版把带守卫的剥离正则一起上了，番名变成 'ShowS01E11745'，
    而且这条回归被写进用例当成了期望值。
    """
    group, name, season, ep = parse_title("[Sub] Show S01E11745 [1080p]")
    assert ep == -2, "5 位不是合法集号"
    assert name == "Show", f"番名没洗干净：{name!r}"


@pytest.mark.parametrize("raw,want", [
    # (R15) 双编号锚点是跨源集号归一的【唯一】入口，卡在 3 位就等于千集长番永远学不到 ep_offset
    ("[LoliHouse] 药师少女 - 16(88) [1080p]", 88),
    ("[LoliHouse] ONE PIECE - 16(1088) [1080p]", 1088),
    ("[LoliHouse] Show - 3(1191) [1080p]", 1191),
    # 年份括号必须挡住：3 位时代它天然匹配不上，放宽到 4 位之后不加守卫就会推出 offset=2020
    ("[组] Show - 04(2024) [1080p]", None),
    ("[组] Show - 04(1999) [1080p]", None),
    ("[组] Show - 04(2026) [1080p]", None),
    # 碟片编号/相等：绝对号必须严格大于季内号
    ("[组] Show - 12(3) [1080p]", None),
    ("[组] Show - 16(16) [1080p]", None),
])
def test_dual_numbering_supports_long_running_shows(raw, want):
    assert extract_episode_abs(raw) == want


# ---------------- (R16) 四位数字的两类同形物：三处守卫【有意不同】 ----------------

@pytest.mark.parametrize("raw,abs_", [
    # 双编号：猜错的代价【不可逆】（ep_offset 只学一次、全项目没有重置入口）→ 年份与分辨率都排
    ("[组] Show - 16(1080) [x264]", None),
    ("[组] Show - 16(1440) [x264]", None),
    ("[组] Show - 05 (2160) [x264]", None),
    ("[组] Show - 16(2024) [x264]", None),
    ("[组] Show - 16(1999) [x264]", None),
    # 真的绝对集号照常学得到
    ("[LoliHouse] ONE PIECE - 16(1088) [1080p]", 1088),
    ("[LoliHouse] Show - 3(1191) [1080p]", 1191),
])
def test_dual_numbering_blocks_resolutions_too(raw, abs_):
    """双编号锚点排年份【也排分辨率】。

    同文件另两条同族正则（括号写法、EP 兜底）当初都挡了 1080/1440/2160，唯独这条只挡年份 ——
    于是 '- 16(1080)' 会把 ep_offset 学成 1064，而 _learn_and_normalize_episode 只在
    ep_offset 为空时学一次、全项目没有任何入口能重置它，跨源集号归一从此对这部番永久失效。
    """
    assert extract_episode_abs(raw) == abs_


@pytest.mark.parametrize("raw,name,ep", [
    # EP 兜底【只排年份、不排分辨率】：没人用 EP 写分辨率，1080 几乎必是集号（海贼王真播过第 1080 集）
    ("[ToonsHub] One Piece EP1080 1080p CR WEB-DL", "OnePiece", 1080),
    ("[ToonsHub] One Piece EP1174 1080p CR WEB-DL", "OnePiece", 1174),
    # 带单位的分辨率由尾部 (?![\dpPiI]) 挡住，不靠年份/分辨率守卫
    ("[Sub] Show EP.1080p [x264]", "ShowEP.1080p", -2),
    # 年份那一半保留：EP.2023 确实需要它。番名仍要洗干净（剥离侧不带守卫）
    ("[Sub] Show EP.2023 [BD]", "Show", -2),
])
def test_ep_fallback_only_excludes_years(raw, name, ep):
    """抽取排年份、剥离一概洗干净 —— 认不出集号 ≠ 名字要留着它。

    带分辨率守卫时 EP1080 既认不出集号、番名也不剥，变成 'OnePieceEP10801080pCRWEB-DL'，
    而番名经 alias_key 就是番的身份键：同一部番的 EP1174 进「OnePiece」、EP1080 另建一条垃圾番。
    """
    g, got_name, season, got_ep = parse_title(raw)
    assert (got_name, got_ep) == (name, ep)


@pytest.mark.parametrize("raw,ep", [
    ("[组][番名][1170][1080P][简日]", 1170),   # 括号写法：年份+分辨率都排，四位集号照常认
    ("[组][番名][2023][172][1080P]", 172),
    ("[组][番名][1080][简日]", -2),
])
def test_bracket_form_still_excludes_both(raw, ep):
    """括号写法维持原判据：裸写在方括号里的 1080 几乎必是分辨率。"""
    assert parse_title(raw)[3] == ep
