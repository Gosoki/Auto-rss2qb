"""两张种子表加 prev_status（E-49）；drop 两个与唯一约束完全重复的索引（E-48）。

【E-49】force 重下从终态（deleted / excluded / stalled / 已归档）出发时，进锁段把 status 置成
`downloading` 占位再出锁去 await 取种（最长 180 秒）。协程活不到回写那一步（进程被杀 / 库抖）时，
行永久停在 downloading，下一轮 `sweep_stale_delivering` 只能把它**无条件写成 pending**：
deleted 变回自动队列、excluded 的排除被撤销、stalled 丢掉 HAVE 身份（flush 当场为同一集换源下第二份到
同一目录）、已归档的 archived_at 再也回不来。`prev_status` 记住占位前的状态，复位时按它还原。
配套：占位时**不再**清 archived_at 与 qB 实时态（挪到交付成功那一刻清），这样崩溃后行上什么都没丢。

【E-48】`ix_anime_alias_title` 与 `ix_sourcegroup_name` 各自的表上都有 UniqueConstraint
（uq_alias_title_season 的最左前缀 / uq_sourcegroup_name），唯一索引本来就服务 `WHERE title=?` /
`WHERE name=?`。实测 `WHERE name=?` 走的已是 sqlite_autoindex_sourcegroup_1，这两个索引连被选中都不会；
留着是纯写放大（MySQL 上还各多一个 764 字节键的 B 树）。`db/models.py` 另外两处明写的规则
「唯一约束已覆盖等值查找，不再另建普通索引」——同一个决定该在 3 处生效，只落了 1 处。

类型定型照基线的规矩：自由文本落 TEXT、MySQL 侧带 utf8mb4_bin（与 status 列同型；tests/test_mysql_compat.py 会拿模型核对产物）。只有业务表要改，role=meta 整段跳过。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = 'c9a4f1e2d7b3'
down_revision = 'b7d2e4a91c66'
branch_labels = None
depends_on = None

_TABLES = ("animetorrent", "movietorrent")
# (索引名, 表)。与 baseline 里 batch_op.f('ix_…') 生成的名字一致。
_DROP_IDX = [("ix_anime_alias_title", "anime_alias"),
             ("ix_sourcegroup_name", "sourcegroup")]


def _role() -> str:
    from alembic import context
    return context.get_x_argument(as_dictionary=True).get("role", "data")


def _prev_status_col() -> sa.Column:
    # 与 status 列同一族、同一定型（自由文本 → TEXT，MySQL 侧 utf8mb4_bin）：可空，
    # None = 没有占位前状态，复位时按老规矩落 pending。不带 DEFAULT，所以能用 TEXT
    # （fail_reason 那列是因为 NOT NULL DEFAULT '' 才被迫用 VARCHAR）。
    return sa.Column(
        "prev_status",
        sa.Text().with_variant(mysql.TEXT(collation="utf8mb4_bin"), "mysql"),
        nullable=True)


def _add_column_if_missing(table: str, col: sa.Column) -> None:
    """加列，已存在就跳过（理由同 b2c9e4f17a03：DDL 隐式提交、版本号最后才写，中途断掉重跑时前面几步已生效）。"""
    from alembic import context
    if context.is_offline_mode():
        # --sql 模式没有真连接，查不到"列在不在"；离线产物是给人看的脚本，原样发出 ADD COLUMN。
        op.add_column(table, col)
        return
    bind = context.get_bind()
    if col.name in {c["name"] for c in sa.inspect(bind).get_columns(table)}:
        return
    op.add_column(table, col)


def upgrade() -> None:
    if _role() != "data":
        return
    from alembic import context
    if context.is_offline_mode():
        for table in _TABLES:
            _add_column_if_missing(table, _prev_status_col())
        for name, table in _DROP_IDX:
            op.drop_index(name, table_name=table)
        return
    bind = context.get_bind()
    insp = sa.inspect(bind)
    have = set(insp.get_table_names())
    # 【每一步都幂等】MySQL 上 DDL 隐式提交、版本号最后才写，中途断掉重跑时前面几步已经生效。
    for table in _TABLES:
        if table in have:
            _add_column_if_missing(table, _prev_status_col())
    for name, table in _DROP_IDX:
        if table not in have:
            continue
        if name in {i["name"] for i in insp.get_indexes(table)}:
            # 直接 DROP，不走 batch_alter_table。e5a71c0d2b93 踩过的是"同一个 batch 里先 drop 再 create"
            # 会被反射到的旧定义覆盖；单独的 drop_index 在 batch 里其实也能掉（R34 对抗审计实测）——
            # 但仍不走 batch，免得将来有人在同一个 batch 里追加 create 又踩回去。
            op.drop_index(name, table_name=table)


def downgrade() -> None:
    raise NotImplementedError("本项目不支持降级，见 alembic/README")
