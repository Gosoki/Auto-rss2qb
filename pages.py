"""NiceGUI 界面：仪表盘 + 番剧管理 + 待确认 + 手动补下。

纯 Python 写界面。事件处理都用工厂函数生成，避免 NiceGUI 把事件对象
塞进带默认值的 lambda 参数导致闭包变量被覆盖。
"""
from nicegui import ui

import core


def _toggle_handler(anime_id: int):
    def handler(e):
        core.set_if_down(anime_id, e.value)
    return handler


def _confirm_handler(anime_id: int):
    async def handler():
        core.confirm_anime(anime_id)
        n = await core.download_pending_for_anime(anime_id)
        dashboard.refresh()
        ui.notify(f"已确认，开始补下 {n} 集")
    return handler


async def _download_all():
    n = await core.download_all_pending()
    dashboard.refresh()
    ui.notify(f"已触发补下 {n} 集")


@ui.refreshable
def dashboard():
    st = core.get_stats()
    with ui.row().classes("gap-4 flex-wrap"):
        for label, val in [("番剧", st["anime"]), ("自动下载", st["on"]),
                           ("已下集", st["done"]), ("待下", st["pending"])]:
            with ui.card().classes("items-center px-6 py-3"):
                ui.label(str(val)).classes("text-2xl font-bold")
                ui.label(label).classes("text-xs text-gray-400")

    # 待确认（P1 单 ANi 源通常为空，结构给 P2 的 Mikan 用）
    pend = core.pending_confirm()
    if pend:
        ui.label("待确认新番").classes("text-lg font-bold mt-4")
        for a in pend:
            with ui.row().classes("items-center gap-3"):
                ui.label(f"{a.quarter}  {a.title}  第{a.season}季")
                ui.button("确认下载", on_click=_confirm_handler(a.id)).props("size=sm color=primary")

    # 番剧管理
    ui.label("番剧管理").classes("text-lg font-bold mt-4")
    animes = core.list_anime()
    if not animes:
        ui.label("（还没有番剧，等轮询器抓到第一批）").classes("text-gray-400")
    for a in animes:
        with ui.row().classes("items-center gap-3"):
            ui.switch(value=a.if_down, on_change=_toggle_handler(a.id))
            badge = "✓" if a.confirmed else "⏳"
            ui.label(f"{badge}  {a.quarter}  {a.title}  第{a.season}季")

    # 最近种子
    ui.label("最近").classes("text-lg font-bold mt-4")
    rows = [{
        "id": t.id,
        "time": str(t.release_time or t.created_at)[:16],
        "name": f"{t.anime_title} 第{t.season}季 第{_ep(t.episode)}集",
        "status": t.status,
    } for t in core.list_torrents(30)]
    ui.table(
        columns=[
            {"name": "time", "label": "时间", "field": "time", "align": "left"},
            {"name": "name", "label": "番剧", "field": "name", "align": "left"},
            {"name": "status", "label": "状态", "field": "status", "align": "left"},
        ],
        rows=rows,
        row_key="id",
    ).classes("w-full")


def _ep(e) -> str:
    if e == -1:
        return "特别"
    if e == -2:
        return "?"
    return str(int(e)) if float(e).is_integer() else str(e)


@ui.page("/")
def index():
    ui.dark_mode(True)
    with ui.row().classes("items-center w-full max-w-4xl mx-auto mt-2"):
        ui.label("autorss").classes("text-2xl font-bold")
        ui.space()
        ui.button("补下待下", on_click=_download_all).props("flat")
        ui.button("刷新", on_click=dashboard.refresh).props("flat")
    with ui.column().classes("w-full max-w-4xl mx-auto"):
        dashboard()
    ui.timer(20.0, dashboard.refresh)
