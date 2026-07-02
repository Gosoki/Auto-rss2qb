"""robot 通知推送 —— 可选功能。

NOTIFY_URL 留空时 `notify()` 直接静默返回，因此通知是完全可选的：
不配置就不发消息，主流程照常运行。发送失败只记录、不抛出，绝不影响主流程。
"""
import logging
import time
from urllib.parse import quote

import requests

from config import (
    NOTIFY_DELAY,
    NOTIFY_ENABLED,
    NOTIFY_RETRIES,
    NOTIFY_TIMEOUT,
    NOTIFY_URL,
)

# 用独立的标准 logger 记录自身失败，避免和业务日志(logger.log)相互调用成环
_logger = logging.getLogger("autorss.notify")


def notify(message):
    if not NOTIFY_ENABLED:
        return
    if NOTIFY_DELAY:
        time.sleep(NOTIFY_DELAY)  # 简单限速
    url = f"{NOTIFY_URL}/💡{quote(message)}"
    for _ in range(NOTIFY_RETRIES):
        try:
            resp = requests.get(url, timeout=NOTIFY_TIMEOUT)
        except requests.RequestException as e:
            _logger.warning("通知发送失败: %s", e)
            return
        if resp.status_code == 200:
            try:
                if resp.json().get("status") == "ok":
                    return
            except ValueError:
                return  # 非 JSON 但已 200，视为送达
        else:
            _logger.warning("通知返回状态码 %s - %s", resp.status_code, message)
