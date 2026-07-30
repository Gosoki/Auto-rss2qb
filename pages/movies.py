"""OVA・剧场版页 `/movies`：仿番剧那边的标签布局 —— 仪表盘 / 列表 / 待识别 / 已忽略 / 订阅源。

剧场版数据（Movie/MovieTorrent）与 TV 番剧完全分离，逻辑在 movies.py；本页只管展示与交互。
剧场版整个列表本身就是『待人工下载』，故不设『待确认』；订阅源=固定的 Mikan 季度扫描（非 RSS 订阅）。
"""
from datetime import datetime

from nicegui import ui

import config
from db.models import MovieTorrent
from core import engine, movies as mov, worker
from sources.parse import SEASON_CN
from .layout import (WEEKDAY_CN, barline, confirm, expand_collapse_bar, frame,
                     human_size, kpi_cards, live_status, meta_card,
                     name_of, paginate, parse_bgm_id, qb_live_text,
                     recent_table, torrent_status_cn, warn_banner)

# 同 pages/anime.py：{键: 显示名} 只留这一份，设置页的下拉与本页的键集合都从它来
_TABS_DEF = (("overview", "仪表盘", "dashboard"), ("list", "列表", "movie"),
             ("fail", "待识别", "sync_problem"), ("reject", "已忽略", "block"),
             ("sources", "订阅源", "rss_feed"))
MOVIE_TAB_LABELS = {k: name for k, name, _ in _TABS_DEF}
_TABS = tuple(MOVIE_TAB_LABELS)


def _mov_live_status(*a):
    """剧场版把库态 pending 的『待下』显示为蓝色『可下载』（与影片页术语/配色统一）；其余同 live_status。"""
    text, color = live_status(*a)
    return ("可下载", "blue") if text == "待下" else (text, color)


def _group_by_year_quarter(items):
    """两级分组：先按年份、年内再按季度。季度 key 形如 '26C'（前两位=年）。
    返回 [(年份2位, [(季度, [movie...]), ...]), ...]，年份倒序、季度倒序、未知垫底。"""
    from collections import defaultdict
    by_year: dict = defaultdict(lambda: defaultdict(list))
    for it in items:
        q = it.quarter or "未知"
        year = q[:2] if (q != "未知" and len(q) >= 2 and q[:2].isdigit()) else "未知"
        by_year[year][q].append(it)
    out = []
    for year in sorted(by_year, key=lambda y: (1, 0) if y == "未知" else (0, -int(y))):
        qs = sorted(by_year[year], reverse=True)
        if "未知" in qs:                       # 未知季度垫到年内最后
            qs.remove("未知"); qs.append("未知")
        out.append((year, [(q, by_year[year][q]) for q in qs]))
    return out


def _season_toggle_btn(key: str, name: str, selected: set) -> None:
    """季度点选按钮：在 selected 集里=填充蓝，不在=描边灰；点击切换。比多选下拉直观，不用展开。"""
    b = ui.button(name)

    def _restyle():
        if key in selected:
            b.props(remove="outline").props("unelevated color=primary")
        else:
            b.props(remove="unelevated").props("outline color=blue-grey")

    def _toggle():
        selected.discard(key) if key in selected else selected.add(key)
        _restyle()

    b.on("click", _toggle)
    _restyle()


def _notify_relocate_movie(rep):
    """把 relocate_movie 结果转成用户提示（对齐番剧 _notify_relocate）。"""
    if rep.get("error"):
        ui.notify(f"移动失败：{rep['error']}", type="negative")
        return
    parts = []
    if rep.get("moved"):
        parts.append(f"已移动 {rep['moved']} 个版本到新目录")
    if rep.get("redownload"):
        parts.append(f"{rep['redownload']} 个版本 qB 未跟踪/连不上 → 已清状态待重下到新目录")
    if rep.get("failed"):
        parts.append(f"{rep['failed']} 个版本移动被拒（{rep.get('fail_code')}：新目录不可写，未改动）")
    if rep.get("stalled_kept"):   # 停滞版本有意不动：保留停滞标记与删除入口，但文件没跟着走，要说清楚
        parts.append(f"{rep['stalled_kept']} 个版本处于停滞、未移动（保留停滞标记）")
    if rep.get("delivering"):     # 正在交付给 qB 的版本：路径在交付前就定死了，搬不了也不能碰
        parts.append(f"{rep['delivering']} 个版本正在交付给 qB、未移动"
                     "（它会先落到旧目录，交付完成后再点一次即可搬过来）")
    msg = "；".join(parts) or "无需移动"
    warn = bool(rep.get("redownload") or rep.get("failed")
                or rep.get("stalled_kept") or rep.get("delivering"))
    if (rep.get("redownload") or rep.get("stalled_kept")) and rep.get("old_path"):
        msg += f"。⚠️ 旧文件在 {rep['old_path']} 需你手动清理"
    ui.notify(msg, type="warning" if warn else "positive")


async def _maybe_relocate_movie(movie_id, old_path, refresh_cb):
    """重绑/重识别改了季度后：归档目录变了且有已下版本就问是否搬迁，并按结果提示（对齐番剧）。"""
    new_path = mov.movie_save_path(movie_id)
    if not new_path or new_path == old_path:
        return   # 路径没变，无需移动
    # 与 relocate_movie 选行同谓词：HAVE 且【未归档】（已归档的不在 qB、移不动，
    # 算进来会导致『说要搬 N 个 → 点确认 → 报"无需移动"』）
    _ts = mov.movie_torrents(movie_id)
    dl = [t for t in _ts if t.status in engine.HAVE_STATUSES and not t.archived_at]
    arch = [t for t in _ts if t.status in engine.HAVE_STATUSES and t.archived_at]
    if not dl:
        if arch:
            ui.notify(f"归档目录已更新。但有 {len(arch)} 个版本已归档（不在 qB，无法代为移动），"
                      f"其文件仍在旧目录 {old_path or '原位置'}，需你手动处理", type="warning")
        else:
            ui.notify("归档目录已更新（无已下文件，新版本将下到新目录）", type="positive")
        return
    _extra = f"；另有 {len(arch)} 个版本已归档、无法代为移动（文件留在旧目录）" if arch else ""
    if not await confirm("归档目录变了，移动已下文件？",
                         f"{len(dl)} 个版本已下，移到新目录：{new_path}{_extra}",
                         ok_label="移动文件", ok_icon="drive_file_move"):
        ui.notify("已更新记录，未移动文件（新版本将下到新目录，旧文件留在原处）", type="warning")
        return
    rep = await mov.relocate_movie(movie_id, old_path)
    refresh_cb()
    _notify_relocate_movie(rep)


def render_movie_detail(movie_id: int, refresh_outer=None, on_close=None) -> None:
    """把某剧场版详情渲染进当前容器：元信息 + 版本列表（逐条下/删）+ 识别/忽略。
    on_close：非空则在标题行右侧渲染 X 关闭键（关掉外层 dialog）。"""
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
        # 标题行：标题+标签塞进左侧可换行容器；外层 no-wrap + X shrink-0 → X 永远钉右上角，窄屏先挤标签换行
        with ui.row().classes("items-start gap-2 w-full no-wrap"):
            with ui.row().classes("items-center gap-2 flex-wrap grow min-w-0"):
                ui.label(name_of(cur)).classes("text-2xl font-bold")
                ui.button(icon="edit", on_click=_bind).props("flat round dense size=sm color=primary").tooltip(
                    "认错了？手动绑定正确的 bgm（粘链接或 ID）")
                ui.badge(cur.mikan_type or "剧场版").props("color=deep-purple")  # Mikan 桶判定
                if cur.rejected:
                    ui.badge("已忽略").props("color=grey")
                elif not cur.bangumi_id:
                    ui.badge("未识别").props("color=red")
            if on_close:
                ui.button(icon="close", on_click=on_close).props("flat round dense").classes(
                    "shrink-0")

        wd = f"  {WEEKDAY_CN[cur.air_weekday]}" if cur.air_weekday is not None else ""
        meta_card(cur.cover_url, [
            ("日文名", cur.jp_name),
            ("片长", cur.duration),
            ("季度", engine.quarter_label(cur.quarter) if cur.quarter else "—"),
            ("放送", f"{cur.air_date or '—'}{wd}"),
            ("类型", cur.platform),
            ("原作", cur.author),
            ("导演", cur.director),
            ("音乐", cur.music),
            ("声优", cur.cast),
            ("来源", " · ".join(sources) or "—"),
        ], cur.bangumi_id, cur.summary, rating=cur.rating)

        with ui.row().classes("items-center gap-3 flex-wrap"):
            ui.button("重新识别", icon="refresh", on_click=_enrich).props(
                "flat dense size=sm").style("font-size:12px")
            if cur.rejected:
                ui.button("恢复订阅", icon="undo", on_click=_restore).props(
                    "dense size=sm color=primary unelevated").style("font-size:12px")
            else:
                ui.button("忽略", icon="block", on_click=_reject).props(
                    "flat dense size=sm color=grey").style("font-size:12px")
        ui.label(f"版本 / 种子（{len(ts)}）").classes("text-sm font-bold mt-2")
        if not ts:
            ui.label("（还没有种子）").classes("text-gray-400")
            return
        for t in ts:
            with ui.column().classes("w-full gap-1 py-1").style(
                    "border-bottom:1px solid rgba(255,255,255,.08)"):
                with ui.row().classes("items-center gap-2 w-full text-sm"):
                    ui.label(engine.torrent_time(t)).classes("shrink-0 text-gray-400 text-xs")
                    ui.label(t.raw_title or t.source).classes("grow break-all")
                with ui.row().classes("items-center gap-2 flex-wrap"):   # 状态标签 + 下载：统一左下
                    live = qb_live_text(t)
                    if live:  # 完成(做种/100%)才绿，下载中用蓝
                        _done = (t.qb_progress or 0) >= 1
                        ui.badge(live).props(f"color={'green' if _done else 'blue'}").tooltip(
                            "qB 实时状态")
                    else:  # 无 qB 实时态：刚交付未同步→下载中；其余按状态
                        # 失败原因挂在徽标 tooltip 上。剧场版【没有】番剧那套自动退避重发（core/movies.py
                        # 的论证正是"失败时人就在跟前，给个能看的 fail_reason 比排队重发有用"）——
                        # 而这个字段原本任何页面都不显示，那句论证在 UI 上是落空的：qB 连不上时状态还会
                        # 落回 pending，列表只显示『待下』，用户完全看不出点过一次且失败了。
                        _b = ui.badge(torrent_status_cn(t.status, t.qb_progress, t.qb_synced_at)).props(
                            "color=blue-grey")
                        if t.fail_reason:
                            _b.props("color=orange").tooltip(
                                f"上次失败：{t.fail_reason}（剧场版不自动重发，要重试就点右边『下载』）")
                    _vdl = ui.button("下载", icon="download", on_click=_force(t.id)).props(
                        "size=sm flat dense").style("font-size:12px")
                    _vdl.set_enabled(config.QB_ENABLED)
                    _vdl.tooltip("强制下这一版本到文件夹" if config.QB_ENABLED
                                 else "qB 未启用，去设置页开启后可下载")
                    if t.status in engine.HAVE_STATUSES:  # 下过/在下/停滞(盘上有文件)都可删（delete 函数亦接受 stalled）
                        ui.button(icon="delete_forever",
                                  on_click=_del(t.id, t.archived_at, t.save_path)).props(
                            "size=sm flat dense color=negative").tooltip(
                            "已归档：不在 qB，只能标记已删，文件需你手动清理" if t.archived_at
                            else "删除这一版本的文件（qB+硬盘，不可撤销）")
                    if t.status in engine.DOWNLOADABLE_STATUSES:   # 未下载的可直接排除
                        ui.button("排除", icon="block", on_click=_exclude(t.id)).props(
                            "size=sm flat dense color=grey").style("font-size:12px").tooltip(
                            "不想要这版本：从可下排除（不删文件，只改状态；可恢复）")
                    if t.status == "excluded":   # 已排除：给『恢复』放回可下
                        ui.button("恢复", icon="undo", on_click=_unexclude(t.id)).props(
                            "size=sm flat dense color=primary").style("font-size:12px").tooltip("放回可下")

    def _after():
        body.refresh()
        if refresh_outer:
            refresh_outer()

    async def _enrich():
        old_path = mov.movie_save_path(movie_id)
        ok = await mov.enrich_movie(movie_id)
        _after()
        ui.notify("识别成功" if ok else "未识别到（可粘贴 bgm 链接绑定）")
        if ok:
            await _maybe_relocate_movie(movie_id, old_path, _after)

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

    def _del(mt_id, archived=None, save_path=""):
        async def h():
            if archived:   # 已归档：种子早已移出 qB，删不到文件 → 明确告知路径，让用户自己清理
                if not await confirm(
                        "这一版本已归档，只能标记已删",
                        f"它已从 qB 移除，本工具删不到文件。确认后仅标记为『已删』。\n\n"
                        f"⚠️ 文件仍在（需你手动删）：{save_path or '（未记录路径，请到下载目录自行查找）'}\n\n"
                        f"想连文件一起删：先点『下载』把它重新挂回 qB，再删。",
                        ok_label="标记已删", ok_icon="delete_forever"):
                    return
            elif not await confirm("删除这一版本的文件？",
                                   "通过 qB 连同硬盘文件一起删除，不可撤销。",
                                   ok_label="删除文件", ok_icon="delete_forever"):
                return
            ok = await mov.delete_movie_torrent(mt_id)
            _after()
            if ok and archived:
                ui.notify(f"已标记为已删；文件仍在 {save_path or '下载目录'}，需手动清理", type="warning")
            else:
                ui.notify("已删除该版本文件" if ok else "没删成（qB 未连上或无文件）",
                          type="positive" if ok else "warning")
        return h

    def _exclude(mt_id):
        async def h():
            if not await confirm("排除这一版本？",
                                 "从可下里排除：不再显示为可下、RSS 再遇到同种子也不重收；可随时点『恢复』放回。",
                                 ok_label="排除", ok_icon="block"):
                return
            ok = mov.exclude_torrent(mt_id)
            _after()
            ui.notify("已排除" if ok else "排除失败（已下载的用『删除文件』）",
                      type="positive" if ok else "warning")
        return h

    def _unexclude(mt_id):
        def h():
            mov.unexclude_torrent(mt_id)
            _after()
            ui.notify("已恢复到可下")
        return h

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
                          on_click=lambda: dlg.submit(inp.value)).props("color=primary unelevated")
        val = await dlg
        if not val:
            return
        bid = parse_bgm_id(val)
        if bid is None:
            ui.notify("请粘贴 bgm 链接或数字 ID", type="warning")
            return
        old_path = mov.movie_save_path(movie_id)
        ok = await mov.bind_movie_bgm(movie_id, bid)
        _after()
        ui.notify("已绑定并识别 ✓" if ok else "绑定失败：ID 不存在或取不到 bgm 数据",
                  type="positive" if ok else "negative")
        if ok:
            await _maybe_relocate_movie(movie_id, old_path, _after)

    body()


@ui.page("/movies")
def movies_page(t: str = ""):
    """t = 当前 tab（写在 URL ?t= 里），刷新后停在同一 tab。
    默认空串：顶栏导航进来（无 ?t=）时留给下面按 MOVIE_DEFAULT_TAB 决定默认停哪个 tab。"""
    with frame("movies"):
        list_page = {"n": 1, "expand": None}  # 剧场版列表：分页页码 + 一键展开/收起意图（None=默认全开）
        detail_dlg = ui.dialog()

        def _list_goto(e):
            list_page["n"] = int(e.value)
            list_panel.refresh()

        def open_detail(movie_id):
            detail_dlg.clear()
            with detail_dlg, ui.card().classes("w-full").style("max-width:860px"):
                render_movie_detail(movie_id, refresh_outer=refresh_all, on_close=detail_dlg.close)
            detail_dlg.open()

        list_dlg = ui.dialog()   # KPI『失败』点开的种子清单（对齐番剧页）

        def _open_failed():
            """失败 KPI 点开：列出失败/停滞的版本，点片名进详情页补下重试 / 忽略。"""
            list_dlg.clear()
            rows = mov.failed_rows()
            with list_dlg, ui.card().classes("w-full").style("max-width:720px"):
                ui.label(f"失败 · {len(rows)}").classes("text-base font-bold")
                ui.label("下载失败过的版本（取种/发送失败）。点片名进详情页补下重试 / 忽略。").classes(
                    "text-xs text-gray-400")
                if not rows:
                    ui.label("（空）").classes("text-gray-500 p-2")
                for r in rows:
                    with ui.column().classes("gap-0 w-full py-1").style(
                            "border-bottom:1px solid rgba(255,255,255,.08)"):
                        ui.label(r["name"]).classes(
                            "text-sm text-blue-400 cursor-pointer hover:underline").on(
                            "click", lambda mid=r["movie_id"]: open_detail(mid))  # 不关本弹窗，详情叠上面
                        ui.label(r["raw"] or "—").classes("text-xs text-gray-500 break-all")
                ui.button("关闭", on_click=list_dlg.close).props("flat")
            list_dlg.open()

        # ---- 事件 ----
        async def _scan(year_in, sel):
            yr = int(year_in.value or datetime.now().year)
            letters = [k for k in SEASON_CN if k in sel]   # 按 A/B/C/D 顺序取选中的
            if not letters:
                ui.notify("至少选一个季度", type="warning")
                return
            ui.notify(f"扫描 {yr} 年 {len(letters)} 个季度的剧场版/OVA…（走 Mikan+bgm，请稍候）")
            # 走 worker 那把 _scan_lock 而不是页面自己的标志：一次全年扫描是几分钟的 Mikan+bgm 串行请求，
            # 而页面级防抖只在【本浏览器标签】里有效——挡不住后台自动扫描那一轮，也挡不住第二个标签页。
            # 并发跑两轮不只是请求翻倍：_upsert_movie 是"先查后插"、mikan_id 上没有唯一约束，
            # 两轮交错时未识别的番组会留下两行重复 Movie。
            res = await worker.scan_movies_now(yr, letters)
            if res is None:
                ui.notify("已有一轮剧场版扫描在跑（后台自动扫描或另一处触发），本次跳过", type="warning")
                return
            refresh_all()
            tail = f"，{res['errors']} 个出错" if res["errors"] else ""
            ui.notify(
                f"扫描完成：命中 {res['seen']} 部，新增 {res['movies']}，种子 {res['torrents']}{tail}",
                type="positive")

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

        def _redownload(mt_id):
            async def h():   # 重下一条已删除的剧场版种子，找回文件（download_movie_torrent 对 deleted 会重新交 qB）
                ok = await mov.download_movie_torrent(mt_id)
                refresh_all()
                ui.notify("已重新下载" if ok else "重新下载失败（qB 未连上 / 种子不可用）",
                          type="positive" if ok else "warning")
            return h

        def _unexclude_mv(mt_id):
            def h():   # 取消排除：把 excluded 放回可下
                mov.unexclude_torrent(mt_id)
                refresh_all()
                ui.notify("已恢复到可下")
            return h

        def _bind_from_input(movie_id, inp):
            async def h():
                bid = parse_bgm_id(inp.value or "")
                if bid is None:
                    ui.notify("请粘贴 bgm 链接或数字 ID", type="warning")
                    return
                old_path = mov.movie_save_path(movie_id)
                ok = await mov.bind_movie_bgm(movie_id, bid)
                refresh_all()
                ui.notify("已绑定并识别 ✓" if ok else "绑定失败：ID 不存在或取不到 bgm 数据",
                          type="positive" if ok else "negative")
                if ok:
                    await _maybe_relocate_movie(movie_id, old_path, refresh_all)
            return h

        def _refail(movie_id):
            async def h():
                old_path = mov.movie_save_path(movie_id)
                ok = await mov.enrich_movie(movie_id)
                refresh_all()
                ui.notify("识别成功 ✓" if ok else "还是没识别到（可粘贴 bgm 链接绑定）",
                          type="positive" if ok else "warning")
                if ok:
                    await _maybe_relocate_movie(movie_id, old_path, refresh_all)
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

        def _movie_card(m, ts):
            # "已下 N" = 这部片有几个版本已经落到盘上（含停滞的半成品）——与同页搬迁框的
            # "{n} 个版本已下"(87 行)、详情页"删除文件"按钮的门槛(178 行) 同集合，三者可互相校验。
            # 不用 TRACKED：sent→stalled 只是引擎停止轮询的内部决定，盘上文件一点没变，
            # 若跟着 TRACKED 走，这个数字会无缘无故从 1 掉到 0，还把占空间的半成品藏起来。
            ndone = sum(1 for t in ts if t.status in engine.HAVE_STATUSES)
            srcs = sorted({t.source for t in ts if t.source})
            with ui.card().classes("w-full"):
                with ui.row().classes("gap-3 items-start no-wrap w-full"):
                    if m.cover_url:
                        ui.image(m.cover_url).classes("rounded").style("min-width:4rem;width:4rem")
                    with ui.column().classes("gap-1 grow min-w-0"):
                        with ui.row().classes("items-center gap-2 flex-wrap"):
                            ui.badge(m.mikan_type or "剧场版").props("color=deep-purple")  # Mikan 桶判定
                            ui.label(name_of(m)).classes(
                                "text-lg cursor-pointer text-blue-400 hover:underline font-bold").on(
                                "click", lambda mid=m.id: open_detail(mid))
                            if not m.bangumi_id:
                                ui.badge("未识别").props("color=red").tooltip("bgm 没匹配上，去『待识别』手动绑定")
                        with ui.row().classes("gap-4 text-xs text-gray-400 flex-wrap"):
                            ui.label(f"放送 {m.air_date or '—'}")
                            ui.label(f"版本 {len(ts)}")
                            ui.label(f"已下 {ndone}")
                            ui.label("来源 " + (" · ".join(srcs) or "—"))
                    with ui.column().classes("gap-1 items-end shrink-0"):
                        ui.button("下载", icon="download",
                                  on_click=lambda mid=m.id: open_detail(mid)).props(
                            "color=primary unelevated").tooltip("打开详情，自己挑版本下载")
                        ui.button("忽略", icon="block", on_click=_reject(m.id)).props(
                            "flat color=grey")

        # ---- 面板 ----
        @ui.refreshable
        def overview_panel():
            ov = mov.overview()
            k = ov["kpi"]
            # 影片维度四卡（粉字）与种子维度三卡（绿字）分组；数字保持语义色；待识别/已忽略点击跳 tab
            kpi_cards([("电影", k["total"], "", None),
                       ("已识别", k["matched"], "", None),
                       ("待识别", k["unmatched"], "red", lambda: tabs.set_value("fail")),
                       ("已忽略", k["rejected"], "", lambda: tabs.set_value("reject")),
                       "|",
                       # 这一组是【种子/版本维度】：全用种子数，别混影片数（原来这张是"有已下版本的片数"，
                       # 与同组其它三张不同量纲，同页还有另外两个口径不同的『已下』）
                       ("已交付", ov["status"]["sent"], "green", None),
                       ("可下载", ov["status"]["pending"], "blue", None),
                       ("失败", ov["status"]["error"], "red", _open_failed),
                       ("版本", k["versions"], "", None)])
            if not ov["config"]["qb"]:
                warn_banner("qB 未启用：剧场版也只采集不下载（设置页开 QB_ENABLED 后生效）")

            # ── 各季度（电影数）──
            ui.label(f"各季度（电影数）· {len(ov['by_quarter'])}").classes("text-sm font-bold mt-3 pl-1")
            with ui.column().classes("w-full gap-0 pl-1"):
                maxv = max((tot for _, tot, _ in ov["by_quarter"]), default=1) or 1
                if not ov["by_quarter"]:
                    ui.label("—").classes("text-gray-500 text-sm")
                for qk, tot, _ in ov["by_quarter"]:
                    barline(engine.quarter_label(qk), tot, maxv, lw="w-36", text=f"{tot}")

            # ── 种子状态 ──（剧场版逐版本人工下，无待确认/首选/备用概念，只列库态计数）
            with ui.row().classes("items-center gap-2 mt-3 pl-1 flex-wrap"):
                ui.label("种子状态").classes("text-sm font-bold")
                ui.label("各状态种子计数").classes("text-xs text-gray-400")
            chips = [
                ("已交付", ov["status"]["sent"], "green",
                 "已发送给 qB 的版本数（含仍在下的）。真正下完的看下方『已完成』"),
                ("可下载", ov["status"]["pending"], "blue", "还没下的版本，进详情逐条下"),
                ("跳过数", ov["status"]["skipped"], "blue-grey",
                 "已忽略剧场版留下的种子；恢复时若一版都没下过会放回可下载"),
                ("失败数", ov["status"]["error"], "red", "下载出错的版本"),
            ]
            # 少见状态只在非零时露出（与番剧侧同口径）：保证各 chip 之和 == 种子数，又不让全新库挂一排 0
            for _lb, _key, _cl, _tip in (
                ("下载中", "downloading", "blue", "正在交付给 qB 的瞬时态（通常一闪而过）"),
                ("停滞", "stalled", "deep-orange", "已交付但长期零推进，脱离轮询、等人工处理"),
                ("已删除", "deleted", "grey", "人工删过的版本"),
                ("已排除", "excluded", "grey", "人工排除的版本（不再参与下载）"),
            ):
                if ov["status"].get(_key):
                    chips.append((_lb, ov["status"][_key], _cl, _tip))
            chips.append(("种子数", k["versions"], "blue-grey", "全部种子/版本数（各状态之和）"))
            with ui.row().classes("gap-2 flex-wrap pl-1 items-center"):
                for label, val, color, tip in chips:
                    b = ui.badge(f"{label} {val}").props(f"color={color}").classes("text-sm")
                    if tip:
                        b.tooltip(tip)
            # ── 下载状态 ──（qB 实时态，独立成区；与番剧页同款：后台轮询之外可点『立刻刷新』手动拉一次）
            # qB 未启用也照常显示：qb_summary 只查库、不打 qB 接口，此时列的是最后已知进度，只把手动同步按钮禁掉。
            q = ov["qb"]
            _qb_on = ov["config"]["qb"]
            with ui.row().classes("items-center gap-2 mt-3 pl-1 flex-wrap"):
                ui.label("下载状态").classes("text-sm font-bold")
                ui.label("qB 实时下载进度" if _qb_on else "qB 未启用·最后进度").classes(
                    "text-xs text-gray-400")
                _sync = ui.button("立刻刷新", icon="sync", on_click=_qb_sync_now).props(
                    "outline color=primary size=sm").style("font-size:12px")
                _sync.set_enabled(_qb_on)
                _sync.tooltip("立即向 qB 拉一次最新进度（不必等后台轮询）" if _qb_on
                              else "qB 未启用，去设置页开启后可同步")
            with ui.row().classes("gap-2 flex-wrap pl-1 items-center"):
                ui.badge(f"已完成 {q['completed']}").props("color=green").classes("text-sm")
                ui.badge(f"下载中 {q['downloading']}").props("color=blue").classes("text-sm")
                ui.badge(f"已跟踪 {q['tracked']}").props("color=blue").classes("text-sm").tooltip(
                    "qB 里正在跟踪的种子数（已交付给 qB 的）")
                if q["dlspeed"]:
                    ui.badge(f"↓ {human_size(q['dlspeed'])}/s").props("color=blue").classes("text-sm")
                ui.badge(f"完成率 {q['avg_progress'] * 100:.0f}%").props("color=blue-grey").classes(
                    "text-sm").tooltip("已交付种子的平均完成度")

            # ── 采集状态 ──（剧场版=Mikan 季度扫描；开关/间隔在『订阅源』tab 调）
            with ui.row().classes("items-center gap-2 mt-3 pl-1 flex-wrap"):
                ui.label("采集状态").classes("text-sm font-bold")
                ui.label("后台采集与识别").classes("text-xs text-gray-400")
            with ui.row().classes("gap-2 flex-wrap pl-1"):
                ui.badge("扫描开启" if config.MOVIE_SCAN_ENABLED else "扫描暂停").props(
                    f"color={'green' if config.MOVIE_SCAN_ENABLED else 'red'}").classes("text-sm").tooltip(
                    "后台是否定期扫 Mikan 剧场版/OVA 桶（『订阅源』页切换）")
                ui.badge(f"识别 {k['matched']}/{k['total']}").props("color=blue").classes("text-sm").tooltip(
                    "已匹配到 bgm 的 / 全部")
                ui.badge(f"扫描间隔 {config.MOVIE_SCAN_INTERVAL // 3600}h").props(
                    "color=blue-grey").classes("text-sm")
                ui.badge(f"上次 {config.MOVIE_SCAN_LAST or '从未'}").props(
                    "color=blue-grey").classes("text-sm")

        @ui.refreshable
        def inflight_panel():
            rows = mov.inflight_movie_rows()
            _inflight_total = engine.inflight_count(MovieTorrent)
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
                        text, color = _mov_live_status(r["status"], r["qb_state"], r["qb_progress"],
                                                       r["qb_synced_at"], r["qb_dlspeed"])
                        # 同番剧侧：no-wrap + 标题 grow/min-w-0 + 徽标 shrink-0，徽标恒在右
                        with ui.row().classes("items-center gap-3 w-full text-sm py-1 no-wrap").style(
                                "border-bottom:1px solid rgba(255,255,255,.08)"):
                            ui.label(r["name"]).classes("grow break-all min-w-0")
                            ui.badge(text).props(f"color={color}").classes("shrink-0 text-sm")

        @ui.refreshable
        def recent_panel():
            ui.label("新入库（最近 50 条种子）").classes("text-sm font-bold mt-4 pl-1")
            rows = []
            for r in mov.recent_movie_rows(50):
                text, color = _mov_live_status(r["status"], r["qb_state"], r["qb_progress"],
                                               r["qb_synced_at"], r["qb_dlspeed"])
                rows.append({
                    "id": r["id"],
                    "detail_id": r["movie_id"],
                    "time": r["time"],
                    "name": r["name"],
                    "src": r["source"],
                    "raw": r["raw"] or "—",
                    "status": text,
                    "status_color": color,
                })
            recent_table(rows, "剧场版",
                         on_row_click=lambda row: row.get("detail_id") and open_detail(row["detail_id"]))

        @ui.refreshable
        def list_panel():
            # 今年 / 上年 小结（数字大、标签小；零值项变灰）—— 仿番剧表的当季/上季小结，剧场版按年
            with ui.row().classes("w-full gap-3 flex-wrap mb-3 items-stretch"):
                for b in mov.year_brief():
                    stats = [
                        (b["matched"], "已识别", "text-blue-400"),
                        (b["fail"], "待识别", "text-red-400" if b["fail"] else "text-gray-500"),
                        (b["ignored"], "已忽略", "text-gray-400" if b["ignored"] else "text-gray-500"),
                    ]
                    with ui.card().classes("gap-2 py-3").style("flex:1 1 300px"):
                        with ui.row().classes("items-center gap-2 flex-wrap"):
                            ui.badge(b["tag"]).props(
                                f"color={'primary' if b['tag'] == '今年' else 'blue-grey'}").classes("text-sm")
                            ui.label(f"{b['key']} 年 · {b['total']} 部").classes("font-bold text-base")
                        with ui.row().classes("gap-6"):
                            for num, lbl, color in stats:
                                with ui.column().classes("items-center gap-0"):
                                    ui.label(str(num)).classes(f"text-2xl font-bold leading-none {color}")
                                    ui.label(lbl).classes("text-xs text-gray-400 mt-1")
                        ui.separator()
                        with ui.row().classes("gap-4 text-xs text-gray-400"):
                            ui.label(f"已下 {b['done']}")
                            ui.label(f"待下 {b['pending']}")
                            ui.label(f"版本 {b['versions']}")

            items = mov.list_movies()
            if not items:
                ui.label("还没有剧场版/OVA。去『订阅源』tab 点『扫描』从 Mikan 拉取。").classes(
                    "text-gray-400 p-4")
                return
            yrs = max(1, config.MOVIE_PAGE_YEARS)  # 防 0（每页 0 年会除零）
            shown, total_pages, page = paginate(_group_by_year_quarter(items), list_page["n"], yrs)
            list_page["n"] = page
            with ui.row().classes("items-center gap-3 pl-1 pb-1 flex-wrap"):
                expand_collapse_bar(list_page, list_panel.refresh)
                if total_pages > 1:
                    ui.pagination(1, total_pages, direction_links=True, value=page,
                                  on_change=_list_goto).props("size=sm")
                    ui.label(f"共 {total_pages} 页 · 每页 {yrs} 年").classes("text-xs text-gray-500")
            exp = list_page["expand"]  # None=默认仅最新年展开(年内季度全铺开)；True/False=一键全展开/收起(跨页一致)
            tmap = mov.torrents_by_movie(
                [m.id for _, qs in shown for _, grp in qs for m in grp])  # 本页种子一次查齐
            for i, (year, quarters) in enumerate(shown):
                year_open = exp if exp is not None else i == 0   # 一级(年份)可折叠，默认仅最新年展开
                year_total = sum(len(grp) for _, grp in quarters)
                _yt = "未知年份" if year == "未知" else f"20{year} 年"   # 未知季度别拼成『20未知 年』
                year_exp = ui.expansion(f"{_yt}   ·   {year_total} 部",
                                        value=year_open).classes("w-full")
                # 懒加载在『年』这级：年展开才建内容。二级『季度』不折叠——小标题 + 卡直接全铺开。
                _fl = {"built": False}

                def _fill(qs=quarters, box=year_exp, fl=_fl):
                    if fl["built"]:
                        return
                    fl["built"] = True
                    with box:
                        for q, grp in qs:
                            ui.label(f"{engine.quarter_label(q)}   ·   {len(grp)} 部").classes(
                                "text-sm font-bold text-gray-400 mt-3 mb-1 pl-1")  # 二级季度小标题(不可折叠)
                            for m in grp:
                                _movie_card(m, tmap.get(m.id, []))

                if year_open:
                    _fill()
                else:
                    year_exp.on_value_change(lambda e, f=_fill: f() if e.value else None)

        @ui.refreshable
        def fail_panel():
            items = mov.list_unmatched_movies()
            if not items:
                ui.label("没有待识别的剧场版。（bgm 没自动匹配上的会出现在这里，可绑定或忽略）").classes(
                    "text-gray-400 p-4")
                return
            ui.label("这些剧场版没自动匹配到 bgm：缺规范名/日语文件夹名/季度。可『重试识别』或粘贴 bgm 链接『绑定』。").classes(
                "text-xs text-gray-400 p-2")
            _smap = mov.source_map()   # 批量取来源，避免逐片查的 N+1
            for m in items:
                with ui.card().classes("w-full"):
                    with ui.row().classes("items-center gap-3 flex-wrap"):
                        ui.badge("未识别").props("color=red")
                        ui.label(name_of(m)).classes(
                            "text-lg cursor-pointer text-blue-400 hover:underline").on(
                            "click", lambda mid=m.id: open_detail(mid))
                        ui.label("来源: " + (" · ".join(_smap.get(m.id, [])) or "—")).classes(
                            "text-xs text-gray-400")
                    with ui.row().classes("items-stretch gap-3 flex-wrap"):
                        inp = ui.input(placeholder="bgm 链接或 ID").props("dense outlined").classes("min-w-96")
                        ui.button("绑定", icon="link", on_click=_bind_from_input(m.id, inp)).props("color=primary unelevated")
                        ui.button("重试识别", icon="refresh", on_click=_refail(m.id)).props("flat color=grey")
                        ui.button("忽略", on_click=_reject(m.id)).props("flat color=grey")

        @ui.refreshable
        def reject_panel():
            rej = mov.list_rejected_movies()
            dels = mov.deleted_torrent_rows()      # 已删除种子（本页最底折叠①）
            excls = mov.excluded_torrent_rows()    # 已排除种子（本页最底折叠②）
            if not rej and not dels and not excls:
                ui.label("没有已忽略的剧场版。（列表里点『忽略』会进这里，可随时恢复）").classes(
                    "text-gray-400 p-4")
                return
            _smap = mov.source_map()   # 批量取来源，避免逐片查的 N+1
            for m in rej:
                with ui.card().classes("w-full"):
                    with ui.row().classes("items-center gap-3 flex-wrap"):
                        ui.badge("已忽略").props("color=grey")
                        ui.label(name_of(m)).classes(
                            "text-lg cursor-pointer text-blue-400 hover:underline").on(
                            "click", lambda mid=m.id: open_detail(mid))
                        ui.label("来源: " + (" · ".join(_smap.get(m.id, [])) or "—")).classes(
                            "text-xs text-gray-400")
                    with ui.row().classes("items-stretch gap-3 flex-wrap"):
                        ui.button("恢复订阅", icon="undo", on_click=_restore(m.id)).props(
                            "color=primary unelevated")

            # 已删除的种子：本页最底的独立折叠，可『重新下载』找回（与番剧侧对称）
            if dels:
                with ui.expansion(f"已删除的种子（{len(dels)}）", icon="delete_outline").classes(
                        "w-full mt-2").props("dense"):
                    ui.label("你手动删掉文件的剧场版版本（终态，不会自动重下）。需要就点『重新下载』找回。").classes(
                        "text-xs text-gray-500 p-1")
                    for d in dels:
                        with ui.row().classes("items-center gap-3 w-full text-sm py-1").style(
                                "border-bottom:1px solid rgba(255,255,255,.06)"):
                            ui.label(d["name"]).classes(
                                "shrink-0 cursor-pointer text-blue-400 hover:underline").on(
                                "click", lambda mid=d["movie_id"]: open_detail(mid))
                            ui.label(d["raw"]).classes("text-gray-500 text-xs break-all min-w-0 grow")
                            _rb = ui.button("重新下载", icon="download",
                                            on_click=_redownload(d["id"])).props(
                                "flat dense size=sm color=primary").style("font-size:14px")
                            _rb.set_enabled(config.QB_ENABLED)
                            _rb.tooltip("强制重新下这一版本到原目录（找回删掉的文件）" if config.QB_ENABLED
                                        else "qB 未启用，去设置页开启后可下载")

            # 已排除的种子：独立折叠，可『恢复』放回可下（与番剧侧对称）
            if excls:
                with ui.expansion(f"已排除的种子（{len(excls)}）", icon="block").classes(
                        "w-full mt-2").props("dense"):
                    ui.label("你从可下里主动排除的剧场版版本（不显示为可下、RSS 再遇到也不重收）。点『恢复』放回。").classes(
                        "text-xs text-gray-500 p-1")
                    for x in excls:
                        with ui.row().classes("items-center gap-3 w-full text-sm py-1").style(
                                "border-bottom:1px solid rgba(255,255,255,.06)"):
                            ui.label(x["name"]).classes(
                                "shrink-0 cursor-pointer text-blue-400 hover:underline").on(
                                "click", lambda mid=x["movie_id"]: open_detail(mid))
                            ui.label(x["raw"]).classes("text-gray-500 text-xs break-all min-w-0 grow")
                            ui.button("恢复", icon="undo", on_click=_unexclude_mv(x["id"])).props(
                                "flat dense size=sm color=primary").style("font-size:14px").tooltip(
                                "放回可下")

        @ui.refreshable
        def sources_panel():
            ui.label("剧场版/OVA 的来源固定为 Mikan 季度浏览页的『剧场版/OVA 桶』——非 RSS 订阅，"
                     "不用像番剧那边配字幕组。识别走 bgm；是不是电影以 Mikan 桶为准（哪怕 bgm 识别成 TV 也留在这）。").classes(
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
                    ui.button("保存", icon="save", on_click=lambda: _save_scan(f)).props("color=primary unelevated")
                last = config.MOVIE_SCAN_LAST or "从未"
                ui.label(f"上次扫描：{last}").classes("text-xs text-gray-400")
                ui.label("剧场版桶更新不频繁，间隔别设太小；改动即时生效，到点自动扫。").classes(
                    "text-xs text-gray-500")

            # 手动立即扫描（可指定年份/季度回填历史）
            with ui.card().classes("w-full"):
                ui.label("手动立即扫描").classes("font-bold")
                sel_seasons = set(SEASON_CN)   # 默认全选（A/B/C/D）
                with ui.row().classes("items-stretch gap-3 flex-wrap"):
                    year = ui.number("年份", value=datetime.now().year, format="%d").props(
                        "dense outlined").classes("w-28")
                    with ui.row().classes("items-stretch gap-2"):
                        for _k, _v in SEASON_CN.items():
                            _season_toggle_btn(_k, _v, sel_seasons)
                    ui.button("立即扫描", icon="travel_explore",
                              on_click=lambda: _scan(year, sel_seasons)).props("color=primary unelevated")
                ui.label("想补抓往年的剧场版就改年份手动扫；日常交给上面的自动扫描即可。").classes(
                    "text-xs text-gray-500")

        def refresh_all():
            overview_panel.refresh()
            inflight_panel.refresh()
            recent_panel.refresh()
            list_panel.refresh()
            fail_panel.refresh()
            reject_panel.refresh()
            sources_panel.refresh()

        async def _qb_sync_now():
            """『下载状态』的立刻刷新：主动向 qB 拉一次在下版本的进度，不必等后台轮询（对齐番剧页）。"""
            if not config.QB_ENABLED:
                ui.notify("qB 未启用（去设置页开启『发送种子到 qB』）", type="warning")
                return
            try:
                n = await mov.sync_qb_status()
            except Exception as e:                       # qB 连不上/超时：给可读提示，别掀翻页面
                ui.notify(f"同步失败：{e}", type="negative")
                return
            overview_panel.refresh()
            inflight_panel.refresh()
            ui.notify(f"已同步 {n} 个版本的进度" if n else "没有正在下载的版本", type="positive")

        # ---- 标签 ----
        with ui.tabs().classes("w-full") as tabs:
            for _k, _name, _icon in _TABS_DEF:
                ui.tab(_k, _name, _icon)
        # 懒加载：首屏只构建当前 tab，切到别的 tab 首次才建（未建过的 .refresh() 是安全 no-op）
        _builders = {
            "overview": lambda: (overview_panel(), inflight_panel(), recent_panel()),
            "list": list_panel, "fail": fail_panel,
            "reject": reject_panel, "sources": sources_panel,
        }
        _slots: dict = {}
        _built: set = set()

        def _build_tab(key):
            if key in _built or key not in _slots:
                return
            _built.add(key)
            with _slots[key]:
                _builders[key]()

        start = t if t in _TABS else (
            config.MOVIE_DEFAULT_TAB if config.MOVIE_DEFAULT_TAB in _TABS else "list")
        with ui.tab_panels(tabs, value=start).props("keep-alive transition-prev=fade transition-next=fade transition-duration=120").classes("w-full"):
            for _k in _TABS:
                _slots[_k] = ui.tab_panel(_k)   # 空面板本身作容器，懒建时直接填入（不套内层 div，保持 tab-panel 原生行距）

        def _on_tab(e):   # 切 tab：写 URL(不重载) + 首次构建该 tab
            ui.run_javascript(f"history.replaceState(null,'','?t='+encodeURIComponent('{e.value}'))")
            _build_tab(e.value)
        tabs.on_value_change(_on_tab)
        _build_tab(start)   # 构建首屏 tab
