"""(R15) 测试夹具不许碰非一次性的库。

2026-08-31 真的发生过一次：一个"只读"审计 agent 为了复现问题写了个探针，
把 `db.engine` 指到了开发库 `data/autorss.db`，然后用了 `clean_tables` 夹具 ——
99 部番、1621 条种子被 DELETE 干净，只剩它自己插进去的夹具行（`番0`）。
数据从 90 分钟前的备份恢复了，但那次没丢是运气。

根因不在 conftest 的 DB_PATH（它在 import config 之前就指到临时文件了，是对的），
而在 `clean_tables` 用的是 `testdb.get_session()` —— 读的是【调用时刻的 db.engine】。
任何人重新赋值 `db.engine` 之后再用这个夹具，它就照着新目标删，且一声不吭。
"""
import os
import tempfile

import pytest
from pathlib import Path
from sqlmodel import create_engine

_ROOT = Path(__file__).resolve().parent.parent

from conftest import _assert_throwaway


def test_refuses_a_database_outside_the_temp_dir(tmp_path):
    """指向开发库时必须当场抛，而不是"先删了再说"。"""
    repo_db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "autorss.db")
    eng = create_engine(f"sqlite:///{repo_db}")
    with pytest.raises(RuntimeError, match="非临时库"):
        _assert_throwaway(eng, "测试")


def test_allows_the_throwaway_db(tmp_path):
    """pytest 自己的 tmp_path 与 conftest 的临时库都在系统临时目录下，必须放行。"""
    eng = create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    _assert_throwaway(eng, "测试")          # 不抛即通过
    _assert_throwaway(create_engine("sqlite://"), "测试")   # :memory: 天然一次性


def test_refuses_non_sqlite():
    """MySQL 上跑清表夹具一定是搞错了——那是共享库，不是一次性的。"""
    eng = create_engine("mysql+pymysql://u:p@127.0.0.1:3306/whatever")
    with pytest.raises(RuntimeError, match="非 SQLite"):
        _assert_throwaway(eng, "测试")


def test_a_production_function_cannot_write_to_a_non_throwaway_db(testdb, monkeypatch, tmp_path):
    """**这条才是真正的回归网**：用例里调【生产函数】时，闸也必须生效。

    第一版这条用例是假的：docstring 写着「用 request.getfixturevalue 拿夹具，走真实调用路径」，
    而函数体里根本没有那句 —— 它只是把 engine 换掉之后【自己再调一遍 _assert_throwaway】，
    验证的是"我传给它的参数会让它抛"，不是"真实路径上它会被调到"。
    实测：把 conftest 里那两行闸删掉，全套 710 条照样全绿。

    2026-08-31 那次事故的真实形状正是这一条：agent 把 db.engine 指向开发库之后
    **调了生产函数**（clean_tables 只是其中一种），行就被 DELETE 掉了。
    现在闸装在 get_session / get_meta_session 上，这条用例走的就是那条路。
    """
    import db as _db
    from core import anime as A
    from db.models import Anime

    outside = _ROOT / "data" / "__never_created__.db"
    # 【先清一次】断言的语义是"本次运行没碰盘"，不能被上一次跑挂时留下的残留污染 ——
    # 否则闸修好之后这条用例仍然永久红，而红的原因跟被测行为无关。
    outside.unlink(missing_ok=True)
    monkeypatch.setattr(_db, "engine", create_engine(f"sqlite:///{outside}"))
    with pytest.raises(RuntimeError, match="非临时库"):
        A.list_all_anime()                 # 任意一个走 get_session 的生产函数
    assert not outside.exists(), "闸没拦住，库文件被建出来了"


def test_clean_tables_itself_is_guarded(testdb, monkeypatch):
    """clean_tables 那条路同样要走到真实的删表入口。"""
    import db as _db
    outside = _ROOT / "data" / "__never_created2__.db"
    outside.unlink(missing_ok=True)        # 同上
    monkeypatch.setattr(_db, "engine", create_engine(f"sqlite:///{outside}"))
    from sqlmodel import delete
    from db.models import Anime
    with pytest.raises(RuntimeError, match="非临时库"):
        with _db.get_session() as s:       # clean_tables 的第一步就是它
            s.exec(delete(Anime))
    assert not outside.exists()


def test_tempdir_hijack_does_not_disarm_the_guard(monkeypatch):
    """判据不能依赖 tempfile.gettempdir() —— 那是个可写的模块级变量。

    实测过：用例里 `tempfile.tempdir = "/"` 之后，旧判据对任何路径都返回真、闸形同虚设。
    现在钉的是 import 本文件时算出的 _TMP_ROOT，谁也改不动。
    """
    import tempfile as _t
    from conftest import _TMP_ROOT
    monkeypatch.setattr(_t, "tempdir", "/")      # 劫持
    eng = create_engine(f"sqlite:///{_ROOT / 'data' / 'x.db'}")
    with pytest.raises(RuntimeError, match="非临时库"):
        _assert_throwaway(eng, "测试")
    assert _TMP_ROOT != "/", "_TMP_ROOT 不该跟着被改"
