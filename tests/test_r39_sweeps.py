"""第 39 轮：巡检口径（A6 占槽别季行 / A8 缺集尾巴按实际发布日重推 / A9 撤销完结时推断更时间戳）。

三条的共同点：都只让"判下齐 / 报缺集 / 报断更"变得更保守或更准，不改任何下载闸。
  · A6：第一季的旧正片占满本季 1..T（真库 anime#6），会把才播 8 集的番判成下齐、缺的 9..12 也不报。
    只剔 1..T 内"不可能属于本季"的行；超界行照旧计入，两套编号闸不能瞎。
  · A8：番中途休播、恢复周更后，按首播日推算会每周多报一条"缺下一集"。锚在 max(seen) 那一集的实际发布日上。
  · A9：已下齐的番被撤销完结（删了一集）后，回到订阅态照样不会有新种子 —— 不该紧接着报一条假断更。
    审查 F7：时间戳只推迟 IDLE_DAYS 天；有开播日的番改靠"播完且集齐"豁免（时间闸与 is_finished 同一个周更模型）。
  · F8：槽位被别季旧行占着的缺集，补齐也下不了（have_eps 仍认旧行），提示改成先处理绑定，不再引导去点补齐。
"""
import asyncio
from datetime import date, datetime, timedelta

import pytest
from sqlmodel import select

from core import anime as A
from db.models import Anime, AnimeTorrent


def _async(v=True):
    async def _c():
        return v
    return _c()


@pytest.fixture(autouse=True)
def _fresh_notify_state():
    """同 test_finish_idle：通知的进程内冷却是模块级的，番 id 在用例间会重复。"""
    from services import notify as _N
    _N._last_sent.clear()
    yield
    _N._last_sent.clear()


def _at(d: date, weeks: float) -> datetime:
    return datetime(d.year, d.month, d.day) + timedelta(weeks=weeks)


def _mk(db, *, total, air, rows, **kw):
    """建一部在追的番 + 种子。rows = [(集号, 状态, release_time 或 None)] 或带第 4 项 raw_title。"""
    with db.get_session() as s:
        a = Anime(title="百女友", display_name="百女友 第三季", season=3, quarter="26C", confirmed=True,
                  rejected=False, total_episodes=total, air_date=air.isoformat() if air else None,
                  bangumi_id=1, **kw)
        s.add(a)
        s.commit()
        s.refresh(a)
        for i, r in enumerate(rows):
            ep, st, rel = r[:3]
            raw = r[3] if len(r) > 3 else f"[ANi] 百女友 - {int(ep):02d}"
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{a.id * 1000 + i:040x}", source="ANi", site="nyaa",
                               anime_title="百女友", season=1, episode=float(ep), status=st,
                               qb_progress=1.0, release_time=rel, raw_title=raw, created_at=datetime.now()))
        s.commit()
        return a.id


def _anime(db, aid):
    with db.get_session() as s:
        a = s.get(Anime, aid)
        s.expunge(a)
        return a


# ==================== A6：占了 1..T 槽位的别季行不计 ====================

def _six_like(db, *, tail_status="skipped", release=True):
    """真库 anime#6 的形状：本季 12 集、首播 17 周前；第一季 1..12 早 150 周、已 sent；本季 1..8 sent，9..12 按 tail_status。"""
    d = date.today() - timedelta(weeks=17)
    rows = [(e, "sent", _at(d, -150 + e) if release else None) for e in range(1, 13)]   # 第一季（条件③）
    rows += [(e, "sent", _at(d, e - 1) if release else None) for e in range(1, 9)]       # 本季按周播
    if tail_status:
        rows += [(e, tail_status, _at(d, e - 1) if release else None) for e in range(9, 13)]
    return _mk(db, total=12, air=d, rows=rows)


async def test_previous_season_rows_do_not_fill_the_slots(clean_tables, cfg, monkeypatch):
    """第一季的 1..12 占满槽位：coverage 只剩本季的 1..8，sweep_finished 不打下齐，seen/have 不计第一季的行。"""
    cfg(ANIME_FINISH_ENABLED=True)
    monkeypatch.setattr(A, "notify_event", lambda k, m, **kw: _async(True))
    aid = _six_like(clean_tables)
    a = _anime(clean_tables, aid)
    assert A.is_finished(a, set(range(1, 13))) is True, "前提：时间闸已过、只差 coverage —— 不剔就会被判下齐"
    assert A.episode_coverage([aid])[aid] == set(range(1, 9))
    await A.sweep_finished()
    assert _anime(clean_tables, aid).finished_at is None, "第一季的旧行把才播一半的第三季判成了下齐"
    have, seen = A._episode_sets([aid])[aid]
    assert have == set(range(1, 9)), "已下的集里混进了第一季的 sent 行"
    assert seen == set(range(1, 13)), "本季 9..12 的 skipped 行仍算见过"
    assert A.episode_progress([aid])[aid] == (8, 12)


def test_slots_held_by_previous_season_are_reported_missing(clean_tables, cfg):
    """本季 9..12 从没进过库、槽位被第一季占着：这是真缺口，要报出来（以前 seen 被第一季填满、一声不吭）。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    aid = _six_like(clean_tables, tail_status=None)
    assert A.episode_gaps([_anime(clean_tables, aid)]) == {aid: [9, 10, 11, 12]}


def test_over_total_rows_still_trip_the_two_numberings_gate(clean_tables):
    """反向：1..T 齐全 + T+1..T+3 三条超界行（发布晚于首播 + T + 26 周，命中条件①②）——
    超界行不许剔，否则 _coverage_has_two_numberings 失明、本季被判下齐（开了停订会停掉下一季）。"""
    d = date.today() - timedelta(weeks=60)
    rows = [(e, "sent", _at(d, e - 1)) for e in range(1, 13)]
    rows += [(12 + k, "sent", _at(d, 48 + k)) for k in range(1, 4)]
    aid = _mk(clean_tables, total=12, air=d, rows=rows)
    a = _anime(clean_tables, aid)
    late = A._EpRow(13.0, _at(d, 49))
    assert A._episode_cannot_belong(a, late) and not A._slot_impostor(a, late), "前提：超界行命中①②但不算占槽"
    cov = A.episode_coverage([aid])[aid]
    assert {13, 14, 15} <= cov
    assert A.is_finished(a, cov) is False, "超界行被剔掉了：两套编号闸失效"


def test_without_release_time_nothing_changes(clean_tables):
    """release_time 为空的行判不了（_episode_cannot_belong 不判），口径与改之前一样。"""
    aid = _six_like(clean_tables, release=False)
    assert A.episode_coverage([aid])[aid] == set(range(1, 13))
    assert A._episode_sets([aid])[aid][0] == set(range(1, 13))


def test_backfill_extra_search_covers_slots_held_by_previous_season(clean_tables, cfg, monkeypatch):
    """补齐的"按集号补搜"同口径：第一季的旧行占着 9..12 时，这几集也要补搜（只多搜几集，不改入库判据）。"""
    import sources
    queries = []

    class _Stub:
        def __init__(self, *a, **kw):
            pass

        async def fetch(self, content_cache=None):
            return []

    for site in ("nyaa", "mikan"):
        monkeypatch.setitem(sources.SOURCES, site, _Stub)
        monkeypatch.setitem(sources.SEARCH_URL, site, lambda q: queries.append(q) or f"https://x/?q={q}")
    cfg(ANIME_BACKFILL_EXTRA_EPS=4, ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    aid = _six_like(clean_tables, tail_status=None)
    asyncio.run(A.backfill_source(aid))
    tails = {q.rsplit(" ", 1)[-1] for q in queries if " " in q}
    assert {"09", "10", "11", "12"} <= tails, f"占槽的集没有补搜：{sorted(queries)}"


# ==================== A8：缺集尾巴按 max(seen) 那一集的实际发布日重推 ====================

_D = date(2026, 7, 5)


def _a8(total=24):
    return Anime(title="x", season=1, quarter="26C", confirmed=True, rejected=False,
                 total_episodes=total, air_date=_D.isoformat())


def _hiatus_release(n):
    """1..6 按周播，第 6 集之后休播 3 周：第 n(≥7) 集在首播后第 n+2 周。"""
    return _D + timedelta(weeks=n - 1 if n <= 6 else n + 2)


def test_hiatus_still_reported_while_it_lasts(cfg):
    """休播期间（还没恢复）分不清休播与断更：第 8 周照报 [7]（已知取舍，宽限放大请调 ANIME_GAP_SLACK_WEEKS）。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    today = _D + timedelta(weeks=8)
    assert A.missing_episodes(_a8(), set(range(1, 7)), today, tail_from=_hiatus_release(6)) == [7]


@pytest.mark.parametrize("week", [9, 10, 11, 12, 13])
def test_resumed_after_hiatus_is_not_a_gap(cfg, week):
    """恢复周更之后：按首播日推每周都多报一集，按最后一集的实际发布日推则不报。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    today = _D + timedelta(weeks=week)
    seen = set(range(1, week - 1))            # 第 week-2 集恰好今天发布
    assert A.missing_episodes(_a8(), seen, today) == [week - 1], "前提：不给锚点时原逻辑会报"
    assert A.missing_episodes(_a8(), seen, today, tail_from=_hiatus_release(week - 2)) == []


def test_stalled_source_is_reported_as_before(cfg):
    """停摆（1..6 按排期，之后什么都没有）：第 10 周报 [7,8,9]，与原逻辑相同。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    today = _D + timedelta(weeks=10)
    seen = set(range(1, 7))
    assert A.missing_episodes(_a8(), seen, today) == [7, 8, 9]
    assert A.missing_episodes(_a8(), seen, today, tail_from=_D + timedelta(weeks=5)) == [7, 8, 9]


def test_tail_from_never_touches_middle_holes(cfg):
    """只砍 max(seen) 之后的尾巴：中间的洞照报。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    today = _D + timedelta(weeks=9)
    seen = set(range(1, 8)) - {3}
    assert A.missing_episodes(_a8(), seen, today, tail_from=today) == [3]


def test_tail_from_truncates_the_absolute_numbering_branch(cfg):
    """绝对编号分支（续作沿用全系列编号，只能数个数）同样按尾巴上限截断；缺在末尾、按排期发布时与原逻辑等价
    （缺在开头时会晚报缺头集数那么多周，见 missing_episodes 的 docstring，r39 F9）。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    a = _a8(total=8)
    today = _D + timedelta(weeks=9)
    seen = set(range(12, 19))                 # 7 集，第 18 集是本季第 7 集
    assert A.missing_episodes(a, seen, today) == [19]
    assert A.missing_episodes(a, seen, today, tail_from=_D + timedelta(weeks=6)) == [19], "按排期：等价"
    assert A.missing_episodes(a, seen, today, tail_from=today) == [], "第 18 集今天才发：第 19 集还不该报"


def _gap_anime(db, rows):
    d = date.today() - timedelta(weeks=11)
    return _mk(db, total=24, air=d, rows=[(e, st, rel(d) if rel else None, *rest) for e, st, rel, *rest in rows])


def test_gaps_resumed_after_hiatus_via_tail_anchors(clean_tables, cfg):
    """经 episode_gaps 走 _tail_anchors：休播 3 周后恢复、第 9 集今天发布 → 不报；那一集没有发布时间 → 退回原逻辑。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)

    def rel_of(n):
        return lambda d: _at(d, n - 1 if n <= 6 else n + 2)
    aid = _gap_anime(clean_tables, [(n, "sent", rel_of(n)) for n in range(1, 10)])
    assert A.episode_gaps([_anime(clean_tables, aid)]) == {}
    aid2 = _gap_anime(clean_tables, [(n, "sent", rel_of(n) if n < 9 else None) for n in range(1, 10)])
    assert A.episode_gaps([_anime(clean_tables, aid2)]) == {aid2: [10]}, "锚点集全无发布时间时应走原逻辑"


def test_gaps_backfilled_rows_keep_their_real_gap(clean_tables, cfg):
    """回填场景：旧集今天才入库（created_at=今天）但 release_time 按排期 —— 锚点用发布日，真缺口不被压掉。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    aid = _gap_anime(clean_tables, [(n, "sent", (lambda m: lambda d: _at(d, m - 1))(n)) for n in range(1, 7)])
    # 首播 11 周前：应播 10 集；第 6 集按排期发布于第 5 周 → 尾巴上限 6 + 6 − 2 = 10，与原逻辑一致
    assert A.episode_gaps([_anime(clean_tables, aid)]) == {aid: [7, 8, 9, 10]}


def test_gaps_tail_pass_keeps_the_end_mark(clean_tables, cfg):
    """第二遍（带锚点）必须把 [END] 一起带上，否则截断被覆盖。

    [END] 行被人工排除了（不算见过，所以 max(seen)=6 仍会走尾巴那一遍）：应播 10、[END]=8 → 只报 7、8。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    rows = [(n, "sent", (lambda m: lambda d: _at(d, m - 1))(n)) for n in range(1, 7)]
    rows.append((8, "excluded", lambda d: _at(d, 7), "[ANi] 百女友 - 08 [END]"))
    aid = _gap_anime(clean_tables, rows)
    assert A.episode_gaps([_anime(clean_tables, aid)]) == {aid: [7, 8]}


# ==================== A9：撤销完结时把断更时间戳推到现在 ====================

async def test_revoked_finish_does_not_trigger_a_fake_idle_alert(clean_tables, cfg, monkeypatch):
    """已下齐的番删了一集 → sweep_finished 撤销完结，同时把 idle_notified_at 推到现在；
    紧接着的 sweep_idle（最后一条种子已安静 30 天、没有开播日 → r38 的"播完且集齐"豁免不生效）不推送。
    注意这只是【推迟】：没有开播日的番到"撤销时刻 + IDLE_DAYS 天"照样会报一条（r39 F7，见下面时间推进那条用例）；
    有开播日的番不靠这个时间戳，靠与 is_finished 同一时间闸的豁免。"""
    sent = []
    monkeypatch.setattr(A, "notify_event", lambda k, m, **kw: (sent.append(k), _async(True))[1])
    cfg(ANIME_FINISH_ENABLED=True, ANIME_IDLE_DAYS=14)
    aid = _mk(clean_tables, total=2, air=None, rows=[(1, "sent", None), (2, "sent", None)])
    with clean_tables.get_session() as s:
        for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid)):
            t.created_at = datetime.now() - timedelta(days=30)
            s.add(t)
        s.commit()
    assert await A.sweep_finished() == 1
    with clean_tables.get_session() as s:
        s.exec(select(AnimeTorrent).where(AnimeTorrent.episode == 2)).one().status = "deleted"
        s.commit()
    before = datetime.now()
    await A.sweep_finished()
    a = _anime(clean_tables, aid)
    assert a.finished_at is None, "前提：删一集后完结标记应被撤销"
    assert a.idle_notified_at is not None and a.idle_notified_at >= before - timedelta(seconds=5)
    sent.clear()
    assert await A.sweep_idle() == 0
    assert sent == [], "刚撤销完结就送来一条假断更"
    # 对照：时间戳没推的话，同一部番这一轮会被报出来（证明上面那条断言走得到推送分支）
    with clean_tables.get_session() as s:
        s.get(Anime, aid).idle_notified_at = None
        s.commit()
    assert await A.sweep_idle() == 1 and sent == ["idle"]


def test_total_change_does_not_touch_idle_stamp(monkeypatch):
    """反向：bgm 改总集数清 finished_at 时不推断更时间戳 —— 那时番可能真的还在播，断更提醒要照常。"""
    a = Anime(display_name="某番", total_episodes=12, finished_at=datetime(2026, 1, 1))
    monkeypatch.setattr(A.engine, "apply_bgm_meta",
                        lambda obj, info, keep_path=False: setattr(obj, "total_episodes", 24))
    A._apply_bgm(a, {"total_episodes": 24})
    assert a.finished_at is None and a.idle_notified_at is None


# ==================== F7：断更豁免的时间闸与 is_finished 同一个周更模型 ====================

class _Clock(datetime):
    """替换 core.anime 里的 datetime：now() 走模拟时钟，好把巡检往后推几周看。"""
    t = None

    @classmethod
    def now(cls, tz=None):
        return cls.t


async def _finale_then_delete(db, cfg, monkeypatch, *, air: bool, delete_after_days: int) -> list:
    """T=12 的番：终集按周更播出后 1 天（F）判下齐；F + delete_after_days 天删掉终集（撤销下齐）；
    之后 39 天每天跑一次 sweep_idle。返回送出断更的那几天（距 F 的天数）。air=False：没有开播日。"""
    sent = []
    monkeypatch.setattr(A, "notify_event", lambda k, m, **kw: (sent.append(k), _async(True))[1])
    monkeypatch.setattr(A, "datetime", _Clock)
    cfg(ANIME_FINISH_ENABLED=True, ANIME_IDLE_DAYS=14, ANIME_GAP_SLACK_WEEKS=2, ANIME_IDLE_WINDOW_X=4,
        ANIME_POLL_OK_LAST="")
    base = datetime.combine(date.today() - timedelta(weeks=60), datetime.min.time())
    f = base + timedelta(weeks=11, days=1)
    aid = _mk(db, total=12, air=base.date() if air else None, rows=[(e, "sent", None) for e in range(1, 13)])
    with db.get_session() as s:
        for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid)):
            t.created_at = base + timedelta(weeks=t.episode - 1, hours=12)     # 每周入库一集
            s.add(t)
        s.commit()
    _Clock.t = f
    assert await A.sweep_finished() == 1, "前提：终集播出后判下齐"
    _Clock.t = f + timedelta(days=delete_after_days)
    with db.get_session() as s:
        s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid,
                                          AnimeTorrent.episode == 12.0)).one().status = "deleted"
        s.commit()
    await A.sweep_finished()
    assert _anime(db, aid).finished_at is None, "前提：删了终集撤销下齐"
    start, hits = _Clock.t, []
    for i in range(1, 40):
        _Clock.t = start + timedelta(days=i)
        if await A.sweep_idle():
            hits.append((_Clock.t - f).days)
    return hits


@pytest.mark.parametrize("delete_after_days", [0, 2, 5])
async def test_deleting_the_finale_soon_after_never_sends_a_fake_idle(clean_tables, cfg, monkeypatch,
                                                                      delete_after_days):
    """有开播日：看完终集几天内删掉（撤销下齐），之后不送假断更。
    旧闸（首播 +(T+宽限) 周）下 F+2 天删 → F+17 天报一条（A9 的时间戳只推迟了几天）。"""
    hits = await _finale_then_delete(clean_tables, cfg, monkeypatch, air=True,
                                     delete_after_days=delete_after_days)
    assert hits == [], f"撤销下齐后送出了假断更（F+{hits} 天）"


async def test_without_air_date_the_revoke_stamp_only_postpones(clean_tables, cfg, monkeypatch):
    """对照（也证明上面那条走得到推送分支）：没有开播日的番豁免永远不生效，A9 的时间戳只把假断更推迟到
    "撤销时刻 + IDLE_DAYS 天"之后，到时照样报 —— 这是已知残余，注释里写的就是"只是推迟"。"""
    hits = await _finale_then_delete(clean_tables, cfg, monkeypatch, air=False, delete_after_days=2)
    assert hits and hits[0] >= 2 + 14, f"没有开播日的番的断更时机变了：F+{hits} 天"


async def test_idle_exemption_starts_when_the_finale_has_aired(clean_tables, cfg, monkeypatch):
    """豁免的时间闸 = 首播 +(T−1) 周（终集按周更已播），不再等首播 +(T+宽限) 周。
    关着下齐判定（从没判过下齐）、1..12 都见过、最后一条种子安静 15 天：
      · 首播 13 周前（终集 2 周前已播；旧闸要到第 14 周）→ 不报；
      · 首播 10 周前（终集还没到播出日）→ 照报：这时"见过 12 集"可能是两套编号凑出来的，时间闸不能拿掉。"""
    monkeypatch.setattr(A, "notify_event", lambda k, m, **kw: _async(True))
    cfg(ANIME_FINISH_ENABLED=False, ANIME_IDLE_DAYS=14, ANIME_GAP_SLACK_WEEKS=2, ANIME_POLL_OK_LAST="")

    def mk(weeks_ago):
        aid = _mk(clean_tables, total=12, air=date.today() - timedelta(weeks=weeks_ago),
                  rows=[(e, "sent", None) for e in range(1, 13)])
        with clean_tables.get_session() as s:
            for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid)):
                t.created_at = datetime.now() - timedelta(days=15)
                s.add(t)
            s.commit()
        return aid
    done, airing = mk(13), mk(10)
    assert await A.sweep_idle() == 1
    assert _anime(clean_tables, done).idle_notified_at is None, "终集已播、集数收齐的番被报成断更"
    assert _anime(clean_tables, airing).idle_notified_at is not None, "终集还没播的番被豁免了"


# ==================== F8：槽位被别季旧行占着的缺集，提示不再引导去补齐 ====================

def test_slot_held_gaps_marks_slots_taken_by_previous_season(clean_tables, cfg):
    """#6 的形状：缺的 9..12 槽位上都躺着第一季的 sent 行 → 全标成占槽（补齐拿回来也会被『同集已有』挡住）。
    普通缺集（槽位上什么都没有）不标。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    aid = _six_like(clean_tables, tail_status=None)
    gaps = A.episode_gaps([_anime(clean_tables, aid)])
    assert A.slot_held_gaps(gaps) == {aid: [9, 10, 11, 12]}
    plain = _gap_anime(clean_tables, [(n, "sent", (lambda m: lambda d: _at(d, m - 1))(n)) for n in range(1, 7)])
    g2 = A.episode_gaps([_anime(clean_tables, plain)])
    assert g2 == {plain: [7, 8, 9, 10]} and A.slot_held_gaps(g2) == {}


def test_slot_held_only_counts_rows_that_block_the_download(clean_tables, cfg):
    """只认真会挡住下载的占槽行（_COVERED_SQL）：第一季 ep9 已删且是该集最后一份（ep_dropped，E-73）照样挡 → 算；
    ep10 只删了重复份、ep11 还是跳过 —— 都不挡，补齐照常有用 → 不算。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    aid = _six_like(clean_tables, tail_status=None)
    old_before = datetime.now() - timedelta(weeks=100)
    with clean_tables.get_session() as s:
        old = {int(t.episode): t for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid))
               if t.release_time < old_before}
        old[9].status, old[9].ep_dropped = "deleted", True
        old[10].status, old[10].ep_dropped = "deleted", False
        old[11].status = "skipped"
        s.commit()
    gaps = A.episode_gaps([_anime(clean_tables, aid)])
    assert gaps == {aid: [9, 10, 11, 12]}, "前提：占槽行不论状态都不算见过"
    assert A.slot_held_gaps(gaps) == {aid: [9, 12]}


def test_sweep_gaps_does_not_send_slot_held_episodes_to_backfill(clean_tables, cfg, monkeypatch):
    """推送：占槽的集说"槽位被别的季的旧种子占着、先处理绑定"，不叫人去点『自动补齐』；
    没被占的集照旧叫人去点『自动补齐』。去重键仍按全部缺集算（文案变了不该多推一条）。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100, NOTIFY_EVENTS=["gap"], NOTIFY_URL="https://x",
        ANIME_FINISH_UNSUB=False)
    held_only = _six_like(clean_tables, tail_status=None)
    d = date.today() - timedelta(weeks=17)
    rows = [(e, "sent", _at(d, -150 + e)) for e in range(1, 11)]        # 第一季只占到第 10 集
    rows += [(e, "sent", _at(d, e - 1)) for e in range(1, 9)]
    mixed = _mk(clean_tables, total=12, air=d, rows=rows)
    sent = {}

    async def fake(kind, msg, key=None, cooldown=None, **kw):
        sent[key] = msg
        return True
    monkeypatch.setattr(A, "notify_event", fake)
    assert asyncio.run(A.sweep_gaps()) == 2
    m1 = sent[f"{held_only}:9,10,11,12"]
    assert "缺第 9～12 集" in m1 and "槽位被别的季的旧种子占着" in m1 and "『自动补齐』" not in m1
    free, held = sent[f"{mixed}:9,10,11,12"].split("；")
    assert "缺第 11～12 集" in free and "『自动补齐』" in free
    assert "缺第 9～10 集" in held and "槽位被别的季的旧种子占着" in held and "『自动补齐』" not in held
