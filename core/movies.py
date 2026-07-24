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
from sqlmodel import func, select

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
    # 整轮网络故障（一部没命中且有报错）不刷新基准——否则一次抓取失败就要等满一个间隔才重试；留给 5 分钟心跳重扫。
    total_fail = res["seen"] == 0 and res["errors"] > 0
    if (seasons is None or set(seasons) >= {"A", "B", "C", "D"}) and not total_fail:
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
        # 种子维度用 SQL 聚合（不整表拉进内存）
        status = {st: c for st, c in s.exec(
            select(MovieTorrent.status, func.count()).group_by(MovieTorrent.status))}
        versions = s.exec(select(func.count()).select_from(MovieTorrent)).one()
        dl_ids = set(s.exec(select(MovieTorrent.movie_id)
                            .where(MovieTorrent.status == "downloaded").distinct()))
    active = [m for m in all_m if not m.rejected]
    active_ids = {m.id for m in active}
    q_of = {m.id: (m.quarter or "未知") for m in active}
    total_by_q = Counter(q_of[m.id] for m in active)
    dl_by_q = Counter(q_of[mid] for mid in dl_ids if mid in q_of)
    qs = sorted((q for q in total_by_q if q != "未知"), reverse=True)
    if "未知" in total_by_q:
        qs.append("未知")
    return {
        "kpi": {
            "total": len(active),
            "matched": sum(1 for m in active if m.bangumi_id),
            "unmatched": sum(1 for m in active if not m.bangumi_id),
            "downloaded": len([mid for mid in dl_ids if mid in active_ids]),
            "rejected": sum(1 for m in all_m if m.rejected),
            "versions": versions,
        },
        "by_quarter": [(q, total_by_q.get(q, 0), dl_by_q.get(q, 0)) for q in qs],
        "status": {k: status.get(k, 0) for k in
                   ("downloaded", "downloading", "pending", "error", "skipped")},
        "qb": engine.qb_summary(MovieTorrent),
        "config": {"qb": config.QB_ENABLED},
    }


def year_brief() -> list[dict]:
    """列表页顶部小结：今年 + 上年 的剧场版分布（已识别/待识别/已忽略）+ 种子维度（已下/待下/版本）。
    剧场版按年归档（quarter 前两位=年份后两位），故按年而非季度小结。"""
    now_year = datetime.now().year

    def yr_of(q):
        return 2000 + int(q[:2]) if q and q[:2].isdigit() else None

    with get_session() as s:
        movies = list(s.exec(select(Movie.id, Movie.quarter, Movie.rejected, Movie.bangumi_id)))
        # 种子按 (movie_id,状态) 库内聚合，再按影片年份归拢（MovieTorrent 无 quarter 列）
        tcounts = list(s.exec(select(MovieTorrent.movie_id, MovieTorrent.status, func.count())
                              .group_by(MovieTorrent.movie_id, MovieTorrent.status)))
    yr_by_mid = {mid: yr_of(q) for mid, q, _, _ in movies}
    out = []
    for tag, yr in (("今年", now_year), ("上年", now_year - 1)):
        mv = [(rej, bid) for _, q, rej, bid in movies if yr_of(q) == yr]
        qc: dict[str, int] = {}
        for mid, st, c in tcounts:
            if yr_by_mid.get(mid) == yr:
                qc[st] = qc.get(st, 0) + c
        out.append({
            "tag": tag, "key": yr,
            "total": len(mv),
            "matched": sum(1 for rej, bid in mv if not rej and bid),   # 已识别（有 bgm、未忽略）
            "fail": sum(1 for rej, bid in mv if not rej and not bid),  # 待识别（未匹配 bgm）
            "ignored": sum(1 for rej, bid in mv if rej),               # 已忽略
            "versions": sum(qc.values()),
            "done": qc.get("downloaded", 0),
            "pending": qc.get("pending", 0) + qc.get("error", 0),
        })
    return out


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
        "movie_id": t.movie_id,
        "time": engine.torrent_time(t),
        "name": names.get(t.movie_id) or (t.raw_title or "?"),
        "source": t.source,
        "status": t.status,
        "qb_state": t.qb_state,
        "qb_progress": t.qb_progress,
        "qb_synced_at": t.qb_synced_at,
        "qb_dlspeed": t.qb_dlspeed,
        "raw": t.raw_title or "",
    } for t in ts]


def inflight_movie_rows(limit: int = 50) -> list[dict]:
    """仪表盘『正在下载』区：当前在下的剧场版/OVA 种子（口径同 has_inflight），按完成度降序。"""
    with get_session() as s:
        ts = list(s.exec(
            select(MovieTorrent).where(*engine._inflight_where(MovieTorrent))
            .order_by(MovieTorrent.qb_progress.desc(), MovieTorrent.created_at.desc()).limit(limit)))
        ids = {t.movie_id for t in ts if t.movie_id}
        names = ({m.id: (m.display_name or m.title) for m in
                  s.exec(select(Movie).where(Movie.id.in_(ids)))} if ids else {})
    return [{
        "id": t.id,
        "name": names.get(t.movie_id) or (t.raw_title or "?"),
        "status": t.status,
        "qb_state": t.qb_state,
        "qb_progress": t.qb_progress,
        "qb_synced_at": t.qb_synced_at,
        "qb_dlspeed": t.qb_dlspeed,
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
        # deleted 也算『有过一版』：唯一版本被用户删掉后，不该把 skipped 旧版翻出来重下（与承诺一致）
        anydl = any(t.status in ("downloaded", "downloading", "deleted") for t in rows)
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
        # 手动重识别：允许更新季度（哪怕已下过）——季度变了由 UI 层确认后 relocate_movie 搬已下文件
        engine.apply_bgm_meta(m, info, keep_quarter=False)
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
        # 手动纠正绑定：允许更新季度（哪怕已下过）——季度变了由 UI 层确认后 relocate_movie 搬已下文件
        engine.apply_bgm_meta(m, info, keep_quarter=False)
        s.add(m)
        s.commit()
        for other in list(s.exec(select(Movie).where(
                Movie.bangumi_id == bgm_id, Movie.id != m.id))):
            _merge_movie(s, other.id, m.id)
    return True


def movie_save_path(movie_id: int) -> str | None:
    """该剧场版当前的归档目录（build_save_path 结果：[子目录]/[季度]/片名）；算不出返回 None。

    取值与 download_movie_torrent 一致：季度用 m.quarter，片名 jp_name→display_name→title。剧场版不建 Season 子目录。
    """
    with get_session() as s:
        m = s.get(Movie, movie_id)
        if m is None:
            return None
        quarter = (m.quarter or "unknown")
        folder = (m.jp_name or m.display_name) or m.title or "movie"
    return engine.build_save_path(quarter, folder, sub_dir=config.MOVIE_DOWN_PATH,
                                  quarter_fmt=config.MOVIE_QUARTER_FMT)


async def relocate_movie(movie_id: int, old_path: str | None = None) -> dict:
    """把该剧场版已下/在下的种子移到当前归档目录（改季度/重绑后调用；调用方应已落新 m.quarter/名）。

    对齐番剧 relocate_anime：qB 跟踪该种子 → setLocation 原地搬 + 更新 save_path；qB 关/连不上/不跟踪
    (remove-on-complete) → 清完成状态待人工重下到新目录；setLocation 报 403/409(新目录不可写) → 只报告、不动状态。
    返回 {new_path, old_path, moved, redownload, untracked, failed, fail_code?, error?}。
    """
    new_path = movie_save_path(movie_id)
    rep = {"new_path": new_path, "old_path": old_path, "moved": 0,
           "redownload": 0, "untracked": 0, "failed": 0}
    if new_path is None:
        rep["error"] = "算不出新路径（越界或无片）"
        return rep
    with get_session() as s:
        pairs = [(t.id, t.info_hash) for t in s.exec(select(MovieTorrent).where(
            MovieTorrent.movie_id == movie_id,
            MovieTorrent.status.in_(["downloaded", "downloading"])))]
    if not pairs:
        return rep

    def _clear(ids):   # 清完成状态→pending，等人工重下到新目录
        with get_session() as s:
            for tid in ids:
                t = s.get(MovieTorrent, tid)
                if t is not None and t.status in ("downloaded", "downloading"):
                    t.status = "pending"
                    s.add(t)
            s.commit()

    def _mark_moved(ids):
        with get_session() as s:
            for tid in ids:
                t = s.get(MovieTorrent, tid)
                if t is not None:
                    t.save_path = new_path
                    s.add(t)
            s.commit()

    all_ids = [tid for tid, _ in pairs]
    if not config.QB_ENABLED:   # qB 关：只能清状态待重下 + 提醒
        _clear(all_ids)
        rep["redownload"] = len(all_ids)
        return rep
    info = await engine.qb.torrents_info([h for _, h in pairs])
    if info is None:            # qB 连不上：同上
        _clear(all_ids)
        rep["redownload"] = len(all_ids)
        return rep
    tracked = [(tid, h) for tid, h in pairs if h in info]
    untracked = [tid for tid, h in pairs if h not in info]
    if untracked:               # remove-on-complete 等：qB 已不认识 → 清状态待重下
        _clear(untracked)
        rep["untracked"] = rep["redownload"] = len(untracked)
    if tracked:
        code = await engine.qb.set_location([h for _, h in tracked], new_path)
        if code == 200:
            _mark_moved([tid for tid, _ in tracked])
            rep["moved"] = len(tracked)
        elif code is None:      # 中途连不上：退回清状态待重下
            _clear([tid for tid, _ in tracked])
            rep["redownload"] += len(tracked)
        else:                   # 403/409：新目录不可写/建不了 → 只报告，不动状态
            rep["failed"] = len(tracked)
            rep["fail_code"] = code
    return rep


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
            if t is None or (t.status in ("downloading", "downloaded") and t.archived_at is None):
                return False  # 已在下/已下 → 幂等短路，防并发重复交 qB；例外：已归档的可重新下（重新交回 qB）
            # 跨表【不】去重：剧场版/番剧各下到各自目录（用户要各归各、重复提交也接受）。qB 按 hash 物理去重、
            # 不会真下两遍；某侧删文件后另一侧由 sync 落 error——不再造 progress=1 的幽灵 pointer。
            m = s.get(Movie, t.movie_id)
            t.status = "downloading"
            # 重新下：清归档标记 + 重置 qB 实时态，作『全新在下』重新跟踪、从新完成点重算归档倒计时（否则会被立刻再归档）
            t.archived_at = None
            t.qb_progress, t.qb_state, t.qb_synced_at, t.qb_progress_at = 0.0, "", None, None
            s.add(t)
            s.commit()
            url = t.download_url
            info_hash = t.info_hash
            quarter = (m.quarter if m else "") or "unknown"
            folder = (m and (m.jp_name or m.display_name)) or t.raw_title or "movie"

    save_path = engine.build_save_path(quarter, folder, sub_dir=config.MOVIE_DOWN_PATH,
                                       quarter_fmt=config.MOVIE_QUARTER_FMT)
    if save_path is None:
        log.error("拒绝越界保存路径 - movie torrent %s", mt_id)
        _set_status(mt_id, "error")
        return False
    try:
        data = await engine.fetch_torrent_bytes(url)
        ok = await engine.add_to_qb(data, save_path, f"autoRSS-movie {quarter}", quarter, info_hash=info_hash)
    except asyncio.CancelledError:
        _set_status(mt_id, "pending")
        raise
    except Exception as e:
        log.error("剧场版下载失败 - %s", e)
        _set_status(mt_id, "error")
        return False
    if not ok:
        _set_status(mt_id, "error")
        return False
    with get_session() as s:   # 记实际保存路径：改季度/重绑后据此移动或提醒旧位置
        t = s.get(MovieTorrent, mt_id)
        if t is not None:
            t.save_path = save_path
            s.add(t)
            s.commit()
    if config.QB_SYNC_STATUS:
        _set_status(mt_id, "downloaded")
        engine.qb_kick.set()   # 唤醒 qB 同步循环，立即开始跟这个新交付的种子
    else:
        engine.settle_downloaded(MovieTorrent, mt_id)  # 关跟踪：发送即已下，落定 qb_progress=1、脱离 in-flight
    log.info("已加入qB（剧场版）- torrent=%s", mt_id)
    await notify(f"{folder} 🎬📥")
    return True


async def delete_movie_torrent(mt_id: int) -> bool:
    """删除单条剧场版种子在 qB 里的文件（走 qB 接口），标记为 deleted（终态，恢复时不重下）。

    若同一 hash TV 管线还在用，则只脱手本行、不删 qB/文件，免得毁了对面。
    """
    with get_session() as s:
        t = s.get(MovieTorrent, mt_id)
        if t is None or t.status not in ("downloaded", "downloading", "stalled"):
            return False  # stalled(停滞异常) 也允许删：清掉 qB 里卡死的残缺文件
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
