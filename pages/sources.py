"""源管理：增删源组、切策略(全下/审核)、调优先级、开关。

worker 每轮从这里读，改完下一轮就生效，不用重启。
render_sources() 抽出来复用：/sources 独立页 与 番剧列表『订阅源』tab 都调它。
"""
from nicegui import ui

import core
from .layout import frame

SITE_OPTS = {"nyaa": "nyaa", "mikan": "mikan"}
POLICY_OPTS = {"auto": "全下", "review": "审核"}


def render_sources() -> None:
    """把源管理 UI 渲染进当前容器（由调用方套 frame）。"""
    ui.label("每个组 = feed（nyaa 用户名或完整 RSS URL）+ 策略 + 优先级。"
             "多源同一集只下一份，按优先级选高的；改完下一轮生效。").classes(
        "text-xs text-gray-400")
    ui.label("① 字幕组白名单：只比对 []/【】 里的组名。 "
             "② 标题关键词：比对整条标题（如按语言 繁日/简日）。 两者可叠加(AND)。").classes(
        "text-xs text-gray-500 mb-2")

    @ui.refreshable
    def group_list():
        groups = core.list_source_groups()
        if not groups:
            ui.label("（还没有源组，下面添加）").classes("text-gray-400")
        for g in groups:
            with ui.card().classes("w-full"):
                with ui.row().classes("items-center gap-2 w-full flex-wrap"):
                    name = ui.input("名字", value=g.name).classes("w-32")
                    site = ui.select(SITE_OPTS, value=g.site, label="类型").classes("w-24")
                    policy = ui.select(POLICY_OPTS, value=g.policy, label="策略").classes("w-24")
                    priority = ui.number("优先级", value=g.priority, format="%d").classes("w-24")
                    enabled = ui.switch("启用", value=g.enabled).props("dense")
                feed = ui.input("feed（用户名 或 完整 RSS URL）", value=g.feed).classes("w-full")
                subgroups = ui.input("字幕组白名单（匹配 []/【】 里的组名；逗号分隔，空=全部）",
                                     value=g.subgroups).classes("w-full")
                tfilter = ui.input("标题关键词（匹配整条标题，不只括号；逗号分隔，空=不限；如 繁日）",
                                   value=g.title_filter).classes("w-full")
                with ui.row().classes("gap-2"):
                    ui.button("保存", icon="save",
                              on_click=_save(g.id, name, site, policy, priority, enabled, feed, subgroups, tfilter)
                              ).props("size=sm color=primary")
                    ui.button("删除", icon="delete",
                              on_click=_delete(g.id)).props("size=sm flat color=grey")

        ui.separator().classes("my-2")
        ui.label("添加新组").classes("font-bold")
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center gap-2 w-full flex-wrap"):
                n_name = ui.input("名字").classes("w-32")
                n_site = ui.select(SITE_OPTS, value="nyaa", label="类型").classes("w-24")
                n_policy = ui.select(POLICY_OPTS, value="auto", label="策略").classes("w-24")
                n_priority = ui.number("优先级", value=50, format="%d").classes("w-24")
            n_feed = ui.input("feed（nyaa 用户名如 Lilith-Raws，或完整 RSS URL）").classes("w-full")
            n_subgroups = ui.input("字幕组白名单（匹配 []/【】 里的组名；逗号分隔，空=全部）").classes("w-full")
            n_tfilter = ui.input("标题关键词（匹配整条标题，不只括号；逗号分隔，空=不限；如 繁日/简日 分语言）").classes("w-full")
            ui.button("添加", icon="add",
                      on_click=_add(n_name, n_site, n_policy, n_priority, n_feed, n_subgroups, n_tfilter)
                      ).props("size=sm color=primary")

    def _save(gid, name, site, policy, priority, enabled, feed, subgroups, tfilter):
        def h():
            if not name.value or not feed.value:
                ui.notify("名字和 feed 不能为空", type="warning")
                return
            core.update_source_group(
                gid, name=name.value.strip(), site=site.value, policy=policy.value,
                priority=int(priority.value or 0), enabled=bool(enabled.value),
                feed=feed.value.strip(), subgroups=(subgroups.value or "").strip(),
                title_filter=(tfilter.value or "").strip(),
            )
            group_list.refresh()
            ui.notify("已保存（下一轮生效）")
        return h

    def _delete(gid):
        def h():
            core.delete_source_group(gid)
            group_list.refresh()
            ui.notify("已删除")
        return h

    def _add(name, site, policy, priority, feed, subgroups, tfilter):
        def h():
            if not name.value or not feed.value:
                ui.notify("名字和 feed 不能为空", type="warning")
                return
            core.add_source_group(
                name.value.strip(), site.value, feed.value.strip(),
                policy.value, int(priority.value or 0),
                subgroups=(subgroups.value or "").strip(),
                title_filter=(tfilter.value or "").strip(),
            )
            group_list.refresh()
            ui.notify("已添加（下一轮生效）")
        return h

    group_list()


@ui.page("/sources")
def sources():
    with frame():
        ui.label("源管理").classes("text-2xl font-bold")
        render_sources()
