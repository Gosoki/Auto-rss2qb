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


def test_proxy_mode_still_blocks_literal_internal_ip_on_the_torrent_path(monkeypatch, site):
    """取种是严格口径，代理开着也一样拦——download_url 整个来自 RSS 正文。

    【走真实入口 engine.fetch_torrent_bytes】直接调 ssrf.block_internal_request 是在测钩子本身，
    测不到"取种那条路径到底装没装这个钩子"——实测把 core/engine.py 里装钩子那一行删掉，
    全套用例照样全绿，而真实取种会当场把 SSRF 载荷打到 qB 上。
    """
    from core import engine
    monkeypatch.setitem(config._v, "OPEN_PROXY", True)
    monkeypatch.setitem(config._v, "PROXY_URL", "http://127.0.0.1:1")
    url = f"http://127.0.0.1:{site.victim_port}/api/v2/torrents/delete?hashes=all"
    with pytest.raises(ValueError, match="SSRF"):
        asyncio.run(engine.fetch_torrent_bytes(url))
    assert site.victim.hits == [], "SSRF 载荷打到目标服务上了"


_INTERNAL_NOTATIONS = ["127.0.0.1", "10.0.0.1", "192.168.1.1", "[::1]",
                       "2130706433", "0x7f000001", "[::ffff:127.0.0.1]", "localhost"]


@pytest.mark.parametrize("host", _INTERNAL_NOTATIONS)
def test_internal_addresses_in_every_notation_are_refused(host):
    """花式写法（十进制/十六进制整数、IPv4-mapped IPv6）都会被 getaddrinfo 解析成真内网 IP。"""
    req = httpx.Request("GET", f"http://{host}/x")
    req.extensions["autorss_hops"] = [("first.example", None)]
    with pytest.raises(ValueError, match="SSRF"):
        asyncio.run(ssrf.guard_redirect_request(req))


@pytest.mark.parametrize("host", _INTERNAL_NOTATIONS)
def test_internal_addresses_are_refused_with_a_proxy_too(host, monkeypatch):
    """(E-40) **开着代理时同样要拦** —— 上面那条用例只在无代理模式下跑过。

    这就是本项目第②种缺陷形状的教科书例子：判据（"内网地址一律拒"）的作用域覆盖两种模式，
    而验证只覆盖了一种。旧写法在代理分支里只对 `ipaddress` 解析得出来的【字面 IP】判定，
    解析不出来的一律放行 —— 于是这张参数表里的 `2130706433` / `0x7f000001` / `localhost`
    三个会直穿过去；请求交给代理之后，本机 clash/v2ray 自己 inet_aton 出 127.0.0.1
    并从【同一台机器】连过去。而"开着本机 clash/v2ray"按 core/ssrf.py 自己的说法
    "几乎是默认姿势"。
    """
    monkeypatch.setitem(config._v, "OPEN_PROXY", True)
    monkeypatch.setitem(config._v, "PROXY_URL", "http://127.0.0.1:7890")
    assert config.PROXY, "前提没摆对：这条用例要在代理【开着】时跑"
    req = httpx.Request("GET", f"http://{host}/x")
    req.extensions["autorss_hops"] = [("first.example", None)]
    with pytest.raises(ValueError, match="SSRF"):
        asyncio.run(ssrf.guard_redirect_request(req))


def test_public_hosts_still_pass_with_a_proxy(monkeypatch):
    """反向：代理开着时公网地址照常放行，别把正常抓源一起拦了。"""
    monkeypatch.setitem(config._v, "OPEN_PROXY", True)
    monkeypatch.setitem(config._v, "PROXY_URL", "http://127.0.0.1:7890")
    for host in ("1.1.1.1", "93.184.216.34"):
        req = httpx.Request("GET", f"http://{host}/x")
        req.extensions["autorss_hops"] = [("first.example", None)]
        asyncio.run(ssrf.guard_redirect_request(req))       # 不抛就算过


def test_torrent_path_blocks_even_the_first_hop(site):
    """取种走的是【每一跳都判】的严格口径：download_url 整个来自 RSS 正文，没有"用户自填"这回事。

    同样走真实入口。断言落在"受害端零痕迹"上：只断言抛异常是不够的——
    SSRF 的伤害在请求本身（qB 的 delete 是个 GET），事后再抛异常已经晚了。
    """
    from core import engine
    url = f"http://127.0.0.1:{site.victim_port}/feed.xml"    # 首跳就是内网，取种口径下必须拦
    with pytest.raises(ValueError, match="SSRF"):
        asyncio.run(engine.fetch_torrent_bytes(url))
    assert site.victim.hits == []


def test_same_host_redirect_is_allowed(site):
    """同主机同端口的跳转要放行——否则误伤"局域网自建服务把 http 跳到自己的 https"。

    那种跳转没有把请求送到别的地方，而这条守卫要挡的恰恰是"第三方替用户改写了目的地"。
    一律按"是不是重定向"来拦，会把 ntfy / Bark / 自建 Mikan 镜像整类配置打死。
    """
    async def go():
        async with httpx.AsyncClient(**config.http_client_kwargs(10)) as c:
            return await fetch.get_bytes(c, f"http://127.0.0.1:{site.server_port}/self")
    assert asyncio.run(go()) == b"<rss/>"


def test_manual_torrent_link_allows_the_first_hop(site):
    """(E-21) 手填的 .torrent 链接：首跳放行 —— 与 D-05 同口径。

    这个地址是用户自己在输入框里打的，"局域网自建镜像 / 内网私站取种"是正当用法；
    而采集链路的 download_url 整个来自 RSS 正文，没有"用户自填"这回事，所以那边首跳也不信。
    两处口径原本分家，只是因为 fetch_torrent_bytes 当初只服务 RSS 来源、后来被手动下载复用了。
    """
    import asyncio

    from core import engine
    url = f"http://127.0.0.1:{site.victim_port}/feed.xml"
    # 严格口径（采集链路）：首跳就拦，受害端零痕迹
    with pytest.raises(ValueError, match="SSRF"):
        asyncio.run(engine.fetch_torrent_bytes(url))
    assert site.victim.hits == []
    # 宽松口径：首跳放行 —— 请求真的打到了那台内网服务上
    try:
        asyncio.run(engine.fetch_torrent_bytes(url, strict=False))
    except Exception:
        pass                      # 拿不拿得到内容无所谓，这里断言的是"请求发出去了"
    assert site.victim.hits, "手填链接的首跳被拦了 —— 内网私站取种会用不了"


def test_the_manual_page_actually_uses_the_loose_mode(site, monkeypatch):
    """(E-21) 光有 strict 形参不够 —— 手动下载那条【调用点】必须真的传 strict=False。

    第一版用例只调 engine.fetch_torrent_bytes(strict=False) 直接验形参，
    于是把 core/manual.py 里那个实参删掉，用例照样绿 —— 守卫盖不住调用点，
    正是本项目第②种缺陷形状。这条走真实的 add_manual。
    """
    import asyncio

    from core import engine, manual as MAN
    monkeypatch.setitem(config._v, "QB_ENABLED", True)
    monkeypatch.setattr(engine, "qb_is_local", lambda: False)

    async def ok(*a, **kw):
        return True
    monkeypatch.setattr(engine.qb, "add_torrent", ok)

    url = f"http://127.0.0.1:{site.victim_port}/feed.xml"
    asyncio.run(MAN.add_manual(url, None, "/tmp"))
    assert site.victim.hits, ("手动下载的 .torrent 链接首跳被拦了 —— "
                              "调用点没传 strict=False（内网私站取种会用不了）")


@pytest.mark.parametrize("ip", ["198.18.0.1", "198.18.255.254", "198.19.0.1", "198.19.255.254"])
def test_fake_ip_pool_is_exempt_in_proxy_mode(ip, monkeypatch):
    """(R20 回归) 代理的 fake-ip 池不能被当成内网 —— 否则代理模式下【全部出站】都会被拒。

    clash-meta 默认 `fake-ip-range: 198.18.0.1/16`、sing-box 默认 `198.18.0.0/15`，
    而 ipaddress 把 198.18.0.0/15（RFC 2544 基准段）判为 is_private=True。
    开着 fake-ip 时本机解析【每一个域名】都得到这一段里的地址 —— E-40 那条收紧
    如果不豁免它，抓源/取种/bgm 会一律被拒，而 core/ssrf.py 自己的说明就写着
    "开着本机 clash/v2ray 几乎是默认姿势"。

    豁免它是安全的：这一段在正常网络里不承载任何真实服务，只有本机解析器【就是】
    fake-ip 代理时才会返回它，而那时真正的连接由代理去建。
    """
    import ipaddress
    assert ipaddress.ip_address(ip).is_private, "前提变了：这一段不再被判为 private"
    assert asyncio.run(ssrf.safe_ip_for(ip)) is None, "严格口径下仍应拒"
    assert asyncio.run(ssrf.safe_ip_for(ip, allow_fake_ip=True)) == ip, "代理模式下被误拒了"


@pytest.mark.parametrize("ip", ["127.0.0.1", "192.168.1.5", "10.0.0.1", "169.254.1.1", "0.0.0.0"])
def test_real_internal_addresses_stay_blocked_even_with_fake_ip_allowance(ip):
    """豁免只针对 fake-ip 那一段，真正的环回/局域网/链路本地照拒。"""
    assert asyncio.run(ssrf.safe_ip_for(ip, allow_fake_ip=True)) is None, f"{ip} 被放行了"
