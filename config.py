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
# False = 开发/无 qB 模式：照常采集元数据，但不发送种子给 qB（种子留在待下）
QB_ENABLED = _bool("QB_ENABLED", True)

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

# ---- Mikan 发现（P2，默认关；开了才抓 Mikan 全站发现非 ANi 番）----
MIKAN_ENABLED = _bool("MIKAN_ENABLED", False)
MIKAN_RSS_URL = os.getenv("MIKAN_RSS_URL", "https://mikanani.me/RSS/Classic")
MIKAN_BASE = os.getenv("MIKAN_BASE", "https://mikanani.me")
# 只保留这些字幕组的发现（逗号分隔，留空=全部；用于压 Mikan 噪声）
MIKAN_SUBGROUPS = [s.strip() for s in os.getenv("MIKAN_SUBGROUPS", "").split(",") if s.strip()]

# ---- Bangumi 富集（P3，默认关；开了才用真实放送日定季度+规范名）----
ENRICH_ENABLED = _bool("ENRICH_ENABLED", False)
BGM_API = os.getenv("BGM_API", "https://api.bgm.tv")
ENRICH_TIMEOUT = _int("ENRICH_TIMEOUT", 15)

# ---- 通知（NOTIFY_URL 留空即关闭）----
NOTIFY_URL = os.getenv("NOTIFY_URL", "")
NOTIFY_TIMEOUT = _int("NOTIFY_TIMEOUT", 10)
