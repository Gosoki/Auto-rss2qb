"""生成 qBittorrent 用的「屏蔽日本 IP」过滤规则文件（.p2p）。

用法：
    .venv/bin/python make_jp_ipfilter.py            # 写出 ipfilter-jp.p2p
    .venv/bin/python make_jp_ipfilter.py --force    # 有源取不到也照写（默认拒写，见下）

【设计前提】宁可误杀，绝不放过。所以：
1. 取【多个来源的并集】而不是任一单一来源。注册数据（RIR）和地理定位数据（GeoLite2）
   覆盖面不同：AWS 东京、GCP 大阪这类机房的 IP 注册在美国 ARIN 名下，只查 RIR 会整片漏掉；
   反过来 GeoLite2 也会漏掉刚分配、还没被探测到的新段。两类都要。
2. 云厂商的日本区另行显式加入（AWS ap-northeast-1/3、GCP asia-northeast1/2）——
   监控/取证节点最爱开在这上面，不能只指望地理库标对。
3. 相邻区间之间【小于 GAP_FILL 个地址的缝】直接填平（默认 4096 = 一个 /20）。
   日本的地址块常常是同一个 /16 切给多家 ISP，缝里多半还是日本；填平既减少行数，
   也顺手盖住"某个源漏登记、另一个源也没标"的空档。这是明确的「宁可误杀」取舍。
4. 任何一个源取不到就【拒绝写文件】（除非 --force）。少一个源＝少一片覆盖，
   而少的那片你不会知道是哪片——静默写出一个更薄的文件才是真正的危险。

【为什么是 .p2p】qB 三种格式里只有 .p2p 既是区间制（行数最少）又带描述字段，
且能装下 IPv6。IPv6 行统一排在文件末尾：万一哪个 qB 版本的解析器碰到 v6 行就停，
前面的 IPv4 规则也已经全部生效了。

【更新】这份数据会过期（新分配的日本地址段不在旧文件里）。建议每月跑一次重新生成，
生成完在 qB 里点一下『应用』重新载入。
"""
from __future__ import annotations

import ipaddress
import json
import sys
import urllib.request

OUT = "ipfilter-jp.p2p"
GAP_FILL = 4096          # 相邻区间间隔 ≤ 此值就填平（仅 IPv4；v6 地址空间太大，不做）
TIMEOUT = 60

# 五大 RIR 的分配记录：注册国＝JP 的全部地址段（含在别的 RIR 登记的日本机构）
_RIRS = {
    "apnic": "https://ftp.apnic.net/apnic/stats/apnic/delegated-apnic-latest",
    "arin": "https://ftp.arin.net/pub/stats/arin/delegated-arin-extended-latest",
    "ripencc": "https://ftp.ripe.net/pub/stats/ripencc/delegated-ripencc-latest",
    "lacnic": "https://ftp.lacnic.net/pub/stats/lacnic/delegated-lacnic-latest",
    "afrinic": "https://ftp.afrinic.net/pub/stats/afrinic/delegated-afrinic-latest",
}
_CIDR_LISTS = {
    "ipdeny-v4": "https://www.ipdeny.com/ipblocks/data/aggregated/jp-aggregated.zone",
    "ipdeny-v6": "https://www.ipdeny.com/ipv6/ipaddresses/aggregated/jp-aggregated.zone",
    "ipverse-v4": "https://raw.githubusercontent.com/ipverse/rir-ip/master/country/jp/ipv4-aggregated.txt",
    "ipverse-v6": "https://raw.githubusercontent.com/ipverse/rir-ip/master/country/jp/ipv6-aggregated.txt",
    # GeoLite2 派生的地理定位表（v4+v6 混在一个文件）：抓注册在境外、实际落在日本的段
    "geolite2": "https://raw.githubusercontent.com/Loyalsoldier/geoip/release/text/jp.txt",
}
_AWS_JP = {"ap-northeast-1", "ap-northeast-3"}          # 东京、大阪（-2 是首尔，不要）
_GCP_JP = {"asia-northeast1", "asia-northeast2"}        # 东京、大阪（-3 是首尔，不要）


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "autorss-ipfilter/1"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _add_cidr(out: list, text: str) -> int:
    n = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            net = ipaddress.ip_network(line, strict=False)
        except ValueError:
            continue
        out.append(net)
        n += 1
    return n


def _from_rir(text: str) -> list:
    """RIR 分配记录：形如 apnic|JP|ipv4|1.0.16.0|4096|20110412|allocated

    IPv4 的第 5 段是【地址个数】而不是前缀长度，且不保证是 2 的幂（历史遗留的零散分配），
    所以按 start~start+count-1 的区间算，再交给 summarize 切成 CIDR。
    """
    nets = []
    for line in text.splitlines():
        f = line.split("|")
        if len(f) < 7 or f[1] != "JP" or f[6] in ("reserved", "available"):
            continue
        try:
            if f[2] == "ipv4":
                start = ipaddress.IPv4Address(f[3])
                end = ipaddress.IPv4Address(int(start) + int(f[4]) - 1)
                nets += ipaddress.summarize_address_range(start, end)
            elif f[2] == "ipv6":
                nets.append(ipaddress.ip_network(f"{f[3]}/{f[4]}", strict=False))
        except (ValueError, ipaddress.AddressValueError):
            continue
    return nets


def _from_aws(text: str) -> list:
    d = json.loads(text)
    return ([ipaddress.ip_network(p["ip_prefix"]) for p in d["prefixes"]
             if p["region"] in _AWS_JP]
            + [ipaddress.ip_network(p["ipv6_prefix"]) for p in d["ipv6_prefixes"]
               if p["region"] in _AWS_JP])


def _from_gcp(text: str) -> list:
    out = []
    for p in json.loads(text)["prefixes"]:
        if p.get("scope") in _GCP_JP:
            cidr = p.get("ipv4Prefix") or p.get("ipv6Prefix")
            if cidr:
                out.append(ipaddress.ip_network(cidr))
    return out


def collect() -> tuple[list, list]:
    """返回 (网段列表, 失败的源名列表)。每个源单独报数，方便看出谁贡献了什么。"""
    nets, failed = [], []

    def take(name, fn):
        try:
            got = fn()
        except Exception as e:                       # 网络/格式问题都只影响这一个源
            print(f"  ✗ {name:<12} 取失败：{type(e).__name__}: {e}")
            failed.append(name)
            return
        nets.extend(got)
        v4 = sum(1 for n in got if n.version == 4)
        print(f"  ✓ {name:<12} {len(got):>6} 段（v4 {v4} / v6 {len(got) - v4}）")

    for name, url in _RIRS.items():
        take(name, lambda u=url: _from_rir(_get(u)))
    for name, url in _CIDR_LISTS.items():
        def one(u=url):
            acc = []
            _add_cidr(acc, _get(u))
            return acc
        take(name, one)
    take("aws-jp", lambda: _from_aws(_get("https://ip-ranges.amazonaws.com/ip-ranges.json")))
    take("gcp-jp", lambda: _from_gcp(_get("https://www.gstatic.com/ipranges/cloud.json")))
    return nets, failed


def merge(nets: list, version: int) -> list:
    """合成【最少条数】的区间列表。collapse 只并 CIDR 邻接，这里再按整数区间并一次，
    顺带填平 ≤ GAP_FILL 的小缝（v4 才填）。返回 [(start_int, end_int), …]。"""
    same = [n for n in nets if n.version == version]
    if not same:
        return []
    ranges = sorted((int(n.network_address), int(n.broadcast_address))
                    for n in ipaddress.collapse_addresses(same))
    gap = GAP_FILL if version == 4 else 0
    out = [list(ranges[0])]
    for s, e in ranges[1:]:
        if s <= out[-1][1] + 1 + gap:                # 重叠、紧邻、或缝小到该填平
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def main() -> int:
    force = "--force" in sys.argv
    print("拉取数据源…")
    nets, failed = collect()
    if failed and not force:
        print(f"\n❌ 有 {len(failed)} 个源没取到：{', '.join(failed)}")
        print("   少一个源就少一片覆盖，而你不会知道少的是哪片 —— 已【拒绝写出文件】，"
              "旧文件原样保留。\n   网络问题就重跑；确实要将就，加 --force。")
        return 1

    v4, v6 = merge(nets, 4), merge(nets, 6)
    n_addr = sum(e - s + 1 for s, e in v4)
    lines = [f"JP:{ipaddress.IPv4Address(s)}-{ipaddress.IPv4Address(e)}" for s, e in v4]
    # v6 排最后：万一解析器碰到 v6 行就罢工，前面的 v4 规则已经全部读进去了
    lines += [f"JP:{ipaddress.IPv6Address(s)}-{ipaddress.IPv6Address(e)}" for s, e in v6]
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("# 日本 IP 屏蔽规则（qBittorrent .p2p 格式）\n"
                f"# 由 make_jp_ipfilter.py 生成；IPv4 {len(v4)} 条 + IPv6 {len(v6)} 条\n")
        f.write("\n".join(lines) + "\n")

    print(f"\n✅ 已写出 {OUT}")
    print(f"   IPv4 {len(v4):>5} 条区间，覆盖 {n_addr:,} 个地址（≈ {n_addr / 2**24:.1f} 个 /8）")
    print(f"   IPv6 {len(v6):>5} 条区间")
    print(f"   qB 载入后日志会报 'IP filter successfully parsed: N rules'，"
          f"N 应约等于 {len(v4) + len(v6)}")
    if force and failed:
        print(f"   ⚠️ 是在缺 {', '.join(failed)} 的情况下生成的，覆盖面不完整")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
