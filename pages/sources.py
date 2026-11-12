"""源管理：增删源组、切策略(自动下载/人工审核)、调优先级、开关。

worker 每轮从这里读，改完下一轮就生效，不用重启。
render_sources() 抽出来复用：/sources 独立页 与 番剧列表『订阅源』tab 都调它。
"""
from nicegui import ui

from core import anime
from services import fetch as _fetch
from sources import SOURCES
from sources.nyaa import nyaa_feed_url
from .layout import busy_action, frame, confirm

# 类型下拉直接取自 sources.SOURCES —— 加一个源时不必再回来改这里
SITE_OPTS = {k: k for k in SOURCES}
POLICY_OPTS = {"auto": "自动下载", "review": "人工审核"}


def _opts_with(opts: dict, value: str) -> dict:
    """下拉选项，确保库里那个值一定在里面（不在就临时加一项并标注）。

    典型场景：先建了某个源组、又回滚到不支持该 site 的版本。
    """
    if value in opts:
        return opts
    return {**opts, value: f"{value}（本版本不支持）"}


def _bad_priority(priority) -> bool:
    """优先级输入框为空/非数字时拦下保存并提示。True=有问题，调用方直接 return。

    以前是 `int(priority.value or 0)`：清空输入框（ui.number 给回 None）就静默写成 0，
    而优先级决定多源择优挑哪一份——用户只是想改别的字段，却把这个源悄悄降到了最低。
    """
    v = priority.value
    if v is None or str(v).strip() == "":
        ui.notify("优先级要填数字（留空会被当成 0，多源择优时这个源就永远挑不中了）", type="warning")
        return True
    try:
        int(v)
    except (TypeError, ValueError):
        ui.notify("优先级只能填数字", type="warning")
        return True
    return False


def _csv(text) -> list:
    """逗号分隔的一栏 → 词表（strip、去空）。与 core/worker.build_sources 拆 subgroups / title_filter 同一写法。"""
    return [x.strip() for x in (text or "").split(",") if x.strip()]


async def probe_feed(site: str, feed: str, subgroups: str, title_filter: str) -> tuple[str, bool]:
    """『试抓』：按表单上【当前填的值】（不必先保存）抓一次 feed，返回（一段人话, 是否警告）。

    只读：不入库、不推进 seen、不碰 worker / health 的任何状态。feed 只请求一次——
    带过滤与不带过滤的两遍解析共用同一份字节（RssSource.fetch 的 content_cache）。
    源对象的构造与 core/worker.build_sources 同一写法（那边改了这里要跟着改，有用例钉着）。
    『能收』的条数用不带本组过滤再解析一遍的结果，不用 feed 原始条数：这样『能收 N>0 而本组 0 条』
    才确切等于被白名单 / 关键词挡光，不会把合集、解析不出番名的也算进去。
    """
    cls = SOURCES.get(site)
    if cls is None:
        return f"本版本不支持『{site}』这个类型", True
    if not (feed or "").strip():
        return "先填 feed", True
    subs, kws = _csv(subgroups), _csv(title_filter)
    try:
        url = nyaa_feed_url(feed) if site == "nyaa" else feed.strip()
        cache: dict = {}
        got = await cls("试抓", url, "review", 0, subs, kws).fetch(content_cache=cache)
        every = got if not (subs or kws) else await cls("试抓", url).fetch(content_cache=cache)
    except Exception as e:
        # 【必须在这里兜住并脱敏】不能漏给 busy_action：它的兜底会 log.exception 并原样 toast，
        # 而 Mikan『我的番组』订阅地址把 token 放在 query 里，httpx 异常的 str() 带完整 URL
        return f"没抓到：{type(e).__name__}: {_fetch.redact(e)[:160]}", True
    if not every:
        return "feed 抓到了，但里面没有能收的条目（空 feed，或全是合集 / 解析不出番名的）", True
    if not got:
        return (f"feed 里有 {len(every)} 条能收，但没有一条通过这个组的白名单 / 关键词"
                "——检查拼写（包含即算、不分大小写）"), True
    return (f"feed 里 {len(every)} 条能收，这个组收 {len(got)} 条。最前面几条：\n"
            + "\n".join("· " + i.raw_title for i in got[:3])), False


def render_sources() -> None:
    """把源管理 UI 渲染进当前容器（由调用方套 frame）。"""
    ui.label("每个组 = 一个 feed（nyaa 用户名或完整 RSS URL）+ 两道可选过滤 + 策略 + 优先级；改完下一轮采集生效。"
             "同一个 feed 可以建多个组（比如按『简日』『繁日』分开、给不同优先级），每轮只抓一次。").classes(
        "text-xs text-gray-400")
    # 『只有人工审核的组在发的』而不是『人工审核＝新番先进待确认』：入库路径里有自动升确认，
    # 自动下载的组后来也发了同一部番，会把它转成在追
    ui.label("优先级：数字越大越优先，同一集多个组都有时只下最高的那一份。"
             "策略只在一部【新番】第一次出现时起作用：自动下载的组发现的直接开追；"
             "只有人工审核的组在发的，先进『待确认』等你点。已经在追的番，哪个组的种子都参与择优。").classes(
        "text-xs text-gray-400")
    # 与上面那段功能对等（都是本页开头的介绍），灰度必须一样 —— 早先一段 gray-400、
    # 一段 gray-500，同一页开头连着两段两种灰。全站到底该分几级见 docs/DECISIONS.md E-37。
    ui.label("① 字幕组白名单：只比对 []/【】 里的组名。 "
             "② 标题关键词：比对整条标题（如按语言 繁日/简日）。 "
             "一栏里用逗号写多个，命中任意一个就算（包含即算、不分大小写）；两栏都填要同时满足。").classes(
        "text-xs text-gray-400 mb-2")

    # 【不用 @ui.refreshable 整页重建】(r39 U11) 以前整张列表连同底下的添加表单是一个 refreshable，
    # 保存/添加/删除任一组都 refresh 整块：别的组卡片上【没点保存】的改动、底下半填的新组表单，
    # 全被服务端快照冲掉（用户改了 A 组的关键词还没保存，顺手保存了 B 组，A 的改动一声不响就没了）。
    # 改成稳定容器 + 按 id 登记卡片：保存只回写本卡、添加只追加一张、删除只删一张，其余控件原样不动。
    cards: dict = {}                                # {源组 id: 该组的卡片}
    # 空状态放在卡片容器【外面】：_resort 按下标挪卡片，容器里混一个 label 下标就错位了
    empty = ui.label("（还没有源组，下面添加）").classes("text-gray-500")
    cards_box = ui.column().classes("w-full")       # ui.column 默认间距与外层一致，跟原来 refreshable 平铺时对齐

    async def _probe(site, feed, subgroups, tfilter, out):
        text, warn = await probe_feed(site.value, feed.value, subgroups.value, tfilter.value)
        out.text = text
        out.classes(remove="text-gray-400 text-amber-400", add="text-amber-400" if warn else "text-gray-400")
        out.set_visibility(True)

    def _card(g):
        """在 cards_box 里建一张源组卡片并登记进 cards。"""
        with cards_box, ui.card().classes("w-full") as card:
            with ui.row().classes("items-center gap-2 w-full flex-wrap"):
                name = ui.input("名字", value=g.name).classes("w-32")
                # 【库里的值不在选项里也要能渲染】ui.select 拿到不在 options 里的 value 会抛，
                # 而这一页正是删除源组的【唯一入口】——一旦崩掉，用户连把那个坏组删掉都做不到。
                # 后台对同样的数据只是 log.warning 跳过（见 core/worker.py 的 build_sources），
                # 没有理由 UI 这边反而硬崩。
                site = ui.select(_opts_with(SITE_OPTS, g.site), value=g.site,
                                 label="类型").classes("w-32")
                policy = ui.select(_opts_with(POLICY_OPTS, g.policy), value=g.policy,
                                   label="策略").classes("w-32")
                priority = ui.number("优先级", value=g.priority, format="%d").classes("w-32")
                enabled = ui.switch("启用", value=g.enabled)
            feed = ui.input("feed（用户名 或 完整 RSS URL）", value=g.feed).classes("w-full")
            subgroups = ui.input("字幕组白名单（空=全部）", value=g.subgroups).classes("w-full")
            tfilter = ui.input("标题关键词（空=不限）", value=g.title_filter).classes("w-full")
            with ui.row().classes("gap-2 items-center"):
                save_btn = ui.button("保存", icon="save").props("unelevated color=primary")
                ui.button("删除", icon="delete",
                          on_click=_delete(g.id)).props("flat color=grey")
                ui.button("试抓", icon="rss_feed",
                          on_click=lambda e: busy_action(
                              e.sender, f"source-probe-{g.id}",
                              lambda: _probe(site, feed, subgroups, tfilter, probe_out),
                              fail="试抓失败")).props("flat color=primary")
                # 【未保存标记】『启用』是开关，拨一下看着像即时生效，其实要点本卡『保存』才落库；
                # 把停用的源拨开却没保存，这个源就一直不抓，界面上看不出来。碰过就标，保存成功才收
                dirty = ui.label("有改动，还没保存").classes("text-xs text-amber-400")
            probe_out = ui.label().classes("text-xs whitespace-pre-line break-all")
            probe_out.set_visibility(False)
            dirty.set_visibility(False)
            for w in (name, site, policy, priority, enabled, feed, subgroups, tfilter):
                w.on_value_change(lambda: dirty.set_visibility(True))
            save_btn.on_click(_save(g.id, name, site, policy, priority, enabled, feed, subgroups, tfilter, dirty))
        cards[g.id] = card

    def _resort():
        """按库里的顺序（优先级降序）摆放已有卡片：只挪位置、不重建，卡片上没保存的输入不受影响。"""
        order = [g.id for g in anime.list_source_groups() if g.id in cards]
        for i, gid in enumerate(order):
            cards[gid].move(target_index=i)

    def _save(gid, name, site, policy, priority, enabled, feed, subgroups, tfilter, dirty):
        def h():
            if not (name.value or "").strip() or not (feed.value or "").strip():
                # 【必须 strip 再判空】纯空格是 truthy，会一路放行到下面的 .strip() 落成空串——
                # 而空 feed 在 nyaa 那边拼出来的是【不带用户名的全站 RSS】，
                # 首启种入的 ANi 组又恰好是 policy='auto'：自动建番、自动确认、自动下载整站。
                ui.notify("名字和 feed 不能为空（也不能只填空格）", type="warning")
                return
            if _bad_priority(priority):
                return
            if not anime.update_source_group(
                gid, name=name.value.strip(), site=site.value, policy=policy.value,
                priority=int(priority.value or 0), enabled=bool(enabled.value),
                feed=feed.value.strip(), subgroups=(subgroups.value or "").strip(),
                title_filter=(tfilter.value or "").strip(),
            ):
                ui.notify(f"保存失败：多半是已有叫『{name.value.strip()}』的源组（换个名字），"
                          "也可能是某个字段太长写不进库——具体原因看 /logs", type="warning")
                return
            # 不重建（理由见 cards 上方）：把输入框回写成实际存进库的值（去空格、优先级取整），屏上与库一致
            for inp in (name, feed, subgroups, tfilter):
                inp.value = (inp.value or "").strip()
            priority.value = int(priority.value)
            dirty.set_visibility(False)             # 排在回写之后：回写本身会再触发一次 on_value_change
            _resort()                               # 改了优先级的话，卡片顺序跟着库走
            ui.notify("已保存（下一轮生效）", type="positive")
        return h

    def _delete(gid):
        async def h():
            if not await confirm(
                    "删除这个源组？",
                    "将永久删除该源组的 feed、字幕组白名单、关键词过滤、优先级等全部配置，不可撤销。"
                    "如只想暂停使用，关掉『启用』开关再点保存即可。",
                    ok_label="删除", ok_icon="delete_forever"):
                return
            anime.delete_source_group(gid)
            card = cards.pop(gid, None)             # 两个确认框都点了『删除』时第二次已经不在了
            if card is not None:
                card.delete()
            empty.set_visibility(not cards)
            ui.notify("已删除", type="positive")
        return h

    def _add(name, site, policy, priority, feed, subgroups, tfilter):
        def h():
            if not (name.value or "").strip() or not (feed.value or "").strip():
                ui.notify("名字和 feed 不能为空（也不能只填空格）", type="warning")   # 理由同『保存』
                return
            if _bad_priority(priority):
                return
            if not anime.add_source_group(
                name.value.strip(), site.value, feed.value.strip(),
                policy.value, int(priority.value or 0),
                subgroups=(subgroups.value or "").strip(),
                title_filter=(tfilter.value or "").strip(),
            ):
                ui.notify(f"添加失败：多半是已有叫『{name.value.strip()}』的源组（换个名字），"
                          "也可能是某个字段太长写不进库——具体原因看 /logs", type="warning")
                return
            # 只追加新组这一张卡（名字有唯一约束，按名字找回刚建的那一行）
            new = next((g for g in anime.list_source_groups() if g.name == name.value.strip()), None)
            if new is not None:
                _card(new)
                _resort()
            empty.set_visibility(not cards)
            # 表单回到初始值，方便接着添加下一个
            name.value = feed.value = subgroups.value = tfilter.value = ""
            site.value, policy.value, priority.value = "nyaa", "auto", 50
            ui.notify("已添加（下一轮生效）", type="positive")
        return h

    for _g in anime.list_source_groups():
        _card(_g)
    empty.set_visibility(not cards)

    # 添加表单建在卡片容器外面、只建一次：保存/添加/删除都不碰它，半填的内容不会丢
    ui.separator().classes("my-2")
    ui.label("添加新组").classes("font-bold")
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-2 w-full flex-wrap"):
            n_name = ui.input("名字").classes("w-32")
            n_site = ui.select(SITE_OPTS, value="nyaa", label="类型").classes("w-32")
            n_policy = ui.select(POLICY_OPTS, value="auto", label="策略").classes("w-32")
            n_priority = ui.number("优先级", value=50, format="%d").classes("w-32")
        n_feed = ui.input("feed（用户名 或 完整 RSS URL）").classes("w-full")
        n_subgroups = ui.input("字幕组白名单（空=全部）").classes("w-full")
        n_tfilter = ui.input("标题关键词（空=不限）").classes("w-full")
        with ui.row().classes("gap-2 items-center"):
            ui.button("添加", icon="add",
                      on_click=_add(n_name, n_site, n_policy, n_priority, n_feed, n_subgroups, n_tfilter)
                      ).props("unelevated color=primary")
            ui.button("试抓", icon="rss_feed",
                      on_click=lambda e: busy_action(
                          e.sender, "source-probe-new",
                          lambda: _probe(n_site, n_feed, n_subgroups, n_tfilter, n_out),
                          fail="试抓失败")).props("flat color=primary")
        n_out = ui.label().classes("text-xs whitespace-pre-line break-all")
        n_out.set_visibility(False)


@ui.page("/sources")
def sources():
    with frame():
        ui.label("订阅源").classes("text-2xl font-bold")
        render_sources()
