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
