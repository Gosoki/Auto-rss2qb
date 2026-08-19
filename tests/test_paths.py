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
