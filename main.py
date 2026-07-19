"""入口：初始化数据库、启动后台轮询器、跑 NiceGUI 界面。

运行： python main.py    然后浏览器打开 http://<host>:8080
"""
import asyncio
import logging

from nicegui import app, ui

import core
import pages  # noqa: F401  导入即注册页面
from config import WEB_PORT
from db import init_db
from worker import run_worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@app.on_startup
async def _startup():
    init_db()
    core.reset_downloading()  # 复位上次遗留的 downloading
    asyncio.create_task(run_worker())


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(title="autorss", port=WEB_PORT, show=False, reload=False)
