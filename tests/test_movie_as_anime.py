"""(R30) 同一个 bgm subject 同时是一部番和一部剧场版。

【怎么发现的】不是读代码，是**拿真库反查不变量**：把注释里写死的那些"应该成立的话"
逐条写成 SQL 去真数据上验，发现 `info_hash` 在两张种子表里各出现一次 ——
而两张表各有唯一约束、**跨表没有**。顺着查到根因就是这条。

真库正好 2 对，且恰好就是 anime 表里仅有的两条 `platform='剧场版'`。
"""
import pytest


def test_detects_a_subject_that_exists_in_both_tables(clean_tables):
    from core import anime as A
    from db.models import Anime, Movie

    with clean_tables.get_session() as s:
        s.add(Anime(title="剧场版 某片", display_name="剧场版 某片", bangumi_id=501958,
                    platform="剧场版", confirmed=False, rejected=True, quarter="25C"))
        s.add(Movie(title="剧场版 某片", display_name="剧场版 某片", bangumi_id=501958,
                    quarter="25C"))
        s.commit()

    hits = A.suspect_movie_as_anime()
    assert len(hits) == 1, f"没报出来：{hits}"
    assert hits[0]["bgm"] == 501958
    assert hits[0]["a_state"] == "超期忽略/待确认"


def test_a_normal_tv_anime_is_not_reported(clean_tables):
    """反向：番剧与剧场版各管各的，别把不相干的两条报成一对。"""
    from core import anime as A
    from db.models import Anime, Movie

    with clean_tables.get_session() as s:
        s.add(Anime(title="某番", bangumi_id=111, platform="TV", confirmed=True, quarter="26C"))
        s.add(Movie(title="某片", bangumi_id=222, quarter="26C"))
        s.commit()
    assert A.suspect_movie_as_anime() == []


def test_it_reports_and_never_changes_state(clean_tables):
    """只报不改 —— 与另外两条 `suspect_*` 同一条纪律。

    程序判不出该留哪一条（剧场版那条通常才是对的，但番剧那条底下可能已经下过东西）。
    """
    from core import anime as A
    from db.models import Anime, Movie

    with clean_tables.get_session() as s:
        a = Anime(title="剧场版 某片", bangumi_id=333, platform="剧场版",
                  confirmed=True, rejected=False, quarter="26C")
        s.add(a); s.add(Movie(title="剧场版 某片", bangumi_id=333, quarter="26C"))
        s.commit(); s.refresh(a)
        aid = a.id

    assert len(A.suspect_movie_as_anime()) == 1
    with clean_tables.get_session() as s:
        got = s.get(Anime, aid)
    assert got.confirmed is True and got.rejected is not True, \
        "检测函数改了状态 —— 它必须是只读的"


@pytest.mark.parametrize("n", [1, 30])
def test_the_scan_is_one_pass_not_n_plus_one(clean_tables, n):
    """查询次数不能随番数增长 —— 它挂在仪表盘的同步构建路径上。

    与 R27 给 `suspect_wrong_binding` 补的那条同一个理由：每次渲染 + 每 30 秒的
    定时刷新各跑一遍，N+1 会把事件循环钉住。
    """
    import sqlalchemy as sa

    from core import anime as A
    from db.models import Anime, Movie

    with clean_tables.get_session() as s:
        for i in range(n):
            s.add(Anime(title=f"番{i}", bangumi_id=9000 + i, confirmed=True, quarter="26C"))
        s.add(Movie(title="片", bangumi_id=9000, quarter="26C"))
        s.commit()

    cnt = [0]

    def _tick(*a, **k):
        cnt[0] += 1
    sa.event.listen(clean_tables.engine, "before_cursor_execute", _tick)
    try:
        assert len(A.suspect_movie_as_anime()) == 1
    finally:
        sa.event.remove(clean_tables.engine, "before_cursor_execute", _tick)
    # 命中 1 条时会为那一条各数一次两张种子表 → 常数条，不随 n 涨
    assert cnt[0] <= 5, f"{n} 部番发了 {cnt[0]} 条 SQL —— 随番数涨就是 N+1"


@pytest.mark.nicegui_main_file("tests/render_main.py")
async def test_the_dashboard_actually_shows_the_warning(user, clean_tables):
    """(R30) 仪表盘必须真的把这条报出来 —— 光有检测函数等于没有。

    上一轮（R29）刚栽在同一个形状上：`archive_round()` 的锁有守卫、
    "生产驱动者会不会走它"没有，把驱动者换回裸调用全量一条红都没有。
    所以凡是"加了一个检测/一道约束"，都要补一条**驱动者级**用例。

    这里把检测的两个出口都钉住：函数报了、**且**首页横幅上看得见。
    """
    from db.models import Anime, Movie

    with clean_tables.get_session() as s:
        s.add(Anime(title="剧场版 某片", display_name="剧场版 某片", bangumi_id=501958,
                    platform="剧场版", confirmed=False, rejected=True, quarter="25C"))
        s.add(Movie(title="剧场版 某片", display_name="剧场版 某片", bangumi_id=501958,
                    quarter="25C"))
        s.commit()

    await user.open("/?t=overview")
    # 【断在短片段上】`current_layout` 的 dump 会截断长文本（R29 踩过），
    # 所以断言落在横幅开头那一小截，而不是整句话。
    await user.should_see("在番剧表")
