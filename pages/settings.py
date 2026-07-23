"""设置页 `/settings`：读当前配置、改、即时生效。

绝大多数项写进数据库 settings 表并热更新内存（config.set_many），保存即生效、不必重启。
仅 WEB_PORT 这类绑定项仍走 .env（_RESTART_ONLY），改了要重启。数字项做校验，避免写入非数字。
"""
from nicegui import ui

from core import anime, engine
import config
from sources.parse import format_quarter
from .layout import frame

_NUMERIC = {"ANIME_POLL_INTERVAL", "ANIME_DOWNLOAD_GRACE_MIN", "WEB_PORT", "QB_SYNC_INTERVAL",
            "QB_SYNC_BACKSTOP_MIN", "QB_ACTIVE_FLOOR_KBPS", "QB_SLOW_ROUNDS",
            "ANIME_PAGE_YEARS", "MOVIE_PAGE_YEARS"}
_PASSWORD = {"QB_PASSWORD"}
_RESTART_ONLY = {"WEB_PORT"}  # 绑端口，仍走 .env、改了要重启；其余都进 DB 即时生效

_QUARTER_PRESETS = {
    "{yy}{q}": "字母  → 26C",
    "{yy}{season}": "季节  → 26夏",
    "{yy}年{m}月": "月份  → 26年7月",
    "{yy}{q} · {m}月 · {season}": "组合  → 26C · 7月 · 夏",
}


def _quarter_setting(f: dict, key: str, title: str, note: str, value: str) -> None:
    """季度模板设置块：标题 + 说明 + 模板输入 + 实时预览 + 预设下拉。控件写入 f[key]。"""
    ui.separator()
    ui.label(title).classes("font-bold text-sm")
    inp = ui.input("季度模板", value=value).classes("w-full")
    f[key] = inp
    ui.label(note + "  占位：{yy}=26 {yyyy}=2026 {q}=C {season}=夏 {m}=7").classes(
        "text-xs text-gray-500")
    preview = ui.label().classes("text-sm text-blue")

    def _prev():
        preview.text = "预览： " + " ／ ".join(
            format_quarter("26" + c, inp.value or "") for c in "ABCD")

    inp.on_value_change(lambda e: _prev())

    def _pick(e):
        if e.value:
            inp.value = e.value  # 触发 on_value_change 刷新预览
            _prev()

    ui.select(_QUARTER_PRESETS, label="预设（选中填入上面模板，可再手改）",
              on_change=_pick).props("dense outlined").classes("w-full")
    _prev()


@ui.page("/settings")
def settings():
    with frame("settings"):
        ui.label("设置").classes("text-2xl font-bold")
        ui.label("改动存进数据库、保存即时生效（页面刷新可见）。仅 Web 端口改动需重启。").classes(
            "text-xs text-gray-400 mb-2")

        f: dict = {}  # 表单控件，key = .env 键名

        def _switch(key, label, val):
            f[key] = ui.switch(label, value=val).props("dense")

        def _text(key, label, val):
            f[key] = ui.input(label, value=str(val)).classes("w-full")

        def _num(key, label, val):
            f[key] = ui.number(label, value=val, format="%d").classes("w-full")

        def _password(key, label):
            f[key] = ui.input(label, value="", password=True).classes("w-full")  # 不回填现值

        with ui.card().classes("w-full"):
            ui.label("采集").classes("font-bold")
            _switch("ANIME_POLL_ENABLED", "启用后台采集（关=暂停抓取；首次配置好前可先关着）",
                    config.ANIME_POLL_ENABLED)
            _num("ANIME_POLL_INTERVAL", "轮询间隔（秒）", config.ANIME_POLL_INTERVAL)
            _num("ANIME_DOWNLOAD_GRACE_MIN", "下载缓冲窗口（分钟，多源等偏好组补齐）", config.ANIME_DOWNLOAD_GRACE_MIN)
            _switch("ANIME_TOP_PRIORITY_INSTANT", "最高优先级组入库即下（跳过缓冲窗口）", config.ANIME_TOP_PRIORITY_INSTANT)
            _switch("ANIME_MULTIBRACKET_PARSE",
                    "多括号命名回退捕获（沸羊羊/悠哈/GM-Team 等 [组][番名][集] 格式）",
                    config.ANIME_MULTIBRACKET_PARSE)
            ui.label("默认关：解析不出番名的种子直接进『待识别』。开了才尝试从括号块猜番名——best-effort，"
                     "偶尔可能猜错，拿不准会自动跳过；大组(ANi/Lilith 等)永不受影响。可在『解析测试』页粘标题验证。").classes(
                "text-xs text-gray-500")
            ui.label("Bangumi 识别：项目恒开（规范名/季度/日语文件夹名统一采用 bgm）。").classes(
                "text-xs text-gray-500")
            ui.label("源组（feed/策略/优先级/字幕组白名单）都在『源管理』页配置，改完下一轮生效。").classes(
                "text-xs text-gray-500")

        with ui.card().classes("w-full"):
            ui.label("下载 / qBittorrent").classes("font-bold")
            _switch("QB_ENABLED", "发送种子到 qB（关=只采集不下载）", config.QB_ENABLED)
            _switch("QB_SYNC_STATUS", "读取 qB 实时状态（关=发送过去即『已下』，完全不轮询 qB）",
                    config.QB_SYNC_STATUS)
            _num("QB_SYNC_INTERVAL", "qB 活跃轮询间隔（秒）——仅在有种子正在下时按此频率拉进度",
                 config.QB_SYNC_INTERVAL)
            _num("QB_SYNC_BACKSTOP_MIN", "qB 保底自查间隔（分钟）——没被唤醒也每隔这么久兜底扫一次",
                 config.QB_SYNC_BACKSTOP_MIN)
            with ui.row().classes("items-center gap-4 flex-wrap"):
                _num("QB_ACTIVE_FLOOR_KBPS", "慢速地板（KB/s，慢于此算没在真下）", config.QB_ACTIVE_FLOOR_KBPS)
                _num("QB_SLOW_ROUNDS", "判慢轮次（连续几轮都没真下才休眠）", config.QB_SLOW_ROUNDS)
            ui.label("开状态跟踪：事件驱动——种子交给 qB 时立刻开始跟、按活跃间隔拉进度，全下完就休眠、不再打扰 qB；"
                     "下完/做种/文件缺失/卡住无源/慢过地板 的种子都不再高频轮询，只由保底间隔偶尔兜底（默认 180=3 小时）。"
                     "慢速地板 20KB/s + 连续 3 轮判慢：某种子长期龟速也能让循环休眠；但只要还有别的种子在真下，"
                     "每轮批量刷新会顺便把慢的一起更新。关状态跟踪：发送过去即当『已下』，一次 qB 都不查（零轮询、看不到进度）。").classes(
                "text-xs text-gray-500")

            ui.separator()
            ui.label("完成回调（可选·精确兜底）").classes("font-bold text-sm")
            _text("QB_CALLBACK_TOKEN", "回调 token（可选，防被乱调；填了下面命令会自动带上 &t=，改完记得点保存）",
                  config.QB_CALLBACK_TOKEN)
            ui.label("把下面这行填进 qB → Options → Downloads → 『Run external program on torrent finished』，"
                     "种子一下完 qB 就回调、精确把这一集标『已下』（%I 由 qB 替换成种子 hash）：").classes(
                "text-xs text-gray-500")

            @ui.refreshable
            def _cb_cmd():
                tok = (f["QB_CALLBACK_TOKEN"].value or "").strip()   # 读输入框实时值，不是已保存值
                cmd = (f'curl -s -X POST "http://127.0.0.1:{config.WEB_PORT}/api/qb/done?hash=%I'
                       + (f'&t={tok}' if tok else '') + '"')
                ui.code(cmd).classes("w-full text-xs")

            _cb_cmd()
            f["QB_CALLBACK_TOKEN"].on_value_change(lambda: _cb_cmd.refresh())   # token 一改，命令即时跟着变
            ui.label("为什么需要它：种子若长期龟速/卡住被降级停跟，又恰好在休眠里下完并被 qB『完成即删种』删掉，"
                     "我们看不到它到 100%，会把它标成『失败』。配了这个回调就能精确兜底标成『已下』。"
                     "不配也行——这种情况很少（多半是剧场版没源慢下），标失败后手动补一下即可。"
                     "仅当 qB 与本程序在同一台机器（回调打 127.0.0.1）时可用。").classes("text-xs text-gray-500")

            _text("QB_URL", "qB 地址", config.QB_URL)
            _text("QB_USERNAME", "qB 用户名", config.QB_USERNAME)
            _password("QB_PASSWORD", "qB 密码（留空=不修改）")
            _text("DOWN_PATH", "下载保存根目录（番剧放这里的『番剧/』下）", config.DOWN_PATH)
            _text("MOVIE_DOWN_PATH", "电影下载目录（留空=用上面根目录的『剧场版/』；填了=放这个独立目录）",
                  config.MOVIE_DOWN_PATH)

            ui.separator()
            ui.label("目录结构").classes("font-bold text-sm")
            _switch("ANIME_SEASON_SUBFOLDER",
                    "番名目录下再建『Season N』二级子目录（关=番剧文件直接放番名目录）",
                    config.ANIME_SEASON_SUBFOLDER)
            ui.label("番剧与剧场版分开归档：下载根 /『番剧』或『剧场版』/ 季度 / 名字 …").classes(
                "text-xs text-gray-500")
            ui.label("开：… / 番剧 / 26C · 7月 · 夏 / 番名 / Season 3 / 番剧.mp4"
                     "　｜　关：… / 番剧 / … / 番名 / 番剧.mp4").classes("text-xs text-gray-500")

            _quarter_setting(f, "QUARTER_FMT", "季度文件夹命名（只控制下载文件夹）",
                             "按季度建下载文件夹时，季度目录名怎么写。", config.QUARTER_FMT)

        with ui.card().classes("w-full"):
            ui.label("面板 / 显示").classes("font-bold")
            _switch("ANIME_SHOW_PENDING", "番剧表里也显示『待确认』的番", config.ANIME_SHOW_PENDING)
            _switch("ANIME_SHOW_REJECTED", "番剧表里也显示『已忽略』的番", config.ANIME_SHOW_REJECTED)
            ui.label("番剧表默认只显示订阅中；上面两项各自决定要不要也带上（它们仍在各自标签页）。").classes(
                "text-xs text-gray-500")
            ui.separator()
            ui.label("分页：一页显示多少年的季度（超出翻页）").classes("font-bold text-sm")
            with ui.row().classes("items-center gap-4 flex-wrap"):
                f["ANIME_PAGE_YEARS"] = ui.number("番剧表 · 年", value=config.ANIME_PAGE_YEARS,
                                                   min=1, max=5, format="%d").classes("w-32")
                f["MOVIE_PAGE_YEARS"] = ui.number("剧场版 · 年", value=config.MOVIE_PAGE_YEARS,
                                                  min=1, max=5, format="%d").classes("w-32")
            ui.label("1 年 = 4 个季度。改完保存，下次进列表即生效。").classes("text-xs text-gray-500")
            _quarter_setting(f, "QUARTER_FMT_UI", "季度显示",
                             "页面上季度怎么显示：番剧表季度标题 / 仪表盘 / 详情。", config.QUARTER_FMT_UI)

        with ui.card().classes("w-full"):
            ui.label("网络 / 通知").classes("font-bold")
            _switch("OPEN_PROXY", "启用代理", config.OPEN_PROXY)
            _text("PROXY_URL", "代理地址", config.PROXY_URL)
            _text("NOTIFY_URL", "通知 URL（空=关闭）", config.NOTIFY_URL)
            _num("WEB_PORT", "Web 端口", config.WEB_PORT)

        async def _save():
            updates = {}
            for key, ctrl in f.items():
                v = ctrl.value
                if key in _PASSWORD:
                    if v is None or str(v).strip() == "":
                        continue  # 留空=不改密码，不把空值写回覆盖
                    updates[key] = str(v).strip()
                elif isinstance(v, bool):
                    updates[key] = "true" if v else "false"
                elif key in _NUMERIC:
                    try:
                        updates[key] = str(int(v))
                    except (ValueError, TypeError):
                        ui.notify(f"{key} 需要是整数，已取消保存", type="negative")
                        return
                else:
                    updates[key] = str(v).strip()
            db_updates = {k: v for k, v in updates.items() if k not in _RESTART_ONLY}
            env_updates = {k: v for k, v in updates.items() if k in _RESTART_ONLY}
            if db_updates:
                sync_was_on = config.QB_SYNC_STATUS   # 捕获切换前旧值（set_many 即时改内存），供下面判 on→off
                qb_was_on = config.QB_ENABLED
                config.set_many(db_updates)   # 写数据库 + 更新内存，即时生效
                # qB 发送开着 → 保存后测一次连接：连不上就自动关掉开关（免得停在『开着却下不了』的迷惑态）
                if config.QB_ENABLED:
                    client = await engine.qb._login()
                    if client is None:
                        config.set_many({"QB_ENABLED": "false"})
                        if "QB_ENABLED" in f:
                            f["QB_ENABLED"].value = False   # 表单开关同步关掉
                        ui.notify("连不上 qB，已自动关闭『发送到 qB』开关（检查地址/端口/账号密码）",
                                  type="warning")
                    else:
                        await client.aclose()
                        engine.qb_kick.set()      # 连上了：立即唤醒同步循环自查，别等一个保底周期
                # 关跟踪/关发送（含上面连不上自动关）→ 落定切换时刻仍在下的旧种子，
                # 否则它们再无路径推进、永久卡『正在下载』、has_inflight 恒真
                if (sync_was_on and not config.QB_SYNC_STATUS) or (qb_was_on and not config.QB_ENABLED):
                    engine.settle_inflight_off()
            if env_updates:
                config.update_env(env_updates)  # WEB_PORT 等结构项仍走 .env
            msg = "已保存，即时生效" + ("（Web 端口改动需重启）" if env_updates else "")
            ui.notify(msg, type="positive")

        async def _reenrich():
            ui.notify("正在重新识别全部…（走 bgm，可能要一会儿）")
            n = await anime.reenrich_all()
            ui.notify(f"重新识别完成：{n} 部命中", type="positive")

        with ui.row().classes("items-center gap-2 mt-2"):
            ui.button("保存", icon="save", on_click=_save).props("color=primary")
            ui.button("重新识别全部", icon="refresh", on_click=_reenrich).props("flat")
