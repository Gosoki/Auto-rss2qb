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
_INTERNAL_MARKERS = ("_FINISH_BACKFILL_DONE", "_KNOWN_NOTIFY_EVENTS",
                     "_backfill_legacy_progress_done", "_idle_backfilled")


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
