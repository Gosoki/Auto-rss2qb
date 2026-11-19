"""(r41) 提示里清理下错的文件：两套集号的重复份 / 已标『不属于本季』的别季文件。

用户的取舍：漏集致命、多一份不致命 —— 判据宁严勿宽，拿不准的不列、不勾或锁住。qB 一律 mock。
真阳性形状照生产 #27 / #6 / #8 造；守卫用例是会把"这一集盘上一份不剩"的形状（keeper 不在 / 没下完 / 共种子 / 预览后变了…）。
"""
import asyncio
from datetime import datetime, timedelta

import pytest

import config
import db

db.init_db()
config.load_from_db()
db.apply_configured_backend()

W0 = datetime.now() - timedelta(weeks=30)
GB = 1 << 30
_n = iter(range(1, 10 ** 6))


def _anime(s, **kw):
    from db.models import Anime
    base = dict(title="殿下", display_name="擅长逃跑的殿下 第二季", season=2, quarter="26C",
                confirmed=True, rejected=False, bangumi_id=900, total_episodes=12)
    base.update(kw)
    a = Anime(**base)
    s.add(a); s.commit(); s.refresh(a)
    return a.id


def _row(s, aid, src, ep, rt, status="sent", **kw):
    from db.models import AnimeTorrent
    n = next(_n)
    base = dict(anime_id=aid, info_hash=f"{n:040x}", source=src, site="nyaa", season=2, episode=float(ep),
                raw_title=f"[{src}] 殿下 - {ep} #{n}", status=status, download_url=f"https://x/{n}.torrent",
                created_at=rt, release_time=rt, save_path="/dl/26C/殿下/Season 2")
    base.update(kw)
    t = AnimeTorrent(**base)
    s.add(t); s.commit(); s.refresh(t)
    return t.id


def _weekly(s, aid, src, eps, start, lag=timedelta(0), **kw):
    return {ep: _row(s, aid, src, ep, start + timedelta(weeks=i) + lag, **kw) for i, ep in enumerate(eps)}


def _p27(s, lock=None, **kw):
    """#27 的形状：T=12，ANi 13–23 周更、Nix-Raws 1–11 晚 8 小时，两边都已下完；Nix 3–10 各有一条 CATCHPLAY 待下。"""
    aid = _anime(s, pref_source=lock, **kw)
    ani = _weekly(s, aid, "ANi", range(13, 24), W0)
    nix = _weekly(s, aid, "Nix-Raws", range(1, 12), W0, lag=timedelta(hours=8))
    cp = _weekly(s, aid, "CATCHPLAY", range(3, 11), W0 + timedelta(weeks=2), lag=timedelta(hours=12), status="pending")
    return aid, ani, nix, cp


def _get(clean_tables, tid):
    from db.models import AnimeTorrent
    with clean_tables.get_session() as s:
        t = s.get(AnimeTorrent, tid)
        s.expunge(t)
        return t


def _set(clean_tables, tid, **kw):
    from db.models import AnimeTorrent
    with clean_tables.get_session() as s:
        t = s.get(AnimeTorrent, tid)
        for k, v in kw.items():
            setattr(t, k, v)
        s.add(t); s.commit()


class FakeQB:
    """qB 替身：默认每个 hash 都在、下完、各自一个文件。over[hash] 覆盖单条；down=True 时 torrents_info 返回 None。"""

    def __init__(self):
        self.over, self.missing, self.down = {}, set(), False
        self.info_calls, self.deleted = [], []

    async def torrents_info(self, hs):
        self.info_calls.append(list(hs))
        if self.down:
            return None
        out = {}
        for h in hs:
            if h in self.missing:
                continue
            out[h] = {"hash": h, "progress": 1, "state": "stoppedUP", "size": GB,
                      "content_path": f"/dl/26C/殿下/Season 2/{h}.mkv", **self.over.get(h, {})}
        return out

    async def delete(self, hs, delete_files=True):
        self.deleted += list(hs)
        return True


@pytest.fixture
def qb(monkeypatch):
    from core import engine
    f = FakeQB()
    monkeypatch.setattr(engine.qb, "torrents_info", f.torrents_info)
    monkeypatch.setattr(engine.qb, "delete", f.delete)
    return f


def _run(c):
    return asyncio.run(c)


def _picks(items):
    return [(i["drop_id"], i["drop_hash"], i["keep_id"], i["keep_hash"]) for i in items]


# ================================================================ 真阳性

def test_p1_returner_shape_cleans_all_and_drops_the_aliases(clean_tables, qb):
    """#27：留 ANi → Nix 1–11 共 11 份全列、全勾；删完 ep_dropped=1（E-73 沿用，D1），ANi 不动；
    CATCHPLAY 的季内号待下锁着不挑，改回『按优先级』后也不挑（写 0 的话它会被下回来）。"""
    from core import anime as A
    with clean_tables.get_session() as s:
        aid, ani, nix, cp = _p27(s, lock="ANi")
    pv = A.cleanup_preview(aid, "two")
    assert pv["offset"] == 12 and not pv["warn"], pv
    assert [i["drop_id"] for i in pv["items"]] == [nix[e] for e in range(1, 12)]
    assert all(i["checked"] for i in pv["items"]) and {i["keep_id"] for i in pv["items"]} == set(ani.values())
    assert pv["items"][0]["keep_ep"] == 13 and pv["items"][0]["keep_src"] == "ANi"
    pr = _run(A.cleanup_probe(pv["items"]))
    assert pr["qb"] and not any(i["locked"] for i in pr["items"]) and all(i["size"] == GB for i in pr["items"])
    res = _run(A.cleanup_execute(aid, "two", _picks(pv["items"])))
    assert sorted(res["deleted"]) == sorted(nix.values()) and res["bytes"] == 11 * GB, res
    assert not (res["skipped"] or res["failed"] or res["gone"])
    for tid in nix.values():
        t = _get(clean_tables, tid)
        assert t.status == "deleted" and t.ep_dropped, (tid, t.status, t.ep_dropped)
    assert {_get(clean_tables, t).status for t in ani.values()} == {"sent"}
    assert not (A.download_plan(aid) & set(cp.values()))
    A.set_pref_source(aid, "")
    assert not (A.download_plan(aid) & set(cp.values())), "改回按优先级后，CATCHPLAY 的季内号被自动下回来了"


def test_p1_keep_other_group_lists_abs_side(clean_tables):
    """反过来留 Nix-Raws：删的是 ANi 13–23，右边写 Nix 的第 1–11 集。"""
    from core import anime as A
    with clean_tables.get_session() as s:
        aid, ani, nix, _ = _p27(s)
    pv = A.cleanup_preview(aid, "two", keep_src="Nix-Raws")
    assert [(i["drop_ep"], i["keep_ep"]) for i in pv["items"]] == [(e, e - 12) for e in range(13, 24)]


def test_p2_foreign_rows_listed_unchecked_and_deleted_without_dropping(clean_tables, qb):
    """#6：24 条已标『不属于本季』的第一季正片 → 全列、全不勾；全传进去才删，ep_dropped=0；本季 25–36 不动。"""
    from core import anime as A
    with clean_tables.get_session() as s:
        aid = _anime(s, total_episodes=12)
        old = _weekly(s, aid, "ANi", range(1, 25), W0 - timedelta(weeks=100), foreign_season=True)
        cur = _weekly(s, aid, "ANi", range(25, 37), W0)
    pv = A.cleanup_preview(aid, "foreign")
    assert len(pv["items"]) == 24 and not any(i["checked"] for i in pv["items"])
    assert all(i["keep_id"] is None for i in pv["items"])
    res = _run(A.cleanup_execute(aid, "foreign", _picks(pv["items"])))
    assert sorted(res["deleted"]) == sorted(old.values()), res
    for tid in old.values():
        t = _get(clean_tables, tid)
        assert t.status == "deleted" and not t.ep_dropped
    assert {_get(clean_tables, t).status for t in cur.values()} == {"sent"}


def _p8(s, lock):
    """#8：锁某组；它第 6–8 集是上一季的旧集（已下），绿茶字幕组新发的 6–8 在待下。"""
    aid = _anime(s, total_episodes=13, pref_source=lock)
    old = {e: _row(s, aid, "喵萌奶茶屋", e, W0 + timedelta(days=e)) for e in (6, 7, 8)}
    _weekly(s, aid, "喵萌奶茶屋", range(9, 17), W0 + timedelta(weeks=10))
    new = {e: [_row(s, aid, "绿茶字幕组", e, W0 + timedelta(weeks=20), status="pending"),
               _row(s, aid, "绿茶字幕组", e, W0 + timedelta(weeks=20, minutes=1), status="pending")]
           for e in (6, 7, 8)}
    return aid, old, new


def test_p3_locked_marking_offers_candidates_for_freed_eps(clean_tables):
    from core import anime as A
    with clean_tables.get_session() as s:
        aid, old, new = _p8(s, "喵萌奶茶屋")
    c = A.locked_freed_candidates(aid, list(old.values()))
    assert sorted(c) == [6, 7, 8], c
    for e in (6, 7, 8):
        assert {x["id"] for x in c[e]} == set(new[e]) and c[e][0]["source"] == "绿茶字幕组"
    # 锁定组自己也有待下的 → 那一集会自己下，不问
    with clean_tables.get_session() as s:
        _row(s, aid, "喵萌奶茶屋", 7, W0 + timedelta(weeks=21), status="pending")
    assert sorted(A.locked_freed_candidates(aid, list(old.values()))) == [6, 8]


def test_p3_unlocked_needs_no_candidates(clean_tables):
    from core import anime as A
    with clean_tables.get_session() as s:
        aid, old, _ = _p8(s, None)
    assert A.locked_freed_candidates(aid, list(old.values())) == {}


# ================================================================ 守卫：误删形状

def test_g1_keeper_group_only_eps_go_to_skip(clean_tables):
    """#104 ep14/15：ANi 有、Nix 没有对应的季内集 → 只在『不删的』里，原因是只有这一份。"""
    from core import anime as A
    with clean_tables.get_session() as s:
        aid = _anime(s, pref_source="ANi")
        ani = _weekly(s, aid, "ANi", range(13, 19), W0)
        _weekly(s, aid, "Nix-Raws", (1,), W0, lag=timedelta(hours=8))
        _weekly(s, aid, "Nix-Raws", (4, 5, 6), W0 + timedelta(weeks=3), lag=timedelta(hours=8))
    pv = A.cleanup_preview(aid, "two")
    assert {i["keep_ep"] for i in pv["items"]} == {13, 16, 17, 18}
    sk = {x["row_id"]: x["reason"] for x in pv["skipped"]}
    assert sk == {ani[14]: "只有这一份", ani[15]: "只有这一份"}, sk
    assert not ({ani[14], ani[15]} & {i["drop_id"] for i in pv["items"]})


def test_g2_drop_group_only_ep_is_never_listed(clean_tables):
    """Nix 7 已下、ANi 19 只有待下 → Nix 7 进『不删的』（删了一份不剩），不进 items。"""
    from core import anime as A
    with clean_tables.get_session() as s:
        aid, ani, nix, _ = _p27(s, lock="ANi")
        _set(clean_tables, ani[19], status="pending")
    pv = A.cleanup_preview(aid, "two")
    assert nix[7] not in {i["drop_id"] for i in pv["items"]}
    assert {"row_id": nix[7], "ep": 7, "src": "Nix-Raws",
            "reason": "另一套编号里没有这一集，删了就一份不剩"} in pv["skipped"]


def test_g2b_drop_side_outside_its_numbering_is_not_a_candidate(clean_tables):
    """季内组里有一条大于总集数的号（Nix 13，没有发布时间，所以不参与配对判据、两组照样算得出 d=12）：
    它不在季内号段里，不能拿它去配 ANi 25 删掉（那多半是另一套编号的第 13 集，ANi 25 是别的内容）。"""
    from core import anime as A
    with clean_tables.get_session() as s:
        aid, ani, nix, _ = _p27(s, lock="ANi")
        n13 = _row(s, aid, "Nix-Raws", 13, W0 + timedelta(weeks=12), release_time=None)
        _row(s, aid, "ANi", 25, W0 + timedelta(weeks=12))
    pv = A.cleanup_preview(aid, "two")
    assert pv["offset"] == 12 and len(pv["items"]) == 11, pv["offset"]
    assert n13 not in {i["drop_id"] for i in pv["items"]}
    assert all(i["drop_ep"] <= 12 for i in pv["items"])


def test_g3_out_of_window_unchecked_and_not_deleted_unless_passed(clean_tables, qb):
    from core import anime as A
    with clean_tables.get_session() as s:
        aid, ani, nix, _ = _p27(s, lock="ANi")
    _set(clean_tables, nix[5], release_time=W0 + timedelta(weeks=4, days=13))
    pv = A.cleanup_preview(aid, "two")
    it = next(i for i in pv["items"] if i["drop_id"] == nix[5])
    assert not it["checked"] and "相差 13 天" in it["note"], it
    picks = _picks(i for i in pv["items"] if i["checked"])
    _run(A.cleanup_execute(aid, "two", picks))
    assert _get(clean_tables, nix[5]).status == "sent"


@pytest.mark.parametrize("kw", [dict(status="stalled"), dict(status="downloading"), dict(status="error"),
                                dict(archived_at=datetime.now()), dict(foreign_season=True)],
                         ids=["停滞", "交付中", "失败", "已归档", "别季"])
def test_g4_keeper_must_be_a_finished_live_row(clean_tables, kw):
    from core import anime as A
    with clean_tables.get_session() as s:
        aid, ani, nix, _ = _p27(s, lock="ANi")
    _set(clean_tables, ani[17], **kw)
    pv = A.cleanup_preview(aid, "two")
    assert nix[5] not in {i["drop_id"] for i in pv["items"]}, kw
    assert any(x["row_id"] == nix[5] and "一份不剩" in x["reason"] for x in pv["skipped"])


def test_g5_drop_in_delivery_is_not_listed(clean_tables):
    from core import anime as A
    with clean_tables.get_session() as s:
        aid, ani, nix, _ = _p27(s, lock="ANi")
    _set(clean_tables, nix[3], status="downloading")
    pv = A.cleanup_preview(aid, "two")
    assert nix[3] not in {i["drop_id"] for i in pv["items"]}
    assert nix[3] not in {x["row_id"] for x in pv["skipped"]}


def test_g6_keeper_broke_after_preview_nothing_written(clean_tables, qb):
    from core import anime as A
    with clean_tables.get_session() as s:
        aid, ani, nix, _ = _p27(s, lock="ANi")
    pv = A.cleanup_preview(aid, "two")
    _set(clean_tables, ani[15], status="error")
    res = _run(A.cleanup_execute(aid, "two", _picks(i for i in pv["items"] if i["drop_id"] == nix[3])))
    assert res["skipped"] == [(nix[3], "预览后状态变了")] and not res["deleted"], res
    assert _get(clean_tables, nix[3]).status == "sent" and not qb.deleted and not qb.info_calls


def test_g7_qb_down_writes_nothing(clean_tables, qb):
    from core import anime as A
    with clean_tables.get_session() as s:
        aid, ani, nix, _ = _p27(s, lock="ANi")
    pv = A.cleanup_preview(aid, "two")
    qb.down = True
    assert _run(A.cleanup_probe(pv["items"]))["qb"] is False
    res = _run(A.cleanup_execute(aid, "two", _picks(pv["items"])))
    assert len(res["failed"]) == 11 and {r for _, r in res["failed"]} == {"qB 连不上"} and not res["deleted"]
    assert {_get(clean_tables, t).status for t in nix.values()} == {"sent"} and not qb.deleted


@pytest.mark.parametrize("case", ["progress", "missingFiles", "absent"])
def test_g8_keeper_not_verified_in_qb_is_skipped(clean_tables, qb, case):
    from core import anime as A
    with clean_tables.get_session() as s:
        aid, ani, nix, _ = _p27(s, lock="ANi")
    keep_hash = _get(clean_tables, ani[16]).info_hash
    if case == "progress":
        qb.over[keep_hash] = {"progress": 0.9}
    elif case == "missingFiles":
        qb.over[keep_hash] = {"state": "missingFiles"}
    else:
        qb.missing.add(keep_hash)
    pv = A.cleanup_preview(aid, "two")
    pr = _run(A.cleanup_probe(pv["items"]))
    it = next(i for i in pr["items"] if i["drop_id"] == nix[4])
    assert it["locked"] and not it["checked"] and "没法确认文件还在" in it["note"]
    res = _run(A.cleanup_execute(aid, "two", _picks(i for i in pv["items"] if i["drop_id"] == nix[4])))
    assert res["skipped"] == [(nix[4], "留下那份在 qB 里查不到或没下完")], res
    assert _get(clean_tables, nix[4]).status == "sent" and not qb.deleted


def test_g8_drop_absent_in_qb_says_drop_side(clean_tables, qb):
    """要删的那份早已移出 qB（生产约 7 成已交付行如此）：照样锁住不删，但原因写要删的那份删不到，不能怪到留下那份头上。"""
    from core import anime as A
    with clean_tables.get_session() as s:
        aid, ani, nix, _ = _p27(s, lock="ANi")
    qb.missing.add(_get(clean_tables, nix[4]).info_hash)
    pv = A.cleanup_preview(aid, "two")
    it = next(i for i in _run(A.cleanup_probe(pv["items"]))["items"] if i["drop_id"] == nix[4])
    assert it["locked"] and not it["checked"]
    assert it["note"] == "要删的这份 qB 里已经没有了，这里删不到文件，这一份先不删", it["note"]
    res = _run(A.cleanup_execute(aid, "two", _picks(i for i in pv["items"] if i["drop_id"] == nix[4])))
    assert res["skipped"] == [(nix[4], "要删的这份 qB 里已经没有了，删不到文件")], res
    assert _get(clean_tables, nix[4]).status == "sent" and not qb.deleted


def test_g9_same_file_on_disk_is_skipped(clean_tables, qb):
    from core import anime as A
    with clean_tables.get_session() as s:
        aid, ani, nix, _ = _p27(s, lock="ANi")
    kh, dh = _get(clean_tables, ani[18]).info_hash, _get(clean_tables, nix[6]).info_hash
    qb.over[kh] = {"content_path": "/dl/x/同一个.mkv"}
    qb.over[dh] = {"content_path": "/dl/x/同一个.mkv"}
    pv = A.cleanup_preview(aid, "two")
    res = _run(A.cleanup_execute(aid, "two", _picks(i for i in pv["items"] if i["drop_id"] == nix[6])))
    assert res["skipped"] and not res["deleted"] and _get(clean_tables, nix[6]).status == "sent"


def test_g10_hash_shared_with_pending_movie_is_never_touched(clean_tables, qb):
    """#101 2414↔598：剧场版那边同一个种子还在待下 → 不删、不标；传进去也挡住，qB 零调用。"""
    from core import anime as A
    from db.models import Movie, MovieTorrent
    with clean_tables.get_session() as s:
        aid = _anime(s, total_episodes=12)
        tid = _row(s, aid, "ANi", 4, W0, foreign_season=True)
        h = _get(clean_tables, tid).info_hash
        m = Movie(title="鸣海的平日", display_name="怪兽8号 鸣海的平日", bangumi_id=901)
        s.add(m); s.commit(); s.refresh(m)
        s.add(MovieTorrent(info_hash=h, movie_id=m.id, status="pending", raw_title="x", source="ANi",
                           download_url="https://x/m.torrent"))
        s.commit()
    pv = A.cleanup_preview(aid, "foreign")
    assert not pv["items"] and pv["skipped"][0]["row_id"] == tid and "怪兽8号 鸣海的平日" in pv["skipped"][0]["reason"]
    res = _run(A.cleanup_execute(aid, "foreign", [(tid, h, None, None)]))
    assert res["skipped"] == [(tid, "预览后状态变了")] and not qb.info_calls and not qb.deleted
    assert _get(clean_tables, tid).status == "sent"


def test_g11_only_narrows_never_widens(clean_tables, qb):
    from core import anime as A
    with clean_tables.get_session() as s:
        aid, ani, nix, cp = _p27(s, lock="ANi")
    pv = A.cleanup_preview(aid, "two")
    it = pv["items"][0]
    bad = [(ani[13], _get(clean_tables, ani[13]).info_hash, nix[1], it["drop_hash"]),   # 预览里没有这一对（反着传）
           (it["drop_id"], "f" * 40, it["keep_id"], it["keep_hash"]),                    # 换了 hash
           (cp[3], _get(clean_tables, cp[3]).info_hash, ani[15], pv["items"][2]["keep_hash"]),  # 待下的
           (it["drop_id"], it["drop_hash"], ani[14], pv["items"][1]["keep_hash"])]       # 换了留哪份
    res = _run(A.cleanup_execute(aid, "two", bad))
    assert len(res["skipped"]) == 4 and not res["deleted"] and not qb.deleted, res
    res = _run(A.cleanup_execute(aid, "foreign", _picks(pv["items"][:1])))   # 类别不对
    assert res["skipped"] and not qb.deleted


def test_g12_running_twice_is_idempotent(clean_tables, qb):
    from core import anime as A
    with clean_tables.get_session() as s:
        aid, ani, nix, _ = _p27(s, lock="ANi")
    picks = _picks(A.cleanup_preview(aid, "two")["items"])
    _run(A.cleanup_execute(aid, "two", picks))
    assert A.cleanup_preview(aid, "two")["items"] == []
    n = len(qb.deleted)
    res = _run(A.cleanup_execute(aid, "two", picks))
    assert not res["deleted"] and len(qb.deleted) == n


def test_g12b_concurrent_runs_keeping_opposite_groups_never_delete_both(clean_tables, qb, monkeypatch):
    """一个标签页留 ANi 执行到一半（卡在 qB），另一个改锁 Nix 再执行：第二次要等第一次写完再复核，
    这时它要留的 Nix 那份已经删了，只能缩小 —— 同一集两套编号不能都被删掉。"""
    import asyncio as aio
    from core import anime as A, engine
    with clean_tables.get_session() as s:
        aid, ani, nix, _ = _p27(s, lock="ANi")
    picks_a = _picks(A.cleanup_preview(aid, "two")["items"])
    orig, gate = qb.torrents_info, {}

    async def slow(hs):
        if "ev" in gate and not gate["ev"].is_set():
            await gate["ev"].wait()
        return await orig(hs)
    monkeypatch.setattr(engine.qb, "torrents_info", slow)

    async def go():
        monkeypatch.setattr(A, "_CLEANUP_LOCK", aio.Lock())    # 每个 asyncio.run 一个新循环
        gate["ev"] = aio.Event()
        t1 = aio.create_task(A.cleanup_execute(aid, "two", picks_a))
        await aio.sleep(0.05)
        A.set_pref_source(aid, "Nix-Raws")
        picks_b = _picks(A.cleanup_preview(aid, "two")["items"])
        assert picks_b, "改锁那一刻 Nix 还在，另一个标签页看得到清单"
        t2 = aio.create_task(A.cleanup_execute(aid, "two", picks_b))
        await aio.sleep(0.05)
        gate["ev"].set()
        return await aio.gather(t1, t2)
    _run(go())
    for k in range(1, 12):
        pair = {_get(clean_tables, nix[k]).status, _get(clean_tables, ani[k + 12]).status}
        assert pair != {"deleted"}, f"第 {k} 集两份都删了"


def test_g13_ambiguous_offset_below_total_has_no_pairing(clean_tables):
    """#106 杀手旅店 O=13/T=15：d < T，按 D1 有意不当两套编号（这一段按源各下一份）。"""
    from core import anime as A
    with clean_tables.get_session() as s:
        aid = _anime(s, total_episodes=15, pref_source="ANi")
        _weekly(s, aid, "ANi", range(14, 21), W0)
        _weekly(s, aid, "Nix-Raws", range(1, 8), W0, lag=timedelta(hours=8))
    pv = A.cleanup_preview(aid, "two")
    assert pv["offset"] is None and pv["items"] == []


def test_g14_unlocked_without_keep_src_lists_nothing(clean_tables):
    from core import anime as A
    with clean_tables.get_session() as s:
        aid, *_ = _p27(s)
    pv = A.cleanup_preview(aid, "two")
    assert pv["items"] == [] and pv["warn"] == ["先选只下哪个组"]


def _g15(clean_tables, qb):
    """锁季内组：Nix 1–11 发布与 ANi 同步、却是后来一次补进库的（入库时间挤在一天），ANi 13–23 被清掉。"""
    from core import anime as A
    with clean_tables.get_session() as s:
        aid = _anime(s, pref_source="Nix-Raws")
        ani = _weekly(s, aid, "ANi", range(13, 24), W0)
        for e in range(1, 12):
            _row(s, aid, "Nix-Raws", e, W0 + timedelta(weeks=e - 1, hours=8),
                 created_at=W0 + timedelta(weeks=12))
    pv = A.cleanup_preview(aid, "two")
    assert len(pv["items"]) == 11
    res = _run(A.cleanup_execute(aid, "two", _picks(pv["items"])))
    assert sorted(res["deleted"]) == sorted(ani.values())
    return aid, ani


def test_g15_locked_in_season_group_no_false_lock_lag(clean_tables, qb):
    """D1：清掉全系列号后 ep_dropped=1，锁定源落后不报这些号（写 0 会冒出假的『锁定源落后』）。"""
    from core import anime as A
    aid, ani = _g15(clean_tables, qb)
    a = A.get_anime(aid)
    assert aid not in A.locked_out_episodes([a])


def test_g15_control_dropped_zero_would_report(clean_tables, qb):
    from core import anime as A
    aid, ani = _g15(clean_tables, qb)
    for tid in ani.values():
        _set(clean_tables, tid, ep_dropped=False)
    lo = A.locked_out_episodes([A.get_anime(aid)]).get(aid)
    assert lo and set(lo["eps"]) >= set(range(13, 24)), lo


# ================================================================ 执行细节

def test_gone_and_archived_are_reported_with_path(clean_tables, qb):
    """qB 里早就没有了的 / 已归档的：只改记录，结果里给路径（文件可能还在盘上）。"""
    from core import anime as A
    with clean_tables.get_session() as s:
        aid = _anime(s)
        a = _row(s, aid, "ANi", 1, W0, foreign_season=True)
        b = _row(s, aid, "ANi", 2, W0, foreign_season=True, archived_at=datetime.now())
    qb.missing.add(_get(clean_tables, a).info_hash)
    res = _run(A.cleanup_execute(aid, "foreign", _picks(A.cleanup_preview(aid, "foreign")["items"])))
    assert sorted(i for i, _ in res["gone"]) == sorted([a, b]) and not res["deleted"], res
    assert all(p.startswith("/dl/26C/殿下/Season 2/[ANi]") for _, p in res["gone"])
    assert {_get(clean_tables, t).status for t in (a, b)} == {"deleted"}


def test_execute_logs_one_line_per_row(clean_tables, qb, caplog):
    from core import anime as A
    with clean_tables.get_session() as s:
        aid, ani, nix, _ = _p27(s, lock="ANi")
    pv = A.cleanup_preview(aid, "two")
    with caplog.at_level("INFO", logger="autorss"):
        _run(A.cleanup_execute(aid, "two", _picks(pv["items"][:2])))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("清理(two)")]
    assert len(lines) == 2 and "「Nix-Raws 第1集」 留 tid=" in lines[0] and "「ANi 第13集」" in lines[0]
    assert lines[0].endswith("结果=deleted")


# ================================================================ 回归

def test_alias_keeper_and_delete_drops_episode_unchanged(clean_tables):
    """单条 🗑：两套编号里的一份能找到另一套里留着的那份（换文案）；删它照旧算"这一集的最后一份"（E-73 不变）。"""
    from core import anime as A
    with clean_tables.get_session() as s:
        aid, ani, nix, _ = _p27(s)
    assert A.alias_keeper(nix[3]) == {"keep_id": ani[15], "keep_src": "ANi", "keep_ep": 15, "ep": 3}
    assert A.alias_keeper(ani[15])["keep_id"] == nix[3]
    assert A.delete_drops_episode(nix[3]) is True
    _set(clean_tables, nix[3], release_time=W0 + timedelta(weeks=5))   # 超出时间窗：拿不准是同一集，不换说法
    assert A.alias_keeper(nix[3]) is None
    assert A.alias_keeper(_get(clean_tables, nix[4]).id)["keep_ep"] == 16


def test_best_offset_extracted_without_behavior_change(clean_tables):
    from core import anime as A
    with clean_tables.get_session() as s:
        aid, *_ = _p27(s)
    h = A.two_numberings()[aid]
    assert (h["abs_src"], h["in_src"], h["offset"], h["pairs"]) == ("ANi", "Nix-Raws", 12, 11)
    assert A._best_offset({"A": {13: W0}, "B": {}}, 12) is None
