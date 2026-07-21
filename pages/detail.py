"""番剧详情：可作为独立页 /anime/{id}，也可用 render_detail 渲染进悬浮框(dialog)。"""
from nicegui import ui

from core import anime
import config
from .layout import confirm, ep_str, frame, name_of, qb_live_text, season_label

_STATUS = {"downloaded": "已下", "pending": "待下", "downloading": "下载中",
           "error": "失败", "skipped": "跳过"}
_WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def render_detail(anime_id: int, refresh_outer=None) -> None:
    """把某番详情渲染进当前容器。refresh_outer：改动数据后刷新外层列表（番剧列表/待确认/已忽略 等）。"""
    if anime.get_anime(anime_id) is None:
        ui.label("番剧不存在").classes("text-gray-400 p-4")
        return

    @ui.refreshable
    def body():
        cur = anime.get_anime(anime_id)
        if cur is None:
            ui.label("番剧不存在").classes("text-gray-400 p-4")
            return
        eps = anime.list_episodes(anime_id)
        sources = sorted({t.source for t in eps})
        with ui.row().classes("items-center gap-2 flex-wrap"):
            ui.label(name_of(cur)).classes("text-2xl font-bold")
            _sl = season_label(cur)
            if _sl:
                ui.badge(_sl).props("color=blue-grey")
            if cur.rejected:
                ui.badge("已忽略").props("color=grey")
            else:
                ui.badge("✓ 已确认" if cur.confirmed else "⏳ 待确认").props(
                    f"color={'green' if cur.confirmed else 'orange'}")

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
                        _kv("季度", anime.quarter_label(cur.quarter))
                        _kv("放送", f"{cur.air_date or '—'}{wd}")
                        _kv("类型", cur.platform)
                        _kv("总集数", cur.total_episodes)
                        _kv("评分", cur.rating)
                        _kv("来源", " · ".join(sources) or "—")
                    if cur.bangumi_id:
                        ui.link(f"bgm.tv/subject/{cur.bangumi_id}",
                                f"https://bgm.tv/subject/{cur.bangumi_id}").props("target=_blank").classes("text-xs")
                    ui.label(f"原始标题: {cur.title}").classes("text-xs text-gray-500")
            if cur.summary:
                ui.separator()
                ui.label(cur.summary).classes("text-sm text-gray-300 whitespace-pre-wrap")

        # 操作
        with ui.row().classes("items-center gap-3 flex-wrap"):
            ui.button("重新识别", icon="refresh", on_click=_enrich).props("flat size=sm")
            if cur.rejected:
                ui.button("恢复订阅", icon="undo", on_click=_restore).props("size=sm color=primary")
            else:
                if not cur.confirmed:
                    ui.button("确认下载", on_click=_confirm).props("size=sm color=primary")
                ui.button("忽略", on_click=_reject).props("size=sm flat color=grey")
            ui.button("补下本番", on_click=_download).props("flat size=sm")
            if sources:  # 首选下载源（多源时选从哪个组下）
                opts = {"": "按优先级"}
                for sname in sources:
                    opts[sname] = sname
                ui.select(opts, value=(cur.pref_source or ""), label="下载源",
                          on_change=_set_source).props("dense outlined").classes("min-w-40")

        # 分集 / 种子（每条可单独强制下载）
        ui.label(f"分集 / 种子（{len(eps)}）").classes("text-sm font-bold mt-2")
        if not eps:
            ui.label("（还没有种子）").classes("text-gray-400")
            return
        for t in eps:
            with ui.row().classes("items-center gap-2 w-full py-1 text-sm").style(
                    "border-bottom:1px solid rgba(255,255,255,.08)"):
                ui.label(f"第{ep_str(t.episode)}集").classes("w-14")
                ui.label(str(t.release_time or t.created_at)[:16]).classes("w-28 text-gray-400")
                ui.label(t.source).classes("grow break-all")
                live = qb_live_text(t)
                if live:  # qB 实时态（下载中 45% ↓2MB/s / 做种 100%）优先展示
                    ui.badge(live).props("color=teal").tooltip("qB 实时状态")
                else:
                    ui.badge(_STATUS.get(t.status, t.status)).props("color=blue-grey")
                ui.button("下载", icon="download", on_click=_force(t.id)).props(
                    "size=sm flat dense").tooltip("强制下这一条到文件夹（无视去重/优先级）")
                if t.status in ("downloaded", "downloading"):  # 下过才给按集删
                    ui.button(icon="delete_forever", on_click=_del_one(t.id)).props(
                        "size=sm flat dense color=negative").tooltip("删除这一集的文件（qB+硬盘，不可撤销）")

    def _after():
        body.refresh()
        if refresh_outer:
            refresh_outer()

    # ---- 事件 ----
    def _set_source(e):
        anime.set_pref_source(anime_id, e.value or "")
        body.refresh()
        ui.notify("下载源：" + (e.value or "按优先级"))

    async def _enrich():
        ok = await anime.enrich_anime(anime_id)
        _after()
        ui.notify("识别成功" if ok else "未识别到（Mikan/bgm 没有或查不到）")

    async def _confirm():
        anime.confirm_anime(anime_id)
        n = await anime.download_pending_for_anime(anime_id)
        _after()
        ui.notify(f"已确认，补下 {n} 集")

    def _reject():
        anime.reject_anime(anime_id)
        _after()
        ui.notify("已忽略，移到『已忽略』页")

    async def _restore():
        anime.restore_anime(anime_id)
        n = await anime.download_pending_for_anime(anime_id)
        _after()
        ui.notify(f"已恢复到『订阅中』，补下 {n} 集")

    async def _download():
        n = await anime.download_pending_for_anime(anime_id)
        _after()
        ui.notify(f"已触发补下 {n} 集")

    def _force(torrent_id):
        async def h():
            ok = await anime.download_anime_torrent(torrent_id, force=True)
            _after()
            if ok:
                ui.notify("已强制下载到文件夹", type="positive")
            elif not config.QB_ENABLED:
                ui.notify("未启用 qB（QB_ENABLED=false），无法真正下载", type="warning")
            else:
                ui.notify("下载失败，看日志", type="negative")
        return h

    def _del_one(torrent_id):
        async def h():
            if not await confirm("删除这一集的文件？",
                                 "通过 qB 连同硬盘文件一起删除，不可撤销。",
                                 ok_label="删除文件", ok_icon="delete_forever"):
                return
            ok = await anime.delete_anime_torrent(torrent_id)
            _after()
            ui.notify("已删除该集文件" if ok else "没删成（qB 未连上或该集无文件）",
                      type="positive" if ok else "warning")
        return h

    body()


@ui.page("/anime/{anime_id}")
def detail_page(anime_id: int):
    with frame():
        ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/")).props("flat round dense")
        render_detail(anime_id)
