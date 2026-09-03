"""测试套件。

只测【纯函数与内存内逻辑】：不打网络、不连 qB、不依赖真实数据库。
目的是给重构上一道回归网——本项目历史上出过的 bug 有相当比例是"改了一处判据，
另一处手抄的同款判据没跟上"，而那类回归只要有表驱动用例就能当场兜住。
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 【必须在 import config 之前】config.DB_PATH 是模块级、启动时从环境变量读一次。
# 指到临时文件，免得用例把开发用的 data/autorss.db 写脏（init_db 会真的建表、跑迁移）。
_TMP_ROOT = os.path.realpath(tempfile.gettempdir())   # 钉死在 import 时，见 _assert_throwaway
_TMP_DB = Path(tempfile.mkdtemp(prefix="autorss-test-")) / "test.db"
os.environ["DB_PATH"] = str(_TMP_DB)

import config  # noqa: E402


@pytest.fixture
def cfg(monkeypatch):
    """临时改配置值。config 的读取走模块级 __getattr__ → _v 字典，直接改字典即可，
    不写数据库、不影响别的用例（monkeypatch 会逐键还原）。"""
    def _set(**kw):
        for k, v in kw.items():
            monkeypatch.setitem(config._v, k, v)
    return _set


@pytest.fixture(scope="session")
def testdb():
    """建一个空的临时库并跑完迁移。返回 db 模块。

    只给"必须有库才能测"的用例用（如 sync 状态机）；纯函数用例不要依赖它。
    """
    import db
    # 【与生产同一条启动序列】main.py 是 init_db() → load_from_db() → apply_configured_backend()，
    # 而业务表的迁移由最后那一步负责（R21 起）。少调一步，用例跑的就是另一套建库路径。
    db.init_db()
    config.load_from_db()
    db.apply_configured_backend()
    return db


# 落在 meta 库里的【内部标记】（不是用户配置）。它们记录的是"这件一次性的事做过了"，
# 跨重启有效——正因为如此，用例之间也必须清掉，否则前一个用例做过的事会让后一个用例
# 走上另一条分支（实测踩过：完结回填标记）。
# ⚠️ 这里的字符串必须与生产代码里那个键【逐字相同】——写错了不会报错，只是白清一个不存在的键。
# 已经踩过两次：`_backfill_legacy_progress_done` 是编的，真实键是 core/engine.py 的
# `_QB_PROGRESS_BACKFILLED`；`_idle_backfilled` 当初压根忘了加。
# 下面那条用例会去生产代码里核对，别再手抄漏了。
_INTERNAL_MARKERS = ("_FINISH_BACKFILL_DONE", "_KNOWN_NOTIFY_EVENTS",
                     "_QB_PROGRESS_BACKFILLED", "_idle_backfilled")


def impl_of(tree, fn_name: str):
    """按公开名找到【真正的函数体】——有 `_<name>_inner` 就返回它，没有才返回同名函数。

    (R33) 八个页面入口改成了 `wrapper → _<name>_inner` 两层（外层只做 `engine.in_flight`
    登记）。所有按函数名取 AST 断言"函数体里必须有 / 不许有 X"的守卫都要经这里：
    直接按公开名找到的是空壳 ——【正向】守卫（不许有 X）会**静默变成真空**，
    【反向】守卫（必须有 X）会红在"找不到"而不是红在缺陷上。
    第一次就撞上了两条（test_bind_preview / test_wrong_season_match），所以收成一个口。
    找不到返回 None，让调用方自己 assert 出带路径的提示。
    """
    import ast
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return fns.get(f"_{fn_name}_inner") or fns.get(fn_name)


def _assert_throwaway(engine, what: str) -> None:
    """删数据之前，先确认删的是【一次性的库】。不是就当场炸掉。

    【为什么必须有这一道】clean_tables 用的是 `testdb.get_session()`，而它读的是
    **调用时刻的 `db.engine`**——不是本文件顶上那个 DB_PATH。任何代码只要在用例里
    （或在一个 import 了 db 的探针脚本里）把 `db.engine` 重新指向别处，这个夹具就会
    照着新目标 DELETE 六张业务表，而且一声不吭。

    这不是假想：2026-08-31 真的发生过一次——一个只读审计的 agent 为了复现问题写了个探针，
    把 db.engine 指到了 data/autorss.db，然后用了 clean_tables。开发库里 99 部番、1621 条种子
    被清空，只剩下它自己插进去的夹具行（`番0`）。数据是从备份恢复的，但那次的代价是运气好：
    恰好 90 分钟前有一份完整备份。

    判据取"路径必须在系统临时目录下"而不是"路径不等于 data/autorss.db"：
    后者是黑名单，换个库名就绕过去了；前者是白名单，凡不是一次性的一律拦下。
    """
    url = engine.url
    if url.get_backend_name() != "sqlite":
        raise RuntimeError(f"拒绝在非 SQLite 上跑 {what}：{url.render_as_string(hide_password=True)}")
    path = url.database
    if not path:
        return                      # :memory: —— 天然一次性
    # 【用会话启动时钉住的临时根，不用 gettempdir()】gettempdir() 每次都读 tempfile.tempdir，
    # 而那是个可写的模块级变量：用例里 `tempfile.tempdir = "/"` 之后判据就恒真、闸形同虚设
    # （实测可绕过）。_TMP_ROOT 在 import 本文件时算一次，之后谁也改不动它。
    real = os.path.realpath(path)
    if not real.startswith(_TMP_ROOT + os.sep):
        raise RuntimeError(
            f"拒绝在【非临时库】上跑 {what}：{real}\n"
            f"测试夹具只允许操作系统临时目录下的一次性库（本次会话是 {_TMP_DB}）。\n"
            f"如果你是在写探针脚本：不要把 db.engine 指向开发库再用 clean_tables —— "
            f"它会 DELETE 掉全部六张业务表。")


@pytest.fixture(autouse=True)
def _restore_config_after_each_test():
    """每条用例跑完把 `config._v` 与 meta 库的 `setting` 表还原。(R24)

    【为什么必须是 autouse 的通用夹具，不是逐条用例自己 finally】
    `config.set_many()` 会**先写 meta 库的 setting 行、再 `_v.update(...)`**，
    而 `_v` 是模块级、整个 pytest session 只有一份，`clean_tables` 只清 4 个内部标记键。
    实测：`tests/test_notify_events.py` 有两条用例直接调它且没有还原，
    于是 `config.NOTIFY_EVENTS` 从默认的 9 个事件变成 `['delivered']` 并**保持到 session 结束** ——
    此后任何不带 `cfg` 夹具的用例里 `notify_enabled('finished'/'idle'/'stalled'/…)` 恒为 False。

    危险的不是"配置不对"，是它让一整类用例**变成空的**：
    "断言没发通知"恒成立、`sweep_finished` 里 `may_mark = ok or not notify_enabled(...)`
    也恒走那一支 —— 而那正是 R20 修过的那条 E-32 冷却回归的判据。
    这两条用例又确实需要真写库（它们测的就是"存进去再读回来"这条路），所以不能改成 monkeypatch。

    还原顺序要紧：先还内存再还库。反过来的话，中间那一刻 `_v` 还是脏的，
    而 `load_from_db` 之类的收尾逻辑会把脏值再写回去。
    """
    import config as _C

    snap_v = dict(_C._v)
    snap_rows = None
    try:
        import db as _db
        from db.models import Setting
        with _db.get_meta_session() as s:
            snap_rows = {r.key: r.value for r in s.exec(__import__("sqlmodel").select(Setting))}
    except Exception:
        pass                        # 还没建库的纯函数用例：没什么可还原的
    yield
    _C._v.clear()
    _C._v.update(snap_v)
    if snap_rows is None:
        return
    try:
        import db as _db
        from sqlmodel import select as _sel
        from db.models import Setting
        with _db.get_meta_session() as s:
            now = {r.key: r for r in s.exec(_sel(Setting))}
            changed = False
            for k, v in snap_rows.items():
                row = now.get(k)
                if row is None:
                    s.add(Setting(key=k, value=v))
                    changed = True
                elif row.value != v:
                    row.value = v
                    s.add(row)
                    changed = True
            for k, row in now.items():           # 用例新加的键：删掉
                if k not in snap_rows:
                    s.delete(row)
                    changed = True
            if changed:
                s.commit()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _guard_every_session(monkeypatch):
    """把"测试不许碰非一次性库"这条约束装到【所有会话入口】上，而不只是 clean_tables。

    【为什么必须扩到这里】上一版闸只挂在 clean_tables，而那条约束的作用域大得多：
      · testdb 夹具本身会 db.init_db()，在【调用时刻的 db.engine】上跑 alembic upgrade
        （SQLite 侧 batch_alter_table 是"建影子表→拷数据→改名"，中途出错就是真丢数据）；
      · **用例里调任何生产函数**都用 db.get_session()——探针实测：把 db.engine 指到一个
        开发库之后调 core.anime._merge_anime，它照样把行 DELETE 掉了；
      · upgrade_from 那条路同理。
    也就是说 2026-08-31 那次把开发库清空的形状，只堵住 clean_tables 是【原样可复现】的。

    改成 autouse 地包住 get_session / get_meta_session：会话一开就查，覆盖上面全部路径，
    且不需要生产代码知道测试的存在（生产侧一行没动）。
    """
    import db as _db
    real_s, real_m = _db.get_session, _db.get_meta_session

    def _s():
        _assert_throwaway(_db.engine, "get_session（用例里的任何写库操作）")
        return real_s()

    def _m():
        _assert_throwaway(_db.meta_engine, "get_meta_session（用例里的任何写库操作）")
        return real_m()

    monkeypatch.setattr(_db, "get_session", _s)
    monkeypatch.setattr(_db, "get_meta_session", _m)
    # 生产模块在 import 时就 `from db import get_session` 绑过去了，得逐个换掉
    for mod in ("core.anime", "core.movies", "core.engine", "core.worker", "core.manual"):
        try:
            m = __import__(mod, fromlist=["*"])
        except Exception:
            continue
        if hasattr(m, "get_session"):
            monkeypatch.setattr(m, "get_session", _s)
        if hasattr(m, "get_meta_session"):
            monkeypatch.setattr(m, "get_meta_session", _m)


@pytest.fixture
def clean_tables(testdb):
    """每个用例前清空业务表与内部标记，用例之间互不干扰。"""
    from sqlmodel import delete
    from db.models import Anime, AnimeTorrent, AnimeAlias, Movie, MovieTorrent, Setting
    _assert_throwaway(testdb.engine, "clean_tables（会 DELETE 六张业务表）")
    _assert_throwaway(testdb.meta_engine, "clean_tables 的 meta 侧")
    with testdb.get_session() as s:
        for m in (AnimeTorrent, AnimeAlias, Anime, MovieTorrent, Movie):
            s.exec(delete(m))
        s.commit()
    with testdb.get_meta_session() as s:
        # 【按前缀删，不能按精确键名】(R26) 这些标记的键现在带业务库身份后缀
        # （`_idle_backfilled@sqlite:/path/to.db`，理由见 db.data_identity）——
        # 按精确名删等于一个都删不掉，而删不掉的后果是隐蔽的：
        # 前一个用例做过的回填会让后一个用例走上另一条分支。
        from sqlmodel import select as _sel
        for row in s.exec(_sel(Setting)):
            if any(row.key == k or row.key.startswith(k + "@") for k in _INTERNAL_MARKERS):
                s.delete(row)
        s.commit()
    return testdb


@pytest.fixture
def upgrade_from(tmp_path):
    """造一个【真正的旧库】：升到指定 revision 就停，灌完数据再升到 head。

    用法：
        eng = upgrade_from("c7e1a93b4d02")      # 停在那一版，此时表结构是旧的
        ... 用 eng 灌数据（可以插入新版会被约束拦下的数据）...
        upgrade_from.to_head(eng)               # 走真实的升级路径
        ... 断言 ...

    【为什么需要它】本项目的缺陷有相当比例**只在存量库上出现**：第 9 轮五条 P1 里三条如此，
    而全套用例当时用的都是新建库 fixture——新建库一路建到 head，永远碰不到"老结构 + 新代码"
    这条路径。"建到 head 再把版本号改回去"是不够的：那样表结构其实还是新的。
    """
    import sqlalchemy as sa

    from db import schema

    made = []

    def _make(revision: str):
        p = tmp_path / f"old-{revision}-{len(made)}.db"
        eng = sa.create_engine(f"sqlite:///{p}")
        schema.upgrade(eng, "data", revision)
        made.append(eng)
        return eng

    _make.to_head = lambda eng: schema.upgrade(eng, "data")
    _make.revision_of = lambda eng: schema.current_revision(eng, "data")
    return _make
