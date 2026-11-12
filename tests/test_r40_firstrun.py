"""首次打开采集前的确认 + 迁移前自动备份。

· 设置页：采集开关这次从关变开、开始使用日空着、有启用的自动下载源组、库里还没有任何已确认的番
  ＝首次使用 → 保存前先问一句（第一轮采集会把整个 feed 当新番自动下载）。取消＝整次保存不落。
  判定是纯函数 settings._first_run_poll_groups（表驱动全组合），页面保存处理器真点按钮验它确实被用上。
  确认框挂着时再点保存直接忽略（两个框一继续一取消会留下假提示）。
· db/schema.upgrade：业务库是本地配置库那个 SQLite 文件、已有版本且不等于目标 → 迁移【之前】备一份
  （文件名 -preupgrade，保留份数与日常备份分开数，不把用户原有的备份挤掉；设置页列表标『升级前』）；
  一次启动（meta 链 → data 链）只在第一次迁移之前备一份，拿它回旧版程序要起得来；
  meta 链那次 config 还没读出来：不清理旧备份、库里写着 MySQL 就不备；
  新库 / 已是 head / 库比代码新 / MySQL / 另一个 SQLite 文件都不备；备份抛异常或自检不过只记 WARNING，升级照常。
"""
import asyncio
import shutil
import sqlite3
import sys
import itertools
import logging
import os
import time
from contextlib import contextmanager

import pytest
import sqlalchemy as sa
from nicegui import ui
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction
from nicegui.testing.user_notify import UserNotify
from sqlmodel import delete, select

import config
import db
from db import backup as B
from db import schema
from db.models import Anime, Setting, SourceGroup

_MAIN = "tests/render_main.py"
OLD = "d3f8b21c5e40"      # 随便一个中间版本：停在这里就是真正的旧库


# ================================================================ FLOW-10 判定（纯函数）

def _want(was_on, now_on, sd, groups, has_conf):
    return list(groups) if (not was_on and now_on and not sd.strip() and groups and not has_conf) else []


@pytest.mark.parametrize("was_on,now_on,sd,groups,has_conf", list(itertools.product(
    (False, True), (False, True), ("", "  ", "2026-07-01"), ([], ["ANi"], ["ANi", "自建"]), (False, True))))
def test_first_run_poll_groups_table(was_on, now_on, sd, groups, has_conf):
    from pages.settings import _first_run_poll_groups
    got = _first_run_poll_groups(was_on, now_on, sd, groups, has_conf)
    assert got == _want(was_on, now_on, sd, groups, has_conf)


def test_first_run_poll_groups_only_the_first_run_asks():
    """正向控制：上面的表里真的有一格是要问的，且点名的就是传进去的组。"""
    from pages.settings import _first_run_poll_groups
    assert _first_run_poll_groups(False, True, "", ["ANi", "自建"], False) == ["ANi", "自建"]


# ================================================================ FLOW-10 设置页保存

_BASE = dict(QB_ENABLED=False, DOWN_PATH="/data", ANIME_DOWN_PATH="", MOVIE_DOWN_PATH="",
             ANIME_POLL_ENABLED=False, ANIME_START_DATE="", SITE_NAME="autorss")


@pytest.fixture
def groups(clean_tables):
    """源组表 clean_tables 不清：先挪开现有的，用例自己放，结束原样放回。"""
    with clean_tables.get_session() as s:
        saved = [g.model_dump(exclude={"id"}) for g in s.exec(select(SourceGroup))]
        s.exec(delete(SourceGroup)); s.commit()

    def _add(name, policy="auto", enabled=True):
        with clean_tables.get_session() as s:
            s.add(SourceGroup(name=name, site="nyaa", feed=f"u={name}", policy=policy, enabled=enabled))
            s.commit()
    yield _add
    with clean_tables.get_session() as s:
        s.exec(delete(SourceGroup))
        for d in saved:
            s.add(SourceGroup(**d))
        s.commit()


@pytest.fixture
def page(clean_tables, cfg, monkeypatch, groups):
    cfg(**_BASE)
    monkeypatch.setattr(config, "update_env", lambda updates: None)   # 别碰仓库里真的 .env
    notes = []

    def _record(self, message, **kw):
        self.messages.append(message)
        notes.append(message)
    monkeypatch.setattr(UserNotify, "__call__", _record)
    return {"db": clean_tables, "notes": notes, "groups": groups}


def _live(user, kind):
    return [e for e in user.find(kind).elements if not e.is_deleted]


def _stub_confirm(monkeypatch, answer):
    """settings 是 `from .layout import confirm`：打桩要打在 render_main 重新加载出来的那个模块上。"""
    S = sys.modules["pages.settings"]
    asked = []

    async def fake(title, note="", **kw):
        asked.append((title, note)); return answer
    monkeypatch.setattr(S, "confirm", fake)
    return asked


def _spy_rule(monkeypatch):
    """记下保存处理器调判定函数时传的实参（守的是"处理器确实走这个纯函数"，表驱动那 72 格才不是摆设）。"""
    S = sys.modules["pages.settings"]
    real, calls = S._first_run_poll_groups, []

    def _wrap(*a):
        calls.append(a)
        return real(*a)
    monkeypatch.setattr(S, "_first_run_poll_groups", _wrap)
    return calls


async def _open_and_flip(user, site="r40站"):
    await user.open("/settings")
    sw = [s for s in _live(user, ui.switch) if s.text.startswith("启用后台采集")]
    assert len(sw) == 1, "找不到『启用后台采集』开关"
    sw[0].value = True
    inp = [i for i in _live(user, ui.input) if i.props.get("label") == "站点名"]
    assert len(inp) == 1
    inp[0].value = site                 # 顺带改一项别的，看取消时它存没存
    btn = next(b for b in _live(user, ui.button) if b.text == "保存")
    UserInteraction(user, {btn}, None).click()


def _db_value(page, key):
    with db.get_meta_session() as s:
        row = s.get(Setting, key)
        return row.value if row else None


@pytest.mark.nicegui_main_file(_MAIN)
async def test_first_run_asks_and_cancel_saves_nothing(user: User, page, monkeypatch):
    page["groups"]("ANi")
    page["groups"]("Mikan", policy="review")
    asked = _stub_confirm(monkeypatch, False)
    calls = _spy_rule(monkeypatch)
    await _open_and_flip(user)
    await user.should_see("已取消保存")
    assert calls == [(False, True, "", ["ANi"], False)], calls
    assert len(asked) == 1, asked
    title, note = asked[0]
    assert "开始使用日" in title and "ANi" in note and "Mikan" not in note, asked[0]
    assert "几十部" in note and "几十 GB" in note, note
    assert "本页『番剧』一栏" in note, "顶栏也有个『番剧』页，要说清是设置页这一栏"
    assert config.ANIME_POLL_ENABLED is False and _db_value(page, "ANIME_POLL_ENABLED") != "true"
    assert config.SITE_NAME == "autorss", "取消＝整次保存都不落，别的改动也不该存进去"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_first_run_continue_saves(user: User, page, monkeypatch):
    page["groups"]("ANi")
    asked = _stub_confirm(monkeypatch, True)
    await _open_and_flip(user)
    await user.should_see("已保存，即时生效")
    assert len(asked) == 1
    assert config.ANIME_POLL_ENABLED is True and config.SITE_NAME == "r40站"


@pytest.mark.parametrize("setup", ["已有确认的番", "只有待确认组", "自动组已停用", "填了开始使用日", "采集本来就开着"])
@pytest.mark.nicegui_main_file(_MAIN)
async def test_not_first_run_does_not_ask(user: User, page, monkeypatch, cfg, setup):
    page["groups"]("ANi", policy="review" if setup == "只有待确认组" else "auto",
                   enabled=setup != "自动组已停用")
    if setup == "已有确认的番":
        with page["db"].get_session() as s:
            s.add(Anime(title="在追", confirmed=True)); s.commit()
    if setup == "填了开始使用日":
        cfg(ANIME_START_DATE="2026-10-01")
    if setup == "采集本来就开着":
        cfg(ANIME_POLL_ENABLED=True)
    asked = _stub_confirm(monkeypatch, False)
    await _open_and_flip(user)
    await user.should_see("已保存，即时生效")
    assert asked == [], f"{setup}：不是首次使用，不该问 {asked}"
    assert config.ANIME_POLL_ENABLED is True


@pytest.mark.nicegui_main_file(_MAIN)
async def test_start_date_typed_in_the_form_counts(user: User, page, monkeypatch):
    """确认框教的恢复路径：取消 → 在表单里填开始使用日 → 再保存。这一刻日期只在表单里、库里还是空的，
    判定必须读表单值——读 config.ANIME_START_DATE 就会一直问、永远存不进去。"""
    page["groups"]("ANi")
    asked = _stub_confirm(monkeypatch, False)
    calls = _spy_rule(monkeypatch)
    await user.open("/settings")
    sw = [s for s in _live(user, ui.switch) if s.text.startswith("启用后台采集")]
    sw[0].value = True
    sd = [i for i in _live(user, ui.input) if i.props.get("label") == "开始使用日"]
    assert len(sd) == 1
    sd[0].value = "2026-10-01"
    assert config.ANIME_START_DATE == "", "前提：库里的开始使用日还是空的"
    UserInteraction(user, {next(b for b in _live(user, ui.button) if b.text == "保存")}, None).click()
    await user.should_see("已保存，即时生效")
    assert asked == [], asked
    assert [c[2] for c in calls] == ["2026-10-01"], calls
    assert config.ANIME_POLL_ENABLED is True and config.ANIME_START_DATE == "2026-10-01"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_double_click_save_opens_one_dialog(user: User, page, monkeypatch):
    """连点两下保存：只挂一个确认框。两个框一个"继续"一个"取消"时，最后一句会是
    "采集仍关着"——而采集已经开了。"""
    page["groups"]("ANi")
    S = sys.modules["pages.settings"]
    pending: list = []

    async def fake(title, note="", **kw):
        fut = asyncio.get_event_loop().create_future()
        pending.append(fut)
        return await fut
    monkeypatch.setattr(S, "confirm", fake)
    await user.open("/settings")
    [s for s in _live(user, ui.switch) if s.text.startswith("启用后台采集")][0].value = True
    btn = next(b for b in _live(user, ui.button) if b.text == "保存")
    UserInteraction(user, {btn}, None).click()
    UserInteraction(user, {btn}, None).click()
    await asyncio.sleep(0.2)
    assert len(pending) == 1, f"连点两下弹出了 {len(pending)} 个确认框"
    pending[0].set_result(True)
    await user.should_see("已保存，即时生效")
    assert config.ANIME_POLL_ENABLED is True
    assert not any("采集仍关着" in n for n in page["notes"]), page["notes"]
    # 框答完之后闸要放开：库里的开关回到关、再点保存，还能再问
    monkeypatch.setitem(config._v, "ANIME_POLL_ENABLED", False)
    UserInteraction(user, {btn}, None).click()
    await asyncio.sleep(0.2)
    assert len(pending) == 2, "确认框答完后闸没复位，之后再也问不出来"
    pending[1].set_result(False)
    await user.should_see("已取消保存")


@pytest.mark.nicegui_main_file(_MAIN)
async def test_backup_list_marks_pre_upgrade(user: User, page, tmp_path, monkeypatch):
    d = tmp_path / "bk"
    d.mkdir()
    (d / "autorss-full-20261101-000000-preupgrade.db").write_bytes(b"x")
    (d / "autorss-full-20261102-000000.db").write_bytes(b"x")
    monkeypatch.setattr(B, "BACKUP_DIR", d)
    await user.open("/settings")
    marks = [b for b in _live(user, ui.badge) if b.text == "升级前"]
    assert len(marks) == 1, "只有 -preupgrade 那一份标『升级前』"


@pytest.mark.nicegui_main_file(_MAIN)
async def test_check_failure_saves_without_asking(user: User, page, monkeypatch, caplog):
    """查库出错：不问（宁可漏报），保存照常，留一条日志。"""
    page["groups"]("ANi")
    from core import anime as A

    def boom(**kw):
        raise RuntimeError("库忙")
    monkeypatch.setattr(A, "list_source_groups", boom)
    asked = _stub_confirm(monkeypatch, False)
    await _open_and_flip(user)
    await user.should_see("已保存，即时生效")
    assert asked == [] and config.ANIME_POLL_ENABLED is True
    assert any("首次使用检查失败" in r.getMessage() for r in caplog.records)


# ================================================================ FLOW-11 迁移前自动备份

@pytest.fixture
def spy(monkeypatch):
    """记下 backup_now 与真正的迁移（command.upgrade）的先后；backup_now 被调时库处在哪一版。"""
    rec = {"order": [], "kw": [], "rev_at_backup": []}
    real_up = schema.command.upgrade

    def _up(cfg, target, *a, **k):
        rec["order"].append("migrate")
        return real_up(cfg, target, *a, **k)
    monkeypatch.setattr(schema.command, "upgrade", _up)
    rec["engine"] = None

    def _bk(**kw):
        rec["order"].append("backup")
        rec["kw"].append(kw)
        if rec["engine"] is not None:
            rec["rev_at_backup"].append(schema.current_revision(rec["engine"], "data"))
        if rec.get("raise"):
            raise OSError("No space left on device")
        return {"path": "/x/autorss-full-20260101-000000-preupgrade.db"}
    monkeypatch.setattr(B, "backup_now", _bk)
    monkeypatch.setattr(B, "verify", lambda p: (False, "打不开") if rec.get("bad") else (True, "可用"))
    return rec


def test_old_local_db_is_backed_up_before_migration(upgrade_from, spy, monkeypatch, cfg, caplog):
    """meta 已是新版、只有 data 链旧（切回本地库那种）：照样备一次，保留份数用用户的设置。"""
    cfg(BACKUP_KEEP=4)
    monkeypatch.setattr(config, "loaded_from_db", True)
    eng = upgrade_from(OLD)
    spy["order"].clear()
    monkeypatch.setattr(db, "meta_engine", eng)        # 业务库＝配置库那个文件（默认本地部署的形状）
    spy["engine"] = eng
    with caplog.at_level(logging.INFO, logger="autorss"):
        upgrade_from.to_head(eng)
    assert spy["order"] == ["backup", "migrate"], spy["order"]
    assert spy["rev_at_backup"] == [OLD], "备份必须发生在迁移之前"
    assert spy["kw"] == [{"keep": 4, "pre_upgrade": True}], spy["kw"]
    assert schema.current_revision(eng, "data") == schema.head_revision()
    head = schema.head_revision()
    assert any("升级前自动备份：" in r.getMessage() and OLD in r.getMessage() and head in r.getMessage()
               for r in caplog.records if r.levelno == logging.INFO), "日志要写明升级前自动备份与新旧版本号"



def test_head_db_is_not_backed_up(upgrade_from, spy, monkeypatch):
    eng = upgrade_from(schema.head_revision())
    monkeypatch.setattr(db, "meta_engine", eng)
    spy["order"].clear()
    upgrade_from.to_head(eng)
    assert spy["order"] == [], spy["order"]


def test_new_db_is_not_backed_up(tmp_path, spy, monkeypatch):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'new.db'}")
    monkeypatch.setattr(db, "meta_engine", eng)
    schema.upgrade(eng, "data")
    assert spy["order"] == ["migrate"], spy["order"]


def test_other_sqlite_file_is_not_backed_up(upgrade_from, spy):
    """backup_now 恒快照配置库那个文件；升级的是另一个文件时备出来的不是它，不备。"""
    eng = upgrade_from(OLD)
    spy["order"].clear()
    upgrade_from.to_head(eng)
    assert spy["order"] == ["migrate"], spy["order"]


def test_mysql_is_not_backed_up(spy, monkeypatch):
    eng = sa.create_engine("mysql+pymysql://u:p@127.0.0.1:1/x")    # 不连：版本号与迁移都打桩

    @contextmanager
    def _same(e):
        yield e
    monkeypatch.setattr(schema, "current_revision", lambda e, r: OLD)
    monkeypatch.setattr(schema, "_ddl_engine", _same)
    monkeypatch.setattr(schema.command, "upgrade", lambda cfg, target: spy["order"].append("migrate"))
    schema.upgrade(eng, "data")
    assert spy["order"] == ["migrate"], spy["order"]


def test_backup_failure_only_warns(upgrade_from, spy, monkeypatch, caplog):
    eng = upgrade_from(OLD)
    monkeypatch.setattr(db, "meta_engine", eng)
    spy["order"].clear()
    spy["raise"] = True
    with caplog.at_level(logging.WARNING, logger="autorss"):
        upgrade_from.to_head(eng)
    assert spy["order"] == ["backup", "migrate"], spy["order"]
    assert schema.current_revision(eng, "data") == schema.head_revision()
    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "升级前自动备份失败" in r.getMessage()]
    assert len(warns) == 1, [r.getMessage() for r in caplog.records]


# ---------------- 保留份数：升级前那份不挤掉日常备份

@pytest.fixture
def fresh_backup_dir(tmp_path, monkeypatch):
    d = tmp_path / "backups"
    d.mkdir()
    monkeypatch.setattr(B, "BACKUP_DIR", d)
    return d


def test_pre_upgrade_backup_does_not_count_against_daily_ones(clean_tables, fresh_backup_dir):
    old = time.time() - 86400
    for i in range(3):
        p = fresh_backup_dir / f"autorss-full-2026080{i}-000000.db"
        p.write_bytes(b"x")
        os.utime(p, (old + i, old + i))
    res = B.backup_now(keep=3, pre_upgrade=True)
    assert res["path"].endswith("-preupgrade.db") and res["pruned"] == [], res
    items = B.list_backups()
    assert len(items) == 4 and [d["pre_upgrade"] for d in items].count(True) == 1, items
    res2 = B.backup_now(keep=3)                        # 日常备份：自己那组 4 份 → 删最旧的 1 份
    assert res2["pruned"] == ["autorss-full-20260800-000000.db"], res2["pruned"]
    assert os.path.exists(res["path"]), "升级前那份不该被日常的保留份数删掉"


def _meta_at(tmp_path, rev, backend=None):
    """一个只升了 meta 链、停在 rev 的配置库文件；backend 非空就往 setting 里写 DB_BACKEND。"""
    p = tmp_path / "meta-old.db"
    eng = sa.create_engine(f"sqlite:///{p}")
    schema.upgrade(eng, "meta", rev)
    if backend:
        with sqlite3.connect(p) as c:
            c.execute("INSERT INTO setting(key, value) VALUES ('DB_BACKEND', ?)", (backend,))
    return eng


def test_meta_chain_before_config_loaded_does_not_prune(tmp_path, spy, monkeypatch, cfg):
    """meta 链跑在 load_from_db 之前：此刻 BACKUP_KEEP 是默认值，拿它去清会把用户设的 30 份删到 7 份。"""
    cfg(BACKUP_KEEP=7)
    monkeypatch.setattr(config, "loaded_from_db", False)
    eng = _meta_at(tmp_path, OLD)
    monkeypatch.setattr(db, "meta_engine", eng)
    spy["order"].clear()
    schema.upgrade(eng, "meta")
    assert spy["order"] == ["backup", "migrate"], spy["order"]
    assert spy["kw"] == [{"keep": 0, "pre_upgrade": True}], spy["kw"]


def test_meta_chain_with_mysql_backend_in_db_is_not_backed_up(tmp_path, spy, monkeypatch, cfg):
    """config 还没读出来（DB_BACKEND 看着是默认的 sqlite）：业务库指向要直接从库里读，是 MySQL 就不备。"""
    cfg(DB_BACKEND="sqlite")
    monkeypatch.setattr(config, "loaded_from_db", False)
    eng = _meta_at(tmp_path, OLD, backend="mysql")
    monkeypatch.setattr(db, "meta_engine", eng)
    spy["order"].clear()
    schema.upgrade(eng, "meta")
    assert spy["order"] == ["migrate"], spy["order"]


def test_newer_db_than_code_is_refused_without_backup(tmp_path, spy, monkeypatch):
    """库比代码新：拒绝启动、不迁移，也不备——否则旧版程序每重启一次就多一份。"""
    eng = _meta_at(tmp_path, OLD)
    monkeypatch.setattr(db, "meta_engine", eng)
    monkeypatch.setattr(schema, "current_revision", lambda e, r: "ffffffffffff")
    spy["order"].clear()
    with pytest.raises(RuntimeError, match="比当前程序新"):
        schema.upgrade(eng, "meta")
    assert spy["order"] == [], spy["order"]


def test_backup_failing_self_check_only_warns(tmp_path, spy, monkeypatch, caplog):
    """自检不过：只告警、照常升级；也不算"本进程备过了"——紧接着的 data 链那次再试一份。"""
    eng = _meta_at(tmp_path, OLD)
    schema.upgrade(eng, "data", OLD)
    monkeypatch.setattr(db, "meta_engine", eng)
    spy["order"].clear()
    spy["bad"] = True
    with caplog.at_level(logging.WARNING, logger="autorss"):
        schema.upgrade(eng, "meta")
        schema.upgrade(eng, "data")
    assert schema.current_revision(eng, "data") == schema.head_revision()
    assert spy["order"] == ["backup", "migrate", "backup", "migrate"], spy["order"]
    assert any(r.levelno == logging.WARNING and "自检未通过" in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]


# ---------------- 一次真实启动：只备一份，而且拿回旧版程序起得来

@pytest.fixture
def old_code(tmp_path, monkeypatch):
    """old_code(True) 之后 schema 看到的是"少了最新一版迁移"的旧程序（alembic 目录拷一份、删掉 head 那个文件）。"""
    real_root = schema._ROOT
    new_head = schema.head_revision()
    old_root = tmp_path / "oldcode"
    shutil.copytree(real_root / "alembic", old_root / "alembic",
                    ignore=shutil.ignore_patterns("__pycache__"))
    victims = [p for p in (old_root / "alembic" / "versions").glob("*.py") if new_head in p.name]
    assert len(victims) == 1, victims
    victims[0].unlink()

    def _switch(old: bool):
        monkeypatch.setattr(schema, "_ROOT", old_root if old else real_root)
    _switch.new_head = new_head
    return _switch


def _startup(monkeypatch, eng):
    """与 main 同一条序列：init_db（meta 链）→ apply_configured_backend（data 链）。
    load_from_db 不调（它读的是这份新库里几乎为空的 setting），只把"那一刻 config 还没读出来"模拟出来。"""
    monkeypatch.setattr(db, "meta_engine", eng)
    monkeypatch.setattr(db, "engine", eng)
    monkeypatch.setattr(config, "loaded_from_db", False)
    db.init_db()
    monkeypatch.setattr(config, "loaded_from_db", True)
    db.apply_configured_backend()


def test_one_startup_backs_up_once_before_meta_and_old_program_boots(testdb, tmp_path, monkeypatch, cfg, old_code):
    cfg(DB_BACKEND="sqlite", BACKUP_KEEP=7)
    bdir = tmp_path / "backups"
    bdir.mkdir()
    monkeypatch.setattr(B, "BACKUP_DIR", bdir)
    eng = db._make_sqlite_engine(str(tmp_path / "live.db"))

    old_code(True)                                   # ① 旧版程序把库建起来
    old_head = schema.head_revision()
    _startup(monkeypatch, eng)
    assert list(bdir.iterdir()) == [], "全新库不该备"

    old_code(False)                                  # ② 新版程序启动：meta、data 两条链都要迁移
    _startup(monkeypatch, eng)
    assert schema.current_revision(eng, "data") == old_code.new_head
    pre = [p for p in bdir.iterdir() if p.name.endswith("-preupgrade.db")]
    assert len(pre) == 1 and len(list(bdir.iterdir())) == 1, [p.name for p in bdir.iterdir()]
    with sqlite3.connect(pre[0]) as c:
        meta_rev = c.execute("select version_num from alembic_version_meta").fetchone()[0]
        data_rev = c.execute("select version_num from alembic_version_data").fetchone()[0]
    assert (meta_rev, data_rev) == (old_head, old_head), "快照必须拍在 meta 链迁移之前"

    restored = tmp_path / "restored.db"              # ③ 退回旧版程序 + 恢复这份：必须起得来
    shutil.copy(pre[0], restored)
    eng2 = db._make_sqlite_engine(str(restored))
    old_code(True)
    _startup(monkeypatch, eng2)
    assert schema.current_revision(eng2, "meta") == old_head


def test_conftest_isolates_pre_upgrade_backup():
    """【必须排在上面那条真实启动用例之后】它在本进程里备过一份。conftest 的 autouse 夹具要做到：
    备份目录不在仓库的 data/ 下（判据写松时临时库的快照与 prune 会落到真备份上），"已备过"的记录每条用例清空。"""
    assert not str(B.BACKUP_DIR).startswith(str(config.BASE_DIR / "data")), B.BACKUP_DIR
    assert schema._pre_upgrade_done == set(), schema._pre_upgrade_done
    # 会话级那半：testdb（session 级）的 init_db 跑在一切函数级夹具之前，只靠函数级的改向挡不住它。
    # 运行时从用例里看不出夹具的作用域，只能查声明。
    import ast
    from pathlib import Path
    tree = ast.parse((Path(__file__).parent / "conftest.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_session_backup_dir")
    kw = {k.arg: ast.literal_eval(k.value) for d in fn.decorator_list if isinstance(d, ast.Call) for k in d.keywords}
    assert kw.get("scope") == "session" and kw.get("autouse") is True, kw
