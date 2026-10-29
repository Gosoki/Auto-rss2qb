"""r39 搬迁与路径（Q2 / U7a / Q9）。

真实风险（生产副本 replay2.db 回放 anime#2）：『搬回来』入口里 old_path == new_path，
1 集在错误目录、qB 认识；另 10 集就在当前目录、qB 已不认识（remove-on-complete）。
修前 relocate 把这 10 集清成 pending，提示写『旧文件在 {当前归档目录} 需你手动清理』——
用户照做，删掉的就是当前目录里唯一的一份。
"""
import asyncio
from datetime import datetime

import pytest
from sqlmodel import select

from core import anime as A
from core import engine as ce
from core import movies as M
from db.models import Anime, AnimeTorrent, Movie, MovieTorrent

OLD = "/data/25D/番"


def _setup(cfg, down="/data"):
    cfg(QB_ENABLED=True, DOWN_PATH=down, ANIME_DOWN_PATH="", QUARTER_FMT="{yy}{q}",
        ANIME_SEASON_SUBFOLDER=False, MOVIE_DOWN_PATH="", MOVIE_QUARTER_FMT="{yyyy}")


def _anime(s, rows):
    """rows: [(hash 字母, 集号, 状态, save_path)]。返回 anime_id。"""
    a = Anime(title="番", display_name="番", quarter="26A", confirmed=True)
    s.add(a); s.commit(); s.refresh(a)
    for h, ep, st, sp in rows:
        s.add(AnimeTorrent(anime_id=a.id, info_hash=h * 40, raw_title=f"[组][{h}]", episode=ep,
                           status=st, qb_progress=1.0 if st == "sent" else 0.3, save_path=sp,
                           created_at=datetime.now()))
    s.commit()
    return a.id


def _qb(monkeypatch, known=(), code=200):
    """假 qB：known 里的 hash 它认识，其余不认识；set_location 记下收到的 hash。"""
    moved = []

    async def info(hashes):
        return {h: {"state": "stalledUP"} for h in hashes if h[0] in known}

    async def set_location(hashes, path):
        moved.append((sorted(h[0] for h in hashes), path))
        return code
    monkeypatch.setattr(ce.qb, "torrents_info", info)
    monkeypatch.setattr(ce.qb, "set_location", set_location)
    return moved


def _status(clean_tables, aid):
    with clean_tables.get_session() as s:
        return {t.info_hash[0]: (t.status, t.save_path)
                for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid))}


# ---------------- same_dir：同一物理目录的不同写法 ----------------

@pytest.mark.parametrize("a,b,want,why", [
    ("/data/26A/番", "/data/26A/番", True, "逐字相同"),
    ("/data/26A/番/", "/data/26A/番", True, "末尾多一个分隔符"),
    ("/data/26A/番", "/data/26a/番", False, "POSIX 区分大小写，不能替它合并"),
    ("/data/25D/番", "/data/26A/番", False, "真的不同目录"),
    ("E:\\Anime\\AutoRSS\\26A\\番", "E:/Anime/AutoRSS/26A/番", True, "Windows：只差分隔符"),
    ("E:\\Anime\\AutoRSS\\26A\\番", "e:\\anime\\autorss\\26a\\番", True, "Windows：只差大小写"),
    ("E:/Anime/AutoRSS/26A/番", "E:\\Anime\\AutoRSS\\26A\\番", True, "Windows：a 用正斜杠也认得出"),
    ("\\\\NAS\\share\\番", "//NAS/share/番", True, "UNC：只差分隔符"),
    ("//NAS/share/番", "\\\\NAS\\share\\番", True, "UNC：正斜杠写法在前也认得出（r39 F2）"),
    ("//NAS/share/番", "//nas/SHARE/番", True, "正斜杠 UNC 也按 Windows 规则合并大小写"),
    ("/data/26A/番", "E:\\data\\26A\\番", False, "一侧 POSIX 一侧 Windows：不同目录"),
    ("E:\\Anime\\26A\\番", "E:\\Anime\\25D\\番", False, "Windows：真的不同目录"),
    ("", "/data", False, "空不等于非空"),
    (None, "", True, "空与空相同"),
    ("/data", None, False, "调用方没给 old_path"),
])
def test_same_dir(a, b, want, why):
    """(r39 F2) 每条都两种参数顺序各测一遍：结果不许跟谁在前有关（relocate/rows_in_wrong_dir 都把行上的 save_path 放前面）。"""
    assert ce.same_dir(a, b) is want, why
    assert ce.same_dir(b, a) is want, f"{why}（参数反过来）"


async def test_unc_spelling_change_keeps_rows_in_place(clean_tables, cfg, monkeypatch):
    """(r39 F2) DOWN_PATH 原来写 //NAS/share（行上存正斜杠），后来改成 \\\\NAS\\share：同一个物理目录，
    rows_in_wrong_dir 不报、relocate 不清 qB 不认识的行。修前 same_dir 只看第一个参数（行上的正斜杠）→ posixpath → 判成不在位。"""
    _setup(cfg, down="\\\\NAS\\share")
    with clean_tables.get_session() as s:
        aid = _anime(s, [])
    NEW = A.anime_save_path(aid)
    assert NEW == "\\\\NAS\\share\\26A\\番", NEW
    with clean_tables.get_session() as s:
        s.add(AnimeTorrent(anime_id=aid, info_hash="a" * 40, raw_title="a", episode=1, status="sent",
                           qb_progress=1.0, save_path="//NAS/share/26A/番", created_at=datetime.now()))
        s.commit()
    _qb(monkeypatch)
    assert ce.rows_in_wrong_dir(A.list_episodes(aid), NEW) == []
    rep = await A.relocate_anime(aid, old_path=NEW)
    assert (rep["redownload"], rep["moved"]) == (0, 0), rep
    assert _status(clean_tables, aid)["a"] == ("sent", "//NAS/share/26A/番")


def test_gate_predicates_ignore_windows_spelling_differences():
    """搬迁闸与 relocate 选行同一谓词：只改分隔符/大小写的写法既不算『不在位』，也不算『目录变了』。"""
    from types import SimpleNamespace
    NEW = "E:\\Anime\\AutoRSS\\26A\\番"
    rows = [SimpleNamespace(status="sent", save_path="e:/anime/autorss/26A/番", archived_at=None)]
    assert ce.rows_in_wrong_dir(rows, NEW) == []
    assert ce.needs_relocate(rows, NEW, "E:/Anime/AutoRSS/26A/番") is False
    assert ce.needs_relocate(rows, NEW, "E:\\Anime\\AutoRSS\\25D\\番") is True, "目录真的变了照样要问"


# ---------------- U7a / Q2(a)(b)：已在当前目录的行不碰；提示目录只取实际被清的行 ----------------

async def test_move_back_keeps_in_place_rows_and_points_only_at_the_old_dir(clean_tables, cfg, monkeypatch):
    """anime#2 回放的形状：搬回来入口(old_path == new_path)，c 在旧目录、qB 认识；a 在旧目录、qB 不认识；
    b/d 就在当前目录、qB 不认识。b/d 必须保持 sent，提示里的旧目录只能是 OLD。"""
    _setup(cfg)
    with clean_tables.get_session() as s:
        aid = _anime(s, [])
    NEW = A.anime_save_path(aid)
    assert NEW == "/data/26A/番"
    with clean_tables.get_session() as s:
        for h, ep, sp in (("a", 1, OLD), ("b", 2, NEW), ("c", 3, OLD), ("d", 4, NEW)):
            s.add(AnimeTorrent(anime_id=aid, info_hash=h * 40, raw_title=h, episode=ep, status="sent",
                               qb_progress=1.0, save_path=sp, created_at=datetime.now()))
        s.commit()
    moved = _qb(monkeypatch, known="c")

    rep = await A.relocate_anime(aid, old_path=NEW)

    got = _status(clean_tables, aid)
    assert got["b"] == ("sent", NEW) and got["d"] == ("sent", NEW), f"已在当前目录的行被清了：{got}"
    assert got["a"][0] == "pending", got
    assert got["c"] == ("sent", NEW), got
    assert moved == [(["c"], NEW)], f"setLocation 只该收到旧目录那一条：{moved}"
    assert (rep["moved"], rep["redownload"], rep["in_place"]) == (1, 1, 2), rep
    assert rep["redownload_dirs"] == [OLD], rep
    assert rep["old_path"] == OLD and NEW not in rep["old_path"], rep


async def test_in_place_tracked_rows_are_not_set_located(clean_tables, cfg, monkeypatch):
    """已在位、qB 认识的行：setLocation 本就是空操作，跳过；也不计 moved、不计 in_place。"""
    _setup(cfg)
    with clean_tables.get_session() as s:
        aid = _anime(s, [("a", 1, "sent", "/data/26A/番"), ("b", 2, "sent", OLD)])
    moved = _qb(monkeypatch, known="ab")
    rep = await A.relocate_anime(aid, old_path=OLD)
    assert moved == [(["b"], "/data/26A/番")], moved
    assert (rep["moved"], rep["in_place"], rep["redownload"]) == (1, 0, 0), rep
    assert rep["old_path"] == OLD, "没有被清/停滞的行时，传入的旧目录确实不是当前目录 → 保留"


async def test_special_episode_dir_never_enters_old_path(clean_tables, cfg, monkeypatch):
    """特别篇/未知集是唯一一份（E-4），它所在的目录不进『旧文件在…』那句——停滞的特别篇也一样。"""
    _setup(cfg)
    SP_DIR = "/data/25C/番SP"
    with clean_tables.get_session() as s:
        aid = _anime(s, [("a", 1, "sent", OLD), ("b", -1, "sent", SP_DIR), ("c", -2, "sent", SP_DIR),
                         ("e", -1, "stalled", SP_DIR)])
    _qb(monkeypatch)
    rep = await A.relocate_anime(aid, old_path="/data/26A/番")
    assert (rep["special_kept"], rep["stalled_kept"], rep["redownload"]) == (2, 1, 1), rep
    assert rep["old_path"] == OLD and SP_DIR not in rep["old_path"], rep
    assert rep["redownload_dirs"] == [OLD], rep
    assert rep["stalled_dirs"] == [], "唯一的停滞行是特别篇：停滞那句不该点名任何目录（r39 F1）"
    assert _status(clean_tables, aid)["b"][0] == "sent"


async def test_stalled_dir_enters_old_path(clean_tables, cfg, monkeypatch):
    """停滞行不清，但它的半成品留在原处，目录要进 old_path（提示『停滞的半成品留在…』用）。"""
    _setup(cfg)
    STALL_DIR = "/data/25B/番"
    with clean_tables.get_session() as s:
        aid = _anime(s, [("a", 1, "sent", OLD), ("e", 2, "stalled", STALL_DIR)])
    _qb(monkeypatch)
    rep = await A.relocate_anime(aid, old_path="/data/26A/番")
    assert rep["stalled_kept"] == 1 and rep["redownload"] == 1, rep
    assert rep["old_path"] == "、".join(sorted({OLD, STALL_DIR})), rep
    assert rep["redownload_dirs"] == [OLD], "停滞行没被清，不进 redownload_dirs"
    assert rep["stalled_dirs"] == [STALL_DIR], "停滞那句只用停滞行自己的目录，不混进被清行的（r39 F1）"


async def test_windows_spelling_variants_change_nothing(clean_tables, cfg, monkeypatch):
    """DOWN_PATH 写成 'E:\\…'，行上记的是 'e:/…'（只差分隔符/大小写）：同一个目录，0 行变化、不问 qB。"""
    _setup(cfg, down="E:\\Anime\\AutoRSS")
    with clean_tables.get_session() as s:
        aid = _anime(s, [])
    NEW = A.anime_save_path(aid)
    assert NEW == "E:\\Anime\\AutoRSS\\26A\\番"
    variant = NEW.replace("\\", "/").lower()
    with clean_tables.get_session() as s:
        for h, ep in (("a", 1), ("b", 2)):
            s.add(AnimeTorrent(anime_id=aid, info_hash=h * 40, raw_title=h, episode=ep, status="sent",
                               qb_progress=1.0, save_path=variant, created_at=datetime.now()))
        s.commit()
    asked = []

    async def info(hashes):
        asked.append(hashes)
        return {}
    monkeypatch.setattr(ce.qb, "torrents_info", info)

    rep = await A.relocate_anime(aid, old_path=NEW)

    assert asked == [], "全部已在位：连 qB 都不该问"
    assert (rep["moved"], rep["redownload"]) == (0, 0), rep
    assert all(v == ("sent", variant) for v in _status(clean_tables, aid).values())


async def test_cleared_row_without_save_path_and_move_back_gives_no_old_path(clean_tables, cfg, monkeypatch):
    """被清的是没记 save_path 的老行、且是搬回来入口(old_path == new_path)：判不出旧文件在哪，
    old_path 置 None，redownload_dirs 如实写『原目录未记录』（页面据此换成『原来放在哪里没有记录』，r39 F4）。"""
    _setup(cfg)
    with clean_tables.get_session() as s:
        aid = _anime(s, [("a", 1, "sent", None)])
    _qb(monkeypatch)
    rep = await A.relocate_anime(aid, old_path="/data/26A/番")
    assert rep["redownload"] == 1, rep
    assert rep["old_path"] is None, rep
    assert rep["redownload_dirs"] == ["原目录未记录"], rep
    assert rep["stalled_dirs"] == [], rep


async def test_movie_line_shares_the_same_in_place_rule(clean_tables, cfg, monkeypatch):
    """剧场版走同一个 relocate：已在当前年份目录、qB 不认识的版本保持 sent。"""
    _setup(cfg)
    with clean_tables.get_session() as s:
        m = Movie(title="片", display_name="片", quarter="2026")   # 有名字，目录名不随种子标题回退
        s.add(m); s.commit(); s.refresh(m)
        mid = m.id
    NEW = M.movie_save_path(mid)
    assert NEW == "/data/2026/片"
    with clean_tables.get_session() as s:
        s.add(MovieTorrent(movie_id=mid, info_hash="a" * 40, raw_title="a", status="sent",
                           qb_progress=1.0, save_path=NEW))
        s.add(MovieTorrent(movie_id=mid, info_hash="b" * 40, raw_title="b", status="sent",
                           qb_progress=1.0, save_path="/data/2025/片"))
        s.commit()
    _qb(monkeypatch)
    rep = await M.relocate_movie(mid, old_path=NEW)
    with clean_tables.get_session() as s:
        got = {t.info_hash[0]: t.status for t in s.exec(select(MovieTorrent).where(MovieTorrent.movie_id == mid))}
    assert got == {"a": "sent", "b": "pending"}, got
    assert (rep["in_place"], rep["redownload"], rep["old_path"]) == (1, 1, "/data/2025/片"), rep


# ---------------- Q2(c)：两个提示函数 ----------------

_NOTIFIERS = [("pages.anime_detail", "notify_relocate_anime"), ("pages.movies", "_notify_relocate_movie")]


def _notify(monkeypatch, mod, fn, **kw):
    import importlib
    m = importlib.import_module(mod)
    got = []
    monkeypatch.setattr(m.ui, "notify", lambda msg, **k: got.append((msg, k)))
    rep = {"new_path": "/n", "old_path": "/o", "moved": 0, "redownload": 0, "redownload_dirs": [],
           "stalled_dirs": [], "untracked": 0, "in_place": 0, "failed": 0, "stalled_kept": 0,
           "special_kept": 0, "delivering": 0}
    rep.update(kw)
    getattr(m, fn)(rep)
    assert len(got) == 1
    return got[0]


@pytest.mark.parametrize("mod, fn", _NOTIFIERS)
def test_redownload_warning_names_only_the_cleared_dirs(monkeypatch, mod, fn):
    msg, kw = _notify(monkeypatch, mod, fn, redownload=3, untracked=3, redownload_dirs=["/o"])
    assert kw.get("type") == "warning", kw
    assert "连不上" not in msg, msg
    assert "qB 里已没有这条种子" in msg and "已清成待下" in msg, msg
    assert "旧文件（如果还在）在 /o" in msg and "下完再删" in msg and "别整目录删" in msg, msg
    assert "/n" not in msg, f"当前目录被说成了旧目录：{msg}"
    assert "没跟着搬、仍在用" not in msg, "没有别的在用文件时不该补那句"
    assert "需你手动清理" not in msg, msg
    if mod == "pages.movies":
        assert "不会自动重下" in msg and "手动点『下载』" in msg, msg
    else:
        assert "待确认或已忽略的要先确认或恢复订阅" in msg, msg


@pytest.mark.parametrize("mod, fn", _NOTIFIERS)
def test_redownload_with_stalled_mentions_files_still_in_use(monkeypatch, mod, fn):
    """(r39 F1) 『还有别的文件在用』那句不断言在哪个目录（没逐一核对）；停滞那句只用 stalled_dirs。"""
    msg, _ = _notify(monkeypatch, mod, fn, redownload=1, redownload_dirs=["/o"], stalled_kept=1,
                     stalled_dirs=["/s"])
    assert "另有停滞、特别篇、移动被拒或交付中的文件没跟着搬、仍在用" in msg, msg
    assert "该目录里" not in msg, msg
    assert "停滞的半成品留在 /s，需你手动处理" in msg, msg


@pytest.mark.parametrize("mod, fn", _NOTIFIERS)
def test_stalled_notice_uses_only_stalled_dirs(monkeypatch, mod, fn):
    """(r39 F1) old_path 是被清行与停滞行的并集，停滞那句不再用它：stalled_dirs 为空就不出那句。"""
    msg, kw = _notify(monkeypatch, mod, fn, stalled_kept=1, old_path="/o")
    assert "停滞的半成品留在" not in msg and kw.get("type") == "warning", msg
    msg, _ = _notify(monkeypatch, mod, fn, stalled_kept=1, old_path=None)
    assert "停滞的半成品留在" not in msg, msg
    msg, _ = _notify(monkeypatch, mod, fn, stalled_kept=1, old_path="/o、/s", stalled_dirs=["/s"])
    assert "停滞的半成品留在 /s，" in msg and "/o" not in msg, msg


@pytest.mark.parametrize("mod, fn", _NOTIFIERS)
def test_unrecorded_old_dir_is_not_spelled_like_a_path(monkeypatch, mod, fn):
    """(r39 F4) 被清的全是没记 save_path 的老行：不拼成『在 原目录未记录：…』。"""
    msg, _ = _notify(monkeypatch, mod, fn, redownload=3, untracked=3, redownload_dirs=["原目录未记录"])
    assert "原来放在哪里没有记录" in msg and "自行核对" in msg, msg
    assert "在 原目录未记录" not in msg, msg
    assert "唯一一份" in msg, msg


@pytest.mark.parametrize("mod, fn", _NOTIFIERS)
def test_in_place_is_reported_as_untouched(monkeypatch, mod, fn):
    msg, _ = _notify(monkeypatch, mod, fn, in_place=2)
    assert "2" in msg and "qB 已不跟踪、文件本就在当前目录，未改动" in msg, msg
    assert "删" not in msg, msg


@pytest.mark.parametrize("mod, fn", _NOTIFIERS)
def test_only_special_kept_has_no_cleanup_wording(monkeypatch, mod, fn):
    msg, kw = _notify(monkeypatch, mod, fn, special_kept=2)
    assert kw.get("type") == "warning", kw
    for w in ("清理", "再删", "手动处理", "旧文件", "/o"):
        assert w not in msg, (w, msg)


async def test_move_back_end_to_end_notice_never_names_the_current_dir(clean_tables, cfg, monkeypatch):
    """relocate → 页面提示一条龙（anime#2 形状）：提示里不得出现当前目录。"""
    import pages.anime_detail as D
    _setup(cfg)
    with clean_tables.get_session() as s:
        aid = _anime(s, [("a", 1, "sent", OLD)] + [(c, i + 2, "sent", "/data/26A/番") for i, c in enumerate("bcdefghijk")])
    _qb(monkeypatch, known="a")
    rep = await A.relocate_anime(aid, old_path="/data/26A/番")
    assert (rep["moved"], rep["redownload"], rep["in_place"]) == (1, 0, 10), rep
    got = []
    monkeypatch.setattr(D.ui, "notify", lambda msg, **k: got.append(msg))
    D.notify_relocate_anime(rep)
    assert "/data/26A/番" not in got[0] and "删" not in got[0], got
    assert "10 集 qB 已不跟踪" in got[0], got
    assert all(v[0] == "sent" for v in _status(clean_tables, aid).values())


def _anime_notice(monkeypatch, rep):
    import pages.anime_detail as D
    got = []
    monkeypatch.setattr(D.ui, "notify", lambda msg, **k: got.append(msg))
    D.notify_relocate_anime(rep)
    return got[0]


async def test_stalled_special_plus_cleared_notice_names_no_stalled_dir(clean_tables, cfg, monkeypatch):
    """(r39 F1) 审查探针原样：被清的 a 在 OLD，唯一的停滞行是特别篇。修前提示写『停滞的半成品留在 OLD』，
    而 OLD 里只有被清那一集（重下完成前唯一一份）、根本没有停滞文件。"""
    _setup(cfg)
    with clean_tables.get_session() as s:
        aid = _anime(s, [("a", 1, "sent", OLD), ("e", -1, "stalled", "/data/25C/番SP")])
    _qb(monkeypatch)
    rep = await A.relocate_anime(aid, old_path="/data/26A/番")
    assert (rep["redownload"], rep["stalled_kept"], rep["stalled_dirs"]) == (1, 1, []), rep
    msg = _anime_notice(monkeypatch, rep)
    assert "停滞的半成品留在" not in msg, msg
    assert "该目录里" not in msg, msg
    assert "这 1 集的旧文件（如果还在）在 /data/25D/番" in msg, msg


async def test_stalled_notice_names_only_the_stalled_rows_own_dir(clean_tables, cfg, monkeypatch):
    """(r39 F1) 停滞在 25B、被清在 25D：『停滞的半成品留在』后面只能是 25B，修前是『25B、25D』。"""
    _setup(cfg)
    STALL_DIR = "/data/25B/番"
    with clean_tables.get_session() as s:
        aid = _anime(s, [("a", 1, "sent", OLD), ("e", 2, "stalled", STALL_DIR)])
    _qb(monkeypatch)
    rep = await A.relocate_anime(aid, old_path=OLD)
    msg = _anime_notice(monkeypatch, rep)
    tail = msg.split("停滞的半成品留在", 1)[1]
    assert tail.startswith(f" {STALL_DIR}，"), msg
    assert OLD not in tail, f"被清行的目录混进了停滞那句：{msg}"


@pytest.mark.parametrize("line, noun", [("anime", "集"), ("movie", "个版本")])
async def test_midway_disconnect_error_promises_no_redownload(clean_tables, cfg, monkeypatch, line, noun):
    """(r39 F3) setLocation 中途连不上：error 是用户唯一看得到的一句，不许诺『待重下』（剧场版不会自动重下），名词跟着线走。"""
    _setup(cfg)
    if line == "anime":
        with clean_tables.get_session() as s:
            oid = _anime(s, [("a", 1, "sent", OLD), ("b", 2, "sent", OLD)])
        go = lambda: A.relocate_anime(oid, old_path=OLD)   # noqa: E731
    else:
        with clean_tables.get_session() as s:
            m = Movie(title="片", display_name="片", quarter="2026")
            s.add(m); s.commit(); s.refresh(m)
            oid = m.id
            for h in "ab":
                s.add(MovieTorrent(movie_id=oid, info_hash=h * 40, raw_title=h, status="sent",
                                   qb_progress=1.0, save_path="/data/2025/片"))
            s.commit()
        go = lambda: M.relocate_movie(oid, old_path="/data/2025/片")   # noqa: E731
    _qb(monkeypatch, known="a", code=None)
    rep = await go()
    assert rep["untracked"] == 1, rep
    assert f"此前已有 1 {noun}因 qB 确实不认识，已清成待下" in rep["error"], rep["error"]
    assert "重下" not in rep["error"], rep["error"]
    if line == "movie":
        assert "集" not in rep["error"], rep["error"]


# ---------------- F5 / F35：两个确认框 ----------------

def _row(sp, status="sent", archived=None):
    from types import SimpleNamespace
    return SimpleNamespace(status=status, save_path=sp, archived_at=archived)


def _dialog(monkeypatch, line, new_path, rows):
    """把 maybe_relocate_* 接到假数据上，记下确认框（返回 False＝用户取消）与通知。"""
    if line == "anime":
        import pages.anime_detail as P
        monkeypatch.setattr(P.anime, "anime_save_path", lambda _id: new_path)
        monkeypatch.setattr(P.anime, "list_episodes", lambda _id: rows)
        run = lambda old: P.maybe_relocate_anime(1, old)   # noqa: E731
    else:
        import pages.movies as P
        monkeypatch.setattr(P.mov, "movie_save_path", lambda _id: new_path)
        monkeypatch.setattr(P.mov, "movie_torrents", lambda _id: rows)
        run = lambda old: P._maybe_relocate_movie(1, old, lambda: None)   # noqa: E731
    asked, notes = [], []

    async def fake_confirm(title, body, **kw):
        asked.append((title, body))
        return False
    monkeypatch.setattr(P, "confirm", fake_confirm)
    monkeypatch.setattr(P.ui, "notify", lambda msg, **k: notes.append(msg))
    return run, asked, notes


_LINES = [("anime", "集"), ("movie", "个版本")]


@pytest.mark.parametrize("line, noun", _LINES)
async def test_dialog_counts_only_rows_relocate_will_touch(monkeypatch, line, noun):
    """(r39 F5) 目录真的变了：已在新目录的那行 relocate 会跳过，不算；save_path 为空的老行 relocate 照常处理，要算。"""
    NEW = "/data/26A/番"
    rows = [_row(NEW), _row(NEW), _row(OLD), _row(None), _row(OLD, archived=datetime.now())]
    run, asked, _ = _dialog(monkeypatch, line, NEW, rows)
    await run(OLD)
    assert asked and asked[0][0] == "归档目录变了，移动已下文件？", asked
    assert asked[0][1].startswith(f"2 {noun}已下，移到新目录"), asked


@pytest.mark.parametrize("line, noun", _LINES)
async def test_dialog_title_treats_spelling_variants_as_the_same_dir(monkeypatch, line, noun):
    """(r39 F5) old_path 与 new_path 只差分隔符/大小写：同一个目录，标题说『搬回去』，不说『归档目录变了』。"""
    NEW = "E:\\Anime\\26A\\番"
    run, asked, _ = _dialog(monkeypatch, line, NEW, [_row("E:\\Anime\\25D\\番"), _row(NEW)])
    await run("e:/anime/26a/番")
    assert asked and asked[0][0] == "有已下文件不在归档目录里，搬回去？", asked
    assert asked[0][1].startswith(f"1 {noun}的文件落在别处"), asked


@pytest.mark.parametrize("line, noun", _LINES)
async def test_move_back_dialog_does_not_assert_the_cause(monkeypatch, line, noun):
    """(r39 F35) 设置页目录分裂提示让用户『编辑季度原样保存』，走的就是搬回来这一支：
    括号不能只写『上次没搬成』（那时是刚改了设置）。"""
    NEW = "/data/26A/番"
    run, asked, _ = _dialog(monkeypatch, line, NEW, [_row(OLD), _row(NEW)])
    await run(NEW)
    assert asked, "文件落在别处却没问"
    assert "（上次没搬成，或改过下载目录设置）" in asked[0][1], asked


@pytest.mark.parametrize("line, noun", _LINES)
async def test_dialog_skipped_when_every_file_is_already_in_place(monkeypatch, line, noun):
    """(r39 F5) 目录变了但已下的都已在新目录（改走又改回、当时没搬）：不弹『0 … 移到新目录』，只提示不用移动。"""
    NEW = "/data/26A/番"
    run, asked, notes = _dialog(monkeypatch, line, NEW, [_row(NEW), _row(NEW, status="stalled")])
    await run(OLD)
    assert asked == [], asked
    assert notes and "本就在新目录，不用移动" in notes[0], notes


# ---------------- Q9：fill_content_paths 的两处竞态 ----------------

async def test_fill_skips_write_when_relocate_moved_the_row_meanwhile(clean_tables, cfg, monkeypatch):
    """fill 在 await qB 期间，页面上的 relocate 已把这行搬到新目录、清空 content_path：
    qB 刚才回的是搬迁前的旧路径，不能写回；下一轮再问，补上新路径。"""
    _setup(cfg)
    ce._path_miss.clear()
    NEW = "/data/26A/番"
    with clean_tables.get_session() as s:
        aid = _anime(s, [("a", 1, "sent", OLD)])
    gate, calls = asyncio.Event(), []

    async def info(hashes):
        calls.append(list(hashes))
        if len(calls) == 1:                     # fill 的第一问：卡住，等 relocate 跑完
            await gate.wait()
            return {h: {"content_path": f"{OLD}/01.mkv", "state": "stalledUP"} for h in hashes}
        return {h: {"content_path": f"{NEW}/01.mkv", "state": "stalledUP"} for h in hashes}

    async def set_location(hashes, path):
        return 200
    monkeypatch.setattr(ce.qb, "torrents_info", info)
    monkeypatch.setattr(ce.qb, "set_location", set_location)

    task = asyncio.create_task(ce.fill_content_paths())
    for _ in range(50):
        if calls:
            break
        await asyncio.sleep(0)
    assert calls, "fill 没走到 await qB"
    rep = await A.relocate_anime(aid, old_path=OLD)
    assert rep["moved"] == 1, rep
    gate.set()
    assert await task == 0

    with clean_tables.get_session() as s:
        t = s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid)).one()
    assert t.content_path is None and t.save_path == NEW, (t.content_path, t.save_path)
    assert not ce._path_miss, "save_path 变了只是跳过，不该记成 qB 里没有"

    assert await ce.fill_content_paths() == 1
    with clean_tables.get_session() as s:
        t = s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid)).one()
    assert t.content_path == f"{NEW}/01.mkv", t.content_path


async def test_fill_miss_is_keyed_by_hash_not_by_reused_row_id(clean_tables, cfg, monkeypatch):
    """hash X 在 qB 里查不到 → 记下不再问；这行删掉后新行复用同一个 id、hash 是 Y：Y 照样要问、补上路径。"""
    _setup(cfg)
    ce._path_miss.clear()
    with clean_tables.get_session() as s:
        aid = _anime(s, [("x", 1, "sent", OLD)])
        tid = s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid)).one().id
    asked = []

    async def info(hashes):
        asked.append(list(hashes))
        return {h: {"content_path": f"{OLD}/{h[0]}.mkv", "state": "stalledUP"} for h in hashes if h[0] == "y"}
    monkeypatch.setattr(ce.qb, "torrents_info", info)

    assert await ce.fill_content_paths() == 0
    assert asked == [["x" * 40]]
    with clean_tables.get_session() as s:
        s.delete(s.get(AnimeTorrent, tid))
        s.commit()
        s.add(AnimeTorrent(id=tid, anime_id=aid, info_hash="y" * 40, raw_title="y", episode=1,
                           status="sent", save_path=OLD, created_at=datetime.now()))
        s.commit()
    asked.clear()

    assert await ce.fill_content_paths() == 1
    assert asked == [["y" * 40]], "复用了旧 id 的新种子被当成『问过、qB 里没有』跳过了"
    with clean_tables.get_session() as s:
        assert s.get(AnimeTorrent, tid).content_path == f"{OLD}/y.mkv"
