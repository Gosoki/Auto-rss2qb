"""qB 交付前预检：三个入口共用，且它是 qb_down 告警的触发点。

抽成一个函数之后最容易出的错是"某个入口的语义悄悄变了"——
尤其是 QB_ENABLED=false 这一支：那是"不下载"，不是"故障"。
"""
import pytest

from core import anime as A, engine as E


@pytest.fixture
def reachable(monkeypatch):
    def _set(alive):
        async def probe():
            return alive
        monkeypatch.setattr(E.qb, "reachable", probe)
    return _set


@pytest.fixture(autouse=True)
def quiet_notify(monkeypatch):
    async def noop(*a, **kw):
        return True
    monkeypatch.setattr(A, "notify_state", noop)


async def test_disabled_qb_is_not_a_failure(cfg, monkeypatch):
    """QB_ENABLED=false 是"只采集不下载"，不是故障——不该探测、不该报警、不该拦下调用方。"""
    cfg(QB_ENABLED=False)
    called = []

    async def probe():
        called.append(1)
        return False
    monkeypatch.setattr(E.qb, "reachable", probe)
    assert await A.qb_precheck() is True
    assert called == [], "关掉 qB 时不该去探测它"


async def test_alive_and_dead(cfg, reachable):
    cfg(QB_ENABLED=True)
    reachable(True)
    assert await A.qb_precheck() is True
    reachable(False)
    assert await A.qb_precheck() is False


async def test_state_notification_is_edge_triggered(cfg, reachable, monkeypatch):
    """这条预检每轮都跑。电平触发的话，qB 关机一夜就是几十条一模一样的推送。"""
    cfg(QB_ENABLED=True)
    flips = []

    async def rec(kind, bad, bad_msg, ok_msg=""):
        flips.append(bad)
        return True
    monkeypatch.setattr(A, "notify_state", rec)
    reachable(False)
    for _ in range(3):
        await A.qb_precheck()
    assert flips == [True, True, True], "预检每轮都调 notify_state（边沿抑制在 state() 里做）"


async def test_all_three_entrypoints_gate_on_it(cfg, reachable, monkeypatch, clean_tables):
    """flush / 逐番补下 / 批量补下 三处都必须过这道闸——少一处，qB 掉线时那个入口会把
    整批种子挨个跑一遍网络（每条都去源站取种、再发给一个根本不在的 qB）。"""
    cfg(QB_ENABLED=True)
    reachable(False)

    async def boom(*a, **kw):
        raise AssertionError("qB 连不上时不该真去下载")
    monkeypatch.setattr(A, "download_anime_torrent", boom)
    assert await A.flush_ready_downloads() == 0
    assert await A.download_all_pending() == 0


@pytest.mark.parametrize("qb_alive,want_fetch", [(True, True), (False, False), (None, True)])
async def test_instant_download_respects_the_round_level_qb_probe(
        clean_tables, cfg, monkeypatch, qb_alive, want_fetch):
    """『最高优先级即时下载』要看本轮开头那一次 qB 探测的结果（E-22）。

    这条路默认开着，而种入的 ANi 组 priority=100 —— 于是几乎每条新种子都走它、一条都不走 flush。
    qB 没开机时，download_anime_torrent 会先真去源站把种子取回来（最长 180 秒）才发现连不上：
    一轮 7 条新条目 = 7 次无用 GET。
    而原样把 qb_precheck 搬进 process_item 也不对——qB 在线时每个新条目都要多打一次
    GET /app/version（1 → 8 次/轮），拿常态的开销换罕态的浪费，净收益为负。
    qb_alive=None（零散入口没探过）时按老路走，不额外拦。
    """
    from datetime import datetime

    from core import anime as A, engine
    from sources.base import ParsedItem

    cfg(QB_ENABLED=True, ANIME_TOP_PRIORITY_INSTANT=True, DOWN_PATH="/data")
    A.seed_source_groups()
    fetched = []

    async def _fetch(url):
        fetched.append(url)
        return b"d1:xe"
    monkeypatch.setattr(engine, "fetch_torrent_bytes", _fetch)
    monkeypatch.setattr(engine, "add_to_qb", lambda *a, **k: _ok(True))
    monkeypatch.setattr(A.enrich, "resolve", lambda *a, **k: _ok({"bangumi_id": 4242}))
    monkeypatch.setattr(A, "notify_event", lambda *a, **k: _ok(True))

    item = ParsedItem(info_hash="c" * 40, raw_title="[ANi] 某番 - 05 [1080P]",
                      anime_title="某番", season=1, episode=5, quarter="26C",
                      release_time=datetime.now(), download_url="http://x/5.torrent",
                      source="ANi", site="nyaa", policy="auto", priority=100)
    await A.process_item(item, known_hashes=set(), qb_alive=qb_alive)

    assert bool(fetched) is want_fetch, (
        f"qb_alive={qb_alive} 时取种 {len(fetched)} 次，期望 {'有' if want_fetch else '没有'}")


async def _ok(v):
    return v
