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
