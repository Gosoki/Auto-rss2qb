"""(R14) bgm 匹配到了错误的季 → 降级成『待确认』，不掉进任何自动结论。

真实案例：`[百冬练习组&LoliHouse] Re:从零开始的异世界生活 … - 78` 标题没有季标记 →
season=1 → 两个候选名【一致】命中 bgm 140001『第一季』(2016-04-03, 26 集) → 不是平票、
直接绑上 → air_date 2016 早于开始使用日 → 整部番被判『超期忽略』，一集不下，
而界面上它和用户手动拒绝的番长得一模一样。正确答案是『第四季 夺还篇』(绝对第 78 集 = 该季第 1 集)。
"""
from datetime import datetime, timedelta

import pytest

from core import anime as A
from db.models import Anime, AnimeTorrent
from sources.base import ParsedItem


def _anime(total, air_date):
    return Anime(title="x", season=1, total_episodes=total, air_date=air_date)


def _item(ep, release, **kw):
    return ParsedItem(info_hash="0" * 40, raw_title="x", anime_title="x", season=1, episode=ep,
                      quarter="26C", release_time=release, download_url="http://x/y.torrent",
                      source="组", site="nyaa", **kw)


@pytest.mark.parametrize("total,air,ep,rel,want,why", [
    # 真实的绑错季：第 78 集 > 26 集，且比该季首播晚了 543 周（本季跨度只有 26 周）
    (26, "2016-04-03", 78, datetime(2026, 8, 20), True, "Re:Zero 绑成 2016 年的第一季"),
    (25, "2018-10-01", 88, datetime(2026, 8, 20), True, "转生史莱姆绑成 2018 年的第一季"),
    # 【不能误伤】用全系列绝对编号、但绑的就是当前这一季 —— 真库 98 部里有 15 部(15%)是这样，
    # 其中 8 部正在正常追番。只看"集号超出总集数"会把它们全推进待确认，等于把自动化关掉。
    (24, "2026-04-03", 88, datetime(2026, 8, 20), False, "真库 anime#30：绑对第四季，只是用绝对编号"),
    (13, "2026-07-05", 21, datetime(2026, 8, 30), False, "本季 13 集、收到第 21 集，仍在窗口内"),
    (9, "2026-01-24", 22, datetime(2026, 7, 28), False, "金牌得主第二季：首播后 26 周的正常补流"),
    # 集号没超出总集数 —— 第一个条件就不成立
    (26, "2016-04-03", 12, datetime(2026, 8, 20), False, "集号在本季范围内，再老也不判"),
    # 判不了就不判（与本项目"宁可不判、绝不误判"的一贯口径一致）
    (26, "2016-04-03", 78, None, False, "缺发布时间"),
    (0, "2016-04-03", 78, datetime(2026, 8, 20), False, "bgm 没给总集数"),
    (26, "", 78, datetime(2026, 8, 20), False, "bgm 没给首播日"),
    (26, "2016-04-03", -2, datetime(2026, 8, 20), False, "未知集不参与"),
    (26, "2016-04-03", -1, datetime(2026, 8, 20), False, "特别篇不参与"),
    # ── (R18) 对称的另一半：发布得【远早于】本季首播，且这一侧【不看集号】 ──
    # 真库 anime#6：『超超超超超喜欢你的100个女朋友 第三季』(2026-07-05, 12 集) 底下挂着
    # 24 条 `[ANi] 超超超超超喜歡你的 100 個女朋友 - 01..24`（标题不带季标记，全是第一季正片），
    # 发布时间早 134~143 周，且已 sent 进第三季的目录。它们填满 1..12 这些槽位，
    # 于是 sweep_finished 把一部才播 8 周的番判成了完结。
    (12, "2026-07-05", 1, datetime(2023, 10, 8), True, "真库 anime#6：第一季正片挂到了第三季下"),
    (12, "2026-07-05", 12, datetime(2023, 12, 24), True, "同上，集号落在 [1,total] 内也要能判"),
    # 【不能误伤】bgm 记的是电视首播日，而流媒体/抢先场常早几天 —— 真库 7 部都属于这类
    (12, "2026-07-10", 1, datetime(2026, 7, 3), False, "真库 anime#31：早 1 周，正常抢先"),
    (12, "2026-07-07", 1, datetime(2026, 6, 21), False, "真库 anime#52：早 2.3 周，正常"),
    (13, "2026-07-05", 12, datetime(2026, 6, 10), False, "真库 anime#8：早 3.6 周，本轮余量下【不判】(见 E-34)"),
    (12, "2026-07-05", -1, datetime(2023, 10, 8), False, "先行特典本来就可能早于首播，不参与"),
    # 【把余量的常数项与斜率都钉死】span = total + _OFF_SEASON_SLACK_WEEKS(26)。
    # 不钉的话这个正在等拍板的参数落在守卫盲区里：第 19 轮实测把 -span 换成 E-34 里的另一个
    # 候选 -4，全套用例照样全绿——拍板结果落地时不会有任何用例告诉你"你确实改了它"。
    (12, "2026-07-05", 1, datetime(2026, 7, 5) - timedelta(weeks=38, days=1), True,
     "12 集番：早于首播 38 周(=12+26) 再多一天 → 判"),
    (12, "2026-07-05", 1, datetime(2026, 7, 5) - timedelta(weeks=38) + timedelta(days=1), False,
     "同上，差一天不到 38 周 → 不判"),
    (26, "2026-07-05", 1, datetime(2026, 7, 5) - timedelta(weeks=52, days=1), True,
     "26 集番的门槛是 52 周(=26+26)——斜率也要钉，否则只钉住了常数项"),
    (26, "2026-07-05", 1, datetime(2026, 7, 5) - timedelta(weeks=40), False,
     "26 集番早 40 周还不到它的 52 周门槛 → 不判（同样的 40 周在 12 集番上会判）"),
])
def test_episode_cannot_belong(total, air, ep, rel, want, why):
    assert A._episode_cannot_belong(_anime(total, air), _item(ep, rel)) is want, why


@pytest.fixture
def _no_alias_hit(monkeypatch):
    """让 _resolve_anime 一定走 bgm 那条路（不被已有别名短路）。"""
    return monkeypatch


async def _ingest(monkeypatch, clean_tables, cfg, info, ep, release):
    async def _fake_resolve(*a_, **k_):
        return info
    monkeypatch.setattr(A.enrich, "resolve", _fake_resolve)
    item = _item(ep, release)
    item.anime_title = "某番"
    item.search_names = ["某番"]
    await A.process_item(item)
    with clean_tables.get_session() as s:
        t = s.exec(__import__("sqlmodel").select(AnimeTorrent)).first()
        return s.get(Anime, t.anime_id) if t else None


async def test_wrong_season_goes_to_pending_confirm_not_ignored(monkeypatch, clean_tables, cfg):
    """绑错季时【不能】掉进『超期忽略』——那一支必须让位给『待确认』。

    这是本条修复的全部要点：绑错季时 air_date 往往是十年前的初代，超期判定会二话不说
    把它打成『超期忽略』，一部正在更新的番就此静默停更。
    """
    cfg(ANIME_START_DATE="2026-05-30")
    a = await _ingest(monkeypatch, clean_tables, cfg,
                      {"bangumi_id": 140001, "display_name": "第一季",
                       "air_date": "2016-04-03", "total_episodes": 26},
                      ep=78, release=datetime(2026, 8, 20))
    assert a is not None and a.bangumi_id == 140001, "绑定要保留（元数据/封面/offset 学习都要它）"
    assert (a.confirmed, a.rejected) == (False, False), \
        f"应为『待确认』，实际 confirmed={a.confirmed} rejected={a.rejected}"


async def test_normal_absolute_numbering_still_auto_confirms(monkeypatch, clean_tables, cfg):
    """绑对了季、只是用绝对编号 → 照常自动确认，不能因为这条修复而多一道人工闸。"""
    cfg(ANIME_START_DATE="2026-01-01")
    a = await _ingest(monkeypatch, clean_tables, cfg,
                      {"bangumi_id": 515594, "display_name": "第四季",
                       "air_date": "2026-04-03", "total_episodes": 24},
                      ep=88, release=datetime(2026, 8, 20))
    assert a is not None and (a.confirmed, a.rejected) == (True, False), \
        f"应为『追番中』，实际 confirmed={a.confirmed} rejected={a.rejected}"


async def test_genuinely_old_anime_still_gets_ignored(monkeypatch, clean_tables, cfg):
    """真正的老番（集号也在本季范围内）照旧判『超期忽略』——开始使用日那条规则没被削弱。"""
    cfg(ANIME_START_DATE="2026-05-30")
    a = await _ingest(monkeypatch, clean_tables, cfg,
                      {"bangumi_id": 400602, "display_name": "老番",
                       "air_date": "2023-09-29", "total_episodes": 28},
                      ep=5, release=datetime(2023, 10, 29))
    assert a is not None and (a.confirmed, a.rejected) == (False, True), \
        f"应为『超期忽略』，实际 confirmed={a.confirmed} rejected={a.rejected}"


# ---------------- 闸必须装在【两处】，不能只装建番那一处 ----------------

async def _ingest_into_existing(clean_tables, *, total, air, ep, release, hash_seed):
    """库里先有一部『待确认』的番 + 别名，再投一条 auto 源的种子进去（走 alias 命中那条出口）。"""
    from sources.parse import candidate_names
    with clean_tables.get_session() as s:
        a = Anime(title="某番", season=1, quarter="26C", bangumi_id=140001,
                  air_date=air, total_episodes=total, confirmed=False, rejected=False)
        s.add(a); s.commit(); s.refresh(a)
        s.add(A.AnimeAlias(title=A.alias_key("某番"), season=1, anime_id=a.id))
        s.commit()
        aid = a.id
    item = _item(ep, release)
    item.info_hash = hash_seed * 40
    item.anime_title = "某番"
    item.search_names = candidate_names("某番")
    item.policy = "auto"
    await A.process_item(item)
    with clean_tables.get_session() as s:
        return s.get(Anime, aid)


async def test_auto_promote_does_not_undo_the_downgrade(clean_tables, cfg):
    """【广度】绑错季而被降级成『待确认』的番，不能被 process_item 的『自动升确认』升回去。

    这条闸原来只装在 _resolve_anime 的"新建番"分支里，而升确认分支的入口条件与被降级的番
    完全吻合：待确认(0,0) + 有 bangumi_id + auto 源 + 一集都没下过。

    【场景必须选对，否则这条用例是假的】升确认的函数体里还有一道
    `not _aired_before_start(a.air_date)`，所以"绑到十年前的老季"那一类本来就被超期判据挡着，
    用它来测这道闸的话，把闸拆掉用例照样绿（第一版正是这么写的，靠回退验证才发现）。
    这里造的是**另一半**：所绑的季【不早于】开始使用日，超期判据帮不上忙，只有这道闸能挡。
    """
    from datetime import datetime
    cfg(ANIME_START_DATE="2020-01-01")          # 番不超期 → 超期判据不介入
    a = await _ingest_into_existing(clean_tables, total=12, air="2026-01-05",
                                    ep=30, release=datetime(2026, 11, 1), hash_seed="a")
    assert (a.confirmed, a.rejected) == (False, False), \
        f"绑错季的番被自动升确认了：confirmed={a.confirmed} rejected={a.rejected}"


async def test_auto_promote_still_works_for_normal_anime(clean_tables, cfg):
    """【别扩太宽】绑定正常的『待确认』番，auto 源贡献种子时照旧升确认。

    升确认这条分支救的是"review/泛 feed 先建番把 auto 主力番静默压进待确认"这类真问题，
    不能因为多了一道闸就把它整个废掉。与上一条只差集号/发布时间两个值。
    """
    from datetime import datetime
    cfg(ANIME_START_DATE="2020-01-01")
    a = await _ingest_into_existing(clean_tables, total=12, air="2026-01-05",
                                    ep=5, release=datetime(2026, 2, 5), hash_seed="b")
    assert (a.confirmed, a.rejected) == (True, False), \
        f"正常番没能被升确认：confirmed={a.confirmed} rejected={a.rejected}"


# ---------------- await 期间的人工绑定不能被后台盖掉 ----------------

async def test_manual_binding_during_the_await_wins(clean_tables, monkeypatch, cfg):
    """后台重识别在等 bgm 的这 120 秒里，用户手工绑定了正确的 id —— 后台回来不许盖掉它。

    `enrich.resolve` 的整体预算是 120 秒，是全项目最长的 await 窗口之一。原来 await 之后
    只重取了一次 Anime，挡的是"番被删了"，没挡"绑定变了"，而 `apply_bgm_meta` 对
    bangumi_id 是无条件覆写。触发面最大的是设置页『批量重新识别』：几十个 id 逐个跑，
    窗口是几十个 120 秒之和。覆写之后还会走身份守卫 `_merge_anime` —— 那一步会【删掉】
    另一条番记录，没有撤销入口。
    """
    with clean_tables.get_session() as s:
        a = Anime(title="某番", season=1, bangumi_id=None)     # 待识别
        s.add(a); s.commit(); s.refresh(a); aid = a.id

    async def _slow_resolve(*a_, **k_):
        # 模拟 await 期间用户在另一条路径上完成了人工绑定
        with clean_tables.get_session() as s2:
            row = s2.get(Anime, aid)
            row.bangumi_id = 576121          # 用户填的正确 id
            row.confirmed = False
            s2.add(row); s2.commit()
        return {"bangumi_id": 999999, "display_name": "后台自动匹配到的另一部",
                "air_date": "2020-01-01", "total_episodes": 12}

    monkeypatch.setattr(A.enrich, "resolve", _slow_resolve)
    await A.enrich_anime(aid, freeze_empty_path=True)

    with clean_tables.get_session() as s:
        got = s.get(Anime, aid)
    assert got.bangumi_id == 576121, \
        f"用户手工绑的 576121 被后台的自动结果盖成了 {got.bangumi_id}"
    assert got.display_name != "后台自动匹配到的另一部", "连元数据也一起盖掉了"


async def test_reidentify_still_overwrites_when_nobody_else_touched_it(clean_tables, monkeypatch, cfg):
    """【别扩太宽】没有第三方改动时，『重新识别』照旧覆写 —— 那正是这个按钮的用途。

    判据是 compare-and-set，不是"有值就不覆盖"。
    """
    with clean_tables.get_session() as s:
        a = Anime(title="某番", season=1, bangumi_id=111, display_name="旧名")
        s.add(a); s.commit(); s.refresh(a); aid = a.id

    async def _resolve(*a_, **k_):
        return {"bangumi_id": 222, "display_name": "新名",
                "air_date": "2026-01-05", "total_episodes": 12}

    monkeypatch.setattr(A.enrich, "resolve", _resolve)
    await A.enrich_anime(aid)

    with clean_tables.get_session() as s:
        got = s.get(Anime, aid)
    assert got.bangumi_id == 222 and got.display_name == "新名", \
        f"无人改动时应照常覆写，实际 bgm={got.bangumi_id} name={got.display_name!r}"


# ---------------- 决定『超期忽略』的地方有三处，闸必须都装 ----------------

def test_batch_start_date_filter_skips_suspect_bindings(clean_tables, cfg):
    """【广度·第三处】设置页的『应用开始使用日过滤』不能把被降级的番打回『超期忽略』。

    闸原来只装在两个【手上有 item】的地方（建番、自动升确认），而决定『超期忽略』的
    第三处 `apply_start_date_filter()` 是批量重算、一条 item 都没有。
    于是用户点一次那个按钮，修复等于没做——真库实测 anime#100（Re:Zero 绑成 2016 年
    第一季）点一次就掉回『超期忽略』，从主列表消失、永久停更。
    这是同一轮里第三次同款广度错误，所以判据改成了「给一部番就能算」的 binding_looks_wrong。
    """
    from datetime import datetime
    cfg(ANIME_START_DATE="2026-05-30")
    with clean_tables.get_session() as s:
        # 绑错季而被降级的番：air_date 是十年前的初代，集号远超该季且发布时间远超窗口
        bad = Anime(title="绑错季的", season=1, bangumi_id=140001, air_date="2016-04-03",
                    total_episodes=26, confirmed=False, rejected=False)
        # 对照：真正的老番（集号在本季范围内），应照常被判超期忽略
        old = Anime(title="真老番", season=1, bangumi_id=222, air_date="2016-04-03",
                    total_episodes=26, confirmed=False, rejected=False)
        s.add(bad); s.add(old); s.commit(); s.refresh(bad); s.refresh(old)
        s.add(AnimeTorrent(info_hash="a" * 40, anime_id=bad.id, source="组", site="nyaa",
                           raw_title="x", season=1, episode=78.0, status="pending",
                           release_time=datetime(2026, 8, 20)))
        s.add(AnimeTorrent(info_hash="b" * 40, anime_id=old.id, source="组", site="nyaa",
                           raw_title="y", season=1, episode=5.0, status="pending",
                           release_time=datetime(2016, 5, 1)))
        s.commit()
        bad_id, old_id = bad.id, old.id

    A.apply_start_date_filter()

    with clean_tables.get_session() as s:
        b, o = s.get(Anime, bad_id), s.get(Anime, old_id)
    assert (b.confirmed, b.rejected) == (False, False), \
        f"绑定可疑的番被批量过滤打回超期忽略了：confirmed={b.confirmed} rejected={b.rejected}"
    assert o.rejected is True, "真正的老番应照常判超期忽略——判据别扩太宽"


def test_merge_does_not_resurrect_a_suspect_binding(clean_tables, cfg):
    """【广度·第四处】合并时的 union-active 不能把被降级的番升回『追番中』。

    `_merge_anime` 的 union-active 规则本身是对的（防"番静默从追番中掉回待确认、从此停更"），
    但合并也跑在【自动】路径上（enrich_anime 末尾的身份守卫，由后台 retry_unmatched /
    批量重新识别驱动），那里它会把刚被降级的番悄悄恢复自动下载——而降级的意思正是
    "这个 bgm 绑定多半错了，等人看一眼"。
    """
    from datetime import datetime
    with clean_tables.get_session() as s:
        keeper = Anime(title="绑错季的", season=1, bangumi_id=140001, air_date="2016-04-03",
                       total_episodes=26, confirmed=False, rejected=False)   # 被降级
        loser = Anime(title="健康的", season=1, bangumi_id=140001, air_date="2016-04-03",
                      total_episodes=26, confirmed=True, rejected=False)     # 追番中
        s.add(keeper); s.add(loser); s.commit(); s.refresh(keeper); s.refresh(loser)
        s.add(AnimeTorrent(info_hash="c" * 40, anime_id=keeper.id, source="组", site="nyaa",
                           raw_title="x", season=1, episode=78.0, status="pending",
                           release_time=datetime(2026, 8, 20)))
        s.commit()
        kid, lid = keeper.id, loser.id
        A._merge_anime(s, lid, kid)

    with clean_tables.get_session() as s:
        k = s.get(Anime, kid)
    assert (k.confirmed, k.rejected) == (False, False), \
        f"合并把绑定可疑的番升回了追番中：confirmed={k.confirmed} rejected={k.rejected}"


def test_merge_still_unions_active_for_healthy_bindings(clean_tables, cfg):
    """【别扩太宽】绑定正常时，union-active 照旧生效——它防的是合并致停更，不能废掉。"""
    with clean_tables.get_session() as s:
        keeper = Anime(title="待确认的", season=1, bangumi_id=333, air_date="2026-01-05",
                       total_episodes=12, confirmed=False, rejected=False)
        loser = Anime(title="追番中的", season=1, bangumi_id=333, air_date="2026-01-05",
                      total_episodes=12, confirmed=True, rejected=False)
        s.add(keeper); s.add(loser); s.commit(); s.refresh(keeper); s.refresh(loser)
        kid, lid = keeper.id, loser.id
        A._merge_anime(s, lid, kid)

    with clean_tables.get_session() as s:
        k = s.get(Anime, kid)
    assert (k.confirmed, k.rejected) == (True, False), \
        f"绑定正常的番合并后应保持追番中：confirmed={k.confirmed} rejected={k.rejected}"


async def test_a_normal_torrent_cannot_undo_the_downgrade(clean_tables, cfg):
    """【判据的作用域】绑错季的番来一条【正常集号】的新种子，不能因此被升回追番中。

    自动升确认那处原来用的是 per-item 的 _episode_cannot_belong(a, item) —— 它只看当前这一条。
    于是库里已经躺着可疑种子、番也已被降级，只要下一条种子集号正常，这道闸就放行。
    换成 binding_looks_wrong(s, a)（看该番【已入库的全部种子】）才对得上。"""
    from datetime import datetime
    from sources.parse import candidate_names
    cfg(ANIME_START_DATE="2020-01-01")
    with clean_tables.get_session() as s:
        a = Anime(title="某番", season=1, quarter="26C", bangumi_id=140001,
                  air_date="2026-01-05", total_episodes=12, confirmed=False, rejected=False)
        s.add(a); s.commit(); s.refresh(a)
        s.add(A.AnimeAlias(title=A.alias_key("某番"), season=1, anime_id=a.id))
        s.add(AnimeTorrent(info_hash="d" * 40, anime_id=a.id, source="组", site="nyaa",
                           raw_title="x", season=1, episode=30.0, status="pending",
                           release_time=datetime(2026, 11, 1)))
        s.commit()
        aid = a.id

    item = _item(5.0, datetime(2026, 2, 5))
    item.info_hash = "e" * 40
    item.anime_title = "某番"
    item.search_names = candidate_names("某番")
    item.policy = "auto"
    await A.process_item(item)

    with clean_tables.get_session() as s:
        got = s.get(Anime, aid)
    assert (got.confirmed, got.rejected) == (False, False), \
        f"一条正常种子把降级撤销了：confirmed={got.confirmed} rejected={got.rejected}"


def test_merge_sees_evidence_on_the_loser_side(clean_tables, cfg):
    """【证据可能长在 loser 上】合并时 keeper 常是刚绑定、还没有种子的残条。

    这道闸跑在种子搬家之前，只查 keeper 的话在最常见的路径上完全看不见证据。
    """
    from datetime import datetime
    with clean_tables.get_session() as s:
        keeper = Anime(title="刚绑定的残条", season=1, bangumi_id=140001, air_date="2016-04-03",
                       total_episodes=26, confirmed=False, rejected=False)   # 无种子
        loser = Anime(title="攒着可疑种子的主番", season=1, bangumi_id=140001, air_date="2016-04-03",
                      total_episodes=26, confirmed=True, rejected=False)     # 追番中
        s.add(keeper); s.add(loser); s.commit(); s.refresh(keeper); s.refresh(loser)
        s.add(AnimeTorrent(info_hash="f" * 40, anime_id=loser.id, source="组", site="nyaa",
                           raw_title="y", season=1, episode=78.0, status="pending",
                           release_time=datetime(2026, 8, 20)))
        s.commit()
        kid, lid = keeper.id, loser.id
        A._merge_anime(s, lid, kid)

    with clean_tables.get_session() as s:
        k = s.get(Anime, kid)
    assert (k.confirmed, k.rejected) == (False, False), \
        f"证据在 loser 上时闸没生效：confirmed={k.confirmed} rejected={k.rejected}"


def test_the_one_shot_ignore_also_skips_suspect_bindings(clean_tables, cfg):
    """【广度·第五处】设置页那个一次性的『把开始日前的已确认老番转为超期忽略』同样要让路。

    全项目写 confirmed/rejected 的地方一共五处：建番、自动升确认、apply_start_date_filter、
    _merge_anime、以及这个 ignore_confirmed_before_start。前四处都装了闸，这一处最后补上。
    """
    from datetime import datetime
    cfg(ANIME_START_DATE="2026-05-30")
    with clean_tables.get_session() as s:
        bad = Anime(title="绑错季的", season=1, bangumi_id=140001, air_date="2016-04-03",
                    total_episodes=26, confirmed=True, rejected=False)
        old = Anime(title="真老番", season=1, bangumi_id=222, air_date="2016-04-03",
                    total_episodes=26, confirmed=True, rejected=False)
        s.add(bad); s.add(old); s.commit(); s.refresh(bad); s.refresh(old)
        s.add(AnimeTorrent(info_hash="1" * 40, anime_id=bad.id, source="组", site="nyaa",
                           raw_title="x", season=1, episode=78.0, status="pending",
                           release_time=datetime(2026, 8, 20)))
        s.add(AnimeTorrent(info_hash="2" * 40, anime_id=old.id, source="组", site="nyaa",
                           raw_title="y", season=1, episode=5.0, status="pending",
                           release_time=datetime(2016, 5, 1)))
        s.commit()
        bad_id, old_id = bad.id, old.id

    A.ignore_confirmed_before_start()

    with clean_tables.get_session() as s:
        b, o = s.get(Anime, bad_id), s.get(Anime, old_id)
    assert (b.confirmed, b.rejected) == (True, False), \
        f"绑定可疑的番被一次性忽略打成了超期忽略：confirmed={b.confirmed} rejected={b.rejected}"
    assert (o.confirmed, o.rejected) == (False, True), "真正的老番应照常转为超期忽略"


# ---------------- 绑到了单集特典 ----------------

def test_a_one_episode_subject_receiving_a_series_is_suspect(clean_tables):
    """bgm 上一部作品常拆成很多条目：正片、特别篇、PV、单集特典各占一条。

    搜名字时特典与正片同名，而 resolve 只按名字投票 —— 整部番就可能被绑到那条【只有 1 集】
    的特典上。真库实证：anime#96 绑到 664060『AKANE On My Mind〜饅頭こわい』(1 集·类型=其他)，
    而它的种子是「朱音落语」第 3~12 集、**已经下了 10 集**；正确答案是 576121『落语朱音』(12 集)。
    后果不只是名字错：total_episodes=1 让完结判定永远算不对，归档目录名也是特典的名字。
    """
    with clean_tables.get_session() as s:
        a = Anime(title="某番", season=1, bangumi_id=664060, air_date="2026-06-27",
                  total_episodes=1, confirmed=True, rejected=False)
        s.add(a); s.commit(); s.refresh(a)
        for i, ep in enumerate((3.0, 4.0, 5.0)):
            s.add(AnimeTorrent(info_hash=f"{i + 200:040x}", anime_id=a.id, source="组",
                               site="nyaa", raw_title="x", season=1, episode=ep, status="sent"))
        s.commit()
        assert A.binding_looks_wrong(s, a) is True


def test_a_genuine_one_episode_work_is_not_suspect(clean_tables):
    """【别扩太宽】真的单集作品（剧场版/OVA 进了 TV 表）会有多个字幕组的多个版本种子，
    但它们都是【同一集】—— 判据数的是不同集号，不是种子条数。"""
    with clean_tables.get_session() as s:
        a = Anime(title="某剧场版", season=1, bangumi_id=501958, air_date="2025-07-18",
                  total_episodes=1, confirmed=True, rejected=False)
        s.add(a); s.commit(); s.refresh(a)
        for i, src in enumerate(("桜都", "LoliHouse", "喵萌")):      # 三个组，同一集
            s.add(AnimeTorrent(info_hash=f"{i + 300:040x}", anime_id=a.id, source=src,
                               site="nyaa", raw_title="y", season=1, episode=1.0, status="pending"))
        s.commit()
        assert A.binding_looks_wrong(s, a) is False


# ---------------- 同一部番被拆成两条、且绑到不同 bgm（R19） ----------------

def test_suspect_duplicate_is_found_when_bgm_differs(clean_tables):
    """(R19) 全项目"一部番不该有两条记录"的判据只有 `bangumi_id 相同 → _merge_anime` 一种，
    而分裂的真实成因往往是【两条绑到了不同的 subject】—— 那时守卫恒不成立，界面上零提示。

    真库实证：anime#60『北斗神拳 拳王军杂兵们的挽歌 第二季』(bgm 637339) 与
    anime#86『北斗神拳 -FIST OF THE NORTH STAR-』(bgm 454438，2026-04 的另一部重制)。
    ANi 的标题带 `Animatica「…」` 前缀、别的源不带 → 第三个别名键 → 新建一部 →
    绑到别的 subject → 被判超期忽略，它下面的第 16~20 集**永远不会被下**。
    """
    from db.models import AnimeAlias
    with clean_tables.get_session() as s:
        a = Anime(title="北斗神拳拳王军杂兵们的挽歌", season=2, confirmed=True, bangumi_id=637339)
        b = Anime(title="北斗之拳拳王军杂兵们的挽歌", season=1, bangumi_id=454438, rejected=True)
        s.add(a); s.add(b); s.commit(); s.refresh(a); s.refresh(b)
        # 季号照抄真库：三条别名的 season 都是 1（ANi 的标题里没有季标记，
        # 『第二季』是 bgm 规范名带来的、只写进 Anime.season，不进别名键）
        s.add(AnimeAlias(title="Animatica「北斗之拳拳王军杂兵们的挽歌」", season=1, anime_id=a.id))
        s.add(AnimeAlias(title="北斗神拳拳王军杂兵们的挽歌", season=1, anime_id=a.id))
        s.add(AnimeAlias(title="北斗之拳拳王军杂兵们的挽歌", season=1, anime_id=b.id))
        s.commit()
        aid, bid = a.id, b.id

    hits = A.suspect_duplicate_anime()
    assert len(hits) == 1, f"没认出这一对：{hits}"
    d = hits[0]
    assert {d["a"], d["b"]} == {aid, bid}
    assert d["shared"] == ["北斗之拳拳王军杂兵们的挽歌"]
    assert d["a_rejected"] or d["b_rejected"]


def test_same_bgm_pairs_are_reported_too(clean_tables):
    """(E-18 之后) 绑到同一个 subject 的两条【也要报】。

    以前跳过它的理由是"身份守卫会自己合"，而 E-18 定了【自动路径一律不合、不删行】——
    于是同 bgm_id 的两条会一直留着，正需要有人看见并到详情页走人工路径合并（那条带回显）。
    """
    from db.models import AnimeAlias
    with clean_tables.get_session() as s:
        a = Anime(title="某番", season=1, bangumi_id=111)
        b = Anime(title="某番别写法", season=1, bangumi_id=111)
        s.add(a); s.add(b); s.commit(); s.refresh(a); s.refresh(b)
        s.add(AnimeAlias(title="XYZ「某番」", season=1, anime_id=a.id))
        s.add(AnimeAlias(title="某番", season=1, anime_id=b.id))
        s.commit()
    assert len(A.suspect_duplicate_anime()) == 1, "同 bgm 的两条现在也该报出来"


def test_a_title_that_starts_with_its_own_bracket_is_not_stripped(clean_tables):
    """(R19) 番名自带方头括号的不能被当成"制作公司前缀"剥掉 —— 本项目在 `【我推的孩子】` 上踩过一次。

    真库里含方头/直角括号的番名只有两个：`Animatica「…」`（该剥）与
    `『你们先走我断后』，于是10年后我成为了传说`（不该剥）。
    """
    assert A.canonical_alias("Animatica「北斗之拳拳王军杂兵们的挽歌」") == "北斗之拳拳王军杂兵们的挽歌"
    for keep in ("『你们先走我断后』，于是10年后我成为了传说", "【我推的孩子】",
                 "「不是前缀」后面还有正文"):
        assert A.canonical_alias(keep) == keep, f"不该剥的被剥了：{keep}"


def test_same_title_different_season_is_not_a_duplicate(clean_tables):
    """(R19) 同名不同季的两条【不是】重复 —— 别名表的唯一键就是 (title, season)。

    只按标题求交集的话，S1 与 S2 会被报成"多半是同一部番被拆成了两条"，
    而横幅给的指引是"把错的那条改绑成对的 bgm，身份守卫会自动合并" ——
    用户照做就是 `_merge_anime` 删掉一整行。
    真库里同一个别名标题挂多个 season 的已经有 3 组（『超超超超超喜欢你的100个女朋友』{1,3} 等），
    只是它们目前都指向同一个 anime_id 所以还没炸。
    """
    from db.models import AnimeAlias
    with clean_tables.get_session() as s:
        s1 = Anime(title="某番", season=1, bangumi_id=111)
        s2 = Anime(title="某番", season=2, bangumi_id=222)
        s.add(s1); s.add(s2); s.commit(); s.refresh(s1); s.refresh(s2)
        s.add(AnimeAlias(title="某番", season=1, anime_id=s1.id))
        s.add(AnimeAlias(title="某番", season=2, anime_id=s2.id))
        s.commit()
    assert A.suspect_duplicate_anime() == [], "同名不同季被报成了重复番"


def test_two_unbound_rows_sharing_a_name_are_reported(clean_tables):
    """(R19) 两边 bangumi_id 都是 None 时【要报】——身份守卫全都要求 bangumi_id 非空，
    这一对没有任何人会去合，恰恰是最该被看见的。原来那句短路对它恒成立，把它静默跳过了。"""
    from db.models import AnimeAlias
    with clean_tables.get_session() as s:
        x = Anime(title="没认出来的番", season=1)
        y = Anime(title="没认出来的番别写法", season=1)
        s.add(x); s.add(y); s.commit(); s.refresh(x); s.refresh(y)
        s.add(AnimeAlias(title="ABC「没认出来的番」", season=1, anime_id=x.id))
        s.add(AnimeAlias(title="没认出来的番", season=1, anime_id=y.id))
        s.commit()
    assert len(A.suspect_duplicate_anime()) == 1, "两条都没绑 bgm 的重复番被静默跳过了"


# ---------------- 批量『刷新资料』不得改绑定（E-42，R20） ----------------

async def test_batch_reenrich_never_rebinds(clean_tables, monkeypatch, cfg):
    """(E-42) 批量入口对【已绑上的番】只按 subject id 刷元数据，不重新匹配。

    真库实测：已有绑定的正确率 96/98 = 98.0%，而把自动路径重跑一遍的绑定错误率是 4.3% ——
    点一次『识别全部』就是拿 98% 去换 95.7%，而且改错的那几条还会走身份守卫 `_merge_anime`，
    那一步会删掉另一条番记录、没有撤销入口。
    """
    from services import enrich
    called = {"resolve": 0, "by_id": []}

    async def no_resolve(*a, **kw):
        called["resolve"] += 1
        return {"bangumi_id": 999999, "display_name": "被改绑成了别的番"}

    async def by_id(bid):
        called["by_id"].append(bid)
        return {"bangumi_id": bid, "display_name": "正确的番", "rating": 8.1}

    monkeypatch.setattr(enrich, "resolve", no_resolve)
    monkeypatch.setattr(enrich, "fetch_by_id", by_id)
    with clean_tables.get_session() as s:
        a = Anime(title="已绑好的番", season=1, confirmed=True, quarter="26C", bangumi_id=12345)
        s.add(a)
        s.commit()
        s.refresh(a)
        aid = a.id

    await A.reenrich_scope(None)

    with clean_tables.get_session() as s:
        row = s.get(Anime, aid)
        assert row.bangumi_id == 12345, "批量刷新把已绑好的番改绑了"
        assert row.rating == 8.1, "元数据没刷上"
    assert called["by_id"] == [12345], "没走按 id 直取"
    assert called["resolve"] == 0, "已绑上的番仍然走了按名字投票的匹配"


async def test_batch_reenrich_still_retries_the_unbound(clean_tables, monkeypatch, cfg):
    """没绑上的照常尝试识别 —— 这个入口的另一半用途就是"把之前没认出来的再试试"。"""
    from services import enrich
    hit = []

    async def resolve(*a, **kw):
        hit.append(1)
        return {"bangumi_id": 777, "display_name": "认出来了"}

    monkeypatch.setattr(enrich, "resolve", resolve)
    with clean_tables.get_session() as s:
        a = Anime(title="没认出来的番", season=1, quarter="26C")
        s.add(a)
        s.commit()
        s.refresh(a)
        aid = a.id

    await A.reenrich_scope(None)
    with clean_tables.get_session() as s:
        assert s.get(Anime, aid).bangumi_id == 777, "没绑上的番没有被尝试识别"
    assert hit, "没走 resolve"


async def test_enrich_never_deletes_a_row(clean_tables, monkeypatch, cfg):
    """(E-18 + R20 收口) 识别路径【一律不合并、不删行】—— 合并只在『绑定 bgm』那条带回显的路上做。

    `_merge_anime` 的最后一步是 `s.delete(loser)`，没有撤销入口。
    `enrich_anime` 的四个入口没有一个满足"用户明确绑定并看过回显"这个前提：
      · retry_unmatched / reenrich_scope —— 后台，没有人在看；
      · 详情页与『待识别』列表的『重新识别』—— 虽然是人点的，但它在 resolve 之前
        【根本不知道会绑到哪个 subject】，没法预先回显；用户按下去的意思是"帮这一部重算一次"，
        不是"把另一条记录删掉"。
    第 20 轮的审计端到端复现过：详情页点一次『重新识别』，另一条番整行消失、零提示。
    """
    from services import enrich

    async def resolve(*a, **kw):
        return {"bangumi_id": 555, "display_name": "同一部番"}
    monkeypatch.setattr(enrich, "resolve", resolve)

    with clean_tables.get_session() as s:
        old = Anime(title="已经在追的", season=1, confirmed=True, quarter="26C", bangumi_id=555)
        new_a = Anime(title="刚发现的", season=1, quarter="26C")
        s.add(old); s.add(new_a); s.commit(); s.refresh(old); s.refresh(new_a)
        old_id, new_id = old.id, new_a.id

    await A.enrich_anime(new_id)
    with clean_tables.get_session() as s:
        assert s.get(Anime, old_id) is not None, "识别路径把另一条番删掉了"
        assert s.get(Anime, new_id) is not None


async def test_explicit_bind_still_merges(clean_tables, monkeypatch, cfg):
    """『绑定 bgm』照常合并 —— 那条路的四个调用点全部经 require_bind_confirm，
    用户已经看过"你要绑的是《X》"和"会删掉哪条"。不能因为 E-18 一起关掉。"""
    from services import enrich

    async def by_id(bid):
        return {"bangumi_id": bid, "display_name": "同一部番"}
    monkeypatch.setattr(enrich, "fetch_by_id", by_id)

    with clean_tables.get_session() as s:
        old = Anime(title="已经在追的", season=1, confirmed=True, quarter="26C", bangumi_id=556)
        new_a = Anime(title="刚发现的", season=1, quarter="26C")
        s.add(old); s.add(new_a); s.commit(); s.refresh(old); s.refresh(new_a)
        old_id, new_id = old.id, new_a.id

    await A.bind_anime_bgm(new_id, 556)
    with clean_tables.get_session() as s:
        assert s.get(Anime, old_id) is None, "带回显的绑定路径的合并被一起关掉了"
        assert s.get(Anime, new_id) is not None


# ---------------- (R21) 同一条规矩的【剧场版那一半】 ----------------

async def test_movie_enrich_never_deletes_a_row(clean_tables, monkeypatch, cfg):
    """(R21) 剧场版的识别路径同样【不合并、不删行】。

    R20 只收口了番剧侧，`enrich_movie` 原样保留 `_merge_movie` —— 同一件事有两处、
    只改了一处，本项目第①号形状。`_merge_movie` 的最后一步也是 `s.delete(loser)`。

    ⚠️ R20 曾**回退过** `_upsert_movie` 那一处的同款改动，理由是"那里的 keeper 是按
    bgm_id 查出来的、合并是构造上正确的"。那个判断对 `_upsert_movie` 成立，
    **但这里不是那条路**：`enrich_movie` 的 bgm_id 来自一次全新的 `enrich.resolve`，
    两行"是同一部"从没被证明过。剧场版的续作/重制/总集编彼此极像，
    而"上架日当首映日"的日期校验对它们系统性地判错（那段风险 core/movies.py 自己写着）。
    """
    from core import movies as M
    from db.models import Movie
    from services import enrich

    async def resolve(*a, **kw):
        return {"bangumi_id": 888, "display_name": "同一部剧场版"}
    monkeypatch.setattr(enrich, "resolve", resolve)

    with clean_tables.get_session() as s:
        old = Movie(title="已经下好的", quarter="2024", bangumi_id=888)
        new_m = Movie(title="刚扫到的", quarter="2026")
        s.add(old); s.add(new_m); s.commit(); s.refresh(old); s.refresh(new_m)
        old_id, new_id = old.id, new_m.id

    await M.enrich_movie(new_id)
    with clean_tables.get_session() as s:
        assert s.get(Movie, old_id) is not None, "剧场版识别路径把另一部片删掉了"
        assert s.get(Movie, new_id) is not None


async def test_movie_explicit_bind_still_merges(clean_tables, monkeypatch, cfg):
    """反向：剧场版『绑定 bgm』照常合并（它经 bind_preview + require_bind_confirm，有回显）。"""
    from core import movies as M
    from db.models import Movie
    from services import enrich

    async def by_id(bid):
        return {"bangumi_id": bid, "display_name": "同一部剧场版"}
    monkeypatch.setattr(enrich, "fetch_by_id", by_id)

    with clean_tables.get_session() as s:
        old = Movie(title="已经下好的", quarter="2024", bangumi_id=889)
        new_m = Movie(title="刚扫到的", quarter="2026")
        s.add(old); s.add(new_m); s.commit(); s.refresh(old); s.refresh(new_m)
        old_id, new_id = old.id, new_m.id

    await M.bind_movie_bgm(new_id, 889)
    with clean_tables.get_session() as s:
        assert s.get(Movie, old_id) is None, "带回显的绑定路径的合并被一起关掉了"
        assert s.get(Movie, new_id) is not None


def test_no_identification_path_calls_a_merge():
    """广度守卫：两条线的【识别】函数体里都不许出现 `_merge_*`。

    行为用例各测各的一半，挡不住"以后有人往 enrich_* 里又加回一处合并"。
    这里对两个函数的 AST 一起断言 —— 少写一个就等于那条线没守。
    用 AST 的真实调用节点，不是字符串匹配（两个文件的注释里都写满了 `_merge_anime`
    / `_merge_movie`，按字符串判会被自己的解释判红）。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for mod, fn_name in (("core/anime.py", "enrich_anime"), ("core/movies.py", "enrich_movie")):
        tree = ast.parse((root / mod).read_text(encoding="utf-8"))
        fns = [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name]
        assert fns, f"没找到 {mod}::{fn_name}，用例的前提坏了"
        called = {n.func.id for n in ast.walk(fns[0])
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        bad = {c for c in called if c.startswith("_merge")}
        assert not bad, f"{mod}::{fn_name} 又在识别路径上合并了：{sorted(bad)}"

    # 反向：绑定路径必须【仍然】合并，别把两条一起关掉
    for mod, fn_name in (("core/anime.py", "bind_anime_bgm"), ("core/movies.py", "bind_movie_bgm")):
        tree = ast.parse((root / mod).read_text(encoding="utf-8"))
        fns = [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name]
        assert fns, f"没找到 {mod}::{fn_name}"
        called = {n.func.id for n in ast.walk(fns[0])
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert any(c.startswith("_merge") for c in called), \
            f"{mod}::{fn_name} 的合并被一起关掉了 —— 那条路是有回显的，该保留"


async def test_movie_enrich_does_not_even_write_a_clashing_binding(clean_tables, monkeypatch, cfg):
    """(R22) 识别撞上别人已占的 bgm 时，**连绑定本身都不写**。

    R21 只把这里的"合并删行"去掉了，可**删除动作在 `_upsert_movie` 里原样保留着** ——
    而那一处的立论是 R20 写的「keeper 是按 bgm_id 查出来的、两行本来就声称同一个 subject」。
    一旦识别路径把一个**未经证明**的 bgm_id 写进去，那个立论就变成循环论证：
    下一轮剧场版扫描（自动到点，或用户点一次『扫描』）调 `_upsert_movie`，
    它按这个 bgm_id 查出 keeper、`s.delete` 掉正确的那一行 ——
    **删除只是被推迟了一轮，而且这一次是全自动发生的。**
    """
    from core import movies as M
    from db.models import Movie
    from services import enrich

    async def resolve(*a, **kw):
        return {"bangumi_id": 777001, "display_name": "认错成了这一部"}
    monkeypatch.setattr(enrich, "resolve", resolve)

    with clean_tables.get_session() as s:
        owner = Movie(title="正主（已下好）", quarter="2024", bangumi_id=777001)
        mine = Movie(title="刚扫到的", quarter="2026")
        s.add(owner); s.add(mine); s.commit(); s.refresh(owner); s.refresh(mine)
        owner_id, my_id = owner.id, mine.id

    await M.enrich_movie(my_id)
    with clean_tables.get_session() as s:
        assert s.get(Movie, owner_id) is not None, "正主那一行被删了"
        me = s.get(Movie, my_id)
        assert me.bangumi_id != 777001, (
            "撞车的绑定被写进去了 —— 下一轮扫描的 _upsert_movie 会照着它删掉正主")
        assert me.display_name != "认错成了这一部", "元数据也不该按未经证明的绑定覆盖"


async def test_movie_enrich_still_writes_a_clean_binding(clean_tables, monkeypatch, cfg):
    """反向：没撞车时照常写 —— 别为了消掉那条路把识别整个关掉。"""
    from core import movies as M
    from db.models import Movie
    from services import enrich

    async def resolve(*a, **kw):
        return {"bangumi_id": 777002, "display_name": "认对了"}
    monkeypatch.setattr(enrich, "resolve", resolve)

    with clean_tables.get_session() as s:
        mine = Movie(title="刚扫到的", quarter="2026")
        s.add(mine); s.commit(); s.refresh(mine); my_id = mine.id

    await M.enrich_movie(my_id)
    with clean_tables.get_session() as s:
        me = s.get(Movie, my_id)
        assert me.bangumi_id == 777002 and me.display_name == "认对了"
