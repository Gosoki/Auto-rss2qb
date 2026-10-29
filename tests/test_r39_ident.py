"""(R39) 识别线：I3 日期闸关着时先问 Mikan-hash 桥、I9 单集条目不收第 N 集、
M8 的 enrich 部分（fetch_by_id 这条路也记 bgm 没问成）。

I3 的生产依据：首条种子落在"日期闸关闭"那一支的番共 19 部，名字投票绑错 4 部
（#100→140001 第一季 等）；同样 19 个 hash 问桥，13 个有答案、全部等于最终的正确绑定；
125 部已确认番的首条 hash 测桥：119 一致、0 不一致、6 无答案。
I9 的生产依据：anime#96 绑到 664060『AKANE On My Mind』(1 集·其他)，种子却是第 3~12 集。
"""
import asyncio
import json
from datetime import datetime, timedelta

import httpx
import pytest
from sqlmodel import select

H = "31cfad725b7eb954521f3c83e889e2d349621af8"     # 真库 #100 的首条 hash（只当字符串用）


def _subject(sid=633836, date="2026-08-12", eps=8):
    return json.dumps({"id": sid, "name": f"S{sid}", "name_cn": f"番{sid}", "date": date,
                       "eps": eps, "total_episodes": eps}).encode()


def _stub_resolve_deps(monkeypatch, *, bridge=None, search=None, bridge_sleep=0.0,
                       meta=None):
    """桩掉 _mikan_bridge / _search_one / _retryable / _fetch_cast，返回调用计数。

    bridge：桥返回的 bgm id（None = 桥没答案）；bridge_sleep：桥挂住多久（模拟 Mikan 涓流）。
    search：名字 → bgm id 的映射，命中的放送日一律给 2016-04-03（日期闸关着时它不参与判断）。
    """
    from services import enrich as E
    calls = {"bridge": 0, "search": []}

    async def _bridge(client, ih):
        calls["bridge"] += 1
        if bridge_sleep:
            await asyncio.sleep(bridge_sleep)
        return bridge

    async def _search(client, name, est, date_ref, episode=None):
        calls["search"].append(name)
        hit = (search or {}).get(name)
        return ({"id": hit, "name": name}, datetime(2016, 4, 3)) if hit else None

    async def _retryable(fn):
        return 200, (meta or _subject())

    async def _no_cast(client, bid):
        return None
    monkeypatch.setattr(E, "_mikan_bridge", _bridge)
    monkeypatch.setattr(E, "_search_one", _search)
    monkeypatch.setattr(E, "_retryable", _retryable)
    monkeypatch.setattr(E, "_fetch_cast", _no_cast)
    return calls


# ---------------- I3：日期闸关着时先问桥 ----------------

async def test_date_gate_off_asks_bridge_first(monkeypatch):
    """① 第 79 集（越出 1..30，日期闸关闭）带发布时间：桥给 633836、名字会给 140001 →
    取桥的答案，而且根本不去搜名字。这就是 #100 的形状：名字投票稳稳绑到 2016 年的第一季。"""
    from services import enrich as E
    calls = _stub_resolve_deps(monkeypatch, bridge=633836, search={"Re:Zero": 140001})
    got = await E.resolve(["Re:Zero"], datetime(2026, 8, 20), 79, H)
    assert got is not None and got["bangumi_id"] == 633836
    assert calls["search"] == [], "桥已给答案，不该再走名字投票"
    assert calls["bridge"] == 1


async def test_bridge_miss_falls_back_to_names_and_asks_once(monkeypatch):
    """② 桥没答案 → 照原来走名字投票，而且第②步不再问第二次桥。"""
    from services import enrich as E
    calls = _stub_resolve_deps(monkeypatch, bridge=None, search={"Re:Zero": 140001})
    got = await E.resolve(["Re:Zero"], datetime(2026, 8, 20), 79, H)
    assert got is not None and got["bangumi_id"] == 140001
    assert calls["bridge"] == 1, "同一个 hash 不问两次"


async def test_date_gate_on_keeps_names_first(monkeypatch):
    """③ 第 5 集带发布时间（日期闸开着）→ 名字优先不变，名字命中就不问桥。"""
    from services import enrich as E
    calls = _stub_resolve_deps(monkeypatch, bridge=999, search={"Re:Zero": 140001})
    got = await E.resolve(["Re:Zero"], datetime(2026, 8, 20), 5, H)
    assert got is not None and got["bangumi_id"] == 140001
    assert calls["bridge"] == 0


async def test_movie_episode_none_unchanged(monkeypatch):
    """④ 剧场版传 episode=None：不走桥优先，名字命中就不问桥（行为与改前相同）。"""
    from services import enrich as E
    calls = _stub_resolve_deps(monkeypatch, bridge=999, search={"Re:Zero": 140001})
    got = await E.resolve(["Re:Zero"], None, None, H)
    assert got is not None and got["bangumi_id"] == 140001
    assert calls["bridge"] == 0


async def test_no_pubdate_in_range_episode_asks_bridge_first(monkeypatch):
    """⑤ 没有发布时间、第 3 集 → resolve 同样把 est/date_ref 置空（日期闸关闭）→ 先问桥。"""
    from services import enrich as E
    calls = _stub_resolve_deps(monkeypatch, bridge=777, search={"Re:Zero": 1})
    got = await E.resolve(["Re:Zero"], None, 3, H)
    assert got is not None and got["bangumi_id"] == 777
    assert calls["search"] == []


async def test_hung_bridge_falls_back_to_names_within_budget(monkeypatch):
    """⑥ 桥挂住（Mikan 涓流）→ 在自己的上限内放弃、回落名字投票，整体仍在预算内返回；
    而且【不记 bgm 的账】——这是 Mikan 慢，记了会让 retry_unmatched 的退避阶梯被错误退款。

    上限 = min(ENRICH_TIMEOUT×ENRICH_RETRY_TIMES, 预算/2)。把预算压到 2 秒，上限就是 1 秒。
    """
    from services import enrich as E
    monkeypatch.setattr(E, "_RESOLVE_BUDGET", 2)
    calls = _stub_resolve_deps(monkeypatch, bridge=633836, bridge_sleep=30,
                               search={"Re:Zero": 140001})
    before = E.net_failures()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    got = await E.resolve(["Re:Zero"], datetime(2026, 8, 20), 79, H)
    assert loop.time() - t0 < 2, "必须在整体预算内回落，不能让桥把名字投票的时间吃光"
    assert got is not None and got["bangumi_id"] == 140001, "桥超时应回落到名字投票的结果"
    assert calls["search"] == ["Re:Zero"]
    assert E.net_failures() == before, "Mikan 超时不该记成 bgm 没问成"


async def test_bridge_timeout_is_not_retried_in_step_two(monkeypatch):
    """⑦ 桥超时过、名字也没命中 → 第②步不再问桥（否则 Mikan 涓流时同一次识别被拖两回）。"""
    from services import enrich as E
    monkeypatch.setattr(E, "_RESOLVE_BUDGET", 2)
    calls = _stub_resolve_deps(monkeypatch, bridge=633836, bridge_sleep=30, search={})
    got = await E.resolve(["Re:Zero"], datetime(2026, 8, 20), 79, H)
    assert got is None
    assert calls["bridge"] == 1


async def test_bridge_answer_skips_single_episode_gate(monkeypatch):
    """桥的结果不加 I9 那道单集闸：桥是按 hash 精确查番组，不是按名字猜。"""
    from services import enrich as E
    _stub_resolve_deps(monkeypatch, bridge=500001, search={},
                       meta=_subject(500001, "2026-07-01", 1))
    got = await E.resolve(["某番"], datetime(2026, 8, 20), 40, H)
    assert got is not None and got["bangumi_id"] == 500001


async def test_resolve_anime_reuses_existing_season_via_bridge(monkeypatch, clean_tables):
    """⑧ 端到端（_resolve_anime）：库里已有一部番绑 633836（8 集、2026-08-12 首播），
    来了一条用绝对编号的第 79 集（LoliHouse 型），桥给 633836 → 复用这部番并登记别名。

    这是 I3 如实写进 prod_impact 的后果：绝对编号 78–85 与另一组的季内 12–19 是同一批内容，
    并进同一部番后每集多下一份（集号没有交集，不会撞去重键、不会漏集），用户可以锁源。
    _episode_cannot_belong 在这里为 False（只晚 1.2 周，本季跨度 8+26 周），所以不走 misfit。
    """
    from core import anime as A
    from db.models import Anime, AnimeAlias
    from sources.base import ParsedItem

    with clean_tables.get_session() as s:
        a = Anime(title="Re:ゼロから始める異世界生活 4th season", season=4, quarter="26C",
                  bangumi_id=633836, air_date="2026-08-12", total_episodes=8,
                  confirmed=True, rejected=False)
        s.add(a); s.commit(); s.refresh(a)
        aid = a.id
    calls = _stub_resolve_deps(monkeypatch, bridge=633836, search={"Re:从零开始的异世界生活": 140001})
    item = ParsedItem(info_hash=H, raw_title="x", anime_title="Re:从零开始的异世界生活", season=1,
                      episode=79, quarter="26C", release_time=datetime(2026, 8, 20),
                      download_url="http://x/y.torrent", source="组", site="nyaa",
                      search_names=["Re:从零开始的异世界生活"])
    got = await A._resolve_anime(item)
    assert got == aid, "应复用已绑 633836 的那部番，而不是另建"
    assert calls["search"] == []
    with clean_tables.get_session() as s:
        assert len(s.exec(select(Anime)).all()) == 1
        al = s.exec(select(AnimeAlias).where(
            AnimeAlias.title == A.alias_key("Re:从零开始的异世界生活"),
            AnimeAlias.season == 1)).first()
        assert al is not None and al.anime_id == aid, "别名要登记到被复用的那部番"


# ---------------- I9：单集条目不收第 N 集 ----------------

@pytest.mark.parametrize("subject,ep,hit", [
    ({"eps": 1, "total_episodes": 1}, 6, True),
    ({"eps": 1}, 2, True),                          # 只有 eps 字段、第 2 集：最小命中
    ({"total_episodes": 1, "eps": 0}, 2, True),     # total_episodes 优先，与 _subject_to_info 同口径
    ({"eps": 1, "total_episodes": 1}, 11.5, True),  # 小数集同样是"第 N 集"
    ({"eps": 1, "total_episodes": 1}, 1, False),    # 单集作品的第 1 集就是它本身
    ({"eps": 1, "total_episodes": 1}, 0, False),
    ({"eps": 1, "total_episodes": 1}, -1, False),   # 特别篇
    ({"eps": 1, "total_episodes": 1}, -2, False),   # 未知集
    ({"eps": 1, "total_episodes": 1}, None, False),  # 剧场版 / 不带集号
    ({"eps": 0, "total_episodes": 12}, 6, False),
    ({"eps": 0, "total_episodes": 0}, 6, False),    # bgm 还没填集数：不当单集
    ({}, 6, False),                                 # 缺字段：同上
    ({"eps": 12, "total_episodes": 12}, 40, False),
])
def test_single_ep_gate(subject, ep, hit):
    from services.enrich import _single_ep_cannot_host
    assert _single_ep_cannot_host(subject, ep) is hit


def _search_returns(monkeypatch, data):
    from services import enrich as E

    async def _retryable(fn):
        return 200, json.dumps({"data": data}).encode()
    monkeypatch.setattr(E, "_retryable", _retryable)
    return E


_AKANE = [
    {"id": 576121, "date": "2026-04-04", "eps": 12, "total_episodes": 12,
     "name": "あかね噺", "name_cn": "落语朱音"},           # 正确答案，但被日期闸挡在窗口外
    {"id": 664060, "date": "2026-06-27", "eps": 1, "total_episodes": 1,
     "name": "AKANE On My Mind〜饅頭こわい", "name_cn": ""},  # 第一个过闸的：单集特典
    {"id": 29944, "date": "2004-11-25", "eps": 3, "total_episodes": 3, "name": "x", "name_cn": ""},
]


async def test_akane_single_episode_special_is_refused_for_episode_6(monkeypatch):
    """#96：'Akane-banashi' 第一个过闸的是 664060（1 集·其他）。
    不带集号时行为不变（仍返回它）；第 6 集时这个名字弃权，返回 None。"""
    E = _search_returns(monkeypatch, _AKANE)
    rel = datetime(2026, 8, 4)
    est = rel - timedelta(weeks=5)
    hit = await E._search_one(None, "Akane-banashi", est, rel)
    assert hit is not None and hit[0]["id"] == 664060, "不带集号：旧行为"
    assert await E._search_one(None, "Akane-banashi", est, rel, 6) is None


async def test_single_episode_first_hit_abstains_instead_of_taking_the_next(monkeypatch):
    """第一个过闸的是单集特典、第二个是多集 TV，第 6 集 → 返回 None，【不得】返回 TV。

    continue 去取排名更低的候选等于替用户新猜一次：『… OVA - 02』被解析成 ep=2，
    同系列 TV 恰好也在窗口里的话就会改绑到 TV、与 TV 第 2 集共用去重键 (aid,2)，
    OVA 那份先 sent 就把 TV 第 2 集永久挡掉——静默漏集且不可逆。
    """
    E = _search_returns(monkeypatch, [
        {"id": 700001, "date": "2026-07-01", "eps": 1, "total_episodes": 1,
         "name": "Some Show OVA", "name_cn": ""},
        {"id": 700002, "date": "2026-07-03", "eps": 12, "total_episodes": 12,
         "name": "Some Show", "name_cn": ""},
    ])
    assert await E._search_one(None, "Some Show", None, None, 6) is None


async def test_gate_only_looks_at_the_first_passing_candidate(monkeypatch):
    """反向：第一个过闸的是多集 TV 时照常返回它，后面排着的单集特典不影响。"""
    E = _search_returns(monkeypatch, [
        {"id": 700002, "date": "2026-07-03", "eps": 12, "total_episodes": 12,
         "name": "Some Show", "name_cn": ""},
        {"id": 700001, "date": "2026-07-01", "eps": 1, "total_episodes": 1,
         "name": "Some Show OVA", "name_cn": ""},
    ])
    hit = await E._search_one(None, "Some Show", None, None, 6)
    assert hit is not None and hit[0]["id"] == 700002


async def test_abstention_does_not_count_as_bgm_failure(monkeypatch):
    """弃权是"问到了、答案不能用"，不是"没问成"——不能记 bgm 失败（否则退避阶梯被错误退款）。"""
    E = _search_returns(monkeypatch, _AKANE)
    rel = datetime(2026, 8, 4)
    before = E.net_failures()
    assert await E._search_one(None, "Akane-banashi", rel - timedelta(weeks=5), rel, 6) is None
    assert E.net_failures() == before


# ---------------- M8（enrich 部分）：fetch_by_id 这条路也记"bgm 没问成" ----------------

def _stub_capped(monkeypatch, *, code=200, body=None, exc=None):
    from services import enrich as E

    async def _capped(client, method, url, **kw):
        if exc is not None:
            raise exc
        if url.endswith("/characters"):
            return 200, b"[]"
        return code, (body if body is not None else _subject(501958, "2025-12-19", 1))
    monkeypatch.setattr(E, "_capped", _capped)
    return E


@pytest.mark.parametrize("code,counted", [(429, True), (500, True), (503, True), (404, False)])
async def test_fetch_by_id_counts_rate_limit_and_5xx(monkeypatch, cfg, code, counted):
    """与 _search_one 同口径：429/5xx 是没问成；404（条目被合并/删除、NSFW 条目对未登录请求）
    是"问到了、对方说没有"——算成故障会让剧场版扫描把正常的 404 判成 bgm 挂了、冻住扫描基准。"""
    cfg(ENRICH_RETRY_TIMES=1)
    E = _stub_capped(monkeypatch, code=code, body=b"")
    before = E.net_failures()
    assert await E.fetch_by_id(501958) is None
    assert E.net_failures() == before + (1 if counted else 0)


async def test_fetch_by_id_counts_connection_failure(monkeypatch, cfg):
    cfg(ENRICH_RETRY_TIMES=1)
    E = _stub_capped(monkeypatch, exc=httpx.ConnectError("bgm down"))
    before = E.net_failures()
    assert await E.fetch_by_id(501958) is None
    assert E.net_failures() == before + 1


async def test_fetch_by_id_bad_json_is_not_a_network_failure(monkeypatch, cfg):
    """JSON 坏了是"问到了、给的东西不对"，与 _search_one / _resolve_inner 同口径不记。"""
    cfg(ENRICH_RETRY_TIMES=1)
    E = _stub_capped(monkeypatch, code=200, body=b"<html>not json</html>")
    before = E.net_failures()
    assert await E.fetch_by_id(501958) is None
    assert E.net_failures() == before


async def test_fetch_by_id_success_is_not_counted(monkeypatch, cfg):
    cfg(ENRICH_RETRY_TIMES=1)
    E = _stub_capped(monkeypatch)
    before = E.net_failures()
    info = await E.fetch_by_id(501958)
    assert info is not None and info["bangumi_id"] == 501958
    assert E.net_failures() == before


async def test_fetch_by_id_budget_timeout_counts_once(monkeypatch):
    """整体预算超时也记一笔：这个预算里只有 bgm 一个对端（与 resolve 外层【不记】的理由不同，
    那边罩着 Mikan 桥、说不清是谁慢）。"""
    from services import enrich as E
    monkeypatch.setattr(E, "_RESOLVE_BUDGET", 0.2)

    async def _hang(bgm_id):
        await asyncio.sleep(30)
    monkeypatch.setattr(E, "_fetch_by_id_inner", _hang)
    before = E.net_failures()
    assert await E.fetch_by_id(501958) is None
    assert E.net_failures() == before + 1
