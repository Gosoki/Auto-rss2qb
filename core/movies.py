"""剧场版 / OVA 逻辑（与 TV 番剧 anime.py 完全分离，只共用 engine 底层）。

来源仅 Mikan 季度浏览页的『剧场版/OVA 桶』——不碰 TV 那边的订阅源。识别用 bgm。
『是不是电影以 Mikan 桶为准』：桶里的一律当剧场版/OVA 收进 Movie（哪怕 bgm 把类型识别成 TV，
也只是详情页的 bgm 元数据，不改变它在剧场版列表里，也不转去番剧表）。剧场版一部作品逐版本人工点下。
"""
import asyncio
import logging
from collections import Counter
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

import config
from core import engine
from db import get_session
from db.models import AnimeTorrent, Movie, MovieTorrent
from services import enrich
from services.notify import notify
from sources import mikan

log = logging.getLogger("autorss")

_dl_lock = asyncio.Lock()  # 串行化剧场版下载，防同一片并发重复交 qB


def _has_downloads(s, movie_id: int) -> bool:
    return s.exec(select(MovieTorrent).where(
        MovieTorrent.movie_id == movie_id,
        MovieTorrent.status.in_(["downloading", "downloaded"]))).first() is not None


def _merge_movie(s, loser_id: int, keeper_id: int) -> None:
    """同一 bgm_id 裂成多条时合并：把 loser 的种子并到 keeper 并删 loser。

    剧场版只有『忽略(rejected)』一个人工状态（逐版本手动下，无审批/首选源），故合并时两方任一被忽略则仍忽略。
    """
    if loser_id == keeper_id:
        return
    keeper = s.get(Movie, keeper_id)
    loser = s.get(Movie, loser_id)
    if keeper is not None and loser is not None:
        keeper.rejected = keeper.rejected or loser.rejected
        s.add(keeper)
    for t in s.exec(select(MovieTorrent).where(MovieTorrent.movie_id == loser_id)):
        t.movie_id = keeper_id
        s.add(t)
    if loser is not None:
        s.delete(loser)
    s.commit()


# ---------------- 发现（Mikan 季度剧场版/OVA 桶 + bgm 识别） ----------------

def _upsert_movie(mikan_id: str, title: str, bgm_id: int | None,
                  info: dict | None, label: str) -> tuple[int, bool]:
    """按 bgm_id（无则 mikan_id）定位/新建 Movie，写入 bgm 元数据。返回 (movie_id, 是否新建)。

    是不是电影以 Mikan 桶为准（mikan_type，列表徽标用）；platform 存 bgm 的类型（详情页展示，跟 bgm）。
    """
    mikan_type = ("剧场版" if ("剧场" in label or "劇場" in label)
                  else "OVA" if ("OVA" in label or "OAD" in label or "OAV" in label) else "剧场版")
    with get_session() as s:
        movie = None
        if bgm_id is not None:
            movie = s.exec(select(Movie).where(Movie.bangumi_id == bgm_id)).first()
        if movie is None:
            movie = s.exec(select(Movie).where(Movie.mikan_id == mikan_id)).first()
        is_new = movie is None
        if movie is None:
            movie = Movie(title=title, mikan_id=mikan_id)
        movie.mikan_id = mikan_id
        movie.mikan_type = mikan_type   # Mikan 桶判定（剧场版/OVA），列表徽标用
        if not movie.display_name:
            movie.display_name = title  # 无 bgm 时先用 Mikan 展示名兜底
        engine.apply_bgm_meta(movie, info, keep_quarter=(not is_new and _has_downloads(s, movie.id)))
        s.add(movie)
        s.commit()
        s.refresh(movie)
        if movie.bangumi_id is not None:  # 身份守卫：同 bgm_id 合并
            for other in list(s.exec(select(Movie).where(
                    Movie.bangumi_id == movie.bangumi_id, Movie.id != movie.id))):
                _merge_movie(s, other.id, movie.id)
        return movie.id, is_new


def _store_movie_torrents(movie_id: int, items: list) -> int:
    """把某剧场版番组抓来的种子入库（按 hash 去重、逐条提交），全部 pending。返回新增数。"""
    n = 0
    with get_session() as s:
        for item in items:
            if s.exec(select(MovieTorrent).where(MovieTorrent.info_hash == item.info_hash)).first():
                continue
            s.add(MovieTorrent(
                info_hash=item.info_hash, movie_id=movie_id, source=item.source,
                site=item.site, raw_title=item.raw_title, download_url=item.download_url,
                release_time=item.release_time, priority=item.priority, status="pending",
            ))
            try:
                s.commit()
                n += 1
            except IntegrityError:
                s.rollback()
    return n


async def discover_movies(year: int, seasons: list[str] | None = None) -> dict:
    """扫描 Mikan 指定年份/季度的剧场版·OVA 桶，识别(bgm)入库为 Movie 并抓其种子。

    seasons：['A','B','C','D'] 子集（冬春夏秋），None=全年四季。
    『是不是电影』以 Mikan 桶为准——桶里的一律当剧场版/OVA 收进 Movie（哪怕 bgm 把类型识别成 TV，
    也只是详情页的 bgm 元数据，不改变它在剧场版列表里）。本函数只碰 Movie/MovieTorrent，不写 TV 表。
    返回 {'movies','torrents','seen','errors'}。
    """
    seasons = seasons or ["A", "B", "C", "D"]
    added_movies = added_torrents = seen = errors = 0
    async with mikan.make_client() as client:
        for letter in seasons:
            try:
                bucket = await mikan.discover_movie_bucket(client, year, letter)
            except Exception as e:
                log.error("发现剧场版失败 %s%s: %s", year, letter, e)
                errors += 1
                continue
            log.info("Mikan %s年%s 剧场版/OVA 桶：%d 部",
                     year, mikan.season_cn(letter), len(bucket))
            for mikan_id, title, mlabel in bucket:
                try:
                    bgm_id = await mikan.fetch_detail(client, mikan_id)
                    info = await enrich.fetch_by_id(bgm_id) if bgm_id is not None else None
                    movie_id, is_new = _upsert_movie(mikan_id, title, bgm_id, info, mlabel)
                    seen += 1
                    added_movies += 1 if is_new else 0
                    items = await mikan.fetch_bangumi_torrents(client, mikan_id)
                    added_torrents += _store_movie_torrents(movie_id, items)
                except Exception as e:
                    log.error("处理剧场版失败 mikan=%s(%s): %s", mikan_id, title, e)
                    errors += 1
    log.info("剧场版发现完成 %s：命中 %d/新增 %d/种子 %d，出错 %d",
             year, seen, added_movies, added_torrents, errors)
    return {"movies": added_movies, "torrents": added_torrents, "seen": seen, "errors": errors}


async def scan_now(year: int, seasons: list[str] | None = None) -> dict:
    """扫描一次并记下扫描时间（手动『立即扫描』与后台自动扫描共用）。只碰剧场版，不涉 TV。"""
    res = await discover_movies(year, seasons)
    # 只有覆盖当年四季的完整扫描才刷新自动扫描时间基准；手动只扫单季(回填历史)不该顶掉它、推迟自动全年扫。
    if seasons is None or set(seasons) >= {"A", "B", "C", "D"}:
        config.set_many({"MOVIE_SCAN_LAST": datetime.now().isoformat(timespec="seconds")})
    return res


async def auto_scan_tick() -> bool:
    """后台心跳调用：开了自动扫描且到点（距上次 ≥ MOVIE_SCAN_INTERVAL）就扫当年四季。扫了返回 True。"""
    if not config.MOVIE_SCAN_ENABLED:
        return False
    last = config.MOVIE_SCAN_LAST
    if last:
        try:
            elapsed = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
            if 0 <= elapsed < config.MOVIE_SCAN_INTERVAL:
                return False  # 还没到点（elapsed<0=系统时钟被回拨，视作到点、照扫，自愈不停摆）
        except ValueError:
            pass
    await scan_now(datetime.now().year)
    return True


# ---------------- 查询（给 /movies 页） ----------------

def overview() -> dict:
    """/movies 仪表盘的聚合数据：KPI + 各季度(电影数/已下) + qB 实时态。"""
    with get_session() as s:
        all_m = list(s.exec(select(Movie)))
        pairs = list(s.exec(select(MovieTorrent.movie_id, MovieTorrent.status)))
    active = [m for m in all_m if not m.rejected]
    active_ids = {m.id for m in active}
    q_of = {m.id: (m.quarter or "未知") for m in active}
    dl_ids = {mid for mid, st in pairs if st == "downloaded"}
    total_by_q = Counter(q_of[m.id] for m in active)
    dl_by_q = Counter(q_of[mid] for mid in dl_ids if mid in q_of)
    qs = sorted((q for q in total_by_q if q != "未知"), reverse=True)
    if "未知" in total_by_q:
        qs.append("未知")
    status = Counter(st for _, st in pairs)
    return {
        "kpi": {
            "total": len(active),
            "matched": sum(1 for m in active if m.bangumi_id),
            "unmatched": sum(1 for m in active if not m.bangumi_id),
            "downloaded": len([mid for mid in dl_ids if mid in active_ids]),
            "rejected": sum(1 for m in all_m if m.rejected),
            "versions": len(pairs),
        },
        "by_quarter": [(q, total_by_q.get(q, 0), dl_by_q.get(q, 0)) for q in qs],
        "status": {k: status.get(k, 0) for k in
                   ("downloaded", "downloading", "pending", "error", "skipped")},
        "qb": engine.qb_summary(MovieTorrent),
        "config": {"qb": config.QB_ENABLED},
    }


def list_unmatched_movies() -> list[Movie]:
    """未识别（bgm 没匹配上）的剧场版/OVA——供『待识别』tab 手动绑定。"""
    with get_session() as s:
        return list(s.exec(select(Movie).where(
            Movie.bangumi_id.is_(None), Movie.rejected.is_not(True))
            .order_by(Movie.created_at.desc())))


def list_movies() -> list[Movie]:
    """未忽略的剧场版/OVA（/movies 页展示）。"""
    with get_session() as s:
        return list(s.exec(select(Movie).where(Movie.rejected.is_not(True))
                           .order_by(Movie.quarter.desc(), Movie.id)))


def list_rejected_movies() -> list[Movie]:
    """已忽略的剧场版/OVA（/movies 页底部『已忽略』区展示，可恢复）。"""
    with get_session() as s:
        return list(s.exec(select(Movie).where(Movie.rejected == True)  # noqa: E712
                           .order_by(Movie.quarter.desc(), Movie.id)))


def get_movie(movie_id: int) -> Movie | None:
    with get_session() as s:
        return s.get(Movie, movie_id)


def movie_torrents(movie_id: int) -> list[MovieTorrent]:
    with get_session() as s:
        return list(s.exec(select(MovieTorrent).where(MovieTorrent.movie_id == movie_id)
                           .order_by(MovieTorrent.created_at.desc())))


def movie_sources(movie_id: int) -> list[str]:
    with get_session() as s:
        rows = s.exec(select(MovieTorrent.source).where(MovieTorrent.movie_id == movie_id)).all()
    return sorted({r for r in rows if r})


def torrents_by_movie(movie_ids: list[int]) -> dict[int, list[MovieTorrent]]:
    """一次查出多部片的种子，按 movie_id 归组（列表页批量渲染用，免得每张卡片各查 2 次库=N+1）。"""
    if not movie_ids:
        return {}
    out: dict[int, list[MovieTorrent]] = {}
    with get_session() as s:
        for t in s.exec(select(MovieTorrent).where(MovieTorrent.movie_id.in_(movie_ids))
                        .order_by(MovieTorrent.created_at.desc())):
            out.setdefault(t.movie_id, []).append(t)
    return out


def recent_movie_rows(limit: int = 50) -> list[dict]:
    """新入库列表：剧场版/OVA 种子 + 片的规范名（比原始种子标题可读）+ 原始种子标题。

    MovieTorrent 表只含剧场版/OVA 种子（TV 在 AnimeTorrent），故无需再过滤。
    """
    with get_session() as s:
        ts = list(s.exec(select(MovieTorrent).order_by(MovieTorrent.created_at.desc()).limit(limit)))
        ids = {t.movie_id for t in ts if t.movie_id}
        names = ({m.id: (m.display_name or m.title) for m in
                  s.exec(select(Movie).where(Movie.id.in_(ids)))} if ids else {})
    return [{
        "id": t.id,
        "time": engine.torrent_time(t),
        "name": names.get(t.movie_id) or (t.raw_title or "?"),
        "source": t.source,
        "status": t.status,
        "raw": t.raw_title or "",
    } for t in ts]


# ---------------- 操作（给 /movies 页 + 详情） ----------------

def reject_movie(movie_id: int) -> None:
    with get_session() as s:
        m = s.get(Movie, movie_id)
        if m is None:
            return
        m.rejected = True
        s.add(m)
        for t in s.exec(select(MovieTorrent).where(
                MovieTorrent.movie_id == movie_id, MovieTorrent.status.in_(["pending", "error"]))):
            t.status = "skipped"
            s.add(t)
        s.commit()


def restore_movie(movie_id: int) -> None:
    with get_session() as s:
        m = s.get(Movie, movie_id)
        if m is None:
            return
        m.rejected = False
        s.add(m)
        rows = list(s.exec(select(MovieTorrent).where(MovieTorrent.movie_id == movie_id)))
        anydl = any(t.status in ("downloaded", "downloading") for t in rows)
        for t in rows:  # 剧场版=一部作品：已有一版就别把 skipped 旧版翻出来（deleted 是用户主动删，也不重下）
            if t.status == "skipped" and not anydl:
                t.status = "pending"
                s.add(t)
        s.commit()


async def enrich_movie(movie_id: int) -> bool:
    """手动重识别某剧场版：用已有名字 + 最近一条种子回退，重取 bgm 元数据覆盖。"""
    with get_session() as s:
        m = s.get(Movie, movie_id)
        if m is None:
            return False
        t = s.exec(select(MovieTorrent).where(MovieTorrent.movie_id == movie_id)
                   .order_by(MovieTorrent.created_at.desc())).first()
        names = [n for n in (m.display_name, m.jp_name, m.title) if n]
        info_hash = t.info_hash if t else None
        release_time = t.release_time if t else None
    info = await enrich.resolve(names, release_time, None, info_hash)
    with get_session() as s:
        m = s.get(Movie, movie_id)
        if m is None:
            return False
        engine.apply_bgm_meta(m, info, keep_quarter=_has_downloads(s, movie_id))
        s.add(m)
        s.commit()
        if m.bangumi_id is not None:
            for other in list(s.exec(select(Movie).where(
                    Movie.bangumi_id == m.bangumi_id, Movie.id != m.id))):
                _merge_movie(s, other.id, m.id)
    return bool(info)


async def bind_movie_bgm(movie_id: int, bgm_id: int) -> bool:
    """手动把剧场版绑定到指定 bgm subject id：取元数据覆盖 + 身份合并。"""
    info = await enrich.fetch_by_id(bgm_id)
    if not info:
        return False
    with get_session() as s:
        m = s.get(Movie, movie_id)
        if m is None:
            return False
        engine.apply_bgm_meta(m, info, keep_quarter=_has_downloads(s, movie_id))
        s.add(m)
        s.commit()
        for other in list(s.exec(select(Movie).where(
                Movie.bangumi_id == bgm_id, Movie.id != m.id))):
            _merge_movie(s, other.id, m.id)
    return True


# ---------------- 下载 ----------------

def _set_status(mt_id: int, status: str) -> None:
    engine.set_torrent_status(MovieTorrent, mt_id, status)


def reset_downloading() -> None:
    """启动时把上次遗留的 downloading 复位为 pending。"""
    engine.reset_downloading(MovieTorrent)


async def download_movie_torrent(mt_id: int) -> bool:
    """强制下某一版本到 qB（详情页逐条下用）。剧场版不建 Season 子目录。成功返回 True。"""
    if not config.QB_ENABLED:
        return False
    async with _dl_lock:
        with get_session() as s:
            t = s.get(MovieTorrent, mt_id)
            if t is None or t.status in ("downloading", "downloaded"):
                return False  # 已在下/已下 → 幂等短路，防并发（详情页多次点同一版本）重复交 qB
            # 跨表守卫：同一物理种子已被 TV 管线拿去下/下完 → 文件已在 qB，别用不同路径重复提交。
            if engine.hash_owned_elsewhere(t.info_hash, AnimeTorrent):
                t.status = "downloaded"
                s.add(t)
                s.commit()
                log.info("跳过跨表重复种子（TV 已持有）- movie torrent=%s", mt_id)
                return True
            m = s.get(Movie, t.movie_id)
            t.status = "downloading"
            s.add(t)
            s.commit()
            url = t.download_url
            quarter = (m.quarter if m else "") or "unknown"
            folder = (m and (m.jp_name or m.display_name)) or t.raw_title or "movie"

    save_path = engine.build_save_path(quarter, folder, top="剧场版", root=config.MOVIE_DOWN_PATH)
    if save_path is None:
        log.error("拒绝越界保存路径 - movie torrent %s", mt_id)
        _set_status(mt_id, "error")
        return False
    try:
        data = await engine.fetch_torrent_bytes(url)
        ok = await engine.add_to_qb(data, save_path, f"autoRSS-movie {quarter}", quarter)
    except asyncio.CancelledError:
        _set_status(mt_id, "pending")
        raise
    except Exception as e:
        log.error("剧场版下载失败 - %s", e)
        _set_status(mt_id, "error")
        return False
    _set_status(mt_id, "downloaded" if ok else "error")
    if ok:
        log.info("已加入qB（剧场版）- torrent=%s", mt_id)
        await notify(f"{folder} 🎬📥")
    return ok


async def delete_movie_torrent(mt_id: int) -> bool:
    """删除单条剧场版种子在 qB 里的文件（走 qB 接口），标记回 skipped。

    若同一 hash TV 管线还在用，则只脱手本行、不删 qB/文件，免得毁了对面。
    """
    with get_session() as s:
        t = s.get(MovieTorrent, mt_id)
        if t is None or t.status not in ("downloaded", "downloading"):
            return False
        h = t.info_hash
    if engine.hash_owned_elsewhere(h, AnimeTorrent):
        _set_status(mt_id, "deleted")  # TV 侧还持有同一种子 → 只脱手，不删文件
        return True
    if not await engine.qb.delete([h], delete_files=True):
        return False
    _set_status(mt_id, "deleted")   # 用户主动删除：终态，恢复时不会被重新下（区别于 skipped）
    log.info("删除文件（剧场版单条）- torrent=%s", mt_id)
    return True


async def sync_qb_status() -> int:
    """从 qB 同步剧场版种子实时态。"""
    return await engine.sync_qb_status(MovieTorrent)
