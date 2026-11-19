"""R41 生产实况那一包：

· PR-1 仪表盘巡检提示『锁定源落后』（core/alerts 的 lag 类）：判据同 locked_out_episodes，兜底被挡时写明原因；
  别的组照常来新集不复活，改锁了别的组才复活，锁定组追上就消失。
· PR-2 绝对编号续作中间的洞：同一个组发过 k−1 与 k+1、任何组都没有 k，才报 k 缺集。
· PR-3 锁源缺集的『两套编号同步』豁免只在锁定组那一侧集号 ≤ 总集数时生效（#8 的 22、23 不再被豁免）。
· PR-4 已忽略番 / 孤儿的待下单独一档『已忽略番』。
· perf-1 仪表盘 30 秒定时器：数据没变不重建，变了（含 qB 进度、按此刻算的文案）就重建。
形状照生产库副本里的 #8『相反的你和我 第二季』：bgm 记 13 集，喵萌（锁定）发到 16，ANi / Nix 用同一套绝对号发到 25、缺 24。
"""
import asyncio
import itertools
import sys
from datetime import datetime, timedelta

import pytest
from nicegui import ui
from nicegui.testing import User
from sqlmodel import select

import config
import db
from core import alerts, anime as A
from db.models import AlertAck, Anime, AnimeTorrent

db.init_db()
config.load_from_db()
db.apply_configured_backend()

_MAIN = "tests/render_main.py"
NOW = datetime.now()
LOCK = "喵萌奶茶屋"
_seq = itertools.count(1)


def _d(days):
    return NOW - timedelta(days=days)


@pytest.fixture
def env(cfg):
    cfg(ANIME_LOCK_FALLBACK_DAYS=14, ANIME_LOCK_LAG_DAYS=7, ANIME_LOCK_SYNC_DAYS=2, ANIME_FINISH_UNSUB=False,
        FEED_GAPS="", ANIME_POLL_FAILED="", ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=60, QB_ENABLED=False)
    return cfg


def _anime(s, **kw):
    base = dict(title="相反的你和我", display_name="相反的你和我 第二季", season=2, quarter="26C", confirmed=True,
                rejected=False, total_episodes=13, bangumi_id=8, air_date=_d(140).date().isoformat())
    base.update(kw)
    a = Anime(**base)
    s.add(a)
    s.commit()
    s.refresh(a)
    return a


def _row(s, aid, src, ep, status, at, **kw):
    n = next(_seq)
    t = AnimeTorrent(anime_id=aid, info_hash=f"{n:040x}", source=src, site="nyaa",
                     raw_title=f"[{src}] 相反的你和我 - {int(ep):02d} [1080p][简日]", season=1, episode=float(ep),
                     status=status, download_url=f"https://x/{n}.torrent", created_at=at,
                     release_time=at - timedelta(hours=1), **kw)
    s.add(t)
    s.commit()
    s.refresh(t)
    return t.id


def _shape8(s, **akw):
    """#8：锁喵萌。喵萌发 13（126 天前）、14（73 天前）、15（66 天前）、16（14 天前），全部已下。
    ANi 13~23 周更、25 晚一个月（都 pending）；第 24 集谁都没有。ANi 的 22、23 与喵萌的 14、15 前后脚入库。"""
    a = _anime(s, pref_source=LOCK, **akw)
    for ep, days in ((13, 126), (14, 73), (15, 66), (16, 14)):
        _row(s, a.id, LOCK, ep, "sent", _d(days))
    for ep in range(13, 24):
        _row(s, a.id, "ANi", ep, "pending", _d(137 - 7 * (ep - 13) - (1 if ep >= 22 else 0)))
    _row(s, a.id, "ANi", 25, "pending", _d(38))
    return a.id


def _subs():
    with db.get_session() as s:
        return list(s.exec(select(Anime).where(*A.subscribed_where())))


# ---------------------------------------------------------------- PR-3

def test_lock_lag_no_longer_exempts_same_numbering_when_lock_side_is_over_total(clean_tables, env):
    """喵萌的 14、15 本身就超出总集数 13 —— 两边是同一套绝对号，ANi 的 22、23 只是被甩开，不是"两套编号同步"。"""
    with clean_tables.get_session() as s:
        aid = _shape8(s)
    got = A.locked_out_episodes(_subs())[aid]
    assert got["eps"] == [17, 18, 19, 20, 21, 22, 23, 25], got
    assert got["lock_max"] == 16


def test_lock_lag_still_exempts_in_season_numbering(clean_tables, env):
    """锁定组发的是季内号（≤ 总集数）、别组同时在发绝对号：照旧是"两套编号同步在发"，不算落后。"""
    with clean_tables.get_session() as s:
        a = _anime(s, pref_source=LOCK, total_episodes=12)
        _row(s, a.id, LOCK, 1, "sent", _d(60))
        _row(s, a.id, LOCK, 2, "sent", _d(30))
        _row(s, a.id, "ANi", 14, "pending", _d(30))      # 与锁定组的第 2 集前后脚，差 12 集
    assert A.locked_out_episodes(_subs()) == {}


# ---------------------------------------------------------------- PR-2

def test_bracket_holes_only_same_group_both_sides():
    seen = {13, 14, 15, 17, 18, 20}
    assert A._bracket_holes({"ANi": {13, 14, 15, 17, 18}}, seen) == {16}
    assert A._bracket_holes({"ANi": {13, 14, 15}, "Nix": {17, 18}}, seen) == set()   # 两侧不是同一组
    assert A._bracket_holes({"ANi": {18, 20}, "Nix": {19}}, {18, 19, 20}) == set()   # 别的组有 19 → 不是洞


def test_gap_reports_the_hole_in_absolute_numbering(clean_tables, env):
    """#8：见过 13~23、25（全部超出总集数），第 24 集谁都没有 —— 以前整部番不报。"""
    with clean_tables.get_session() as s:
        aid = _anime(s).id
        for ep in list(range(13, 24)) + [25]:
            _row(s, aid, "ANi", ep, "pending", _d(137 - 7 * (ep - 13)))
    assert A.episode_gaps(_subs()) == {aid: [24]}


def test_gap_hole_needs_the_same_group_on_both_sides(clean_tables, env):
    with clean_tables.get_session() as s:
        a = _anime(s)
        for ep in range(13, 24):
            _row(s, a.id, "ANi", ep, "pending", _d(137 - 7 * (ep - 13)))
        _row(s, a.id, "Nix-Raws", 25, "pending", _d(38))
    assert A.episode_gaps(_subs()) == {}


def test_gap_hole_needs_both_sides(clean_tables, env):
    """23 之后连缺 24、25，同一个组只有一侧（23），不报：分不清是两套编号混着来还是真缺。"""
    with clean_tables.get_session() as s:
        a = _anime(s)
        for ep in list(range(13, 24)) + [26]:
            _row(s, a.id, "ANi", ep, "pending", _d(137 - 7 * (ep - 13)))
    assert A.episode_gaps(_subs()) == {}


def test_gap_hole_ignores_ambiguous_range(clean_tables, env):
    """有集号歧义段（ep_offset < 总集数）的番照旧不判。"""
    with clean_tables.get_session() as s:
        a = _anime(s, ep_offset=12, total_episodes=24)
        for ep in list(range(13, 24)) + [25, 26]:
            _row(s, a.id, "ANi", ep, "pending", _d(137 - 7 * (ep - 13)))
    assert A.episode_gaps(_subs()) == {}


def test_gap_hole_does_not_add_a_fake_tail(clean_tables, env):
    """洞已单独报了，尾巴按 lo~hi 的跨度数：开播 10 周、跨度 13~23 共 11 集，应播 9 集 → 只报洞、不报尾巴。"""
    with clean_tables.get_session() as s:
        aid = _anime(s, air_date=_d(70).date().isoformat()).id
        for ep in [e for e in range(13, 24) if e != 18]:
            _row(s, aid, "ANi", ep, "pending", _d(70 - 7 * (ep - 13) // 2))
    assert A.episode_gaps(_subs()) == {aid: [18]}


# ---------------------------------------------------------------- PR-1

def _lag(view=None):
    return [f for f in (view or alerts.view())["live"] if f["kind"] == "lag"]


def test_lock_lag_alert_names_the_blocking_gate(clean_tables, env):
    with clean_tables.get_session() as s:
        aid = _shape8(s)
    why = A.lock_fallback_preview()[1].get(aid)
    assert why, "形状前提坏了：#8 的兜底应被番级闸挡住"
    (f,) = _lag()
    assert f["open"] == [aid] and f"(#{aid})" in f["text"]
    assert "第 17～23、25 集" in f["text"] and f"停在第 16 集" in f["text"]
    assert A.lock_fallback_blocked_text(why) in f["text"]
    assert "只补这几集" in f["text"] and "改回按优先级" in f["text"]
    assert "lock" not in f, "只给『打开详情』，不在仪表盘直接改下载"


def test_lock_lag_alert_says_auto_fallback_when_not_blocked(clean_tables, env):
    """兜底没被挡住：文案说满 N 天会自动补，不编造挡住原因。"""
    with clean_tables.get_session() as s:
        a = _anime(s, pref_source=LOCK, total_episodes=12)
        for ep in range(1, 6):
            _row(s, a.id, LOCK, ep, "sent", _d(80 - 7 * ep))
        for ep in range(1, 10):
            _row(s, a.id, "ANi", ep, "skipped" if ep <= 5 else "pending", _d(81 - 7 * ep))
    (f,) = _lag()
    assert "自动兜底被挡住了" not in f["text"] and "满 14 天" in f["text"]


def test_lock_lag_alert_stays_read_until_lock_changes(clean_tables, env):
    with clean_tables.get_session() as s:
        aid = _shape8(s)
    (f,) = _lag()
    alerts.ack(f["ident"], f["kind"], f["fact"], f["text"])
    assert _lag() == []
    with clean_tables.get_session() as s:            # 别的组照常来新集：不是新事实
        _row(s, aid, "ANi", 26, "pending", _d(31))
        _row(s, aid, LOCK, 17, "sent", _d(1))         # 锁定组跟上一集、仍落后：也不复活
    assert _lag() == []
    with clean_tables.get_session() as s:            # 改锁了别的组：复活，并说清变了什么
        a = s.get(Anime, aid)
        a.pref_source = "Nix-Raws"
        s.add(a)
        s.commit()
        _row(s, aid, "ANi", 27, "pending", _d(24))
    (f2,) = _lag()
    assert f2["revived"] and ("锁定的下载源", LOCK, "Nix-Raws") in f2["revived"]["changed"]


def test_lock_lag_alert_disappears_once_lock_catches_up(clean_tables, env):
    with clean_tables.get_session() as s:
        aid = _shape8(s)
        assert len(_lag()) == 1
        for ep in range(17, 26):
            _row(s, aid, LOCK, ep, "sent", _d(2))
    assert _lag() == []


def test_lock_lag_alert_revives_when_lagging_again_after_catch_up(clean_tables, env):
    """标已读时被挡的那批集后来都下上了（提示消失），之后同一个锁定组又落后：现在挡着的全是标已读之后才入库的，是新事实。"""
    with clean_tables.get_session() as s:
        aid = _shape8(s)
    (f,) = _lag()
    alerts.ack(f["ident"], f["kind"], f["fact"], f["text"])
    with clean_tables.get_session() as s:
        row = s.exec(select(AlertAck).where(AlertAck.ident == f["ident"])).one()
        row.created_at = _d(30)
        s.add(row)
        s.commit()
        assert _lag() == [], "前提：只是标了已读，那批集还挡着，不复活"
        for ep in range(17, 26):
            _row(s, aid, LOCK, ep, "sent", _d(28))
    assert _lag() == [], "锁定组追上了，提示消失"
    with clean_tables.get_session() as s:
        _row(s, aid, "ANi", 26, "pending", _d(20))
        _row(s, aid, "ANi", 27, "pending", _d(13))
    (f2,) = _lag()
    assert f2["fact"] == f["fact"], "指纹没变，复活靠的是 since"
    assert f2["revived"] and f2["revived"]["changed"][0][0] == "被挡住的集"


def test_lock_lag_alert_off_when_lag_days_zero(clean_tables, env):
    env(ANIME_LOCK_LAG_DAYS=0)
    with clean_tables.get_session() as s:
        _shape8(s)
    assert _lag() == []


# ---------------------------------------------------------------- PR-4

def test_pending_of_ignored_and_orphan_shows_own_bucket(clean_tables, env):
    with clean_tables.get_session() as s:
        a = _anime(s, rejected=True, confirmed=False)
        _row(s, a.id, "ANi", 1, "pending", _d(3))
        _row(s, 999999, "ANi", 2, "pending", _d(3))
        b = _anime(s, title="在追", display_name="在追", bangumi_id=9)
        _row(s, b.id, "ANi", 1, "sent", _d(3))
        _row(s, b.id, "Nix", 1, "pending", _d(3))     # 同集已有：备用项
    p = A.pending_breakdown()
    assert (p["ignored"], p["backup"]) == (2, 1), p


# ---------------------------------------------------------------- perf-1 / 仪表盘

async def _until(pred, tries=100):
    for _ in range(tries):
        if pred():
            return True
        await asyncio.sleep(0.05)
    return pred()


def _ids(user, kind, starts):
    return {e.id for e in user.find(kind).elements if not e.is_deleted and str(e.text).startswith(starts)}


def _snapshot(user):
    return {"head": _ids(user, ui.label, "订阅中"), "tail": _ids(user, ui.label, "种子状态"),
            "inflight": _ids(user, ui.label, "正在下载（"), "recent": _ids(user, ui.label, "新入库（"),
            "chips": {e.text for e in user.find(ui.badge).elements if not e.is_deleted}}


async def _tick(user):
    t = next(e for e in user.find(ui.timer).elements if not e.is_deleted and e.interval == 30.0)
    t.callback()
    await asyncio.sleep(0.1)          # 重建后旧元素的删除要等一拍


@pytest.mark.nicegui_main_file(_MAIN)
async def test_timer_skips_rebuild_when_nothing_changed(user: User, clean_tables, env):
    with clean_tables.get_session() as s:
        a = _anime(s, rejected=True, confirmed=False)
        _row(s, a.id, "ANi", 1, "pending", _d(3))
        bid = _anime(s, title="在追", display_name="在追", bangumi_id=9).id
        tid = _row(s, bid, "ANi", 1, "sent", _d(3), qb_state="downloading", qb_progress=0.1, qb_hash="h1")
    await user.open("/?t=overview")
    await user.should_see("正在下载（")
    s0 = _snapshot(user)
    assert all(s0[k] for k in ("head", "tail", "inflight", "recent")), s0
    assert "已忽略番 1" in s0["chips"], "PR-4：已忽略番的待下要单独一档"
    await _tick(user)
    assert _snapshot(user) == s0, "数据没变，四块都不该重建"

    with clean_tables.get_session() as s:            # qB 进度变了：tail（完成率）与正在下载区要重建
        t = s.get(AnimeTorrent, tid)
        t.qb_progress = 0.6
        s.add(t)
        s.commit()
    await _tick(user)
    s1 = _snapshot(user)
    assert s1["inflight"] != s0["inflight"] and s1["tail"] != s0["tail"]
    assert s1["recent"] != s0["recent"], "新入库表里的徽标带进度，也得重建"
    # head 按整份 overview() 比（粗一点但不会漏刷），qB 汇总变了它也跟着重建，这里不钉

    with clean_tables.get_session() as s:            # 新入库一条：KPI（种子数）与新入库表都要重建
        _row(s, bid, "ANi", 2, "pending", _d(0))
    await _tick(user)
    s2 = _snapshot(user)
    assert s2["head"] != s1["head"] and s2["recent"] != s1["recent"]


@pytest.mark.nicegui_main_file(_MAIN)
async def test_timer_rebuilds_tail_when_time_based_text_changes(user: User, clean_tables, env, monkeypatch):
    """『上次采集』是按此刻算的（超时变红）：库里一行没动，它变了也得重建。"""
    await user.open("/?t=overview")
    await user.should_see("种子状态")
    s0 = _snapshot(user)
    await _tick(user)
    assert _snapshot(user)["tail"] == s0["tail"]
    monkeypatch.setattr(sys.modules["pages.anime"], "poll_stale", lambda *a, **k: True)
    await _tick(user)
    assert _snapshot(user)["tail"] != s0["tail"]
