"""两处无人值守流程的小缺口。

  · 断更巡检：字幕组在见过的最大集号上打了完结标记（[END] 等）的番是播完了，不报断更；
    标记打在更早的集上、或根本没有标记的照报。
  · 放行下载期间刷"采集在动"：单次取种最长 180 秒，积压几十集时整段没有别的活跃信号，
    看守会把它判成采集停摆、提示重启（重启会打断正在交付的那条）。四个交付循环每交付一条都要刷一次。
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from core import anime as A
from core import engine as ce
from core import health
from db.models import Anime, AnimeTorrent

OLD = datetime.now() - timedelta(hours=5)
_n = iter(range(1, 10 ** 6))


def _anime(s, **kw):
    base = dict(title="某番", display_name="某番", season=1, quarter="26C", confirmed=True, rejected=False,
                total_episodes=12, bangumi_id=1)
    base.update(kw)
    a = Anime(**base)
    s.add(a)
    s.commit()
    s.refresh(a)
    return a.id


def _row(s, aid, src, ep, status="pending", **kw):
    n = next(_n)
    base = dict(anime_id=aid, info_hash=f"{n:040x}", source=src, site="nyaa", season=1, episode=float(ep),
                raw_title=f"[{src}] 某番 - {int(ep):02d} [1080p]", status=status,
                download_url=f"https://x/{n}.torrent", created_at=OLD, release_time=OLD)
    base.update(kw)
    t = AnimeTorrent(**base)
    s.add(t)
    s.commit()
    s.refresh(t)
    return t.id


# ── 断更巡检：终集带完结标记 ─────────────────────────────────────

async def test_idle_skips_anime_whose_last_seen_episode_is_end_marked(clean_tables, cfg, monkeypatch):
    """三部番都安静了 30 天、都判不了"已下齐"（bgm 没给总集数）：
    终集（见过的最大集号）带 [END] 的不报；[END] 打在第 11 集、后面还见过第 12 集的照报；没有标记的照报。
    "终集完结"那部的第 12 集只见到没下到（别的组的 skipped 行）：比的是见过的最大集号，不是已下的。"""
    sent = []

    async def notify(kind, msg, **kw):
        sent.append(msg)
        return True
    monkeypatch.setattr(A, "notify_event", notify)
    cfg(ANIME_IDLE_DAYS=14)
    quiet = datetime.now() - timedelta(days=30)
    with clean_tables.get_session() as s:
        for name, end_ep in (("终集完结", 12), ("中途完结", 11), ("没有标记", None)):
            aid = _anime(s, title=name, display_name=name, total_episodes=0, air_date=None)
            for ep in range(1, 13):
                title = f"[G] {name} - {ep:02d} [1080p]" + (" [END]" if ep == end_ep else "")
                only_seen = name == "终集完结" and ep == 12
                _row(s, aid, "H" if only_seen else "G", ep, "skipped" if only_seen else "sent",
                     raw_title=title, created_at=quiet)
    assert await A.sweep_idle() == 2
    assert sent and "终集完结" not in sent[0]
    assert "中途完结" in sent[0] and "没有标记" in sent[0]


# ── 放行期间刷活跃时刻 ───────────────────────────────────────────

@pytest.fixture
def touches(monkeypatch):
    calls = []
    monkeypatch.setattr(health, "touch", lambda: calls.append(1))
    return calls


@pytest.fixture
def chain(cfg, monkeypatch):
    """真交付链：只把取种和加进 qB 两步换成记账。"""
    cfg(QB_ENABLED=True, QB_SYNC_STATUS=True, DOWN_PATH="/tmp/dl", ANIME_DOWN_PATH="",
        ANIME_FINISH_UNSUB=False, ANIME_DOWNLOAD_GRACE_MIN=0, ANIME_REPLACE_VERSIONS=True,
        ANIME_SINGLE_WORK_AUTO=True)

    async def fetch(url):
        return b"d1:xe"

    async def add(*a, **k):
        return True

    async def notify(kind, msg, **kw):
        return True
    monkeypatch.setattr(ce, "fetch_torrent_bytes", fetch)
    monkeypatch.setattr(ce, "add_to_qb", add)
    monkeypatch.setattr(A, "notify_event", notify)


def _fail_every_delivery(monkeypatch):
    """交付恒失败（取种 180 秒超时正是最慢的那种）：失败也是"在动"，照样要刷。"""
    async def deliver(tid, **kw):
        return False
    monkeypatch.setattr(A, "download_anime_torrent", deliver)


@pytest.mark.parametrize("ok", [True, False])
def test_flush_touches_once_per_delivery(clean_tables, chain, touches, monkeypatch, ok):
    if not ok:
        _fail_every_delivery(monkeypatch)
    with clean_tables.get_session() as s:
        aid = _anime(s)
        for ep in (1, 2, 3):
            _row(s, aid, "G", ep)
    assert asyncio.run(A.flush_ready_downloads(qb_alive=True)) == (3 if ok else 0)
    assert len(touches) >= 3


@pytest.mark.parametrize("ok", [True, False])
def test_version_delivery_touches(clean_tables, chain, touches, monkeypatch, ok):
    if not ok:
        _fail_every_delivery(monkeypatch)
    with clean_tables.get_session() as s:
        aid = _anime(s)
        _row(s, aid, "ANi", 4, "sent", qb_progress=1.0, raw_title="[ANi] 某番 - 04 [1080P][CHT][MP4]")
        _row(s, aid, "ANi", 4, "skipped", raw_title="[ANi] 某番 - 04 [1080P][CHT][V2][MP4]")
    assert asyncio.run(A._deliver_new_versions()) == (1 if ok else 0)
    assert len(touches) >= 1


@pytest.mark.parametrize("ok", [True, False])
def test_single_work_delivery_touches(clean_tables, chain, touches, monkeypatch, ok):
    if not ok:
        _fail_every_delivery(monkeypatch)
    with clean_tables.get_session() as s:
        aid = _anime(s, total_episodes=1, platform="WEB")
        _row(s, aid, "X", -2, raw_title="[X] 某番 [WebRip 1080p]")
    assert asyncio.run(A.deliver_single_works(qb_alive=True)) == (1 if ok else 0)
    assert len(touches) >= 1


@pytest.mark.parametrize("ok", [True, False])
@pytest.mark.parametrize("step", ["catchup", "fallback"])
def test_lock_fallback_touches_in_both_steps(cfg, monkeypatch, touches, step, ok):
    """锁定源兜底的 A 步（锁定组补发）与 B 步（别的组兜底）各交付一条，各刷一次（交付成败都刷）。
    计划与复核打桩，只看交付循环。"""
    cfg(QB_ENABLED=True)
    a_plan = {1: dict(name="某番", lock="L", kw="", eps={5: [101]}, fb={5: ["G"]})}
    b_plan = {1: dict(name="某番", lock="L", kw="", days=9, kw_missed=[], eps={6: [(202, "G")]})}
    monkeypatch.setattr(A, "_lock_catchup_plan", lambda: a_plan if step == "catchup" else {})
    monkeypatch.setattr(A, "lock_fallback_plan", lambda **kw: b_plan if step == "fallback" else {})
    monkeypatch.setattr(A, "_lock_fallback_ok", lambda *a, **kw: True)
    monkeypatch.setattr(A, "_mark_lock_fallback", lambda tid: None)

    async def deliver(tid, **kw):
        return ok

    async def notify(kind, msg, **kw):
        return True
    monkeypatch.setattr(A, "download_anime_torrent", deliver)
    monkeypatch.setattr(A, "notify_event", notify)
    assert asyncio.run(A.deliver_lock_fallback(qb_alive=True)) == (1 if ok else 0)
    assert len(touches) == 1
