"""SSRF 守卫：出站请求不许打到内网/环回地址。

【为什么单独一个模块】它有两个使用口径，而两者要给到不同层：
  · block_internal_request —— 【每一跳都判】。用在取种上：download_url 完全来自 RSS 正文，
    是攻击者可控的字符串，没有"用户自填所以可信"这回事。
    开了代理也照判字面内网 IP，只跳过 DNS 解析与钉 IP（那两步在代理模式下确实无意义）。
  · guard_redirect_request —— 【首跳放行、重定向后的每一跳都判】。用在其余全部出站上，
    由 config.http_client_kwargs 默认装上，所以【新写一处抓取不需要记得加】。
    首跳放行是有意的：feed 地址、webhook 地址、qB 地址都是用户自己在设置页填的，
    自建 Mikan 镜像 / 局域网 Bark 是正当用法，一律拦死等于砍掉这些场景。

守的是这个：被攻陷（或本就恶意）的源站回一个 302，就能让常驻协程周期性地对同机 qB 发一个
可控 GET —— 审查时实测收到过 `torrents/delete?hashes=all`。用户填的地址是他自己的选择，
第三方【替他改写】的跳转不是。

放在 core/ 而不是 services/：config 要用它（默认钩子），而 config 是最底层，
不能反过来 import core.engine。本模块只依赖 config 的 PROXY 一项，无环。
"""
import asyncio
import ipaddress
import socket

import httpx

import config


def ip_is_internal(ip) -> bool:
    """ipaddress 对象是否属内网/环回/链路本地/保留等不可路由到公网的范围。
    IPv4-mapped IPv6（::ffff:127.0.0.1）先归一到内嵌 IPv4 再判，防映射写法绕过。"""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


# 代理软件的 fake-ip 池。clash-meta 默认 `fake-ip-range: 198.18.0.1/16`，
# sing-box 默认 `198.18.0.0/15` —— 取并集就是 198.18.0.0/15（RFC 2544 的基准测试段）。
# 【为什么必须豁免它】开着 fake-ip 时，本机解析【每一个域名】都会得到这一段里的一个地址，
# 而 ipaddress 把 198.18.0.0/15 判为 is_private=True。不豁免的话，
# 代理模式下的内网判定会把**全部出站**（抓源、取种、bgm）一律拒掉——
# 而 core/ssrf.py 自己的说明就写着"开着本机 clash/v2ray 几乎是默认姿势"。
# 【豁免它是安全的】这一段在正常网络里不承载任何真实服务：只有当本机解析器【就是】
# fake-ip 代理时才会返回它，而那时真正的连接由代理去建、目标是域名对应的真实主机。
# 只豁免 IPv4：sing-box 的 IPv6 fake-ip 用 fc00::/18，落在 ULA 里，
# 豁免它等于放行整段 ULA（真实的局域网 IPv6 就住在那儿），代价不对等。
_FAKE_IP_V4 = ipaddress.ip_network("198.18.0.0/15")


async def safe_ip_for(host: str, allow_fake_ip: bool = False) -> str | None:
    """解析 host 并逐个校验，返回一个【已校验安全】的 IP；有任何一个地址落在内网/环回则返回 None。

    返回 IP 而不是只回布尔，是为了让调用方把连接【钉】在这个地址上——见 block_internal_request
    里对 DNS 重绑定的说明。字面 IP 原样返回（校验通过的话），域名取第一个解析结果。
    解析失败/无结果保守视作内网（反正也连不上）。
    """
    def _bad(ip, resolved: bool) -> bool:
        # 【豁免只对【解析出来的】地址成立】(R21 收紧) fake-ip 的立论是"只有当本机解析器
        # 就是 fake-ip 代理时才会返回这一段"——那是关于 **DNS 解析结果** 的论断。
        # URL 里【字面写着】198.18.x.x 完全不需要这个前提：被投毒的 feed 给出
        # `http://198.18.0.1:8080/api/v2/torrents/delete?hashes=all` 时，
        # 原来这里照样放行（而 config._internal_literal_host 还会顺手把它挂成直连、绕开代理），
        # 于是守卫既没拦住、又替它避开了代理。字面 IP 一律按 ip_is_internal 判。
        if resolved and allow_fake_ip and ip.version == 4 and ip in _FAKE_IP_V4:
            return False                       # 代理的 fake-ip 池，见 _FAKE_IP_V4 处的说明
        return ip_is_internal(ip)

    host = (host or "").strip("[]")            # 去 IPv6 字面量方括号
    try:
        return None if _bad(ipaddress.ip_address(host), resolved=False) else host
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError, ValueError):
        return None
    if not infos:
        return None
    first = None
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return None                        # 解析出无法识别的地址形态 → 保守拒
        if _bad(addr, resolved=True):
            return None                        # 只要有一个内网地址就整体拒（别给轮询解析留缝）
        if first is None:
            first = info[4][0]
    return first


async def block_internal_request(request: httpx.Request) -> None:
    """请求级钩子：种子下载不许打到内网/环回地址（含重定向后的每一跳）——挡住 RSS 里的 SSRF 载荷。

    【校验完还要把连接钉在校验过的那个 IP 上】否则是典型的 TOCTOU：我们在这里解析一次判定安全，
    httpx 建连接时会【再解析一次】，两次之间没有任何绑定。攻击者用 TTL=0 的 DNS 交替应答
    （第一次回公网 IP 放行、第二次回 127.0.0.1）即可绕过——实测可取回本机服务的响应。
    把 URL 主机改写成已校验的 IP、Host 头保留原域名、https 再带上 sni_hostname，
    证书校验与虚拟主机都照常工作（实测 http/https 均 200）。
    本钩子对每一跳重定向都会跑，所以每跳各自钉各自的，不必自己重写重定向循环。

    【代理模式下只跳过"解析+钉IP"，不跳过整条守卫】旧写法是 `if config.PROXY: return`，
    理由写的是"目标由代理侧解析、本地判定既无意义又会误伤"——那句话**只对域名成立**。
    URL 里写死的字面私网/环回 IP 是谁解析都一样的地址，不存在误伤，跳过它是零收益。
    而代价很大：本项目要抓 nyaa/bgm，「开着本机 clash/v2ray」几乎是默认姿势，
    于是一个把 download_url 写成 http://127.0.0.1:8080/api/v2/torrents/delete?hashes=all
    的 feed，在最常见的配置下能让后台 flush 协程替它去打同机 qB。实测复现过。
    """
    host = request.url.host or ""
    if config.PROXY:
        # 【代理模式下也要解析主机名，只是不钉 IP】(E-40，2026-09-01 拍板)
        # 旧写法的判据是"ipaddress 解析得出来的才判"，而那既不是"是不是字面 IP"、
        # 更不是"是不是主机名"——三类东西一起从这个缺口出去：
        #   ① `localhost`：它还被 config._DIRECT_PATTERNS 挂成【恒直连】，连代理都不走，
        #      代理指向一个死端口时照样能打通；
        #   ② `2130706433` / `0x7f000001` 这类整数写法：ipaddress 一律 ValueError → 放行，
        #      而请求交给代理之后，本机 clash/v2ray 会自己 inet_aton 出 127.0.0.1
        #      并从【同一台机器】连过去。这两个写法【正是】本项目自己
        #      tests/test_ssrf.py 那条 "..._in_every_notation_are_refused" 参数表里的——
        #      那条用例只在无代理模式下跑，是标准的"约束的作用域大于验证的作用域"。
        #   ③ 攻击者自有域名指向 127.0.0.1（这一条是【有意放行】的，见下）。
        #
        # 【为什么不钉 IP】代理模式下真正去解析目标的是代理侧，本地钉了也没用
        #（请求发给代理时 Host 才是目标），钉反而会把 URL 改写成一个代理够不着的地址。
        # 【误伤面】域名指向局域网自建服务（Mikan 镜像 / Bark / ntfy）会被拦。
        # 但 guard_redirect_request 的【首跳本来就放行】，被这一条影响的只有
        # 「重定向之后的跳」与「取种」——那两处指向局域网本来就不是正当用法。
        if await safe_ip_for(host, allow_fake_ip=True) is None:
            raise ValueError(f"拒绝请求内网/环回地址（防 SSRF）：{host}")
        return
    ip = await safe_ip_for(host)
    if ip is None:
        raise ValueError(f"拒绝请求内网/环回地址（防 SSRF）：{host}")
    # 【已知取舍】钉住单个地址 = 放弃 httpx/anyio 的 happy-eyeballs 多地址回退（RFC 6555）：
    # 域名解析出多个地址时，原先连不通第一个会自动试下一个，现在只连我们选中的那个。
    # 接受它的理由：多地址回退只在"第一个地址不通、其余通"时才有价值，而那对本工具的场景
    # （固定几个种子站、走同一条家宽）很罕见；而不钉就等于把 DNS 重绑定的洞留着。
    # 真要两者兼得，得在这条关键下载路径上加一层"逐个候选地址重试"的循环——
    # 那条路径一旦写错就是【所有下载全挂】，代价不对等，故不做。
    if ip != host:                     # 域名 → 钉到刚校验过的地址；字面 IP 无需改写
        # 【Host 头要带端口】request.url.host 不含端口，直接拿它当 Host 会把
        # 'example.com:8443' 写成 'example.com' —— 非默认端口的私站取种恒失败，
        # 而浏览器/curl 一切正常，排查方向会被完全带偏。netloc 是"主机[:端口]"的原样。
        # 改 host 之前先取 netloc（copy_with 之后 netloc 里的主机已经变成 IP 了）。
        netloc = request.url.netloc.decode("ascii")
        request.url = request.url.copy_with(host=ip)
        request.headers["Host"] = netloc
        if request.url.scheme == "https":
            request.extensions["sni_hostname"] = host

async def guard_redirect_request(request: httpx.Request) -> None:
    """请求级钩子：**首跳放行，重定向后的每一跳强制内网判定**。config 默认给所有客户端装上。

    【怎么知道这是第几跳】httpx 建重定向请求时用 `Request(extensions=request.extensions)`，
    而 Request 里是 `dict(extensions)` —— 浅拷贝：字典本身是新的，但里面的【可变值是同一个对象】。
    所以往 extensions 里塞一个 list、在钩子里往它 append，计数就能顺着整条重定向链传下去，
    调用方一行都不用改（实测：首跳 len=0、302 之后 len=1）。
    不用 response 钩子看 history 是因为那时请求【已经发出去了】——对 SSRF 来说太晚。
    """
    hops = request.extensions.setdefault("autorss_hops", [])
    here = (request.url.host, request.url.port)
    if not hops:
        hops.append(here)
        return                       # 首跳：地址是用户自己填的，放行（自建镜像/局域网 webhook）
    if here == hops[0]:
        # 【同主机同端口的跳转照样放行】不然会误伤一种很常见的正当配置：局域网自建服务
        # （ntfy / Bark / Mikan 镜像）把 http 跳到自己的 https。那种跳转没有把请求送到
        # 「别的地方」，而这条守卫要挡的恰恰是"第三方替用户改写了目的地"。
        # 端口一并比：同主机换端口就是换了个服务（192.168.1.5:9000 → :8080 很可能正是 qB）。
        # 端口取的是 URL 里【显式写的】那个，http/https 的默认端口两边都是 None，故协议升级能过。
        return
    await block_internal_request(request)
