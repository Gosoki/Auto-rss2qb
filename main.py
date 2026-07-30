"""入口：初始化数据库、启动后台轮询器、跑 NiceGUI 界面。

运行： python main.py    然后浏览器打开 http://<host>:2333
"""
import asyncio
import logging
import sys

# 【版本闸放在最前面，早于任何业务导入】网络层全靠 asyncio.timeout 做总超时（services/fetch.py、
# services/enrich.py、engine.fetch_torrent_bytes），而它是 Python 3.11 才有的。3.10 上模块能导入、
# 页面能开、设置能存——只有每次真去抓取时才抛 AttributeError，且被各处的宽 except 收成一行日志：
# 表现正是下面 _startup 注释最忌的那种"看着一切正常，实则什么都没在跑"。宁可起不来，也别假装正常。
if sys.version_info < (3, 11):
    raise SystemExit(
        f"需要 Python ≥ 3.11，当前是 {sys.version.split()[0]}。"
        "（网络层用了 3.11 的 asyncio.timeout；在 3.10 上界面能开但采集/识别/下载会全部静默失败）")

from nicegui import app, ui  # noqa: E402

import config
import pages  # noqa: F401  导入即注册页面
from config import WEB_HOST, WEB_PORT
from core.logsetup import setup_logging
from core.netguard import install as install_netguard
from core import worker as worker_mod
from core.worker import (init_business_state, run_db_watch, run_movie_scan, run_qb_sync,
                         run_reenrich_retry, run_worker)
import db
from db import apply_configured_backend, init_db

setup_logging()   # 控制台 + 滚动文件(data/autorss.log) + 内存环形缓冲(供 /logs 页实时看)
install_netguard(app)   # 网段白名单中间件（WEB_ALLOW_CIDRS 为空则放行一切；须在起服务器前挂）

log = logging.getLogger("autorss")


@app.on_startup
async def _startup():
    # 【这三步必须整体兜住】它们裸调时，任一抛异常都会让下面五个 create_task 一个都建不起来，
    # 而 NiceGUI 只是把异常记一行、Web 服务照常起：页面能开、看着一切正常，实则采集/下载/
    # qB 同步/重识别【全部没在跑】，用户只会觉得"好几天没更新了"。宁可明确停摆也不要假装正常。
    try:
        init_db()
        config.load_from_db()       # 把数据库里的配置覆盖加载进内存（必须在建表之后）
        # 配置读出来了才知道业务数据该连哪：DB_BACKEND=mysql 就把业务表切过去。
        # 配置本身恒在本地 SQLite，所以即使 MySQL 连不上也读得到设置、进得去设置页改（见 db 的双引擎说明）。
        log.info("业务数据库：%s", apply_configured_backend())
    except Exception as e:
        # 用 fatal 而不是普通停摆：建表/迁移失败时 SELECT 1 照样能通，看守协程一探就会误判"已恢复"，
        # 让后台在一个半截的表结构上跑起来。这种必须等人来处理。
        log.exception("启动初始化失败，系统停摆")
        db.mark_data_fatal(f"启动初始化失败：{type(e).__name__}: {e}")
    # 业务库不可用时【跳过】这几步初始化——它们全要写业务表，此刻必抛。不是放弃：
    # run_db_watch 探到库回来会补跑同一个函数，所以这里静静让过去，别把启动整个掀翻。
    if db.data_down():
        log.error("业务数据库停摆中，采集/下载/同步全部暂停；到设置页『数据库』改连接或切回本地 SQLite")
        # 这一支跳过了 init_business_state，其中的"复位上个进程遗留的 downloading"就欠着了。
        # 记个账，让 run_db_watch 在库首次可用时补做（那时库从启动起一直 down，
        # 所有后台循环都空转过，绝不可能有交付协程在半途，复位是安全的）。
        worker_mod._startup_reset_pending = True
    else:
        try:
            init_business_state()
        except Exception:           # 复位/种源组失败不该连累后台协程的建立
            log.exception("业务初始化失败（采集仍会启动，遗留的 downloading 可能未复位）")
    # 协程照常建：它们各自都按 db.data_down() 把门，停摆期间空转短睡，库一恢复就自动接上。
    asyncio.create_task(run_db_watch())    # 业务库健康看守：连不上→停摆；恢复→自动接上
    asyncio.create_task(run_worker())
    asyncio.create_task(run_qb_sync())    # qB 种子实时态同步（独立频率）
    asyncio.create_task(run_movie_scan())  # 剧场版/OVA 自动扫描（独立频率）
    asyncio.create_task(run_reenrich_retry())  # 『待识别』番后台重试 bgm（独立频率，不阻塞采集）


if __name__ in {"__main__", "__mp_main__"}:
    # 监听地址默认回环 127.0.0.1：本工具无鉴权、设置页含 qB 密码等敏感信息，默认不对局域网开放。
    # 可在设置页改绑定地址（写 .env、需重启）；改成 0.0.0.0 会对内网开放，务必自行加鉴权/反代（页内已警示）。
    ui.run(title="autorss", host=WEB_HOST, port=WEB_PORT, show=False, reload=False)
