"""剧场版审批页 `/movies`：暂存 → 人工审批 → 下载。

⚠️ 数据源未接：当前 RSS（ANi / Mikan）全是 TV 分集，管线里还没有"剧场版"
这个概念。本页把路由、导航、审批队列的骨架先搭好；等确定了怎么识别/采集
剧场版（比如按标题关键词判定、或单独的源），把数据接到 core 后这里就能用。
"""
from nicegui import ui

from .layout import frame


@ui.page("/movies")
def movies():
    with frame("movies"):
        ui.label("剧场版").classes("text-2xl font-bold")
        with ui.card().classes("w-full items-start"):
            ui.label("暂无剧场版数据").classes("text-gray-400")
            ui.label("剧场版的识别/采集还没接入管线。接好后，暂存的剧场版会出现在这里，"
                     "由你审批后再下载。").classes("text-xs text-gray-500")
