"""依赖锁文件守卫 + 断档『知道了』只清用户看到的那几条。变异后必须红。"""
import json
import re
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

import config
from core import health

ROOT = Path(__file__).resolve().parent.parent


def _reqs(name):
    out = []
    for ln in (ROOT / name).read_text(encoding="utf-8").splitlines():
        ln = ln.split("#")[0].strip()
        if ln:
            out.append(Requirement(ln))
    return out


def test_every_direct_dependency_is_pinned_in_lock_and_meets_the_floor():
    """deploy.sh 只按锁文件装：txt 里加了依赖而锁文件没跟，生产就装不上它；锁的版本低于下限，就是锁了没测过的旧版。"""
    lock = {canonicalize_name(r.name): r for r in _reqs("requirements.lock")}
    for r in _reqs("requirements.txt"):
        n = canonicalize_name(r.name)
        assert n in lock, f"{r.name} 不在 requirements.lock 里"
        spec = [s for s in lock[n].specifier]
        assert len(spec) == 1 and spec[0].operator == "==", f"{r.name} 在锁文件里不是 == 固定版本：{lock[n]}"
        assert r.specifier.contains(spec[0].version, prereleases=True), \
            f"{r.name} 锁的 {spec[0].version} 不满足 requirements.txt 的 {r.specifier}"


def test_deploy_installs_from_the_lock():
    sh = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert re.search(r"^uv pip install -r requirements\.lock$", sh, re.M), "deploy.sh 没按锁文件装依赖"
    assert not re.search(r"^uv pip install -r requirements\.txt", sh, re.M)


def _gap(src, frm):
    return {"src": src, "from": frm, "to": frm, "at": frm}


def test_ack_only_clears_the_gaps_the_user_saw(cfg, monkeypatch):
    """页面打开后后台新记的断档用户没看过；它还是锁源兜底与缺集自动补的断档闸，不能跟着『知道了』一起清。"""
    monkeypatch.setattr(config, "set_many", lambda kv: config._v.update(kv))
    old = _gap("nyaa", "2026-11-01T01:00")
    cfg(FEED_GAPS=json.dumps([old]))
    shown = health.gaps()                                   # 横幅渲染时拿到的
    new = _gap("Mikan", "2026-11-02T01:00")
    config._v["FEED_GAPS"] = health.gaps_with([new])        # 渲染之后 worker 又记了一条
    health.clear_gaps(shown)
    assert health.gaps() == [new]
    health.clear_gaps()                                     # 不传 = 旧行为：全清
    assert health.gaps() == []


def test_banner_ack_passes_the_rendered_gaps():
    src = (ROOT / "pages" / "layout.py").read_text(encoding="utf-8")
    assert "health.clear_gaps(gaps)" in src and "health.clear_gaps()" not in src
