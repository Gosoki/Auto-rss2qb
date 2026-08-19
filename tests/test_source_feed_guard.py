"""源组 feed 的空值防线。

空 feed 在 nyaa 那边拼出来的是【不带用户名的全站 RSS】——几十条/分钟、什么都有。
而源组的默认策略是 auto：自动建番、自动确认、自动下载。
一个不小心存进去的空 feed 就能让 qB 开始下载整个 nyaa。
"""
import pytest

from sources.nyaa import nyaa_feed_url


@pytest.mark.parametrize("bad", ["", "   ", "\t", "\n  \n", None])
def test_empty_feed_is_refused(bad):
    """(R5) UI 那道闸只是第一道——API、直接改库、旧数据都绕得过它，服务端必须自己挡住。"""
    with pytest.raises(ValueError, match="不能为空"):
        nyaa_feed_url(bad)


def test_username_and_url_still_work():
    assert nyaa_feed_url("ANiTorrent").endswith("u=ANiTorrent&c=1_0")
    assert nyaa_feed_url("  ANiTorrent  ").endswith("u=ANiTorrent&c=1_0")
    assert nyaa_feed_url("https://nyaa.si/?page=rss&u=x") == "https://nyaa.si/?page=rss&u=x"


def test_the_generated_url_is_never_a_firehose():
    """防回归：拼出来的 URL 必须带一个非空的 u= 参数。"""
    url = nyaa_feed_url("SomeUser")
    assert "u=SomeUser" in url and "u=&" not in url
