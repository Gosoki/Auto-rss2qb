"""(R40 ui-dash) 番剧仪表盘 / 番剧表 / 待确认 / 待识别 与全站手机档的界面守卫。

钉的是这些行为（每条都对着一处"改回去就会红"的改动）：
· 巡检横幅给『打开详情』入口：按钮去向与横幅正文说的那条记录绑定（dup 两条、mva 是番剧那条）；
  点了打开那部番的详情、横幅还在（打开不等于已读）。
· warn_banner：带按钮时文字与按钮再包一层（窄屏竖排），文案里的 \\n 真换行；多按钮同在一个可折行的组里。
· 『新入库』按「番+集」并行：灰行并进领头行，彩色行一条不少；关了 qB 跟踪时灰色的『已下』不算"不用管"。
· 『新入库』表 <1024px 隐去的列，必须在番名格里那行补回来（两个断点互补）。
· 仪表盘健康信号（overview_tail）排在两张变长列表之前。
· 番剧表：在追的番有失败/停滞时左上角标『失败/停滞 N』，悬停不再说『都到手了』；季度小结的合计叫『未下』。
· 已读的提示：首屏只数仍成立的，不自动删；对话框标题分开数。
· 待确认卡片写出来源组；待识别卡片两枚徽标有说明，且说明与代码事实一致。
· 失败/异常清单（番剧与剧场版）：每行带失败/停滞徽标与原因，番剧还带集号；未知集清单不出徽标。
· 手机档 CSS：一个 599 块里放内边距变量、KPI 列数、tab 内边距；外部前提（nicegui.css 的变量与分层）也钉住。
· 『正在下载』番名不从词中间拆开；环图图例在窄画布上的换行宽度放得下。
"""
import ast
import asyncio
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from nicegui import ui
from nicegui.elements.tooltip import Tooltip
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction

import config
import db

db.init_db()
config.load_from_db()
db.apply_configured_backend()

_MAIN = "tests/render_main.py"
_ROOT = Path(__file__).resolve().parent.parent


async def _until(pred, tries=100):
    for _ in range(tries):
        if pred():
            return True
        await asyncio.sleep(0.05)
    return pred()


def _click(user, el):
    UserInteraction(user, {el}, None).click()


def _live(user, kind):
    return [e for e in user.find(kind).elements if not e.is_deleted]


def _parent(el):
    return next(el.ancestors())


def _children(el):
    return list(el.default_slot.children)


# ---------------------------------------------------------------- 巡检横幅的『打开详情』（U1-5）

_T0 = datetime(2026, 1, 1)


def _fake_findings(monkeypatch):
    """五类发现各造一条（直接喂构造式，不经判据）。id 互不相同，mva 的番剧 21 / 剧场版 22 都出现在正文里。"""
    from core import alerts
    raws = {
        "dup": {"a": 11, "b": 12, "a_born": _T0, "b_born": _T0, "a_name": "甲番", "b_name": "乙番",
                "a_bgm": 1, "b_bgm": 2, "a_confirmed": True, "a_rejected": False,
                "b_confirmed": False, "b_rejected": False, "shared": ["甲乙"]},
        "mva": {"a": 21, "m": 22, "a_born": _T0, "m_born": _T0, "bgm": 3, "a_confirmed": False,
                "a_rejected": True, "a_name": "剧番", "a_state": "超期忽略", "an": 2, "mn": 3},
        "wrb": {"id": 31, "born": _T0, "name": "错季番", "bgm": 4, "season": 2, "total": 12, "bad": 3,
                "eps": [30], "trigger_eps": [30]},
        "ign": {"id": 41, "born": _T0, "name": "忽略番", "bgm": 5, "confirmed": False, "rejected": True,
                "eps": [3], "impossible": False, "total": 12},
        "rer": {"id": 51, "born": _T0, "name": "重号番", "eps": [2], "groups": ["组"]},
    }
    for kind, raw in raws.items():
        monkeypatch.setitem(alerts._BUILD, kind,
                            (lambda raw=raw: [raw], alerts._BUILD[kind][1]))


def test_every_banner_button_opens_the_record_its_text_names(monkeypatch):
    """按钮去向与正文说的那条记录绑在一起：open 里的每个 id 都以 #id 出现在自己的正文里；
    dup 两条都给（指引是两条都核对）；mva 给番剧那条（要点『忽略本番』的是它），不是剧场版那条。"""
    from core import alerts
    _fake_findings(monkeypatch)
    found = {f["kind"]: f for f in alerts.scan()}
    for kind in ("dup", "mva", "wrb", "rer"):
        f = found[kind]
        assert f.get("open"), f"{kind} 的横幅没有可打开的记录"
        for i in f["open"]:
            assert f"#{i}" in f["text"], f"{kind} 的按钮要打开 #{i}，正文里却没提它"
    assert found["dup"]["open"] == [11, 12]
    assert found["mva"]["open"] == [21], "mva 要打开的是番剧表那条，不是剧场版那条"
    assert found["wrb"]["open"] == [31] and found["rer"]["open"] == [51]
    # ign 合成一条横幅、指引是去『已忽略』页：不给入口
    assert "open" not in found["ign"]


def _wrb_anime(db, title):
    """一部『绑定看着不对』的番（同 test_alert_ack 的造法）。"""
    from db.models import Anime, AnimeTorrent
    with db.get_session() as s:
        a = Anime(title=title, display_name=title, season=3, quarter="26C", confirmed=True,
                  rejected=False, bangumi_id=598058, total_episodes=12, air_date="2026-07-05")
        s.add(a); s.commit(); s.refresh(a)
        for i, ep in enumerate((1, 2, 3)):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{a.id:08x}{i:032x}", raw_title=f"[组] - {ep}",
                               episode=float(ep), status="pending", release_time=datetime(2024, 1, 1)))
        s.commit()
        return a.id


@pytest.mark.nicegui_main_file(_MAIN)
async def test_open_detail_from_the_banner_shows_that_show_and_keeps_the_banner(
        user: User, clean_tables, cfg):
    from db.models import AlertAck
    from sqlmodel import select
    cfg(QB_ENABLED=False)
    _wrb_anime(clean_tables, "绑错季的番")
    await user.open("/?t=overview")
    await user.should_see("多半是 bgm 绑错了季")
    btn = next(b for b in _live(user, ui.button) if b.text == "打开详情")
    _click(user, btn)

    def _detail_open():
        for d in _live(user, ui.dialog):
            if d.value and any(isinstance(x, ui.label) and "绑错季的番" in str(x.text)
                               for x in d.descendants()):
                return True
        return False
    assert await _until(_detail_open), "点『打开详情』没有弹出那部番的详情"
    await user.should_see("多半是 bgm 绑错了季")          # 打开详情不等于已读
    with clean_tables.get_session() as s:
        assert not list(s.exec(select(AlertAck)))


# ---------------------------------------------------------------- warn_banner 的结构（U2-2 / U1-5）

@pytest.mark.nicegui_main_file(_MAIN)
async def test_banner_layout_with_none_one_and_many_buttons(user: User, clean_tables, cfg, monkeypatch):
    """不带按钮：文字直接在横幅行里、带 pre-line；带按钮：文字与按钮再包一层（窄屏竖排、≥640 横排），
    按钮同在一个可折行的组里；≥2 个按钮的组挂 warn-acts（桌面上封顶宽度）。只防结构被改回去，证明不了像素。"""
    cfg(QB_ENABLED=False)
    _fake_findings(monkeypatch)
    await user.open("/?t=overview")

    plain = next(lb for lb in _live(user, ui.label)
                 if str(lb.text).startswith("qB 未启用：只采集元数据") and "text-amber-400" in lb.classes)
    assert "whitespace-pre-line" in plain.classes
    assert "no-wrap" in _parent(plain).classes, "不带按钮的横幅结构变了"

    acks = [b for b in _live(user, ui.button) if b.text == "知道了"]
    groups = {id(_parent(b)): _parent(b) for b in acks}
    single = [g for g in groups.values() if len(_children(g)) == 1]
    many = [g for g in groups.values() if len(_children(g)) == 3]
    assert single and many, "前提：应有单按钮（ign）与三按钮（dup）两种横幅"
    for g in single + many:
        assert {"shrink-0", "flex-wrap"} <= set(g.classes)
        wrap = _parent(g)
        assert {"flex-col", "sm:flex-row", "sm:flex-nowrap", "min-w-0"} <= set(wrap.classes)
        label = _children(wrap)[0]
        assert isinstance(label, ui.label) and "whitespace-pre-line" in label.classes
    assert "warn-acts" not in single[0].classes
    texts = [b.text for b in _children(many[0])]
    assert texts == ["打开 #11", "打开 #12", "知道了"] and "warn-acts" in many[0].classes



@pytest.mark.nicegui_main_file(_MAIN)
async def test_each_dup_button_opens_its_own_show(user: User, clean_tables, cfg, monkeypatch):
    """dup 横幅的两个『打开 #id』按钮各开各的：按钮在循环里建，回调要按值绑住 id，
    晚绑定的话两个按钮都会打开最后那一部。"""
    from core import alerts
    cfg(QB_ENABLED=False)
    a = _wrb_anime(clean_tables, "重复甲番")
    b = _wrb_anime(clean_tables, "重复乙番")
    raw = {"a": a, "b": b, "a_born": _T0, "b_born": _T0, "a_name": "重复甲番", "b_name": "重复乙番",
           "a_bgm": 1, "b_bgm": 2, "a_confirmed": True, "a_rejected": False,
           "b_confirmed": False, "b_rejected": False, "shared": ["重复"]}
    monkeypatch.setitem(alerts._BUILD, "dup", (lambda: [raw], alerts._BUILD["dup"][1]))
    await user.open("/?t=overview")

    def _open_dialog_names():
        for d in _live(user, ui.dialog):
            if d.value:
                return d, {n for n in ("重复甲番", "重复乙番")
                           if any(isinstance(x, ui.label) and n in str(x.text) for x in d.descendants())}
        return None, set()

    for want, other in ((a, "重复乙番"), (b, "重复甲番")):
        _click(user, next(x for x in _live(user, ui.button) if x.text == f"打开 #{want}"))
        assert await _until(lambda: bool(_open_dialog_names()[1])), f"点『打开 #{want}』没有弹出详情"
        dlg, names = _open_dialog_names()
        assert other not in names and len(names) == 1, f"点『打开 #{want}』打开的是 {names}"
        dlg.close()
        assert await _until(lambda: _open_dialog_names()[0] is None)

def _css_block(css: str, head: str) -> tuple[int, str]:
    """按花括号配对取出以 head 开头的 @media 块。返回 (起始位置, 块内容)。"""
    i = css.index(head)
    j, depth = i + len(head), 1
    while depth:
        depth += {"{": 1, "}": -1}.get(css[j], 0)
        j += 1
    return i, css[i + len(head): j - 1]


def test_the_banner_button_cap_only_applies_on_wide_screens():
    """封顶宽度只在 ≥640px（按钮与文字横排时）生效；窄屏按钮在文字下面，封了反而把按钮挤成好几行。"""
    from pages import layout
    _, blk = _css_block(layout._HEAD_BASE_CSS, "@media(min-width:640px){")
    assert ".warn-acts{max-width:min(22rem,50%)}" in blk
    assert layout._HEAD_BASE_CSS.count(".warn-acts") == 1


# ---------------------------------------------------------------- 『新入库』并行（U1-2）

def _row(i, aid, ep, status, color, *, quiet, src="组A"):
    return {"id": i, "detail_id": aid, "time": "2026-11-12 05:00", "name": f"番{aid}", "src": src,
            "raw": f"raw{i}", "status": status, "status_color": color,
            "_key": (aid, ep) if aid and ep is not None and ep >= 0 else None, "_quiet": quiet}


def _fold(rows):
    from pages.anime import _fold_recent
    return _fold_recent(rows)


def _more_n(r):
    m = re.match(r"另 (\d+) 个版本：", r.get("more", ""))
    return int(m.group(1)) if m else 0


def test_fold_one_done_one_backup_becomes_one_row_with_source_in_more():
    out = _fold([_row(1, 7, 1.0, "已完成", "green", quiet=False),
                 _row(2, 7, 1.0, "备用项", "blue-grey", quiet=True, src="桜都字幕组")])
    assert [r["id"] for r in out] == [1]
    assert out[0]["more"] == "另 1 个版本：桜都字幕组 备用项"
    assert "_key" not in out[0] and "_quiet" not in out[0]


def test_fold_never_greys_out_a_second_coloured_row():
    out = _fold([_row(1, 7, 2.0, "已完成", "green", quiet=False),
                 _row(2, 7, 2.0, "失败", "red", quiet=False),
                 _row(3, 7, 2.0, "备用项", "blue-grey", quiet=True)])
    assert [(r["id"], r["status_color"]) for r in out] == [(1, "green"), (2, "red")]
    assert _more_n(out[0]) == 1 and "more" not in out[1]


def test_fold_an_all_grey_episode_into_its_first_row_and_count_repeats():
    out = _fold([_row(1, 7, 3.0, "备用项", "blue-grey", quiet=True, src="组B"),
                 _row(2, 7, 3.0, "备用项", "blue-grey", quiet=True, src="组B"),
                 _row(3, 7, 3.0, "备用项", "blue-grey", quiet=True, src="组B")])
    assert [r["id"] for r in out] == [1]
    assert out[0]["more"] == "另 2 个版本：组B 备用项 ×2"


def test_fold_leaves_unknown_special_and_orphan_rows_alone():
    rows = [_row(1, 7, -1.0, "未知集", "purple", quiet=False),
            _row(2, 7, -1.0, "备用项", "blue-grey", quiet=True),
            _row(3, 7, -2.0, "特别篇", "blue-grey", quiet=True),
            _row(4, 7, -2.0, "特别篇", "blue-grey", quiet=True),
            _row(5, None, 1.0, "跳过", "blue-grey", quiet=True),
            _row(6, None, 1.0, "跳过", "blue-grey", quiet=True)]
    assert [r["id"] for r in _fold(rows)] == [1, 2, 3, 4, 5, 6]


def test_fold_conserves_rows_and_keeps_order():
    rows = [_row(1, 7, 1.0, "备用项", "blue-grey", quiet=True),
            _row(2, 8, 1.0, "将下载", "blue", quiet=False),
            _row(3, 7, 1.0, "已完成", "green", quiet=False),     # 领头行在灰行之后
            _row(4, 8, 1.0, "备用项", "blue-grey", quiet=True),
            _row(5, 7, 2.0, "已忽略", "grey", quiet=True),
            _row(6, 7, 1.0, "备用项", "blue-grey", quiet=True)]
    out = _fold(rows)
    ids = [r["id"] for r in out]
    assert ids == [2, 3, 5], "输出必须是输入的子序列，领头行是第一条非灰的"
    assert len(out) + sum(_more_n(r) for r in out) == len(rows)
    assert _more_n(out[1]) == 2 and _more_n(out[0]) == 1


@pytest.mark.nicegui_main_file(_MAIN)
async def test_grey_already_downloaded_row_is_not_folded_away(user: User, clean_tables, cfg):
    """关了 qB 跟踪时已下的行是灰色『已下』：它不能被当成"不用管"并进一条更新的备用项里。
    （判据在 recent_panel 的 _quiet：不是已到手的状态 且 徽标中性。只看颜色的话这条就丢了。）"""
    from db.models import Anime, AnimeTorrent
    cfg(QB_ENABLED=False, QB_SYNC_STATUS=False)
    with clean_tables.get_session() as s:
        a = Anime(title="并行番", display_name="并行番", season=1, quarter="26D",
                  confirmed=True, bangumi_id=771001)
        s.add(a); s.commit(); s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="a" * 40, raw_title="[甲] 并行番 - 01", source="甲",
                           episode=1.0, status="sent", created_at=datetime.now() - timedelta(hours=2)))
        s.add(AnimeTorrent(anime_id=a.id, info_hash="b" * 40, raw_title="[乙] 并行番 - 01", source="乙",
                           episode=1.0, status="pending", created_at=datetime.now()))
        s.commit()
    await user.open("/?t=overview")
    tbl = next(t for t in _live(user, ui.table) if any(c["name"] == "src" for c in t._props["columns"]))
    assert [(r["status"], r.get("more")) for r in tbl.rows] == [("已下", "另 1 个版本：乙 备用项")]


@pytest.mark.nicegui_main_file(_MAIN)
async def test_special_episode_rows_are_never_folded(user: User, clean_tables, cfg):
    """特别篇（集号 < 0）同一个负数集号下的几条不是"同一集的几个版本"：各自独占一行。"""
    from db.models import Anime, AnimeTorrent
    cfg(QB_ENABLED=False)
    with clean_tables.get_session() as s:
        a = Anime(title="特别篇番", display_name="特别篇番", season=1, quarter="26D",
                  confirmed=True, bangumi_id=771002)
        s.add(a); s.commit(); s.refresh(a)
        for i, h in enumerate("ef"):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=h * 40, raw_title=f"[组] 特别篇番 SP{i}", source="组",
                               episode=-1.0, status="pending"))
        s.commit()
    await user.open("/?t=overview")
    tbl = next(t for t in _live(user, ui.table) if any(c["name"] == "src" for c in t._props["columns"]))
    assert [(r["status"], r.get("more")) for r in tbl.rows] == [("特别篇", None)] * 2


# ---------------------------------------------------------------- 『新入库』表在窄屏（U4-1）

_HIDE = "gt-sm"


def _recent_table_problems(columns, template) -> list[str]:
    """藏了哪一列，窄屏就得在番名格里补回来；番名能换行。返回问题列表（空＝没问题）。"""
    bad = []
    hidden = [c for c in columns if _HIDE in (c.get("classes") or "").split()]
    for c in columns:
        if (_HIDE in (c.get("classes") or "").split()) != (_HIDE in (c.get("headerClasses") or "").split()):
            bad.append(f"{c['name']} 的表头与单元格隐藏类不一致")
    name = next(c for c in columns if c["name"] == "name")
    if name.get("classes") or name.get("headerClasses"):
        bad.append("番名列不许带隐藏类")
    if 'class="lt-md' not in template:
        return bad + ["番名格里没有 lt-md 那一行"]
    blk = template[template.index('class="lt-md'):]
    for c in hidden:
        if f"props.row.{c['field']}" not in blk:
            bad.append(f"{c['name']} 列在窄屏被隐去，却没在 lt-md 那行补回来")
    if "q-badge" not in blk or "props.row.status_color" not in blk:
        bad.append("lt-md 那行没有状态徽标")
    if "status" not in [c["name"] for c in hidden]:
        bad.append("状态列没有在窄屏隐去（它会被挤出屏幕）")
    if template.count("white-space:normal;overflow-wrap:anywhere") < 1:
        bad.append("番名 div 不能换行")
    more = re.search(r'<div v-if="props\.row\.more"[^>]*>', template)
    if more and "white-space:normal" not in more.group(0):
        bad.append("『另 N 个版本』那行不能换行：q-td 是 nowrap，长串会把表格撑出屏幕（实测 360 宽时 0/18 徽标在屏内）")
    return bad


@pytest.mark.nicegui_main_file(_MAIN)
@pytest.mark.parametrize("path", ["/?t=overview", "/movies?t=overview"])
async def test_recent_table_brings_hidden_columns_back_on_narrow_screens(user: User, clean_tables, cfg, path):
    cfg(QB_ENABLED=False)
    await user.open(path)
    tbl = next(t for t in _live(user, ui.table) if any(c["name"] == "src" for c in t._props["columns"]))
    cols, tpl = tbl._props["columns"], tbl.slots["body-cell-name"].template
    assert _recent_table_problems(cols, tpl) == []
    # 反证：检查函数本身得会红
    import copy
    no_hide = copy.deepcopy(cols)
    for c in no_hide:
        if c["name"] == "status":
            c.pop("classes"); c.pop("headerClasses")
    assert _recent_table_problems(no_hide, tpl)
    assert _recent_table_problems(cols, tpl.replace("lt-md", "lt-xx"))
    assert _recent_table_problems(cols, tpl.replace("props.row.src", "props.row.raw"))


# ---------------------------------------------------------------- 仪表盘的面板顺序（U1-3）

def test_health_signals_come_before_the_growing_lists():
    """overview_tail（种子/下载/采集状态）必须排在『正在下载』『新入库』之前：生产没配推送时页面是唯一渠道，
    健康信号不许落到变长列表之后。只钉有理由的这一半，其余顺序是版面取舍、不钉。"""
    tree = ast.parse((_ROOT / "pages/anime.py").read_text(encoding="utf-8"))
    builders = next(n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "_builders" for t in n.targets))
    lam = next(v for k, v in zip(builders.keys, builders.values)
               if isinstance(k, ast.Constant) and k.value == "overview")
    names = [c.func.id for c in lam.body.elts if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
    assert names.index("overview_tail") < names.index("inflight_panel") < names.index("recent_panel")


# ---------------------------------------------------------------- 番剧表的失败/停滞徽标（U1-6）

def _show(db, name, eps, *, confirmed=True, bid, quarter=None):
    from core import anime as A
    from db.models import Anime, AnimeTorrent
    with db.get_session() as s:
        a = Anime(title=name, display_name=name, season=1, confirmed=confirmed, bangumi_id=bid,
                  quarter=quarter or A.quarter_of(datetime.now()), total_episodes=12)
        s.add(a); s.commit(); s.refresh(a)
        for ep, st in eps:
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{bid:032x}{int(ep):08x}", raw_title=f"[组] {name} - {ep}",
                               source="组", episode=float(ep), status=st))
        s.commit()
        return a.id


@pytest.mark.nicegui_main_file(_MAIN)
async def test_manage_card_flags_failed_or_stalled_only_on_tracked_shows(user: User, clean_tables, cfg):
    cfg(QB_ENABLED=False, ANIME_SHOW_PENDING=True)
    _show(clean_tables, "停滞番", [(e, "stalled" if e == 4 else "sent") for e in range(1, 7)], bid=881001)
    _show(clean_tables, "失败番", [(e, "error" if e == 5 else "sent") for e in range(1, 7)], bid=881002)
    _show(clean_tables, "干净番", [(e, "sent") for e in range(1, 4)], bid=881003)
    _show(clean_tables, "待确认番", [(1, "error")], confirmed=False, bid=881004)
    await user.open("/?t=manage")
    flagged = [b for b in _live(user, ui.badge) if b.text == "失败/停滞 1"]
    owners = set()
    for b in flagged:
        card = next(p for p in b.ancestors() if isinstance(p, ui.card))
        owners |= {x.text for x in card.descendants() if isinstance(x, ui.label)} & {
            "停滞番", "失败番", "干净番", "待确认番"}
    assert len(flagged) == 2 and owners == {"停滞番", "失败番"}
    tree = str(user.current_layout)
    assert "6/6" in tree and "5/6" in tree
    tips = [str(t.text) for t in _live(user, Tooltip)]
    assert not [t for t in tips if "已下 6 集" in t and "都到手了" in t], "停滞的番悬停还在说『都到手了』"
    # 仪表盘 KPI『订阅中』弹窗里的卡片与番剧表同一判据
    await user.open("/?t=overview")
    _click(user, _kpi_card(user, "订阅中"))
    assert await _until(lambda: any(d.value for d in _live(user, ui.dialog)))
    dlg = next(d for d in _live(user, ui.dialog) if d.value)
    assert len([x for x in dlg.descendants() if isinstance(x, ui.badge) and x.text == "失败/停滞 1"]) == 2


# ---------------------------------------------------------------- 季度小结叫『未下』（U1-8）

@pytest.mark.nicegui_main_file(_MAIN)
async def test_quarter_brief_calls_the_total_not_downloaded(user: User, clean_tables, cfg):
    cfg(QB_ENABLED=False)
    _show(clean_tables, "本季番", [(1, "sent")], bid=882001)
    from db.models import AnimeTorrent
    with clean_tables.get_session() as s:
        aid = s.exec(__import__("sqlmodel").select(AnimeTorrent.anime_id)).first()
        s.add(AnimeTorrent(anime_id=aid, info_hash="c" * 40, raw_title="[乙] 本季番 - 01", source="乙",
                           episode=1.0, status="pending"))
        s.commit()
    await user.open("/?t=manage")
    labels = [str(lb.text) for lb in _live(user, ui.label)]
    assert "未下 1" in labels and "待下 1" not in labels


# ---------------------------------------------------------------- 已读的提示（U1-7）

def _drop_bad(db, aid):
    from sqlmodel import delete
    from db.models import AnimeTorrent
    with db.get_session() as s:
        s.exec(delete(AnimeTorrent).where(AnimeTorrent.anime_id == aid))
        s.commit()


@pytest.mark.nicegui_main_file(_MAIN)
async def test_acked_line_counts_only_what_still_holds_and_never_deletes(user: User, clean_tables, cfg):
    from core import alerts
    cfg(QB_ENABLED=False)
    a1 = _wrb_anime(clean_tables, "已读甲")
    a2 = _wrb_anime(clean_tables, "已读乙")
    for f in alerts.scan():
        alerts.ack(f["ident"], f["kind"], f["fact"], f["text"])
    from db.models import AlertAck
    from sqlmodel import select
    with clean_tables.get_session() as s:            # 甲先标、乙后标：列表默认按标记时间倒序，乙在前
        for r in s.exec(select(AlertAck)):
            r.created_at = datetime(2026, 1, 2) if "已读乙" in r.summary else datetime(2026, 1, 1)
            s.add(r)
        s.commit()
    _drop_bad(clean_tables, a2)                         # 后标的乙那条不成立了（排序守卫：它不能排在仍成立的前面）
    await user.open("/?t=overview")
    await user.should_see("已读的提示 1 条")
    await user.should_not_see("不成立")
    _click(user, next(b for b in _live(user, ui.button) if b.text == "查看"))
    await user.should_see("仍成立 1 条")
    await user.should_see("已不成立 1 条")
    # 对话框里仍成立的排前面
    dlg = next(d for d in _live(user, ui.dialog) if d.value)
    states = [b.text for b in dlg.descendants() if isinstance(b, ui.badge)
              and b.text in ("现在仍然成立", "现在已经不成立了")]
    assert states == ["现在仍然成立", "现在已经不成立了"]

    _drop_bad(clean_tables, a1)
    await user.open("/?t=overview")
    await user.should_not_see("已读的提示")
    assert len(alerts.view()["acked"]) == 2, "已读记录被自动删了"


# ---------------------------------------------------------------- 待确认卡片的来源（U1-9）

@pytest.mark.nicegui_main_file(_MAIN)
async def test_confirm_card_names_the_source_group(user: User, clean_tables, cfg):
    cfg(QB_ENABLED=False)
    _show(clean_tables, "待确认来源番", [(1, "pending")], confirmed=False, bid=883001)
    from db.models import AnimeTorrent
    from sqlmodel import update
    with clean_tables.get_session() as s:
        s.exec(update(AnimeTorrent).values(source="桜都字幕组"))
        s.commit()
    await user.open("/?t=confirm")
    # 只认卡片上的那行字：左下角『单源 1』的悬停说明里也有同一句，不能拿它充数
    labels = [str(x.text) for x in _live(user, ui.label) if type(x) is ui.label]
    assert "来源: 桜都字幕组" in labels


# ---------------------------------------------------------------- 待识别卡片的两枚徽标（U1-10）

@pytest.mark.nicegui_main_file(_MAIN)
async def test_fail_card_badges_explain_themselves(user: User, clean_tables, cfg):
    from db.models import Anime
    cfg(QB_ENABLED=False)
    with clean_tables.get_session() as s:
        s.add(Anime(title="没绑甲", season=1, quarter="26D", confirmed=True))
        s.add(Anime(title="没绑乙", season=1, quarter="26D", confirmed=False))
        s.commit()
    await user.open("/?t=fail")
    tips = {t.props.get("target"): str(t.text) for t in _live(user, Tooltip)}
    got = {}
    for b in _live(user, ui.badge):
        if b.text in ("自动", "待确认"):
            assert f"#{b.html_id}" in tips, f"『{b.text}』徽标没有说明"
            got[b.text] = tips[f"#{b.html_id}"]
    assert set(got) == {"自动", "待确认"}
    assert "放回『待确认』" in got["自动"], "说明与事实相反：手动绑定一律放回待确认"
    assert "后台不会下它" in got["待确认"]


# ---------------------------------------------------------------- 失败/异常清单（U4-4）

def test_failed_rows_carry_status_reason_and_episode(clean_tables):
    from core import anime as A, movies as M
    from db.models import Movie, MovieTorrent
    aid = _show(clean_tables, "失败行番", [(6, "error"), (3, "stalled")], bid=884001)
    from db.models import AnimeTorrent
    from sqlmodel import update
    with clean_tables.get_session() as s:
        s.exec(update(AnimeTorrent).where(AnimeTorrent.status == "error").values(
            fail_reason="取种失败：HTTP 404"))
        m = Movie(title="失败片", quarter="2026", bangumi_id=884002)
        s.add(m); s.commit(); s.refresh(m)
        s.add(MovieTorrent(movie_id=m.id, info_hash="d" * 40, raw_title="mv", status="stalled",
                           fail_reason="72 小时没有进展"))
        s.commit()
    rows = {r["episode"]: r for r in A.failed_rows() if r["anime_id"] == aid}
    assert rows[6.0]["status"] == "error" and rows[6.0]["fail_reason"] == "取种失败：HTTP 404"
    assert rows[3.0]["status"] == "stalled" and rows[3.0]["fail_reason"] == ""
    mrows = M.failed_rows()
    assert [(r["status"], r["fail_reason"]) for r in mrows] == [("stalled", "72 小时没有进展")]


def _kpi_card(user, label):
    lb = next(x for x in _live(user, ui.label) if x.text == label)
    return next(p for p in lb.ancestors() if isinstance(p, ui.card))


@pytest.mark.nicegui_main_file(_MAIN)
async def test_failed_list_tells_the_two_rows_of_one_show_apart(user: User, clean_tables, cfg):
    from db.models import AnimeTorrent
    from sqlmodel import update
    cfg(QB_ENABLED=False)
    _show(clean_tables, "两行番", [(6, "error"), (3, "stalled")], bid=885001)
    with clean_tables.get_session() as s:
        s.exec(update(AnimeTorrent).where(AnimeTorrent.status == "error").values(
            fail_reason="取种失败：HTTP 404 Not Found"))
        s.commit()
    await user.open("/?t=overview")
    _click(user, _kpi_card(user, "失败/异常"))
    await user.should_see("失败 / 异常 · 2")
    dlg = next(d for d in _live(user, ui.dialog) if d.value)
    labels = [str(x.text) for x in dlg.descendants() if isinstance(x, ui.label)]
    badges = [x.text for x in dlg.descendants() if isinstance(x, ui.badge)]
    assert "两行番  第6集" in labels and "两行番  第3集" in labels
    assert sorted(badges) == ["停滞", "失败"]
    assert any("HTTP 404" in t for t in labels)



@pytest.mark.nicegui_main_file(_MAIN)
async def test_movie_failed_list_marks_failed_and_stalled_with_reason(user: User, clean_tables, cfg):
    """剧场版侧同一张清单：每行标出失败还是停滞，有原因就写出来。"""
    from db.models import Movie, MovieTorrent
    cfg(QB_ENABLED=False)
    with clean_tables.get_session() as s:
        m = Movie(title="两版片", quarter="2026", bangumi_id=887001)
        s.add(m); s.commit(); s.refresh(m)
        s.add(MovieTorrent(movie_id=m.id, info_hash="e" * 40, raw_title="[组] 两版片 1080p", status="error",
                           fail_reason="取种失败：HTTP 404"))
        s.add(MovieTorrent(movie_id=m.id, info_hash="f" * 40, raw_title="[组] 两版片 720p", status="stalled"))
        s.commit()
    await user.open("/movies?t=overview")
    _click(user, _kpi_card(user, "失败/异常"))
    await user.should_see("失败 / 异常 · 2")
    dlg = next(d for d in _live(user, ui.dialog) if d.value)
    labels = [str(x.text) for x in dlg.descendants() if isinstance(x, ui.label)]
    badges = [x.text for x in dlg.descendants() if isinstance(x, ui.badge)]
    assert sorted(badges) == ["停滞", "失败"]
    assert any("HTTP 404" in t for t in labels)

@pytest.mark.nicegui_main_file(_MAIN)
async def test_unknown_episode_list_shows_no_status_badge(user: User, clean_tables, cfg):
    cfg(QB_ENABLED=False)
    _show(clean_tables, "未知集番", [(-2, "pending")], bid=886001)
    await user.open("/?t=overview")
    chip = next(b for b in _live(user, ui.badge) if str(b.text).startswith("未知集"))
    _click(user, chip)
    await user.should_see("未知集 · 1")
    dlg = next(d for d in _live(user, ui.dialog) if d.value)
    assert not [x for x in dlg.descendants() if isinstance(x, ui.badge)]
    assert "未知集番  第?集" in [str(x.text) for x in dlg.descendants() if isinstance(x, ui.label)]


# ---------------------------------------------------------------- 手机档 CSS（U4-2 / U4-3 / U4-6）

_PHONE = "@media(max-width:599px){"


def test_phone_block_holds_padding_kpi_columns_and_tabs():
    from pages import layout
    css = layout._HEAD_BASE_CSS
    assert css.count(_PHONE) == 1, "全站只开一个 599 的手机档块"
    pos, blk = _css_block(css, _PHONE)
    assert ":root{--nicegui-default-padding:.5rem}" in blk
    assert ".kpi-group{grid-template-columns:repeat(var(--kpi-m" in blk, (
        "KPI 3 列必须限定在手机档：不限定的话 620px 宽时两组并排、单卡被压到 78px")
    assert pos > css.index(".kpi-group{display:grid"), "手机档要排在 2 列那条之后（同特异性靠先后）"
    assert "@layer" not in css, "这段必须不属任何 @layer，才压得过 layer(nicegui) 里的 nicegui.css"


def test_six_tabs_fit_a_360px_phone():
    """手机档 tab 内边距 N：每个 tab ≈ max(14px×字数, 24px 图标) + 2N，加起来要放进 360px 屏的 tab 条（312px）。
    加第七个 tab 或写成四个字就会红：那时当场决定缩字还是给 ui.tabs() 加 mobile-arrows。"""
    from pages import anime, layout, movies
    _, blk = _css_block(layout._HEAD_BASE_CSS, _PHONE)
    n = int(re.search(r"\.q-tab\{padding:0 (\d+)px\}", blk).group(1))
    for defs in (anime._TABS_DEF, movies._TABS_DEF):
        assert sum(max(14 * len(name), 24) + 2 * n for _, name, _ in defs) <= 312, defs


def test_nicegui_still_drives_padding_through_that_variable_in_a_layer():
    """外部前提：nicegui.css 在 :root 定义 --nicegui-default-padding，四层容器的 padding 都吃它，
    且以 layer(nicegui) 引入。NiceGUI 升级改了变量名或分层时这里红，而不是手机布局悄悄退回 60px 留白。"""
    import nicegui
    base = Path(nicegui.__file__).parent
    css = (base / "static" / "nicegui.css").read_text(encoding="utf-8")
    assert re.search(r":root\s*\{[^}]*--nicegui-default-padding\s*:", css)
    rule = re.search(r"([^{}]*)\{[^}]*padding:\s*var\(--nicegui-default-padding\)", css)
    assert rule, "nicegui.css 里没有吃这个变量的 padding 规则了"
    for sel in (".nicegui-content", ".nicegui-tab-panel", ".nicegui-card", ".nicegui-expansion-content"):
        assert sel in rule.group(1), sel
    html = (base / "templates" / "index.html").read_text(encoding="utf-8")
    assert re.search(r"nicegui\.css\"\)\s*layer\(nicegui\)", html)


@pytest.mark.nicegui_main_file(_MAIN)
async def test_phone_css_reaches_the_rendered_page(user: User, clean_tables, cfg):
    from pages import layout
    cfg(QB_ENABLED=False)
    await user.open("/?t=overview")
    assert _PHONE in user.client.head_html and layout._HEAD_BASE_CSS in user.client.head_html


@pytest.mark.nicegui_main_file(_MAIN)
@pytest.mark.parametrize("path", ["/?t=overview", "/movies?t=overview"])
async def test_kpi_groups_fill_whole_rows_on_phones(user: User, clean_tables, cfg, path):
    """每组 n 张卡、手机上 m 列（奇数张时第一张跨两列）：不留空洞、每组不超过两行。"""
    cfg(QB_ENABLED=False)
    await user.open(path)
    groups = [e for e in user.client.elements.values() if "kpi-group" in e.classes]
    assert groups
    for g in groups:
        n, m = len(_children(g)), int(g._style["--kpi-m"])
        cells = n + n % 2
        assert m in (2, 3) and cells % m == 0 and cells // m <= 2, (path, n, m)


# ---------------------------------------------------------------- 正在下载的番名 / 环图图例（U4-9）

def _fn_src(path, name):
    """函数源码，去掉整行注释（注释里解释为什么不用 break-all，不算用了它）。"""
    src = (_ROOT / path).read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == name)
    return "\n".join(ln for ln in ast.get_source_segment(src, fn).splitlines()
                     if not ln.lstrip().startswith("#"))


@pytest.mark.parametrize("path", ["pages/anime.py", "pages/movies.py"])
def test_inflight_names_break_between_words(path):
    seg = _fn_src(path, "inflight_panel")
    assert "break-all" not in seg and "overflow-wrap:anywhere" in seg


def test_donut_legend_width_fits_the_narrowest_canvas_of_each_tier():
    """图例从画布 50%+35px 起画：每档的换行宽度 ≤ 该档最窄画布/2 − 35。
    最窄画布 = 下一档 maxWidth+1，最后一档取 360px 手机的 276px。删掉窄档或把宽度调大就红。"""
    tree = ast.parse((_ROOT / "pages/anime.py").read_text(encoding="utf-8"))
    media = next(ast.literal_eval(v) for d in ast.walk(tree) if isinstance(d, ast.Dict)
                 for k, v in zip(d.keys, d.values) if isinstance(k, ast.Constant) and k.value == "media")
    tiers = sorted(media, key=lambda t: -t["query"]["maxWidth"])
    for i, t in enumerate(tiers):
        narrowest = tiers[i + 1]["query"]["maxWidth"] + 1 if i + 1 < len(tiers) else 276
        width = t["option"]["legend"]["textStyle"]["width"]
        assert width <= narrowest / 2 - 35, (t, narrowest)
