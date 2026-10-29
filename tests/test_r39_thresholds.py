"""r39：业务阈值进设置页（D12/A15）。默认值逐项等于原来的常量，行为不变。"""
import re
from pathlib import Path

import pytest

import config

_SETTINGS_SRC = (Path(__file__).resolve().parent.parent / "pages" / "settings.py").read_text(encoding="utf-8")


def test_ranged_keys_are_int_numeric_and_rendered():
    """_RANGE 里的每个键：_SPEC 是 int、默认值落在范围内、进了 _NUMERIC、设置页有对应的数字框。"""
    from pages.settings import _NUMERIC
    for k, (lo, hi) in config._RANGE.items():
        kind, default = config._SPEC[k]
        assert kind is int, k
        assert lo <= default <= hi, k
        assert k in _NUMERIC, k
        assert f'_num("{k}"' in _SETTINGS_SRC, k


def test_every_number_box_is_registered_numeric():
    """ui.number 回的是 float：漏登 _NUMERIC 的整数键会被存成 '26.0'，_coerce 读回来变 0、设置静默失效。
    设置页上所有数字框都必须登记（WEB_PORT 走 .env，也在表里）。"""
    from pages.settings import _NUMERIC
    keys = set(re.findall(r'_num\("([A-Z0-9_]+)"', _SETTINGS_SRC))
    keys |= set(re.findall(r'_num_into\(f, "([A-Z0-9_]+)"', _SETTINGS_SRC))
    assert keys, "没扫到数字框，正则过期了"
    for k in keys:
        assert k in _NUMERIC, k
        assert k == "WEB_PORT" or config._SPEC[k][0] is int, k


def test_out_of_range_values_are_clamped_on_read(cfg):
    cfg(ANIME_IDLE_WINDOW_X=1, ALERT_REPEAT_HOURS=99999, ANIME_BACKFILL_EXTRA_EPS=-3)
    assert config.ANIME_IDLE_WINDOW_X == 2, "取 1 时提醒过一次的番再次断更也永远不提醒，必须钳到 2"
    assert config.ALERT_REPEAT_HOURS == 720
    assert config.ANIME_BACKFILL_EXTRA_EPS == 0
    assert config.raw("ANIME_IDLE_WINDOW_X") == 1, "raw() 仍返回存储值"


def test_retry_backoff_default_matches_the_old_table():
    from datetime import datetime
    from core import engine
    assert engine.retry_backoff_min() == (1, 5, 30, 180, 720, 1440)
    for n, minutes in enumerate((1, 5, 30, 180, 720, 1440)):
        delta = (engine.next_retry_at(n) - datetime.now()).total_seconds() / 60
        assert abs(delta - minutes) < 0.1
    assert engine.next_retry_at(6) is None


def test_retry_backoff_follows_the_setting(cfg):
    from core import engine
    cfg(ANIME_RETRY_BACKOFF_MIN=["10", "20"])
    assert engine.retry_backoff_min() == (10, 20)
    assert engine.next_retry_at(1) is not None
    assert engine.next_retry_at(2) is None


@pytest.mark.parametrize("bad", [[], ["0"], ["x"], [str(i) for i in range(1, 23)], ["99999"]])
def test_broken_retry_backoff_falls_back_to_default(cfg, bad):
    """库里的值坏了绝不返回空表：空表等于第一次暂时性失败就落 error，断一下网就漏集。"""
    from core import engine
    cfg(ANIME_RETRY_BACKOFF_MIN=bad)
    assert engine.retry_backoff_min() == (1, 5, 30, 180, 720, 1440)


@pytest.mark.parametrize("raw, want", [
    ("1, 5,30", (1, 5, 30)), ("1，5、30", (1, 5, 30)), (["2", "3"], (2, 3)),
    ("", None), ("-1", None), ("1 5", None), ("0,5", None),
])
def test_parse_retry_backoff(raw, want):
    from core import engine
    assert engine.parse_retry_backoff(raw) == want


def test_backfill_prefer_empty_means_no_preference():
    """留空＝不偏好：kw_match('', x) 恒真，排序第二键全相同、退回按入库顺序。"""
    from core.anime import _kw_match
    assert _kw_match("", "[组] 番 - 01 [720p]") and _kw_match("", None)
    assert _kw_match("1080", "[组] 番 - 01 [1080P]") and not _kw_match("1080", "[组] 番 - 01 [720p]")


def test_lock_sync_window_follows_the_setting(clean_tables, cfg):
    """锁定组季内 1~12、ANi 绝对 13~24，两边每集相隔 3 天：默认 2 天窗口不算同步（报），调到 3 天就豁免。"""
    from core import anime as A
    from tests.test_r38_fixes import _lock_setup
    rows = [("ANi", 12 + e, 7 * e, "pending") for e in range(1, 13)]
    rows += [("喵萌奶茶屋", e, 7 * e + 3, "sent") for e in range(1, 13)]
    a = _lock_setup(clean_tables, rows)
    cfg(ANIME_LOCK_LAG_DAYS=7, ANIME_LOCK_SYNC_DAYS=2)
    assert A.locked_out_episodes([a]), "默认窗口：相隔 3 天不算同步"
    cfg(ANIME_LOCK_SYNC_DAYS=3)
    assert A.locked_out_episodes([a]) == {}
