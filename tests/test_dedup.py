"""集去重与集号折算的不变量。

这一组测的是全项目最容易"改一处漏一处"的判据：dedup_key / ambiguous_range / _foldable /
auto_downloadable_ep 被四条下载路径共用（flush、即时下、补下挑选、download_plan 标注），
历史上就是因为四处各写各的而出过"漏整整 12 集"和"同一集下两份"。
"""
import pytest

from sqlmodel import select

from core.anime import _foldable, ambiguous_range, auto_downloadable_ep, dedup_key


class A:
    """够用的 Anime 替身：这几个判据只读这两个字段。"""
    def __init__(self, ep_offset=None, total_episodes=None):
        self.ep_offset, self.total_episodes = ep_offset, total_episodes


# ---------------- auto_downloadable_ep ----------------

@pytest.mark.parametrize("ep,ok", [
    (1, True), (0, True), (12.5, True), (1170, True),
    (-1, False),   # 特别篇：多组多版本，自动挑一份常下错东西
    (-2, False),   # 未知集/疑似批量：一堆解析失败或整季合集包
])
def test_auto_downloadable_ep(ep, ok):
    assert auto_downloadable_ep(ep) is ok


# ---------------- _foldable / ambiguous_range ----------------

@pytest.mark.parametrize("off,total,foldable,amb", [
    # O ≥ T：两套编号取值域不相交，'ep > T' 判据完备 → 可折、无歧义段
    (24, 12, True, None),
    (12, 12, True, None),
    # O < T：取值域重叠 (O, T]，一条都不折，改成按 (集号,源) 去重
    (12, 24, False, (12, 24)),
    (1, 24, False, (1, 24)),
    # 数据不全 → 既不折也没有歧义段（绝不能拿 None 去做算术）
    (None, 24, False, None),
    (12, None, False, None),
    (None, None, False, None),
    (0, 24, False, None),
])
def test_fold_and_ambiguous(off, total, foldable, amb):
    a = A(off, total)
    assert _foldable(a) is foldable
    assert ambiguous_range(a) == amb


def test_foldable_and_ambiguous_are_mutually_exclusive():
    """同一部番不可能既"可折"又"有歧义段"——两者的判据必须互斥，否则集号会被两套规则轮流改写。"""
    for off in (None, 0, 1, 5, 12, 24, 100):
        for total in (None, 0, 1, 12, 24):
            a = A(off, total)
            assert not (_foldable(a) and ambiguous_range(a) is not None), (off, total)


# ---------------- dedup_key ----------------

def test_dedup_key_without_ambiguity_ignores_source():
    """无歧义段：同一 (番,集) 不论来自哪个源都是同一个键——这就是"同一集只下一份"。"""
    assert dedup_key(None, 1, 5, "ANi") == dedup_key(None, 1, 5, "Mikan") == (1, 5)


def test_dedup_key_inside_ambiguity_separates_sources():
    """歧义段内：绝对号 O+k 与季内号 O+k 写出来一样却是不同的两集，
    键必须带上源，否则先入库的源会把另一个源真正的那一集永久挡掉。"""
    amb = (12, 24)
    assert dedup_key(amb, 1, 13, "ANi") != dedup_key(amb, 1, 13, "Mikan")
    assert dedup_key(amb, 1, 13, "ANi") == (1, 13, "ANi")


@pytest.mark.parametrize("ep", [12, 24.0, 25, 1, -1, -2])
def test_dedup_key_outside_ambiguity_is_source_agnostic(ep):
    """边界闭合口径：歧义段是【左开右闭】(O, T]。O 本身与 T 之外都不带源。"""
    amb = (12, 24)
    inside = 12 < ep <= 24
    key = dedup_key(amb, 1, ep, "ANi")
    assert (len(key) == 3) is inside, (ep, key)


def test_dedup_key_none_source_is_stable():
    """source 为 None 与空串必须落同一个键，否则同一条种子会因为字段是 NULL 还是 '' 而命运不同。"""
    amb = (12, 24)
    assert dedup_key(amb, 1, 13, None) == dedup_key(amb, 1, 13, "")


def test_dedup_key_tolerates_non_numeric_episode():
    """集号异常时不能抛——它在四条下载路径的热路径上，抛一次就是整轮放行中断。"""
    assert dedup_key((12, 24), 1, None, "ANi") == (1, None)


def test_revive_uses_the_same_dedup_key_as_flush(clean_tables):
    """换源兜底的分组键必须与 flush 同口径（dedup_key），不能按 (番, 集)。

    歧义段（O<T 的番在 (O,T] 上两套编号重叠）里，同一个集号在不同源下指的是**不同的集**。
    按 (番,集) 分组时，B 源那条已 sent 的"第 13 集"会和 A 源真正失败的"第 13 集"算进同一组，
    组里出现 HANDLED ⇒ 判成"该集已有着落" ⇒ A 源的 skipped 兄弟永远不会被复活，
    那一集永久卡死在唯一失败源上（flush 与补下都只挑 pending/error，永不碰 skipped）。
    """
    from datetime import datetime

    from core import anime as A
    from db.models import Anime, AnimeTorrent

    with clean_tables.get_session() as s:
        a = Anime(title="歧义番", display_name="歧义番", confirmed=True,
                  ep_offset=12, total_episodes=24)
        s.add(a)
        s.commit()
        s.refresh(a)
        assert A.ambiguous_range(a) == (12, 24), "用例前提不成立：没有歧义段"
        now = datetime.now()
        s.add(AnimeTorrent(anime_id=a.id, info_hash="a" * 40, raw_title="[A源][13]",
                           episode=13, source="A源", status="error", created_at=now))
        s.add(AnimeTorrent(anime_id=a.id, info_hash="b" * 40, raw_title="[A源][13] 备用",
                           episode=13, source="A源", status="skipped", created_at=now))
        # B 源的"第 13 集"在歧义段上是【另一集】，不该给 A 源当挡箭牌
        s.add(AnimeTorrent(anime_id=a.id, info_hash="c" * 40, raw_title="[B源][13]",
                           episode=13, source="B源", status="sent", created_at=now))
        s.commit()

    A._revive_orphaned_skipped()

    with clean_tables.get_session() as s:
        revived = s.exec(select(AnimeTorrent).where(AnimeTorrent.info_hash == "b" * 40)).one()
    assert revived.status == "pending", "A 源的 skipped 兄弟没被复活——被 B 源的同号集掩蔽了"


def test_restore_uses_the_same_dedup_key_as_flush(clean_tables):
    """`restore_anime` 复活 skipped 兄弟时也要用 dedup_key（第六条消费路径）。

    歧义段里 A 源用绝对号发的『13』与 B 源用季内号发的『13』不是同一集。按裸集号算的话，
    A 源那条已下的挡住 B 源真正的第 13 集：那条 skipped 永远不复活，而 flush（只挑 pending）、
    批量补下、换源兜底（要组里有 error，而 reject 把 error 也压成了 skipped）三条路都不碰它
    ⇒ 那一集永久收不到，页面却弹绿色「已恢复到『订阅中』」。
    """
    from datetime import datetime

    from core import anime as A
    from db.models import Anime, AnimeTorrent

    with clean_tables.get_session() as s:
        a = Anime(title="歧义番", display_name="歧义番", confirmed=True, rejected=True,
                  ep_offset=12, total_episodes=24)
        s.add(a)
        s.commit()
        s.refresh(a)
        assert A.ambiguous_range(a) == (12, 24), "用例前提不成立：没有歧义段"
        now = datetime.now()
        s.add(AnimeTorrent(anime_id=a.id, info_hash="a" * 40, raw_title="[A源][13]",
                           episode=13, source="A源", status="sent", created_at=now))
        s.add(AnimeTorrent(anime_id=a.id, info_hash="b" * 40, raw_title="[B源][13]",
                           episode=13, source="B源", status="skipped", created_at=now))
        s.commit()
        aid = a.id

    A.restore_anime(aid)

    with clean_tables.get_session() as s:
        b = s.exec(select(AnimeTorrent).where(AnimeTorrent.info_hash == "b" * 40)).one()
    assert b.status == "pending", "B 源真正的第 13 集被 A 源的同号集挡住了，永远复活不了"
