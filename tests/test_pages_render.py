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
db.apply_configured_backend()   # 业务表的迁移在这一步（同 main.py）

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


# ---------------- (R22) 维护窗口里，七个页面仍然要渲染得出来 ----------------

@pytest.mark.nicegui_main_file(_MAIN)
@pytest.mark.parametrize("path", PAGES)
async def test_every_page_still_renders_during_maintenance(user: User, path):
    """整库维护（切库 / 迁移）期间打开任意页面，都必须渲染成功。

    ⚠️ R21 加维护提示块时写的是 `tint("orange")` —— 而 `_TOKENS` 里只有
    blue/green/red/amber/grey/ink-soft，**没有 orange**：`tint()` 直接 KeyError。
    而 `_db_down_notice()` 在 `frame()` 的 `try: yield` **之前**执行，那个兜底够不着 ——
    于是维护窗口一开，**七个页面全部构建失败**，偏偏那正是用户盯着屏幕等结果的几十秒。

    这条分支在 R21 是零覆盖的：4 条 maintenance 用例全停在 `db` 这一层，
    渲染这一层一次都没走到。约束的作用域大于验证的作用域，第②号形状。
    """
    import db as _db

    with _db.maintenance("正在迁移数据"):
        await user.open(path)
        tree = str(user.current_layout)
    assert "数据库维护中" in tree, f"{path} 维护期间没显示维护提示"
    assert len(tree) > 300, f"{path} 维护期间几乎渲染不出东西（{len(tree)} 字符）"


def test_every_colour_the_notice_uses_is_in_the_token_table():
    """反向：上一条是行为级的。这一条钉住"提示块用的颜色都在 token 表里"。

    `tint()` 拿不到 token 就 KeyError，而它被调用的位置在兜底之外 ——
    加一个新颜色时忘了往表里登记，代价是整站白屏。
    """
    import ast
    import inspect

    from pages import layout as L

    tree = ast.parse(inspect.getsource(L._db_down_notice))
    wanted = {n.args[0].value for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id in ("tint", "text_token") and n.args
              and isinstance(n.args[0], ast.Constant)}
    assert wanted, "一个 token 都没用到，用例的前提坏了"
    missing = sorted(wanted - set(L._TOKENS))
    assert not missing, f"提示块用了 token 表里没有的颜色（tint 会 KeyError、整页白屏）：{missing}"


# ---------------- (R24) 番剧详情：全项目唯一一块"构建期才炸"的盲区 ----------------

_DETAIL_MAIN = "tests/render_detail_main.py"


@pytest.mark.nicegui_main_file(_DETAIL_MAIN)
async def test_the_anime_detail_component_builds(user: User, clean_tables):
    """`render_anime_detail` 要能真的构建出来。

    它是渲染进列表页悬浮框的**组件**，没有独立路由（文件第一行自己写着这句）——
    于是上面那 7 条路由怎么开都开不到它。而它是个 659 行、116 处 `ui.*` 调用的文件，
    并且是好几个**不可逆操作**的唯一入口（补齐该源 / 删种子 / 改集号 / 绑定 bgm）。
    R21 那次 `tint("orange")` 让七个页面同时构建失败，就是这类错 ——
    而这个文件当时（以及此后）连一行都没被执行过。

    这里用一条**只存在于测试进程**的路由把它挂起来（见 tests/render_detail_main.py）。
    """
    from datetime import datetime

    from db.models import Anime, AnimeTorrent

    with clean_tables.get_session() as s:
        a = Anime(title="测试番", display_name="测试番·中文名", jp_name="テスト",
                  season=2, quarter="26C", confirmed=True, bangumi_id=4242,
                  total_episodes=13, ep_offset=13, air_date="2026-07-05", air_weekday=5)
        s.add(a)
        s.commit()
        s.refresh(a)
        for i, (ep, st) in enumerate(((1.0, "sent"), (2.0, "pending"), (3.0, "error"),
                                      (-1.0, "pending"), (4.0, "stalled"))):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{i:040x}", source="ANi",
                               raw_title=f"[ANi] 测试番 - {int(ep):02d} [1080P]",
                               season=2, episode=ep, status=st,
                               save_path="/data/动画/26C/テスト/Season 2",
                               created_at=datetime.now()))
        s.commit()
        aid = a.id

    await user.open(f"/detail/{aid}")
    tree = str(user.current_layout)
    assert len(tree) > 800, f"详情几乎没渲染出来（{len(tree)} 字符）"
    for word in ("测试番·中文名", "重新识别", "编辑季度"):
        assert word in tree, f"详情里找不到「{word}」"


@pytest.mark.nicegui_main_file(_DETAIL_MAIN)
async def test_the_detail_of_a_missing_anime_does_not_blow_up(user: User, clean_tables):
    """番被删掉之后再打开它的详情（列表页的悬浮框可能拿着旧 id）：要说人话，不能炸。"""
    await user.open("/detail/999999")
    assert "番剧不存在" in str(user.current_layout)


@pytest.mark.nicegui_main_file(_DETAIL_MAIN)
async def test_the_detail_builds_while_the_db_is_in_maintenance(user: User, clean_tables):
    """维护窗口里打开详情也不能炸 —— 与七个页面同一条纪律（R22 那条 P0 就是这么漏的）。"""
    import db as _db

    with _db.maintenance("正在迁移数据"):
        await user.open("/detail/1")
        tree = str(user.current_layout)
    assert "数据库维护中" in tree


# ---------------- (R24) 徽标居中：从【渲染出来的页面】上验，不是从常量上验 ----------------

@pytest.mark.nicegui_main_file(_MAIN)
async def test_the_badge_centering_rule_reaches_the_rendered_page(user: User):
    """徽标的字必须垂直+水平都居中，而且这条规则要**真的进到这次渲染的 head 里**。

    `tests/test_ui_badge_style.py` 那两条守卫查的是「常量内容对不对」和
    「注入调用在不在 frame() 里」—— 都是源码级的。这一条从**渲染结果**上验：
    NiceGUI 的层叠层顺序、`add_head_html` 的时机、Quasar 自己的样式，
    任何一环变了都可能让规则到不了页面，而源码看着一点没变。

    （另用真实 HTTP 响应核对过同一件事：响应体里确实有
    `.q-badge{…display:inline-flex!important;align-items:center!important;
    justify-content:center!important}`，以及 NiceGUI 的层序声明。）
    """
    from pages.layout import _HEAD_BADGE_CSS

    for needed in ("display:inline-flex", "align-items:center", "justify-content:center"):
        assert needed in _HEAD_BADGE_CSS, f"徽标居中规则少了 {needed}"

    await user.open("/")
    # 【head_html 是一个字符串，不是列表】按列表迭代会拿到一个个字符 ——
    # 第一版就是这么写的，断言恒假，看着像规则没进去，其实是用例读错了类型。
    head = str(user.client.head_html) + str(type(user.client).shared_head_html)
    assert _HEAD_BADGE_CSS in head, \
        "徽标 CSS 没进这次渲染的 head —— 规则到不了浏览器（常量写得再对也没用）"
