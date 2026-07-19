"""可选通知推送（NOTIFY_URL 留空即静默关闭）。失败只记录，不影响主流程。"""
import logging
from urllib.parse import quote

import httpx

from config import NOTIFY_TIMEOUT, NOTIFY_URL

log = logging.getLogger("autorss")


async def notify(message: str) -> None:
    if not NOTIFY_URL:
        return
    url = f"{NOTIFY_URL}/💡{quote(message)}"
    try:
        async with httpx.AsyncClient(timeout=NOTIFY_TIMEOUT) as client:
            await client.get(url)
    except httpx.HTTPError as e:
        log.warning("通知发送失败: %s", e)
