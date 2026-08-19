"""剧场版扫描的入库守卫。

扫描一部片要 await 两次网络（详情页 + 种子 RSS），这期间用户完全可能在页面上把它绑到
别的 bgm 上、于是这部 Movie 被合并掉。种子按 info_hash 全局去重，挂到已消失的 Movie 上
就成了孤儿：它占着 hash，而真正那部片【永远收不到这几个版本】。
"""
from datetime import datetime

import pytest
from sqlmodel import select

from core import movies as M
from db.models import Movie, MovieTorrent


class Item:
    def __init__(self, h):
        self.info_hash, self.source, self.site = h, "Mikan", "mikan"
        self.raw_title, self.download_url = f"[组] 剧场版 {h[:4]}", "http://x/t.torrent"
        self.release_time, self.priority = datetime.now(), 0


def test_stores_torrents_for_a_live_movie(clean_tables):
    with clean_tables.get_session() as s:
        m = Movie(title="片", quarter="26C")
        s.add(m)
        s.commit()
        s.refresh(m)
        mid = m.id
    assert M._store_movie_torrents(mid, [Item("a" * 40), Item("b" * 40)]) == 2


def test_skips_when_the_movie_vanished_mid_scan(clean_tables):
    """(R3) 这部片在抓种期间被合并掉了 → 本轮的版本一个都不入库，
    而不是挂成孤儿把 info_hash 占住。"""
    with clean_tables.get_session() as s:
        m = Movie(title="片", quarter="26C")
        s.add(m)
        s.commit()
        s.refresh(m)
        mid = m.id
        s.delete(m)                      # 模拟：await 期间被合并掉
        s.commit()
    assert M._store_movie_torrents(mid, [Item("a" * 40)]) == 0
    with clean_tables.get_session() as s:
        assert s.exec(select(MovieTorrent)).all() == []


def test_hash_is_deduped_globally(clean_tables):
    """同一个 hash 只入一次——这正是"孤儿会占住 hash"这条风险的来源。"""
    with clean_tables.get_session() as s:
        m1, m2 = Movie(title="甲", quarter="26C"), Movie(title="乙", quarter="26C")
        s.add(m1)
        s.add(m2)
        s.commit()
        s.refresh(m1)
        s.refresh(m2)
        id1, id2 = m1.id, m2.id
    assert M._store_movie_torrents(id1, [Item("c" * 40)]) == 1
    assert M._store_movie_torrents(id2, [Item("c" * 40)]) == 0
