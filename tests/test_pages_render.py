"""页面【真的渲染出来了】吗——不是"HTTP 200"。

之前每轮收尾的冒烟只 `curl` 状态码，而 NiceGUI 的 200 只代表那层 HTML 外壳发出去了：
界面内容全靠 socket.io 之后推下来。外壳照发、内容一个字没有，也是 200。
这一组用 NiceGUI 官方的 `testing.User` 把页面真正构建一遍，断言元素树里有东西。

它同时能挡住一整类"只在点下去那一刻才炸"的错：页面构建期抛异常（漏 import、
拼错属性、模板里引用了不存在的字段）在这里会当场红，而 curl 只会看到 200 或 500。
"""
import pytest
from nicegui.testing import User

import config
import db

db.init_db()
config.load_from_db()

_MAIN = "tests/render_main.py"
PAGES = ["/", "/movies", "/parse", "/manual", "/settings", "/sources", "/logs"]


@pytest.mark.nicegui_main_file(_MAIN)
@pytest.mark.parametrize("path", PAGES)
async def test_every_page_renders_something(user: User, path):
    """每个页面都要构建出一棵非空的元素树。"""
    await user.open(path)
    tree = str(user.current_layout)
    assert len(tree) > 500, f"{path} 渲染出来几乎是空的（{len(tree)} 字符）"


@pytest.mark.nicegui_main_file(_MAIN)
@pytest.mark.parametrize("path,keywords", [
    ("/", ["番剧", "剧场版", "设置"]),
    ("/movies", ["剧场版"]),
    ("/settings", ["qBittorrent", "代理", "通知"]),
    ("/sources", ["源管理", "字幕组白名单"]),
])
async def test_pages_contain_their_own_content(user: User, path, keywords):
    """再往前一步：不只是"有元素"，而是**这个页面自己的内容**在里面。

    只断言长度挡不住"骨架渲染了、主体面板整个没出来"——而那正是数据层出问题时的表现。
    """
    await user.open(path)
    tree = str(user.current_layout)
    missing = [k for k in keywords if k not in tree]
    assert not missing, f"{path} 里找不到：{missing}"


# ---------------- 列表上的『已下/可下』（R20） ----------------

@pytest.mark.nicegui_main_file(_MAIN)
async def test_manage_list_shows_episode_progress(user: User, clean_tables):
    """(R20) 番剧列表要显示『已下集数/库里有种子的集数』——这是用来查错的那个比值。

    它把两类问题一眼摊开：分子 < 分母 = 有集号还没到手；分母 > bgm 记的总集数 = 集号本身就不对。
    """
    from db.models import Anime, AnimeTorrent
    with clean_tables.get_session() as s:
        a = Anime(title="测试番", season=1, confirmed=True, quarter="26C",
                  bangumi_id=1, total_episodes=12)
        s.add(a)
        s.commit()
        s.refresh(a)
        for i, st in enumerate([("sent")] * 3 + ["pending"] * 2, start=1):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{i:040x}", raw_title=f"t{i}",
                               episode=i, status=st))
        s.commit()

    await user.open("/?t=manage")
    tree = str(user.current_layout)
    assert "3/5" in tree, f"列表上没有『已下/可下』这个比值：{tree[:400]}"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_finished_badge_is_gone(user: User, clean_tables, cfg):
    """(R20) 『已完结』不再显示——它的信息量被『已下/可下』覆盖了（12/12 就是完结）。

    但开了停订时仍要出徽标：那是【行为变化】不是状态描述，在此之前这部番在列表里
    与正常追番的番长得一模一样，而它已经不再自动下新集了。
    """
    from datetime import datetime

    from db.models import Anime, AnimeTorrent
    with clean_tables.get_session() as s:
        a = Anime(title="完结番", season=1, confirmed=True, quarter="26C",
                  bangumi_id=2, total_episodes=2, finished_at=datetime.now())
        s.add(a)
        s.commit()
        s.refresh(a)
        for i in (1, 2):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"f{i:039x}", raw_title=f"t{i}",
                               episode=i, status="sent", qb_progress=1.0))
        s.commit()

    cfg(ANIME_FINISH_UNSUB=False)
    await user.open("/?t=manage")
    tree = str(user.current_layout)
    assert "已完结" not in tree, "『已完结』徽标还在"
    assert "已停订" not in tree, "没开停订却出了『已停订』"
    assert "2/2" in tree, "进度比值没显示"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_unsubscribed_badge_appears_when_the_switch_is_on(user: User, clean_tables, cfg):
    """开了停订时必须出徽标——它标的是"这部番不再自动下新集了"，是行为不是状态。"""
    from datetime import datetime

    from db.models import Anime, AnimeTorrent
    with clean_tables.get_session() as s:
        a = Anime(title="停订番", season=1, confirmed=True, quarter="26C",
                  bangumi_id=3, total_episodes=2, finished_at=datetime.now())
        s.add(a)
        s.commit()
        s.refresh(a)
        for i in (1, 2):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"e{i:039x}", raw_title=f"t{i}",
                               episode=i, status="sent", qb_progress=1.0))
        s.commit()

    cfg(ANIME_FINISH_UNSUB=True)
    await user.open("/?t=manage")
    assert "已停订" in str(user.current_layout), "开了停订却没有任何标记"
