"""保存路径构建：越界、跨平台、字节截断。

build_save_path 决定文件落到哪；它出错的后果是"下到了不该下的地方"或"整条下载静默失败"，
而且不像集号错那样能从 UI 上看出来，所以边界必须钉死。
"""
import ntpath
import posixpath

import pytest

from core.engine import build_save_path, prev_quarter, quarter_label, safe_name


# ---------------- safe_name ----------------

@pytest.mark.parametrize("raw,expect", [
    ("普通番名", "普通番名"),
    ("a/b", "a_b"), ("a\\b", "a_b"), ("a:b", "a_b"), ("a*b?c", "a_b_c"),
    ("<x>|y", "_x__y"),
    ("..", "unknown"), (".", "unknown"), ("...", "unknown"),
    ("../etc", "_etc"),           # 分隔符先被替换，剩下的 '..' 被 strip('.') 剥掉
    ("", "unknown"), ("   ", "unknown"), (None, "unknown"),
    ("  留白  ", "留白"),
    ("名字.", "名字"),             # 尾点会被 Windows 吞掉，先剥
])
def test_safe_name(raw, expect):
    assert safe_name(raw) == expect


def test_safe_name_never_escapes_one_segment():
    """无论输入什么，产物都必须是【单段】名字：不含任何路径分隔符，也不是 . / .."""
    for raw in ("../../etc/passwd", "C:\\Windows\\System32", "a/../../b", "\x00\x1f邪恶",
                "..\\..\\..", "/absolute/path", "名字\n换行"):
        out = safe_name(raw)
        assert "/" not in out and "\\" not in out, raw
        assert out not in (".", ".."), raw
        assert out and out == out.strip(), raw


def test_safe_name_truncates_by_bytes_without_splitting_chars():
    """按字节截断（文件系统限制是字节），但不能把多字节字符切成半个 —— 切碎会得到无法解码的文件名。"""
    long_cjk = "番" * 200                      # 每个 3 字节 = 600 字节
    out = safe_name(long_cjk)
    assert len(out.encode("utf-8")) <= 200
    out.encode("utf-8").decode("utf-8")        # 能往返解码就说明没切碎
    assert set(out) == {"番"}


# ---------------- quarter_label / prev_quarter ----------------

@pytest.mark.parametrize("q,prev", [
    ("26A", "25D"), ("26B", "26A"), ("26C", "26B"), ("26D", "26C"),
])
def test_prev_quarter(q, prev):
    assert prev_quarter(q) == prev


def test_quarter_label_follows_ui_template(cfg):
    """页面显示名走 QUARTER_FMT_UI；它留空时【跟随】文件夹模板（见 config.__getattr__ 的派生项）。"""
    cfg(QUARTER_FMT_UI="{yyyy}年{season}", QUARTER_FMT="{yy}{q}")
    assert quarter_label("26A") == "2026年冬"
    cfg(QUARTER_FMT_UI="")
    assert quarter_label("26A") == "26A"


@pytest.mark.parametrize("fmt", ["{year}{season}", "{yy.foo}", "{", "{yy:>{yyyy}}", "{0}"])
def test_bad_quarter_template_never_raises(cfg, fmt):
    """模板由用户在设置页手填。任何渲染季度名的页面都在调它——抛一次就是整页打挂，
    所以出错必须回退成原始季度键，且长度要封顶（'{yy:>{yyyy}}' 能生成几千字符的目录名）。"""
    cfg(QUARTER_FMT_UI=fmt)
    out = quarter_label("26A")
    assert isinstance(out, str) and 0 < len(out) <= 60


# ---------------- build_save_path ----------------

def test_no_root_configured_returns_none(cfg):
    """工作目录与该侧目录都空 = 无处下载。必须返回 None 而不是拼出一个相对路径。"""
    cfg(DOWN_PATH="", QUARTER_FMT="{year}{season}", ANIME_SEASON_SUBFOLDER=False)
    assert build_save_path("26A", "某番", sub_dir="") is None


def test_posix_layout(cfg):
    cfg(DOWN_PATH="/downloads", QUARTER_FMT="{yyyy}年{season}", ANIME_SEASON_SUBFOLDER=False)
    p = build_save_path("26A", "某番", sub_dir="番剧")
    assert p == posixpath.join("/downloads", "番剧", "2026年冬", "某番")


def test_windows_layout_uses_backslash(cfg):
    """根是 Windows 形态时分隔符必须跟着变——否则 qB 回报的路径与我们存的逐字对不上，
    搬迁/归档的路径比对会全部落空。"""
    cfg(DOWN_PATH=r"D:\media", QUARTER_FMT="{yy}{q}", ANIME_SEASON_SUBFOLDER=False)
    p = build_save_path("26A", "某番", sub_dir="anime")
    assert p.startswith(r"D:\media") and "\\" in p and "/" not in p


def test_empty_quarter_fmt_skips_the_quarter_dir(cfg):
    """模板留空 = 不建季度目录（用户可能想把全部番平铺）。"""
    cfg(DOWN_PATH="/downloads", ANIME_SEASON_SUBFOLDER=False)
    assert build_save_path("26A", "某番", sub_dir="", quarter_fmt="") == "/downloads/某番"


def test_season_subfolder_toggle(cfg):
    cfg(DOWN_PATH="/downloads", QUARTER_FMT="", ANIME_SEASON_SUBFOLDER=True)
    assert build_save_path("26A", "某番", season=2, sub_dir="") == "/downloads/某番/Season 2"
    cfg(ANIME_SEASON_SUBFOLDER=False)
    assert build_save_path("26A", "某番", season=2, sub_dir="") == "/downloads/某番"


@pytest.mark.parametrize("evil", ["../../etc", "..", "a/../../..", "/etc/passwd", "名字/子目录"])
def test_folder_name_cannot_escape_root(cfg, evil):
    """番名来自字幕组标题（完全外部可控），绝不能靠它跳出下载根目录。"""
    cfg(DOWN_PATH="/downloads", QUARTER_FMT="{yy}{q}", ANIME_SEASON_SUBFOLDER=False)
    p = build_save_path("26A", evil, sub_dir="番剧")
    assert p is None or p.startswith("/downloads/番剧/"), (evil, p)


@pytest.mark.parametrize("evil_quarter", ["../..", "26A/../..", "..\\..", "\x00"])
def test_quarter_cannot_escape_root(cfg, evil_quarter):
    cfg(DOWN_PATH="/downloads", QUARTER_FMT="{yy}{q}", ANIME_SEASON_SUBFOLDER=False)
    p = build_save_path(evil_quarter, "某番", sub_dir="番剧")
    assert p is None or p.startswith("/downloads/番剧/"), (evil_quarter, p)


def test_absolute_sub_dir_when_no_work_dir(cfg):
    """没有工作目录时，该侧目录就是绝对根（番剧与剧场版可以落在不同盘）。"""
    cfg(DOWN_PATH="", QUARTER_FMT="", ANIME_SEASON_SUBFOLDER=False)
    assert build_save_path("26A", "某番", sub_dir="/mnt/tv") == "/mnt/tv/某番"


# ---------------- 搬迁闸问的是"文件在哪"，不是"记录改没改"（R18） ----------------

def test_rows_in_wrong_dir_finds_files_left_behind():
    """(R18) 搬迁失败/被拒绝一次之后，必须还能被发现。

    两侧的 maybe_relocate_* 原本都写 `if new_path == old_path: return`，而四个调用点拿到的
    old_path **也是**同一个 `*_save_path(id)` 算出来的——这道闸问的是"我这次操作把记录改了没有"，
    而不是"盘上的文件跟当前归档目录对得上没有"。于是搬迁只要没成一次，
    之后【再没有任何入口】能补搬，engine.relocate 的提示还会指向一个没有文件的目录。

    真库实证：anime#96『落语朱音』10 集躺在旧错绑名的目录里，记录早已改对，界面上没有任何地方
    还会提出搬它。
    """
    from types import SimpleNamespace

    from core import engine

    def row(status="sent", path="/new", archived=None):
        return SimpleNamespace(status=status, save_path=path, archived_at=archived)

    NEW = "/anime/26B/正确的名字"
    OLD = "/anime/26C/错绑时的名字"
    assert engine.rows_in_wrong_dir([row(path=NEW)], NEW) == []
    assert len(engine.rows_in_wrong_dir([row(path=OLD)], NEW)) == 1, "落在旧目录的行没被发现"
    # 已归档的不算：不在 qB，setLocation 移不动，算进来会出现「说要搬 → 点确认 → 报无需移动」
    assert engine.rows_in_wrong_dir([row(path=OLD, archived="x")], NEW) == []
    # 没交付过的不算
    assert engine.rows_in_wrong_dir([row(status="pending", path=OLD)], NEW) == []
    # 空 save_path 不算（老行）
    assert engine.rows_in_wrong_dir([row(path="")], NEW) == []


@pytest.mark.parametrize("new,old,paths,want,why", [
    ("/new", "/new", ["/new"], False, "路径没变、文件也都在该在的地方 → 不打扰"),
    ("/new", "/old", ["/old"], True, "这次操作改了归档目录"),
    ("/new", "/new", ["/old"], True, "路径没变，但盘上文件落在别处（R18 补的正是这一支）"),
    ("/new", "/new", [""], False, "老行没记 save_path，判不了就不判"),
    ("", "/old", ["/old"], False, "算不出新路径（未配置下载目录）→ 什么都别做"),
    ("/new", None, ["/new"], True, "调用方没给 old_path 也要当成变了"),
])
def test_needs_relocate(new, old, paths, want, why):
    """(R19) 搬迁闸的判据是纯函数，表驱动地钉住。

    【为什么从"grep 源码"改成这个】上一版守卫只断言两个页面文件里出现过 'rows_in_wrong_dir'
    这个字符串、且没出现过老闸的逐字写法。第 19 轮的审计实测：把番剧侧的闸整段回退回 R18 之前，
    只要保留上面那句提到 rows_in_wrong_dir 的【注释】，全套用例一条都不红 ——
    也就是说 R18 在番剧侧的整个修复可以被完整撤掉而套件毫无察觉，
    而那条守卫的 docstring 自称守的正是"本项目反复出现的第①种缺陷形状"。
    """
    from types import SimpleNamespace

    from core import engine
    rows = [SimpleNamespace(status="sent", save_path=p, archived_at=None) for p in paths]
    assert engine.needs_relocate(rows, new, old) is want, why


def test_both_relocate_gates_call_the_shared_predicate():
    """(R19) 两侧的闸必须真的【调用】那个纯函数，而不是只在注释里提到它。

    用 AST 看函数体里有没有 `engine.needs_relocate(...)` 这个调用 —— 注释和字符串都骗不过它。
    """
    import ast
    import pathlib
    for f, fn in (("pages/anime_detail.py", "maybe_relocate_anime"),
                  ("pages/movies.py", "_maybe_relocate_movie")):
        tree = ast.parse(pathlib.Path(f).read_text(encoding="utf8"))
        body = next((n for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn), None)
        assert body is not None, f"{f} 里找不到 {fn}"
        calls = [n for n in ast.walk(body)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "needs_relocate"]
        assert calls, f"{f}::{fn} 没有调用 engine.needs_relocate —— 闸被换回自己手写的判据了"


# ---------------- 剧场版按上映那一年归档（E-30，R20） ----------------

@pytest.mark.parametrize("air,anime_q,movie_q,why", [
    ("2023-12-22", "24A", "23A", "真库 #46『间谍过家家 代号：白』：12 月首映被算成了次年"),
    ("2023-12-01", "24A", "23A", "真库 #41"),
    ("2025-12-31", "26A", "25A", "真库 #11"),
    ("2026-01-05", "26A", "26A", "1 月首映两者一致"),
    ("2026-07-05", "26C", "26A", "7 月：番剧归夏季，剧场版只看年份"),
    ("2026-11-30", "26D", "26A", "11 月同上"),
])
def test_movie_quarter_uses_the_release_year(air, anime_q, movie_q, why):
    """(E-30) `quarter_of` 的「12 月归次年冬季」对番剧是对的（季播番跨 1–3 月播完），
    对一次性上映的剧场版是错的 —— 页面上那一栏就叫『年份』，归档目录也只用年份。
    真库 70 部里有 5 部因此被归到了次年。"""
    from datetime import datetime

    from sources.parse import movie_quarter_of, quarter_of
    dt = datetime.fromisoformat(air)
    assert quarter_of(dt) == anime_q, "番剧那条规则不该被动到"
    assert movie_quarter_of(dt) == movie_q, why


def test_apply_bgm_meta_picks_the_right_quarter_per_line():
    """(E-30) 两条线取不同的键 —— 判据是"这个对象有没有 mikan_type 这一列"（Movie 独有），
    比传参更不容易漏：新增调用点时不用记得多传一个 flag。"""
    from types import SimpleNamespace

    from core import engine
    info = {"quarter": "24A", "movie_quarter": "23A", "bangumi_id": 1}

    a = SimpleNamespace(quarter="", bangumi_id=None)                       # 没有 mikan_type → 番剧
    engine.apply_bgm_meta(a, info, keep_path=False)
    assert a.quarter == "24A"

    m = SimpleNamespace(quarter="", bangumi_id=None, mikan_type="剧场版")   # 有 → 剧场版
    engine.apply_bgm_meta(m, info, keep_path=False)
    assert m.quarter == "23A", "剧场版取到了番剧那条规则算出来的季度"
