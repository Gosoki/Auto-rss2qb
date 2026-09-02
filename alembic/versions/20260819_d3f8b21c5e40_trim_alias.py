"""把存量的超长 anime_alias.title 截断到列长，并合并因此产生的重复。

【为什么需要这条数据修复】新版本在【写入与查询两侧】都按 191 字符截断（core.anime.alias_key），
而旧代码是原样写入的：SQLite 不限长，所以老库里可能躺着 250 字符的别名。
截断只作用于新写入的话，那些老行【谁也查不到】——查询侧拿截断后的键去比全长的值，永远不等：
那部番会静默裂成两部（每来一条种子建一部新的），而日志里只有一行"番名对照登记失败"。

截断后可能撞上 (title, season) 的唯一约束：那说明这两条本来就指向同一部番的同一季，
保留 anime_id 较小的那条（更早建的、种子多半挂在它下面），删掉后来的。

MySQL 侧不需要跑：那边 VARCHAR(191) 在 STRICT 模式下根本存不进超长值（正是那条缺陷的成因）。
但这条 revision 【两种后端都跑】——判据是数据本身，没有超长行时它就是个空操作。
"""
from alembic import op
import sqlalchemy as sa

revision = 'd3f8b21c5e40'
down_revision = 'c7e1a93b4d02'
branch_labels = None
depends_on = None

_LIMIT = 191      # 与 db.dialect._COL_LEN["anime_alias.title"] 一致


def _role() -> str:
    from alembic import context
    return context.get_x_argument(as_dictionary=True).get("role", "data")


def upgrade() -> None:
    if _role() != "data":
        return
    from alembic import context
    if context.is_offline_mode():
        # --sql 模式没有真连接，读不到数据；这是一条【数据修复】revision，离线脚本里发不出来。
        # 与其发一条可能删错行的 SQL，不如什么都不发（离线产物本来就要人过目再执行）。
        print("[trim_alias] 离线(--sql)模式跳过：这是数据修复，需要连库才能判断")
        return
    bind = op.get_bind()
    if "anime_alias" not in sa.inspect(bind).get_table_names():
        return                      # 全新库：baseline 刚建完就是空表，没什么可修的
    # SQLite 的 length() 数字符，MySQL 的 LENGTH() 数字节——这里要的是字符
    n = "char_length" if bind.dialect.name == "mysql" else "length"
    rows = bind.execute(sa.text(
        f"SELECT id, title, season, anime_id FROM anime_alias WHERE {n}(title) > :lim"
    ), {"lim": _LIMIT}).fetchall()
    if not rows:
        return
    keep: dict = {}                 # (截断后的 title, season) -> 保留的那一行 id
    doomed = []
    for rid, title, season, anime_id in sorted(rows, key=lambda r: (r[3], r[0])):
        key = (title[:_LIMIT], season)
        exist = bind.execute(sa.text(
            "SELECT id FROM anime_alias WHERE title = :t AND season = :s"), 
            {"t": key[0], "s": season}).fetchone()
        if key in keep or exist is not None:
            doomed.append(rid)      # 截断后会撞唯一约束 → 删掉后来的这条
        else:
            keep[key] = rid
            bind.execute(sa.text("UPDATE anime_alias SET title = :t WHERE id = :i"),
                         {"t": key[0], "i": rid})
    for rid in doomed:
        bind.execute(sa.text("DELETE FROM anime_alias WHERE id = :i"), {"i": rid})
    print(f"[trim_alias] 截断 {len(keep)} 条超长别名，删除 {len(doomed)} 条截断后重复的")


def downgrade() -> None:
    # 截掉的字符找不回来了。与本项目其余 revision 同口径：不支持 downgrade。
    raise NotImplementedError("不支持回退：被截断的番名无法还原")
