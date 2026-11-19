"""(r41) 清理下错文件的界面：『只下 X』框里的勾选项、详情页横幅『看清单并清理』、单条 🗑 的两套编号说法、
锁源时『不属于本季』的顺带下载、仪表盘 mva 的两个忽略按钮。qB 一律 mock。"""
import asyncio
from datetime import timedelta

import pytest
from nicegui import ui
from nicegui.testing.user_interaction import UserInteraction

import config
import db

db.init_db()
config.load_from_db()
db.apply_configured_backend()

from tests.test_r41_cleanup import FakeQB, W0, _get, _p27, _row, _weekly, _anime  # noqa: E402

_DETAIL = "tests/render_detail_main.py"
_MAIN = "tests/render_main.py"


def _live(user, kind):
    try:
        return [e for e in user.find(kind).elements if not e.is_deleted]
    except AssertionError:          # 页面上一个都没有（对话框还没弹出来）
        return []


def _labels(user) -> str:
    return "\n".join(str(e.text or "") for e in _live(user, ui.label))


def _click(user, el):
    UserInteraction(user, {el}, None).click()


async def _until(pred, what, tries=100):
    for _ in range(tries):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(what)


def _btn(user, text, starts=False):
    return [b for b in _live(user, ui.button) if (b.text or "").startswith(text) if starts or b.text == text]


@pytest.fixture
def qb(monkeypatch):
    from core import engine
    f = FakeQB()
    monkeypatch.setattr(engine.qb, "torrents_info", f.torrents_info)
    monkeypatch.setattr(engine.qb, "delete", f.delete)
    return f


# ---------------------------------------------------------------- 『只下 X』框

@pytest.mark.parametrize("tick", [True, False], ids=["勾上", "不勾"])
@pytest.mark.nicegui_main_file(_DETAIL)
async def test_lock_dialog_optional_cleanup(user, clean_tables, qb, cfg, tick):
    cfg(QB_ENABLED=True)
    with clean_tables.get_session() as s:
        aid, ani, nix, _ = _p27(s)
    await user.open(f"/detail/{aid}")
    _click(user, _btn(user, "只下 ANi")[0])
    await _until(lambda: [c for c in _live(user, ui.checkbox) if (c.text or "").startswith("顺便删掉")], "没出勾选项")
    await _until(lambda: [c for c in _live(user, ui.checkbox) if "11.0GB" in (c.text or "")], "大小没填上")
    also = next(c for c in _live(user, ui.checkbox) if (c.text or "").startswith("顺便删掉"))
    assert also.text == "顺便删掉『Nix-Raws』已经下好的 11 份重复（11.0GB）" and also.value is False
    labels = " ".join(e.text or "" for e in _live(user, ui.label))
    assert "已经下了的文件这一步不动。下面可以顺便删掉『Nix-Raws』多出来的那几份——每一份都对得上『ANi』的同一集。" in labels
    assert "已经下了的文件不动、不删" not in labels
    if tick:
        also.value = True
        await _until(lambda: _btn(user, "只下 ANi 并删掉 11 份"), "主按钮没变成『只下 ANi 并删掉 11 份』")
        _click(user, _btn(user, "只下 ANi 并删掉 11 份")[0])
    else:
        _click(user, [b for b in _btn(user, "只下 ANi") if b.props.get("color") == "primary"][-1])
    await _until(lambda: _get(clean_tables, nix[1]).status == ("deleted" if tick else "sent")
                 and getattr(__import__("core.anime", fromlist=["x"]).get_anime(aid), "pref_source") == "ANi",
                 "锁定/删除没按勾选执行")
    await asyncio.sleep(0.1)
    assert {_get(clean_tables, t).status for t in nix.values()} == ({"deleted"} if tick else {"sent"})
    assert {_get(clean_tables, t).status for t in ani.values()} == {"sent"}


# ---------------------------------------------------------------- 详情页横幅与清理框

@pytest.mark.nicegui_main_file(_DETAIL)
async def test_banner_b1_opens_cleanup_and_deletes(user, clean_tables, qb, cfg):
    cfg(QB_ENABLED=True)
    with clean_tables.get_session() as s:
        aid, ani, nix, _ = _p27(s, lock="ANi")
    await user.open(f"/detail/{aid}")
    await user.should_see("有 11 份是另一套编号的重复（『Nix-Raws』第 1–11 集 = 『ANi』第 13–23 集）。")
    _click(user, _btn(user, "看清单并清理")[0])
    await user.should_see("删掉 11 份重复文件？")
    await user.should_see("通过 qB 连同硬盘文件一起删，不能撤销。每一份右边写着留下的那份。")
    await user.should_see("删掉的是『Nix-Raws』的第 1–11 集，这些内容在『ANi』里是第 13–23 集，留着。"
                          "以后『Nix-Raws』这几个号不再自动下；想要回某一份，在那一行点『下载』。")
    await _until(lambda: [b for b in _btn(user, "删掉勾选的 11 份") if b.enabled], "探完 qB 主按钮没亮")
    _click(user, _btn(user, "删掉勾选的 11 份")[0])
    await _until(lambda: _get(clean_tables, nix[11]).status == "deleted", "没删")
    await _until(lambda: not [e for e in _live(user, ui.label) if "另一套编号的重复" in (e.text or "")], "横幅没消失")


@pytest.mark.nicegui_main_file(_DETAIL)
async def test_cleanup_tail_follows_ticked_rows(user, clean_tables, qb, cfg):
    """C3 的『删掉的是第 a–b 集』只算勾着的那几份：#27 第 1 集超窗不勾时，写的不能还是 1–11。"""
    cfg(QB_ENABLED=True)
    with clean_tables.get_session() as s:
        aid, ani, nix, _ = _p27(s, lock="ANi")
    await user.open(f"/detail/{aid}")
    _click(user, _btn(user, "看清单并清理")[0])
    await _until(lambda: [b for b in _btn(user, "删掉勾选的 11 份") if b.enabled], "探完 qB 主按钮没亮")
    lab = next(e for e in _live(user, ui.label) if e.text == "删：Nix-Raws 第 1 集")
    cb = next(c for c in next(lab.ancestors()).default_slot.children if isinstance(c, ui.checkbox))
    cb.value = False
    await user.should_see("删掉的是『Nix-Raws』的第 2–11 集，这些内容在『ANi』里是第 14–23 集，留着。", retries=20)
    _click(user, _btn(user, "删掉勾选的 10 份")[0])
    await _until(lambda: _get(clean_tables, nix[11]).status == "deleted", "没删")
    assert _get(clean_tables, nix[1]).status == "sent"


@pytest.mark.nicegui_main_file(_DETAIL)
async def test_banner_b2_foreign_all_unchecked(user, clean_tables, qb, cfg):
    cfg(QB_ENABLED=True)
    with clean_tables.get_session() as s:
        aid = _anime(s)
        old = _weekly(s, aid, "ANi", range(1, 4), W0 - timedelta(weeks=100), foreign_season=True)
    await user.open(f"/detail/{aid}")
    await user.should_see("有 3 份已标『不属于本季』的别季文件还在这部番的目录里，它们不影响本季。")
    _click(user, _btn(user, "看清单并清理")[0])
    await user.should_see("删掉 3 个别季文件？")
    await _until(lambda: _btn(user, "删掉勾选的 0 份"), "foreign 应当默认一份不勾")
    assert not _btn(user, "删掉勾选的 0 份")[0].enabled
    assert all(_get(clean_tables, t).status == "sent" for t in old.values())


@pytest.mark.nicegui_main_file(_DETAIL)
async def test_cleanup_dialog_qb_down_keeps_button_disabled(user, clean_tables, qb, cfg):
    cfg(QB_ENABLED=True)
    qb.down = True
    with clean_tables.get_session() as s:
        aid, *_ = _p27(s, lock="ANi")
    await user.open(f"/detail/{aid}")
    _click(user, _btn(user, "看清单并清理")[0])
    await user.should_see("qB 连不上，现在删不了")
    b = _btn(user, "删掉勾选的", starts=True)[0]
    assert not b.enabled


# ---------------------------------------------------------------- 单条 🗑

@pytest.mark.nicegui_main_file(_DETAIL)
async def test_single_delete_uses_alias_wording(user, clean_tables, qb, cfg, monkeypatch):
    from pages import anime_detail as D
    cfg(QB_ENABLED=True)
    with clean_tables.get_session() as s:
        aid, ani, nix, _ = _p27(s)
    asked = []

    # (r42 E-81) 这一集仅剩的一份走三选一框；两套集号时默认『只删这一份』，说法换成另一套编号里留着的那份
    async def pick_default(opts, label, raw, save_path):
        asked.append(opts)
        return opts["default"], None
    monkeypatch.setattr(D, "_ask_delete", pick_default)
    await user.open(f"/detail/{aid}")
    def _row_texts(b):   # 🗑 所在那一条种子行（最近的 ui.column）里的全部文字
        col = next(a for a in b.ancestors() if isinstance(a, ui.column))
        return {str(c.text) for c in col.descendants() if isinstance(c, ui.label)}
    first = next(b for b in _live(user, ui.button) if b.props.get("icon") == "delete_forever"
                 and {"第1集", "Nix-Raws"} <= _row_texts(b))
    _click(user, first)
    await _until(lambda: asked, "没弹确认框")
    assert asked[0]["default"] == "drop", asked[0]
    assert ("只删这一份：这一集另有『ANi』第 13 集那份（两套编号，内容相同），"
            "程序不再按「第 1 集」补下") == D._drop_text(asked[0])
    await user.should_see("已删除该集文件；这一集还有『ANi』第 13 集那份。", retries=20)
    t = _get(clean_tables, nix[1])
    assert t.status == "deleted" and t.ep_dropped, "标记照旧写 1（E-73 不变）"


# ---------------------------------------------------------------- 『不属于本季』锁源守卫

@pytest.mark.parametrize("tick", [True, False], ids=["勾上", "不勾"])
@pytest.mark.nicegui_main_file(_DETAIL)
async def test_mark_foreign_when_locked_offers_download(user, clean_tables, cfg, monkeypatch, tick):
    from core import anime as A
    cfg(QB_ENABLED=True)
    with clean_tables.get_session() as s:
        aid = _anime(s, total_episodes=13, pref_source="喵萌奶茶屋", season=2,
                     air_date=(W0 + timedelta(weeks=19)).strftime("%Y-%m-%d"))
        old = _row(s, aid, "喵萌奶茶屋", 6, W0, season=1)
        _weekly(s, aid, "喵萌奶茶屋", range(9, 12), W0 + timedelta(weeks=20))
        new = _row(s, aid, "绿茶字幕组", 6, W0 + timedelta(weeks=20, days=1), status="pending")
    got = []

    async def fake_dl(tid, force=False, **kw):
        got.append((tid, force))
        return True
    monkeypatch.setattr(A, "download_anime_torrent", fake_dl)
    await user.open(f"/detail/{aid}")
    _click(user, _btn(user, "不属于本季")[0])
    r2 = ("这部番锁定了『喵萌奶茶屋』，标完后第 6 集没有锁定组的版本，不会自动下。"
          "下面勾着的会在标完后立刻下载；不勾就要你以后自己点『下载』。")
    await _until(lambda: r2 in _labels(user), "确认框里没有 R2")
    cb = next(c for c in _live(user, ui.checkbox) if (c.text or "").startswith("第 6 集：绿茶字幕组"))
    assert cb.value is True
    dlg_btn = [b for b in _btn(user, "不属于本季") if b.props.get("color") == "primary"]
    if not tick:
        cb.value = False                 # 不勾就不下：只标记，那一集留给人以后自己点
    _click(user, dlg_btn[-1])
    if tick:
        await _until(lambda: got == [(new, True)], f"没顺带下载：{got}")
    else:
        await _until(lambda: _get(clean_tables, old).foreign_season, "没标上")
        await asyncio.sleep(0.2)
        assert got == [], f"没勾却下了：{got}"
    assert _get(clean_tables, old).foreign_season


# ---------------------------------------------------------------- 仪表盘 mva

def _mva(db, pend=2, sent=1):
    from db.models import Anime, AnimeTorrent, Movie, MovieTorrent
    with db.get_session() as s:
        a = Anime(title="鸣海", display_name="怪兽8号 鸣海的日常", season=1, quarter="26C",
                  confirmed=True, rejected=False, bangumi_id=555, total_episodes=4)
        m = Movie(title="鸣海的平日", display_name="怪兽8号 鸣海的平日", quarter="2026", bangumi_id=555)
        s.add(a); s.add(m); s.commit(); s.refresh(a); s.refresh(m)
        for i in range(pend + sent):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"a{i:039x}", raw_title=f"[ANi] 鸣海 - {i + 1}",
                               episode=float(i + 1), status="sent" if i < sent else "pending", source="ANi"))
        for i in range(2):
            s.add(MovieTorrent(movie_id=m.id, info_hash=f"b{i:039x}", raw_title=f"mv{i}", status="pending"))
        s.commit()
        return a.id, m.id


@pytest.mark.parametrize("keep", ["anime", "movie"])
@pytest.mark.nicegui_main_file(_MAIN)
async def test_mva_buttons_ignore_one_side_without_qb(user, clean_tables, cfg, monkeypatch, keep):
    from core import engine
    from db.models import Anime, AnimeTorrent, Movie, MovieTorrent
    from pages import anime as P
    from sqlmodel import select
    cfg(QB_ENABLED=False)
    aid, mid = _mva(clean_tables)

    async def boom(*a, **kw):
        raise AssertionError("mva 的忽略按钮不该碰 qB")
    monkeypatch.setattr(engine.qb, "torrents_info", boom)
    monkeypatch.setattr(engine.qb, "delete", boom)
    asked = []

    async def yes(title, note, **kw):
        asked.append((title, note))
        return True
    monkeypatch.setattr(P, "confirm", yes)
    await user.open("/?t=overview")
    await user.should_see("番剧表(#")
    lab = "留番剧，忽略剧场版" if keep == "anime" else "留剧场版，忽略番剧"
    _click(user, _btn(user, lab)[0])
    await _until(lambda: asked, "没弹确认框")
    if keep == "anime":
        assert asked[0] == ("忽略剧场版这条？",
                            "剧场版《怪兽8号 鸣海的平日》没下的版本不再下。番剧这边不动，已下的文件不动。")
    else:
        assert asked[0] == ("忽略番剧这条？",
                            "番剧『怪兽8号 鸣海的日常』没下的集不再下。已经下了的 1 个文件不动"
                            "（剧场版那边可能用的就是同一个文件）；确实要删，到『已忽略』里这部番的卡片上删。")

    def _state():
        with clean_tables.get_session() as s:
            return (s.get(Anime, aid).rejected, s.get(Movie, mid).rejected,
                    sorted(t.status for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid))),
                    sorted(t.status for t in s.exec(select(MovieTorrent).where(MovieTorrent.movie_id == mid))))
    if keep == "anime":
        await _until(lambda: _state() == (False, True, ["pending", "pending", "sent"], ["skipped", "skipped"]),
                     f"剧场版没被忽略：{_state()}")
    else:
        await _until(lambda: _state() == (True, False, ["sent", "skipped", "skipped"], ["pending", "pending"]),
                     f"番剧没被忽略：{_state()}")
    await _until(lambda: not _btn(user, lab), "处理完提示没收起来")


@pytest.mark.nicegui_main_file(_MAIN)
async def test_ack_buttons_have_tooltip(user, clean_tables, cfg):
    from nicegui.elements.tooltip import Tooltip
    cfg(QB_ENABLED=False)
    _mva(clean_tables)
    await user.open("/?t=overview")
    await user.should_see("番剧表(#")
    ack = _btn(user, "知道了")[0]
    tips = {t.props.get("target"): str(t.text) for t in _live(user, Tooltip)}
    assert tips.get(f"#c{ack.id}") == "先收起来；问题还在，处理完会自己消失", tips
