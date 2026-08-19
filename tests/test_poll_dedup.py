"""采集轮的批量查重：省掉 N 次数据库往返，但不能因此漏判或重复处理。

这一组守的是 R3 那次提速改动——它把"每条 RSS 条目各查一次库"换成了"一批预取一次"，
而批量预取天然有个盲区：预取【之后】才入库的 hash 不在集合里。
"""
from datetime import datetime

import pytest
from sqlmodel import select

from core import anime as A
from db.models import Anime, AnimeTorrent


@pytest.fixture
def seeded(clean_tables):
    with clean_tables.get_session() as s:
        a = Anime(title="番", season=1, confirmed=True)
        s.add(a)
        s.commit()
        s.refresh(a)
        for i in range(3):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{i:040x}", raw_title=f"t{i}",
                               episode=i + 1, status="sent", created_at=datetime.now()))
        s.commit()
    return clean_tables


def test_existing_hashes_finds_exactly_the_known_ones(seeded):
    have = [f"{i:040x}" for i in range(3)]
    new = [f"{i:040x}" for i in range(90, 95)]
    assert A.existing_hashes(have + new) == set(have)


def test_existing_hashes_handles_empty_and_none(seeded):
    assert A.existing_hashes([]) == set()
    assert A.existing_hashes([None, "", None]) == set()


async def test_known_hashes_short_circuits_without_touching_the_db(seeded, monkeypatch):
    """传了预取集合就不该再查库——这正是这次改动要省掉的那次往返。"""
    calls = []
    real = A.get_session

    def counting(*a, **kw):
        calls.append(1)
        return real(*a, **kw)
    monkeypatch.setattr(A, "get_session", counting)

    class Item:
        info_hash = f"{0:040x}"
    assert await A.process_item(Item(), known_hashes={f"{0:040x}"}) is False
    assert calls == [], "命中预取集合时不该开 session"


async def test_without_known_hashes_it_still_queries(seeded):
    """零散入口（手动补齐等）不传集合时，照旧自己查一次，行为不变。"""
    class Item:
        info_hash = f"{0:040x}"
    assert await A.process_item(Item()) is False
