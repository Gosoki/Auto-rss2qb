"""(R27) 改键名必须带迁移 —— 三个一次性回填标记的存量键认领。

R26 把 `_FINISH_BACKFILL_DONE` / `_idle_backfilled` / `_QB_PROGRESS_BACKFILLED`
从全局键改成了 `<键>@<业务库身份>`，理由是对的（标记住在 meta 库、判的却是业务库），
**但没有迁移旧键**。而每一台已经在跑的安装，meta 里存的都是旧名 ——
真库实测 setting 表里就是那三个旧名，一个新名都没有。

于是升上 R26 之后的第一次启动，三条一次性回填全部重跑一遍。其中
`_QB_PROGRESS_BACKFILLED` 是一条**改数据**的迁移：它把所有
`status='sent' 且 qb_progress<1.0` 的行写成 1.0 —— 那批正是**正在下载**的种子。

这一组把"认领"的三条性质钉死：认得到、认完旧键就没了、换库之后不再被认领。
"""
import pytest

import db
from db.models import Setting

_QB_KEY = "_QB_PROGRESS_BACKFILLED"


def _put_legacy(key, value="1"):
    with db.get_meta_session() as s:
        if s.get(Setting, key) is None:
            s.add(Setting(key=key, value=value))
            s.commit()


def _meta_keys():
    with db.get_meta_session() as s:
        from sqlmodel import select
        return {r.key for r in s.exec(select(Setting))}


async def test_the_data_changing_backfill_does_not_rerun_on_an_existing_install(clean_tables):
    """存量安装升级后，那条【改数据】的一次性迁移不许重跑。

    没有认领时它会把正在下载的种子（sent 且 progress<1）写成 1.0，
    而那之后没有任何路径能改回来：掉出 `_inflight_where`、`qb_state` 又不在补查名单里，
    同时 `sent ∈ HAVE_STATUSES` 让集去重认定这一集已到手。
    """
    from core import engine as ce
    from db.models import AnimeTorrent

    with clean_tables.get_session() as s:
        s.add(AnimeTorrent(anime_id=1, info_hash="a" * 40, raw_title="x - 01",
                           episode=1.0, status="sent", qb_progress=0.5,
                           qb_state="downloading"))
        s.commit()

    _put_legacy(_QB_KEY)                     # 存量安装的样子：只有旧的全局键
    ce.backfill_legacy_progress_once()

    with clean_tables.get_session() as s:
        from sqlmodel import select
        t = s.exec(select(AnimeTorrent)).first()
    assert t.qb_progress == 0.5, \
        "那条改数据的一次性迁移又跑了一遍 —— 正在下载的种子被写成了已下完"

    keys = _meta_keys()
    assert _QB_KEY not in keys, "旧键没被删掉：以后切到别的库它还会再被认领一次"
    assert db.scoped_flag(_QB_KEY) in keys, "认领之后没写下带身份的新键"


async def test_a_genuinely_fresh_database_still_gets_backfilled(clean_tables):
    """反向：旧键与新键都没有时，回填必须照跑 —— 别把认领做成"一律跳过"。"""
    from core import engine as ce
    from db.models import AnimeTorrent

    with clean_tables.get_session() as s:
        s.add(AnimeTorrent(anime_id=1, info_hash="b" * 40, raw_title="x - 01",
                           episode=1.0, status="sent", qb_progress=0.0))
        s.commit()

    assert _QB_KEY not in _meta_keys() and db.scoped_flag(_QB_KEY) not in _meta_keys()
    ce.backfill_legacy_progress_once()

    with clean_tables.get_session() as s:
        from sqlmodel import select
        t = s.exec(select(AnimeTorrent)).first()
    assert t.qb_progress == 1.0, "全新库的历史 sent 行没被回填"
    assert db.scoped_flag(_QB_KEY) in _meta_keys()


async def test_adoption_is_one_shot_so_a_second_database_still_backfills(monkeypatch,
                                                                        clean_tables):
    """认领只发生一次：切到另一个业务库之后，那个库仍然要自己回填一遍。

    这正是 R26 想修的问题。如果认领时不把旧键删掉，旧键会在每一个新库上再被认领一次 ——
    R26 修的东西原样回来，而且这次还多了一层"看起来做过迁移了"的假象。
    """
    from core import engine as ce

    _put_legacy(_QB_KEY)
    assert db.adopt_legacy_flag(_QB_KEY) is True, "第一次该认领成功"

    monkeypatch.setattr(db, "data_identity", lambda eng=None: "mysql:10.0.0.9:3306/other")
    assert db.adopt_legacy_flag(_QB_KEY) is False, \
        "换了业务库还能认领到旧键 —— 那个库从来没回填过"
    assert db.scoped_flag(_QB_KEY) not in _meta_keys()
    ce.backfill_legacy_progress_once()        # 新库照常回填，并落下自己的标记
    assert db.scoped_flag(_QB_KEY) in _meta_keys()


@pytest.mark.parametrize("key", ["_FINISH_BACKFILL_DONE", "_idle_backfilled"])
def test_the_two_notification_latches_also_adopt_the_old_key(key, clean_tables):
    """另外两个标记（"别在首轮刷屏通知"）走的是同一条认领路径。

    它们重跑的后果不改数据，但一样难看：存量番在某一轮被整批当成"首轮回填"而静默，
    而完结判定那条闩**只在有命中的那一轮才落**，静默的恰恰就是那一轮。
    """
    from core import anime as A

    _put_legacy(key)
    assert A._backfilled(key) is True, "读不到旧键 —— 存量安装会被当成从没回填过"
    keys = _meta_keys()
    assert key not in keys and db.scoped_flag(key) in keys


def test_all_three_latches_go_through_the_shared_helpers():
    """三处标记必须共用 `db.scoped_flag` / `db.adopt_legacy_flag`，不许各写各的。

    "三处同一个决定、只落到一处"是本项目反复出现的第①种缺陷形状，R26 那次
    正是这个形状的反面：三处都改了键名，但**零处**带迁移。
    这条用例数的是实际调用，不是字符串出现次数。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    calls = {"scoped_flag": 0, "adopt_legacy_flag": 0}
    for d in ("core/anime.py", "core/engine.py"):
        tree = ast.parse((root / d).read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                name = (n.func.attr if isinstance(n.func, ast.Attribute)
                        else n.func.id if isinstance(n.func, ast.Name) else "")
                if name in calls:
                    calls[name] += 1
    assert calls["scoped_flag"] >= 2, f"带身份的键名不是共用实现算出来的：{calls}"
    assert calls["adopt_legacy_flag"] >= 2, (
        f"只有 {calls['adopt_legacy_flag']} 处走了旧键认领 —— "
        "没走的那处在存量安装上会重跑它的一次性回填")


async def test_switching_to_another_database_never_marks_live_downloads_complete(clean_tables):
    """(R27) 换业务库时，那条改数据的迁移不许把**正在下载**的种子写成已下完。

    标记按业务库分作用域（R26 的决定，是对的），可"换一个业务库"就等于标记读不到 ——
    而换库是个正常操作：设置页『迁移数据』把含在下种子的库整个搬到 MySQL、
    紧接着『切到 MySQL』→ `init_business_state` → 本函数。旧键认领只发生一次，
    救不了这条路径。

    所以标记之外还要一道**数据本身**的判据：上线前的库从来没写过 `qb_synced_at`
    （那一列与 qb_progress 是同一批加的）。库里只要有一条 `qb_synced_at IS NOT NULL`，
    就说明实时态跟踪在这个库上跑过 —— 此时的 `sent 且 progress<1` 是真的在下。
    """
    from datetime import datetime

    from sqlmodel import select

    from core import engine as ce
    from db.models import AnimeTorrent

    with clean_tables.get_session() as s:
        # 正在下：已同步过、进度过半
        s.add(AnimeTorrent(anime_id=1, info_hash="c" * 40, raw_title="x - 01", episode=1.0,
                           status="sent", qb_progress=0.55, qb_state="downloading",
                           qb_synced_at=datetime.now()))
        # 同一个库里另一条刚发出去、还没同步回来的
        s.add(AnimeTorrent(anime_id=1, info_hash="d" * 40, raw_title="x - 02", episode=2.0,
                           status="sent", qb_progress=0.0, qb_state=""))
        s.commit()

    # 全新身份的库：标记读不到（模拟"刚切过去"），旧键也没有
    assert db.scoped_flag("_QB_PROGRESS_BACKFILLED") not in _meta_keys()
    ce.backfill_legacy_progress_once()

    with clean_tables.get_session() as s:
        got = {t.info_hash: t.qb_progress for t in s.exec(select(AnimeTorrent))}
    assert got["c" * 40] == 0.55, "正在下载的种子被写成了已下完 —— 它从此永久脱轨"
    assert got["d" * 40] == 0.0, "同库里刚发出去的那条也被一起写死了"


async def test_the_criterion_is_per_database_not_per_table(clean_tables):
    """(R28) 那道数据判据的作用域必须是【这个库】，不是【这张表】。

    R27 第一版把探测写在 `for model_cls` **循环体内**逐表判，而立论说的是
    "实时态跟踪在这个**业务库**上跑过没有"。一张表只要从来没有一行被 qB 同步过，
    逐表写法对它就恒不成立 —— 真库快照正是这种形状：
    `animetorrent` 1679 行里 527 行有 `qb_synced_at`，而 `movietorrent` **569 行一条都没有**
    （剧场版逐版本人工点下，很多库里根本没交付过）。
    于是切库之后，剧场版那张表里正在下的版本被整表写成 1.0。

    **这条用例必须只在 MovieTorrent 那一侧留在下的行** —— 两张表都放的话，
    逐表写法也能过（AnimeTorrent 那张有同步记录），用例就白写了。
    """
    from datetime import datetime

    from sqlmodel import select

    from core import engine as ce
    from db.models import AnimeTorrent, MovieTorrent

    with clean_tables.get_session() as s:
        # 番剧表：有同步记录，但没有"在下"的行
        s.add(AnimeTorrent(anime_id=1, info_hash="e" * 40, raw_title="x - 01", episode=1.0,
                           status="sent", qb_progress=1.0, qb_synced_at=datetime.now()))
        # 剧场版表：正在下，且这张表【从来没有】任何 qb_synced_at
        s.add(MovieTorrent(movie_id=1, info_hash="f" * 40, raw_title="movie 1080p",
                           status="sent", qb_progress=0.42, qb_state="downloading"))
        s.commit()

    ce.backfill_legacy_progress_once()

    with clean_tables.get_session() as s:
        mt = s.exec(select(MovieTorrent)).first()
    assert mt.qb_progress == 0.42, (
        "剧场版那张表里正在下的版本被写成了已下完 —— "
        "判据逐表判时，一张从没被同步过的表等于没有闸")
