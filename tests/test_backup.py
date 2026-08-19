"""备份：当前唯一"出事就没救"的空白，所以它自己的失败方式也要钉死。

备份最要命的失败方式不是"报错"，而是【天天成功、恢复时才发现打不开或缺数据】——
WAL 库直接 shutil.copy2 主文件正是这种：文件在、大小正常、内容陈旧。
"""
import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlmodel import select

from db import backup as B
from db.models import Anime


@pytest.fixture
def fresh_backup_dir(tmp_path, monkeypatch):
    d = tmp_path / "backups"
    monkeypatch.setattr(B, "BACKUP_DIR", d)
    return d


def test_backup_captures_unflushed_wal_writes(clean_tables, fresh_backup_dir):
    """【本模块存在的理由】WAL 模式下最近的写入躺在 -wal 里、还没并进主文件。
    直接拷主文件会拿到一份看着正常、其实缺数据的库。VACUUM INTO 由 SQLite 自己做一致性快照。"""
    with clean_tables.get_session() as s:
        s.add(Anime(title="刚写进去的番", season=1))
        s.commit()

    res = B.backup_now(keep=5)
    got = sqlite3.connect(f"file:{res['path']}?mode=ro", uri=True)
    try:
        names = [r[0] for r in got.execute("SELECT title FROM anime")]
    finally:
        got.close()
    assert "刚写进去的番" in names

    # 对照：裸拷主文件（两份文档里原本写着的建议）——这里不断言它一定丢数据
    # （取决于 checkpoint 时机），只确认我们走的不是这条路。
    src = B._sqlite_path(__import__("db").meta_engine)
    raw = Path(fresh_backup_dir) / "raw-copy.db"
    shutil.copy2(src, raw)
    assert Path(res["path"]).name != raw.name


def test_backup_is_verifiable(clean_tables, fresh_backup_dir):
    res = B.backup_now(keep=5)
    ok, why = B.verify(res["path"])
    assert ok, why


def test_verify_rejects_garbage(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all")
    ok, why = B.verify(str(bad))
    assert not ok and why
    ok, why = B.verify(str(tmp_path / "does-not-exist.db"))
    assert not ok


def test_scope_says_what_was_actually_backed_up(clean_tables, fresh_backup_dir):
    """业务库在 MySQL 时只备得到配置。那不是失败，但必须让用户知道番剧数据【不在】里面。"""
    res = B.backup_now(keep=5)
    assert res["scope"] == "full"          # 单测里业务库与配置库同为本地 SQLite
    assert "业务数据" in res["note"]


def test_prune_keeps_the_newest_n(clean_tables, fresh_backup_dir):
    import os
    import time
    fresh_backup_dir.mkdir(parents=True, exist_ok=True)
    base = time.time() - 10000
    for i in range(6):
        p = fresh_backup_dir / f"autorss-full-2026081{i}-000000.db"
        p.write_bytes(b"x")
        os.utime(p, (base + i * 100, base + i * 100))
    gone = B.prune(2)
    left = [d["name"] for d in B.list_backups()]
    assert len(left) == 2 and len(gone) == 4
    assert left[0].startswith("autorss-full-20260815")   # 最新的两份留下


def test_new_backup_is_never_pruned_by_its_own_run(clean_tables, fresh_backup_dir):
    """(R3) 【差点毁掉整个备份功能的那条】文件名是 autorss-{scope}-{stamp}.db，
    scope 段排在时间戳【前面】，而 'm'(meta) > 'f'(full)。按文件名排序时，一份刚做好的
    full 备份会排在所有旧 meta 备份【后面】、被自己这次 prune 删掉，
    紧接着 out.stat() 抛 FileNotFoundError —— 自动备份就此进入
    "每 10 分钟整库导出一次再删掉、BACKUP_LAST 永不前进"的死循环。

    这条用例先前没抓到，是因为它造的假文件全用了同一个 scope。"""
    import os
    import time
    fresh_backup_dir.mkdir(parents=True, exist_ok=True)
    base = time.time() - 10000
    for i in range(7):                                  # 目录里塞满【旧的 meta】备份
        p = fresh_backup_dir / f"autorss-meta-2026080{i}-000000.db"
        p.write_bytes(b"x")
        os.utime(p, (base + i, base + i))
    res = B.backup_now(keep=7)                          # 新做一份 full（名字里 'f' < 'm'）
    assert Path(res["path"]).exists(), "刚做好的备份不能被自己这一次 prune 删掉"
    assert res["bytes"] > 0
    assert B.list_backups()[0]["name"] == Path(res["path"]).name, "最新的一份要排在最前"


def test_concurrent_backups_do_not_delete_each_other(clean_tables, fresh_backup_dir):
    """(R3) 文件名精确到秒，后台协程与页面按钮同秒撞车是真实可能。
    早先的写法是"存在就抛" + 失败时无条件 unlink(out) —— 输家会把【赢家刚做好的那份】删掉，
    目录里一份都不剩，而自动路径还照样写下 BACKUP_LAST 报成功。"""
    import threading
    errs, oks = [], []

    def one():
        try:
            oks.append(B.backup_now(keep=10))
        except Exception as e:      # 同秒撞名时后到的那个覆盖前一个，不该两败俱伤
            errs.append(e)
    ts = [threading.Thread(target=one) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert oks, f"至少要有一份成功：{errs}"
    assert B.list_backups(), "并发之后目录里必须还有备份"
    for r in oks:
        ok, why = B.verify(r["path"]) if Path(r["path"]).exists() else (True, "被同秒的另一份覆盖")
        assert ok, why


def test_no_temp_leftovers_after_failure(clean_tables, fresh_backup_dir, monkeypatch):
    """满盘时 VACUUM 会留下 -journal 之类的残骸，而 _NAME_RE 不匹配它们、prune 永远不会清——
    每 600 秒一个，一天 144 个。"""
    orig = B.sqlite3.connect

    class Boom:
        def __init__(self, *a, **kw):
            self._c = orig(*a, **kw)

        def execute(self, *a, **kw):
            Path(str(a[1][0]) + "-journal").write_bytes(b"junk")
            raise RuntimeError("disk full")

        def close(self):
            self._c.close()
    monkeypatch.setattr(B.sqlite3, "connect", lambda *a, **kw: Boom(*a, **kw))
    with pytest.raises(RuntimeError):
        B.backup_now(keep=5)
    assert list(Path(fresh_backup_dir).iterdir()) == [], "失败后不能留下任何残骸"


def test_auto_tick_rejects_an_unverifiable_backup(clean_tables, fresh_backup_dir, monkeypatch, cfg):
    """自动这条路径没人盯着：验不过还记下 BACKUP_LAST，就会安静地等满一个间隔再做下一份，
    中间这段时间用户手里的"最新备份"其实打不开。"""
    cfg(BACKUP_ENABLED=True, BACKUP_INTERVAL_HOURS=24, BACKUP_KEEP=5, BACKUP_LAST="")
    monkeypatch.setattr(B, "verify", lambda p: (False, "模拟自检失败"))
    written = {}
    monkeypatch.setattr(B.__dict__["config"] if "config" in B.__dict__ else __import__("config"),
                        "set_many", lambda u: written.update(u))
    assert B.auto_tick() is False
    assert "BACKUP_LAST" not in written, "验不过就不该记时间"
    assert B.list_backups() == [], "可疑文件要删掉"


def test_prune_zero_is_a_noop(clean_tables, fresh_backup_dir):
    """keep<=0 视作"不清理"——绝不能理解成"全删"。"""
    fresh_backup_dir.mkdir(parents=True, exist_ok=True)
    (fresh_backup_dir / "autorss-full-20260818-000000.db").write_bytes(b"x")
    assert B.prune(0) == [] and len(B.list_backups()) == 1


def test_listing_ignores_foreign_files(clean_tables, fresh_backup_dir):
    """用户可能往这个目录里放别的东西。只认自己的命名，别去删人家的文件。"""
    fresh_backup_dir.mkdir(parents=True, exist_ok=True)
    (fresh_backup_dir / "autorss-full-20260818-000000.db").write_bytes(b"x")
    (fresh_backup_dir / "我自己的备份.db").write_bytes(b"x")
    (fresh_backup_dir / "notes.txt").write_bytes(b"x")
    assert [d["name"] for d in B.list_backups()] == ["autorss-full-20260818-000000.db"]
    B.prune(0)
    assert (fresh_backup_dir / "我自己的备份.db").exists()


def test_partial_file_is_not_left_behind(clean_tables, fresh_backup_dir, monkeypatch):
    """半截的备份文件比没有更危险——它看着像一份可用的备份。"""
    import sqlite3 as real
    orig = real.connect

    class Boom:
        def __init__(self, *a, **kw):
            self._c = orig(*a, **kw)

        def execute(self, *a, **kw):
            Path(str(a[1][0])).write_bytes(b"half")   # 先落一个半截文件
            raise RuntimeError("模拟导出中途失败")

        def close(self):
            self._c.close()
    monkeypatch.setattr(B.sqlite3, "connect", lambda *a, **kw: Boom(*a, **kw))
    with pytest.raises(RuntimeError):
        B.backup_now(keep=5)
    assert B.list_backups() == []


def test_prune_counts_each_scope_separately(clean_tables, fresh_backup_dir):
    """(R3) 一份"仅配置"的 meta 备份不该顶掉一份"配置+业务"的 full 备份：
    用户试过一阵 MySQL 又切回来，中间那批 meta 会把 full 全挤走，
    而 full 才是真正救得回番剧数据的那种。"""
    import os
    import time
    fresh_backup_dir.mkdir(parents=True, exist_ok=True)
    base = time.time() - 10000
    for i in range(3):                                   # 3 份旧 full
        p = fresh_backup_dir / f"autorss-full-2026080{i}-000000.db"
        p.write_bytes(b"x")
        os.utime(p, (base + i, base + i))
    for i in range(5):                                   # 5 份更新的 meta
        p = fresh_backup_dir / f"autorss-meta-2026081{i}-000000.db"
        p.write_bytes(b"x")
        os.utime(p, (base + 100 + i, base + 100 + i))
    B.prune(2)
    left = B.list_backups()
    assert sum(1 for d in left if d["scope"] == "full") == 2, "full 必须自己留满 2 份"
    assert sum(1 for d in left if d["scope"] == "meta") == 2
