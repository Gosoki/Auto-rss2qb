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
