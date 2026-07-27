"""剧场版 / OVA 逻辑（与 TV 番剧 anime.py 完全分离，只共用 engine 底层）。

来源仅 Mikan 季度浏览页的『剧场版/OVA 桶』——不碰 TV 那边的订阅源。识别用 bgm。
『是不是电影以 Mikan 桶为准』：桶里的一律当剧场版/OVA 收进 Movie（哪怕 bgm 把类型识别成 TV，
也只是详情页的 bgm 元数据，不改变它在剧场版列表里，也不转去番剧表）。剧场版一部作品逐版本人工点下。

【有意与 TV 侧不同的两点，均为产品决定，勿"对齐"补上】：
· 不受『开始使用日』(ANIME_START_DATE) 约束——那是给自动下载兜底的，剧场版本就不自动下。
· 无 flush 自动放行、也无 _revive_orphaned_skipped 式的自动换源兜底——
  哪一版下失败就停在那里，由用户在详情页自己挑另一版下，不替用户决定。
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
from sources.parse import format_quarter

log = logging.getLogger("autorss")

_dl_lock = asyncio.Lock()  # 串行化剧场版下载，防同一片并发重复交 qB


def _has_downloads(s, movie_id: int) -> bool:
    """该片是否已下过（在下/已下/停滞/曾删）——有则季度已落盘/曾落盘，重识别不该改（与番剧 _has_downloads 对齐）。
    含 deleted（删过也算季度已定）+ stalled（半成品仍在盘、save_path 按旧季度写过），否则重识别把季度冲掉、文件散目录。"""
    return s.exec(select(MovieTorrent).where(
        MovieTorrent.movie_id == movie_id,
        MovieTorrent.status.in_(engine.HANDLED_STATUSES))).first() is not None


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
        engine.apply_bgm_meta(movie, info, keep_path=(not is_new and _has_downloads(s, movie.id)))
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
                            .where(MovieTorrent.status == "sent").distinct()))
    active = [m for m in all_m if not m.rejected]
    q_of = {m.id: (m.quarter or "未知") for m in active}   # 键域即 active 的 id 集，成员判断复用它
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
            "rejected": sum(1 for m in all_m if m.rejected),
            "versions": versions,
        },
        "by_quarter": [(q, total_by_q.get(q, 0), dl_by_q.get(q, 0)) for q in qs],
        # 八种应用侧 status 全列（含 deleted/excluded），与番剧侧同口径：仪表盘『种子数』号称"各状态之和"
        # 八状态【全集】取自 engine，别再手抄：仪表盘对用户承诺"种子数 = 各状态之和"，
        # 将来加第 9 个状态时漏改这里，页面数字就会静默对不上（且不报错）
        "status": {k: status.get(k, 0) for k in engine.ALL_STATUSES},
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
            "done": qc.get("sent", 0),
            "pending": sum(qc.get(k, 0) for k in engine.DOWNLOADABLE_STATUSES),
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


def source_map() -> dict:
    """{片 id: [来源...]}，一次 DISTINCT 查齐（对齐番剧 source_map）。

    列表页/待识别页逐片调 movie_sources 是 N+1：每片一个 session、一条 SQL；
    片数一多（剧场版按年攒）渲染就线性变慢。这里一次查完，行数只与『片×来源』有关。
    """
    from collections import defaultdict
    with get_session() as s:
        pairs = list(s.exec(select(MovieTorrent.movie_id, MovieTorrent.source).distinct()))
    src: dict = defaultdict(set)
    for mid, source in pairs:
        if mid:
            src[mid].add(source or "?")
    return {mid: sorted(v) for mid, v in src.items()}


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
                MovieTorrent.movie_id == movie_id,
                MovieTorrent.status.in_(engine.DOWNLOADABLE_STATUSES))):
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
        # deleted/stalled 也算『有过一版』：唯一版本被删/停滞后，不该把 skipped 旧版翻出来重下（与番剧 _HANDLED 一致）
        anydl = any(t.status in engine.HANDLED_STATUSES for t in rows)
        for t in rows:  # 剧场版=一部作品：已有一版就别把 skipped 旧版翻出来（deleted 是用户主动删，也不重下）
            if t.status == "skipped" and not anydl:
                t.status = "pending"
                s.add(t)
        s.commit()


def _terminal_torrent_rows(status: str) -> list[dict]:
    """某终态(deleted/excluded)的剧场版种子行（片名/原名），供『已忽略』页底部折叠展示。"""
    with get_session() as s:
        ts = list(s.exec(select(MovieTorrent).where(MovieTorrent.status == status)
                         .order_by(MovieTorrent.created_at.desc())))
        ids = {t.movie_id for t in ts if t.movie_id}
        names = ({m.id: (m.display_name or m.title) for m in
                  s.exec(select(Movie).where(Movie.id.in_(ids)))} if ids else {})
    return [{"id": t.id, "movie_id": t.movie_id,
             "name": names.get(t.movie_id) or "?", "raw": t.raw_title or ""} for t in ts]


def deleted_torrent_rows() -> list[dict]:
    """已删除(deleted)的剧场版种子——『已删除种子』折叠 + 重新下载找回（与番剧对称）。"""
    return _terminal_torrent_rows("deleted")


def excluded_torrent_rows() -> list[dict]:
    """已排除(excluded)的剧场版种子——『已排除种子』折叠 + 恢复放回可下（与番剧对称）。"""
    return _terminal_torrent_rows("excluded")


def exclude_torrent(mt_id: int) -> bool:
    """排除一条不想要的待下版本（实现见 engine.exclude_torrent，两条线共用）。
    不再显示在可下队列、RSS 再遇到同 hash 也不重收；可用 unexclude_torrent 撤销。"""
    return engine.exclude_torrent(MovieTorrent, mt_id)


def unexclude_torrent(mt_id: int) -> bool:
    """取消排除：把 excluded 的版本放回 pending（可下）。返回是否放回了。"""
    return engine.unexclude_torrent(MovieTorrent, mt_id)


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
        engine.apply_bgm_meta(m, info, keep_path=False)
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
        engine.apply_bgm_meta(m, info, keep_path=False)
        s.add(m)
        s.commit()
        for other in list(s.exec(select(Movie).where(
                Movie.bangumi_id == bgm_id, Movie.id != m.id))):
            _merge_movie(s, other.id, m.id)
    return True


def _movie_folder(m, t=None):
    """剧场版下载文件夹名——download_movie_torrent 与 movie_save_path 统一口径（B3）：
    bgm 日文/中文名 → 种子原始标题 → m.title → 'movie'。缺 bgm 元数据时两处都回退到同一名字。"""
    return (((m.jp_name or m.display_name) if m else "")
            or (t.raw_title if t else "") or (m.title if m else "") or "movie")


def movie_save_path(movie_id: int) -> str | None:
    """该剧场版当前的归档目录（build_save_path 结果：[子目录]/[季度]/片名）；算不出返回 None。

    与 download_movie_torrent 【同口径】：都走 _movie_folder(m, 最新种子行)。缺 bgm 元数据时都回退种子原始标题，
    故显示/relocate 目标与实际落地一致（B3）。剧场版不建 Season 子目录。
    """
    with get_session() as s:
        m = s.get(Movie, movie_id)
        if m is None:
            return None
        t = s.exec(select(MovieTorrent).where(MovieTorrent.movie_id == movie_id)
                   .order_by(MovieTorrent.created_at.desc())).first()   # 最新种子行，缺元数据时回退，同下载口径
        quarter = (m.quarter or "unknown")
        folder = _movie_folder(m, t)
    return engine.build_save_path(quarter, folder, sub_dir=config.MOVIE_DOWN_PATH,
                                  quarter_fmt=config.MOVIE_QUARTER_FMT)


async def relocate_movie(movie_id: int, old_path: str | None = None) -> dict:
    """把该剧场版已下/在下/停滞的版本移到当前归档目录（改季度/重绑后调用；调用方应已落新 m.quarter/名）。
    实现见 engine.relocate（与番剧共用同一份）。"""
    return await engine.relocate(MovieTorrent, MovieTorrent.movie_id, movie_id,
                                 movie_save_path(movie_id), old_path, noun="个版本")


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
            if t is None or (t.status in engine.TRACKED_STATUSES and t.archived_at is None):
                return False  # 已在下/已下 → 幂等短路，防并发重复交 qB；例外：已归档的可重新下（重新交回 qB）
            # 跨表【不】去重：剧场版/番剧各下到各自目录（用户要各归各、重复提交也接受）。qB 按 hash 物理去重、
            # 不会真下两遍；某侧删文件后另一侧由 sync 落 error——不再造 progress=1 的幽灵 pointer。
            m = s.get(Movie, t.movie_id)
            orig_status = t.status        # 供失败恢复：从终态(deleted/excluded)重下失败别降级成 error
            orig_archived = t.archived_at  # 同上：重下【已归档】的版本失败要把归档标记原样放回
            t.status = "downloading"
            # 重新下：清归档标记 + 重置 qB 实时态，作『全新在下』重新跟踪、从新完成点重算归档倒计时（否则会被立刻再归档）
            t.archived_at = None
            t.qb_progress, t.qb_state, t.qb_synced_at, t.qb_progress_at = 0.0, "", None, None
            s.add(t)
            s.commit()
            url = t.download_url
            info_hash = t.info_hash
            quarter = (m.quarter if m else "") or "unknown"
            folder = _movie_folder(m, t)   # 与 movie_save_path 同口径（B3）

    # 与番剧侧对齐：失败不无条件写 error。终态(deleted/excluded)重下失败降级成 error 会丢掉
    # 『用户已处理』的语义；已归档重下失败降级则让那份仍在盘上的旧文件脱离 HAVE_STATUSES，
    # UI 上再没有入口能删它。CancelledError 以前写死 pending，同样会抹掉终态。
    _from_terminal = (orig_status in engine.MANUAL_TERMINAL_STATUSES
                      or orig_archived is not None)
    fail_status = orig_status if _from_terminal else "error"
    defer_status = orig_status if _from_terminal else "pending"   # qB 连不上时的落点，同番剧侧

    def _fail(status: str = "") -> None:
        st = status or fail_status
        _set_status(mt_id, st)
        if orig_archived is not None:
            with get_session() as s2:
                t2 = s2.get(MovieTorrent, mt_id)
                if t2 is not None and t2.status == st:
                    t2.archived_at = orig_archived
                    s2.add(t2)
                    s2.commit()

    save_path = engine.build_save_path(quarter, folder, sub_dir=config.MOVIE_DOWN_PATH,
                                       quarter_fmt=config.MOVIE_QUARTER_FMT)
    if save_path is None:
        log.error("拒绝越界保存路径 - movie torrent %s", mt_id)
        _fail()
        return False
    try:
        data = await engine.fetch_torrent_bytes(url)
        # 分类固定不带后缀；标签只放【年份】(不带季度)。format_quarter 解析不出时原样回退（如 unknown）
        ok = await engine.add_to_qb(data, save_path, "AutoRSS-Movie",
                                    format_quarter(quarter, "{yyyy}"), info_hash=info_hash)
    except asyncio.CancelledError:
        _fail()
        raise
    except Exception as e:
        log.error("剧场版下载失败 - %s", e)
        _fail()
        return False
    if ok is None:             # qB 连不上：留在待下，别记 error
        log.warning("qB 连不上，本条留待重发 - movie torrent %s", mt_id)
        _fail(defer_status)
        return False
    if not ok:
        _fail()
        return False
    with get_session() as s:   # 记实际保存路径：改季度/重绑后据此移动或提醒旧位置
        t = s.get(MovieTorrent, mt_id)
        if t is not None:
            t.save_path = save_path
            s.add(t)
            s.commit()
    if config.QB_SYNC_STATUS:
        _set_status(mt_id, "sent")
        engine.qb_kick.set()   # 唤醒 qB 同步循环，立即开始跟这个新交付的种子
    else:
        engine.settle_sent(MovieTorrent, mt_id)  # 关跟踪：发送即已下，落定 qb_progress=1、脱离 in-flight
    log.info("已加入qB（剧场版）- torrent=%s", mt_id)
    await notify(f"{folder} 🎬📥")
    return True


async def delete_movie_torrent(mt_id: int) -> bool:
    """删除单条剧场版种子在 qB 里的文件（走 qB 接口），标记为 deleted（终态，恢复时不重下）。

    若同一 hash TV 管线还在用，则只脱手本行、不删 qB/文件，免得毁了对面。

    已归档(archived_at)的：种子早已从 qB 移除，qB 代删不到文件 → 只落 deleted 终态，
    硬盘文件留在 save_path 由用户自行清理（UI 在确认框里把该路径显示出来）。
    """
    with get_session() as s:
        t = s.get(MovieTorrent, mt_id)
        if t is None or t.status not in engine.HAVE_STATUSES:
            return False  # stalled 也允许删
        h = t.info_hash
        if t.archived_at is not None:      # 已归档：不在 qB，删不到文件，只改状态
            t.status = "deleted"
            s.add(t)
            s.commit()
            log.info("删除已归档条目（仅标记，文件留在 %s）- torrent=%s", t.save_path or "?", mt_id)
            return True
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


def failed_rows() -> list[dict]:
    """status∈{error, stalled}（下载失败过 / 长期停滞）的版本，供 KPI『失败』点开查看、进详情页处理。
    对齐番剧的 anime.failed_rows：一次取种子 + 批量取片名，避免逐条查库。"""
    with get_session() as s:
        ts = list(s.exec(select(MovieTorrent)
                         .where(MovieTorrent.status.in_(["error", "stalled"]))
                         .order_by(MovieTorrent.created_at.desc())))
        ids = {t.movie_id for t in ts if t.movie_id}
        names = ({m.id: (m.display_name or m.title) for m in
                  s.exec(select(Movie).where(Movie.id.in_(ids)))} if ids else {})
    return [{"id": t.id, "movie_id": t.movie_id,
             "name": names.get(t.movie_id) or (t.raw_title or "?"),
             "raw": t.raw_title or ""} for t in ts]
