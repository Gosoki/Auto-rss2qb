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


def _add_column_if_missing(table: str, col: sa.Column) -> None:
    """加列，已存在就跳过。

    【为什么必须幂等】一条 revision 里的多个 ALTER 在 MySQL 上【不在同一个事务里】
    （DDL 隐式提交），而版本号是整条 revision 跑完才写。所以掉电/被 kill 在第 2 条 ALTER 上时，
    库里已经有了前 1 列、版本号却还停在旧值——下次启动重跑这条 revision，第一条 ALTER 就撞
    1060 Duplicate column，异常一路上抛到 main.py 被标成 fatal，而 fatal【只能人工解除】。
    于是每次重启都是同一个死循环，用户只能自己去改库。实测复现过。
    """
    from alembic import context
    if context.is_offline_mode():
        # 【--sql 模式下没有真连接】get_bind() 给的是 MockConnection，sa.inspect 会抛
        # NoInspectionAvailable。离线模式的产物是一份给人看的 SQL 脚本，本来就无从判断
        # "列在不在"——原样发出 ADD COLUMN 即可（这也是改动前的行为）。
        op.add_column(table, col)
        return
    bind = context.get_bind()
    have = {c["name"] for c in sa.inspect(bind).get_columns(table)}
    if col.name in have:
        return
    op.add_column(table, col)


def upgrade() -> None:
    if _role() != "data":
        return
    _add_column_if_missing("anime", sa.Column(
        "finished_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
        nullable=True))
    # server_default 用 "0" 而不是 sa.false()：SQLite 把布尔存成 0/1，两种方言下写法一致。
    _add_column_if_missing("anime", sa.Column(
        "finish_optout", sa.Boolean(), nullable=False, server_default="0"))
    _add_column_if_missing("anime", sa.Column(
        "idle_notified_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
        nullable=True))


def downgrade() -> None:
    if _role() != "data":
        return
    for col in ("idle_notified_at", "finish_optout", "finished_at"):
        op.drop_column("anime", col)
