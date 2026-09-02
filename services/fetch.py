"""带【字节上限 + 总超时】的 HTTP 取回。

为什么需要它：httpx 的 timeout 是【每次读】的超时、逐块重置——服务端只要保持涓流发送
（每 5 秒吐 1 字节）就能永不触发，把调用方永久挂住。而 `resp.content` 一次性把整个响应体
读进内存，没有任何上限：实测一个 200MB 的 feed 会让常驻内存涨 1.4GB（约 7 倍放大）。

订阅源地址是用户自己填的、内容来自第三方站点，属于不可信输入；采集循环又是常驻协程，
一次挂死就是"好几天没更新了"。core/engine.py 的 fetch_torrent_bytes 早就是这么写的
（32MB 上限 + asyncio.timeout(180) 流式读），这里把同一套范式抽出来给 feed/HTML 复用。
"""
import asyncio
import re
import zlib

import httpx

FEED_CAP = 16 * 1024 * 1024     # 单个 feed / HTML 页的上限。实际 RSS 通常几十 KB，16MB 已极宽松
TOTAL_TIMEOUT = 120             # 整个传输的总时长上限（秒），与 httpx 的逐块超时是两回事

# 【为什么要自己管解压】httpx 的 aiter_bytes() 吐的是【解压后】的数据，而上限只能在拿到数据之后判——
# 一个 400KB 的 gzip 压缩体可以在【单个块】里解出几十 MB，等我们发现超限时内存峰值早就上去了
# （实测：声称 16MB 上限，tracemalloc 峰值 141.8MB）。
# 两道闸：① 请求时要求 identity（大多数服务端会照做，那就根本没有解压这回事）；
#        ② 仍然收到压缩响应时，走 aiter_raw() 按【线上原始字节】封顶，并用 decompressobj 的
#           max_length 限制每次解出的量——这样压缩比再高也撑不爆内存。
_IDENTITY = {"Accept-Encoding": "identity"}
# zlib 能增量解的两种（增量 = 能在解出 cap 字节时立刻停手，压缩炸弹撑不爆内存）。
_ZLIB_WBITS = {"gzip": 16 + zlib.MAX_WBITS, "deflate": zlib.MAX_WBITS, "x-gzip": 16 + zlib.MAX_WBITS}


def _httpx_decoders() -> frozenset:
    """httpx 这套安装里【真正解得开】的编码名。

    httpx 对装不上解码器的编码（br 需要 brotli、zstd 需要 zstandard，两者都不是本项目的依赖）
    是【静默放行】的：它把压缩字节原样交出来，不报任何错。所以不能假设"交回 httpx 就一定能解"。
    """
    try:
        from httpx import _decoders
        return frozenset(_decoders.SUPPORTED_DECODERS)
    except Exception:      # httpx 内部结构变了就退回最保守的一组（这三个是 HTTP 规范里的常备项）
        return frozenset({"identity", "gzip", "deflate"})


_HTTPX_DECODERS = _httpx_decoders()


def _with_identity(kw: dict) -> None:
    """就地把 Accept-Encoding: identity 塞进 kw['headers']，调用方已给的同名头优先。

    走 httpx.Headers 而不是 dict 合并：httpx 允许 headers 是 dict / 二元组列表 / Headers，
    直接 `{**kw["headers"]}` 遇到列表就 TypeError，遇到小写键还会发出两条 Accept-Encoding。
    """
    hdrs = httpx.Headers(kw.get("headers") or {})
    if "accept-encoding" not in hdrs:
        hdrs["Accept-Encoding"] = "identity"
    kw["headers"] = hdrs


_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)


def safe_url(url: str) -> str:
    """一条 URL 里【可以写进日志】的那一段：只留 scheme://host[:port]。

    路径与查询串都要去掉——本项目的两类地址恰好都把密钥放在那里：
      · 推送：Bark/Server酱 把密钥放在 **path** 里（https://api.day.app/<密钥>/消息）
      · 订阅：Mikan『我的番组』把 token 放在 **query** 里（/RSS/MyBangumi?token=…）
    userinfo 同理（https://alice:hunter2@host/…）。
    """
    from urllib.parse import urlsplit
    try:
        p = urlsplit(url or "")
        host = p.hostname or ""
        if not host:
            return "(地址无法解析)"
        return f"{p.scheme}://{host}:{p.port}" if p.port else f"{p.scheme}://{host}"
    except ValueError:
        return "(地址无法解析)"


def redact(text) -> str:
    """把一段文本里出现的所有 URL 换成 safe_url 形式。

    【为什么光改我们自己的消息不够】httpx 自己抛的 HTTPStatusError/ConnectError 的 str()
    就带着完整 URL，而 core/worker.py 那行 `log.error("抓取失败 %s: %s", name, e)`
    既不截断也不脱敏 —— Mikan『我的番组』订阅地址随便一次 403/500 就把 ?token=… 整条写进
    data/autorss.log（滚动 5 份）与 /logs 页的「下载完整日志」。
    """
    return _URL_RE.sub(lambda m: safe_url(m.group(0)), str(text))


class TooLarge(Exception):
    """响应体超过上限。单列一个类型，方便调用方在日志里区分"太大"与"网络错误"。"""


async def _read_capped(resp: httpx.Response, cap: int, url: str) -> bytes:
    """把响应体读进内存并封顶。压缩响应走增量解压，原始字节与解压后字节【各自】封顶。"""
    # 【必须按 token 解析，不能拿整条头去查表】Content-Encoding 是个列表：
    # 'gzip, identity'、两个独立的 Content-Encoding 头、大小写混写 —— 都合法。
    # 早先这里拿整条字符串精确匹配，于是 'gzip, identity' 查不到表就落进"交回 httpx"的回退路径，
    # 而那条路径【没有原始字节闸】，压缩炸弹的内存峰值原样回到 148MB —— 等于整个封顶被一个逗号绕开。
    codings = [c.strip().lower()
               for raw in resp.headers.get_list("content-encoding")
               for c in raw.split(",")]
    codings = [c for c in codings if c and c != "identity"]
    if not codings:
        buf = bytearray()
        async for chunk in resp.aiter_raw():
            buf += chunk
            if len(buf) > cap:
                raise TooLarge(f"响应体超过 {cap} 字节上限：{safe_url(url)}")
        return bytes(buf)
    if len(codings) > 1:
        # 【分层压缩直接拒收】httpx 会把 'gzip, gzip' 逐层解开，而我们的回退路径只能看到
        # 最外层的原始字节数——实测 1831 字节的双层炸弹能把进程 RSS 顶到 2076MB（上限声称 16MB）。
        # 真实的 feed 服务端不会分层压缩；会这么发的只有想撑爆你的那一方。
        raise httpx.DecodingError(
            f"响应用了多重压缩编码 {'+'.join(codings)}（正常服务端不会这么发）：{safe_url(url)}")
    wbits = _ZLIB_WBITS.get(codings[0])
    if wbits is None:
        # 多重编码 / 我们不认识的单一编码：交回 httpx 解，但先确认它【真的解得开】。
        # 【httpx 对装不上解码器的编码是静默放行的】——SUPPORTED_DECODERS 里没有 br/zstd 时
        # （brotli/zstandard 不是本项目的依赖），它会原样把【压缩字节】交给我们，
        # 于是 feedparser 拿到一堆二进制垃圾，抛出一个与编码毫无关系的解析错误。
        # 那是最难查的失败形态：日志里既没有"编码不支持"也没有"下载失败"。宁可在这里明确报错。
        if any(c not in _HTTPX_DECODERS for c in codings):
            bad = [c for c in codings if c not in _HTTPX_DECODERS]
            raise httpx.DecodingError(
                f"响应用了本程序解不开的压缩编码 {'+'.join(bad)}（已请求 identity 但服务端未照做）：{safe_url(url)}")
        # 回退路径也【必须有原始字节闸】——不该是一个没有防护的洞。
        # num_bytes_downloaded 是 httpx 自己记的线上字节数。
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf += chunk
            if len(buf) > cap or resp.num_bytes_downloaded > cap:
                raise TooLarge(f"响应体超过 {cap} 字节上限（编码 {'+'.join(codings)}）：{safe_url(url)}")
        return bytes(buf)
    dec = zlib.decompressobj(wbits)
    raw_seen, out = 0, bytearray()
    async for chunk in resp.aiter_raw():
        raw_seen += len(chunk)
        if raw_seen > cap:                  # 线上原始字节也封顶：压缩体本身就不该有这么大
            raise TooLarge(f"压缩响应超过 {cap} 字节上限：{safe_url(url)}")
        # max_length 让每次最多解出"还能再收多少"，多出来的留在 unconsumed_tail 里不占额外内存
        try:
            piece = dec.decompress(chunk, max_length=cap - len(out) + 1)
        except zlib.error as e:
            # 裸 deflate（RFC1951，无 zlib 头）：实测某些站在 Accept-Encoding: deflate 下就这么发。
            # 只在【第一块】上重试一次，用负 wbits 重新解；再不行就归一成 httpx 的异常族——
            # zlib.error 不属那一族，会从各调用点的 except 里漏出去。
            if raw_seen == len(chunk) and wbits == zlib.MAX_WBITS:
                dec = zlib.decompressobj(-zlib.MAX_WBITS)
                try:
                    piece = dec.decompress(chunk, max_length=cap + 1)
                except zlib.error as e2:
                    raise httpx.DecodingError(f"解压失败：{e2}") from e2
            else:
                raise httpx.DecodingError(f"解压失败：{e}") from e
        out += piece
        if len(out) > cap:
            raise TooLarge(f"解压后超过 {cap} 字节上限（疑似压缩炸弹）：{safe_url(url)}")
    try:
        out += dec.flush()
    except zlib.error as e:
        raise httpx.DecodingError(f"解压收尾失败：{e}") from e
    if len(out) > cap:
        raise TooLarge(f"解压后超过 {cap} 字节上限（疑似压缩炸弹）：{safe_url(url)}")
    return bytes(out)


async def request_capped(client: httpx.AsyncClient, method: str, url: str, *,
                         cap: int = FEED_CAP, timeout: int = TOTAL_TIMEOUT,
                         **kw) -> tuple[int, bytes]:
    """通用版：任意方法、**不** raise_for_status，回 (status_code, 封顶后的响应体)。

    【为什么要这个形态】(R32) `services/enrich` 的 6 处出站原本走裸 `client.get/post`，
    四道闸（identity 请求 / 线上原始字节封顶 / decompressobj 增量解压 / 多重编码拒收）
    一条都没生效 —— 实测一个 255 KB 的 gzip 响应让进程 RSS 涨 **540 MB**
    （压缩比 1029:1），而 `_mikan_bridge` 就串在采集主链路上；
    这个进程同时是 Web UI、qB 同步、交付协程与看守协程，被 OOM 杀掉就是全没。
    可它们又都要**自己看 status_code**（429/5xx 要退款、404 要静默），
    所以不能用会 `raise_for_status()` 的 `get_bytes`。

    POST 也走这里：bgm 的搜索接口是 POST，而它同样是第三方响应。
    """
    _with_identity(kw)
    async with asyncio.timeout(timeout):
        async with client.stream(method, url, **kw) as resp:
            return resp.status_code, await _read_capped(resp, cap, url)


async def get_bytes(client: httpx.AsyncClient, url: str, *,
                    cap: int = FEED_CAP, timeout: int = TOTAL_TIMEOUT, **kw) -> bytes:
    """流式取回并封顶。超限抛 TooLarge，超时抛 TimeoutError，HTTP 错误照常抛。"""
    _with_identity(kw)
    async with asyncio.timeout(timeout):
        async with client.stream("GET", url, **kw) as resp:
            resp.raise_for_status()
            return await _read_capped(resp, cap, url)


async def get_text(client: httpx.AsyncClient, url: str, *,
                   cap: int = FEED_CAP, timeout: int = TOTAL_TIMEOUT, **kw) -> str:
    """同 get_bytes，按响应头声明的字符集解码（取不到则 utf-8，坏字节替换而不是抛）。

    用 charset_encoding（响应头里的）而不是 httpx 的 resp.encoding：后者在流式模式下
    可能要读完整个 body 做字符集嗅探，正好绕开我们要设的上限。
    """
    _with_identity(kw)
    async with asyncio.timeout(timeout):
        async with client.stream("GET", url, **kw) as resp:
            resp.raise_for_status()
            enc = resp.charset_encoding or "utf-8"
            data = await _read_capped(resp, cap, url)
            try:
                return data.decode(enc, "replace")
            except (LookupError, UnicodeError):
                # 字符集名来自第三方响应头（Content-Type: charset=xxx），是不可信输入：
                # 编造一个不存在的编码名就能让 decode 抛 LookupError——它不属 httpx 异常族，
                # 会从各调用点的 except httpx.HTTPError 里漏出去打断整轮采集。回落 utf-8 照常解。
                # 【只接 LookupError 不够】它盖住的是"编码名不存在"和"是二进制编解码器"
                # （bz2_codec / hex_codec 这一族，Python 明确抛 LookupError: not a text encoding）。
                # 还有一类是【存在、也是文本编码、但拒绝 errors='replace'】：
                #   b"…".decode("idna", "replace")     → UnicodeError: Unsupported error handling
                #   b"…".decode("punycode", "replace") → UnicodeDecodeError（内部 ascii 解码，无视 errors）
                # 两者都不是 LookupError，照样能从这里逃出去打断整轮采集。
                # UnicodeDecodeError 是 UnicodeError 的子类，一并盖住。
                return data.decode("utf-8", "replace")
