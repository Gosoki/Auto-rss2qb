"""通知的事件层：订阅过滤 / 边沿触发 / 冷却 / 限流。

这一层存在的理由是"qB 掉线时每 30 秒一条推送"——那不是通知，是骚扰，
而且会把真正重要的那条淹掉。所以每一条抑制规则都要有用例。
"""
import pytest

import config
import services.notify as N
from sqlmodel import select


def _fresh():
    """把模块级状态清成刚 import 的样子。

    【不能用 notify.reset_state()】那是【生产语义】的重置（用户保存设置时调），它【故意】
    不碰状态记忆与限流账本——理由写在它自己的 docstring 里。用它当测试夹具的话，
    上一条用例留下的 _state_now 会渗进下一条，而且这两套语义一旦被同一个函数承担，
    以后谁想收窄生产那边就会被一堆用例挡住。夹具要的是"干净的进程"，直接清就好。
    """
    N._last_sent.clear()
    N._state_now.clear()
    N._sent_times.clear()
    N._state_times.clear()
    N._state_suppressed.clear()
    N._dropped = 0
    N._fail_streak, N._muted_until = 0, 0.0


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
    _fresh()
    yield box
    _fresh()


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
    assert "没能送到" in sent[-1]


async def test_rate_limit_off_by_zero(sent, cfg):
    cfg(NOTIFY_MAX_PER_HOUR=0)
    for i in range(30):
        await N.event("delivered", f"第{i}集")
    assert len(sent) == 30


async def test_reset_state_clears_cooldown_not_state_memory(sent, cfg):
    """保存设置 = 清冷却与熔断，【不】清状态记忆与限流账本。

    冷却：用户改完开关就该立刻看到效果，而不是等 6 小时窗口过期。
    状态记忆：清了它，"坏→好"那次翻转会退化成 state() 里"只记不发"的 None→好 那一支，
    于是一次不相干的保存就把『qB 已恢复』整条吃掉（边沿要等故障【还在持续】才自愈，
    保存恰好落在故障刚结束那几秒时就真的没了）。
    限流账本：桶容量是现读 config 的，改大改小立刻生效；清账本等于"点几次保存就能绕过上限"。
    """
    # 冷却清掉了 → 同一条能立刻再发
    await N.event("failed", "1 条失败", key="k", cooldown=3600)
    await N.event("failed", "1 条失败", key="k", cooldown=3600)
    assert len(sent) == 1, "冷却应当挡住第二条"
    N.reset_state()
    await N.event("failed", "1 条失败", key="k", cooldown=3600)
    assert len(sent) == 2, "保存设置后冷却应当清掉"

    # 状态记忆留着 → 保存不会把『已恢复』吃掉
    sent.clear()
    await N.state("qb_down", True, "掉线", "恢复")
    assert sent == ["🔌掉线"]
    N.reset_state()                       # 用户此刻在设置页点了保存
    await N.state("qb_down", False, "掉线", "恢复")
    assert sent == ["🔌掉线", "🔌恢复"], f"『已恢复』被一次保存吃掉了：{sent}"

    # 限流账本留着 → 连点保存不能刷额度
    # 【改配置一律走 cfg 夹具】config 的读取是模块级 __getattr__ → _v；直接 `config.X = v`
    # 会在模块上创建一个真属性，从此【永久遮蔽】__getattr__，函数末尾改回去也没用——
    # 后面所有用例的 cfg(X=...) 全部静默失效。写这条用例时就这么踩了一次。
    sent.clear()
    N._sent_times.clear()
    cap = 2
    cfg(NOTIFY_MAX_PER_HOUR=cap)
    for i in range(cap):
        await N.event("delivered", f"第{i}集")
    N.reset_state()
    await N.event("delivered", "刷额度")
    assert len(sent) == cap, f"保存一次设置就把限流额度刷掉了：{sent}"


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


# ---------------- 限流记账的时机 / 黑洞地址熔断（R17） ----------------

async def test_failed_sends_do_not_burn_the_hourly_quota(monkeypatch, cfg):
    """(R17) 桶按【送达】记账，不按【尝试】记账。

    用户口径就是"每小时最多收到几条"（设置页标签）与"另有 N 条没能送到"（消息尾巴）——
    两句说的都是收到几条。早先是先 append 再 await：推送服务抖一下、连着失败 N 条，
    用户一条没收到，一小时的额度却烧光了，接下来真正该送达的告警全被自己的桶挡在门外。
    """
    _fresh()
    box, alive = [], [False]

    async def fake(msg):
        if not alive[0]:
            return False              # 推送服务抖动中
        box.append(msg)
        return True
    monkeypatch.setattr(N, "notify", fake)
    cfg(NOTIFY_URL="http://push.example/key", NOTIFY_MAX_PER_HOUR=3,
        NOTIFY_EVENTS=list(N.EVENTS))
    for i in range(3):
        assert await N.event("delivered", f"抖动期第{i}集") is False
    alive[0] = True
    for i in range(3):
        assert await N.event("delivered", f"恢复后第{i}集") is True, "额度被失败的那几条烧掉了"
    assert len(box) == 3
    assert await N.event("delivered", "第4条") is False, "送达 3 条之后桶就该满"
    _fresh()


async def test_dropped_counter_is_restored_whole(monkeypatch, cfg):
    """(R17) 发送失败时"另有 N 条没能送到"要【原样】还回去，不能塌成 1。

    塌成 1 之后用户看到的是"偶尔漏一条"，于是永远不会想到去调大 NOTIFY_MAX_PER_HOUR，
    而实际漏的是几十条。低报比不报更有误导性。
    """
    _fresh()
    box, alive = [], [True]

    async def fake(msg):
        if not alive[0]:
            return False
        box.append(msg)
        return True
    monkeypatch.setattr(N, "notify", fake)
    cfg(NOTIFY_URL="http://push.example/key", NOTIFY_MAX_PER_HOUR=1,
        NOTIFY_EVENTS=list(N.EVENTS))
    await N.event("delivered", "占掉额度")
    for i in range(9):
        await N.event("delivered", f"被丢的第{i}条")
    assert N._dropped == 9
    N._sent_times.clear()                 # 一小时过去了，桶空了
    alive[0] = False
    assert await N.event("delivered", "这条发失败") is False
    assert N._dropped == 9, f"失败一次就把 9 条塌成了 {N._dropped} 条"
    alive[0] = True
    await N.event("delivered", "这条成功")
    assert box[-1].endswith("（另有 9 条没能送到）"), box[-1]
    _fresh()


async def test_black_hole_url_stops_being_dialed(monkeypatch, cfg):
    """(R17) 推送地址变成黑洞时要熔断——否则每条通知都白等满 NOTIFY_TIMEOUT，
    而 notify() 就串在交付主链路上（每交付一集一条 await），一轮 flush 会被拖成 N × 超时。

    桶救不了这件事：桶只在送达时记账，发不出去的那条一枚令牌都不占。
    """
    _fresh()
    tries = []

    async def fake_send(msg, base=""):
        tries.append(msg)
        return False                      # 永远发不出去
    monkeypatch.setattr(N, "_send_once", fake_send)
    cfg(NOTIFY_URL="http://black.hole/key", NOTIFY_MAX_PER_HOUR=0,
        NOTIFY_EVENTS=list(N.EVENTS))
    for i in range(20):
        assert await N.notify(f"第{i}条") is False
    assert len(tries) == N._FAIL_MUTE_AFTER, \
        f"熔断后还在拨号：发了 {len(tries)} 次网络请求"

    N.reset_state()                       # 用户改完 NOTIFY_URL 点了保存
    await N.notify("改完地址第一条")
    assert len(tries) == N._FAIL_MUTE_AFTER + 1, "保存设置应当立刻解除熔断"
    _fresh()


async def test_one_success_clears_the_fail_streak(monkeypatch, cfg):
    """连续失败的计数要被一次成功清零——否则一天里零星失败 5 次就把推送闭嘴 5 分钟。"""
    _fresh()
    alive = [False]

    async def fake_send(msg, base=""):
        return alive[0]
    monkeypatch.setattr(N, "_send_once", fake_send)
    cfg(NOTIFY_URL="http://push.example/key", NOTIFY_MAX_PER_HOUR=0,
        NOTIFY_EVENTS=list(N.EVENTS))
    for _ in range(N._FAIL_MUTE_AFTER - 1):
        await N.notify("失败")
    alive[0] = True
    assert await N.notify("成功") is True
    assert N._fail_streak == 0
    alive[0] = False
    for _ in range(N._FAIL_MUTE_AFTER - 1):
        assert await N.notify("再失败") is False
    assert N._muted_until == 0.0, "没到连续 %d 次就熔断了" % N._FAIL_MUTE_AFTER
    _fresh()


async def test_state_event_with_no_ok_message_burns_no_token(sent):
    """"只记不发"的那两支不能扣状态桶的令牌——它们连请求都没发出去。

    扣了的话，一个 ok_msg 为空的状态型事件会靠"一切正常"这件事本身消耗抖动预算。
    """
    _fresh()
    for i in range(N._STATE_CAP_PER_HOUR + 2):
        await N.state("qb_down", True, "掉线")      # 无 ok_msg
        await N.state("qb_down", False, "掉线")     # 只记不发
    assert len(N._state_times.get("qb_down", [])) == len(sent), \
        "扣的令牌数应当等于真的推出去的条数"
    _fresh()


async def test_muted_messages_are_counted_too(monkeypatch, cfg):
    """(R18) 熔断期丢掉的条数也要记账，否则它比限流还隐形。

    桶是按【送达】记账的，所以熔断期间 _rate_ok() 恒通过、_dropped 恒为 0 ——
    用户既收不到通知，也不会在任何一条消息尾巴上看到"有几条没送到"，
    唯一痕迹是日志里那一行 warning。而通知本来就是给不看日志的人用的。
    """
    _fresh()
    alive = [False]
    sent = []

    async def fake_send(msg, base=""):
        if not alive[0]:
            return False
        sent.append(msg)
        return True
    monkeypatch.setattr(N, "_send_once", fake_send)
    cfg(NOTIFY_URL="http://black.hole/key", NOTIFY_MAX_PER_HOUR=0, NOTIFY_EVENTS=list(N.EVENTS))

    for i in range(N._FAIL_MUTE_AFTER):                  # 先把熔断烧出来
        await N.event("delivered", f"失败{i}")
    assert N._muted_until > 0
    burned = N._dropped
    for i in range(7):                                   # 熔断期又来了 7 条
        assert await N.event("delivered", f"熔断期{i}") is False
    assert N._dropped == burned + 7, \
        f"熔断期丢掉的没有记账：{N._dropped} vs {burned + 7}"

    N.reset_state()                                      # 用户改完地址点了保存
    alive[0] = True
    await N.event("delivered", "恢复后第一条")
    assert sent[-1].endswith(f"（另有 {burned + 7} 条没能送到）"), sent[-1]
    _fresh()


async def test_test_button_does_not_touch_cooldowns_or_global_config(monkeypatch, cfg):
    """(R18) 『发送测试通知』自称"只验证、不改变任何状态"，就得真的做到两件事：

    ① 不清冷却 —— reset_state 会把 failed/stalled 的 6 小时窗口与 finished 的 7 天窗口一起清掉，
       点一下测试按钮，下一轮巡检就可能把一模一样的告警再推一遍。
    ② 不改全局 NOTIFY_URL —— 早先是临时换掉、await 完再换回来，而那段窗口最长有
       NOTIFY_TIMEOUT，期间任何后台协程发出的通知都会被送到这个还没保存、可能填错的地址上。
    """
    _fresh()
    got = []

    async def fake_send(msg, base=""):
        got.append(base)
        return True
    monkeypatch.setattr(N, "_send_once", fake_send)
    cfg(NOTIFY_URL="http://saved.example/key", NOTIFY_MAX_PER_HOUR=0, NOTIFY_EVENTS=list(N.EVENTS))

    await N.event("failed", "1 条失败", key="k", cooldown=3600)
    assert len(got) == 1

    N.clear_mute()                                    # 按钮处理器现在只调这一个
    await N.notify("测试", url_override="http://typed-in-the-box/xyz")
    assert got[-1] == "http://typed-in-the-box/xyz", "没有用框里那个地址"
    import config as C
    assert C.NOTIFY_URL == "http://saved.example/key", "全局 NOTIFY_URL 被改动了"

    n = len(got)
    await N.event("failed", "1 条失败", key="k", cooldown=3600)
    assert len(got) == n, "冷却窗口被测试按钮清掉了，同一条告警会重推"
    _fresh()


def test_the_test_button_uses_the_narrow_reset():
    """守住调用点：设置页那个按钮必须调 clear_mute，不能退回 reset_state。"""
    import pathlib
    src = pathlib.Path("pages/settings.py").read_text(encoding="utf8")
    seg = src[src.index("async def _test_notify"):src.index("async def _test_notify") + 2200]
    # 【只看代码，不看注释】第一版把注释一起扫了，于是解释"为什么不用 reset_state"的那句话
    # 自己把用例判红了——静态守卫扫源码时这是最常见的自伤。
    code = "\n".join(l.split("#", 1)[0] for l in seg.splitlines() if not l.strip().startswith("#"))
    assert "notify.clear_mute()" in code, "测试按钮没走窄的那个重置"
    assert "reset_state" not in code, "测试按钮又清掉了冷却窗口"
    assert "url_override=" in code, "测试按钮又去改全局 NOTIFY_URL 了"
    assert '_v["NOTIFY_URL"]' not in code


# ---------------- (R21) 三层抑制里，每一层被挡下都要留痕迹 ----------------

async def test_the_state_bucket_leaves_a_trace_when_it_suppresses(cfg, monkeypatch, caplog):
    """状态型小桶挡下一场翻转时必须留一行日志 —— 而且【每场只留一行】。

    三层抑制里另外两层各自都留账：`_rate_ok` 挡下时 `_dropped += 1`（条数会挂在下一条消息
    尾巴上），熔断期挡下时 `_dropped += 1` + 一行 warning。**只有这一层什么都不留**，
    于是桶满之后一整场 qb_down/db_down 的翻转颗粒无收 ——
    而这两个恰恰是全项目仅有的带外故障信号。

    【为什么不按次记 `_dropped`】抑制是按边沿【每轮重判】的，按次记会把同一次故障报成
    "另有 40 条没能送到"。所以粒度是"每条被压住的边沿只记一次"。
    """
    # 【必须自己清一次】本文件的模块级状态由 `sent` 夹具里的 _fresh() 负责，
    # 而这两条用例没用那个夹具 —— 不清的话上一条留下的 _state_times / _state_suppressed
    # 会渗进来（实测：单跑绿、整文件跑红，最难查的那种）。
    _fresh()
    import logging

    sent = []

    async def ok(msg, base):
        sent.append(msg)
        return True
    monkeypatch.setattr(N, "_send_once", ok)
    cfg(NOTIFY_URL="http://push.example/k", NOTIFY_EVENTS=["qb_down"], NOTIFY_MAX_PER_HOUR=0)

    with caplog.at_level(logging.WARNING, logger="autorss"):
        for _ in range(6):                       # 6 组 down/up 把 12 枚令牌打满（都是真送达）
            await N.state("qb_down", True, "坏", "好")
            await N.state("qb_down", False, "坏", "好")
        assert len(N._state_times["qb_down"]) == N._STATE_CAP_PER_HOUR, "前提：桶已经满了"
        before = len(sent)
        for _ in range(20):                      # 之后这一整场都被挡下
            await N.state("qb_down", True, "坏", "好")
            await N.state("qb_down", False, "坏", "好")

    assert len(sent) == before, "桶满之后不该再送出去"
    muted_logs = [r for r in caplog.records if "抖动限流" in r.getMessage()]
    assert len(muted_logs) == 1, (
        f"被挡下的这一场留了 {len(muted_logs)} 行日志 —— 应该恰好 1 行"
        "（0 行=无声无息，多行=同一次故障刷屏）")
    assert "qb_down" in muted_logs[0].getMessage()


async def test_the_trace_comes_back_after_the_bucket_frees_up(cfg, monkeypatch, caplog):
    """反向：桶松开、下一场再被挡住时，要重新记一行 —— 不能只记这辈子第一次。"""
    # 【必须自己清一次】本文件的模块级状态由 `sent` 夹具里的 _fresh() 负责，
    # 而这两条用例没用那个夹具 —— 不清的话上一条留下的 _state_times / _state_suppressed
    # 会渗进来（实测：单跑绿、整文件跑红，最难查的那种）。
    _fresh()
    import logging
    import time as _t

    async def ok(msg, base):
        return True
    monkeypatch.setattr(N, "_send_once", ok)
    cfg(NOTIFY_URL="http://push.example/k", NOTIFY_EVENTS=["qb_down"], NOTIFY_MAX_PER_HOUR=0)

    with caplog.at_level(logging.WARNING, logger="autorss"):
        for _ in range(6):
            await N.state("qb_down", True, "坏", "好")
            await N.state("qb_down", False, "坏", "好")
        await N.state("qb_down", True, "坏", "好")            # 第一场：记一行
        # 桶按滚动一小时过期 —— 把时间戳整体前移，模拟一小时后
        N._state_times["qb_down"] = [t - 3601 for t in N._state_times["qb_down"]]
        await N.state("qb_down", False, "坏", "好")            # 松开了，这条发得出去
        for _ in range(6):                                     # 再打满一次
            await N.state("qb_down", True, "坏", "好")
            await N.state("qb_down", False, "坏", "好")
        await N.state("qb_down", True, "坏", "好")            # 第二场：要再记一行

    muted = [r for r in caplog.records if "抖动限流" in r.getMessage()]
    assert len(muted) == 2, f"第二场没再记（拿到 {len(muted)} 行）—— 说明标记没被松开时清掉"


# ---------------- (R24) 配置污染不能跨用例传播 ----------------

def test_writing_config_does_not_leak_into_the_next_test(testdb):
    """`config.set_many` 写完之后，收尾夹具必须把内存与 meta 库都还原。

    ⚠️ 这条挡的不是"配置不对"，是它让**一整类用例变成空的**：
    `set_many` 先写 meta 库的 setting 行、再 `_v.update(...)`，而 `_v` 是模块级、
    整个 pytest session 只有一份。本文件上面两条用例（它们确实需要真写库 ——
    测的就是"存进去再读回来"这条路）曾把 `NOTIFY_EVENTS` 从 9 个事件改成 `['delivered']`
    并**保持到 session 结束**：此后任何不带 `cfg` 夹具的用例里
    `notify_enabled('finished'/'idle'/'stalled'/…)` 恒为 False，
    于是"断言没发通知"恒成立、`sweep_finished` 里
    `may_mark = ok or not notify_enabled(...)` 也恒走那一支 ——
    而那正是 R20 修过的 E-32 冷却回归的判据。

    还原由 `tests/conftest.py::_restore_config_after_each_test`（autouse）负责，
    这条用例只是把"它确实在工作"钉住。
    """
    import config as _C

    before = list(_C.NOTIFY_EVENTS)
    assert len(before) >= 5, f"前提坏了：进这条用例时 NOTIFY_EVENTS 已经被污染成 {before}"
    _C.set_many({"NOTIFY_EVENTS": "delivered"})
    assert _C.NOTIFY_EVENTS == ["delivered"], "写进去都没生效，用例的前提坏了"
    # 收尾夹具会还原 —— 下一条用例看到的应当仍是 before。
    # 这里不能自己还原，否则测的就是自己的 finally 而不是夹具（第③号形状）。


def test_the_previous_test_did_not_leave_anything_behind():
    """紧跟在上一条后面：看它写下的东西有没有被夹具收干净。

    两条必须相邻且按顺序跑 —— 这正是"用例间污染"唯一能被观测到的方式。
    """
    import config as _C

    assert _C.NOTIFY_EVENTS != ["delivered"], \
        "上一条用例写的 NOTIFY_EVENTS 漏到了这一条 —— 收尾夹具没生效"
    assert len(_C.NOTIFY_EVENTS) >= 5, f"NOTIFY_EVENTS 被污染成 {_C.NOTIFY_EVENTS}"
