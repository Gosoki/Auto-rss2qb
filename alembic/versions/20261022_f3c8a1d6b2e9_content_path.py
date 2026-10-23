"""animetorrent / movietorrent 加 content_path：qB 回报的内容路径，给『播放』用。

语义见 db.models.AnimeTorrent 的注释。已有行留空（NULL），由归档轮按 hash 向 qB 补齐；已经归档的问不到了，保持空。
只有业务表要改，role=meta 整段跳过。类型照 save_path：自由文本 → TEXT（MySQL 上钉 utf8mb4_bin）；可空，免得 MySQL 的 TEXT 默认值问题。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = 'f3c8a1d6b2e9'
down_revision = 'e8b3d5a2f714'
branch_labels = None
depends_on = None


def _role() -> str:
    from alembic import context
    return context.get_x_argument(as_dictionary=True).get("role", "data")


def _add_column_if_missing(table: str, col: sa.Column) -> None:
    """加列，已存在就跳过（必须幂等，理由详见 c7e1a93b4d02 的同名函数）。"""
    from alembic import context
    if context.is_offline_mode():
        op.add_column(table, col)
        return
    have = {c["name"] for c in sa.inspect(context.get_bind()).get_columns(table)}
    if col.name in have:
        return
    op.add_column(table, col)


def upgrade() -> None:
    if _role() != "data":
        return
    for table in ("animetorrent", "movietorrent"):
        _add_column_if_missing(table, sa.Column(
            "content_path", sa.Text().with_variant(mysql.TEXT(collation="utf8mb4_bin"), "mysql"),
            nullable=True))


def downgrade() -> None:
    raise NotImplementedError("本项目不支持降级，见 alembic/README")
