"""E-77 方案 C：animetorrent.foreign_season（用户标的『不属于本季』）。语义见 db.models 对应字段的注释。

只有业务表要改，role=meta 整段跳过。布尔列照 a7d3c5e91f42：NOT NULL + server_default "0"。
存量行一律为 0，不回填 —— 这个标记只能由用户在详情页点按钮产生，后台永不自动打。
"""
from alembic import op
import sqlalchemy as sa

revision = '41998a08995d'
down_revision = 'a7d3c5e91f42'
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
    _add_column_if_missing("animetorrent", sa.Column("foreign_season", sa.Boolean(), nullable=False, server_default="0"))


def downgrade() -> None:
    raise NotImplementedError("本项目不支持降级，见 alembic/README")
