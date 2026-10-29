"""r39：四个布尔标记列。语义见 db.models 对应字段的注释。

· animetorrent.lock_fallback   —— 锁定源停更兜底下的那一份（锁定组补发后再下它的版本）
· animetorrent.ep_dropped      —— 用户删的是该集最后一份：这一集不要了，自动路径不再补
· animetorrent.episode_manual  —— 用户亲手改过集号：集号展开/回折/截断修复都跳过
· movie.quarter_locked         —— 人工改过年份：后台扫描不覆盖

只有业务表要改，role=meta 整段跳过。布尔列照 c7e1a93b4d02 的 finish_optout：NOT NULL + server_default "0"。
存量行一律为 0，不回填。
"""
from alembic import op
import sqlalchemy as sa

revision = 'a7d3c5e91f42'
down_revision = 'f3c8a1d6b2e9'
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
    for col in ("lock_fallback", "ep_dropped", "episode_manual"):
        _add_column_if_missing("animetorrent", sa.Column(col, sa.Boolean(), nullable=False, server_default="0"))
    _add_column_if_missing("movie", sa.Column("quarter_locked", sa.Boolean(), nullable=False, server_default="0"))


def downgrade() -> None:
    raise NotImplementedError("本项目不支持降级，见 alembic/README")
