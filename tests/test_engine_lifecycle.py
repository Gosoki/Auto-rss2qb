"""种子交付后的生命周期：完成回调、归档、跨表同 hash。

这三处共用 HAVE_STATUSES / TRACKED_STATUSES 等状态集合，历史上就是"某处漏了一个状态"
导致某类种子永远卡住或被误删，所以按状态逐个钉。
"""
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from core import engine
from db.models import Anime, AnimeTorrent, Movie, MovieTorrent

H = "a" * 40


@pytest.fixture
def two_tables(clean_tables):
    """在 TV 与剧场版两张表里各放一条【同一个 info_hash】的种子（同一物理种子被两条线各自持有）。"""
    def make(tv_kw, mv_kw):
        with clean_tables.get_session() as s:
            a = Anime(title="T", season=1)
            m = Movie(title="M", quarter="26C")
            s.add(a)
            s.add(m)
            s.commit()
            s.refresh(a)
            s.refresh(m)
            s.add(AnimeTorrent(anime_id=a.id, info_hash=H, raw_title="tv", episode=1,
                               created_at=datetime.now(), **tv_kw))
            s.add(MovieTorrent(movie_id=m.id, info_hash=H, raw_title="mv",
                               created_at=datetime.now(), **mv_kw))
            s.commit()
    return make


def _rows(db):
    with db.get_session() as s:
        return (s.exec(select(AnimeTorrent)).one(), s.exec(select(MovieTorrent)).one())


# ---------------- mark_done_by_hash ----------------

@pytest.mark.parametrize("status", ["sent", "downloading", "stalled"])
def test_callback_rescues_every_have_status(clean_tables, two_tables, status):
    """stalled 尤其重要：它已被 sync 脱离轮询、不再复查，这个回调是它唯一的翻身机会。"""
    two_tables({"status": "deleted"}, {"status": status, "qb_progress": 0.4})
    assert engine.mark_done_by_hash(H) is True
    _, mv = _rows(clean_tables)
    assert (mv.status, mv.qb_progress) == ("sent", 1.0)


def test_callback_walks_both_tables(clean_tables, two_tables):
    """(R1) 修前命中第一张表就 return，跨表同 hash 时剧场版那条永远救不回。"""
    two_tables({"status": "downloading", "qb_progress": 0.2},
               {"status": "stalled", "qb_progress": 0.4})
    assert engine.mark_done_by_hash(H) is True
    tv, mv = _rows(clean_tables)
    assert tv.status == "sent" and mv.status == "sent"


def test_callback_does_not_reset_the_archive_countdown(clean_tables, two_tables):
    """qB 重启会重放回调。对早就落定的行再写一次，会把 qb_synced_at 推到现在——
    而它正是归档倒计时的起点，等于让一条下完很久的种子白白推迟 N 天归档。"""
    old = datetime.now() - timedelta(days=30)
    with clean_tables.get_session() as s:
        a = Anime(title="T", season=1)
        s.add(a)
        s.commit()
        s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash=H, raw_title="tv", episode=1, status="sent",
                           qb_progress=1.0, qb_state="", qb_synced_at=old, created_at=old))
        s.commit()
    assert engine.mark_done_by_hash(H) is True     # 仍算命中
    with clean_tables.get_session() as s:
        assert s.exec(select(AnimeTorrent)).one().qb_synced_at == old


def test_callback_does_not_wipe_a_seeding_state(clean_tables):
    """(R2) 正常下完的行会被 sync 写上做种态（uploading/stalledUP…）。
    重放回调把 qb_state 抹空会让做种统计凭空掉数，还会把 qb_synced_at 推到现在、白推迟归档。"""
    old = datetime.now() - timedelta(days=30)
    with clean_tables.get_session() as s:
        a = Anime(title="T", season=1)
        s.add(a)
        s.commit()
        s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash=H, raw_title="tv", episode=1, status="sent",
                           qb_progress=1.0, qb_state="uploading", qb_synced_at=old, created_at=old))
        s.commit()
    assert engine.mark_done_by_hash(H) is True
    with clean_tables.get_session() as s:
        t = s.exec(select(AnimeTorrent)).one()
    assert (t.qb_state, t.qb_synced_at) == ("uploading", old)


def test_callback_completes_a_settle_sent_row(clean_tables):
    """(R2) settle_sent / settle_inflight_off 写的是 ("sent",1.0,"",None)——qb_synced_at 是空的，
    它们【正等着这个回调来补上真实的完成时刻】。跳过就等于让那些行永不归档
    （归档要求 qb_synced_at 非空）。"""
    with clean_tables.get_session() as s:
        a = Anime(title="T", season=1)
        s.add(a)
        s.commit()
        s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash=H, raw_title="tv", episode=1, status="sent",
                           qb_progress=1.0, qb_state="", qb_synced_at=None,
                           created_at=datetime.now()))
        s.commit()
    assert engine.mark_done_by_hash(H) is True
    with clean_tables.get_session() as s:
        assert s.exec(select(AnimeTorrent)).one().qb_synced_at is not None


@pytest.mark.parametrize("status", ["deleted", "skipped", "excluded", "error", "pending"])
def test_callback_ignores_terminal_and_undelivered(clean_tables, two_tables, status):
    """终态与还没交付的行不该被回调改活——那会让用户删掉的集自己回来。"""
    two_tables({"status": status}, {"status": status})
    assert engine.mark_done_by_hash(H) is False


@pytest.mark.parametrize("bad", ["", "xyz", "A" * 39, "g" * 40, None, "../../etc"])
def test_callback_rejects_bad_hash(clean_tables, bad):
    """hash 来自外部 query 参数，必须先校验 40hex 再碰库。"""
    assert engine.mark_done_by_hash(bad) is False


def test_callback_is_case_insensitive(clean_tables, two_tables):
    two_tables({"status": "sent", "qb_progress": 0.5}, {"status": "deleted"})
    assert engine.mark_done_by_hash(H.upper()) is True


# ---------------- 归档 ----------------

@pytest.fixture
def archivable(clean_tables):
    """一条"下完很久了"的种子，满足归档的全部条件；kw 用来逐个破坏其中一条。"""
    def make(**kw):
        old = datetime.now() - timedelta(days=30)
        fields = dict(status="sent", qb_progress=1.0, qb_state="", archived_at=None,
                      qb_synced_at=old)
        fields.update(kw)
        with clean_tables.get_session() as s:
            a = Anime(title="T", season=1)
            s.add(a)
            s.commit()
            s.refresh(a)
            s.add(AnimeTorrent(anime_id=a.id, info_hash=H, raw_title="tv", episode=1,
                               created_at=old, **fields))
            s.commit()
    return make


async def _archive(cfg, monkeypatch, deleted, **overrides):
    conf = dict(QB_ENABLED=True, QB_SYNC_STATUS=True, QB_ARCHIVE_AFTER_DAYS=7)
    conf.update(overrides)
    cfg(**conf)

    async def fake_delete(hashes, delete_files=False):
        deleted.extend(hashes)
        return True
    monkeypatch.setattr(engine.qb, "delete", fake_delete)
    return await engine.archive_old_completed()


async def test_archives_a_long_finished_torrent(archivable, cfg, monkeypatch):
    archivable()
    deleted = []
    assert await _archive(cfg, monkeypatch, deleted) == 1
    assert deleted == [H]


async def test_missing_files_is_never_archived(archivable, cfg, monkeypatch):
    """(R1) missingFiles = qB 找不到盘上的文件了。它的 qb_progress 停在最后一次同步的值，
    可能正好是 1.0 —— 归档会把种子从 qB 移除，恰恰端掉唯一的修复入口（恢复文件后重校验），
    UI 上那条醒目告警也被改写成温和的『已归档』。"""
    archivable(qb_state="missingFiles")
    deleted = []
    assert await _archive(cfg, monkeypatch, deleted) == 0
    assert deleted == []


@pytest.mark.parametrize("kw", [
    {"qb_progress": 0.9},                                   # 没下完
    {"archived_at": datetime.now()},                        # 已归档过
    {"qb_synced_at": None},                                 # 从没被 qB 确认过
    {"qb_synced_at": datetime.now()},                       # 刚下完，没到期
    {"status": "stalled"},                                  # 停滞的不是"已完成"
])
async def test_archive_preconditions(archivable, cfg, monkeypatch, kw):
    archivable(**kw)
    deleted = []
    assert await _archive(cfg, monkeypatch, deleted) == 0


async def test_archive_off_when_tracking_disabled(archivable, cfg, monkeypatch):
    """关跟踪时 qb_synced_at 写死在【交付那一刻】，归档倒计时会从交付开始算，
    N 天后会把还在下的种子从 qB 移除。没有跟踪就没有归档的语义基础。"""
    archivable()
    deleted = []
    assert await _archive(cfg, monkeypatch, deleted, QB_SYNC_STATUS=False) == 0
    assert deleted == []


def test_switching_back_to_local_keeps_the_ui_reachable_when_migration_is_broken(monkeypatch):
    """迁移本身坏掉时，「切回本地 SQLite」至少要把连接层救回来（否则自救按钮也是死的）。

    fatal 的来源之一就是"启动时建表/迁移失败"，而它的两条清除路径以前**都**要先跑一遍迁移——
    当 fatal 的根因就在 alembic 层时（一条 revision 文件坏了、一条在两个后端上都失败的 revision），
    用户唯一的自救按钮跑的正是那件失败的事：实测两个出口都抛同一个 SyntaxError。
    """
    import db

    monkeypatch.setattr(db, "upgrade_data_schema",
                        lambda: (_ for _ in ()).throw(SyntaxError("revision 文件坏了")))
    db.mark_data_fatal("启动初始化失败")

    db.switch_data_engine(None)          # 不该抛

    # engine_for(None) 建的是新引擎而不是 meta_engine 本身（同一个文件两个池），故比 URL
    assert str(db.engine.url) == str(db.meta_engine.url), "连接层没回到本地，设置页也就打不开"
    assert db.is_data_down(), "迁移仍然失败，停摆不该被解除"
    assert "迁移仍然失败" in db.data_down_reason()

    db._data_fatal = db._data_down = ""   # 还原，别污染后面的用例


def test_switching_to_another_engine_still_rolls_back_on_failure(monkeypatch, tmp_path):
    """切到【别的库】失败时仍然原样退回旧引擎——这一半的行为不变。"""
    import db

    before = db.engine
    monkeypatch.setattr(db, "upgrade_data_schema",
                        lambda: (_ for _ in ()).throw(RuntimeError("连不上")))
    with pytest.raises(RuntimeError):
        db.switch_data_engine(f"sqlite:///{tmp_path/'other.db'}")
    assert db.engine is before


async def test_reconnect_button_consumes_the_pending_init_immediately(clean_tables, monkeypatch):
    """「立即重连」探通之后要【当场】补跑业务初始化，不能等看守协程那一轮。

    probe_data_engine 一成功，is_data_down() 当场变 False，各后台循环下一次醒来就开始交付
    （写 downloading 行）。而 run_db_watch 的恢复边沿最多 30 秒后才轮到——那时它调的
    reset_downloading 打的就是**正在交付**的行：打回 pending 会当场解除集去重，
    同一集被两个源各下一份到同一目录。
    """
    from datetime import datetime

    import db as _db
    import pages.layout as L
    from core import worker

    with clean_tables.get_session() as s:
        a = Anime(title="番", quarter="26A", confirmed=True)
        s.add(a)
        s.commit()
        s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="a" * 40, raw_title="[组][01]",
                           episode=1, status="downloading", created_at=datetime.now()))
        s.commit()

    monkeypatch.setattr(worker, "_startup_reset_pending", True)
    monkeypatch.setattr(L.ui, "notify", lambda *a, **k: None)
    monkeypatch.setattr(L.ui, "navigate", type("X", (), {"reload": staticmethod(lambda: None)})())
    _db.mark_data_down("模拟停摆")

    await L._db_reconnect()

    assert worker._startup_reset_pending is False, "欠账没被消费，看守协程稍后会打到正在交付的行上"
    with clean_tables.get_session() as s:
        assert s.exec(select(AnimeTorrent)).one().status == "pending"


async def test_relocate_with_qb_disabled_changes_nothing(clean_tables, cfg, monkeypatch):
    """qB 关着时 relocate 一行都不动——原写法把已下集清成 pending「待重下」。

    那个前提不成立：qB 关着时三条下载入口第一行就 return False，flush 与两个批量补下
    实测全部返回 0，**没有任何路径能把它们下回来**。而清掉的代价是三重的：
      ① 页面照着 rep["redownload"] 提示"旧文件在 X 需你手动清理"，用户照做就删光了唯一一份；
      ② "哪些集已到手"的记录被永久抹掉；
      ③ 清成 pending 后掉出 HAVE_STATUSES，而两个删除按钮的门槛正是 HAVE ——
         那份旧文件连 UI 入口都没有了。
    """
    from datetime import datetime

    from core import anime as A

    cfg(QB_ENABLED=False, DOWN_PATH="/data")
    with clean_tables.get_session() as s:
        a = Anime(title="番", display_name="番", quarter="26A", confirmed=True)
        s.add(a)
        s.commit()
        s.refresh(a)
        for i, h in enumerate("abc"):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=h * 40, raw_title=f"[组][{i}]",
                               episode=i + 1, status="sent", qb_progress=1.0,
                               save_path="/data/26A/番", created_at=datetime.now()))
        s.commit()
        aid = a.id

    rep = await A.relocate_anime(aid, old_path="/data/26A/番")

    assert rep.get("error") and "qB 未启用" in rep["error"]
    assert not rep.get("redownload"), "qB 关着时不该把已下集清成 pending"
    with clean_tables.get_session() as s:
        rows = list(s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid)))
    assert all(t.status == "sent" and t.qb_progress == 1.0 for t in rows), \
        "已下集的状态被改写了——那份记录抹掉就再也回不来"
    assert all(t.save_path == "/data/26A/番" for t in rows), "路径不该被改成新目录"


# ---------------- (R21) SQLite 引擎的连接期设置：两条建法必须一致 ----------------

def test_every_sqlite_engine_gets_the_same_connect_time_pragmas(tmp_path):
    """两处建 SQLite 引擎（默认库 / 切到另一个 SQLite 文件）必须拿到同样的 PRAGMA。

    R21 之前只有 `_make_sqlite_engine` 挂了 PRAGMA，`engine_for()` 的 `sqlite://`
    分支是裸 `create_engine`：实测切过去的引擎是 `journal_mode=delete`。
    这是本项目第①号形状（同一个决定应该在 N 处生效、只落了 1 处）的又一例，
    而且它的后果特别隐蔽——**用例验证的引擎与生产用的引擎配置不同**。

    【判据查真实的 PRAGMA，不查源码字符串】源码里出现 "journal_mode" 只证明
    这几个字被打出来过（注释里就有），不证明连接上真的设成了 WAL。
    """
    import db
    from sqlalchemy import text

    for name, eng in (("engine_for(None)", db.engine_for(None)),
                      ("engine_for('sqlite://…')",
                       db.engine_for(f"sqlite:///{tmp_path/'other.db'}"))):
        with eng.connect() as c:
            jm = c.execute(text("PRAGMA journal_mode")).scalar()
            bt = c.execute(text("PRAGMA busy_timeout")).scalar()
        assert str(jm).lower() == "wal", f"{name} 拿到的是 journal_mode={jm}，不是 WAL"
        assert int(bt) >= 5000, f"{name} 的 busy_timeout={bt}，低于 5 秒"
        eng.dispose()


def test_the_two_sqlite_paths_share_one_factory():
    """反向：上一条只要"结果对"，两处各自复制一份 PRAGMA 也能过。

    这里钉住"只有一个工厂"这件事本身——复制粘贴出来的第二份，下次只会被改掉一半。
    用 AST 查 `engine_for` 里对 sqlite 分支真实调用了哪个函数（不是字符串匹配：
    上面那条 docstring 里就写着 `create_engine` 三个字）。
    """
    import ast
    import inspect
    import db

    tree = ast.parse(inspect.getsource(db.engine_for))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "create_engine" not in called, \
        "engine_for 又自己裸调 create_engine 了——SQLite 的连接期设置会漏掉一半"
    assert "_sqlite_engine" in called, "engine_for 的 sqlite 分支没走共用工厂"


# ---------------- (R21) "连不上" 与 "schema 不对" 必须分得开 ----------------

@pytest.mark.parametrize("msg,is_conn", [
    # SQLite（本项目默认后端）把这些全抛成 OperationalError —— 光看异常类分不出来
    ("(sqlite3.OperationalError) no such table: animetorrent", False),
    ("(sqlite3.OperationalError) no such column: animetorrent.retry_at", False),
    ("(sqlite3.OperationalError) database is locked", False),
    ("(sqlite3.OperationalError) unable to open database file", True),
    ("(sqlite3.OperationalError) disk I/O error", True),
    # MySQL 侧
    ("(pymysql.err.OperationalError) (1054, \"Unknown column 'x' in 'field list'\")", False),
    ("(pymysql.err.ProgrammingError) (1146, \"Table 'a.b' doesn't exist\")", False),
    ("(pymysql.err.OperationalError) (1205, 'Lock wait timeout exceeded')", False),
    ("(pymysql.err.OperationalError) (2003, \"Can't connect to MySQL server\")", True),
    ("(pymysql.err.OperationalError) (2006, 'MySQL server has gone away')", True),
    ("(pymysql.err.InterfaceError) (0, '')", True),
])
def test_only_real_connection_failures_take_the_whole_site_down(msg, is_conn):
    """页面兜底靠这个判据决定要不要把全站标成停摆。

    标错的代价是**四条后台循环集体丢轮次**，而且会翻转：`no such table` 被判成"连不上"
    → 全站停摆 → 看守协程的 `SELECT 1` 在同一个文件上必然成功 → 停摆被解除
    → 下一次渲染再标一次。用户拿到的是恒定错误的诊断和一句永远兑现不了的"会自动恢复"。
    """
    import db
    assert db.looks_like_connection_error(Exception(msg)) is is_conn, msg


def test_the_page_fallback_actually_consults_that_judgement():
    """反向：上一条只测了判据函数本身，测不出"页面到底有没有用它"。

    用 AST 查 `pages/layout.py` 里那个 `except (OperationalError, InterfaceError)` 处理块
    真的调了它——不是字符串匹配（本文件与生产代码的注释里都写着这个函数名）。
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path("pages/layout.py").read_text(encoding="utf-8"))
    handlers = [h for h in ast.walk(tree) if isinstance(h, ast.ExceptHandler)
                and h.type is not None
                and "OperationalError" in ast.dump(h.type)]
    assert handlers, "没找到那个 except 块，用例的前提坏了"
    called = {n.func.attr for h in handlers for n in ast.walk(h)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "looks_like_connection_error" in called, \
        "页面兜底没问过判据，又变回『凡是 OperationalError 就当连不上』了"


# ---------------- (R21) 启动时【只升业务数据真正所在的那个库】 ----------------

def test_a_mysql_backend_never_migrates_the_unused_local_database(tmp_path):
    """`DB_BACKEND=mysql` 时，本地那份 SQLite 不该被跑一遍 data 迁移链。

    启动顺序是 `init_db()` → `config.load_from_db()` → `apply_configured_backend()`，
    而模块级 `engine` 在头两步里恒等于 `meta_engine`（本地 SQLite）。
    R21 之前 `init_db()` 跟着调 `upgrade_data_schema()`，于是 MySQL 用户每次启动都会
    对一个**完全不用**的本地库跑整条 data 链。两条真实代价：
      · 链里有两条【改数据】的 revision（删重复 anime_alias、把重复行的 mikan_id 置空），
        跑在那份陈旧的本地业务表上——而 `db/backup.py` 明写它"可能是 MySQL 出问题后
        唯一剩下的番剧数据副本"；
      · 那份库上的迁移失败会穿透到 `mark_data_fatal`（"启动初始化失败，系统停摆"），
        而真正的业务库此刻连试都还没试过。

    起子进程跑，因为要的是【一个干净进程的完整启动序列】：本进程里 db 早就 import 过、
    engine 也已经指向别处，在里面测等于什么都没测。
    """
    import os
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path

    p = tmp_path / "local.db"
    code = ("import config, db;"
            "db.init_db();"
            "config.load_from_db();"
            "config._v['DB_BACKEND']='mysql';"
            "config._v['DB_MYSQL_HOST']='';"      # 参数不全 → 走停摆分支，不去连真 MySQL
            "db.apply_configured_backend()")
    r = subprocess.run([sys.executable, "-c", code],
                       env={**os.environ, "DB_PATH": str(p)},
                       cwd=str(Path(__file__).resolve().parent.parent),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]

    con = sqlite3.connect(p)
    try:
        tabs = {t[0] for t in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    assert tabs == {"setting", "alembic_version_meta"}, (
        f"业务数据在 MySQL，本地库却被建/升了这些表：{sorted(tabs - {'setting', 'alembic_version_meta'})}")


def test_a_sqlite_backend_still_gets_its_business_tables(tmp_path):
    """反向：上一条只证明"没建"，证明不了默认后端下业务表还建得出来。

    把 data 链从 `init_db()` 挪到 `apply_configured_backend()` 时，最容易出的错
    就是"挪走了、但新家那一支忘了加"——那样默认后端会启动即无表。
    """
    import os
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path

    p = tmp_path / "local.db"
    r = subprocess.run(
        [sys.executable, "-c",
         "import config, db; db.init_db(); config.load_from_db(); db.apply_configured_backend()"],
        env={**os.environ, "DB_PATH": str(p)},
        cwd=str(Path(__file__).resolve().parent.parent), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]

    con = sqlite3.connect(p)
    try:
        tabs = {t[0] for t in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    missing = {"anime", "animetorrent", "anime_alias", "movie", "movietorrent",
               "sourcegroup", "alembic_version_data"} - tabs
    assert not missing, f"默认后端下业务表没建出来：{sorted(missing)}"


# ---------------- (R21) 整库维护闸 ----------------

def test_maintenance_blocks_every_business_session(clean_tables):
    """维护期间 `get_session()` 一律拒绝 —— 全仓 300+ 处业务读写全部走它。

    挡的是这件事：迁移是"先 DELETE 清空目标 → 按 500 行一批【显式带 id】INSERT"，
    中途任何一条并发插入都会占住一个即将被显式 id 写入的号 →
    `IntegrityError: UNIQUE constraint failed`，而目标库停在"清空 + 写了一半"。
    以前的互斥只有 `worker._poll_lock` + `_scan_lock` 两把轮次锁，
    页面上的写入口（补齐该源 / 新增源组 / 绑定 bgm）完全不受约束 —— 作用域比约束小。
    """
    import db

    with db.get_session():          # 维护外：正常
        pass
    with db.maintenance("正在迁移数据"):
        assert db.maintenance_reason() == "正在迁移数据"
        assert db.is_data_down(), "维护期间后台四条循环必须按停摆跳过本轮"
        with pytest.raises(db.DatabaseBusy):
            db.get_session()
    assert not db.maintenance_reason()
    assert not db.is_data_down(), "维护结束必须自动解除，不能把系统留在停摆里"
    with db.get_session():          # 维护后：恢复
        pass


def test_maintenance_is_released_even_when_the_work_blows_up(clean_tables):
    """维护块里抛异常时闸也必须落下 —— 否则一次失败的迁移会把整站永久锁死。"""
    import db

    with pytest.raises(RuntimeError):
        with db.maintenance("正在迁移数据"):
            raise RuntimeError("迁移中途炸了")
    assert not db.maintenance_reason(), "异常路径上闸没落下，整站被永久锁死"
    with db.get_session():
        pass


def test_maintenance_refuses_to_nest(clean_tables):
    """两个维护同时开始 = 两边各自以为独占。第二个必须被拒绝，而不是悄悄覆盖。"""
    import db

    with db.maintenance("正在迁移数据"):
        with pytest.raises(db.DatabaseBusy):
            with db.maintenance("正在切库"):
                pass
        assert db.maintenance_reason() == "正在迁移数据", "外层的理由被内层覆写了"
    assert not db.maintenance_reason()


def test_an_in_flight_delivery_blocks_maintenance(clean_tables):
    """交付协程在半途时必须拒绝切库/迁移。

    交付的时序是：进锁置 `downloading` → `await` 取种(最长 180 秒)+加 qB → 按
    **整数主键**回写。而 `db/transfer.py` 明确保留主键 —— 两个库里 id=501 是两条
    毫不相干的种子。维护窗口横在这中间时，回写落进另一个库的另一行：
    那一集被静默标成"已交付"（∈HAVE_STATUSES，集去重从此永远挡着），而盘上什么都没有。
    `db.maintenance()` 挡得住维护【期间】的读写，挡不住"await 跨过整个窗口、结束后才回写"，
    所以开始之前必须先确认没有这类协程在半途。
    """
    from core import engine as ce

    assert ce.maintenance_blockers() == []
    with clean_tables.get_session() as s:
        a = Anime(title="T", season=1, confirmed=True)
        s.add(a)
        s.commit()
        s.refresh(a)
        row = AnimeTorrent(anime_id=a.id, info_hash="c" * 40, raw_title="t",
                           episode=1, status="downloading")
        s.add(row)
        s.commit()
        s.refresh(row)
        tid = row.id
    # 【光有 downloading 行还不够】(R24) 落库的 downloading 只说明"某进程某一刻开始交付"，
    # 不说明"此刻真的有协程在管"。真在途的会登记进 `engine._delivering`；
    # 没登记的是**残骸**（交付途中库抖了一下、回写没成功）——
    # 而残骸永远不会消失，只按状态列数的话一条残骸就把切库/迁移永久拒死。
    assert ce.maintenance_blockers() == [], \
        "只有一条没人管的 downloading 残骸，不该挡住维护"
    with ce.delivering(AnimeTorrent, tid):
        blockers = ce.maintenance_blockers()
        assert blockers and "交付中" in blockers[0], blockers
    assert ce.maintenance_blockers() == [], "交付结束后闸没跟着放开"


def test_both_maintenance_buttons_consult_the_blocker_list():
    """反向：上一条只测了闸本身，测不出"两个按钮到底有没有问过它"。

    切库与迁移是两个独立的处理器 —— 这正是本项目第①号形状（同一个约束应该在 N 处
    生效、只落了 1 处）最容易复发的地方。用 AST 查两个处理器真的调了它。
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path("pages/settings.py").read_text(encoding="utf-8"))
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in ("_switch_backend", "_migrate"):
        assert name in fns, f"没找到处理器 {name}，用例的前提坏了"
        # 【连"被当参数传进去"的也要算】`run.io_bound(engine.maintenance_blockers)`
        # 里那个名字是个 ast.Attribute 值、不是 ast.Call 的 func —— 第一版只看调用节点，
        # 于是明明接上了却判红。同一件事的两种写法都得认。
        attrs = {n.attr for n in ast.walk(fns[name]) if isinstance(n, ast.Attribute)}
        assert "maintenance_blockers" in attrs, f"{name} 没问过在途闸"
        assert "maintenance" in attrs, f"{name} 没把动作放进维护窗口"


def test_maintenance_itself_refuses_when_something_is_in_flight():
    """把关必须在 `maintenance()` 里，不能只在调用方。

    调用方"先查一遍再进维护"的写法，中间隔着一个 `await confirm(...)`
    ——用户点确认要几秒到几分钟，窗口大得能开进一整轮交付。
    """
    import db

    with pytest.raises(db.DatabaseBusy) as ei:
        with db.maintenance("正在迁移数据", blocked_by=lambda: ["有 1 条番剧种子正在交付中"]):
            pass
    assert "交付中" in str(ei.value)
    assert not db.maintenance_reason(), "被拒绝时不该把闸留在置位状态"


def test_both_handlers_pass_the_blocker_into_maintenance():
    """反向：两个处理器都必须把 blocked_by 传进去 —— 少传一个就等于那条路没把关。"""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("pages/settings.py").read_text(encoding="utf-8"))
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in ("_switch_backend", "_migrate"):
        calls = [n for n in ast.walk(fns[name])
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "maintenance"]
        assert calls, f"{name} 里没有 db.maintenance(...)"
        for c in calls:
            kw = {k.arg for k in c.keywords}
            assert "blocked_by" in kw, f"{name} 的 db.maintenance() 没传 blocked_by"


# ---------------- (R22) 在途闸的两处补齐 ----------------

async def test_a_running_collection_round_blocks_maintenance(clean_tables):
    """采集轮 / 剧场版扫描轮在半途时也必须拒绝维护。

    `process_item` 是跨 await 持整数主键的典型：
    `anime_id = await _resolve_anime(item)`（enrich 预算 120 秒）→ 出 await 之后
    新开会话按那个 id 写种子行。而 `transfer` 保留主键 ——
    切库之后那一行会带着**旧库的主键**写进新库。

    R21 把这条约束留给了调用方"记得拿锁"：`_migrate` 拿了、`_switch_backend` 一把都没拿，
    标准的第①号形状（R22 端到端复现过：采集轮卡在 enrich 的 await 上时闸放行）。
    现在收进判据，调用方不必再记得任何事。
    """
    from core import anime as A
    from core import engine as ce
    from core import worker as W

    assert ce.maintenance_blockers() == []
    async with W._poll_lock:
        blockers = ce.maintenance_blockers()
        assert any("采集轮" in b for b in blockers), blockers
    async with W._scan_lock:
        blockers = ce.maintenance_blockers()
        assert any("剧场版扫描轮" in b for b in blockers), blockers
    async with A._enrich_lock:
        blockers = ce.maintenance_blockers()
        assert any("重识别" in b for b in blockers), blockers
    assert ce.maintenance_blockers() == [], "锁放开之后闸没跟着放开"


def test_the_blocker_list_covers_every_lock_that_spans_an_await():
    """在途闸必须覆盖**每一条**跨 await 持业务库主键的后台线。

    R21 只列了两条（downloading 行 / qB 同步）；R22 补了采集轮与剧场版扫描轮，
    而**第五条（延迟重识别）又漏了** —— 它单轮最多 50 部、每部
    `await enrich.resolve()`（预算 120 秒，全项目最长的 await 之一），出 await 后
    按同一个整数主键写回。同一个函数上第①号形状连中两次。

    这条守卫的判据：仓库里所有"轮次锁"（模块级 `asyncio.Lock`，名字以 `_lock` 结尾）
    都必须被 `maintenance_blockers` 看见。新加一把锁却忘了登记，这里会红。
    """
    import ast
    import inspect
    from pathlib import Path

    from core import engine as ce

    root = Path(__file__).resolve().parent.parent
    known = set()
    for mod in ("core/worker.py", "core/anime.py", "core/movies.py"):
        tree = ast.parse((root / mod).read_text(encoding="utf8"))
        for n in tree.body:                      # 只看模块级
            if not (isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)):
                continue
            f = n.value.func
            if isinstance(f, ast.Attribute) and f.attr == "Lock":
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id.endswith("_lock"):
                        known.add(t.id)
    assert known, "一把轮次锁都没找到，用例的前提坏了"

    src = inspect.getsource(ce.maintenance_blockers)
    # 两条交付锁豁免：交付本身已经由 `status == "downloading"` 那一条判据覆盖
    # （进锁即置位、成败都会自己写回），而这两把锁只在【无 await 的那一小段】里持有。
    # 番剧侧 core/anime.py 叫 _download_lock、剧场版侧 core/movies.py 叫 _dl_lock ——
    # 名字不一样这件事本身也记在 GLOSSARY 的对称实现那一节。
    exempt = {"_download_lock", "_dl_lock"}
    missing = sorted(known - exempt - {k for k in known if k in src})
    assert not missing, (
        f"这些轮次锁没被在途闸看见：{missing}。"
        "它们各自代表一条跨 await 持业务库主键的线，漏一条就等于那条线上的切库不设防")


def test_the_blocker_check_does_not_kill_the_escape_hatch(monkeypatch):
    """库连不上时，在途预检必须返回空表而不是抛异常。

    `switch_data_engine` 的 docstring 明写"切回本地 SQLite"是 fatal/停摆时的
    **唯一自救出口**（"至少要让连接层回到本地，好让用户看得到设置页"）。
    而这个预检要对着 `db.engine` 发两条 COUNT —— 正是那台连不上的 MySQL。
    第一版没接异常：MySQL 一挂，这个 R21 新加的预检就把自救出口堵死了
    （实测弹『切换失败』，engine 一步都没动）。

    库都连不上，本来也不可能有在途交付；真正的把关在 `db.maintenance(blocked_by=...)`。
    """
    from sqlalchemy.exc import OperationalError

    from core import engine as ce

    def boom():
        raise OperationalError("SELECT 1", {}, Exception("Can't connect to MySQL server"))

    # 【必须打在 core.engine 上，不是 db 上】engine 是 `from db import get_session` 直接绑的名字，
    # 打 db.get_session 根本不影响它 —— 第一版就是这么写的，于是 boom 一次都没被调用，
    # 用例测的是"正常路径返回空表"，而那是恒真的。变异（把 return [] 改成 raise）没被抓到才发现。
    monkeypatch.setattr(ce, "get_session", boom)
    called = {"n": 0}
    orig = boom

    def counting():
        called["n"] += 1
        return orig()
    monkeypatch.setattr(ce, "get_session", counting)

    assert ce.maintenance_blockers() == [], "库连不上时预检抛了异常，自救出口被堵死"
    assert called["n"] == 1, "补丁没生效 —— 这条用例根本没走到那个 except"


def test_migrate_does_not_take_the_locks_it_is_judged_by():
    """`_migrate` 不能自己拿那两把轮次锁 —— 拿了就会被自己判成"采集轮正在跑"。

    这是把约束从"调用方记得拿锁"收进判据之后必然要跟着改的一半：
    只改判据、不改调用方，迁移会永远开不起来（自锁）。
    """
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parent.parent / "pages/settings.py")
                     .read_text(encoding="utf8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_migrate")
    dumped = ast.dump(fn)
    assert "_poll_lock" not in dumped and "_scan_lock" not in dumped, \
        "_migrate 又自己拿轮次锁了 —— 它会被 maintenance_blockers 判成忙，永远开不起来"


# ---------------- (R24) 交付残骸：既要清得掉，又不能误伤在途 ----------------

async def test_a_db_blip_during_delivery_does_not_wedge_the_row_forever(clean_tables,
                                                                       monkeypatch, cfg):
    """交付途中库抖一下，行不能永久卡在 `downloading`。

    `download_anime_torrent` 在锁内原子置位 + commit，出锁 `await` 取种(最长 180s)+投递，
    回来再写库。**回写这一步撞上 OperationalError**（MySQL 重启 / 连接被切 /
    `MYSQL_READ_TIMEOUT=15` 切断慢查询 / 锁等待）时异常直接冒出函数，行就停在 downloading。
    而这一状态**没有任何在线恢复路径**：
      · `_sync_qb_status` 显式跳过 downloading 行（"交付协程独占"）；
      · `downloading ∈ HAVE_STATUSES` ⇒ 集去重认定该集已有一份，flush/补下永不再挑；
      · `run_db_watch` 的恢复边沿调 `init_business_state(reset_leftovers=False)`（运行中掉线那一支
        该标志恒为 False，有意为之），**不复位**。
    只有重启进程才清得掉。而 R22 把 downloading 收进在途闸之后又多一条：
    设置页的『切库』『迁移』从此**永久**被拒，提示还写着"等它跑完（最多几分钟）再来"。
    """
    from core import engine as ce
    from db.models import Anime, AnimeTorrent

    with clean_tables.get_session() as s:
        a = Anime(title="T", season=1, confirmed=True)
        s.add(a); s.commit(); s.refresh(a)
        row = AnimeTorrent(anime_id=a.id, info_hash="e" * 40, raw_title="x",
                           episode=1, status="downloading")   # 上一次交付留下的残骸
        s.add(row); s.commit()

    assert ce.maintenance_blockers() == [], "残骸不该挡住维护"
    assert ce.sweep_stale_delivering() == 1, "残骸没被清扫掉"
    with clean_tables.get_session() as s:
        assert s.exec(select(AnimeTorrent)).one().status == "pending", \
            "残骸没回到待下 —— 这一集永远不会再被下"


async def test_the_sweep_never_touches_a_real_in_flight_delivery(clean_tables, cfg):
    """反向：**真在途**的那一行绝不能被清扫碰到。

    打回 pending 会当场解除集去重 —— 同一集会被另一个源再下一份到**同一个目录**，
    而交付协程回来还会把它写回 sent。这正是 `reset_downloading` 那段长注释警告的事。
    """
    from core import engine as ce
    from db.models import Anime, AnimeTorrent

    with clean_tables.get_session() as s:
        a = Anime(title="T", season=1, confirmed=True)
        s.add(a); s.commit(); s.refresh(a)
        row = AnimeTorrent(anime_id=a.id, info_hash="f" * 40, raw_title="x",
                           episode=1, status="downloading")
        s.add(row); s.commit(); s.refresh(row)
        tid = row.id

    with ce.delivering(AnimeTorrent, tid):
        assert ce.sweep_stale_delivering() == 0, "把正在交付的那一行清掉了"
        with clean_tables.get_session() as s:
            assert s.exec(select(AnimeTorrent)).one().status == "downloading"


def test_both_delivery_paths_register_and_unregister():
    """两条交付路径都必须登记 + 注销 —— 少一处，那条线上的残骸就永远清不掉。

    注销放在**外层包装的 finally** 里：函数体从进锁到最后一次回写有一百多行、
    多条 return 与 raise，任何一条漏掉注销都会让那一行被永久当成"正在交付中"。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for mod, pub, inner in (("core/anime.py", "download_anime_torrent",
                             "_download_anime_torrent_inner"),
                            ("core/movies.py", "download_movie_torrent",
                             "_download_movie_torrent_inner")):
        tree = ast.parse((root / mod).read_text(encoding="utf8"))
        fns = {n.name: n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert pub in fns and inner in fns, f"{mod} 少了包装或内层"
        outer = ast.dump(fns[pub])
        assert "discard" in outer and "_delivering" in outer, f"{mod}::{pub} 没注销交付登记"
        tries = [n for n in ast.walk(fns[pub]) if isinstance(n, ast.Try) and n.finalbody]
        assert tries, f"{mod}::{pub} 的注销不在 finally 里 —— 异常路径上会漏"
        assert "_delivering" in ast.dump(fns[inner]), f"{mod}::{inner} 没登记交付"
