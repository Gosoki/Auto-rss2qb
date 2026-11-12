"""(R40 ui-detail) 番剧 / 剧场版详情弹窗：要处理的事看得见、说得清、点了有用。

钉的是：
· U2-1 告警横幅在元信息卡上面，且不随『版本』关键词的分集区刷新被重建（横幅上的按钮不被吞点击）；
· U2-4 『集号超出』在已锁源时降成一行灰字，不再占琥珀块；
· U2-3 简介默认收成 3 行，点一下展开 / 再点收起；
· U2-5 删除确认框说的就是被点的那一行（集号、组名、原名），不是循环里别的行；
· U2-6 版本关键词一条都没命中时有横幅和红色提示，判据先按锁定源筛、只数正集；
· U2-7 失败 / 停滞 / 重试中的原因印在行上，而不给那几类"不会自动重发"的行印假承诺；
· U2-8 『跳过』有气泡；同集内已有一份的排第一，不跨集打乱；
· U2-9 下载源下拉锁定后显示『只下 X』而存的仍是裸组名；『自动补齐』排在『补齐该源』前、后者降灰；
· U2-11 『下载』按钮置灰的行 == core 对 force 也会短路的行（反向钉：置灰的行确实下不了）；
· U2-12 晚重发提醒指向详情页真有、且做得成的动作（『不属于本季』）；
· U4-5 手机上详情底部有『关闭』（sm:hidden），点了关掉弹窗；不传 on_close 时没有它。
"""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from nicegui import ui
from nicegui.elements.tooltip import Tooltip
from nicegui.testing.user_interaction import UserInteraction

_MAIN = "tests/render_detail_main.py"
_PAGES = "tests/render_main.py"
OLD = datetime.now() - timedelta(days=30)


# ---------------------------------------------------------------- 工具（与 test_r39_ui_detail 同写法）

def _fire(user, el, event):
    """按浏览器的方式投递事件：只带元素 id 与监听 id，由 Client.handle_event 去找元素（删了就丢）。"""
    lids = [lid for lid, lis in el._event_listeners.items() if lis.type == event]
    assert lids, f"{el} 上没有 {event} 监听，用例的前提坏了"
    for lid in lids:
        user.client.handle_event({"id": el.id, "listener_id": lid, "args": []})


async def _until(pred, what):
    for _ in range(60):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(what)


def _live(user, kind):
    return [e for e in user.find(kind).elements if not e.is_deleted]


def _labels(user, needle):
    return [e for e in _live(user, ui.label) if needle in (e.text or "")]


def _kw_input(user):
    ins = [e for e in _live(user, ui.input) if e.props.get("label") == "版本"]
    assert len(ins) == 1, len(ins)
    return ins[0]


def _ep_header(user):
    ls = [e for e in _live(user, ui.label) if (e.text or "").startswith("分集 / 种子")]
    assert len(ls) == 1, len(ls)
    return ls[0]


def _row_of(el):
    """el 所在那一条种子行（ui.column：集号行 + 原名行 [+ 原因行]）。"""
    return next(a for a in el.ancestors() if isinstance(a, ui.column))


def _row_text(col) -> str:
    return " ".join(e.text for e in col.descendants() if isinstance(e, ui.label))


def _tooltip(user, el) -> str:
    tips = [t for t in user.find(Tooltip).elements if t.props.get("target") == f"#{el.html_id}"]
    return tips[0].text if tips else ""


_n = iter(range(1, 10 ** 6))


def _anime(s, **kw):
    from db.models import Anime
    base = dict(title="某番", display_name="某番", jp_name="ボウバン", season=1, quarter="26C",
                confirmed=True, rejected=False, bangumi_id=4242, total_episodes=12)
    base.update(kw)
    a = Anime(**base)
    s.add(a); s.commit(); s.refresh(a)
    return a


def _row(s, aid, src, ep, status="pending", raw=None, **kw):
    from db.models import AnimeTorrent
    n = next(_n)
    base = dict(anime_id=aid, info_hash=f"{n:040x}", source=src, site="nyaa", season=1, episode=float(ep),
                raw_title=raw or f"[{src}] 某番 - {int(ep) if ep >= 0 else 0:02d} [1080p] #{n}", status=status,
                download_url=f"https://x/{n}.torrent", created_at=OLD, release_time=OLD)
    base.update(kw)
    t = AnimeTorrent(**base)
    s.add(t); s.commit(); s.refresh(t)
    return t.id


# ---------------------------------------------------------------- U2-1 / U2-4 横幅位置与锁源后降级

def _conflict_anime(clean_tables, pref_source=None):
    """total=12，第 1 集源 A、第 13 集源 B → 必出『集号超出 bgm 记载』。"""
    with clean_tables.get_session() as s:
        aid = _anime(s, pref_source=pref_source).id
        _row(s, aid, "A", 1, raw="[A] 某番 - 01 [繁日]")
        _row(s, aid, "B", 13, raw="[B] 某番 - 13 [繁日]")
    return aid


@pytest.mark.nicegui_main_file(_MAIN)
async def test_banners_sit_above_the_meta_card_and_survive_a_keyword_refresh(user, clean_tables):
    aid = _conflict_anime(clean_tables)
    await user.open(f"/detail/{aid}")
    order = [e for e in user.client.layout.descendants() if isinstance(e, ui.label)]
    ban = [e for e in order if "集号超出" in (e.text or "")]
    jp = [e for e in order if e.text == "日文"]
    assert len(ban) == 1 and len(jp) == 1, (len(ban), len(jp))
    assert order.index(ban[0]) < order.index(jp[0]), "告警横幅排在元信息卡后面：打开详情首屏看不见它"

    header, kw = _ep_header(user), _kw_input(user)
    kw.value = "繁日"
    _fire(user, kw, "blur")
    await _until(lambda: header.is_deleted, "blur 之后分集区没有重建，用例前提坏了")
    assert not ban[0].is_deleted, "横幅还在分集区里：改关键词就把它连同上面的按钮一起重建了"


@pytest.mark.parametrize("pref", [None, "A"], ids=["未锁源", "已锁A"])
@pytest.mark.nicegui_main_file(_MAIN)
async def test_numbering_conflict_turns_grey_once_a_source_is_locked(user, clean_tables, pref):
    aid = _conflict_anime(clean_tables, pref_source=pref)
    await user.open(f"/detail/{aid}")
    ls = _labels(user, "集号超出")
    assert len(ls) == 1, [e.text for e in ls]
    cls = ls[0].classes
    if pref is None:
        assert "text-amber-400" in cls, cls
    else:
        assert "text-gray-400" in cls and "text-amber-400" not in cls, cls
        assert "已锁定『A』" in ls[0].text and "别的组的不会自动下" in ls[0].text, ls[0].text


# ---------------------------------------------------------------- U2-3 简介默认收起

@pytest.mark.nicegui_main_file(_MAIN)
async def test_summary_is_clamped_and_toggles_on_click(user, clean_tables):
    summary = "第一段简介。\n[简介原文]\n日本語のあらすじ。\n第四行\n第五行"
    with clean_tables.get_session() as s:
        aid = _anime(s, summary=summary).id
    await user.open(f"/detail/{aid}")
    lab = next(e for e in _live(user, ui.label) if e.text == summary)
    assert "line-clamp-3" in lab.classes, lab.classes
    _fire(user, lab, "click")
    await _until(lambda: "line-clamp-3" not in lab.classes, "点一下没有展开")
    _fire(user, lab, "click")
    await _until(lambda: "line-clamp-3" in lab.classes, "再点一下没有收起")


# ---------------------------------------------------------------- U2-5 删除确认框说清是哪一行

@pytest.mark.parametrize("archived", [False, True], ids=["在qB里", "已归档"])
@pytest.mark.nicegui_main_file(_MAIN)
async def test_anime_delete_confirm_names_the_clicked_row(user, clean_tables, monkeypatch, archived):
    from pages import anime_detail as D
    with clean_tables.get_session() as s:
        aid = _anime(s).id
        arch = datetime.now() if archived else None
        _row(s, aid, "ANi", 2, status="sent", raw="[ANi] 某番 - 02 原名二", archived_at=arch)
        _row(s, aid, "喵萌", 3, status="sent", raw="[喵萌] 某番 - 03 原名三", archived_at=arch)
    asked = []

    async def fake_confirm(title, note, **kw):
        asked.append((title, note)); return False

    async def boom(*a, **k):
        raise AssertionError("用户没点确定，却删了")
    monkeypatch.setattr(D, "confirm", fake_confirm)
    monkeypatch.setattr(D.anime, "delete_anime_torrent", boom)
    await user.open(f"/detail/{aid}")
    dels = [b for b in _live(user, ui.button) if b.props.get("icon") == "delete_forever"]
    btn = next(b for b in dels if "原名三" in _row_text(_row_of(b)))
    _fire(user, btn, "click")
    await _until(lambda: asked, "点删除没有弹确认框")
    title, note = asked[0]
    assert "第3集" in title and "喵萌" in title and "第2集" not in title, title
    assert note.splitlines()[0] == "[喵萌] 某番 - 03 原名三", note
    assert "原名二" not in note, note


@pytest.mark.nicegui_main_file(_MAIN)
async def test_movie_delete_confirm_starts_with_the_clicked_versions_raw_title(user, clean_tables, monkeypatch):
    from db.models import Movie, MovieTorrent
    from pages import movies as M
    with clean_tables.get_session() as s:
        m = Movie(title="某片", display_name="某片", quarter="26A", bangumi_id=2)
        s.add(m); s.commit(); s.refresh(m)
        for i, raw in enumerate(["[甲] 某片 BD 1080p", "[乙] 某片 WEB 720p"]):
            s.add(MovieTorrent(movie_id=m.id, info_hash=f"{i + 7:040x}", raw_title=raw, status="sent"))
        s.commit()
        mid = m.id
    asked = []

    async def fake_confirm(title, note, **kw):
        asked.append((title, note)); return False
    monkeypatch.setattr(M, "confirm", fake_confirm)
    await user.open(f"/mdetail/{mid}")
    dels = [b for b in _live(user, ui.button) if b.props.get("icon") == "delete_forever"]
    btn = next(b for b in dels if "WEB 720p" in _row_text(_row_of(b)))
    _fire(user, btn, "click")
    await _until(lambda: asked, "点删除没有弹确认框")
    assert asked[0][1].splitlines()[0] == "[乙] 某片 WEB 720p", asked[0]


# ---------------------------------------------------------------- U2-6 版本关键词零命中

def _kw_anime(clean_tables, rows, pref_source=None, pref_keyword=None):
    with clean_tables.get_session() as s:
        aid = _anime(s, pref_source=pref_source, pref_keyword=pref_keyword).id
        for src, ep, raw in rows:
            _row(s, aid, src, ep, raw=raw)
    return aid


async def _set_kw(user, value):
    header, kw = _ep_header(user), _kw_input(user)
    kw.value = value
    _fire(user, kw, "blur")
    await _until(lambda: header.is_deleted, "blur 之后分集区没有重建")


def _kw_banner(user, kw):
    """U2-6 那条横幅（以『版本关键词『…』』开头；keyword_blocked_episodes 那条文案里也有这几个字，但不在开头）。"""
    return [e for e in _live(user, ui.label) if (e.text or "").startswith(f"版本关键词『{kw}』")]


@pytest.mark.nicegui_main_file(_MAIN)
async def test_keyword_with_no_hit_shows_a_banner_and_a_red_toast(user, clean_tables, cfg):
    cfg(ANIME_LOCK_LAG_DAYS=0)   # 关掉 keyword_blocked_episodes 那条（要等 N 天的），只看本条
    aid = _kw_anime(clean_tables, [("喵萌", 1, "[喵萌] 某番 - 01 [CHT]"), ("喵萌", 2, "[喵萌] 某番 - 02 [CHT]")])
    await user.open(f"/detail/{aid}")
    await _set_kw(user, "繁日")
    assert len(_kw_banner(user, "繁日")) == 1, "填了一条都不命中的关键词，分集区没有横幅"
    await user.should_see("没有一条含这个词")
    assert any("已保存，但现有 2 条种子里没有一条含这个词" in m for m in user.notify.messages), user.notify.messages
    await _set_kw(user, "CHT")
    assert not _kw_banner(user, "繁日") and not _kw_banner(user, "CHT")
    await user.should_see("2 条种子里 2 条含此词")


@pytest.mark.nicegui_main_file(_MAIN)
async def test_keyword_hitting_one_row_shows_no_banner(user, clean_tables, cfg):
    cfg(ANIME_LOCK_LAG_DAYS=0)
    aid = _kw_anime(clean_tables, [("喵萌", 1, "[喵萌] 某番 - 01 [CHT]"), ("喵萌", 2, "[喵萌] 某番 - 02 [繁日]")],
                    pref_keyword="繁日")
    await user.open(f"/detail/{aid}")
    assert not _kw_banner(user, "繁日")


@pytest.mark.parametrize("case", ["锁源交叉", "只有特别篇命中", "只有不属于本季的行命中"])
@pytest.mark.nicegui_main_file(_MAIN)
async def test_keyword_hits_outside_what_would_auto_download_do_not_count(user, clean_tables, cfg, case):
    """锁了 ANi、只有别的组的行含词；只有特别篇（-1）含词；或只有标了『不属于本季』的行含词：
    实际一集都不会自动下，横幅要出。"""
    cfg(ANIME_LOCK_LAG_DAYS=0)
    if case == "锁源交叉":
        aid = _kw_anime(clean_tables, [("ANi", 1, "[ANi] 某番 - 01 [CHT]"), ("喵萌", 1, "[喵萌] 某番 - 01 [繁日]")],
                        pref_source="ANi", pref_keyword="繁日")
        scope = "『ANi』的 1 条"
    elif case == "只有不属于本季的行命中":
        aid = _kw_anime(clean_tables, [("喵萌", 1, "[喵萌] 某番 - 01 [CHT]")], pref_keyword="繁日")
        with clean_tables.get_session() as s:
            _row(s, aid, "喵萌", 1, raw="[喵萌] 某番 - 01 [繁日] 旧季", foreign_season=True)
        scope = "现有 1 条"
    else:
        aid = _kw_anime(clean_tables, [("喵萌", 1, "[喵萌] 某番 - 01 [CHT]"), ("喵萌", -1, "[喵萌] 某番 SP [繁日]")],
                        pref_keyword="繁日")
        scope = "现有 1 条"
    await user.open(f"/detail/{aid}")
    ban = _kw_banner(user, "繁日")
    assert len(ban) == 1 and scope in ban[0].text, [e.text for e in ban]


@pytest.mark.nicegui_main_file(_MAIN)
async def test_keyword_with_nothing_comparable_keeps_the_plain_toast_and_no_banner(user, clean_tables, cfg):
    """库里没有可比的正集（只有特别篇）时说不出"几条里几条"：不出横幅、不弹红色提示，提示保留原句。"""
    cfg(ANIME_LOCK_LAG_DAYS=0)
    aid = _kw_anime(clean_tables, [("喵萌", -1, "[喵萌] 某番 SP [CHT]")])
    await user.open(f"/detail/{aid}")
    await _set_kw(user, "繁日")
    assert not _kw_banner(user, "繁日"), "没有可比的正集也出了『现有 0 条…』横幅"
    msgs = user.notify.messages
    assert any("只下原名含此词的版本" in m for m in msgs), msgs
    assert not any("没有一条含这个词" in m for m in msgs), msgs


@pytest.mark.nicegui_main_file(_MAIN)
async def test_keyword_zero_hit_banner_yields_to_the_blocked_episodes_banner(user, clean_tables, cfg):
    """两条同时成立时只出带集号的那条（keyword_blocked_episodes），不叠两块说同一件事。"""
    cfg(ANIME_LOCK_LAG_DAYS=1)
    aid = _kw_anime(clean_tables, [("喵萌", 1, "[喵萌] 某番 - 01 [CHT]")], pref_keyword="繁日")
    await user.open(f"/detail/{aid}")
    assert _labels(user, "没有一条命中你设的版本关键词『繁日』"), "前提：keyword_blocked_episodes 那条横幅要出"
    assert not _kw_banner(user, "繁日")


# ---------------------------------------------------------------- U2-7 原因印在行上

@pytest.mark.nicegui_main_file(_MAIN)
async def test_stalled_error_and_retrying_rows_show_their_reason(user, clean_tables, cfg):
    cfg(QB_ENABLED=True, ANIME_FINISH_UNSUB=False)
    with clean_tables.get_session() as s:
        aid = _anime(s).id
        _row(s, aid, "ANi", 1, status="stalled", fail_reason="qB 报错（已下 50%）", qb_progress=0.5)
        _row(s, aid, "ANi", 2, status="error", fail_reason="取种失败：HTTP 404", retry_count=5)
        _row(s, aid, "ANi", 3, status="pending", fail_reason="qB 连不上",
             retry_at=datetime.now() + timedelta(hours=2), retry_count=2)
    await user.open(f"/detail/{aid}")
    await user.should_see("停滞原因：qB 报错（已下 50%）")
    await user.should_see("失败原因：取种失败：HTTP 404（已自动重试 5 次仍不行）")
    await user.should_see("上次失败：qB 连不上 · ")
    await user.should_see("之后自动重发")


@pytest.mark.parametrize("gate", ["待确认", "该集已删"])
@pytest.mark.nicegui_main_file(_MAIN)
async def test_rows_that_will_not_be_retried_never_promise_a_retry(user, clean_tables, cfg, gate):
    """同样带 retry_at + fail_reason 的待下行，落在『不会自动重发』的闸分支上：行上不许出现"之后自动重发"。"""
    cfg(QB_ENABLED=True, ANIME_FINISH_UNSUB=False)
    with clean_tables.get_session() as s:
        aid = _anime(s, confirmed=(gate != "待确认")).id
        _row(s, aid, "ANi", 3, status="pending", fail_reason="qB 连不上",
             retry_at=datetime.now() + timedelta(hours=2), retry_count=2)
        if gate == "该集已删":
            _row(s, aid, "喵萌", 3, status="deleted", ep_dropped=True)
    await user.open(f"/detail/{aid}")
    await user.should_not_see("之后自动重发")
    await user.should_not_see("上次失败：")


@pytest.mark.nicegui_main_file(_MAIN)
async def test_movie_rows_show_stalled_and_failed_reasons_apart(user, clean_tables, cfg):
    from db.models import Movie, MovieTorrent
    cfg(QB_ENABLED=True)
    with clean_tables.get_session() as s:
        m = Movie(title="某片", display_name="某片", quarter="26A", bangumi_id=2)
        s.add(m); s.commit(); s.refresh(m)
        s.add(MovieTorrent(movie_id=m.id, info_hash="5" * 40, raw_title="[甲] 某片", status="stalled",
                           qb_progress=0.4, fail_reason="从 qB 消失（已下 40%）"))
        s.commit()
        mid = m.id
    await user.open(f"/mdetail/{mid}")
    await user.should_see("停滞原因：从 qB 消失（已下 40%）")
    await user.should_not_see("上次失败")
    with clean_tables.get_session() as s:
        s.add(MovieTorrent(movie_id=mid, info_hash="6" * 40, raw_title="[乙] 某片", status="pending",
                           fail_reason="取种失败：HTTP 404"))
        s.commit()
    await user.open(f"/mdetail/{mid}")
    await user.should_see("上次失败：取种失败：HTTP 404（不自动重发）")


# ---------------------------------------------------------------- U2-8 跳过气泡 + 同集内排序

@pytest.mark.nicegui_main_file(_MAIN)
async def test_skipped_badge_explains_itself(user, clean_tables):
    with clean_tables.get_session() as s:
        aid = _anime(s).id
        _row(s, aid, "ANi", 1, status="skipped")
    await user.open(f"/detail/{aid}")
    b = next(b for b in _live(user, ui.badge) if b.text == "跳过")
    assert "右边『下载』" in _tooltip(user, b), _tooltip(user, b)


@pytest.mark.nicegui_main_file(_MAIN)
async def test_rows_with_a_copy_come_first_within_an_episode_and_episodes_keep_their_order(user, clean_tables, cfg):
    cfg(ANIME_FINISH_UNSUB=False)
    t0 = datetime.now() - timedelta(days=10)
    with clean_tables.get_session() as s:
        aid = _anime(s).id
        # 入库时间从新到旧（list_episodes 同集内按入库时间倒序）：第 1 集 跳过 → 已下；第 2 集 跳过 → 将下载
        _row(s, aid, "C", 1, status="skipped", raw="E1-跳过", created_at=t0 + timedelta(days=3))
        _row(s, aid, "A", 1, status="sent", raw="E1-已下", created_at=t0)
        _row(s, aid, "C", 2, status="skipped", raw="E2-跳过", created_at=t0 + timedelta(days=3))
        _row(s, aid, "A", 2, status="pending", raw="E2-将下", created_at=t0)
        # 失败行入库最早：不靠入库时间、只靠『失败归第二档』才排得到『跳过』前面
        _row(s, aid, "B", 2, status="error", raw="E2-失败", created_at=t0 - timedelta(days=1))
        _row(s, aid, "A", 3, status="pending", raw="E3-将下", created_at=t0)
    await user.open(f"/detail/{aid}")
    names = {"E1-跳过", "E1-已下", "E2-跳过", "E2-将下", "E2-失败", "E3-将下"}
    # user.find(...).elements 是集合、没有顺序：按元素 id（创建序 = 渲染序）排
    order = [e.text for e in sorted(_live(user, ui.label), key=lambda e: e.id) if e.text in names]
    assert order == ["E1-已下", "E1-跳过", "E2-将下", "E2-失败", "E2-跳过", "E3-将下"], order


# ---------------------------------------------------------------- U2-9 下拉显示 / 按钮顺序 / 指路文案

@pytest.mark.nicegui_main_file(_MAIN)
async def test_locked_source_reads_only_x_but_stores_the_bare_group(user, clean_tables):
    from core import anime as A
    with clean_tables.get_session() as s:
        aid = _anime(s, pref_source="ANi").id
        _row(s, aid, "ANi", 1)
        _row(s, aid, "喵萌", 1)
    await user.open(f"/detail/{aid}")
    sel = next(e for e in _live(user, ui.select) if "" in e.options)
    assert sel.value == "ANi" and sel.options[sel.value] == "只下 ANi", (sel.value, sel.options)
    assert sel.options[""] == "下载源：按优先级·多源兜底", sel.options
    sel.value = "喵萌"
    await _until(lambda: A.get_anime(aid).pref_source == "喵萌",
                 f"存进库的不是裸组名：{A.get_anime(aid).pref_source!r}")


@pytest.mark.nicegui_main_file(_MAIN)
async def test_auto_backfill_comes_before_the_loose_one_which_is_grey(user, clean_tables):
    with clean_tables.get_session() as s:
        aid = _anime(s).id
        _row(s, aid, "ANi", 1)
    await user.open(f"/detail/{aid}")
    auto = next(b for b in _live(user, ui.button) if b.text == "自动补齐")
    loose = next(b for b in _live(user, ui.button) if b.text == "补齐该源")
    assert auto.id < loose.id, "会把在追的番打回待确认的『补齐该源』排在了『自动补齐』前面"
    assert loose.props.get("color") == "grey" and auto.props.get("color") == "primary", (loose.props, auto.props)


def test_gap_hints_no_longer_send_people_to_the_loose_backfill():
    """【弱守卫，只防改回去】番剧卡片缺集气泡那句指路：改指『自动补齐』，不再教人改下载源再点『补齐该源』。"""
    src = Path("pages/anime.py").read_text(encoding="utf-8")
    assert "再点『补齐该源』" not in src
    assert "进详情页点『自动补齐』（当前下载源搜不到时会列出别的组有哪些集" in src


# ---------------------------------------------------------------- U2-11 已在 qB 里的行『下载』置灰

def _real_chain(cfg, monkeypatch):
    """真交付链：只把取种与加进 qB 换成记账（同 tests/test_r39_download.py 的 real_chain）。"""
    from core import anime as A, engine as E, movies as MV
    cfg(QB_ENABLED=True, QB_SYNC_STATUS=True, DOWN_PATH="/tmp/dl", ANIME_DOWN_PATH="", MOVIE_DOWN_PATH="",
        ANIME_FINISH_UNSUB=False, ANIME_DOWNLOAD_GRACE_MIN=0)
    fetched = []

    async def fetch(url):
        fetched.append(url); return b"d1:xe"

    async def ok(*a, **k):
        return True
    monkeypatch.setattr(E, "fetch_torrent_bytes", fetch)
    monkeypatch.setattr(E, "add_to_qb", ok)
    monkeypatch.setattr(A, "notify_event", ok)
    monkeypatch.setattr(MV, "notify_event", ok)
    monkeypatch.setattr(A, "qb_precheck", ok)
    return fetched


_SHAPES = [("sent", False), ("sent", True), ("downloading", False), ("downloading", True), ("pending", False),
           ("skipped", False), ("stalled", False), ("deleted", False), ("excluded", False)]


@pytest.mark.nicegui_main_file(_MAIN)
async def test_anime_download_button_is_disabled_exactly_where_force_cannot_deliver(
        user, clean_tables, cfg, monkeypatch):
    from core import anime as A, engine as E
    from db.models import AnimeTorrent
    fetched = _real_chain(cfg, monkeypatch)
    ids = {}
    with clean_tables.get_session() as s:
        aid = _anime(s).id
        for i, (st, arch) in enumerate(_SHAPES, start=1):   # 各占一集，互不挡同集闸
            ids[i] = _row(s, aid, f"G{i}", i, status=st, raw=f"ROW{i:02d}",
                          archived_at=(OLD if arch else None), save_path="/tmp/dl/x")
    await user.open(f"/detail/{aid}")
    enabled = {}
    for b in _live(user, ui.button):
        if b.text == "下载":
            txt = _row_text(_row_of(b))
            i = next(i for i in ids if f"ROW{i:02d}" in txt)
            enabled[i] = b.enabled
    assert set(enabled) == set(ids), enabled
    for i, tid in ids.items():
        before = len(fetched)
        await A.download_anime_torrent(tid, force=True)
        went = len(fetched) > before
        with clean_tables.get_session() as s:
            t = s.get(AnimeTorrent, tid)
        assert enabled[i] == went, f"{_SHAPES[i - 1]}：按钮 enabled={enabled[i]}，force 走到取种={went}（{t.status}）"
    assert enabled[1] is False and enabled[2] is True, "已下未归档置灰、已归档可重新交回"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_movie_download_button_is_disabled_exactly_where_force_cannot_deliver(
        user, clean_tables, cfg, monkeypatch):
    from core import movies as MV
    from db.models import Movie, MovieTorrent
    fetched = _real_chain(cfg, monkeypatch)
    ids = {}
    with clean_tables.get_session() as s:
        m = Movie(title="某片", display_name="某片", quarter="26A", bangumi_id=2)
        s.add(m); s.commit(); s.refresh(m)
        for i, (st, arch) in enumerate(_SHAPES, start=1):
            t = MovieTorrent(movie_id=m.id, info_hash=f"{900 + i:040x}", raw_title=f"VER{i:02d}", status=st,
                             download_url=f"https://x/m{i}.torrent", archived_at=(OLD if arch else None))
            s.add(t); s.commit(); s.refresh(t)
            ids[i] = t.id
        mid = m.id
    await user.open(f"/mdetail/{mid}")
    enabled = {}
    for b in _live(user, ui.button):
        if b.text == "下载":
            txt = _row_text(_row_of(b))
            i = next(i for i in ids if f"VER{i:02d}" in txt)
            enabled[i] = b.enabled
    assert set(enabled) == set(ids), enabled
    for i, tid in ids.items():
        before = len(fetched)
        await MV.download_movie_torrent(tid)
        assert enabled[i] == (len(fetched) > before), (_SHAPES[i - 1], enabled[i])
    assert enabled[1] is False and enabled[2] is True


# ---------------------------------------------------------------- U2-12 晚重发提醒指向做得成的动作

def test_late_rerelease_alert_points_at_not_this_season_and_that_action_works(clean_tables, cfg):
    """生产 #8 的形状：第二季（7 月首播）里第 6～8 集的旧行是第一季的已下份（发布早于首播 30 天以上），
    新一季的同号集晚 20 周以上才来、被挡着。提醒让人点的『不属于本季』在旧行上真有，标完提醒消失、新行进计划。"""
    from core import alerts, anime as A, engine as E
    from db.models import AnimeTorrent
    cfg(ANIME_RERELEASE_WEEKS=20, ANIME_FINISH_UNSUB=False, ANIME_BACKFILL_PREAIR_DAYS=30)
    air = datetime(2026, 7, 1)
    olds, news = [], []
    with clean_tables.get_session() as s:
        a = _anime(s, season=2, air_date="2026-07-01", display_name="相反的你和我 第二季")
        aid = a.id
        for ep in (6, 7, 8):
            olds.append(_row(s, aid, "喵萌", ep, status="sent", season=1,
                             release_time=air - timedelta(days=120) + timedelta(weeks=ep)))
            news.append(_row(s, aid, "绿茶", ep, status="pending", season=1,
                             release_time=air + timedelta(weeks=ep + 22), created_at=OLD))
    rer = [f for f in alerts.scan() if f["kind"] == "rer"]
    assert len(rer) == 1, rer
    txt = rer[0]["text"]
    assert "『不属于本季』" in txt and "『排除』" not in txt and "单独绑 bgm" not in txt, txt
    a = A.get_anime(aid)
    with clean_tables.get_session() as s:
        old_rows = [s.get(AnimeTorrent, i) for i in olds]
    assert all(A.foreign_season_suspect(a, t) for t in old_rows), "旧行上没有『不属于本季』按钮，提醒指的动作做不了"
    assert not E.exclude_torrent(AnimeTorrent, olds[0]), "前提：旧文案的『排除』对已下的行做不了"
    for i in olds:
        assert A.set_foreign_season(i, True)
    assert A.suspect_rerelease_collision() == [], "标完之后提醒没消失"
    assert set(news) <= A.download_plan(aid), "标完之后新一季那几集没进自动下载计划"


# ---------------------------------------------------------------- U4-5 手机上详情底部的『关闭』

def _click(user, el):
    UserInteraction(user, {el}, None).click()


def _in(el, kind):
    return next((p for p in el.ancestors() if isinstance(p, kind)), None)


@pytest.mark.nicegui_main_file(_PAGES)
async def test_anime_detail_has_a_phone_only_close_at_the_bottom(user, clean_tables):
    with clean_tables.get_session() as s:
        aid = _anime(s, quarter="26D").id
        _row(s, aid, "ANi", 1)
    await user.open("/?t=manage")
    card = next(c for c in _live(user, ui.card) if "cover-card" in c.classes)
    _click(user, card)
    await _until(lambda: any(b.text == "关闭" and _in(b, ui.dialog) for b in _live(user, ui.button)),
                 "详情弹窗底部没有『关闭』")
    btn = next(b for b in _live(user, ui.button) if b.text == "关闭" and _in(b, ui.dialog))
    assert "sm:hidden" in btn.parent_slot.parent.classes, btn.parent_slot.parent.classes
    dlg = _in(btn, ui.dialog)
    assert dlg.value
    _click(user, btn)
    await _until(lambda: not dlg.value, "点『关闭』没有关掉详情弹窗")


@pytest.mark.nicegui_main_file(_PAGES)
async def test_movie_detail_has_a_phone_only_close_at_the_bottom(user, clean_tables):
    from db.models import Movie, MovieTorrent
    with clean_tables.get_session() as s:
        m = Movie(title="某片", display_name="某片", quarter="26A", bangumi_id=2)
        s.add(m); s.commit(); s.refresh(m)
        s.add(MovieTorrent(movie_id=m.id, info_hash="8" * 40, raw_title="[甲] 某片", status="pending"))
        s.commit()
    await user.open("/movies?t=list")
    card = next(c for c in _live(user, ui.card) if "cover-card" in c.classes)
    title = next(e for e in card.descendants() if "cursor-pointer" in getattr(e, "classes", [])
                 and isinstance(e, ui.label))
    _click(user, title)
    await _until(lambda: any(b.text == "关闭" and _in(b, ui.dialog) for b in _live(user, ui.button)),
                 "剧场版详情弹窗底部没有『关闭』")
    btn = next(b for b in _live(user, ui.button) if b.text == "关闭" and _in(b, ui.dialog))
    assert "sm:hidden" in btn.parent_slot.parent.classes
    dlg = _in(btn, ui.dialog)
    _click(user, btn)
    await _until(lambda: not dlg.value, "点『关闭』没有关掉剧场版详情弹窗")


@pytest.mark.nicegui_main_file(_MAIN)
async def test_no_bottom_close_without_on_close(user, clean_tables):
    with clean_tables.get_session() as s:
        aid = _anime(s).id
        _row(s, aid, "ANi", 1)
    await user.open(f"/detail/{aid}")
    assert not [b for b in _live(user, ui.button) if b.text == "关闭"]
