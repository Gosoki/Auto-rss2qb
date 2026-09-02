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


def _client(handler, **kw):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw)


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


@pytest.mark.parametrize("charset,body,exc,why", [
    ("not-a-charset", b"hi", "LookupError", "编造一个不存在的编码名"),
    ("bz2_codec", b"hi", "LookupError", "存在但是二进制编解码器（Python 明确抛 not a text encoding）"),
    ("hex_codec", b"hi", "LookupError", "同上"),
    # 【只接 LookupError 是不够的】这两个是【存在、也是文本编码、但拒绝 errors='replace'】：
    #   b"…".decode("idna", "replace")     → UnicodeError: Unsupported error handling
    #   b"…".decode("punycode", "replace") → UnicodeDecodeError（内部 ascii 解码，无视 errors）
    # 上一版用例喂的编码名正好只覆盖会抛 LookupError 的那一半 ——
    # "用例只测了自己传进去的那一类"，而漏掉的那一类照样能打断整轮采集。
    ("idna", b"hi", "UnicodeError", "存在、是文本编码，但拒绝 errors='replace'"),
    # punycode 对 ASCII 能解出来，要非 ASCII 字节才抛 —— 而"响应体是二进制"恰恰是常态
    ("punycode", b"\xff\xfe", "UnicodeDecodeError", "内部 ascii 解码，无视 errors 参数"),
])
async def test_bogus_charset_does_not_raise(charset, body, exc, why):
    """字符集名来自第三方响应头，是不可信输入；decode 抛的异常不属 httpx 异常族，
    会从各调用点的 except httpx.HTTPError 里漏出去打断整轮采集。"""
    # 先确认这个编码在本机确实会抛报告里说的那一类，否则这条用例是空跑
    with pytest.raises(Exception) as ei:
        body.decode(charset, "replace")
    assert type(ei.value).__name__ == exc, f"前提变了：{charset} 现在抛 {type(ei.value).__name__}"

    def h(r):
        return _resp(body, headers={"content-type": f"text/html; charset={charset}"})
    async with _client(h) as c:
        assert await get_text(c, "http://x/") == body.decode("utf-8", "replace"), why


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


async def test_redirect_chain_is_capped(monkeypatch):
    """(E-41) 重定向跳数要有上界 —— 中间跳的响应体【不受 cap 约束】。

    httpx 在跟随重定向之前先 `await response.aread()` 把这一跳整个读进内存，
    并把每一跳连同它的内容一路挂在 response.history 上带到最终响应：
    N 跳的响应体是【累加驻留】的，而 _read_capped 只作用在最终那一个响应上，
    从头到尾看不见中间跳。实测 4 跳 × 100MB → 进程 RSS 峰值 568MB。
    压跳数不能根治，但立刻把最坏值压掉一个数量级，且不碰 SSRF 守卫
    （守卫的跳数计数靠 httpx 的 extensions 传递，自己写重定向循环要一并接管，写错就是守卫失效）。
    """
    import config
    assert config.http_client_kwargs(30).get("max_redirects") == config._MAX_REDIRECTS
    assert config._MAX_REDIRECTS <= 5, "跳数上限放得太松了"

    hops = {"n": 0}

    def h(r):
        hops["n"] += 1
        if hops["n"] <= 10:
            return httpx.Response(302, headers={"location": f"http://x/{hops['n']}"})
        return _resp(b"done")

    async with _client(h, follow_redirects=True, max_redirects=config._MAX_REDIRECTS) as c:
        with pytest.raises(httpx.TooManyRedirects):
            await get_bytes(c, "http://x/")
    assert hops["n"] <= config._MAX_REDIRECTS + 1, f"跟了 {hops['n']} 跳，上限没生效"


# ---------------- (R21) 『内网地址不走代理』的广度守卫 ----------------

def test_every_production_http_client_reports_its_target():
    """全仓每一处 `config.http_client_kwargs(...)` 都必须传 `url=`。

    `url=` 是"内网地址不走代理"唯一的输入：`config._direct_mounts` 只能把
    【字面内网 IP】的目标钉成直连，靠的就是调用方把真实目标 URL 传进来。
    漏传的后果不是"少一个优化"——`MIKAN_BASE=http://192.168.1.50:7001` 这类
    自建镜像（设置页明确支持）的请求会被塞进代理，必然失败。

    R21 之前生产代码 7 处只传了 5 处，漏的两处都在 `services/enrich.py`，
    而其中 `_resolve_inner` 恰恰要打 MIKAN_BASE 做桥接 —— 标准的第①号形状。

    【用 AST 而不是 grep】本文件与生产代码的注释里都写满了 `url=`。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    offenders = []
    for py in sorted(root.rglob("*.py")):
        if any(x in py.parts for x in (".venv", "tests", "__pycache__", ".git", "alembic")):
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "http_client_kwargs"):
                continue
            if not any(k.arg == "url" for k in n.keywords):
                offenders.append(f"{py.relative_to(root)}:{n.lineno}")
    assert not offenders, "这些出站客户端没报告自己要打的地址（内网直连会失效）：" + "; ".join(offenders)


def test_the_direct_mounts_helper_accepts_several_targets():
    """一个 AsyncClient 打两个可配置地址时（enrich 的 BGM_API + MIKAN_BASE），两个都要挂上。

    收成"一串"之前，那一处只能二选一 —— 而无论选哪个，另一个的内网直连都是失效的。
    """
    import config

    old = config._v.get("PROXY_SKIP_INTERNAL", True)
    config._v["PROXY_SKIP_INTERNAL"] = True
    try:
        one = config._direct_mounts("http://192.168.1.50:7001")
        two = config._direct_mounts(("http://10.0.0.7:8080", "http://192.168.1.50:7001"))
        assert "all://192.168.1.50" in one
        assert "all://192.168.1.50" in two and "all://10.0.0.7" in two, \
            "多目标时只挂上了其中一个"
        assert "all://10.0.0.7" not in one
    finally:
        config._v["PROXY_SKIP_INTERNAL"] = old


# ---------------- (R21) qB 的凭据绝不交给代理，也绝不跟着重定向走 ----------------

@pytest.mark.parametrize("qb_url,host", [
    ("http://nas:8080", "nas"),                       # 本地 DNS / hosts
    ("http://qb.mydomain.com:8080", "qb.mydomain.com"),
    ("http://qb.fritz.box:8080", "qb.fritz.box"),     # 路由器默认域
    ("http://192.168.1.9:8080", "192.168.1.9"),       # 字面 IP（旧写法，本来就直连）
])
def test_the_qb_host_is_always_mounted_direct(qb_url, host):
    """qB 的主机一律直连 —— 登录 POST 带着**明文**账号密码。

    `_internal_literal_host` 只认字面 IP，`_DIRECT_PATTERNS` 只静态覆盖
    localhost / 127.0.0.1 / [::1] / *.local / *.lan / *.internal / *.home.arpa。
    把 QB_URL 写成主机名或自定义域（本地 DNS、AdGuard、路由器默认域，都很常见）时一个都不命中，
    于是 `/api/v2/auth/login` 连同凭据一起 POST 给代理，而本项目自己把
    "开着本机 clash/v2ray"称作"几乎默认的姿势"。
    """
    import config

    old = config._v.get("PROXY_SKIP_INTERNAL", True)
    config._v["PROXY_SKIP_INTERNAL"] = True
    try:
        m = config._direct_mounts(qb_url, force_direct=qb_url)
        assert f"all://{host}" in m, f"{qb_url} 的主机没被挂成直连，凭据会走代理"
    finally:
        config._v["PROXY_SKIP_INTERNAL"] = old


def test_the_qb_client_actually_asks_for_that_mount():
    """反向：上一条把 `force_direct=` 自己传了进去 —— 它测的是助手函数，
    **测不出 `_login` 到底传没传**。这正是本项目第③号形状（用例断言自己传的参数）：
    把 `_login` 里的 `force_direct=config.QB_URL` 删掉，上一条照样全绿（实测）。

    这里用 AST 查那次调用真的带了这个关键字。
    """
    import ast
    import inspect
    import textwrap

    from services import qbittorrent

    tree = ast.parse(textwrap.dedent(inspect.getsource(qbittorrent.QBittorrent._login)))
    ok = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "http_client_kwargs"
             and any(k.arg == "force_direct" for k in n.keywords)
             for n in ast.walk(tree))
    assert ok, "_login 没让 qB 的主机强制直连 —— 主机名写法下明文凭据会走代理"


def test_the_qb_client_does_not_follow_redirects():
    """qB 客户端必须 `follow_redirects=False`。

    httpx 对 307/308 **原样复用 request.stream** 重发（只剥 Authorization 头），
    而 SSRF 守卫只拦内网目标、公网目标一律放行。于是 QB_URL 那头随便什么东西回一个
    `307 + Location: https://collect.evil.tld/`，明文的 username/password 就被完整
    POST 到公网去了 —— 不需要 qB 本身被攻陷。

    【断言的是 _login 真的把它关掉了】用 AST 查那一句赋值，不是字符串匹配
    （上面这段注释里就写着 follow_redirects）。
    """
    import ast
    import inspect
    import textwrap

    from services import qbittorrent

    # dedent：方法的源码带着类体的缩进，直接 ast.parse 会 IndentationError
    tree = ast.parse(textwrap.dedent(inspect.getsource(qbittorrent.QBittorrent._login)))
    turned_off = False
    for n in ast.walk(tree):
        if not isinstance(n, ast.Assign):
            continue
        for t in n.targets:
            if (isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                    and t.slice.value == "follow_redirects"
                    and isinstance(n.value, ast.Constant) and n.value.value is False):
                turned_off = True
    assert turned_off, "qB 客户端又跟随重定向了 —— 明文凭据可被 307 重发到任意主机"


def test_the_qb_client_has_a_total_wall_clock_timeout():
    """qB 的每次请求都要有【总】墙钟上限。

    httpx 的 timeout 是**逐块**的：对端每 <30 秒吐一个字节，read timeout 就一直被重置、
    永不触发。而 qB 客户端串在 `poll_once` 的第一个 await 上
    （run_worker → `async with _poll_lock:` → qb_precheck → reachable → _request → _ensure → _login），
    挂住＝采集轮永久停在第一行、`_poll_lock` 永不释放，而设置页的『迁移数据』也要拿这把锁。
    涓流对端不必是恶意的：同机反代、隧道、运营商门户都会这么表现。
    """
    import ast
    import inspect
    import textwrap

    from services import qbittorrent

    for fn in (qbittorrent.QBittorrent._login, qbittorrent.QBittorrent._request):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        has = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "timeout"
                  and isinstance(n.func.value, ast.Name) and n.func.value.id == "asyncio"
                  for n in ast.walk(tree))
        assert has, f"{fn.__name__} 没有 asyncio.timeout 总上限，涓流对端能把它永久挂住"


# ---------------- (R22) 代理地址的保存前校验 ----------------

@pytest.mark.parametrize("value,ok", [
    ("", True),
    ("   ", True),                              # 留空=不用代理
    ("http://127.0.0.1:7890", True),
    ("https://proxy.lan:8443", True),
    ("127.0.0.1:7890", False),                  # 最常见的手填法：没有协议头
    ("192.168.1.10", False),
    ("ftp://x", False),
    ("http://", False),                         # 解析不出主机名
])
def test_a_bad_proxy_url_is_refused_before_it_is_saved(value, ok):
    """`PROXY_URL` 必须在保存前拦下，不能等到出站时才炸。

    httpx 是在**建 AsyncClient 那一步**校验 proxy URL 的：`127.0.0.1:7890`
    （占位符只写了 "http://… 或 https://…"，这是最常见的手填法）会让
    `config.http_client_kwargs` 的**每一个**消费者抛 `ValueError: Unknown scheme` ——
    取源、bgm 识别、通知、qB 全线断，而日志里那一行指向的是各自的 URL，方向全错。

    `_save` 对 WEB_PORT / WEB_HOST / WEB_ALLOW_CIDRS / 下载目录 / ANIME_START_DATE
    都有保存前校验，唯独这一项是裸的 —— 第①号形状。
    """
    from pages.settings import _bad_proxy
    assert (_bad_proxy(value) == "") is ok, f"{value!r} -> {_bad_proxy(value)!r}"


def test_a_bad_proxy_url_really_does_break_every_outbound_client():
    """反向：证明"拦下来"这件事本身有意义 —— 不拦的话 httpx 真的会在建 client 那步抛。"""
    import httpx
    import pytest as _pt

    import config

    old = config._v.get("OPEN_PROXY"), config._v.get("PROXY_URL")
    config._v["OPEN_PROXY"], config._v["PROXY_URL"] = True, "127.0.0.1:7890"
    try:
        with _pt.raises(ValueError):
            httpx.AsyncClient(**config.http_client_kwargs(10, url="https://example.com"))
    finally:
        config._v["OPEN_PROXY"], config._v["PROXY_URL"] = old


def test_the_save_handler_actually_calls_that_check():
    """反向：上一条测的是助手函数。这一条查 `_save` 真的调了它（第③号形状：别只测自己传的参数）。"""
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parent.parent / "pages/settings.py")
                     .read_text(encoding="utf8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_save")

    # 【判据必须是"返回值进了控制流"，不是"名字出现过"】(R24 修)
    # 上一版只 `assert "_bad_proxy" in called` —— 把
    # `if (why := _bad_proxy(...)): ui.notify(...); return` 换成裸的一句
    # `_bad_proxy(updates.get("PROXY_URL", ""))`，闸整条拆掉而守卫仍绿（实测 1129 全绿）。
    # 这与 R21 拆 require_bind_confirm 那次是同一个形状，只是换了个函数。
    guarded = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        if "_bad_proxy" not in ast.dump(node.test):
            continue        # 调用要落在 if 的判据里（海象或先赋值再 if 都算）
        if any(isinstance(n, ast.Return) for n in ast.walk(node)):
            guarded = True
    assert guarded, (
        "_save 里 _bad_proxy 的返回值没有进控制流 —— 校验形同虚设："
        "用户把代理填成 `127.0.0.1:7890` 会保存成功，"
        "而 httpx 在建 client 那一步抛 ValueError，取源/识别/通知/qB 全线断")
