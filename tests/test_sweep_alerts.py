"""积压告警（anime.sweep_alerts）：失败 / 停滞 / 待识别。

这条巡检是全项目【唯一】会主动说"有事要你处理"的地方——别的都要用户自己想起来去翻页面。
所以它的覆盖面不对称是很贵的错误：R17 之前三条统计全部只查番剧表，
于是剧场版侧的失败与停滞【永远不响】，而那两种状态走的是同一套 engine.sync_qb_status。
"""
import itertools

import pytest

import config
import core.anime as A
import services.notify as N
from db.models import Anime, AnimeTorrent, Movie, MovieTorrent


@pytest.fixture
def alerts(clean_tables, monkeypatch, cfg):
    """收走推送内容，并把两个模块的通知路径都指向真实的事件层（订阅/冷却/限流照跑）。"""
    box = []

    async def fake(msg):
        box.append(msg)
        return True
    monkeypatch.setattr(N, "notify", fake)
    N._last_sent.clear()
    N._state_now.clear()
    N._sent_times.clear()
    N._state_times.clear()
    N._state_suppressed.clear()
    N._fail_streak, N._muted_until = 0, 0.0
    cfg(NOTIFY_URL="http://push.example/key", NOTIFY_MAX_PER_HOUR=0,
        NOTIFY_EVENTS=list(N.EVENTS), NOTIFY_BACKLOG_MIN=1)
    yield box
    N._last_sent.clear()
    N._state_now.clear()


_seq = itertools.count(1)          # mikan_id 有唯一约束，别让同一条用例里的两部撞名


def _anime_torrent(s, status):
    a = Anime(title="番", quarter="26C", confirmed=True, bangumi_id=1)
    s.add(a)
    s.commit()
    s.refresh(a)
    s.add(AnimeTorrent(anime_id=a.id, raw_title="t", info_hash=f"a{next(_seq):039d}",
                       episode=1, status=status))
    s.commit()


def _movie_torrent(s, status):
    m = Movie(title="剧场版", quarter="26C", bangumi_id=2,
              mikan_id=f"mk{next(_seq)}")
    s.add(m)
    s.commit()
    s.refresh(m)
    s.add(MovieTorrent(movie_id=m.id, raw_title="t", info_hash=f"m{next(_seq):039d}",
                       status=status))
    s.commit()


async def test_movie_failures_are_reported(alerts, clean_tables):
    """(R17) 剧场版的 error 也要报——它以前一条都不响。"""
    with clean_tables.get_session() as s:
        _movie_torrent(s, "error")
    res = await A.sweep_alerts()
    assert res["movie_error"] == 1
    assert any("失败" in m for m in alerts), f"剧场版失败没有推送：{alerts}"


async def test_movie_stalls_are_reported(alerts, clean_tables):
    with clean_tables.get_session() as s:
        _movie_torrent(s, "stalled")
    res = await A.sweep_alerts()
    assert res["movie_stalled"] == 1
    assert any("无进度" in m for m in alerts), alerts


async def test_movie_backlog_counts_toward_the_threshold(alerts, clean_tables):
    """未识别的剧场版同样堆在『待识别』tab 里，也该计入积压。"""
    with clean_tables.get_session() as s:
        s.add(Movie(title="没认出来的剧场版", quarter="26C", mikan_id="mk9"))
        s.commit()
    res = await A.sweep_alerts()
    assert res["movie_backlog"] == 1
    assert any("待识别" in m for m in alerts), alerts


async def test_both_sides_are_broken_down_in_the_message(alerts, clean_tables):
    """两边都有积压时消息里要写清各自多少——否则"5 条失败"没法决定去哪个页面看。"""
    with clean_tables.get_session() as s:
        _anime_torrent(s, "error")
        _movie_torrent(s, "error")
    await A.sweep_alerts()
    msg = next(m for m in alerts if "失败" in m)
    assert "番剧 1" in msg and "剧场版 1" in msg, msg
    assert "2 条" in msg, msg


async def test_key_distinguishes_which_side_changed(alerts, clean_tables):
    """(R17) 去重 key 必须含两边的数：只用总数的话，番剧 +1 / 剧场版 −1 得到同一个 key，
    6 小时冷却窗口内这次变化就被静默掉了。"""
    with clean_tables.get_session() as s:
        _anime_torrent(s, "error")
        _anime_torrent(s, "error")
    await A.sweep_alerts()
    assert len(alerts) >= 1
    n = len(alerts)
    with clean_tables.get_session() as s:                 # 总数不变，构成变了
        from sqlmodel import select
        t = s.exec(select(AnimeTorrent)).first()
        s.delete(t)
        s.commit()
        _movie_torrent(s, "error")
    await A.sweep_alerts()
    assert len(alerts) > n, f"构成变了却因为总数没变被冷却吃掉：{alerts}"


async def test_quiet_when_nothing_is_wrong(alerts, clean_tables):
    """一切正常时一条都不该发（首次观测到"好"的状态型只记不发）。"""
    await A.sweep_alerts()
    assert alerts == [], alerts


# ---------------- (R21) 去重键要认【是哪几条】，不是【有几条】 ----------------

async def test_a_new_stall_is_reported_even_when_the_count_bounces_back(alerts, clean_tables):
    """3 → 2 → 3：用户处理掉一条停滞、随后又新卡死一条 —— 第三次必须照样报。

    去重键原来是 `f"{stalls}+{m_stalls}"`（条数）+ 6 小时冷却，而 `notify.cooldown_active`
    按 (kind, key) 记账：条数兜回 6 小时内出现过的旧值时，新告警被静默吞掉。
    而这条巡检是全项目唯一会主动说"有事要你处理"的地方，
    用户此刻看到的仪表盘数字恰好还是他已经被通知过的那个 3 ——
    没有任何迹象说明里面换了一条新的。函数 docstring 明写"变了立刻再说一次"，
    在这个形状下是假话。
    """
    from sqlmodel import select

    with clean_tables.get_session() as s:
        for _ in range(3):
            _anime_torrent(s, "stalled")
    await A.sweep_alerts()
    assert len(alerts) == 1, "第一次没报"

    # 用户处理掉一条（改成人工终态）
    with clean_tables.get_session() as s:
        row = s.exec(select(AnimeTorrent).where(AnimeTorrent.status == "stalled")).first()
        row.status = "deleted"
        s.add(row)
        s.commit()
    await A.sweep_alerts()
    assert len(alerts) == 2, "降到 2 条时没报"

    # 又新卡死一条 → 条数回到 3，但【是另一条】
    with clean_tables.get_session() as s:
        _anime_torrent(s, "stalled")
    await A.sweep_alerts()
    assert len(alerts) == 3, "条数兜回 3 时新告警被旧 key 的冷却吞掉了"


async def test_an_unchanged_batch_still_does_not_repeat(alerts, clean_tables):
    """反向：同一批一条没变时仍然不许重复打扰 —— 冷却本身不能因为换了键就失效。"""
    with clean_tables.get_session() as s:
        for _ in range(3):
            _anime_torrent(s, "stalled")
    await A.sweep_alerts()
    await A.sweep_alerts()
    await A.sweep_alerts()
    assert len(alerts) == 1, f"同一批被重复推送了 {len(alerts)} 次"


# ---------------- (R26) 绑定可疑：只报不改 ----------------

async def test_a_confirmed_anime_with_impossible_episodes_is_reported(alerts, clean_tables):
    """先确认、之后才收到矛盾种子的番，必须被报出来 —— 而且**不许自动改状态**。

    `binding_looks_wrong` 全项目 4 个调用点**全是"不让番【进入】追番中"的闸**
    （建番、自动升确认、批量重算超期、合并前），没有任何一处对已 confirmed 的番重算，
    三条巡检也都不看它，页面上一次都没引用过。
    于是这样的番永久停在错状态，而它仍显示"追番中"、集去重照常生效 ——
    那批不属于本季的种子会一直占着去重键，**挡住真正的本季集**。

    真库实证：anime#6（第 3 季、bgm 记 12 集）下面挂着 24 条第一季的正片，
    `binding_looks_wrong` 返回 True，而它 `confirmed=True / rejected=False`。

    **只报不改**：自动置 `confirmed=False` 会让整部番掉出 `subscribed_where()`、
    停掉自动下载，而判据本身有 bgm 数据质量的残留风险。
    """
    from datetime import datetime, timedelta

    from db.models import Anime, AnimeTorrent

    with clean_tables.get_session() as s:
        a = Anime(title="某番 第三季", display_name="某番 第三季", season=3, quarter="26C",
                  confirmed=True, rejected=False, bangumi_id=598058,
                  total_episodes=12, air_date="2026-07-05")
        s.add(a); s.commit(); s.refresh(a)
        # 第一季的正片：发布时间比本季首播早两年多
        for i in range(3):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{i:040x}", raw_title=f"x - {i+1:02d}",
                               season=3, episode=float(i + 1), status="pending",
                               release_time=datetime(2026, 7, 5) - timedelta(weeks=130)))
        s.commit()
        aid = a.id

    from core import anime as A
    hits = A.suspect_wrong_binding()
    assert [h["id"] for h in hits] == [aid], f"没报出来：{hits}"
    assert hits[0]["bad"] == 3 and hits[0]["eps"] == [1, 2, 3]

    res = await A.sweep_alerts()
    assert res["wrong_binding"] == 1
    assert any("绑定看着不对" in m for m in alerts), f"巡检没推送：{alerts}"

    with clean_tables.get_session() as s:
        got = s.get(Anime, aid)
    assert got.confirmed is True and got.rejected is not True, \
        "只该报，不该自动改状态 —— 置 confirmed=False 会停掉整部番的自动下载"


async def test_a_healthy_anime_is_not_reported(alerts, clean_tables):
    """反向：绑定正常的番不该被报 —— 误报会让用户学会无视这条横幅。"""
    from datetime import datetime

    from core import anime as A
    from db.models import Anime, AnimeTorrent

    with clean_tables.get_session() as s:
        a = Anime(title="正常番", season=1, quarter="26C", confirmed=True,
                  bangumi_id=111, total_episodes=12, air_date="2026-07-05")
        s.add(a); s.commit(); s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="9" * 40, raw_title="x - 03",
                           season=1, episode=3.0, status="pending",
                           release_time=datetime(2026, 7, 19)))
        s.commit()
    assert A.suspect_wrong_binding() == []


async def test_the_scan_does_not_grow_one_query_per_anime(clean_tables):
    """(R27) 全库扫的查询次数不能随番数增长。

    它挂在仪表盘的**同步构建路径**上（`pages/anime.py` 的 warn_banner），
    每次渲染 + 每 30 秒的定时刷新各跑一遍。R26 刚加它的时候是 N+1（判据里查一次种子、
    取 bad 明细再查一次），真库 99 部番实测 120~155ms —— 事件循环被同步冻住那么久。

    这条用例数的是**真正发到数据库的语句条数**：番数从 3 部涨到 30 部，
    语句数必须一条不涨。退回逐部番查的写法时它会从 2 涨到 60+。
    """
    from datetime import datetime, timedelta

    import sqlalchemy as sa

    from core import anime as A
    from db.models import Anime, AnimeTorrent

    def _seed(n, offset):
        with clean_tables.get_session() as s:
            for k in range(n):
                a = Anime(title=f"番{offset + k}", season=3, quarter="26C", confirmed=True,
                          rejected=False, bangumi_id=900000 + offset + k,
                          total_episodes=12, air_date="2026-07-05")
                s.add(a); s.commit(); s.refresh(a)
                s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{offset + k:040x}",
                                   raw_title="x - 01", season=3, episode=1.0, status="pending",
                                   release_time=datetime(2026, 7, 5) - timedelta(weeks=130)))
                s.commit()

    def _count():
        n = [0]
        eng = clean_tables.engine

        def _tick(*a, **k):
            n[0] += 1
        sa.event.listen(eng, "before_cursor_execute", _tick)
        try:
            A.suspect_wrong_binding()
        finally:
            sa.event.remove(eng, "before_cursor_execute", _tick)
        return n[0]

    _seed(3, 0)
    few = _count()
    _seed(27, 100)
    many = _count()
    assert len(A.suspect_wrong_binding()) == 30, "先确认这 30 部真的都被扫到了"
    assert many == few, f"查询数随番数涨了：3 部 {few} 条 → 30 部 {many} 条（N+1 又回来了）"
