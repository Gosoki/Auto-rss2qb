"""配置：默认值硬编码在 _SPEC；建库时写进数据库 settings 表，之后以数据库为唯一来源。

读取一律走 `config.<KEY>`（经模块 __getattr__ 返回当前值），别再 `from config import KEY`
（那样会在导入时绑死快照，改了不生效）。设置页保存 → 写库 + 更新内存 → 即时生效。
例外：DB_PATH（开库前提）、WEB_PORT（绑端口）本质上就得重启，走 .env/硬编码默认，不进 settings 表。
"""
import ipaddress
import os
import re
import tempfile
import threading
from pathlib import Path

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

# ---- 结构性/绑定项：走 .env，改了需重启（DB_PATH 是启动 DB 的前提，WEB_HOST/WEB_PORT 绑监听）----
DB_PATH = os.getenv("DB_PATH", str(DATA_DIR / "autorss.db"))
try:
    WEB_PORT = int(os.getenv("WEB_PORT", "8080") or "8080")
    if not (1 <= WEB_PORT <= 65535):     # 超范围端口 ui.run 会绑定失败起不来 → 回落默认
        WEB_PORT = 8080
except ValueError:
    WEB_PORT = 8080
# 监听地址：空/未设=只本机(127.0.0.1)。0.0.0.0=整个局域网可访问——本工具无鉴权、含 qB 密码，慎改（见设置页提示）
WEB_HOST = os.getenv("WEB_HOST") or "127.0.0.1"
try:
    ipaddress.ip_address(WEB_HOST)       # 拼错的绑定地址（非法 IP）→回落 127，别让 ui.run 绑定失败起不来
except ValueError:
    if WEB_HOST != "localhost":
        WEB_HOST = "127.0.0.1"

# ---- 可热改设置：{键: (类型, 默认值)}，类型 bool/int/str/list ----
_SPEC = {
    "QB_ENABLED": (bool, True),
    "QB_SYNC_STATUS": (bool, True),         # 开=读 qB 实时态(下载中/进度/做种…)；关=发送过去即『已下』、完全不轮询 qB
    "QB_SYNC_INTERVAL": (int, 20),          # 有种子在下时的活跃轮询间隔（秒）——只在下载窗口内轮询
    "QB_SYNC_BACKSTOP_MIN": (int, 180),     # 保底自查间隔（分钟）：全无在下时才睡这么久，默认 3 小时
    "QB_IDLE_RECHECK_MIN": (int, 10),       # 中档自查间隔（分钟）：还有没下完的在下种子但都不活跃(慢/stalled/暂停)时，
                                            # 每隔这么久自查一次（介于高频轮询与保底长睡之间），别等一个保底周期才发现完成
    "QB_ACTIVE_FLOOR_KBPS": (int, 20),      # 慢速地板（KB/s）：下载慢于此算『没在真下』；0=只要有速度就算
    "QB_SLOW_ROUNDS": (int, 3),             # 连续几轮都没在真下才退出高频轮询、休眠（防单次抖动误判）
    "QB_STALL_TIMEOUT_MIN": (int, 1440),    # 停滞超时（分钟）：已交付的在下种子若进度连续这么久无推进，标『停滞(异常)』
                                            # 供人工处理——不自动换源、脱离轮询。默认 1 天；0=关闭该检测
    "QB_ARCHIVE_AFTER_DAYS": (int, 0),      # 完成归档（天）：种子下载完成超过这么多天后，自动从 qB 移除【只删种子、留文件】
                                            # 并标『已归档』(不再跟踪)。默认 0=关闭；如设 7=完成 7 天后清出 qB 列表
    "QB_CALLBACK_TOKEN": (str, ""),         # qB 完成回调 /api/qb/done 的校验 token（空=不校验；填了 qB 命令里要带 &t=）
    "QB_URL": (str, "http://127.0.0.1:8080"),
    "QB_USERNAME": (str, ""),
    "QB_PASSWORD": (str, ""),
    "DOWN_PATH": (str, "/media/upan/Anime"),   # 工作目录=下载根；动漫/电影留空时都落它下面
    "ANIME_DOWN_PATH": (str, ""),           # 动漫独立下载根（空=用工作目录 DOWN_PATH/番剧；填了=放这个独立目录，可另一块盘）
    "MOVIE_DOWN_PATH": (str, ""),            # 电影独立下载根（空=用工作目录 DOWN_PATH/剧场版；填了=放这个独立目录，可另一块盘）
    "ANIME_SEASON_SUBFOLDER": (bool, True),
    "QUARTER_FMT": (str, "{yy}{q} · {m}月 · {season}"),   # 番剧下载文件夹的季度目录名
    "MOVIE_QUARTER_FMT": (str, "{yyyy}"),   # 电影下载文件夹命名（默认年份 2026）；番剧走 QUARTER_FMT
    "QUARTER_FMT_UI": (str, ""),            # 空 = 跟随 QUARTER_FMT（见 __getattr__）
    "ANIME_SHOW_PENDING": (bool, False),
    "ANIME_SHOW_REJECTED": (bool, False),
    "ANIME_PAGE_YEARS": (int, 3),           # 番剧表一页显示几年的番（1~5，×4 得季度数）
    "MOVIE_PAGE_YEARS": (int, 5),            # 剧场版列表一页显示几年（1~5）
    "ANIME_DEFAULT_TAB": (str, "manage"),   # 番剧页默认停哪个标签（overview/manage/confirm/fail/reject/sources），URL 带 ?t= 时以 URL 为准
    "MOVIE_DEFAULT_TAB": (str, "list"),     # 剧场版页默认停哪个标签（overview/list/fail/reject/sources）
    "ANIME_MULTIBRACKET_PARSE": (bool, False),    # 全括号命名(沸羊羊/悠哈/GM-Team 等)番名回退捕获——默认关，开了才对空名种子尝试从括号块猜番名
    "ANIME_POLL_ENABLED": (bool, True),           # 后台采集总开关（全新库首启默认关，见 load_from_db）
    "ANIME_POLL_INTERVAL": (int, 1200),
    "ANIME_DOWNLOAD_GRACE_MIN": (int, 120),
    "ANIME_TOP_PRIORITY_INSTANT": (bool, True),
    "OPEN_PROXY": (bool, False),
    "PROXY_URL": (str, ""),
    "WEB_ALLOW_CIDRS": (str, ""),   # Web 访问网段白名单(CIDR,逗号分隔;空=不限)——绑 0.0.0.0 时限定可信内网,本机恒放行,即时生效
    "NOTIFY_URL": (str, ""),
    "NOTIFY_TIMEOUT": (int, 10),
    "ENRICH_TIMEOUT": (int, 15),
    "ENRICH_RETRY_TIMES": (int, 3),          # bgm 请求瞬时失败(超时/连接)的即时重试次数
    "REENRICH_RETRY_BASE": (int, 30),        # 『待识别』番延迟重试基准等待（分钟），每失败一次翻倍（默认 30 分钟）
    "REENRICH_RETRY_MAX": (int, 1440),       # 延迟重试等待上限（分钟），翻倍到此封顶（默认 1440=24 小时）
    "REENRICH_MAX_TRIES": (int, 12),         # 每部『待识别』番最多自动重试几次（满则停自动、留手动）
    "ANI_RSS_URL": (str, "https://nyaa.si/?page=rss&u=ANiTorrent"),
    "MIKAN_ENABLED": (bool, False),
    "MIKAN_RSS_URL": (str, "https://mikanani.me/RSS/Classic"),
    "MIKAN_BASE": (str, "https://mikanani.me"),
    "MIKAN_SUBGROUPS": (list, ""),          # 逗号分隔 → list
    "BGM_API": (str, "https://api.bgm.tv"),
    # ---- 剧场版/OVA 自动扫描（来源固定为 Mikan 季度桶）----
    "MOVIE_SCAN_ENABLED": (bool, False),    # 自动扫描开关（关=只在 /movies 手动点扫描）
    "MOVIE_SCAN_INTERVAL": (int, 43200),    # 每隔多少秒自动扫一次剧场版（默认 12 小时）
    "MOVIE_SCAN_LAST": (str, ""),           # 上次扫描时间（ISO，运行时更新；非用户填）
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


def __getattr__(name):
    """动态读当前配置值：config.QB_ENABLED 等；PROXY / QUARTER_FMT_UI 为派生项。"""
    if name == "PROXY":
        return _v["PROXY_URL"] if (_v["OPEN_PROXY"] and _v["PROXY_URL"]) else None
    if name == "QUARTER_FMT_UI":
        return _v["QUARTER_FMT_UI"] or _v["QUARTER_FMT"]  # 空则跟随文件夹模板
    if name in _v:
        return _v[name]
    raise AttributeError(f"module 'config' has no attribute {name!r}")


def http_client_kwargs(timeout: int = 30) -> dict:
    """httpx.AsyncClient 的公共 kwargs：超时 + 跟随重定向 +（启用时）代理。各处抓取统一走它。"""
    kwargs = {"timeout": timeout, "follow_redirects": True}
    proxy = __getattr__("PROXY")
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs


def load_from_db() -> None:
    """启动时（init_db 之后）加载配置，并把 settings 表缺的键补齐写入。

    新库 = 写入全部默认值；以后往 _SPEC 新加的设置项，下次启动也会补上缺的键。
    """
    from sqlmodel import select

    from db import get_session
    from db.models import Setting
    with get_session() as s:
        have = {r.key: r.value for r in s.exec(select(Setting))}
        fresh = not have  # settings 表原本为空 = 全新库首启
        for k, (kind, default) in _SPEC.items():
            if k not in have:
                if fresh and k in _FRESH_OFF:
                    have[k] = "false"          # 全新库首启：配置好前先别自动采集
                else:
                    have[k] = _to_raw(kind, default)
                s.add(Setting(key=k, value=have[k]))
        s.commit()
    for k in _SPEC:
        _v[k] = _coerce(_SPEC[k][0], have[k])


def set_many(updates: dict) -> None:
    """把设置写进数据库并即时更新内存（热生效）。updates: {键: 字符串值}，非 _SPEC 键忽略。"""
    from db import get_session
    from db.models import Setting
    applied = {}
    with get_session() as s:
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
