"""数据库：SQLModel 之上的【双引擎】——配置永远在本地 SQLite，业务数据可切到 MySQL。

    meta 引擎（恒为本地 SQLite，DB_PATH）  ── setting 表
    data 引擎（SQLite 或 MySQL，可热切换）  ── 其余 6 张业务表

为什么要拆：数据库连接信息本身得存在某个数据库里。如果它跟业务数据一起搬去 MySQL，
那 MySQL 连不上时就再也读不到"该怎么连 MySQL"——先有鸡还是先有蛋。把 setting 钉死在本地
SQLite 就没这个问题：任何时候都读得到配置、切得回来、改得了连接参数。

默认两个引擎指向同一个 SQLite 文件，行为与改造前完全一致。切到 MySQL 后 setting 仍在本地文件。

SQLite 侧开 WAL 让读写并发（后台轮询写、UI 读）；MySQL 侧用连接池 + pre_ping（空闲长连接会被
服务端按 wait_timeout 掐断，不 ping 就会在下次查询时抛 'MySQL server has gone away'）。
"""
import logging

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from config import DB_PATH
from .dialect import adapt_metadata, is_mysql

log = logging.getLogger("autorss")

# 恒留在本地 SQLite 的表（配置）。其余表跟随 data 引擎。
META_TABLES = ("setting",)


def _make_sqlite_engine(path: str):
    eng = create_engine(
        f"sqlite:///{path}",
        echo=False,
        connect_args={"check_same_thread": False},  # NiceGUI 线程池里也会用到连接
    )

    @event.listens_for(eng, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return eng


def make_mysql_engine(url: str):
    """建 MySQL 引擎。pool_pre_ping 必开：服务端 wait_timeout(默认 8 小时) 会掐掉空闲长连接，
    而我们的后台协程正是"长期空闲后突然要查"，不 ping 就会撞 'MySQL server has gone away'。"""
    return create_engine(url, echo=False, pool_pre_ping=True, pool_recycle=3600,
                         pool_size=5, max_overflow=5)


# 【顺序要紧】metadata 是 import models 时才被填充的，必须先导入模型、再定型，最后才建引擎/建表。
# 在空 metadata 上调 adapt_metadata 等于什么都没做，那样 MySQL 会拿到 SQLModel 的兜底
# VARCHAR(255)，把 917 字符的 summary 截断或直接报错。
from . import models  # noqa: E402,F401  仅为注册表结构
adapt_metadata(SQLModel.metadata)

meta_engine = _make_sqlite_engine(DB_PATH)   # 配置库：恒为本地 SQLite
engine = meta_engine                          # 业务库：默认与配置同库；切到 MySQL 后指向 MySQL

# 业务库健康状态：非空＝连不上，内容是最后一次的错误摘要（供 UI 与日志显示）。
#
# 【连不上【不】回退到本地 SQLite】——引擎照旧指着配置的那个库（SQLAlchemy 建引擎并不连接），
# 整个系统停摆等它回来。回退看着"还能用"，实则是【另一份数据集】：停摆期间的确认/下载/改季度
# 全写进本地库，MySQL 回来后这些改动凭空消失，而界面自始至终没有任何异样——比直接停摆危险得多。
# 想用本地库必须去设置页手动切（那是明确的意思表示，切完 _data_down 自然清空）。
_data_down = ""


def _tables(meta: bool) -> list:
    """按归属拆表：meta=True 取 setting，False 取业务表。"""
    return [t for name, t in SQLModel.metadata.tables.items()
            if (name in META_TABLES) is meta]


def data_backend() -> str:
    """当前业务数据落在哪：'sqlite' | 'mysql'。"""
    return "mysql" if is_mysql(engine) else "sqlite"


def engine_desc(eng) -> str:
    """任意引擎的人话描述（绝不含密码）。迁移确认框要用它把两端【具体是哪个库】写清楚——
    只说"本地/MySQL"的话，方向算错了用户根本发现不了。"""
    u = eng.url
    if is_mysql(eng):
        return f"MySQL {u.host}:{u.port or 3306}/{u.database}"
    return f"本地 SQLite {u.database}"


def data_target_desc() -> str:
    """当前业务库的人话描述（设置页显示用，绝不含密码）。"""
    return engine_desc(engine)


def init_db():
    """把两个引擎都升到最新版本。meta 与 data 指向同一个 SQLite 时，两边各管各的表、互不重复。"""
    from . import migrate
    migrate.upgrade(meta_engine, "meta")     # setting 表
    init_data_engine()


def init_data_engine() -> None:
    """把当前 data 引擎升到最新版本。切库/迁移到新库后也要调它（新库可能完全是空的）。

    以前这里是 create_all + 五个"每次启动都比对补一遍"的临时迁移函数。改成 Alembic 之后
    库里有 alembic_version_data 记着版本号：全新库一路建到 head，已有库只跑缺的那几步，
    已是最新则一条 DDL 都不发。表结构与索引（含仅 SQLite 有的 partial index）都在版本脚本里。
    """
    from . import migrate
    migrate.upgrade(engine, "data")


def configured_mysql_url() -> str | None:
    """按 setting 里的连接参数拼 MySQL URL；参数不全返回 None。"""
    import config
    from .dialect import mysql_url
    host, name = (config.DB_MYSQL_HOST or "").strip(), (config.DB_MYSQL_NAME or "").strip()
    if not host or not name:
        return None
    return mysql_url(host, config.DB_MYSQL_PORT, config.DB_MYSQL_USER,
                     config.DB_MYSQL_PASSWORD, name, config.DB_MYSQL_CHARSET or "utf8mb4")


MYSQL_ERR_NO_DB = 1049        # ER_BAD_DB_ERROR：Unknown database（库不存在）
MYSQL_ERR_DB_EXISTS = 1007    # ER_DB_CREATE_EXISTS


def mysql_errno(exc) -> int | None:
    """从 SQLAlchemy 包装的异常里挖出 MySQL 的错误码（挖不到返回 None）。

    用错误码而不是匹配英文报错文本：MySQL 的消息随版本和 lc_messages 变，
    而错误码是稳定契约（1049=库不存在，可据此提示用户去点『创建数据库』）。
    """
    orig = getattr(exc, "orig", exc)
    args = getattr(orig, "args", None)
    if args and isinstance(args[0], int):
        return args[0]
    return None


def create_mysql_database(host: str, port: int, user: str, password: str,
                          name: str, charset: str = "utf8mb4") -> str:
    """在 MySQL 服务器上建库（已存在则原样返回，不报错）。返回人话结果。

    连的是【服务器】不是具体库——库还不存在，带库名根本连不上。
    库名与字符集是拼进 SQL 的（CREATE DATABASE 的标识符不接受绑定参数），
    故先过 db.dialect 里的白名单校验再拼，且用反引号包起来。
    排序规则跟着字符集走：utf8mb4 用 utf8mb4_unicode_ci，其余交给服务端默认。
    """
    from .dialect import BINARY_COLLATION, mysql_server_url, valid_charset, valid_db_name
    name = (name or "").strip()
    charset = (charset or "utf8mb4").strip()
    if not valid_db_name(name):
        raise ValueError("库名只能用字母、数字、下划线，且不超过 64 个字符")
    if not valid_charset(charset):
        raise ValueError("字符集名不合法")
    url = mysql_server_url(host, port, user, password, charset)
    eng = make_mysql_engine(url)
    try:
        with eng.connect() as conn:
            exists = conn.exec_driver_sql(
                "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s",
                (name,)).fetchone() is not None
            if exists:
                return f"库 `{name}` 已存在，无需创建"
            # 库默认排序规则也用二进制：建表时每列都会显式带 COLLATE（见 dialect.adapt_metadata），
            # 但库默认值一致才不会让日后手工加的表悄悄变回 _ci 语义（Ⅱ==II 那类等价折叠）。
            coll = f" COLLATE {BINARY_COLLATION}" if charset == "utf8mb4" else ""
            conn.exec_driver_sql(
                f"CREATE DATABASE `{name}` CHARACTER SET {charset}{coll}")
            conn.commit()
        log.info("已在 MySQL %s:%s 上创建库 %s（%s%s）", host, port, name, charset, coll)
        return f"已创建库 `{name}`（{charset}{coll}）"
    finally:
        eng.dispose()


def data_down() -> str:
    """业务库当前连不上的原因；空串＝正常。全项目判『能不能干活』都读这个。"""
    return _data_down


def mark_data_down(reason: str) -> None:
    """就地标记停摆。给【页面/处理器撞上连接层异常】的那一刻用：状态立刻对全站生效，
    不必等看守协程下一次探测（那可能还有 30 秒，这期间后台照跑、别的页面照报错）。
    已经是停摆则不覆盖，保留最先那条原因。"""
    global _data_down
    if not _data_down:
        _data_down = reason[:200]
        log.error("业务数据库连不上，系统停摆（不回退本地库，等它回来）：%s", _data_down)


def probe_data_engine() -> str:
    """探一次业务库（SELECT 1）。通了返回空串，否则返回错误摘要并把状态标成停摆。

    从『不通』变回『通』时补跑一次 Alembic 升级：停摆期间可能漏过了版本升级——
    比如换了带新迁移的代码才启动，而那会儿库还没回来，启动时的 init_data_engine 根本没跑成。
    只在状态【变化】时记日志，否则每 30s 一条会把日志刷爆。
    """
    global _data_down
    was = _data_down
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    except Exception as e:
        _data_down = f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"
        if not was:
            log.error("业务数据库连不上，系统停摆（不回退本地库，等它回来）：%s", _data_down)
        return _data_down
    _data_down = ""
    if was:
        try:
            init_data_engine()
        except Exception as e:
            _data_down = f"版本升级失败 {type(e).__name__}: {str(e).splitlines()[0][:160]}"
            log.error("业务数据库回来了，但升级失败，仍停摆：%s", _data_down)
            return _data_down
        log.info("业务数据库已恢复，系统继续：%s", data_target_desc())
    return ""


def apply_configured_backend() -> str:
    """启动时按 DB_BACKEND 把业务库连过去。返回人话结果，供启动日志用。

    连不上【不让应用起不来】，也【不回退到本地 SQLite】（回退的危害见 _data_down 处注释）：
    引擎照旧指着配置的库、系统标为停摆，应用照常起——配置在本地 SQLite，设置页进得去，
    可以改连接参数或手动切回本地库；MySQL 复活后由 run_db_watch 自动接上，不用重启。
    """
    global engine, _data_down
    import config
    if (config.DB_BACKEND or "sqlite") != "mysql":
        probe_data_engine()
        return data_target_desc()
    url = configured_mysql_url()
    if url is None:
        _data_down = "DB_BACKEND=mysql 但连接参数不全（去设置页『数据库』补全，或切回本地 SQLite）"
        log.error("业务数据库停摆：%s", _data_down)
        return f"停摆 — {_data_down}"
    engine = engine_for(url)      # 只建引擎不连接；连不上也照样指着它，等它回来
    if probe_data_engine():
        return f"停摆 — {data_target_desc()}：{_data_down}"
    init_data_engine()
    return data_target_desc()


def engine_for(url: str | None):
    """按 URL 建引擎：None=默认本地 SQLite(DB_PATH)，sqlite:// 走 SQLite 工厂，其余走 MySQL 工厂。

    按 scheme 分派而不是"非 None 即 MySQL"：切换逻辑本身与后端无关，能用两个 SQLite 文件
    完整地测出来（没有 MySQL 服务器的环境下这是唯一可行的验证路径）。
    """
    if url is None:
        return _make_sqlite_engine(DB_PATH)
    if url.startswith("sqlite"):
        return create_engine(url, echo=False, connect_args={"check_same_thread": False})
    return make_mysql_engine(url)


def switch_data_engine(url: str | None) -> None:
    """把业务数据引擎切到 url（None=切回本地 SQLite）。【只切连接，不搬数据】。

    切换点的安全性：本项目所有 `with get_session()` 块内都没有 await（审计用 AST 全仓核过），
    单进程 asyncio 下这些块是原子的，故从 UI 处理器里切不会切在半个事务中间。
    新库连不上/建表失败就原样退回旧引擎，别把应用留在半死状态。
    """
    global engine
    old = engine
    engine = engine_for(url)
    try:
        init_data_engine()
    except Exception:
        if engine is not old:
            engine.dispose()
        engine = old
        raise
    if old is not meta_engine:   # 默认态下 old 就是 meta_engine，不能 dispose（配置还要用它）
        old.dispose()
    global _data_down
    _data_down = ""              # 切过来的库刚跑通了迁移，必然是活的；停摆状态就此解除
    log.info("业务数据库已切换到：%s", data_target_desc())


def get_session() -> Session:
    """业务数据会话（anime / movie / 种子 / 源组）。"""
    return Session(engine)


def get_meta_session() -> Session:
    """配置会话（setting 表，恒为本地 SQLite，与业务库切换无关）。"""
    return Session(meta_engine)
