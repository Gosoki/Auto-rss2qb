"""给 `testing.User` 用的第二个入口垫片：把番剧详情挂到一条真实路由上。

`render_anime_detail` 是渲染进列表页悬浮框的**组件**，文件第一行自己写着「不再有独立页路由」——
于是 `tests/test_pages_render.py` 的 7 条路由怎么开都开不到它，
而那是个 659 行、116 处 `ui.*` 调用的文件，是全项目唯一一块"构建期才炸"的盲区。
它还是好几个不可逆操作的唯一入口（补齐该源 / 删种子 / 改集号 / 绑定 bgm）。

垫片的写法与 render_main.py 逐字同源（先清 sys.modules 再 import pages，理由写在那边），
只多挂一条 `/detail/{anime_id}`。**这条路由只存在于测试进程里**，生产的路由表不受影响。
"""
import sys

from nicegui import ui

for _name in [n for n in list(sys.modules) if n == "pages" or n.startswith("pages.")]:
    del sys.modules[_name]

import pages  # noqa: E402,F401  导入即注册全部页面
from pages.anime_detail import render_anime_detail  # noqa: E402
from pages.layout import frame  # noqa: E402


@ui.page("/detail/{anime_id}")
def _detail(anime_id: int):
    with frame("番剧"):
        render_anime_detail(anime_id)


ui.run(show=False, reload=False)   # testing 模式下会被拦截，不会真的起服务
