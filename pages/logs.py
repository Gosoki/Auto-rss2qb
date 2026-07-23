"""运行日志页：在浏览器里实时看后台采集/下载/重识别/qB 同步的近况，便于纠错与看是否正常运行。

日志同时打到控制台与滚动文件 data/autorss.log；本页读内存环形缓冲（最近若干条），更早的翻文件或点下载。
"""
import logging

from nicegui import ui

from core.logsetup import LOG_PATH, ring
from pages.layout import frame

# 级别过滤档位：文案 → 最低 levelno（该值及以上才显示）
_LEVELS = {"全部": 0, "警告以上": logging.WARNING, "仅错误": logging.ERROR}

# levelname → 文字色，一眼分清正常/异常
_COLOR = {"DEBUG": "text-gray-500", "INFO": "text-gray-300",
          "WARNING": "text-amber-400", "ERROR": "text-red-400", "CRITICAL": "text-red-400"}


def _download() -> None:
    try:
        ui.download.content(LOG_PATH.read_bytes(), "autorss.log")
    except OSError:
        ui.notify("日志文件还不存在（后台还没写出任何日志）", type="warning")


@ui.page("/logs")
def logs_page():
    state = {"floor": 0, "auto": True}   # floor=级别下限；auto=是否自动刷新

    with frame("logs") as header_right:
        with header_right:
            ui.button(icon="download", on_click=_download).props(
                "flat round dense color=white").tooltip("下载完整日志文件 autorss.log")

        with ui.row().classes("items-center gap-4 w-full pb-1"):
            ui.toggle(list(_LEVELS), value="全部",
                      on_change=lambda e: (state.update(floor=_LEVELS[e.value]), view.refresh())
                      ).props("dense")
            ui.switch("自动刷新", value=True,
                      on_change=lambda e: state.update(auto=e.value))
            ui.space()
            count = ui.label().classes("text-xs text-gray-500")

        @ui.refreshable
        def view():
            items = [x for x in reversed(ring.snapshot()) if x["levelno"] >= state["floor"]]
            count.text = f"最近 {len(items)} 条（新→旧）"
            if not items:
                ui.label("暂无日志——后台还没产出，或当前级别下没有记录。").classes(
                    "text-sm text-gray-500 p-2")
                return
            with ui.column().classes("w-full gap-0 font-mono").style(
                    "font-size:12px;line-height:1.55"):
                for x in items:
                    ui.label(x["line"]).classes(
                        _COLOR.get(x["level"], "text-gray-300")).style(
                        "white-space:pre-wrap;word-break:break-all")

        view()
        ui.timer(5.0, lambda: state["auto"] and view.refresh())  # 自动刷新时每 5s 拉一次最新
