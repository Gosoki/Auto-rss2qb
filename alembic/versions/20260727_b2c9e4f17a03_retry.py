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


def _add_column_if_missing(table: str, col: sa.Column) -> None:
    """加列，已存在就跳过。理由同 c7e1a93b4d02：一条 revision 里的多个 ALTER 在 MySQL 上
    不在同一事务里（DDL 隐式提交），而版本号最后才写——中途断掉就再也升不上去了。"""
    from alembic import context
    if context.is_offline_mode():
        # 【--sql 模式下没有真连接】get_bind() 给的是 MockConnection，sa.inspect 会抛
        # NoInspectionAvailable。离线模式的产物是一份给人看的 SQL 脚本，本来就无从判断
        # "列在不在"——原样发出 ADD COLUMN 即可（这也是改动前的行为）。
        op.add_column(table, col)
        return
    bind = context.get_bind()
    if col.name in {c["name"] for c in sa.inspect(bind).get_columns(table)}:
        return
    op.add_column(table, col)


def upgrade() -> None:
    if _role() != "data":
        return
    for table in _TABLES:
        # server_default：已有行要有确定值。新行的默认值由模型给，两边一致。
        _add_column_if_missing(table, sa.Column(
            "retry_count", sa.BigInteger().with_variant(mysql.BIGINT(), "mysql"),
            nullable=False, server_default="0"))
        _add_column_if_missing(table, sa.Column(
            "retry_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True))
        # 用 VARCHAR(300) 而不是别处自由文本惯用的 TEXT：MySQL 不允许 TEXT 列带 DEFAULT
        # （错误 1101 "BLOB, TEXT … can't have a default value"），而这一列是 NOT NULL DEFAULT ''。
        # 写入侧本就截到 300 字，长度对得上。db.dialect._COL_LEN 里同步钉了这个长度。
        _add_column_if_missing(table, sa.Column(
            "fail_reason",
            sa.String(300).with_variant(mysql.VARCHAR(300, collation="utf8mb4_bin"), "mysql"),
            nullable=False, server_default=""))


def downgrade() -> None:
    if _role() != "data":
        return
    for table in _TABLES:
        for col in ("fail_reason", "retry_at", "retry_count"):
            op.drop_column(table, col)
