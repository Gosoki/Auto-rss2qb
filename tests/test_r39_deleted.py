"""(r39 D11 / D8 / DEC1x) 已删除的语义与补齐口径。

D11（E-73）：删 = 这一集不要了。删的是同集（同番同集号，不分源）最后一份时置 ep_dropped，任何自动路径都不再为
这一集下别的版本；只删了重复份的不置 —— 留下那份以后被 relocate 清回待下、或 0% 落 error，还得能自动补回来。
D8：补齐的"这一集已有"按 dedup_key 算（歧义段带源），与 flush / 计划 / 即时同集闸同口径。
DEC1x：自动补齐不收没有发布时间的目标组结果（判断不了是不是前几季的同号集）。
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from db.models import Anime, AnimeTorrent

_OLD = datetime.now() - timedelta(hours=3)


def _mk_anime(s, **kw):
    base = dict(title="某番", display_name="某番", season=1, quarter="26C", confirmed=True, rejected=False,
                total_episodes=12, bangumi_id=1)
    base.update(kw)
    a = Anime(**base)
    s.add(a)
    s.commit()
    s.refresh(a)
    return a


def _mk_row(s, aid, n, ep, status, source="ANi", **kw):
    t = AnimeTorrent(anime_id=aid, info_hash=f"{n:040x}", source=source, site="nyaa",
                     raw_title=f"[{source}] 某番 - {int(ep):02d} [1080p]", season=1, episode=float(ep),
                     status=status, download_url=f"https://x/{n}.torrent", created_at=_OLD, **kw)
    s.add(t)
    s.commit()
    s.refresh(t)
    return t.id


def _row(clean_tables, tid):
    with clean_tables.get_session() as s:
        t = s.get(AnimeTorrent, tid)
        s.expunge(t)
        return t


def _qb_has_everything(monkeypatch):
    from core import engine

    async def info(hs):
        return {h: {"state": "stoppedUP", "save_path": "/dl"} for h in hs}

    async def delete(hs, delete_files=True):
        return True
    monkeypatch.setattr(engine.qb, "torrents_info", info)
    monkeypatch.setattr(engine.qb, "delete", delete)


def _no_real_delivery(monkeypatch):
    """自动路径只记录挑中了谁，不真交付。"""
    from core import anime as A
    sent = []

    async def fake_dl(tid, force=False, **kw):   # **kw：补齐下发传 prefs_gate（r39 D4）
        sent.append(tid)
        return True

    async def alive():
        return True
    monkeypatch.setattr(A, "download_anime_torrent", fake_dl)
    monkeypatch.setattr(A, "qb_precheck", alive)
    return sent


# ── 写入：删最后一份才置 ep_dropped ─────────────────────────────────

def test_deleting_the_last_copy_drops_the_episode_and_a_duplicate_does_not(clean_tables, cfg, monkeypatch):
    """同集两份：先删的那份是重复份（False），再删剩下那份才是最后一份（True）。已归档分支同样判。
    页面确认框用的 delete_drops_episode 与写库那一刻同一个判据。"""
    from core import anime as A
    _qb_has_everything(monkeypatch)
    with clean_tables.get_session() as s:
        a = _mk_anime(s)
        dup_a = _mk_row(s, a.id, 1, 1, "sent", "ANi")
        dup_b = _mk_row(s, a.id, 2, 1, "sent", "LoliHouse")
        arch = _mk_row(s, a.id, 3, 2, "sent", archived_at=_OLD, qb_progress=1.0)

    assert A.delete_drops_episode(dup_b) is False
    assert asyncio.run(A.delete_anime_torrent(dup_b)) is True
    t = _row(clean_tables, dup_b)
    assert (t.status, t.ep_dropped) == ("deleted", False), "删的是重复份：同集还有一份 sent"

    assert A.delete_drops_episode(dup_a) is True
    assert asyncio.run(A.delete_anime_torrent(dup_a)) is True
    assert _row(clean_tables, dup_a).ep_dropped is True, "剩下那份才是最后一份"

    assert A.delete_drops_episode(arch) is True
    assert asyncio.run(A.delete_anime_torrent(arch)) is True
    t = _row(clean_tables, arch)
    assert (t.status, t.ep_dropped) == ("deleted", True), "已归档分支也要判"


def test_last_copy_uses_the_coarse_key_but_grouping_stays_dedup_key(clean_tables, cfg, monkeypatch):
    """歧义段（O=12<T=24）：『是不是最后一份』按粗键 (番, 集) 判 —— NEST 13 还在盘上时删 ANi 13 不算最后一份；
    NEST 13 只是待下时删 ANi 13 算最后一份，但挡的是 dedup_key 那一组 (13, ANi)，NEST 13 照常会下。"""
    from core import anime as A
    _qb_has_everything(monkeypatch)
    cfg(ANIME_FINISH_UNSUB=False)
    with clean_tables.get_session() as s:
        a = _mk_anime(s, ep_offset=12, total_episodes=24)
        ani13 = _mk_row(s, a.id, 1, 13, "sent", "ANi")
        _mk_row(s, a.id, 2, 13, "sent", "NEST")
        ani14 = _mk_row(s, a.id, 3, 14, "sent", "ANi")
        nest14 = _mk_row(s, a.id, 4, 14, "pending", "NEST")
        aid = a.id
    asyncio.run(A.delete_anime_torrent(ani13))
    assert _row(clean_tables, ani13).ep_dropped is False, "粗键：同集号还有 NEST 那份在盘上"
    asyncio.run(A.delete_anime_torrent(ani14))
    assert _row(clean_tables, ani14).ep_dropped is True
    assert nest14 in A.download_plan(aid), "歧义段按 (集号, 源) 分组：删 ANi 14 不挡 NEST 14"


def test_deleting_the_whole_anime_marks_every_emptied_episode(clean_tables, cfg, monkeypatch):
    """整部删：同集两份都在这一批里也要标成已删（先整批置 deleted 再判）；交付中的同集行让路时不标。
    之后恢复订阅，被删光的集的 skipped 兄弟不复活。"""
    from core import anime as A
    _qb_has_everything(monkeypatch)
    with clean_tables.get_session() as s:
        a = _mk_anime(s, rejected=True)
        one = _mk_row(s, a.id, 1, 1, "sent", "ANi")
        one_skip = _mk_row(s, a.id, 2, 1, "skipped", "Mikan")
        two_a = _mk_row(s, a.id, 3, 2, "sent", "ANi")
        two_b = _mk_row(s, a.id, 4, 2, "sent", "Mikan")
        three = _mk_row(s, a.id, 5, 3, "sent", "ANi")
        _mk_row(s, a.id, 6, 3, "downloading", "Mikan")
        aid = a.id
    r = asyncio.run(A.delete_anime_files(aid))
    assert r["total"] == 4
    got = {tid: (_row(clean_tables, tid).status, _row(clean_tables, tid).ep_dropped)
           for tid in (one, two_a, two_b, three)}
    assert got == {one: ("deleted", True), two_a: ("deleted", True), two_b: ("deleted", True),
                   three: ("deleted", False)}, got
    A.restore_anime(aid)
    assert _row(clean_tables, one_skip).status == "skipped", "删光了的集，恢复订阅不复活"


# ── 读取：该集已删 → 所有自动路径都不碰 ───────────────────────────────

def _dropped_episode(clean_tables, dropped=True):
    """第 6 集：删过最后一份（D），还有待下 F、失败 H、被去重的 E。"""
    with clean_tables.get_session() as s:
        a = _mk_anime(s)
        ids = dict(
            D=_mk_row(s, a.id, 1, 6, "deleted", "ANi", ep_dropped=dropped),
            F=_mk_row(s, a.id, 2, 6, "pending", "Mikan", priority=10),
            H=_mk_row(s, a.id, 3, 6, "error", "NEST", priority=20),
            E=_mk_row(s, a.id, 4, 6, "skipped", "X"),
        )
        return a.id, ids


def test_a_dropped_episode_is_left_alone_by_every_automatic_path(clean_tables, cfg, monkeypatch):
    from core import anime as A
    cfg(ANIME_FINISH_UNSUB=False, ANIME_DOWNLOAD_GRACE_MIN=0)
    aid, ids = _dropped_episode(clean_tables)
    sent = _no_real_delivery(monkeypatch)

    assert A.download_plan(aid) == set()
    assert A.download_plan(aid, for_backfill=True) == set()
    assert A.download_plans_for_ids([aid]) == (set(), set())
    assert asyncio.run(A.flush_ready_downloads(qb_alive=True)) == 0
    assert asyncio.run(A.download_pending_for_anime(aid)) == 0
    assert asyncio.run(A.download_all_pending()) == 0
    assert asyncio.run(A._download_new_rows(aid, [ids["F"], ids["H"]])) == 0
    assert sent == []
    A._revive_orphaned_skipped()
    assert _row(clean_tables, ids["E"]).status == "skipped", "组里有 error 也不复活：用户不要这一集了"


def test_a_duplicate_deletion_still_lets_the_episode_come_back(clean_tables, cfg, monkeypatch):
    """同样的形状、但删的是重复份（ep_dropped=0）：存量 deleted 不再挡（E-73 有意放宽）。"""
    from core import anime as A
    cfg(ANIME_FINISH_UNSUB=False, ANIME_DOWNLOAD_GRACE_MIN=0)
    aid, ids = _dropped_episode(clean_tables, dropped=False)
    _no_real_delivery(monkeypatch)
    assert A.download_plan(aid) == {ids["F"]}
    A._revive_orphaned_skipped()
    assert _row(clean_tables, ids["E"]).status == "pending"


def test_a_kept_copy_that_dies_is_picked_up_again(clean_tables, cfg, monkeypatch):
    """删了重复份，留下的那份后来出事：
    · relocate 把它清回待下 → 计划里有它、flush 会重发它；
    · 0% 落 error → 被去重的兄弟复活、计划里有它。"""
    from core import anime as A
    cfg(ANIME_FINISH_UNSUB=False, ANIME_DOWNLOAD_GRACE_MIN=0)
    _qb_has_everything(monkeypatch)
    with clean_tables.get_session() as s:
        a = _mk_anime(s)
        keep1 = _mk_row(s, a.id, 1, 1, "sent", "ANi")
        dup1 = _mk_row(s, a.id, 2, 1, "sent", "Mikan")
        keep2 = _mk_row(s, a.id, 3, 2, "sent", "ANi")
        dup2 = _mk_row(s, a.id, 4, 2, "sent", "Mikan")
        sib2 = _mk_row(s, a.id, 5, 2, "skipped", "NEST")
        aid = a.id
    asyncio.run(A.delete_anime_torrent(dup1))
    asyncio.run(A.delete_anime_torrent(dup2))
    assert not _row(clean_tables, dup1).ep_dropped and not _row(clean_tables, dup2).ep_dropped
    with clean_tables.get_session() as s:
        s.get(AnimeTorrent, keep1).status = "pending"     # relocate._clear 的效果
        s.get(AnimeTorrent, keep2).status = "error"       # r38 的 0% 规则
        s.commit()
    sent = _no_real_delivery(monkeypatch)
    assert asyncio.run(A.flush_ready_downloads(qb_alive=True)) >= 1
    assert keep1 in sent
    assert _row(clean_tables, sib2).status == "pending", "flush 开头的换源兜底把兄弟复活了"
    assert sib2 in A.download_plan(aid) or sib2 in sent


def test_single_work_deleted_duplicate_then_kept_copy_dies_is_redelivered(clean_tables, cfg, monkeypatch):
    """单部作品（-2 不经即时同集闸，deliver_single_works 是唯一守卫）：删的是重复份、留下那份 0% 落 error →
    重新挑一份下。删的是最后一份的仍不下（见 test_r38_fixes 的『删过的片』）。"""
    from core import anime as A
    cfg(ANIME_SINGLE_WORK_AUTO=True, QB_ENABLED=True, ANIME_DOWNLOAD_GRACE_MIN=0, ANIME_FINISH_UNSUB=False)
    with clean_tables.get_session() as s:
        a = _mk_anime(s, total_episodes=1, platform="WEB")
        _mk_row(s, a.id, 1, -2, "deleted", "X", ep_dropped=False)
        _mk_row(s, a.id, 2, -2, "error", "Y")
        third = _mk_row(s, a.id, 3, -2, "pending", "Z")
    sent = _no_real_delivery(monkeypatch)
    assert asyncio.run(A.deliver_single_works()) == 1
    assert sent == [third]


@pytest.mark.parametrize("dropped", [True, False])
def test_a_dropped_episode_is_not_reported_as_locked_out(clean_tables, cfg, dropped):
    """锁了 ANi，ANi 停在第 5 集；第 6 集别的组下过一份、被用户删了（最后一份）→ 不算锁源缺集，也不进『只补这几集』。
    删的若是重复份（False），照旧报。"""
    from core import anime as A
    cfg(ANIME_LOCK_LAG_DAYS=7, ANIME_FINISH_UNSUB=False)
    long_ago = datetime.now() - timedelta(days=60)
    with clean_tables.get_session() as s:
        a = _mk_anime(s, pref_source="ANi")
        for ep in range(1, 6):
            _mk_row(s, a.id, ep, ep, "sent", "ANi")
        _mk_row(s, a.id, 10, 6, "deleted", "Mikan", ep_dropped=dropped)
        f = _mk_row(s, a.id, 11, 6, "pending", "NEST")
        s.get(AnimeTorrent, f).created_at = long_ago
        s.commit()
        s.refresh(a)
        s.expunge(a)
    want = {} if dropped else {6.0: True}
    assert {k: True for k in A.locked_out_candidates(a.id)} == want
    assert bool(A.locked_out_episodes([a])) is (not dropped)
    assert A.pending_breakdown()["locked"] == (0 if dropped else 1)
    with clean_tables.get_session() as s:
        ts = list(s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == a.id)))
    assert bool(A.locked_out_torrent_ids(ts)) is (not dropped)


# ── 即时同集闸与计划标注同口径 ───────────────────────────────────────

def test_plan_and_the_same_episode_gate_agree(clean_tables, cfg, monkeypatch):
    """计划不标『将下载』的，闸也挡（落 skipped、日志写『已有一份或该集已删』）；计划标了的，闸放行。"""
    from core import anime as A
    from core import engine as ce
    cfg(QB_ENABLED=True, QB_SYNC_STATUS=True, DOWN_PATH="/tmp/dl", ANIME_FINISH_UNSUB=False)

    async def ok_fetch(url):
        return b"torrent"

    async def ok_add(*a, **k):
        return True
    monkeypatch.setattr(ce, "fetch_torrent_bytes", ok_fetch)
    monkeypatch.setattr(ce, "add_to_qb", ok_add)
    with clean_tables.get_session() as s:
        a = _mk_anime(s)
        _mk_row(s, a.id, 1, 5, "deleted", "ANi", ep_dropped=True)
        gone = _mk_row(s, a.id, 2, 5, "pending", "Mikan")
        _mk_row(s, a.id, 3, 7, "deleted", "ANi", ep_dropped=False)
        dup = _mk_row(s, a.id, 4, 7, "pending", "Mikan")
        aid = a.id
    plan = A.download_plan(aid)
    assert gone not in plan and dup in plan
    assert asyncio.run(A.download_anime_torrent(gone)) is False
    assert _row(clean_tables, gone).status == "skipped"
    assert asyncio.run(A.download_anime_torrent(dup)) is True
    assert _row(clean_tables, dup).status == "sent"


# ── force：失败不给假承诺；成功清标记 ─────────────────────────────────

@pytest.mark.parametrize("how", ["fetch_fails", "qb_down"])
def test_force_failure_on_a_dropped_episode_lands_on_error_without_retry(clean_tables, cfg, monkeypatch, how):
    """该集已删的集上点『下载』失败：flush 认这一集有着落、永远不会重发它 → 落 error、不排重试，原因写明要再点一次。"""
    from core import anime as A
    from core import engine as ce
    cfg(QB_ENABLED=True, QB_SYNC_STATUS=True, DOWN_PATH="/tmp/dl")

    async def bad_fetch(url):
        raise RuntimeError("502")

    async def ok_fetch(url):
        return b"torrent"

    async def qb_gone(*a, **k):
        return None
    monkeypatch.setattr(ce, "fetch_torrent_bytes", bad_fetch if how == "fetch_fails" else ok_fetch)
    monkeypatch.setattr(ce, "add_to_qb", qb_gone)
    with clean_tables.get_session() as s:
        a = _mk_anime(s)
        _mk_row(s, a.id, 1, 5, "deleted", "ANi", ep_dropped=True)
        f = _mk_row(s, a.id, 2, 5, "pending", "Mikan")
        # 对照：同样的失败在没删过的集上照旧进重试队列 / 落回待下
        g = _mk_row(s, a.id, 3, 8, "pending", "Mikan")
    want_ret = False if how == "fetch_fails" else None
    assert asyncio.run(A.download_anime_torrent(f, force=True)) is want_ret
    t = _row(clean_tables, f)
    assert (t.status, t.retry_at) == ("error", None), (t.status, t.retry_at)
    assert "该集已删" in t.fail_reason and "再点『下载』" in t.fail_reason, t.fail_reason
    assert asyncio.run(A.download_anime_torrent(g, force=True)) is want_ret
    assert _row(clean_tables, g).status == "pending"


def test_force_success_clears_the_flag(clean_tables, cfg, monkeypatch):
    from core import anime as A
    from core import engine as ce
    cfg(QB_ENABLED=True, QB_SYNC_STATUS=True, DOWN_PATH="/tmp/dl")

    async def ok_fetch(url):
        return b"torrent"

    async def ok_add(*a, **k):
        return True
    monkeypatch.setattr(ce, "fetch_torrent_bytes", ok_fetch)
    monkeypatch.setattr(ce, "add_to_qb", ok_add)
    with clean_tables.get_session() as s:
        a = _mk_anime(s)
        d = _mk_row(s, a.id, 1, 5, "deleted", "ANi", ep_dropped=True)
        f = _mk_row(s, a.id, 2, 5, "pending", "Mikan")
    assert asyncio.run(A.download_anime_torrent(f, force=True)) is True
    assert _row(clean_tables, d).ep_dropped is False, "用户又要了这一集：以后这一份死了要能自动补"


def test_settling_a_stale_placeholder_clears_the_flag(clean_tables, cfg, monkeypatch):
    """force 交付在 add_to_qb 成功后崩溃 → 清扫按已交付落定。标记不清的话，这一份以后死了会被它自己的旧标记挡死。"""
    from core import engine as ce
    cfg(QB_ENABLED=True, QB_SYNC_STATUS=True)

    async def info(hs):
        return {h: {"save_path": "/dl/x"} for h in hs}
    monkeypatch.setattr(ce.qb, "torrents_info", info)
    with clean_tables.get_session() as s:
        a = _mk_anime(s)
        d = _mk_row(s, a.id, 1, 4, "deleted", "ANi", ep_dropped=True)
        r = _mk_row(s, a.id, 2, 4, "downloading", "Mikan", prev_status="pending")
    assert asyncio.run(ce.sweep_stale_delivering()) == 1
    assert _row(clean_tables, r).status == "sent"
    assert _row(clean_tables, d).ep_dropped is False


# ── 集号身份变化：并番 / 折号 ─────────────────────────────────────────

def test_merging_two_animes_clears_flags_that_the_other_side_covers(clean_tables, cfg):
    """并番：loser 删光的第 3 集在 keeper 那边有货 → 清；keeper 删光的第 5 集在 loser 那边有货 → 清；
    两边都没货的第 4 集保持已删。"""
    from core import anime as A
    with clean_tables.get_session() as s:
        keeper = _mk_anime(s, bangumi_id=7)
        loser = _mk_anime(s, bangumi_id=None, title="某番2")
        _mk_row(s, keeper.id, 1, 3, "sent", "ANi")
        l3 = _mk_row(s, loser.id, 2, 3, "deleted", "Mikan", ep_dropped=True)
        l4 = _mk_row(s, loser.id, 3, 4, "deleted", "Mikan", ep_dropped=True)
        k5 = _mk_row(s, keeper.id, 4, 5, "deleted", "ANi", ep_dropped=True)
        _mk_row(s, loser.id, 5, 5, "sent", "Mikan")
        kid, lid = keeper.id, loser.id
    with clean_tables.get_session() as s:
        A._merge_anime(s, lid, kid)
    assert [_row(clean_tables, i).ep_dropped for i in (l3, l4, k5)] == [False, True, False]


def test_refolding_into_an_episode_that_has_a_copy_clears_the_flag(clean_tables):
    """可折番（O=24 ≥ T=12）：绝对号 27 那份折回第 3 集，第 3 集的"该集已删"作废。"""
    from core import anime as A
    with clean_tables.get_session() as s:
        a = _mk_anime(s, ep_offset=24, total_episodes=12)
        _mk_row(s, a.id, 1, 27, "sent", "ANi")
        d = _mk_row(s, a.id, 2, 3, "deleted", "Mikan", ep_dropped=True)
        aid = a.id
    with clean_tables.get_session() as s:
        assert A._refold_absolute_episodes(s, s.get(Anime, aid)) == 1
        s.commit()
    assert _row(clean_tables, d).ep_dropped is False


# ── 页面：徽标排在重试分支之前 ─────────────────────────────────────────

@pytest.mark.nicegui_main_file("tests/render_detail_main.py")
async def test_detail_badge_for_a_dropped_episode_beats_the_retry_badge(user, clean_tables, cfg):
    """删除时兄弟行正在退避队列里：不能显示『重试中·X 之后自动重发』—— 那次重发永远不会发生。"""
    cfg(ANIME_FINISH_UNSUB=False)
    with clean_tables.get_session() as s:
        a = _mk_anime(s, jp_name="ボウバン")
        _mk_row(s, a.id, 1, 5, "deleted", "ANi", ep_dropped=True)
        _mk_row(s, a.id, 2, 5, "pending", "Mikan", retry_count=1,
                retry_at=datetime.now() + timedelta(hours=1), fail_reason="取种失败：502")
        aid = a.id
    await user.open(f"/detail/{aid}")
    tree = str(user.current_layout)
    assert "该集已删·不自动下" in tree
    assert "重试中" not in tree


def test_dashboard_recent_list_says_dropped_before_retrying(clean_tables, cfg):
    """(r39 F14) 仪表盘『新入库』与详情页同一判据、同一判序：落在"该集已删"那一集上的待下行（哪怕在退避队列里）
    显示『该集已删·不自动下』，不说『重试中』。只删了重复份的、同集还有一份在盘上的、歧义段里另一个源的同号集都不算。"""
    from core import anime as A
    from pages.layout import live_status
    cfg(ANIME_FINISH_UNSUB=False)
    soon = datetime.now() + timedelta(hours=1)
    with clean_tables.get_session() as s:
        a = _mk_anime(s, ep_offset=12, total_episodes=24)
        _mk_row(s, a.id, 1, 5, "deleted", "ANi", ep_dropped=True)
        gone = _mk_row(s, a.id, 2, 5, "pending", "Mikan", retry_count=1, retry_at=soon)
        _mk_row(s, a.id, 3, 6, "deleted", "ANi", ep_dropped=False)       # 只删了重复份
        dup = _mk_row(s, a.id, 4, 6, "pending", "Mikan")
        _mk_row(s, a.id, 5, 7, "deleted", "ANi", ep_dropped=True)
        _mk_row(s, a.id, 6, 7, "sent", "LoliHouse")                       # 同集还有一份在盘上
        have = _mk_row(s, a.id, 7, 7, "pending", "Mikan")
        _mk_row(s, a.id, 8, 14, "deleted", "ANi", ep_dropped=True)       # 歧义段 (12, 24]：按 (集号, 源) 分组
        nest = _mk_row(s, a.id, 9, 14, "pending", "NEST")
    rows = {r["id"]: r for r in A.recent_anime_rows(50)}
    assert rows[gone]["dropped"] is True
    assert not any(rows[i]["dropped"] for i in (dup, have, nest))
    r = rows[gone]
    assert live_status(r["status"], in_plan=False, episode=r["episode"], retry_at=r["retry_at"],
                       dropped=r["dropped"]) == ("该集已删·不自动下", "grey")
    assert live_status("pending", in_plan=False, episode=5.0, retry_at=soon)[0] == "重试中", "没删的照旧"


# ── 特别篇 / 未知集不是"同一集"（r39 F17）──────────────────────────────

def test_specials_of_a_normal_anime_never_count_as_the_last_copy_for_the_page(clean_tables, cfg):
    """粗键对 -1/-2 也成立：删普通番的 SP1（没有别的 -1 在盘上）时，确认框不能说"这一集仅剩的一份"。
    单部作品的 -2 就是整部作品，照旧按最后一份说。"""
    from core import anime as A
    with clean_tables.get_session() as s:
        a = _mk_anime(s)
        sp = _mk_row(s, a.id, 1, -1, "sent", "ANi")
        unk = _mk_row(s, a.id, 2, -2, "sent", "ANi")
        w = _mk_anime(s, total_episodes=1, platform="WEB", bangumi_id=2, title="某作品")
        whole = _mk_row(s, w.id, 3, -2, "sent", "ANi")
    assert A.delete_drops_episode(sp) is False
    assert A.delete_drops_episode(unk) is False
    assert A.delete_drops_episode(whole) is True


def test_force_failure_on_a_special_does_not_claim_the_episode_was_dropped(clean_tables, cfg, monkeypatch):
    """删过 SP1（ep_dropped）后 force 下 SP2 取种失败：原因里不能写"该集已删"—— SP1 和 SP2 不是同一集。
    -1 本来就不排重试，照旧落 error。"""
    from core import anime as A
    from core import engine as ce
    cfg(QB_ENABLED=True, QB_SYNC_STATUS=True, DOWN_PATH="/tmp/dl")

    async def bad_fetch(url):
        raise RuntimeError("502")
    monkeypatch.setattr(ce, "fetch_torrent_bytes", bad_fetch)
    with clean_tables.get_session() as s:
        a = _mk_anime(s)
        _mk_row(s, a.id, 1, -1, "deleted", "ANi", ep_dropped=True)
        sp2 = _mk_row(s, a.id, 2, -1, "pending", "Mikan")
    assert asyncio.run(A.download_anime_torrent(sp2, force=True)) is False
    t = _row(clean_tables, sp2)
    assert t.status == "error" and t.retry_at is None
    assert "该集已删" not in t.fail_reason, t.fail_reason


# ── D8：补齐的"已有"按 dedup_key ───────────────────────────────────────

def _bf(clean_tables, cfg, monkeypatch, found, *, rows, ep_offset=None, total=12, confirmed=True,
        air="2026-07-05", during_fetch=None):
    import sources
    from core import anime as A

    class _Stub:
        def __init__(self, *a, **kw):
            pass

        async def fetch(self, content_cache=None):
            if during_fetch:
                during_fetch()
            return list(found)

    for site in ("nyaa", "mikan"):
        monkeypatch.setitem(sources.SOURCES, site, _Stub)
        monkeypatch.setitem(sources.SEARCH_URL, site, lambda q: f"https://x/?q={q}")
    cfg(ANIME_BACKFILL_PREAIR_DAYS=30, ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100,
        ANIME_BACKFILL_EXTRA_EPS=0, ANIME_FINISH_UNSUB=False)
    sent = _no_real_delivery(monkeypatch)
    with clean_tables.get_session() as s:
        a = _mk_anime(s, title="某某歧义番", display_name="某某歧义番", ep_offset=ep_offset, total_episodes=total,
                      confirmed=confirmed, air_date=air)
        for i, (src, ep) in enumerate(rows):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{9000 + i:040x}", source=src, site="nyaa",
                               anime_title="某某歧义番", season=1, episode=float(ep), status="sent",
                               raw_title=f"[{src}] 某某歧义番 - {ep:02d}", created_at=datetime.now()))
        s.commit()
        return a.id, sent, A


def _item(group, ep, n, released=datetime(2026, 9, 1)):
    from sources.base import ParsedItem
    return ParsedItem(info_hash=f"{n:040x}", raw_title=f"[{group}] 某某歧义番 - {ep:02d} [1080p]",
                      anime_title="某某歧义番", season=1, episode=float(ep), quarter="26C",
                      release_time=released, download_url=f"https://x/{n}.torrent", source=group,
                      site="nyaa", search_names=["某某歧义番"])


def test_backfill_others_and_group_pick_use_dedup_key_in_the_ambiguous_range(clean_tables, cfg, monkeypatch):
    """歧义番 O=12、T=24：ANi 的 13/14 已下。NEST 的 13/14 是真正的另外两集 —— others 要列出来，挑定 NEST 要入库并各下一份。"""
    found = [_item("NEST", 13, 1), _item("NEST", 14, 2)]
    aid, sent, A = _bf(clean_tables, cfg, monkeypatch, found, rows=[("ANi", 13), ("ANi", 14)],
                       ep_offset=12, total=24)
    res = asyncio.run(A.backfill_source(aid))
    assert [(o["group"], o["eps"]) for o in res["others"]] == [("NEST", [13, 14])], res
    res = asyncio.run(A.backfill_source(aid, group="NEST", site="nyaa"))
    assert (res["ingested"], res["downloaded"]) == (2, 2), res
    assert len(sent) == 2


def test_backfill_still_filters_same_numbers_when_there_is_no_ambiguity(clean_tables, cfg, monkeypatch):
    found = [_item("NEST", 3, 1), _item("NEST", 4, 2)]
    aid, sent, A = _bf(clean_tables, cfg, monkeypatch, found, rows=[("ANi", 3), ("ANi", 4)])
    res = asyncio.run(A.backfill_source(aid))
    assert res["others"] == [], res
    res = asyncio.run(A.backfill_source(aid, group="NEST", site="nyaa"))
    assert (res["ingested"], res.get("downloaded")) == (0, 0), res
    assert sent == []


@pytest.mark.parametrize("ambiguous", [True, False])
def test_download_new_rows_keys_by_dedup_key(clean_tables, cfg, monkeypatch, ambiguous):
    from core import anime as A
    cfg(ANIME_FINISH_UNSUB=False)
    sent = _no_real_delivery(monkeypatch)
    with clean_tables.get_session() as s:
        a = _mk_anime(s, ep_offset=12 if ambiguous else None, total_episodes=24 if ambiguous else 12)
        _mk_row(s, a.id, 1, 13, "sent", "ANi")
        nest = _mk_row(s, a.id, 2, 13, "pending", "NEST")
        aid = a.id
    assert asyncio.run(A._download_new_rows(aid, [nest])) == (1 if ambiguous else 0)
    assert sent == ([nest] if ambiguous else [])


# ── DEC1x：自动补齐不收没有发布时间的 ───────────────────────────────────

@pytest.mark.parametrize("confirmed", [True, False])
def test_auto_backfill_skips_target_results_without_a_release_time(clean_tables, cfg, monkeypatch, confirmed):
    """自动补齐（不论番在不在追）：没有发布时间的目标组结果不入库、不下发，no_time 记一条。"""
    found = [_item("ANi", 2, 1, released=None)]
    aid, sent, A = _bf(clean_tables, cfg, monkeypatch, found, rows=[("ANi", 1)], confirmed=confirmed)
    res = asyncio.run(A.backfill_source(aid, name_filter=True))
    assert (res["ingested"], res["no_time"]) == (0, 1), res
    assert sent == []


def test_loose_backfill_and_group_pick_still_take_results_without_a_release_time(clean_tables, cfg, monkeypatch):
    """宽松『补齐该源』照旧入库并转待确认；挑定组照旧入库并下发（同类风险只报不改）。"""
    found = [_item("ANi", 2, 1, released=None), _item("NEST", 3, 2, released=None)]
    aid, sent, A = _bf(clean_tables, cfg, monkeypatch, found, rows=[("ANi", 1)])
    res = asyncio.run(A.backfill_source(aid))
    assert (res["ingested"], res["to_confirm"], res["no_time"]) == (1, True, 0), res
    res = asyncio.run(A.backfill_source(aid, group="NEST", site="nyaa"))
    assert (res["ingested"], res["downloaded"]) == (1, 1), res


@pytest.mark.parametrize("knob", ["preair_off", "no_air_date"])
def test_auto_backfill_without_a_time_criterion_does_not_skip(clean_tables, cfg, monkeypatch, knob):
    """PREAIR_DAYS=0（用户关了时间判据）或番没有 air_date：本来就判不了时间，不跳过。"""
    found = [_item("ANi", 2, 1, released=None)]
    aid, sent, A = _bf(clean_tables, cfg, monkeypatch, found, rows=[("ANi", 1)],
                       air=None if knob == "no_air_date" else "2026-07-05")
    if knob == "preair_off":
        cfg(ANIME_BACKFILL_PREAIR_DAYS=0)
    res = asyncio.run(A.backfill_source(aid, name_filter=True))
    assert (res["ingested"], res["no_time"]) == (1, 0), res
    assert len(sent) == 1, "在追的番：自动补齐直接下"


def test_confirming_during_the_fetch_is_not_undone(clean_tables, cfg, monkeypatch):
    """抓取期间用户点了『确认下载』：过滤与订阅态脱钩，番的确认状态不会被改回待确认。"""
    holder = {}

    def confirm_now():
        with clean_tables.get_session() as s:
            a = s.get(Anime, holder["aid"])
            a.confirmed = True
            s.add(a)
            s.commit()

    found = [_item("ANi", 2, 1), _item("ANi", 3, 2, released=None)]
    aid, sent, A = _bf(clean_tables, cfg, monkeypatch, found, rows=[("ANi", 1)], confirmed=False,
                       during_fetch=confirm_now)
    holder["aid"] = aid
    res = asyncio.run(A.backfill_source(aid, name_filter=True))
    assert (res["ingested"], res["no_time"], res["to_confirm"]) == (1, 1, False), res
    with clean_tables.get_session() as s:
        assert s.get(Anime, aid).confirmed is True
