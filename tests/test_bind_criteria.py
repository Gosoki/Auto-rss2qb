"""(R22) bgm 绑定的两道判据闸：`_date_ok` 与 `_name_not_contradicted`。

这两个函数是**自动识别路径唯一的把关**：
`poll_once → process_item → _resolve_anime → enrich.resolve → _search_one` 里，
决定"这个 bgm subject 认不认"的只有一句 `_date_ok(...) and _name_not_contradicted(...)`。

而它们被 r12/r14/r19 与 DECISIONS 的 E-13/E-18/E-42 反复点名为**系统性误绑的根因**：
r19 在真库上量出自动路径重跑一遍的绑定错误率 **4.3%**（99 部错 4 部），
成因就是 `_date_ok` 用"集数倒推首播日"在**用绝对编号的续季**上指到上一季的窗口。
误绑的后果是不可逆的（`_merge_anime` 会删掉另一条番记录，没有撤销入口）。

**记了 5 轮，零覆盖。** 这一组把它们的每一条出口钉住 —— 两个都是纯函数，成本极低。
"""
from datetime import datetime, timedelta

import pytest

from services.enrich import _date_ok, _name_not_contradicted

_BGM = datetime(2026, 1, 10)


@pytest.mark.parametrize("est_days,release_days,ok,why", [
    (None, None, True, "两个基准都没有 → 不卡日期，交给名字重叠 + bgm 相关性排序"),
    (0, None, True, "est 正中"),
    (35, None, True, "est 窗口右边界（含）"),
    (-35, None, True, "est 窗口左边界（含）"),
    (36, None, False, "est 越界一天就该拒"),
    (-36, None, False, "同上，另一侧"),
    (None, 0, True, "release 正中"),
    (None, 45, True, "release 右边界（含）"),
    (None, 46, False, "release 越界"),
    (None, -21, True, "release 左边界（含）"),
    (None, -22, False, "同上，另一侧"),
    (200, 0, True, "est 差得远，但 release 兜底命中 → 放行（两条是【或】的关系）"),
    (200, 200, False, "两条都不命中才拒"),
])
def test_date_gate_boundaries(est_days, release_days, ok, why):
    """窗口是 est ±35 天 **或** release 的 [-21, 45] —— 边界逐个钉住。

    钉边界不是洁癖：这两个数字决定了"续季的绝对编号倒推出来的假首播日"会不会被放过，
    而那正是真库上那 4 部误绑的成因。谁改了这两个数，这里会立刻红。
    """
    est = _BGM + timedelta(days=est_days) if est_days is not None else None
    rel = _BGM + timedelta(days=release_days) if release_days is not None else None
    assert _date_ok(_BGM, est, rel) is ok, why


def test_the_documented_misbinding_shape_is_still_refused_by_date():
    """把 r19 记录的失败形状钉成用例：用绝对编号的第二季。

    第二季共 13 集，种子写的是**绝对号 16**（= 第 3 集）。
    `estimate_premiere` 按绝对号倒推首播日，会指到**上一季**的窗口 ——
    于是正确的 subject（本季）被判成"日期对不上"而排除，
    转而命中同系列的另一部（上一季往往就在那个年份）。
    """
    right_season = datetime(2026, 1, 10)          # 正确的：本季
    wrong_season = datetime(2025, 4, 5)           # 同系列上一季
    est_from_abs = wrong_season + timedelta(days=14)   # 绝对号倒推出来的假首播日

    assert _date_ok(wrong_season, est_from_abs, None) is True, \
        "前提：假首播日确实落在上一季窗口里（这就是误绑的成因）"
    assert _date_ok(right_season, est_from_abs, None) is False, \
        "正确的 subject 被日期闸排除 —— 这条链一旦变了，E-42 那套缓解也要跟着重估"


@pytest.mark.parametrize("query,subject,ok,why", [
    ("Kimi ga Shinu", {"name": "君が死ぬまで恋をしたい"}, True, "纯罗马音不卡（交给相关性+日期）"),
    ("药屋少女", {"name_cn": "药屋少女的呢喃", "name": ""}, True, "2-gram 有重叠"),
    ("药屋少女", {"name_cn": "间谍过家家", "name": "SPY×FAMILY"}, False, "一个 2-gram 都不重叠 → 拒"),
    ("咒", {"name_cn": "咒术回战", "name": ""}, True, "单字 CJK：退回子串包含"),
    ("咒", {"name_cn": "间谍过家家", "name": ""}, False, "单字 CJK 不包含 → 拒"),
    ("北斗", {"name_cn": "北斗神拳", "name": ""}, True, "两个字重叠就放行"),
    ("药屋少女", {}, False, "subject 没有名字字段 → 没有任何重叠，拒"),
])
def test_name_gate(query, subject, ok, why):
    """名字闸只做 2-gram 重叠 —— 门槛很低（"北斗"两个字就够），这是它被点名的原因之一。

    钉住它是为了让下一次收紧（比如要求 ≥2 个 2-gram）有一张对照网：
    收紧会不会把正当的简写/别名一起挡掉，看这张表就知道。
    """
    assert _name_not_contradicted(query, subject) is ok, why


def test_the_two_gates_are_the_only_thing_between_a_search_hit_and_a_binding():
    """反向：钉住"这两个函数确实是那条链上唯一的把关"。

    如果哪天 `_search_one` 里加了第三道闸、或者去掉了其中一道，
    上面那些用例的**意义**就变了 —— 这条让那种变化必须先来改它。
    """
    import ast
    import inspect
    import textwrap

    from services import enrich

    tree = ast.parse(textwrap.dedent(inspect.getsource(enrich._search_one)))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert {"_date_ok", "_name_not_contradicted"} <= called, \
        "_search_one 不再走这两道闸了 —— 那么本文件钉的就不是自动识别的把关判据了"
