"""采集轮末三处（换源兜底 / 修正版交付 / 修正版删旧）的预筛：结果不能变，稳态下不许再把整表拉进 Python。

  · _revive_orphaned_skipped 先按"有触发行的番"收窄：三种触发形状（排除 / 取种失败重试中 / 失败）各自单独
    出现时，skipped 兄弟都必须放回待下 —— 预筛判据写窄一种，那种形状的集就永久卡在失败源上（漏集）。
  · _deliver_new_versions 先列投影再取整行：取种失败在退避排队（retry_at 未到）的 v2 不抢跑，到点的照常交付。
  · _retire_old_versions 的 EXISTS 预筛与逐条查旧版同口径：停滞、没下完、行 id 比新版大的旧版照样被删。
  · 性能守卫：库里只有 v1、没有触发行时三个函数都不发取 animetorrent 整行的 SELECT；有触发行时换源兜底的
    大查询只扫有触发行的那几部番；_retire_old_versions 发出的语句数不随"已下完、旧版已删的 v2"条数增长。
"""
import asyncio
import re
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from core import anime as A
from core import engine as ce
from db.models import Anime, AnimeTorrent

OLD = datetime.now() - timedelta(hours=5)
_n = iter(range(1, 10 ** 6))


def _anime(s, **kw):
    base = dict(title="某番", display_name="某番", season=1, quarter="26C", confirmed=True, rejected=False,
                total_episodes=12, bangumi_id=1)
    base.update(kw)
    a = Anime(**base)
    s.add(a)
    s.commit()
    s.refresh(a)
    return a.id


def _row(s, aid, src, ep, status="pending", **kw):
    n = next(_n)
    base = dict(anime_id=aid, info_hash=f"{n:040x}", source=src, site="nyaa", season=1, episode=float(ep),
                raw_title=f"[{src}] 某番 - {int(ep):02d} [1080p]", status=status,
                download_url=f"https://x/{n}.torrent", created_at=OLD, release_time=OLD)
    base.update(kw)
    t = AnimeTorrent(**base)
    s.add(t)
    s.commit()
    s.refresh(t)
    return t.id


def _status(clean_tables, tid):
    with clean_tables.get_session() as s:
        return s.get(AnimeTorrent, tid).status


@pytest.fixture
def chain(cfg, monkeypatch):
    """真交付链：只把取种和加进 qB 两步换成记账。"""
    cfg(QB_ENABLED=True, QB_SYNC_STATUS=True, DOWN_PATH="/tmp/dl", ANIME_DOWN_PATH="",
        ANIME_FINISH_UNSUB=False, ANIME_DOWNLOAD_GRACE_MIN=0, ANIME_REPLACE_VERSIONS=True)
    fetched = []

    async def fetch(url):
        fetched.append(url)
        return b"d1:xe"

    async def add(*a, **k):
        return True

    async def notify(kind, msg, **kw):
        return True
    monkeypatch.setattr(ce, "fetch_torrent_bytes", fetch)
    monkeypatch.setattr(ce, "add_to_qb", add)
    monkeypatch.setattr(A, "notify_event", notify)
    return fetched


# ── 换源兜底：三种触发形状各自单独出现 ─────────────────────────────

def test_each_trigger_shape_alone_revives_its_skipped_sibling(clean_tables, cfg):
    """四部番：三部各只有一种触发形状（只排除 / 只取种失败重试中 / 只失败），第四部没有任何触发行。
    前三部的 skipped 兄弟都放回待下；第四部的 skipped 不动（它的首选还没试过）。"""
    cfg(ANIME_FINISH_UNSUB=False)
    sib = {}
    with clean_tables.get_session() as s:
        for name, (st, rc) in {"excluded": ("excluded", 0), "failing": ("pending", 1),
                               "error": ("error", 0), "none": ("pending", 0)}.items():
            aid = _anime(s, title=name, display_name=name)
            _row(s, aid, "首选", 1, st, retry_count=rc)
            _row(s, aid, "首选", 2, "sent", qb_progress=1.0)      # 别的集已下：不能连带挡住第 1 集
            sib[name] = _row(s, aid, "兄弟", 1, "skipped")
    A._revive_orphaned_skipped()
    got = {k: _status(clean_tables, v) for k, v in sib.items()}
    assert got == {"excluded": "pending", "failing": "pending", "error": "pending", "none": "skipped"}, got


# ── 修正版交付：退避中的 v2 不抢跑 ───────────────────────────────

def test_v2_waiting_for_retry_is_not_delivered_until_due(clean_tables, chain):
    """取种失败在退避排队的 v2（retry_at 在未来）不交付；同一轮里到点的那条照常交付。"""
    now = datetime.now()
    with clean_tables.get_session() as s:
        aid = _anime(s)
        for ep in (4, 5):
            _row(s, aid, "ANi", ep, "sent", qb_progress=1.0, raw_title=f"[ANi] 某番 - {ep:02d} [1080P][CHT][MP4]")
        later = _row(s, aid, "ANi", 4, "pending", retry_count=1, retry_at=now + timedelta(hours=2),
                     raw_title="[ANi] 某番 - 04 [1080P][CHT][V2][MP4]")
        due = _row(s, aid, "ANi", 5, "pending", retry_count=1, retry_at=now - timedelta(minutes=1),
                   raw_title="[ANi] 某番 - 05 [1080P][CHT][V2][MP4]")
    assert asyncio.run(A._deliver_new_versions()) == 1
    assert _status(clean_tables, later) == "pending"
    assert _status(clean_tables, due) == "sent"
    assert len(chain) == 1


# ── 修正版删旧：EXISTS 预筛不能比逐条查旧版窄 ─────────────────────────

V1 = "[ANi] 某番 - 41 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]"
V2 = "[ANi] 某番 - 41 [1080P][Baha][WEB-DL][AAC AVC][CHT][V2][MP4]"


def test_retire_takes_a_stalled_unfinished_old_with_larger_id(clean_tables, chain, monkeypatch):
    """旧版是『停滞』、进度没满、且行 id 比新版大（v2 先入库、v1 后从别的 feed 收到并被人工下过）：
    新版下完后照样要删它。EXISTS 写窄任何一项（漏 stalled / 要求 id 更小 / 要求旧版已下完）这条都红。"""
    with clean_tables.get_session() as s:
        aid = _anime(s)
        new = _row(s, aid, "ANi", 41, "sent", raw_title=V2, anime_title="某番", qb_progress=1.0)
        old = _row(s, aid, "ANi", 41, "stalled", raw_title=V1, anime_title="某番", qb_progress=0.4,
                   content_path="E:/A/41.mp4")
        assert old > new
        hashes = {"new": s.get(AnimeTorrent, new).info_hash, "old": s.get(AnimeTorrent, old).info_hash}
    calls = []

    async def info(hs):
        return {hashes["new"]: {"content_path": "E:/A/41 v2.mp4", "progress": 1.0, "state": "stoppedUP"},
                hashes["old"]: {"content_path": "E:/A/41.mp4", "progress": 0.4, "state": "stalledDL"}}

    async def delete(hs, delete_files=True):
        calls.append((tuple(hs), delete_files))
        return True
    monkeypatch.setattr(ce.qb, "torrents_info", info)
    monkeypatch.setattr(ce.qb, "delete", delete)
    assert asyncio.run(A._retire_old_versions()) == 1
    assert calls == [((hashes["old"],), True)]
    assert _status(clean_tables, old) == "deleted"


# ── 性能守卫 ──────────────────────────────────────────────────────

def _capture(engine, with_params=False):
    stmts = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        stmts.append((statement, parameters) if with_params else statement)
    sa.event.listen(engine, "before_cursor_execute", spy)
    return stmts, lambda: sa.event.remove(engine, "before_cursor_execute", spy)


def _select_list(stmt):
    m = re.match(r"\s*SELECT\s+(.*?)\s+FROM\s", stmt, re.S | re.I)
    return m.group(1) if m else ""


def _full_row_selects(stmts):
    """取 animetorrent 整行的 SELECT：列清单里同时有 download_url 与 qb_state（任何一处列投影都不取这两列）。"""
    return [x for x in stmts if "animetorrent.download_url" in _select_list(x)
            and "animetorrent.qb_state" in _select_list(x)]


def _steady_v1_library(s, n_anime=3):
    """只有 v1、没有失败/排除/重试中的行：已下完的、被同集去重压成 skipped 的、还在缓冲的待下。"""
    for i in range(n_anime):
        aid = _anime(s, title=f"番{i}", display_name=f"番{i}")
        for ep in range(1, 7):
            _row(s, aid, "A", ep, "sent", qb_progress=1.0)
            _row(s, aid, "B", ep, "skipped")
        _row(s, aid, "A", 7, "pending")


def test_steady_state_round_end_does_not_load_whole_rows(clean_tables, chain):
    with clean_tables.get_session() as s:
        _steady_v1_library(s)
    stmts, stop = _capture(clean_tables.engine)
    try:
        A._revive_orphaned_skipped()
        revive = list(stmts)
        stmts.clear()
        assert asyncio.run(A._deliver_new_versions()) == 0
        deliver = list(stmts)
        stmts.clear()
        assert asyncio.run(A._retire_old_versions()) == 0
        retire = list(stmts)
    finally:
        stop()
    # 换源兜底本来就是列投影，所以对它的判据更严：没有触发行时一行种子都不取（只问番 id）
    scans = [x for x in revive if "animetorrent.id" in _select_list(x)]
    assert not scans, f"没有任何触发行，换源兜底仍逐行扫了种子表：{scans}"
    assert not _full_row_selects(deliver), "修正版交付把待下/skipped 整行装配了"
    assert not _full_row_selects(retire), "修正版删旧把已下完的行整行装配了"


def test_revive_scan_is_narrowed_to_anime_with_trigger_rows(clean_tables, cfg):
    """三部番里只有一部有一条排除行：换源兜底逐行扫种子的那条大查询只许绑这一部的 id。
    生产库里长期有人工排除/落失败的行，"全库零触发行"几乎不出现 —— 真正省时间的是把大查询收窄。"""
    cfg(ANIME_FINISH_UNSUB=False)
    with clean_tables.get_session() as s:
        _steady_v1_library(s)
        hot = _anime(s, title="有排除", display_name="有排除")
        _row(s, hot, "A", 1, "excluded")
    stmts, stop = _capture(clean_tables.engine, with_params=True)
    try:
        A._revive_orphaned_skipped()
    finally:
        stop()
    scans = [(x, p) for x, p in stmts if "animetorrent.retry_count" in _select_list(x)
             and "animetorrent.id" in _select_list(x)]
    assert len(scans) == 1, scans
    stmt, params = scans[0]
    m = re.search(r"animetorrent\.anime_id IN \(([^)]*)\)", stmt)
    assert m and m.group(1).count("?") == 1, stmt
    assert hot in params, params


def _retire_stmt_count(clean_tables, n_v2):
    with clean_tables.get_session() as s:
        aid = _anime(s)
        for ep in range(1, n_v2 + 1):
            _row(s, aid, "ANi", ep, "deleted", qb_progress=1.0, fail_reason="已被 v2 替换",
                 raw_title=f"[ANi] 某番 - {ep:02d} [1080P][CHT][MP4]")
            _row(s, aid, "ANi", ep, "sent", qb_progress=1.0, raw_title=f"[ANi] 某番 - {ep:02d} [1080P][CHT][V2][MP4]")
    stmts, stop = _capture(clean_tables.engine)
    try:
        assert asyncio.run(A._retire_old_versions()) == 0
    finally:
        stop()
    return len(stmts)


def test_retire_statement_count_does_not_grow_with_finished_v2_rows(clean_tables, chain):
    """旧版早就删掉了的 v2 每轮不该各发一条 SQL 去找旧版：1 条与 10 条发出的语句数相同。"""
    from sqlmodel import delete
    one = _retire_stmt_count(clean_tables, 1)
    with clean_tables.get_session() as s:
        s.exec(delete(AnimeTorrent))
        s.exec(delete(Anime))
        s.commit()
    ten = _retire_stmt_count(clean_tables, 10)
    assert one == ten, f"1 条 v2 发 {one} 条语句，10 条发 {ten} 条"
