"""番剧详情组件：render_anime_detail 渲染进列表页的悬浮框(dialog)，不再有独立页路由。"""
from nicegui import ui

from core import anime, engine
import config
from .layout import (WEEKDAY_CN, confirm, ep_str, meta_card, name_of, parse_bgm_id,
                     qb_live_text, season_label, source_options, torrent_status_cn)


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
        # 标题行：标题+第X季+状态徽章塞进左侧可换行容器（grow/min-w-0），窄屏先把它们挤下去换行；
        # 外层 no-wrap + X 用 shrink-0 → X 永远钉在右上角，不被挤走。items-start 让 X 贴顶。
        with ui.row().classes("items-start gap-2 w-full no-wrap"):
            with ui.row().classes("items-center gap-2 flex-wrap grow min-w-0"):
                ui.label(name_of(cur)).classes("text-2xl font-bold")
                ui.button(icon="edit", on_click=_bind).props("flat round dense size=sm color=primary").tooltip(
                    "认错了？手动绑定正确的 bgm（粘链接或 ID）")
                _sl = season_label(cur)
                if _sl:
                    ui.badge(_sl).props("color=blue-grey")
                if cur.rejected:
                    ui.badge("已忽略").props("color=grey")
                else:
                    ui.badge("✓ 已确认" if cur.confirmed else "⏳ 待确认").props(
                        f"color={'green' if cur.confirmed else 'orange'}")
            if on_close:
                ui.button(icon="close", on_click=on_close).props("flat round dense").classes(
                    "shrink-0")

        # 元操作行：重新识别 / 忽略 —— 放在标题下面（dense 收紧内边距 + -ml-1 抵消残余左边距，跟标题左缘对齐）
        with ui.row().classes("items-center gap-3 flex-wrap -ml-1"):
            ui.button("重新识别", icon="refresh", on_click=_enrich).props(
                "flat dense size=sm").style("font-size:12px")
            if cur.rejected:
                ui.button("恢复订阅", icon="undo", on_click=_restore).props(
                    "dense size=sm color=primary").style("font-size:12px")
            else:
                ui.button("忽略本番", icon="block", on_click=_reject).props(
                    "flat dense size=sm color=grey").style("font-size:12px")

        # 元信息卡（封面 + bgm 元数据 + 简介）
        wd = f"  {WEEKDAY_CN[cur.air_weekday]}" if cur.air_weekday is not None else ""
        meta_card(cur.cover_url, [
            ("日文", cur.jp_name),
            ("集数", cur.total_episodes),
            ("季度", engine.quarter_label(cur.quarter)),
            ("放送", f"{cur.air_date or '—'}{wd}"),
            ("类型", cur.platform),
            ("原作", cur.author),
            ("导演", cur.director),
            ("音乐", cur.music),
            ("声优", cur.cast),
            ("来源", " · ".join(sources) or "—"),
        ], cur.bangumi_id, cur.summary, rating=cur.rating)

        # 下载类操作（重新识别/忽略在上面的元操作行）
        with ui.row().classes("items-center gap-3 flex-wrap"):
            if sources:  # 下载源放最前：按优先级=多源兜底；选具体组=锁定，之后只下这个组
                ui.select(source_options(sources, "按优先级·多源兜底"),
                          value=(cur.pref_source or ""), label="下载源",
                          on_change=_set_source).props("dense outlined").classes("min-w-52").tooltip(
                    "『按优先级』= 多源自动挑、缺集用别的源兜底；"
                    "选某个组 = 锁定，之后只下这个组，它缺的集不兜底（自己来点下载）")
            if not cur.rejected and not cur.confirmed:
                ui.button("确认下载", on_click=_confirm).props("size=sm color=primary").style(
                    "font-size:12px")
            _dln = ui.button("下载该源", icon="download", on_click=_download).props(
                "flat dense size=sm").style("font-size:12px")
            _dln.set_enabled(config.QB_ENABLED)
            _dln.tooltip("qB 未启用，去设置页开启后可下载" if not config.QB_ENABLED
                         else "按左边『下载源』下：锁了某源→下该源缺的每一集；『按优先级』→每集下应下的那份，已下的跳过")
            _bf1 = ui.button("补齐该源", icon="playlist_add", on_click=_backfill_loose).props(
                "flat dense size=sm").style("font-size:12px")
            _bf1.tooltip("去 nyaa/Mikan 按名搜『当前下载源』的种子补漏收（季度过滤，你人工审核）。"
                         "入库后转『待确认』，点『确认下载』才下。")
            _bf2 = ui.button("自动补齐", icon="auto_awesome", on_click=_backfill_strict).props(
                "flat dense size=sm").style("font-size:12px")
            _bf2.tooltip("同『补齐该源』，但额外用番名近似过滤挡掉同名衍生作/别的季，更少需人工把关。")

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
                    if t.status in ("pending", "error"):  # 待下/失败的集号都可点改：不止 -2，
                        _neg = t.episode == -2                            # 解析也可能写错正整数(把分辨率/季号当集号)
                        ui.label(ep_txt).classes(
                            "shrink-0 cursor-pointer hover:underline"
                            + (" text-blue" if _neg else "")).on(
                            "click", _set_ep(t.id)).tooltip(
                            "集号没解析出来；点这里改" if _neg else "集号不对？点这里改")
                    else:
                        ui.label(ep_txt).classes("shrink-0")              # 主要信息：集号（已下等不可改）
                    ui.label(t.source or "—").classes("shrink-0")         # 主要信息：字幕组（同色）
                    ui.label(engine.torrent_time(t)).classes(
                        "shrink-0 text-gray-500 text-xs")                 # 次要：时间(12px)
                    ui.space()
                    live = qb_live_text(t)
                    if live:  # qB 实时态：完成(做种/100%)才绿，下载中用 teal
                        _done = (t.qb_progress or 0) >= 1
                        ui.badge(live).props(f"color={'green' if _done else 'teal'}").tooltip(
                            "qB 实时状态")
                    elif t.status == "pending":  # 待下：未知集 / 将下载 / 备用
                        if t.episode == -2:  # -2 后台不自动下，别标『将下载』
                            ui.badge("未知集").props("color=purple").tooltip(
                                "批量/集号没解析出来，后台不自动下。点左边『第?集』改集号，或下方排除")
                        elif t.id in plan:
                            ui.badge("将下载").props("color=blue").tooltip(
                                "这一集的首选版本，补下/自动下会下它")
                        else:
                            ui.badge("备用").props("color=blue-grey").tooltip(
                                "不会自动下：同集已由首选/已下覆盖，或非锁定源；要它就点右边下载")
                    elif t.status == "error":
                        ui.badge("失败·可补下" if t.id in plan else "失败").props(
                            f"color={'orange' if t.id in plan else 'red'}").tooltip(
                            "下载失败过；点右边『下载』或『补下本番』手动重试（后台不自动重试 error）"
                            if t.id in plan else "下载失败过")
                    else:  # 无 qB 实时态：刚交付未同步→下载中；其余(已下完/跳过/已删/已排除)按状态
                        ui.badge(torrent_status_cn(t.status, t.qb_progress, t.qb_synced_at)).props(
                            "color=blue-grey")
                    if t.status == "excluded":  # 已排除：给『恢复』放回待下
                        ui.button("恢复", icon="undo", on_click=_unexclude(t.id)).props(
                            "size=sm flat dense color=primary").style("font-size:12px").tooltip(
                            "放回待下，重新参与下载/去重")
                    if t.status in ("pending", "error"):  # 未下载的才可直接排除
                        ui.button("排除", icon="block", on_click=_exclude(t.id)).props(
                            "size=sm flat dense color=grey").style("font-size:12px").tooltip(
                            "不想要这条：从待下直接排除（不删文件，只改状态；可撤销）")
                    if t.status in ("downloaded", "downloading"):  # 下过才给按集删
                        ui.button(icon="delete_forever", on_click=_del_one(t.id)).props(
                            "size=sm flat dense color=negative").tooltip(
                            "删除这一集的文件（qB+硬盘，不可撤销）")
                    _dlb = ui.button("下载", icon="download", on_click=_force(t.id)).props(  # 下载放最后
                        "size=sm flat dense").style("font-size:12px")
                    _dlb.set_enabled(config.QB_ENABLED)
                    _dlb.tooltip("强制下这一条到文件夹（无视去重/优先级）" if config.QB_ENABLED
                                 else "qB 未启用，去设置页开启后可下载")
                # 第二行：种子原名（次要，同时间灰）——隐形『第N集』占位精确对齐字幕组（任意集号宽度都准）
                with ui.row().classes("items-start gap-3 w-full text-sm no-wrap"):
                    ui.label(ep_txt).classes("shrink-0").style("visibility:hidden")
                    ui.label(t.raw_title or "—").classes(
                        "text-gray-500 break-all min-w-0 text-xs")

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

    async def _bind():
        dlg = ui.dialog()
        with dlg, ui.card().classes("gap-2"):
            ui.label("绑定 bgm").classes("font-bold")
            ui.label("自动认错了就把正确的 bgm 链接或 ID 填这——直接取权威元数据覆盖。").classes(
                "text-xs text-gray-400")
            inp = ui.input(placeholder="bgm.tv/subject/464376 或 464376").props(
                "dense outlined autofocus").classes("min-w-80")
            with ui.row().classes("gap-2 justify-end w-full"):
                ui.button("取消", on_click=lambda: dlg.submit(None)).props("flat")
                ui.button("绑定", icon="link",
                          on_click=lambda: dlg.submit(inp.value)).props("color=primary")
        val = await dlg
        if not val:
            return
        bid = parse_bgm_id(val)
        if not bid:
            ui.notify("没认出 bgm ID（粘 bgm.tv/subject/数字 或纯数字）", type="warning")
            return
        ok = await anime.bind_anime_bgm(anime_id, bid)
        _after()
        ui.notify("已绑定并识别 ✓（回到待确认，去点确认下载）" if ok
                  else "绑定失败：ID 不存在或取不到 bgm 数据", type="positive" if ok else "negative")

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
        ui.notify(f"已触发下载 {n} 集")

    _bf_busy = {"v": False}   # 防长耗时补齐期间重复点击/双击并发跑多次

    async def _run_backfill(strict):
        if _bf_busy["v"]:
            ui.notify("正在补齐中，请稍候…", type="info")
            return
        _bf_busy["v"] = True
        ui.notify("正在搜索补齐…（去 nyaa/Mikan 按名搜，请稍候）")
        try:
            res = await anime.backfill_source(anime_id, strict)
        except Exception as e:            # 兜住 fetch 之外的意外，别逃逸崩掉处理器
            ui.notify(f"补齐出错：{e}", type="negative")
            return
        finally:
            _bf_busy["v"] = False
        _after()
        if res.get("error"):
            ui.notify(res["error"], type="warning")
        elif res["ingested"]:
            if res.get("to_confirm"):
                ui.notify(f"补入库 {res['ingested']} 条（搜到 {res['found']}）→ 已转『待确认』，去点『确认下载』",
                          type="positive")
            else:   # rejected 番：入库了但订阅态没变，不会自动下
                ui.notify(f"补入库 {res['ingested']} 条（搜到 {res['found']}），但本番仍在『已忽略』；"
                          "需先『恢复订阅』这些才会下载", type="warning")
        else:
            ui.notify(f"没有新种子可补（搜到 {res['found']}，都已有或被季号/名字过滤）", type="info")

    async def _backfill_loose():
        await _run_backfill(False)

    async def _backfill_strict():
        await _run_backfill(True)

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

    def _set_ep(torrent_id):
        async def h():
            dlg = ui.dialog()
            with dlg, ui.card().classes("gap-2"):
                ui.label("改集号").classes("font-bold")
                ui.label("这条集号没解析出来（批量/命名怪）。填对集号，它就进正常下载+去重流程。"
                         "支持 .5（如 12.5）。").classes("text-xs text-gray-400")
                num = ui.number("集号", value=1, min=0, step=1, format="%g").props(
                    "dense outlined autofocus")
                with ui.row().classes("gap-2 justify-end w-full"):
                    ui.button("取消", on_click=lambda: dlg.submit(None)).props("flat")
                    ui.button("确定", on_click=lambda: dlg.submit(num.value)).props("color=primary")
            val = await dlg
            if val is None:
                return
            if val < 0:
                ui.notify("集号要 ≥ 0", type="warning")
                return
            ok = anime.set_torrent_episode(torrent_id, float(val))
            _after()
            ui.notify(f"已改为第 {ep_str(float(val))} 集" if ok else "改不了（已下载的种子不改集号）",
                      type="positive" if ok else "warning")
        return h

    def _exclude(torrent_id):
        async def h():
            if not await confirm("排除这一条？",
                                 "从待下里直接排除（终态：不再下、不再挂在未知集，RSS 再遇到同种子也不重收）。",
                                 ok_label="排除", ok_icon="block"):
                return
            ok = anime.exclude_torrent(torrent_id)
            _after()
            ui.notify("已排除" if ok else "排除失败（已下载的用『删除文件』）",
                      type="positive" if ok else "warning")
        return h

    def _unexclude(torrent_id):
        def h():
            ok = anime.unexclude_torrent(torrent_id)
            _after()
            ui.notify("已放回待下" if ok else "取消失败", type="positive" if ok else "warning")
        return h

    body()
