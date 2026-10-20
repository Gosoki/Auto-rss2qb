"""共享布局：顶栏导航 + 统一的内容容器。所有页面都套 frame()。

页面级组件放在自己的页面文件里；这里只放跨页面复用的东西。
"""
import asyncio
import logging
from contextlib import contextmanager
from html import escape

from nicegui import Client, context, ui
from sqlalchemy.exc import InterfaceError, OperationalError

import config
import db
from core import engine, health, worker
from sources.parse import quarter_sort_key

log = logging.getLogger("autorss")

# ui.notify 认的是【当前槽位】：内部走 context.client → slot.parent.client，而 Slot 对 parent
# 只有【弱引用】。处理器 await 期间，它所在的面板随时可能被刷掉——30s 定时器就在刷仪表盘，
# refreshable.refresh() 会 clear() 掉整棵子树，元素一没人引用就即刻回收，那个弱引用随之落空，
# notify 便抛 RuntimeError 掀翻整个处理器（nicegui 自己的兜底 handler 取 client 时会再抛一次，
# 日志里成对出现的两段 traceback 就是这么来的）。
# 只有【面板被清空与 notify 之间发生过 await】才会撞上：refresh() 是丢给后台任务做的，紧跟其后的
# notify 跑在清空之前——所以"先 refresh_all() 再 notify"的上百处一直没事；真正中招的是耗时长的
# 处理器（补下全部 / 扫描剧场版 / 重新识别 / 迁移数据…），它们 await 的工夫定时器把面板换掉了。
# 与下面 confirm() 里那段 canary 注释是同一个坑的两副面孔。
# 这里给 ui.notify 包一层：正常路径原样跑；槽位已随面板消失时，退回到还连着的客户端发。
# 直接替换 ui.notify 而不是另起个名字，是不想留坑——以后新写的 ui.notify 也自动是安全的。
_ui_notify = ui.notify


# 有彩色底的通知类型：这几种的底色都是浅色档（positive #21BA45 / warning #F2C037 /
# info #31CCEC / negative 被 ui.colors 定成了 red-400），Quasar 却一律配白字 → 1.9~2.9:1。
# 【不带 type 的通知【不能】改】那时底色是 Quasar 默认的 #323232 深灰，深色前景只有 1.64:1。
_LIGHT_NOTIFY_TYPES = ("positive", "negative", "warning", "info")


def _slot_safe_notify(message, **kwargs) -> None:
    # 【必须写 textColor 这个驼峰名】NiceGUI 的 notify 只把 close_button/multi_line 两个参数
    # 转成驼峰（见 nicegui.functions.notify 的 ARG_MAP），其余【原样】下发给 Quasar。
    # 而 Quasar 认的是 textColor —— 写 text_color 会被它当成不认识的选项直接忽略，
    # 也就是说这一行如果写成蛇形，看起来改了、实际什么都没发生（本项目就这么错过一次）。
    if kwargs.get("type") in _LIGHT_NOTIFY_TYPES or kwargs.get("color"):
        kwargs.setdefault("textColor", "dark")
    try:
        _ui_notify(message, **kwargs)
    except RuntimeError:
        # 自用工具，正常只开一个页面；真开了多个标签页就都弹一下——总比把这条结果丢了强。
        for client in list(Client.instances.values()):
            if client.has_socket_connection:
                with client:
                    _ui_notify(message, **kwargs)


ui.notify = _slot_safe_notify


def reload_other_tabs() -> int:
    """让【其他】已连接的标签页刷新一次，返回刷新了几个。当前标签页由调用方自己决定怎么处理。

    (E-55，2026-09-02 拍板选 A) 页面渲染时把业务库的整数主键捕进了闭包（按钮的 torrent_id /
    anime_id）；切库之后那些主键指向另一个库里毫不相干的行，而 `db/transfer.py` 明确保留主键。
    在途闸（E-46）盖不住这一类：那里的 "await" 是用户的思考时间。所以切库成功后把其他标签页
    整个刷掉 —— 它们重新渲染就拿到新库的主键。只漏"确认框已弹出、切库恰在此刻、然后点确认"
    那一毫秒级的窗口（B 案要在 20 多个写回入口逐点比对库身份，为那一毫秒不值当）。
    形状与上面 _slot_safe_notify 的多标签页分支相同：遍历 Client.instances、只碰有 socket 的。
    """
    me = None
    try:
        me = context.client
    except RuntimeError:
        pass
    n = 0
    for client in list(Client.instances.values()):
        if client is me or not client.has_socket_connection:
            continue
        with client:
            ui.navigate.reload()
        n += 1
    return n

# 下拉恒为『锚在输入框下方的菜单』。Quasar 的 behavior 默认值是 default，判定是
# `platform.is.mobile !== true && behavior !== "dialog" ? false : behavior !== "menu" && …`——
# 即手机/平板上默认翻成顶部弹出的全宽 dialog（带一个没用的只读输入框），跟桌面端两副样子。
# 定死 menu，所有 ui.select（含以后新加的）在任何设备上都是同一个下拉。
ui.select.default_props("behavior=menu dense outlined")
# 输入类控件的统一形态定在这里，而不是每个调用点各写一遍 `.props("dense outlined")`。
# 此前是后者：38 处逐字重复，于是漏一处就多一种样子（movies 的『扫描间隔』输入框就是全站唯一
# 一个下划线样式的框）。定成默认值之后，以后新加的控件默认就是对的，也不会再有"漏写"这件事。
ui.input.default_props("dense outlined")
ui.number.default_props("dense outlined")
ui.textarea.default_props("dense outlined")
ui.switch.default_props("dense")
# 【折叠面板：两档，默认是紧凑那一档】(R22)
# 全站 13 处 ui.expansion 原本 9 处写 dense、4 处不写，而两组之间没有任何成文的规则 ——
# 那 4 处恰好是【番剧/剧场版列表的季度·年份分组头】，它们没有外框卡片，
# 折叠头本身就是分组之间唯一的视觉分隔，需要更大的行高；
# 其余 9 处要么在 ui.card() 里（设置页的分区），要么是页脚的次级折叠（已删除/已排除），
# 外框已经提供了分隔。两档各有其用，但**必须是成文的两档，不能是漂移**。
# 默认取多数那一档（dense）；主结构分组用 `.props(remove="dense")` 显式退出，
# 调用点因此永远只做一个选择：我是不是主结构分组。
ui.expansion.default_props("dense")
# 按钮：no-caps 让拉丁文标签不被 Quasar 强制大写（中文看不出差别，混排时才显形）。
# 【它是全局默认，调用点不要再写】写了也不会错，但会让角色词表里同一个角色出现两个名字
#（"unelevated color=primary" 与 "…no-caps" 曾经各占 13 / 5 处，渲染出来一模一样）。
# tests/test_ui_badge_style.py 的 _WIDGET_DEFAULTS 里登记了 button:{no-caps} 来守住这一条。
ui.button.default_props("no-caps")

# ---- 全站术语表：改文案前先看这里，UI 上同一个东西只能有一个叫法 ----
#   TV 周更 → 「番剧」        （不用"动漫/动画/anime"）
#   剧场版/OVA → 「剧场版」    （不用"电影/影片/OVA・剧场版"）
#   一条可下载的种子 → 「种子」。剧场版侧【有意】叫「版本」（Q9 已定 B，见 docs/GLOSSARY.md §一）：
#     一部剧场版真的有多个发布版本、逐个人工挑，与番剧"一集一份"语义不同，两侧不统一。
#   status=pending → 「待下」  （剧场版侧显示成"可下载"，同一个区分）
#   review 策略 → 「人工审核」  rejected → 「已忽略」  未匹配 bgm → 「待识别」
#     ↑ 这一项曾经三个叫法：术语表写「未识别」、tab 与 KPI 用「待识别」、番剧卡片写「未匹配」。
#       统一取「待识别」——tab、KPI、空状态文案、以及各处 tooltip 里的"去『待识别』手动绑定"都是它。
#   episode=-2（集号没解析出来）→ 「未知集」（不要写成"未识别"，那是 bgm 那一档的词）
NAV = [("manage", "番剧", "/"), ("movies", "剧场版", "/movies"),
       ("parse", "解析测试", "/parse"), ("manual", "手动下载", "/manual"),
       ("logs", "运行日志", "/logs"), ("settings", "全局设置", "/settings")]


def _jump_targets() -> list:
    """『跳转到』下拉的外链目标（新标签打开）：qB 后台(设置里填的地址，空则不列) + 常用站点。每次渲染读，改地址即时生效。"""
    qb = (config.QB_URL or "").strip()
    mikan = (config.MIKAN_BASE or "https://mikanani.me").rstrip("/")
    return ([("qB 后台", qb)] if qb else []) + [
        ("Nyaa", "https://nyaa.si"),
        ("Mikan", mikan),
        ("Bangumi", "https://bgm.tv"),
    ]

# 应用侧种子状态 → 中文（番剧表/剧场版/详情/新入库共用）
# 【文案里不带装饰符号】(R21) `stalled` 原来带着一个警告 emoji —— 而这些值最终全部渲染进
# ui.badge，全站 19 种徽标文案里只有它和另外两处带符号。严重度由颜色承担
# （stalled 有专属的 deep-orange，见 SEVERITY_COLOR 与 _HEAD_BADGE_CSS），
# 再叠一套符号编码只会让用户去找一个并不存在的规律。守卫见 tests/test_ui_badge_style.py。
STATUS_CN = {"sent": "已交付", "pending": "待下", "downloading": "下载中",
             "error": "失败", "skipped": "跳过", "deleted": "已删", "excluded": "已排除",
             "stalled": "停滞"}
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def torrent_status_cn(status: str, qb_progress=0, qb_synced_at=None) -> str:
    """无 qB 实时态时的回落显示。有实时态时调用方用 qb_live_text，不走这里。

    sent 的含义随『是否读 qB 实时态』而变，故文案也跟着变：
    · 开跟踪：sent = 已发给 qB，可能还在下 → 还没首次同步回来时显示『下载中』，
      同步过之后显示『已交付』（真正下完与否看 qb_live_text/『已完成』那个数）。
    · 关跟踪：不读 qB，发送即视为下完（settle_sent 会把 qb_progress 落成 1）→ 直接显示『已下』，
      此时说『已交付』反而让人以为还没下完。
    """
    if status == "sent":
        if not config.QB_SYNC_STATUS:
            return "已下"                     # 关跟踪：发送即完成
        if (qb_progress or 0) < 1.0 and qb_synced_at is None:
            return "下载中"                   # 刚发出去，还没同步回来
    return STATUS_CN.get(status, status)


# 【parse_bgm_id 只此一份】原来 core/manual.py 与本文件各写了一份行为完全相同的实现
# （实测 7/7 输入一致），而调用点分家：manual 那条手动下载路径用 core 那份，
# 四个 UI 绑定入口（pages/anime.py、anime_detail.py、movies.py ×2）用本文件这份。
# 这正是 DECISIONS.md 的 E-13「收紧 parse_bgm_id 的判据」踩坑的形状——真去收紧时必然只改一半，
# 于是"页面上绑定被收紧了、手动下载那条路没有"，而两处都不会报错。
# 各页面继续 `from .layout import parse_bgm_id`，导入路径不变。
from core.manual import parse_bgm_id  # noqa: F401  转出给各页面用；实现在 core/manual.py


def source_options(sources, blank: str = "按优先级") -> dict:
    """下载源下拉选项：{'': 占位(按优先级), 源: 源, ...}。blank 为空选项文案。"""
    return {"": blank, **{s: s for s in sources}}


def group_by_quarter(items):
    """按季度分组，返回 [(季度, [item...]), ...]，季度倒序、未知垫底。items 需有 .quarter。"""
    by_q: dict[str, list] = {}
    for it in items:
        by_q.setdefault(it.quarter or "未知", []).append(it)
    # 【按四位年排，不能按季度键的字符串排】季度键的年份只有两位，'99D' > '26C'，
    # 于是 1999 年首播的长番（海贼王）会排到当季之上的第一位，而调用方又让第一组默认展开：
    # 打开番剧页看到的是 1999 年那一部，当季反而折叠在下面。真库实测过。
    quarters = sorted((q for q in by_q if q != "未知"), key=quarter_sort_key, reverse=True)
    if "未知" in by_q:
        quarters.append("未知")
    return [(q, by_q[q]) for q in quarters]


def barline(label, value, maxv, color="oklch(70.7% 0.165 254.624)", lw="w-32", text=None) -> None:
    """一行『标签 + 比例条 + 数值』。比例条按 value 长；text 可自定义右侧文案（默认取 value）。番剧/剧场版共用。"""
    pct = (value / maxv * 100) if maxv else 0
    with ui.row().classes("items-center gap-3 w-full text-sm py-0.5 min-w-0"):
        ui.label(str(label)).classes(f"{lw} shrink-0 truncate").tooltip(str(label))
        with ui.element("div").classes("grow rounded min-w-0").style(
                "background:rgba(255,255,255,.08);height:12px"):
            ui.element("div").style(
                f"width:{pct:.1f}%;height:12px;background:{color};border-radius:6px")
        ui.label(text if text is not None else str(value)).classes(
            "shrink-0 text-gray-400 text-right").style("min-width:5rem")


def kpi_cards(cards) -> None:
    """一排 KPI 数字卡：cards=[(标签, 数值, 高亮色或'') / (…, on_click), ...]；
    给了 on_click 的卡可点（手型光标+悬浮高亮）。数字染色需值非零且给了高亮色；标签一律 gray-400
    （曾有第 5 元素"标签色"用来分组，全站没有一个调用点传过，R36 体检删掉 —— 分组只靠 "|"）。
    列表里放字符串 "|" 会把左右拆成两组：每组内卡等宽铺满、左右平衡（仿番剧列表 flex:1），够宽时并排，窄了整组换行成上下布局。"""
    groups: list[list] = [[]]
    for card in cards:
        if card == "|":
            groups.append([])
        else:
            groups[-1].append(card)

    def _card(card, grow: bool) -> None:
        label, val, hi, *rest = card
        on_click = rest[0] if rest else None
        c = ui.card().classes("items-center px-3 py-2" + (
            " cursor-pointer hover:bg-white/5" if on_click else ""))
        if grow:
            c.style("flex:1 1 0")          # 组内各卡等宽铺满，左右平衡
        with c:
            shade = "500" if hi == "green" else "400"   # 绿跟徽标同档(-500)，红及其余 -400
            # 【没有高亮色 / 值为 0 的要显式落一档灰，不能继承纯白】(R24)
            # 原来这两种情况不加任何颜色类，直接继承 Quasar 的 `body--dark{color:#fff}` ——
            # 于是一排卡里『不用动手的数字』（订阅中/已忽略/种子数/版本，以及所有 0）是纯白，
            # 而『需要动手的数字』落在 -400/-500 档（亮度约 70%）：**最该被看见的那个对比度最低**。
            # 同一页『番剧表』tab 的季度小结卡做法正相反（零值降到 gray-500、非零才上色），
            # 两处必须同一次改完，否则又是同一个决定只落一半。
            # 只有两档灰可用（见本文件的灰阶纪律，守卫 test_only_two_greys_exist 钉着）：
            # 中性非零用 gray-400（与标签同档，靠字号+字重拉开层级），零值降到 gray-500。
            # 于是最亮的永远是需要动手的那些彩色数字。
            cls = "text-2xl font-bold " + (f"text-{hi}-{shade}" if hi and val
                                           else "text-gray-500" if not val else "text-gray-400")
            ui.label(str(val)).classes(cls).style(  # 预留 5 位数宽度：数字增减时卡不抖、各卡等宽
                "min-width:5ch;text-align:center;font-variant-numeric:tabular-nums")
            ui.label(label).classes("text-xs text-gray-400")
        if on_click:
            # tabindex + 回车：键盘也能点（r37：可点的 KPI 卡以前是键盘够不着的 div）
            c.props("tabindex=0").on("click", on_click).on("keydown.enter", on_click)

    if len(groups) == 1:                   # 单组（如剧场版页）：维持原来的自由换行、左对齐
        with ui.row().classes("gap-3 flex-wrap p-1"):
            for card in groups[0]:
                _card(card, grow=False)
    else:                                  # 多组：每组一个 grid，够宽 n 列一排、窄了折成 2×2、再窄整组换行；绝不裁掉右边
        with ui.row().classes("w-full gap-4 flex-wrap items-stretch p-1"):
            for group in groups:
                with ui.element("div").classes("kpi-group items-stretch").style(
                        f"flex:1 1 200px;--kpi-n:{len(group)}"):
                    for card in group:
                        _card(card, grow=True)


# 全站色板（Tailwind v4 的 oklch token，与 _HEAD_BADGE_CSS 里那批徽标底色同源）。
# 【别再手写 sRGB 十六进制或 rgba()】pages/manual.py 曾经用的是 Tailwind **v3** 的老色号
#（#22c55e / #60a5fa / #9ca3af），与全站 v4 的同名色不是同一个颜色；更别扭的是同一个盒子里
# 底色用 v3 的绿、图标却用 Tailwind 类 text-green-400（走 v4），两种绿并排。
_TOKENS = {
    "blue": "70.7% 0.165 254.624",     # blue-400
    "green": "72.3% 0.219 149.579",    # green-500
    "red": "70.4% 0.191 22.216",       # red-400
    "amber": "82.8% 0.189 84.429",     # amber-400
    "grey": "70.7% 0.022 261.325",     # gray-400
    # 【前景色，不是说明文的灰阶】顶栏站名与下拉菜单项用它 —— 那是【正文/导航】文字，
    # 与 E-37 的两档说明文灰（gray-400 / gray-500）不是同一件事，别把它读成"第三档灰"。
    # 它以前是写死的 #d1d5dc（Tailwind gray-300），而写死的十六进制既不受 token 表管、
    # 也不在 text-gray-* 那条守卫的视野里 —— 第 20 轮的审计正是从这儿找出来的。
    "ink-soft": "87% 0.008 261",       # ≈ gray-300，柔和前景
}


def tint(token: str, alpha: float = 0.12) -> str:
    """染色提示块的底色，全站唯一配方。

    这个模式（一个带浅色底的圆角块，用来把一段提示从正文里拎出来）站里有 4 个实例，
    曾经是 3 种配方：amber @ .12（oklch）、red @ .12（oklch）、green @ .10（rgba + 还多一条边框）、
    blue @ .08（rgba、无边框）。透明度三档、色彩空间两种、边框有无不一 ——
    同一个"提示块"在四个页面上看着像四个不同的东西。
    """
    return f"background:oklch({_TOKENS[token]} / {alpha})"


def text_token(token: str) -> str:
    """同一批 token 的前景色，给需要写死颜色的地方（如 CSS 字符串里）用。"""
    return f"oklch({_TOKENS[token]})"


def require_config_loaded() -> bool:
    """配置没从库里读出来时，任何【把表单值写回配置】的处理器都必须当场退出。

    表单上此时全是硬编码默认值，写下去就是把库里已有配置整体改写成默认，
    而且全程零报错、还会弹一句绿色的成功提示。设置页顶部那条红 banner 说的就是这件事。

    【为什么要抽成共用函数】这道闸原来只写在设置页的 _save 里，而全站还有三个同样
    "把表单值写回配置"的处理器（设置页『应用开始使用日过滤』、/movies 的『保存自动扫描设置』、
    /parse 的多括号开关），一个都没过闸 —— 闸的作用域比它保护的东西小，
    正是本项目第②种缺陷形状。新增这类处理器时，第一行就调它。

    【什么时候不用调】写的值不来自表单、而是代码里的字面量时（如 _switch_backend 写的
    "mysql"/"sqlite"），没有"被默认值覆盖"这回事；而且那恰恰是库出问题时的自救出口，
    拦掉反而把人关在门外。tests/test_ui_busy.py 的守卫里登记了这条豁免。
    """
    if config.loaded_from_db:
        return True
    ui.notify("配置没能从数据库读出来，此时写入会把你原有的设置整体覆盖成默认值，已拦下。"
              "先修好数据库再来。", type="negative")
    return False


def warn_banner(text: str, action: tuple | list | None = None) -> None:
    """全站唯一的警告块：amber-400 文字 + 同色 12% 底 + Material warning 图标。

    需要用户注意的一律用这个块；图标由本函数出，文案里不要再带 ⚠️。

    `action=(按钮文字, 图标, 回调)`：给"只报不改"那一类横幅配一个就地的处置入口；要两个就传一个列表
    （锁定源落后：『只补这几集』『改回按优先级』）。
    不给就是原来的样子（另外 9 个调用点一个字都不用改）。**别再造第二个带按钮的横幅** ——
    全站 10 处用的是同一个块，再抄一份就是"改了样式只改一处"的温床。

    【灰度只有两档，别再加第三档】（E-37，用户 2026-09-01 拍板"只剩下 2 种灰色就行"）
      · text-gray-400 —— 说明文、字段名、次要标签。默认用它。
      · text-gray-500 —— 更弱的附注：页码、原始种子标题、"没有内容"这类占位。
      · 【正文不着灰】番剧简介、bgm 字段的值、帮助气泡里的正文、INFO 级日志行 ——
        这些是要读的内容，用默认前景色。它们曾经用 gray-300，那实际上是第三档灰，
        而"正文"和"说明文"本来就不该在同一个灰阶里排队。
      · hover 态用 hover:text-white，不要用更亮的灰——那会变相引入第三档。
    tests/test_ui_badge_style.py 里有守卫钉着这条。
    多行长文（如设置页那两条）图标顶部对齐、只有文字缩进换行。
    """
    with ui.row().classes("items-start gap-2 p-2 rounded w-full no-wrap").style(
            tint("amber")):
        # 图标压到与 text-sm 同高(20px)并锁行高，才能跟首行文字齐平、也不被长文挤掉
        ui.icon("warning").classes("text-amber-400 shrink-0").style(
            "font-size:20px;line-height:20px")
        # grow：把 action 按钮推到右端。不带 action 的调用点只是让文字块占满剩余宽度，外观不变。
        ui.label(text).classes("text-sm text-amber-400 min-w-0 grow")
        for _lab, _icon, _cb in ([action] if isinstance(action, tuple) else (action or [])):
            ui.button(_lab, icon=_icon, on_click=_cb).props(
                "flat dense color=amber").classes("btn-sm shrink-0 self-start")


def recent_table(rows, name_label: str, on_row_click=None) -> None:
    """『新入库』表：rows 已构造好(id/time/name/src/raw/status/status_color)，name_label 为番名列标题；
    番名下再压一行灰色原始种子名（长名换行、完整显示）；状态列渲染成彩色徽标（含 qB 实时态）。
    番剧表与剧场版共用。on_row_click(row) 非空时，番名可点开详情（传回整行 dict）。"""
    tbl = ui.table(
        columns=[
            {"name": "time", "label": "时间", "field": "time", "align": "left"},
            {"name": "name", "label": name_label, "field": "name", "align": "left"},
            # 窄屏隐去次要列。【用 Quasar 的 gt-xs，不用 hidden sm:table-cell】(r37) Quasar 的 .hidden 带
            # !important，压过了 sm:table-cell —— 这一列以前在任何宽度下都不显示。gt-xs 只在 <600px 时 display:none。
            {"name": "src", "label": "来源", "field": "src", "align": "left",
             "classes": "gt-xs", "headerClasses": "gt-xs"},
            {"name": "status", "label": "状态", "field": "status", "align": "left"},
        ],
        rows=rows, row_key="id",
    ).classes("w-full")
    # Quasar 的默认空态是英文 "No data available" 配一个警告三角——全站唯一一处英文，
    # 而新库第一次打开首页就会看到它。用中文插槽换掉（模板里不能出现半角引号）。
    tbl.add_slot("no-data", """
        <div class="full-width row flex-center q-py-md text-gray-400" style="font-size:14px">
            还没有记录
        </div>
    """)
    # 番名：可点则染蓝加手型、点击 $emit 回传整行给 Python；不可点则纯文本。原始名恒为灰色小字。
    name_top = ('<div class="cursor-pointer text-blue-400" '
                "@click=\"() => $parent.$emit('opendetail', props.row)\">{{ props.row.name }}</div>"
                ) if on_row_click else "<div>{{ props.row.name }}</div>"
    tbl.add_slot("body-cell-name", f'''
        <q-td :props="props">
            {name_top}
            <!-- 【字号不在这儿定】(R27) 这一处原来写死 10px + gray-400，是全站唯一一档 10px
                 （其余只有 12/14/18），而同一份内容（原始种子标题）在别的五处全是
                 text-xs(12px)/gray-500 —— 按钮、徽标、分页、输入框的尺寸刚被逐条收成
                 "只在一处定"，唯独这一处漏在 Vue 插槽字符串里、谁也扫不到。
                 warn_banner 的注释也把"原始种子标题"明确列在 gray-500 那一档。 -->
            <div class="text-xs text-gray-500"
                 style="white-space:normal;word-break:break-all">
                {{{{ props.row.raw }}}}
            </div>
        </q-td>
    ''')
    if on_row_click:
        tbl.on("opendetail", lambda e: on_row_click(e.args))
    tbl.add_slot("body-cell-status", r'''
        <q-td :props="props">
            <q-badge :color="props.row.status_color || 'blue-grey'" :label="props.row.status" />
        </q-td>
    ''')


# bgm 官方评分档位文案：无独立接口，按分数四舍五入到整数分本地映射（4→较差 6→还行 …）
_BGM_RATING_TIERS = {
    1: "不忍直视", 2: "很差", 3: "差", 4: "较差", 5: "不过不失",
    6: "还行", 7: "推荐", 8: "力荐", 9: "神作", 10: "超神作",
}


def rating_label(score) -> str | None:
    return _BGM_RATING_TIERS.get(round(score)) if score else None


def cover_thumb(url) -> str:
    """bgm 封面原图 → c 尺寸（150×212），给列表/卡片的小图用。

    库里存的是 bgm 接口给的 large 原图，实测 80~500KB 一张；c 尺寸 8~20KB，两者路径只差
    `/pic/cover/l/` ↔ `/pic/cover/c/`，换个地址就行、不用再请求 bgm。图由浏览器直接去 bgm 图床拉，
    不经过本服务；图床缓存头 30 天。详情页的大图仍用原图。不是 bgm 图床的地址原样返回。"""
    if url and "/pic/cover/l/" in url:
        return url.replace("/pic/cover/l/", "/pic/cover/c/", 1)
    return url or ""


def finished_tip(a, resub_hint: str) -> str:
    """『已下齐』徽标的悬停说明，详情页与番剧卡片共用。停订与否由全局开关决定，所以必须说清
    【当前这台机器上】它意味着什么，否则用户看到"已下齐"无从判断后台还会不会继续下。
    【叫"下齐"不叫"完结"】系统确认不了一部番真的播完了，判据只是"按 bgm 总集数，1~N 集都在手"。
    resub_hint：在当前位置怎么找到『继续订阅』（详情页就在右边、卡片上要进详情页）。"""
    return (f"按 bgm 总集数，1~{a.total_episodes} 集全部到手（判定于 {a.finished_at.strftime('%Y-%m-%d %H:%M')}）。"
            + (f"已停止自动下新集——若这部番其实还没完（bgm 的总集数少记了），{resub_hint}。"
               if config.ANIME_FINISH_UNSUB
               else "仅作标记，后台仍会继续下新集（『下齐后停止自动下新集』开关在设置页）。"))


def meta_card(cover_url, kv_pairs, bangumi_id, summary, rating=None) -> None:
    """详情元信息卡：封面 + bgm 链接（图下） + 两列 kv 网格 + 右上角豆瓣式评分（分+评价） + 简介。
    番剧/剧场版详情共用，kv_pairs=[(标签, 值)...] 各页自备（字段集略不同）。"""
    with ui.card().classes("w-full"):
        with ui.row().classes("gap-4 items-start w-full flex-col sm:flex-row sm:flex-nowrap"):   # 窄屏竖排；宽屏三列不换行(长标题在中列内换行、不把评分挤下去)
            # 左列：海报原图完整（不裁）——锁定高度、宽度随图片自然比例走（原生 img：高定死、宽 auto）
            if cover_url:
                # cover_url 来自 bgm 接口（第三方、不可信）。ui.html 是原样注入，不转义的话
                # 一个引号就能闭合 src 属性、往 <img> 上挂任意属性（onerror=… 即 XSS）。
                # 这里仍用原生 img 而不是 ui.image：本列要的是"高定死、宽随比例"，q-img 做不到。
                ui.html(f'<img src="{escape(cover_url, quote=True)}" '
                        f'style="height:18.5rem;width:auto" class="rounded">').classes("shrink-0 w-fit")
            # 中列：两列 kv 网格 + bgm 链接。self-stretch 让本列撑到封面等高，bgm 链接 mt-auto 贴底
            with ui.column().classes("gap-1 grow min-w-0 self-stretch"):
                with ui.grid(columns=2).classes("w-full gap-x-8 gap-y-1 items-baseline").style(
                        "grid-template-columns:auto minmax(0,1fr)"):
                    for pair in kv_pairs:
                        if pair is None:                 # 分隔行：留一点空当，把非 bgm 字段隔开
                            ui.element("div").style("height:0.4rem")
                            ui.element("div")
                            continue
                        kk, vv = pair
                        # 左侧字段名＝说明文（gray-400），右侧的值是【正文】——正文不着灰，
                        # 用默认前景色。全站只保留两档灰，见 warn_banner 的说明。
                        ui.label(kk).classes("text-sm text-gray-400")
                        ui.label(str(vv) if vv not in (None, "") else "—").classes("text-sm")
                if bangumi_id:  # bgm 链接：mt-auto 贴到中列底部；kv 顶满时被顶着往下走
                    ui.link(f"bgm.tv/subject/{bangumi_id}",
                            f"https://bgm.tv/subject/{bangumi_id}").props(
                        "target=_blank").classes("text-xs mt-auto")
            # 右列：大号分数 / 中文评价，竖排右对齐
            if rating:
                with ui.column().classes("gap-0.5 shrink-0 items-end"):
                    ui.label(f"{rating:g}").classes(
                        "text-3xl font-bold text-amber-400 leading-none")
                    _lab = rating_label(rating)
                    if _lab:
                        ui.label(_lab).classes("text-xs text-gray-400")
        if summary:
            ui.separator()
            ui.label(summary).classes("text-sm whitespace-pre-wrap")   # 简介是正文，不着灰


def name_of(a) -> str:
    return a.display_name or a.title


def season_label(a):
    """季徽标文案：第2季起显示『第N季』，第1季不显示。"""
    return f"第{a.season}季" if (a.season or 1) > 1 else None


def platform_spec(obj):
    """bgm 判定为非 TV（剧场版/OVA/WEB…）时的紫色类型徽标 (文案, 颜色, 悬停说明)；TV 或未知返回 None。

    冷门/怪格式常被当周更番收进番剧表，但 bgm 可能识别成剧场版/OVA——打个紫标好一眼看出、自行判断。"""
    p = getattr(obj, "platform", None)
    return (p, "deep-purple", "bgm 判定的类型（非 TV）") if p and p != "TV" else None


def sources_spec(sources):
    """多源/单源徽标 (文案, 颜色, 悬停说明)：多源(>1)蓝、单源灰，悬停列出来源；没有来源返回 None。"""
    if not sources:
        return None
    n = len(sources)
    return ((f"多源 {n}", "blue") if n > 1 else (f"单源 {n}", "blue-grey")) + ("来源: " + " · ".join(sources),)


def cover_box(cover_url, *, dim: bool = False, top_left=(), top_right=(), bottom_left=(), bottom_right=()):
    """封面卡片的封面区：番剧表/待确认/已忽略/剧场版共用，四个角的位置全站一致。返回容器，调用方可再挂点击。

    每个角是一串徽标 (文案, 颜色, 悬停说明或 None)，None 项跳过；同一个角有多个时上下叠放
    （而不是并排），这样左右两个角各有两三个徽标时也不会挤在一起。dim：封面置灰。

    【封面】c 尺寸小图（见 cover_thumb），浏览器直连 bgm 图床、不经过本服务也不调 bgm 接口；
    q-img 默认 loading=lazy，只拉滚到附近的图；ratio 先占住高度，图慢慢来时网格不跳。"""
    box = ui.element("div").classes("relative w-full")
    with box:
        thumb = cover_thumb(cover_url)
        if thumb:
            # no-transition：q-img 默认每次挂载都淡入一次，面板一重建，满屏封面就一起闪一下
            img = ui.image(thumb).props("ratio=0.707 no-spinner no-transition").classes("w-full")
            if dim:
                img.style("filter:grayscale(1);opacity:.45")
        else:
            with ui.element("div").classes(
                    "w-full flex items-center justify-center text-gray-500").style(
                    "aspect-ratio:150/212;background:rgba(255,255,255,.04)"):
                ui.icon("image_not_supported", size="28px")
        for specs, pos in ((top_left, "top-1 left-1 items-start"), (top_right, "top-1 right-1 items-end"),
                           (bottom_left, "bottom-1 left-1 items-start"),
                           (bottom_right, "bottom-1 right-1 items-end")):
            specs = [x for x in specs if x]
            if not specs:
                continue
            with ui.column().classes(f"absolute {pos} gap-1"):
                for text, color, tip in specs:
                    b = ui.badge(text).props(f"color={color}")
                    if tip:
                        b.tooltip(tip)
    return box


def cover_title(name: str, struck: bool = False):
    """封面卡片上的标题：最多两行（.cover-card-title），悬停看全名。struck：灰色删除线（已忽略）。"""
    lbl = ui.label(name).classes(
        "cover-card-title " + ("text-gray-500 line-through" if struck else "text-blue-400"))
    lbl.props["title"] = name          # 两行截断后，悬停看全名
    return lbl


def poll_time_text(iso: str) -> str:
    """运行时键里的 ISO 时间 → 徽标短文案：今天只给 HH:MM，别的日子给 MM-DD HH:MM，空给『从未』。"""
    if not iso:
        return "从未"
    try:
        from datetime import datetime as _dt
        t = _dt.fromisoformat(iso)
    except ValueError:
        return iso
    return t.strftime("%H:%M") if t.date() == _dt.now().date() else t.strftime("%m-%d %H:%M")


def poll_stale(iso: str, interval_s: int) -> bool:
    """『上次采集』是不是已经陈旧：超过两个轮询间隔没有完成新的一轮。空（从未）不算陈旧 —— 那一格另有文案。

    采集关着 / 业务库停摆 / 上一轮被维护打断（半轮不记）都会让时间戳冻住，这一格必须能变红，
    否则"坏了"和"好着"又长得一模一样（核实意见）。
    """
    if not iso:
        return False
    try:
        from datetime import datetime as _dt
        return (_dt.now() - _dt.fromisoformat(iso)).total_seconds() > 2 * max(60, int(interval_s or 0))
    except ValueError:
        return False


def ep_str(e) -> str:
    if e == -1:
        return "SP"
    if e == -2:
        return "?"
    return str(int(e)) if float(e).is_integer() else str(e)


def ep_ranges_text(eps) -> str:
    """升序整数集号压成区间文字：[1, 8, 9, 10, 11, 12] → "1、8～12"。"""
    parts, run = [], []
    for k in eps:
        if run and k == run[-1] + 1:
            run.append(k)
        else:
            if run:
                parts.append(str(run[0]) if len(run) == 1 else f"{run[0]}～{run[-1]}")
            run = [k]
    if run:
        parts.append(str(run[0]) if len(run) == 1 else f"{run[0]}～{run[-1]}")
    return "、".join(parts)


def ep_label(e, single_work: bool = False) -> str:
    """列表里的集号文案：正集『第3集』、未知集『第?集』、特别篇『SP』（不套『第…集』，以前拼出来是『第特别集』）。
    single_work：番是单部作品，其 -2 也按特别篇标 SP（判据见 engine.is_special_ep）。"""
    return "SP" if engine.is_special_ep(e, single_work) else f"第{ep_str(e)}集"


# qB 原始态 → 中文：词表（分类 + 中文名）统一在 core.engine._QB_STATES，本层只负责翻译，
# 不再自建一份——历史上两边各记一份导致 moving/checkingResumeData 只有 UI 认识、
# engine 集合漏收，进而『已完成』长期少记（见 AUDIT.md B2/B3）。
# 下载完成后的各种做种/暂停态一律显示『已完成』，不再对用户区分做种（B6）。


def qb_state_cn(state: str) -> str:
    """qB 原始态 → 中文；词表里没有的原样返回（新 qB 版本冒出的未知态不至于显示成空白）。"""
    return engine.QB_STATE_CN.get(state or "", state or "")


def human_size(n) -> str:
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}PB"


def qb_live_text(t) -> str:
    """种子的 qB 实时态一行文案，如『下载中 45% ↓2.1MB/s』/『已完成 100%』；无实时态返回 ''。"""
    # 这些 status 优先于任何 qB 残留态，返回 '' 让调用方回落到 torrent_status_cn(status)：
    #   · deleted/excluded：删除/排除只改 status、不清实时态，不拦住会显示成『已归档』或『已完成 100%』；
    #   · stalled：判停滞时同样只改 status，qb_state 仍是判定前那一刻的值（downloading/stalledDL…），
    #     而 stalled 已脱离轮询、这个值永远不会再更新——不拦住就恒显示『下载中 12%』，
    #     『停滞』这个标记在详情页/新入库根本到不了，而停滞集恰恰是设计上要人工处理的那些。
    if getattr(t, "status", "") in engine.MANUAL_TERMINAL_STATUSES or \
            getattr(t, "status", "") == "stalled":
        return ""
    #   · downloading：(E-49) 从已归档/停滞重下时占位段**不再清**归档标记与旧实时态（崩溃后要还原），
    #     所以交付中的那 ≤180 秒行上还挂着旧 archived_at / 旧 progress —— 不拦住会显示成『已归档』
    #     或『已完成 100%』，而它此刻正在被重新交付。回落到 torrent_status_cn 的『交付中』。
    if getattr(t, "status", "") == "downloading":
        return ""
    if getattr(t, "archived_at", None):
        return "已归档"          # 完成后已从 qB 移除(留文件)、不再跟踪
    if not getattr(t, "qb_state", ""):
        return ""
    parts = [qb_state_cn(t.qb_state)]
    pr = t.qb_progress or 0
    parts.append(f"{pr * 100:.0f}%")
    if (t.qb_dlspeed or 0) > 0:
        parts.append(f"↓{human_size(t.qb_dlspeed)}/s")
    return " ".join(parts)


# 状态 → 徽标色的【严重度】部分，全站唯一一份。没列进来的状态是中性（blue-grey）。
# 这张表存在的理由：同一个 error 以前在番剧侧是红的、在剧场版侧是灰的——
# 而两边渲染的是同一列 status。颜色是最先被读到的信号，不该按渲染路径分叉。
SEVERITY_COLOR = {"error": "red", "stalled": "deep-orange"}


def live_status(status, qb_state="", qb_progress=0, qb_synced_at=None,
                qb_dlspeed=0, in_plan=None, confirmed=True, episode=None,
                auto_off: str = "", retry_at=None, single_work: bool = False,
                locked_out: bool = False) -> tuple[str, str]:
    """新入库/正在下载：把一条种子压成 (文案, 徽标色)，复刻详情页那套阶梯。

    有 qB 实时态 → 『下载中 X% ↓速度』/『做种 100%』(完成绿、在下蓝)；否则 in_plan 非空(番剧)时
    区分待下『将下载/备用』、失败『可补下/失败』；再否则按 torrent_status_cn（刚交付未同步→下载中）。
    in_plan=None 表示不区分首选/备用（剧场版没有集去重，用这个）。
    confirmed=False：番未确认（待确认），其待下不显示将下载/备用（那要点确认才会下），而显示『待确认』。
    single_work：番是单部作品，其 -2 按『特别篇』而不是『未知集』显示（判据见 engine.is_special_ep）。
    locked_out：这条被锁定源挡住、这一集一份都没有 → 『锁源缺集』，不是『备用项』（判据见 core.anime.locked_out_torrent_ids）。"""
    # 同 qb_live_text：人工终态与停滞的 qB 残留态是陈旧的，别拿它盖住真实 status
    if status in engine.MANUAL_TERMINAL_STATUSES or status == "stalled":
        return (torrent_status_cn(status, qb_progress, qb_synced_at),
                SEVERITY_COLOR.get(status, "blue-grey"))
    if status == "downloading":     # 交付中的占位：旧实时态是陈旧的（理由见 qb_live_text，E-49）
        return torrent_status_cn(status, qb_progress, qb_synced_at), "blue"
    if qb_state:
        pr = qb_progress or 0
        parts = [qb_state_cn(qb_state), f"{pr * 100:.0f}%"]
        if (qb_dlspeed or 0) > 0:
            parts.append(f"↓{human_size(qb_dlspeed)}/s")
        return " ".join(parts), ("green" if pr >= 1 else "blue")
    if in_plan is not None:
        # 下面这两支是 engine.DOWNLOADABLE_STATUSES 的【渲染侧对偶】：调用方(pages/anime.py)按同一集合
        # 决定要不要算 in_plan，这里再逐个状态分支出文案。它不是集合判断故无法直接引用常量——
        # 将来往 DOWNLOADABLE_STATUSES 加成员时，这里必须同步加分支，否则新状态会静默掉进下面的兜底。
        if status == "pending":
            # 【判序与 core.anime.pending_breakdown 一致：未知 → 番级原因 → 计划】
            # 那边把 -1/-2 列为【最先判】，理由写得很清楚："不论番确不确认"，
            # 否则卡片数与点开的列表条数对不上。这里以前先判 confirmed，两处结论相反。
            if episode is not None and episode < 0:
                # 特别篇不用处理（中性色、只作提示），未知集才要人工补集号——与仪表盘两个桶同一判据
                return (("特别篇", "blue-grey") if engine.is_special_ep(episode, single_work)
                        else ("未知集", "purple"))
            if auto_off:
                # 番级原因（待确认/已忽略/已完结）：三者都不会自动下，但**指引不同**。
                # 以前一律显示『待确认』并把用户指去那个 tab，而已忽略/已完结的番不在里面，
                # 用户到那儿只会看到一个空列表。
                return auto_off, ("orange" if auto_off == "待确认" else
                                  "grey" if auto_off == "已忽略" else "teal")
            if not confirmed:
                return "待确认", "orange"
            if retry_at:
                # 暂时性失败排队中（retry_at 非空）：它仍是待下、也不在本轮计划里，但说它是『备用项』
                # （同集已有更优版本）是错的 —— 详情页对同一行说的是『重试中』。(R36 体检 logic-5)
                return "重试中", "orange"
            if in_plan:
                return "将下载", "blue"
            return ("锁源缺集", "orange") if locked_out else ("备用项", "blue-grey")
        if status == "error":
            return ("失败·可补下", "orange") if in_plan else ("失败", "red")
    # 【严重度配色对所有渲染路径一致】兜底这一支以前一律给中性灰，于是 error 在
    # in_plan is None 的路径（剧场版全线）上被压成灰色，而番剧侧同一个状态是红的：
    # 同一件事在两条线上一个像告警、一个像普通信息。颜色是这套界面里最先被读到的信号，
    # 它不该取决于"这条渲染路径当初是谁写的"。
    return torrent_status_cn(status, qb_progress, qb_synced_at), SEVERITY_COLOR.get(status, "blue-grey")


def paginate(seq: list, page: int, size: int):
    """把 seq 按每页 size 切片。返回 (本页元素, 总页数, 收敛后的页码)。

    页码越界时夹到合法范围（数据变少后停在最后一页而非空页）。
    """
    total = max(1, (len(seq) + size - 1) // size)
    page = max(1, min(page, total))
    return seq[(page - 1) * size:page * size], total, page


def page_visibility(on_visible=None) -> dict:
    """页面在不在前台：返回 {"on": bool}，定时器回调先看它再决定刷不刷。(r37 #20)

    NiceGUI 的 ui.timer 在服务端跑，浏览器标签页切到后台它也照跑。番剧页默认停在仪表盘，打开站点就会
    留一个：每 30 秒 38 条 SQL / 约 1.7 MB 数据、MySQL 上约 150 ms 卡在事件循环上 —— 一个没人看的标签页
    一天约 4.9 GB 的库→应用流量。浏览器的 visibilitychange 告诉服务端前后台；回到前台时调 on_visible
    立刻补刷一次（不必等下一个 30 秒）。"""
    state = {"on": True}

    def _h(e):
        v = e.args[0] if isinstance(e.args, (list, tuple)) and e.args else e.args
        was, state["on"] = state["on"], bool(v)
        if state["on"] and not was and on_visible is not None:
            on_visible()
    ui.on("autorss_visibility", _h)
    ui.add_body_html("<script>document.addEventListener('visibilitychange',"
                     "()=>{try{emitEvent('autorss_visibility',!document.hidden)}catch(e){}});</script>")
    return state


def expand_collapse_bar(state: dict, refresh) -> None:
    """一行『全部展开 / 全部收起』小按钮：把展开意图记进 state['expand']（True/False）再刷新面板。

    通过持久状态 + 整体重建来生效，故即便分页翻页，展开/收起也对所有页一致，而非只影响当前页那几个。
    渲染分组时用 state['expand']（None=各分组按自身默认）决定每个 ui.expansion 的初始开合。
    """
    def _set(v):
        state["expand"] = v
        state.get("open", {}).clear()   # 逐个手动开合的记忆作废：以这次全局展开/收起为准
        refresh()
    with ui.row().classes("items-center gap-4 pl-1 pb-2"):
        for text, val in (("全部展开", True), ("全部收起", False)):
            ui.label(text).classes(
                "cursor-pointer text-sm text-gray-500 hover:text-white transition-colors").props(
                "tabindex=0").on("click", lambda v=val: _set(v)).on("keydown.enter", lambda v=val: _set(v))


async def confirm(title: str, note: str = "", ok_label: str = "确定",
                  ok_icon: str = "", ok_color: str = "negative") -> bool:
    """弹一个确认框，等用户选择，用完即销毁自身（不残留隐藏 dialog 累积）。返回是否点了确认。

    【必须建在 client.layout 里】ui.dialog 会在【调用方当前槽位】种一个隐藏 canary 元素，
    并对它挂 weakref.finalize(→ dialog.delete())。而调用方常常是「先 refresh 面板、再 await confirm」
    的处理器（如改季度后问要不要搬文件）；refresh 不被 await 时会排成 asyncio 任务，
    恰好在 `await dlg` 这个挂起点才真正执行 container.clear() —— 把刚种下的 canary 一起清掉，
    对话框在显示前就被销毁，await 永不返回、处理器协程静默泄漏，用户只看到操作"没反应"。
    包进 client.layout 后 canary 落在页面级槽位，面板 refresh 碰不到它；销毁仍由末尾 finally 负责。
    """
    with context.client.layout, ui.dialog() as dlg, ui.card().style("max-width:92vw"):
        ui.label(title).classes("font-bold")
        if note:
            # pre-line：保留 note 里的换行（如"文件仍在 <路径>"这类要单独成行的提示），
            # 同时仍按宽度自动折行；单行 note 的显示不受影响。
            ui.label(note).classes("text-xs text-gray-400").style(
                "white-space:pre-line;word-break:break-all")
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("取消", on_click=lambda: dlg.submit(False)).props("flat")
            ok = ui.button(ok_label, on_click=lambda: dlg.submit(True)).props(f"color={ok_color} unelevated")
            if ok_icon:
                ok.props(f"icon={ok_icon}")
    try:
        return bool(await dlg)         # 点叉/点外部关闭 → None → False（当取消）
    finally:
        dlg.delete()


async def confirm_bind_merge(anime_id: int, bgm_id: int, kind: str = "anime") -> bool:
    """绑定 bgm 之前的回显闸。返回 True 表示可以继续绑。

    **两个调用点必须共用这一份**（详情页『绑定 bgm』、列表页待识别的『绑定』）——
    `bind_anime_bgm` 的注释就点名了这两处，而本项目最常见的缺陷形状正是"同一件事只改了一半"。

    为什么要挡：这个按钮看着只是"改个 ID"，实际可能【删掉另一条番记录】。
    bind_anime_bgm 末尾的身份守卫会把占用同一个 bgm_id 的番 `_merge_anime` 过来，
    而合并的最后一步是 `s.delete(loser)`，没有撤销入口。用户可能刚在列表里看着那条
    『追番中、已经下过几集』的番，一次绑定之后它就没了。

    只在【真的会触发合并】时弹框：不触发合并的绑定（绝大多数）照常一次点击完成，不加噪。
    """
    # 【两条线共用这一份】kind="movie" 走剧场版的对称实现。番剧侧补了闸而剧场版侧没补，
    # 正是本项目最常见的广度错误；tests/test_bind_preview.py 的 AST 守卫现在两边都扫。
    if kind == "movie":
        from core import movies as _mod
    else:
        from core import anime as _mod
    pv = _mod.bind_preview(anime_id, bgm_id)
    if not pv["merge"]:
        return True
    lines = [f"bgm {bgm_id} 已被另一条番占用。绑定会把它并过来，并【删除】那条记录："]
    for m in pv["merge"]:
        what = "片" if kind == "movie" else "番"
        lines.append(f"  · {'movie' if kind == 'movie' else 'anime'}#{m['id']}「{m['name']}」【{m['state']}】")
        lines.append(f"    {m['torrents']} 条种子"
                     + (f"（其中 {m['handled']} 条已下）" if m["handled"] else "")
                     + (f"、{m['aliases']} 条别名" if m["aliases"] else "")
                     + f"将迁到本{what}，该{what}记录被删除")
    for w in pv["warn"]:
        lines.append("")
        lines.append("⚠ " + w)
    return await confirm(f"这次绑定会合并两条{'剧场版' if kind == 'movie' else '番'}记录",
                         "\n".join(lines),
                         ok_label="仍然绑定", ok_icon="link")


async def require_bind_confirm(obj_id: int, bgm_id: int, kind: str = "anime") -> bool:
    """绑定 bgm 之前的【两道】闸：先回显要绑到哪一部，再回显会不会删记录。返回 True 表示继续。

    【为什么必须回显番名】(E-13，2026-09-01 拍板) 收紧 `parse_bgm_id` 的正则挡得住
    "把一段番名/一条 Mikan 链接粘进来"，**挡不住记错一位**——而绑定末尾的身份守卫
    `_merge_anime` / `_merge_movie` 会删掉另一条记录，没有撤销入口。
    "你要绑的是《XXX》"这一句是唯一能让人当场发现填错的东西：
    id 错一位取回的多半是一部完全不相干的作品，名字一摆出来就看出来了。

    两道闸的先后有讲究：先问"是不是这一部"（用户能判断），再问"要不要删那条"（后果更重）。
    反过来的话，用户是在还不知道自己填错了的情况下去回答那个更重的问题。

    取不到 bgm 资料时【不放行】：那说明这个 id 在 bgm 上不存在，或者此刻网络不通——
    两种情况下都不该继续往库里写一个我们自己都没核实过的绑定。
    """
    from services import enrich
    info = await enrich.fetch_by_id(bgm_id)
    if not info:
        ui.notify(f"bgm 上取不到 subject {bgm_id}（ID 不存在，或此刻连不上 bgm）。"
                  "没有核实过的绑定不会写进库里。", type="negative")
        return False
    name = info.get("display_name") or info.get("jp_name") or "(无名)"
    meta = " · ".join(x for x in (
        info.get("air_date") or "", f"{info['total_episodes']} 集" if info.get("total_episodes") else "",
        info.get("platform") or "") if x)
    if not await confirm(f"绑定到《{name}》？",
                         f"bgm {bgm_id}" + (f"\n{meta}" if meta else "")
                         + "\n\n对不上的话就是 ID 填错了——绑定之后元数据、封面、归档目录都会跟着变，"
                           "若这个 bgm 已被另一条记录占用还会【删掉】那一条。",
                         ok_label="就是它", ok_icon="link", ok_color="primary"):
        return False
    return await confirm_bind_merge(obj_id, bgm_id, kind=kind)


# ---- 恒定 head html（内容与运行时状态无关，提到模块级，frame() 每次渲染直接注入、免重复拼接）----
# 封面等图不带 Referer 去 bgm 图床：万一 bgm 哪天按 Referer 防盗链也不裂，且不泄露访问者来源
_HEAD_REFERRER = '<meta name="referrer" content="no-referrer">'
# 整页重载防白闪 + 顶栏/标签不做加载时的怪过渡：html 直接上暗底；关掉 q-header/q-tab 的加载过渡。
_HEAD_PRELOAD = (
    "<style>"
    "html,body{background:#121212}"                                  # html+body 都上暗底，重载瞬间不白闪
    ".q-header{transition:none!important}"                            # 顶栏底色不白→灰淡入
    ".q-tab,.q-tab__indicator,.q-tabs__content{transition:none!important}"  # 标签/指示器不做加载动效
    "html.preload *{transition:none!important}"                      # 加载期禁掉一切过渡
    "</style>"
    "<script>document.documentElement.classList.add('preload');"
    "addEventListener('load',function(){setTimeout(function(){"
    "document.documentElement.classList.remove('preload')},600)});</script>")
# 全站去卡片阴影，改成扁平 + 一条细边（统一风格）
_HEAD_BASE_CSS = (
    "<style>"
    "body{font-size:14px}"   # 基础字号 14px：没显式定大小的继承文字统一 14；番名单独 text-lg(18px)
    ".q-card{box-shadow:none!important;border:1px solid rgba(255,255,255,.08)}"
    ".q-table__container,.q-table__card,.q-table{box-shadow:none!important}"
    # 【这里不再列 .q-table .q-badge】徽标字号由下面 _HEAD_BADGE_CSS 里那条 !important 一处定，
    # 留在这里只会变成"同一件事写在两处"——两处一旦漂开，表格里的徽标会和别处不一样大。
    ".q-table tbody td,.q-table thead th{font-size:14px}"
    # KPI 每组：窄屏 2 列（2×2 不裁右边），≥860px 时按卡数 n 列一排
    ".kpi-group{display:grid;gap:.75rem;grid-template-columns:repeat(2,minmax(0,1fr))}"
    # 窄屏两列时，一组是奇数张（番剧仪表盘 5:5）就让【第一张】横跨整行，别让落单的只占半行留个空洞。
    # 每组第一张是总量（订阅中 / 已交付），放大它比放大排在最后的那张顺眼
    "@media(max-width:859px){.kpi-group>:first-child:nth-last-child(odd){grid-column:span 2}}"
    "@media(min-width:860px){.kpi-group{grid-template-columns:repeat(var(--kpi-n,4),minmax(0,1fr))}}"
    # 数字输入：隐去浏览器原生上下箭头，统一成纯输入框
    "input[type=number]::-webkit-inner-spin-button,input[type=number]::-webkit-outer-spin-button"
    "{-webkit-appearance:none;margin:0}input[type=number]{-moz-appearance:textfield}"
    # 设置页数字项栅格：桌面每格 1/4 宽（4 列），窄屏落到 2 列；顶端对齐
    ".field-grid{display:grid;gap:.75rem 1.25rem;grid-template-columns:repeat(2,minmax(0,1fr));align-items:start}"
    "@media(min-width:760px){.field-grid{grid-template-columns:repeat(4,minmax(0,1fr))}}"
    # 顶栏品牌：只要汉堡在（<1024，与 lg:hidden 同断点）就绝对居中——汉堡在左、品牌居中、动作在右；
    # ≥1024 汉堡消失、内联导航接管居中位，品牌回到静态靠左跟导航同排。两个断点必须一致，否则会出现
    # 『汉堡在、品牌却还靠左』的错位。
    "@media(max-width:1023px){.brand-center{position:absolute;left:50%;top:50%;"
    "transform:translate(-50%,-50%);margin:0!important}}"
    # 顶栏『跳转到』悬浮下拉：自写 :hover 规则（不依赖 Tailwind 的 group-hover——它在本环境的 Tailwind 构建里不生成）。
    # 菜单本体/条目也全用自写 CSS：block 条目撑满菜单宽，hover 高亮整行铺满（不再是浮在中间的圆角小块）。
    # focus-within 与 hover 并列：display:none 的子树不进 tab 序，只有 :hover 的话
    # 键盘用户永远打不开这个菜单（≥1024px 时汉堡是隐藏的，没有第二条路）。
    ".jumpdd .jumpmenu{display:none}"
    ".jumpdd:hover .jumpmenu,.jumpdd:focus-within .jumpmenu{display:block}"
    ".jumpdd>[tabindex]:focus-visible{outline:2px solid rgba(255,255,255,.45);outline-offset:-2px;border-radius:6px}"
    ".jumplist{background:#1b1e24;border:1px solid rgba(255,255,255,.14);border-radius:8px;"
    "overflow:hidden;padding:4px 0;box-shadow:0 8px 24px rgba(0,0,0,.45)}"
    ".jumplist .jitem{display:block;padding:6px 18px;font-size:14px;line-height:1.5;"
    f"color:{text_token('ink-soft')};white-space:nowrap;cursor:pointer;transition:background .12s}}"
    ".jumplist .jitem:hover{background:rgba(255,255,255,.08)}"
    ".jumplist .jitem{text-decoration:none}"
    # 顶栏导航：用真链接（<a>）而不是可点的 <div>——键盘 Tab 到得了、能中键/新标签打开、
    # 屏幕阅读器也认得。样式全在这里定，免得 Tailwind 的 underline 类与链接默认下划线互相盖。
    ".navlink{text-decoration:none}"
    ".navlink.is-active{text-decoration:underline;text-underline-offset:8px;text-decoration-thickness:2px}"
    # 焦点可见：本站是纯深色背景，浏览器默认的黑色焦点框基本看不见。
    # 【不要在这里写 border-radius】本段在裸 <style> 里、不属任何 @layer，会压过 Quasar 的
    # .q-btn--round{border-radius:50%}，把所有圆形图标按钮压成圆角方块。outline 自己会跟随
    # 元素既有的圆角，不需要我们指定。
    "a:focus-visible,.q-btn:focus-visible,.jitem:focus-visible{outline:2px solid "
    "oklch(70.7% 0.165 254.624);outline-offset:2px}"
    "</style>")
# 全站徽标统一配色：Tailwind 色板映射到 .q-badge。放 @layer overrides 才能压过 Quasar 的 .bg-* !important。
# 封面卡片网格（番剧表/待确认/已忽略/剧场版共用）：按容器宽度自动排列数（桌面多列、手机至少 2 列）。
_HEAD_COVER_CSS = (
    "<style>"
    # width:100% 必须写：季度/年份折叠块的内容区是 align-items:flex-start 的 flex 列，不写的话网格只按
    # 内容收缩成 3 列、右边大片空着（auto-fill 要靠一个确定的宽度才排得出列数）。
    # 列宽下限写成 min(固定宽, 50%-8px)：屏再窄也至少两列，不会掉成一列、封面撑满整屏。
    ".cover-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(160px,calc(50% - 8px)),1fr));"
    "gap:16px;padding:8px 4px;width:100%}"
    ".cover-card:hover{border-color:rgba(255,255,255,.28)!important}"
    ".cover-card-title{font-size:17px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;"
    # overflow-wrap:anywhere 而不是 word-break:break-all：后者把 "Chainsmoker" 这种英文词从中间拆开，
    # 前者只在一个词本身放不下时才断（中文本来就逐字换行，不受影响）
    "overflow:hidden;line-height:1.35;min-height:2.7em;overflow-wrap:anywhere}"
    "</style>"
)

_HEAD_BADGE_CSS = (
    "<style>@layer overrides{"
    ".q-badge.bg-green{background:oklch(72.3% 0.219 149.579)!important}"               # green-500（绿单独调暗）
    ".q-badge.bg-red{background:oklch(70.4% 0.191 22.216)!important}"                  # red-400（红单独调暗）
    ".q-badge.bg-blue,.q-badge.bg-primary{background:oklch(70.7% 0.165 254.624)!important}"  # blue-400
    ".q-badge.bg-blue-grey,.q-badge.bg-grey{background:oklch(70.7% 0.022 261.325)!important}"  # 中性灰统一：gray-400
    ".q-badge.bg-orange{background:oklch(75% 0.183 55.934)!important}"                 # orange-400
    ".q-badge.bg-purple{background:oklch(71.4% 0.203 305.504)!important}"              # purple-400
    ".q-badge.bg-teal{background:oklch(77.7% 0.152 181.912)!important}"                # teal-400
    ".q-badge.bg-indigo{background:oklch(67.3% 0.182 276.935)!important}"              # indigo-400
    ".q-badge.bg-deep-purple{background:oklch(70.2% 0.183 293.541)!important}"         # violet-400
    ".q-badge.bg-amber{background:oklch(82.8% 0.189 84.429)!important}"                # amber-400
    ".q-badge.bg-pink{background:oklch(71.8% 0.202 349.761)!important}"                # pink-400
    # deep-orange（停滞态专用）此前【不在】映射表里，是全站唯一一个走 Quasar 原色的徽标——
    # 偏偏它标的是最需要被看见的状态。补进来，取 orange-500：比 orange-400 更红更沉，
    # 与相邻的 orange（需人工但可挽回）一眼能分开，又仍在同一套色板里。
    ".q-badge.bg-deep-orange{background:oklch(70.5% 0.213 47.604)!important}"          # orange-500
    ".q-btn.text-grey{color:oklch(70.7% 0.022 261.325)!important}"                     # 次级灰按钮→gray-400(灰2)
    # ---- 前景色与字号：与上面的背景是【同一个决定的两半】，必须放在一起改 ----
    # 上面把徽标背景整体换成了 Tailwind 的 -400/-500 档（亮度 67%~83%），而 Quasar 的
    # `.q-badge{color:#fff}` 是写死的白字：amber 上只有 1.7:1、green 2.2:1、orange 2.4:1，
    # 全线低于可读下限。全站的状态语义【只】靠徽标传达，这是最不该看不清的一处。
    # 浅底一律配深色前景 → 同样这批颜色能到 6.6:1~12:1。
    ".q-badge.bg-green,.q-badge.bg-red,.q-badge.bg-blue,.q-badge.bg-primary,"
    ".q-badge.bg-blue-grey,.q-badge.bg-grey,.q-badge.bg-orange,.q-badge.bg-deep-orange,"
    ".q-badge.bg-purple,.q-badge.bg-teal,.q-badge.bg-indigo,.q-badge.bg-deep-purple,"
    ".q-badge.bg-amber,.q-badge.bg-pink,.q-badge.bg-negative,.q-badge.bg-positive"
    "{color:#0b0d10!important}"
    # 实心按钮同病：ui.colors 把 primary/negative 定成了同一档 blue-400/red-400，白字 2.6:1。
    # 只影响实心（bg-*）；flat/outline 用的是 text-*，不在此列。
    ".q-btn.bg-primary,.q-btn.bg-negative{color:#0b0d10!important}"
    # 【徽标的字号、行高、居中：三条必须一起定，只定字号是把同一件事改了一半】
    # 此前列表/详情里的徽标是 12px、仪表盘的带 text-sm 是 14px，同一个『待确认』在同一屏三种大小。
    # 而 Tailwind 的 `text-sm` 同时设 font-size(14px) 与 line-height(20px)：只全局定字号的那一版，
    # 带 text-sm 的那批行高 20px、其余的吃 Quasar 的 `line-height:1`(=14px)——框高 24px vs 18px，
    # 同一屏仍是两种高度，只是不再是两种字号。全站 71 处徽标的 text-sm 已剥净，
    # 调用点不必（也不该）再各写字号，见 tests/test_ui_badge_style.py 的守卫。
    #
    # 【层叠关系：这一段先后写错过两次，第三版逐条实测过，改之前先把实测重跑一遍】
    # NiceGUI 的 templates/index.html 第 13 行【首先】声明了层序（层序由首次声明决定）：
    #     @layer theme, base, quasar, nicegui, components, utilities, overrides, quasar_importants;
    #
    # ① Quasar 的两份 CSS 通过 `@import ... layer(quasar)` / `layer(quasar_importants)` 进层
    #    ——**层是在 @import 那一侧指定的**，所以 grep 它的文件内容看不到任何 @layer。
    #    据此断言"Quasar 不在任何层里"是【错的】（第一版错在这里）。
    # ② Tailwind 由 tailwindcss.min.js 在运行时注入，而它内嵌的样式表逐字写着
    #    `@layer theme, base, components, utilities;` + `@import './utilities.css' layer(utilities)`
    #    ——工具类【进 @layer utilities】。据此断言"Tailwind 不属于任何层、所以压过所有分层规则"
    #    也是【错的】（第二版错在这里）。它那句重复的层序声明是 no-op，排序以 NiceGUI 那句为准。
    # ③ 于是普通声明的实际强弱是：utilities(Tailwind) < overrides(本段) —— 也就是说
    #    **本段的普通声明本来就压得过 Tailwind 工具类和 Quasar 的 .q-badge**，
    #    下面 .q-badge 那五条 !important 在今天【一条都不需要】。留着无害（也挡得住万一的
    #    行内样式），但别再拿"否则压不过 XX"当理由——那句话两个版本都是假的。
    # ④ 真正非要 !important 不可的有两处，理由各不相同：
    #    · 背景色那批：要压 Quasar 写在 `quasar_importants` 层里的 `.bg-*` !important。
    #      !important 声明的层序是【反向】的（早声明的层赢），quasar_importants 排在最末＝最弱，
    #      所以本层的 !important 赢得过它。
    #    · `.q-btn.btn-sm`：Quasar 的 `size=` 渲染成【元素上的行内 style】，
    #      而任何分层样式表规则都输给行内非 important 声明——只有 !important 压得住。
    #
    # inline-flex + align-items:center 把"字在框里居中"从"靠字体度量碰运气"变成布局保证：
    # Quasar 原样式没有 display 规则、靠 vertical-align:baseline 定位，而中日文字形的
    # ascent+descent 超过 1em，line-height 接近 1 时字会偏低、上下留白不等。
    ".q-badge{font-size:14px!important;line-height:1.45!important;"
    "display:inline-flex!important;align-items:center!important;justify-content:center!important}"
    # 【分页的字号也在这里定】(R21) 三处 ui.pagination 此前两种写法：两处 `size=sm`（Quasar 的
    # xs8/sm10/md14/lg20/xl24 档，渲染成**行内** font-size:10px），一处 `dense` ——
    # 而 QPagination 的 props 表里**根本没有 dense**（Vue 把它当 fallthrough attr 扔到根 div 上，
    # 零效果）。于是同一个控件在同一个页面上一个 10px、一个 14px，还有一个写了等于没写。
    # 与徽标/按钮同一条纪律：尺寸由全局一处定，调用点只选角色。
    # 用 !important 的理由同 .btn-sm —— Quasar 的 size= 渲染成行内样式，分层规则压不住。
    ".q-pagination .q-btn{font-size:12px!important}"
    # ---- 按钮只有两档尺度，且【只在这里定】----
    # 默认 14px（Quasar 的 md）＝面板级与主要操作；.btn-sm 12px ＝行内、密集行、元操作行。
    #
    # 【为什么必须是全局 CSS，而不是调用点的 size=sm】Quasar 的 `size=` 渲染成【行内
    # font-size】（xs8 / sm10 / md14 / lg20 / xl24），而本项目要的 12px 不在这张表里——
    # 于是 23 个按钮统统写成 `size=sm` 再叠一句 `.style("font-size:12px")` 把 10 掰回 12。
    # 两层叠加的代价不是难看，是【同一个角色出现两种尺寸】：anime_detail 里同为
    # `flat dense size=sm` 的按钮，一处被掰到 12px、一处 14px，全凭哪个调用点先写。
    # 这与徽标那一节是同一条原则、同一次教训：尺度由全局定，调用点只选【角色】。
    #
    # !important 的理由也同上：要压的是 Quasar 写在元素上的行内 font-size
    # （author !important 胜过 inline 非 important），以及调用点顺手加的 Tailwind 工具类。
    ".q-btn.btn-sm{font-size:12px!important}"
    "}</style>")


def _health_notices() -> None:
    """采集停摆 / 暂停太久 / 源连续失败 / RSS 断档：全站挂一条（判据见 core/health.py，r37 #1 #7）。

    以前这些只体现在仪表盘 tab 的『上次采集』徽标上，恢复后第一轮跑完就变回灰色；生产上推送地址又是空的，
    于是容器停了 15 天、回来之后漏了哪段，全程没有任何地方说过一个字。挂在 frame() 上，任何页面都看得见。"""
    lines = []
    if (p := health.poll_problem()):
        lines.append(p)
    if (sp := health.sources_problem()):
        lines.append(sp)
    gaps = health.gaps()
    if not (lines or gaps):
        return
    tail = "（没配推送地址，这些只在页面上看得见。）" if not config.NOTIFY_URL else ""
    with ui.column().classes("w-full max-w-7xl mx-auto px-2 pt-2 gap-1"):
        for ln in lines:
            warn_banner(ln + tail)
        if gaps:
            def _ack():
                health.clear_gaps()
                ui.navigate.reload()
            warn_banner("RSS 断档：下面这段时间的发布在两轮采集之间滚出了 feed、没抓到，可能漏集——"
                        + "；".join(health.gap_text(g) for g in gaps)
                        + "。去番剧表看缺集提示，用『补齐该源』补；补完点『知道了』。" + tail,
                        action=("知道了", "done", _ack))


def _db_down_notice(detail: str = "") -> None:
    """业务库停摆的说明块：说清是什么、影响什么、怎么办，并给两个出口。

    刻意【不】自动回退到本地 SQLite：那样界面看着正常，实则在往另一份数据集里写，
    等 MySQL 回来这些改动凭空消失（详见 db 模块 _data_down 处的注释）。
    """
    # 【维护中不是"连不上"】(R21) 切库/迁移期间 is_data_down() 也为真（那是有意的：
    # 后台四条循环的把门判据只此一条），但文案必须分开——把一次几秒的、用户自己点出来的
    # 维护说成"数据库连不上，系统已停摆…恢复后 30 秒内自动接上"，是纯粹的误导。
    if (m := db.maintenance_reason()):
        # 【颜色必须取自 token 表】(R22 修) 第一版写的是 tint("orange") —— 而 _TOKENS 里
        # 只有 blue/green/red/amber/grey/ink-soft，**没有 orange**：`tint()` 直接 KeyError，
        # 而这一句在 frame() 的 `try: yield` **之前**执行，兜底够不着 ——
        # 于是维护窗口一开，七个页面【全部构建失败】，偏偏那正是用户盯着屏幕等结果的几十秒。
        # 图标颜色同理，别再写死 oklch（R20 刚清理过一批绕开 token 表的硬编码色）。
        with ui.column().classes("w-full gap-2 p-3 rounded").style(tint("amber")):
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.icon("build").classes("text-xl").style(f"color:{text_token('amber')}")
                ui.label("数据库维护中").classes("text-base font-bold")
            ui.label(f"{m}。这期间采集/下载/同步会跳过，页面数据也读不出来——"
                     "通常只有几秒，完成后刷新即可。").classes("text-xs text-gray-400")
        return
    why = detail or db.data_down_reason() or "未知原因"
    with ui.column().classes("w-full gap-2 p-3 rounded").style(
            tint("red")):
        with ui.row().classes("items-center gap-2 no-wrap"):
            ui.icon("error").classes("text-xl").style("color:oklch(70.4% 0.191 22.216)")
            ui.label("数据库连不上，系统已停摆").classes("text-base font-bold")
        ui.label(f"业务库：{db.data_target_desc()}").classes("text-xs text-gray-400 break-all")
        ui.label(why).classes("text-xs text-gray-500 break-all")
        # 两种停摆的出路完全不同，文案必须分开：连不上的会自愈，配置错/启动失败的不会。
        # 混着写会让用户干等——他以为"30 秒后会自己好"，而那条路根本不存在。
        if db._data_fatal:
            ui.label("采集、下载、qB 同步全部暂停，不会写入任何数据。设置照常可改（配置存在本地）。"
                     "【这类故障不会自动恢复】：到设置页把连接参数补全后点『切到 MySQL』，"
                     "或点『切回本地 SQLite』先用着。").classes("text-xs text-gray-400")
        else:
            ui.label("采集、下载、qB 同步全部暂停，不会写入任何数据。设置照常可改（配置存在本地）。"
                     "数据库恢复后 30 秒内自动接上，不用重启。").classes("text-xs text-gray-400")
        with ui.row().classes("gap-2 flex-wrap"):
            ui.button("立即重连", icon="refresh", on_click=_db_reconnect).props(
                "unelevated color=primary").tooltip("马上探一次；通了就自动恢复并刷新本页")
            ui.button("去设置页改数据库", icon="settings",
                      on_click=lambda: ui.navigate.to("/settings")).props(
                "flat color=primary").tooltip(
                "改连接参数，或手动『切回本地 SQLite』先用着（那是明确选择，不会悄悄发生）")


async def _db_reconnect() -> None:
    if db._data_fatal:
        # 这类停摆不是"连不上"，探测解不了它（探的根本不是目标库）。直接说清该去哪，
        # 别让用户反复点一个注定原地打转的按钮。
        ui.notify("这不是连接问题：到设置页『数据库』补全参数后点『切到 MySQL』，"
                  "或点『切回本地 SQLite』", type="warning")
        return
    # 同 run_db_watch：建连接是同步的，主机被 DROP 时要挂到 connect_timeout。
    # 在处理器里直接调会连页面一起冻住，用户只会觉得"点了没反应"。
    if (m := db.maintenance_reason()):
        # 维护中点『立即重连』：探测返回的就是维护理由，报成"还是连不上"是误导。
        ui.notify(f"{m} —— 维护中，请等它结束（通常只有几秒）再刷新页面", type="info")
        return
    err = await asyncio.to_thread(db.probe_data_engine)
    if err:
        ui.notify(f"还是连不上：{err}", type="negative")
        return
    # 【探通了就【立刻】把欠下的初始化补上，不能等看守协程】probe_data_engine 一成功，
    # is_data_down() 当场变 False，各后台循环下一次醒来就开始交付（写 downloading 行）。
    # 而 run_db_watch 的恢复边沿最多要 30 秒后才轮到——那时它清的就是【正在交付】的行：
    # 动它的占位会当场解除集去重，同一集被两个源各下一份到同一目录（这正是 _stale_sweep_pending
    # 那段注释警告的事）。在这里补做，窗口从 30 秒缩到几毫秒；标记被消费掉之后，看守协程那次会以
    # sweep_leftovers=False 跑，只剩三个幂等操作，无害。
    try:
        await asyncio.to_thread(worker.init_business_state, worker._stale_sweep_pending)
        await worker.sweep_leftovers_if_pending()    # 清扫要 await 问 qB，不能在上面那个线程里做（E-57）
    except Exception as e:
        log.exception("重连后补跑业务初始化失败")
        ui.notify(f"数据库通了，但初始化没跑成：{type(e).__name__}: {e}（看守协程 30 秒后会再试）",
                  type="warning")
    ui.notify("数据库回来了，正在刷新…", type="positive")
    ui.navigate.reload()


# 长操作的按钮状态：全站的【默认写法】，16 个调用点。
# 【"唯一一份"是句假话，别再这么写】此前这里写的是"全站唯一一份"，而实际另有几种并存，
# 每种都有自己的理由，但对用户呈现的是"有的按钮会转圈、有的不会"：
#   · settings『立刻备份』——手写 btn.props("loading") + try/finally（它要在 finally 里
#     刷新备份列表，且成功/失败的 toast 文案各不相同，收编进来反而绕）；
#   · settings『重新激活全部任务』/ anime『重新识别(批量)』/ anime_detail『补齐』——
#     各自一个模块级 busy 字典/集合，去重粒度是【按对象】（按番、按源）而不是按按钮，
#     busy_action 的单一 key 表达不了；
#   · movies『立即扫描』——真正的并发保险在服务端 worker._scan_lock（页面级防抖挡不住
#     后台自动扫描和第二个标签页），但按钮上的 loading 已经补上了。
# 也就是说：**去重粒度不是"这一个按钮"的场合可以不用它，但 loading 一律要有**。
# 【"一律要有"曾经有一半是假的】(R36 体检) 走 busy_action 的调用点里 8 个把处理器写成 `(e=None)`，
# NiceGUI 因此不带参调用、e 恒 None、这里拿到 btn=None —— 去重与异常兜底都在，只有 loading 从没亮过，
# 而现有 tests/test_ui_busy 测的是 busy_action 自己（传进来的 btn 是桩），对签名坑免疫。
# 现在签名是 `(e)`，守卫是真点按钮看 _props（tests/test_health_r36）。更稳的写法是建按钮时把元素捕进
# 闭包传进来、不依赖框架的签名嗅探（3.15 起 handle_event 加了 expect_args，说明那是内部细节）—— 待改。
# 【为什么当初要收】站里本来有三种各自正确的写法，各自只落在一两处，
# 而**十二个**同样耗时的按钮一件都没做：最长的是绑定 bgm / 重新识别那几个——
# _RESOLVE_BUDGET 是 120 秒，期间按钮毫无反应，用户会连点，于是并发跑好几轮、
# 重复弹搬迁确认框。异常兜底同理：NiceGUI 的 on_click 里逃出去的异常只进服务端日志，
# 用户看到的就是"点了没反应"。
_BUSY: dict = {}


async def busy_action(btn, key: str, coro_fn, *, ok: str = "", fail: str = "操作失败"):
    """跑一个长操作：置 loading、按 key 去重、兜住异常、给一句 toast。

    key 是【模块级】去重键（同一个按钮在多个客户端/多次渲染下共用），所以不同页面的
    同类操作要用不同的 key。coro_fn 是一个无参 async 函数（用 lambda 闭包传参）。
    返回 coro_fn 的返回值；被去重挡下或抛异常时返回 None。

    【为什么不做成装饰器】这些处理器的参数五花八门（有的要 refresh 外层、有的要接着问
    搬迁确认），闭包传进来最省事，也不必改各处的签名。

    【调用方的签名必须是 `async def h(e)`，不能是 `h(e=None)`】(R36 体检 ux-1) NiceGUI 的
    `handle_event` 用 `helpers.expects_arguments(handler)` 决定传不传事件，判据是"有没有【无默认值】
    的形参"：`(e=None)` 会被 `handler()` 不带参调用，`e` 恒为 None，这里拿到的 btn 就是 None ——
    loading 那一支整段跳过，8 个 120~240 秒级的按钮按下去全都毫无反应。一个 `=None` 的差别。
    守卫：tests/test_health_r36 按 AST 钉"把 e.sender 交给 busy_action 的处理器至少有一个无默认值形参"。
    """
    if _BUSY.get(key):
        ui.notify("上一次还没跑完，请稍候", type="info")
        return None
    _BUSY[key] = True
    if btn is not None:
        btn.props("loading")
    try:
        res = await coro_fn()
        if ok:
            ui.notify(ok, type="positive")
        return res
    except Exception as e:
        log.exception("按钮操作失败：%s", key)
        ui.notify(f"{fail}：{type(e).__name__}: {str(e)[:160]}", type="negative")
        return None
    finally:
        _BUSY.pop(key, None)
        # coro_fn 里常有 refresh 把这颗按钮所在的区整个重建（overview_tail 每 30 秒也会）：
        # 旧元素已删就别再碰它（核实意见）。
        if btn is not None and not btn.is_deleted:
            btn.props(remove="loading")


@contextmanager
def frame(active: str = ""):
    """页面骨架：暗色 + 顶栏（站名 + 导航 + 右侧动作位）。

    yield 出顶栏右侧的容器，页面可往里放全局动作按钮（如刷新/补下）；不放就是空的。
    """
    ui.dark_mode(True)
    ui.page_title(config.SITE_NAME or "autorss")   # 浏览器标签页标题＝站点名（每次渲染读，改了刷新即变）
    # 全站主色＝blue-400、负色＝red-400，用 oklch（跟徽标/链接同源，P3 屏上也完全同色，不走 sRGB 夹紧）
    ui.colors(primary="oklch(70.7% 0.165 254.624)", negative="oklch(70.4% 0.191 22.216)")
    ui.add_head_html(_HEAD_REFERRER)
    ui.add_head_html(_HEAD_PRELOAD)
    ui.add_head_html(_HEAD_BASE_CSS)
    ui.add_head_html(_HEAD_BADGE_CSS)
    ui.add_head_html(_HEAD_COVER_CSS)
    with ui.header().classes("p-0").style(
            "background:#15171c;border-bottom:1px solid rgba(255,255,255,.08);box-shadow:none"):
        # 内容包进固定 56px 高的行——用内容锁死高度，右侧有没有按钮都不改变（q-header 的 height 会被 quasar 忽略）
        with ui.row().classes("items-center gap-2 w-full px-4 relative flex-nowrap").style("height:56px"):
            # 窄屏(<1024px)：导航+跳转到 全收进汉堡（lg:hidden＝≥1024 隐藏）。断点跟下面的内联导航严格互补，
            # 保证任何宽度下『跳转到』都有着落——要么在内联导航里，要么在汉堡里，不留够不着的空档。
            with ui.button(icon="menu").props("flat round dense color=white").classes("lg:hidden"):
                with ui.menu().props("dark"):
                    for key, label, path in NAV:
                        mi = ui.menu_item(label, on_click=lambda p=path: ui.navigate.to(p))
                        if key == active:
                            mi.classes("text-blue-400 font-semibold")
                    for _name, _url in _jump_targets():   # 跳转到：外链新标签打开（qB后台/Nyaa/Mikan/Bangumi）
                        ui.menu_item(f"{_name} ↗", on_click=lambda u=_url: ui.navigate.to(u, new_tab=True))
            # 品牌：手机绝对居中(.brand-center)，≥640 静态靠左跟导航同排
            with ui.row().classes("items-center gap-2 mr-2 sm:mr-6 brand-center"):
                ui.icon("live_tv").classes("text-2xl").style("color:oklch(70.7% 0.165 254.624)")  # blue-400
                ui.label(config.SITE_NAME or "AutoRSS").classes("text-lg font-bold max-sm:hidden").style(
                    f"color:{text_token('ink-soft')};letter-spacing:.5px")   # 站名用柔和前景；窄屏隐去，只留居中图标
            # 桌面端(≥1024px)：内联导航，绝对定位在顶栏水平居中（不受左侧品牌/右侧按钮宽度影响）。
            # w-max 必须有：绝对元素 left-1/2 的收缩宽度只有父宽 50%，不锁 max-content 会被压缩、把每个项文字挤成两行。
            # flex-nowrap + whitespace-nowrap 双保险，行不换、字不折。
            # max-lg:hidden＝<1024 隐藏改走汉堡菜单（此向可靠，hidden+lg:flex 在本环境无法复原）。
            with ui.row().classes("items-center gap-2 flex-nowrap whitespace-nowrap w-max max-lg:hidden "
                                  "absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2"):
                for key, label, path in NAV:
                    # 【必须是 ui.link 而不是可点的 ui.label】≥1024px 时汉堡（唯一可聚焦的导航）被隐藏，
                    # 接管的却是一串 <div>：键盘用户在桌面宽度下【完全够不着导航】，也没法中键开新标签。
                    cls = "navlink text-sm px-2 transition-colors "
                    cls += ("is-active text-blue-400 font-semibold"
                            if key == active else "text-gray-400 hover:text-white")
                    ui.link(label, path).classes(cls)
                # 跳转到：外链下拉（qB后台/Nyaa/Mikan/Bangumi）。自写 CSS『.jumpdd:hover .jumpmenu』控制显隐——
                # 【鼠标悬浮即显示、移开即收起】，不用点击。不再单独设断点——整条内联导航已是 ≥1024 才出现，
                # 窄屏时它和导航一起收进汉堡（汉堡菜单里已列了同样的外链），不会出现两边都够不着的空档。
                with ui.element("div").classes("relative jumpdd"):
                    # tabindex=0：触发行本身可聚焦，下面 CSS 里 `.jumpdd:focus-within` 那条兜底才是活的
                    # —— 键盘 Tab 到它、或 ≥1024px 的触屏点一下（没有 hover）都能打开。(R36 体检 ux-7)
                    with ui.row().classes("items-center gap-0.5 text-sm px-2 cursor-pointer "
                                          "text-gray-400 hover:text-white transition-colors").props("tabindex=0"):
                        ui.label("跳转到")
                        ui.icon("open_in_new").style("font-size:14px")
                    # jumpmenu 默认 display:none，父 .jumpdd:hover 时 display:block；pt-1 透明桥补触发行与菜单的缝。
                    # 用普通 block div 装条目（非 flex column），条目 display:block 天然撑满、hover 整行铺满。
                    with ui.element("div").classes("jumpmenu absolute left-0 top-full z-50 pt-1"):
                        with ui.element("div").classes("jumplist min-w-max"):
                            for _name, _url in _jump_targets():
                                # 同上：链接而非可点 div。附带修好另一件事——ui.navigate.to(new_tab=True)
                                # 走的是 window.open，会被浏览器的弹窗拦截器当成非用户手势拦下；
                                # <a target="_blank"> 不会（NiceGUI 自己的文档也建议这么换）。
                                ui.link(_name, _url, new_tab=True).classes("jitem")

            ui.space()
            header_right = ui.row().classes("items-center gap-1")  # 页面自定义动作位
            ui.button(icon="refresh", on_click=lambda: ui.navigate.reload()).props(
                "flat round dense color=white").tooltip("刷新本页")
    # 业务库停摆时全站挂一条：任何页面都看得见，别让人对着一个"看着正常"的空界面猜。
    already_notified = db.is_data_down()
    if already_notified:
        with ui.column().classes("w-full max-w-7xl mx-auto px-2 pt-2 gap-1"):
            _db_down_notice()
    else:
        try:
            _health_notices()
        except Exception as e:          # 提示块自己出错不能连累整个页面
            log.warning("采集健康提示渲染失败：%s: %s", type(e).__name__, e)
    # 全站内容宽度 max-w-7xl（1280px）：番剧表卡片一行 6 张左右；所有页面同宽，切页面不跳边框。
    with ui.column().classes("w-full max-w-7xl mx-auto p-2") as body:
        try:
            yield header_right
        except db.DatabaseBusy as e:
            # 整库维护（切库/迁移）期间任何 get_session() 都会抛这个。这不是故障，
            # 也不该走 mark_data_down（维护自己会在结束时把状态清干净）。
            body.clear()
            with body:
                _db_down_notice(str(e))
        except (OperationalError, InterfaceError) as e:
            # 页面主体在这个 with 里跑，异常会被抛回 yield 点——正是唯一的兜底位置。
            # 只接【连接层】异常：schema 出了事（表/列不对）要原样 500 冒出来让人看见，
            # 套上"数据库连不上"反而误导。
            # 【异常类不足以区分】(R21) 原来这里只按类判，注释说"ProgrammingError 才是 schema
            # 问题"——那只在 MySQL 上成立。SQLite（本项目**默认**后端）把 `no such table`
            # 和 `database is locked` 一起抛成 OperationalError，于是缺一张表就会被判成停摆，
            # 而看守协程的 SELECT 1 在同一个文件上必然成功、又把它解除，来回翻转。
            # 判据收进 db.looks_like_connection_error（那里写着完整理由与实测）。
            if not db.looks_like_connection_error(e):
                raise
            first = str(e).splitlines()[0]
            db.mark_data_down(f"{type(e).__name__}: {first}")   # 让全站状态立刻一致，别等看守那 30s
            # 不依赖页面主体画到哪一步：先清空，免得半截残骸叠在提示上面。
            body.clear()
            if not already_notified:      # 进页面时还是好的、渲染中途才断 → 这里才是第一次告诉用户
                with body:
                    _db_down_notice(first)
