"""主流程编排 + 给 UI 用的查询/操作函数。

管线：一条标准条目进来 → 按 info_hash 去重 → 登记番剧(番剧管理单位) →
入库种子 → 若该番开启下载且已确认，就取 .torrent 加进 qBittorrent。
"""
import logging
import os
import re

import httpx
from sqlmodel import func, select

from config import DOWN_PATH, PROXY
from db import get_session
from models import Anime, Torrent
from notify import notify
from qbittorrent import QBittorrent

log = logging.getLogger("autorss")
qb = QBittorrent()

_ILLEGAL = re.compile(r'[<>:"/\\|?*]')


def _safe(name: str) -> str:
    return _ILLEGAL.sub("_", name).strip()


# ---------------- 管线 ----------------

def _get_or_create_anime(session, item) -> Anime:
    anime = session.exec(
        select(Anime).where(Anime.title == item.anime_title, Anime.season == item.season)
    ).first()
    if anime is not None:
        return anime
    anime = Anime(
        title=item.anime_title,
        season=item.season,
        quarter=item.quarter,
        if_down=(item.source_kind == "ani"),      # 非ANi 默认不下
        confirmed=(item.source_kind == "ani"),    # 非ANi 默认待确认
        source_kind=item.source_kind,
    )
    session.add(anime)
    session.commit()
    session.refresh(anime)
    return anime


async def process_item(item) -> bool:
    """处理一条标准条目。返回 True 表示是新种子（之前没见过）。"""
    with get_session() as s:
        seen = s.exec(select(Torrent).where(Torrent.info_hash == item.info_hash)).first()
        if seen is not None:
            return False  # 同一种子（可能来自另一个源）已见过

        anime = _get_or_create_anime(s, item)
        torrent = Torrent(
            info_hash=item.info_hash,
            source=item.source,
            site=item.site,
            anime_title=item.anime_title,
            season=item.season,
            episode=item.episode,
            quarter=anime.quarter,
            download_url=item.download_url,
            release_time=item.release_time,
            status="pending",
        )
        s.add(torrent)
        s.commit()
        s.refresh(torrent)
        torrent_id = torrent.id
        should_download = anime.if_down and anime.confirmed

    log.info("新增 - %s - %s 第%s季 第%s集", item.source, item.anime_title, item.season, item.episode)
    if should_download:
        await download_torrent(torrent_id)
    return True


async def download_torrent(torrent_id: int) -> bool:
    """取种子文件并加入 qBittorrent。成功返回 True。

    开头做一次原子占位（status→downloading）防止并发重复领取：只有 pending/error
    才会被下载；占位到 commit 之间没有 await，其它触发看到 downloading 会直接跳过。
    """
    with get_session() as s:
        t = s.get(Torrent, torrent_id)
        if t is None or t.status not in ("pending", "error"):
            return False  # 不存在 / 已下过 / 正在下
        t.status = "downloading"
        s.add(t)
        s.commit()
        url = t.download_url
        quarter = t.quarter or "unknown"
        title = t.anime_title
        season = t.season
        episode = t.episode

    kwargs = {"timeout": 60, "follow_redirects": True}
    if PROXY:
        kwargs["proxy"] = PROXY
    try:
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content

        save_path = f"{DOWN_PATH}/{quarter}/{_safe(title)}/Season {season}"
        try:  # 尽力创建目录（跨用户的 qB 需要），失败不阻断
            os.makedirs(save_path, exist_ok=True)
            os.chmod(save_path, 0o777)
        except OSError:
            pass

        ok = await qb.add_torrent(data, save_path, f"autoRSS {quarter}", quarter)
    except Exception as e:  # 任何失败都回写 error，避免卡在 downloading
        log.error("下载失败 - %s - %s", title, e)
        _set_status(torrent_id, "error")
        return False

    _set_status(torrent_id, "downloaded" if ok else "error")
    if ok:
        log.info("已加入qB - %s 第%s季 第%s集", title, season, episode)
        await notify(f"{title}[{episode}] 📥")
    return ok


def _set_status(torrent_id: int, status: str) -> None:
    with get_session() as s:
        t = s.get(Torrent, torrent_id)
        if t is not None:
            t.status = status
            s.add(t)
            s.commit()


def reset_downloading() -> None:
    """启动时把上次异常退出遗留的 downloading 复位为 pending，好被重新下。"""
    with get_session() as s:
        for t in s.exec(select(Torrent).where(Torrent.status == "downloading")):
            t.status = "pending"
            s.add(t)
        s.commit()


# ---------------- 给 UI 的查询 ----------------

def get_stats() -> dict:
    with get_session() as s:
        anime = s.exec(select(func.count()).select_from(Anime)).one()
        on = s.exec(select(func.count()).select_from(Anime).where(Anime.if_down == True)).one()  # noqa: E712
        done = s.exec(select(func.count()).select_from(Torrent).where(Torrent.status == "downloaded")).one()
        pending = s.exec(
            select(func.count()).select_from(Torrent).where(Torrent.status.in_(["pending", "error"]))
        ).one()
    return {"anime": anime, "on": on, "done": done, "pending": pending}


def list_anime() -> list[Anime]:
    with get_session() as s:
        return list(s.exec(select(Anime).order_by(Anime.quarter.desc(), Anime.title)))


def pending_confirm() -> list[Anime]:
    with get_session() as s:
        return list(s.exec(select(Anime).where(Anime.confirmed == False)))  # noqa: E712


def list_torrents(limit: int = 50) -> list[Torrent]:
    with get_session() as s:
        return list(s.exec(select(Torrent).order_by(Torrent.created_at.desc()).limit(limit)))


# ---------------- 给 UI 的操作 ----------------

def set_if_down(anime_id: int, value: bool) -> None:
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is not None:
            a.if_down = value
            s.add(a)
            s.commit()


def confirm_anime(anime_id: int) -> None:
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is not None:
            a.confirmed = True
            a.if_down = True
            s.add(a)
            s.commit()


async def download_pending_for_anime(anime_id: int) -> int:
    """把某番剧下 status=pending 的种子补下（用于人工确认后放行）。返回补下数量。"""
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None:
            return 0
        rows = list(s.exec(
            select(Torrent).where(
                Torrent.anime_title == a.title,
                Torrent.season == a.season,
                Torrent.status.in_(["pending", "error"]),
            )
        ))
        ids = [t.id for t in rows]
    for tid in ids:
        await download_torrent(tid)
    return len(ids)


async def download_all_pending() -> int:
    """补下所有『已订阅且已确认』番剧的待下/失败种子。返回补下数量。"""
    with get_session() as s:
        pend = list(s.exec(select(Torrent).where(Torrent.status.in_(["pending", "error"]))))
        ids = []
        for t in pend:
            a = s.exec(
                select(Anime).where(Anime.title == t.anime_title, Anime.season == t.season)
            ).first()
            if a is not None and a.if_down and a.confirmed:
                ids.append(t.id)
    for tid in ids:
        await download_torrent(tid)
    return len(ids)
