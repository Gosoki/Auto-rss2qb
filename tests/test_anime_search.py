"""番剧表搜索框（R27，对标 ani-rss §8 的第 5 条）。

这一组守的是三件**都会静默失效**的事：
① 匹配判据看的是四个字段，不是只看 UI 上那个显示名；
② 搜索词存在 `manage_page` 里，面板重建后还在（存控件上会被 refresh 抹掉）；
③ 搜到零结果时搜索框**仍然在**——否则人被锁在空列表里，连搜索词都清不掉。
"""
import pytest
from nicegui.testing import User

import config
import db
from pages.anime import _kw_hit

db.init_db()
config.load_from_db()
db.apply_configured_backend()

_MAIN = "tests/render_main.py"


class _A:
    """只带 _kw_hit 用到的四个字段的替身（不打库）。"""

    def __init__(self, display_name=None, title="", jp_name=None, bangumi_id=None):
        self.display_name, self.title = display_name, title
        self.jp_name, self.bangumi_id = jp_name, bangumi_id


@pytest.mark.parametrize("kw,hit", [
    ("葬送", True),          # 中文显示名
    ("frieren", True),       # 解析名，且大小写不敏感
    ("FRIEREN", True),       # 同上，反向大小写
    ("葬送のフリーレン", True),  # 日文原名＝磁盘上的目录名
    ("400602", True),        # bgm ID（数字字段要能被子串匹配）
    ("0060", True),          # bgm ID 的中段子串
    ("咒术", False),
    ("", True),              # 空串是任何字符串的子串——调用方负责不传空串（见页面里的 if kw）
])
def test_kw_hit_looks_at_all_four_fields(kw, hit):
    a = _A(display_name="葬送的芙莉莲", title="Sousou no Frieren",
           jp_name="葬送のフリーレン", bangumi_id=400602)
    assert _kw_hit(a, kw.lower()) is hit


def test_kw_hit_survives_all_null_fields():
    """四个字段全空的番（刚入库、还没富化）不能让搜索崩掉。"""
    assert _kw_hit(_A(), "x") is False


@pytest.mark.nicegui_main_file(_MAIN)
async def test_search_box_filters_the_table(user: User, clean_tables):
    """真的在页面上打字：命中的留下、没命中的消失，且计数行报对。"""
    from db.models import Anime

    with clean_tables.get_session() as s:
        s.add(Anime(title="Sousou no Frieren", display_name="葬送的芙莉莲",
                    jp_name="葬送のフリーレン", quarter="26C", confirmed=True))
        s.add(Anime(title="Jujutsu Kaisen", display_name="咒术回战",
                    jp_name="呪術廻戦", quarter="26C", confirmed=True))
        s.commit()

    await user.open("/?t=manage")
    await user.should_see("葬送的芙莉莲")
    await user.should_see("咒术回战")

    # 【必须走 should_see，不能打完字直接读 user.current_layout】(R27)
    # `.type()` 只把值写进控件并同步调用 on_change，而 on_change 里的 `refresh()`
    # 是**下一轮事件循环**才真正重建面板的。直接读树读到的是打字前那一版：
    # 这条用例第一版就是这么写的，于是"过滤没生效"看着像产品 bug，其实是用例自己抢跑。
    user.find(marker="anime-search").type("芙莉莲")
    await user.should_see("匹配 1 / 2 部")
    await user.should_see("葬送的芙莉莲")
    await user.should_not_see("咒术回战")


@pytest.mark.nicegui_main_file(_MAIN)
async def test_search_box_still_there_when_nothing_matches(user: User, clean_tables):
    """零结果时搜索框必须还在——这是能退出搜索态的唯一出口。"""
    from db.models import Anime

    with clean_tables.get_session() as s:
        s.add(Anime(title="Sousou no Frieren", display_name="葬送的芙莉莲",
                    quarter="26C", confirmed=True))
        s.commit()

    await user.open("/?t=manage")
    user.find(marker="anime-search").type("绝不可能存在的番名zzz")
    await user.should_see("没有匹配")
    await user.should_see(marker="anime-search")   # 框还在＝还能把搜索词清掉
    await user.should_not_see("葬送的芙莉莲")


@pytest.mark.nicegui_main_file(_MAIN)
async def test_search_goes_back_to_page_one(user: User, clean_tables, cfg):
    """在第 2 页上搜一个**结果仍然不止一页**的词，要落回第 1 页。

    【这条用例的第一版是假守卫】(R27) 第一版搜的是只命中一个季度的词，于是过滤后
    总页数变成 1、`paginate` 把越界的页码 2 夹回 1 —— 不重置页码也照样看得见结果，
    变异（把 `manage_page["n"] = 1` 换成 pass）全绿。
    要让"重置页码"这个决定本身可观察，命中集必须**仍然跨页**：此时不重置就停在第 2 页。
    """
    from nicegui import ui

    from db.models import Anime

    cfg(ANIME_PAGE_YEARS=1)     # 每页 4 个季度 → 5 个季度正好两页
    with clean_tables.get_session() as s:
        for q, name in (("26A", "斯普林第一页"), ("25D", "斯普林b"), ("25C", "斯普林c"),
                        ("25B", "斯普林d"), ("25A", "斯普林第二页")):
            s.add(Anime(title=name, display_name=name, quarter=q, confirmed=True))
        s.commit()

    await user.open("/?t=manage")
    await user.should_see("斯普林第一页")
    await user.should_not_see("斯普林第二页")

    pager = next(iter(user.find(kind=ui.pagination).elements))
    with user.client:
        pager.value = 2         # 翻到第 2 页（等价于点分页器）
    await user.should_see("斯普林第二页")
    await user.should_not_see("斯普林第一页")

    # "斯普林" 命中全部 5 部 → 过滤后仍是两页 → 不重置页码就会停在第 2 页
    user.find(marker="anime-search").type("斯普林")
    await user.should_see("匹配 5 / 5 部")
    await user.should_see("斯普林第一页")
    # 搜索态强制展开：命中的番散在各季，季度分组的行是**懒建**的（折叠着就根本没建），
    # 不强制展开的话只有第一个季度看得见，其余命中"搜到了但看不到"。
    # 25B 在本页的第 4 个分组上，默认是折叠的。
    await user.should_see("斯普林d")


def test_the_search_box_is_not_inside_the_refreshable_panel():
    """(R27 修) 搜索框必须建在 `manage_panel` **外面**。

    它的 `on_change` 最后一句是 `manage_panel.refresh()`，而 refresh 会
    `container.clear()` 把面板里的元素整批删掉再新建 —— 输入框自己也在里面的话，
    **每打完一段字停顿 300ms（debounce）焦点就没了**：用户搜「葬送」停一下
    想补「的芙莉莲」，后面一个字都打不进去，必须重新点一次输入框。

    这条用例按 AST 判"`.mark("anime-search")` 那一句在不在 `manage_panel` 的函数体里"，
    而不是看渲染结果 —— 焦点丢失在 `testing.User` 里根本观察不到（那一层没有真正的 DOM），
    所以只能钉结构。
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parent.parent.joinpath("pages/anime.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)

    def _marks_search(node) -> bool:
        return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "mark"
                   and any(isinstance(a, ast.Constant) and a.value == "anime-search"
                           for a in n.args)
                   for n in ast.walk(node))

    panels = [n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "manage_panel"]
    assert panels, "manage_panel 不见了 —— 这条守卫的前提坏了"
    assert not any(_marks_search(p) for p in panels), (
        "搜索框又建回 manage_panel 里面了 —— 它每次 refresh 都会被销毁重建，输入时焦点会丢")

    holders = [n.name for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name != "manage_panel"
               and _marks_search(n)]
    assert holders, "全文件都找不到搜索框了 —— 别是被删了"
