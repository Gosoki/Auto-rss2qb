"""主页 `/`：番剧列表[按季度] / 待确认 / 待识别 / 已忽略 / 新入库 / 仪表盘。

刷新面板定义在 page 函数内部（每个浏览器连接各自一份），避免模块级
单例 refreshable 在多页面/多客户端下互相串。
"""
from urllib.parse import quote

from nicegui import ui

from core import anime, engine
import config
from .layout import (confirm, ep_str, expand_collapse_bar, frame, group_by_quarter,
                     human_size, kpi_cards, live_status, name_of, paginate, parse_bgm_id,
                     platform_badge, qb_disabled_banner, recent_table, season_label,
                     source_options)
from .sources import render_sources


def _state_rank(a):
    """管理页组内排序：追番中(0) < 待确认(1) < 已拒绝(2)——后两者垫底。"""
    if a.rejected:
        return 2
    return 1 if not a.confirmed else 0


_TAB_KEYS = ("overview", "manage", "confirm", "fail", "reject", "sources")


@ui.page("/")
def anime_page(t: str = ""):
    """t 为当前 tab（写在 URL ?t= 里），这样刷新（整页重载）能回到同一 tab、不跳回番剧表。
    默认空串：顶栏导航进来（无 ?t=）时留给下面按 ANIME_DEFAULT_TAB 决定默认停哪个 tab。"""
    with frame("manage"):  # 本页用 tab + 30s 定时刷新，不往顶栏右侧放自定义动作
        manage_page = {"n": 1, "expand": None}  # 番剧表：分页页码 + 一键展开/收起意图（None=按默认）

        def _manage_goto(e):
            manage_page["n"] = int(e.value)
            manage_panel.refresh()

        # ---- 刷新（页面局部，闭包内共享）----
        @ui.refreshable
        def overview_panel():
            ov = anime.overview()
            k = ov["kpi"]
            ps = ov["pending_split"]

            # ── KPI 卡片 ──（『未知集』『失败』可点开看是哪几个、进详情处理）
            # 番维度四卡（粉字）与种子维度四卡（绿字）各自打包，"|" 分组，窄了整组换行成上下布局；数字保持各自语义色
            kpi_cards([("订阅中", k["tracking"], "", None, "pink-400"),
                       ("待识别", k["fail"], "red", lambda: tabs.set_value("fail"), "pink-400"),
                       ("待确认", k["confirm"], "orange", lambda: tabs.set_value("confirm"), "pink-400"),
                       ("已忽略", k["rejected"], "", lambda: tabs.set_value("reject"), "pink-400"),
                       "|",
                       ("已下载", k["done"], "green", None, "green-500"),
                       ("未知集", ps["unknown"], "purple", _open_unknown, "green-500"),
                       ("失败数", ov["status"]["error"], "red", _open_failed, "green-500"),
                       ("种子数", k["torrents"], "", None, "green-500")])

            # ── qB 未启用提醒 ──
            if not ov["config"]["qb"]:
                qb_disabled_banner("qB 未启用：只采集元数据、不实际下载（设置页开启 QB_ENABLED 后生效）")

            # ── 订阅源组 ──
            ui.label("订阅源组").classes("text-sm font-bold mt-3 pl-1")
            with ui.row().classes("gap-2 flex-wrap pl-1"):
                for name, site, policy, priority, enabled in ov["groups"]:
                    pol = "全下" if policy == "auto" else "待确认"
                    tail = "" if enabled else " · 停用"
                    ui.badge(f"{name} · {site} · {pol} · P{priority}{tail}").props(
                        f"color={'blue-grey' if enabled else 'grey'}").classes("text-sm")

            # ── 下载番剧 / 种子来源（左右分开，窄屏自动堆叠）──
            with ui.row().classes("w-full gap-6 flex-wrap mt-3"):
                with ui.column().classes("gap-1 min-w-0").style("flex:1 1 320px"):
                    bqs = ov["by_quarter_state"]
                    with ui.row().classes("items-center gap-3 flex-wrap w-full"):
                        ui.label(f"各季度番剧 · {len(bqs)} 季").classes("text-sm font-bold")
                        with ui.row().classes("items-center gap-3 flex-wrap ml-auto"):  # 图例靠右
                            for _lab, _c in (("订阅", "oklch(70.7% 0.165 254.624)"),        # blue-400
                                             ("审核", "oklch(75% 0.183 55.934)"),            # orange-400
                                             ("忽略", "oklch(70.7% 0.022 261.325)")):        # gray-400
                                with ui.row().classes("items-center gap-1 text-xs text-gray-400"):
                                    ui.element("div").style(
                                        f"width:9px;height:9px;border-radius:2px;background:{_c}")
                                    ui.label(_lab)
                    with ui.column().classes("w-full gap-0").style("max-height:200px;overflow-y:auto"):
                        if not bqs:
                            ui.label("—").classes("text-gray-500 text-sm")
                        for q, sub, rev, ign in bqs:
                            total = sub + rev + ign
                            with ui.row().classes("items-center gap-3 w-full text-sm py-0.5 min-w-0"):
                                ui.label(engine.quarter_label(q)).classes(
                                    "w-36 shrink-0 truncate").tooltip(engine.quarter_label(q))
                                # 满宽 100% 的比例条，按订阅/审核/忽略切三段（0 段跳过）
                                with ui.element("div").classes("grow rounded overflow-hidden flex min-w-0").style(
                                        "height:13px;background:rgba(255,255,255,.08)"):
                                    for _val, _c, _n in ((sub, "oklch(70.7% 0.165 254.624)", "订阅"),    # blue-400
                                                         (rev, "oklch(75% 0.183 55.934)", "审核"),        # orange-400
                                                         (ign, "oklch(70.7% 0.022 261.325)", "忽略")):    # gray-400
                                        if _val and total:
                                            ui.element("div").style(
                                                f"width:{_val / total * 100:.1f}%;height:100%;background:{_c}"
                                            ).tooltip(f"{_n} {_val}")
                                ui.label(f"{total} 部").classes(
                                    "shrink-0 text-gray-400 text-right text-xs").style("min-width:3rem")
                with ui.column().classes("gap-1 min-w-0").style("flex:1 1 320px"):
                    ui.label(f"种子来源 · {len(ov['by_source'])} 个源").classes("text-sm font-bold")
                    if not ov["by_source"]:
                        ui.label("—").classes("text-gray-500 text-sm")
                    else:
                        # 环图：各源的种子占比（悬停切片→环心显示源名+数量）。ov['by_source']=[(源,种子数,已下)…]
                        ui.echart({
                            # Tailwind -400 十色（sRGB hex；ECharts canvas 不吃 oklch）
                            "color": ["#50a2ff", "#a684ff", "#00d492", "#ff8904", "#ff6467",
                                      "#00d3f3", "#fb64b6", "#9ae600", "#c27aff", "#00d5be"],
                            "tooltip": {"trigger": "item", "formatter": "{b}<br/>{c} 种子 · {d}%"},
                            "legend": {"type": "scroll", "orient": "vertical", "right": "2%",
                                       "top": "middle", "textStyle": {"color": "#99a1af"}},  # gray-400(灰2)
                            "series": [{
                                "name": "种子来源", "type": "pie",
                                "radius": ["45%", "72%"], "center": ["32%", "50%"],
                                "avoidLabelOverlap": False,
                                "itemStyle": {"borderColor": "#1a1c22", "borderWidth": 2},
                                "label": {"show": False, "position": "center"},
                                "emphasis": {"label": {"show": True, "color": "#d1d5dc",
                                                       "fontSize": 14, "fontWeight": "bold",
                                                       "formatter": "{b}\n{c}"}},
                                "data": [{"name": src, "value": tot} for src, tot, _ in ov["by_source"]],
                            }],
                        }).classes("w-full").style("height:220px")

            # ── 种子状态 ──
            with ui.row().classes("items-center gap-2 mt-3 pl-1 flex-wrap"):
                ui.label("种子状态").classes("text-sm font-bold")
                ui.label("各状态种子计数（含 qB 实时态）").classes("text-xs text-gray-400")
                _dla = ui.button("补下全部", icon="download", on_click=_download_all).props(
                    "outline color=primary size=sm").style("font-size:12px")
                _dla.set_enabled(config.QB_ENABLED)
                _dla.tooltip("订阅中所有待下集立即下" if config.QB_ENABLED
                             else "qB 未启用，去设置页开启后可下载")
            # 待下拆 将下载/备用/待确认/未知（库态『下载中』恒≈0、与 qB 实时态重复不单列）；失败、种子总数一并列出，与 KPI 卡呼应
            chips = [
                ("已下载", ov["status"]["downloaded"], "green", None),
                ("将下载", ps["will"], "blue", "已确认番·本集首选（含特别篇），会自动下"),
                ("备用项", ps["backup"], "blue-grey", "同集已有更优版本，不会自动下"),
                ("待确认", ps["unconfirmed"], "orange", "番还没确认，去『待确认』页点确认才会下"),
                ("未知集", ps["unknown"], "purple", "批量/未知集，后台不自动下，需在详情页手动下"),
                ("跳过数", ov["status"]["skipped"], "blue-grey",
                 "同集已有别版在下/已下被去重，或忽略番的积压；换源兜底时可能被复活"),
                ("失败数", ov["status"]["error"], "red", "下载出错的种子"),
                ("种子数", k["torrents"], "grey", "全部种子数（各状态之和）"),
            ]
            with ui.row().classes("gap-2 flex-wrap pl-1 items-center"):
                for label, val, color, tip in chips:
                    b = ui.badge(f"{label} {val}").props(f"color={color}").classes("text-sm")
                    if tip:
                        b.tooltip(tip)
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
            with ui.row().classes("items-center gap-2 mt-3 pl-1 flex-wrap"):
                ui.label("采集状态").classes("text-sm font-bold")
                ui.label("后台采集与 bgm 识别的运行情况").classes("text-xs text-gray-400")
                with ui.button("重新识别", icon="sync").props("outline color=primary size=sm").style("font-size:12px"):
                    with ui.menu():
                        ui.menu_item("识别当季", on_click=_reident(1))
                        ui.menu_item("识别半年（近 2 季）", on_click=_reident(2))
                        ui.menu_item("识别 1 年（近 4 季）", on_click=_reident(4))
                        ui.menu_item("识别全部", on_click=_reident(None))
            with ui.row().classes("gap-2 flex-wrap pl-1"):
                ui.badge("采集开启" if ov["config"]["poll_on"] else "采集暂停").props(
                    f"color={'green' if ov['config']['poll_on'] else 'red'}").classes("text-sm").tooltip(
                    "后台是否在抓取（在设置页『采集』开关切换）")
                ui.badge(f"识别番数 {enr}/{tot}").props("color=blue").classes("text-sm").tooltip(
                    "已匹配到 bgm 的番 / 全部")
                ui.badge(f"轮询间隔 {ov['config']['poll']}s").props("color=blue-grey").classes("text-sm").tooltip(
                    "后台每隔这么久抓一次源（设置页可改）")
                ui.badge(f"缓冲窗口 {ov['config']['grace']}min").props("color=blue-grey").classes("text-sm").tooltip(
                    "一集首次发现后等这么久再下，给更高优先级的源补齐（设置页可改）")

        @ui.refreshable
        def confirm_panel():
            pend = anime.pending_confirm()
            if not pend:
                ui.label("没有待确认的番。（『待确认』策略的源组发现的番会出现在这里）").classes("text-gray-400 p-4")
                return
            for i, (q, items) in enumerate(group_by_quarter(pend)):
                with ui.expansion(f"{engine.quarter_label(q)}   ·   {len(items)} 部", value=(i == 0)).classes("w-full"):
                    for a in items:
                        srcs = anime.anime_sources(a.id)
                        with ui.card().classes("w-full"):
                            with ui.row().classes("items-center gap-3 flex-wrap"):
                                ui.badge("待确认").props("color=orange")
                                ui.label(name_of(a)).classes(
                                    "cursor-pointer text-blue-400 hover:underline").on(
                                    "click", lambda aid=a.id: open_detail(aid))
                                sl = season_label(a)
                                if sl:
                                    ui.badge(sl).props("color=purple")
                                platform_badge(a)   # bgm 判定非 TV（剧场版/OVA…）时紫标提示
                                ui.label("来源: " + (" · ".join(srcs) or "—")).classes("text-xs text-gray-400")
                            with ui.row().classes("items-stretch gap-3 flex-wrap"):
                                sel = ui.select(source_options(srcs, "从哪下：按优先级"),
                                                value="").props("dense outlined").classes("min-w-48")
                                ui.button("确认下载", on_click=_confirm(a.id, sel)).props("color=primary unelevated")
                                ui.button("忽略", on_click=_reject(a.id)).props("flat color=grey")

        @ui.refreshable
        def reject_panel():
            rej = anime.list_rejected_anime()
            if not rej:
                ui.label("没有已忽略的番。（待确认/详情页点『忽略』会进这里，可随时恢复）").classes("text-gray-400 p-4")
                return
            for i, (q, items) in enumerate(group_by_quarter(rej)):
                with ui.expansion(f"{engine.quarter_label(q)}   ·   {len(items)} 部", value=(i == 0)).classes("w-full"):
                    for a in items:
                        with ui.card().classes("w-full"):
                            with ui.row().classes("items-center gap-3 flex-wrap"):
                                ui.badge("已忽略").props("color=grey")
                                ui.label(name_of(a)).classes(
                                    "cursor-pointer text-blue-400 hover:underline").on(
                                    "click", lambda aid=a.id: open_detail(aid))
                                sl = season_label(a)
                                if sl:
                                    ui.badge(sl).props("color=purple")
                                ui.label("来源: " + (" · ".join(anime.anime_sources(a.id)) or "—")).classes(
                                    "text-xs text-gray-400")
                            with ui.row().classes("items-stretch gap-3 flex-wrap"):
                                ui.button("恢复订阅", icon="undo", on_click=_restore(a.id)).props(
                                    "color=primary unelevated")
                                nf = anime.downloaded_count(a.id)
                                if nf:  # 只有确实下过文件才给『删除文件』
                                    ui.button("删除文件", icon="delete_forever",
                                              on_click=_del_files(a.id, name_of(a), nf)).props(
                                        "flat color=negative").tooltip(
                                        "连同 qB 里的硬盘文件一起删（不可撤销）")

        @ui.refreshable
        def fail_panel():
            items = anime.list_unmatched_anime()
            if not items:
                ui.label("没有待识别的番。（bgm 没自动匹配上的番会出现在这里，可重试或手动绑定）").classes(
                    "text-gray-400 p-4")
                return
            ui.label("这些番没自动匹配到 bgm：缺规范名 / 日语文件夹名 / 季度。"
                     "可『重试识别』，或粘贴 bgm 链接/ID『绑定』，实在没有就『忽略』。").classes(
                "text-xs text-gray-400 p-2")
            for a in items:
                srcs = anime.anime_sources(a.id)
                with ui.card().classes("w-full"):
                    with ui.row().classes("items-center gap-3 flex-wrap"):
                        ui.badge("未匹配").props("color=red")
                        ui.label(name_of(a)).classes(
                            "cursor-pointer text-blue-400 hover:underline").on(
                            "click", lambda aid=a.id: open_detail(aid))
                        sl = season_label(a)
                        if sl:
                            ui.badge(sl).props("color=purple")
                        ui.badge("待确认" if not a.confirmed else "自动").props(
                            f"color={'orange' if not a.confirmed else 'blue-grey'}")
                        ui.label("来源: " + (" · ".join(srcs) or "—")).classes("text-xs text-gray-400")
                        ui.link("去 bgm 搜", f"https://bgm.tv/subject_search/{quote(a.title)}?cat=2").props(
                            "target=_blank").classes("text-xs")
                    with ui.row().classes("items-stretch gap-3 flex-wrap"):
                        inp = ui.input(
                            placeholder="bgm 链接或 ID，如 bgm.tv/subject/464376 或 464376").props(
                            "dense outlined").classes("min-w-96")
                        ui.button("绑定", icon="link", on_click=_bind(a.id, inp)).props("color=primary unelevated")
                        ui.button("重试识别", icon="refresh", on_click=_refail(a.id)).props("flat color=grey")
                        ui.button("忽略", on_click=_reject(a.id)).props("flat color=grey")

        @ui.refreshable
        def inflight_panel():
            rows = anime.inflight_anime_rows()
            ui.label(f"正在下载（{len(rows)}）").classes("text-sm font-bold mt-4 pl-1")
            with ui.card().classes("w-full"):
                if not config.QB_SYNC_STATUS:
                    ui.label("已关闭 qB 状态跟踪：发送即视为『已下』，不跟踪下载进度（设置页可重新开启）。").classes(
                        "text-gray-500 text-sm")
                    return
                if not rows:
                    ui.label("暂无正在下载的种子").classes("text-gray-500 text-sm")
                    return
                with ui.column().classes("w-full gap-0"):
                    for r in rows:
                        text, color = live_status(r["status"], r["qb_state"], r["qb_progress"],
                                                  r["qb_synced_at"], r["qb_dlspeed"])
                        with ui.row().classes("items-center gap-3 w-full text-sm py-1").style(
                                "border-bottom:1px solid rgba(255,255,255,.08)"):
                            ui.label(f'{r["name"]}  第{ep_str(r["episode"])}集').classes(
                                "grow break-all")
                            ui.badge(text).props(f"color={color}")

        @ui.refreshable
        def recent_panel():
            ui.label("新入库（最近 50 条种子）").classes("text-sm font-bold mt-4 pl-1")
            raw = anime.recent_anime_rows(50)
            # 待下/失败行标『将下载/备用』：只给『已确认』的番批量算 download_plan（每番一查）；
            # 未确认（待确认）的番不算、显示『待确认』——它要点确认才会下，标将下载是假的。
            pend_aids = {r["anime_id"] for r in raw
                         if r["status"] in ("pending", "error") and r["anime_id"]}
            confirmed = anime.confirmed_anime_ids(pend_aids)
            plan_ids = anime.download_plan_for_ids(confirmed)   # 批量一次算完，避免每番一查(N+1)
            rows = []
            for r in raw:
                if r["status"] in ("pending", "error"):
                    conf = r["anime_id"] in confirmed
                    in_plan = r["id"] in plan_ids
                else:
                    conf, in_plan = True, None
                text, color = live_status(r["status"], r["qb_state"], r["qb_progress"],
                                          r["qb_synced_at"], r["qb_dlspeed"], in_plan, conf)
                rows.append({
                    "id": r["id"],
                    "detail_id": r["anime_id"],
                    "time": r["time"],
                    "name": f'{r["name"]}  第{ep_str(r["episode"])}集',
                    "src": r["source"],
                    "raw": r["raw"] or "—",
                    "status": text,
                    "status_color": color,
                })
            recent_table(rows, "番剧",
                         on_row_click=lambda row: row.get("detail_id") and open_detail(row["detail_id"]))

        @ui.refreshable
        def manage_panel():
            # 当季 / 上季小结（数字大、标签小；零值待确认项变灰）
            with ui.row().classes("w-full gap-3 flex-wrap mb-3 items-stretch"):
                for b in anime.quarter_brief():
                    stats = [
                        (b["shows"], "订阅中", "text-blue-400"),
                        (b["confirm"], "待确认", "text-orange-400" if b["confirm"] else "text-gray-500"),
                        (b["fail"], "待识别", "text-red-400" if b["fail"] else "text-gray-500"),
                        (b["ignored"], "已忽略", "text-gray-400" if b["ignored"] else "text-gray-500"),
                    ]
                    with ui.card().classes("gap-2 py-3").style("flex:1 1 300px"):
                        with ui.row().classes("items-center gap-2"):
                            ui.badge(b["tag"]).props(
                                f"color={'primary' if b['tag'] == '当季' else 'blue-grey'}").classes("text-sm")
                            ui.label(engine.quarter_label(b["key"])).classes("font-bold text-base")
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
                    return config.ANIME_SHOW_REJECTED
                if not a.confirmed:
                    return config.ANIME_SHOW_PENDING
                return True
            animes = [a for a in anime.list_all_anime() if _visible(a)]
            if not animes:
                ui.label("（还没有番剧，等采集）").classes("text-gray-400 p-4")
                return
            animes.sort(key=lambda a: (_state_rank(a), a.id))  # 追番中在上，待确认、已拒绝垫底
            src_map = anime.source_map()
            yrs = max(1, config.ANIME_PAGE_YEARS)  # 防 0（每页 0 季会除零）
            groups, total_pages, page = paginate(group_by_quarter(animes), manage_page["n"], yrs * 4)
            manage_page["n"] = page
            with ui.row().classes("items-center gap-3 pl-1 pb-1 flex-wrap"):
                expand_collapse_bar(manage_page, manage_panel.refresh)
                if total_pages > 1:
                    ui.pagination(1, total_pages, direction_links=True, value=page,
                                  on_change=_manage_goto).props("size=sm")
                    ui.label(f"共 {total_pages} 页 · 每页 {yrs} 年").classes("text-xs text-gray-500")
            exp = manage_page["expand"]  # None=各季按默认(仅最新季开)；True/False=一键全展开/收起(跨页一致)
            for i, (q, items) in enumerate(groups):
                _open = exp if exp is not None else i == 0
                _exp = ui.expansion(f"{engine.quarter_label(q)}   ·   {len(items)} 部",
                                    value=_open).classes("w-full")
                # 懒加载：折叠的季度先不建行（省首建成本）；首次展开时才建，之后不再重建
                _fl = {"built": False}

                def _fill(qi=items, box=_exp, fl=_fl):
                    if fl["built"]:
                        return
                    fl["built"] = True
                    with box:
                        for a in qi:
                            _anime_row(a, src_map.get(a.id))

                if _open:
                    _fill()
                else:
                    _exp.on_value_change(lambda e, f=_fill: f() if e.value else None)

        def refresh_dynamic():
            # 用户操作后的全动态刷新：含『待确认/待识别』，好让确认/绑定/忽略后的番立即从对应列表流转。
            overview_panel.refresh()
            inflight_panel.refresh()
            confirm_panel.refresh()
            reject_panel.refresh()
            fail_panel.refresh()
            recent_panel.refresh()

        def _refresh_live(tab):
            # 刷新某 tab 的只读实时区：overview 的实时状态/新入库、reject 列表。其余 tab 无实时区，不动。
            # 不含 confirm/fail——它们有用户正在输入的绑定框/源下拉，重建会清空半途输入。
            if tab == "overview":
                overview_panel.refresh()
                inflight_panel.refresh()
                recent_panel.refresh()
            elif tab == "reject":
                reject_panel.refresh()

        def refresh_timer():
            # 30s 定时器：只刷『当前可见 tab』的实时区，隐藏 tab 不动（省 CPU、消除每 30s 的周期性卡顿）。
            _refresh_live(tabs.value)

        def refresh_all():
            refresh_dynamic()
            manage_panel.refresh()

        # 详情悬浮框（复用一个 dialog，点开时清空重建，避免累积 + 不跳页丢滚动位置）
        detail_dlg = ui.dialog()

        def open_detail(anime_id):
            from .anime_detail import render_anime_detail
            detail_dlg.clear()
            with detail_dlg, ui.card().classes("w-full").style("max-width:860px"):
                render_anime_detail(anime_id, refresh_outer=refresh_all, on_close=detail_dlg.close)
            detail_dlg.open()

        # KPI 卡点开：列出对应种子（未知集/失败），点番名进详情页处理。详情弹窗叠在本弹窗之上、
        # 不关掉它——关了详情仍回到这层列表，一层一层来。
        list_dlg = ui.dialog()

        def _open_torrent_list(title, desc, fetch):
            list_dlg.clear()
            rows = fetch()
            with list_dlg, ui.card().classes("w-full").style("max-width:720px"):
                ui.label(f"{title} · {len(rows)}").classes("text-base font-bold")
                if desc:
                    ui.label(desc).classes("text-xs text-gray-400")
                if not rows:
                    ui.label("（空）").classes("text-gray-500 p-2")
                for r in rows:
                    with ui.column().classes("gap-0 w-full py-1").style(
                            "border-bottom:1px solid rgba(255,255,255,.08)"):
                        ui.label(r["name"]).classes(
                            "text-sm text-blue-400 cursor-pointer hover:underline").on(
                            "click", lambda aid=r["anime_id"]: open_detail(aid))  # 不关本弹窗，详情叠上面
                        ui.label(r["raw"] or "—").classes("text-xs text-gray-500 break-all")
                ui.button("关闭", on_click=list_dlg.close).props("flat")
            list_dlg.open()

        def _open_unknown():
            _open_torrent_list(
                "未知集", "批量打包 / 集号没解析出来的种子，后台不自动下。点番名进详情页处理（下载/忽略）。",
                anime.unknown_episode_rows)

        def _open_failed():
            _open_torrent_list(
                "失败", "下载失败过的种子（取种/发送失败）。点番名进详情页补下重试 / 忽略。",
                anime.failed_rows)

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
            async def h():
                if not await confirm(f"删除《{name}》的 {cnt} 个已下文件？",
                                     "通过 qB 连同硬盘文件一起删除，不可撤销。",
                                     ok_label="删除文件", ok_icon="delete_forever"):
                    return
                n = await anime.delete_anime_files(anime_id)
                refresh_all()
                ui.notify(f"已删除 {n} 个文件" if n else "没删成（qB 未连上或已无文件）",
                          type="positive" if n else "warning")
            return h

        def _bind(anime_id, inp):
            async def h():
                bid = parse_bgm_id(inp.value or "")
                if bid is None:
                    ui.notify("请粘贴 bgm 链接或数字 ID", type="warning")
                    return
                ok = await anime.bind_anime_bgm(anime_id, bid)
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
            refresh_all()   # 含 manage_panel，好让季度小结的已下/待下计数也即时更新（展开/页码已持久化，不打乱视图）
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
                if sources:   # 源徽标放最前：多源(>1)蓝 / 单源(==1)灰
                    n = len(sources)
                    _lab, _c = (f"多源 {n}", "blue") if n > 1 else (f"单源 {n}", "blue-grey")
                    ui.badge(_lab).props(f"color={_c}").tooltip("来源: " + " · ".join(sources))
                if a.rejected:                       # 状态徽标（互斥，最多一个）
                    ui.badge("已忽略").props("color=grey")
                elif not a.confirmed:
                    ui.badge("待确认").props("color=orange")
                color = "text-gray-500 line-through" if a.rejected else "text-blue-400"
                ui.label(name_of(a)).classes(
                    f"cursor-pointer {color} hover:underline").on(
                    "click", lambda aid=a.id: open_detail(aid))
                sl = season_label(a)
                if sl:
                    ui.badge(sl).props("color=purple")
                platform_badge(a)   # bgm 判定非 TV（剧场版/OVA…）时紫标提示

        # ---- 页面布局 ----
        with ui.tabs().classes("w-full") as tabs:
            ui.tab("overview", "仪表盘", "dashboard")
            ui.tab("manage", "番剧表", "movie")
            ui.tab("confirm", "待确认", "help_outline")
            ui.tab("fail", "待识别", "sync_problem")
            ui.tab("reject", "已忽略", "block")
            ui.tab("sources", "订阅源", "rss_feed")
        # 懒加载：首屏只构建当前 tab 的内容；切到别的 tab 首次才建（6 个面板 → 1 个，砍首屏构建/推送）。
        # 面板都是 @ui.refreshable，未构建过的 .refresh() 是安全 no-op，所以刷新逻辑无需改动。
        _builders = {
            "overview": lambda: (overview_panel(), inflight_panel(), recent_panel()),
            "manage": manage_panel, "confirm": confirm_panel,
            "fail": fail_panel, "reject": reject_panel, "sources": render_sources,
        }
        _slots: dict = {}
        _built: set = set()

        def _build_tab(key):
            if key in _built or key not in _slots:
                return
            _built.add(key)
            with _slots[key]:
                _builders[key]()

        start = t if t in _TAB_KEYS else (
            config.ANIME_DEFAULT_TAB if config.ANIME_DEFAULT_TAB in _TAB_KEYS else "manage")
        with ui.tab_panels(tabs, value=start).props("keep-alive transition-prev=fade transition-next=fade transition-duration=120").classes("w-full"):
            for _k in _TAB_KEYS:
                _slots[_k] = ui.tab_panel(_k)   # 空面板本身作容器，懒建时直接填入（不套内层 div，保持 tab-panel 原生行距）

        def _on_tab(e):   # 切 tab：写 URL(不重载，刷新后仍停在此 tab) + 首次构建；已建过的切回来刷新实时区显示最新
            ui.run_javascript(f"history.replaceState(null,'','?t='+encodeURIComponent('{e.value}'))")
            already = e.value in _built
            _build_tab(e.value)
            if already:
                _refresh_live(e.value)
        tabs.on_value_change(_on_tab)
        _build_tab(start)   # 构建首屏 tab

        ui.timer(30.0, refresh_timer)  # 只刷当前可见 tab 的实时区（见 refresh_timer）
