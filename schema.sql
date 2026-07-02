-- 参考建表语句。
-- SQLite 会在启动时自动建表（见 db.py 的 SCHEMA），此文件主要给 DB_TYPE=mysql 时手动执行。
-- 下面是 MySQL 版（SQLite 用 TEXT/INTEGER 即可，见 db.py）。

CREATE TABLE anime (                            -- 订阅表：一部番要不要下载
  anime_name VARCHAR(255) PRIMARY KEY,
  if_down    TINYINT NOT NULL DEFAULT 1         -- 1=下载 0=跳过
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE anime_season (                     -- 季度表：某番某季属于哪个季度
  anime_name VARCHAR(255) NOT NULL,
  season     INT NOT NULL,
  quarter    VARCHAR(8) NOT NULL,               -- 决定下载文件夹，如 24C
  PRIMARY KEY (anime_name, season)
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE rss_torrent (                      -- 种子表：每一集
  torrent_url     VARCHAR(255) PRIMARY KEY,     -- 种子 id
  rss_group       VARCHAR(64),
  anime_title     VARCHAR(255),
  number_of_words DECIMAL(6,1),                 -- 集数（-1特别篇 / -2未知；DECIMAL 才能存 11.5 这类小数集）
  status          TINYINT NOT NULL DEFAULT 0,   -- 0未下 1已加入qB
  season          INT,
  release_time    DATETIME,
  torrent_from    VARCHAR(64)
) DEFAULT CHARSET=utf8mb4;
