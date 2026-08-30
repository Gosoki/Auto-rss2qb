"""日志与页面里不能出现凭据。

本项目有三类 URL 内嵌的秘密，且它们都没有别的配置方式：
  · 推送密钥在 **path** 里（Bark/Server酱：https://api.day.app/<密钥>/消息）
  · 推送的 basic-auth 在 **userinfo** 里（config 里没有 NOTIFY_USER/NOTIFY_PASS，这是唯一方式）
  · Mikan『我的番组』订阅 token 在 **query** 里
而日志会落盘（data/autorss.log 滚动 5 份）并可从 /logs 页整份下载。
"""
import httpx
import pytest

import config
from services.fetch import redact, safe_url
from services.notify import _safe_host

_SECRETS = ["hunter2", "SECRETKEY22CHARS0000", "MYTOKEN123"]


@pytest.mark.parametrize("url,want", [
    ("https://alice:hunter2@ntfy.homelab/mytopic", "ntfy.homelab"),
    ("https://api.day.app/SECRETKEY22CHARS0000/msg", "api.day.app"),
    ("http://user:pw@host:8080/x", "host:8080"),
    ("", "(未配置)"),
])
def test_safe_host_drops_userinfo(url, want, monkeypatch):
    """`_safe_host` 不能返回 netloc —— 那一段【含 userinfo】。

    `urlsplit('https://alice:hunter2@h/t').netloc == 'alice:hunter2@h'`，
    于是任何一次推送失败都会把 basic-auth 口令写进日志。
    （core/ssrf.py 用的是 httpx.URL.netloc，那个不含 userinfo，所以那边抄对了。）
    """
    monkeypatch.setitem(config._v, "NOTIFY_URL", url)
    assert _safe_host() == want


@pytest.mark.parametrize("url,want", [
    ("https://mikanani.me/RSS/MyBangumi?token=MYTOKEN123", "https://mikanani.me"),
    ("https://api.day.app/SECRETKEY22CHARS0000/msg", "https://api.day.app"),
    ("https://alice:hunter2@ntfy.lan/t", "https://ntfy.lan"),
    ("http://127.0.0.1:8080/x", "http://127.0.0.1:8080"),
])
def test_safe_url_keeps_only_scheme_and_host(url, want):
    """path / query / userinfo 三处都要去掉——本项目的秘密恰好分布在这三处。"""
    assert safe_url(url) == want


def test_redact_cleans_urls_inside_third_party_messages():
    """光改我们自己的消息不够：httpx 抛的异常 str() 就带完整 URL。

    Mikan『我的番组』随便一次 403/500，`log.error("抓取失败 %s: %s", name, e)`
    就把 ?token=… 整条写进 data/autorss.log。
    """
    url = "https://mikanani.me/RSS/MyBangumi?token=MYTOKEN123"
    # 直接用 httpx 真实产生的那句消息（Server error '500' for url '<完整URL>'）
    out = redact(f"Server error '500 Internal Server Error' for url '{url}'")
    assert "MYTOKEN123" not in out and "mikanani.me" in out


def test_our_own_fetch_errors_do_not_carry_the_full_url():
    """`services/fetch` 造的异常消息以前是以完整 URL 结尾的。"""
    import asyncio

    from services import fetch

    url = "https://api.day.app/SECRETKEY22CHARS0000/msg"

    def _handler(request):
        # 用 stream 回内容，避免 MockTransport 上的 content 被读两次（StreamConsumed）
        return httpx.Response(200, stream=httpx.ByteStream(b"x" * 100), request=request,
                              headers={"Content-Length": "100"})

    async def _go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as c:
            with pytest.raises(fetch.TooLarge) as ei:
                await fetch.get_bytes(c, url, cap=10)
        return str(ei.value)

    msg = asyncio.run(_go())
    assert "SECRETKEY22CHARS0000" not in msg, f"异常消息里带着路径密钥：{msg}"
    assert "api.day.app" in msg


@pytest.mark.parametrize("tok", ["A1b2+C3d4/e5=", "tok&admin", "tok#1", "my token", "令牌abc"])
def test_callback_token_is_url_encoded_in_the_curl_command(tok):
    """设置页给的那条 curl 里，token 必须 URL 编码。

    `openssl rand -base64` 产出的 token 带 `+` 是最现实的情形，而 `+` 在 query 里被解成空格。
    实测：`+` `&` `#` 都会让回调收到错的 token，空格会让 curl 直接退出码 3（请求没发出去），
    而全程静默（curl -s、qB 不显示外部程序输出、api 侧一行日志都没有）。
    """
    from urllib.parse import parse_qs, urlsplit

    from pages.settings import qb_callback_curl

    # 【必须调生产函数】原先这条用例自己调 quote() 拼同一条命令、从不 import 本模块——
    # 把生产代码里那句 quote(...) 换回裸 tok，全量用例照样全绿（实测过）。
    cmd = qb_callback_curl(tok, 2333)
    url = cmd.split('"')[1]
    assert parse_qs(urlsplit(url).query)["t"] == [tok], "编码后解不回原值"
    assert " " not in url, "URL 里有裸空格，curl 会当成两个参数"


def test_empty_token_yields_no_t_parameter():
    """没配 token 时命令里不该出现空的 &t=——那会让 api 侧走进「设了 token」的分支。"""
    from pages.settings import qb_callback_curl
    assert "&t=" not in qb_callback_curl("", 2333)


# ---------------- 代理：内网地址不走代理（E-20） ----------------

def _proxy_of(client, url):
    """这个 client 打这条 URL 时会不会走代理；走则返回代理主机，否则 None。"""
    t = client._transport_for_url(httpx.URL(url))
    pool = getattr(t, "_pool", None)
    px = getattr(pool, "_proxy_url", None) if pool else None
    return px.host.decode() if px else None


@pytest.mark.parametrize("target", [
    "http://127.0.0.1:8080/api/v2/auth/login",
    "http://192.168.1.5:8080/x",
    "http://10.0.0.9/x",
    "http://172.16.3.4/x",
    "http://localhost:8080/x",
])
def test_internal_targets_bypass_the_proxy(target, monkeypatch):
    """内网/本机地址一律直连。

    qB 通常就在 127.0.0.1 或局域网上，一旦被代理走，登录请求里的 username/password
    是**明文** POST 给那个代理的；而代理回的 502 还会落进「凭据错」分支、冷却 300 秒，
    并在日志里让用户去改密码——把锅甩给用户。
    """
    monkeypatch.setenv("HTTP_PROXY", "http://10.9.9.9:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://10.9.9.9:1")
    monkeypatch.setitem(config._v, "PROXY_SKIP_INTERNAL", True)
    with httpx.Client(**config.http_client_kwargs(5, url=target)) as c:
        assert _proxy_of(c, target) is None, "内网地址被代理走了"


def test_public_targets_still_use_the_proxy(monkeypatch):
    """而公网仍要走代理——「跳过内网」不能把代理功能整个废掉。"""
    monkeypatch.setitem(config._v, "PROXY_SKIP_INTERNAL", True)
    monkeypatch.setitem(config._v, "OPEN_PROXY", True)
    monkeypatch.setitem(config._v, "PROXY_URL", "http://10.9.9.9:1")
    with httpx.Client(**config.http_client_kwargs(5, url="https://api.bgm.tv/x")) as c:
        assert _proxy_of(c, "https://api.bgm.tv/x") == "10.9.9.9"


def test_env_proxy_is_overridden_for_internal_targets(monkeypatch):
    """环境变量里的 HTTP_PROXY 也要被压住。

    httpx 默认 trust_env=True，那条路径**不经过设置页的「启用代理」开关**——
    开关关着时它照样接管。只有挂载表压得住它（实测 mounts 同时压过环境代理与显式 proxy）。
    """
    monkeypatch.setenv("HTTP_PROXY", "http://10.9.9.9:1")
    monkeypatch.setitem(config._v, "PROXY_SKIP_INTERNAL", True)
    monkeypatch.setitem(config._v, "OPEN_PROXY", False)      # 开关是【关】的
    url = "http://127.0.0.1:8080/x"
    with httpx.Client(**config.http_client_kwargs(5, url=url)) as c:
        assert _proxy_of(c, url) is None


def test_qb_login_never_goes_through_the_proxy(monkeypatch):
    """qB 的登录请求不能被代理走——那条请求里的 username/password 是**明文**的。

    qB 客户端曾是全仓唯一自建 client 的出站，完全绕过代理设置；而 httpx 默认
    trust_env=True，于是环境里的 HTTP_PROXY 会把 /auth/login 连同凭据一起 POST 给代理，
    代理回的 502 还会落进「凭据错」分支、冷却 300 秒并让用户去改密码——把锅甩给用户。

    【断言的是行为，不是源码文本】上一版这条用例 grep `_login` 的源码找
    "http_client_kwargs"，而**我自己写的注释里就有这几个字**——把代码回退掉照样绿。
    """
    import asyncio
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = []

    class _Proxy(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen.append(self.rfile.read(n).decode("utf-8", "replace"))
            self.send_response(502)
            self.end_headers()

        def do_GET(self):
            seen.append(self.path)
            self.send_response(502)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _Proxy)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{srv.server_address[1]}")
        monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{srv.server_address[1]}")
        monkeypatch.setitem(config._v, "PROXY_SKIP_INTERNAL", True)
        monkeypatch.setitem(config._v, "QB_URL", "http://127.0.0.1:9/")   # 9=discard，必然连不上
        monkeypatch.setitem(config._v, "QB_USERNAME", "admin")
        monkeypatch.setitem(config._v, "QB_PASSWORD", "s3cr3t-pw")

        from services.qbittorrent import QBittorrent
        asyncio.run(QBittorrent()._login())
    finally:
        srv.shutdown()

    assert seen == [], f"qB 登录被代理走了，代理收到：{[x[:60] for x in seen]}"
    assert not any("s3cr3t-pw" in x for x in seen)
