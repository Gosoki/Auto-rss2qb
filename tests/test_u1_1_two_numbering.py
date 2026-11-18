"""(U1-1) 两套集号同时在下：横幅 + 『只下 X』。

判据（core.anime.two_numberings）在 2026-11-19 生产副本上回放 5 中 5、零误报（scratchpad r40/prodcopy/U1-1_REPLAY.md）。
真阳性形状照着生产那 5 部造；易误报形状照着回放里被挡掉的那几部造（单源跨过总集数、字幕组同号落后、
季内组只在待下、旧集整批补发、已锁源、O<T 歧义段）。
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from nicegui import ui
from nicegui.testing.user_interaction import UserInteraction

import config
import db

db.init_db()
config.load_from_db()
db.apply_configured_backend()

W0 = datetime(2026, 10, 7, 17, 15)
_n = iter(range(1, 10 ** 6))


def _anime(s, **kw):
    from db.models import Anime
    base = dict(title="归还者", display_name="归还者 第二季", season=2, quarter="26D",
                confirmed=True, rejected=False, bangumi_id=777, total_episodes=12)
    base.update(kw)
    a = Anime(**base)
    s.add(a); s.commit(); s.refresh(a)
    return a.id


def _row(s, aid, src, ep, rt, status="sent", **kw):
    from db.models import AnimeTorrent
    n = next(_n)
    base = dict(anime_id=aid, info_hash=f"{n:040x}", source=src, site="nyaa", season=2, episode=float(ep),
                raw_title=f"[{src}] 归还者 - {ep} #{n}", status=status, download_url=f"https://x/{n}.torrent",
                created_at=rt, release_time=rt)
    base.update(kw)
    s.add(AnimeTorrent(**base)); s.commit()


def _weekly(s, aid, src, eps, start, lag=timedelta(0), **kw):
    for i, ep in enumerate(eps):
        _row(s, aid, src, ep, start + timedelta(weeks=i) + lag, **kw)


def _returner(s, **kw):
    """#132 的形状：ANi 13–18 周更，Nix-Raws 2–6 晚 8 小时（ANi 14 ↔ Nix 2），两边都已交付。"""
    aid = _anime(s, **kw)
    _weekly(s, aid, "ANi", range(13, 19), W0)
    _weekly(s, aid, "Nix-Raws", range(2, 7), W0 + timedelta(weeks=1), lag=timedelta(hours=8))
    return aid


def _hits():
    from core import anime
    return anime.two_numberings()


# ---------------------------------------------------------------- 真阳性

def test_returner_shape_is_reported(clean_tables):
    with clean_tables.get_session() as s:
        aid = _returner(s)
    h = _hits()[aid]
    assert (h["abs_src"], h["in_src"], h["offset"]) == ("ANi", "Nix-Raws", 12), h
    assert h["abs_eps"] == list(range(13, 19)) and h["in_eps"] == list(range(2, 7))
    assert h["pairs"] == 5


def test_fansub_a_few_days_late_with_large_offset_is_reported(clean_tables):
    """#140 / #121 的形状：季内组晚 2~5 天、偏移等于或远大于总集数。"""
    with clean_tables.get_session() as s:
        a = _anime(s, total_episodes=13)
        _weekly(s, a, "ANi", range(14, 20), W0)
        for i, late in enumerate((4, 2, 4, 3, 5)):
            _row(s, a, "桜都字幕组", i + 1, W0 + timedelta(weeks=i, days=late))
        b = _anime(s, title="五叶草", display_name="黑色五叶草 第二季", bangumi_id=778, total_episodes=13)
        _weekly(s, b, "ANi", range(171, 178), W0)
        _weekly(s, b, "Nix-Raws", range(3, 8), W0 + timedelta(weeks=2), lag=timedelta(hours=8))
    hits = _hits()
    assert hits[a]["offset"] == 13 and hits[a]["in_src"] == "桜都字幕组", hits
    assert hits[b]["offset"] == 170, hits


# ---------------------------------------------------------------- 易误报形状（都不报）

def test_one_group_counting_past_total_is_not_two_numberings(clean_tables):
    """#86/#98：一个组从 1 连续数过总集数，只是跨季累计编号，每集只有一个号。"""
    with clean_tables.get_session() as s:
        aid = _anime(s)
        _weekly(s, aid, "ANi", range(1, 25), W0)
        _weekly(s, aid, "Nix-Raws", range(16, 20), W0 + timedelta(weeks=15), status="pending")
    assert aid not in _hits()


def test_same_numbering_fansub_lagging_weeks_is_not_reported(clean_tables):
    """bgm 总集数少记、字幕组同号落后 8 周：偏移 8 < 总集数，不是两套编号。"""
    with clean_tables.get_session() as s:
        aid = _anime(s)
        _weekly(s, aid, "ANi", range(13, 19), W0)
        _weekly(s, aid, "喵萌", range(5, 11), W0)
    assert aid not in _hits()


def test_seasonal_group_only_pending_is_not_reported(clean_tables):
    """#140 的 Nix-Raws：只入库没交付，"同时在下"不成立。"""
    with clean_tables.get_session() as s:
        aid = _anime(s)
        _weekly(s, aid, "ANi", range(13, 19), W0)
        _weekly(s, aid, "Nix-Raws", range(1, 7), W0, lag=timedelta(hours=8), status="pending")
    assert aid not in _hits()


def test_old_batch_released_on_one_day_is_not_reported(clean_tables):
    """#8 绿茶字幕组：06/07/08 同一天整批补发，只能与周更组碰上一集。"""
    with clean_tables.get_session() as s:
        aid = _anime(s, total_episodes=13)
        _weekly(s, aid, "Nix-Raws", range(16, 24), W0)
        for ep in (6, 7, 8):
            _row(s, aid, "绿茶字幕组", ep, W0 + timedelta(weeks=2, days=1))
    assert aid not in _hits()


@pytest.mark.parametrize("kw", [dict(pref_source="ANi"), dict(rejected=True), dict(confirmed=False)],
                         ids=["已锁源", "已忽略", "待确认"])
def test_locked_or_not_tracking_is_not_reported(clean_tables, kw):
    with clean_tables.get_session() as s:
        aid = _returner(s, **kw)
    assert aid not in _hits()


def test_rows_marked_not_this_season_do_not_count(clean_tables):
    from db.models import AnimeTorrent
    from sqlmodel import select
    with clean_tables.get_session() as s:
        aid = _returner(s)
        for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.source == "Nix-Raws")):
            t.foreign_season = True
            s.add(t)
        s.commit()
    assert aid not in _hits()


def test_ambiguous_offset_below_total_is_left_alone(clean_tables):
    """#106 杀手旅店：O=13 < T=15，ANi 14、15 落在 1..T 里。真两套编号，但与 D1 同口径有意不报。"""
    with clean_tables.get_session() as s:
        aid = _anime(s, total_episodes=15)
        _weekly(s, aid, "ANi", range(14, 21), W0)
        _weekly(s, aid, "Nix-Raws", range(2, 8), W0 + timedelta(weeks=1), lag=timedelta(hours=8))
    assert aid not in _hits()


def test_thresholds_come_from_settings(clean_tables, cfg):
    with clean_tables.get_session() as s:
        aid = _returner(s)
    cfg(ANIME_TWO_NUMBERING_PAIRS=6)
    assert aid not in _hits(), "只对上 5 集，要求 6 集时不该报"
    cfg(ANIME_TWO_NUMBERING_PAIRS=3, ANIME_TWO_NUMBERING_DAYS=1)
    assert aid in _hits()
    cfg(ANIME_TWO_NUMBERING_DAYS=0)          # 越界读回时钳到 1，不会变成"永远对不上"或负窗口
    assert config.ANIME_TWO_NUMBERING_DAYS == 1


# ---------------------------------------------------------------- 仪表盘发现

def test_finding_text_lock_targets_and_stable_fact(clean_tables):
    from core import alerts
    with clean_tables.get_session() as s:
        aid = _returner(s)
    f = [x for x in alerts.scan() if x["kind"] == "two"]
    assert len(f) == 1, f
    f = f[0]
    assert f["open"] == [aid] and f["lock"] == ["ANi", "Nix-Raws"]
    assert f"#{aid}" in f["text"] and "两套集号同时在下" in f["text"] and "k+12" in f["text"]
    assert "ANIME_" not in f["text"]
    with clean_tables.get_session() as s:      # 下周各来一集：事实不变，读过的不复活
        _row(s, aid, "ANi", 19, W0 + timedelta(weeks=6))
        _row(s, aid, "Nix-Raws", 7, W0 + timedelta(weeks=6, hours=8))
    g = [x for x in alerts.scan() if x["kind"] == "two"][0]
    assert g["ident"] == f["ident"] and g["fact"] == f["fact"]


# ---------------------------------------------------------------- 界面

def _live(user, kind):
    return [e for e in user.find(kind).elements if not e.is_deleted]


def _click(user, el):
    UserInteraction(user, {el}, None).click()


async def _until(pred, what):
    for _ in range(60):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(what)


def _pref(clean_tables, aid):
    from db.models import Anime
    with clean_tables.get_session() as s:
        return s.get(Anime, aid).pref_source


@pytest.mark.parametrize("ok", [True, False], ids=["确认", "取消"])
@pytest.mark.nicegui_main_file("tests/render_detail_main.py")
async def test_detail_banner_locks_only_after_confirm(user, clean_tables, monkeypatch, ok):
    from pages import anime_detail as D
    with clean_tables.get_session() as s:
        aid = _returner(s)
    asked = []

    async def fake_confirm(title, note, **kw):
        asked.append((title, note)); return ok
    monkeypatch.setattr(D, "confirm", fake_confirm)
    await user.open(f"/detail/{aid}")
    await user.should_see("两套集号同时在下")
    assert not [e for e in _live(user, ui.label) if "集号超出 bgm 记载" in (e.text or "")], \
        "坐实了两套编号还挂着那条『请你自己判断』的通用横幅"
    btns = {b.text: b for b in _live(user, ui.button) if (b.text or "").startswith("只下 ")}
    assert set(btns) == {"只下 ANi", "只下 Nix-Raws"}, set(btns)
    _click(user, btns["只下 Nix-Raws"])
    await _until(lambda: asked, "点『只下』没有弹确认框")
    title, note = asked[0]
    assert "Nix-Raws" in title and "ANi" in note and "不再自动下" in note and "不动" in note, (title, note)
    await asyncio.sleep(0.1)
    assert _pref(clean_tables, aid) == ("Nix-Raws" if ok else None)


@pytest.mark.nicegui_main_file("tests/render_main.py")
async def test_dashboard_banner_locks_and_disappears(user, clean_tables, monkeypatch, cfg):
    from pages import anime_detail as D
    cfg(QB_ENABLED=False)
    with clean_tables.get_session() as s:
        aid = _returner(s)

    async def yes(title, note, **kw):
        return True
    monkeypatch.setattr(D, "confirm", yes)
    await user.open("/?t=overview")
    await user.should_see("两套集号同时在下")
    btn = next(b for b in _live(user, ui.button) if b.text == "只下 ANi")
    _click(user, btn)
    await _until(lambda: _pref(clean_tables, aid) == "ANi", "点了『只下 ANi』并确认，却没锁上")
    await _until(lambda: not [e for e in _live(user, ui.label) if "两套集号同时在下" in (e.text or "")],
                 "锁了源横幅还在")


@pytest.mark.nicegui_main_file("tests/render_main.py")
async def test_dashboard_groups_several_hits_into_one_card(user, clean_tables, monkeypatch, cfg):
    """两部以上同时命中时合成一张卡（生产一次 5 部，各挂一整条会把仪表盘淹掉）：说明只出一次，
    每部一行带自己的『只下 X』；在行里锁一部，只有那一部消失。"""
    from pages import anime_detail as D
    cfg(QB_ENABLED=False)
    with clean_tables.get_session() as s:
        a1 = _returner(s)
        a2 = _returner(s, title="第二部", display_name="第二部 第二季", bangumi_id=778)

    async def yes(title, note, **kw):
        return True
    monkeypatch.setattr(D, "confirm", yes)
    await user.open("/?t=overview")
    await user.should_see("2 部番两套集号同时在下")
    heads = [e for e in _live(user, ui.label) if "两套集号同时在下" in (e.text or "")]
    assert len(heads) == 1, [e.text for e in heads]
    rows = [e for e in _live(user, ui.label) if (e.text or "").startswith("『") and "季内第 k 集" in e.text]
    assert {f"#{a1}" in r.text or f"#{a2}" in r.text for r in rows} == {True} and len(rows) == 2
    btn = [b for b in _live(user, ui.button) if b.text == "只下 ANi"]
    assert len(btn) == 2
    _click(user, btn[0])
    await _until(lambda: sum(_pref(clean_tables, i) == "ANi" for i in (a1, a2)) == 1, "行里的『只下 ANi』没锁上")
    await _until(lambda: len([e for e in _live(user, ui.label)
                              if (e.text or "").startswith("『") and "季内第 k 集" in e.text]) <= 1,
                 "锁了一部，合成卡里它那一行还在")
