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
