"""网段白名单：唯一的访问控制手段，出错的两个方向都很贵——
放宽了等于把无鉴权面板（含 qB 明文密码、可让 qB 往任意目录下载、可带文件删种）暴露给整个内网；
收紧过头则把用户挡在自己的面板外，而这是个跑在无头 LXC 里的服务。
"""
import pytest

from core.netguard import SubnetGuard, _allowed, _parse, not_blocked_by


# ---------------- CIDR 解析容错 ----------------

@pytest.mark.parametrize("raw,count", [
    ("", 0), ("   ", 0), (",,,", 0),
    ("192.168.1.0/24", 1),
    (" 192.168.1.0/24 , 10.0.0.0/8 ", 2),
    ("192.168.1.5/24", 1),                       # strict=False：主机位非零也收
    ("192.168.1.0/24,不是网段,10.0.0.0/8", 2),    # 坏条目跳过，好的照收
    ("不是网段", 0),
    ("192.168.1.1", 1),                          # 裸 IP = /32
    ("fd00::/8", 1),
])
def test_parse_is_tolerant(raw, count):
    assert len(_parse(raw)) == count


# ---------------- 放行判据 ----------------

@pytest.mark.parametrize("ip", ["127.0.0.1", "127.1.2.3", "::1", "::ffff:127.0.0.1"])
def test_loopback_always_allowed(ip):
    """回环恒放行——这是"误配也锁不死自己"的唯一保证，改白名单前后都必须成立。"""
    assert _allowed(ip, _parse("10.0.0.0/8")) is True
    assert _allowed(ip, ()) is True


def test_lan_matching():
    nets = _parse("192.168.1.0/24")
    assert _allowed("192.168.1.50", nets) is True
    assert _allowed("192.168.2.50", nets) is False


def test_ipv4_mapped_v6_is_unwrapped():
    """::ffff:192.168.1.50 与 192.168.1.50 必须同判——否则同一台机器换个协议栈就能绕过。"""
    nets = _parse("192.168.1.0/24")
    assert _allowed("::ffff:192.168.1.50", nets) is True
    assert _allowed("::ffff:10.9.9.9", nets) is False


def test_versions_are_not_cross_matched():
    """v4 网段不该放行 v6 地址，反之亦然。"""
    assert _allowed("fd00::1", _parse("0.0.0.0/0")) is False
    assert _allowed("10.0.0.1", _parse("::/0")) is False


@pytest.mark.parametrize("bad", ["", None, "not-an-ip", "999.999.999.999", "192.168.1.1/24"])
def test_unparseable_peer_is_denied(bad):
    """取不到/解析不出对端 IP 时 fail-closed。"""
    assert _allowed(bad, _parse("192.168.1.0/24")) is False


def test_all_entries_unparseable_falls_closed_but_keeps_loopback():
    """白名单配了但全是坏值 → 只有回环能进。fail-closed，且本机还能进去改回来。"""
    nets = _parse("垃圾,更多垃圾")
    assert nets == ()
    assert _allowed("192.168.1.50", nets) is False
    assert _allowed("127.0.0.1", nets) is True


# ---------------- 自锁检测（设置页存前用） ----------------

def test_not_blocked_by_semantics():
    assert not_blocked_by("192.168.1.50", "") is True            # 空白名单 = 不限制
    assert not_blocked_by("192.168.1.50", "   ") is True
    assert not_blocked_by("192.168.1.50", "10.0.0.0/8") is False  # 会被挡 → 设置页该拦下保存
    assert not_blocked_by("192.168.1.50", "192.168.1.0/24") is True
    assert not_blocked_by("127.0.0.1", "10.0.0.0/8") is True     # 回环恒放行
    # 【名字就是契约】ip 为空是"无法判定"而不是"确定放行"；调用方要的若是"确定能进"，
    # 必须自己先保证 ip 非空（见 pages/settings.py 的 `my_ip and not_blocked_by(...)`）。
    assert not_blocked_by("", "10.0.0.0/8") is True
    assert not_blocked_by(None, "10.0.0.0/8") is True


# ---------------- 中间件 ----------------

@pytest.fixture(autouse=True)
def config_loaded(monkeypatch):
    """默认按"配置已从库里读出来"跑（真实进程的常态）。
    单测进程里没人调 load_from_db，不设的话中间件会一律走 fail-closed 分支。
    要测 fail-closed 的用例自己覆盖成 False（见文件末尾）。"""
    import config as C
    monkeypatch.setattr(C, "loaded_from_db", True)


async def _run(scope, cidrs, cfg):
    cfg(WEB_ALLOW_CIDRS=cidrs)
    sent, passed = [], []

    async def app(s, r, sd):
        passed.append(True)

    async def send(msg):
        sent.append(msg)
    await SubnetGuard(app)(scope, None, send)
    return bool(passed), sent


async def test_http_and_ws_both_guarded(cfg):
    """HTTP 与 WebSocket 都要挡：只挡 HTTP 的话，页面打不开但 socket.io 通道还能用。"""
    for kind in ("http", "websocket"):
        ok, sent = await _run({"type": kind, "client": ("10.9.9.9", 1)}, "192.168.1.0/24", cfg)
        assert ok is False, kind
        assert sent, kind
    ok, _ = await _run({"type": "http", "client": ("192.168.1.5", 1)}, "192.168.1.0/24", cfg)
    assert ok is True


async def test_empty_whitelist_passes_everything(cfg):
    ok, _ = await _run({"type": "http", "client": ("1.2.3.4", 1)}, "", cfg)
    assert ok is True


async def test_non_http_scopes_pass_through(cfg):
    """lifespan 之类的 scope 不该被拦——拦了服务起不来。"""
    ok, _ = await _run({"type": "lifespan"}, "192.168.1.0/24", cfg)
    assert ok is True


async def test_missing_client_is_rejected_when_whitelist_set(cfg):
    ok, _ = await _run({"type": "http", "client": None}, "192.168.1.0/24", cfg)
    assert ok is False


async def test_reject_response_does_not_leak_the_whitelist(cfg):
    """403 的正文不能回显白名单内容——那等于告诉扫描者该伪造成哪个网段。"""
    _, sent = await _run({"type": "http", "client": ("10.9.9.9", 1)}, "192.168.1.0/24", cfg)
    body = b"".join(m.get("body", b"") for m in sent).decode()
    assert "192.168" not in body and "/24" not in body


# ---------------- 配置没读出来时必须 fail-closed（R2 P0） ----------------

@pytest.fixture
def unloaded_config(monkeypatch):
    """模拟"建表/迁移失败，config.load_from_db 没跑成"——内存里全是硬编码默认值。
    覆盖掉上面的 autouse fixture。"""
    import config as C
    monkeypatch.setattr(C, "loaded_from_db", False)


async def test_fails_closed_when_config_never_loaded(cfg, unloaded_config):
    """(R2) WEB_ALLOW_CIDRS 的硬编码默认值是空串，而空串在本模块的含义是【放行一切】。
    于是"数据库建表失败"这条本该更安全的路径，反而把无鉴权面板对整个局域网敞开。
    读不到配置就只放行回环——本模块 docstring 声明的就是 fail-closed。"""
    cfg(WEB_ALLOW_CIDRS="")                       # 默认值：正常情况下等于"不限制"
    ok, _ = await _run({"type": "http", "client": ("192.168.1.50", 1)}, "", cfg)
    assert ok is False, "配置没读出来时不能放行局域网地址"


async def test_not_ready_says_it_is_not_a_whitelist_problem(cfg, unloaded_config):
    """(R3) 这一支拒绝的原因是"数据库还没初始化好"。用默认那句"你的网段不在允许访问列表内"
    会把用户支去查路由和防火墙——而他可能根本没配过白名单，方向完全错。"""
    _, sent = await _run({"type": "http", "client": ("192.168.1.50", 1)}, "", cfg)
    body = b"".join(m.get("body", b"") for m in sent).decode()
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    assert status == 503, "不是权限问题，是没就绪"
    assert "白名单" in body and "journalctl" in body


async def test_loopback_still_gets_in_when_config_never_loaded(cfg, unloaded_config):
    """但本机必须进得去——否则用户没法到设置页把库修好，等于把自己锁死。"""
    ok, _ = await _run({"type": "http", "client": ("127.0.0.1", 1)}, "", cfg)
    assert ok is True


async def test_loaded_config_takes_the_normal_path(cfg):
    """配置读出来了就走原路径：空白名单 = 不限制，谁都能进（autouse fixture 已置 True）。"""
    ok, _ = await _run({"type": "http", "client": ("192.168.1.50", 1)}, "", cfg)
    assert ok is True
