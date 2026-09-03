"""(E-55，2026-09-02 拍板选 A) 切库成功后把其他标签页整个刷掉。

页面渲染时把业务库的整数主键捕进了闭包；切库之后那些主键指向另一个库里毫不相干的行
（transfer 保留主键）。在途闸盖不住这一类（那里的 await 是用户的思考时间），所以刷新。
"""
import ast
from pathlib import Path


def test_reload_other_tabs_reloads_every_connected_client_except_the_caller(monkeypatch):
    from nicegui import Client, context, ui

    from pages import layout as L

    reloaded = []

    class _Fake:
        def __init__(self, name, connected=True):
            self.name, self.has_socket_connection = name, connected

        def __enter__(self):
            reloaded.append(("enter", self.name))
            return self

        def __exit__(self, *a):
            return False

    me, other, gone = _Fake("me"), _Fake("other"), _Fake("gone", connected=False)
    monkeypatch.setattr(Client, "instances", {"a": me, "b": other, "c": gone})
    monkeypatch.setattr(type(context), "client", property(lambda self: me))
    calls = []
    monkeypatch.setattr(ui.navigate, "reload", lambda: calls.append(reloaded[-1][1]))

    assert L.reload_other_tabs() == 1
    assert calls == ["other"], calls        # 自己不刷、断开的不碰


def test_the_switch_handler_reloads_other_tabs_only_after_success():
    """(AST) `_switch_backend` 里对 reload_other_tabs 的调用，必须在 db.maintenance 那段之后、
    且不在 except 分支里 —— 切换失败（留在原库）时别把别人的页面刷掉。"""
    src = (Path(__file__).resolve().parent.parent / "pages/settings.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_switch_backend")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", "")) == "reload_other_tabs"]
    assert len(calls) == 1, "切库处理器里要恰好调一次 reload_other_tabs"
    # 它是经 run.io_bound(db.switch_data_engine, url) 丢进线程的 —— 是被引用不是被调，按 Attribute 找
    switch = [n for n in ast.walk(fn) if isinstance(n, ast.Attribute) and n.attr == "switch_data_engine"]
    assert len(switch) == 1, "找不到 switch_data_engine 的引用，守卫前提坏了"
    # (R34 对抗审计) 上一版只查"在 maintenance 之后、不在 except 里"：把 reload 放进维护块里、
    # switch 之前，或放进 finally，都绿。现在要求：在 switch 之后，且**整个**包住 switch 的那个 Try
    # 语句的子树里都没有它（body / handlers / finalbody 一概不行）—— 只能在 try 语句整体之后。
    assert calls[0].lineno > switch[0].lineno, "刷新要在真正切换之后"
    wrapping = [t for t in ast.walk(fn) if isinstance(t, ast.Try)
                and any(n is switch[0] for n in ast.walk(t))]
    assert wrapping, "switch_data_engine 不在 try 里？处理器形状变了，守卫要跟着改"
    for t in wrapping:
        assert not any(n is calls[0] for n in ast.walk(t)), \
            "reload_other_tabs 落在包住 switch 的 try 语句里（body/except/finally 都算）：切换失败也会刷别人的页面"
