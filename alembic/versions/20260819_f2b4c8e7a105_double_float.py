"""浮点列在 MySQL 上改成 DOUBLE（8 字节）。

【为什么】MySQL 的 FLOAT 是 4 字节：qB 报的 0.4212345 存进去读回来是 0.42123448848724365。
而 sync_qb_status 判"进度有没有推进"用的是 `qb_progress > prev_progress`，round-trip
精度损失让它在进度【冻结】时也有约一半概率为真 → qb_progress_at 每轮刷新 →
`status = "stalled"` 永远走不到。后果不是少一个徽标：该行恒满足 _inflight_where
（qB 同步循环永不休眠），而它仍在 HAVE_STATUSES 里、集去重闸一直挡着 ⇒ 同集换源永远不会发生。
用户看到的是一集永远卡在 42%、界面零告警。

SQLite 的 REAL 本来就是 8 字节，所以这条 revision 【只对 MySQL 有意义】；SQLite 上是空操作。
已存的值不会因 ALTER 恢复精度，所以升级后每条在下种子会多刷新一次 qb_progress_at
（多一个轮询周期的宽限），一轮之后自然收敛。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = 'f2b4c8e7a105'
down_revision = 'e5a71c0d2b93'
branch_labels = None
depends_on = None

# 表 → 该表要加宽的浮点列。
# 【这张表漏了 animetorrent.episode】而本条已经跑过 —— 已应用的 revision 是不可变的，
# 往这里补列对已升级的库【完全无效】（实测：补了之后真库里那一列仍是 float）。
# 补漏另开了一条 20260820_a3c9e1f70b28_double_episode.py。
# 这里保持原样，是为了让脚本描述的与它【实际执行过】的一致。
# tests/test_mysql_compat.py 有一条用例断言"所有加宽 revision 合起来覆盖全部浮点列"。
_FLOAT_COLS = {
    "animetorrent": ["qb_progress"],
    "movietorrent": ["qb_progress"],
    "anime": ["rating"],
    "movie": ["rating"],
}


# 各列在模型里的 nullable 声明（离线模式读不到库，只能按模型来；在线模式从 inspect 取真值）
_NULLABLE = {
    ("animetorrent", "qb_progress"): False,
    ("movietorrent", "qb_progress"): False,
    ("anime", "rating"): True, ("movie", "rating"): True,
}


def _role() -> str:
    from alembic import context
    return context.get_x_argument(as_dictionary=True).get("role", "data")


def upgrade() -> None:
    if _role() != "data":
        return
    from alembic import context
    bind = context.get_bind()
    if context.is_offline_mode():
        # --sql 模式没有真连接：拿不到方言以外的信息，按 MySQL 原样发出（SQLite 侧本来就不需要）
        if bind.dialect.name == "mysql":
            for t, cols in _FLOAT_COLS.items():
                for c in cols:
                    # 【existing_nullable 不能一律写 True】MySQL 的 MODIFY 是【整列重定义】：
                    # 漏掉 NOT NULL 就等于把它摘掉。qb_progress 与 episode 都是 NOT NULL，
                    # --sql 产物照着执行会静默放宽约束。离线模式读不到库，只能按模型的声明来。
                    op.alter_column(t, c, type_=mysql.DOUBLE(asdecimal=False),
                                    existing_nullable=_NULLABLE[(t, c)])
        return
    if bind.dialect.name != "mysql":
        return            # SQLite 的 REAL 已经是 8 字节，无事可做

    # 幂等：已经是 DOUBLE 就跳过。中途掉电重跑也安全（DDL 隐式提交，与 baseline 同款理由）。
    insp = sa.inspect(bind)
    have = set(insp.get_table_names())
    for t, cols in _FLOAT_COLS.items():
        if t not in have:
            continue
        cur = {c["name"]: c for c in insp.get_columns(t)}
        for c in cols:
            info = cur.get(c)
            if info is None or "DOUBLE" in str(info["type"]).upper():
                continue
            op.alter_column(t, c, type_=mysql.DOUBLE(asdecimal=False),
                            existing_nullable=info.get("nullable", True))


def downgrade() -> None:
    raise NotImplementedError("本项目不支持降级，见 alembic/README")
