"""E-77 方案 C：用户标的『不属于本季』（AnimeTorrent.foreign_season）。

真库 anime#6『100 个女朋友 第三季』(12 集) 底下挂着第一季的 '- 01..12/24'，已 sent：
  · 集去重（_covered / _COVERED_SQL）把它们算"这一集已有"，本季真正的第 9..12 集永远下不下来；
  · 删掉它们也不行：E-73 判成"该集已删"（ep_dropped），照样挡。
标成『不属于本季』之后，这一行对本番的集号逻辑"不存在"（去重、最后一份、巡检、绑定可疑都不认它），文件和行都在；
自动路径永不下它、不拿它当修正版的旧版、搬迁时 qB 不认识也不清状态。只能由用户点按钮产生。
"""
import asyncio
from datetime import date, datetime, timedelta

import pytest
from sqlmodel import select

from core import anime as A
from core import engine as ce
from db.models import Anime, AnimeTorrent

_CREATED = datetime.now() - timedelta(hours=3)


@pytest.fixture(autouse=True)
def _fresh_notify_state():
    """同 test_r39_sweeps：通知的进程内冷却是模块级的，番 id 在用例间会重复。"""
    from services import notify as _N
    _N._last_sent.clear()
    yield
    _N._last_sent.clear()


def _at(d: date, weeks: float) -> datetime:
    return datetime(d.year, d.month, d.day) + timedelta(weeks=weeks)


def _mk(db, rows, *, total=12, air=None, season=3, **kw):
    """建一部在追的番 + 种子。rows = [(标签, 集号, 状态, release_time, 额外列 dict 可省)]。返回 (anime_id, {标签: id})。"""
    with db.get_session() as s:
        a = Anime(**{**dict(title="百女友", display_name="百女友 第三季", season=season, quarter="26C", confirmed=True,
                            rejected=False, total_episodes=total, air_date=air.isoformat() if air else None,
                            bangumi_id=598058), **kw})
        s.add(a)
        s.commit()
        s.refresh(a)
        ids = {}
        for i, r in enumerate(rows):
            tag, ep, st, rel = r[:4]
            extra = dict(r[4]) if len(r) > 4 else {}
            src = extra.pop("source", "ANi")
            t = AnimeTorrent(anime_id=a.id, info_hash=f"{a.id * 1000 + i:040x}", source=src, site="nyaa",
                             anime_title="百女友", season=extra.pop("season", 1), episode=float(ep), status=st,
                             qb_progress=1.0 if st == "sent" else 0.0, release_time=rel,
                             raw_title=extra.pop("raw_title", f"[{src}] 百女友 - {int(ep):02d} [1080p]"),
                             download_url=f"https://x/{a.id}-{i}.torrent",
                             created_at=extra.pop("created_at", _CREATED), **extra)
            s.add(t)
            s.commit()
            ids[tag] = t.id
        return a.id, ids


def _six(db, tail="pending", old_status="sent", **kw):
    """#6 形状：本季 12 集、首播 17 周前；第一季 1..12 早 150 周且 old_status（默认已 sent）；本季 1..8 sent；本季 9..12 按 tail。"""
    d = date.today() - timedelta(weeks=17)
    rows = [(f"old{e}", e, old_status, _at(d, -150 + e)) for e in range(1, 13)]
    rows += [(f"cur{e}", e, "sent", _at(d, e - 1)) for e in range(1, 9)]
    if tail:
        rows += [(f"cur{e}", e, tail, _at(d, e - 1)) for e in range(9, 13)]
    aid, ids = _mk(db, rows, air=d, **kw)
    return aid, ids


def _row(db, tid):
    with db.get_session() as s:
        t = s.get(AnimeTorrent, tid)
        s.expunge(t)
        return t


def _anime(db, aid):
    with db.get_session() as s:
        a = s.get(Anime, aid)
        s.expunge(a)
        return a


def _real_delivery(cfg, monkeypatch, tmp_path):
    """真跑 download_anime_torrent 的整条交付链，只把取种和 qB 换成假的。返回取过的 URL 列表。"""
    cfg(QB_ENABLED=True, QB_SYNC_STATUS=True, DOWN_PATH=str(tmp_path), ANIME_DOWN_PATH="",
        ANIME_FINISH_UNSUB=False, ANIME_DOWNLOAD_GRACE_MIN=0)
    fetched = []

    async def fetch(url):
        fetched.append(url)
        return b"d1:xe"

    async def add(*a, **k):
        return True

    async def notify(kind, msg, **kw):
        return True

    async def alive():
        return True
    monkeypatch.setattr(ce, "fetch_torrent_bytes", fetch)
    monkeypatch.setattr(ce, "add_to_qb", add)
    monkeypatch.setattr(A, "notify_event", notify)
    monkeypatch.setattr(A, "qb_precheck", alive)
    return fetched


# ==================== #6：标记前挡住、标记后照常下 ====================

def test_six_shape_tail_is_blocked_until_marked_then_flush_delivers_it(clean_tables, cfg, monkeypatch, tmp_path):
    fetched = _real_delivery(cfg, monkeypatch, tmp_path)
    aid, ids = _six(clean_tables)
    tail = {ids[f"cur{e}"] for e in range(9, 13)}
    assert not (A.download_plan(aid) & tail), "前提：第一季的 sent 占着 9..12，本季那几集不会被挑"
    assert A.mark_slot_impostors_foreign(aid) == 12
    assert A.download_plan(aid) == tail, "标完之后本季 9..12 就是这一集的首选"

    n = asyncio.run(A.flush_ready_downloads(qb_alive=True))
    assert n == 4
    assert _row(clean_tables, ids["cur9"]).status == "sent", "本季第 9 集没交付出去"
    assert len(fetched) == 4
    for e in range(1, 13):
        t = _row(clean_tables, ids[f"old{e}"])
        assert (t.status, t.foreign_season) == ("sent", True), "别季行的状态被动了"


def test_marking_releases_the_skipped_tail(clean_tables, cfg):
    """生产上本季 9..12 早被即时同集闸落成了 skipped：标完要放回待下，否则 flush/补下/计划都不挑 skipped —— 照样漏集。
    同集还有别的本季份（这里 ep10 另有一份 sent）的不放；已忽略的番不放（恢复订阅时自然会放）。"""
    cfg(ANIME_FINISH_UNSUB=False)
    aid, ids = _six(clean_tables, tail="skipped")
    with clean_tables.get_session() as s:
        s.add(AnimeTorrent(anime_id=aid, info_hash="e" * 40, source="LoliHouse", episode=10.0, status="sent",
                           raw_title="[LoliHouse] 百女友 - 10", created_at=_CREATED))
        s.commit()
    assert A.mark_slot_impostors_foreign(aid) == 12
    got = {e: _row(clean_tables, ids[f"cur{e}"]).status for e in range(9, 13)}
    assert got == {9: "pending", 10: "skipped", 11: "pending", 12: "pending"}, got
    assert A.download_plan(aid) == {ids["cur9"], ids["cur11"], ids["cur12"]}

    rej, rids = _six(clean_tables, tail="skipped", rejected=True)
    assert A.mark_slot_impostors_foreign(rej) == 12
    assert {_row(clean_tables, rids[f"cur{e}"]).status for e in range(9, 13)} == {"skipped"}
    A.restore_anime(rej)
    assert {_row(clean_tables, rids[f"cur{e}"]).status for e in range(9, 13)} == {"pending"}, \
        "恢复订阅时（_covered 不认别季行）才放回"


def test_single_mark_does_not_release_another_groups_old_season_row(clean_tables, cfg):
    """逐条按钮只标了 ANi 的第一季第 9 集：同一集里别的组发的第一季旧集（还没标、躺在 skipped）不许放回待下 ——
    放回去 flush 可能正好挑中它，把前一季的内容又下进本季第 9 集的槽位。本季那份照常放回。"""
    cfg(ANIME_FINISH_UNSUB=False)
    d = date.today() - timedelta(weeks=17)
    aid, ids = _mk(clean_tables, [
        ("old9", 9, "sent", _at(d, -141)),
        ("old9b", 9, "skipped", _at(d, -141), {"source": "LoliHouse", "raw_title": "[LoliHouse] 百女友 - 09"}),
        ("cur9", 9, "skipped", _at(d, 8)),
    ], air=d)
    assert A.set_foreign_season(ids["old9"], True)
    assert _row(clean_tables, ids["cur9"]).status == "pending"
    assert _row(clean_tables, ids["old9b"]).status == "skipped", "别的组的第一季旧集被放回待下了"
    assert A.download_plan(aid) == {ids["cur9"]}


def test_button_offered_only_for_rows_that_look_like_another_season(cfg):
    """季号不同不够：很多组发续作不写季号（解析成第 1 季），生产 #6 就有本季播出期间发布、季号为 1 的本季正片。
    要再加"发布早于本季首播 − ANIME_BACKFILL_PREAIR_DAYS 天"才给按钮；占槽别季行照旧给；特别篇不给。"""
    cfg(ANIME_BACKFILL_PREAIR_DAYS=30)
    d = date(2026, 7, 5)
    a = Anime(title="x", season=3, total_episodes=12, air_date=d.isoformat())

    def row(ep, season, rel):
        return AnimeTorrent(episode=float(ep), season=season, release_time=rel)
    assert A.foreign_season_suspect(a, row(5, 1, _at(d, -140))), "占槽别季行"
    assert A.foreign_season_suspect(a, row(20, 1, _at(d, -10))), "季号不同、首播前十周发布：多半是前一季"
    assert not A.foreign_season_suspect(a, row(5, 1, _at(d, 4))), "本季播出期间发布、只是没写季号：不给"
    assert not A.foreign_season_suspect(a, row(1, 1, _at(d, -1))), "首播前一周的抢先：不给"
    assert not A.foreign_season_suspect(a, row(5, 3, _at(d, -10))), "季号相同：不给"
    assert not A.foreign_season_suspect(a, row(-1, 1, _at(d, -140))), "特别篇：不给"
    assert not A.foreign_season_suspect(a, row(5, 1, None)), "没有发布时间：判不了就不给"


def test_mark_count_and_idempotence(clean_tables, cfg):
    """只标 _slot_impostor 认得出、还没标、不在交付中的；再点一次是 0。本季的行、没有发布时间的、交付中的都不碰。"""
    aid, ids = _six(clean_tables)
    with clean_tables.get_session() as s:
        s.get(AnimeTorrent, ids["old3"]).status = "downloading"
        s.get(AnimeTorrent, ids["old4"]).release_time = None     # 判不了：留给逐条按钮
        s.commit()
    assert A.mark_slot_impostors_foreign(aid) == 10
    assert A.mark_slot_impostors_foreign(aid) == 0, "不幂等"
    flags = {k: _row(clean_tables, v).foreign_season for k, v in ids.items()}
    assert {k for k, f in flags.items() if f} == {f"old{e}" for e in range(1, 13)} - {"old3", "old4"}
    assert A.mark_slot_impostors_foreign(999999) == 0


def test_set_foreign_season_rules_and_undo_restores_the_slot(clean_tables, cfg):
    """只许标正集、不碰交付中；已是这个值返回 False。撤销后原样占位：本季那一集又被挡住（被挡的仍是待下，不改状态）。"""
    cfg(ANIME_FINISH_UNSUB=False)
    aid, ids = _six(clean_tables)
    with clean_tables.get_session() as s:
        sp = AnimeTorrent(anime_id=aid, info_hash="f" * 40, source="ANi", episode=-1.0, status="sent",
                          raw_title="[ANi] 百女友 SP", created_at=_CREATED)
        s.add(sp)
        s.get(AnimeTorrent, ids["old2"]).status = "downloading"
        s.commit()
        sp_id = sp.id
    assert A.set_foreign_season(sp_id, True) is False, "特别篇不占集号，不给标"
    assert A.set_foreign_season(ids["old2"], True) is False, "交付中的不动"
    assert A.set_foreign_season(ids["old9"], False) is False, "本来就没标"
    assert A.set_foreign_season(ids["old9"], True) is True
    assert A.set_foreign_season(ids["old9"], True) is False, "已经标过"
    assert ids["cur9"] in A.download_plan(aid)
    assert A.set_foreign_season(ids["old9"], False) is True
    assert ids["cur9"] not in A.download_plan(aid), "撤销后没恢复占位"
    assert _row(clean_tables, ids["cur9"]).status == "pending"


# ==================== 永不自动下 ====================

def test_foreign_pending_is_never_picked_but_force_still_works(clean_tables, cfg, monkeypatch, tmp_path):
    """标了的待下行：计划 / flush / 补下 / 批量补下都不挑它；非 force 交付返回 False、什么都不写；force 照常下。"""
    fetched = _real_delivery(cfg, monkeypatch, tmp_path)
    d = date.today() - timedelta(weeks=5)
    aid, ids = _mk(clean_tables, [("x", 3, "pending", _at(d, 2)), ("y", 4, "error", _at(d, 3))], air=d)
    assert A.set_foreign_season(ids["x"], True) and A.set_foreign_season(ids["y"], True)
    assert A.download_plan(aid) == set() and A.download_plan(aid, for_backfill=True) == set()
    assert asyncio.run(A.flush_ready_downloads(qb_alive=True)) == 0
    assert asyncio.run(A.download_pending_for_anime(aid)) == 0
    assert asyncio.run(A.download_all_pending()) == 0
    assert asyncio.run(A.download_locked_out(aid)) == 0
    assert fetched == []

    assert asyncio.run(A.download_anime_torrent(ids["x"])) is False
    assert asyncio.run(A.download_anime_torrent(ids["x"], prefs_gate=False)) is False, "越过锁定的入口也不下它"
    t = _row(clean_tables, ids["x"])
    assert (t.status, t.fail_reason, t.prev_status, t.retry_count) == ("pending", "", None, 0), "非 force 被挡时写了状态"
    assert fetched == []

    assert asyncio.run(A.download_anime_torrent(ids["x"], force=True)) is True
    t = _row(clean_tables, ids["x"])
    assert (t.status, t.foreign_season) == ("sent", True), "force 被挡了，或交付把标记清了"


def test_skipped_foreign_sibling_is_not_revived(clean_tables, cfg):
    """换源兜底：首选源失败时复活同集 skipped 兄弟 —— 别季的那条不复活，它的 error 也不算"本季首选源不行了"。
    标记直接写库：走 set_foreign_season 的话，它自己会放回让出那一集的 skipped（另一条用例测），这里只看兜底这一条路。"""
    cfg(ANIME_FINISH_UNSUB=False)
    d = date.today() - timedelta(weeks=5)
    aid, ids = _mk(clean_tables, [("bad", 2, "error", _at(d, 1)),
                                  ("fsk", 2, "skipped", _at(d, 1), {"source": "X", "foreign_season": True}),
                                  ("fbad", 3, "error", _at(d, 2), {"foreign_season": True}),
                                  ("sk3", 3, "skipped", _at(d, 2), {"source": "Y"})],
                   air=d)
    A._revive_orphaned_skipped()
    assert _row(clean_tables, ids["fsk"]).status == "skipped", "别季的 skipped 被复活了"
    assert _row(clean_tables, ids["sk3"]).status == "skipped", "别季行的 error 被当成本季首选源失败"


# ==================== 删除 / 最后一份 ====================

def test_deleting_a_foreign_row_never_drops_the_episode(clean_tables, cfg, monkeypatch):
    """删一条别季的行永远不是"这一集不要了"；本季那份的"最后一份"也不把别季兄弟算成"还剩一份"。"""
    async def info(hs):
        return {h: {"state": "stoppedUP"} for h in hs}

    async def delete(hs, delete_files=True):
        return True
    monkeypatch.setattr(ce.qb, "torrents_info", info)
    monkeypatch.setattr(ce.qb, "delete", delete)
    d = date.today() - timedelta(weeks=5)
    aid, ids = _mk(clean_tables, [("f5", 5, "sent", _at(d, 4)), ("f6", 6, "sent", _at(d, 5)),
                                  ("c6", 6, "sent", _at(d, 5), {"source": "LoliHouse"})], air=d)
    assert A.set_foreign_season(ids["f5"], True) and A.set_foreign_season(ids["f6"], True)
    assert A.delete_drops_episode(ids["f5"]) is False
    assert asyncio.run(A.delete_anime_torrent(ids["f5"])) is True
    t = _row(clean_tables, ids["f5"])
    assert (t.status, t.ep_dropped) == ("deleted", False)

    assert A.delete_drops_episode(ids["c6"]) is True, "别季的 f6 不算同集还剩一份"
    asyncio.run(A.delete_anime_torrent(ids["c6"]))
    assert _row(clean_tables, ids["c6"]).ep_dropped is True


def test_dropped_old_row_marked_foreign_unblocks_and_undo_restores(clean_tables, cfg):
    """#6 用户先删过第一季那批（删光 → ep_dropped），再标成不属于本季：本季那一集放行；并番/折号的
    _clear_stale_dropped 不碰别季行的标记；撤销后"该集已删"原样回来。"""
    cfg(ANIME_FINISH_UNSUB=False)
    aid, ids = _six(clean_tables, old_status="deleted")
    with clean_tables.get_session() as s:
        for e in range(1, 13):
            s.get(AnimeTorrent, ids[f"old{e}"]).ep_dropped = True
        s.commit()
    assert ids["cur9"] not in A.download_plan(aid), "前提：该集已删挡着"
    assert A.mark_slot_impostors_foreign(aid) == 12
    assert ids["cur9"] in A.download_plan(aid)
    with clean_tables.get_session() as s:
        A._clear_stale_dropped(s, aid)
        s.commit()
    assert _row(clean_tables, ids["old9"]).ep_dropped is True, "别季行的标记被顺手清了，撤销就回不去了"
    assert A.set_foreign_season(ids["old9"], False)
    assert ids["cur9"] not in A.download_plan(aid), "撤销后没恢复成该集已删"


# ==================== 巡检与统计不计它 ====================

def test_stats_do_not_count_foreign_rows(clean_tables, cfg):
    """release_time 判不了的别季行（_slot_impostor 认不出）由用户逐条标：标完下齐覆盖 / 已下集数 / 缺集都不再计它。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100, ANIME_FINISH_ENABLED=True)
    d = date.today() - timedelta(weeks=17)
    rows = [(f"cur{e}", e, "sent", _at(d, e - 1), {"season": 3}) for e in range(1, 9)]
    rows += [(f"old{e}", e, "sent", None) for e in range(9, 13)]
    aid, ids = _mk(clean_tables, rows, air=d)
    a = _anime(clean_tables, aid)
    assert A.episode_coverage([aid])[aid] == set(range(1, 13)), "前提：没有发布时间，_slot_impostor 认不出"
    assert A.episode_progress([aid])[aid] == (12, 12) and A.episode_gaps([a]) == {}
    for e in range(9, 13):
        assert A.set_foreign_season(ids[f"old{e}"], True)
    assert A.episode_coverage([aid])[aid] == set(range(1, 9))
    assert A.is_finished(a, A.episode_coverage([aid])[aid]) is False
    assert A.episode_progress([aid])[aid] == (8, 8)
    assert A.episode_gaps([a]) == {aid: [9, 10, 11, 12]}
    assert A.slot_held_gaps({aid: [9, 10, 11, 12]}) == {}, "标了的行不算占槽"


def test_over_total_foreign_rows_still_trip_the_two_numberings_gate(clean_tables):
    """（与 A6 同一处理）超界的别季行在下齐覆盖里照旧计：剔掉会让两套编号闸少数几个超界集号，
    is_finished 从 False 翻成 True —— 开了停订就停掉一部还在播的番。"""
    d = date.today() - timedelta(weeks=60)
    rows = [(f"c{e}", e, "sent", _at(d, e - 1)) for e in range(1, 13)]
    rows += [(f"o{k}", 12 + k, "sent", _at(d, 48 + k)) for k in range(1, 4)]
    aid, ids = _mk(clean_tables, rows, air=d)
    for k in range(1, 4):
        assert A.set_foreign_season(ids[f"o{k}"], True)
    cov = A.episode_coverage([aid])[aid]
    assert {13, 14, 15} <= cov
    assert A.is_finished(_anime(clean_tables, aid), cov) is False


def test_slot_held_gaps_and_wrong_binding_clear_after_marking(clean_tables, cfg):
    """#6 本季 9..12 从没进过库：标之前是"占槽缺集"+"绑定看着不对"；标完占槽没了（缺集照报，可以去补齐了），
    绑定告警自然消失；补齐的补搜集号也不再被别季行挡掉。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    aid, _ = _six(clean_tables, tail=None)
    a = _anime(clean_tables, aid)
    gaps = A.episode_gaps([a])
    assert gaps == {aid: [9, 10, 11, 12]} and A.slot_held_gaps(gaps) == {aid: [9, 10, 11, 12]}
    assert aid in {w["id"] for w in A.suspect_wrong_binding()}
    with clean_tables.get_session() as s:
        assert A.binding_looks_wrong(s, s.get(Anime, aid)) is True

    assert A.mark_slot_impostors_foreign(aid) == 12
    gaps = A.episode_gaps([a])
    assert gaps == {aid: [9, 10, 11, 12]} and A.slot_held_gaps(gaps) == {}
    assert aid not in {w["id"] for w in A.suspect_wrong_binding()}
    with clean_tables.get_session() as s:
        assert A.binding_looks_wrong(s, s.get(Anime, aid)) is False


def test_numbering_conflict_and_rerelease_ignore_foreign_rows(clean_tables, cfg):
    """疑似两套编号、疑似同号新季重发 —— 标了的别季行都不再当证据。"""
    cfg(ANIME_RERELEASE_WEEKS=8)
    d = date.today() - timedelta(weeks=20)
    rows = [(f"c{e}", e, "sent", _at(d, e - 1)) for e in range(1, 6)]
    rows += [(f"o{k}", 12 + k, "sent", _at(d, 1), {"source": "Nix"}) for k in range(1, 4)]
    rows += [("late", 3, "pending", _at(d, 15), {"source": "Erai"})]
    aid, ids = _mk(clean_tables, rows, air=d)
    assert A.episode_numbering_conflict(aid) and aid in {x["id"] for x in A.suspect_rerelease_collision()}
    for k in range(1, 4):
        A.set_foreign_season(ids[f"o{k}"], True)
    assert A.episode_numbering_conflict(aid) == []
    assert all(src != "Nix" for src, *_ in A.episode_ranges_by_source(aid))
    A.set_foreign_season(ids["late"], True)
    assert aid not in {x["id"] for x in A.suspect_rerelease_collision()}


def test_locked_group_foreign_rows_do_not_count_as_its_episodes(clean_tables, cfg):
    """锁定组的别季行不算它发过的集（不抬高 lock_max）：锁 ANi、ANi 本季只到第 3 集，第一季的 1..12 标了之后，
    别的组的 4..6 就是"被锁挡住的本季集"；别季的待下行自己也不算锁源缺集。"""
    cfg(ANIME_LOCK_LAG_DAYS=1, ANIME_LOCK_SYNC_DAYS=2, ANIME_FINISH_UNSUB=False)
    d = date.today() - timedelta(weeks=17)
    rows = [(f"old{e}", e, "sent", _at(d, -150 + e)) for e in range(1, 13)]
    rows += [(f"c{e}", e, "sent", _at(d, e - 1)) for e in range(1, 4)]
    rows += [(f"n{e}", e, "pending", _at(d, e - 1), {"source": "NEST", "created_at": datetime.now() - timedelta(days=10)})
             for e in range(4, 7)]
    rows += [("fp", 7, "pending", _at(d, 6), {"source": "NEST", "created_at": datetime.now() - timedelta(days=10)})]
    aid, ids = _mk(clean_tables, rows, air=d, pref_source="ANi")
    a = _anime(clean_tables, aid)
    assert A.locked_out_episodes([a]) == {}, "前提：第一季的 ANi 1..12 让锁定组看着发到了第 12 集"
    A.mark_slot_impostors_foreign(aid)
    A.set_foreign_season(ids["fp"], True)
    lo = A.locked_out_episodes([a])
    assert lo[aid]["eps"] == [4, 5, 6] and lo[aid]["lock_max"] == 3
    with clean_tables.get_session() as s:
        ts = list(s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid)))
    assert A.locked_out_torrent_ids(ts) == {ids["n4"], ids["n5"], ids["n6"]}
    pb = A.pending_breakdown()
    assert pb["locked"] == 3 and pb["backup"] == 1, pb


# ==================== 修正版替换 ====================

def _versions(clean_tables, cfg, monkeypatch, rows):
    cfg(ANIME_REPLACE_VERSIONS=True, QB_ENABLED=True, ANIME_DOWNLOAD_GRACE_MIN=0, ANIME_FINISH_UNSUB=False)
    calls = []
    rel = datetime.now() - timedelta(days=1)
    aid, ids = _mk(clean_tables, [(k, 4, st, rel, {"raw_title": raw, "content_path": f"/dl/{k}.mp4"})
                                  for k, raw, st in rows], air=date.today() - timedelta(weeks=5))

    async def fake_download(tid, force=False, **kw):
        calls.append(("download", tid))
        return True

    async def fake_info(hs):
        return {h: {"content_path": f"/dl/{h}.mp4", "progress": 1.0, "state": "stoppedUP"} for h in hs}

    async def fake_delete(hs, delete_files=True):
        calls.append(("delete", tuple(hs)))
        return True
    monkeypatch.setattr(A, "download_anime_torrent", fake_download)
    monkeypatch.setattr(ce.qb, "torrents_info", fake_info)
    monkeypatch.setattr(ce.qb, "delete", fake_delete)
    return aid, ids, calls


def test_version_replacement_never_touches_foreign_rows(clean_tables, cfg, monkeypatch):
    """别季的旧版不当成"我们那份"去替换；别季的 v2 不自动下（走 force，交付里的总闸挡不住，只能靠候选过滤）；
    别季的新版下完也不拿来删本季的旧版。"""
    aid, ids, calls = _versions(clean_tables, cfg, monkeypatch, [
        ("v1", "[ANi] 百女友 - 04 [1080P][CHT]", "sent"),
        ("v2", "[ANi] 百女友 - 04 [1080P][CHT][V2]", "skipped"),
    ])
    A.set_foreign_season(ids["v1"], True)
    assert asyncio.run(A.replace_versions()) == (0, 0) and calls == [], "别季的旧版被当成要替换的那份"
    assert A._version_pick_ok(ids["v2"]) is False

    A.set_foreign_season(ids["v1"], False)
    A.set_foreign_season(ids["v2"], True)
    assert asyncio.run(A.replace_versions()) == (0, 0) and calls == [], "别季的 v2 被自动下了"

    with clean_tables.get_session() as s:
        v2 = s.get(AnimeTorrent, ids["v2"])
        v2.status, v2.qb_progress = "sent", 1.0
        s.commit()
    assert asyncio.run(A._retire_old_versions()) == 0 and calls == [], "别季的新版顶掉了本季的旧版"
    assert _row(clean_tables, ids["v1"]).status == "sent"


# ==================== 搬迁 ====================

async def test_relocate_keeps_untracked_foreign_rows(clean_tables, cfg, monkeypatch):
    """qB 不认识的别季行：像特别篇一样不清成待下（清了也没有路径会下回来），报告单列 foreign_kept，
    不进"旧文件在哪"的目录集合。"""
    cfg(QB_ENABLED=True, DOWN_PATH="/data", ANIME_DOWN_PATH="", QUARTER_FMT="{yy}{q}",
        ANIME_SEASON_SUBFOLDER=False)
    with clean_tables.get_session() as s:
        a = Anime(title="番", display_name="番", quarter="26A", confirmed=True)
        s.add(a); s.commit(); s.refresh(a)
        for h, ep, sp in (("a", 1, "/data/25D/番"), ("b", 2, "/data/25C/别季")):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=h * 40, raw_title=f"[组][{h}]", episode=ep, status="sent",
                               qb_progress=1.0, save_path=sp, created_at=datetime.now()))
        s.commit()
        aid = a.id
        fb = s.exec(select(AnimeTorrent.id).where(AnimeTorrent.info_hash == "b" * 40)).first()
    assert A.set_foreign_season(fb, True)

    async def info(hashes):
        return {}                     # qB 两条都不认识（remove-on-complete）
    monkeypatch.setattr(ce.qb, "torrents_info", info)
    rep = await A.relocate_anime(aid, old_path="/data/25D/番")
    assert (rep["redownload"], rep["foreign_kept"], rep["special_kept"]) == (1, 1, 0), rep
    assert rep["redownload_dirs"] == ["/data/25D/番"] and rep["old_path"] == "/data/25D/番", rep
    with clean_tables.get_session() as s:
        got = {t.info_hash[0]: t.status for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid))}
    assert got == {"a": "pending", "b": "sent"}, got


def test_relocate_notice_for_foreign_kept_has_no_cleanup_wording(monkeypatch):
    import pages.anime_detail as D
    got = []
    monkeypatch.setattr(D.ui, "notify", lambda msg, **k: got.append((msg, k)))
    base = {"new_path": "/n", "old_path": "/o", "moved": 0, "redownload": 0, "redownload_dirs": [],
            "stalled_dirs": [], "untracked": 0, "in_place": 0, "failed": 0, "stalled_kept": 0,
            "special_kept": 0, "foreign_kept": 2, "delivering": 0}
    D.notify_relocate_anime(base)
    msg, kw = got[-1]
    assert kw.get("type") == "warning" and "2" in msg and "不属于本季" in msg, msg
    for w in ("清理", "再删", "手动处理", "旧文件", "/o"):
        assert w not in msg, (w, msg)
    D.notify_relocate_anime(dict(base, redownload=1, untracked=1, redownload_dirs=["/o"]))
    msg, _ = got[-1]
    assert "不属于本季" in msg and "别当成这几集的旧文件删" in msg, msg


# ==================== 详情页 ====================

@pytest.mark.nicegui_main_file("tests/render_detail_main.py")
async def test_detail_page_shows_badge_undo_and_the_bulk_button(user, clean_tables, cfg):
    """#6 本季 9..12 从没进过库：横幅上有『把占槽的 12 条旧种子标为不属于本季』；标完之后每条别季行有灰色徽标和『撤销』，
    横幅里的这个按钮消失。没标的别季行（季号不同）有『不属于本季』按钮。"""
    from nicegui import ui
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    aid, _ = _six(clean_tables, tail=None)
    await user.open(f"/detail/{aid}")
    await user.should_see("把占槽的 12 条旧种子标为不属于本季")
    assert [b for b in user.find(kind=ui.button, content="不属于本季").elements if b.text == "不属于本季"]

    A.mark_slot_impostors_foreign(aid)
    await user.open(f"/detail/{aid}")
    await user.should_not_see("把占槽的 12 条旧种子标为不属于本季")
    badges = [b for b in user.find(ui.badge).elements if getattr(b, "text", "") == "不属于本季"]
    undo = [b for b in user.find(kind=ui.button, content="撤销").elements if b.text == "撤销"]
    assert len(badges) == 12 and len(undo) == 12, (len(badges), len(undo))


@pytest.mark.nicegui_main_file("tests/render_detail_main.py")
async def test_detail_page_offers_the_bulk_button_when_the_tail_is_already_in_the_library(user, clean_tables, cfg):
    """#6 的真实形状：本季 9..12 早就入了库、被同集闸压成 skipped —— 算"见过"、不报缺集，占槽缺集横幅不出；
    批量按钮照样要给（否则 24 条只能逐条点）。标完之后不再出。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    aid, _ = _six(clean_tables, tail="skipped")
    assert A.episode_gaps([_anime(clean_tables, aid)]) == {}, "前提：没有缺集，F8 那条横幅不会出"
    await user.open(f"/detail/{aid}")
    await user.should_see("把占槽的 12 条旧种子标为不属于本季")
    A.mark_slot_impostors_foreign(aid)
    await user.open(f"/detail/{aid}")
    await user.should_not_see("把占槽的 12 条旧种子标为不属于本季")
