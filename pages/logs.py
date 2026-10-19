"""运行日志页：在浏览器里实时看后台采集/下载/重识别/qB 同步的近况，便于纠错与看是否正常运行。

日志同时打到控制台与滚动文件 data/autorss.log；本页读内存环形缓冲（最近若干条），更早的翻文件或点下载。
"""
import logging

from nicegui import ui

from core.logsetup import LOG_PATH, errors_ring, ring, warnings_ring
from pages.layout import frame, page_visibility

# 级别过滤档位：文案 → 最低 levelno（该值及以上才显示）
_LEVELS = {"全部": 0, "警告以上": logging.WARNING, "仅错误": logging.ERROR}

# levelname → 文字色，一眼分清正常/异常。
# 【INFO 不着色】它是基线：日志页大部分行都是 INFO，给它一档灰等于把"正常"也标成次要，
# 而真正要一眼看见的 WARNING/ERROR 反而少了对比。DEBUG 才需要压下去。
# 顺带这也把全站的灰收成两档（gray-400 / gray-500），见 layout.warn_banner 的说明。
_COLOR = {"DEBUG": "text-gray-500", "INFO": "",
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

        def _buf():
            # 『警告以上』『仅错误』各读自己那份单独的缓冲（最近 100 条、含重启前从日志文件读回的），
            # 而不是从全量的最近 200 条里筛 —— INFO 一多，几个小时前的警告就被挤没了。
            # 那两份挂在 root 上，NiceGUI 等框架自己的报错也在里面；『全部』只看本应用的日志。
            f = state["floor"]
            return errors_ring if f >= logging.ERROR else warnings_ring if f >= logging.WARNING else ring

        @ui.refreshable
        def view():
            buf = _buf()
            items = list(reversed(buf.snapshot()))
            what = {errors_ring: "条错误", warnings_ring: "条警告/错误"}.get(buf, "条")
            count.text = (f"最近 {len(items)} {what}（新→旧"
                          + ("，单独保留最近 %d 条、含重启前的）" % buf.buf.maxlen if buf is not ring else "）"))
            if not items:
                ui.label("暂无日志——后台还没产出，或当前级别下没有记录。").classes(
                    "text-sm text-gray-500 p-2")
                return
            with ui.column().classes("w-full gap-0 font-mono").style(
                    "font-size:12px;line-height:1.55"):
                for x in items:
                    ui.label(x["line"]).classes(
                        _COLOR.get(x["level"], "")).style(
                        "white-space:pre-wrap;word-break:break-all")

        view()
        seen = {"seq": None}

        def _tick():
            # 自动刷新时每 5s 看一次：标签页在后台、或这 5 秒当前那份缓冲没有新日志，就不重建（r37 #23：
            # 以前每 5 秒把 200 行整块重建一遍，不管有没有变化、有没有人在看）
            cur = (id(_buf()), _buf().seq)
            if not (state["auto"] and vis["on"]) or cur == seen["seq"]:
                return
            seen["seq"] = cur
            view.refresh()
        vis = page_visibility(_tick)
        ui.timer(5.0, _tick)
