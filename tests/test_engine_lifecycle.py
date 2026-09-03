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
        # 【必须把 discard 钉在 finally 体上】(R28) 原来的两条断言是分开的：
        # "outer 里有 discard" + "outer 里有带 finalbody 的 Try" —— 两条各自成立，
        # 却**没有任何一条把它们绑在一起**。实测变异：把
        # `try: return await _inner(...) / finally: discard(...)` 改成
        # `try: _r = await _inner(...); discard(...); return _r / finally: pass`，
        # 四条断言逐条仍然成立，全量 1197 一条红都没有 —— 而异常路径从此不注销，
        # 交付回写撞上 OperationalError 时那一行永远留在 `_delivering` 里：
        # `sweep_stale_delivering()` 因 `is_delivering()` 为真永远跳过它（行永久停在
        # downloading、∈HAVE_STATUSES、集去重认定已有一份），`maintenance_blockers()`
        # 又永远数到它 —— 切库/迁移被**永久**拒死。正是 R25 §一③ 那条 P1。
        tries = [n for n in ast.walk(fns[pub]) if isinstance(n, ast.Try) and n.finalbody]
        assert tries, f"{mod}::{pub} 没有 try/finally"
        in_finally = any("discard" in ast.dump(node)
                         for t in tries for node in t.finalbody)
        assert in_finally, (
            f"{mod}::{pub} 的 discard 不在 finally 体里 —— 异常路径上不会注销，"
            "那一行会被永久当成『正在交付中』")
        assert "_delivering" in ast.dump(fns[inner]), f"{mod}::{inner} 没登记交付"


async def test_the_archive_and_sweep_rounds_block_maintenance():
    """(R27) 归档轮与巡检轮在跑的时候，在途闸必须说话。

    这两条与另外五条是同一种形状：**跨 await 持业务库的整数主键**。
      · 归档：读 `(id, info_hash)` → `await qb.delete()`（单次上限 45 秒）
              → 出 await 后 `s.get(model_cls, tid)` 按主键写 `archived_at`；
      · 巡检：读 `Anime.id` → `await notify_event()`（每条上限 NOTIFY_TIMEOUT）
              → 回来 `s.get(Anime, a.id)` 写 `finished_at`。
    而 `db/transfer.py` **保留主键**：维护窗口横在中间时，回写落进另一个库的另一行。

    【为什么现成的守卫抓不到】`test_the_blocker_list_covers_every_lock_that_spans_an_await`
    的判据是"每一把模块级轮次锁都必须被闸看见" —— 而这两条线以前**一把锁都没有**，
    于是既不在闸里、也不在守卫的视野里。约束的作用域比验证的作用域小（第②种形状）。
    """
    from core import engine as ce
    from core import worker as W

    assert ce.maintenance_blockers() == [], "起点就不干净，下面的断言说明不了任何事"
    async with W._archive_lock:
        assert any("归档" in r for r in ce.maintenance_blockers()), \
            "归档轮在跑，闸却说可以切库"
    assert ce.maintenance_blockers() == [], "归档轮结束后闸没跟着放开"
    async with W._sweep_lock:
        assert any("巡检" in r for r in ce.maintenance_blockers()), \
            "巡检轮在跑，闸却说可以切库"
    assert ce.maintenance_blockers() == [], "巡检轮结束后闸没跟着放开"


async def test_the_archive_round_actually_holds_its_lock(monkeypatch):
    """光有锁不够：驱动它的那条后台线得**真的**拿着它跑。

    这条用例在 `archive_old_completed` 里面问闸 —— 拿不到锁就等于没加。
    只断言"锁存在"或"闸认识这把锁"是测不到这一层的：把 `async with _archive_lock:`
    整行删掉，上一条用例照样全绿。
    """
    from core import engine as ce
    from core import worker as W

    seen = {}

    async def fake_archive():
        seen["blockers"] = ce.maintenance_blockers()
        return 0

    monkeypatch.setattr(W.engine, "archive_old_completed", fake_archive)
    await W.archive_round()
    assert seen.get("blockers"), "归档跑起来了，闸却是空的 —— 锁没被真的持住"
    assert any("归档" in r for r in seen["blockers"]), seen["blockers"]


async def test_the_sweep_round_actually_holds_its_lock(monkeypatch):
    """巡检轮同理：得在**轮子里面**问一次闸。"""
    from core import engine as ce
    from core import worker as W

    seen = {}

    def probe():
        seen["blockers"] = ce.maintenance_blockers()
        return 0

    monkeypatch.setattr(W.engine, "sweep_stale_delivering", probe)
    monkeypatch.setattr(W.anime, "sweep_finished", probe)
    monkeypatch.setattr(W.anime, "sweep_idle", probe)
    monkeypatch.setattr(W.anime, "sweep_alerts", probe)
    await W.sweep_round()
    assert seen.get("blockers"), "巡检跑起来了，闸却是空的 —— 锁没被真的持住"
    assert any("巡检" in r for r in seen["blockers"]), seen["blockers"]


async def test_a_crashing_delivery_still_unregisters(monkeypatch, clean_tables):
    """行为面：交付协程抛异常时，交付登记必须被摘掉，残骸清扫才能把那一行救回来。

    (R28) 上面那条静态守卫的第一版没把 `discard` 与 `finally` 绑在一起，
    变异后全量 1197 全绿 —— 所以这里再钉一次**行为**：内层必抛，
    断言 ① `_delivering` 空了；② `sweep_stale_delivering()` 真的把那一行复位成 pending。
    静态判据能被"换个写法"绕过，这一条不能。
    """
    from sqlmodel import select

    from core import anime as A
    from core import engine as ce
    from db.models import AnimeTorrent

    with clean_tables.get_session() as s:
        t = AnimeTorrent(anime_id=1, info_hash="7" * 40, raw_title="x - 01", episode=1.0,
                         status="downloading")
        s.add(t); s.commit(); s.refresh(t)
        tid = t.id

    async def boom(torrent_id, force=False):
        ce._delivering.add(("AnimeTorrent", int(torrent_id)))   # 内层进锁时做的那一下
        raise RuntimeError("回写撞上库抖动")

    monkeypatch.setattr(A, "_download_anime_torrent_inner", boom)
    try:
        await A.download_anime_torrent(tid)
    except RuntimeError:
        pass
    assert ("AnimeTorrent", int(tid)) not in ce._delivering, \
        "协程抛异常之后交付登记还留着 —— 这一行会被永久当成『正在交付中』"
    assert ce.sweep_stale_delivering() == 1, "残骸清扫救不回它"
    with clean_tables.get_session() as s:
        assert s.exec(select(AnimeTorrent)).one().status == "pending"


# ---------------- (R33) E-46：枚举"工作单元"，并用形状守卫逼着登记 ----------------

# 【这张表就是 E-46 的答案】core/ 里每一个"出 await 之后还开 get_session"的 async 函数，
# 都必须能说出它被哪道闸看见。名单从 R21 到 R27 错过四次，因为它从来只存在于人的记忆里；
# 现在它存在这里，而且下面那条用例会用 AST **扫形状**去核对它 —— 新写了一个同形函数
# 却没登记，红的是用例，不是用户的数据。
#   lock:<名>   ＝ 调用方整轮持着那把模块级锁（闸按 .locked() 看）
#   delivering  ＝ 进锁即落 status='downloading' 并登记 _delivering（闸按行数看）
#   sync_busy   ＝ qB 同步的轮次锁（闸按 sync_busy() 看）
#   in_flight   ＝ 页面驱动的入口，本函数是 `engine.in_flight` 包住的 wrapper 的 _inner
#   caller:<名> ＝ 只被某个已登记的函数调用（不是页面直接调的）
_STRADDLERS = {
    "core/anime.py": {
        "_resolve_anime":                 "caller:process_item",
        "process_item":                   "lock:_poll_lock",
        "_download_anime_torrent_inner":  "delivering",
        "flush_ready_downloads":          "lock:_poll_lock",
        "sweep_finished":                 "lock:_sweep_lock",
        "sweep_idle":                     "lock:_sweep_lock",
        "enrich_anime":                   "caller:_manual_enrich_inner|_retry_unmatched_inner",  # _resolve_anime 调的是 enrich.resolve，不是它（R33 对抗审计纠）
        "_bind_anime_bgm_inner":          "in_flight",
        "_manual_enrich_inner":           "in_flight",
        "_retry_unmatched_inner":         "lock:_enrich_lock",
        "_download_all_pending_inner":    "in_flight",
        "_backfill_source_inner":         "in_flight",
        "_delete_anime_files_inner":      "in_flight",
        # (R33 对抗审计补) 下面三条是扫描器认得"经辅助函数写回"之后才冒出来的
        "_delete_anime_torrent_inner":    "in_flight",     # 读 → await qb.delete → _set_status(id)
        "_download_pending_for_anime_inner": "in_flight",  # 读 id 列表 → await 预检 → 逐个交付
        "relocate_anime":                 "in_flight",     # 真正跨 await 的在 engine.relocate
    },
    "core/engine.py": {
        "archive_old_completed":          "lock:_archive_lock",
        "_sync_qb_status":                "sync_busy",
        "relocate":                       "caller:relocate_anime|relocate_movie",  # 读 (id,hash) → await setLocation → _mark_moved(ids)
    },
    "core/movies.py": {
        "_enrich_movie_inner":            "in_flight",
        "_bind_movie_bgm_inner":          "in_flight",
        "_download_movie_torrent_inner":  "delivering",
        "_refresh_movie_torrents_inner":  "in_flight",
        "_delete_movie_torrent_inner":    "in_flight",     # 审计探针在这条上真把 deleted 写进了另一个库
        "relocate_movie":                 "in_flight",
        "_discover_loop":                 "lock:_scan_lock",  # 只经 discover_movies ← scan_now / worker 整年扫描，都在 _scan_lock 里
    },
}


_CORE = ("core/engine.py", "core/anime.py", "core/movies.py")


def _session_openers(root):
    """三个 core 模块里【会打开业务库会话】的函数名集合：{模块相对路径: {函数名}}。

    直接调 `get_session` 的算；调了本模块里已算上的算；anime/movies 里调 `engine.<已算上>` 的也算
    （求不动点）。(R33 对抗审计) 第一版扫描只认函数体里**字面**的 `get_session(`，
    于是 `delete_movie_torrent` 这种"读主键 → await qb.delete → `_set_status(id)`"的
    经辅助函数写回的形状整个漏掉 —— 审计的探针在它身上真的把 deleted 写进了另一个库。
    "已知盲区，手工列入"在这一条上失效了：手工列入的前提是知道它在，而它正是不知道的那个。
    """
    import ast

    trees = {rel: ast.parse((root / rel).read_text(encoding="utf-8")) for rel in _CORE}

    def callees(fn):
        out = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Name):
                    out.add(("", f.id))
                elif isinstance(f, ast.Attribute):
                    base = f.value.id if isinstance(f.value, ast.Name) else "?"
                    out.add((base, f.attr))
        return out

    openers = {}
    for rel in _CORE:                       # engine 先算，anime/movies 再引用它
        fns = {n.name: n for n in trees[rel].body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        eng = openers.get("core/engine.py", set())
        mine, changed = set(), True
        while changed:
            changed = False
            for name, fn in fns.items():
                if name in mine:
                    continue
                cs = callees(fn)
                if (any(a == "get_session" for _, a in cs)
                        or any(b == "" and a in mine for b, a in cs)
                        or any(b == "engine" and a in eng for b, a in cs)):
                    mine.add(name)
                    changed = True
        openers[rel] = mine
    return openers


def _straddlers_by_shape(root, rel, openers):
    """AST 扫出"出 await 之后还开业务库会话"的 async 函数（含经辅助函数、经 `engine.*`、
    经函数体内嵌套 def 打开的）。

    只看本函数体自己的语句顺序（嵌套 def 的**体**不展开，但对嵌套 def 的**调用**按它是否
    开会话计入 —— `engine.relocate` 里的 `_mark_moved` 就是这种：读 → await set_location → 写）。
    行号顺序是启发式（按行比较，同一行的不算跨）。
    """
    import ast

    out = []
    tree = ast.parse((root / rel).read_text(encoding="utf-8"))
    mine = openers[rel]
    eng = openers["core/engine.py"]
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        # 函数体内嵌套的 def，若自己开会话（直接或经本模块/engine 的 opener），对它的调用也算
        nested = set()
        for n in ast.walk(fn):
            if n is not fn and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for c in ast.walk(n):
                    if isinstance(c, ast.Call):
                        f = c.func
                        if ((isinstance(f, ast.Name) and (f.id == "get_session" or f.id in mine))
                                or (isinstance(f, ast.Attribute) and (
                                    f.attr == "get_session"
                                    or (isinstance(f.value, ast.Name) and f.value.id == "engine"
                                        and f.attr in eng)))):
                            nested.add(n.name)
                            break
        body = []
        stack = list(ast.iter_child_nodes(fn))
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            body.append(n)
            stack.extend(ast.iter_child_nodes(n))
        awaits = [n.lineno for n in body if isinstance(n, ast.Await)]
        # 被 await 的那个调用本身算（`await download_anime_torrent(t.id)` 拿着出 await 前读的主键
        # 去写，是标准的跨），但它**实参里**的调用不算 —— 实参在 await 之前求值
        # （`await engine.relocate(..., anime_save_path(anime_id), ...)` 换行写的实参会落在下一行）。
        in_args = set()
        for n in body:
            if isinstance(n, ast.Await) and isinstance(n.value, ast.Call):
                for a in ast.walk(n.value):
                    if a is not n.value:
                        in_args.add(id(a))
        sess = []
        for n in body:
            if not isinstance(n, ast.Call) or id(n) in in_args:
                continue
            f = n.func
            if isinstance(f, ast.Name) and (f.id == "get_session" or f.id in mine or f.id in nested):
                sess.append(n.lineno)
            elif isinstance(f, ast.Attribute) and (
                    f.attr == "get_session"
                    or (isinstance(f.value, ast.Name) and f.value.id == "engine" and f.attr in eng)):
                sess.append(n.lineno)
        if awaits and sess and max(sess) > min(awaits):
            out.append(fn.name)
    return sorted(out)


def _enclosing_callers(root, names):
    """{被调函数名: {调用它的外层函数名}}，扫 core/ pages/ services/ 与 main.py。

    `foo(` 与 `x.foo(` 都算（页面经 `anime.foo(`、movies 经 `engine.foo(` 调）；**只提到名字**也算
    （worker.sweep_round 把 `anime.sweep_finished` 放进元组再逐个 `fn()`）—— 宁可多报。
    模块级的记作 "<module>"。只认名字不认模块：两条线各有一个 `_set_status`，
    但登记表里没有这种重名，真撞上时守卫会多报不会漏报。
    """
    import ast

    graph = _reference_graph(root)
    return {n: set(graph.get(n, ())) for n in names}


def _reference_graph(root):
    """{被提到的名字: {提到它的外层函数名}}，整仓算一次（按 root 缓存 —— 调用链要反复查）。"""
    import ast

    cache = _reference_graph.__dict__.setdefault("cache", {})
    if root in cache:
        return cache[root]
    out = {}
    files = [*root.glob("core/*.py"), *root.glob("pages/*.py"), *root.glob("services/*.py"),
             root / "main.py"]
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8"))

        def visit(node, owner):      # 给每个节点标上最近的外层函数
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(ch, ch.name)
                    continue
                nm = ch.id if isinstance(ch, ast.Name) else ch.attr if isinstance(ch, ast.Attribute) else ""
                if nm and owner != nm:
                    out.setdefault(nm, set()).add(owner)
                visit(ch, owner)
        visit(tree, "<module>")
    cache[root] = out
    return out


def _lock_holders(root, lock_names):
    """{锁名: {函数体里有 `async with <锁>` / `with <锁>` 的函数名}}，扫 core/ pages/ services/ main.py。"""
    import ast

    out = {n: set() for n in lock_names}
    files = [*root.glob("core/*.py"), *root.glob("pages/*.py"), *root.glob("services/*.py"),
             root / "main.py"]
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for w in ast.walk(fn):
                if isinstance(w, (ast.With, ast.AsyncWith)):
                    for item in w.items:
                        ce = item.context_expr
                        nm = ce.id if isinstance(ce, ast.Name) else ce.attr if isinstance(ce, ast.Attribute) else ""
                        if nm in out:
                            out[nm].add(fn.name)
    return out


def test_every_pk_straddler_is_registered():
    """core/ 里每一个"出 await 之后还开 get_session"的函数，都必须在 _STRADDLERS 里有一行。

    这是 E-46 的守卫：它扫的是**形状**（await 之后开 session），不是名单。
    新写了一个同形函数而没登记 → 这里红。登记时必须写清楚它被哪道闸看见，
    而"caller:"只允许指向**本表里已有**的名字（否则等于把责任推给一个不存在的人）。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    openers = _session_openers(root)
    problems = []
    for rel, table in _STRADDLERS.items():
        found = _straddlers_by_shape(root, rel, openers)
        missing = [f for f in found if f not in table]
        if missing:
            problems.append(f"{rel}: 这些函数出 await 后还开 session，却没登记怎么被闸看见 → {missing}")
        # in_flight 的条目允许扫不到：wrapper 委托的 _inner 自己可能只是再委托一层（_manual_enrich_inner）
        stale = [f for f in table if f not in found and table[f] != "in_flight"]
        if stale:
            problems.append(f"{rel}: 登记表里这些名字 AST 已经扫不到了（改名/删了？）→ {stale}")
        for f, how in table.items():
            if how.startswith("caller:"):
                for c in how.split(":", 1)[1].split("|"):
                    if not any(c in t for t in _STRADDLERS.values()):
                        problems.append(f"{rel}::{f} 说自己被 {c} 盖住，但 {c} 不在本表里")
    # 【caller: 必须与调用图一致】(R33 对抗审计) 上一版只查"名字在不在表里"：
    # 表里 enrich_anime 写着被 _resolve_anime 盖住，而 _resolve_anime 从不调它（它调 enrich.resolve）；
    # 给 _resolve_anime 在锁外加一个新调用方，全套照样绿。这里按 AST 收集 core/ pages/ services/ main.py
    # 里每一处对该函数的调用（含 `模块.名字(` 形态）所在的外层函数，要求恰好 ⊆ 所列 caller。
    everything = {f: how for t in _STRADDLERS.values() for f, how in t.items()}
    real_callers = _enclosing_callers(root, [f for f, how in everything.items()
                                            if how.startswith("caller:")])
    for f, how in everything.items():
        if not how.startswith("caller:"):
            continue
        listed = set(how.split(":", 1)[1].split("|"))
        actual = real_callers.get(f, set())
        if not actual:
            problems.append(f"{f} 登记为 caller:，但整个仓库没人调它 —— 名单是假的或函数已死")
        extra = sorted(actual - listed)
        if extra:
            problems.append(f"{f} 还被这些不在名单上的函数调：{extra}（它们在闸外就是漏洞）")
        phantom = sorted(listed - actual)
        if phantom:
            problems.append(f"{f} 说自己被 {phantom} 盖住，但它们根本不调 {f}")
    # 【lock:L 必须真的在 f 或它的调用链上被拿】(R33 对抗审计) 表里写一把**真锁、闸看得见、
    # 但函数根本不在它里面跑**的锁（process_item → lock:_sweep_lock），上面全绿。
    # 这里沿调用图往上找（core/pages/services/main + worker），要求某一层的函数体里有
    # `async with <…>.L` / `with L`。
    lock_names = sorted({how.split(":", 1)[1] for how in everything.values() if how.startswith("lock:")})
    holders = _lock_holders(root, lock_names)
    for f, how in everything.items():
        if not how.startswith("lock:"):
            continue
        L = how.split(":", 1)[1]
        graph = _reference_graph(root)
        chain, frontier = {f}, {f}
        while frontier:
            nxt = set()
            for g in frontier:
                nxt |= graph.get(g, set())
            nxt -= chain
            chain |= nxt
            frontier = nxt
        if not (chain & holders.get(L, set())):
            problems.append(f"{f} 登记为 lock:{L}，但它和它的所有调用方都没有 `async with {L}`：调用链 {sorted(chain)}")
    # 【caller: 链必须走到一道真闸，且不许绕圈】"A 说被 B 盖住、B 说被 A 盖住"在上面那条里是合法的。
    for f, how in everything.items():
        seen, cur = set(), f
        while everything[cur].startswith("caller:"):
            if cur in seen:
                problems.append(f"{f} 的 caller: 链绕圈了：{sorted(seen)}")
                break
            seen.add(cur)
            # 多个 caller 各自都得到闸；这里沿第一个走，其余在各自的条目里被同样地追
            cur = everything[cur].split(":", 1)[1].split("|")[0]
    assert not problems, "\n  ".join(["E-46 的工作单元登记表与代码对不上："] + problems)


async def test_every_registered_gate_is_one_the_maintenance_window_actually_sees(clean_tables):
    """登记表里写的 `lock:<名字>` 一把把真的拿住，`maintenance_blockers()` 必须说话。

    上一条只核"表与代码对得上"，核不了"表里写的那道闸是真的"——
    `lock:_foo_lock` 随手写一个不存在的锁名，或者写一把闸看不见的真锁，上一条照样绿。
    这里按表逐把拿住真锁问闸。`delivering` / `sync_busy` / `in_flight` 各有自己的行为用例。
    """
    from core import anime as A
    from core import engine as ce
    from core import worker as W

    assert ce.maintenance_blockers() == [], "起点就不干净"
    names = sorted({how.split(":", 1)[1] for t in _STRADDLERS.values()
                    for how in t.values() if how.startswith("lock:")})
    assert names, "表里一把锁都没有？前提坏了"
    bad = []
    for name in names:
        lock = getattr(W, name, None) or getattr(A, name, None)
        if lock is None:
            bad.append(f"lock:{name} —— worker / anime 里都没有这把锁")
            continue
        async with lock:
            if not ce.maintenance_blockers():
                bad.append(f"lock:{name} 拿住了，maintenance_blockers() 却是空的 —— 这把锁闸看不见")
        assert ce.maintenance_blockers() == [], f"放开 {name} 之后闸没归零"
    assert not bad, "\n  ".join(["登记表里这些闸是假的："] + bad)


def test_in_flight_entries_are_really_wrapped():
    """登记表里标 in_flight 的，必须真的有一个同名 wrapper 用 `engine.in_flight(` 包住它。

    光在表里写 in_flight 不算数 —— 那又回到"名单只存在于纸上"。这里按 AST 核：
    存在 `async def <name去掉 _ 与 _inner>`，且它的函数体里有 `with engine.in_flight(...)`
    并在里面 `await _<name>(...)`。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    bad = []
    for rel, table in _STRADDLERS.items():
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
        for inner, how in table.items():
            if how != "in_flight":
                continue
            if not inner.endswith("_inner"):
                # 没拆两层的（relocate_anime 这种一句委托）：函数体自己的每个 await 都得在 with 里
                fn = fns.get(inner)
                if fn is None:
                    bad.append(f"{rel}: {inner} 标了 in_flight，但没这个函数")
                    continue
                withs = [n for n in ast.walk(fn) if isinstance(n, ast.With)
                         and any(isinstance(i.context_expr, ast.Call)
                                 and getattr(i.context_expr.func, "attr",
                                             getattr(i.context_expr.func, "id", "")) == "in_flight"
                                 for i in n.items)]
                inside = {id(a) for w_ in withs for a in ast.walk(w_) if isinstance(a, ast.Await)}
                all_awaits = [a for a in ast.walk(fn) if isinstance(a, ast.Await)]
                if not all_awaits or any(id(a) not in inside for a in all_awaits):
                    bad.append(f"{rel}: `{inner}` 有 await 落在 `with engine.in_flight(...)` 外面")
                continue
            pub = inner[1:-len("_inner")]
            w = fns.get(pub)
            if w is None:
                bad.append(f"{rel}: {inner} 标了 in_flight，但没有公开的 wrapper `{pub}`")
                continue
            # 【看 With 节点，不看字符串】(R33 自查) 第一版用 `"in_flight" in ast.dump(w)`，
            # 而 wrapper 的 docstring 里就写着 "见 engine.in_flight" —— 把登记整个拿掉它照样绿。
            # 判据：存在一个 With，其上下文表达式是对 in_flight 的调用，且 _inner 的调用在它体内。
            withs = [n for n in ast.walk(w) if isinstance(n, ast.With)
                     and any(isinstance(i.context_expr, ast.Call)
                             and getattr(i.context_expr.func, "attr",
                                         getattr(i.context_expr.func, "id", "")) == "in_flight"
                             for i in n.items)]
            wrapped = any(
                any(isinstance(c, ast.Call)
                    and getattr(c.func, "id", getattr(c.func, "attr", "")) == inner
                    for c in ast.walk(wn))
                for wn in withs)
            if not wrapped:
                bad.append(f"{rel}: `{pub}` 没有在 `with engine.in_flight(...)` 体内调用 {inner}")
                continue
            # 【await 也得在 with 里】(R33 对抗审计) `with in_flight(): coro = _inner()` / `return await coro`
            # —— Call 在 with 体内、await 在外面，登记在真正跑之前就注销了。上一版只看 Call，这个变异存活。
            inside = {id(a) for wn in withs for a in ast.walk(wn) if isinstance(a, ast.Await)}
            all_awaits = [a for a in ast.walk(w) if isinstance(a, ast.Await)]
            if not all_awaits or any(id(a) not in inside for a in all_awaits):
                bad.append(f"{rel}: `{pub}` 有 await 落在 `with engine.in_flight(...)` 外面")
    assert not bad, "\n  ".join(["这些 in_flight 登记是空头的："] + bad)


async def test_a_page_entry_blocks_maintenance_while_it_runs(clean_tables, monkeypatch):
    """行为面：页面入口跑到一半时，在途闸必须说话；结束（含异常）后必须放开。

    要 clean_tables：`maintenance_blockers()` 读不到业务库时**整个返回空表**（那是给『切回本地』
    留的自救出口），in_flight 那一段根本走不到 —— 没有库的话这条用例单跑必红、跟在别的用例后面才绿。
    """
    from core import anime as A
    from core import engine as ce

    assert ce.maintenance_blockers() == [], "起点就不干净"

    seen = {}

    async def slow_inner(anime_id, bgm_id, report=None):
        seen["during"] = ce.maintenance_blockers()
        raise RuntimeError("模拟 bgm 中途炸了")

    monkeypatch.setattr(A, "_bind_anime_bgm_inner", slow_inner)
    try:
        await A.bind_anime_bgm(1, 2)
    except RuntimeError:
        pass
    assert any("绑定 bgm" in r for r in seen["during"]), \
        f"绑定 bgm 跑到一半，闸却没看见它：{seen['during']}"
    assert ce.maintenance_blockers() == [], "异常之后登记没注销 —— 切库会被永久拒死"


async def test_wait_until_quiet_returns_when_the_gate_clears(clean_tables):
    """`wait_until_quiet` 要真的等：闸忙时不立刻返回，放空后立刻返回。"""
    import asyncio

    from core import engine as ce

    with ce.in_flight("测试用"):
        async def release():
            await asyncio.sleep(0.3)
            ce._in_flight.clear()
        asyncio.get_running_loop().create_task(release())
        t0 = asyncio.get_running_loop().time()
        left = await ce.wait_until_quiet(timeout=5, poll=0.05)
        dt = asyncio.get_running_loop().time() - t0
    assert left == [], f"闸放空了却还回了阻塞项：{left}"
    assert 0.25 < dt < 2, f"等待时长不对（{dt:.2f}s）—— 要么没等、要么没及时返回"

    with ce.in_flight("一直占着"):
        # wait_for：把"忽略超时、永远等"这个变异变成失败而不是挂死（审计的 M4c 挂了 300 秒）
        left = await asyncio.wait_for(ce.wait_until_quiet(timeout=0.3, poll=0.05), timeout=3)
    assert left and "一直占着" in left[0], "超时后必须把还在阻塞的东西回出来，交给原来的拒绝路径"


async def test_gate_still_lists_memory_signals_when_the_db_is_unreadable(clean_tables, monkeypatch):
    """(R33 对抗审计) 业务库读不了时，锁 / in_flight / 交付登记这些**内存信号**照样要列出来。

    R22 在 except 里整个 `return []`（"别把『切回本地』堵死"）。探针：MySQL 抖动 → 用户走自救出口
    → 此刻『删除单集文件』正停在 qb.delete 的 await 上 → 三道预检全拿到 [] → 切库放行 →
    await 回来后 `deleted` 按 MySQL 的主键写进了 SQLite 的另一行。
    自救出口只需要跳过那两条 COUNT，不需要跳过内存信号。
    """
    from sqlalchemy.exc import OperationalError

    from core import engine as ce
    from core import worker as W

    def boom():
        raise OperationalError("SELECT 1", {}, Exception("库连不上"))

    monkeypatch.setattr(ce, "get_session", boom)
    assert ce.maintenance_blockers() == [], "库读不了、也没有在途工作 → 自救出口必须开着"
    with ce.in_flight("删除单集文件"):
        got = ce.maintenance_blockers()
        assert any("删除单集文件" in r for r in got), f"库读不了时 in_flight 被吞了：{got}"
    async with W._poll_lock:
        got = ce.maintenance_blockers()
        assert any("采集轮" in r for r in got), f"库读不了时轮次锁被吞了：{got}"
    ce._delivering.add(("AnimeTorrent", 999))
    try:
        got = ce.maintenance_blockers()
        assert any("交付中" in r for r in got), f"库读不了时交付登记被吞了：{got}"
    finally:
        ce._delivering.discard(("AnimeTorrent", 999))
    assert ce.maintenance_blockers() == [], "全放开后必须归零"


async def test_wait_until_quiet_polls_off_the_event_loop(clean_tables, monkeypatch):
    """(R33 对抗审计) 轮询里的 `maintenance_blockers()` 必须在线程上跑，不能在事件循环上。

    它对业务库发两条 SELECT，而那可能是台连不上的 MySQL —— E-36 定的纪律是碰 MySQL 的同步调用
    一律上线程；设置页那一次预检就是 `run.io_bound` 的。这里一轮最多 60 次，更不能例外。
    `tests/test_mysql_compat.py` 的 ON_THREAD_ONLY 只扫页面处理器，看不见 core 里的这一处，所以单独钉。
    """
    import threading

    from core import engine as ce

    threads = []
    real = ce.maintenance_blockers

    def spy():
        threads.append(threading.get_ident())
        return real()

    monkeypatch.setattr(ce, "maintenance_blockers", spy)
    await ce.wait_until_quiet(timeout=0.2, poll=0.05)
    assert threads, "轮询一次都没调 maintenance_blockers —— 前提坏了"
    assert threading.get_ident() not in threads, "maintenance_blockers 在事件循环线程上同步跑了"


def test_every_maintenance_window_waits_before_refusing():
    """(R33) 设置页每一处 `db.maintenance(...)` 之前都必须先 `await engine.wait_until_quiet(...)`。

    "有界地等、等不到再拒"这个决定有**两个**入口（切库 / 迁移）。只落一处就是
    本项目第①号缺陷形状。按 AST 逐个 async 处理器核：含 db.maintenance 的，
    必须在它之前（同一函数体内）出现对 wait_until_quiet 的 await。
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent.joinpath("pages/settings.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    seen, bad = 0, []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        maint = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", "") == "maintenance"]
        if not maint:
            continue
        seen += 1
        # 【判据落在语句序列上】(R33 对抗审计) 上一版按行号比：去掉 `await`（留一个没人等的协程）、
        # 或把 wait 挪进 `if False:` 死分支，都在"更早的行上有个 wait_until_quiet 调用"，照样绿。
        # 现在要求：在**包含 db.maintenance 那条语句的同一个语句列表**里，它前面有一条语句，
        # 其值是 `await …wait_until_quiet(...)`。
        def _is_await_wait(stmt):
            v = getattr(stmt, "value", None)
            return (isinstance(v, ast.Await) and isinstance(v.value, ast.Call)
                    and getattr(v.value.func, "attr", "") == "wait_until_quiet")

        def _stmt_lists(node):
            for field in ("body", "orelse", "finalbody", "handlers"):
                seq = getattr(node, field, None)
                if isinstance(seq, list) and seq and isinstance(seq[0], ast.stmt):
                    yield seq
            for ch in ast.iter_child_nodes(node):
                yield from _stmt_lists(ch)

        ok = False
        for seq in _stmt_lists(fn):
            for i, stmt in enumerate(seq):
                if any(n is m for m in maint for n in ast.walk(stmt)):
                    if any(_is_await_wait(prev) for prev in seq[:i]):
                        ok = True
                    break
        if not ok:
            bad.append(f"pages/settings.py:{fn.lineno} {fn.name}() 进维护窗口前没有先 await 等在途工作"
                       "（要在同一个语句序列里、在 db.maintenance 之前）")
    assert seen >= 2, f"只找到 {seen} 个进维护窗口的处理器，守卫的前提坏了"
    assert not bad, "\n  ".join(["这些处理器直接拒绝、没先等："] + bad)


def test_background_task_handles_are_kept():
    """(R33) main.py 里每一个 create_task 的结果都必须被存住。

    asyncio 只对 task 持弱引用（官方文档明写要存引用），而 E-46 讨论的"切库前停掉后台线"
    也以"有句柄"为前提。按 AST 核：main.py 里不允许出现**表达式语句**形态的裸 create_task。
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent.joinpath("main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # 【判据落在"存进了哪里"】(R33 对抗审计) 上一版只拦裸表达式语句：`_ = create_task(...)`、
    # 或存进 _startup 的局部列表（函数一返回就丢），都算"存住了"，而 asyncio 同样只剩弱引用。
    # 现在要求每个 create_task 调用的祖先链里有 `background_tasks.extend/append(...)`
    # 或对 `background_tasks` 的赋值 / 增量赋值。
    parent = {}
    for n in ast.walk(tree):
        for ch in ast.iter_child_nodes(n):
            parent[ch] = n

    def _kept(call):
        cur = call
        while cur in parent:
            cur = parent[cur]
            if (isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute)
                    and cur.func.attr in ("extend", "append")
                    and isinstance(cur.func.value, ast.Name)
                    and cur.func.value.id == "background_tasks"):
                return True
            if isinstance(cur, (ast.Assign, ast.AugAssign)):
                targets = cur.targets if isinstance(cur, ast.Assign) else [cur.target]
                if any(isinstance(t, ast.Name) and t.id == "background_tasks" for t in targets):
                    return True
        return False

    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "create_task"]
    lost = [c.lineno for c in calls if not _kept(c)]
    assert not lost, f"main.py 这些行的 create_task 句柄没存进 background_tasks：{lost}"
    assert len(calls) >= 7, f"只找到 {len(calls)} 个 create_task，后台线少了？"


# ---------------- E-49：崩溃后的『交付中』残骸要还原到占位前的状态（2026-09-02 拍板） ----------------

@pytest.mark.parametrize("line", ["anime", "movie"])
@pytest.mark.parametrize("orig", ["deleted", "excluded", "stalled", "archived"])
async def test_a_crashed_force_redownload_restores_the_original_state(clean_tables, cfg, monkeypatch,
                                                                       line, orig):
    """force 重下从终态出发 → 取种的 await 里"进程死了" → 下一轮清扫必须把它放回原状态，一样都不少。

    以前进锁占位时就清掉 archived_at 与 qB 实时态、清扫又一律写 pending：
    deleted 变回自动队列、excluded 的排除被撤销、**stalled 丢掉 HAVE 身份**（flush 当场为同一集换源
    下第二份到同一目录）、已归档的 archived_at 再也回不来。四种原态 × 两条线逐个钉。
    "崩溃"的模拟：让取种抛一个**不是 Exception 的**东西（BaseException），
    交付协程的回写路径整个跳过，只剩外层 finally 的注销 —— 与进程被杀后重启的行是同一副样子。
    """
    from datetime import datetime

    from core import anime as A
    from core import engine as ce
    from core import movies as M
    from db.models import Anime, AnimeTorrent, Movie, MovieTorrent

    class _Crash(BaseException):
        pass

    async def die(url):
        raise _Crash()

    monkeypatch.setattr(ce, "fetch_torrent_bytes", die)
    cfg(QB_ENABLED=True, DOWN_PATH="/tmp/dl")

    fields = dict(info_hash="9" * 40, raw_title="x", download_url="http://x/t",
                  qb_progress=0.4, qb_state="stalledDL",
                  qb_synced_at=datetime(2026, 1, 1), qb_progress_at=datetime(2026, 1, 1))
    if orig == "archived":
        fields.update(status="sent", qb_progress=1.0, qb_state="", archived_at=datetime(2026, 2, 2))
    else:
        fields["status"] = orig

    with clean_tables.get_session() as s:
        if line == "anime":
            a = Anime(title="T", season=1, confirmed=True, quarter="26A")
            s.add(a); s.commit(); s.refresh(a)
            row = AnimeTorrent(anime_id=a.id, episode=3, **fields)
        else:
            m = Movie(title="M", quarter="2026")
            s.add(m); s.commit(); s.refresh(m)
            row = MovieTorrent(movie_id=m.id, **fields)
        s.add(row); s.commit(); s.refresh(row)
        tid = row.id
        before = {k: getattr(row, k) for k in ("status", "archived_at", "qb_progress",
                                                "qb_state", "qb_synced_at", "qb_progress_at")}

    with pytest.raises(_Crash):
        if line == "anime":
            await A.download_anime_torrent(tid, force=True)
        else:
            await M.download_movie_torrent(tid)     # 剧场版一律是"点了就下"，没有 force 形参

    model = AnimeTorrent if line == "anime" else MovieTorrent
    with clean_tables.get_session() as s:
        t = s.get(model, tid)
        assert t.status == "downloading", "前提：崩溃后行停在占位上"
        assert t.prev_status == before["status"], "占位时没记下原状态"
    assert ce.sweep_stale_delivering() == 1
    with clean_tables.get_session() as s:
        t = s.get(model, tid)
        after = {k: getattr(t, k) for k in before}
        assert after == before, f"{line}/{orig}：还原后与占位前不一致：{after} != {before}"
        assert t.prev_status is None, "记号用完要清"


@pytest.mark.parametrize("reset", ["sweep", "startup"])
def test_both_reset_points_restore_the_original_state(clean_tables, reset):
    """两个复位点（运行中的 sweep_stale_delivering / 启动时的 reset_downloading）同一口径。

    E-49 点名"两个复位点都要改"—— 只改一处就是本项目第①号缺陷形状。
    """
    from core import engine as ce
    from db.models import Anime, AnimeTorrent, MovieTorrent

    with clean_tables.get_session() as s:
        a = Anime(title="T", season=1, confirmed=True)
        s.add(a); s.commit(); s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="a" * 40, raw_title="x", episode=1,
                           status="downloading", prev_status="excluded"))
        s.add(AnimeTorrent(anime_id=a.id, info_hash="b" * 40, raw_title="y", episode=2,
                           status="downloading", prev_status=None))     # 老库残骸：没有记号
        s.commit()

    if reset == "sweep":
        assert ce.sweep_stale_delivering() == 2
    else:
        ce.reset_downloading(AnimeTorrent)
        ce.reset_downloading(MovieTorrent)
    with clean_tables.get_session() as s:
        got = {t.info_hash[0]: (t.status, t.prev_status) for t in s.exec(select(AnimeTorrent))}
    assert got == {"a": ("excluded", None), "b": ("pending", None)}, got


def test_the_placeholder_no_longer_wipes_archive_and_progress_in_the_lock():
    """(AST) 两条交付线进锁占位那一段，不许再清 archived_at / qB 实时态 —— 清了崩溃后就放不回。

    行为用例（崩溃还原 ×8、重下成功不再归档 ×2）才是主守卫；这条钉的是位置：清理只许出现在
    `save_path = save_path` 之后（交付成功那一段），给以后改这段的人一句当场的提示。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for rel, fn_name in (("core/anime.py", "_download_anime_torrent_inner"),
                         ("core/movies.py", "_download_movie_torrent_inner")):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == fn_name)
        # 找到"save_path 回写"那一行，作为成功段的起点
        success_line = min(n.lineno for n in ast.walk(fn) if isinstance(n, ast.Assign)
                           and any(isinstance(t, ast.Attribute) and t.attr == "save_path" for t in n.targets))
        # (R34 对抗审计) 上一版只认 `x.archived_at = None` 这一种写法；元组赋值、setattr、清 qb_* 都漏。
        # 现在：任何对这五个属性的赋值（含 Tuple 目标）或 setattr 调用，都不许出现在成功段之前。
        _fields = {"archived_at", "qb_progress", "qb_state", "qb_synced_at", "qb_progress_at"}

        def _is_wipe(value):        # 清空：写常量（None / 0.0 / ""）或全是常量的元组；从变量放回原值的不算
            if isinstance(value, ast.Constant):
                return value.value in (None, 0, 0.0, "")
            if isinstance(value, ast.Tuple):
                return all(_is_wipe(e) for e in value.elts)
            return False

        def _targets(node):
            out = set()
            if isinstance(node, ast.Assign) and _is_wipe(node.value):
                for t in node.targets:
                    for leaf in ast.walk(t):
                        if isinstance(leaf, ast.Attribute) and leaf.attr in _fields:
                            out.add(leaf.attr)
            if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "setattr"
                    and len(node.args) >= 3 and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value in _fields and _is_wipe(node.args[2])):
                out.add(node.args[1].value)
            return out

        wipes = [(n.lineno, _targets(n)) for n in ast.walk(fn) if _targets(n)]
        assert any("archived_at" in f for _, f in wipes), \
            f"{rel}::{fn_name} 里找不到对 archived_at 的清理（成功段该清一次）"
        early = [(ln, sorted(f)) for ln, f in wipes if ln < success_line]
        assert not early, f"{rel}::{fn_name} 在交付成功之前就动了归档标记/qB 实时态（E-49）：{early}"


# ---------------- E-4：relocate 搬不动时，特别篇/未知集不清成 pending（2026-09-02 拍板） ----------------

async def test_relocate_keeps_special_and_unknown_episodes_instead_of_clearing_them(
        clean_tables, cfg, monkeypatch):
    """qB 不认识这个 hash（remove-on-complete）时，正集清成 pending 等重下；-1/-2 **不清**、报告单列。

    四条自动/批量路径都被 auto_downloadable_ep 挡住 -1/-2：清成 pending 之后没有任何路径会再下它，
    而报告还会提示"旧文件需你手动清理"—— 用户照做就删光了唯一一份。
    剧场版那张表没有 episode 列：同一段代码要用 getattr 兜底，那边一律照旧清（本用例末尾顺带核）。
    """
    from datetime import datetime

    from core import anime as A
    from core import engine as ce
    from core import movies as M
    from db.models import Movie, MovieTorrent

    cfg(QB_ENABLED=True, DOWN_PATH="/data")

    async def nobody_home(hashes):      # qB 在线，但这些 hash 它一个都不认识
        return {}
    monkeypatch.setattr(ce.qb, "torrents_info", nobody_home)

    with clean_tables.get_session() as s:
        a = Anime(title="番", display_name="番", quarter="26A", confirmed=True)
        s.add(a); s.commit(); s.refresh(a)
        for h, ep in (("a", 1), ("b", -1), ("c", -2), ("d", 2)):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=h * 40, raw_title=f"[组][{ep}]",
                               episode=ep, status="sent", qb_progress=1.0,
                               save_path="/data/26A/番", created_at=datetime.now()))
        s.commit()
        aid = a.id

        # 停滞且 -1 的行：只算进 stalled_kept，不重复计入 special_kept（R34 对抗审计的变异 M9 要挡的）
        s.add(AnimeTorrent(anime_id=a.id, info_hash="e" * 40, raw_title="[组][SP]", episode=-1,
                           status="stalled", qb_progress=0.3, save_path="/data/26A/番",
                           created_at=datetime.now()))
        s.commit()

    rep = await A.relocate_anime(aid, old_path="/data/26A/番")
    assert (rep["redownload"], rep["special_kept"], rep["stalled_kept"]) == (2, 2, 1), rep
    with clean_tables.get_session() as s:
        got = {t.info_hash[0]: t.status for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid))}
    assert got == {"a": "pending", "b": "sent", "c": "sent", "d": "pending", "e": "stalled"}, got

    # 剧场版：没有 episode 列，getattr 兜底 → 一律可重下（别让番剧的判据在这边变成 AttributeError）
    with clean_tables.get_session() as s:
        m = Movie(title="片", quarter="2026")
        s.add(m); s.commit(); s.refresh(m)
        s.add(MovieTorrent(movie_id=m.id, info_hash="e" * 40, raw_title="mv", status="sent",
                           qb_progress=1.0, save_path="/data/2026/片"))
        s.commit()
        mid = m.id
    rep = await M.relocate_movie(mid, old_path="/data/2026/片")
    assert rep["redownload"] == 1 and rep["special_kept"] == 0, rep


@pytest.mark.parametrize("mod, fn", [("pages.anime_detail", "notify_relocate_anime"),
                                     ("pages.movies", "_notify_relocate_movie")])
def test_both_relocate_notifiers_report_kept_specials_as_a_warning(monkeypatch, mod, fn):
    """两个页面把 relocate 报告翻成人话的函数，对 special_kept 都得：算进文案、算进 warning、
    **不**说"旧文件需你手动清理"（那是唯一的一份，行还指着它）、出路不是"点『下载』"（force 被 TRACKED 短路）。

    (R34 对抗审计) 上一版是 AST 查字符串常量 —— 读了不用、从 warn/old_path 里剔掉都照样绿。改成行为用例。
    """
    import importlib

    m = importlib.import_module(mod)
    got = []
    monkeypatch.setattr(m.ui, "notify", lambda msg, **kw: got.append((msg, kw)))
    getattr(m, fn)({"new_path": "/n", "old_path": "/o", "moved": 0, "redownload": 0, "untracked": 0,
                    "failed": 0, "stalled_kept": 0, "special_kept": 2, "delivering": 0})
    assert got, "没有弹提示"
    msg, kw = got[0]
    assert "2" in msg and kw.get("type") == "warning", (msg, kw)
    assert "需你手动清理" not in msg, msg
    assert "『删除』" in msg, msg
    assert "要搬就点『下载』" not in msg, msg


def test_a_downloading_placeholder_never_renders_as_archived_or_complete():
    """(E-49 的显示面) 占位段不再清旧的归档标记/实时态，交付中的那 ≤180 秒行上还挂着它们 ——
    两个渲染入口都得让『交付中』盖过陈旧的『已归档』/『已完成 100%』。"""
    from datetime import datetime

    from pages.layout import live_status, qb_live_text

    class _T:
        status = "downloading"
        archived_at = datetime(2026, 2, 2)
        qb_state, qb_progress, qb_dlspeed = "", 1.0, 0
    assert qb_live_text(_T()) == "", "qb_live_text 把交付中的行显示成了『已归档』"
    text, color = live_status("downloading", qb_state="uploading", qb_progress=1.0)
    assert "100%" not in text and "做种" not in text, text


@pytest.mark.parametrize("line", ["anime", "movie"])
async def test_a_second_force_click_during_delivery_of_an_archived_row_is_refused(clean_tables, cfg,
                                                                                  monkeypatch, line):
    """(R34 对抗审计 P1) 已归档行 force 重下、取种 await 中再点一次『下载』→ 第二条协程必须被挡在锁里。

    E-49 之后占位段不再清 archived_at，于是"已归档的可 force 重下"这条例外会把**交付中**的行也放进来：
    两条协程交付同一行，两次取种都失败时第二条按 orig_status='downloading' 恢复、还把 prev_status 清掉，
    清扫只能落 pending —— 已归档行掉出 HAVE，flush 为同一集换源下第二份到归档文件所在目录。
    HEAD 之前靠锁内清 archived_at 侥幸挡住；现在 downloading 单独钉死。
    """
    import asyncio
    from datetime import datetime

    from core import anime as A
    from core import engine as ce
    from core import movies as M
    from db.models import Anime, AnimeTorrent, Movie, MovieTorrent

    cfg(QB_ENABLED=True, DOWN_PATH="/tmp/dl")
    gate = asyncio.Event()
    fetches = []

    async def slow_fetch(url):
        fetches.append(url)
        await gate.wait()
        raise RuntimeError("502")      # 两次都失败的那条链

    monkeypatch.setattr(ce, "fetch_torrent_bytes", slow_fetch)
    with clean_tables.get_session() as s:
        if line == "anime":
            a = Anime(title="T", season=1, confirmed=True, quarter="26A")
            s.add(a); s.commit(); s.refresh(a)
            row = AnimeTorrent(anime_id=a.id, episode=3, info_hash="c" * 40, raw_title="x",
                               download_url="http://x/t", status="sent", qb_progress=1.0,
                               archived_at=datetime(2026, 2, 2))
        else:
            m = Movie(title="M", quarter="2026")
            s.add(m); s.commit(); s.refresh(m)
            row = MovieTorrent(movie_id=m.id, info_hash="c" * 40, raw_title="x",
                               download_url="http://x/t", status="sent", qb_progress=1.0,
                               archived_at=datetime(2026, 2, 2))
        s.add(row); s.commit(); s.refresh(row)
        tid = row.id

    dl = (lambda: A.download_anime_torrent(tid, force=True)) if line == "anime" \
        else (lambda: M.download_movie_torrent(tid))
    first = asyncio.get_running_loop().create_task(dl())
    await asyncio.sleep(0.05)               # 第一条已进 await
    assert fetches == ["http://x/t"], "前提：第一条协程停在取种上"
    # wait_for：闸没了的话第二条协程会跟第一条一样卡在 gate 上 —— 那要红，不要挂死
    second = await asyncio.wait_for(dl(), timeout=2)     # 双击
    assert second is False and fetches == ["http://x/t"], "第二条协程进了锁、又取了一次种"
    gate.set()
    await first
    model = AnimeTorrent if line == "anime" else MovieTorrent
    with clean_tables.get_session() as s:
        t = s.get(model, tid)
    assert (t.status, t.archived_at, t.prev_status) == ("sent", datetime(2026, 2, 2), None), \
        "失败后已归档行没有原样放回"


@pytest.mark.parametrize("line", ["anime", "movie"])
async def test_a_successfully_redelivered_archived_row_is_not_rearchived_at_once(clean_tables, cfg,
                                                                                 monkeypatch, line):
    """(R34 对抗审计 P2) 成功段要把旧的 qb_progress/qb_synced_at 清掉 —— 否则刚交回 qB 的种子
    带着旧完成时间，下一轮 archive_old_completed 立刻把它 qb.delete 掉（下载中断、UI 显示『已归档』）。
    这一段以前只有 AST 守卫盯 `archived_at = None`，删掉 qB 重置那一行全量照样绿。
    """
    from datetime import datetime, timedelta

    from core import anime as A
    from core import engine as ce
    from core import movies as M
    from db.models import Anime, AnimeTorrent, Movie, MovieTorrent

    cfg(QB_ENABLED=True, QB_SYNC_STATUS=True, DOWN_PATH="/tmp/dl", QB_ARCHIVE_AFTER_DAYS=7)

    async def ok_fetch(url):
        return b"torrent"

    async def ok_add(*a, **k):
        return True
    deleted = []

    async def fake_delete(hashes, delete_files=False):
        deleted.extend(hashes)
        return True

    async def fake_info(hashes):
        return {h: {"state": "uploading", "progress": 1.0, "dlspeed": 0, "size": 1} for h in hashes}
    monkeypatch.setattr(ce, "fetch_torrent_bytes", ok_fetch)
    monkeypatch.setattr(ce, "add_to_qb", ok_add)
    monkeypatch.setattr(ce.qb, "delete", fake_delete)
    monkeypatch.setattr(ce.qb, "torrents_info", fake_info)

    old = datetime.now() - timedelta(days=30)
    with clean_tables.get_session() as s:
        if line == "anime":
            a = Anime(title="T", season=1, confirmed=True, quarter="26A")
            s.add(a); s.commit(); s.refresh(a)
            row = AnimeTorrent(anime_id=a.id, episode=3, info_hash="d" * 40, raw_title="x",
                               download_url="http://x/t", status="sent", qb_progress=1.0,
                               qb_synced_at=old, archived_at=old)
        else:
            m = Movie(title="M", quarter="2026")
            s.add(m); s.commit(); s.refresh(m)
            row = MovieTorrent(movie_id=m.id, info_hash="d" * 40, raw_title="x",
                               download_url="http://x/t", status="sent", qb_progress=1.0,
                               qb_synced_at=old, archived_at=old)
        s.add(row); s.commit(); s.refresh(row)
        tid = row.id

    ok = await (A.download_anime_torrent(tid, force=True) if line == "anime"
                else M.download_movie_torrent(tid))
    assert ok is True
    model = AnimeTorrent if line == "anime" else MovieTorrent
    with clean_tables.get_session() as s:
        t = s.get(model, tid)
        assert (t.status, t.archived_at, t.qb_progress, t.qb_synced_at, t.prev_status) == \
            ("sent", None, 0.0, None, None), (t.status, t.archived_at, t.qb_progress, t.qb_synced_at)
    assert await ce.archive_old_completed() == 0, "刚重下的种子被立刻再归档了"
    assert deleted == [], deleted


def test_callback_rescue_clears_the_stall_reason(clean_tables, two_tables):
    """(R34 对抗审计) stalled → 回调救回 sent 时把 fail_reason 一并清掉，否则剧场版页把它显示成『上次失败』。"""
    from core import engine as ce
    from db.models import AnimeTorrent, MovieTorrent

    reason = "从 qB 消失（已下 40%，半成品文件应仍在目录里）"
    two_tables(dict(status="stalled", qb_progress=0.4, fail_reason=reason),
               dict(status="stalled", qb_progress=0.4, fail_reason=reason))
    assert ce.mark_done_by_hash(H)
    tv, mv = _rows(clean_tables)
    assert (tv.status, tv.fail_reason) == ("sent", ""), (tv.status, tv.fail_reason)
    assert (mv.status, mv.fail_reason) == ("sent", ""), (mv.status, mv.fail_reason)
