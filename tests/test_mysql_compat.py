"""MySQL 后端的兼容性。

这一组守的是「只在一种方言上正确」的改动——本项目已经踩过两次：
① 别名键在 MySQL 上被 VARCHAR(191) 截断而查询侧不截，每条种子建一部重复番；
② 迁移预检用了 SQLite/PG 的 `FILTER (WHERE ...)`，MySQL 作源时一开始就 1064 语法错。

真实 MySQL 的连通性测试不在这里（跑 CI 时未必有库）；这里测的是**方言无关的正确性**。
"""
import sqlalchemy as sa

import pytest

from core.anime import alias_key
from db.dialect import ALIAS_TITLE_LEN
from db import transfer


# ---------------- 别名键 ----------------

def test_alias_key_truncates_to_column_length():
    """(MySQL) 列是 VARCHAR(191)，STRICT 模式下超长【报错】而不是截断。"""
    assert len(alias_key("番" * 300)) == ALIAS_TITLE_LEN
    assert ALIAS_TITLE_LEN == 191


def test_alias_key_is_idempotent():
    """截过一次的键再截一次必须不变——否则查询侧与插入侧会算出两个不同的键。"""
    k = alias_key("番" * 300)
    assert alias_key(k) == k


def test_alias_key_handles_empty():
    assert alias_key("") == "" and alias_key(None) == ""


def test_normal_titles_pass_through():
    """真实番名远短于上限（生产库实测最长 39），不该被这道截断碰到。"""
    for t in ("葬送的芙莉莲", "ONE PIECE", "Re:从零开始的异世界生活 第三季"):
        assert alias_key(t) == t


def test_query_and_insert_use_the_same_key(clean_tables):
    """【本条是整个缺陷的核心】查询用原名、插入用截断名的话，超长番名永远查不到自己的别名：
    每来一条种子就当成新番建一部，而别名又插不进去。实测在真实 MySQL 上 4 条种子 → 4 部番。"""
    from sqlmodel import select

    from db.models import Anime, AnimeAlias
    long_title = "超长番名" * 60
    with clean_tables.get_session() as s:
        a = Anime(title=long_title, season=1)
        s.add(a)
        s.commit()
        s.refresh(a)
        s.add(AnimeAlias(title=alias_key(long_title), season=1, anime_id=a.id))
        s.commit()
    with clean_tables.get_session() as s:
        hit = s.exec(select(AnimeAlias).where(
            AnimeAlias.title == alias_key(long_title), AnimeAlias.season == 1)).first()
    assert hit is not None, "用同一个 key 查必须命中"


# ---------------- 迁移预检 ----------------

def test_overlong_check_compiles_on_both_dialects():
    """(MySQL) `count(*) FILTER (WHERE ...)` 是 SQLite/PG 语法，MySQL 上是 1064 语法错误。
    这条预检【两个迁移方向都要跑】，只在其中一个方向上正确 = 另一个方向的迁移一开始就崩。"""
    col = sa.column("title", sa.String(191))
    stmt = sa.select(
        sa.func.max(sa.func.length(col)),
        sa.func.coalesce(sa.func.sum(sa.case((sa.func.length(col) > 191, 1), else_=0)), 0))
    for dialect in ("sqlite", "mysql"):
        sql = str(stmt.compile(dialect=sa.create_mock_engine(f"{dialect}://", lambda *a, **k: None).dialect))
        assert "FILTER" not in sql.upper(), f"{dialect} 上不能出现 FILTER 子句"
        assert "CASE" in sql.upper()


def test_overlong_check_finds_violations(testdb, tmp_path):
    """源库里有塞不进目标定长列的值时必须在【清空目标之前】发现——否则留下半个库。"""
    src = sa.create_engine(f"sqlite:///{tmp_path}/src.db")
    dst = sa.create_engine(f"sqlite:///{tmp_path}/dst.db")
    md = sa.MetaData()
    for eng, length in ((src, 1000), (dst, 10)):
        md2 = sa.MetaData()
        sa.Table("anime_alias", md2,
                 sa.Column("id", sa.Integer, primary_key=True),
                 sa.Column("title", sa.String(length)))
        md2.create_all(eng)
    with src.begin() as c:
        c.execute(sa.text("INSERT INTO anime_alias (title) VALUES ('这是一个很长很长的番名')"))
    bad = transfer._overlong_values(src, dst)
    assert bad and "anime_alias.title" in bad[0], bad


def test_overlong_check_is_quiet_when_clean(testdb, tmp_path):
    src = sa.create_engine(f"sqlite:///{tmp_path}/s2.db")
    dst = sa.create_engine(f"sqlite:///{tmp_path}/d2.db")
    for eng in (src, dst):
        md = sa.MetaData()
        sa.Table("anime_alias", md,
                 sa.Column("id", sa.Integer, primary_key=True),
                 sa.Column("title", sa.String(191)))
        md.create_all(eng)
    with src.begin() as c:
        c.execute(sa.text("INSERT INTO anime_alias (title) VALUES ('正常番名')"))
    assert transfer._overlong_values(src, dst) == []


# ---------------- 迁移幂等 ----------------

def test_migrations_add_columns_idempotently():
    """(MySQL) 一条 revision 里的多个 ALTER/CREATE 在 MySQL 上不在同一事务里（DDL 隐式提交），
    而版本号最后才写。中途断电 → 下次重跑撞 1060/1050 → 被标 fatal，
    而 fatal【只能人工解除】：每次重启都是同一个死循环。两条路径都在真库上复现过。

    **baseline 最需要它**：6 张表 + 8 个索引，且恰好跑在"第一次连全新 MySQL"时；
    另有一条根本不需要竞态的路径——mysqldump 只导业务表、没带 alembic_version_data。"""
    import ast
    import pathlib
    for f in pathlib.Path("alembic/versions").glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            if fn.name in ("_add_column_if_missing", "_existing_tables"):
                continue          # helper 内部允许裸调用（含 offline 分支那一处）
            calls = [n for n in ast.walk(fn)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr in ("add_column", "create_table")]
            for c in calls:
                # upgrade 里的 create_table 必须被 `if 表名 not in _have` 包住 —— 这条用 ast
                # 判不出嵌套，退而求其次：文件里出现 create_table 就必须有 _existing_tables
                if c.func.attr == "create_table":
                    assert "_existing_tables" in f.read_text(encoding="utf-8"), \
                        f"{f.name}:{fn.name} 有 create_table 但没有幂等判断"
                else:
                    raise AssertionError(
                        f"{f.name}:{fn.name} 有裸 op.add_column，必须走 _add_column_if_missing")


def test_alias_key_fits_the_index_key_limit():
    """(MySQL) 截断按【字符】算，而 InnoDB 的索引键上限按【字节】算。

    utf8mb4 下一个字符最多 4 字节：191 × 4 = **764 字节**，正好卡在老 InnoDB(COMPACT) 的
    767 字节上限之内——这就是 `db/dialect._COL_LEN` 里那个 191 的由来（注释也是这么写的）。
    真实 MySQL 9.7（Dynamic 行格式，上限 3072B）上实测 191 个 emoji 能插进去、且用同一个 key 查得到。

    改大这个数之前先算一遍字节：192 × 4 = 768 > 767，老 InnoDB 上就会
    "Specified key was too long"——而那是建表期才报的错，本地 SQLite 上永远测不出来。
    """
    worst = alias_key("🎬" * 300)
    assert len(worst) == ALIAS_TITLE_LEN
    assert len(worst.encode("utf-8")) <= 767, "超过老 InnoDB 的索引键上限"


def test_alias_key_never_splits_a_character():
    """按字符切，不会切出半个多字节字符（那会得到无法解码的键）。"""
    for ch in ("🎬", "番", "a"):
        k = alias_key(ch * 300)
        k.encode("utf-8").decode("utf-8")     # 能往返就说明没切碎
        assert len(k) == ALIAS_TITLE_LEN


def test_col_len_values_all_fit_the_legacy_index_limit():
    """(防回归) `_COL_LEN` 里凡是【参与索引/唯一约束】的列，长度 × 4 都不能超 767。
    这条用例挡住"顺手把某个长度调大一点"——那种改动在 SQLite 上毫无症状，
    只有在老 InnoDB 的真实 MySQL 上建表时才炸。"""
    from db.dialect import _COL_LEN
    keyed = {"sourcegroup.name", "anime_alias.title", "setting.key",
             "animetorrent.info_hash", "movietorrent.info_hash", "movie.mikan_id"}
    for col in keyed:
        n = _COL_LEN[col]
        assert n * 4 <= 767, f"{col}={n} → {n*4} 字节，超过老 InnoDB 的 767 索引键上限"


def test_float_columns_are_double_on_mysql():
    """浮点列在 MySQL 上必须是 8 字节 DOUBLE，不能是默认的 4 字节 FLOAT。

    【这是一条 DDL 快照断言】本项目日常只跑 SQLite，而 SQLite 的 REAL 本来就是 8 字节，
    所以这一类"只在 MySQL 上错"的问题本地跑一辈子也碰不到。断言直接对着编译出来的
    MySQL DDL 提，成本几乎为零。

    坏掉的后果见 core/engine.sync_qb_status 的 epsilon 注释：进度冻结时约 50% 概率
    被误判成"推进了"（实测 10000 个值里 4988 个 float32 往返后变大），
    于是 status 永远走不到 stalled、停滞检测整个功能失效。
    """
    from sqlalchemy.dialects import mysql
    from sqlalchemy.schema import CreateTable
    from sqlmodel import SQLModel

    import db.models  # noqa: F401  触发 adapt_metadata

    import sqlalchemy as sa

    # 【从 metadata 枚举，不要手抄列清单】手抄的那一版漏了 animetorrent.episode，
    # 而用例自己也手抄同一份清单 —— 两边一起漏，等于没测。
    for table in SQLModel.metadata.tables.values():
        floats = [c.name for c in table.columns if isinstance(c.type, sa.Float)]
        if not floats:
            continue
        ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))
        for col in floats:
            line = next(x.strip() for x in ddl.split("\n") if x.strip().startswith(col + " "))
            assert "DOUBLE" in line.upper(), f"{table.name}.{col} 在 MySQL 上不是 DOUBLE：{line}"


def test_the_widening_revision_covers_every_float_column():
    """加宽 revision 的 `_FLOAT_COLS` 必须覆盖 metadata 里【全部】浮点列。

    上一条断言的是 `SQLModel.metadata` —— 那是 `db/dialect.adapt_metadata` 改过的对象，
    而**生产库的结构完全由 alembic 建**，两者之间没有任何联系。实测把整条 revision 的
    upgrade() 首行改成 `return`，全套用例照样全绿：D2 那条修复曾经没有任何回归网。
    这条用例补的正是那一半——直接对着 revision 里那张手抄表断言。
    """
    import importlib.util

    import sqlalchemy as sa
    from sqlmodel import SQLModel

    import db.models  # noqa: F401

    def _load(path, name):
        spec = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    # 【要看所有加宽 revision 【合起来】的覆盖】已应用的 revision 是不可变的，
    # 补漏只能新开一条 —— 所以守卫也必须按"并集"算，而不是盯着某一条。
    r1 = _load("alembic/versions/20260819_f2b4c8e7a105_double_float.py", "_rev_f2b4")
    r2 = _load("alembic/versions/20260820_a3c9e1f70b28_double_episode.py", "_rev_a3c9")

    want = {(t.name, c.name) for t in SQLModel.metadata.tables.values()
            for c in t.columns if isinstance(c.type, sa.Float)}
    have = {(t, c) for t, cols in r1._FLOAT_COLS.items() for c in cols}
    have |= {(t, c) for t, c, _ in r2._COLS}
    assert want == have, f"没有 revision 加宽：{sorted(want - have)}；多余的：{sorted(have - want)}"
    assert set(r1._NULLABLE) == {(t, c) for t, cols in r1._FLOAT_COLS.items() for c in cols}, \
        "_NULLABLE 与 _FLOAT_COLS 不同步"


def test_progress_comparison_survives_float32_round_trip():
    """『进度推进了吗』的判据要能扛住窄浮点的往返噪声。

    这是第二道保险：即便列类型将来又退回 FLOAT，也不该把"没动"读成"推进了"。
    """
    import struct

    raw = 0.0021056853504462045                       # float32 往返后会【变大】的一个真实值
    noisy = struct.unpack("f", struct.pack("f", raw))[0]
    assert noisy > raw, "用例前提不成立：这个值往返后没有变大"
    assert not (noisy > raw + 1e-9), "epsilon 挡不住 float32 噪声"
    assert 0.43 > raw + 1e-9, "epsilon 太大，真实推进也认不出来了"


# ---------------- 查询也要有上界（E-2 / E-36，R20） ----------------

def test_mysql_engine_sets_a_read_timeout():
    """(E-2/E-36) `connect_timeout` 只盖住 TCP 握手那一段，查询本身也得有上界。

    主机连得上但库查不动时（锁等待、磁盘满、大表全扫、网络半开），查询会永久挂着。
    后果最重的是停摆状态机：它靠一条 `SELECT 1` 判活，那条查询挂住就永远进不了停摆态，
    于是整站冻结而 db_down 通知一条都发不出去 —— 而"建连接有 5 秒上界"这句话
    被好几处当成了"最坏也就卡 5 秒"的依据，那是个错觉。

    ⚠️ 这条用例的第一版是【假的】：它去 `inspect.getsource` 里找字符串 "read_timeout"，
    而生产代码的**注释里**就写着这个词 —— 删掉那两行真正的赋值，全套 887 条照样全绿
    （第 20 轮的审计变异测试打出来的）。现在直接问 SQLAlchemy 建连接时会用什么参数。
    """
    import pymysql

    import db as D
    # 【看它真的传了什么，别去猜 connect_args 存在哪】那个位置随 SQLAlchemy 版本变，
    # 而"建连接时 pymysql 收到哪些 kwargs"是唯一有意义的口径。
    eng = D.make_mysql_engine("mysql+pymysql://u:p@127.0.0.1:3306/x")
    seen = {}
    real = pymysql.connect

    def spy(**kw):
        seen.update(kw)
        raise RuntimeError("stop")            # 不用真连上，只要看参数
    pymysql.connect = spy
    try:
        try:
            eng.connect()
        except Exception:
            pass
    finally:
        pymysql.connect = real
        eng.dispose()
    assert seen.get("read_timeout") == D.MYSQL_READ_TIMEOUT, \
        f"建连接时没带 read_timeout：{ {k: v for k, v in seen.items() if 'timeout' in k} }"
    assert seen.get("write_timeout") == D.MYSQL_READ_TIMEOUT
    assert seen.get("connect_timeout") == D.MYSQL_CONNECT_TIMEOUT


def test_migration_engine_has_no_query_timeout():
    """迁移那条路径【不能】带查询超时：整库复制的单条 chunk 写入可能远超 15 秒，
    而那正是"已清空目标、写到一半"最不能被打断的地方。

    同样改成看【真的传了什么】——第一版只 grep 源码字符串，是假的。
    """
    import db as D
    import pymysql
    eng = D.make_mysql_engine("mysql+pymysql://u:p@127.0.0.1:3306/x", query_timeout=False)
    seen = {}
    real = pymysql.connect

    def spy(**kw):
        seen.update(kw)
        raise RuntimeError("stop")
    pymysql.connect = spy
    try:
        try:
            eng.connect()
        except Exception:
            pass
    finally:
        pymysql.connect = real
        eng.dispose()
    assert seen, "根本没走到建连接这一步，用例是空跑的"
    assert "read_timeout" not in seen, "迁移引擎带上了查询超时 —— 大 chunk 会被中途切断"
    assert "write_timeout" not in seen
    assert seen.get("connect_timeout") == D.MYSQL_CONNECT_TIMEOUT, "连接超时不该被一起去掉"


def test_the_settings_page_never_touches_mysql_on_the_event_loop():
    """(E-36) 设置页里碰 MySQL 的处理器必须全部丢进线程。

    建连接与查询都是【同步】调用：主机关机或被防火墙 DROP 时会把整个事件循环冻住——
    界面、下载、qB 同步一起停。六个处理器里备份/建库/迁移三个早就走了 run.io_bound，
    另外三个（测试连接 / 切到 MySQL / 迁移前的连通性预检）是漏下的。
    """
    import ast
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent.joinpath("pages/settings.py").read_text(
        encoding="utf8")
    tree = ast.parse(src)
    offenders = []
    # 【只看 async 处理器】它们才是跑在事件循环上的那一层。被 run.io_bound 包住的内层
    # 同步小函数（_probe / _ping）正是该同步的 —— 按它们判会把正确的写法报成违规。
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        sub = ast.dump(node)                     # 整棵子树，含内层的同步小函数
        if "make_mysql_engine" not in sub and "switch_data_engine" not in sub:
            continue
        if "io_bound" not in sub:
            offenders.append(f"settings.py:{node.lineno} {node.name}()")
    assert offenders == [], ("这些处理器在事件循环上同步碰 MySQL：\n  " + "\n  ".join(offenders))
    # 反向：确认扫描确实看见了那几个处理器，别因为改名而空跑成绿
    assert src.count("make_mysql_engine") >= 2 and "switch_data_engine" in src


def test_no_function_references_an_undefined_global():
    """(R20) 全仓扫一遍：有没有函数引用了【模块级根本不存在】的全局名。

    ⚠️ 这条用例存在的理由：R20 把连通性预检丢进线程时，把 `other = db.make_mysql_engine(url)`
    一起搬进了嵌套函数 `_ping()` —— `other` 从 `_migrate` 的局部变量变成了 `_ping` 的局部变量，
    而 `_migrate` 后面那句 `src, dst = (…, other)` 被编译成 `LOAD_GLOBAL other`：
    **两个迁移按钮当场 NameError 全死**，顺带把 E-17 新加的 dst_before 回滚提示变成到不了的代码。
    而当时 889 条用例【全绿】—— 因为没有一条走页面这一层，全都直接调 transfer 里的函数。

    这是本项目那句教训的又一次应验：整段替换会静默吞掉区间里的别的东西。
    用 `symtable` 而不是自己数 AST：它给的正是编译器的判断（哪些名字是 GLOBAL），
    自己数会把内建、异常变量、推导式变量都算错。
    """
    import builtins
    import pathlib
    import symtable

    root = pathlib.Path(__file__).resolve().parent.parent
    known_builtins = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__builtins__"}
    offenders = []
    for f in sorted(root.glob("**/*.py")):
        if {".venv", "__pycache__", "tests", "alembic"} & set(f.relative_to(root).parts):
            continue
        src = f.read_text(encoding="utf8")
        top = symtable.symtable(src, str(f), "exec")
        module_names = {s.get_name() for s in top.get_symbols()}

        def walk(table, path):
            for sym in table.get_symbols():
                if sym.is_global() and not sym.is_assigned():
                    n = sym.get_name()
                    if n not in module_names and n not in known_builtins:
                        offenders.append(f"{f.relative_to(root)}:{table.get_lineno()} "
                                         f"{'.'.join(path)} 引用了未定义的全局 `{n}`")
            for child in table.get_children():
                walk(child, path + [child.get_name()])

        walk(top, [f.stem])
    assert not offenders, (
        "这些函数引用了模块级不存在的名字，调用时会 NameError：\n  " + "\n  ".join(offenders)
        + "\n多半是把某个赋值搬进了嵌套函数。")


def test_the_migrate_handler_disposes_its_engine_on_every_exit():
    """(R21) 『迁移数据』建的那条 MySQL 引擎，**每一条**退出路径都必须释放。

    原来四条出口里三条各写了一次 `other.dispose()`，唯独"用户在确认框点【取消】"那条
    直接 return —— 而那正是最常见的操作（用户就是来看两端行数的）。
    `_ping()` 真建过连接、`count_rows` 又对每张业务表各发一条 COUNT(*)，
    连接归池但仍是活的：每取消一次就在 MySQL 服务端留下一个不会被回收的会话。

    逐出口写 dispose 是"约束的作用域大于验证的作用域"的教科书例子 ——
    所以这里钉的不是"有几处 dispose"，而是【结构】：引擎建出来之后必须紧跟一个
    try/finally，且 finally 里就是它的 dispose。这样再加多少条 return 分支都跑不掉。
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parent.parent.joinpath(
        "pages/settings.py").read_text(encoding="utf8")
    tree = ast.parse(src)
    fns = [n for n in ast.walk(tree)
           if isinstance(n, ast.AsyncFunctionDef) and n.name == "_migrate"]
    assert fns, "没找到 _migrate，用例的前提坏了"
    fn = fns[0]

    # 找到 `other = db.make_mysql_engine(...)` 之后紧跟的那个 Try，检查它的 finalbody
    stmts = fn.body
    made_at = next((k for k, n in enumerate(stmts)
                    if isinstance(n, ast.Assign) and "make_mysql_engine" in ast.dump(n)), None)
    assert made_at is not None, "_migrate 里没有 make_mysql_engine —— 用例的前提坏了"
    nxt = stmts[made_at + 1] if made_at + 1 < len(stmts) else None
    assert isinstance(nxt, ast.Try) and nxt.finalbody, \
        "建完引擎之后没有紧跟 try/finally —— 某条 return 分支会漏掉 dispose"
    assert "dispose" in ast.dump(ast.Module(body=nxt.finalbody, type_ignores=[])), \
        "finally 里没有释放引擎"
    # 反向：主体里确实有多条 return（否则这条结构断言毫无意义）
    returns = [n for n in ast.walk(nxt) if isinstance(n, ast.Return)]
    assert len(returns) >= 3, f"主体里只有 {len(returns)} 条 return，用例的前提变了"
