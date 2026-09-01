"""自动备份协程（core.worker.run_backup）的一条性质：业务库停摆时【照备】。

R17 之前这里有一道 `if not db.is_data_down()`，注释写的是"此时业务库连接本身就不可信"。
那道门的作用域比它保护的东西大：backup_now 的快照源恒为 meta_engine（本地 SQLite），
VACUUM INTO 全程不碰 data 引擎。后果是反的——MySQL 掉线期间一份备份都不做，
而 mark_data_fatal 不自愈，于是"业务库出事"这个最需要有备份的时刻，
恰好是备份彻底停摆的时刻。
"""
import asyncio

import pytest

import core.worker as W


class _Stop(Exception):
    """把 while True 打断在第二圈开头。"""


@pytest.fixture
def one_round(monkeypatch):
    """跑 run_backup 的第一圈，返回 auto_tick 被调用了几次。"""
    async def _run(data_down: bool) -> int:
        calls = []
        rounds = [0]

        async def fake_sleep(_):
            rounds[0] += 1
            if rounds[0] > 1:
                raise _Stop
        monkeypatch.setattr(W.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(W.db, "is_data_down", lambda: data_down)

        async def fake_to_thread(fn, *a, **kw):
            calls.append(fn)
            return True
        monkeypatch.setattr(W.asyncio, "to_thread", fake_to_thread)
        with pytest.raises(_Stop):
            await W.run_backup()
        return calls
    return _run


async def test_backup_runs_while_the_business_db_is_down(one_round):
    """(R17) 备的是本地配置库，业务库连不上与它无关——这一圈必须照做。"""
    from db import backup
    calls = await one_round(True)
    # 【要断言调的是哪个函数，不能只数次数】只看 len(calls)==1 的话，把 auto_tick 换成
    # 任何别的同步函数（等于自动备份完全不做了）用例照样绿 —— 实测把它换成 list_backups，
    # 本文件与 test_backup.py 合计 20 条全部通过。
    assert calls == [backup.auto_tick], f"这一圈跑的不是自动备份：{calls}"


async def test_backup_runs_normally(one_round):
    from db import backup
    assert await one_round(False) == [backup.auto_tick]


def test_the_snapshot_source_never_touches_the_data_engine(tmp_path, monkeypatch, testdb):
    """本文件 docstring 的【论据】：backup_now 的快照源恒为 meta_engine，全程不碰 data 引擎。

    上面两条只验到了【控制流】（那道 is_data_down 的门在不在），而这句机制性的断言
    一行代码都没跑过 —— 于是"业务库停摆时照备"这个结论是悬空的：万一 backup_now 真的
    会碰 data 引擎，去掉那道门就等于让备份在业务库停摆时每次抛异常。
    这里给 data 引擎装一个"一连接就抛"的监听，跑真实的 backup_now。
    """
    from sqlalchemy import event

    import db
    from db import backup as B

    monkeypatch.setattr(B, "BACKUP_DIR", tmp_path)
    touched = []

    @event.listens_for(db.engine, "engine_connect")
    def _seen(conn, *a):
        touched.append(1)

    try:
        res = B.backup_now(keep=3)
    finally:
        event.remove(db.engine, "engine_connect", _seen)
    assert touched == [], f"备份过程中连了 {len(touched)} 次业务引擎"
    ok, why = B.verify(res["path"])
    assert ok, why
