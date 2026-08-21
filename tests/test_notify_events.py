"""通知的事件层：订阅过滤 / 边沿触发 / 冷却 / 限流。

这一层存在的理由是"qB 掉线时每 30 秒一条推送"——那不是通知，是骚扰，
而且会把真正重要的那条淹掉。所以每一条抑制规则都要有用例。
"""
import pytest

import config
import services.notify as N
from sqlmodel import select


@pytest.fixture
def sent(monkeypatch, cfg):
    box = []

    async def fake(msg):
        box.append(msg)
        return True          # 【必须返回 True】真实的 notify() 返回"送出去了没有"，
                             # 返回 None 会被当成发送失败——那样测的就是另一套行为了。
    monkeypatch.setattr(N, "notify", fake)
    cfg(NOTIFY_URL="http://push.example/key", NOTIFY_MAX_PER_HOUR=0,
        NOTIFY_EVENTS=list(N.EVENTS))
    N.reset_state()
    yield box
    N.reset_state()


# ---------------- 订阅过滤 ----------------

async def test_unsubscribed_event_is_dropped(sent, cfg):
    cfg(NOTIFY_EVENTS=["delivered"])
    await N.event("failed", "x")
    assert sent == []
    await N.event("delivered", "y")
    assert len(sent) == 1


async def test_empty_list_means_all_off(sent, cfg):
    """【留空＝全关】，不是全开。本项目别处"留空=不限"的含义恰好相反，最容易想当然。"""
    cfg(NOTIFY_EVENTS=[])
    for k in N.EVENTS:
        await N.event(k, "x")
    assert sent == []


async def test_no_url_means_silent(sent, cfg):
    cfg(NOTIFY_URL="")
    await N.event("failed", "x")
    assert sent == []


async def test_icon_is_prefixed(sent):
    await N.event("finished", "某番全 12 集已下齐")
    assert sent[0].startswith(N.EVENTS["finished"][1])


# ---------------- 状态型：边沿触发 ----------------

async def test_state_fires_only_on_flip(sent):
    """电平触发的话，qB 关机一夜就是几十条一模一样的推送（那条预检每轮都跑）。"""
    for _ in range(5):
        await N.state("qb_down", True, "掉线", "恢复")
    assert len(sent) == 1 and "掉线" in sent[0]
    for _ in range(5):
        await N.state("qb_down", False, "掉线", "恢复")
    assert len(sent) == 2 and "恢复" in sent[1]


async def test_first_observation_of_bad_counts_as_a_flip(sent):
    """启动时就已经坏着，也必须被告知一次。"""
    await N.state("db_down", True, "停摆", "恢复")
    assert len(sent) == 1


async def test_first_observation_of_good_is_silent(sent):
    """一切正常时不该有通知。"""
    await N.state("db_down", False, "停摆", "恢复")
    assert sent == []


async def test_state_events_are_independent(sent):
    await N.state("qb_down", True, "qB 掉线", "")
    await N.state("db_down", True, "库停摆", "")
    assert len(sent) == 2


# ---------------- 冷却 ----------------

async def test_cooldown_suppresses_same_key(sent):
    for _ in range(4):
        await N.event("failed", "3 条失败", key="3", cooldown=3600)
    assert len(sent) == 1


async def test_different_key_passes(sent):
    """条数变了就该再说一次——"积压从 3 涨到 30"是新信息。"""
    await N.event("failed", "3 条失败", key="3", cooldown=3600)
    await N.event("failed", "30 条失败", key="30", cooldown=3600)
    assert len(sent) == 2


# ---------------- 限流 ----------------

async def test_rate_limit_drops_and_reports(sent, cfg):
    cfg(NOTIFY_MAX_PER_HOUR=2)
    for i in range(5):
        await N.event("delivered", f"第{i}集")
    assert len(sent) == 2, "超过上限的要丢掉"
    cfg(NOTIFY_MAX_PER_HOUR=0)          # 放开后，下一条要带上"你有几条没收到"
    await N.event("delivered", "又一集")
    assert "被限流丢弃" in sent[-1]


async def test_rate_limit_off_by_zero(sent, cfg):
    cfg(NOTIFY_MAX_PER_HOUR=0)
    for i in range(30):
        await N.event("delivered", f"第{i}集")
    assert len(sent) == 30


async def test_reset_state_clears_memory(sent):
    """用户改完通知设置就该立刻看到效果，而不是等冷却过期或状态再翻转一次。"""
    await N.state("qb_down", True, "掉线", "恢复")
    N.reset_state()
    await N.state("qb_down", True, "掉线", "恢复")
    assert len(sent) == 2


def test_event_keys_are_stable():
    """键是存进 settings 表的字面量——改名等于改用户配置。这条用例挡住"顺手改个更好听的名字"。

    【加键是允许的，改/删键不允许】新键靠 config._merge_new_notify_events 并进老库的订阅里
    （见同文件下面那几条用例）；而改名会让老库里那条订阅指向一个不存在的事件——
    表现是"这类通知突然一条都不来了"，且设置页看上去一切正常。加键时把它列进来即可。
    """
    assert set(N.EVENTS) == {"delivered", "movie", "failed", "stalled", "finished",
                             "idle", "qb_down", "db_down", "backlog"}


def test_default_subscription_preserves_old_behaviour():
    """老库升级时 load_from_db 只补缺键，会写入这个默认值——里面必须含 delivered，
    否则用户升级后会发现"交付通知没了"。"""
    assert "delivered" in config._SPEC["NOTIFY_EVENTS"][1]


# ---------------- 记账必须晚于发送（R4 P0） ----------------

async def test_state_events_bypass_the_rate_limit(sent, cfg):
    """(R4) 状态型每次翻转最多两条，频次天然有上界，不该和"每交付一集一条"抢同一个桶。
    共用一个桶时桶满会把边沿推迟到下一轮，而 qb_down 的下一轮可能是 20 分钟后——
    那正是最需要立刻知道的时候。"""
    cfg(NOTIFY_MAX_PER_HOUR=1)
    await N.event("delivered", "占掉额度")
    assert len(sent) == 1
    await N.state("qb_down", True, "qB 掉线", "qB 恢复")
    assert len(sent) == 2 and "掉线" in sent[1], "状态型不该被 delivered 挤掉"


async def test_dropped_state_event_can_be_resent(sent, cfg, monkeypatch):
    """【全项目唯一的带外故障信号】一条没发出去的 qb_down 如果照样把状态记成"坏"，
    后续每一轮都判为"没翻转"而静默返回——用户最后只会收到一条孤零零的"已恢复"。"""
    boom = {"n": 0}

    async def flaky(msg):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("模拟发送失败")
        sent.append(msg)
    monkeypatch.setattr(N, "notify", flaky)
    with pytest.raises(RuntimeError):
        await N.state("qb_down", True, "qB 掉线", "qB 恢复")
    assert sent == []
    await N.state("qb_down", True, "qB 掉线", "qB 恢复")     # 下一轮必须还能发出去
    assert len(sent) == 1 and "掉线" in sent[0]


async def test_unsubscribed_state_does_not_latch(sent, cfg):
    """没订阅时状态也不该记住——用户中途把这个事件勾上，下一轮就该收到当前的坏消息。"""
    cfg(NOTIFY_EVENTS=["delivered"])
    await N.state("db_down", True, "停摆", "恢复")
    assert sent == []
    cfg(NOTIFY_EVENTS=["delivered", "db_down"])
    await N.state("db_down", True, "停摆", "恢复")
    assert len(sent) == 1


async def test_dropped_event_does_not_burn_the_cooldown(sent, cfg):
    """(R4) 一条被限流丢掉的通知照样把 (kind,key) 的冷却烧掉 6 小时的话，
    那条告警在窗口内【永远不会重发】。"""
    cfg(NOTIFY_MAX_PER_HOUR=1)
    await N.event("delivered", "占掉额度")
    assert await N.event("failed", "3 条失败", key="3", cooldown=3600) is False
    cfg(NOTIFY_MAX_PER_HOUR=0)
    assert await N.event("failed", "3 条失败", key="3", cooldown=3600) is True


async def test_event_returns_whether_it_was_sent(sent, cfg):
    """返回值是契约的一部分：调用方据此决定要不要落"已通知"的标记。"""
    assert await N.event("delivered", "x") is True
    cfg(NOTIFY_EVENTS=[])
    assert await N.event("delivered", "y") is False


async def test_state_recovery_still_fires_after_a_sent_bad(sent):
    await N.state("qb_down", True, "掉线", "恢复")
    await N.state("qb_down", False, "掉线", "恢复")
    assert [s[1:] for s in sent] == ["掉线", "恢复"]


# ---------------- list 型配置的前向兼容（R4 Q5） ----------------

def test_new_events_merge_into_existing_choice(testdb, monkeypatch):
    """(R4) 【前向兼容陷阱】load_from_db 只补【缺失的键】。NOTIFY_EVENTS 一旦存过一次，
    以后往 EVENTS 里加的任何新事件，对所有老库都是【静默默认关】——用户升级后得自己想到
    去勾一下，而他根本不知道多了这么个事件。那会挡住后续所有"加一类提醒"的改进。"""
    from sqlmodel import select

    from db.models import Setting
    # 模拟：用户存过一次选择，快照里也只有当时那几个事件
    config.set_many({"NOTIFY_EVENTS": "delivered,failed"})
    with testdb.get_meta_session() as s:
        row = s.get(Setting, config._KNOWN_EVENTS_KEY)
        if row is None:
            row = Setting(key=config._KNOWN_EVENTS_KEY, value="")
        row.value = "delivered,failed"          # 快照停在"当时只有这两个事件"
        s.add(row)
        s.commit()
    config.load_from_db()
    now = set(config.NOTIFY_EVENTS)
    assert "delivered" in now and "failed" in now
    assert set(N.EVENTS) - now == set(), "本版本新增的事件应当被并进来"


def test_explicitly_unchecked_events_stay_off(testdb):
    """但用户【显式取消过】的不能被重新打开——那些键早就在快照里了，不属于"新增"。"""
    config.load_from_db()                       # 先让快照与当前 EVENTS 对齐
    config.set_many({"NOTIFY_EVENTS": "delivered"})
    config.load_from_db()
    assert set(config.NOTIFY_EVENTS) == {"delivered"}, "用户取消掉的不该被并回来"


async def test_movie_delivery_uses_its_own_event_key(clean_tables, monkeypatch, cfg):
    """剧场版交付走 'movie' 事件键，不再复用 'delivered'（D-18）。

    【必须跑真实的交付函数】只断言 EVENTS 表里有这个键、或者自己 notify_event("movie", ...)
    一下，都是在测我自己传进去的东西：把 core/movies.py 那一行改回 delivered，
    全套用例照样全绿——实测过。这里把 download_movie_torrent 的外部依赖全部替身掉，
    只留通知那一步，直接看它用的是哪个键。
    """
    from datetime import datetime

    from core import movies as M
    from db.models import Movie, MovieTorrent

    with clean_tables.get_session() as s:
        m = Movie(title="某剧场版", quarter="26A", jp_name="某剧场版")
        s.add(m)
        s.commit()
        s.add(MovieTorrent(movie_id=m.id, info_hash="a" * 40, raw_title="[组] 某剧场版",
                           download_url="http://x/a.torrent", status="pending",
                           created_at=datetime.now()))
        s.commit()
        mt_id = s.exec(select(MovieTorrent)).one().id

    calls = []
    monkeypatch.setattr(M, "notify_event",
                        lambda kind, msg, **kw: (calls.append(kind), _ok())[1])
    monkeypatch.setattr(M.engine, "fetch_torrent_bytes", lambda url: _ret(b"d1:xe"))
    monkeypatch.setattr(M.engine, "add_to_qb", lambda *a, **k: _ret(True))
    monkeypatch.setitem(config._v, "DOWN_PATH", "/data")
    monkeypatch.setitem(config._v, "QB_ENABLED", True)

    assert await M.download_movie_torrent(mt_id) is True
    assert calls == ["movie"], f"剧场版交付用的事件键是 {calls}，不是 ['movie']"


async def _ok():
    return True


async def _ret(v):
    return v


def test_first_sight_of_the_snapshot_only_records_it(testdb, monkeypatch):
    """快照键【第一次出现】时只记账、不动用户的选择——这条分支此前零覆盖。

    它的正确性决定了「新增一个事件键会不会对老库静默默认关」。相邻那条分支
    （快照已存在 → 并入新键）有用例，这条没有；而两条的后果是相反的。
    """
    from sqlmodel import select

    from db import get_meta_session
    from db.models import Setting

    with get_meta_session() as s:
        for k in ("NOTIFY_EVENTS", config._KNOWN_EVENTS_KEY):
            row = s.exec(select(Setting).where(Setting.key == k)).first()
            if row is not None:
                s.delete(row)
        s.add(Setting(key="NOTIFY_EVENTS", value="failed"))   # 用户只留了一个事件
        s.commit()
    monkeypatch.setitem(config._v, "NOTIFY_EVENTS", ["failed"])

    config._merge_new_notify_events({})

    with get_meta_session() as s:
        snap = s.get(Setting, config._KNOWN_EVENTS_KEY)
        chosen = s.exec(select(Setting).where(Setting.key == "NOTIFY_EVENTS")).first()
    assert snap is not None and "movie" in snap.value, "第一次见就该把当前全集记成快照"
    assert chosen.value == "failed", "第一次见快照键时不该动用户已有的选择"
    assert config._v["NOTIFY_EVENTS"] == ["failed"]


async def test_unsubscribed_state_event_does_not_burn_the_bucket(sent, cfg):
    """没订阅的状态型事件不该扣令牌——它每轮都判成翻转，能把别的事件饿死一整小时（F11）。

    enabled() 原本在 event() 内部才判，而 `_state_now` 只在发送成功时才落记忆：
    于是未订阅的 kind 每一轮都判翻转、每一轮扣一枚。db_watch 每 30 秒一轮 = 120 次/小时，
    足够把额度打光。
    """
    cfg(NOTIFY_EVENTS=["db_down"])                 # 只订阅 db_down
    for _ in range(N._STATE_CAP_PER_HOUR + 4):     # 未订阅的 qb_down 反复翻转
        await N.state("qb_down", True, "坏了")
        await N.state("qb_down", False, "好了", "恢复")
    assert sent == []
    # 【直接断言桶】而不是断言"别的 kind 还发得出去"——桶已经按 kind 分账了，
    # 那样断言会被分账那一半掩蔽掉，测不到"扣费时机"这件事本身。
    assert not N._state_times.get("qb_down"), \
        f"没订阅的 kind 扣了 {len(N._state_times.get('qb_down', []))} 枚令牌"

    await N.state("db_down", True, "库停摆了")      # 已订阅的照常发
    assert len(sent) == 1 and "库停摆" in sent[0]


async def test_state_buckets_are_per_kind(sent, cfg):
    """一个抖动的 kind 不该把别的 kind 饿死——桶按 kind 分账。"""
    cfg(NOTIFY_EVENTS=["qb_down", "db_down"])
    for _ in range(N._STATE_CAP_PER_HOUR + 2):
        await N.state("qb_down", True, "坏")
        N._state_now.pop("qb_down", None)          # 强制每轮都判成翻转
    burned = len(sent)
    await N.state("db_down", True, "库停摆了")
    assert len(sent) == burned + 1, "qb_down 把 db_down 的额度吃光了"


async def test_concurrent_flips_push_only_once(sent, cfg, monkeypatch):
    """同一次翻转被两个协程同时看到时只推一条（F12）。

    边沿判定与写回之间隔着 await：qB 掉线时 qB 同步轮与 flush 的 qb_precheck 会同时进来
    （分属不同锁），两个协程都读到同一个 prev、都判成翻转。
    """
    import asyncio

    cfg(NOTIFY_EVENTS=["qb_down"])
    slow = asyncio.Event()

    async def _slow_notify(msg, **kw):
        await slow.wait()
        sent.append(msg)
        return True
    monkeypatch.setattr(N, "notify", _slow_notify)

    t1 = asyncio.create_task(N.state("qb_down", True, "qB 连不上"))
    t2 = asyncio.create_task(N.state("qb_down", True, "qB 连不上"))
    await asyncio.sleep(0)
    slow.set()
    await asyncio.gather(t1, t2)
    assert len(sent) == 1, f"同一次翻转推了 {len(sent)} 条"
