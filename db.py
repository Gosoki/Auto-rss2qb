"""数据库连接与执行层（支持 SQLite / MySQL 两种后端）。

- 统一用 `?` 占位符写 SQL（SQLite 原生；MySQL 后端自动转成 %s）。
- SQLite 后端启动时自动建表（见 SCHEMA），并开启 WAL + busy_timeout，
  这样常驻的 main 在写、manage.py 同时查也不会 "database is locked"。
- release_time 统一以字符串存储，两种后端都兼容、可排序。

只负责『怎么连、怎么安全执行』；具体查什么、改什么在 repo.py。
"""
from config import DB_TYPE, MYSQL, SQLITE_PATH
from logger import log

# 3 张表：
#   anime         订阅表：一部番（不含季）要不要下载
#   anime_season  季度表：某番的某一季属于哪个季度（决定下载文件夹）
#   rss_torrent   种子表：每一集
SCHEMA = """
CREATE TABLE IF NOT EXISTS anime (
    anime_name TEXT PRIMARY KEY,
    if_down    INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS anime_season (
    anime_name TEXT NOT NULL,
    season     INTEGER NOT NULL,
    quarter    TEXT NOT NULL,
    PRIMARY KEY (anime_name, season)
);
CREATE TABLE IF NOT EXISTS rss_torrent (
    torrent_url     TEXT PRIMARY KEY,
    rss_group       TEXT,
    anime_title     TEXT,
    number_of_words INTEGER,
    status          INTEGER NOT NULL DEFAULT 0,
    season          INTEGER,
    release_time    TEXT,
    torrent_from    TEXT
);
"""


class Database:
    """后端基类：insert/update/query 三个方法对两种后端行为一致。"""

    integrity_errors = ()

    def _cursor(self):
        raise NotImplementedError

    def _prepare(self, sql):
        return sql  # 占位符转换，子类按需覆盖

    def _rollback(self):
        try:
            self._conn.rollback()
        except Exception:
            pass

    def insert(self, sql, params=None):
        """执行 INSERT。成功 True；主键/唯一键重复 False（正常情况，不记错误）；其它错误记录并返回 False。"""
        try:
            cursor = self._cursor()
            cursor.execute(self._prepare(sql), params or ())
            self._conn.commit()
            cursor.close()
            return True
        except self.integrity_errors:
            self._rollback()
            return False
        except Exception as e:
            self._rollback()
            log.error(f"数据库写入失败: {e} | SQL: {sql} | 参数: {params}")
            return False

    def update(self, sql, params=None):
        try:
            cursor = self._cursor()
            cursor.execute(self._prepare(sql), params or ())
            self._conn.commit()
            cursor.close()
            return True
        except Exception as e:
            self._rollback()
            log.error(f"数据库更新失败: {e} | SQL: {sql} | 参数: {params}")
            return False

    def query(self, sql, params=None):
        """执行 SELECT，返回结果元组列表；出错返回空列表。"""
        try:
            cursor = self._cursor()
            cursor.execute(self._prepare(sql), params or ())
            rows = cursor.fetchall()
            cursor.close()
            return rows
        except Exception as e:
            log.error(f"数据库查询失败: {e} | SQL: {sql} | 参数: {params}")
            return []


class SqliteDatabase(Database):
    def __init__(self):
        import sqlite3
        self._conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")     # 允许读写并发
        self._conn.execute("PRAGMA busy_timeout=5000")    # 遇锁最多等 5 秒
        self._conn.executescript(SCHEMA)                  # 自动建表
        self._conn.commit()
        self.integrity_errors = (sqlite3.IntegrityError,)

    def _cursor(self):
        return self._conn.cursor()


class MysqlDatabase(Database):
    def __init__(self):
        import pymysql
        self._pymysql = pymysql
        self._conn = pymysql.connect(**MYSQL)
        self.integrity_errors = (pymysql.err.IntegrityError,)

    def _cursor(self):
        try:
            self._conn.ping(reconnect=True)  # 空闲后连接失效时自动重连
        except Exception:
            self._conn = self._pymysql.connect(**MYSQL)
        return self._conn.cursor()

    def _prepare(self, sql):
        return sql.replace("?", "%s")  # 我们的 SQL 里不含字面量 ?，直接替换安全


_db = None


def get_db():
    global _db
    if _db is None:
        _db = SqliteDatabase() if DB_TYPE == "sqlite" else MysqlDatabase()
    return _db
