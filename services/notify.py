"""可选通知推送（NOTIFY_URL 留空即静默关闭）。失败只记录，不影响主流程。"""
import logging
from urllib.parse import quote

import httpx

import config
from services.fetch import TooLarge, get_bytes

log = logging.getLogger("autorss")

_BODY_CAP = 64 * 1024      # 只关心"送到没送到"，响应体一概不用；给 64KB 已远超任何推送服务的回执


async def notify(message: str) -> None:
    """推一条通知。这是【最外圈的旁枝】：调用点在 download_*_torrent 的末尾（交付成功后），
    它一旦挂住或抛出，整条采集/放行链路就跟着停——故这里对超时与异常都取最保守的做法。

    · 走 fetch.get_bytes：httpx 的 timeout 是【每次读】的、逐块重置，涓流响应能永不触发；
      NOTIFY_URL 是用户自己填的第三方地址，必须有【总时长】上限与响应体上限（理由详见 fetch 模块）。
    · except Exception 而不是 httpx.HTTPError：URL 写坏时抛的是 httpx.InvalidURL（不继承 HTTPError）、
      端口越界时抛的是 ExceptionGroup，两者都会从这里逃出去打断本轮交付。通知失败不该有这种权力。
      （CancelledError 继承 BaseException，不在此列，关服仍能正常打断。）
    """
    if not config.NOTIFY_URL:
        return
    url = f"{config.NOTIFY_URL}/💡{quote(message, safe='')}"  # safe='' 连 '/' 也编码，防可控番名注入额外路径段
    timeout = max(1, config.NOTIFY_TIMEOUT)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            await get_bytes(client, url, cap=_BODY_CAP, timeout=timeout)
    except TooLarge:
        pass          # 回执体太大 = 请求已经发出去、通知已经推了，只是我们不读它。不是失败。
    except httpx.HTTPStatusError as e:
        # get_bytes 里的 raise_for_status 对 3xx 也抛，而有些推送服务受理后就是 302 到状态页——
        # 那同样是"送到了"。只有 4xx/5xx 才是真没送到，值得记一条。
        if e.response.status_code >= 400:
            log.warning("通知发送失败: %s", e)
    except Exception as e:
        log.warning("通知发送失败: %s", e)
