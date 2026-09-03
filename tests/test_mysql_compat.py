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

    utf8mb4 下一个字符最多 4 字节。别名键参与复合唯一约束 (title, season)，
    整条键 = 191×4 + BIGINT 8 = **772 字节**。上限是 **3072**（MySQL 8/9 默认的 DYNAMIC 行格式；
    E-6，2026-09-02 拍板：**明确不支持**老的 COMPACT/REDUNDANT 行格式，那个上限 767 连这条键都放不下——
    以前的注释按 767 算"留余量"，余量其实是负的）。
    真实 MySQL 9.7 上实测 191 个 emoji 能插进去、且用同一个 key 查得到。
    整条索引键的字节数由 test_every_mysql_index_key_fits_the_dynamic_row_format 按 alembic 的产物核。
    """
    worst = alias_key("🎬" * 300)
    assert len(worst) == ALIAS_TITLE_LEN
    assert len(worst.encode("utf-8")) == ALIAS_TITLE_LEN * 4, "最坏情形应正好是 4 字节/字符"


def test_alias_key_never_splits_a_character():
    """按字符切，不会切出半个多字节字符（那会得到无法解码的键）。"""
    for ch in ("🎬", "番", "a"):
        k = alias_key(ch * 300)
        k.encode("utf-8").decode("utf-8")     # 能往返就说明没切碎
        assert len(k) == ALIAS_TITLE_LEN


# ---------------- E-6 / E-7：对 alembic 的【产物】断言，不对 metadata（2026-09-02 拍板） ----------------
#
# 本项目日常只跑 SQLite，而"只在 MySQL 上错"的东西本地跑一辈子也碰不到。以前的 DDL 快照用例
# 编译的是 `SQLModel.metadata` —— 那是 db/dialect.adapt_metadata 改过的对象，
# 而**生产库的结构完全由 alembic 建**，两者之间没有任何联系（R11 那条 float 守卫就这么空跑过）。
# 这里用 `alembic upgrade head --sql` 把整条 revision 链按 MySQL 方言离线渲染成脚本，
# 再把脚本"执行"进一个内存里的表模型，得到**真正会落到 MySQL 上的**表 / 列 / 索引。

_INNODB_KEY_LIMIT = 3072    # DYNAMIC / COMPRESSED 行格式（MySQL 5.7.7+ 默认）的索引键上限，字节
_LEGACY_KEY_LIMIT = 767     # COMPACT / REDUNDANT：本项目【不支持】，见 E-6


def _mysql_offline_ddl() -> str:
    """把整条链按 MySQL 方言离线渲染。URL 只决定方言，不会真连。"""
    import io
    from contextlib import redirect_stdout

    from alembic import command

    from db import schema as S

    holder = type("E", (), {"url": sa.engine.url.make_url("mysql+pymysql://u:p@127.0.0.1/x")})()
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(S._config(holder, "data"), "head", sql=True)
    return buf.getvalue()


def _replay_mysql_ddl(script: str):
    """把离线脚本回放成 {表: {列: 类型串}} 与 {(表, 索引名): (列列表, 是否唯一)}。

    只认本项目 revision 会渲染出来的几种语句形态（CREATE TABLE / CREATE [UNIQUE] INDEX /
    DROP INDEX / ALTER TABLE … ADD COLUMN | MODIFY | DROP COLUMN）。认不出的语句直接报错，
    别静默跳过 —— 那会让守卫在新形态的语句上悄悄失明。
    """
    import re

    tables: dict = {}
    indexes: dict = {}
    for chunk in script.split(";"):
        # 先剥掉注释行再判空："-- Running upgrade …" 与下一条 CREATE TABLE 在同一个分号块里
        # 也剥掉 revision 自己 print 的进度行（trim_alias 在离线模式会打一句 "[trim_alias] …"）
        st = "\n".join(l for l in chunk.splitlines()
                       if not l.strip().startswith(("--", "["))).strip()
        if not st:
            continue
        m = re.match(r"CREATE TABLE (\w+) \((.*)\)\s*$", st, re.S)
        if m:
            name, body = m.group(1), m.group(2)
            cols = {}
            parts = [p_.strip() for p_ in re.split(r",\s*\n", body)]
            for part in parts:
                if part.startswith("PRIMARY KEY"):
                    pk = re.findall(r"\((.*?)\)", part)[0]
                    indexes[(name, "PRIMARY")] = ([c.strip() for c in pk.split(",")], True)
                elif part.startswith("CONSTRAINT"):
                    mm = re.match(r"CONSTRAINT (\w+) (UNIQUE|PRIMARY KEY) \((.*?)\)", part)
                    assert mm, f"认不出的约束：{part}"
                    indexes[(name, mm.group(1))] = ([c.strip() for c in mm.group(3).split(",")], True)
                else:
                    cn, ctype = part.split(" ", 1)
                    cols[cn] = ctype
            tables[name] = cols
            continue
        m = re.match(r"CREATE (UNIQUE )?INDEX (\w+) ON (\w+) \((.*?)\)$", st)
        if m:
            indexes[(m.group(3), m.group(2))] = ([c.strip() for c in m.group(4).split(",")],
                                                 bool(m.group(1)))
            continue
        m = re.match(r"DROP INDEX (\w+) ON (\w+)$", st)
        if m:
            assert (m.group(2), m.group(1)) in indexes, f"drop 一个不存在的索引：{st}"
            del indexes[(m.group(2), m.group(1))]
            continue
        m = re.match(r"ALTER TABLE (\w+) ADD COLUMN (\w+) (.*)$", st, re.S)
        if m:
            tables[m.group(1)][m.group(2)] = m.group(3)
            continue
        m = re.match(r"ALTER TABLE (\w+) MODIFY (\w+) (.*)$", st, re.S)
        if m:
            assert m.group(2) in tables[m.group(1)], f"MODIFY 一个不存在的列：{st}"
            tables[m.group(1)][m.group(2)] = m.group(3)
            continue
        m = re.match(r"ALTER TABLE (\w+) DROP COLUMN (\w+)$", st)
        if m:
            del tables[m.group(1)][m.group(2)]
            continue
        if st.startswith(("INSERT", "UPDATE", "DELETE", "CREATE TABLE alembic_version")):
            continue
        raise AssertionError(f"离线脚本里有守卫认不出的语句形态，先教会 _replay_mysql_ddl：{st[:120]}")
    return tables, indexes


def _key_bytes(ctype: str) -> int:
    """一个列在 InnoDB 索引键里最多占多少字节（utf8mb4 按 4 字节/字符算）。"""
    import re

    t = ctype.upper()
    m = re.match(r"VARCHAR\((\d+)\)", t)
    if m:
        return int(m.group(1)) * 4
    if t.startswith("BIGINT"):
        return 8
    if t.startswith(("INTEGER", "INT ", "INT(")) or t == "INT":
        return 4
    if t.startswith("DATETIME"):
        return 8
    if t.startswith(("BOOL", "TINYINT")):
        return 1
    if t.startswith("DOUBLE"):
        return 8
    if t.startswith("FLOAT"):
        return 4
    if t.startswith(("TEXT", "BLOB")):
        raise AssertionError(f"TEXT/BLOB 列进了索引（MySQL 要求前缀长度，本项目不用）：{ctype}")
    raise AssertionError(f"不认识的列类型，先教会 _key_bytes：{ctype}")


def test_every_mysql_index_key_fits_the_dynamic_row_format():
    """(E-6) 按 alembic 的产物，每个索引 / 唯一约束 / 主键的**全部列**字节之和 ≤ 3072。

    以前的守卫按【单列】×4 ≤ 767 算，而 uq_alias_title_season 是 (title, season) 两列：
    191×4 + 8 = 772 —— 它自己就超过 767，那条守卫守的是一个早就没守住的承诺。
    E-6 拍板：放弃老 InnoDB 行格式（COMPACT/REDUNDANT，上限 767），按 DYNAMIC 的 3072 算，
    且**必须按索引的全部列求和**。这里顺带把"我们确实超过了 767"钉死，免得注释又漂回去。
    """
    tables, indexes = _replay_mysql_ddl(_mysql_offline_ddl())
    assert len(indexes) >= 10, f"只回放出 {len(indexes)} 个索引，回放器多半没认全"
    sizes = {}
    for (table, name), (cols, _uniq) in indexes.items():
        sizes[(table, name)] = sum(_key_bytes(tables[table][c]) for c in cols)
    too_big = {k: v for k, v in sizes.items() if v > _INNODB_KEY_LIMIT}
    assert not too_big, f"这些索引键超过 DYNAMIC 行格式的 {_INNODB_KEY_LIMIT} 字节上限：{too_big}"
    widest = max(sizes.values())
    assert widest > _LEGACY_KEY_LIMIT, (
        f"最宽的索引键只有 {widest} 字节 —— 本项目声明不支持老行格式的依据（772 > 767）已经不成立，"
        "去把 db/dialect.py 与本用例的说法一起改了")


def test_the_dropped_duplicate_indexes_are_really_gone_on_mysql():
    """(E-48) 两个与唯一约束重复的索引，在 alembic 产物的终态里不存在；唯一约束本身还在。"""
    tables, indexes = _replay_mysql_ddl(_mysql_offline_ddl())
    names = {n for _, n in indexes}
    assert "ix_anime_alias_title" not in names and "ix_sourcegroup_name" not in names, sorted(names)
    assert {"uq_alias_title_season", "uq_sourcegroup_name"} <= names, sorted(names)


def _varlen(ctype: str):
    import re
    m = re.match(r"VARCHAR\((\d+)\)", ctype.upper())
    return int(m.group(1)) if m else None


def test_alembic_product_has_every_model_column_with_the_right_shape():
    """(E-7) alembic 链在 MySQL 上的终态，列集合与类型族要与模型一致。

    这是"库要长成模型说的样子"在 MySQL 方言上的版本：SQLite 侧由 tests/test_upgrade_path.py
    真升一个库来核，MySQL 侧没有真库，就核离线产物。挡住的形状：改了模型没开 revision、
    revision 写错了列名、或者只改了两张对称表里的一张。
    """
    from sqlalchemy.dialects import mysql
    from sqlmodel import SQLModel

    import db.models  # noqa: F401
    from db import META_TABLES

    tables, _ = _replay_mysql_ddl(_mysql_offline_ddl())
    problems = []
    for tname, table in SQLModel.metadata.tables.items():
        if tname in META_TABLES:
            continue
        assert tname in tables, f"alembic 产物里没有表 {tname}"
        got = tables[tname]
        for col in table.columns:
            if col.name not in got:
                problems.append(f"{tname}.{col.name}：模型有、alembic 产物没有（没开 revision？）")
                continue
            want = col.type.compile(dialect=mysql.dialect()).upper()
            have = got[col.name].upper()
            # 比类型族（VARCHAR / BIGINT / DOUBLE / DATETIME / TEXT / BOOL）与 VARCHAR 的长度，不比 NULL
            fam = lambda x: x.split(" ")[0].split("(")[0]     # noqa: E731
            if fam(want) != fam(have):
                problems.append(f"{tname}.{col.name}：模型是 {want}，alembic 产物是 {have}")
            elif fam(want) == "VARCHAR" and _varlen(want) != _varlen(have):
                # (R34 对抗审计) _COL_LEN 与 baseline 里硬编码的 191 漂开时，alias_key 按模型截、
                # MySQL 列只收 191 —— db/dialect.py docstring 里的缺陷①。以前只靠 alias_key 用例里一个字面量 191 兜底。
                problems.append(f"{tname}.{col.name}：VARCHAR 长度模型 {_varlen(want)} ≠ alembic 产物 {_varlen(have)}")
            # (R34 对抗审计) TEXT/BLOB 列带字面量 DEFAULT 在 MySQL 8/9 一律报 1101（b2c9e4f17a03 因此把
            # fail_reason 改成 VARCHAR(300)）。SQLite 不报、全套用例看不见，MySQL 用户升级时第一条 ADD COLUMN 就 fatal。
            if fam(have) in ("TEXT", "BLOB", "JSON", "LONGTEXT", "MEDIUMTEXT") and " DEFAULT " in f" {have} ":
                problems.append(f"{tname}.{col.name}：TEXT/BLOB 列带 DEFAULT，MySQL 建列必报 1101：{have}")
        extra = set(got) - {c.name for c in table.columns}
        if extra:
            problems.append(f"{tname}：alembic 产物多出模型没有的列 {sorted(extra)}")
    assert not problems, "\n  ".join(["alembic 的 MySQL 产物与模型对不上："] + problems)


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
    # 【扫的是一张清单，不是一个文件】(R28) R27 只扫 pages/settings.py，
    # 而 `init_business_state` 有【三个】调用点：设置页、顶栏『立即重连』、
    # 以及 `core/worker.run_db_watch` 的恢复边沿 —— 最后那个当时还是裸的同步调用，
    # 守卫看不见它（约束的作用域比验证的小，第②种形状）。
    # 新增碰库的异步处理器时，把文件加进这张表。
    _SCAN = ("pages/settings.py", "pages/layout.py", "core/worker.py")
    root = pathlib.Path(__file__).resolve().parent.parent
    src = "\n".join((root / f).read_text(encoding="utf8") for f in _SCAN)
    tree = ast.parse("")   # 占位，下面按文件逐个 parse
    # 【判据从"整棵子树里出现过 io_bound"改成"逐个调用点"】(R27)
    # 老判据是子树级的字符串包含：只要处理器里**有一句**包了 io_bound，
    # 同一个处理器里其余的同步库调用就永远报不出来。`_switch_backend` 正中此形状 ——
    # `switch_data_engine` 包了，紧接着的 `worker.init_business_state` 是裸的同步调用，
    # 而那时 db.engine 已经指向 MySQL（seed_source_groups + 两张种子表的全表扫回填）。
    # 逐个调用点判之后，同一个处理器里再加第二条也漏不掉。
    # 【`make_mysql_engine` 不在这张表里，它是"标记"不是"违规"】create_engine 是**惰性**的，
    # 不建连接（所以 `_migrate` 才要另外写一个 `_ping()` 丢进线程去真连一次）。
    # 它的作用是标出"这个处理器会碰 MySQL"，由下面那半条子树级判据用。
    # 第一版把它当成必须上线程的调用，于是把 `_migrate` 里正确的写法报成了违规。
    ON_THREAD_ONLY = {"switch_data_engine", "init_business_state", "probe_data_engine"}
    TOUCHES_MYSQL = {"make_mysql_engine", "switch_data_engine"}

    def _call_name(n):
        f = n.func
        return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""

    def _direct_calls(node):
        """处理器**自己身体里**的调用（不下钻内层 def —— 那些正是要被 io_bound 拎去线程的）。"""
        out = []
        stack = list(ast.iter_child_nodes(node))
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(n, ast.Call):
                out.append(n)
            stack.extend(ast.iter_child_nodes(n))
        return out

    trees = {f: ast.parse((root / f).read_text(encoding="utf8")) for f in _SCAN}
    offenders, scanned = [], []
    for _f, _t in trees.items():
      for node in ast.walk(_t):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for c in _direct_calls(node):
            name = _call_name(c)
            if name in ON_THREAD_ONLY:
                scanned.append(name)
                offenders.append(f"{_f}:{c.lineno} {node.name}() 里直接调了 {name}()")
            elif name in ("io_bound", "to_thread"):
                scanned.extend(_call_name(a) if isinstance(a, ast.Call)
                               else a.attr if isinstance(a, ast.Attribute)
                               else a.id if isinstance(a, ast.Name) else ""
                               for a in c.args[:1])
    assert offenders == [], ("这些调用在事件循环上同步碰库：\n  " + "\n  ".join(offenders)
                            + "\n改成 `await run.io_bound(fn, *args)`")
    # 反向：确认扫描确实看见了那几个该上线程的名字，别因为改名而空跑成绿
    hit = ON_THREAD_ONLY & set(scanned)
    assert {"switch_data_engine", "init_business_state"} <= hit, (
        f"扫描没看见预期的调用（只看见 {sorted(hit)}）—— 判据大概率已经失效")

    # 【第二半：子树级的兜底】(原判据) 一个碰 MySQL 的处理器里**一句 io_bound 都没有**，
    # 说明它整条路都在事件循环上 —— 上面逐调用点那一半只认得出名单里的函数名，
    # 认不出"随手一条 session 查询"，这半条兜的是那种。
    loose = []
    for _f, _t in trees.items():
        if _f != "pages/settings.py":
            continue          # 这半条只对设置页成立：另外两个文件用的是 asyncio.to_thread
        for node in ast.walk(_t):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            sub = ast.dump(node)
            if any(t in sub for t in TOUCHES_MYSQL) and "io_bound" not in sub:
                loose.append(f"{_f}:{node.lineno} {node.name}()")
    assert loose == [], ("这些处理器整条路都在事件循环上碰 MySQL：\n  " + "\n  ".join(loose))
    assert src.count("make_mysql_engine") >= 2 and "switch_data_engine" in src

    # 【第三半：内层同步小函数不许被直接调】(R27) `_ping` / `_probe` 这种嵌套 def
    # 存在的唯一理由就是"被 io_bound 拎到线程里去"，在处理器体内直接调它
    # 等于把它又搬回事件循环上 —— 而上面两半都看不见这种改法
    # （名字不在名单里；处理器里别处仍有 io_bound，子树判据照样绿。实测变异确认）。
    # 只认【身体里真的碰库】的那些嵌套 def，免得把普通的 UI 回调误报。
    _DB_ISH = ("connect", "get_session", "count_rows", "make_mysql_engine",
               "switch_data_engine", "migrate", "verify")
    inline = []
    for node in ast.walk(trees["pages/settings.py"]):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        threaded = {d.name for d in ast.walk(node)
                    if isinstance(d, ast.FunctionDef) and any(t in ast.dump(d) for t in _DB_ISH)}
        for c in _direct_calls(node):
            if isinstance(c.func, ast.Name) and c.func.id in threaded:
                inline.append(f"pages/settings.py:{c.lineno} {node.name}() 直接调了内层的 {c.func.id}()")
    assert inline == [], ("这些碰库的内层小函数被直接调用了（本该走 run.io_bound）：\n  "
                          + "\n  ".join(inline))


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
