"""下载候选的挑选顺序与每集去重（第 1 轮改动的回归网）。

pick_order 与 _download_candidates 决定"这一集下哪一份"。它们出错的表现是
"某集永远下不下来"或"同一集下了两份进同一目录"——两种都不会报错，只能靠用例守。
"""
from datetime import datetime, timedelta

import pytest

from core.anime import _download_candidates
from core.engine import pick_best, pick_order

T0 = datetime(2026, 1, 1)


class T:
    """够用的 AnimeTorrent 替身。算下载计划的真实路径喂进来的是列投影出的 Row，
    上面【没有 retry_count 列】——所以这里也提供一个不带该属性的变体（见 test_row_without_retry_count）。"""
    def __init__(self, tid, ep, source, priority, status="pending", retry_count=0, minutes=0):
        self.id, self.episode, self.source, self.priority = tid, ep, source, priority
        self.status, self.retry_count = status, retry_count
        self.created_at = T0 + timedelta(minutes=minutes)

    def __repr__(self):
        return f"T{self.id}({self.source},p{self.priority},{self.status})"


class RowLike:
    """模拟列投影出来的 Row：只有 _PLAN_COLS 里那八列，没有 retry_count。"""
    __slots__ = ("id", "episode", "source", "priority", "status", "created_at")

    def __init__(self, tid, ep, source, priority, status="pending", minutes=0):
        self.id, self.episode, self.source, self.priority = tid, ep, source, priority
        self.status, self.created_at = status, T0 + timedelta(minutes=minutes)


# ---------------- pick_order ----------------

def test_default_order_is_priority_then_age():
    rows = [T(1, 3, "A", 50, minutes=10), T(2, 3, "B", 90), T(3, 3, "C", 50)]
    assert [t.id for t in pick_order(rows)] == [2, 3, 1]


def test_pref_source_is_a_hard_lock_with_fallback():
    """钉了首选源就只看它；该源一条都没有时才退回全部（硬锁但不至于什么都下不了）。"""
    rows = [T(1, 3, "A", 90), T(2, 3, "B", 10)]
    assert pick_order(rows, pref="B")[0].id == 2
    assert pick_order(rows, pref="不存在的源")[0].id == 1


def test_prefer_fresh_pushes_failed_candidates_last():
    """(R1) 修前：该集优先级最高的那份坏了，每次都还挑它、每次都失败，
    同集另一个源的健康 pending 兄弟永远轮不到 → 该集永久停滞。"""
    broken = T(1, 3, "A", 90, status="error", retry_count=3)
    healthy = T(2, 3, "B", 50)
    assert pick_order([broken, healthy], prefer_fresh=True)[0].id == 2
    assert pick_order([broken, healthy], prefer_fresh=True)[1].id == 1   # 仍在候选里，只是排后面


def test_prefer_fresh_keeps_priority_among_equally_fresh():
    """没失败过的之间，排序口径不变（优先级降序、入库时间升序）——别把正常情况也改了。"""
    rows = [T(1, 3, "A", 50), T(2, 3, "B", 90), T(3, 3, "C", 50, minutes=-5)]
    assert [t.id for t in pick_order(rows, prefer_fresh=True)] == [2, 3, 1]


def test_retried_but_pending_ranks_below_never_tried():
    """重试过但已回到 pending 的，排在从没试过的后面——同样是"先试没坏过的那份"。"""
    retried = T(1, 3, "A", 90, status="pending", retry_count=2)
    fresh = T(2, 3, "B", 90)
    assert pick_order([retried, fresh], prefer_fresh=True)[0].id == 2


def test_default_path_is_unchanged_by_the_new_flag():
    """后台 flush 走的是默认口径（只从 pending 里挑）——第 1 轮的改动不能碰它。"""
    broken = T(1, 3, "A", 90, status="error")
    healthy = T(2, 3, "B", 50)
    assert pick_best([broken, healthy]).id == 1


def test_row_without_retry_count_does_not_raise():
    """算下载计划的路径喂进来的 Row 没有 retry_count 列。取不到要当 0（等价于"没失败过"），
    绝不能抛 —— 那会让仪表盘整块打挂。"""
    rows = [RowLike(1, 3, "A", 50), RowLike(2, 3, "B", 90)]
    assert [t.id for t in pick_order(rows, prefer_fresh=True)] == [2, 1]


def test_empty_candidates_returns_empty_not_error():
    assert pick_order([]) == []


# ---------------- _download_candidates ----------------

def test_one_group_per_episode_ordered():
    """(R1) 返回的是【每集一串有序候选】而不是每集一条：
    执行侧逐条试到成功，标注侧取 [0] —— 两侧看到的第一条必须是同一条。"""
    rows = [T(1, 3, "A", 90, status="error"), T(2, 3, "B", 50),
            T(3, 4, "A", 90), T(4, 4, "B", 50)]
    groups = _download_candidates(rows)
    assert len(groups) == 2
    by_first = {g[0].id: [t.id for t in g] for g in groups}
    assert by_first == {2: [2, 1], 3: [3, 4]}


def test_have_eps_skips_the_whole_episode():
    """已经有一份的集整组跳过——这是"同一集只下一份"的闸。"""
    rows = [T(1, 3, "A", 90), T(2, 4, "A", 90)]
    groups = _download_candidates(rows, have_eps={(3,)})
    assert [g[0].episode for g in groups] == [4]


@pytest.mark.parametrize("ep", [-1, -2])
def test_specials_and_unknown_are_never_auto_selected(ep):
    """特别篇(-1)与未知集/疑似批量(-2)一律不自动下，要人工对准那一条点『下载』。"""
    assert _download_candidates([T(1, ep, "A", 90)]) == []


def test_ambiguous_range_splits_by_source():
    """歧义段内按 (集号,源) 分组：同一个写法的 13 在两个源那里是不同的两集，
    必须各给一组，否则先入库的源会把另一个源真正的第 13 集永久挡掉。"""
    rows = [T(1, 13, "ANi", 90), T(2, 13, "Mikan", 50)]
    groups = _download_candidates(rows, amb=(12, 24))
    assert len(groups) == 2
    assert sorted(g[0].id for g in groups) == [1, 2]
    # 无歧义段时同一集只有一组
    assert len(_download_candidates(rows, amb=None)) == 1


def test_pref_source_applies_within_each_episode():
    rows = [T(1, 3, "A", 90), T(2, 3, "B", 10), T(3, 4, "A", 90), T(4, 4, "B", 10)]
    groups = _download_candidates(rows, pref="B")
    assert sorted(g[0].id for g in groups) == [2, 4]


# ---------------- 三态在候选循环里的语义（R2 回归网） ----------------
#
# 【这一组必须调【产品函数本身】】早先它是在本文件里手抄了一份内层循环再测那份副本——
# 那样测的是"我抄的这段对不对"，产品代码改坏了它一声不吭。第 3 轮审计点名了这个问题。


import pytest as _pytest
from datetime import datetime as _dt

from core import anime as A, engine as E
from db.models import Anime, AnimeTorrent


@_pytest.fixture
def one_anime(clean_tables):
    """一部已确认的番 + 第 3 集的两个候选（A 源优先级高、B 源低）。返回 (anime_id, {源: 种子id})。"""
    with clean_tables.get_session() as s:
        a = Anime(title="番", season=1, confirmed=True, quarter="26C")
        s.add(a)
        s.commit()
        s.refresh(a)
        ids = {}
        for src, pri, h in (("A", 90, "a"), ("B", 50, "b")):
            t = AnimeTorrent(anime_id=a.id, info_hash=h * 40, raw_title=f"[{src}] 番 - 03 [1080p]",
                             episode=3, status="pending", source=src, priority=pri,
                             created_at=_dt.now())
            s.add(t)
            s.commit()
            s.refresh(t)
            ids[src] = t.id
        return a.id, ids


@_pytest.fixture
def dl_results(monkeypatch):
    """把产品里的 download_anime_torrent 换成可编排的假实现，记录被尝试过的种子 id。
    qb.reachable 也一并放行（两个批量入口都有预检）。"""
    tried = []

    async def reachable():
        return True
    monkeypatch.setattr(E.qb, "reachable", reachable)

    def _set(results: dict):
        async def fake(tid, force=False):
            tried.append(tid)
            return results.get(tid, False)
        monkeypatch.setattr(A, "download_anime_torrent", fake)
        return tried
    return _set


async def test_torrent_specific_failure_falls_through_to_the_sibling(one_anime, dl_results):
    """这一条自己的毛病（坏种子）→ 换同集的下一个源，这正是"逐条试到成功"的初衷。"""
    aid, ids = one_anime
    tried = dl_results({ids["A"]: False, ids["B"]: True})
    assert await A.download_pending_for_anime(aid) == 1
    assert tried == [ids["A"], ids["B"]]


async def test_systemic_failure_stops_immediately(one_anime, dl_results):
    """(R2) qB 连不上 / 保存路径算不出来 → 当场收手。
    换个 hash 再试只会让同一集两份都进 qB，或把该集候选全烧成 error 而 flush 只挑 pending。"""
    aid, ids = one_anime
    tried = dl_results({ids["A"]: None, ids["B"]: True})
    assert await A.download_pending_for_anime(aid) == 0
    assert tried == [ids["A"]], "系统性失败后不该再试同集的第二个候选"


async def test_batch_entrypoint_has_the_same_semantics(one_anime, dl_results):
    """download_all_pending 是另一个批量入口，必须与逐番版同口径。"""
    aid, ids = one_anime
    tried = dl_results({ids["A"]: None})
    assert await A.download_all_pending() == 0
    assert tried == [ids["A"]]


async def test_batch_entrypoints_precheck_qb(one_anime, monkeypatch, cfg):
    """(R2) 两个批量入口都要有 flush 同款的 qB 预检——少了它，qB 掉线时这个按钮会把
    整批种子挨个跑一遍网络（每条都去源站取种、再发给一个根本不在的 qB）。

    预检的前提是 QB_ENABLED（默认 False，那时 download_anime_torrent 自己会立刻返回，
    不会打网络），所以这里要显式打开才测得到那条闸。"""
    cfg(QB_ENABLED=True)
    aid, _ = one_anime
    called = []

    async def unreachable():
        called.append(1)
        return False

    async def should_not_run(tid, force=False):
        raise AssertionError("qB 连不上时不该真去下载")
    monkeypatch.setattr(E.qb, "reachable", unreachable)
    monkeypatch.setattr(A, "download_anime_torrent", should_not_run)
    assert await A.download_pending_for_anime(aid) == 0
    assert await A.download_all_pending() == 0
    assert len(called) == 2


async def test_success_stops_trying_the_rest(one_anime, dl_results):
    """第一条就成功时不该再碰同集的其它候选——那会让同一集下两份。"""
    aid, ids = one_anime
    tried = dl_results({ids["A"]: True, ids["B"]: True})
    assert await A.download_pending_for_anime(aid) == 1
    assert tried == [ids["A"]]
