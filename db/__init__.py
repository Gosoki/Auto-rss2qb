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
    _migrate_rename_tables()   # 老表改名（必须在 create_all 前，免得建出空的新表、老数据留在旧表）
    SQLModel.metadata.create_all(engine)
    _migrate_add_columns()
    _migrate_drop_columns()    # 删模型已移除、老库还残留的列（含 NOT NULL 会让新 INSERT 失败）


_TABLE_RENAMES = {"titlealias": "anime_alias"}  # 旧表名 → 新表名（番剧番名对照加 anime 前缀）


def _migrate_rename_tables():
    """把旧表名改到新表名，保住老数据。幂等（改过一次旧表就没了）。"""
    inspector = sa.inspect(engine)
    for old, new in _TABLE_RENAMES.items():
        if inspector.has_table(old) and not inspector.has_table(new):
            with engine.begin() as conn:
                conn.exec_driver_sql(f'ALTER TABLE "{old}" RENAME TO "{new}"')
            log.info("数据库迁移：表 %s → %s", old, new)


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


# 模型已删、但老库可能还留着的列。含 NOT NULL 的残留列会让不带该列的新 INSERT 直接失败，故启动时必删。
_DROPPED_COLUMNS = {
    "anime": ["enriched", "source_kind"],
    "movie": ["confirmed", "pref_source", "enriched"],
    "animetorrent": ["qb_eta"],
    "movietorrent": ["qb_eta"],
}


def _migrate_drop_columns():
    """删掉模型里已移除、但老库还残留的列（SQLite 3.35+ 支持 DROP COLUMN）。幂等（删过就没了）。"""
    inspector = sa.inspect(engine)
    for table, cols in _DROPPED_COLUMNS.items():
        if not inspector.has_table(table):
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for col in cols:
            if col in existing:
                with engine.begin() as conn:
                    conn.exec_driver_sql(f'ALTER TABLE "{table}" DROP COLUMN "{col}"')
                log.info("数据库迁移：%s 删列 %s", table, col)


def get_session() -> Session:
    return Session(engine)
