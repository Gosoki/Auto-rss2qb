"""(R42 / R43 layout) 番剧详情页的按钮布局：只防结构被改回去，证明不了像素（像素要靠截图实测）。

钉的是：
· 每条种子行右侧是固定三格（.ep-acts）：『下载』恒在，🗑 / 排除 / 恢复 与它同在一个容器里，空格也占位；
· 『别季』（标为不属于本季）/『撤销』常驻在第三格，不收菜单（用户：不要单纯收起来）；
· 详情页里没有任何菜单：顶部管理行 = 主操作 + 重新识别 / 编辑季度 / 忽略本番，都常驻；
· 下载区（下载源、版本、下载该源 / 自动补齐 / 补齐该源）在元信息卡下面、分集列表上面；
· 『重新识别』跑一百多秒，转圈挂在它自己身上；
· .ep-acts 是 grid（格宽固定，各行『下载』才在同一列）；两组常驻按钮手机上是等宽网格。
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
        _row(s, aid, "戊", 5, "sent", save_path="/tmp/dl/x", foreign_season=True)   # 🗑 + 撤销
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
        assert len(kids) == 3 and kids[1] is b, "三格：处置 / 下载 / 别季，空格也要占位"
        assert _row_of(acts) is _row_of(b)
    for text, icon in (("排除", None), ("恢复", None), ("", "delete_forever")):
        bs = _btns(user, text, icon) if icon is None else _btns(user, icon=icon)
        assert bs, (text, icon)
        for b in bs:
            acts = _acts_of(b)
            assert acts is not None and _slots(acts)[0] is b, f"{text or icon} 不在处置格"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_foreign_season_actions_sit_in_the_third_slot(user, clean_tables, cfg):
    cfg(QB_ENABLED=True)
    aid = _seed(clean_tables)
    await user.open(f"/detail/{aid}")
    undo = _btns(user, "撤销")
    assert len(undo) == 1
    assert not _in_menu(undo[0]), "『撤销』又收回菜单里了"
    acts = _acts_of(undo[0])
    assert acts is not None and _slots(acts)[2] is undo[0], "『撤销』应常驻在第三格"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_foreign_suspect_button_sits_in_the_third_slot(user, clean_tables, cfg, monkeypatch):
    from core import anime as A
    cfg(QB_ENABLED=True)
    aid = _seed(clean_tables)
    monkeypatch.setattr(A, "foreign_season_suspect", lambda cur, t: t.source == "乙")
    await user.open(f"/detail/{aid}")
    bs = _btns(user, "非本季")
    assert len(bs) == 1 and not _in_menu(bs[0])
    acts = _acts_of(bs[0])
    assert acts is not None and _slots(acts)[2] is bs[0], "『别季』应常驻在第三格"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_no_menu_and_download_block_sits_below_meta(user, clean_tables, cfg):
    cfg(QB_ENABLED=True)
    aid = _seed(clean_tables)
    await user.open(f"/detail/{aid}")
    # 顶栏导航是 menu_item 菜单，不算；详情页的按钮一个都不许在菜单里
    assert not [b.text for b in _live(user, ui.button) if _in_menu(b)], "详情页按钮又收进菜单了：要常驻，不收起来"
    btn = {t: _btns(user, t) for t in ("确认下载", "重新识别", "编辑季度", "忽略本番",
                                       "下载该源", "自动补齐", "补齐该源")}
    for t, bs in btn.items():
        assert len(bs) == 1, t
    # 建树顺序：管理行（标题下）→ 元信息卡 → 下载区 → 分集列表
    meta = [l for l in _live(user, ui.label) if l.text == "日文"][0]
    eps = [l for l in _live(user, ui.label) if l.text.startswith("分集 / 种子")][0]
    for t in ("确认下载", "重新识别", "编辑季度", "忽略本番"):
        assert btn[t][0].id < meta.id, f"{t} 应在标题下的管理行里"
    for t in ("下载该源", "自动补齐", "补齐该源"):
        assert meta.id < btn[t][0].id < eps.id, f"{t} 应在元信息卡下面、分集列表上面"
    sel = [x for x in _live(user, ui.select)]
    assert sel and meta.id < sel[0].id < eps.id, "下载源应在下载区里"
    # 两组各自一个 .act-grid（手机上等宽网格），主操作带 act-main 独占第一行
    grids = {id(next(a for a in btn[t][0].ancestors() if "act-grid" in a.classes))
             for t in btn}
    assert len(grids) == 2, "管理行与下载区按钮应各在一个 .act-grid 里"
    assert "act-main" in btn["确认下载"][0].classes


@pytest.mark.nicegui_main_file(_MAIN)
async def test_reidentify_spins_on_its_own_button(user, clean_tables, cfg, monkeypatch):
    from core import anime as A
    cfg(QB_ENABLED=True)
    aid = _seed(clean_tables)
    gate = asyncio.Event()

    async def slow(_aid):
        await gate.wait()
        return False
    monkeypatch.setattr(A, "manual_enrich", slow)
    await user.open(f"/detail/{aid}")
    btn = _btns(user, "重新识别")[0]
    UserInteraction(user, {btn}, None).click()
    for _ in range(60):
        if "loading" in btn.props:
            break
        await asyncio.sleep(0.05)
    assert "loading" in btn.props, "『重新识别』跑的那一百多秒里没有转圈"
    gate.set()
    await user.should_see("未识别到", retries=40)


def test_ep_acts_css_is_a_fixed_grid():
    from pages import layout
    assert ".ep-acts{display:grid;grid-template-columns:" in layout._HEAD_BASE_CSS
    assert "grid-template-columns:repeat(3,minmax(0,1fr))" in layout._HEAD_BASE_CSS
    assert ".act-grid>.act-main{grid-column:1/-1}" in layout._HEAD_BASE_CSS
