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


# ---------------- 写配置的入口都要过 loaded_from_db 那道闸（R19） ----------------

def test_every_config_writer_checks_loaded_from_db():
    """(R19) `loaded_from_db` 这道闸原来只写在 _save 里，而同一页还有第二个写配置的入口。

    闸的理由（_save 里那段注释）是：配置没从库里读出来时表单上全是硬编码默认值，
    一按就把库里已有配置整体改写成默认，全程零报错还弹一句绿色的『已保存』。
    而『应用开始使用日过滤』做的是一模一样的事 —— 它写 ANIME_START_DATE，
    值取自一个用 config.ANIME_START_DATE 渲染的框（读不出来时就是默认的空串）。
    于是页面顶部正红着那条 banner、点保存被拦，点这个按钮却能把库里真实的日期覆盖成空，
    紧接着把全部超期忽略的番放回待确认，最后弹一条【绿色】的成功提示。

    这是本项目第②种缺陷形状：约束的作用域比它保护的东西小。
    用 AST 查：pages/ 里每个含 `config.set_many(` 的函数，函数体里必须也出现
    `_require_config_loaded()` 或 `config.loaded_from_db`。
    """
    import ast
    import pathlib

    # 写的值不来自表单、而是代码里的字面量时，没有"被默认值覆盖"这回事；
    # 而且那恰恰是库出问题时的自救出口，拦掉反而把人关在门外。逐条写清理由。
    _EXEMPT = {
        "_switch_backend": "写的是字面量 'mysql'/'sqlite'，且它正是库出问题时的自救出口",
    }

    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted((root / "pages").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf8"))
        fns = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in fns:
            # 【只看最内层的那个函数】外层容器（页面函数、面板函数）把内层的调用也 dump 进来，
            # 按它判会把一堆无关的壳函数报成违规。
            inner = [m for m in fns if m is not node
                     and any(m is c for c in ast.walk(node))]
            own = ast.dump(node)
            for m in inner:
                own = own.replace(ast.dump(m), "")
            if "attr='set_many'" not in own and 'attr="set_many"' not in own:
                continue
            if node.name in _EXEMPT:
                continue
            if "require_config_loaded" in own or "loaded_from_db" in own:
                continue
            offenders.append(f"{path.name}:{node.lineno} {node.name}()")
    assert not offenders, (
        "这些处理器会把表单值写回配置，却没过 loaded_from_db 那道闸——"
        "库读不出来时它们会把用户原有的设置整体覆盖成默认值：\n  " + "\n  ".join(offenders)
        + "\n（写的是字面量、不来自表单的话，登记进本用例的 _EXEMPT 并写清理由）")


def test_the_config_gate_exemptions_are_not_stale():
    """反向守卫：豁免名单里的函数必须还存在，否则那条豁免是在放空枪。"""
    import pathlib
    src = "".join(p.read_text(encoding="utf8")
                  for p in (pathlib.Path(__file__).resolve().parent.parent / "pages").glob("*.py"))
    for name in ("_switch_backend",):
        assert f"def {name}(" in src, f"豁免名单里的 {name} 已经不存在了，把它从 _EXEMPT 里删掉"


def test_quarter_fmt_ui_box_renders_the_raw_value():
    """(R19) 设置页的『季度显示』框必须渲染【原始值】，否则"留空＝跟随"这个状态没法表达。

    config.QUARTER_FMT_UI 走 __getattr__，返回 `_v["QUARTER_FMT_UI"] or _v["QUARTER_FMT"]`——
    读派生、写原始，两端不对称，一次保存就把"跟随"塌缩成"钉死在当时那个字面量"。
    （真库上这件事【已经发生过】：raw 值现在是 '{yy}{q}' 而不是空串。）
    """
    import pathlib
    import re
    src = pathlib.Path(__file__).resolve().parent.parent.joinpath("pages/settings.py").read_text(
        encoding="utf8")
    i = src.index('_quarter_setting(f, "QUARTER_FMT_UI"')
    seg = src[i:i + 900]
    code = "\n".join(l.split("#", 1)[0] for l in seg.splitlines() if not l.strip().startswith("#"))
    assert re.search(r'config\.raw\(\s*"QUARTER_FMT_UI"\s*\)', code), \
        "『季度显示』框又渲染派生值了 —— 留空状态会被下一次保存钉死"


# ---------------- (R22) 页面上的周期性定时器都要有停摆闸 ----------------

def test_every_periodic_timer_skips_while_the_db_is_down():
    """`ui.timer(≥10s, cb)` 的回调都必须先判 `db.is_data_down()`。

    剧场版页的 `_refresh_live` 早有这道闸，注释还写着"番剧页的同名函数也是这个口径
    （见 pages/anime.py 的 refresh_timer）"—— **而番剧页根本没有**。
    同一句话只在一处兑现，第①号形状；而番剧页是默认首页。

    两个后果：① MySQL 掉线/被 DROP 时首页每 30 秒撞一次库，而建连接是同步调用、
    上界 `MYSQL_CONNECT_TIMEOUT=5` 秒 —— 事件循环每 30 秒被冻 5 秒，每开一个标签页翻一倍；
    ② R21 之后维护期 `is_data_down()` 也为真，这些刷新会在 refresh 里抛 `DatabaseBusy`。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted((root / "pages").glob("*.py")):
        src = path.read_text(encoding="utf8")
        tree = ast.parse(src)
        funcs = {n.name: n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "timer" and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "ui"):
                continue
            iv = n.args[0] if n.args else None
            if not (isinstance(iv, ast.Constant) and isinstance(iv.value, (int, float))
                    and iv.value >= 10):
                continue        # 只管周期性的长间隔定时器；一次性/短间隔的另有用途
            cb = n.args[1] if len(n.args) > 1 else None
            name = cb.id if isinstance(cb, ast.Name) else None
            fn = funcs.get(name)
            if fn is None:
                offenders.append(f"{path.name}:{n.lineno} 回调不是本模块里的具名函数，看不了")
                continue
            if "is_data_down" not in ast.dump(fn):
                offenders.append(f"{path.name}:{n.lineno} {name}() 没有停摆闸")
    assert not offenders, ("这些周期性定时器会在库停摆/维护时照撞不误：\n  "
                           + "\n  ".join(offenders))


def test_every_bgm_bind_entry_wraps_the_confirm_gate_too():
    """(R27) 四个绑定入口的 `require_bind_confirm` 必须**在 busy_action 里面**。

    它第一句就是 `enrich.fetch_by_id`（`_RESOLVE_BUDGET` = 120 秒），后面 `bind_*_bgm`
    还有第二次同样预算的往返。把回显闸留在外面，等于这道防抖只覆盖了后一半：
    bgm 慢或不通时按钮不 loading、也没有去重键，用户连点就会叠出两个
    「绑定到《X》？」确认框，两条协程各自跑一遍 bind —— 而末尾的身份守卫
    `_merge_anime` / `_merge_movie` 最后一步是 `s.delete(loser)`，**没有撤销入口**。

    剧场版侧三个入口（`_bind` / `_bind_from_input` / `_refail`）原来**整条都没有**
    busy_action，最长约 240 秒零反馈。

    判据按 AST：每一处 `require_bind_confirm(...)` 调用，都必须落在某个
    被 `busy_action(...)` 当参数传出去的内层函数体内。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    offenders, seen = [], 0
    for rel in ("pages/anime.py", "pages/anime_detail.py", "pages/movies.py"):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        # 收集"被 busy_action 当参数传出去的函数名"
        wrapped = set()
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call)
                    and getattr(n.func, "id", getattr(n.func, "attr", "")) == "busy_action"):
                for arg in n.args:
                    if isinstance(arg, ast.Name):
                        wrapped.add(arg.id)
        # 每个 require_bind_confirm 调用，找它最内层的 def
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            inner = {d.name for d in ast.walk(fn)
                     if isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef)) and d is not fn}
            for c in ast.walk(fn):
                if (isinstance(c, ast.Call)
                        and getattr(c.func, "id", "") == "require_bind_confirm"):
                    # 只在【最内层】那个 def 上判（外层函数会把它一起 walk 到）
                    if any(isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef))
                           and d is not fn and c in list(ast.walk(d)) for d in ast.walk(fn)):
                        continue
                    seen += 1
                    if fn.name not in wrapped:
                        offenders.append(f"{rel}:{c.lineno} 在 {fn.name}() 里，"
                                         "而它没有被 busy_action 包住")
                    del inner
    assert seen >= 4, f"只找到 {seen} 处 require_bind_confirm 调用（应有 4 处）—— 守卫的前提坏了"
    assert not offenders, ("这些绑定入口的回显闸不在防抖里：\n  " + "\n  ".join(offenders))


def test_switches_behind_the_config_gate_roll_themselves_back():
    """(R27) 过 `require_config_loaded()` 那道闸的**开关**，被拦下时必须把自己拨回去。

    全站过这道闸的另外三个处理器（设置页 `_save` / `_apply_filter`、movies 的
    `_save_scan`）都是**按钮**，没有可见状态需要回滚。只有 /parse 的
    『多括号回退捕获』是 `ui.switch`：`on_value_change` 触发时值**已经**变成用户
    刚拨到的那个了，早退等于界面显示"开"、`config.ANIME_MULTIBRACKET_PARSE` 一点没变、
    而下面的判定结果仍按"关"算 —— 用户会以为解析逻辑坏了。

    判据：pages/ 里凡是被 `.on_value_change(...)` 接上的处理器，
    只要它里面出现 `require_config_loaded`，就必须也出现 `set_value`（把控件拨回真值）。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    offenders, checked = [], 0
    for path in sorted((root / "pages").glob("*.py")):
        src = path.read_text(encoding="utf8")
        tree = ast.parse(src)
        # 被 on_value_change 接上的处理器名
        hooked = {a.id for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "on_value_change"
                  for a in n.args if isinstance(a, ast.Name)}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in hooked:
                continue
            body = ast.dump(node)
            if "require_config_loaded" not in body:
                continue
            checked += 1
            if "set_value" not in body:
                offenders.append(f"{path.name}:{node.lineno} {node.name}()")
    assert checked >= 1, "一个『开关 + 配置闸』的处理器都没找到 —— 这条守卫的前提坏了"
    assert not offenders, (
        "这些开关被配置闸拦下后没把自己拨回去，界面与实际配置会相反：\n  "
        + "\n  ".join(offenders))
