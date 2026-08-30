"""页面【真的渲染出来了】吗——不是"HTTP 200"。

之前每轮收尾的冒烟只 `curl` 状态码，而 NiceGUI 的 200 只代表那层 HTML 外壳发出去了：
界面内容全靠 socket.io 之后推下来。外壳照发、内容一个字没有，也是 200。
这一组用 NiceGUI 官方的 `testing.User` 把页面真正构建一遍，断言元素树里有东西。

它同时能挡住一整类"只在点下去那一刻才炸"的错：页面构建期抛异常（漏 import、
拼错属性、模板里引用了不存在的字段）在这里会当场红，而 curl 只会看到 200 或 500。
"""
import pytest
from nicegui.testing import User

import config
import db

db.init_db()
config.load_from_db()

_MAIN = "tests/render_main.py"
PAGES = ["/", "/movies", "/parse", "/manual", "/settings", "/sources", "/logs"]


@pytest.mark.nicegui_main_file(_MAIN)
@pytest.mark.parametrize("path", PAGES)
async def test_every_page_renders_something(user: User, path):
    """每个页面都要构建出一棵非空的元素树。"""
    await user.open(path)
    tree = str(user.current_layout)
    assert len(tree) > 500, f"{path} 渲染出来几乎是空的（{len(tree)} 字符）"


@pytest.mark.nicegui_main_file(_MAIN)
@pytest.mark.parametrize("path,keywords", [
    ("/", ["番剧", "剧场版", "设置"]),
    ("/movies", ["剧场版"]),
    ("/settings", ["qBittorrent", "代理", "通知"]),
    ("/sources", ["源管理", "字幕组白名单"]),
])
async def test_pages_contain_their_own_content(user: User, path, keywords):
    """再往前一步：不只是"有元素"，而是**这个页面自己的内容**在里面。

    只断言长度挡不住"骨架渲染了、主体面板整个没出来"——而那正是数据层出问题时的表现。
    """
    await user.open(path)
    tree = str(user.current_layout)
    missing = [k for k in keywords if k not in tree]
    assert not missing, f"{path} 里找不到：{missing}"
