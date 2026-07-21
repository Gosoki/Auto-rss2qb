"""数据库：SQLite + SQLModel，开启 WAL 让读写并发（后台轮询写、UI 读）。"""
import logging

import sqlalchemy as sa
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from config import DB_PATH

log = logging.getLogger("autorss")

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
    _migrate_add_columns()


def _migrate_add_columns():
    """给已存在的表补上模型里新增的列（create_all 不会 ALTER 老表）。

    只做『加列』这一种轻量迁移，足以覆盖后续给模型加字段的场景（如 bgm 元数据、
    qB 实时态等）。新列一律以可空加入，老行取 NULL。
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
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}'
                )
            log.info("数据库迁移：%s 加列 %s", table.name, col.name)


def get_session() -> Session:
    return Session(engine)
