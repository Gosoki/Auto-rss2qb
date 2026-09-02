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


# ---------------- (R21) 脱敏必须是结构性的，不能靠"每个调用点记得写" ----------------

def test_every_log_handler_redacts_urls(tmp_path, monkeypatch):
    """日志的三个出口（控制台 / 滚动文件 / /logs 的环形缓冲）都不许出现带密钥的完整 URL。

    泄漏的形状是 `except Exception as e: log.warning("… %s", e)` —— httpx 的
    `HTTPStatusError`/`ConnectError` 的 `str()` 原样带着完整 URL。
    私有站的 .torrent 直链把 **passkey 放在 query 里**（手动下载那条路的输入就是它），
    Mikan『我的番组』订阅地址把 token 放在 query 里。一次 403 就写进三个地方，
    而 /logs 页有『下载完整日志』按钮。

    仓库里早有 `redact`，但 R21 之前生产代码**只有 2 处在用**，
    而全仓有 **77 个** `except … as e` 会把异常写进日志 —— 逐处加必然漏。
    所以判据是"过滤器装上了没有"，不是"某一处写没写"。
    """
    import logging

    import core.logsetup as L

    monkeypatch.setattr(L, "LOG_PATH", tmp_path / "t.log")
    monkeypatch.setattr(L, "_configured", False)
    root = logging.getLogger()
    old_handlers, old_ring = list(root.handlers), list(L.ring.filters)
    try:
        L.setup_logging()
        log = logging.getLogger("autorss")
        log.warning("抓取失败: Client error '403' for url "
                    "'https://priv.example/rss?passkey=SUPERSECRET42'")
        try:
            raise RuntimeError("boom https://priv.example/d.torrent?passkey=SUPERSECRET42")
        except RuntimeError:
            log.exception("带栈的")
        for h in root.handlers:
            h.flush()
        text = (tmp_path / "t.log").read_text(encoding="utf-8")
        ring_text = "\n".join(i["line"] for i in L.ring.snapshot())
        # 【只看 setup_logging 自己装上去的那几个】root 上还挂着 pytest 的日志插件 handler，
        # 它当然没有我们的过滤器 —— 对全部 root.handlers 做断言会永远红。
        handlers_seen = [h for h in root.handlers if h not in old_handlers]
        ring_filters_seen = list(L.ring.filters)
    finally:
        # 【别用 importlib.reload 收尾】它会造出一个**新的** L.ring 对象，
        # 而 'autorss' logger 上挂着的仍是旧的那一个 —— 下一条用例再 setup_logging()
        # 就有了两个 ring，行为随执行顺序漂移（实测：单跑绿、整文件跑红）。
        # 直接把动过的东西还原就够了（_configured 由 monkeypatch 还原）。
        root.handlers[:] = old_handlers
        L.ring.filters[:] = old_ring
        L.ring.buf.clear()
        logging.getLogger("autorss").removeHandler(L.ring)

    assert "SUPERSECRET42" not in text, "滚动日志文件里躺着密钥（/logs 页可整份下载）"
    # 【行为断言不够，还要钉结构】过滤器是**原地改 record** 的，而 'autorss' 的 handler
    # （ring）先跑、root 的（文件/控制台）后跑 —— 于是只要 ring 挂着，文件那半就搭了便车：
    # 把 root handler 上的过滤器整个摘掉，上面那条断言照样绿（实测）。
    # 两处各自都必须挂上，否则任何一处被摘掉都会在别的日志路径上漏。
    assert handlers_seen, "setup_logging 一个 handler 都没装上，用例的前提坏了"
    # 【按类名比，不用 isinstance】本文件末尾那个 importlib.reload(L) 会造出一个**新的**
    # _RedactUrls 类对象，而 handler 上挂的实例来自旧的那个 —— isinstance 于是判否，
    # 用例在整文件跑时莫名其妙地红（单跑却是绿的）。结构守卫按名字比就够了。
    missing = [type(h).__name__ for h in handlers_seen
               if not any(type(f).__name__ == "_RedactUrls" for f in h.filters)]
    assert not missing, (f"这些 handler 没挂脱敏过滤器：{missing}。"
                         "现在靠 ring 先跑、原地改了 record 才没漏 —— 那是巧合不是设计")
    assert any(type(f).__name__ == "_RedactUrls" for f in ring_filters_seen), \
        "环形缓冲（/logs 页实时视图）没挂脱敏过滤器"
    assert "SUPERSECRET42" not in ring_text, "/logs 页的实时视图里躺着密钥"
    assert "priv.example" in text, "脱敏过头了：主机名要留着，否则日志失去诊断价值"
    assert "Traceback" in text, "异常栈整个丢了 —— 脱敏不该吃掉栈"


def test_the_filter_clears_args_not_just_msg():
    """脱敏必须连 `record.args` 一起清掉。

    只改 `record.msg` 而留着 args，handler 会拿 `msg % args` **再格式化一次**，
    原文当场回来 —— 第一版就是这么假绿的。
    """
    import logging

    from core.logsetup import _RedactUrls

    rec = logging.LogRecord("autorss", logging.WARNING, __file__, 1,
                            "抓取失败 %s", ("https://priv.example/x?passkey=SECRET9",), None)
    assert _RedactUrls().filter(rec) is True
    try:
        got = rec.getMessage()
    except TypeError as e:      # msg 改了、args 留着 → `msg % args` 直接炸
        raise AssertionError(f"args 没清掉，record 已经格式化不出来了：{e}") from None
    assert "SECRET9" not in got, "args 没清掉，格式化一次密钥就回来了"


def test_the_non_log_exits_redact_explicitly():
    """过滤器盖不到的两类出口必须自己脱敏：

      · 返回给页面的 `error`（`pages/manual.py` 原样弹成红字 toast）
      · **持久化进库**的 `fail_reason`（详情页会展示它，而且它会一直留在库里）

    ⚠️ **第一版这条守卫是假的**：它对每个文件收集"全文件出现过的调用名"，
    断言 `"redact" in 里面` —— **不区分调用点**。而每个文件里都有一处
    `log.error(..., fetch.redact(e))`，那一处恰好是**最不需要**显式脱敏的地方
    （R21 已经给三个日志 handler 都装了过滤器）。
    把真正写进库的那几处 redact 全删掉、只留 log.error 那一处，守卫照样全绿（实测）。

    现在按【出口】查：每个 `except … as e` 里，凡是把异常喂给 `_fail(reason=…)` /
    `_retry(…)` 或直接 `return` 出去的，实参里必须出现 redact 调用。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    offenders = []
    for mod in ("core/manual.py", "core/anime.py", "core/movies.py"):
        tree = ast.parse((root / mod).read_text(encoding="utf-8"))
        for h in ast.walk(tree):
            if not isinstance(h, ast.ExceptHandler) or not h.name:
                continue
            for n in ast.walk(h):
                # ① 交给持久化出口：_fail(reason=…) / _retry(…)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                        and n.func.id in ("_fail", "_retry"):
                    expr = ast.dump(n)
                # ② 直接 return 出去（manual 那条路的返回值会被页面弹成红字）
                elif isinstance(n, ast.Return) and n.value is not None:
                    expr = ast.dump(n.value)
                else:
                    continue
                uses_exc = f"Name(id='{h.name}'" in expr
                # msg = fetch.redact(e) 之后 return {"error": msg} 也算脱敏过了
                # `msg = fetch.redact(e)` 之后再 return/传出去的，也算脱敏过了
                via_var = any(f"Name(id='{v}'" in expr for v in ("msg", "emsg")) \
                    and "redact" in ast.dump(h)
                if uses_exc and "redact" not in expr and not via_var:
                    offenders.append(f"{mod}:{n.lineno}")
    assert not offenders, (
        "这些出口把未脱敏的异常写进了库或返回给了页面（日志过滤器盖不到它们）：\n  "
        + "\n  ".join(offenders))


async def test_a_failed_manual_download_leaks_nothing(tmp_path, monkeypatch, cfg):
    """端到端：手填一条带 passkey 的私站直链、服务器回 403 —— 返回值里不能有 passkey。

    这条路的输入正是"用户自己打的 .torrent 直链"，而私有站的 passkey 就在 query 里。
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from core import manual

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(403)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cfg(QB_ENABLED=True)
        url = f"http://127.0.0.1:{srv.server_address[1]}/d.torrent?passkey=SUPERSECRET42"
        r = await manual.add_manual(url, None, str(tmp_path))
    finally:
        srv.shutdown()

    assert r["ok"] is False
    assert "SUPERSECRET42" not in str(r), f"返回给页面的错误里带着 passkey：{r}"
    assert "127.0.0.1" in str(r["error"]), "脱敏过头了：主机名要留着，否则用户不知道是哪一步失败"


def test_redaction_does_not_break_the_noise_filter(tmp_path, monkeypatch):
    """(R22) 脱敏过滤器不能把 `record.exc_info` 清掉 —— 那会让断连噪声过滤器整个失效。

    本过滤器挂在三个 handler 上，其中 ring 挂在 'autorss' logger 上，
    而 `logging.callHandlers` 先走本 logger 的 handler 再往 root 传 ——
    **ring 上这一个总是第一个跑**。第一版无条件 `record.exc_info = None`，
    于是 root 那两个 handler 上的 `_SuppressDeletedSlot.filter` 里
    `record.exc_info[1] if record.exc_info else None` 恒为 None：
    它要滤的那一族 NiceGUI 断连噪声（消息体是通用的『按钮操作失败：%s』，
    特征全在**异常**里）从此一条都滤不掉，日志被刷屏并掩盖真错。
    """
    import logging

    import core.logsetup as L

    monkeypatch.setattr(L, "LOG_PATH", tmp_path / "t.log")
    monkeypatch.setattr(L, "_configured", False)
    root = logging.getLogger()
    old_handlers, old_ring = list(root.handlers), list(L.ring.filters)
    try:
        L.setup_logging()
        log = logging.getLogger("autorss")
        try:
            raise RuntimeError("The client this element belongs to has been deleted")
        except RuntimeError:
            log.exception("按钮操作失败：%s", "db-op")
        try:
            raise RuntimeError("真错 https://priv.example/x?passkey=SUPERSECRET42")
        except RuntimeError:
            log.exception("真的出事了")
        for h in root.handlers:
            h.flush()
        text = (tmp_path / "t.log").read_text(encoding="utf-8")
    finally:
        # 【别用 importlib.reload 收尾】它会造出一个**新的** L.ring 对象，
        # 而 'autorss' logger 上挂着的仍是旧的那一个 —— 下一条用例再 setup_logging()
        # 就有了两个 ring，行为随执行顺序漂移（实测：单跑绿、整文件跑红）。
        # 直接把动过的东西还原就够了（_configured 由 monkeypatch 还原）。
        root.handlers[:] = old_handlers
        L.ring.filters[:] = old_ring
        L.ring.buf.clear()
        logging.getLogger("autorss").removeHandler(L.ring)

    assert "has been deleted" not in text, "断连噪声没被滤掉 —— 脱敏把 exc_info 清了"
    assert "真的出事了" in text and "Traceback" in text, "真错连同栈一起被吃掉了"
    assert "SUPERSECRET42" not in text, "栈里的密钥没脱敏"
