"""存量库的升级路径。

**本项目的缺陷有相当比例只在这条路径上出现**：第 9 轮五条 P1 里三条如此
（迁移预检、baseline 的 partial index、老库的唯一约束），而当时全套用例用的都是新建库——
新建库一路建到 head，永远碰不到"老结构 + 新代码"。

这里的 upgrade_from fixture 造的是**真正的旧库**（升到某个中间 revision 就停），
不是"建到 head 再把版本号改回去"——后者的表结构其实还是新的。
"""
import threading

import sqlalchemy as sa
from sqlmodel import Session

from db import schema
from db.models import Movie


def test_every_revision_script_loads():
    """所有 revision 脚本都能被 alembic 加载 —— 挡"文件坏了导致应用整个起不来"。

    实测踩过：一条 commit message 被写进了 revision 文件开头，
    于是 alembic 加载整个 versions 目录时 SyntaxError，应用起不来。
    全套用例当时确实会报错，但报的是三条不相干用例的 ERROR，看不出根因。
    """
    from alembic.script import ScriptDirectory
    revs = list(ScriptDirectory(str(schema._ROOT / "alembic")).walk_revisions())
    assert len(revs) >= 5
    assert schema.head_revision() == revs[0].revision


def test_legacy_db_with_preexisting_indexes_self_heals(tmp_path):
    """老库：表和索引都在、版本行丢了 → 必须自愈到 head，不能 fatal。

    Alembic 化之前的 db.__init__._migrate_inflight_indexes 每次启动都建同名的
    ix_*_inflight，所以任何一个老库原地升级都会撞上"index already exists"——
    不需要任何竞态。撞上就是 mark_data_fatal，而 fatal 只能人工解除：每次重启同一个死循环。
    """
    p = tmp_path / "legacy.db"
    eng = sa.create_engine(f"sqlite:///{p}")
    schema.upgrade(eng, "data")
    with eng.begin() as c:                      # 模拟老库：表和索引都在，版本行没了
        c.execute(sa.text("DELETE FROM alembic_version_data"))
    with eng.connect() as c:
        idx = [r[0] for r in c.execute(sa.text(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE '%inflight%'"))]
    assert len(idx) == 2, "用例前提不成立：那两条 partial index 不在"

    schema.upgrade(eng, "data")                 # 再升一次：不能抛
    assert schema.current_revision(eng, "data") == schema.head_revision()


def test_old_db_with_duplicate_mikan_ids_upgrades_cleanly(upgrade_from):
    """停在加唯一约束之前的库，里面有重复 mikan_id → 升级要自动摘掉多余的链接。

    保留"有 bgm_id 的、其次 id 最小的"那一行；其余置空。**不自动合并**——
    合并会删行，而"两行是不是同一部"在迁移里没有可靠判据。
    """
    eng = upgrade_from("d3f8b21c5e40")           # uniq_mikan 的前一版
    with Session(eng) as s:
        s.add(Movie(title="重复A", mikan_id="1000", quarter="26A"))
        s.add(Movie(title="重复B", mikan_id="1000", quarter="26A", bangumi_id=777))
        s.add(Movie(title="重复C", mikan_id="1000", quarter="26A"))
        s.add(Movie(title="独立", mikan_id="2000", quarter="26A"))
        s.commit()

    upgrade_from.to_head(eng)

    with Session(eng) as s:
        rows = {m.title: m.mikan_id for m in s.exec(sa.select(Movie)).scalars()}
    assert rows["重复B"] == "1000", "保留的应该是有 bgm_id 的那一行"
    assert rows["重复A"] is None and rows["重复C"] is None
    assert rows["独立"] == "2000", "没有重复的不该被动"
    with eng.connect() as c:                     # 约束真的生效了
        uniq = [r[0] for r in c.execute(sa.text(
            "SELECT name FROM pragma_index_list('movie') WHERE \"unique\"=1")) if "mikan" in r[0]]
    assert uniq, "唯一索引没建上"


def test_old_db_with_overlong_aliases_gets_trimmed(upgrade_from):
    """停在 trim_alias 之前的库，里面有超长别名 → 升级要截断并合并截断后重复的。"""
    from db.models import Anime, AnimeAlias
    eng = upgrade_from("c7e1a93b4d02")           # trim_alias 的前一版
    long_a = "番" * 250
    long_b = "番" * 254                           # 截断到 191 后与上面相同
    with Session(eng) as s:
        a = Anime(title="长名番", quarter="26A", confirmed=True)
        s.add(a)
        s.commit()
        s.refresh(a)
        s.add(AnimeAlias(title=long_a, anime_id=a.id))
        s.add(AnimeAlias(title=long_b, anime_id=a.id))
        s.commit()

    upgrade_from.to_head(eng)

    with Session(eng) as s:
        titles = [x.title for x in s.exec(sa.select(AnimeAlias)).scalars()]
    assert len(titles) == 1, f"截断后重复的没合并：{[len(t) for t in titles]}"
    assert len(titles[0]) == 191


def test_concurrent_upgrades_are_serialised(tmp_path):
    """两个线程同时跑 alembic 必须被串行化。

    alembic 的 EnvironmentContext 装的是**进程级模块代理**，两次升级重叠时后退出的那个
    `del globals_[attr]` 会删掉对方的代理。而本项目真的会并发调它：schema.upgrade 有 5 个
    调用点，其中两个在【非事件循环线程】里（看守协程每 30 秒的探测、页面『立即重连』），
    还有一个是 run.io_bound 里的整库迁移。

    去掉锁之后在副本上实测 6 次：4 次留下「alembic_version=head 而唯一索引没建上」
    （而 upgrade() 开头那句 `if cur == head: return` 让这一态**永远不会被修复**），
    2 次整进程 SIGSEGV。加锁后 60 线程零异常。
    """
    import sqlalchemy as sa

    from db import schema

    eng = sa.create_engine(f"sqlite:///{tmp_path / 'race.db'}")
    errs = []

    def _go():
        try:
            schema.upgrade(eng, "data")
        except Exception as e:                       # noqa: BLE001
            errs.append(f"{type(e).__name__}: {e}")

    ts = [threading.Thread(target=_go) for _ in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert not errs, f"并发升级抛了异常：{errs[:3]}"
    assert schema.current_revision(eng, "data") == schema.head_revision()
    with eng.connect() as c:                         # DDL 真的跑完了，不只是版本号推进
        uniq = [r[0] for r in c.execute(sa.text(
            'SELECT name FROM pragma_index_list("movie") WHERE "unique"=1')) if "mikan" in r[0]]
    assert uniq, "版本号到了 head，但唯一索引没建上——这一态永远不会被自动修复"
