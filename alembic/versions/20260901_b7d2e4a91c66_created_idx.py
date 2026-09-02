"""两张种子表的 created_at 加索引。

仪表盘打开后 `ui.timer(30.0)` 每 30 秒刷一次『最近入库』，发的是
`SELECT <整行> FROM animetorrent ORDER BY created_at DESC LIMIT 50`。
两张种子表原本只有 anime_id/movie_id 与 in-flight 三种索引，这条查询的执行计划是
`SCAN` + `USE TEMP B-TREE FOR ORDER BY`——而 `SELECT *` 让 sorter 要把每行完整记录搬一遍。

真库快照实测（99 番 / 1675 种子 / 70 片 / 569 版本，中位 60 次）：
    animetorrent  5.50 ms → 0.23 ms   （SCAN → SCAN USING INDEX ix_animetorrent_created_at）
    movietorrent  1.84 ms → 0.22 ms
而 sent 是只增不减的终态、行数随挂机线性增长，全表扫那一项会一直变贵。

【为什么必须单开一条】本项目**只有一条建库路径**：全新库同样是一路 alembic 建过去的
（`db/schema.upgrade` 的 docstring：「全新库会一路建过去」），没有 `create_all`/metadata 那一支。
baseline `41170d6a7ad4` 是逐张 `op.create_table` 手写的，里面**没有** created_at 索引 ——
也就是说全新库的这两个索引正是本脚本建出来的。
模型上的 `index=True` 只服务于 `tests/test_upgrade_path.py` 那条"库要长成模型说的样子"的守卫，
它自己不会让任何库多出索引。
"""
from alembic import op
import sqlalchemy as sa

revision = 'b7d2e4a91c66'
down_revision = 'a3c9e1f70b28'
branch_labels = None
depends_on = None

# (索引名, 表, 列)。索引名与 SQLModel 的 `index=True` 生成的名字【必须一致】，
# 否则全新库（走 metadata 建表）与存量库（走本脚本）会拿到两个不同名的索引，
# 而"两条建库路径产出不同的库"正是本项目反复栽跟头的那种失效形状。
_IDX = [("ix_animetorrent_created_at", "animetorrent", "created_at"),
        ("ix_movietorrent_created_at", "movietorrent", "created_at")]


def _role() -> str:
    from alembic import context
    return context.get_x_argument(as_dictionary=True).get("role", "data")


def upgrade() -> None:
    if _role() != "data":
        return
    from alembic import context
    if context.is_offline_mode():
        for name, table, col in _IDX:
            op.create_index(name, table, [col])
        return
    bind = context.get_bind()
    insp = sa.inspect(bind)
    have = set(insp.get_table_names())
    for name, table, col in _IDX:
        if table not in have:
            continue        # 防御性：baseline 恒排在本条之前建表，真实链上到不了这里
        if name in {i["name"] for i in insp.get_indexes(table)}:
            continue        # 幂等：重跑本条、或版本行丢失后重放时会走到
        op.create_index(name, table, [col])


def downgrade() -> None:
    raise NotImplementedError("本项目不支持降级，见 alembic/README")
