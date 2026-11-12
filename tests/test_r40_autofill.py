"""后台缺集自动补（autofill_gaps + _backfill_source_inner 的 only_eps）。

钉的是"只补缺的那几集、只收对得上的那一份、判不准就整部不碰"：
  · 条目闸：缺第 5 集时只收目标组本季的第 5 集；不缺的第 6 集、前一部分 / 前几季的同号集、没有发布时间的、
    别的组的（组名按精确相等认，不按子串）、锁了源时联合发布那份、不命中版本关键词的都不入库（入库就算"见过"，缺集提示会被藏掉）；
  · 发布日窗口：按 air_date 推、再拿库里相邻集的实际发布日校准 —— 绑到前一季条目 / air_date 是先行上映日时，
    前一季同号集不收（开播前余量调得再大也封顶在一季之内）；后一季同号集（晚于应播日 + 总集数 周）不收；
    库里见过的集都没有发布时间、集号超出总集数、用户排除过的集不补；
  · 番级 / 全局闸：占槽缺集、编号冲突、歧义段、总集数未知、绑定可疑、有源失败、采集暂停、锁了源且锁定组还没发到最后一集
    （补回的旧集会让之后别组新到的集被『两套编号同步』永久豁免）、别的组有被锁挡住的集或锁定组静默期里有 RSS 断档
    （补回旧集会把锁源兜底的时钟拨回去）→ 整部跳过；
  · 时间闸、24 小时不重试、一次最多 max_anime 部、请求数上界；补到后 confirmed 不变、推一条，下发失败不推；
  · sweep_round：设置为 0 / qB 没开时不调它，采集暂停时连 qB 都不探；它抛异常不传出去。
源的 fetch 打桩返回构造的条目，零网络。
"""
import asyncio
from datetime import date, datetime, timedelta

import pytest
from sqlmodel import select

from core import anime as A
from db.models import Anime, AnimeTorrent

AIR = date.today() - timedelta(days=70)     # 到今天应播到第 11 集，余量 2 周 → 应已播到第 9 集
# 锁了源的番要锁定组已发到最后一集才补（见 autofill_gaps 的番级闸）：锁源用例的锁定组发到第 12 集、只缺第 5 集
TO_END = (1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12)


def _due(k):
    return datetime.combine(AIR, datetime.min.time()) + timedelta(days=7 * (k - 1), hours=12)


def _mk(db, *, title="尼古喵喵", eps=(1, 2, 3, 4, 6, 7, 8, 9), src="ANi", pref=None, kw=None,
        bgm=1, total=12, extra_rows=()):
    with db.get_session() as s:
        a = Anime(title=title, display_name=title, season=1, quarter="26C", confirmed=True, rejected=False,
                  total_episodes=total, air_date=AIR.isoformat(), bangumi_id=bgm, pref_source=pref,
                  pref_keyword=kw)
        s.add(a); s.commit(); s.refresh(a)
        rows = [(src, e, _due(e)) for e in eps] + list(extra_rows)
        for i, (g, e, rel) in enumerate(rows):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{a.id:04x}{9000 + i:036x}", source=g, site="nyaa",
                               anime_title=title, season=1, episode=float(e), status="sent",
                               raw_title=f"[{g}] {title} - {e:02d} [1080p]", release_time=rel,
                               created_at=datetime.now(), priority=100))
        s.commit()
        return a.id


def _item(group, ep, n, released, title="尼古喵喵", tag="1080p"):
    from sources.base import ParsedItem
    return ParsedItem(info_hash=f"{n:040x}", raw_title=f"[{group}] {title} - {ep:02d} [{tag}]",
                      anime_title=title, season=1, episode=float(ep), quarter="26C",
                      release_time=released, download_url=f"https://x/{n}.torrent", source=group,
                      site="nyaa", search_names=[title])


def _env(cfg, monkeypatch, found=()):
    """打桩：搜索返回 found；下载记账并把行落成 sent（autofill 按 HAVE 行认"补回了"）；推送记账。"""
    import sources

    class _Stub:
        def __init__(self, *a, **kw):
            pass

        async def fetch(self, content_cache=None):
            return list(found)

    for site in ("nyaa", "mikan"):
        monkeypatch.setitem(sources.SOURCES, site, _Stub)
        monkeypatch.setitem(sources.SEARCH_URL, site, lambda q: f"https://x/?q={q}")
    sent, pushed = [], []

    async def _fake_dl(tid, force=False, **kw):
        with A.get_session() as s:
            t = s.get(AnimeTorrent, tid)
            t.status = "sent"
            s.add(t); s.commit()
        sent.append(tid)
        return True

    async def _alive():
        return True

    async def _push(ev, msg, **kw):
        pushed.append((ev, msg))
        return True
    monkeypatch.setattr(A, "download_anime_torrent", _fake_dl)
    monkeypatch.setattr(A, "qb_precheck", _alive)
    monkeypatch.setattr(A, "notify_event", _push)
    monkeypatch.setattr(A.health, "failing_sources", lambda: [])
    cfg(ANIME_GAP_AUTOFILL_DAYS=2, ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100,
        ANIME_BACKFILL_PREAIR_DAYS=30, ANIME_BACKFILL_EXTRA_EPS=0, ANIME_POLL_FAILED="",
        ANIME_FINISH_UNSUB=False, ALERT_REPEAT_DAYS=7, ANIME_POLL_ENABLED=True)
    return sent, pushed


def _calls(monkeypatch):
    """把补齐内层换成只记账的桩：番级 / 全局闸的用例只关心"调没调"。"""
    called = []

    async def _inner(aid, name_filter=False, group=None, site=None, only_eps=None):
        called.append((aid, dict(only_eps or {})))
        return {"ingested": 0}
    monkeypatch.setattr(A, "_backfill_source_inner", _inner)
    return called


def _hashes(db, aid):
    with db.get_session() as s:
        return {t.info_hash for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid))}


# ── 条目闸 ───────────────────────────────────────────────────────────

def test_fills_only_the_missing_episode_of_this_season(clean_tables, cfg, monkeypatch):
    """缺第 5 集：目标组本季第 5 集入库并下发；同时搜到的第 6 集（不缺）、前一部分的第 5 集（早于本季首播 4 周，
    开播前余量 30 天挡不住）、早两年的第 5 集、没有发布时间的、别的组的，一条都不入库。补完番仍是已确认，推一条。"""
    a_air = datetime.combine(AIR, datetime.min.time())
    good = _item("ANi", 5, 1, _due(5) + timedelta(hours=1))
    found = [good,
             _item("ANi", 6, 2, _due(6)),                          # 不缺
             _item("ANi", 5, 3, a_air - timedelta(weeks=4)),       # 前一部分的同号集
             _item("ANi", 5, 4, a_air - timedelta(days=730)),      # 前几季的同号集
             _item("ANi", 5, 6, None),                             # 没有发布时间
             _item("NEST", 5, 5, _due(5))]                         # 别的组
    sent, pushed = _env(cfg, monkeypatch, found)
    aid = _mk(clean_tables)
    before = _hashes(clean_tables, aid)
    res = asyncio.run(A.autofill_gaps())
    assert _hashes(clean_tables, aid) - before == {good.info_hash}, res
    assert len(sent) == 1 and res["filled"] == {aid: [5]}, res
    with clean_tables.get_session() as s:
        assert s.get(Anime, aid).confirmed is True
    assert pushed and pushed[0][0] == "gap" and "自动补回了第 5 集" in pushed[0][1], pushed


def test_no_release_time_is_gated_even_without_preair_window(clean_tables, cfg, monkeypatch):
    """开播前余量设成 0（补齐自己的"没时间不收"随之关掉）时，后台照样不收没有发布时间的条目。"""
    sent, pushed = _env(cfg, monkeypatch, [_item("ANi", 5, 1, None)])
    cfg(ANIME_BACKFILL_PREAIR_DAYS=0)
    aid = _mk(clean_tables)
    before = _hashes(clean_tables, aid)
    res = asyncio.run(A.autofill_gaps())
    assert res["tried"] == [aid] and _hashes(clean_tables, aid) == before and not pushed


def test_no_release_time_in_library_means_no_fill(clean_tables, cfg, monkeypatch):
    """库里见过的集都没有发布时间：拿不到相邻集校准，这一集不补（只按 air_date 推会被别季的 air_date 带偏）。"""
    sent, pushed = _env(cfg, monkeypatch, [_item("ANi", 5, 921, _due(5))])
    aid = _mk(clean_tables)
    with clean_tables.get_session() as s:
        for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid)):
            t.release_time = None
            s.add(t)
        s.commit()
    res = asyncio.run(A.autofill_gaps())
    assert res["tried"] == [] and not sent, res


def test_substring_group_is_not_taken_as_target(clean_tables, cfg, monkeypatch):
    """没锁源、库里是 ANi：组名里含 ani 的 Kanimiso（补齐按子串认目标组）同名同集、时间对得上，也不入库。"""
    sent, pushed = _env(cfg, monkeypatch, [_item("Kanimiso", 5, 911, _due(5))])
    aid = _mk(clean_tables)
    before = _hashes(clean_tables, aid)
    res = asyncio.run(A.autofill_gaps())
    assert res["tried"] == [aid] and _hashes(clean_tables, aid) == before and not pushed, (res, pushed)


def test_locked_source_only_takes_the_locked_group_exactly(clean_tables, cfg, monkeypatch):
    """锁『喵萌奶茶屋』：联合发布『喵萌奶茶屋&LoliHouse』那份不入库（入了也不会被下，却会把缺集提示藏掉）。"""
    joint, own = _item("喵萌奶茶屋&LoliHouse", 5, 11, _due(5)), _item("喵萌奶茶屋", 5, 12, _due(5))
    sent, _ = _env(cfg, monkeypatch, [joint, own])
    aid = _mk(clean_tables, src="喵萌奶茶屋", pref="喵萌奶茶屋", eps=TO_END)
    before = _hashes(clean_tables, aid)
    asyncio.run(A.autofill_gaps())
    assert _hashes(clean_tables, aid) - before == {own.info_hash}
    assert len(sent) == 1


def test_version_keyword_miss_is_not_ingested_and_gap_stays(clean_tables, cfg, monkeypatch):
    """设了版本关键词：不命中的那份不入库 —— 后台从不下它，入了库缺集提示却会消失（漏集还没人知道）。"""
    sent, pushed = _env(cfg, monkeypatch, [_item("ANi", 5, 21, _due(5), tag="繁日")])
    aid = _mk(clean_tables, kw="简日")
    before = _hashes(clean_tables, aid)
    asyncio.run(A.autofill_gaps())
    assert _hashes(clean_tables, aid) == before and not sent and not pushed
    with clean_tables.get_session() as s:
        animes = list(s.exec(select(Anime)))
    assert A.episode_gaps(animes) == {aid: [5]}, "缺集提示被藏掉了"


def test_only_eps_none_keeps_manual_backfill_unchanged(clean_tables, cfg, monkeypatch):
    """不传 only_eps（详情页『自动补齐』）：不缺的第 6 集照收，返回值里没有 gated。"""
    sent, _ = _env(cfg, monkeypatch, [_item("ANi", 5, 1, _due(5)), _item("ANi", 6, 2, _due(6))])
    aid = _mk(clean_tables)
    res = asyncio.run(A.backfill_source(aid, name_filter=True))
    assert res["ingested"] == 2 and "gated" not in res, res


# ── 番级 / 全局闸 ───────────────────────────────────────────────────

def test_happy_path_calls_inner_with_only_eps(clean_tables, cfg, monkeypatch):
    """对照组：闸全过时调一次，only_eps = {5: (应播日 − 开播前余量, 应播日 + 总集数 周)}（库里第 4 集按排期发，est = 应播日）。"""
    _env(cfg, monkeypatch)
    called = _calls(monkeypatch)
    aid = _mk(clean_tables)
    asyncio.run(A.autofill_gaps())
    assert called == [(aid, {5: (AIR + timedelta(days=28) - timedelta(days=30), AIR + timedelta(days=28, weeks=12))})]


def test_slot_held_gap_is_not_autofilled(clean_tables, cfg, monkeypatch):
    """第 5 集的槽位被第一季的旧行占着（补回来也被同集闸挡住）→ 不补。bgm 为空，绑定可疑那道闸不起作用。"""
    _env(cfg, monkeypatch)
    called = _calls(monkeypatch)
    old = datetime.combine(AIR, datetime.min.time()) - timedelta(weeks=12 + 26 + 4)
    aid = _mk(clean_tables, bgm=None, extra_rows=[("ANi", 5, old)])
    with clean_tables.get_session() as s:
        animes = list(s.exec(select(Anime)))
    gaps = A.episode_gaps(animes)
    assert gaps == {aid: [5]} and A.slot_held_gaps(gaps) == {aid: [5]}, "前提：第 5 集是占槽缺集"
    asyncio.run(A.autofill_gaps())
    assert called == []


def test_numbering_conflict_skips_the_anime(clean_tables, cfg, monkeypatch):
    _env(cfg, monkeypatch)
    called = _calls(monkeypatch)
    _mk(clean_tables)
    monkeypatch.setattr(A, "episode_numbering_conflict", lambda aid: [13])
    asyncio.run(A.autofill_gaps())
    assert called == []


def test_binding_looks_wrong_skips_the_anime(clean_tables, cfg, monkeypatch):
    _env(cfg, monkeypatch)
    called = _calls(monkeypatch)
    _mk(clean_tables)
    monkeypatch.setattr(A, "binding_looks_wrong", lambda s, a: True)
    asyncio.run(A.autofill_gaps())
    assert called == []


def test_ambiguous_range_or_unknown_total_skips_the_anime(clean_tables, cfg, monkeypatch):
    """歧义段 / 总集数未知时 episode_gaps 本来就不判；这里把缺集打桩成有，钉住 autofill 自己那道闸。"""
    _env(cfg, monkeypatch)
    called = _calls(monkeypatch)
    aid = _mk(clean_tables)
    monkeypatch.setattr(A, "episode_gaps", lambda animes, sets=None: {aid: [5]})
    with clean_tables.get_session() as s:
        a = s.get(Anime, aid)
        a.ep_offset = 4                    # O < T：有歧义段
        s.add(a); s.commit()
    asyncio.run(A.autofill_gaps())
    assert called == []
    with clean_tables.get_session() as s:
        a = s.get(Anime, aid)
        a.ep_offset, a.total_episodes = None, None
        s.add(a); s.commit()
    asyncio.run(A.autofill_gaps())
    assert called == []
    with clean_tables.get_session() as s:
        a = s.get(Anime, aid)
        a.total_episodes, a.air_date = 12, None
        s.add(a); s.commit()
    asyncio.run(A.autofill_gaps())
    assert called == [], "没有 air_date：应播日推不出来"
    with clean_tables.get_session() as s:
        a = s.get(Anime, aid)
        a.air_date = AIR.isoformat()
        s.add(a); s.commit()
    asyncio.run(A.autofill_gaps())
    assert [c[0] for c in called] == [aid], "对照：几道闸都撤掉之后应当补"


def test_failing_or_missed_sources_skip_everything(clean_tables, cfg, monkeypatch):
    _env(cfg, monkeypatch)
    called = _calls(monkeypatch)
    _mk(clean_tables)
    monkeypatch.setattr(A.health, "failing_sources", lambda: [("ANi", 3, "超时")])
    assert asyncio.run(A.autofill_gaps()).get("blocked") and called == []
    monkeypatch.setattr(A.health, "failing_sources", lambda: [])
    cfg(ANIME_POLL_FAILED="ANi")
    assert asyncio.run(A.autofill_gaps()).get("blocked") and called == []


# ── 时间闸、重试、数量 ───────────────────────────────────────────────

def test_waits_until_gap_is_old_enough(clean_tables, cfg, monkeypatch):
    """第 5 集应播日距今 42 天；余量 2 周 + N 天：N=29 时差一天不补，N=28 时补。"""
    _env(cfg, monkeypatch)
    called = _calls(monkeypatch)
    _mk(clean_tables)
    cfg(ANIME_GAP_AUTOFILL_DAYS=29)
    asyncio.run(A.autofill_gaps())
    assert called == []
    cfg(ANIME_GAP_AUTOFILL_DAYS=28)
    asyncio.run(A.autofill_gaps())
    assert len(called) == 1


def test_no_retry_within_24_hours(clean_tables, cfg, monkeypatch):
    _env(cfg, monkeypatch)
    called = _calls(monkeypatch)
    aid = _mk(clean_tables)
    asyncio.run(A.autofill_gaps())
    asyncio.run(A.autofill_gaps())
    assert len(called) == 1
    A._autofill_tried[aid] = datetime.now() - timedelta(hours=25)
    asyncio.run(A.autofill_gaps())
    assert len(called) == 2


def test_at_most_max_anime_and_most_gaps_first(clean_tables, cfg, monkeypatch):
    _env(cfg, monkeypatch)
    called = _calls(monkeypatch)
    one = _mk(clean_tables, title="一")
    three = _mk(clean_tables, title="三", eps=(1, 2, 6, 7, 8, 9))       # 缺 3、4、5
    two = _mk(clean_tables, title="二", eps=(1, 2, 3, 6, 7, 8, 9))      # 缺 4、5
    asyncio.run(A.autofill_gaps(max_anime=2))
    assert [c[0] for c in called] == [three, two]
    assert one not in A._autofill_tried


# ── sweep_round 接线 ────────────────────────────────────────────────

def test_sweep_round_calls_autofill_only_when_enabled(cfg, monkeypatch):
    from core import worker as W
    hits = []

    async def _af(*a, **kw):
        hits.append(1)
        return {}

    alive, probes = [True], []

    async def _alive():
        probes.append(1)
        return alive[0]
    for name in ("sweep_finished", "sweep_idle", "sweep_alerts", "sweep_lock_lag", "sweep_gaps"):
        monkeypatch.setattr(W.anime, name, lambda: 0)
    monkeypatch.setattr(W.engine, "sweep_stale_delivering", lambda: 0)
    monkeypatch.setattr(W.anime, "autofill_gaps", _af)
    monkeypatch.setattr(W.anime, "qb_precheck", _alive)
    cfg(QB_ENABLED=True, ANIME_GAP_AUTOFILL_DAYS=0, ANIME_POLL_ENABLED=True)
    asyncio.run(W.sweep_round())
    assert hits == []
    cfg(ANIME_GAP_AUTOFILL_DAYS=2, ANIME_POLL_ENABLED=False)
    asyncio.run(W.sweep_round())
    assert hits == [] and probes == [], "采集暂停时不探 qB（探了会在 qB 关着时推『连不上』、恢复时再推一条）"
    cfg(ANIME_POLL_ENABLED=True)
    cfg(QB_ENABLED=False, ANIME_GAP_AUTOFILL_DAYS=2)
    asyncio.run(W.sweep_round())
    assert hits == []
    cfg(QB_ENABLED=True)
    alive[0] = False                       # qB 连不上
    asyncio.run(W.sweep_round())
    assert hits == []
    alive[0] = True
    asyncio.run(W.sweep_round())
    assert hits == [1]


# ── 发布日窗口：拿库里相邻集的实际发布日校准 ─────────────────────────

def _mk_at(db, air: date, *, eps, title="尼古喵喵", src="ANi", total=12, bgm=1, pref=None,
           offset=None, raw_ep=lambda e: e, rel=None, air_str=None):
    """建一部番：库里第 e 集默认在 air + 7(e−1) 天中午发布（rel 可覆盖）；air_str 是番上记的 air_date（默认 = air）。"""
    rel = rel or (lambda e: datetime.combine(air, datetime.min.time()) + timedelta(days=7 * (e - 1), hours=12))
    with db.get_session() as s:
        a = Anime(title=title, display_name=title, season=1, quarter="26C", confirmed=True, rejected=False,
                  total_episodes=total, air_date=air_str or air.isoformat(), bangumi_id=bgm, pref_source=pref,
                  ep_offset=offset)
        s.add(a); s.commit(); s.refresh(a)
        for i, e in enumerate(eps):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{a.id:04x}{7000 + i:036x}", source=src, site="nyaa",
                               anime_title=title, season=1, episode=float(e), status="sent",
                               raw_title=f"[{src}] {title} - {raw_ep(e):02d} [1080p]", release_time=rel(e),
                               created_at=datetime.now(), priority=100))
        s.commit()
        return a.id


def _new(db, aid, before):
    with db.get_session() as s:
        return {t.info_hash: t for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid))
                if t.info_hash not in before}


def _gaps_now(db):
    with db.get_session() as s:
        return A.episode_gaps(list(s.exec(select(Anime))))


def test_bound_to_previous_season_entry_does_not_pull_that_seasons_episode(clean_tables, cfg, monkeypatch):
    """绑错季：库里是第二季的种子（10 周前开播），番却绑在第一季的 bgm 条目上（同为 12 集、早一年开播），
    binding_looks_wrong 看不出来。只按 air_date 推，第一季的第 5 集"发布时间正好对得上"；库里第 4、6 集晚一年 → 不收。"""
    real_air = date.today() - timedelta(weeks=10)
    s1_air = real_air - timedelta(weeks=52)
    s1_ep5 = _item("ANi", 5, 321, datetime.combine(s1_air, datetime.min.time()) + timedelta(weeks=4, hours=12))
    sent, pushed = _env(cfg, monkeypatch, [s1_ep5])
    aid = _mk_at(clean_tables, real_air, eps=(1, 2, 3, 4, 6, 7, 8, 9, 10), air_str=s1_air.isoformat())
    with clean_tables.get_session() as s:
        assert not A.binding_looks_wrong(s, s.get(Anime, aid)), "前提：绑定可疑那道闸看不出来"
    assert _gaps_now(clean_tables) == {aid: [5]}
    before = _hashes(clean_tables, aid)
    res = asyncio.run(A.autofill_gaps())
    assert not _new(clean_tables, aid, before) and not sent and not pushed, (res, pushed)
    assert res["tried"] == [], "两种推法差得远时窗口为空，连搜都不该去搜"


def test_early_air_date_does_not_let_previous_cour_in(clean_tables, cfg, monkeypatch):
    """air_date 是先行上映日（比电视首播早 13 周），前一季紧挨着在上一个季度播：前一季第 5 集落在只按 air_date 推的窗口里。"""
    real_air = date.today() - timedelta(weeks=10)
    bgm_air = prev_air = real_air - timedelta(weeks=13)
    prev_ep5 = _item("ANi", 5, 311, datetime.combine(prev_air, datetime.min.time()) + timedelta(weeks=4, hours=12))
    sent, pushed = _env(cfg, monkeypatch, [prev_ep5])
    aid = _mk_at(clean_tables, real_air, eps=(1, 2, 3, 4, 6, 7, 8, 9, 10), air_str=bgm_air.isoformat())
    assert _gaps_now(clean_tables) == {aid: [5]}
    before = _hashes(clean_tables, aid)
    res = asyncio.run(A.autofill_gaps())
    assert not _new(clean_tables, aid, before) and not pushed, (res, pushed)


def test_later_season_same_number_is_not_filled(clean_tables, cfg, monkeypatch):
    """第一季 40 周前开播、缺第 5 集；第二季（同组、同标题、不写季号）26 周后开播。搜索（新的在前）同时回来
    第二季的第 5 集与第一季真正的第 5 集 —— 只收后者。"""
    air = date.today() - timedelta(weeks=40)
    at = datetime.combine(air, datetime.min.time())
    s2_ep5 = _item("ANi", 5, 101, at + timedelta(weeks=26 + 4, hours=12))
    s1_ep5 = _item("ANi", 5, 102, at + timedelta(weeks=4, hours=12))
    sent, pushed = _env(cfg, monkeypatch, [s2_ep5, s1_ep5])
    aid = _mk_at(clean_tables, air, eps=(1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12))
    before = _hashes(clean_tables, aid)
    res = asyncio.run(A.autofill_gaps())
    assert set(_new(clean_tables, aid, before)) == {s1_ep5.info_hash} and res["filled"] == {aid: [5]}, res


def test_later_season_only_is_neither_filled_nor_announced(clean_tables, cfg, monkeypatch):
    """搜索只回来后一季的第 5 集（第一季那份已滚出第一页）：不入库、不下、不推『补回了』。"""
    air = date.today() - timedelta(weeks=60)
    at = datetime.combine(air, datetime.min.time())
    sent, pushed = _env(cfg, monkeypatch, [_item("ANi", 5, 111, at + timedelta(weeks=52 + 4, hours=12))])
    aid = _mk_at(clean_tables, air, eps=(1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12))
    before = _hashes(clean_tables, aid)
    res = asyncio.run(A.autofill_gaps())
    assert not _new(clean_tables, aid, before) and not sent and not pushed, (res, pushed)


def test_foreign_marked_later_season_row_is_not_refetched(clean_tables, cfg, monkeypatch):
    """用户把库里那条第 5 集标成『不属于本季』（它是后一季的）；搜到同一集的另一个 hash，不该下回来占住第 5 集。"""
    air = date.today() - timedelta(weeks=40)
    at = datetime.combine(air, datetime.min.time())
    late = at + timedelta(weeks=30, hours=12)
    sent, pushed = _env(cfg, monkeypatch, [_item("ANi", 5, 421, late + timedelta(days=1))])
    aid = _mk_at(clean_tables, air, eps=tuple(range(1, 13)),
                 rel=lambda e: late if e == 5 else at + timedelta(days=7 * (e - 1), hours=12))
    with clean_tables.get_session() as s:
        t = s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid, AnimeTorrent.episode == 5.0)).one()
        t.foreign_season = True
        s.add(t); s.commit()
    assert _gaps_now(clean_tables) == {aid: [5]}
    res = asyncio.run(A.autofill_gaps())
    assert not sent and not pushed, (res, pushed)


def test_next_season_right_after_is_not_filled(clean_tables, cfg, monkeypatch):
    """下一季紧接着播（同组同标题、季内编号）：它的第 5 集恰在本季第 5 集应播日 + 12 周那天发 —— 窗口上界不含这一天。"""
    air = date.today() - timedelta(weeks=40)
    at = datetime.combine(air, datetime.min.time())
    sent, pushed = _env(cfg, monkeypatch, [_item("ANi", 5, 121, at + timedelta(weeks=12 + 4, hours=12))])
    aid = _mk_at(clean_tables, air, eps=(1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12))
    before = _hashes(clean_tables, aid)
    asyncio.run(A.autofill_gaps())
    assert not _new(clean_tables, aid, before) and not sent


def test_late_air_date_upper_bound_follows_actual_schedule(clean_tables, cfg, monkeypatch):
    """air_date 比组实际发布晚 4 周（组跟的是更早的网络首播）：上界取两种推法里早的那个 —— 按实际排期，
    紧接着播的下一季第 5 集（实际首播 + 16 周）不收；本季真正的第 5 集照收。"""
    real_air = date.today() - timedelta(weeks=30)
    at = datetime.combine(real_air, datetime.min.time())
    nxt = _item("ANi", 5, 131, at + timedelta(weeks=16, hours=12))
    good = _item("ANi", 5, 132, at + timedelta(weeks=4, hours=13))
    sent, pushed = _env(cfg, monkeypatch, [nxt, good])
    aid = _mk_at(clean_tables, real_air, eps=(1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12),
                 air_str=(real_air + timedelta(weeks=4)).isoformat())
    assert _gaps_now(clean_tables) == {aid: [5]}
    before = _hashes(clean_tables, aid)
    res = asyncio.run(A.autofill_gaps())
    assert set(_new(clean_tables, aid, before)) == {good.info_hash}, res


def test_release_on_the_earliest_day_is_accepted(clean_tables, cfg, monkeypatch):
    """窗口下界含当天：发布日期恰等于『应播日 − 开播前余量』的要收。"""
    edge = datetime.combine(AIR + timedelta(days=28 - 30), datetime.min.time()) + timedelta(hours=3)
    sent, pushed = _env(cfg, monkeypatch, [_item("ANi", 5, 51, edge)])
    aid = _mk(clean_tables)
    assert asyncio.run(A.autofill_gaps())["filled"] == {aid: [5]}


# ── 集级：超出总集数、用户处理过的集 ─────────────────────────────────

def test_absolute_branch_gap_beyond_total_is_not_filled(clean_tables, cfg, monkeypatch):
    """第二季（bgm 12 集）库里是绝对号 14..24、真缺第 13 集；绝对编号分支只能数个数，报成"缺第 25 集"。
    后台不该把第三季第 1 集（- 25）当成本季缺的集补进来。"""
    air = date.today() - timedelta(weeks=40)
    at = datetime.combine(air, datetime.min.time())
    sent, pushed = _env(cfg, monkeypatch, [_item("ANi", 25, 201, datetime.now() - timedelta(weeks=5))])
    aid = _mk_at(clean_tables, air, eps=tuple(range(14, 25)), rel=lambda e: at + timedelta(days=7 * (e - 13), hours=12))
    assert _gaps_now(clean_tables) == {aid: [25]} and not A.episode_numbering_conflict(aid), "前提"
    before = _hashes(clean_tables, aid)
    res = asyncio.run(A.autofill_gaps())
    assert not _new(clean_tables, aid, before) and not pushed and res["tried"] == [], (res, pushed)


def test_excluded_only_copy_is_not_refilled(clean_tables, cfg, monkeypatch):
    """第 5 集唯一的一条被用户排除：缺集提示照报，但后台不拿同组的另一个 hash 把它下回来（用户处理过的集不替他做主）。"""
    sent, pushed = _env(cfg, monkeypatch, [_item("ANi", 5, 411, _due(5) + timedelta(hours=3))])
    aid = _mk(clean_tables, eps=(1, 2, 3, 4, 5, 6, 7, 8, 9))
    with clean_tables.get_session() as s:
        t = s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid, AnimeTorrent.episode == 5.0)).one()
        t.status = "excluded"
        s.add(t); s.commit()
    assert _gaps_now(clean_tables) == {aid: [5]}
    res = asyncio.run(A.autofill_gaps())
    assert not sent and not pushed and res["tried"] == [], res


# ── 条目闸：集号按归一后的算 ─────────────────────────────────────────

def test_learned_offset_absolute_item_is_normalized_then_matched(clean_tables, cfg, monkeypatch):
    """O=12、T=12（可折）：库里是折过的 1..4、6..9（标题写 13..21）。目标组的『- 17』按归一后的 5 收；
    同组第一季的『- 05』（一年前）、下一季的『- 29』不收；ep_offset 不变。"""
    air = date.today() - timedelta(days=70)
    at = datetime.combine(air, datetime.min.time())
    good = _item("ANi", 17, 501, at + timedelta(weeks=4, hours=12))
    s1 = _item("ANi", 5, 502, at - timedelta(weeks=52))
    s3 = _item("ANi", 29, 503, at + timedelta(weeks=5))
    sent, pushed = _env(cfg, monkeypatch, [s1, s3, good])
    aid = _mk_at(clean_tables, air, eps=(1, 2, 3, 4, 6, 7, 8, 9), offset=12, raw_ep=lambda e: e + 12)
    before = _hashes(clean_tables, aid)
    res = asyncio.run(A.autofill_gaps())
    new = _new(clean_tables, aid, before)
    assert set(new) == {good.info_hash} and new[good.info_hash].episode == 5.0 and res["filled"] == {aid: [5]}, res
    with clean_tables.get_session() as s:
        assert s.get(Anime, aid).ep_offset == 12


def test_half_episode_is_not_taken_as_the_integer(clean_tables, cfg, monkeypatch):
    """总集篇『05.5』不是第 5 集。"""
    half = _item("ANi", 5, 511, _due(5) + timedelta(days=3))
    half.episode, half.raw_title = 5.5, "[ANi] 尼古喵喵 - 05.5 [1080p]"
    sent, pushed = _env(cfg, monkeypatch, [half])
    aid = _mk(clean_tables)
    before = _hashes(clean_tables, aid)
    asyncio.run(A.autofill_gaps())
    assert _hashes(clean_tables, aid) == before and not sent


# ── 抓取期间的变化：按入库那一刻的锁 / 订阅态 ────────────────────────

def _flip_during_fetch(monkeypatch, found, change):
    import sources

    class _Flip:
        def __init__(self, *a, **kw):
            pass

        async def fetch(self, content_cache=None):
            change()
            return list(found)
    for site in ("nyaa", "mikan"):
        monkeypatch.setitem(sources.SOURCES, site, _Flip)


def test_lock_changed_during_fetch_is_not_ingested(clean_tables, cfg, monkeypatch):
    """抓取期间用户把锁定源从 ANi 改成 NEST：ANi 的第 5 集不入库（入了也会被放行前复核挡在待下、缺集提示却消失）。"""
    it = _item("ANi", 5, 701, _due(5))
    sent, pushed = _env(cfg, monkeypatch)
    aid = _mk(clean_tables, pref="ANi", eps=TO_END)

    def _change():
        with clean_tables.get_session() as s:
            a = s.get(Anime, aid)
            a.pref_source = "NEST"
            s.add(a); s.commit()
    _flip_during_fetch(monkeypatch, [it], _change)
    before = _hashes(clean_tables, aid)
    asyncio.run(A.autofill_gaps())
    assert _hashes(clean_tables, aid) == before and _gaps_now(clean_tables) == {aid: [5]}


def test_unsubscribed_during_fetch_is_not_ingested(clean_tables, cfg, monkeypatch):
    """抓取期间这部番下齐停订了：不入库，也不把番打回待确认。"""
    sent, pushed = _env(cfg, monkeypatch)
    cfg(ANIME_FINISH_UNSUB=True)
    aid = _mk(clean_tables)

    def _change():
        with clean_tables.get_session() as s:
            a = s.get(Anime, aid)
            a.finished_at = datetime.now()
            s.add(a); s.commit()
    _flip_during_fetch(monkeypatch, [_item("ANi", 5, 702, _due(5))], _change)
    before = _hashes(clean_tables, aid)
    res = asyncio.run(A.autofill_gaps())
    assert res["tried"] == [aid] and _hashes(clean_tables, aid) == before
    with clean_tables.get_session() as s:
        assert s.get(Anime, aid).confirmed is True


def test_finished_unsubscribed_anime_is_not_tried(clean_tables, cfg, monkeypatch):
    """下齐停订的番不在对象里。"""
    _env(cfg, monkeypatch)
    called = _calls(monkeypatch)
    cfg(ANIME_FINISH_UNSUB=True)
    aid = _mk(clean_tables)
    with clean_tables.get_session() as s:
        a = s.get(Anime, aid)
        a.finished_at = datetime.now()
        s.add(a); s.commit()
    asyncio.run(A.autofill_gaps())
    assert called == []


# ── 全局闸：采集暂停 ─────────────────────────────────────────────────

def test_paused_collection_blocks_autofill(clean_tables, cfg, monkeypatch):
    """采集总开关关着：flush / 修正版 / 锁源兜底都随采集停了，后台补缺也不去搜种子站、不下发。"""
    _env(cfg, monkeypatch)
    called = _calls(monkeypatch)
    _mk(clean_tables)
    cfg(ANIME_POLL_ENABLED=False)
    res = asyncio.run(A.autofill_gaps())
    assert called == [] and res.get("blocked") == "采集已暂停"


# ── 锁了源的番：别动锁源兜底的时钟 ───────────────────────────────────

def _locked_with_others(db, lock, created):
    """锁 lock、锁定组发了 1..4、7..12（跳过第 6 集，入库于 created）；别的组 NEST 有 4、6、8..12 待下（入库于 20 天前；
    第 4 集是兜底发布时间单调闸的锚）。
    缺第 5 集；NEST 的第 6 集是锁定组中间跳过、被锁挡住的集 —— 锁定组已发到最后一集，只剩 locked_out_candidates 那道闸挡着。"""
    aid = _mk(db, src=lock, pref=lock, eps=(1, 2, 3, 4, 7, 8, 9, 10, 11, 12))
    old = datetime.now() - timedelta(days=20)
    with db.get_session() as s:
        for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid)):
            t.created_at = created
            s.add(t)
        for e in (4, 6, 8, 9, 10, 11, 12):
            s.add(AnimeTorrent(anime_id=aid, info_hash=f"{0xabc000 + e:040x}", source="NEST", site="nyaa",
                               anime_title="尼古喵喵", season=1, episode=float(e), status="pending",
                               raw_title=f"[NEST] 尼古喵喵 - {e:02d} [1080p]", release_time=_due(e),
                               created_at=old, priority=50))
        s.commit()
    return aid


def test_autofill_does_not_reset_lock_fallback_clock(clean_tables, cfg, monkeypatch):
    """锁定组 30 天没入库、跳过的第 6 集 NEST 有，兜底这一轮就该补。后台补回锁定组的第 5 集会让
    『锁定组最新入库』变成今天、兜底再推迟 N 天 —— 这类番后台不碰。"""
    lock = "喵萌奶茶屋"
    sent, pushed = _env(cfg, monkeypatch, [_item(lock, 5, 31, _due(5))])
    cfg(ANIME_LOCK_FALLBACK_DAYS=14, ANIME_LOCK_LAG_DAYS=7, ANIME_LOCK_SYNC_DAYS=2)
    aid = _locked_with_others(clean_tables, lock, datetime.now() - timedelta(days=30))
    assert _gaps_now(clean_tables) == {aid: [5]}
    assert list(A.lock_fallback_plan()[aid]["eps"]) == [6], "前提：兜底本来这一轮就该补第 6 集"
    res = asyncio.run(A.autofill_gaps())
    assert res["tried"] == [] and not sent
    assert list(A.lock_fallback_plan()[aid]["eps"]) == [6]


def test_autofill_does_not_open_the_feed_gap_gate(clean_tables, cfg, monkeypatch):
    """锁定组静默期里有没点『知道了』的 RSS 断档时兜底只报不改；后台补回一集会让那段断档移出［锁定组最新入库, 现在］。"""
    import json
    lock = "喵萌奶茶屋"
    sent, pushed = _env(cfg, monkeypatch, [_item(lock, 5, 31, _due(5))])
    now = datetime.now()
    gap = {"src": "nyaa", "from": (now - timedelta(days=25)).isoformat(timespec="minutes"),
           "to": (now - timedelta(days=22)).isoformat(timespec="minutes"), "at": now.isoformat(timespec="minutes")}
    cfg(ANIME_LOCK_FALLBACK_DAYS=14, ANIME_LOCK_LAG_DAYS=7, ANIME_LOCK_SYNC_DAYS=2, FEED_GAPS=json.dumps([gap]))
    aid = _locked_with_others(clean_tables, lock, now - timedelta(days=30))
    later = now + timedelta(days=15)
    assert A.lock_fallback_plan(today=later) == {}, "前提：断档闸挡着"
    asyncio.run(A.autofill_gaps())
    assert A.lock_fallback_plan(today=later) == {} and not sent and aid


def test_locked_with_feed_gap_in_silence_is_skipped(clean_tables, cfg, monkeypatch):
    """别的组还没有被锁挡住的集，但锁定组静默期里有 RSS 断档：同样不碰（补回旧集会把断档挪出兜底的窗口）。没有断档时照补。"""
    import json
    _env(cfg, monkeypatch)
    called = _calls(monkeypatch)
    lock = "喵萌奶茶屋"
    now = datetime.now()
    aid = _mk(clean_tables, src=lock, pref=lock, eps=TO_END)
    with clean_tables.get_session() as s:
        for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid)):
            t.created_at = now - timedelta(days=20)
            s.add(t)
        s.commit()
    assert not A.locked_out_candidates(aid)
    gap = {"src": "nyaa", "from": (now - timedelta(days=10)).isoformat(timespec="minutes"),
           "to": (now - timedelta(days=8)).isoformat(timespec="minutes"), "at": now.isoformat(timespec="minutes")}
    cfg(FEED_GAPS=json.dumps([gap]))
    asyncio.run(A.autofill_gaps())
    assert called == []
    cfg(FEED_GAPS="[]")
    asyncio.run(A.autofill_gaps())
    assert [c[0] for c in called] == [aid]


def _lock_stopped_at(db, cfg, monkeypatch, last: int, autofill: bool):
    """锁定组发到第 last 集（跳过第 5 集）后停更，10 天前最后一次入库；NEST 一直跟着发，last−1、last 已在库
    （不在锁定组之后，不算被锁挡住）。可选地先跑一轮后台补缺，然后第二天 NEST 的第 last+1 集到了。"""
    lock = "喵萌奶茶屋"
    _env(cfg, monkeypatch, [_item(lock, 5, 31, _due(5))])
    cfg(ANIME_LOCK_FALLBACK_DAYS=14, ANIME_LOCK_LAG_DAYS=7, ANIME_LOCK_SYNC_DAYS=2)
    now = datetime.now()
    aid = _mk(db, src=lock, pref=lock, eps=[e for e in range(1, last + 1) if e != 5])
    with db.get_session() as s:
        for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid)):
            t.created_at = now - timedelta(days=10)
            s.add(t)
        for e in (last - 1, last):
            s.add(AnimeTorrent(anime_id=aid, info_hash=f"{0xabc000 + e:040x}", source="NEST", site="nyaa",
                               anime_title="尼古喵喵", season=1, episode=float(e), status="pending",
                               raw_title=f"[NEST] 尼古喵喵 - {e:02d} [1080p]", release_time=_due(e),
                               created_at=now - timedelta(days=10), priority=50))
        s.commit()
    assert not A.locked_out_candidates(aid), "前提：此刻别的组没有被锁挡住的集"
    res = asyncio.run(A.autofill_gaps()) if autofill else None
    with db.get_session() as s:
        s.add(AnimeTorrent(anime_id=aid, info_hash=f"{0xabc100 + last:040x}", source="NEST", site="nyaa",
                           anime_title="尼古喵喵", season=1, episode=float(last + 1), status="pending",
                           raw_title=f"[NEST] 尼古喵喵 - {last + 1:02d} [1080p]", release_time=_due(last + 1),
                           created_at=now + timedelta(days=1), priority=50))
        s.commit()
    with db.get_session() as s:
        animes = list(s.exec(select(Anime)))
    later = now + timedelta(days=20)
    return aid, res, A.locked_out_episodes(animes, later), A.lock_fallback_plan(today=later)


@pytest.mark.parametrize("last", [9, 11])
def test_lock_lag_control_without_autofill(clean_tables, cfg, monkeypatch, last):
    """对照：不跑后台补缺，20 天后 NEST 的第 last+1 集照报锁源落后、照进兜底。"""
    aid, _, lag, plan = _lock_stopped_at(clean_tables, cfg, monkeypatch, last, autofill=False)
    assert lag.get(aid, {}).get("eps") == [last + 1] and list(plan[aid]["eps"]) == [last + 1]


@pytest.mark.parametrize("last", [9, 11])
def test_autofill_does_not_poison_lock_sync_exemption(clean_tables, cfg, monkeypatch, last):
    """锁定组还没发到最后一集（含只差一集）：后台不补它的旧集。补回的第 5 集入库时间是今天，第二天 NEST 的
    第 last+1 集（与 5 差 > 2）会被『两套编号同步在发』永久豁免 —— 锁定组就此停更时那一集既不提醒也不兜底。"""
    aid, res, lag, plan = _lock_stopped_at(clean_tables, cfg, monkeypatch, last, autofill=True)
    assert res["tried"] == [], res
    assert lag.get(aid, {}).get("eps") == [last + 1], "锁源落后提醒把这一集豁免掉了"
    assert list(plan.get(aid, {}).get("eps", {})) == [last + 1], "锁源兜底永远不补这一集"


# ── 开播前余量封顶 ───────────────────────────────────────────────────

def test_preair_margin_is_capped_below_one_season(clean_tables, cfg, monkeypatch):
    """开播前余量调到 100 天：前一季（同组同名、紧挨着播完）的第 5 集早应播日 12 周，不收；本季第 5 集照收。
    余量封顶在 7×总集数−1 天，最早那头刚好晚前一季同号集一天。"""
    prev = _item("ANi", 5, 951, _due(5) - timedelta(weeks=12))
    good = _item("ANi", 5, 952, _due(5) + timedelta(hours=1))
    sent, pushed = _env(cfg, monkeypatch, [prev, good])
    cfg(ANIME_BACKFILL_PREAIR_DAYS=100)
    aid = _mk(clean_tables)
    before = _hashes(clean_tables, aid)
    res = asyncio.run(A.autofill_gaps())
    assert _hashes(clean_tables, aid) - before == {good.info_hash}, res


# ── 请求数、补搜范围 ─────────────────────────────────────────────────

def _count_env(cfg, monkeypatch, found=(), fail=False):
    """同 _env，但每次搜索记一条 URL；fail=True 时每次搜索都抛（站挂了 / 被限流）。"""
    sent, pushed = _env(cfg, monkeypatch, found)
    import sources
    urls = []

    class _Stub:
        def __init__(self, name, url, **kw):
            self.url = url

        async def fetch(self, content_cache=None):
            urls.append(self.url)
            if fail:
                raise RuntimeError("429 Too Many Requests")
            return list(found)

    for site in ("nyaa", "mikan"):
        monkeypatch.setitem(sources.SOURCES, site, _Stub)
        monkeypatch.setitem(sources.SEARCH_URL, site, lambda q, _s=site: f"https://{_s}/?q={q}")
    return sent, pushed, urls


def test_request_count_upper_bound(clean_tables, cfg, monkeypatch):
    """最坏情形：搜索名凑满 5 个、缺 ≥4 集、补搜 4 集 → 一部番 2 站 ×(5 + 2×4) = 26 次，一轮最多 3 部 = 78 次；
    试过的番 24 小时内不再发请求。"""
    sent, pushed, urls = _count_env(cfg, monkeypatch)
    cfg(ANIME_BACKFILL_EXTRA_EPS=4)
    for n, t in enumerate(("甲番", "乙番", "丙番", "丁番")):
        aid = _mk(clean_tables, title=t, eps=(1, 2, 8, 9), bgm=10 + n)       # 缺 3..7
        with clean_tables.get_session() as s:
            a = s.get(Anime, aid)
            a.jp_name, a.display_name = f"{t}ジャパン", f"{t}展示名"
            rows = list(s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid)
                               .order_by(AnimeTorrent.id.desc())))
            for i, r in enumerate(rows[:3]):          # 组中途改名：最近 3 条各一个名字 → 搜索名 3 + jp + dn = 5
                r.raw_title = f"[ANi] {t}别名{chr(0x58 + i)} - {int(r.episode):02d} [1080p]"
                r.created_at = datetime.now() + timedelta(seconds=10 - i)
                s.add(r)
            s.add(a); s.commit()
    res = asyncio.run(A.autofill_gaps())
    assert len(res["tried"]) == 3 and len(urls) == 3 * 2 * (5 + 2 * 4), (res, len(urls))
    res = asyncio.run(A.autofill_gaps())
    assert len(res["tried"]) == 1 and len(urls) == 78 + 26, "第二轮只轮到第 4 部"
    asyncio.run(A.autofill_gaps())
    assert len(urls) == 78 + 26, "4 部都在 24 小时内试过，第三轮 0 次"


def test_targeted_search_only_for_only_eps(clean_tables, cfg, monkeypatch):
    """"番名 集号"补搜只搜 only_eps 里的集：第 1~4 集是占槽缺集（一定被挡），额度不该花在它们身上而轮不到第 7 集。"""
    sent, pushed, urls = _count_env(cfg, monkeypatch)
    cfg(ANIME_BACKFILL_EXTRA_EPS=4)
    old = datetime.combine(AIR, datetime.min.time()) - timedelta(weeks=12 + 26 + 4)
    aid = _mk(clean_tables, bgm=None, eps=(5, 6, 8, 9, 10), extra_rows=[("ANi", e, old) for e in (1, 2, 3, 4)])
    res = asyncio.run(A.autofill_gaps())
    targeted = sorted({u.rsplit(" ", 1)[-1] for u in urls if u[-3:-2] == " "})
    assert res["tried"] == [aid] and targeted == ["07"], (res, targeted)


# ── 下发失败、异常 ───────────────────────────────────────────────────

@pytest.mark.parametrize("ret", [False, None])
def test_no_push_when_delivery_fails(clean_tables, cfg, monkeypatch, ret):
    """入库了但没下到（qB 拒收 False / 系统性失败 None）：不推『补回了』，filled 为空。"""
    sent, pushed = _env(cfg, monkeypatch, [_item("ANi", 5, 1, _due(5) + timedelta(hours=1))])

    async def _dl(tid, force=False, **kw):
        return ret
    monkeypatch.setattr(A, "download_anime_torrent", _dl)
    aid = _mk(clean_tables)
    before = _hashes(clean_tables, aid)
    res = asyncio.run(A.autofill_gaps())
    assert len(_hashes(clean_tables, aid) - before) == 1, "前提：入库了"
    assert res["tried"] == [aid] and not pushed and res["filled"] == {}, res


def test_inner_exception_is_recorded_and_not_retried(clean_tables, cfg, monkeypatch):
    """补齐内层抛异常：这部番照样记 24 小时账，下一轮巡检不重打种子站。"""
    _env(cfg, monkeypatch)
    calls = []

    async def _boom(aid, **kw):
        calls.append(aid)
        raise RuntimeError("boom")
    monkeypatch.setattr(A, "_backfill_source_inner", _boom)
    aid = _mk(clean_tables)
    for _ in range(3):
        try:
            asyncio.run(A.autofill_gaps())
        except RuntimeError:
            pass
    assert calls == [aid]


def test_sweep_round_survives_autofill_exception(cfg, monkeypatch):
    """缺集自动补抛异常不传出 sweep_round（巡检协程不能因此死掉），记一条巡检错误。"""
    from core import worker as W
    errs = []

    async def _af(*a, **kw):
        raise RuntimeError("boom")

    async def _alive():
        return True
    for name in ("sweep_finished", "sweep_idle", "sweep_alerts", "sweep_lock_lag", "sweep_gaps"):
        monkeypatch.setattr(W.anime, name, lambda: 0)
    monkeypatch.setattr(W.engine, "sweep_stale_delivering", lambda: 0)
    monkeypatch.setattr(W.anime, "autofill_gaps", _af)
    monkeypatch.setattr(W.anime, "qb_precheck", _alive)
    monkeypatch.setattr(W, "loop_error", lambda what, e, **kw: errs.append(what))
    cfg(QB_ENABLED=True, ANIME_GAP_AUTOFILL_DAYS=2, ANIME_POLL_ENABLED=True)
    asyncio.run(W.sweep_round())
    assert errs == ["巡检·缺集自动补 "]
