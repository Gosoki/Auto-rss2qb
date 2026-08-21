"""长操作按钮的统一行为：loading + 防重入 + 异常兜底。

十二个同样耗时的按钮此前一件都没做。最长的是绑定 bgm / 重新识别那几个——
识别的时间预算是 120 秒（enrich._RESOLVE_BUDGET），期间按钮毫无反应，
用户会连点，于是并发跑好几轮、重复弹搬迁确认框。
异常兜底同理：NiceGUI 的 on_click 里逃出去的异常只进服务端日志，用户看到的是"点了没反应"。
"""
import asyncio

import pytest

from pages import layout as L


class _Btn:
    """记录 props 调用的假按钮。"""

    def __init__(self):
        self.state = set()

    def props(self, add=None, remove=None):
        if add:
            self.state.add(add)
        if remove:
            self.state.discard(remove)
        return self


@pytest.fixture
def toasts(monkeypatch):
    got = []
    monkeypatch.setattr(L.ui, "notify", lambda msg, **kw: got.append((msg, kw.get("type"))))
    L._BUSY.clear()
    return got


async def test_second_click_is_ignored_while_running(toasts):
    """连点只跑一轮——这是 120 秒死键最直接的伤害。"""
    started = asyncio.Event()
    release = asyncio.Event()
    runs = []

    async def _slow():
        runs.append(1)
        started.set()
        await release.wait()

    btn = _Btn()
    t1 = asyncio.create_task(L.busy_action(btn, "k", _slow))
    await started.wait()
    assert "loading" in btn.state, "跑着的时候按钮没进 loading 态"

    await L.busy_action(btn, "k", _slow)          # 第二次点击
    assert len(runs) == 1, "连点跑了两轮"
    assert any("还没跑完" in m for m, _ in toasts), "第二次点击没有任何反馈"

    release.set()
    await t1
    assert "loading" not in btn.state, "跑完没有摘掉 loading"


async def test_exception_is_reported_not_swallowed(toasts):
    """处理器抛异常时用户要看得见——否则就是"点了没反应"。"""
    async def _boom():
        raise RuntimeError("炸了")

    btn = _Btn()
    assert await L.busy_action(btn, "k2", _boom, fail="识别失败") is None
    assert any(t == "negative" and "识别失败" in m and "RuntimeError" in m for m, t in toasts)
    assert "loading" not in btn.state, "异常之后按钮卡在 loading 上"
    assert not L._BUSY.get("k2"), "异常之后去重键没释放，按钮从此再也点不动"


async def test_key_is_released_so_the_button_works_again(toasts):
    """跑完之后同一个键必须能再跑——别把按钮永久锁死。"""
    runs = []

    async def _fast():
        runs.append(1)

    for _ in range(3):
        await L.busy_action(None, "k3", _fast)
    assert len(runs) == 3


async def test_different_keys_do_not_block_each_other(toasts):
    """不同按钮之间不该互相挡——去重是按 key 的。"""
    release = asyncio.Event()

    async def _slow():
        await release.wait()

    t = asyncio.create_task(L.busy_action(None, "a", _slow))
    await asyncio.sleep(0)
    ran = []
    await L.busy_action(None, "b", lambda: _mark(ran))
    assert ran == [1], "另一个按钮被无关的 key 挡住了"
    release.set()
    await t


async def _mark(box):
    box.append(1)
