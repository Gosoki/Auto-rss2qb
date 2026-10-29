"""(R39 U10) 详情页改『版本』关键词只重建分集区，不能连同一行的按钮一起删掉。

『版本』输入框是 blur / 回车生效的。用户填完关键词、直接去点同一行的『下载该源』『确认下载』时，
浏览器先发 blur、再发 click。以前 blur 的处理器里是 `body.refresh()` —— 整块详情重建，
同一行那几个按钮被删、换成新 id 的新按钮；随后带着【旧 id】到达的 click 在
`Client.handle_event` 里按 id 查不到元素，被**静默丢弃**：用户点了，什么都没发生，也没有任何提示。

所以点击必须走 `client.handle_event`（浏览器 websocket 事件的同一入口：按 id 找元素，删了就丢）。
`user.find(...).click()` 拿的是 Python 对象引用，元素删没删都照点不误，测不出这类回归。
"""
import asyncio

import pytest
from nicegui import ui

_MAIN = "tests/render_detail_main.py"


def _kw_input(user):
    ins = [e for e in user.find(ui.input).elements if e.props.get("label") == "版本"]
    assert len(ins) == 1, f"『版本』输入框应恰好一个，找到 {len(ins)} 个"
    return ins[0]


def _button(user, text):
    bs = [b for b in user.find(kind=ui.button, content=text).elements if b.text == text]
    assert len(bs) == 1, f"『{text}』按钮应恰好一个，找到 {len(bs)} 个"
    return bs[0]


def _fire(user, el, event):
    """按浏览器的方式投递事件：只带元素 id 与监听 id，由 Client.handle_event 去找元素。"""
    lids = [lid for lid, lis in el._event_listeners.items() if lis.type == event]
    assert lids, f"{el} 上没有 {event} 监听，用例的前提坏了"
    for lid in lids:
        user.client.handle_event({"id": el.id, "listener_id": lid, "args": []})


async def _until(pred, what):
    """refresh 是 fire-and-forget 的后台任务（AwaitableResponse），要等它真的跑完再断言。"""
    for _ in range(60):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(what)


def _ep_header(user):
    """分集区标题『分集 / 种子（N）』—— 它被删了，说明分集区那次 refresh 确实跑过了。"""
    ls = [e for e in user.find(kind=ui.label, content="分集 / 种子").elements
          if not e.is_deleted]
    assert len(ls) == 1, f"分集区标题应恰好一个，找到 {len(ls)} 个"
    return ls[0]


def _seed(clean_tables, *, confirmed, pref_keyword, titles):
    from db.models import Anime, AnimeTorrent

    with clean_tables.get_session() as s:
        a = Anime(title="某番", display_name="某番", jp_name="ボウバン", season=1, quarter="26C",
                  confirmed=confirmed, bangumi_id=4242, total_episodes=12,
                  pref_keyword=pref_keyword)
        s.add(a); s.commit(); s.refresh(a)
        for i, raw in enumerate(titles):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{i + 1:040x}", source="喵萌奶茶屋",
                               raw_title=raw, season=1, episode=1.0, status="pending"))
        s.commit()
        return a.id


@pytest.mark.parametrize("btn_text", ["下载该源", "确认下载"])
@pytest.mark.nicegui_main_file(_MAIN)
async def test_editing_the_keyword_does_not_swallow_the_next_click_on_the_action_row(
        user, clean_tables, cfg, monkeypatch, btn_text):
    """填完关键词直接点『下载该源』/『确认下载』：blur 之后原按钮还活着，带原 id 的点击能落到处理器上。"""
    from pages import anime_detail as D

    cfg(QB_ENABLED=True)          # 否则『下载该源』是 disable 的，handle_event 本来就不投递
    called = []

    async def fake_download(aid):
        called.append(("download", aid)); return 1

    async def fake_gated(aid, action, pref_source=None):
        called.append((action, aid)); return None    # None = 用户在闸上取消：不再刷新、不弹提示

    monkeypatch.setattr(D.anime, "download_pending_for_anime", fake_download)
    monkeypatch.setattr(D, "subscribe_gated", fake_gated)

    aid = _seed(clean_tables, confirmed=False, pref_keyword=None,
                titles=["[喵萌奶茶屋] 某番 - 01 [1080p][繁日]"])
    await user.open(f"/detail/{aid}")
    btn, kw, header = _button(user, btn_text), _kw_input(user), _ep_header(user)

    kw.value = "繁日"
    _fire(user, kw, "blur")
    await _until(lambda: header.is_deleted, "blur 之后分集区没有重建 —— 关键词没生效，用例前提坏了")
    assert D.anime.get_anime(aid).pref_keyword == "繁日"
    assert not btn.is_deleted, f"改关键词把同一行的『{btn_text}』也删了：紧跟着的那次点击会被静默丢弃"
    assert not kw.is_deleted, "改关键词把输入框自己也重建了（回车后焦点会丢）"

    _fire(user, btn, "click")
    want = "download" if btn_text == "下载该源" else "confirm"
    await _until(lambda: called, f"带原 id 点『{btn_text}』，处理器没有被调用（点击被吞了）")
    assert called == [(want, aid)], called


def _row_text_of(badge) -> str:
    """徽标所在那一条（ui.column 包两行：集号行 + 种子原名行）里全部 label 的文字。"""
    col = next(a for a in badge.ancestors() if isinstance(a, ui.column))
    return " ".join(e.text for e in col.descendants() if isinstance(e, ui.label))


def _will_download(user):
    return [b for b in user.find(ui.badge).elements
            if getattr(b, "text", "") == "将下载" and not b.is_deleted]


@pytest.mark.nicegui_main_file(_MAIN)
async def test_editing_the_keyword_moves_the_will_download_badge_to_the_other_version(
        user, clean_tables):
    """同一集有简日、繁日两条：关键词从『简日』改成『繁日』，『将下载』要跟着换到繁日那条上。

    只刷新分集区，标注就必须在分集区里重算 —— 这条钉住"关键词改了、标注没跟上"。
    """
    from pages import anime_detail as D

    aid = _seed(clean_tables, confirmed=True, pref_keyword="简日",
                titles=["[喵萌奶茶屋] 某番 - 01 [1080p][简日]",
                        "[喵萌奶茶屋] 某番 - 01 [1080p][繁日]"])
    await user.open(f"/detail/{aid}")
    before = _will_download(user)
    assert len(before) == 1 and "简日" in _row_text_of(before[0]), \
        [_row_text_of(b) for b in before]

    kw = _kw_input(user)
    kw.value = "繁日"
    _fire(user, kw, "blur")
    await _until(lambda: before[0].is_deleted, "blur 之后分集区没有重建")
    assert D.anime.get_anime(aid).pref_keyword == "繁日"
    after = _will_download(user)
    assert len(after) == 1, [_row_text_of(b) for b in after]
    txt = _row_text_of(after[0])
    assert "繁日" in txt and "简日" not in txt, txt


@pytest.mark.parametrize("event", ["blur", "keydown.enter"])
@pytest.mark.parametrize("stored", [None, "繁日"], ids=["库里没词-写库", "库里已是-不写库"])
@pytest.mark.nicegui_main_file(_MAIN)
async def test_keyword_input_shows_the_stripped_value_that_was_saved(user, clean_tables, event, stored):
    """(r39 F37) 填『 繁日 』：库里存 strip 后的『繁日』，框里也要回写成『繁日』，屏幕和库一致。

    输入框不再随 body.refresh() 重建（理由见 ep_section），不回写就一直显示带空格的原样。
    库里已经是『繁日』（走『没改就什么都不做』那支、不写库）时也要规整。
    """
    from pages import anime_detail as D

    aid = _seed(clean_tables, confirmed=True, pref_keyword=stored,
                titles=["[喵萌奶茶屋] 某番 - 01 [1080p][繁日]"])
    await user.open(f"/detail/{aid}")
    kw, header = _kw_input(user), _ep_header(user)
    kw.value = " 繁日 "
    _fire(user, kw, event)
    await _until(lambda: kw.value == "繁日", f"{event} 之后输入框没回写 strip 后的值：{kw.value!r}")
    assert D.anime.get_anime(aid).pref_keyword == "繁日"
    if stored is None:   # 写了库就会刷分集区：等它跑完再收尾，别把后台 refresh 留到用例结束之后
        await _until(lambda: header.is_deleted, f"{event} 写了库却没刷新分集区")
