"""后台轮询器：常驻协程，每 POLL_INTERVAL 秒抓一次所有源并处理。"""
import asyncio
import logging

from config import MIKAN_ENABLED, POLL_INTERVAL
from core import process_item
from sources.ani import AniSource

log = logging.getLogger("autorss")

# 源顺序即优先级：ANi 排前面先处理（同一集 ANi 先占，Mikan 版靠逻辑集去重自然跳过）
SOURCES = [AniSource()]
if MIKAN_ENABLED:
    from sources.mikan import MikanSource
    SOURCES.append(MikanSource())


async def poll_once() -> None:
    for source in SOURCES:
        try:
            items = await source.fetch()
        except Exception as e:
            log.error("抓取失败 %s: %s", source.name, e)
            continue
        new = 0
        for item in items:
            try:
                if await process_item(item):
                    new += 1
            except Exception as e:
                log.error("处理失败 %s: %s", getattr(item, "anime_title", "?"), e)
        log.info("源 %s：%d 条，新增 %d", source.name, len(items), new)


async def run_worker() -> None:
    log.info("轮询器启动，每 %d 秒一轮", POLL_INTERVAL)
    while True:
        try:
            await poll_once()
        except Exception as e:
            log.error("本轮异常: %s", e)
        await asyncio.sleep(POLL_INTERVAL)
