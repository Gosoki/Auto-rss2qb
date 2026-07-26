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
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from config import DB_PATH
from .dialect import adapt_metadata, is_mysql, is_sqlite, quote

log = logging.getLogger("autorss")

_NO_DEFAULT = object()  # _column_default 解析不出默认值时的哨兵

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
    """建表 + 跑迁移。meta 与 data 指向同一个 SQLite 时，两边各建各的表、互不重复。"""
    from . import models  # noqa: F401  确保表被注册
    SQLModel.metadata.create_all(meta_engine, tables=_tables(True))
    init_data_engine()


def init_data_engine() -> None:
    """在当前 data 引擎上建业务表并跑迁移。切库后也要调它（新库可能是空的）。"""
    from . import models  # noqa: F401
    SQLModel.metadata.create_all(engine, tables=_tables(False))
    _migrate_add_columns()   # 给模型新增字段的表加列（开发期加字段免删整表）
    _migrate_rename_downloaded_to_sent()   # 老库状态值改名：downloaded → sent
    _migrate_inflight_indexes()   # 给 in-flight 高频查询建 partial index（仅 SQLite）
    _migrate_drop_redundant_indexes()   # 清掉 info_hash 上冗余的非唯一索引（唯一约束索引已覆盖）
    _migrate_widen_mysql_ints()   # 老 MySQL 库的 INT 列拓成 BIGINT（qb_size 会溢出 2GiB）


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


def apply_configured_backend() -> str:
    """启动时按 DB_BACKEND 把业务库连过去。返回人话结果，供启动日志用。

    连不上【不让应用起不来】——退回本地 SQLite 并在日志里说清楚，用户还能进设置页改连接参数
    （配置本来就在本地库，读得到）。否则一次 MySQL 抽风就把整个工具锁死在外面。
    """
    import config
    if (config.DB_BACKEND or "sqlite") != "mysql":
        return data_target_desc()
    url = configured_mysql_url()
    if url is None:
        log.warning("DB_BACKEND=mysql 但连接参数不全，暂用本地 SQLite")
        return data_target_desc()
    try:
        switch_data_engine(url)
    except Exception as e:
        log.error("连接 MySQL 失败（%s），暂用本地 SQLite；请到设置页『数据库』检查连接参数", e)
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
    log.info("业务数据库已切换到：%s", data_target_desc())


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

    标识符引用与参数占位符都走方言（SQLite 是 "x"/?，MySQL 是 `x`/%s），别手拼。
    """
    inspector = sa.inspect(engine)
    for table in _tables(meta=False):
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
            tq, cq = quote(engine, table.name), quote(engine, col.name)
            with engine.begin() as conn:
                conn.exec_driver_sql(f"ALTER TABLE {tq} ADD COLUMN {cq} {ddl_type}")
                if val is not _NO_DEFAULT:      # 非空列：把刚加进来的老行 NULL 回填成模型默认值
                    conn.execute(sa.text(f"UPDATE {tq} SET {cq}=:v WHERE {cq} IS NULL"), {"v": val})
            log.info("数据库迁移：%s 加列 %s%s", table.name, col.name,
                     "" if val is _NO_DEFAULT else f"（回填 {val!r}）")


def _migrate_rename_downloaded_to_sent() -> None:
    """老库状态值迁移：status='downloaded' → 'sent'（该状态语义一直是"已交付给 qB"，故改名以正名）。

    必须做：全项目已无一处再读 'downloaded'，老行若不迁移就会对所有查询静默失联——
    删不掉、统计不到、去重判据认不出（进而把已下过的集重下一遍、重识别把季度冲掉）。
    幂等：迁完就没有匹配行，之后每次启动都是 0 行 UPDATE，代价可忽略。
    """
    with engine.begin() as conn:
        n = 0
        for table in ("animetorrent", "movietorrent"):
            tq = quote(engine, table)
            n += conn.execute(sa.text(
                f"UPDATE {tq} SET status='sent' WHERE status='downloaded'")).rowcount or 0
    if n:
        log.info("数据库迁移：%d 条种子状态 downloaded → sent", n)


def _migrate_inflight_indexes() -> None:
    """给 _inflight_where 的高频查询(has_inflight/has_active_downloading/inflight_*_rows，每唤醒轮 +
    每仪表盘刷新都跑)建 partial index。in-flight 集合天然极小 → 索引也小；常态『无在下』时不必再全表扫
    两表才能确认为空。谓词与 _inflight_where 前两条件对齐(qb_state 作残余过滤)，对查询结果透明、行为等价。

    注意：partial index 的谓词里写死了状态名，而 `CREATE INDEX IF NOT EXISTS` 对【已存在】的同名索引
    什么都不做——状态改名后老库会留着谓词过时的旧索引（永不命中，白占空间且悄悄失去加速）。
    故先比对 sqlite_master 里的实际 SQL，谓词对不上就 DROP 重建；一致则原样跳过，不做无谓重建。

    谓词里的状态集【由 engine.TRACKED_STATUSES 拼出】，不再手抄——它必须与 _inflight_where 的第一个
    条件逐字对齐，两边各写一份的话改了 engine 这里不会报错，只会静默失去索引加速。

    【MySQL 不支持 partial index】（没有 CREATE INDEX ... WHERE），故只在 SQLite 上做；
    MySQL 侧由 create_all 建的普通索引也能把范围缩下来。
    """
    if not is_sqlite(engine):
        return
    from core.engine import TRACKED_STATUSES   # 延迟导入：db 是底层，engine 依赖 db
    states = ",".join(f"'{s}'" for s in TRACKED_STATUSES)
    want = {
        f"ix_{table}_inflight":
            f"CREATE INDEX ix_{table}_inflight ON {table}(status, qb_progress) "
            f"WHERE status IN ({states}) AND qb_progress < 1.0"
        for table in ("animetorrent", "movietorrent")
    }
    with engine.begin() as conn:
        for name, stmt in want.items():
            cur = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
            if cur is not None:
                if (cur[0] or "").strip() == stmt:
                    continue                       # 谓词一致，无需动
                conn.exec_driver_sql(f"DROP INDEX {name}")   # 谓词过时（如状态改名）→ 重建
                log.info("数据库迁移：重建过时的 partial index %s", name)
            conn.exec_driver_sql(stmt)


def _migrate_drop_redundant_indexes() -> None:
    """去掉 info_hash 上冗余的非唯一索引 ix_*_info_hash：该列有 UniqueConstraint(uq_*)，唯一索引已覆盖
    全部等值查找（hash_owned_elsewhere/mark_done_by_hash/去重 upsert）。老库由此前 index=True 建过 ix_*，
    create_all 不会自动 DROP，这里显式清掉。

    先用 inspector 查存在性再 DROP，而不是 `DROP INDEX IF EXISTS`——后者 MySQL 8 不支持；
    且 MySQL 的 DROP INDEX 必须带 ON 表名。
    """
    inspector = sa.inspect(engine)
    for table, name in (("animetorrent", "ix_animetorrent_info_hash"),
                        ("movietorrent", "ix_movietorrent_info_hash")):
        if not inspector.has_table(table):
            continue
        if name not in {i["name"] for i in inspector.get_indexes(table)}:
            continue
        with engine.begin() as conn:
            if is_mysql(engine):
                conn.exec_driver_sql(f"DROP INDEX {quote(engine, name)} ON {quote(engine, table)}")
            else:
                conn.exec_driver_sql(f"DROP INDEX {quote(engine, name)}")


def _migrate_widen_mysql_ints() -> None:
    """把老 MySQL 库里 4 字节的 INT 列拓成 BIGINT（仅 MySQL；SQLite 的 INTEGER 本来就是 64 位）。

    必做：qb_size 存字节数，MySQL 的 INT 上限 2147483647 ≈ 2 GiB。第一个超过 2 GiB 的种子
    （BDRip 单集、剧场版常规 3~10GB）会在 STRICT 模式下抛 1264，把 sync_qb_status 整轮的
    进度更新一起回滚，且该行永远留在 in-flight、每轮复发、界面零报错。
    新库由 dialect.adapt_metadata 直接建成 BIGINT，这里只管在此之前建过表的库。
    幂等：已经是 bigint 的跳过，之后每次启动都是 0 条 ALTER。
    """
    if not is_mysql(engine):
        return
    with engine.begin() as conn:
        for table in _tables(meta=False):
            for col in table.columns:
                if col.primary_key or not isinstance(col.type, sa.BigInteger):
                    continue
                cur = conn.exec_driver_sql(
                    "SELECT DATA_TYPE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
                    (table.name, col.name)).fetchone()
                if cur is None or (cur[0] or "").lower() == "bigint":
                    continue
                null_sql = "NULL" if col.nullable else "NOT NULL"
                conn.exec_driver_sql(
                    f"ALTER TABLE {quote(engine, table.name)} "
                    f"MODIFY {quote(engine, col.name)} BIGINT {null_sql}")
                log.info("数据库迁移：%s.%s 由 %s 拓宽为 BIGINT", table.name, col.name, cur[0])


def get_session() -> Session:
    """业务数据会话（anime / movie / 种子 / 源组）。"""
    return Session(engine)


def get_meta_session() -> Session:
    """配置会话（setting 表，恒为本地 SQLite，与业务库切换无关）。"""
    return Session(meta_engine)
