"""数据访问层：所有涉及具体表和 SQL 的操作都集中在这里（占位符统一用 ?）。

三张表（结构见 db.py 的 SCHEMA）：
    anime(anime_name, if_down)                番是否要下载（订阅开关，按番名整体）
    anime_season(anime_name, season, quarter) 某番某季属于哪个季度（决定下载文件夹）
    rss_torrent(torrent_url, ...)             每一集的种子记录，status: 0未下 1已加入qB

设计要点：
- 季度绑定在 (番, 季) 上，返回番的新季会进新季度，不会和第一季混。
- 登记跟集数无关：第一次见到某 (番, 季) 就登记（ensure_*），天然支持第0话/连续编号。
"""
from db import get_db


# ---------- anime：订阅开关 ----------

def ensure_anime(anime_name):
    """确保订阅表里有这个番（默认下载）。已存在则不动，返回 True 表示是新番。"""
    return get_db().insert(
        "INSERT INTO anime (anime_name, if_down) VALUES (?, 1)", (anime_name,)
    )


def is_subscribed(anime_name):
    """该番是否要下载（if_down=1）。未登记视为不下载。"""
    rows = get_db().query("SELECT if_down FROM anime WHERE anime_name = ?", (anime_name,))
    return bool(rows) and int(rows[0][0]) == 1


def set_download(anime_name, enabled):
    """开/关某番的自动下载。"""
    return get_db().update(
        "UPDATE anime SET if_down = ? WHERE anime_name = ?",
        (1 if enabled else 0, anime_name),
    )


# ---------- anime_season：某番某季的季度 ----------

def ensure_season(anime_name, season, quarter):
    """第一次见到某 (番, 季) 就登记它的季度；已存在不动。返回 True 表示是新登记。"""
    return get_db().insert(
        "INSERT INTO anime_season (anime_name, season, quarter) VALUES (?, ?, ?)",
        (anime_name, season, quarter),
    )


def get_quarter(anime_name, season):
    """取某番某季的季度（决定下载文件夹）；没有返回 None。"""
    rows = get_db().query(
        "SELECT quarter FROM anime_season WHERE anime_name = ? AND season = ?",
        (anime_name, season),
    )
    return rows[0][0] if rows else None


# ---------- rss_torrent：每一集的种子 ----------

def add_torrent(item):
    """写入一集种子记录。返回 True=新记录，False=已存在。"""
    return get_db().insert(
        "INSERT INTO rss_torrent "
        "(torrent_url, rss_group, anime_title, number_of_words, status, season, release_time, torrent_from) "
        "VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
        (item.torrent_id, item.rss_group, item.anime_title, item.episode,
         item.season, item.release_time.strftime("%Y-%m-%d %H:%M:%S"), item.torrent_from),
    )


def mark_downloaded(torrent_id):
    return get_db().update(
        "UPDATE rss_torrent SET status = 1 WHERE torrent_url = ?", (torrent_id,)
    )


def pending(limit=None):
    """待下载的集（status=0），按发布时间倒序。limit=None 表示全部。"""
    sql = ("SELECT torrent_url, torrent_from, anime_title, number_of_words, season, release_time "
           "FROM rss_torrent WHERE status = 0 ORDER BY release_time DESC")
    params = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return get_db().query(sql, params)


def get_torrent(torrent_id):
    """按种子 id 取一条记录：(torrent_url, torrent_from, anime_title, number_of_words, season)。"""
    rows = get_db().query(
        "SELECT torrent_url, torrent_from, anime_title, number_of_words, season "
        "FROM rss_torrent WHERE torrent_url = ?",
        (torrent_id,),
    )
    return rows[0] if rows else None


def episode_counts():
    """每部番的进度：(anime_title, 已下集数, 总集数)。"""
    return get_db().query(
        "SELECT anime_title, SUM(status = 1) AS downloaded, COUNT(*) AS total "
        "FROM rss_torrent GROUP BY anime_title ORDER BY anime_title"
    )


# ---------- 汇总查询（给 manage.py / 以后的 UI 用） ----------

def list_anime():
    """已登记的番剧-季度：(quarter, season, anime_name, if_down)。"""
    return get_db().query(
        "SELECT s.quarter, s.season, s.anime_name, a.if_down "
        "FROM anime_season s JOIN anime a ON a.anime_name = s.anime_name "
        "ORDER BY s.quarter DESC, s.anime_name, s.season"
    )


def stats():
    """整体统计：(番总数, 开启下载的番数, 已下集数, 待下集数)。"""
    rows = get_db().query(
        "SELECT "
        "(SELECT COUNT(*) FROM anime), "
        "(SELECT COUNT(*) FROM anime WHERE if_down = 1), "
        "(SELECT COUNT(*) FROM rss_torrent WHERE status = 1), "
        "(SELECT COUNT(*) FROM rss_torrent WHERE status = 0)"
    )
    return rows[0] if rows else (0, 0, 0, 0)
