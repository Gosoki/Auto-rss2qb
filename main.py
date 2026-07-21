"""入口：初始化数据库、启动后台轮询器、跑 NiceGUI 界面。

运行： python main.py    然后浏览器打开 http://<host>:8080
"""
import asyncio
import logging

from nicegui import app, ui

import config
import core
import movies
import pages  # noqa: F401  导入即注册页面
from config import WEB_PORT
from db import init_db
from worker import run_qb_sync, run_worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class _SuppressDeletedSlot(logging.Filter):
    """滤掉 NiceGUI 定时器在客户端断开瞬间偶发的良性报错（parent slot deleted）——
    面板 30s 自动刷新的 ui.timer 在页面被拆掉的一刹那可能再触发一次、访问已删元素的 parent_slot。
    不影响功能（该客户端已走），只是刷屏并掩盖真错，故按消息精确过滤掉。"""
    _NEEDLE = "parent slot of the element has been deleted"

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return self._NEEDLE not in record.getMessage() and self._NEEDLE not in str(exc or "")


for _h in logging.getLogger().handlers:
    _h.addFilter(_SuppressDeletedSlot())


@app.on_startup
async def _startup():
    init_db()
    config.load_from_db()           # 把数据库里的配置覆盖加载进内存（必须在建表之后）
    core.migrate_to_alias_model()   # 旧库 → 对照模型（幂等，迁移前自动备份）
    core.seed_source_groups()       # 首启种入 ANi/Mikan 两个源组
    core.backfill_mikan_whitelist()  # 老库升级：回填 Mikan 白名单（必须在轮询前）
    core.backfill_seasons()          # 老库升级：用 bgm 规范名回填季号（纠正误判成第1季的存量）
    core.backfill_quarters()         # 老库升级：用 bgm 放送日回填季度（纠正种子时间推错的季度）
    core.backfill_unmatched_review()  # 老库升级：未匹配 bgm 却自动确认的番改回待确认（不该自动下）
    core.reset_downloading()        # 复位上次遗留的 downloading（TV）
    movies.reset_downloading()      # 复位上次遗留的 downloading（剧场版）
    asyncio.create_task(run_worker())
    asyncio.create_task(run_qb_sync())  # qB 种子实时态同步（独立频率）


if __name__ in {"__main__", "__mp_main__"}:
    # 绑定回环地址：本工具无鉴权、设置页含 qB 密码等敏感信息，默认不对局域网开放。
    # 如需内网访问，改 host 并自行加鉴权/反代。
    ui.run(title="autorss", host="127.0.0.1", port=WEB_PORT, show=False, reload=False)
