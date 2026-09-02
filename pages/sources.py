"""源管理：增删源组、切策略(自动下载/人工审核)、调优先级、开关。

worker 每轮从这里读，改完下一轮就生效，不用重启。
render_sources() 抽出来复用：/sources 独立页 与 番剧列表『订阅源』tab 都调它。
"""
from nicegui import ui

from core import anime
from sources import SOURCES
from .layout import frame, confirm

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


def render_sources() -> None:
    """把源管理 UI 渲染进当前容器（由调用方套 frame）。"""
    ui.label("每个组 = feed（nyaa 用户名或完整 RSS URL）+ 策略 + 优先级。"
             "多源同一集只下一份，按优先级选高的；改完下一轮生效。").classes(
        "text-xs text-gray-400")
    # 与上面那段功能对等（都是本页开头的介绍），灰度必须一样 —— 早先一段 gray-400、
    # 一段 gray-500，同一页开头连着两段两种灰。全站到底该分几级见 docs/DECISIONS.md E-37。
    ui.label("① 字幕组白名单：只比对 []/【】 里的组名。 "
             "② 标题关键词：比对整条标题（如按语言 繁日/简日）。 两者可叠加(AND)。").classes(
        "text-xs text-gray-400 mb-2")

    @ui.refreshable
    def group_list():
        groups = anime.list_source_groups()
        if not groups:
            ui.label("（还没有源组，下面添加）").classes("text-gray-500")
        for g in groups:
            with ui.card().classes("w-full"):
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
                subgroups = ui.input("字幕组白名单（匹配 []/【】 里的组名；逗号分隔，空=全部）",
                                     value=g.subgroups).classes("w-full")
                tfilter = ui.input("标题关键词（匹配整条标题，不只括号；逗号分隔，空=不限；如 繁日）",
                                   value=g.title_filter).classes("w-full")
                with ui.row().classes("gap-2"):
                    ui.button("保存", icon="save",
                              on_click=_save(g.id, name, site, policy, priority, enabled, feed, subgroups, tfilter)
                              ).props("unelevated color=primary")
                    ui.button("删除", icon="delete",
                              on_click=_delete(g.id)).props("flat color=grey")

        ui.separator().classes("my-2")
        ui.label("添加新组").classes("font-bold")
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center gap-2 w-full flex-wrap"):
                n_name = ui.input("名字").classes("w-32")
                n_site = ui.select(SITE_OPTS, value="nyaa", label="类型").classes("w-32")
                n_policy = ui.select(POLICY_OPTS, value="auto", label="策略").classes("w-32")
                n_priority = ui.number("优先级", value=50, format="%d").classes("w-32")
            n_feed = ui.input("feed（nyaa 用户名如 Lilith-Raws，或完整 RSS URL）").classes("w-full")
            n_subgroups = ui.input("字幕组白名单（匹配 []/【】 里的组名；逗号分隔，空=全部）").classes("w-full")
            n_tfilter = ui.input("标题关键词（匹配整条标题，不只括号；逗号分隔，空=不限；如 繁日/简日 分语言）").classes("w-full")
            ui.button("添加", icon="add",
                      on_click=_add(n_name, n_site, n_policy, n_priority, n_feed, n_subgroups, n_tfilter)
                      ).props("unelevated color=primary")

    def _save(gid, name, site, policy, priority, enabled, feed, subgroups, tfilter):
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
            group_list.refresh()
            ui.notify("已保存（下一轮生效）", type="positive")
        return h

    def _delete(gid):
        async def h():
            if not await confirm(
                    "删除这个源组？",
                    "将永久删除该源组的 feed、字幕组白名单、关键词过滤、优先级等全部配置，不可撤销。"
                    "如只想暂停使用，改用『启用』开关即可。",
                    ok_label="删除", ok_icon="delete_forever"):
                return
            anime.delete_source_group(gid)
            group_list.refresh()
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
            group_list.refresh()
            ui.notify("已添加（下一轮生效）", type="positive")
        return h

    group_list()


@ui.page("/sources")
def sources():
    with frame():
        ui.label("源管理").classes("text-2xl font-bold")
        render_sources()
