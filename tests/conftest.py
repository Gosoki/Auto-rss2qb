"""测试套件。

只测【纯函数与内存内逻辑】：不打网络、不连 qB、不依赖真实数据库。
目的是给重构上一道回归网——本项目历史上出过的 bug 有相当比例是"改了一处判据，
另一处手抄的同款判据没跟上"，而那类回归只要有表驱动用例就能当场兜住。
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 【必须在 import config 之前】config.DB_PATH 是模块级、启动时从环境变量读一次。
# 指到临时文件，免得用例把开发用的 data/autorss.db 写脏（init_db 会真的建表、跑迁移）。
_TMP_DB = Path(tempfile.mkdtemp(prefix="autorss-test-")) / "test.db"
os.environ["DB_PATH"] = str(_TMP_DB)

import config  # noqa: E402


@pytest.fixture
def cfg(monkeypatch):
    """临时改配置值。config 的读取走模块级 __getattr__ → _v 字典，直接改字典即可，
    不写数据库、不影响别的用例（monkeypatch 会逐键还原）。"""
    def _set(**kw):
        for k, v in kw.items():
            monkeypatch.setitem(config._v, k, v)
    return _set


@pytest.fixture(scope="session")
def testdb():
    """建一个空的临时库并跑完迁移。返回 db 模块。

    只给"必须有库才能测"的用例用（如 sync 状态机）；纯函数用例不要依赖它。
    """
    import db
    db.init_db()
    config.load_from_db()
    return db


# 落在 meta 库里的【内部标记】（不是用户配置）。它们记录的是"这件一次性的事做过了"，
# 跨重启有效——正因为如此，用例之间也必须清掉，否则前一个用例做过的事会让后一个用例
# 走上另一条分支（实测踩过：完结回填标记）。
# ⚠️ 这里的字符串必须与生产代码里那个键【逐字相同】——写错了不会报错，只是白清一个不存在的键。
# 已经踩过两次：`_backfill_legacy_progress_done` 是编的，真实键是 core/engine.py 的
# `_QB_PROGRESS_BACKFILLED`；`_idle_backfilled` 当初压根忘了加。
# 下面那条用例会去生产代码里核对，别再手抄漏了。
_INTERNAL_MARKERS = ("_FINISH_BACKFILL_DONE", "_KNOWN_NOTIFY_EVENTS",
                     "_QB_PROGRESS_BACKFILLED", "_idle_backfilled")


@pytest.fixture
def clean_tables(testdb):
    """每个用例前清空业务表与内部标记，用例之间互不干扰。"""
    from sqlmodel import delete
    from db.models import Anime, AnimeTorrent, AnimeAlias, Movie, MovieTorrent, Setting
    with testdb.get_session() as s:
        for m in (AnimeTorrent, AnimeAlias, Anime, MovieTorrent, Movie):
            s.exec(delete(m))
        s.commit()
    with testdb.get_meta_session() as s:
        for k in _INTERNAL_MARKERS:
            row = s.get(Setting, k)
            if row is not None:
                s.delete(row)
        s.commit()
    return testdb


@pytest.fixture
def upgrade_from(tmp_path):
    """造一个【真正的旧库】：升到指定 revision 就停，灌完数据再升到 head。

    用法：
        eng = upgrade_from("c7e1a93b4d02")      # 停在那一版，此时表结构是旧的
        ... 用 eng 灌数据（可以插入新版会被约束拦下的数据）...
        upgrade_from.to_head(eng)               # 走真实的升级路径
        ... 断言 ...

    【为什么需要它】本项目的缺陷有相当比例**只在存量库上出现**：第 9 轮五条 P1 里三条如此，
    而全套用例当时用的都是新建库 fixture——新建库一路建到 head，永远碰不到"老结构 + 新代码"
    这条路径。"建到 head 再把版本号改回去"是不够的：那样表结构其实还是新的。
    """
    import sqlalchemy as sa

    from db import schema

    made = []

    def _make(revision: str):
        p = tmp_path / f"old-{revision}-{len(made)}.db"
        eng = sa.create_engine(f"sqlite:///{p}")
        schema.upgrade(eng, "data", revision)
        made.append(eng)
        return eng

    _make.to_head = lambda eng: schema.upgrade(eng, "data")
    _make.revision_of = lambda eng: schema.current_revision(eng, "data")
    return _make
