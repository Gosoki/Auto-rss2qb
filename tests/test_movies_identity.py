"""剧场版的身份归并：认错人的代价是【删掉正主那一行】。

剧场版线只有 rejected 一个人工状态，识别→合并的链路比 TV 短，但也因此少了几道护栏。
"""
from datetime import datetime

import pytest
from sqlmodel import select

from core import movies as M
from db.models import Movie, MovieTorrent


@pytest.fixture
def two_movies(clean_tables):
    with clean_tables.get_session() as s:
        a = Movie(title="正主", quarter="26C", bangumi_id=None, rejected=True)
        b = Movie(title="幽灵", quarter="26C", bangumi_id=None)
        s.add(a)
        s.add(b)
        s.commit()
        s.refresh(a)
        s.refresh(b)
        s.add(MovieTorrent(movie_id=b.id, info_hash="b" * 40, raw_title="幽灵的版本",
                           status="pending", created_at=datetime.now()))
        s.commit()
        return a.id, b.id


async def test_manual_bind_undoes_inherited_rejection(clean_tables, two_movies, monkeypatch):
    """(R3) 合并规则是"两方任一被忽略则仍忽略"——那对后台归并是对的，
    但【手动点绑定】是一个明确的"我要这部片"的动作。继承过来之后 UI 弹绿色"识别成功 ✓"，
    片却从『列表』和『待识别』同时消失，只能去『已忽略』tab 找。"""
    rejected_id, target_id = two_movies
    with clean_tables.get_session() as s:
        s.get(Movie, rejected_id).bangumi_id = 12345
        s.commit()

    async def fake(bgm_id):
        return {"bangumi_id": 12345, "display_name": "片名", "air_date": "2026-07-01",
                "quarter": "26C", "platform": "剧场版"}
    monkeypatch.setattr(M.enrich, "fetch_by_id", fake)
    assert await M.bind_movie_bgm(target_id, 12345) is True
    with clean_tables.get_session() as s:
        assert s.get(Movie, target_id).rejected is not True, "手动绑定后不该是已忽略"


def test_merge_moves_torrents_and_deletes_loser(clean_tables, two_movies):
    keeper_id, loser_id = two_movies
    with clean_tables.get_session() as s:
        M._merge_movie(s, loser_id, keeper_id)
    with clean_tables.get_session() as s:
        assert s.get(Movie, loser_id) is None
        assert s.exec(select(MovieTorrent)).one().movie_id == keeper_id


def test_background_merge_still_inherits_rejection(clean_tables, two_movies):
    """后台归并保持原规则：用户忽略过的片不该因为被合并而复活。"""
    rejected_id, other_id = two_movies
    with clean_tables.get_session() as s:
        M._merge_movie(s, rejected_id, other_id)
        assert s.get(Movie, other_id).rejected is True


def test_merge_refuses_when_both_sides_have_files(clean_tables):
    """(R4/D3) 合并会【删行】且不可逆，而触发它的是一次可能出错的自动识别。
    同系列的续作/重制版/总集编彼此极像，认错一次就是把一部【已经下完】的片
    连同版本记录一起删掉，而用户看到的只是一句"识别成功 ✓"。
    两边都有文件时几乎必然是认错了——宁可留下两行让人看见。"""
    from datetime import datetime as _dt
    with clean_tables.get_session() as s:
        a, b = Movie(title="甲", quarter="26C"), Movie(title="乙", quarter="26C")
        s.add(a)
        s.add(b)
        s.commit()
        s.refresh(a)
        s.refresh(b)
        for i, m in enumerate((a, b)):
            s.add(MovieTorrent(movie_id=m.id, info_hash=f"{i:040x}", raw_title=f"v{i}",
                               status="sent", created_at=_dt.now()))
        s.commit()
        ida, idb = a.id, b.id
        M._merge_movie(s, ida, idb)
    with clean_tables.get_session() as s:
        assert s.get(Movie, ida) is not None, "两边都已下过时不该删掉任何一行"
        assert s.get(Movie, idb) is not None


def test_merge_proceeds_when_only_one_side_has_files(clean_tables):
    """一边空着时合并是安全的——那本来就是同一部片裂成了两行。"""
    from datetime import datetime as _dt
    with clean_tables.get_session() as s:
        a, b = Movie(title="幽灵", quarter="26C"), Movie(title="正主", quarter="26C")
        s.add(a)
        s.add(b)
        s.commit()
        s.refresh(a)
        s.refresh(b)
        s.add(MovieTorrent(movie_id=b.id, info_hash="e" * 40, raw_title="v",
                           status="sent", created_at=_dt.now()))
        s.commit()
        ida, idb = a.id, b.id
        M._merge_movie(s, ida, idb)
    with clean_tables.get_session() as s:
        assert s.get(Movie, ida) is None and s.get(Movie, idb) is not None


def test_upsert_steals_mikan_id_from_a_stale_row(clean_tables):
    """mikan_id 是唯一索引，而 _upsert_movie 会【无条件覆写】它——必须先从旧行上摘下来（D-12）。

    真实链路：某片早期扫进来时 bgm 没识别出来（只有 mikan_id=1001），后来它被识别出
    bgm_id=777，而库里另有一行已经挂着 bgm 777。这时 upsert 按 bgm_id 命中后一行，
    再把 mikan_id=1001 写上去——若不先摘，就是 IntegrityError，
    表现为"这部片每一轮扫描都失败"，日志只有一行『处理剧场版失败』。
    """
    with clean_tables.get_session() as s:
        old = Movie(title="早期扫进来的", quarter="26A", mikan_id="1001")
        known = Movie(title="已识别的", quarter="26A", mikan_id="1000", bangumi_id=777)
        s.add(old)
        s.add(known)
        s.commit()
        old_id, known_id = old.id, known.id

    mid, is_new = M._upsert_movie("1001", "已识别的", 777, None, "剧场版")

    assert mid == known_id and not is_new
    with clean_tables.get_session() as s:
        assert s.get(Movie, known_id).mikan_id == "1001"
        stale = s.get(Movie, old_id)
        # 那行要么被身份守卫合并掉（同 bgm_id），要么还在但已失去 Mikan 链接——
        # 两种都可以，唯独不能是"两行都挂着 1001"。
        assert stale is None or stale.mikan_id is None


async def test_refresh_versions_needs_a_mikan_id(clean_tables):
    """没有 mikan_id 的片（早期入库/合并时保留了另一侧）点『刷新版本』要给明确回话，不能静默。"""
    with clean_tables.get_session() as s:
        m = Movie(title="没链接的", quarter="26A")
        s.add(m)
        s.commit()
        mid = m.id
    r = await M.refresh_movie_torrents(mid)
    assert r["ok"] is False and "Mikan" in r["msg"]


async def test_refresh_versions_stores_new_torrents(clean_tables, monkeypatch):
    """有 mikan_id 就按需重拉一次该片的 RSS，只新增没见过的版本。"""
    from sources.base import ParsedItem
    with clean_tables.get_session() as s:
        m = Movie(title="有链接的", quarter="26A", mikan_id="2000")
        s.add(m)
        s.commit()
        mid = m.id

    def _item(h):
        return ParsedItem(info_hash=h, raw_title=f"[组] 片 [{h[:4]}]", anime_title="片",
                          season=1, episode=-1, quarter="26A", release_time=datetime.now(),
                          download_url=f"http://x/{h}.torrent", source="组", site="mikan")

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(M.mikan, "make_client", lambda: _FakeClient())
    monkeypatch.setattr(M.mikan, "fetch_bangumi_torrents",
                        lambda c, i: _await([_item("a" * 40), _item("b" * 40)]))
    r = await M.refresh_movie_torrents(mid)
    assert r["ok"] and r["seen"] == 2 and r["added"] == 2
    r2 = await M.refresh_movie_torrents(mid)          # 再点一次：站上没新的
    assert r2["seen"] == 2 and r2["added"] == 0


async def _await(v):
    return v


def test_background_scan_never_moves_the_folder_of_a_downloaded_movie(clean_tables, cfg):
    """后台扫描不许改已下片的归档目录（D-14）。

    这部片在 bgm 还没识别出来时就下过了：jp_name/quarter 都是空的，文件落在
    `.../unknown/<种子原名>/`。下一轮扫描 bgm 成功了——若把这两个字段填上，归档目录就整个
    换了地方，而盘上的文件没人去搬（后台扫描这条链路没有 UI、不 relocate），
    页面显示的目录与实际落地从此分家。要挪目录得走详情页『重新识别』（那条带 relocate）。
    """
    cfg(DOWN_PATH="/data")           # D-11 之后默认是空串；不设根目录 movie_save_path 恒为 None，
    with clean_tables.get_session() as s:   # 那样最后那句路径断言就是 None == None，白过
        m = Movie(title="早下的片", quarter="", jp_name=None, mikan_id="3000")
        s.add(m)
        s.commit()
        s.add(MovieTorrent(movie_id=m.id, info_hash="c" * 40, raw_title="[组] 早下的片",
                           status="sent", created_at=datetime.now()))
        s.commit()
        mid = m.id
    before = M.movie_save_path(mid)
    assert before is not None, "用例前提不成立：算不出归档目录"

    M._upsert_movie("3000", "早下的片", None,
                    {"quarter": "26C", "jp_name": "早ク下ノ片", "display_name": "早下的片"}, "剧场版")

    with clean_tables.get_session() as s:
        row = s.get(Movie, mid)
        assert row.quarter == "" and row.jp_name is None, "后台扫描把已下片的归档字段改了"
    assert M.movie_save_path(mid) == before


async def test_background_reenrich_never_moves_a_downloaded_anime_folder(clean_tables, cfg, monkeypatch):
    """后台重识别不许改已下【番剧】的归档目录——与剧场版侧同一条契约（P1-5）。

    一部还没识别出 bgm 就被人工点下过的番：jp_name 空、文件落在 <根>/<季度>/<种子解析名>/。
    后台 retry_unmatched 识别成功后若把 jp_name 填上，新集就去了日文原名的目录，
    已下的集留在旧目录，同一部番裂成两个文件夹，全程无提示；而这条链路不 relocate。
    """
    from datetime import datetime as _dt

    from core import anime as A
    from db.models import Anime, AnimeTorrent

    cfg(DOWN_PATH="/data")           # D-11 之后默认是空串，不设根目录就算不出路径
    with clean_tables.get_session() as s:
        a = Anime(title="Kuma Bear", display_name="Kuma Bear", quarter="26C", confirmed=True)
        s.add(a)
        s.commit()
        s.add(AnimeTorrent(anime_id=a.id, info_hash="d" * 40, raw_title="[组] Kuma Bear [01]",
                           episode=1, status="sent", created_at=_dt.now()))
        s.commit()
        aid = a.id
    before = A.anime_save_path(aid)
    assert before is not None, "用例前提不成立：算不出归档目录"

    async def _fake_resolve(*a_, **k):
        return {"display_name": "くまクマ熊ベアー", "jp_name": "くまクマ熊ベアー",
                "quarter": "25A", "bangumi_id": 12345}
    monkeypatch.setattr(A.enrich, "resolve", _fake_resolve)

    # 【走真实入口，不要自己把 freeze_empty_path 传进去】那样是在测我传的参数：
    # 实测把两个调用点的 freeze_empty_path=True 同时删掉，全套用例照样全绿，
    # 而 retry_unmatched() 与 reenrich_scope(None) 都会把归档目录改掉。
    with clean_tables.get_session() as s:
        row = s.get(Anime, aid)
        row.bangumi_id = None                # 让它进得了 retry_unmatched 的候选池
        row.enrich_tries = 0
        s.commit()
    assert await A.retry_unmatched() >= 0
    assert A.anime_save_path(aid) == before, "后台 retry_unmatched 把已下番的归档目录改了"

    assert await A.reenrich_scope(None) >= 0
    assert A.anime_save_path(aid) == before, "批量『重新识别』把已下番的归档目录改了"

    # 带 relocate 的入口不受影响：它要能把目录修正过来，否则详情页那个按钮就成了摆设
    assert await A.manual_enrich(aid)
    assert A.anime_save_path(aid) != before


async def test_stolen_mikan_id_does_not_strand_the_torrents(clean_tables, caplog):
    """(R17) 被摘走 mikan_id 的【无主行】不能丢在原地：那是一条永久孤儿。

    现有的 test_upsert_steals_mikan_id_from_a_stale_row 用了一行【没有种子】的旧行，
    恰好绕开了有代价的那一支。真实形状是：某片早期扫进来时 bgm 没识别出来，
    用户已经手动下过它的版本；后来它被识别出 bgm_id，而库里另有一行挂着那个 bgm。
    摘掉 mikan_id 之后那行变成"无 mikan_id、无 bgm_id、却挂着种子"——
    『刷新版本』要 mikan_id 所以被禁用，重扫该年份会把 mikan_id 认到 keeper 身上，
    孤儿永远等不到自己的那一轮。共用同一个 mikan_id 就意味着是同一个 Mikan 番组，
    所以正解是把它并进 keeper。
    """
    with clean_tables.get_session() as s:
        old = Movie(title="早期扫进来的", quarter="26A", mikan_id="2001")
        known = Movie(title="已识别的", quarter="26A", mikan_id="2000", bangumi_id=888)
        s.add(old)
        s.add(known)
        s.commit()
        old_id, known_id = old.id, known.id
        s.add(MovieTorrent(movie_id=old_id, raw_title="早就下过的版本",
                           info_hash="f" * 40, status="pending"))
        s.commit()

    mid, is_new = M._upsert_movie("2001", "已识别的", 888, None, "剧场版")
    assert mid == known_id and not is_new

    with clean_tables.get_session() as s:
        assert s.get(Movie, old_id) is None, "无主的旧行被留在原地了"
        rows = list(s.exec(select(MovieTorrent).where(MovieTorrent.movie_id == known_id)))
        assert [t.raw_title for t in rows] == ["早就下过的版本"], \
            f"孤儿的种子没跟过来：{[(t.movie_id, t.raw_title) for t in rows]}"


async def test_stolen_row_with_a_different_bgm_id_is_left_alone(clean_tables):
    """被摘的那行【有自己的 bgm_id】时不能合并——那是两部不同的片碰巧共用过一个 Mikan 链接，
    合并会删掉一整行。失去 Mikan 链接是正确的收场。"""
    with clean_tables.get_session() as s:
        other = Movie(title="另一部片", quarter="26A", mikan_id="2101", bangumi_id=111)
        known = Movie(title="这一部", quarter="26A", mikan_id="2100", bangumi_id=222)
        s.add(other)
        s.add(known)
        s.commit()
        other_id, known_id = other.id, known.id

    M._upsert_movie("2101", "这一部", 222, None, "剧场版")
    with clean_tables.get_session() as s:
        row = s.get(Movie, other_id)
        assert row is not None, "有自己 bgm_id 的另一部片被合并删掉了"
        assert row.mikan_id is None and row.bangumi_id == 111


async def test_a_downloaded_orphan_is_never_deleted(clean_tables, caplog):
    """(R18) loser 已下过、keeper 还没下过 —— 这一半绝不能合并，而 _merge_movie 的保险盖不住它。

    _merge_movie 的保险是"**两边都**下过就拒绝"，而摘 mikan_id 这条路径上 keeper 的典型情形
    恰恰是"刚按 bgm 命中、一份都没下过"，于是保险在这一半上恒为假 —— 而这正是代价最大的一半：
    合并的最后一步是 s.delete(loser)，丢掉的是 Movie 整行（片名/年份/日文名/全部 bgm 元数据），
    页面上那部片直接消失、版本挂到另一部名下，不可逆。
    （种子行会跟过去、save_path 保留，所以盘上文件不打空——丢的是"这是哪部片"这件事。）

    R18 之前这条会静默删行，且日志里一个字都没有。
    """
    with clean_tables.get_session() as s:
        old = Movie(title="早就下完的片", quarter="24D", mikan_id="2301", jp_name="劇場版 前篇")
        known = Movie(title="刚识别出来的", quarter="26A", mikan_id="2300", bangumi_id=1234)
        s.add(old)
        s.add(known)
        s.commit()
        old_id, known_id = old.id, known.id
        s.add(MovieTorrent(movie_id=old_id, raw_title="已下完的版本", info_hash="c" * 40,
                           status="sent", qb_progress=1.0))
        s.commit()

    with caplog.at_level("WARNING"):
        M._upsert_movie("2301", "刚识别出来的", 1234, None, "剧场版")

    with clean_tables.get_session() as s:
        kept = s.get(Movie, old_id)
        assert kept is not None, "已经下完的那部片被静默删掉了"
        assert kept.jp_name == "劇場版 前篇" and kept.quarter == "24D", "元数据没保住"
        assert kept.mikan_id is None, "Mikan 链接本来就该被摘走"
        rows = list(s.exec(select(MovieTorrent).where(MovieTorrent.movie_id == old_id)))
        assert len(rows) == 1, "它的版本记录不该被搬走"
    assert any("已经下过东西" in r.getMessage() for r in caplog.records), \
        f"留下孤儿行时必须留一条可发现的记录：{[r.getMessage() for r in caplog.records]}"


async def test_merge_still_refuses_when_both_sides_downloaded(clean_tables, caplog):
    """两边都已下过东西时仍然拒绝合并——那道保险不能被这条新路径绕过。

    R18 之后这一支其实走不到 _merge_movie 了（上面那条更早的闸先拦住 loser 已下过的情形），
    但结果必须一样：两行都在、都不删。
    """
    with clean_tables.get_session() as s:
        old = Movie(title="早期扫进来的", quarter="26A", mikan_id="2201")
        known = Movie(title="已识别的", quarter="26A", mikan_id="2200", bangumi_id=999)
        s.add(old)
        s.add(known)
        s.commit()
        old_id, known_id = old.id, known.id
        s.add(MovieTorrent(movie_id=old_id, raw_title="A", info_hash="a" * 40, status="sent"))
        s.add(MovieTorrent(movie_id=known_id, raw_title="B", info_hash="b" * 40, status="sent"))
        s.commit()

    with caplog.at_level("WARNING"):
        M._upsert_movie("2201", "已识别的", 999, None, "剧场版")
    with clean_tables.get_session() as s:
        assert s.get(Movie, old_id) is not None, "两边都下过还是被静默合并了"
    assert any(("拒绝合并" in r.getMessage() or "已经下过东西" in r.getMessage())
               for r in caplog.records), \
        f"不合并时必须留下可发现的记录：{[r.getMessage() for r in caplog.records]}"
