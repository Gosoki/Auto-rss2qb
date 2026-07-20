"""主流程编排 + 给 UI 用的查询/操作函数。

一条标准条目进来 → 按 info_hash 去重 → 用『番名对照(TitleAlias)』定位到唯一的番
(命中即知；未命中则查一次 bgm，有对应番就复用、否则新建) → 入库种子(带 anime_id) →
由 flush_ready_downloads 按『缓冲窗口 + 优先级』对每集只下一份。
"""
import asyncio
import logging
import os
import re
import shutil
from collections import Counter
from datetime import datetime, timedelta

import httpx
from sqlalchemy.exc import IntegrityError
from sqlmodel import func, select

import enrich
import config
from db import get_session
from models import Anime, SourceGroup, TitleAlias, Torrent
from notify import notify
from qbittorrent import QBittorrent
from sources.parse import extract_quarter, format_quarter, season_from_name

log = logging.getLogger("autorss")
qb = QBittorrent()

# 串行化『选集→占位下载』，防止 worker flush 与 UI 补下并发对同一集重复放行
_download_lock = asyncio.Lock()

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_BGM_FIELDS = ("bangumi_id", "display_name", "jp_name", "air_date", "air_weekday",
               "total_episodes", "platform", "cover_url", "rating", "summary")


def _safe(name: str) -> str:
    """清洗成安全的单段文件夹名：去非法字符/控制符，并挡掉 '.'/'..' 路径穿越。"""
    cleaned = _ILLEGAL.sub("_", name or "").strip().strip(".").strip()
    return cleaned or "unknown"


def quarter_folder(quarter: str) -> str:
    """内部季度键(26C) → 下载文件夹用的季度目录名（config.QUARTER_FMT）。"""
    return format_quarter(quarter, config.QUARTER_FMT)


def quarter_label(quarter: str) -> str:
    """内部季度键(26C) → 页面显示用的季度名（config.QUARTER_FMT_UI，与下载文件夹分开）。"""
    return format_quarter(quarter, config.QUARTER_FMT_UI)


def _prev_quarter(q: str) -> str:
    """上一个季度键：26C→26B，26A→25D（A 是年内第一季）。解析不出回空串。"""
    m = re.fullmatch(r"(\d{2})([A-D])", q or "")
    if not m:
        return ""
    yy, letter = int(m.group(1)), m.group(2)
    if letter == "A":
        return f"{yy - 1:02d}D"
    return f"{yy}{chr(ord(letter) - 1)}"


def quarter_brief() -> list[dict]:
    """番剧列表页顶部小结：当季 + 上季 的番剧流水线分布 + 种子维度。"""
    cur = extract_quarter(datetime.now())
    prev = _prev_quarter(cur)
    with get_session() as s:
        animes = list(s.exec(select(Anime.quarter, Anime.confirmed, Anime.rejected, Anime.bangumi_id)))
        torrents = list(s.exec(select(Torrent.quarter, Torrent.status)))
    out = []
    for tag, q in (("当季", cur), ("上季", prev)):
        aq = [(conf, rej, bid) for aqk, conf, rej, bid in animes if (aqk or "") == q]
        tq = [st for tqk, st in torrents if (tqk or "") == q]
        out.append({
            "tag": tag, "key": q,
            "shows": sum(1 for conf, rej, bid in aq if conf and not rej),      # 追番中
            "confirm": sum(1 for conf, rej, bid in aq if not conf and not rej and bid),  # 待确认
            "fail": sum(1 for conf, rej, bid in aq if not rej and not bid),    # 待识别(未匹配)
            "ignored": sum(1 for conf, rej, bid in aq if rej),                 # 已忽略
            "torrents": len(tq),
            "done": sum(1 for st in tq if st == "downloaded"),
            "pending": sum(1 for st in tq if st in ("pending", "error")),
        })
    return out


def _is_auto(kind: str) -> bool:
    return kind in ("auto", "ani")  # 兼容旧值 'ani'


def _apply_bgm(a: Anime, info: dict | None, keep_quarter: bool = False) -> None:
    """把 enrich.resolve 的结果写进 anime（只覆盖非空字段）。

    keep_quarter=True（手动重富集时）不动已定的季度——季度是归档路径的一部分，
    确定后应保持稳定，否则已下分集会散落到另一个季度目录。
    """
    if not info:
        return
    for k in _BGM_FIELDS:
        v = info.get(k)
        if v is not None:
            setattr(a, k, v)
    if info.get("quarter") and not (keep_quarter and a.quarter):
        a.quarter = info["quarter"]
    # 季号以 bgm 规范名为准：ANi 罗马音标题常写 "Season 3" 本地解析不到而回 1，
    # 而 bgm 规范名带『第三季』，能纠正（名字没季标记则保留本地解析值）。
    sn = season_from_name(a.display_name) or season_from_name(a.jp_name)
    if sn:
        a.season = sn


def _top_priority() -> int:
    """当前启用源组里的最高优先级（『最高优先级即时下载』的判据）。"""
    with get_session() as s:
        vals = [g.priority or 0 for g in s.exec(
            select(SourceGroup).where(SourceGroup.enabled == True))]  # noqa: E712
    return max(vals) if vals else 0


# ---------------- 管线 ----------------

async def _resolve_anime(item) -> int:
    """把一条种子映射到唯一的 Anime，返回 anime_id。

    ① 番名对照命中 → 直接返回（不查 bgm）；
    ② 未命中 → 富集拿 bgm_id：有对应番则复用，否则新建；无论如何登记一条对照。
    """
    with get_session() as s:
        alias = s.exec(select(TitleAlias).where(
            TitleAlias.title == item.anime_title, TitleAlias.season == item.season)).first()
        if alias is not None:
            return alias.anime_id

    # 未命中：富集定身份（尽力而为，拿不到就当独立新番）
    info = await enrich.resolve(item.search_names, item.release_time, item.episode, item.info_hash)

    with get_session() as s:
        alias = s.exec(select(TitleAlias).where(  # 重入保护：再查一次
            TitleAlias.title == item.anime_title, TitleAlias.season == item.season)).first()
        if alias is not None:
            return alias.anime_id

        bgm_id = info.get("bangumi_id") if info else None
        anime = None
        if bgm_id is not None:
            anime = s.exec(select(Anime).where(Anime.bangumi_id == bgm_id)).first()
        if anime is None:
            # 未匹配到 bgm 的番，即使来自自动源也不自动确认/下载——进『富集失败』等人工绑定
            auto = _is_auto(item.source_kind) and bgm_id is not None
            anime = Anime(
                title=item.anime_title, season=item.season, quarter=item.quarter,
                if_down=auto, confirmed=auto, source_kind=item.source_kind, enriched=True,
            )
            _apply_bgm(anime, info)
            s.add(anime)
            try:
                s.commit()
                s.refresh(anime)
            except IntegrityError:
                # 旧库残留的 uq_anime_title_season 撞车 → 复用同 (title,季) 的既有番
                s.rollback()
                anime = s.exec(select(Anime).where(
                    Anime.title == item.anime_title, Anime.season == item.season)).first()
                if anime is None:
                    raise
        # 登记番名对照（并发/竞态下可能已存在则忽略）
        if not s.exec(select(TitleAlias).where(
                TitleAlias.title == item.anime_title, TitleAlias.season == item.season)).first():
            s.add(TitleAlias(title=item.anime_title, season=item.season, anime_id=anime.id))
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
        return anime.id


async def process_item(item) -> bool:
    """处理一条标准条目。返回 True 表示是新种子（之前没见过）。"""
    # 1) 种子级去重：同一 hash 见过就跳过（跨源相等）
    with get_session() as s:
        if s.exec(select(Torrent).where(Torrent.info_hash == item.info_hash)).first() is not None:
            return False

    # 2) 定位到唯一的番（对照命中不查 bgm；未命中查一次）
    anime_id = await _resolve_anime(item)

    # 3) 入库种子（带 anime_id）。一般不在这里下：交给 flush_ready_downloads。
    with get_session() as s:
        a = s.get(Anime, anime_id)
        torrent = Torrent(
            info_hash=item.info_hash,
            anime_id=anime_id,
            source=item.source,
            site=item.site,
            anime_title=item.anime_title,
            season=item.season,
            episode=item.episode,
            quarter=a.quarter if a else item.quarter,
            download_url=item.download_url,
            release_time=item.release_time,
            priority=item.priority,
            status="pending",
        )
        s.add(torrent)
        s.commit()
        s.refresh(torrent)
        torrent_id = torrent.id
        should_download = bool(a and a.confirmed and not a.rejected)

    log.info("新增 - %s - %s 第%s季 第%s集", item.source, item.anime_title, item.season, item.episode)
    # 最高优先级即时下载：开关开 + 自动下的番 + 来自最高优先级组 → 入库就下，不等缓冲窗口
    if config.TOP_PRIORITY_INSTANT and should_download and (item.priority or 0) >= _top_priority():
        await download_torrent(torrent_id)
    return True


async def download_torrent(torrent_id: int, force: bool = False) -> bool:
    """取种子文件并加入 qBittorrent。成功返回 True。

    『选集去重 + 占位』整段放在 _download_lock 里做，且集去重同时看 downloading/downloaded，
    这样 worker flush 与 UI 补下并发时也不会对同一集重复放行两份。
    force=True：强制下这一条（无视当前状态、跳过集去重），用于详情页手动指定下载。
    """
    if not config.QB_ENABLED:
        return False  # 无 qB 模式：只采集元数据，不发送种子（保持 pending）

    async with _download_lock:
        with get_session() as s:
            t = s.get(Torrent, torrent_id)
            if t is None or (not force and t.status not in ("pending", "error")):
                return False  # 不存在 / 已下过 / 正在下（force 时不受此限）
            anime_id = t.anime_id
            episode = t.episode
            season = t.season
            title = t.anime_title
            # 同集去重：同一 (anime_id, 集) 已有别的种子在下/已下 → 跳过（force 时不去重，强制下这条）
            if not force and isinstance(episode, (int, float)) and episode >= 0 and anime_id:
                dup = s.exec(select(Torrent).where(
                    Torrent.anime_id == anime_id,
                    Torrent.episode == episode,
                    Torrent.status.in_(["downloading", "downloaded"]),
                    Torrent.id != torrent_id,
                )).first()
                if dup is not None:
                    t.status = "skipped"
                    s.add(t)
                    s.commit()
                    log.info("跳过重复集 - %s 第%s季 第%s集（已有一份在下/已下）", title, season, episode)
                    return False
            t.status = "downloading"  # 原子占位（锁内，别的协程看得到）
            s.add(t)
            s.commit()
            url = t.download_url
            quarter = t.quarter or "unknown"
            a = s.get(Anime, anime_id) if anime_id else None
            # 文件夹名统一用 bgm 日语原名，没有再退中文规范名，最后退种子解析番名
            folder_name = (a and (a.jp_name or a.display_name)) or t.anime_title

    # 组装保存路径并做包含校验，杜绝标题里的 '..' 等把种子写到 config.DOWN_PATH 之外
    parts = [config.DOWN_PATH, _safe(quarter_folder(quarter)), _safe(folder_name)]
    if config.SEASON_SUBFOLDER:
        parts.append(f"Season {int(season)}")  # 二级子目录（可在设置里关）
    save_path = os.path.join(*parts)
    root = os.path.realpath(config.DOWN_PATH)
    real = os.path.realpath(save_path)
    if real != root and not real.startswith(root + os.sep):
        log.error("拒绝越界保存路径 - %s -> %s", title, save_path)
        _set_status(torrent_id, "error")
        return False

    kwargs = {"timeout": 60, "follow_redirects": True}
    if config.PROXY:
        kwargs["proxy"] = config.PROXY
    try:
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content

        try:  # 尽力创建目录（跨用户的 qB 需要），失败不阻断
            os.makedirs(save_path, exist_ok=True)
            os.chmod(save_path, 0o777)
        except OSError:
            pass

        ok = await qb.add_torrent(data, save_path, f"autoRSS {quarter}", quarter)
    except asyncio.CancelledError:
        _set_status(torrent_id, "pending")  # 被取消（关停等）→ 复位，别永久卡 downloading
        raise
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


async def flush_ready_downloads() -> int:
    """缓冲窗口 + 严格优先级：每轮跑一次。

    对『自动下载且已确认』的番，把待下种子按 (anime_id, 集) 归组——因为按番的真实身份
    分组，不同组不同写法的同一集会算作同一集，天然只留一份。每集首次被发现后满
    config.DOWNLOAD_GRACE_MIN 分钟才放行，到点从该集所有种子挑优先级最高的下一份（错误的排后，
    留作降级）。特别篇/未知集不做集去重，逐个下。返回实际触发下载的数量。
    """
    grace = timedelta(minutes=config.DOWNLOAD_GRACE_MIN)
    now = datetime.now()
    chosen: list[int] = []
    with get_session() as s:
        auto = list(s.exec(
            select(Anime).where(Anime.confirmed == True, Anime.rejected.is_not(True))  # noqa: E712
        ))
        auto_ids = {a.id for a in auto}
        pref_map = {a.id: a.pref_source for a in auto if a.pref_source}
        if not auto_ids:
            return 0
        downloaded = {
            (t.anime_id, t.episode)
            for t in s.exec(select(Torrent).where(Torrent.status == "downloaded"))
        }
        groups: dict = {}
        specials: list[int] = []
        # 只自动放行 pending：error 不在这里无限重试（高优先级失败→本组还有 pending 低优先级自然降级；
        # 全 error 则本轮不重试，留给人工补下）。
        for t in s.exec(select(Torrent).where(Torrent.status == "pending")):
            if t.anime_id not in auto_ids:
                continue
            if t.episode is None or t.episode < 0:
                if t.episode == -1:
                    specials.append(t.id)  # 特别篇直接下；-2(未知/疑似批量) 不自动下，可人工补下
                continue
            groups.setdefault((t.anime_id, t.episode), []).append(t)

    for key, ts in groups.items():
        if key in downloaded:
            continue  # 这一集已有一份
        first_seen = min(t.created_at for t in ts)
        if now - first_seen < grace:
            continue  # 缓冲窗口未到，等偏好组
        cands = ts
        pref = pref_map.get(key[0])          # key = (anime_id, episode)
        if pref:
            matched = [t for t in ts if pref in (t.source or "")]
            if matched:
                cands = matched              # 钉了首选源就只从它里选，没有才退回全部
        best = sorted(cands, key=lambda t: (-(t.priority or 0), t.created_at))[0]
        chosen.append(best.id)
    chosen.extend(specials)

    n = 0
    for tid in chosen:
        if await download_torrent(tid):
            n += 1
    return n


# ---------------- 给 UI 的查询 ----------------

def overview() -> dict:
    """概览页所需的全部聚合数据，一次性算好；页面只负责渲染。"""
    with get_session() as s:
        animes = list(s.exec(select(Anime).where(Anime.rejected.is_not(True))))  # 非拒绝（含待确认）
        rejected = s.exec(select(func.count()).select_from(Anime)
                          .where(Anime.rejected == True)).one()  # noqa: E712
        torrents = list(s.exec(select(Torrent)))
        groups = list(s.exec(select(SourceGroup)))

    confirmed = [a for a in animes if a.confirmed]
    pending_c = [a for a in animes if not a.confirmed and a.bangumi_id]  # 待确认=已匹配未确认；未匹配的算『富集失败』
    status = Counter(t.status for t in torrents)

    # 各季度：已确认番数 + 已下集
    shows_by_q = Counter((a.quarter or "未知") for a in confirmed)
    done_by_q = Counter((t.quarter or "未知") for t in torrents if t.status == "downloaded")
    qkeys = set(shows_by_q) | set(done_by_q)
    qs = sorted((q for q in qkeys if q != "未知"), reverse=True)
    if "未知" in qkeys:
        qs.append("未知")
    by_quarter = [(q, shows_by_q.get(q, 0), done_by_q.get(q, 0)) for q in qs]

    # 各来源：种子数 + 已下
    src_total = Counter((t.source or "?") for t in torrents)
    src_done = Counter((t.source or "?") for t in torrents if t.status == "downloaded")
    by_source = sorted(((src, cnt, src_done.get(src, 0)) for src, cnt in src_total.items()),
                       key=lambda x: -x[1])

    return {
        "kpi": {
            "tracking": len(confirmed), "fail": sum(1 for a in animes if not a.bangumi_id),
            "confirm": len(pending_c), "rejected": rejected,
            "done": status.get("downloaded", 0),
            "pending": status.get("pending", 0) + status.get("error", 0),
            "multi": len(multi_source_map()), "torrents": len(torrents),
        },
        "status": {k: status.get(k, 0) for k in
                   ("downloaded", "downloading", "pending", "error", "skipped")},
        "by_quarter": by_quarter,
        "by_source": by_source,
        "enriched": (sum(1 for a in animes if a.bangumi_id), len(animes)),
        "groups": [(g.name, g.site, g.policy, g.priority, g.enabled)
                   for g in sorted(groups, key=lambda g: -g.priority)],
        "config": {"qb": config.QB_ENABLED, "poll_on": config.POLL_ENABLED,
                   "poll": config.POLL_INTERVAL, "grace": config.DOWNLOAD_GRACE_MIN},
    }


def list_anime() -> list[Anime]:
    """番剧管理用：只列『已确认、未拒绝』的番；待审核的在『待确认』页，拒绝的在『拒绝』页。"""
    with get_session() as s:
        return list(s.exec(
            select(Anime).where(Anime.rejected.is_not(True), Anime.confirmed == True)  # noqa: E712
            .order_by(Anime.quarter.desc(), Anime.id)
        ))


def list_all_anime() -> list[Anime]:
    """管理页统一视图：所有番（含待确认、已拒绝）；组内排序（状态垫底）交给页面。"""
    with get_session() as s:
        return list(s.exec(select(Anime).order_by(Anime.quarter.desc(), Anime.id)))


def list_rejected() -> list[Anime]:
    """已拒绝的番（『拒绝』页展示，可恢复）。"""
    with get_session() as s:
        return list(s.exec(
            select(Anime).where(Anime.rejected == True)  # noqa: E712
            .order_by(Anime.quarter.desc(), Anime.id)
        ))


def list_unenriched() -> list[Anime]:
    """富集失败/未匹配 bgm 的番（bangumi_id 为空、未拒绝）——供『富集失败』页人工处理。"""
    with get_session() as s:
        return list(s.exec(
            select(Anime).where(Anime.bangumi_id.is_(None), Anime.rejected.is_not(True))
            .order_by(Anime.created_at.desc())
        ))


def multi_source_map() -> dict:
    """{番 id: [来源...]}，仅含来源多于一个的番（管理页据此标『多源』）。"""
    from collections import defaultdict
    with get_session() as s:
        pairs = list(s.exec(select(Torrent.anime_id, Torrent.source)))
    src: dict = defaultdict(set)
    for aid, source in pairs:
        if aid:
            src[aid].add(source)
    return {aid: sorted(v) for aid, v in src.items() if len(v) > 1}


def pending_confirm() -> list[Anime]:
    """待确认：已匹配 bgm 但未确认、未拒绝的番。未匹配的在『富集失败』，绑定后才来这里。"""
    with get_session() as s:
        return list(s.exec(select(Anime).where(
            Anime.confirmed == False, Anime.rejected.is_not(True),  # noqa: E712
            Anime.bangumi_id.is_not(None))))


def list_torrents(limit: int = 50) -> list[Torrent]:
    with get_session() as s:
        return list(s.exec(select(Torrent).order_by(Torrent.created_at.desc()).limit(limit)))


def get_anime(anime_id: int) -> Anime | None:
    with get_session() as s:
        return s.get(Anime, anime_id)


def list_episodes(anime_id: int) -> list[Torrent]:
    """某番剧的全部种子（按集数、再按入库时间倒序），供详细页展示分集/来源。"""
    with get_session() as s:
        return list(s.exec(
            select(Torrent).where(Torrent.anime_id == anime_id)
            .order_by(Torrent.episode, Torrent.created_at.desc())
        ))


def sources_for(anime_id: int) -> list[str]:
    """某番剧现有的所有来源（去重排序），供待确认/详情页展示与选源。"""
    with get_session() as s:
        rows = s.exec(select(Torrent.source).where(Torrent.anime_id == anime_id)).all()
    return sorted({r for r in rows if r})


def downloaded_count(anime_id: int) -> int:
    """该番已下/在下（硬盘上有文件）的种子数——供 UI 决定要不要显示『删除文件』。"""
    with get_session() as s:
        return len(s.exec(select(Torrent.id).where(
            Torrent.anime_id == anime_id,
            Torrent.status.in_(["downloaded", "downloading"]),
        )).all())


# ---------------- 给 UI 的操作 ----------------

def confirm_anime(anime_id: int, pref_source: str = "") -> None:
    """确认下载该番；pref_source 非空则钉住首选下载源（本次及以后新集都优先用它）。"""
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is not None:
            a.confirmed = True
            a.if_down = True
            a.pref_source = pref_source or None
            s.add(a)
            s.commit()


def set_pref_source(anime_id: int, source: str) -> None:
    """改某番的首选下载源（空=按优先级）。详情页用。"""
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is not None:
            a.pref_source = source or None
            s.add(a)
            s.commit()


def reject_anime(anime_id: int) -> None:
    """拒绝某个番：打上 rejected（移出主列表进『拒绝』页）、不下载，积压待下种子标记跳过。"""
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None:
            return
        a.rejected = True
        a.confirmed = True
        a.if_down = False
        s.add(a)
        for t in s.exec(select(Torrent).where(
            Torrent.anime_id == anime_id,
            Torrent.status.in_(["pending", "error"]),
        )):
            t.status = "skipped"
            s.add(t)
        s.commit()


def restore_anime(anime_id: int) -> None:
    """从『拒绝』一步恢复到『追番中』（确认+下载），并把拒绝时跳过的待下种子放回 pending。

    不再绕经『待确认』——恢复即意味着『我又要了』；skipped→pending 与 reject 对称，让补下能拿到货。
    """
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None:
            return
        a.rejected = False
        a.confirmed = True
        a.if_down = True
        s.add(a)
        for t in s.exec(select(Torrent).where(
            Torrent.anime_id == anime_id,
            Torrent.status == "skipped",
        )):
            t.status = "pending"
            s.add(t)
        s.commit()


def _merge_anime(s, loser_id: int, keeper_id: int) -> None:
    """把 loser 番的对照与种子并到 keeper，删除 loser（保持一个 bgm_id 唯一一部番）。"""
    if loser_id == keeper_id:
        return
    for al in s.exec(select(TitleAlias).where(TitleAlias.anime_id == loser_id)):
        al.anime_id = keeper_id
        s.add(al)
    for t in s.exec(select(Torrent).where(Torrent.anime_id == loser_id)):
        t.anime_id = keeper_id
        s.add(t)
    loser = s.get(Anime, loser_id)
    if loser is not None:
        s.delete(loser)
    s.commit()


def _has_downloads(s, anime_id: int) -> bool:
    """该番是否已有在下/已下的种子——有则季度已落盘，不该再改（避免散目录）。"""
    return s.exec(select(Torrent).where(
        Torrent.anime_id == anime_id,
        Torrent.status.in_(["downloading", "downloaded"]),
    )).first() is not None


async def enrich_anime(anime_id: int) -> bool:
    """手动富集某番剧：用它已有的名字 + 最近一条种子回退，重取 bgm 元数据并覆盖。"""
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None:
            return False
        t = s.exec(
            select(Torrent).where(Torrent.anime_id == anime_id)
            .order_by(Torrent.created_at.desc())
        ).first()
        names = [n for n in (a.display_name, a.jp_name, a.title) if n]
        info_hash = t.info_hash if t else None
        release_time = t.release_time if t else None
        episode = t.episode if t else None

    info = await enrich.resolve(names, release_time, episode, info_hash)
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None:
            return False
        a.enriched = True
        # 无已下集就采用 bgm 季度（纠正种子解析得来的错季度）；有已下集才保留，避免散目录
        _apply_bgm(a, info, keep_quarter=_has_downloads(s, anime_id))
        s.add(a)
        s.commit()
        # 身份守卫：若该 bgm_id 已被别的番占用，合并过来，杜绝同一部番裂成两条
        if a.bangumi_id is not None:
            for other in list(s.exec(select(Anime).where(
                    Anime.bangumi_id == a.bangumi_id, Anime.id != a.id))):
                _merge_anime(s, other.id, a.id)
    return bool(info)


async def bind_bgm(anime_id: int, bgm_id: int) -> bool:
    """把某番手动绑定到指定 bgm subject id：取元数据覆盖 + 身份合并。返回是否成功。

    自动匹配失败（罗马音/冷门名搜不到）时的人工兜底：用户给准确的 bgm id，直接取权威元数据。
    """
    info = await enrich.fetch_by_id(bgm_id)
    if not info:
        return False
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None:
            return False
        a.enriched = True
        a.confirmed = False  # 绑定后进『审核/待确认』，等人工确认下载
        # 无已下集就采用 bgm 季度（纠正错季度）；有已下集才保留，避免散目录
        _apply_bgm(a, info, keep_quarter=_has_downloads(s, anime_id))
        s.add(a)
        s.commit()
        # 身份守卫：该 bgm_id 已被别的番占用 → 合并过来，杜绝一部番裂成两条
        for other in list(s.exec(select(Anime).where(
                Anime.bangumi_id == bgm_id, Anime.id != a.id))):
            _merge_anime(s, other.id, a.id)
    return True


async def reenrich_all() -> int:
    """对所有番重跑一次富集（回填 jp_name/规范名/简介/评分等新字段）。返回命中数。"""
    with get_session() as s:
        ids = [a.id for a in s.exec(select(Anime))]
    n = 0
    for aid in ids:
        try:
            if await enrich_anime(aid):
                n += 1
        except Exception as e:
            log.warning("重富集失败 anime=%s: %s", aid, e)
    log.info("重新富集完成：%d/%d 命中", n, len(ids))
    return n


async def download_pending_for_anime(anime_id: int) -> int:
    """把某番剧下 status=pending/error 的种子补下（人工确认后放行）。返回补下数量。

    加番剧级授权闸门：只对『已确认且未拒绝』的番补下。
    """
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None or not (a.confirmed and not a.rejected):
            return 0
        pref = a.pref_source
        rows = list(s.exec(
            select(Torrent).where(
                Torrent.anime_id == anime_id,
                Torrent.status.in_(["pending", "error"]),
            )
        ))
    # 首选源排前面，再按优先级——集去重会让排在前面的先占，从而按首选源下
    rows.sort(key=lambda t: (0 if (pref and pref in (t.source or "")) else 1,
                             -(t.priority or 0), t.created_at))
    ids = [t.id for t in rows]
    for tid in ids:
        await download_torrent(tid)
    return len(ids)


async def download_all_pending() -> int:
    """补下所有『已订阅且已确认』番剧的待下/失败种子。返回补下数量。"""
    with get_session() as s:
        auto_ids = {
            a.id for a in s.exec(
                select(Anime).where(Anime.confirmed == True, Anime.rejected.is_not(True))  # noqa: E712
            )
        }
        ids = [t.id for t in s.exec(select(Torrent).where(Torrent.status.in_(["pending", "error"])))
               if t.anime_id in auto_ids]
    for tid in ids:
        await download_torrent(tid)
    return len(ids)


async def delete_torrent_file(torrent_id: int) -> bool:
    """删除单条种子在 qB 里的文件（走 qB 接口），标记回 skipped。详情页按集删用。"""
    with get_session() as s:
        t = s.get(Torrent, torrent_id)
        if t is None or t.status not in ("downloaded", "downloading"):
            return False
        h = t.info_hash
    if not await qb.delete([h], delete_files=True):
        return False
    with get_session() as s:
        t = s.get(Torrent, torrent_id)
        if t is not None:
            t.status = "skipped"  # 文件已删，标记不再持有
            s.add(t)
            s.commit()
    log.info("删除文件（单集）- torrent=%s", torrent_id)
    return True


async def delete_files(anime_id: int) -> int:
    """删除该番在 qB 里的已下/在下种子及其硬盘文件（走 qB 正规接口，非裸删文件系统）。

    显式、独立于『拒绝』的动作，需 UI 二次确认。成功后把这些种子标记回 skipped（不再持有）。
    返回删除的种子数；qB 未连上/无已下则返回 0。
    """
    with get_session() as s:
        rows = list(s.exec(select(Torrent).where(
            Torrent.anime_id == anime_id,
            Torrent.status.in_(["downloaded", "downloading"]),
        )))
        pairs = [(t.id, t.info_hash) for t in rows]
    if not pairs:
        return 0
    ok = await qb.delete([h for _, h in pairs], delete_files=True)
    if not ok:
        return 0
    with get_session() as s:
        for tid, _ in pairs:
            t = s.get(Torrent, tid)
            if t is not None:
                t.status = "skipped"  # 文件已删，标记不再持有
                s.add(t)
        s.commit()
    log.info("删除文件 - anime=%s 共 %d 个种子", anime_id, len(pairs))
    return len(pairs)


# ---------------- 源组（字幕组）管理 ----------------

def list_source_groups(enabled_only: bool = False) -> list[SourceGroup]:
    with get_session() as s:
        q = select(SourceGroup)
        if enabled_only:
            q = q.where(SourceGroup.enabled == True)  # noqa: E712
        return list(s.exec(q.order_by(SourceGroup.priority.desc(), SourceGroup.id)))


def add_source_group(name, site, feed, policy, priority, enabled=True, subgroups="", title_filter="") -> None:
    with get_session() as s:
        s.add(SourceGroup(name=name, site=site, feed=feed, policy=policy,
                          priority=int(priority), enabled=enabled, subgroups=subgroups,
                          title_filter=title_filter))
        s.commit()


def update_source_group(gid: int, **fields) -> None:
    with get_session() as s:
        g = s.get(SourceGroup, gid)
        if g is None:
            return
        for k, v in fields.items():
            setattr(g, k, v)
        s.add(g)
        s.commit()


def delete_source_group(gid: int) -> None:
    with get_session() as s:
        g = s.get(SourceGroup, gid)
        if g is not None:
            s.delete(g)
            s.commit()


def backfill_mikan_whitelist() -> None:
    """老库升级：Mikan 组的白名单若从未设过(NULL)，用全局 config.MIKAN_SUBGROUPS 回填一次。

    必须在 worker 首轮轮询『之前』调用，否则首轮 Mikan 无白名单会漏进非目标字幕组。
    """
    if not config.MIKAN_SUBGROUPS:
        return
    with get_session() as s:
        for g in s.exec(select(SourceGroup).where(SourceGroup.site == "mikan")):
            if g.subgroups is None:  # 从未设过（新加列旧行为 NULL）；显式设成 '' 的不动
                g.subgroups = ",".join(config.MIKAN_SUBGROUPS)
                s.add(g)
        s.commit()


def backfill_seasons() -> int:
    """老库升级：用已存的 bgm 规范名/日文名回填季号（第X季/Season N），纠正早先解析成第1季的存量。返回修正条数。"""
    n = 0
    with get_session() as s:
        for a in s.exec(select(Anime)).all():
            sn = season_from_name(a.display_name) or season_from_name(a.jp_name)
            if sn and sn != a.season:
                log.info("回填季号 - %s 第%s季 → 第%s季", a.display_name or a.title, a.season, sn)
                a.season = sn
                s.add(a)
                n += 1
        if n:
            s.commit()
    return n


def backfill_quarters() -> int:
    """老库升级：用 bgm 放送日回填季度。

    季度早先可能来自种子发布时间（对长期连载/中途入库的番不准，如 50 集里抓到第 40 集）；
    bgm 放送日才是首播季度。只纠正『有放送日、无已下集、且当前季度与放送日不符』的番，
    连带把其未下种子的季度也一起对齐（决定下载目录）。返回修正条数。"""
    n = 0
    with get_session() as s:
        for a in s.exec(select(Anime).where(Anime.air_date.is_not(None))):
            try:
                q = extract_quarter(datetime.strptime(a.air_date, "%Y-%m-%d"))
            except (ValueError, TypeError):
                continue
            if q == a.quarter or _has_downloads(s, a.id):
                continue
            log.info("回填季度 - %s %s → %s", a.display_name or a.title, a.quarter or "?", q)
            a.quarter = q
            s.add(a)
            for t in s.exec(select(Torrent).where(Torrent.anime_id == a.id)):
                if t.status not in ("downloaded", "downloading"):
                    t.quarter = q
                    s.add(t)
            n += 1
        if n:
            s.commit()
    return n


def backfill_unmatched_review() -> int:
    """老库升级：未匹配 bgm(bangumi_id 为空)却已被自动确认的番，改回未确认——未富集的不该自动下。返回条数。"""
    n = 0
    with get_session() as s:
        for a in s.exec(select(Anime).where(
                Anime.bangumi_id.is_(None), Anime.rejected.is_not(True),
                Anime.confirmed == True)):  # noqa: E712
            log.info("未匹配转待确认 - %s", a.title)
            a.confirmed = False
            a.if_down = False
            s.add(a)
            n += 1
        if n:
            s.commit()
    return n


def seed_source_groups() -> None:
    """首启种入现有的 ANi(全下) + Mikan(审核)，保持原行为，也给个可编辑的起点。"""
    with get_session() as s:
        if s.exec(select(SourceGroup)).first() is not None:
            return
        s.add(SourceGroup(name="ANi", site="nyaa", feed=config.ANI_RSS_URL,
                          policy="auto", priority=100, enabled=True))
        s.add(SourceGroup(name="Mikan", site="mikan", feed=config.MIKAN_RSS_URL,
                          policy="review", priority=10, enabled=config.MIKAN_ENABLED,
                          subgroups=",".join(config.MIKAN_SUBGROUPS)))
        s.commit()


# ---------------- 迁移：旧模型(每写法一条 + merged_into) → 对照模型 ----------------

def migrate_to_alias_model() -> None:
    """把旧库迁到『唯一 Anime + TitleAlias』模型。幂等；旧库特征 = anime 有 merged_into 列。"""
    import sqlalchemy as sa
    from db import engine

    insp = sa.inspect(engine)
    if not insp.has_table("anime"):
        return
    cols = {c["name"] for c in insp.get_columns("anime")}
    if "merged_into" not in cols:
        return  # 已是新模型（或全新库）

    bak = config.DB_PATH + ".bak"
    if not os.path.exists(bak):  # 只在首次迁移时备份，别用已迁移数据覆盖原始备份
        try:
            shutil.copy(config.DB_PATH, bak)
            log.info("迁移前已备份数据库 → %s", bak)
        except Exception as e:
            log.warning("备份失败（继续迁移）: %s", e)

    with engine.connect() as conn:
        rows = conn.exec_driver_sql("SELECT id, title, season, merged_into FROM anime").fetchall()

    # 跟随 merged_into 合并链到根，避免 A→B→C 时别名指向中间的落败番（会被删成悬空）
    merged_map = {aid: merged for aid, title, season, merged in rows}

    def _root(aid):
        seen = set()
        while merged_map.get(aid) is not None and aid not in seen:
            seen.add(aid)
            aid = merged_map[aid]
        return aid

    with get_session() as s:
        existing = {(a.title, a.season) for a in s.exec(select(TitleAlias))}
        losers = []
        for aid, title, season, merged in rows:
            target = _root(aid)
            if (title, season) not in existing:
                s.add(TitleAlias(title=title, season=season, anime_id=target))
                existing.add((title, season))
            if merged is not None:
                losers.append(aid)
        s.commit()

        # 先删落败番，再回填/修复种子 anime_id（valid 集据此判定，顺带修历史悬空引用）
        for aid in losers:
            obj = s.get(Anime, aid)
            if obj is not None:
                s.delete(obj)
        s.commit()

        alias_map = {(a.title, a.season): a.anime_id for a in s.exec(select(TitleAlias))}
        valid = {a.id for a in s.exec(select(Anime))}
        for t in s.exec(select(Torrent)):
            if t.anime_id and t.anime_id in valid:
                continue  # 已正确关联（幂等）
            aid = alias_map.get((t.anime_title, t.season))
            if aid is not None and aid in valid:
                t.anime_id = aid
                s.add(t)
        s.commit()
    log.info("迁移到对照模型完成：对照 %d 条，删除落败番 %d 条", len(existing), len(losers))
