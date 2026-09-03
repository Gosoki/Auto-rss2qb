"""存量库的升级路径。

**本项目的缺陷有相当比例只在这条路径上出现**：第 9 轮五条 P1 里三条如此
（迁移预检、baseline 的 partial index、老库的唯一约束），而当时全套用例用的都是新建库——
新建库一路建到 head，永远碰不到"老结构 + 新代码"。

这里的 upgrade_from fixture 造的是**真正的旧库**（升到某个中间 revision 就停），
不是"建到 head 再把版本号改回去"——后者的表结构其实还是新的。
"""
import threading

import sqlalchemy as sa
from sqlmodel import Session

from db import schema
from db.models import Movie


def test_every_revision_script_loads():
    """所有 revision 脚本都能被 alembic 加载 —— 挡"文件坏了导致应用整个起不来"。

    实测踩过：一条 commit message 被写进了 revision 文件开头，
    于是 alembic 加载整个 versions 目录时 SyntaxError，应用起不来。
    全套用例当时确实会报错，但报的是三条不相干用例的 ERROR，看不出根因。
    """
    from alembic.script import ScriptDirectory
    revs = list(ScriptDirectory(str(schema._ROOT / "alembic")).walk_revisions())
    assert len(revs) >= 5
    assert schema.head_revision() == revs[0].revision


def test_legacy_db_with_preexisting_indexes_self_heals(tmp_path):
    """老库：表和索引都在、版本行丢了 → 必须自愈到 head，不能 fatal。

    Alembic 化之前的 db.__init__._migrate_inflight_indexes 每次启动都建同名的
    ix_*_inflight，所以任何一个老库原地升级都会撞上"index already exists"——
    不需要任何竞态。撞上就是 mark_data_fatal，而 fatal 只能人工解除：每次重启同一个死循环。
    """
    p = tmp_path / "legacy.db"
    eng = sa.create_engine(f"sqlite:///{p}")
    schema.upgrade(eng, "data")
    with eng.begin() as c:                      # 模拟老库：表和索引都在，版本行没了
        c.execute(sa.text("DELETE FROM alembic_version_data"))
    with eng.connect() as c:
        idx = [r[0] for r in c.execute(sa.text(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE '%inflight%'"))]
    assert len(idx) == 2, "用例前提不成立：那两条 partial index 不在"

    schema.upgrade(eng, "data")                 # 再升一次：不能抛
    assert schema.current_revision(eng, "data") == schema.head_revision()


def test_old_db_with_duplicate_mikan_ids_upgrades_cleanly(upgrade_from):
    """停在加唯一约束之前的库，里面有重复 mikan_id → 升级要自动摘掉多余的链接。

    保留"有 bgm_id 的、其次 id 最小的"那一行；其余置空。**不自动合并**——
    合并会删行，而"两行是不是同一部"在迁移里没有可靠判据。
    """
    eng = upgrade_from("d3f8b21c5e40")           # uniq_mikan 的前一版
    with Session(eng) as s:
        s.add(Movie(title="重复A", mikan_id="1000", quarter="26A"))
        s.add(Movie(title="重复B", mikan_id="1000", quarter="26A", bangumi_id=777))
        s.add(Movie(title="重复C", mikan_id="1000", quarter="26A"))
        s.add(Movie(title="独立", mikan_id="2000", quarter="26A"))
        s.commit()

    upgrade_from.to_head(eng)

    with Session(eng) as s:
        rows = {m.title: m.mikan_id for m in s.exec(sa.select(Movie)).scalars()}
    assert rows["重复B"] == "1000", "保留的应该是有 bgm_id 的那一行"
    assert rows["重复A"] is None and rows["重复C"] is None
    assert rows["独立"] == "2000", "没有重复的不该被动"
    with eng.connect() as c:                     # 约束真的生效了
        uniq = [r[0] for r in c.execute(sa.text(
            "SELECT name FROM pragma_index_list('movie') WHERE \"unique\"=1")) if "mikan" in r[0]]
    assert uniq, "唯一索引没建上"


def test_old_db_with_overlong_aliases_gets_trimmed(upgrade_from):
    """停在 trim_alias 之前的库，里面有超长别名 → 升级要截断并合并截断后重复的。"""
    from db.models import Anime, AnimeAlias
    eng = upgrade_from("c7e1a93b4d02")           # trim_alias 的前一版
    long_a = "番" * 250
    long_b = "番" * 254                           # 截断到 191 后与上面相同
    with Session(eng) as s:
        a = Anime(title="长名番", quarter="26A", confirmed=True)
        s.add(a)
        s.commit()
        s.refresh(a)
        s.add(AnimeAlias(title=long_a, anime_id=a.id))
        s.add(AnimeAlias(title=long_b, anime_id=a.id))
        s.commit()

    upgrade_from.to_head(eng)

    with Session(eng) as s:
        titles = [x.title for x in s.exec(sa.select(AnimeAlias)).scalars()]
    assert len(titles) == 1, f"截断后重复的没合并：{[len(t) for t in titles]}"
    assert len(titles[0]) == 191


def test_concurrent_upgrades_are_serialised(tmp_path):
    """两个线程同时跑 alembic 必须被串行化。

    alembic 的 EnvironmentContext 装的是**进程级模块代理**，两次升级重叠时后退出的那个
    `del globals_[attr]` 会删掉对方的代理。而本项目真的会并发调它：schema.upgrade 有 5 个
    调用点，其中两个在【非事件循环线程】里（看守协程每 30 秒的探测、页面『立即重连』），
    还有一个是 run.io_bound 里的整库迁移。

    去掉锁之后在副本上实测 6 次：4 次留下「alembic_version=head 而唯一索引没建上」
    （而 upgrade() 开头那句 `if cur == head: return` 让这一态**永远不会被修复**），
    2 次整进程 SIGSEGV。加锁后 60 线程零异常。
    """
    import sqlalchemy as sa

    from db import schema

    eng = sa.create_engine(f"sqlite:///{tmp_path / 'race.db'}")
    errs = []

    def _go():
        try:
            schema.upgrade(eng, "data")
        except Exception as e:                       # noqa: BLE001
            errs.append(f"{type(e).__name__}: {e}")

    ts = [threading.Thread(target=_go) for _ in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert not errs, f"并发升级抛了异常：{errs[:3]}"
    assert schema.current_revision(eng, "data") == schema.head_revision()
    with eng.connect() as c:                         # DDL 真的跑完了，不只是版本号推进
        uniq = [r[0] for r in c.execute(sa.text(
            'SELECT name FROM pragma_index_list("movie") WHERE "unique"=1')) if "mikan" in r[0]]
    assert uniq, "版本号到了 head，但唯一索引没建上——这一态永远不会被自动修复"


# ---------------- role 闸的广度守卫（R18） ----------------

def test_every_revision_gates_on_role():
    """每条 revision 的 upgrade() 都必须自己判 role —— 而且**漏写不会报错**，只会静默做错事。

    本项目的迁移是两条 role 分链（`alembic_version_meta` / `alembic_version_data`），
    **同一个 revision 在两个引擎上各跑一次**，脚本内部靠 `_role()` 决定自己该做哪一半。
    漏写这道闸时，一条本该只动业务表的迁移会在 meta 引擎上也跑一遍：
    · 默认布局下两个引擎指着**同一个 SQLite 文件**，所以本地看不出任何异常；
    · 切了 MySQL 之后，meta 引擎指向本地那个只有 setting 表的库，
      迁移要么在"表不存在"上炸掉、要么（更糟）安静地把业务表建到配置库里。
    而无论哪种，**没有任何现有机制会说话**——docs/DECISIONS.md 的 E-1 讨论要不要
    干脆收成一条链，正是因为"每条新 revision 都要记得手抄这道闸，而漏写拦不住"。
    E-1 怎么定是另一回事；在它定下来之前，至少让漏写当场变红。

    豁免：确实与 role 无关的 revision（两边都要跑同样的事）在下面登记并写清理由。
    """
    import pathlib
    import ast
    import re

    # revision id → 为什么这条不需要 role 闸
    _ROLE_AGNOSTIC: dict[str, str] = {}

    offenders = []
    for f in sorted(pathlib.Path("alembic/versions").glob("*.py")):
        src = f.read_text(encoding="utf8")
        rev = re.search(r"^revision(?::\s*str)?\s*=\s*['\"]([^'\"]+)", src, re.M)
        rev = rev.group(1) if rev else f.name
        if rev in _ROLE_AGNOSTIC:
            continue
        # 【必须用 AST 只看 upgrade()，不能切源码字符串】(R22 修)
        # 第一版是 `src[src.index("def upgrade("):]` —— 这一刀切到**文件末尾**，
        # 把 `def downgrade()` 一起装了进去；判据又只是子串匹配（注释里的同名词同样满足）。
        # 而 `b2c9e4f17a03_retry.py` 与 `c7e1a93b4d02_finish_idle.py` 的 downgrade() 里
        # 各有一句 `if _role() != "data": return` —— 这两条恰恰是最危险的加列 revision。
        # 实测：把 upgrade() 里那句闸删掉，守卫仍判绿；
        # 照着抄一条新 revision、只在 downgrade 写闸，同样判绿 ——
        # 而它会在 role=meta 那一遍把业务表建进【配置库】，正是本守卫 docstring 自己写的那句话。
        tree = ast.parse(src)
        up = next((n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == "upgrade"), None)
        if up is None:
            offenders.append(f"{f.name}（revision {rev}）没有 upgrade()")
            continue
        # 闸的形态：调用本脚本的 _role()，或直接读 x_argument。查【真实调用节点】，
        # 注释与字符串都不再算数。
        gated = any(
            (isinstance(n.func, ast.Name) and n.func.id == "_role")
            or (isinstance(n.func, ast.Attribute) and n.func.attr == "get_x_argument")
            for n in ast.walk(up) if isinstance(n, ast.Call))
        if not gated:
            offenders.append(f"{f.name}（revision {rev}）的 upgrade() 里没有 role 判定")
    assert not offenders, (
        "这些迁移没有 role 闸，会在【两个引擎上各跑一次】而没人拦得住：\n  "
        + "\n  ".join(offenders)
        + "\n若某条确实与 role 无关，把它的 revision id 登记进本用例的 _ROLE_AGNOSTIC 并写清理由。")


def test_the_role_gate_guard_is_not_vacuous():
    """反向守卫：上面那条不能因为"读不到脚本"而空跑成绿。"""
    import pathlib
    files = list(pathlib.Path("alembic/versions").glob("*.py"))
    assert len(files) >= 5, f"只找到 {len(files)} 个 revision 脚本，上面那条守卫可能在空扫"
    assert any("_role()" in f.read_text(encoding="utf8") for f in files), \
        "一个脚本都没有 _role()，说明闸的形态变了，守卫的判据要跟着改"


def test_each_role_upgrades_its_own_database_to_head_and_touches_nothing_else(tmp_path):
    """(E-1，2026-09-02 拍板选 B) 两条 role 链各自在【分开的】空库上真跑到 head，核终态。

    上面那条 AST 守卫挡得住"忘了写闸"，挡不住"闸写反了"：把某条 data revision 的
    `if _role() != "data": return` 抄成 `!= "meta"`，AST 上照样"有 role 判定"（变异实测绿）。
    只有真跑才分得出：闸写反的那条会在 meta 库上对着不存在的业务表 ALTER（炸）、
    或把业务列建进配置库、而 data 库缺一段。默认布局两个引擎共用一个 SQLite 文件，
    所以这里必须用两个文件 —— 那正是切了 MySQL 之后的形状。
    【边界】(R34 对抗审计) 闸**整段删掉**时这条单独看仍绿：逐步幂等的 revision（`if table in have`）在
    meta 库上是 no-op。那种漏法由上面的 AST 守卫接住；两条互补，别删任何一条。
    """
    import sqlite3

    from db import META_TABLES, schema
    from db.models import SQLModel  # noqa: F401  注册表结构

    meta_eng = sa.create_engine(f"sqlite:///{tmp_path / 'meta.db'}")
    data_eng = sa.create_engine(f"sqlite:///{tmp_path / 'data.db'}")
    schema.upgrade(meta_eng, "meta")
    schema.upgrade(data_eng, "data")
    assert schema.current_revision(meta_eng, "meta") == schema.head_revision()
    assert schema.current_revision(data_eng, "data") == schema.head_revision()

    def tables(eng):
        con = sqlite3.connect(eng.url.database)
        try:
            return {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        finally:
            con.close()

    business = {t for t in SQLModel.metadata.tables if t not in META_TABLES}
    assert tables(meta_eng) == set(META_TABLES) | {"alembic_version_meta"}, tables(meta_eng)
    assert tables(data_eng) == business | {"alembic_version_data"}, tables(data_eng)
    # data 库要长成模型说的样子：每张业务表的列集合与模型一致（加列 revision 漏跑一条这里就红）
    insp = sa.inspect(data_eng)
    for t in business:
        want = {c.name for c in SQLModel.metadata.tables[t].columns}
        have = {c["name"] for c in insp.get_columns(t)}
        assert want == have, f"{t}：模型 {sorted(want - have)} 缺 / 多出 {sorted(have - want)}"


# ---------------- (R21) alembic 产出的库要与【模型声明的索引】一致 ----------------

def test_the_migrated_database_has_every_index_the_models_declare(upgrade_from):
    """模型上每一个 `index=True` / `Index(...)`，升级完的库里都得真有一个同名索引。

    【第一版这条用例是假的，留着当反面教材】原本写成"全新库 vs 升级上来的库，索引集合相同"——
    而本项目**只有一条建库路径**：全新库同样是一路 alembic 建过去的（见 db/schema.upgrade
    的 docstring："全新库会一路建过去"），没有 `create_all` 那一支。
    于是那条断言是拿同一条链的产物跟自己比，**永远成立**：
    把 revision 里的索引名改错、甚至整段删掉剧场版那一半，它照样全绿（实测两次变异都没红）。

    真正的不变式是"库要长成模型说的样子"，所以这里拿 `SQLModel.metadata` 当基准。
    它同时挡住两种错法：
      · revision 手写的索引名与 SQLModel 自动生成的 `ix_<表>_<列>` 不一致
        —— 此后任何按名字判断"有没有这个索引"的代码（包括 revision 自己的幂等闸）都会错；
      · 两张表只加了一张（第①号形状）。
    """
    import sqlite3

    from sqlmodel import SQLModel

    import db.models  # noqa: F401  只为注册表结构

    eng = upgrade_from("41170d6a7ad4")      # 从 baseline 起
    upgrade_from.to_head(eng)

    con = sqlite3.connect(eng.url.database)
    try:
        got = {t: {r[1] for r in con.execute(f"PRAGMA index_list({t})")}
               for t in (r[0] for r in con.execute(
                   "SELECT name FROM sqlite_master WHERE type='table' "
                   "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'alembic_%'"))}
    finally:
        con.close()

    missing, extra = [], []
    checked = 0
    for name, table in SQLModel.metadata.tables.items():
        if name not in got:
            continue        # meta 侧的表（setting）不在业务库里
        declared = {ix.name for ix in table.indexes}
        for ix in table.indexes:
            checked += 1
            if ix.name not in got[name]:
                missing.append(f"{name}.{ix.name}")
        # 【反向：不许多出模型没声明的】(R34 对抗审计) E-48 那条 revision 的 drop 只有离线分支被
        # 回放器看着；线上分支把 `if name in …` 写反成 `not in`，SQLite 真升上来两个索引一个没掉、
        # 版本号照写，全套仍绿。sqlite_autoindex_* 是唯一约束自带的，ix_*_inflight 是方言手建的 partial index。
        for ix_name in got[name]:
            if ix_name.startswith("sqlite_autoindex_") or ix_name.endswith("_inflight"):
                continue
            if ix_name not in declared:
                extra.append(f"{name}.{ix_name}")
    assert checked >= 8, f"只比对了 {checked} 个索引，模型注册多半没生效"
    assert not missing, ("升级完的库里缺这些模型声明过的索引（多半是 revision 写错了名字、"
                         "或者只加了两张表中的一张）：" + "、".join(missing))
    assert not extra, "升级完的库里多出模型没声明的索引（drop 的 revision 没真的 drop？）：" + "、".join(extra)



def test_trim_alias_keeps_the_row_its_docstring_promises(upgrade_from):
    """(R22) `trim_alias` 保留的必须是 **anime_id 较小**的那条 —— 与它的 docstring 一致。

    docstring 写的是「保留 anime_id 较小的那条（更早建的、种子多半挂在它下面）」，
    而实现是 `sorted(rows, key=lambda r: r[0])`（r[0] 是 anime_alias.id），
    循环里解包出来的 `anime_id` 一次都没用到。

    两条超长别名截断后撞同一个 (title, season)、而它们各自指向的番在 anime 表里的先后顺序
    与别名行的先后顺序相反时，活下来的映射指向的是【后建的那部番】——
    而按 docstring 的立论，种子挂在先建的那部下面：**该番名的新种子全部落到另一部番上，
    老集数留在原来那部，番静默裂成两半**，日志里只有一行「截断 N 条…删除 M 条」。
    触发面窄，但这是一条【改数据且不可回退】的 revision，判据与文档必须说同一件事。
    """
    import sqlalchemy as sa

    eng = upgrade_from("c7e1a93b4d02")          # 停在加约束之前
    with eng.begin() as c:
        # 直接照表的实际非空列插，别一个个试出来
        cols = [r[1] for r in c.exec_driver_sql("PRAGMA table_info(anime)").fetchall()]
        notnull = {r[1]: r[4] for r in c.exec_driver_sql("PRAGMA table_info(anime)").fetchall()
                   if r[3] and r[5] == 0}     # NOT NULL 且非主键
        for aid, name in ((7, "老番"), (3, "新番")):
            vals = {"id": aid, "title": name}
            for col in notnull:
                if col in vals:
                    continue
                vals[col] = 0 if col in ("season", "confirmed", "rejected",
                                         "enrich_tries", "finish_optout") else ""
            keys = ", ".join(vals)
            ph = ", ".join(f":{k}" for k in vals)
            c.execute(sa.text(f"INSERT INTO anime ({keys}) VALUES ({ph})"), vals)
        long1, long2 = "甲" * 250, "甲" * 251     # 截断到 191 之后是同一个 title
        c.execute(sa.text(
            "INSERT INTO anime_alias (id, title, season, anime_id, created_at) "
            "VALUES (1, :t, 1, 7, '2026-01-01 00:00:00')"), {"t": long1})
        c.execute(sa.text(
            "INSERT INTO anime_alias (id, title, season, anime_id, created_at) "
            "VALUES (2, :t, 1, 3, '2026-01-01 00:00:00')"), {"t": long2})
    upgrade_from.to_head(eng)

    with eng.connect() as c:
        rows = c.exec_driver_sql(
            "SELECT id, anime_id FROM anime_alias ORDER BY id").fetchall()
    assert len(rows) == 1, f"截断后应只剩一条，实际 {rows}"
    assert rows[0][1] == 3, (
        f"活下来的别名指向 anime_id={rows[0][1]}，而 docstring 承诺的是 anime_id 较小的那条(3)")


def test_alembic_never_builds_its_own_engine(tmp_path, monkeypatch):
    """(R27) 跑 DDL 的连接必须是**我们建的**那个引擎，不能让 alembic 自己建。

    `alembic/env.py` 在拿不到 `config.attributes["connection"]` 时会
    `engine_from_config(...)` 自己建一个 —— 那个引擎**只有一个 URL**，
    `db.make_mysql_engine` 设的 `connect_timeout=5` 一条都不生效。

    后果不是"慢一点"：`schema.upgrade()` 的调用点全包在 `db.maintenance()` 里，
    对着一台**关机**的 MySQL 升级时，建连接会挂到操作系统的 TCP 超时（分钟级），
    这期间 `get_session()` 对全站恒抛 `DatabaseBusy`（七个页面停在"数据库维护中"），
    而本模块的进程级 `_LOCK` 又被那个线程占着 —— 『切回本地 SQLite』这条自救出口
    同样拿不到锁。两个出口一起死，只能重启进程。
    """
    from alembic import command

    seen = {}
    real = command.upgrade

    def spy(cfg, target, **kw):
        seen["conn"] = cfg.attributes.get("connection")
        return real(cfg, target, **kw)

    monkeypatch.setattr(command, "upgrade", spy)
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    schema.upgrade(eng, "data")
    assert seen.get("conn") is not None, \
        "没把引擎交给 alembic —— 它会自己建一个不带任何超时参数的"
    assert hasattr(seen["conn"], "connect"), \
        "env.py 写的是 `connectable.connect()`，传进去的必须是 Engine 不是 Connection"
    eng.dispose()


def test_the_ddl_engine_for_mysql_has_a_connect_timeout_but_no_query_timeout(monkeypatch):
    """MySQL 的 DDL 引擎：握手要有上界，查询**不能**有。

    `read_timeout` 是每次 socket 读写的上界，而一条 DDL（大表加索引）合法地跑几分钟
    很正常 —— 套上它会把正常迁移拦腰切断，那正是"表结构已经改了一半"最不能被打断的地方
    （与整库迁移用 `query_timeout=False` 同理）。这里要的只是连不上时快点失败。
    """
    from db import schema as S

    calls = []
    made = sa.create_engine("sqlite://")     # 真引擎（E-50 要往上挂 connect 事件），不连 MySQL

    def fake_make(url, query_timeout=True):
        calls.append(query_timeout)
        return made

    monkeypatch.setattr("db.make_mysql_engine", fake_make)
    fake_url = sa.engine.url.make_url(
        "mysql+pymysql://u:p@127.0.0.1:3306/x")
    holder = type("E", (), {"url": fake_url})()
    with S._ddl_engine(holder) as e:
        assert e is made, "MySQL 分支没有另建 DDL 引擎"
    assert calls == [False], f"DDL 引擎的 query_timeout 参数不对：{calls}"


def test_the_ddl_engine_bounds_metadata_lock_waits(monkeypatch):
    """(E-50，2026-09-02 拍板) DDL 连接一建立就 `SET SESSION lock_wait_timeout = 60`。

    MySQL 默认是一年。被元数据锁挡住的 ALTER 跑在 db.maintenance() 里，期间全站 DatabaseBusy、
    『切回本地』也拿不到 schema._LOCK —— 两个出口同时死。这里不连库：把 connect 事件对着一个
    假的 DBAPI 连接触发一次，核它发了什么。
    """
    import db as D
    from db import schema as S

    made = sa.create_engine("sqlite://")
    monkeypatch.setattr("db.make_mysql_engine", lambda url, query_timeout=True: made)
    holder = type("E", (), {"url": sa.engine.url.make_url("mysql+pymysql://u:p@127.0.0.1:3306/x")})()

    sent = []

    class _Cur:
        def execute(self, sql, *a):
            sent.append(sql)

        def __getattr__(self, name):        # fetchone/close 之类：方言自己的探测语句
            return lambda *a, **k: None

    class _Conn:
        def cursor(self):
            return _Cur()

        def __getattr__(self, name):        # 方言自己的 on_connect（create_function 之类）一律吞掉
            return lambda *a, **k: None

    with S._ddl_engine(holder) as e:
        # 触发的是 SQLAlchemy 的 connect 事件（pool 拿到新 DBAPI 连接时那一次）
        e.pool.dispatch.connect(_Conn(), None)
    assert D.MYSQL_DDL_LOCK_WAIT_TIMEOUT == 60, "拍板的值是 60 秒"
    ours = [q for q in sent if "lock_wait_timeout" in str(q)]
    assert ours == [f"SET SESSION lock_wait_timeout = {D.MYSQL_DDL_LOCK_WAIT_TIMEOUT}"], sent


def test_sqlite_reuses_the_callers_engine(tmp_path):
    """SQLite 不另建：本地文件没有握手这回事，busy_timeout 已经在 `_sqlite_engine` 里设过。"""
    from db import schema as S

    eng = sa.create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    with S._ddl_engine(eng) as e:
        assert e is eng
    eng.dispose()
