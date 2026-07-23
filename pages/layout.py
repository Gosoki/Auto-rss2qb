"""共享布局：顶栏导航 + 统一的内容容器。所有页面都套 frame()。

页面级组件放在自己的页面文件里；这里只放跨页面复用的东西。
"""
import re
from contextlib import contextmanager

from nicegui import ui

import config

NAV = [("manage", "动漫番剧", "/"), ("movies", "OVA・剧场版", "/movies"),
       ("parse", "解析测试", "/parse"), ("settings", "设置", "/settings")]

# 应用侧种子状态 → 中文（番剧表/剧场版/详情/新入库共用）
STATUS_CN = {"downloaded": "已下", "pending": "待下", "downloading": "下载中",
             "error": "失败", "skipped": "跳过", "deleted": "已删", "excluded": "已排除"}
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def torrent_status_cn(status: str, qb_progress=0, qb_synced_at=None) -> str:
    """无 qB 实时态时的回落显示：已交给 qB 但还没首次同步回来（刚发送）→『下载中』；其余按 STATUS_CN
    （含已下完/跳过/已删）。有实时态时调用方用 qb_live_text 显示『下载中 X% / 做种 100%』，不走这里。
    关了状态跟踪（QB_SYNC_STATUS=false）时不读 qB，发送即『已下』，故 downloaded 一律显示已下、不显『下载中』。"""
    if (config.QB_SYNC_STATUS and status == "downloaded"
            and (qb_progress or 0) < 1.0 and qb_synced_at is None):
        return "下载中"
    return STATUS_CN.get(status, status)


def parse_bgm_id(text: str) -> int | None:
    """从用户输入里抠出 bgm subject id：优先 bgm.tv/subject/<id>，退而取任意数字。取不到返回 None。"""
    m = re.search(r"subject/(\d+)", text or "") or re.search(r"(\d+)", text or "")
    return int(m.group(1)) if m else None


def source_options(sources, blank: str = "按优先级") -> dict:
    """下载源下拉选项：{'': 占位(按优先级), 源: 源, ...}。blank 为空选项文案。"""
    return {"": blank, **{s: s for s in sources}}


def group_by_quarter(items):
    """按季度分组，返回 [(季度, [item...]), ...]，季度倒序、未知垫底。items 需有 .quarter。"""
    by_q: dict[str, list] = {}
    for it in items:
        by_q.setdefault(it.quarter or "未知", []).append(it)
    quarters = sorted((q for q in by_q if q != "未知"), reverse=True)
    if "未知" in by_q:
        quarters.append("未知")
    return [(q, by_q[q]) for q in quarters]


def kpi_cards(cards) -> None:
    """一排 KPI 数字卡：cards=[(标签, 数值, 高亮色或'') / (…, on_click) / (…, on_click, 标签色带深浅如'pink-300'), ...]；
    给了 on_click 的卡可点（手型光标+悬浮高亮）。数字染色需值非零且给了高亮色；标签色用来把同类卡分组。
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
        label_color = rest[1] if len(rest) > 1 else None   # 说明文字颜色，缺省灰
        c = ui.card().classes("items-center px-3 py-2" + (
            " cursor-pointer hover:bg-white/5" if on_click else ""))
        if grow:
            c.style("flex:1 1 0")          # 组内各卡等宽铺满，左右平衡
        with c:
            cls = "text-2xl font-bold" + (f" text-{hi}-400" if hi and val else "")
            ui.label(str(val)).classes(cls).style(  # 预留 5 位数宽度：数字增减时卡不抖、各卡等宽
                "min-width:5ch;text-align:center;font-variant-numeric:tabular-nums")
            ui.label(label).classes(
                "text-xs " + (f"text-{label_color}" if label_color else "text-gray-400"))
        if on_click:
            c.on("click", on_click)

    if len(groups) == 1:                   # 单组（如剧场版页）：维持原来的自由换行、左对齐
        with ui.row().classes("gap-3 flex-wrap p-1"):
            for card in groups[0]:
                _card(card, grow=False)
    else:                                  # 多组：每组打包、左右平铺满宽，窄了整组换行成上下布局
        with ui.row().classes("w-full gap-4 flex-wrap items-stretch p-1"):
            for group in groups:
                with ui.row().classes("gap-3 flex-wrap items-stretch").style("flex:1 0 440px"):
                    for card in group:
                        _card(card, grow=True)


def qb_disabled_banner(text: str) -> None:
    """qB 未启用时的黄色提醒横幅；text 为各页自定文案。"""
    with ui.row().classes("items-center gap-2 p-2 rounded w-full").style(
            "background:rgba(234,179,8,.12)"):
        ui.icon("warning").classes("text-yellow-500")
        ui.label(text).classes("text-sm text-yellow-200")


def recent_table(rows, name_label: str, on_row_click=None) -> None:
    """『新入库』表：rows 已构造好(id/time/name/src/raw/status/status_color)，name_label 为番名列标题；
    番名下再压一行灰色原始种子名（长名换行、完整显示）；状态列渲染成彩色徽标（含 qB 实时态）。
    番剧表与剧场版共用。on_row_click(row) 非空时，番名可点开详情（传回整行 dict）。"""
    tbl = ui.table(
        columns=[
            {"name": "time", "label": "时间", "field": "time", "align": "left"},
            {"name": "name", "label": name_label, "field": "name", "align": "left"},
            {"name": "src", "label": "来源", "field": "src", "align": "left"},
            {"name": "status", "label": "状态", "field": "status", "align": "left"},
        ],
        rows=rows, row_key="id",
    ).classes("w-full")
    # 番名：可点则染蓝加手型、点击 $emit 回传整行给 Python；不可点则纯文本。原始名恒为灰色小字。
    name_top = ('<div class="cursor-pointer text-blue-4" '
                "@click=\"() => $parent.$emit('opendetail', props.row)\">{{ props.row.name }}</div>"
                ) if on_row_click else "<div>{{ props.row.name }}</div>"
    tbl.add_slot("body-cell-name", f'''
        <q-td :props="props">
            {name_top}
            <div class="text-grey-6"
                 style="font-size:11px;white-space:normal;word-break:break-all">
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


def meta_card(cover_url, kv_pairs, bangumi_id, summary, rating=None) -> None:
    """详情元信息卡：封面 + bgm 链接（图下） + 两列 kv 网格 + 右上角豆瓣式评分（分+评价） + 简介。
    番剧/剧场版详情共用，kv_pairs=[(标签, 值)...] 各页自备（字段集略不同）。"""
    with ui.card().classes("w-full"):
        with ui.row().classes("gap-4 items-start no-wrap w-full"):
            # 左列：海报原图完整（不裁）——锁定高度、宽度随图片自然比例走（原生 img：高定死、宽 auto）
            if cover_url:
                ui.html(f'<img src="{cover_url}" style="height:18.5rem;width:auto" '
                        f'class="rounded">').classes("shrink-0 w-fit")
            # 中列：两列 kv 网格 + bgm 快捷方式(subject id + 跳转图标) 摆在『来源』下面
            with ui.column().classes("gap-1 grow min-w-0"):
                with ui.grid(columns=2).classes("gap-x-8 gap-y-1").style(
                        "grid-template-columns:auto minmax(0,1fr)"):
                    for pair in kv_pairs:
                        if pair is None:                 # 分隔行：留一点空当，把非 bgm 字段隔开
                            ui.element("div").style("height:0.4rem")
                            ui.element("div")
                            continue
                        kk, vv = pair
                        ui.label(kk).classes("text-xs text-gray-400")
                        ui.label(str(vv) if vv not in (None, "") else "—")
                if bangumi_id:  # bgm 链接，摆在『来源』下面
                    ui.link(f"bgm.tv/subject/{bangumi_id}",
                            f"https://bgm.tv/subject/{bangumi_id}").props(
                        "target=_blank").classes("text-xs")
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
            ui.label(summary).classes("text-sm text-gray-300 whitespace-pre-wrap")


def name_of(a) -> str:
    return a.display_name or a.title


def season_label(a):
    """季徽标文案：第2季起显示『第N季』，第1季不显示。"""
    return f"第{a.season}季" if (a.season or 1) > 1 else None


def platform_badge(obj) -> None:
    """bgm 判定为非 TV（剧场版/OVA/WEB…）时，给番剧行加个紫色徽标提示 bgm 眼里的实际类型。

    冷门/怪格式常被当周更番收进番剧表，但 bgm 可能识别成剧场版/OVA——打个紫标好一眼看出、自行判断。"""
    p = getattr(obj, "platform", None)
    if p and p != "TV":
        ui.badge(p).props("color=deep-purple").tooltip("bgm 判定的类型（非 TV）")


def ep_str(e) -> str:
    if e == -1:
        return "特别"
    if e == -2:
        return "?"
    return str(int(e)) if float(e).is_integer() else str(e)


# qB 原始态 → 中文（做种态统一叫『做种』，暂停做种即『已完成』）
_QB_STATE_CN = {
    "downloading": "下载中", "forcedDL": "下载中", "metaDL": "取元数据",
    "forcedMetaDL": "取元数据", "stalledDL": "等待下载", "queuedDL": "排队下载",
    "checkingDL": "校验中", "allocating": "分配空间", "uploading": "做种",
    "forcedUP": "做种", "stalledUP": "做种", "queuedUP": "排队做种", "checkingUP": "校验中",
    "pausedDL": "已暂停", "stoppedDL": "已暂停", "pausedUP": "已完成", "stoppedUP": "已完成",
    "checkingResumeData": "校验中", "moving": "移动中", "error": "错误",
    "missingFiles": "文件缺失", "unknown": "未知",
}


def qb_state_cn(state: str) -> str:
    return _QB_STATE_CN.get(state or "", state or "")


def human_size(n) -> str:
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}PB"


def qb_live_text(t) -> str:
    """种子的 qB 实时态一行文案，如『下载中 45% ↓2.1MB/s』/『做种 100%』；无实时态返回 ''。"""
    if not getattr(t, "qb_state", ""):
        return ""
    parts = [qb_state_cn(t.qb_state)]
    pr = t.qb_progress or 0
    parts.append(f"{pr * 100:.0f}%")
    if (t.qb_dlspeed or 0) > 0:
        parts.append(f"↓{human_size(t.qb_dlspeed)}/s")
    return " ".join(parts)


def live_status(status, qb_state="", qb_progress=0, qb_synced_at=None,
                qb_dlspeed=0, in_plan=None, confirmed=True) -> tuple[str, str]:
    """新入库/正在下载：把一条种子压成 (文案, 徽标色)，复刻详情页那套阶梯。

    有 qB 实时态 → 『下载中 X% ↓速度』/『做种 100%』(完成绿、在下 teal)；否则 in_plan 非空(番剧)时
    区分待下『将下载/备用』、失败『可补下/失败』；再否则按 torrent_status_cn（刚交付未同步→下载中）。
    in_plan=None 表示不区分首选/备用（剧场版没有集去重，用这个）。
    confirmed=False：番未确认（待确认），其待下不显示将下载/备用（那要点确认才会下），而显示『待确认』。"""
    if qb_state:
        pr = qb_progress or 0
        parts = [qb_state_cn(qb_state), f"{pr * 100:.0f}%"]
        if (qb_dlspeed or 0) > 0:
            parts.append(f"↓{human_size(qb_dlspeed)}/s")
        return " ".join(parts), ("green" if pr >= 1 else "teal")
    if in_plan is not None:
        if status == "pending":
            if not confirmed:
                return "待确认", "orange"
            return ("将下载", "blue") if in_plan else ("备用", "blue-grey")
        if status == "error":
            return ("失败·可补下", "orange") if in_plan else ("失败", "red")
    return torrent_status_cn(status, qb_progress, qb_synced_at), "blue-grey"


def paginate(seq: list, page: int, size: int):
    """把 seq 按每页 size 切片。返回 (本页元素, 总页数, 收敛后的页码)。

    页码越界时夹到合法范围（数据变少后停在最后一页而非空页）。
    """
    total = max(1, (len(seq) + size - 1) // size)
    page = max(1, min(page, total))
    return seq[(page - 1) * size:page * size], total, page


def expand_collapse_bar(state: dict, refresh) -> None:
    """一行『全部展开 / 全部收起』小按钮：把展开意图记进 state['expand']（True/False）再刷新面板。

    通过持久状态 + 整体重建来生效，故即便分页翻页，展开/收起也对所有页一致，而非只影响当前页那几个。
    渲染分组时用 state['expand']（None=各分组按自身默认）决定每个 ui.expansion 的初始开合。
    """
    def _set(v):
        state["expand"] = v
        refresh()
    with ui.row().classes("items-center gap-4 pl-1 pb-2"):
        for text, val in (("全部展开", True), ("全部收起", False)):
            ui.label(text).classes(
                "cursor-pointer text-sm text-gray-500 hover:text-gray-200 transition-colors").on(
                "click", lambda v=val: _set(v))


async def confirm(title: str, note: str = "", ok_label: str = "确定",
                  ok_icon: str = "", ok_color: str = "negative") -> bool:
    """弹一个确认框，等用户选择，用完即销毁自身（不残留隐藏 dialog 累积）。返回是否点了确认。"""
    with ui.dialog() as dlg, ui.card():
        ui.label(title).classes("font-bold")
        if note:
            ui.label(note).classes("text-xs text-gray-400")
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("取消", on_click=lambda: dlg.submit(False)).props("flat")
            ok = ui.button(ok_label, on_click=lambda: dlg.submit(True)).props(f"color={ok_color}")
            if ok_icon:
                ok.props(f"icon={ok_icon}")
    try:
        return bool(await dlg)         # 点叉/点外部关闭 → None → False（当取消）
    finally:
        dlg.delete()


@contextmanager
def frame(active: str = ""):
    """页面骨架：暗色 + 顶栏（站名 + 导航 + 右侧动作位）。

    yield 出顶栏右侧的容器，页面可往里放全局动作按钮（如刷新/补下）；不放就是空的。
    """
    ui.dark_mode(True)
    # 全站去卡片阴影，改成扁平 + 一条细边（统一风格）
    ui.add_head_html(
        "<style>.q-card{box-shadow:none!important;border:1px solid rgba(255,255,255,.08)}"
        ".q-table__container,.q-table__card,.q-table{box-shadow:none!important}"
        ".q-table tbody td{font-size:14px}</style>")   # q-table 表体默认 13px，抬成 14 与全站 12/14 统一
    with ui.header().classes("p-0").style(
            "background:#15171c;border-bottom:1px solid rgba(255,255,255,.07);box-shadow:none"):
        # 内容包进固定 56px 高的行——用内容锁死高度，右侧有没有按钮都不改变（q-header 的 height 会被 quasar 忽略）
        with ui.row().classes("items-center gap-2 w-full px-4").style("height:56px;overflow:hidden"):
            with ui.row().classes("items-center gap-2 mr-6"):
                ui.icon("live_tv").classes("text-2xl").style("color:#60a5fa")
                ui.label("autorss").classes("text-lg font-bold").style("color:#f3f4f6;letter-spacing:.5px")
            for key, label, path in NAV:
                cls = "cursor-pointer text-sm px-2 transition-colors "
                cls += ("text-blue-400 font-semibold underline underline-offset-8 decoration-2"
                        if key == active else "text-gray-400 hover:text-gray-100")
                ui.label(label).classes(cls).on("click", lambda p=path: ui.navigate.to(p))
            ui.space()
            header_right = ui.row().classes("items-center gap-1")  # 页面自定义动作位
            ui.button(icon="refresh", on_click=lambda: ui.navigate.reload()).props(
                "flat round dense color=white").tooltip("刷新本页")
    with ui.column().classes("w-full max-w-5xl mx-auto p-2"):
        yield header_right
