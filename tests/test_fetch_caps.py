"""取回层的上限：订阅源地址是用户自己填的、内容来自第三方站点，属不可信输入；
采集循环是常驻协程，一次挂死就是"好几天没更新了"。
"""
import gzip
import tracemalloc
import zlib

import httpx
import pytest

from services import fetch
from services.fetch import TooLarge, get_bytes, get_text


def _resp(content: bytes, status=200, headers=None, chunk=64 * 1024):
    """构造一个【流式】响应。

    直接 httpx.Response(200, content=...) 得到的是"已经读完"的响应，aiter_raw() 会抛
    StreamConsumed —— 那是 MockTransport 的构造方式所致，与真实网络无关（真实响应恒是流式）。
    分块吐出还能顺带模拟"单个块很大"这种压缩炸弹的实际形态。
    """
    def gen():
        for i in range(0, max(len(content), 1), chunk):
            yield content[i:i + chunk]
    return httpx.Response(status, stream=_Stream(gen), headers=headers or {})


class _Stream(httpx.AsyncByteStream):
    """异步流：AsyncClient 走的是 AsyncByteStream 分支（同步版会在 _send_single_request 断言失败）。"""

    def __init__(self, gen):
        self._gen = gen

    async def __aiter__(self):
        for chunk in self._gen():
            yield chunk


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_small_response_passes():
    async with _client(lambda r: _resp(b"hello")) as c:
        assert await get_bytes(c, "http://x/") == b"hello"


async def test_identity_is_requested():
    """第一道闸：直接要求服务端别压缩。大多数会照做，那就根本没有解压这回事。"""
    seen = {}

    def h(r):
        seen["ae"] = r.headers.get("accept-encoding")
        return _resp(b"ok")
    async with _client(h) as c:
        await get_bytes(c, "http://x/")
    assert seen["ae"] == "identity"


async def test_oversized_plain_body_is_rejected():
    async with _client(lambda r: _resp(b"x" * 5000)) as c:
        with pytest.raises(TooLarge):
            await get_bytes(c, "http://x/", cap=1000)


async def test_gzip_bomb_is_capped_by_output(monkeypatch):
    """(R3) 【本组的核心】httpx 的 aiter_bytes() 吐的是解压后的数据，上限只能在拿到之后判——
    一个几百 KB 的压缩体能在【单个块】里解出几十 MB，等发现超限内存峰值早就上去了。
    现在按解压后字节增量封顶，压缩比再高也撑不爆。"""
    bomb = gzip.compress(b"\0" * (40 * 1024 * 1024))     # 40MB 零字节 → 压缩后几十 KB
    assert len(bomb) < 200 * 1024, "构造的压缩体本身应该很小"

    def h(r):
        return _resp(bomb, headers={"content-encoding": "gzip"})
    async with _client(h) as c:
        with pytest.raises(TooLarge):
            await get_bytes(c, "http://x/", cap=1024 * 1024)


async def test_gzip_bomb_does_not_blow_up_memory():
    """不只是"要报错"，还得"没吃掉那么多内存"——报错但峰值已经 141MB 等于没修。"""
    bomb = gzip.compress(b"\0" * (64 * 1024 * 1024))

    def h(r):
        return _resp(bomb, headers={"content-encoding": "gzip"})
    tracemalloc.start()
    try:
        async with _client(h) as c:
            with pytest.raises(TooLarge):
                await get_bytes(c, "http://x/", cap=1024 * 1024)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 24 * 1024 * 1024, f"峰值 {peak/1024/1024:.1f}MB —— 上限是 1MB，不该吃这么多"


async def test_legit_gzip_still_works():
    """真正的压缩响应要能正常解出来（服务端忽略 identity 是常见的）。"""
    body = ("<rss>" + "内容" * 500 + "</rss>").encode()

    def h(r):
        return _resp(gzip.compress(body), headers={"content-encoding": "gzip"})
    async with _client(h) as c:
        assert await get_bytes(c, "http://x/") == body


async def test_deflate_is_handled_too():
    body = b"deflate payload" * 100
    co = zlib.compressobj()
    packed = co.compress(body) + co.flush()

    def h(r):
        return _resp(packed, headers={"content-encoding": "deflate"})
    async with _client(h) as c:
        assert await get_bytes(c, "http://x/") == body


async def test_compressed_wire_bytes_are_capped_too():
    """压缩体本身超过上限也要拒——不然对方只要发一个 100MB 的 gzip 就能耗光带宽与内存。"""
    big = gzip.compress(b"a" * (3 * 1024 * 1024))

    def h(r):
        return _resp(big, headers={"content-encoding": "gzip"})
    async with _client(h) as c:
        with pytest.raises(TooLarge):
            await get_bytes(c, "http://x/", cap=len(big) // 2)


async def test_unknown_encoding_falls_back_without_crashing():
    """br/zstd 等交回 httpx 自己解，保持旧行为，不能因为不认识就抛。"""
    def h(r):
        return _resp(b"plain-but-labelled", headers={"content-encoding": "identity"})
    async with _client(h) as c:
        assert await get_bytes(c, "http://x/") == b"plain-but-labelled"


async def test_get_text_decodes_and_caps():
    def h(r):
        return _resp("中文内容".encode("gbk"),
                     headers={"content-type": "text/html; charset=gbk"})
    async with _client(h) as c:
        assert await get_text(c, "http://x/") == "中文内容"


async def test_bogus_charset_does_not_raise():
    """字符集名来自第三方响应头，编造一个不存在的就能让 decode 抛 LookupError——
    它不属 httpx 异常族，会从各调用点的 except 里漏出去打断整轮采集。"""
    def h(r):
        return _resp(b"hi", headers={"content-type": "text/html; charset=not-a-charset"})
    async with _client(h) as c:
        assert await get_text(c, "http://x/") == "hi"


async def test_http_errors_still_raise():
    async with _client(lambda r: _resp(b"", status=503)) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await get_bytes(c, "http://x/")


# ---------------- 多值 / 异形 Content-Encoding（R3 P1） ----------------

@pytest.mark.parametrize("headers", [
    {"content-encoding": "gzip, identity"},      # 合法的多值写法
    {"content-encoding": "GZIP"},                # 大小写
    {"content-encoding": " gzip "},              # 空白
])
async def test_odd_content_encoding_still_capped(headers):
    """(R3) 【一个逗号就能绕开整个封顶】早先拿整条 Content-Encoding 字符串去精确查表，
    'gzip, identity' 查不到就落进"交回 httpx"的回退路径——而那条路径没有原始字节闸，
    压缩炸弹的内存峰值原样回到 148MB。必须按 token 解析。"""
    bomb = gzip.compress(b"\0" * (32 * 1024 * 1024))

    def h(r):
        return _resp(bomb, headers=headers)
    tracemalloc.start()
    try:
        async with _client(h) as c:
            with pytest.raises(TooLarge):
                await get_bytes(c, "http://x/", cap=512 * 1024)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 24 * 1024 * 1024, f"峰值 {peak/1024/1024:.1f}MB"


@pytest.mark.parametrize("enc", ["br", "zstd"])
async def test_undecodable_encoding_raises_instead_of_returning_garbage(enc):
    """(R3) httpx 对装不上解码器的编码是【静默放行】的：把压缩字节原样交出来、不报任何错。
    于是 feedparser 拿到二进制垃圾，抛出一个与编码毫无关系的解析错误——
    日志里既没有"编码不支持"也没有"下载失败"，是最难查的失败形态。"""
    def h(r):
        return _resp(b"\x1b compressed payload", headers={"content-encoding": enc})
    async with _client(h) as c:
        with pytest.raises(httpx.DecodingError, match="解不开"):
            await get_bytes(c, "http://x/")


async def test_unknown_encoding_fallback_still_has_a_raw_cap(monkeypatch):
    """能解的编码走回退路径时，也必须有原始字节闸——不能是个没防护的洞。"""
    import services.fetch as F
    monkeypatch.setattr(F, "_HTTPX_DECODERS", frozenset({"identity", "gzip", "deflate", "br"}))

    def h(r):
        return _resp(b"z" * (4 * 1024 * 1024), headers={"content-encoding": "br"})
    async with _client(h) as c:
        with pytest.raises((TooLarge, httpx.DecodingError)):
            await get_bytes(c, "http://x/", cap=256 * 1024)


@pytest.mark.parametrize("hdrs", [
    {"User-Agent": "x"},
    [("User-Agent", "x")],
    httpx.Headers({"User-Agent": "x"}),
    None,
])
async def test_caller_headers_of_any_shape(hdrs):
    """httpx 允许 headers 是 dict / 二元组列表 / Headers。早先的 {**kw['headers']} 遇列表就 TypeError。"""
    seen = {}

    def h(r):
        seen["ae"] = r.headers.get_list("accept-encoding")
        seen["ua"] = r.headers.get("user-agent")
        return _resp(b"ok")
    async with _client(h) as c:
        assert await get_bytes(c, "http://x/", headers=hdrs) == b"ok"
    assert seen["ae"] == ["identity"], "不能发出两条 Accept-Encoding"
    if hdrs is not None:
        assert seen["ua"] == "x", "调用方自己的头要保留"


async def test_caller_can_override_accept_encoding():
    seen = {}

    def h(r):
        seen["ae"] = r.headers.get("accept-encoding")
        return _resp(b"ok")
    async with _client(h) as c:
        await get_bytes(c, "http://x/", headers={"Accept-Encoding": "gzip"})
    assert seen["ae"] == "gzip"


async def test_raw_deflate_is_handled():
    """裸 deflate（RFC1951，无 zlib 头）：实测某些站在 Accept-Encoding: deflate 下就这么发。"""
    body = b"raw deflate payload" * 50
    co = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    packed = co.compress(body) + co.flush()

    def h(r):
        return _resp(packed, headers={"content-encoding": "deflate"})
    async with _client(h) as c:
        assert await get_bytes(c, "http://x/") == body


async def test_corrupt_compressed_body_raises_httpx_error():
    """zlib.error 不属 httpx 异常族，会从各调用点的 except 里漏出去打断整轮采集。"""
    def h(r):
        return _resp(b"\x1f\x8b" + b"garbage" * 100, headers={"content-encoding": "gzip"})
    async with _client(h) as c:
        with pytest.raises(httpx.HTTPError):
            await get_bytes(c, "http://x/")


async def test_layered_compression_is_rejected():
    """(R5) 【一个 1831 字节的双层炸弹能把进程顶到 2GB】httpx 会把 'gzip, gzip' 逐层解开，
    而回退路径只看得到最外层的原始字节数——上限声称 16MB，实测 RSS 峰值 2076MB。
    真实的 feed 服务端不会分层压缩；会这么发的只有想撑爆你的那一方。"""
    inner = gzip.compress(b"\0" * (32 * 1024 * 1024))
    bomb = gzip.compress(inner)
    assert len(bomb) < 300 * 1024

    def h(r):
        return _resp(bomb, headers={"content-encoding": "gzip, gzip"})
    tracemalloc.start()
    try:
        async with _client(h) as c:
            with pytest.raises(httpx.DecodingError, match="多重压缩"):
                await get_bytes(c, "http://x/", cap=1024 * 1024)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 24 * 1024 * 1024, f"峰值 {peak/1024/1024:.1f}MB"
