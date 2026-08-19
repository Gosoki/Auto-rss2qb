"""SSRF 守卫：首跳放行、重定向后强制内网判定（D-05）。

这条守卫防的是"被攻陷的源站回一个 302，让常驻协程周期性地对同机 qB 发可控 GET"。
用真实的 httpx + 真实的本地 HTTP 服务端跑，不 mock：这里的正确性【全靠 httpx 对
extensions 的浅拷贝语义】——换个 httpx 版本就可能悄悄失效，而 mock 掉就测不出来了。
"""
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

import config
from core import ssrf
from services import fetch


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.hits.append(self.path)
        if self.path.startswith("/evil"):
            # 跳到【另一个端口】——现实里那就是同机的 qB。同主机同端口不算"送到别的地方"，
            # 见 test_same_host_redirect_is_allowed。
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.victim_port}/pwned")
            self.end_headers()
        elif self.path.startswith("/self"):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/feed.xml")
            self.end_headers()
        else:
            body = b"<rss/>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *a):
        pass


def _serve():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    srv.hits = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def site():
    """两个服务端：srv 扮演用户填的源站，victim 扮演同机的 qB。"""
    srv, victim = _serve(), _serve()
    srv.victim_port = victim.server_address[1]
    victim.victim_port = victim.server_address[1]
    srv.victim = victim
    yield srv
    srv.shutdown()
    victim.shutdown()


def test_first_hop_to_lan_is_allowed(site):
    """首跳放行：feed 地址是用户自己在设置页填的，自建 Mikan 镜像是正当用法。"""
    async def go():
        async with httpx.AsyncClient(**config.http_client_kwargs(10)) as c:
            return await fetch.get_bytes(c, f"http://127.0.0.1:{site.server_port}/feed.xml")
    assert asyncio.run(go()) == b"<rss/>"


def test_redirect_into_lan_is_blocked_before_the_request_lands(site):
    """重定向后的跳被拦——而且要在【请求发出去之前】拦。

    只断言抛异常是不够的：SSRF 的伤害在请求本身（qB 的 delete 是个 GET），
    事后再抛异常已经晚了。所以这里断言目标端点【一次都没被打到】。
    """
    async def go():
        async with httpx.AsyncClient(**config.http_client_kwargs(10)) as c:
            await fetch.get_bytes(c, f"http://127.0.0.1:{site.server_port}/evil")
    with pytest.raises(ValueError, match="SSRF"):
        asyncio.run(go())
    assert [p for p in site.victim.hits if "pwned" in p] == [], "SSRF 载荷打到了目标服务上"


def test_hop_marker_survives_the_redirect():
    """跳序标记靠 httpx `Request(extensions=...)` 的浅拷贝传递——这是整条守卫的地基。

    httpx 若改成深拷贝，标记就永远是"首跳"，守卫会【静默失效】（不报错、只是不再拦）。
    这条用例是那个变更的哨兵。
    """
    req = httpx.Request("GET", "http://example.com/")
    asyncio.run(ssrf.guard_redirect_request(req))
    hop2 = httpx.Request("GET", "http://example.com/2", extensions=req.extensions)
    assert hop2.extensions["autorss_hops"] is req.extensions["autorss_hops"]
    assert len(hop2.extensions["autorss_hops"]) == 1     # 非空 = 不再是首跳


def test_proxy_mode_skips_dns_but_not_literal_internal_ips(monkeypatch):
    """开了代理只跳过【解析 + 钉 IP】，不跳过整条守卫。

    「目标由代理侧解析、本地判定既无意义又会误伤」只对**域名**成立。URL 里写死的
    字面私网 IP 是谁解析都一样的地址，不存在误伤——而本项目要抓 nyaa/bgm，
    「开着本机 clash」几乎是默认姿势，整条跳过等于把守卫挂在一个与安全无关的开关上。
    """
    monkeypatch.setitem(config._v, "OPEN_PROXY", True)
    monkeypatch.setitem(config._v, "PROXY_URL", "http://127.0.0.1:1")

    lan = httpx.Request("GET", "http://127.0.0.1/x")
    lan.extensions["autorss_hops"] = [("evil.example", None)]   # 冒充"已经是第二跳"
    with pytest.raises(ValueError, match="SSRF"):
        asyncio.run(ssrf.guard_redirect_request(lan))

    # 域名照旧放行：本地解析在代理模式下判不了，也不该判
    wan = httpx.Request("GET", "http://example.com/x")
    wan.extensions["autorss_hops"] = [("evil.example", None)]
    asyncio.run(ssrf.guard_redirect_request(wan))


def test_proxy_mode_still_blocks_literal_internal_ip_on_the_torrent_path(monkeypatch):
    """取种是严格口径，代理开着也一样拦——download_url 整个来自 RSS 正文。"""
    monkeypatch.setitem(config._v, "OPEN_PROXY", True)
    monkeypatch.setitem(config._v, "PROXY_URL", "http://127.0.0.1:1")
    with pytest.raises(ValueError, match="SSRF"):
        asyncio.run(ssrf.block_internal_request(
            httpx.Request("GET", "http://127.0.0.1:8080/api/v2/torrents/delete?hashes=all")))


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.1", "192.168.1.1", "[::1]",
                                  "2130706433", "0x7f000001", "[::ffff:127.0.0.1]"])
def test_internal_addresses_in_every_notation_are_refused(host):
    """花式写法（十进制/十六进制整数、IPv4-mapped IPv6）都会被 getaddrinfo 解析成真内网 IP。"""
    req = httpx.Request("GET", f"http://{host}/x")
    req.extensions["autorss_hops"] = [("first.example", None)]
    with pytest.raises(ValueError, match="SSRF"):
        asyncio.run(ssrf.guard_redirect_request(req))


def test_torrent_path_blocks_even_the_first_hop():
    """取种走的是【每一跳都判】的严格口径：download_url 整个来自 RSS 正文，没有"用户自填"这回事。"""
    with pytest.raises(ValueError, match="SSRF"):
        asyncio.run(ssrf.block_internal_request(httpx.Request("GET", "http://127.0.0.1/x.torrent")))


def test_same_host_redirect_is_allowed(site):
    """同主机同端口的跳转要放行——否则误伤"局域网自建服务把 http 跳到自己的 https"。

    那种跳转没有把请求送到别的地方，而这条守卫要挡的恰恰是"第三方替用户改写了目的地"。
    一律按"是不是重定向"来拦，会把 ntfy / Bark / 自建 Mikan 镜像整类配置打死。
    """
    async def go():
        async with httpx.AsyncClient(**config.http_client_kwargs(10)) as c:
            return await fetch.get_bytes(c, f"http://127.0.0.1:{site.server_port}/self")
    assert asyncio.run(go()) == b"<rss/>"
