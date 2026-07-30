"""带【字节上限 + 总超时】的 HTTP 取回。

为什么需要它：httpx 的 timeout 是【每次读】的超时、逐块重置——服务端只要保持涓流发送
（每 5 秒吐 1 字节）就能永不触发，把调用方永久挂住。而 `resp.content` 一次性把整个响应体
读进内存，没有任何上限：实测一个 200MB 的 feed 会让常驻内存涨 1.4GB（约 7 倍放大）。

订阅源地址是用户自己填的、内容来自第三方站点，属于不可信输入；采集循环又是常驻协程，
一次挂死就是"好几天没更新了"。core/engine.py 的 fetch_torrent_bytes 早就是这么写的
（32MB 上限 + asyncio.timeout(180) 流式读），这里把同一套范式抽出来给 feed/HTML 复用。
"""
import asyncio

import httpx

FEED_CAP = 16 * 1024 * 1024     # 单个 feed / HTML 页的上限。实际 RSS 通常几十 KB，16MB 已极宽松
TOTAL_TIMEOUT = 120             # 整个传输的总时长上限（秒），与 httpx 的逐块超时是两回事


class TooLarge(Exception):
    """响应体超过上限。单列一个类型，方便调用方在日志里区分"太大"与"网络错误"。"""


async def get_bytes(client: httpx.AsyncClient, url: str, *,
                    cap: int = FEED_CAP, timeout: int = TOTAL_TIMEOUT, **kw) -> bytes:
    """流式取回并封顶。超限抛 TooLarge，超时抛 TimeoutError，HTTP 错误照常抛。"""
    async with asyncio.timeout(timeout):
        async with client.stream("GET", url, **kw) as resp:
            resp.raise_for_status()
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf += chunk
                if len(buf) > cap:
                    raise TooLarge(f"响应体超过 {cap} 字节上限：{url}")
            return bytes(buf)


async def get_text(client: httpx.AsyncClient, url: str, *,
                   cap: int = FEED_CAP, timeout: int = TOTAL_TIMEOUT, **kw) -> str:
    """同 get_bytes，按响应头声明的字符集解码（取不到则 utf-8，坏字节替换而不是抛）。

    用 charset_encoding（响应头里的）而不是 httpx 的 resp.encoding：后者在流式模式下
    可能要读完整个 body 做字符集嗅探，正好绕开我们要设的上限。
    """
    async with asyncio.timeout(timeout):
        async with client.stream("GET", url, **kw) as resp:
            resp.raise_for_status()
            enc = resp.charset_encoding or "utf-8"
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf += chunk
                if len(buf) > cap:
                    raise TooLarge(f"响应体超过 {cap} 字节上限：{url}")
            return bytes(buf).decode(enc, "replace")
