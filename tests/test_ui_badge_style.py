"""(R14/R16) 控件的视觉形态只由【一处】决定 —— 徽标、输入框、按钮共用同一条纪律。

全站 71 个 `ui.badge` 是唯一的标签机制（没有 ui.chip、没有自绘的带背景 label）。
它的形态历史上被改过两轮，每轮都只改了一半：
  · 第一轮：调用点各写各的字号，同一个『待确认』在同一屏出现 12px / 14px 两种大小
    → 全局定了 `.q-badge{font-size:14px}`，但**没定行高**；
  · 于是仍有 31 个徽标带着 Tailwind 的 `text-sm`，而 `text-sm` 同时设 font-size(14px)
    与 line-height(20px) —— 字号一样了，框高却是 24px vs 18px，同屏两种高度。
这条用例把"形态只由全局 CSS 决定"钉死，挡的是第三轮再来一次。
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# 会改变徽标字号/行高的 Tailwind 工具类。调用点一个都不该出现——它们只会让全局规则失效一半。
_METRIC_CLASSES = re.compile(r"\b(text-(?:xs|sm|base|lg|xl|\d+xl)|leading-\w+)\b")


def _badge_chains(src: str):
    """产出源码里每一段完整的 `ui.badge(...)....(...)` 链式调用（含跨行写法）。

    必须按括号配平来切，不能按行 grep：31 处里有 12 处是跨行写的，
    第一版修复正是因为正则不跨行而漏掉了它们（同一件事又只改了一半）。
    """
    pos = 0
    while True:
        i = src.find("ui.badge(", pos)
        if i < 0:
            return
        j = i + len("ui.badge(")
        depth = 1
        while j < len(src) and depth:
            depth += (src[j] == "(") - (src[j] == ")")
            j += 1
        k = j
        while True:
            m = re.match(r"\s*\.\w+\(", src[k:])
            if not m:
                break
            k2 = k + m.end()
            d = 1
            while k2 < len(src) and d:
                d += (src[k2] == "(") - (src[k2] == ")")
                k2 += 1
            k = k2
        yield i, src[i:k]
        pos = k


def _badge_vars(src: str) -> set:
    """收集被赋值成 ui.badge(...) 的局部变量名（`b = ui.badge(...)` 这种分语句写法）。"""
    import ast
    names = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        # 剥掉链式调用，找到最里层的 ui.badge(...)
        call = node.value
        while isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) \
                and isinstance(call.func.value, ast.Call):
            call = call.func.value
        f = call.func if isinstance(call, ast.Call) else None
        if isinstance(f, ast.Attribute) and f.attr == "badge":
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


def test_no_badge_sets_its_own_metrics():
    """调用点不许再写 text-sm / leading-* —— 字号与行高由全局 CSS 一处定。

    扫两种写法：① 链式 `ui.badge(...).classes("text-sm")`（含跨行）；
    ② 分语句 `b = ui.badge(...)` 之后 `b.classes("text-sm")`。

    【这道扫描的已知边界，别当成完备保证】它是静态的，绕得过去的写法至少有：
    把类名拼起来（`"text-" + "sm"`）、从变量/常量里取、或者把 badge 传进别的函数再加类。
    它挡的是"顺手写一个 text-sm"这种最常见的形态，不是恶意绕过。真正的兜底是
    全局 CSS 里的 !important —— 即便漏一个，视觉上也不会分裂。
    """
    offenders = []
    for path in sorted((_ROOT / "pages").glob("*.py")):
        src = path.read_text(encoding="utf8")
        for off, chain in _badge_chains(src):
            hit = _METRIC_CLASSES.findall(chain)
            if hit:
                offenders.append(f"{path.name}:{src[:off].count(chr(10)) + 1} 带 {hit}（链式）")
        for var in _badge_vars(src):
            for m in re.finditer(rf"\b{re.escape(var)}\.classes\(\s*\"([^\"]*)\"", src):
                hit = _METRIC_CLASSES.findall(m.group(1))
                if hit:
                    offenders.append(
                        f"{path.name}:{src[:m.start()].count(chr(10)) + 1} 带 {hit}（分语句 {var}）")
    assert not offenders, (
        "这些徽标自己设了字号/行高，会让全局规则只生效一半：\n  " + "\n  ".join(offenders))


def test_badges_exist_and_are_the_only_tag_widget():
    """前一条若因为『一个 ui.badge 都没有』而空过，就毫无意义——这里钉住前提。

    顺带守住"标签只有一种实现"：出现 ui.chip 或自绘的带背景 label 就是新的割裂来源。
    """
    total = 0
    for path in (_ROOT / "pages").glob("*.py"):
        src = path.read_text(encoding="utf8")
        total += sum(1 for _ in _badge_chains(src))
        assert "ui.chip(" not in src, f"{path.name} 引入了第二种标签控件 ui.chip"
    assert total >= 60, f"只找到 {total} 个 ui.badge，扫描逻辑可能失效了"


def test_global_badge_css_defines_size_and_centering():
    """全局规则必须【同时】给出字号、行高与居中方式——三者是同一个决定的三份。

    断言的是真正发进 <head> 的那段 CSS 本身（_HEAD_BADGE_CSS 就是产物，不是它的替身）。
    """
    from pages.layout import _HEAD_BADGE_CSS
    css = _HEAD_BADGE_CSS.replace(" ", "")
    assert "font-size:14px!important" in css, (
        "字号没定，或漏了 !important。注意理由：本段在 @layer overrides 里，而 Tailwind 由 "
        "tailwindcss.min.js 运行时注入、**不属于任何层**；规范里普通声明是『无层级 > 有层级』，"
        "所以调用点顺手写一个 text-sm 就能顶掉它。（Quasar 反而压得过——它在 layer(quasar)，"
        "排在 overrides 之前；层是在 @import 那侧指定的，grep 它的 CSS 文件看不到。）")
    assert "line-height:1.45!important" in css, "行高没定或漏了 !important——只定字号就是把同一件事改一半"
    assert "align-items:center!important" in css and "inline-flex!important" in css, (
        "缺垂直居中：Quasar 原样式没有 display 规则、靠 vertical-align:baseline 定位，"
        "而中日文字形的 ascent+descent 超过 1em，line-height 接近 1 时字会偏低、上下留白不等")


def test_the_badge_css_is_actually_injected_into_head():
    """断言常量的内容只证明"字符串写对了"，不证明"它被送进了页面"。

    实测过：把 `pages/layout.py` 里那句 `ui.add_head_html(_HEAD_BADGE_CSS)` 整行删掉，
    全套用例照样全绿 —— 这就是"约束的作用域比验证的作用域大"。

    但只查"有没有这句调用"仍然不够：把它挪进一个从没被调用的函数、或包进 `if False:`，
    第一版守卫照样绿（实测）。所以这里额外要求它**落在 frame() 的函数体里**——
    frame() 是每个页面都会套的那层外壳，是全站唯一"一定会执行"的注入点。
    """
    import ast
    src = (_ROOT / "pages" / "layout.py").read_text(encoding="utf8")
    tree = ast.parse(src)

    frame = next((n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "frame"),
                 None)
    assert frame is not None, "pages/layout.py 里没有 frame()——这条守卫的前提没了"

    parent = {}
    for node in ast.walk(frame):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    def _alive(call):
        """这条调用不在恒假分支里。"""
        cur = call
        while cur in parent:
            cur = parent[cur]
            if isinstance(cur, ast.If) and isinstance(cur.test, ast.Constant) and not cur.test.value:
                return False
        return True

    injected = {
        n.args[0].id
        for n in ast.walk(frame)
        if isinstance(n, ast.Call)
        and ((isinstance(n.func, ast.Attribute) and n.func.attr == "add_head_html")
             or (isinstance(n.func, ast.Name) and n.func.id == "add_head_html"))
        and n.args and isinstance(n.args[0], ast.Name) and _alive(n)
    }
    assert "_HEAD_BADGE_CSS" in injected, (
        f"徽标 CSS 没有在 frame() 里被 add_head_html 注入（frame 里实际注入的是 {sorted(injected)}）——"
        "常量写得再对，页面上也不会生效；挪进别的函数或恒假分支同样不算数")


# ---------------- (R16) 输入类控件：形态同样只由全局默认值决定 ----------------

# pages/layout.py 顶上给这些控件定了全局默认 props；调用点再写一遍就是冗余。
_WIDGET_DEFAULTS = {
    "input": {"dense", "outlined"},
    "number": {"dense", "outlined"},
    "textarea": {"dense", "outlined"},
    "select": {"dense", "outlined"},
    "switch": {"dense"},
}


def _widget_chains(src: str, widget: str):
    """产出 `ui.<widget>(...)....(...)` 的完整链式调用（含跨行）。"""
    pos = 0
    while True:
        i = src.find(f"ui.{widget}(", pos)
        if i < 0:
            return
        j = i + len(f"ui.{widget}(")
        depth = 1
        while j < len(src) and depth:
            depth += (src[j] == "(") - (src[j] == ")")
            j += 1
        k = j
        while True:
            m = re.match(r"\s*\.\w+\(", src[k:])
            if not m:
                break
            k2 = k + m.end()
            d = 1
            while k2 < len(src) and d:
                d += (src[k2] == "(") - (src[k2] == ")")
                k2 += 1
            k = k2
        yield i, src[i:k]
        pos = k


def test_no_input_widget_repeats_the_global_form():
    """调用点不许再写 dense / outlined —— 那是 pages/layout.py 的全局默认值。

    这是徽标那条纪律的另一半。项目当初把输入类控件的形态定成了 default_props，
    注释写着「此前是 38 处逐字重复，于是漏一处就多一种样子（movies 的『扫描间隔』输入框
    就是全站唯一一个下划线样式的框）」——但**只加了默认值，没清掉那 45 处重复**。

    留着它们不是"多余但无害"：将来改全局默认（比如换成 filled），这 45 处会把它
    **覆盖回去**，于是只有一部分控件跟着变——正是那条注释想根除的形状。
    清理已用"改前/改后逐页对比渲染结果"验证过：7 个页面的 props 块逐字相同。
    """
    offenders = []
    for path in sorted((_ROOT / "pages").rglob("*.py")):
        src = path.read_text(encoding="utf8")
        for widget, defaults in _WIDGET_DEFAULTS.items():
            for off, chain in _widget_chains(src, widget):
                for m in re.finditer(r'\.props\(\s*f?"([^"]*)"', chain):
                    dup = defaults & set(m.group(1).split())
                    if dup:
                        line = src[:off].count(chr(10)) + 1
                        offenders.append(f"{path.name}:{line} ui.{widget} 重复写了 {sorted(dup)}")
    assert not offenders, (
        "这些控件重复了全局默认 props，会让日后改默认值只生效一半：\n  " + "\n  ".join(offenders))


def test_the_global_widget_defaults_are_actually_set():
    """反向：上一条依赖"全局默认值存在"这个前提，前提没了它就变成一条空断言。"""
    src = (_ROOT / "pages" / "layout.py").read_text(encoding="utf8")
    for widget, defaults in _WIDGET_DEFAULTS.items():
        m = re.search(rf'ui\.{widget}\.default_props\(\s*"([^"]*)"', src)
        assert m, f"ui.{widget} 没有设全局默认 props"
        got = set(m.group(1).split())
        assert defaults <= got, f"ui.{widget} 的全局默认少了 {sorted(defaults - got)}"
