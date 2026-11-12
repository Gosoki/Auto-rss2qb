"""文档与说明文字背后的事实。

说明文字本身钉不住，这组钉的是"照着文档做会不会出事"和"文档点名的东西还在不在"：
· 设置页『备份』的恢复步骤真照抄跑一遍（它是全项目唯一一份恢复说明，跑不通就是事故现场）；
· 设置页两句说明背后的行为（bgm 请求次数含首次、带 token 的回调认非本机对端）；
· 每个用户可调的设置键都有页面入口，只在建库时读一次的三个键是显式豁免、不许多出读者；
· DECISIONS 顶部"还待拍板"的名单里没有正文已标 ✅/❌ 的编号；
· GLOSSARY / README / tests/README 点名的巡检类别、函数、文件都真实存在；
· deploy.sh 收尾提示按 .env 里实际的 WEB_HOST 说话，.env.example 不许有生效的 WEB_HOST。
不钉条数、秒数、协程个数这类会漂的数字——每加一条用例就红的守卫是误报。
"""
import ast
import re
import shutil
import subprocess
import types
from pathlib import Path

import httpx
import pytest

import config

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ---------- 设置页『备份』的恢复步骤（DOC-1） ----------

def _restore_help() -> str:
    """_backup_panel 里那段『恢复步骤』的原文（按源码取，测的就是页面上显示的那一份）。"""
    tree = ast.parse(_read("pages/settings.py"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_backup_panel")
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_help" and node.args
                and isinstance(node.args[0], ast.Constant) and str(node.args[0].value).startswith("恢复步骤")):
            return node.args[0].value
    raise AssertionError("前提：_backup_panel 里没找到『恢复步骤』那段 _help")


def _restore_commands() -> tuple[str, str]:
    lines = _restore_help().split("\n")
    i2 = next(i for i, ln in enumerate(lines) if ln.startswith("2. "))
    step2 = lines[i2 + 1].strip()
    i3 = next(i for i, ln in enumerate(lines) if ln.startswith("3. "))
    step3 = lines[i3 + 1].strip()                 # 整行原文，用户复制的就是它
    assert step2.startswith("cd ") and step3.startswith("cp "), (step2, step3)
    return step2, step3


@pytest.mark.skipif(shutil.which("sh") is None, reason="no sh")
@pytest.mark.parametrize("with_sidecars", [True, False], ids=["有-wal-shm", "无-wal-shm"])
def test_restore_steps_run_as_written(tmp_path, with_sidecars):
    """照页面上的第 2、3 步在程序目录里敲一遍：库换成选中的备份、原库改名留底、-wal 被挪走。
    第 2 步的 for 在没有 -shm 时退出码本来就是 1，只看第 3 步的退出码。"""
    app = tmp_path / "app dir"                     # 目录名带空格
    (app / "data" / "backups").mkdir(parents=True)
    live = app / "data" / "autorss.db"
    live.write_bytes(b"LIVE")
    if with_sidecars:
        (app / "data" / "autorss.db-wal").write_bytes(b"WAL")
        (app / "data" / "autorss.db-shm").write_bytes(b"SHM")
    (app / "data" / "backups" / "autorss-full-20261101-090000.db").write_bytes(b"BACKUP")
    step2, step3 = _restore_commands()
    step3 = step3.replace("<选中的那份>", "autorss-full-20261101-090000.db")
    script = f"{step2}\n{step3}\n"                 # 同一个 shell 里接着敲
    r = subprocess.run(["sh", "-c", script], cwd=app, capture_output=True, text=True)
    assert r.returncode == 0, (script, r.stderr)
    assert live.read_bytes() == b"BACKUP"
    assert (app / "data" / "autorss.db.before-restore").read_bytes() == b"LIVE"
    assert not (app / "data" / "autorss.db-wal").exists()
    if with_sidecars:
        assert (app / "data" / "autorss.db-wal.before-restore").read_bytes() == b"WAL"


# ---------- 设置页说明背后的行为（DOC-5） ----------

@pytest.mark.parametrize("times", [1, 3])
async def test_enrich_retry_times_is_total_attempts(cfg, monkeypatch, times):
    """设置页写的是『bgm 请求最多尝试几次（含首次）』：N 就是总请求数，不是"首次之外再重试 N 次"。"""
    from services import enrich
    cfg(ENRICH_RETRY_TIMES=times)

    async def _nosleep(_):
        return None
    monkeypatch.setattr(enrich.asyncio, "sleep", _nosleep)
    calls = []

    async def boom():
        calls.append(1)
        raise httpx.ConnectError("x")
    with pytest.raises(httpx.HTTPError):
        await enrich._retryable(boom)
    assert len(calls) == times


async def test_callback_with_token_works_from_another_machine(cfg, monkeypatch):
    """设置页写的是『qB 在另一台机器时必须填 token』：填对了 token，非本机对端就得放行；没填 token 就拒。"""
    from core import engine
    from pages import api
    monkeypatch.setattr(engine, "mark_done_by_hash", lambda h: True)
    req = types.SimpleNamespace(client=types.SimpleNamespace(host="10.9.9.9"), headers={})
    cfg(QB_CALLBACK_TOKEN="s3cret")
    assert (await api.qb_done(req, hash="zz", t="s3cret"))["ok"] is True
    assert (await api.qb_done(req, hash="zz", t="nope"))["ok"] is False
    cfg(QB_CALLBACK_TOKEN="")
    assert (await api.qb_done(req, hash="zz", t=""))["ok"] is False


# ---------- 设置键的页面入口（DOC-6） ----------

_PAGES = "\n".join(p.read_text(encoding="utf-8") for p in sorted((_ROOT / "pages").glob("*.py")))
# 只在首次建库种默认源组时读一次（core.anime.seed_source_groups），之后在订阅源页改
_SEED_ONLY = {"ANI_RSS_URL", "MIKAN_ENABLED", "MIKAN_RSS_URL"}


def _on_page(k: str) -> bool:
    return bool(re.search(r"""["']%s["']|config\.%s\b""" % (k, k), _PAGES))


def test_every_user_key_has_a_page_entry():
    """规矩是"规则 / 阈值一律进设置页"：_SPEC 里非运行时、非豁免的键都得在 pages/ 里出现。"""
    assert len(config._SPEC) > 50, "前提：_SPEC 没读到"
    missing = [k for k in config._SPEC
               if k not in config._RUNTIME_KEYS and k not in _SEED_ONLY and not _on_page(k)]
    assert missing == [], f"这些设置键没有任何页面入口：{missing}"


def test_seed_only_keys_have_no_page_entry_and_no_new_reader():
    """_SPEC 上的注释说这三个键只在建库时读：多出一个运行期读者，那句注释就成了假话。"""
    for k in _SEED_ONLY:
        assert k in config._SPEC and not _on_page(k), k
    readers = {}
    for p in _ROOT.rglob("*.py"):
        rel = p.relative_to(_ROOT).as_posix()
        if rel.startswith(("tests/", ".venv/")) or rel in ("config.py", "conftest.py"):
            continue
        for k in _SEED_ONLY:
            n = len(re.findall(r"config\.%s\b" % k, p.read_text(encoding="utf-8")))
            if n:
                readers[(rel, k)] = n
    assert readers == {("core/anime.py", "ANI_RSS_URL"): 1, ("core/anime.py", "MIKAN_ENABLED"): 1,
                       ("core/anime.py", "MIKAN_RSS_URL"): 1, ("sources/mikan.py", "MIKAN_RSS_URL"): 1}, readers


# ---------- DECISIONS 顶部总览（DOC-4） ----------

def _numbers(seg: str) -> set[int]:
    """`E-a ~ E-b` 展开；后面跟"复议"的不算（复议的是已定的那条）。数字整段匹配：不然"E-31 复议"会被切成 E-3。"""
    out: set[int] = set()
    for a, b in re.findall(r"E-(\d+)(?:\s*~\s*E-(\d+))?(?!\d)(?!\s*复议)", seg):
        out |= set(range(int(a), int(b or a) + 1))
    return out


def _decisions():
    txt = _read("docs/DECISIONS.md")
    head = txt.splitlines()[0]
    pending = set().union(*(_numbers(seg) for seg in re.findall(r"待拍板：\**([^；]*)", head)))
    settled = _numbers(re.search(r"；([^；]*?)均已定", head).group(1))
    rows = {int(m.group(1)) for m in re.finditer(r"^>?\s*\|\s*\*\*E-(\d+)\*\*", txt, re.M)}
    done = {int(m.group(1)) for m in
            re.finditer(r"^>?\s*\|\s*\*\*E-(\d+)\*\*\s*[^|]*?(✅|❌)", txt, re.M)}
    return pending, settled, rows, done


def test_decisions_header_lists_nothing_already_done():
    pending, _, _, done = _decisions()
    assert {58, 72} <= pending and 31 not in pending, f"前提：总览没解析对：{sorted(pending)}"
    assert len(done) > 20, "前提：正文的 ✅/❌ 行没解析出来"
    assert not pending & done, f"总览里列为待拍板、正文却已标 ✅/❌：{sorted(pending & done)}"


def test_decisions_every_entry_is_pending_or_settled():
    """正文每一条要么在总览的待拍板名单里，要么行首标了 ✅/❌，要么在总览"均已定"那一串里——
    做完了却既不标 ✅ 也从名单里拿掉的条目，读总览的人就再也找不到它。"""
    pending, settled, rows, done = _decisions()
    assert {1, 39, 54} <= settled, f"前提：总览『均已定』那串没解析对：{sorted(settled)}"
    orphan = sorted(rows - pending - settled - done)
    assert not orphan, f"这些条目既不在待拍板名单、也没标 ✅/❌、也不在『均已定』里：{orphan}"


# ---------- GLOSSARY（DOC-9） ----------

def _glossary_section(head: str) -> str:
    return _read("docs/GLOSSARY.md").split(f"\n## {head}")[1].split("\n## ")[0]


def test_glossary_names_every_alert_kind():
    from core import alerts
    sec = _glossary_section("十九、")
    missing = [k for k in alerts.KINDS if f"`{k}`" not in sec]
    assert not missing, f"GLOSSARY §十九 没写这几类巡检发现：{missing}"


def test_status_word_tables_point_at_each_other():
    """状态词表分两处写（layout.py 总表 + GLOSSARY §二十二 易混词），只看一处的人得能找到另一处。"""
    lay = _read("pages/layout.py")
    term = lay[lay.index("# ---- 全站术语表"):lay.index("\nNAV = ")]
    assert "docs/GLOSSARY.md §二十二" in term, "layout.py 术语表注释要指到 GLOSSARY §二十二"
    sec = _glossary_section("二十二、")
    assert "pages/layout.py" in sec and "订阅中" in sec and "在追" in sec, "GLOSSARY §二十二 要指回 layout.py 并写易混词"


def test_glossary_symmetric_names_exist_on_both_sides():
    """§五 说这些名字番剧 / 剧场版两条线各有一份：按"两侧都有"判，只看任一侧会放过改名的那一侧。"""
    sec = _glossary_section("五、")
    names = {n for n in re.findall(r"`([a-z_]+)`", sec) if "_" in n}
    assert len(names) >= 10, "前提：§五 的名字没抽出来"

    def _defs(rel):
        return {n.name for n in ast.walk(ast.parse(_read(rel)))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    da, dm = _defs("core/anime.py"), _defs("core/movies.py")
    gone = sorted(n for n in names if n not in da or n not in dm)
    assert not gone, f"GLOSSARY §五 说两条线各有一份的名字，至少一侧已经没有了：{gone}"


# ---------- README / tests/README 点名的文件（DOC-7 / DOC-8） ----------

def test_tests_readme_names_only_existing_files():
    names = set(re.findall(r"`(test_[a-z0-9_]+\.py)`", _read("tests/README.md")))
    assert len(names) >= 15, "前提：tests/README 的文件名没抽出来"
    gone = sorted(n for n in names if not (_ROOT / "tests" / n).exists())
    assert not gone, f"tests/README.md 点名的文件不存在：{gone}"


def test_readme_links_point_to_existing_files():
    links = set(re.findall(r"\]\(((?:docs|tests)/[^)#]+)\)", _read("README.md")))
    assert len(links) >= 4, "前提：README 的相对链接没抽出来"
    gone = sorted(x for x in links if not (_ROOT / x).exists())
    assert not gone, f"README.md 链到的文件不存在：{gone}"


# ---------- deploy.sh 收尾提示 / .env.example（DOC-12） ----------

def _done_hint(tmp_path, env_text: str) -> str:
    src = _read("deploy.sh")
    block = src.split("# >>> done-hint")[1].split("# <<< done-hint")[0].split("\n", 1)[1]
    (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    script = "set -euo pipefail\nSVC=autorss; PORT=2333; IP=10.9.8.7; LAN_HINT=\n" + block
    r = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")
@pytest.mark.parametrize("env, exposed", [
    ("WEB_PORT=2333\nWEB_HOST=0.0.0.0\n", True),
    ("WEB_PORT=2333\nWEB_HOST=127.0.0.1\n", False),
    ("WEB_HOST=localhost\n", False),
    ("WEB_HOST=127.0.0.1\nWEB_HOST=0.0.0.0\n", True),     # dotenv 取最后一条：实际绑的是 0.0.0.0
    ("WEB_HOST=0.0.0.0\nWEB_HOST=127.0.0.1\n", True),     # 拿不准 → 照样警告
    ('WEB_HOST="0.0.0.0"\n', True),
    ("WEB_HOST=::\n", True),
    ("WEB_HOST=::10\n", True),                            # 前缀像 ::1，但不是回环
    ("WEB_HOST=192.168.1.5\n", True),
])
def test_done_hint_follows_the_env(tmp_path, env, exposed):
    out = _done_hint(tmp_path, env)
    assert "无鉴权" in out, out                       # 两个分支都得提无鉴权
    if exposed:
        assert "已对局域网开放" in out and "http://10.9.8.7:2333" in out, out
    else:
        assert "已对局域网开放" not in out and "http://127.0.0.1:2333" in out and "10.9.8.7" not in out, out
    assert "WEB_ALLOW_CIDRS" not in out and "允许网段" in out, "指引要用设置页上看得见的名字"


def test_env_example_never_activates_web_host():
    """`.env.example` 里 WEB_HOST 只能是注释行：deploy.sh 靠"没有这一行"才补 WEB_HOST=0.0.0.0，
    有了生效行，全新部署的容器就只绑回环、从外面进不来。"""
    lines = _read(".env.example").splitlines()
    assert any(x.lstrip("# ").startswith("WEB_HOST=") for x in lines), "前提：.env.example 要提到 WEB_HOST"
    assert not [x for x in lines if x.lstrip().startswith("WEB_HOST=")], lines
