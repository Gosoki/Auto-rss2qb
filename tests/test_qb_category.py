"""qB 分类名可配（R27，对标 ani-rss §8 的第 4 条）。

这件事有**三条投递路径**（番剧 / 剧场版 / 手动），而本项目反复出现的第①种缺陷形状就是
"同一个决定应该落到 N 处、只落到 1 处"——三处里漏一处不会报错，只会在 qB 里
多出一个改不掉的老分类。所以这里既钉行为（配置真的流到 qB 那一下），
也钉静态形状（三个调用点都不许再写字面量）。
"""
import ast
import inspect

import pytest
from nicegui.testing import User

import config
import db

db.init_db()
config.load_from_db()
db.apply_configured_backend()

_SITES = {                       # 文件 → 该文件里 add_to_qb 应当使用的配置键
    "core/anime.py": "QB_CATEGORY_ANIME",
    "core/movies.py": "QB_CATEGORY_MOVIE",
    "core/manual.py": "QB_CATEGORY_MANUAL",
}


def _add_to_qb_calls(path):
    """AST 取出该文件里所有 `engine.add_to_qb(...)` 调用节点。

    【为什么不 grep】注释里写一句 `add_to_qb(..., config.QB_CATEGORY_ANIME, ...)`
    就能让字符串匹配的守卫变绿——本项目已经踩过一次（R25 的徽标符号守卫）。
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == "add_to_qb"]


@pytest.mark.parametrize("path,key", _SITES.items())
def test_every_delivery_path_reads_the_category_from_config(path, key):
    """三条路径的分类实参必须是 `config.<对应键>`，不许是字符串字面量。"""
    calls = _add_to_qb_calls(path)
    assert calls, f"{path} 里一个 add_to_qb 调用都没有——调用点搬走了？这条守卫已失效"
    for call in calls:
        assert len(call.args) >= 3, f"{path}:{call.lineno} add_to_qb 的分类参数不再是第 3 个位置参数"
        cat = call.args[2]
        assert not isinstance(cat, ast.Constant), \
            f"{path}:{call.lineno} 分类又写回了字面量 {cat.value!r}"
        assert (isinstance(cat, ast.Attribute) and cat.attr == key
                and isinstance(cat.value, ast.Name) and cat.value.id == "config"), \
            f"{path}:{call.lineno} 分类没读 config.{key}，读的是 {ast.dump(cat)[:80]}"


def test_the_old_module_constant_is_gone():
    """`core.manual.MANUAL_CATEGORY` 必须不存在。

    留着一个"看起来还能用"的常量，下一个人就会接着用它——于是"分类可配"这个决定
    在第三条路径上悄悄失效。删掉它，误用会当场 AttributeError。
    """
    from core import manual
    assert not hasattr(manual, "MANUAL_CATEGORY"), \
        "MANUAL_CATEGORY 还在，手动路径迟早绕回写死的分类"
    assert manual.MANUAL_TAG == "Manual", "标签不该跟着分类一起动（它带的是语义，不是标签名）"


@pytest.mark.parametrize("key,default", [
    ("QB_CATEGORY_ANIME", "AutoRSS-Anime"),
    ("QB_CATEGORY_MOVIE", "AutoRSS-Movie"),
    ("QB_CATEGORY_MANUAL", "AutoRSS-Manual"),
])
def test_defaults_are_byte_for_byte_the_old_literals(key, default):
    """老库升上来必须【行为一字不变】：默认值就是改之前那三个写死的字符串。"""
    assert config._SPEC[key] == (str, default)


async def test_the_configured_category_actually_reaches_qb(monkeypatch, tmp_path, cfg):
    """行为面：改了配置，qB 那一下收到的就是新分类。

    走手动路径——它是三条里最短、也是历史上最容易掉队的那条。
    """
    from core import engine, manual as MAN

    seen = {}

    async def fake_add_torrent(data, save_path, category, tags):
        seen["category"], seen["tags"] = category, tags
        return True

    monkeypatch.setattr(engine, "qb_is_local", lambda: False)
    monkeypatch.setattr(engine.qb, "add_torrent", fake_add_torrent)
    cfg(QB_ENABLED=True, QB_CATEGORY_MANUAL="我自己的分类")

    res = await MAN.add_manual("", b"d4:infod6:lengthi1e4:name1:a12:piece lengthi1eee", str(tmp_path))
    assert res["ok"], res
    assert seen["category"] == "我自己的分类", f"分类没跟着配置走：{seen}"
    assert seen["tags"] == "Manual", f"标签被顺手改掉了：{seen}"


@pytest.mark.nicegui_main_file("tests/render_main.py")
async def test_settings_page_exposes_all_three(user: User):
    """"可配"必须在设置页上真的看得见——三个都要有，少一个就是只兑现了一半。"""
    await user.open("/settings")
    for label in ("番剧分类", "剧场版分类", "手动下载分类"):
        await user.should_see(label)


def test_no_stray_hardcoded_category_left_anywhere():
    """全仓不许再出现写死的 AutoRSS-* 分类名（配置默认值与本用例自身除外）。"""
    import pathlib

    root = pathlib.Path(inspect.getfile(config)).parent
    offenders = []
    for p in root.rglob("*.py"):
        if ".venv" in p.parts or p.name in ("config.py", "test_qb_category.py"):
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]          # 注释里提一嘴是允许的
            if "AutoRSS-Anime" in code or "AutoRSS-Movie" in code or "AutoRSS-Manual" in code:
                offenders.append(f"{p.relative_to(root)}:{i}")
    assert not offenders, f"这些地方又把分类名写死了：{offenders}"
