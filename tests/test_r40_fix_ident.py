"""识别与采集健康的三处守卫。

· 像剧场版的单部作品：用户在待确认上点了『确认下载』、或从已忽略『恢复订阅』之后，不管卡片上原来写的是什么原因，
  deliver_single_works 都不许再把它打回待确认；没人点过的照旧打回；不像剧场版的番确认 / 恢复不改原因。
· 稀疏 feed（关键词搜索 / 单番）空了一轮：seen 被清掉，下一轮出现的第一条不报断档；非空 feed 的断档照报。
  判据是整条 feed（白名单过滤之前）有没有带时间的条目，不是本组过滤后剩几条——本组 0 条照常推进 seen，
  同 URL 的其它组照报断档。
· 采集轮从源循环里早退（后面的组撞 DatabaseBusy / existing_hashes 抛错 / 协程被取消）：前面各组推进过的 seen
  还原成轮前，下一轮恰好报一次断档，再下一轮不重报；本轮因空 feed 清掉的 seen 不随回滚复活。
  收尾（写 FEED_GAPS 之后的推送）出错不回滚，下一轮不重报。
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import config


# ---------------- 像剧场版的单部作品：人点过就不打回 ----------------
def _single(clean_tables, cfg, monkeypatch, *, confirmed, rejected, reason, platform="剧场版"):
    from core import anime as A
    from db.models import Anime, AnimeTorrent
    cfg(ANIME_SINGLE_WORK_AUTO=True, QB_ENABLED=True, ANIME_DOWNLOAD_GRACE_MIN=0,
        ANIME_FINISH_UNSUB=False, ANIME_START_DATE="")
    old = datetime.now() - timedelta(hours=3)
    with clean_tables.get_session() as s:
        a = Anime(title="某片", season=1, quarter="26D", confirmed=confirmed, rejected=rejected,
                  confirm_reason=reason, total_episodes=1, platform=platform, bangumi_id=502499)
        s.add(a)
        s.commit()
        t = AnimeTorrent(anime_id=a.id, info_hash=f"{1:040x}", source="组", priority=10,
                         raw_title="[组] 某片 [Movie][WebRip]", episode=-2, status="pending", created_at=old)
        s.add(t)
        s.commit()
        aid, tid = a.id, t.id
    sent = []

    async def fake_dl(torrent_id, force=False):
        sent.append(torrent_id)
        with clean_tables.get_session() as s:
            row = s.get(AnimeTorrent, torrent_id)
            row.status = "sent"
            s.add(row)
            s.commit()
        return True
    monkeypatch.setattr(A, "download_anime_torrent", fake_dl)
    return A, aid, tid, sent


def _state(clean_tables, aid):
    from db.models import Anime
    with clean_tables.get_session() as s:
        a = s.get(Anime, aid)
        return a.confirmed, a.rejected, a.confirm_reason


@pytest.mark.parametrize("reason", ["review", "unmatched", "bind", "backfill", "released", "wrong_season", None])
def test_confirm_of_a_movie_like_work_sticks_whatever_the_card_said(clean_tables, cfg, monkeypatch, reason):
    """生产上的 GROTESQQQUE：卡片原因是『人工审核源发现』，点确认后下一轮被换成『像是剧场版』又回来。"""
    A, aid, tid, sent = _single(clean_tables, cfg, monkeypatch, confirmed=False, rejected=False, reason=reason)
    A.confirm_anime(aid)
    assert asyncio.run(A.deliver_single_works()) == 1 and sent == [tid]
    assert _state(clean_tables, aid)[:2] == (True, False)


def test_restore_of_a_movie_like_work_sticks(clean_tables, cfg, monkeypatch):
    A, aid, tid, sent = _single(clean_tables, cfg, monkeypatch, confirmed=False, rejected=True, reason=None)
    A.restore_anime(aid)
    assert asyncio.run(A.deliver_single_works()) == 1 and sent == [tid]
    assert _state(clean_tables, aid)[:2] == (True, False)


def test_confirm_of_ova_platform_with_same_bgm_in_movie_table_sticks(clean_tables, cfg, monkeypatch):
    """番剧侧 platform 是 OVA、剧场版表里有同一个 bgm（E-52 同体）也算像剧场版：确认后不许再打回。"""
    from db.models import Movie
    A, aid, tid, sent = _single(clean_tables, cfg, monkeypatch, confirmed=False, rejected=False,
                                reason="review", platform="OVA")
    with clean_tables.get_session() as s:
        s.add(Movie(bangumi_id=502499, title="某片"))
        s.commit()
    A.confirm_anime(aid)
    assert asyncio.run(A.deliver_single_works()) == 1 and sent == [tid]
    assert _state(clean_tables, aid) == (True, False, "movie")


def test_restore_of_web_platform_with_same_hash_in_movie_table_sticks(clean_tables, cfg, monkeypatch):
    """番剧侧 platform 是 WEB、剧场版表里有同一个种子 hash：恢复订阅后不许再打回。"""
    from db.models import Movie, MovieTorrent
    A, aid, tid, sent = _single(clean_tables, cfg, monkeypatch, confirmed=True, rejected=True,
                                reason="review", platform="WEB")
    with clean_tables.get_session() as s:
        m = Movie(bangumi_id=502499, title="某片")
        s.add(m)
        s.commit()
        s.add(MovieTorrent(info_hash=f"{1:040x}", movie_id=m.id, status="pending"))
        s.commit()
    A.restore_anime(aid)
    assert _state(clean_tables, aid) == (True, False, "movie")
    assert asyncio.run(A.deliver_single_works()) == 1 and sent == [tid]


def test_confirm_or_restore_of_a_non_movie_keeps_its_reason(clean_tables, cfg, monkeypatch):
    """不像剧场版的番，确认 / 恢复都不许写上 movie（否则以后被打回待确认时卡片会说它像剧场版）。"""
    A, aid, tid, sent = _single(clean_tables, cfg, monkeypatch, confirmed=False, rejected=False,
                                reason="review", platform="WEB")
    A.confirm_anime(aid)
    assert _state(clean_tables, aid) == (True, False, "review")
    A.reject_anime(aid)
    A.restore_anime(aid)
    assert _state(clean_tables, aid) == (True, False, "review")


def test_never_clicked_movie_like_work_still_bounces(clean_tables, cfg, monkeypatch):
    """没人点过的（后台自动升成在追的）照旧打回待确认、不下。"""
    A, aid, tid, sent = _single(clean_tables, cfg, monkeypatch, confirmed=True, rejected=False, reason=None)
    assert asyncio.run(A.deliver_single_works()) == 0 and sent == []
    assert _state(clean_tables, aid) == (False, False, "movie")


# ---------------- 采集轮的 seen 记账 ----------------
def _sec(d):
    return d.replace(microsecond=0)


@pytest.fixture
def poll(clean_tables, monkeypatch, cfg):
    from core import health
    from core import worker as W
    import db

    cfg(QB_ENABLED=False, ANIME_POLL_ENABLED=True, NOTIFY_URL="", HEARTBEAT_URL="",
        SOURCE_FEED_SEEN="", FEED_GAPS="")
    monkeypatch.setattr(health, "_seen", {})
    monkeypatch.setattr(health, "_fail", {})
    writes, gaps = [], []
    monkeypatch.setattr(config, "set_many", lambda u: writes.append(u) or {})
    real_gap = health.gap_record

    def spy_gap(src, frm, to):
        gaps.append((src, frm, to))
        return real_gap(src, frm, to)
    monkeypatch.setattr(health, "gap_record", spy_gap)

    async def fake_precheck():
        return False
    monkeypatch.setattr(W.anime, "qb_precheck", fake_precheck)

    async def fake_process(item, known_hashes=None, qb_alive=None):
        if item.kind == "busy":
            raise db.DatabaseBusy("整库维护中")
        if item.kind == "cancel":
            raise asyncio.CancelledError()
        return True
    monkeypatch.setattr(W, "process_item", fake_process)
    n = {"i": 0}

    def source(name, url, oldest, kinds):
        items = []
        for k in kinds:
            n["i"] += 1
            items.append(SimpleNamespace(info_hash=f"{n['i']:040x}", anime_title=f"番{n['i']}", kind=k))

        class FakeSource:
            async def fetch(self, content_cache=None):
                return items
        s = FakeSource()
        s.name, s.rss_url, s.oldest_entry_time = name, url, oldest
        return s

    def run(*sources):
        monkeypatch.setattr(W, "build_sources", lambda: list(sources))
        return W.poll_once()

    return SimpleNamespace(health=health, source=source, run=run, writes=writes, gaps=gaps, W=W)


_URL_A = "https://nyaa.si/?page=rss&u=ANiTorrent"
_URL_B = "https://mikanani.me/RSS/Classic"
_URL_Q = "https://nyaa.si/?page=rss&q=some+new+show&u=someone"


async def test_empty_feed_clears_seen_so_its_first_entry_is_not_a_gap(poll):
    """以前记过 seen 的稀疏 feed 空了一轮：seen 必须清掉（只"不推进"的话下一轮拿 1 小时前的 seen 去比，照样误报）。"""
    now = datetime.now(timezone.utc)
    poll.health.mark_seen(_URL_Q, _sec(now - timedelta(hours=1)))
    await poll.run(poll.source("新番搜索", _URL_Q, None, []))
    assert poll.health.seen_at(_URL_Q) is None, "空 feed 这一轮还留着 / 推进了 seen"
    assert poll.health._feed_key(_URL_Q) not in json.loads(poll.writes[-1]["SOURCE_FEED_SEEN"])
    # 第一条在空的那一轮之后 15 分钟才发布：比上一轮抓取时刻晚 10 分钟以上，正是会误报的形状
    await poll.run(poll.source("新番搜索", _URL_Q, datetime.now(timezone.utc) + timedelta(minutes=15), ["ok"]))
    assert poll.gaps == []
    assert not any(w.get("FEED_GAPS") for w in poll.writes)
    assert poll.health.seen_at(_URL_Q) is not None, "非空之后要照常记 seen"


async def test_non_empty_feed_gap_is_still_reported(poll):
    now = datetime.now(timezone.utc)
    prev, o1 = _sec(now - timedelta(days=15)), _sec(now - timedelta(days=9))
    poll.health.mark_seen(_URL_A, prev)
    await poll.run(poll.source("ANi", _URL_A, o1, ["ok"]))
    assert poll.gaps == [("ANi", prev, o1)]


async def test_group_with_zero_items_on_non_empty_feed_still_advances_seen(poll):
    """feed 有带时间的条目、只是本组白名单一条没命中：照常推进 seen，不能当空 feed 清掉。"""
    now = datetime.now(timezone.utc)
    prev = _sec(now - timedelta(minutes=20))
    poll.health.mark_seen(_URL_B, prev)
    await poll.run(poll.source("Mikan-某组", _URL_B, now - timedelta(days=1), []))
    assert poll.health.seen_at(_URL_B) is not None and poll.health.seen_at(_URL_B) > prev


async def test_shared_url_gap_reported_even_when_other_group_matches_nothing(poll):
    """同一个 URL 两组、后一组本组 0 条：断档照报、seen 不被清（清了的话同 URL 的组永远报不出断档）。"""
    now = datetime.now(timezone.utc)
    prev, o1 = _sec(now - timedelta(days=15)), _sec(now - timedelta(days=9))
    poll.health.mark_seen(_URL_A, prev)
    await poll.run(poll.source("ANi-1", _URL_A, o1, ["ok"]), poll.source("ANi-2", _URL_A, o1, []))
    assert poll.gaps == [("ANi-1", prev, o1)]
    assert poll.health.seen_at(_URL_A) is not None


async def test_early_exit_does_not_revive_seen_cleared_by_empty_feed(poll):
    """稀疏 feed 这一轮空（清掉 seen）→ 同一轮后面的组撞 DatabaseBusy：回滚不许把清掉的 seen 放回来，
    否则下一轮它出现的第一条会拿 1 小时前的 seen 去比、误报断档。"""
    import db
    now = datetime.now(timezone.utc)
    poll.health.mark_seen(_URL_Q, _sec(now - timedelta(hours=1)))
    with pytest.raises(db.DatabaseBusy):
        await poll.run(poll.source("新番搜索", _URL_Q, None, []),
                       poll.source("Mikan", _URL_B, now - timedelta(days=1), ["busy"]))
    assert poll.health.seen_at(_URL_Q) is None
    await poll.run(poll.source("新番搜索", _URL_Q, datetime.now(timezone.utc) + timedelta(minutes=15), ["ok"]))
    assert poll.gaps == []


async def test_failure_after_feed_gaps_written_is_not_rolled_back(poll, monkeypatch):
    """收尾的推送抛错时 FEED_GAPS 已写过、seen 已推进：下一轮不许再报同一段断档。"""
    now = datetime.now(timezone.utc)
    prev, o1 = _sec(now - timedelta(days=15)), _sec(now - timedelta(days=9))
    poll.health.mark_seen(_URL_A, prev)

    async def boom(*a, **k):
        raise RuntimeError("推送挂了")
    monkeypatch.setattr(poll.W, "notify_event", boom)
    with pytest.raises(RuntimeError):
        await poll.run(poll.source("ANi", _URL_A, o1, ["ok"]))
    assert sum(1 for w in poll.writes if w.get("FEED_GAPS")) == 1
    poll.gaps.clear()

    async def quiet(*a, **k):
        return None
    monkeypatch.setattr(poll.W, "notify_event", quiet)
    await poll.run(poll.source("ANi", _URL_A, o1, ["ok"]))
    assert poll.gaps == []
    assert sum(1 for w in poll.writes if w.get("FEED_GAPS")) == 1


@pytest.mark.parametrize("how", ["busy_item", "prefetch_raises", "cancelled"])
async def test_early_exit_rolls_seen_back_and_the_gap_is_reported_once_next_round(poll, monkeypatch, how):
    """ANi 有 6 天断档 → 第二组早退 → 本轮什么都不写；下一轮恰好报一次，第三轮不重报。"""
    import db
    now = datetime.now(timezone.utc)
    prev, o1 = _sec(now - timedelta(days=15)), _sec(now - timedelta(days=9))
    prev_b = _sec(now - timedelta(minutes=20))
    poll.health.mark_seen(_URL_A, prev)
    poll.health.mark_seen(_URL_B, prev_b)
    if how == "busy_item":
        with pytest.raises(db.DatabaseBusy):
            await poll.run(poll.source("ANi", _URL_A, o1, ["ok"]),
                           poll.source("Mikan", _URL_B, now - timedelta(days=1), ["busy"]))
    elif how == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await poll.run(poll.source("ANi", _URL_A, o1, ["ok"]),
                           poll.source("Mikan", _URL_B, now - timedelta(days=1), ["cancel"]))
    else:
        real = poll.W.engine.existing_hashes
        calls = {"n": 0}

        def boom(model, hashes):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("existing_hashes 抛了")
            return real(model, hashes)
        monkeypatch.setattr(poll.W.engine, "existing_hashes", boom)
        with pytest.raises(RuntimeError):
            await poll.run(poll.source("ANi", _URL_A, o1, ["ok"]),
                           poll.source("Mikan", _URL_B, now - timedelta(days=1), ["ok"]))
        monkeypatch.setattr(poll.W.engine, "existing_hashes", real)
    assert not any("FEED_GAPS" in w for w in poll.writes)
    assert poll.health.seen_at(_URL_A) == prev, "早退的那一轮把前面组的 seen 留在了抓取时刻"
    assert poll.health.seen_at(_URL_B) == prev_b
    poll.gaps.clear()
    await poll.run(poll.source("ANi", _URL_A, o1, ["ok"]),
                   poll.source("Mikan", _URL_B, now - timedelta(days=1), ["ok"]))
    assert poll.gaps == [("ANi", prev, o1)]
    wrote = [w["FEED_GAPS"] for w in poll.writes if w.get("FEED_GAPS")]
    assert len(wrote) == 1 and len(json.loads(wrote[0])) == 1
    poll.gaps.clear()
    await poll.run(poll.source("ANi", _URL_A, o1, ["ok"]),
                   poll.source("Mikan", _URL_B, now - timedelta(days=1), ["ok"]))
    assert poll.gaps == [], "正常跑完的一轮之后不许重报"
