"""(r42 E-81) 单集删除三选一：删的是这一集仅剩的一份时，由用户选换一份 / 以后自动补 / 这一集不要了。

扩展 E-73：只有选『这一集不要了』（以及不传 drop 的老入口，如 cleanup）才置 ep_dropped；默认项不置（漏集致命、多下不致命）。
core 一次算好确认框要的事实（delete_options），换一份走 delete_and_replace：先删后下、删完按主键复核选中那条。
qB 与交付一律 mock。
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from db.models import Anime, AnimeTorrent, Movie, MovieTorrent

_OLD = datetime.now() - timedelta(hours=3)
_n = iter(range(1, 10 ** 6))


def _anime(s, **kw):
    base = dict(title="某番", display_name="某番", season=1, quarter="26C", confirmed=True, rejected=False,
                total_episodes=12, bangumi_id=1)
    base.update(kw)
    a = Anime(**base)
    s.add(a); s.commit(); s.refresh(a)
    return a.id


def _row(s, aid, ep, status, source="ANi", raw=None, **kw):
    n = next(_n)
    base = dict(anime_id=aid, info_hash=f"{n:040x}", source=source, site="nyaa",
                raw_title=raw or f"[{source}] 某番 - {int(ep):02d} [1080p] #{n}", season=1, episode=float(ep),
                status=status, download_url=f"https://x/{n}.torrent", created_at=_OLD, release_time=_OLD)
    base.update(kw)
    t = AnimeTorrent(**base)
    s.add(t); s.commit(); s.refresh(t)
    return t.id


def _get(clean_tables, tid):
    with clean_tables.get_session() as s:
        t = s.get(AnimeTorrent, tid)
        s.expunge(t)
        return t


def _set(clean_tables, tid, **kw):
    with clean_tables.get_session() as s:
        t = s.get(AnimeTorrent, tid)
        for k, v in kw.items():
            setattr(t, k, v)
        s.add(t); s.commit()


def _qb(monkeypatch, *, down=False, missing=(), on_delete=None):
    from core import engine
    calls = []

    async def info(hs):
        if down:
            return None
        return {h: {"state": "stoppedUP"} for h in hs if h not in missing}

    async def delete(hs, delete_files=True):
        calls.append(list(hs))
        if on_delete:
            on_delete()
        return True
    monkeypatch.setattr(engine.qb, "torrents_info", info)
    monkeypatch.setattr(engine.qb, "delete", delete)
    return calls


def _fake_dl(monkeypatch, result=True):
    from core import anime as A
    sent = []

    async def dl(tid, force=False, prefs_gate=None):
        sent.append((tid, force, prefs_gate))
        return result

    async def alive():
        return True
    monkeypatch.setattr(A, "download_anime_torrent", dl)
    monkeypatch.setattr(A, "qb_precheck", alive)
    return sent


# ================================================================ 1–2 drop 参数

def test_default_still_drops_the_episode(clean_tables, monkeypatch):
    """不传 drop（老入口、cleanup）：删最后一份照旧置 ep_dropped（E-73 不变）。"""
    from core import anime as A
    _qb(monkeypatch)
    with clean_tables.get_session() as s:
        aid = _anime(s)
        t = _row(s, aid, 1, "sent")
    assert asyncio.run(A.delete_anime_torrent(t)) is True
    assert _get(clean_tables, t).ep_dropped is True


@pytest.mark.parametrize("branch", ["archived", "movie_shared", "gone", "normal"])
def test_drop_false_reaches_every_branch(clean_tables, monkeypatch, branch):
    from core import anime as A
    with clean_tables.get_session() as s:
        aid = _anime(s)
        t = _row(s, aid, 1, "sent", archived_at=_OLD if branch == "archived" else None)
        h = s.get(AnimeTorrent, t).info_hash
        if branch == "movie_shared":
            m = Movie(title="片", display_name="片", quarter="26C", bangumi_id=9)
            s.add(m); s.commit(); s.refresh(m)
            s.add(MovieTorrent(movie_id=m.id, info_hash=h, raw_title="片", status="sent")); s.commit()
    calls = _qb(monkeypatch, missing={h} if branch == "gone" else ())
    r = asyncio.run(A.delete_anime_torrent(t, drop=False))
    assert r == ("gone" if branch == "gone" else True)
    row = _get(clean_tables, t)
    assert (row.status, row.ep_dropped) == ("deleted", False)
    assert bool(calls) == (branch == "normal")


# ================================================================ 3 放回同组 skipped

def test_drop_false_requeues_skipped_siblings_of_the_same_group_only(clean_tables, monkeypatch):
    from core import anime as A
    _qb(monkeypatch)
    with clean_tables.get_session() as s:
        aid = _anime(s)
        t = _row(s, aid, 1, "sent", "ANi")
        sib = _row(s, aid, 1, "skipped", "LoliHouse")
        foreign = _row(s, aid, 1, "skipped", "Old", foreign_season=True)
        other_ep = _row(s, aid, 2, "skipped", "LoliHouse")
    asyncio.run(A.delete_anime_torrent(t, drop=False))
    assert _get(clean_tables, sib).status == "pending"
    assert _get(clean_tables, foreign).status == "skipped", "别季的不放"
    assert _get(clean_tables, other_ep).status == "skipped", "别的集不受影响"


def test_drop_true_does_not_requeue(clean_tables, monkeypatch):
    from core import anime as A
    _qb(monkeypatch)
    with clean_tables.get_session() as s:
        aid = _anime(s)
        t = _row(s, aid, 1, "sent", "ANi")
        sib = _row(s, aid, 1, "skipped", "LoliHouse")
    asyncio.run(A.delete_anime_torrent(t, drop=True))
    assert _get(clean_tables, sib).status == "skipped"


def test_no_requeue_when_the_episode_still_has_a_copy(clean_tables, monkeypatch):
    from core import anime as A
    _qb(monkeypatch)
    with clean_tables.get_session() as s:
        aid = _anime(s)
        t = _row(s, aid, 1, "sent", "ANi")
        _row(s, aid, 1, "sent", "Mikan")
        sib = _row(s, aid, 1, "skipped", "LoliHouse")
    asyncio.run(A.delete_anime_torrent(t, drop=False))
    assert _get(clean_tables, sib).status == "skipped", "同集还有一份在盘上"


def test_ambiguous_range_other_source_same_number_is_not_requeued(clean_tables, monkeypatch, cfg):
    """歧义段（O=12<T=24）：NEST 的『13』与 ANi 的『13』不是同一集，不放回。"""
    from core import anime as A
    cfg(ANIME_FINISH_UNSUB=False)
    _qb(monkeypatch)
    with clean_tables.get_session() as s:
        aid = _anime(s, ep_offset=12, total_episodes=24)
        t = _row(s, aid, 13, "sent", "ANi")
        same = _row(s, aid, 13, "skipped", "ANi", raw="[ANi] 某番 - 13 v2")
        other = _row(s, aid, 13, "skipped", "NEST")
    asyncio.run(A.delete_anime_torrent(t, drop=False))
    assert _get(clean_tables, same).status == "pending"
    assert _get(clean_tables, other).status == "skipped"


# ================================================================ 4 不置 dropped 之后 flush 的真实行为

def test_after_keep_flush_picks_a_sibling_unless_locked(clean_tables, monkeypatch, cfg):
    from core import anime as A
    cfg(ANIME_FINISH_UNSUB=False, ANIME_DOWNLOAD_GRACE_MIN=0)
    _qb(monkeypatch)
    with clean_tables.get_session() as s:
        free = _anime(s, bangumi_id=1)
        locked = _anime(s, bangumi_id=2, pref_source="ANi")
        t1 = _row(s, free, 1, "sent", "ANi")
        p1 = _row(s, free, 1, "pending", "Mikan")
        t2 = _row(s, locked, 1, "sent", "ANi")
        p2 = _row(s, locked, 1, "pending", "Mikan")
    asyncio.run(A.delete_anime_torrent(t1, drop=False))
    asyncio.run(A.delete_anime_torrent(t2, drop=False))
    sent = _fake_dl(monkeypatch)
    asyncio.run(A.flush_ready_downloads(qb_alive=True))
    got = {tid for tid, *_ in sent}
    assert p1 in got, "没锁源：flush 下一轮补上"
    assert p2 not in got, "锁了源：别组的不自动下"


# ================================================================ 6–8 delete_options

def _opts_shape(clean_tables, **akw):
    with clean_tables.get_session() as s:
        aid = _anime(s, **akw)
        ids = dict(
            t=_row(s, aid, 1, "sent", "ANi"),
            err=_row(s, aid, 1, "error", "Mikan", retry_count=3),
            ani2=_row(s, aid, 1, "pending", "ANi", raw="[ANi] 某番 - 01 v2 [CHT]"),
            nest=_row(s, aid, 1, "pending", "NEST", raw="[NEST] 某番 - 01 [CHS]"),
            skip=_row(s, aid, 1, "skipped", "LoliHouse", raw="[LoliHouse] 某番 - 01 [CHT]"),
            excl=_row(s, aid, 1, "excluded", "X"),
            dele=_row(s, aid, 1, "deleted", "Y"),
            foreign=_row(s, aid, 1, "pending", "Old", foreign_season=True),
            ep2=_row(s, aid, 2, "pending", "NEST"),
        )
    return aid, ids


def test_candidates_exclude_the_wrong_rows_and_keep_other_groups(clean_tables, cfg):
    from core import anime as A
    cfg(ANIME_FINISH_UNSUB=False)
    aid, ids = _opts_shape(clean_tables, pref_source="ANi")
    o = A.delete_options(ids["t"])
    assert o["kind"] == "last"
    got = [c["id"] for c in o["cands"]]
    assert set(got) == {ids["err"], ids["ani2"], ids["nest"], ids["skip"]}, got
    assert got[0] == ids["ani2"], "锁定组在前"
    assert got[-1] == ids["err"], "失败过的排最后"
    c0 = o["cands"][0]
    assert c0["same_src"] and c0["lock_src"]
    assert o["auto_next"]["id"] == ids["ani2"]
    assert o["options"] == ["swap", "drop"] and o["default"] == "swap", "后台会自动补时不重复给『以后自动补』"


def test_auto_next_respects_lock_keyword_retry_and_subscription(clean_tables, cfg):
    from core import anime as A
    cfg(ANIME_FINISH_UNSUB=False)
    with clean_tables.get_session() as s:
        aid = _anime(s, pref_source="ANi", pref_keyword="CHT")
        t = _row(s, aid, 1, "sent", "ANi")
        _row(s, aid, 1, "pending", "NEST", raw="[NEST] 某番 - 01 [CHT]")          # 别组：锁源不挑
        late = _row(s, aid, 1, "pending", "ANi", raw="[ANi] 某番 - 01 v2 [CHT]",
                    retry_count=1, retry_at=datetime.now() + timedelta(hours=1))      # 没到点
        _row(s, aid, 1, "pending", "ANi", raw="[ANi] 某番 - 01 [CHS]")              # 关键词不中
    o = A.delete_options(t)
    assert o["auto_next"] is None
    assert "keep" in o["options"] and o["default"] == "swap"
    assert o["cands"][0]["kw_ok"] and not o["cands"][-1]["kw_ok"], "命中关键词的排前面"
    _set(clean_tables, late, retry_at=None)
    assert A.delete_options(t)["auto_next"]["id"] == late
    sk = None
    with clean_tables.get_session() as s:
        sk = _row(s, aid, 1, "skipped", "ANi", raw="[ANi] 某番 - 01 [CHT] 跳过的")
    _set(clean_tables, late, status="error")
    assert A.delete_options(t)["auto_next"]["id"] == sk, "skipped 算进去（删时会被放回待下）"
    with clean_tables.get_session() as s:
        a = s.get(Anime, aid); a.rejected = True; s.add(a); s.commit()
    assert A.delete_options(t)["auto_next"] is None, "没在订阅：后台什么都不会下"


def test_options_and_defaults(clean_tables, cfg):
    from core import anime as A
    cfg(ANIME_FINISH_UNSUB=False)
    with clean_tables.get_session() as s:
        aid = _anime(s)
        lone = _row(s, aid, 1, "sent")
        arch = _row(s, aid, 2, "sent", archived_at=_OLD)
        _row(s, aid, 2, "pending", "Mikan")
        d1 = _row(s, aid, 3, "sent")
        _row(s, aid, 3, "sent", "Mikan")
        sp = _row(s, aid, -1, "sent")
        fs = _row(s, aid, 4, "sent", foreign_season=True)
        dl = _row(s, aid, 5, "downloading")
    o = A.delete_options(lone)
    assert (o["options"], o["default"]) == (["keep", "drop"], "keep"), "没候选：默认以后自动补"
    o = A.delete_options(arch)
    assert "swap" not in o["options"] and o["default"] == "keep", "已归档不给换一份"
    assert A.delete_options(d1)["kind"] == "dup"
    assert A.delete_options(sp)["kind"] == "special"
    assert A.delete_options(fs)["kind"] == "foreign"
    assert A.delete_options(dl) is None
    assert A.delete_options(10 ** 6) is None


def test_alias_defaults_to_drop(clean_tables, cfg):
    """两套集号：Nix-Raws 1–11 与 ANi 13–23 周更对得上；删 Nix 3 默认『只删这一份』（不置还会再下一份重复）。"""
    from core import anime as A
    cfg(ANIME_FINISH_UNSUB=False)
    w0 = datetime.now() - timedelta(weeks=30)
    with clean_tables.get_session() as s:
        aid = _anime(s)
        for i, e in enumerate(range(13, 24)):
            _row(s, aid, e, "sent", "ANi", release_time=w0 + timedelta(weeks=i))
        nix = {e: _row(s, aid, e, "sent", "Nix-Raws", release_time=w0 + timedelta(weeks=i, hours=8))
               for i, e in enumerate(range(1, 12))}
        _row(s, aid, 3, "pending", "CATCHPLAY")
    o = A.delete_options(nix[3])
    assert o["alias"] and o["alias"]["keep_src"] == "ANi"
    assert o["default"] == "drop" and "swap" in o["options"]


# ================================================================ 9 delete_and_replace

def _swap_shape(clean_tables, **akw):
    with clean_tables.get_session() as s:
        aid = _anime(s, **akw)
        t = _row(s, aid, 1, "sent", "ANi")
        p = _row(s, aid, 1, "pending", "Mikan")
        sib = _row(s, aid, 1, "skipped", "NEST")
    return aid, t, p, sib


def test_replace_deletes_then_force_downloads_even_across_a_lock(clean_tables, monkeypatch, cfg):
    from core import anime as A
    cfg(ANIME_FINISH_UNSUB=False)
    order = []
    _qb(monkeypatch, on_delete=lambda: order.append("del"))
    sent = _fake_dl(monkeypatch)
    _, t, p, _ = _swap_shape(clean_tables, pref_source="ANi")
    orig = A.download_anime_torrent

    async def dl(tid, force=False, prefs_gate=None):
        order.append("dl")
        return await orig(tid, force=force, prefs_gate=prefs_gate)
    monkeypatch.setattr(A, "download_anime_torrent", dl)
    r = asyncio.run(A.delete_and_replace(t, p))
    assert r == {"deleted": True, "picked": True, "reason": ""}
    assert order == ["del", "dl"], "先删后下"
    assert sent == [(p, True, None)], "force=True、不做放行前复核（锁定组以外也能下）"
    assert _get(clean_tables, t).ep_dropped is False


def test_replace_does_nothing_when_qb_is_down(clean_tables, monkeypatch, cfg):
    from core import anime as A
    cfg(ANIME_FINISH_UNSUB=False)
    _qb(monkeypatch, down=True)
    sent = _fake_dl(monkeypatch)
    _, t, p, _ = _swap_shape(clean_tables)
    assert asyncio.run(A.delete_and_replace(t, p))["picked"] == "skip"
    assert sent == [] and _get(clean_tables, t).status == "sent"


def test_replace_goes_stale_when_the_episode_got_covered_meanwhile(clean_tables, monkeypatch, cfg):
    from core import anime as A
    cfg(ANIME_FINISH_UNSUB=False)
    _, t, p, sib = _swap_shape(clean_tables)
    _qb(monkeypatch, on_delete=lambda: _set(clean_tables, sib, status="sent"))   # flush 在两次 await 之间补上了
    sent = _fake_dl(monkeypatch)
    assert asyncio.run(A.delete_and_replace(t, p))["picked"] == "stale"
    assert sent == []


@pytest.mark.parametrize("bad", ["other_anime", "other_ep", "foreign", "downloading", "self"])
def test_replace_rejects_forged_picks(clean_tables, monkeypatch, cfg, bad):
    from core import anime as A
    cfg(ANIME_FINISH_UNSUB=False)
    _qb(monkeypatch)
    sent = _fake_dl(monkeypatch)
    with clean_tables.get_session() as s:
        aid = _anime(s)
        t = _row(s, aid, 1, "sent", "ANi")
        if bad == "other_anime":
            pick = _row(s, _anime(s, bangumi_id=77), 1, "pending", "Mikan")
        elif bad == "other_ep":
            pick = _row(s, aid, 2, "pending", "Mikan")
        elif bad == "foreign":
            pick = _row(s, aid, 1, "pending", "Mikan", foreign_season=True)
        elif bad == "downloading":
            pick = _row(s, aid, 1, "downloading", "Mikan")
        else:
            pick = t
    assert asyncio.run(A.delete_and_replace(t, pick))["picked"] == "stale"
    assert sent == []


def test_replace_is_registered_in_flight_while_deleting(clean_tables, monkeypatch, cfg):
    from core import anime as A, engine
    cfg(ANIME_FINISH_UNSUB=False)
    seen = []
    _qb(monkeypatch, on_delete=lambda: seen.extend(engine.in_flight_labels()))
    _fake_dl(monkeypatch)
    _, t, p, _ = _swap_shape(clean_tables)
    asyncio.run(A.delete_and_replace(t, p))
    assert "删除并换一份" in seen, seen


def test_replace_download_fails_episode_stays_recoverable(clean_tables, monkeypatch, cfg):
    from core import anime as A
    cfg(ANIME_FINISH_UNSUB=False, ANIME_DOWNLOAD_GRACE_MIN=0)
    _qb(monkeypatch)
    _fake_dl(monkeypatch, result=False)
    aid, t, p, sib = _swap_shape(clean_tables)
    _set(clean_tables, p, fail_reason="种子取不到")
    r = asyncio.run(A.delete_and_replace(t, p))
    assert r["picked"] is False and r["reason"] == "种子取不到"
    assert _get(clean_tables, t).ep_dropped is False
    assert _get(clean_tables, sib).status == "pending", "同组 skipped 已放回待下"
    assert A.download_plan(aid) & {p, sib}, "flush 能接上"


# ================================================================ r42 复查：C1 / C5 / UI-1

def test_c1_old_season_impostors_stay_out_of_requeue_and_candidates(clean_tables, monkeypatch, cfg):
    """第三季第 9 集的槽位被第一季旧集（ANi，2023）占着；另一组的第一季旧集躺在 skipped。
    删掉占槽那份选『以后自动补』：旧季 skipped 不放回、不当候选，flush 下的是本季那条。"""
    from core import anime as A
    cfg(ANIME_FINISH_UNSUB=False, ANIME_DOWNLOAD_GRACE_MIN=0)
    _qb(monkeypatch)
    old, new = datetime(2023, 11, 1), datetime(2026, 8, 30)
    with clean_tables.get_session() as s:
        aid = _anime(s, air_date="2026-07-05", season=3)
        a_old = _row(s, aid, 9, "sent", "ANi", release_time=old, created_at=new)
        b_old = _row(s, aid, 9, "skipped", "Nekomoe", release_time=old, created_at=new)
        c_new = _row(s, aid, 9, "skipped", "ANi", release_time=new, created_at=new)
    o = A.delete_options(a_old)
    assert [c["id"] for c in o["cands"]] == [c_new]
    assert o["auto_next"]["id"] == c_new
    asyncio.run(A.delete_anime_torrent(a_old, drop=False))
    assert _get(clean_tables, b_old).status == "skipped", "前一季的旧集不放回待下"
    assert _get(clean_tables, c_new).status == "pending"
    sent = _fake_dl(monkeypatch)
    asyncio.run(A.flush_ready_downloads(qb_alive=True))
    assert [tid for tid, *_ in sent] == [c_new]


def test_c5_finished_anime_defaults_to_drop(clean_tables, cfg):
    """判过下齐的番：默认『这一集不要了』（不置的话下一轮会撤销下齐）；换一份 / 以后自动补仍可选。"""
    from core import anime as A
    cfg(ANIME_FINISH_UNSUB=False)
    with clean_tables.get_session() as s:
        aid = _anime(s, finished_at=_OLD)
        t = _row(s, aid, 1, "sent")
        _row(s, aid, 1, "pending", "Mikan", retry_count=1, retry_at=datetime.now() + timedelta(hours=1))
        lone = _row(s, aid, 2, "sent")
    o = A.delete_options(t)
    assert o["finished"] and o["default"] == "drop" and o["options"] == ["swap", "keep", "drop"]
    o = A.delete_options(lone)
    assert o["finished"] and o["default"] == "drop" and "keep" in o["options"]
    with clean_tables.get_session() as s:
        a = s.get(Anime, aid); a.finished_at = None; s.add(a); s.commit()
    o = A.delete_options(t)
    assert not o["finished"] and o["default"] == "swap"


def test_ui1_lock_blocked_follows_lock_blocks(clean_tables, cfg):
    """锁定组没发过的集（在它最大集号之后）→ lock_blocked；锁定组发过的 → 不是。"""
    from core import anime as A
    cfg(ANIME_FINISH_UNSUB=False)
    with clean_tables.get_session() as s:
        aid = _anime(s, pref_source="ANi")
        for e in (1, 2, 3):
            _row(s, aid, e, "sent" if e == 3 else "deleted", "ANi")
        own = _row(s, aid, 3, "sent", "Mikan")
        _row(s, aid, 3, "excluded", "X")
        late = _row(s, aid, 5, "sent", "Mikan")
        _row(s, aid, 9, "sent", "ANi", foreign_season=True)   # 别季行不抬高锁定组最大集号
    assert A.delete_options(late)["lock_blocked"] is True
    with clean_tables.get_session() as s:
        t3 = s.exec(__import__("sqlmodel").select(AnimeTorrent).where(
            AnimeTorrent.anime_id == aid, AnimeTorrent.episode == 3, AnimeTorrent.source == "ANi")).one()
        t3.status = "deleted"; s.add(t3); s.commit()
    assert A.delete_options(own)["lock_blocked"] is False
