"""手动下载页 `/manual`：把 magnet/.torrent（链接或上传）交给 qB，默认下到 工作目录/Temp；
可选填 bgm 识别成正常番剧目录——下之前定向，或下完再『移到正常目录』。一次性工具，不建 Anime 记录。"""
from nicegui import ui

import config
from core import manual
from .layout import frame


@ui.page("/manual")
def manual_page():
    with frame("manual"):
        ui.label("手动下载").classes("text-2xl font-bold")
        ui.label("把 magnet / .torrent（链接或上传文件）交给 qB。默认下到『工作目录/Temp』；"
                 "填 bgm 可识别成正常番剧目录（季度/日文名/Season）——可在下载前定向，或下完再移过去。"
                 "一次性下载，不进番剧列表、不做去重追踪。").classes("text-xs text-gray-400 mb-2")

        up = {"bytes": None, "name": ""}          # 上传的 .torrent 字节
        st = {"hash": None, "path": None}          # 上次下载的种子 hash + 识别算出的正常目录

        with ui.card().classes("w-full gap-3"):
            # ---- 种子输入 ----
            ui.label("种子").classes("font-bold text-sm")
            tin = ui.input("magnet: / http(s) .torrent 链接",
                           placeholder="magnet:?xt=urn:btih:… 或 https://…/x.torrent").props(
                "dense outlined clearable").classes("w-full")

            def _on_upload(e):
                up["bytes"] = e.content.read()
                up["name"] = e.name
                ui.notify(f"已选 .torrent 文件：{e.name}", type="positive")

            def _on_clear():
                up["bytes"] = None; up["name"] = ""

            ui.upload(label="或上传 .torrent 文件", auto_upload=True, max_files=1,
                      on_upload=_on_upload, on_rejected=lambda: ui.notify("只收单个 .torrent 文件", type="warning")
                      ).props('accept=".torrent" flat').classes("w-full")
            ui.label("链接与上传二选一；两者都给时优先用上传的文件。").classes("text-xs text-gray-500")

            ui.separator()
            # ---- 保存位置 ----
            ui.label("保存位置").classes("font-bold text-sm")
            spath = ui.input("保存目录（默认 工作目录/Temp）", value=manual.temp_path()).props(
                "dense outlined").classes("w-full")
            ui.label("远程 qB 时这是【qB 主机上】的绝对路径。识别后可一键设成正常番剧目录。").classes(
                "text-xs text-gray-500")

            ui.separator()
            # ---- bgm 识别 ----
            ui.label("bgm 识别（可选）").classes("font-bold text-sm")
            with ui.row().classes("items-stretch gap-3 w-full"):
                bgm = ui.input("bgm 链接 / ID", placeholder="bgm.tv/subject/12345 或 12345").props(
                    "dense outlined clearable").classes("grow")

                async def _identify():
                    r = await manual.identify_folder(bgm.value or "")
                    if not r["ok"]:
                        ui.notify(r["error"], type="warning"); ident.refresh(); return
                    st["path"] = r["path"]
                    ui.notify(f"识别：{r['name']}（{r['quarter']} S{r['season']}）", type="positive")
                    ident.refresh()

                ui.button("识别", icon="search", on_click=_identify).props("color=primary unelevated no-caps")

            @ui.refreshable
            def ident():
                if not st["path"]:
                    return
                with ui.row().classes("items-center gap-2 w-full flex-wrap"):
                    ui.icon("folder").classes("text-blue-400")
                    ui.label(st["path"]).classes("text-sm text-blue-400 break-all min-w-0")
                    ui.button("设为保存位置", icon="drive_file_move", on_click=lambda: (
                        spath.set_value(st["path"]), ui.notify("已把保存位置设为正常目录"))).props(
                        "flat dense size=sm color=primary").classes("text-xs")
            ident()

            ui.separator()
            # ---- 动作 ----
            with ui.row().classes("items-center gap-3 flex-wrap"):
                async def _download():
                    if not config.QB_ENABLED:
                        ui.notify("qB 未启用（去设置页开启『发送种子到 qB』）", type="warning"); return
                    r = await manual.add_manual(tin.value or "", up["bytes"], spath.value or "")
                    if r["ok"]:
                        st["hash"] = r.get("info_hash")
                        ui.notify(f"已交给 qB → {r['save_path']}"
                                  + ("" if r.get("info_hash") else "（未取到 hash，稍后不能自动移动）"),
                                  type="positive")
                        after.refresh()
                    else:
                        ui.notify(f"下载失败：{r['error']}", type="negative")

                _dl = ui.button("下载", icon="download", on_click=_download).props("color=primary unelevated")
                _dl.set_enabled(config.QB_ENABLED)
                if not config.QB_ENABLED:
                    _dl.tooltip("qB 未启用，去设置页开启后可下载")

                async def _relocate():
                    if not st["hash"]:
                        ui.notify("先『下载』一条种子（且能取到 hash）", type="warning"); return
                    if not st["path"]:
                        ui.notify("先『识别』出正常目录", type="warning"); return
                    code = await manual.relocate_manual(st["hash"], st["path"])
                    if code == 200:
                        spath.set_value(st["path"])
                        ui.notify(f"已移到 {st['path']}", type="positive")
                    elif code is None:
                        ui.notify("移动失败：qB 连不上", type="negative")
                    else:
                        ui.notify(f"移动失败（{code}：新目录不可写/建不了，或 qB 还没拿到该种子）", type="negative")

                ui.button("移到正常目录", icon="drive_file_move", on_click=_relocate).props(
                    "flat color=primary").tooltip("把刚下载的这条从 Temp 移到识别出的正常目录（下完后+识别后可用）")

            @ui.refreshable
            def after():
                if st["hash"]:
                    ui.label(f"上次下载 hash：{st['hash']}").classes("text-xs text-gray-500")
            after()
