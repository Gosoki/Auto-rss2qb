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
