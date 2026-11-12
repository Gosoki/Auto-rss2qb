"""版本关键词把新集挡住时要有人说话（keyword_blocked_episodes 及其三个出口）。

pref_keyword 在 flush / 补下里是硬过滤、不兜底。字幕组中途改了标签写法（简日 → CHS）之后后面的集一集都不下，
而缺集（有种子＝见过）、锁源落后（只看锁定组之外的行）、断更（一直有新种子）都看不见，仪表盘还把它们算进『备用项』。
这组用例钉的是：
  · 判据：探针场景（1~3 集简日已下、4~7 集 CHS 入库已久）必须报；命中版本只是晚到（未满天数）、该集已有一份 /
    已删 / 有排除过的行、没设关键词、『不属于本季』的、占槽的别季旧行、两套编号同步在发的都不报；
    锁了源时只看锁定组的行；0 天 = 关；
  · 出口：巡检推一条 gap、同一批第二轮不重复、挡住的集变了再推；详情页有横幅；仪表盘把这些行从『备用项』挪进
    『锁源缺集』且各桶之和不变，点开的列表点得到这部番，新入库列表的逐条徽标与它同口径；
  · 只报不改：下载计划仍然是空的。
"""
import asyncio
from datetime import date, datetime, timedelta

import pytest
from sqlmodel import select

import services.notify as N
from core import anime as A
from db.models import Anime, AnimeTorrent

NOW = datetime.now()


def _mk(db, rows, *, kw="简日", lock=None, confirmed=True, title="关键词番", total=12, offset=None):
    """建一部番。rows = [(集号, 组, 标签, 状态, 几天前入库) 或带第 6 项额外列 dict]（发布时间默认 = 入库时间）。"""
    with db.get_session() as s:
        a = Anime(title=title, display_name=title, season=1, quarter="26D", confirmed=confirmed,
                  rejected=False, bangumi_id=9001, total_episodes=total, ep_offset=offset,
                  air_date=(date.today() - timedelta(weeks=7)).isoformat(),
                  pref_keyword=kw, pref_source=lock)
        s.add(a)
        s.commit()
        s.refresh(a)
        for i, r in enumerate(rows):
            ep, src, tag, st, days = r[:5]
            extra = r[5] if len(r) > 5 else {}
            at = NOW - timedelta(days=days)
            cols = {"release_time": at, "created_at": at, **extra}
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{a.id * 1000 + i:040x}", source=src, site="nyaa",
                               anime_title=title, raw_title=f"[{src}] {title} - {ep:02} [{tag}]", season=1,
                               episode=float(ep), quarter="26D", status=st, priority=100,
                               qb_progress=1.0 if st == "sent" else 0.0, **cols))
        s.commit()
        return a.id


def _flow_s1(db, **kw):
    """flow 探针 S1：关键词=简日，1~3 集简日已下，4~7 集组改标签成 CHS、入库 28~7 天。"""
    rows = [(e, "喵萌", "简日", "sent", 49 - 7 * (e - 1)) for e in (1, 2, 3)]
    rows += [(e, "喵萌", "CHS", "pending", 49 - 7 * (e - 1)) for e in (4, 5, 6, 7)]
    return _mk(db, rows, **kw)


def _animes(db):
    with db.get_session() as s:
        out = list(s.exec(select(Anime)))
        for a in out:
            s.expunge(a)
        return out


@pytest.fixture
def lag7(cfg):
    cfg(ANIME_LOCK_LAG_DAYS=7, ANIME_FINISH_UNSUB=False, ANIME_LOCK_SYNC_DAYS=2)


# ==================== 判据 ====================

def test_flow_probe_scenario_is_reported_and_nothing_else_sees_it(clean_tables, lag7):
    aid = _flow_s1(clean_tables)
    animes = _animes(clean_tables)
    assert A.keyword_blocked_episodes(animes) == {aid: [4, 5, 6, 7]}
    # 前提：别的巡检确实看不见、下载计划确实为空（只报不改：这里不动它）
    assert A.episode_gaps(animes) == {} and A.locked_out_episodes(animes) == {}
    assert not A.download_plan(aid)


def test_matching_version_merely_late_is_not_reported(clean_tables, lag7):
    """CHS 才来 3 天（想要的版本可能只是晚几天）不报；CHS 早来了、简日版晚到但已在待下 → 有命中，不报。"""
    rows = [(4, "喵萌", "CHS", "pending", 3),
            (5, "喵萌", "CHS", "pending", 20), (5, "喵萌", "简日", "pending", 1)]
    _mk(clean_tables, rows)
    assert A.keyword_blocked_episodes(_animes(clean_tables)) == {}


def test_lag_is_measured_from_the_earliest_pending_row(clean_tables, lag7):
    """同一集一条早、一条晚：按最早那条算天数（晚到的重发不该把提醒往后推）。"""
    aid = _mk(clean_tables, [(4, "喵萌", "CHS", "pending", 20), (4, "喵萌", "CHS v2", "pending", 1)])
    assert A.keyword_blocked_episodes(_animes(clean_tables)) == {aid: [4]}


def test_covered_dropped_or_excluded_is_not_reported(clean_tables, lag7):
    """第 4 集已有一份（别的组的 sent，看全部来源）；第 5 集该集已删（ep_dropped）；
    第 6 集命中关键词的那条被排除了；第 8 集排除的是不命中的那条、另一条不命中的仍待下 ——
    该集有排除过的行就是用户对这一集做过决定，不报（也是让某一集的提醒停下来的办法）；第 7 集是对照，要报。
    各集入库隔 4 天（大于同步窗口），免得命中行把别的集按"两套编号同步在发"豁免掉、掩盖这里要钉的判据。"""
    aid = _mk(clean_tables, [
        (4, "喵萌", "CHS", "pending", 20), (4, "ANi", "CHT", "sent", 20),
        (5, "喵萌", "CHS", "pending", 24), (5, "喵萌", "简日", "deleted", 24, {"ep_dropped": True}),
        (6, "喵萌", "CHS", "pending", 28), (6, "喵萌", "简日", "excluded", 28),
        (7, "喵萌", "CHS", "pending", 32),
        (8, "喵萌", "CHS", "pending", 36), (8, "喵萌", "CHS v2", "excluded", 36),
    ])
    assert A.keyword_blocked_episodes(_animes(clean_tables)) == {aid: [7]}


@pytest.mark.parametrize("st", ["error", "skipped"])
def test_hit_version_in_other_status_is_not_reported(clean_tables, lag7, st):
    """命中关键词的那条在（失败 / 同集闸跳过），只剩不命中的待下：不是"没有一条命中"，归失败告警等去管。"""
    _mk(clean_tables, [(4, "喵萌", "CHS", "pending", 20), (4, "喵萌", "简日", st, 20)])
    assert A.keyword_blocked_episodes(_animes(clean_tables)) == {}


def test_covered_looks_at_all_sources_even_when_locked(clean_tables, lag7):
    """锁了喵萌：第 6 集喵萌只有不命中的待下行，但 ANi 那份已经在盘上（兜底 / 手动补的）→ 有着落，不报。"""
    _mk(clean_tables, [(6, "喵萌", "CHS", "pending", 20), (6, "ANi", "CHT", "sent", 20)], lock="喵萌")
    assert A.keyword_blocked_episodes(_animes(clean_tables)) == {}


def test_error_only_episode_is_not_reported(clean_tables, lag7):
    """这一集只剩失败行（没有待下行）：归失败告警，不归这条。"""
    _mk(clean_tables, [(4, "喵萌", "CHS", "error", 20)])
    assert A.keyword_blocked_episodes(_animes(clean_tables)) == {}


def test_keyword_match_is_case_insensitive_like_flush(clean_tables, lag7):
    """判据必须是 flush 用的同一个 _kw_match（大小写不敏感）：关键词 chs、标题 [CHS] 是命中，会下，不能报。"""
    aid = _mk(clean_tables, [(4, "喵萌", "CHS", "pending", 20)], kw="chs")
    assert A.keyword_blocked_episodes(_animes(clean_tables)) == {}
    assert A.download_plan(aid)


def _two_numberings(hit_base, other_base):
    """两个组同一天各发一集、每周一集，共 8 周：命中关键词的组（简日，已下）从 hit_base 起编号，
    另一个组（CHT，待下）从 other_base 起编号。"""
    rows = []
    for i in range(8):
        d = 56 - 7 * i
        rows.append((hit_base + i, "喵萌", "简日", "sent", d))
        rows.append((other_base + i, "ANi", "CHT", "pending", d))
    return rows


@pytest.mark.parametrize("hit_base,other_base,total,offset", [
    (1, 13, 12, None),     # 命中的组用季内号，另一组用绝对号
    (13, 1, 12, None),     # 反过来
    (13, 25, 24, 12),      # 歧义番 O=12 / T=24 的绝对号尾巴
])
def test_two_numberings_in_sync_is_not_reported(clean_tables, lag7, hit_base, other_base, total, offset):
    """没锁源、只设了关键词，两个组用不同编号同步在发：命中关键词的那组每集都下了，用户一集没漏，不该报。"""
    _mk(clean_tables, _two_numberings(hit_base, other_base), total=total, offset=offset)
    assert A.keyword_blocked_episodes(_animes(clean_tables)) == {}


def _weekly(e):
    return {"release_time": NOW - timedelta(days=49 - 7 * (e - 1))}


def test_sync_exemption_compares_release_time_not_ingest_time(clean_tables, lag7):
    """发布时间每周一集、入库时间挤在一起 —— 这是真被挡住，要报。按入库时间比，挤在一起的简日行与 CHS 行
    "同一时间"且集号差 > 2，会被当成两套编号同步在发而漏报。两种来路：
      · 中途才加的番：一次采回整季历史，所有行同一天入库；
      · 简日行按时入库，CHS 的 4~7 集是一次补齐搜回来的（与第 3 集同一天入库）。"""
    late = _mk(clean_tables, [(e, "喵萌", "简日", "sent", 20, _weekly(e)) for e in (1, 2, 3)]
               + [(e, "喵萌", "CHS", "pending", 20, _weekly(e)) for e in (4, 5, 6, 7)], title="中途加的番")
    batch = _mk(clean_tables, [(e, "喵萌", "简日", "sent", 49 - 7 * (e - 1)) for e in (1, 2, 3)]
                + [(e, "喵萌", "CHS", "pending", 35, _weekly(e)) for e in (4, 5, 6, 7)], title="补齐搜回的番")
    assert A.keyword_blocked_episodes(_animes(clean_tables)) == {late: [4, 5, 6, 7], batch: [4, 5, 6, 7]}


def test_same_numbering_a_day_apart_is_not_sync(clean_tables, lag7):
    """同一套编号前后脚：简日的组发完第 3 集就停了，另一组第二天发第 4 集（CHT）。集号只差 1，不是两套编号，要报。"""
    aid = _mk(clean_tables, [(3, "喵萌", "简日", "sent", 29), (4, "ANi", "CHT", "pending", 28),
                             (5, "ANi", "CHT", "pending", 21)])
    assert A.keyword_blocked_episodes(_animes(clean_tables)) == {aid: [4, 5]}


def test_slot_impostor_row_does_not_count_as_a_torrent(clean_tables, lag7, cfg):
    """第 4 集库里只有一条别季的旧种子（发布比本季首播早一年多，占槽行，待下、不命中关键词）：
    缺集提示不认它、报缺第 4 集；这里也不能认它，否则同一集两条提示说法相反。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=200)
    rows = [(e, "喵萌", "简日", "sent", 49 - 7 * (e - 1)) for e in (1, 2, 3, 5, 6)]
    rows += [(4, "ANi", "CHT", "pending", 20, {"release_time": NOW - timedelta(weeks=70)})]
    aid = _mk(clean_tables, rows)
    animes = _animes(clean_tables)
    assert 4 in A.episode_gaps(animes).get(aid, [])
    assert A.keyword_blocked_episodes(animes) == {}


def test_no_keyword_or_not_subscribed_is_not_reported(clean_tables, lag7):
    _flow_s1(clean_tables, kw=None)
    _flow_s1(clean_tables, confirmed=False, title="待确认番")
    assert A.keyword_blocked_episodes(_animes(clean_tables)) == {}


def test_rejected_or_finished_unsub_is_not_reported(clean_tables, cfg):
    """已忽略 / 下齐后已停订的番不在追，不报（只看 confirmed 不够）。"""
    cfg(ANIME_LOCK_LAG_DAYS=7, ANIME_FINISH_UNSUB=True)
    a1 = _flow_s1(clean_tables, title="已忽略")
    a2 = _flow_s1(clean_tables, title="已停订")
    with clean_tables.get_session() as s:
        x, y = s.get(Anime, a1), s.get(Anime, a2)
        x.rejected = True
        y.finished_at = NOW
        s.add(x)
        s.add(y)
        s.commit()
    assert A.keyword_blocked_episodes(_animes(clean_tables)) == {}


def test_locked_source_only_looks_at_the_locked_groups_rows(clean_tables, lag7):
    """锁了喵萌：第 4 集喵萌 CHS → 报；第 5 集只有 ANi 的（归锁源落后管）→ 不报；
    第 6 集喵萌 CHS + ANi 简日 → 报（ANi 那条锁着也不会下）。"""
    aid = _mk(clean_tables, [
        (4, "喵萌", "CHS", "pending", 20),
        (5, "ANi", "CHT", "pending", 20),
        (6, "喵萌", "CHS", "pending", 20), (6, "ANi", "简日", "pending", 20),
    ], lock="喵萌")
    assert A.keyword_blocked_episodes(_animes(clean_tables)) == {aid: [4, 6]}


def test_foreign_season_rows_do_not_count(clean_tables, lag7):
    _mk(clean_tables, [(4, "喵萌", "CHS", "pending", 20, {"foreign_season": True})])
    assert A.keyword_blocked_episodes(_animes(clean_tables)) == {}


def test_non_integer_episode_is_not_reported(clean_tables, lag7):
    """4.5 这类总集篇 / 半集、第 0 集不是正集，flush 本来就不自动下它，不归这条提醒。"""
    _mk(clean_tables, [(4.5, "喵萌", "CHS", "pending", 20), (0, "喵萌", "CHS", "pending", 20)])
    assert A.keyword_blocked_episodes(_animes(clean_tables)) == {}


def test_zero_days_turns_it_off(clean_tables, cfg, lag7):
    _flow_s1(clean_tables)
    cfg(ANIME_LOCK_LAG_DAYS=0)
    assert A.keyword_blocked_episodes(_animes(clean_tables)) == {}


# ==================== 仪表盘 ====================

def _pending_total(db):
    with db.get_session() as s:
        return len(list(s.exec(select(AnimeTorrent.id).where(AnimeTorrent.status == "pending"))))


def test_breakdown_moves_blocked_rows_out_of_backup_and_keeps_the_sum(clean_tables, lag7):
    s1 = _flow_s1(clean_tables)
    with clean_tables.get_session() as s:   # 同集的『不属于本季』待下行不跟着挪，留在『备用项』
        s.add(AnimeTorrent(anime_id=s1, info_hash=f"{999:040x}", source="喵萌", site="nyaa", raw_title="[喵萌] 旧 - 04",
                           episode=4.0, status="pending", foreign_season=True, created_at=NOW - timedelta(days=20)))
        s.commit()
    # 锁了源的番：锁定组 CHS 行进『锁源缺集』；别的组同集的简日行照旧（被锁挡着、ep ≤ 锁定源最大集号 → 备用项）
    _mk(clean_tables, [(1, "喵萌", "简日", "sent", 30), (2, "喵萌", "CHS", "pending", 20),
                       (2, "ANi", "简日", "pending", 20)], lock="喵萌", title="锁源番")
    b = A.pending_breakdown()
    assert b["locked"] == 5 and b["backup"] == 2, b
    assert sum((b["will"], b["backup"], b["unconfirmed"], b["unknown"], b["special"],
                b["finished"], b["locked"])) == _pending_total(clean_tables), b


def test_breakdown_moves_per_episode_not_per_show(clean_tables, lag7):
    """同一部番：第 4 集被挡（进『锁源缺集』），第 3 集已有一份、它的另一条待下仍是『备用项』。"""
    _mk(clean_tables, [(3, "喵萌", "简日", "sent", 30), (3, "喵萌", "CHS", "pending", 30),
                       (4, "喵萌", "CHS", "pending", 20)])
    b = A.pending_breakdown()
    assert (b["locked"], b["backup"]) == (1, 1), b


def test_dashboard_card_opens_a_list_that_names_the_show(clean_tables, lag7):
    """卡片上『锁源缺集』有数时，点开的列表（locked_out_rows）里必须找得到这部番，否则卡片有数、点开是空的。"""
    _flow_s1(clean_tables)
    assert A.pending_breakdown()["locked"] == 4
    rows = A.locked_out_rows()
    assert [r["name"] for r in rows] == ["关键词番"]
    assert "『简日』" in rows[0]["raw"] and "4～7" in rows[0]["raw"]


def test_recent_list_badge_agrees_with_dashboard_bucket(clean_tables, lag7):
    """新入库列表逐条徽标（locked_out_torrent_ids → 『锁源缺集』）与仪表盘桶同口径：被挡的行标『锁源缺集』；
    已有一份那一集的另一条待下仍是『备用项』；锁了源时只有锁定组的行跟着挪。"""
    _flow_s1(clean_tables)
    _mk(clean_tables, [(3, "喵萌", "简日", "sent", 30), (3, "喵萌", "CHS", "pending", 30)], title="已有一份")
    _mk(clean_tables, [(1, "喵萌", "简日", "sent", 30), (2, "喵萌", "CHS", "pending", 20),
                       (2, "ANi", "简日", "pending", 20)], lock="喵萌", title="锁源番")
    rows = {(r["name"], r["episode"], r["source"]): r["locked_out"]
            for r in A.recent_anime_rows(50) if r["status"] == "pending"}
    assert rows == {**{("关键词番", float(e), "喵萌"): True for e in (4, 5, 6, 7)}, ("已有一份", 3.0, "喵萌"): False,
                    ("锁源番", 2.0, "喵萌"): True, ("锁源番", 2.0, "ANi"): False}, rows


# ==================== 巡检推送 ====================

@pytest.fixture
def pushes(monkeypatch, cfg):
    """真实事件层（订阅 / 冷却照跑），只收走发出去的内容。"""
    box = []

    async def fake(msg):
        box.append(msg)
        return True
    monkeypatch.setattr(N, "notify", fake)
    N._last_sent.clear()
    N._sent_times.clear()
    N._fail_streak, N._muted_until = 0, 0.0
    cfg(NOTIFY_URL="http://push.example/key", NOTIFY_MAX_PER_HOUR=0, NOTIFY_EVENTS=["gap"])
    yield box
    N._last_sent.clear()


def test_sweep_gaps_pushes_once_and_not_again_for_the_same_batch(clean_tables, lag7, pushes):
    _flow_s1(clean_tables)
    assert asyncio.run(A.sweep_gaps()) == 1
    assert len(pushes) == 1
    m = pushes[0]
    assert "关键词番：第 4～7 集有种子" in m and "『简日』" in m and "7 天以上没下" in m and "改了标签写法" in m
    assert asyncio.run(A.sweep_gaps()) == 0 and len(pushes) == 1, "同一批第二轮不该重复"


def test_sweep_gaps_pushes_again_when_blocked_set_grows(clean_tables, lag7, pushes):
    """挡住的集变了（多了一集）是新的一条：冷却期内也要再推。"""
    aid = _flow_s1(clean_tables)
    assert asyncio.run(A.sweep_gaps()) == 1
    with clean_tables.get_session() as s:
        s.add(AnimeTorrent(anime_id=aid, info_hash=f"{77777:040x}", source="喵萌", site="nyaa",
                           raw_title="[喵萌] 关键词番 - 08 [CHS]", season=1, episode=8.0, quarter="26D",
                           status="pending", priority=100, created_at=NOW - timedelta(days=8)))
        s.commit()
    assert asyncio.run(A.sweep_gaps()) == 1 and "4～8" in pushes[-1], pushes


def test_sweep_gaps_names_the_locked_group(clean_tables, lag7, pushes):
    """锁了源时判的只是锁定组的行（别的组可能有命中的版本），文案要点名是锁定组发的。"""
    _mk(clean_tables, [(1, "喵萌", "简日", "sent", 30), (2, "喵萌", "CHS", "pending", 20),
                       (2, "ANi", "简日", "pending", 20)], lock="喵萌")
    assert asyncio.run(A.sweep_gaps()) == 1
    assert "锁定的『喵萌』发的第 2 集有种子" in pushes[0], pushes


# ==================== 详情页 ====================

@pytest.mark.nicegui_main_file("tests/render_detail_main.py")
async def test_detail_page_shows_the_banner(user, clean_tables, lag7):
    aid = _flow_s1(clean_tables)
    await user.open(f"/detail/{aid}")
    await user.should_see("第 4～7 集有种子，但没有一条命中你设的版本关键词『简日』")
    await user.should_see("命中『简日』的版本出现之前不会自动下")


@pytest.mark.nicegui_main_file("tests/render_detail_main.py")
async def test_detail_page_has_no_banner_without_keyword(user, clean_tables, lag7):
    aid = _flow_s1(clean_tables, kw=None)
    await user.open(f"/detail/{aid}")
    await user.should_not_see("没有一条命中你设的版本关键词")


def test_dashboard_locked_chip_tooltip_mentions_keyword():
    """『锁源缺集』那张卡也收被版本关键词挡住的行，说明要跟着说清楚，否则用户只会去找锁定源。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "pages" / "anime.py").read_text(encoding="utf-8")
    i = src.index('("锁源缺集", ps["locked"]')
    assert "或被版本关键词挡住" in src[i:src.index("_open_locked", i)]
