"""番剧详情组件：render_anime_detail 渲染进列表页的悬浮框(dialog)，不再有独立页路由。"""
from nicegui import ui

from core import anime, engine
import config
from .layout import (WEEKDAY_CN, confirm, ep_str, meta_card, name_of, parse_bgm_id,
                     qb_live_text, season_label, source_options, torrent_status_cn,
                     warn_banner)

# 正在补齐的番 id。挂模块级而不是弹窗闭包里：闭包随详情弹窗重建而重置（关掉再打开就绕过了），
# 而补齐是几十秒的多站搜索 + 批量入库。模块级还顺带挡住"多个标签页对同一部番同时点"。
_backfill_running: set = set()


def notify_relocate_anime(rep):
    """把 relocate_anime 的结果转成用户提示（番剧侧唯一一份，两个页面共用）。"""
    if rep.get("error"):
        ui.notify(f"移动失败：{rep['error']}", type="negative")
        return
    parts = []
    if rep.get("moved"):
        parts.append(f"已移动 {rep['moved']} 集到新目录")
    if rep.get("redownload"):
        parts.append(f"{rep['redownload']} 集 qB 未跟踪/连不上 → 已清状态待重下到新目录")
    if rep.get("failed"):
        parts.append(f"{rep['failed']} 集移动被拒（{rep.get('fail_code')}：新目录不可写，未改动）")
    if rep.get("stalled_kept"):   # 停滞集有意不动：不降级、不自动重下，但要告诉用户文件没跟着走
        parts.append(f"{rep['stalled_kept']} 集处于停滞、未移动（保留停滞标记，未自动重下）")
    if rep.get("delivering"):     # 正在交付给 qB 的集：路径在交付前就定死了，搬不了也不能碰
        parts.append(f"{rep['delivering']} 集正在交付给 qB、未移动"
                     "（它会先落到旧目录，交付完成后再点一次『编辑季度』即可搬过来）")
    msg = "；".join(parts) or "无需移动"
    warn = bool(rep.get("redownload") or rep.get("failed")
                or rep.get("stalled_kept") or rep.get("delivering"))
    if (rep.get("redownload") or rep.get("stalled_kept")) and rep.get("old_path"):
        msg += f"。⚠️ 旧文件在 {rep['old_path']} 需你手动清理"
    ui.notify(msg, type="warning" if warn else "positive")


async def maybe_relocate_anime(anime_id: int, old_path, after=None):
    """改季度/重绑/重识别后：归档目录变了且有已下集就问是否搬迁，并按结果提示。

    【模块级、两个页面共用】详情页和列表页（『待识别』tab 的绑定/重试识别）都要走它——
    早先只有详情页有，列表页那两条路径改完目录既不问也不提，已下的集被静默遗弃在旧目录，
    同一部番裂成两个文件夹。对齐 movies 侧早已模块级的 _maybe_relocate_movie。
    """
    new_path = anime.anime_save_path(anime_id)
    if not new_path or new_path == old_path:
        return   # 路径没变，无需移动
    # 计数必须与 relocate_anime 的选行【同谓词】：HAVE 且【未归档】。
    # 已归档的早已不在 qB、setLocation 移不动，若算进来就会出现
    #『弹窗说要搬 N 集 → 点确认 → 报"无需移动" → 文件静默留在旧目录』。
    _eps = anime.list_episodes(anime_id)
    dl = [t for t in _eps if t.status in engine.HAVE_STATUSES and not t.archived_at]
    arch = [t for t in _eps if t.status in engine.HAVE_STATUSES and t.archived_at]
    if not dl:
        if arch:   # 只有归档文件：搬不了，但必须告诉用户它们留在哪儿
            ui.notify(f"归档目录已更新。但有 {len(arch)} 集已归档（不在 qB，无法代为移动），"
                      f"其文件仍在旧目录 {old_path or '原位置'}，需你手动处理", type="warning")
        else:
            ui.notify("归档目录已更新（无已下文件，新集将下到新目录）", type="positive")
        return
    _extra = f"；另有 {len(arch)} 集已归档、无法代为移动（文件留在旧目录）" if arch else ""
    if not await confirm("归档目录变了，移动已下文件？",
                         f"{len(dl)} 集已下，移到新目录：{new_path}{_extra}",
                         ok_label="移动文件", ok_icon="drive_file_move"):
        ui.notify("已更新记录，未移动文件（新集将下到新目录，旧文件留在原处）", type="warning")
        return
    rep = await anime.relocate_anime(anime_id, old_path)
    if after:
        after()
    notify_relocate_anime(rep)


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
                    ui.badge(_sl).props("color=purple")
                if cur.rejected:
                    ui.badge("超期忽略" if not cur.confirmed else "已忽略").props("color=grey")
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
            ui.button("编辑季度", icon="event", on_click=_edit_quarter).props(
                "flat dense size=sm").style("font-size:12px").tooltip(
                "手动改归档季度（bgm 定错/无放送日时兜底）。改后可把已下文件移到新目录。")
            if cur.rejected:
                ui.button("恢复订阅", icon="undo", on_click=_restore).props(
                    "dense size=sm color=primary unelevated").style("font-size:12px")
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
        with ui.row().classes("items-stretch gap-3 flex-wrap"):
            if sources:  # 下载源放最前：按优先级=多源兜底；选具体组=锁定，之后只下这个组
                ui.select(source_options(sources, "下载源：按优先级·多源兜底"),
                          value=(cur.pref_source or ""),
                          on_change=_set_source).props("dense outlined").classes(
                    "w-full sm:w-auto sm:min-w-52").tooltip(
                    "『按优先级』= 多源自动挑、缺集用别的源兜底；"
                    "选某个组 = 锁定，之后只下这个组，它缺的集不兜底（自己来点下载）")
                _kwin = ui.input(value=(cur.pref_keyword or ""), label="版本",
                                 placeholder="繁日/简日/1080p…").props("dense outlined").classes(
                    "w-36").tooltip(
                    "版本关键词：在『下载源』基础上，再只下种子原名含此词的版本（繁日/简日/画质等，大小写不敏感）。"
                    "留空=不限；硬过滤、缺此版本的集不兜底。回车或点输入框外生效。")
                _kwin.on("blur", lambda: _set_keyword(_kwin.value))
                _kwin.on("keydown.enter", lambda: _set_keyword(_kwin.value))
            if not cur.rejected and not cur.confirmed:
                ui.button("确认下载", on_click=_approve).props("color=primary unelevated")
            _dln = ui.button("下载该源", icon="download", on_click=_download).props("flat")
            _dln.set_enabled(config.QB_ENABLED)
            _dln.tooltip("qB 未启用，去设置页开启后可下载" if not config.QB_ENABLED
                         else "按左边『下载源』下：锁了某源→下该源缺的每一集；『按优先级』→每集下应下的那份，已下的跳过。"
                              "只下正集——特别篇/未知集要对准下面那一条点『下载』")
            _btn_bf_loose = ui.button("补齐该源", icon="playlist_add", on_click=_backfill_loose).props(
                "flat dense size=sm").style("font-size:14px")
            _btn_bf_loose.tooltip("去 nyaa/Mikan 按名搜『当前下载源』的种子补漏收（季度过滤，你人工审核）。"
                         "入库后转『待确认』，点『确认下载』才下。")
            _btn_bf_auto = ui.button("自动补齐", icon="auto_awesome", on_click=_backfill_auto).props(
                "flat dense size=sm").style("font-size:14px")
            _btn_bf_auto.tooltip("同『补齐该源』，但额外用番名近似过滤挡掉同名衍生作/别的季，更少需人工把关。")

        # 分集 / 种子（每条可单独强制下载）；标题右侧灰字标注会发送给 qB 的保存目录（按规则算出，全番同一目录）
        with ui.row().classes("items-center gap-2 w-full mt-2 flex-wrap"):
            ui.label(f"分集 / 种子（{len(eps)}）").classes("text-sm font-bold shrink-0")
            ui.space()
            _sp = anime.anime_save_path(anime_id)
            _unident = not (cur.jp_name or cur.display_name)   # 无 bgm 规范名/日文名 = 未识别/待绑定，目录名靠种子解析名兜底、可能不准
            if not _sp:
                ui.label("↓ 未配置下载目录").classes("text-gray-500 text-xs break-all min-w-0 text-right")
            elif _unident:
                ui.label(f"⚠️ 未识别，暂用解析名：{_sp}").classes(
                    "text-amber-400 text-xs break-all min-w-0 text-right").tooltip(
                    "该番还没匹配到 bgm，保存目录暂用种子解析名（可能不准）。建议先『绑定 bgm』或『重新识别』再下，"
                    "免得目录名不对、日后改季度/重绑还要搬文件")
            else:
                ui.label(f"↓ {_sp}").classes(
                    "text-gray-500 text-xs break-all min-w-0 text-right").tooltip(
                    "下载时发送给 qB 的保存目录（按 工作目录/季度/番名/Season 规则算出）")
        # 跨源集号体系不一致的兜底提示：能从 '16(88)' 双编号推出偏移的番已在入库时自动折算，
        # 这里剩下的是推不出、必须人工判断的（同一集会被当成两集，各下一份到同一目录）。
        _conf = anime.episode_numbering_conflict(anime_id)
        if _conf:
            warn_banner(
                f"疑似同集不同编号：本番共 {cur.total_episodes} 集，但有种子标着第 "
                + "、".join(ep_str(e) for e in _conf)
                + " 集——多半是某个源用了【全系列绝对集号】而另一个用【季内集号】。"
                  "这两种写法会被当成不同的集，同一集因此会被下两份到同一个目录。"
                  "请核对后手动排除多余的那份。"
                + ("本番两套编号的取值范围重叠，系统【不会】自动折算，请手动改集号合并。"
                   if (cur.ep_offset and cur.total_episodes and cur.ep_offset < cur.total_episodes)
                   else "若某个源的标题里带 “16(88)” 这类双编号，系统会自动学到偏移量并从此自行折算。"))
        # 【歧义段】前面各季共 O 集(ep_offset)、本季 T 集：绝对编号取值域 [O+1,O+T] 与季内编号
        # [1,T] 在 O<T 时于 [O+1,T] 上重叠，光看集号分辨不出是哪一套，系统一条都不折
        #（折一半会让同一个源的前后半季撞成同一个去重键、静默漏掉半季，见 core.anime._foldable）。
        # 这里只陈述事实、不做猜测：告诉用户这一段可能各下一份、要合并请手动改集号。
        if cur.ep_offset and cur.total_episodes and cur.ep_offset < cur.total_episodes:
            warn_banner(
                f"本番有集号歧义段：前面各季共 {cur.ep_offset} 集、本季 {cur.total_episodes} 集，"
                f"于是第 {cur.ep_offset + 1}~{cur.total_episodes} 集这一段，"
                "『全系列绝对集号』与『季内集号』两种写法的取值范围重叠、分辨不出。"
                "系统对这段【既不自动合并、也不跨源去重】：同一集最多每个源各下一份，宁可多下也不冒险把两集不同内容当成一集而漏下。"
                "如确认某两条其实是同一集，点左边集号手动改成一致即可。")
        if not eps:
            ui.label("（还没有种子）").classes("text-gray-400")
            return
        # 两个问题各查各的：pending 行问『后台自动会下吗』(只算 pending，与 flush 同口径)，
        # error 行问『点补下会挑中吗』(含 error)。混用一个集合会让高优先级的 error 顶掉
        # 同集真正会被自动下的 pending，把『将下载』标到不会下的那条上。
        plan = anime.download_plan(anime_id)
        backfill_plan = anime.download_plan(anime_id, for_backfill=True)
        # 这一集是否『有着落』：将下(plan) 或 已有一份(HAVE_STATUSES：已下/在下/停滞)。与 flush 同口径——
        # stalled 也算有着落(不再误报缺集，B4)；deleted 不算(删了同集新 hash 会当将下载来下)。
        covered = {t.episode for t in eps
                   if t.id in plan or t.id in backfill_plan or t.status in anime.HAVE_STATUSES}
        for t in eps:
            ep_txt = f"第{ep_str(t.episode)}集"
            with ui.column().classes("w-full gap-0 py-1").style(
                    "border-bottom:1px solid rgba(255,255,255,.08)"):
                # 第一行：集号 · 字幕组 · 时间 同一行居中（天然竖直齐平），状态/按钮 space() 推到最右
                with ui.row().classes("items-center gap-3 w-full text-sm flex-wrap"):
                    if t.status in engine.DOWNLOADABLE_STATUSES:  # 待下/失败的集号都可点改：不止 -2，
                        _neg = t.episode == -2                            # 解析也可能写错正整数(把分辨率/季号当集号)
                        ui.label(ep_txt).classes(
                            "shrink-0 cursor-pointer hover:underline"
                            + (" text-blue-400" if _neg else "")).on(
                            "click", _set_ep(t.id)).tooltip(
                            "集号没解析出来；点这里改" if _neg else "集号不对？点这里改")
                    else:
                        ui.label(ep_txt).classes("shrink-0")              # 主要信息：集号（已下等不可改）
                    ui.label(t.source or "—").classes("shrink-0")         # 主要信息：字幕组（同色）
                    ui.label(engine.torrent_time(t)).classes(
                        "shrink-0 text-gray-500 text-xs")                 # 次要：时间(12px)
                    ui.space()
                    live = qb_live_text(t)
                    if live:  # qB 实时态：完成(做种/100%)才绿，下载中用蓝
                        _done = (t.qb_progress or 0) >= 1
                        ui.badge(live).props(f"color={'green' if _done else 'blue'}").tooltip(
                            "qB 实时状态")
                    elif t.status == "pending":  # 待下：未知集 / 重试中 / 将下载 / 备用
                        # 重试信息挂在各自徽标的 tooltip 里，让【集号状态】始终占着徽标位。
                        _retry_tip = ("" if not t.retry_at else
                                      f"；上次失败：{t.fail_reason or '暂时性失败'}"
                                      f"（{t.retry_at.strftime('%m-%d %H:%M')} 后重试）")
                        # 【-1/-2 后台永远不自动下】这条最要紧，不能被别的徽标盖掉。
                        # 它们连"排队重试"都不会有（见 core.anime.auto_downloadable_ep 与 _retry），
                        # 所以措辞里绝不能出现"会自动重试"——那是我们做不到的承诺。
                        if t.episode is not None and t.episode < 0:
                            _neg_lab, _neg_tip = (
                                ("未知集", "批量/集号没解析出来，后台不自动下。点左边『第?集』改集号，或下方排除")
                                if t.episode == -2 else
                                ("特别篇", "特别篇/OVA 不自动下（多组多版本，自动挑一份常挑错）。要它就点右边『下载』"))
                            ui.badge(_neg_lab).props("color=purple").tooltip(
                                _neg_tip
                                + (_retry_tip.replace("后重试", "后仍不会自动重发，需人工下")
                                   if t.retry_at else ""))
                        elif t.retry_at:   # 暂时性失败排队中：它仍是待下，但要让人看见"为什么还没下"
                            ui.badge(f"重试中·第{t.retry_count}次").props("color=orange").tooltip(
                                f"{t.fail_reason or '暂时性失败'} · "
                                f"{t.retry_at.strftime('%m-%d %H:%M')} 之后自动重发"
                                "（等不及就点右边『下载』立刻重试）")
                        elif t.id in plan:
                            ui.badge("将下载").props("color=blue").tooltip(
                                "这一集的首选版本，补下/自动下会下它")
                        elif t.episode in covered:
                            ui.badge("备用项").props("color=blue-grey").tooltip(
                                "同集已有『将下载/已下』的更优版本；要这条就点右边下载")
                        else:  # 该集没有任何将下/已下版本——被锁定源/版本过滤光了=真·缺集
                            ui.badge("缺集·仅非锁源").props("color=orange").tooltip(
                                "这一集在当前锁定源/版本下没有可下的，只剩被过滤掉的版本；要它就点右边下载")
                    elif t.status == "error":
                        # error 行问的是『点补下会不会挑它』——后台自动下从不重试 error，
                        # 故这里必须用 backfill_plan，不能用只含 pending 的 plan
                        _bf = t.id in backfill_plan
                        _why = f"{t.fail_reason}；" if t.fail_reason else ""
                        _tried = f"（已自动重试 {t.retry_count} 次仍不行）" if t.retry_count else ""
                        ui.badge("失败·可补下" if _bf else "失败").props(
                            f"color={'orange' if _bf else 'red'}").tooltip(
                            _why + (f"点右边『下载』或『补下本番』手动重试{_tried}"
                                    if _bf else f"下载失败过{_tried}"))
                    else:  # 无 qB 实时态：刚交付未同步→下载中；其余(已下完/跳过/已删/已排除)按状态
                        ui.badge(torrent_status_cn(t.status, t.qb_progress, t.qb_synced_at)).props(
                            "color=blue-grey")
                    if t.status == "excluded":  # 已排除：给『恢复』放回待下
                        ui.button("恢复", icon="undo", on_click=_unexclude(t.id)).props(
                            "size=sm flat dense color=primary").style("font-size:12px").tooltip(
                            "放回待下，重新参与下载/去重")
                    if t.status in engine.DOWNLOADABLE_STATUSES:  # 未下载的才可直接排除
                        ui.button("排除", icon="block", on_click=_exclude(t.id)).props(
                            "size=sm flat dense color=grey").style("font-size:12px").tooltip(
                            "不想要这条：从待下直接排除（不删文件，只改状态；可撤销）")
                    if t.status in engine.HAVE_STATUSES:  # 下过/在下/停滞(盘上有文件)都可删（delete 函数亦接受 stalled）
                        ui.button(icon="delete_forever",
                                  on_click=_del_one(t.id, t.archived_at, t.save_path)).props(
                            "size=sm flat dense color=negative").tooltip(
                            "已归档：不在 qB，只能标记已删，文件需你手动清理" if t.archived_at
                            else "删除这一集的文件（qB+硬盘，不可撤销）")
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

    def _set_keyword(val):
        v = (val or "").strip()
        # 【没改就什么都不做】blur 每次失焦都触发——点一下输入框再点别处，本来什么都没改，
        # 却会写一次库、整块 body.refresh()（正在滚动/展开的状态全丢）、还弹一条橙色警告。
        cur_kw = getattr(anime.get_anime(anime_id), "pref_keyword", "") or ""
        if v == cur_kw:
            return
        anime.set_pref_keyword(anime_id, v)
        body.refresh()
        ui.notify(f"版本限定『{v}』：只下原名含此词的版本（缺此版本的集不兜底）" if v
                  else "已取消版本限定", type="warning" if v else "positive")

    async def _enrich():
        # 识别成功会写入 bgm 名/季号 → 归档目录随之改变，必须和绑定(_bind)、列表页那两条路径
        # 一样问一句要不要搬迁。剧场版侧的同名 _enrich 本来就是这么写的（pages/movies.py:213）；
        # 番剧详情页这条一直漏着，已下的集会被静默遗弃在旧目录、同一部番裂成两个文件夹。
        old_path = anime.anime_save_path(anime_id)
        anime.reset_enrich_tries(anime_id)   # 手动重识别：清零后台重试计数，重新获得自动重试机会
        ok = await anime.enrich_anime(anime_id)
        _after()
        ui.notify("识别成功" if ok else "未识别到（Mikan/bgm 没有或查不到）")
        if ok:
            await maybe_relocate_anime(anime_id, old_path, _after)

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
        if not bid:
            ui.notify("没认出 bgm ID（粘 bgm.tv/subject/数字 或纯数字）", type="warning")
            return
        old_path = anime.anime_save_path(anime_id)   # 重绑前的归档路径（bind 可能改名/季度/季号）
        ok = await anime.bind_anime_bgm(anime_id, bid)
        _after()
        ui.notify("已绑定并识别 ✓（回到待确认，去点确认下载）" if ok
                  else "绑定失败：ID 不存在或取不到 bgm 数据", type="positive" if ok else "negative")
        if ok:
            await maybe_relocate_anime(anime_id, old_path, _after)

    async def _edit_quarter():
        cur = anime.get_anime(anime_id)
        dlg = ui.dialog()
        with dlg, ui.card().classes("gap-2"):
            ui.label("编辑归档季度").classes("font-bold")
            ui.label("内部键 = 两位年 + 季母（A冬 B春 C夏 D秋），如 26A=2026年冬。"
                     "改后可把已下文件移到新目录。").classes("text-xs text-gray-400")
            inp = ui.input("季度键", value=(cur.quarter if cur else "")).props(
                "dense outlined autofocus").classes("min-w-60")
            prev = ui.label().classes("text-sm text-blue-400")

            def _pv():
                q = (inp.value or "").strip().upper()
                good = len(q) == 3 and q[:2].isdigit() and q[2] in "ABCD"
                prev.text = ("预览：" + engine.quarter_label(q)) if good else "格式：26A / 26B / 26C / 26D"

            inp.on_value_change(lambda: _pv())
            _pv()
            with ui.row().classes("gap-2 justify-end w-full"):
                ui.button("取消", on_click=lambda: dlg.submit(None)).props("flat")
                ui.button("保存", icon="save",
                          on_click=lambda: dlg.submit(inp.value)).props("color=primary")
        val = await dlg
        if val is None:
            return
        old_path = anime.anime_save_path(anime_id)
        if not anime.set_quarter(anime_id, val):
            ui.notify("季度格式不对（应为 26A/26B/26C/26D）", type="warning")
            return
        _after()
        ui.notify(f"季度已改为 {engine.quarter_label((val or '').strip().upper())}", type="positive")
        await maybe_relocate_anime(anime_id, old_path, _after)


    async def _approve():
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

    async def _run_backfill(name_filter):
        # 防抖挂在【番 id】上而不是弹窗闭包里：闭包随详情弹窗重建而重置，关掉再打开就绕过去了，
        # 而补齐是几十秒的多站并发搜索 + 批量入库，重入会重复打站点、并把番反复踢回待确认。
        # 挂在模块级集合上还顺带挡住"两个标签页对同一部番同时点"。
        if anime_id in _backfill_running:
            ui.notify("正在补齐中，请稍候…", type="info")
            return
        # 【先占位再问】下面的确认框是个 await，会让出事件循环等用户点按钮：若等确认之后才占位，
        # 双击的两次点击都能在占位前通过检查、各弹一个框，两个都确认就并发跑两轮。
        _backfill_running.add(anime_id)
        try:
            return await _run_backfill_inner(name_filter)
        finally:
            _backfill_running.discard(anime_id)

    async def _run_backfill_inner(name_filter):
        cur = anime.get_anime(anime_id)
        if cur is not None and cur.confirmed and not cur.rejected:
            # 已确认番：补齐入库新种子会把整部番退回『待确认』，后台从此暂停自动下新集，直到重新确认——先告知
            if not await confirm(
                    "补齐会暂停这部番的自动下载",
                    "这部番当前『已确认』、正在自动追。补齐入库新种子后会把它退回『待确认』，"
                    "后台将暂停自动下新集，直到你去『待确认』页重新点确认。继续？",
                    ok_label="继续补齐", ok_icon="playlist_add", ok_color="primary"):
                return
        ui.notify("正在搜索补齐…（去 nyaa/Mikan 按名搜，请稍候）")
        try:
            res = await anime.backfill_source(anime_id, name_filter)
        except Exception as e:            # 兜住 fetch 之外的意外，别逃逸崩掉处理器
            ui.notify(f"补齐出错：{e}", type="negative")
            return
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
            # 归属检查跳掉的那些要单独说：它与两个补齐按钮的松紧【无关】，换另一个按钮再点结果一样，
            # 不点明的话用户只会以为"过滤太严"而白试一遍。
            _st = f"；其中 {res['stolen']} 条对照表显示属于别的番（详见 /logs）" if res.get("stolen") else ""
            ui.notify(f"没有新种子可补（搜到 {res['found']}，都已有或被季号/名字过滤{_st}）", type="info")

    async def _backfill_loose():
        await _run_backfill(False)

    async def _backfill_auto():
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

    def _del_one(torrent_id, archived=None, save_path=""):
        async def h():
            if archived:   # 已归档：种子早已移出 qB，删不到文件 → 明确告知路径与后果，让用户自己清理
                if not await confirm(
                        "这一集已归档，只能标记已删",
                        f"它已从 qB 移除，本工具删不到文件。确认后仅标记为『已删』。\n\n"
                        f"⚠️ 文件仍在（需你手动删）：{save_path or '（未记录路径，请到下载目录自行查找）'}\n\n"
                        f"注意：标记后这一集就算『没有了』，若该集还有别的源的待下版本，"
                        f"后台可能自动补下一份——旧文件不会被覆盖，会变成同一集两份。"
                        f"请尽快自行删除上面的文件；只是不想要这个版本的话，用『排除』更合适。\n"
                        f"想连文件一起删：先点『下载』把它重新挂回 qB，再删。",
                        ok_label="标记已删", ok_icon="delete_forever"):
                    return
            # 已归档那个分支早就写明了"备用源会自动补下"，普通删除这条一直没提——
            # 而它才是最常走的路径。用户多半是为了腾磁盘，二十分钟后文件换个字幕组回来了。
            elif not await confirm("删除这一集的文件？",
                                   "通过 qB 连同硬盘文件一起删除，不可撤销。\n"
                                   "这一集若还有别的源的『备用项』待下，后台会在下一轮采集自动补下一份（删的是这一条种子，不是整集）。不想让它回来：先把备用项『排除』，或整部番用『忽略』。",
                                   ok_label="删除文件", ok_icon="delete_forever"):
                return
            ok = await anime.delete_anime_torrent(torrent_id)
            _after()
            if ok and archived:
                ui.notify(f"已标记为已删；文件仍在 {save_path or '下载目录'}，需手动清理", type="warning")
            else:
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
                    ui.button("确定", on_click=lambda: dlg.submit(num.value)).props("color=primary unelevated")
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
                                 "从待下里排除：不再自动下、不再挂在未知集，RSS 再遇到同种子也不重收；"
                                 "排除后可随时点『恢复』放回待下。",
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
