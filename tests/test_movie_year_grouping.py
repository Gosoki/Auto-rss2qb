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
