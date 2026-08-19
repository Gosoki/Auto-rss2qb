"""集去重与集号折算的不变量。

这一组测的是全项目最容易"改一处漏一处"的判据：dedup_key / ambiguous_range / _foldable /
auto_downloadable_ep 被四条下载路径共用（flush、即时下、补下挑选、download_plan 标注），
历史上就是因为四处各写各的而出过"漏整整 12 集"和"同一集下两份"。
"""
import pytest

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
