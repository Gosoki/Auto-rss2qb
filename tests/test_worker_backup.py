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


# ---------------- (R22) 维护期的 DatabaseBusy 不是"异常" ----------------

def test_maintenance_pauses_are_not_logged_as_errors(caplog):
    """整库维护（切库/迁移）落在某条后台循环的轮次中间时，不该被报成 ERROR。

    那不是异常，是**计划内的**、用户自己点出来的、秒级的暂停。
    逐条报成 `ERROR ...异常` 会在 /logs 页顶出一片红，掩盖同一时间段里真正的错误 ——
    而这个项目一共有 13 处这样的记账点，逐处判必然漏（第①号形状），所以收成一个函数。
    """
    import logging

    import db
    from core import worker as W

    with caplog.at_level(logging.INFO, logger="autorss"):
        W.loop_error("本轮", db.DatabaseBusy("数据库维护中（正在切库），请稍候再试"))
        W.loop_error("本轮", RuntimeError("真的炸了"))

    busy = [r for r in caplog.records if "维护中" in r.getMessage()]
    real = [r for r in caplog.records if "真的炸了" in r.getMessage()]
    assert len(busy) == 1 and busy[0].levelno == logging.INFO, \
        "维护期的暂停被报成了 ERROR（或者一行都没记）"
    assert len(real) == 1 and real[0].levelno == logging.ERROR, \
        "真正的异常被降级了 —— 那比多报几条红更糟"


def test_every_background_loop_routes_through_that_one_helper():
    """13 处记账点必须全部走 `loop_error`，别再有人直接 `log.error(...异常...)`。

    这条挡的正是第①号形状：收成一个函数之后，漏改一处的代价是"那条循环仍然把维护报成红"。
    用 AST 查真实调用，不是字符串匹配（helper 自己的 docstring 里就写着 log.error）。
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "core/worker.py").read_text(encoding="utf8")
    tree = ast.parse(src)
    # loop_error 自己那一行当然是 log.error —— 它就是那个出口，别把它算成违规
    helper = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "loop_error")
    inside_helper = {id(x) for x in ast.walk(helper)}
    offenders = []
    for n in ast.walk(tree):
        if id(n) in inside_helper:
            continue
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "error" and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "log"):
            continue
        first = n.args[0] if n.args else None
        if isinstance(first, ast.Constant) and "异常" in str(first.value):
            offenders.append(f"worker.py:{n.lineno} {first.value[:30]}")
    assert not offenders, ("这些记账点绕过了 loop_error，维护期会被报成红：\n  "
                           + "\n  ".join(offenders))
    # 反向：确认 loop_error 真的被用着（否则上一条会因为"一处都没有"而空过）
    used = sum(1 for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "loop_error")
    assert used >= 10, f"只有 {used} 处走 loop_error，比预期的 13 处少太多"


async def test_maintenance_does_not_fire_a_fake_db_down_alert(monkeypatch, cfg):
    """整库维护撞上看守协程的 30 秒节拍时，不能推『数据库停摆』。

    维护会让 `is_data_down()` 为真（那是有意的：后台循环的把门判据只此一条），
    但看守协程把这个真/假喂给了**边沿触发**的 `notify_state("db_down", …)` ——
    于是用户会为自己刚点下去的、几秒钟的『切到 MySQL』收到一条
    『数据库停摆，采集/下载/同步已暂停』，维护结束再收一条『数据库恢复』。
    随后 `was_down and not now_down` 那条恢复边沿还会成立，白跑一次 init_business_state。
    """
    import asyncio

    import db
    from core import worker as W

    pushed, inits, probes = [], [], []

    async def fake_state(kind, bad, bad_msg, ok_msg=""):
        pushed.append((kind, bad))

    monkeypatch.setattr(W, "notify_state", fake_state)
    monkeypatch.setattr(W, "init_business_state", lambda *a, **kw: inits.append(1))
    monkeypatch.setattr(db, "probe_data_engine", lambda: probes.append(1) or "")

    async def fast_sleep(_):
        raise asyncio.CancelledError      # 只跑一轮就退出
    monkeypatch.setattr(W.asyncio, "sleep", fast_sleep)

    with db.maintenance("正在迁移数据"):
        with pytest.raises(asyncio.CancelledError):
            await W.run_db_watch()

    assert probes == [], "维护期间还去探了一次库（探出什么结论都会污染状态）"
    assert pushed == [], f"维护期间推了假告警：{pushed}"
    assert inits == [], "维护期间白跑了一次业务初始化"


async def test_the_watchdog_still_reports_a_real_outage(monkeypatch, cfg):
    """反向：真的连不上时照常推 —— 别为了消掉误报把真告警一起关掉。"""
    import asyncio

    import db
    from core import worker as W

    pushed = []

    async def fake_state(kind, bad, bad_msg, ok_msg=""):
        pushed.append((kind, bad))

    monkeypatch.setattr(W, "notify_state", fake_state)
    monkeypatch.setattr(W, "init_business_state", lambda *a, **kw: None)
    monkeypatch.setattr(db, "probe_data_engine", lambda: "连不上：Can't connect")

    calls = {"n": 0}

    async def fast_sleep(_):
        calls["n"] += 1
        raise asyncio.CancelledError
    monkeypatch.setattr(W.asyncio, "sleep", fast_sleep)

    with pytest.raises(asyncio.CancelledError):
        await W.run_db_watch()
    assert pushed == [("db_down", True)], f"真停摆时没推告警：{pushed}"


async def test_archive_still_runs_while_something_is_downloading(clean_tables, cfg, monkeypatch):
    """(R22) 有种子在真下时，完成归档仍然要跑。

    `archive_old_completed()` 原来只在 `run_qb_sync` 的**外层**循环体里跑一次，
    而内层 while 的四个出口里有一个是 `has_active_downloading()` ——
    只要每轮都有一条在真下，`idle` 恒被清零、内层永不退出，外层那一句一次都不做。
    判据是 `qb_dlspeed >= max(1, QB_ACTIVE_FLOOR_KBPS*1024)`，而设置页写着"0=只要有速度就算"：
    `QB_ACTIVE_FLOOR_KBPS=0` + 一条涓流种子就能把循环永久钉住 ——
    **`QB_ARCHIVE_AFTER_DAYS` 对它的目标用户（长期挂着下载的人）恒不生效**
    （实测：跑满 200 个内层同步轮，archive 被调 0 次）。
    """
    import asyncio

    from core import engine as E
    from core import worker as W
    from db.models import Anime, AnimeTorrent

    cfg(QB_ENABLED=True, QB_SYNC_STATUS=True, QB_SYNC_INTERVAL=1,
        QB_SLOW_ROUNDS=3, QB_IDLE_RECHECK_MIN=1, QB_ACTIVE_FLOOR_KBPS=0)
    with clean_tables.get_session() as s:
        a = Anime(title="T", season=1, confirmed=True)
        s.add(a); s.commit(); s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="d" * 40, raw_title="x", episode=1,
                           status="sent", qb_progress=0.4, qb_state="downloading"))
        s.commit()

    calls = []

    async def fake_archive():
        calls.append(1)
        return 0

    async def fake_info(hashes):
        return {h: {"state": "downloading", "progress": 0.4,
                    "dlspeed": 1_000_000, "size": 1} for h in hashes}

    async def yes():
        return True

    monkeypatch.setattr(E, "archive_old_completed", fake_archive)
    monkeypatch.setattr(E.qb, "torrents_info", fake_info)
    monkeypatch.setattr(E.qb, "reachable", yes)
    monkeypatch.setattr(E, "qb_kick", asyncio.Event())
    monkeypatch.setattr(W, "_ARCHIVE_EVERY", 0)      # 节流开到 0：每轮都该跑到
    _real_sleep = asyncio.sleep     # 【先存原函数】直接 lambda: asyncio.sleep(0) 会递归调到自己

    async def fast(_):
        await _real_sleep(0)
    monkeypatch.setattr(W.asyncio, "sleep", fast)

    task = asyncio.create_task(W.run_qb_sync())
    E.qb_kick.set()
    for _ in range(60):
        await asyncio.sleep(0.01)
        if len(calls) >= 3:
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert E.has_active_downloading(), "前提：这一条确实被判成『在真下』（内层不会退出）"
    assert len(calls) >= 3, (
        f"内层高频轮询期间归档只被调了 {len(calls)} 次 —— "
        "有人一直在下载时 QB_ARCHIVE_AFTER_DAYS 就永远不生效")


async def test_the_sweep_loop_actually_clears_delivery_wreckage(clean_tables, monkeypatch, cfg):
    """巡检轮必须真的调到残骸清扫 —— 否则 `sweep_stale_delivering` 是死代码。

    残骸（`status=downloading` 但没有协程在管）以前**只有重启进程才清得掉**：
    sync 显式跳过它、集去重认定该集已有一份、看守协程的恢复边沿也不复位它，
    而它还会把设置页的切库/迁移永久拒死。
    """
    import asyncio

    from sqlmodel import select

    from core import worker as W
    from db.models import Anime, AnimeTorrent

    with clean_tables.get_session() as s:
        a = Anime(title="T", season=1, confirmed=True)
        s.add(a); s.commit(); s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="a" * 40, raw_title="x",
                           episode=1, status="downloading"))
        s.commit()

    async def noop():
        return 0
    for name in ("sweep_finished", "sweep_idle", "sweep_alerts"):
        monkeypatch.setattr(W.anime, name, noop)

    slept = {"n": 0}

    async def fast(_):
        slept["n"] += 1
        if slept["n"] >= 2:
            raise asyncio.CancelledError
    monkeypatch.setattr(W.asyncio, "sleep", fast)

    with pytest.raises(asyncio.CancelledError):
        await W.run_sweep()

    with clean_tables.get_session() as s:
        assert s.exec(select(AnimeTorrent)).one().status == "pending", \
            "巡检轮跑完了残骸还在 —— 清扫没被调到"
