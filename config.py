"""配置：从 .env 读取，全部有默认值。改配置不用动代码。"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


def _bool(key, default=False):
    v = os.getenv(key)
    return default if v is None or v.strip() == "" else v.strip().lower() in ("1", "true", "yes", "on")


def _int(key, default):
    v = os.getenv(key)
    return default if v is None or v.strip() == "" else int(v)


# ---- qBittorrent ----
QB_URL = os.getenv("QB_URL", "http://127.0.0.1:8080")
QB_USERNAME = os.getenv("QB_USERNAME", "")
QB_PASSWORD = os.getenv("QB_PASSWORD", "")

# ---- 路径 ----
DOWN_PATH = os.getenv("DOWN_PATH", "/media/upan/Anime")   # qB 保存根目录
DB_PATH = os.getenv("DB_PATH", str(DATA_DIR / "autorss.db"))

# ---- 代理（OPEN_PROXY=true 才生效）----
PROXY_URL = os.getenv("PROXY_URL", "")
OPEN_PROXY = _bool("OPEN_PROXY", False)
PROXY = PROXY_URL if (OPEN_PROXY and PROXY_URL) else None

# ---- 轮询 ----
POLL_INTERVAL = _int("POLL_INTERVAL", 1200)   # 每轮抓取间隔（秒）

# ---- Web ----
WEB_PORT = _int("WEB_PORT", 8080)

# ---- 源 ----
ANI_RSS_URL = os.getenv("ANI_RSS_URL", "https://nyaa.si/?page=rss&u=ANiTorrent")

# ---- 通知（NOTIFY_URL 留空即关闭）----
NOTIFY_URL = os.getenv("NOTIFY_URL", "")
NOTIFY_TIMEOUT = _int("NOTIFY_TIMEOUT", 10)
