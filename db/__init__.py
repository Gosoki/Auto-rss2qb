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


MYSQL_CONNECT_TIMEOUT = 5   # 秒。只管【建连接】，不限制查询本身


def make_mysql_engine(url: str):
    """建 MySQL 引擎。pool_pre_ping 必开：服务端 wait_timeout(默认 8 小时) 会掐掉空闲长连接，
    而我们的后台协程正是"长期空闲后突然要查"，不 ping 就会撞 'MySQL server has gone away'。

    为什么要收紧 connect_timeout（pymysql 默认已有 10 秒，实测确认）：
    主机【关机或被防火墙 DROP】时（拒绝连接那种是立刻返回，不算），建连接会一直挂到超时。
    而建连接是【同步】调用——页面处理器里随手一条查询就能把整个事件循环冻住这么久，
    界面、下载、qB 同步一起停。看守协程那条路径已经丢进线程池了（见 worker.run_db_watch，
    实测把界面冻结从 5 秒降到 29 毫秒），但普通查询这条路没有，只能靠超时兜底。
    5 秒对局域网/常见云库都绰绰有余，最坏卡顿也压到勉强可接受。
    """
    return create_engine(url, echo=False, pool_pre_ping=True, pool_recycle=3600,
                         pool_size=5, max_overflow=5,
                         connect_args={"connect_timeout": MYSQL_CONNECT_TIMEOUT})


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
# 【自愈不了的停摆】与"连不上"分开记：探测通了也不解除，必须人工介入（改配置 / 切库 / 修好再重启）。
# 目前有两个来源：① 配置参数不全，压根没有正确的库可指；② 启动初始化就失败（建表/迁移抛异常）。
# 踩过的坑：参数不全时只标 _data_down，而 engine 此时仍是本地 SQLite（没有别的地方可指），
# 看守协程 30 秒后拿这个引擎探一次 SELECT 1——探的是本地库，必然成功——于是停摆被自动解除，
# 系统就在【错误的库】上继续跑，正是本文件极力要避免的那种静默回退。
# 这一条 probe 解不了：只有改完配置、重新走 apply_configured_backend / switch_data_engine 才清除。
_data_fatal = ""


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
    from . import schema
    schema.upgrade(meta_engine, "meta")     # setting 表
    upgrade_data_schema()


def upgrade_data_schema() -> None:
    """把当前 data 引擎的【表结构】升到最新版本（不碰引擎本身，故不叫 init_*）。切库/迁移到新库后也要调它（新库可能完全是空的）。

    以前这里是 create_all + 五个"每次启动都比对补一遍"的临时迁移函数。改成 Alembic 之后
    库里有 alembic_version_data 记着版本号：全新库一路建到 head，已有库只跑缺的那几步，
    已是最新则一条 DDL 都不发。表结构与索引（含仅 SQLite 有的 partial index）都在版本脚本里。
    """
    from . import schema
    schema.upgrade(engine, "data")


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
    排序规则跟着字符集走：utf8mb4 用 BINARY_COLLATION(utf8mb4_bin)，其余交给服务端默认。
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


def data_down_reason() -> str:
    """业务库当前不可用的【原因串】；空串＝正常。要判真假请用 is_data_down()。

    名字带 _reason 是有必要的：它长得像谓词却返回字符串，调用点历史上混着
    `bool(db.data_down())` / `if db.data_down():` / 直接把它拼进文案三种写法，
    将来只要有人写 `if db.data_down() == True` 就会静默恒假。
    配置层故障排在前面：它是根因，且自愈不了，先报它才不会误导。
    """
    return _data_fatal or _data_down


def is_data_down() -> bool:
    """业务库现在能不能干活。各后台循环/页面的把门判据统一走它。"""
    return bool(data_down_reason())


def mark_data_fatal(reason: str) -> None:
    """标记【自愈不了】的停摆：看守协程探通了也不会解除它。

    给"探测本身证明不了系统可用"的故障用——比如启动时建表/迁移就失败：
    此刻 SELECT 1 照样能通（连接是好的），但表结构可能是半截的，让后台跑起来只会到处报错。
    解除方式只有人工介入：改配置后重新 apply_configured_backend，或手动 switch_data_engine。
    """
    global _data_fatal
    if not _data_fatal:
        _data_fatal = reason[:200]
        log.error("业务数据库停摆（需人工处理，不会自动恢复）：%s", _data_fatal)


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
    比如换了带新迁移的代码才启动，而那会儿库还没回来，启动时的 upgrade_data_schema 根本没跑成。
    只在状态【变化】时记日志，否则每 30s 一条会把日志刷爆。
    """
    global _data_down
    if _data_fatal:
        # 配置就是错的，engine 根本没指向目标库——此刻去探它，探通了也毫无意义
        # （多半探的是本地 SQLite），反而会把停摆解除。直接原样返回，等人去改配置。
        return _data_fatal
    was = _data_down
    # 【先把引擎快照下来，写结论前再核一次】本函数会被丢进线程池跑（worker.run_db_watch、
    # 页面的『立即重连』），而 switch_data_engine 在事件循环上随时可能把模块级 engine 换掉。
    # 不核对的话会出现：探测在【旧库】上失败 → 期间用户切到了新库并成功 →
    # 这条陈旧结论把刚切好的库标成停摆，而它其实好好的。
    eng = engine
    try:
        with eng.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    except Exception as e:
        if eng is not engine:
            return ""      # 期间切库了：这次探的是旧库，结论作废，不要污染新状态
        _data_down = f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"
        if not was:
            log.error("业务数据库连不上，系统停摆（不回退本地库，等它回来）：%s", _data_down)
        return _data_down
    if eng is not engine:
        return ""          # 探的是旧库、期间切走了；新库的状态由 switch_data_engine 自己负责
    if was:
        # 【顺序要紧】升级跑完了才清停摆标记。本函数会被丢到线程里跑（见 worker.run_db_watch），
        # 若先清标记再升级，事件循环上的协程会在【迁移进行到一半】时看到"库已恢复"而涌进来查表。
        try:
            upgrade_data_schema()
        except Exception as e:
            _data_down = f"版本升级失败 {type(e).__name__}: {str(e).splitlines()[0][:160]}"
            log.error("业务数据库回来了，但升级失败，仍停摆：%s", _data_down)
            return _data_down
        log.info("业务数据库已恢复，系统继续：%s", data_target_desc())
    _data_down = ""
    return ""


def apply_configured_backend() -> str:
    """启动时按 DB_BACKEND 把业务库连过去。返回人话结果，供启动日志用。

    连不上【不让应用起不来】，也【不回退到本地 SQLite】（回退的危害见 _data_down 处注释）：
    引擎照旧指着配置的库、系统标为停摆，应用照常起——配置在本地 SQLite，设置页进得去，
    可以改连接参数或手动切回本地库；MySQL 复活后由 run_db_watch 自动接上，不用重启。
    """
    global engine, _data_fatal
    import config
    _data_fatal = ""       # 每次重新按配置连都重算，别让上一次的配置错误粘住
    if (config.DB_BACKEND or "sqlite") != "mysql":
        probe_data_engine()
        return data_target_desc()
    url = configured_mysql_url()
    if url is None:
        # 走【配置层】停摆而不是 _data_down：此刻 engine 仍是本地 SQLite，
        # 若记成连接层故障，看守协程一探本地库就通过、把停摆解除，系统在错的库上继续跑。
        _data_fatal = "DB_BACKEND=mysql 但连接参数不全（去设置页『数据库』补全，或切回本地 SQLite）"
        log.error("业务数据库停摆：%s", _data_fatal)
        return f"停摆 — {_data_fatal}"
    engine = engine_for(url)      # 只建引擎不连接；连不上也照样指着它，等它回来
    if probe_data_engine():
        return f"停摆 — {data_target_desc()}：{_data_down}"
    upgrade_data_schema()
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

    切换点的安全性：`with get_session()` 块内都没有 await，所以**在事件循环这一条线上**
    它们是原子的，从 UI 处理器里切不会切在半个事务中间。
    【但"单进程 asyncio ⇒ 全都原子"这个说法不准，别照抄】数据库并非只被事件循环访问：
    `pages/api.py` 的 `/api/qb/done` 是**同步**路由，FastAPI 会把它丢进 anyio 工作线程；
    `asyncio.to_thread(db.probe_data_engine)`（看守协程与页面的『立即重连』）同理。
    这几条线程路径与事件循环里的"读改写"序列是真的会交错的——
    这正是 DECISIONS 的 E-12 要拍板的事。在它定下来之前，这段话只对事件循环内部成立。

    【迁移失败时的两种处理，取决于切去哪】
      · 切到【别的库】失败 → 原样退回旧引擎并抛，别把应用留在半死状态（旧行为，不变）。
      · 切回【本地 SQLite】失败 → 仍然把 engine 指过去，但**保留 fatal 标记**。
        理由：fatal 的来源之一就是"启动时建表/迁移失败"，而它的两条清除路径
        （启动时的 apply_configured_backend、设置页的本函数）以前**都**要先跑一遍迁移——
        当 fatal 的根因就在 alembic 层时，用户唯一的自救按钮跑的正是那件失败的事，
        「切回本地 SQLite」与「去设置页改数据库」两个出口同时是死的（实测复现过：
        一条 revision 文件坏掉时，两个按钮都抛同一个 SyntaxError）。
        至少要让连接层回到本地，好让用户看得到设置页、看得到那条 fatal 原因。
    """
    global engine, _data_down, _data_fatal
    old = engine
    engine = engine_for(url)
    try:
        upgrade_data_schema()
    except Exception as e:
        if url is not None:
            if engine is not old:
                engine.dispose()
            engine = old
            raise
        # 切回本地却仍然失败：连接层落到本地，但停摆状态【不解除】——
        # 表结构可能是半截的，让后台在上面跑比停着更糟。
        if old is not meta_engine:
            old.dispose()
        _data_fatal = (f"已切回本地 SQLite，但迁移仍然失败：{type(e).__name__}: {e}。"
                       "这多半不是连接问题（迁移脚本本身有问题）——请看日志，"
                       "修好之后重启；界面此刻可用，但采集/下载仍停着。")
        log.exception("切回本地 SQLite 后迁移仍失败，保留停摆")
        return
    if old is not meta_engine:   # 默认态下 old 就是 meta_engine，不能 dispose（配置还要用它）
        old.dispose()
    # 走到这里说明迁移真的跑通了，库必然是活的；两种停摆一并解除
    # （配置层那条也要清：用户在设置页补全参数后正是靠这个动作恢复的）
    _data_down = _data_fatal = ""
    log.info("业务数据库已切换到：%s", data_target_desc())


def get_session() -> Session:
    """业务数据会话（anime / movie / 种子 / 源组）。"""
    return Session(engine)


def get_meta_session() -> Session:
    """配置会话（setting 表，恒为本地 SQLite，与业务库切换无关）。"""
    return Session(meta_engine)
