"""入口：初始化数据库、启动后台轮询器、跑 NiceGUI 界面。

运行： python main.py    然后浏览器打开 http://<host>:8080
"""
import asyncio

from nicegui import app, ui

import config
import pages  # noqa: F401  导入即注册页面
from config import WEB_PORT
from core import anime, engine, movies
from core.logsetup import setup_logging
from core.worker import run_movie_scan, run_qb_sync, run_reenrich_retry, run_worker
from db import init_db

setup_logging()   # 控制台 + 滚动文件(data/autorss.log) + 内存环形缓冲(供 /logs 页实时看)


@app.on_startup
async def _startup():
    init_db()
    config.load_from_db()           # 把数据库里的配置覆盖加载进内存（必须在建表之后）
    anime.seed_source_groups()       # 首启种入 ANi/Mikan 两个源组
    anime.reset_downloading()        # 复位上次遗留的 downloading（TV）
    movies.reset_downloading()      # 复位上次遗留的 downloading（剧场版）
    engine.backfill_legacy_downloaded_once()  # 一次性：历史 downloaded 标记为已完成，免得被新模型误判『在下』
    asyncio.create_task(run_worker())
    asyncio.create_task(run_qb_sync())    # qB 种子实时态同步（独立频率）
    asyncio.create_task(run_movie_scan())  # 剧场版/OVA 自动扫描（独立频率）
    asyncio.create_task(run_reenrich_retry())  # 『待识别』番后台重试 bgm（独立频率，不阻塞采集）


if __name__ in {"__main__", "__mp_main__"}:
    # 绑定回环地址：本工具无鉴权、设置页含 qB 密码等敏感信息，默认不对局域网开放。
    # 如需内网访问，改 host 并自行加鉴权/反代。
    ui.run(title="autorss", host="127.0.0.1", port=WEB_PORT, show=False, reload=True)
