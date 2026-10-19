"""r37 全方面审计之后的修复（docs/audit-2026-10-r37.md）。

真实事故：2026-09-27 宿主机关机，容器停了 15 天；回来之后 5 部 10 月新番缺第 1 集，全程零提醒。
另有锁定源断更挡住别的组的后续集（碧蓝航线 6~12 集被挡两个多月）、航海王四位集号被截成 117。
"""
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from core import anime as A
from core import health
from db.models import Anime, AnimeTorrent

TODAY = date(2026, 10, 19)


def _a(**kw):
    base = dict(title="x", season=1, quarter="26D", confirmed=True, rejected=False,
                total_episodes=12, air_date="2026-07-02")
    base.update(kw)
    return Anime(**base)


# ── 缺集提示的三道新闸（#14 与尼古喵喵）────────────────────────────────

def test_mixed_numbering_is_not_reported(cfg):
    """杀手旅店 2：Nix-Raws 用季内号（2），ANi 用绝对号（14、15）。以前被判"缺第 1 集"——
    而 ANi 的 14 就是第 1 集，照提示去补会多下一份。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    a = _a(total_episodes=24, air_date="2026-10-01")       # 到 10/19 最多播到第 3 集
    assert A.missing_episodes(a, {2, 14, 15}, TODAY) == []
    assert A.missing_episodes(a, {2, 3}, TODAY) == [1], "编号正常时第 1 集照报"


def test_long_series_is_not_judged(cfg):
    """航海王 1191 集只收了最近 4 集：报"缺 1187 集"是噪音。上限调大就会报出来——证明是这道闸挡的。"""
    a = _a(total_episodes=1191, air_date="1999-10-20")
    seen = {1174, 1175, 1176, 1177}
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    assert A.missing_episodes(a, seen, TODAY) == []
    cfg(ANIME_GAP_MAX_EPISODES=5000)
    assert len(A.missing_episodes(a, seen, TODAY)) == 1191 - 4


def test_end_marker_caps_the_expected_count(clean_tables, cfg):
    """尼古喵喵：bgm 记 13 集，LoliHouse 第 12 集标了 [END] —— 第 13 集不存在，不报缺。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    now = datetime.now()
    with clean_tables.get_session() as s:
        ended = _a(title="打了END的番", total_episodes=13, air_date="2025-01-05")
        plain = _a(title="没打END的番", total_episodes=13, air_date="2025-01-05")
        s.add(ended); s.add(plain); s.commit(); s.refresh(ended); s.refresh(plain)
        n = 0
        for a, mark in ((ended, "[END]"), (plain, "")):
            for ep in range(1, 13):
                n += 1
                s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{n:040x}", episode=ep, status="sent",
                                   raw_title=f"[LoliHouse] 某番 - {ep:02d} [WebRip 1080p]{mark if ep == 12 else ''}",
                                   created_at=now))
        s.commit()
        ids = (ended.id, plain.id)
        animes = [s.get(Anime, i) for i in ids]
    assert A.episode_gaps(animes) == {ids[1]: [13]}


# ── 锁定源落后（#2）────────────────────────────────────────────────

def _lib(s, lock, rows, **kw):
    """rows: [(源, 集号, 状态, 入库时刻)]"""
    a = _a(title=kw.pop("title", "锁源番"), pref_source=lock, **kw)
    s.add(a); s.commit(); s.refresh(a)
    for i, (src, ep, st, at) in enumerate(rows):
        s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{a.id:08x}{i:032x}", source=src, episode=ep,
                           status=st, raw_title=f"[{src}] 某番 - {ep}", created_at=at))
    s.commit()
    return a.id


def test_stopped_lock_source_is_reported(clean_tables, cfg):
    """碧蓝航线：锁喵萌，喵萌停在第 5 集；ANi 的 6~8 集几天后陆续出来 → 6~8 被挡。"""
    cfg(ANIME_LOCK_LAG_DAYS=7)
    t0 = datetime(2026, 7, 6)
    rows = [("喵萌奶茶屋", e, "sent", t0 + timedelta(days=7 * (e - 1))) for e in range(1, 6)]
    rows += [("ANi", e, "pending", t0 + timedelta(days=7 * (e - 1))) for e in range(1, 9)]
    with clean_tables.get_session() as s:
        aid = _lib(s, "喵萌奶茶屋", rows)
        a = s.get(Anime, aid)
    got = A.locked_out_episodes([a], today=datetime(2026, 10, 19))
    assert got == {aid: {"eps": [6, 7, 8], "groups": ["ANi"], "lock_max": 5}}, got


def test_two_numberings_in_step_are_not_reported(clean_tables, cfg):
    """锁定源用季内号 1~3、别的组用绝对号 14~16，两边同一周发 —— 不是落后，不报。"""
    cfg(ANIME_LOCK_LAG_DAYS=7)
    t0 = datetime(2026, 7, 6)
    rows = [("LoliHouse", e, "sent", t0 + timedelta(days=7 * (e - 1), hours=5)) for e in (1, 2, 3)]
    rows += [("ANi", e + 13, "pending", t0 + timedelta(days=7 * (e - 1))) for e in (1, 2, 3)]
    with clean_tables.get_session() as s:
        aid = _lib(s, "LoliHouse", rows, total_episodes=13)
        a = s.get(Anime, aid)
    assert A.locked_out_episodes([a], today=datetime(2026, 10, 19)) == {}


def test_recent_episodes_wait_for_the_lag(clean_tables, cfg):
    """别的组 3 天前才发的那一集：锁定源可能只是晚几天，过了 ANIME_LOCK_LAG_DAYS 才报。0=关。"""
    t0 = datetime(2026, 10, 1)
    rows = [("ANi", 13, "sent", t0), ("绿茶字幕组", 14, "pending", datetime(2026, 10, 16))]
    with clean_tables.get_session() as s:
        aid = _lib(s, "ANi", rows, total_episodes=14)
        a = s.get(Anime, aid)
    cfg(ANIME_LOCK_LAG_DAYS=7)
    assert A.locked_out_episodes([a], today=datetime(2026, 10, 19)) == {}
    assert aid in A.locked_out_episodes([a], today=datetime(2026, 10, 24))
    cfg(ANIME_LOCK_LAG_DAYS=0)
    assert A.locked_out_episodes([a], today=datetime(2026, 10, 24)) == {}


def test_locked_bucket_on_the_dashboard(clean_tables, cfg):
    """仪表盘：被锁挡住、一份都没有的那几条进『锁源缺集』，不再算『备用项』；七档之和仍等于待下总数。"""
    cfg(ANIME_FINISH_UNSUB=False)
    t0 = datetime(2026, 7, 6)
    rows = [("喵萌奶茶屋", 1, "sent", t0), ("ANi", 1, "pending", t0),      # 同集已有一份 → 备用项
            ("ANi", 2, "pending", t0), ("ANi", 3, "pending", t0)]          # 锁定源没有 → 锁源缺集
    with clean_tables.get_session() as s:
        _lib(s, "喵萌奶茶屋", rows)
    b = A.pending_breakdown()
    assert (b["locked"], b["backup"]) == (2, 1), b
    assert sum(b.values()) == 3
    assert [r["name"] for r in A.locked_out_rows()] == ["锁源番"]


# ── 航海王四位集号（#4）────────────────────────────────────────────

def test_truncated_four_digit_episodes_are_repaired(clean_tables):
    now = datetime.now()
    with clean_tables.get_session() as s:
        a = _a(title="海贼王", total_episodes=1191)
        s.add(a); s.commit(); s.refresh(a)
        for i, (ep, st) in enumerate(((1174, "pending"), (1175, "skipped"), (1176, "sent"))):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{i + 1:040x}", episode=117, status=st,
                               raw_title=f"[Nix-Raws] 海贼王 / One Piece S01E{ep} [CR WEB-DL 1080p]",
                               created_at=now))
        s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{9:040x}", episode=117, status="pending",
                           raw_title="[ANi] 海贼王 - 117 [1080P]", created_at=now))   # 真的第 117 集
        s.commit()
        aid = a.id
    assert A.repair_truncated_episodes() == 2
    assert A.repair_truncated_episodes() == 0, "幂等"
    with clean_tables.get_session() as s:
        got = {t.info_hash[-1]: (t.episode, t.status) for t in
               s.exec(A.select(AnimeTorrent).where(AnimeTorrent.anime_id == aid))}
    assert got == {"1": (1174.0, "pending"),
                   "2": (1175.0, "pending"),     # 当初被"同集已有一份"挡成 skipped 的，放回待下
                   "3": (117.0, "sent"),         # 下过的连同盘上文件，不碰
                   "9": (117.0, "pending")}, got  # 真的第 117 集，不碰


# ── 采集健康（#1 #7）────────────────────────────────────────────────

@pytest.fixture
def fresh_health(monkeypatch, cfg):
    monkeypatch.setattr(health, "_seen", {})
    monkeypatch.setattr(health, "_fail", {})
    monkeypatch.setattr(health, "_last_activity", datetime.now())
    cfg(SOURCE_FEED_SEEN="", FEED_GAPS="")
    return health


def test_window_overflow_is_detected(fresh_health):
    """上次成功抓取之后、这次 feed 里最老一条之前的那段，已经滚出了 RSS。"""
    t1 = datetime(2026, 9, 27, 6, 4, tzinfo=timezone.utc)
    url = "https://nyaa.si/?page=rss&u=ANiTorrent"
    assert health.check_window(url, t1 - timedelta(days=9), now=t1) is None, "第一次只记时刻"
    t2 = datetime(2026, 10, 12, 9, 20, tzinfo=timezone.utc)
    oldest = datetime(2026, 10, 3, 4, 0, tzinfo=timezone.utc)
    assert health.check_window(url, oldest, now=t2) == (t1, oldest)
    t3 = t2 + timedelta(minutes=20)
    assert health.check_window(url, oldest, now=t3) is None, "正常轮询：最老一条早于上次，没有断档"


def test_window_state_survives_restart(fresh_health, monkeypatch, cfg):
    """断档恰恰发生在重启前后：上次成功的时刻要能从持久化的 SOURCE_FEED_SEEN 读回来。"""
    url = "https://mikanani.me/RSS/Classic"
    t1 = datetime(2026, 9, 27, 6, 4, tzinfo=timezone.utc)
    health.check_window(url, t1 - timedelta(days=1), now=t1)
    cfg(SOURCE_FEED_SEEN=health.seen_snapshot())
    monkeypatch.setattr(health, "_seen", None)          # 模拟重启：内存里的没了
    t2 = datetime(2026, 10, 12, 9, 20, tzinfo=timezone.utc)
    assert health.check_window(url, t2 - timedelta(hours=20), now=t2) is not None


def test_poll_problem_paused_and_stalled(fresh_health, cfg, monkeypatch):
    now = datetime(2026, 10, 19, 12, 0)
    cfg(ANIME_POLL_ENABLED=False, ALERT_POLL_PAUSED_HOURS=6, ANIME_POLL_LAST="2026-10-19T09:00:00")
    assert health.poll_problem(now) is None, "暂停 3 小时：还没到阈值"
    cfg(ANIME_POLL_LAST="2026-10-19T04:00:00")
    assert "采集已暂停" in health.poll_problem(now)
    cfg(ANIME_POLL_LAST="")
    assert health.poll_problem(now) is None, "全新库从没采过：那是还没配好，不报"
    cfg(ANIME_POLL_ENABLED=True, ALERT_POLL_STALL_MIN=90, ANIME_POLL_INTERVAL=1200)
    monkeypatch.setattr(health, "_last_activity", now - timedelta(minutes=30))
    assert health.poll_problem(now) is None
    monkeypatch.setattr(health, "_last_activity", now - timedelta(hours=3))
    assert "没有任何动静" in health.poll_problem(now)


def test_stall_threshold_follows_a_long_poll_interval(fresh_health, cfg):
    """轮询间隔调到 2 小时的人：90 分钟没动静是正常的，阈值取 3 个间隔。"""
    cfg(ALERT_POLL_STALL_MIN=90, ANIME_POLL_INTERVAL=7200)
    assert health.stall_threshold() == timedelta(hours=6)


def test_source_failure_streak(fresh_health, cfg):
    cfg(ALERT_SOURCE_FAIL_ROUNDS=3)
    for _ in range(2):
        health.record_source("Mikan(兜底)", False, "HTTPStatusError: 403")
    assert health.failing_sources() == []
    health.record_source("Mikan(兜底)", False, "HTTPStatusError: 403")
    assert health.failing_sources() == [("Mikan(兜底)", 3, "HTTPStatusError: 403")]
    health.record_source("Mikan(兜底)", True)
    assert health.failing_sources() == []
    health.record_source("已删掉的组", False, "x")
    health.forget_sources({"ANi"})
    assert health.fail_streak("已删掉的组") == 0


# ── 源返回的不是 RSS（#6）───────────────────────────────────────────

def test_non_feed_is_a_fetch_failure():
    from sources.base import NotAFeed
    from sources.nyaa import NyaaSource
    src = NyaaSource("ANi", "https://example.invalid/rss")
    with pytest.raises(NotAFeed):
        src._parse_feed(b'<?xml version="1.0"?><maintenance><msg>down</msg></maintenance>')
    with pytest.raises(NotAFeed):
        src._parse_feed(b"")
    ok = src._parse_feed(b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
                         b'<item><title>x</title><pubDate>Sat, 03 Oct 2026 04:00:00 -0000</pubDate></item>'
                         b'</channel></rss>')
    assert ok == [], "正经的 feed 照常解析（这条没有 hash，被丢掉）"
    assert src.oldest_entry_time == datetime(2026, 10, 3, 4, 0, tzinfo=timezone.utc), "最老一条的时间算的是过滤之前的"


# ── 补齐：别的组只统计不入库，挑定了才补（#10）──────────────────────────

def _item(group, ep, n, title="尼古喵喵", season=1, site="mikan"):
    from sources.base import ParsedItem
    return ParsedItem(info_hash=f"{n:040x}", raw_title=f"[{group}] {title} - {ep:02d} [1080p]",
                      anime_title=title, season=season, episode=float(ep), quarter="26C",
                      release_time=None, download_url=f"https://x/{n}.torrent", source=group,
                      site=site, search_names=[title])


def test_backfill_lists_other_groups_and_can_take_one(clean_tables, cfg, monkeypatch):
    import sources
    found = [_item("NEST", e, 100 + e) for e in range(1, 13)] + [_item("ANi", 7, 200)]

    class _Stub:
        def __init__(self, *a, **kw):
            pass

        async def fetch(self, content_cache=None):
            return list(found)

    for site in ("nyaa", "mikan"):
        monkeypatch.setitem(sources.SOURCES, site, _Stub)
        monkeypatch.setitem(sources.SEARCH_URL, site, lambda q: f"https://x/?q={q}")
    sent = []

    async def _fake_dl(tid, force=False):
        sent.append(tid)
        return True

    async def _alive():
        return True
    monkeypatch.setattr(A, "download_anime_torrent", _fake_dl)
    monkeypatch.setattr(A, "qb_precheck", _alive)
    now = datetime.now()
    with clean_tables.get_session() as s:
        a = _a(title="尼古喵喵", display_name="尼古喵喵", total_episodes=12, pref_source="ANi")
        s.add(a); s.commit(); s.refresh(a)
        for e in range(1, 8):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{e:040x}", source="ANi", site="nyaa",
                               anime_title="尼古喵喵", season=1, episode=e, status="sent",
                               raw_title=f"[ANi] 尼古喵喵 - {e:02d}", created_at=now))
        s.commit()
        aid = a.id

    res = asyncio.run(A.backfill_source(aid))
    assert res["ingested"] == 0, "ANi 的第 7 集已经有了，锁定源没有新东西"
    assert res["others"] == [{"group": "NEST", "site": "mikan", "eps": [8, 9, 10, 11, 12],
                              "sample": res["others"][0]["sample"]}], res["others"]
    with clean_tables.get_session() as s:
        assert s.get(Anime, aid).confirmed, "什么都没入库，不该把番打回待确认"

    res = asyncio.run(A.backfill_source(aid, group="NEST", site="mikan"))
    assert (res["ingested"], res["downloaded"]) == (5, 5), res
    with clean_tables.get_session() as s:
        assert s.get(Anime, aid).confirmed, "挑定的组：直接补，不打回待确认"
        eps = sorted(t.episode for t in s.exec(A.select(AnimeTorrent).where(
            AnimeTorrent.anime_id == aid, AnimeTorrent.source == "NEST")))
    assert eps == [8.0, 9.0, 10.0, 11.0, 12.0], "只入库本番还没下过的集"
    assert len(sent) == 5


def test_download_locked_out_keeps_the_lock(clean_tables, cfg, monkeypatch):
    """『只补这几集』：不改锁，锁定源之后那几集用别的组各下一份。"""
    sent = []

    async def _fake_dl(tid, force=False):
        sent.append(tid)
        return True

    async def _alive():
        return True
    monkeypatch.setattr(A, "download_anime_torrent", _fake_dl)
    monkeypatch.setattr(A, "qb_precheck", _alive)
    t0 = datetime(2026, 7, 6)
    rows = [("喵萌奶茶屋", e, "sent", t0) for e in range(1, 6)]
    rows += [("ANi", e, "pending", t0) for e in range(4, 9)] + [("Nix-Raws", 8, "pending", t0)]
    with clean_tables.get_session() as s:
        aid = _lib(s, "喵萌奶茶屋", rows)
    plan = A.locked_out_candidates(aid)
    assert sorted(plan) == [6.0, 7.0, 8.0]
    assert asyncio.run(A.download_locked_out(aid)) == 3
    assert len(sent) == 3
    with clean_tables.get_session() as s:
        assert s.get(Anime, aid).pref_source == "喵萌奶茶屋"


# ── 单番判下齐（#17）────────────────────────────────────────────────

def test_single_anime_judgement_waits_for_the_first_backfill(clean_tables, cfg, monkeypatch):
    """首轮回填之前：单番判定只撤销不判命中（否则历史上早已下齐的番会被逐条推送、还提前落下回填闩）。"""
    cfg(ANIME_FINISH_ENABLED=True, NOTIFY_URL="")
    monkeypatch.setattr(A, "episode_coverage", lambda ids: {i: set(range(1, 13)) for i in ids})
    monkeypatch.setattr(A, "_cannot_have_aired_all", lambda a: False)
    with clean_tables.get_session() as s:
        a = _a(title="早就下齐的番", air_date="2025-01-05")
        s.add(a); s.commit(); s.refresh(a)
        aid = a.id
    assert asyncio.run(A.sweep_finished({aid})) == 0
    assert not A._finish_backfilled(), "单番判定不许替整库落回填闩"
    A._mark_finish_backfilled()
    assert asyncio.run(A.sweep_finished({aid})) == 1


# ── 设置改动留日志、保存不再自动关 qB（#9 #5）─────────────────────────

def test_set_many_reports_and_logs_changes_with_secrets_masked(testdb, caplog, monkeypatch):
    import logging

    import config
    monkeypatch.setitem(config._v, "QB_PASSWORD", "old-secret")
    monkeypatch.setitem(config._v, "ANIME_POLL_INTERVAL", 1200)
    try:
        with caplog.at_level(logging.INFO, logger="autorss"):
            changed = config.set_many({"QB_PASSWORD": "new-secret", "ANIME_POLL_INTERVAL": "1200",
                                       "ANIME_POLL_LAST": "2026-10-19T09:00:00"})
        assert set(changed) == {"QB_PASSWORD", "ANIME_POLL_LAST"}, "没变的不算"
        text = caplog.text
        assert "设置已修改" in text and "QB_PASSWORD: *** → ***" in text
        assert "secret" not in text, "密码不进日志"
        assert "ANIME_POLL_LAST" not in text, "运行时键不进日志"
    finally:
        config.set_many({"QB_PASSWORD": "", "ANIME_POLL_LAST": ""})


def test_saving_settings_never_turns_qb_off():
    """以前保存任何一项都会探测 qB、失败就把『发送到 qB』永久写成 false。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "pages" / "settings.py").read_text(encoding="utf-8")
    assert 'set_many({"QB_ENABLED": "false"})' not in src
    assert "_QB_CONN_KEYS" in src


# ── 日志页：警告/错误单独保留最近 100 条（用户 2026-10-19）──────────────────

def test_new_nicegui_slot_message_is_filtered():
    """NiceGUI 3 把断连噪声的文案改成 "The parent slot of Timer(id=307) has been deleted."，
    旧的精确字符串匹配不上 —— 生产日志里又冒出了这种 ERROR。"""
    import logging

    from core.logsetup import _SuppressDeletedSlot
    f = _SuppressDeletedSlot()

    def rec(msg, exc=None):
        r = logging.LogRecord("nicegui", logging.ERROR, "x", 1, msg, (), None)
        if exc:
            r.exc_info = (type(exc), exc, None)
        return r
    assert not f.filter(rec("The parent slot of Timer(id=307) has been deleted."))
    assert not f.filter(rec("按钮操作失败：%s", RuntimeError("The parent slot of Button(id=12) has been deleted.")))
    assert f.filter(rec("抓取失败 ANi: ConnectError: boom")), "真错误不能被吞"


def test_alert_rings_are_seeded_from_the_log_file(tmp_path, monkeypatch):
    from core import logsetup as L
    log = tmp_path / "autorss.log"
    (tmp_path / "autorss.log.1").write_text(
        "2026-10-18 01:00:00,000 WARNING 旧文件里的警告\n", encoding="utf-8")
    log.write_text(
        "2026-10-19 09:00:00,000 INFO 源 ANi：75 条，新增 0\n"
        "2026-10-19 09:01:00,000 ERROR 本轮异常: boom\n"
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1, in <module>\n'
        "RuntimeError: boom\n"
        "2026-10-19 09:02:00,000 ERROR The parent slot of Timer(id=307) has been deleted.\n"
        "2026-10-19 09:03:00,000 WARNING 抓取失败 Mikan(兜底): 403\n", encoding="utf-8")
    monkeypatch.setattr(L, "LOG_PATH", log)
    monkeypatch.setattr(L, "warnings_ring", L.RingHandler(100))
    monkeypatch.setattr(L, "errors_ring", L.RingHandler(100))
    L._seed_alert_rings()
    w = [x["line"].split(" ", 3)[3].splitlines()[0] for x in L.warnings_ring.snapshot()]
    assert w == ["旧文件里的警告", "本轮异常: boom", "抓取失败 Mikan(兜底): 403"], w
    e = L.errors_ring.snapshot()
    assert len(e) == 1 and "RuntimeError: boom" in e[0]["line"], "异常栈跟着它那一行走"


# ── r37 后续：自动补齐直接下 / 整机断网 / 缺集清单 / 日志降噪 ──────────────────

def test_auto_backfill_downloads_directly_for_a_tracked_show(clean_tables, cfg, monkeypatch):
    """『自动补齐』在追的番：补到就直接下、不打回待确认；没在追的番照旧转待确认。宽松的『补齐该源』照旧打回。"""
    import sources
    found = [_item("ANi", 8, 300, site="nyaa"), _item("ANi", -1, 301, site="nyaa")]   # 一集正片 + 一条特别篇

    class _Stub:
        def __init__(self, *a, **kw):
            pass

        async def fetch(self, content_cache=None):
            return list(found)
    for site in ("nyaa", "mikan"):
        monkeypatch.setitem(sources.SOURCES, site, _Stub)
        monkeypatch.setitem(sources.SEARCH_URL, site, lambda q: f"https://x/?q={q}")
    sent = []

    async def _fake_dl(tid, force=False):
        sent.append(tid)
        return True

    async def _alive():
        return True
    monkeypatch.setattr(A, "download_anime_torrent", _fake_dl)
    monkeypatch.setattr(A, "qb_precheck", _alive)
    now = datetime.now()

    def _show(title, confirmed):
        with clean_tables.get_session() as s:
            a = _a(title=title, display_name="尼古喵喵", confirmed=confirmed, pref_source="ANi")
            s.add(a); s.commit(); s.refresh(a)
            for e in range(1, 8):
                s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{a.id:08x}{e:032x}", source="ANi", site="nyaa",
                                   anime_title="尼古喵喵", season=1, episode=e, status="sent",
                                   raw_title=f"[ANi] 尼古喵喵 - {e:02d}", created_at=now))
            s.commit()
            return a.id

    tracked = _show("在追的", True)
    res = asyncio.run(A.backfill_source(tracked, name_filter=True))
    assert (res["ingested"], res.get("downloaded"), res["direct"]) == (2, 1, True), res
    assert len(sent) == 1, "特别篇不自动下"
    with clean_tables.get_session() as s:
        assert s.get(Anime, tracked).confirmed, "在追的番不打回待确认"
    with clean_tables.get_session() as s:
        for t in s.exec(A.select(AnimeTorrent).where(AnimeTorrent.anime_id == tracked,
                                                      AnimeTorrent.episode >= 8)):
            s.delete(t)
        s.commit()
    res = asyncio.run(A.backfill_source(tracked, name_filter=False))
    assert res["to_confirm"] and not res["direct"], "宽松的『补齐该源』照旧打回待确认"

    pending = _show("待确认的", False)
    res = asyncio.run(A.backfill_source(pending, name_filter=True))
    assert not res["direct"] and "downloaded" not in res


def test_network_down_is_one_message(fresh_health, cfg):
    """所有源都连续失败、原因全是 DNS/连接类 → 一句"多半断网"，而不是逐个源报失败。"""
    cfg(ALERT_SOURCE_FAIL_ROUNDS=3)
    names = {"ANi", "喵萌(简中)", "Mikan(兜底)"}
    health.forget_sources(names)
    for _ in range(3):
        for n in names:
            health.record_source(n, False, "ConnectError: [Errno -3] Temporary failure in name resolution")
    assert health.network_down()
    assert "断网" in health.sources_problem()
    health.record_source("ANi", False, "HTTPStatusError: 403 Forbidden")     # 有一个源的原因不是网络
    assert not health.network_down()
    assert health.sources_problem().startswith("源抓取连续失败")
    health.record_source("ANi", True)
    assert not health.network_down(), "有源还抓得到，就不是断网"


def test_gap_rows_list_every_show_with_gaps(clean_tables, cfg):
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    now = datetime.now()
    with clean_tables.get_session() as s:
        a = _a(title="缺集番", air_date="2025-01-05", pref_source="ANi")
        s.add(a); s.commit(); s.refresh(a)
        for ep in (1, 2, 3, 5, 6, 7):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{ep:040x}", raw_title=f"t{ep}", episode=ep,
                               status="sent", created_at=now))
        s.commit()
    rows = A.gap_rows()
    assert [r["name"] for r in rows] == ["缺集番"]
    assert rows[0]["raw"].startswith("缺第 4、8～12 集；锁定『ANi』"), rows[0]["raw"]
    assert A.ep_ranges([12, 1, 2, 3, 7]) == "1～3、7、12"


def test_qb_down_is_logged_once_not_every_round(monkeypatch, cfg, caplog):
    """断网 30 小时里『qB 连不上』每轮记一行 WARNING（213 次），把日志缓冲冲掉了。现在只在翻转时记。"""
    import logging

    from core import engine
    cfg(QB_ENABLED=True, NOTIFY_URL="")
    state = {"alive": False}

    async def _reach():
        return state["alive"]
    monkeypatch.setattr(engine.qb, "reachable", _reach)
    monkeypatch.setattr(A, "_qb_down_rounds", 0)
    with caplog.at_level(logging.INFO, logger="autorss"):
        for _ in range(5):
            asyncio.run(A.qb_precheck())
        state["alive"] = True
        asyncio.run(A.qb_precheck())
    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "qB 连不上" in r.getMessage()]
    assert len(warns) == 1
    assert any("qB 恢复" in r.getMessage() for r in caplog.records)


def test_second_nicegui_slot_variant_is_filtered():
    import logging

    from core.logsetup import _SuppressDeletedSlot
    r = logging.LogRecord("nicegui", logging.ERROR, "x", 1,
                          "The parent element this slot belongs to has been deleted.", (), None)
    assert not _SuppressDeletedSlot().filter(r)


def test_backfill_finds_a_romaji_only_group_by_episode_query(clean_tables, cfg, monkeypatch):
    """无自觉圣女：Nix-Raws 跳过了第 8 集，只有 Erai-raws（标题只写罗马音、带 CR 简繁字幕）有。
    两道坎：① nyaa 搜索只回最近 75 条，第 8 集被挤出第一页 —— 要按"名字 + 08"补搜；
    ② 罗马音标题与 bgm 的日文/中文名对不上 —— 要拿本番种子标题里的搜索名（含罗马音）来比。
    挑定 Erai-raws 补时，同一集好几个版本里先试 1080p。"""
    import sources
    from sources.base import ParsedItem
    romaji = "Mujikaku Seijo wa Kyou mo Muishiki ni Chikara wo Tare Nagasu"

    def erai(res, n):
        return ParsedItem(info_hash=f"{n:040x}", raw_title=f"[Erai-raws] {romaji} - 08 [{res} CR WEB-DL AVC AAC][MultiSub]",
                          anime_title=romaji, season=1, episode=8.0, quarter="26C", release_time=None,
                          download_url=f"https://x/{n}.torrent", source="Erai-raws", site="nyaa",
                          search_names=[romaji])
    queries = []

    class _Stub:
        def __init__(self, name, url, **kw):
            self.url = url

        async def fetch(self, content_cache=None):
            queries.append(self.url)
            return [erai("480p", 500), erai("1080p", 501)] if self.url.endswith("%2008") else []
    for site in ("nyaa", "mikan"):
        monkeypatch.setitem(sources.SOURCES, site, _Stub)
        monkeypatch.setitem(sources.SEARCH_URL, site, lambda q: "https://x/?q=" + q.replace(" ", "%20"))
    tried = []

    async def _fake_dl(tid, force=False):
        with clean_tables.get_session() as s:
            tried.append(s.get(AnimeTorrent, tid).raw_title)
        return True

    async def _alive():
        return True
    monkeypatch.setattr(A, "download_anime_torrent", _fake_dl)
    monkeypatch.setattr(A, "qb_precheck", _alive)
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    with clean_tables.get_session() as s:
        a = _a(title="无自觉圣女今天也无意识地释放力量", display_name="无自觉圣女今天也无意识地释放力量",
               jp_name="無自覚聖女は今日も無意識に力を垂れ流す", air_date="2026-06-30")
        s.add(a); s.commit(); s.refresh(a)
        for e in (1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{e:040x}", source="Nix-Raws", site="mikan", season=1,
                               anime_title="无自觉圣女今天也无意识地释放力量", episode=e, status="sent",
                               raw_title=f"[Nix-Raws] 无自觉圣女今天也无意识地释放力量 / Mujikaku Seijo wa Kyou mo "
                                         f"Muishiki ni Chikara wo Tare S01E{e:02d} [CR WEB-DL 1080p AVC AAC][简繁内封]",
                               created_at=datetime.now()))
        s.commit()
        aid = a.id
    res = asyncio.run(A.backfill_source(aid))
    assert any(q.endswith("%2008") for q in queries), "已知缺第 8 集，要按集号补搜"
    assert [(o["group"], o["eps"]) for o in res["others"]] == [("Erai-raws", [8])], res["others"]
    res = asyncio.run(A.backfill_source(aid, group="Erai-raws", site="nyaa"))
    assert res["downloaded"] == 1 and "1080p" in tried[0], tried
