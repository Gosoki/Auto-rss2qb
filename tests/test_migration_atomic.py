"""SQLite 迁移必须原子：中途失败时已执行的 DDL 不留痕，重跑能成功。

pysqlite 只在 DML 前隐式 BEGIN，DDL 一律自动提交。多条 revision 一次升级时，
第一条的 ADD COLUMN 在任何 DML 之前执行 → 直接落盘；后面某条失败，alembic_version 回滚、列却留着 →
下次启动重跑第一条撞 duplicate column → mark_data_fatal，永久停摆。修法见 db/schema.py 的 _ddl_engine。
"""
import pytest
import sqlalchemy as sa

from db import schema


def _cols(eng, table):
    return {c["name"] for c in sa.inspect(eng).get_columns(table)}


def test_ddl_engine_rolls_back_ddl(tmp_path):
    """两步 DDL，第二步抛错 → 第一步不留痕；重跑成功。"""
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'a.db'}")
    with eng.begin() as c:
        c.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    with pytest.raises(RuntimeError):
        with schema._ddl_engine(eng) as ddl, ddl.begin() as c:
            c.exec_driver_sql("ALTER TABLE t ADD COLUMN a INTEGER")
            raise RuntimeError("第二步炸了")
    assert "a" not in _cols(eng, "t"), "第一步的 ADD COLUMN 自动提交了——事务没盖住 DDL"

    with schema._ddl_engine(eng) as ddl, ddl.begin() as c:     # 重跑：不撞 duplicate column
        c.exec_driver_sql("ALTER TABLE t ADD COLUMN a INTEGER")
        c.exec_driver_sql("ALTER TABLE t ADD COLUMN b INTEGER")
    assert {"a", "b"} <= _cols(eng, "t")
    eng.dispose()


def test_multi_revision_upgrade_failure_leaves_no_trace(upgrade_from, monkeypatch):
    """真走 alembic：从 f3c8a1d6b2e9 升两条，a7d3c5e91f42 的列已加完、写版本号时抛错 →
    它加的列一条不留、版本号停在原地；撤掉故障后重跑升到 head。
    alembic 对 SQLite（transactional_ddl=False）是每条 revision 各一个事务，原子单位就是一条 revision。"""
    from alembic.runtime import migration

    old = "f3c8a1d6b2e9"
    eng = upgrade_from(old)
    before = _cols(eng, "animetorrent")
    assert "lock_fallback" not in before          # 前提：旧库确实没有 a7d3c5e91f42 的列

    real = migration.HeadMaintainer.update_to_step

    def boom(self, step):
        if "a7d3c5e91f42" in step.to_revisions:
            raise RuntimeError("注入：revision 写版本号前失败")
        return real(self, step)

    monkeypatch.setattr(migration.HeadMaintainer, "update_to_step", boom)
    with pytest.raises(RuntimeError):
        upgrade_from.to_head(eng)
    assert upgrade_from.revision_of(eng) == old
    assert _cols(eng, "animetorrent") == before, "失败的升级留下了半截表结构"

    monkeypatch.setattr(migration.HeadMaintainer, "update_to_step", real)
    upgrade_from.to_head(eng)
    assert upgrade_from.revision_of(eng) == schema.head_revision()
    assert {"lock_fallback", "foreign_season"} <= _cols(eng, "animetorrent")
