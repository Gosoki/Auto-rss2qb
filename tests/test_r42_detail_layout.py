"""(R42 layout) 番剧详情页的按钮布局：只防结构被改回去，证明不了像素（像素要靠截图实测）。

钉的是：
· 每条种子行右侧是固定三格（.ep-acts）：『下载』恒在，🗑 / 排除 / 恢复 与它同在一个容器里，空格也占位；
· 『不属于本季』『撤销』收在行内 ⋯ 菜单里（常驻会把那一行的状态徽标往左推，徽标右缘成锯齿）；
· 顶部只有一条操作栏：『确认下载』『下载该源』『自动补齐』常驻，『补齐该源』『重新识别』『编辑季度』『忽略本番』在 ⋯ 里；
· 『重新识别』点完菜单就关，转圈挂在 ⋯ 上，否则那一百多秒里没有任何反馈；
· .ep-acts 是 grid（格宽固定，各行『下载』才在同一列）。
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from nicegui import ui
from nicegui.elements.tooltip import Tooltip
from nicegui.testing.user_interaction import UserInteraction

_MAIN = "tests/render_detail_main.py"
OLD = datetime.now() - timedelta(days=30)
_n = iter(range(1, 10 ** 6))


def _live(user, kind):
    return [e for e in user.find(kind).elements if not e.is_deleted]


def _btns(user, text=None, icon=None):
    return [b for b in _live(user, ui.button)
            if (text is None or b.text == text) and (icon is None or b.props.get("icon") == icon)]


def _row_of(el):
    return next(a for a in el.ancestors() if isinstance(a, ui.column))


def _in_menu(el) -> bool:
    return any(isinstance(a, ui.menu) for a in el.ancestors())


def _slots(acts):
    """三格里的元素。tooltip 是按钮的同级兄弟（靠 target 挂回按钮），在 DOM 里不占格，不算。"""
    return [c for c in acts.default_slot.children if not isinstance(c, Tooltip)]


def _acts_of(el):
    return next((a for a in el.ancestors() if "ep-acts" in a.classes), None)


def _anime(s, **kw):
    from db.models import Anime
    base = dict(title="某番", display_name="某番", jp_name="ボウバン", season=1, quarter="26C",
                confirmed=False, rejected=False, bangumi_id=4242, total_episodes=12)
    base.update(kw)
    a = Anime(**base)
    s.add(a); s.commit(); s.refresh(a)
    return a


def _row(s, aid, src, ep, status="pending", **kw):
    from db.models import AnimeTorrent
    n = next(_n)
    base = dict(anime_id=aid, info_hash=f"{n:040x}", source=src, site="nyaa", season=1, episode=float(ep),
                raw_title=f"[{src}] 某番 - {int(ep):02d} [1080p] #{n}", status=status,
                download_url=f"https://x/{n}.torrent", created_at=OLD, release_time=OLD)
    base.update(kw)
    t = AnimeTorrent(**base)
    s.add(t); s.commit(); s.refresh(t)
    return t.id


def _seed(clean_tables):
    with clean_tables.get_session() as s:
        aid = _anime(s).id
        _row(s, aid, "甲", 1, "pending")                          # 排除
        _row(s, aid, "乙", 2, "sent", save_path="/tmp/dl/x")      # 🗑
        _row(s, aid, "丙", 3, "excluded")                         # 恢复
        _row(s, aid, "丁", 4, "deleted")                          # 处置格空
        _row(s, aid, "戊", 5, "sent", save_path="/tmp/dl/x", foreign_season=True)   # 🗑 + ⋯ 撤销
    return aid


@pytest.mark.nicegui_main_file(_MAIN)
async def test_every_seed_row_has_the_fixed_three_slot_actions(user, clean_tables, cfg):
    cfg(QB_ENABLED=True)
    aid = _seed(clean_tables)
    await user.open(f"/detail/{aid}")
    dls = _btns(user, "下载")
    assert len(dls) == 5, len(dls)
    for b in dls:
        acts = _acts_of(b)
        assert acts is not None, "『下载』不在 .ep-acts 里"
        kids = _slots(acts)
        assert len(kids) == 3 and kids[1] is b, "三格：处置 / 下载 / ⋯，空格也要占位"
        assert _row_of(acts) is _row_of(b)
    for text, icon in (("排除", None), ("恢复", None), ("", "delete_forever")):
        bs = _btns(user, text, icon) if icon is None else _btns(user, icon=icon)
        assert bs, (text, icon)
        for b in bs:
            acts = _acts_of(b)
            assert acts is not None and _slots(acts)[0] is b, f"{text or icon} 不在处置格"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_foreign_season_actions_live_in_the_row_menu(user, clean_tables, cfg):
    cfg(QB_ENABLED=True)
    aid = _seed(clean_tables)
    await user.open(f"/detail/{aid}")
    undo = [b for b in _btns(user, "撤销")]
    assert len(undo) == 1
    assert _in_menu(undo[0]), "『撤销』应收在行内 ⋯ 菜单里"
    acts = _acts_of(undo[0])
    assert acts is not None and _slots(acts)[2] is next(
        a for a in undo[0].ancestors() if isinstance(a, ui.button)), "⋯ 应在第三格"
    for b in _btns(user, "不属于本季"):
        assert _in_menu(b)


@pytest.mark.nicegui_main_file(_MAIN)
async def test_top_bar_keeps_main_actions_and_folds_the_rest(user, clean_tables, cfg):
    cfg(QB_ENABLED=True)
    aid = _seed(clean_tables)
    await user.open(f"/detail/{aid}")
    for t in ("确认下载", "下载该源", "自动补齐"):
        bs = _btns(user, t)
        assert len(bs) == 1 and not _in_menu(bs[0]), t
    for t in ("补齐该源", "重新识别", "编辑季度", "忽略本番"):
        bs = _btns(user, t)
        assert len(bs) == 1 and _in_menu(bs[0]), f"{t} 应收在操作栏 ⋯ 里"
    # 操作栏在元信息卡之前（标题下、横幅上）：按建树顺序比 id
    more = _btns(user, icon="more_horiz")
    assert len(more) == 1
    meta = [l for l in _live(user, ui.label) if l.text == "日文"]
    assert meta and more[0].id < meta[0].id, "操作栏应在元信息卡上面"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_reidentify_spins_on_the_menu_trigger(user, clean_tables, cfg, monkeypatch):
    from core import anime as A
    cfg(QB_ENABLED=True)
    aid = _seed(clean_tables)
    gate = asyncio.Event()

    async def slow(_aid):
        await gate.wait()
        return False
    monkeypatch.setattr(A, "manual_enrich", slow)
    await user.open(f"/detail/{aid}")
    more = _btns(user, icon="more_horiz")[0]
    UserInteraction(user, {_btns(user, "重新识别")[0]}, None).click()
    for _ in range(60):
        if "loading" in more.props:
            break
        await asyncio.sleep(0.05)
    assert "loading" in more.props, "转圈没挂在 ⋯ 上：菜单关了以后看不到任何反馈"
    gate.set()
    await user.should_see("未识别到", retries=40)


def test_ep_acts_css_is_a_fixed_grid():
    from pages import layout
    assert ".ep-acts{display:grid;grid-template-columns:" in layout._HEAD_BASE_CSS
