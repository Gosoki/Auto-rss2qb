"""通知的前景色注入。

这一条单独立用例，是因为它错过一次：写成 text_color（蛇形）时看起来改了、实际什么都没发生——
NiceGUI 只把 close_button/multi_line 转驼峰，其余原样下发，而 Quasar 认的是 textColor。
这类"静默无效"的改动没有用例根本发现不了。
"""
import pytest

from pages import layout


@pytest.fixture
def captured(monkeypatch):
    got = []
    monkeypatch.setattr(layout, "_ui_notify", lambda msg, **kw: got.append((msg, kw)))
    return got


@pytest.mark.parametrize("t", ["positive", "negative", "warning", "info"])
def test_light_backgrounds_get_dark_text(captured, t):
    layout._slot_safe_notify("x", type=t)
    assert captured[0][1].get("textColor") == "dark", "必须是驼峰 textColor，蛇形会被 Quasar 忽略"


def test_default_toast_keeps_white_text(captured):
    """不带 type 时底色是 Quasar 默认的 #323232 深灰——深色前景只有 1.64:1，绝不能改。"""
    layout._slot_safe_notify("x")
    assert "textColor" not in captured[0][1]


def test_explicit_color_also_gets_dark_text(captured):
    layout._slot_safe_notify("x", color="amber")
    assert captured[0][1].get("textColor") == "dark"


def test_caller_override_wins(captured):
    layout._slot_safe_notify("x", type="positive", textColor="white")
    assert captured[0][1]["textColor"] == "white"


def test_snake_case_is_not_used_anywhere(captured):
    """防回归：谁要是又写成 text_color，这条会红。"""
    layout._slot_safe_notify("x", type="warning")
    assert "text_color" not in captured[0][1]
