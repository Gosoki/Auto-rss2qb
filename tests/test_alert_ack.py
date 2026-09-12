"""(R35) 三类『只报不改』的巡检发现的已读归档。

用户的原话：「这些找个地方收起来吧，做一个已读的归档，不然越积越多」。
设计经 4 方案 × 4 视角的评审打磨过，下面每条守卫都对着评审实测出的一个失效形状。
"""
import ast
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import select

from core import alerts
from db.models import AlertAck, Anime, AnimeAlias, AnimeTorrent, Movie, MovieTorrent


def _mva_pair(db, *, confirmed=False, rejected=True):
    """造一对"同 bgm 既是番又是剧场版"。返回 (anime_id, movie_id)。"""
    with db.get_session() as s:
        a = Anime(title="剧场版 X", display_name="剧场版 X", season=1, quarter="26A",
                  confirmed=confirmed, rejected=rejected, bangumi_id=583746)
        m = Movie(title="X 剧场版", display_name="X 剧场版", quarter="2026", bangumi_id=583746)
        s.add(a); s.add(m); s.commit(); s.refresh(a); s.refresh(m)
        s.add(MovieTorrent(movie_id=m.id, info_hash="7" * 40, raw_title="mv", status="pending"))
        s.commit()
        return a.id, m.id


def _wrb_anime(db, eps=(1, 2, 3), *, title="某番"):
    """造一部『绑定看着不对』的番：本季 total=12、却收到发布时间远早于本季的正片。

    info_hash 按番名派生 —— 它在 animetorrent 上有全局唯一约束，两部番各自从 0 编号会撞。
    """
    import hashlib
    pre = hashlib.blake2s(title.encode(), digest_size=8).hexdigest()
    with db.get_session() as s:
        a = Anime(title=title, display_name=title, season=3, quarter="26C", confirmed=True,
                  rejected=False, bangumi_id=598058, total_episodes=12,
                  air_date="2026-07-05")
        s.add(a); s.commit(); s.refresh(a)
        for i, ep in enumerate(eps):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{pre}{i:024x}",
                               raw_title=f"[组] - {ep}",
                               episode=float(ep), status="pending",
                               release_time=datetime(2024, 1, 1)))
        s.commit()
        return a.id


def _ack_all():
    for f in alerts.scan():
        alerts.ack(f["ident"], f["kind"], f["fact"], f["text"])


# ---------------------------------------------------------------- 基本行为

def test_ack_hides_only_that_one(clean_tables):
    """读过一条，只有那一条被收起来 —— 不是"这一类"全收。"""
    a1 = _wrb_anime(clean_tables, title="番甲")
    _wrb_anime(clean_tables, title="番乙")
    found = alerts.scan()
    assert len(found) == 2, f"前提：应当有两条发现，实际 {found}"
    mine = next(f for f in found if f["ident"] == f"wrb:{alerts._tag(a1, _born(clean_tables, a1))}")
    alerts.ack(mine["ident"], mine["kind"], mine["fact"], mine["text"])

    live = alerts.view()["live"]
    assert len(live) == 1, f"应当只收起一条，实际剩 {len(live)}"
    assert live[0]["ident"] != mine["ident"]


def _born(db, anime_id):
    with db.get_session() as s:
        return s.get(Anime, anime_id).created_at


def test_cosmetic_change_keeps_it_acked(clean_tables):
    """采集噪声不该让它回来 —— 用户点名的那一类。

    剧场版又收到一条种子（mn 1→2）、番名被 bgm 回填改写，都不是新事实。
    """
    aid, mid = _mva_pair(clean_tables)
    _ack_all()
    assert alerts.view()["live"] == []

    with clean_tables.get_session() as s:
        s.add(MovieTorrent(movie_id=mid, info_hash="8" * 40, raw_title="mv2", status="pending"))
        m = s.get(Movie, mid)
        m.display_name = "X 剧场版（港译）"
        a = s.get(Anime, aid)
        a.display_name = "剧场版 X（港译）"
        s.add(m); s.add(a); s.commit()

    assert alerts.view()["live"] == [], "采到一条新种子/改个名就让用户重点一次『知道了』"


def test_state_change_revives_it(clean_tables):
    """番剧那条从『超期忽略』变成『追番中』必须回来 —— 危险正是在这一刻变成真的。"""
    aid, _ = _mva_pair(clean_tables, confirmed=False, rejected=True)
    _ack_all()
    assert alerts.view()["live"] == []

    with clean_tables.get_session() as s:
        a = s.get(Anime, aid)
        a.confirmed, a.rejected = True, False       # 『恢复订阅』
        s.add(a); s.commit()

    live = alerts.view()["live"]
    assert len(live) == 1, "订阅态翻转了却没回来"
    changed = live[0]["revived"]["changed"]
    assert any("订阅状态" in k and n == "追番中" for k, _o, n in changed), changed


def test_confirmed_alone_revives_a_duplicate_pair(clean_tables):
    """(评审实测的致命形状) `confirmed` 单独翻转也必须复活。

    `confirmed 0→1` 在入库链路上是**自动**发生的。一对"待确认+待确认"的重复番被读过之后，
    后台会把它们各自升成追番中 —— 那时同一部番的集会跨两条记录各下一份
    （集去重键是 (anime_id, episode)，跨记录失效）。
    指纹若只吃 `rejected`、或吃 `a_state` 那个三态展示串，这一位一个字都不动。
    """
    with clean_tables.get_session() as s:
        x = Anime(title="番 X", season=1, quarter="26A", confirmed=False, rejected=False,
                  bangumi_id=111)
        y = Anime(title="番 Y", season=1, quarter="26A", confirmed=False, rejected=False,
                  bangumi_id=222)
        s.add(x); s.add(y); s.commit(); s.refresh(x); s.refresh(y)
        # 【别名的唯一键就是 (title, season)】两条番不可能存下逐字相同的别名 ——
        # 真库那对（#60/#86）正是靠 `canonical_alias` 剥掉制作公司前缀之后才撞上的。
        s.add(AnimeAlias(title="同一个名字", season=1, anime_id=x.id))
        s.add(AnimeAlias(title="Animatica「同一个名字」", season=1, anime_id=y.id))
        s.commit()
        xid = x.id
    assert len(alerts.scan()) == 1, "前提：应当报出这一对"
    _ack_all()
    assert alerts.view()["live"] == []

    with clean_tables.get_session() as s:          # 后台自动升确认，rejected 一动不动
        a = s.get(Anime, xid)
        a.confirmed = True
        s.add(a); s.commit()

    live = alerts.view()["live"]
    assert len(live) == 1, "confirmed 自动升上去了却没复活 —— 两条记录会各下一份"
    assert any("订阅状态" in k for k, _o, _n in live[0]["revived"]["changed"])


def test_same_episode_again_is_silent_but_a_new_one_revives(clean_tables):
    """坏集号的**集合**才是判据，不是坏种子的条数。

    同一集又来一个字幕组的版本（条数 +1、集合不变）是常态噪声；
    出现一个**新的**坏集号才是"又来了一批"。
    """
    aid = _wrb_anime(clean_tables, eps=(1, 2, 3))
    _ack_all()

    with clean_tables.get_session() as s:          # 同一集的另一个版本
        s.add(AnimeTorrent(anime_id=aid, info_hash="a" * 40, raw_title="[另一个组] - 3",
                           episode=3.0, status="pending", release_time=datetime(2024, 1, 1)))
        s.commit()
    assert alerts.view()["live"] == [], "同一集多一个版本就让用户重点一次"

    with clean_tables.get_session() as s:          # 新的坏集号
        s.add(AnimeTorrent(anime_id=aid, info_hash="b" * 40, raw_title="[组] - 9",
                           episode=9.0, status="pending", release_time=datetime(2024, 1, 1)))
        s.commit()
    live = alerts.view()["live"]
    assert len(live) == 1, "出现新的坏集号却没复活"
    assert any("集号" in k for k, _o, _n in live[0]["revived"]["changed"])


def test_the_single_episode_criterion_also_has_a_live_fingerprint(clean_tables):
    """(评审实测的致命形状) `_binding_looks_wrong_rows` 的**第二条**判据也要能复活。

    判据②是「total_episodes==1 却收到多个正片集号」（绑到了单集特典）。它命中时
    坏集号恒为空 —— 指纹若只装坏集号，这一整类读过一次就**永远**不会再报，
    哪怕后来又多收了十集。
    """
    with clean_tables.get_session() as s:
        a = Anime(title="绑到特典的番", season=1, quarter="26C", confirmed=True, rejected=False,
                  bangumi_id=664060, total_episodes=1)
        s.add(a); s.commit(); s.refresh(a)
        for ep in (1, 2):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{ep:040x}", raw_title=f"x - {ep}",
                               episode=float(ep), status="pending"))
        s.commit()
        aid = a.id
    found = alerts.scan()
    assert len(found) == 1 and found[0]["kind"] == "wrb", found
    assert found[0]["fact"].endswith("eps=1,2"), f"判据②的指纹是空的：{found[0]['fact']}"
    _ack_all()
    assert alerts.view()["live"] == []

    with clean_tables.get_session() as s:          # 又收到第 3 集
        s.add(AnimeTorrent(anime_id=aid, info_hash="c" * 40, raw_title="x - 3",
                           episode=3.0, status="pending"))
        s.commit()
    assert len(alerts.view()["live"]) == 1, "判据②那一类读过一次就永远不会再报了"


def test_a_rebuilt_row_with_the_same_id_is_reported_again(clean_tables):
    """身份要带出生时刻：删掉再建回来的是**另一条**发现，必须重新报。

    SQLite 的整型主键会回收（不带 AUTOINCREMENT），而"删掉一条番"是个现成的按钮。
    只按 id 认身份的话，一条全新的发现会被上一条的已读静默吃掉。
    """
    aid = _wrb_anime(clean_tables)
    _ack_all()
    assert alerts.view()["live"] == []

    with clean_tables.get_session() as s:          # 删掉，再用同一个 id 建一条新的
        for t in s.exec(select(AnimeTorrent)):
            s.delete(t)
        s.delete(s.get(Anime, aid))
        s.commit()
        a = Anime(id=aid, title="另一部番", display_name="另一部番", season=3, quarter="26C",
                  confirmed=True, rejected=False, bangumi_id=598058, total_episodes=12,
                  air_date="2026-07-05")
        s.add(a); s.commit()
        for i, ep in enumerate((5, 6)):
            s.add(AnimeTorrent(anime_id=aid, info_hash=f"{i + 50:040x}", raw_title=f"y - {ep}",
                               episode=float(ep), status="pending",
                               release_time=datetime(2024, 1, 1)))
        s.commit()

    live = alerts.view()["live"]
    assert len(live) == 1 and live[0]["revived"] is None, \
        "同一个 id 的新行被上一条的已读吃掉了（或被当成复活而不是全新一条）"


def test_unack_brings_it_straight_back(clean_tables):
    """取消已读＝它立刻回到仪表盘。可逆，且方向是"重新出现"。"""
    _wrb_anime(clean_tables)
    _ack_all()
    assert alerts.view()["live"] == []
    ident = alerts.view()["acked"][0]["ident"]
    assert alerts.unack(ident) is True
    assert len(alerts.view()["live"]) == 1
    assert alerts.unack(ident) is False, "已经没有了还报成删掉了"


def test_acked_list_tells_whether_it_still_holds(clean_tables):
    """已读列表要说清每条此刻还成不成立；`drop_gone` 只清不成立的那些。"""
    aid = _wrb_anime(clean_tables)
    _ack_all()
    assert [r["state"] for r in alerts.view()["acked"]] == ["fresh"]

    with clean_tables.get_session() as s:          # 把那批坏种子删掉＝用户处理完了
        for t in s.exec(select(AnimeTorrent)):
            s.delete(t)
        s.commit()
    assert alerts.scan() == []
    assert [r["state"] for r in alerts.view()["acked"]] == ["gone"]
    assert alerts.drop_gone() == 1
    assert alerts.view()["acked"] == []

    _wrb_anime(clean_tables, title="还活着的")     # 反向：还成立的那条不许被清掉
    _ack_all()
    assert alerts.drop_gone() == 0, "把还成立的已读记录也清掉了"
    assert len(alerts.view()["acked"]) == 1


def test_ack_twice_keeps_one_row(clean_tables):
    """重复点收敛成一条（upsert），别在表里越积越多。"""
    _wrb_anime(clean_tables)
    _ack_all(); _ack_all(); _ack_all()
    with clean_tables.get_session() as s:
        assert len(list(s.exec(select(AlertAck)))) == 1


# ---------------------------------------------------------------- 广度：推送那一路

async def test_ack_also_silences_the_push(clean_tables, cfg, monkeypatch):
    """(广度守卫) 已读必须**同时**在推送上生效 —— 三类里只有 wrb 有推送通道。

    只让仪表盘认『知道了』的话，横幅没了而手机每 6 小时照旧响一次 ——
    同一个决定只落一处，本项目第①号缺陷形状。

    【这条守卫写不好就是假的】(评审实测) `services.notify` 的冷却按 (kind, key) 记在
    **进程内存**里。照着"跑两次 sweep_alerts、断言第二次没发"去写的话，有缺陷与无缺陷
    两种实现都不会发（第二次被冷却吃掉），用例恒绿。所以这里：
      ① 每个断言前把冷却清干净；② 断言的是**发出去的正文**，不是"什么都没发生"；
      ③ 带正向控制 —— 没读过的时候必须发。
    """
    from core import anime as A
    from services import notify

    sent = []

    async def fake_event(kind, text, key=None, cooldown=0):
        sent.append(text)
        return True

    monkeypatch.setattr(A, "notify_event", fake_event)
    cfg(NOTIFY_BACKLOG_MIN=999)          # 把"待识别积压"那条状态通知挡掉，只留我们要看的

    def _run():
        sent.clear()
        notify._last_sent.clear()        # 冷却是进程内的，不清就分不出"没发"和"被冷却吃了"
        A._wb_notified = set()
        import asyncio
        asyncio.get_event_loop()
        return sent

    _wrb_anime(clean_tables)

    _run()
    await A.sweep_alerts()
    assert any("绑定看着不对" in t for t in sent), "正向控制：没读过的时候本来就该发"

    _ack_all()
    _run()
    await A.sweep_alerts()
    assert not any("绑定看着不对" in t for t in sent), \
        "点了『知道了』，手机还是会响 —— 已读只落在仪表盘上"


async def test_acking_one_of_many_does_not_trigger_a_new_push(clean_tables, cfg, monkeypatch):
    """(评审实测的致命形状) 用户点『知道了』，**不能**因此收到一条新推送。

    推送的去重键是"这一批是哪几条"的 id 集合哈希。归档一条会让集合从 {a,b,c} 变成 {a,b} ——
    那是个从没发过的新 key，于是下一轮巡检因为用户点了知道了而推一条新通知。
    集合变【小】从来不是"有新事情要你处理"。
    """
    from core import anime as A
    from services import notify

    sent = []

    async def fake_event(kind, text, key=None, cooldown=0):
        sent.append(text)
        return True

    monkeypatch.setattr(A, "notify_event", fake_event)
    cfg(NOTIFY_BACKLOG_MIN=999)
    a1 = _wrb_anime(clean_tables, title="番甲")
    _wrb_anime(clean_tables, title="番乙")

    notify._last_sent.clear(); A._wb_notified = set(); sent.clear()
    await A.sweep_alerts()
    assert any("绑定看着不对" in t for t in sent), "前提：第一轮该发"

    f = next(x for x in alerts.scan()
             if x["ident"] == f"wrb:{alerts._tag(a1, _born(clean_tables, a1))}")
    alerts.ack(f["ident"], f["kind"], f["fact"], f["text"])

    notify._last_sent.clear()            # 把冷却清掉：现在只看"该不该发"，不靠冷却兜
    sent.clear()
    await A.sweep_alerts()
    assert not any("绑定看着不对" in t for t in sent), \
        "用户点了『知道了』，反而收到一条新推送"

    _wrb_anime(clean_tables, title="番丙")          # 反向：真有新的一条，还是要发
    notify._last_sent.clear(); sent.clear()
    await A.sweep_alerts()
    assert any("绑定看着不对" in t for t in sent), "出现了新的一条却不发了"


# ---------------------------------------------------------------- 结构守卫

def test_the_archive_list_is_a_dialog_not_an_inline_expansion():
    """(评审实测的致命形状) 已读列表必须是对话框，不能是就地展开的面板。

    仪表盘的 `overview_head` 每 30 秒被 `ui.timer` 重建一次。放一个 `ui.expansion` 在里面的话，
    用户展开去点『取消已读』，最多 30 秒就被合上 —— R27 已经为完全同构的 bug 付过一次学费
    （见 pages/anime.py 里 manage_panel 那段 docstring）。而现有的"哪些面板不许放交互态"
    守卫的名单是硬编码的、不含 overview_head，它不会说话。
    """
    src = (Path(__file__).resolve().parent.parent / "pages/anime.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    head = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "overview_head"), None)
    assert head is not None, "overview_head 改名了？这条守卫的前提坏了"
    bad = [n.lineno for n in ast.walk(head) if isinstance(n, ast.Call)
           and getattr(n.func, "attr", "") in ("expansion", "dialog")]
    assert not bad, f"overview_head 里出现了就地展开/对话框构造（第 {bad} 行）：30 秒后会被重建掉"
    # 正向：已读列表确实存在，且走的是既有的那个对话框
    opener = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "_open_acked"), None)
    assert opener is not None, "找不到 _open_acked —— 已读列表没了？"
    assert any(isinstance(n, ast.Attribute) and n.attr == "clear"
               and getattr(n.value, "id", "") == "list_dlg" for n in ast.walk(opener)), \
        "_open_acked 没有用既有的 list_dlg 对话框"


def test_the_banner_text_has_exactly_one_definition():
    """三段横幅文案只许有一份（在 core/alerts）。

    它要被用在三个地方：横幅、已读列表里的存档、以及推送。留在页面里就得抄第二遍，
    而两份文案必然漂移 —— 那时已读列表里显示的是另一套措辞。
    """
    page = (Path(__file__).resolve().parent.parent / "pages/anime.py").read_text(encoding="utf-8")
    for phrase in ("多半是同一部番被拆成了两条", "各有一条记录", "多半是 bgm 绑错了季"):
        assert phrase not in page, f"横幅文案又长回页面里了：{phrase!r}"
    core = (Path(__file__).resolve().parent.parent / "core/alerts.py").read_text(encoding="utf-8")
    for phrase in ("多半是同一部番被拆成了两条", "各有一条记录", "多半是 bgm 绑错了季"):
        assert phrase in core, f"core/alerts 里找不到这句文案：{phrase!r}"


def test_every_kind_has_a_full_construction():
    """加一类发现就必须同时给出 判据/身份/指纹/文案 —— 少一样当场红。"""
    assert set(alerts._BUILD) == set(alerts.KINDS)
    for kind, (find, build) in alerts._BUILD.items():
        assert callable(find) and callable(build), kind


@pytest.mark.parametrize("kind", ["dup", "mva", "wrb"])
def test_no_fingerprint_uses_a_display_string(kind):
    """(AST) 指纹里不许出现展示串 `a_state` —— 它不是单射。

    `(confirmed=0, rejected=0)` 与 `(0,1)` 都映成"超期忽略/待确认"，而横幅尾巴只读 rejected。
    拿它当指纹，"那部番从待确认掉成超期忽略"（它下面的集从此一集都不会被下）一个字都不动。
    """
    src = (Path(__file__).resolve().parent.parent / "core/alerts.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == f"_{kind}")
    # 找到 fact 那个键的值，扫它用到的所有下标
    fact = None
    for d in ast.walk(fn):
        if isinstance(d, ast.Dict):
            for k, v in zip(d.keys, d.values):
                if isinstance(k, ast.Constant) and k.value == "fact":
                    fact = v
    assert fact is not None, f"_{kind} 里找不到 fact"
    keys = {n.slice.value for n in ast.walk(fact)
            if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)}
    assert "a_state" not in keys, f"_{kind} 的指纹吃了展示串 a_state：{sorted(keys)}"
    assert keys, f"_{kind} 的指纹没有用到任何字段？"
