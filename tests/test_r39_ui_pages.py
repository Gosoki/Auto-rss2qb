"""(R39 U11/U12/U13/U15) 番剧页与订阅源页：一处操作不许冲掉别处的半途输入，长操作跑完要真的反映到界面上。

· U11 订阅源页：保存/添加/删除一个组，别的组卡片上没保存的改动、半填的新组表单都要还在；
· U12 详情关掉之后才跑完的长操作要刷外层；从明细列表进详情处理完关掉，列表要跟着变；
· U13 待确认/待识别 tab 切回来时，数据变了才重建、没变不碰（卡片上的下拉不被重建）；
· U15 『刷新资料』的范围选择是 page 级对话框，30 秒定时器重建 overview_tail 时不会被收掉，
  长操作的失败/完成提示都发得出来。
"""
import asyncio
import sys

import pytest
from nicegui import ui
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction

import config
import db

db.init_db()
config.load_from_db()
db.apply_configured_backend()

_MAIN = "tests/render_main.py"


async def _until(pred, tries=100):
    """刷新可能排在后台任务里：轮询到条件成立（最多约 5 秒）。返回最后一次的判断结果。"""
    for _ in range(tries):
        if pred():
            return True
        await asyncio.sleep(0.05)
    return pred()


def _click(user, el):
    UserInteraction(user, {el}, None).click()


def _live(user, kind):
    return [e for e in user.find(kind).elements if not e.is_deleted]


def _button(user, text):
    bs = [b for b in _live(user, ui.button) if b.text == text]
    assert bs, f"找不到『{text}』按钮"
    return bs[0]


def _in(el, kind):
    """el 最近的 kind 类型祖先。"""
    return next((p for p in el.ancestors() if isinstance(p, kind)), None)


# ---------------------------------------------------------------- U11 订阅源页

def _drop_test_groups():
    from core import anime as A
    for g in A.list_source_groups():
        if g.name.startswith("r39"):
            A.delete_source_group(g.id)


def _name_input(user, value):
    return next((i for i in _live(user, ui.input)
                 if i.props.get("label") == "名字" and i.value == value), None)


def _form_inputs(user):
    """『添加新组』表单：含『添加』按钮的那张卡片里的输入框，按 label 取。"""
    card = _in(_button(user, "添加"), ui.card)
    return {i.props.get("label"): i for i in card.descendants() if isinstance(i, (ui.input, ui.number))}


def _btn_in(card, text):
    return next(b for b in card.descendants() if isinstance(b, ui.button) and b.text == text)


@pytest.mark.nicegui_main_file(_MAIN)
async def test_save_add_delete_keep_unsaved_edits_elsewhere(user: User, clean_tables, monkeypatch):
    from core import anime as A

    _drop_test_groups()
    assert A.add_source_group("r39甲", "nyaa", "feedA", "auto", 10)
    assert A.add_source_group("r39乙", "nyaa", "feedB", "auto", 20)
    try:
        await user.open("/sources")
        name_a = _name_input(user, "r39甲")
        card_b = _in(_name_input(user, "r39乙"), ui.card)
        form = _form_inputs(user)
        feed_key = next(k for k in form if k and k.startswith("feed"))

        name_a.value = "r39甲-改"                  # A 组改了名字，还没点保存
        form["名字"].value = "r39丙"               # 新组表单填了一半
        _click(user, _btn_in(card_b, "保存"))
        await user.should_see("已保存")
        await asyncio.sleep(0.2)                   # refresh() 不 await 时排在下一拍执行：等它（若有）真的跑完再断言
        assert not name_a.is_deleted and name_a.value == "r39甲-改", "保存 B 组冲掉了 A 组没保存的改动"
        assert not form["名字"].is_deleted and form["名字"].value == "r39丙", "保存 B 组冲掉了半填的新组表单"

        form[feed_key].value = "feedC"
        _click(user, _btn_in(_in(form["名字"], ui.card), "添加"))
        await user.should_see("已添加")
        await asyncio.sleep(0.2)
        assert _name_input(user, "r39丙") is not None, "新组的卡片没出现"
        assert not name_a.is_deleted and name_a.value == "r39甲-改", "添加新组冲掉了 A 组没保存的改动"
        assert (form["名字"].value, form[feed_key].value, form["优先级"].value) == ("", "", 50), \
            "添加成功后表单没回到初始值"

        async def yes(*a, **k):
            return True
        monkeypatch.setattr(sys.modules["pages.sources"], "confirm", yes)
        _click(user, _btn_in(card_b, "删除"))
        await user.should_see("已删除")
        await asyncio.sleep(0.2)
        assert card_b.is_deleted, "删掉的组卡片还在"
        assert not name_a.is_deleted and name_a.value == "r39甲-改", "删除 B 组冲掉了 A 组没保存的改动"
    finally:
        _drop_test_groups()


# ---------------------------------------------------------------- U12 详情关掉之后

def _outer_labels(user, text):
    """不在任何对话框里的、文字恰好是 text 的 label（详情弹窗里那份不算）。"""
    return [lb for lb in _live(user, ui.label) if lb.text == text and _in(lb, ui.dialog) is None]


@pytest.mark.nicegui_main_file(_MAIN)
async def test_outer_card_refreshes_when_a_long_action_ends_after_the_detail_closed(
        user: User, clean_tables, monkeypatch):
    from core import anime as A
    from db.models import Anime

    with clean_tables.get_session() as s:
        a = Anime(title="旧名", display_name="旧名", season=1, quarter="26D",
                  confirmed=True, bangumi_id=930001)
        s.add(a)
        s.commit()
        aid = a.id
    gate = asyncio.Event()

    async def slow_backfill(anime_id, *args, **kw):   # 补齐跑着的时候用户关了详情；跑完时番名变了
        await gate.wait()
        with clean_tables.get_session() as s:
            row = s.get(Anime, anime_id)
            row.display_name = "新名"
            s.add(row)
            s.commit()
        return {"found": 0, "ingested": 0}
    monkeypatch.setattr(A, "backfill_source", slow_backfill)

    await user.open("/?t=manage")
    assert _outer_labels(user, "旧名"), "前提：番剧表卡片上是旧名"
    card = next(c for c in _live(user, ui.card) if "cover-card" in c.classes)
    _click(user, card)
    assert await _until(lambda: any(b.text == "自动补齐" for b in _live(user, ui.button)))
    btn = _button(user, "自动补齐")
    _click(user, btn)
    await asyncio.sleep(0.1)                      # 让处理器走到 backfill_source 的等待处
    _in(btn, ui.dialog).value = False             # 点完就关详情
    gate.set()
    assert await _until(lambda: _outer_labels(user, "新名")), \
        "详情关掉之后才跑完的补齐没刷外层，卡片还是旧名"
    assert aid not in sys.modules["pages.anime_detail"]._backfill_running


@pytest.mark.nicegui_main_file(_MAIN)
async def test_list_dialog_is_rebuilt_after_handling_an_item_in_the_detail(
        user: User, clean_tables, monkeypatch):
    from core import anime as A
    from db.models import Anime

    ids = []
    with clean_tables.get_session() as s:
        for i, n in enumerate(("未知甲", "未知乙")):
            a = Anime(title=n, display_name=n, season=1, quarter="26D",
                      confirmed=True, bangumi_id=931000 + i)
            s.add(a)
            s.commit()
            ids.append(a.id)
    rows = [{"name": "未知甲 第?集", "anime_id": ids[0], "raw": "[组] 未知甲 合集"},
            {"name": "未知乙 第?集", "anime_id": ids[1], "raw": "[组] 未知乙 合集"}]
    monkeypatch.setattr(A, "unknown_episode_rows", lambda: list(rows))

    await user.open("/?t=overview")
    chip = next(b for b in _live(user, ui.badge) if b.text.startswith("未知集"))
    _click(user, chip)
    await user.should_see("未知集 · 2")

    row = next(lb for lb in _live(user, ui.label) if lb.text == "未知甲 第?集")
    _click(user, row)                             # 从列表行进详情（详情叠在列表上面）
    assert await _until(lambda: any(b.text == "忽略本番" for b in _live(user, ui.button)))
    ign = _button(user, "忽略本番")
    _click(user, ign)                             # 同步操作：详情开着，只记"外层脏了"
    await user.should_see("已忽略")
    rows.pop(0)                                   # 处理掉一条，列表数据只剩 1 条
    _in(ign, ui.dialog).value = False             # 关掉详情，回到列表
    await user.should_see("未知集 · 1")
    await user.should_not_see("未知集 · 2")


# ---------------------------------------------------------------- U13 切回待确认/待识别

def _add_pending(clean_tables, name, bid, source=None):
    from db.models import Anime, AnimeTorrent
    with clean_tables.get_session() as s:
        a = Anime(title=name, display_name=name, season=1, quarter="26D",
                  confirmed=False, bangumi_id=bid)
        s.add(a)
        s.commit()
        if source:
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{bid:040x}", raw_title=f"[{source}] {name} - 01",
                               source=source, episode=1, status="pending"))
            s.commit()
        return a.id


def _switch_away_and_back(user, key):
    tabs = next(iter(user.find(ui.tabs).elements))
    tabs.value = "overview"
    tabs.value = key


@pytest.mark.nicegui_main_file(_MAIN)
async def test_empty_confirm_tab_shows_new_arrivals_when_switched_back(user: User, clean_tables):
    await user.open("/?t=confirm")
    await user.should_see("没有待确认的番")
    _add_pending(clean_tables, "新来的番", 940001)       # 后台（或别的标签页）新进来一部

    _switch_away_and_back(user, "confirm")
    assert await _until(lambda: any(b.text == "确认下载" for b in _live(user, ui.button))), \
        "切回待确认 tab 还停在空状态，新进来的番看不到"
    assert any(lb.text == "新来的番" for lb in _live(user, ui.label))


@pytest.mark.nicegui_main_file(_MAIN)
async def test_confirm_tab_is_not_rebuilt_when_nothing_changed(user: User, clean_tables):
    _add_pending(clean_tables, "没变的番", 940002)
    await user.open("/?t=confirm")
    sel = _live(user, ui.select)[0]
    old_id = sel.id

    _switch_away_and_back(user, "confirm")
    await asyncio.sleep(0.2)
    assert not sel.is_deleted, "数据没变，切回来却把卡片重建了（半途输入会丢）"
    assert [s.id for s in _live(user, ui.select)] == [old_id]


@pytest.mark.nicegui_main_file(_MAIN)
async def test_confirm_tab_is_rebuilt_when_only_the_locked_source_changed(user: User, clean_tables):
    from db.models import Anime

    aid = _add_pending(clean_tables, "锁源的番", 940003, source="组A")
    await user.open("/?t=confirm")
    sel = _live(user, ui.select)[0]
    assert sel.value == "", "前提：卡片下拉是『按优先级』"
    with clean_tables.get_session() as s:            # 别的标签页在详情里锁了源：成员没变，只变了 pref_source
        a = s.get(Anime, aid)
        a.pref_source = "组A"
        s.add(a)
        s.commit()

    _switch_away_and_back(user, "confirm")
    assert await _until(lambda: sel.is_deleted), \
        "锁定源变了却没重建：卡片下拉还是旧值，点『确认下载』会把刚设的锁静默解掉"
    assert [s.value for s in _live(user, ui.select)] == ["组A"]


# ---------------------------------------------------------------- U15 刷新资料

def _tick(user):
    """手动跑一次 30 秒定时器（与真定时器同一个回调：只刷当前 tab 的实时区，含 overview_tail）。"""
    t = next(t for t in user.find(ui.timer).elements if t.interval == 30.0)
    with user.client:
        t.callback()


def _reident_dlg(user):
    return _in(_button(user, "刷新当季"), ui.dialog)


@pytest.mark.nicegui_main_file(_MAIN)
async def test_reident_scope_dialog_survives_the_timer_rebuild(user: User, clean_tables):
    await user.open("/?t=overview")
    trigger = _button(user, "刷新资料")
    _click(user, trigger)
    dlg = _reident_dlg(user)
    assert dlg.value, "点『刷新资料』没打开范围选择"

    _tick(user)
    assert await _until(lambda: trigger.is_deleted), "前提：定时器确实重建了 overview_tail"
    assert dlg.value and not dlg.is_deleted, "定时器重建 overview_tail 把范围选择收掉了"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_reident_failure_is_reported(user: User, clean_tables, monkeypatch, caplog):
    from core import anime as A

    async def boom(seasons=None):
        raise RuntimeError("bgm 挂了")
    monkeypatch.setattr(A, "reenrich_scope", boom)
    await user.open("/?t=overview")
    _click(user, _button(user, "刷新资料"))
    _click(user, _button(user, "刷新当季"))
    await user.should_see("刷新资料失败")
    assert not _reident_dlg(user).value, "选了范围之后对话框应关掉"
    # busy_action 兜住异常时 log.exception 的那一行正是本用例要走的路径，不算 user 夹具说的"意外 ERROR"；
    # 只摘掉这一条，别的 ERROR 照样让用例红
    recs = caplog.get_records("call")
    recs[:] = [r for r in recs if not r.getMessage().startswith("按钮操作失败：reident-anime")]


@pytest.mark.nicegui_main_file(_MAIN)
async def test_reident_reports_the_count_when_done(user: User, clean_tables, monkeypatch):
    from core import anime as A

    async def three(seasons=None):
        return 3
    monkeypatch.setattr(A, "reenrich_scope", three)
    await user.open("/?t=overview")
    _click(user, _button(user, "刷新资料"))
    _click(user, _button(user, "刷新当季"))
    await user.should_see("完成：3 部")
