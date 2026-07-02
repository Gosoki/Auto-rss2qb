"""一次性迁移：把旧的 MySQL 库导入新的 SQLite（新 3 表结构）。

在能连到 MySQL 的内网机器上，DB_TYPE 保持 sqlite，运行一次：
    python migrate_mysql_to_sqlite.py
源 = config 里的 MYSQL_*（旧库），目标 = SQLITE_PATH（新库，脚本会自动建表）。

要点：
- 每个 (番, 季) 的季度用『该季最早一集的发布时间』重新推算，比旧 anime_quarter 更准，
  也顺便把返回番的不同季度分开。
- 种子的 status（已下/未下）原样保留，避免迁完又重下一遍。
- 建议先在空的 SQLite 上跑；重复的记录会自动跳过。
- 本脚本无法在没有 MySQL 的环境测试，跑前最好先备份。
"""
import pymysql

from config import MYSQL, SQLITE_PATH
from db import get_db
from rss import extract_quarter


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt is not None else None


def main():
    print(f"源 MySQL: {MYSQL['host']}:{MYSQL['port']}/{MYSQL['database']}")
    print(f"目标 SQLite: {SQLITE_PATH}")
    src = pymysql.connect(**MYSQL)
    dst = get_db()  # SQLite，自动建表
    cur = src.cursor()

    # 1) 每个 (番, 季) 的季度：用最早发布时间推算
    cur.execute("SELECT anime_title, season, MIN(release_time) FROM rss_torrent "
                "GROUP BY anime_title, season")
    seasons = cur.fetchall()
    for anime_title, season, min_release in seasons:
        dst.insert("INSERT INTO anime (anime_name, if_down) VALUES (?, 1)", (anime_title,))
        if min_release is None:
            continue
        dst.insert("INSERT INTO anime_season (anime_name, season, quarter) VALUES (?, ?, ?)",
                   (anime_title, season, extract_quarter(min_release)))
    print(f"季度记录: {len(seasons)} 条")

    # 2) 订阅开关：沿用旧 anime_quarter 的 if_down（同名只要有一季开着就算开）
    cur.execute("SELECT anime_name, MAX(if_down) FROM anime_quarter GROUP BY anime_name")
    subs = cur.fetchall()
    for anime_name, if_down in subs:
        dst.insert("INSERT INTO anime (anime_name, if_down) VALUES (?, 1)", (anime_name,))
        dst.update("UPDATE anime SET if_down = ? WHERE anime_name = ?",
                   (1 if str(if_down) == "1" else 0, anime_name))
    print(f"订阅开关: {len(subs)} 条")

    # 3) 种子记录：保留 status
    cur.execute("SELECT torrent_url, rss_group, anime_title, number_of_words, status, "
                "season, release_time, torrent_from FROM rss_torrent")
    torrents = cur.fetchall()
    migrated = 0
    for (torrent_url, rss_group, anime_title, number_of_words, status,
         season, release_time, torrent_from) in torrents:
        if dst.insert(
            "INSERT INTO rss_torrent (torrent_url, rss_group, anime_title, number_of_words, "
            "status, season, release_time, torrent_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (torrent_url, rss_group, anime_title, number_of_words,
             int(status) if status is not None else 0, season, _fmt(release_time), torrent_from),
        ):
            migrated += 1
    print(f"种子记录: {migrated}/{len(torrents)} 条（重复自动跳过）")

    cur.close()
    src.close()
    print("迁移完成 ✅")


if __name__ == "__main__":
    main()
