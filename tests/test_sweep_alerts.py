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
