"""仪表盘拆桶的不变量。

`pending_breakdown` 的 docstring 承诺"五者之和 = 待下总数"。这类"卡片数字加起来要对得上"的
承诺最容易在加桶时被破坏，而破坏之后没有任何报错——只是某一类种子从此在面板上消失。
本项目已经出过一次：特别篇曾掉进『备用项』，而那张卡的说明是"同集已有更优版本"。
"""
from datetime import datetime

import pytest
from sqlmodel import select

from core import anime as A
from db.models import Anime, AnimeTorrent


@pytest.fixture
def mixed_library(clean_tables):
    """一个覆盖各分支的库：已确认/未确认/已忽略/已完结 × 正集/特别篇/未知集。"""
    now = datetime.now()
    seq = [0]
    with clean_tables.get_session() as s:
        specs = [
            ("已确认", dict(confirmed=True)),
            ("未确认", dict(confirmed=False)),
            ("已忽略", dict(confirmed=True, rejected=True)),
            ("已完结", dict(confirmed=True, finished_at=now, total_episodes=2)),
        ]
        made = []
        for title, kw in specs:
            a = Anime(title=title, season=1, quarter="26C", **kw)
            s.add(a)
            s.commit()
            s.refresh(a)
            made.append(a.id)
            for ep in (1, 2, -1, -2):
                seq[0] += 1
                s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{seq[0]:040x}",
                                   raw_title=f"[X] {title} - {ep}", episode=ep,
                                   status="pending", source="X", priority=50, created_at=now))
        # 一条孤儿（番不存在）
        seq[0] += 1
        s.add(AnimeTorrent(anime_id=999999, info_hash=f"{seq[0]:040x}", raw_title="孤儿",
                           episode=1, status="pending", source="X", created_at=now))
        s.commit()
        return made


def _pending_total(db):
    with db.get_session() as s:
        return s.exec(select(A.func.count()).select_from(AnimeTorrent)
                      .where(AnimeTorrent.status == "pending")).one()


@pytest.mark.parametrize("unsub", [False, True])
def test_buckets_sum_to_pending_total(clean_tables, mixed_library, cfg, unsub):
    """(R5) 五者之和必须等于待下总数——两种停订开关下都要成立。"""
    cfg(ANIME_FINISH_UNSUB=unsub)
    b = A.pending_breakdown()
    total = _pending_total(clean_tables)
    assert sum((b["will"], b["backup"], b["unconfirmed"], b["unknown"], b["finished"])) == total, b


def test_specials_go_to_unknown_not_backup(clean_tables, mixed_library, cfg):
    """特别篇(-1)与未知集(-2)必须进『特别篇/未知集』这一档。
    掉进『备用项』的话，那张卡的说明"同集已有更优版本"是纯粹的误导——本项目已经踩过一次。"""
    cfg(ANIME_FINISH_UNSUB=False)
    b = A.pending_breakdown()
    assert b["unknown"] == 8, "4 部番 × 2 条（-1 与 -2）"


def test_finished_bucket_only_counts_when_unsub_is_on(clean_tables, mixed_library, cfg):
    """停订关着时"已完结"不该占一档——那时它照常自动下，归入 will/backup 才是事实。"""
    cfg(ANIME_FINISH_UNSUB=False)
    assert A.pending_breakdown()["finished"] == 0
    cfg(ANIME_FINISH_UNSUB=True)
    assert A.pending_breakdown()["finished"] > 0


def test_orphan_and_rejected_go_to_backup(clean_tables, mixed_library, cfg):
    """番已忽略/孤儿的待下不会自动下，归『备用项』（而不是凭空消失）。"""
    cfg(ANIME_FINISH_UNSUB=False)
    b = A.pending_breakdown()
    assert b["backup"] >= 3, "已忽略番的 2 条正集 + 1 条孤儿"


def test_breakdown_keys_are_stable(clean_tables, mixed_library):
    """页面按键名取值。少一个键就是 KeyError（白屏），多一个键无害但要有人知道。"""
    assert set(A.pending_breakdown()) == {"will", "backup", "unconfirmed", "unknown", "finished"}


def test_auto_off_reasons_matches_pending_breakdown_wording(clean_tables, cfg):
    """『为什么不自动下』三种原因的用词与判序必须与 pending_breakdown 一致。

    以前渲染侧只有 confirmed 一个布尔，于是已忽略/已完结的番也被显示成『待确认』
    并把用户指去那个 tab —— 而它们根本不在里面，用户到那儿只看到一个空列表。
    """
    from datetime import datetime

    from core import anime as A
    from db.models import Anime

    cfg(ANIME_FINISH_UNSUB=True)
    with clean_tables.get_session() as s:
        rows = {
            "待确认": Anime(title="a", quarter="26A", confirmed=False),
            "已忽略": Anime(title="b", quarter="26A", confirmed=True, rejected=True),
            "已完结": Anime(title="c", quarter="26A", confirmed=True, finished_at=datetime.now()),
        }
        normal = Anime(title="d", quarter="26A", confirmed=True)
        for r in rows.values():
            s.add(r)
        s.add(normal)
        s.commit()
        ids = {k: r.id for k, r in rows.items()}
        ids["正常"] = normal.id

    got = A.auto_off_reasons(set(ids.values()))
    for want, aid in ids.items():
        if want == "正常":
            assert aid not in got, "会自动下的番不该出现在原因表里"
        else:
            assert got.get(aid) == want, f"{want} 被判成了 {got.get(aid)!r}"


def test_live_status_judges_unknown_episodes_first(cfg):
    """-1/-2 要【最先判】——与 pending_breakdown 同口径，否则卡片数与列表条数对不上。"""
    from pages.layout import live_status

    # 一部"待确认"番的特别篇：pending_breakdown 把它算进『未知』，渲染侧也必须
    assert live_status("pending", in_plan=False, episode=-1, auto_off="待确认")[0] == "特别篇"
    assert live_status("pending", in_plan=False, episode=-2, auto_off="已忽略")[0] == "未知集"


def test_internal_marker_names_actually_exist_in_production_code():
    """conftest 清的那几个「一次性标记」必须是生产代码里真有的键。

    写错了**不会报错**，只是白清一个不存在的键——而它的后果是隐蔽的：
    前一个用例做过的事会让后一个用例走上另一条分支。已经踩过两次
    （`_backfill_legacy_progress_done` 是编的、`_idle_backfilled` 当初忘了加）。
    """
    import pathlib

    from tests.conftest import _INTERNAL_MARKERS

    root = pathlib.Path(__file__).resolve().parent.parent
    src = "\n".join((root / d).read_text(encoding="utf-8")
                    for d in ("core/anime.py", "core/engine.py", "config.py"))
    missing = [k for k in _INTERNAL_MARKERS if f'"{k}"' not in src and f"'{k}'" not in src]
    assert not missing, f"conftest 里这些键在生产代码里不存在：{missing}"


def test_no_undefined_names_anywhere():
    """全仓不能有「用到了但没导入」的名字。

    NiceGUI 的 handler 大多是 lambda / 闭包，里面的名字**只在点击那一刻才解析**——
    漏一个 import，`import pages.x` 照样成功、页面照常渲染 200，
    只有用户真的点下去才 NameError，而那个异常又只进服务端日志（用户看到"点了没反应"）。
    刚刚就漏过一次：给设置页五个按钮接上 busy_action，却没加那一行 import。
    """
    import ast
    import builtins
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    bad = {}
    # 【扫全仓，不只是 pages/】同一个坑在 core/worker.py 与 services/notify.py 上又踩过一次：
    # 加了 fetch.redact(...) 却没绑定 fetch，import 照样成功、只有真跑到那一行才 NameError。
    files = [f for d in ("pages", "core", "services", "sources", "db")
             for f in sorted((root / d).glob("*.py"))]
    for p in files:
        tree = ast.parse(p.read_text(encoding="utf-8"))
        bound = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    bound.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(n.name)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                bound.add(n.id)
            elif isinstance(n, ast.arg):
                bound.add(n.arg)
            elif isinstance(n, (ast.Global, ast.Nonlocal)):
                bound.update(n.names)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                bound.add(n.name)                       # except ... as e
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        # 模块级魔术名不是"导入"来的
        miss = sorted(used - bound - set(dir(builtins))
                      - {"__file__", "__name__", "__doc__", "__package__"})
        if miss:
            bad[p.name] = miss
    assert not bad, f"这些名字用到了但没定义/导入：{bad}"
