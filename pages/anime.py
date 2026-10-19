"""主页 `/`：番剧列表[按季度] / 待确认 / 待识别 / 已忽略 / 新入库 / 仪表盘。

刷新面板定义在 page 函数内部（每个浏览器连接各自一份），避免模块级
单例 refreshable 在多页面/多客户端下互相串。
"""
from urllib.parse import quote

from nicegui import ui

import db
from db.models import AnimeTorrent
from core import alerts, anime, engine
import config
from .anime_detail import maybe_relocate_anime, render_anime_detail, restore_anime_gated, subscribe_gated
from .layout import (busy_action, confirm, cover_box, cover_title, ep_ranges_text, finished_tip,
                     require_bind_confirm,
                     ep_label,
                     expand_collapse_bar,
                     frame, group_by_quarter,
                     human_size, kpi_cards, live_status, name_of, paginate, parse_bgm_id,
                     platform_spec, recent_table, season_label, source_options, sources_spec,
                     warn_banner, poll_time_text, poll_stale)
from .sources import render_sources


def _kw_hit(a, kw: str) -> bool:
    """番剧表搜索框的匹配判据：大小写不敏感子串，命中中文名/解析名/日文名/bgm ID 任一。

    【为什么四个字段都要看】UI 显示的是 `display_name or title`，但同一部番在别处
    露出的是另外两个：日文名是**下载目录名**（用户从磁盘上看到的就是它），
    bgm ID 是详情页与外站链接上的身份键。只匹配显示名的话，
    "我在硬盘上看到这个目录，回来搜一下是哪部"这个最常见的用法直接搜不到。
    """
    return any(kw in str(v).lower()
               for v in (a.display_name, a.title, a.jp_name, a.bangumi_id) if v)


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
        # 番剧表：分页页码 + 一键展开/收起意图（None=按默认）+ 搜索词。
        # 【搜索词必须存在这里、不能只留在输入框控件上】面板是 @ui.refreshable，
        # 翻页/展开/详情弹窗关闭都会整体重建它，控件上的值会跟着没。
        # open：用户手动开合过的折叠块 {季度键: 开/合}。详情弹窗里的操作（确认下载/补齐/忽略…）会整体重建
        # 这几个面板，不记的话重建后一律回到"只开最新一季"——正看着别的季度，一点操作它就被收起来了。
        # 点『全部展开/收起』时清空（expand_collapse_bar），以最后那次全局操作为准。
        manage_page = {"n": 1, "expand": None, "kw": "", "open": {}}
        confirm_page = {"expand": None, "open": {}}   # 待确认/已忽略也按季度分组、默认只开第一组：同样要一键展开（R36 ux-6）
        reject_page = {"expand": None, "open": {}}

        def _manage_goto(e):
            manage_page["n"] = int(e.value)
            manage_panel.refresh()

        # 搜索框与命中计数【建在 refreshable 之外】(R27 修) —— 它们必须活过 manage_panel 的重建。
        # 第一版把搜索框建在 manage_panel 里面，而 on_change 最后一句就是
        # `manage_panel.refresh()`：refresh 会 container.clear() 把这个输入框一起删掉再新建一个，
        # 于是**每打完一段字停顿 300ms（debounce），焦点和光标就没了** ——
        # 用户搜「葬送」停一下想补「的芙莉莲」，后面一个字都打不进去。
        # 计数文案改成给稳定的 label 赋 .text（同 logs.py 的写法），不参与重建。
        _match_lbl: dict = {}

        def _set_match_count(hit: int, total: int) -> None:
            el = _match_lbl.get("el")
            if el is not None:
                el.text = f"匹配 {hit} / {total} 部" if total else ""

        def _build_manage_tab() -> None:
            """番剧表 tab：先建一次稳定的搜索行，再建可刷新的面板本体。"""
            with ui.row().classes("items-center gap-3 pl-1 pt-1 flex-wrap"):
                # 纯前端过滤，不打库：过滤的是 list_all_anime() 本来就要取的那一份。
                # 【为什么贴着表放】它作用的对象是下面这张番剧表，与"全部展开/收起""分页"
                # 是同一组表控件，放到页面顶部会让人以为它管的是整页六个 tab。
                ui.input(placeholder="搜索番名 / bgm ID", value=manage_page["kw"],
                         on_change=_manage_search).props(
                    "clearable debounce=300").classes(
                    "w-56 min-w-0 max-sm:w-full").mark("anime-search")
                _match_lbl["el"] = ui.label().classes("text-xs text-gray-500")
            manage_panel()

        def _manage_search(e):
            # clearable 的叉号把值置成 None，不是空串。
            new = (e.value or "").strip()
            if new == manage_page["kw"]:
                return          # debounce 期间的重复回调：不重建，免得打断正在输入的人
            manage_page["kw"] = new
            # 回第一页：paginate 本身会把越界页码夹回合法范围，所以不重置也不会出空页——
            # 但那样是"夹到结果集的最后一页"，搜完落在最后一页上，人以为没搜到。
            manage_page["n"] = 1
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
            # 番维度四卡（单位：部）与种子维度四卡（单位：条）各自打包，"|" 分组，窄了整组换行成上下布局；数字保持各自语义色
            kpi_cards([("订阅中", k["tracking"], "", lambda: tabs.set_value("manage")),
                       ("待识别", k["fail"], "red", lambda: tabs.set_value("fail")),
                       ("待确认", k["confirm"], "orange", lambda: tabs.set_value("confirm")),
                       ("已忽略", k["rejected"], "", lambda: tabs.set_value("reject")),
                       "|",
                       ("已交付", k["done"], "green", None),
                       ("未知集", ps["unknown"], "purple", _open_unknown),
                       # 名字与下方 chip『失败数』(仅 error) 区分开：这张卡是"需要处理的总数"，
                       # 含长期停滞，和点开后的 failed_rows()/弹窗标题『失败 / 异常』一致
                       ("失败/异常", ov["status"]["error"] + ov["status"]["stalled"], "red",
                        _open_failed),
                       ("种子数", k["torrents"], "", None)])

            # ── qB 未启用提醒 ──
            if not ov["config"]["qb"]:
                warn_banner("qB 未启用：只采集元数据、不实际下载（设置页开启 QB_ENABLED 后生效）")

            # ── 三类『只报不改』的巡检发现 ──
            # 判据在 core/anime 的三个 suspect_*，身份/事实指纹、文案、已读过滤在 core/alerts。
            # 【文案为什么不在这里】它要被用在三个地方：这条横幅、『已读的提示』列表里的存档、
            # 以及巡检推送。留在页面里的话另外两处就得各抄一遍（第①号缺陷形状）。
            # 三类各自"为什么只报不改"的长注释跟着搬到了 core/alerts 的 _dup/_mva/_wrb 上方。
            _al = alerts.view()
            for _a in _al["live"]:
                _pre = ""
                if _a["revived"]:
                    _chg = "；".join(f"{k} {o} → {n}" for k, o, n in _a["revived"]["changed"])
                    _pre = (f"【你在 {_a['revived']['at'].strftime('%m-%d %H:%M')} 标过已读，"
                            f"之后：{_chg}】")
                warn_banner(_pre + _a["text"], action=("知道了", "inbox", _ack_alert(_a)))

            if _al["acked"]:
                _gone = sum(1 for r in _al["acked"] if r["state"] == "gone")
                with ui.row().classes("items-center gap-2 pl-1"):
                    ui.label(f"已读的提示 {len(_al['acked'])} 条"
                             + (f"（其中 {_gone} 条现在已经不成立了）" if _gone else "")
                             ).classes("text-sm text-gray-400")
                    ui.button("查看", icon="inbox", on_click=_open_acked).props(
                        "flat dense").classes("btn-sm")

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
            el.options["baseOption"]["legend"]["selected"] = dict(_legend_sel)   # 配置分 base/media，图例在 base 里
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
                        # 图例显示【全名】、不截断：环图与图例按像素排（环心 100px，图例从 205px 起），
                        # 宽屏下最长的组名（"豌豆字幕组&风之圣殿字幕组&LoliHouse" 约 220px）也是一行；放不下的源照旧翻页。
                        # 图表本身不足 480px 宽（窄屏、两列堆叠时）走 media：环图按比例放、长名换行而不是截断。
                        ui.echart({
                            "baseOption": {
                                # Tailwind -400 十色（sRGB hex；ECharts canvas 不吃 oklch）
                                "color": ["#50a2ff", "#a684ff", "#00d492", "#ff8904", "#ff6467",
                                          "#00d3f3", "#fb64b6", "#9ae600", "#c27aff", "#00d5be"],
                                "tooltip": {"trigger": "item", "formatter": "{b}<br/>{c} 种子 · {d}%"},
                                "legend": {"type": "scroll", "orient": "vertical", "left": 205,
                                           "top": "middle",
                                           "textStyle": {"color": "#99a1af",  # gray-400(灰2)
                                                         "width": 240, "overflow": "break"},
                                           "selected": dict(_legend_sel)},  # 回填用户的图例排除态
                                "series": [{
                                    "name": "种子来源", "type": "pie",
                                    "radius": ["45%", "72%"], "center": [100, "50%"],
                                    "avoidLabelOverlap": False,
                                    "itemStyle": {"borderColor": "#1a1c22", "borderWidth": 2},
                                    "label": {"show": False, "position": "center"},
                                    "emphasis": {"label": {"show": True, "color": "#d1d5dc",
                                                           "fontSize": 14, "fontWeight": "bold",
                                                           "formatter": "{b}\n{c}"}},
                                    "data": [{"name": src, "value": tot} for src, tot in ov["by_source"]],
                                }],
                            },
                            "media": [{
                                "query": {"maxWidth": 480},
                                "option": {
                                    "legend": {"left": "50%", "textStyle": {"width": 140}},
                                    "series": [{"center": ["25%", "50%"]}],
                                },
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
            # 待下拆 将下载/备用/待确认/未知集/特别篇（库态『下载中』恒≈0、与 qB 实时态重复不单列）；失败、种子总数一并列出，与 KPI 卡呼应
            chips = [
                # sent = 已发给 qB（含仍在下的）；真正下完的数在下方 qB 实时区的『已完成』
                ("已交付", ov["status"]["sent"], "green",
                 "已发送给 qB 的种子数（含仍在下载的）。真正下完的看下方『已完成』"),
                ("将下载", ps["will"], "blue", "已确认番·本集首选，会自动下（特别篇/未知集不在其列，需手动下）"),
                ("备用项", ps["backup"], "blue-grey", "同集已有『将下载/已下』的更优版本，不会自动下"),
                ("待确认", ps["unconfirmed"], "orange", "番还没确认，去『待确认』页点确认才会下"),
                *([("已下齐·停订", ps["finished"], "teal",
                    "番已按 bgm 总集数下齐、且设置里开了『下齐后停止自动下新集』，所以这些不会自动下。"
                    "若那部番其实还没完（bgm 的总集数少记了），去详情页点『继续订阅』")]
                  if ps.get("finished") else []),
                ("未知集", ps["unknown"], "purple",
                 "集号没解析出来的（批量打包/命名怪）：后台不自动下。点开看是哪几条，"
                 "去详情页点『第?集』补集号、设为特别篇，或排除", _open_unknown),
                # 特别篇只作提示、不是待办：它们本来就不该自动下，堆着也不用管，所以只在非零时露出、用中性色
                *([("特别篇", ps["special"], "blue-grey",
                    "特别篇·OVA，以及剧场版/WEB 等单部作品：后台不自动下，不用处理。"
                    "点开看是哪几条，要哪条就到详情页对准它点『下载』", _open_special)]
                  if ps.get("special") else []),
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
                for label, val, color, tip, *click in chips:   # 可选第 5 项：点开明细的回调
                    b = ui.badge(f"{label} {val}").props(f"color={color}")
                    if tip:
                        b.tooltip(tip)
                    if click:
                        b.classes("cursor-pointer").on("click", click[0])
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
                ui.badge(f"已完成 {q['completed']}").props("color=green").tooltip(
                    "qB 报告进度已满的种子数（真正下完的）—— 上面『已交付』卡对照的就是它")
                ui.badge(f"下载中 {q['downloading']}").props("color=blue").tooltip("qB 里此刻正在下的种子数")
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
                # 【上次采集】(R36 体检 ux-5) 上面四枚全是配置的镜像，没有一枚说"实际发生过什么"：
                # 6 条 feed 全 403 时仪表盘与正常时逐字相同。这两枚读的是 poll_once 每轮末写的运行时键
                # （与剧场版页的 MOVIE_SCAN_LAST 同一条路），只有最近一轮，不做历史 —— 历史在日志里。
                _pc = ov["config"]
                _failed = _pc["poll_failed"]
                _stale = poll_stale(_pc["poll_last"], _pc["poll"])
                if not _pc["poll_last"]:
                    _tip = "还没跑完过一轮采集"
                else:
                    _tip = f"上一轮采集结束于 {_pc['poll_last']}" + ("（全部源抓取成功）" if not _failed else "")
                    if _pc["poll_ok_last"] and _pc["poll_ok_last"] != _pc["poll_last"]:
                        _tip += f"；上次至少一个源抓到：{poll_time_text(_pc['poll_ok_last'])}"
                    if _stale:
                        _tip += (f"。已经超过两个轮询间隔（{_pc['poll']}s）没有完成新的一轮 —— "
                                 "采集关着、业务库停摆、上一轮被维护打断都会这样")
                ui.badge(f"上次采集 {poll_time_text(_pc['poll_last'])}").props(
                    f"color={'red' if _stale else 'blue-grey'}").tooltip(_tip)
                if _failed:
                    ui.badge(f"{len(_failed)} 个源抓取失败").props("color=red").tooltip(
                        "上一轮里这些源没抓到：" + "、".join(_failed) + " —— 原因看日志页的『抓取失败』行")

        @ui.refreshable
        def confirm_panel():
            pend = anime.pending_confirm()
            if not pend:
                ui.label("没有待确认的番。（『待确认』策略的源组发现的番会出现在这里）").classes("text-gray-500 p-4")
                return
            smap = anime.source_map()   # 批量取来源，避免逐番查的 N+1
            expand_collapse_bar(confirm_page, confirm_panel.refresh)
            exp = confirm_page["expand"]   # None=只开第一组；True/False=一键全展开/收起
            for i, (q, items) in enumerate(group_by_quarter(pend)):
                with ui.expansion(f"{engine.quarter_label(q)}   ·   {len(items)} 部",
                                  value=confirm_page["open"].get(q, i == 0 if exp is None else exp)).classes(
                        "w-full").props(remove="dense").on_value_change(
                        lambda e, q=q: confirm_page["open"].__setitem__(q, e.value)):
                    with ui.element("div").classes("cover-grid"):
                        for a in items:
                            _confirm_card(a, smap.get(a.id, []))

        def _confirm_card(a, srcs):
            # 待确认一张卡：封面/番名点开详情，下面是操作区。整张卡【不】可点——里面有下拉框和按钮，
            # 点它们会冒泡成"打开详情"。操作区用 mt-auto 压到卡底，同一行的卡按钮对齐。
            with ui.card().tight().classes("cover-card w-full"):
                _card_cover(a, status=("待确认", "orange", None), sources=srcs).classes(
                    "cursor-pointer").on("click", lambda aid=a.id: open_detail(aid))
                with ui.column().classes("px-3 py-2 gap-1.5 w-full grow"):
                    _card_title(a).classes("cursor-pointer").on("click", lambda aid=a.id: open_detail(aid))
                    _rs, _rd = anime.confirm_reason_text(a.confirm_reason)
                    ui.label("原因: " + _rs).classes("text-xs text-orange-400").tooltip(_rd)
                    with ui.column().classes("w-full gap-2 mt-auto pt-1"):
                        # 预填当前锁定源：这个番可能是【补齐/绑定 bgm 打回来的】、本来就设过锁，
                        # 下拉恒空会让用户点一下『确认下载』就把锁静默解掉（选中值会被写回）。
                        # 与详情页那个【同一个控件】对齐：同样的 source_options、同样绑 pref_source。
                        # 卡片窄，空选项只写"按优先级"（全写会截成"下载源：按优…"），完整含义放悬停里说
                        sel = ui.select(source_options(srcs, "按优先级"),
                                        value=(a.pref_source or "")).classes("w-full")
                        sel.tooltip("下载源：不选＝按优先级、多源兜底；选一个组＝锁定只下这个组")
                        with ui.row().classes("w-full gap-2 items-center"):
                            ui.button("确认下载", on_click=_approve(a.id, sel)).props(
                                "unelevated color=primary").classes("grow")
                            ui.button("忽略", on_click=_reject(a.id)).props("flat color=grey")

        @ui.refreshable
        def reject_panel():
            rej = anime.list_rejected_anime()
            dels = anime.deleted_torrent_rows()      # 已删除种子（本页最底折叠①）
            excls = anime.excluded_torrent_rows()    # 已排除种子（本页最底折叠②）
            if not rej and not dels and not excls:
                ui.label("没有已忽略的番。（待确认/详情页点『忽略』会进这里，可随时恢复）").classes("text-gray-500 p-4")
                return
            smap = anime.source_map()   # 批量取来源，避免逐番 N+1
            dlmap = anime.downloaded_counts([a.id for a in rej])   # 同上：可删文件数也一次取齐
            sw = anime.single_work_ids() if dels or excls else set()   # 集号标 SP 用（单部作品的 -2）
            expand_collapse_bar(reject_page, reject_panel.refresh)
            exp = reject_page["expand"]
            for i, (q, items) in enumerate(group_by_quarter(rej)):
                with ui.expansion(f"{engine.quarter_label(q)}   ·   {len(items)} 部",
                                  value=reject_page["open"].get(q, i == 0 if exp is None else exp)).classes(
                        "w-full").props(remove="dense").on_value_change(
                        lambda e, q=q: reject_page["open"].__setitem__(q, e.value)):
                    with ui.element("div").classes("cover-grid"):
                        for a in items:
                            _reject_card(a, smap.get(a.id, []), dlmap.get(a.id, 0))

            # 已删除的种子：本页最底的独立折叠，可『重新下载』找回（deleted 是终态、不会自动重下）
            if dels:
                with ui.expansion(f"已删除的种子（{len(dels)}）", icon="delete_outline",
                                  value=reject_page["open"].get("__deleted", False)).classes(
                        "w-full mt-2").on_value_change(
                        lambda e: reject_page["open"].__setitem__("__deleted", e.value)):
                    ui.label("你手动删掉文件的种子（终态，不会自动重下）。需要就点『重新下载』找回。").classes(
                        "text-xs text-gray-500 p-1")
                    for d in dels:
                        with ui.row().classes("items-center gap-3 w-full text-sm py-1").style(
                                "border-bottom:1px solid rgba(255,255,255,.06)"):
                            ui.label(d["name"]).classes(
                                "shrink-0 cursor-pointer text-blue-400 hover:underline").on(
                                "click", lambda aid=d["anime_id"]: open_detail(aid))
                            ui.label(ep_label(d["episode"], d["anime_id"] in sw)).classes("shrink-0 text-gray-400")
                            ui.label(d["raw"]).classes("text-gray-500 text-xs break-all min-w-0 grow")
                            _rb = ui.button("重新下载", icon="download",
                                            on_click=_redownload(d["id"])).props(
                                "flat dense color=primary").classes("btn-sm")
                            _rb.set_enabled(config.QB_ENABLED)
                            _rb.tooltip("强制重新下这条到原目录（找回删掉的文件）" if config.QB_ENABLED
                                        else "qB 未启用，去设置页开启后可下载")

            # 已排除的种子：独立折叠，可『恢复』放回待下（excluded 是主动从待下排除的终态）
            if excls:
                with ui.expansion(f"已排除的种子（{len(excls)}）", icon="block",
                                  value=reject_page["open"].get("__excluded", False)).classes(
                        "w-full mt-2").on_value_change(
                        lambda e: reject_page["open"].__setitem__("__excluded", e.value)):
                    ui.label("你从待下里主动排除的种子（不自动下、RSS 再遇到也不重收）。点『恢复』放回待下。").classes(
                        "text-xs text-gray-500 p-1")
                    for x in excls:
                        with ui.row().classes("items-center gap-3 w-full text-sm py-1").style(
                                "border-bottom:1px solid rgba(255,255,255,.06)"):
                            ui.label(x["name"]).classes(
                                "shrink-0 cursor-pointer text-blue-400 hover:underline").on(
                                "click", lambda aid=x["anime_id"]: open_detail(aid))
                            ui.label(ep_label(x["episode"], x["anime_id"] in sw)).classes("shrink-0 text-gray-400")
                            ui.label(x["raw"]).classes("text-gray-500 text-xs break-all min-w-0 grow")
                            ui.button("恢复", icon="undo", on_click=_unexclude(x["id"])).props(
                                "flat dense color=primary").classes("btn-sm").tooltip(
                                "放回待下，重新参与下载/去重")

        def _reject_card(a, srcs, nf):
            # 已忽略一张卡：同待确认，封面/番名点开详情、操作区压到卡底。封面不置灰——这一页全是已忽略，灰了反而看不清。
            with ui.card().tight().classes("cover-card w-full"):
                # 与详情页同一套词：超期忽略(rejected 且 confirmed=False，按开始使用日自动判) / 已忽略(人工)
                status = (("超期忽略", "grey", "开播早于『开始使用日』，自动判的忽略。"
                                             "改开始使用日或点『恢复订阅』都能放回来")
                          if not a.confirmed else ("已忽略", "grey", None))
                _card_cover(a, status=status, sources=srcs).classes(
                    "cursor-pointer").on("click", lambda aid=a.id: open_detail(aid))
                with ui.column().classes("px-3 py-2 gap-1.5 w-full grow"):
                    _card_title(a).classes("cursor-pointer").on("click", lambda aid=a.id: open_detail(aid))
                    with ui.row().classes("w-full gap-2 items-center no-wrap mt-auto pt-1"):
                        ui.button("恢复订阅", icon="undo", on_click=_restore(a.id)).props(
                            "unelevated color=primary").classes("grow")
                        if nf:  # 只有确实下过文件才给『删除文件』；卡片窄，只留图标，说明放 tooltip
                            ui.button(icon="delete_forever",
                                      on_click=_del_files(a.id, name_of(a), nf)).props(
                                "flat color=negative").tooltip(
                                f"删除已下的 {nf} 个文件：连同 qB 里的硬盘文件一起删（不可撤销）")

        @ui.refreshable
        def fail_panel():
            items = anime.list_unmatched_anime()
            if not items:
                ui.label("没有待识别的番。（bgm 没自动匹配上的番会出现在这里，可重试或手动绑定）").classes(
                    "text-gray-500 p-4")
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
                sw = anime.single_work_ids()
                with ui.column().classes("w-full gap-0"):
                    for r in rows:
                        text, color = live_status(r["status"], r["qb_state"], r["qb_progress"],
                                                  r["qb_synced_at"], r["qb_dlspeed"])
                        # no-wrap + 标题 grow/min-w-0 + 徽标 shrink-0：长标题在自己那一格里换行，
                        # 徽标永远钉在右侧，不会被挤到下一行左边去
                        with ui.row().classes("items-center gap-3 w-full text-sm py-1 no-wrap").style(
                                "border-bottom:1px solid rgba(255,255,255,.08)"):
                            ui.label(f'{r["name"]}  {ep_label(r["episode"], r["anime_id"] in sw)}').classes(
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
            sw = anime.single_work_ids()   # 单部作品的 -2 按特别篇标（SP / 『特别篇』徽标）
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
                                          auto_off=off_why.get(r["anime_id"], ""),
                                          retry_at=r.get("retry_at"),
                                          single_work=r["anime_id"] in sw)
                rows.append({
                    "id": r["id"],
                    "detail_id": r["anime_id"],
                    "time": r["time"],
                    "name": f'{r["name"]}  {ep_label(r["episode"], r["anime_id"] in sw)}',
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
                            # 【这个数叫『已交付』不叫『已下』】(R24) 同一个词此前指三个不同的量：
                            #   · 这里 = `quarter_brief()['done']` = **status=='sent' 的种子条数**；
                            #   · 列表行的 tooltip「已下 N 集」= episode_progress 的分子
                            #     （HAVE_STATUSES 的**不同正集号**个数，R20 你拍板的口径）；
                            #   · 剧场版详情的「已下 N」= 该片 HAVE_STATUSES 的**版本条数**。
                            # 而这里这个数在仪表盘 KPI 上就叫『已交付』—— 同一个数两个名字。
                            # 「已下」留给"真到手"那两个口径。
                            ui.label(f"已交付 {b['done']}")
                            ui.label(f"待下 {b['pending']}")
                            ui.label(f"种子 {b['torrents']}")

            # 面板设置：追番中恒显示；待确认/已拒绝各自按开关决定带不带上
            def _visible(a):
                if a.rejected:
                    return config.ANIME_SHOW_REJECTED
                if not a.confirmed:
                    return config.ANIME_SHOW_PENDING
                return True
            all_animes = [a for a in anime.list_all_anime() if _visible(a)]
            if not all_animes:
                _set_match_count(0, 0)
                ui.label("（还没有番剧，等采集）").classes("text-gray-500 p-4")
                return
            kw = (manage_page["kw"] or "").strip().lower()
            animes = [a for a in all_animes if _kw_hit(a, kw)] if kw else all_animes
            animes.sort(key=lambda a: (_state_rank(a), a.id))  # 追番中在上，待确认、已拒绝垫底
            src_map = anime.source_map()
            prog_map = anime.episode_progress([a.id for a in animes])   # 一条 SQL，与 source_map 同形
            gap_map = anime.episode_gaps(animes)    # 按开播日推算应播、库里却没见过的集（缺集提示）
            yrs = max(1, config.ANIME_PAGE_YEARS)  # 防 0（每页 0 季会除零）
            groups, total_pages, page = paginate(group_by_quarter(animes), manage_page["n"], yrs * 4)
            manage_page["n"] = page
            _set_match_count(len(animes), len(all_animes) if kw else 0)
            with ui.row().classes("items-center gap-3 pl-1 pb-1 flex-wrap"):
                expand_collapse_bar(manage_page, manage_panel.refresh)
                if total_pages > 1:
                    ui.pagination(1, total_pages, direction_links=True, value=page,
                                  on_change=_manage_goto)
                    ui.label(f"共 {total_pages} 页 · 每页 {yrs} 年").classes("text-xs text-gray-500")
            if not animes:
                ui.label(f"（没有匹配「{manage_page['kw']}」的番剧）").classes("text-gray-500 p-4")
                return
            # 搜索态下强制展开：命中的番散在各季，全收着等于搜完还要一季季点开。
            exp = manage_page["expand"]  # None=各季按默认(仅最新季开)；True/False=一键全展开/收起(跨页一致)
            if kw and exp is None:
                exp = True
            for i, (q, items) in enumerate(groups):
                _open = manage_page["open"].get(q, exp if exp is not None else i == 0)
                # 主结构分组：退出紧凑档（理由见 pages/layout.py 的 ui.expansion 默认值）
                _exp = ui.expansion(f"{engine.quarter_label(q)}   ·   {len(items)} 部",
                                    value=_open).classes("w-full").props(remove="dense")
                # 懒加载：折叠的季度先不建行（省首建成本）；首次展开时才建，之后不再重建
                _fl = {"built": False}

                def _fill(qi=items, box=_exp, fl=_fl):
                    if fl["built"]:
                        return
                    fl["built"] = True
                    with box, ui.element("div").classes("cover-grid"):
                        for a in qi:
                            _anime_card(a, src_map.get(a.id), prog_map.get(a.id), gap_map.get(a.id))

                def _toggle(e, q=q, f=_fill):
                    manage_page["open"][q] = e.value     # 记住手动开合，面板重建后照此恢复
                    if e.value:
                        f()

                if _open:
                    _fill()
                _exp.on_value_change(_toggle)

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
            """刷新某 tab 的只读实时区。只有仪表盘有实时区，其余 tab 一律不动。

            【不含 confirm/fail】它们有用户正在输入的绑定框/源下拉，重建会清空半途输入。

            【R27 把 reject 也摘了】那一支原来每 30 秒重建一次『已忽略』面板，而面板里
            每个 `ui.expansion` 都是 `value=(i == 0)` 硬编码重建、没有任何持久化的展开意图
            （对比 `manage_page["expand"]`）。于是用户刚展开的折叠块每 30 秒自动收起一次、
            滚动位置一起丢 —— 最难受的正是底部那个『已删除的种子』，它恰恰是用户展开去找条目
            点『重新下载』的地方：展开 → 找 → 还没点到就合上了。
            摘掉它不丢任何东西：本页的操作走 `refresh_dynamic()`（那里点名刷了 reject_panel），
            而后台改动 rejected 的只有『开始使用日超期忽略』那条批量重算 ——
            那不是 30 秒尺度的实时量。顺带省掉每 30 秒一次事件循环上的同步查询
            （list_rejected_anime + deleted/excluded_torrent_rows + downloaded_counts，
            真库快照上整页构建 127ms）。
            """
            if tab == "overview":
                _refresh_overview(charts=False)   # 30s 定时器不碰图表，环图交互态不被打断
                inflight_panel.refresh()
                recent_panel.refresh()

        def refresh_timer():
            # 30s 定时器：只刷『当前可见 tab』的实时区，隐藏 tab 不动（省 CPU、消除每 30s 的周期性卡顿）。
            # 【停摆/维护时不撞库】(R22) 剧场版页的同名函数早有这道闸，注释还写着"番剧页的
            # 同名函数也是这个口径"—— 而这一页根本没有：同一句话只在一处兑现，第①号形状。
            # 两个后果：① MySQL 掉线时首页每 30 秒撞一次库，而建连接是同步调用、上界 5 秒，
            #   事件循环每 30 秒被冻 5 秒，每开一个标签页翻一倍；
            # ② R21 之后维护期 is_data_down() 也为真，这些刷新会在 refresh 里抛 DatabaseBusy。
            if db.is_data_down():
                return
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
            """未知集 / 特别篇 / 失败异常 的明细弹窗。分页而不是一次全渲染——总数照实显示在标题里。"""
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
                                          title, desc, fetch, int(e.value)))
                        ui.label(f"第 {page}/{pages} 页").classes("text-xs text-gray-500")
                    ui.space()
                    ui.button("关闭", on_click=list_dlg.close).props("flat")
            list_dlg.open()

        def _open_unknown():
            _open_torrent_list(
                "未知集", "批量打包/集号没解析出来的种子，后台不自动下。点番名进详情页：点『第?集』补集号或设为特别篇，"
                "要它就对准那一条点『下载』，不要就排除。（特别篇与剧场版/WEB 单部作品不在这里，它们不用处理）",
                anime.unknown_episode_rows)

        def _open_special():
            _open_torrent_list(
                "特别篇", "特别篇·OVA，以及 bgm 判定为剧场版/WEB 等单部作品（总共 1 集）名下没有集号的种子。"
                "后台不自动下，也不用处理；要哪条就点番名进详情页、对准它点『下载』。其实是正片的，点集号改回来。",
                anime.special_episode_rows)

        def _ack_alert(f):
            """把一条发现收进『已读的提示』。f 是 core.alerts.view() 里的一项。"""
            def h():
                alerts.ack(f["ident"], f["kind"], f["fact"], f["text"])
                ui.notify("已收进『已读的提示』")     # 只说已经发生的事，不预告"以后不会再看到"
                overview_head.refresh()
            return h

        def _open_acked():
            """『已读的提示』列表。

            【必须是对话框，不能是就地展开】仪表盘的 overview_head 每 30 秒被
            `ui.timer` 重建一次（见本文件底部）。放一个 ui.expansion 在里面的话，
            用户展开去点『取消已读』，最多 30 秒就被合上 —— R27 已经为完全同构的
            bug 付过一次学费（见 manage_panel 那段 docstring）。对话框活在
            client.layout 上，不受面板重建影响。
            """
            list_dlg.clear()
            rows = alerts.view()["acked"]
            with list_dlg, ui.card().classes("w-full").style("max-width:720px"):
                ui.label(f"已读的提示 · {len(rows)}").classes("text-base font-bold")
                ui.label("这些是你点过『知道了』的巡检发现。它们的关键事实变了会自己回到上面 —— "
                         "比如那条番从『超期忽略』变成『追番中』、改了 bgm 绑定、或者出问题的集号变了。"
                         ).classes("text-xs text-gray-400")
                if not rows:
                    ui.label("（还没有收起过任何提示）").classes("text-gray-500 p-2")
                for r in rows:
                    with ui.column().classes("gap-0 w-full py-1").style(
                            "border-bottom:1px solid rgba(255,255,255,.08)"):
                        with ui.row().classes("items-center gap-2 w-full"):
                            ui.badge(alerts.KINDS.get(r["kind"], r["kind"])).props("color=blue-grey")
                            # 两句都是【此刻算出来的事实】，不写"已解决"——程序判不出用户是修好了还是删了
                            ui.badge("现在仍然成立" if r["state"] == "fresh" else "现在已经不成立了"
                                     ).props("color=blue-grey" if r["state"] == "fresh" else "color=green")
                            ui.space()
                            ui.button("取消已读", icon="undo",
                                      on_click=_unack(r["ident"])).props("flat dense").classes("btn-sm")
                        ui.label(r["summary"] or "—").classes("text-sm break-all")
                        ui.label(f"标记于 {r['at'].strftime('%Y-%m-%d %H:%M')}").classes(
                            "text-xs text-gray-500")
                with ui.row().classes("items-center gap-3 w-full mt-1"):
                    if any(r["state"] == "gone" for r in rows):
                        ui.button("清掉已经不成立的", icon="delete_sweep",
                                  on_click=_drop_gone).props("flat dense color=grey").classes("btn-sm")
                    ui.space()
                    ui.button("关闭", on_click=list_dlg.close).props("flat")
            list_dlg.open()      # 【别漏这一行】建完内容还要真的打开，否则点『查看』毫无反应

        def _unack(ident):
            def h():
                alerts.unack(ident)
                ui.notify("已取消已读，它回到了上面的提示里")
                overview_head.refresh()
                _open_acked()          # 就地重建列表，数字与行跟着变
            return h

        def _drop_gone():
            n = alerts.drop_gone()
            ui.notify(f"清掉了 {n} 条已经不成立的记录")
            overview_head.refresh()
            _open_acked()

        def _open_failed():
            _open_torrent_list(
                "失败 / 异常",
                "下载失败过（取种/发送失败）或长期停滞无进展的种子。正集可在详情页『下载该源』重试；特别篇/未知集只能对准那一条点『下载』。",
                anime.failed_rows)

        # ---- 事件处理（闭包，直接引用上面的刷新函数）----
        def _approve(anime_id, sel=None):
            async def h():
                pref = (sel.value if sel is not None else "") or ""
                n = await subscribe_gated(anime_id, "confirm", pref)   # E-52 的闸，四个入口共用
                if n is None:
                    return
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
                n = await restore_anime_gated(anime_id)     # E-52 的闸在里面，两个入口共用
                if n is None:
                    return
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
            async def h(e):
                bid = parse_bgm_id(inp.value or "")
                if bid is None:
                    ui.notify("请粘贴 bgm 链接或数字 ID", type="warning")
                    return
                # 走 busy_action：**从回显闸就开始包**。(R27) 原来只包了下半段，
                # 而 `require_bind_confirm` 第一句就是 `enrich.fetch_by_id`（预算 120 秒）——
                # bgm 慢或不通时那 120 秒里按钮不 loading、也没有去重键，用户连点就会
                # 叠出两个「绑定到《X》？」确认框、两条协程各跑一遍 fetch。
                async def _go():
                    # 【绑定前回显】与详情页同一道闸（共用 layout.require_bind_confirm）：
                    # 这里是『待识别』列表里的绑定按钮，同样会触发身份合并、同样会删掉另一条番。
                    if not await require_bind_confirm(anime_id, bid):
                        return
                    # 【必须在识别之前记下旧目录】待识别的番 jp_name/display_name 都是空的，
                    # apply_bgm_meta 的 keep_path 只冻结【已有值】的字段，挡不住它们被 bgm 名整体写入
                    # → 归档目录当场就变了。不问搬迁的话，之前下好的集会被静默遗弃在旧目录，
                    # 同一部番裂成两个文件夹（详情页那条路径一直是问的，这里漏了）。
                    old_path = anime.anime_save_path(anime_id)
                    _rep: dict = {}    # 收『这次绑定冻结了哪些字段』，见 core 里 report 出参的说明
                    ok = await anime.bind_anime_bgm(anime_id, bid, report=_rep)
                    refresh_all()
                    # 【冻结了就别报绿】(R22) 有已归档文件时 keep_path 会把番名/季度全冻住，
                    # 而用户来点这个按钮多半正是为了纠正认错的名字 —— 报成功等于骗他。
                    _msg = ("绑定失败：ID 不存在或取不到 bgm 数据" if not ok
                            else ("已绑定，但这一部有【已归档】的文件搬不动 —— 片名/目录保持原样，只换了 bgm 与封面简介。要一并改名：先在 qB 里重新添加那些种子（或手动移动文件），再绑一次" if _rep.get("frozen") else "已绑定并识别 ✓"))
                    ui.notify(_msg, type=("negative" if not ok
                                          else "warning" if _rep.get("frozen") else "positive"))
                    if ok:
                        await maybe_relocate_anime(anime_id, old_path, refresh_all)
                await busy_action(e.sender, f"bind:{anime_id}", _go,
                                  fail="绑定失败")
            return h

        def _refail(anime_id):
            async def h(e):
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
                await busy_action(e.sender, f"refail:{anime_id}", _go,
                                  fail="识别失败")
            return h

        async def _download_all(e):
            # 【补下全部要防重入】它可能跑几十秒，而同文件 758 行的『重新识别』早就有现成写法。
            # 连点会叠出多轮并发补下：同一集被多轮各挑一次候选，qB 侧靠 409 兜着，
            # 但日志与"已触发 N 集"的数字会变得没法解释。
            async def _go():
                n = await anime.download_all_pending()
                refresh_all()   # 含 manage_panel，好让季度小结的已下/待下计数也即时更新
                ui.notify(f"已触发补下 {n} 集", type="positive")
            await busy_action(e.sender, "download-all", _go, fail="补下失败")

        async def _qb_sync_now(e):
            """『下载状态』的立刻刷新：主动向 qB 拉一次在下种子的进度，不必等后台轮询（默认间隔可能上千秒）。

            【走 busy_action】(R36 体检 ux-2) 这个按钮被按下的典型时机恰恰是"我觉得 qB 那边不对劲"，
            也就是最可能走满 `_QB_TOTAL_TIMEOUT`（45 秒）的那一次 —— 以前从头到尾没碰过按钮元素，
            同一行另外两个按钮都有反馈，只有它是 45 秒死键。异常也交给 busy_action 兜（fail=同步失败）。
            """
            if not config.QB_ENABLED:
                ui.notify("qB 未启用（去设置页开启『发送种子到 qB』）", type="warning")
                return
            if engine.sync_busy(AnimeTorrent):
                # 后台那一轮正在跑。不排队等它：sync 对"qB 里查不到的种子"要连续两轮 miss 才判死，
                # 两轮压在一起会把宽限窗口抹平（见 engine._sync_locks）。这里直接告诉用户"已经在同步了"。
                ui.notify("已有一轮同步在跑，稍等片刻再看", type="info")
                return

            async def _go():
                n = await anime.sync_qb_status(manual=True)
                _refresh_overview(charts=False)              # 刷计数区，不重建环图（保住图例排除态）
                inflight_panel.refresh()                     # 『正在下载』列表的进度条也跟着更新
                if n is None:
                    # 【None ≠ 0】没能问到 qB（连不上/超时）。busy_action 的 except 接不住它：
                    # 整条链从头到尾不抛异常（理由见 engine.sync_qb_status 的三态说明）。
                    ui.notify("没能问到 qB（连不上或超时），本次一行都没改。"
                              "检查设置页的 qB 地址与账号密码，或看日志页。", type="negative")
                else:
                    ui.notify(f"已同步 {n} 条种子的进度" if n else "没有正在下载的种子",
                              type="positive")
            # 先一句 toast 再转圈（核实意见）：这个按钮所在的面板每 30 秒被定时器重建，loading 只活到下一个
            # tick；能盖住整段 45 秒的是 toast —— 与同页『刷新资料』的写法一致。
            ui.notify("正在向 qB 拉最新进度…（qB 不通时最长约 45 秒）", type="info")
            await busy_action(e.sender, "qb-sync-anime", _go, fail="同步失败")

        _reident_busy = {"v": False}   # 防抖：整轮 bgm 遍历可能跑几分钟，期间连点会叠出多轮并发请求

        def _reident(seasons):
            async def h():
                if _reident_busy["v"]:
                    ui.notify("正在刷新资料中，请稍候…", type="info")
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

        def _card_cover(a, *, dim: bool = False, status=None, sources=None, prog=None, missing=None):
            """番剧卡片的封面区（番剧表/待确认/已忽略共用），四角徽标的摆法与剧场版同一份（layout.cover_box）：
              · 左上：状态 status=(文案, 颜色, 悬停说明或 None)，下面叠『已下齐』；
              · 右上：季号，下面叠 bgm 判定的非 TV 类型（WEB/剧场版/OVA…）；
              · 左下：多源/单源（悬停看来源）；右下：已下/可下（只有番剧表给 prog；missing＝缺集，见下）。
            dim：封面置灰（番剧表里的已忽略番）。返回容器，调用方可再挂点击。"""
            fin = None
            if a.finished_at:
                # 开了『下齐后停止自动下新集』时写明停订：那是行为变化，不写的话它和正常追番的番
                # 长得一样，用户只会觉得"它好久没更新了"。说明与详情页同一份（layout.finished_tip）。
                fin = ("已下齐·停订" if config.ANIME_FINISH_UNSUB else "已下齐", "teal",
                       finished_tip(a, "进详情页点『继续订阅』"))
            sl = season_label(a)
            # 【已下/可下】分子＝已发给 qB 的不同正集号数，分母＝库里见过的不同正集号数。
            # 按【集号】数而不是按种子条数：同一集常有好几个源各一份，按条数数出来的比值没有意义。
            #   · 分子 < 分母 → 有集号还没到手（锁定源锁太死 / 关键词没匹配上 / 卡在缓冲窗口）
            #   · 分母 > bgm 记的总集数 → 集号本身就不对（别的季的正片挂到了这条记录下面）
            # 【缺集】missing＝按开播日推算应已播出、库里却一条种子都没见过的集（core.anime.missing_episodes）。
            # 有缺集时分母改成"应有集数"＝库里见过的 + 缺的，这样 7/7 会显示成 7/12——
            # 否则采集停过、锁定源断更漏下的集永远看不出来（比值是满的、还是蓝色）。
            progress = None
            if prog:
                _have, _seen = prog
                _tot = a.total_episodes or 0
                _over = bool(_tot and _seen > _tot)
                _den = _seen + len(missing or ())
                # 红＝集号超出 bgm 记载、橙＝缺集或还有集没到手、蓝＝正常（都到手了）
                _c = "red" if _over else ("orange" if _have < _den else "blue")
                if missing:
                    _tip = (f"已下 {_have} 集 / 应有 {_den} 集：缺第 {ep_ranges_text(missing)} 集"
                            "——按 bgm 开播日推算早该播了，库里却一条种子都没有。"
                            "采集停过一阵（RSS 只留最近几天）或锁定的源中途断更都会这样；"
                            "进详情页把『下载源』换成还在更新的组，再点『补齐该源』")
                else:
                    _tip = (f"已下 {_have} 集 / 库里有 {_seen} 集的种子"
                            + (f"；bgm 记 {_tot} 集" if _tot else "；bgm 未给总集数")
                            + ("。集号超出 bgm 记载——多半有别的季的正片挂到了这条记录下面，"
                               "进详情页看『各源的集号范围』" if _over else
                               ("。还有集号没到手：可能是锁定源锁太死、版本关键词没匹配上，"
                                "或还在缓冲窗口里" if _have < _seen else "。都到手了")))
                progress = (f"{_have}/{_den}", _c, _tip)
            return cover_box(a.cover_url, dim=dim,
                             top_left=[status, fin],
                             top_right=[(sl, "purple", None) if sl else None, platform_spec(a)],
                             bottom_left=[sources_spec(sources)],
                             bottom_right=[progress])

        def _card_title(a, struck: bool = False):
            """番剧卡片上的番名（layout.cover_title）。struck：灰色删除线（已忽略）。"""
            return cover_title(name_of(a), struck)

        def _anime_card(a, sources=None, prog=None, missing=None):
            # 一部番一张卡：封面（四角徽标）+ 番名。整张卡可点进详情（徽标的 tooltip 照常悬停可看）。
            status = (("已忽略", "grey", None) if a.rejected else
                      ("待确认", "orange", None) if not a.confirmed else None)
            with ui.card().tight().classes("cover-card cursor-pointer w-full").on(
                    "click", lambda aid=a.id: open_detail(aid)):
                _card_cover(a, dim=a.rejected, status=status, sources=sources, prog=prog, missing=missing)
                with ui.column().classes("px-3 py-2 gap-1.5 w-full"):
                    _card_title(a, struck=a.rejected)

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
            "manage": _build_manage_tab, "confirm": confirm_panel,
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

        # 【immediate=False】(R36 体检) NiceGUI 的 ui.timer 默认 immediate=True：回调在浏览器 socket 握手
        # 那一刻就跑一次，而页面内容几十毫秒前刚构建完 —— 实测那一次把 34 条 SQL / 157 ms 的仪表盘
        # 逐字重建了第二遍（前后 DOM 归一后完全相同）。首屏由构建给，定时器只管之后的 30 秒一刷。
        ui.timer(30.0, refresh_timer, immediate=False)  # 只刷当前可见 tab 的实时区（见 refresh_timer）
