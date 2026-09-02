"""采集轮的批量查重：省掉 N 次数据库往返，但不能因此漏判或重复处理。

这一组守的是 R3 那次提速改动——它把"每条 RSS 条目各查一次库"换成了"一批预取一次"，
而批量预取天然有个盲区：预取【之后】才入库的 hash 不在集合里。
"""
from datetime import datetime

import pytest

from core import engine as E
from sqlmodel import select

from core import anime as A
from db.models import Anime, AnimeTorrent


@pytest.fixture
def seeded(clean_tables):
    with clean_tables.get_session() as s:
        a = Anime(title="番", season=1, confirmed=True)
        s.add(a)
        s.commit()
        s.refresh(a)
        for i in range(3):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{i:040x}", raw_title=f"t{i}",
                               episode=i + 1, status="sent", created_at=datetime.now()))
        s.commit()
    return clean_tables


def test_existing_hashes_finds_exactly_the_known_ones(seeded):
    have = [f"{i:040x}" for i in range(3)]
    new = [f"{i:040x}" for i in range(90, 95)]
    assert E.existing_hashes(AnimeTorrent, have + new) == set(have)


def test_existing_hashes_handles_empty_and_none(seeded):
    assert E.existing_hashes(AnimeTorrent, []) == set()
    assert E.existing_hashes(AnimeTorrent, [None, "", None]) == set()


async def test_known_hashes_short_circuits_without_touching_the_db(seeded, monkeypatch):
    """传了预取集合就不该再查库——这正是这次改动要省掉的那次往返。"""
    calls = []
    real = A.get_session

    def counting(*a, **kw):
        calls.append(1)
        return real(*a, **kw)
    monkeypatch.setattr(A, "get_session", counting)

    class Item:
        info_hash = f"{0:040x}"
    assert await A.process_item(Item(), known_hashes={f"{0:040x}"}) is False
    assert calls == [], "命中预取集合时不该开 session"


async def test_without_known_hashes_it_still_queries(seeded):
    """零散入口（手动补齐等）不传集合时，照旧自己查一次，行为不变。"""
    class Item:
        info_hash = f"{0:040x}"
    assert await A.process_item(Item()) is False


# ---------------- (R21) 两条入库路径都必须批量预取 ----------------

def test_movie_store_prefetches_hashes_in_one_query(clean_tables, monkeypatch):
    """剧场版入库不许逐条查重 —— 一批版本只发【一条】查重 SQL。

    R21 之前这里是每个版本各发一条 `SELECT <整行> WHERE info_hash = ?`。
    站上的版本绝大多数上轮已入库，于是每次重扫都把已知 hash 各查一遍：
    真库 70 部 / 569 个版本 = 639 条 SQL、657ms 的**同步阻塞**，而它跑在事件循环上
    （页面、下载放行、qB 同步一起卡）。番剧侧的 poll_once 早就批量预取了 —— 这一半漏了。
    """
    from types import SimpleNamespace

    from sqlalchemy import event

    from core import movies as M
    from db.models import Movie, MovieTorrent

    with clean_tables.get_session() as s:
        m = Movie(title="片", quarter="2026")
        s.add(m); s.commit(); s.refresh(m); mid = m.id
        for i in range(6):     # 一半已入库
            s.add(MovieTorrent(movie_id=mid, info_hash=f"{i:040x}", raw_title=f"v{i}"))
        s.commit()

    items = [SimpleNamespace(info_hash=f"{i:040x}", source="s", site="mikan",
                             raw_title=f"v{i}", download_url="u", release_time=None,
                             priority=0) for i in range(12)]

    seen = []

    @event.listens_for(clean_tables.engine, "before_cursor_execute")
    def _c(conn, cur, statement, params, ctx, many):
        if "info_hash" in statement and statement.lstrip().upper().startswith("SELECT"):
            seen.append(statement.split("\n")[0])

    try:
        n = M._store_movie_torrents(mid, items)
    finally:
        event.remove(clean_tables.engine, "before_cursor_execute", _c)

    assert n == 6, f"应当只入库 6 条新版本，实际 {n}"
    assert len(seen) == 1, (
        f"查重发了 {len(seen)} 条 SQL —— 应当是一条 IN 预取。前几条：{seen[:3]}")


def test_both_ingest_paths_use_the_shared_prefetch():
    """反向：两条线都必须走 `engine.existing_hashes`，别再各写一份逐条查。

    行为用例各测各的一半；这里钉住"只有一份实现、两边都在用"。
    用 AST 查真实调用节点（两个文件的注释里都写着这个函数名，按字符串判会被自己的解释判绿）。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for mod, fn_name in (("core/worker.py", "poll_once"),
                         ("core/movies.py", "_store_movie_torrents")):
        tree = ast.parse((root / mod).read_text(encoding="utf-8"))
        fns = [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name]
        assert fns, f"没找到 {mod}::{fn_name}，用例的前提坏了"
        attrs = {n.attr for n in ast.walk(fns[0]) if isinstance(n, ast.Attribute)}
        assert "existing_hashes" in attrs, f"{mod}::{fn_name} 没用共用的批量预取"


# ---------------- (R22) 采集轮本体的端到端冒烟 ----------------

async def test_poll_once_runs_end_to_end(clean_tables, monkeypatch, cfg):
    """`poll_once` 是主链路的驱动者，而它整个函数体**从来没有一行被用例执行过**。

    全仓唯一提到它的用例是上面那条 AST 守卫 —— 只断言函数体里出现过
    `existing_hashes` 这个名字，不问它被怎么调用、返回值有没有被用
    （R21 记过同形的假守卫："唯一守卫是函数体里出现过这几个名字，不问返回值有没有被用"）。

    而 R22 刚往这个函数里加了 `except db.DatabaseBusy: raise`（整轮早退）——
    改一个零覆盖的函数，等于改完没验。这条把整条链真跑一遍。
    """
    from types import SimpleNamespace

    from core import engine as E
    from core import worker as W

    cfg(QB_ENABLED=False, ANIME_POLL_ENABLED=True)

    seen = []
    items = [SimpleNamespace(info_hash=f"{i:040x}", anime_title=f"番{i}") for i in range(3)]

    class FakeSource:
        name = "假源"

        async def fetch(self):
            return items

    async def fake_process(item, known_hashes=None, qb_alive=None):
        seen.append((item.info_hash, item.info_hash in (known_hashes or set())))
        return True

    async def fake_precheck():
        return False        # qB 不可达：flush 这一段直接早退，不打网络

    monkeypatch.setattr(W, "build_sources", lambda: [FakeSource()])
    monkeypatch.setattr(W, "process_item", fake_process)
    monkeypatch.setattr(W.anime, "qb_precheck", fake_precheck)

    # 库里先放一条，验证批量预取真的把"已知"传了下去
    with clean_tables.get_session() as s:
        from db.models import Anime, AnimeTorrent
        a = Anime(title="旧番", season=1, confirmed=True)
        s.add(a); s.commit(); s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{1:040x}", raw_title="x", episode=1))
        s.commit()

    await W.poll_once()

    assert len(seen) == 3, f"三条条目没有全部走到 process_item：{seen}"
    known_flags = dict(seen)
    assert known_flags[f"{1:040x}"] is True, "库里已有的那条没被批量预取认出来"
    assert known_flags[f"{0:040x}"] is False


async def test_poll_once_bails_out_of_the_whole_round_on_maintenance(clean_tables,
                                                                     monkeypatch, cfg):
    """维护一开，整轮早退 —— 不能逐条落成 ERROR。

    一轮 feed 几十到几千条（真实 Mikan 番组 feed 4193 条），而 /logs 的环形缓冲只有 200 条：
    逐条记会把实时视图整块冲掉，同一时间段里真正的错误全被挤出去。
    """
    from types import SimpleNamespace

    import db
    from core import worker as W

    cfg(QB_ENABLED=False, ANIME_POLL_ENABLED=True)
    calls = {"n": 0}
    items = [SimpleNamespace(info_hash=f"{i:040x}", anime_title=f"番{i}") for i in range(50)]

    class FakeSource:
        name = "假源"

        async def fetch(self):
            return items

    async def fake_process(item, known_hashes=None, qb_alive=None):
        calls["n"] += 1
        raise db.DatabaseBusy("数据库维护中（正在切库），请稍候再试")

    async def fake_precheck():
        return False

    monkeypatch.setattr(W, "build_sources", lambda: [FakeSource()])
    monkeypatch.setattr(W, "process_item", fake_process)
    monkeypatch.setattr(W.anime, "qb_precheck", fake_precheck)

    with pytest.raises(db.DatabaseBusy):
        await W.poll_once()
    assert calls["n"] == 1, f"整轮没早退，逐条试了 {calls['n']} 次（真实 feed 是几千条）"
