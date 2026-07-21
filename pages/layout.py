"""共享布局：顶栏导航 + 统一的内容容器。所有页面都套 frame()。

页面级组件放在自己的页面文件里；这里只放跨页面复用的东西。
"""
from contextlib import contextmanager

from nicegui import ui

NAV = [("manage", "动漫番剧", "/"), ("movies", "OVA・剧场版", "/movies"),
       ("settings", "设置", "/settings")]


def name_of(a) -> str:
    return a.display_name or a.title


def season_label(a):
    """季徽标文案：第2季起显示『第N季』，第1季不显示。"""
    return f"第{a.season}季" if (a.season or 1) > 1 else None


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


def paginate(seq: list, page: int, size: int):
    """把 seq 按每页 size 切片。返回 (本页元素, 总页数, 收敛后的页码)。

    页码越界时夹到合法范围（数据变少后停在最后一页而非空页）。
    """
    total = max(1, (len(seq) + size - 1) // size)
    page = max(1, min(page, total))
    return seq[(page - 1) * size:page * size], total, page


def expand_collapse_bar(exps: list) -> None:
    """一行『全部展开 / 全部收起』小按钮，统一开合传入的 ui.expansion 列表。

    exps 传空列表进来、渲染分组时逐个 append；点按钮时列表已填满，故能全量操作。
    """
    with ui.row().classes("items-center gap-1 pl-1 pb-1"):
        ui.button("全部展开", icon="unfold_more",
                  on_click=lambda: [e.set_value(True) for e in exps]).props("flat dense size=sm")
        ui.button("全部收起", icon="unfold_less",
                  on_click=lambda: [e.set_value(False) for e in exps]).props("flat dense size=sm")


@contextmanager
def frame(active: str = ""):
    """页面骨架：暗色 + 顶栏（站名 + 导航 + 右侧动作位）。

    yield 出顶栏右侧的容器，页面可往里放全局动作按钮（如刷新/补下）；不放就是空的。
    """
    ui.dark_mode(True)
    # 全站去卡片阴影，改成扁平 + 一条细边（统一风格）
    ui.add_head_html(
        "<style>.q-card{box-shadow:none!important;border:1px solid rgba(255,255,255,.08)}</style>")
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
