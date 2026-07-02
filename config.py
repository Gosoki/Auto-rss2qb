"""配置加载：所有可变项都从同目录下的 .env 读取（见 .env.example）。

修改配置只需编辑 .env，不用改动任何代码。
"""
import os

from dotenv import load_dotenv

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 从脚本所在目录读取 .env，避免因工作目录不同而读不到配置
load_dotenv(os.path.join(_BASE_DIR, ".env"))


def _get(key, default=None, required=False):
    value = os.getenv(key, default)
    if required and (value is None or value == ""):
        raise RuntimeError(f"缺少必需的配置项 {key}，请在 .env 中设置")
    return value


def _get_bool(key, default=False):
    value = os.getenv(key)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _get_int(key, default):
    value = os.getenv(key)
    if value is None or value.strip() == "":
        return default
    return int(value)


# ---- qBittorrent Web UI ----
QB_URL = _get("QB_URL", "http://127.0.0.1:8080")
QB_USERNAME = _get("QB_USERNAME", required=True)
QB_PASSWORD = _get("QB_PASSWORD", required=True)

# ---- 代理 ----
OPEN_PROXY = _get_bool("OPEN_PROXY", False)
PROXY_URL = _get("PROXY_URL", "")
# requests 的 proxies 参数：关闭代理时传 None
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if OPEN_PROXY and PROXY_URL else None

# ---- 路径 ----
DOWN_PATH = _get("DOWN_PATH", "/media/upan/Anime")  # qBittorrent 保存根目录
LOG_PATH = _get("LOG_PATH", "log")                  # 日志目录
TORRENT_PATH = _get("TORRENT_PATH", "torrent")      # 临时种子文件目录

# ---- 运行行为 ----
STOP_TIME = _get_int("STOP_TIME", 1200)             # 每轮抓取间隔（秒）
TRY_INSERT_ALL = _get_bool("TRY_INSERT_ALL", False)  # True=遍历全部条目，避免漏番；False=遇到已存在即休眠
STARTUP_DOWNLOAD_LIMIT = _get_int("STARTUP_DOWNLOAD_LIMIT", 100)  # 启动时补下的最大条数
POLL_DOWNLOAD_LIMIT = _get_int("POLL_DOWNLOAD_LIMIT", 10)         # 每轮补下的最大条数

# ---- 数据库 ----
# DB_TYPE = sqlite（默认，本地文件，无需额外依赖）或 mysql
DB_TYPE = _get("DB_TYPE", "sqlite").strip().lower()
SQLITE_PATH = _get("SQLITE_PATH", os.path.join(_BASE_DIR, "autorss.db"))

# MySQL 连接信息（仅 DB_TYPE=mysql 时才要求填写）
_mysql_required = DB_TYPE == "mysql"
MYSQL = {
    "host": _get("MYSQL_HOST", "127.0.0.1"),
    "port": _get_int("MYSQL_PORT", 3306),
    "user": _get("MYSQL_USER", required=_mysql_required),
    "password": _get("MYSQL_PASSWORD", required=_mysql_required),
    "database": _get("MYSQL_DATABASE", "jp_autorss"),
    "charset": "utf8mb4",  # 正确存储日文/繁体/emoji
}

# ---- 通知（robot 推送）----
# NOTIFY_URL 留空则自动关闭通知，程序照常运行
NOTIFY_URL = _get("NOTIFY_URL", "")
NOTIFY_ENABLED = _get_bool("NOTIFY_ENABLED", True) and bool(NOTIFY_URL)
NOTIFY_DELAY = _get_int("NOTIFY_DELAY", 1)      # 每次推送前的等待（秒），用于限速
NOTIFY_TIMEOUT = _get_int("NOTIFY_TIMEOUT", 10)
NOTIFY_RETRIES = _get_int("NOTIFY_RETRIES", 3)
