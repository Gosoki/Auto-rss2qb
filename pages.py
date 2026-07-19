"""NiceGUI 界面：顶栏 + 标签页（番剧管理[按季度] / 待确认 / 概览 / 最近）。

事件处理用工厂函数生成，避免 NiceGUI 把事件对象塞进带默认值的 lambda 参数。
"""
from nicegui import ui

import core


def _name(a) -> str:
    return a.display_name or a.title


def _ep(e) -> str:
    if e == -1:
        return "特别"
    if e == -2:
        return "?"
    return str(int(e)) if float(e).is_integer() else str(e)


# ---------------- 事件处理 ----------------

def _toggle_handler(anime_id: int):
    def handler(e):
        core.set_if_down(anime_id, e.value)
        overview_panel.refresh()
    return handler


def _confirm_handler(anime_id: int):
    async def handler():
        core.confirm_anime(anime_id)
        n = await core.download_pending_for_anime(anime_id)
        _refresh_all()
        ui.notify(f"已确认，补下 {n} 集")
    return handler


def _reject_handler(anime_id: int):
    def handler():
        core.reject_anime(anime_id)
        _refresh_all()
        ui.notify("已拒绝，不再下载")
    return handler


def _enrich_handler(anime_id: int):
    async def handler():
        ok = await core.enrich_anime(anime_id)
        manage_panel.refresh()
        overview_panel.refresh()
        ui.notify("富集成功" if ok else "富集未命中（Mikan/bgm 没有或查不到）")
    return handler


async def _download_all():
    n = await core.download_all_pending()
    _refresh_dynamic()
    ui.notify(f"已触发补下 {n} 集")


def _refresh_dynamic():
    overview_panel.refresh()
    confirm_panel.refresh()
    recent_panel.refresh()


def _refresh_all():
    _refresh_dynamic()
    manage_panel.refresh()


# ---------------- 各标签页内容 ----------------

def _anime_row(a, sources=None):
    with ui.row().classes("items-center gap-3 pl-2 py-1"):
        ui.switch(value=a.if_down, on_change=_toggle_handler(a.id))
        badge = "✓" if a.confirmed else "⏳"
        ui.label(f"{badge}  {_name(a)}  第{a.season}季")
        if a.source_kind != "ani":
            ui.badge("Mikan").props("color=orange")
        if sources:  # 来源多于一个 → 标多源，悬停看有哪些
            ui.badge(f"多源 {len(sources)}").props("color=green").tooltip("来源: " + " · ".join(sources))
        if a.bangumi_id:
            ui.link("bgm", f"https://bgm.tv/subject/{a.bangumi_id}").props("target=_blank").classes("text-xs")
        ui.button(icon="refresh", on_click=_enrich_handler(a.id)).props("size=sm flat round dense").tooltip("富集")


@ui.refreshable
def manage_panel():
    animes = core.list_anime()
    if not animes:
        ui.label("（还没有番剧，等采集）").classes("text-gray-400 p-4")
        return
    src_map = core.multi_source_map()
    by_q: dict[str, list] = {}
    for a in animes:
        by_q.setdefault(a.quarter or "未知", []).append(a)
    quarters = sorted((q for q in by_q if q != "未知"), reverse=True)
    if "未知" in by_q:
        quarters.append("未知")
    for q in quarters:
        with ui.expansion(f"{q}   ·   {len(by_q[q])} 部", value=True).classes("w-full"):
            for a in by_q[q]:
                _anime_row(a, src_map.get(a.id))


@ui.refreshable
def confirm_panel():
    pend = core.pending_confirm()
    if not pend:
        ui.label("没有待确认的番。（开启 Mikan 后，发现的非 ANi 番会出现在这里）").classes("text-gray-400 p-4")
        return
    for a in pend:
        with ui.row().classes("items-center gap-3 p-1"):
            ui.badge("Mikan").props("color=orange")
            ui.label(f"{a.quarter}  {_name(a)}  第{a.season}季")
            ui.button("确认下载", on_click=_confirm_handler(a.id)).props("size=sm color=primary")
            ui.button("拒绝", on_click=_reject_handler(a.id)).props("size=sm flat color=grey")


@ui.refreshable
def overview_panel():
    st = core.get_stats()
    with ui.row().classes("gap-4 flex-wrap p-2"):
        for label, val in [("番剧", st["anime"]), ("自动下载", st["on"]),
                           ("已下集", st["done"]), ("待下", st["pending"])]:
            with ui.card().classes("items-center px-6 py-3"):
                ui.label(str(val)).classes("text-2xl font-bold")
                ui.label(label).classes("text-xs text-gray-400")
    # 各季度番剧数
    by_q: dict[str, int] = {}
    for a in core.list_anime():
        by_q[a.quarter or "未知"] = by_q.get(a.quarter or "未知", 0) + 1
    if by_q:
        ui.label("各季度").classes("text-sm font-bold mt-2 pl-2")
        with ui.row().classes("gap-2 flex-wrap pl-2"):
            for q in sorted(by_q, reverse=True):
                ui.badge(f"{q}: {by_q[q]}").props("color=blue-grey")


@ui.refreshable
def recent_panel():
    rows = [{
        "id": t.id,
        "time": str(t.release_time or t.created_at)[:16],
        "name": f"{t.anime_title} 第{t.season}季 第{_ep(t.episode)}集",
        "src": t.source,
        "status": t.status,
    } for t in core.list_torrents(50)]
    ui.table(
        columns=[
            {"name": "time", "label": "时间", "field": "time", "align": "left"},
            {"name": "name", "label": "番剧", "field": "name", "align": "left"},
            {"name": "src", "label": "来源", "field": "src", "align": "left"},
            {"name": "status", "label": "状态", "field": "status", "align": "left"},
        ],
        rows=rows,
        row_key="id",
    ).classes("w-full")


@ui.page("/")
def index():
    ui.dark_mode(True)
    with ui.header().classes("items-center justify-between"):
        ui.label("autorss").classes("text-xl font-bold")
        with ui.row():
            ui.button("补下待下", on_click=_download_all).props("flat color=white")
            ui.button("刷新", on_click=_refresh_all).props("flat color=white")

    with ui.tabs().classes("w-full") as tabs:
        ui.tab("manage", "番剧管理", "movie")
        ui.tab("confirm", "待确认", "help_outline")
        ui.tab("overview", "概览", "dashboard")
        ui.tab("recent", "最近", "history")
    with ui.tab_panels(tabs, value="manage").classes("w-full max-w-5xl mx-auto"):
        with ui.tab_panel("manage"):
            manage_panel()
        with ui.tab_panel("confirm"):
            confirm_panel()
        with ui.tab_panel("overview"):
            overview_panel()
        with ui.tab_panel("recent"):
            recent_panel()

    ui.timer(30.0, _refresh_dynamic)  # 只刷动态区，不重建管理页（避免展开状态被重置）
