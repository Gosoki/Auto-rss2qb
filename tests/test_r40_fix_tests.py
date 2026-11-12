"""现有守卫守不住的判据，以及测试夹具自身的安全网。

每条都对应一处"把生产代码 / 夹具里的那个判据删掉，全套照样全绿"的缺口：
  · 锁定源兜底（E-74）：交付前复核 _lock_fallback_ok 的五问、本轮新断档传进计划、候选行自己的闸、
    锁定组中间跳过的集、别季行不当证据、None 整轮停手；
  · 缺集尾巴 / 断更豁免 / 修正版发布时间差的 abs / 采集轮四处接线 / 搬迁三道老闸；
  · 两个阈值设置（放行窗口、缺集提醒冷却）真的读设置；
  · conftest：会话闸覆盖全部模块级绑定、模块级状态与源组表每条用例复位、写盘目录默认在一次性目录、
    临时库目录在会话结束时删掉。
造数函数与夹具直接复用对应的 test_r39_* 文件（按模块名 import，拿到的是 pytest 收集的同一个模块对象）。
"""
import asyncio
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlmodel import select

# 【必须在模块顶层】与 tests/test_alert_ack.py 一样在收集期导入：第一次导入若发生在用例里，
# 那时 db.get_session 已被闸换掉、它碰巧绑到闸上，会话闸的覆盖守卫就成了真空
from core import alerts  # noqa: F401

import test_r39_download as D
import test_r39_foreign_season as F
import test_r39_lock_fallback as L
import test_r39_notify as T
import test_r39_relocate as R
import test_r39_sweeps as S
from test_r39_lock_fallback import env  # noqa: F401  （夹具）
from test_r39_notify import poll  # noqa: F401  （夹具）

from core import anime as A
from db.models import Anime, AnimeTorrent

_ROOT = Path(__file__).resolve().parent.parent


# ══ 锁定源兜底（E-74）════════════════════════════════════════════════════════

def _mutate_on_first_delivery(clean_tables, fn):
    """第一次交付『期间』做一件事（模拟取种那 180 秒里用户动了什么），之后的交付照常返回 True。"""
    seen = []

    def result(tid, force):
        if not seen:
            with clean_tables.get_session() as s:
                fn(s)
                s.commit()
        seen.append(tid)
        return True
    return result


# ── _lock_fallback_ok：交付前当场复核 ──────────────────────────────────────────

@pytest.mark.parametrize("what", ["ignored", "unconfirmed", "keyword", "fb_days_off"])
async def test_b_step_rechecks_everything_before_each_delivery(clean_tables, env, monkeypatch, what):
    """B 步用 prefs_gate=False 越过了交付里的放行前复核，_lock_fallback_ok 是它【唯一】的复核：
    第 6 集交付期间番被忽略 / 打回待确认 / 设了不命中的关键词 / 兜底天数被改 → 第 7~9 集不许再按旧计划下。"""
    with clean_tables.get_session() as s:
        aid, ids = L._shape1(s)

    def change(s):
        a = s.get(Anime, aid)
        if what == "ignored":
            a.rejected = True
        elif what == "unconfirmed":
            a.confirmed = False
        elif what == "keyword":
            a.pref_keyword = "简日"
        elif what == "fb_days_off":
            env(ANIME_LOCK_FALLBACK_DAYS=30)      # 用户把兜底天数从 14 改成了 30
        s.add(a)
    calls, _ = L._fake_delivery(monkeypatch, clean_tables, result=_mutate_on_first_delivery(clean_tables, change))
    assert await A.deliver_lock_fallback(qb_alive=True) == 1
    assert calls == [(ids[("ANi", 6)], False)], f"{what}：交付期间变了，后面的集仍按旧计划下了"


@pytest.mark.parametrize("what", ["excluded", "foreign", "already_sent"])
async def test_b_step_rechecks_the_row_itself(clean_tables, env, monkeypatch, what):
    """第 6 集交付期间，第 7 集那一条被用户排除 / 标成『不属于本季』/ 已经被别的路径下了 → 跳过它，第 8、9 集照常。"""
    with clean_tables.get_session() as s:
        aid, ids = L._shape1(s)
    t7 = ids[("ANi", 7)]

    def change(s):
        t = s.get(AnimeTorrent, t7)
        if what == "excluded":
            t.status = "excluded"
        elif what == "foreign":
            t.foreign_season = True
        else:
            t.status = "sent"
        s.add(t)
    calls, _ = L._fake_delivery(monkeypatch, clean_tables, result=_mutate_on_first_delivery(clean_tables, change))
    await A.deliver_lock_fallback(qb_alive=True)
    assert t7 not in [c[0] for c in calls], f"{what}：这一条在交付期间已经变了，仍被交付"
    assert not L._get(clean_tables, t7).lock_fallback


def _two_caught_up(s):
    """兜底过第 6、7 集；锁定组 3 小时前把这两集都补发了。"""
    a = L._anime(s)
    ids = {}
    for e in range(1, 8):
        ids[("ANi", e)] = L._row(s, a.id, "ANi", e, "sent" if e >= 6 else "skipped", L._d(75 - 7 * (e - 1)),
                                 lock_fallback=(e >= 6))
    for e in range(1, 6):
        ids[(L.LOCK, e)] = L._row(s, a.id, L.LOCK, e, "sent", L._d(75 - 7 * (e - 1) - 1))
    for e in (6, 7):
        ids[(L.LOCK, e)] = L._row(s, a.id, L.LOCK, e, "pending", L._d(0, hours=3))
    return a.id, ids


# excluded / deleted 两个参数单独去掉哪一个判据都打不红（另一个判据兜着），
# 但两者同时去掉时只有它们红 —— 它们是『force 之前再核锁定组那条没被排除 / 删除』唯一的守卫，别当凑数删
@pytest.mark.parametrize("what", ["excluded", "deleted", "foreign", "ignored", "keyword"])
async def test_a_step_rechecks_before_each_forced_delivery(clean_tables, env, monkeypatch, what):
    """A 步走 force：同集闸、订阅闸、『不属于本季』总闸全都越过，_lock_fallback_ok 是唯一的复核。
    第 6 集交付期间，用户排除 / 删了锁定组第 7 集那一条、把它标成别季、忽略了这部番、或设了不命中的关键词 → 第 7 集不许 force。"""
    with clean_tables.get_session() as s:
        aid, ids = _two_caught_up(s)
    l7 = ids[(L.LOCK, 7)]

    def change(s):
        t, a = s.get(AnimeTorrent, l7), s.get(Anime, aid)
        if what in ("excluded", "deleted"):
            t.status = what
        elif what == "foreign":
            t.foreign_season = True
        elif what == "ignored":
            a.rejected = True
        else:
            a.pref_keyword = "简日"
        s.add(t)
        s.add(a)
    calls, _ = L._fake_delivery(monkeypatch, clean_tables, result=_mutate_on_first_delivery(clean_tables, change))
    assert await A.deliver_lock_fallback(qb_alive=True) == 1
    assert calls == [(ids[(L.LOCK, 6)], True)], f"{what}：第 7 集仍被 force 下了"


# ── 本轮新断档必须一路传到计划里（worker → deliver → plan 的中间那一环）──────────────────

async def test_new_gaps_of_this_round_reach_the_plan(clean_tables, env, monkeypatch):
    """采集停摆后恢复的那一轮：断档还没落库（worker 轮末才写 FEED_GAPS），只在 new_gaps 里。
    deliver_lock_fallback 不把它传给 lock_fallback_plan 的话，兜底正好在这一轮触发（docstring 里的 #10）。"""
    with clean_tables.get_session() as s:
        L._shape1(s)
    inside = {"src": "nyaa", "from": L._d(30).isoformat(timespec="minutes"),
              "to": L._d(20).isoformat(timespec="minutes"), "at": L.NOW.isoformat(timespec="minutes")}
    calls, _ = L._fake_delivery(monkeypatch, clean_tables)
    assert await A.deliver_lock_fallback(qb_alive=True, new_gaps=[inside]) == 0 and calls == []


def test_a_gap_straddling_the_lock_groups_last_row_blocks(clean_tables, env):
    """断档从锁定组最新入库【之前】开始、之后才结束：同样与［锁定组最新入库, 现在］相交。"""
    with clean_tables.get_session() as s:
        L._shape1(s)
    g = {"src": "nyaa", "from": L._d(60).isoformat(timespec="minutes"),
         "to": L._d(40).isoformat(timespec="minutes"), "at": L.NOW.isoformat(timespec="minutes")}
    env(FEED_GAPS=json.dumps([g]))
    assert A.lock_fallback_plan() == {}


# ── 候选行自己的闸 ─────────────────────────────────────────────────────────────

def test_candidate_without_release_time_is_not_picked(clean_tables, env):
    """候选那一行自己没有发布时间（k 在锁定组最新一集之后，『发布时间一致』闸只看两边共有的集、看不到它）
    → 单调闸判不了，不要；别的集照常。"""
    with clean_tables.get_session() as s:
        aid, _ = L._shape1(s, ani_rel={7: None})
    assert L._plan_eps(A.lock_fallback_plan(), aid) == [6, 8, 9]


def test_candidate_still_in_retry_backoff_is_not_picked(clean_tables, env):
    """取种失败、还在退避排队的不抢跑（同 flush）。"""
    with clean_tables.get_session() as s:
        aid, ids = L._shape1(s)
        t = s.get(AnimeTorrent, ids[("ANi", 6)])
        t.retry_count, t.retry_at = 1, L.NOW + timedelta(hours=2)
        s.add(t)
        s.commit()
    assert L._plan_eps(A.lock_fallback_plan(), aid) == [7, 8, 9]


def test_non_pending_rows_are_not_candidates(clean_tables, env):
    """别的组在这一集只有失败 / 跳过的行：不是候选（计划的 eps 还要给提醒文案和详情页用，不能混进不会下的行）。"""
    with clean_tables.get_session() as s:
        aid, ids = L._shape1(s)
        for e, st in ((6, "error"), (7, "skipped")):
            t = s.get(AnimeTorrent, ids[("ANi", e)])
            t.status = st
            s.add(t)
        s.commit()
    assert L._plan_eps(A.lock_fallback_plan(), aid) == [8, 9]


def test_absolute_numbering_season_is_never_filled(clean_tables, env):
    """k≤T 这道闸自己的用例：全库都是绝对编号（13~24，T=12，没有 1..T 内的行）→ 两套编号冲突闸是空的，
    只有 k>T 挡得住。test_r39_lock_fallback 里 `total_episodes=8` 那条参数化同时命中编号冲突闸，删掉 k>T 它照样绿。"""
    with clean_tables.get_session() as s:
        a = L._anime(s)
        for e in range(13, 24):
            L._row(s, a.id, "ANi", e, "skipped" if e <= 17 else "pending", L._d(75 - 7 * (e - 13)), priority=10)
        for e in range(13, 18):
            L._row(s, a.id, L.LOCK, e, "sent", L._d(75 - 7 * (e - 13) - 1))
        aid = a.id
        a = s.get(Anime, aid)
        assert A.episode_numbering_conflict(aid) == [], "前提：编号冲突闸在这个形状上是空的"
    assert aid in A.locked_out_episodes([a], lag_days=14), "前提：提醒是会报的"
    assert L._plan_eps(A.lock_fallback_plan(), aid) == []


# ── 锁定组中间跳过的集（_lock_hole）──────────────────────────────────────────────

def _hole_shape(s, other_eps=range(1, 13)):
    """锁喵萌：发了 1~5、7~12（第 6 集一直没有，最新一条 20 天前）；ANi 按 other_eps 周更、每集早一天。"""
    a = L._anime(s)
    ids = {}
    for e in other_eps:
        ids[("ANi", e)] = L._row(s, a.id, "ANi", e, "pending" if e == 6 else "skipped",
                                 L._d(100 - 7 * (e - 1)), priority=10)
    for e in [*range(1, 6), *range(7, 13)]:
        ids[(L.LOCK, e)] = L._row(s, a.id, L.LOCK, e, "sent", L._d(100 - 7 * (e - 1) - 1))
    return a.id, ids


def test_hole_episode_is_filled_from_a_group_on_the_same_numbering(clean_tables, env):
    with clean_tables.get_session() as s:
        aid, ids = _hole_shape(s)
    plan = A.lock_fallback_plan()
    assert plan[aid]["eps"] == {6: [(ids[("ANi", 6)], "ANi")]}


def test_hole_episode_needs_the_group_to_have_the_locks_latest_episode(clean_tables, env):
    """别的组没发过锁定组最新那一集（编号是否同源无从核对）→ 中间跳过的集也不拿它补。"""
    with clean_tables.get_session() as s:
        aid, _ = _hole_shape(s, other_eps=range(1, 9))     # ANi 只到第 8 集，没有第 12 集
    assert L._plan_eps(A.lock_fallback_plan(), aid) == []


def test_hole_episode_needs_an_anchor_inside_the_group(clean_tables, env):
    """别的组在第 6 集之前一集都没有（没有锚集）→ 单调闸判不了，不要。"""
    with clean_tables.get_session() as s:
        aid, _ = _hole_shape(s, other_eps=[6, 12])
    assert L._plan_eps(A.lock_fallback_plan(), aid) == []


# ── 『不属于本季』的行不许当证据 ────────────────────────────────────────────────

def test_foreign_rows_are_not_evidence_for_the_same_numbering_gate(clean_tables, env):
    """ANi 的 1~5 被用户标成了『不属于本季』（前一季的同号集）：它就没发过锁定组最新那一集（第 5 集），
    编号同源无从核对 → 不兜底。把别季行取进来的话，这几行会被当成"ANi 发过第 5 集"的证据。"""
    with clean_tables.get_session() as s:
        aid, ids = L._shape1(s)
        for e in range(1, 6):
            t = s.get(AnimeTorrent, ids[("ANi", e)])
            t.foreign_season = True
            s.add(t)
        s.commit()
    assert L._plan_eps(A.lock_fallback_plan(), aid) == []


# ── None（qB 掉线 / 目录配错）整轮停手：跨番 ─────────────────────────────────────

async def test_none_stops_the_other_animes_too(clean_tables, env, monkeypatch):
    with clean_tables.get_session() as s:
        L._shape1(s)
        L._shape1(s, title="另一番", display_name="另一番", bangumi_id=2)
    calls, _ = L._fake_delivery(monkeypatch, clean_tables, result=lambda tid, force: None)
    assert await A.deliver_lock_fallback(qb_alive=True) == 0
    assert len(calls) == 1, "第一部番已经返回 None（系统性失败），第二部番不该再试"


async def test_none_in_a_step_skips_b_step(clean_tables, env, monkeypatch):
    with clean_tables.get_session() as s:
        L._caught_up_shape(s)
        L._shape1(s, title="另一番", display_name="另一番", bangumi_id=2)
    calls, _ = L._fake_delivery(monkeypatch, clean_tables, result=lambda tid, force: None)
    assert await A.deliver_lock_fallback(qb_alive=True) == 0
    assert len(calls) == 1 and calls[0][1] is True, "A 步已经返回 None，B 步不该再跑"


# ══ 缺集尾巴 / 断更豁免 / 修正版 abs ══════════════════════════════════════════

def test_tail_cut_keeps_a_hole_that_is_above_the_cap(cfg):
    """test_r39_sweeps 里 test_tail_from_never_touches_middle_holes 的洞是第 3 集，而尾巴上限是 5 ——
    洞本来就在上限之内，把 `k <= hi or k <= tail_max` 写成 `k <= tail_max` 它照样绿。洞要挖在 (上限, max(seen)) 之间才分得出来。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    today = S._D + timedelta(weeks=9)
    seen = set(range(1, 8)) - {6}             # hi=7，尾巴上限 7+0−2=5，洞在第 6 集
    assert A.missing_episodes(S._a8(), seen, today, tail_from=today) == [6]


def test_tail_cap_subtracts_the_slack(cfg):
    """尾巴上限 = hi + (今天 − 锚点) 周 − 宽限。test_r39_sweeps 的锚点全是『今天』或恰好按排期，宽限减不减结果都一样。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    today = S._D + timedelta(weeks=10)
    seen = set(range(1, 8))
    assert A.missing_episodes(S._a8(), seen, today) == [8, 9], "前提：不给锚点时按首播日推会报"
    # 第 7 集一周前才发：第 8 集还在宽限内，不报
    assert A.missing_episodes(S._a8(), seen, today, tail_from=today - timedelta(weeks=1)) == []
    # 第 7 集三周前发的：上限 7+3−2=8，报第 8 集、不报第 9 集
    assert A.missing_episodes(S._a8(), seen, today, tail_from=today - timedelta(weeks=3)) == [8]


async def test_idle_exemption_needs_every_episode(clean_tables, cfg, monkeypatch):
    """终集按周更早该播了、却只见过 11/12 集、已安静 15 天：这正是"终集一直没来"，必须照报。
    test_r39_sweeps 里两部番都是 12/12，把 `>= total` 写成 `>= total - 1` 照样绿。"""
    monkeypatch.setattr(A, "notify_event", lambda k, m, **kw: S._async(True))
    cfg(ANIME_FINISH_ENABLED=False, ANIME_IDLE_DAYS=14, ANIME_GAP_SLACK_WEEKS=2, ANIME_POLL_OK_LAST="")
    aid = S._mk(clean_tables, total=12, air=date.today() - timedelta(weeks=13),
                rows=[(e, "sent", None) for e in range(1, 12)])
    with clean_tables.get_session() as s:
        for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid)):
            t.created_at = datetime.now() - timedelta(days=15)
            s.add(t)
        s.commit()
    assert await A.sweep_idle() == 1
    assert S._anime(clean_tables, aid).idle_notified_at is not None, "差终集的番被当成『播完且集齐』豁免了"


def test_a_v2_released_long_before_the_old_version_is_not_a_fix(clean_tables, cfg, monkeypatch):
    """test_r39_download 的 test_release_gap_uses_absolute_value 只测了"早 8 小时照常替换"—— 去掉 abs() 它照样绿（负数恒小于门槛）。
    abs 真正要挡的是这一头："v2"比我们那份早了一个多季度（别季同集号挂错番）：不下、不删。"""
    cfg(ANIME_REPLACE_MAX_GAP_DAYS=30)
    ids, _h, calls = D._versions(clean_tables, cfg, monkeypatch, [
        ("v1", D.SUB, "sent", {}), ("v2", D.SUB_V2, "skipped", dict(release_time=D.OLD - timedelta(days=100))),
    ], {"v1": "E:/A/41.mp4", "v2": "E:/A/41 v2.mp4"})
    assert asyncio.run(A.replace_versions()) == (0, 0)
    assert calls == []


# ══ 采集轮（worker.poll_once）的四处接线 ══════════════════════════════════════

async def test_a_new_gap_is_written_to_feed_gaps(poll):
    """发现断档的那一轮必须把它写进 FEED_GAPS：仪表盘横幅、锁定源兜底的断档闸读的都是这个键
    （推送只发一次，而且推送地址可能没配 —— 不落库就等于没人知道）。"""
    now = datetime.now(timezone.utc)
    prev, o1 = T._sec(now - timedelta(days=15)), T._sec(now - timedelta(days=9))
    poll.health.mark_seen(T._URL, prev)
    await poll.run(poll.source("ANi", T._URL, o1, ["ok"]))
    assert poll.gaps == [(prev, o1)], "前提：这一轮判出了断档"
    written = [w["FEED_GAPS"] for w in poll.writes if "FEED_GAPS" in w]
    assert written, "断档只推送、没落库"
    rec = json.loads(written[-1])
    assert len(rec) == 1 and rec[0]["src"] == "ANi", rec


async def test_maintenance_mid_round_leaves_seen_untouched(poll, monkeypatch):
    """判断档那一步不许顺手把 seen 写了：整轮被维护打断（DatabaseBusy）时条目一条没入库，
    seen 必须原地不动，下一轮才会重新算出这段断档。"""
    import db
    from core import worker as W

    async def busy(item, known_hashes=None, qb_alive=None):
        raise db.DatabaseBusy("维护中")
    monkeypatch.setattr(W, "process_item", busy)
    now = datetime.now(timezone.utc)
    prev, o1 = T._sec(now - timedelta(days=15)), T._sec(now - timedelta(days=9))
    poll.health.mark_seen(T._URL, prev)
    with pytest.raises(db.DatabaseBusy):
        await poll.run(poll.source("ANi", T._URL, o1, ["ok"]))
    assert poll.health.seen_at(T._URL) == prev, "整轮早退了，seen 却已经推进：下一轮判不出这段断档"


async def test_a_recovered_source_clears_its_fail_streak(poll):
    """抓到了就清零：否则恢复之后『连续失败』永远挂着，锁定源兜底的 B 步也永远被 sources_failing 挡住。"""
    for _ in range(3):
        poll.health.record_source("ANi", False, "403")
    assert poll.health.fail_streak("ANi") == 3
    await poll.run(poll.source("ANi", T._URL, datetime.now(timezone.utc) - timedelta(days=1), ["ok"]))
    assert poll.health.fail_streak("ANi") == 0


async def test_a_group_that_fails_to_build_keeps_counting_across_rounds(poll, monkeypatch):
    """构造阶段就被跳过的组（feed 为空 / 未知站点）也要一轮一轮数上去，数不到阈值就永远不告警。"""
    from core import worker as W
    monkeypatch.setattr(W, "_build_skipped", ["坏配置的组"])
    src = poll.source("ANi", T._URL, datetime.now(timezone.utc) - timedelta(days=1), ["ok"])
    await poll.run(src)
    await poll.run(src)
    assert poll.health.fail_streak("坏配置的组") == 2


# ══ 搬迁（engine.relocate）三道老闸 ═══════════════════════════════════════════
# 三道闸旁边的注释写的都是同一个结局："报告让用户去删旧文件，删掉的就是唯一的一份 / qB 正在写的那份"

async def test_unreachable_qb_touches_nothing(clean_tables, cfg, monkeypatch):
    """qB 连不上（torrents_info 返回 None）≠ qB 说不认识这些 hash：一行都不许清成待下，报告里带 error。"""
    from core import engine as ce
    R._setup(cfg)
    with clean_tables.get_session() as s:
        aid = R._anime(s, [("a", 1, "sent", R.OLD), ("b", 2, "sent", R.OLD), ("c", 3, "stalled", R.OLD)])

    async def down(hashes):
        return None
    monkeypatch.setattr(ce.qb, "torrents_info", down)
    before = R._status(clean_tables, aid)
    rep = await A.relocate_anime(aid, old_path=R.OLD)
    assert R._status(clean_tables, aid) == before, "qB 连不上时行被改了：下一轮会整部重发，而文件一个都没动"
    assert rep.get("error") and "连不上" in rep["error"], rep
    assert (rep["moved"], rep["redownload"], rep["untracked"]) == (0, 0, 0), rep


async def test_archived_rows_are_left_alone(clean_tables, cfg, monkeypatch):
    """已归档的集不在 qB 里（本来就该不认识）：不许当成『qB 不认识 → 清成待下』，否则整季归档过的集全部重下一遍。"""
    R._setup(cfg)
    with clean_tables.get_session() as s:
        aid = R._anime(s, [("a", 1, "sent", R.OLD), ("b", 2, "sent", R.OLD)])
        for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid)):
            if t.info_hash[0] == "b":
                t.archived_at = datetime.now()
                s.add(t)
        s.commit()
    R._qb(monkeypatch, known="")
    rep = await A.relocate_anime(aid, old_path=R.OLD)
    got = R._status(clean_tables, aid)
    assert got["b"] == ("sent", R.OLD), f"已归档的集被清成待下了：{got}"
    assert got["a"][0] == "pending" and rep["redownload"] == 1, (got, rep)


async def test_delivering_placeholder_is_left_alone(clean_tables, cfg, monkeypatch):
    """交付中的占位行（downloading：还没 add 进 qB）问 qB 必然查不到：不许清成待下，报告里单独计数。"""
    R._setup(cfg)
    with clean_tables.get_session() as s:
        aid = R._anime(s, [("a", 1, "sent", R.OLD), ("b", 2, "downloading", R.OLD)])
    R._qb(monkeypatch, known="")
    rep = await A.relocate_anime(aid, old_path=R.OLD)
    got = R._status(clean_tables, aid)
    assert got["b"] == ("downloading", R.OLD), f"交付中的占位行被动了：{got}"
    assert rep["delivering"] == 1 and rep["redownload"] == 1, rep


# ══ 阈值设置真的读设置 ═══════════════════════════════════════════════════════

def test_flush_grace_window_follows_the_setting(clean_tables, cfg, monkeypatch, tmp_path):
    """放行窗口：入库 45 分钟的待下种子，窗口 120 分钟时还在等；调到 30 分钟就放行。
    主交付路径的窗口别处没有用例（都是 GRACE=0 + 3 小时前入库），整句判断删掉也照样绿。"""
    F._real_delivery(cfg, monkeypatch, tmp_path)
    d = date.today() - timedelta(weeks=5)
    F._mk(clean_tables, [("a", 3, "pending", F._at(d, 2), {"created_at": datetime.now() - timedelta(minutes=45)})],
          air=d)
    cfg(ANIME_DOWNLOAD_GRACE_MIN=120)
    assert asyncio.run(A.flush_ready_downloads(qb_alive=True)) == 0, "入库 45 分钟、窗口 120 分钟：还该等"
    cfg(ANIME_DOWNLOAD_GRACE_MIN=30)
    assert asyncio.run(A.flush_ready_downloads(qb_alive=True)) == 1, "窗口调到 30 分钟没放行：读的不是设置"


def test_gap_alert_cooldown_follows_the_setting(clean_tables, cfg, monkeypatch):
    """缺集提醒交给 notify_event 的冷却 = ALERT_REPEAT_DAYS 天（默认 7，这里设 3 才分得出来）。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100, NOTIFY_EVENTS=["gap"], NOTIFY_URL="https://x",
        ANIME_FINISH_UNSUB=False, ALERT_REPEAT_DAYS=3)
    S._six_like(clean_tables, tail_status=None)
    seen = []

    async def fake(kind, msg, key=None, cooldown=None, **kw):
        seen.append(cooldown)
        return True
    monkeypatch.setattr(A, "notify_event", fake)
    assert asyncio.run(A.sweep_gaps()) == 1
    assert seen == [3 * 86400]


# ══ conftest 自身 ═══════════════════════════════════════════════════════════

def test_every_module_level_session_binding_is_guarded():
    """生产代码里凡是模块级 `from db import get_session / get_meta_session` 的模块，都必须被 _guard_every_session 换掉。
    清单是手写的：漏一个，那个模块的写库函数在 db.engine 被指到开发库时照写不误（2026-08-31 事故同一形状）。"""
    import importlib
    import pkgutil
    leaks = []
    for pkg in ("core", "services", "sources", "pages"):
        p = importlib.import_module(pkg)
        for mi in pkgutil.iter_modules(p.__path__):
            try:
                m = importlib.import_module(f"{pkg}.{mi.name}")
            except Exception:
                continue
            for name in ("get_session", "get_meta_session"):
                f = getattr(m, name, None)
                # 被闸包过的是 conftest 里的闭包 _s/_m；没包过的就是 db 包里的原函数
                if f is not None and getattr(f, "__module__", "") == "db":
                    leaks.append(f"{pkg}.{mi.name}.{name}")
    assert not leaks, f"这些模块级会话入口没被 _guard_every_session 换掉：{leaks}"


def test_reset_process_state_really_resets(testdb):
    """把每一项都弄脏 → 调 reset_process_state → 必须全部回到初值；漏复位一项就点名是哪一项。"""
    from conftest import reset_process_state
    import services.notify as N
    from core import engine as E, health as H, movies as M, worker as W

    A._wb_notified = {1}; A._qb_down_rounds = 3; A._meta_due.add(1)
    E._path_miss.add("x"); E.qb_kick.set(); E._sync_locks[int] = object()
    H._seen = {"u": 1}
    W._build_skipped.append("x"); W._stale_sweep_pending = True
    M._scan_fail_streak, M._scan_retry_at = 2, datetime.now()
    N._last_sent[("a", "b")] = 1.0; N._state_now["x"] = True; N._state_members["x"] = frozenset()
    N._state_times["x"] = [1.0]; N._state_suppressed["x"] = True; N._sent_times.append(1.0)
    N._dropped, N._fail_streak, N._muted_until = 1, 2, 9e18
    reset_process_state()
    left = {
        "A._wb_notified": A._wb_notified, "A._qb_down_rounds": A._qb_down_rounds, "A._meta_due": A._meta_due,
        "E._path_miss": E._path_miss, "E.qb_kick": E.qb_kick.is_set(), "E._sync_locks": E._sync_locks,
        "H._seen": H._seen,
        "W._build_skipped": W._build_skipped, "W._stale_sweep_pending": W._stale_sweep_pending,
        "M._scan_fail_streak": M._scan_fail_streak, "M._scan_retry_at": M._scan_retry_at,
        "N._last_sent": N._last_sent, "N._state_now": N._state_now, "N._state_members": N._state_members,
        "N._state_times": N._state_times, "N._state_suppressed": N._state_suppressed, "N._sent_times": N._sent_times,
        "N._dropped": N._dropped, "N._fail_streak": N._fail_streak, "N._muted_until": N._muted_until,
    }
    dirty = {k: v for k, v in left.items() if v}
    assert not dirty, f"这些模块级状态没被复位：{dirty}"


def test_clean_tables_also_clears_source_groups(testdb, request):
    """上一条用例留下的源组（如『坏配置的组』）会被下一条的采集轮当成失败的源。"""
    from db.models import SourceGroup
    with testdb.get_session() as s:
        s.add(SourceGroup(name="上一条用例留下的组", site="mikan", feed_url="http://x.invalid/rss"))
        s.commit()
    request.getfixturevalue("clean_tables")
    with testdb.get_session() as s:
        assert s.exec(select(SourceGroup)).all() == [], "clean_tables 没清源组表"


# 下面两条靠文件内的执行顺序配对：前一条把根日志器调到 CRITICAL 后不还原，后一条断言 _reset_process_state
# 已在两条之间还原了它（不还原的话后面断言 caplog 的用例会收不到 INFO/WARNING）。必须相邻，顺序不能换。
def test_root_log_level_leak_setup():
    import logging
    logging.getLogger().setLevel(logging.CRITICAL)


def test_root_log_level_is_restored_between_tests():
    import logging
    assert logging.getLogger().level != logging.CRITICAL, "上一条用例改的根日志器级别没被还原"


def test_default_write_dirs_are_throwaway():
    """不靠各用例自己打补丁时，备份目录、日志文件、IP 过滤缓存也都落在系统临时目录下：
    新写一条忘了 monkeypatch 的用例，写不进仓库的 data/（backup_now 的 prune 会删掉那里的真备份）。
    backup.BACKUP_DIR 会被会话级夹具 _session_backup_dir 改掉，量不到 DATA_DIR 改向本身；
    backup.DATA_DIR 是 import 那一刻拿到的值，才是改向生效与否的证据。"""
    from conftest import _TMP_ROOT
    from core import ipfilter, logsetup
    from db import backup
    for what, p in (("备份目录", backup.BACKUP_DIR), ("备份目录的上级", backup.DATA_DIR),
                    ("日志文件", logsetup.LOG_PATH), ("IP 过滤缓存", ipfilter._DIR)):
        assert os.path.realpath(p).startswith(_TMP_ROOT + os.sep), f"{what}默认指向 {p}，不在一次性目录里"


def test_the_session_db_dir_is_removed_at_exit(tmp_path):
    """每次会话 mkdtemp 一个目录放临时库：进程退出时必须删掉，不然系统临时目录里会一直攒。"""
    code = ("import sys; sys.path.insert(0, 'tests'); import conftest, db; db.init_db(); "
            "assert conftest._TMP_DB.exists(); print(conftest._TMP_DB.parent)")
    # 'tests' 必须插在最前：否则 import 到的是仓库根那个只注册插件的 conftest.py
    r = subprocess.run([sys.executable, "-c", code], cwd=_ROOT, capture_output=True, text=True, timeout=60,
                       env={**os.environ, "TMPDIR": str(tmp_path)})
    assert r.returncode == 0, r.stderr[-2000:]
    made = Path(r.stdout.strip().splitlines()[-1])
    assert made.name.startswith("autorss-test-") and os.path.realpath(made).startswith(os.path.realpath(tmp_path))
    assert not made.exists(), f"会话结束后临时库目录还在：{made}"
