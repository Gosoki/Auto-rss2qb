"""下载计划的等价性：批量口径必须与逐番口径逐字相等。

这一组守的是"为了快而改写查询"这类改动——它们最容易在某个边角上悄悄改变结果，
而结果错了的表现只是"某条种子的『将下载』徽标标反了"，没人会立刻发现。
"""
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from core import anime as A
from db.models import Anime, AnimeTorrent


@pytest.fixture
def library(clean_tables):
    """造一个覆盖各种边角的小库：多源、多状态、锁源、版本关键词、歧义段、特别篇、已忽略番。"""
    now = datetime.now()
    with clean_tables.get_session() as s:
        specs = [
            dict(title="普通番", confirmed=True),
            dict(title="锁源番", confirmed=True, pref_source="ANi"),
            dict(title="锁版本番", confirmed=True, pref_keyword="繁日"),
            dict(title="歧义段番", confirmed=True, ep_offset=12, total_episodes=24),
            dict(title="可折番", confirmed=True, ep_offset=24, total_episodes=12),
            dict(title="已忽略番", confirmed=True, rejected=True),
            dict(title="未确认番", confirmed=False),
        ]
        animes = []
        for sp in specs:
            a = Anime(season=1, quarter="26C", **sp)
            s.add(a)
            animes.append(a)
        s.commit()
        for a in animes:
            s.refresh(a)
        k = 0
        for a in animes:
            for ep, status, src, pri, kw in [
                (1, "sent", "ANi", 50, "繁日"), (1, "pending", "Mikan", 90, "简日"),
                (2, "pending", "ANi", 50, "繁日"), (2, "pending", "Mikan", 90, "简日"),
                (3, "error", "ANi", 90, "繁日"), (3, "pending", "Mikan", 10, "简日"),
                (4, "stalled", "ANi", 50, "繁日"), (4, "pending", "Mikan", 90, "简日"),
                (5, "deleted", "ANi", 50, "繁日"), (5, "pending", "Mikan", 90, "简日"),
                (13, "pending", "ANi", 50, "繁日"), (13, "pending", "Mikan", 90, "简日"),
                (-1, "pending", "ANi", 50, "繁日"), (-2, "pending", "ANi", 50, "繁日"),
                (6, "skipped", "ANi", 50, "繁日"), (7, "excluded", "ANi", 50, "繁日"),
            ]:
                k += 1
                s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{k:040x}",
                                   raw_title=f"[{src}] {a.title} - {ep} [1080p][{kw}]",
                                   episode=ep, status=status, source=src, priority=pri,
                                   created_at=now - timedelta(minutes=k)))
        s.commit()
        return [a.id for a in animes]


@pytest.mark.parametrize("for_backfill", [False, True])
def test_batch_matches_per_anime(library, for_backfill):
    """批量版 = 对每个 id 调逐番版求并。两者分家过一次（口径不一致 → 页面标注与实际相反）。"""
    batch = A.download_plan_for_ids(library, for_backfill=for_backfill)
    one_by_one = set()
    for aid in library:
        one_by_one |= A.download_plan(aid, for_backfill=for_backfill)
    assert batch == one_by_one


def test_combined_matches_two_separate_calls(library):
    """(R3) 合成一次算两种口径，结果必须与分别调两次逐字相等——这是那次提速改动的全部约束。"""
    p_auto, p_backfill = A.download_plans_for_ids(library)
    assert p_auto == A.download_plan_for_ids(library)
    assert p_backfill == A.download_plan_for_ids(library, for_backfill=True)


def test_auto_plan_never_includes_error_rows(library, clean_tables):
    """自动口径的候选只含 pending：后台 flush 有意不重试 error，
    把 error 也算候选会让高优先级的 error 顶掉同集真正会被自动下的 pending，页面就标反了。"""
    plan = A.download_plan_for_ids(library)
    with clean_tables.get_session() as s:
        errs = {t.id for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.status == "error"))}
    assert not (plan & errs)


def test_rejected_anime_are_excluded(library, clean_tables):
    """已忽略的番一条都不会被下——执行侧要求 `confirmed and not rejected`，
    这里若不排除就会把它的待下标成『将下载』，而实际点下载返回 0 集（页面撒谎）。"""
    plan = A.download_plan_for_ids(library)
    with clean_tables.get_session() as s:
        rej = {a.id for a in s.exec(select(Anime).where(Anime.title == "已忽略番"))}
        rej_t = {t.id for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id.in_(rej)))}
    assert not (plan & rej_t)


def test_unconfirmed_filtering_is_the_callers_job(library, clean_tables):
    """【这是契约，不是 bug——别"顺手修"它】

    计划函数自己只排除 rejected，【不】排除 confirmed=False。"未确认的番不算"这条闸
    在【调用方】：pages/anime.py 先过 confirmed_anime_ids，pending_breakdown 先按 conf 筛。
    直接把未确认的 id 喂进来，它的待下就会被标成『将下载』——而那要点确认才会下。

    把闸下沉进函数本身对现有三个调用方都是 no-op（它们已经先过滤了），能堵住这个坑，
    但会改变详情页对未确认番的显示，属需拍板项（见 docs/audit-2026-08-r3.md）。
    这条用例的作用是：把当前契约写死，谁改了行为都会在这里红一次、被迫看到上面这段。
    """
    with clean_tables.get_session() as s:
        un = {a.id for a in s.exec(select(Anime).where(Anime.title == "未确认番"))}
        un_t = {t.id for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id.in_(un)))}
    assert A.download_plan_for_ids(un) & un_t, "当前契约：函数自己不过滤 confirmed"
    # 而调用方按约定先过滤之后，结果里就没有它们了
    confirmed_only = [i for i in library if i not in un]
    assert not (A.download_plan_for_ids(confirmed_only) & un_t)


def test_empty_input_is_cheap_and_empty(library):
    assert A.download_plan_for_ids([]) == set()
    assert A.download_plan_for_ids([None, 0]) == set()
    assert A.download_plans_for_ids([]) == (set(), set())


def test_all_three_pick_paths_agree_on_a_retried_candidate(clean_tables, cfg):
    """标注 / 补下 / 后台 flush 三条挑选路径必须挑同一条（D-08）。

    造一条"优先级高但已经失败重试过"的 pending 与一条"优先级低但干净"的兄弟。
    改造前：补下侧四键排序会挑干净的那条，而标注侧的列投影里没有 retry_count、
    排序退化成两键，于是详情页把『将下载』标在高优先级那条上——两边指着不同的种子，
    用户点补下下到的和徽标说的不是一回事。
    """
    now = datetime.now()
    with clean_tables.get_session() as s:
        a = Anime(title="口径番", display_name="口径番", confirmed=True)
        s.add(a)
        s.commit()
        s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="a" * 40, raw_title="[高优先][01]",
                           episode=1, source="高优先", priority=9, status="pending",
                           retry_count=3, created_at=now - timedelta(hours=2)))
        s.add(AnimeTorrent(anime_id=a.id, info_hash="b" * 40, raw_title="[低优先][01]",
                           episode=1, source="低优先", priority=1, status="pending",
                           created_at=now - timedelta(hours=1)))
        s.commit()
        aid = a.id

    # ① 标注侧（列投影）
    plan_ids, backfill_ids = A.download_plans_for_ids([aid])
    # ② 补下侧（整行 ORM）
    with clean_tables.get_session() as s:
        rows = list(s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid)))
        cands = A._download_candidates(rows, None, set(), None)
        backfill_first = cands[0][0].id
    # ③ 后台 flush 侧——必须跑【真正的 flush】。直接调 pick_best(prefer_fresh=True) 是在测
    #    自己传进去的参数，flush 那一行改回去也照样绿，等于什么都没钉住。
    import asyncio

    import pytest as _pytest
    picked = []

    async def _fake_download(tid):
        picked.append(tid)
        return True

    mp = _pytest.MonkeyPatch()
    mp.setattr(A, "download_anime_torrent", _fake_download)
    cfg(ANIME_DOWNLOAD_GRACE_MIN=0)                  # 别等缓冲窗口
    try:
        asyncio.run(A.flush_ready_downloads())
    finally:
        mp.undo()
    assert len(picked) == 1, f"flush 没挑或挑了多条：{picked}"
    flush_first = picked[0]

    assert len(plan_ids) == 1
    marked = next(iter(plan_ids))
    assert marked == backfill_first == flush_first, (
        f"三条路径挑了不同的种子：标注={marked} 补下={backfill_first} flush={flush_first}")
    with clean_tables.get_session() as s:
        assert s.get(AnimeTorrent, marked).source == "低优先", "失败过 3 次的那条仍被优先挑中"


def test_plan_respects_the_retry_backoff_like_flush_does(clean_tables, cfg):
    """标注侧也要看 retry_at——否则徽标指的种子和后台真下的不是同一条（P2-3）。

    同一集两条 pending 且【都失败重试过】：A 优先级高但退避到 3 小时后，B 优先级低但已到点。
    这不是构造出来的边角：`_fail` 同时写 status=pending 与 retry_at，只有成功才清零，
    所以「pending 且 retry_count>0」必然带 retry_at。
    改动前 flush 下 B、标注标 A，而 D-08 那条用例两条种子的 retry_at 都是 None，正好绕开这一维。
    """
    now = datetime.now()
    with clean_tables.get_session() as s:
        a = Anime(title="退避番", display_name="退避番", confirmed=True)
        s.add(a)
        s.commit()
        s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="e" * 40, raw_title="[高优先][01]",
                           episode=1, source="高优先", priority=9, status="pending",
                           retry_count=3, retry_at=now + timedelta(hours=3),
                           created_at=now - timedelta(hours=2)))
        s.add(AnimeTorrent(anime_id=a.id, info_hash="f" * 40, raw_title="[低优先][01]",
                           episode=1, source="低优先", priority=1, status="pending",
                           retry_count=1, retry_at=now - timedelta(minutes=1),
                           created_at=now - timedelta(hours=1)))
        s.commit()
        aid = a.id

    plan_ids, _ = A.download_plans_for_ids([aid])
    single = A.download_plan(aid)
    with clean_tables.get_session() as s:
        marked = {s.get(AnimeTorrent, tid).source for tid in plan_ids}
        marked_single = {s.get(AnimeTorrent, tid).source for tid in single}
    assert marked == {"低优先"}, f"批量标注挑了还在退避里的那条：{marked}"
    assert marked_single == {"低优先"}, f"单番标注挑了还在退避里的那条：{marked_single}"
