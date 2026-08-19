"""完结检测与断更提醒：anime 加三列。

finished_at / finish_optout / idle_notified_at —— 语义见 db.models.Anime 的注释。
只有业务表要改，故 role=meta 时整段跳过（meta 库只管 setting）。

类型定型照基线的规矩来：DateTime 在 MySQL 上带 fsp=6（否则只到秒，排序会并列）；
布尔列给 server_default 让已有行有确定值（新行的默认值由模型给，两边一致）。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = 'c7e1a93b4d02'
down_revision = 'b2c9e4f17a03'
branch_labels = None
depends_on = None


def _role() -> str:
    from alembic import context
    return context.get_x_argument(as_dictionary=True).get("role", "data")


def upgrade() -> None:
    if _role() != "data":
        return
    op.add_column("anime", sa.Column(
        "finished_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
        nullable=True))
    # server_default 用 "0" 而不是 sa.false()：SQLite 把布尔存成 0/1，两种方言下写法一致。
    op.add_column("anime", sa.Column(
        "finish_optout", sa.Boolean(), nullable=False, server_default="0"))
    op.add_column("anime", sa.Column(
        "idle_notified_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
        nullable=True))


def downgrade() -> None:
    if _role() != "data":
        return
    for col in ("idle_notified_at", "finish_optout", "finished_at"):
        op.drop_column("anime", col)
