"""r40：推送地址的 {msg} 占位符（FLOW-2）与"推送通道坏了 / 后台协程退出"上横幅（FLOW-7）。

钉住的是：
  · 地址里没有 {msg} 时发出的 URL 与以前【逐字相同】（Bark 用户零影响）；
  · 有 {msg} 时正文编码后落在每一处占位符上、路径后不再追加；『发送测试通知』走同一条路；
  · 日志与横幅文案里只有主机名，没有 token；
  · notify.problem()：已保存地址连续失败到熔断门槛才说话，成功一次 / 保存设置即清零，没配地址恒 None，
    测试按钮试别的地址成败都不算账、试已保存地址照常算，熔断期 / 未订阅 / 限流挡下的不算；
    原因读得通（空异常串只留类名）、地址格式坏时不引用 httpx 的异常串（里面可能有口令）；
  · worker.log_task_exit 把异常 / 正常退出的协程记进 health.tasks_problem()，被 cancel 的不记；
  · 两条都挂成全站横幅；协程退出那条不带"没配推送地址"的尾巴（它没有推送事件）。
零网络：拦在 notify 模块里的 get_bytes 上（地址解析那条用 httpx.MockTransport）。
"""
import asyncio
import logging
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import services.notify as N
from core import health, worker

_MAIN = "tests/render_main.py"


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(N, "_fail_streak", 0)
    monkeypatch.setattr(N, "_muted_until", 0.0)
    monkeypatch.setattr(N, "_dropped", 0)
    monkeypatch.setattr(N, "_sent_times", [])
    monkeypatch.setattr(N, "_last_sent", {})
    monkeypatch.setattr(N, "_state_now", {})
    monkeypatch.setattr(N, "_state_times", {})


@pytest.fixture
def urls(monkeypatch, fresh):
    """拦下真正发出去的 URL；box["fail"] 给一个异常工厂就让这一条失败。"""
    box = {"sent": [], "fail": None}

    async def fake_get_bytes(client, url, **kw):
        box["sent"].append(url)
        if box["fail"]:
            raise box["fail"](url)
        return b"ok"
    monkeypatch.setattr(N, "get_bytes", fake_get_bytes)
    return box


# ---------------- FLOW-2：{msg} 占位符 ----------------

_MSG = "测试/a&b"          # 编码后：%E6%B5%8B%E8%AF%95%2Fa%26b


@pytest.mark.parametrize("base,want", [
    ("https://api.day.app/KEY", "https://api.day.app/KEY/💡%E6%B5%8B%E8%AF%95%2Fa%26b"),
    ("https://api.day.app/KEY/", "https://api.day.app/KEY//💡%E6%B5%8B%E8%AF%95%2Fa%26b"),
    ("https://bark.example.com/push/KEY", "https://bark.example.com/push/KEY/💡%E6%B5%8B%E8%AF%95%2Fa%26b"),
])
async def test_without_placeholder_url_is_byte_identical(urls, cfg, base, want):
    """没写 {msg}：与改动前逐字相同（期望值是手写的字面量，不是照着实现再拼一遍）。"""
    cfg(NOTIFY_URL=base)
    assert await N.notify(_MSG) is True
    assert urls["sent"] == [want]


async def test_placeholder_gets_the_encoded_message_in_place(urls, cfg):
    msg = "a/b&c?d#e f%g中"
    cfg(NOTIFY_URL="https://ntfy.sh/topic/publish?message={msg}")
    assert await N.notify(msg) is True
    [url] = urls["sent"]
    assert url == "https://ntfy.sh/topic/publish?message=a%2Fb%26c%3Fd%23e%20f%25g%E4%B8%AD"
    p = urlsplit(url)
    assert p.path == "/topic/publish", "正文又被接到路径后面了"
    assert parse_qs(p.query) == {"message": [msg]}, "正文没完整落在占位符上（有字符没编码，拆出了别的参数）"


async def test_every_placeholder_is_replaced(urls, cfg):
    cfg(NOTIFY_URL="https://x.example/{msg}?title={msg}")
    await N.notify("a b")
    assert urls["sent"] == ["https://x.example/a%20b?title=a%20b"]


async def test_test_button_override_uses_the_same_placeholder_path(urls, cfg):
    cfg(NOTIFY_URL="")
    ok = await N.notify("Hi", url_override="https://api.telegram.org/botT/sendMessage?chat_id=1&text={msg}")
    assert ok is True
    assert urls["sent"] == ["https://api.telegram.org/botT/sendMessage?chat_id=1&text=Hi"]


async def test_other_braces_in_url_and_message_survive(urls, cfg):
    """只认字面 {msg}：地址 / 正文里别的花括号原样保留（str.format 会在 try 之外抛 KeyError，打断交付链）。"""
    cfg(NOTIFY_URL='https://x.example/{topic}/p?j={"a":1}&m={msg}&{0}&{}')
    assert await N.notify("{msg} {0} {topic} }{ %s {msg}") is True
    assert urls["sent"] == [
        'https://x.example/{topic}/p?j={"a":1}&m='
        '%7Bmsg%7D%20%7B0%7D%20%7Btopic%7D%20%7D%7B%20%25s%20%7Bmsg%7D&{0}&{}']


async def test_message_that_is_literally_the_placeholder(urls, cfg):
    """正文本身就是 "{msg}"：编码后不再是占位符，不会被二次替换。"""
    cfg(NOTIFY_URL="https://x.example/a?m={msg}&n={msg}")
    await N.notify("{msg}")
    assert urls["sent"] == ["https://x.example/a?m=%7Bmsg%7D&n=%7Bmsg%7D"]


def _status_error(code):
    def make(url):
        req = httpx.Request("GET", url)
        return httpx.HTTPStatusError(f"Client error '{code}' for url '{url}'", request=req,
                                     response=httpx.Response(code, request=req))
    return make


async def test_no_token_in_logs_or_banner_text(urls, cfg, caplog):
    """Telegram 的 token 在路径里、chat_id 在 query 里：日志与 problem() 里都只能剩主机名。"""
    cfg(NOTIFY_URL="https://api.telegram.org/botSECRETTOK42:AA/sendMessage?chat_id=CHATID9&text={msg}")
    caplog.set_level(logging.WARNING, logger="autorss")
    urls["fail"] = _status_error(401)
    for _ in range(N._FAIL_MUTE_AFTER - 1):
        await N.notify("x")
    urls["fail"] = lambda url: httpx.ConnectError(f"boom {url}")
    await N.notify("x")
    text = caplog.text
    prob = N.problem()
    assert prob and "api.telegram.org" in text
    for leak in ("SECRETTOK42", "CHATID9"):
        assert leak not in text, f"日志里带着 {leak}"
        assert leak not in prob, f"横幅文案里带着 {leak}：{prob}"
    assert "ConnectError" in prob, "最近一次的原因没写进横幅"


async def test_empty_reason_reads_as_a_sentence(urls, cfg):
    """超时（地址是黑洞的典型情形）的异常串是空的：横幅里只留类名，不出现 "TimeoutError: 。"；
    末尾自带句点的原因不叠出 ".。"。"""
    cfg(NOTIFY_URL="https://push.example/KEY")
    urls["fail"] = lambda url: TimeoutError()
    for _ in range(5):
        N.clear_mute()
        await N.notify("x")
    prob = N.problem()
    assert "：TimeoutError。到设置页" in prob, prob
    urls["fail"] = lambda url: httpx.TooManyRedirects("Exceeded maximum allowed redirects.")
    N.clear_mute()
    await N.notify("x")
    assert "：TooManyRedirects: Exceeded maximum allowed redirects。到设置页" in N.problem(), N.problem()


class _MockClient(httpx.AsyncClient):
    """真 httpx 走一遍 URL 解析（InvalidURL 是 httpx 自己抛的，打桩 get_bytes 测不到），传输层一律连不上。"""
    def __init__(self, **kw):
        kw.pop("mounts", None)
        kw.pop("proxy", None)

        def handler(req):
            raise httpx.ConnectError("All connection attempts failed", request=req)
        super().__init__(transport=httpx.MockTransport(handler), **kw)


async def test_malformed_address_reason_has_no_secret(monkeypatch, fresh, cfg, caplog):
    """basic-auth 口令里带未编码的 # 时 httpx 报 "Invalid port: 'hunter2'"——fetch.redact 认不出这种片段，
    日志与横幅都不能用它的异常串。"""
    cfg(NOTIFY_URL="https://alice:hunter2#x@ntfy.lan/t?m={msg}", PROXY_ENABLED=False)
    monkeypatch.setattr(N.httpx, "AsyncClient", _MockClient)
    caplog.set_level(logging.DEBUG)
    for _ in range(5):
        N.clear_mute()
        assert await N.notify("x") is False
    prob = N.problem()
    assert prob and "InvalidURL: 地址格式不对" in prob, prob
    assert "hunter2" not in prob, prob
    assert "hunter2" not in caplog.text


# ---------------- FLOW-7 (a)：notify.problem() ----------------

async def test_problem_needs_five_in_a_row_and_one_success_clears_it(urls, cfg):
    cfg(NOTIFY_URL="https://push.example/KEY")
    urls["fail"] = _status_error(404)
    for _ in range(4):
        assert await N.notify("x") is False
    assert N.problem() is None, "4 次就挂横幅了（偶尔抖几下不该说）"
    await N.notify("x")
    prob = N.problem()
    assert prob and "连续 5 次" in prob and "HTTP 404" in prob and "发送测试通知" in prob, prob
    assert "KEY" not in prob

    N.clear_mute()                       # 『发送测试通知』按钮先调它：不能顺手把横幅也清了
    assert N.problem() is not None
    urls["fail"] = None
    assert await N.notify("x") is True
    assert N.problem() is None, "成功一次没清零"


async def test_problem_is_none_without_an_address(cfg, monkeypatch):
    monkeypatch.setattr(N, "_chan_fails", 99)
    monkeypatch.setattr(N, "_chan_reason", "HTTP 404")
    cfg(NOTIFY_URL="")
    assert N.problem() is None
    cfg(NOTIFY_URL="https://push.example/KEY")
    assert N.problem() is not None


async def test_reset_state_clears_the_problem(urls, cfg):
    cfg(NOTIFY_URL="https://push.example/KEY")
    urls["fail"] = _status_error(500)
    for _ in range(5):
        await N.notify("x")
    assert N.problem()
    N.reset_state()                      # 用户改了 NOTIFY_* 点保存
    assert N.problem() is None


async def test_testing_another_address_does_not_count(urls, cfg):
    """测试按钮试的是输入框里还没保存的地址：它失败不该让已保存的地址挂上"推送坏了"。"""
    cfg(NOTIFY_URL="https://push.example/KEY")
    urls["fail"] = _status_error(404)
    for _ in range(6):
        N.clear_mute()
        await N.notify("x", url_override="https://typo.example/KEY")
    assert N.problem() is None


async def test_trying_another_address_successfully_does_not_clear(urls, cfg):
    """试的是别的（没保存的）地址：它通了不代表已保存的那个好了，横幅不能跟着消失。"""
    cfg(NOTIFY_URL="https://push.example/KEY")
    urls["fail"] = _status_error(404)
    for _ in range(5):
        await N.notify("x")
    assert N.problem()
    urls["fail"] = None
    N.clear_mute()
    assert await N.notify("t", url_override="https://other.example/KEY2") is True
    assert N.problem() is not None


async def test_test_button_on_saved_address_clears_or_counts(urls, cfg):
    """测试按钮试的正是已保存地址：横幅让人去点的就是它，通了要清横幅，没通要继续累计。"""
    saved = "https://push.example/KEY"
    cfg(NOTIFY_URL=saved)
    urls["fail"] = _status_error(404)
    for _ in range(5):
        await N.notify("x")
    assert N.problem()
    N.clear_mute()
    await N.notify("t", url_override=saved)
    assert "连续 6 次" in N.problem()
    urls["fail"] = None
    N.clear_mute()
    assert await N.notify("t", url_override=saved) is True
    assert N.problem() is None, "照横幅说的去点了测试、也发出去了，横幅却还挂着"


async def test_muted_calls_do_not_inflate_the_count(urls, cfg):
    """熔断期直接丢掉的不是"又失败了一次"：没发请求就不记账。"""
    cfg(NOTIFY_URL="https://push.example/KEY", NOTIFY_EVENTS=["failed", "qb_down"], NOTIFY_MAX_PER_HOUR=0)
    urls["fail"] = _status_error(500)
    for _ in range(5):
        await N.notify("x")
    assert N._chan_fails == 5 and time.monotonic() < N._muted_until
    n_sent = len(urls["sent"])
    for _ in range(20):
        assert await N.notify("x") is False
        assert await N.event("failed", "m") is False
        await N.state("qb_down", True, "down")
    assert len(urls["sent"]) == n_sent, "熔断期间还在发请求"
    assert N._chan_fails == 5, f"熔断期间直接返回的也被算成失败：{N._chan_fails}"
    assert "连续 5 次" in N.problem()


async def test_unsubscribed_and_rate_limited_do_not_count(urls, cfg):
    cfg(NOTIFY_URL="https://push.example/KEY", NOTIFY_EVENTS=["failed"], NOTIFY_MAX_PER_HOUR=1)
    for _ in range(10):
        assert await N.event("stalled", "m") is False          # 未订阅
    assert N._chan_fails == 0
    assert await N.event("failed", "m1") is True               # 占掉唯一一枚令牌
    for _ in range(10):
        assert await N.event("failed", "m") is False           # 限流
    assert N._chan_fails == 0 and N.problem() is None


# ---------------- FLOW-7 (b)：后台协程退出 ----------------

async def test_task_exit_is_noted_but_cancel_is_not(caplog):
    async def boom():
        raise RuntimeError("x")

    async def forever():
        await asyncio.sleep(3600)

    async def returns():
        return None

    c = asyncio.create_task(forever(), name="qb_sync")
    await asyncio.sleep(0)
    c.cancel()
    await asyncio.gather(c, return_exceptions=True)
    worker.log_task_exit(c)
    assert health.tasks_problem() is None, "关服时被 cancel 的协程也挂了横幅"

    b = asyncio.create_task(boom(), name="sweep")
    await asyncio.gather(b, return_exceptions=True)
    worker.log_task_exit(b)
    prob = health.tasks_problem()
    assert prob and "后台协程 sweep 已退出" in prob and "重启服务" in prob, prob

    r = asyncio.create_task(returns(), name="backup")
    await r
    worker.log_task_exit(r)
    assert "backup" in health.tasks_problem(), "正常 return 也是退出（它们本该永远跑着）"


# ---------------- FLOW-7 (c)：横幅 ----------------

@pytest.mark.nicegui_main_file(_MAIN)
async def test_both_problems_show_as_site_banners(user, clean_tables, cfg, monkeypatch):
    cfg(QB_ENABLED=False, NOTIFY_URL="https://push.example/KEY")
    await user.open("/?t=overview")
    await user.should_not_see("推送已连续")
    await user.should_not_see("已退出，对应的功能停了")

    monkeypatch.setattr(N, "_chan_fails", 5)
    monkeypatch.setattr(N, "_chan_reason", "HTTP 404")
    health.note_dead("sweep")
    await user.open("/?t=overview")
    await user.should_see("推送已连续 5 次没发出去：HTTP 404")
    await user.should_see("后台协程 sweep 已退出")


@pytest.mark.nicegui_main_file(_MAIN)
async def test_dead_task_banner_has_no_push_tail(user, clean_tables, cfg):
    """没配推送地址时，协程退出那条不带"没配推送地址，这些只在页面上看得见"的尾巴：它没有推送事件，
    配了地址也只在页面上，带尾巴等于暗示"配了推送就能收到"。"""
    cfg(QB_ENABLED=False, NOTIFY_URL="")
    health.note_dead("worker")
    await user.open("/?t=overview")
    await user.should_see("后台协程 worker 已退出，对应的功能停了；重启服务可恢复，原因看运行日志")
    await user.should_not_see("已退出，对应的功能停了；重启服务可恢复，原因看运行日志（没配推送地址")
