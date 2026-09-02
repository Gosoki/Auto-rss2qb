"""(R32) Mikan 季度页解析不许有二次方回溯 —— 它跑在事件循环上。

原来 `_ROW_LABEL_RE = id="data-row-\\d+"[^>]*>\\s*(.*?)\\s*</div>` 带 re.S：
块里没有 `</div>` 时，**每一个** `id="data-row-` 起点都要让 `.*?` 一路扫到块尾才判失败，
再退回下一个起点重来 —— O(起点数 × 块长度)。实测 2000 起点 / 37 KB → **2.57 秒**，
8000 起点 → 24.4 秒；而页面大小仍在 `fetch.get_text` 的 16 MB 上限之内，
两道现有的闸（字节上限、`warn_if_not_a_feed`）都不覆盖这种形状。

`_parse_movie_bucket` 是**同步函数、跑在事件循环上**：冻住期间 Web UI 不响应、
qB 同步不跑、交付协程不动、看守协程探不了库。
"""
import time

import pytest

from sources.mikan import _parse_movie_bucket, _row_label


@pytest.mark.parametrize("html_in,want", [
    ('<div id="data-row-1" class="x">  剧场版 </div>', "剧场版"),
    ('<div id="data-row-2">OVA</div>', "OVA"),
    ('<div id="data-row-3" data-x="a">\n  スペシャル\n</div>', "スペシャル"),
    # 【内层标签那种必须原样保留】把 `(.*?)` 收窄成 `([^<]*)` 会让它变成取不到 ——
    # 而那正是"顺手优化正则"最容易犯的错：性能修好了、行为悄悄变了。
    ('<div id="data-row-5"><span>剧场版</span></div>', "<span>剧场版</span>"),
    ('<div id="data-row-6">no close', ""),
    ('<div>没有 data-row</div>', ""),
])
def test_row_label_behaviour_is_unchanged(html_in, want):
    assert _row_label(html_in) == want


def test_a_block_with_thousands_of_starts_does_not_blow_up():
    """判据是**相对倍数**，不是硬性毫秒预算。

    硬预算在满负载的全量跑里会假红（R26 那条正则用例栽过一次）。
    这里比的是"起点数翻 4 倍，耗时不该翻 10 倍以上" —— 二次方时它是 16 倍起步
    （实测旧实现 1000→4000 约 0.6s→7s，十倍以上），线性时约 4 倍。
    """
    # 【规模要克制】跑一次就够：变异回二次方之后，2000 起点单次就是 2.5 秒 ——
    # 重复三次会让"守卫变红"变成"守卫跑了一分半"，那种红没人愿意等。
    def _t(n):
        blk = ('<div class="sk-bangumi" data-dayofweek="9">'
               + '<div id="data-row-1" x>' * n + "A" * 2000)
        a = time.perf_counter()
        _parse_movie_bucket(blk)
        return time.perf_counter() - a

    small, big = _t(1000), _t(4000)
    assert big < max(small * 10, 0.02), (
        f"起点数 ×4，耗时 ×{big / small if small else 0:.1f} —— 像是回到了二次方回溯"
        f"（{small * 1000:.2f}ms → {big * 1000:.2f}ms）")
    # 绝对值兜一道：二次方时这一档是秒级，线性时是毫秒级，中间隔着三个数量级
    assert big < 0.3, f"4000 个起点花了 {big * 1000:.0f}ms —— 事件循环会被冻住"


def test_a_well_formed_bucket_still_parses():
    """反向：正常页面照常解析出剧场版桶。"""
    htm = (
        '<div class="sk-bangumi" data-dayofweek="1"><div id="data-row-1">周一</div>'
        '<a href="/Home/Bangumi/111" title="某周更番"></a></div>'
        '<div class="sk-bangumi" data-dayofweek="9"><div id="data-row-2">剧场版</div>'
        '<a href="/Home/Bangumi/222" title="某剧场版"></a>'
        '<a href="/Home/Bangumi/333" title="某OVA"></a></div>'
    )
    got = _parse_movie_bucket(htm)
    assert [(m, n) for m, n, _ in got] == [("222", "某剧场版"), ("333", "某OVA")], got
    assert all(lbl == "剧场版" for *_, lbl in got)
