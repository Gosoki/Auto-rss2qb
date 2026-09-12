"""告警已读归档表 alert_ack。

三类"只报不改"的巡检发现（同一部番被拆成两条 / 同 bgm 既是番又是剧场版 / 绑定看着不对）
一直挂在仪表盘上，处理不掉就越积越多。这张表记"用户对**这一条**发现点过知道了"：
`ident` 是发现的身份、`fact` 是当时的事实指纹，事实变了就自动复活。判据见 core/alerts.py。

【为什么落业务库而不是 meta 的 setting】ident 里嵌着 anime.id / movie.id，那两个数只在
某一个业务库内部有意义；而 meta 恒本地、不随业务库走。走 `db.scoped_flag` 的话，用户点一次
『迁移数据』`data_identity()` 就变，数据一行没动而**全部已读复活** —— 那正是本功能要消掉的东西。
落业务库则随 `migrate_data` 整行搬走（已加进 db/transfer.TABLE_ORDER）。

类型定型照基线的规矩：整数 BIGINT、自由文本按 db/dialect._COL_LEN 钉成 VARCHAR
（这三列都是 NOT NULL DEFAULT ''，落 TEXT 会撞 MySQL 1101）、DateTime 在 MySQL 上带 fsp=6。
只有业务表要改，role=meta 整段跳过。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = 'd1f6b83c4e29'
down_revision = 'c9a4f1e2d7b3'
branch_labels = None
depends_on = None

_TABLE = "alert_ack"


def _role() -> str:
    from alembic import context
    return context.get_x_argument(as_dictionary=True).get("role", "data")


def _varchar(n: int):
    return sa.String(n).with_variant(mysql.VARCHAR(n, collation="utf8mb4_bin"), "mysql")


def _existing_tables() -> set:
    """当前库里已有的表。用来让本条幂等（理由同 baseline：MySQL 上 DDL 隐式提交、
    版本号最后才写，中途断掉重跑会撞 1050 Table already exists 并被标 fatal）。"""
    from alembic import context
    if context.is_offline_mode():
        return set()        # --sql 模式没有真连接；离线产物是给人看的脚本，原样输出
    return set(sa.inspect(context.get_bind()).get_table_names())


def upgrade() -> None:
    if _role() != "data":
        return
    if _TABLE in _existing_tables():
        return
    op.create_table(
        _TABLE,
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('kind', _varchar(16), nullable=False, server_default=''),
        sa.Column('ident', _varchar(255), nullable=False),
        sa.Column('fact', _varchar(1000), nullable=False, server_default=''),
        sa.Column('summary', _varchar(500), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'),
                  nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ident', name='uq_alert_ack_ident'),
    )


def downgrade() -> None:
    raise NotImplementedError("本项目不支持降级，见 alembic/README")
