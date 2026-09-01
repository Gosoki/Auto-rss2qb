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
    "unelevated color=primary +btn-sm": "与输入框同行的主操作（设置页『应用开始使用日过滤』）",
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
    for path in sorted((_ROOT / "pages").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf8"))
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
                v = a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None
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
    return out


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
