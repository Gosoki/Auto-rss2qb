"""(r42 E-81) 详情页单集 🗑 的确认框（顶层两项：还要这一集 / 这一集不要了；"还要"下面列候选或说以后怎么补）：
选项与默认项照 core.delete_options，确认后调对的入口、传对的 drop。

写法同 test_r40_ui_detail：打开真详情页、按浏览器方式投递点击，core 的删除 / 换一份入口一律打桩只记参数。
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from nicegui import ui

_MAIN = "tests/render_detail_main.py"
OLD = datetime.now() - timedelta(days=30)
_n = iter(range(1, 10 ** 6))


def _fire(user, el, event):
    lids = [lid for lid, lis in el._event_listeners.items() if lis.type == event]
    assert lids, f"{el} 上没有 {event} 监听，用例的前提坏了"
    for lid in lids:
        user.client.handle_event({"id": el.id, "listener_id": lid, "args": []})


async def _until(pred, what):
    for _ in range(60):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(what)


def _live(user, kind):
    return [e for e in user.client.layout.descendants() if isinstance(e, kind) and not e.is_deleted]


def _anime(s, **kw):
    from db.models import Anime
    base = dict(title="某番", display_name="某番", jp_name="ボウバン", season=1, quarter="26C",
                confirmed=True, rejected=False, bangumi_id=4242, total_episodes=12)
    base.update(kw)
    a = Anime(**base)
    s.add(a); s.commit(); s.refresh(a)
    return a.id


def _row(s, aid, src, ep, status="pending", **kw):
    from db.models import AnimeTorrent
    n = next(_n)
    base = dict(anime_id=aid, info_hash=f"{n + 500:040x}", source=src, site="nyaa", season=1, episode=float(ep),
                raw_title=f"[{src}] 某番 - {int(ep):02d} [1080p] #{n}", status=status,
                download_url=f"https://x/{n}.torrent", created_at=OLD, release_time=OLD)
    base.update(kw)
    t = AnimeTorrent(**base)
    s.add(t); s.commit(); s.refresh(t)
    return t.id


def _stub_core(monkeypatch, D):
    calls = []

    async def dele(tid, drop=True):
        calls.append(("delete", tid, drop)); return True

    async def swap(tid, pick):
        calls.append(("swap", tid, pick)); return {"deleted": True, "picked": True, "reason": ""}
    monkeypatch.setattr(D.anime, "delete_anime_torrent", dele)
    monkeypatch.setattr(D.anime, "delete_and_replace", swap)
    return calls


def _del_btn(user, tag):
    """原名里带 #tag 的那一行的 🗑。"""
    dels = [b for b in _live(user, ui.button) if b.props.get("icon") == "delete_forever"]
    return next(b for b in dels if f"#{tag}" in " ".join(
        e.text for e in next(a for a in b.ancestors() if isinstance(a, ui.column)).descendants()
        if isinstance(e, ui.label)))


async def _open_dialog(user, aid, tag):
    await user.open(f"/detail/{aid}")
    _fire(user, _del_btn(user, tag), "click")
    await _until(lambda: len(_live(user, ui.radio)) >= 1, "点删除没有弹确认框")
    radios = _live(user, ui.radio)
    mode, pick = radios[0], (radios[1] if len(radios) > 1 else None)
    ok = next(b for b in _live(user, ui.button) if (b.props.get("color") == "negative"
                                                   and "unelevated" in b.props))
    cancel = [b for b in _live(user, ui.button) if b.text == "取消"][-1]
    return mode, pick, ok, cancel


def _shape(clean_tables, **akw):
    """第 3 集：ANi 已下（被删的那份），Mikan 一条待下、NEST 一条失败过。"""
    with clean_tables.get_session() as s:
        aid = _anime(s, **akw)
        n = next(_n)
        t = _row(s, aid, "ANi", 3, status="sent", raw_title=f"[ANi] 某番 - 03 #{n}del")
        mk = _row(s, aid, "Mikan", 3)
        ne = _row(s, aid, "NEST", 3, status="error", retry_count=2)
    return aid, t, mk, ne, f"{n}del"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_swap_is_default_and_confirm_calls_delete_and_replace(user, clean_tables, cfg, monkeypatch):
    from pages import anime_detail as D
    cfg(ANIME_FINISH_UNSUB=False)
    aid, t, mk, ne, tag = _shape(clean_tables, pref_source="ANi")
    calls = _stub_core(monkeypatch, D)
    mode, pick, ok, _ = await _open_dialog(user, aid, tag)
    assert list(mode.options) == ["want", "drop"], mode.options
    assert mode.value == "want" and ok.text == "删除并下载"
    assert list(pick.options) == [mk, ne, "keep"] and pick.value == mk, "锁源时后台不会自动下别组：候选末尾多一条等待项"
    assert pick.options["keep"] == "先不换，等『ANi』以后再发", "等待项只放短句，完整说法选中后才出现"
    await user.should_see("锁着『ANi』：只这一集用『Mikan』，锁不变")
    _fire(user, ok, "click")
    await _until(lambda: calls, "确认后没有调换一份")
    assert calls == [("swap", t, mk)]


@pytest.mark.nicegui_main_file(_MAIN)
async def test_keep_and_drop_pass_the_right_drop_flag(user, clean_tables, cfg, monkeypatch):
    from pages import anime_detail as D
    cfg(ANIME_FINISH_UNSUB=False)
    aid, t, *_rest, tag = _shape(clean_tables, pref_source="ANi")
    calls = _stub_core(monkeypatch, D)
    mode, pick, ok, _ = await _open_dialog(user, aid, tag)
    pick.value = "keep"
    await _until(lambda: ok.text == "删除文件", ok.text)
    _fire(user, ok, "click")
    await _until(lambda: calls, "确认后没有删")
    assert calls == [("delete", t, False)]


@pytest.mark.nicegui_main_file(_MAIN)
async def test_drop_choice_drops(user, clean_tables, cfg, monkeypatch):
    from pages import anime_detail as D
    cfg(ANIME_FINISH_UNSUB=False)
    aid, t, *_rest, tag = _shape(clean_tables)
    calls = _stub_core(monkeypatch, D)
    mode, pick, ok, _ = await _open_dialog(user, aid, tag)
    assert list(mode.options) == ["want", "drop"]
    assert "keep" not in pick.options, "没锁源、后台下一轮会自动补：不重复给等待项"
    mode.value = "drop"
    await _until(lambda: ok.text == "删除，不要这一集", ok.text)
    _fire(user, ok, "click")
    await _until(lambda: calls, "确认后没有删")
    assert calls == [("delete", t, True)]


@pytest.mark.nicegui_main_file(_MAIN)
async def test_archived_has_no_swap(user, clean_tables, cfg, monkeypatch):
    from pages import anime_detail as D
    cfg(ANIME_FINISH_UNSUB=False)
    with clean_tables.get_session() as s:
        aid = _anime(s)
        n = next(_n)
        t = _row(s, aid, "ANi", 3, status="sent", archived_at=OLD, raw_title=f"[ANi] 某番 - 03 #{n}arc")
        _row(s, aid, "Mikan", 3)
    calls = _stub_core(monkeypatch, D)
    mode, pick, ok, _ = await _open_dialog(user, aid, f"{n}arc")
    assert pick is None and mode.value == "want", "已归档不给换一份：不列候选"
    assert ok.text == "标记已删"
    await user.should_see("核对文件名再删——同名的就是新下的那份")
    _fire(user, ok, "click")
    await _until(lambda: calls, "确认后没有删")
    assert calls == [("delete", t, False)]


@pytest.mark.nicegui_main_file(_MAIN)
async def test_no_candidate_shows_one_line_and_keeps(user, clean_tables, cfg, monkeypatch):
    from pages import anime_detail as D
    cfg(ANIME_FINISH_UNSUB=False)
    with clean_tables.get_session() as s:
        aid = _anime(s)
        n = next(_n)
        t = _row(s, aid, "ANi", 3, status="sent", raw_title=f"[ANi] 某番 - 03 #{n}one")
    calls = _stub_core(monkeypatch, D)
    mode, pick, ok, _ = await _open_dialog(user, aid, f"{n}one")
    assert pick is None and mode.value == "want" and ok.text == "删除文件"
    await user.should_see("以后有别的版本进来再自动下（现在库里没有别的版本）")
    _fire(user, ok, "click")
    await _until(lambda: calls, "确认后没有删")
    assert calls == [("delete", t, False)]


@pytest.mark.nicegui_main_file(_MAIN)
async def test_more_candidates_keeps_wait_item_last(user, clean_tables, cfg, monkeypatch):
    from pages import anime_detail as D
    cfg(ANIME_FINISH_UNSUB=False)
    with clean_tables.get_session() as s:
        aid = _anime(s, pref_source="ANi")
        n = next(_n)
        _row(s, aid, "ANi", 3, status="sent", raw_title=f"[ANi] 某番 - 03 #{n}many")
        ids = [_row(s, aid, f"G{i}", 3) for i in range(D._CAND_FOLD + 2)]
    _stub_core(monkeypatch, D)
    mode, pick, ok, _ = await _open_dialog(user, aid, f"{n}many")
    assert len(pick.options) == D._CAND_FOLD + 1 and list(pick.options)[-1] == "keep"
    more = next(b for b in _live(user, ui.button) if b.text.startswith("还有"))
    _fire(user, more, "click")
    await _until(lambda: len(pick.options) == len(ids) + 1, "展开后候选没补全")
    assert list(pick.options)[-1] == "keep" and set(list(pick.options)[:-1]) == set(ids)


@pytest.mark.nicegui_main_file(_MAIN)
async def test_cancel_deletes_nothing(user, clean_tables, cfg, monkeypatch):
    from pages import anime_detail as D
    cfg(ANIME_FINISH_UNSUB=False)
    aid, *_rest, tag = _shape(clean_tables)
    calls = _stub_core(monkeypatch, D)
    mode, _, _, cancel = await _open_dialog(user, aid, tag)
    _fire(user, cancel, "click")
    await _until(lambda: mode.is_deleted, "点取消框没关")
    await asyncio.sleep(0.1)
    assert calls == []


@pytest.mark.nicegui_main_file(_MAIN)
async def test_duplicate_uses_the_simple_confirm_and_never_drops(user, clean_tables, cfg, monkeypatch):
    from pages import anime_detail as D
    cfg(ANIME_FINISH_UNSUB=False)
    with clean_tables.get_session() as s:
        aid = _anime(s)
        n = next(_n)
        _row(s, aid, "ANi", 3, status="sent", raw_title=f"[ANi] 某番 - 03 #{n}dup")
        _row(s, aid, "Mikan", 3, status="sent")
    calls = _stub_core(monkeypatch, D)
    asked = []

    async def fake_confirm(title, note, **kw):
        asked.append(note); return True

    async def no_ask(*a, **k):
        raise AssertionError("同集还有一份，不该弹三选一")
    monkeypatch.setattr(D, "confirm", fake_confirm)
    monkeypatch.setattr(D, "_ask_delete", no_ask)
    await user.open(f"/detail/{aid}")
    _fire(user, _del_btn(user, f"{n}dup"), "click")
    await _until(lambda: calls, "确认后没有删")
    assert "同集还留着别的版本" in asked[0]
    assert calls[0][2] is False, calls


# ================================================================ r42 复查：UI-1 / C4 / C5 / UI-2

def _keep_opts(**kw):
    base = dict(lock="ANi", kw="", auto_next=None, sub=True, single=False, fallback=False, cands=[],
                lock_blocked=False, archived=False)
    base.update(kw)
    return base


@pytest.mark.parametrize("days", [0, 7])
def test_keep_text_when_lock_group_never_sent_this_episode(cfg, days):
    """锁定组没发过这一集：选『以后自动补』会进锁源缺集提醒、会触发兜底，不能说"只等它再发、不会提醒"。"""
    from pages import anime_detail as D
    cfg(ANIME_LOCK_FALLBACK_DAYS=days)
    s = D._keep_text(_keep_opts(lock_blocked=True))
    assert "锁定组没发过这一集：会出现在锁源缺集提醒里；『ANi』以后发了会自动下" in s and "不会提醒" not in s
    assert ("满 7 天停更兜底可能用别的组补一份" in s) == (days == 7) and ("兜底" in s) == (days == 7)
    assert "只等『ANi』以后再发这一集" in D._keep_text(_keep_opts())


def test_keep_text_archived_reminds_same_name(cfg):
    from pages import anime_detail as D
    cfg(ANIME_FINISH_UNSUB=False)
    assert "核对文件名再删——同名的就是新下的那份" in D._keep_text(_keep_opts(lock="", archived=True))
    assert "核对文件名" not in D._keep_text(_keep_opts(lock=""))


@pytest.mark.nicegui_main_file(_MAIN)
async def test_finished_defaults_to_drop_and_warns_on_keep_swap(user, clean_tables, cfg, monkeypatch):
    from pages import anime_detail as D
    cfg(ANIME_FINISH_UNSUB=False)
    aid, t, mk, ne, tag = _shape(clean_tables, finished_at=OLD)
    _stub_core(monkeypatch, D)
    mode, pick, ok, _ = await _open_dialog(user, aid, tag)
    assert "del-radio" in mode.classes and "del-radio" in pick.classes
    assert mode.value == "drop" and pick.value == mk

    def shown():
        lb = [lb for lb in _live(user, ui.label) if "这部已判下齐" in lb.text]
        assert lb
        return lb[0].visible and all(a.visible for a in lb[0].ancestors())
    assert not shown(), "选『不要了』时不提撤销下齐"
    mode.value = "want"
    await _until(shown, "选还要这一集没提会撤销下齐")
