"""OVA・剧场版页 `/movies`：仿番剧那边的标签布局 —— 仪表盘 / 列表 / 待识别 / 已忽略 / 订阅源。

剧场版数据（Movie/MovieTorrent）与 TV 番剧完全分离，逻辑在 movies.py；本页只管展示与交互。
剧场版整个列表本身就是『待人工下载』，故不设『待确认』；订阅源=固定的 Mikan 季度扫描（非 RSS 订阅）。
"""
import re
from datetime import datetime

from nicegui import ui

import config
from core import movies as mov
from .layout import expand_collapse_bar, frame, human_size, name_of, paginate, qb_live_text

_SEASONS = {"A": "冬", "B": "春", "C": "夏", "D": "秋"}
_STATUS = {"downloaded": "已下", "pending": "待下", "downloading": "下载中",
           "error": "失败", "skipped": "跳过"}
_WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_TABS = ("overview", "list", "fail", "reject", "sources")


def _q_label(q: str) -> str:
    from core import anime
    return anime.quarter_label(q)


def render_movie_detail(movie_id: int, refresh_outer=None) -> None:
    """把某剧场版详情渲染进当前容器：元信息 + 版本列表（逐条下/删）+ 识别/忽略。"""
    if mov.get_movie(movie_id) is None:
        ui.label("剧场版不存在").classes("text-gray-400 p-4")
        return

    @ui.refreshable
    def body():
        cur = mov.get_movie(movie_id)
        if cur is None:
            ui.label("剧场版不存在").classes("text-gray-400 p-4")
            return
        ts = mov.movie_torrents(movie_id)
        sources = sorted({t.source for t in ts})
        with ui.row().classes("items-center gap-2 flex-wrap"):
            ui.label(name_of(cur)).classes("text-2xl font-bold")
            ui.badge(cur.mikan_type or "剧场版").props("color=deep-purple")  # Mikan 桶判定
            if cur.rejected:
                ui.badge("已忽略").props("color=grey")
            elif not cur.bangumi_id:
                ui.badge("未识别").props("color=red")

        with ui.card().classes("w-full"):
            with ui.row().classes("gap-4 items-start no-wrap w-full"):
                if cur.cover_url:
                    ui.image(cur.cover_url).classes("rounded").style("min-width:7rem;width:7rem")
                with ui.column().classes("gap-1 grow"):
                    wd = f"  {_WEEKDAY[cur.air_weekday]}" if cur.air_weekday is not None else ""
                    with ui.grid(columns=2).classes("gap-x-8 gap-y-1"):
                        def _kv(k, v):
                            ui.label(k).classes("text-xs text-gray-400")
                            ui.label(str(v) if v not in (None, "") else "—")
                        _kv("季度", _q_label(cur.quarter) if cur.quarter else "—")
                        _kv("放送", f"{cur.air_date or '—'}{wd}")
                        _kv("类型", cur.platform)
                        _kv("评分", cur.rating)
                        _kv("来源", " · ".join(sources) or "—")
                    if cur.bangumi_id:
                        ui.link(f"bgm.tv/subject/{cur.bangumi_id}",
                                f"https://bgm.tv/subject/{cur.bangumi_id}").props(
                            "target=_blank").classes("text-xs")
                    ui.label(f"原始标题: {cur.title}").classes("text-xs text-gray-500")
            if cur.summary:
                ui.separator()
                ui.label(cur.summary).classes("text-sm text-gray-300 whitespace-pre-wrap")

        with ui.row().classes("items-center gap-3 flex-wrap"):
            ui.button("重新识别", icon="refresh", on_click=_enrich).props("flat size=sm")
            if cur.rejected:
                ui.button("恢复", icon="undo", on_click=_restore).props("size=sm color=primary")
            else:
                ui.button("忽略", on_click=_reject).props("size=sm flat color=grey")
            if sources:
                opts = {"": "按优先级"}
                for sname in sources:
                    opts[sname] = sname
                ui.select(opts, value=(cur.pref_source or ""), label="下载源",
                          on_change=_set_source).props("dense outlined").classes("min-w-40")
        if not cur.bangumi_id:
            with ui.row().classes("items-center gap-2 flex-wrap"):
                inp = ui.input(placeholder="bgm 链接或 ID，如 bgm.tv/subject/464376 或 464376").props(
                    "dense outlined").classes("min-w-96")
                ui.button("绑定", icon="link", on_click=lambda: _bind(inp)).props("size=sm color=primary")

        ui.label(f"版本 / 种子（{len(ts)}）").classes("text-sm font-bold mt-2")
        if not ts:
            ui.label("（还没有种子）").classes("text-gray-400")
            return
        for t in ts:
            with ui.row().classes("items-center gap-2 w-full py-1 text-sm").style(
                    "border-bottom:1px solid rgba(255,255,255,.08)"):
                ui.label(str(t.release_time or t.created_at)[:16]).classes("w-28 text-gray-400")
                ui.label(t.raw_title or t.source).classes("grow break-all")
                live = qb_live_text(t)
                if live:
                    ui.badge(live).props("color=teal").tooltip("qB 实时状态")
                else:
                    ui.badge(_STATUS.get(t.status, t.status)).props("color=blue-grey")
                ui.button("下载", icon="download", on_click=_force(t.id)).props(
                    "size=sm flat dense").tooltip("强制下这一版本到文件夹")
                if t.status in ("downloaded", "downloading"):
                    ui.button(icon="delete_forever", on_click=_del(t.id)).props(
                        "size=sm flat dense color=negative").tooltip("删除这一版本的文件（qB+硬盘，不可撤销）")

    def _after():
        body.refresh()
        if refresh_outer:
            refresh_outer()

    def _set_source(e):
        mov.set_movie_pref(movie_id, e.value or "")
        body.refresh()
        ui.notify("下载源：" + (e.value or "按优先级"))

    async def _enrich():
        ok = await mov.enrich_movie(movie_id)
        _after()
        ui.notify("识别成功" if ok else "未识别到（可粘贴 bgm 链接绑定）")

    def _reject():
        mov.reject_movie(movie_id)
        _after()
        ui.notify("已忽略")

    def _restore():
        mov.restore_movie(movie_id)
        _after()
        ui.notify("已恢复")

    def _force(mt_id):
        async def h():
            ok = await mov.download_movie_torrent(mt_id)
            _after()
            if ok:
                ui.notify("已强制下载到文件夹", type="positive")
            elif not config.QB_ENABLED:
                ui.notify("未启用 qB（QB_ENABLED=false），无法真正下载", type="warning")
            else:
                ui.notify("下载失败，看日志", type="negative")
        return h

    def _del(mt_id):
        def open_confirm():
            dlg = ui.dialog()
            with dlg, ui.card():
                ui.label("删除这一版本的文件？").classes("font-bold")
                ui.label("通过 qB 连同硬盘文件一起删除，不可撤销。").classes("text-xs text-gray-400")
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("取消", on_click=dlg.close).props("flat")

                    async def _do():
                        dlg.close()
                        ok = await mov.delete_movie_torrent(mt_id)
                        _after()
                        ui.notify("已删除该版本文件" if ok else "没删成（qB 未连上或无文件）",
                                  type="positive" if ok else "warning")
                    ui.button("删除文件", icon="delete_forever", on_click=_do).props("color=negative")
            dlg.open()
        return open_confirm

    async def _bind(inp):
        v = (inp.value or "").strip()
        m = re.search(r"subject/(\d+)", v) or re.search(r"(\d+)", v)
        if not m:
            ui.notify("请粘贴 bgm 链接或数字 ID", type="warning")
            return
        ok = await mov.bind_movie_bgm(movie_id, int(m.group(1)))
        _after()
        ui.notify("已绑定并识别 ✓" if ok else "绑定失败：ID 不存在或取不到 bgm 数据",
                  type="positive" if ok else "negative")

    body()


@ui.page("/movies")
def movies_page(t: str = "list"):
    """t = 当前 tab（写在 URL ?t= 里），刷新后停在同一 tab。"""
    with frame("movies"):
        list_page = {"n": 1}  # 列表分页：每页 20 个季度（5 年），当前页码
        detail_dlg = ui.dialog()

        def _list_goto(e):
            list_page["n"] = int(e.value)
            list_panel.refresh()

        def open_detail(movie_id):
            detail_dlg.clear()
            with detail_dlg, ui.card().classes("w-full").style("max-width:860px"):
                with ui.row().classes("w-full justify-end"):
                    ui.button(icon="close", on_click=detail_dlg.close).props("flat round dense")
                render_movie_detail(movie_id, refresh_outer=refresh_all)
            detail_dlg.open()

        # ---- 事件 ----
        async def _scan(year_in, seas_in):
            yr = int(year_in.value or datetime.now().year)
            letters = [x for x in (seas_in.value or []) if x in _SEASONS]
            if not letters:
                ui.notify("至少选一个季度", type="warning")
                return
            ui.notify(f"扫描 {yr} 年 {len(letters)} 个季度的剧场版/OVA…（走 Mikan+bgm，请稍候）")
            res = await mov.scan_now(yr, letters)
            refresh_all()
            tail = f"，{res['errors']} 个出错" if res["errors"] else ""
            ui.notify(
                f"扫描完成：命中 {res['seen']} 部，新增 {res['movies']}，种子 {res['torrents']}{tail}",
                type="positive")

        def _download(movie_id):
            async def h():
                n = await mov.download_movie(movie_id)
                refresh_all()
                if n:
                    ui.notify("已触发下载（一个最佳版本；要别的版本点番名进详情逐条下）", type="positive")
                elif not config.QB_ENABLED:
                    ui.notify("未启用 qB（设置页开 QB_ENABLED 才真正下载）", type="warning")
                else:
                    ui.notify("没有可下的版本（可能已下过）", type="warning")
            return h

        def _reject(movie_id):
            def h():
                mov.reject_movie(movie_id)
                refresh_all()
                ui.notify("已忽略（『已忽略』tab 可恢复）")
            return h

        def _restore(movie_id):
            def h():
                mov.restore_movie(movie_id)
                refresh_all()
                ui.notify("已恢复")
            return h

        def _bind(movie_id, inp):
            async def h():
                v = (inp.value or "").strip()
                m = re.search(r"subject/(\d+)", v) or re.search(r"(\d+)", v)
                if not m:
                    ui.notify("请粘贴 bgm 链接或数字 ID", type="warning")
                    return
                ok = await mov.bind_movie_bgm(movie_id, int(m.group(1)))
                refresh_all()
                ui.notify("已绑定并识别 ✓" if ok else "绑定失败：ID 不存在或取不到 bgm 数据",
                          type="positive" if ok else "negative")
            return h

        def _refail(movie_id):
            async def h():
                ok = await mov.enrich_movie(movie_id)
                refresh_all()
                ui.notify("识别成功 ✓" if ok else "还是没识别到（可粘贴 bgm 链接绑定）",
                          type="positive" if ok else "warning")
            return h

        def _save_scan(f):
            try:
                secs = max(3600, int(float(f["hours"].value or 12) * 3600))
            except (ValueError, TypeError):
                ui.notify("间隔要填数字（小时）", type="warning")
                return
            config.set_many({
                "MOVIE_SCAN_ENABLED": "true" if f["enabled"].value else "false",
                "MOVIE_SCAN_INTERVAL": str(secs),
            })
            sources_panel.refresh()
            on = "开" if f["enabled"].value else "关"
            ui.notify(f"已保存：自动扫描{on}，每 {secs // 3600} 小时一次", type="positive")

        def _movie_card(m):
            ts = mov.movie_torrents(m.id)
            ndone = sum(1 for t in ts if t.status in ("downloaded", "downloading"))
            with ui.card().classes("w-full"):
                with ui.row().classes("gap-3 items-start no-wrap w-full"):
                    if m.cover_url:
                        ui.image(m.cover_url).classes("rounded").style("min-width:4rem;width:4rem")
                    with ui.column().classes("gap-1 grow min-w-0"):
                        with ui.row().classes("items-center gap-2 flex-wrap"):
                            ui.badge(m.mikan_type or "剧场版").props("color=deep-purple")  # Mikan 桶判定
                            ui.label(name_of(m)).classes(
                                "cursor-pointer text-blue-400 hover:underline font-bold").on(
                                "click", lambda mid=m.id: open_detail(mid))
                            if not m.bangumi_id:
                                ui.badge("未识别").props("color=red").tooltip("bgm 没匹配上，去『待识别』手动绑定")
                        with ui.row().classes("gap-4 text-xs text-gray-400 flex-wrap"):
                            ui.label(f"放送 {m.air_date or '—'}")
                            ui.label(f"版本 {len(ts)}")
                            ui.label(f"已下 {ndone}")
                            ui.label("来源 " + (" · ".join(mov.movie_sources(m.id)) or "—"))
                    with ui.column().classes("gap-1 items-end shrink-0"):
                        ui.button("下载", icon="download", on_click=_download(m.id)).props(
                            "size=sm color=primary").tooltip("下一个最佳版本；要别的版本点番名进详情逐条下")
                        ui.button("忽略", icon="block", on_click=_reject(m.id)).props(
                            "size=sm flat color=grey")

        # ---- 面板 ----
        @ui.refreshable
        def overview_panel():
            ov = mov.overview()
            k = ov["kpi"]
            cards = [("电影", k["total"], ""), ("已识别", k["matched"], ""),
                     ("待识别", k["unmatched"], "red"), ("已下", k["downloaded"], "green"),
                     ("已忽略", k["rejected"], ""), ("版本", k["versions"], "")]
            with ui.row().classes("gap-3 flex-wrap p-1"):
                for label, val, hi in cards:
                    with ui.card().classes("items-center px-5 py-2"):
                        cls = "text-2xl font-bold" + (f" text-{hi}-400" if hi and val else "")
                        ui.label(str(val)).classes(cls)
                        ui.label(label).classes("text-xs text-gray-400")
            if not ov["config"]["qb"]:
                with ui.row().classes("items-center gap-2 p-2 rounded w-full").style(
                        "background:rgba(234,179,8,.12)"):
                    ui.icon("warning").classes("text-yellow-500")
                    ui.label("qB 未启用：剧场版也只采集不下载（设置页开 QB_ENABLED 后生效）").classes(
                        "text-sm text-yellow-200")
            else:
                q = ov["qb"]
                with ui.row().classes("gap-2 flex-wrap pl-1 items-center mt-1"):
                    ui.badge(f"qB 跟踪 {q['tracked']}").props("color=teal").classes("text-sm")
                    ui.badge(f"下载中 {q['downloading']}").props("color=teal").classes("text-sm")
                    ui.badge(f"做种 {q['seeding']}").props("color=teal").classes("text-sm")
                    if q["dlspeed"]:
                        ui.badge(f"↓ {human_size(q['dlspeed'])}/s").props("color=teal").classes("text-sm")
            ui.label(f"各季度（电影数）· {len(ov['by_quarter'])}").classes("text-sm font-bold mt-3 pl-1")
            with ui.column().classes("w-full gap-0 pl-1"):
                maxv = max((tot for _, tot, _ in ov["by_quarter"]), default=1) or 1
                if not ov["by_quarter"]:
                    ui.label("—").classes("text-gray-500 text-sm")
                for qk, tot, _ in ov["by_quarter"]:
                    with ui.row().classes("items-center gap-3 w-full text-sm py-0.5"):
                        ui.label(_q_label(qk)).classes("w-36 shrink-0 truncate")
                        with ui.element("div").classes("grow rounded").style(
                                "background:rgba(255,255,255,.07);height:12px"):
                            ui.element("div").style(
                                f"width:{tot / maxv * 100:.1f}%;height:12px;background:#a855f7;border-radius:6px")
                        ui.label(f"{tot}").classes("shrink-0 text-gray-400 text-right").style(
                            "min-width:5rem")

        @ui.refreshable
        def recent_panel():
            ui.label("新入库（最近 50 条种子）").classes("text-sm font-bold mt-4 pl-1")
            rows = [{
                "id": r["id"],
                "time": r["time"],
                "name": r["name"],
                "src": r["source"],
                "raw": r["raw"] or "—",
                "status": _STATUS.get(r["status"], r["status"]),
            } for r in mov.recent_movie_rows(50)]
            tbl = ui.table(
                columns=[
                    {"name": "time", "label": "时间", "field": "time", "align": "left"},
                    {"name": "name", "label": "剧场版", "field": "name", "align": "left"},
                    {"name": "src", "label": "来源", "field": "src", "align": "left"},
                    {"name": "status", "label": "状态", "field": "status", "align": "left"},
                ],
                rows=rows, row_key="id",
            ).classes("w-full")
            # 剧场版名下压一行灰色原始种子名：长名自动换行、完整显示。
            tbl.add_slot("body-cell-name", r'''
                <q-td :props="props">
                    <div>{{ props.row.name }}</div>
                    <div class="text-grey-6"
                         style="font-size:11px;white-space:normal;word-break:break-all">
                        {{ props.row.raw }}
                    </div>
                </q-td>
            ''')

        @ui.refreshable
        def list_panel():
            from core import anime
            items = mov.list_movies()
            if not items:
                ui.label("还没有剧场版/OVA。去『订阅源』tab 点『扫描』从 Mikan 拉取。").classes(
                    "text-gray-400 p-4")
                return
            by_q: dict[str, list] = {}
            for m in items:
                by_q.setdefault(m.quarter or "未知", []).append(m)
            quarters = sorted((q for q in by_q if q != "未知"), reverse=True)
            if "未知" in by_q:
                quarters.append("未知")
            shown, total_pages, page = paginate(quarters, list_page["n"], 20)
            list_page["n"] = page
            exps: list = []
            with ui.row().classes("items-center gap-3 pl-1 pb-1 flex-wrap"):
                expand_collapse_bar(exps)
                if total_pages > 1:
                    ui.pagination(1, total_pages, direction_links=True, value=page,
                                  on_change=_list_goto).props("size=sm")
                    ui.label(f"共 {total_pages} 页 · 每页 5 年").classes("text-xs text-gray-500")
            for q in shown:
                grp = by_q[q]
                exp = ui.expansion(f"{anime.quarter_label(q)}   ·   {len(grp)} 部",
                                   value=True).classes("w-full")
                exps.append(exp)
                with exp:
                    for m in grp:
                        _movie_card(m)

        @ui.refreshable
        def fail_panel():
            items = mov.list_unmatched_movies()
            if not items:
                ui.label("没有待识别的剧场版。（bgm 没自动匹配上的会出现在这里，可绑定或忽略）").classes(
                    "text-gray-400 p-4")
                return
            ui.label("这些剧场版没自动匹配到 bgm：缺规范名/日语文件夹名/季度。可『重试识别』或粘贴 bgm 链接『绑定』。").classes(
                "text-xs text-gray-400 p-2")
            for m in items:
                with ui.card().classes("w-full"):
                    with ui.row().classes("items-center gap-3 flex-wrap"):
                        ui.badge("未识别").props("color=red")
                        ui.label(name_of(m)).classes(
                            "cursor-pointer text-blue-400 hover:underline").on(
                            "click", lambda mid=m.id: open_detail(mid))
                        ui.label("来源: " + (" · ".join(mov.movie_sources(m.id)) or "—")).classes(
                            "text-xs text-gray-400")
                    with ui.row().classes("items-center gap-2 flex-wrap"):
                        inp = ui.input(placeholder="bgm 链接或 ID").props("dense outlined").classes("min-w-96")
                        ui.button("绑定", icon="link", on_click=_bind(m.id, inp)).props("size=sm color=primary")
                        ui.button("重试识别", icon="refresh", on_click=_refail(m.id)).props("size=sm flat")
                        ui.button("忽略", on_click=_reject(m.id)).props("size=sm flat color=grey")

        @ui.refreshable
        def reject_panel():
            rej = mov.list_rejected_movies()
            if not rej:
                ui.label("没有已忽略的剧场版。（列表里点『忽略』会进这里，可随时恢复）").classes(
                    "text-gray-400 p-4")
                return
            for m in rej:
                with ui.row().classes("items-center gap-3 pl-2 py-1 flex-wrap"):
                    ui.badge("已忽略").props("color=grey")
                    ui.label(name_of(m)).classes(
                        "cursor-pointer text-gray-400 line-through hover:underline").on(
                        "click", lambda mid=m.id: open_detail(mid))
                    ui.label("来源: " + (" · ".join(mov.movie_sources(m.id)) or "—")).classes(
                        "text-xs text-gray-400")
                    ui.button("恢复", icon="undo", on_click=_restore(m.id)).props("size=sm flat color=primary")

        @ui.refreshable
        def sources_panel():
            ui.label("剧场版/OVA 的来源固定为 Mikan 季度浏览页的『剧场版/OVA 桶』——非 RSS 订阅，"
                     "不用像番剧那边配字幕组。识别走 bgm；判成周更 TV 的会自动转去番剧『待确认』。").classes(
                "text-xs text-gray-400 mb-2")

            # 自动扫描（定期自动抓，无需手动）
            with ui.card().classes("w-full"):
                ui.label("自动扫描").classes("font-bold")
                f = {}
                f["enabled"] = ui.switch("开启自动扫描（后台定期扫『当年』四季的剧场版/OVA）",
                                         value=config.MOVIE_SCAN_ENABLED).props("dense")
                with ui.row().classes("items-center gap-3 flex-wrap"):
                    f["hours"] = ui.number("扫描间隔（小时）",
                                           value=round(config.MOVIE_SCAN_INTERVAL / 3600, 1),
                                           min=1, format="%g").classes("w-40")
                    ui.button("保存", icon="save", on_click=lambda: _save_scan(f)).props("color=primary")
                last = config.MOVIE_SCAN_LAST or "从未"
                ui.label(f"上次扫描：{last}").classes("text-xs text-gray-400")
                ui.label("剧场版桶更新不频繁，间隔别设太小；改动即时生效，到点自动扫。").classes(
                    "text-xs text-gray-500")

            # 手动立即扫描（可指定年份/季度回填历史）
            with ui.card().classes("w-full"):
                ui.label("手动立即扫描").classes("font-bold")
                with ui.row().classes("items-end gap-3 flex-wrap"):
                    year = ui.number("年份", value=datetime.now().year, format="%d").classes("w-28")
                    seas = ui.select(_SEASONS, multiple=True, value=list(_SEASONS),
                                     label="季度").props("dense outlined").classes("min-w-64")
                    ui.button("立即扫描", icon="travel_explore",
                              on_click=lambda: _scan(year, seas)).props("color=primary")
                ui.label("想补抓往年的剧场版就改年份手动扫；日常交给上面的自动扫描即可。").classes(
                    "text-xs text-gray-500")

        def refresh_all():
            overview_panel.refresh()
            recent_panel.refresh()
            list_panel.refresh()
            fail_panel.refresh()
            reject_panel.refresh()
            sources_panel.refresh()

        # ---- 标签 ----
        with ui.tabs().classes("w-full") as tabs:
            ui.tab("overview", "仪表盘", "dashboard")
            ui.tab("list", "列表", "movie")
            ui.tab("fail", "待识别", "sync_problem")
            ui.tab("reject", "已忽略", "block")
            ui.tab("sources", "订阅源", "rss_feed")
        tabs.on_value_change(lambda e: ui.run_javascript(
            f"history.replaceState(null,'','?t='+encodeURIComponent('{e.value}'))"))
        start = t if t in _TABS else "list"
        with ui.tab_panels(tabs, value=start).classes("w-full"):
            with ui.tab_panel("overview"):
                overview_panel()
                recent_panel()
            with ui.tab_panel("list"):
                list_panel()
            with ui.tab_panel("fail"):
                fail_panel()
            with ui.tab_panel("reject"):
                reject_panel()
            with ui.tab_panel("sources"):
                sources_panel()
