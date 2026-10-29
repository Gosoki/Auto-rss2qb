"""(r39 U7) 改下载目录规则后的『目录分裂』提示。

改 DOWN_PATH / 子目录 / 季度模板 / Season 子目录只影响之后的新集，已下的文件留在原目录——同一部番分在两个文件夹，
而保存时只有一句绿色『已保存』。现在只读、只提示（不改任何行）：

· 只数【这次改动造成的】：改之前在位、改之后不在位。改之前就不在位的（#96 那种）不算，『改回原值即可』对它不成立；
· 判据走 engine.same_dir：生产库 DOWN_PATH 是 'E:\\Anime\\AutoRSS'，改写成 'E:/Anime/AutoRSS' 或全小写物理目录没变，
  逐字比较会报『113 部 988 集还在原目录』；
· DOWN_PATH 两侧都管：剧场版那一侧一起报；
· 算不出来（快照 None / 出错）就不弹：宁可漏报。
"""
import itertools
from datetime import datetime

import pytest
from nicegui import ui
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction
from nicegui.testing.user_notify import UserNotify

import config
import db
from core import anime as A
from core import movies as M
from db.models import Anime, AnimeTorrent, Movie, MovieTorrent

db.init_db()
config.load_from_db()
db.apply_configured_backend()

_MAIN = "tests/render_main.py"
_BASE = dict(QB_ENABLED=False, DOWN_PATH="/data", ANIME_DOWN_PATH="", QUARTER_FMT="{yy}{q}",
             ANIME_SEASON_SUBFOLDER=False, MOVIE_DOWN_PATH="", MOVIE_QUARTER_FMT="{yyyy}")
WIN = "E:\\Anime\\AutoRSS"
HERE = "<在位>"            # 行的 save_path 取改之前那部的归档目录
_hash = (f"{i:040x}" for i in itertools.count(1))


def _anime(s, rows, confirmed=True):
    """rows: [(状态, save_path, 已归档[, 集号])]，集号缺省 1。save_path 为 HERE 时填该番当前（改之前）的归档目录。"""
    a = Anime(title="番", display_name="番", quarter="26A", confirmed=confirmed)
    s.add(a); s.commit(); s.refresh(a)
    here = A.anime_save_path(a.id)
    for st, sp, archived, *ep in rows:
        s.add(AnimeTorrent(anime_id=a.id, info_hash=next(_hash), raw_title="[组] 番", episode=(ep or [1])[0],
                           status=st, save_path=here if sp == HERE else sp,
                           archived_at=datetime.now() if archived else None))
    s.commit()
    return a.id


def _movie(s, rows):
    m = Movie(title="片", jp_name="片", quarter="26A")
    s.add(m); s.commit(); s.refresh(m)
    here = M.movie_save_path(m.id)
    for st, sp, archived in rows:
        s.add(MovieTorrent(movie_id=m.id, info_hash=next(_hash), raw_title="[组] 片", status=st,
                           save_path=here if sp == HERE else sp,
                           archived_at=datetime.now() if archived else None))
    s.commit()
    return m.id


_IN = [("sent", HERE, False)]
_NEW_FMT = {"QUARTER_FMT": "{yyyy}-{q}"}


@pytest.mark.parametrize("before,after,rows,confirmed,want", [
    pytest.param({}, _NEW_FMT, _IN, True, (1, 1, 1), id="在位-改季度模板-算"),
    pytest.param({}, {"ANIME_DOWN_PATH": "番剧"}, _IN, True, (1, 1, 1), id="在位-改番剧子目录-算"),
    pytest.param({}, {"ANIME_SEASON_SUBFOLDER": True}, _IN, True, (1, 1, 1), id="在位-开Season子目录-算"),
    # (r39 F36) 文案写『M 集』：同一集两个版本只算一集；不同集各算；特别篇/未知集号不标识哪一集，逐条计
    pytest.param({}, _NEW_FMT, _IN * 2, True, (1, 1, 1), id="同集两版本-算一集"),
    pytest.param({}, _NEW_FMT, [("sent", HERE, False, 1), ("sent", HERE, False, 2)], True, (1, 2, 1),
                 id="不同集-各算"),
    pytest.param({}, _NEW_FMT, [("sent", HERE, False, -1), ("sent", HERE, False, -1),
                                ("sent", HERE, False, -2), ("sent", HERE, False, -2)], True, (1, 4, 1),
                 id="特别篇未知集-逐条计"),
    pytest.param({}, _NEW_FMT, _IN, False, (1, 1, 0), id="没在追-在追不算"),
    pytest.param({}, _NEW_FMT, [("sent", "/data/25D/旧名", False)], True, (0, 0, 0), id="改之前就不在位-不算"),
    pytest.param({}, _NEW_FMT, [("sent", HERE, True)], True, (0, 0, 0), id="已归档-不算"),
    pytest.param({}, _NEW_FMT, [("sent", "", False)], True, (0, 0, 0), id="save_path空-不算"),
    pytest.param({}, _NEW_FMT, [("error", HERE, False)], True, (0, 0, 0), id="不在HAVE-不算"),
    pytest.param({}, {"DOWN_PATH": "", "ANIME_DOWN_PATH": ""}, _IN, True, (0, 0, 0), id="改完算不出目录-跳过"),
    pytest.param({"DOWN_PATH": "", "ANIME_DOWN_PATH": ""}, {}, [("sent", "/old/26A/番", False)], True,
                 (0, 0, 0), id="改之前算不出目录-跳过"),
    pytest.param({"DOWN_PATH": WIN}, {"DOWN_PATH": "E:/Anime/AutoRSS"}, _IN, True, (0, 0, 0), id="Windows只改分隔符-不算"),
    pytest.param({"DOWN_PATH": WIN}, {"DOWN_PATH": "e:\\anime\\autorss"}, _IN, True, (0, 0, 0), id="Windows只改大小写-不算"),
    pytest.param({"DOWN_PATH": WIN}, {"DOWN_PATH": WIN + "\\"}, _IN, True, (0, 0, 0), id="Windows末尾多分隔符-不算"),
    pytest.param({"DOWN_PATH": WIN}, {"DOWN_PATH": "E:\\Anime\\New"}, _IN, True, (1, 1, 1), id="Windows真换目录-算"),
])
def test_anime_split_count(clean_tables, cfg, before, after, rows, confirmed, want):
    cfg(**{**_BASE, **before})
    with clean_tables.get_session() as s:
        _anime(s, rows, confirmed)
    snap = A.anime_path_snapshot()
    cfg(**after)
    assert A.anime_split_count(snap) == want


def test_anime_split_count_skips_rows_newer_than_snapshot(clean_tables, cfg):
    """快照之后才有文件的番不在 before 里：不知道它改之前该在哪，跳过。"""
    cfg(**_BASE)
    snap = A.anime_path_snapshot()
    with clean_tables.get_session() as s:
        _anime(s, _IN)
    cfg(**_NEW_FMT)
    assert snap == {} and A.anime_split_count(snap) == (0, 0, 0)


@pytest.mark.parametrize("before,after,rows,want", [
    pytest.param({}, {"MOVIE_QUARTER_FMT": "{yyyy}年"}, _IN, (1, 1), id="改剧场版模板-算"),
    pytest.param({}, {"MOVIE_DOWN_PATH": "剧场版"}, _IN, (1, 1), id="改剧场版子目录-算"),
    pytest.param({}, {"DOWN_PATH": "/media"}, _IN, (1, 1), id="改工作目录-剧场版也算"),
    pytest.param({}, _NEW_FMT, _IN, (0, 0), id="番剧模板不影响剧场版"),
    pytest.param({}, {"MOVIE_QUARTER_FMT": "{yyyy}年"}, [("sent", "/data/2025/片", False)], (0, 0), id="改之前就不在位-不算"),
    pytest.param({}, {"MOVIE_QUARTER_FMT": "{yyyy}年"}, [("sent", HERE, True)], (0, 0), id="已归档-不算"),
    pytest.param({"DOWN_PATH": WIN}, {"DOWN_PATH": "E:/anime/autorss"}, _IN, (0, 0), id="Windows等价写法-不算"),
])
def test_movie_split_count(clean_tables, cfg, before, after, rows, want):
    cfg(**{**_BASE, **before})
    with clean_tables.get_session() as s:
        _movie(s, rows)
    snap = M.movie_path_snapshot()
    cfg(**after)
    assert M.movie_split_count(snap) == want


# ---------------------------------------------------------------- 设置页

def _live(user, kind):
    return [e for e in user.find(kind).elements if not e.is_deleted]


def _input(user, label, value=None):
    got = [i for i in _live(user, ui.input)
           if i.props.get("label") == label and (value is None or i.value == value)]
    assert len(got) == 1, f"按 label={label!r} value={value!r} 找到 {len(got)} 个输入框"
    return got[0]


@pytest.fixture
def page(clean_tables, cfg, monkeypatch):
    """设置页保存的公共准备：基准配置 + 记下每条 notify 的参数 + 拦掉 .env 写入 + 记快照被调了几次。"""
    cfg(**_BASE)
    monkeypatch.setattr(config, "update_env", lambda updates: None)   # 别碰仓库里真的 .env
    notes = []

    def _record(self, message, **kw):
        self.messages.append(message)
        notes.append((message, kw))
    monkeypatch.setattr(UserNotify, "__call__", _record)
    calls = {"anime": 0, "movie": 0}
    real_a, real_m = A.anime_path_snapshot, M.movie_path_snapshot

    def _a():
        calls["anime"] += 1
        return real_a()

    def _m():
        calls["movie"] += 1
        return real_m()
    monkeypatch.setattr(A, "anime_path_snapshot", _a)
    monkeypatch.setattr(M, "movie_path_snapshot", _m)
    return {"db": clean_tables, "notes": notes, "calls": calls}


async def _save(user):
    btn = next(b for b in _live(user, ui.button) if b.text == "保存")
    UserInteraction(user, {btn}, None).click()
    await user.should_see("已保存，即时生效")


def _split_notes(notes):
    return [(m, kw) for m, kw in notes if m.startswith("下载目录规则改了")]


@pytest.mark.nicegui_main_file(_MAIN)
async def test_changing_quarter_fmt_warns(user: User, page):
    with page["db"].get_session() as s:
        _anime(s, _IN)
    await user.open("/settings")
    _input(user, "命名模板", "{yy}{q}").value = "{yyyy}-{q}"
    await _save(user)
    got = _split_notes(page["notes"])
    assert len(got) == 1, page["notes"]
    msg, kw = got[0]
    assert "1 部番 1 集（其中在追 1 部）" in msg and "剧场版" not in msg
    assert "改回原值" in msg and "编辑季度" in msg
    assert kw.get("type") == "warning" and kw.get("timeout") == 0 and kw.get("close_button") == "知道了", kw
    assert page["calls"] == {"anime": 1, "movie": 0}, "只改番剧模板，不该去拍剧场版的快照"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_changing_site_name_does_not_warn_nor_snapshot(user: User, page):
    with page["db"].get_session() as s:
        _anime(s, _IN)
    await user.open("/settings")
    _input(user, "站点名").value = "r39站"
    await _save(user)
    assert not _split_notes(page["notes"]), page["notes"]
    assert page["calls"] == {"anime": 0, "movie": 0}, "路径键没变，不该跑快照（生产库约 73ms）"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_equivalent_windows_down_path_does_not_warn(user: User, page, cfg):
    cfg(DOWN_PATH=WIN)
    with page["db"].get_session() as s:
        _anime(s, _IN)
        _movie(s, _IN)
    await user.open("/settings")
    _input(user, "工作目录（下载根）", WIN).value = "E:/Anime/AutoRSS"
    await _save(user)
    assert config.DOWN_PATH == "E:/Anime/AutoRSS", "设置本身要照常存进去"
    assert page["calls"] == {"anime": 1, "movie": 1}, "DOWN_PATH 两侧都管：两侧都要拍快照，否则这条用例是空的"
    assert not _split_notes(page["notes"]), page["notes"]


@pytest.mark.nicegui_main_file(_MAIN)
async def test_movie_only_split_warns_without_anime_remedy(user: User, page):
    """只有剧场版分裂：报剧场版那一半；『编辑季度』是番剧的补救办法，不写进去。"""
    with page["db"].get_session() as s:
        _movie(s, _IN)
    await user.open("/settings")
    _input(user, "命名模板", "{yyyy}").value = "{yyyy}年"
    await _save(user)
    got = _split_notes(page["notes"])
    assert len(got) == 1, page["notes"]
    assert "剧场版 1 部 1 个版本" in got[0][0] and "部番" not in got[0][0] and "编辑季度" not in got[0][0]
    assert "新集" not in got[0][0], "剧场版没有『新集』的说法(r39 F36)"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_snapshot_failure_saves_without_warning(user: User, page, monkeypatch, caplog):
    """快照出错：只记日志、不弹（宁可漏报），保存照常。"""
    with page["db"].get_session() as s:
        _anime(s, _IN)

    def boom():
        raise RuntimeError("库忙")
    monkeypatch.setattr(A, "anime_path_snapshot", boom)
    await user.open("/settings")
    _input(user, "命名模板", "{yy}{q}").value = "{yyyy}-{q}"
    await _save(user)
    assert config.QUARTER_FMT == "{yyyy}-{q}"
    assert not _split_notes(page["notes"]), page["notes"]
    assert any("目录分裂快照失败" in r.getMessage() for r in caplog.records), "出错要留一条日志，否则无从查起"
