"""(R36 体检) P3 那批小修的守卫 —— 逻辑失真 / 推送正文 / 面板可达性。每条变异后必须红。

  · logic-5   仪表盘对退避重试中的待下行说『重试中』，不说『备用项』
  · logic-7   _merge_movie 与 _merge_anime 同一条季度护栏
  · logic-2   完结闩的旧键认领不再等"第一次有命中"
  · notify-8  保存设置只在真的改了 NOTIFY_* 时才清通知冷却
  · notify-6/7 交付推送：番剧用展示名+季号，剧场版带版本身份
  · ux-6/7/9  待确认/已忽略的展开条、『跳转到』可聚焦、『已完成』『下载中』有 tooltip
  · logic-10  搬迁确认框：目录没变但文件落在别处时说"搬回来"
"""
import ast
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import select

import config
import db
from db.models import Anime, AnimeTorrent, Movie, MovieTorrent, Setting

ROOT = Path(__file__).resolve().parent.parent
_MAIN = "tests/render_main.py"


async def _ret(v):
    return v


# ---------------- logic-5：退避重试中 ≠ 备用项 ----------------

def test_dashboard_says_retrying_not_backup_for_a_row_in_backoff(clean_tables):
    """同一行种子，详情页说『重试中』、仪表盘说『备用项』（同集已有更优版本）—— 两个说法相反。"""
    from core import anime as A
    from pages.layout import live_status

    later = datetime.now() + timedelta(minutes=30)
    text, color = live_status("pending", "", 0, None, 0, False, True, episode=3, retry_at=later)
    assert "重试" in text and "备用" not in text, f"退避中的待下行被说成了 {text!r}"
    text2, _ = live_status("pending", "", 0, None, 0, False, True, episode=3)      # 正向控制
    assert text2 == "备用项", f"没有 retry_at 的备用行文案变了：{text2!r}"
    # 仪表盘那份行投影必须把 retry_at 带出来，否则上面那一档永远走不到
    with clean_tables.get_session() as s:
        a = Anime(title="T", season=1, confirmed=True)
        s.add(a); s.commit(); s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="1" * 40, raw_title="x", episode=3,
                           status="pending", retry_at=later, retry_count=1,
                           created_at=datetime.now()))
        s.commit()
    rows = A.recent_anime_rows()
    assert rows and rows[0].get("retry_at") is not None, "recent_anime_rows 没把 retry_at 投影出来"


# ---------------- logic-7：剧场版合并的季度护栏 ----------------

def test_merge_movie_inherits_the_quarter_when_only_the_loser_has_files(clean_tables):
    """keeper 尚未落盘、loser 已有文件 → 合并后用 loser 的季度，新版本才不会与旧文件散在两个年份目录。"""
    from core import movies as M

    with clean_tables.get_session() as s:
        keeper = Movie(title="片", quarter="26A")
        loser = Movie(title="片", quarter="25D")
        s.add(keeper); s.add(loser); s.commit(); s.refresh(keeper); s.refresh(loser)
        s.add(MovieTorrent(movie_id=loser.id, info_hash="2" * 40, raw_title="v1", status="sent",
                           save_path="/m/25D/片", created_at=datetime.now()))
        s.commit()
        kid, lid = keeper.id, loser.id
        M._merge_movie(s, lid, kid)
    with clean_tables.get_session() as s:
        assert s.get(Movie, lid) is None, "loser 没被合并掉（前提坏了）"
        assert s.get(Movie, kid).quarter == "25D", "keeper 没落盘却没继承 loser 的季度"
    # 正向控制：keeper 自己已有文件时季度不动
    with clean_tables.get_session() as s:
        k2 = Movie(title="片2", quarter="26A"); l2 = Movie(title="片2", quarter="25D")
        s.add(k2); s.add(l2); s.commit(); s.refresh(k2); s.refresh(l2)
        s.add(MovieTorrent(movie_id=k2.id, info_hash="3" * 40, raw_title="v", status="sent",
                           created_at=datetime.now()))
        s.commit()
        M._merge_movie(s, l2.id, k2.id)
        assert s.get(Movie, k2.id).quarter == "26A"


# ---------------- logic-2：完结闩的旧键认领 ----------------

async def test_finish_latch_adopts_the_legacy_key_even_when_nothing_is_finished(clean_tables):
    """一个从来没有过完结命中的库，旧全局键 _FINISH_BACKFILL_DONE 也必须被认领掉 ——
    否则它会躺到"下一个恰好有命中的业务库"去认，那个库被当成回填过了，首批完结通知静默。"""
    from core import anime as A

    key = "_FINISH_BACKFILL_DONE"
    with db.get_meta_session() as s:
        if s.get(Setting, key) is None:
            s.add(Setting(key=key, value="1")); s.commit()
    assert await A.sweep_finished() == 0            # 空库：没有任何命中
    with db.get_meta_session() as s:
        keys = {r.key for r in s.exec(select(Setting))}
    assert key not in keys and db.scoped_flag(key) in keys, \
        f"没有命中的那一轮没做旧键认领：{sorted(k for k in keys if 'FINISH' in k)}"


# ---------------- notify-8：保存别的设置不清通知冷却 ----------------

def test_saving_unrelated_settings_keeps_notify_cooldowns():
    """(AST) 设置页里每一处 notify.reset_state() 都必须在一个提到 NOTIFY_ 的 if 里。

    改个轮询间隔就把 failed/stalled 的 6 小时冷却清掉，下一轮巡检会把同一批告警原样再推一遍；
    同一个文件的『发送测试通知』早就为此专门避开了 reset_state。
    """
    tree = ast.parse((ROOT / "pages/settings.py").read_text(encoding="utf-8"))
    parents = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parents[c] = n
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "reset_state"]
    assert calls, "设置页里找不到 notify.reset_state()（守卫前提坏了）"
    for c in calls:
        p, guarded = parents.get(c), False
        while p is not None:
            if isinstance(p, ast.If) and "NOTIFY_" in ast.unparse(p.test):
                guarded = True
                break
            p = parents.get(p)
        assert guarded, f"pages/settings.py:{c.lineno} 的 reset_state() 没被『真的改了 NOTIFY_*』的条件守着"


# ---------------- notify-6/7：交付推送的正文 ----------------

async def test_delivered_push_names_the_anime_by_display_name_and_season(clean_tables, monkeypatch, cfg):
    """种子行的 anime_title 是入库快照（同一部番三种写法），推送该用 bgm 纠正后的展示名，并带季号。"""
    from core import anime as A

    with clean_tables.get_session() as s:
        a = Anime(title="Sousou no Frieren", display_name="葬送的芙莉莲", season=2, confirmed=True,
                  quarter="26A")
        s.add(a); s.commit(); s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="4" * 40, raw_title="[组] Sousou no Frieren - 03",
                           anime_title="Sousou no Frieren", episode=3.0, season=2,
                           download_url="http://x/a.torrent", status="pending",
                           created_at=datetime.now()))
        s.commit()
        tid = s.exec(select(AnimeTorrent)).one().id

    sent = []
    monkeypatch.setattr(A, "notify_event", lambda kind, msg, **kw: (sent.append((kind, msg)), _ret(True))[1])
    monkeypatch.setattr(A.engine, "fetch_torrent_bytes", lambda url: _ret(b"d1:xe"))
    monkeypatch.setattr(A.engine, "add_to_qb", lambda *a, **k: _ret(True))
    cfg(DOWN_PATH="/data", QB_ENABLED=True, QB_SYNC_STATUS=False)

    assert await A.download_anime_torrent(tid) is True
    texts = [m for k, m in sent if k == "delivered"]
    assert len(texts) == 1, f"delivered 事件发了 {len(texts)} 次：{sent}"
    assert "葬送的芙莉莲" in texts[0] and "Sousou" not in texts[0], texts[0]
    assert "第2季" in texts[0] and "第3集" in texts[0] and "3.0" not in texts[0], texts[0]


async def test_movie_push_tells_which_version_was_delivered(clean_tables, monkeypatch, cfg):
    """同一部片 53 个版本推出来一模一样 —— 正文要带来源与标题片段。"""
    from core import movies as M

    with clean_tables.get_session() as s:
        m = Movie(title="某剧场版", quarter="26A", jp_name="某剧场版")
        s.add(m); s.commit(); s.refresh(m)
        s.add(MovieTorrent(movie_id=m.id, info_hash="5" * 40, raw_title="[LoliHouse] 某剧场版 [BDRip 1080p]",
                           source="LoliHouse", download_url="http://x/a.torrent", status="pending",
                           created_at=datetime.now()))
        s.commit()
        mt_id = s.exec(select(MovieTorrent)).one().id

    sent = []
    monkeypatch.setattr(M, "notify_event", lambda kind, msg, **kw: (sent.append((kind, msg)), _ret(True))[1])
    monkeypatch.setattr(M.engine, "fetch_torrent_bytes", lambda url: _ret(b"d1:xe"))
    monkeypatch.setattr(M.engine, "add_to_qb", lambda *a, **k: _ret(True))
    cfg(DOWN_PATH="/data", QB_ENABLED=True, QB_SYNC_STATUS=False)

    assert await M.download_movie_torrent(mt_id) is True
    texts = [m for k, m in sent if k == "movie"]
    assert len(texts) == 1, sent
    assert "LoliHouse" in texts[0] and "BDRip" in texts[0], f"推送里分不出是哪个版本：{texts[0]!r}"


# ---------------- ux-6：待确认 / 已忽略 的一键展开 ----------------

def test_confirm_and_reject_panels_have_an_expand_all_bar():
    """(AST) 两个按季度分组、默认只开第一组的面板都必须挂 expand_collapse_bar —— KPI 说 5 条，点进去只看见 3 条。"""
    tree = ast.parse((ROOT / "pages/anime.py").read_text(encoding="utf-8"))
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
           and n.name in ("confirm_panel", "reject_panel")}
    assert set(fns) == {"confirm_panel", "reject_panel"}, f"面板函数没找齐：{sorted(fns)}"
    for name, fn in fns.items():
        calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
                 and getattr(c.func, "id", "") == "expand_collapse_bar"]
        assert calls, f"{name} 没有『全部展开 / 全部收起』条"
        exps = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
                and getattr(c.func, "attr", "") == "expansion"
                and c.args and "quarter_label" in ast.unparse(c.args[0])]   # 只看按季度分的那几组
        assert exps, f"{name} 里找不到按季度分组的 ui.expansion（守卫前提坏了）"
        for e in exps:
            kw = {k.arg: ast.unparse(k.value) for k in e.keywords}
            assert "exp" in kw.get("value", ""), f"{name}:{e.lineno} 的 ui.expansion 没看展开意图（条是死的）"


# ---------------- ux-7 / ux-9 / logic-10：真渲染 ----------------

@pytest.mark.nicegui_main_file(_MAIN)
async def test_jump_menu_trigger_is_focusable(user, clean_tables, cfg):
    """顶栏『跳转到』的 :focus-within 兜底要有可聚焦的触发行，键盘与 ≥1024px 的触屏才打得开。"""
    cfg(QB_ENABLED=False)
    await user.open("/?t=overview")
    labels = [e for e in user.find("跳转到").elements]
    assert labels, "顶栏没有『跳转到』"
    parent = labels[0].parent_slot.parent
    assert str(parent._props.get("tabindex")) == "0", f"触发行不可聚焦：{parent._props}"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_download_status_badges_all_have_tooltips(user, clean_tables, cfg):
    """『下载状态』区五枚徽标里『已完成』『下载中』也要有 tooltip —— 上方文案让用户来对照的正是『已完成』。"""
    from nicegui import ui
    from nicegui.elements.tooltip import Tooltip

    cfg(QB_ENABLED=False)
    for path in ("/?t=overview", "/movies"):
        await user.open(path)
        badges = [b for b in user.find(ui.badge).elements
                  if str(getattr(b, "text", "")).startswith(("已完成", "下载中"))]
        assert len(badges) >= 2, f"{path} 上找不到『已完成』『下载中』徽标"
        # NiceGUI 3.16 的 .tooltip() 不是孩子：它建一个兄弟 Tooltip，props['target'] 指向元素 id
        targets = {t.props.get("target") for t in user.find(Tooltip).elements}
        for b in badges:
            assert f"#{b.html_id}" in targets, f"{path} 的『{b.text}』没有 tooltip"


async def test_relocate_prompt_says_move_back_when_the_path_did_not_change(monkeypatch):
    """目录没变、但盘上有 N 集落在别处（上次没搬成）：确认框要说"搬回来"，而不是"归档目录变了"。"""
    from pages import anime_detail as D

    NEW = "/anime/26B/正确的名字"
    rows = [SimpleNamespace(status="sent", save_path="/anime/26C/错绑时的名字", archived_at=None),
            SimpleNamespace(status="sent", save_path=NEW, archived_at=None)]
    monkeypatch.setattr(D.anime, "anime_save_path", lambda aid: NEW)
    monkeypatch.setattr(D.anime, "list_episodes", lambda aid: rows)
    asked = []

    async def fake_confirm(title, body, **kw):
        asked.append((title, body)); return False
    monkeypatch.setattr(D, "confirm", fake_confirm)
    monkeypatch.setattr(D.ui, "notify", lambda *a, **k: None)

    await D.maybe_relocate_anime(1, NEW)              # old_path == new_path
    assert asked, "目录没变但文件落在别处，却没有提出搬迁（R18 的那一支丢了）"
    title, body = asked[0]
    assert "搬回" in title + body and "1 集" in body, f"确认框还在说『目录变了』：{asked[0]}"
    asked.clear()
    await D.maybe_relocate_anime(1, "/anime/26C/错绑时的名字")   # 正向控制：目录真的变了
    assert asked and "移到新目录" in asked[0][1], asked


# ---------------- ux-1 / ux-2：按钮的 loading 必须真的亮 ----------------

def test_busy_action_handlers_take_the_event_positionally():
    """(AST) 把 e.sender 交给 busy_action 的处理器至少要有一个【无默认值】形参。

    NiceGUI 的 handle_event 用 helpers.expects_arguments 决定传不传事件，判据就是这一条：
    `async def h(e=None)` 会被 handler() 不带参调用，e 恒为 None，busy_action 拿到 btn=None，
    loading 那一支整段跳过 —— 8 个 120~240 秒级的按钮按下去全都毫无反应。
    """
    bad = []
    FN = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
    for rel in sorted((ROOT / "pages").glob("*.py")):
        tree = ast.parse(rel.read_text(encoding="utf-8"))
        parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        for c in ast.walk(tree):
            if not (isinstance(c, ast.Call) and getattr(c.func, "id", "") == "busy_action"
                    and c.args and "sender" in ast.unparse(c.args[0])):
                continue
            fn = parents.get(c)                      # 最近的那一层函数才是 NiceGUI 真正调用的处理器
            while fn is not None and not isinstance(fn, FN):
                fn = parents.get(fn)
            assert fn is not None, f"pages/{rel.name}:{c.lineno} 的 busy_action 不在任何函数里？"
            # 判据逐字对齐 nicegui.helpers.expects_arguments：posonly / 普通 / kwonly 里只要有一个没默认值就算，
            # *args / **kwargs 不算（核实意见：手写判据少算一种参数种类，守卫本身就是假的）
            a = fn.args
            positional = a.posonlyargs + a.args
            required = (len(positional) - len(a.defaults)) + sum(1 for d in a.kw_defaults if d is None)
            if required < 1:
                name = getattr(fn, "name", "<lambda>")
                bad.append(f"pages/{rel.name}:{fn.lineno} {name}({ast.unparse(a)})")
    assert not bad, "这些处理器会被 NiceGUI 不带参调用，e 恒为 None、loading 永远不亮：\n" + "\n".join(bad)


@pytest.mark.nicegui_main_file(_MAIN)
async def test_download_all_button_really_shows_loading(user, clean_tables, cfg, monkeypatch):
    """真点『补下全部』：长操作跑着的时候按钮必须处于 loading（第一版 (e=None) 让它永远不亮）。"""
    from pages import anime as P

    cfg(QB_ENABLED=True)
    await user.open("/?t=overview")
    btn = next(iter(user.find("补下全部").elements))
    seen = []

    async def fake_download_all():
        seen.append(bool(btn._props.get("loading")))
        return 0
    monkeypatch.setattr(P.anime, "download_all_pending", fake_download_all)
    user.find("补下全部").click()
    await user.should_see("已触发补下")
    assert seen == [True], f"补下跑着的时候按钮没在 loading：{seen}"
    assert not btn._props.get("loading"), "跑完没摘掉 loading"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_qb_sync_button_really_shows_loading(user, clean_tables, cfg, monkeypatch):
    """真点『立刻刷新』：qB 不通时这一下最长 45 秒，期间必须有转圈（以前从头到尾没碰过按钮元素）。"""
    from pages import anime as P

    cfg(QB_ENABLED=True)
    await user.open("/?t=overview")
    btn = next(iter(user.find("立刻刷新").elements))
    seen = []

    async def fake_sync(manual=False):
        seen.append(bool(btn._props.get("loading")))
        return 0
    monkeypatch.setattr(P.anime, "sync_qb_status", fake_sync)
    user.find("立刻刷新").click()
    await user.should_see("正在向 qB 拉最新进度")      # 立刻出现的 toast：面板被 30 秒定时器重建后 loading 就没了，它才盖得住 45 秒
    await user.should_see("没有正在下载的种子")
    assert seen == [True], f"同步跑着的时候按钮没在 loading：{seen}"
    assert not btn._props.get("loading"), "跑完没摘掉 loading"


# ---------------- ux-5：仪表盘要说"上一轮采集发生了什么" ----------------

async def test_poll_round_records_when_it_ran_and_which_sources_failed(clean_tables, monkeypatch, cfg):
    """一轮采集结束要落两个运行时键：结束时间、抓取失败的源名。失败以前只进 200 条的环形日志。"""
    from core import worker as W

    class Good:
        name = "好源"

        async def fetch(self, content_cache=None):
            return []

    class Bad:
        name = "坏源"

        async def fetch(self, content_cache=None):
            raise RuntimeError("403 Forbidden")

    async def fake_precheck():
        return False
    monkeypatch.setattr(W, "build_sources", lambda: [Good(), Bad()])
    monkeypatch.setattr(W.anime, "qb_precheck", fake_precheck)
    cfg(ANIME_POLL_LAST="", ANIME_POLL_FAILED="")

    cfg(ANIME_POLL_OK_LAST="")
    await W.poll_once()
    assert config.ANIME_POLL_LAST, "轮末没记结束时间"
    datetime.fromisoformat(config.ANIME_POLL_LAST)
    assert config.ANIME_POLL_FAILED == "坏源", f"失败源没记对：{config.ANIME_POLL_FAILED!r}"
    assert config.ANIME_POLL_OK_LAST == config.ANIME_POLL_LAST, "有一个源抓到了，『上次至少一个源抓到』该跟着更新"

    monkeypatch.setattr(W, "build_sources", lambda: [Bad()])
    cfg(ANIME_POLL_OK_LAST="2026-01-01T00:00:00")
    await W.poll_once()
    assert config.ANIME_POLL_OK_LAST == "2026-01-01T00:00:00", "一个源都没抓到，『上次至少一个源抓到』不该前进"

    monkeypatch.setattr(W, "build_sources", lambda: [Good()])
    await W.poll_once()
    assert config.ANIME_POLL_FAILED == "", "全部成功的那一轮没把失败清空"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_dashboard_shows_last_poll_and_failed_sources(user, clean_tables, cfg):
    """『采集状态』区以前四枚全是配置的镜像：6 条 feed 全 403 时与正常时逐字相同。"""
    from nicegui import ui
    from nicegui.elements.tooltip import Tooltip

    cfg(QB_ENABLED=False, ANIME_POLL_LAST=datetime.now().replace(microsecond=0).isoformat(),
        ANIME_POLL_FAILED="喵萌(繁中)\nMikan(后备)")
    await user.open("/?t=overview")
    await user.should_see("上次采集")
    await user.should_see("2 个源抓取失败")
    tips = " ".join(str(getattr(t, "text", "")) for t in user.find(Tooltip).elements)
    assert "喵萌(繁中)" in tips and "Mikan(后备)" in tips, "失败的源名没在 tooltip 里"

    cfg(ANIME_POLL_FAILED="")
    await user.open("/?t=overview")
    await user.should_not_see("个源抓取失败")

    def last_badge():
        return next(b for b in user.find(ui.badge).elements if str(getattr(b, "text", "")).startswith("上次采集"))
    assert last_badge()._props.get("color") == "blue-grey"
    # 陈旧：超过两个轮询间隔没有完成新的一轮（采集关着 / 库停摆 / 上一轮被打断）→ 必须上红
    cfg(ANIME_POLL_LAST=(datetime.now() - timedelta(seconds=3 * config.ANIME_POLL_INTERVAL)).isoformat(timespec="seconds"))
    await user.open("/?t=overview")
    assert last_badge()._props.get("color") == "red", "时间戳冻住了徽标还是中性色 —— 坏了和好着又长得一样"


async def test_a_source_group_that_fails_to_build_counts_as_failed_too(clean_tables, monkeypatch, cfg):
    """(核实意见) 分母要含构造阶段就被跳过的组：nyaa 组 feed 为空在 build_sources 里就 raise、连 fetch 都到不了，
    徽标上不能把它当成"全部成功"。"""
    from core import worker as W
    from db.models import SourceGroup

    with clean_tables.get_session() as s:
        s.add(SourceGroup(name="坏配置的组", site="nyaa", feed="", policy="auto", priority=0))
        s.commit()

    async def fake_precheck():
        return False
    monkeypatch.setattr(W.anime, "qb_precheck", fake_precheck)
    cfg(ANIME_POLL_LAST="", ANIME_POLL_FAILED="")
    await W.poll_once()
    assert "坏配置的组" in config.ANIME_POLL_FAILED.split("\n"), f"构造阶段跳过的组在徽标上隐形：{config.ANIME_POLL_FAILED!r}"
