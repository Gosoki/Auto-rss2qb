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
from contextlib import contextmanager

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from config import DB_PATH
from .dialect import adapt_metadata, is_mysql

log = logging.getLogger("autorss")

# 恒留在本地 SQLite 的表（配置）。其余表跟随 data 引擎。
META_TABLES = ("setting",)


def _sqlite_engine(url: str):
    """建一个 SQLite 引擎，并把**全部** SQLite 侧的连接期设置挂上去。

    【为什么单独抽出来】本仓库有两处在建 SQLite 引擎：本函数（默认业务库/配置库）
    与 `engine_for()` 的 `sqlite://` 分支（切到另一个 SQLite 文件）。
    R21 之前 PRAGMA 只挂在前者上，实测后者拿到的是 `journal_mode=delete`——
    这正是本项目反复出现的第①号形状：同一个决定应该在 N 处生效，只落了 1 处。
    影响面看着小（`sqlite://` 这条分支目前只有切库用例走），但它的后果是
    **用例验证的引擎与生产用的引擎配置不同**，那类差异一旦被依赖就查不出来。

    · `journal_mode=WAL`：读不挡写。本项目有若干条线程路径会与事件循环并发碰库
      （见 `switch_data_engine` 的说明），非 WAL 下它们互相阻塞。
      **注意 WAL 是写进库文件头的持久属性**，对同一个文件只需设一次，
      但每次连都设是幂等的、也顺手把"别人把它改回 delete 了"这件事纠正回来。
    · `busy_timeout=5000`：与 Python `sqlite3.connect(timeout=5.0)` 的默认值相同，
      这一句今天是**冗余**的（实测两条路径都已经是 5000）。留着是把意图写死：
      默认值变了、或换到别的 DBAPI 上时，这里仍然是 5 秒。
    """
    eng = create_engine(
        url,
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


def _make_sqlite_engine(path: str):
    """按【文件路径】建默认 SQLite 引擎（配置库恒走这条，业务库默认也走这条）。"""
    return _sqlite_engine(f"sqlite:///{path}")


MYSQL_CONNECT_TIMEOUT = 5   # 秒。只管【建连接】，不限制查询本身
# 秒。管【一次查询等回包】多久。connect_timeout 只覆盖 TCP 握手那一段——
# 主机连得上但库查不动时（锁等待、磁盘满、大表全扫、网络半开），查询会永久挂着，
# 而那正是"停摆状态机永远进不去、整站冻结而 db_down 通知一条发不出"的成因（E-2/E-36）。
# 【迁移那条路径要不带它】整库复制的单条 chunk 写入可能远超 15 秒，
# 而那恰恰是"已清空目标、写到一半"最不能被打断的地方 —— 见 make_mysql_engine 的 query_timeout 形参。
MYSQL_READ_TIMEOUT = 15


def make_mysql_engine(url: str, query_timeout: bool = True):
    """建 MySQL 引擎。pool_pre_ping 必开：服务端 wait_timeout(默认 8 小时) 会掐掉空闲长连接，
    而我们的后台协程正是"长期空闲后突然要查"，不 ping 就会撞 'MySQL server has gone away'。

    为什么要收紧 connect_timeout（pymysql 默认已有 10 秒，实测确认）：
    主机【关机或被防火墙 DROP】时（拒绝连接那种是立刻返回，不算），建连接会一直挂到超时。
    而建连接是【同步】调用——页面处理器里随手一条查询就能把整个事件循环冻住这么久，
    界面、下载、qB 同步一起停。看守协程那条路径已经丢进线程池了（见 worker.run_db_watch，
    实测把界面冻结从 5 秒降到 29 毫秒），但普通查询这条路没有，只能靠超时兜底。
    5 秒对局域网/常见云库都绰绰有余，最坏卡顿也压到勉强可接受。

    【connect_timeout 只盖住握手那一段，查询本身还得有上界】(E-2/E-36，2026-09-01 拍板)
    主机连得上但库查不动时（锁等待、磁盘满、大表全扫、网络半开），查询会永久挂着——
    而"建连接有 5 秒上界"这句话被好几处当成了"最坏也就卡 5 秒"的依据，那是个错觉。
    后果最重的是停摆状态机：它靠一条 `SELECT 1` 判活，那条查询挂住就永远进不了停摆态，
    于是整站冻结而 db_down 通知一条都发不出去。

    query_timeout=False 给【迁移】那条路径用：整库复制的单条 chunk 写入可能远超 15 秒，
    而那正是"已清空目标、写到一半"最不能被打断的地方。别的调用点一律用默认值。
    """
    args = {"connect_timeout": MYSQL_CONNECT_TIMEOUT}
    if query_timeout:
        # pymysql 的 read_timeout / write_timeout 是【每次 socket 读写】的上界，
        # 不是整条语句的总时长；对"查询挂住不回包"这一类足够了，也不会误杀慢但持续有回包的查询。
        args["read_timeout"] = MYSQL_READ_TIMEOUT
        args["write_timeout"] = MYSQL_READ_TIMEOUT
    return create_engine(url, echo=False, pool_pre_ping=True, pool_recycle=3600,
                         pool_size=5, max_overflow=5, connect_args=args)


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


# ---------------- 整库维护闸（R21）----------------
#
# 【它挡的是什么】"切库"与"整库迁移"这两件事会把整个业务库换掉/清空重写，
# 而此刻后台四条循环与页面处理器仍在往同一个库写。以前的互斥只有迁移那里借用的
# `worker._poll_lock` + `_scan_lock` 两把轮次锁 —— 它们的作用域**比约束小**：
# 页面上的写入口（详情页『补齐该源』、源管理『新增源组』、『绑定 bgm』…）完全不受约束。
# 实测复现过：迁移是"先 DELETE 清空目标 → 按 500 行一批【显式带 id】INSERT"，
# 中途并发插入一条 AnimeTorrent 会占住一个即将被显式 id 写入的号 →
# `IntegrityError: UNIQUE constraint failed: animetorrent.id` → 迁移中止，
# 而目标库停在"清空 + 写了一半"的状态（实测 1200 行的源，目标只剩 501 行）。
#
# 【为什么闸装在 get_session() 上】全仓 316 处业务库访问【全部】走它，
# 而 db/transfer.py、db/schema.py、db/backup.py 用的是显式引擎、一处都不走它 ——
# 也就是说这一个点既拦得住全部业务读写，又拦不到维护自己。装在别处必然漏，
# 这个项目已经反复吃过"同一个约束只落在 N 处中的 1 处"的亏。
#
# 【为什么连"读"也一起拦】维护期间读到的是半个库（anime 全在、animetorrent 还空），
# 而集去重的 have_eps 正是按读到的东西判的 —— 读到中间态比读不到危险得多。
# 窗口是秒级的（真库 1675 行迁移 < 1 秒），页面兜底会显示"数据库维护中"。
_maintenance = ""


class DatabaseBusy(RuntimeError):
    """业务库正在做整库维护（切库 / 迁移），此刻不接受任何读写。"""


@contextmanager
def maintenance(reason: str, blocked_by=None):
    """把业务库置为"维护中"：期间 get_session() 一律拒绝，后台四条循环按停摆跳过本轮。

    reason 会原样显示给用户，写成人话（"正在迁移数据"而不是 "migrate_data"）。

    blocked_by：一个 `() -> list[str]` 的可调用（实际传的是 `core.engine.maintenance_blockers`，
    这里收成参数只为避开 db → core 的环导入）。非空就拒绝开始，内容原样带进异常。
    【为什么这道检查必须在这里、而不是在调用方】它挡的是"交付协程跨 await 持着旧库的整数主键、
    维护结束之后才回写"那一种——闸只挡得住维护【期间】的读写。调用方先查一遍再进维护的写法，
    中间隔着一个 `await confirm(...)`（用户点确认，几秒到几分钟），窗口大得能开进一整轮交付。
    放在这里，检查与置位之间没有任何 await，在事件循环这一条线上是原子的。
    """
    global _maintenance
    if _maintenance:
        raise DatabaseBusy(f"另一项数据库维护正在进行：{_maintenance}")
    if blocked_by is not None and (busy := blocked_by()):
        raise DatabaseBusy("现在不能做：" + "；".join(busy))
    _maintenance = reason
    log.warning("业务库进入维护，业务读写暂停：%s", reason)
    try:
        yield
    finally:
        _maintenance = ""
        log.info("业务库维护结束：%s", reason)


def maintenance_reason() -> str:
    """非空＝正在做整库维护，内容是给用户看的人话。"""
    return _maintenance


def data_identity(eng=None) -> str:
    """当前业务库的**身份串**——判据与 `transfer.same_database` 同口径。(R26)

    给"这一件一次性的事，在**这个业务库**上做过没有"这类标记当作用域用。

    【为什么必须带身份】那类标记（`_FINISH_BACKFILL_DONE` / `_idle_backfilled` /
    `_QB_PROGRESS_BACKFILLED`）判的对象是**业务库**（注释原文："本库【从来】没判过完结"），
    可它们存在 **meta 库**里 —— 而按双引擎设计 meta 恒留本地、**不随业务库走**。
    于是切库之后标记与库不匹配，两个方向都错：
      · 【标记跟过来了】业务库 A 跑过一轮 → meta 落 `_idle_backfilled`；切到业务库 B
        （设置页明说"切过去就是那个库里已有的内容"）→ B 从没回填过，标记却已是"做过了"，
        于是 B 里所有"最后一条种子早于 ANIME_IDLE_DAYS×4"的番首轮就发断更告警 ——
        而那批静默是切库**之前**的历史，不是刚发生的，正是这个标记存在的唯一理由。
        （端到端复现过：切库后首轮直接推 10 部。）
      · 【标记没跟过来】业务库在 MySQL、本地 meta 被恢复成一份旧备份（backup 文档化的恢复
        流程只换本地文件）→ 标记消失 → 对着一个跑了半年的库重新"首次回填"一遍。

    最小改动、零迁移：把身份拼进键名。换库自动重新回填、切回来也不会重复回填。
    """
    u = (eng or engine).url
    if u.get_backend_name() == "sqlite":
        import os
        return f"sqlite:{os.path.realpath(u.database) if u.database else ':memory:'}"
    return f"{u.get_backend_name()}:{(u.host or '').lower()}:{u.port or 3306}/{u.database}"


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
    """把【配置库】升到最新版本（`setting` 表）。**不碰业务表**。

    【为什么不在这里一并升业务库】(R21) 启动顺序是
    `init_db()` → `config.load_from_db()` → `apply_configured_backend()`（见 main.py），
    而模块级 `engine` 在头两步里恒等于 `meta_engine`（本地 SQLite）——
    配置都还没读出来，根本不知道业务数据该落在哪。
    早先这里跟着调 `upgrade_data_schema()`，于是 `DB_BACKEND=mysql` 的用户每次启动都会
    **对一个完全不用的本地 SQLite 跑一遍整条 data 迁移链**，两条真实代价：
      · data 链里有两条【改数据】的 revision（`d3f8b21c5e40` 删重复 anime_alias、
        `e5a71c0d2b93` 把重复行的 mikan_id 置空），它们跑在那份陈旧的本地业务表上——
        而 `db/backup.py` 的模块说明明写那份"可能是 MySQL 出问题后唯一剩下的番剧数据副本"。
      · 那份库上的任何迁移失败都会穿透到 main.py 的 `mark_data_fatal`（"启动初始化失败，
        系统停摆"），而真正的业务库此刻**连试都还没试过**——一个应用根本不用的数据库
        把整台机器停在那儿，日志里给的还是误导性的原因。
    现在"升哪个库"与"业务数据在哪个库"由 `apply_configured_backend()` 一处决定，永远一致。
    """
    from . import schema
    schema.upgrade(meta_engine, "meta")     # setting 表


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
    # 【维护也算"此刻不能用"】四条后台循环的把门判据只有 is_data_down() 这一条，
    # 维护并进来就等于一次覆盖全部后台线，不必去逐条改（那正是广度错误的温床）。
    return _data_fatal or _data_down or _maintenance


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


# 【"库连不上" vs "库在、只是这条语句不行"】——判据不能只看异常类。(R21)
#
# 页面兜底原本写的是 `except (OperationalError, InterfaceError): mark_data_down(...)`，
# 注释说"ProgrammingError（表不存在之类）才是 schema 问题"。**那套映射只在 MySQL 上成立。**
# 实测 SQLite：`SELECT * FROM 不存在的表` 抛的正是 `sqlalchemy.exc.OperationalError`
# （`(sqlite3.OperationalError) no such table: …`），`database is locked` 同样是它。
# 而 SQLite 是本项目的**默认**后端，于是默认配置下：
#   表缺一张（迁移半截、恢复了一份旧备份）→ 被判成"数据库连不上，系统已停摆"
#   → 四条后台循环全部按门跳过本轮、页面挂红条
#   → 而看守协程的 `SELECT 1` 在同一个文件上必然成功、把停摆解除
#   → 下一次渲染再标一次：状态来回翻转，后台随机丢轮次，诊断恒是假的。
#
# 所以这里按【错误内容】再收一道。两类要排除：
#   · schema 类（表/列不对）：库是通的，原样 500 让人看见真正的原因。
#   · 锁竞争：迁移/备份正在写库时的瞬时争用，几秒后自己就好，不该让全站进停摆状态机。
_NOT_A_CONNECTION_ERROR = (
    "no such table", "no such column", "has no column",      # SQLite schema
    "unknown column", "doesn't exist", "unknown table",      # MySQL schema
    "database is locked", "database table is locked",        # SQLite 瞬时锁竞争
    "lock wait timeout",                                     # MySQL 瞬时锁竞争
)


def looks_like_connection_error(exc: BaseException) -> bool:
    """这条异常是不是"业务库连不上/用不了"（而不是语句本身有问题）。

    调用方拿它决定要不要把全站标成停摆——标错的代价是**四条后台循环集体丢轮次**，
    所以宁可漏标（原样 500，人看得见真实原因），不要错标。
    """
    msg = str(exc).lower()
    return not any(h in msg for h in _NOT_A_CONNECTION_ERROR)


def probe_data_engine() -> str:
    """探一次业务库（SELECT 1）。通了返回空串，否则返回错误摘要并把状态标成停摆。

    从『不通』变回『通』时补跑一次 Alembic 升级：停摆期间可能漏过了版本升级——
    比如换了带新迁移的代码才启动，而那会儿库还没回来，启动时的 upgrade_data_schema 根本没跑成。
    只在状态【变化】时记日志，否则每 30s 一条会把日志刷爆。
    """
    global _data_down
    if _maintenance:
        # 维护期间探测毫无意义：引擎正被换掉/库正被清空，探出什么结论都会污染状态。
        return _maintenance
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
        # 本地 SQLite：先探通再升级——与下面 MySQL 那一支【同一个形状】。
        # （R21 之前这一支不升级，业务表是靠 init_db 顺带升的，见那里的说明。）
        if probe_data_engine():
            return f"停摆 — {data_target_desc()}：{_data_down}"
        upgrade_data_schema()
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
        # 【必须与 _make_sqlite_engine 走同一个工厂】否则这条分支建出来的引擎没有 WAL，
        # 与默认业务库的行为不一致——见 _sqlite_engine 的说明。
        return _sqlite_engine(url)
    return make_mysql_engine(url)


def switch_data_engine(url: str | None) -> None:
    """把业务数据引擎切到 url（None=切回本地 SQLite）。【只切连接，不搬数据】。

    切换点的安全性：`with get_session()` 块内都没有 await，所以**在事件循环这一条线上**
    它们是原子的，从 UI 处理器里切不会切在半个事务中间。
    【但"单进程 asyncio ⇒ 全都原子"这个说法不准，别照抄】数据库并非只被事件循环访问。
    E-12（2026-09-01 已拍板）把 `/api/qb/done` 从同步路由改成了 `async def`，
    **唯一一条会与交付路径交错的线程写库路径由此消失**；剩下的线程路径是这几条：
      · `asyncio.to_thread(db.probe_data_engine)`（看守协程 + 页面『立即重连』）—— 只读 SELECT 1
      · `asyncio.to_thread(backup.auto_tick)` —— 只读（VACUUM INTO 到另一个文件）
      · `run.io_bound(db.switch_data_engine / transfer.migrate_data / ...)`（设置页）—— 写，
        但这几条都要用户点按钮才发生，且它们本身就是"把整个库换掉"的操作，
        与"交付路径的读改写"并发本来就没有正确解，靠的是用户不会一边迁移一边下载
      · `asyncio.to_thread(worker.init_business_state, ...)`（页面『立即重连』）—— **写**。
        它里面的 `reset_downloading()` 会无条件把 downloading 打回 pending，
        与事件循环里正在交付的行有一个窗口（见 pages/layout.py `_db_reconnect` 的说明：
        窗口从看守协程的 30 秒缩到了几毫秒，但不是零）。
    所以这段话准确的说法是：**交付路径的原子性只在事件循环内部成立**，
    上面第四条是已知且已缩到最小的例外。

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
    """业务数据会话（anime / movie / 种子 / 源组）。

    整库维护（切库 / 迁移）期间抛 `DatabaseBusy` —— 理由见 `maintenance()` 上面那段。
    """
    if _maintenance:
        raise DatabaseBusy(f"数据库维护中（{_maintenance}），请稍候再试")
    return Session(engine)


def get_meta_session() -> Session:
    """配置会话（setting 表，恒为本地 SQLite，与业务库切换无关）。"""
    return Session(meta_engine)
