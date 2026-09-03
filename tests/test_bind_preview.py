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


# ---------------- (R21) 『已归档的文件搬不动』这道闸，两条线都要有 ----------------

@pytest.mark.parametrize("line", ["anime", "movie"])
async def test_binding_freezes_the_path_when_files_cannot_be_moved(line, clean_tables,
                                                                   monkeypatch, cfg):
    """已归档的行搬不动（不在 qB，`engine.relocate` 显式排除它们）——
    此时『绑定 bgm』必须冻结目录相关字段，否则新集落新目录、这批文件永久留在旧目录，
    而 UI 上没有任何按钮能把它们搬过去。

    表驱动两条线一起测：R21 之前 `_has_unmovable_files` 全仓只有一处、只在番剧侧，
    而两条线走的是**同一个** relocate、同一个归档逻辑 —— 约束在两条线上都成立，
    闸却只装在一条线上。只测一条线的用例，正是让这种漏装活下来的原因。
    """
    from datetime import datetime

    from services import enrich

    async def by_id(bid):
        return {"bangumi_id": bid, "display_name": "改名之后的名字",
                "jp_name": "新JP名", "air_date": "2020-01-01"}
    monkeypatch.setattr(enrich, "fetch_by_id", by_id)

    if line == "anime":
        from core import anime as mod
        from db.models import Anime as Row, AnimeTorrent as Tor
        with clean_tables.get_session() as s:
            r = Row(title="旧名", season=1, quarter="26C", jp_name="旧JP名", confirmed=True)
            s.add(r); s.commit(); s.refresh(r); rid = r.id
            s.add(Tor(anime_id=rid, info_hash="e" * 40, raw_title="x", episode=1,
                      status="sent", archived_at=datetime.now()))
            s.commit()
        await mod.bind_anime_bgm(rid, 4242)
        with clean_tables.get_session() as s:
            got = s.get(Row, rid)
    else:
        from core import movies as mod
        from db.models import Movie as Row, MovieTorrent as Tor
        with clean_tables.get_session() as s:
            r = Row(title="旧名", quarter="2019", jp_name="旧JP名")
            s.add(r); s.commit(); s.refresh(r); rid = r.id
            s.add(Tor(movie_id=rid, info_hash="f" * 40, raw_title="x",
                      status="sent", archived_at=datetime.now()))
            s.commit()
        await mod.bind_movie_bgm(rid, 4243)
        with clean_tables.get_session() as s:
            got = s.get(Row, rid)

    assert got.bangumi_id in (4242, 4243), "绑定本身没生效，用例的前提坏了"
    assert got.jp_name == "旧JP名", (
        f"{line} 侧：存在搬不动的已归档文件，绑定却改了建目录用的名字 —— 会造成无法补救的散目录")


def test_the_unmovable_gate_exists_on_both_lines():
    """反向：两条线的 `bind_*_bgm` 都必须把 `_has_unmovable_files` 喂给 keep_path。

    上面那条行为用例只要求"名字没变"，把 `keep_path=True` 写死也能过 ——
    那会让"没有归档文件时的正常改名"一起失效。这里钉住的是判据本身。
    """
    import ast
    from pathlib import Path
    from conftest import impl_of

    root = Path(__file__).resolve().parent.parent
    for mod, fn_name in (("core/anime.py", "bind_anime_bgm"), ("core/movies.py", "bind_movie_bgm")):
        tree = ast.parse((root / mod).read_text(encoding="utf-8"))
        # 【判据要落在真正的函数体上】(R33) 页面入口是 `wrapper → _<name>_inner` 两层，
        # 逻辑全在 _inner 里；统一经 conftest.impl_of 找，理由写在它的 docstring 里。
        fns = [impl_of(tree, fn_name)]
        assert fns[0] is not None, f"没找到 {mod}::{fn_name}"
        # 【判据必须落在同一个调用节点上】(R24 修)
        # 上一版是两个互不相干的条件的 and：① 文件里出现过 "has_unmovable_files"；
        # ② 函数体里【任意一个】 Call 带 keep_path=。把 `keep_path=_frozen` 改成
        # `keep_path=True`（`_frozen = has_unmovable_files(...)` 那一行原样留着），两个条件仍然成立 ——
        # 而这正是本条 docstring 上面点名要挡的那个变异（实测 1129 条全绿）。
        # 现在要求：keep_path 的实参**不是写死的常量**，而且它的名字能追回
        # has_unmovable_files 的返回值。
        frozen_names = {t.id for n in ast.walk(fns[0]) if isinstance(n, ast.Assign)
                        for t in n.targets if isinstance(t, ast.Name)
                        and "has_unmovable_files" in ast.dump(n.value)}
        ok = False
        for n in ast.walk(fns[0]):
            if not isinstance(n, ast.Call):
                continue
            for kw in n.keywords:
                if kw.arg != "keep_path":
                    continue
                assert not isinstance(kw.value, ast.Constant), (
                    f"{mod}::{fn_name} 的 keep_path 是写死的 {kw.value.value!r} —— "
                    "写死 True 会让『没有归档文件时的正常改名』永久失效，"
                    "而那是全项目唯一的人工纠名入口；写死 False 则冻结整个失效")
                if ("has_unmovable_files" in ast.dump(kw.value)
                        or (isinstance(kw.value, ast.Name) and kw.value.id in frozen_names)):
                    ok = True
        assert ok, f"{mod}::{fn_name} 的 keep_path 不是由 _has_unmovable_files 决定的"


# ---------------- (R21) E-13 那道闸的【行为】用例 ----------------
#
# ⚠️ 这一组存在的理由：R20 加完这道闸之后，唯一的守卫是一条 AST 名字存在性断言
# （"require_bind_confirm 的函数体里出现过 fetch_by_id / confirm / confirm_bind_merge"），
# 它不问返回值有没有被用。第 21 轮的审计把闸整条拆掉——
#   ① `if not info:` 改成 `if False:`（取不到 bgm 资料照样放行）
#   ② 第一道框的返回值直接丢弃（用户点『取消』照样绑）
#   ③ `return await confirm_bind_merge(...)` 改成调完丢弃、无条件 True
# ——900 条用例全绿。名字在不在，和它做不做事，是两回事。

@pytest.fixture
def gate(monkeypatch):
    """把 require_bind_confirm 的三个外部依赖都换成可编程的桩，并记录调用次序。"""
    from pages import layout as L
    from services import enrich

    log = {"confirm": [], "notify": [], "merge": 0}
    plan = {"info": {"display_name": "药屋少女的呢喃", "air_date": "2026-01-10",
                     "total_episodes": 24, "platform": "TV"},
            "answers": [True, True]}

    async def fake_fetch(bid):
        return plan["info"]

    async def fake_confirm(title, body="", **kw):
        log["confirm"].append((title, body))
        return plan["answers"][len(log["confirm"]) - 1] if plan["answers"] else True

    async def fake_merge(obj_id, bgm_id, kind="anime"):
        log["merge"] += 1
        return plan["answers"][1] if len(plan["answers"]) > 1 else True

    monkeypatch.setattr(enrich, "fetch_by_id", fake_fetch)
    monkeypatch.setattr(L, "confirm", fake_confirm)
    monkeypatch.setattr(L, "confirm_bind_merge", fake_merge)
    monkeypatch.setattr(L.ui, "notify", lambda msg, **kw: log["notify"].append(str(msg)))
    return L, plan, log


async def test_the_gate_refuses_when_bgm_has_no_such_subject(gate):
    """取不到 bgm 资料 → 不放行，而且【一个确认框都不弹】。

    DECISIONS E-13 白纸黑字："取不到 bgm 资料时不放行"——那说明这个 id 在 bgm 上不存在，
    或者此刻网络不通，两种情况下都不该往库里写一个我们自己都没核实过的绑定。
    """
    L, plan, log = gate
    plan["info"] = None
    assert await L.require_bind_confirm(1, 999999) is False
    assert log["confirm"] == [], "资料都没取到就弹框了"
    assert log["merge"] == 0
    assert any("999999" in m for m in log["notify"]), "没告诉用户是哪个 id 取不到"


async def test_the_gate_shows_the_subject_name_and_stops_on_cancel(gate):
    """第一道框必须把【番名】摆出来，点取消就停 —— 且不再往下走第二道闸。

    "你要绑的是《XXX》"这一句是唯一能让人当场发现【ID 记错一位】的东西：
    收紧 parse_bgm_id 的正则挡得住"粘了一段番名"，挡不住"少打一个数字"。
    """
    L, plan, log = gate
    plan["answers"] = [False]
    assert await L.require_bind_confirm(1, 4242) is False
    assert len(log["confirm"]) == 1, "点了取消还继续弹"
    title, body = log["confirm"][0]
    assert "药屋少女的呢喃" in title, f"第一道框没显示番名：{title!r}"
    assert "4242" in body, "没显示 bgm id，对不上时无从核对"
    assert log["merge"] == 0, "第一道框答了取消，却还是走到了合并确认"


async def test_the_gate_returns_what_the_merge_confirmation_says(gate):
    """第二道闸（会不会删记录）说不，整道闸就得返回 False。"""
    L, plan, log = gate
    plan["answers"] = [True, False]
    assert await L.require_bind_confirm(1, 4242) is False
    assert log["merge"] == 1


async def test_the_gate_lets_a_fully_confirmed_bind_through(gate):
    """反向：两道都点了确认就得放行 —— 否则这道闸等于把功能关死。"""
    L, plan, log = gate
    plan["answers"] = [True, True]
    assert await L.require_bind_confirm(1, 4242) is True
    assert log["merge"] == 1


# ---------------- (R21) 剧场版侧的 bind_preview 也要有行为覆盖 ----------------
#
# 上面四条行为用例全部打在 `core.anime.bind_preview` 上，`core/movies.py` 的对称实现
# **一条都没走到**。而 tests/test_single_definition.py 的 _INTENTIONAL 白名单专门为
# `bind_preview` 登记了"两条线的可删对象与告警维度不同"——也就是说项目明确知道有两份实现，
# 覆盖却只有一份：第①号形状（两处只做一处）叠第②号（验证范围小于约束范围）。
# 触发条件：用户在 /movies 的两个绑定入口填一个已被另一条 Movie 占用的 bgm id。
# 回显若恒返回空，`confirm_bind_merge` 会直接 `return True`——一句"会删掉哪条"都不显示，
# 而 `_merge_movie` 的最后一步是 `s.delete(loser)`。

def test_movie_preview_reports_the_row_that_will_be_deleted(clean_tables):
    """剧场版的回显同样要点名"哪一部会被删"、它是什么状态、下过几个版本。"""
    from core import movies as M
    from db.models import Movie, MovieTorrent

    with clean_tables.get_session() as s:
        keep = Movie(title="刚扫到的", quarter="2026")
        other = Movie(title="剧场版·总集编", display_name="总集编（已下好）",
                      quarter="2024", bangumi_id=555001, rejected=False)
        s.add(keep); s.add(other); s.commit(); s.refresh(keep); s.refresh(other)
        for i in range(2):
            s.add(MovieTorrent(movie_id=other.id, info_hash=f"{i + 200:040x}",
                               raw_title=f"v{i}", status="sent"))
        s.add(MovieTorrent(movie_id=other.id, info_hash=f"{300:040x}",
                           raw_title="v2", status="pending"))
        s.commit()
        keep_id, other_id = keep.id, other.id

    pv = M.bind_preview(keep_id, 555001)
    assert len(pv["merge"]) == 1, "剧场版回显没报出会被删的那一条"
    m = pv["merge"][0]
    assert m["id"] == other_id
    assert m["name"] == "总集编（已下好）"
    assert m["state"] == "正常"
    assert m["torrents"] == 3, "版本数少报了"
    assert m["handled"] == 2, "已下版本数少报了 —— 这正是用户判断『能不能删』的依据"
    assert m["aliases"] == 0 and m["episodes"] == [], "剧场版没有别名与集号，形状仍要与番剧侧对齐"
    assert pv["warn"] == [], "剧场版没有集号，不产生编号体系告警"


def test_movie_preview_is_silent_when_nothing_gets_deleted(clean_tables):
    """不触发合并时必须返回空 —— 否则每次正常绑定都凭空多一个弹框。"""
    from core import movies as M
    from db.models import Movie

    with clean_tables.get_session() as s:
        m = Movie(title="片", quarter="2026")
        s.add(m); s.commit(); s.refresh(m); mid = m.id
    assert M.bind_preview(mid, 999_999_999) == {"merge": [], "warn": []}


def test_movie_preview_marks_an_ignored_row_as_such(clean_tables):
    """被删的那一条若处在『已忽略』，回显要说出来 —— 合并会把这个状态传染给保留的那条。"""
    from core import movies as M
    from db.models import Movie

    with clean_tables.get_session() as s:
        keep = Movie(title="刚扫到的", quarter="2026")
        other = Movie(title="被忽略过的", quarter="2024", bangumi_id=555002, rejected=True)
        s.add(keep); s.add(other); s.commit(); s.refresh(keep)
        keep_id = keep.id
    assert M.bind_preview(keep_id, 555002)["merge"][0]["state"] == "已忽略"


@pytest.mark.parametrize("line", ["anime", "movie"])
async def test_binding_without_archived_files_really_renames(line, clean_tables,
                                                             monkeypatch, cfg):
    """反向的那一半：**没有**归档文件时，绑定必须真的改名。

    R23 只写了"有归档文件 → 冻结"这一侧，于是把 `keep_path=True` 写死也能全绿 ——
    而写死之后，`core/anime.py` 注释里那句「显式绑定是用户手动纠名的**唯一入口**」
    就永久失效：用户点『绑定 bgm』纠正认错的片名，页面弹绿色的『已绑定并识别 ✓』
    （`report["frozen"]` 仍是 False，连 R23 加的黄色警告都不会出现），片名却一个字没变。

    结构守卫钉判据，这一条钉行为 —— 两侧都要有，缺一侧就能被写死绕过。
    """
    from services import enrich

    async def by_id(bid):
        return {"bangumi_id": bid, "display_name": "改对了的名字",
                "jp_name": "新JP名", "air_date": "2020-01-01"}
    monkeypatch.setattr(enrich, "fetch_by_id", by_id)

    if line == "anime":
        from core import anime as mod
        from db.models import Anime as Row, AnimeTorrent as Tor
        with clean_tables.get_session() as s:
            r = Row(title="旧名", season=1, quarter="26C", jp_name="旧JP名", confirmed=True)
            s.add(r); s.commit(); s.refresh(r); rid = r.id
            s.add(Tor(anime_id=rid, info_hash="1" * 40, raw_title="x", episode=1,
                      status="sent", archived_at=None))     # 没有归档 → 搬得动
            s.commit()
        rep: dict = {}
        await mod.bind_anime_bgm(rid, 5151, report=rep)
        with clean_tables.get_session() as s:
            got = s.get(Row, rid)
    else:
        from core import movies as mod
        from db.models import Movie as Row, MovieTorrent as Tor
        with clean_tables.get_session() as s:
            r = Row(title="旧名", quarter="2019", jp_name="旧JP名")
            s.add(r); s.commit(); s.refresh(r); rid = r.id
            s.add(Tor(movie_id=rid, info_hash="2" * 40, raw_title="x",
                      status="sent", archived_at=None))
            s.commit()
        rep = {}
        await mod.bind_movie_bgm(rid, 5152, report=rep)
        with clean_tables.get_session() as s:
            got = s.get(Row, rid)

    assert rep.get("frozen") is False, f"{line} 侧误报了冻结"
    assert got.jp_name == "新JP名", (
        f"{line} 侧：没有归档文件却没改名 —— 全项目唯一的人工纠名入口失效了")
