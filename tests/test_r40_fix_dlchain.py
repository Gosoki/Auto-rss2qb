"""(r40 fix-dlchain) 下载链路上几处"做了但没跟上"的地方。每组用例钉的是：

· 死种转失败之后 qB 里又下完了（R39A-2）：归档轮的 recheck_stalled 把交付过的失败行认回已下完，但【只认下完】——
  有进度没下完的失败行不放回（放回就又算"这一集已有"，把同集健康的兄弟挡住）；fail_reason 按番剧 / 剧场版分叉。
· 停滞超时设成 0（DOC-2）：两样都不做，0% 与有进度的都留在 sent；设置页说明不再写"不自动换源"那句旧话。
· 修正版删旧版（R39A-3）：每删一条旧版之前重读新版，新版没了 / 被标别季就收手；剧场版还持有旧版 hash 时 qB 一下都不碰。
· 兜底那份不当旧版删（R39A-5 ①），A 步推送不说"补发了"（R39A-5 ②，断言在 test_r39_lock_fallback）；
  换锁不清兜底标记（E-74 安全网：换锁 / 点错改回之后 A 步照样 force 下锁定组的那一集）。
· 排除锁定组那一集（R39A-6）：判据不动（不报锁源落后、B 步不补），只在排除成功、且这一集此刻没有别的一份时
  在提示里当场说清出路。
· 剧场版单版本删除先问 qB 在不在（R39B-4）：不在就返回 "gone"，页面如实说文件没动。
· /movies『立即扫描』整轮失败不报绿（R39B-6），如实说有没有排自动重试。
零网络：qB 与推送全部打桩。
"""
import asyncio
import itertools
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from nicegui import ui
from nicegui.testing.user_interaction import UserInteraction
from sqlmodel import select

from core import anime as A
from core import engine as ce
from db.models import Anime, AnimeTorrent, Movie, MovieTorrent

_CREATED = datetime.now() - timedelta(hours=3)
_DETAIL_MAIN = "tests/render_detail_main.py"
_MAIN = "tests/render_main.py"
LOCK = "喵萌奶茶屋"
_seq = itertools.count(1)


def _at(d: date, days: float) -> datetime:
    return datetime(d.year, d.month, d.day) + timedelta(days=days)


def _mk(db, rows, *, total=12, air=None, season=2, **kw):
    with db.get_session() as s:
        a = Anime(**{**dict(title="某番", display_name="某番 第二季", season=season, quarter="26D", confirmed=True,
                            rejected=False, total_episodes=total, air_date=air.isoformat() if air else None,
                            bangumi_id=777), **kw})
        s.add(a)
        s.commit()
        s.refresh(a)
        ids = {}
        for i, r in enumerate(rows):
            tag, ep, st, rel = r[:4]
            extra = dict(r[4]) if len(r) > 4 else {}
            src = extra.pop("source", "ANi")
            t = AnimeTorrent(anime_id=a.id, info_hash=f"{a.id * 1000 + i:040x}", source=src, site="nyaa",
                             anime_title="某番", season=extra.pop("season", 1), episode=float(ep), status=st,
                             qb_progress=1.0 if st == "sent" else 0.0, release_time=rel,
                             raw_title=extra.pop("raw_title", f"[{src}] 某番 - {int(ep):02d} [1080p]"),
                             download_url=f"https://x/{a.id}-{i}.torrent",
                             created_at=extra.pop("created_at", _CREATED), **extra)
            s.add(t)
            s.commit()
            ids[tag] = t.id
        return a.id, ids


def _get(db, model_cls, tid):
    with db.get_session() as s:
        t = s.get(model_cls, tid)
        s.expunge(t)
        return t


def _real_delivery(cfg, monkeypatch, tmp_path, **extra):
    cfg(QB_ENABLED=True, QB_SYNC_STATUS=True, DOWN_PATH=str(tmp_path), ANIME_DOWN_PATH="",
        ANIME_FINISH_UNSUB=False, ANIME_DOWNLOAD_GRACE_MIN=0, ANIME_BACKFILL_PREAIR_DAYS=30, **extra)

    async def fetch(url):
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


# ── R39A-2：死种转失败之后 qB 里下完了 ─────────────────────────────────────────────────────

def _stall_cfg(cfg, minutes=1440):
    cfg(QB_ENABLED=True, QB_SYNC_STATUS=True, QB_STALL_TIMEOUT_MIN=minutes, QB_SYNC_INTERVAL=30)


def _dead_seed(db, model_cls=AnimeTorrent, prog=0.0, h=f"{1:040x}"):
    """已交付（save_path 非空）、qB 里 0% 两天没动的种子。"""
    now = datetime.now()
    with db.get_session() as s:
        if model_cls is AnimeTorrent:
            a = Anime(title="x", season=1, quarter="26C", confirmed=True, rejected=False, total_episodes=12)
            s.add(a)
            s.commit()
            s.refresh(a)
            t = AnimeTorrent(anime_id=a.id, episode=1, raw_title="x 1")
        else:
            m = Movie(title="某片", bangumi_id=2)
            s.add(m)
            s.commit()
            s.refresh(m)
            t = MovieTorrent(movie_id=m.id, raw_title="[组] 某片")
        t.info_hash, t.status, t.save_path = h, "sent", "/dl/26C/x"
        t.qb_progress, t.qb_state = prog, "stalledDL"
        t.qb_synced_at, t.qb_progress_at = now - timedelta(seconds=30), now - timedelta(days=2)
        s.add(t)
        s.commit()
        return t.id


def test_dead_seed_turned_error_is_taken_back_when_qb_finishes_it(clean_tables, cfg, monkeypatch):
    """0% 一天 → 失败（r38）；第二天 qB 把它下完了 → 归档轮的复查把它落成已下完、原因清空。"""
    _stall_cfg(cfg)
    tid = _dead_seed(clean_tables)
    state = {"p": 0.0, "st": "stalledDL"}

    async def info(hs):
        return {f"{1:040x}": {"state": state["st"], "progress": state["p"], "dlspeed": 0, "size": 1,
                              "content_path": "/dl/x1.mp4"}}
    monkeypatch.setattr(ce.qb, "torrents_info", info)
    asyncio.run(ce.sync_qb_status(AnimeTorrent))
    assert _get(clean_tables, AnimeTorrent, tid).status == "error", "前提：0% 一天 → 失败"
    state.update(p=1.0, st="stalledUP")
    asyncio.run(ce.sync_qb_status(AnimeTorrent))
    with clean_tables.get_session() as s:      # 认回这一步自己要写：别靠前面那次 sync 留下的值蒙混过关
        t = s.get(AnimeTorrent, tid)
        t.qb_synced_at, t.qb_state = None, "stalledDL"
        s.add(t)
        s.commit()
    assert asyncio.run(ce.recheck_stalled()) == 1
    got = _get(clean_tables, AnimeTorrent, tid)
    assert (got.status, got.qb_progress, got.fail_reason) == ("sent", 1.0, ""), (got.status, got.fail_reason)
    assert got.qb_synced_at is not None, "归档倒计时要有起点"
    assert got.qb_state == "", "停滞时的 qB 状态不该留着"


def test_dead_movie_seed_turned_error_is_taken_back_too(clean_tables, cfg, monkeypatch):
    """剧场版的 fail_reason 也承诺了"下完会自动认回来"：剧场版交付过的失败行同样被复查认回。"""
    _stall_cfg(cfg)
    tid = _dead_seed(clean_tables, MovieTorrent)
    with clean_tables.get_session() as s:
        t = s.get(MovieTorrent, tid)
        t.status, t.fail_reason = "error", "原因"
        s.add(t)
        s.commit()

    async def info(hs):
        return {f"{1:040x}": {"state": "stalledUP", "progress": 1.0, "dlspeed": 0, "size": 1}}
    monkeypatch.setattr(ce.qb, "torrents_info", info)
    assert asyncio.run(ce.recheck_stalled()) == 1
    got = _get(clean_tables, MovieTorrent, tid)
    assert (got.status, got.qb_progress, got.fail_reason) == ("sent", 1.0, ""), (got.status, got.fail_reason)


def test_error_row_with_some_progress_is_not_put_back_and_the_healthy_sibling_still_downloads(
        clean_tables, cfg, monkeypatch, tmp_path):
    """A 是落成失败的死种，同集 B 健康、在待下。复查时 qB 里 A 蹭到 0.2% 又不动了：A 不能放回 sent
    （放回就算"这一集已有"，B 被挡住、A 一天后落 stalled，这一集卡死等人），B 照常被下。"""
    _real_delivery(cfg, monkeypatch, tmp_path, QB_STALL_TIMEOUT_MIN=1440, QB_SYNC_INTERVAL=30)
    d = date.today() - timedelta(weeks=3)
    aid, ids = _mk(clean_tables, [
        ("A", 2, "error", datetime.now() - timedelta(days=2),
         {"save_path": "/dl/26D/x", "season": 2, "fail_reason": "1440 分钟一点进度都没有"}),
        ("B", 2, "pending", datetime.now() - timedelta(days=2),
         {"source": "Nekomoe", "season": 2, "raw_title": "[Nekomoe] 某番 - 02", "created_at": datetime.now()}),
    ], air=d)
    hA = _get(clean_tables, AnimeTorrent, ids["A"]).info_hash

    async def info(hs):
        return {hA: {"state": "stalledDL", "progress": 0.002, "dlspeed": 0, "size": 1}} if hA in hs else {}
    monkeypatch.setattr(ce.qb, "torrents_info", info)
    assert asyncio.run(ce.recheck_stalled()) == 0
    assert _get(clean_tables, AnimeTorrent, ids["A"]).status == "error"
    asyncio.run(A.flush_ready_downloads(qb_alive=True))
    assert _get(clean_tables, AnimeTorrent, ids["B"]).status == "sent", "健康的兄弟被挡住了"


@pytest.mark.parametrize("save_path, qb, why", [
    ("", {"state": "stalledUP", "progress": 1.0}, "从没交付过的失败（save_path 为空）不问 qB"),
    ("/dl/x", None, "qB 里已经没有这条（用户删了）"),
    ("/dl/x", {"state": "checkingUP", "progress": 1.0}, "100% 但在校验这类过渡态"),
    ("/dl/x", {"state": "pausedDL", "progress": 1.0 - 1e-3}, "没下完"),
    ("/dl/x", {"state": "stalledUP", "progress": 0.9}, "做种态但没下完"),
])
def test_error_rows_that_must_be_left_alone(clean_tables, cfg, monkeypatch, save_path, qb, why):
    _stall_cfg(cfg)
    tid = _dead_seed(clean_tables)
    with clean_tables.get_session() as s:
        t = s.get(AnimeTorrent, tid)
        t.status, t.save_path, t.fail_reason = "error", save_path, "原因"
        s.add(t)
        s.commit()
    asked = []

    async def info(hs):
        asked.append(list(hs))
        return {f"{1:040x}": {**qb, "dlspeed": 0, "size": 1}} if qb else {}
    monkeypatch.setattr(ce.qb, "torrents_info", info)
    assert asyncio.run(ce.recheck_stalled()) == 0, why
    got = _get(clean_tables, AnimeTorrent, tid)
    assert (got.status, got.fail_reason) == ("error", "原因"), why
    if not save_path:
        assert not asked, "从没交付过的失败行不该去问 qB"


@pytest.mark.parametrize("to", ["deleted", "excluded", "pending"])
def test_error_row_changed_during_the_await_is_not_written_back(clean_tables, cfg, monkeypatch, to):
    """torrents_info 的 await 期间这一行被人删了 / 排除了 / 点『补下』清回待下：写回前按主键重读，不覆盖成 sent。"""
    _stall_cfg(cfg)
    tid = _dead_seed(clean_tables)
    with clean_tables.get_session() as s:
        t = s.get(AnimeTorrent, tid)
        t.status = "error"
        s.add(t)
        s.commit()

    async def info(hs):
        with clean_tables.get_session() as s:
            t = s.get(AnimeTorrent, tid)
            t.status = to
            s.add(t)
            s.commit()
        return {f"{1:040x}": {"state": "stalledUP", "progress": 1.0, "dlspeed": 0, "size": 1}}
    monkeypatch.setattr(ce.qb, "torrents_info", info)
    assert asyncio.run(ce.recheck_stalled()) == 0
    assert _get(clean_tables, AnimeTorrent, tid).status == to


@pytest.mark.parametrize("model_cls", [AnimeTorrent, MovieTorrent])
def test_dead_seed_fail_reason_only_promises_what_that_pipeline_does(clean_tables, cfg, monkeypatch, caplog,
                                                                     model_cls):
    """番剧有换源（锁源 / 版本关键词的不换）；剧场版没有任何自动下载路径，原因和日志都不能写"换源"。"""
    _stall_cfg(cfg)
    tid = _dead_seed(clean_tables, model_cls)

    async def info(hs):
        return {f"{1:040x}": {"state": "stalledDL", "progress": 0.0, "dlspeed": 0, "size": 1}}
    monkeypatch.setattr(ce.qb, "torrents_info", info)
    asyncio.run(ce.sync_qb_status(model_cls))
    got = _get(clean_tables, model_cls, tid)
    assert got.status == "error"
    assert "下完会自动认回来" in got.fail_reason, got.fail_reason
    if model_cls is AnimeTorrent:
        assert "后台会换源" in got.fail_reason and "锁了源" in got.fail_reason, got.fail_reason
        assert "剧场版" not in got.fail_reason, got.fail_reason
    else:
        assert "剧场版不会自动换" in got.fail_reason and "后台会换源" not in got.fail_reason, got.fail_reason
    logs = [r.getMessage() for r in caplog.records if "零进度、转为失败" in r.getMessage()]
    assert len(logs) == 1, logs
    assert ("以便换源" in logs[0]) is (model_cls is AnimeTorrent), logs


# ── DOC-2：停滞 / 死种超时设成 0 ───────────────────────────────────────────────────────────

def test_stall_timeout_zero_turns_both_branches_off(clean_tables, cfg, monkeypatch):
    """设置页标签写的"0=关"：0% 的不转失败、有进度的不转停滞，两条都留在 sent。"""
    _stall_cfg(cfg, minutes=0)
    ids = [_dead_seed(clean_tables, prog=p, h=f"{i + 1:040x}") for i, p in enumerate((0.0, 0.4))]

    async def info(hs):
        return {h: {"state": "stalledDL", "progress": p, "dlspeed": 0, "size": 1}
                for h, p in ((f"{1:040x}", 0.0), (f"{2:040x}", 0.4))}
    monkeypatch.setattr(ce.qb, "torrents_info", info)
    asyncio.run(ce.sync_qb_status(AnimeTorrent))
    assert [_get(clean_tables, AnimeTorrent, i).status for i in ids] == ["sent", "sent"]


def test_settings_help_describes_the_dead_seed_branch():
    src = (Path(__file__).resolve().parent.parent / "pages" / "settings.py").read_text(encoding="utf-8")
    sec = src[src.index('_section("下载 / qBittorrent"'):]
    sec = sec[:sec.index("ui.separator()")]
    assert "死种" in sec and "剧场版要自己在版本列表里另点一个" in sec and "设成 0 两样都不做" in sec, sec
    assert "供人工处理（不自动换源）。" not in sec


# ── R39A-3：修正版删旧版 ───────────────────────────────────────────────────────────────────

def _v1v2(db, movie_holds_v1=False, v1_kw=None, second_old=False):
    rel = datetime.now() - timedelta(days=2)
    with db.get_session() as s:
        a = Anime(title="某番", display_name="某番", season=1, quarter="26C", confirmed=True, rejected=False,
                  total_episodes=12, bangumi_id=1)
        s.add(a)
        s.commit()
        s.refresh(a)
        v1 = AnimeTorrent(anime_id=a.id, info_hash="1" * 40, source="ANi", site="nyaa", anime_title="某番", season=1,
                          episode=4.0, status="sent", qb_progress=1.0, raw_title="[ANi] 某番 - 04 [1080P][CHT]",
                          release_time=rel, created_at=rel, content_path="/dl/v1.mp4", **(v1_kw or {}))
        v2 = AnimeTorrent(anime_id=a.id, info_hash="2" * 40, source="ANi", site="nyaa", anime_title="某番", season=1,
                          episode=4.0, status="sent", qb_progress=1.0, raw_title="[ANi] 某番 - 04 [1080P][CHT][V2]",
                          release_time=rel + timedelta(hours=5), created_at=rel, content_path="/dl/v2.mp4")
        s.add(v1)
        s.add(v2)
        if second_old:   # 同组同集另一份旧版（重发的 v1，hash 不同）
            s.add(AnimeTorrent(anime_id=a.id, info_hash="4" * 40, source="ANi", site="nyaa", anime_title="某番",
                               season=1, episode=4.0, status="sent", qb_progress=1.0,
                               raw_title="[ANi] 某番 - 04 [1080P][CHT]", release_time=rel + timedelta(hours=1),
                               created_at=rel, content_path="/dl/v1b.mp4"))
        if movie_holds_v1:
            m = Movie(title="某番 剧场版", bangumi_id=2)
            s.add(m)
            s.commit()
            s.refresh(m)
            s.add(MovieTorrent(movie_id=m.id, info_hash="1" * 40, source="ANi", raw_title="[ANi] 某番 - 04",
                               status="sent", qb_progress=0.4))
        s.commit()
        return a.id, v1.id, v2.id


def _retire_stubs(monkeypatch, on_info=None, v1_progress=1.0, on_delete=None):
    deleted = []

    async def fake_info(hs):
        if on_info:
            on_info()
        return {"1" * 40: {"content_path": "/dl/v1.mp4", "progress": v1_progress,
                           "state": "stoppedUP" if v1_progress >= 1 else "downloading"},
                "2" * 40: {"content_path": "/dl/v2.mp4", "progress": 1.0, "state": "stoppedUP"},
                "4" * 40: {"content_path": "/dl/v1b.mp4", "progress": 1.0, "state": "stoppedUP"}}

    async def fake_delete(hs, delete_files=True):
        deleted.append((tuple(hs), delete_files))
        if on_delete:
            on_delete()
        return True
    monkeypatch.setattr(ce.qb, "torrents_info", fake_info)
    monkeypatch.setattr(ce.qb, "delete", fake_delete)
    return deleted


@pytest.mark.parametrize("what", ["user_deleted", "foreign"])
def test_retire_rechecks_the_new_version_after_the_await(clean_tables, cfg, monkeypatch, what):
    """torrents_info 的 await 期间用户删了新版（"只删这一份"）或把它标成别季：旧版不能再被连文件删掉。"""
    cfg(ANIME_REPLACE_VERSIONS=True, QB_ENABLED=True, ANIME_DOWNLOAD_GRACE_MIN=0, ANIME_FINISH_UNSUB=False)
    aid, v1_id, v2_id = _v1v2(clean_tables)

    def change_v2():
        with clean_tables.get_session() as s:
            t = s.get(AnimeTorrent, v2_id)
            if what == "user_deleted":
                A._mark_user_deleted(s, t)
            else:
                t.foreign_season = True
                s.add(t)
            s.commit()
    deleted = _retire_stubs(monkeypatch, on_info=change_v2)
    assert asyncio.run(A._retire_old_versions()) == 0
    assert _get(clean_tables, AnimeTorrent, v1_id).status == "sent"
    assert not deleted, deleted


def test_retire_rechecks_the_new_version_before_every_old_one(clean_tables, cfg, monkeypatch):
    """两份旧版：删第一份的 qb.delete 的 await 期间用户删了新版 —— 第二份旧版不能再删（只在第一份前重读不够）。"""
    cfg(ANIME_REPLACE_VERSIONS=True, QB_ENABLED=True, ANIME_DOWNLOAD_GRACE_MIN=0, ANIME_FINISH_UNSUB=False)
    aid, v1_id, v2_id = _v1v2(clean_tables, second_old=True)

    def user_deletes_v2():
        with clean_tables.get_session() as s:
            t = s.get(AnimeTorrent, v2_id)
            if t.status != "deleted":
                A._mark_user_deleted(s, t)
                s.commit()
    deleted = _retire_stubs(monkeypatch, on_delete=user_deletes_v2)
    assert asyncio.run(A._retire_old_versions()) == 1
    assert len(deleted) == 1, deleted
    with clean_tables.get_session() as s:
        olds = s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid,
                                                   AnimeTorrent.id != v2_id)).all()
        assert sorted(o.status for o in olds) == ["deleted", "sent"], [(o.id, o.status) for o in olds]


def test_retire_does_not_touch_qb_when_the_movie_side_holds_the_old_hash(clean_tables, cfg, monkeypatch):
    """剧场版那边同一个 hash 还在下：qB 里不摘（单集删除 / 整部删除同口径），只落旧行；原因如实写。"""
    cfg(ANIME_REPLACE_VERSIONS=True, QB_ENABLED=True, ANIME_DOWNLOAD_GRACE_MIN=0, ANIME_FINISH_UNSUB=False)
    aid, v1_id, v2_id = _v1v2(clean_tables, movie_holds_v1=True)
    deleted = _retire_stubs(monkeypatch, v1_progress=0.4)
    assert asyncio.run(A._retire_old_versions()) == 1
    assert not deleted, f"剧场版还持有的种子被从 qB 里摘掉了：{deleted}"
    o = _get(clean_tables, AnimeTorrent, v1_id)
    assert o.status == "deleted" and not o.ep_dropped
    assert "剧场版" in o.fail_reason and "qB 里没动" in o.fail_reason, o.fail_reason
    assert "只删了种子" not in o.fail_reason, o.fail_reason


def test_retire_still_deletes_a_plain_old_version(clean_tables, cfg, monkeypatch):
    """对照：新版好好的、没有剧场版持有 → 旧版照常连文件删。"""
    cfg(ANIME_REPLACE_VERSIONS=True, QB_ENABLED=True, ANIME_DOWNLOAD_GRACE_MIN=0, ANIME_FINISH_UNSUB=False)
    aid, v1_id, v2_id = _v1v2(clean_tables)
    deleted = _retire_stubs(monkeypatch)
    assert asyncio.run(A._retire_old_versions()) == 1
    assert deleted == [(("1" * 40,), True)]
    assert _get(clean_tables, AnimeTorrent, v1_id).status == "deleted"


# ── R39A-5 ①：兜底那份不当旧版删 ───────────────────────────────────────────────────────────

def test_retire_never_deletes_the_lock_fallback_copy(clean_tables, cfg, monkeypatch):
    """锁喵萌、第 4 集由 ANi 兜底（lock_fallback）；用户手动下了 ANi 的 v2；锁定组后来补发了第 4 集（skipped）。
    E-74『兜底那份永不自动删』：v1 不被删，A 步仍认这一集。"""
    cfg(ANIME_REPLACE_VERSIONS=True, QB_ENABLED=True, ANIME_DOWNLOAD_GRACE_MIN=0, ANIME_FINISH_UNSUB=False,
        ANIME_LOCK_FALLBACK_DAYS=14)
    aid, v1_id, v2_id = _v1v2(clean_tables, v1_kw={"lock_fallback": True})
    with clean_tables.get_session() as s:
        a = s.get(Anime, aid)
        a.pref_source = LOCK
        s.add(a)
        s.add(AnimeTorrent(anime_id=aid, info_hash="3" * 40, source=LOCK, anime_title="某番", season=1, episode=4.0,
                           status="skipped", raw_title=f"[{LOCK}] 某番 - 04 [1080p]",
                           release_time=datetime.now() - timedelta(hours=5),
                           created_at=datetime.now() - timedelta(hours=4)))
        s.commit()
    assert 4.0 in (A._lock_catchup_plan().get(aid) or {}).get("eps", {}), "前提：A 步认这一集"
    deleted = _retire_stubs(monkeypatch)
    assert asyncio.run(A._retire_old_versions()) == 0
    assert not deleted, deleted
    assert _get(clean_tables, AnimeTorrent, v1_id).status == "sent"
    assert 4.0 in (A._lock_catchup_plan().get(aid) or {}).get("eps", {}), "退旧版之后 A 步不再认这一集"


# ── R39A-5 守卫：换锁不清兜底标记（E-74 的安全网） ─────────────────────────────────────────
# 换锁之后 A 步按新锁定组补下兜底过的集是有意保留的（B 步约 11% 误触发，兜底那份可能是错内容）。
# 这几条防的是以后有人去做"换锁就清 lock_fallback"：那样新锁定组的这一集失去一切自动路径，点错再改回也救不回。

NEW = "绿茶字幕组"


def _fb_row(s, aid, src, ep, status, at, **kw):
    n = next(_seq) + 7000
    t = AnimeTorrent(anime_id=aid, info_hash=f"{n:040x}", source=src, site="nyaa",
                     raw_title=f"[{src}] 某番 - {int(ep):02d} [1080p]", season=1, episode=float(ep),
                     status=status, download_url=f"https://x/{n}.torrent", created_at=at,
                     release_time=at - timedelta(hours=1), **kw)
    s.add(t)
    s.commit()
    s.refresh(t)
    return t.id


def _fb_base(db, cfg):
    """锁喵萌，喵萌发了 1~5（已下）后停更；第 6 集由 ANi 兜底（lock_fallback，已下）。"""
    cfg(ANIME_LOCK_FALLBACK_DAYS=14, ANIME_LOCK_LAG_DAYS=7, ANIME_LOCK_SYNC_DAYS=2, ANIME_FINISH_UNSUB=False,
        FEED_GAPS="", ANIME_DOWNLOAD_GRACE_MIN=60, QB_ENABLED=True)
    now = datetime.now()
    with db.get_session() as s:
        a = Anime(title="某番", display_name="某番", season=1, quarter="26C", confirmed=True, rejected=False,
                  total_episodes=12, bangumi_id=1, pref_source=LOCK)
        s.add(a)
        s.commit()
        s.refresh(a)
        for e in range(1, 6):
            _fb_row(s, a.id, LOCK, e, "sent", now - timedelta(days=75 - 7 * (e - 1)))
        fb = _fb_row(s, a.id, "ANi", 6, "sent", now - timedelta(days=40), priority=10, lock_fallback=True)
        return a.id, fb


def _fake_force(monkeypatch, db):
    calls = []

    async def fake_dl(tid, force=False, **kw):
        calls.append((tid, force))
        with db.get_session() as s:
            t = s.get(AnimeTorrent, tid)
            t.status = "sent"
            s.add(t)
            s.commit()
        return True

    async def fake_notify(kind, msg, **kw):
        return True
    monkeypatch.setattr(A, "download_anime_torrent", fake_dl)
    monkeypatch.setattr(A, "notify_event", fake_notify)
    return calls


async def test_lock_change_still_fetches_the_new_groups_copy_of_a_fallback_episode(clean_tables, cfg, monkeypatch):
    """换锁到绿茶（它的第 6 集早在库里）：A 步 force 下它，兜底那份的标记还在（详情页『兜底』徽标靠它）。"""
    aid, fb = _fb_base(clean_tables, cfg)
    with clean_tables.get_session() as s:
        new6 = _fb_row(s, aid, NEW, 6, "pending", datetime.now() - timedelta(days=41), priority=5)
    A.set_pref_source(aid, NEW)
    calls = _fake_force(monkeypatch, clean_tables)
    await A.deliver_lock_fallback(qb_alive=True)
    assert (new6, True) in calls, f"换锁后新锁定组的第 6 集没有被 A 步下：{calls}"
    assert _get(clean_tables, AnimeTorrent, fb).lock_fallback, "换锁后兜底那份的 lock_fallback 被清了"


@pytest.mark.parametrize("via", [NEW, ""])
async def test_lock_toggled_and_back_keeps_the_catchup_promise(clean_tables, cfg, monkeypatch, via):
    """喵萌 → 绿茶 → 喵萌（下拉框点错改回），或先『改回按优先级』再锁回喵萌：之后喵萌补发的第 6 集仍被 A 步 force 下。"""
    aid, fb = _fb_base(clean_tables, cfg)
    A.set_pref_source(aid, via)
    A.set_pref_source(aid, LOCK)
    with clean_tables.get_session() as s:
        late6 = _fb_row(s, aid, LOCK, 6, "skipped", datetime.now() - timedelta(hours=3))   # 即时同集闸落成 skipped
    calls = _fake_force(monkeypatch, clean_tables)
    await A.deliver_lock_fallback(qb_alive=True)
    assert (late6, True) in calls, f"锁改回原来的组之后，它补发的第 6 集不再自动下：{calls}"
    assert _get(clean_tables, AnimeTorrent, fb).lock_fallback


# ── R39A-6：排除锁定组那一集 ──────────────────────────────────────────────────────────────

def _lrow(s, aid, src, ep, status, at, **kw):
    n = next(_seq) + 5000
    s.add(AnimeTorrent(anime_id=aid, info_hash=f"{n:040x}", source=src, site="nyaa",
                       raw_title=f"[{src}] 某番 - {int(ep):02d} [1080p]", season=1, episode=float(ep),
                       status=status, download_url=f"https://x/{n}.torrent", created_at=at,
                       release_time=at - timedelta(hours=1), **kw))


@pytest.mark.parametrize("excluded_ep", [6, 7])
async def test_excluding_the_lock_groups_episode_is_not_reported(clean_tables, cfg, monkeypatch, excluded_ep):
    """排除锁定组那一集＝用户处理过：不推锁源落后（推了就是"锁定组没有第 N 集"这种失实内容），B 步不补，A 步不动。"""
    cfg(ANIME_LOCK_FALLBACK_DAYS=14, ANIME_LOCK_LAG_DAYS=7, ANIME_LOCK_SYNC_DAYS=2, ANIME_FINISH_UNSUB=False,
        FEED_GAPS="", ANIME_DOWNLOAD_GRACE_MIN=60, QB_ENABLED=True, ALERT_REPEAT_DAYS=7)
    now = datetime.now()
    with clean_tables.get_session() as s:
        a = Anime(title="某番", display_name="某番", season=1, quarter="26C", confirmed=True, rejected=False,
                  total_episodes=12, bangumi_id=1, pref_source=LOCK)
        s.add(a)
        s.commit()
        s.refresh(a)
        for e in range(1, 8):
            _lrow(s, a.id, LOCK, e, "excluded" if e == excluded_ep else "sent", now - timedelta(days=60 - 7 * (e - 1)))
            _lrow(s, a.id, "ANi", e, "pending" if e == excluded_ep else "skipped",
                  now - timedelta(days=61 - 7 * (e - 1)), priority=10)
        s.commit()
    msgs = []

    async def fake_notify(kind, msg, **kw):
        msgs.append((kind, msg))
        return True
    monkeypatch.setattr(A, "notify_enabled", lambda k: True)
    monkeypatch.setattr(A, "notify_event", fake_notify)
    assert await A.sweep_lock_lag() == 0 and not msgs, msgs
    assert A.lock_fallback_plan() == {} and A._lock_catchup_plan() == {}


def _notes(user, monkeypatch):
    seen = []
    orig = type(user.notify).__call__

    def rec(self, message, **kw):
        seen.append((message, kw.get("type")))
        return orig(self, message, **kw)
    monkeypatch.setattr(type(user.notify), "__call__", rec)
    return seen


async def _wait_for(seen, pred):
    for _ in range(300):
        if any(pred(m) for m, _t in seen):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"等不到提示：{seen}")


@pytest.mark.parametrize("lock, src, sibling, ok, want", [
    (LOCK, LOCK, False, True, "lock"),     # 锁着喵萌、排除的是喵萌那一条：别的组的这一集不会自动下，当场说
    (LOCK, "ANi", False, True, "plain"),   # 排除的是别的组那条：锁定组那一集照常下，不多嘴
    (None, LOCK, False, True, "plain"),    # 没锁源：按优先级换源，不多嘴
    (LOCK, LOCK, True, True, "plain"),     # 同组另一版本已下好、只是清掉失败的那条：这一集不缺，不多嘴
    (LOCK, LOCK, False, False, "fail"),    # 排除失败：照旧说失败，不提锁
])
@pytest.mark.nicegui_main_file(_DETAIL_MAIN)
async def test_exclude_toast_explains_the_lock_only_for_the_lock_groups_row(
        user, clean_tables, cfg, monkeypatch, lock, src, sibling, ok, want):
    from pages import anime_detail as D

    async def yes(*a, **k):
        return True
    monkeypatch.setattr(D, "confirm", yes)
    if not ok:
        monkeypatch.setattr(A, "exclude_torrent", lambda tid: False)
    with clean_tables.get_session() as s:
        a = Anime(title="某番", display_name="某番", season=1, quarter="26C", confirmed=True, rejected=False,
                  total_episodes=12, bangumi_id=1, pref_source=lock)
        s.add(a)
        s.commit()
        s.refresh(a)
        aid = a.id
        _lrow(s, aid, LOCK, 5, "sent", datetime.now() - timedelta(days=8))   # 下拉框里要有锁定组这个选项
        _lrow(s, aid, src, 6, "error", datetime.now() - timedelta(days=1), save_path="/dl/x")
        if sibling:
            _lrow(s, aid, src, 6, "sent", datetime.now() - timedelta(hours=20), qb_progress=1.0)
        s.commit()
    seen = _notes(user, monkeypatch)
    await user.open(f"/detail/{aid}")
    btns = [b for b in user.find(kind=ui.button, content="排除").elements if b.text == "排除"]
    assert len(btns) == 1, btns
    UserInteraction(user, {btns[0]}, None).click()
    await _wait_for(seen, lambda m: m.startswith(("已排除", "排除失败")))
    (msg, typ), = [(m, t) for m, t in seen if m.startswith(("已排除", "排除失败"))]
    if want == "lock":
        assert "不会自动下" in msg and f"『{LOCK}』" in msg and "『下载』" in msg, msg
    elif want == "plain":
        assert msg == "已排除" and typ == "positive", (msg, typ)
    else:
        assert msg.startswith("排除失败") and typ == "warning", (msg, typ)


# ── R39B-4：剧场版单版本删除先问 qB 在不在 ──────────────────────────────────────────────────

def _movie_row(db, status="sent"):
    with db.get_session() as s:
        m = Movie(title="某片", display_name="某片", quarter="2026", bangumi_id=7)
        s.add(m)
        s.commit()
        s.refresh(m)
        mt = MovieTorrent(movie_id=m.id, info_hash="b" * 40, raw_title="[组] 某片 [BDRip]", status=status,
                          qb_progress=1.0, save_path="E:/M/2026/某片")
        s.add(mt)
        s.commit()
        return m.id, mt.id


def _qb_for_movie(monkeypatch, info_ret):
    deleted = []

    async def info(hs):
        return info_ret

    async def delete(hs, delete_files=True):
        deleted.append(tuple(hs))
        return True
    monkeypatch.setattr(ce.qb, "torrents_info", info)
    monkeypatch.setattr(ce.qb, "delete", delete)
    return deleted


@pytest.mark.parametrize("info_ret, want, status, sent_delete", [
    ({}, "gone", "deleted", False),                                    # qB 里早没了：不发删除，如实返回 gone
    ({"b" * 40: {"state": "stalledUP", "progress": 1.0}}, True, "deleted", True),   # qB 里有：照常删
    (None, False, "sent", False),                                      # qB 连不上：什么都不改
])
def test_movie_delete_asks_qb_first(clean_tables, monkeypatch, info_ret, want, status, sent_delete):
    from core import movies as M
    _, mt_id = _movie_row(clean_tables)
    deleted = _qb_for_movie(monkeypatch, info_ret)
    assert asyncio.run(M.delete_movie_torrent(mt_id)) == want
    assert _get(clean_tables, MovieTorrent, mt_id).status == status
    assert bool(deleted) is sent_delete, deleted


@pytest.mark.parametrize("in_qb", [False, True])
@pytest.mark.nicegui_main_file(_DETAIL_MAIN)
async def test_movie_delete_toast_does_not_claim_files_were_deleted_when_qb_had_nothing(
        user, clean_tables, cfg, monkeypatch, in_qb):
    from pages import movies as P

    async def yes(*a, **k):
        return True
    monkeypatch.setattr(P, "confirm", yes)
    cfg(QB_ENABLED=True)
    mid, mt_id = _movie_row(clean_tables)
    _qb_for_movie(monkeypatch, {"b" * 40: {"state": "stalledUP", "progress": 1.0}} if in_qb else {})
    seen = _notes(user, monkeypatch)
    await user.open(f"/mdetail/{mid}")
    btns = [b for b in user.find(ui.button).elements if b.props.get("icon") == "delete_forever"]
    assert len(btns) == 1, btns
    UserInteraction(user, {btns[0]}, None).click()
    await _wait_for(seen, lambda m: m.startswith("已"))
    (msg, typ), = [(m, t) for m, t in seen if m.startswith("已")]
    if in_qb:
        assert (msg, typ) == ("已删除该版本文件", "positive")
    else:
        assert "文件仍在 E:/M/2026/某片" in msg and typ == "warning", (msg, typ)


# ── R39B-6：/movies『立即扫描』整轮失败 ────────────────────────────────────────────────────

_SCAN = {"movies": 0, "torrents": 0, "seen": 0, "errors": 0, "bgm_down": False, "mikan_ids": set(),
         "failed": False, "retry_at": None}


async def _scan_toasts(user, monkeypatch, cfg, res, auto_on=True):
    """桩 worker.scan_movies_now（不走真扫描：真失败会打 ERROR 日志，user 夹具收尾判红）。
    按类型取『立即扫描』按钮：按文字找会先找到同名的『手动立即扫描』标签。"""
    from core import worker as W
    cfg(MOVIE_SCAN_ENABLED=auto_on)

    async def fake(year, letters):
        return dict(res)
    monkeypatch.setattr(W, "scan_movies_now", fake)
    seen = _notes(user, monkeypatch)
    await user.open("/movies?t=sources")
    await user.should_see("手动立即扫描")
    btn = next(b for b in user.find(ui.button).elements if b.text == "立即扫描")
    UserInteraction(user, {btn}, None).click()
    await _wait_for(seen, lambda m: m.startswith(("扫描完成", "扫描失败")))
    await asyncio.sleep(0.05)
    return [(m, t) for m, t in seen if not m.startswith("扫描 ")]     # 去掉开头那句"扫描 … 请稍候"


@pytest.mark.parametrize("auto_on, retry_at, want, not_want", [
    (True, "2026-11-12T15:11:00", "稍后自动重试", "稍后再扫一次"),     # 当年四季 + 开着自动扫描：真排了重试
    (False, "2026-11-12T15:11:00", "稍后再扫一次", "自动重试"),        # 自动扫描关着：不会自己重试
    (True, None, "稍后再扫一次", "自动重试"),                          # 往年 / 单季：没排重试
])
@pytest.mark.nicegui_main_file(_MAIN)
async def test_total_scan_failure_is_not_green_and_tells_the_truth_about_retry(
        user, clean_tables, cfg, monkeypatch, auto_on, retry_at, want, not_want):
    res = {**_SCAN, "errors": 4, "failed": True, "retry_at": retry_at}
    toasts = await _scan_toasts(user, monkeypatch, cfg, res, auto_on)
    assert len(toasts) == 1, toasts
    msg, typ = toasts[0]
    assert typ == "warning", f"整轮失败却报绿：{toasts}"
    assert msg.startswith("扫描失败："), msg
    assert "4 个出错" in msg and want in msg and not_want not in msg, msg


@pytest.mark.nicegui_main_file(_MAIN)
async def test_bgm_down_scan_says_retry_once(user, clean_tables, cfg, monkeypatch):
    """bgm 一部都没问成：两句提示都不是绿的，『重试』只在 bgm 那句里说一次。"""
    res = {**_SCAN, "seen": 3, "movies": 3, "torrents": 9, "bgm_down": True, "failed": True,
           "retry_at": "2026-11-12T15:11:00"}
    toasts = await _scan_toasts(user, monkeypatch, cfg, res)
    assert [t for _m, t in toasts] == ["warning", "warning"], toasts
    assert sum("重试" in m for m, _t in toasts) == 1 and "bgm 连接失败" in toasts[1][0], toasts
    assert toasts[0][0].startswith("扫描完成："), toasts    # 源站扫到了，只是 bgm 没问成


@pytest.mark.parametrize("res", [
    {**_SCAN, "seen": 5, "movies": 1, "torrents": 3},                    # 干净的一轮
    {**_SCAN, "seen": 5, "movies": 1, "torrents": 3, "errors": 2},       # 个别片出错、整轮不算失败
    {**_SCAN},                                                           # 桶是空的（年初）：不是失败
])
@pytest.mark.nicegui_main_file(_MAIN)
async def test_non_failed_scan_stays_green_and_says_nothing_about_retry(user, clean_tables, cfg, monkeypatch, res):
    toasts = await _scan_toasts(user, monkeypatch, cfg, res)
    assert len(toasts) == 1 and toasts[0][1] == "positive" and toasts[0][0].startswith("扫描完成："), toasts
    assert "重试" not in toasts[0][0] and "再扫" not in toasts[0][0], toasts
