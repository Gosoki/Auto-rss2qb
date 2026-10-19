"""面板重建后，用户手动展开过的季度要保持展开。

详情弹窗/卡片上的操作（确认下载、补齐该源、忽略…）会整体重建番剧页的几个面板。以前重建时
每个季度折叠块都回到默认的"只开最新一季"——用户正看着别的季度，一点操作它就被收起来了，
看上去像"只剩当前季度的番"。
"""
import asyncio

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


def _expansions(user, needle):
    return [e for e in user.find(ui.expansion).elements if needle in (e.props.get("label") or "")]


@pytest.mark.nicegui_main_file(_MAIN)
async def test_opened_quarter_stays_open_after_rebuild(user: User, clean_tables):
    from db.models import Anime
    with clean_tables.get_session() as s:
        # 最新一季放两部、只忽略其中一部：那一组必须还在，26C 才始终是"第二组"。
        # （若忽略掉最新一季仅有的那部，26C 会变成第一组，而旧代码本来就默认展开第一组——测了等于没测。）
        for i, (q, name) in enumerate((("26D", "新季甲"), ("26D", "新季丁"), ("26C", "上季乙"))):
            s.add(Anime(title=name, display_name=name, season=1, quarter=q,
                        confirmed=False, bangumi_id=900000 + i))
        s.commit()

    await user.open("/?t=confirm")
    old = _expansions(user, "26C")[0]
    assert not old.value, "前提：第二个季度默认收起"
    old.value = True                          # 用户手动展开它

    # 在【最新一季】那组里点一次『忽略』——任何一个会重建面板的操作都行
    newest = _expansions(user, "26D")[0]
    btn = next(b for b in newest.descendants() if isinstance(b, ui.button) and b.text == "忽略")
    UserInteraction(user, {btn}, None).click()
    for _ in range(100):                      # 面板重建是异步的：等到 26D 那组只剩 1 部
        await asyncio.sleep(0.05)
        d = _expansions(user, "26D")
        if d and "1 部" in d[0].props.get("label", ""):
            break
    assert d and "1 部" in d[0].props.get("label", ""), "前提：忽略生效、面板重建过"

    now = _expansions(user, "26C")
    assert len(now) == 1 and now[0] is not old, "前提：26C 那组是重建出来的新元素"
    assert now[0].value, "手动展开的季度在面板重建后被收起来了"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_expand_all_overrides_manual_memory(user: User, clean_tables):
    """点『全部收起』后，之前手动展开的记忆作废——以最后那次全局操作为准。"""
    from db.models import Anime
    with clean_tables.get_session() as s:
        for i, (q, name) in enumerate((("26D", "新季甲"), ("26C", "上季乙"))):
            s.add(Anime(title=name, display_name=name, season=1, quarter=q,
                        confirmed=False, bangumi_id=910000 + i))
        s.commit()

    await user.open("/?t=confirm")
    old = _expansions(user, "26C")[0]
    old.value = True                          # 第二组（26C）默认收着，手动展开
    user.find("全部收起").click()
    for _ in range(100):                      # 面板重建是异步的：等到旧的折叠块被换掉再检查
        await asyncio.sleep(0.05)
        now = [e for e in user.find(ui.expansion).elements if " 部" in (e.props.get("label") or "")]
        if old not in now:
            break
    assert old not in now, "前提：面板确实重建过"
    assert not any(e.value for e in now), "『全部收起』之后还有季度开着"


async def _until(pred, tries=100):
    """面板重建是异步的：轮询到条件成立（最多约 5 秒）。返回最后一次的判断结果。"""
    for _ in range(tries):
        if pred():
            return True
        await asyncio.sleep(0.05)
    return pred()


@pytest.mark.nicegui_main_file(_MAIN)
async def test_search_opens_a_manually_closed_quarter(user: User, clean_tables):
    """手动收起过的季度，搜索时也要展开——否则顶上写着"匹配 1 部"，卡片却藏在折叠里。
    清空搜索后回到手动收起的状态（搜索期间的开合不记）。"""
    from db.models import Anime
    with clean_tables.get_session() as s:
        s.add(Anime(title="新季甲", display_name="新季甲", season=1, quarter="26D", confirmed=True))
        s.add(Anime(title="上季乙", display_name="上季乙", season=1, quarter="26C", confirmed=True))
        s.commit()

    await user.open("/?t=manage")
    d = _expansions(user, "26D")[0]
    assert d.value, "前提：最新一季默认展开"
    d.value = False                                   # 用户手动收起（记进开合记忆）

    user.find(marker="anime-search").type("新季")
    assert await _until(lambda: _expansions(user, "26D") and _expansions(user, "26D")[0] is not d), \
        "前提：搜索后面板重建过"
    assert _expansions(user, "26D")[0].value, "搜索时手动收起过的季度没展开，搜到的番看不见"

    searched = _expansions(user, "26D")[0]
    user.find(marker="anime-search").clear()
    assert await _until(lambda: _expansions(user, "26D") and _expansions(user, "26D")[0] is not searched)
    assert not _expansions(user, "26D")[0].value, "清空搜索后没回到手动收起的状态"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_hidden_tab_is_refreshed_only_when_switched_back(user: User, clean_tables):
    """操作后只立刻刷新当前 tab；隐藏着的 tab 切回来时才补刷，切回来看到的一定是最新的。"""
    from db.models import Anime
    with clean_tables.get_session() as s:
        s.add(Anime(title="待确认番", display_name="待确认番", season=1, quarter="26D",
                    confirmed=False, bangumi_id=920001))
        s.commit()

    def pending_badges():
        return [b for b in user.find(ui.badge).elements if b.text == "待确认"]

    await user.open("/?t=manage")
    assert pending_badges(), "前提：番剧表卡片上有『待确认』"
    tabs = next(iter(user.find(ui.tabs).elements))
    tabs.value = "confirm"
    await user.should_see("确认下载")
    btn = next(b for b in user.find(ui.button).elements if b.text == "确认下载")
    UserInteraction(user, {btn}, None).click()
    await user.should_see("没有待确认的番")         # 当前 tab（待确认）立刻刷新了

    assert pending_badges(), "隐藏着的番剧表被立刻重建了（应当等切回来再刷）"
    tabs.value = "manage"
    assert await _until(lambda: not pending_badges()), "切回番剧表后没有补刷，还显示『待确认』"
