"""订阅源：两个站共用一份解析逻辑。

【背景更正】前几轮把 sources/ 当成"插件系统"来论证，那是理解错了——本项目**没有插件系统**，
这就是两个普通模块。合并它们的唯一理由是重复【已经在产生缺陷】：
源层的改动有 9 次是"同一件事改两遍"，最近一次（字幕组白名单改成大小写不敏感）只改了一半。

这一组用例守的就是"两个源的行为必须一致"——它是那份重复的替代品。
"""
import feedparser
import pytest

from sources import SEARCH_URL, SOURCES
from sources.base import RssSource
from sources.mikan import MikanSource
from sources.nyaa import NyaaSource

NYAA_XML = """<?xml version="1.0"?><rss version="2.0" xmlns:nyaa="https://nyaa.si/xmlns/nyaa">
<channel><item>
<title>{title}</title><link>https://nyaa.si/download/1.torrent</link>
<pubDate>Sun, 03 Aug 2026 12:00:00 -0000</pubDate>
<nyaa:infoHash>{h}</nyaa:infoHash></item></channel></rss>"""

MIKAN_XML = """<?xml version="1.0"?><rss version="2.0"><channel><item>
<title>{title}</title><link>https://mikanani.me/Home/Episode/{h}</link>
<enclosure url="https://mikanani.me/Download/x.torrent" type="application/x-bittorrent"/>
<pubDate>Sun, 03 Aug 2026 12:00:00 -0000</pubDate></item></channel></rss>"""


def _parse_one(cls, xml, title, h="a" * 40, **kw):
    src = cls("测试", "http://x/", **kw)
    feed = feedparser.parse(xml.format(title=title, h=h))
    return src._parse(feed.entries[0])


BOTH = [(NyaaSource, NYAA_XML), (MikanSource, MIKAN_XML)]


# ---------------- 两个源必须行为一致 ----------------

@pytest.mark.parametrize("cls,xml", BOTH, ids=["nyaa", "mikan"])
def test_basic_parse(cls, xml):
    it = _parse_one(cls, xml, "[ANi] 某番 - 07 [1080P][Baha][WEB-DL][CHT]")
    assert it is not None
    assert (it.anime_title, it.season, it.episode, it.source) == ("某番", 1, 7, "ANi")
    assert it.site == cls.site and it.info_hash == "a" * 40
    assert it.download_url


@pytest.mark.parametrize("cls,xml", BOTH, ids=["nyaa", "mikan"])
def test_subgroup_whitelist_is_case_insensitive_on_both(cls, xml):
    """(R5/S-01) 这条曾经只在一个源上被修好。用户填 lolihouse 而站上写 LoliHouse 时，
    另一个源组每轮全灭，日志只有一行"0 条"，没有任何指向。"""
    t = "[LoliHouse] 某番 - 07 [1080p]"
    assert _parse_one(cls, xml, t, subgroups=["lolihouse"]) is not None
    assert _parse_one(cls, xml, t, subgroups=["LOLIHOUSE"]) is not None
    assert _parse_one(cls, xml, t, subgroups=["nekomoe"]) is None


@pytest.mark.parametrize("cls,xml", BOTH, ids=["nyaa", "mikan"])
def test_title_filter_is_case_insensitive_on_both(cls, xml):
    t = "[组] 某番 - 07 [1080P][繁日]"
    assert _parse_one(cls, xml, t, title_filter=["1080p"]) is not None
    assert _parse_one(cls, xml, t, title_filter=["简日"]) is None


@pytest.mark.parametrize("cls,xml", BOTH, ids=["nyaa", "mikan"])
def test_bad_hash_is_refused_on_both(cls, xml):
    """40 位 hex 是跨源去重键的形状，也挡住脏 hash 注入 qB 的 '|' 分隔符。"""
    for bad in ("", "xyz", "A" * 39, "g" * 40):
        assert _parse_one(cls, xml, "[组] 某番 - 07 [1080p]", h=bad) is None


@pytest.mark.parametrize("cls,xml", BOTH, ids=["nyaa", "mikan"])
def test_batch_posts_are_skipped_on_both(cls, xml):
    assert _parse_one(cls, xml, "[组] 某番 01-12 合集 [1080p]") is None


@pytest.mark.parametrize("cls,xml", BOTH, ids=["nyaa", "mikan"])
def test_empty_name_is_skipped_on_both(cls, xml):
    """番名解析为空 → 无法定位/去重，跳过免撞库。
    【两个源的判定顺序曾经不同】（一个在白名单前、一个在后），合并后自然一致。"""
    assert _parse_one(cls, xml, "[组][1080P][x264][CHS]") is None


@pytest.mark.parametrize("cls,xml", BOTH, ids=["nyaa", "mikan"])
def test_broken_entry_does_not_raise(cls, xml):
    """一条坏条目不该掀翻整轮采集。"""
    src = cls("测试", "http://x/")
    assert src._parse(object()) is None


# ---------------- 站点表 ----------------

def test_sources_table_matches_classes():
    """SOURCES 是"加一个源要改的唯一一处"。键必须与类自己声明的 site 一致，
    否则 engine 按 site 查时区、pages 按 site 建下拉都会错位。"""
    for site, cls in SOURCES.items():
        assert cls.site == site, f"{cls.__name__}.site={cls.site!r} 与表键 {site!r} 不符"
        assert issubclass(cls, RssSource)


def test_every_source_declares_a_timezone():
    """(时区基准) 这条知识以前存在 engine 的一张字典里——那是全项目唯一一处
    "新增源时漏改【不会报错】、只是发布时间整天错"的地方。现在它是类属性，缺了就是 None。"""
    for cls in SOURCES.values():
        assert cls.TZ is not None, f"{cls.__name__} 没声明 TZ"


def test_engine_reads_timezone_from_the_source():
    from core.engine import _site_tz
    assert _site_tz("nyaa") == NyaaSource.TZ
    assert _site_tz("mikan") == MikanSource.TZ
    assert _site_tz("不存在的站") is None
    assert _site_tz("") is None


def test_search_urls_are_optional_but_valid():
    """补齐用的搜索入口不是每个源都必须有——缺一行会被跳过并记日志，
    而不是像以前那样走进一个静默的 `return []`。"""
    for site, fn in SEARCH_URL.items():
        assert site in SOURCES
        url = fn("某番")
        assert url.startswith("http")


def test_ui_dropdown_comes_from_the_table():
    from pages.sources import SITE_OPTS
    assert set(SITE_OPTS) == set(SOURCES)


# ---------------- 兄弟路径（R7 的核心教训） ----------------

def test_missing_tz_is_not_silently_defaulted():
    """(R7/N-12) 把时区从 engine 的字典搬到类属性，是为了消灭"漏改不报错"这个失效形状。
    可若基类给一个 UTC 默认值，漏写 TZ 的源会静静地按 UTC 显示——形状原样搬了过来。
    默认 None + 这条用例，才真的把它消掉。"""
    assert RssSource.TZ is None, "基类不该给时区默认值"
    for cls in SOURCES.values():
        assert cls.TZ is not None, f"{cls.__name__} 漏了 TZ"


def test_missing_download_url_is_logged(caplog):
    """(R7/N-10) 合并前 nyaa 缺 <link> 会 AttributeError 进兜底、记一条 ERROR；
    改成 .get() 之后变成完全静默丢弃——站点改版把下载地址挪走时，
    表现是"这个源突然只收到一半条目"而日志一个字都没有。"""
    import logging
    xml = NYAA_XML.replace("<link>https://nyaa.si/download/1.torrent</link>", "")
    with caplog.at_level(logging.WARNING, logger="autorss"):
        assert _parse_one(NyaaSource, xml, "[组] 某番 - 07 [1080p]") is None
    assert any("缺下载地址" in r.message for r in caplog.records), "静默丢弃是不行的"


def test_movie_rss_path_shares_the_hardened_bits():
    """(R7/N-08/09) `mikan.fetch_bangumi_torrents` 是本项目第三条 RSS 解析路径（服务于剧场版）。
    它因为语义不同（不过滤合集、不查白名单、允许 -1/-2 集号）没有被合并进 RssSource ——
    那没问题，但合并时修好的几件通用的事必须手动同步过去，否则就是新的"改一半"。"""
    import inspect

    from sources import mikan
    src = inspect.getsource(mikan.fetch_bangumi_torrents)
    assert "bozo" in src, "缺 feed.bozo 告警：feed 坏掉时表现同样是 0 条，但没人知道为什么"
    assert "repr(entry)" in src, "兜底里直接 entry.get(...) 的话，处理器自己会抛"
    assert mikan._HEX40_RE is __import__("sources.base", fromlist=["x"])._HEX40_RE, \
        "hash 校验正则应复用基类那一份，别各留一个拷贝"


_ALL_BRACKET = [
    "[诸神字幕组][2024年10月新番][青之箱][05][1080P][简日双语]",
    "[某组][V2][孤独摇滚][05][1080P][简体]",
    "[Group][TVrip][某部番][05][1080P]",
    "[天使动漫论坛][www.tsdm39.com][10月新番][败犬女主太多了][01][1080P]",
    "[千夏字幕组][赛马娘 Season 3_Uma Musume S3][03][1080p]",
    "[FLsnow][おねがいアイプリ/Onegai_Aipri][1080P][20][CHS/CHT&JPN]",
]


@pytest.mark.parametrize("cls,xml", BOTH, ids=["nyaa", "mikan"])
@pytest.mark.parametrize("raw", _ALL_BRACKET)
def test_all_bracket_titles_are_left_to_the_multibracket_switch(cls, xml, raw, monkeypatch):
    """全括号命名的标题在开关关着时必须被丢弃，**不能**猜出一个名字。

    `_clean_name` 的解包回退是为「番名本身写成【…】」这一种形状加的（见 test_parse.py）。
    第一版只判了"首块不是标签"，于是任何全括号标题的第一个块都被当成番名：
    `[诸神字幕组][2024年10月新番][青之箱][05]` → 番名 `2024年10月新番`。
    后果不是"名字难看"——该组当季所有番共用这一个别名、被并成同一部：落进同一个目录，
    集去重键 (anime_id, episode) 让不同番的同号集互相撞成 skipped，且 info_hash 被占死【不可逆】。
    而且它**架空了 ANIME_MULTIBRACKET_PARSE 开关**（默认关，因为猜名可能猜错）。

    走源的真实入口断言：现有 MULTIBRACKET 夹具直接调 parse_multibracket，
    而这些标题今天根本走不到那个函数——用例照样全绿。
    """
    import config
    monkeypatch.setitem(config._v, "ANIME_MULTIBRACKET_PARSE", False)
    assert _parse_one(cls, xml, raw) is None, "全括号标题在开关关着时不该猜出番名"


@pytest.mark.parametrize("cls,xml", BOTH, ids=["nyaa", "mikan"])
@pytest.mark.parametrize("raw,want", [
    ("[喵萌奶茶屋&LoliHouse] 【我推的孩子】 / Oshi no Ko - 11 [WebRip 1080p].mkv", "我推的孩子"),
    ("[NC-Raws] 【我推的孩子】 - 11 (B-Global 1920x1080).mp4", "我推的孩子"),
    ("[Skymoon-Raws] 【推しの子】 - 05 [WebRip][1080p]", "推しの子"),
])
def test_bracket_wrapped_title_still_parses_with_the_switch_off(cls, xml, raw, want, monkeypatch):
    """而「番名本身写成【…】」的必须照常解析——开关关着也一样（那不是猜名，那个块就是番名）。"""
    import config
    monkeypatch.setitem(config._v, "ANIME_MULTIBRACKET_PARSE", False)
    item = _parse_one(cls, xml, raw)
    assert item is not None and item.anime_title == want


@pytest.mark.parametrize("cls,xml", BOTH, ids=["nyaa", "mikan"])
@pytest.mark.parametrize("raw,want_in_names", [
    ("[北宇治字幕组] 【咒术回战】 第二季 - 24 [1080p]", "咒术回战"),
    ("[喵萌奶茶屋] 【我推的孩子】 S2 - 03 [1080p]", "我推的孩子"),
    ("[组] 【葬送的芙莉莲】第二季 - 03 [1080p]", "葬送的芙莉莲"),
    ("[NC-Raws] 【我推的孩子】 - 11 (B-Global 1920x1080).mp4", "我推的孩子"),
])
def test_search_names_carry_the_real_title_not_just_the_season(cls, xml, raw, want_in_names):
    """**search_names 里必须有番名**，不能只剩 `第二季` / `S2`。

    `ParsedItem.search_names` 是 `enrich.resolve` 的唯一入参。番名解析对了而搜索词塌成
    `['第二季']` 时，两部不同的「第二季」会搜到同一个 bgm 条目 → 被合并成一部番，
    落进同一目录、集号互撞、info_hash 被占死，且 `retry_unmatched` 只捞 bangumi_id 为空的，
    绑错的永远不会被重识别。

    【这条用例存在的直接原因】全仓 tests/ 里 `item.search_names` 一次都没被断言过——
    第 11 轮修好了 anime_title、把 search_names 弄坏了，全套用例照样全绿。
    """
    item = _parse_one(cls, xml, raw)
    assert item is not None
    joined = " ".join(item.search_names)
    assert want_in_names in joined, f"搜索词里没有番名：{item.search_names}"
    assert item.search_names and all(len(n.strip()) > 2 for n in item.search_names), \
        f"搜索词退化成了季号：{item.search_names}"
