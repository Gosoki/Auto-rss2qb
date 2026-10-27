"""qB 的 IP 过滤（core/ipfilter）：定期生成「屏蔽某国 IP」的 .p2p，包成只有 HTTP 下载源的私有种子让 qB 自己下。"""
import asyncio
import gzip
import hashlib
import ipaddress
import json
from datetime import datetime, timedelta

import pytest

from core import ipfilter as F


# ── 生成 ──────────────────────────────────────────────

_RIR = (b"apnic|JP|ipv4|1.0.16.0|4096|20110412|allocated\n"
        b"apnic|JP|ipv4|1.0.33.0|256|20110412|allocated\n"        # 与上一段隔 256 个地址
        b"apnic|JP|ipv6|2001:200::|32|19990813|allocated\n"
        b"apnic|KR|ipv4|1.11.0.0|65536|20100126|allocated\n"
        b"apnic|JP|ipv4|9.9.9.0|256|20110412|reserved\n")
_AWS = json.dumps({"prefixes": [{"ip_prefix": "3.112.0.0/14", "region": "ap-northeast-1"},
                                {"ip_prefix": "3.34.0.0/15", "region": "ap-northeast-2"}],
                   "ipv6_prefixes": [{"ipv6_prefix": "2406:da14::/32", "region": "ap-northeast-1"}]}).encode()
_AZURE = json.dumps({"values": [{"properties": {"region": "japaneast", "addressPrefixes": ["20.18.0.0/16"]}},
                                {"properties": {"region": "koreacentral", "addressPrefixes": ["20.41.0.0/16"]}},
                                {"properties": {"region": "", "addressPrefixes": ["13.0.0.0/8"]}}]}).encode()
_ORACLE = json.dumps({"regions": [{"region": "ap-tokyo-1", "cidrs": [{"cidr": "129.146.0.0/21"}]},
                                  {"region": "us-ashburn-1", "cidrs": [{"cidr": "129.213.0.0/16"}]}]}).encode()


def _v4(rs):
    return sorted((str(ipaddress.IPv4Address(s)), str(ipaddress.IPv4Address(e))) for v, s, e in rs if v == 4)


def test_parsers_pick_only_the_wanted_country():
    jp = {"JP"}
    assert _v4(F._p_rir(_RIR, jp)["JP"]) == [("1.0.16.0", "1.0.31.255"), ("1.0.33.0", "1.0.33.255")], \
        "reserved、别国的不算"
    assert _v4(F._p_range_csv(b"1.0.16.0,1.0.31.255,JP\n1.0.32.0,1.0.63.255,CN\n2001:200::,2001:200:ffff:ffff:ffff:ffff:ffff:ffff,JP\n",
                              jp)["JP"]) == [("1.0.16.0", "1.0.31.255")]
    assert _v4(F._p_geofeed(b"# c\n43.224.32.0/22,JP,JP-13,Shinagawa-ku,\n45.32.0.0/21,US,US-NJ,,\n", jp)["JP"]) == \
        [("43.224.32.0", "43.224.35.255")]
    assert _v4(F._p_cidrs(b"# comment\n27.0.0.0/22\nnot-a-cidr\n", "JP")["JP"]) == [("27.0.0.0", "27.0.3.255")]
    assert _v4(F._p_cloud("aws", _AWS, jp)["JP"]) == [("3.112.0.0", "3.115.255.255")], "首尔的不算"
    assert _v4(F._p_cloud("azure", _AZURE, jp)["JP"]) == [("20.18.0.0", "20.18.255.255")], "不分区域的大段不算"
    assert _v4(F._p_cloud("oracle", _ORACLE, jp)["JP"]) == [("129.146.0.0", "129.146.7.255")]
    assert _v4(F._p_cloud("azure", _AZURE, {"US"})["US"]) == []
    dat = gzip.compress(b"001.000.192.000 - 001.000.255.255 , 000 , x\n")
    assert _v4(F._p_dat(dat)["P2P"]) == [("1.0.192.0", "1.0.255.255")], "带前导零的 eMule 地址"


def test_render_fills_country_gaps_but_not_antip2p():
    nets = {"JP": F._p_rir(_RIR, {"JP"})["JP"] + F._p_cloud("aws", _AWS, {"JP"})["JP"],
            "P2P": [(4, int(ipaddress.IPv4Address("5.0.0.0")), int(ipaddress.IPv4Address("5.0.0.255"))),
                    (4, int(ipaddress.IPv4Address("5.0.1.10")), int(ipaddress.IPv4Address("5.0.1.20")))]}
    text, n4, n6, per = F.render(nets, ["JP"], 4096)
    lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
    assert "JP:1.0.16.0-1.0.33.255" in lines, "隔 256 个地址的两段被填平"
    assert "P2P:5.0.0.0-5.0.0.255" in lines and "P2P:5.0.1.10-5.0.1.20" in lines, "反 P2P 名单不填缝"
    assert lines[-1].startswith("JP:2001:200::") or lines[-1].startswith("JP:2406:"), "IPv6 排在最后"
    assert all(":" not in ln.split(":", 1)[1] for ln in lines[:n4]), "v4 全在前面"
    assert per == {"JP": 2, "P2P": 2} and n6 == 2
    text0, *_ = F.render(nets, ["JP"], 0)
    assert "JP:1.0.16.0-1.0.31.255" in text0 and "JP:1.0.33.0-1.0.33.255" in text0, "填缝 0 就不填"


def test_countries_setting_is_normalised(cfg):
    cfg(IPFILTER_COUNTRIES=["kr", " JP", "jp", "xyz", ""])
    assert F.countries() == ["KR", "JP"]
    cfg(IPFILTER_COUNTRIES=[])
    assert F.countries() == ["JP"], "一个都没有就按日本"


def test_generate_unions_every_source_and_reports_failures(cfg, monkeypatch):
    """每个源都要问到；按国家的表 404 算「没有数据」，共用的源取不到算失败；Azure 先读下载页再取 JSON。"""
    cfg(IPFILTER_GAP_FILL=4096)
    asked = []
    az_json = "https://download.microsoft.com/download/x/ServiceTags_Public_20261019.json"

    async def fake_get(client, url):
        asked.append(url)
        if url == F._CLOUD["azure"]:
            return 200, f'<a href="{az_json}">'.encode()
        if url == az_json:
            return 200, _AZURE
        if url == F._RIRS["apnic"]:
            return 200, _RIR
        if url == F._CLOUD["aws"]:
            return 200, _AWS
        if url == F._CLOUD["oracle"]:
            return 200, _ORACLE
        if url == F._CLOUD["gcp"]:
            return 200, b'{"prefixes": []}'
        if "ipdeny.com/ipv6" in url:
            return 404, b""
        if "lacnic" in url:
            return 503, b""
        if "dbip-country-ipv4" in url:
            return 200, b"60.0.0.0,60.0.0.255,JP\n"
        return 200, b""
    monkeypatch.setattr(F, "_get", fake_get)
    res = asyncio.run(F.generate(["JP"], antip2p=False))
    assert res["failed"] == ["lacnic（HTTP 503）"], "ipdeny 没有 v6 表不算失败，RIR 取不到算"
    assert az_json in asked and F._ANTIP2P not in asked
    assert len(asked) == len(F._sources(["JP"], False)) + 1
    for want in ("JP:60.0.0.0-60.0.0.255", "JP:20.18.0.0-20.18.255.255", "JP:129.146.0.0-129.146.7.255",
                 "JP:3.112.0.0-3.115.255.255"):
        assert want in res["text"]
    assert res["counts"]["azure:JP"] == 1 and res["per"]["JP"] == 5


# ── 交给 qB ──────────────────────────────────────────────

@pytest.mark.parametrize("down,want", [
    ("E:\\Anime\\AutoRSS", "E:\\Anime\\AutoRSS\\IPBlocker"),     # 生产：和季度文件夹 26C、26D 并列
    ("E:\\Anime\\AutoRSS\\", "E:\\Anime\\AutoRSS\\IPBlocker"),
    ("/mnt/media/dl", "/mnt/media/dl/IPBlocker"),
    ("", ""),
])
def test_default_dir_is_inside_the_working_dir(cfg, down, want):
    cfg(DOWN_PATH=down, ANIME_DOWN_PATH="番剧", IPFILTER_DIR="")
    assert F.target_dir() == want, "固定在工作目录下，不跟番剧目录走"


def test_custom_dir_wins(cfg):
    cfg(DOWN_PATH="E:\\Anime\\AutoRSS", IPFILTER_DIR="D:\\Filters")
    assert F.qb_file_path() == "D:\\Filters\\ipfilter.p2p"


def test_build_torrent_is_private_web_seed_only():
    url = "http://10.0.0.230:2333/api/ipfilter/abc/ipfilter.p2p"
    data = b"JP:1.0.16.0-1.0.31.255\n" * 30000      # 两个多 piece
    t, h = F.build_torrent(data, "ipfilter.p2p", url)
    assert b"8:url-list%d:%s" % (len(url), url.encode()) in t
    assert b"7:privatei1e" in t and b"announce" not in t
    pieces = b"".join(hashlib.sha1(data[i:i + F._PIECE]).digest() for i in range(0, len(data), F._PIECE))
    assert len(pieces) == 20 * -(-len(data) // F._PIECE)
    info = F._bencode({"name": "ipfilter.p2p", "length": len(data), "piece length": F._PIECE,
                       "pieces": pieces, "private": 1})
    assert info in t and h == hashlib.sha1(info).hexdigest()


class _FakeQB:
    def __init__(self, qb_path="E:/Anime/AutoRSS/IPBlocker/ipfilter.p2p", enabled=True):
        self.calls, self.progress = [], 0.0
        self.prefs = {"ip_filter_enabled": enabled, "ip_filter_path": qb_path}

    async def add_torrent(self, torrent, save_path, category, tags):
        self.calls.append(("add", save_path, tags))
        return True

    async def delete(self, hashes, delete_files=True):
        self.calls.append(("delete", tuple(hashes), delete_files))
        return True

    async def torrents_info(self, hashes):
        return {h: {"progress": self.progress} for h in hashes}

    async def preferences(self):
        return dict(self.prefs)

    async def set_preferences(self, prefs):
        self.calls.append(("prefs", prefs))
        return True


def _setup(cfg, monkeypatch, tmp_path, qb, failed=(), v4=4):
    from core import engine
    cfg(QB_ENABLED=True, DOWN_PATH="E:\\Anime\\AutoRSS", ANIME_DOWN_PATH="", IPFILTER_DIR="", IPFILTER_STATE="",
        IPFILTER_SEED_URL="http://10.0.0.230:2333", IPFILTER_DAYS=30, IPFILTER_COUNTRIES=["JP"])
    monkeypatch.setattr(F, "_DIR", tmp_path)
    monkeypatch.setattr(engine, "qb", qb)

    async def fake_generate(ccs=None, gap=None, antip2p=None):
        return {"text": "# x\nJP:1.0.16.0-1.0.31.255\nP2P:5.0.0.0-5.0.0.255\n", "v4": v4 + 1, "v6": 0,
                "per": {"JP": v4, "P2P": 1}, "counts": {}, "failed": list(failed), "countries": ["JP"]}
    monkeypatch.setattr(F, "generate", fake_generate)


def test_update_then_finish_reloads_qb_filter(testdb, cfg, monkeypatch, tmp_path):
    qb = _FakeQB()
    _setup(cfg, monkeypatch, tmp_path, qb)
    assert asyncio.run(F.update()).startswith("已交给 qB")
    assert qb.calls == [("add", "E:\\Anime\\AutoRSS\\IPBlocker", "AutoRSS-IPFilter")]
    st = F.run_state()
    digest, name = st["serving"].split("/")
    assert name == "ipfilter.p2p" and "JP:1.0.16.0-1.0.31.255" in F.serving_file(digest, name).read_text()
    assert F.serving_file("0" * 16, name) is None, "旧种子的下载源随之失效"
    assert not F.due(), "还有没下完的那份时不再开新的一轮"

    assert asyncio.run(F.finish_pending()) is None, "没下完：什么都不做"
    qb.progress = 1.0
    res = asyncio.run(F.finish_pending())
    assert "qB 已重新加载" in res
    assert qb.calls[1:] == [("delete", (st["pending"],), False),
                            ("prefs", {"ip_filter_enabled": False}), ("prefs", {"ip_filter_enabled": True})]
    assert not F.run_state()["pending"]


@pytest.mark.parametrize("qb_path,enabled,old", [("C:/other.p2p", True, "C:/other.p2p"), ("", False, "")])
def test_finish_points_qb_filter_at_the_file(testdb, cfg, monkeypatch, tmp_path, qb_path, enabled, old):
    """qB 还没指向这个文件（指着别的、或 IP 过滤关着）：替用户打开并指过来，原来指着别的就写明。"""
    qb = _FakeQB(qb_path=qb_path, enabled=enabled)
    _setup(cfg, monkeypatch, tmp_path, qb)
    asyncio.run(F.update())
    qb.progress = 1.0
    res = asyncio.run(F.finish_pending())
    assert qb.calls[-1] == ("prefs", {"ip_filter_enabled": True,
                                      "ip_filter_path": "E:/Anime/AutoRSS/IPBlocker/ipfilter.p2p"})
    assert "已在 qB 里打开 IP 过滤" in res and (f"原来指向 {old}" in res) == bool(old)


def test_finish_when_qb_settings_unreadable(testdb, cfg, monkeypatch, tmp_path):
    qb = _FakeQB()
    _setup(cfg, monkeypatch, tmp_path, qb)

    async def no_prefs():
        return None
    qb.preferences = no_prefs
    asyncio.run(F.update())
    qb.progress = 1.0
    assert "E:\\Anime\\AutoRSS\\IPBlocker\\ipfilter.p2p" in asyncio.run(F.finish_pending())
    assert not any(c[0] == "prefs" for c in qb.calls)


@pytest.mark.parametrize("failed,v4,want", [(["lacnic（HTTP 503）"], 4, "没取到"), ((), 0, "国家代码")])
def test_incomplete_rules_never_reach_qb(testdb, cfg, monkeypatch, tmp_path, failed, v4, want):
    """有源取不到 / 一条都没生成：不换文件、不碰 qB，6 小时后再试。"""
    qb = _FakeQB()
    _setup(cfg, monkeypatch, tmp_path, qb, failed=failed, v4=v4)
    assert want in asyncio.run(F.update())
    assert qb.calls == [] and not (tmp_path / F.NAME).exists()
    now = datetime.now()
    assert not F.due(now + timedelta(hours=5)) and F.due(now + timedelta(hours=7))


def test_due(testdb, cfg):
    cfg(QB_ENABLED=True, IPFILTER_DAYS=30, IPFILTER_STATE="")
    assert F.due()
    now = datetime.now()
    F._save(last=(now - timedelta(days=29)).isoformat())
    assert not F.due(now)
    assert F.due(now + timedelta(days=2))
    cfg(IPFILTER_DAYS=0)
    assert not F.due(now + timedelta(days=60)), "0=关"


def test_route_serves_only_current_file(testdb, cfg, monkeypatch, tmp_path):
    from fastapi import HTTPException
    from pages import api
    cfg(IPFILTER_STATE="")
    monkeypatch.setattr(F, "_DIR", tmp_path)
    (tmp_path / "ipfilter.p2p").write_text("JP:1.0.16.0-1.0.31.255\n")
    F._save(serving="abcd/ipfilter.p2p")
    resp = asyncio.run(api.ipfilter_file("abcd", "ipfilter.p2p"))
    assert str(resp.path).endswith("ipfilter.p2p")
    for d, n in (("0000", "ipfilter.p2p"), ("abcd", "../autorss.db")):
        with pytest.raises(HTTPException):
            asyncio.run(api.ipfilter_file(d, n))
