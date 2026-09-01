"""配置：默认值硬编码在 _SPEC；建库时写进数据库 settings 表，之后以数据库为唯一来源。

读取一律走 `config.<KEY>`（经模块 __getattr__ 返回当前值），别再 `from config import KEY`
（那样会在导入时绑死快照，改了不生效）。设置页保存 → 写库 + 更新内存 → 即时生效。
例外：DB_PATH（开库前提）、WEB_PORT（绑端口）本质上就得重启，走 .env/硬编码默认，不进 settings 表。
"""
import ipaddress
import logging
import os
import re
import tempfile
import threading
from pathlib import Path

log = logging.getLogger("autorss")

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
_env_lock = threading.Lock()

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH)
except Exception:
    pass

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
try:
    # 0700：库里存着 qB 明文密码与 bgm token，默认的 0755/0644 意味着同机任何账号都能读走。
    # 每次启动都收一遍（不只是新建时），好让老库也能被一次升级修好。失败不致命——
    # 挂载点是 NTFS/exFAT 或跑在非 owner 身份下时 chmod 会抛，那种环境本来也谈不上文件权限。
    DATA_DIR.chmod(0o700)
except OSError:
    pass

# ---- 结构性/绑定项：走 .env，改了需重启（DB_PATH 是启动 DB 的前提，WEB_HOST/WEB_PORT 绑监听）----
DB_PATH = os.getenv("DB_PATH", str(DATA_DIR / "autorss.db"))
_WEB_PORT_DEFAULT = 2333
try:
    WEB_PORT = int(os.getenv("WEB_PORT", str(_WEB_PORT_DEFAULT)) or _WEB_PORT_DEFAULT)
    if not (1 <= WEB_PORT <= 65535):     # 超范围端口 ui.run 会绑定失败起不来 → 回落默认
        WEB_PORT = _WEB_PORT_DEFAULT
except ValueError:
    WEB_PORT = _WEB_PORT_DEFAULT
# 监听地址：空/未设=只本机(127.0.0.1)。0.0.0.0=整个局域网可访问——本工具无鉴权、含 qB 密码，慎改（见设置页提示）
# 【它只监听 IPv4】而很多系统的 `localhost` 先解析成 IPv6 的 ::1 —— 浏览器打 http://localhost:2333
# 会连 [::1] 被拒、直接显示自己的错误页（一片白，且后端日志里一个字都没有，因为请求根本没到）。
# 启动横幅打印的是确切地址，照那个开。要两个协议栈都进得来就设 0.0.0.0。
WEB_HOST = os.getenv("WEB_HOST") or "127.0.0.1"
try:
    ipaddress.ip_address(WEB_HOST)       # 拼错的绑定地址（非法 IP）→回落 127，别让 ui.run 绑定失败起不来
except ValueError:
    if WEB_HOST != "localhost":
        WEB_HOST = "127.0.0.1"

# ---- 可热改设置：{键: (类型, 默认值)}，类型 bool/int/str/list ----
_SPEC = {
    "SITE_NAME": (str, ""),          # 站点名：顶栏站名 + 浏览器标签页标题（空=回落 autorss）
    "QB_ENABLED": (bool, False),
    "QB_SYNC_STATUS": (bool, True),         # 开=读 qB 实时态(下载中/进度/做种…)；关=发送过去即『已下』、完全不轮询 qB
    "QB_SYNC_INTERVAL": (int, 30),          # 有种子在下时的活跃轮询间隔（秒）——只在下载窗口内轮询
    "QB_SYNC_BACKSTOP_MIN": (int, 120),     # 保底自查间隔（分钟）：全无在下时才睡这么久，默认 2 小时
    "QB_IDLE_RECHECK_MIN": (int, 10),       # 中档自查间隔（分钟）：还有没下完的在下种子但都不活跃(慢/stalled/暂停)时，
                                            # 每隔这么久自查一次（介于高频轮询与保底长睡之间），别等一个保底周期才发现完成
    "QB_ACTIVE_FLOOR_KBPS": (int, 50),      # 慢速地板（KB/s）：下载慢于此算『没在真下』；0=只要有速度就算
    "QB_SLOW_ROUNDS": (int, 3),             # 连续几轮都没在真下才退出高频轮询、休眠（防单次抖动误判）
    "QB_STALL_TIMEOUT_MIN": (int, 1440),    # 停滞超时（分钟）：已交付的在下种子若进度连续这么久无推进，标『停滞(异常)』
                                            # 供人工处理——不自动换源、脱离轮询。默认 1 天；0=关闭该检测
    "QB_ARCHIVE_AFTER_DAYS": (int, 0),      # 完成归档（天）：种子下载完成超过这么多天后，自动从 qB 移除【只删种子、留文件】
                                            # 并标『已归档』(不再跟踪)。默认 0=关闭；如设 7=完成 7 天后清出 qB 列表
    "QB_CALLBACK_TOKEN": (str, ""),         # qB 完成回调 /api/qb/done 的校验 token（空=不校验；填了 qB 命令里要带 &t=）
    "QB_URL": (str, "http://127.0.0.1:8080"),
    "QB_USERNAME": (str, ""),
    "QB_PASSWORD": (str, ""),
    # 【默认空，不是 /home】空 = build_save_path 返回 None = 拒绝下载并报"未配置下载目录"。
    # 曾默认 /home：那是系统目录，而首配链路上【没有任何一处会拦】——设置页的防呆判的是
    # "两个根不能都为空"，预填的 /home 恒满足它，于是新用户装完直接点保存，番就下进了
    # /home/26C/<番名>。默认值必须是"过不了自己那道校验"的值，防呆才真的存在。
    "DOWN_PATH": (str, ""),                 # 工作目录=下载根；番剧/剧场版留空时都直接落它下面
    # 【留空 = 直接落工作目录，不额外分类】——不是 DOWN_PATH/番剧。实测落的是 /home/26C/<番名>。
    # 想要 DOWN_PATH/番剧/… 就把这两项填成【相对名】（如 番剧 / 剧场版），engine.build_save_path
    # 会把它拼在工作目录下面；填绝对路径则整个换根（可另一块盘）。
    # 早先这两行注释写的是"空=用工作目录 DOWN_PATH/番剧"，与实现不符——engine.py 与 settings.py
    # 两处的说明都是对的，只有这里错，而这里恰恰是读代码的人最先看到的地方。
    "ANIME_DOWN_PATH": (str, ""),           # 番剧独立下载根（空=直接落工作目录；相对名=拼在其下；绝对路径=换根）
    "MOVIE_DOWN_PATH": (str, ""),           # 剧场版独立下载根（同上）
    "ANIME_SEASON_SUBFOLDER": (bool, False),
    "QUARTER_FMT": (str, "{yy}{q}"),   # 番剧下载文件夹的季度目录名
    "MOVIE_QUARTER_FMT": (str, "{yyyy}"),   # 电影下载文件夹命名（默认年份 2026）；番剧走 QUARTER_FMT
    "QUARTER_FMT_UI": (str, ""),            # 空 = 跟随 QUARTER_FMT（见 __getattr__）
    "ANIME_SHOW_PENDING": (bool, True),
    "ANIME_SHOW_REJECTED": (bool, True),
    "ANIME_PAGE_YEARS": (int, 3),           # 番剧表一页显示几年的番（1~5，×4 得季度数）
    "MOVIE_PAGE_YEARS": (int, 5),            # 剧场版列表一页显示几年（1~5）
    "ANIME_DEFAULT_TAB": (str, "overview"), # 番剧页默认停哪个标签（overview/manage/confirm/fail/reject/sources），URL 带 ?t= 时以 URL 为准
    "MOVIE_DEFAULT_TAB": (str, "overview"), # 剧场版页默认停哪个标签（overview/list/fail/reject/sources）
    "ANIME_MULTIBRACKET_PARSE": (bool, False),    # 全括号命名(沸羊羊/悠哈/GM-Team 等)番名回退捕获——默认关，开了才对空名种子尝试从括号块猜番名
    "ANIME_POLL_ENABLED": (bool, True),           # 后台采集总开关（全新库首启默认关，见 load_from_db）
    "ANIME_POLL_INTERVAL": (int, 1200),
    "ANIME_DOWNLOAD_GRACE_MIN": (int, 120),
    "ANIME_TOP_PRIORITY_INSTANT": (bool, True),
    "ANIME_START_DATE": (str, ""),          # 开始使用日 YYYY-MM-DD：早于这天开播的番自动判『超期忽略』、不自动下载
                                            # （种子照常入库）；空=不限。改日期可逆，只动待确认/超期番，不碰人工确认/拒绝。
                                            # 【只作用于 TV 番剧】：剧场版逐版本人工点下、本就不会自动下载，
                                            # 故有意不受此限（已确认的产品决定，勿"对齐"加上去）
    "OPEN_PROXY": (bool, False),
    "PROXY_URL": (str, ""),                  # 代理地址：支持 http:// / https://；socks5:// 需另装 socksio 包
    "PROXY_USER": (str, ""),                 # 代理账号（需认证的代理才填；空=不认证）
    "PROXY_PASS": (str, ""),                 # 代理密码（同上）
    # 【默认开】内网/本机地址不走代理。关掉它之前请想清楚：qB 通常就在 127.0.0.1 或局域网上，
    # 一旦被代理走，登录请求里的 username/password 是【明文】POST 给那个代理的。
    # 它同时也压过**环境变量**里的 HTTP_PROXY —— httpx 默认 trust_env=True，
    # 于是设置页开关关着时环境里那个代理照样接管，本项开着才挡得住。
    "PROXY_SKIP_INTERNAL": (bool, True),
    "WEB_ALLOW_CIDRS": (str, ""),   # Web 访问网段白名单(CIDR,逗号分隔;空=不限)——绑 0.0.0.0 时限定可信内网,本机恒放行,即时生效
    "NOTIFY_URL": (str, ""),
    # 想收哪些事件（键见 services/notify.EVENTS）。【留空 = 全关】，不是全开——
    # 这与本项目别处"留空=不限"（字幕组白名单、标题关键词）恰好相反，设置页上写明了。
    # 默认值含 delivered，所以老库升级后【行为一字不变】（load_from_db 只补缺键）。
    "NOTIFY_EVENTS": (list, "delivered,movie,failed,stalled,finished,idle,qb_down,db_down,backlog"),
    "NOTIFY_MAX_PER_HOUR": (int, 20),       # 全事件合计的限流上限（0=不限）。被丢弃的条数会挂在下一条消息尾巴上
    "NOTIFY_BACKLOG_MIN": (int, 5),         # 『待识别』积压到几部才提醒
    # ---- 完结 / 断更巡检（见 core.anime.sweep_finished / sweep_idle）----
    "ANIME_FINISH_ENABLED": (bool, True),   # 判定完结（只打标记 + 发通知，不改下载行为）
    # 【默认关】判定完结后是否【真的停止自动下新集】。判据已经很保守（要 bgm 给出总集数、
    # 无集号歧义段、1..T 每一集都在手），但 bgm 的总集数少记一集是真实存在的——那会让最后一集
    # 永远下不下来。开之前请先让它跑一阵、看『已完结』的标记打得对不对。
    "ANIME_FINISH_UNSUB": (bool, False),
    "ANIME_IDLE_DAYS": (int, 14),           # 一部追番中的番多少天没有新种子就提醒（0=关）
    "SWEEP_INTERVAL_MIN": (int, 180),       # 完结/断更巡检的间隔（分钟）
    "NOTIFY_TIMEOUT": (int, 10),
    "ENRICH_TIMEOUT": (int, 15),
    "ENRICH_RETRY_TIMES": (int, 3),          # bgm 请求瞬时失败(超时/连接)的即时重试次数
    "REENRICH_RETRY_BASE": (int, 30),        # 『待识别』番延迟重试基准等待（分钟），每失败一次翻倍（默认 30 分钟）
    "REENRICH_RETRY_MAX": (int, 1440),       # 延迟重试等待上限（分钟），翻倍到此封顶（默认 1440=24 小时）
    "REENRICH_MAX_TRIES": (int, 5),          # 每部『待识别』番最多自动重试几次（满则停自动、留手动）
    "ANI_RSS_URL": (str, "https://nyaa.si/?page=rss&u=ANiTorrent"),
    "MIKAN_ENABLED": (bool, False),
    "MIKAN_RSS_URL": (str, "https://mikanani.me/RSS/Classic"),
    "MIKAN_BASE": (str, "https://mikanani.me"),
    "BGM_API": (str, "https://api.bgm.tv"),
    # ---- 剧场版/OVA 自动扫描（来源固定为 Mikan 季度桶）----
    "MOVIE_SCAN_ENABLED": (bool, False),    # 自动扫描开关（关=只在 /movies 手动点扫描）
    "MOVIE_SCAN_INTERVAL": (int, 604800),   # 每隔多少秒自动扫一次剧场版（默认 7 天）——
                                            # 剧场版桶更新很慢，扫太勤没意义、只是白打 Mikan
    "MOVIE_SCAN_LAST": (str, ""),           # 上次扫描时间（ISO，运行时更新；非用户填）
    # ---- 自动备份（整库快照，走 VACUUM INTO；见 db/backup.py）----
    # 这是全项目唯一"出事就没救"的空白的补丁：我们有迁移、有 Alembic、有停摆自愈，
    # 但在此之前【没有任何一份可回滚的数据副本】。默认开——备份的价值全在"没想起来时它已经在做了"。
    "BACKUP_ENABLED": (bool, True),
    "BACKUP_INTERVAL_HOURS": (int, 24),     # 每隔多少小时自动备一次（跨重启不会误重备，判据同剧场版扫描）
    "BACKUP_KEEP": (int, 7),                # 保留最近几份（0=不自动清理）
    "BACKUP_LAST": (str, ""),               # 上次备份时间（ISO，运行时更新；非用户填）
    # ---- 业务数据库（这几项本身恒存在【本地 SQLite】的 setting 表里，见 db.__init__ 的双引擎说明）----
    "DB_BACKEND": (str, "sqlite"),          # 业务表落在哪：'sqlite'(本地文件) | 'mysql'
    "DB_MYSQL_HOST": (str, ""),
    "DB_MYSQL_PORT": (int, 3306),
    "DB_MYSQL_USER": (str, ""),
    "DB_MYSQL_PASSWORD": (str, ""),
    "DB_MYSQL_NAME": (str, ""),             # 库名（database），需已存在
    "DB_MYSQL_CHARSET": (str, "utf8mb4"),   # 必须是 utf8mb4，否则日文/emoji 番名存不进去
}

# 全新库首启时这些键种成 false（而非其 _SPEC 默认）：配置还没弄好，先别自动采集
_FRESH_OFF = {"ANIME_POLL_ENABLED"}


def _coerce(kind, raw):
    """把字符串/原值按类型转换；转不动回该类型的空值。"""
    if kind is bool:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if kind is int:
        try:
            return int(str(raw).strip())
        except (ValueError, TypeError):
            return 0
    if kind is list:
        return [s.strip() for s in str(raw).split(",") if s.strip()]
    return str(raw)


def _to_raw(kind, default) -> str:
    """把 _SPEC 默认值转成存进 settings 表的字符串形式。"""
    if kind is bool:
        return "true" if default else "false"
    if kind is list:
        return ",".join(default) if isinstance(default, (list, tuple)) else str(default)
    return str(default)


# 内存当前值：先用硬编码默认值兜底；启动时 load_from_db() 再用数据库里的值覆盖
_v = {k: _coerce(kind, default) for k, (kind, default) in _SPEC.items()}


def raw(name: str):
    """取【原始】配置值，不走 __getattr__ 的派生。

    派生项（PROXY / QUARTER_FMT_UI）在读的时候会替你回落到别的键，那对使用方是对的，
    但对【设置页的输入框】是错的：框里要能表达"这一项留空"这个状态本身，
    而派生值会把空串渲染成它回落到的那个字面量——读派生、写原始，一次保存就把
    "跟随"塌缩成"钉死在当时那个值"。要渲染可编辑的原始值就用它。
    """
    return _v[name]


def __getattr__(name):
    """动态读当前配置值：config.QB_ENABLED 等；PROXY / QUARTER_FMT_UI 为派生项。"""
    if name == "PROXY":
        return _v["PROXY_URL"] if (_v["OPEN_PROXY"] and _v["PROXY_URL"]) else None
    if name == "QUARTER_FMT_UI":
        return _v["QUARTER_FMT_UI"] or _v["QUARTER_FMT"]  # 空则跟随文件夹模板
    if name in _v:
        return _v[name]
    raise AttributeError(f"module 'config' has no attribute {name!r}")


# 恒直连的地址形状。httpx 的 URLPattern 不支持 CIDR，所以私网【网段】没法写成通配，
# 只能靠调用方把真实目标 URL 传进来（见 http_client_kwargs 的 url 形参）；
# 这里能静态覆盖的是本机与常见的内网域名后缀。
_DIRECT_PATTERNS = ("all://localhost", "all://127.0.0.1", "all://[::1]",
                    "all://*.local", "all://*.lan", "all://*.internal", "all://*.home.arpa")


def _direct_mounts(url: str | None) -> dict:
    """要绕开代理直连的挂载表。空表＝没有需要绕的。

    【为什么用 mounts 而不是 NO_PROXY】实测 httpx 的 mounts **同时压过环境变量代理与显式 proxy**，
    所以这一份挂载在两种来源下都成立——而"环境变量在开关关着时接管"正是要挡的事之一。
    """
    if not _v.get("PROXY_SKIP_INTERNAL", True):
        return {}
    mounts = {p: None for p in _DIRECT_PATTERNS}
    host = _internal_literal_host(url)
    if host:
        mounts[f"all://{host}"] = None     # 目标就是个字面内网 IP（qB 常见写法）
    return mounts


def _internal_literal_host(url: str | None) -> str:
    """url 的主机若是【字面】内网/环回 IP 就返回它，否则空串。

    只认字面量：域名要解析才知道，而这里是同步路径，不能在事件循环上做 DNS。
    真实场景里 qB 与自建服务的地址几乎都是直接写 IP 的，够用。
    """
    if not url:
        return ""
    import ipaddress
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url).hostname or "").strip("[]")
        ip = ipaddress.ip_address(host)
    except ValueError:
        return ""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    ok = (ip.is_private or ip.is_loopback or ip.is_link_local
          or ip.is_reserved or ip.is_unspecified)
    return host if ok else ""


_MAX_REDIRECTS = 3          # 见 http_client_kwargs 里那段说明（E-41）


def http_client_kwargs(timeout: int = 30, url: str | None = None) -> dict:
    """httpx.AsyncClient 的公共 kwargs：超时 + 跟随重定向 +（启用时）代理。各处抓取统一走它。
    代理账号/密码任一非空时走带认证的 httpx.Proxy；socks5:// 需自行装 socksio——
    缺了它是在【建 AsyncClient 那一步】就抛 ImportError（不是发请求时），而且抛的不是 httpx.HTTPError，
    所以各处只接 HTTPError 的 except 都拦不住它。"""
    # 【SSRF 守卫默认就装上】首跳放行（地址是用户在设置页自己填的，自建镜像/局域网 webhook 是正当用法），
    # 重定向后的每一跳强制内网判定。装在这里而不是各调用点，是因为"新加一处抓取时忘了加"
    # 恰好是本项目反复栽跟头的那种失效形状——而这一处忘了，代价是常驻协程周期性地
    # 对同机 qB 发一个源站可控的 GET（审查实测收到过 torrents/delete?hashes=all）。
    # 延迟导入：core.ssrf 要 import config，模块级会成环。
    from core import ssrf
    # 【跳数压到 3】httpx 默认允许 20 跳，而每一跳的响应体都会被【完整读进内存】——
    # httpx 在跟随重定向之前先 `await response.aread()`，并且把每一跳连同它的内容
    # 一路挂在 response.history 上带到最终响应。也就是说 N 跳的响应体是【累加驻留】的，
    # 而 services/fetch 那套逐块封顶（_read_capped）只作用在【最终】那一个响应上，
    # 从头到尾看不见中间跳。实测 4 跳 × 100MB → 进程 RSS 峰值 568MB；
    # 通知那条反差最大：它的 cap 只有 64KB。
    # 【为什么不自己写重定向循环（E-41 的 A 案）】那要接管 Location 解析、相对路径拼接、
    # 方法降级、循环检测，而 SSRF 守卫的跳数计数（autorss_hops）正是靠 httpx 的
    # extensions 浅拷贝语义顺着链传下去的——自己写循环必须把这条一并接管，
    # 写错就是守卫整个失效。压跳数是几行的事，立刻把最坏值压掉一个数量级，且不碰守卫。
    # 3 跳够用：真实的 feed/取种最多是 "http→https" + "裸域→www" 这两跳。
    kwargs = {"timeout": timeout, "follow_redirects": True, "max_redirects": _MAX_REDIRECTS,
              "event_hooks": {"request": [ssrf.guard_redirect_request]}}
    mounts = _direct_mounts(url)
    if mounts:
        # 【即使没配代理也挂上】它同时挡住环境变量里的 HTTP_PROXY——
        # 那条路径不经过我们的开关，只有挂载表压得住。
        kwargs["mounts"] = mounts
    proxy = __getattr__("PROXY")
    if proxy:
        user, pw = _v["PROXY_USER"], _v["PROXY_PASS"]
        if user or pw:
            import httpx
            kwargs["proxy"] = httpx.Proxy(url=proxy, auth=(user, pw))
        else:
            kwargs["proxy"] = proxy
    return kwargs


# 【配置是否真的从数据库读出来过】False = 内存里全是硬编码默认值。
# 这不只是"少了几个用户偏好"：WEB_ALLOW_CIDRS 的默认值是空串，而空串在 netguard 里的含义是
# 【不限制、放行一切】。于是"建表/迁移失败"这条本该更安全的失败路径，反而把一个无鉴权的、
# 存着 qB 明文密码的面板对整个局域网敞开——典型的 fail-open。
# netguard 据此在配置没读出来时退回"只放行回环"（fail-closed），见 core/netguard.py。
loaded_from_db = False


def load_from_db() -> None:
    """启动时（init_db 之后）加载配置，并把 settings 表缺的键补齐写入。

    新库 = 写入全部默认值；以后往 _SPEC 新加的设置项，下次启动也会补上缺的键。
    成功后置 loaded_from_db=True（网段白名单据此判断自己该不该 fail-closed）。
    """
    global loaded_from_db
    from sqlmodel import select

    from db import get_meta_session
    from db.models import Setting
    # 【走 meta 会话】配置恒存本地 SQLite，与业务库是否切到 MySQL 无关——
    # 否则连不上 MySQL 时就读不到"该怎么连 MySQL"，先有鸡还是先有蛋。
    # 【幂等写 + 撞了重跑一次】这个循环会 INSERT（Setting.key 是主键），
    # 而它和下面 _merge_new_notify_events 面对的是【同一个】并发风险，
    # 却只有后者被整段兜住——那正是本项目第①种缺陷形状（同一件事两处，只护了一处）。
    # 触发窗口就是那段注释自己列的："升级后第一次启动"（_SPEC 加了新键、库里还没有）
    # 叠上两个进程同时起：service 还跑着又手动试跑一次、deploy.sh 重启期间新旧进程重叠。
    # 两边的 SELECT 拿到同一份"缺这些键"的快照，各自 INSERT，后 commit 的那个直接
    # IntegrityError —— 而它会穿透 load_from_db → main.py 的启动 try → mark_data_fatal，
    # 一次并发启动就能让整台机器停在那里。
    def _fill_missing() -> dict:
        with get_meta_session() as s:
            have = {r.key: r.value for r in s.exec(select(Setting))}
            fresh = not have  # settings 表原本为空 = 全新库首启
            for k, (kind, default) in _SPEC.items():
                if k in have:
                    continue
                if fresh and k in _FRESH_OFF:
                    have[k] = "false"          # 全新库首启：配置好前先别自动采集
                else:
                    have[k] = _to_raw(kind, default)
                if s.get(Setting, k) is None:  # 幂等：赢家可能已经插好了
                    s.add(Setting(key=k, value=have[k]))
            s.commit()
            return have

    try:
        have = _fill_missing()
    except Exception as e:
        # 重跑一次：这一次那些键已经被赢家插好，走的是"没有缺键"的空路径，必成功。
        # 【不能只 except 不重跑】have 是下面读配置用的，吞掉异常会让它未定义。
        log.warning("补齐配置键时撞车（多半是并发启动），重试一次：%s: %s", type(e).__name__, e)
        have = _fill_missing()
    for k in _SPEC:
        _v[k] = _coerce(_SPEC[k][0], have[k])
    loaded_from_db = True
    # 【必须排在 loaded_from_db 之后、且整段兜住】走到这里配置已经全部读进 _v 了，
    # 并入新事件只是一个锦上添花的升级步骤——它的成败与"配置读没读出来"毫无关系，
    # 却曾经能决定后者：这个函数会写库（INSERT/UPDATE + commit），而它抛出的任何异常都会穿透
    # load_from_db → main.py 的启动 try → db.mark_data_fatal，而 fatal 的解除方式【只有人工介入】
    # （探测探通了也不解除）。连带后果是 netguard 只放行回环、设置页拒绝保存——
    # 一次并发启动撞上的 UNIQUE 约束，就能让整台机器停在那里。
    # 实测触发窗口恰好是"升级后第一次启动"：service 还跑着又手动试跑一次（README 就是这么教的）、
    # deploy.sh 重启期间新旧进程重叠、备份的 VACUUM INTO 把 meta 库锁过 busy_timeout、磁盘满。
    try:
        _merge_new_notify_events(have)
    except Exception as e:
        log.warning("并入新增通知事件失败（不影响启动，下次再试）：%s: %s", type(e).__name__, e)


_KNOWN_EVENTS_KEY = "_KNOWN_NOTIFY_EVENTS"    # 内部键，不进 _SPEC（不该出现在设置页）


def _merge_new_notify_events(have: dict) -> None:
    """把【本版本新增的】通知事件并进用户已有的 NOTIFY_EVENTS。

    【为什么必须有这一步】load_from_db 只补【缺失的键】。NOTIFY_EVENTS 一旦存过一次，
    以后往 services.notify.EVENTS 里加的任何新事件，对所有老库都是【静默默认关】——
    用户升级后得自己想到去设置页勾一下，而他根本不知道多了这么个事件。
    那会挡住后续所有"加一类提醒"的改进。

    只并【本版本新出现】的键：用一份"上次见过哪些事件"的快照做差集，
    所以用户【显式取消过】的事件不会被重新打开（那些键早就在快照里了）。
    """
    from sqlmodel import select

    from db import get_meta_session
    from db.models import Setting
    try:
        from services.notify import EVENTS
    except Exception:                    # 循环导入等极端情况：宁可不并，也别拖垮启动
        return
    all_keys = set(EVENTS)
    with get_meta_session() as s:
        row = s.exec(select(Setting).where(Setting.key == _KNOWN_EVENTS_KEY)).first()
        known = {k for k in (row.value if row else "").split(",") if k} if row else None
        if known is None:
            # 第一次见（老库或新库）：只记快照，不动用户的选择。
            # 幂等写法：并发启动时两个进程可能同时走到这里，裸 INSERT 会撞 UNIQUE。
            exist = s.get(Setting, _KNOWN_EVENTS_KEY)
            if exist is None:
                s.add(Setting(key=_KNOWN_EVENTS_KEY, value=",".join(sorted(all_keys))))
                s.commit()
            return
        fresh = all_keys - known
        if not fresh:
            return
        cur = set(_v.get("NOTIFY_EVENTS") or [])
        if not cur:
            # 【用户把通知全关了 —— 那是一个明确的意思表示，不该被下个版本推翻】
            # 空集与新键取并集就是新键，等于"关全部"在每次升级时自动变回"开几个"。
            # 快照仍要更新：否则下次升级又会把这些键当成"新增"再并一次。
            row.value = ",".join(sorted(all_keys))
            s.add(row)
            s.commit()
            return
        merged = sorted(cur | fresh)
        # 【先落库成功，再改内存】与同文件 set_many 的规矩一致：写库失败时内存不该单方面前进，
        # 否则本进程用着一份库里并不存在的配置，重启即回退，而没有任何迹象。
        row.value = ",".join(sorted(all_keys))
        setting = s.exec(select(Setting).where(Setting.key == "NOTIFY_EVENTS")).first()
        if setting is not None:
            setting.value = ",".join(merged)
            s.add(setting)
        s.add(row)
        s.commit()
        _v["NOTIFY_EVENTS"] = merged


def set_many(updates: dict) -> None:
    """把设置写进数据库并即时更新内存（热生效）。updates: {键: 字符串值}，非 _SPEC 键忽略。"""
    from db import get_meta_session
    from db.models import Setting
    applied = {}
    with get_meta_session() as s:   # 同 load_from_db：配置恒走本地 SQLite
        for k, raw in updates.items():
            if k not in _SPEC:
                continue
            row = s.get(Setting, k)
            if row is None:
                s.add(Setting(key=k, value=str(raw)))
            else:
                row.value = str(raw)
                s.add(row)
            applied[k] = _coerce(_SPEC[k][0], raw)
        s.commit()               # 先落库成功
    _v.update(applied)           # 再更新内存：commit 抛异常时不会留下未持久化、重启即回退的幽灵值


def update_env(updates: dict) -> None:
    """把 updates 写回 .env（原地改已有键、追加新键）。仅用于 WEB_PORT 等重启才生效的结构项。"""
    def _fmt(v: str) -> str:
        v = str(v)
        if v and not re.search(r'[\s#"\']', v):
            return v
        return '"' + v.replace('\\', '\\\\').replace('"', '\\"') + '"'

    with _env_lock:
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
        seen, out = set(), []
        for line in lines:
            m = re.match(r"\s*([A-Za-z0-9_]+)\s*=", line)
            if m and m.group(1) in updates:
                out.append(f"{m.group(1)}={_fmt(updates[m.group(1)])}")
                seen.add(m.group(1))
            else:
                out.append(line)
        for k, v in updates.items():
            if k not in seen:
                out.append(f"{k}={_fmt(v)}")
        text = "\n".join(out) + "\n"
        fd, tmp = tempfile.mkstemp(dir=str(BASE_DIR), prefix=".env.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, ENV_PATH)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
