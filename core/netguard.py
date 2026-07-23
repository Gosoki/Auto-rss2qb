"""网段访问控制：可选的 IP 白名单（CIDR），限定只有指定网段能访问 Web UI。

绑 0.0.0.0 对内网开放时，用它把访问收窄到可信网段（如 192.168.1.0/24）。WEB_ALLOW_CIDRS 为空=不限制。
本机回环（127.0.0.0/8、::1）恒放行，避免误配把自己锁在外面。作用于 HTTP 与 WebSocket 两种连接。
看的是直连对端 IP——经反向代理时对端是代理，此表应留空、改在代理层做鉴权（X-Forwarded-For 可伪造，不采信）。
配置走数据库、即时生效，改白名单无需重启。
"""
import ipaddress
import logging
from functools import lru_cache

import config

log = logging.getLogger("autorss")

_LOOPBACK = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))


@lru_cache(maxsize=8)
def _parse(raw: str) -> tuple:
    """逗号分隔的 CIDR 串 → 网络对象元组；逐条容错，坏条目跳过并告警（按 raw 串缓存，改了才重解析）。"""
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning("网段白名单：忽略无法解析的 CIDR %r", part)
    return tuple(nets)


def _allowed(ip_str, nets: tuple) -> bool:
    """对端 IP 是否落在回环或白名单内。只比同版本网段，避免 v4/v6 混比。"""
    if not ip_str:
        return False
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):          # ::ffff:192.168.x.x → 取内嵌 v4 再比
        ip = ip.ipv4_mapped
    return (any(ip in n for n in _LOOPBACK if n.version == ip.version)
            or any(ip in n for n in nets if n.version == ip.version))


class SubnetGuard:
    """ASGI 中间件：WEB_ALLOW_CIDRS 非空时，非白名单网段的 HTTP/WS 一律拒；空=放行一切。

    白名单配了但全解析失败 → nets 为空 → 只有回环能过（fail-closed，但本机仍能进去改回来）。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            raw = (config.WEB_ALLOW_CIDRS or "").strip()
            if raw:
                client = scope.get("client")
                if not _allowed(client[0] if client else None, _parse(raw)):
                    await self._reject(scope, send)
                    return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(scope, send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
        await send({"type": "http.response.body",
                    "body": "403 Forbidden：你的网段不在允许访问列表内".encode()})


def install(app) -> None:
    """挂到 NiceGUI 的 FastAPI app 上（须在服务器启动前调用）。"""
    app.add_middleware(SubnetGuard)
