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


async def _run(scope, cidrs, cfg, host="192.168.1.5:2333", hosts="", loaded=None):
    """跑一遍中间件。scope 里没写 headers 时补一个可信的 Host —— 真实请求恒有它，
    而 R21 起中间件会校验 Host（挡 DNS 重绑定）。要测 Host 那一层的用例自己传 host=。"""
    cfg(WEB_ALLOW_CIDRS=cidrs, WEB_ALLOW_HOSTS=hosts)
    if loaded is not None:      # 只有要测 fail-closed 那一支的用例才传它
        import config as _C
        _C.loaded_from_db = loaded
    scope.setdefault("headers", [(b"host", host.encode())] if host else [])
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


# ---------------- (R21) Host 头校验：挡 DNS 重绑定 ----------------

@pytest.mark.parametrize("host,ok_", [
    # 用户真的会用来访问本机的名字 —— 一律放行
    ("192.168.1.5:2333", True),
    ("127.0.0.1:2333", True),
    ("10.0.0.9", True),
    ("[::1]:2333", True),
    ("localhost:2333", True),
    ("LOCALHOST", True),
    ("nas.local", True),
    ("autorss.lan:2333", True),
    ("box.internal", True),
    ("fritz.home.arpa", True),
    ("panel.localhost:2333", True),
    # 攻击面：可公开注册的名字，没在白名单里就一律拒
    ("rebind.evil.tld:2333", False),
    ("evil.com", False),
    ("autorss.mydomain.com", False),
    ("", False),                     # 没有 Host 头（HTTP/1.0、部分探针）——无法判定，按不可信处理
])
def test_only_names_that_really_point_at_this_box_are_accepted(host, ok_):
    """浏览器不允许脚本伪造 Host，所以这一条就能根治 DNS 重绑定。

    攻击链：`rebind.evil.tld` 的 A 记录 TTL=1s，先解析到攻击者服务器，
    诱导受害者打开 `http://rebind.evil.tld:2333/`（端口是本项目公开的默认值），
    页面 JS 轮询 fetch('/')；攻击者随后把 DNS 改答 127.0.0.1（或面板所在的局域网 IP）。
    重绑定完成后**浏览器认为该来源与面板同源**：JS 能读页面 HTML、取 NiceGUI 的 client id、
    连 /_nicegui_ws/socket.io/，像用户本人一样驱动这个**无鉴权**面板
    （设置页上渲染着 qB 的地址与账号密码，下载目录也能改）。

    两道现有防线都不生效：SubnetGuard 看到的对端就是受害者本机（127.0.0.1 恒放行）
    或白名单网段内的 IP；WEB_HOST=127.0.0.1 也不设防（请求确实来自本机）。
    """
    from core.netguard import host_allowed
    assert host_allowed(host, "") is ok_, host


def test_a_configured_domain_is_accepted():
    """走自有域名/反向代理的用户把域名填进 WEB_ALLOW_HOSTS 就能进。"""
    from core.netguard import host_allowed
    assert host_allowed("autorss.mydomain.com", "autorss.mydomain.com") is True
    assert host_allowed("autorss.mydomain.com:8080", " autorss.mydomain.com , x.y ") is True
    assert host_allowed("other.mydomain.com", "autorss.mydomain.com") is False


async def test_a_rebound_request_is_refused_by_the_middleware(cfg):
    """端到端：对端是受害者本机（网段那一层必然放行），Host 却是攻击者的域名 → 必须拒。"""
    ok, sent = await _run({"type": "http", "client": ("127.0.0.1", 1)}, "", cfg,
                          host="rebind.evil.tld:2333")
    assert ok is False, "DNS 重绑定的请求被放进来了"
    body = b"".join(m.get("body", b"") for m in sent).decode()
    assert "rebind.evil.tld" in body, "没告诉用户被拒的是哪个 Host"
    assert "本机IP" in body, "没给出『按 IP 进来改配置』这条不会把人锁死的出路"


async def test_the_websocket_channel_is_guarded_too(cfg):
    """只挡 HTTP 没用：重绑定之后攻击页面真正要连的是 /_nicegui_ws/socket.io/。"""
    ok, sent = await _run({"type": "websocket", "client": ("127.0.0.1", 1)}, "", cfg,
                          host="rebind.evil.tld:2333")
    assert ok is False
    assert sent and sent[0]["type"] == "websocket.close"


async def test_ip_access_always_works_so_nobody_gets_locked_out(cfg):
    """按 IP 访问永远放行 —— 域名填错时这是唯一的自救出路，不能被这道校验堵死。"""
    for ip_host in ("127.0.0.1:2333", "192.168.1.5:2333", "[::1]:2333"):
        ok, _ = await _run({"type": "http", "client": ("192.168.1.5", 1)}, "", cfg,
                           host=ip_host, hosts="only.this.domain")
        assert ok is True, ip_host


async def test_the_qb_completion_callback_still_gets_through(cfg):
    """(R22) qB 的『完成时运行外部程序』回调不能被 Host 校验挡住。

    设置页生成的命令是 `curl -s -X POST "http://127.0.0.1:<端口>/api/qb/done?hash=%I"` ——
    curl 会发 `Host: 127.0.0.1:<端口>`，字面 IP，放行。
    但 qB 装在**另一台机器**上的用户会把它改成主机名或域名，那时就要靠 WEB_ALLOW_HOSTS。
    这条钉住前一半（默认配置下开箱即用），后一半由 403 正文里的指引兜住。
    """
    ok, _ = await _run({"type": "http", "client": ("127.0.0.1", 1)}, "", cfg,
                       host="127.0.0.1:2333")
    assert ok is True, "默认生成的回调命令被自己的 Host 校验挡住了"
    # 装在别的机器上、改成域名的情形：填进白名单就通
    ok2, _ = await _run({"type": "http", "client": ("10.0.0.5", 1)}, "", cfg,
                        host="autorss.mylan.example:2333", hosts="autorss.mylan.example")
    assert ok2 is True


@pytest.mark.parametrize("loaded", [True, False])
async def test_host_is_checked_in_both_config_branches(loaded, cfg, monkeypatch):
    """(R22) Host 校验必须在【两支都生效】—— 包括"配置没从库里读出来"那一支。

    第一版把它写进了 `else:`（配置读出来了那一支），于是 `loaded_from_db` 为假时
    这道 DNS 重绑定防线**整个不设防** —— 而那正是重绑定的目标场景：
    重绑定之后浏览器发的请求，对端就是受害者本机 127.0.0.1，必然过得了网段那一关。
    也就是说建表/迁移失败期间（本项目专门为它写了 503 分支和"本机仍进得去改回来"的设计），
    攻击页面照样能读页面 HTML、连 /_nicegui_ws/socket.io/，而设置页上渲染着 qB 的明文密码。

    配置读不出来时 `WEB_ALLOW_HOSTS` 取到的是默认空串，只放行字面 IP / localhost /
    内网后缀 —— 那正是 fail-closed 该有的样子，不需要为这一支放宽。
    """
    import config as _C

    old = _C.loaded_from_db
    try:
        ok, _ = await _run({"type": "http", "client": ("127.0.0.1", 1)}, "", cfg,
                           host="rebind.evil.tld:2333", loaded=loaded)
        assert ok is False, f"loaded_from_db={loaded} 这一支上 Host 校验没生效"
        ok2, _ = await _run({"type": "http", "client": ("127.0.0.1", 1)}, "", cfg,
                            host="127.0.0.1:2333", loaded=loaded)
        assert ok2 is True, f"loaded_from_db={loaded} 这一支把本机也挡了 —— 会锁死自救出口"
    finally:
        _C.loaded_from_db = old
