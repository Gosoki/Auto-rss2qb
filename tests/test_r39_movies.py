"""r39 剧场版线（D）：年份锁 M1y、重新识别只出候选 M2、往前刷新版本 M3、版本按发布时间排 M6、
bgm 一部都没问成不推进基准 + 退避 M8、两个 Mikan 番组落同一行留痕 M7。

全部不打网络：Mikan / bgm 一律 monkeypatch 成假实现；扫描的内存退避态每条用例复位。
"""
import logging
from datetime import datetime, timedelta

import httpx
import pytest
from sqlmodel import select

import config
from core import movies as M
from db.models import Movie, MovieTorrent

_DETAIL_MAIN = "tests/render_detail_main.py"   # 它 import 了全部页面，/movies 也挂在上面
_Y = datetime.now().year


def _q(year: int) -> str:
    return f"{year % 100:02d}A"


@pytest.fixture(autouse=True)
def _reset_scan_backoff(monkeypatch):
    """scan_now 的退避是模块级内存态，用例之间必须清零，否则前一条的失败会挡住后一条的扫描。"""
    monkeypatch.setattr(M, "_scan_fail_streak", 0)
    monkeypatch.setattr(M, "_scan_retry_at", None)


class _Client:
    """mikan.make_client() 的替身：只要能 async with。"""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Item:
    def __init__(self, h, release=None):
        self.info_hash, self.source, self.site = h, "Mikan", "mikan"
        self.raw_title, self.download_url = f"[组] 剧场版 {h[:4]}", "http://x/t.torrent"
        self.release_time, self.priority = release or datetime.now(), 0


def _add_movie(s, **kw) -> int:
    m = Movie(**{"title": "片", **kw})
    s.add(m)
    s.commit()
    s.refresh(m)
    return m.id


def _info(bgm, mq, name="bgm 名"):
    return {"bangumi_id": bgm, "display_name": name, "air_date": "2024-01-05", "movie_quarter": mq}


# ======================= M1y 年份锁 =======================

def test_set_quarter_also_locks(clean_tables):
    with clean_tables.get_session() as s:
        mid = _add_movie(s, quarter="24A", bangumi_id=500)
    assert M.set_quarter(mid, "23A") is True
    with clean_tables.get_session() as s:
        m = s.get(Movie, mid)
        assert (m.quarter, m.quarter_locked) == ("23A", True)


def test_scan_of_the_same_bgm_keeps_a_locked_year(clean_tables):
    """人工改过年份的片，同一个 bgm 的下一轮扫描只刷新名字/元数据，年份不许被算回去。"""
    with clean_tables.get_session() as s:
        mid = _add_movie(s, quarter="24A", bangumi_id=500, mikan_id="m1", display_name="旧名")
    M.set_quarter(mid, "23A")
    got, is_new = M._upsert_movie("m1", "片", 500, _info(500, "24A", "新名"), "剧场版")
    assert (got, is_new) == (mid, False)
    with clean_tables.get_session() as s:
        m = s.get(Movie, mid)
        assert m.quarter == "23A", "扫描把人工改的年份冲回去了"
        assert m.quarter_locked is True
        assert m.display_name == "新名", "只冻年份，名字照常刷新"


def test_scan_still_overwrites_an_unlocked_year(clean_tables):
    with clean_tables.get_session() as s:
        mid = _add_movie(s, quarter="23A", bangumi_id=500, mikan_id="m1")
    M._upsert_movie("m1", "片", 500, _info(500, "24A"), "剧场版")
    with clean_tables.get_session() as s:
        m = s.get(Movie, mid)
        assert (m.quarter, m.quarter_locked) == ("24A", False)


@pytest.mark.parametrize("old_bgm", [500, None])
async def test_bind_to_another_subject_unlocks(clean_tables, monkeypatch, old_bgm):
    """换绑到别的条目（含原来没绑）→ 解锁，年份跟着新条目走。"""
    with clean_tables.get_session() as s:
        mid = _add_movie(s, quarter="24A", bangumi_id=old_bgm)
    M.set_quarter(mid, "23A")

    async def by_id(bid):
        return _info(bid, "25A")
    monkeypatch.setattr(M.enrich, "fetch_by_id", by_id)
    assert await M.bind_movie_bgm(mid, 600) is True
    with clean_tables.get_session() as s:
        m = s.get(Movie, mid)
        assert (m.bangumi_id, m.quarter, m.quarter_locked) == (600, "25A", False)


async def test_bind_to_the_same_subject_keeps_the_lock(clean_tables, monkeypatch):
    """绑的还是同一个条目（『重新识别』多半就是这样）→ 锁保留、年份还原，元数据照常刷新。"""
    with clean_tables.get_session() as s:
        mid = _add_movie(s, quarter="24A", bangumi_id=500)
    M.set_quarter(mid, "23A")

    async def by_id(bid):
        return _info(bid, "24A", "刷新后的名")
    monkeypatch.setattr(M.enrich, "fetch_by_id", by_id)
    assert await M.bind_movie_bgm(mid, 500) is True
    with clean_tables.get_session() as s:
        m = s.get(Movie, mid)
        assert (m.quarter, m.quarter_locked, m.display_name) == ("23A", True, "刷新后的名")


def test_merge_inherits_the_lock(clean_tables):
    with clean_tables.get_session() as s:
        keeper = _add_movie(s, title="留下的", quarter="24A", bangumi_id=700)
        loser = _add_movie(s, title="并掉的", quarter="23A", quarter_locked=True)
        M._merge_movie(s, loser, keeper)
    with clean_tables.get_session() as s:
        assert s.get(Movie, loser) is None
        k = s.get(Movie, keeper)
        assert (k.quarter, k.quarter_locked) == ("23A", True), "用户的年份纠正随 loser 一起被删了"


def test_merge_does_not_move_a_keeper_that_already_has_files(clean_tables):
    """keeper 已下过：它的目录已落盘，不因 loser 的锁改年份。"""
    with clean_tables.get_session() as s:
        keeper = _add_movie(s, title="留下的", quarter="24A", bangumi_id=701)
        loser = _add_movie(s, title="并掉的", quarter="23A", quarter_locked=True)
        s.add(MovieTorrent(movie_id=keeper, info_hash="k" * 40, raw_title="v", status="sent"))
        s.commit()
        M._merge_movie(s, loser, keeper)
    with clean_tables.get_session() as s:
        k = s.get(Movie, keeper)
        assert (k.quarter, k.quarter_locked) == ("24A", False)


# ======================= M2 重新识别只出候选 =======================

def _movie_for_suggest(s, **kw) -> int:
    mid = _add_movie(s, title="原名", display_name="展示名", quarter="24A", **kw)
    s.add(MovieTorrent(movie_id=mid, info_hash="h" * 40, raw_title="[组] 片", status="pending"))
    s.commit()
    return mid


def _snapshot(s, mid):
    m = s.get(Movie, mid)
    return (m.bangumi_id, m.quarter, m.display_name, m.quarter_locked)


async def test_suggest_uses_mikan_and_never_searches_when_it_has_a_link(clean_tables, monkeypatch):
    with clean_tables.get_session() as s:
        mid = _movie_for_suggest(s, mikan_id="3001")
        before = _snapshot(s, mid)

    async def detail(client, mk):
        assert mk == "3001"
        return 4321

    async def resolve(*a, **kw):
        raise AssertionError("Mikan 已经给了精确链接，不该再模糊搜")
    monkeypatch.setattr(M.mikan, "make_client", lambda: _Client())
    monkeypatch.setattr(M.mikan, "fetch_detail", detail)
    monkeypatch.setattr(M.enrich, "resolve", resolve)

    r = await M.suggest_movie_bgm(mid)
    assert (r["bgm_id"], r["via"], r["current"]) == (4321, "mikan", None)
    with clean_tables.get_session() as s:
        assert _snapshot(s, mid) == before, "suggest 只出候选，不许写库"


async def test_suggest_reports_a_mikan_error_instead_of_guessing(clean_tables, monkeypatch):
    """Mikan 打不开 ≠ 没有链接：页面上很可能有精确链接，这时不许退回模糊搜。"""
    with clean_tables.get_session() as s:
        mid = _movie_for_suggest(s, mikan_id="3001")
        before = _snapshot(s, mid)

    async def detail(client, mk):
        raise httpx.ConnectError("连不上")

    async def resolve(*a, **kw):
        raise AssertionError("Mikan 抛错时不许退回模糊搜")
    monkeypatch.setattr(M.mikan, "make_client", lambda: _Client())
    monkeypatch.setattr(M.mikan, "fetch_detail", detail)
    monkeypatch.setattr(M.enrich, "resolve", resolve)

    r = await M.suggest_movie_bgm(mid)
    assert r["bgm_id"] is None
    assert "Mikan 取不到" in r["msg"] and "粘 bgm 链接" in r["msg"]
    with clean_tables.get_session() as s:
        assert _snapshot(s, mid) == before


@pytest.mark.parametrize("mikan_id", ["3001", None])
async def test_suggest_falls_back_to_an_undated_search(clean_tables, monkeypatch, mikan_id):
    """页面打开了但没有 bgm 链接 / 根本没有 mikan_id → 按名字搜，日期基准一律不设（上架日不是首映日）。"""
    with clean_tables.get_session() as s:
        mid = _movie_for_suggest(s, mikan_id=mikan_id)
        before = _snapshot(s, mid)
    seen = {}

    async def detail(client, mk):
        return None

    async def resolve(names, release_time=None, episode=None, info_hash=None):
        seen.update(names=names, release=release_time, est=episode, h=info_hash)
        return {"bangumi_id": 9001, "display_name": "搜到的"}
    monkeypatch.setattr(M.mikan, "make_client", lambda: _Client())
    monkeypatch.setattr(M.mikan, "fetch_detail", detail)
    monkeypatch.setattr(M.enrich, "resolve", resolve)

    r = await M.suggest_movie_bgm(mid)
    assert (r["bgm_id"], r["via"]) == (9001, "search")
    assert "未校验日期" in r["msg"]
    assert seen["release"] is None and seen["est"] is None and seen["h"] == "h" * 40
    assert seen["names"] == ["展示名", "原名"]
    with clean_tables.get_session() as s:
        assert _snapshot(s, mid) == before


@pytest.mark.parametrize("current, want", [(111, "与当前绑定 111 不同"), (222, "就是当前绑定")])
async def test_suggest_says_how_the_candidate_relates_to_the_current_binding(clean_tables, monkeypatch,
                                                                          current, want):
    with clean_tables.get_session() as s:
        mid = _movie_for_suggest(s, mikan_id="3001", bangumi_id=current)

    async def detail(client, mk):
        return 222
    monkeypatch.setattr(M.mikan, "make_client", lambda: _Client())
    monkeypatch.setattr(M.mikan, "fetch_detail", detail)

    r = await M.suggest_movie_bgm(mid)
    assert r["bgm_id"] == 222 and r["current"] == current
    assert want in r["msg"], r["msg"]
    with clean_tables.get_session() as s:
        assert s.get(Movie, mid).bangumi_id == current


async def test_the_page_helper_confirms_before_binding(monkeypatch):
    """页面 helper：无候选只提示；有候选先弹说明、过回显闸，用户取消就不绑；确认了才 bind + 搬迁确认。"""
    from pages import movies as P

    calls = []
    monkeypatch.setattr(P.ui, "notify", lambda msg, **kw: calls.append(("notify", msg, kw.get("type"))))
    monkeypatch.setattr(M, "movie_save_path", lambda mid: "/old")

    async def bind(mid, bid, report=None):
        calls.append(("bind", mid, bid))
        return True

    async def relocate(mid, old, cb):
        calls.append(("relocate", mid, old))
    monkeypatch.setattr(M, "bind_movie_bgm", bind)
    monkeypatch.setattr(P, "_maybe_relocate_movie", relocate)

    # ① 没候选
    async def none(mid):
        return {"bgm_id": None, "via": "mikan", "current": None, "msg": "Mikan 取不到：x"}
    monkeypatch.setattr(M, "suggest_movie_bgm", none)

    async def gate(*a, **kw):
        raise AssertionError("没候选不该进回显闸")
    monkeypatch.setattr(P, "require_bind_confirm", gate)
    await P._confirm_and_bind(7, lambda: None)()
    assert calls == [("notify", "Mikan 取不到：x", "warning")]

    # ② 有候选、与现绑定不同、用户取消
    calls.clear()

    async def cand(mid):
        return {"bgm_id": 222, "via": "mikan", "current": 111, "msg": "与当前绑定 111 不同"}
    monkeypatch.setattr(M, "suggest_movie_bgm", cand)
    answer = {"v": False}

    async def gate2(mid, bid, kind="anime"):
        calls.append(("gate", mid, bid, kind))
        return answer["v"]
    monkeypatch.setattr(P, "require_bind_confirm", gate2)
    await P._confirm_and_bind(7, lambda: None)()
    assert calls == [("notify", "与当前绑定 111 不同", "warning"), ("gate", 7, 222, "movie")]

    # ③ 用户确认
    calls.clear()
    answer["v"] = True
    await P._confirm_and_bind(7, lambda: None)()
    assert [c[0] for c in calls] == ["notify", "gate", "bind", "notify", "relocate"]
    assert ("bind", 7, 222) in calls and calls[3][2] == "positive"


# ======================= M3 往前刷新版本 =======================

def _patch_rss(monkeypatch, fail=()):
    got = []

    async def rss(client, mk):
        got.append(mk)
        if mk in fail:
            raise httpx.ConnectError("Mikan 挂了")
        return [_Item(f"{mk:0>40}"[-40:])]
    monkeypatch.setattr(M.mikan, "make_client", lambda: _Client())
    monkeypatch.setattr(M.mikan, "fetch_bangumi_torrents", rss)
    return got


def _forbid_identity_paths(monkeypatch):
    """往前刷新只拉 RSS：任何自动身份判断 / bgm 请求都不许发生。"""
    def boom(*a, **kw):
        raise AssertionError("往前刷新碰了身份判断 / bgm")

    async def aboom(*a, **kw):
        raise AssertionError("往前刷新碰了身份判断 / bgm")
    monkeypatch.setattr(M, "_upsert_movie", boom)
    monkeypatch.setattr(M.enrich, "fetch_by_id", aboom)
    monkeypatch.setattr(M.mikan, "fetch_detail", aboom)


async def test_refresh_back_years_window_and_skip(clean_tables, monkeypatch, cfg):
    cfg(MOVIE_REFRESH_BACK_YEARS=2, MOVIE_SCAN_LAST="2026-01-01T00:00:00")
    with clean_tables.get_session() as s:
        _add_movie(s, mikan_id="old3", quarter=_q(_Y - 3))      # 窗口外
        _add_movie(s, mikan_id="old2", quarter=_q(_Y - 2))      # 窗口下沿
        _add_movie(s, mikan_id="cur", quarter=_q(_Y))           # 窗口上沿
        _add_movie(s, mikan_id="skipme", quarter=_q(_Y - 1))    # 本轮当年扫描已拉过
        _add_movie(s, mikan_id="noq", quarter=None)             # 年份未知
        _add_movie(s, mikan_id=None, quarter=_q(_Y - 1))        # 没有 Mikan 番组
    got = _patch_rss(monkeypatch)
    _forbid_identity_paths(monkeypatch)

    out = await M._refresh_back_years(_Y, {"skipme"})
    assert sorted(got) == ["cur", "old2"]
    assert out == {"movies": 2, "added": 2, "errors": 0}
    assert config.MOVIE_SCAN_LAST == "2026-01-01T00:00:00", "往前刷新不许碰扫描基准"


async def test_refresh_back_years_clamps_the_setting(clean_tables, monkeypatch, cfg):
    cfg(MOVIE_REFRESH_BACK_YEARS=9)            # 库里被改成越界值：读取时钳到 3
    with clean_tables.get_session() as s:
        _add_movie(s, mikan_id="y3", quarter=_q(_Y - 3))
        _add_movie(s, mikan_id="y4", quarter=_q(_Y - 4))
    got = _patch_rss(monkeypatch)
    await M._refresh_back_years(_Y, set())
    assert got == ["y3"]


async def test_refresh_back_years_rejected_movie_gets_skipped_and_errors_are_isolated(
        clean_tables, monkeypatch, cfg):
    cfg(MOVIE_REFRESH_BACK_YEARS=1)
    with clean_tables.get_session() as s:
        bad = _add_movie(s, mikan_id="bad", quarter=_q(_Y - 1))
        rej = _add_movie(s, mikan_id="rej", quarter=_q(_Y - 1), rejected=True)
    _patch_rss(monkeypatch, fail={"bad"})
    _forbid_identity_paths(monkeypatch)

    out = await M._refresh_back_years(_Y, set())
    assert out == {"movies": 1, "added": 1, "errors": 1}, "一部 Mikan 抛错不该拖垮其它"
    with clean_tables.get_session() as s:
        rows = list(s.exec(select(MovieTorrent)))
    assert [(t.movie_id, t.status) for t in rows] == [(rej, "skipped")], "已忽略的片新版本要记 skipped"
    assert bad not in {t.movie_id for t in rows}


def _patch_discover(monkeypatch, res):
    async def fake(year, seasons=None):
        return dict(res)
    monkeypatch.setattr(M, "discover_movies", fake)


def _record_refresh(monkeypatch):
    calls = []

    async def fake(year, skip):
        calls.append((year, set(skip)))
        return {"movies": 0, "added": 0, "errors": 0}
    monkeypatch.setattr(M, "_refresh_back_years", fake)
    return calls


_OK = {"movies": 0, "torrents": 0, "seen": 3, "errors": 0, "bgm_down": False, "mikan_ids": {"a", "b"}}


@pytest.mark.parametrize("k, res, want", [
    (2, _OK, [(_Y, {"a", "b"})]),
    (0, _OK, []),                                                   # 0 = 关
    (2, {**_OK, "seen": 0, "errors": 4}, []),                       # 当年那轮 Mikan 全挂
    (2, {**_OK, "bgm_down": True}, []),                             # 当年那轮 bgm 一部都没问成
])
async def test_auto_scan_tick_refreshes_back_years_only_after_a_good_scan(clean_tables, monkeypatch, cfg,
                                                                         k, res, want):
    cfg(MOVIE_SCAN_ENABLED=True, MOVIE_SCAN_LAST="", MOVIE_REFRESH_BACK_YEARS=k)
    _patch_discover(monkeypatch, res)
    calls = _record_refresh(monkeypatch)
    assert await M.auto_scan_tick() is True
    assert calls == want


async def test_a_broken_refresh_does_not_break_the_tick(clean_tables, monkeypatch, cfg):
    cfg(MOVIE_SCAN_ENABLED=True, MOVIE_SCAN_LAST="", MOVIE_REFRESH_BACK_YEARS=2)
    _patch_discover(monkeypatch, _OK)

    async def boom(year, skip):
        raise RuntimeError("库抖了一下")
    monkeypatch.setattr(M, "_refresh_back_years", boom)
    assert await M.auto_scan_tick() is True
    assert config.MOVIE_SCAN_LAST, "当年那轮成功了，基准照常推进"


# ======================= M6 版本按发布时间排 =======================

def test_movie_torrents_are_ordered_by_release_time(clean_tables):
    base = datetime(2026, 3, 1)
    with clean_tables.get_session() as s:
        mid = _add_movie(s, quarter="26A")
        # 入库顺序与发布顺序相反（RSS 从新到旧逐条 commit 时就是这样）
        for i, h in enumerate("abc"):
            s.add(MovieTorrent(movie_id=mid, info_hash=h * 40, raw_title=h,
                               created_at=base + timedelta(minutes=i),
                               release_time=base - timedelta(days=10 * i)))
        # 没有发布时间的老行退回入库时间：比 a 的发布时间晚，排最前
        s.add(MovieTorrent(movie_id=mid, info_hash="d" * 40, raw_title="d",
                           created_at=base + timedelta(days=1), release_time=None))
        s.commit()
    assert [t.raw_title for t in M.movie_torrents(mid)] == ["d", "a", "b", "c"]


# ======================= M8 bgm 一部都没问成 + 退避 =======================

def _patch_scan(monkeypatch, bgm_ids, by_id, bucket_fail=False):
    """当年桶 A 季放这几部（mikan id = m0, m1 …），其余季为空；bgm 由 by_id 回答。返回请求计数。"""
    n = {"bucket": 0}

    async def bucket(client, year, letter):
        n["bucket"] += 1
        if bucket_fail:
            raise httpx.ConnectError("Mikan 挂了")
        return [(f"m{i}", f"片{i}", "剧场版") for i in range(len(bgm_ids))] if letter == "A" else []

    async def detail(client, mk):
        return bgm_ids[int(mk[1:])]

    async def rss(client, mk):
        return []
    monkeypatch.setattr(M.mikan, "make_client", lambda: _Client())
    monkeypatch.setattr(M.mikan, "discover_movie_bucket", bucket)
    monkeypatch.setattr(M.mikan, "fetch_detail", detail)
    monkeypatch.setattr(M.mikan, "fetch_bangumi_torrents", rss)
    monkeypatch.setattr(M.enrich, "fetch_by_id", by_id)
    return n


async def _not_found(bid):            # 问到了、对方说没有（404）：不记 bgm 失败
    return None


async def _down(bid):                 # 连接失败 / 5xx：enrich 会记一次 bgm 没问成
    M.enrich._note_bgm_fail()
    return None


async def _found(bid):
    return _info(bid, _q(_Y))


async def test_all_404_is_not_bgm_down(clean_tables, monkeypatch, cfg):
    """年初桶里只有一两部且恰好都 404：bgm 是通的，基准照常推进（判据是『一部都没问成』）。"""
    cfg(MOVIE_SCAN_LAST="")
    _patch_scan(monkeypatch, [11, 12], _not_found)
    res = await M.scan_now(_Y)
    assert (res["bgm_down"], res["failed"]) == (False, False)
    assert config.MOVIE_SCAN_LAST and M._scan_retry_at is None


async def test_bgm_down_freezes_the_baseline_and_schedules_a_retry(clean_tables, monkeypatch, cfg):
    cfg(MOVIE_SCAN_LAST="", MOVIE_SCAN_RETRY_MIN=30, MOVIE_SCAN_INTERVAL=604800)
    _patch_scan(monkeypatch, [11, 12], _down)
    res = await M.scan_now(_Y)
    assert (res["bgm_down"], res["failed"]) == (True, True)
    assert config.MOVIE_SCAN_LAST == "", "bgm 一部都没问成还推进了基准"
    left = (M._scan_retry_at - datetime.now()).total_seconds()
    assert 29 * 60 < left <= 30 * 60
    assert res["retry_at"]
    with clean_tables.get_session() as s:
        assert all(m.bangumi_id is None for m in s.exec(select(Movie))), "新片以待识别落库，不落裸 bgm_id"


@pytest.mark.parametrize("code, down", [(503, True), (429, True), (404, False)])
async def test_bgm_down_through_the_real_fetch_by_id(clean_tables, monkeypatch, cfg, code, down):
    """端到端：真 enrich.fetch_by_id（只把底层 HTTP 换成固定状态码）。429/5xx 记『没问成』，404 是『问到了、没有』。"""
    from services import enrich
    cfg(MOVIE_SCAN_LAST="", ENRICH_RETRY_TIMES=1)

    async def capped(client, method, url, **kw):
        return code, b""
    monkeypatch.setattr(enrich, "_capped", capped)
    _patch_scan(monkeypatch, [11, 12], enrich.fetch_by_id)
    res = await M.scan_now(_Y)
    assert res["bgm_down"] is down
    assert bool(config.MOVIE_SCAN_LAST) is (not down)


async def test_partial_bgm_hit_advances(clean_tables, monkeypatch, cfg):
    cfg(MOVIE_SCAN_LAST="")
    hits = iter([True, False])

    async def mixed(bid):
        if next(hits):
            return await _found(bid)
        return await _down(bid)
    _patch_scan(monkeypatch, [11, 12], mixed)
    res = await M.scan_now(_Y)
    assert (res["bgm_down"], res["failed"]) == (False, False)
    assert config.MOVIE_SCAN_LAST


async def test_no_bgm_ids_at_all_is_not_bgm_down(clean_tables, monkeypatch, cfg):
    """Mikan 页上都没有 bgm 链接 = 一次都没问 bgm，谈不上 bgm 挂了。"""
    cfg(MOVIE_SCAN_LAST="")
    _patch_scan(monkeypatch, [None, None], _down)
    res = await M.scan_now(_Y)
    assert (res["bgm_down"], res["failed"]) == (False, False)


async def test_mikan_down_backs_off_without_requests(clean_tables, monkeypatch, cfg):
    """Mikan 全挂：第一次 tick 失败排重试；窗口内第二次 tick 一个请求都不发；窗口过了再扫。"""
    cfg(MOVIE_SCAN_ENABLED=True, MOVIE_SCAN_LAST="", MOVIE_SCAN_RETRY_MIN=30, MOVIE_SCAN_INTERVAL=604800)
    n = _patch_scan(monkeypatch, [], _found, bucket_fail=True)
    assert await M.auto_scan_tick() is True
    assert n["bucket"] == 4
    assert await M.auto_scan_tick() is False
    assert n["bucket"] == 4, "退避窗口内又打了 Mikan"
    monkeypatch.setattr(M, "_scan_retry_at", datetime.now() - timedelta(seconds=1))
    assert await M.auto_scan_tick() is True
    assert n["bucket"] == 8


async def test_backoff_doubles_and_is_capped_by_the_interval(clean_tables, monkeypatch, cfg):
    cfg(MOVIE_SCAN_LAST="", MOVIE_SCAN_RETRY_MIN=30, MOVIE_SCAN_INTERVAL=90 * 60)
    _patch_scan(monkeypatch, [], _found, bucket_fail=True)
    waits = []
    for _ in range(4):
        await M.scan_now(_Y)
        waits.append(round((M._scan_retry_at - datetime.now()).total_seconds() / 60))
    assert waits == [30, 60, 90, 90]
    assert M._scan_fail_streak == 4


async def test_success_clears_the_backoff(clean_tables, monkeypatch, cfg):
    cfg(MOVIE_SCAN_LAST="")
    monkeypatch.setattr(M, "_scan_fail_streak", 3)
    monkeypatch.setattr(M, "_scan_retry_at", datetime.now() + timedelta(hours=1))
    _patch_scan(monkeypatch, [11], _found)
    res = await M.scan_now(_Y)
    assert not res["failed"]
    assert (M._scan_fail_streak, M._scan_retry_at) == (0, None)


async def test_past_year_or_partial_scan_does_not_touch_the_backoff(clean_tables, monkeypatch, cfg):
    """退避只管会推进基准的『当年 + 四季』那种；手动补抓往年/单季失败不排自动重试。"""
    cfg(MOVIE_SCAN_LAST="")
    _patch_scan(monkeypatch, [], _found, bucket_fail=True)
    r1 = await M.scan_now(_Y - 1)
    r2 = await M.scan_now(_Y, ["A"])
    assert r1["failed"] and r2["failed"]
    assert (M._scan_fail_streak, M._scan_retry_at, r1["retry_at"], r2["retry_at"]) == (0, None, None, None)


async def test_manual_scan_ignores_the_backoff(clean_tables, monkeypatch, cfg):
    from core import worker as W
    cfg(MOVIE_SCAN_LAST="")
    monkeypatch.setattr(M, "_scan_retry_at", datetime.now() + timedelta(hours=1))
    n = _patch_scan(monkeypatch, [11], _found)
    res = await W.scan_movies_now(_Y, ["A", "B", "C", "D"])
    assert res is not None and n["bucket"] == 4, "手动『立即扫描』被退避闸挡住了"
    assert M._scan_retry_at is None


async def test_expired_retry_counts_as_due_even_if_the_last_scan_is_recent(clean_tables, monkeypatch, cfg):
    """(F25) 开着周扫描、基准是 1 小时前：手动扫当年四季碰上 bgm 全挂 → 排了重试；
    窗口内不扫，重试一到期就扫，不再被『距上次 < 间隔』挡到下一个间隔（页面/日志写的『自动重试』要成立）。"""
    recent = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    cfg(MOVIE_SCAN_ENABLED=True, MOVIE_SCAN_LAST=recent, MOVIE_SCAN_RETRY_MIN=30, MOVIE_SCAN_INTERVAL=604800)
    n = _patch_scan(monkeypatch, [11, 12], _down)
    assert await M.auto_scan_tick() is False, "没挂重试时照旧按间隔判"
    res = await M.scan_now(_Y)
    assert res["bgm_down"] and res["retry_at"] and config.MOVIE_SCAN_LAST == recent
    assert n["bucket"] == 4
    assert await M.auto_scan_tick() is False
    assert n["bucket"] == 4, "退避窗口内又打了 Mikan"
    monkeypatch.setattr(M, "_scan_retry_at", datetime.now() - timedelta(seconds=1))
    n2 = _patch_scan(monkeypatch, [11, 12], _found)   # 网络恢复了
    assert await M.auto_scan_tick() is True, "重试到期了却被『距上次 < 间隔』挡住"
    assert n2["bucket"] == 4
    assert config.MOVIE_SCAN_LAST != recent and M._scan_retry_at is None


@pytest.mark.parametrize("enabled, want, not_want", [(True, "自动重试", "稍后再扫一次"),
                                                     (False, "稍后再扫一次", "自动重试")])
async def test_failure_log_says_auto_retry_only_when_auto_scan_is_on(clean_tables, monkeypatch, cfg, caplog,
                                                                     enabled, want, not_want):
    """(F25) 自动扫描关着时 auto_scan_tick 直接 return，日志不许写『自动重试』。"""
    cfg(MOVIE_SCAN_ENABLED=enabled, MOVIE_SCAN_LAST="")
    _patch_scan(monkeypatch, [], _found, bucket_fail=True)
    with caplog.at_level(logging.WARNING, logger="autorss"):
        await M.scan_now(_Y)
    msgs = [r.getMessage() for r in caplog.records if "剧场版扫描整轮失败" in r.getMessage()]
    assert len(msgs) == 1, caplog.text
    assert want in msgs[0] and not_want not in msgs[0], msgs[0]


async def _run_all_once(monkeypatch):
    """设置页『重新激活全部任务』：抓源桩成空操作，只看剧场版那一段。"""
    import asyncio
    from core import engine as E, worker as W

    async def no_poll():
        return None
    monkeypatch.setattr(W, "poll_once", no_poll)
    monkeypatch.setattr(E, "qb_kick", asyncio.Event())   # 模块级 Event，别把 set 状态漏给别的用例
    return await W.run_all_once()


@pytest.mark.parametrize("last", ["", "recent"])
async def test_run_all_once_bypasses_the_backoff(clean_tables, monkeypatch, cfg, last):
    """(F26) 连续失败 5 次、退避还剩 16 小时；用户修好网络点『重新激活全部任务』→ 立刻扫，不被退避闸静默挡掉。
    基准是最近的也一样：挂着重试 = 上一轮基准型扫描失败了，本来就该重扫。后台心跳照旧受闸。"""
    if last:
        last = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    cfg(MOVIE_SCAN_ENABLED=True, ANIME_POLL_ENABLED=True, MOVIE_SCAN_LAST=last, MOVIE_SCAN_INTERVAL=604800)
    monkeypatch.setattr(M, "_scan_fail_streak", 5)
    monkeypatch.setattr(M, "_scan_retry_at", datetime.now() + timedelta(hours=16))
    n = _patch_scan(monkeypatch, [11], _found)
    assert await M.auto_scan_tick() is False and n["bucket"] == 0, "后台心跳不该绕过退避闸"
    ok, msg = await _run_all_once(monkeypatch)
    assert n["bucket"] == 4, "『重新激活全部任务』被退避闸挡住了"
    assert ok, msg
    assert (M._scan_fail_streak, M._scan_retry_at) == (0, None)


async def test_run_all_once_still_waits_for_the_interval_without_a_pending_retry(clean_tables, monkeypatch, cfg):
    """(F26) 绕过的只是退避闸：没挂重试、基准又是最近的，照旧『按需』不扫。"""
    recent = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    cfg(MOVIE_SCAN_ENABLED=True, ANIME_POLL_ENABLED=True, MOVIE_SCAN_LAST=recent, MOVIE_SCAN_INTERVAL=604800)
    n = _patch_scan(monkeypatch, [11], _found)
    ok, _ = await _run_all_once(monkeypatch)
    assert ok and n["bucket"] == 0


def test_clamp_scan_setting():
    assert M.clamp_scan_setting("MOVIE_REFRESH_BACK_YEARS", -1) == 0
    assert M.clamp_scan_setting("MOVIE_REFRESH_BACK_YEARS", 9) == 3
    assert M.clamp_scan_setting("MOVIE_SCAN_RETRY_MIN", 1) == 5
    assert M.clamp_scan_setting("MOVIE_SCAN_RETRY_MIN", 99999) == 1440
    for k, (lo, hi) in M.SCAN_SETTING_RANGE.items():
        kind, default = config._SPEC[k]
        assert kind is int and lo <= default <= hi, k
        assert k not in config._RANGE, f"{k} 在 /movies 上改，别登记进设置页那张 _RANGE"


# ======================= M7 两个番组落到同一行 =======================

async def test_two_mikan_entries_on_one_bgm_leave_a_warning(clean_tables, monkeypatch, cfg, caplog):
    cfg(MOVIE_SCAN_LAST="")
    _patch_scan(monkeypatch, [900, 900], _found)
    with caplog.at_level(logging.WARNING, logger="autorss"):
        await M.scan_now(_Y)
    assert any("m0、m1 都链到 bgm 900" in r.getMessage() for r in caplog.records), caplog.text
    with clean_tables.get_session() as s:
        assert len(list(s.exec(select(Movie)))) == 1, "只留痕，不该多出或少掉行"


# ======================= 页面照常渲染 =======================

@pytest.mark.nicegui_main_file(_DETAIL_MAIN)
async def test_sources_tab_shows_the_two_new_boxes(user, clean_tables):
    await user.open("/movies?t=sources")
    await user.should_see("往前刷新（年）")
    await user.should_see("失败重试（分钟）")


@pytest.mark.nicegui_main_file(_DETAIL_MAIN)
async def test_fail_tab_and_detail_still_render(user, clean_tables):
    with clean_tables.get_session() as s:
        mid = _add_movie(s, title="待识别的片", display_name="待识别的片", quarter="26A")
    await user.open("/movies?t=fail")
    await user.should_see("重试识别")
    await user.open(f"/mdetail/{mid}")
    await user.should_see("重新识别")
    await user.should_see("编辑年份")
