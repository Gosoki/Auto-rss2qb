"""TV 番剧主流程 + 给 UI 的查询/操作（剧场版/OVA 在 movies.py，两者只共用 engine 底层）。

一条标准条目进来 → 按 info_hash 去重 → 用『番名对照(AnimeAlias)』定位到唯一的番
(命中即知；未命中则查一次 bgm，有对应番就复用、否则新建) → 入库种子(带 anime_id) →
由 flush_ready_downloads 按『缓冲窗口 + 优先级』对每集只下一份。
"""
import asyncio
import logging
import re
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlmodel import func, select

import config
from core import engine
from db import get_session
from db.models import Anime, AnimeTorrent, MovieTorrent, SourceGroup, AnimeAlias
from services import enrich
from services.notify import notify
from sources.parse import candidate_names, extract_quarter, season_from_name

log = logging.getLogger("autorss")

# 串行化『选集→占位下载』，防止 worker flush 与 UI 补下并发对同一集重复放行
_download_lock = asyncio.Lock()


def quarter_brief() -> list[dict]:
    """番剧列表页顶部小结：当季 + 上季 的番剧流水线分布 + 种子维度。"""
    cur = extract_quarter(datetime.now())
    prev = engine.prev_quarter(cur)
    with get_session() as s:
        # 番数按当季/上季两季拉；种子维度按 (季度,状态) 在库内聚合，都不整表扫、不把种子行拉进内存
        animes = list(s.exec(select(
            Anime.quarter, Anime.confirmed, Anime.rejected, Anime.bangumi_id)
            .where(Anime.quarter.in_([cur, prev]))))
        tcounts = list(s.exec(
            select(AnimeTorrent.quarter, AnimeTorrent.status, func.count())
            .where(AnimeTorrent.quarter.in_([cur, prev]))
            .group_by(AnimeTorrent.quarter, AnimeTorrent.status)))
    out = []
    for tag, q in (("当季", cur), ("上季", prev)):
        aq = [(conf, rej, bid) for aqk, conf, rej, bid in animes if (aqk or "") == q]
        qc = {st: c for tqk, st, c in tcounts if (tqk or "") == q}
        out.append({
            "tag": tag, "key": q,
            # 互斥四分：已忽略(rej) / 待识别(未匹配 bgm) / 待确认(有 bgm 未确认) / 追番中(有 bgm 已确认)
            "shows": sum(1 for conf, rej, bid in aq if conf and not rej and bid),      # 追番中
            "confirm": sum(1 for conf, rej, bid in aq if not conf and not rej and bid),  # 待确认
            "fail": sum(1 for conf, rej, bid in aq if not rej and not bid),    # 待识别(未匹配)
            "ignored": sum(1 for conf, rej, bid in aq if rej),                 # 已忽略
            "torrents": sum(qc.values()),
            "done": qc.get("downloaded", 0),
            "pending": qc.get("pending", 0) + qc.get("error", 0),
        })
    return out


def _is_auto(kind: str) -> bool:
    return kind == "auto"


def _parse_date(s):
    """把 'YYYY-MM-DD'(或 'YYYY-MM'/'YYYY') 解析成 date；解析不出返回 None。"""
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _aired_before_start(air_date) -> bool:
    """该番开播日是否早于『开始使用日』(config.ANIME_START_DATE)。开始日空/开播日未知 → False(不判超期)。"""
    start = _parse_date(config.ANIME_START_DATE)
    aired = _parse_date(air_date)
    return bool(start and aired and aired < start)


def apply_start_date_filter() -> int:
    """按『开始使用日』重算超期忽略。超期忽略 = (rejected=True, confirmed=False)——人工拒绝必是 confirmed=True，
    故此组合唯一表示超期忽略、可与人工决定区分。可逆、只动该动的番：
    · 超期(开播<开始日) 且 仍待确认(未确认未拒) → 判超期忽略(置 rejected，confirmed 保持 False)，不自动下；
    · 已不超期(改早开始日/未知开播日/关闭) 且 当前是超期忽略 → 释放回待确认(清 rejected)。
    人工确认(confirmed=True)、人工拒绝(rejected 且 confirmed=True) 一律不碰——改日期不会掀翻用户的手动决定。返回变更数。"""
    changed = 0
    with get_session() as s:
        for a in s.exec(select(Anime)):
            out = _aired_before_start(a.air_date)
            if out and not a.rejected and not a.confirmed:      # 超期 + 待确认 → 判超期忽略
                a.rejected = True
                s.add(a); changed += 1
            elif not out and a.rejected and not a.confirmed:    # 不再超期 + 当前是超期忽略 → 释放回待确认
                a.rejected = False
                s.add(a); changed += 1
        if changed:
            s.commit()
            log.info("开始使用日过滤：%d 部番超期状态变更", changed)
    return changed


def ignore_confirmed_before_start() -> int:
    """一次性：把『开始使用日之前开播、当前已确认(追番中)』的番也转为超期忽略(rejected=True, confirmed=False)。
    供设置里手动触发——自动确认与人工确认都是 confirmed=True、无法区分，故这步须用户显式执行；执行后想留哪部再单独恢复。
    未设开始使用日则不动。返回处理数。"""
    if not _parse_date(config.ANIME_START_DATE):
        return 0
    changed = 0
    with get_session() as s:
        for a in s.exec(select(Anime).where(Anime.confirmed == True, Anime.rejected.is_not(True))):  # noqa: E712
            if _aired_before_start(a.air_date):
                a.confirmed, a.rejected = False, True
                s.add(a); changed += 1
        if changed:
            s.commit()
            log.info("一次性：把 %d 部开始日前的已确认老番转为超期忽略", changed)
    return changed


def _kw_match(kw: str, raw: str) -> bool:
    """版本关键词是否命中种子原名？大小写不敏感子串（繁日/简日/1080p 等）。调用方保证 kw 非空。"""
    return kw.lower() in (raw or "").lower()


def _apply_bgm(a: Anime, info: dict | None, keep_quarter: bool = False) -> None:
    """把 enrich 结果写进 TV 番（engine 落库 + 按 bgm 规范名纠正季号）。"""
    engine.apply_bgm_meta(a, info, keep_quarter)
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
        alias = s.exec(select(AnimeAlias).where(
            AnimeAlias.title == item.anime_title, AnimeAlias.season == item.season)).first()
        if alias is not None:
            return alias.anime_id

    # 未命中：富集定身份（尽力而为，拿不到就当独立新番）
    info = await enrich.resolve(item.search_names, item.release_time, item.episode, item.info_hash)

    with get_session() as s:
        alias = s.exec(select(AnimeAlias).where(  # 重入保护：再查一次
            AnimeAlias.title == item.anime_title, AnimeAlias.season == item.season)).first()
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
                confirmed=auto,
            )
            _apply_bgm(anime, info)   # 落 air_date 等 bgm 字段（下面判超期要用）
            if anime.confirmed and _aired_before_start(anime.air_date):
                anime.confirmed, anime.rejected = False, True   # 自动确认但早于开始使用日 → 超期忽略，不自动下
            s.add(anime)
            s.commit()          # Anime 无唯一约束，(title,季) 的去重由 AnimeAlias 负责，此处不会撞约束
            s.refresh(anime)
        # 登记番名对照（并发/竞态下可能已存在则忽略）
        if not s.exec(select(AnimeAlias).where(
                AnimeAlias.title == item.anime_title, AnimeAlias.season == item.season)).first():
            s.add(AnimeAlias(title=item.anime_title, season=item.season, anime_id=anime.id))
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
        return anime.id


async def process_item(item) -> bool:
    """处理一条标准条目。返回 True 表示是新种子（之前没见过）。"""
    # 1) 种子级去重：同一 hash 见过就跳过（跨源相等）
    with get_session() as s:
        if s.exec(select(AnimeTorrent).where(AnimeTorrent.info_hash == item.info_hash)).first() is not None:
            return False

    # 2) 定位到唯一的番（对照命中不查 bgm；未命中查一次）
    anime_id = await _resolve_anime(item)

    # 3) 入库种子（带 anime_id）。一般不在这里下：交给 flush_ready_downloads。
    with get_session() as s:
        a = s.get(Anime, anime_id)
        torrent = AnimeTorrent(
            info_hash=item.info_hash,
            anime_id=anime_id,
            source=item.source,
            site=item.site,
            anime_title=item.anime_title,
            raw_title=item.raw_title,
            season=item.season,
            episode=item.episode,
            quarter=a.quarter if a else item.quarter,
            download_url=item.download_url,
            release_time=item.release_time,
            priority=item.priority,
            status="pending",
        )
        s.add(torrent)
        try:
            s.commit()
        except IntegrityError:
            # 并发写入（如同时的剧场版发现）已插了同 hash → 视作已存在，跳过
            s.rollback()
            return False
        s.refresh(torrent)
        torrent_id = torrent.id
        # 自动升确认：auto 源为『已识别、未确认、未拒』的番贡献种子 → 转自动下。
        # 救回『review/泛 feed 先建番把 auto 主力番静默压进待确认』与『bgm 瞬时失败先建的番』。
        if (a is not None and not a.confirmed and not a.rejected
                and a.bangumi_id is not None and _is_auto(item.source_kind)):
            if _aired_before_start(a.air_date):
                a.rejected = True   # 超期(早于开始使用日) → 判超期忽略(rejected 且 confirmed 仍 False)，不自动下（种子照常入库）
            else:
                a.confirmed = True
            s.add(a)
            s.commit()
        should_download = bool(a and a.confirmed and not a.rejected)
        lock = a.pref_source if a else None   # 锁定源：入库即下也只放行锁定组
        kw = a.pref_keyword if a else None     # 版本关键词：即时下载也只放行命中该版本的

    log.info("新增 - %s - %s 第%s季 第%s集", item.source, item.anime_title, item.season, item.episode)
    # 最高优先级即时下载：开关开 + 自动下的番 + 来自最高优先级组 + (未锁源或正是锁定源) → 入库就下，不等缓冲窗口。
    # 排除 -2(未知/批量)：与 flush 一致留人工处理，别让它是否被下取决于来源组优先级（instant 下、flush 不下）。
    if (config.ANIME_TOP_PRIORITY_INSTANT and should_download
            and item.episode != -2
            and (item.priority or 0) >= _top_priority()
            and (not lock or lock == (item.source or ""))
            and (not kw or _kw_match(kw, item.raw_title))):
        await download_anime_torrent(torrent_id)
    return True


async def download_anime_torrent(torrent_id: int, force: bool = False) -> bool:
    """取种子文件并加入 qBittorrent。成功返回 True。

    『选集去重 + 占位』整段放在 _download_lock 里做，且集去重同时看 downloading/downloaded，
    这样 worker flush 与 UI 补下并发时也不会对同一集重复放行两份。
    force=True：强制下这一条（无视当前状态、跳过集去重），用于详情页手动指定下载。
    """
    if not config.QB_ENABLED:
        return False  # 无 qB 模式：只采集元数据，不发送种子（保持 pending）

    async with _download_lock:
        with get_session() as s:
            t = s.get(AnimeTorrent, torrent_id)
            if t is None:
                return False
            if t.status in ("downloading", "downloaded") and not (force and t.archived_at is not None):
                return False  # 已在下/已下：幂等短路（force 也不例外）。例外：已归档的可 force 重新下（重新交回 qB）
            if not force and t.status not in ("pending", "error"):
                return False  # 非 force：只放行 pending/error；skipped/deleted 需 force 才强制下
            anime_id = t.anime_id
            episode = t.episode
            season = t.season
            title = t.anime_title
            # 跨表【不】去重：番剧/剧场版各下到各自目录（用户要各归各、重复提交也接受）。qB 按 hash 物理去重、
            # 不会真下两遍；某侧删文件后另一侧由 sync 落 error——不再造 progress=1 的幽灵 pointer（曾致删/下竞态静默丢文件）。
            # 同集去重：同一 (anime_id, 集) 已有别的种子在下/已下 → 跳过（force 时不去重，强制下这条）。
            # 注：deleted 不进去重集——用户删的是"那一条种子"，同集来了新 hash 允许照常自动下（deleted 本身
            # 状态非 pending、flush 不会自动选它，同 hash 也在入库处去重，故被删的那条不会自动回来；force 例外）。
            # 含特别篇 -1（每番只放一份，与 flush 的 have_special 意图一致）；-2 未知集按设计逐个下、不去重。
            if not force and isinstance(episode, (int, float)) and (episode >= 0 or episode == -1) and anime_id:
                dup = s.exec(select(AnimeTorrent).where(
                    AnimeTorrent.anime_id == anime_id,
                    AnimeTorrent.episode == episode,
                    AnimeTorrent.status.in_(["downloading", "downloaded"]),
                    AnimeTorrent.id != torrent_id,
                )).first()
                if dup is not None:
                    t.status = "skipped"
                    s.add(t)
                    s.commit()
                    log.info("跳过重复集 - %s 第%s季 第%s集（已有一份在下/已下）", title, season, episode)
                    return False
            t.status = "downloading"  # 原子占位（锁内，别的协程看得到）
            # 重新下：清归档标记 + 重置 qB 实时态，让它作为『全新在下』被重新跟踪、从新完成点重算归档倒计时——
            # 否则重下已归档的种子会带着旧 qb_progress=1/旧完成时间，被下一轮完成归档立刻再归档掉。
            t.archived_at = None
            t.qb_progress, t.qb_state, t.qb_synced_at, t.qb_progress_at = 0.0, "", None, None
            s.add(t)
            s.commit()
            url = t.download_url
            info_hash = t.info_hash
            a = s.get(Anime, anime_id) if anime_id else None
            # 季度与季号都以 bgm 纠正后的 Anime 为准：种子行的 quarter/季号是入库时快照，重识别后会过时，
            # 沿用会把同一部番的新旧集散到两个季度目录（有下载时 keep_quarter 已锁死 a.quarter 保持稳定）。
            quarter = (a.quarter if (a and a.quarter) else t.quarter) or "unknown"
            # 文件夹名统一用 bgm 日语原名，没有再退中文规范名，最后退种子解析番名
            folder_name = (a and (a.jp_name or a.display_name)) or t.anime_title
            if a is not None:
                season = a.season  # 用 bgm 纠正后的季号建 Season 子目录（种子把续作季号常解析回 1）

    # 组装保存路径（含越界校验），TV 按设置可加 Season N 子目录
    save_path = engine.build_save_path(quarter, folder_name, season=season,
                                       sub_dir=config.ANIME_DOWN_PATH)
    if save_path is None:
        log.error("拒绝越界保存路径 - %s -> %s / %s", title, quarter, folder_name)
        _set_status(torrent_id, "error")
        return False
    try:
        data = await engine.fetch_torrent_bytes(url)
        ok = await engine.add_to_qb(data, save_path, f"autoRSS {quarter}", quarter, info_hash=info_hash)
    except asyncio.CancelledError:
        _set_status(torrent_id, "pending")  # 被取消（关停等）→ 复位，别永久卡 downloading
        raise
    except Exception as e:  # 任何失败都回写 error，避免卡在 downloading
        log.error("下载失败 - %s - %s", title, e)
        _set_status(torrent_id, "error")
        return False

    if not ok:
        _set_status(torrent_id, "error")
        return False
    with get_session() as s:   # 记实际保存路径：改季度/重绑后据此移动或提醒旧位置
        t = s.get(AnimeTorrent, torrent_id)
        if t is not None:
            t.save_path = save_path
            s.add(t)
            s.commit()
    if config.QB_SYNC_STATUS:
        _set_status(torrent_id, "downloaded")
        engine.qb_kick.set()   # 唤醒 qB 同步循环，立即开始跟这个新交付的种子
    else:
        engine.settle_downloaded(AnimeTorrent, torrent_id)  # 关跟踪：发送即已下，落定 qb_progress=1、脱离 in-flight
    log.info("已加入qB - %s 第%s季 第%s集", title, season, episode)
    await notify(f"{title}[{episode}] 📥")
    return True


def _set_status(torrent_id: int, status: str) -> None:
    engine.set_torrent_status(AnimeTorrent, torrent_id, status)


def reset_downloading() -> None:
    """启动时把上次异常退出遗留的 downloading 复位为 pending，好被重新下。"""
    engine.reset_downloading(AnimeTorrent)


def _revive_orphaned_skipped() -> None:
    """换源兜底：某集的胜出源事后转 error（qB 侧失败/消失），而该集已无在下/已下/已删时，把当初被同集去重
    压成 skipped 的兄弟种子放回 pending——否则 flush/补下都只挑 pending/error、永不碰 skipped，该集会永久卡死
    在唯一失败源上、别的可用源被 skipped 终态排除。只对『已确认未拒绝』的自动番生效；deleted 的集不复活（用户
    特意删的不重下，与 restore_anime 口径一致）。幂等：skipped→pending 后不再是 skipped，收敛于源数上限。"""
    with get_session() as s:
        auto_ids = set(s.exec(select(Anime.id).where(
            Anime.confirmed == True, Anime.rejected.is_not(True))))  # noqa: E712
        if not auto_ids:
            return
        rows = list(s.exec(select(AnimeTorrent).where(
            AnimeTorrent.anime_id.in_(auto_ids),
            AnimeTorrent.status.in_(["error", "skipped", "downloaded", "downloading", "deleted", "stalled"]))))
        by_ep: dict = {}
        for t in rows:
            by_ep.setdefault((t.anime_id, t.episode), set()).add(t.status)
        # 目标集：有 error，且无 downloaded/downloading/deleted/stalled（首选已败、该集尚无可用/已删/停滞的下载）。
        # stalled 也算『已处理』→ 不复活兄弟源：停滞的那条留人工处理，不自动换源（与 flush 阻断口径一致）。
        revive = {k for k, sts in by_ep.items()
                  if "error" in sts and not ({"downloaded", "downloading", "deleted", "stalled"} & sts)}
        if not revive:
            return
        changed = 0
        for t in rows:
            if t.status == "skipped" and (t.anime_id, t.episode) in revive:
                t.status = "pending"
                s.add(t)
                changed += 1
        if changed:
            s.commit()
            log.info("换源兜底：复活 %d 个被去重的 skipped 兄弟（该集首选源已失败）", changed)


async def flush_ready_downloads() -> int:
    """缓冲窗口 + 严格优先级：每轮跑一次。

    对『自动下载且已确认』的番，把待下种子按 (anime_id, 集) 归组——因为按番的真实身份
    分组，不同组不同写法的同一集会算作同一集，天然只留一份。每集首次被发现后满
    config.ANIME_DOWNLOAD_GRACE_MIN 分钟才放行，到点从该集所有种子挑优先级最高的下一份（错误的排后，
    留作降级）。特别篇/未知集不做集去重，逐个下。返回实际触发下载的数量。
    """
    _revive_orphaned_skipped()   # 先把『首选源已失败、该集无其它下载』的 skipped 兄弟放回 pending，本轮即可换源
    grace = timedelta(minutes=max(0, config.ANIME_DOWNLOAD_GRACE_MIN))  # 负值会使门槛永假、废掉多源补齐，钳到 0
    now = datetime.now()
    chosen: list[int] = []
    with get_session() as s:
        auto = list(s.exec(
            select(Anime).where(Anime.confirmed == True, Anime.rejected.is_not(True))  # noqa: E712
        ))
        auto_ids = {a.id for a in auto}
        pref_map = {a.id: a.pref_source for a in auto if a.pref_source}
        kw_map = {a.id: a.pref_keyword for a in auto if a.pref_keyword}
        if not auto_ids:
            return 0
        # 『该集已有一份』阻断自动换源的集：downloaded（已下/在下的交付）+ stalled（停滞异常，留着人工处理，
        # 不自动抓另一个源顶上）。deleted 不算——删的那条不自动回来，但同集来新 hash 仍允许自动下（非整集拉黑）。
        downloaded = {
            (t.anime_id, t.episode)
            for t in s.exec(select(AnimeTorrent).where(
                AnimeTorrent.status.in_(["downloaded", "stalled"])))
        }
        groups: dict = {}
        special_groups: dict = {}          # anime_id -> [特别篇(-1)种子]，按番去重只放一份
        # 只自动放行 pending：error 不在这里无限重试（高优先级失败→本组还有 pending 低优先级自然降级；
        # 全 error 则本轮不重试，留给人工补下）。
        for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.status == "pending")):
            if t.anime_id not in auto_ids:
                continue
            lock = pref_map.get(t.anime_id)
            if lock and lock != (t.source or ""):
                continue  # 锁定源：这部番只收锁定组的种子（硬锁、不兜底）；别的源一律不自动下
            kw = kw_map.get(t.anime_id)
            if kw and not _kw_match(kw, t.raw_title):
                continue  # 版本关键词：只收命中该版本的（硬锁、不兜底）
            if t.episode is None or t.episode < 0:
                if t.episode == -1:
                    special_groups.setdefault(t.anime_id, []).append(t)  # 特别篇按番归组
                continue  # -2(未知/疑似批量) 不自动下，可人工补下
            groups.setdefault((t.anime_id, t.episode), []).append(t)

    def _pick(ts, aid):
        return engine.pick_best(ts, pref_map.get(aid))

    for key, ts in groups.items():
        if key in downloaded:
            continue  # 这一集已有一份
        first_seen = min(t.created_at for t in ts)
        if now - first_seen < grace:
            continue  # 缓冲窗口未到，等偏好组
        chosen.append(_pick(ts, key[0]).id)  # key = (anime_id, episode)
    # 特别篇：每番只放一份（多字幕组版本别全下），走同样的缓冲窗口，且该番未下过特别篇才放
    have_special = {aid for (aid, ep) in downloaded if ep == -1}
    for aid, ts in special_groups.items():
        if aid in have_special:
            continue
        if now - min(t.created_at for t in ts) < grace:
            continue
        chosen.append(_pick(ts, aid).id)

    n = 0
    for tid in chosen:
        if await download_anime_torrent(tid):
            n += 1
    return n


# ---------------- 给 UI 的查询 ----------------

def overview() -> dict:
    """概览页所需的全部聚合数据，一次性算好；页面只负责渲染。"""
    with get_session() as s:
        animes = list(s.exec(select(Anime).where(Anime.rejected.is_not(True))))  # 非拒绝（含待确认）
        rejected = s.exec(select(func.count()).select_from(Anime)
                          .where(Anime.rejected == True)).one()  # noqa: E712
        groups = list(s.exec(select(SourceGroup)))
        all_aq = list(s.exec(select(Anime.id, Anime.quarter)))  # 所有 TV 番(含待确认/忽略)的 id+季度
        # 种子维度全用 SQL 聚合（GROUP BY count / DISTINCT）：种子攒到几千条也不把整表拉进内存，
        # 只在库内算完返回几个数字——CPU/内存/DB 传输都轻。
        status = {st: c for st, c in s.exec(
            select(AnimeTorrent.status, func.count()).group_by(AnimeTorrent.status))}
        total_torrents = s.exec(select(func.count()).select_from(AnimeTorrent)).one()
        dl_ids = set(s.exec(select(AnimeTorrent.anime_id)
                            .where(AnimeTorrent.status == "downloaded").distinct()))
        src_total = {src: c for src, c in s.exec(
            select(AnimeTorrent.source, func.count()).group_by(AnimeTorrent.source))}
        src_done = {src: c for src, c in s.exec(
            select(AnimeTorrent.source, func.count())
            .where(AnimeTorrent.status == "downloaded").group_by(AnimeTorrent.source))}

    confirmed = [a for a in animes if a.confirmed]
    pending_c = [a for a in animes if not a.confirmed and a.bangumi_id]  # 待确认=已匹配未确认；未匹配的算『富集失败』

    # 各季度：总番数（含待确认/待识别/已忽略）+ 有已下集的番数（真·比例，分子分母同为"部"）
    total_by_q = Counter((q or "未知") for _, q in all_aq)
    aid_q = {aid: (q or "未知") for aid, q in all_aq}
    dl_by_q = Counter(aid_q[aid] for aid in dl_ids if aid in aid_q)
    qs = sorted((q for q in total_by_q if q != "未知"), reverse=True)
    if "未知" in total_by_q:
        qs.append("未知")
    by_quarter = [(q, total_by_q.get(q, 0), dl_by_q.get(q, 0)) for q in qs]

    # 各季度番剧按流水线 3 桶：订阅(已确认)/审核(未确认待处理=待确认+待识别)/忽略(已拒绝)，互斥、和=该季总番数
    nonrej_ids = {a.id for a in animes}
    sub_by_q = Counter((a.quarter or "未知") for a in animes if a.confirmed)
    rev_by_q = Counter((a.quarter or "未知") for a in animes if not a.confirmed)
    ign_by_q = Counter(aid_q[aid] for aid, _ in all_aq if aid not in nonrej_ids)
    by_quarter_state = [(q, sub_by_q.get(q, 0), rev_by_q.get(q, 0), ign_by_q.get(q, 0)) for q in qs]

    # 各来源：种子数 + 已下
    by_source = sorted((((src or "?"), cnt, src_done.get(src, 0)) for src, cnt in src_total.items()),
                       key=lambda x: -x[1])

    split = pending_breakdown()   # 待下拆 将下载/备用/待确认/未知，算一次给 KPI 与状态区共用
    return {
        "kpi": {
            "tracking": len(confirmed), "fail": sum(1 for a in animes if not a.bangumi_id),
            "confirm": len(pending_c), "rejected": rejected,
            "done": status.get("downloaded", 0),
            "will": split["will"],   # 顶部只汇总『真会自动下的』，别再用糊在一起的 pending 总数误导
            "torrents": total_torrents,
        },
        "status": {k: status.get(k, 0) for k in
                   ("downloaded", "downloading", "pending", "error", "skipped", "stalled")},
        "pending_split": split,
        "by_quarter": by_quarter,
        "by_quarter_state": by_quarter_state,
        "by_source": by_source,
        "enriched": (sum(1 for a in animes if a.bangumi_id), len(animes)),
        "groups": [(g.name, g.site, g.policy, g.priority, g.enabled)
                   for g in sorted(groups, key=lambda g: -g.priority)],
        "config": {"qb": config.QB_ENABLED, "poll_on": config.ANIME_POLL_ENABLED,
                   "poll": config.ANIME_POLL_INTERVAL, "grace": config.ANIME_DOWNLOAD_GRACE_MIN},
        "qb": engine.qb_summary(AnimeTorrent),
    }


def list_all_anime() -> list[Anime]:
    """管理页统一视图：所有番（含待确认、已拒绝）；组内排序（状态垫底）交给页面。"""
    with get_session() as s:
        return list(s.exec(select(Anime).order_by(Anime.quarter.desc(), Anime.id)))


def list_rejected_anime() -> list[Anime]:
    """已拒绝的番（『拒绝』页展示，可恢复）。"""
    with get_session() as s:
        return list(s.exec(
            select(Anime).where(Anime.rejected == True)  # noqa: E712
            .order_by(Anime.quarter.desc(), Anime.id)
        ))


def list_unmatched_anime() -> list[Anime]:
    """未匹配 bgm 的番（bangumi_id 为空、未拒绝）——供『待识别』页人工处理。"""
    with get_session() as s:
        return list(s.exec(
            select(Anime).where(
                Anime.bangumi_id.is_(None), Anime.rejected.is_not(True))
            .order_by(Anime.created_at.desc())
        ))


def source_map() -> dict:
    """{番 id: [来源...]}，含所有有种子的番（管理页据此标『多源 N / 单源 1』）。"""
    from collections import defaultdict
    with get_session() as s:
        # DISTINCT 让库内先去重 (番,来源) 对，返回的行数只与『番×来源』有关，与种子总数无关
        pairs = list(s.exec(select(AnimeTorrent.anime_id, AnimeTorrent.source).distinct()))
    src: dict = defaultdict(set)
    for aid, source in pairs:
        if aid:
            src[aid].add(source)
    return {aid: sorted(v) for aid, v in src.items() if v}


def pending_confirm() -> list[Anime]:
    """待确认：已匹配 bgm 但未确认、未拒绝的番。未匹配的在『待识别』，绑定后才来这里。"""
    with get_session() as s:
        return list(s.exec(select(Anime).where(
            Anime.confirmed == False, Anime.rejected.is_not(True),  # noqa: E712
            Anime.bangumi_id.is_not(None))))


def recent_anime_rows(limit: int = 50) -> list[dict]:
    """新入库列表：种子 + 番的规范名（比原始解析名可读）+ 原始种子标题（区分同集不同版本）。

    AnimeTorrent 表只含 TV 种子（剧场版/OVA 在 MovieTorrent），故无需再过滤。
    """
    with get_session() as s:
        ts = list(s.exec(select(AnimeTorrent).order_by(AnimeTorrent.created_at.desc()).limit(limit)))
        ids = {t.anime_id for t in ts if t.anime_id}
        names = ({a.id: (a.display_name or a.title) for a in
                  s.exec(select(Anime).where(Anime.id.in_(ids)))} if ids else {})
    return [{
        "id": t.id,
        "anime_id": t.anime_id,
        "time": engine.torrent_time(t),
        "name": names.get(t.anime_id) or (t.anime_title or "?"),
        "episode": t.episode,
        "source": t.source,
        "status": t.status,
        "qb_state": t.qb_state,
        "qb_progress": t.qb_progress,
        "qb_synced_at": t.qb_synced_at,
        "qb_dlspeed": t.qb_dlspeed,
        "raw": t.raw_title or "",
    } for t in ts]


def inflight_anime_rows(limit: int = 50) -> list[dict]:
    """仪表盘『正在下载』区：当前在下的 TV 种子（口径同 has_inflight），按完成度降序、接近下完的在上。"""
    with get_session() as s:
        ts = list(s.exec(
            select(AnimeTorrent).where(*engine._inflight_where(AnimeTorrent))
            .order_by(AnimeTorrent.qb_progress.desc(), AnimeTorrent.created_at.desc()).limit(limit)))
        ids = {t.anime_id for t in ts if t.anime_id}
        names = ({a.id: (a.display_name or a.title) for a in
                  s.exec(select(Anime).where(Anime.id.in_(ids)))} if ids else {})
    return [{
        "id": t.id,
        "name": names.get(t.anime_id) or (t.anime_title or "?"),
        "episode": t.episode,
        "status": t.status,
        "qb_state": t.qb_state,
        "qb_progress": t.qb_progress,
        "qb_synced_at": t.qb_synced_at,
        "qb_dlspeed": t.qb_dlspeed,
    } for t in ts]


def confirmed_anime_ids(ids) -> set:
    """给定番 id 集合，返回其中『已确认下载』的。新入库里未确认（待确认）番的待下不显示
    将下载/备用（那是假的、要点确认才会下），而是显示『待确认』——故先筛出已确认的。"""
    ids = {i for i in ids if i}
    if not ids:
        return set()
    with get_session() as s:
        return set(s.exec(select(Anime.id).where(
            Anime.id.in_(ids), Anime.confirmed == True)))  # noqa: E712


def get_anime(anime_id: int) -> Anime | None:
    with get_session() as s:
        return s.get(Anime, anime_id)


def list_episodes(anime_id: int) -> list[AnimeTorrent]:
    """某番剧的全部种子（按集数、再按入库时间倒序），供详细页展示分集/来源。"""
    with get_session() as s:
        return list(s.exec(
            select(AnimeTorrent).where(AnimeTorrent.anime_id == anime_id)
            .order_by(AnimeTorrent.episode, AnimeTorrent.created_at.desc())
        ))


def anime_sources(anime_id: int) -> list[str]:
    """某番剧现有的所有来源（去重排序），供待确认/详情页展示与选源。"""
    with get_session() as s:
        rows = s.exec(select(AnimeTorrent.source).where(AnimeTorrent.anime_id == anime_id)).all()
    return sorted({r for r in rows if r})


def downloaded_count(anime_id: int) -> int:
    """该番已下/在下（硬盘上有文件）的种子数——供 UI 决定要不要显示『删除文件』。"""
    with get_session() as s:
        return len(s.exec(select(AnimeTorrent.id).where(
            AnimeTorrent.anime_id == anime_id,
            AnimeTorrent.status.in_(["downloaded", "downloading"]),
        )).all())


# ---------------- 给 UI 的操作 ----------------

def confirm_anime(anime_id: int, pref_source: str = "") -> None:
    """确认下载该番；pref_source 非空则锁定下载源（本次及以后只下这个组，缺集不兜底）。"""
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is not None:
            a.confirmed = True
            a.pref_source = pref_source or None
            s.add(a)
            s.commit()


def set_pref_source(anime_id: int, source: str) -> None:
    """设/改某番的锁定下载源（空=按优先级多源兜底；非空=锁定只下这个组）。详情页用。"""
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is not None:
            a.pref_source = source or None
            s.add(a)
            s.commit()


def set_pref_keyword(anime_id: int, keyword: str) -> None:
    """设/改某番的版本关键词（空=不限；非空=只下 raw_title 命中该词的版本，与锁定源叠加）。详情页用。"""
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is not None:
            a.pref_keyword = (keyword or "").strip() or None
            s.add(a)
            s.commit()


def set_quarter(anime_id: int, quarter: str) -> bool:
    """手动改某番的归档季度（内部键如 26A；bgm 三级兜底之外的最终人工纠错）。

    校验格式（两位年 + A/B/C/D）；成功返回 True。改后由调用方触发 relocate_anime 移动已下文件。
    """
    q = (quarter or "").strip().upper()
    if not re.fullmatch(r"\d{2}[A-D]", q):
        return False
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None:
            return False
        a.quarter = q
        s.add(a)
        s.commit()
    return True


def reject_anime(anime_id: int) -> None:
    """拒绝某个番：打上 rejected（移出主列表进『拒绝』页）、不下载，积压待下种子标记跳过。"""
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None:
            return
        a.rejected = True
        a.confirmed = True   # 人工拒绝置 confirmed=True → 与『超期忽略(confirmed=False)』区分，改开始日不会掀翻它
        s.add(a)
        for t in s.exec(select(AnimeTorrent).where(
            AnimeTorrent.anime_id == anime_id,
            AnimeTorrent.status.in_(["pending", "error"]),
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
        a.confirmed = True   # 恢复=确认，confirmed=True → 改开始日不会再把它判超期忽略（超期忽略需 confirmed=False）
        s.add(a)
        all_rows = list(s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == anime_id)))
        # deleted 也算『该集已处理过』：用户特意删过的集，其去重落选的 skipped 兄弟不该被复活重下。
        have_eps = {t.episode for t in all_rows if t.status in ("downloaded", "downloading", "deleted")}
        for t in all_rows:
            # 只放回『该集尚无下载/未被删过』的 skipped（集去重留下的旧版本）；用户主动删过的记为 deleted，
            # 其集已进 have_eps 而被排除——免得恢复订阅时把用户特意删掉的文件又重新下回来。
            if t.status == "skipped" and t.episode not in have_eps:
                t.status = "pending"
                s.add(t)
        s.commit()


def _merge_anime(s, loser_id: int, keeper_id: int) -> None:
    """把 loser 番的对照/种子/订阅状态并到 keeper，删除 loser（保持一个 bgm_id 唯一一部番）。

    keeper 恒为当前操作的番（可能是刚绑定的『待确认』残条），loser 可能才是已确认/已下的主番；
    故合并前先把订阅状态迁过来，别随 loser 一起删掉——否则番会静默从『追番中』掉回『待确认』停更。
    """
    if loser_id == keeper_id:
        return
    keeper = s.get(Anime, keeper_id)
    loser = s.get(Anime, loser_id)
    if keeper is not None and loser is not None:
        # 迁订阅态，别随 loser 删掉致停更/复活：追不追=confirmed 且未 rejected，按两方『活跃』并集；
        # 都不活跃时保留『拒绝优先于待确认』；pref_source 空则补。
        active = (keeper.confirmed and not keeper.rejected) or (loser.confirmed and not loser.rejected)
        if active:
            keeper.confirmed, keeper.rejected = True, False
        else:
            keeper.confirmed = keeper.confirmed or loser.confirmed
            keeper.rejected = keeper.rejected or loser.rejected
        if not keeper.pref_source and loser.pref_source:
            keeper.pref_source = loser.pref_source
        # 季度：keeper 尚未落盘而 loser 已有在下/已下文件时采用 loser 的季度，
        # 免得合并后新集去了 keeper 的（可能不同）季度目录，与已落盘的旧集散在两处。
        if loser.quarter and not _has_downloads(s, keeper_id) and _has_downloads(s, loser_id):
            keeper.quarter = loser.quarter
        s.add(keeper)
    for al in s.exec(select(AnimeAlias).where(AnimeAlias.anime_id == loser_id)):
        al.anime_id = keeper_id
        s.add(al)
    for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == loser_id)):
        t.anime_id = keeper_id
        s.add(t)
    if loser is not None:
        s.delete(loser)
    s.commit()


def _has_downloads(s, anime_id: int) -> bool:
    """该番是否已下过（在下/已下/曾删）——有则季度已落盘/曾落盘，不该被重识别改（避免散目录）。

    含 deleted：用户删过文件也算『季度已定』，否则全删后 keep_quarter 失效、重识别会把稳定季度冲掉。
    """
    return s.exec(select(AnimeTorrent).where(
        AnimeTorrent.anime_id == anime_id,
        AnimeTorrent.status.in_(["downloading", "downloaded", "deleted"]),
    )).first() is not None


async def enrich_anime(anime_id: int) -> bool:
    """手动富集某番剧：用它已有的名字 + 最近一条种子回退，重取 bgm 元数据并覆盖。"""
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None:
            return False
        t = s.exec(
            select(AnimeTorrent).where(AnimeTorrent.anime_id == anime_id)
            .order_by(AnimeTorrent.created_at.desc())
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


async def bind_anime_bgm(anime_id: int, bgm_id: int) -> bool:
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
        a.confirmed = False  # 绑定后进『待确认』，等人工确认下载
        # 无已下集就采用 bgm 季度（纠正错季度）；有已下集才保留，避免散目录
        _apply_bgm(a, info, keep_quarter=_has_downloads(s, anime_id))
        s.add(a)
        s.commit()
        # 身份守卫：该 bgm_id 已被别的番占用 → 合并过来，杜绝一部番裂成两条
        for other in list(s.exec(select(Anime).where(
                Anime.bangumi_id == bgm_id, Anime.id != a.id))):
            _merge_anime(s, other.id, a.id)
        # 显式绑定以用户意图为准：若并入的旧番曾被忽略、致 keeper 继承了 rejected，纠回『待确认』，
        # 别让"用户主动绑定识别"的番静默掉进已忽略、从此停更。
        a = s.get(Anime, a.id)
        if a is not None and a.rejected:
            a.rejected, a.confirmed = False, False
            s.add(a)
            s.commit()
    return True


async def reenrich_all() -> int:
    """对所有番重跑一次富集（回填 jp_name/规范名/简介/评分等新字段）。返回命中数。"""
    return await reenrich_scope(None)


async def reenrich_scope(seasons: int | None = None) -> int:
    """按季度范围重新识别（bgm）：seasons=1 当季 / 2 近半年 / 4 近1年 / None 全部。返回命中数。

    对范围内的番重跑一次识别——顺带把之前『待识别』(未匹配)的重试、已匹配的刷新元数据。
    """
    quarters = None
    if seasons:
        quarters, q = set(), extract_quarter(datetime.now())
        for _ in range(seasons):
            quarters.add(q)
            q = engine.prev_quarter(q)
    with get_session() as s:
        # 跳过已忽略的番：批量重识别不该 reenrich 忽略番——否则它拿到 bgm_id 触发身份合并、
        # union-active 会把『已忽略』静默复活成『追番中』。要刷新某忽略番元数据可进其详情页单独点『重新识别』。
        base = select(Anime.id).where(Anime.rejected.is_not(True))
        if quarters is None:
            ids = list(s.exec(base))
        else:
            ids = list(s.exec(base.where(Anime.quarter.in_(quarters))))
    n = 0
    for aid in ids:
        reset_enrich_tries(aid)   # 手动重识别：清零后台重试计数，让未识别番重新获得自动重试机会
        try:
            if await enrich_anime(aid):
                n += 1
        except Exception as e:
            log.warning("重新识别失败 anime=%s: %s", aid, e)
    log.info("重新识别（范围=%s）完成：%d/%d 命中", seasons or "全部", n, len(ids))
    return n


def reset_enrich_tries(anime_id: int) -> None:
    """清零某番的 bgm 后台重试计数（手动『重新识别』时调用，让它重新获得自动重试机会）。"""
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is not None and a.enrich_tries:
            a.enrich_tries = 0
            s.add(a)
            s.commit()


async def retry_unmatched() -> int:
    """后台延迟重试(指数退避)：对『待识别』(bangumi_id 空、未拒) 且未满次数上限的番，按『失败等待翻倍』重跑 bgm。

    每番下次到点 = max(上次尝试, 建番时) + min(BASE * 2^已试次数, MAX)；到点才试。每试记 enrich_tries += 1、
    刷新 last_enrich_at，满 REENRICH_MAX_TRIES 停自动、留手动（手动重识别清零重来）。enrich_anime 绕开
    alias 短路、真查 bgm；命中即落 bgm_id → 从『待识别』升『待确认』，之后有 auto 源再来种子会自动升确认。返回命中数。
    """
    cap = max(1, config.REENRICH_MAX_TRIES)
    base = max(1, config.REENRICH_RETRY_BASE) * 60          # 配置单位=分钟 → 秒
    maxd = max(base, config.REENRICH_RETRY_MAX * 60)
    now = datetime.now()
    due: list[int] = []
    with get_session() as s:
        for a in s.exec(select(Anime).where(
                Anime.bangumi_id.is_(None), Anime.rejected.is_not(True),
                Anime.enrich_tries < cap).order_by(Anime.enrich_tries)):
            delay = min(base * (2 ** (a.enrich_tries or 0)), maxd)  # 失败一次等待翻倍，封顶 MAX
            ref = a.last_enrich_at or a.created_at
            if (now - ref).total_seconds() >= delay:
                due.append(a.id)
                if len(due) >= 50:      # 单轮上限，防一次性狂打 bgm
                    break
    n = 0
    for aid in due:
        with get_session() as s:
            a = s.get(Anime, aid)
            if a is None:
                continue
            a.enrich_tries = (a.enrich_tries or 0) + 1
            a.last_enrich_at = datetime.now()
            s.add(a)
            s.commit()
        try:
            if await enrich_anime(aid):
                n += 1
        except Exception as e:
            log.warning("延迟重识别失败 anime=%s: %s", aid, e)
    if due:
        log.info("延迟重识别：%d 到点，命中 %d", len(due), n)
    return n


def _select_downloads(rows: list, pref: str | None = None, have_eps: set | None = None) -> list:
    """从一部番的待下种子里挑要下的：按集号分组，每集选一份（首选源优先、其次优先级）。
    特别篇(-1)、未知集(-2) 各自作为独立集号，互不挤占（下过 -1 不再挡待下的 -2，反之亦然）。
    have_eps 里的集（已在下/已下）跳过。返回选中的 AnimeTorrent 列表。
    """
    have = have_eps or set()

    def _best(cands: list):
        return engine.pick_best(cands, pref)

    # 按集号分组：正集每集一份；负集 -1/-2 各自独立成组、各一份。早前把所有负集并成一个
    # 互斥槽，会让下过 -1 就整组跳过、挡住待下的 -2（反之亦然）；按集号细分即可各行其是。
    by_ep: dict = {}
    for t in rows:
        if t.episode in have:
            continue
        by_ep.setdefault(t.episode, []).append(t)
    return [_best(ts) for ts in by_ep.values()]


async def download_pending_for_anime(anime_id: int) -> int:
    """把某番剧下 status=pending/error 的种子补下（人工确认后放行）。返回触发的下载数。

    加番剧级授权闸门：只对『已确认且未拒绝』的番补下。
    正集按集去重、负集整组一份（见 _select_downloads）——避免同一集/同片多版本被全部拉下。
    """
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None or not (a.confirmed and not a.rejected):
            return 0
        pref, kw = a.pref_source, a.pref_keyword
        all_rows = list(s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == anime_id)))
    have_eps = {t.episode for t in all_rows if t.status in ("downloaded", "downloading", "deleted")}  # deleted 也算已处理，不重下
    pending = [t for t in all_rows if t.status in ("pending", "error")]
    if pref:  # 锁定源：只补锁定组的待下集（硬锁、不兜底）
        pending = [t for t in pending if pref == (t.source or "")]
    if kw:     # 版本关键词：再过滤到命中该版本的（繁日/简日/画质…；硬锁、不兜底）
        pending = [t for t in pending if _kw_match(kw, t.raw_title)]
    chosen = _select_downloads(pending, pref, have_eps)
    n = 0
    for t in chosen:
        if await download_anime_torrent(t.id):
            n += 1
    return n


def download_plan(anime_id: int) -> set[int]:
    """这部番『现在补下/自动下会挑中』的种子 id 集合——供详情页标『将下载 / 备用』。

    与 download_pending_for_anime 同一套挑选（锁定源过滤 + 跳过已下集 + 每集一份、负集整组一份），
    只算不下。不在这个集合里的待下种子=备用（同集已被首选/已下覆盖，或非锁定源）。
    """
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None:
            return set()
        pref, kw = a.pref_source, a.pref_keyword
        all_rows = list(s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == anime_id)))
    have_eps = {t.episode for t in all_rows if t.status in ("downloaded", "downloading", "deleted")}  # deleted 也算已处理，不重下
    pending = [t for t in all_rows if t.status in ("pending", "error")]
    if pref:
        pending = [t for t in pending if pref == (t.source or "")]
    if kw:
        pending = [t for t in pending if _kw_match(kw, t.raw_title)]
    return {t.id for t in _select_downloads(pending, pref, have_eps)}


def download_plan_for_ids(anime_ids) -> set[int]:
    """批量版 download_plan：给定一组番 id，返回它们『会真下』的种子 id 并集（供新入库一次性标将下载/备用）。
    与 download_all_pending 同一挑选口径（锁定源过滤 + 跳过已下/在下集 + 每集一份、负集整组一份），只算不下。
    把逐番 N 次查询压成 2 次：一次拿这些番的锁定源、一次拿它们的全部种子；等价于对每个 id 调 download_plan 求并。"""
    ids = {i for i in anime_ids if i}
    if not ids:
        return set()
    with get_session() as s:
        animes = list(s.exec(select(Anime).where(Anime.id.in_(ids))))
        pref_map = {a.id: a.pref_source for a in animes}
        kw_map = {a.id: a.pref_keyword for a in animes}
        rows = list(s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id.in_(ids))))
    by_anime: dict = {}
    have_by_anime: dict = {}
    for t in rows:
        if t.status in ("pending", "error"):
            by_anime.setdefault(t.anime_id, []).append(t)
        elif t.status in ("downloaded", "downloading", "deleted"):   # deleted 也算已处理，不重下
            have_by_anime.setdefault(t.anime_id, set()).add(t.episode)
    plan: set = set()
    for aid, pending in by_anime.items():
        lock = pref_map.get(aid)
        if lock:
            pending = [t for t in pending if lock == (t.source or "")]
        kw = kw_map.get(aid)
        if kw:
            pending = [t for t in pending if _kw_match(kw, t.raw_title)]
        plan |= {t.id for t in _select_downloads(pending, lock, have_by_anime.get(aid))}
    return plan


def pending_breakdown() -> dict:
    """把『待下』(pending)拆成 将下载/备用/待确认/未知，供仪表盘种子状态区看清那一大坨到底是什么。
    · 将下载 = 已确认番·本集首选（download_plan 会挑中，会自动下；含特别篇 -1 的首选）
    · 备用   = 已确认番·同集已有更优版本（不会自动下）；含已拒绝番/孤儿的残留
    · 待确认 = 番还没确认（要点确认才下）
    · 未知   = 批量/未知集(-2)——flush 后台不自动下（即便被 _select_downloads 挑中），需人工在详情页下
    四者之和 = 待下总数。复用批量 download_plan_for_ids，仅几条查询、只在仪表盘打开时算。"""
    with get_session() as s:
        conf = {aid: c for aid, c in s.exec(
            select(Anime.id, Anime.confirmed).where(Anime.rejected.is_not(True)))}
        pend = list(s.exec(select(AnimeTorrent.id, AnimeTorrent.anime_id, AnimeTorrent.episode)
                           .where(AnimeTorrent.status == "pending")))
    plan = download_plan_for_ids({aid for aid, c in conf.items() if c})
    will = backup = unconfirmed = unknown = 0
    for tid, aid, ep in pend:
        c = conf.get(aid)
        if c is None:            # 番已拒绝/孤儿 → 不会自动下
            backup += 1
        elif not c:              # 番未确认
            unconfirmed += 1
        elif ep == -2:           # 批量/未知集：flush 不自动下（即便 plan 挑中），单列，别混进将下载
            unknown += 1
        elif tid in plan:        # 已确认·本集首选（含 -1 特别篇的首选）
            will += 1
        else:                    # 已确认·非首选（同集有更优）
            backup += 1
    return {"will": will, "backup": backup, "unconfirmed": unconfirmed, "unknown": unknown}


def _torrent_rows(*where) -> list[dict]:
    """按条件取 TV 种子并解析番名，供 KPI 卡点开的列表弹窗（未知集/失败等）复用。"""
    with get_session() as s:
        ts = list(s.exec(select(AnimeTorrent).where(*where)
                         .order_by(AnimeTorrent.created_at.desc())))
        ids = {t.anime_id for t in ts if t.anime_id}
        names = ({a.id: (a.display_name or a.title) for a in
                  s.exec(select(Anime).where(Anime.id.in_(ids)))} if ids else {})
    return [{
        "id": t.id,
        "anime_id": t.anime_id,
        "name": names.get(t.anime_id) or (t.anime_title or "?"),
        "raw": t.raw_title or "",
    } for t in ts]


def unknown_episode_rows() -> list[dict]:
    """待下里 episode==-2（批量/无法解析集号，flush 不自动下）的种子，供 KPI『未知集』点开手动处理。"""
    return _torrent_rows(AnimeTorrent.status == "pending", AnimeTorrent.episode == -2)


def failed_rows() -> list[dict]:
    """status∈{error, stalled}（下载失败过 / 长期停滞的异常）的种子，供 KPI『失败』点开查看 / 进详情处理。"""
    return _torrent_rows(AnimeTorrent.status.in_(["error", "stalled"]))


def set_torrent_episode(torrent_id: int, episode: float) -> bool:
    """手动改一条种子的集号——把 -2 未知集 / 误判集号救回正常集，让它进正常下载+去重流程。
    只动未下载的(pending/error)；改完仍是待下，由 flush / 补下本番按新集号处理。返回是否改了。"""
    with get_session() as s:
        t = s.get(AnimeTorrent, torrent_id)
        if t is None or t.status not in ("pending", "error"):
            return False
        t.episode = episode
        s.add(t)
        s.commit()
    return True


def exclude_torrent(torrent_id: int) -> bool:
    """直接排除一条不想要的待下种子：置专门的终态 excluded（不删文件、不碰 qB，只改状态）——
    flush/补下永不再挑、restore 不复活、RSS 再遇到同 hash 也不重收，彻底脱离待下/未知集；可用
    unexclude_torrent 撤销。只动未下载的(pending/error)。返回是否排除了。"""
    with get_session() as s:
        t = s.get(AnimeTorrent, torrent_id)
        if t is None or t.status not in ("pending", "error"):
            return False
        t.status = "excluded"
        s.add(t)
        s.commit()
    return True


def unexclude_torrent(torrent_id: int) -> bool:
    """取消排除：把 excluded 的种子放回 pending，重新参与下载/去重。返回是否放回了。"""
    with get_session() as s:
        t = s.get(AnimeTorrent, torrent_id)
        if t is None or t.status != "excluded":
            return False
        t.status = "pending"
        s.add(t)
        s.commit()
    return True


async def download_all_pending() -> int:
    """补下所有『已订阅且已确认』番剧的待下/失败种子。返回触发数。

    按番各自去重（正集每集一份、负集整组一份），避免多版本/多特别篇被一次全拉。
    """
    with get_session() as s:
        auto = list(s.exec(select(Anime).where(  # noqa: E712
            Anime.confirmed == True, Anime.rejected.is_not(True))))
        pref_map = {a.id: a.pref_source for a in auto}
        kw_map = {a.id: a.pref_keyword for a in auto}
        auto_ids = set(pref_map)
        rows = list(s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id.in_(auto_ids)))) if auto_ids else []
    by_anime: dict = {}
    have_by_anime: dict = {}
    for t in rows:
        if t.status in ("pending", "error"):
            by_anime.setdefault(t.anime_id, []).append(t)
        elif t.status in ("downloaded", "downloading", "deleted"):   # deleted 也算已处理，不重下
            have_by_anime.setdefault(t.anime_id, set()).add(t.episode)
    n = 0
    for aid, pending in by_anime.items():
        lock = pref_map.get(aid)
        if lock:  # 锁定源：只补锁定组
            pending = [t for t in pending if lock == (t.source or "")]
        kw = kw_map.get(aid)
        if kw:  # 版本关键词：再过滤到命中该版本的
            pending = [t for t in pending if _kw_match(kw, t.raw_title)]
        for t in _select_downloads(pending, lock, have_by_anime.get(aid)):
            if await download_anime_torrent(t.id):
                n += 1
    return n


def _norm_name(s: str) -> str:
    """归一化番名做集合比对：去所有空白 + 小写（罗马音大小写不敏感）。"""
    return re.sub(r"\s+", "", (s or "")).lower()


async def backfill_source(anime_id: int, strict: bool = False) -> dict:
    """『补齐该源』/『自动补齐』：去 nyaa/Mikan 按名搜『该源』的种子，把漏收的补进这部番。

    该源 = 锁定源(pref_source)；没锁则取该番最高优先级的源。搜到的按 hash 去重、【季号过滤】（挡 S1/S2 混淆），
    strict=True(自动补齐) 再加【番名近似过滤】挡同名衍生作；新的入库 pending 且把番置 confirmed=False（复用待确认
    审核，不自动下——交给用户点『确认下载』）。返回 {found, kept, ingested, sites}。已忽略(rejected)的番不改订阅态。"""
    from sources.nyaa import NyaaSource, nyaa_search_url
    from sources.mikan import MikanSource, mikan_search_url

    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None:
            return {"found": 0, "kept": 0, "ingested": 0, "sites": [], "error": "番不存在"}
        rows = list(s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == anime_id)))
        pref, quarter = a.pref_source, a.quarter
        names = [n for n in (a.jp_name, a.display_name) if n]     # 搜索名（有序：先 日/中，再罗马音）
        ref_names = {_norm_name(x) for x in (a.jp_name, a.display_name, a.title) if x}  # strict 参考名集

    latest = max(rows, key=lambda t: t.created_at, default=None)
    if latest:
        for n in candidate_names(latest.raw_title):
            if n not in names:
                names.append(n)
    ref_names |= {_norm_name(t.anime_title) for t in rows if t.anime_title}
    # 季号过滤基准：用本番种子【实际解析出的季号】而非 bgm 纠正后的 a.season——否则锁定源的续季番若种子标题
    # 无季标记(解析成 season=1)，会与 a.season=2 全对不上而假阴、补齐永远搜不到。
    existing_seasons = {t.season for t in rows}
    queries = [n for n in names if len(n.replace(" ", "")) >= 2][:4]   # 限 4 个查询，别打太多请求
    if not queries:
        return {"found": 0, "kept": 0, "ingested": 0, "sites": [], "error": "没有可搜索的番名"}

    # 目标源(组名)+站点：锁定源→只补该源；没锁→补最高优先级的源（Mikan 群组同优先级即并列全取）
    tors = [(t.source, t.site, t.priority or 0) for t in rows if t.source]
    if pref:
        targets = [(src, site) for src, site, _ in tors if pref == (src or "")]
    else:
        maxpri = max((pr for _, _, pr in tors), default=0)
        targets = [(src, site) for src, site, pr in tors if pr == maxpri]
    site_groups: dict = {}
    for src, site in targets:
        site_groups.setdefault(site, set()).add(src)
    if not site_groups:
        return {"found": 0, "kept": 0, "ingested": 0, "sites": [], "error": "该番还没有任何来源，无法判断去哪搜"}

    # 抓取（唯一分站处）：按站构造搜索源，复用 Source._parse（含组名白名单/合集过滤/hash 校验）。
    # 各 (site × 查询名) 并发抓，墙钟=最慢一次而非累加，避免最坏 ~8×30s 串行阻塞几分钟。
    async def _fetch_one(site, groups, q):
        try:
            if site == "nyaa":
                src_obj = NyaaSource("补齐", nyaa_search_url(q), subgroups=list(groups), title_filter=[])
            elif site == "mikan":
                src_obj = MikanSource("补齐", mikan_search_url(q), subgroups=list(groups), title_filter=[])
            else:
                return []
            return await src_obj.fetch()
        except Exception as e:
            log.warning("补齐搜索失败 site=%s q=%s: %s", site, q, e)
            return []

    tasks = [_fetch_one(site, groups, q) for site, groups in site_groups.items() for q in queries]
    found: dict = {}
    for items in await asyncio.gather(*tasks):
        for it in items:
            found.setdefault(it.info_hash, it)

    # 过滤：季号一致（挡 S1/S2 混淆）+ strict 番名近似（挡同名衍生作/恶搞）
    kept = []
    for it in found.values():
        if it.season not in existing_seasons:
            continue
        if strict:
            res = {_norm_name(it.anime_title)} | {_norm_name(x) for x in (it.search_names or [])}
            if not (ref_names & res):
                continue
        kept.append(it)

    # 入库：hash 去重、anime_id 直挂、不登 alias、status=pending
    ingested = 0
    to_confirm = False
    with get_session() as s:
        a_now = s.get(Anime, anime_id)   # 重取：await 抓取期间该番可能被合并/删除，别把种子插到悬空 anime_id 上
        if a_now is None:
            return {"found": len(found), "kept": len(kept), "ingested": 0,
                    "sites": list(site_groups), "error": "该番已被合并或删除，补齐取消"}
        for it in kept:
            if s.exec(select(AnimeTorrent).where(AnimeTorrent.info_hash == it.info_hash)).first():
                continue
            s.add(AnimeTorrent(
                info_hash=it.info_hash, anime_id=anime_id, source=it.source, site=it.site,
                anime_title=it.anime_title, raw_title=it.raw_title, season=it.season,
                episode=it.episode, quarter=quarter or it.quarter,
                download_url=it.download_url, release_time=it.release_time,
                priority=it.priority, status="pending"))
            try:
                s.commit()
                ingested += 1
            except IntegrityError:
                s.rollback()
        if ingested and not a_now.rejected:   # 有新货且未忽略(实时态) → 转待确认，复用审核流、别自动下
            a_now.confirmed = False
            s.add(a_now)
            s.commit()
            to_confirm = True
    log.info("补齐 anime=%s strict=%s：搜到 %d（站 %s）→ 留 %d → 入库 %d",
             anime_id, strict, len(found), list(site_groups), len(kept), ingested)
    return {"found": len(found), "kept": len(kept), "ingested": ingested,
            "sites": list(site_groups), "to_confirm": to_confirm}


async def sync_qb_status() -> int:
    """从 qB 同步 TV 种子实时态（剧场版走 movies.sync_qb_status）。"""
    return await engine.sync_qb_status(AnimeTorrent)


def anime_save_path(anime_id: int) -> str | None:
    """该番当前的归档目录（build_save_path 结果：[子目录]/[季度]/番名/[Season N]）；算不出返回 None。

    与 download_anime_torrent 的取值一致：季度用 a.quarter，番名 jp_name→display_name→title。
    """
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None:
            return None
        quarter = a.quarter or "unknown"
        folder = (a.jp_name or a.display_name) or a.title or "unknown"
        season = a.season
    return engine.build_save_path(quarter, folder, season=season,
                                  sub_dir=config.ANIME_DOWN_PATH)


async def relocate_anime(anime_id: int, old_path: str | None = None) -> dict:
    """把该番已下/在下的种子移到当前归档目录（改季度/重绑后调用；调用方应已落新 a.quarter/名/季号）。

    qB 跟踪该种子 → setLocation 原地搬 + 更新 save_path；qB 关/连不上/不跟踪(remove-on-complete)
    → 清完成状态待重下到新目录；setLocation 报 403/409(新目录不可写) → 只报告、不动状态。
    返回 {new_path, old_path, moved, redownload, untracked, failed, fail_code?, error?}。
    """
    new_path = anime_save_path(anime_id)
    rep = {"new_path": new_path, "old_path": old_path, "moved": 0,
           "redownload": 0, "untracked": 0, "failed": 0}
    if new_path is None:
        rep["error"] = "算不出新路径（越界或无番）"
        return rep
    with get_session() as s:
        pairs = [(t.id, t.info_hash) for t in s.exec(select(AnimeTorrent).where(
            AnimeTorrent.anime_id == anime_id,
            AnimeTorrent.status.in_(["downloaded", "downloading"]),
            AnimeTorrent.archived_at.is_(None)))]   # 已归档的不在 qB，setLocation 移不动、别误清成 pending 触发重下
    if not pairs:
        return rep

    def _clear(ids):   # 清完成状态→pending，等 flush 重下到新目录
        with get_session() as s:
            for tid in ids:
                t = s.get(AnimeTorrent, tid)
                if t is not None and t.status in ("downloaded", "downloading"):
                    t.status = "pending"
                    s.add(t)
            s.commit()

    def _mark_moved(ids):
        with get_session() as s:
            for tid in ids:
                t = s.get(AnimeTorrent, tid)
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


async def delete_anime_torrent(torrent_id: int) -> bool:
    """删除单条种子在 qB 里的文件（走 qB 接口），标记为 deleted。详情页按集删用。

    deleted 是用户主动删除的终态：恢复订阅时不会被重新下（区别于集去重落选、可复活的 skipped）。
    若同一 hash 剧场版管线还在用，则只脱手本行、不删 qB/文件，免得毁了对面。
    """
    with get_session() as s:
        t = s.get(AnimeTorrent, torrent_id)
        if t is None or t.status not in ("downloaded", "downloading", "stalled") or t.archived_at is not None:
            return False  # stalled 也允许删；已归档(archived_at)不删——已不在 qB、代删不到文件，须先『重新下载』再删
        h = t.info_hash
    if engine.hash_owned_elsewhere(h, MovieTorrent):
        _set_status(torrent_id, "deleted")  # 剧场版侧还持有同一种子 → 只脱手，不删文件
        return True
    if not await engine.qb.delete([h], delete_files=True):
        return False
    _set_status(torrent_id, "deleted")   # 用户主动删除：终态，恢复订阅时不会被重新下（区别于集去重的 skipped）
    log.info("删除文件（单集）- torrent=%s", torrent_id)
    return True


async def delete_anime_files(anime_id: int) -> int:
    """删除该番在 qB 里的已下/在下种子及其硬盘文件（走 qB 正规接口，非裸删文件系统）。

    显式、独立于『拒绝』的动作，需 UI 二次确认。成功后把这些种子标记为 deleted（终态，恢复订阅不重下）。
    与剧场版共享 hash 的只脱手不删文件。返回处理的种子数；qB 未连上/无已下则返回 0。
    """
    with get_session() as s:
        rows = list(s.exec(select(AnimeTorrent).where(
            AnimeTorrent.anime_id == anime_id,
            AnimeTorrent.status.in_(["downloaded", "downloading", "stalled"]),  # 含停滞异常，一并清
            AnimeTorrent.archived_at.is_(None),  # 已归档的跳过：不在 qB、代删不到文件
        )))
        pairs = [(t.id, t.info_hash) for t in rows]
    if not pairs:
        return 0
    exclusive = [h for _, h in pairs if not engine.hash_owned_elsewhere(h, MovieTorrent)]
    if exclusive and not await engine.qb.delete(exclusive, delete_files=True):
        return 0
    with get_session() as s:
        for tid, _ in pairs:
            t = s.get(AnimeTorrent, tid)
            if t is not None:
                t.status = "deleted"  # 用户主动删除，终态；恢复订阅时不重下（区别集去重的 skipped）
                s.add(t)
        s.commit()
    log.info("删除文件 - anime=%s 共 %d 个种子（独占 %d 个删文件）", anime_id, len(pairs), len(exclusive))
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


def seed_source_groups() -> None:
    """首启种入现有的 ANi(全下) + Mikan(待确认)，保持原行为，也给个可编辑的起点。"""
    with get_session() as s:
        if s.exec(select(SourceGroup)).first() is not None:
            return
        s.add(SourceGroup(name="ANi", site="nyaa", feed=config.ANI_RSS_URL,
                          policy="auto", priority=100, enabled=True))
        s.add(SourceGroup(name="Mikan", site="mikan", feed=config.MIKAN_RSS_URL,
                          policy="review", priority=10, enabled=config.MIKAN_ENABLED,
                          subgroups=",".join(config.MIKAN_SUBGROUPS)))
        s.commit()
