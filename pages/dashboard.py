"""主页 `/`：番剧列表[按季度] / 待确认 / 待识别 / 已忽略 / 新入库 / 仪表盘。

刷新面板定义在 page 函数内部（每个浏览器连接各自一份），避免模块级
单例 refreshable 在多页面/多客户端下互相串。
"""
import re
from urllib.parse import quote

from nicegui import ui

from core import anime
import config
from .layout import ep_str, frame, human_size, name_of, season_label
from .sources import render_sources


def _state_rank(a):
    """管理页组内排序：追番中(0) < 待确认(1) < 已拒绝(2)——后两者垫底。"""
    if a.rejected:
        return 2
    return 1 if not a.confirmed else 0


def _group_by_quarter(animes):
    """按季度分组，返回 [(季度, [番...]), ...]，季度倒序、未知垫底。"""
    by_q: dict[str, list] = {}
    for a in animes:
        by_q.setdefault(a.quarter or "未知", []).append(a)
    quarters = sorted((q for q in by_q if q != "未知"), reverse=True)
    if "未知" in by_q:
        quarters.append("未知")
    return [(q, by_q[q]) for q in quarters]


# 概览用：种子状态 → (文案, quasar 颜色)
_STATUS_CHIP = [("downloaded", "已下", "green"), ("downloading", "下载中", "blue"),
                ("pending", "待下", "grey"), ("error", "失败", "red"),
                ("skipped", "跳过", "blue-grey")]


def _barline(label, value, maxv, extra="", color="#3b82f6", lw="w-32", text=None):
    """一行『标签 + 比例条 + 数值』。比例条按 value 长；text 可自定义右侧文案（默认 value+extra）。"""
    pct = (value / maxv * 100) if maxv else 0
    with ui.row().classes("items-center gap-3 w-full text-sm py-0.5 min-w-0"):
        ui.label(str(label)).classes(f"{lw} shrink-0 truncate").tooltip(str(label))
        with ui.element("div").classes("grow rounded min-w-0").style(
                "background:rgba(255,255,255,.07);height:12px"):
            ui.element("div").style(
                f"width:{pct:.1f}%;height:12px;background:{color};border-radius:6px")
        ui.label(text if text is not None else f"{value}{extra}").classes(
            "shrink-0 text-gray-400 text-right").style("min-width:5rem")


_TAB_KEYS = ("overview", "manage", "confirm", "fail", "reject", "sources")


@ui.page("/")
def dashboard(t: str = "manage"):
    """t 为当前 tab（写在 URL ?t= 里），这样刷新（整页重载）能回到同一 tab、不跳回番剧表。"""
    with frame("manage"):  # 本页用 tab + 30s 定时刷新，不往顶栏右侧放自定义动作
        # ---- 刷新（页面局部，闭包内共享）----
        @ui.refreshable
        def overview_panel():
            ov = anime.overview()
            k = ov["kpi"]

            # ── KPI 卡片 ──
            cards = [("订阅中", k["tracking"], ""), ("待识别", k["fail"], "red"),
                     ("待确认", k["confirm"], "orange"), ("已忽略", k["rejected"], ""),
                     ("已下集", k["done"], "green"), ("待下", k["pending"], "orange"),
                     ("多源", k["multi"], ""), ("种子", k["torrents"], "")]
            with ui.row().classes("gap-3 flex-wrap p-1"):
                for label, val, hi in cards:
                    with ui.card().classes("items-center px-5 py-2"):
                        cls = "text-2xl font-bold" + (f" text-{hi}-400" if hi and val else "")
                        ui.label(str(val)).classes(cls)
                        ui.label(label).classes("text-xs text-gray-400")

            # ── qB 未启用提醒 ──
            if not ov["config"]["qb"]:
                with ui.row().classes("items-center gap-2 p-2 rounded w-full").style(
                        "background:rgba(234,179,8,.12)"):
                    ui.icon("warning").classes("text-yellow-500")
                    ui.label("qB 未启用：只采集元数据、不实际下载（设置页开启 QB_ENABLED 后生效）").classes(
                        "text-sm text-yellow-200")

            # ── 订阅源组 ──
            ui.label("订阅源组").classes("text-sm font-bold mt-3 pl-1")
            with ui.row().classes("gap-2 flex-wrap pl-1"):
                for name, site, policy, priority, enabled in ov["groups"]:
                    pol = "全下" if policy == "auto" else "审核"
                    tail = "" if enabled else " · 停用"
                    ui.badge(f"{name} · {site} · {pol} · P{priority}{tail}").props(
                        f"color={'blue-grey' if enabled else 'grey'}").classes("text-sm")

            # ── 下载番剧 / 种子来源（左右分开，窄屏自动堆叠）──
            with ui.row().classes("w-full gap-6 flex-wrap mt-3"):
                with ui.column().classes("gap-1 min-w-0").style("flex:1 1 320px"):
                    ui.label(f"下载番剧（下载 / 总番）· {len(ov['by_quarter'])}").classes("text-sm font-bold")
                    with ui.column().classes("w-full gap-0").style("max-height:220px;overflow-y:auto"):
                        maxdl = max((dn for _, _, dn in ov["by_quarter"]), default=1) or 1  # 按下载数缩放
                        if not ov["by_quarter"]:
                            ui.label("—").classes("text-gray-500 text-sm")
                        for q, shows, done in ov["by_quarter"]:
                            _barline(anime.quarter_label(q), done, maxdl, lw="w-36", text=f"{done} / {shows}")
                with ui.column().classes("gap-1 min-w-0").style("flex:1 1 320px"):
                    ui.label(f"种子来源（已下 / 种子）· {len(ov['by_source'])}").classes("text-sm font-bold")
                    with ui.column().classes("w-full gap-0").style("max-height:220px;overflow-y:auto"):
                        maxs = max((tot for _, tot, _ in ov["by_source"]), default=1)
                        if not ov["by_source"]:
                            ui.label("—").classes("text-gray-500 text-sm")
                        for src, tot, done in ov["by_source"]:
                            _barline(src, tot, maxs, color="#8b5cf6", text=f"{done} / {tot}")

            # ── 种子状态 ──
            with ui.row().classes("items-center gap-3 mt-3 pl-1"):
                ui.label("种子状态").classes("text-sm font-bold")
                ui.button("补下全部", icon="download", on_click=_download_all).props(
                    "outline color=primary size=sm").tooltip("订阅中所有待下集立即下")
            with ui.row().classes("gap-2 flex-wrap pl-1 items-center"):
                for key, txt, color in _STATUS_CHIP:
                    ui.badge(f"{txt} {ov['status'][key]}").props(f"color={color}").classes("text-sm")
            # qB 实时态（接上 qB 后每 QB_SYNC_INTERVAL 秒刷新）
            if ov["config"]["qb"]:
                q = ov["qb"]
                with ui.row().classes("gap-2 flex-wrap pl-1 items-center mt-1"):
                    ui.badge(f"qB 跟踪 {q['tracked']}").props("color=teal").classes("text-sm").tooltip(
                        "qB 里正在跟踪的种子数（已交付给 qB 的）")
                    ui.badge(f"下载中 {q['downloading']}").props("color=teal").classes("text-sm")
                    ui.badge(f"做种 {q['seeding']}").props("color=teal").classes("text-sm")
                    if q["dlspeed"]:
                        ui.badge(f"↓ {human_size(q['dlspeed'])}/s").props("color=teal").classes("text-sm")
                    ui.badge(f"平均 {q['avg_progress'] * 100:.0f}%").props("color=blue-grey").classes(
                        "text-sm").tooltip("已交付种子的平均完成度")

            # ── 采集状态 / 源组 ──
            enr, tot = ov["enriched"]
            with ui.row().classes("items-center gap-3 mt-3 pl-1"):
                ui.label("采集状态").classes("text-sm font-bold")
                with ui.button("重新识别", icon="sync").props("outline color=primary size=sm"):
                    with ui.menu():
                        ui.menu_item("识别当季", on_click=_reident(1))
                        ui.menu_item("识别半年（近 2 季）", on_click=_reident(2))
                        ui.menu_item("识别 1 年（近 4 季）", on_click=_reident(4))
                        ui.menu_item("识别全部", on_click=_reident(None))
            with ui.row().classes("gap-2 flex-wrap pl-1"):
                ui.badge("采集开启" if ov["config"]["poll_on"] else "采集暂停").props(
                    f"color={'green' if ov['config']['poll_on'] else 'red'}").classes("text-sm").tooltip(
                    "后台是否在抓取（在设置页『采集』开关切换）")
                ui.badge(f"识别番数 {enr}/{tot}").props("color=indigo").classes("text-sm").tooltip(
                    "已匹配到 bgm 的番 / 全部")
                ui.badge(f"轮询间隔 {ov['config']['poll']}s").props("color=blue-grey").classes("text-sm").tooltip(
                    "后台每隔这么久抓一次源（设置页可改）")
                ui.badge(f"缓冲窗口 {ov['config']['grace']}min").props("color=blue-grey").classes("text-sm").tooltip(
                    "一集首次发现后等这么久再下，给更高优先级的源补齐（设置页可改）")

        @ui.refreshable
        def confirm_panel():
            pend = anime.pending_confirm()
            if not pend:
                ui.label("没有待确认的番。（『审核』策略的源组发现的番会出现在这里）").classes("text-gray-400 p-4")
                return
            for i, (q, items) in enumerate(_group_by_quarter(pend)):
                with ui.expansion(f"{anime.quarter_label(q)}   ·   {len(items)} 部", value=(i == 0)).classes("w-full"):
                    for a in items:
                        srcs = anime.sources_for(a.id)
                        with ui.card().classes("w-full"):
                            with ui.row().classes("items-center gap-3 flex-wrap"):
                                ui.badge("审核").props("color=orange")
                                ui.label(name_of(a)).classes(
                                    "cursor-pointer text-blue-400 hover:underline").on(
                                    "click", lambda aid=a.id: open_detail(aid))
                                sl = season_label(a)
                                if sl:
                                    ui.badge(sl).props("color=blue-grey")
                                ui.label("来源: " + (" · ".join(srcs) or "—")).classes("text-xs text-gray-400")
                            with ui.row().classes("items-center gap-2 flex-wrap"):
                                opts = {"": "从哪下：按优先级"}
                                for sname in srcs:
                                    opts[sname] = sname
                                sel = ui.select(opts, value="").props("dense outlined").classes("min-w-48")
                                ui.button("确认下载", on_click=_confirm(a.id, sel)).props("size=sm color=primary")
                                ui.button("忽略", on_click=_reject(a.id)).props("size=sm flat color=grey")

        @ui.refreshable
        def reject_panel():
            rej = anime.list_rejected()
            if not rej:
                ui.label("没有已忽略的番。（待确认/详情页点『忽略』会进这里，可随时恢复）").classes("text-gray-400 p-4")
                return
            for i, (q, items) in enumerate(_group_by_quarter(rej)):
                with ui.expansion(f"{anime.quarter_label(q)}   ·   {len(items)} 部", value=(i == 0)).classes("w-full"):
                    for a in items:
                        with ui.row().classes("items-center gap-3 pl-2 py-1 flex-wrap"):
                            ui.badge("已忽略").props("color=grey")
                            ui.label(name_of(a)).classes(
                                "cursor-pointer text-blue-400 hover:underline").on(
                                "click", lambda aid=a.id: open_detail(aid))
                            sl = season_label(a)
                            if sl:
                                ui.badge(sl).props("color=blue-grey")
                            ui.label("来源: " + (" · ".join(anime.sources_for(a.id)) or "—")).classes(
                                "text-xs text-gray-400")
                            ui.button("恢复订阅", icon="undo", on_click=_restore(a.id)).props(
                                "size=sm flat color=primary")
                            nf = anime.downloaded_count(a.id)
                            if nf:  # 只有确实下过文件才给『删除文件』
                                ui.button("删除文件", icon="delete_forever",
                                          on_click=_del_files(a.id, name_of(a), nf)).props(
                                    "size=sm flat color=negative").tooltip(
                                    "连同 qB 里的硬盘文件一起删（不可撤销）")

        @ui.refreshable
        def fail_panel():
            items = anime.list_unenriched()
            if not items:
                ui.label("没有待识别的番。（bgm 没自动匹配上的番会出现在这里，可重试或手动绑定）").classes(
                    "text-gray-400 p-4")
                return
            ui.label("这些番没自动匹配到 bgm：缺规范名 / 日语文件夹名 / 季度。"
                     "可『重试识别』，或粘贴 bgm 链接/ID『绑定』，实在没有就『忽略』。").classes(
                "text-xs text-gray-400 p-2")
            for a in items:
                srcs = anime.sources_for(a.id)
                with ui.card().classes("w-full"):
                    with ui.row().classes("items-center gap-3 flex-wrap"):
                        ui.badge("未匹配").props("color=red")
                        ui.label(name_of(a)).classes(
                            "cursor-pointer text-blue-400 hover:underline").on(
                            "click", lambda aid=a.id: open_detail(aid))
                        sl = season_label(a)
                        if sl:
                            ui.badge(sl).props("color=blue-grey")
                        ui.badge("待确认" if not a.confirmed else "自动").props(
                            f"color={'orange' if not a.confirmed else 'blue-grey'}")
                        ui.label("来源: " + (" · ".join(srcs) or "—")).classes("text-xs text-gray-400")
                        ui.link("去 bgm 搜", f"https://bgm.tv/subject_search/{quote(a.title)}?cat=2").props(
                            "target=_blank").classes("text-xs")
                    with ui.row().classes("items-center gap-2 flex-wrap"):
                        inp = ui.input(
                            placeholder="bgm 链接或 ID，如 bgm.tv/subject/464376 或 464376").props(
                            "dense outlined").classes("min-w-96")
                        ui.button("绑定", icon="link", on_click=_bind(a.id, inp)).props("size=sm color=primary")
                        ui.button("重试识别", icon="refresh", on_click=_refail(a.id)).props("size=sm flat")
                        ui.button("忽略", on_click=_reject(a.id)).props("size=sm flat color=grey")

        @ui.refreshable
        def recent_panel():
            ui.label("新入库（最近 50 条种子）").classes("text-sm font-bold mt-4 pl-1")
            st = {"downloaded": "已下", "downloading": "下载中", "pending": "待下",
                  "error": "失败", "skipped": "跳过"}
            rows = [{
                "id": r["id"],
                "time": r["time"],
                "name": f'{r["name"]}  第{ep_str(r["episode"])}集',
                "src": r["source"],
                "raw": r["raw"] or "—",
                "status": st.get(r["status"], r["status"]),
            } for r in anime.recent_rows(50)]
            ui.table(
                columns=[
                    {"name": "time", "label": "时间", "field": "time", "align": "left"},
                    {"name": "name", "label": "番剧", "field": "name", "align": "left"},
                    {"name": "src", "label": "来源", "field": "src", "align": "left"},
                    {"name": "raw", "label": "原始标题", "field": "raw", "align": "left"},
                    {"name": "status", "label": "状态", "field": "status", "align": "left"},
                ],
                rows=rows, row_key="id",
            ).classes("w-full")

        @ui.refreshable
        def manage_panel():
            # 当季 / 上季小结（数字大、标签小；零值审核项变灰）
            with ui.row().classes("w-full gap-3 flex-wrap mb-3 items-stretch"):
                for b in anime.quarter_brief():
                    stats = [
                        (b["shows"], "订阅中", "text-blue-300"),
                        (b["confirm"], "待确认", "text-orange-400" if b["confirm"] else "text-gray-600"),
                        (b["fail"], "待识别", "text-red-400" if b["fail"] else "text-gray-600"),
                        (b["ignored"], "已忽略", "text-gray-400" if b["ignored"] else "text-gray-600"),
                    ]
                    with ui.card().classes("gap-2 py-3").style("flex:1 1 300px"):
                        with ui.row().classes("items-center gap-2"):
                            ui.badge(b["tag"]).props(
                                f"color={'primary' if b['tag'] == '当季' else 'blue-grey'}")
                            ui.label(anime.quarter_label(b["key"])).classes("font-bold text-base")
                        with ui.row().classes("gap-6"):
                            for num, lbl, color in stats:
                                with ui.column().classes("items-center gap-0"):
                                    ui.label(str(num)).classes(f"text-2xl font-bold leading-none {color}")
                                    ui.label(lbl).classes("text-xs text-gray-400 mt-1")
                        ui.separator()
                        with ui.row().classes("gap-4 text-xs text-gray-400"):
                            ui.label(f"已下 {b['done']}")
                            ui.label(f"待下 {b['pending']}")
                            ui.label(f"种子 {b['torrents']}")

            # 面板设置：追番中恒显示；待确认/已拒绝各自按开关决定带不带上
            def _visible(a):
                if a.rejected:
                    return config.MANAGE_SHOW_REJECTED
                if not a.confirmed:
                    return config.MANAGE_SHOW_PENDING
                return True
            animes = [a for a in anime.list_all_anime() if _visible(a)]
            if not animes:
                ui.label("（还没有番剧，等采集）").classes("text-gray-400 p-4")
                return
            animes.sort(key=lambda a: (_state_rank(a), a.id))  # 追番中在上，待确认、已拒绝垫底
            src_map = anime.multi_source_map()
            for i, (q, items) in enumerate(_group_by_quarter(animes)):
                with ui.expansion(f"{anime.quarter_label(q)}   ·   {len(items)} 部", value=(i == 0)).classes("w-full"):
                    for a in items:
                        _anime_row(a, src_map.get(a.id))

        def refresh_dynamic():
            overview_panel.refresh()
            confirm_panel.refresh()
            reject_panel.refresh()
            fail_panel.refresh()
            recent_panel.refresh()

        def refresh_all():
            refresh_dynamic()
            manage_panel.refresh()

        # 详情悬浮框（复用一个 dialog，点开时清空重建，避免累积 + 不跳页丢滚动位置）
        detail_dlg = ui.dialog()

        def open_detail(anime_id):
            from .detail import render_detail
            detail_dlg.clear()
            with detail_dlg, ui.card().classes("w-full").style("max-width:860px"):
                with ui.row().classes("w-full justify-end"):
                    ui.button(icon="close", on_click=detail_dlg.close).props("flat round dense")
                render_detail(anime_id, refresh_outer=refresh_all)
            detail_dlg.open()

        # ---- 事件处理（闭包，直接引用上面的刷新函数）----
        def _confirm(anime_id, sel=None):
            async def h():
                pref = (sel.value if sel is not None else "") or ""
                anime.confirm_anime(anime_id, pref)
                n = await anime.download_pending_for_anime(anime_id)
                refresh_all()
                ui.notify(f"已确认，补下 {n} 集" + (f"（源：{pref}）" if pref else ""))
            return h

        def _reject(anime_id):
            def h():
                anime.reject_anime(anime_id)
                refresh_all()
                ui.notify("已忽略，移到『已忽略』页")
            return h

        def _restore(anime_id):
            async def h():
                anime.restore_anime(anime_id)
                n = await anime.download_pending_for_anime(anime_id)
                refresh_all()
                ui.notify(f"已恢复到『订阅中』，补下 {n} 集")
            return h

        def _del_files(anime_id, name, cnt):
            def open_confirm():
                dlg = ui.dialog()
                with dlg, ui.card():
                    ui.label(f"删除《{name}》的 {cnt} 个已下文件？").classes("font-bold")
                    ui.label("通过 qB 连同硬盘文件一起删除，不可撤销。").classes("text-xs text-gray-400")
                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button("取消", on_click=dlg.close).props("flat")

                        async def _do():
                            dlg.close()
                            n = await anime.delete_anime_files(anime_id)
                            refresh_all()
                            ui.notify(f"已删除 {n} 个文件" if n else "没删成（qB 未连上或已无文件）",
                                      type="positive" if n else "warning")
                        ui.button("删除文件", icon="delete_forever", on_click=_do).props("color=negative")
                dlg.open()
            return open_confirm

        def _bind(anime_id, inp):
            async def h():
                v = (inp.value or "").strip()
                m = re.search(r"subject/(\d+)", v) or re.search(r"(\d+)", v)
                if not m:
                    ui.notify("请粘贴 bgm 链接或数字 ID", type="warning")
                    return
                ok = await anime.bind_bgm(anime_id, int(m.group(1)))
                refresh_all()
                ui.notify("已绑定并识别 ✓" if ok else "绑定失败：ID 不存在或取不到 bgm 数据",
                          type="positive" if ok else "negative")
            return h

        def _refail(anime_id):
            async def h():
                ok = await anime.enrich_anime(anime_id)
                refresh_all()
                ui.notify("识别成功 ✓" if ok else "还是没识别到（可手动粘贴 bgm 链接绑定）",
                          type="positive" if ok else "warning")
            return h

        async def _download_all():
            n = await anime.download_all_pending()
            refresh_dynamic()
            ui.notify(f"已触发补下 {n} 集")

        def _reident(seasons):
            async def h():
                scope = {1: "当季", 2: "近半年", 4: "近1年", None: "全部"}.get(seasons, "")
                ui.notify(f"正在重新识别（{scope}）…走 bgm，可能要一会儿")
                cnt = await anime.reenrich_scope(seasons)
                overview_panel.refresh()
                ui.notify(f"识别完成：{cnt} 部命中", type="positive")
            return h

        def _anime_row(a, sources=None):
            with ui.row().classes("items-center gap-3 pl-2 py-1"):
                if a.rejected:                       # 状态徽标（互斥，最多一个）
                    ui.badge("已忽略").props("color=grey")
                elif not a.confirmed:
                    ui.badge("待确认").props("color=orange")
                elif a.source_kind in ("review", "other"):
                    ui.badge("审核").props("color=blue-grey")  # 已确认但来自审核源（区别 ANi 直下）
                color = "text-gray-500 line-through" if a.rejected else "text-blue-400"
                ui.label(name_of(a)).classes(
                    f"cursor-pointer {color} hover:underline").on(
                    "click", lambda aid=a.id: open_detail(aid))
                sl = season_label(a)
                if sl:
                    ui.badge(sl).props("color=blue-grey")
                if sources:
                    ui.badge(f"多源 {len(sources)}").props("color=green").tooltip("来源: " + " · ".join(sources))

        # ---- 页面布局 ----
        with ui.tabs().classes("w-full") as tabs:
            ui.tab("overview", "仪表盘", "dashboard")
            ui.tab("manage", "番剧表", "movie")
            ui.tab("confirm", "待确认", "help_outline")
            ui.tab("fail", "待识别", "sync_problem")
            ui.tab("reject", "已忽略", "block")
            ui.tab("sources", "订阅源", "rss_feed")
        # 切 tab 时把当前 tab 写进 URL（不重载），这样『刷新』重载后仍停在该 tab
        tabs.on_value_change(lambda e: ui.run_javascript(
            f"history.replaceState(null,'','?t='+encodeURIComponent('{e.value}'))"))
        start = t if t in _TAB_KEYS else "manage"
        with ui.tab_panels(tabs, value=start).classes("w-full"):
            with ui.tab_panel("overview"):
                overview_panel()
                recent_panel()
            with ui.tab_panel("manage"):
                manage_panel()
            with ui.tab_panel("confirm"):
                confirm_panel()
            with ui.tab_panel("fail"):
                fail_panel()
            with ui.tab_panel("reject"):
                reject_panel()
            with ui.tab_panel("sources"):
                render_sources()

        ui.timer(30.0, refresh_dynamic)  # 只刷动态区，不重建管理页（避免展开状态被重置）
