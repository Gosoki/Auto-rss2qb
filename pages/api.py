"""qB 完成回调接口 `/api/qb/done`：qB『完成时运行外部程序』回调这里，精确把该集标记为已下完。

在 qB 里配（Options → Downloads → Run external program on torrent finished）：
    curl -s -X POST "http://127.0.0.1:<Web端口>/api/qb/done?hash=%I"
%I 是 qB 替换成的种子 v1 info hash（正好是我们的主键）。设了 QB_CALLBACK_TOKEN 就再带 &t=<token>。

用 POST 而非 GET：GET 会被任意网页的 <img>/<script> 无声触发（CSRF），把在下种子伪造成『已下』；
POST 无法这样触发。qB 的 run-external-program 支持 curl -X POST，故合法回调不受影响。

不配也行——只是慢速下完后被 qB 删（remove-on-complete）的种子会被标 error（详见设置页说明）。
接口只认我们自己表里已交付的 hash、校验 40hex，绑在 127.0.0.1，风险低。
"""
from nicegui import app

import config
from core import engine


@app.post("/api/qb/done")
def qb_done(hash: str = "", t: str = "") -> dict:
    """qB 完成回调：hash=种子 info hash（%I），t=可选 token。标记成功返回 {'ok':True,'marked':True}。"""
    tok = config.QB_CALLBACK_TOKEN
    if tok and t != tok:
        return {"ok": False, "error": "bad token"}
    return {"ok": True, "marked": engine.mark_done_by_hash(hash)}
