"""r40：每日摘要推送（core/digest.py）。

钉住三件事：
  · 什么时候发（tick）：到点 = 当前小时 ≥ NOTIFY_DIGEST_AT 且距起算点满 NOTIFY_DIGEST_HOURS 小时
    （整天倍数的间隔起算点回到上次送达那天的 AT 点，其余取上次送达的整点）；
    LAST 为空 / 损坏 / 晚于现在一天以上视作到点；关着 / 没地址 / 没订阅 / 库停摆 / 推送熔断中一律不发也不算文本；
    送达才写 NOTIFY_DIGEST_LAST，没送到按 30 秒起翻倍退避重试（不连坐别的通知）；没到点时一条业务库查询都不发。
  · 发什么（digest_text）：每个数字都与仪表盘 / 巡检现成函数的返回值一致（不手抄期望值）；
    全为 0 时写"没有要你处理的事"；整条不超过 500 字、编码后不超过 3000 字节。
  · 接在哪：worker.run_db_watch 库没停摆的那一支里调它，异常走 loop_error。
"""
import ast
import itertools
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest
import sqlalchemy as sa
from sqlmodel import select

import config
import db
from core import alerts, anime as A, digest as D, health
from db.models import Anime, AnimeTorrent, Movie, MovieTorrent
from services import notify as N

_seq = itertools.count(1)


def _fresh_notify():
    N._last_sent.clear()
    N._sent_times.clear()
    N._dropped = 0
    N._fail_streak, N._muted_until = 0, 0.0


@pytest.fixture
def on(monkeypatch, cfg, testdb):
    """摘要开着、订阅着、地址配好、LAST 为空，库没停摆。
    要 testdb：送达后 LAST 走 config.set_many 真落 meta 库（否则测的是落库失败那一支）。"""
    cfg(NOTIFY_URL="http://push.example/key", NOTIFY_EVENTS=list(N.EVENTS), NOTIFY_MAX_PER_HOUR=0,
        NOTIFY_DIGEST_HOURS=24, NOTIFY_DIGEST_AT=9, NOTIFY_DIGEST_LAST="")
    monkeypatch.setattr(db, "is_data_down", lambda: False)
    _fresh_notify()
    yield
    _fresh_notify()


@pytest.fixture
def push(monkeypatch, on):
    """推送换成记账（ok 控制送没送到）。"""
    box = {"msgs": [], "ok": True}

    async def fake(msg, **kw):
        box["msgs"].append(msg)
        return box["ok"]
    monkeypatch.setattr(N, "notify", fake)
    return box


@pytest.fixture
def stub_text(monkeypatch):
    """只测时机的用例：文本换成桩，记下被算了几次（"先判到点再算文本"）。"""
    calls = []

    def fake(since, now):
        calls.append((since, now))
        return "摘要"
    monkeypatch.setattr(D, "digest_text", fake)
    return calls


T = datetime(2026, 11, 12, 10, 0, 0)


# ==================== 时机 ====================

async def test_due_sends_and_records_last(push, stub_text, caplog):
    caplog.set_level("INFO", logger="autorss")
    assert await D.tick(T) is True
    assert any(r.getMessage() == "每日摘要已发出" for r in caplog.records), "送达要留一行日志，事后查得到"
    assert len(push["msgs"]) == 1 and push["msgs"][0].startswith(N.EVENTS["digest"][1])
    assert config.NOTIFY_DIGEST_LAST == T.isoformat(timespec="seconds")
    assert await D.tick(T + timedelta(seconds=30)) is False, "刚发过，下个节拍不该再发"
    assert len(push["msgs"]) == 1


async def test_before_the_hour_is_not_due(push, stub_text):
    assert await D.tick(datetime(2026, 11, 12, 8, 59, 59)) is False
    assert push["msgs"] == [] and stub_text == [], "没到点就不该算文本"


async def test_interval_not_full_is_not_due(push, stub_text, cfg):
    cfg(NOTIFY_DIGEST_LAST="2026-11-12T09:00:20")
    assert await D.tick(datetime(2026, 11, 12, 23, 59)) is False
    assert await D.tick(datetime(2026, 11, 13, 8, 59)) is False
    assert push["msgs"] == [] and stub_text == []
    assert await D.tick(datetime(2026, 11, 13, 9, 0)) is True, "满 24 小时就发"


async def _run(start: datetime, end: datetime, step=timedelta(seconds=30)):
    """从 start 到 end 每 30 秒一拍调 tick，返回送达时刻列表。"""
    sent, t = [], start
    while t < end:
        if await D.tick(t):
            sent.append(t)
        t += step
    return sent


async def test_first_send_in_the_evening_returns_to_AT(push, stub_text):
    """升级 / 首次启用在晚上 21:37：第一条当场发（LAST 为空），第二天起回到 9 点，而不是从此每天 21 点。"""
    sent = await _run(datetime(2026, 11, 12, 21, 37), datetime(2026, 11, 16, 0, 0))
    assert [s.hour for s in sent] == [21, 9, 9, 9], [s.strftime("%m-%d %H:%M") for s in sent]


async def test_lowering_AT_takes_effect(push, stub_text, cfg):
    """原来每天 14 点发；把『几点之后发』改成 8，第二天就在 8 点发。"""
    cfg(NOTIFY_DIGEST_LAST="2026-11-12T14:00:20", NOTIFY_DIGEST_AT=8)
    sent = await _run(datetime(2026, 11, 13, 0, 0), datetime(2026, 11, 14, 0, 0))
    assert [s.hour for s in sent] == [8], [s.strftime("%m-%d %H:%M") for s in sent]


async def test_one_late_day_does_not_ratchet(push, stub_text, cfg):
    """某天推送坏到 20 点才通：当天 20 点补上，之后回到 9 点。"""
    cfg(NOTIFY_DIGEST_LAST="2026-11-11T09:00:10")
    push["ok"] = False
    await _run(datetime(2026, 11, 12, 9, 0), datetime(2026, 11, 12, 20, 0))
    push["ok"] = True
    sent = await _run(datetime(2026, 11, 12, 20, 0), datetime(2026, 11, 15, 0, 0))
    assert [s.hour for s in sent] == [20, 9, 9], [s.strftime("%m-%d %H:%M") for s in sent]


async def test_non_daily_interval_counts_from_the_hour_of_last_send(push, stub_text, cfg):
    """间隔不是整天倍数（6 小时）：仍从上次送达的整点起算，不往 AT 点拉。"""
    cfg(NOTIFY_DIGEST_HOURS=6, NOTIFY_DIGEST_LAST="2026-11-12T13:30:00")
    assert await D.tick(datetime(2026, 11, 12, 18, 59)) is False
    assert await D.tick(datetime(2026, 11, 12, 19, 0)) is True


async def test_clock_rolled_back_a_day_counts_as_due(push, stub_text, cfg):
    """LAST 比现在晚一天以上（时钟被回拨）：视作到点，否则要静默停发到时钟追上；窗口退回往前 HOURS 小时。"""
    cfg(NOTIFY_DIGEST_LAST=(T + timedelta(days=2)).isoformat(timespec="seconds"))
    assert await D.tick(T) is True
    assert stub_text[0][0] == T - timedelta(hours=24)


async def test_measured_from_the_hour_of_last_send(push, stub_text, cfg):
    """昨天 09:00:25 送达（节拍 + 推送耗时），今天 09:00:05 就该发：不按整点起算的话每天晚几十秒、一年漂两小时。"""
    cfg(NOTIFY_DIGEST_LAST="2026-11-11T09:00:25")
    assert await D.tick(datetime(2026, 11, 12, 9, 0, 5)) is True


@pytest.mark.parametrize("bad", ["garbage", "2026-13-45T99:00:00", "2026-11-11T09:00:00+09:00"])
async def test_broken_last_counts_as_due(push, stub_text, cfg, bad):
    """损坏 / 带时区（与本地时间不能相减）都视作到点，自愈而不是永久不发；窗口退回往前 HOURS 小时。"""
    cfg(NOTIFY_DIGEST_LAST=bad)
    assert await D.tick(T) is True
    assert stub_text[0][0] == T - timedelta(hours=24)


async def test_hours_zero_means_off(push, stub_text, cfg):
    cfg(NOTIFY_DIGEST_HOURS=0)
    assert await D.tick(T) is False
    assert push["msgs"] == [] and stub_text == []


async def test_no_url_means_off(push, stub_text, cfg):
    cfg(NOTIFY_URL="")
    assert await D.tick(T) is False
    assert stub_text == []


async def test_unsubscribed_means_off(push, stub_text, cfg):
    cfg(NOTIFY_EVENTS=[k for k in N.EVENTS if k != "digest"])
    assert await D.tick(T) is False
    assert push["msgs"] == [] and stub_text == []


async def test_failed_delivery_keeps_last_and_retries_next_beat(push, stub_text):
    push["ok"] = False
    assert await D.tick(T) is False
    assert len(push["msgs"]) == 1 and config.NOTIFY_DIGEST_LAST == "", "没送到就不能记"
    push["ok"] = True
    assert await D.tick(T + timedelta(seconds=30)) is True, "下个节拍重试"
    assert config.NOTIFY_DIGEST_LAST == (T + timedelta(seconds=30)).isoformat(timespec="seconds")


async def test_repeated_failures_back_off(push, stub_text):
    """连着没送到：30 秒、1 分、2 分……翻倍退避，最长 1 小时；退避期间不算文本。"""
    push["ok"] = False
    assert await D.tick(T) is False                              # 第 1 次，下次 T+30s
    assert await D.tick(T + timedelta(seconds=30)) is False      # 第 2 次，下次 T+90s
    assert await D.tick(T + timedelta(seconds=60)) is False
    assert len(push["msgs"]) == 2 and len(stub_text) == 2, "退避期间不该再试"
    assert await D.tick(T + timedelta(seconds=90)) is False
    assert len(push["msgs"]) == 3
    D._fails = 30
    await D.tick(T + timedelta(hours=1))
    assert D._retry_at == T + timedelta(hours=2), "退避上限 1 小时"


async def test_delivery_clears_backoff(push, stub_text, cfg):
    """送达后退避清零：下一次（隔天）再失败，又从 30 秒起退避，而不是接着上回翻倍。"""
    push["ok"] = False
    for sec in (0, 30, 90, 210):
        await D.tick(T + timedelta(seconds=sec))
    push["ok"] = True
    assert await D.tick(T + timedelta(seconds=450)) is True
    push["ok"] = False
    nxt = T + timedelta(days=1)
    assert await D.tick(nxt) is False
    assert D._retry_at == nxt + timedelta(seconds=30)


async def test_backoff_ignores_a_clock_rolled_back(push, stub_text):
    """时钟被往回拨两小时：退避截止点离现在超过 1 小时，按没有退避处理，而不是一直等到时钟追上。"""
    later = datetime(2026, 11, 12, 15, 0)
    D._fails, D._retry_at = 1, later + timedelta(seconds=30)
    assert await D.tick(later - timedelta(hours=2)) is True


async def test_reset_retry_clears_backoff_and_settings_page_calls_it(push, stub_text):
    D._fails, D._retry_at = 5, T + timedelta(minutes=10)
    D.reset_retry()
    assert await D.tick(T) is True
    src = (Path(__file__).resolve().parent.parent / "pages" / "settings.py").read_text(encoding="utf-8")
    assert "notify.reset_state()\n                    digest.reset_retry()" in src, "改了推送设置要清摘要的退避"


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def real_stack(monkeypatch, on, stub_text):
    """走真的 notify.event / notify.notify（熔断、_dropped 都是真的），只换掉 _send_once 和 monotonic。"""
    clock = _Clock()
    monkeypatch.setattr(time, "monotonic", clock)
    box = {"tried": [], "fail": lambda m: False, "clock": clock}

    async def send(message, base=""):
        box["tried"].append(message)
        return not box["fail"](message)
    monkeypatch.setattr(N, "_send_once", send)
    return box


async def test_digest_only_failure_does_not_mute_other_events(real_stack):
    """只有摘要发不出去（比如正文太长被拒），6 小时里每 10 分钟一条交付通知：交付通知照常送达，
    不能被摘要反复失败续上的共享熔断丢掉。"""
    icon = N.EVENTS["digest"][1]
    real_stack["fail"] = lambda m: m.startswith(icon)
    now, ok, total = T, 0, 0
    for beat in range(6 * 120):
        if beat % 20 == 7:
            total += 1
            ok += bool(await N.event("delivered", f"某番 第 {beat} 集"))
        await D.tick(now)
        now += timedelta(seconds=30)
        real_stack["clock"].t += 30
    assert total and ok == total, f"{total} 条交付通知只送达 {ok} 条"
    assert sum(1 for m in real_stack["tried"] if m.startswith(icon)) >= 2, "摘要确实试过、失败过"


async def test_digest_only_failure_with_busy_channel_is_bounded(real_stack):
    """别的通知来得勤（连续失败计数一直被清零、熔断合不上）：摘要一小时内的重试仍有上界。"""
    icon = N.EVENTS["digest"][1]
    real_stack["fail"] = lambda m: m.startswith(icon)
    now = T
    for beat in range(120):
        if beat % 4 == 1:
            await N.event("delivered", f"某番 第 {beat} 集")
        await D.tick(now)
        now += timedelta(seconds=30)
        real_stack["clock"].t += 30
    tried = sum(1 for m in real_stack["tried"] if m.startswith(icon))
    assert 2 <= tried <= 12, f"1 小时内摘要重试了 {tried} 次"


async def test_rate_limit_bucket_does_not_block_it(push, stub_text, cfg):
    """每小时限流桶被交付通知打满时照样发（一天一条，不该被挤掉），也不占桶。"""
    cfg(NOTIFY_MAX_PER_HOUR=1)
    N._sent_times[:] = [time.monotonic()]
    assert await D.tick(T) is True
    assert len(N._sent_times) == 1


async def test_muted_push_is_not_tried(push, stub_text):
    """推送熔断中：不试、不算文本，也不把它记成一条"没能送到"。"""
    N._muted_until = time.monotonic() + 300
    assert await D.tick(T) is False
    assert push["msgs"] == [] and stub_text == [] and N._dropped == 0


async def test_skipped_while_business_db_down(push, stub_text, monkeypatch):
    monkeypatch.setattr(db, "is_data_down", lambda: True)
    assert await D.tick(T) is False
    assert push["msgs"] == [] and stub_text == []


async def test_window_looks_back_at_most_three_intervals(push, stub_text, cfg):
    """摘要关了几个月再打开：窗口最多回看 3 个间隔，不写"近 2160 小时"。"""
    cfg(NOTIFY_DIGEST_LAST=(T - timedelta(days=100)).isoformat(timespec="seconds"))
    assert await D.tick(T) is True
    assert stub_text[0] == (T - timedelta(hours=72), T)


async def test_window_starts_at_last_send(push, stub_text, cfg):
    """中间停过机：窗口从上次送达算起（三天前），不只看最近 24 小时。"""
    last = T - timedelta(days=3)
    cfg(NOTIFY_DIGEST_LAST=last.isoformat(timespec="seconds"))
    assert await D.tick(T) is True
    assert stub_text[0] == (last, T)


async def test_record_failure_still_stops_resending(push, stub_text, monkeypatch):
    """送达了但 LAST 落库失败（meta 库被备份锁住）：本进程内也不能每 30 秒重发一条。"""
    def boom(_):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(config, "set_many", boom)
    assert await D.tick(T) is True
    assert await D.tick(T + timedelta(seconds=30)) is False
    assert len(push["msgs"]) == 1


async def test_not_due_issues_no_business_sql(clean_tables, push):
    """没到点时一条业务库查询都不发（看守 30 秒一拍）；到点那一拍确实查了库（监听不是摆设）。"""
    stmts = []

    def spy(conn, cursor, statement, *a):
        stmts.append(statement)
    sa.event.listen(clean_tables.engine, "before_cursor_execute", spy)
    try:
        assert await D.tick(datetime(2026, 11, 12, 8, 0)) is False
        assert stmts == [], stmts
        now = datetime.now()
        assert await D.tick(now.replace(hour=max(now.hour, 9))) is True
        assert stmts, "到点那一拍应当查库算文本"
    finally:
        sa.event.remove(clean_tables.engine, "before_cursor_execute", spy)


# ==================== 文本 ====================

def _anime(s, **kw):
    base = dict(title="番", display_name="番", season=1, quarter="26D", confirmed=True, rejected=False,
                bangumi_id=next(_seq) + 1000)
    base.update(kw)
    a = Anime(**base)
    s.add(a)
    s.commit()
    s.refresh(a)
    return a.id


def _row(s, aid, ep, status, at, src="ANi", rel=None):
    n = next(_seq)
    s.add(AnimeTorrent(anime_id=aid, info_hash=f"{n:040x}", source=src, site="nyaa", raw_title=f"[{src}] x - {ep}",
                       episode=float(ep), status=status, created_at=at, release_time=rel or at,
                       download_url=f"https://x/{n}.torrent"))


@pytest.fixture
def quiet(cfg, monkeypatch):
    """系统一切正常：采集关着且从没采过（poll_problem 不判）、没有源在失败。"""
    cfg(ANIME_POLL_ENABLED=False, ANIME_POLL_LAST="", ANIME_FINISH_UNSUB=False, FEED_GAPS="", QB_ENABLED=True,
        ANIME_LOCK_LAG_DAYS=7, ANIME_LOCK_SYNC_DAYS=2, ANIME_IGNORED_AIRING_DAYS=30,
        ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    monkeypatch.setattr(health, "_fail", {})


def test_every_number_matches_the_dashboard(clean_tables, quiet, cfg):
    cfg(FEED_GAPS='[{"src": "Mikan", "from": "2026-11-01T01:00:00", "to": "2026-11-02T03:00:00", "at": ""},'
                  ' {"src": "nyaa", "from": "2026-11-03T01:00:00", "to": "2026-11-03T05:00:00", "at": ""}]')
    now = datetime.now()
    since = now - timedelta(hours=24)
    with clean_tables.get_session() as s:
        # 下了什么：窗口内 2 部 3 集有着落；窗口内 pending、窗口外 sent 都不算
        a1 = _anime(s, display_name="甲番")
        a2 = _anime(s, display_name="乙番")
        _row(s, a1, 1, "sent", now - timedelta(hours=2))
        _row(s, a1, 2, "sent", now - timedelta(hours=1))
        _row(s, a1, 2, "stalled", now - timedelta(hours=1), src="B")   # 同集第二份不重复计集数
        _row(s, a2, 5, "downloading", now - timedelta(hours=3))
        _row(s, a2, 6, "pending", now - timedelta(hours=3))
        _row(s, a2, 4, "sent", now - timedelta(days=3))
        _row(s, a2, 7, "error", now - timedelta(days=3))
        # 待确认 / 待识别
        _anime(s, display_name="丙番", confirmed=False, confirm_reason="review")
        _anime(s, display_name="丁番", confirmed=False, confirm_reason="wrong_season")
        _anime(s, display_name="戊番", confirmed=False, confirm_reason="bind")
        _anime(s, display_name="未识别", bangumi_id=None, confirmed=False)
        # 缺集：开播 8 周，见过 1~3、5、6
        air = date.today() - timedelta(weeks=8)
        g = _anime(s, display_name="缺集番", total_episodes=12, air_date=air.isoformat())
        for e in (1, 2, 3, 5, 6):
            at = datetime(air.year, air.month, air.day) + timedelta(weeks=e - 1)
            _row(s, g, e, "sent", at, rel=at)
        # 锁定源落后：锁喵萌，喵萌停在第 3 集，ANi 第 4~6 集早已入库
        lk = _anime(s, display_name="锁番", total_episodes=12, pref_source="喵萌")
        for e in range(1, 7):
            _row(s, lk, e, "skipped" if e <= 3 else "pending", now - timedelta(days=60 - 7 * e))
        for e in range(1, 4):
            _row(s, lk, e, "sent", now - timedelta(days=60 - 7 * e - 1), src="喵萌")
        # 未读提示：超期忽略的番最近还在收新集
        ig = _anime(s, display_name="忽略番", confirmed=False, rejected=True)
        _row(s, ig, 3, "skipped", now - timedelta(days=1))
        # 剧场版：窗口内 2 个可下载新版本；窗口外的、已下的不算
        m = Movie(title="剧场版", mikan_id="m1")
        s.add(m)
        s.commit()
        s.refresh(m)
        for i, (st, at) in enumerate((("pending", now - timedelta(hours=1)), ("pending", now - timedelta(hours=5)),
                                      ("pending", now - timedelta(days=3)), ("sent", now - timedelta(hours=1)))):
            s.add(MovieTorrent(info_hash=f"{9000 + i:040x}", movie_id=m.id, status=st, created_at=at))
        s.commit()

    text = D.digest_text(since, now)
    line1, line2, line3 = text.split("\n")

    assert line1.startswith("近 24 小时约下了 3 集（2 部）："), line1
    assert "甲番" in line1 and "乙番" in line1

    ov = A.overview()
    pend = A.pending_confirm()
    with clean_tables.get_session() as s:
        locked = list(s.exec(select(Anime).where(Anime.pref_source.is_not(None), Anime.pref_source != "")))
        subs = list(s.exec(select(Anime).where(*A.subscribed_where())))
    lag = A.locked_out_episodes(locked)
    live = alerts.view()["live"]
    fails = ov["status"]["error"] + ov["status"]["stalled"]
    # 前提：每一项都非 0，否则"数字一致"会因为那一项根本没写出来而空过
    assert len(pend) and ov["kpi"]["fail"] and ov["kpi"]["gaps"] and lag and fails and live and health.gaps()
    assert ov["kpi"]["gaps"] == len(A.episode_gaps(subs)), "仪表盘缺集与巡检同口径"
    assert ov["kpi"]["confirm"] == len(pend)

    assert line2.startswith(f"等你处理：待确认 {len(pend)} 部（"), line2
    assert sum(f"{A.display_of(a)}·{A.confirm_reason_text(a.confirm_reason)[0]}" in line2 for a in pend) == 2
    assert " 等）" in line2, "待确认超过 2 部要写『等』"
    for frag in (f"待识别 {ov['kpi']['fail']} 部", f"缺集 {ov['kpi']['gaps']} 部", f"锁定源落后 {len(lag)} 部",
                 f"失败/异常 {fails} 条", f"未读提示 {len(live)} 条", "剧场版新版本 2 个",
                 f"RSS 断档 {len(health.gaps())} 段"):
        assert frag in line2, (frag, line2)
    assert line3 == "系统：正常"


def test_line1_counts_regular_episodes_only_and_says_days(clean_tables, quiet):
    """特别篇 / 未知集（episode ≤ 0）不算进"下了 X 集"；窗口 ≥ 48 小时按天写。"""
    now = datetime.now()
    with clean_tables.get_session() as s:
        a = _anime(s, display_name="甲番")
        _row(s, a, 1, "sent", now - timedelta(hours=1))
        _row(s, a, 0, "sent", now - timedelta(hours=1))
        _row(s, a, -1, "sent", now - timedelta(hours=1))
        s.commit()
    assert D.digest_text(now - timedelta(hours=24), now).startswith("近 24 小时约下了 1 集（1 部）：甲番")
    assert D.digest_text(now - timedelta(hours=72), now).startswith("近 3 天约下了 1 集（1 部）")


def test_line1_names_top_three_by_count_and_marks_more(clean_tables, quiet):
    now = datetime.now()
    with clean_tables.get_session() as s:
        for name, n in (("一集", 1), ("四集", 4), ("三集", 3), ("两集", 2)):
            a = _anime(s, display_name=name)
            for e in range(1, n + 1):
                _row(s, a, e, "sent", now - timedelta(hours=1))
        s.commit()
    assert D.digest_text(now - timedelta(hours=24), now).split("\n")[0].endswith("：四集、三集、两集…")


def test_qb_disabled_is_reported_like_the_dashboard(clean_tables, quiet, cfg):
    cfg(QB_ENABLED=False)
    now = datetime.now()
    assert A.overview()["config"]["qb"] is False
    assert D.digest_text(now - timedelta(hours=24), now).split("\n")[2] == "系统：qB 未启用，只采集不下载"


def test_all_zero_says_nothing_to_do(clean_tables, quiet):
    now = datetime.now()
    text = D.digest_text(now - timedelta(hours=24), now)
    assert text == "近 24 小时没有新下的\n没有要你处理的事\n系统：正常", text


def test_system_line_uses_existing_problem_texts(clean_tables, quiet, monkeypatch):
    monkeypatch.setattr(health, "poll_problem", lambda: "采集停摆了")
    monkeypatch.setattr(health, "sources_problem", lambda: "源挂了")
    monkeypatch.setattr(N, "problem", lambda: "推送连续失败", raising=False)
    now = datetime.now()
    assert D.digest_text(now - timedelta(hours=24), now).split("\n")[2] == "系统：采集停摆了；源挂了；推送连续失败"


def test_total_length_is_capped(clean_tables, quiet, monkeypatch):
    now = datetime.now()
    with clean_tables.get_session() as s:
        for i in range(8):
            aid = _anime(s, display_name=f"名字非常非常长的一部番剧第{i}部" * 6)
            _row(s, aid, 1, "sent", now - timedelta(hours=1))
        for i in range(5):
            _anime(s, display_name="待确认的长名字" * 10, confirmed=False, confirm_reason="review")
        s.commit()
    monkeypatch.setattr(health, "poll_problem", lambda: "停" * 400)
    monkeypatch.setattr(health, "sources_problem", lambda: "源" * 400)
    text = D.digest_text(now - timedelta(hours=24), now)
    assert len(text) <= 500, len(text)
    assert text.count("\n") == 2 and "待确认 5 部" in text
    # 推送走 GET、编码进 URL：bark-server 请求头上限 4096 字节，正文要给密钥与请求头留出约 1 KB
    assert len(quote(text, safe="")) <= 3000, len(quote(text, safe=""))


def test_clip_bytes_cuts_on_a_whole_character():
    s = "汉" * 10 + "😀" * 10
    for b in range(9, 200, 7):
        out = D._clip_bytes(s, b)
        assert len(quote(out, safe="")) <= b and out.endswith("…")
        out.encode("utf-8")
    assert D._clip_bytes("abc", 9) == "abc"


def test_longest_todo_line_is_not_clipped(clean_tables, quiet, monkeypatch):
    """第二行每一项都顶满（待确认两部长番名、各项四位数）：一项都不能被字节封顶截掉。"""
    class _P:
        def __init__(self):
            self.display_name, self.title, self.confirm_reason = "长" * 40, "长" * 40, "wrong_season"
    monkeypatch.setattr(A, "overview", lambda: {"kpi": {"fail": 9999, "gaps": 9999},
                                                "status": {"error": 9999, "stalled": 9999},
                                                "config": {"qb": True}})
    monkeypatch.setattr(A, "pending_confirm", lambda: [_P() for _ in range(9999)])
    monkeypatch.setattr(A, "display_of", lambda a: a.display_name)
    monkeypatch.setattr(A, "locked_out_episodes", lambda _: list(range(9999)))
    monkeypatch.setattr(alerts, "view", lambda: {"live": list(range(9999))})
    monkeypatch.setattr(health, "gaps", lambda: list(range(9999)))
    now = datetime.now()
    line2 = D.digest_text(now - timedelta(hours=24), now).split("\n")[1]
    assert not line2.endswith("…") and line2.endswith("RSS 断档 9999 段"), line2


# ==================== 配置与接线 ====================

def test_settings_keys_registered():
    assert "digest" in config._SPEC["NOTIFY_EVENTS"][1].split(","), "新库默认就订阅"
    assert config._SPEC["NOTIFY_DIGEST_HOURS"] == (int, 24) and config._RANGE["NOTIFY_DIGEST_HOURS"] == (0, 720)
    assert config._SPEC["NOTIFY_DIGEST_AT"] == (int, 9) and config._RANGE["NOTIFY_DIGEST_AT"] == (0, 23)
    assert "NOTIFY_DIGEST_LAST" in config._RUNTIME_KEYS


def test_run_db_watch_calls_tick_in_the_db_up_branch():
    """AST：digest.tick() 在 run_db_watch 的 `if not now_down:` 分支里、外面套着走 loop_error 的 try。"""
    src = (Path(__file__).resolve().parent.parent / "core" / "worker.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_db_watch")
    found = False
    for br in ast.walk(fn):
        if not (isinstance(br, ast.If) and ast.unparse(br.test) == "not now_down"):
            continue
        for tr in ast.walk(br):
            if not isinstance(tr, ast.Try):
                continue
            calls = {ast.unparse(c.func) for st in tr.body for c in ast.walk(st) if isinstance(c, ast.Call)}
            handled = any("loop_error" in ast.unparse(h) for h in tr.handlers)
            found |= "digest.tick" in calls and handled
    assert found, "run_db_watch 库没停摆的那一支里没调 digest.tick（或没走 loop_error）"


async def test_run_db_watch_survives_a_failing_tick(monkeypatch, caplog):
    """一拍跑下来：tick 被调到；它抛异常时记一行 ERROR、看守协程照常睡到下一拍。"""
    import asyncio

    from core import worker as W

    calls = []

    async def boom():
        calls.append(1)
        raise RuntimeError("坏了")

    async def noop(*a, **kw):
        return None
    monkeypatch.setattr(W.digest, "tick", boom)
    monkeypatch.setattr(W, "notify_state", noop)
    monkeypatch.setattr(W, "_check_poll_health", noop)
    monkeypatch.setattr(db, "probe_data_engine", lambda: "")
    monkeypatch.setattr(db, "is_data_down", lambda: False)
    monkeypatch.setattr(config, "loaded_from_db", True, raising=False)

    async def fast_sleep(_):
        raise asyncio.CancelledError
    monkeypatch.setattr(W.asyncio, "sleep", fast_sleep)
    with pytest.raises(asyncio.CancelledError):
        await W.run_db_watch()
    assert calls == [1]
    assert any("每日摘要" in r.getMessage() for r in caplog.records)
