"""采集轮的 bgm 熔断：连续 N 部新番都因 bgm 没问成（连接失败 / 整体预算超时）没识别成，本轮剩下的跳过识别。

跳过的番走与识别失败完全相同的路径：进『待识别』、种子照常入库。查无此番不计数（bgm 是通的）。
打桩 enrich._resolve_inner（resolve 外层的预算与记账照真的跑），零网络。
"""
import inspect
import logging
from datetime import datetime

import pytest
from sqlmodel import select

from core import anime as A
from db.models import Anime, AnimeTorrent
from services import enrich
from sources.base import ParsedItem


def _item(i):
    title = f"熔断测试番{i}"
    return ParsedItem(info_hash=f"{i:040x}", raw_title=f"[ANi] {title} - 01 [1080P]", anime_title=title,
                      season=1, episode=1, quarter="26D", release_time=datetime.now(),
                      download_url="http://x/1.torrent", source="ANi", site="nyaa", policy="auto",
                      priority=0, search_names=[title])


@pytest.fixture
def bgm(monkeypatch):
    """script：每次 _resolve_inner 被调时依次取一个行为——net（连接失败）/ timeout（耗尽预算）/ miss（查无）。"""
    st = {"script": [], "calls": 0}
    monkeypatch.setattr(enrich, "_RESOLVE_BUDGET", 0.05)

    async def _inner(names, est, date_ref, info_hash, episode=None):
        st["calls"] += 1
        b = st["script"].pop(0) if st["script"] else "miss"
        if b == "net":
            enrich._note_bgm_fail()
        elif b == "timeout":
            import asyncio
            await asyncio.sleep(5)
        return None
    monkeypatch.setattr(enrich, "_resolve_inner", _inner)
    return st


@pytest.fixture
def base(clean_tables, cfg):
    cfg(BGM_ROUND_BREAKER=2, ANIME_START_DATE="", QB_ENABLED=False)
    return clean_tables


def _all_unmatched(db, n):
    with db.get_session() as s:
        animes = s.exec(select(Anime)).all()
        assert len(animes) == n
        assert all(a.bangumi_id is None and a.confirm_reason == "unmatched" and not a.confirmed
                   and not a.rejected for a in animes)
        assert len(s.exec(select(AnimeTorrent)).all()) == n, "种子没有照常入库"


async def test_two_timeouts_then_skip_but_still_ingested(base, bgm, caplog):
    bgm["script"] = ["timeout", "net"]
    A.bgm_round_start()
    for i in range(1, 5):
        assert await A.process_item(_item(i)) is True
    assert bgm["calls"] == 2, "熔断后仍在调 resolve"
    _all_unmatched(base, 4)
    with caplog.at_level(logging.WARNING):
        A.bgm_round_end()
    assert "剩下 2 部跳过识别" in caplog.text


async def test_not_found_does_not_count(base, bgm):
    bgm["script"] = ["miss", "miss", "miss", "miss"]
    A.bgm_round_start()
    for i in range(1, 5):
        await A.process_item(_item(i))
    assert bgm["calls"] == 4


async def test_not_found_breaks_the_streak(base, bgm):
    """'连续'：中间夹一部查无（bgm 通着）就从头数。"""
    bgm["script"] = ["net", "miss", "net", "net", "net"]
    A.bgm_round_start()
    for i in range(1, 6):
        await A.process_item(_item(i))
    assert bgm["calls"] == 4


async def test_zero_disables(base, bgm, cfg):
    cfg(BGM_ROUND_BREAKER=0)
    bgm["script"] = ["net"] * 4
    A.bgm_round_start()
    for i in range(1, 5):
        await A.process_item(_item(i))
    assert bgm["calls"] == 4


async def test_next_round_resets(base, bgm):
    bgm["script"] = ["net", "net"]
    A.bgm_round_start()
    for i in range(1, 4):
        await A.process_item(_item(i))
    assert bgm["calls"] == 2
    A.bgm_round_start()
    await A.process_item(_item(4))
    assert bgm["calls"] == 3, "下一轮没重置，bgm 恢复了也认不了新番"


async def test_outside_a_round_never_breaks(base, bgm):
    """没开轮的调用方（默认状态）不熔断。"""
    A._bgm_round.set(None)
    bgm["script"] = ["net"] * 4
    for i in range(1, 5):
        await A.process_item(_item(i))
    assert bgm["calls"] == 4


def test_poll_once_opens_and_closes_the_round():
    from core import worker
    src = inspect.getsource(worker.poll_once)
    assert "anime.bgm_round_start()" in src and "anime.bgm_round_end()" in src
