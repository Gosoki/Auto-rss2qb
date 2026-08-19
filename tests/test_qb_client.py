"""qB 客户端的三态返回与会话处理。

三态（True=受理 / False=qB 明确拒 / None=根本没连上）是整条交付链正确性的地基：
把"连不上"揉进 False，qB 掉线时会把整批种子打成 error 要人工补下。
用 httpx.MockTransport 挡住真实网络。
"""
import httpx
import pytest

import config
from services.qbittorrent import QBittorrent


@pytest.fixture
def qb(monkeypatch, cfg):
    """一个每次都"已登录"的客户端，_request 直接走注入的 handler。"""
    cfg(QB_URL="http://qb.test:8080", QB_USERNAME="u", QB_PASSWORD="p")
    client = QBittorrent()

    def use(handler):
        """handler(request) -> httpx.Response"""
        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(transport=transport, base_url=config.QB_URL)

        async def ensure():
            return http
        monkeypatch.setattr(client, "_ensure", ensure)
        return client
    return use


def _ok(text="Ok.", code=200):
    return lambda req: httpx.Response(code, text=text)


# ---------------- add_torrent 三态 ----------------

async def test_add_accepted(qb):
    c = qb(_ok())
    assert await c.add_torrent(b"d1:ae", "/dl", "cat", "tag") is True


async def test_add_rejected_is_false_not_none(qb):
    """qB 明确拒了这一条（坏种子/路径不可写/重复）——重试多少次都一样，调用方该记失败原因。"""
    c = qb(_ok("Fails.", 200))
    assert await c.add_torrent(b"d1:ae", "/dl", "cat", "tag") is False


async def test_add_conflict_is_false(qb):
    """新版 qB 对『该 hash 已在 qB』回 409。仍是 False——由 engine.add_to_qb 据 info_hash 兜底判已交付。"""
    c = qb(_ok("already in", 409))
    assert await c.add_torrent(b"d1:ae", "/dl", "cat", "tag") is False


async def test_unreachable_is_none(qb, monkeypatch):
    """连不上必须是 None：种子本身没问题，调用方该把它留在待下、下轮再来。"""
    c = QBittorrent()

    async def no_login():
        return None
    monkeypatch.setattr(c, "_ensure", no_login)
    assert await c.add_torrent(b"d1:ae", "/dl", "cat", "tag") is None
    assert await c.add_url("magnet:?xt=urn:btih:" + "a" * 40, "/dl", "c", "t") is None
    assert await c.torrents_info(["a" * 40]) is None
    assert await c.reachable() is False


async def test_transport_error_becomes_none_not_exception(qb):
    """传输层错误不能穿透到调用方：契约是"失败返回 None"。
    漏出去会穿透 torrents_info → sync_qb_status，让状态同步永久停摆。"""
    def boom(req):
        raise httpx.ConnectError("refused")
    c = qb(boom)
    assert await c.torrents_info(["a" * 40]) is None


async def test_weird_exception_also_becomes_none(qb):
    """httpx 有一批异常【不】继承 HTTPError（典型是 InvalidURL：query 过长等构造期错误）。
    只接 HTTPError 会让它漏出去。"""
    def boom(req):
        raise httpx.InvalidURL("too long")
    c = qb(boom)
    assert await c.torrents_info(["a" * 40]) is None


async def test_add_url_three_states(qb):
    assert await qb(_ok()).add_url("magnet:?xt=urn:btih:" + "a" * 40, "/dl", "c", "t") is True
    assert await qb(_ok("Fails.", 200)).add_url("magnet:?x", "/dl", "c", "t") is False


# ---------------- torrents_info ----------------

async def test_torrents_info_maps_by_hash(qb):
    payload = '[{"hash":"AAAA","state":"downloading","progress":0.5},' \
              '{"hash":"bbbb","state":"uploading","progress":1.0}]'
    c = qb(_ok(payload))
    info = await c.torrents_info(["aaaa", "bbbb"])
    assert set(info) == {"aaaa", "bbbb"}, "qB 回的 hash 大小写不定，键必须归一成小写"
    assert info["aaaa"]["progress"] == 0.5


async def test_torrents_info_empty_dict_is_not_none(qb):
    """空 dict（qB 在线但这批一个都不在）与 None（连不上）是两个完全不同的信号：
    前者要走落定流程，后者本轮不动。揉在一起会让被删的种子永久滞留 in-flight。"""
    c = qb(_ok("[]"))
    got = await c.torrents_info(["a" * 40])
    assert got == {} and got is not None


async def test_torrents_info_malformed_json_is_none(qb):
    """qB 返回畸形数据时宁可当"这轮没拿到"，也不能抛给上层。"""
    c = qb(_ok("<html>login page</html>"))
    assert await c.torrents_info(["a" * 40]) is None


async def test_torrents_info_empty_input_does_not_hit_the_network(qb):
    called = []

    def h(req):
        called.append(req)
        return httpx.Response(200, text="[]")
    c = qb(h)
    assert await c.torrents_info([]) == {}
    assert called == [], "空列表不该打 qB"


# ---------------- 登录失败的负缓存（R2 P1） ----------------

async def test_login_failure_backs_off(monkeypatch, cfg):
    """(R2) qB 自带失败登录封禁（默认 5 次 → 封该 IP 3600 秒），而两条后台线各每 30s 要一次会话：
    改密/重启后约 75 秒就能把自己封进去，之后改对密码也连不上、设置页还会自动关掉开关。"""
    cfg(QB_URL="http://qb.test:8080", QB_USERNAME="u", QB_PASSWORD="bad")
    c = QBittorrent()
    tries = []

    async def failing_login():
        tries.append(1)
        c._fail_until = __import__("time").monotonic() + 300     # 模拟凭据类失败的长冷却
        return None
    monkeypatch.setattr(c, "_login", failing_login)
    assert await c._ensure() is None
    assert await c._ensure() is None
    assert await c._ensure() is None
    assert len(tries) == 1, "冷却窗口内不该反复去撞 qB 的登录接口"


async def test_saving_settings_clears_the_cooldown(monkeypatch, cfg):
    """用户显式改配置后必须能立刻重试——冷却是为了不撞封禁计数，不是惩罚用户的操作。"""
    cfg(QB_URL="http://qb.test:8080", QB_USERNAME="u", QB_PASSWORD="p")
    c = QBittorrent()
    c._fail_until = __import__("time").monotonic() + 300
    await c.reset_cooldown()
    assert c._fail_until == 0.0
    tries = []

    async def ok_login():
        tries.append(1)
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    monkeypatch.setattr(c, "_login", ok_login)
    assert await c._ensure() is not None and len(tries) == 1


async def test_successful_login_clears_the_cooldown(monkeypatch, cfg):
    cfg(QB_URL="http://qb.test:8080", QB_USERNAME="u", QB_PASSWORD="p")
    c = QBittorrent()

    async def ok_login():
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    monkeypatch.setattr(c, "_login", ok_login)
    c._fail_until = 0.0
    assert await c._ensure() is not None
    assert c._fail_until == 0.0
