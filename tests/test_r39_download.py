"""r39 下载健壮性 + 提权（D4 / D7b / I6 / I8，外加 Q2(d) 的旧版提示文案）。

D4  交付前按番此刻的订阅/锁定源/版本关键词复核一次（prefs_gate）：候选是在 await 之前一次选好的，途中改了偏好的不再按旧偏好下。
D7b 整机断网期间退避表用满不转失败，按最后一档继续排队。
I6  修正版替换的判据收拢成 _replaceable：标签严格相等 + 季号 + 番名 + 发布时间差不超过 ANIME_REPLACE_MAX_GAP_DAYS。
I8  别的源先收过、行上优先级更低的待下种子，被高优先级源再见到时提到该源的优先级（不升确认）。
"""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import httpx
import pytest
from sqlmodel import select

from core import anime as A
from core import engine as ce
from db.models import Anime, AnimeTorrent

OLD = datetime.now() - timedelta(hours=5)


def _anime(s, **kw):
    base = dict(title="某番", display_name="某番", season=1, quarter="26C", confirmed=True, rejected=False,
                total_episodes=12, bangumi_id=1)
    base.update(kw)
    a = Anime(**base)
    s.add(a)
    s.commit()
    s.refresh(a)
    return a


_n = iter(range(1, 10 ** 6))


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


def _get(clean_tables, model, pk):
    with clean_tables.get_session() as s:
        o = s.get(model, pk)
        s.expunge(o)
        return o


@pytest.fixture
def real_chain(cfg, monkeypatch):
    """真交付链：只把取种和加进 qB 两步换成记账。返回取过种的 URL 列表；on_fetch(url) 可在取种当中插一手。"""
    cfg(QB_ENABLED=True, QB_SYNC_STATUS=True, DOWN_PATH="/tmp/dl", ANIME_DOWN_PATH="",
        ANIME_FINISH_UNSUB=False, ANIME_DOWNLOAD_GRACE_MIN=0)
    fetched = []
    hooks = {"on_fetch": None}

    async def fetch(url):
        fetched.append(url)
        if hooks["on_fetch"]:
            hooks["on_fetch"](url)
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
    return NS(fetched=fetched, hooks=hooks)


def _set_anime(clean_tables, aid, **kw):
    with clean_tables.get_session() as s:
        a = s.get(Anime, aid)
        for k, v in kw.items():
            setattr(a, k, v)
        s.add(a)
        s.commit()


# ── D4：放行前复核 ────────────────────────────────────────────────

def test_lock_changed_mid_flush_only_first_delivered_then_new_lock(clean_tables, real_chain):
    """flush 途中（第 1 集取种时）把锁定源改成 GB：第 1 集照常交付；GA 第 2 集复核没过、仍是 pending（不是 skipped）；
    下一轮交付 GB 的第 2 集。"""
    with clean_tables.get_session() as s:
        aid = _anime(s).id
        ga1 = _row(s, aid, "GA", 1, priority=100)
        ga2 = _row(s, aid, "GA", 2, priority=100)
        gb2 = _row(s, aid, "GB", 2, priority=10)
    real_chain.hooks["on_fetch"] = lambda url: _set_anime(clean_tables, aid, pref_source="GB")
    assert asyncio.run(A.flush_ready_downloads(qb_alive=True)) == 1
    assert _get(clean_tables, AnimeTorrent, ga1).status == "sent"
    t = _get(clean_tables, AnimeTorrent, ga2)
    assert t.status == "pending" and not t.fail_reason, "被挡的行原样留在待下，不能被顺手标成 skipped / 记失败"
    assert len(real_chain.fetched) == 1, "GA 第 2 集不该去取种"
    real_chain.hooks["on_fetch"] = None
    assert asyncio.run(A.flush_ready_downloads(qb_alive=True)) == 1
    assert _get(clean_tables, AnimeTorrent, gb2).status == "sent"
    assert _get(clean_tables, AnimeTorrent, ga2).status == "pending"


def test_unconfirmed_mid_flush_stops_the_rest(clean_tables, real_chain):
    """flush 途中番被打回待确认（confirmed=False）：后面的条目不再交付，留在 pending。"""
    with clean_tables.get_session() as s:
        aid = _anime(s).id
        ids = [_row(s, aid, "GA", e) for e in (1, 2, 3)]
    real_chain.hooks["on_fetch"] = lambda url: _set_anime(clean_tables, aid, confirmed=False)
    assert asyncio.run(A.flush_ready_downloads(qb_alive=True)) == 1
    st = [_get(clean_tables, AnimeTorrent, i).status for i in ids]
    assert st.count("sent") == 1 and st.count("pending") == 2, st
    assert len(real_chain.fetched) == 1


def test_keyword_miss_returns_false_unless_force_or_gate_off(clean_tables, real_chain):
    """直接调非 force 交付、版本关键词没命中：返回 False（不是 None）、状态不变、没去取种。
    同样的行 force=True 或 prefs_gate=False 时照常交付。"""
    with clean_tables.get_session() as s:
        aid = _anime(s, pref_keyword="CHT").id
        a1 = _row(s, aid, "GA", 1)
        a2 = _row(s, aid, "GA", 2)
        a3 = _row(s, aid, "GA", 3)
    assert asyncio.run(A.download_anime_torrent(a1)) is False
    assert _get(clean_tables, AnimeTorrent, a1).status == "pending" and real_chain.fetched == []
    assert asyncio.run(A.download_anime_torrent(a2, force=True)) is True
    assert asyncio.run(A.download_anime_torrent(a3, prefs_gate=False)) is True
    assert {_get(clean_tables, AnimeTorrent, i).status for i in (a2, a3)} == {"sent"}


@pytest.mark.parametrize("change", [dict(rejected=True), dict(pref_source="GB")])
def test_force_does_not_gate_by_default(clean_tables, real_chain, change):
    """E-54 不变：人工 force 默认不复核（忽略了的番、别的组的行，点『下载』照样下）。"""
    with clean_tables.get_session() as s:
        aid = _anime(s, **change).id
        tid = _row(s, aid, "GA", 1)
    assert asyncio.run(A.download_anime_torrent(tid, force=True)) is True


def test_download_locked_out_still_delivers_through_real_chain(clean_tables, real_chain):
    """『只补这几集』是有意越过锁定源的：走真实交付链（prefs_gate=False）照样能下别的组的行。"""
    with clean_tables.get_session() as s:
        aid = _anime(s, pref_source="LOCK").id
        _row(s, aid, "LOCK", 1, "sent")
        _row(s, aid, "LOCK", 2, "sent")
        other = _row(s, aid, "ANi", 3)
    assert asyncio.run(A.download_locked_out(aid)) == 1
    assert _get(clean_tables, AnimeTorrent, other).status == "sent"


def test_group_pick_backfill_delivers_through_real_chain_despite_lock(clean_tables, real_chain):
    """(r39 F20)『补齐该源』挑定了一个组（_download_new_rows，strict_kw=False → prefs_gate=False）是有意越过锁定/关键词/订阅的：
    番锁了别的组、设了不命中的关键词、还在待确认，走真实交付链照样能下。谁把 prefs_gate 写错，复核会把它静默挡成零交付。
    对照：同样的行按『自动补齐』口径（strict_kw=True）不下。"""
    with clean_tables.get_session() as s:
        aid = _anime(s, pref_source="LOCK", pref_keyword="CHS", confirmed=False).id
        _row(s, aid, "LOCK", 1, "sent")
        picked = _row(s, aid, "NEST", 2)
        auto = _row(s, aid, "NEST", 3)
    assert asyncio.run(A._download_new_rows(aid, [auto], kw="CHS", strict_kw=True)) == 0
    assert asyncio.run(A._download_new_rows(aid, [picked], kw="CHS")) == 1
    assert _get(clean_tables, AnimeTorrent, picked).status == "sent"
    assert _get(clean_tables, AnimeTorrent, auto).status == "pending"


def test_deliver_single_works_still_delivers_through_real_chain(clean_tables, real_chain, cfg):
    """单部作品自动下一份（非 force，默认复核）：复核与它自己的选片同口径，照样能交付 -2 行。"""
    cfg(ANIME_SINGLE_WORK_AUTO=True)
    with clean_tables.get_session() as s:
        aid = _anime(s, total_episodes=1, platform="WEB").id
        tid = _row(s, aid, "X", -2, raw_title="[X] 某番 [WebRip 1080p]")
    assert asyncio.run(A.deliver_single_works(qb_alive=True)) == 1
    assert _get(clean_tables, AnimeTorrent, tid).status == "sent"


@pytest.mark.parametrize("ignored", [False, True])
def test_deliver_new_versions_rechecks_after_picking(clean_tables, real_chain, cfg, monkeypatch, ignored):
    """修正版交付走 force（越过同集闸），但传 prefs_gate=True：选好之后、交付之前番被忽略了就不交付。"""
    cfg(ANIME_REPLACE_VERSIONS=True)
    with clean_tables.get_session() as s:
        aid = _anime(s).id
        _row(s, aid, "ANi", 4, "sent", raw_title="[ANi] 某番 - 04 [1080P][CHT][MP4]", qb_progress=1.0)
        v2 = _row(s, aid, "ANi", 4, "skipped", raw_title="[ANi] 某番 - 04 [1080P][CHT][V2][MP4]")
    real = A.download_anime_torrent

    async def ignore_then_deliver(tid, *a, **kw):
        if ignored:      # 窗口期：计划已定、交付还没开始，用户点了忽略
            _set_anime(clean_tables, aid, rejected=True)
        return await real(tid, *a, **kw)
    monkeypatch.setattr(A, "download_anime_torrent", ignore_then_deliver)
    assert asyncio.run(A._deliver_new_versions()) == (0 if ignored else 1)
    assert _get(clean_tables, AnimeTorrent, v2).status == ("skipped" if ignored else "sent")
    assert len(real_chain.fetched) == (0 if ignored else 1)


@pytest.mark.parametrize("race", ["exclude_v2", "delete_last_old"])
def test_deliver_new_versions_rechecks_each_pick_after_the_previous_await(clean_tables, real_chain, cfg, race):
    """(r39 F21) 两集都有修正版要交付；第 1 条取种期间用户 ① 排除了另一集的 v2，或 ② 删掉了另一集仅剩的旧版（该集已删）。
    第 2 条不能再用 force 交付：① 是用户不要这条；② 交付成功还会经 clear_ep_dropped 把"这一集不要了"清掉。"""
    cfg(ANIME_REPLACE_VERSIONS=True)
    ids = {}
    with clean_tables.get_session() as s:
        aid = _anime(s).id
        for ep in (4, 5):
            ids[("v1", ep)] = _row(s, aid, "ANi", ep, "sent", qb_progress=1.0,
                                   raw_title=f"[ANi] 某番 - {ep:02d} [1080P][CHT][MP4]")
            ids[("v2", ep)] = _row(s, aid, "ANi", ep, "skipped",
                                   raw_title=f"[ANi] 某番 - {ep:02d} [1080P][CHT][V2][MP4]")
    url_ep = {_get(clean_tables, AnimeTorrent, ids[("v2", ep)]).download_url: ep for ep in (4, 5)}
    hit = {}

    def on_fetch(url):
        if hit:
            return
        hit["other"] = other = 9 - url_ep[url]
        with clean_tables.get_session() as s:
            if race == "exclude_v2":
                t = s.get(AnimeTorrent, ids[("v2", other)])
                t.status = "excluded"
            else:
                t = s.get(AnimeTorrent, ids[("v1", other)])
                t.status, t.ep_dropped = "deleted", True
            s.add(t)
            s.commit()
    real_chain.hooks["on_fetch"] = on_fetch
    assert asyncio.run(A._deliver_new_versions()) == 1
    assert len(real_chain.fetched) == 1, "第 2 条不该去取种"
    other = hit["other"]
    assert _get(clean_tables, AnimeTorrent, ids[("v2", other)]).status == (
        "excluded" if race == "exclude_v2" else "skipped")
    if race == "delete_last_old":
        assert _get(clean_tables, AnimeTorrent, ids[("v1", other)]).ep_dropped is True, "用户的『该集已删』被清掉了"


# ── D7b：整机断网期间用满不转失败 ─────────────────────────────────

def _tail_row(clean_tables, **kw):
    with clean_tables.get_session() as s:
        aid = _anime(s).id
        return aid, _row(s, aid, "GA", 1, retry_count=len(ce.retry_backoff_min()), **kw)


def _fail_fetch(monkeypatch, exc):
    async def fetch(url):
        raise exc
    monkeypatch.setattr(ce, "fetch_torrent_bytes", fetch)


@pytest.mark.parametrize("down", [True, False])
def test_exhausted_backoff_during_network_down_keeps_queueing(clean_tables, real_chain, monkeypatch, down):
    """rc=表长、取种抛 ConnectError：整机断网 → pending、rc+1、retry_at≈now+最后一档；没断网 → error（与以前一样）。"""
    _aid, tid = _tail_row(clean_tables)
    rc0 = len(ce.retry_backoff_min())
    monkeypatch.setattr(A.health, "network_down", lambda: down)
    _fail_fetch(monkeypatch, httpx.ConnectError("[Errno 8] nodename nor servname provided"))
    assert asyncio.run(A.download_anime_torrent(tid)) is False
    t = _get(clean_tables, AnimeTorrent, tid)
    assert t.fail_reason.startswith("取种失败")
    if down:
        assert t.status == "pending" and t.retry_count == rc0 + 1
        want = datetime.now() + timedelta(minutes=ce.retry_backoff_min()[-1])
        assert abs((t.retry_at - want).total_seconds()) < 60
    else:
        assert t.status == "error" and t.retry_at is None


def test_exhausted_backoff_on_404_without_network_down_goes_error(clean_tables, real_chain, monkeypatch):
    """网络正常时的 404：照旧用满落 error（有告警），不无限排队。"""
    _aid, tid = _tail_row(clean_tables)
    monkeypatch.setattr(A.health, "network_down", lambda: False)
    req = httpx.Request("GET", "https://x/t.torrent")
    _fail_fetch(monkeypatch, httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req)))
    assert asyncio.run(A.download_anime_torrent(tid)) is False
    assert _get(clean_tables, AnimeTorrent, tid).status == "error"


def test_two_tail_siblings_deliver_exactly_once_after_recovery(clean_tables, real_chain):
    """同集两个兄弟都在尾档排队，恢复且到点：flush 只交付 1 条，另一条不交付。"""
    rc = len(ce.retry_backoff_min())
    past = datetime.now() - timedelta(minutes=1)
    with clean_tables.get_session() as s:
        aid = _anime(s).id
        a = _row(s, aid, "GA", 1, retry_count=rc, retry_at=past, priority=50)
        b = _row(s, aid, "GB", 1, retry_count=rc, retry_at=past, priority=40)
    assert asyncio.run(A.flush_ready_downloads(qb_alive=True)) == 1
    st = sorted(_get(clean_tables, AnimeTorrent, i).status for i in (a, b))
    assert st == ["pending", "sent"] and len(real_chain.fetched) == 1


def test_tail_row_revives_skipped_sibling(clean_tables, cfg):
    """尾档排队的行（pending、rc>0）照常让 _revive_orphaned_skipped 复活 skipped 兄弟去换源。"""
    cfg(ANIME_FINISH_UNSUB=False)
    rc = len(ce.retry_backoff_min()) + 3
    with clean_tables.get_session() as s:
        aid = _anime(s).id
        _row(s, aid, "GA", 1, retry_count=rc, retry_at=datetime.now() + timedelta(hours=20))
        sib = _row(s, aid, "GB", 1, "skipped")
    A._revive_orphaned_skipped()
    assert _get(clean_tables, AnimeTorrent, sib).status == "pending"


# ── I6：修正版替换判据 _replaceable ─────────────────────────────────

SUB = "[ANi] 假面骑士ZEZTZ - 41 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]"
SUB_V2 = "[ANi] 假面骑士ZEZTZ - 41 [1080P][Baha][WEB-DL][AAC AVC][CHT][V2][MP4]"
DUB = "[ANi] 假面骑士ZEZTZ [中文配音] - 41 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]"
DUB_V2 = "[ANi] 假面骑士ZEZTZ [中文配音] - 41 [1080P][Baha][WEB-DL][AAC AVC][CHT][V2][MP4]"


def _versions(clean_tables, cfg, monkeypatch, rows, qb_paths):
    """rows=[(key, raw, status, 额外字段)]，同组 ANi 第 41 集；qb_paths={key: qB 回报的 content_path}（缺省=qB 里没有）。"""
    cfg(ANIME_REPLACE_VERSIONS=True, QB_ENABLED=True, ANIME_DOWNLOAD_GRACE_MIN=0, ANIME_FINISH_UNSUB=False)
    ids = {}
    with clean_tables.get_session() as s:
        aid = _anime(s, title="假面骑士ZEZTZ").id
        for k, raw, st, extra in rows:
            kw = dict(raw_title=raw, anime_title="假面骑士ZEZTZ", qb_progress=1.0 if st == "sent" else 0.0)
            kw.update(extra)
            ids[k] = _row(s, aid, "ANi", 41, st, **kw)
    hashes = {k: _get(clean_tables, AnimeTorrent, i).info_hash for k, i in ids.items()}
    calls = []

    async def fake_download(tid, force=False, **kw):
        calls.append(("download", tid, force))
        with clean_tables.get_session() as s:
            t = s.get(AnimeTorrent, tid)
            t.status, t.qb_progress = "sent", 1.0
            s.add(t)
            s.commit()
        return True

    async def fake_info(hs):
        return {hashes[k]: {"content_path": p, "progress": 1.0, "state": "stoppedUP"}
                for k, p in qb_paths.items()}

    async def fake_delete(hs, delete_files=True):
        calls.append(("delete", tuple(hs), delete_files))
        return True
    monkeypatch.setattr(A, "download_anime_torrent", fake_download)
    monkeypatch.setattr(ce.qb, "torrents_info", fake_info)
    monkeypatch.setattr(ce.qb, "delete", fake_delete)
    return ids, hashes, calls


@pytest.mark.parametrize("new_status", ["skipped", "sent"])
def test_dub_superset_v2_neither_downloads_nor_deletes(clean_tables, cfg, monkeypatch, new_status):
    """旧版是字幕版（已下），组发了『[中文配音]… [V2]』：标签是旧版的超集，以前判成同一种 → 下配音版、再把字幕版连文件删掉。
    现在严格相等：不下；人工 force 下来的配音 v2 也不触发删旧版。"""
    ids, _h, calls = _versions(clean_tables, cfg, monkeypatch, [
        ("v1", SUB, "sent", {}), ("v2", DUB_V2, new_status, {}),
    ], {"v1": "E:/A/41.mp4", "v2": "E:/A/41 dub.mp4"})
    assert asyncio.run(A.replace_versions()) == (0, 0) and calls == []
    assert _get(clean_tables, AnimeTorrent, ids["v1"]).status == "sent"


def test_dub_old_version_plus_plain_v2_is_not_replaced(clean_tables, cfg, monkeypatch):
    """#724 型：下的是配音版，来了普通字幕版的 V2 —— 那是另一个版本，不替换。"""
    ids, _h, calls = _versions(clean_tables, cfg, monkeypatch, [
        ("v1", DUB, "sent", {}), ("v2", SUB_V2, "skipped", {}),
    ], {"v1": "E:/A/41.mp4"})
    assert asyncio.run(A.replace_versions()) == (0, 0) and calls == []


@pytest.mark.parametrize("extra", [dict(season=2), dict(anime_title="另一部番")])
def test_season_or_title_mismatch_is_not_replaced(clean_tables, cfg, monkeypatch, extra):
    """季号不同（跨季同集号）、入库番名不同（别名碰撞）：标签一样、时间也近，也不替换。"""
    ids, _h, calls = _versions(clean_tables, cfg, monkeypatch, [
        ("v1", SUB, "sent", {}), ("v2", SUB_V2, "skipped", extra),
    ], {"v1": "E:/A/41.mp4"})
    assert asyncio.run(A.replace_versions()) == (0, 0) and calls == []


@pytest.mark.parametrize("n_days, replaced", [(30, False), (40, True)])
def test_release_gap_threshold(clean_tables, cfg, monkeypatch, n_days, replaced):
    """发布时间相差 31 天：门槛 30 不替换，40 替换（先下新版、下完删旧版）。"""
    cfg(ANIME_REPLACE_MAX_GAP_DAYS=n_days)
    ids, hashes, calls = _versions(clean_tables, cfg, monkeypatch, [
        ("v1", SUB, "sent", dict(release_time=OLD - timedelta(days=31))), ("v2", SUB_V2, "skipped", {}),
    ], {"v1": "E:/A/41.mp4", "v2": "E:/A/41 v2.mp4"})
    assert asyncio.run(A.replace_versions()) == ((1, 1) if replaced else (0, 0))
    if replaced:
        assert calls == [("download", ids["v2"], True), ("delete", (hashes["v1"],), True)]
    else:
        assert calls == []


def test_release_gap_uses_absolute_value(clean_tables, cfg, monkeypatch):
    """nyaa 存 UTC、mikan 存北京墙钟：新版"早"8 小时也是近的，照常替换。"""
    ids, _h, calls = _versions(clean_tables, cfg, monkeypatch, [
        ("v1", SUB, "sent", {}), ("v2", SUB_V2, "skipped", dict(release_time=OLD - timedelta(hours=8))),
    ], {"v1": "E:/A/41.mp4", "v2": "E:/A/41 v2.mp4"})
    assert asyncio.run(A.replace_versions()) == (1, 1)


@pytest.mark.parametrize("side", ["v1", "v2"])
def test_missing_release_time_is_not_replaced(clean_tables, cfg, monkeypatch, side):
    """任一侧没有发布时间：判断不了是不是同一季的修正，拿不准就两份都留。"""
    rows = [("v1", SUB, "sent", {}), ("v2", SUB_V2, "skipped", {})]
    rows = [(k, raw, st, dict(release_time=None) if k == side else ex) for k, raw, st, ex in rows]
    _ids, _h, calls = _versions(clean_tables, cfg, monkeypatch, rows, {"v1": "E:/A/41.mp4"})
    assert asyncio.run(A.replace_versions()) == (0, 0) and calls == []


def test_retire_respects_the_gap_for_manually_forced_v2(clean_tables, cfg, monkeypatch):
    """人工 force 下来的 v2（已下完）发布时间与旧版相差超过门槛：删旧版这一步也不动旧版。"""
    cfg(ANIME_REPLACE_MAX_GAP_DAYS=30)
    ids, _h, calls = _versions(clean_tables, cfg, monkeypatch, [
        ("v1", SUB, "sent", dict(release_time=OLD - timedelta(days=84))), ("v2", SUB_V2, "sent", {}),
    ], {"v1": "E:/A/41.mp4", "v2": "E:/A/41 v2.mp4"})
    assert asyncio.run(A._retire_old_versions()) == 0 and calls == []
    assert _get(clean_tables, AnimeTorrent, ids["v1"]).status == "sent"


def test_same_variant_helper_is_gone():
    """_same_variant 已被 _replaceable 取代（判据只此一份）。"""
    assert not hasattr(A, "_same_variant")


# ── Q2(d)：qB 里已经没有旧版时的提示 ─────────────────────────────

@pytest.mark.parametrize("old_cp, want, not_want", [
    ("E:/A/41.mp4", "旧文件 E:/A/41.mp4 如果还在可手动删", "别手动删"),
    (None, "未删任何文件（可能与新版是同一个文件，别手动删）", "可手动删"),
    ("e:\\A\\41 v2.mp4", "未删任何文件（可能与新版是同一个文件，别手动删）", "可手动删"),   # 与新版同一个文件
])
def test_retire_note_when_old_is_gone_from_qb(clean_tables, cfg, monkeypatch, old_cp, want, not_want):
    """旧版已不在 qB：什么都没删。只有确知旧版另有一个文件才指给人删；路径不明或与新版同一个文件时叫人别删。"""
    ids, _h, calls = _versions(clean_tables, cfg, monkeypatch, [
        ("v1", SUB, "sent", dict(content_path=old_cp)), ("v2", SUB_V2, "sent", {}),
    ], {"v2": "E:/A/41 v2.mp4"})
    assert asyncio.run(A._retire_old_versions()) == 1
    assert calls == [], "旧版不在 qB 里，不该发删除"
    o = _get(clean_tables, AnimeTorrent, ids["v1"])
    assert o.status == "deleted" and want in o.fail_reason and not_want not in o.fail_reason, o.fail_reason


def test_retire_note_when_movie_side_holds_the_same_hash(clean_tables, cfg, monkeypatch):
    """(r39 F22) 旧版已不在 qB、剧场版那边仍持有同一个 hash：qB 按 hash 只存一份，那个"旧文件"多半就是剧场版那份 ——
    不能叫人去手动删。日志也不能写"只删种子 / 连文件"（什么都没删，F23）。"""
    from db.models import MovieTorrent
    ids, hashes, calls = _versions(clean_tables, cfg, monkeypatch, [
        ("v1", SUB, "sent", dict(content_path="E:/Movie/X/41.mp4")), ("v2", SUB_V2, "sent", {}),
    ], {"v2": "E:/A/41 v2.mp4"})
    with clean_tables.get_session() as s:
        s.add(MovieTorrent(movie_id=1, info_hash=hashes["v1"], raw_title="mv", status="sent"))
        s.commit()
    logged = []
    monkeypatch.setattr(A.log, "info", lambda fmt, *a, **k: logged.append(fmt % a))
    assert asyncio.run(A._retire_old_versions()) == 1
    assert calls == []
    o = _get(clean_tables, AnimeTorrent, ids["v1"])
    assert o.status == "deleted" and "未删任何文件" in o.fail_reason and "剧场版" in o.fail_reason, o.fail_reason
    assert "可手动删" not in o.fail_reason, o.fail_reason
    line = next(x for x in logged if x.startswith("修正版：已删除旧版"))
    assert "未删任何东西" in line and "只删种子" not in line and "连文件" not in line, line


# ── I8：已入库种子提权 ───────────────────────────────────────────

def _item(h, priority, source="ANi"):
    return NS(info_hash=h, priority=priority, source=source, anime_title="某番", raw_title="x")


def test_promote_known_raises_priority_only(clean_tables):
    """① auto@20 先入库，auto@100 再见到：行提到 100；番状态、来源、站点、状态都不动；再查不再命中。"""
    with clean_tables.get_session() as s:
        aid = _anime(s).id
        tid = _row(s, aid, "ANi", 2, priority=20, site="mikan")
    h = _get(clean_tables, AnimeTorrent, tid).info_hash
    assert A.promotable_hashes([h, "f" * 40], 100) == {h}
    A.promote_known(_item(h, 100))
    t = _get(clean_tables, AnimeTorrent, tid)
    assert (t.priority, t.status, t.source, t.site) == (100, "pending", "ANi", "mikan")
    a = _get(clean_tables, Anime, aid)
    assert a.confirmed is True and not a.rejected
    assert A.promotable_hashes([h], 100) == set()


@pytest.mark.parametrize("prio", [20, 10, 0])
def test_promotable_hashes_needs_strictly_higher_priority(clean_tables, prio):
    """② 本源优先级不高于行（含 0＝没设优先级）→ 空集。"""
    with clean_tables.get_session() as s:
        aid = _anime(s).id
        tid = _row(s, aid, "ANi", 2, priority=20)
    h = _get(clean_tables, AnimeTorrent, tid).info_hash
    assert A.promotable_hashes([h], prio) == set()


@pytest.mark.parametrize("status", ["sent", "skipped"])
def test_handled_rows_are_not_promoted(clean_tables, status):
    """③ 行已下 / 已被去重：不动（只提待下的）。"""
    with clean_tables.get_session() as s:
        aid = _anime(s).id
        tid = _row(s, aid, "ANi", 2, status, priority=20)
    h = _get(clean_tables, AnimeTorrent, tid).info_hash
    assert A.promotable_hashes([h], 100) == set()
    A.promote_known(_item(h, 100))
    assert _get(clean_tables, AnimeTorrent, tid).priority == 20


def test_promote_does_not_confirm_pending_anime(clean_tables):
    """④ 番是待确认（含刚『绑定 bgm』的 confirm_reason='bind'）：提权之后仍是待确认，原因不变。"""
    with clean_tables.get_session() as s:
        aid = _anime(s, confirmed=False, confirm_reason="bind").id
        tid = _row(s, aid, "ANi", 2, priority=20)
    h = _get(clean_tables, AnimeTorrent, tid).info_hash
    A.promote_known(_item(h, 100))
    assert _get(clean_tables, AnimeTorrent, tid).priority == 100
    a = _get(clean_tables, Anime, aid)
    assert a.confirmed is False and a.confirm_reason == "bind"


async def test_poll_once_promotes_when_higher_source_sees_known_hash(clean_tables, monkeypatch, cfg):
    """⑤ 采集轮里两个带优先级的源先后见到同一个 hash：低的先入库，高的那个把它提上去。"""
    from core import worker as W
    cfg(QB_ENABLED=False, ANIME_POLL_ENABLED=True)
    with clean_tables.get_session() as s:
        aid = _anime(s).id
    h = "a" * 40

    class Src:
        def __init__(self, name, priority):
            self.name, self.priority = name, priority

        async def fetch(self, content_cache=None):
            return [_item(h, self.priority, self.name)]

    async def fake_process(item, known_hashes=None, qb_alive=None):
        if item.info_hash in (known_hashes or set()):
            return False
        with clean_tables.get_session() as s:      # 只做入库这一步（真 process_item 会去查 bgm）
            s.add(AnimeTorrent(anime_id=aid, info_hash=item.info_hash, source=item.source, raw_title="x",
                               episode=2.0, status="pending", priority=item.priority))
            s.commit()
        return True

    async def fake_precheck():
        return False
    monkeypatch.setattr(W, "build_sources", lambda: [Src("Mikan(兜底)", 20), Src("ANi", 100)])
    monkeypatch.setattr(W, "process_item", fake_process)
    monkeypatch.setattr(W.anime, "qb_precheck", fake_precheck)
    await W.poll_once()
    with clean_tables.get_session() as s:
        t = s.exec(select(AnimeTorrent).where(AnimeTorrent.info_hash == h)).one()
        assert (t.priority, t.source, t.status) == (100, "Mikan(兜底)", "pending")
