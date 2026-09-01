"""(R14) 『绑定 bgm』的绑定前回显。

这个按钮在用户眼里只是"改个 ID"，实际可能【删掉另一条番记录】——bind_anime_bgm 末尾的
身份守卫会把占用同一个 bgm_id 的番 _merge_anime 过来，而合并的最后一步是 s.delete(loser)，
没有撤销入口。真实案例（Re:Zero）：LoliHouse 用全系列绝对编号 78/79/80 建了一条番、
ANi 用季内编号 12/13/14 建了另一条，两条其实是同一批集；把前者绑到后者的 bgm 上会
删掉那条『追番中、已下 3 集』的记录，且合并后 3 集内容产生 6 个去重键、每集下两份。
"""
import ast
from pathlib import Path

import pytest

from core import anime as A
from db.models import Anime, AnimeTorrent, AnimeAlias

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def two_rows(clean_tables):
    """同一部番裂成两条：#1 用绝对编号、#2 用季内编号，且 #2 已占着目标 bgm_id。"""
    with clean_tables.get_session() as s:
        a1 = Anime(title="Re:从零开始的异世界生活", season=1, quarter="16B",
                   bangumi_id=140001, air_date="2016-04-03", total_episodes=26,
                   confirmed=False, rejected=True)
        a2 = Anime(title="Re：从零开始的异世界生活", display_name="第四季 夺还篇", season=4,
                   quarter="26C", bangumi_id=633836, air_date="2026-08-12",
                   total_episodes=8, confirmed=True, rejected=False)
        s.add(a1); s.add(a2); s.commit(); s.refresh(a1); s.refresh(a2)
        s.add(AnimeAlias(title="Re:从零开始的异世界生活", season=1, anime_id=a1.id))
        s.add(AnimeAlias(title="Re：从零开始的异世界生活", season=4, anime_id=a2.id))
        for i, ep in enumerate((78.0, 79.0, 80.0)):     # 绝对编号，全部待下
            s.add(AnimeTorrent(info_hash=f"{i:040x}", anime_id=a1.id, source="LoliHouse",
                               site="nyaa", raw_title=f"x - {int(ep)}", season=1,
                               episode=ep, status="pending"))
        for i, ep in enumerate((12.0, 13.0, 14.0)):     # 季内编号，全部已下
            s.add(AnimeTorrent(info_hash=f"{i + 100:040x}", anime_id=a2.id, source="ANi",
                               site="nyaa", raw_title=f"y S04E{int(ep)}", season=4,
                               episode=ep, status="sent"))
        s.commit()
        return a1.id, a2.id


def test_preview_reports_the_row_that_will_be_deleted(clean_tables, two_rows):
    """回显必须点名"哪一条番会被删"，以及它的状态与已下集数——这是不可逆的那一步。"""
    a1, a2 = two_rows
    pv = A.bind_preview(a1, 633836)
    assert len(pv["merge"]) == 1
    m = pv["merge"][0]
    assert m["id"] == a2
    assert m["state"] == "追番中", "被删的是一条正在追的番，必须让用户看见"
    assert m["torrents"] == 3 and m["handled"] == 3, "3 条种子全部已下，回显不能少报"
    assert m["aliases"] == 1


def test_preview_warns_when_episode_numbering_is_incompatible(clean_tables, two_rows):
    """两边集号一集都不重合 = 多半在用不同的编号体系，合并后每集会下两份。"""
    a1, _ = two_rows
    warn = " ".join(A.bind_preview(a1, 633836)["warn"])
    assert "没有一集重合" in warn
    assert "78–80" in warn and "12–14" in warn, "要把两边的区间摆出来，不能只说『可能有问题』"
    assert "6 个去重键" in warn


def test_no_merge_no_noise(clean_tables, two_rows):
    """不触发合并的绑定是绝大多数——它们必须一次点击完成，不能凭空多一个弹框。"""
    a1, _ = two_rows
    assert A.bind_preview(a1, 999_999_999) == {"merge": [], "warn": []}


def test_overlapping_episodes_do_not_warn(clean_tables):
    """集号有交集 = 至少共用同一套编号，合并是安全的，不该报警。

    这条守住"告警别扩得太宽"：一旦对正常的重绑也弹警告，用户就会开始无脑点『仍然绑定』，
    真正危险的那次也一起被点过去了。
    """
    with clean_tables.get_session() as s:
        a1 = Anime(title="甲", season=1, bangumi_id=111)
        a2 = Anime(title="乙", season=1, bangumi_id=222)
        s.add(a1); s.add(a2); s.commit(); s.refresh(a1); s.refresh(a2)
        for i, (aid, ep) in enumerate([(a1.id, 5.0), (a1.id, 6.0), (a2.id, 6.0), (a2.id, 7.0)]):
            s.add(AnimeTorrent(info_hash=f"{i:040x}", anime_id=aid, source="X", site="nyaa",
                               raw_title="z", season=1, episode=ep, status="pending"))
        s.commit()
        i1, i2 = a1.id, a2.id
    pv = A.bind_preview(i1, 222)
    assert pv["merge"] and pv["merge"][0]["id"] == i2, "合并本身要照常回显"
    assert pv["warn"] == [], "第 6 集两边都有 → 同一套编号 → 不该报警"


def test_every_bind_call_site_has_the_gate():
    """**广度不变量**：pages/ 里每一处调 bind_anime_bgm / bind_movie_bgm 的地方
    都必须先过 require_bind_confirm（它内含两道闸：先回显要绑到哪一部，再回显会不会删记录）。

    这条不写成行为断言是有意的：要挡的正是"将来有人加了第三个绑定入口却忘了加闸"，
    而"不存在第三个没加闸的入口"只能靠静态扫描回答。本项目最常见的缺陷形状就是
    同一件事只改了一半——bind_anime_bgm 的注释点名了两个调用点，第一版修复也确实只改了详情页那个。
    """
    offenders = []
    # 【递归 glob】pages 现在没有子包，但新建一个 pages/sub/binder.py 就能绕过 *.py ——
    # 实测过。守卫的扫描面必须比"当前的目录结构"更宽。
    for path in sorted((_ROOT / "pages").rglob("*.py")):
        src = path.read_text(encoding="utf8")
        tree = ast.parse(src)
        parent = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[child] = node

        def _gate_pos(node):
            """这个函数体里 require_bind_confirm 的调用位置（取最靠前的一个）；没有则 None。"""
            best = None
            for n in ast.walk(node):
                if not isinstance(n, ast.Call):
                    continue
                f = n.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name != "require_bind_confirm":
                    continue
                # 【闸不能写在恒假分支里】实测 `if False:` 包住闸能骗过第一版守卫
                anc, dead = n, False
                while anc in parent:
                    anc = parent[anc]
                    if isinstance(anc, ast.If) and isinstance(anc.test, ast.Constant) \
                            and not anc.test.value:
                        dead = True
                        break
                if dead:
                    continue
                best = n.lineno if best is None else min(best, n.lineno)
            return best

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            # 【裸函数名也要认】`from core.anime import bind_anime_bgm` 之后直接调用时
            # node.func 是 ast.Name 而不是 ast.Attribute，第一版只认后者、整条放行。
            called = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if called not in ("bind_anime_bgm", "bind_movie_bgm"):
                continue
            cur, gated, where = node, False, f"{path.relative_to(_ROOT)}:{node.lineno}"
            while cur in parent:
                cur = parent[cur]
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    g = _gate_pos(cur)
                    # 【闸必须在 bind 之前】写在后面等于先删了再问，实测能骗过第一版
                    if g is not None and g < node.lineno:
                        gated = True
                        break
            if not gated:
                offenders.append(where)
    assert not offenders, f"这些绑定入口没有过回显闸：{offenders}"


# ---------------- 绑定前先回显"要绑到哪一部"（E-13，R20） ----------------

@pytest.mark.parametrize("text,want,why", [
    ("https://bgm.tv/subject/12345", 12345, "标准链接"),
    ("bgm.tv/subject/999", 999, "不带协议"),
    ("12345", 12345, "整串就是数字"),
    ("  12345  ", 12345, "两端空白无所谓"),
    # 【兜底收紧】原来是 re.search(r"(\d+)")——从任意文本里抠第一串数字
    ("第2季 1080p", None, "粘了一段番名 → 原来会得到 2，绑到一个完全不相干的 subject"),
    ("某番名 2026", None, "同上"),
    ("https://mikanani.me/Home/Bangumi/3384", None, "粘了 Mikan 链接 → 原来会得到 3384"),
    ("", None, "空"),
])
def test_parse_bgm_id_only_takes_a_bare_number(text, want, why):
    """(E-13) 兜底要求【整串就是数字】。

    原来从任意文本里抠第一串数字，于是粘一段番名/一条 Mikan 链接都会得到一个"合法"的 id，
    而绑定末尾的身份守卫会【删掉】另一条记录、没有撤销入口。
    """
    from core.manual import parse_bgm_id
    assert parse_bgm_id(text) == want, why


def test_the_gate_shows_the_target_name_before_writing():
    """(E-13) 收紧正则挡不住【记错一位】—— 那只能靠绑定前回显取回的番名。

    id 错一位取回的多半是一部完全不相干的作品，名字一摆出来就看出来了。
    取不到 bgm 资料时不放行：那说明 id 不存在或此刻网络不通，
    两种情况下都不该往库里写一个我们自己都没核实过的绑定。
    """
    import ast
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent.joinpath("pages/layout.py").read_text(
        encoding="utf8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "require_bind_confirm")
    calls = [n.func.attr for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    names = [n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "fetch_by_id" in calls, "没有去 bgm 取回资料，就没法回显『你要绑的是哪一部』"
    assert "confirm_bind_merge" in names, "没有接上原来那道『会不会删记录』的闸"
    assert names.count("confirm") >= 1, "没有回显确认框"
