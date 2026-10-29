"""番剧详情组件：render_anime_detail 渲染进列表页的悬浮框(dialog)，不再有独立页路由。"""
from nicegui import ui

from core import anime, engine
import config
from .layout import (SEVERITY_COLOR, WEEKDAY_CN, busy_action, confirm, require_bind_confirm,
                     ep_label, ep_ranges_text, ep_str, finished_tip, meta_card,
                     name_of, page_dialog, parse_bgm_id, wait_dialog,
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
        # (r39 Q2) 不写『连不上』：连不上走上面的 error 分支、一行都不动，这里只可能是 qB 说没有。
        # 也不许诺『待重下到新目录』：flush 只重下在订阅中、符合锁定源和版本关键词的，待确认/已忽略的番不会。
        parts.append(f"{rep['redownload']} 集 qB 里已没有这条种子（多半是下完后被移除）→ 已清成待下"
                     "（在订阅中、符合锁定源和版本关键词的才会自动重下；待确认或已忽略的要先确认或恢复订阅）")
    if rep.get("in_place"):       # (r39 Q2) 文件本就在当前目录：relocate 不碰它们，说一声免得用户以为漏了
        parts.append(f"{rep['in_place']} 集 qB 已不跟踪、文件本就在当前目录，未改动")
    if rep.get("failed"):
        parts.append(f"{rep['failed']} 集移动被拒（{rep.get('fail_code')}：新目录不可写，未改动）")
    if rep.get("stalled_kept"):   # 停滞集有意不动：不降级、不自动重下，但要告诉用户文件没跟着走
        parts.append(f"{rep['stalled_kept']} 集处于停滞、未移动（保留停滞标记，未自动重下）")
    if rep.get("special_kept"):   # 特别篇/未知集：清成待下也没有路径会再下它（E-4），所以不动，要说清
        # 【出路不能是"点『下载』"】(R34 对抗审计) 这些行仍是 sent，force 也被 TRACKED 短路，点了只会
        # 弹"下载失败"。能走通的是：先『删除』（qB 不认识它 → 只标记、文件留在旧目录）再『下载』；
        # 或者手动把文件搬到新目录、状态不动。它们**不进**下面任何一句清理/处理提示——
        # 那是唯一的一份，行还指着它。
        parts.append(f"{rep['special_kept']} 条特别篇/未知集 qB 未跟踪、未移动（这类不会自动重下；"
                     "要搬到新目录：先对准那条点『删除』（只标记，文件留在原处）再点『下载』，"
                     "或手动把文件挪过去、状态不用改）")
    if rep.get("foreign_kept"):   # (E-77 C) 标了『不属于本季』的：自动路径永不下它，清成待下就回不来了，同样不动
        # 与特别篇同理：那是这一条唯一的一份、行还指着它，不进下面任何一句清理提示
        parts.append(f"{rep['foreign_kept']} 条标了『不属于本季』的 qB 未跟踪、未移动（它们不会自动重下；"
                     "要搬就手动把文件挪过去、状态不用改）")
    if rep.get("delivering"):     # 正在交付给 qB 的集：路径在交付前就定死了，搬不了也不能碰
        parts.append(f"{rep['delivering']} 集正在交付给 qB、未移动"
                     "（它会先落到旧目录，交付完成后再点一次『编辑季度』即可搬过来）")
    msg = "；".join(parts) or "无需移动"
    warn = bool(rep.get("redownload") or rep.get("failed")
                or rep.get("stalled_kept") or rep.get("special_kept") or rep.get("foreign_kept")
                or rep.get("delivering"))
    # (r39 Q2) 旧的一句『旧文件在 {old_path} 需你手动清理』有两处会删掉唯一一份：『搬回来』入口里
    # old_path 就是当前目录；而且一个目录里可能还混着停滞/特别篇/移动被拒/交付中的文件，整目录删就全没了。
    # 现在目录只取【实际被清的那几集】所在处（engine 给的 redownload_dirs），并写明下完再删、只删这几集。
    if rep.get("redownload") and rep.get("redownload_dirs"):
        if rep["redownload_dirs"] == ["原目录未记录"]:   # (r39 F4) 全是没记 save_path 的老行：别把这几个字拼得像一个路径
            msg += (f"。⚠️ 这 {rep['redownload']} 集原来放在哪里没有记录，等新目录下完后自行核对"
                    "（下完前旧文件如果还在，那是唯一一份）")
        else:
            msg += (f"。⚠️ 这 {rep['redownload']} 集的旧文件（如果还在）在 {'、'.join(rep['redownload_dirs'])}："
                    "等新目录重新下完再删，下完前那是唯一一份；只删这几集，别整目录删")
        # (r39 F1) 不断言『该目录里』：这几类行在哪个目录没有逐一核对，只提醒它们也没跟着搬
        if any(rep.get(k) for k in ("stalled_kept", "special_kept", "failed", "delivering")):
            msg += "；另有停滞、特别篇、移动被拒或交付中的文件没跟着搬、仍在用，删之前认准是哪几集"
        if rep.get("foreign_kept"):   # (E-77 C) 别季那几条的集号可能与被清的这几集相同，最容易被当成旧文件删掉
            msg += "；标了『不属于本季』的文件也没跟着搬、仍在用，别当成这几集的旧文件删"
    # (r39 F1) 只用停滞行自己的目录（engine 已排除特别篇与当前目录），不用 old_path：那是被清行与停滞行的并集
    if rep.get("stalled_kept") and rep.get("stalled_dirs"):
        msg += f"。⚠️ 停滞的半成品留在 {'、'.join(rep['stalled_dirs'])}，需你手动处理"
    ui.notify(msg, type="warning" if warn else "positive")


async def subscribe_gated(anime_id: int, action: str, pref_source: str | None = None) -> int | None:
    """让一部番【进入订阅并立刻补下】的唯一入口：先过 E-52 的闸，再做动作、再补下。

    action ∈ {"confirm"（『确认下载』）, "restore"（『恢复订阅』）, "resubscribe"（『继续订阅』）}。
    返回补下的集数；用户在闸上取消则返回 None（调用方据此不弹"已确认/已恢复"）。

    【为什么要闸、为什么三个动作都要】(E-52，2026-09-02 拍板：只报不改 + 按钮上加闸；R34 对抗审计把
    "只有『恢复订阅』一个入口"这句证伪了：改开始日会把同体番打回**待确认**，列表上就是『确认下载』；
    『绑定 bgm』『补齐该源』也把番打回待确认。) 同一部作品可能在番剧表与剧场版表里各有一条记录
    （真库里正好 2 部）。两张种子表各自唯一、**跨表没有**约束。任何一个动作都会立刻补下 —— 两边各自
    交付同一个 info_hash：qB 对已存在的 hash 返回失败，add_to_qb 的幂等兜底查到"它确实在 qB 里"判成功，
    于是**两条记录都显示『已交付』，文件只落在其中一个目录**。仪表盘横幅只是提醒；闸在按钮上。
    程序判不出该留哪一条（剧场版那条通常才是对的，但番剧那条底下可能已经下过东西），所以是确认框
    而不是拒绝：把后果说清楚，让用户自己拍。
    """
    verbs = {"confirm": "确认下载", "restore": "恢复订阅", "resubscribe": "继续订阅"}
    assert action in verbs, action
    twin = anime.movie_twin(anime_id)
    if twin is not None:
        ok = await confirm(
            f"这部番在剧场版里也有一条记录：《{twin['m_name']}》(#{twin['m']})",
            (f"两边有 {twin['shared']} 条种子是同一个文件。" if twin["shared"] else
             "两边目前没有共用的种子，但它们是同一个 bgm 条目、以后会各收一份。")
            + f"『{verbs[action]}』会立刻补下：qB 对同一个种子只收一次，两条记录都会显示『已交付』，"
              "文件却只落在其中一个目录。\n通常剧场版那条才是对的 —— 建议改成删掉番剧这一条。"
              f"仍要{verbs[action]}吗？",
            ok_label=f"仍然{verbs[action]}", ok_icon="undo", ok_color="warning")
        if not ok:
            return None
    if action == "confirm":
        anime.confirm_anime(anime_id, pref_source)
    elif action == "restore":
        anime.restore_anime(anime_id)
    else:
        anime.resubscribe(anime_id)
    return await anime.download_pending_for_anime(anime_id)


async def restore_anime_gated(anime_id: int) -> int | None:
    """『恢复订阅』= subscribe_gated(…, "restore")。留这个名字给两个现有调用点。"""
    return await subscribe_gated(anime_id, "restore")


async def maybe_relocate_anime(anime_id: int, old_path, after=None):
    """改季度/重绑/重识别后：归档目录变了且有已下集就问是否搬迁，并按结果提示。

    【模块级、两个页面共用】详情页和列表页（『待识别』tab 的绑定/重试识别）都要走它——
    早先只有详情页有，列表页那两条路径改完目录既不问也不提，已下的集被静默遗弃在旧目录，
    同一部番裂成两个文件夹。对齐 movies 侧早已模块级的 _maybe_relocate_movie。
    """
    new_path = anime.anime_save_path(anime_id)
    if not new_path:
        return
    # 计数必须与 relocate_anime 的选行【同谓词】：HAVE 且【未归档】。
    # 已归档的早已不在 qB、setLocation 移不动，若算进来就会出现
    #『弹窗说要搬 N 集 → 点确认 → 报"无需移动" → 文件静默留在旧目录』。
    _eps = anime.list_episodes(anime_id)
    dl = [t for t in _eps if t.status in engine.HAVE_STATUSES and not t.archived_at]
    arch = [t for t in _eps if t.status in engine.HAVE_STATUSES and t.archived_at]
    # 【闸的判据是"文件在不在该在的地方"，不是"这次操作改没改记录"】理由见 engine.rows_in_wrong_dir：
    # old_path 与 new_path 都是同一个纯函数算出来的，只比这两个的话，搬迁失败或被拒绝一次之后
    # 就【再没有任何入口】能补搬。
    stale = engine.rows_in_wrong_dir(_eps, new_path)
    if not engine.needs_relocate(_eps, new_path, old_path):
        return   # 路径没变，且盘上文件也都在该在的地方
    if not dl:
        if arch:   # 只有归档文件：搬不了，但必须告诉用户它们留在哪儿
            ui.notify(f"归档目录已更新。但有 {len(arch)} 集已归档（不在 qB，无法代为移动），"
                      f"其文件仍在旧目录 {old_path or '原位置'}，需你手动处理", type="warning")
        else:
            ui.notify("归档目录已更新（无已下文件，新集将下到新目录）", type="positive")
        return
    _extra = f"；另有 {len(arch)} 集已归档、无法代为移动（文件留在旧目录）" if arch else ""
    # 【确认框说的集数＝relocate 实际会动的行】(r39 F5) 已在当前目录的它不碰（同 rows_in_wrong_dir 判据）；
    # save_path 为空的老行判不了在哪，relocate 照常让它参与，这里也算上。以前用 len(dl) 把已在位的也算了进去。
    _n = len(stale) + sum(1 for t in dl if not t.save_path)
    if not _n:   # 已下的都已在新目录（例如改走又改回、当时没搬）：别弹『0 集…移到新目录』
        ui.notify(f"归档目录已更新（已下的文件本就在新目录，不用移动{_extra}）",
                  type="warning" if arch else "positive")
        return
    # 两种来路两种话（R36 体检 logic-10）：目录真的变了 → "移到新目录"；目录没变、是盘上有 N 集落在别处
    # （上次搬迁失败/被拒，或改过下载目录设置） → "搬回来"。后者正是 rows_in_wrong_dir 被抽出来的理由，以前算完就丢。
    # 用 same_dir 而不是 !=：只差分隔符/大小写算同一目录，与 needs_relocate 同一谓词 (r39 F5)
    if not engine.same_dir(new_path, old_path):
        _title, _body = "归档目录变了，移动已下文件？", f"{_n} 集已下，移到新目录：{new_path}{_extra}"
    else:
        # 括号里不断言原因 (r39 F35)：设置页目录分裂提示让用户『编辑季度原样保存』，走的正是这一支，
        # 那时文件不在位是因为刚改了设置，不是上次没搬成
        _title, _body = ("有已下文件不在归档目录里，搬回去？",
                         f"{_n} 集的文件落在别处（上次没搬成，或改过下载目录设置），搬回当前归档目录：{new_path}{_extra}")
    if not await confirm(_title, _body,
                         ok_label="移动文件", ok_icon="drive_file_move", ok_color="primary"):
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
                ui.button(icon="edit", on_click=_bind).props(
                    "flat round dense color=primary").classes("btn-sm").tooltip(
                    "认错了？手动绑定正确的 bgm（粘链接或 ID）")
                _sl = season_label(cur)
                if _sl:
                    ui.badge(_sl).props("color=purple")
                if cur.rejected:
                    ui.badge("超期忽略" if not cur.confirmed else "已忽略").props("color=grey")
                elif cur.confirmed:
                    ui.badge("已确认").props("color=green")
                elif cur.bangumi_id is None:
                    # 还没识别出 bgm 的属于『待识别』，原因文案（如"后来才匹配上 bgm"）对它不成立，不带
                    ui.badge("待确认").props("color=orange")
                else:
                    _rs, _rd = anime.confirm_reason_text(cur.confirm_reason)
                    ui.badge(f"待确认 · {_rs}").props("color=orange").tooltip(_rd)
                if cur.finished_at:
                    ui.badge("已下齐").props("color=teal").tooltip(finished_tip(cur, "点右边的『继续订阅』"))
            if on_close:
                ui.button(icon="close", on_click=on_close).props("flat round dense").classes(
                    "shrink-0")

        # 元操作行：重新识别 / 忽略 —— 放在标题下面（dense 收紧内边距 + -ml-1 抵消残余左边距，跟标题左缘对齐）
        with ui.row().classes("items-center gap-3 flex-wrap -ml-1"):
            ui.button("重新识别", icon="refresh", on_click=_enrich).props(
                "flat dense").classes("btn-sm")
            ui.button("编辑季度", icon="event", on_click=_edit_quarter).props(
                "flat dense").classes("btn-sm").tooltip(
                "手动改归档季度（bgm 定错/无放送日时兜底）。改后可把已下文件移到新目录。")
            if cur.rejected:
                ui.button("恢复订阅", icon="undo", on_click=_restore).props(
                    "unelevated dense color=primary").classes("btn-sm")
            else:
                if cur.finished_at:
                    ui.button("继续订阅", icon="autorenew", on_click=_resubscribe).props(
                        "unelevated dense color=primary").classes("btn-sm").tooltip(
                        "清掉『已下齐』标记并【不再自动判它下齐】。bgm 的总集数少记一集时用这个——"
                        "否则下一轮巡检立刻又会把它判回已下齐。")
                ui.button("忽略本番", icon="block", on_click=_reject).props(
                    "flat dense color=grey").classes("btn-sm")

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
                          on_change=_set_source).classes(
                    "w-full sm:w-auto sm:min-w-52").tooltip(
                    "『按优先级』= 多源自动挑、缺集用别的源兜底；"
                    + (f"选某个组 = 锁定，之后只下这个组；它停更满 {config.ANIME_LOCK_FALLBACK_DAYS} 天、"
                       "核对无误的缺集才自动用别的组补一份，其余缺集自己来点下载"
                       if config.ANIME_LOCK_FALLBACK_DAYS > 0 else
                       "选某个组 = 锁定，之后只下这个组，它缺的集不兜底（自己来点下载）"))
                _kwin = ui.input(value=(cur.pref_keyword or ""), label="版本",
                                 placeholder="繁日/简日/1080p…").classes(
                    "w-36").tooltip(
                    "版本关键词：在『下载源』基础上，再只下种子原名含此词的版本（繁日/简日/画质等，大小写不敏感）。"
                    "留空=不限；硬过滤、缺此版本的集不兜底。回车或点输入框外生效。")
                _kwin.on("blur", lambda: _set_keyword(_kwin.value, _kwin))
                _kwin.on("keydown.enter", lambda: _set_keyword(_kwin.value, _kwin))
            if not cur.rejected and not cur.confirmed:
                ui.button("确认下载", on_click=_approve).props("unelevated color=primary")
            _dln = ui.button("下载该源", icon="download", on_click=_download).props("flat")
            _dln.set_enabled(config.QB_ENABLED)
            _dln.tooltip("qB 未启用，去设置页开启后可下载" if not config.QB_ENABLED
                         else "按左边『下载源』下：锁了某源→下该源缺的每一集；『按优先级』→每集下应下的那份，已下的跳过。"
                              "只下正集——特别篇/未知集要对准下面那一条点『下载』")
            # 【与同一行的『下载该源』同档】这三个按钮是一组控制条，不是列表行。
            # dense 在本项目的按钮语法里表示"行内"（＝12px，见 layout.py 的 .btn-sm），
            # 写在这里会让同一行出现 14px 与 12px 两档。
            _btn_bf_loose = ui.button("补齐该源", icon="playlist_add", on_click=_backfill_loose).props(
                "flat")
            _btn_bf_loose.tooltip("去 nyaa/Mikan 按名搜『当前下载源』的种子补漏收（季度过滤，你人工审核）。"
                         "入库后转『待确认』，点『确认下载』才下。")
            _btn_bf_auto = ui.button("自动补齐", icon="auto_awesome", on_click=_backfill_auto).props(
                "flat")
            _btn_bf_auto.tooltip("同『补齐该源』，但额外用番名过滤挡掉同名衍生作/别的季。"
                                 "在追的番补到就直接下（不打回待确认）；没在追的番照旧转『待确认』。")

        ep_section()   # 分集区单独可刷新（改版本关键词只刷它），理由见 ep_section

    @ui.refreshable
    def ep_section():
        """分集区（标题行 + 各类横幅 + 逐条种子），单独可刷新。

        【为什么从 body 里拆出来】(R39 U10) 改『版本』关键词(_set_keyword)以前是 body.refresh()：
        整块重建会把上面操作行的『确认下载』『下载该源』一起删掉换新 id。用户填完关键词直接去点
        这两个按钮时，浏览器先发 blur、再发 click——click 带的是旧 id，Client.handle_event
        按 id 查不到元素就静默丢弃：点了，什么都没发生，也没有任何提示。
        关键词只影响这一段的标注（将下载/备用项/锁源缺集…），上面那几块除了输入框自己都不读
        pref_keyword，所以改关键词只刷这一段（输入框不重建，回车后焦点也还在）。
        其余改动照旧经 _after() 刷整个 body（会连带重建这一段）。
        自己取 cur/eps：单独刷新时不能用 body 那次的旧快照。
        【残留】逐条的『下载』『排除』等按钮就在这一段里、照样会被重建：填完关键词直接点它们，
        仍会丢一次点击（要再点一下）。
        """
        cur = anime.get_anime(anime_id)
        if cur is None:
            return
        eps = anime.list_episodes(anime_id)
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
            # 【只陈述事实，不下"这是两套编号"的结论】原文案断言"多半是某个源用绝对集号、
            # 另一个用季内集号，同一集会被下两份"——真库实测它 8 次命中里只有 2 次成立
            # （#6/#60/#25/#47/#23 都是【一个源】用跨季连续编号数过了总集数，每集只有一个号）。
            # 试过四种收紧判据，每种在真库上都有可观误报，与 _foldable 记载的结论一致。
            # 所以改成把【逐源的集号区间】摆出来：区间完全错开 = 真的两套编号；互相重叠 = 只是连续编号。
            _rng = anime.episode_ranges_by_source(anime_id)
            _rng_txt = "；".join(
                f"{src} 第{ep_str(lo)}集" if lo == hi else f"{src} 第{ep_str(lo)}–{ep_str(hi)}集（{n}集）"
                for src, lo, hi, n in _rng)
            warn_banner(
                f"集号超出 bgm 记载：本番共 {cur.total_episodes} 集，但有种子标着第 "
                + "、".join(ep_str(e) for e in _conf) + " 集。\n"
                + f"各源的集号范围：{_rng_txt}\n"
                + "判断方法：若某两个源的区间【完全错开】（如 A 是 13–19、B 是 1–7），"
                  "那是两套编号并存，同一集会各下一份到同一目录——锁定其中一个源即可避免。"
                  "若各源区间互相重叠（如都落在 1–33 内），那只是这个组用了跨季连续编号，不会重复。"
                + ("\n本番两套编号的取值范围重叠，系统【不会】自动折算，需要合并请手动改集号。"
                   if (cur.ep_offset and cur.total_episodes and cur.ep_offset < cur.total_episodes)
                   else "\n若某个源的标题里带 “16(88)” 这类双编号，系统会自动学到偏移量并从此自行折算。"))
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
        # 【缺集】按开播日推算应已播出、库里却一条种子都没有的集（判据见 core.anime.missing_episodes）
        _miss = anime.episode_gaps([cur]).get(cur.id) or []
        # (r39 F8) 槽位被别季旧种子占着的那几集：补齐拿回来也会被『同集已有』挡住，不能再叫人去点补齐（见 slot_held_gaps）
        _held = anime.slot_held_gaps({cur.id: _miss}).get(cur.id, []) if _miss else []
        _free = [e for e in _miss if e not in _held]
        if _free:
            warn_banner(
                f"缺第 {ep_ranges_text(_free)} 集：按 bgm 开播日推算早该播了，库里却一条种子都没有。"
                "采集停过一阵（RSS 只留最近几天，停久了那几集就滚出去了）或锁定的源中途断更都会这样。"
                "点『自动补齐』去搜（在追的番补到就直接下）：当前下载源搜不到时，会列出别的组有哪些集，挑一个就能补。")
        # (E-77 C) 出路就在横幅上：把占槽的旧行标成『不属于本季』。条数与 mark_slot_impostors_foreign 同一个判据
        # （_foreign_markable）——它会把整部番里认得出的占槽行一次标完，不只是缺的这几集。
        _nmark = sum(1 for t in eps if anime._foreign_markable(cur, t))
        if _held:
            _held_txt = (f"缺第 {ep_ranges_text(_held)} 集：这几集的槽位被别的季的旧种子占着（同集号、发布比本季首播早得多），"
                         "补齐也下不了 —— 补回来的本季这几集会被『同集已有』挡住；删掉旧种子也不行（会被当成这一集不要了）。")
            if _nmark:
                warn_banner(_held_txt + f"点右边把这 {_nmark} 条旧种子标为『不属于本季』：文件不动、可逐条撤销，"
                            "本季这几集就会照常自动下。",
                            action=(f"把占槽的 {_nmark} 条旧种子标为不属于本季", "playlist_remove",
                                    _mark_impostors(_nmark)))
            else:   # 占槽行正在交付中（标不了）：等它落定再来
                warn_banner(_held_txt + "占槽的那条正在交付中，等它落定后再回来把它标为『不属于本季』。")
        else:
            # #6 的真实形状：本季那几集早就入了库（被同集闸压成 skipped / 停在待下），算"见过"、不报缺集，
            # 上面那条横幅就不出 —— 批量按钮在这里同样给一份。只在占槽行【真挡着】某一集时出（_covered 的那种）。
            _block = sorted({int(t.episode) for t in eps if anime._foreign_markable(cur, t) and anime._covered(t)})
            if _block:
                warn_banner(
                    f"第 {ep_ranges_text(_block)} 集的位置被别的季的旧种子占着（同集号、发布比本季首播早得多）："
                    "本季这几集的种子（已经来了的和以后来的）会被『同集已有』挡住、不会自动下；"
                    "删掉旧种子也不行（会被当成这一集不要了）。"
                    f"点右边把这 {_nmark} 条旧种子标为『不属于本季』：文件不动、可逐条撤销。",
                    action=(f"把占槽的 {_nmark} 条旧种子标为不属于本季", "playlist_remove",
                            _mark_impostors(_nmark)))
        # 【锁定源落后】别的组已有后续集、锁定源迟迟不跟（判据见 core.anime.locked_out_episodes）
        _lo = anime.locked_out_episodes([cur]).get(cur.id)
        if _lo:
            warn_banner(
                f"锁定的下载源『{cur.pref_source}』停在第 {_lo['lock_max']} 集，"
                f"{'、'.join(_lo['groups'])} 已经发了第 {ep_ranges_text(_lo['eps'])} 集，"
                + (f"锁定组停更满 {config.ANIME_LOCK_FALLBACK_DAYS} 天、核对无误的会自动用别的组补一份；"
                   "如果锁定组其实发了（采集漏收），先点『自动补齐』。不想等也"
                   if config.ANIME_LOCK_FALLBACK_DAYS > 0 else "因为锁了源一集都不会下。")
                + "可以只用别的组补上锁定源之后这几集（不改锁），或改回按优先级。",
                action=[("只补这几集", "download", _download_locked_out),
                        ("改回按优先级", "lock_open", _unlock_source)])
        if not eps:
            ui.label("（还没有种子）").classes("text-gray-500")
            return
        # 两个问题各查各的：pending 行问『后台自动会下吗』(只算 pending，与 flush 同口径)，
        # error 行问『点补下会挑中吗』(含 error)。混用一个集合会让高优先级的 error 顶掉
        # 同集真正会被自动下的 pending，把『将下载』标到不会下的那条上。
        plan = anime.download_plan(anime_id)
        backfill_plan = anime.download_plan(anime_id, for_backfill=True)
        # 这一集是否『有着落』：将下(plan) 或 已有一份(HAVE_STATUSES：已下/在下/停滞)。与 flush 同口径——
        # stalled 也算有着落(不再误报缺集，B4)；deleted 不在这里算：只删了重复份的同集照常会下，
        # "该集已删"的 pending 行由下面的 _dropped 单独标，走不到用 covered 的那一支（E-73）。
        # 标了『不属于本季』的行（E-77 C）不算这一集有着落，与 core 的 _covered 同口径
        covered = {t.episode for t in eps
                   if t.id in plan or t.id in backfill_plan
                   or (t.status in anime.HAVE_STATUSES and not t.foreign_season)}
        # 【该集已删】(E-73) 组内有"该集已删"(deleted+ep_dropped) 行、且一份都不在盘上的集：后台任何自动路径
        # 都不再为它下别的版本。分组键与 flush / 计划 / 即时同集闸同口径（dedup_key，歧义段带源）。
        _amb = anime.ambiguous_range(cur)
        # 『不属于本季』的行两边都不算（它的标记不起作用、它在盘上也不算本季这一集有货），同 core 的 _covered
        _dropped = ({anime.dedup_key(_amb, 0, t.episode, t.source) for t in eps
                     if t.status == "deleted" and t.ep_dropped and not t.foreign_season}
                    - {anime.dedup_key(_amb, 0, t.episode, t.source) for t in eps
                       if t.status in anime.HAVE_STATUSES and not t.foreign_season})
        # 单部作品（剧场版/WEB 等只有 1 集）的 -2 按特别篇算、标 SP —— 与仪表盘同一判据（engine.is_special_ep）
        _single = engine.is_single_work(cur.platform, cur.total_episodes)
        for t in eps:
            ep_txt = ep_label(t.episode, _single)
            _sp = engine.is_special_ep(t.episode, _single)
            with ui.column().classes("w-full gap-0 py-1").style(
                    "border-bottom:1px solid rgba(255,255,255,.08)"):
                # 第一行：集号 · 字幕组 · 时间 同一行居中（天然竖直齐平），状态/按钮 space() 推到最右
                with ui.row().classes("items-center gap-3 w-full text-sm flex-wrap"):
                    if t.status in engine.DOWNLOADABLE_STATUSES:  # 待下/失败的集号都可点改：不止 -2，
                        _neg = t.episode == -2 and not _sp                # 解析也可能写错正整数(把分辨率/季号当集号)
                        ui.label(ep_txt).classes(
                            "shrink-0 cursor-pointer hover:underline"
                            + (" text-blue-400" if _neg else "")).on(
                            "click", _set_ep(t.id, t.episode)).tooltip(
                            "集号没解析出来；点这里补集号或设为特别篇" if _neg
                            else "特别篇（SP）；其实是正片？点这里改集号" if _sp else "集号不对？点这里改")
                    else:
                        ui.label(ep_txt).classes("shrink-0")              # 主要信息：集号（已下等不可改）
                    ui.label(t.source or "—").classes("shrink-0")         # 主要信息：字幕组（同色）
                    if t.lock_fallback:   # 锁定源停更时后台替你补的那一份（E-74）：让人事后找得到、删得掉
                        ui.badge("兜底").props("color=teal outline").tooltip(
                            "锁定的组停更了一阵，后台用这个组给这一集补了一份。锁定组以后补发这一集会再自动下它的版本；"
                            "这一份不会自动删，不要了点右边删除。"
                            "锁定组补发之前别删：删了等于这一集不要了，它的版本也不会再自动下")
                    if t.foreign_season:   # (E-77 C) 用户标的：别的季的，不占本季的集号
                        ui.badge("不属于本季").props("color=grey").tooltip(
                            "你标过：这一条是别的季的。它不占本季这一集的位置（本季那一集照常自动下），"
                            "文件不动、还在原目录；后台不会自动下它、不会拿它做修正版替换，搬迁时也不会清它的状态。"
                            "标错了点右边『撤销』")
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
                            # 特别篇不用处理（中性色），未知集才要人工补集号（紫色）——与仪表盘两个桶同口径
                            _neg_lab, _neg_col, _neg_tip = (
                                ("特别篇", "blue-grey",
                                 ("bgm 判定这部是单部作品（剧场版/WEB 等，总共 1 集），这条标题里本来就没有集号，按特别篇算。"
                                  if t.episode == -2 else "特别篇/OVA。")
                                 + "后台不自动下（多组多版本，自动挑一份常挑错），不用处理；要它就点右边『下载』")
                                if _sp else
                                ("未知集", "purple",
                                 "批量/集号没解析出来，后台不自动下。点左边『第?集』补集号或设为特别篇，或下方排除"))
                            ui.badge(_neg_lab).props(f"color={_neg_col}").tooltip(
                                _neg_tip
                                + (_retry_tip.replace("后重试", "后仍不会自动重发，需人工下")
                                   if t.retry_at else ""))
                        elif t.foreign_season:
                            # 【要排在 retry / plan / covered 之前】(E-77 C) 任何自动路径都不下它：落进『重试中』会承诺
                            # "X 之后自动重发"，落进『备用项』/『锁源缺集』则说的是本季这一集的事 —— 都不对
                            ui.badge("不自动下").props("color=grey").tooltip(
                                "标了『不属于本季』，后台任何自动路径都不会下它；要就点右边『下载』")
                        elif not cur.confirmed and not cur.rejected:
                            # 【subscribed_where 的第三半】flush 的判据是"已确认 且 未忽略 且
                            # （开停订时）未判完结"。上面那条完结分支与 download_plan 里的 rejected
                            # 闸各补了一半，只有 confirmed 这一半没跟上——于是"待确认"番的每一条
                            # pending 都会拿到 plan、被标成蓝色『将下载』，tooltip 还写着"补下/自动下会下它"。
                            # 而 flush 一条都不挑，点『下载该源』也直接返回 0：
                            # 同一行在仪表盘『新入库』里显示的是橙色『待确认』，两个界面结论相反。
                            # 判序与 core.anime.pending_breakdown 对齐（未知 → 未确认 → 已完结 → 计划）。
                            ui.badge("待确认·不自动下").props("color=orange").tooltip(
                                "这部番还没确认，后台不会自动下它的任何一集。"
                                "点上方的『确认下载』放行，或点右边『下载』只要这一集。")
                        elif cur.rejected:
                            # 同理：『超期忽略』(apply_start_date_filter 置 rejected 但不动种子状态)
                            # 的番，pending 行还在，落进下面的 else 会被说成"锁定源过滤光了"——同一句错话。
                            ui.badge("已忽略·不自动下").props("color=grey").tooltip(
                                "这部番在『已忽略』里，后台不会自动下它。"
                                "到上方点『恢复订阅』，或点右边『下载』只要这一集。")
                        elif cur.finished_at and config.ANIME_FINISH_UNSUB:
                            # 【要排在 retry / plan / covered 【全部】之前】这部番已判完结且开了停订 ⇒
                            # 它整个掉出 subscribed_where，flush 一条都不会挑，所以：
                            #  · 落进下面的 else 会被解释成"锁定源/版本过滤光了"——用户多半两样都没设过；
                            #  · 落进上面的『重试中·第N次』会承诺"X 时之后自动重发"——那次重发【永不发生】，
                            #    因为整部番不在候选里。这条假承诺比前一条更糟：它让人干等。
                            # 判序与 core.anime.pending_breakdown 的『已完结』单列一档对齐（那边是对的）。
                            ui.badge("已停订·不自动下").props("color=teal").tooltip(
                                "这部番已按 bgm 总集数下齐，且设置页开着『下齐后停止自动下新集』，所以它的新集"
                                "一律不自动下（含失败后的自动重发）。要这一集就点右边『下载』；"
                                "若其实还没播完，点上方的『继续订阅』。")
                        elif anime.dedup_key(_amb, 0, t.episode, t.source) in _dropped:
                            # 【要排在 retry / plan / covered 之前】(E-73) 删除时这一行可能正在退避队列里（retry_at 非空），
                            # 落进下面的『重试中』会承诺"X 之后自动重发"——flush 与同集闸都认这一集有着落，永不重发。
                            ui.badge("该集已删·不自动下").props("color=grey").tooltip(
                                "你删掉了这一集的最后一份，后台不会再自动下它的其它版本；要就点右边『下载』")
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
                            # 与仪表盘/新入库同一个词（r37 #2：那两处以前叫它『备用项』，说法正好相反）
                            ui.badge("锁源缺集").props("color=orange").tooltip(
                                "这一集在当前锁定源/版本下没有可下的，只剩被过滤掉的版本；"
                                "要它就点右边下载，或用上方横幅的『只补这几集』")
                    elif t.status == "error":
                        # error 行问的是『点补下会不会挑它』——后台自动下从不重试 error，
                        # 故这里必须用 backfill_plan，不能用只含 pending 的 plan
                        _bf = t.id in backfill_plan
                        _why = f"{t.fail_reason}；" if t.fail_reason else ""
                        _tried = f"（已自动重试 {t.retry_count} 次仍不行）" if t.retry_count else ""
                        if not anime.is_subscribed(cur):
                            # 【E-54，2026-09-02 拍板选 C：契约不动、改显示】download_plan 按契约只排除
                            # rejected + 停订，`confirmed` 由调用方过滤 —— 于是待确认番的 error 行也在
                            # backfill_plan 里，上面会把它标成『失败·可补下』、tooltip 让人去点『下载该源』，
                            # 而 download_pending_for_anime 第一行就是 `if not is_subscribed(a): return 0`。
                            # 同一行在仪表盘『新入库』里（先过 confirmed_anime_ids）显示的是红色『失败』。
                            # 这里按 is_subscribed 分叉，判据与 pending 分支那三条（待确认/已忽略/已停订）同源；
                            # 已忽略/已停订的番 _bf 本来就是 False，也走这里 —— 让 tooltip 说清是哪道闸，
                            # 而不是一句"下载失败过"。
                            _gate = ("这部番还没确认" if not cur.confirmed else
                                     "这部番在『已忽略』里" if cur.rejected else "这部番已下齐并停订")
                            ui.badge("失败").props("color=red").tooltip(
                                _why + f"{_gate}，『补下』不会挑它；要这一集就点右边『下载』{_tried}")
                        else:
                            ui.badge("失败·可补下" if _bf else "失败").props(
                                f"color={'orange' if _bf else 'red'}").tooltip(
                                _why + (f"点右边『下载』或『下载该源』手动重试{_tried}"
                                        if _bf else f"下载失败过{_tried}"))
                    elif t.status == "stalled":
                        # 【停滞要说清为什么、以及它不会自己动】(E-3) 三个来源（qB 报错 / 长期无推进 /
                        # 从 qB 消失但盘上有半成品）都把原因写进 fail_reason；stalled 有意不进补下、
                        # 不自动换源，所以要告诉用户出路在右边的按钮上。
                        ui.badge("停滞").props(f"color={SEVERITY_COLOR['stalled']}").tooltip(
                            (f"{t.fail_reason}；" if t.fail_reason else "")
                            + "半成品文件在下载目录里。停滞集不会自动重下/换源："
                              "要重试点右边『下载』；要换源：在同集别的版本那一行点『下载』；"
                              "点『删除』＝这一集不要了，后台不会再自动下")
                    else:  # 无 qB 实时态：刚交付未同步→下载中；其余(已下完/跳过/已删/已排除)按状态
                        # 【严重度色取公共表】写死 blue-grey 会让同一条 stalled/error 在仪表盘是
                        # 橙红、在详情页是中性灰——而两边渲染的是同一列 status。
                        ui.badge(torrent_status_cn(t.status, t.qb_progress, t.qb_synced_at)).props(
                            f"color={SEVERITY_COLOR.get(t.status, 'blue-grey')}")
                    if t.status == "excluded":  # 已排除：给『恢复』放回待下
                        ui.button("恢复", icon="undo", on_click=_unexclude(t.id)).props(
                            "flat dense color=primary").classes("btn-sm").tooltip(
                            "放回待下，重新参与下载/去重")
                    if t.status in engine.DOWNLOADABLE_STATUSES:  # 未下载的才可直接排除
                        ui.button("排除", icon="block", on_click=_exclude(t.id)).props(
                            "flat dense color=grey").classes("btn-sm").tooltip(
                            "不想要这条：从待下直接排除（不删文件，只改状态；可撤销）")
                    # (E-77 C)『不属于本季』只由这里（和缺集横幅上的批量按钮）产生，后台永不自动打。
                    # 只对看着像别季的正集给按钮（判据见 anime.foreign_season_suspect）。
                    # 交付中的不给（set_foreign_season 也不改它）：等它落定。
                    if t.foreign_season:
                        ui.button("撤销", icon="undo", on_click=_set_foreign(t.id, False, t.episode)).props(
                            "flat dense color=primary").classes("btn-sm").tooltip(
                            "撤销『不属于本季』：这一条重新占用本季这一集的位置")
                    elif t.status != "downloading" and anime.foreign_season_suspect(cur, t):
                        ui.button("不属于本季", icon="playlist_remove",
                                  on_click=_set_foreign(t.id, True, t.episode)).props(
                            "flat dense color=grey").classes("btn-sm").tooltip(
                            "这一条是别的季的（同集号的旧正片）：标记后不再占用本季这一集的位置，文件不动，可撤销")
                    # 下过/在下/停滞(盘上有文件)都可删（delete 函数亦接受 stalled）。交付中的占位行不给按钮：
                    # delete_anime_torrent 对 downloading 直接返回 False，而它身上可能还挂着旧的 archived_at，
                    # 会被渲染成"已归档：只能标记已删"这种错话（E-49 后占位段不清归档标记）。
                    if t.status in engine.HAVE_STATUSES and t.status != "downloading":
                        ui.button(icon="delete_forever",
                                  on_click=_del_one(t.id, t.archived_at, t.save_path,
                                                    t.episode is not None and t.episode < 0 and not _single,
                                                    t.foreign_season)).props(
                            "flat dense color=negative").classes("btn-sm").tooltip(
                            "已归档：不在 qB，只能标记已删，文件需你手动清理" if t.archived_at
                            else "删除这一集的文件（qB+硬盘，不可撤销）")
                    _dlb = ui.button("下载", icon="download", on_click=_force(t.id, t.archived_at)).props(  # 下载放最后
                        "flat dense").classes("btn-sm")
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
        # 【连外层一起刷】(r37 #11) 待确认卡片上的『下载源』下拉是建面板时的快照：只刷详情的话，回到卡片
        # 点『确认下载』会把卡片上那个旧值（往往是"按优先级"）带回去，把刚锁的源静默解掉。
        _after()
        if e.value:
            ui.notify(f"已锁定：之后只下 {e.value}（它停更满 {config.ANIME_LOCK_FALLBACK_DAYS} 天、"
                      "核对无误的缺集才自动用别的组补）" if config.ANIME_LOCK_FALLBACK_DAYS > 0 else
                      f"已锁定：之后只下 {e.value}（缺集不兜底，自己来点下载）", type="warning")
        else:
            ui.notify("已改回『按优先级』：多源自动挑、缺集用别的源兜底", type="positive")

    def _set_keyword(val, inp):
        v = (val or "").strip()
        # 【回写 strip 后的值】(r39 F37) 输入框不再随 body.refresh() 重建，不回写的话填『 繁日 』后
        # 库里是『繁日』、框里还是『 繁日 』。放在『没改』判断之前：库里已是『繁日』时再填带空格的也要规整。
        # 值相同时 NiceGUI 不发更新，回车那一路焦点/光标不受影响
        inp.value = v
        # 【没改就什么都不做】blur 每次失焦都触发——点一下输入框再点别处，本来什么都没改，
        # 却会写一次库、重建一遍分集区（正在滚动/展开的状态全丢）、还弹一条橙色警告。
        cur_kw = getattr(anime.get_anime(anime_id), "pref_keyword", "") or ""
        if v == cur_kw:
            return
        anime.set_pref_keyword(anime_id, v)
        # 【只刷分集区，不刷 body】理由见 ep_section：body.refresh() 会删掉同一行的按钮，
        # 紧跟在 blur 后面到达的那次点击就被吞了。
        ep_section.refresh()
        ui.notify(f"版本限定『{v}』：只下原名含此词的版本（缺此版本的集不兜底）" if v
                  else "已取消版本限定", type="warning" if v else "positive")

    async def _enrich(e):
        # 识别成功会写入 bgm 名/季号 → 归档目录随之改变，必须和绑定(_bind)、列表页那两条路径
        # 一样问一句要不要搬迁。剧场版侧的同名 _enrich 本来就是这么写的（pages/movies.py:213）；
        # 番剧详情页这条一直漏着，已下的集会被静默遗弃在旧目录、同一部番裂成两个文件夹。
        # 【走 busy_action】识别的时间预算是 120 秒（enrich._RESOLVE_BUDGET）——
        # 期间按钮毫无反应，用户会连点，于是并发跑好几轮、重复弹搬迁确认框。
        async def _go():
            old_path = anime.anime_save_path(anime_id)
            ok = await anime.manual_enrich(anime_id)   # 含清零后台重试计数（三个入口同口径）
            _after()
            ui.notify("识别成功" if ok else "未识别到（Mikan/bgm 没有或查不到）",
                      type="positive" if ok else "warning")
            if ok:
                await maybe_relocate_anime(anime_id, old_path, _after)
        await busy_action(e.sender, f"enrich:{anime_id}", _go, fail="识别失败")

    async def _bind():
        dlg = page_dialog()
        if dlg is None:
            return
        with dlg, ui.card().classes("gap-2"):
            ui.label("绑定 bgm").classes("font-bold")
            ui.label("自动认错了就把正确的 bgm 链接或 ID 填这——直接取权威元数据覆盖。").classes(
                "text-xs text-gray-400")
            inp = ui.input(placeholder="bgm.tv/subject/464376 或 464376").props("autofocus").classes("w-80 min-w-0 max-sm:w-full")
            inp.on("keydown.enter", lambda: dlg.submit(inp.value))   # 有 autofocus 就该能回车提交
            with ui.row().classes("gap-2 justify-end w-full"):
                ui.button("取消", on_click=lambda: dlg.submit(None)).props("flat")
                ui.button("绑定", icon="link",
                          on_click=lambda: dlg.submit(inp.value)).props("unelevated color=primary")
        try:
            val = await wait_dialog(dlg)
        finally:
            dlg.delete()
        if not val:
            return
        bid = parse_bgm_id(val)
        if not bid:
            ui.notify("没认出 bgm ID（粘 bgm.tv/subject/数字 或纯数字）", type="warning")
            return
        # 【弹窗关掉之后这一段才是长操作】bind 会走 fetch_by_id（预算 120 秒），
        # 期间页面没有任何反馈，用户会以为没点上而再点一次。走 busy_action 去重。
        # 【回显闸也在里面】(R27) `require_bind_confirm` 第一句就是同一个
        # `fetch_by_id`（同样 120 秒预算），把它留在 busy_action 外面等于这道防抖
        # 只覆盖了整条链路的后一半。
        async def _go():
            # 【绑定前回显】可能删掉另一条番记录 —— 闸门与列表页共用一份，见 layout.require_bind_confirm
            if not await require_bind_confirm(anime_id, bid):
                return
            old_path = anime.anime_save_path(anime_id)   # 重绑前的归档路径（bind 可能改名/季度/季号）
            _rep: dict = {}    # 收『这次绑定冻结了哪些字段』，见 core 里 report 出参的说明
            ok = await anime.bind_anime_bgm(anime_id, bid, report=_rep)
            _after()
            # 冻结了就别报绿，理由同 pages/anime.py 那一处
            _msg = ("绑定失败：ID 不存在或取不到 bgm 数据" if not ok
                    else ("已绑定，但这一部有【已归档】的文件搬不动 —— 片名/目录保持原样，只换了 bgm 与封面简介。要一并改名：先在 qB 里重新添加那些种子（或手动移动文件），再绑一次" if _rep.get("frozen")
                          else "已绑定并识别 ✓（回到待确认，去点确认下载）"))
            ui.notify(_msg, type=("negative" if not ok
                                  else "warning" if _rep.get("frozen") else "positive"))
            if ok:
                await maybe_relocate_anime(anime_id, old_path, _after)
        await busy_action(None, f"bind:{anime_id}", _go, fail="绑定失败")

    async def _edit_quarter():
        cur = anime.get_anime(anime_id)
        dlg = page_dialog()
        if dlg is None:
            return
        with dlg, ui.card().classes("gap-2"):
            ui.label("编辑归档季度").classes("font-bold")
            ui.label("内部键 = 两位年 + 季母（A冬 B春 C夏 D秋），如 26A=2026年冬。"
                     "改后可把已下文件移到新目录。").classes("text-xs text-gray-400")
            inp = ui.input("季度键", value=(cur.quarter if cur else "")).props("autofocus").classes("w-60 min-w-0 max-sm:w-full")
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
                          on_click=lambda: dlg.submit(inp.value)).props("unelevated color=primary")
        try:
            val = await wait_dialog(dlg)
        finally:
            dlg.delete()
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
        n = await subscribe_gated(anime_id, "confirm")     # E-52 的闸：『确认下载』也是入口之一
        if n is None:
            return
        _after()
        ui.notify(f"已确认，补下 {n} 集", type="positive")

    def _reject():
        anime.reject_anime(anime_id)
        _after()
        ui.notify("已忽略，移到『已忽略』页", type="positive")

    async def _restore():
        n = await restore_anime_gated(anime_id)
        if n is None:
            return          # 用户在 E-52 的闸上取消了
        _after()
        ui.notify(f"已恢复到『订阅中』，补下 {n} 集", type="positive")

    async def _resubscribe():
        """继续订阅：清完结标记 + 记下别再自动判它完结，顺手把攒下的待下补一遍。"""
        n = await subscribe_gated(anime_id, "resubscribe")  # E-52 的闸
        if n is None:
            return
        _after()
        ui.notify(f"已继续订阅，补下 {n} 集（此后不再自动判它下齐）" if n
                  else "已继续订阅（此后不再自动判它下齐）", type="positive")

    async def _download():
        """『下载该源』/『补下全部』：把这部番下的 pending/error 补一遍。

        【0 集不能报绿】(R31) `download_pending_for_anime` 第一行就是
        `if not is_subscribed(a): return 0` —— 而这一页对**待确认**的番照样把 error 行
        标成橙色『失败·可补下』（标注侧用的 `download_plan` 只排除 rejected、
        **不看 confirmed**，那是 D9 记录在案的契约，不在这里改）。
        于是用户按提示点下去，什么都没发生，却收到一句绿色的"已触发下载 0 集"。
        全站的口径是"提示只描述【已经发生的事实】"，所以这里要说清楚**为什么**是 0。
        """
        n = await anime.download_pending_for_anime(anime_id)
        _after()
        if n:
            ui.notify(f"已触发下载 {n} 集", type="positive")
            return
        cur = anime.get_anime(anime_id)
        if cur is not None and not anime.is_subscribed(cur):
            why = ("这部番还在【待确认】，批量补下只对已确认的番生效 —— "
                   "先点『确认』，或对准某一条点右边的『下载』（单条下载不受此限）"
                   if not cur.confirmed else
                   "这部番已被【忽略】，先『恢复订阅』"
                   if cur.rejected else
                   "这部番已【下齐】且开着停订，先点『继续订阅』")
            ui.notify(f"没有触发下载：{why}", type="warning")
        elif anime.download_plan(anime_id, for_backfill=True):
            # 【0 集还有第三种成因，别都说成"没得下"】(R31) 闸没挡住、候选也确实存在，
            # 那就是这一轮没放行 —— 最常见的是 qB 连不上
            # （`download_pending_for_anime` 那边会写一行 WARNING）。
            # 说成"该下的都下过了"是假话，而用户看不到日志。
            ui.notify("有待补的集，但这一轮一条都没放行 —— 多半是 qB 此刻连不上，"
                      "去 /logs 看最近一条 WARNING；qB 恢复后会自动继续", type="warning")
        else:
            ui.notify("没有可补下的集（该下的都下过了，或都被锁定源/版本关键词挡住）",
                      type="info")

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
        # 『自动补齐』对在追的番补到就直接下、不打回待确认（core.anime._backfill_source_inner），不必问
        if cur is not None and not name_filter and cur.confirmed and not cur.rejected:
            # 已确认番：补齐入库新种子会把整部番退回『待确认』，后台从此暂停自动下新集，直到重新确认——先告知
            if not await confirm(
                    "补齐会暂停这部番的自动下载",
                    "这部番当前『已确认』、正在自动追。补齐入库新种子后会把它退回『待确认』，"
                    "后台将暂停自动下新集，直到你去『待确认』页重新点确认。继续？",
                    ok_label="继续补齐", ok_icon="playlist_add", ok_color="primary"):
                return
        ui.notify("正在搜索补齐…（去 nyaa/Mikan 按名搜，请稍候）", type="info")
        try:
            res = await anime.backfill_source(anime_id, name_filter)
        except Exception as e:            # 兜住 fetch 之外的意外，别逃逸崩掉处理器
            ui.notify(f"补齐出错：{e}", type="negative")
            return
        _after()
        # (r39 DEC1x) 自动补齐不收没有发布时间的目标组结果（判断不了是不是前几季的同号集）：入库与没入库都要说一句
        _nt = (f"；另有 {res['no_time']} 条没有发布时间、判断不了是不是本季的，自动补齐没收；"
               "要的话用『补齐该源』（会转待确认人工过目）" if res.get("no_time") else "")
        if res.get("error"):
            ui.notify(res["error"], type="warning")
        elif res["ingested"]:
            if res.get("direct"):
                n = res.get("downloaded", 0)
                ui.notify((f"补入库 {res['ingested']} 条（搜到 {res['found']}），已下发 {n} 集"
                           if n else
                           f"补入库 {res['ingested']} 条（搜到 {res['found']}），但没有新下发："
                           "补到的集已经有了、是特别篇，或不合『版本』关键词；qB 连不上时也会这样") + _nt,
                          type="positive" if n else "info")
            elif res.get("to_confirm"):
                ui.notify(f"补入库 {res['ingested']} 条（搜到 {res['found']}）→ 已转『待确认』，去点『确认下载』" + _nt,
                          type="positive")
            else:   # rejected 番：入库了但订阅态没变，不会自动下
                ui.notify(f"补入库 {res['ingested']} 条（搜到 {res['found']}），但本番仍在『已忽略』；"
                          "需先『恢复订阅』这些才会下载" + _nt, type="warning")
        else:
            # 归属检查跳掉的那些要单独说：它与两个补齐按钮的松紧【无关】，换另一个按钮再点结果一样，
            # 不点明的话用户只会以为"过滤太严"而白试一遍。
            _st = (f"；其中 {res['stolen']} 条对照表显示属于别的番（详见 /logs）" if res.get("stolen") else "") + (
                f"；另有 {res['too_old']} 条发布时间不在本季播出期（多半是前几季的同号集），没收" if res.get("too_old") else "")
            ui.notify(f"当前下载源没有新种子可补（搜到 {res['found']}，都已有或被季号/名字过滤{_st}）"
                      + _nt + ("；别的组有，见弹出的列表" if res.get("others") else ""), type="info")
        if res.get("others"):
            await _offer_other_groups(res["others"])

    async def _download_locked_out():
        plan = anime.locked_out_candidates(anime_id)
        if not plan:
            ui.notify("没有可补的集（别的组的那几集已经下过了，或都不在待下里）", type="info")
            return
        if not await confirm(
                "用别的组补锁定源之后的集？",
                f"第 {ep_ranges_text(sorted(int(e) for e in plan))} 集各下一份（每集按源优先级挑："
                + "、".join(sorted({ts[0].source for ts in plan.values()})) + "）。"
                "下载源仍锁着，以后的新集还是只下锁定的组。",
                ok_label="补这几集", ok_icon="download", ok_color="primary"):
            return
        n = await anime.download_locked_out(anime_id)
        _after()
        ui.notify(f"已下发 {n} 集" if n else "一集都没放行——多半是 qB 此刻连不上，去 /logs 看最近的 WARNING",
                  type="positive" if n else "warning")

    def _unlock_source():
        anime.set_pref_source(anime_id, "")
        _after()
        ui.notify("已改回『按优先级』：多源自动挑、缺集用别的源兜底；下一轮采集就会补下被挡住的集",
                  type="positive")

    async def _offer_other_groups(others):
        """补齐后：别的组还有本番没下过的集 → 列出来让用户挑一个补（r37 #10）。

        【必须建在 client.layout 里、用完销毁】理由同 layout.confirm：调用方刚 _after()（body.refresh）过，
        刷新排在下一个挂起点才真正执行 —— 对话框若建在点击按钮所在的槽位里，`await dlg` 那一刻
        槽位被整块清掉，对话框还没显示就被销毁（第一版就是这样：补齐跑完、列表根本没弹出来）。"""
        dlg = page_dialog()
        if dlg is None:
            return
        with dlg, ui.card().classes("gap-2 w-full max-w-xl"):
            ui.label("别的组还有本番没下过的集").classes("font-bold")
            ui.label("这些是搜索时顺带看到的别的组，没有入库。挑一个组：只把它这几集入库、各下一份，"
                     "不改下载源，也不把番打回待确认。看一眼原名再挑——同名的别的作品偶尔也会混进来。").classes(
                "text-xs text-gray-400")
            for o in others:
                with ui.row().classes("items-center gap-2 w-full no-wrap"):
                    with ui.column().classes("gap-0 min-w-0 grow"):
                        ui.label(f"{o['group']} · {o['site']} · 第 {ep_ranges_text(o['eps'])} 集").classes(
                            "text-sm")
                        ui.label(o["sample"]).classes("text-xs text-gray-500 break-all")
                    _b = ui.button("补这个组", on_click=lambda o=o: dlg.submit(o)).props(
                        "flat dense color=primary").classes("btn-sm shrink-0")
                    _b.set_enabled(config.QB_ENABLED)
            with ui.row().classes("justify-end w-full"):
                ui.button("关闭", on_click=lambda: dlg.submit(None)).props("flat")
        try:
            pick = await wait_dialog(dlg)
        finally:
            dlg.delete()
        if not pick:
            return
        ui.notify(f"正在补 {pick['group']}…", type="info")
        # 这里还在外层 _run_backfill 的占位里（_backfill_running 持着本番 id），不必再占一次
        res = await anime.backfill_source(anime_id, group=pick["group"], site=pick["site"])
        _after()
        if res.get("error"):
            ui.notify(res["error"], type="warning")
        elif res.get("downloaded"):
            ui.notify(f"{pick['group']}：入库 {res['ingested']} 条，已下发 {res['downloaded']} 集", type="positive")
        elif res.get("ingested"):
            ui.notify(f"{pick['group']}：入库 {res['ingested']} 条，但一集都没放行——多半是 qB 此刻连不上",
                      type="warning")
        else:
            ui.notify(f"{pick['group']}：没有可补的（那几集已下过，或刚被别处补上）", type="info")

    async def _backfill_loose():
        await _run_backfill(False)

    async def _backfill_auto():
        await _run_backfill(True)

    def _force(torrent_id, archived=None):
        async def h():
            ok = await anime.download_anime_torrent(torrent_id, force=True)
            _after()
            if ok:
                ui.notify("已强制下载到文件夹", type="positive")
            elif not config.QB_ENABLED:
                ui.notify("未启用 qB（QB_ENABLED=false），无法真正下载", type="warning")
            elif not archived and anime.torrent_status(torrent_id) in engine.TRACKED_STATUSES:
                # 幂等短路（已在下/已下、或连点了两次）不是失败，日志里也不会有东西。
                # 已归档的行 force 是『重新交回 qB』，失败会原样还原成 sent —— 那是真失败，不走这句
                ui.notify("这一条已经在下载或已经下好了", type="info")
            else:
                ui.notify("下载失败，看日志", type="negative")
        return h

    def _del_one(torrent_id, archived=None, save_path="", special=False, foreign=False):
        async def h():
            # 【删 = 这一集不要了】(E-73) 删的若是同集最后一份，后台不再自动下这一集的其它版本；同集还留着别的份时只删这一份。
            # 是哪一种问 core（与真正写库的 _is_last_copy 同一个判据），别按页面上已渲染的行自己数。
            drops = anime.delete_drops_episode(torrent_id)
            _what = ("这是这一集仅剩的一份：删掉后后台不会再自动下这一集的其它版本（想换版本就在那一行点『下载』）。"
                     if drops else "同集还留着别的版本，只删这一份；留下那份以后若失败，后台照常会补。")
            if special:   # (r39 F17) 普通番的特别篇 / 未知集：彼此不是"同一集"，后台也本来就不自动下，上面两句都不对
                _what = "特别篇 / 未知集后台本来就不自动下，只删这一份。"
            elif foreign:   # (E-77 C) 别季的行：删它不是"本季这一集不要了"（delete_drops_episode 对它恒 False），上面两句都不对
                _what = "这一条标了『不属于本季』：只删这一份，不影响本季这一集（本季那一集照常自动下）。"
            if archived:   # 已归档：种子早已移出 qB，删不到文件 → 明确告知路径与后果，让用户自己清理
                if not await confirm(
                        "这一集已归档，只能标记已删",
                        f"它已从 qB 移除，本工具删不到文件。确认后仅标记为『已删』。\n\n"
                        f"⚠️ 文件仍在（需你手动删）：{save_path or '（未记录路径，请到下载目录自行查找）'}\n\n"
                        f"{_what}请尽快自行删除上面的文件。\n"
                        f"想连文件一起删：先点『下载』把它重新挂回 qB，再删。",
                        ok_label="标记已删", ok_icon="delete_forever"):
                    return
            elif not await confirm("删除这一集的文件？",
                                   "通过 qB 连同硬盘文件一起删除，不可撤销。\n" + _what,
                                   ok_label="删除文件", ok_icon="delete_forever"):
                return
            ok = await anime.delete_anime_torrent(torrent_id)
            _after()
            _tail = "；同集其它版本不会再自动下" if ok and drops else ""
            if ok and (archived or ok == "gone"):
                # 已归档的、或早就在 qB 里被手动移除的：本工具删不到文件，只标记了已删
                ui.notify(f"这一集已经不在 qB 里，只标记为已删；文件仍在 {save_path or '下载目录'}，需手动清理{_tail}",
                          type="warning")
            else:
                ui.notify(("已删除该集文件" + _tail) if ok else "没删成（qB 未连上或该集无文件）",
                          type="positive" if ok else "warning")
        return h

    def _set_ep(torrent_id, cur_ep=None):
        async def h():
            dlg = page_dialog()
            if dlg is None:
                return
            with dlg, ui.card().classes("gap-2"):
                ui.label("改集号").classes("font-bold")
                ui.label("填对集号，它就进正常下载+去重流程，支持 .5（如 12.5）。"
                         "是特别篇/OVA/总集篇就点『设为特别篇』：不再挂在未知集，也不会自动下。").classes(
                    "text-xs text-gray-400")
                # 默认填当前集号（没解析出来的才填 1）：以前恒为 1，误按回车就把集号改成了 1
                num = ui.number("集号", value=(cur_ep if cur_ep is not None and cur_ep >= 0 else 1),
                                min=0, step=1, format="%g").props("autofocus")
                num.on("keydown.enter", lambda: dlg.submit(num.value))
                with ui.row().classes("gap-2 justify-end w-full"):
                    ui.button("设为特别篇", on_click=lambda: dlg.submit(-1)).props("flat color=primary")
                    ui.space()
                    ui.button("取消", on_click=lambda: dlg.submit(None)).props("flat")
                    ui.button("确定", on_click=lambda: dlg.submit(num.value)).props("unelevated color=primary")
            try:
                val = await wait_dialog(dlg)
            finally:
                dlg.delete()
            if val is None:
                return
            if val < 0 and val != -1:   # -1 只能经『设为特别篇』按钮来；输入框里的负数一律不收
                ui.notify("集号要 ≥ 0", type="warning")
                return
            ok = anime.set_torrent_episode(torrent_id, float(val))
            _after()
            ui.notify(("已设为特别篇（SP）" if val == -1 else f"已改为第 {ep_str(float(val))} 集") if ok
                      else "改不了（已下载的种子不改集号）",
                      type="positive" if ok else "warning")
        return h

    def _exclude(torrent_id):
        async def h():
            if not await confirm("排除这一条？",
                                 "从待下里排除：不再自动下、不再挂在未知集，RSS 再遇到同种子也不重收；"
                                 "排除后可随时点『恢复』放回待下。",
                                 ok_label="排除", ok_icon="block", ok_color="primary"):
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

    def _set_foreign(torrent_id, flag, episode):
        """(E-77 C) 那一条的『不属于本季』/『撤销』。标上要先确认（会放行本季那一集的自动下载），撤销直接做。"""
        async def h():
            if flag and not await confirm(
                    "这一条不属于本季？",
                    f"标记后这一条不再占用本季第 {ep_str(episode)} 集的位置，本季那一集会照常自动下载；"
                    "文件不动、还在原目录；它不会被自动下载、替换或搬迁清状态；可以撤销。",
                    ok_label="不属于本季", ok_icon="playlist_remove", ok_color="primary"):
                return
            ok = anime.set_foreign_season(torrent_id, flag)
            _after()
            if ok:
                ui.notify("已标为『不属于本季』：本季这一集照常自动下" if flag
                          else "已撤销：这一条重新占用本季这一集的位置", type="positive")
            else:
                ui.notify("没改成（这一条正在交付中，或状态已经变了），刷新后再试", type="warning")
        return h

    def _mark_impostors(n):
        """(E-77 C) 缺集横幅上的批量按钮：把整部番里认得出的占槽旧行一次标成『不属于本季』。"""
        async def h():
            if not await confirm(
                    f"把 {n} 条占槽的旧种子标为『不属于本季』？",
                    "这些是集号落在本季范围内、发布却比本季首播早得多的旧种子（多半是前一季的正片）。"
                    "标记后它们不再占用本季的集号，本季那几集会照常自动下载；文件不动、还在原目录；"
                    "它们不会被自动下载、替换或搬迁清状态；每一条都可以在下面单独撤销。",
                    ok_label="全部标记", ok_icon="playlist_remove", ok_color="primary"):
                return
            k = anime.mark_slot_impostors_foreign(anime_id)
            _after()
            ui.notify(f"已把 {k} 条旧种子标为『不属于本季』：本季那几集照常自动下" if k
                      else "没有可标的了（可能已经标过，或正在交付中）", type="positive" if k else "info")
        return h

    body()
