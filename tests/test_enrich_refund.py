"""待识别番的退避阶梯与「bgm 不可达退款」。

退避阶梯一共只有 REENRICH_MAX_TRIES 次、总跨度约 15 小时，正好能被 bgm 的一次限流窗口吃光。
但退款判据写错的代价更大：**候选池里常驻的恰恰是 bgm 搜不到的番**，
按"一部都没命中"退款等于把阶梯整个废掉——enrich_tries 永远回到 0、每个节拍重打一遍 bgm。
"""
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from core import anime as A
from db.models import Anime


@pytest.fixture
def unmatched(clean_tables):
    """n 部到点的『待识别』番（bangumi_id 为空、创建时间足够久）。"""
    def _mk(n):
        with clean_tables.get_session() as s:
            for i in range(n):
                s.add(Anime(title=f"未识别{i}", season=1, confirmed=True,
                            bangumi_id=None, enrich_tries=0,
                            created_at=datetime.now() - timedelta(days=30)))
            s.commit()
    return _mk


def _tries(db):
    with db.get_session() as s:
        return sorted(a.enrich_tries for a in s.exec(select(Anime)))


async def test_searched_but_not_found_consumes_a_try(clean_tables, unmatched, monkeypatch, cfg):
    """(R4) 【这是稳态，不是故障】问到了 bgm、只是搜不到 —— 必须照常消耗一次机会，
    否则退避阶梯永远走不到头，后台每个节拍都会重打一遍 bgm。"""
    cfg(REENRICH_MAX_TRIES=5, REENRICH_RETRY_BASE=1)
    unmatched(4)

    async def searched_nothing(aid, **kw):
        return False        # 没碰 net_failures：表示请求发出去了、也收到了回应
    monkeypatch.setattr(A, "enrich_anime", searched_nothing)
    await A.retry_unmatched()
    assert _tries(clean_tables) == [1, 1, 1, 1], "搜不到要照常计次"


async def test_all_network_failures_refund(clean_tables, unmatched, monkeypatch, cfg):
    """每一部都【没问成】（连接层全失败）→ 退回本轮消耗的次数。"""
    cfg(REENRICH_MAX_TRIES=5, REENRICH_RETRY_BASE=1)
    unmatched(4)

    async def cannot_reach(aid, **kw):
        A.enrich._note_bgm_fail()
        return False
    monkeypatch.setattr(A, "enrich_anime", cannot_reach)
    await A.retry_unmatched()
    assert _tries(clean_tables) == [0, 0, 0, 0], "一部都没问成时不该消耗机会"


async def test_partial_network_failure_does_not_refund(clean_tables, unmatched, monkeypatch, cfg):
    """只要有一部真的问到了 bgm，就说明对端是活的——不能退款。"""
    cfg(REENRICH_MAX_TRIES=5, REENRICH_RETRY_BASE=1)
    unmatched(4)
    seen = []

    async def mixed(aid, **kw):
        seen.append(aid)
        if len(seen) > 1:               # 第一部问到了，其余没问成
            A.enrich._note_bgm_fail()
        return False
    monkeypatch.setattr(A, "enrich_anime", mixed)
    await A.retry_unmatched()
    assert _tries(clean_tables) == [1, 1, 1, 1]


async def test_too_few_candidates_never_refunds(clean_tables, unmatched, monkeypatch, cfg):
    """样本太小不足以判断"对端挂了"——两部都没问成也可能只是这两条的地址有问题。"""
    cfg(REENRICH_MAX_TRIES=5, REENRICH_RETRY_BASE=1)
    unmatched(2)

    async def cannot_reach(aid, **kw):
        A.enrich._note_bgm_fail()
        return False
    monkeypatch.setattr(A, "enrich_anime", cannot_reach)
    await A.retry_unmatched()
    assert _tries(clean_tables) == [1, 1]


async def test_ladder_actually_terminates(clean_tables, unmatched, monkeypatch, cfg):
    """跑满 REENRICH_MAX_TRIES 后这批番要退出自动重试池——这正是退款判据写错时会失去的性质。"""
    cfg(REENRICH_MAX_TRIES=3, REENRICH_RETRY_BASE=0)
    unmatched(4)

    async def searched_nothing(aid, **kw):
        return False
    monkeypatch.setattr(A, "enrich_anime", searched_nothing)
    for _ in range(5):
        with clean_tables.get_session() as s:      # 把上次尝试时间推回去，模拟"到点了"
            for a in s.exec(select(Anime)):
                a.last_enrich_at = datetime.now() - timedelta(days=1)
                s.add(a)
            s.commit()
        await A.retry_unmatched()
    assert _tries(clean_tables) == [3, 3, 3, 3], "到达上限后不该再被选中"


# ---------------- 计数器的口径（R5 E-01/02/03） ----------------

def test_counter_only_tracks_bgm(clean_tables):
    """(R5) 【计数器的唯一消费者问的是"bgm 到底可不可达"】
    早先它把 Mikan 桥的失败也记进同一个数：于是"bgm 一切正常、只是 Mikan 打不开"
    会被判成"bgm 整体不可达"，退避阶梯被无限退款、每个节拍重打一遍 bgm。"""
    import asyncio

    import httpx

    from services import enrich as E

    def mikan_dead(req):
        raise httpx.ConnectError("mikan down")
    before = E.net_failures()
    c = httpx.AsyncClient(transport=httpx.MockTransport(mikan_dead))
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        E._mikan_bridge(c, "a" * 40))
    assert E.net_failures() == before, "Mikan 打不开不该记进 bgm 的账"


async def test_rate_limited_bgm_counts_as_unreachable():
    """429 与 5xx 是【没问成】——而它们恰恰是最可能整批发生、最该退款的一类。"""
    import httpx

    from services import enrich as E
    for code in (429, 500, 503):
        before = E.net_failures()
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r, c=code: httpx.Response(c))) as c:
            await E._search_one(c, "某番", None, None)
        assert E.net_failures() == before + 1, f"HTTP {code} 应算没问成"


async def test_normal_miss_does_not_count():
    """200 + 空结果 = 问到了但搜不到，这是稳态，不能算故障。"""
    import httpx

    from services import enrich as E
    before = E.net_failures()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": []}))) as c:
        await E._search_one(c, "某番", None, None)
    assert E.net_failures() == before


# ---------------- 整体时间预算（D-06） ----------------

async def test_resolve_has_an_overall_budget(monkeypatch):
    """识别是【串在采集主链路上】的：process_item 在种子落库之前调它，
    而 poll_once 要等所有条目处理完才轮到 flush 放行下载。
    没有整体封顶时最坏可达 ~22 分钟/番，二十个新番能把采集堵住数小时——
    而 nyaa 的 RSS 是滑动窗口，被堵期间滚出去的条目【永远不会再被采到】。"""
    import asyncio

    from services import enrich as E
    monkeypatch.setattr(E, "_RESOLVE_BUDGET", 1)

    async def never_returns(*a, **kw):
        await asyncio.sleep(30)
    monkeypatch.setattr(E, "_resolve_inner", never_returns)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await E.resolve(["某番"], None, 1, None) is None
    assert loop.time() - t0 < 3, "必须在预算内返回"


async def test_budget_timeout_does_not_guess_who_timed_out(monkeypatch):
    """(R7/N-02) 【超时不能自己记一笔 bgm 失败】——这条用例先前断言的正是相反的行为。

    整体预算罩住的不只是 bgm，还有 Mikan 桥（_mikan_bridge），而它自己的注释明写着
    "不记 bgm 的账：混在一起会让 'Mikan 打不开' 被判成 'bgm 整体不可达'，把退避阶梯无限退款"。
    无条件记的话，Mikan 涓流就能让每一轮都退款：enrich_tries 永远回到 0、每个节拍重打一遍 bgm，
    而 bgm 从头到尾都是 200。出厂默认配置下实测复现过。

    内层已经在每一个真实的 bgm 失败点各记了一笔，外层什么都不做才是准确的。"""
    import asyncio

    from services import enrich as E
    monkeypatch.setattr(E, "_RESOLVE_BUDGET", 1)

    async def never_returns(*a, **kw):
        await asyncio.sleep(30)
    monkeypatch.setattr(E, "_resolve_inner", never_returns)
    before = E.net_failures()
    assert await E.resolve(["某番"], None, 1, None) is None
    assert E.net_failures() == before, "超时不代表 bgm 没问成——内层才知道真相"


async def test_candidate_names_are_capped(monkeypatch):
    """候选名截到 3 个：每个名字都是一次 bgm 往返，而第 4 个之后的命中率极低。"""
    from services import enrich as E
    seen = {}

    async def capture(names, est, date_ref, info_hash):
        seen["n"] = list(names)
        return None
    monkeypatch.setattr(E, "_resolve_inner", capture)
    await E.resolve([f"名{i}" for i in range(10)], None, 1, None)
    assert len(seen["n"]) == E._MAX_CANDIDATE_NAMES == 3


# ---------------- (R14) 平票不绑，退『待识别』 ----------------

def _stub_search(mapping):
    """把 name → (subject_id, 放送日) 的映射做成 _search_one 的桩。"""
    from datetime import datetime as _dt

    async def _one(client, name, est, date_ref):
        hit = mapping.get(name)
        if hit is None:
            return None
        sid, day = hit
        return {"id": sid, "name": name}, _dt.fromisoformat(day)
    return _one


async def _resolve_with(monkeypatch, mapping, names, *, episode=1174, release_time=None):
    from services import enrich as E

    class _Resp:
        status_code = 200

        def json(self):
            return {"id": 975, "name": "ONE PIECE", "name_cn": "航海王",
                    "date": "1999-10-20", "eps": 1191}

    async def _retryable(fn):
        return _Resp()

    async def _no_cast(client, bid):
        return None

    async def _no_bridge(client, ih):
        return None

    monkeypatch.setattr(E, "_search_one", _stub_search(mapping))
    monkeypatch.setattr(E, "_retryable", _retryable)
    monkeypatch.setattr(E, "_fetch_cast", _no_cast)
    monkeypatch.setattr(E, "_mikan_bridge", _no_bridge)
    return await E.resolve(names, release_time=release_time, episode=episode)


async def test_tied_votes_do_not_bind(monkeypatch):
    """几个候选名各命中一部不同的番、又没有放送日可判 → 【什么都不绑】。

    修前是 `sorted(votes, key=...)[0]`，而 sorted 是稳定排序：全平局时"第一个"就是
    候选名的【书写顺序】——等于拿种子标题里哪个名字写在前面来决定绑哪部番。
    真库 anime#99 就是这样绑成了「海贼王女」(2021)，随后被判超期忽略、一集都不下。
    """
    got = await _resolve_with(monkeypatch, {
        "海贼王": (311310, "2021-10-02"),          # 2-gram「海贼」误命中「海贼王女」
        "ワンピース": (90795, "2005-12-18"),
        "One Piece S01E1174": (975, "1999-10-20"),  # 正确答案，同样只有 1 票
    }, ["海贼王", "ワンピース", "One Piece S01E1174"])
    assert got is None, f"三方平票必须留待人工，实际绑成了 {got}"


async def test_unanimous_names_still_bind(monkeypatch):
    """多个名字命中【同一部】不是平票——它恰恰是最强的证据，必须照常绑。"""
    got = await _resolve_with(monkeypatch, {
        "航海王": (975, "1999-10-20"),
        "ONE PIECE": (975, "1999-10-20"),
    }, ["航海王", "ONE PIECE"])
    assert got is not None and got["bangumi_id"] == 975


async def test_single_name_hit_still_binds(monkeypatch):
    """只有一个名字命中也不是平票（并列者只有它自己），照常绑——
    否则 feed 缺 pubDate 的番会集体退回『待识别』。"""
    got = await _resolve_with(monkeypatch, {"航海王": (975, "1999-10-20")},
                              ["航海王", "查不到的名字"])
    assert got is not None and got["bangumi_id"] == 975


async def test_more_votes_wins_over_tie(monkeypatch):
    """票数不同就不是平票：2 票的胜过 1 票的。"""
    got = await _resolve_with(monkeypatch, {
        "航海王": (975, "1999-10-20"),
        "ONE PIECE": (975, "1999-10-20"),
        "海贼王": (311310, "2021-10-02"),
    }, ["航海王", "ONE PIECE", "海贼王"])
    assert got is not None and got["bangumi_id"] == 975


async def test_date_proximity_breaks_the_tie(monkeypatch):
    """同票但放送日贴合度不同 → 仍按日期判，不算平票。

    这条守住的是"平票判据别扩得太宽"：季番（集号 1..30 且有发布时间）有日期基准，
    gap 各不相同，绝不该被误判成平票而集体退回『待识别』。
    """
    from datetime import datetime
    got = await _resolve_with(monkeypatch, {
        "某番": (975, "1999-10-20"),
        "Some Show": (311310, "2021-10-02"),
    }, ["某番", "Some Show"], episode=5, release_time=datetime(2021, 11, 1))
    assert got is not None, "有日期基准时不该判成平票"
