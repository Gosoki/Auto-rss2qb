"""给 NiceGUI 的 testing.User 用的入口垫片。

`nicegui.testing` 会按 `nicegui_main_file` 标记**重新加载**这个文件来注册页面路由
（它的 user fixture 每个用例都会把路由表清空重建）。不能直接指向 `pages/__init__.py`：
那是个包内模块，被当成独立文件加载时里面的相对导入会 ImportError。
也不能指向 `main.py`：那里的 `ui.run()` 会真的去绑端口，且 `@app.on_startup` 会拉起七个后台协程。

【必须先把 pages 从 sys.modules 里清掉】否则单独跑这个文件是绿的、全量跑却是 404：
全量时别的用例早就 `import pages.layout` 过了，于是这里的 `import pages` 成了空操作、
`@ui.page` 装饰器一个都不会重跑，而 fixture 刚把路由表清空——页面就全没了。
（"单独跑绿、全量跑红"是用例间污染的典型形状，这里把它掐在源头。）
"""
import sys

from nicegui import ui

for _name in [n for n in list(sys.modules) if n == "pages" or n.startswith("pages.")]:
    del sys.modules[_name]

import pages  # noqa: E402,F401  导入即注册全部页面

ui.run(show=False, reload=False)   # testing 模式下会被拦截，不会真的起服务
