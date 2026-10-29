"""r39：通知与采集健康（A11 新失败源再推 / A12 熔断期状态型不计数 / A13 seen 等入库后再推进）。"""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

import config
import services.notify as N


def _fresh():
    """清成刚 import 的样子（理由同 test_notify_events._fresh；这里多清一个 r39 的 _state_members）。"""
    N._last_sent.clear()
    N._state_now.clear()
    N._state_members.clear()
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
        return True
    monkeypatch.setattr(N, "notify", fake)
    cfg(NOTIFY_URL="http://push.example/key", NOTIFY_MAX_PER_HOUR=0,
        NOTIFY_EVENTS=list(N.EVENTS))
    _fresh()
    yield box
    _fresh()


async def _src(members):
    """模拟 _check_poll_health 的一轮：有失败源就是坏，文案带全量名单。"""
    ms = frozenset(members)
    await N.state("source_down", bool(ms), "源抓取连续失败：" + "、".join(sorted(ms)),
                  "源抓取恢复正常", members=ms)


# ---------------- A11：source_down 出现新成员再推 ----------------

async def test_new_failing_source_pushes_again_and_shrink_does_not(sent):
    """A 挂着时 B 也挂了：纯边沿不会再说话（状态一直是"坏"）。新成员要再推，集合变小不推。"""
    await _src({"A"})
    assert len(sent) == 1, "A 挂：推"
    await _src({"A"})
    assert len(sent) == 1, "同一批每轮重判：不推"
    await _src({"A", "B"})
    assert len(sent) == 2 and "B" in sent[1], "B 也挂了：再推，文案是全量"
    await _src({"B"})
    assert len(sent) == 2, "A 恢复、只剩 B：集合变小，不推"
    await _src({"A", "B"})
    assert len(sent) == 3, "A 恢复过、又挂回来：算新成员"
    await _src({"A"})
    await _src({"A", "B"})
    assert len(sent) == 4, "B 恢复后再挂：算新成员"
    await _src(set())
    assert len(sent) == 5 and "恢复" in sent[4], "全部恢复：推『恢复』"
    assert "source_down" not in N._state_members, "恢复送达后整份丢掉"
    await _src({"A"})
    assert len(sent) == 6, "新的一场从头算"


async def test_members_are_not_recorded_while_unsubscribed(sent, cfg):
    cfg(NOTIFY_EVENTS=["delivered"])
    await _src({"A"})
    assert sent == [] and "source_down" not in N._state_members
    cfg(NOTIFY_EVENTS=["delivered", "source_down"])
    await _src({"A"})
    assert len(sent) == 1, "中途勾上，下一轮就收到当前的坏消息"


async def test_failed_push_of_a_new_member_is_retried(sent, monkeypatch):
    """新成员那条没送到：记忆要还原，下一轮还得算"新"——不然 B 挂掉这件事就被吃了。"""
    await _src({"A"})
    alive = [False]

    async def flaky(msg):
        if not alive[0]:
            return False
        sent.append(msg)
        return True
    monkeypatch.setattr(N, "notify", flaky)
    await _src({"A", "B"})
    assert len(sent) == 1
    assert N._state_members["source_down"] == frozenset({"A"}), "没送到也记成了说过"
    alive[0] = True
    await _src({"A", "B"})
    assert len(sent) == 2 and "B" in sent[1]


async def test_exception_restores_members(sent, monkeypatch):
    await _src({"A"})

    async def boom(msg):
        raise RuntimeError("模拟发送异常")
    monkeypatch.setattr(N, "notify", boom)
    with pytest.raises(RuntimeError):
        await _src({"A", "B"})
    assert N._state_members["source_down"] == frozenset({"A"})
    assert N._state_now["source_down"] is True


async def test_new_member_pushes_once_under_concurrency(sent, monkeypatch):
    """新成员那一推同样先占位：两个协程同时看到时只推一条。"""
    await _src({"A"})
    gate = asyncio.Event()

    async def slow(msg):
        await gate.wait()
        sent.append(msg)
        return True
    monkeypatch.setattr(N, "notify", slow)
    t1 = asyncio.create_task(_src({"A", "B"}))
    t2 = asyncio.create_task(_src({"A", "B"}))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(t1, t2)
    assert len(sent) == 2, f"同一个新成员推了 {len(sent) - 1} 条"


async def test_muted_does_not_eat_new_members(sent):
    """熔断期直接返回、不动成员记忆：熔断一过，新成员照样推出去。"""
    await _src({"A"})
    N._muted_until = time.monotonic() + 300
    await _src({"A", "B"})
    assert len(sent) == 1
    assert N._state_members["source_down"] == frozenset({"A"}), "熔断期把 B 记成说过了"
    N._muted_until = 0.0
    await _src({"A", "B"})
    assert len(sent) == 2 and "B" in sent[1]


async def test_state_without_members_is_unchanged(sent):
    """不传 members 的状态型（qb_down 等）还是纯边沿，也不碰成员记忆。"""
    for _ in range(3):
        await N.state("qb_down", True, "掉线", "恢复")
    await N.state("qb_down", False, "掉线", "恢复")
    assert [s[1:] for s in sent] == ["掉线", "恢复"]
    assert N._state_members == {}


async def test_reset_state_keeps_members(sent):
    await _src({"A"})
    N.reset_state()
    await _src({"A"})
    assert len(sent) == 1, "保存一次设置就把正挂着的源当新成员再推了一遍"


async def test_check_poll_health_passes_failing_source_names(sent, cfg, monkeypatch):
    """worker 把失败源组名传进去：新的源挂掉要再推，有源恢复不推。"""
    from core import health
    from core import worker as W

    monkeypatch.setattr(health, "_fail", {})
    monkeypatch.setattr(health, "_alive", {"甲", "乙", "丙"})
    cfg(ALERT_SOURCE_FAIL_ROUNDS=3, NOTIFY_EVENTS=["source_down"])
    health._fail["甲"] = (3, "HTTPStatusError: 403")
    await W._check_poll_health()
    assert len(sent) == 1 and "甲" in sent[0]
    health._fail["乙"] = (3, "HTTPStatusError: 500")
    await W._check_poll_health()
    assert len(sent) == 2 and "乙" in sent[1], "新的失败源没有再推"
    health._fail.pop("甲")
    await W._check_poll_health()
    assert len(sent) == 2, "有源恢复（集合变小）不该推"
    assert N._state_members["source_down"] == frozenset({"乙"})


# ---------------- A12：熔断期状态型不计数 ----------------

async def test_muted_state_calls_are_not_counted(monkeypatch, cfg):
    """熔断中每轮重判的同一条边沿不算"没送到"：它没丢，熔断一过照常发出（D-04）。"""
    _fresh()
    alive, box = [False], []

    async def fake_send(msg, base=""):
        if not alive[0]:
            return False
        box.append(msg)
        return True
    monkeypatch.setattr(N, "_send_once", fake_send)
    cfg(NOTIFY_URL="http://black.hole/key", NOTIFY_MAX_PER_HOUR=0, NOTIFY_EVENTS=list(N.EVENTS))
    for i in range(N._FAIL_MUTE_AFTER):                  # 先把熔断烧出来
        await N.notify(f"失败{i}")
    assert N._muted_until > 0 and N._dropped == 0
    for _ in range(100):
        await N.state("qb_down", True, "qB 连不上", "qB 恢复")
        await N.state("source_down", True, "源挂了", "源恢复", members=frozenset({"A"}))
    assert N._dropped == 0, f"熔断期的状态型重判被记成了 {N._dropped} 条没送到"
    assert "qb_down" not in N._state_now and "source_down" not in N._state_now
    assert not N._state_times.get("qb_down"), "熔断期扣了状态桶的令牌"

    N._muted_until = 0.0                                  # 静默期满
    alive[0] = True
    await N.state("qb_down", True, "qB 连不上", "qB 恢复")
    assert len(box) == 1 and "另有" not in box[0], box
    assert N._state_now["qb_down"] is True
    _fresh()


# ---------------- A13：seen 等本组入库后再推进 ----------------

def _op_err():
    return OperationalError("INSERT INTO anime_torrent ...", {}, Exception("connection refused"))


def _sec(d: datetime) -> datetime:
    return d.replace(microsecond=0)


@pytest.fixture
def poll(clean_tables, monkeypatch, cfg):
    """跑 poll_once 的最小环境：假源、假 process_item，轮末写配置换成记录（不写 meta 库）。"""
    from core import health
    from core import worker as W

    cfg(QB_ENABLED=False, ANIME_POLL_ENABLED=True, NOTIFY_URL="", HEARTBEAT_URL="",
        SOURCE_FEED_SEEN="", FEED_GAPS="")
    monkeypatch.setattr(health, "_seen", {})
    monkeypatch.setattr(health, "_fail", {})
    writes, gaps = [], []
    monkeypatch.setattr(config, "set_many", lambda u: writes.append(u) or {})
    real_gap = health.gap_record

    def spy_gap(src, frm, to):
        gaps.append((frm, to))
        return real_gap(src, frm, to)
    monkeypatch.setattr(health, "gap_record", spy_gap)

    async def fake_precheck():
        return False
    monkeypatch.setattr(W.anime, "qb_precheck", fake_precheck)

    async def fake_process(item, known_hashes=None, qb_alive=None):
        if item.kind == "broke":
            raise _op_err()
        if item.kind == "dup":
            raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))
        return True
    monkeypatch.setattr(W, "process_item", fake_process)

    n = {"i": 0}

    def source(name, url, oldest, kinds):
        items = []
        for k in kinds:
            n["i"] += 1
            items.append(SimpleNamespace(info_hash=f"{n['i']:040x}", anime_title=f"番{n['i']}", kind=k))

        class FakeSource:
            async def fetch(self, content_cache=None):
                return items
        s = FakeSource()
        s.name, s.rss_url, s.oldest_entry_time = name, url, oldest
        return s

    def run(*sources):
        monkeypatch.setattr(W, "build_sources", lambda: list(sources))
        return W.poll_once()

    return SimpleNamespace(health=health, source=source, run=run, writes=writes, gaps=gaps)


_URL = "https://nyaa.si/?page=rss&u=ANiTorrent"


async def test_connection_error_does_not_advance_seen(poll):
    now = datetime.now(timezone.utc)
    prev = _sec(now - timedelta(hours=1))
    poll.health.mark_seen(_URL, prev)
    await poll.run(poll.source("ANi", _URL, now - timedelta(days=9), ["ok", "broke", "ok"]))
    assert poll.health.seen_at(_URL) == prev, "条目没入库（库连不上），seen 却推进了"
    assert json.loads(poll.writes[-1]["SOURCE_FEED_SEEN"]) == poll.health._seen, "轮末写回的不是内存里那份"


async def test_gap_round_with_connection_error_reports_each_span_once(poll):
    """停机超过窗口后的第一轮已经报了 [prev, O1]，这一轮入库又撞上连接错误：
    seen 写成 O1 —— 下一轮不重报 [prev, …]，失败条目滚出去的那段 [O1, O2] 会单独报出来。"""
    now = datetime.now(timezone.utc)
    prev = _sec(now - timedelta(days=15))
    o1 = _sec(now - timedelta(days=9))
    o2 = _sec(now - timedelta(days=8))
    poll.health.mark_seen(_URL, prev)
    await poll.run(poll.source("ANi", _URL, o1, ["broke"]))
    assert poll.gaps == [(prev, o1)]
    assert poll.health.seen_at(_URL) == o1
    poll.gaps.clear()
    await poll.run(poll.source("ANi", _URL, o2, ["ok"]))     # 库回来了，feed 往前滚了一天
    assert poll.gaps == [(o1, o2)], f"下一轮报的断档不对：{poll.gaps}"


async def test_normal_round_advances_to_fetch_time(poll):
    now = datetime.now(timezone.utc)
    poll.health.mark_seen(_URL, _sec(now - timedelta(hours=1)))
    before = _sec(datetime.now(timezone.utc))
    await poll.run(poll.source("ANi", _URL, now - timedelta(days=9), ["ok", "ok"]))
    after = datetime.now(timezone.utc)
    assert before <= poll.health.seen_at(_URL) <= after


async def test_non_connection_error_still_advances(poll):
    """唯一约束之类不是"库停了"，照常推进（与页面兜底同一口径）。"""
    now = datetime.now(timezone.utc)
    poll.health.mark_seen(_URL, _sec(now - timedelta(hours=1)))
    before = _sec(datetime.now(timezone.utc))
    await poll.run(poll.source("ANi", _URL, now - timedelta(days=9), ["dup", "ok"]))
    assert poll.health.seen_at(_URL) >= before


async def test_lock_error_is_not_a_connection_error(poll, monkeypatch):
    """SQLite 的锁竞争也抛 OperationalError，但它不是"库停了"（looks_like_connection_error 排除）。"""
    from core import worker as W

    async def locked(item, known_hashes=None, qb_alive=None):
        raise OperationalError("UPDATE", {}, Exception("database is locked"))
    monkeypatch.setattr(W, "process_item", locked)
    now = datetime.now(timezone.utc)
    poll.health.mark_seen(_URL, _sec(now - timedelta(hours=1)))
    before = _sec(datetime.now(timezone.utc))
    await poll.run(poll.source("ANi", _URL, now - timedelta(days=9), ["ok"]))
    assert poll.health.seen_at(_URL) >= before


@pytest.mark.parametrize("order", [("ok", "broke"), ("broke", "ok")])
async def test_shared_url_any_broken_group_holds_seen_back(poll, order):
    """同一个 URL 两组共用：本轮只要有一组撞上连接错误，最终 seen 就是 max(本轮开头, 最老一条)，
    不能被另一组的抓取时刻盖掉（否则撞上的那组的失败条目滚出去时判不出断档）。"""
    now = datetime.now(timezone.utc)
    prev = _sec(now - timedelta(hours=1))
    oldest = _sec(now - timedelta(days=9))
    poll.health.mark_seen(_URL, prev)
    await poll.run(poll.source("ANi-甲", _URL, oldest, [order[0]]),
                   poll.source("ANi-乙", _URL, oldest, [order[1]]))
    assert poll.health.seen_at(_URL) == max(prev, oldest)
    assert poll.gaps == [], "同一 URL 第二组重复报了断档"
