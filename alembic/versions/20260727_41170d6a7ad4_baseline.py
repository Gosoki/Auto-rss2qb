"""基线：全部表结构的起点。

本项目【不做向后迁移】——切到 Alembic 那天起，老库一律重建，故这里是唯一的起点，
没有 downgrade 到"更早"的概念。以后改表结构一律新增 revision，别改这个文件。

同一条版本链会被两个引擎各跑一遍（见 alembic/env.py 的双引擎说明），
脚本内按 role 决定自己建哪部分：
    role=meta → 只建 setting（配置恒在本地 SQLite）
    role=data → 建其余 6 张业务表 + 索引

以下三件事原先由 db/__init__.py 里五个临时迁移函数在每次启动时"补"，现在一次性做在基线里：
  · 非主键整数一律 BIGINT —— MySQL 的 INT 只有 4 字节，qb_size 存字节数会在 2GiB 溢出
  · 字符串按是否参与索引分别定型为 VARCHAR(n) / TEXT，MySQL 侧一律 utf8mb4_bin
    （_ci 排序规则会把 'Ⅱ' 判等于 'II'、全角'－'等于半角'-'，撞唯一约束且让等值查找变松散）
  · info_hash 上【不建】普通索引（唯一约束的索引已覆盖全部等值查找）
partial index 是 SQLite 独有语法，MySQL 不支持，故按方言分支。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = '41170d6a7ad4'
down_revision = None
branch_labels = None
depends_on = None

# in-flight 高频查询用的 partial index 谓词里写死了状态名，必须与 core.engine.TRACKED_STATUSES
# 和 _inflight_where 的第一个条件逐字对齐。从词表拼出来，别手抄——手抄改了不会报错，
# 只会静默失去索引加速。
def _inflight_index_sql(table: str) -> str:
    from core.engine import TRACKED_STATUSES
    states = ",".join(f"'{s}'" for s in TRACKED_STATUSES)
    # 【IF NOT EXISTS 不是可省的】本条 revision 的其余部分都做了幂等（6 张表各有 _have 闸），
    # 唯独这两条 partial index 漏了——而它们恰恰是【最容易已经存在】的：
    # Alembic 化之前的 db/__init__._migrate_inflight_indexes 每次启动都建同名索引，
    # 所以任何一个老库原地升级都会撞上 "index already exists"，不需要任何竞态。
    # 撞上就是 mark_data_fatal，而 fatal 只能人工解除 → 每次重启同一个死循环。
    return (f"CREATE INDEX IF NOT EXISTS ix_{table}_inflight ON {table}(status, qb_progress) "
            f"WHERE status IN ({states}) AND qb_progress < 1.0")


def _role() -> str:
    from alembic import context
    return context.get_x_argument(as_dictionary=True).get("role", "data")


def _existing_tables() -> set:
    """当前库里已有的表。用来让整条 baseline 幂等。

    【为什么 baseline 最需要幂等】它是 DDL 最多的一条（6 张表 + 8 个索引；2 条 partial index 走 IF NOT EXISTS），
    而它恰好跑在"第一次连一个全新 MySQL"的时候——那正是最容易被打断的时刻。
    MySQL 上 DDL 隐式提交、而版本号是整条 revision 跑完才写，所以中途断掉后重跑必撞
    1050 Table already exists，异常上抛被标 fatal，而 fatal【只能人工解除】：每次重启同一个死循环。
    另一条同样常见的路径根本不需要竞态——用 mysqldump 只导业务表、没带 alembic_version_data，
    重启时一样撞 1050。

    后来的 revision 用的是逐列判断（_add_column_if_missing），这里用逐表：
    建表与它的索引是一个整体，表在就整块跳过。
    """
    from alembic import context
    if context.is_offline_mode():
        # --sql 模式没有真连接，无从判断"表在不在"；离线产物是给人看的脚本，原样输出全部 DDL。
        return set()
    return set(sa.inspect(context.get_bind()).get_table_names())


def upgrade() -> None:
    role = _role()
    _have = _existing_tables()
    bind = op.get_bind()

    if role == "meta":
        # 配置表：恒在本地 SQLite。业务库切到 MySQL 后那边【永远】不会有这张表。
        if "setting" in _have:
            return
        op.create_table(
            "setting",
            sa.Column("key", sa.String(length=191), nullable=False),
            sa.Column("value", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("key"),
        )
        return

    # ---- role=data：业务表 ----
    if 'anime' not in _have:
        op.create_table('anime',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('bangumi_id', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=True),
        sa.Column('title', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('display_name', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('jp_name', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('season', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=False),
        sa.Column('quarter', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('air_date', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('air_weekday', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=True),
        sa.Column('total_episodes', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=True),
        sa.Column('platform', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('cover_url', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('rating', sa.Float(), nullable=True),
        sa.Column('summary', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('author', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('director', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('music', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('cast', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('confirmed', sa.Boolean(), nullable=False),
        sa.Column('rejected', sa.Boolean(), nullable=False),
        sa.Column('pref_source', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('pref_keyword', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('enrich_tries', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=False),
        sa.Column('ep_offset', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=True),
        sa.Column('last_enrich_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=True),
        sa.Column('created_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=False),
        sa.PrimaryKeyConstraint('id')
        )
        with op.batch_alter_table('anime', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_anime_bangumi_id'), ['bangumi_id'], unique=False)

    if 'anime_alias' not in _have:
        op.create_table('anime_alias',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=191).with_variant(mysql.VARCHAR(collation='utf8mb4_bin', length=191), 'mysql'), nullable=False),
        sa.Column('season', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=False),
        sa.Column('anime_id', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=False),
        sa.Column('created_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('title', 'season', name='uq_alias_title_season')
        )
        with op.batch_alter_table('anime_alias', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_anime_alias_anime_id'), ['anime_id'], unique=False)
            batch_op.create_index(batch_op.f('ix_anime_alias_title'), ['title'], unique=False)

    if 'animetorrent' not in _have:
        op.create_table('animetorrent',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('info_hash', sa.String(length=64).with_variant(mysql.VARCHAR(collation='utf8mb4_bin', length=64), 'mysql'), nullable=False),
        sa.Column('anime_id', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=False),
        sa.Column('source', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('site', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('anime_title', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('raw_title', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('season', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=False),
        sa.Column('episode', sa.Float(), nullable=False),
        sa.Column('quarter', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('status', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('download_url', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('save_path', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('release_time', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=True),
        sa.Column('priority', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=False),
        sa.Column('created_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=False),
        sa.Column('qb_state', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('qb_progress', sa.Float(), nullable=False),
        sa.Column('qb_dlspeed', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=False),
        sa.Column('qb_size', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=False),
        sa.Column('qb_synced_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=True),
        sa.Column('qb_progress_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=True),
        sa.Column('archived_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('info_hash', name='uq_animetorrent_info_hash')
        )
        with op.batch_alter_table('animetorrent', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_animetorrent_anime_id'), ['anime_id'], unique=False)

    if 'movie' not in _have:
        op.create_table('movie',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('bangumi_id', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=True),
        sa.Column('mikan_id', sa.String(length=64).with_variant(mysql.VARCHAR(collation='utf8mb4_bin', length=64), 'mysql'), nullable=True),
        sa.Column('mikan_type', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('title', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('display_name', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('jp_name', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('quarter', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('air_date', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('air_weekday', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=True),
        sa.Column('total_episodes', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=True),
        sa.Column('duration', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('platform', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('cover_url', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('rating', sa.Float(), nullable=True),
        sa.Column('summary', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('author', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('director', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('music', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('cast', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=True),
        sa.Column('rejected', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=False),
        sa.PrimaryKeyConstraint('id')
        )
        with op.batch_alter_table('movie', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_movie_bangumi_id'), ['bangumi_id'], unique=False)
            batch_op.create_index(batch_op.f('ix_movie_mikan_id'), ['mikan_id'], unique=False)

    if 'movietorrent' not in _have:
        op.create_table('movietorrent',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('info_hash', sa.String(length=64).with_variant(mysql.VARCHAR(collation='utf8mb4_bin', length=64), 'mysql'), nullable=False),
        sa.Column('movie_id', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=False),
        sa.Column('source', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('site', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('raw_title', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('status', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('download_url', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('save_path', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('release_time', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=True),
        sa.Column('priority', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=False),
        sa.Column('created_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=False),
        sa.Column('qb_state', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('qb_progress', sa.Float(), nullable=False),
        sa.Column('qb_dlspeed', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=False),
        sa.Column('qb_size', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=False),
        sa.Column('qb_synced_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=True),
        sa.Column('qb_progress_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=True),
        sa.Column('archived_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('info_hash', name='uq_movietorrent_info_hash')
        )
        with op.batch_alter_table('movietorrent', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_movietorrent_movie_id'), ['movie_id'], unique=False)

    if 'sourcegroup' not in _have:
        op.create_table('sourcegroup',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=191).with_variant(mysql.VARCHAR(collation='utf8mb4_bin', length=191), 'mysql'), nullable=False),
        sa.Column('site', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('feed', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('policy', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('priority', sa.BigInteger().with_variant(mysql.BIGINT(), 'mysql'), nullable=False),
        sa.Column('subgroups', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('title_filter', sa.Text().with_variant(mysql.TEXT(collation='utf8mb4_bin'), 'mysql'), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_sourcegroup_name')
        )
        with op.batch_alter_table('sourcegroup', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_sourcegroup_name'), ['name'], unique=False)
    if bind.dialect.name == "sqlite":
        # partial index：in-flight 集合天然极小，常态『无在下』时不必全表扫两表才能确认为空。
        # MySQL 没有 CREATE INDEX ... WHERE，那边靠上面建的普通复合索引兜。
        for t in ("animetorrent", "movietorrent"):
            op.execute(_inflight_index_sql(t))


def downgrade() -> None:
    """基线没有更早的版本可退。真要重来就删库重建（本项目不做向后迁移）。"""
    raise NotImplementedError("baseline 是起点，不支持 downgrade；要重来请删库重建")
