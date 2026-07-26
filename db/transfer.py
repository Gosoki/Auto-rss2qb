"""数据迁移：把业务表在两个库之间整体搬运（SQLite ⇄ MySQL，双向同一套代码）。

与"切换数据库"的分工：
  · switch_data_engine —— 只改连接，一行数据都不动（换到已有数据的库、或先建空库再迁）
  · migrate_data       —— 把源库的业务数据复制到目标库（连接不变，搬完要不要切由调用方决定）

设计要点：
  · 【保留主键】anime_id / movie_id 是跨表的逻辑外键（模型里没声明 FK，靠 id 关联），
    id 一变整个库的关联全断。故显式带 id 插入，插完再把 MySQL 的 AUTO_INCREMENT 顶到 max(id)+1，
    否则下一条新行会从 1 开始撞主键。
  · 【表序】先父后子（anime → alias/torrent，movie → torrent），失败时中途停下也不会留下
    指向不存在父行的孤儿。
  · 【分块】每批 500 行，避免一次性把几万行 executemany 塞进一个事务（MySQL 有
    max_allowed_packet，SQLite 有变量数上限）。
  · 【不搬 setting】配置恒留本地 SQLite，见 db.__init__ 的双引擎说明。
  · 【幂等/可重跑】默认要求目标表为空，避免主键冲突半途炸掉留下半个库；overwrite=True 时
    先清空目标业务表再搬。
"""
import logging
import os

import sqlalchemy as sa
from sqlmodel import SQLModel

from .dialect import is_mysql, quote

log = logging.getLogger("autorss")

# 复制顺序：父表在前。虽然模型没声明 FK，但 anime_id/movie_id 是逻辑外键，
# 中断时按这个序至少不会留下"子行指向还没搬过来的父行"。
TABLE_ORDER = ("sourcegroup", "anime", "anime_alias", "animetorrent", "movie", "movietorrent")

_CHUNK = 500


def _table(name):
    return SQLModel.metadata.tables[name]


def same_database(a, b) -> bool:
    """两个 Engine 是不是指向【同一个物理库】。对象身份不算数，看连接目标。

    SQLite 比 realpath（相对路径、软链、./ 前缀都能指向同一个文件）；
    其余按 (方言, 主机, 端口, 库名) 比，主机大小写不敏感、端口取默认值。
    """
    ua, ub = a.url, b.url
    if ua.get_backend_name() != ub.get_backend_name():
        return False
    if ua.get_backend_name() == "sqlite":
        da, dbn = ua.database, ub.database
        if not da or not dbn:            # 内存库（:memory:）各自独立
            return da == dbn
        return os.path.realpath(da) == os.path.realpath(dbn)
    return ((ua.host or "").lower(), ua.port or 3306, ua.database) == \
           ((ub.host or "").lower(), ub.port or 3306, ub.database)


def count_rows(engine, tables=TABLE_ORDER) -> dict:
    """各业务表的行数（表不存在记 0）。用于迁移前后比对与"目标库是否为空"的判断。"""
    out = {}
    insp = sa.inspect(engine)
    with engine.connect() as conn:
        for name in tables:
            if not insp.has_table(name):
                out[name] = 0
                continue
            out[name] = conn.execute(
                sa.select(sa.func.count()).select_from(_table(name))).scalar_one()
    return out


def _reset_autoincrement(engine, name: str) -> None:
    """把 MySQL 的 AUTO_INCREMENT 顶到 max(id)+1。

    带显式 id 插入不会推进 AUTO_INCREMENT 计数器，不修的话下一条新行会从 1 开始、
    立刻撞上已存在的主键（表现为迁移后"一采集就报 Duplicate entry"）。
    SQLite 的 rowid 自增取的是 max(rowid)+1，天然不需要处理。
    """
    if not is_mysql(engine):
        return
    t = _table(name)
    if "id" not in t.c:
        return
    with engine.begin() as conn:
        mx = conn.execute(sa.select(sa.func.max(t.c.id))).scalar()
        conn.exec_driver_sql(
            f"ALTER TABLE {quote(engine, name)} AUTO_INCREMENT = {int(mx or 0) + 1}")


def migrate_data(src_engine, dst_engine, *, overwrite: bool = False,
                 progress=None) -> dict:
    """把业务表从 src 复制到 dst。

    返回 {"moved": {表名: 实际写入行数}, "src_before": {表名: 迁移【开始前】源库行数}}。
    src_before 必须带出来给 verify 用——不能让 verify 事后现查源库，那有个自证陷阱：
    万一源和目标其实是同一个库，overwrite 已经把它清空了，现查两边都是 0，反而"校验通过"。
    （别把它塞进 moved 里当一个键：那样 sum(moved.values()) 这种自然写法会当场炸。）

    overwrite=False 且目标已有数据 → 直接抛，别在"目标非空"时半途撞主键留下残局。
    progress(table, done, total) 可选回调，供 UI 显示进度。
    """
    # 【比连接目标，不比对象身份】同一个物理库完全可以有两个不同的 Engine 对象
    #（调用方按同一串 URL 又 create_engine 了一次），那时 `is` 判等不出来，
    # 而 overwrite 会先把目标清空、再从"已经空了的源"读出 0 行——数据当场蒸发且伪装成成功。
    # 这是删数据之前的最后一道闸，将来任何调用方算错方向都必须炸在这里。
    if same_database(src_engine, dst_engine):
        raise ValueError("源库与目标库指向同一个数据库，无需迁移（也不能迁，会把它清空）")

    src_counts = count_rows(src_engine)
    dst_counts = count_rows(dst_engine)
    if not overwrite and any(dst_counts.values()):
        busy = "、".join(f"{k} {v} 行" for k, v in dst_counts.items() if v)
        raise ValueError(f"目标库已有数据（{busy}）。请先勾选『覆盖目标库』，或换一个空库。")

    if overwrite:
        # 逆序清空（先子后父），语义上更干净；这些表之间没有真 FK，顺序其实不影响执行
        with dst_engine.begin() as conn:
            for name in reversed(TABLE_ORDER):
                conn.execute(sa.delete(_table(name)))
        log.info("数据迁移：已清空目标库业务表")

    moved = {}
    for name in TABLE_ORDER:
        t = _table(name)
        total = src_counts.get(name, 0)
        done = 0
        cols = list(t.c.keys())
        with src_engine.connect() as sconn:
            # 按主键排序取，保证分页稳定；stream_results 让大表不必一次读进内存
            order = t.c.id if "id" in t.c else t.c[cols[0]]
            result = sconn.execution_options(stream_results=True, yield_per=_CHUNK).execute(
                sa.select(t).order_by(order))
            while True:
                rows = result.fetchmany(_CHUNK)
                if not rows:
                    break
                payload = [dict(zip(cols, r)) for r in rows]
                with dst_engine.begin() as dconn:
                    dconn.execute(sa.insert(t), payload)   # 显式带 id，保住跨表关联
                done += len(payload)
                if progress:
                    progress(name, done, total)
        _reset_autoincrement(dst_engine, name)
        moved[name] = done
        log.info("数据迁移：%s %d 行", name, done)
    return {"moved": moved, "src_before": src_counts}


def verify(src_engine, dst_engine, src_counts: dict | None = None) -> list:
    """迁完逐表比行数，返回不一致的说明（空列表=完全一致）。

    src_counts 传【迁移开始前】的源库行数快照（migrate_data 会一并返回）。
    不传就现查源库——那样有个致命的自证陷阱：万一源和目标其实是同一个库，
    overwrite 已经把它清空了，现查两边都是 0，0==0 反而"校验通过"。
    """
    a = src_counts if src_counts is not None else count_rows(src_engine)
    b = count_rows(dst_engine)
    return [f"{k}: 源 {a.get(k, 0)} 行 / 目标 {b[k]} 行" for k in TABLE_ORDER if a.get(k, 0) != b[k]]
