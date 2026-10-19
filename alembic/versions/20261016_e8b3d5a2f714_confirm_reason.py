"""anime 加 confirm_reason：记下番为什么进了『待确认』。

只给待确认页显示用，语义见 db.models.Anime 的注释、键与文案见 core.anime.CONFIRM_REASONS。
已有行留空（上线前进待确认的，原因已无从得知，页面显示『未记录』）。
只有业务表要改，role=meta 整段跳过。类型照基线：可空自由文本 → TEXT（MySQL 上钉 utf8mb4_bin）。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = 'e8b3d5a2f714'
down_revision = 'd1f6b83c4e29'
branch_labels = None
depends_on = None


def _role() -> str:
    from alembic import context
    return context.get_x_argument(as_dictionary=True).get("role", "data")


def _add_column_if_missing(table: str, col: sa.Column) -> None:
    """加列，已存在就跳过。必须幂等：MySQL 上 DDL 隐式提交、版本号最后才写，中途断掉重跑会撞
    1060 Duplicate column 并被标成 fatal（理由详见 c7e1a93b4d02 的同名函数）。
    --sql 离线模式没有真连接（get_bind 给的是 MockConnection），原样发出 ADD COLUMN。"""
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
    _add_column_if_missing("anime", sa.Column(
        "confirm_reason", sa.Text().with_variant(mysql.TEXT(collation="utf8mb4_bin"), "mysql"),
        nullable=True))


def downgrade() -> None:
    raise NotImplementedError("本项目不支持降级，见 alembic/README")
