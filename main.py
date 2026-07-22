"""入口：初始化数据库、启动后台轮询器、跑 NiceGUI 界面。

运行： python main.py    然后浏览器打开 http://<host>:8080
"""
import asyncio
import logging

from nicegui import app, ui

import config
import pages  # noqa: F401  导入即注册页面
from config import WEB_PORT
from core import anime, engine, movies
from core.worker import run_movie_scan, run_qb_sync, run_worker
from db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class _SuppressDeletedSlot(logging.Filter):
    """滤掉 NiceGUI 在客户端断开瞬间偶发的一族良性报错——面板 30s 自动刷新的 ui.timer、或断连后
    async 处理器回来时 ui.notify/refresh 访问已删元素/客户端，都会抛这几条兄弟消息。客户端已走、不影响
    功能，只是刷屏并掩盖真错，故按消息精确过滤。三条 needle 都足够特指，不会误吞真正的错误。"""
    _NEEDLES = (
        "parent slot of the element has been deleted",
        "The client this element belongs to has been deleted",
        "The client this outbox belongs to has been deleted",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        text = record.getMessage() + " " + str(exc or "")
        return not any(n in text for n in self._NEEDLES)


for _h in logging.getLogger().handlers:
    _h.addFilter(_SuppressDeletedSlot())


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


if __name__ in {"__main__", "__mp_main__"}:
    # 绑定回环地址：本工具无鉴权、设置页含 qB 密码等敏感信息，默认不对局域网开放。
    # 如需内网访问，改 host 并自行加鉴权/反代。
    ui.run(title="autorss", host="127.0.0.1", port=WEB_PORT, show=False, reload=True)
