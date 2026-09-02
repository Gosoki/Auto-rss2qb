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
    subprocess.run([sys.executable, "-c", "import config, db; db.init_db(); config.load_from_db(); db.apply_configured_backend()"],
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


# ---------------- verify 要看目标库【迁移前】有什么（E-17，R20） ----------------

def test_verify_flags_a_shrinking_target(tmp_path):
    """(E-17) 只比"源==目标"的话，**任何** overwrite 都会被报成"已校验的成功"。

    空源守卫挡得住"源 0 行"，挡不住更常见的那一种：切到 MySQL 用了几个月，
    再点一次『本地 → MySQL』当保险，源是几个月前的陈旧本地库，逐表行数完全一致、
    verify 名正言顺地通过，而库被回滚了几个月，界面上一句提示都没有。
    """
    from db import transfer
    src = {"anime": 10, "animetorrent": 100}
    # 迁完两边一致 —— 老口径到此为止就"通过"了
    assert transfer.verify.__defaults__ == (None, None), "verify 的签名变了"

    class _Eng:
        pass

    # 直接测判据本身：目标迁前 99 部、迁后 10 部 = 少了 89 部
    out = transfer.verify.__wrapped__ if hasattr(transfer.verify, "__wrapped__") else None
    assert out is None      # 没有装饰器，下面走真实路径

    import sqlalchemy as sa
    from sqlmodel import SQLModel
    a = sa.create_engine(f"sqlite:///{tmp_path}/a.db")
    b = sa.create_engine(f"sqlite:///{tmp_path}/b.db")
    SQLModel.metadata.create_all(a)
    SQLModel.metadata.create_all(b)
    # 两边都是 0 行 → 逐表一致
    assert transfer.verify(a, b, {k: 0 for k in transfer.TABLE_ORDER}) == []
    # 但如果目标库【迁移前】有 99 部番，这次迁完只剩 0 —— 必须提醒
    bad = transfer.verify(a, b, {k: 0 for k in transfer.TABLE_ORDER},
                          {**{k: 0 for k in transfer.TABLE_ORDER}, "anime": 99})
    assert bad and "回滚" in bad[-1], f"目标库被清空却没有任何提醒：{bad}"
    assert "99 → 0" in bad[-1]
    a.dispose(); b.dispose()


def test_a_stale_local_source_is_upgraded_before_it_is_migrated(tmp_path):
    """(R22) 『本地 SQLite → MySQL』要能在源库版本过旧时照样迁成。

    R21 之后 `init_db()` 只升 meta，data 链交给 `apply_configured_backend()` ——
    于是 `DB_BACKEND=mysql` 时**没有任何路径**再升本地那份 SQLite，
    它冻结在用户最后一次以 SQLite 为业务库时的版本。
    而『本地 → MySQL』恒取 `db.meta_engine` 当源，`migrate_data` 是按 head 的列去读它的：
    实测报 `源库的表 anime 读不出来…no such column: anime.finished_at`
    （目标库未被改动，失败是安全的），而那条提示说的"用本程序打开它跑一次升级后再迁"
    对 MySQL 用户已经**无路可走**。

    修法是在用户明确要读它的那一刻升一次。这条用例钉住的就是这件事。
    """
    import pytest
    import sqlalchemy as sa

    from db import schema, transfer

    src = sa.create_engine(f"sqlite:///{tmp_path/'old.db'}")
    dst = sa.create_engine(f"sqlite:///{tmp_path/'new.db'}")
    schema.upgrade(src, "data", "b2c9e4f17a03")     # 停在老版本
    schema.upgrade(dst, "data")                     # 目标是 head

    with src.connect() as c:
        info = c.exec_driver_sql("PRAGMA table_info(anime)").fetchall()
    notnull = [r[1] for r in info if r[3] and r[5] == 0]
    vals = {"id": 1, "title": "甲"}
    for k in notnull:
        # 日期列不能填空串：迁移读回来会 
        vals.setdefault(k, 0 if k in ("season", "confirmed", "rejected", "enrich_tries")
                        else ("2026-01-01 00:00:00" if k.endswith("_at") else ""))
    with src.begin() as c:
        c.execute(sa.text(f"INSERT INTO anime ({','.join(vals)}) "
                          f"VALUES ({','.join(':' + k for k in vals)})"), vals)

    # 不升级：按 head 的列去读旧库，必然读不出来（这正是被修掉的那个状态）
    with pytest.raises(ValueError, match="源库的表"):
        transfer.migrate_data(src, dst, overwrite=True)

    # 升一次之后就能迁 —— 这就是设置页在迁移那一刻做的事
    schema.upgrade(src, "data")
    res = transfer.migrate_data(src, dst, overwrite=True)
    assert res["moved"]["anime"] == 1


def test_the_migrate_handler_upgrades_its_source():
    """反向：上一条自己调了 `schema.upgrade`，测不出**设置页到底有没有调**（第③号形状）。"""
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parent.parent / "pages/settings.py")
                     .read_text(encoding="utf8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_migrate")
    dumped = ast.dump(fn)
    assert "db_schema" in dumped and "upgrade" in dumped, \
        "_migrate 没在读源库之前把它升到 head —— 旧结构的本地库迁不动，而 MySQL 用户没有别的升级入口"
