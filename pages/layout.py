"""共享布局：顶栏导航 + 统一的内容容器。所有页面都套 frame()。

页面级组件放在自己的页面文件里；这里只放跨页面复用的东西。
"""
from contextlib import contextmanager

from nicegui import ui

NAV = [("manage", "番剧列表", "/"), ("movies", "剧场版", "/movies"),
       ("sources", "源管理", "/sources"), ("settings", "设置", "/settings")]


def name_of(a) -> str:
    return a.display_name or a.title


def season_label(a):
    """季徽标文案：第2季起显示『第N季』，第1季不显示。"""
    return f"第{a.season}季" if (a.season or 1) > 1 else None


def ep_str(e) -> str:
    if e == -1:
        return "特别"
    if e == -2:
        return "?"
    return str(int(e)) if float(e).is_integer() else str(e)


@contextmanager
def frame(active: str = ""):
    """页面骨架：暗色 + 顶栏（站名 + 导航 + 右侧动作位）。

    yield 出顶栏右侧的容器，页面可往里放全局动作按钮（如刷新/补下）；不放就是空的。
    """
    ui.dark_mode(True)
    with ui.header().classes("items-center gap-2"):
        ui.label("autorss").classes("text-xl font-bold mr-2")
        for key, label, path in NAV:
            btn = ui.button(label, on_click=lambda p=path: ui.navigate.to(p)).props("flat color=white")
            if key == active:
                btn.classes("font-bold underline")
        ui.space()
        header_right = ui.row().classes("items-center gap-1")
    with ui.column().classes("w-full max-w-5xl mx-auto p-2"):
        yield header_right
