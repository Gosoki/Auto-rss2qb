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
