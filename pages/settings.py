"""设置页 `/settings`：读当前配置、改、即时生效。

绝大多数项写进数据库 settings 表并热更新内存（config.set_many），保存即生效、不必重启。
仅 WEB_PORT 这类绑定项仍走 .env（_RESTART_ONLY），改了要重启。数字项做校验，避免写入非数字。
"""
import ipaddress
import json

from nicegui import context, run, ui

from core import anime, engine, netguard, worker
import config
from db import backup
from services import notify
from db.dialect import BINARY_COLLATION
from sources.parse import format_quarter
from .layout import confirm, frame, warn_banner

_NUMERIC = {"BACKUP_INTERVAL_HOURS", "BACKUP_KEEP",
            "NOTIFY_MAX_PER_HOUR", "NOTIFY_BACKLOG_MIN", "ANIME_IDLE_DAYS", "SWEEP_INTERVAL_MIN",
            "ANIME_POLL_INTERVAL", "ANIME_DOWNLOAD_GRACE_MIN", "WEB_PORT", "QB_SYNC_INTERVAL",
            "QB_SYNC_BACKSTOP_MIN", "QB_ACTIVE_FLOOR_KBPS", "QB_SLOW_ROUNDS",
            "QB_IDLE_RECHECK_MIN", "QB_STALL_TIMEOUT_MIN", "QB_ARCHIVE_AFTER_DAYS",
            "ANIME_PAGE_YEARS", "MOVIE_PAGE_YEARS",
            "ENRICH_RETRY_TIMES", "REENRICH_RETRY_BASE", "REENRICH_RETRY_MAX", "REENRICH_MAX_TRIES",
            "ENRICH_TIMEOUT", "NOTIFY_TIMEOUT", "DB_MYSQL_PORT"}
_PASSWORD = {"QB_PASSWORD", "PROXY_PASS", "DB_MYSQL_PASSWORD"}
# 绑监听地址/端口：仍走 .env、改了要重启；其余设置都进 DB 即时生效
_RESTART_ONLY = {"WEB_HOST", "WEB_PORT"}


def _env_values() -> dict:
    """读 .env 的当前值（供 _RESTART_ONLY 那两项渲染用）。文件不在/读不动就回空，调用方自行回落。"""
    try:
        from dotenv import dotenv_values
        return {k: v for k, v in dotenv_values(config.ENV_PATH).items() if v is not None}
    except Exception:
        return {}


def _valid_host(v: str) -> bool:
    """绑定地址：合法 IP（含 0.0.0.0 / ::）或 localhost 才算有效。"""
    if v == "localhost":
        return True
    try:
        ipaddress.ip_address(v)
        return True
    except ValueError:
        return False


def _bad_cidrs(v: str) -> list:
    """返回无法解析的 CIDR 条目（空列表=全合法）。与 netguard._parse 同源规则。"""
    bad = []
    for part in v.split(","):
        part = part.strip()
        if part:
            try:
                ipaddress.ip_network(part, strict=False)
            except ValueError:
                bad.append(part)
    return bad


# 各页标签表【从页面模块直接引用】，不再在这里抄一份——抄一份就得靠人记得同步，
# 而它是"改了也不报错、只是下拉里少一项"的那种静默失配。
from .anime import ANIME_TAB_LABELS   # noqa: E402
from .movies import MOVIE_TAB_LABELS  # noqa: E402

_QUARTER_PRESETS = {
    "{yyyy}": "年份  → 2026",
    "{yy}{q}": "字母  → 26C",
    "{yy}{season}": "季节  → 26夏",
    "{yy}年{m}月": "月份  → 26年7月",
    "{yy}{q} · {m}月 · {season}": "组合  → 26C · 7月 · 夏",
}


def _help(text: str) -> None:
    """标题旁的帮助 ⓘ：点击弹出说明（替代常驻灰色说明文，让页面更清爽）。"""
    with ui.button(icon="help_outline").props("flat round dense size=sm color=grey"):
        with ui.menu():
            ui.label(text).classes("text-xs text-gray-300 p-3").style(
                "max-width:26rem;white-space:pre-line;line-height:1.6")


def _section(title: str, help: str = "") -> None:
    """小节标题；有 help 就在标题右侧放帮助 ⓘ（点击弹出），替代常驻说明文。"""
    with ui.row().classes("items-center gap-1 no-wrap"):
        ui.label(title).classes("font-bold text-sm")
        if help:
            _help(help)


def _quarter_setting(f: dict, key: str, title: str, note: str, value: str,
                     empty_hint: str = "留空＝用默认", tpl_label: str = "命名模板") -> None:
    """模板设置块：标题(带帮助 ⓘ) + 模板输入 + 实时预览 + 预设下拉。控件写入 f[key]。
    empty_hint：模板留空时预览处显示什么（文件夹项=不分类；季度显示项=跟随）。
    tpl_label：模板输入框标签（文件夹命名用『命名模板』；季度显示项传『季度模板』）。"""
    ui.separator()
    _section(title, note + "\n\n占位：{yy}=26  {yyyy}=2026  {q}=C  {season}=夏  {m}=7")
    inp = ui.input(tpl_label, value=value).props("dense outlined").classes("w-full")
    f[key] = inp
    preview = ui.label().classes("text-sm text-blue-400")

    def _prev():
        if not (inp.value or "").strip():
            preview.text = "预览： " + empty_hint
            return
        preview.text = "预览： " + " ／ ".join(
            format_quarter("26" + c, inp.value) for c in "ABCD")

    inp.on_value_change(lambda e: _prev())

    def _pick(e):
        if e.value:
            inp.value = e.value  # 触发 on_value_change 刷新预览
            _prev()

    ui.select(_QUARTER_PRESETS, label="预设（选中填入上面模板，可再手改）",
              on_change=_pick).props("dense outlined").classes("w-full")
    _prev()


# 模块级表单助手：settings() 里那几个同名闭包只在页面函数内可见，_db_panel 在模块级，
# 需要自己的一份。语义与闭包版一致（写进同一个 f 字典，由统一的 _save 收集）。
def _text_into(f: dict, key: str, label: str, val, ph: str = "") -> None:
    f[key] = ui.input(label, value=str(val), placeholder=ph).props("dense outlined").classes("w-full")


def _num_into(f: dict, key: str, label: str, val) -> None:
    f[key] = ui.number(label, value=val, format="%d").props("dense outlined").classes("w-full")


def _pw_into(f: dict, key: str, label: str) -> None:
    f[key] = ui.input(label, value="", password=True).props("dense outlined").classes("w-full")


def _backup_panel(f: dict) -> None:
    """『备份』分栏：开关/间隔/保留份数 + 立刻备份 + 现有备份列表。

    这是全项目唯一"出事就没救"的空白的补丁。刻意做得朴素：只做【备份】不做【恢复】——
    恢复是一次性、要停服、要看清楚的操作，做成一个网页按钮反而危险（点错了就把现在的库盖了）。
    页面上直接给出恢复用的命令，人照着敲一次即可。
    """
    _section("自动备份", "整库快照（VACUUM INTO，不是拷文件——本项目的 SQLite 开着 WAL，"
                         "直接拷主文件会拿到一份看着正常、其实缺最近写入的库）。"
                         "业务库切到 MySQL 时这里只备得到配置，页面会明确标出来。")
    with ui.element("div").classes("field-grid w-full"):
        f["BACKUP_ENABLED"] = ui.switch("开启自动备份", value=config.BACKUP_ENABLED).props("dense")
        _num_into(f, "BACKUP_INTERVAL_HOURS", "间隔（小时）", config.BACKUP_INTERVAL_HOURS)
        _num_into(f, "BACKUP_KEEP", "保留份数（0=不清理）", config.BACKUP_KEEP)

    @ui.refreshable
    def _list():
        items = backup.list_backups()
        if not items:
            ui.label("还没有备份。点右边『立刻备份』或等自动备份到点。").classes(
                "text-xs text-gray-400")
            return
        ui.label(f"共 {len(items)} 份 · 目录 {backup.BACKUP_DIR}").classes("text-xs text-gray-400")
        for d in items[:10]:
            with ui.row().classes("items-center gap-2 w-full no-wrap"):
                ui.badge("配置+业务" if d["scope"] == "full" else "仅配置").props(
                    "color=green" if d["scope"] == "full" else "color=orange")
                ui.label(d["name"]).classes("text-xs text-gray-400 grow break-all min-w-0")
                ui.label(f"{d['bytes'] / 1024:.0f} KB").classes("text-xs text-gray-500")

    async def _now():
        btn.props("loading")
        try:
            res = await run.io_bound(backup.backup_now, config.BACKUP_KEEP)
            ok, why = backup.verify(res["path"])
            _list.refresh()
            ui.notify(f"已备份 {res['bytes'] / 1024:.0f} KB —— {res['note']}"
                      if ok else f"备份文件自检未通过：{why}",
                      type="positive" if ok else "negative")
        except Exception as e:
            ui.notify(f"备份失败：{type(e).__name__}: {str(e)[:160]}", type="negative")
        finally:
            btn.props(remove="loading")

    with ui.row().classes("gap-2 items-center mt-2"):
        btn = ui.button("立刻备份", icon="backup", on_click=_now).props("color=primary unelevated")
        _help("恢复步骤（顺序不能乱）：\n"
              "1. systemctl stop autorss\n"
              "2. 【必须】删掉 data/autorss.db-wal 与 data/autorss.db-shm\n"
              "3. cp data/backups/<选中的那份> data/autorss.db\n"
              "4. systemctl start autorss\n\n"
              "第 2 步不能省：本库开着 WAL，最近的写入躺在 -wal 文件里。只覆盖主文件的话，"
              "SQLite 启动时会把【事故现场那份 -wal】重放到你刚恢复的库上——你拿回的是出事后的数据，"
              "而 `pragma quick_check` 照样返回 ok，每一步验证都是绿的，根本看不出来。\n\n"
              "恢复前建议先 `sqlite3 <备份文件> \'pragma quick_check\'` 确认那份能打开。")
    _list()


def _db_panel(f: dict) -> None:
    """『数据库』分栏：MySQL 连接参数 + 两个互不相同的动作（切换 / 迁移）。

    这两件事最容易被搞混，UI 上必须写死区别：
      · 切换 = 只改连接，一行数据都不动（用于"目标库里已经有数据了"）
      · 迁移 = 复制数据过去，连接不变（搬完想用新库还得再点切换）
    配置本身恒存本地 SQLite，所以 MySQL 连不上时这个面板照样打得开、改得了。
    """
    import db
    from db import transfer
    from db.dialect import driver_missing

    _section("业务数据库",
             "配置（本页所有设置）恒存在本地 SQLite，不随业务数据走——否则 MySQL 连不上时就再也"
             "读不到『该怎么连 MySQL』。可迁到 MySQL 的是业务表：番剧 / 剧场版 / 种子 / 源组 / 番名对照。")

    @ui.refreshable
    def _status():
        with ui.row().classes("items-center gap-2 flex-wrap"):
            ui.label("当前业务数据库：").classes("text-sm")
            ui.badge(db.data_target_desc()).props(
                f"color={'purple' if db.data_backend() == 'mysql' else 'blue-grey'}").classes("text-sm")
            # 【必须兜住】这是页面【构建期】对业务库发的查询。业务库若是运行中掉线的 MySQL，
            # 异常会从构建函数冒出去让 /settings 整页 500——而『切回本地 SQLite』按钮和底部
            # 『保存』都排在本面板之后，用户就再也没有页面入口能自救了，
            # 正好违背本面板 docstring 承诺的"MySQL 连不上时照样打得开、改得了"。
            try:
                cnt = transfer.count_rows(db.engine)
                ui.label(f"（{sum(cnt.values())} 行业务数据）").classes("text-xs text-gray-400")
            except Exception as e:
                ui.label(f"（连不上：{type(e).__name__}）").classes("text-xs text-red-400")
        ui.label(f"配置库（不参与迁移）：本地 SQLite {config.DB_PATH}").classes("text-xs text-gray-500")

    _status()
    if miss := driver_missing():
        warn_banner(miss)

    with ui.element("div").classes("field-grid w-full"):
        _text_into(f, "DB_MYSQL_HOST", "MySQL 地址", config.DB_MYSQL_HOST, "如 10.0.0.230")
        _num_into(f, "DB_MYSQL_PORT", "端口", config.DB_MYSQL_PORT)
        _text_into(f, "DB_MYSQL_USER", "用户名", config.DB_MYSQL_USER)
        _pw_into(f, "DB_MYSQL_PASSWORD", "密码（留空=不修改）")
        _text_into(f, "DB_MYSQL_NAME", "库名", config.DB_MYSQL_NAME, "不存在可用下面的『创建数据库』建")
        _text_into(f, "DB_MYSQL_CHARSET", "字符集", config.DB_MYSQL_CHARSET or "utf8mb4")
    ui.label("连接参数改完要先点页面底部的『保存』才会生效。库可以用下面的按钮直接建"
             "（需要该账号有 CREATE 权限），表由本工具自动建。"
             "字符集保持 utf8mb4，否则日文番名和 emoji 存不进去。").classes("text-xs text-gray-500")

    async def _test():
        url = db.configured_mysql_url()
        if url is None:
            ui.notify("MySQL 地址与库名都要填", type="warning")
            return
        try:
            eng = db.make_mysql_engine(url)
            with eng.connect() as c:
                ver = c.exec_driver_sql("SELECT VERSION()").scalar()
                cnt = transfer.count_rows(eng)
            eng.dispose()
            ui.notify(f"连接成功：MySQL {ver}；该库现有业务数据 {sum(cnt.values())} 行", type="positive")
        except Exception as e:
            # 库不存在是最常见的一种失败，单独认出来给出下一步动作，别让用户对着
            # "Unknown database 'xxx'" 的英文原文猜。用错误码判，不匹配报错文本（会随版本/语言变）。
            if db.mysql_errno(e) == db.MYSQL_ERR_NO_DB:
                ui.notify(f"服务器连上了，但库 `{config.DB_MYSQL_NAME}` 不存在 —— "
                          "点右边的『创建数据库』即可", type="warning")
            else:
                ui.notify(f"连不上：{type(e).__name__}: {str(e)[:160]}", type="negative")

    async def _create_db():
        name = (config.DB_MYSQL_NAME or "").strip()
        host = (config.DB_MYSQL_HOST or "").strip()
        if not host or not name:
            ui.notify("MySQL 地址与库名都要填，并先点页面底部的『保存』", type="warning")
            return
        if not await confirm(
                f"在 {host} 上创建数据库 `{name}`？",
                f"会执行：CREATE DATABASE `{name}` CHARACTER SET "
                f"{config.DB_MYSQL_CHARSET or 'utf8mb4'}"
                # 排序规则必须跟 create_mysql_database 实跑的一致（那边用 BINARY_COLLATION）：
                # 写 _unicode_ci 而实跑 _bin，用户按确认框的文字去核对库属性会对不上。
                # 非 utf8mb4 时那边不带 COLLATE（交给服务端默认），这里也照样不写。
                + (f" COLLATE {BINARY_COLLATION}"
                   if (config.DB_MYSQL_CHARSET or "utf8mb4") == "utf8mb4" else "") + "\n"
                "只建空库、不建表也不写数据（表会在你『切换』过去时自动建）。\n"
                "库已存在则什么都不做。需要该账号有 CREATE 权限。",
                ok_label="创建", ok_icon="add", ok_color="primary"):
            return
        try:
            msg = await run.io_bound(
                db.create_mysql_database, host, config.DB_MYSQL_PORT, config.DB_MYSQL_USER,
                config.DB_MYSQL_PASSWORD, name, config.DB_MYSQL_CHARSET or "utf8mb4")
        except ValueError as e:            # 库名/字符集非法
            ui.notify(str(e), type="negative")
            return
        except Exception as e:
            ui.notify(f"创建失败：{type(e).__name__}: {str(e)[:160]}"
                      "（该账号可能没有 CREATE 权限）", type="negative")
            return
        ui.notify(msg + "。接下来可以点『切到 MySQL』建表，或先『迁移数据』把现有数据搬过去。",
                  type="positive")

    async def _switch_backend(to_mysql: bool):
        """只切连接、不动数据。"""
        url = db.configured_mysql_url() if to_mysql else None
        if to_mysql and url is None:
            ui.notify("MySQL 地址与库名都要填，并先点页面底部的『保存』", type="warning")
            return
        target = "MySQL" if to_mysql else "本地 SQLite"
        if not await confirm(
                f"把业务数据库切到 {target}？",
                "【只改连接，不搬任何数据】。切过去之后你看到的就是那个库里已有的内容——\n"
                "如果目标库是空的，页面上会变成一部番都没有（数据仍在原库、切回来就还在）。\n"
                "想把数据带过去，请先用下面的『迁移数据』。",
                ok_label=f"切到 {target}", ok_icon="swap_horiz", ok_color="primary"):
            return
        try:
            db.switch_data_engine(url)
        except Exception as e:
            if db.mysql_errno(e) == db.MYSQL_ERR_NO_DB:
                ui.notify(f"库 `{config.DB_MYSQL_NAME}` 不存在，已留在原库 —— 先点『创建数据库』",
                          type="warning")
            else:
                ui.notify(f"切换失败，已留在原库：{type(e).__name__}: {str(e)[:160]}", type="negative")
            return
        config.set_many({"DB_BACKEND": "mysql" if to_mysql else "sqlite"})
        # 【新库要补业务初始化】switch_data_engine 只负责建表/升版本。切到一个全新空库后
        # sourcegroup 是空的 → build_sources() 返回空列表 → 采集循环每轮抓 0 个源、
        # 一条日志都不报，用户只会觉得"切完就再也不更新了"，重启进程才恢复。
        # 复位遗留 downloading 的条件严格按 _startup_reset_pending 走（理由见 worker.init_business_state）：
        #   · 常态（运行中切库）标志是 False → 不复位。可能有交付协程正卡在 await，把它的 downloading
        #     占位打回 pending 会当场解除集去重。
        #   · 『启动时库就不可用、用户到这里补全参数才恢复』标志是 True → 必须在这里【就地消费掉】。
        #     否则它一直挂着，而 run_db_watch 的 was_down 也仍是 True，下一次 30 秒心跳探通时会补做
        #     一次复位——那已是系统恢复运行【之后】，采集/UI 都可能有交付在途，正好踩中上面那个坑。
        try:
            worker.init_business_state(reset_leftovers=worker._startup_reset_pending)
        except Exception as e:
            ui.notify(f"已切库，但业务初始化失败（源组可能为空）：{type(e).__name__}: {str(e)[:120]}",
                      type="warning")
        _status.refresh()
        ui.notify(f"已切到 {db.data_target_desc()}（数据未改动）。刷新页面看新库内容。", type="positive")

    async def _migrate(to_mysql: bool):
        """复制数据；连接不变。"""
        url = db.configured_mysql_url()
        if url is None:
            ui.notify("MySQL 地址与库名都要填，并先点页面底部的『保存』", type="warning")
            return
        try:
            other = db.make_mysql_engine(url)
            with other.connect():
                pass
        except Exception as e:
            ui.notify(f"连不上 MySQL：{type(e).__name__}: {str(e)[:160]}", type="negative")
            return
        # 【本地那一端恒取 meta_engine，绝不能用 db.engine】。db.engine 是"当前在用的业务库"——
        # 已经切到 MySQL 之后它就是那个 MySQL，拿它当"本地"会让源和目标指向同一个物理库：
        # 两个不同的 Engine 对象、`is` 判等不出来，于是 overwrite 先把它清空、再从空库读出 0 行，
        # 最后 verify 拿"删完之后"的两边行数比 0==0，弹一句绿色的『迁移完成并校验一致』——
        # 用户的数据就这么没了。meta_engine 恒指向 DB_PATH 那个本地文件，两种后端下都对。
        src, dst = (db.meta_engine, other) if to_mysql else (other, db.meta_engine)
        s_cnt, d_cnt = transfer.count_rows(src), transfer.count_rows(dst)
        arrow = "本地 SQLite → MySQL" if to_mysql else "MySQL → 本地 SQLite"
        # 把两端【具体是哪个库】写进确认框：只写"本地/MySQL"时，用户无从发现方向算错了
        note = (f"{arrow}\n"
                f"源：{db.engine_desc(src)}（{sum(s_cnt.values())} 行）\n"
                f"目标：{db.engine_desc(dst)}（现有 {sum(d_cnt.values())} 行）\n"
                "【只复制业务数据，配置不动】。主键(id)原样保留，跨表关联不会断。\n")
        over = any(d_cnt.values())
        if over:
            note += "⚠️ 目标库【非空】，继续会先清空它的业务表再写入——那些数据将不可恢复。\n"
        # 【目标正是当前在用的业务库】这是最危险的一种：清空+逐批写入是在【线程】里跑的，
        # 而后台采集/同步/页面还在读同一个库，中间态（anime 全在、animetorrent 还空）是可见的，
        # 集去重的 have_eps 会漏判，于是同一集被重下一份。下面用两把轮次锁把采集与扫描挡住，
        # 但页面读取挡不住，所以这里必须把话说清楚。
        live_target = transfer.same_database(dst, db.engine)
        if live_target:
            note += ("⚠️ 目标就是【当前正在使用】的业务库。迁移期间采集与剧场版扫描会被暂停，"
                     "但页面上看到的数据会在清空→写入之间短暂不完整；请迁完再操作。\n")
        note += "迁移完成后连接【仍在原库】，想改用新库请再点『切换』。"
        if not await confirm("开始迁移数据？", note,
                             ok_label="清空目标库并迁移" if over else "开始迁移",
                             ok_icon="content_copy", ok_color="negative" if over else "primary"):
            return
        try:
            # 【拿住两把轮次锁】迁移是"清空目标 → 逐批写入"，而后台采集正在往同一个库写：
            # 复制出的会是撕裂快照（可产生 anime_id 指向不存在父行的孤儿种子，或静默丢行）。
            # 两把锁正是 worker 用来串行化采集轮与剧场版扫描轮的那两把，这里借用它们把两条线挡在门外。
            async with worker._poll_lock, worker._scan_lock:
                res = await run.io_bound(transfer.migrate_data, src, dst, overwrite=over)
            moved = res["moved"]
            # 用【迁前】的源快照校验：现查源库有个自证陷阱——万一两端其实是同一个库，
            # 清空之后现查两边都是 0，反而"校验通过"。
            bad = transfer.verify(src, dst, res["src_before"])
        except Exception as e:
            ui.notify(f"迁移失败：{type(e).__name__}: {str(e)[:200]}", type="negative")
            return
        finally:
            other.dispose()
        detail = "、".join(f"{k} {v}" for k, v in moved.items() if v)
        if bad:
            ui.notify(f"迁移完成但行数对不上：{'；'.join(bad)}", type="warning")
        else:
            ui.notify(f"迁移完成并校验一致：{detail or '（源库为空）'}。"
                      "连接仍在原库，要用新库请点『切换』。", type="positive")
        _status.refresh()

    # 这两个按钮属于【上面那组连接参数】——它们只验证/准备连接，不改变当前用哪个库，
    # 所以放在字段正下方，别混进下面的『切换』分区惹人误会。
    # 代码位置必须在 _test/_create_db 定义之后；这中间没有别的 UI 元素，渲染出来仍紧贴字段。
    with ui.row().classes("gap-2 flex-wrap"):
        ui.button("测试连接", icon="network_check", on_click=_test).props(
            "flat color=primary no-caps").tooltip("只连一下看通不通，不改变当前在用的库")
        ui.button("创建数据库", icon="add", on_click=_create_db).props(
            "flat color=primary no-caps").tooltip(
            "在 MySQL 服务器上建一个空库（CREATE DATABASE，utf8mb4）。只建库不建表，"
            "表在你『切到 MySQL』时自动建。库已存在则什么都不做。")

    ui.separator().classes("my-2")
    ui.label("① 切换数据库（只改连接，不动数据）").classes("text-sm font-bold")
    ui.label("用于目标库里已经有数据、或想先切到空库再迁。切错了再切回来即可，数据不会丢。").classes(
        "text-xs text-gray-500 mb-1")
    with ui.row().classes("gap-2 flex-wrap"):
        ui.button("切到 MySQL", icon="storage", on_click=lambda: _switch_backend(True)).props(
            "color=primary unelevated no-caps")
        ui.button("切回本地 SQLite", icon="undo", on_click=lambda: _switch_backend(False)).props(
            "flat color=grey no-caps")

    ui.separator().classes("my-2")
    ui.label("② 迁移数据（复制数据，连接不变）").classes("text-sm font-bold")
    ui.label("把业务数据整体复制到另一个库，两个方向都支持。保留主键、迁完自动校验行数；"
             "目标库非空时会要你确认清空。迁移期间建议先在上面『番剧』里关掉后台采集，避免边搬边写。").classes(
        "text-xs text-gray-500 mb-1")
    with ui.row().classes("gap-2 flex-wrap"):
        ui.button("本地 SQLite → MySQL", icon="upload", on_click=lambda: _migrate(True)).props(
            "color=primary unelevated no-caps")
        ui.button("MySQL → 本地 SQLite", icon="download", on_click=lambda: _migrate(False)).props(
            "flat color=primary no-caps")


@ui.page("/settings")
def settings():
    with frame("settings") as header_right:
        with header_right:   # 顶栏右侧快捷保存：功能同页面底部『保存』（_save 在下方定义，lambda 延迟到点击时解析）
            ui.button(icon="save", on_click=lambda: _save()).props(
                "flat round dense color=white").tooltip("保存设置（同页面底部的『保存』按钮）")
        ui.label("全局设置").classes("text-2xl font-bold")
        if not config.loaded_from_db:
            warn_banner("配置没能从数据库读出来，下面显示的全部是【硬编码默认值】，不是你设过的。"
                        "此时保存会被拦下（否则会把原有设置整体覆盖掉）。先修好数据库再来。")
        ui.label("保存即时生效、页面刷新可见；仅 Web 绑定地址/端口改动需重启。").classes(
            "text-xs text-gray-400 mb-2")

        f: dict = {}  # 表单控件，key = .env 键名

        def _switch_field(key, label, val):
            f[key] = ui.switch(label, value=val).props("dense")

        def _text(key, label, val, ph=""):
            f[key] = ui.input(label, value=str(val), placeholder=ph).props(
                "dense outlined").classes("w-full")

        def _select(key, label, options, val):
            # 下拉单选：options={键:显示名}，存的是键；当前值不在选项里时回落到第一个键
            v = val if val in options else next(iter(options))
            f[key] = ui.select(options, label=label, value=v).props("dense outlined").classes("w-full")

        def _num(key, label, val, mn=None, mx=None):
            # 数字项：标签在框内浮动，框占满所在栅格格（配合 field-grid 即 1/4 宽）。1/4 窄框放不下的长标签会截断成 …
            kw = {}
            if mn is not None:
                kw["min"] = mn
            if mx is not None:
                kw["max"] = mx
            f[key] = ui.number(label, value=val, format="%d", **kw).props(
                "dense outlined").classes("w-full")

        def _password(key, label):
            f[key] = ui.input(label, value="", password=True).props("dense outlined").classes("w-full")  # 不回填现值

        _sub_ph = ("留空=直接落工作目录；或填相对目录名（如 番剧）" if config.DOWN_PATH
                   else "工作目录未设，此处须填绝对路径")

        # ========== 折叠 ① 通用（默认展开）==========
        with ui.card().classes("w-full"), ui.expansion(
                "通用（站点 / qB / 保存 / 网络 / Web / 高级）", icon="tune", value=True).classes(
                "w-full").props("dense"):
            _section("站点", "显示在顶栏左上角与浏览器标签页标题。保存后刷新页面即变。")
            with ui.element("div").classes("field-grid w-full"):
                _text("SITE_NAME", "站点名", config.SITE_NAME, "空=autorss")

            ui.separator()
            _section("下载 / qBittorrent",
                     "开=跟 qB 实时进度：交付即跟、活跃时高频轮询、有未完成但不活跃时按『中档自查』兜、全下完休眠；"
                     "『慢速地板+判慢轮次』判定是否还在真下。关=发送即当『已下』、完全不查 qB。"
                     "『停滞超时』：进度连续这么久无推进→标『停滞(异常)』供人工处理（不自动换源）。"
                     "『完成归档』：完成超这么多天→从 qB 移除【留文件】、标『已归档』、不再跟踪。\n"
                     "它依赖上面的『读取 qB 实时状态』——关掉跟踪时我们不知道种子有没有真下完，归档会整个停用（否则会把还在下的种子从 qB 摘掉、只留半成品）。")
            _switch_field("QB_ENABLED", "发送种子到 qB（关=只采集不下载）", config.QB_ENABLED)
            _switch_field("QB_SYNC_STATUS", "读取 qB 实时状态（关=发送过去即『已下』，完全不轮询 qB）",
                    config.QB_SYNC_STATUS)
            with ui.element("div").classes("field-grid w-full"):
                _num("QB_SYNC_INTERVAL", "活跃轮询间隔（秒）", config.QB_SYNC_INTERVAL)
                _num("QB_IDLE_RECHECK_MIN", "中档自查间隔（分钟）", config.QB_IDLE_RECHECK_MIN)
                _num("QB_SYNC_BACKSTOP_MIN", "保底自查间隔（分钟）", config.QB_SYNC_BACKSTOP_MIN)
                _num("QB_ACTIVE_FLOOR_KBPS", "慢速地板（KB/s）", config.QB_ACTIVE_FLOOR_KBPS)
                _num("QB_SLOW_ROUNDS", "判慢轮次", config.QB_SLOW_ROUNDS)
                _num("QB_STALL_TIMEOUT_MIN", "停滞超时（分钟，0=关）", config.QB_STALL_TIMEOUT_MIN)
                _num("QB_ARCHIVE_AFTER_DAYS", "完成归档（天，0=关）", config.QB_ARCHIVE_AFTER_DAYS)

            ui.separator()
            with ui.element("div").classes(   # qB 连接 与 完成回调 左右并排两列，窄屏自动堆叠
                    "grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-2 w-full items-start"):
                with ui.column().classes("gap-2 min-w-0"):   # 左列：qB 连接
                    _section("qB 连接")
                    _text("QB_URL", "qB 地址", config.QB_URL)
                    _text("QB_USERNAME", "qB 用户名", config.QB_USERNAME)
                    _password("QB_PASSWORD", "qB 密码（留空=不修改）")
                with ui.column().classes("gap-2 min-w-0"):   # 右列：完成回调（可选兜底）
                    _section("完成回调（可选·精确兜底）",
                             "可选兜底：慢速种子在休眠期间下完、又被 qB『完成即删种』删掉，会被误标『失败』；"
                             "配了它就精确标『已下』。不配也行（少见）。仅 qB 与本程序同机时可用。")
                    _text("QB_CALLBACK_TOKEN", "回调 token（可选，防乱调；填了命令会自动带 &t=）",
                          config.QB_CALLBACK_TOKEN)

                    @ui.refreshable
                    def _cb_cmd():
                        tok = (f["QB_CALLBACK_TOKEN"].value or "").strip()   # 读输入框实时值，不是已保存值
                        cmd = (f'curl -s -X POST "http://127.0.0.1:{config.WEB_PORT}/api/qb/done?hash=%I'
                               + (f'&t={tok}' if tok else '') + '"')

                        async def _copy(c=cmd):
                            # ui.clipboard.write 是【单向】的 run_javascript：非安全上下文（局域网
                            # http:// 而非 https/localhost）里 navigator.clipboard 压根不存在，浏览器
                            # 只在 console 里报一句，Python 侧收不到任何失败信号——于是恒弹绿色『已复制』
                            # 而剪贴板里什么都没有。改成自己发一段【带回执】的 JS，按真实结果提示；
                            # 复制不了也不是死路：左边那个只读输入框本来就能手动全选复制。
                            try:
                                ok = await ui.run_javascript(
                                    "(async () => { try { await navigator.clipboard.writeText("
                                    + json.dumps(c) + "); return true } catch (e) { return false } })()",
                                    timeout=3.0)
                            except Exception:      # 客户端没回应/已断开
                                ok = False
                            if ok:
                                ui.notify("已复制命令到剪贴板", type="positive")
                            else:
                                ui.notify("浏览器不允许自动复制（局域网 http 属非安全上下文）—— "
                                          "请手动全选左边那行命令复制", type="warning")

                        with ui.row().classes("items-center gap-2 w-full no-wrap"):
                            ui.input(value=cmd).props("dense outlined readonly").classes(
                                "grow font-mono min-w-0").style("font-size:12px")
                            ui.button(icon="content_copy", on_click=_copy).props(
                                "flat round dense color=primary").tooltip("复制命令")

                    _cb_cmd()
                    f["QB_CALLBACK_TOKEN"].on_value_change(lambda: _cb_cmd.refresh())   # token 改则命令跟着变
                    ui.label("↑ 复制这行填进 qB → Options → Downloads →『Run external program on torrent "
                             "finished』（%I=种子 hash）").classes("text-xs text-gray-500")

            ui.separator()
            _section("保存 & 命名",
                     "有工作目录时，番剧/剧场版目录按【相对】拼在它下面：留空=直接落工作目录（不额外分类），"
                     "填相对名（如 番剧 / 剧场版）则各建子目录。没设工作目录时，番剧/剧场版须各填【绝对】路径（可不同盘）。"
                     "两侧都空又无工作目录=无处下载、保存拦下。")
            _text("DOWN_PATH", "工作目录（下载根）", config.DOWN_PATH)
            _text("ANIME_DOWN_PATH", "番剧下载目录", config.ANIME_DOWN_PATH, _sub_ph)
            _text("MOVIE_DOWN_PATH", "剧场版下载目录", config.MOVIE_DOWN_PATH, _sub_ph)
            if config.QB_ENABLED and not engine.qb_is_local():
                warn_banner("qB 在远程主机（非 127.0.0.1）：以上路径是【qB 主机上】的绝对路径，不是本机路径。"
                            "本机不会真的建这些目录，由 qB 在它那侧建/写；请确保该路径在 qB 主机上存在且可写。")
            _quarter_setting(f, "QUARTER_FMT_UI", "季度显示",
                             "页面上季度怎么显示：番剧表季度标题 / 仪表盘 / 详情。留空＝跟随番剧的下载文件夹命名。",
                             config.QUARTER_FMT_UI, empty_hint="留空＝跟随番剧下载文件夹命名", tpl_label="季度模板")

            ui.separator()
            _section("网络 / 通知",
                     "代理支持 http:// / https://；socks5:// 需另装 socksio 包（未装时填 socks5:// 会在请求时出错）。"
                     "代理账号/密码仅『需认证的代理』才填，留空=不认证。通知 URL：留空=关闭推送。")
            _switch_field("OPEN_PROXY", "启用代理", config.OPEN_PROXY)
            with ui.element("div").classes("field-grid w-full"):
                _text("PROXY_URL", "代理地址", config.PROXY_URL, "http://… 或 https://…（socks5 需装 socksio）")
                f["PROXY_URL"].classes(add="col-span-2")   # 代理地址占 1/2（4 列栅格里跨 2 格）
                _text("PROXY_USER", "代理账号", config.PROXY_USER, "留空=不认证")
                _password("PROXY_PASS", "代理密码（留空=不改）")
            _text("NOTIFY_URL", "通知 URL（空=关闭）", config.NOTIFY_URL)
            _section("通知事件",
                     "勾了哪些就只收哪些。【留空＝一条都不发】——注意这与『订阅源』页上"
                     "『留空＝不限』（字幕组白名单、标题关键词）的含义相反。\n"
                     "『qB 连不上』『数据库停摆』『待识别积压』是状态型：只在状态【翻转】的那一刻"
                     "各发一条（进一条、出一条），不会每轮重复轰炸。\n"
                     "『下载失败』『停滞异常』由后台巡检合并上报，同样的条数 6 小时内只说一次。")
            f["NOTIFY_EVENTS"] = ui.select(
                {k: f"{icon} {cn}" for k, (cn, icon) in notify.EVENTS.items()},
                value=list(config.NOTIFY_EVENTS or []), multiple=True,
                label="推送这些事件").props("dense outlined use-chips").classes("w-full")
            with ui.element("div").classes("field-grid w-full"):
                _num("NOTIFY_MAX_PER_HOUR", "每小时最多几条（0=不限）", config.NOTIFY_MAX_PER_HOUR)
                _num("NOTIFY_BACKLOG_MIN", "待识别积压到几部才提醒", config.NOTIFY_BACKLOG_MIN)

            ui.separator()
            _section("完结 / 断更巡检",
                     "完结判定：bgm 给出总集数、且 1~总集数【每一集都在手】才算完结（有集号歧义段的番一律不判）。\n"
                     "『判完结后停止自动下新集』默认关——判据虽保守，但 bgm 的总集数少记一集是真实存在的，"
                     "那会让最后一集永远下不下来。建议先让它跑一阵、看『已完结』标得对不对再开。\n"
                     "断更提醒：一部追番中的番多少天没有新种子就说一声——那是发现『源失效/字幕组停更』的"
                     "唯一自动手段（那类故障不报错，表现只是『好几天没更新了』）。")
            with ui.element("div").classes("field-grid w-full"):
                f["ANIME_FINISH_ENABLED"] = ui.switch(
                    "判定完结（只标记+通知）", value=config.ANIME_FINISH_ENABLED).props("dense")
                f["ANIME_FINISH_UNSUB"] = ui.switch(
                    "判完结后停止自动下新集", value=config.ANIME_FINISH_UNSUB).props("dense")
                _num("ANIME_IDLE_DAYS", "断更提醒阈值（天，0=关）", config.ANIME_IDLE_DAYS)
                # 【给下限】这一项没有"0=关"的语义（关完结/断更各有自己的开关），
                # 而 run_sweep 里是 max(60, ...) 秒——填 0 会变成每 60 秒来一次全表扫描。
                _num("SWEEP_INTERVAL_MIN", "巡检间隔（分钟，最少 5）", config.SWEEP_INTERVAL_MIN, 5, 1440)

            ui.separator()
            _section("Web 访问",
                     "绑定地址：127.0.0.1=仅本机；0.0.0.0=整个局域网可访问。改绑定地址/端口写 .env、需重启；"
                     "非法地址保存时会被拦下，留空=回落 127.0.0.1。")
            # 【这两项要读 .env 的当前值，不能读内存】它们是 _RESTART_ONLY：保存只写 .env，
            # 而 config.WEB_HOST/WEB_PORT 是进程启动时读的、代表"现在实际绑着的"，不会跟着变。
            # 若表单渲染内存值：改成 8081 保存 → .env=8081、内存仍 8080 → 页面重新渲染后框里又是 8080
            # → 再点一次保存就把 .env 悄悄写回 8080，用户的改动【静默丢失】。
            # 反过来也不能把 .env 的值灌回 config：那会让 config.WEB_PORT 从"实际绑着的端口"
            # 变成"重启后才生效的端口"，而上面那条 qB 完成回调命令正是拿它拼的，
            # 改完没重启就会给出一条指向【没人监听的端口】的命令。两个语义必须分开。
            # 【.env 里的值必须先过校验再往框里放】否则会把用户锁死：_save 对绑定地址只认 IP/localhost，
            # 而 .env 里完全可能是主机名（uvicorn 收得下 'nas.local'）。框里一旦渲染出这种值，
            # 保存时校验不过 →『已取消保存』→ 这一页【任何】设置都再也存不下去，
            # 而用户根本想不到是绑定地址那一栏在作梗。校验不过就回落到运行值，并在下面单独提示。
            _env_now = _env_values()
            _env_raw_host = _env_now.get("WEB_HOST") or ""
            _env_host = _env_raw_host if _valid_host(_env_raw_host) else config.WEB_HOST
            _bad_env_host = bool(_env_raw_host) and not _valid_host(_env_raw_host)
            try:
                _env_port = int(_env_now.get("WEB_PORT") or config.WEB_PORT)
                if not 1 <= _env_port <= 65535:
                    raise ValueError
            except (TypeError, ValueError):     # .env 被手改成非数字/越界：回落到运行值，别让整页 500
                _env_port = config.WEB_PORT
            with ui.element("div").classes("field-grid w-full"):
                _text("WEB_HOST", "绑定地址", _env_host)
                _num("WEB_PORT", "Web 端口", _env_port)
                _text("WEB_ALLOW_CIDRS", "允许网段(CIDR)", config.WEB_ALLOW_CIDRS)
            if _bad_env_host:
                warn_banner(f".env 里的 WEB_HOST 是 {_env_raw_host!r}，本页只接受 IP 或 localhost，"
                            f"已用当前实际绑定值 {config.WEB_HOST} 填入。"
                            "直接保存会把 .env 里那个值覆盖掉；要保留它请手改 .env、别在这里存。")
            elif (_env_host, _env_port) != (config.WEB_HOST, config.WEB_PORT):
                warn_banner(f"这两项已改成 {_env_host}:{_env_port}，但当前进程仍绑着 "
                            f"{config.WEB_HOST}:{config.WEB_PORT} —— 重启后才生效。")
            warn_banner("本工具无鉴权、本页含 qB 密码。绑 0.0.0.0 时用『允许网段』把访问限定在可信内网（如 "
                        "192.168.1.0/24，多个用逗号），即时生效、留空=不限制。本机恒放行；若新网段会把你当前访问挡在门外，"
                        "保存时会被拦下。经反向代理时对端是代理 IP，此项应留空、鉴权交给代理。")

            ui.separator()
            _section("高级（超时 / 站点地址 · 一般不用动）",
                     "一般不用改。超时：网络慢可调大。站点地址：换镜像时才改，改错会导致识别/抓取全挂，结尾别带 /。")
            with ui.element("div").classes("field-grid w-full"):
                _num("ENRICH_TIMEOUT", "Bangumi 请求超时（秒）", config.ENRICH_TIMEOUT)
                _num("NOTIFY_TIMEOUT", "通知推送超时（秒）", config.NOTIFY_TIMEOUT)
            _text("MIKAN_BASE", "Mikan 站点根地址", config.MIKAN_BASE)
            _text("BGM_API", "Bangumi API 根地址", config.BGM_API)

        # ========== 折叠 ② 番剧 ==========
        with ui.card().classes("w-full"), ui.expansion(
                "番剧", icon="movie", value=True).classes("w-full").props("dense"):
            _section("采集",
                     "Bangumi 识别恒开：规范名/季度/日文名统一取自 bgm。源组（feed/策略/优先级/字幕组）在『订阅源』tab 配置。")
            _switch_field("ANIME_POLL_ENABLED", "启用后台采集（关=暂停抓取；首次配置好前可先关着）",
                    config.ANIME_POLL_ENABLED)
            with ui.element("div").classes("field-grid w-full"):
                _num("ANIME_POLL_INTERVAL", "轮询间隔（秒）", config.ANIME_POLL_INTERVAL)
                _num("ANIME_DOWNLOAD_GRACE_MIN", "下载缓冲窗口（分钟，多源等偏好组补齐）",
                     config.ANIME_DOWNLOAD_GRACE_MIN)
            _switch_field("ANIME_TOP_PRIORITY_INSTANT", "最高优先级组入库即下（跳过缓冲窗口）",
                    config.ANIME_TOP_PRIORITY_INSTANT)
            _switch_field("ANIME_MULTIBRACKET_PARSE",
                    "多括号命名回退捕获（识别 [组][番名][集] 格式）",
                    config.ANIME_MULTIBRACKET_PARSE)
            ui.label("默认关：认不出番名的种子直接进『待识别』。开=尝试从括号块猜名（可能猜错，拿不准自动跳过；"
                     "大组不受影响），可在『解析测试』页验证。").classes("text-xs text-gray-500")

            ui.separator()
            _section("开始使用日 · 老番过滤",
                     "排除开播早于此日的老番、不自动下（种子照常入库，『已忽略』页可看/恢复）。"
                     "新入库的老番建库时即自动判超期忽略（不分自动源/待确认源）；对【已有】的番不自动动，需点右侧『应用』——"
                     "它会先存下这个日期、再按它重算【含当前正在追(已确认)的老番也会被判超期忽略】。\n\n"
                     "反悔：把开始日清空（或调很早）再点『应用』，就把超期忽略的番全放回待确认。")

            async def _apply_filter():   # 应用：先存下输入框里的开始日（免得还得先去点下面『保存』），再按它重算
                sd = (f["ANIME_START_DATE"].value or "").strip()
                if sd and anime._parse_date(sd) is None:   # 空=不限（合法）；非空则须能解析
                    ui.notify("开始使用日格式不对（应为 YYYY-MM-DD，如 2026-07-01）", type="negative")
                    return
                # 不拦空开始日：空=没有番算超期→apply_start_date_filter 会把所有超期忽略放回待确认（=释放/反悔）
                if not await confirm("保存并应用开始使用日？",
                                     "先把这个开始日存下，再把『开播早于它』的番都判为超期忽略、停止自动下载——【包括当前正在追(已确认)的老番】。"
                                     "若把开始日改早/清空后再点，则相反：把进入范围的超期忽略放回待确认。想单独保留哪部，之后去『已忽略』页恢复。",
                                     ok_label="保存并应用", ok_icon="filter_alt", ok_color="primary"):
                    return
                config.set_many({"ANIME_START_DATE": sd})   # 存 DB + 热更内存，下面重算即读它
                n = anime.apply_start_date_filter() + anime.ignore_confirmed_before_start()  # 待确认↔超期 + 追番中→超期
                ui.notify(f"已保存并应用：{n} 部番状态变更" if n else "已保存；没有需要变更的番", type="positive")

            with ui.row().classes("items-stretch gap-3"):   # 日期框 + 应用按钮同一行、等高，按钮在右
                _text("ANIME_START_DATE", "开始使用日", config.ANIME_START_DATE, "YYYY-MM-DD，空=不限")
                f["ANIME_START_DATE"].classes(remove="w-full", add="w-56")   # 收窄到定宽，给右侧按钮腾位
                ui.button("应用开始使用日过滤", icon="filter_alt", on_click=_apply_filter).props(
                    "color=primary unelevated no-caps").classes("text-xs")   # 不加 size=sm/dense → 随行拉伸到与输入框等高

            ui.separator()
            _section("Bangumi 重试（识别不到时）",
                     "认不到 bgm 的番进『待识别』：先即时重试挡抖动，再指数退避后台重试（每失败翻倍、封顶 24h），"
                     "满次数就停、留手动（详情页『重新识别』清零重来）。查到 bgm 自动升『待确认』。")
            with ui.element("div").classes("field-grid w-full"):
                _num("ENRICH_RETRY_TIMES", "即时重试次数（bgm 请求超时/连接错时）", config.ENRICH_RETRY_TIMES)
                _num("REENRICH_RETRY_BASE", "延迟重试基准等待（分钟，失败后翻倍）", config.REENRICH_RETRY_BASE)
                _num("REENRICH_RETRY_MAX", "延迟重试等待上限（分钟，翻倍封顶）", config.REENRICH_RETRY_MAX)
                _num("REENRICH_MAX_TRIES", "每番最多重试几次", config.REENRICH_MAX_TRIES)

            ui.separator()
            _section("归档")
            _switch_field("ANIME_SEASON_SUBFOLDER",
                    "番名目录下再建『Season N』二级子目录（关=番剧文件直接放番名目录）",
                    config.ANIME_SEASON_SUBFOLDER)
            ui.label("开：… / 番剧 / 26C · 7月 · 夏 / 番名 / Season 3 / 番剧.mp4"
                     "　｜　关：… / 番剧 / … / 番名 / 番剧.mp4").classes("text-xs text-gray-500")
            _quarter_setting(f, "QUARTER_FMT", "下载文件夹命名（默认按季度）",
                             "番剧按季度建下载文件夹时，季度目录名怎么写；留空＝不建季度目录、直接放番名。",
                             config.QUARTER_FMT, empty_hint="留空＝不建季度目录，直接 …/番剧/番名/")

            ui.separator()
            _section("番剧表显示",
                     "番剧表默认只显示订阅中，上两项决定要不要带上『待确认/已忽略』。默认标签页=进番剧页先落哪个标签"
                     "（地址带 ?t= 时以其为准）。分页：1 年=4 个季度。")
            _switch_field("ANIME_SHOW_PENDING", "番剧表里也显示『待确认』的番", config.ANIME_SHOW_PENDING)
            _switch_field("ANIME_SHOW_REJECTED", "番剧表里也显示『已忽略』的番", config.ANIME_SHOW_REJECTED)
            with ui.element("div").classes("field-grid w-full"):
                _select("ANIME_DEFAULT_TAB", "默认标签页", ANIME_TAB_LABELS, config.ANIME_DEFAULT_TAB)
                _num("ANIME_PAGE_YEARS", "分页 · 每页年数", config.ANIME_PAGE_YEARS, 1, 5)

        # ========== 折叠 ③ 剧场版 ==========
        with ui.card().classes("w-full"), ui.expansion(
                "剧场版", icon="theaters", value=True).classes("w-full").props("dense"):
            _section("列表显示",
                     "默认标签页=进剧场版页先落哪个标签。分页：1 年=4 个季度。"
                     "自动扫描开关/间隔在『剧场版页 → 订阅源』里。")
            with ui.element("div").classes("field-grid w-full"):
                _select("MOVIE_DEFAULT_TAB", "默认标签页", MOVIE_TAB_LABELS, config.MOVIE_DEFAULT_TAB)
                _num("MOVIE_PAGE_YEARS", "分页 · 每页年数", config.MOVIE_PAGE_YEARS, 1, 5)
            _quarter_setting(f, "MOVIE_QUARTER_FMT", "下载文件夹命名（默认按年份）",
                             "剧场版按此建下载文件夹（默认年份，如 2026；同年归一个文件夹）；留空＝不分类、直接放片名。",
                             config.MOVIE_QUARTER_FMT, empty_hint="留空＝不建年份目录，直接 …/片名/")

        # ========== 折叠 ④ 数据库 ==========
        with ui.card().classes("w-full"), ui.expansion(
                "数据库", icon="storage", value=False).classes("w-full").props("dense"):
            _db_panel(f)

        # ========== 折叠 ⑤ 备份 ==========
        with ui.card().classes("w-full"), ui.expansion(
                "备份", icon="backup", value=False).classes("w-full").props("dense"):
            _backup_panel(f)

        async def _save():
            if not config.loaded_from_db:
                # 【配置根本没从库里读出来时，绝不能保存】此刻表单上显示的每一个值都是【硬编码默认值】，
                # 不是用户设过的。一按保存就会把库里全部已有配置改写成默认——其中
                # WEB_ALLOW_CIDRS 会被清空，而空串在 netguard 里的含义是【放行一切】：
                # 一个无鉴权、存着 qB 密码的面板重新对整个局域网敞开，NOTIFY_URL 的推送密钥也被抹掉，
                # 而全程零报错、还弹一句绿色的"已保存"。
                ui.notify("数据库没读出来，当前显示的是默认值——现在保存会覆盖掉你原有的全部设置，已拦下。"
                          "请先修好数据库（journalctl -u autorss -n 50）", type="negative")
                return
            updates = {}
            for key, ctrl in f.items():
                v = ctrl.value
                if key in _PASSWORD:
                    if v is None or str(v).strip() == "":
                        continue  # 留空=不改密码，不把空值写回覆盖
                    updates[key] = str(v).strip()
                elif isinstance(v, bool):
                    updates[key] = "true" if v else "false"
                elif isinstance(v, (list, tuple, set)):
                    # 【list 型配置】config 那边用逗号分隔的字符串存（见 config._to_raw），
                    # NOTIFY_EVENTS 是第一个走这条路的表单项。不加这一支的话，
                    # str(v) 会把整个 Python 列表字面量（含中括号和引号）原样写进库。
                    updates[key] = ",".join(str(x).strip() for x in v if str(x).strip())
                elif key in _NUMERIC:
                    try:
                        updates[key] = str(int(v))
                    except (ValueError, TypeError):
                        ui.notify(f"{key} 需要是整数，已取消保存", type="negative")
                        return
                else:
                    updates[key] = str(v).strip()
            # 保存前校验绑定项：非法值提前拦下，别写进 .env/库导致启动失败或静默锁死
            port = updates.get("WEB_PORT")
            if port is not None and not (1 <= int(port) <= 65535):
                ui.notify("Web 端口需在 1~65535 之间，已取消保存", type="negative")
                return
            host = updates.get("WEB_HOST", "")
            if host and not _valid_host(host):
                ui.notify(f"绑定地址 {host!r} 不是合法 IP（如 127.0.0.1 / 0.0.0.0），已取消保存",
                          type="negative")
                return
            bad = _bad_cidrs(updates.get("WEB_ALLOW_CIDRS", ""))
            if bad:
                ui.notify(f"允许网段无法解析：{', '.join(bad)}（示例 192.168.1.0/24），已取消保存",
                          type="negative")
                return
            # 存前自锁检测：设了网段限制时，拿不到你的 IP 或你的 IP 不在网段内 → 一律拦下保存
            # （fail-closed，别让人把自己锁死；回环恒放行，本机保存不受影响。空网段=不限制、无自锁风险）
            new_cidrs = updates.get("WEB_ALLOW_CIDRS", "").strip()
            if new_cidrs:
                try:
                    my_ip = context.client.ip
                except Exception:
                    my_ip = ""
                if not (my_ip and netguard.not_blocked_by(my_ip, new_cidrs)):
                    where = f"你正从 {my_ip} 访问，该地址不在" if my_ip else "无法确认你当前访问 IP 是否在"
                    ui.notify(f"{where}要保存的允许网段内——保存后可能把你自己挡在门外，已取消保存。"
                              f"请把你所在网段一并加入（或留空=不限制）。", type="negative")
                    return
            # 路径防呆：每侧有效根 =(该侧目录 or 工作目录)不能为空，否则无处下载
            work = updates.get("DOWN_PATH", "")
            for side, key in (("番剧", "ANIME_DOWN_PATH"), ("剧场版", "MOVIE_DOWN_PATH")):
                if not (updates.get(key, "") or work):
                    ui.notify(f"{side}下载目录与工作目录都为空——无处下载。请填工作目录，或填这一侧的绝对路径。",
                              type="negative")
                    return
            sd = updates.get("ANIME_START_DATE", "").strip()
            if sd and anime._parse_date(sd) is None:
                ui.notify("开始使用日格式不对（应为 YYYY-MM-DD，如 2026-07-01），已取消保存", type="negative")
                return
            db_updates = {k: v for k, v in updates.items() if k not in _RESTART_ONLY}
            env_updates = {k: v for k, v in updates.items() if k in _RESTART_ONLY}
            if db_updates:
                sync_was_on = config.QB_SYNC_STATUS   # 捕获切换前旧值（set_many 即时改内存），供下面判 on→off
                qb_was_on = config.QB_ENABLED
                config.set_many(db_updates)   # 写数据库 + 更新内存，即时生效
                # 通知的冷却/状态记忆是【进程内】的：不清的话，用户刚把某个事件勾上，
                # 还要等冷却过期或状态再翻转一次才看得到效果——那看起来就像"设置没生效"。
                notify.reset_state()
                # 注：改开始使用日【不】自动重算已有番——由用户在设置里点『应用』按钮显式触发（更可控）
                # qB 发送开着 → 保存后测一次连接：连不上就自动关掉开关（免得停在『开着却下不了』的迷惑态）
                probe_failed = False
                if config.QB_ENABLED:
                    # 【先解除登录冷却】用户刚改完账号密码，不该还被自己上一次失败的负缓存挡着——
                    # 那会让"密码明明改对了"的这次保存照样报连不上，然后把开关自动关掉。
                    # 冷却是为了别把 qB 的失败登录封禁计数撞满，不是惩罚用户的显式操作。
                    await engine.qb.reset_cooldown()
                    client = await engine.qb._login()
                    if client is None:
                        probe_failed = True
                        config.set_many({"QB_ENABLED": "false"})
                        if "QB_ENABLED" in f:
                            f["QB_ENABLED"].value = False   # 表单开关同步关掉
                        ui.notify("连不上 qB，已自动关闭『发送到 qB』开关（检查地址/端口/账号密码）",
                                  type="warning")
                    else:
                        await client.aclose()
                        engine.qb_kick.set()      # 连上了：立即唤醒同步循环自查，别等一个保底周期
                # 关跟踪/关发送 → 落定切换时刻仍在下的旧种子，
                # 否则它们再无路径推进、永久卡『正在下载』、has_inflight 恒真。
                #
                # 【只对用户显式关闭落定，探测失败自动关的不落定】(probe_failed)：
                # 收表单收的是【全量】而不是差量，所以改个站点名/轮询间隔都会走上面那次 qB 探测；
                # 而那是一次性单发 _login()，不复用会话、不重试、任何失败（网络抖动/qB 正在重启）
                # 都塌缩成 None。据此把在下种子写成 status=sent + qb_progress=1.0 后，它们永久掉出
                # _inflight_where，把开关重新打开 sync 也不会再拉它们，UI 从此谎报 100% 已完成。
                # 对照 sync_qb_status 对同样的失败信号是"本轮不动、不改任何状态"——两条路径不该互相打架。
                # 探测失败只是把开关关掉+弹警告，等用户看到提示自己处理；真要落定就由他显式关一次。
                explicit_off = ((sync_was_on and not config.QB_SYNC_STATUS)
                                or (qb_was_on and not config.QB_ENABLED and not probe_failed))
                if explicit_off:
                    engine.settle_inflight_off()
            if env_updates:
                config.update_env(env_updates)  # WEB_PORT 等结构项仍走 .env
            msg = "已保存，即时生效" + ("（Web 绑定地址/端口改动需重启）" if env_updates else "")
            ui.notify(msg, type="positive")

        _reactivate_busy = {"v": False}

        async def _reactivate():
            if _reactivate_busy["v"]:
                return                       # 防抖：跑着时连点直接忽略，别叠多轮并发抓源
            if not await confirm("重新激活全部任务？",
                                 "立刻抓一遍所有源、把到点的下载发往 qB、按需扫剧场版，并唤醒 qB 状态检查"
                                 "（约等于重启一次服务，但不重启进程）。源多时要一会儿。",
                                 ok_color="primary"):
                return
            _reactivate_busy["v"] = True
            reactivate_btn.props("loading")
            try:
                # 结果文案与成败都由 worker 出：它才知道这一轮到底做没做（库停摆/已有一轮在跑都会跳过），
                # 跳过时必须用警告色——否则『其实什么都没做』会被显示成绿色的成功。
                ok, msg = await worker.run_all_once()
                ui.notify(msg, type="positive" if ok else "warning")
            except Exception as e:
                ui.notify(f"重新激活异常：{e}", type="negative")
            finally:
                _reactivate_busy["v"] = False
                reactivate_btn.props(remove="loading")

        with ui.row().classes("items-center gap-2 mt-2"):
            ui.button("保存", icon="save", on_click=_save).props("color=primary unelevated")
            reactivate_btn = ui.button("重新激活全部任务", icon="restart_alt",
                                       on_click=_reactivate).props("flat")
