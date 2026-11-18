"""三条网络判据（r41）：代理 502 算断网 / 设置页不在事件循环上做 DNS / qB 反代 5xx 不当凭据错。"""
import asyncio
import logging
import socket
import time
import types

import httpx
import pytest

import config
from core import health
from core import ipfilter as F
from services.qbittorrent import QBittorrent, _LOGIN_COOLDOWN_AUTH, _LOGIN_COOLDOWN_NET


# ---------------- 1) 开代理时断网：ProxyError 502 ----------------

@pytest.fixture
def down3(monkeypatch, cfg):
    monkeypatch.setattr(health, "_fail", {})
    cfg(ALERT_SOURCE_FAIL_ROUNDS=1)
    names = {"ANi", "Mikan(兜底)", "喵萌(简中)"}
    health.forget_sources(names)

    def fail_all(e):
        for n in names:
            health.record_source(n, False, f"{type(e).__name__}: {e}")
        return health.network_down()
    return fail_all


@pytest.mark.parametrize("msg,want", [("502 Bad Gateway", True), ("503 Service Unavailable", True),
                                      ("504 Gateway Timeout", True),
                                      ("407 Proxy Authentication Required", False)])
def test_proxy_error_gateway_is_network_down(down3, msg, want):
    """https 经 CONNECT 走代理，宿主机断网时 httpx 抛 ProxyError('502 Bad Gateway') 而不是 ConnectError。
    407 是代理认证没配对，不是断网。"""
    assert down3(httpx.ProxyError(msg)) is want


def test_connect_error_still_network_down(down3):
    assert down3(httpx.ConnectError("[Errno -3] Temporary failure in name resolution")) is True


# ---------------- 2) auto_seed_base 不在同步路径做 DNS ----------------

class _Sock:
    calls: list = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def connect(self, addr):
        _Sock.calls.append(addr[0])

    def getsockname(self):
        return ("10.0.0.5", 1)


@pytest.fixture
def no_dns(monkeypatch):
    _Sock.calls = []

    def boom(*a, **k):
        raise AssertionError("同步路径不许 getaddrinfo")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    # 只换掉 ipfilter 眼里的 socket 模块：全局换 socket.socket 会把事件循环自己的 self-pipe 也换掉
    monkeypatch.setattr(F, "socket", types.SimpleNamespace(
        socket=_Sock, getaddrinfo=boom, AF_INET=socket.AF_INET, SOCK_DGRAM=socket.SOCK_DGRAM))


def test_hostname_not_resolved_on_sync_path(cfg, no_dns):
    cfg(QB_URL="http://qb.lan:8080", IPFILTER_SEED_URL="")
    assert F.auto_seed_base() == ""
    assert _Sock.calls == [], "主机名不能交给同步的 socket.connect（会在里面同步解析）"


def test_literal_ip_still_computed(cfg, no_dns):
    cfg(QB_URL="http://10.0.0.230:8080")
    assert F.auto_seed_base() == f"http://10.0.0.5:{config.WEB_PORT}"
    assert _Sock.calls == ["10.0.0.230"]


async def test_hostname_resolved_async(cfg, no_dns, monkeypatch):
    cfg(QB_URL="http://qb.lan:8080", IPFILTER_SEED_URL="")
    loop = asyncio.get_running_loop()

    async def fake(host, port, **kw):
        assert host == "qb.lan"
        return [(socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("10.0.0.230", port))]
    monkeypatch.setattr(loop, "getaddrinfo", fake)
    assert await F.seed_base() == f"http://10.0.0.5:{config.WEB_PORT}"
    assert _Sock.calls == ["10.0.0.230"]


# ---------------- 3) qB 登录：反代 5xx 走网络档 ----------------

def _login_with(monkeypatch, cfg, code, text=""):
    cfg(QB_URL="http://qb.test:8080", QB_USERNAME="u", QB_PASSWORD="p")
    transport = httpx.MockTransport(lambda req: httpx.Response(code, text=text))
    monkeypatch.setattr(config, "http_client_kwargs", lambda *a, **k: {"transport": transport})
    c = QBittorrent()
    t0 = time.monotonic()
    assert asyncio.run(c._login()) is None
    return c, c._fail_until - t0


@pytest.mark.parametrize("code", [502, 503])
def test_proxy_5xx_is_network_cooldown(monkeypatch, cfg, caplog, code):
    with caplog.at_level(logging.INFO, logger="autorss"):
        c, wait = _login_with(monkeypatch, cfg, code, "Bad Gateway")
    assert wait <= _LOGIN_COOLDOWN_NET + 1
    assert c._net_fails == 1
    assert "密码" not in caplog.text


@pytest.mark.parametrize("code,text", [(200, "Fails."), (403, "Your IP address has been banned")])
def test_credential_errors_keep_long_cooldown(monkeypatch, cfg, caplog, code, text):
    with caplog.at_level(logging.INFO, logger="autorss"):
        c, wait = _login_with(monkeypatch, cfg, code, text)
    assert wait > _LOGIN_COOLDOWN_NET + 1 and wait <= _LOGIN_COOLDOWN_AUTH + 1
    assert c._net_fails == 0
    assert "密码" in caplog.text
