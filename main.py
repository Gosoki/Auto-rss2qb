"""主流程编排：抓取订阅 -> 登记 -> 入库 -> 下载种子加入 qBittorrent。

登记跟集数无关：第一次见到某 (番, 季) 就登记它（ensure_anime / ensure_season），
所以第0话、特别篇、第二季从第15话开始，都能被正确登记并下载。
数据表见 db.py 的 SCHEMA，SQL 全在 repo.py。
"""
import os
import re
import time

import requests

from config import (
    DOWN_PATH,
    POLL_DOWNLOAD_LIMIT,
    PROXIES,
    STARTUP_DOWNLOAD_LIMIT,
    STOP_TIME,
    TORRENT_PATH,
    TRY_INSERT_ALL,
)
from logger import log
from notify import notify
from qbittorrent import QBittorrent
from rss import SOURCES, torrent_download_url
import repo

qb = QBittorrent()

# 文件名里不允许出现的字符，拼进保存路径前替换掉，避免建目录失败
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*]')


def _safe_name(name):
    return _ILLEGAL_CHARS.sub("_", name).strip()


def register(item):
    """第一次见到某番/某季就登记（幂等，跟集数无关）。"""
    repo.ensure_anime(item.anime_title)
    if repo.ensure_season(item.anime_title, item.season, item.quarter):
        log.info(f"新登记 - {item.quarter} - {item.anime_title} 第{item.season}季")
        # 只在看起来是首播（第0/1集或特别篇）时才发通知，避免中途集数刷屏
        if item.episode in (0, 1, -1):
            notify(f"{item.quarter}新番 - {item.anime_title} 第{item.season}季 😍")


def download(torrent_id, torrent_from, anime_title, episode, season, is_new=False, force=False):
    """取种子文件并加入 qBittorrent。

    默认只下载『订阅开启』的番；force=True（手动下载）时忽略该开关。
    """
    if not force and not repo.is_subscribed(anime_title):
        return  # 该番被标记为不下载

    quarter = repo.get_quarter(anime_title, season)
    if quarter is None:
        if is_new:
            log.warning(f"无季度记录 - {anime_title} - 第{season}季 - 第{episode}集")
        return

    url = torrent_download_url(torrent_from, torrent_id)
    if not url:
        log.error(f"未知种子来源 {torrent_from}，无法下载 {anime_title}")
        return

    try:
        resp = requests.get(url, proxies=PROXIES, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(f"下载种子失败 - {anime_title} - 原因：{e}")
        return

    os.makedirs(TORRENT_PATH, exist_ok=True)
    torrent_file = os.path.join(TORRENT_PATH, f"{torrent_id}.torrent")
    try:
        with open(torrent_file, "wb") as f:
            f.write(resp.content)
    except OSError as e:
        log.error(f"保存种子文件失败 - 原因：{e}")
        return

    try:
        save_path = f"{DOWN_PATH}/{quarter}/{_safe_name(anime_title)}/Season {season}"
        if qb.add_torrent(save_path, torrent_file, quarter):
            log.info(f"添加QB成功 - {anime_title} - 第{season}季 - 第{episode}集")
            repo.mark_downloaded(torrent_id)
            notify(f"{anime_title}[{episode}] 📥")
    finally:
        if os.path.exists(torrent_file):
            os.remove(torrent_file)


def download_pending(limit):
    """把 rss_torrent 中 status=0 的种子补下（按发布时间倒序）。"""
    for torrent_id, torrent_from, anime_title, episode, season, _release in repo.pending(limit):
        download(torrent_id, torrent_from, anime_title, episode, season)


def process_source(source):
    """抓取单个订阅源并处理新条目。"""
    for item in source.fetch_items():
        register(item)

        if repo.add_torrent(item):
            log.is_sleep = False
            log.info(
                f"已更新订阅 - {item.rss_group} - {item.anime_title} - "
                f"第{item.season}季 - 第{item.episode}集"
            )
            try:
                download(item.torrent_id, item.torrent_from, item.anime_title,
                         item.episode, item.season, is_new=True)
            except Exception as e:
                log.error(
                    f"添加QB失败 - {item.anime_title} - 第{item.season}季 - "
                    f"第{item.episode}集 ,原因：{e}"
                )
        elif not TRY_INSERT_ALL:
            # 遇到已存在的条目：后面的更旧，通常都已处理过，进入休眠
            log.info(f"订阅无新内容,进入休眠,{STOP_TIME}秒后尝试更新")
            log.is_sleep = True
            return


def run():
    log.info("程序启动")
    log.info("尝试下载已入库番剧")
    download_pending(STARTUP_DOWNLOAD_LIMIT)
    log.info("完成已入库番剧下载")
    notify("程序启动，完成已入库番剧下载")

    while True:
        try:
            for source in SOURCES:
                process_source(source)
            download_pending(POLL_DOWNLOAD_LIMIT)
        except Exception as e:
            # 常驻进程：单轮出错只记录并继续，不让整个程序退出
            log.error(f"本轮处理异常: {e}")
        time.sleep(STOP_TIME)


if __name__ == "__main__":
    run()
