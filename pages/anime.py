"""主页 `/`：番剧列表[按季度] / 待确认 / 待识别 / 已忽略 / 新入库 / 仪表盘。

刷新面板定义在 page 函数内部（每个浏览器连接各自一份），避免模块级
单例 refreshable 在多页面/多客户端下互相串。
"""
from urllib.parse import quote

from nicegui import ui

from db.models import AnimeTorrent
from core import anime, engine
import config
from .anime_detail import maybe_relocate_anime, render_anime_detail
from .layout import (busy_action, confirm, require_bind_confirm, ep_str, expand_collapse_bar,
                     frame, group_by_quarter,
                     human_size, kpi_cards, live_status, name_of, paginate, parse_bgm_id,
                     platform_badge, recent_table, season_label, source_options,
                     warn_banner)
from .sources import render_sources


def _state_rank(a):
    """管理页组内排序：追番中(0) < 待确认(1) < 已拒绝(2)——后两者垫底。"""
    if a.rejected:
        return 2
    return 1 if not a.confirmed else 0


# 本页的 tab：{键: 显示名}。设置页『默认标签页』下拉直接用它，页面侧的合法键集合由它派生——
# 早先两处各记一份（这里一个元组、settings 一个字典），加/改 tab 时漏改一处就静默不一致。
# 图标一并放进来：否则"只留一份"只兑现了【键】，显示名仍要在 ui.tab 里再抄一遍（改了也不报错）。
_TABS_DEF = (("overview", "仪表盘", "dashboard"), ("manage", "番剧表", "movie"),
             ("confirm", "待确认", "help_outline"), ("fail", "待识别", "sync_problem"),
             ("reject", "已忽略", "block"), ("sources", "订阅源", "rss_feed"))
ANIME_TAB_LABELS = {k: name for k, name, _ in _TABS_DEF}   # 设置页『默认标签页』下拉直接用它
_TAB_KEYS = tuple(ANIME_TAB_LABELS)


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
        # 仪表盘的 head/charts/tail 是三个独立 refreshable，但用的是同一份 overview() 数据。
        # 各自调一次等于把它算三遍——overview() 要跑 15 条查询 + pending_breakdown，
        # 后者在大库时是整个仪表盘的开销大头（实测 19k 种子：overview 182ms，其中 157ms 是它）。
        # 这里让三者共用【本轮】的一份快照；每轮刷新开头清空，出了本轮不复用（不会读到陈旧值）。
        _ov_snap: dict = {}

        def _overview():
            if "v" not in _ov_snap:
                _ov_snap["v"] = anime.overview()
            return _ov_snap["v"]

        @ui.refreshable
        def overview_head():   # KPI + 订阅源组（计数，随定时器刷新，无交互态可丢）
            ov = _overview()
            k = ov["kpi"]
            ps = ov["pending_split"]

            # ── KPI 卡片 ──（『未知集』『失败』可点开看是哪几个、进详情处理）
            # 番维度四卡（粉字）与种子维度四卡（绿字）各自打包，"|" 分组，窄了整组换行成上下布局；数字保持各自语义色
            kpi_cards([("订阅中", k["tracking"], "", lambda: tabs.set_value("manage")),
                       ("待识别", k["fail"], "red", lambda: tabs.set_value("fail")),
                       ("待确认", k["confirm"], "orange", lambda: tabs.set_value("confirm")),
                       ("已忽略", k["rejected"], "", lambda: tabs.set_value("reject")),
                       "|",
                       ("已交付", k["done"], "green", None),
                       ("特别篇/未知集", ps["unknown"], "purple", _open_unknown),
                       # 名字与下方 chip『失败数』(仅 error) 区分开：这张卡是"需要处理的总数"，
                       # 含长期停滞，和点开后的 failed_rows()/弹窗标题『失败 / 异常』一致
                       ("失败/异常", ov["status"]["error"] + ov["status"]["stalled"], "red",
                        _open_failed),
                       ("种子数", k["torrents"], "", None)])

            # ── qB 未启用提醒 ──
            if not ov["config"]["qb"]:
                warn_banner("qB 未启用：只采集元数据、不实际下载（设置页开启 QB_ENABLED 后生效）")

            # ── 疑似同一部番被拆成两条 ──
            # 【只提示、不自动合】绑到不同 bgm subject 的两条，程序判不出哪条是对的；
            # 自动合并会删行且不可逆（剧场版那边已经为此付过一次学费）。
            # 而它又必须被看见：真库里那一对的其中一条被判了超期忽略，
            # 挂在它下面的 5 集【永远不会被下】，而现有的身份守卫只认 bangumi_id 相同、看不到它。
            for _d in anime.suspect_duplicate_anime():
                _tail = ("；其中『%s』已被忽略，它下面的集不会下载"
                         % (_d["a_name"] if _d["a_rejected"] else _d["b_name"])
                         if (_d["a_rejected"] or _d["b_rejected"]) else "")
                warn_banner(
                    f"『{_d['a_name']}』(#{_d['a']}，bgm {_d['a_bgm']}) 与 "
                    f"『{_d['b_name']}』(#{_d['b']}，bgm {_d['b_bgm']}) 共用番名 "
                    f"「{'、'.join(_d['shared'])}」，多半是同一部番被拆成了两条{_tail}。"
                    "去详情页核对 bgm 绑定：把错的那条改绑成对的 bgm，身份守卫会自动把它们合并。")

            # ── 订阅源组 ──
            ui.label("订阅源组").classes("text-sm font-bold mt-3 pl-1")
            with ui.row().classes("gap-2 flex-wrap pl-1"):
                for name, site, policy, priority, enabled in ov["groups"]:
                    pol = "自动下载" if policy == "auto" else "人工审核"
                    tail = "" if enabled else " · 停用"
                    ui.badge(f"{name} · {site} · {pol} · P{priority}{tail}").props(
                        f"color={'blue' if enabled else 'grey'}")   # 启用=蓝，停用=灰

        # 环图图例选择的持久化：用户点图例排除某字幕组后存下选择态，重建时塞回 legend.selected，
        # 任何刷新路径（定时器/用户操作/重识别）重建环图都不会把排除的源刷回来。
        _legend_sel: dict = {}

        def _remember_legend(e):
            a = e.args[0] if isinstance(e.args, list) and e.args else e.args
            if not isinstance(a, dict):
                return
            if isinstance(a.get("selected"), dict):
                sel = a["selected"]                                  # {selected:{…}} 或全量事件对象
            elif a and all(isinstance(v, bool) for v in a.values()):
                sel = a                                              # 直接就是 {源名: bool}
            else:
                return
            _legend_sel.clear()
            _legend_sel.update(sel)
            # 关键：把最新选择写进当前环图元素的 props 并推给前端。定时器每 30s 刷 head/tail，
            # 前端据此重挂载环图时读的是元素 props——props 不更新就会用建图时的旧 selected 把排除态刷回来。
            el = e.sender
            el.options.setdefault("legend", {})["selected"] = dict(_legend_sel)
            el.update()

        @ui.refreshable
        def charts_panel():   # 图表单独一个 refreshable：不被 30s 定时器刷，避免环图重建丢交互态（悬停/图例翻页）
            ov = _overview()
            # ── 下载番剧 / 种子来源（左右分开，窄屏自动堆叠）──
            with ui.row().classes("w-full gap-6 flex-wrap mt-3 pl-1"):   # pl-1 跟其他区块(订阅源组/种子状态)左对齐，别突出去
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
                            _ql = engine.quarter_label(q)   # 同值算一次，label 与 tooltip 复用
                            with ui.row().classes("items-center gap-3 w-full text-sm py-0.5 min-w-0"):
                                ui.label(_ql).classes("w-36 shrink-0 truncate").tooltip(_ql)
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
                        # 环图：各源的种子占比（悬停切片→环心显示源名+数量）。ov['by_source']=[(源,种子数)…]
                        ui.echart({
                            # Tailwind -400 十色（sRGB hex；ECharts canvas 不吃 oklch）
                            "color": ["#50a2ff", "#a684ff", "#00d492", "#ff8904", "#ff6467",
                                      "#00d3f3", "#fb64b6", "#9ae600", "#c27aff", "#00d5be"],
                            "tooltip": {"trigger": "item", "formatter": "{b}<br/>{c} 种子 · {d}%"},
                            "legend": {"type": "scroll", "orient": "vertical", "right": "2%",
                                       # 组名可以很长（"豌豆字幕组&风之圣殿字幕组&LoliHouse"），不截就把环图挤扁。
                                       # 108px ≈ 8 个汉字 + 省略号（图例字号默认 12）。按【像素】截而不是按字数：
                                       # 中英混排时各行收在同一竖线上，比"都留 8 个字"齐整。全名仍在——悬停切片，环心会显示。
                                       "top": "middle", "textStyle": {"color": "#99a1af",  # gray-400(灰2)
                                                                      "width": 108,
                                                                      "overflow": "truncate",
                                                                      "ellipsis": "…"},
                                       "selected": dict(_legend_sel)},  # 回填用户的图例排除态
                            "series": [{
                                "name": "种子来源", "type": "pie",
                                "radius": ["45%", "72%"], "center": ["32%", "50%"],
                                "avoidLabelOverlap": False,
                                "itemStyle": {"borderColor": "#1a1c22", "borderWidth": 2},
                                "label": {"show": False, "position": "center"},
                                "emphasis": {"label": {"show": True, "color": "#d1d5dc",
                                                       "fontSize": 14, "fontWeight": "bold",
                                                       "formatter": "{b}\n{c}"}},
                                "data": [{"name": src, "value": tot} for src, tot in ov["by_source"]],
                            }],
                        }).classes("w-full").style("height:220px").on(
                            "chart:legendselectchanged", _remember_legend, args=["selected"])

        @ui.refreshable
        def overview_tail():   # 种子状态(含 qB 实时徽标) + 采集状态：随定时器刷新，qB 进度要 30s 更新
            ov = _overview()
            k = ov["kpi"]
            ps = ov["pending_split"]
            # ── 种子状态 ──
            with ui.row().classes("items-center gap-2 mt-3 pl-1 flex-wrap"):
                ui.label("种子状态").classes("text-sm font-bold")
                ui.label("各状态种子计数").classes("text-xs text-gray-400")
                _dla = ui.button("补下全部", icon="download", on_click=_download_all).props(
                    "outline color=primary").classes("btn-sm")
                _dla.set_enabled(config.QB_ENABLED)
                _dla.tooltip("订阅中所有待下集立即下" if config.QB_ENABLED
                             else "qB 未启用，去设置页开启后可下载")
            # 待下拆 将下载/备用/待确认/未知（库态『下载中』恒≈0、与 qB 实时态重复不单列）；失败、种子总数一并列出，与 KPI 卡呼应
            chips = [
                # sent = 已发给 qB（含仍在下的）；真正下完的数在下方 qB 实时区的『已完成』
                ("已交付", ov["status"]["sent"], "green",
                 "已发送给 qB 的种子数（含仍在下载的）。真正下完的看下方『已完成』"),
                ("将下载", ps["will"], "blue", "已确认番·本集首选，会自动下（特别篇/未知集不在其列，需手动下）"),
                ("备用项", ps["backup"], "blue-grey", "同集已有『将下载/已下』的更优版本，不会自动下"),
                ("待确认", ps["unconfirmed"], "orange", "番还没确认，去『待确认』页点确认才会下"),
                *([("已完结", ps["finished"], "teal",
                    "番已判定完结、且设置里开了『判完结后停止自动下新集』，所以这些不会自动下。"
                    "若那部番其实还没完（bgm 的总集数少记了），去详情页点『继续订阅』")]
                  if ps.get("finished") else []),
                ("特别篇/未知集", ps["unknown"], "purple",
                 "特别篇·OVA 与批量/未知集：后台一律不自动下，需在详情页对准那一条点『下载』"),
                ("跳过数", ov["status"]["skipped"], "blue-grey",
                 "同集已有别版在下/已下被去重，或忽略番的积压；换源兜底时可能被复活"),
                ("失败数", ov["status"]["error"], "red", "下载出错的种子"),
            ]
            # 少见状态只在非零时露出：保证各 chip 之和 == 种子数（否则『各状态之和』就是句假话），
            # 又不让全新库挂一排 0。库态『下载中』恒≈0（交付即置 sent），同理只在非零时显示。
            for _lb, _key, _cl, _tip in (
                ("下载中", "downloading", "blue", "已挑中正在交付给 qB 的瞬时态（通常一闪而过）"),
                ("停滞", "stalled", "deep-orange", "已交付但长期零推进，脱离轮询、等人工处理"),
                ("已删除", "deleted", "grey", "人工删过的种子（同集来新 hash 仍会照常下）"),
                ("已排除", "excluded", "grey", "人工排除的种子（不再参与自动下载）"),
            ):
                if ov["status"].get(_key):
                    chips.append((_lb, ov["status"][_key], _cl, _tip))
            chips.append(("种子数", k["torrents"], "grey", "全部种子数（各状态之和）"))
            with ui.row().classes("gap-2 flex-wrap pl-1 items-center"):
                for label, val, color, tip in chips:
                    b = ui.badge(f"{label} {val}").props(f"color={color}")
                    if tip:
                        b.tooltip(tip)
            # ── 下载状态 ──（qB 实时态，独立成区；后台每 QB_SYNC_INTERVAL 秒刷，也可点『立刻刷新』手动拉一次）
            # qB 未启用也照常显示：qb_summary 只查库、不打 qB 接口，此时列的是最后已知进度，只把手动同步按钮禁掉。
            q = ov["qb"]
            _qb_on = ov["config"]["qb"]
            with ui.row().classes("items-center gap-2 mt-3 pl-1 flex-wrap"):
                ui.label("下载状态").classes("text-sm font-bold")
                ui.label("qB 实时下载进度" if _qb_on else "qB 未启用·最后进度").classes(
                    "text-xs text-gray-400")
                _sync = ui.button("立刻刷新", icon="sync", on_click=_qb_sync_now).props(
                    "outline color=primary").classes("btn-sm")
                _sync.set_enabled(_qb_on)
                _sync.tooltip("立即向 qB 拉一次最新进度（不必等后台轮询）" if _qb_on
                              else "qB 未启用，去设置页开启后可同步")
            with ui.row().classes("gap-2 flex-wrap pl-1 items-center"):
                ui.badge(f"已完成 {q['completed']}").props("color=green")
                ui.badge(f"下载中 {q['downloading']}").props("color=blue")
                ui.badge(f"已跟踪 {q['tracked']}").props("color=blue").tooltip(
                    "qB 里正在跟踪的种子数（已交付给 qB 的）")
                if q["dlspeed"]:
                    ui.badge(f"↓ {human_size(q['dlspeed'])}/s").props("color=blue")
                ui.badge(f"完成率 {q['avg_progress'] * 100:.0f}%").props("color=blue-grey").tooltip("已交付种子的平均完成度")

            # ── 采集状态 / 源组 ──
            enr, tot = ov["enriched"]
            with ui.row().classes("items-center gap-2 mt-3 pl-1 flex-wrap"):
                ui.label("采集状态").classes("text-sm font-bold")
                ui.label("后台采集与识别").classes("text-xs text-gray-400")
                # 【字号不在调用点定】原写法是 `size=sm` + `.style("font-size:12px")` 两层叠加：
                # Quasar 的 size 是行内 font-size（sm=10px、md=14px），那句 .style 是在把 sm 掰回 12px。
                # 与徽标那一节同一条原则：字号该由全局定，调用点只选【角色 + 档位】。
                # 【档位跟同类走】上面『补下全部』『立刻刷新』与 /movies 的『立刻刷新』都是
                # 页头行里的 outline 触发器、都是 btn-sm；R18 里我把这一个改成了 14px，
                # 于是同一页同一角色出现两种尺寸——正是这轮统一要根除的形状。四个一律 12px。
                _rb = ui.button("刷新资料", icon="sync").props(
                    "outline color=primary").classes("btn-sm")
                _rb.tooltip("按 bgm 重新拉封面/评分/总集数/放送日等元数据。"
                            "【已经绑好的番只刷资料、不会改绑定】——批量重新匹配在真库上实测"
                            "错误率 4.3%，而已有绑定的正确率是 98%。"
                            "要改某一部的绑定去它的详情页，那里有绑定回显。")
                with _rb:
                    with ui.menu():
                        ui.menu_item("刷新当季", on_click=_reident(1))
                        ui.menu_item("刷新半年（近 2 季）", on_click=_reident(2))
                        ui.menu_item("刷新 1 年（近 4 季）", on_click=_reident(4))
                        ui.menu_item("刷新全部", on_click=_reident(None))
            with ui.row().classes("gap-2 flex-wrap pl-1"):
                ui.badge("采集开启" if ov["config"]["poll_on"] else "采集暂停").props(
                    f"color={'green' if ov['config']['poll_on'] else 'red'}").tooltip(
                    "后台是否在抓取（在设置页『采集』开关切换）")
                ui.badge(f"识别番数 {enr}/{tot}").props("color=blue").tooltip(
                    "已匹配到 bgm 的番 / 全部")
                ui.badge(f"轮询间隔 {ov['config']['poll']}s").props("color=blue-grey").tooltip(
                    "后台每隔这么久抓一次源（设置页可改）")
                ui.badge(f"缓冲窗口 {ov['config']['grace']}min").props("color=blue-grey").tooltip(
                    "一集首次发现后等这么久再下，给更高优先级的源补齐（设置页可改）")

        @ui.refreshable
        def confirm_panel():
            pend = anime.pending_confirm()
            if not pend:
                ui.label("没有待确认的番。（『待确认』策略的源组发现的番会出现在这里）").classes("text-gray-400 p-4")
                return
            smap = anime.source_map()   # 批量取来源，避免逐番查的 N+1
            for i, (q, items) in enumerate(group_by_quarter(pend)):
                with ui.expansion(f"{engine.quarter_label(q)}   ·   {len(items)} 部", value=(i == 0)).classes("w-full"):
                    for a in items:
                        srcs = smap.get(a.id, [])
                        with ui.card().classes("w-full"):
                            with ui.row().classes("items-center gap-3 flex-wrap"):
                                ui.badge("待确认").props("color=orange")
                                ui.label(name_of(a)).classes(
                                    "text-lg cursor-pointer text-blue-400 hover:underline").on(
                                    "click", lambda aid=a.id: open_detail(aid))
                                sl = season_label(a)
                                if sl:
                                    ui.badge(sl).props("color=purple")
                                platform_badge(a)   # bgm 判定非 TV（剧场版/OVA…）时紫标提示
                                ui.label("来源: " + (" · ".join(srcs) or "—")).classes("text-xs text-gray-400")
                            with ui.row().classes("items-stretch gap-3 flex-wrap"):
                                # 预填当前锁定源：这个番可能是【补齐/绑定 bgm 打回来的】、本来就设过锁，
                                # 下拉恒空会让用户点一下『确认下载』就把锁静默解掉（选中值会被写回）。
                                # 与详情页那个【同一个控件】逐字对齐：同样的 source_options、
                                # 同样绑 pref_source，宽度类与占位文案也该一样。
                                # min-w-48 是个裸的硬地板（窄屏不收缩也不满宽），是这轮统一漏下的最后一个。
                                sel = ui.select(source_options(srcs, "下载源：按优先级·多源兜底"),
                                                value=(a.pref_source or "")).classes(
                                    "w-full sm:w-auto sm:min-w-52")
                                ui.button("确认下载", on_click=_approve(a.id, sel)).props("unelevated color=primary")
                                ui.button("忽略", on_click=_reject(a.id)).props("flat color=grey")

        @ui.refreshable
        def reject_panel():
            rej = anime.list_rejected_anime()
            dels = anime.deleted_torrent_rows()      # 已删除种子（本页最底折叠①）
            excls = anime.excluded_torrent_rows()    # 已排除种子（本页最底折叠②）
            if not rej and not dels and not excls:
                ui.label("没有已忽略的番。（待确认/详情页点『忽略』会进这里，可随时恢复）").classes("text-gray-400 p-4")
                return
            smap = anime.source_map()   # 批量取来源，避免逐番 N+1
            dlmap = anime.downloaded_counts([a.id for a in rej])   # 同上：可删文件数也一次取齐
            for i, (q, items) in enumerate(group_by_quarter(rej)):
                with ui.expansion(f"{engine.quarter_label(q)}   ·   {len(items)} 部", value=(i == 0)).classes("w-full"):
                    for a in items:
                        with ui.card().classes("w-full"):
                            with ui.row().classes("items-center gap-3 flex-wrap"):
                                ui.badge("已忽略").props("color=grey")
                                ui.label(name_of(a)).classes(
                                    "text-lg cursor-pointer text-blue-400 hover:underline").on(
                                    "click", lambda aid=a.id: open_detail(aid))
                                sl = season_label(a)
                                if sl:
                                    ui.badge(sl).props("color=purple")
                                ui.label("来源: " + (" · ".join(smap.get(a.id, [])) or "—")).classes(
                                    "text-xs text-gray-400")
                            with ui.row().classes("items-stretch gap-3 flex-wrap"):
                                ui.button("恢复订阅", icon="undo", on_click=_restore(a.id)).props(
                                    "unelevated color=primary")
                                nf = dlmap.get(a.id, 0)
                                if nf:  # 只有确实下过文件才给『删除文件』
                                    ui.button("删除文件", icon="delete_forever",
                                              on_click=_del_files(a.id, name_of(a), nf)).props(
                                        "flat color=negative").tooltip(
                                        "连同 qB 里的硬盘文件一起删（不可撤销）")

            # 已删除的种子：本页最底的独立折叠，可『重新下载』找回（deleted 是终态、不会自动重下）
            if dels:
                with ui.expansion(f"已删除的种子（{len(dels)}）", icon="delete_outline").classes(
                        "w-full mt-2").props("dense"):
                    ui.label("你手动删掉文件的种子（终态，不会自动重下）。需要就点『重新下载』找回。").classes(
                        "text-xs text-gray-500 p-1")
                    for d in dels:
                        with ui.row().classes("items-center gap-3 w-full text-sm py-1").style(
                                "border-bottom:1px solid rgba(255,255,255,.06)"):
                            ui.label(d["name"]).classes(
                                "shrink-0 cursor-pointer text-blue-400 hover:underline").on(
                                "click", lambda aid=d["anime_id"]: open_detail(aid))
                            ui.label(f"第{ep_str(d['episode'])}集").classes("shrink-0 text-gray-400")
                            ui.label(d["raw"]).classes("text-gray-500 text-xs break-all min-w-0 grow")
                            _rb = ui.button("重新下载", icon="download",
                                            on_click=_redownload(d["id"])).props(
                                "flat dense color=primary").classes("btn-sm")
                            _rb.set_enabled(config.QB_ENABLED)
                            _rb.tooltip("强制重新下这条到原目录（找回删掉的文件）" if config.QB_ENABLED
                                        else "qB 未启用，去设置页开启后可下载")

            # 已排除的种子：独立折叠，可『恢复』放回待下（excluded 是主动从待下排除的终态）
            if excls:
                with ui.expansion(f"已排除的种子（{len(excls)}）", icon="block").classes(
                        "w-full mt-2").props("dense"):
                    ui.label("你从待下里主动排除的种子（不自动下、RSS 再遇到也不重收）。点『恢复』放回待下。").classes(
                        "text-xs text-gray-500 p-1")
                    for x in excls:
                        with ui.row().classes("items-center gap-3 w-full text-sm py-1").style(
                                "border-bottom:1px solid rgba(255,255,255,.06)"):
                            ui.label(x["name"]).classes(
                                "shrink-0 cursor-pointer text-blue-400 hover:underline").on(
                                "click", lambda aid=x["anime_id"]: open_detail(aid))
                            ui.label(f"第{ep_str(x['episode'])}集").classes("shrink-0 text-gray-400")
                            ui.label(x["raw"]).classes("text-gray-500 text-xs break-all min-w-0 grow")
                            ui.button("恢复", icon="undo", on_click=_unexclude(x["id"])).props(
                                "flat dense color=primary").classes("btn-sm").tooltip(
                                "放回待下，重新参与下载/去重")

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
            smap = anime.source_map()   # 批量取来源，避免逐番 N+1
            for a in items:
                srcs = smap.get(a.id, [])
                with ui.card().classes("w-full"):
                    with ui.row().classes("items-center gap-3 flex-wrap"):
                        # 全站统一叫『待识别』：tab、KPI 卡、空状态文案、剧场版侧的 tooltip
                        # 都用这个词，只有这里曾写「未匹配」。同一个状态三个叫法会让用户
                        # 以为是三件事（GLOSSARY 里没有把它列为"有意的分歧"）。
                        ui.badge("待识别").props("color=red")
                        ui.label(name_of(a)).classes(
                            "text-lg cursor-pointer text-blue-400 hover:underline").on(
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
                            placeholder="bgm 链接或 ID，如 bgm.tv/subject/464376 或 464376").classes("w-96 min-w-0 max-sm:w-full")   # 桌面 384px；手机满卡宽(min-w-0 让 q-input 真能收缩，否则撑破卡片横向溢出)
                        ui.button("绑定", icon="link", on_click=_bind(a.id, inp)).props("unelevated color=primary")
                        ui.button("重试识别", icon="refresh", on_click=_refail(a.id)).props("flat color=grey")
                        ui.button("忽略", on_click=_reject(a.id)).props("flat color=grey")

        @ui.refreshable
        def inflight_panel():
            rows = anime.inflight_anime_rows()
            _inflight_total = engine.inflight_count(AnimeTorrent)
            ui.label(f"正在下载（{_inflight_total}{'，显示前 %d' % len(rows) if _inflight_total > len(rows) else ''}）").classes("text-sm font-bold mt-4 pl-1")
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
                        # no-wrap + 标题 grow/min-w-0 + 徽标 shrink-0：长标题在自己那一格里换行，
                        # 徽标永远钉在右侧，不会被挤到下一行左边去
                        with ui.row().classes("items-center gap-3 w-full text-sm py-1 no-wrap").style(
                                "border-bottom:1px solid rgba(255,255,255,.08)"):
                            ui.label(f'{r["name"]}  第{ep_str(r["episode"])}集').classes(
                                "grow break-all min-w-0")
                            ui.badge(text).props(f"color={color}").classes("shrink-0")

        @ui.refreshable
        def recent_panel():
            ui.label("新入库（最近 50 条种子）").classes("text-sm font-bold mt-4 pl-1")
            raw = anime.recent_anime_rows(50)
            # 待下/失败行标『将下载/备用』：只给『已确认』的番批量算 download_plan（每番一查）；
            # 未确认（待确认）的番不算、显示『待确认』——它要点确认才会下，标将下载是假的。
            pend_aids = {r["anime_id"] for r in raw
                         if r["status"] in engine.DOWNLOADABLE_STATUSES and r["anime_id"]}
            confirmed = anime.confirmed_anime_ids(pend_aids)
            # 【为什么不下】而不只是【下不下】：三种原因的指引完全不同，
            # 以前一律显示『待确认』并把用户指去那个 tab，而已忽略/已完结的番不在里面。
            off_why = anime.auto_off_reasons(pend_aids)
            # 两种口径【一次算出】：pending 行看『自动会下吗』，error 行看『补下会挑吗』
            # ——后台自动下从不重试 error，用同一个集合会标反。
            # 两者只差"候选含不含 error"，分开调会把番元数据、种子行、"已有一份"的键全部重算一遍。
            plan_ids, backfill_ids = anime.download_plans_for_ids(confirmed)
            rows = []
            for r in raw:
                if r["status"] in engine.DOWNLOADABLE_STATUSES:
                    conf = r["anime_id"] in confirmed
                    in_plan = r["id"] in (backfill_ids if r["status"] == "error" else plan_ids)
                else:
                    conf, in_plan = True, None
                text, color = live_status(r["status"], r["qb_state"], r["qb_progress"],
                                          r["qb_synced_at"], r["qb_dlspeed"], in_plan, conf,
                                          episode=r["episode"],
                                          auto_off=off_why.get(r["anime_id"], ""))
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
                                f"color={'primary' if b['tag'] == '当季' else 'blue-grey'}")
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
            prog_map = anime.episode_progress([a.id for a in animes])   # 一条 SQL，与 source_map 同形
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
                            _anime_row(a, src_map.get(a.id), prog_map.get(a.id))

                if _open:
                    _fill()
                else:
                    _exp.on_value_change(lambda e, f=_fill: f() if e.value else None)

        def _refresh_overview(charts):
            # 刷新仪表盘：head(计数)+tail(种子状态/qB实时)恒刷；charts(图表)仅 charts=True 时刷。
            # 定时器传 charts=False → 不重建环图，保住悬停/图例翻页等交互态。
            _ov_snap.clear()          # 本轮取最新数据，下面三个面板共用这一份
            try:
                overview_head.refresh()
                if charts:
                    charts_panel.refresh()
                overview_tail.refresh()
            finally:
                _ov_snap.clear()      # 出了本轮就作废，避免下次读到陈旧快照

        def refresh_dynamic():
            # 用户操作后的全动态刷新：含『待确认/待识别』，好让确认/绑定/忽略后的番立即从对应列表流转。
            _refresh_overview(charts=True)   # 用户操作不在悬停图表，可连图表一起刷
            inflight_panel.refresh()
            confirm_panel.refresh()
            reject_panel.refresh()
            fail_panel.refresh()
            recent_panel.refresh()

        def _refresh_live(tab):
            # 刷新某 tab 的只读实时区：overview 的实时状态/新入库、reject 列表。其余 tab 无实时区，不动。
            # 不含 confirm/fail——它们有用户正在输入的绑定框/源下拉，重建会清空半途输入。
            if tab == "overview":
                _refresh_overview(charts=False)   # 30s 定时器不碰图表，环图交互态不被打断
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
            detail_dlg.clear()
            with detail_dlg, ui.card().classes("w-full").style("max-width:860px"):
                render_anime_detail(anime_id, refresh_outer=refresh_all, on_close=detail_dlg.close)
            detail_dlg.open()

        # KPI 卡点开：列出对应种子（未知集/失败），点番名进详情页处理。详情弹窗叠在本弹窗之上、
        # 不关掉它——关了详情仍回到这层列表，一层一层来。
        list_dlg = ui.dialog()

        _LIST_PAGE_SIZE = 50   # 弹窗每页条数：挂机久了这两类可能上百条，一次建几百个元素会明显卡

        def _open_torrent_list(title, desc, fetch, page: int = 1):
            """未知集 / 失败异常 的明细弹窗。分页而不是一次全渲染——总数照实显示在标题里。"""
            list_dlg.clear()
            rows = fetch()
            shown, pages, page = paginate(rows, page, _LIST_PAGE_SIZE)
            with list_dlg, ui.card().classes("w-full").style("max-width:720px"):
                ui.label(f"{title} · {len(rows)}").classes("text-base font-bold")
                if desc:
                    ui.label(desc).classes("text-xs text-gray-400")
                if not rows:
                    ui.label("（空）").classes("text-gray-500 p-2")
                for r in shown:
                    with ui.column().classes("gap-0 w-full py-1").style(
                            "border-bottom:1px solid rgba(255,255,255,.08)"):
                        ui.label(r["name"]).classes(
                            "text-sm text-blue-400 cursor-pointer hover:underline").on(
                            "click", lambda aid=r["anime_id"]: open_detail(aid))  # 不关本弹窗，详情叠上面
                        ui.label(r["raw"] or "—").classes("text-xs text-gray-500 break-all")
                with ui.row().classes("items-center gap-3 w-full mt-1"):
                    if pages > 1:
                        # 翻页＝按新页码重建本弹窗（fetch 会重查，数据变了页码也会被 paginate 夹回合法范围）
                        ui.pagination(1, pages, value=page, direction_links=True,
                                      on_change=lambda e: _open_torrent_list(
                                          title, desc, fetch, int(e.value))).props("dense")
                        ui.label(f"第 {page}/{pages} 页").classes("text-xs text-gray-500")
                    ui.space()
                    ui.button("关闭", on_click=list_dlg.close).props("flat")
            list_dlg.open()

        def _open_unknown():
            _open_torrent_list(
                "特别篇 / 未知集", "特别篇·OVA 与批量打包/集号没解析出来的种子，后台一律不自动下。点番名进详情页、对准那一条点『下载』（或忽略）。",
                anime.unknown_episode_rows)

        def _open_failed():
            _open_torrent_list(
                "失败 / 异常",
                "下载失败过（取种/发送失败）或长期停滞无进展的种子。正集可在详情页『下载该源』重试；特别篇/未知集只能对准那一条点『下载』。",
                anime.failed_rows)

        # ---- 事件处理（闭包，直接引用上面的刷新函数）----
        def _approve(anime_id, sel=None):
            async def h():
                pref = (sel.value if sel is not None else "") or ""
                anime.confirm_anime(anime_id, pref)
                n = await anime.download_pending_for_anime(anime_id)
                refresh_all()
                ui.notify(f"已确认，补下 {n} 集" + (f"（源：{pref}）" if pref else ""), type="positive")
            return h

        def _reject(anime_id):
            def h():
                anime.reject_anime(anime_id)
                refresh_all()
                ui.notify("已忽略，移到『已忽略』页", type="positive")
            return h

        def _restore(anime_id):
            async def h():
                anime.restore_anime(anime_id)
                n = await anime.download_pending_for_anime(anime_id)
                refresh_all()
                ui.notify(f"已恢复到『订阅中』，补下 {n} 集", type="positive")
            return h

        def _del_files(anime_id, name, cnt):
            async def h():
                if not await confirm(f"删除《{name}》的 {cnt} 个已下文件？",
                                     "通过 qB 连同硬盘文件一起删除，不可撤销。\n"
                                     "这一集若还有别的源的『备用项』待下，后台会在下一轮采集自动补下一份（删的是这一条种子，不是整集）。不想让它回来：先把备用项『排除』，或整部番用『忽略』。",
                                     ok_label="删除文件", ok_icon="delete_forever"):
                    return
                n = await anime.delete_anime_files(anime_id)
                refresh_all()
                ui.notify(f"已删除 {n} 个文件" if n else "没删成（qB 未连上或已无文件）",
                          type="positive" if n else "warning")
            return h

        def _redownload(tid):
            async def h():   # force 重下一条已删除种子，找回文件
                ok = await anime.download_anime_torrent(tid, force=True)
                refresh_all()
                ui.notify("已重新下载" if ok else "重新下载失败（qB 未连上 / 种子不可用）",
                          type="positive" if ok else "warning")
            return h

        def _unexclude(tid):
            def h():   # 取消排除：把 excluded 放回待下
                anime.unexclude_torrent(tid)
                refresh_all()
                ui.notify("已恢复到待下", type="positive")
            return h

        def _bind(anime_id, inp):
            async def h(e=None):
                bid = parse_bgm_id(inp.value or "")
                if bid is None:
                    ui.notify("请粘贴 bgm 链接或数字 ID", type="warning")
                    return
                # 【绑定前回显】与详情页同一道闸（共用 layout.require_bind_confirm）：
                # 这里是『待识别』列表里的绑定按钮，同样会触发身份合并、同样会删掉另一条番。
                if not await require_bind_confirm(anime_id, bid):
                    return

                # 走 busy_action：bind 会调 fetch_by_id（预算 120 秒），期间没有反馈会被连点
                async def _go():
                    # 【必须在识别之前记下旧目录】待识别的番 jp_name/display_name 都是空的，
                    # apply_bgm_meta 的 keep_path 只冻结【已有值】的字段，挡不住它们被 bgm 名整体写入
                    # → 归档目录当场就变了。不问搬迁的话，之前下好的集会被静默遗弃在旧目录，
                    # 同一部番裂成两个文件夹（详情页那条路径一直是问的，这里漏了）。
                    old_path = anime.anime_save_path(anime_id)
                    ok = await anime.bind_anime_bgm(anime_id, bid)
                    refresh_all()
                    ui.notify("已绑定并识别 ✓" if ok else "绑定失败：ID 不存在或取不到 bgm 数据",
                              type="positive" if ok else "negative")
                    if ok:
                        await maybe_relocate_anime(anime_id, old_path, refresh_all)
                await busy_action(getattr(e, "sender", None), f"bind:{anime_id}", _go,
                                  fail="绑定失败")
            return h

        def _refail(anime_id):
            async def h(e=None):
                # 走 busy_action：识别预算 120 秒，期间按钮无反应会被连点（同详情页理由）
                async def _go():
                    old_path = anime.anime_save_path(anime_id)   # 同 _bind：识别成功会改归档目录
                    # 【必须走 manual_enrich】这里曾直接调 enrich_anime，不清 enrich_tries：
                    # 试满 5 次的番点了这个按钮只当场试一次，此后仍然一次都不自动重试，
                    # 而详情页那个同名按钮是清的——同一个操作两个页面两种行为。
                    ok = await anime.manual_enrich(anime_id)
                    refresh_all()
                    ui.notify("识别成功 ✓" if ok else "还是没识别到（可手动粘贴 bgm 链接绑定）",
                              type="positive" if ok else "warning")
                    if ok:
                        await maybe_relocate_anime(anime_id, old_path, refresh_all)
                await busy_action(getattr(e, "sender", None), f"refail:{anime_id}", _go,
                                  fail="识别失败")
            return h

        async def _download_all(e=None):
            # 【补下全部要防重入】它可能跑几十秒，而同文件 758 行的『重新识别』早就有现成写法。
            # 连点会叠出多轮并发补下：同一集被多轮各挑一次候选，qB 侧靠 409 兜着，
            # 但日志与"已触发 N 集"的数字会变得没法解释。
            async def _go():
                n = await anime.download_all_pending()
                refresh_all()   # 含 manage_panel，好让季度小结的已下/待下计数也即时更新
                ui.notify(f"已触发补下 {n} 集", type="positive")
            await busy_action(getattr(e, "sender", None), "download-all", _go, fail="补下失败")

        async def _qb_sync_now():
            """『下载状态』的立刻刷新：主动向 qB 拉一次在下种子的进度，不必等后台轮询（默认间隔可能上千秒）。"""
            if not config.QB_ENABLED:
                ui.notify("qB 未启用（去设置页开启『发送种子到 qB』）", type="warning")
                return
            if engine.sync_busy(AnimeTorrent):
                # 后台那一轮正在跑。不排队等它：sync 对"qB 里查不到的种子"要连续两轮 miss 才判死，
                # 两轮压在一起会把宽限窗口抹平（见 engine._sync_locks）。这里直接告诉用户"已经在同步了"。
                ui.notify("已有一轮同步在跑，稍等片刻再看", type="info")
                return
            try:
                n = await anime.sync_qb_status(manual=True)
            except Exception as e:                       # qB 连不上/超时：别让异常掀翻页面，给个可读提示
                ui.notify(f"同步失败：{e}", type="negative")
                return
            _refresh_overview(charts=False)              # 刷计数区，不重建环图（保住图例排除态）
            inflight_panel.refresh()                     # 『正在下载』列表的进度条也跟着更新
            ui.notify(f"已同步 {n} 条种子的进度" if n else "没有正在下载的种子", type="positive")

        _reident_busy = {"v": False}   # 防抖：整轮 bgm 遍历可能跑几分钟，期间连点会叠出多轮并发请求

        def _reident(seasons):
            async def h():
                if _reident_busy["v"]:
                    ui.notify("正在重新识别中，请稍候…", type="info")
                    return
                scope = {1: "当季", 2: "近半年", 4: "近1年", None: "全部"}.get(seasons, "")
                ui.notify(f"正在刷新元数据（{scope}）…走 bgm，可能要一会儿。"
                          "已绑好的番只刷资料、不会改绑定；没认出来的会再试一次", type="info")
                _reident_busy["v"] = True
                try:
                    cnt = await anime.reenrich_scope(seasons)
                finally:
                    _reident_busy["v"] = False
                _refresh_overview(charts=True)   # 重识别改了数据，连图表一起刷
                ui.notify(f"完成：{cnt} 部刷到了 bgm 资料", type="positive")
            return h

        def _anime_row(a, sources=None, prog=None):
            # 块级容器 + 行内徽标/标题：徽标贴着标题同排，标题过长时标题自己换行（徽标不再被挤到单独一行）。
            # 【display 不在这里定】徽标的 display 由 pages/layout.py 的全局规则一处定成
            # inline-flex（那是"字在框里居中"的布局保证）。这里只负责 vertical-align 与间距。
            # 早先这 6 处各挂了一个 inline-block，而 !important 的全局规则把它整个顶掉——
            # 类是死的，注释里"q-badge 默认 display:inline"这句自全局 CSS 落地起就不再成立，
            # 下一个人照着它在别处"统一"只会重复一遍无效操作。
            with ui.element("div").classes("pl-2 py-2 leading-relaxed"):
                if sources:   # 源徽标放最前：多源(>1)蓝 / 单源(==1)灰
                    n = len(sources)
                    _lab, _c = (f"多源 {n}", "blue") if n > 1 else (f"单源 {n}", "blue-grey")
                    ui.badge(_lab).props(f"color={_c}").classes(
                        "align-middle mr-2").tooltip("来源: " + " · ".join(sources))
                if a.rejected:                       # 状态徽标（互斥，最多一个）
                    ui.badge("已忽略").props("color=grey").classes("align-middle mr-2")
                elif not a.confirmed:
                    ui.badge("待确认").props("color=orange").classes("align-middle mr-2")
                elif a.finished_at and config.ANIME_FINISH_UNSUB:
                    # 【只在真的会改变行为时才出徽标】"已完结"本身不显示——它的信息量被右边那个
                    # 『已下/可下』覆盖了（12/12 就是完结）。但开了停订时这部番【不再自动下新集】，
                    # 那是行为变化不是状态描述：在此之前它在列表里与正常追番的番长得一模一样，
                    # 用户唯一能察觉的迹象是"它好久没更新了"，而那与源失效、字幕组断更完全同形。
                    ui.badge("已停订").props("color=teal").classes("align-middle mr-2").tooltip(
                        "判定为完结并已停止自动下新集（设置页『完结后停止自动下新集』）——"
                        "若其实还没完（bgm 总集数少记了），进详情页点『继续订阅』。")
                color = "text-gray-500 line-through" if a.rejected else "text-blue-400"
                ui.label(name_of(a)).classes(
                    f"text-lg inline align-middle cursor-pointer {color} hover:underline").on(
                    "click", lambda aid=a.id: open_detail(aid))
                # 【已下/可下】分子＝已发给 qB 的不同正集号数，分母＝库里见过的不同正集号数。
                # 按【集号】数而不是按种子条数：同一集常有好几个源各一份，按条数数出来的比值没有意义。
                # 它把两类问题一眼摊开：
                #   · 分子 < 分母 → 有集号还没到手（锁定源锁太死 / 关键词没匹配上 / 卡在缓冲窗口）
                #   · 分母 > bgm 记的总集数 → 集号本身就不对（别的季的正片挂到了这条记录下面）
                if prog:
                    _have, _seen = prog
                    _tot = a.total_episodes or 0
                    _over = bool(_tot and _seen > _tot)
                    _c = "red" if _over else ("orange" if _have < _seen else "blue-grey")
                    ui.badge(f"{_have}/{_seen}").props(f"color={_c}").classes(
                        "align-middle ml-2").tooltip(
                        f"已下 {_have} 集 / 库里有 {_seen} 集的种子"
                        + (f"；bgm 记 {_tot} 集" if _tot else "；bgm 未给总集数")
                        + ("。集号超出 bgm 记载——多半有别的季的正片挂到了这条记录下面，"
                           "进详情页看『各源的集号范围』" if _over else
                           ("。还有集号没到手：可能是锁定源锁太死、版本关键词没匹配上，"
                            "或还在缓冲窗口里" if _have < _seen else "。都到手了")))
                sl = season_label(a)
                if sl:
                    ui.badge(sl).props("color=purple").classes("align-middle ml-2")
                p = getattr(a, "platform", None)   # 内联原 platform_badge：bgm 判定非 TV(剧场版/OVA…)紫标，好带上行内排版类
                if p and p != "TV":
                    ui.badge(p).props("color=deep-purple").classes(
                        "align-middle ml-2").tooltip("bgm 判定的类型（非 TV）")

        # ---- 页面布局 ----
        with ui.tabs().classes("w-full") as tabs:
            for _k, _name, _icon in _TABS_DEF:
                ui.tab(_k, _name, _icon)
        # 懒加载：首屏只构建当前 tab 的内容；切到别的 tab 首次才建（6 个面板 → 1 个，砍首屏构建/推送）。
        # 面板都是 @ui.refreshable，未构建过的 .refresh() 是安全 no-op，所以刷新逻辑无需改动。
        _builders = {
            "overview": lambda: (_ov_snap.clear(), overview_head(), charts_panel(),
                                 overview_tail(), inflight_panel(), recent_panel(),
                                 _ov_snap.clear()),
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
