"""qB 完成回调接口 `/api/qb/done`：qB『完成时运行外部程序』回调这里，精确把该集标记为已下完。

在 qB 里配（Options → Downloads → Run external program on torrent finished）：
    curl -s -X POST "http://127.0.0.1:<Web端口>/api/qb/done?hash=%I"
%I 是 qB 替换成的种子 v1 info hash（正好是我们的主键）。设了 QB_CALLBACK_TOKEN 就再带 &t=<token>。

用 POST 而非 GET：GET 会被任意网页的 <img>/<script> 无声触发（CSRF），把在下种子伪造成『已下』；
POST 无法这样触发。qB 的 run-external-program 支持 curl -X POST，故合法回调不受影响。

不配也行——只是慢速下完后被 qB 删（remove-on-complete）的种子会被标 error（详见设置页说明）。
接口只认我们自己表里已交付的 hash、校验 40hex，绑在 127.0.0.1，风险低。
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
    # 两侧编码成 bytes 再常量时间比较：str 版 compare_digest 遇非 ASCII 的 t（外部可控 query 参数）会抛
    # TypeError→500 刷栈；bytes 版无此限制。仍是常量时间，堵计时侧信道（绑 0.0.0.0 时才有意义）。
    elif not hmac.compare_digest(t.encode("utf-8"), tok.encode("utf-8")):
        return {"ok": False, "error": "bad token"}
    return {"ok": True, "marked": engine.mark_done_by_hash(hash)}
