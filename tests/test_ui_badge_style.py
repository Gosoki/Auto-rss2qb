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
        "字号没定。至于 !important —— 它在【今天】其实是多余的：本段在 @layer overrides 里，"
        "而 Tailwind 的工具类进 @layer utilities（tailwindcss.min.js 内嵌的样式表逐字写着 "
        "`@import './utilities.css' layer(utilities)`），NiceGUI 模板首先声明的层序把 "
        "utilities 排在 overrides 【之前】，所以普通声明本来就压得过它；Quasar 在 layer(quasar)，"
        "更早，同样压得过。这里保留 !important 只是为了挡住万一的行内样式，"
        "别再拿『否则压不过 Tailwind/Quasar』当理由——那两句话都被实测证伪过。"
        "详见 pages/layout.py 里那段带 ①②③④ 的说明。")
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
    # 【button 也要管】它是唯一还有 85 个调用点的控件，而 no-caps 已是全局默认
    #（pages/layout.py 的 ui.button.default_props）。漏掉它的后果不是样式错，
    # 是角色词表里同一个角色出现"带 no-caps"和"不带"两个名字。
    "button": {"no-caps"},
    # 【expansion 有两档，默认是紧凑那一档】(R22) 全站 13 处原本 9 处写 dense、4 处不写，
    # 两组之间没有任何成文规则 —— 那 4 处恰好是列表的【季度·年份分组头】（没有外框卡片，
    # 折叠头本身就是分组之间唯一的视觉分隔）。两档各有其用，但必须是成文的两档，不能是漂移。
    # 默认取多数那一档；主结构分组用 `.props(remove="dense")` 显式退出。
    "expansion": {"dense"},
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


# ---------------- 按钮的角色词表（R18） ----------------
#
# 全站按钮只允许这几种 props 组合，每一种对应一个【角色】。
# 这不是审美洁癖：同一个角色写成两种样子时，用户学到的"这种按钮是主操作"的规律就断了，
# 而断在哪儿是随机的（取决于哪个文件先被改）。徽标那一节踩过一模一样的坑
# （同一个『待确认』在同一屏三种大小），代价是花了两轮才收干净。
#
# 【为什么用白名单而不是"检查有没有 unelevated"这类规则】角色是语义，规则是形式。
# 白名单逼着新增样式的人回答一句"它是什么角色"，而规则只会被绕过。
# 【键 = props + 尺寸档】尺寸不并进来，词表就说不全一个角色：R18 立的论是"同一个角色出现
# 两种尺寸"，而 .btn-sm 是调用点自己挂的 class、根本不在键里，于是那个症状原样还在
#（实测当时 3 个 props 组合各有两种尺寸）。键写成 `<props>` 或 `<props> +btn-sm`。
_BUTTON_ROLES: dict[str, str] = {
    # 语法：{形态} {round} {dense} {color}[ +btn-sm]
    #   形态  flat=无底色 / outline=描边 / unelevated=实心无阴影
    #   dense 收内边距（行内、密集行用）
    #   +btn-sm 是尺寸档：12px；不带就是默认 14px
    #   no-caps【不写】：pages/layout.py 的 ui.button.default_props("no-caps") 已是全局默认，
    #     调用点再写一遍只会让同一个角色出现两个名字。_WIDGET_DEFAULTS 里登记了 button 守这条。
    #
    # 排版上只有两条硬规矩，其余交给这张表：
    #   ① 一屏里 unelevated 最多一个（主操作）
    #   ② 同一行里的按钮尺寸档必须一致

    # ── 实心：主操作 ──────────────────────────────────────────────
    "unelevated color=primary": "主操作（确认下载 / 保存 / 绑定 / 恢复订阅 / 立刻备份）",
    # 【这一档是有意的，不是漏改】它与一个输入框同行，14px 的行高会比输入框胖一圈；
    # 不写 dense 是因为 dense 收的是内边距，而这里要的是"跟着行走"。全站只有这一处。
    "unelevated color=primary +btn-sm": ("与输入框同行的主操作（设置页『应用开始使用日过滤』）；"
                                        "以及点选/切换按钮的【已选中】档（剧场版季度点选）"),
    "unelevated dense color=primary +btn-sm": "行内主操作（详情页元操作行的『恢复订阅』『继续订阅』）",

    # ── 描边：会展开下一层的触发器 ────────────────────────────────
    "outline color=primary +btn-sm": "页头行里的触发器（『补下全部』『立刻刷新』『重新识别』下拉）",

    # ── 无底色：取消与次级 ────────────────────────────────────────
    "flat": "取消 / 关闭，以及与主操作同一档的次级按钮（『下载该源』『补齐该源』）",
    "flat color=grey": "次级具名操作（忽略 / 重试识别 / 删除源）",
    # 【别读成"只验证不改状态"】用它的 6 个按钮里有 3 个会改状态（创建数据库、两个方向的迁移）。
    # 破坏性在本项目里由【确认框】把关，不由按钮权重表达 —— 权重表达的是"这是不是这一屏的主线"。
    "flat color=primary": "次级操作（测试连接 / 发送测试通知 / 创建数据库 / 对等操作的另一个方向）",
    "flat color=negative": "危险操作（删除文件）",
    "flat dense +btn-sm": "行内次级操作（详情页元操作行 / 种子行）",
    "flat dense color=primary +btn-sm": "行内次级操作，主色（重新下载 / 恢复 / 下载这一条）",
    "flat dense color=grey +btn-sm": "行内次级操作，灰（忽略 / 排除）",
    "flat dense color=negative +btn-sm": "行内危险操作（删这一集 / 这一版本的文件）",

    # ── 图标按钮 ─────────────────────────────────────────────────
    "flat round dense": "面板级图标按钮（关掉整个详情面板）—— 与行内那档有意分两级",
    "flat round dense color=primary +btn-sm": "行内图标按钮，主色（就地编辑 / 复制命令）",
    "flat round dense color=grey +btn-sm": "行内图标按钮，灰（移除附件 / ? 提示）",
    "flat round dense color=white": "深色顶栏上的图标按钮",

    # ── R21 补登记：以前【采集不到】所以从没被检查过的四种 ───────────
    # （`_button_calls` 原来只走链式 + 只认 ast.Constant，于是 f-string props 与
    #   "先赋值再 .props()" 两种写法整个不可见。补采之后这几个才浮出来。）
    "color={} unelevated": "确认框的『确定』按钮 —— 颜色随危险度动态给（primary / negative）",
    "icon={}": "给上面那个确定按钮补图标；附加 props，不单独构成一个角色",
    "loading": "忙碌态；busy_action 期间挂上，附加 props，不单独构成一个角色",
    "outline color=grey +btn-sm": "点选/切换按钮的【未选中】档（剧场版季度点选）",
}


def _button_calls():
    r"""(文件名, 行号, props 字符串, 该链上全部 .classes() 实参) —— 每个 `ui.button(...)` 链一条。

    【必须用 AST，不能用正则】第一版用的是
    `ui\.button\(.{0,200}?\)[^\n]*(?:\n[^\n]*){0,2}?\.props\(\s*"([^"]*)"` + re.S，
    那个惰性量词在回溯次序上排在贪婪的 `[^\n]*` 之后，遇到跨行写法时会跳到【下一行】去找
    `.props(`，把本行的 props 整段吞掉。实测：pages/ 下共 85 处链式 `ui.button(...).props("…")`，
    正则只看见 69 处真的 + 2 处幻影（把 N+1 行的 props 记在第 N 行的按钮上）。
    也就是说白名单声称"全站按钮只允许这几种组合"，实际有 16 处从来没被检查过——
    一条自称覆盖全站的守卫，覆盖不到的恰好是最容易跑偏的跨行写法。

    实现要点：`ast.walk` 会把 `ui.button(..).props(..).classes(..).tooltip(..)` 这条链上的
    【每一层】Call 都访问一遍，所以按链根的 id 去重、只保留收集得最全的那一条（最外层）。
    """
    import ast
    by_root: dict = {}
    extra: list = []          # 分语句写法（b = ui.button(...) 之后 b.props("…")）
    assign_cls: dict = {}     # (文件, 变量) → 赋值那条链上写下的 classes
    for path in sorted((_ROOT / "pages").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf8"))
        # 【分语句写法也要采】(R21) `_button_calls` 原本只走链式调用，于是两种写法整个消失：
        #   ① `b = ui.button(name)` 之后在别处 `b.props("outline color=blue-grey")`
        #      —— 链根上一个 props 都没有，函数连一行都不 append（pages/movies.py 的季度点选）；
        #   ② f-string props：`.props(f"color={ok_color} unelevated")`
        #      —— 实参不是 ast.Constant，被静默跳过（layout.py 里**全站所有确认框**的确定按钮）。
        # 实测 pages/ 下 `ui.button(` 共 90 处，旧实现只采到 88 —— 而漏掉的两个恰好都是
        # 词表里没登记的样式。一条自称"覆盖全站"的守卫，覆盖不到的正是最容易跑偏的写法。
        btn_vars = _assigned_button_vars(tree)
        for _v, _c in btn_vars.items():
            assign_cls[(path.name, _v)] = _c
        if btn_vars:
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("props", "classes")):
                    continue
                # 【顺着链走到根】`b.props(remove="outline").props("unelevated color=primary")`
                # 里第二个 props 的 func.value 是一个 Call，不是 Name —— 只认 Name 会漏掉它，
                # 而那正是 pages/movies.py 季度点选按钮的写法（两档样式一档都采不到）。
                root = node.func.value
                while isinstance(root, ast.Call) and isinstance(root.func, ast.Attribute):
                    root = root.func.value
                if not (isinstance(root, ast.Name) and root.id in btn_vars):
                    continue
                v = _const_or_template(node.args[0] if node.args else None)
                if v is None:
                    continue
                extra.append((path.name, node.lineno, root.id, node.func.attr, v))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            cur, props, classes = node, [], []
            while isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute):
                attr = cur.func.attr
                if (attr == "button" and isinstance(cur.func.value, ast.Name)
                        and cur.func.value.id == "ui"):
                    break                                  # 到达链根 ui.button(
                a = cur.args[0] if cur.args else None
                v = _const_or_template(a)
                if v is not None and attr in ("props", "classes"):
                    (props if attr == "props" else classes).append((v, cur.func.value.end_lineno))
                cur = cur.func.value
            else:
                continue                                   # 这条链的根不是 ui.button
            key = (path.name, id(cur))
            if len(props) + len(classes) >= len(by_root.get(key, ((), ()))[0]) + len(by_root.get(key, ((), ()))[1]):
                by_root[key] = (props, classes)
    out = []
    for (fname, _), (props, classes) in by_root.items():
        for val, ln in props:
            out.append((fname, ln, val, [c for c, _ in classes]))
    # 分语句写法：按 (文件, 变量名) 把散落各处的 props/classes 归到同一个按钮上
    grouped: dict = {}
    for fname, ln, var, attr, val in extra:
        g = grouped.setdefault((fname, var), {"props": [], "classes": [],
                                              "assign_classes": assign_cls.get((fname, var), [])})
        g[attr].append((val, ln))
    for (fname, _var), g in grouped.items():
        cls = [c for c, _ in g["classes"]] + g["assign_classes"]
        for val, ln in g["props"]:
            out.append((fname, ln, val, cls))
    return out


def _assigned_widget_vars(tree, widget: str) -> dict:
    """`x = ui.<widget>(...)` → {变量名: 赋值链上的 classes 实参}。(R22)

    【为什么要抽出来】R21 给按钮补了"先赋值、之后再 `x.props(...)`"这种写法的采集
    （原来只走链式，90 个按钮只采到 88），但**同一个决定只落到了按钮那一半** ——
    分页那条守卫仍然只认链式：把 `ui.pagination(...)` 改写成
    `p = ui.pagination(...)` + `p.props("size=sm")`，守卫完全看不见（实测）。
    正是本项目第①号形状。收成一份，两处共用。
    """
    import ast
    out: dict = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call, classes = node.value, []
        while isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
            if call.func.attr == "classes":
                v = _const_or_template(call.args[0] if call.args else None)
                if v is not None:
                    classes.append(v)
            if not isinstance(call.func.value, ast.Call):
                break
            call = call.func.value
        f = call.func if isinstance(call, ast.Call) else None
        if isinstance(f, ast.Attribute) and f.attr == widget:
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.setdefault(t.id, []).extend(classes)
    return out


def _const_or_template(node):
    """字符串实参 → 字符串；f-string → 把每个插值位换成 `{}` 的骨架；其余 → None。

    f-string 不能直接跳过：全站所有确认框的『确定』按钮都是 `.props(f"color={ok_color} unelevated")`，
    跳过它等于那一整类按钮从来没被检查过。换成骨架之后它有一个稳定的键 `color={} unelevated`，
    照样能登记成一个角色。
    """
    import ast
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append("{}")
        return "".join(parts)
    return None


def _assigned_button_vars(tree) -> dict:
    """`x = ui.button(...)` → {变量名: 赋值链上的 classes}。实现见 _assigned_widget_vars。"""
    return _assigned_widget_vars(tree, "button")


def _role_key(props: str, classes) -> str:
    """角色词表的键：props 加上尺寸档（12px 的按钮带 ` +btn-sm` 后缀）。"""
    return props + (" +btn-sm" if "btn-sm" in " ".join(classes) else "")


def _button_props():
    return [(f, ln, p) for f, ln, p, _ in _button_calls()]


def test_every_button_uses_a_registered_role():
    """每个按钮的 (props, 尺寸档) 组合都得在角色词表里登记。

    加一种新样式不是错，**不写清它是什么角色**才是。登记时顺手就会发现
    "这和已有的某个角色其实是同一件事"——那正是这条用例想让人停下来想的一秒。

    【用白名单而不是"检查有没有 unelevated"这类规则】角色是语义，规则是形式。
    白名单逼着加新样式的人回答一句"它是什么角色"，而规则只会被绕过。

    ⚠️ 这条用例被删过一次：R18 里我重写取样函数时，用「从 A 函数的 def 行切到 B 用例的 def 行」
    整段替换，而它正好夹在 A 和 B 中间，于是连同它一起被换掉了 ——
    从那一刻起 `_BUTTON_ROLES` 成了一本没人读的死字典，而 R18 的文档还写着"新增两条"。
    第 19 轮的审计把它揪了出来。**切片替换会静默吞掉区间里的别的东西，别再这么改文件。**
    """
    rows = _button_calls()
    assert len(rows) >= 60, f"只扫到 {len(rows)} 个按钮，取样逻辑大概是失效了"
    unknown = [f'{f}:{ln} → "{k}"' for f, ln, k in
               ((f, ln, _role_key(p, cls)) for f, ln, p, cls in rows)
               if k not in _BUTTON_ROLES]
    assert not unknown, (
        "这些按钮的样式不在角色词表里，全站按钮会开始分裂：\n  " + "\n  ".join(unknown)
        + "\n若确实是个新角色，把它连同一句话的说明加进 _BUTTON_ROLES。")


def test_no_role_has_two_sizes_by_accident():
    """(R19) 同一个 props 组合出现两种尺寸时，两种都必须在词表里各自登记。

    R18 的立论就是"同一个角色出现两种尺寸"，而当时的词表只按 props 建键、尺寸在键之外，
    所以立论修完症状还在：实测 `flat round dense color=primary`、`outline color=primary`、
    `unelevated color=primary` 三个组合各有两档。前两个是漏改（已补齐），
    第三个是真的有意（与输入框同行），于是它以 `+btn-sm` 单独占一行。
    """
    import collections
    sizes = collections.defaultdict(set)
    for _, _, p, cls in _button_calls():
        sizes[p].add("btn-sm" if "btn-sm" in " ".join(cls) else "")
    split = {p: s for p, s in sizes.items() if len(s) > 1}
    for p in split:
        for suffix in ("", " +btn-sm"):
            assert (p + suffix) in _BUTTON_ROLES, (
                f'props "{p}" 同时用了两种尺寸，但词表里没有 "{p}{suffix}" 这一条——'
                "要么是漏挂 .btn-sm，要么就把这一档登记清楚（写明什么时候用哪一档）")


def test_no_button_sets_its_own_font_size():
    """按钮不许在调用点写死字号。

    Quasar 的 `size=` 是【行内 font-size】（sm=10px / md=14px），所以
    `size=sm` + `.style("font-size:12px")` 是两层叠加——曾经因此出现全站唯一一个
    12px 的文字按钮，而它上下相邻的文字都是 14px。与徽标同一条原则：
    字号由全局定，调用点只选角色。
    """
    import pathlib
    import re
    offenders = []
    for path in sorted((_ROOT / "pages").glob("*.py")):
        src = path.read_text(encoding="utf8")
        for m in re.finditer(r'ui\.button\(.{0,300}?\.style\(\s*"([^"]*font-size[^"]*)"', src, re.S):
            offenders.append(f"{path.name}:{src[:m.start()].count(chr(10)) + 1} → {m.group(1)}")
        # size=sm 只允许出现在图标按钮（round dense）上：文字按钮用它会掉到 10px
        for m in re.finditer(r'ui\.button\((.{0,200}?)\)[^\n]*(?:\n[^\n]*){0,2}?\.props\(\s*"([^"]*)"', src, re.S):
            props = m.group(2)
            if "size=sm" in props and "round" not in props:
                offenders.append(
                    f"{path.name}:{src[:m.start()].count(chr(10)) + 1} 文字按钮用了 size=sm（=10px）")
    assert not offenders, "按钮在调用点定了字号：\n  " + "\n  ".join(offenders)


def test_dense_buttons_are_always_the_small_scale():
    """(R18) `dense` 在本项目的按钮语法里表示【行内】，而行内恒等于 12px。

    没有这条的话 `flat dense` 是个尺寸未定的角色：同一块详情面板里，元操作行三个
    `flat dense` 是 12px，而下载行里的『补齐该源』『自动补齐』同样写 `flat dense` 却是 14px，
    全凭哪个调用点先写——这正是 layout.py 那段注释逐字描述过的症状，换了实现机制
    （缺 .btn-sm 而不是缺 .style）之后原样还在。
    真要 14px 就别写 dense（那两个按钮已经改成 `flat`，与同一行的『下载该源』一致）。

    round 的图标按钮不在此列：它们分两档是有意的（面板级 vs 行内），见角色词表。
    """
    bad = [f"{f}:{ln} props(\"{p}\") classes={cls}" for f, ln, p, cls in _button_calls()
           if "dense" in p.split() and "round" not in p.split()
           and "btn-sm" not in " ".join(cls)]
    assert not bad, ("这些按钮写了 dense（=行内）却没挂 .btn-sm（=12px），"
                     "同一个角色会出现两种尺寸：\n  " + "\n  ".join(bad))


def test_no_button_sizes_itself_with_tailwind():
    """(R18) 按钮不许用 Tailwind 的 text-* 定字号——那绕开了 .btn-sm 这个唯一开关。

    `test_no_button_sets_its_own_font_size` 只扫 `.style(font-size)` 与 props 里的 `size=sm`，
    Tailwind 类不在它的视野里，于是 settings.py 那个『应用开始使用日过滤』用 `text-xs` 定了
    12px 静悄悄地存在。今天没有视觉差异（两条路都得到 12px），风险是 latent：
    哪天把 .btn-sm 从 12px 调成别的值，这一个按钮不会跟着变——而那正是这条纪律存在的理由。
    """
    bad = [f"{f}:{ln} classes={c!r}" for f, ln, _, cls in _button_calls()
           for c in cls if _METRIC_CLASSES.search(c)]
    assert not bad, "按钮用 Tailwind 类定了字号，绕开了 .btn-sm：\n  " + "\n  ".join(bad)


def test_only_two_greys_exist():
    """(E-37) 全站的灰只有两档：text-gray-400（说明文/字段名）与 text-gray-500（更弱的附注）。

    正文不着灰（番剧简介、bgm 字段的值、帮助气泡正文、INFO 日志行）—— 那些是要读的内容，
    用默认前景色；它们曾经用 gray-300，实际上是第三档灰，而"正文"和"说明文"
    本来就不该在同一个灰阶里排队。hover 态用 hover:text-white，别用更亮的灰。

    没有这条守卫时的实测状态：gray-400 六十处、gray-500 四十九处、gray-300 三处，
    而 warn_banner 的文档写死的是"中性说明＝gray-500"——规矩写下之后没人照着做。
    """
    import re
    allowed = {"text-gray-400", "text-gray-500"}
    pat = re.compile(r"\b(?:hover:)?text-gray-(\d{2,3})\b")
    offenders = []
    for path in sorted((_ROOT / "pages").glob("*.py")):
        src = path.read_text(encoding="utf8")
        for m in pat.finditer(src):
            token = m.group(0)
            if token.startswith("hover:"):
                offenders.append(f"{path.name}:{src[:m.start()].count(chr(10)) + 1} {token}"
                                 "（hover 用 hover:text-white，别引入第三档灰）")
            elif token not in allowed:
                offenders.append(f"{path.name}:{src[:m.start()].count(chr(10)) + 1} {token}")
    # 【CSS 字符串里写死的十六进制也要扫】token 类名那条判据看不见它们 ——
    # 第 20 轮的审计正是从这儿翻出两处 `#d1d5dc`（Tailwind gray-300），
    # 那是被 E-37 拿掉的第三档灰，只是换了个写法躲过了守卫。
    # 颜色一律走 layout._TOKENS + text_token()，别再写死。
    _GREYISH = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")
    _ALLOWED_HEX = {
        # 底色，不是文字色
        "#121212", "#0b0d10", "#1b1e24", "#15171c", "#1a1c22",
        "#fff", "#ffffff", "#000", "#000000",
        # 【ECharts 的配置例外】pages/anime.py 的环图 option 是【传给 JS 图表库的 JSON】、
        # 走 canvas 渲染，不是 CSS。oklch() 在 canvas fillStyle 里的支持依赖浏览器版本，
        # 而这个项目没有浏览器可验 —— 拿一个看不见的图去换"写法统一"不值。
        # 代价是这两个值要与 layout._TOKENS 手工对齐：
        #   #99a1af = grey(gray-400)、#d1d5dc = ink-soft。改 token 时记得同步。
        "#99a1af", "#d1d5dc",
    }
    for path in sorted((_ROOT / "pages").glob("*.py")):
        for ln, line in enumerate(path.read_text(encoding="utf8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue                      # 注释里提到某个色号不算
            for m in _GREYISH.finditer(line):
                if m.group(0).lower() in _ALLOWED_HEX:
                    continue
                r, g, b = (int(m.group(0)[1:][i:i + 2] or "0", 16) for i in (0, 2, 4)) \
                    if len(m.group(0)) == 7 else (0, 0, 0)
                if len(m.group(0)) == 7 and max(r, g, b) - min(r, g, b) <= 24:
                    offenders.append(f"{path.name}:{ln} 写死了灰色 {m.group(0)} "
                                     "（颜色走 layout._TOKENS + text_token()）")
    assert not offenders, (
        "只允许 text-gray-400 / text-gray-500 两档灰，颜色一律走 token：\n  " + "\n  ".join(offenders)
        + "\n（要读的正文不着灰，用默认前景色）")


# ---------------- (R21) 徽标【文案】的一致性 ----------------

# 徽标是全站唯一的标签机制，19 种文案里历史上只有两种带装饰符号
# （`✓ 已确认` / `🎊 已完结`），其余 17 种是纯文字。
# 两种写法混在同一屏时，用户会去找"带勾的和不带勾的差在哪"——而答案是"没差，
# 只是写的人不同"。颜色已经承担了区分语义的职责（green/orange/teal/grey…），
# 装饰符号是第二套并行的编码，且只覆盖了 2/19。
#
# 【判据用 Unicode 类别 So，不是"首字符白名单"】第一版写成"首字符必须是中日文/字母/数字"，
# 当场误伤四类正当写法：f-string 的 `{name} …`、下载速度的 `↓ 1.2MB/s`、
# 空值占位的 `—`、以及文案内部的间隔号 `·`。
# `So`（Symbol, other）把它们干净地分开了：✓(2713) ⏳(23F3) 🎊(1F38A) ✔ ★ 都是 So；
# 箭头 ↓→ 是 `Sm`（有方向语义，不是装饰）、`—` 是 `Pd`、`·` 是 `Po`，全部放行。
# 【扫整串而不只是首字符】emoji 放中间同样是第二套编码；而且原来那处是三元写法
# （`"✓ 已确认" if … else "⏳ 待确认"`），只钉前半截的话把符号搬到 else 分支就绕过去了。
_SO = "So"


def _badge_literal_texts():
    """(文件名, 徽标里的每一个字面字符串) —— 含三元的两个分支；变量/表达式跳过。"""
    import unicodedata  # noqa: F401  (供下面的用例用同一个 import 语义)
    for p in sorted((_ROOT / "pages").glob("*.py")):
        src = p.read_text(encoding="utf-8")
        for _, chain in _badge_chains(src):
            # 只看 ui.badge(...) 的【第一个实参段】：.props()/.tooltip() 里的字符串是
            # 说明文字，不是标签文案，不受这条纪律约束（tooltip 里有 emoji 无所谓）。
            head = chain[: chain.find(").props(")] if ").props(" in chain else chain
            head = head[: head.find(").tooltip(")] if ").tooltip(" in head else head
            for lit in re.findall(r'"([^"]*)"', head) + re.findall(r"'([^']*)'", head):
                yield p.name, lit


def test_badge_texts_carry_no_decorative_symbol():
    """徽标文案里不许出现 Unicode `So` 类字符（emoji / ✓ / ⏳ / ★ …）。

    语义由【颜色 + 文字】承担；再叠一套符号编码，只会覆盖到一部分标签，
    让用户去找一个并不存在的规律。
    """
    import unicodedata
    texts = list(_badge_literal_texts())
    assert len(texts) >= 15, f"只抓到 {len(texts)} 条字面徽标文案，抓取逻辑多半坏了"
    bad = [(f, t, c) for f, t in texts for c in t if unicodedata.category(c) == _SO]
    assert not bad, "徽标文案带了装饰符号：" + "; ".join(
        f"{f}: {t!r} 含 {c!r}" for f, t, c in bad)


def test_the_symbol_guard_would_actually_fire():
    """反向：上一条依赖 `unicodedata.category(...) == "So"` 真能认出那几个符号。

    判据本身写错（比如把 `So` 打成 `Sc`）时，上一条会永远绿——这正是本项目
    反复踩过的"守卫看着在守、其实什么都没守"的形状。这里直接钉住判据。
    """
    import unicodedata
    for ch in "✓⏳🎊✔★":
        assert unicodedata.category(ch) == _SO, f"{ch!r} 没被判成装饰符号"
    for ch in "↓→—·":   # 有方向/分隔语义的，必须放行
        assert unicodedata.category(ch) != _SO, f"{ch!r} 被误判成装饰符号"


def test_the_symbol_rule_also_covers_texts_computed_elsewhere():
    """上面两条只看 `ui.badge(...)` 里的**字面量** —— 而 pages/ 下 72 处徽标里有 21 处
    的文案是别处算好再传进来的（`ui.badge(text)` / `ui.badge(torrent_status_cn(...))`）。

    R21 第一版守卫就栽在这个盲区上：`STATUS_CN["stalled"]` 当时带着一个 U+26A0 警告号，
    正是要禁的 `So`，它经 `torrent_status_cn` / `live_status` 渲染到三个徽标上，
    而那条守卫的说明还写着"19 种文案里只有两种带符号"——**规则的作用域大于验证的作用域**，
    本项目第②号形状，这一次是我自己写的守卫踩进去的。

    这里从【文案的来源】查：状态词表的全部取值 + 两个文案函数在状态 × qB 态全组合下的返回。
    """
    import unicodedata
    import sys
    from pathlib import Path

    sys.path.insert(0, str(_ROOT))
    from core import engine as ce
    from pages import layout as L

    texts = []
    texts += [(f"STATUS_CN[{k!r}]", v) for k, v in L.STATUS_CN.items()]
    texts += [(f"SEVERITY_COLOR 覆盖的 {k}", k) for k in L.SEVERITY_COLOR]
    for st in list(L.STATUS_CN):
        for prog in (0.0, 0.5, 1.0):
            texts.append((f"torrent_status_cn({st},{prog})", L.torrent_status_cn(st, prog, None)))
            for qs in [""] + list(ce._QB_STATES):
                for in_plan in (None, True, False):
                    t, _c = L.live_status(st, qs, prog, None, 0, in_plan, True, 1, "")
                    texts.append((f"live_status({st},{qs},in_plan={in_plan})", t))
    assert len(texts) > 200, f"只枚举出 {len(texts)} 条，组合表坏了"
    bad = [(src, t, c) for src, t in texts for c in str(t)
           if unicodedata.category(c) == _SO]
    assert not bad, "徽标文案（含算出来的）带了装饰符号：" + "; ".join(
        f"{src} -> {t!r} 含 {c!r}" for src, t, c in bad[:8])


# ---------------- (R21) 分页与"第二种标签控件" ----------------

def test_pagination_never_sets_its_own_size():
    """三处 `ui.pagination` 的尺寸由全局 CSS 一处定，调用点不许自己写。

    R21 之前是两种写法：两处 `size=sm`（Quasar 的档位，渲染成**行内** font-size:10px），
    一处 `dense` —— 而 QPagination 的 props 表里**根本没有 dense**，
    Vue 把它当 fallthrough attr 扔到根 div 上，零效果。
    于是同一个控件在同一页上一个 10px、一个 14px，还有一个写了等于没写：
    与徽标当年"同一个『待确认』三种大小"完全同形。
    """
    import ast

    bad = []
    for path in sorted((_ROOT / "pages").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf8"))
        # 【分语句写法也要采】(R22) `p = ui.pagination(...)` 之后 `p.props("size=sm")` ——
        # R21 给按钮补过这一段，却**只落到了按钮那一半**，分页这条守卫仍然只认链式：
        # 改写成分语句，加回 `size=sm` 它照样全绿（实测）。第①号形状。
        pg_vars = _assigned_widget_vars(tree, "pagination")
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "props"):
                continue
            var_root = node.func.value
            while isinstance(var_root, ast.Call) and isinstance(var_root.func, ast.Attribute):
                var_root = var_root.func.value
            if isinstance(var_root, ast.Name) and var_root.id in pg_vars:
                v = _const_or_template(node.args[0] if node.args else None)
                if v is not None and ("size=" in v or "dense" in v):
                    bad.append(f"{path.name}:{node.lineno} → {v!r}（分语句写法）")
                continue
            # 顺着 `ui.pagination(...).props("…")` 这条链找根，把链上所有 props 收下来
            root, props = node.func.value, []
            v = _const_or_template(node.args[0] if node.args else None)
            if v is not None:
                props.append(v)
            # 【走到链根就停，别再多走一步】第一版一路走到了 `ui` 这个 Name，
            # 于是"根是不是 pagination"永远判否 —— 守卫看着在跑，实际一条都没检查
            # （实测：把 `.props("size=sm")` 加回去，它照样全绿）。
            is_pagination = False
            while isinstance(root, ast.Call) and isinstance(root.func, ast.Attribute):
                if root.func.attr == "pagination":
                    is_pagination = True
                    break
                if root.func.attr == "props":
                    pv = _const_or_template(root.args[0] if root.args else None)
                    if pv is not None:
                        props.append(pv)
                root = root.func.value
            if not is_pagination:
                continue
            for pv in props:
                if "size=" in pv or "dense" in pv:
                    bad.append(f"{path.name}:{node.lineno} → {pv!r}")
    assert not bad, "分页在调用点自己定了尺寸：" + "; ".join(bad)


def test_the_global_css_sizes_the_pagination():
    """反向：上一条只禁调用点写，禁完之后必须有【一处】全局规则接管，否则分页会变回 14px。"""
    from pages import layout
    css = layout._HEAD_BADGE_CSS
    assert ".q-pagination .q-btn{font-size:" in css, "全局没给分页定字号"
    assert "!important" in css.split(".q-pagination .q-btn{")[1][:60], \
        "Quasar 的 size= 是行内样式，不带 !important 压不住"


def test_no_second_tag_widget_sneaks_in_through_props():
    """全站唯一的标签机制是 `ui.badge`。

    已有的 `test_badges_exist_and_are_the_only_tag_widget` 查的是 `ui.chip(`，
    挡不住从 props 里溜进来的那一种：`ui.select(..., multiple=True).props("use-chips")`
    会把选中项渲染成 `<q-chip>` —— 而项目里没有任何 `.q-chip` 规则，
    它吃 Quasar 原样式（16px 圆角、14px、深底白字），与徽标那套 4px 直角 + oklch 色底
    是两种完全不同的东西，同一屏里就有了两种"标签"。
    """
    bad = []
    for p in sorted((_ROOT / "pages").glob("*.py")):
        src = p.read_text(encoding="utf-8")
        for i, line in enumerate(src.splitlines(), 1):
            if "use-chips" in line and not line.lstrip().startswith("#"):
                bad.append(f"{p.name}:{i}")
    assert not bad, "这些地方引入了第二种标签控件（q-chip）：" + "; ".join(bad)


def test_expansion_has_exactly_two_documented_tiers():
    """折叠面板只允许两档，而且【哪一处属于哪一档】必须是显式的。

    R22 之前是漂移：13 处里 9 处写 `dense`、4 处不写，两组之间没有任何成文的规则。
    现在紧凑档是全局默认（`ui.expansion.default_props("dense")`），
    主结构分组（列表的季度·年份分组头，没有外框卡片、折叠头本身就是分隔）
    用 `.props(remove="dense")` 显式退出 —— 调用点因此永远只做一个选择：
    "我是不是主结构分组"。

    判据：任何调用点都不许再写 `props("dense")`（那是默认值，写了就是冗余、
    而冗余正是漂移的起点）；退出那一档必须用 `remove=`。
    """
    import ast

    bad_dense, opted_out = [], []
    for path in sorted((_ROOT / "pages").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf8"))
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "props"):
                continue
            root, is_exp = n.func.value, False
            while isinstance(root, ast.Call) and isinstance(root.func, ast.Attribute):
                if root.func.attr == "expansion":
                    is_exp = True
                    break
                root = root.func.value
            if not is_exp:
                continue
            v = _const_or_template(n.args[0] if n.args else None)
            if v is not None and "dense" in v:
                bad_dense.append(f"{path.name}:{n.lineno}")
            if any(k.arg == "remove" for k in n.keywords):
                opted_out.append(f"{path.name}:{n.lineno}")

    assert not bad_dense, ("这些折叠面板在调用点写了 dense —— 那已经是全局默认："
                           + "; ".join(bad_dense))
    assert len(opted_out) == 4, (
        f"显式退出紧凑档的有 {len(opted_out)} 处（预期 4 处：番剧列表 3 + 剧场版 1）："
        + "; ".join(opted_out) + "。多了说明第二档在扩散，少了说明分组头被压扁了")


# ---------------- (R24) 文案与配色的两条纪律 ----------------

def test_the_movie_side_never_says_pending_the_anime_way():
    """剧场版页不许出现字面量「待下」。

    GLOSSARY 第一节把「待下」/「可下载」列为**有意区分**：
    番剧的 pending 是"后台到点会自动下、不用管"，剧场版的 pending 是"等着你去点" ——
    同一个 status，对用户的含义**相反**。剧场版全线没有任何自动下载路径，
    写「待下」等于告诉用户这几百条不用管。

    这个词在这一页漂回来过一次了（`_mov_live_status` 就是当初为改写它写的），
    所以钉成守卫。`== "待下"` 那种等值比较是**判据**不是文案，排除掉。
    """
    import ast

    # 【只看渲染出去的文案实参，别扫源码文本】第一版按行 grep，当场被**本文件自己的
    # docstring**（`_mov_live_status` 里那句解释"把『待下』改写成『可下载』"）判红 ——
    # 同一个坑本项目踩过四次。这里只取 `ui.<widget>(...)` 的字符串实参。
    tree = ast.parse((_ROOT / "pages" / "movies.py").read_text(encoding="utf-8"))
    bad = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name) and n.func.value.id == "ui"):
            continue
        for a in n.args:
            v = _const_or_template(a)
            if v and "待下" in v:
                bad.append(f"movies.py:{n.lineno}: ui.{n.func.attr}({v!r})")
    assert not bad, "剧场版页用了番剧那条线的『待下』（两条线含义相反）：\n  " + "\n  ".join(bad)


def test_the_kpi_number_never_inherits_pure_white():
    """KPI 卡的大数字必须显式落色 —— 不能靠继承 `body--dark{color:#fff}`。

    原来"没有高亮色"与"值为 0"两种情况不加任何颜色类，于是一排卡里
    『不用动手的数字』（订阅中/已忽略/种子数/版本，以及所有 0）是**纯白**，
    而『需要动手的数字』落在 -400/-500 档（亮度约 70%）——
    **最该被看见的那个对比度最低**。
    """
    import ast
    import inspect

    from pages import layout as L

    src = inspect.getsource(L.kpi_cards)
    tree = ast.parse(__import__("textwrap").dedent(src))
    # 找 `cls = "text-2xl font-bold" ...` 那个赋值，要求它的每条分支都给出一个颜色类
    target = None
    for n in ast.walk(tree):
        if (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "cls"
                                              for t in n.targets)):
            target = n
    assert target is not None, "没找到 KPI 数字的样式赋值，用例的前提坏了"
    dumped = ast.dump(target)
    assert "IfExp" in dumped, "样式里没有分支 —— 说明高亮/中性/零值没有各自的落色"
    assert "text-gray-500" in dumped and "text-gray-400" in dumped, \
        "中性与零值没有显式落灰，会继承纯白"


def test_every_empty_state_uses_the_weaker_grey():
    """空状态占位一律用 `text-gray-500`（更弱的那一档）。

    `pages/layout.py` 的灰阶纪律成文写着：
    「gray-500 —— 更弱的附注：页码、原始种子标题、"没有内容"这类占位」。
    实际 14 处空状态里只有 3 处照做，其余 11 处是 gray-400 —— 与正文同档。
    最直接的一处：仪表盘上『暂无正在下载的种子』是 gray-500，而同一屏
    『没有待确认的番』是 gray-400，两句都是"这里什么都没有"，深浅却不一样。

    这不是洁癖：两档灰是这套界面里**唯一**用来区分"内容"与"附注"的手段
    （见 `test_only_two_greys_exist`），占位跑到内容那一档，用户会以为那句话是内容。
    """
    import ast

    _EMPTY_PREFIXES = ("没有待确认", "没有已忽略", "没有待识别", "还没有番剧", "还没有种子",
                       "还没有剧场版", "还没有备份", "还没有源组", "暂无正在下载", "暂无日志")
    bad = []
    for path in sorted((_ROOT / "pages").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines()
        for n in ast.walk(ast.parse(src)):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "label"):
                continue
            v = _const_or_template(n.args[0] if n.args else None)
            if not v or not any(v.startswith(e) for e in _EMPTY_PREFIXES):
                continue
            seg = "\n".join(lines[n.lineno - 1:(n.end_lineno or n.lineno) + 2])
            if "text-gray-500" not in seg:
                bad.append(f"{path.name}:{n.lineno} {v[:24]}")
    assert not bad, "这些空状态占位没用更弱的那一档灰：\n  " + "\n  ".join(bad)
