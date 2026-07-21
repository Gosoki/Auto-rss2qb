"""OVA・剧场版页 `/movies`：扫描 Mikan 剧场版/OVA 桶 → bgm 识别 → 审批 / 逐版本下载。

剧场版数据（Movie/MovieTorrent）与 TV 番剧完全分离，逻辑在 movies.py；本页只管展示与交互。
详情/下载复用统一的视觉 helper（name_of / qb_live_text），但走剧场版自己的操作。
"""
import re
from datetime import datetime

from nicegui import ui

import config
import movies as mov
from .layout import frame, name_of, qb_live_text

_SEASONS = {"A": "冬", "B": "春", "C": "夏", "D": "秋"}
_STATUS = {"downloaded": "已下", "pending": "待下", "downloading": "下载中",
           "error": "失败", "skipped": "跳过"}
_WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


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
            ui.badge(cur.platform or "剧场版").props("color=deep-purple")
            if cur.rejected:
                ui.badge("已忽略").props("color=grey")
            elif not cur.bangumi_id:
                ui.badge("未识别").props("color=red")

        # 元信息卡（封面 + bgm 元数据 + 简介）
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

        # 操作
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
        if not cur.bangumi_id:  # 未识别 → 手动绑定 bgm
            with ui.row().classes("items-center gap-2 flex-wrap"):
                inp = ui.input(placeholder="bgm 链接或 ID，如 bgm.tv/subject/464376 或 464376").props(
                    "dense outlined").classes("min-w-96")
                ui.button("绑定", icon="link", on_click=lambda: _bind(inp)).props("size=sm color=primary")

        # 版本 / 种子（剧场版=一部作品，各条即不同字幕组/画质版本；逐条可下/删）
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


def _q_label(q: str) -> str:
    import core
    return core.quarter_label(q)


@ui.page("/movies")
def movies_page():
    with frame("movies"):
        ui.label("OVA・剧场版").classes("text-2xl font-bold")
        ui.label("从 Mikan 各季度『剧场版/OVA』发现，识别走 bgm；bgm 判成周更 TV 的会跳过（电影只抓电影）。").classes(
            "text-xs text-gray-400 mb-2")

        detail_dlg = ui.dialog()

        def open_detail(movie_id):
            detail_dlg.clear()
            with detail_dlg, ui.card().classes("w-full").style("max-width:860px"):
                with ui.row().classes("w-full justify-end"):
                    ui.button(icon="close", on_click=detail_dlg.close).props("flat round dense")
                render_movie_detail(movie_id, refresh_outer=movie_list.refresh)
            detail_dlg.open()

        with ui.card().classes("w-full"):
            with ui.row().classes("items-end gap-3 flex-wrap"):
                year = ui.number("年份", value=datetime.now().year, format="%d").classes("w-28")
                seas = ui.select(_SEASONS, multiple=True, value=list(_SEASONS),
                                 label="季度").props("dense outlined").classes("min-w-64")
                ui.button("扫描剧场版/OVA", icon="travel_explore",
                          on_click=lambda: _scan(year, seas)).props("color=primary")
            ui.label("首次扫描要抓 Mikan 季度页 + 每部详情 + bgm，稍慢；抓到的种子默认待人工下载。").classes(
                "text-xs text-gray-500")

        async def _scan(year_in, seas_in):
            yr = int(year_in.value or datetime.now().year)
            letters = [x for x in (seas_in.value or []) if x in _SEASONS]
            if not letters:
                ui.notify("至少选一个季度", type="warning")
                return
            ui.notify(f"扫描 {yr} 年 {len(letters)} 个季度的剧场版/OVA…（走 Mikan+bgm，请稍候）")
            res = await mov.discover_movies(yr, letters)
            movie_list.refresh()
            tail = f"，{res['errors']} 个出错" if res["errors"] else ""
            tv = f"，转入 TV {res['to_tv_shows']} 部" if res["to_tv_shows"] else ""
            ui.notify(
                f"扫描完成：电影命中 {res['seen']} 部，新增 {res['movies']}，种子 {res['torrents']}{tv}{tail}",
                type="positive")

        def _download(movie_id):
            async def h():
                n = await mov.download_movie(movie_id)
                movie_list.refresh()
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
                movie_list.refresh()
                ui.notify("已忽略")
            return h

        def _movie_card(m):
            ts = mov.movie_torrents(m.id)
            ndone = sum(1 for t in ts if t.status in ("downloaded", "downloading"))
            with ui.card().classes("w-full"):
                with ui.row().classes("gap-3 items-start no-wrap w-full"):
                    if m.cover_url:
                        ui.image(m.cover_url).classes("rounded").style("min-width:4rem;width:4rem")
                    with ui.column().classes("gap-1 grow min-w-0"):
                        with ui.row().classes("items-center gap-2 flex-wrap"):
                            ui.badge(m.platform or "剧场版").props("color=deep-purple")
                            ui.label(name_of(m)).classes(
                                "cursor-pointer text-blue-400 hover:underline font-bold").on(
                                "click", lambda mid=m.id: open_detail(mid))
                            if not m.bangumi_id:
                                ui.badge("未识别").props("color=red").tooltip("bgm 没匹配上，可进详情手动绑定")
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

        @ui.refreshable
        def movie_list():
            items = mov.list_movies()
            if not items:
                ui.label("还没有剧场版/OVA。点上面『扫描剧场版/OVA』从 Mikan 拉取。").classes(
                    "text-gray-400 p-4")
                return
            by_q: dict[str, list] = {}
            for m in items:
                by_q.setdefault(m.quarter or "未知", []).append(m)
            quarters = sorted((q for q in by_q if q != "未知"), reverse=True)
            if "未知" in by_q:
                quarters.append("未知")
            import core
            for i, q in enumerate(quarters):
                grp = by_q[q]
                with ui.expansion(f"{core.quarter_label(q)}   ·   {len(grp)} 部",
                                  value=(i == 0)).classes("w-full"):
                    for m in grp:
                        _movie_card(m)

        movie_list()
