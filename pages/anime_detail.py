"""番剧详情组件：render_anime_detail 渲染进列表页的悬浮框(dialog)，不再有独立页路由。"""
from nicegui import ui

from core import anime, engine
import config
from .layout import (STATUS_CN, WEEKDAY_CN, confirm, ep_str, meta_card, name_of,
                     qb_live_text, season_label, source_options)


def render_anime_detail(anime_id: int, refresh_outer=None, on_close=None) -> None:
    """把某番详情渲染进当前容器。refresh_outer：改动数据后刷新外层列表（番剧列表/待确认/已忽略 等）。
    on_close：非空则在标题行右侧渲染 X 关闭键（关掉外层 dialog）。"""
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
        # 标题行：标题 + 第X季 + 状态徽章都贴在标题后；X 关闭键推到最右
        with ui.row().classes("items-center gap-2 flex-wrap w-full"):
            ui.label(name_of(cur)).classes("text-2xl font-bold")
            _sl = season_label(cur)
            if _sl:
                ui.badge(_sl).props("color=blue-grey")
            if cur.rejected:
                ui.badge("已忽略").props("color=grey")
            else:
                ui.badge("✓ 已确认" if cur.confirmed else "⏳ 待确认").props(
                    f"color={'green' if cur.confirmed else 'orange'}")
            ui.space()
            if on_close:
                ui.button(icon="close", on_click=on_close).props("flat round dense")

        # 元操作行：重新识别 / 忽略 —— 放在标题下面
        with ui.row().classes("items-center gap-2 flex-wrap"):
            ui.button("重新识别", icon="refresh", on_click=_enrich).props("flat size=sm").style(
                "font-size:12px")
            if cur.rejected:
                ui.button("恢复订阅", icon="undo", on_click=_restore).props(
                    "size=sm color=primary").style("font-size:12px")
            else:
                ui.button("忽略本番", icon="block", on_click=_reject).props(
                    "size=sm flat color=grey").style("font-size:12px")

        # 元信息卡（封面 + bgm 元数据 + 简介）
        wd = f"  {WEEKDAY_CN[cur.air_weekday]}" if cur.air_weekday is not None else ""
        meta_card(cur.cover_url, [
            ("季度", engine.quarter_label(cur.quarter)),
            ("放送", f"{cur.air_date or '—'}{wd}"),
            ("类型", cur.platform),
            ("总集数", cur.total_episodes),
            ("评分", cur.rating),
            ("来源", " · ".join(sources) or "—"),
        ], cur.bangumi_id, cur.title, cur.summary)

        # 下载类操作（重新识别/忽略在上面的元操作行）
        with ui.row().classes("items-center gap-3 flex-wrap"):
            if not cur.rejected and not cur.confirmed:
                ui.button("确认下载", on_click=_confirm).props("size=sm color=primary").style(
                    "font-size:12px")
            ui.button("补下本番", on_click=_download).props("flat size=sm").style("font-size:12px")
            if sources:  # 下载源：按优先级=多源兜底；选具体组=锁定，之后只下这个组
                ui.select(source_options(sources, "按优先级·多源兜底"),
                          value=(cur.pref_source or ""), label="下载源",
                          on_change=_set_source).props("dense outlined").classes("min-w-52").tooltip(
                    "『按优先级』= 多源自动挑、缺集用别的源兜底；"
                    "选某个组 = 锁定，之后只下这个组，它缺的集不兜底（自己来点下载）")

        # 分集 / 种子（每条可单独强制下载）
        ui.label(f"分集 / 种子（{len(eps)}）").classes("text-sm font-bold mt-2")
        if not eps:
            ui.label("（还没有种子）").classes("text-gray-400")
            return
        plan = anime.download_plan(anime_id)  # 待下里『会真下』的那些（首选/锁定组），其余待下=备用
        for t in eps:
            ep_txt = f"第{ep_str(t.episode)}集"
            with ui.column().classes("w-full gap-0 py-1").style(
                    "border-bottom:1px solid rgba(255,255,255,.08)"):
                # 第一行：集号 · 字幕组 · 时间 同一行居中（天然竖直齐平），状态/按钮 space() 推到最右
                with ui.row().classes("items-center gap-3 w-full text-sm no-wrap"):
                    ui.label(ep_txt).classes("shrink-0")                  # 主要信息：集号
                    ui.label(t.source or "—").classes("shrink-0")         # 主要信息：字幕组（同色）
                    ui.label(engine.torrent_time(t)).classes("shrink-0 text-gray-500").style(
                        "font-size:11px")                                 # 次要：时间
                    ui.space()
                    live = qb_live_text(t)
                    if live:  # qB 实时态（下载中 45% ↓2MB/s / 做种 100%）优先展示
                        ui.badge(live).props("color=teal").tooltip("qB 实时状态")
                    elif t.status == "pending":  # 待下：区分『会真下的首选』和『备用』
                        if t.id in plan:
                            ui.badge("将下载").props("color=green").tooltip(
                                "这一集的首选版本，补下/自动下会下它")
                        else:
                            ui.badge("备用").props("color=blue-grey").tooltip(
                                "不会自动下：同集已由首选/已下覆盖，或非锁定源；要它就点右边下载")
                    elif t.status == "error":
                        ui.badge("失败·可补下" if t.id in plan else "失败").props(
                            f"color={'orange' if t.id in plan else 'red'}").tooltip(
                            "下载失败过；点右边『下载』或『补下本番』手动重试（后台不自动重试 error）"
                            if t.id in plan else "下载失败过")
                    else:
                        ui.badge(STATUS_CN.get(t.status, t.status)).props("color=blue-grey")
                    ui.button("下载", icon="download", on_click=_force(t.id)).props(
                        "size=sm flat dense").style("font-size:12px").tooltip(
                        "强制下这一条到文件夹（无视去重/优先级）")
                    if t.status in ("downloaded", "downloading"):  # 下过才给按集删
                        ui.button(icon="delete_forever", on_click=_del_one(t.id)).props(
                            "size=sm flat dense color=negative").tooltip("删除这一集的文件（qB+硬盘，不可撤销）")
                # 第二行：种子原名（次要，同时间灰）——隐形『第N集』占位精确对齐字幕组（任意集号宽度都准）
                with ui.row().classes("items-start gap-3 w-full text-sm no-wrap"):
                    ui.label(ep_txt).classes("shrink-0").style("visibility:hidden")
                    ui.label(t.raw_title or "—").classes("text-gray-500 break-all min-w-0").style(
                        "font-size:11px")

    def _after():
        body.refresh()
        if refresh_outer:
            refresh_outer()

    # ---- 事件 ----
    def _set_source(e):
        anime.set_pref_source(anime_id, e.value or "")
        body.refresh()
        if e.value:
            ui.notify(f"已锁定：之后只下 {e.value}（缺集不兜底，自己来点下载）", type="warning")
        else:
            ui.notify("已改回『按优先级』：多源自动挑、缺集用别的源兜底", type="positive")

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
