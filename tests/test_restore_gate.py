"""(E-52，2026-09-02 拍板) 『恢复订阅』落下去之前，先看这部番在剧场版表里有没有同体。

同一个 bgm subject 在番剧表与剧场版表各有一条时（真库里正好 2 部），恢复订阅会立刻补下，
两边各自交付同一个 info_hash：qB 只收一次，两条记录都显示『已交付』，文件只落在其中一个目录。
仪表盘横幅（R30）只是提醒；闸在按钮上。
"""
import ast
from pathlib import Path

import pytest

from db.models import Anime, AnimeTorrent, Movie, MovieTorrent


def _twin_pair(db, shared: bool = True):
    with db.get_session() as s:
        a = Anime(title="剧场版 X", display_name="剧场版 X", season=1, quarter="26A",
                  confirmed=False, rejected=True, bangumi_id=583746)
        m = Movie(title="X 剧场版", display_name="X 剧场版", quarter="2026", bangumi_id=583746)
        s.add(a); s.add(m); s.commit(); s.refresh(a); s.refresh(m)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="7" * 40, raw_title="[组] X 剧场版",
                           episode=-1, status="skipped"))
        s.add(MovieTorrent(movie_id=m.id, info_hash=("7" if shared else "8") * 40,
                           raw_title="[组] X 剧场版", status="pending"))
        s.commit()
        return a.id, m.id


def test_movie_twin_finds_the_same_subject_in_the_other_table(clean_tables):
    from core import anime as A

    aid, mid = _twin_pair(clean_tables)
    t = A.movie_twin(aid)
    assert t == {"m": mid, "m_name": "X 剧场版", "shared": 1}, t


def test_movie_twin_is_none_without_a_movie_row(clean_tables):
    from core import anime as A

    with clean_tables.get_session() as s:
        a = Anime(title="普通番", season=1, quarter="26A", rejected=True, bangumi_id=1)
        s.add(a); s.commit(); s.refresh(a)
        aid = a.id
    assert A.movie_twin(aid) is None


async def test_restore_asks_first_when_a_twin_exists_and_cancel_changes_nothing(clean_tables, monkeypatch):
    from core import anime as A
    from pages import anime_detail as D

    aid, _ = _twin_pair(clean_tables)
    asked = []

    async def fake_confirm(title, note="", **kw):
        asked.append((title, note))
        return False                      # 用户点了取消

    monkeypatch.setattr(D, "confirm", fake_confirm)
    assert await D.restore_anime_gated(aid) is None
    assert asked and "X 剧场版" in asked[0][0] and "1 条种子是同一个文件" in asked[0][1], asked
    with clean_tables.get_session() as s:
        a = s.get(Anime, aid)
        assert a.rejected and not a.confirmed, "取消之后番不该被恢复"
    assert A.movie_twin(aid) is not None


async def test_restore_proceeds_when_the_user_insists(clean_tables, monkeypatch):
    from pages import anime_detail as D

    aid, _ = _twin_pair(clean_tables)

    async def yes(*a, **k):
        return True

    async def no_download(anime_id):
        return 0
    monkeypatch.setattr(D, "confirm", yes)
    monkeypatch.setattr(D.anime, "download_pending_for_anime", no_download)
    assert await D.restore_anime_gated(aid) == 0
    with clean_tables.get_session() as s:
        a = s.get(Anime, aid)
        assert not a.rejected and a.confirmed


async def test_restore_does_not_ask_without_a_twin(clean_tables, monkeypatch):
    from pages import anime_detail as D

    with clean_tables.get_session() as s:
        a = Anime(title="普通番", season=1, quarter="26A", rejected=True, bangumi_id=1)
        s.add(a); s.commit(); s.refresh(a)
        aid = a.id

    async def boom(*a, **k):
        raise AssertionError("没有同体不该弹确认框")

    async def no_download(anime_id):
        return 0
    monkeypatch.setattr(D, "confirm", boom)
    monkeypatch.setattr(D.anime, "download_pending_for_anime", no_download)
    assert await D.restore_anime_gated(aid) == 0


_ENTRIES = ("confirm_anime", "restore_anime", "resubscribe")   # 让番【进入订阅并补下】的三个 core 入口


def test_every_subscribe_entry_in_pages_goes_through_the_gate():
    """广度守卫：pages/ 里凡是调 confirm_anime / restore_anime / resubscribe 的，只允许是 subscribe_gated 自己。

    『恢复订阅』两个入口、『确认下载』两个入口、『继续订阅』一个 —— 五处按钮做的是同一件事
    （进入订阅 + 立刻补下）。R34 对抗审计证伪了"只有『恢复订阅』一个触发口"：改开始日会把同体番打回
    待确认，列表上就是『确认下载』。只给一处加闸就是第①号缺陷形状。
    (R34 对抗审计) 同时认 `anime.x(` 的 Attribute 形式、`from core.anime import x` 之后的 Name 形式、
    以及 import 本身；反向计数按 AST 数 await 节点，不数源码字符串（注释就能满足字符串计数）。
    """
    root = Path(__file__).resolve().parent.parent
    bad, gated_calls = [], 0
    for f in root.glob("pages/*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module == "core.anime":
                for a in n.names:
                    if a.name in _ENTRIES:
                        bad.append(f"{f.name}:{n.lineno} 直接 import 了 {a.name}，绕开了 subscribe_gated")

        def visit(node, owner):
            nonlocal gated_calls
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(ch, ch.name)
                    continue
                if isinstance(ch, ast.Call):
                    fn = ch.func
                    nm = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
                    if nm in _ENTRIES and owner != "subscribe_gated":
                        bad.append(f"{f.name}:{ch.lineno} 在 {owner}() 里直接调了 {nm}，绕过了 E-52 的闸")
                    if nm in ("subscribe_gated", "restore_anime_gated") and owner not in (
                            "subscribe_gated", "restore_anime_gated"):
                        gated_calls += 1
                visit(ch, owner)
        visit(tree, "<module>")
    assert not bad, "\n  ".join(bad)
    # 反向：五个按钮入口都还在、都调 gated（详情页：确认/恢复/继续；列表页：确认/恢复）
    assert gated_calls == 5, f"进入订阅的按钮入口应有 5 处走闸，现在 {gated_calls} 处"


def test_movie_twin_is_found_by_shared_hash_even_when_bgm_differs(clean_tables):
    """(R34 对抗审计) 番剧侧绑错到 TV 条目（bgm 不同）、或番剧没有 bgm 时，共用的种子才是最硬的同体证据。"""
    from core import anime as A

    with clean_tables.get_session() as s:
        a = Anime(title="剧场版 X", season=1, quarter="26A", rejected=True, bangumi_id=111)   # 绑错的 TV 条目
        m = Movie(title="X 剧场版", display_name="X 剧场版", quarter="2026", bangumi_id=999)
        b = Anime(title="没 bgm 的", season=1, quarter="26A", rejected=True, bangumi_id=None)
        s.add(a); s.add(m); s.add(b); s.commit(); s.refresh(a); s.refresh(m); s.refresh(b)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="7" * 40, raw_title="x", episode=-1, status="skipped"))
        s.add(AnimeTorrent(anime_id=b.id, info_hash="8" * 40, raw_title="y", episode=-1, status="skipped"))
        s.add(MovieTorrent(movie_id=m.id, info_hash="7" * 40, raw_title="x", status="pending"))
        s.add(MovieTorrent(movie_id=m.id, info_hash="8" * 40, raw_title="y", status="pending"))
        s.commit()
        aid, bid, mid = a.id, b.id, m.id
    assert A.movie_twin(aid) == {"m": mid, "m_name": "X 剧场版", "shared": 1}
    assert A.movie_twin(bid) == {"m": mid, "m_name": "X 剧场版", "shared": 1}


async def test_confirm_download_is_gated_too(clean_tables, monkeypatch):
    """『确认下载』走同一道闸：有同体先问；取消则番仍是待确认、一集都不下。"""
    from pages import anime_detail as D

    aid, _ = _twin_pair(clean_tables)
    with clean_tables.get_session() as s:
        a = s.get(Anime, aid)
        a.rejected, a.confirmed = False, False       # 改开始日之后的形状：待确认
        s.add(a); s.commit()
    asked = []

    async def fake_confirm(title, note="", **kw):
        asked.append(note)
        return False
    monkeypatch.setattr(D, "confirm", fake_confirm)
    assert await D.subscribe_gated(aid, "confirm") is None
    assert asked and "『确认下载』会立刻补下" in asked[0], asked
    with clean_tables.get_session() as s:
        a = s.get(Anime, aid)
        assert not a.confirmed
