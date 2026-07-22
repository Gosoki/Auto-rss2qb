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
            "QB_SYNC_BACKSTOP_MIN", "ANIME_PAGE_YEARS", "MOVIE_PAGE_YEARS"}
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
    preview = ui.label().classes("text-sm text-blue-300")

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
            _num("QB_SYNC_INTERVAL", "qB 活跃轮询间隔（秒）——仅在有种子正在下时按此频率拉进度",
                 config.QB_SYNC_INTERVAL)
            _num("QB_SYNC_BACKSTOP_MIN", "qB 保底自查间隔（分钟）——没被唤醒也每隔这么久兜底扫一次",
                 config.QB_SYNC_BACKSTOP_MIN)
            ui.label("同步是事件驱动：种子交给 qB 时立刻开始跟、按上面的活跃间隔拉进度，全下完就休眠、"
                     "不再打扰 qB。下完/做种/文件缺失的种子不再轮询；保底间隔是兜底自查（默认 180=3 小时）。").classes(
                "text-xs text-gray-500")
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

        def _save():
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
                config.set_many(db_updates)   # 写数据库 + 更新内存，即时生效
                if updates.get("QB_ENABLED") == "true":
                    engine.qb_kick.set()      # qB 刚被打开：立即唤醒同步循环自查，别等一个保底周期
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
