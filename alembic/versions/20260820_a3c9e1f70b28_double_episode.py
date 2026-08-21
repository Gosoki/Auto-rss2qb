"""补一条：animetorrent.episode 也加宽成 DOUBLE。

【为什么要单开一条，而不是改上一条】上一条 `f2b4c8e7a105` 漏了这一列，而它**已经跑过**——
alembic 的版本号一旦推进，改那条脚本对已升级的库是【完全无效】的：本地开发库、
用户的测试库都停在 head，不会再执行它。实测：往 `f2b4c8e7a105._FLOAT_COLS` 里补上 episode 之后，
真库里那一列仍然是 `float`。
**已应用的 revision 只能视为不可变；补漏一律新开一条。**

内容与上一条同理：MySQL 的 FLOAT 是 4 字节，而 episode 参与去重键（dedup_key）与集号折算；
SQLite 的 REAL 本来就是 8 字节，所以这条只对 MySQL 有意义。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = 'a3c9e1f70b28'
down_revision = 'f2b4c8e7a105'
branch_labels = None
depends_on = None

_COLS = [("animetorrent", "episode", False)]      # (表, 列, nullable)


def _role() -> str:
    from alembic import context
    return context.get_x_argument(as_dictionary=True).get("role", "data")


def upgrade() -> None:
    if _role() != "data":
        return
    from alembic import context
    bind = context.get_bind()
    if context.is_offline_mode():
        if bind.dialect.name == "mysql":
            for t, c, nul in _COLS:
                op.alter_column(t, c, type_=mysql.DOUBLE(asdecimal=False), existing_nullable=nul)
        return
    if bind.dialect.name != "mysql":
        return
    insp = sa.inspect(bind)
    have = set(insp.get_table_names())
    for t, c, nul in _COLS:
        if t not in have:
            continue
        info = {x["name"]: x for x in insp.get_columns(t)}.get(c)
        if info is None or "DOUBLE" in str(info["type"]).upper():
            continue      # 幂等：已经是 DOUBLE 就跳过
        op.alter_column(t, c, type_=mysql.DOUBLE(asdecimal=False),
                        existing_nullable=info.get("nullable", nul))


def downgrade() -> None:
    raise NotImplementedError("本项目不支持降级，见 alembic/README")
