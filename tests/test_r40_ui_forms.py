"""设置页 / 日志页 / 订阅源 / 手动下载 / 解析测试 / 剧场版列表：表单与文案的守卫。

· 开始使用日：确认框按【这次填的日期】报会停掉哪几部在追番、几部待确认判超期、几部放回；
  预览与随后两个执行函数逐条同判据（数字对得上、名字就是被停掉的那部、预览不改库）。
· 关『读取 qB 实时状态』且有在途种子：保存前先问（关掉会把它们记成已下完、不再跟踪）；取消＝整次不存；没有在途不问。
· 保存提示点名真改了哪几项（没改就说没有改动）；未保存标记按与已保存值比较，改回原值 / 保存 / 应用开始使用日后收起。
· 日志页：行头压成『月-日 时:分:秒』（INFO 不写级别、WARNING 照写），计数说出最早一条到几点；缓冲里存的不变。
· 带示例的输入框必须 stack-label（Quasar 把未浮起的空框的 placeholder 设成透明）。
· 手机（<480px）设置页栅格 1 列且清掉 col-span；数据库徽标、备份行允许换行（手机上横向溢出）。
· 解析测试：结果卡排在示例列表之前。
· 订阅源：试抓只读、feed 只请求一次、与采集轮同一写法构造源、出错脱敏；卡片未保存标记（收起排在回写之后）。
· 巡检区拆小节：每个阈值的说明与控件在同一小节，每个帮助气泡 ≤700 字。
· 手动下载：『下载』跑着时 loading，成功后清掉用掉的链接，失败保留。
· 剧场版列表卡片『挑版本』『忽略』那一行不许 no-wrap（手机上一字一行）。
· 界面文案不出现配置键名（QB_ENABLED 之类）；剧场版页一律『版本』『可下载』；三处旧叫法不回来。
"""
import ast
import asyncio
import re
import sys
from pathlib import Path

import pytest
from nicegui import ui
from nicegui.elements.upload_files import SmallFileUpload
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction
from nicegui.testing.user_notify import UserNotify
from sqlmodel import select

import config
import db
from core import anime as A
from db.models import Anime, AnimeTorrent, Setting

db.init_db()
config.load_from_db()
db.apply_configured_backend()

_MAIN = "tests/render_main.py"
_ROOT = Path(__file__).resolve().parent.parent
_BASE = dict(QB_ENABLED=False, QB_SYNC_STATUS=False, DOWN_PATH="/data", ANIME_DOWN_PATH="", MOVIE_DOWN_PATH="",
             ANIME_POLL_ENABLED=False, ANIME_START_DATE="", WEB_ALLOW_CIDRS="", PROXY_URL="")


async def _until(pred, tries=100):
    for _ in range(tries):
        if pred():
            return True
        await asyncio.sleep(0.05)
    return pred()


def _live(user, kind):
    """页面上还活着的 kind 控件，【含隐藏的】（user.find 只给可见的，未保存标记默认是隐藏的）。"""
    return [e for e in list(user.client.elements.values()) if isinstance(e, kind) and not e.is_deleted]


def _click(user, el):
    UserInteraction(user, {el}, None).click()


def _button(user, text):
    bs = [b for b in _live(user, ui.button) if b.text == text]
    assert bs, f"找不到『{text}』按钮"
    return bs[-1]


def _label(user, text):
    ls = [x for x in _live(user, ui.label) if x.text == text]
    assert len(ls) == 1, f"按文字 {text!r} 找到 {len(ls)} 个 label"
    return ls[0]


def _input(user, label):
    got = [i for i in _live(user, ui.input) + _live(user, ui.number) if i.props.get("label") == label]
    assert len(got) == 1, f"按 label={label!r} 找到 {len(got)} 个输入框"
    return got[0]


def _meta(key):
    with db.get_meta_session() as s:
        row = s.get(Setting, key)
        return row.value if row else None


@pytest.fixture
def page(clean_tables, cfg, monkeypatch):
    """设置页公共准备：基准配置 + 记下每条 notify + 拦掉 .env 写入。"""
    cfg(**_BASE)
    monkeypatch.setattr(config, "update_env", lambda updates: None)
    notes = []

    def _record(self, message, **kw):
        self.messages.append(message)
        notes.append(message)
    monkeypatch.setattr(UserNotify, "__call__", _record)
    return {"db": clean_tables, "notes": notes}


def _add_anime(s, title, air_date, confirmed=False, rejected=False, bangumi_id=None, total=None, eps=()):
    a = Anime(title=title, display_name=title, air_date=air_date, confirmed=confirmed, rejected=rejected,
              bangumi_id=bangumi_id, total_episodes=total, quarter="26A")
    s.add(a); s.commit(); s.refresh(a)
    for i, ep in enumerate(eps):
        s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{a.id:08x}{i:032x}", raw_title=f"[组] {title} - {ep}",
                           episode=ep, status="sent", qb_progress=1.0))
    s.commit()
    return a.id


# ================================================================ U3-3 开始使用日预览

def _snapshot(s):
    return sorted((a.id, a.confirmed, a.rejected, a.confirm_reason) for a in s.exec(select(Anime)))


def test_start_date_preview_matches_what_apply_does(clean_tables, cfg):
    """预览的三个数与随后两个执行函数的返回值一致，stop 里正是被停掉的那部；预览本身不改库。"""
    with clean_tables.get_session() as s:
        stop_id = _add_anime(s, "在追老番", "2020-01-05", confirmed=True)
        _add_anime(s, "待确认老番", "2020-04-01")
        _add_anime(s, "超期忽略新番", "2026-10-01", rejected=True)
        # 绑定看着不对（单集条目收到多个正片集号）→ 两个执行函数都让路，预览也必须让路
        _add_anime(s, "绑错的在追老番", "2019-01-01", confirmed=True, bangumi_id=999001, total=1, eps=(1, 2))
        _add_anime(s, "开播日未知的在追番", None, confirmed=True)
        # 已忽略（confirmed 与 rejected 都为真）两个执行函数都不碰：不论新老都不能算进停掉 / 放回
        _add_anime(s, "人工忽略新番", "2026-10-01", confirmed=True, rejected=True)
        _add_anime(s, "人工忽略老番", "2020-01-01", confirmed=True, rejected=True)
        before = _snapshot(s)
    pv = A.start_date_preview("2026-01-01")
    with clean_tables.get_session() as s:
        assert _snapshot(s) == before, "预览改了库"
    assert pv == {"stop": ["在追老番"], "ignored": 1, "released": 1}, pv
    cfg(ANIME_START_DATE="2026-01-01")
    assert A.apply_start_date_filter() == pv["ignored"] + pv["released"]
    assert A.ignore_confirmed_before_start() == len(pv["stop"])
    with clean_tables.get_session() as s:
        a = s.get(Anime, stop_id)
        assert (a.confirmed, a.rejected) == (False, True), "stop 里报的那部没被停掉"


def test_start_date_preview_empty_date_stops_nothing(clean_tables):
    """空开始日＝不限：没有番算超期，在追的一部都不停，超期忽略的全放回。"""
    with clean_tables.get_session() as s:
        _add_anime(s, "在追老番", "2020-01-05", confirmed=True)
        _add_anime(s, "超期忽略老番", "2020-01-05", rejected=True)
    assert A.start_date_preview("") == {"stop": [], "ignored": 0, "released": 1}


@pytest.mark.nicegui_main_file(_MAIN)
async def test_apply_start_date_dialog_names_what_will_stop(user: User, page):
    """确认框写出解析后的日期、会停掉几部与番名；点取消什么都不变（在追仍在追、日期没存）。"""
    with page["db"].get_session() as s:
        aid = _add_anime(s, "孤独摇滚老番", "2022-10-08", confirmed=True)
    before = _meta("ANIME_START_DATE")
    await user.open("/settings")
    _input(user, "开始使用日").value = "2026"
    _click(user, _button(user, "应用开始使用日过滤"))
    await user.should_see("开始使用日：2026-01-01")
    await user.should_see("正在追的 1 部会停掉、移到『已忽略』：孤独摇滚老番")
    await user.should_see("要逐部重新确认")
    ok = _button(user, "保存并应用")
    assert ok.props.get("color") == "negative", "要停掉在追番时确认按钮该是警示色"
    _click(user, _button(user, "取消"))
    await _until(lambda: not [b for b in _live(user, ui.button) if b.text == "保存并应用"])
    with page["db"].get_session() as s:
        assert s.get(Anime, aid).confirmed is True
    assert config.ANIME_START_DATE == "" and _meta("ANIME_START_DATE") == before


@pytest.mark.nicegui_main_file(_MAIN)
async def test_apply_start_date_dialog_says_nothing_affected(user: User, page):
    await user.open("/settings")
    _input(user, "开始使用日").value = "2026-07-01"
    _click(user, _button(user, "应用开始使用日过滤"))
    await user.should_see("没有番会受影响，只保存这个日期。")
    assert _button(user, "保存并应用").props.get("color") == "primary"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_apply_start_date_dialog_names_released(user: User, page):
    """超期忽略的番开播日晚于新填的日期 → 确认框要说会把它放回待确认。"""
    with page["db"].get_session() as s:
        _add_anime(s, "超期忽略新番", "2026-10-01", rejected=True)
    await user.open("/settings")
    _input(user, "开始使用日").value = "2026-07-01"
    _click(user, _button(user, "应用开始使用日过滤"))
    await user.should_see("1 部超期忽略的放回『待确认』")


# ================================================================ U3-3 关 qB 跟踪先确认

def _sync_switch(user):
    sw = [x for x in _live(user, ui.switch) if x.text.startswith("读取 qB 实时状态")]
    assert len(sw) == 1
    return sw[0]


@pytest.mark.nicegui_main_file(_MAIN)
async def test_turning_off_qb_sync_with_inflight_asks_first(user: User, page, cfg):
    cfg(QB_SYNC_STATUS=True)
    with page["db"].get_session() as s:
        a = Anime(title="在下的番", display_name="在下的番", quarter="26A", confirmed=True)
        s.add(a); s.commit(); s.refresh(a)
        t = AnimeTorrent(anime_id=a.id, info_hash="ab" * 20, raw_title="[组] 在下的番 - 01", episode=1,
                         status="sent", qb_progress=0.3)
        s.add(t); s.commit(); s.refresh(t)
        tid = t.id
    await user.open("/settings")
    _sync_switch(user).value = False
    _click(user, _button(user, "保存"))
    await user.should_see("关闭 qB 状态跟踪？")
    await user.should_see("现在有 1 条种子还在下载")
    _click(user, _button(user, "取消"))
    await _until(lambda: not [b for b in _live(user, ui.button) if b.text == "关闭并记为已下完"])
    await asyncio.sleep(0.1)
    assert config.QB_SYNC_STATUS is True, "取消了还是关掉了跟踪"
    with page["db"].get_session() as s:
        t = s.get(AnimeTorrent, tid)
        assert (t.status, t.qb_progress) == ("sent", 0.3), "取消了还是把在下的种子落定了"
    assert not [n for n in page["notes"] if n.startswith("已保存")], page["notes"]
    assert any(n.startswith("已取消保存") for n in page["notes"]), f"取消后页面一句话也没有：{page['notes']}"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_turning_off_qb_sync_without_inflight_saves_directly(user: User, page, cfg):
    cfg(QB_SYNC_STATUS=True)
    await user.open("/settings")
    _sync_switch(user).value = False
    _click(user, _button(user, "保存"))
    await user.should_see("已保存，即时生效")
    assert config.QB_SYNC_STATUS is False
    assert not [b for b in _live(user, ui.button) if b.text == "关闭并记为已下完"], "没有在途种子也弹了确认框"


def _add_inflight(s):
    a = Anime(title="在下的番", display_name="在下的番", quarter="26A", confirmed=True)
    s.add(a); s.commit(); s.refresh(a)
    s.add(AnimeTorrent(anime_id=a.id, info_hash="ab" * 20, raw_title="[组] 在下的番 - 01", episode=1,
                       status="sent", qb_progress=0.3))
    s.commit()


@pytest.mark.nicegui_main_file(_MAIN)
async def test_saving_with_qb_sync_already_off_never_asks(user: User, page):
    """跟踪本来就关着：有在途行也不问（这次保存不会去落定任何种子）。"""
    with page["db"].get_session() as s:
        _add_inflight(s)
    await user.open("/settings")
    _click(user, _button(user, "保存"))
    await user.should_see("已保存，即时生效")
    assert not [b for b in _live(user, ui.button) if b.text == "关闭并记为已下完"], "跟踪本来就关着也弹了确认框"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_turning_off_qb_sync_still_saves_when_count_fails(user: User, page, cfg, monkeypatch):
    """数在途时业务库抛错：不问、照常保存（与首次开采集那段同口径），不能让整次保存无声失败。"""
    from core import engine

    def boom(_):
        raise RuntimeError("业务库停摆")
    monkeypatch.setattr(engine, "inflight_count", boom)
    cfg(QB_SYNC_STATUS=True)
    await user.open("/settings")
    _sync_switch(user).value = False
    _click(user, _button(user, "保存"))
    await user.should_see("已保存，即时生效")
    assert config.QB_SYNC_STATUS is False


# ================================================================ U3-4 保存说出改了什么 + 未保存标记

def _same_env(monkeypatch):
    """副本里没有 .env：绑定地址 / 端口两项按『与 .env 不同』恒算改动。让 .env 读出来就是当前值。"""
    monkeypatch.setattr(sys.modules["pages.settings"], "_env_values",
                        lambda: {"WEB_HOST": config.WEB_HOST, "WEB_PORT": str(config.WEB_PORT)})


@pytest.mark.nicegui_main_file(_MAIN)
async def test_save_names_what_changed(user: User, page, monkeypatch):
    _same_env(monkeypatch)
    await user.open("/settings")
    _click(user, _button(user, "保存"))
    await user.should_see("已保存，即时生效")
    assert page["notes"][-1] == "已保存，即时生效（没有改动）", page["notes"]
    _input(user, "轮询间隔（秒）").value = config.ANIME_POLL_INTERVAL + 60
    _click(user, _button(user, "保存"))
    await _until(lambda: len(page["notes"]) >= 2)
    assert page["notes"][-1].startswith("已保存，即时生效（改了 1 项：轮询间隔）"), page["notes"]
    # 番剧 / 剧场版两边同名（建页顺序番剧在前）：只改剧场版那一项，提示要说是哪一边
    movie_years = [i for i in _live(user, ui.number) if i.props.get("label") == "分页 · 每页年数"][-1]
    movie_years.value = 5 if config.MOVIE_PAGE_YEARS != 5 else 4
    _click(user, _button(user, "保存"))
    await _until(lambda: len(page["notes"]) >= 3)
    assert page["notes"][-1].startswith("已保存，即时生效（改了 1 项：剧场版·分页 · 每页年数）"), page["notes"]


@pytest.mark.nicegui_main_file(_MAIN)
async def test_unsaved_marker_follows_the_saved_values(user: User, page):
    await user.open("/settings")
    hdr, btm = _label(user, "未保存"), _label(user, "有未保存的改动")
    assert not hdr.visible and not btm.visible, "刚打开就标了未保存"
    site = _input(user, "站点名")
    orig = site.value
    site.value = orig + "改"
    assert hdr.visible and btm.visible, "改了输入框没出标记"
    site.value = orig
    assert not hdr.visible and not btm.visible, "改回原值标记没消失"
    site.value = orig + "改"
    _click(user, _button(user, "保存"))
    await user.should_see("已保存，即时生效")
    assert not hdr.visible and not btm.visible, "保存成功后标记没收起"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_edits_made_while_saving_stay_marked_unsaved(user: User, page, cfg, monkeypatch):
    """保存要等 qB 登录的那几秒里用户又改了一项：它没存进库，『未保存』标记就不能收起。"""
    from core import engine
    cfg(QB_ENABLED=True)
    await user.open("/settings")
    site = _input(user, "站点名")
    _input(user, "qB 地址").value = "http://10.9.9.9:8080"     # 改连接设置 → 保存时要登录一次

    async def noop():
        return None

    async def login():
        site.value = site.value + "中途改"
        return None
    monkeypatch.setattr(engine.qb, "reset_cooldown", noop)
    monkeypatch.setattr(engine.qb, "_login", login)
    _click(user, _button(user, "保存"))
    assert await _until(lambda: any(n.startswith("已保存，即时生效") for n in page["notes"])), page["notes"]
    assert _label(user, "未保存").visible, "登录期间改的站点名没存进库，标记却收起了"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_apply_start_date_clears_only_its_own_dirt(user: User, page):
    await user.open("/settings")
    hdr = _label(user, "未保存")
    _input(user, "开始使用日").value = "2026-07-01"
    assert hdr.visible
    _click(user, _button(user, "应用开始使用日过滤"))
    await user.should_see("开始使用日：2026-07-01")
    _click(user, _button(user, "保存并应用"))
    assert await _until(lambda: any(n.startswith("已保存") for n in page["notes"]))
    assert config.ANIME_START_DATE == "2026-07-01"
    assert not hdr.visible, "开始使用日已经存了，标记还亮着"
    _input(user, "站点名").value = "别的改动"
    _input(user, "开始使用日").value = "2026-08-01"
    _click(user, _button(user, "应用开始使用日过滤"))
    await user.should_see("开始使用日：2026-08-01")
    _click(user, _button(user, "保存并应用"))
    await _until(lambda: config.ANIME_START_DATE == "2026-08-01")
    assert hdr.visible, "还有别的项没保存，标记却收起了"


# ================================================================ U3-5 日志页

def test_short_log_head():
    from pages.logs import _short
    assert _short("2026-11-12 04:59:13,215 INFO 源 ANi：75 条，新增 0") == "11-12 04:59:13 源 ANi：75 条，新增 0"
    assert _short("2026-11-12 04:59:13,215 WARNING qB 连不上") == "11-12 04:59:13 WARNING qB 连不上"
    assert _short("Traceback (most recent call last):") == "Traceback (most recent call last):"
    multi = "2026-11-12 04:59:13,215 ERROR 本轮异常\nTraceback (most recent call last):\n  File \"x\", line 1"
    assert _short(multi) == "11-12 04:59:13 ERROR 本轮异常\nTraceback (most recent call last):\n  File \"x\", line 1"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_logs_page_shows_short_head_and_oldest(user: User, monkeypatch):
    from core import logsetup as L
    P = sys.modules["pages.logs"]
    r = L.RingHandler(200)
    r.buf.append({"levelno": 20, "level": "INFO", "line": "2026-11-11 20:36:36,807 INFO 源 LoliHouse：18 条，新增 0"})
    r.buf.append({"levelno": 20, "level": "INFO", "line": "2026-11-12 04:59:13,215 INFO 源 ANi：75 条，新增 0"})
    monkeypatch.setattr(L, "ring", r)
    monkeypatch.setattr(P, "ring", r)
    await user.open("/logs")
    await user.should_see("11-11 20:36:36 源 LoliHouse")
    await user.should_see("最近 2 条，最早到 11-11 20:36（新→旧）")
    await user.should_not_see("2026-11-11 20:36:36,807 INFO")


# ================================================================ U3-6 示例常显

@pytest.mark.nicegui_main_file(_MAIN)
async def test_settings_placeholders_show_while_empty(user: User):
    """带标签的输入框空着且没聚焦时，Quasar 把 placeholder 设成透明（.q-field--labeled:not(.q-field--float)）；
    写了示例/默认值的框必须 stack-label，否则那句话只有点进去才看得见。"""
    await user.open("/settings")
    boxes = [e for e in user.client.elements.values()
             if isinstance(e, ui.input) and e.props.get("placeholder") and e.props.get("label")]
    assert len(boxes) >= 10, f"只找到 {len(boxes)} 个带示例的输入框，扫描可能失效了"
    bad = [e.props["label"] for e in boxes if "stack-label" not in e.props]
    assert not bad, f"这些输入框的示例空着时看不见：{bad}"


# ================================================================ U3-7 手机上的设置页

def test_field_grid_goes_single_column_on_phones():
    """后半句钉的是 col-span 的坑：删掉它，『代理地址』那一行会在单列栅格里撑出一条隐式第二列。"""
    from pages.layout import _HEAD_BASE_CSS
    css = _HEAD_BASE_CSS.replace(" ", "")
    assert "@media(max-width:479px){.field-grid{grid-template-columns:minmax(0,1fr)}" in css
    assert ".field-grid>*{grid-column:auto}" in css


@pytest.mark.nicegui_main_file(_MAIN)
async def test_db_badge_and_backup_rows_may_wrap(user: User, monkeypatch):
    """数据库徽标、配置库路径、备份行在手机上必须能换行（不换行时整页横向撑到 996px / 484px）。"""
    from db import backup as B
    fake = [{"name": "autorss-full-20261112-030000.db", "path": "/x", "bytes": 2048, "has_data": True,
             "backend": None, "mysql_db": None, "pre_upgrade": False, "detail": "番剧 3 部"}]
    monkeypatch.setattr(B, "list_backups", lambda: fake)
    await user.open("/settings")
    badge = next(b for b in _live(user, ui.badge) if b.text == db.data_target_desc())
    assert {"whitespace-normal", "break-all", "max-w-full"} <= set(badge.classes), badge.classes
    cfg_line = next(x for x in _live(user, ui.label) if x.text.startswith("配置库（不参与迁移）"))
    assert "break-all" in cfg_line.classes
    name = _label(user, fake[0]["name"])
    row = name.parent_slot.parent
    assert "flex-wrap" in row.classes and "no-wrap" not in row.classes, row.classes


# ================================================================ U3-8 解析测试

@pytest.mark.nicegui_main_file(_MAIN)
async def test_parse_result_sits_above_the_examples(user: User):
    """解析结果要紧跟输入框：排在 13 行示例下面时，粘贴后首屏什么都没变（手机上要滚过一整屏）。"""
    await user.open("/parse")
    inp = next(e for e in user.client.elements.values()
               if isinstance(e, ui.input) and e.props.get("label") == "种子标题")
    inp.value = "[ANi] 葬送的芙莉莲 - 07 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]"
    await user.should_see("会被采集入库")
    labels = [e for e in user.client.elements.values() if isinstance(e, ui.label) and not e.is_deleted]
    verdict = next(x for x in labels if x.text == "会被采集入库")
    hint = next(x for x in labels if x.text == "点一个示例填入：")
    siblings = hint.parent_slot.children
    top = next(a for a in [verdict, *verdict.ancestors()] if a in siblings)
    assert siblings.index(top) < siblings.index(hint), "解析结果排到示例列表下面去了"


# ================================================================ U3-9 订阅源：文案 + 试抓

_H = "0123456789abcdef0123456789abcdef0123456{}"
_FEED = ('<?xml version="1.0"?><rss version="2.0" xmlns:nyaa="https://nyaa.si/xmlns/nyaa"><channel>'
         + "".join(f"<item><title>{t}</title><link>https://nyaa.si/download/{i}.torrent</link>"
                   f"<pubDate>Wed, 11 Nov 2026 12:00:00 -0000</pubDate>"
                   f"<nyaa:infoHash>{_H.format(i)}</nyaa:infoHash></item>"
                   for i, t in enumerate([
                       "[喵萌奶茶屋] 某番 - 03 [简日双语][1080p]",
                       "[喵萌奶茶屋] 某番 - 03 [繁日雙語][1080p]",
                       "[LoliHouse] 别的番 - 05 [WebRip 1080p]"]))
         + "</channel></rss>").encode()
_EMPTY_FEED = b'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'


@pytest.fixture
def one_fetch(monkeypatch):
    from services import fetch
    calls = []
    body = {"v": _FEED}

    async def fake(client, url, **kw):
        calls.append(url)
        return body["v"]
    monkeypatch.setattr(fetch, "get_bytes", fake)
    return calls, body


async def test_probe_reports_feed_vs_group_counts(one_fetch):
    from pages.sources import probe_feed
    calls, body = one_fetch
    text, warn = await probe_feed("nyaa", "someone", "喵萌", "简日")
    assert not warn and "feed 里 3 条能收，这个组收 1 条" in text and "简日双语" in text, text
    assert calls == ["https://nyaa.si/?page=rss&u=someone&c=1_0"], f"同一个 feed 该只请求一次：{calls}"
    text, warn = await probe_feed("nyaa", "someone", "喵盟", "")          # 白名单写错一个字
    assert warn and "有 3 条能收，但没有一条通过" in text, text
    text, warn = await probe_feed("nyaa", "someone", "", "")
    assert not warn and "feed 里 3 条能收，这个组收 3 条" in text, text
    text, warn = await probe_feed("nyaa", "", "", "")
    assert warn and text == "先填 feed", text
    body["v"] = _EMPTY_FEED
    text, warn = await probe_feed("nyaa", "someone", "", "")
    assert warn and "没有能收的条目" in text, text


async def test_probe_redacts_the_feed_url_on_failure(monkeypatch):
    from services import fetch
    from pages.sources import probe_feed

    async def boom(client, url, **kw):
        raise RuntimeError(f"403 for url {url}")
    monkeypatch.setattr(fetch, "get_bytes", boom)
    text, warn = await probe_feed("mikan", "https://mikanani.me/RSS/MyBangumi?token=SECRET123", "", "")
    assert warn and text.startswith("没抓到：RuntimeError") and "SECRET123" not in text, text


def test_probe_builds_the_source_the_way_the_worker_does(clean_tables):
    """『试抓』与采集轮必须按同一写法把源组字段变成源对象，否则试出来的不是采集轮会收的。"""
    from core import worker as W
    from pages import sources as P
    keep = [g.id for g in A.list_source_groups()]
    assert A.add_source_group("u39", "nyaa", "someone", "auto", 99999,
                              subgroups=" 喵萌 , LoliHouse,", title_filter="简日, ,1080p")
    try:
        src = next(x for x in W.build_sources() if x.name == "u39")
        assert (src.rss_url, src.subgroups, src.title_filter) == (
            P.nyaa_feed_url("someone"), P._csv(" 喵萌 , LoliHouse,"), P._csv("简日, ,1080p"))
    finally:
        for g in A.list_source_groups():
            if g.id not in keep:
                A.delete_source_group(g.id)


def _card_of(el):
    return next((p for p in el.ancestors() if isinstance(p, ui.card)), None)


@pytest.mark.nicegui_main_file(_MAIN)
async def test_probe_button_shows_the_result_without_saving(user: User, clean_tables, one_fetch):
    from core import health
    n_groups = len(A.list_source_groups())
    with clean_tables.get_session() as s:
        n_t = len(s.exec(select(AnimeTorrent)).all())
    seen = health.seen_snapshot()
    await user.open("/sources")
    card = _card_of(_button(user, "添加"))
    form = {i.props.get("label"): i for i in card.descendants() if isinstance(i, ui.input)}
    form["feed（用户名 或 完整 RSS URL）"].value = "someone"
    white = form["字幕组白名单（空=全部）"]
    white.value = "喵盟"
    btn = next(b for b in card.descendants() if isinstance(b, ui.button) and b.text == "试抓")
    _click(user, btn)
    await user.should_see("没有一条通过")
    white.value = "喵萌"
    _click(user, btn)
    await user.should_see("这个组收 2 条")
    assert len(A.list_source_groups()) == n_groups, "试抓把源组存进库了"
    with clean_tables.get_session() as s:
        assert len(s.exec(select(AnimeTorrent)).all()) == n_t, "试抓入库了种子"
    assert health.seen_snapshot() == seen, "试抓推进了 seen"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_source_filter_labels_fit_on_phones(user: User):
    """白名单 / 关键词 / feed 的长标签在手机上被截掉，『空=全部』正好看不到；说明挪到页头三段里。"""
    await user.open("/sources")
    labels = {i.props.get("label") for i in _live(user, ui.input)}
    assert {"字幕组白名单（空=全部）", "标题关键词（空=不限）", "feed（用户名 或 完整 RSS URL）"} <= labels, labels
    await user.should_see("优先级：数字越大越优先")
    await user.should_see("命中任意一个就算")


# ================================================================ U3-10 订阅源卡片未保存标记

@pytest.mark.nicegui_main_file(_MAIN)
async def test_source_card_dirty_marker(user: User, clean_tables):
    for g in A.list_source_groups():
        if g.name.startswith("u310"):
            A.delete_source_group(g.id)
    assert A.add_source_group("u310甲", "nyaa", "feedA", "auto", 10)
    assert A.add_source_group("u310乙", "nyaa", "feedB", "auto", 20)
    try:
        await user.open("/sources")
        name_a = next(i for i in _live(user, ui.input) if i.props.get("label") == "名字" and i.value == "u310甲")
        name_b = next(i for i in _live(user, ui.input) if i.props.get("label") == "名字" and i.value == "u310乙")
        card_a, card_b = _card_of(name_a), _card_of(name_b)

        def mark(c):
            return next(x for x in c.descendants() if isinstance(x, ui.label) and x.text == "有改动，还没保存")
        assert not mark(card_a).visible and not mark(card_b).visible
        next(x for x in card_a.descendants() if isinstance(x, ui.switch)).value = False
        assert mark(card_a).visible, "拨了启用开关没出标记"
        assert not mark(card_b).visible, "甲卡的改动把乙卡也标了"
        name_a.value = "  u310甲  "            # 保存时会被 strip 回写 → 回写不能把标记又点亮
        _click(user, next(b for b in card_a.descendants() if isinstance(b, ui.button) and b.text == "保存"))
        await user.should_see("已保存")
        assert not mark(card_a).visible, "保存成功后标记没收起"
        assert name_a.value == "u310甲"
        assert next(g for g in A.list_source_groups() if g.name == "u310甲").enabled is False
    finally:
        for g in A.list_source_groups():
            if g.name.startswith("u310"):
                A.delete_source_group(g.id)


# ================================================================ U3-11 巡检区小节

def _settings_sections():
    """settings() 里每个 `_section(title, help)` 调用：[(行号, 标题, help 的常量文本)]，按行号排。"""
    tree = ast.parse((_ROOT / "pages" / "settings.py").read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "settings")

    def _text(node):
        if node is None:
            return ""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(v.value for v in node.values if isinstance(v, ast.Constant))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return _text(node.left) + _text(node.right)
        return ""
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_section":
            out.append((n.lineno, _text(n.args[0]), _text(n.args[1] if len(n.args) > 1 else None)))
    return fn, sorted(out)


def _key_line(fn, key):
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_num" \
                and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == key:
            return n.lineno
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value == key
                for t in n.targets):
            return n.lineno
    raise AssertionError(f"settings() 里找不到 {key} 的控件")


_SWEEP_HELP = {"ANIME_FINISH_UNSUB": "下齐后停止", "ANIME_IDLE_DAYS": "断更提醒",
               "ANIME_IDLE_WINDOW_X": "断更提醒窗口", "ANIME_GAP_SLACK_WEEKS": "余量",
               "ANIME_GAP_MAX_EPISODES": "长番上限", "ANIME_GAP_AUTOFILL_DAYS": "缺集自动补",
               "ANIME_LOCK_LAG_DAYS": "锁定源落后", "ANIME_LOCK_SYNC_DAYS": "同步窗口",
               "ANIME_LOCK_FALLBACK_DAYS": "锁定源停更兜底", "ANIME_RERELEASE_WEEKS": "同号新集",
               "ANIME_IGNORED_AIRING_DAYS": "超期忽略·仍在更新"}


def test_each_sweep_threshold_is_explained_in_its_own_section():
    """每个巡检阈值：它前面【最近的那个】_section 的帮助里有它的说明。同时钉住"有说明"与"说明跟控件在同一小节"。"""
    fn, secs = _settings_sections()
    bad = []
    for key, word in _SWEEP_HELP.items():
        line = _key_line(fn, key)
        near = [s for s in secs if s[0] < line][-1]
        if word not in near[2]:
            bad.append((key, near[1]))
    assert not bad, f"这些阈值所在小节的帮助里没有它的说明：{bad}"


def test_settings_help_bubbles_stay_readable():
    """一个帮助气泡 26rem 宽、12px 字：上千字要在气泡里内部滚动，找某一项的说明得通读。"""
    _, secs = _settings_sections()
    assert len(secs) >= 15, f"只扫到 {len(secs)} 个 _section，AST 写法可能过期了"
    long = [(t, len(h)) for _, t, h in secs if len(h) > 700]
    assert not long, f"帮助气泡太长：{long}"


# ================================================================ U3-12 手动下载

@pytest.mark.nicegui_main_file(_MAIN)
@pytest.mark.parametrize("ok", [True, False])
async def test_manual_download_shows_loading_and_clears_on_success(user: User, cfg, monkeypatch, ok):
    from core import manual as M
    cfg(QB_ENABLED=True)                       # 按钮的 enabled 是建页时定的
    await user.open("/manual")
    btn = next(b for b in _live(user, ui.button) if b.text == "下载")
    tin = _input(user, "magnet: / http(s) .torrent 链接")
    tin.value = "magnet:?xt=urn:btih:" + "a" * 40
    torrent = b"d4:infod4:name1:xee"
    await _live(user, ui.upload)[0].handle_uploads(
        [SmallFileUpload(name="a.torrent", content_type="application/x-bittorrent", _data=torrent)])
    await user.should_see("a.torrent")
    seen, datas = [], []

    async def fake(link, data, path):
        seen.append(bool(btn._props.get("loading")))
        datas.append(data)
        return {"ok": True, "save_path": "/x"} if ok else {"ok": False, "error": "x"}
    monkeypatch.setattr(M, "add_manual", fake)
    _click(user, btn)
    await user.should_see("已交给 qB" if ok else "下载失败：x")
    assert seen == [True], f"下载跑着的时候按钮没在 loading：{seen}"
    assert await _until(lambda: not btn._props.get("loading")), "跑完没摘掉 loading"
    if ok:
        assert tin.value == "", "成功后链接框没清空：接着贴下一条会把上一个来源再发一遍"
    else:
        assert tin.value.startswith("magnet:"), "失败时把链接清掉了，没法改了重试"
    # 再点一次：上传的文件优先于链接，成功后不清的话发出去的仍是上一个文件
    _click(user, btn)
    assert await _until(lambda: len(datas) == 2 and not btn._props.get("loading"))
    assert datas == ([torrent, None] if ok else [torrent, torrent]), datas


# ================================================================ U4-7 剧场版卡片按钮行

def test_movie_card_button_row_may_wrap():
    """『挑版本』『忽略』那一行不许 no-wrap（含带断点前缀的 flex-nowrap）：卡内不够宽时只会各自折字。"""
    tree = ast.parse((_ROOT / "pages" / "movies.py").read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_movie_card")
    rows = []
    for w in ast.walk(fn):
        if not isinstance(w, ast.With):
            continue
        has_btn = any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "button"
                      and c.args and isinstance(c.args[0], ast.Constant) and c.args[0].value == "挑版本"
                      for b in w.body for c in ast.walk(b))
        if not has_btn:
            continue
        for item in w.items:
            e = item.context_expr
            if isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute) and e.func.attr == "classes":
                rows.append(e.args[0].value)
    assert rows, "找不到包着『挑版本』的那一行（按钮改名了？守卫要跟着改）"
    for cls in rows:
        words = cls.split()
        assert "no-wrap" not in words and not any(w.endswith("flex-nowrap") for w in words), cls


# ================================================================ DOC-10 / DOC-11 文案

def _ui_strings(path: Path):
    """一个模块里所有面向用户的字符串字面量：跳过 docstring 与 log.* 的实参。"""
    tree = ast.parse(path.read_text())
    skip = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body \
                and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant):
            skip.add(id(n.body[0].value))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and isinstance(n.func.value, ast.Name) and n.func.value.id in ("log", "logger", "logging"):
            skip |= {id(a) for a in ast.walk(n) if a is not n}
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip]


_CJK = re.compile(r"[一-鿿]")
# 登记过的：这几处是在教人去改 .env / 看恢复步骤 / 指认一个目录项，键名本身就是要告诉人的东西
_KEY_OK = {("pages/settings.py", "DB_BACKEND"), ("pages/settings.py", "WEB_HOST"), ("core/ipfilter.py", "DOWN_PATH")}


def test_ui_text_does_not_name_config_keys():
    """横幅 / toast 让人去设置页开 QB_ENABLED——设置页上搜不到这个名字，开关叫『发送种子到 qB』。"""
    keys = set(config._SPEC) | {"WEB_HOST", "WEB_PORT", "DB_PATH"}
    pat = re.compile(r"(?<![A-Z0-9_])(" + "|".join(sorted(map(re.escape, keys), key=len, reverse=True))
                     + r")(?![A-Z0-9_])")
    hits, scanned = [], 0
    for d in ("pages", "core", "services"):
        for p in sorted((_ROOT / d).glob("*.py")):
            rel = p.relative_to(_ROOT).as_posix()
            for line, s in _ui_strings(p):
                if not _CJK.search(s):
                    continue
                scanned += 1
                for k in set(pat.findall(s)):
                    if (rel, k) not in _KEY_OK:
                        hits.append(f"{rel}:{line} {k} {s[:40]!r}")
    assert scanned > 1000, f"只扫到 {scanned} 条中文字符串，扫描可能失效了"
    assert not hits, "界面文案里出现了配置键名：\n" + "\n".join(hits)


def test_retired_terms_stay_retired():
    """三组旧叫法：review 策略叫『人工审核』不叫『待确认源』；人工忽略叫『已忽略』不叫『人工拒绝』；页名『订阅源』。"""
    from core import alerts
    assert alerts._BITS_CN["11"] == "已忽略"
    bad = []
    for d in ("pages", "core", "services"):
        for p in sorted((_ROOT / d).glob("*.py")):
            for line, s in _ui_strings(p):
                for w in ("待确认源", "人工拒绝", "源管理"):
                    if w in s:
                        bad.append(f"{p.relative_to(_ROOT).as_posix()}:{line} {w}")
    assert not bad, bad


# 剧场版页里『种子』只许出现在指 qB / RSS 实体的这些短语里
_SEED_OK = ("发送种子到 qB", "在 qB 里重新添加那些种子", "qB 里已没有这条种子", "种子不可用", "重拉种子", "同种子")


def test_movie_page_says_version_and_downloadable_everywhere():
    """GLOSSARY §一：剧场版侧叫『版本』『可下载』。链式 .tooltip()、confirm() 的参数、拼出来的句子都算。"""
    bad = []
    for line, s in _ui_strings(_ROOT / "pages" / "movies.py"):
        if re.search(r"可下(?!载)", s) and s not in ("待下",):
            bad.append(f"{line} 可下 {s[:40]!r}")
        rest = s
        for ok in _SEED_OK:
            rest = rest.replace(ok, "")
        if "种子" in rest:
            bad.append(f"{line} 种子 {s[:40]!r}")
    assert not bad, "\n".join(bad)


def test_settings_hints_name_the_button_not_its_place():
    """通知 URL 与备份空状态两句直接点按钮名、不写方位词（布局一变方位词就错），且点的名字确有其按钮。"""
    src = (_ROOT / "pages" / "settings.py").read_text()
    texts = "".join(v for _, v in _ui_strings(_ROOT / "pages" / "settings.py"))
    assert "填完点『发送测试通知』试一条。" in texts and 'ui.button("发送测试通知"' in src
    assert "还没有备份。点『立刻备份』，或等自动备份到点。" in texts and 'ui.button("立刻备份"' in src
