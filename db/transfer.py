"""数据迁移：把业务表在两个库之间整体搬运（SQLite ⇄ MySQL，双向同一套代码）。

与"切换数据库"的分工：
  · switch_data_engine —— 只改连接，一行数据都不动（换到已有数据的库、或先建空库再迁）
  · migrate_data       —— 把源库的业务数据复制到目标库（连接不变，搬完要不要切由调用方决定）

设计要点：
  · 【保留主键】anime_id / movie_id 是跨表的逻辑外键（模型里没声明 FK，靠 id 关联），
    id 一变整个库的关联全断。故显式带 id 插入，插完再把 MySQL 的 AUTO_INCREMENT 顶到 max(id)+1，
    否则下一条新行会从 1 开始撞主键。
  · 【表序】先父后子（anime → alias/torrent，movie → torrent），失败时中途停下也不会留下
    指向不存在父行的孤儿。
  · 【分块】每批 500 行，避免一次性把几万行 executemany 塞进一个事务（MySQL 有
    max_allowed_packet，SQLite 有变量数上限）。
  · 【不搬 setting】配置恒留本地 SQLite，见 db.__init__ 的双引擎说明。
  · 【幂等/可重跑】默认要求目标表为空，避免主键冲突半途炸掉留下半个库；overwrite=True 时
    先清空目标业务表再搬。
"""
import logging
import os

import sqlalchemy as sa
from sqlmodel import SQLModel

from .dialect import is_mysql, quote

log = logging.getLogger("autorss")

# 复制顺序：父表在前。虽然模型没声明 FK，但 anime_id/movie_id 是逻辑外键，
# 中断时按这个序至少不会留下"子行指向还没搬过来的父行"。
TABLE_ORDER = ("sourcegroup", "anime", "anime_alias", "animetorrent", "movie", "movietorrent")

_CHUNK = 500


def _table(name):
    return SQLModel.metadata.tables[name]


def same_database(a, b) -> bool:
    """两个 Engine 是不是指向【同一个物理库】。对象身份不算数，看连接目标。

    SQLite 比 realpath（相对路径、软链、./ 前缀都能指向同一个文件）；
    其余按 (方言, 主机, 端口, 库名) 比，主机大小写不敏感、端口取默认值。
    """
    ua, ub = a.url, b.url
    if ua.get_backend_name() != ub.get_backend_name():
        return False
    if ua.get_backend_name() == "sqlite":
        da, dbn = ua.database, ub.database
        if not da or not dbn:            # 内存库（:memory:）各自独立
            return da == dbn
        return os.path.realpath(da) == os.path.realpath(dbn)
    return ((ua.host or "").lower(), ua.port or 3306, ua.database) == \
           ((ub.host or "").lower(), ub.port or 3306, ub.database)


def count_rows(engine, tables=TABLE_ORDER) -> dict:
    """各业务表的行数（表不存在记 0）。用于迁移前后比对与"目标库是否为空"的判断。"""
    out = {}
    insp = sa.inspect(engine)
    with engine.connect() as conn:
        for name in tables:
            if not insp.has_table(name):
                out[name] = 0
                continue
            out[name] = conn.execute(
                sa.select(sa.func.count()).select_from(_table(name))).scalar_one()
    return out


def _reset_autoincrement(engine, name: str) -> None:
    """把 MySQL 的 AUTO_INCREMENT 顶到 max(id)+1。

    带显式 id 插入不会推进 AUTO_INCREMENT 计数器，不修的话下一条新行会从 1 开始、
    立刻撞上已存在的主键（表现为迁移后"一采集就报 Duplicate entry"）。
    SQLite 的 rowid 自增取的是 max(rowid)+1，天然不需要处理。
    """
    if not is_mysql(engine):
        return
    t = _table(name)
    if "id" not in t.c:
        return
    with engine.begin() as conn:
        mx = conn.execute(sa.select(sa.func.max(t.c.id))).scalar()
        conn.exec_driver_sql(
            f"ALTER TABLE {quote(engine, name)} AUTO_INCREMENT = {int(mx or 0) + 1}")


def _readable_source_tables(src_engine) -> set:
    """【清空目标之前】先真读一次源库：每张表按模型的完整列取一行。返回可读的表名集合。

    非做不可的理由是执行顺序：真正的 `select(t)`（带模型全部列）在复制循环里才第一次发出，
    而那已经在 overwrite 清空目标【之后】——源库缺表/缺列时异常抛在那一刻，结果是目标数据
    已被删光、源库一行都没搬进来，且不可恢复。这里把同样的读提前到删数据之前。

    count_rows 兜不住这一层：它对缺表直接记 0（当成空表），也从不碰具体的列。
    缺表按"0 行"跳过（与 count_rows 同口径，UI 确认框里那句"源 N 行"已如实告知用户）；
    表在、却读不出模型要的列（源库版本过旧，而迁移有意不升级源库）则直接抛，中止整次迁移。
    """
    insp = sa.inspect(src_engine)
    ok = set()
    with src_engine.connect() as conn:
        for name in TABLE_ORDER:
            if not insp.has_table(name):
                continue
            try:
                conn.execute(sa.select(_table(name)).limit(1)).fetchall()
            except Exception as e:
                raise ValueError(
                    f"源库的表 {name} 读不出来，迁移中止（目标库未被改动）："
                    f"{type(e).__name__}: {str(e).splitlines()[0][:120]}。"
                    "多半是源库版本过旧、缺少新版才有的列——用本程序打开它跑一次升级后再迁。") from e
            ok.add(name)
    return ok


def _dup_unique_values(src_engine, dst_engine) -> list:
    """源库里有哪些值会撞上目标库的唯一约束。返回 ["表.列: 值 x 有 N 行", …]。

    【与 _overlong_values 是同一件事的另一半】那道预检问「值塞不塞得下」，这道问「值撞不撞车」。
    两道都必须在清空目标之前跑，理由一模一样：迁移是"先清空目标、再逐批写入"，
    撞车会让它炸在中间某张表上，留下一个半个库。

    【为什么以前不需要】以前业务表上一条唯一约束都没有（除了主键与 sourcegroup.name）。
    movie.mikan_id 的唯一索引是新加的，而它【自动对迁移生效】——加约束的那一轮只验了
    「建库」与「升级」两条路径，没人想到第三条。这正是"约束的作用域比验证的作用域大"。

    判据取自【目标库反射出来的】唯一索引/约束，不是硬编码的列名：以后再加唯一约束，
    这道预检自动跟上，不必再想起来改这里。
    """
    insp_dst = sa.inspect(dst_engine)
    bad = []
    for name in TABLE_ORDER:
        if not insp_dst.has_table(name) or not sa.inspect(src_engine).has_table(name):
            continue
        t = _table(name)
        uniques = [ix["column_names"] for ix in insp_dst.get_indexes(name) if ix.get("unique")]
        uniques += [uc["column_names"] for uc in insp_dst.get_unique_constraints(name)]
        with src_engine.connect() as conn:
            for cols in uniques:
                cols = [c for c in cols if c and c in t.c]
                if not cols:
                    continue          # 目标库上有、源库模型里没有的列：跳过而不是崩
                key = [t.c[c] for c in cols]
                # NULL 不参与唯一性（两种后端一致），所以只查全部非空的那些行
                q = sa.select(*key, sa.func.count()).where(
                    sa.and_(*[c.is_not(None) for c in key])).group_by(*key).having(sa.func.count() > 1)
                for row in conn.execute(q):
                    vals = "+".join(str(v)[:40] for v in row[:-1])
                    bad.append(f"{name}.{'+'.join(cols)}: {vals} 有 {row[-1]} 行")
    return bad


def _overlong_values(src_engine, dst_engine) -> list:
    """源库里有哪些值塞不进目标库的定长列。返回 ["表.列: 最长 N > 上限 M（k 行超限）", …]。

    【为什么必须在清空目标之前查】迁移是"先清空目标、再逐批写入"，而 MySQL 在
    STRICT_TRANS_TABLES 下遇到超长值是【报错】不是截断。没有这道预检的话，一条 250 字符的
    畸形番名会让整件事炸在第三张表上——此时目标库已经被清空、前两张表已经写进去了，
    留下一个半个库：anime 全在、animetorrent 还空。用户再点一次『切换』就会把 RSS 窗口
    整批当成新种子重下一遍。

    只查目标库真有长度限制的列（SQLite 侧 VARCHAR 不限长，所以目标是 SQLite 时这里恒为空）。

    【清空之前一共三道预检】源表读得出来(_readable_source_tables) / 值塞得下(本函数) /
    值不撞唯一约束(_dup_unique_values)。以后再给业务表加任何一类约束，都要问一句
    "它会不会让写入中途失败"——会的话就得在这里多一道，否则又是一个半个库。
    """
    insp_dst = sa.inspect(dst_engine)
    src_insp = sa.inspect(src_engine)
    bad = []
    for name in TABLE_ORDER:
        if not insp_dst.has_table(name) or not src_insp.has_table(name):
            continue
        t = _table(name)
        limits = {}
        for col in insp_dst.get_columns(name):
            n = getattr(col["type"], "length", None)
            if n:
                limits[col["name"]] = n
        if not limits:
            continue
        with src_engine.connect() as conn:
            for col, lim in limits.items():
                if col not in t.c:
                    continue
                c = t.c[col]
                # 【两个坑，都是"只在一种方言上正确"】这条预检两个迁移方向都要跑
                # （SQLite→MySQL 与 MySQL→SQLite），任何一半只对一种方言就等于另一个方向崩：
                #   ① 计数不能用 .filter()：SQLAlchemy 会原样发出 `count(*) FILTER (WHERE ...)`，
                #      那是 SQLite/PG 语法，MySQL 9.7 上直接 1064。用 CASE。
                #   ② 长度不能用 length()：**MySQL 的 LENGTH() 数的是字节**，而我们比的是目标库的
                #      【字符】上限。实测 LENGTH('番剧名字')=12 而 CHAR_LENGTH=4——于是一条合法的
                #      191 字符中文别名（573 字节）会被误判成超限，把 MySQL→SQLite 这条唯一的
                #      退路永久堵死，错误文案还指引用户去删正常数据。
                #      SQLite 的 length() 数的本来就是字符、且它没有 char_length()，所以按方言分。
                chars = (sa.func.char_length(c) if conn.dialect.name == "mysql"
                         else sa.func.length(c))
                row = conn.execute(sa.select(
                    sa.func.max(chars),
                    sa.func.coalesce(sa.func.sum(
                        sa.case((chars > lim, 1), else_=0)), 0))).first()
                mx, over = (row[0] or 0), (row[1] or 0)
                if over:
                    bad.append(f"{name}.{col}: 最长 {mx} > 上限 {lim}（{over} 行超限）")
    return bad


def migrate_data(src_engine, dst_engine, *, overwrite: bool = False,
                 progress=None) -> dict:
    """把业务表从 src 复制到 dst。

    返回 {"moved": {表名: 实际写入行数}, "src_before": {表名: 迁移【开始前】源库行数}}。
    src_before 必须带出来给 verify 用——不能让 verify 事后现查源库，那有个自证陷阱：
    万一源和目标其实是同一个库，overwrite 已经把它清空了，现查两边都是 0，反而"校验通过"。
    （别把它塞进 moved 里当一个键：那样 sum(moved.values()) 这种自然写法会当场炸。）

    overwrite=False 且目标已有数据 → 直接抛，别在"目标非空"时半途撞主键留下残局。
    progress(table, done, total) 可选回调，供 UI 显示进度。
    """
    # 【比连接目标，不比对象身份】同一个物理库完全可以有两个不同的 Engine 对象
    #（调用方按同一串 URL 又 create_engine 了一次），那时 `is` 判等不出来，
    # 而 overwrite 会先把目标清空、再从"已经空了的源"读出 0 行——数据当场蒸发且伪装成成功。
    # 这是删数据之前的最后一道闸，将来任何调用方算错方向都必须炸在这里。
    if same_database(src_engine, dst_engine):
        raise ValueError("源库与目标库指向同一个数据库，无需迁移（也不能迁，会把它清空）")

    # 【顺便升级】目标库先升到最新版本再往里灌数据：
    #   · 全新空库 → 一路建表到 head，用户不必先切过去建一遍再回来迁；
    #   · 老版本的库 → 补齐缺的 revision，免得拿新模型的列往旧表结构里插而报 Unknown column。
    # 源库【不动】：迁移不该顺手改用户还在用的那一头，它自己启动时会升。
    from . import schema
    schema.upgrade(dst_engine, "data")

    src_counts = count_rows(src_engine)
    readable = _readable_source_tables(src_engine)   # 删目标之前先把源库真读一遍，读不动就在这里中止
    # 【同样必须在清空之前】数据宽度不合法时中止，别留半个库（见 _overlong_values）
    too_long = _overlong_values(src_engine, dst_engine)
    if too_long:
        raise ValueError(
            "源库里有值超过目标库的列长上限，迁移中止（目标库的【数据未被改动】，"
            "但表结构已按新版本升级过——那一步在预检之前）：\n  "
            + "\n  ".join(too_long)
            + "\n这些多半是解析畸形标题产生的垃圾番名。新版本入库时已自动截断，"
              "但老数据要先处理掉：到『番剧表』里找到它们删掉或改名，再重新迁移。")
    # 【第三道，同样在清空之前】唯一约束撞车。见 _dup_unique_values。
    dups = _dup_unique_values(src_engine, dst_engine)
    if dups:
        raise ValueError(
            "源库里有值会撞上目标库的唯一约束，迁移中止（目标库的【数据未被改动】，"
            "但表结构已按新版本升级过——那一步在预检之前）：\n  "
            + "\n  ".join(dups)
            + "\n最常见的是老库里同一个 Mikan 番组留下了两行剧场版。"
              "办法：先用本程序把【源库】当业务库打开一次，启动时的升级会自动摘掉重复的链接，再回来迁。")
    dst_counts = count_rows(dst_engine)
    # 【第四道：空源 + 覆盖 = 纯破坏，没有任何正当用途】
    # 真实形态：用户先切到了 MySQL（于是本地 SQLite 的业务表一直是空的），过后"为保险"
    # 再点一次『本地 SQLite → MySQL』—— 目标就是他当前正在用的那个库，六张表被 DELETE 干净，
    # 再从空源写入 0 行；而 verify 拿 0==0 判"一致"，页面弹的是绿色的「迁移完成并校验一致」。
    # 用户唯一可能察觉的时刻，反而确认了一切正常。而 DB_BACKEND=mysql 时备份的 scope 恒是
    # meta，业务数据一条都不在备份里 —— 没有任何退路。
    if overwrite and not any(src_counts.values()) and any(dst_counts.values()):
        busy = "、".join(f"{k} {v} 行" for k, v in dst_counts.items() if v)
        raise ValueError(
            f"源库是空的（0 行），而目标库有数据（{busy}）——这样迁只会把目标库清空。"
            "已中止，目标库的数据未被改动。\n"
            "如果你是想把数据【从目标库搬回源库】，请点另一个方向的迁移按钮。")
    if not overwrite and any(dst_counts.values()):
        busy = "、".join(f"{k} {v} 行" for k, v in dst_counts.items() if v)
        raise ValueError(f"目标库已有数据（{busy}）。请先勾选『覆盖目标库』，或换一个空库。")

    if overwrite:
        # 逆序清空（先子后父），语义上更干净；这些表之间没有真 FK，顺序其实不影响执行
        with dst_engine.begin() as conn:
            for name in reversed(TABLE_ORDER):
                conn.execute(sa.delete(_table(name)))
        log.info("数据迁移：已清空目标库业务表")

    moved = {}
    for name in TABLE_ORDER:
        t = _table(name)
        total = src_counts.get(name, 0)
        done = 0
        if name not in readable:      # 源库压根没这张表 → 按 0 行处理（同 count_rows 口径）
            moved[name] = 0
            continue
        cols = list(t.c.keys())
        with src_engine.connect() as sconn:
            # 按主键排序取，保证分页稳定；stream_results 让大表不必一次读进内存
            order = t.c.id if "id" in t.c else t.c[cols[0]]
            result = sconn.execution_options(stream_results=True, yield_per=_CHUNK).execute(
                sa.select(t).order_by(order))
            while True:
                rows = result.fetchmany(_CHUNK)
                if not rows:
                    break
                payload = [dict(zip(cols, r)) for r in rows]
                with dst_engine.begin() as dconn:
                    dconn.execute(sa.insert(t), payload)   # 显式带 id，保住跨表关联
                done += len(payload)
                if progress:
                    progress(name, done, total)
        _reset_autoincrement(dst_engine, name)
        moved[name] = done
        log.info("数据迁移：%s %d 行", name, done)
    return {"moved": moved, "src_before": src_counts}


def verify(src_engine, dst_engine, src_counts: dict | None = None) -> list:
    """迁完逐表比行数，返回不一致的说明（空列表=完全一致）。

    src_counts 传【迁移开始前】的源库行数快照（migrate_data 会一并返回）。
    不传就现查源库——那样有个致命的自证陷阱：万一源和目标其实是同一个库，
    overwrite 已经把它清空了，现查两边都是 0，0==0 反而"校验通过"。
    """
    a = src_counts if src_counts is not None else count_rows(src_engine)
    b = count_rows(dst_engine)
    return [f"{k}: 源 {a.get(k, 0)} 行 / 目标 {b[k]} 行" for k in TABLE_ORDER if a.get(k, 0) != b[k]]
