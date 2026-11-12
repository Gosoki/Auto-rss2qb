"""补记文件路径的列投影 + 启动末尾冻结 GC。

  · engine.fill_content_paths 挂在归档轮上：qB 里查不到的行（_path_miss）每一轮都被扫描查询重新取回再丢掉，
    这批行永不归档就永远在。扫描只许取 id / info_hash / save_path 三列，不取整行；补记结果与 miss 记账不变。
  · main._startup 最后 gc.collect(); gc.freeze()：启动后常驻的约 16 万个可回收对象不再被全代回收扫
    （每次约 24 ms，随机落在仪表盘 / 采集的数据函数里）。不 import main —— 那会在 NiceGUI 应用已启动后
    再挂中间件而炸（见 core.worker.log_task_exit 的 docstring）；改用 AST 查，并把 _startup 函数体单独编译、
    依赖全部打桩，走一遍三条启动路径。
"""
import ast
import asyncio
import types
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from core import engine as ce
from db.models import Anime, AnimeTorrent, Movie, MovieTorrent

_ROOT = Path(__file__).resolve().parent.parent


def _h(tag: str) -> str:
    return (tag * 40)[:40]


def _seed(db):
    """番剧侧五种形状 + 剧场版侧两种。返回 {标签: (模型, 行 id)}。"""
    out = {}
    with db.get_session() as s:
        a = Anime(title="番", display_name="番", quarter="26A", confirmed=True)
        m = Movie(title="剧场版", quarter="26A")
        s.add(a); s.add(m); s.commit(); s.refresh(a); s.refresh(m)
        for tag, kw in (("a", {}),                                         # 已下、没路径、qB 认识 → 补上
                        ("b", {}),                                         # 已下、没路径、qB 不认识 → 记 miss
                        ("c", {"status": "stalled"}),                      # 停滞也补
                        ("d", {"archived_at": datetime.now()}),            # 已归档：不问
                        ("e", {"content_path": "/old/e.mkv"}),             # 已有路径：不问、不覆盖
                        ("f", {"status": "pending"})):                     # 没交给 qB：不问
            t = AnimeTorrent(anime_id=a.id, info_hash=_h(tag), raw_title=f"[组] 番 - {tag}", episode=1,
                             save_path="/data/26A/番", created_at=datetime.now(), **{"status": "sent", **kw})
            s.add(t); s.commit()
            out[tag] = (AnimeTorrent, t.id)
        for tag in ("1", "2"):                                              # 1 认识、2 不认识
            t = MovieTorrent(movie_id=m.id, info_hash=_h(tag), raw_title=f"剧场版 {tag}", status="sent",
                             save_path="/data/2026/剧场版", created_at=datetime.now())
            s.add(t); s.commit()
            out[tag] = (MovieTorrent, t.id)
    return out


def _fake_qb(monkeypatch, known):
    asked = []

    async def info(hashes):
        asked.append(set(hashes))
        return {h: {"content_path": f"/qb/{h[0]}.mkv", "state": "stalledUP"} for h in hashes if h[0] in known}
    monkeypatch.setattr(ce.qb, "torrents_info", info)
    return asked


def test_fill_scan_selects_three_columns_not_whole_row(clean_tables, cfg, monkeypatch):
    """扫描"没有内容路径的已下行"的那条 SELECT 只取 id / info_hash / save_path，两张表都是。

    写回时按主键 s.get 取整行是另一条语句（只发生在 qB 回了路径的行上），不在此列。"""
    cfg(QB_ENABLED=True)
    monkeypatch.setattr(ce, "_path_miss", set())   # 用例结束自动还原，成败都不把 hash 留给后面的用例
    _seed(clean_tables)
    _fake_qb(monkeypatch, known="a1")
    stmts = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        stmts.append(statement)
    sa.event.listen(clean_tables.engine, "before_cursor_execute", spy)
    try:
        asyncio.run(ce.fill_content_paths())
    finally:
        sa.event.remove(clean_tables.engine, "before_cursor_execute", spy)

    scans = {}
    for st in stmts:
        flat = " ".join(st.split())
        if flat.startswith("SELECT") and "content_path IS NULL" in flat:
            cols, _, rest = flat[len("SELECT"):].partition(" FROM ")
            scans[rest.split()[0]] = {c.strip().split(".")[-1] for c in cols.split(",")}
    assert set(scans) == {"animetorrent", "movietorrent"}, f"守卫前提坏了：没抓到两张表的扫描语句 {stmts}"
    for table, cols in scans.items():
        assert cols == {"id", "info_hash", "save_path"}, f"{table} 的扫描取了整行：{sorted(cols)}"


def test_fill_behavior_unchanged(clean_tables, cfg, monkeypatch):
    """补记结果正确：认识的补上、已有的不覆盖、已归档 / 没交给 qB 的不问；
    qB 查不到的进 _path_miss，下一轮不再问 qB。"""
    cfg(QB_ENABLED=True)
    monkeypatch.setattr(ce, "_path_miss", set())
    rows = _seed(clean_tables)
    asked = _fake_qb(monkeypatch, known="ac1")

    assert asyncio.run(ce.fill_content_paths()) == 3
    assert asked == [{_h("a"), _h("b"), _h("c")}, {_h("1"), _h("2")}], asked
    with clean_tables.get_session() as s:
        got = {k: s.get(model, i).content_path for k, (model, i) in rows.items()}
    assert got == {"a": "/qb/a.mkv", "b": None, "c": "/qb/c.mkv", "d": None,
                   "e": "/old/e.mkv", "f": None, "1": "/qb/1.mkv", "2": None}, got
    assert ce._path_miss == {_h("b"), _h("2")}, ce._path_miss

    asked.clear()
    assert asyncio.run(ce.fill_content_paths()) == 0
    assert asked == [], f"qB 里查不到的行下一轮又去问了：{asked}"


def _startup_fn():
    tree = ast.parse((_ROOT / "main.py").read_text(encoding="utf-8"))
    fn = next((n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_startup"), None)
    assert fn is not None, "main.py 里找不到 async def _startup（守卫前提坏了）"
    return tree, fn


def _is_call(node, owner: str, attr: str) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attr
            and isinstance(node.func.value, ast.Name) and node.func.value.id == owner)


def test_startup_freezes_gc_after_all_background_tasks():
    """_startup 的最后两条语句恰是 gc.collect(); gc.freeze()，排在所有 create_task / add_done_callback 之后；
    函数里没有 return、没有 await。

    放在条件分支里或中途 return 就只在某条启动路径上生效；collect 不紧挨 freeze，中间留下的垃圾会被一起冻住；
    freeze 之后再建对象就冻不进去。没有 await 是"冻结时还没有页面客户端"的前提：NiceGUI 把 async 的
    on_startup 处理器排成任务、不等它跑完就开始监听，体内无 await 时它一步跑完、端口还没开。"""
    tree, fn = _startup_fn()
    assert any(isinstance(n, ast.Import) and any(a.name == "gc" and a.asname is None for a in n.names)
               for n in tree.body), "main.py 顶层没有 import gc"
    tail = [st.value if isinstance(st, ast.Expr) else None for st in fn.body[-2:]]
    assert len(tail) == 2 and _is_call(tail[0], "gc", "collect") and _is_call(tail[1], "gc", "freeze"), \
        "_startup 的最后两条语句必须恰是 gc.collect(); gc.freeze()（collect 紧挨 freeze，freeze 之后不再有动作）"
    freeze = [n for n in ast.walk(fn) if _is_call(n, "gc", "freeze")]
    assert len(freeze) == 1, "_startup 里不止一处 gc.freeze()"
    later = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr in ("create_task", "add_done_callback")]
    assert later, "_startup 里找不到 create_task（守卫前提坏了）"
    assert freeze[0].lineno > max(later), f"gc.freeze() 在第 {freeze[0].lineno} 行，早于后台协程的建立（{later}）"
    assert not [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Return)], "_startup 里出现了 return：有的启动路径走不到冻结"
    assert not [n.lineno for n in ast.walk(fn) if isinstance(n, (ast.Await, ast.AsyncFor, ast.AsyncWith))], \
        "_startup 里出现了 await：冻结那一刻服务器可能已在监听，重连标签页的页面对象会被一起冻住"


_RUNNERS = ("run_db_watch", "run_worker", "run_qb_sync", "run_movie_scan", "run_reenrich_retry",
            "run_backup", "run_sweep", "run_ipfilter")


def _run_startup(*, init_fails=False, business_fails=False):
    """把 _startup 函数体（去掉装饰器）单独编译、依赖全部打桩跑一遍，返回事件序列与命名空间。"""
    _, fn = _startup_fn()
    fn.decorator_list = []
    events, down = [], {"v": False}

    def init_db():
        if init_fails:
            raise RuntimeError("建表失败")

    def init_business_state():
        events.append("init_business_state")
        if business_fails:
            raise RuntimeError("种源组失败")

    async def _noop():
        await asyncio.sleep(0)

    class _Asyncio:
        @staticmethod
        def create_task(coro, name=None):
            events.append("create_task")
            return asyncio.get_running_loop().create_task(coro, name=name)

    class _Log:
        def __getattr__(self, _):
            return lambda *a, **k: None

    ns = {
        "init_db": init_db,
        "config": types.SimpleNamespace(load_from_db=lambda: None),
        "apply_configured_backend": lambda: "sqlite",
        "log": _Log(),
        "db": types.SimpleNamespace(mark_data_fatal=lambda msg: down.__setitem__("v", True),
                                    is_data_down=lambda: down["v"]),
        "worker_mod": types.SimpleNamespace(_stale_sweep_pending=False),
        "init_business_state": init_business_state,
        "background_tasks": [],
        "asyncio": _Asyncio,
        "log_task_exit": lambda t: None,
        "gc": types.SimpleNamespace(collect=lambda: events.append("collect"),
                                    freeze=lambda: events.append("freeze"),
                                    unfreeze=lambda: events.append("unfreeze")),
        **{r: _noop for r in _RUNNERS},
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), ns)

    async def go():
        await ns["_startup"]()
        for t in ns["background_tasks"]:
            await t
    asyncio.run(go())
    return events, ns


@pytest.mark.parametrize("kw", [{}, {"init_fails": True}, {"business_fails": True}],
                         ids=["正常启动", "启动初始化失败", "业务初始化失败"])
def test_every_startup_path_reaches_freeze_last(kw):
    """三条启动路径（正常 / 启动初始化失败走 fatal 停摆 / 业务初始化失败）都建齐后台协程，并以 collect → freeze 收尾。"""
    events, ns = _run_startup(**kw)
    assert events.count("create_task") == len(_RUNNERS), events
    assert events[-2:] == ["collect", "freeze"], f"冻结不是这条启动路径的最后一步：{events}"
    assert events.count("freeze") == 1 and "unfreeze" not in events, events
    if kw.get("init_fails"):
        assert "init_business_state" not in events and ns["worker_mod"]._stale_sweep_pending is True
