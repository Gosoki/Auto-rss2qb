"""(R31) 剧场版一律按【年】展示，季母对它没有意义。

E-30（2026-09-01 拍板）把 `movie_quarter_of` 钉成恒返回 `<yy>A`，
可**消费侧**一直按季度键分组：真库 6 个年份被劈成 **15 根柱**
（例：`25D 9 / 25C 9 / 25B 5 / 25A 3` 全是 2025 年），
而今后新入库的片一律落进 `<yy>A` —— 同一年的新旧片分在不同组里。

这一组把三个消费点钉住：仪表盘分布、列表分组、详情页那一栏的名字。
（存量季母的**数据**回填是另一件事，见 E-47；显示这一半不该等它。）
"""
import pytest

_MAIN = "tests/render_main.py"


def test_overview_groups_movies_by_year_not_by_quarter(clean_tables):
    from core import movies as M
    from db.models import Movie

    with clean_tables.get_session() as s:
        # 同一年、四种旧季母 + 一部新规则的 A：按季度分会出 4 组，按年只出 1 组
        for q in ("25A", "25B", "25C", "25D"):
            s.add(Movie(title=f"片{q}", quarter=q))
        s.add(Movie(title="片26", quarter="26A"))
        s.commit()

    ov = M.overview()
    assert "by_quarter" not in ov, "键名还叫 by_quarter —— 它装的是四位年，名字会骗人"
    got = {y: tot for y, tot, _ in ov["by_year"]}
    assert got == {"2025": 4, "2026": 1}, f"没有按年聚：{ov['by_year']}"
    assert [y for y, _, _ in ov["by_year"]] == ["2026", "2025"], "年份没有倒序"


def test_unknown_quarter_still_lands_in_its_own_bucket(clean_tables):
    """季度为空的片不能被算进某一年，也不能把整个函数搞崩。"""
    from core import movies as M
    from db.models import Movie

    with clean_tables.get_session() as s:
        s.add(Movie(title="无季度", quarter=""))
        s.add(Movie(title="有季度", quarter="26A"))
        s.commit()

    ov = M.overview()
    assert dict((y, t) for y, t, _ in ov["by_year"]) == {"2026": 1, "未知": 1}
    assert ov["by_year"][-1][0] == "未知", "未知要垫底"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_the_movies_list_has_no_second_level_quarter_heading(user, clean_tables):
    """列表里不许再出现二级季度小标题 —— 年这一级本来就是分组，二级只剩噪声。"""
    from db.models import Movie

    with clean_tables.get_session() as s:
        for q in ("25A", "25B", "25C", "25D"):
            s.add(Movie(title=f"片{q}", display_name=f"片{q}", quarter=q))
        s.commit()

    await user.open("/movies?t=list")
    await user.should_see("2025 年")
    tree = str(user.current_layout)
    # 季母只应出现在"没有二级标题"的意义上：四部片都在同一年组里
    assert tree.count("2025 年") >= 1
    for q in ("25A", "25B", "25C", "25D"):
        assert f"{q}   ·" not in tree, f"二级季度小标题又回来了：{q}"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_the_dashboard_bar_says_year(user, clean_tables):
    from db.models import Movie

    with clean_tables.get_session() as s:
        s.add(Movie(title="片", display_name="片", quarter="25C"))
        s.commit()

    await user.open("/movies?t=overview")
    await user.should_see("各年份")
    await user.should_not_see("各季度")


def test_movies_within_a_year_are_ordered_by_air_date_not_by_quarter_letter(clean_tables):
    """(R33, E-47) 年内按上映日倒序，季母不参与排序。

    剧场版按年归档之后，新识别的片一律落 `<yy>A`、存量还带 B/C/D。按季度键排的后果是
    **凡是重新识别过的片，永远沉到本年最底下**（真库 2025 年：12-31 上映的排在 02-14 下面）。
    而"回填季母"也修不好：全成 A 之后退化成按 id 排。所以排序键必须是上映日。
    """
    from core import movies as M
    from db.models import Movie
    import pages.movies as PM

    with clean_tables.get_session() as s:
        # 故意让季母与上映日反着来：新规则的 A 是最新的片，旧规则的 D 反而更早
        s.add(Movie(title="最新·新规则A", quarter="25A", air_date="2025-12-31"))
        s.add(Movie(title="旧规则D", quarter="25D", air_date="2025-10-01"))
        s.add(Movie(title="旧规则C", quarter="25C", air_date="2025-06-01"))
        s.add(Movie(title="无上映日", quarter="25B", air_date=None))
        s.commit()

    (year, groups), = PM._group_by_year_quarter(M.list_movies())
    assert year == "2025"
    titles = [m.title for _, grp in groups for m in grp]
    assert titles == ["最新·新规则A", "旧规则D", "旧规则C", "无上映日"], (
        f"年内没有按上映日倒序（或空上映日没垫底）：{titles}")


def test_rejected_movies_within_a_year_are_ordered_by_air_date_too(clean_tables):
    """(R33 回归审计) 『已忽略』页是平铺渲染，E-47 只改了主列表，它还按季母排 ——
    12-31 上映的沉到 03-01 之下，正是主列表刚修掉的形状。与 list_movies 同口径：年 → 上映日 → id。
    夹具刻意让季母与上映日反着走（新规则 A 配最晚的日期）。
    """
    from core import movies as M
    from db.models import Movie

    with clean_tables.get_session() as s:
        s.add(Movie(title="新规则A·12-31", quarter="25A", air_date="2025-12-31", rejected=True, bangumi_id=1))
        s.add(Movie(title="旧规则D·10-01", quarter="25D", air_date="2025-10-01", rejected=True, bangumi_id=2))
        s.add(Movie(title="旧规则B·03-01", quarter="25B", air_date="2025-03-01", rejected=True, bangumi_id=3))
        s.add(Movie(title="没有上映日", quarter="25C", air_date=None, rejected=True, bangumi_id=4))
        s.add(Movie(title="去年的", quarter="24D", air_date="2024-12-31", rejected=True, bangumi_id=5))
        s.commit()
    got = [m.title for m in M.list_rejected_movies()]
    assert got == ["新规则A·12-31", "旧规则D·10-01", "旧规则B·03-01", "没有上映日", "去年的"], got
