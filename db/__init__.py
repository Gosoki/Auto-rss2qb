"""数据库：SQLite + SQLModel，开启 WAL 让读写并发（后台轮询写、UI 读）。"""
import logging
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from config import DB_PATH

log = logging.getLogger("autorss")

_NO_DEFAULT = object()  # _column_default 解析不出默认值时的哨兵

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=False,
    connect_args={"check_same_thread": False},  # NiceGUI 线程池里也会用到连接
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def init_db():
    from . import models  # noqa: F401  确保表被注册
    SQLModel.metadata.create_all(engine)
    _migrate_add_columns()   # 给模型新增字段的表加列（开发期加字段免删整表）
    _migrate_inflight_indexes()   # 给 in-flight 高频查询建 partial index


def _column_default(col):
    """取列的模型默认值（用于给非空新列回填老行 NULL）。解析不出返回 _NO_DEFAULT。"""
    d = col.default
    if d is None:
        return _NO_DEFAULT
    if getattr(d, "is_scalar", False):
        return d.arg
    if getattr(d, "is_callable", False):
        try:
            return d.arg(None)          # SQLAlchemy 把 default_factory 包成接收 context 的可调用
        except Exception:
            return _NO_DEFAULT
    return _NO_DEFAULT


def _migrate_add_columns():
    """给已存在的表补上模型里新增的列（create_all 不会 ALTER 老表）。

    覆盖后续给模型加字段的场景（bgm 元数据、qB 实时态等）。新列以可空加入；但对模型标注 NOT NULL
    且带默认值的列，加列后立即把老行的 NULL 回填成模型默认值——否则老行该列为 NULL，会被
    status=='pending' / episode>=0 之类过滤静默漏掉，从下载管线/统计里凭空消失。
    """
    inspector = sa.inspect(engine)
    for table in SQLModel.metadata.tables.values():
        if not inspector.has_table(table.name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            ddl_type = col.type.compile(dialect=engine.dialect)
            val = _column_default(col) if not col.nullable else _NO_DEFAULT
            if isinstance(val, datetime):
                val = val.isoformat(sep=" ")   # 冻结成常量字符串（回填老行，非每行现算）
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}')
                if val is not _NO_DEFAULT:      # 非空列：把刚加进来的老行 NULL 回填成模型默认值
                    conn.exec_driver_sql(
                        f'UPDATE "{table.name}" SET "{col.name}"=? WHERE "{col.name}" IS NULL',
                        (val,))
            log.info("数据库迁移：%s 加列 %s%s", table.name, col.name,
                     "" if val is _NO_DEFAULT else f"（回填 {val!r}）")


def _migrate_inflight_indexes() -> None:
    """给 _inflight_where 的高频查询(has_inflight/has_active_downloading/inflight_*_rows，每唤醒轮 +
    每仪表盘刷新都跑)建 partial index。in-flight 集合天然极小 → 索引也小；常态『无在下』时不必再全表扫
    两表才能确认为空。谓词与 _inflight_where 前两条件对齐(qb_state 作残余过滤)，对查询结果透明、行为等价。"""
    ddl = (
        "CREATE INDEX IF NOT EXISTS ix_animetorrent_inflight ON animetorrent(status, qb_progress) "
        "WHERE status IN ('downloaded','downloading') AND qb_progress < 1.0",
        "CREATE INDEX IF NOT EXISTS ix_movietorrent_inflight ON movietorrent(status, qb_progress) "
        "WHERE status IN ('downloaded','downloading') AND qb_progress < 1.0",
    )
    with engine.begin() as conn:
        for stmt in ddl:
            conn.exec_driver_sql(stmt)


def get_session() -> Session:
    return Session(engine)
