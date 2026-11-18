"""运维三条的守卫：deploy.sh 按启动日志判成败、自动备份失败日志限频 + 超期横幅。
（SQLite 迁移原子性在 tests/test_migration_atomic.py。）每条变异后必须红。
"""
import logging
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_FAIL_INIT = "启动初始化失败，系统停摆"
_FAIL_DOWN = "业务数据库停摆中"
_READY = "自动备份协程启动"


# ---------------- deploy.sh：初始化失败不许打「✅ 部署完成」 ----------------

def test_deploy_markers_still_exist_in_source():
    """deploy.sh 按这几条日志文案判成败；源码里改了文案而 deploy.sh 没跟，就会恒判超时或漏判失败。"""
    sh = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    worker = (ROOT / "core" / "worker.py").read_text(encoding="utf-8")
    for s in (_FAIL_INIT, _FAIL_DOWN, _READY):
        assert s in sh, s
    assert _FAIL_INIT in main and _FAIL_DOWN in main
    assert _READY in worker


def _run_tail(tmp_path, log: str, active: bool = True):
    """把 deploy.sh 从 restart 前记 T0 那一行起到结尾单跑；systemctl/journalctl/sleep 换成假的。"""
    src = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    tail = src[src.index('T0="$(date +%s)"'):]
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "systemctl").write_text(
        '#!/bin/bash\n[ "$1" = is-active ] && exit "$FAKE_ACTIVE"\nexit 0\n')
    (bin_ / "journalctl").write_text(
        '#!/bin/bash\nif [[ " $* " == *" --since "* ]]; then printf "%s\\n" "$FAKE_LOG"; '
        'else echo "最近日志尾巴"; fi\n')
    (bin_ / "sleep").write_text("#!/bin/bash\nexit 0\n")
    for f in bin_.iterdir():
        f.chmod(0o755)
    (tmp_path / ".env").write_text("WEB_PORT=2333\nWEB_HOST=127.0.0.1\n", encoding="utf-8")
    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", FAKE_LOG=log,
               FAKE_ACTIVE="0" if active else "3")
    r = subprocess.run(["bash", "-c", "set -euo pipefail\nSVC=autorss\n" + tail],
                       cwd=tmp_path, env=env, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")
@pytest.mark.parametrize("log, active, ok", [
    (f"NiceGUI ready to go\n{_READY}（开）", True, True),
    # NiceGUI 的 ready 在 _startup 之前就打了：它出现了、初始化随后失败，照样要判失败
    (f"NiceGUI ready to go\n{_FAIL_INIT}\n{_FAIL_DOWN}，采集…\n{_READY}（开）", True, False),
    (f"{_FAIL_DOWN}，采集/下载/同步全部暂停", True, False),
    ("NiceGUI ready to go", True, False),             # 超时：不许当成功
    ("", False, False),                                # 进程没起来
])
def test_deploy_judges_by_startup_log(tmp_path, log, active, ok):
    code, out = _run_tail(tmp_path, log, active)
    assert ("✅" in out) is ok, out
    assert (code == 0) is ok, (code, out)


# ---------------- 自动备份：失败日志限频，恢复记一行 ----------------

def test_auto_backup_failure_logs_once_then_recovery_is_noted(monkeypatch, cfg, caplog):
    from db import backup as B
    import config

    cfg(BACKUP_ENABLED=True, BACKUP_INTERVAL_HOURS=24, BACKUP_KEEP=5, BACKUP_LAST="")

    def boom(keep):
        raise OSError("No space left on device")
    monkeypatch.setattr(B, "backup_now", boom)
    caplog.set_level(logging.INFO, logger="autorss")
    for _ in range(10):
        assert B.auto_tick() is False
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errs) == 1 and "自动备份失败" in errs[0].getMessage(), [r.getMessage() for r in errs]

    caplog.clear()
    monkeypatch.setattr(B, "backup_now", lambda keep: {"path": "x.db", "note": "ok"})
    monkeypatch.setattr(B, "verify", lambda p: (True, ""))
    monkeypatch.setattr(config, "set_many", lambda u: None)
    assert B.auto_tick() is True
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("恢复" in m and "10" in m for m in infos), infos

    caplog.clear()                                     # 恢复后计数归零：下一次失败又是一条 ERROR
    monkeypatch.setattr(B, "backup_now", boom)
    B.auto_tick()
    assert [r.levelno for r in caplog.records] == [logging.ERROR]


# ---------------- 自动备份超期：全站横幅 ----------------

def _iso(dt):
    return dt.isoformat(timespec="seconds")


def test_backup_overdue_banner(cfg, monkeypatch):
    from core import health

    now = datetime.now()
    cfg(BACKUP_ENABLED=True, BACKUP_INTERVAL_HOURS=24)
    cfg(BACKUP_LAST=_iso(now - timedelta(hours=49)))
    assert health.backup_problem(now) and "自动备份" in health.backup_problem(now)
    cfg(BACKUP_LAST=_iso(now - timedelta(hours=47)))
    assert health.backup_problem(now) is None, "未超 2 个间隔不该挂"
    cfg(BACKUP_ENABLED=False, BACKUP_LAST=_iso(now - timedelta(days=30)))
    assert health.backup_problem(now) is None, "关了自动备份不该挂"


def test_backup_never_succeeded_counts_from_process_start(cfg, monkeypatch):
    from core import health

    now = datetime.now()
    cfg(BACKUP_ENABLED=True, BACKUP_INTERVAL_HOURS=24, BACKUP_LAST="")
    monkeypatch.setattr(health, "_started", now - timedelta(minutes=5))
    assert health.backup_problem(now) is None, "刚启动、还没备过，不该误报"
    monkeypatch.setattr(health, "_started", now - timedelta(hours=49))
    assert "一次都没成功" in (health.backup_problem(now) or "")
