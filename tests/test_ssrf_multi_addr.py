"""取种钉 IP 后的换地址重试：只在建连失败时换下一个【同样过了内网判定】的候选地址。

场景："有全局 IPv6、上游 v6 不通"，AAAA 排在前面——只钉第一个地址时取种全挂。
走真实入口 engine.fetch_torrent_bytes；底层传输换成假的，不真连任何地址。
"""
import asyncio
import socket

import httpx
import pytest

from core import engine

V6, V4, V4B, V4C = "2606:4700::1111", "1.1.1.1", "1.0.0.1", "8.8.8.8"


@pytest.fixture
def net(monkeypatch):
    """resolve(...) 设定解析结果；behave[ip] 设定连该地址时的行为；calls 记每次实际连的地址与 Host。"""
    st = {"addrs": [], "behave": {}, "calls": []}

    async def fake_getaddrinfo(self, host, port, **kw):
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
                for a in st["addrs"]]

    loop = asyncio.new_event_loop()
    monkeypatch.setattr(type(loop), "getaddrinfo", fake_getaddrinfo, raising=False)
    loop.close()

    async def fake_send(self, request):
        ip = request.url.host
        st["calls"].append((ip, request.headers.get("Host"), request.extensions.get("sni_hostname")))
        b = st["behave"].get(ip, "ok")
        if b == "connect":
            raise httpx.ConnectError(f"连不上 {ip}", request=request)
        if b == "connect_timeout":
            raise httpx.ConnectTimeout(f"建连超时 {ip}", request=request)
        if b == "read_timeout":
            raise httpx.ReadTimeout(f"读超时 {ip}", request=request)
        if b == "404":
            return httpx.Response(404, stream=httpx.ByteStream(b"nope"), request=request)
        return httpx.Response(200, stream=httpx.ByteStream(b"d4:infoe"), request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_send)
    return st


def test_falls_back_to_second_address_when_first_cannot_connect(net):
    net["addrs"] = [V6, V4]
    net["behave"] = {V6: "connect"}
    data = asyncio.run(engine.fetch_torrent_bytes("https://tracker.example:8443/x.torrent"))
    assert data == b"d4:infoe"
    assert [c[0] for c in net["calls"]] == [V6, V4], "没有换到第二个地址"
    # Host 头与 SNI 不随换地址改变
    assert all(c[1] == "tracker.example:8443" and c[2] == "tracker.example" for c in net["calls"])


def test_connect_timeout_also_falls_back(net):
    net["addrs"] = [V6, V4]
    net["behave"] = {V6: "connect_timeout"}
    assert asyncio.run(engine.fetch_torrent_bytes("http://tracker.example/x.torrent")) == b"d4:infoe"
    assert [c[0] for c in net["calls"]] == [V6, V4]


def test_internal_candidate_refuses_the_whole_host(net):
    """候选里混着内网地址：整体拒，一个地址都不连（不是挑出公网的那个去连）。"""
    net["addrs"] = [V4, "127.0.0.1"]
    with pytest.raises(ValueError, match="SSRF"):
        asyncio.run(engine.fetch_torrent_bytes("http://tracker.example/x.torrent"))
    assert net["calls"] == []


def test_all_fail_raises_the_original_error_and_tries_at_most_three(net):
    net["addrs"] = [V6, V4, V4B, V4C]
    net["behave"] = {a: "connect" for a in net["addrs"]}
    with pytest.raises(httpx.ConnectError, match=f"连不上 {V6}"):
        asyncio.run(engine.fetch_torrent_bytes("http://tracker.example/x.torrent"))
    assert [c[0] for c in net["calls"]] == [V6, V4, V4B]


@pytest.mark.parametrize("behave,exc", [("404", httpx.HTTPStatusError),
                                        ("read_timeout", httpx.ReadTimeout)])
def test_non_connect_errors_are_not_retried(net, behave, exc):
    """请求已经发出去（或服务端已应答）就不换地址重发。"""
    net["addrs"] = [V6, V4]
    net["behave"] = {V6: behave}
    with pytest.raises(exc):
        asyncio.run(engine.fetch_torrent_bytes("http://tracker.example/x.torrent"))
    assert [c[0] for c in net["calls"]] == [V6]
