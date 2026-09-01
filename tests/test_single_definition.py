"""(R15) 「同一件事只此一份」——模块级同名函数的白名单守卫。

本项目最常见的缺陷形状是**广度错误**：同一件事有 N 处，只改了一处。
最容易滋生它的土壤就是「两个文件各写了一份同名函数」——grep 找到一处、改完、收工，
另一处静静地保持旧行为，而且两边都不报错。

这条用例不禁止重名（番剧/剧场版两条线本来就是对称的），它做的是：
**任何跨文件的模块级重名都必须在下面这张表里登记，并写清为什么是有意的。**
新增一处未登记的重名会当场变红，逼作者要么合并、要么解释。

历史上被这个形状咬过的（已合并）：
· `parse_bgm_id` —— core/manual.py 与 pages/layout.py 各一份、行为逐条相同（实测 7/7 一致），
  而调用点分家：手动下载走 core 那份、四个 UI 绑定入口走 pages 那份。
  DECISIONS.md 的 E-13「收紧 parse_bgm_id 判据」若在这个状态下实施，必然只收紧一半。
· `quarter_year` —— core/engine.py 曾自己写 `2000 + int(q[:2])`，而 format_quarter 的 yyyy
  占位另写一份 `"20"+yy`；两份共享同一个「两位年一律当 20xx」的错。
"""
import ast
from collections import defaultdict
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent

# 允许重名的清单：名字 → 为什么是有意的。**加条目时必须写理由**。
_INTENTIONAL = {
    # ---- 番剧 / 剧场版两条线的对称实现（docs/GLOSSARY.md 第一节：有意区分，不要统一）----
    "_has_handled_torrents": "两条线各判各的表（AnimeTorrent / MovieTorrent）",
    "_set_status": "同上",
    "_terminal_torrent_rows": "同上",
    "deleted_torrent_rows": "同上",
    "excluded_torrent_rows": "同上",
    "failed_rows": "同上",
    "overview": "两个仪表盘的聚合口径不同（番剧按季度三桶、剧场版按年份）",
    "source_map": "同上",
    "bind_preview": "绑定前回显：两条线的可删对象与告警维度不同（剧场版没有集号，不产生编号冲突告警）",
    "set_quarter": ("两条线各改各的表；且【语义不同】——番剧改的是『季度』（季母有意义、"
                    "影响 Season 子目录），剧场版只用其中的年份（页面上那一栏就叫年份，"
                    "归档目录走 MOVIE_QUARTER_FMT）。合并会把两个不同的概念挤进一个函数"),
    # ---- 门面：实现在 core/engine.py，两条线各一个薄包装（各自 docstring 写明「实现见 engine.*」）----
    "exclude_torrent": "实现在 core/engine.py，anime/movies 各转一层以带上自己的表",
    "reset_downloading": "同上",
    "sync_qb_status": "同上",
    "unexclude_torrent": "同上",
    # ---- 同名但语义不同，已登记进 docs/GLOSSARY.md ----
    "verify": "db/backup.py 是『这份备份打得开吗』(bool,str)；db/transfer.py 是『迁完行数对得上吗』(list)",
    "_parse_date": ("两份【接受的格式与返回类型都不同】：core/anime 收 YYYY-MM / YYYY 残缺日期、返回 date；"
                    "services/enrich 拒绝残缺日期、另收 MM/DD/YYYY、返回 datetime。见 GLOSSARY.md"),
}

_SKIP_DIRS = ("tests", ".venv", "alembic", "__pycache__", "docs", "data")


def _branch_bodies(node):
    """把 If / Try / With 的各个分支体（语句列表）产出来，含 except 处理器。"""
    for attr in ("body", "orelse", "finalbody"):
        got = getattr(node, attr, None)
        if got:
            yield got
    for h in getattr(node, "handlers", []) or []:
        if getattr(h, "body", None):
            yield h.body


def _importable_defs(node, out, rel, in_class=False):
    """递归收集"能作为模块属性被 import 到"的定义名。

    只看 tree.body 是不够的 —— 实测这四种写法都能把重复藏起来（而且都是现实里会出现的）：
      · 条件定义：`if sys.version_info >= (3, 0): def dedup_key(...)`
      · try 包住：`try: def quarter_sort_key(...) except ImportError: ...`
      · lambda 赋值：`parse_bgm_id = lambda text: ...`
      · 类里的静态方法：`class _Helper: @staticmethod def parse_bgm_id(...)`
        （它不是模块属性，但同样是一份"第二实现"，改判据时一样会被漏掉）
    所以这里递归进 If / Try / With 的各个分支体，并把类体单独标出来。
    """
    for n in getattr(node, "body", []):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name].append(f"{rel}::类内" if in_class else rel)
        elif isinstance(n, ast.ClassDef):
            _importable_defs(n, out, rel, in_class=True)
        elif isinstance(n, ast.Assign) and isinstance(n.value, ast.Lambda):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out[t.id].append(f"{rel}::lambda")
        elif isinstance(n, (ast.If, ast.Try, ast.With, ast.AsyncWith)):
            # 【按分支的语句列表递归，不要按单条语句】第一版对每个 sub 调 _importable_defs(sub)，
            # 而 FunctionDef 自身也有 .body —— 于是它被当成容器往下钻进函数体，
            # 函数名反而一个都没记下，`if True: def dedup_key(...)` 照样放行（实测）。
            for stmts in _branch_bodies(n):
                _importable_defs(ast.Module(body=list(stmts), type_ignores=[]), out, rel, in_class)


def _module_level_defs():
    out = defaultdict(list)
    # 【递归 glob】只扫一层的话，新建一个子包就能把重复藏起来（实测）。
    for path in sorted(_ROOT.rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        if any(rel.startswith(d) or f"/{d}/" in f"/{rel}" for d in _SKIP_DIRS):
            continue
        _importable_defs(ast.parse(path.read_text(encoding="utf8")), out, rel)
    return out


def test_every_cross_file_duplicate_is_registered():
    """跨文件的模块级重名必须登记在 _INTENTIONAL 里。"""
    # 【类方法不算】sources/ 里的 _hash_of / _url_of 是 RssSource 子类的覆写点，
    # 那是文档化的多态设计（base 里 raise NotImplementedError，两个源各给一份），不是重复实现。
    # 但它们仍会被下面 test_key_helpers_have_exactly_one_definition 看见 ——
    # "藏在类里"恰恰是给公共判据偷加第二份实现的方式。
    plain = lambda v: {x for x in v if "::类内" not in x}      # noqa: E731
    defs = _module_level_defs()
    dupes = {k: sorted(plain(v)) for k, v in defs.items() if len(plain(v)) > 1}
    unregistered = {k: v for k, v in dupes.items() if k not in _INTENTIONAL}
    assert not unregistered, (
        "这些函数在多个文件里各有一份模块级定义，却没登记为『有意重名』。\n"
        "要么合并成一份，要么加进 tests/test_single_definition.py 的 _INTENTIONAL 并写清理由：\n  "
        + "\n  ".join(f"{k}: {v}" for k, v in unregistered.items()))


def test_registry_has_no_stale_entries():
    """反向：白名单里不该留着已经合并掉的条目，否则它会悄悄放行一次真正的重复。"""
    plain = lambda v: {x for x in v if "::类内" not in x}      # noqa: E731
    dupes = {k for k, v in _module_level_defs().items() if len(plain(v)) > 1}
    stale = sorted(set(_INTENTIONAL) - dupes)
    assert not stale, f"这些已经不再重名了，请从 _INTENTIONAL 里删掉：{stale}"


@pytest.mark.parametrize("name", ["parse_bgm_id", "quarter_year", "quarter_of",
                                  "format_quarter", "dedup_key", "alias_key",
                                  "quarter_sort_key", "safe_name", "build_save_path"])
def test_key_helpers_have_exactly_one_definition(name):
    """这几个是「改判据」时最可能被动到的公共判据，必须恒为一份实现。

    与上面两条的分工：那两条管的是『重名要登记』，这条管的是『这几个名字连登记都不许』。
    """
    where = _module_level_defs().get(name, [])
    # 这里连"藏在类里/写成 lambda/藏在条件分支里"的第二份也算 —— 它们同样是改判据时会被漏掉的拷贝
    assert len(set(where)) == 1, f"{name} 有 {len(set(where))} 份定义：{sorted(set(where))}"
