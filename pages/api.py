"""qB 完成回调接口 `/api/qb/done`：qB『完成时运行外部程序』回调这里，精确把该集标记为已下完。

在 qB 里配（Options → Downloads → Run external program on torrent finished）：
    curl -s -X POST "http://127.0.0.1:<Web端口>/api/qb/done?hash=%I"
%I 是 qB 替换成的种子 v1 info hash（正好是我们的主键）。设了 QB_CALLBACK_TOKEN 就再带 &t=<token>。

用 POST 而非 GET：GET 会被任意网页的 <img>/<script> 无声触发（CSRF），把在下种子伪造成『已下』；
POST 无法这样触发。qB 的 run-external-program 支持 curl -X POST，故合法回调不受影响。

不配也行——只是慢速下完后被 qB 删（remove-on-complete）的种子会被标 error（详见设置页说明）。
接口只认我们自己表里已交付的 hash、校验 40hex。**但不设 token 时的免鉴权分支靠的是三条"这不是浏览器/
不是反代"的判据（见下），只是纵深防御，不等于鉴权**——qB 与本程序不同机、或本机跑着不受信的浏览器时，
请到设置页设一个 token（页面上那条 curl 会自动带 &t=）。
"""
import hmac

from fastapi import Request
from nicegui import app

import config
from core import engine


@app.post("/api/qb/done")
def qb_done(request: Request, hash: str = "", t: str = "") -> dict:
    """qB 完成回调：hash=种子 info hash（%I），t=可选 token。标记成功返回 {'ok':True,'marked':True}。"""
    tok = config.QB_CALLBACK_TOKEN
    if not tok:
        # 【没设 token 时只认本机】以前 `if tok and ...` 在 token 为空时整条短路＝谁都能调。
        # 上面 docstring 说"POST 无法被网页无声触发"——那只对 <img>/<script> 成立，对自动提交表单
        # 和 no-cors fetch 不成立（简单请求、不触发预检、不需要读响应）。于是绑 0.0.0.0 时，
        # 内网里任意一个网页就能把在下种子批量写成『已下完 100%』（hash 从公开 RSS 就能抓），
        # 那些行从此脱离 in-flight、sync 不再复查、停滞检测也再够不着它们。
        # qB 与本程序不同机时请到设置页设一个 token，命令行会自动带上 &t=<token>。
        peer = getattr(request.client, "host", "") if request.client else ""
        if peer not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return {"ok": False, "error": "callback token required from non-local address"}
        # 【同机反向代理也要拦】main.py 设了 proxy_headers=False，对端恒是直连方——
        # 而同机 nginx/caddy 反代过来的请求，对端【正好就是 127.0.0.1】，于是外网任何人经反代打进来
        # 都会落进这个免 token 分支（hash 从公开 RSS 就能抓，可把在下种子批量伪造成『已下完』）。
        # 判据用『带没带转发头』：反代自己会追加 X-Forwarded-For/Forwarded，客户端删不掉它；
        # 而真·本机 curl 本来就不会带。带了就说明这不是本机直连，必须要 token。
        if request.headers.get("x-forwarded-for") or request.headers.get("forwarded"):
            return {"ok": False, "error": "callback token required behind a reverse proxy"}
        # 【上面两条都是"否定式"判据，各有一个默认配置就能踩穿的缺口】：
        #   · 对端判据：WEB_HOST 默认就是 127.0.0.1，此时能访问到本接口的浏览器【本来就在本机】，
        #     对端恒等于 127.0.0.1 —— 于是本机浏览器里任意一个网页都能 no-cors POST 打进来。
        #   · 转发头判据：nginx/caddy 的最小反代配置【不会】主动加 X-Forwarded-For（要写 proxy_set_header
        #     才有），于是"带了转发头才算反代"在最常见的那份配置下恒为假。
        # 补一条【肯定式】判据收口：浏览器发起的跨源请求必带 Origin，同源与跨源都必带 Sec-Fetch-Site
        # （Chrome/Firefox/Safari 现行版本均有），而 qB 的 `curl -X POST` 三个头一个都不带。
        # 于是"是不是浏览器发来的"变成了正向可判定的事实，不再依赖对方没做什么。
        if (request.headers.get("origin") or request.headers.get("referer")
                or request.headers.get("sec-fetch-site") or request.headers.get("sec-fetch-mode")):
            return {"ok": False, "error": "callback token required for browser-originated requests"}
    # 两侧编码成 bytes 再常量时间比较：str 版 compare_digest 遇非 ASCII 的 t（外部可控 query 参数）会抛
    # TypeError→500 刷栈；bytes 版无此限制。仍是常量时间，堵计时侧信道（绑 0.0.0.0 时才有意义）。
    elif not hmac.compare_digest(t.encode("utf-8"), tok.encode("utf-8")):
        return {"ok": False, "error": "bad token"}
    return {"ok": True, "marked": engine.mark_done_by_hash(hash)}
