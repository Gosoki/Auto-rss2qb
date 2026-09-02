"""手动下载页 `/manual`：把 magnet/.torrent（链接或上传）交给 qB，默认下到 工作目录/Temp；
可选填 bgm 识别成正常番剧目录，点『设为保存位置』就下到那里。一次性工具，不建 Anime 记录、不追踪。"""
from nicegui import ui

import config
from core import manual
from .layout import busy_action, frame, text_token, tint

# .torrent 通常 < 1MB；给足余量后封顶，免得整块读进内存（与 core/engine.py 的取种上限同量级）。
_UPLOAD_CAP = 32 * 1024 * 1024


@ui.page("/manual")
def manual_page():
    with frame("manual"):
        ui.label("手动下载").classes("text-2xl font-bold")
        ui.label("把 magnet / .torrent（链接或上传文件）交给 qB。默认下到『工作目录/Temp』；"
                 "填 bgm 可识别成正常番剧目录（季度/日文名/Season），点『设为保存位置』就下到那里。"
                 "一次性下载，不进番剧列表、不做去重追踪。").classes("text-xs text-gray-400 mb-3")
        # 上传控件改成低调的虚线拖拽框：去掉 Quasar 默认那条亮蓝 header、0.0B/0.00% 统计、常空的文件列表区，
        # 跟全站扁平暗色风统一（选好文件后本就切成下面的绿色确认框，故列表区永远空、直接隐藏）。
        ui.add_head_html(
            "<style>"
            ".tmanual-up{border:1px dashed rgba(255,255,255,.22)!important;border-radius:8px;"
            "background:rgba(255,255,255,.02);box-shadow:none!important}"
            ".tmanual-up .q-uploader__header{background:transparent!important;"
            f"color:{text_token('grey')};min-height:56px}}"
            ".tmanual-up .q-uploader__title{font-size:14px;line-height:1.35;white-space:normal}"
            ".tmanual-up .q-uploader__subtitle{display:none}"          # 隐藏 0.0B / 0.00%
            ".tmanual-up .q-uploader__list{display:none}"              # 隐藏常空的文件列表区
            f".tmanual-up .q-uploader__header .q-btn{{color:{text_token('blue')}}}"   # + 按钮蓝色好认
            "</style>")

        up = {"bytes": None, "name": ""}
        st = {"path": None}

        with ui.card().classes("w-full gap-4"):   # 宽度交给 frame() 的 max-w-5xl 容器，跟其他页卡片一致
            # ========== 种子来源 ==========
            ui.label("种子来源（链接 或 上传文件，二选一）").classes("font-bold text-sm")
            tin = ui.input("magnet: / http(s) .torrent 链接",
                           placeholder="magnet:?xt=urn:btih:… 或 https://…/x.torrent").props("clearable").classes("w-full")
            ui.label("— 或 —").classes("text-xs text-gray-500 self-center")

            @ui.refreshable
            def upbox():
                async def _on_upload(e):
                    # 【NiceGUI 3.16 的事件对象是 slots dataclass，只有 sender/client/file】
                    # 早先这里写的是 e.content.read() / e.name（旧版 API），在当前依赖上必抛
                    # AttributeError——整条"上传 .torrent 文件"的入口是坏的：点了没反应，
                    # 只有日志里一行异常。新 API 是 e.file.name / await e.file.read() / e.file.size()。
                    f = e.file
                    if f.size() > _UPLOAD_CAP:
                        ui.notify(f"文件太大（{f.size() // 1024 // 1024}MB）："
                                  f".torrent 通常不到 1MB，请确认选对了文件", type="warning")
                        return
                    up["bytes"] = await f.read()
                    up["name"] = f.name
                    upbox.refresh()
                    ui.notify(f"已选文件：{f.name}", type="positive")
                if up["name"]:
                    # 与 layout 的另外三个染色块同一个配方（同一批 oklch token @ 12%、无边框）
                    with ui.row().classes("items-center gap-2 w-full p-2 rounded").style(tint("green")):
                        # 前景与上面 tint("green") 的底色必须是【同一个绿】：全站绿取 green-500
                        # （徽标 .q-badge.bg-green 也是它）。这两行原来写的是 green-400 ——
                        # 同一个盒子里底色 green-500、图标与文字 green-400，两种绿并排，
                        # 而 layout.py 那条注释里说的正是这个毛病、只修好了底色那一半。
                        ui.icon("description").classes("text-green-500")
                        ui.label(up["name"]).classes("text-sm text-green-500 grow break-all min-w-0")
                        ui.button(icon="close", on_click=lambda: (up.update(bytes=None, name=""), upbox.refresh())
                                  ).props("flat round dense color=grey").classes("btn-sm").tooltip("移除")
                else:
                    ui.upload(label="上传 .torrent 文件（点右侧 ＋ 选文件）", auto_upload=True, max_files=1,
                              on_upload=_on_upload,
                              on_rejected=lambda: ui.notify("只收单个 .torrent 文件", type="warning")).props(
                        'accept=".torrent" flat bordered').classes("w-full tmanual-up")
            upbox()
            ui.label("两者都填时优先用上传的文件。").classes("text-xs text-gray-500")

            ui.separator()
            # ========== 保存位置 ==========
            ui.label("保存位置").classes("font-bold text-sm")
            spath = ui.input("保存目录（默认 工作目录/Temp）", value=manual.temp_path()).classes("w-full")
            ui.label("远程 qB 时这是【qB 主机上】的绝对路径。填 bgm 识别后可一键设成正常番剧目录。").classes(
                "text-xs text-gray-500")

            ui.separator()
            # ========== bgm 识别（可选）==========
            ui.label("bgm 识别（可选 → 算出正常番剧目录）").classes("font-bold text-sm")
            with ui.row().classes("items-stretch gap-3 w-full"):
                bgm = ui.input("bgm 链接 / ID", placeholder="bgm.tv/subject/12345 或 12345").props("clearable").classes("grow")

                async def _identify():
                    r = await manual.identify_folder(bgm.value or "")
                    if not r["ok"]:
                        st["path"] = None; ui.notify(r["error"], type="warning"); ident.refresh(); return
                    st["path"] = r["path"]
                    ui.notify(f"识别：{r['name']}（{r['quarter']} S{r['season']}）", type="positive")
                    ident.refresh()

                # bgm 往返最长 _RESOLVE_BUDGET（120 秒），期间按钮毫无反应用户会连点 —— 走 busy_action
                ui.button("识别", icon="search",
                          on_click=lambda e: busy_action(e.sender, "manual-identify", _identify,
                                                         fail="识别失败")).props(
                    "unelevated color=primary")

            @ui.refreshable
            def ident():
                if not st["path"]:
                    return
                with ui.column().classes("w-full gap-2 p-3 rounded").style(tint("blue")):
                    with ui.row().classes("items-center gap-2 w-full min-w-0"):
                        ui.icon("folder").classes("text-blue-400 shrink-0")
                        ui.label(st["path"]).classes("text-sm text-blue-400 break-all min-w-0")
                    ui.button("设为保存位置", icon="drive_file_move", on_click=lambda: (
                        spath.set_value(st["path"]), ui.notify("已把保存位置设为正常目录", type="positive"))).props(
                        "unelevated color=primary").classes("self-start")
            ident()

            ui.separator()
            # ========== 下载 ==========
            async def _download():
                if not config.QB_ENABLED:
                    ui.notify("qB 未启用（去设置页开启『发送种子到 qB』）", type="warning"); return
                r = await manual.add_manual(tin.value or "", up["bytes"], spath.value or "")
                if r["ok"]:
                    ui.notify(f"已交给 qB → {r['save_path']}", type="positive")
                else:
                    ui.notify(f"下载失败：{r['error']}", type="negative")

            _dl = ui.button("下载", icon="download", on_click=_download).props(
                "unelevated color=primary").classes("self-start")
            _dl.set_enabled(config.QB_ENABLED)
            if not config.QB_ENABLED:
                _dl.tooltip("qB 未启用，去设置页开启后可下载")
