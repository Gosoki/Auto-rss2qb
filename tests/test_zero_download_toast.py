"""(R31) 『下载该源』补下 0 集时不能报绿。

`download_pending_for_anime` 第一行就是 `if not is_subscribed(a): return 0` ——
而详情页对**待确认**的番照样把 error 行标成橙色『失败·可补下』
（标注侧的 `download_plan` 只排除 rejected、不看 confirmed，那是 D9 记录在案的契约）。
于是用户按提示点下去，什么都没发生，却收到一句**绿色**的"已触发下载 0 集"。

真库里 anime#96『落语朱音』现在正是这个状态（confirmed=0，带 25 条种子）——
它是被『绑定 bgm』那条路径打回待确认的。

全站口径：提示只描述【已经发生的事实】。这一组钉三件事：
0 集不报 positive、说清楚为什么是 0、而"确实没得下"与"闸把你挡住了"要分开说。
"""
import pytest

_MAIN = "tests/render_detail_main.py"


@pytest.mark.parametrize("confirmed,rejected,want", [
    (False, False, "还在【待确认】"),
    (True, True, "已被【忽略】"),
])
@pytest.mark.nicegui_main_file(_MAIN)
async def test_zero_downloads_explains_which_gate_blocked_it(
        user, clean_tables, cfg, confirmed, rejected, want):
    """【这一组的第一版是假守卫，两处都栽了】(R31 自查)

    ① 断言写的是 `should_see("待确认")` —— 而详情页对未确认的番**本来就挂着**
       一个『待确认』徽标，用例不点按钮也能绿。断言必须落在**只有那句提示才有**的字样上。
    ② `QB_ENABLED` 在用例夹具里默认是 False，此时这几个下载按钮是 `disable=True`
       （tooltip 写着"qB 未启用"），而 `UserInteraction.click()` 跳过禁用元素 ——
       **点击根本没发生**，两条断言都是在空跑。要 `cfg(QB_ENABLED=True)` 才点得动。
    """
    from db.models import Anime, AnimeTorrent

    cfg(QB_ENABLED=True)

    with clean_tables.get_session() as s:
        a = Anime(title="某番", display_name="某番", season=1, quarter="26C",
                  confirmed=confirmed, rejected=rejected, bangumi_id=4242, total_episodes=12)
        s.add(a); s.commit(); s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="1" * 40, source="ANi",
                           raw_title="[ANi] 某番 - 01", season=1, episode=1.0, status="error"))
        s.commit()
        aid = a.id

    await user.open(f"/detail/{aid}")
    user.find("下载该源").click()
    await user.should_see(want)
    await user.should_not_see("已触发下载 0 集")


@pytest.mark.nicegui_main_file(_MAIN)
async def test_a_subscribed_anime_with_nothing_to_do_says_so_neutrally(
        user, clean_tables, cfg, monkeypatch):
    """反向：闸没挡住、也确实没得下 —— 要说的是另一句话，不能也怪到闸上。"""
    from core import engine as E
    from db.models import Anime, AnimeTorrent

    cfg(QB_ENABLED=True)

    async def reachable():
        return True                     # 打桩：别让用例真去连 qB（会刷一条 ERROR 日志）
    monkeypatch.setattr(E.qb, "reachable", reachable)

    with clean_tables.get_session() as s:
        a = Anime(title="某番", display_name="某番", season=1, quarter="26C",
                  confirmed=True, rejected=False, bangumi_id=4243, total_episodes=12)
        s.add(a); s.commit(); s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="2" * 40, source="ANi",
                           raw_title="[ANi] 某番 - 01", season=1, episode=1.0, status="sent"))
        s.commit()
        aid = a.id

    await user.open(f"/detail/{aid}")
    user.find("下载该源").click()
    await user.should_see("没有可补下的集")
    await user.should_not_see("已触发下载 0 集")


@pytest.mark.nicegui_main_file(_MAIN)
async def test_zero_because_qb_is_down_is_not_reported_as_nothing_to_do(
        user, clean_tables, cfg, monkeypatch):
    """(R31) 0 集的**第三种**成因：闸没挡住、候选也在，只是这一轮没放行。

    最常见的就是 qB 连不上 —— `download_pending_for_anime` 那边写一行 WARNING 就返回 0，
    而用户看不到日志。把它说成"该下的都下过了"是假话，且会让人以为下过了、不再管它。
    这一条是写用例时才发现的：前一版的 else 分支把三种 0 混成了一种。
    """
    from core import engine as E
    from db.models import Anime, AnimeTorrent

    cfg(QB_ENABLED=True)

    async def unreachable():
        return False
    monkeypatch.setattr(E.qb, "reachable", unreachable)

    with clean_tables.get_session() as s:
        a = Anime(title="某番", display_name="某番", season=1, quarter="26C",
                  confirmed=True, rejected=False, bangumi_id=4244, total_episodes=12)
        s.add(a); s.commit(); s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="3" * 40, source="ANi",
                           raw_title="[ANi] 某番 - 01", season=1, episode=1.0, status="pending"))
        s.commit()
        aid = a.id

    await user.open(f"/detail/{aid}")
    user.find("下载该源").click()
    await user.should_see("多半是 qB 此刻连不上")
    await user.should_not_see("该下的都下过了")
