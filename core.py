"""主流程编排 + 给 UI 用的查询/操作函数。

管线：一条标准条目进来 → 按 info_hash 去重 → 登记番剧(番剧管理单位) →
入库种子 → 若该番开启下载且已确认，就取 .torrent 加进 qBittorrent。
"""
import logging
import os
import re

import httpx
from sqlmodel import func, select

import enrich
from config import DOWN_PATH, ENRICH_ENABLED, PROXY, QB_ENABLED
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
    # 1) 种子级去重：同一 hash 见过就跳过（跨源相等）
    with get_session() as s:
        if s.exec(select(Torrent).where(Torrent.info_hash == item.info_hash)).first() is not None:
            return False
        anime = _get_or_create_anime(s, item)
        anime_id = anime.id
        need_enrich = ENRICH_ENABLED and not anime.enriched

    # 2) 富集（可选，尽力而为，一部番只自动做一次）：拿真实放送日→季度、规范名
    if need_enrich:
        await _enrich_anime(anime_id, item.search_names, item.release_time, item.episode, item.info_hash)

    # 3) 入库种子（用可能已被富集修正过的季度），并决定是否下载
    with get_session() as s:
        anime = s.get(Anime, anime_id)
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


async def _enrich_anime(anime_id: int, names=None, release_time=None,
                        episode=None, info_hash=None) -> bool:
    """富集该番剧：用候选名搜 bgm（放送日校验），拿不到用 hash 兜底。

    enriched 置 True 表示『已尝试』——即便没拿到也不再每集重试；需要重来走 UI 的手动富集。
    """
    info = await enrich.resolve(names, release_time, episode, info_hash)
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None:
            return False
        a.enriched = True
        if info:
            if info.get("quarter"):
                a.quarter = info["quarter"]
            if info.get("bangumi_id"):
                a.bangumi_id = info["bangumi_id"]
            if info.get("display_name"):
                a.display_name = info["display_name"]
            if info.get("air_date"):
                a.air_date = info["air_date"]
        s.add(a)
        s.commit()

        # 跨源去重：同一 bgm_id 已有别的番剧 → 判定重复。ANi 源优先保留，另一条不下载
        # （解决 Mikan 各组把 ANi 已有的番当新番重复采进"待确认"）。
        bgm = a.bangumi_id
        if bgm is not None:
            other = s.exec(
                select(Anime).where(Anime.bangumi_id == bgm, Anime.id != a.id)
            ).first()
            if other is not None:
                loser = other if a.source_kind == "ani" else a
                keeper = a if loser is other else other
                if loser.merged_into is None and (loser.if_down or not loser.confirmed):
                    loser.confirmed = True
                    loser.if_down = False
                    loser.merged_into = keeper.id
                    s.add(loser)
                    for t in s.exec(select(Torrent).where(
                        Torrent.anime_title == loser.title,
                        Torrent.season == loser.season,
                        Torrent.status.in_(["pending", "error"]),
                    )):
                        t.status = "skipped"
                        s.add(t)
                    s.commit()
                    log.info("跨源重复(bgm=%s)：保留《%s》，跳过《%s》", bgm, keeper.title, loser.title)
    return bool(info)


async def download_torrent(torrent_id: int) -> bool:
    """取种子文件并加入 qBittorrent。成功返回 True。

    开头做一次原子占位（status→downloading）防止并发重复领取：只有 pending/error
    才会被下载；占位到 commit 之间没有 await，其它触发看到 downloading 会直接跳过。
    """
    if not QB_ENABLED:
        return False  # 无 qB 模式：只采集元数据，不发送种子（保持 pending）

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

    # 逻辑集去重：同一 (番,季,集) 已被别的源/组下过就跳过（只对可靠集数，不含特别篇/未知）
    if isinstance(episode, (int, float)) and episode >= 0:
        with get_session() as s:
            dup = s.exec(select(Torrent).where(
                Torrent.anime_title == title,
                Torrent.season == season,
                Torrent.episode == episode,
                Torrent.status == "downloaded",
                Torrent.id != torrent_id,
            )).first()
        if dup is not None:
            _set_status(torrent_id, "skipped")
            log.info("跳过重复集 - %s 第%s季 第%s集（已由其它来源下过）", title, season, episode)
            return False

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
        anime = s.exec(select(func.count()).select_from(Anime).where(Anime.merged_into.is_(None))).one()
        on = s.exec(select(func.count()).select_from(Anime).where(Anime.if_down == True)).one()  # noqa: E712
        done = s.exec(select(func.count()).select_from(Torrent).where(Torrent.status == "downloaded")).one()
        pending = s.exec(
            select(func.count()).select_from(Torrent).where(Torrent.status.in_(["pending", "error"]))
        ).one()
    return {"anime": anime, "on": on, "done": done, "pending": pending}


def list_anime() -> list[Anime]:
    """番剧管理用：只列保留的番（跨源重复被合并的不单独显示）。"""
    with get_session() as s:
        return list(s.exec(
            select(Anime).where(Anime.merged_into.is_(None)).order_by(Anime.quarter.desc(), Anime.title)
        ))


def multi_source_map() -> dict:
    """{保留番的 id: [来源...]}，仅含来源多于一个的番（管理页据此标『多源』）。

    按 bgm_id 把跨源重复归为同一部，聚合它们种子的来源。
    """
    from collections import defaultdict
    with get_session() as s:
        animes = list(s.exec(select(Anime)))
        pairs = list(s.exec(select(Torrent.anime_title, Torrent.season, Torrent.source)))
    ts_src: dict = defaultdict(set)
    for title, season, src in pairs:
        ts_src[(title, season)].add(src)

    def _key(a):
        return ("bgm", a.bangumi_id) if a.bangumi_id else ("t", a.title, a.season)

    grp_src: dict = defaultdict(set)
    grp_keeper: dict = {}
    for a in animes:
        k = _key(a)
        grp_src[k] |= ts_src.get((a.title, a.season), set())
        if a.merged_into is None:
            grp_keeper[k] = a.id
    return {kid: sorted(grp_src[k]) for k, kid in grp_keeper.items() if len(grp_src[k]) > 1}


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


def reject_anime(anime_id: int) -> None:
    """拒绝某个（非ANi发现的）番：移出待确认、不下载，并把它积压的待下种子标记跳过。"""
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None:
            return
        a.confirmed = True
        a.if_down = False
        s.add(a)
        for t in s.exec(select(Torrent).where(
            Torrent.anime_title == a.title,
            Torrent.season == a.season,
            Torrent.status.in_(["pending", "error"]),
        )):
            t.status = "skipped"
            s.add(t)
        s.commit()


async def enrich_anime(anime_id: int) -> bool:
    """手动富集某番剧（用它最近一条种子的 hash + 番名/发布时间做回退）。"""
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None:
            return False
        t = s.exec(
            select(Torrent).where(Torrent.anime_title == a.title, Torrent.season == a.season)
            .order_by(Torrent.created_at.desc())
        ).first()
        if t is None:
            return False
        info_hash, name, release_time, episode = t.info_hash, a.title, t.release_time, t.episode
    return await _enrich_anime(anime_id, [name], release_time, episode, info_hash)


async def download_pending_for_anime(anime_id: int) -> int:
    """把某番剧下 status=pending/error 的种子补下（人工确认后放行）。返回补下数量。

    加番剧级授权闸门：只对『已确认且开启下载』的番补下，避免绕过 if_down/confirmed。
    """
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None or not (a.if_down and a.confirmed):
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
