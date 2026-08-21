"""整库迁移的【清空前预检】。

迁移是"先清空目标、再逐批写入"，所以任何会让写入中途失败的东西都必须在清空【之前】查出来。
查漏一样，用户得到的就是一个半个库——而这个方向的目标库往往正是他当前在用的那个。
"""
import sqlalchemy as sa
from sqlmodel import Session

from db import transfer
from db.models import Anime, Movie


def _fresh(tmp_path, name):
    """建一个升到 head 的空业务库。"""
    import subprocess
    import sys
    p = tmp_path / name
    subprocess.run([sys.executable, "-c", "import db; db.init_db()"],
                   env={**__import__("os").environ, "DB_PATH": str(p)},
                   cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent),
                   check=True, capture_output=True)
    return sa.create_engine(f"sqlite:///{p}"), p


def test_duplicate_unique_values_are_refused_before_wiping_the_target(tmp_path):
    """源库里有重复的 movie.mikan_id → 在清空目标【之前】中止，目标库分毫不动。

    唯一索引是后加的，而它【自动对迁移生效】——加约束的那一轮只验了"建库"与"升级"两条路径。
    源库若停在加约束之前的版本，里面就可能留着两行同 mikan_id（uniq_mikan 自己的 docstring
    就承认存量库会有）。没有这道预检时实测：目标库被清空 → 写完前四张表 → 插 movie 时
    IntegrityError，用户当场得到"番剧全在、剧场版一部没有"的半个库，再点一次还是同样的结果。
    """
    src, src_p = _fresh(tmp_path, "src.db")
    dst, _ = _fresh(tmp_path, "dst.db")
    # 源库退回加约束之前的形态，再塞两行同 mikan_id
    with src.begin() as c:
        c.execute(sa.text("DROP INDEX ix_movie_mikan_id"))
        c.execute(sa.text("CREATE INDEX ix_movie_mikan_id ON movie (mikan_id)"))
    with Session(src) as s:
        s.add(Anime(title="源库的番", quarter="26A", confirmed=True))
        s.add(Movie(title="重复0", mikan_id="1000", quarter="26A"))
        s.add(Movie(title="重复1", mikan_id="1000", quarter="26A"))
        s.commit()
    with Session(dst) as s:                       # 目标库＝用户当前在用的活库
        s.add(Anime(title="活库里的番", quarter="26A", confirmed=True))
        s.add(Movie(title="活库里的片", quarter="26A"))
        s.commit()

    before = transfer.count_rows(dst)
    try:
        transfer.migrate_data(src, dst, overwrite=True)
        raise AssertionError("重复值没被拦下，目标库已被清空并写了一半")
    except ValueError as e:
        assert "唯一约束" in str(e) and "mikan_id" in str(e)
    assert transfer.count_rows(dst) == before, "目标库被动过了——预检必须在清空之前"


def test_clean_source_still_migrates(tmp_path):
    """预检不能误伤：没有重复时照常迁完。"""
    src, _ = _fresh(tmp_path, "src2.db")
    dst, _ = _fresh(tmp_path, "dst2.db")
    with Session(src) as s:
        s.add(Anime(title="源库的番", quarter="26A", confirmed=True))
        s.add(Movie(title="片A", mikan_id="1000", quarter="26A"))
        s.add(Movie(title="片B", mikan_id="2000", quarter="26A"))
        s.add(Movie(title="没链接的", quarter="26A"))      # NULL 不参与唯一性
        s.commit()
    transfer.migrate_data(src, dst, overwrite=True)
    assert transfer.count_rows(dst)["movie"] == 3
    assert transfer.count_rows(dst)["anime"] == 1


def test_unreadable_source_table_is_refused_before_wiping(tmp_path):
    """源库版本过旧（少一列）→ 在清空目标【之前】中止（`_readable_source_tables`）。

    这道闸此前零端到端覆盖：实测把它拆掉之后，目标库先被清空、写完 anime 才炸在 movie 上，
    最终 3 anime / 0 movie —— 正是 docstring 里那个「半个库」。
    """
    src, src_p = _fresh(tmp_path, "old_src.db")
    dst, _ = _fresh(tmp_path, "old_dst.db")
    with Session(src) as s:
        s.add(Anime(title="源库的番", quarter="26A", confirmed=True))
        s.add(Movie(title="片", quarter="26A"))
        s.commit()
    with Session(dst) as s:
        s.add(Anime(title="活库里的番", quarter="26A", confirmed=True))
        s.commit()
    # 模拟"源库还是老版本"：把新版才有的列从源库里去掉（SQLite 支持 DROP COLUMN）
    with src.begin() as c:
        c.execute(sa.text("ALTER TABLE movie DROP COLUMN mikan_type"))

    before = transfer.count_rows(dst)
    try:
        transfer.migrate_data(src, dst, overwrite=True)
        raise AssertionError("源库读不出来却没被拦下，目标库已被清空")
    except ValueError as e:
        assert "读不出来" in str(e) or "版本过旧" in str(e)
    assert transfer.count_rows(dst) == before, "目标库被动过了——预检必须在清空之前"


def test_overlong_values_are_refused_before_wiping(tmp_path, monkeypatch):
    """源库里有超过目标库列长上限的值 → 清空之前中止（`_overlong_values`）。

    这道闸的函数本身有用例，但 `migrate_data` 里那句【调用】此前没人管：
    删掉调用行，全套用例照样全绿。这里补的是端到端那一半。
    """
    from db import dialect
    src, _ = _fresh(tmp_path, "long_src.db")
    dst, _ = _fresh(tmp_path, "long_dst.db")
    with Session(src) as s:
        s.add(Anime(title="正常番", quarter="26A", confirmed=True))
        s.commit()
    with Session(dst) as s:
        s.add(Anime(title="活库里的番", quarter="26A", confirmed=True))
        s.commit()
    # 目标是 SQLite（VARCHAR 不限长），所以直接把长度表当成"目标库有限制"来喂：
    # _overlong_values 读的就是目标库反射出来的列长，这里改成一个小值即可触发同一条路径。
    monkeypatch.setattr(transfer, "_overlong_values",
                        lambda s_, d_: ["anime.title: 最长 3 > 上限 2（1 行超限）"])

    before = transfer.count_rows(dst)
    try:
        transfer.migrate_data(src, dst, overwrite=True)
        raise AssertionError("超长值没被拦下，目标库已被清空")
    except ValueError as e:
        assert "列长上限" in str(e)
    assert transfer.count_rows(dst) == before


def test_every_table_is_accounted_for_by_exactly_one_side():
    """每张表都必须被 META_TABLES 或 TABLE_ORDER 恰好认领一次。

    `META_TABLES`（alembic/env.py 的 include_object 靠它分 role）与 `TABLE_ORDER`
    （db/transfer.py 的迁移清单）是两份**毫无关联的手写清单**。新增业务表时只改
    models.py + 写 revision、漏改 TABLE_ORDER 的后果是**静默**的：migrate_data 不搬它、
    count_rows 不数它，而 verify() 也只遍历 TABLE_ORDER —— 用户点「迁移数据」拿到绿色的
    「迁移完成并校验一致」，新库里那张表却是空的；接着点「切换」，那部分数据就此消失。
    """
    from sqlmodel import SQLModel

    import db.models  # noqa: F401
    from db import META_TABLES

    known = set(SQLModel.metadata.tables)
    claimed = set(META_TABLES) | set(transfer.TABLE_ORDER)
    assert known == claimed, (
        f"没被认领的表：{sorted(known - claimed)}；清单里多出来的：{sorted(claimed - known)}")
    assert not (set(META_TABLES) & set(transfer.TABLE_ORDER)), "同一张表被两边都认领了"


def test_empty_source_never_wipes_a_non_empty_target(tmp_path):
    """空源库 + 覆盖 = 纯破坏，必须在清空之前拦下。

    真实形态：用户先切到了 MySQL（本地 SQLite 的业务表因此一直是空的），过后"为保险"
    再点一次『本地 SQLite → MySQL』——目标就是他当前正在用的那个库。六张表被 DELETE 干净、
    再写入 0 行，而 verify 拿 0==0 判"一致"，页面弹绿色的「迁移完成并校验一致」：
    用户唯一可能察觉的时刻反而确认了一切正常。而 DB_BACKEND=mysql 时备份 scope 恒是 meta，
    业务数据一条都不在备份里——没有任何退路。
    """
    src, _ = _fresh(tmp_path, "empty_src.db")
    dst, _ = _fresh(tmp_path, "live_dst.db")
    with Session(dst) as s:
        s.add(Anime(title="活库里的番", quarter="26A", confirmed=True))
        s.add(Movie(title="活库里的片", quarter="26A"))
        s.commit()

    before = transfer.count_rows(dst)
    try:
        transfer.migrate_data(src, dst, overwrite=True)
        raise AssertionError("空源迁移没被拦下，目标库已被清空")
    except ValueError as e:
        assert "源库是空的" in str(e)
    assert transfer.count_rows(dst) == before, "目标库被清空了"
