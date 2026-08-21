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
