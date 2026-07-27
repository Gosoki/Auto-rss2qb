"""暂时性失败的自动重试：animetorrent / movietorrent 各加三列。

retry_count / retry_at / fail_reason —— 语义见 db.models 与 core.engine.RETRY_BACKOFF_MIN。
只有业务表要改，故 role=meta 时整段跳过（meta 库只管 setting）。

类型定型照基线的规矩来：整数一律 BIGINT（MySQL 的 INT 只有 4 字节）、自由文本用 TEXT
且 MySQL 侧 utf8mb4_bin、DateTime 在 MySQL 上带 fsp=6（否则只到秒，排序会并列）。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = 'b2c9e4f17a03'
down_revision = '41170d6a7ad4'
branch_labels = None
depends_on = None

_TABLES = ("animetorrent", "movietorrent")


def _role() -> str:
    from alembic import context
    return context.get_x_argument(as_dictionary=True).get("role", "data")


def upgrade() -> None:
    if _role() != "data":
        return
    for table in _TABLES:
        # server_default：已有行要有确定值。新行的默认值由模型给，两边一致。
        op.add_column(table, sa.Column(
            "retry_count", sa.BigInteger().with_variant(mysql.BIGINT(), "mysql"),
            nullable=False, server_default="0"))
        op.add_column(table, sa.Column(
            "retry_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True))
        # 用 VARCHAR(300) 而不是别处自由文本惯用的 TEXT：MySQL 不允许 TEXT 列带 DEFAULT
        # （错误 1101 "BLOB, TEXT … can't have a default value"），而这一列是 NOT NULL DEFAULT ''。
        # 写入侧本就截到 300 字，长度对得上。db.dialect._COL_LEN 里同步钉了这个长度。
        op.add_column(table, sa.Column(
            "fail_reason",
            sa.String(300).with_variant(mysql.VARCHAR(300, collation="utf8mb4_bin"), "mysql"),
            nullable=False, server_default=""))


def downgrade() -> None:
    if _role() != "data":
        return
    for table in _TABLES:
        for col in ("fail_reason", "retry_at", "retry_count"):
            op.drop_column(table, col)
