"""可选通知推送（NOTIFY_URL 留空即静默关闭）。失败只记录，不影响主流程。"""
import logging
from urllib.parse import quote

import httpx

import config

log = logging.getLogger("autorss")


async def notify(message: str) -> None:
    if not config.NOTIFY_URL:
        return
    url = f"{config.NOTIFY_URL}/💡{quote(message, safe='')}"  # safe='' 连 '/' 也编码，防可控番名注入额外路径段
    try:
        async with httpx.AsyncClient(timeout=config.NOTIFY_TIMEOUT) as client:
            await client.get(url)
    except httpx.HTTPError as e:
        log.warning("通知发送失败: %s", e)
