"""(R24) `_refold_absolute_episodes` —— 全项目唯一一处【批量原地改写集去重键】的代码。

它在两个时机各跑一次：学到 `ep_offset` 的那一刻、以及 bgm 元数据回写之后，
一次扫该番的所有种子行。两条分支完全相反：
  · 可折番（O ≥ T）：把绝对域 (T, O+T] 内的行减掉 offset；
  · 不可折番（O < T）：把【旧规则误折过的行】加回去（存量库回滚）。

DECISIONS E-29 与 benchmark 文档明确记着算错的代价：
「在 #6/#60 上把真实正片集误折成已交付键、造成**不可逆漏集**」。
而它和它的幂等锚 `_orig_episode` 在 R23 之前是**零覆盖**的 ——
一个会批量改写去重键、且文档记着"写错不可逆"的函数，没有任何用例。

它的 docstring 还声称「判据与 `_learn_and_normalize_episode` 的 ② 完全一致」——
同一个决定写在两处（第①号形状），而把两者绑在一起的**只有这句注释**。
"""
import pytest
from sqlmodel import select

import core.anime as A
from db.models import Anime, AnimeTorrent


def _mk(session, *, total, offset, rows):
    """建一部番 + 若干条种子。rows = [(raw_title, episode), ...]"""
    a = Anime(title="测试番", season=2, quarter="26C", confirmed=True,
              total_episodes=total, ep_offset=offset)
    session.add(a)
    session.commit()
    session.refresh(a)
    for i, (raw, ep) in enumerate(rows):
        session.add(AnimeTorrent(anime_id=a.id, info_hash=f"{i:040x}", raw_title=raw,
                                 source="ANi", season=2, episode=ep, status="pending"))
    session.commit()
    return a.id


def _eps(session):
    return sorted((t.raw_title, t.episode) for t in session.exec(select(AnimeTorrent)))


def test_foldable_series_folds_only_the_absolute_range(clean_tables):
    """O ≥ T 的番：只折 (T, O+T] 里的行，季内号一条都不碰。

    O=13、T=13 → 绝对域是 [14, 26]、季内域是 [1, 13]，两者不相交，`ep > T` 这个判据才完备。
    """
    with clean_tables.get_session() as s:
        aid = _mk(s, total=13, offset=13, rows=[
            ("[ANi] 某番 - 16 [1080P]", 16.0),   # 绝对号 → 折成 3
            ("[ANi] 某番 - 26 [1080P]", 26.0),   # 绝对域右边界（含）→ 折成 13
            ("[ANi] 某番 - 14 [1080P]", 14.0),   # 绝对域左边界（T+1）→ 折成 1
            ("[Sub] 某番 - 03 [1080P]", 3.0),    # 季内号 → 不动
            ("[Sub] 某番 - 13 [1080P]", 13.0),   # 季内域右边界 → 不动
            ("[Sub] 某番 [SP] [1080P]", -1.0),   # 特别篇 → 不动
            ("[Sub] 某番 合集 [1080P]", -2.0),   # 未知集 → 不动
        ])
        a = s.get(Anime, aid)
        assert A._foldable(a) is True, "前提：O ≥ T 才可折"
        n = A._refold_absolute_episodes(s, a)
        s.commit()
    assert n == 3, f"应折 3 条，实际 {n}"
    with clean_tables.get_session() as s:
        got = dict(_eps(s))
    assert got["[ANi] 某番 - 16 [1080P]"] == 3.0
    assert got["[ANi] 某番 - 26 [1080P]"] == 13.0
    assert got["[ANi] 某番 - 14 [1080P]"] == 1.0
    assert got["[Sub] 某番 - 03 [1080P]"] == 3.0, "季内号被误折了"
    assert got["[Sub] 某番 - 13 [1080P]"] == 13.0, "季内域右边界被误折了"
    assert got["[Sub] 某番 [SP] [1080P]"] == -1.0 and got["[Sub] 某番 合集 [1080P]"] == -2.0


def test_folding_is_idempotent(clean_tables):
    """连调两次，第二次必须一条都不动。

    幂等锚是 `_orig_episode`（从 raw_title 复算标题原值）：只动"集号还等于标题原值"的行。
    没有它的话，bgm 改一次 `total_episodes` 触发再次回折就会**再折一遍**，
    16 → 3 → 折不动了（3 不在绝对域），但 26 → 13 → 13 仍在季内域边界…
    真正会连折的是 offset 更小的番。这条把锚钉死。
    """
    with clean_tables.get_session() as s:
        aid = _mk(s, total=13, offset=13, rows=[("[ANi] 某番 - 16 [1080P]", 16.0)])
        a = s.get(Anime, aid)
        assert A._refold_absolute_episodes(s, a) == 1
        s.commit()
    with clean_tables.get_session() as s:
        a = s.get(Anime, aid)
        assert A._refold_absolute_episodes(s, a) == 0, "第二次又折了 —— 幂等锚没锚住"
        s.commit()
    with clean_tables.get_session() as s:
        assert dict(_eps(s))["[ANi] 某番 - 16 [1080P]"] == 3.0


def test_a_hand_edited_episode_is_never_pushed_back(clean_tables):
    """用户在详情页手工改过的集号不许被折算推回去。

    判据是 `_orig_episode`：手工改过之后 `episode != 标题原值`，锚不成立 → 跳过。

    ⚠️ 取值必须**仍落在绝对域内**（这里 T=13、O=13 → 绝对域 (13, 26]，人工改成 20）。
    第一版改成了 7 —— 那个值根本进不了折算的 if（`13 < 7` 为假），
    于是拿掉整个锚，用例照样全绿（实测）。**用例走不到的分支，等于没测。**
    """
    with clean_tables.get_session() as s:
        aid = _mk(s, total=13, offset=13, rows=[("[ANi] 某番 - 16 [1080P]", 16.0)])
        t = s.exec(select(AnimeTorrent)).one()
        t.episode = 20.0                     # 人工改成 20：仍在绝对域 (13, 26] 内
        s.add(t)
        s.commit()
        a = s.get(Anime, aid)
        assert A._refold_absolute_episodes(s, a) == 0, \
            "人工改过的行被折了 —— 幂等锚没起作用"
        s.commit()
    with clean_tables.get_session() as s:
        assert dict(_eps(s))["[ANi] 某番 - 16 [1080P]"] == 20.0, "人工改的集号被折算推回去了"


def test_a_title_with_both_numbers_is_left_alone(clean_tables):
    """标题自带双编号（`03(16)`）的行，那个 ep 已经是季内号 —— 再折就错了。

    守卫与 `_learn_and_normalize_episode` 的同一条（`extract_episode_abs`）。
    """
    with clean_tables.get_session() as s:
        # 双编号写法：季内 16、绝对 29（parse 出来的是 16，落在绝对域里，若无守卫就会被折成 3）
        aid = _mk(s, total=13, offset=13, rows=[("[LoliHouse] 某番 - 16(29) [1080p]", 16.0)])
        a = s.get(Anime, aid)
        n = A._refold_absolute_episodes(s, a)
        s.commit()
    assert n == 0, "标题自带双编号的行被折了 —— 那个集号已经是季内号"


def test_unfoldable_series_unwinds_rows_the_old_rule_mis_folded(clean_tables):
    """O < T 的番：不折新行，而且要把【旧规则误折过的】展开回去。

    旧规则无条件按 `ep > T` 折，在 O < T 时只折得动 (T, O+T] 那一段、[O+1, T] 那段折不动 ——
    于是**同一个源**的前半季（未折）与后半季（已折）双双落在键 [O+1, T] 上，
    两批内容不同的集撞成同一个去重键，flush 每键只放行一份，**静默漏掉半季**。
    升级后若不展开回去，老库的行为与升级前一模一样。
    """
    with clean_tables.get_session() as s:
        # O=5 < T=12：旧规则把绝对号 15（>12）折成了 10，标题原值 15 == 10 + 5 → 认得出来
        aid = _mk(s, total=12, offset=5, rows=[
            ("[ANi] 某番 - 15 [1080P]", 10.0),   # 旧规则折过的 → 展开回 15
            ("[ANi] 某番 - 08 [1080P]", 8.0),    # 没折过（原值就等于现值）→ 不动
        ])
        a = s.get(Anime, aid)
        assert A._foldable(a) is False, "前提：O < T 时一条都不折"
        n = A._refold_absolute_episodes(s, a)
        s.commit()
    assert n == 1, f"应展开 1 条，实际 {n}"
    with clean_tables.get_session() as s:
        got = dict(_eps(s))
    assert got["[ANi] 某番 - 15 [1080P]"] == 15.0, "旧规则误折的行没被展开"
    assert got["[ANi] 某番 - 08 [1080P]"] == 8.0, "没折过的行被动了"


def test_unwinding_is_idempotent(clean_tables):
    """展开之后 orig == ep，再跑一次天然不动（docstring 里承诺的那句）。"""
    with clean_tables.get_session() as s:
        aid = _mk(s, total=12, offset=5, rows=[("[ANi] 某番 - 15 [1080P]", 10.0)])
        a = s.get(Anime, aid)
        assert A._refold_absolute_episodes(s, a) == 1
        s.commit()
    with clean_tables.get_session() as s:
        a = s.get(Anime, aid)
        assert A._refold_absolute_episodes(s, a) == 0, "展开之后又动了一次"
        s.commit()


@pytest.mark.parametrize("total,offset,foldable", [
    (13, 13, True),    # O == T：边界，可折
    (13, 14, True),    # O > T
    (13, 12, False),   # O < T：一条都不折
    (13, 0, False),    # 没学到 offset
    (None, 13, False),  # bgm 没给 total
])
def test_the_foldability_gate(total, offset, foldable):
    """`_foldable` 的判据是 O ≥ T（两个取值域不相交）—— 边界逐个钉住。

    这条判据算错的代价文档里写着：把真实正片集误折成已交付键，**不可逆漏集**。
    """
    a = Anime(title="x", season=2, total_episodes=total, ep_offset=offset)
    assert A._foldable(a) is foldable


@pytest.mark.parametrize("total,offset", [(13, 13), (13, 14), (13, 12), (12, 5), (24, 24)])
@pytest.mark.parametrize("ep", [1.0, 3.0, 12.0, 13.0, 14.0, 16.0, 26.0, 27.0])
def test_both_folding_sites_agree_on_every_input(total, offset, ep, clean_tables):
    """`_refold_absolute_episodes` 的 docstring 声称「判据与 `_learn_and_normalize_episode`
    的 ② **完全一致**」—— 而把两者绑在一起的**只有这句注释**（第①号形状）。

    两处是**同一个决定的两种写法**：入库那条用 `item.episode_abs`（解析时就带着的字段）
    挡双编号，回折那条用 `extract_episode_abs(t.raw_title)`（从标题复算）。
    所以不能按"调了哪个函数"去比 —— 那正是我第一版写错的地方。
    **要比的是行为**：同一批 (T, O, ep) 上，两条路必须给出同一个集号。

    这条网住的是"以后有人只改了其中一处"：入库的新种子按新判据折、
    存量行按旧判据折，同一集落在两个键上，各下一份到同一个目录。
    """
    from types import SimpleNamespace

    raw = f"[ANi] 某番 - {int(ep):02d} [1080P]"
    with clean_tables.get_session() as s:
        aid = _mk(s, total=total, offset=offset, rows=[(raw, ep)])
        a = s.get(Anime, aid)
        # 路径 A：入库归一（不带双编号）
        item = SimpleNamespace(episode=ep, episode_abs=None, raw_title=raw)
        via_ingest = A._learn_and_normalize_episode(s, a, item)
        s.rollback()

    with clean_tables.get_session() as s:
        a = s.get(Anime, aid)
        A._refold_absolute_episodes(s, a)
        s.commit()
    with clean_tables.get_session() as s:
        via_refold = s.exec(select(AnimeTorrent)).one().episode

    assert via_ingest == via_refold, (
        f"T={total} O={offset} ep={ep}：入库归一给 {via_ingest}，存量回折给 {via_refold} —— "
        "两处判据已经分家，同一集会落在两个去重键上")
