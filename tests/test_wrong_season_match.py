"""(R14) bgm 匹配到了错误的季 → 降级成『待确认』，不掉进任何自动结论。

真实案例：`[百冬练习组&LoliHouse] Re:从零开始的异世界生活 … - 78` 标题没有季标记 →
season=1 → 两个候选名【一致】命中 bgm 140001『第一季』(2016-04-03, 26 集) → 不是平票、
直接绑上 → air_date 2016 早于开始使用日 → 整部番被判『超期忽略』，一集不下，
而界面上它和用户手动拒绝的番长得一模一样。正确答案是『第四季 夺还篇』(绝对第 78 集 = 该季第 1 集)。
"""
from datetime import datetime

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
