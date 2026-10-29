"""(r39 DEC2 / E-74) 锁定源停更兜底。

B 步（lock_fallback_plan）：锁定组满 ANIME_LOCK_FALLBACK_DAYS 天没新东西、一串核对闸全过的集，用别的组补一份并记 lock_fallback。
A 步（_lock_catchup_plan）：兜底过的集锁定组后来补发了 → force 下它的版本，兜底那份保留（误判的代价只是多一份）。
两步都挂在 deliver_lock_fallback 里，worker.poll_once 每轮调一次。
"""
import itertools
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import select

from core import anime as A
from db.models import Anime, AnimeTorrent

NOW = datetime.now()
_AUTO = object()
_seq = itertools.count(1)
LOCK = "喵萌奶茶屋"


def _d(days, hours=0):
    return NOW - timedelta(days=days, hours=hours)


@pytest.fixture
def env(cfg):
    cfg(ANIME_LOCK_FALLBACK_DAYS=14, ANIME_LOCK_LAG_DAYS=7, ANIME_LOCK_SYNC_DAYS=2, ANIME_FINISH_UNSUB=False,
        FEED_GAPS="", ANIME_DOWNLOAD_GRACE_MIN=60, QB_ENABLED=True)
    return cfg


def _anime(s, **kw):
    base = dict(title="某番", display_name="某番", season=1, quarter="26C", confirmed=True, rejected=False,
                total_episodes=12, bangumi_id=1, pref_source=LOCK)
    base.update(kw)
    a = Anime(**base)
    s.add(a)
    s.commit()
    s.refresh(a)
    return a


def _row(s, aid, src, ep, status, at, rel=_AUTO, title=None, **kw):
    n = next(_seq)
    t = AnimeTorrent(anime_id=aid, info_hash=f"{n:040x}", source=src, site="nyaa",
                     raw_title=title or f"[{src}] 某番 - {int(ep):02d} [1080p]", season=1, episode=float(ep),
                     status=status, download_url=f"https://x/{n}.torrent", created_at=at,
                     release_time=(at - timedelta(hours=1)) if rel is _AUTO else rel, **kw)
    s.add(t)
    s.commit()
    s.refresh(t)
    return t.id


def _shape1(s, ani_rel=None, **akw):
    """#1 碧蓝航线：锁喵萌，喵萌 1~5 之后停更（最新一条 46 天前）；ANi 1~11 周更、每集比喵萌早一天。
    ANi 第 6~9 集首次入库已满 14 天（40/33/26/19 天前），第 10、11 集还没满（12/5 天前）。"""
    a = _anime(s, **akw)
    ids = {}
    for e in range(1, 12):
        at = _d(75 - 7 * (e - 1))
        rel = (ani_rel or {}).get(e, _AUTO)
        ids[("ANi", e)] = _row(s, a.id, "ANi", e, "skipped" if e <= 5 else "pending", at, rel=rel, priority=10)
    for e in range(1, 6):
        ids[(LOCK, e)] = _row(s, a.id, LOCK, e, "sent", _d(75 - 7 * (e - 1) - 1))
    return a.id, ids


def _plan_eps(plan, aid):
    return sorted((plan.get(aid) or {}).get("eps", {}))


def _get(clean_tables, tid):
    with clean_tables.get_session() as s:
        t = s.get(AnimeTorrent, tid)
        s.expunge(t)
        return t


def _fake_delivery(monkeypatch, clean_tables, result=None):
    """download_anime_torrent 换成记账：result(tid, force) 决定返回值（默认 True）；True 时把行落成 sent（进 HAVE）。"""
    calls, sent_msgs = [], []

    async def fake_dl(tid, force=False, **kw):   # **kw：B 步传 prefs_gate=False（r39 D4）
        # (r39 F28) B 步全靠 prefs_gate=False 越过放行前复核（锁定源不等就挡）：谁删了它，生产上 B 步静默零交付，这里要红
        assert force or kw.get("prefs_gate") is False, kw
        calls.append((tid, force))
        ok = True if result is None else result(tid, force)
        if ok:
            with clean_tables.get_session() as s:
                t = s.get(AnimeTorrent, tid)
                t.status = "sent"
                s.add(t)
                s.commit()
        return ok

    async def fake_notify(kind, msg, **kw):
        sent_msgs.append((kind, msg))
        return True
    monkeypatch.setattr(A, "download_anime_torrent", fake_dl)
    monkeypatch.setattr(A, "notify_event", fake_notify)
    return calls, sent_msgs


# ── B 步：计划（纯计算）───────────────────────────────────────────

def test_shape1_picks_only_episodes_past_the_window(clean_tables, env):
    """① 只挑别的组首次入库满 14 天的集，挑的是 ANi；兜底天数、组名都算好了。"""
    with clean_tables.get_session() as s:
        aid, ids = _shape1(s)
    plan = A.lock_fallback_plan()
    assert _plan_eps(plan, aid) == [6, 7, 8, 9]
    assert plan[aid]["eps"][6] == [(ids[("ANi", 6)], "ANi")]
    assert plan[aid]["days"] == 46 and plan[aid]["lock"] == LOCK and plan[aid]["kw_missed"] == []


def test_lock_group_active_recently_means_no_fallback(clean_tables, env):
    """② 锁定组 3 天前还进过东西（哪一集、什么状态都算）→ 不兜底。"""
    with clean_tables.get_session() as s:
        aid, _ = _shape1(s)
        _row(s, aid, LOCK, -1, "pending", _d(3))
    assert A.lock_fallback_plan() == {}


@pytest.mark.parametrize("akw, extra", [
    (dict(total_episodes=None), []),                     # T 未知：编号冲突闸恒为空，不兜底
    (dict(ep_offset=2), []),                             # 有集号歧义段 (2, 12]
    (dict(total_episodes=8), []),                        # k>T（同时也是编号冲突：9~11 > 8）
    (dict(), [("ANi", e) for e in range(12, 26)]),       # #8 形状：集号一路到 25，超过 T=12 → 编号冲突
])
def test_anime_level_gates(clean_tables, env, akw, extra):
    """③ 番级闸：T 未知 / 歧义段 / k>T / 两套编号冲突，都不挑。"""
    with clean_tables.get_session() as s:
        aid, _ = _shape1(s, **akw)
        for src, e in extra:
            _row(s, aid, src, e, "pending", _d(60))
    assert _plan_eps(A.lock_fallback_plan(), aid) == []


def test_binding_looks_wrong_blocks(clean_tables, env):
    """③ 绑定看着不对（种子比所绑那一季的首播早了三年多：多半绑错了季）→ 不兜底。编号冲突闸在这个形状上是空的。"""
    with clean_tables.get_session() as s:
        aid, _ = _shape1(s, air_date="2030-01-06")
        a = s.get(Anime, aid)
        assert A.episode_numbering_conflict(aid) == [] and A.binding_looks_wrong(s, a)
    assert A.lock_fallback_plan() == {}


def test_two_numberings_blocked_by_same_numbering_gate(clean_tables, env):
    """④ #27：锁 Nix 1~7（停在 58 天前），ANi 发的是 13~19（锁定组停了之后才来）。ANi 没发过第 7 集 → 编号不同源，不挑。"""
    with clean_tables.get_session() as s:
        a = _anime(s, pref_source="Nix", total_episodes=24)
        for e in range(1, 8):
            _row(s, a.id, "Nix", e, "sent", _d(100 - 7 * (e - 1)))
        for e in range(13, 20):
            _row(s, a.id, "ANi", e, "pending", _d(50 - 7 * (e - 13)))
        aid = a.id
        a = s.get(Anime, aid)
    assert aid in A.locked_out_episodes([a], lag_days=14), "前提：提醒是会报的"
    assert _plan_eps(A.lock_fallback_plan(), aid) == []


def test_old_season_rows_blocked_by_monotonic_gate(clean_tables, env):
    """⑤ 串季污染：ANi 第 6~8 集的发布时间早于它自己的第 5 集（前一季的同号集）→ 不挑；第 9 集正常。"""
    old = _d(400)
    with clean_tables.get_session() as s:
        aid, _ = _shape1(s, ani_rel={6: old, 7: old, 8: old})
    assert _plan_eps(A.lock_fallback_plan(), aid) == [9]


def test_handled_episode_is_left_alone(clean_tables, env):
    """⑥ 这一集有 excluded 行、或被删成"该集已删"（deleted+ep_dropped）→ 用户处理过，不替他做主。
    只删了重复份的 deleted（ep_dropped=0）不算（r39 F18，与 _covered 同口径）：flush 和锁源缺集统计都认它没有着落，兜底照补。"""
    with clean_tables.get_session() as s:
        aid, _ = _shape1(s)
        _row(s, aid, "ANi", 6, "deleted", _d(40), ep_dropped=False)
        _row(s, aid, "绿茶字幕组", 7, "excluded", _d(33))
        _row(s, aid, "绿茶字幕组", 8, "deleted", _d(26), ep_dropped=True)
    assert _plan_eps(A.lock_fallback_plan(), aid) == [6, 9]


def test_keyword_is_a_hard_condition(clean_tables, env):
    """⑦ 版本关键词是硬条件：一个都不命中就不挑、记 kw_missed；有命中的只在命中的里挑。"""
    with clean_tables.get_session() as s:
        aid, ids = _shape1(s, pref_keyword="简日")
        hit = _row(s, aid, "ANi", 6, "pending", _d(40), title="[ANi] 某番 - 06 [简日][1080p]")
    plan = A.lock_fallback_plan()
    assert plan[aid]["eps"] == {6: [(hit, "ANi")]}, "不命中的 ANi 第 6 集不能混进来"
    assert plan[aid]["kw_missed"] == [7, 8, 9]


def test_zero_days_turns_it_off_and_lag_reminder_off_does_not(clean_tables, env):
    """⑧ N=0 → {}；把提醒关掉（ANIME_LOCK_LAG_DAYS=0）兜底照常工作。"""
    with clean_tables.get_session() as s:
        aid, _ = _shape1(s)
    env(ANIME_LOCK_FALLBACK_DAYS=0)
    assert A.lock_fallback_plan() == {}
    env(ANIME_LOCK_FALLBACK_DAYS=14, ANIME_LOCK_LAG_DAYS=0)
    assert _plan_eps(A.lock_fallback_plan(), aid) == [6, 7, 8, 9]


def test_cross_season_rerelease_blocked_by_same_schedule_gate(clean_tables, env):
    """串季重发：锁定组发本季 1~5 后停更；绿茶在本季播出期间一口气重发了【前一季】1~12（发布时间全一样）。
    组里有 lock_max、组内发布时间也单调（第 6 集与第 5 集同一刻），只有『发布时间一致』挡得住：共有的 1~5 集差了一个多月。"""
    batch = _d(60)
    with clean_tables.get_session() as s:
        a = _anime(s)
        for e in range(1, 6):
            _row(s, a.id, LOCK, e, "sent", _d(100 - 7 * (e - 1)))
        for e in range(1, 13):
            _row(s, a.id, "绿茶字幕组", e, "skipped" if e <= 5 else "pending", batch, rel=batch)
        aid = a.id
        a = s.get(Anime, aid)
    assert aid in A.locked_out_episodes([a], lag_days=14), "前提：提醒是会报的"
    assert _plan_eps(A.lock_fallback_plan(), aid) == []


def test_group_following_this_season_but_also_rereleasing_old_one_is_rejected(clean_tables, env):
    """同组既跟着锁定组发本季 1~5（只晚一天），又重发了前一季 1~12：两边最早那份对得上，但重发那几行对不上 →
    这个组整个不要（按『每一行』比，不只比最早一份）。否则前一季第 9~12 集会被当成本季后续集兜底下了。"""
    batch = _d(60)
    with clean_tables.get_session() as s:
        a = _anime(s)
        for e in range(1, 6):
            _row(s, a.id, LOCK, e, "sent", _d(100 - 7 * (e - 1)))
            _row(s, a.id, "绿茶字幕组", e, "skipped", _d(101 - 7 * (e - 1)))
        for e in range(1, 13):
            _row(s, a.id, "绿茶字幕组", e, "skipped" if e <= 5 else "pending", batch, rel=batch)
        aid = a.id
    assert _plan_eps(A.lock_fallback_plan(), aid) == []


def test_missing_release_time_is_rejected(clean_tables, env):
    """发布时间一致闸：缺发布时间就判不了，不要。"""
    with clean_tables.get_session() as s:
        aid, _ = _shape1(s, ani_rel={3: None})
    assert _plan_eps(A.lock_fallback_plan(), aid) == []


def test_unacknowledged_feed_gap_blocks(clean_tables, env):
    """有没点『知道了』的断档落在［锁定组最新入库, 现在］里 → 可能是漏收，返回 {}。
    本轮刚发现、还没落库的断档（extra_gaps）同样算；断档在锁定组最新入库之前的不挡；记坏了的当相交。"""
    def gap(frm, to):
        return {"src": "nyaa", "from": frm.isoformat(timespec="minutes"), "to": to.isoformat(timespec="minutes"),
                "at": NOW.isoformat(timespec="minutes")}
    with clean_tables.get_session() as s:
        aid, _ = _shape1(s)
    inside = gap(_d(30), _d(20))
    env(FEED_GAPS=json.dumps([inside]))
    assert A.lock_fallback_plan() == {}
    env(FEED_GAPS="")
    assert A.lock_fallback_plan(extra_gaps=[inside]) == {}
    env(FEED_GAPS=json.dumps([gap(_d(70), _d(60))]))
    assert _plan_eps(A.lock_fallback_plan(), aid) == [6, 7, 8, 9], "锁定组 46 天前还在进，60 天前的断档无关"
    env(FEED_GAPS=json.dumps([{"src": "nyaa", "from": "坏了"}]))
    assert A.lock_fallback_plan() == {}


# ── B 步：交付 ─────────────────────────────────────────────────

async def test_deliver_tries_next_on_false_and_marks_only_successes(clean_tables, env, monkeypatch):
    """⑨ 返回 False 换同集下一个候选；成功的才写 lock_fallback；推送里有集号和组名。"""
    with clean_tables.get_session() as s:
        aid, ids = _shape1(s)
        for e in range(1, 7):        # 绿茶：优先级低于 ANi，第 6 集当备选
            ids[("绿茶", e)] = _row(s, aid, "绿茶字幕组", e, "skipped" if e <= 5 else "pending",
                                    _d(75 - 7 * (e - 1), hours=-2), priority=5)
    bad = ids[("ANi", 6)]
    calls, msgs = _fake_delivery(monkeypatch, clean_tables, result=lambda tid, force: tid != bad)
    assert await A.deliver_lock_fallback(qb_alive=True) == 4
    assert [c[0] for c in calls][:2] == [bad, ids[("绿茶", 6)]], "ANi 第 6 集失败后该换绿茶"
    assert all(force is False for _, force in calls), "兜底不用 force：同集闸照旧"
    assert not _get(clean_tables, bad).lock_fallback, "失败的那条不算兜底"
    assert _get(clean_tables, ids[("绿茶", 6)]).lock_fallback
    assert all(_get(clean_tables, ids[("ANi", e)]).lock_fallback for e in (7, 8, 9))
    assert not _get(clean_tables, ids[("ANi", 10)]).lock_fallback
    (kind, msg), = msgs
    assert kind == "lock_fallback" and "第 6～9 集" in msg and "ANi" in msg and "绿茶字幕组" in msg and "46 天" in msg


def _real_chain(monkeypatch, env):
    """真交付链：只把取种和加进 qB 两步换成记账（同 test_r39_download 的 real_chain）。返回取过种的 URL 列表。"""
    from core import engine as ce
    env(QB_SYNC_STATUS=True, DOWN_PATH="/tmp/dl", ANIME_DOWN_PATH="")
    fetched = []

    async def fetch(url):
        fetched.append(url)
        return b"d1:xe"

    async def add(*a, **k):
        return True

    async def notify(kind, msg, **kw):
        return True
    monkeypatch.setattr(ce, "fetch_torrent_bytes", fetch)
    monkeypatch.setattr(ce, "add_to_qb", add)
    monkeypatch.setattr(A, "notify_event", notify)
    return fetched


async def test_b_step_delivers_through_the_real_chain(clean_tables, env, monkeypatch):
    """(r39 F20/F28) B 步走真实 download_anime_torrent：别的组的行越过锁定源照样交付、写上 lock_fallback。
    以后谁删掉或写错 prefs_gate=False，放行前复核会因"锁定源不等"把它们全挡掉 —— 打桩的用例测不出，这条会红。"""
    with clean_tables.get_session() as s:
        aid, ids = _shape1(s)
    fetched = _real_chain(monkeypatch, env)
    assert await A.deliver_lock_fallback(qb_alive=True) == 4
    assert len(fetched) == 4
    for e in (6, 7, 8, 9):
        t = _get(clean_tables, ids[("ANi", e)])
        assert t.status == "sent" and t.lock_fallback, (e, t.status, t.lock_fallback)
    assert _get(clean_tables, ids[("ANi", 10)]).status == "pending"


def test_failing_sources_turn_the_plan_off(clean_tables, env):
    """(r39 F29) 有源在失败：那段时间既没有断档记录也没有锁定组新行，分不清停更还是没抓到 → B 步计划为空。"""
    with clean_tables.get_session() as s:
        aid, _ = _shape1(s)
    assert A.lock_fallback_plan(sources_failing=True) == {}
    assert _plan_eps(A.lock_fallback_plan(), aid) == [6, 7, 8, 9]


async def test_failing_sources_skip_b_step_but_a_step_still_runs(clean_tables, env, monkeypatch):
    """(r39 F29) 有源在失败时只跳过 B 步：已经兜底过、锁定组又补发了的集，A 步照常换回锁定组的版本。"""
    with clean_tables.get_session() as s:
        b_aid, b_ids = _shape1(s)
        a_aid, a_ids = _caught_up_shape(s, title="另一番", display_name="另一番", bangumi_id=2)
    calls, _ = _fake_delivery(monkeypatch, clean_tables)
    assert await A.deliver_lock_fallback(qb_alive=True, sources_failing=True) == 1
    assert calls == [(a_ids[(LOCK, 6)], True)]
    assert all(_get(clean_tables, b_ids[("ANi", e)]).status == "pending" for e in (6, 7, 8, 9))


@pytest.mark.parametrize("bad", [False, True])
async def test_poll_once_passes_the_failing_source_signal(clean_tables, monkeypatch, cfg, bad):
    """(r39 F29) worker.poll_once：本轮有源没抓到 → deliver_lock_fallback(sources_failing=True)；全抓到 → False。"""
    import config as _c
    from core import health
    from core import worker as W
    cfg(QB_ENABLED=False, ANIME_POLL_ENABLED=True, NOTIFY_URL="", HEARTBEAT_URL="", SOURCE_FEED_SEEN="", FEED_GAPS="")
    monkeypatch.setattr(health, "_fail", {})
    monkeypatch.setattr(health, "_seen", {})
    monkeypatch.setattr(_c, "set_many", lambda u: {})
    # build_sources 被换成了假的，它本该在每轮开头清掉的 _build_skipped 就没人清了 —— 别的用例跑真
    # build_sources 时留下的"配置坏了的组"会被并进 failed_sources，让"全抓到"那一档也报成有源在失败
    monkeypatch.setattr(W, "_build_skipped", [])

    class Good:
        name = "好源"

        async def fetch(self, content_cache=None):
            return []

    class Bad:
        name = "坏源"

        async def fetch(self, content_cache=None):
            raise RuntimeError("403 Forbidden")

    async def fake_precheck():
        return False
    got = []

    async def spy(qb_alive=None, new_gaps=None, sources_failing=False):
        got.append(sources_failing)
        return 0
    monkeypatch.setattr(W, "build_sources", lambda: [Good(), Bad()] if bad else [Good()])
    monkeypatch.setattr(W.anime, "qb_precheck", fake_precheck)
    monkeypatch.setattr(W.anime, "deliver_lock_fallback", spy)
    await W.poll_once()
    assert got == [bad]


async def test_deliver_stops_whole_round_on_none(clean_tables, env, monkeypatch):
    """⑨ 返回 None（qB 掉线 / 目录配错）整轮停手：不试同集别的候选、不碰后面的集、不写标记。"""
    with clean_tables.get_session() as s:
        aid, ids = _shape1(s)
    calls, msgs = _fake_delivery(monkeypatch, clean_tables, result=lambda tid, force: None)
    assert await A.deliver_lock_fallback(qb_alive=True) == 0
    assert calls == [(ids[("ANi", 6)], False)]
    assert msgs == []


async def test_failed_delivery_writes_no_mark(clean_tables, env, monkeypatch):
    """下载失败时不写标记。"""
    with clean_tables.get_session() as s:
        aid, ids = _shape1(s)
    calls, msgs = _fake_delivery(monkeypatch, clean_tables, result=lambda tid, force: False)
    assert await A.deliver_lock_fallback(qb_alive=True) == 0
    assert len(calls) == 4 and msgs == []
    with clean_tables.get_session() as s:
        assert not list(s.exec(select(AnimeTorrent).where(AnimeTorrent.lock_fallback == True)))   # noqa: E712


async def test_deliver_respects_qb_switches(clean_tables, env, monkeypatch):
    with clean_tables.get_session() as s:
        _shape1(s)
    calls, _ = _fake_delivery(monkeypatch, clean_tables)
    assert await A.deliver_lock_fallback(qb_alive=False) == 0
    env(QB_ENABLED=False)
    assert await A.deliver_lock_fallback(qb_alive=True) == 0
    assert calls == []


async def test_lock_changed_mid_round_stops_that_anime(clean_tables, env, monkeypatch):
    """计划在 await 之前一次定好；交付期间用户把锁解了 → 后面的集不再按旧计划下（r38 D4 的同款口子）。"""
    with clean_tables.get_session() as s:
        aid, ids = _shape1(s)

    def unlock(tid, force):
        with clean_tables.get_session() as s:
            a = s.get(Anime, aid)
            a.pref_source = None
            s.add(a)
            s.commit()
        return True
    calls, _ = _fake_delivery(monkeypatch, clean_tables, result=unlock)
    assert await A.deliver_lock_fallback(qb_alive=True) == 1
    assert calls == [(ids[("ANi", 6)], False)]


async def test_after_fallback_the_reminders_go_quiet(clean_tables, env, monkeypatch):
    """⑩ 兜底下完：提醒、详情页标注、仪表盘『锁源缺集』都不再含这几集（判据都按 have，不用改）。"""
    with clean_tables.get_session() as s:
        aid, ids = _shape1(s)
        a = s.get(Anime, aid)
        s.expunge(a)
    assert A.pending_breakdown()["locked"] == 6
    _fake_delivery(monkeypatch, clean_tables)
    assert await A.deliver_lock_fallback(qb_alive=True) == 4
    assert A.locked_out_episodes([a])[aid]["eps"] == [10]
    assert A.pending_breakdown()["locked"] == 2
    with clean_tables.get_session() as s:
        ts = list(s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid)))
    assert {t.episode for t in ts if t.id in A.locked_out_torrent_ids(ts)} == {10.0, 11.0}


# ── A 步：锁定组补发后补下 ─────────────────────────────────────────

def _caught_up_shape(s, lock6_status="pending", **akw):
    """兜底过第 6 集（ANi，lock_fallback）；锁定组 3 小时前补发了第 6 集。"""
    a = _anime(s, **akw)
    ids = {}
    for e in range(1, 7):
        ids[("ANi", e)] = _row(s, a.id, "ANi", e, "sent" if e == 6 else "skipped", _d(75 - 7 * (e - 1)),
                               lock_fallback=(e == 6))
    for e in range(1, 6):
        ids[(LOCK, e)] = _row(s, a.id, LOCK, e, "sent", _d(75 - 7 * (e - 1) - 1))
    ids[(LOCK, 6)] = _row(s, a.id, LOCK, 6, lock6_status, _d(0, hours=3))
    return a.id, ids


async def test_lock_group_late_episode_is_fetched_with_force(clean_tables, env, monkeypatch):
    """⑪ + A 步：锁定组补发的第 6 集，即时下载被兜底那份的同集闸落成 skipped（接受去重）；
    A 步随后用 force 下锁定组的版本，兜底那份还在；再跑一遍不再动（幂等）。"""
    with clean_tables.get_session() as s:
        aid, ids = _caught_up_shape(s)
    lock6, fb6 = ids[(LOCK, 6)], ids[("ANi", 6)]
    assert await A.download_anime_torrent(lock6) is False
    assert _get(clean_tables, lock6).status == "skipped"
    calls, msgs = _fake_delivery(monkeypatch, clean_tables)
    assert await A.deliver_lock_fallback(qb_alive=True) == 1
    assert calls == [(lock6, True)]
    fb = _get(clean_tables, fb6)
    assert fb.status == "sent" and fb.lock_fallback, "兜底那份永不自动删"
    (kind, msg), = msgs
    assert kind == "lock_fallback" and "补发了第 6 集" in msg and "ANi" in msg
    assert await A.deliver_lock_fallback(qb_alive=True) == 0 and len(calls) == 1, "A 步重复跑必须幂等"


async def test_catchup_skips_an_episode_whose_fallback_copy_was_deleted_mid_round(clean_tables, env, monkeypatch):
    """(r39 F27) A 步计划在 await 之前定好：交付第 6 集期间用户删了第 7 集的兜底那份（同集最后一份 → 该集已删）。
    第 7 集不能再 force 下锁定组的版本 —— force 成功会经 clear_ep_dropped 把"这一集不要了"整个清掉。"""
    with clean_tables.get_session() as s:
        a = _anime(s)
        ids = {}
        for e in range(1, 8):
            ids[("ANi", e)] = _row(s, a.id, "ANi", e, "sent" if e >= 6 else "skipped", _d(75 - 7 * (e - 1)),
                                   lock_fallback=(e >= 6))
        for e in range(1, 6):
            ids[(LOCK, e)] = _row(s, a.id, LOCK, e, "sent", _d(75 - 7 * (e - 1) - 1))
        for e in (6, 7):
            ids[(LOCK, e)] = _row(s, a.id, LOCK, e, "pending", _d(0, hours=3))
        aid = a.id
    assert sorted(A._lock_catchup_plan()[aid]["eps"]) == [6, 7]
    fb7 = ids[("ANi", 7)]

    def user_deletes_fb7(tid, force):
        with clean_tables.get_session() as s:
            t = s.get(AnimeTorrent, fb7)
            t.status, t.ep_dropped = "deleted", True
            s.add(t)
            s.commit()
        return True
    calls, _ = _fake_delivery(monkeypatch, clean_tables, result=user_deletes_fb7)
    assert await A.deliver_lock_fallback(qb_alive=True) == 1
    assert calls == [(ids[(LOCK, 6)], True)]
    assert _get(clean_tables, ids[(LOCK, 7)]).status == "pending"
    assert _get(clean_tables, fb7).ep_dropped is True


async def test_catchup_ignores_fallback_days_setting(clean_tables, env, monkeypatch):
    """关掉兜底（N=0）之后，已经兜底过的集照样换回锁定组的版本 —— 那本来就是锁的意思。"""
    with clean_tables.get_session() as s:
        aid, ids = _caught_up_shape(s, lock6_status="skipped")
    env(ANIME_LOCK_FALLBACK_DAYS=0)
    calls, _ = _fake_delivery(monkeypatch, clean_tables)
    assert await A.deliver_lock_fallback(qb_alive=True) == 1
    assert calls == [(ids[(LOCK, 6)], True)]


async def test_catchup_leaves_user_deleted_lock_episode_alone(clean_tables, env, monkeypatch):
    """锁定组那一集被用户删过（哪怕它还有别的版本在待下）→ A 步不动。"""
    with clean_tables.get_session() as s:
        aid, ids = _caught_up_shape(s, lock6_status="deleted")
        _row(s, aid, LOCK, 6, "pending", _d(0, hours=3), title=f"[{LOCK}] 某番 - 06 [繁日]")
    calls, _ = _fake_delivery(monkeypatch, clean_tables)
    assert A._lock_catchup_plan() == {}
    assert await A.deliver_lock_fallback(qb_alive=True) == 0 and calls == []


async def test_catchup_waits_for_the_grace_window_and_keyword(clean_tables, env, monkeypatch):
    """A 步也等缓冲窗口（简/繁几个版本到齐再挑），设了版本关键词的必须命中。"""
    with clean_tables.get_session() as s:
        aid, ids = _caught_up_shape(s, pref_keyword="简日")
    assert A._lock_catchup_plan() == {}, "锁定组那条不含『简日』"
    with clean_tables.get_session() as s:
        hit = _row(s, aid, LOCK, 6, "pending", _d(0, hours=3), title=f"[{LOCK}] 某番 - 06 [简日]")
    assert A._lock_catchup_plan()[aid]["eps"] == {6.0: [hit]}
    env(ANIME_DOWNLOAD_GRACE_MIN=600)
    assert A._lock_catchup_plan() == {}


# ── 口径 / 挂载 ─────────────────────────────────────────────────

async def test_lock_lag_message_tells_about_fallback(clean_tables, env, monkeypatch):
    """提醒消息：N>0 时说清会自动补、漏收先点『自动补齐』，关键词不命中的集写明『没有某某版本』；N=0 时照旧。"""
    with clean_tables.get_session() as s:
        _shape1(s, pref_keyword="简日")
    msgs = []

    async def fake_notify(kind, msg, **kw):
        msgs.append(msg)
        return True
    monkeypatch.setattr(A, "notify_enabled", lambda kind: True)
    monkeypatch.setattr(A, "notify_event", fake_notify)
    await A.sweep_lock_lag()
    assert "满 14 天且核对无误会自动用别的组补" in msgs[0] and "自动补齐" in msgs[0]
    assert "第 6～9 集别的组没有『简日』版本" in msgs[0]
    env(ANIME_LOCK_FALLBACK_DAYS=0)
    await A.sweep_lock_lag()
    assert "一集都不会下" in msgs[1]


@pytest.mark.nicegui_main_file("tests/render_detail_main.py")
async def test_detail_page_shows_fallback_badge_and_new_banner(user, clean_tables, env):
    """详情页：兜底那一行挂『兜底』徽标（方便事后找到、删掉）；锁源落后横幅不再说"一集都不会下"。"""
    from nicegui import ui
    with clean_tables.get_session() as s:
        aid, _ = _shape1(s)
        _row(s, aid, "绿茶字幕组", 4, "sent", _d(50), lock_fallback=True)
    await user.open(f"/detail/{aid}")
    assert [b for b in user.find(ui.badge).elements if getattr(b, "text", "") == "兜底"], "兜底那一行没有徽标"
    await user.should_see("核对无误的会自动用别的组补一份")
    await user.should_not_see("因为锁了源一集都不会下")


def test_worker_hooks_it_after_single_works_with_this_rounds_gaps():
    """挂在 deliver_single_works 之后、单独包 try；本轮新发现的断档要一并传进去（FEED_GAPS 在轮末才落库）。"""
    src = (Path(__file__).resolve().parent.parent / "core" / "worker.py").read_text(encoding="utf-8")
    i, j = src.index("anime.deliver_single_works("), src.index("anime.deliver_lock_fallback(")
    assert i < j < src.index('_upd["FEED_GAPS"]')
    assert "deliver_lock_fallback(qb_alive=qb_alive, new_gaps=new_gaps," in src
    assert 'loop_error("锁定源兜底", e)' in src
