"""(R17) `await enrich.resolve` 前后必须对 bangumi_id 做 compare-and-set。

enrich.resolve 的整体预算是 120 秒（services/enrich._RESOLVE_BUDGET），是全项目最长的
await 窗口之一。窗口里用户完全可能在同一个弹窗里点『绑定 bgm』填上正确的 subject，
而 `engine.apply_bgm_meta` 对 bangumi_id 是【无条件覆写】，紧接着的身份守卫
（_merge_anime / _merge_movie）最后一步是 `s.delete(loser)` —— 用户的决定被静默盖掉，
另一条记录（可能是已经下完的那部）连同它的名字/版本一起消失，UI 还弹绿色的『识别成功 ✓』。

**这条用例存在的直接原因**：番剧侧 enrich_anime 修好之后，剧场版侧 enrich_movie 漏了 ——
同一件事有两处、只改了一处，本项目最常见的缺陷形状。
"""
import ast
import asyncio
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent

# 不需要 CAS 的例外，**必须写清理由**
_EXEMPT = {
    "_resolve_anime": (
        "它只对【新建的】Anime 调 _apply_bgm；bgm_id 命中已有番时走的是复用分支、"
        "不碰那条番的 bangumi_id，所以没有"
        "『用户在 await 期间改了绑定』这个可覆盖的对象"),
}


def _funcs_that_resolve_then_write():
    out = []
    for path in sorted(_ROOT.glob("core/*.py")) + sorted(_ROOT.glob("services/*.py")):
        src = path.read_text(encoding="utf8")
        tree = ast.parse(src)
        for n in ast.walk(tree):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.get_source_segment(src, n) or ""
            if "enrich.resolve" in body and ("apply_bgm_meta" in body or "_apply_bgm" in body):
                out.append((path.relative_to(_ROOT).as_posix(), n.lineno, n.name, body))
    return out


def test_every_resolve_then_write_has_a_compare_and_set():
    """凡是 await enrich.resolve 之后会写 bangumi_id 的函数，都要有这道闸（或登记豁免理由）。"""
    found = _funcs_that_resolve_then_write()
    assert found, "扫描逻辑失效了：一个这样的函数都没找到"
    missing = []
    for rel, line, name, body in found:
        if name in _EXEMPT:
            continue
        # 判据：await 之前取过快照、之后与之比较
        if "_before" not in body or "!=" not in body:
            missing.append(f"{rel}:{line} {name}")
    assert not missing, (
        "这些函数在 120 秒的 await 窗口后无条件覆写 bangumi_id，会吃掉用户的手动绑定：\n  "
        + "\n  ".join(missing))


def test_the_exemption_list_is_not_stale():
    """反向：豁免名单里的函数必须还存在，否则它会悄悄放行一个真缺口。"""
    names = {name for _, _, name, _ in _funcs_that_resolve_then_write()}
    stale = sorted(set(_EXEMPT) - names)
    assert not stale, f"这些已经不再匹配扫描条件了，请从 _EXEMPT 删掉：{stale}"


# (r39 M2) 剧场版侧原有两条行为用例（await 期间人工绑定胜出 / 无人改动时照旧覆写）随 enrich_movie 一起撤了：
# 『重新识别』改成 core.movies.suggest_movie_bgm 只出候选、不写库，写库只在带回显的 bind_movie_bgm 上发生，
# 这条路上已没有"await 之后覆写 bangumi_id"的对象。它不写库这件事由
# tests/test_wrong_season_match.py::test_no_identification_path_calls_a_merge 与 tests/test_r39_movies.py 钉住。
