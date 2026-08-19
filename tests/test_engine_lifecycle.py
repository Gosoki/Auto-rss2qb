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
