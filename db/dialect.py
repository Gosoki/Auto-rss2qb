"""跨方言适配：让同一套 SQLModel 模型既能落 SQLite 也能落 MySQL。

模型是照 SQLite 写的（`str` 不带长度、靠 partial index 提速、迁移里用 `?` 占位符），
直接拿去 MySQL 会在建表那一步就炸。这里把差异收在一处，别散进各个模型和迁移函数：

  1. 【无长度 VARCHAR】SQLAlchemy 对 MySQL 会直接抛
     "VARCHAR requires a length on dialect mysql"。所以给每个 str 列定型：
     · 参与主键/索引/唯一约束的 → VARCHAR(n)（MySQL 的索引键必须有确定长度）
     · 其余自由文本            → TEXT（简介可到 917 字符，塞 VARCHAR 会顶到 MySQL 单行 65535 字节上限）
     两种改法对 SQLite 都是无害的（它本来就不看 VARCHAR 长度、TEXT 与 VARCHAR 同一亲和类型）。
  2. 【DATETIME 精度】MySQL 的 DATETIME 默认只到秒，而 created_at 要用来排序、
     同一秒入库的多条会并列成随机序（『新入库』表看着像乱跳）。用 with_variant 给 MySQL 指定 fsp=6。
  3. 【标识符引用与参数占位符】SQLite 用 "x" 和 ?，MySQL 用 `x` 和 %s。迁移语句一律走
     preparer.quote() + SQLAlchemy 具名参数，别再手拼。
  4. 【partial index】`CREATE INDEX ... WHERE ...` 是 SQLite/PG 的特性，MySQL 不支持，
     由调用方按方言跳过（见 db.__init__._migrate_inflight_indexes）。

改模型时要注意的唯一一条：新加的 str 列如果要建索引/唯一约束，长度会自动按 _KEYED_LEN 给；
需要更长就往 _COL_LEN 里加一条，别在 MySQL 上撞 767/3072 字节的索引键上限。
"""
import logging
import re

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

log = logging.getLogger("autorss")

# MySQL 侧字符串列一律用【二进制排序规则】，与 SQLite 默认的 BINARY 对齐。
#
# 为什么不能用 utf8mb4_unicode_ci / utf8mb4_0900_ai_ci 这类默认值：它们做 Unicode 等价折叠，
# 会把 'Ⅱ'(U+2161) 判成等于 'II'、全角 '－'(U+FF0D) 判成等于 '-'、大小写也不分。后果有两层：
#   ① 迁移当场失败——生产库里真有这么两条 alias（id 37『ClevatessⅡ－…』与 id 82『ClevatessII-…』），
#      在 SQLite 里是两行，插进 MySQL 会撞 uq_alias_title_season 报 Duplicate entry；
#   ② 更隐蔽的是运行时：`AnimeAlias.title == 解析名` 的等值查找会变成松散匹配，
#      两部标题只差全角/罗马数字的番会被认成同一部，番名对照表整个失去精度。
# 集去重、番名对照、info_hash 这些键的语义本来就是"逐字节相同才算同一个"，必须用二进制规则。
#
# 选 utf8mb4_bin 而不是 utf8mb4_0900_bin：前者 MySQL 5.7/8/9 与 MariaDB 都有，兼容面广。
# 唯一与 SQLite 的差异是它属 PAD SPACE（比较时忽略尾部空格，'a' == 'a '）；本项目所有字符串键
# 要么是 hex 哈希、要么是 strip 过的解析名，撞不到这个边角，且"源组名多打个空格算重名"反而更合理。
BINARY_COLLATION = "utf8mb4_bin"

# 参与索引/唯一约束/主键的 str 列的默认 VARCHAR 长度。
# 255×4 字节 = 1020，稳在现代 InnoDB(DYNAMIC 行格式) 3072 字节的索引键上限内；
# 老 InnoDB(COMPACT) 上限 767 字节则需要 191——真撞上了就往 _COL_LEN 里单独调小。
_KEYED_LEN = 255

# 个别列的显式长度（覆盖 _KEYED_LEN）。实测生产数据：info_hash 恒 40，源组名 ≤9。
_COL_LEN = {
    "animetorrent.info_hash": 64,
    "movietorrent.info_hash": 64,
    "movie.mikan_id": 64,
    "sourcegroup.name": 191,     # 唯一约束 + 索引，取保守值，老 InnoDB 也进得去
    "anime_alias.title": 191,    # 与 season 组成复合唯一约束，两列合计要留余量
    "setting.key": 191,          # 主键；虽然 setting 永远留在本地 SQLite，仍一并定型免得将来踩坑
    # 下面两条不是索引列，是为了【躲开 MySQL 的 TEXT 不能有 DEFAULT】（错误 1101）：
    # fail_reason 是 NOT NULL DEFAULT ''，落成 TEXT 时 ALTER TABLE 当场报错（SQLite 不报，只有真
    # MySQL 拦得住）。写入侧本来就 reason[:300]，定长 VARCHAR(300) 正好对上，没有截断风险。
    "animetorrent.fail_reason": 300,
    "movietorrent.fail_reason": 300,
}

# 【写入侧要用到的列长】把上面那张表暴露出去：番名对照的键必须在【查询与插入两侧】按同一长度截断，
# 否则超长番名（畸形标题解析出来的）会走进一条静默的死路——见 core.anime.alias_key。
ALIAS_TITLE_LEN = _COL_LEN["anime_alias.title"]


def _keyed_columns(table) -> set:
    """该表里参与主键/索引/唯一约束的列名——MySQL 要求这些列的类型必须有确定长度。"""
    names = set(table.primary_key.columns.keys())
    for idx in table.indexes:
        names |= {c.name for c in idx.columns}
    for con in table.constraints:
        if isinstance(con, (sa.UniqueConstraint, sa.PrimaryKeyConstraint)):
            names |= {c.name for c in con.columns}
    return names


def _is_unlen_string(col) -> bool:
    """这一列是不是"没有指定长度的字符串"（尚未定型）。

    SQLModel 用的不是 sa.String 而是它自己的 AutoString——一个包着 String 的 TypeDecorator。
    对它 isinstance(col.type, sa.String) 是 False，col.type.python_type 还会抛 NotImplementedError，
    所以两条路都不能用；看内层 impl_instance 才准。
    已经是 Text 的要排除，否则第二次调用会把 Text 又变回 VARCHAR，破坏幂等。
    """
    inner = getattr(col.type, "impl_instance", None) or col.type
    return (isinstance(inner, sa.String) and not isinstance(inner, sa.Text)
            and getattr(inner, "length", None) is None)


def adapt_metadata(metadata) -> None:
    """就地给 metadata 里所有表的列定型，使其同时对 SQLite 与 MySQL 合法。

    【必须在 models 导入之后、任何 create_all/DDL 之前调】——metadata 是运行时填充的，
    在 models 还没 import 时它是空的，那时调等于什么都没做。

    幂等：重复调用结果相同（定过型的列 length 已非 None，不会再被处理）。
    对 SQLite 生成的 DDL 与改造前等价——SQLite 忽略 VARCHAR 长度，TEXT/VARCHAR 同属 TEXT
    亲和类型，既有数据和查询都不受影响。

    为什么不能直接靠 SQLModel 的 AutoString：它对 MySQL 的兜底是【一律 VARCHAR(255)】
    （AutoString.mysql_default_length），而 anime.summary 实测已有 917 字符的行——
    MySQL 严格模式下插入会直接报 "Data too long for column"，非严格模式则静默截断丢数据。
    """
    for table in metadata.tables.values():
        keyed = _keyed_columns(table)
        for col in table.columns:
            if isinstance(col.type, sa.DateTime):
                # 用 variant：SQLite 那边仍是原来的 DATETIME，只有 MySQL 拿到 fsp=6。
                # 不给精度的话 MySQL 的 DATETIME 只到秒，同一秒入库的多行 created_at 会并列，
                # 『新入库』表按它排序就会出现随机顺序。
                col.type = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
            elif type(col.type) is sa.Integer and not col.primary_key:
                # 【非主键整数一律 BIGINT】SQLite 的 INTEGER 本来就是 64 位，而 MySQL 的 INT 只有
                # 4 字节（上限 2147483647 ≈ 2 GiB）。qb_size 存的是字节数，生产数据里最大的一条
                # 已经 1.47 GiB、占掉上限 74%——第一个 ≥2 GiB 的种子（BDRip 单集、剧场版常规 3~10GB）
                # 就会在 STRICT 模式下抛 1264：迁移当场中断留下半个库；运行时更糟，
                # sync_qb_status 整轮共用一个 session，一条超限就把同批【所有】种子的进度更新
                # 一起回滚，而该行因进度写不上去永远留在 in-flight，每轮复发且界面零报错。
                # 用 variant：SQLite 侧 BIGINT 仍是 INTEGER，DDL 等价、零行为变化。
                col.type = sa.BigInteger().with_variant(mysql.BIGINT(), "mysql")
            elif _is_unlen_string(col):
                key = f"{table.name}.{col.name}"
                if col.name in keyed or key in _COL_LEN:
                    n = _COL_LEN.get(key, _KEYED_LEN)
                    col.type = sa.String(n).with_variant(
                        mysql.VARCHAR(n, collation=BINARY_COLLATION), "mysql")
                else:
                    col.type = sa.Text().with_variant(       # 自由文本（简介/声优/原始标题…）不设上限
                        mysql.TEXT(collation=BINARY_COLLATION), "mysql")


def quote(engine, name: str) -> str:
    """按当前方言正确引用标识符（SQLite 用 "x"，MySQL 用 `x`）。"""
    return engine.dialect.identifier_preparer.quote(name)


def is_sqlite(engine) -> bool:
    return engine.dialect.name == "sqlite"


def is_mysql(engine) -> bool:
    return engine.dialect.name in ("mysql", "mariadb")


def _mysql_url(host: str, port: int, user: str, password: str,
               database: str, charset: str) -> str:
    """统一的连接串构造。

    【不再手拼查询串】交给 sa.engine.URL.create：它会正确转义每一段，
    query 里的值也不会被当成分隔符。以前 charset 与 host 是直接 f-string 拼进去的，
    能访问设置页的人在『字符集』栏填 `utf8mb4&local_infile=1` 就能让 `&` 从值变成参数分隔符，
    多出来的键被 SQLAlchemy 当 pymysql 连接参数透传——配合同一表单里可控的『MySQL 地址』
    指向恶意服务端，就是经典的 rogue-server LOAD DATA LOCAL 任意文件外带；
    填 `read_default_file=/etc/passwd` 还能把该文件首行经报错弹回页面。
    charset 另外过一遍白名单（双保险：万一将来有人绕开本函数手拼）。
    """
    import sqlalchemy as _sa
    cs = charset if valid_charset(charset) else "utf8mb4"
    return _sa.engine.URL.create(
        "mysql+pymysql", username=user or None, password=password or None,
        host=(host or "").strip() or None, port=int(port) if port else None,
        database=database or None, query={"charset": cs},
    ).render_as_string(hide_password=False)


def mysql_url(host: str, port: int, user: str, password: str, database: str,
              charset: str = "utf8mb4") -> str:
    """连到某个具体库。"""
    return _mysql_url(host, port, user, password, database, charset)


def mysql_server_url(host: str, port: int, user: str, password: str,
                     charset: str = "utf8mb4") -> str:
    """连到 MySQL【服务器】而不进具体库——建库时用（库还不存在，带库名连不上）。"""
    return _mysql_url(host, port, user, password, "", charset)


# 库名/字符集只能拼进 SQL（CREATE DATABASE 的标识符不接受绑定参数），故白名单收死。
# MySQL 库名上限 64 字符；只放行字母数字下划线，反引号、分号、空格、注释符一概进不来。
_IDENT_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_CHARSET_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")


def valid_db_name(name: str) -> bool:
    return bool(_IDENT_RE.match(name or ""))


def valid_charset(cs: str) -> bool:
    return bool(_CHARSET_RE.match(cs or ""))


def driver_missing() -> str:
    """MySQL 驱动缺失时返回提示文案，可用时返回空串。"""
    try:
        import pymysql  # noqa: F401
        return ""
    except ImportError:
        return "未安装 MySQL 驱动，请先在项目环境执行：pip install pymysql"
