"""(R36 体检) 前端速度与请求量的守卫 —— 每条都对着量出来的浪费，变异后必须红。

  · 定时器 immediate=False：页面加载那一刻不重建第二遍（仪表盘 34 条 SQL / 157 ms）
  · _parse_date 缓存：同一个 air_date 一次渲染不再 strptime 1636 次
  · count_rows 两次往返：设置页那句"N 行业务数据"不再打 14 次
  · 同一轮采集里同 URL 的 feed 只抓一次（真库两对源组同 URL：每天 144 次白打 / 12 MB）
"""
import ast

import config
from pathlib import Path


def test_periodic_timers_do_not_rebuild_the_page_on_connect():
    """页面构建完几十毫秒后，socket 握手那一刻定时器不能再把实时区逐字重建一遍。

    NiceGUI 的 `ui.timer` 默认 immediate=True。实测仪表盘那一次是 34 条 SQL / 157 ms，
    前后 DOM 归一后完全相同 —— 纯浪费。日志页（/logs）有意不在这里：它在那几十毫秒里确实可能多出新行。
    """
    root = Path(__file__).resolve().parent.parent
    bad = []
    for rel in ("pages/anime.py", "pages/movies.py"):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        timers = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                  and getattr(n.func, "attr", "") == "timer"
                  and getattr(getattr(n.func, "value", None), "id", "") == "ui"]
        assert timers, f"{rel} 里找不到 ui.timer（守卫前提坏了）"
        for t in timers:
            kw = {k.arg: k.value for k in t.keywords}
            if not (isinstance(kw.get("immediate"), ast.Constant) and kw["immediate"].value is False):
                bad.append(f"{rel}:{t.lineno}")
    assert not bad, f"这些 ui.timer 没写 immediate=False，页面加载会被重建第二遍：{bad}"


def test_air_date_is_parsed_once_per_anime_not_once_per_torrent(clean_tables, monkeypatch):
    """一次 `suspect_wrong_binding()` 里 strptime 的次数不能随种子数线性增长。

    真库 100 番 / 2078 条种子：缓存前 1636 次 / 33 ms，缓存后 0.35 ms。
    """
    from datetime import datetime

    from core import anime as A
    from db.models import Anime, AnimeTorrent

    with clean_tables.get_session() as s:
        a = Anime(title="番", season=3, quarter="26C", confirmed=True, rejected=False,
                  bangumi_id=1, total_episodes=12, air_date="2026-07-05")
        s.add(a); s.commit(); s.refresh(a)
        for i in range(40):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{i:040x}", raw_title=f"x - {i+1}",
                               episode=float(i + 1), status="pending",
                               release_time=datetime(2024, 1, 1)))
        s.commit()

    A._parse_date.cache_clear()
    calls = []
    real = datetime.strptime

    class _Spy(datetime):
        @classmethod
        def strptime(cls, *a, **k):
            calls.append(a[0]); return real(*a, **k)
    monkeypatch.setattr(A, "datetime", _Spy)
    A._parse_date.cache_clear()
    A.suspect_wrong_binding()
    assert len(calls) <= 3, f"一次全库扫描把同一个 air_date 解析了 {len(calls)} 次（应 ≤ 番数×格式数）"


def test_count_rows_makes_two_round_trips_not_fourteen(testdb):
    """设置页只为显示一句"N 行业务数据"，不该对业务库打 14 次往返。"""
    import sqlalchemy as sa

    from db import transfer

    stmts = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        stmts.append(statement)
    sa.event.listen(testdb.engine, "before_cursor_execute", spy)
    try:
        got = transfer.count_rows(testdb.engine)
    finally:
        sa.event.remove(testdb.engine, "before_cursor_execute", spy)
    assert set(got) == set(transfer.TABLE_ORDER), "返回值形状变了（迁移比对靠它）"
    assert all(isinstance(v, int) for v in got.values())
    counts = [x for x in stmts if "count(" in x.lower()]
    assert len(counts) == 1, f"COUNT 发了 {len(counts)} 条（应合成 1 条）：{counts}"
    assert len(stmts) <= 3, f"总往返 {len(stmts)} 次（应 ≤ 3：取表名 + 一条 COUNT）：{stmts}"

# ---------------- R36 体检 load-1：同一轮里同一个 feed URL 只抓一次 ----------------

async def test_same_feed_url_is_fetched_once_per_round_but_filtered_per_group(monkeypatch):
    """两个源组指着同一个 RSS（真库：喵萌简中/繁中、Mikan 兜底/后备），一轮只该发一次请求，
    而两组各自的过滤结果必须与各自单独抓时逐字相同 —— 缓存的是字节，不是解析结果。"""
    from sources.nyaa import NyaaSource
    from services import fetch as F

    def item(n, lang, h):
        return (f'<item><title>[Nekomoe] Show A - {n:02d} [1080p][{lang}]</title>'
                f'<link>https://nyaa.si/download/{n}.torrent</link>'
                f'<pubDate>Mon, 06 Jul 2026 12:00:00 -0000</pubDate>'
                f'<nyaa:infoHash>{h * 40}</nyaa:infoHash></item>')
    feed = ('<?xml version="1.0"?><rss version="2.0" xmlns:nyaa="https://nyaa.si/xmlns/nyaa">'
            '<channel><title>t</title>' + item(1, "CHS", "a") + item(1, "CHT", "b")
            + '</channel></rss>').encode()
    calls = []

    async def fake_get_bytes(client, url, *a, **k):
        calls.append(url)
        return feed
    monkeypatch.setattr(F, "get_bytes", fake_get_bytes)

    url = "https://nyaa.si/?page=rss&u=nekomoekissaten"
    chs = NyaaSource("喵萌(简中)", url, "auto", 0, [], ["CHS"])
    cht = NyaaSource("喵萌(繁中)", url, "auto", 0, [], ["CHT"])

    # 各自单抓（无缓存）的结果当基准
    base_chs = [i.raw_title for i in await chs.fetch()]
    base_cht = [i.raw_title for i in await cht.fetch()]
    assert base_chs and base_cht and base_chs != base_cht, "前提：两组过滤结果不同"
    calls.clear()

    cache: dict = {}
    got_chs = [i.raw_title for i in await chs.fetch(content_cache=cache)]
    got_cht = [i.raw_title for i in await cht.fetch(content_cache=cache)]
    assert calls == [url], f"同一轮同一个 URL 抓了 {len(calls)} 次"
    assert (got_chs, got_cht) == (base_chs, base_cht), "走缓存之后过滤结果变了"


def test_poll_once_hands_every_source_the_same_round_cache():
    """(AST) poll_once 里必须建一份每轮的缓存并传给每个 source.fetch —— 少传等于没做。"""
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "core/worker.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "poll_once")
    fetches = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
               and getattr(n.func, "attr", "") == "fetch"]
    assert fetches, "poll_once 里找不到 source.fetch()"
    for c in fetches:
        assert any(k.arg == "content_cache" for k in c.keywords), \
            f"core/worker.py:{c.lineno} 的 fetch() 没传 content_cache，同 URL 会被重复抓"


def test_parse_date_cache_does_not_float_up(monkeypatch):
    """(核实意见) `_parse_date` 的缓存只许在它这一层：改了开始使用日，上一层 `_aired_before_start` 必须翻转。

    核实时实测把 lru_cache 挪到 `_aired_before_start` 上（体内读 config），8 个相关测试文件全绿 ——
    缓存键漏掉了真实输入，旧结论照旧返回。这条钉住那个边界。
    """
    from core import anime as A

    monkeypatch.setitem(config._v, "ANIME_START_DATE", "2026-07-01")
    assert A._aired_before_start("2026-06-15") is True
    monkeypatch.setitem(config._v, "ANIME_START_DATE", "2026-06-01")
    assert A._aired_before_start("2026-06-15") is False, "改了开始使用日，结论没翻转 —— 缓存飘到了读 config 的那一层"


async def test_round_cache_drops_a_feed_once_no_later_source_needs_it(monkeypatch, clean_tables):
    """(核实意见) 一轮可能跑几个小时（D-06）：feed 字节只活到"后面再没有源要用这个 URL"，不许整轮挂着。"""
    from core import worker as W

    seen = {}

    class S:
        def __init__(self, name, url):
            self.name, self.rss_url = name, url

        async def fetch(self, content_cache=None):
            seen[self.name] = set(content_cache)          # 进来时缓存里有什么
            if self.rss_url not in content_cache:
                content_cache[self.rss_url] = b"<rss/>"    # 照 RssSource.fetch 的口径：取回就存
            return []

    async def fake_precheck():
        return False
    monkeypatch.setattr(W, "build_sources", lambda: [S("甲", "X"), S("乙", "X"), S("丙", "Y")])
    monkeypatch.setattr(W.anime, "qb_precheck", fake_precheck)
    await W.poll_once()
    assert seen["乙"] == {"X"}, f"第二个同 URL 源进来时缓存该有 X：{seen['乙']}"
    assert "X" not in seen["丙"], f"X 已经没有源要用了，还挂在缓存里：{seen['丙']}"
