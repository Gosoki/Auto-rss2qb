"""『不属于本季』（E-77 C）的三处补漏。

一、同集还有别的看着像别季的行时，确认框把它们列出来、由人选是否一起标（逐条按钮与横幅批量按钮各一份）：
    只标点的那一条的话，槽位一让出，同集待下的别季旧集就是 flush 的候选 —— 前一季的内容被下进本季这个集号，
    本季那一集随后被即时同集闸落成 skipped。core 只标用户在确认框里看过的 id（also），绝不自己按判据去找：
    bgm 首播日记晚 / 先行配信时本季正片也满足那个判据，自动标就是漏集。放回闸照旧只认 _slot_impostor。
    一起标错了（名单里混进了先行配信的本季正片）：对那条点撤销，它若是被同集闸压成的 skipped、同组又没着落，放回待下。
二、别季行与『该集已删』（E-73）的两处交汇：别季行 force 交付成功 / 清扫按已交付落定，都不清本季同集的标记；
    撤销时同集已另有一份本季的，这一行自己的标记不恢复（只动这一行，不整部番重算）。
三、flush 取候选时滤掉别季行：否则优先级更高的别季待下行每轮都被挑中、被交付总闸拒掉，本季同集那条永远轮不到。
"""
import asyncio
import logging
from datetime import date, datetime, timedelta

import pytest

from core import anime as A
from core import engine as ce
from db.models import Anime, AnimeTorrent
from tests.test_r39_foreign_season import _at, _mk, _real_delivery, _row


def _shape(db, d):
    """第 9 集上挂着三条别季旧行：已下的（-141 周，占槽）、待下的备用项（-89 周，占槽）、别的组跳过的（-20 周，只有逐条判据认得出）。"""
    return _mk(db, [
        ("old9", 9, "sent", _at(d, -141)),
        ("old9b", 9, "pending", _at(d, -89), {"raw_title": "[ANi] 百女友 - 09 [1080P][Baha]"}),
        ("old9c", 9, "skipped", _at(d, -20), {"source": "LoliHouse", "raw_title": "[LoliHouse] 百女友 - 09"}),
    ], air=d)


def _arrive_cur9(db, aid):
    with db.get_session() as s:
        t = AnimeTorrent(anime_id=aid, info_hash="f" * 40, source="ANi", site="nyaa", anime_title="百女友", season=1,
                         episode=9.0, status="pending", raw_title="[ANi] 百女友 - 09 [1080P][Baha][V0]",
                         download_url="https://x/cur9.torrent", release_time=datetime.now(), created_at=datetime.now())
        s.add(t)
        s.commit()
        return t.id


# ==================== 一、逐条按钮：列同集兄弟，只标列给人看过的 ====================

def test_marking_with_listed_siblings_leaves_nothing_old_to_download(clean_tables, cfg, monkeypatch, tmp_path):
    """一起标 → 让出的槽位上一条别季旧行都不下，本季那一集来了照常下。"""
    fetched = _real_delivery(cfg, monkeypatch, tmp_path)
    cfg(ANIME_BACKFILL_PREAIR_DAYS=30)
    d = date.today() - timedelta(weeks=7)
    aid, ids = _shape(clean_tables, d)
    sib = A.foreign_suspect_siblings(ids["old9"])
    assert [r["id"] for r in sib] == [ids["old9b"], ids["old9c"]], "确认框要列出的同集别季旧行"
    assert {"raw_title", "status", "release_time"} <= set(sib[0])
    assert A.set_foreign_season(ids["old9"], True, also=[r["id"] for r in sib]) is True
    assert asyncio.run(A.flush_ready_downloads(qb_alive=True)) == 0 and fetched == [], "别季旧集被下进了本季第 9 集"
    assert {k: (_row(clean_tables, v).status, _row(clean_tables, v).foreign_season) for k, v in ids.items()} == {
        "old9": ("sent", True), "old9b": ("pending", True), "old9c": ("skipped", True)}
    cur9 = _arrive_cur9(clean_tables, aid)
    asyncio.run(A.download_anime_torrent(cur9))
    assert _row(clean_tables, cur9).status == "sent", "本季真正的第 9 集被同集闸挡了"
    assert A.foreign_suspect_siblings(ids["old9"]) == [], "都标完了，不该再列"


def test_sibling_list_uses_the_buttons_criterion(clean_tables, cfg):
    """判据就是逐条按钮那一个（foreign_season_suspect）；交付中的、已标的、本季的、别的集的不列；特别篇 / 不存在的 id 返回 []。"""
    cfg(ANIME_BACKFILL_PREAIR_DAYS=30)
    d = date.today() - timedelta(weeks=7)
    aid, ids = _mk(clean_tables, [
        ("old9", 9, "sent", _at(d, -141)),
        ("near", 9, "skipped", _at(d, -20), {"source": "B"}),                 # 只有逐条判据认得出
        ("busy", 9, "downloading", _at(d, -141), {"source": "C"}),            # 交付中：不列
        ("done", 9, "pending", _at(d, -141), {"source": "D", "foreign_season": True}),
        ("cur9", 9, "skipped", _at(d, 8), {"source": "E"}),                   # 本季的
        ("other", 8, "pending", _at(d, -141), {"source": "F"}),               # 别的集
        ("sp", -1, "sent", _at(d, -141), {"source": "G"}),
    ], air=d)
    assert [r["id"] for r in A.foreign_suspect_siblings(ids["old9"])] == [ids["near"]]
    assert A.foreign_suspect_siblings(ids["sp"]) == [] and A.foreign_suspect_siblings(999999) == []


def test_also_ignores_rows_outside_the_clicked_episode(clean_tables, cfg, caplog):
    """also 只认同番同集、没标、不在交付中的；别的集、别的番、交付中的、已标的、不存在的 id、自己一律忽略。"""
    cfg(ANIME_FINISH_UNSUB=False)
    d = date.today() - timedelta(weeks=7)
    aid, ids = _mk(clean_tables, [
        ("old9", 9, "sent", _at(d, -141)),
        ("ep8", 8, "pending", _at(d, -141)),
        ("busy", 9, "downloading", _at(d, -141), {"source": "C"}),
        ("done", 9, "pending", _at(d, -141), {"source": "D", "foreign_season": True}),
    ], air=d)
    oid, oids = _mk(clean_tables, [("x9", 9, "pending", _at(d, -141))], air=d)
    with caplog.at_level(logging.INFO, logger="autorss"):
        assert A.set_foreign_season(ids["old9"], True,
                                    also=[ids["ep8"], ids["busy"], ids["done"], oids["x9"], 999999, ids["old9"]])
    assert _row(clean_tables, ids["old9"]).foreign_season
    assert not any(_row(clean_tables, i).foreign_season for i in (ids["ep8"], ids["busy"], oids["x9"]))
    assert "同集另外" not in caplog.text, "已标的那条又被当成一起标的算了一次"


def test_also_needs_the_clicked_row_to_belong_to_an_anime(clean_tables):
    """没绑番的孤儿行：anime_id 都是 0 不等于同一部，also 整个不生效（与列兄弟时无番返回 [] 同口径）。"""
    ids = []
    with clean_tables.get_session() as s:
        for i in range(2):
            t = AnimeTorrent(anime_id=0, info_hash=f"{i + 77:040x}", source="X", site="nyaa", anime_title=f"番{i}",
                             season=1, episode=3.0, status="pending", raw_title=f"[X] 番{i} - 03",
                             download_url=f"https://x/o{i}.torrent", release_time=datetime.now(),
                             created_at=datetime.now())
            s.add(t)
            s.commit()
            ids.append(t.id)
    assert A.foreign_suspect_siblings(ids[0]) == []
    assert A.set_foreign_season(ids[0], True, also=[ids[1]])
    assert not _row(clean_tables, ids[1]).foreign_season, "另一部（都没绑番）的孤儿行被顺带标了"


def test_core_never_marks_siblings_on_its_own(clean_tables, cfg, monkeypatch, tmp_path):
    """bgm 首播日记晚 / 先行配信：本季第 3 集比 bgm 首播日早 6 周发布、组不写季号 —— 它也满足逐条按钮的判据。
    用户只标了旧行（不给 also =『只标这一条』）：本季那条不许被顺带标上，被压成 skipped 的要照常放回并下掉。"""
    fetched = _real_delivery(cfg, monkeypatch, tmp_path)
    cfg(ANIME_BACKFILL_PREAIR_DAYS=30)
    d = date.today() - timedelta(weeks=2)
    aid, ids = _mk(clean_tables, [
        ("old3", 3, "sent", _at(d, -60)),
        ("cur3", 3, "skipped", _at(d, -6), {"raw_title": "[ANi] 百女友 - 03 [1080P][Baha]"}),
    ], air=d, season=2)
    assert [r["id"] for r in A.foreign_suspect_siblings(ids["old3"])] == [ids["cur3"]], "前提：它会出现在确认框里"
    assert A.set_foreign_season(ids["old3"], True)
    asyncio.run(A.flush_ready_downloads(qb_alive=True))
    c = _row(clean_tables, ids["cur3"])
    assert (c.status, c.foreign_season) == ("sent", False), f"{c.status} foreign={c.foreign_season} {fetched}"


def _late_bgm(db, d, extra=()):
    """bgm 首播日记晚：本季第 3 集比 bgm 首播日早 6 周、季号解析成 1，被同集闸压成 skipped；槽位上是早 60 周的旧行。"""
    return _mk(db, [
        ("old3", 3, "sent", _at(d, -60)),
        ("cur3", 3, "skipped", _at(d, -6), {"raw_title": "[ANi] 百女友 - 03 [1080P][Baha]"}),
        *extra,
    ], air=d, season=2)


def test_undo_of_a_wrongly_marked_skipped_row_puts_it_back(clean_tables, cfg, monkeypatch, tmp_path):
    """确认框名单里混进了本季正片、用户点了『一起标』→ 发现标错、对它点撤销：它当初是被同集闸压成的 skipped，
    原样留着就没有任何自动路径会下它（巡检又算"见过"不报缺集），这一集静默漏掉 —— 撤销要把它放回待下。"""
    fetched = _real_delivery(cfg, monkeypatch, tmp_path)
    cfg(ANIME_BACKFILL_PREAIR_DAYS=30)
    d = date.today() - timedelta(weeks=2)
    aid, ids = _late_bgm(clean_tables, d)
    assert [r["id"] for r in A.foreign_suspect_siblings(ids["old3"])] == [ids["cur3"]], "前提：它在名单里"
    assert A.set_foreign_season(ids["old3"], True, also=[ids["cur3"]])
    assert asyncio.run(A.flush_ready_downloads(qb_alive=True)) == 0
    assert A.set_foreign_season(ids["cur3"], False) is True
    assert asyncio.run(A.flush_ready_downloads(qb_alive=True)) == 1
    c = _row(clean_tables, ids["cur3"])
    assert (c.status, c.foreign_season) == ("sent", False), f"撤销后本季第 3 集卡在 {c.status} {fetched}"


@pytest.mark.parametrize("case", ["covered", "rejected"])
def test_undo_leaves_a_skipped_row_alone_when_the_episode_is_covered(clean_tables, cfg, case):
    """反例：同集已有一份本季的（已下），撤销的那条照旧是跳过 —— 放回只为"这一集没着落"，不是每次撤销都放；
    已忽略的番也不放（那是用户自己压下去的，恢复订阅时自然会放）。"""
    cfg(ANIME_BACKFILL_PREAIR_DAYS=30, ANIME_FINISH_UNSUB=False)
    d = date.today() - timedelta(weeks=2)
    if case == "covered":
        aid, ids = _late_bgm(clean_tables, d, [("cur3b", 3, "sent", _at(d, 1), {"source": "X"})])
    else:
        aid, ids = _late_bgm(clean_tables, d)
        with clean_tables.get_session() as s:
            s.get(Anime, aid).rejected = True
            s.commit()
    assert A.set_foreign_season(ids["old3"], True, also=[ids["cur3"]])
    assert A.set_foreign_season(ids["cur3"], False) is True
    assert _row(clean_tables, ids["cur3"]).status == "skipped"


@pytest.mark.nicegui_main_file("tests/render_detail_main.py")
@pytest.mark.parametrize("label,n_marked", [("连同这 2 条一起标", 3), ("只标这一条", 1), ("取消", 0)])
async def test_detail_page_lists_same_episode_siblings_before_marking(user, clean_tables, cfg, label, n_marked):
    """点『不属于本季』：确认框列出同集另外两条旧行的标题；三个按钮各走各的（取消一条都不标）。
    三条都看着像别季的，点哪一条都列另外两条。"""
    from nicegui import ui
    from nicegui.testing.user_interaction import UserInteraction
    cfg(ANIME_BACKFILL_PREAIR_DAYS=30, ANIME_FINISH_UNSUB=False)
    d = date.today() - timedelta(weeks=7)
    aid, ids = _shape(clean_tables, d)
    await user.open(f"/detail/{aid}")
    btns = [b for b in user.find(kind=ui.button, content="不属于本季").elements if b.text == "不属于本季"]
    assert len(btns) == 3
    UserInteraction(user, {btns[0]}, None).click()
    await user.should_see("让出的位置上还有 2 条看着也像别的季的")
    await user.should_see("[LoliHouse] 百女友 - 09")
    user.find(label).click()
    await asyncio.sleep(0.2)
    marked = {k for k, v in ids.items() if _row(clean_tables, v).foreign_season}
    assert len(marked) == n_marked, marked


# ==================== 一、横幅批量按钮：同款 ====================

def _mixed(db, d, busy=True):
    return _mk(db, [
        ("old9", 9, "sent", _at(d, -141)),
        ("near9", 9, "pending", _at(d, -20), {"source": "B", "raw_title": "[B] 百女友 - 09"}),
        ("near8", 8, "skipped", _at(d, -20), {"source": "B", "raw_title": "[B] 百女友 - 08"}),   # 第 8 集批量不让出
    ] + ([("busy9", 9, "downloading", _at(d, -20), {"source": "C"})] if busy else []), air=d)


def test_bulk_mark_lists_and_takes_the_near_old_rows_on_the_freed_episodes(clean_tables, cfg, monkeypatch, tmp_path):
    """批量让出的那几集里，只有逐条判据认得出的旧行要列出来；also 只认这些集里、没标、不在交付中的。"""
    fetched = _real_delivery(cfg, monkeypatch, tmp_path)
    cfg(ANIME_BACKFILL_PREAIR_DAYS=30)
    d = date.today() - timedelta(weeks=7)
    aid, ids = _mixed(clean_tables, d)
    left = A.foreign_suspect_leftovers(aid)
    assert [r["id"] for r in left] == [ids["near9"]], "只列批量让出的那几集里、批量判据认不出的、不在交付中的"
    assert A.mark_slot_impostors_foreign(aid, also=[ids["near9"], ids["near8"], ids["busy9"], 999999]) == 2
    got = {k: _row(clean_tables, v).foreign_season for k, v in ids.items()}
    assert got == {"old9": True, "near9": True, "near8": False, "busy9": False}, got
    assert A.foreign_suspect_leftovers(aid) == [] and A.foreign_suspect_leftovers(999999) == []

    aid2, ids2 = _mixed(clean_tables, d, busy=False)      # 没有交付中的占着：槽位真的让出来了，flush 也不许下旧行
    assert A.mark_slot_impostors_foreign(aid2, also=[r["id"] for r in A.foreign_suspect_leftovers(aid2)]) == 2
    assert asyncio.run(A.flush_ready_downloads(qb_alive=True)) == 0 and fetched == [], "批量标完后别季旧行被下进了第 9 集"


def test_bulk_leftovers_skip_rows_already_marked(clean_tables, cfg):
    """剩余名单只列还没标的：同集一条近旧行已经逐条标过了，批量确认框不再列它。"""
    cfg(ANIME_BACKFILL_PREAIR_DAYS=30)
    d = date.today() - timedelta(weeks=7)
    aid, ids = _mk(clean_tables, [
        ("old9", 9, "sent", _at(d, -141)),
        ("near9", 9, "pending", _at(d, -20), {"source": "B", "foreign_season": True}),
        ("near9c", 9, "skipped", _at(d, -20), {"source": "C"}),
    ], air=d)
    assert [r["id"] for r in A.foreign_suspect_leftovers(aid)] == [ids["near9c"]]


def test_bulk_mark_without_also_marks_only_the_strict_ones(clean_tables, cfg):
    """不给 also = 只标 _slot_impostor 认得出的（判据宁严勿宽），core 自己不按宽判据多标。"""
    cfg(ANIME_BACKFILL_PREAIR_DAYS=30, ANIME_FINISH_UNSUB=False)
    d = date.today() - timedelta(weeks=7)
    aid, ids = _mixed(clean_tables, d)
    assert A.mark_slot_impostors_foreign(aid) == 1
    assert not _row(clean_tables, ids["near9"]).foreign_season


@pytest.mark.nicegui_main_file("tests/render_detail_main.py")
@pytest.mark.parametrize("label,expect", [("连同这 1 条一起标", {"old9", "near9"}), ("只标占槽的 1 条", {"old9"})])
async def test_detail_page_bulk_button_lists_the_near_old_rows(user, clean_tables, cfg, label, expect):
    """横幅批量按钮弹出的确认框列出批量判据认不出的那条旧行；两个按钮各走各的。"""
    cfg(ANIME_BACKFILL_PREAIR_DAYS=30, ANIME_FINISH_UNSUB=False, ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    d = date.today() - timedelta(weeks=7)
    aid, ids = _mixed(clean_tables, d)
    await user.open(f"/detail/{aid}")
    user.find("把占槽的 1 条旧种子标为不属于本季").click()
    await user.should_see("让出的位置上还有 1 条看着也像别的季的")
    await user.should_see("[B] 百女友 - 09")
    user.find(label).click()
    await asyncio.sleep(0.2)
    assert {k for k, v in ids.items() if _row(clean_tables, v).foreign_season} == expect


# ==================== 二、别季行与『该集已删』 ====================

def _dropped5(db, d, old_status):
    """第 5 集：别季行 old5（已标）+ 本季 cur5（用户删了最后一份 → 该集已删）+ 本季 cur5b（待下）。"""
    return _mk(db, [
        ("old5", 5, old_status, _at(d, -146),
         {"foreign_season": True, **({"prev_status": "pending"} if old_status == "downloading" else {})}),
        ("cur5", 5, "deleted", _at(d, 4), {"ep_dropped": True}),
        ("cur5b", 5, "pending", _at(d, 4), {"source": "LoliHouse", "raw_title": "[LoliHouse] 百女友 - 05"}),
    ], air=d)


def test_force_on_a_foreign_row_keeps_this_seasons_dropped_flag(clean_tables, cfg, monkeypatch, tmp_path):
    _real_delivery(cfg, monkeypatch, tmp_path)
    d = date.today() - timedelta(weeks=17)
    aid, ids = _dropped5(clean_tables, d, "pending")
    assert A.download_plan(aid) == set(), "前提：本季第 5 集已删，不自动下"
    assert asyncio.run(A.download_anime_torrent(ids["old5"], force=True)) is True
    assert _row(clean_tables, ids["cur5"]).ep_dropped, "别季行的 force 成功把本季第 5 集的『该集已删』清掉了"
    assert A.download_plan(aid) == set(), "本季第 5 集又会被自动下回来"


def test_sweep_settling_a_foreign_row_keeps_this_seasons_dropped_flag(clean_tables, cfg, monkeypatch, tmp_path):
    """清扫那一处的对称用例：别季行 force 交付的残骸其实已在 qB 里 → 按已交付落定，本季的标记仍在。"""
    _real_delivery(cfg, monkeypatch, tmp_path)
    d = date.today() - timedelta(weeks=17)
    aid, ids = _dropped5(clean_tables, d, "downloading")
    h = _row(clean_tables, ids["old5"]).info_hash

    async def info(hs):
        return {h: {"state": "downloading", "progress": 0.1, "save_path": str(tmp_path)}}
    monkeypatch.setattr(ce.qb, "torrents_info", info)
    assert asyncio.run(ce.sweep_stale_delivering()) == 1
    assert _row(clean_tables, ids["old5"]).status == "sent"
    assert _row(clean_tables, ids["cur5"]).ep_dropped, "别季行的残骸落定把本季第 5 集的『该集已删』清掉了"
    assert A.download_plan(aid) == set()


def test_undo_does_not_restore_a_dropped_flag_over_a_live_copy(clean_tables, cfg, monkeypatch, tmp_path):
    """早先删掉的占槽旧行被判成该集已删 → 标成别季 → 本季那集下到 → 撤销：同集已有一份本季的，这一行的标记不恢复；
    否则那份日后死了（落 error），_covered 仍认这一集有着落，再也补不回来。"""
    _real_delivery(cfg, monkeypatch, tmp_path)
    d = date.today() - timedelta(weeks=17)
    aid, ids = _mk(clean_tables, [
        ("old5", 5, "deleted", _at(d, -146), {"ep_dropped": True}),
        ("cur5", 5, "skipped", _at(d, 4)),
        ("cur5b", 5, "pending", _at(d, 4), {"source": "LoliHouse", "raw_title": "[LoliHouse] 百女友 - 05",
                                            "priority": -1}),
    ], air=d)
    assert A.set_foreign_season(ids["old5"], True)
    assert asyncio.run(A.flush_ready_downloads(qb_alive=True)) == 1
    assert _row(clean_tables, ids["cur5"]).status == "sent"
    assert A.set_foreign_season(ids["old5"], False) is True
    assert _row(clean_tables, ids["old5"]).ep_dropped is False
    with clean_tables.get_session() as s:
        s.get(AnimeTorrent, ids["cur5"]).status = "error"      # 本季那份后来死了
        s.commit()
    assert A.download_plan(aid) == {ids["cur5b"]}, "旧的『该集已删』标记把这一集挡死了（补不回来）"


def test_undo_then_remark_keeps_the_dropped_flag_of_another_row(clean_tables, cfg, monkeypatch, tmp_path):
    """撤销只动被撤销的这一行：同集一条已标别季的 sent 被撤销（重新算一份）再标回，本季那条用户亲手删出来的
    『该集已删』不许被顺手清掉 —— 否则本季这一集被自动下回来，自动化撤销了用户"这一集不要了"的决定。"""
    _real_delivery(cfg, monkeypatch, tmp_path)
    d = date.today() - timedelta(weeks=17)
    aid, ids = _dropped5(clean_tables, d, "sent")
    assert A.download_plan(aid) == set(), "前提：本季第 5 集已删，不自动下"
    assert A.set_foreign_season(ids["old5"], False)
    assert A.set_foreign_season(ids["old5"], True)
    assert _row(clean_tables, ids["cur5"]).ep_dropped, "撤销再标回的一来一回，把本季第 5 集的『该集已删』清掉了"
    assert A.download_plan(aid) == set()


# ==================== 三、别季待下行不饿死本季同集 ====================

def test_foreign_pending_does_not_starve_this_seasons_sibling(clean_tables, cfg, monkeypatch, tmp_path):
    """同一集：别季行（已标、优先级更高）与本季行都待下 —— flush 下的必须是本季那条。
    取候选时不滤别季行的话，每轮都挑中优先级高的别季行、被交付总闸拒掉（不写状态，下一轮输入一样），本季那条永远轮不到。"""
    fetched = _real_delivery(cfg, monkeypatch, tmp_path)
    d = date.today() - timedelta(weeks=5)
    aid, ids = _mk(clean_tables, [("old", 3, "pending", _at(d, -150), {"priority": 9, "foreign_season": True}),
                                  ("cur", 3, "pending", _at(d, 2), {"source": "X", "priority": 1})], air=d)
    assert A.download_plan(aid) == {ids["cur"]}
    assert asyncio.run(A.flush_ready_downloads(qb_alive=True)) == 1, "别季行抢了这一集的名额，本季那条没下"
    assert (_row(clean_tables, ids["cur"]).status, _row(clean_tables, ids["old"]).status) == ("sent", "pending")
    assert len(fetched) == 1
