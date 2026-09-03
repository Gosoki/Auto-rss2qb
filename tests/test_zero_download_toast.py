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


@pytest.mark.parametrize("confirmed,rejected,gate", [
    (False, False, "还没确认"),
    (True, True, "『已忽略』"),
])
@pytest.mark.nicegui_main_file(_MAIN)
async def test_an_unsubscribed_animes_error_row_is_not_advertised_as_backfillable(
        user, clean_tables, cfg, confirmed, rejected, gate):
    """(E-54，2026-09-02 拍板选 C：契约不动、改显示)

    `download_plan` 按契约只排除 rejected + 停订，`confirmed` 由调用方过滤 —— 待确认番的 error 行
    也在 backfill_plan 里，详情页把它标成橙色『失败·可补下』、tooltip 让人去点『下载该源』，
    而那条路第一行就是 `return 0`。同一行在仪表盘『新入库』里是红色『失败』——两个界面结论相反。
    现在：不在订阅里的番，error 行一律红色『失败』，tooltip 说清是哪道闸、出路是右边的『下载』。
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
    await user.should_not_see("失败·可补下")
    # 徽标本身在页面上；tooltip 的文案要从元素的 props 里取（testing.User 的 should_see 看不到 tooltip）
    from nicegui import ui
    badges = [b for b in user.find(ui.badge).elements if getattr(b, "text", "") == "失败"]
    assert badges, "没有红色『失败』徽标"
    assert badges[0].props.get("color") == "red", badges[0].props
    # NiceGUI 的 tooltip 是一个**兄弟**元素，靠 props['target'] = '#<html_id>' 指回去
    from nicegui.elements.tooltip import Tooltip
    targets = {f"#{b.html_id}" for b in badges}
    tips = [t for t in user.find(Tooltip).elements if t.props.get("target") in targets]
    assert tips and gate in tips[0].text and "『补下』不会挑它" in tips[0].text, \
        [t.text for t in tips]


@pytest.mark.nicegui_main_file(_MAIN)
async def test_a_subscribed_animes_error_row_is_still_advertised_as_backfillable(
        user, clean_tables, cfg):
    """反向：已确认、未忽略的番，error 行照旧是橙色『失败·可补下』——别把两种一起改没了。"""
    from db.models import Anime, AnimeTorrent

    cfg(QB_ENABLED=True)
    with clean_tables.get_session() as s:
        a = Anime(title="某番", display_name="某番", season=1, quarter="26C",
                  confirmed=True, rejected=False, bangumi_id=4242, total_episodes=12)
        s.add(a); s.commit(); s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="1" * 40, source="ANi",
                           raw_title="[ANi] 某番 - 01", season=1, episode=1.0, status="error"))
        s.commit()
        aid = a.id

    await user.open(f"/detail/{aid}")
    await user.should_see("失败·可补下")


# ---------------- E-3：两条线详情页的『停滞』徽标都要说清为什么（R34 对抗审计：此前零守卫） ----------------

def _tooltip_of(user, badge_text: str):
    from nicegui import ui
    from nicegui.elements.tooltip import Tooltip

    badges = [b for b in user.find(ui.badge).elements if getattr(b, "text", "") == badge_text]
    assert badges, f"没有『{badge_text}』徽标"
    targets = {f"#{b.html_id}" for b in badges}
    tips = [t for t in user.find(Tooltip).elements if t.props.get("target") in targets]
    return badges[0], (tips[0].text if tips else "")


@pytest.mark.parametrize("reason", ["从 qB 消失（已下 40%，半成品文件应仍在目录里）", "qB 报错（已下 50%）",
                                    "60 分钟进度无推进（40%）"])
@pytest.mark.nicegui_main_file(_MAIN)
async def test_anime_detail_explains_a_stalled_row(user, clean_tables, cfg, reason):
    from db.models import Anime, AnimeTorrent

    cfg(QB_ENABLED=True)
    with clean_tables.get_session() as s:
        a = Anime(title="某番", display_name="某番", season=1, quarter="26C", confirmed=True, bangumi_id=1)
        s.add(a); s.commit(); s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="2" * 40, source="ANi", raw_title="[ANi] 某番 - 01",
                           season=1, episode=1.0, status="stalled", qb_progress=0.4, qb_state="stalledDL",
                           fail_reason=reason))
        s.commit()
        aid = a.id
    await user.open(f"/detail/{aid}")
    badge, tip = _tooltip_of(user, "停滞")
    assert badge.props.get("color") == "deep-orange", badge.props
    assert reason in tip and "『删除』" in tip, tip


@pytest.mark.nicegui_main_file(_MAIN)
async def test_movie_detail_explains_a_stalled_row_and_does_not_call_it_a_failure(user, clean_tables, cfg):
    """剧场版那页原来对任何 fail_reason 都改成橙色『上次失败』—— stalled 要分叉、颜色留 deep-orange。"""
    from db.models import Movie, MovieTorrent

    cfg(QB_ENABLED=True)
    with clean_tables.get_session() as s:
        m = Movie(title="某片", display_name="某片", quarter="2026", bangumi_id=2)
        s.add(m); s.commit(); s.refresh(m)
        s.add(MovieTorrent(movie_id=m.id, info_hash="3" * 40, raw_title="[组] 某片", status="stalled",
                           qb_progress=0.4, qb_state="stalledDL",
                           fail_reason="从 qB 消失（已下 40%，半成品文件应仍在目录里）"))
        s.commit()
        mid = m.id
    await user.open(f"/mdetail/{mid}")
    badge, tip = _tooltip_of(user, "停滞")
    assert badge.props.get("color") == "deep-orange", badge.props
    assert "从 qB 消失" in tip and "『删除』" in tip, tip
    assert "上次失败" not in tip, tip


@pytest.mark.nicegui_main_file(_MAIN)
async def test_an_unsubscribed_because_finished_animes_error_row_names_that_gate(user, clean_tables, cfg):
    """(R34 对抗审计) is_subscribed 的第三种情形：已判完结 + 开了停订。文案要说"已判完结并停订"。"""
    from datetime import datetime

    from db.models import Anime, AnimeTorrent

    cfg(QB_ENABLED=True, ANIME_FINISH_UNSUB=True)
    with clean_tables.get_session() as s:
        a = Anime(title="某番", display_name="某番", season=1, quarter="26C", confirmed=True,
                  rejected=False, bangumi_id=4242, total_episodes=12, finished_at=datetime(2026, 1, 1))
        s.add(a); s.commit(); s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="1" * 40, source="ANi",
                           raw_title="[ANi] 某番 - 01", season=1, episode=1.0, status="error"))
        s.commit()
        aid = a.id
    await user.open(f"/detail/{aid}")
    badge, tip = _tooltip_of(user, "失败")
    assert badge.props.get("color") == "red" and "已判完结并停订" in tip, (badge.props, tip)
