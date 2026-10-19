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
