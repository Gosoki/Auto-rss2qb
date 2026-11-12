"""出站与第三方文本的八处加固（SEC-1 ~ SEC-8）：每条一组"把改动撤掉就会红"的守卫。

钉的是：
· SEC-1 重定向中间跳的包体不许被 httpx 按 Content-Encoding 无上限解压（feed / identity / 取种三条路），
        而最终响应照常解压（钩子判据写宽 → 整个 gzip 源抓不到）。
· SEC-2 ipfilter 的 .dat.gz 解压封顶；截断与多成员不许静默变成"更薄的名单"。
· SEC-3 _variant_tags 对长空白线性，且结果与原写法逐条相同（tests/ 里全部样例 + 生产标题）。
· SEC-4 设置写库失败时，异常串与日志里不带刚填的密钥。
· SEC-5 多成员 gzip 不当成功返回；gzip_ok 时解不开退回 identity 重取一次（并记一行日志），炸弹不重取。
· SEC-6 Mikan 剧场版块的 _BANGUMI_RE 对畸形块线性，且在真实季度页 / 生产标题 / tests/ 样例上与原正则逐条相同。
· SEC-7 _clean_summary 对长空白线性，且与原正则写法逐字等价。
· SEC-8 任何一个解析器给出的坏区间（越界 / 负数 / 起止颠倒 / v4 起 v6 止）都到不了 .p2p。

耗时类用例一律用【相对倍数】判（规模 ×4，耗时不许 ×10），不用单独的硬毫秒预算：
硬预算在满负载的全量跑里会假红（R26 栽过）。语料里的生产数据见文件末尾。
"""
import ast
import asyncio
import base64
import functools
import gzip
import html
import logging
import random
import re
import time
import tracemalloc
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

import config
from core import anime as A
from core import engine
from core import ipfilter as F
from services import enrich
from services import fetch
from sources import mikan as M

_TESTS = Path(__file__).resolve().parent


class _Stream(httpx.AsyncByteStream):
    def __init__(self, data: bytes, chunk: int = 64 * 1024):
        self._d, self._c = data, chunk

    async def __aiter__(self):
        for i in range(0, max(len(self._d), 1), self._c):
            yield self._d[i:i + self._c]


def _best_of_3(fn) -> float:
    best = 9e9
    for _ in range(3):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best


def _test_strings(pred) -> list[str]:
    """tests/ 下全部 .py 里满足 pred 的字符串常量（现有样例），去重后按出现顺序。"""
    out, seen = [], set()
    for p in sorted(_TESTS.glob("*.py")):
        if p.name == Path(__file__).name:
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and pred(node.value):
                if node.value not in seen:
                    seen.add(node.value)
                    out.append(node.value)
    return out


# ══ SEC-1：重定向中间跳 ═════════════════════════════════════════════════

_BOMB_MB = 64
_FEED = b"<rss version='2.0'><channel><item><title>x</title></item></channel></rss>"


@functools.cache
def _bomb() -> bytes:
    return gzip.compress(b"\0" * (_BOMB_MB << 20), 9)


def _hop_handler(hop_body: bytes, final: bytes, final_gzip: bool = False, status: int = 302):
    def h(req):
        if req.url.path == "/hop":
            return httpx.Response(status, headers={"location": "http://93.184.216.34/real",
                                                "content-encoding": "gzip"}, stream=_Stream(hop_body))
        hdr = {"content-encoding": "gzip"} if final_gzip else {}
        return httpx.Response(200, headers=hdr, stream=_Stream(gzip.compress(final) if final_gzip else final))
    return h


async def _peak(coro_factory):
    tracemalloc.start()
    try:
        out = await coro_factory()
        return tracemalloc.get_traced_memory()[1], out
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("gzip_ok", [True, False], ids=["feed(gzip_ok)", "identity"])
async def test_redirect_hop_body_is_not_decompressed_unbounded(gzip_ok, status):
    """feed / 推送 / 心跳 / bgm / 屏蔽列表共用的 config.http_client_kwargs。线上几十 KB 的 3xx 包体，
    没摘头时 httpx 跟随前的 aread() 会在内存里解出 64 MB。httpx 跟随的五种重定向码都要盖住（判据只认 302 就漏了另外四种）。"""
    bomb = _bomb()
    assert len(bomb) < 100 * 1024
    kw = config.http_client_kwargs(30, url="http://93.184.216.34/hop")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_hop_handler(bomb, _FEED, status=status)), **kw) as c:
            return await fetch.get_bytes(c, "http://93.184.216.34/hop", cap=1 << 20, gzip_ok=gzip_ok)

    peak, out = await _peak(go)
    assert out == _FEED
    assert peak < 16 << 20, f"线上 {len(bomb)} 字节，tracemalloc 峰值 {peak >> 20} MB：中间跳被 httpx 解压了"


async def test_torrent_fetch_redirect_hop_is_not_decompressed_unbounded(monkeypatch):
    """取种自己换了 request 钩子；整个覆写 event_hooks 会把 response 钩子一起丢掉。单列一条防"只修了 config"。"""
    bomb = _bomb()
    real = httpx.AsyncClient

    def fake_client(**kw):
        return real(transport=httpx.MockTransport(_hop_handler(bomb, b"d4:infod4:name1:xee")), **kw)
    monkeypatch.setattr(engine.httpx, "AsyncClient", fake_client)

    peak, out = await _peak(lambda: engine.fetch_torrent_bytes("http://93.184.216.34/hop"))
    assert out.startswith(b"d4:info")
    assert peak < 16 << 20, f"取种：tracemalloc 峰值 {peak >> 20} MB"


async def test_final_gzip_response_is_still_decoded_after_a_gzip_redirect_hop():
    """钩子只许碰重定向那一跳：写成"所有响应都摘"时最终响应不再解压，gzip_ok 的 feed 拿到压缩字节 → 整个源抓不到。"""
    kw = config.http_client_kwargs(30, url="http://93.184.216.34/hop")
    h = _hop_handler(gzip.compress(b"<html>302 Found</html>"), _FEED, final_gzip=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(h), **kw) as c:
        assert await fetch.get_bytes(c, "http://93.184.216.34/hop", gzip_ok=True) == _FEED
        assert await fetch.get_text(c, "http://93.184.216.34/real", gzip_ok=True) == _FEED.decode()
        # 不走 fetch 的裸调用（心跳 / 推送那种）：最终响应仍由 httpx 自己解
        assert (await c.get("http://93.184.216.34/hop")).content == _FEED


# ══ SEC-2：.dat.gz 解压封顶，截断 / 多成员照旧报错 ══════════════════════════

_DAT_ROWS = b"".join(b"001.%03d.%03d.000 - 001.%03d.%03d.255 , 000 , x\n" % (i // 256, i % 256, i // 256, i % 256)
                     for i in range(20000))


def test_antip2p_dat_gz_is_decompressed_with_a_cap(monkeypatch):
    """线上上限 64 MB 对应解压后几十 GB。把 _CAP 调到 1 MB，喂 16 MB 的炸弹：峰值必须远低于解压后大小。"""
    monkeypatch.setattr(F, "_CAP", 1 << 20)
    bomb = gzip.compress(b"\0" * (16 << 20), 9)
    tracemalloc.start()
    try:
        with pytest.raises(ValueError):
            F._p_dat(bomb)                # 超限 → 这个源记"解析失败"→ 本次不更新
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 4 << 20, f"线上 {len(bomb)} 字节，解析时峰值 {peak >> 20} MB（上限 1 MB）"


def test_real_shaped_dat_gz_still_parses():
    assert len(F._p_dat(gzip.compress(_DAT_ROWS))[F._P2P]) == 20000
    assert len(F._p_dat(_DAT_ROWS)[F._P2P]) == 20000           # 不压缩的那种照旧


def test_truncated_dat_gz_is_an_error_not_a_thinner_list():
    """decompressobj 对截断的流不报错：不看 eof 就会把 60% 的名单静默换上去（模块头第 4 条）。"""
    full = gzip.compress(_DAT_ROWS)
    with pytest.raises(Exception):
        F._p_dat(full[: len(full) * 6 // 10])
    with pytest.raises(Exception):
        F._p_dat(full[:-8])                                    # 只截掉尾部校验


def test_multi_member_dat_gz_is_never_silently_cut_to_the_first_member():
    two = gzip.compress(_DAT_ROWS[: len(_DAT_ROWS) // 2]) + gzip.compress(_DAT_ROWS[len(_DAT_ROWS) // 2:])
    try:
        got = F._p_dat(two)
    except Exception:
        return                         # 报错 → 这个源记解析失败、本次不更新：可以
    assert len(got[F._P2P]) == 20000, "只解出了第一段，规则悄悄少了一半"


# ══ SEC-3：_variant_tags（决定删不删旧版文件）════════════════════════════

def _old_variant_tags(raw: str) -> set:
    """原写法（不折空白），只用来比对结果。"""
    tags = [a or b for a, b in A._TAG_RE.findall(raw or "")]
    return {t.strip().lower() for t in tags[1:] if not A._EPV_TAG_RE.match(t)}


@pytest.mark.parametrize("head", ["", "05"], ids=["leading", "middle"])
def test_variant_tags_is_linear_in_whitespace(head):
    """空白在开头与夹在中间两种形状都要线性：只 strip() 两端的写法挡得住前者，挡不住 [05<空白×n>x]（~n³）。"""
    def cost(n):
        raw = "[G] 某番 - 05 [v2] [" + head + "　" * n + "x]"
        return _best_of_3(lambda: A._variant_tags(raw))
    small, big = cost(50), cost(200)
    assert big < small * 10 + 0.002, f"空白 ×4，耗时 {small * 1e3:.2f} → {big * 1e3:.2f} ms（原写法 ~n⁴）"


@pytest.mark.parametrize("raw,want", [
    ("[G] 某番 - 05 [1 2] [简 体]", {"1 2", "简 体"}),            # 中间有空白的不是集号/版本号
    ("[G] 某番 [ 05 ] [05　v2] [v2  END] [\t完 ] [ ]", set()),       # 各种空白写法的集号/版本/完结 → 全剔掉
    ("[G] 某番 [05 v2 end x] [1080p]", {"05 v2 end x", "1080p"}),
    ("[G] 某番 [0 5v2] [v 2]", {"0 5v2", "v 2"}),
])
def test_variant_tags_semantics_are_pinned(raw, want):
    """只许把一串空白折成一个，不许整个删掉：删掉会把 [1 2]、[0 5v2] 当成集号剔掉，标签相等的判据随之变松。"""
    assert A._variant_tags(raw) == want


def test_variant_tags_matches_the_old_regex_on_every_known_title():
    """tests/ 里全部带方括号的样例 + 生产标题（各再配全角空格 / 多空白 / 制表三种空白变体），新旧逐条相同。"""
    corpus = _test_strings(lambda s: ("[" in s or "【" in s) and len(s) < 600)
    for t in PROD_TITLES:
        corpus += [t, t.replace(" ", "　"), t.replace(" ", "  "), t.replace(" ", " \t")]
    assert len(corpus) > 500
    diff = [t for t in corpus if A._variant_tags(t) != _old_variant_tags(t)]
    assert not diff, f"{len(diff)} 条结果变了，例如 {diff[:3]}"
    # 语料不是摆设：确实有被剔掉的集号 / 版本标签
    assert sum(1 for t in PROD_TITLES if len(_old_variant_tags(t)) < len(A._TAG_RE.findall(t)) - 1) > 20


def test_one_crafted_row_does_not_stall_the_replace_round(clean_tables, cfg):
    """端到端：第 5 集已下（G 组），库里再躺一条同组名的畸形 v2（skipped）——每轮都会对它算一次标签。
    比的是同一轮在"正常 v2"与"畸形 v2"两种库上的耗时：原写法 300 个全角空格单次就是秒级。"""
    cfg(QB_ENABLED=True, ANIME_REPLACE_VERSIONS=True, ANIME_DOWNLOAD_GRACE_MIN=0)
    from sqlmodel import delete

    from db.models import Anime, AnimeTorrent
    old = datetime.now() - timedelta(hours=5)

    def one_round(v2_title):
        with clean_tables.get_session() as s:
            for m in (AnimeTorrent, Anime):
                s.exec(delete(m))
            a = Anime(title="某番", display_name="某番", season=1, quarter="26C", confirmed=True, rejected=False,
                      total_episodes=12, bangumi_id=1)
            s.add(a)
            s.commit()
            s.refresh(a)
            for i, (raw, st, prog) in enumerate([("[G] 某番 - 05 [1080p]", "sent", 1.0), (v2_title, "skipped", 0.0)], 1):
                s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{i:040x}", source="G", site="nyaa", season=1,
                                   episode=5.0, anime_title="某番", raw_title=raw, status=st, qb_progress=prog,
                                   download_url=f"https://x/{i}.torrent", created_at=old, release_time=old))
            s.commit()
        t0 = time.perf_counter()
        assert asyncio.run(A._deliver_new_versions()) == 0         # 标签不同 → 不替换
        return time.perf_counter() - t0

    base = min(one_round("[G] 某番 - 05 [v2] [x]") for _ in range(2))
    bad = one_round("[G] 某番 - 05 [v2] [" + "　" * 300 + "x]")
    assert bad < base * 10 + 0.05, f"一条畸形标题让这一轮从 {base * 1e3:.0f} ms 变成 {bad * 1e3:.0f} ms"


# ══ SEC-4：设置写库失败时不回显刚填的值 ══════════════════════════════════

@pytest.mark.parametrize("key", ["QB_PASSWORD", "QB_CALLBACK_TOKEN", "PROXY_PASS"])
def test_set_many_write_failure_does_not_echo_values(testdb, caplog, key):
    """用触发器让 setting 的写入真的失败（真实的 UPDATE/INSERT 语句，不用等 busy_timeout）。"""
    from sqlalchemy import text
    from sqlalchemy.exc import StatementError
    secret = "hunter2-" + "PLAIN"
    with testdb.meta_engine.begin() as c:
        for op in ("UPDATE", "INSERT"):
            c.execute(text(f"CREATE TRIGGER r40_boom_{op} BEFORE {op} ON setting "
                           f"BEGIN SELECT RAISE(ABORT, 'r40 写库失败'); END"))
    try:
        with pytest.raises(StatementError) as ei:
            # 两个键：第一个改完变脏，取第二个时 autoflush 就把 UPDATE 发出去了 —— 只包 commit() 盖不住
            config.set_many({key: secret, "ANIME_POLL_LAST": "2026-11-12T00:00:00"})
        with caplog.at_level(logging.ERROR):
            logging.getLogger("nicegui").exception(ei.value)      # NiceGUI 默认处理器就是这么记的
    finally:
        with testdb.meta_engine.begin() as c:
            for op in ("UPDATE", "INSERT"):
                c.execute(text(f"DROP TRIGGER r40_boom_{op}"))
    assert "r40 写库失败" in str(ei.value), "失败原因本身要留着"
    assert secret not in str(ei.value) and secret not in caplog.text, "绑定参数（刚填的密钥）不许进异常串 / 日志"
    assert config.raw(key) != secret, "写库失败时内存不该前进"


def test_other_db_errors_still_show_parameters(testdb):
    """只对设置写入隐藏参数；别的语句报错照旧带参数回显（排障要用）。"""
    from sqlalchemy import text
    from sqlalchemy.exc import StatementError
    with pytest.raises(StatementError) as ei:
        with testdb.meta_engine.begin() as c:
            c.execute(text("INSERT INTO no_such_table_r40 VALUES (:v)"), {"v": "visible-param"})
    assert "visible-param" in str(ei.value)


# ══ SEC-5：多成员 gzip / identity 退路 ═══════════════════════════════════

_A = b"<rss version='2.0'><channel><item><title>1</title></item>"
_B = b"<item><title>2</title></item></channel></rss>"


@pytest.mark.parametrize("chunk", [64 * 1024, 7, 1])
async def test_multi_member_gzip_never_returns_the_first_half(chunk):
    body = gzip.compress(_A) + gzip.compress(_B)

    def h(req):
        if "gzip" in (req.headers.get("accept-encoding") or ""):
            return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=_Stream(body, chunk))
        return httpx.Response(200, stream=_Stream(_A + _B, chunk))
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
        try:
            got = await fetch.get_bytes(c, "http://x/", gzip_ok=True)
        except httpx.DecodingError:
            got = None
    assert got in (None, _A + _B), f"拿到了前半截：{got!r}"


async def test_multi_member_gzip_is_a_decoding_error_without_fallback():
    """不传 gzip_ok 的调用（服务端自己发了多成员 gzip）：报错，不静默截断。"""
    body = gzip.compress(_A) + gzip.compress(_B)

    def h(req):
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=_Stream(body))
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
        with pytest.raises(httpx.DecodingError):
            await fetch.get_bytes(c, "http://x/")


async def test_trailing_padding_after_gzip_is_still_accepted():
    """只对"后面又跟了一段 gzip"报错；尾部填充（零字节 / 垃圾）照旧当正常响应，不能多出抓取失败。"""
    seen = []

    def h(req):
        seen.append(req.headers.get("accept-encoding"))
        return httpx.Response(200, headers={"content-encoding": "gzip"},
                              stream=_Stream(gzip.compress(_A + _B) + b"\0" * 64 + b"junk", 7))
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
        assert await fetch.get_bytes(c, "http://x/", gzip_ok=True) == _A + _B
    assert seen == ["gzip, deflate"]


async def test_gzip_decode_failure_falls_back_to_identity_once(caplog):
    """标了 gzip 实为明文：gzip_ok 时退回 identity 拿到全文，并记一行（否则"每轮取两次"没人知道）。"""
    seen = []

    def h(req):
        ae = req.headers.get("accept-encoding") or ""
        seen.append(ae)
        if "gzip" in ae:
            return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=_Stream(_A + _B))
        return httpx.Response(200, headers={"content-type": "text/xml; charset=utf-8"}, stream=_Stream(_A + _B))
    with caplog.at_level(logging.INFO, logger="autorss"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
            assert await fetch.get_bytes(c, "http://x/feed?token=SECRET", gzip_ok=True) == _A + _B
            assert await fetch.get_text(c, "http://x/feed?token=SECRET", gzip_ok=True) == (_A + _B).decode()
    assert seen == ["gzip, deflate", "identity"] * 2
    msgs = [r.getMessage() for r in caplog.records if "重取" in r.getMessage()]
    assert len(msgs) == 2 and all("http://x" in m and "SECRET" not in m for m in msgs), msgs


async def test_bomb_and_non_gzip_callers_are_not_retried():
    seen = []
    bomb = gzip.compress(b"\0" * (8 << 20))

    def h(req):
        seen.append(req.headers.get("accept-encoding"))
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=_Stream(bomb))
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
        with pytest.raises(fetch.TooLarge):
            await fetch.get_bytes(c, "http://x/", cap=1 << 20, gzip_ok=True)
    assert seen == ["gzip, deflate"], "压缩炸弹不该换个姿势再取一次"
    seen.clear()

    def h2(req):
        seen.append(req.headers.get("accept-encoding"))
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=_Stream(b"not gzip"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(h2)) as c:
        with pytest.raises(httpx.DecodingError):
            await fetch.get_bytes(c, "http://x/")            # 取种 / 推送那条路：本来就是 identity，不重取
    assert seen == ["identity"]


async def test_http_error_is_not_retried():
    seen = []

    def h(req):
        seen.append(req.headers.get("accept-encoding"))
        return httpx.Response(503)
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch.get_text(c, "http://x/", gzip_ok=True)
    assert seen == ["gzip, deflate"]


async def test_fallback_shares_one_total_timeout():
    """压缩路与 identity 重取共用一个总超时：各自计时时一条慢源每轮最多能占 2 × timeout。"""
    async def h(req):
        await asyncio.sleep(0.6)
        if "gzip" in (req.headers.get("accept-encoding") or ""):
            return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=_Stream(_A + _B))
        return httpx.Response(200, stream=_Stream(_A + _B))
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
        with pytest.raises(TimeoutError):
            await fetch.get_bytes(c, "http://x/", gzip_ok=True, timeout=1)


async def test_multi_member_gzip_stops_reading_at_the_second_member():
    """第二段一露头就停：读完整串再报错时，后面每块都被 zlib 整段拼进 unused_data（O(n²)，16 MB 尾巴按 4 KB 一块要几秒，
    全跑在事件循环上）。"""
    pulled = []

    class _Counting(_Stream):
        async def __aiter__(self):
            async for b in super().__aiter__():
                pulled.append(len(b))
                yield b
    body = gzip.compress(_A) + gzip.compress(_B) + b"\0" * (1 << 20)

    def h(req):
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=_Counting(body, 4096))
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
        with pytest.raises(httpx.DecodingError):
            await fetch.get_bytes(c, "http://x/")
    assert len(pulled) <= 2, f"报错前读了 {len(pulled)} 块（共 {len(body) // 4096 + 1} 块）"


# ══ SEC-6：Mikan 剧场版块的 _BANGUMI_RE ═════════════════════════════════

_OLD_BANGUMI_RE = re.compile(r'/Home/Bangumi/(\d+)"[^>]*?title="([^"]*)"')
_MOVIE_HEAD = '<div class="sk-bangumi" data-dayofweek="7"><div id="data-row-7" class="x">剧场版</div>'


def test_unfinished_bangumi_links_scale_linearly():
    def cost(n):
        htm = _MOVIE_HEAD + '/Home/Bangumi/1" ' * n
        assert M._parse_movie_bucket(htm) == []
        return _best_of_3(lambda: M._parse_movie_bucket(htm))
    small, big = cost(1000), cost(4000)
    assert big < max(small * 10, 0.05), f"起点数 ×4，耗时 {small * 1e3:.1f} → {big * 1e3:.1f} ms（原正则平方）"


def test_bangumi_link_within_the_bound_still_matches():
    row = '<a href="/Home/Bangumi/77" %s title="某剧场版">x</a>'
    assert [g[:2] for g in M._parse_movie_bucket(_MOVIE_HEAD + row % ('data-x="' + "a" * 400 + '"'))] == [
        ("77", "某剧场版")]
    real = '<a href="/Home/Bangumi/3736" target="_blank" class="an-text" title="剧场版 某片">某片</a>'
    assert [g[:2] for g in M._parse_movie_bucket(_MOVIE_HEAD + real)] == [("3736", "剧场版 某片")]


def _matches(rx, text):
    return [(m.span(), m.groups()) for m in rx.finditer(text)]


def test_bangumi_re_matches_the_old_regex_on_real_pages_and_samples():
    """真实季度页（85 条）、用生产标题拼的真实形状 <a> 标签、tests/ 里全部 .py 原文：新旧正则的匹配区间与分组逐条相同。"""
    season = gzip.decompress(base64.b64decode("".join(MIKAN_SEASON_GZ_B64))).decode("utf-8")
    assert len(_matches(_OLD_BANGUMI_RE, season)) == 85
    built = "".join(f'<li><a href="/Home/Bangumi/{4000 + i}" target="_blank" class="an-text" '
                    f'title="{html.escape(t)}">{html.escape(t)}</a></li>' for i, t in enumerate(PROD_TITLES))
    corpus = [season, built] + [p.read_text(encoding="utf-8") for p in sorted(_TESTS.glob("*.py"))
                                if p.name != Path(__file__).name]
    corpus += _test_strings(lambda s: "/Home/Bangumi/" in s)
    for text in corpus:
        assert _matches(M._BANGUMI_RE, text) == _matches(_OLD_BANGUMI_RE, text)
    assert [g[:2] for g in M._parse_movie_bucket(season)] == [
        ("4122", "机动战士高达 闪光的哈萨维 喀耳刻的魔女"), ("4124", "祈念守护人"),
        ("4127", "少女与战车 lovelove大作战！ 第1幕"), ("4128", "GROTESQQQUE -グロテスク-"), ("4121", "怪兽8号 鸣海的平日")]


# ══ SEC-7：_clean_summary ═══════════════════════════════════════════════

def _old_clean_summary(s):
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not s:
        return None
    s = re.sub(r"[ \t　]+(?=\n)", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s


def test_clean_summary_scales_linearly_with_whitespace():
    def cost(n):
        s = "简介" + " " * n + "x"
        assert enrich._clean_summary(s) == s
        return _best_of_3(lambda: enrich._clean_summary(s))
    small, big = cost(4000), cost(16000)
    assert big < small * 10 + 0.002, f"空白 ×4，耗时 {small * 1e3:.2f} → {big * 1e3:.2f} ms（原正则平方）"


@pytest.mark.parametrize("raw, want", [
    ("a  \nb\t\n\nc　\n", "a\nb\n\nc"),
    ("a\r\nb \r\n\r\n\r\n\r\nc", "a\nb\n\nc"),
    ("  a  b  ", "a  b"),
    ("a \n \n \n b", "a\n\n b"),
    ("a\xa0 \nb", "a\xa0\nb"),                      # \xa0 不在去除之列
    ("", None), ("   ", None), (None, None),
])
def test_clean_summary_behaviour_is_pinned(raw, want):
    assert enrich._clean_summary(raw) == want


def test_clean_summary_matches_the_old_regex():
    """生产简介（原样 + 每行尾部补各种空白）、生产标题、tests/ 里的多行样例、2 万条随机串：新旧逐字相同。"""
    corpus = [PROD_SUMMARY, None, ""]
    for tail in (" ", "　", "\t", " \t　 ", "\xa0", "\x0b"):
        corpus.append(PROD_SUMMARY.replace("\n", tail + "\n"))
        corpus.append(PROD_SUMMARY.replace("\n", tail + "\r\n") + tail)
    corpus += list(PROD_TITLES) + _test_strings(lambda s: "\n" in s and len(s) < 5000)
    rng = random.Random(40)
    alphabet = [" ", "\t", "　", "\n", "\r", "\xa0", "\x0b", " ", "a", "简"]
    corpus += ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24))) for _ in range(20000)]
    diff = [s for s in corpus if enrich._clean_summary(s) != _old_clean_summary(s)]
    assert not diff, f"{len(diff)} 条结果变了，例如 {diff[:3]!r}"


# ══ SEC-8：坏区间到不了 .p2p ═══════════════════════════════════════════

@pytest.mark.parametrize("nets", [
    {"JP": [(4, 16781312, 16785407), (4, 4294967040, 104294967038)]},     # RIR 个数写坏：终点越过 2^32-1
    {"JP": [(4, 16781312, 16785407), (4, 150994944 + 8192, 150994944)]},  # 起点大于终点（RIR 负个数 / CSV 颠倒）
    {"JP": [(4, 16781312, 16785407), (4, 16781312, 1 << 100)]},           # CSV：v4 起、v6 止
    {"JP": [(4, 16781312, 16785407), (4, -5, 100)]},                      # 负数
    {"JP": [(4, 16781312, 16785407)], "P2P": [(4, 16842751, 16826368)]},  # .dat 颠倒
    {"JP": [(4, 16781312, 16785407), (6, 5, 1)]},                         # v6 颠倒
    {"JP": [(4, 16781312, 16785407), (6, 0, 1 << 128)]},                  # v6 越界
], ids=["v4-over-top", "v4-reversed", "v4-start-v6-end", "negative", "dat-reversed", "v6-reversed", "v6-over-top"])
def test_bad_ranges_never_reach_the_rule_file(nets):
    import ipaddress
    text, n4, n6, per = F.render(nets, ["JP"], 4096)
    rows = [ln for ln in text.splitlines() if not ln.startswith("#")]
    assert rows == ["JP:1.0.16.0-1.0.31.255"] and (n4, n6, per["JP"]) == (1, 0, 1)
    for ln in rows:
        a, b = ln.split(":", 1)[1].split("-")
        assert int(ipaddress.ip_address(a)) <= int(ipaddress.ip_address(b))


def test_boundary_ranges_are_kept():
    """闸是 0 ≤ 起 ≤ 止 ≤ 顶：整段地址空间的两端、单地址区间照常保留（防判据写窄）。"""
    top4, top6 = (1 << 32) - 1, (1 << 128) - 1
    assert F.merge([(4, 0, 0), (4, top4, top4)], 4, 0) == [(0, 0), (top4, top4)]
    assert F.merge([(6, 0, 0), (6, top6, top6)], 6, 0) == [(0, 0), (top6, top6)]


def test_bad_rows_from_every_parser_are_dropped_end_to_end():
    nets = {"JP": [], "P2P": []}
    for got in (F._p_rir(b"apnic|JP|ipv4|1.0.16.0|4096|20110412|allocated\n"
                         b"apnic|JP|ipv4|255.255.255.0|99999999999|20110412|allocated\n"
                         b"apnic|JP|ipv4|9.0.32.0|-5|20110412|allocated\n"
                         b"apnic|JP|ipv4|9.9.32.0|0|20110412|allocated\n", {"JP"}),
                F._p_range_csv(b"8.0.31.255,8.0.16.0,JP\n7.0.0.0,2001:db8::1,JP\n", {"JP"}),
                F._p_dat(b"001.000.255.255 - 001.000.192.000 , 000 , x\n")):
        for g, rs in got.items():
            nets[g].extend(rs)
    text = F.render(nets, ["JP"], 4096)[0]
    assert [ln for ln in text.splitlines() if not ln.startswith("#")] == ["JP:1.0.16.0-1.0.31.255"]


# ══ 语料（生产数据，只读）════════════════════════════════════════════════
# PROD_TITLES：生产实例页面上出现过的真实种子标题（含日志里一条被跳过的）。
# PROD_SUMMARY：生产实例某部番详情页上的 bgm 简介。
# MIKAN_SEASON_GZ_B64：Mikan 一个真实季度浏览页（79.7 KB，含 85 个 /Home/Bangumi/ 链接、5 部剧场版/OVA），gzip 后 base64。

PROD_TITLES = (
    '[7³ACG] 关于我转生变成史莱姆这档事 剧场版 苍海之泪篇/Tensei Shitara Slime Datta Ken: Soukai no Namida-hen 2026 [简繁字幕] BDrip 1080p AV1 OPUS 2.0',
    '[7³ACG] 关于我转生变成史莱姆这档事 剧场版 苍海之泪篇/Tensei Shitara Slime Datta Ken: Soukai no Namida-hen 2026 [简繁字幕] BDrip 1080p x265 OPUS 2.0',
    '[7³ACG] 罗小黑战记 2/The Legend of Hei 2 2025 [简繁字幕] BDrip 1080p AV1 OPUS 2.0',
    '[7³ACG] 罗小黑战记 2/The Legend of Hei 2 2025 [简繁字幕] BDrip 1080p x265 OPUS 2.0',
    '[7³ACG] 藤本树 17-26/Tatsuki Fujimoto 17-26 S01 | 01-08 [简繁字幕] BDrip 1080p AV1 OPUS 2.0',
    '[ANi] Chitosekun wa Ramune Bin no Naka / 彈珠汽水瓶裡的千歲同學 - 18 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]',
    '[ANi] DARK MACHINE THE ANIMATION / 闇機 DARK MACHINE - 05 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]',
    '[ANi] Gattaishin Series / 獸王武神丹帝拜恩 - 06 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]',
    '[ANi] Kamen Rider MYTH / 假面騎士 MY-TH - 06 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]',
    '[ANi] Kamen Rider MYTH / 假面騎士 MY-TH [中文配音] - 05 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]',
    '[ANi] Majikku Naito Reiāsu / 魔法騎士雷阿斯 - 06 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]',
    '[ANi] Sasaki to Pchan / 佐佐木與文鳥小嗶 Season 2 - 06 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]',
    '[ANi] Sora wa Akai Kawa no Hotori / 赤河戀影 - 19 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]',
    '[ANi] Tantei wa Mō Shindeiru / 偵探已經，死了。Season 2 - 06 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]',
    '[ANi] Tensei Shitara Ken Deshita S02 / 轉生就是劍 第二季 - 07 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]',
    '[ANi] 世界最強的魔女，就此開始 - 06 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]',
    '[ANi] 怪兽 8 号 原创短篇动画 鸣海的平日 - 04 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]',
    '[ANi] 新網球王子 U-17 WORLD CUP 世界盃決賽成員決定戰 - 07 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]',
    '[ANi] 重生者的魔法一定要特別 第二季 - 13 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]',
    '[ANi] 重生者的魔法一定要特別 第二季 - 14 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]',
    '[ANi] 重生者的魔法一定要特別 第二季 - 15 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]',
    '[ANi] 重生者的魔法一定要特別 第二季 - 16 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]',
    '[ANi] 重生者的魔法一定要特別 第二季 - 17 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]',
    '[ANi] 重生者的魔法一定要特别 第二季 - 18 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]',
    '[H-Enc] 超时空辉夜姬！ / Chou Kaguya-hime! (BDRip 1080p HEVC FLAC)',
    '[LinRip] ChaO，我代表人类跟人鱼结婚了 ／ ChaO [BDRemux 1080p AVC FLAC][简繁日内封]',
    '[LinRip] ChaO，我代表人类跟人鱼结婚了 ／ ChaO [BDRip 1080p HEVC-10bit FLAC][简繁日内封]',
    '[LinRip] 超时空辉夜姬！ ／ Chou Kaguya-hime [BDRemux 1080p AVC FLAC][简繁内封]',
    '[LinRip] 银魂 吉原炎上 ／ Gintama Yoshiwara in Flames [WEB-DL 1080p H.264 E-AC3][简繁内封]',
    '[LoliHouse] 剧场版 暗杀教室 我们的时光 / Gekijouban Ansatsu Kyoushitsu: Minna no Jikan [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]',
    '[LoliHouse] 怪兽8号 鸣海的平日 / Kaijuu 8-gou Narumi no Heijitsu [01-04 合集][WebRip 1080p HEVC-10bit AAC][简繁内封字幕][Fin]',
    '[LoliHouse] 机动战士高达 闪光的哈萨维 喀耳刻的魔女 / MOBILE SUIT GUNDAM HATHAWAY The Sorcery of Nymph Circe [WebRip 1080p HEVC-10bit AAC][日语内封字幕]',
    '[LoliHouse] 机动战士高达 闪光的哈萨维 喀耳刻的魔女 / MOBILE SUIT GUNDAM HATHAWAY The Sorcery of Nymph Circe [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]',
    '[MingY] 小林家的龙女仆 怕寂寞的龙 / 小林家的龙女仆 怕寂寞的龙 / Kobayashi-san Chi no Maid Dragon: Samishigariya no Ryuu [剧场版][BDRip][简繁日内封]',
    '[NEST] 剧场版 关于我转生变成史莱姆这档事 苍海之泪篇 / 剧场版 転生したらスライムだった件 苍海の涙编 [BDRip 1080p HEVC FLACx2][简繁日内封]',
    '[NEST] 剧场版 暗杀教室 大家的时间 / Assassination Classroom the Movie: Our Time / 剧场版 暗杀教室 みんなの时间 [V2][CR WEB-DL 1080p AVC AAC][简繁日内封]',
    '[NEST] 剧场版 链锯人 蕾塞篇 / 剧场版 チェンソーマン レゼ篇 [MA WEB-DL 1080p AVC DDP5.1 Atmos][简繁日内封]',
    '[NEST] 剧场版 链锯人 蕾塞篇 / 剧场版 チェンソーマン レゼ篇 [MA WEB-DL 2160p HEVC DDP5.1 Atmos][简繁日内封]',
    '[NEST] 哆啦A梦：新・大雄的海底鬼岩城 / 映画ドラえもん 新・のび太の海底鬼岩城 [BDRip 1080p HEVC FLACx2] [简繁日内封]',
    '[Nix-Raws] 世界最强的魔女，就此开始 / 世界最强の魔女、始めました / Sekai Saikyou no Majo Hajimemashita S01E06 [CR WEB-DL 1080p AVC AAC][简繁内封]',
    '[Nix-Raws] 佐佐木与文鸟小哔 Season 2 / 佐佐木与文鸟小哔 Season 2 / 佐々木とピーちゃん シーズン２ / Sasaki to Pii chan S02E06 [CR WEB-DL 1080p AVC AAC][简繁内封]',
    '[Nix-Raws] 侦探已经，死了。Season 2 / 探侦はもう、死んでいる。Season2 / Tantei wa mou Shindeiru S02E06 [CR WEB-DL 1080p AVC AAC][简繁内封]',
    '[Nix-Raws] 兽王武神丹帝拜恩 / 兽王武神丹帝拜恩 / 獣王武神ダンデヴァイン / Juuou Mujin Dandivine S01E06 [CR WEB-DL 1080p AVC AAC][简繁内封]',
    '[Nix-Raws] 弹珠汽水瓶里的千岁同学 / 弹珠汽水瓶里的千岁同学 / 千歳くんはラムネ瓶のなか / Chitose kun wa Ramune Bin no Naka S01E18 [CR WEB-DL 1080p AVC AAC][简繁内封]',
    '[Nix-Raws] 新网球王子 U-17 世界杯 决赛成员选拔赛 / The Prince of Tennis II S03E07 [CR WEB-DL 1080p AVC AAC][简繁内封]',
    '[Nix-Raws] 被放逐的作弊赋予魔术师享受随心所欲的第二人生 / Tsuihou Sareta Cheat Fuyo Majutsushi wa Kimama na S01E06 [CR WEB-DL 1080p AVC AAC][简繁内封]',
    '[Nix-Raws] 被追放的转生重骑士用游戏知识开无双 / Tsuihou Sareta Tensei Juukishi wa Game Chishiki de Musou S01E16 [CR WEB-DL 720p AVC AAC][简繁内封]',
    '[Nix-Raws] 重生者的魔法一定要特别 第二季 / 帰还者の魔法は特别です 第2期 / Kikansha no Mahou wa Tokubetsu desu S02E02 [CR WEB-DL 1080p AVC AAC][简繁内封]',
    '[Nix-Raws] 重生者的魔法一定要特别 第二季 / 帰还者の魔法は特别です 第2期 / Kikansha no Mahou wa Tokubetsu desu S02E03 [CR WEB-DL 1080p AVC AAC][简繁内封]',
    '[Nix-Raws] 重生者的魔法一定要特别 第二季 / 帰还者の魔法は特别です 第2期 / Kikansha no Mahou wa Tokubetsu desu S02E04 [CR WEB-DL 1080p AVC AAC][简繁内封]',
    '[Nix-Raws] 重生者的魔法一定要特别 第二季 / 帰还者の魔法は特别です 第2期 / Kikansha no Mahou wa Tokubetsu desu S02E05 [CR WEB-DL 1080p AVC AAC][简繁内封]',
    '[Nix-Raws] 重生者的魔法一定要特别 第二季 / 帰还者の魔法は特别です 第2期 / Kikansha no Mahou wa Tokubetsu desu S02E06 [CR WEB-DL 1080p AVC AAC][简繁内封]',
    '[Nix-Raws] 魔法骑士雷阿斯 / 魔法骑士雷阿斯 / 魔法骑士レイアース (2026) / Magic Knight Rayearth S01E06 [CR WEB-DL 1080p AVC AAC][简繁内封]',
    '[Prejudice-Studio] 机动战士高达 闪光的哈萨维 喀耳刻的魔女 MOBILE SUIT GUNDAM HATHAWAY The Sorcery of Nymph Circe [Movie][Bilibili WEB-DL 1080P AVC 8bit AAC MP4][简日内嵌]',
    '[Skymoon-Raws] 剧场版「暗杀教室」：我们的时光 / ASSASSINATION CLASSROOM THE MOVIE: OUR TIME [Baha][WEB-DL][CHT][1080p][AVC AAC][MP4]',
    '[Zzz睡不醒][机动战士高达 闪光的哈萨维 喀耳刻的魔女][机动戦士ガンダム 闪光のハサウェイ キルケーの魔女][MOVIE][WEB-DL][1080P][AVC][AC3][简繁日内封][MKV]',
    '[三明治摆烂组&LoliHouse] 世界最强的魔女，就此开始 / 世界最强魔女，始动 / Sekai Saikyou no Majo, Hajimemashita - 05 [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]',
    '[冷番补完字幕组＆白恋字幕组][机动战士高达 闪光的哈萨维 喀耳刻的魔女][机动戦士ガンダム 闪光のハサウェイ キルケーの魔女][MOBILE SUIT GUNDAM HATHAWAY The Sorcery of Nymph Circe][2026][Movie][1080p][内封双语字幕]',
    '[北宇治字幕组] FX战士久留美 / FX战士久留美 / FX Senshi Kurumi-chan [03][WebRip][HEVC_AAC][简繁日内封]',
    '[北宇治字幕组] GROTESQQQUE - 夜露死駆★少女 / YOROSHIKU Girl / 4649GIRL [Movie][WebRip][HEVC_AAC][简日内嵌]',
    '[北宇治字幕组] GROTESQQQUE - 夜露死駆★少女 / YOROSHIKU Girl / 4649GIRL [Movie][WebRip][HEVC_AAC][简繁日内封]',
    '[北宇治字幕组] GROTESQQQUE - 夜露死駆★少女 / YOROSHIKU Girl / 4649GIRL [Movie][WebRip][HEVC_AAC][繁日内嵌]',
    '[喵萌奶茶屋&LoliHouse] 你好，我是受心上人所托来做恋爱药的魔女。 / Horemajo - 06 [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]',
    '[喵萌奶茶屋&LoliHouse] 被放逐的作弊赋予魔法师享受随心所欲的第二人生。~不只是武器，我还能赋予万物「强化点数」，且能照我的意思随时解除，其他人没问题吗？~ / Chii Fuyo - 06 [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]',
    '[桜都字幕组] FX战士久留美 / FX Senshi Kurumi-chan [05][1080P][简体内嵌]',
    '[桜都字幕组] FX战士久留美 / FX Senshi Kurumi-chan [05][1080P][简繁内封]',
    '[桜都字幕组] FX战士久留美 / FX Senshi Kurumi-chan [05][1080P][繁体内嵌]',
    '[桜都字幕组] 冰之城墙 / Koori no Jouheki [20][1080P][简体内嵌]',
    '[桜都字幕组] 冰之城墙 / Koori no Jouheki [20][1080P][简繁内封]',
    '[桜都字幕组] 冰之城墙 / Koori no Jouheki [20][1080P][繁体内嵌]',
    '[桜都字幕组] 学生会也有洞！ / Seitokai ni mo Ana wa Aru！ [06][1080P][简体内嵌]',
    '[桜都字幕组] 学生会也有洞！ / Seitokai ni mo Ana wa Aru！ [06][1080P][简繁内封]',
    '[桜都字幕组] 学生会也有洞！ / Seitokai ni mo Ana wa Aru！ [06][1080P][繁体内嵌]',
    '[樱桃花字幕组]靠死亡游戏混饭吃。44 CLOUDY BEACH Shibou Yuugi de Meshi wo Kuu. 44 Cloudy Beach [1080p][简日内嵌]',
    '[简介原文]',
    '[绿茶字幕组] FX战士久留美 / FX Senshi Kurumi-chan [06][WebRip][1080p][繁日内嵌]',
    '[绿茶字幕组] 某暗部的少女共栖 / Toaru Anbu no Item [03][WebRip][1080p][简繁日内封]',
    '[绿茶字幕组] 药屋少女的呢喃 第三季 / Kusuriya no Hitorigoto [52][WebRip][1080p][简繁日内封]',
    '[绿茶字幕组] 青之箱 第二季 / Ao no Hako S2 [04][WebRip][1080p][简繁日内封]',
    '[绿茶字幕组] 青之箱 第二季 / Ao no Hako S2 [05][WebRip][1080p][简日内嵌]',
    '[绿茶字幕组] 青之箱 第二季 / Ao no Hako S2 [05][WebRip][1080p][繁日内嵌]',
    '[绿茶字幕组] 魔法少女育成计划 restart / Mahou Shoujo Ikusei Keikaku Restart [06][WebRip][1080p][简日内嵌]',
    '[绿茶字幕组] 魔法少女育成计划 restart / Mahou Shoujo Ikusei Keikaku Restart [06][WebRip][1080p][繁日内嵌]',
    '[零始字幕组][风都侦探 假面骑士骸骨的肖像\\Fuuto Tantei Kamen Rider Skull no Shouzou\\FUUTO PI THE PORTRAIT OF KAMEN RIDER SKULL][BDRip][1080P][X264][AAC][双语特效]',
    '[黒ネズミたち] 剧场版「暗杀教室」：我们的时光 / Ansatsu Kyoushitsu Movie: Minna no Jikan (Baha 1920x1080 AVC AAC MP4)',
    '[黒ネズミたち] 少女与战车 更加相亲相爱作战！ / Girls & Panzer: Motto Love Love Sakusen desu! - 01 (ABEMA 1920x1080 AVC AAC MKV)',
    '[黒ネズミたち] 少女与战车 更加相亲相爱作战！ / Girls & Panzer: Motto Love Love Sakusen desu! - 02 (ABEMA 1920x1080 AVC AAC MKV)',
    '[黒ネズミたち] 少女与战车 更加相亲相爱作战！ / Girls & Panzer: Motto Love Love Sakusen desu! - 03 (ABEMA 1920x1080 AVC AAC MKV)',
    '[黒ネズミたち] 少女与战车 更加相亲相爱作战！ / Girls & Panzer: Motto Love Love Sakusen desu! - 04 (ABEMA 1920x1080 AVC AAC MKV)',
    '[黒ネズミたち] 少女与战车 更加相亲相爱作战！ / Girls & Panzer: Motto Love Love Sakusen desu! - 05 (ABEMA 1920x1080 AVC AAC MKV)',
    '[黒ネズミたち] 怪兽 8 号 原创短篇动画 鸣海的平日 / Kaijuu 8-gou: Narumi no Heijitsu - 04 (ABEMA 1920x1080 AVC AAC MKV)',
    '[黒ネズミたち] 怪兽 8 号 原创短篇动画 鸣海的平日 / Kaijuu 8-gou: Narumi no Heijitsu - 04 (Baha 1920x1080 AVC AAC MP4)',
    '[黒ネズミたち] 怪兽 8 号 原创短篇动画 鸣海的平日 / Kaijuu 8-gou: Narumi no Heijitsu - 04 (CR 1920x1080 AVC AAC MKV)',
    '[黒ネズミたち] 怪兽8号 鸣海的日常 / Kaijuu 8-gou: Narumi no Heijitsu - 01 (B-Global 1920x1080 HEVC AAC MKV)',
    '[黒ネズミたち] 怪兽8号 鸣海的日常 / Kaijuu 8-gou: Narumi no Heijitsu - 02 (B-Global 1920x1080 HEVC AAC MKV)',
    '[黒ネズミたち] 怪兽8号 鸣海的日常 / Kaijuu 8-gou: Narumi no Heijitsu - 03 (B-Global 1920x1080 HEVC AAC MKV)',
    '[黒ネズミたち] 怪兽8号 鸣海的日常 / Kaijuu 8-gou: Narumi no Heijitsu - 04 (B-Global 1920x1080 HEVC AAC MKV)',
    '【DBFC字幕组】【龙珠超 比鲁斯】【005】【中日双语字幕】【1080P】【MP4】',
    '【TSDM字幕组】[靠死亡游戏混饭吃。 44:CLOUDY BEACH][MOVIE][MKV][WebRip][AV1 10bit 1080p AAC][简日双语内封][Shibou Yuugi de Meshi o Kuu. 44:Cloudy Beach][死亡游戯で饭を食う。 44:CLOUDY BEACH]',
    '【TSDM字幕组】[靠死亡游戏混饭吃。 44:CLOUDY BEACH][MOVIE][MP4][WebRip][HEVC-10bit 1080p AAC][简日双语内嵌][Shibou Yuugi de Meshi o Kuu. 44:Cloudy Beach][死亡游戯で饭を食う。 44:CLOUDY BEACH]',
    '【喵萌奶茶屋】[有人提到我吗？ / Ore no Koto Nanka Itteta?][04][1080p][简日双语]',
    '【喵萌奶茶屋】[有人提到我吗？ / Ore no Koto Nanka Itteta?][05][1080p][简日双语]',
    '【喵萌奶茶屋】[有人提到我嗎？ / Ore no Koto Nanka Itteta?][04][1080p][繁日雙語]',
    '【喵萌奶茶屋】[有人提到我嗎？ / Ore no Koto Nanka Itteta?][05][1080p][繁日雙語]',
    '【喵萌奶茶屋】★10月新番★[被放逐的作弊賦予魔法師享受隨心所欲的第二人生。~不只是武器，我還能賦予萬物「強化點數」，且能照我的意思隨時解除，其他人沒問題嗎？~ / Chii Fuyo][06][1080p][繁日雙語]',
    '【喵萌奶茶屋】★10月新番★[被放逐的作弊赋予魔法师享受随心所欲的第二人生。~不只是武器，我还能赋予万物「强化点数」，且能照我的意思随时解除，其他人没问题吗？~ / Chii Fuyo][06][1080p][简日双语]',
    '【喵萌奶茶屋】★剧场版★[祈念守护人 / Kusunoki no Bannin][BDRip][1080p][简体]',
    '【喵萌奶茶屋】★剧场版★[祈念守护人 / Kusunoki no Bannin][BDRip][1080p][繁体]',
    '【豌豆字幕组】[新剧场版 银魂 吉原大炎上 / Gintama Yoshiwara in Flames (2026)][简体][804P][WebRip][MP4]',
    '【豌豆字幕组】[新剧场版 银魂 吉原大炎上 / Gintama Yoshiwara in Flames (2026)][繁体][804P][WebRip][MP4]',
)

PROD_SUMMARY = '不断侵蚀世界的异次元空间「影之世界」。\n为了避免最严重的灾害「影之迷宫」导致世界灭亡，\n戴基尔·阿曼德回到13年前的世界，重新过起了校园生活。\n风属性的魔法师罗曼蒂卡·埃鲁、\n迅捷的剑士普兰·舒纳泽，\n以及冰属性的魔剑士阿洁丝特·皇克拉恩。\n戴基尔在赫布里温学院生活期间，与三人成为伙伴，\n并为了攻略突然出现的「影之世界」而四处奔走。\n在短暂的安宁中，「罗曼蒂卡死亡」的恶梦袭向戴基尔。\n出现在他面前的，是「破灭的恶魔」达德涅夫。\n「我知道…那家伙是…罗曼蒂卡的仇人。」\n戴基尔下一场「逆向演算」就此展开！\n\n[简介原文]\n絶えず世界を侵食する異次元空間「影の世界」。\n最悪の災害「影の迷宮」による世界の滅亡を回避するためデジール・アルマンは13年前の世界で学園生活を送っていた。\n ＜風属性の魔法使い＞ロマンティカ・エル、\n＜俊足の検士＞ラム・シュナイザー、\n＜氷属性の魔剣士＞アゼスト・キングスクラウン。\nへブリオン学園での生活を経て3人の仲間を得たデジールは、突如出現した影の世界の攻略のため奔走する。\n束の間訪れた平穏の中、デジールを襲う“ロマンティカの死”の悪夢。現れたのは＜破滅の悪魔＞ダデニュフの姿。\n 「分かってる……あいつは……ロマンティカの仇だ」\n デジールの次なる“逆算（たたかい）”が始まる―?！'

MIKAN_SEASON_GZ_B64 = (
    "H4sIAAAAAAACE+1dW48jSVZ+319h1YoVSFM47pep7kZOZ+bCIpid2V0EQmiUzku1d6rLJds93c0bCJ54RFwEPCDEExKIBx6QEP9m"
    "mYV/QWacyKy0u8rlTFc4K9o1mu7sckVGRqTjxInznXO+870Xq28vR+/m2fr1yzN0Nnqdzy9fr6t/vvreqPzvRZYXK/in+TG9mt/8"
    "OFm/Hs2zl2fVD+fr5Ty5vrzKz/HZbTvT9mZx9eFycT26Wcyv16uyzxHmnynzN0ZohD7DBH5oX6lEn5m/z0bj1nPH9YP3GwvZaywU"
    "w4VWg6Hc1WDoXoPhDC6iGgxXrgbD9hqMlHDR1WAUdjUYvtdgtOawZjCvFg2i/JDhXM2v82T5w2WSzfPrtRnUcvHufHZ5fmk/Oxu9"
    "x+Wjf+Vs9MFe35OXZ+WSrT4h5pOtYa/Wi5vRoihW+RpuqD44TxdXi+XLs+/nLMuzYmNkH98F3W/cl85macq2ZrQ5fCukY5DSF+NS"
    "mF99zwru/NtRepWsyre4+uZ8Vr7wt2/mZ6MsWSfnWfJhUbzL829enrUXp7mneiGmUfVW6FndR/nT1qx/8P33QuD4orpKuLII6YvW"
    "YMv+yuG0h5Jcn88W70fJ9fxNss6zUZFk+W9dn7168faqbnI1X63P59fVRMt252+v6q3oat4a6uomuYbJrJbpy7Nx2d9lvhoHMM8x"
    "QURgNBYpmgmBil//+c3lb8AuxxD6gd3lqn8Wi2U5kpfv8tlNM9efr87z92X/2df2tY1m51fJH3+wb89+WL0ohrjY+vQ6y9+XX6f9"
    "dPV68W71drZKl/NZXt5QJFervJzuuBr/1npovabrt2/OrxdZPlrn79fnaflN58uzV8y+0bvuKN/U/LpYnN3fp21RLvPF25utdttt"
    "y7Hn59Wzz15Vb3KMcfn/6Lu//4/v/vrftwbR3J6MXi/zovwqfnPxJm++CHhD62R5Wa3yr2dXyfU3Z60hmYeM1vP1Vf7yrFxCKhbT"
    "ailJDkuKTyk2S0wQ+JlwfFGtPRmQKaw5Za48EPTi7NXBXbwYJ1vv8J753vFq52m5d5Wrp/nGv96Uu/JzM1PzFVTrZ+eKWi8uL6u3"
    "sl4srtbzG/vpzVWS5m/KFfHybLZYrxdvmrf3v//2T//3t39eLq55Pa4iKQXsfLlajaolN3+1vX5uf6w2lnrn6CFnVOa5TjLhUM7k"
    "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"
    "piiPiDVn0UF9DK9J8ScF6v4/MrKj7383AQA="
)
