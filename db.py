"""数据库：SQLite + SQLModel，开启 WAL 让读写并发（后台轮询写、UI 读）。"""
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from config import DB_PATH

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
    import models  # noqa: F401  确保表被注册
    SQLModel.metadata.create_all(engine)


def get_session() -> Session:
    return Session(engine)
