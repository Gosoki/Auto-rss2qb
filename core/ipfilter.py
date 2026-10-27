"""qB 的 IP 过滤：每隔一段时间重新生成「屏蔽某国 IP」的 .p2p 规则（默认日本），交给 qB。

━━ 生成（原 tools/make_jp_ipfilter.py 的逻辑，工具脚本现在只是这里的命令行入口）━━
【设计前提】宁可误杀，绝不放过。所以：
1. 取【尽可能多的来源的并集】，而不是信任任何一个。每一类来源漏的东西不一样：
   · RIR 注册数据（五大 RIR + ipdeny/ipverse 两个整理版）：按【登记国】，漏掉登记在别国、人在本国的；
   · 地理定位库（GeoLite2 ×2、DB-IP、sapics 的三个合成库）：按【实际位置】，漏掉刚分配、还没被探测到的；
   · 按 ASN 归属（iptoasn、asn-country）：本国运营商用【外国登记的地址】宣告出去的段，前两类都可能漏；
   · 云厂商与 VPS 的官方区段（AWS/GCP/Azure/Oracle 的本国机房，Linode/Vultr/DigitalOcean 的地理表）：
     监控/取证节点最爱开在这上面，地址往往登记在美国，不能只指望地理库标对。
   云厂商区域对照表（_CLOUD_REGIONS）里没有的国家，只靠前三类与 VPS 地理表。
2. 相邻区间之间【小于 IPFILTER_GAP_FILL 个地址的缝】直接填平（默认 4096 = 一个 /20）。
   同一个 /16 常常切给同一国的多家 ISP，缝里多半还是该国；填平既减少行数，
   也顺手盖住"所有源都漏登记"的空档。这是明确的「宁可误杀」取舍。
3. 可选再并上一份通用的反 P2P 名单（IPFILTER_ANTIP2P，DavidMoore/ipfilter，"恶意与侵犯隐私的节点"）：
   不分国家，挡的是在境外开机器的监控方。条数多（约 41 万），误杀也多，所以给开关。
4. 任何一个源取不到就【不更新】（命令行可 --force）。少一个源＝少一片覆盖，
   而少的那片你不会知道是哪片——静默换上一个更薄的文件才是真正的危险。旧文件照用。
【为什么是 .p2p】qB 三种格式里只有 .p2p 既是区间制（行数最少）又带描述字段，
且能装下 IPv6。IPv6 行统一排在文件末尾：万一哪个 qB 版本的解析器碰到 v6 行就停，
前面的 IPv4 规则也已经全部生效了。

━━ 交给 qB ━━
qB 的 IP 过滤只认【qB 那台机器上的】本地文件路径，WebAPI 也传不了文件；而本程序与 qB 常常不在同一台
（生产：本程序在 LXC，qB 在 Windows），本程序写不到 qB 的盘。所以把规则文件包成一个【只有 HTTP 下载源】
（web seed，BEP 19）的私有种子交给 qB：qB 像下普通种子一样，从本程序的 /api/ipfilter/<摘要>/<文件名>
把文件下到 IPFILTER_DIR。下完把种子从 qB 里移除（留文件），再把 qB 的 IP 过滤打开并指向这个文件；
已经指向它时就让 qB 重新读一次（关掉再打开 IP 过滤）。每次更新都是同一个文件（换国家也不变）。

只碰运行时配置与 data/ipfilter/，不碰业务库。
"""
import asyncio
import fnmatch
import gzip
import hashlib
import io
import ipaddress
import json
import logging
import re
import socket
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

import config
from core import engine
from services import fetch

log = logging.getLogger("autorss")

_DIR = config.DATA_DIR / "ipfilter"
NAME = "ipfilter.p2p"              # qB 那边的文件名固定，换国家不用去 qB 改路径
_TAG = "AutoRSS-IPFilter"
_PIECE = 256 * 1024
_CAP = 64 * 1024 * 1024            # 单个数据源的上限：最大的 DB-IP IPv6 表约 20MB
_PARALLEL = 3                      # 同时下几个源：全部加起来约 150MB，一次只在内存里留几份原文
_DONE_WAIT = timedelta(hours=1)    # 交给 qB 后这么久还没下完就报错（局域网里几 MB 是秒级的事）
_RETRY = timedelta(hours=6)        # 生成失败（有源取不到）后隔多久再试，别每 10 分钟把几十个源轮一遍

# ① RIR 分配记录：按登记国筛（含在别的 RIR 登记的该国机构）
_RIRS = {
    "apnic": "https://ftp.apnic.net/apnic/stats/apnic/delegated-apnic-latest",
    "arin": "https://ftp.arin.net/pub/stats/arin/delegated-arin-extended-latest",
    "ripencc": "https://ftp.ripe.net/pub/stats/ripencc/delegated-ripencc-latest",
    "lacnic": "https://ftp.lacnic.net/pub/stats/lacnic/delegated-lacnic-latest",
    "afrinic": "https://ftp.afrinic.net/pub/stats/afrinic/delegated-afrinic-latest",
}
# ② 按国家取的 CIDR 表（{cc}=小写国家代码）。404＝这个源没有该国数据，不算失败
_CIDR_LISTS = {
    "ipdeny-v4": "https://www.ipdeny.com/ipblocks/data/aggregated/{cc}-aggregated.zone",
    "ipdeny-v6": "https://www.ipdeny.com/ipv6/ipaddresses/aggregated/{cc}-aggregated.zone",
    "ipverse-v4": "https://raw.githubusercontent.com/ipverse/rir-ip/master/country/{cc}/ipv4-aggregated.txt",
    "ipverse-v6": "https://raw.githubusercontent.com/ipverse/rir-ip/master/country/{cc}/ipv6-aggregated.txt",
    # GeoLite2 派生的地理定位表（v4+v6 混在一个文件，Loyalsoldier 的版本）
    "geolite2": "https://raw.githubusercontent.com/Loyalsoldier/geoip/release/text/{cc}.txt",
}
# ③ 全球"起,止,国家"表（sapics/ip-location-db 每日更新的整理版，jsDelivr 分发）：地理定位与 ASN 归属
_RANGE_DBS = ("dbip-country", "geolite2-country", "geo-whois-asn-country", "geo-asn-country",
              "iptoasn-country", "asn-country")
_RANGE_URL = "https://cdn.jsdelivr.net/npm/@ip-location-db/{db}/{db}-ipv{v}.csv"
# ④ VPS 厂商自己发布的地理表（RFC 8805 geofeed："前缀,国家,…"）
_GEOFEEDS = {"linode": "https://geoip.linode.com/",
             "vultr": "https://geofeed.constant.com/?csv",
             "digitalocean": "https://www.digitalocean.com/geo/google.csv"}
# ⑤ 云厂商的 IP 表（按区域，见 _CLOUD_REGIONS）。Azure 的 JSON 每周换文件名，要先从下载页里找链接
_CLOUD = {"aws": "https://ip-ranges.amazonaws.com/ip-ranges.json",
          "gcp": "https://www.gstatic.com/ipranges/cloud.json",
          "oracle": "https://docs.oracle.com/en-us/iaas/tools/public_ip_ranges.json",
          "azure": "https://www.microsoft.com/en-us/download/details.aspx?id=56519"}
_AZURE_JSON = re.compile(r"https://download\.microsoft\.com/download/[^\"']+ServiceTags_Public_\d+\.json")
# ⑥ 通用反 P2P 名单（eMule .dat，gzip）
_ANTIP2P = "https://github.com/DavidMoore/ipfilter/releases/download/lists/ipfilter.dat.gz"
# 云厂商区域所在国（事实数据，不是可调的规则），支持 * 通配
_CLOUD_REGIONS = {
    "JP": {"aws": {"ap-northeast-1", "ap-northeast-3"}, "gcp": {"asia-northeast1", "asia-northeast2"},
           "azure": {"japaneast", "japanwest"}, "oracle": {"ap-tokyo-1", "ap-osaka-1"}},
    "KR": {"aws": {"ap-northeast-2"}, "gcp": {"asia-northeast3"}, "azure": {"koreacentral", "koreasouth"},
           "oracle": {"ap-seoul-1", "ap-chuncheon-1"}},
    "HK": {"aws": {"ap-east-1"}, "gcp": {"asia-east2"}, "azure": {"eastasia"}},
    "TW": {"gcp": {"asia-east1"}, "azure": {"taiwannorth", "taiwannorthwest"}},
    "SG": {"aws": {"ap-southeast-1"}, "gcp": {"asia-southeast1"}, "azure": {"southeastasia"},
           "oracle": {"ap-singapore-*"}},
    "CN": {"aws": {"cn-north-1", "cn-northwest-1"}},
    "IN": {"aws": {"ap-south-1", "ap-south-2"}, "gcp": {"asia-south1", "asia-south2"},
           "azure": {"centralindia", "southindia", "westindia", "jioindia*", "indiasouthcentral"},
           "oracle": {"ap-mumbai-1", "ap-hyderabad-1", "ap-delhi-1"}},
    "AU": {"aws": {"ap-southeast-2", "ap-southeast-4"}, "gcp": {"australia-southeast*"},
           "azure": {"australia*"}, "oracle": {"ap-sydney-1", "ap-melbourne-1"}},
    "US": {"aws": {"us-*"}, "gcp": {"us-*"}, "azure": {"*us", "*us[0-9]", "*euap", "usstage*"},
           "oracle": {"us-*"}},
    "CA": {"aws": {"ca-*"}, "gcp": {"northamerica-northeast*"}, "azure": {"canada*"}, "oracle": {"ca-*"}},
    "GB": {"aws": {"eu-west-2"}, "gcp": {"europe-west2"}, "azure": {"uk*"}, "oracle": {"uk-*"}},
    "DE": {"aws": {"eu-central-1"}, "gcp": {"europe-west3", "europe-west10"}, "azure": {"germany*"},
           "oracle": {"eu-frankfurt-1"}},
    "FR": {"aws": {"eu-west-3"}, "gcp": {"europe-west9"}, "azure": {"centralfrance", "southfrance"},
           "oracle": {"eu-paris-1", "eu-marseille-1"}},
}
# 设置页多选的常用项（也可以直接输入别的两位代码）
COUNTRY_CHOICES = {"JP": "日本", "CN": "中国大陆", "HK": "香港", "TW": "台湾", "KR": "韩国",
                   "SG": "新加坡", "US": "美国", "CA": "加拿大", "GB": "英国", "DE": "德国",
                   "FR": "法国", "IN": "印度", "AU": "澳大利亚"}
_P2P = "P2P"                       # 反 P2P 名单在 .p2p 里的描述字段（不是国家代码）


def countries() -> list[str]:
    """设置里的国家代码：去重、转大写、只留两位字母；一个都没有就按日本。"""
    out = []
    for c in config.IPFILTER_COUNTRIES or []:
        c = str(c).strip().upper()
        if re.fullmatch(r"[A-Z]{2}", c) and c not in out:
            out.append(c)
    return out or ["JP"]


# ---------------- 解析：每个源 → {国家: [(4|6, 起, 止), …]} ----------------

def _net(net) -> tuple:
    return net.version, int(net.network_address), int(net.broadcast_address)


def _lines(body: bytes):
    """逐行吐出。按字节读、逐行解码：先整体 decode 再套 StringIO 会把一个 20MB 的表按每字 4 字节
    再复制一份（实测峰值 300MB），几个表同时在解析时尤其明显。"""
    for raw in io.BytesIO(body):
        ln = raw.decode("utf-8", "replace").strip()
        if ln and not ln.startswith("#"):
            yield ln


def _p_rir(body: bytes, ccs: set) -> dict:
    """形如 apnic|JP|ipv4|1.0.16.0|4096|20110412|allocated。IPv4 第 5 段是【地址个数】（不一定是 2 的幂），
    IPv6 第 5 段是前缀长度。"""
    out = {c: [] for c in ccs}
    for ln in _lines(body):
        f = ln.split("|")
        if len(f) < 7 or f[1] not in ccs or f[6] in ("reserved", "available"):
            continue
        try:
            if f[2] == "ipv4":
                s = int(ipaddress.IPv4Address(f[3]))
                out[f[1]].append((4, s, s + int(f[4]) - 1))
            elif f[2] == "ipv6":
                out[f[1]].append(_net(ipaddress.ip_network(f"{f[3]}/{f[4]}", strict=False)))
        except ValueError:
            continue
    return out


def _p_cidrs(body: bytes, cc: str) -> dict:
    """一行一个 CIDR（已经是某一国的表）。"""
    out = []
    for ln in _lines(body):
        try:
            out.append(_net(ipaddress.ip_network(ln, strict=False)))
        except ValueError:
            continue
    return {cc: out}


def _p_range_csv(body: bytes, ccs: set) -> dict:
    """形如 1.0.16.0,1.0.31.255,JP（sapics 的表）。先按末尾国家筛，再解析地址，几十万行里只解析需要的。"""
    out = {c: [] for c in ccs}
    for ln in _lines(body):
        cc = ln[ln.rfind(",") + 1:]
        if cc not in ccs:
            continue
        try:
            s, e = ln.split(",")[:2]
            a, b = ipaddress.ip_address(s), ipaddress.ip_address(e)
            out[cc].append((a.version, int(a), int(b)))
        except ValueError:
            continue
    return out


def _p_geofeed(body: bytes, ccs: set) -> dict:
    """RFC 8805：前缀,国家,地区,城市,邮编。"""
    out = {c: [] for c in ccs}
    for ln in _lines(body):
        f = ln.split(",")
        if len(f) >= 2 and f[1].strip().upper() in ccs:
            try:
                out[f[1].strip().upper()].append(_net(ipaddress.ip_network(f[0].strip(), strict=False)))
            except ValueError:
                continue
    return out


def _hit(region: str, pats) -> bool:
    return any(fnmatch.fnmatchcase(region or "", p) for p in pats)


def _cloud_pairs(name: str, body: bytes) -> list[tuple[str, str]]:
    """云厂商 IP 表 → [(区域, CIDR), …]。"""
    d = json.loads(body)
    if name == "aws":
        return ([(p["region"], p["ip_prefix"]) for p in d.get("prefixes", [])]
                + [(p["region"], p["ipv6_prefix"]) for p in d.get("ipv6_prefixes", [])])
    if name == "gcp":
        return [(p.get("scope", ""), p.get("ipv4Prefix") or p.get("ipv6Prefix"))
                for p in d.get("prefixes", []) if p.get("ipv4Prefix") or p.get("ipv6Prefix")]
    if name == "oracle":
        return [(r["region"], c["cidr"]) for r in d.get("regions", []) for c in r.get("cidrs", [])]
    if name == "azure":
        return [(v["properties"].get("region") or "", p) for v in d.get("values", [])
                for p in v["properties"].get("addressPrefixes", [])]
    raise ValueError(name)


def _p_cloud(name: str, body: bytes, ccs: set) -> dict:
    out = {c: [] for c in ccs}
    pats = {c: _CLOUD_REGIONS.get(c, {}).get(name, set()) for c in ccs}
    for region, cidr in _cloud_pairs(name, body):
        for c in ccs:
            if pats[c] and _hit(region, pats[c]):
                try:
                    out[c].append(_net(ipaddress.ip_network(cidr, strict=False)))
                except ValueError:
                    pass
    return out


def _p_dat(body: bytes) -> dict:
    """eMule .dat：001.000.192.000 - 001.000.255.255 , 000 , 说明。地址带前导零，Python 的 ipaddress 不收，先去掉。"""
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    out = []
    for ln in _lines(body):
        try:
            s, e = (int(ipaddress.IPv4Address(".".join(str(int(x)) for x in part.strip().split("."))))
                    for part in ln.split(",")[0].split("-"))
            out.append((4, s, e))
        except ValueError:
            continue
    return {_P2P: out}


def merge(ranges: list, version: int, gap: int) -> list:
    """合成【最少条数】的区间列表：重叠、紧邻、或缝 ≤ gap 的都并成一段（v4 才填缝；v6 地址空间太大，不做）。
    返回 [(起, 止), …]。"""
    rs = sorted((s, e) for v, s, e in ranges if v == version)
    if not rs:
        return []
    gap = max(0, gap) if version == 4 else 0
    out = [list(rs[0])]
    for s, e in rs[1:]:
        if s <= out[-1][1] + 1 + gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def render(nets: dict, ccs: list, gap: int) -> tuple[str, int, int, dict]:
    """{国家/P2P: 区间} → .p2p 文本。返回 (文本, v4 条数, v6 条数, {组: v4 条数})。
    国家各自合并填缝；反 P2P 名单不填缝。"""
    groups = list(ccs) + ([_P2P] if nets.get(_P2P) else [])
    v4 = [(g, merge(nets.get(g, []), 4, 0 if g == _P2P else gap)) for g in groups]
    v6 = [(g, merge(nets.get(g, []), 6, 0)) for g in groups]
    a4, a6 = ipaddress.IPv4Address, ipaddress.IPv6Address
    lines = [f"{g}:{a4(s)}-{a4(e)}" for g, rs in v4 for s, e in rs]
    # v6 排最后：万一解析器碰到 v6 行就罢工，前面的 v4 规则已经全部读进去了
    lines += [f"{g}:{a6(s)}-{a6(e)}" for g, rs in v6 for s, e in rs]
    n4, n6 = sum(len(rs) for _, rs in v4), sum(len(rs) for _, rs in v6)
    head = (f"# 屏蔽 {','.join(ccs)} 的 IP{'（另含通用反 P2P 名单）' if _P2P in groups else ''}"
            f"（qBittorrent .p2p 格式）\n"
            f"# 由 AutoRSS 生成于 {datetime.now():%Y-%m-%d %H:%M}；IPv4 {n4} 条 + IPv6 {n6} 条\n")
    return head + "\n".join(lines) + "\n", n4, n6, {g: len(rs) for g, rs in v4}


def _sources(ccs: list, antip2p: bool) -> list[tuple[str, str, object, bool]]:
    """[(名字, 地址, 解析函数(body)->{组: 区间}, 404 算不算"没有数据")]。"""
    cs = set(ccs)
    out = [(k, u, lambda b, cs=cs: _p_rir(b, cs), False) for k, u in _RIRS.items()]
    out += [(f"{k}:{c}", u.format(cc=c.lower()), lambda b, c=c: _p_cidrs(b, c), True)
            for c in ccs for k, u in _CIDR_LISTS.items()]
    out += [(f"{db}-v{v}", _RANGE_URL.format(db=db, v=v), lambda b, cs=cs: _p_range_csv(b, cs), False)
            for db in _RANGE_DBS for v in (4, 6)]
    out += [(k, u, lambda b, cs=cs: _p_geofeed(b, cs), False) for k, u in _GEOFEEDS.items()]
    out += [(k, u, lambda b, k=k, cs=cs: _p_cloud(k, b, cs), False) for k, u in _CLOUD.items()]
    if antip2p:
        out.append(("antip2p", _ANTIP2P, _p_dat, False))
    return out


async def _get(client, url: str) -> tuple[int, bytes]:
    """取一次，失败再试一次（几十个源里偶尔一个超时不该让整轮作废）。"""
    for attempt in (1, 2):
        try:
            return await fetch.request_capped(client, "GET", url, cap=_CAP, timeout=180)
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(3)
    raise AssertionError("unreachable")


async def generate(ccs: list | None = None, gap: int | None = None, antip2p: bool | None = None) -> dict:
    """取全部数据源并生成规则。返回 {text, v4, v6, counts, failed, countries}。
    failed 非空时 text 照样给（命令行 --force 用）；程序里的自动更新有失败就不换文件（见 update）。"""
    ccs = ccs or countries()
    gap = config.IPFILTER_GAP_FILL if gap is None else gap
    antip2p = config.IPFILTER_ANTIP2P if antip2p is None else antip2p
    srcs = _sources(ccs, antip2p)
    nets: dict = {}
    counts: dict = {}
    failed: list = []
    sem = asyncio.Semaphore(_PARALLEL)
    async with httpx.AsyncClient(**config.http_client_kwargs(
            60, url=[u for _, u, _, _ in srcs] + ["https://download.microsoft.com/"])) as client:
        async def one(name, url, parse, none_ok):
            async with sem:
                try:
                    code, body = await _get(client, url)
                    if code == 200 and name == "azure":          # 下载页 → 本周的 JSON
                        m = _AZURE_JSON.search(body.decode("utf-8", "replace"))
                        if not m:
                            raise ValueError("下载页里没找到 JSON 链接")
                        code, body = await _get(client, m.group(0))
                except Exception as e:
                    failed.append(f"{name}（{type(e).__name__}）")
                    return
                if code == 404 and none_ok:
                    return                                       # 这个源没有该国数据，不算失败
                if code != 200:
                    failed.append(f"{name}（HTTP {code}）")
                    return
                try:
                    got = await asyncio.to_thread(parse, body)
                except Exception as e:
                    failed.append(f"{name}（解析失败：{type(e).__name__}）")
                    return
                del body
                for g, rs in got.items():
                    nets.setdefault(g, []).extend(rs)
                    counts[f"{name}:{g}" if ":" not in name else name] = len(rs)
        await asyncio.gather(*(one(*s) for s in srcs))
    text, n4, n6, per = await asyncio.to_thread(render, nets, ccs, gap)
    return {"text": text, "v4": n4, "v6": n6, "per": per, "counts": counts, "failed": sorted(failed),
            "countries": ccs}


# ---------------- 交给 qB ----------------

def target_dir() -> str:
    """qB 那台机器上放规则文件的目录：IPFILTER_DIR，空＝默认（default_dir）。"""
    return config.IPFILTER_DIR.strip() or default_dir()


def default_dir() -> str:
    """工作目录（DOWN_PATH）下的 IPBlocker；工作目录没填返回空串（没法替用户猜）。"""
    down = config.DOWN_PATH.strip()
    return engine._path_module(down).join(down, "IPBlocker") if down else ""


def qb_file_path() -> str:
    d = target_dir()
    return engine._path_module(d).join(d, NAME) if d else ""


def seed_base() -> str:
    """qB 来下文件时用的本程序地址：IPFILTER_SEED_URL，空＝自动（auto_seed_base）。"""
    return config.IPFILTER_SEED_URL.strip().rstrip("/") or auto_seed_base()


def auto_seed_base() -> str:
    """本机连 qB 时走的那个 IP + Web 端口。"""
    host = urlsplit(config.QB_URL or "").hostname or "127.0.0.1"
    ip = "127.0.0.1"
    if host not in ("127.0.0.1", "localhost", "::1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((host, 9))          # UDP connect 不发包，只让系统选出走哪块网卡
                ip = s.getsockname()[0]
        except OSError:
            pass
    return f"http://{ip}:{config.WEB_PORT}"


def _bencode(v) -> bytes:
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, str):
        v = v.encode()
    if isinstance(v, bytes):
        return b"%d:%s" % (len(v), v)
    if isinstance(v, list):
        return b"l" + b"".join(_bencode(x) for x in v) + b"e"
    if isinstance(v, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, x) for k, x in v.items())
        return b"d" + b"".join(_bencode(k) + _bencode(x) for k, x in items) + b"e"
    raise TypeError(type(v))


def build_torrent(data: bytes, name: str, url: str) -> tuple[bytes, str]:
    """单文件私有种子，唯一的下载源是 url（web seed）。返回 (种子字节, v1 info hash)。
    private=1：不进 DHT/PEX，这个种子只该在 qB 与本程序之间。"""
    pieces = b"".join(hashlib.sha1(data[i:i + _PIECE]).digest() for i in range(0, len(data), _PIECE))
    info = {"name": name, "length": len(data), "piece length": _PIECE, "pieces": pieces, "private": 1}
    t = {"info": info, "url-list": url, "created by": "AutoRSS", "creation date": int(time.time())}
    return _bencode(t), hashlib.sha1(_bencode(info)).hexdigest()


def run_state() -> dict:
    """运行时状态（存在 IPFILTER_STATE 一个键里）：
    last 上次交给 qB 的时刻 / tried 上次尝试生成的时刻 / pending 还没下完的种子 hash /
    serving 当前提供下载的 "摘要/文件名" / summary 规则概要 / status 给设置页看的一句话。"""
    try:
        v = json.loads(config.IPFILTER_STATE or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def _save(**kw) -> None:
    config.set_many({"IPFILTER_STATE": json.dumps({**run_state(), **kw}, ensure_ascii=False)})


def _status(text: str, **kw) -> str:
    _save(status=f"{datetime.now():%m-%d %H:%M} {text}", **kw)
    return text


def serving_file(digest: str, name: str) -> Path | None:
    """/api/ipfilter/<摘要>/<文件名> 要的那个文件；不是当前这一份就 None（旧种子的下载源随之失效）。"""
    if run_state().get("serving") != f"{digest}/{name}":
        return None
    p = _DIR / name
    return p if p.is_file() else None


async def update() -> str:
    """生成规则 → 包成种子交给 qB。返回给用户看的结果。"""
    qb = engine.qb
    if not config.QB_ENABLED:
        return _status("没更新：qB 未启用")
    if not target_dir():
        return _status("没更新：下载目录（DOWN_PATH）和规则存放目录都没填")
    _save(tried=datetime.now().isoformat(timespec="seconds"))
    try:
        res = await generate()
    except Exception as e:
        log.warning("生成 IP 屏蔽规则失败：%s", e)
        return _status(f"生成失败：{type(e).__name__}，6 小时后再试（qB 里的旧规则照用）")
    if res["failed"]:
        log.warning("生成 IP 屏蔽规则：%d 个源没取到，这次不更新：%s", len(res["failed"]), "、".join(res["failed"]))
        return _status(f"有 {len(res['failed'])} 个数据源没取到（{'、'.join(res['failed'][:3])}），"
                       "这次不更新、旧规则照用，6 小时后再试")
    empty = [c for c in res["countries"] if not res["per"].get(c)]
    if empty:
        return _status(f"{','.join(empty)} 一条 IPv4 规则都没生成出来（国家代码写错了？），这次不更新")
    data = res["text"].encode("utf-8")
    _DIR.mkdir(exist_ok=True)
    tmp = _DIR / (NAME + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(_DIR / NAME)
    digest = hashlib.sha1(data).hexdigest()[:16]
    if run_state().get("pending"):     # 上一次的种子还在 qB 里（没下完）：先摘掉，只留一个
        await qb.delete([run_state()["pending"]], delete_files=False)
    torrent, h = build_torrent(data, NAME, f"{seed_base()}/api/ipfilter/{digest}/{NAME}")
    _save(serving=f"{digest}/{NAME}", pending="")
    ok = await qb.add_torrent(torrent, target_dir(), "", _TAG)
    if not ok:
        return _status("交给 qB 失败：" + ("连不上 qB" if ok is None else "qB 拒收（看日志）"))
    summary = f"{','.join(res['countries'])}：IPv4 {res['v4']} 条 + IPv6 {res['v6']} 条"
    log.info("IP 屏蔽规则已交给 qB 下载（%s）→ %s", summary, qb_file_path())
    return _status(f"已交给 qB 下载（{summary}），等它下完", pending=h, summary=summary,
                   last=datetime.now().isoformat(timespec="seconds"))


def _same_path(a: str, b: str) -> bool:
    return (a or "").replace("\\", "/").strip().lower() == (b or "").replace("\\", "/").strip().lower()


async def finish_pending() -> str | None:
    """qB 下完了没有：下完就把种子摘掉（留文件），qB 已指向这个文件就让它重新读一次。没有待办返回 None。"""
    qb = engine.qb
    st = run_state()
    h = st.get("pending")
    if not h:
        return None
    info = await qb.torrents_info([h])
    if info is None:
        return None                   # 连不上 qB：下轮再看
    d = info.get(h)
    if d is None:
        return _status("种子在 qB 里不见了（被手动删了？），下次到期再更新", pending="")
    if float(d.get("progress", 0) or 0) < 1.0:
        last = st.get("last")
        if last and datetime.now() - datetime.fromisoformat(last) > _DONE_WAIT:
            return _status(f"qB 一直没下完：确认 qB 那台能打开 {seed_base()}（不通就在设置里填『qB 访问本程序的地址』）")
        return None
    await qb.delete([h], delete_files=False)
    path = qb_file_path()
    manual = f"去 qB 设置 → 连接 → IP 过滤，勾上并填 {path}"
    prefs = await qb.preferences()
    if prefs is None:
        tail = f"，但没读到 qB 的设置：{manual}"
    elif prefs.get("ip_filter_enabled") and _same_path(prefs.get("ip_filter_path", ""), path):
        # qB 只在路径变了或开关切换时重读文件；路径没变，就关掉再打开一次
        await qb.set_preferences({"ip_filter_enabled": False})
        await qb.set_preferences({"ip_filter_enabled": True})
        tail = "，qB 已重新加载"
    else:
        # 打开 IP 过滤并指向这个文件（路径变了或开关打开，qB 都会当场读一次）。qB 文档要求路径用正斜杠
        old = prefs.get("ip_filter_path", "") if prefs.get("ip_filter_enabled") else ""
        if await qb.set_preferences({"ip_filter_enabled": True, "ip_filter_path": path.replace("\\", "/")}):
            tail = "，已在 qB 里打开 IP 过滤并指向它" + (f"（原来指向 {old}）" if old else "")
        else:
            tail = f"，但改 qB 设置失败：{manual}"
    log.info("IP 屏蔽规则更新完成：%s%s", path, tail)
    return _status(f"已更新（{st.get('summary', '')}）{tail}", pending="")


async def run_once() -> str:
    """更新一次并等 qB 下完收尾（局域网里几百 KB 是秒级的事，最多等一分钟；没等到就留给后台协程）。"""
    res = await update()
    for _ in range(12):
        if not run_state().get("pending"):
            break
        await asyncio.sleep(5)
        done = await finish_pending()
        if done:
            return done
    return res


def due(now: datetime | None = None) -> bool:
    days = config.IPFILTER_DAYS
    st = run_state()
    if days <= 0 or not config.QB_ENABLED or st.get("pending"):
        return False
    now = now or datetime.now()
    try:
        tried = datetime.fromisoformat(st["tried"]) if st.get("tried") else None
        last = datetime.fromisoformat(st["last"]) if st.get("last") else None
    except ValueError:
        return True
    if tried and (last is None or tried > last) and now - tried < _RETRY:
        return False                  # 上次没成功：隔一阵再试
    return last is None or now - last >= timedelta(days=days)
