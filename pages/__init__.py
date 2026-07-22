"""导入各页面模块即注册 NiceGUI 路由（main.py 只需 `import pages`）。

anime_detail 是详情组件（渲染进列表页悬浮框），无独立路由，由 anime.py 按需导入，不在此登记。
"""
from . import anime, movies, settings, sources  # noqa: F401
