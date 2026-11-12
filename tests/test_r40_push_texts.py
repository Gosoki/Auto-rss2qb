"""三条推送 / 横幅文案要说清"发生了什么、要你点哪里"。

  · 8a：RSS 断档的推送（worker.poll_once 末尾）与全站横幅（layout._health_notices）指向『自动补齐』——
    『补齐该源』是会把番打回待确认的宽松路径，缺集提示与详情页说的都是『自动补齐』；并说清缺集提示要等余量过了才出现。
  · 8b：sweep_alerts 的失败 / 停滞推送点名最新的 3 条"番名 第N集"（同名去重），多的写"等 N 条"；停滞按 fail_reason
    前缀（engine.QB_ERROR_REASON）分"qB 报错（磁盘满 / 目录不可写？）"与"长期无进度"两句。去重键仍按 id 集合：
    同一批第二轮、哪怕正文变了，照旧被冷却压住；冷却中 / 没订阅时不去读番名。
  · 8c：lock_fallback_plan(blocked=…) 说出兜底被哪道番级闸挡住；放行结果与不传 blocked 时逐项相同。
    "有源连续失败"那道整轮闸由 lock_fallback_preview 叠加（kw_missed 不丢、只记本来会补集的番）。
    sweep_lock_lag 的推送与详情页锁源落后横幅把"核对无误会自动补"换成具体原因（断档那道还要说去点『知道了』）。
"""
import hashlib
import inspect
import json
from datetime import datetime, timedelta

import pytest

import services.notify as N
from core import anime as A
from core import engine, health
from db.models import Anime, AnimeTorrent, Movie, MovieTorrent

from tests.test_r39_lock_fallback import LOCK, _anime, _d, _plan_eps, _row, _shape1

NOW = datetime.now()


@pytest.fixture
def env(cfg, monkeypatch):
    cfg(ANIME_LOCK_FALLBACK_DAYS=14, ANIME_LOCK_LAG_DAYS=7, ANIME_LOCK_SYNC_DAYS=2, ANIME_FINISH_UNSUB=False,
        FEED_GAPS="", ANIME_DOWNLOAD_GRACE_MIN=60, QB_ENABLED=True, ALERT_SOURCE_FAIL_ROUNDS=3)
    monkeypatch.setattr(health, "_fail", {})
    return cfg


def _gap(frm, to):
    return {"src": "nyaa", "from": frm.isoformat(timespec="minutes"), "to": to.isoformat(timespec="minutes"),
            "at": NOW.isoformat(timespec="minutes")}


# ── 8a ─────────────────────────────────────────────────────────

def test_feed_gap_push_points_to_auto_backfill():
    """断档推送：不再叫人点『补齐该源』，说『自动补齐』并说明缺集提示什么时候才出现。"""
    from core import worker as W
    src = inspect.getsource(W.poll_once)
    seg = src[src.index('notify_event("feed_gap"'):]
    seg = seg[:seg.index("bypass_rate=True")]
    assert "补齐该源" not in seg
    assert "自动补齐" in seg and "缺集提示要等余量过了或下一集入库才出现" in seg
    assert "可以先到这段时间有更新的番的详情页点『自动补齐』" in seg


def test_feed_gap_banner_points_to_auto_backfill():
    """全站横幅的断档那条与推送同一口径。"""
    from pages import layout as L
    src = inspect.getsource(L._health_notices)
    seg = src[src.index("RSS 断档："):src.index('action=("知道了"')]
    assert "补齐该源" not in seg
    assert "自动补齐" in seg and "缺集提示要等余量过了或下一集入库才出现" in seg
    assert "可以先到这段时间有更新的番的详情页点『自动补齐』" in seg


# ── 8b ─────────────────────────────────────────────────────────

@pytest.fixture
def alerts(clean_tables, monkeypatch, cfg):
    """同 test_sweep_alerts：收走推送内容，订阅 / 冷却 / 限流走真实事件层。"""
    box = []

    async def fake(msg):
        box.append(msg)
        return True
    monkeypatch.setattr(N, "notify", fake)
    for d in (N._last_sent, N._state_now, N._sent_times, N._state_times, N._state_suppressed):
        d.clear()
    N._fail_streak, N._muted_until = 0, 0.0
    cfg(NOTIFY_URL="http://push.example/key", NOTIFY_MAX_PER_HOUR=0,
        NOTIFY_EVENTS=list(N.EVENTS), NOTIFY_BACKLOG_MIN=99)
    yield box
    N._last_sent.clear()
    N._state_now.clear()


def _bad(s, name, ep, status, why="", aid=None, tag=""):
    """建一条失败 / 停滞行；不给 aid 就新建一部同名番。返回 (种子 id, 番 id)。"""
    if aid is None:
        a = Anime(title=name, display_name=name, quarter="26C", confirmed=True, bangumi_id=1)
        s.add(a)
        s.commit()
        s.refresh(a)
        aid = a.id
    t = AnimeTorrent(anime_id=aid, raw_title="t", info_hash=hashlib.sha1(f"{name}-{ep}-{tag}".encode()).hexdigest(),
                     episode=float(ep), status=status, fail_reason=why)
    s.add(t)
    s.commit()
    return t.id, aid


def _qb_err(pct=40):
    return f"{engine.QB_ERROR_REASON}（已下 {pct}%）"


async def test_failed_push_names_newest_three(alerts, clean_tables):
    """失败推送带"番名 第N集"；多于 3 条只点最新的 3 条、写"等 N 条"。"""
    with clean_tables.get_session() as s:
        for i, name in enumerate(["甲番", "乙番", "丙番", "丁番"], start=1):
            _bad(s, name, i, "error")
    await A.sweep_alerts()
    msg, = [m for m in alerts if "交付失败" in m]
    assert "丁番 第4集、丙番 第3集、乙番 第2集 等 4 条" in msg, msg
    assert "甲番" not in msg


async def test_new_failure_is_named_while_old_rows_linger(alerts, clean_tables):
    """失败行留给人工、不会自己消失；库里压着 3 条旧的时，让 id 集合换掉（于是才推）的那条新失败要点到名。"""
    with clean_tables.get_session() as s:
        for i, name in enumerate(["旧番甲", "旧番乙", "旧番丙"], start=1):
            _bad(s, name, i, "error")
    await A.sweep_alerts()
    with clean_tables.get_session() as s:
        _bad(s, "今天刚失败的番", 7, "error")
    await A.sweep_alerts()
    msgs = [m for m in alerts if "交付失败" in m]
    assert len(msgs) == 2 and "今天刚失败的番 第7集" in msgs[1], msgs


async def test_exactly_three_has_no_etc(alerts, clean_tables):
    with clean_tables.get_session() as s:
        for i, name in enumerate(["甲番", "乙番", "丙番"], start=1):
            _bad(s, name, i, "error")
    await A.sweep_alerts()
    msg, = [m for m in alerts if "交付失败" in m]
    assert "丙番 第3集、乙番 第2集、甲番 第1集。" in msg and "等" not in msg, msg


async def test_same_episode_copies_do_not_crowd_out_other_anime(alerts, clean_tables):
    """同一集 3 个源都失败（换源逐个试、逐个落 error）+ 另一部番 1 条：同名只点一次，另一部番不被挤掉。"""
    with clean_tables.get_session() as s:
        _bad(s, "乙番", 2, "error")
        _, aid = _bad(s, "甲番", 5, "error", tag="a")
        _bad(s, "甲番", 5, "error", aid=aid, tag="b")
        _bad(s, "甲番", 5, "error", aid=aid, tag="c")
    await A.sweep_alerts()
    msg, = [m for m in alerts if "交付失败" in m]
    assert "4 条种子交付失败：甲番 第5集、乙番 第2集。" in msg, msg


async def test_failed_push_three_or_fewer_has_no_etc(alerts, clean_tables):
    with clean_tables.get_session() as s:
        _bad(s, "甲番", 5, "error")
        m = Movie(title="某剧场版", display_name="某剧场版", quarter="26C", bangumi_id=2, mikan_id="mk-r40")
        s.add(m)
        s.commit()
        s.refresh(m)
        s.add(MovieTorrent(movie_id=m.id, raw_title="t", info_hash="f" * 40, status="error"))
        s.commit()
    await A.sweep_alerts()
    msg, = [m for m in alerts if "交付失败" in m]
    assert "甲番 第5集、某剧场版" in msg and "等" not in msg, msg


async def test_movie_side_also_names_newest_first(alerts, clean_tables):
    """剧场版那一侧同样按 id 倒排：后失败的片先点到。"""
    with clean_tables.get_session() as s:
        for i, name in enumerate(["旧片", "新片"], start=1):
            m = Movie(title=name, display_name=name, quarter="26C", bangumi_id=10 + i, mikan_id=f"mk-r40-{i}")
            s.add(m)
            s.commit()
            s.refresh(m)
            s.add(MovieTorrent(movie_id=m.id, raw_title="t", info_hash=f"{i:040x}", status="error"))
            s.commit()
    await A.sweep_alerts()
    msg, = [m for m in alerts if "交付失败" in m]
    assert "：新片、旧片。" in msg, msg


async def test_stalled_push_splits_qb_error_from_no_progress(alerts, clean_tables):
    """停滞按 fail_reason 前缀分两句：qB 报错 → 磁盘满 / 目录不可写？；其余 → 长期无进度。各自点名。"""
    with clean_tables.get_session() as s:
        _bad(s, "满盘番", 2, "stalled", _qb_err())
        _bad(s, "没源番", 3, "stalled", "1440 分钟一点进度都没有")
    await A.sweep_alerts()
    msg, = [m for m in alerts if "停滞" in m]
    assert "qB 报错（磁盘满 / 目录不可写？）：满盘番 第2集" in msg, msg
    assert "长期无进度（qB 里可能没源了）：没源番 第3集" in msg, msg


async def test_stalled_qb_error_only_does_not_say_no_progress(alerts, clean_tables):
    with clean_tables.get_session() as s:
        for i, name in enumerate(["一番", "二番", "三番", "四番", "五番"], start=1):
            _bad(s, name, i, "stalled", _qb_err(10))
    await A.sweep_alerts()
    msg, = [m for m in alerts if "停滞" in m]
    assert "长期无进度" not in msg and "没源" not in msg, msg
    assert "qB 报错（磁盘满 / 目录不可写？）：五番 第5集、四番 第4集、三番 第3集 等 5 条" in msg, msg


async def test_stalled_from_engine_qb_error_is_classified_as_qb_error(alerts, clean_tables, monkeypatch, cfg):
    """端到端：engine.sync_qb_status 真写出来的 fail_reason 被认成"qB 报错"那一类（两个模块靠同一个前缀约定）。"""
    cfg(QB_ENABLED=True, QB_SYNC_STATUS=True, QB_SYNC_INTERVAL=30)
    with clean_tables.get_session() as s:
        a = Anime(title="盘满番", display_name="盘满番", quarter="26C", confirmed=True, bangumi_id=1)
        s.add(a)
        s.commit()
        s.refresh(a)
        now = datetime.now()
        s.add(AnimeTorrent(anime_id=a.id, info_hash="0" * 40, raw_title="t", episode=3, status="sent",
                           qb_progress=0.4, qb_state="downloading", created_at=now,
                           qb_synced_at=now - timedelta(minutes=1)))
        s.commit()

    async def fake_info(hashes):
        return {h: {"progress": 0.4, "state": "error", "dlspeed": 0, "size": 1} for h in hashes}
    monkeypatch.setattr(engine.qb, "torrents_info", fake_info)
    await engine.sync_qb_status(AnimeTorrent)
    await A.sweep_alerts()
    msg, = [m for m in alerts if "停滞" in m]
    assert "qB 报错（磁盘满 / 目录不可写？）：盘满番 第3集" in msg and "长期无进度" not in msg, msg


async def test_same_batch_is_still_held_by_cooldown(alerts, clean_tables):
    """文案里多了番名，去重键没变：同一批第二轮仍被冷却压住；换了一条照常再说。"""
    with clean_tables.get_session() as s:
        _bad(s, "甲番", 1, "error")
        _bad(s, "乙番", 2, "stalled", _qb_err(1))
    await A.sweep_alerts()
    await A.sweep_alerts()
    assert len(alerts) == 2, alerts
    with clean_tables.get_session() as s:
        _bad(s, "丙番", 3, "stalled")
    await A.sweep_alerts()
    assert len(alerts) == 3 and "丙番 第3集" in alerts[-1], alerts


async def test_same_ids_body_changed_is_still_held(alerts, clean_tables):
    """同一批 id、正文变了（停滞原因换了类、番改了名）：仍被冷却压住 —— 正文不能掺进去重键。"""
    with clean_tables.get_session() as s:
        tid, _ = _bad(s, "甲番", 1, "stalled", "1440 分钟进度无推进（40%）")
        _bad(s, "乙番", 2, "error")
    await A.sweep_alerts()
    assert len(alerts) == 2, alerts
    with clean_tables.get_session() as s:
        t = s.get(AnimeTorrent, tid)
        t.fail_reason = _qb_err()
        s.add(t)
        for a in s.exec(A.select(Anime)).all():
            a.display_name = a.display_name + "改"
            s.add(a)
        s.commit()
    await A.sweep_alerts()
    assert len(alerts) == 2, alerts


async def test_names_are_read_only_when_the_push_will_go_out(alerts, clean_tables, monkeypatch, cfg):
    """冷却中 / 没订阅时不读番名（每轮巡检把全部失败行和番读一遍是白读）；要发时照常点名。"""
    calls = []
    real = A._alert_names

    def spy(status):
        calls.append(status)
        return real(status)
    monkeypatch.setattr(A, "_alert_names", spy)
    with clean_tables.get_session() as s:
        _bad(s, "甲番", 1, "error")
        _bad(s, "乙番", 2, "stalled", _qb_err())
    await A.sweep_alerts()
    assert sorted(calls) == ["error", "stalled"] and len(alerts) == 2, (calls, alerts)
    calls.clear()
    await A.sweep_alerts()                       # 同一批：两条都在冷却里
    assert calls == [] and len(alerts) == 2, calls
    cfg(NOTIFY_EVENTS=[k for k in N.EVENTS if k not in ("failed", "stalled")])
    with clean_tables.get_session() as s:
        _bad(s, "丙番", 3, "error")             # 换了一批，但没订阅
    await A.sweep_alerts()
    assert calls == [] and len(alerts) == 2, calls


# ── 8c：计划 ────────────────────────────────────────────────────

def _numbering_conflict(s):
    aid, _ = _shape1(s)
    for e in range(12, 26):
        _row(s, aid, "ANi", e, "pending", _d(60))
    return aid


def _no_lock_episode(s):
    a = _anime(s)
    for e in range(1, 6):
        _row(s, a.id, "ANi", e, "pending", _d(40 - e))
    return a.id


def _gap_shape(s):
    return _shape1(s)[0]


_GATES = [
    ("total_unknown", lambda s: _shape1(s, total_episodes=None)[0], "bgm 总集数未知"),
    ("ambiguous", lambda s: _shape1(s, ep_offset=2)[0], "有集号歧义段"),
    ("numbering", _numbering_conflict, "集号超出 bgm 记载"),
    ("no_lock_ep", _no_lock_episode, "锁定组还没发过正片，没有集号可对照"),
    ("binding", lambda s: _shape1(s, air_date="2030-01-06")[0], "绑定可疑（有集号不可能属于所绑的那一季）"),
    ("gap", _gap_shape, A.LOCK_FB_GAP_WHY),
]


@pytest.mark.parametrize("name, build, why", _GATES, ids=[g[0] for g in _GATES])
def test_each_gate_names_its_reason(clean_tables, env, name, build, why):
    """每道番级闸各造一部番：blocked 里的原因对得上；放行结果与不传 blocked 时一致（都不挑这部番）。"""
    with clean_tables.get_session() as s:
        aid = build(s)
        ok_aid, _ = _shape1(s, title="另一部", display_name="另一部", bangumi_id=2)
    if name == "gap":
        env(FEED_GAPS=json.dumps([_gap(_d(30), _d(20))]))
    plain = A.lock_fallback_plan()
    blocked = {}
    assert A.lock_fallback_plan(blocked=blocked) == plain
    assert blocked == {aid: why} if name != "gap" else blocked == {aid: why, ok_aid: why}
    assert _plan_eps(plain, aid) == []
    if name != "gap":
        assert _plan_eps(plain, ok_aid) == [6, 7, 8, 9], "没被挡的照常放行"


def test_preview_names_failing_sources_only_for_anime_that_would_be_filled(clean_tables, env):
    """整轮闸（有源连续失败）由 lock_fallback_preview 叠加：worker 那条路照旧整轮不兜底；
    只给本来会补集的番记这条原因——被别的闸挡住的保留自己的原因，还没到时候的、只有 kw_missed 的都不记；
    kw_missed 照常带回（推送要说"没有某某版本，不会自动补"）。"""
    with clean_tables.get_session() as s:
        aid, _ = _shape1(s)
        bad = _shape1(s, title="绑错", display_name="绑错", bangumi_id=3, air_date="2030-01-06")[0]
        recent, _ = _shape1(s, title="最近还发", display_name="最近还发", bangumi_id=4)
        _row(s, recent, LOCK, -1, "pending", _d(3))
        kw_only, _ = _shape1(s, title="只缺版本", display_name="只缺版本", bangumi_id=5, pref_keyword="繁日")
    health._fail["ANi"] = (3, "HTTPStatusError: 403")
    assert A.lock_fallback_plan(sources_failing=True) == {}
    plan, blocked = A.lock_fallback_preview()
    assert plan == A.lock_fallback_plan()
    assert blocked == {aid: A.LOCK_FB_FAILING_WHY, bad: "绑定可疑（有集号不可能属于所绑的那一季）"}, blocked
    assert plan[kw_only]["kw_missed"] == [6, 7, 8, 9] and not plan[kw_only]["eps"]
    health._fail.clear()
    assert A.lock_fallback_preview()[1] == {bad: "绑定可疑（有集号不可能属于所绑的那一季）"}


def test_not_yet_due_but_gated_is_still_reported(clean_tables, env):
    """锁定组最近还在发（没到时候）但同时被番级闸挡住：照样记——到时候它也补不了，提醒得说清是哪道闸。"""
    with clean_tables.get_session() as s:
        aid, _ = _shape1(s)
        _row(s, aid, LOCK, -1, "pending", _d(3))
    env(FEED_GAPS=json.dumps([_gap(_d(2), _d(1))]))
    blocked = {}
    assert A.lock_fallback_plan(blocked=blocked) == {} == A.lock_fallback_plan()
    assert blocked == {aid: A.LOCK_FB_GAP_WHY}


def test_unblocked_and_not_yet_due_are_not_reported_as_blocked(clean_tables, env):
    """没被挡的不进 blocked；锁定组最近还在进（还没到时候）也不算"挡住"。"""
    with clean_tables.get_session() as s:
        aid, _ = _shape1(s)
        recent, _ = _shape1(s, title="最近还发", display_name="最近还发", bangumi_id=4)
        _row(s, recent, LOCK, -1, "pending", _d(3))
    blocked = {}
    plan = A.lock_fallback_plan(blocked=blocked)
    assert plan == A.lock_fallback_plan() and blocked == {}
    assert _plan_eps(plan, aid) == [6, 7, 8, 9] and _plan_eps(plan, recent) == []


# ── 8c：推送与详情页 ─────────────────────────────────────────────

async def _lag_msgs(monkeypatch):
    msgs = []

    async def fake_notify(kind, msg, **kw):
        msgs.append(msg)
        return True
    monkeypatch.setattr(A, "notify_enabled", lambda kind: True)
    monkeypatch.setattr(A, "notify_event", fake_notify)
    await A.sweep_lock_lag()
    return msgs


async def test_lock_lag_push_names_gap_gate_and_the_ack_button(clean_tables, env, monkeypatch):
    with clean_tables.get_session() as s:
        _shape1(s)
    env(FEED_GAPS=json.dumps([_gap(_d(30), _d(20))]))
    msg, = await _lag_msgs(monkeypatch)
    assert "自动兜底被挡住了：有未确认的 RSS 断档，到页面顶部的断档横幅点『知道了』之后才会兜底" in msg, msg
    assert "核对无误会自动用别的组补" not in msg


async def test_lock_lag_push_names_other_gates_without_ack_hint(clean_tables, env, monkeypatch):
    with clean_tables.get_session() as s:
        _shape1(s, air_date="2030-01-06")
    msg, = await _lag_msgs(monkeypatch)
    assert "自动兜底被挡住了：绑定可疑" in msg and "知道了" not in msg, msg
    assert "核对无误会自动用别的组补" not in msg


async def test_lock_lag_push_names_failing_sources(clean_tables, env, monkeypatch):
    """推送一侧也认"有源在失败"那道整轮闸（worker 那时整轮不兜底，这里不能还说会自动补）。"""
    with clean_tables.get_session() as s:
        _shape1(s)
    health._fail["ANi"] = (3, "HTTPStatusError: 403")
    msg, = await _lag_msgs(monkeypatch)
    assert "自动兜底被挡住了：有源在连续抓取失败" in msg, msg


async def test_lock_lag_push_keeps_kw_missed_when_sources_failing(clean_tables, env, monkeypatch):
    """有源在失败时"别的组没有某某版本，不会自动补"这条不会自己过去的原因照说，不被暂时原因顶掉。"""
    with clean_tables.get_session() as s:
        _shape1(s, pref_keyword="繁日")
    health._fail["ANi"] = (3, "HTTPStatusError: 403")
    msg, = await _lag_msgs(monkeypatch)
    assert "第 6～9 集别的组没有『繁日』版本，不会自动补" in msg, msg
    assert "被挡住" not in msg, msg


async def test_lock_lag_push_unblocked_keeps_old_wording(clean_tables, env, monkeypatch):
    with clean_tables.get_session() as s:
        _shape1(s)
    msg, = await _lag_msgs(monkeypatch)
    assert "满 14 天且核对无误会自动用别的组补" in msg and "被挡住" not in msg, msg


@pytest.mark.nicegui_main_file("tests/render_detail_main.py")
async def test_detail_page_names_the_gate(user, clean_tables, env):
    """详情页锁源落后横幅：被断档挡住时写具体原因和『知道了』；同页顶部的断档横幅指向『自动补齐』。"""
    with clean_tables.get_session() as s:
        aid, _ = _shape1(s)
    env(FEED_GAPS=json.dumps([_gap(_d(30), _d(20))]))
    await user.open(f"/detail/{aid}")
    await user.should_see("自动兜底被挡住了：有未确认的 RSS 断档，到页面顶部的断档横幅点『知道了』之后才会兜底")
    await user.should_not_see("核对无误的会自动用别的组补一份")
    await user.should_see("缺集提示要等余量过了或下一集入库才出现")


@pytest.mark.nicegui_main_file("tests/render_detail_main.py")
async def test_detail_page_names_binding_gate(user, clean_tables, env):
    with clean_tables.get_session() as s:
        aid, _ = _shape1(s, air_date="2030-01-06")
    await user.open(f"/detail/{aid}")
    await user.should_see("自动兜底被挡住了：绑定可疑")


@pytest.mark.nicegui_main_file("tests/render_detail_main.py")
async def test_detail_page_names_failing_sources(user, clean_tables, env):
    """详情页与推送同口径：有源在连续失败时 worker 整轮不兜底，横幅不能还说会自动补。"""
    with clean_tables.get_session() as s:
        aid, _ = _shape1(s)
    health._fail["ANi"] = (3, "HTTPStatusError: 403")
    await user.open(f"/detail/{aid}")
    await user.should_see("自动兜底被挡住了：有源在连续抓取失败")
    await user.should_not_see("核对无误的会自动用别的组补一份")
