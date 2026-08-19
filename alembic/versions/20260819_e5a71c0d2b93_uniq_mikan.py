✨feat(tests,docs): 新增测试套件并更新文档说明

📝docs(.gitignore): 更新测试套件入库说明，强调其重要性
🧪test(tests): 新增 430+ 个用例，确保功能覆盖和回归测试
🐛fix(config): 修复下载目录配置问题，确保新用户能正确设置
♻️refactor(core): 重构 SSRF 守卫逻辑，增强安全性
🔧chore(deps): 更新依赖项，确保兼容性和稳定性
"""movie.mikan_id 改唯一索引。

【为什么】_upsert_movie 是"先查后插"，mikan_id 上没有唯一约束时，两轮扫描交错就会给
同一个 Mikan 番组留下两行 Movie（进程内有 _scan_lock 挡着，跨进程没有）。重复行的表现是
"同一部片在列表里出现两次，种子分散在两行上，哪一行都不全"。

【先摘再建】存量库里可能已经有重复了，直接建唯一索引会 1062/UNIQUE failed。
处理办法是把重复组里【除保留行外】的 mikan_id 置空：保留行选"有 bgm_id 的、其次 id 最小的"
（有 bgm_id 的那行是已识别的，信息更全）。被置空的行数据全在，只是失去 Mikan 链接——
重扫该年份会重新补上，而它若与保留行本就是同一部，_upsert_movie 的身份守卫会把它合并掉。
**不自动合并**：合并会删行，而"两行是不是同一部"在这里没有可靠判据（bgm_id 可能都为空）。

只有业务表要改，故 role=meta 时整段跳过。
"""
from alembic import op
import sqlalchemy as sa

revision = 'e5a71c0d2b93'
down_revision = 'd3f8b21c5e40'
branch_labels = None
depends_on = None

_OLD = 'ix_movie_mikan_id'


def _role() -> str:
    from alembic import context
    return context.get_x_argument(as_dictionary=True).get("role", "data")


def upgrade() -> None:
    if _role() != "data":
        return
    from alembic import context
    if context.is_offline_mode():
        # --sql 模式没有真连接：读不到重复行，也查不到索引在不在。原样发出 DDL
        # （产物是给人看的脚本，重复数据要由执行的人自己先处理——本 docstring 已说明办法）。
        op.drop_index(_OLD, table_name='movie')
        op.create_index(_OLD, 'movie', ['mikan_id'], unique=True)
        return

    bind = context.get_bind()
    insp = sa.inspect(bind)
    if 'movie' not in insp.get_table_names():
        return                      # 老库还没建剧场版表（baseline 之前的分支），无事可做

    # ① 先摘重复。判据是数据本身，跑几次结果一样（幂等）。
    movie = sa.table('movie', sa.column('id', sa.Integer), sa.column('mikan_id', sa.String),
                     sa.column('bangumi_id', sa.Integer))
    dups = bind.execute(
        sa.select(movie.c.mikan_id).where(movie.c.mikan_id.is_not(None))
        .group_by(movie.c.mikan_id).having(sa.func.count() > 1)).scalars().all()
    for mid in dups:
        rows = bind.execute(
            sa.select(movie.c.id, movie.c.bangumi_id).where(movie.c.mikan_id == mid)
            # 有 bgm_id 的排前面（已识别、信息更全），其次 id 小的（先入库的）
            .order_by(movie.c.bangumi_id.is_(None), movie.c.id)).all()
        for r in rows[1:]:
            bind.execute(sa.update(movie).where(movie.c.id == r.id).values(mikan_id=None))
    if dups:
        print(f"[uniq_mikan] {len(dups)} 个重复的 mikan_id，已摘掉多余行上的链接")

    # ② 再建唯一索引。幂等：索引已是 unique 就什么都不做。
    #    【DDL 隐式提交】上面的 UPDATE 与下面的 DDL 在 MySQL 上不在同一个事务里，
    #    掉电在两者之间时版本号还没写，重跑一次即可——所以两段都必须幂等。
    idx = {i['name']: i for i in insp.get_indexes('movie')}
    cur = idx.get(_OLD)
    if cur is not None and cur.get('unique'):
        return                      # 已经是唯一索引了
    # 【不要用 batch_alter_table】SQLite 上它是"建影子表 → 拷数据 → 换名"，而重建索引用的是
    # 【反射到的旧定义】：在同一个 batch 里先 drop 再 create unique，收尾时那个非唯一的旧定义
    # 会被原样重建回去，于是索引仍是非唯一，而版本号照写 head —— 迁移声称跑过、约束根本不在。
    # 实测过。改索引不需要重建表，两种后端都支持直接 DROP + CREATE UNIQUE。
    if cur is not None:
        op.drop_index(_OLD, table_name='movie')
    op.create_index(_OLD, 'movie', ['mikan_id'], unique=True)


def downgrade() -> None:
    raise NotImplementedError("本项目不支持降级，见 alembic/README")
