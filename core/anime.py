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

from sqlalchemy.exc import DatabaseError, IntegrityError
from sqlmodel import func, or_, select

import config
from core import engine
from db import get_session
from db.dialect import ALIAS_TITLE_LEN
from db.models import Anime, AnimeTorrent, MovieTorrent, SourceGroup, AnimeAlias
from services import enrich
from services.notify import event as notify_event, state as notify_state
from sources.parse import (extract_episode_abs, kw_match, parse_title, quarter_of,
                           quarter_sort_key,
                           search_query_names, season_from_name)

log = logging.getLogger("autorss")

# 串行化『选集→占位下载』，防止 worker flush 与 UI 补下并发对同一集重复放行
_download_lock = asyncio.Lock()

# 状态词表统一在 engine（两条线共用，见那里的定义与口径说明）。此处把本模块用到的都转出：
# 一是供 pages 沿用 anime.HAVE_STATUSES，二是让本文件内引用风格一致（全用不带前缀的名字）。
TRACKED_STATUSES = engine.TRACKED_STATUSES
HAVE_STATUSES = engine.HAVE_STATUSES
HANDLED_STATUSES = engine.HANDLED_STATUSES
DOWNLOADABLE_STATUSES = engine.DOWNLOADABLE_STATUSES
MANUAL_TERMINAL_STATUSES = engine.MANUAL_TERMINAL_STATUSES


def _anime_path_parts(a, t=None):
    """该番保存路径三要素 (季度, 文件夹名, 季号)——下载入口(带种子行 t)与显示/relocate 入口(anime_save_path)
    统一走它，消除两处回退链不一致（B2）。季度：a.quarter → t.quarter → 'unknown'；名字：bgm 日文/中文名
    → 种子解析名 → a.title → 'unknown'；季号：有 a 用 a.season，否则用 t.season。"""
    quarter = ((a.quarter if a else "") or (t.quarter if t else "")) or "unknown"
    folder = (((a.jp_name or a.display_name) if a else "")
              or (t.anime_title if t else "") or (a.title if a else "") or "unknown")
    season = a.season if a else (t.season if t else 1)
    return quarter, folder, season


def quarter_brief() -> list[dict]:
    """番剧列表页顶部小结：当季 + 上季 的番剧流水线分布 + 种子维度。"""
    cur = quarter_of(datetime.now())
    prev = engine.prev_quarter(cur)
    with get_session() as s:
        # 番数按当季/上季两季拉；种子维度按 (季度,状态) 在库内聚合，都不整表扫、不把种子行拉进内存
        animes = list(s.exec(select(
            Anime.quarter, Anime.confirmed, Anime.rejected, Anime.bangumi_id)
            .where(Anime.quarter.in_([cur, prev]))))
        # 种子按【所属番的季度】归拢，而不是种子行自己的 quarter：
        # Anime.quarter 才是权威（它决定实际保存目录），AnimeTorrent.quarter 只是无番时的路径回退。
        # 手动改季度/重绑 bgm 只改 Anime.quarter，历史种子行仍留旧季度——按种子行归拢会把同一部番
        # 劈到两个季度卡上，旧卡出现『订阅中 0 部』却还挂着『种子 N / 已下 M』的鬼影。
        # join 到 Anime 后与上面的番数同源，两个数字必然自洽（孤儿种子无番可归，本就不该进小结）。
        tcounts = list(s.exec(
            select(Anime.quarter, AnimeTorrent.status, func.count())
            .join(Anime, Anime.id == AnimeTorrent.anime_id)
            .where(Anime.quarter.in_([cur, prev]))
            .group_by(Anime.quarter, AnimeTorrent.status)))
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
            "done": qc.get("sent", 0),
            "pending": sum(qc.get(k, 0) for k in DOWNLOADABLE_STATUSES),
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


def _aired_before(air_date, start) -> bool:
    """开播日是否早于【已解析的】开始日 start。start/开播日为 None → False。批量循环传入解析好的 start，免逐番重解析。"""
    aired = _parse_date(air_date)
    return bool(start and aired and aired < start)


def _aired_before_start(air_date) -> bool:
    """该番开播日是否早于『开始使用日』(config.ANIME_START_DATE)。开始日空/开播日未知 → False(不判超期)。"""
    return _aired_before(air_date, _parse_date(config.ANIME_START_DATE))


# 【离季余量】判"这条种子还可能属于所绑的那一季吗"时留的宽限。26 周＝半年。
# 迟发、补流、BD 补档、字幕组追补都在这个量级内。真库上做过灵敏度扫描：
# 余量取 8/13 周会误伤『金牌得主 第二季』（本季 9 集、种子在首播后 26 周才来，是正常补流），
# 而 26/52/104 周三档的命中集合完全相同（只剩真正绑错的那一部）——26 是这个平台的下沿，
# 不是凑出来的数。
_OFF_SEASON_SLACK_WEEKS = 26


def _episode_cannot_belong(a, item) -> bool:
    """这条种子的集号【不可能】属于 a 所绑的那一季 —— 即 bgm 多半匹配错了季。

    两个条件同时成立才算数，缺一不可：
      ① 集号超出该季总集数 —— 但**这一条单独完全不够**。真库实测：98 部有 bgm 的番里
         有 15 部（15%）满足它，其中 8 部正在正常追番下载。原因是"用全系列绝对编号"
         是字幕组的常规写法（『相反的你和我 第二季』本季 13 集、种子写 14–21），
         绑定本身是对的，集号归一交给 ep_offset 那套机制处理。只按这一条判就会把它们
         全推进『待确认』，等于把自动化关掉。
      ② 种子的发布时间已经远远超出该季的播出窗口（首播日 + 总集数周 + 半年余量）。
         一季播完很久之后不会再有新集 —— 除非我们绑错了季。

    真实案例（本项目实测）：`[百冬练习组&LoliHouse] Re:从零开始的异世界生活 … - 78`
    标题没有季标记 → season=1 → 两个候选名【一致】命中 bgm 140001『第一季』(2016-04-03，26 集)
    → 不是平票、直接绑上 → air_date 2016 早于开始使用日 → 整部番判『超期忽略』，一集不下。
    而正确答案是 633836『第四季 夺还篇』(绝对第 78 集 = 该季第 1 集)。
    这条判据抓的就是"第 78 集 > 26 集，且种子比该季首播晚了 543 周（本季跨度只有 26 周）"。

    【为什么不直接拒绝绑定】绑定本身还有用：元数据、封面、ep_offset 的学习都要它，
    而"绑错了哪一季"人来看一眼就知道。所以只降级成『待确认』等人工，不清空 bangumi_id。
    """
    ep = getattr(item, "episode", None)
    total = a.total_episodes or 0
    if not isinstance(ep, (int, float)) or ep < 1 or total <= 0:
        return False
    if ep <= total:
        return False
    aired = _parse_date(a.air_date)
    rel = getattr(item, "release_time", None)
    if aired is None or rel is None:
        return False          # 判不了就不判（与本项目"宁可不判，绝不误判"的一贯口径一致）
    weeks = (rel.date() - aired).days / 7
    return weeks > total + _OFF_SEASON_SLACK_WEEKS


def binding_looks_wrong(s, a) -> bool:
    """这部番的 bgm 绑定看着不对 —— 从库里【已有的种子】推，不需要手上正好有一条 item。

    两条判据，命中任一即为可疑：
      ① 有种子的集号【不可能属于所绑的那一季】（见 _episode_cannot_belong）；
      ② 所绑条目只有 1 集，却收到了多个不同的正片集号 —— 典型是被绑到了单集特典上。

    `_episode_cannot_belong(a, item)` 只用到 item 的 `episode` 与 `release_time` 两个属性，
    而 AnimeTorrent 行恰好同名，可以直接当 item 传进去。这样同一个判据就能用在
    "手上没有 item、只有一部番"的场合（批量重算超期、以后的巡检/页面提示）。

    【为什么必须有这个形态】"绑错季 → 待确认"的闸原来只装在两个【有 item】的地方
    （建番、自动升确认），而决定『超期忽略』的地方是**三处** —— 第三处
    `apply_start_date_filter()` 是批量重算，它手上一条 item 都没有。
    于是用户在设置页点一次『应用开始使用日过滤』，被降级的番就被打回『超期忽略』，
    修复等于没做。真库实测：anime#100（Re:Zero，绑成 2016 年第一季）点一次就掉回去。
    这是本轮第三次同款广度错误，所以这次把判据做成"给一部番就能算"，而不是再加第三处拷贝。
    """
    if a is None or a.bangumi_id is None:
        return False
    rows = s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == a.id)).all()
    if any(_episode_cannot_belong(a, t) for t in rows):
        return True
    # 【第二条判据：单集条目收到了多集正片】bgm 上一部作品常拆成很多条目——正片、
    # 特别篇、PV、单集特典各占一条。搜名字时特典与正片同名，而 resolve 只按名字投票，
    # 于是整部番可能被绑到那条【只有 1 集】的特典上。
    # 真库实证：anime#96 绑到 664060『AKANE On My Mind〜饅頭こわい』(1 集·类型=其他)，
    # 而它的种子是「朱音落语」第 3~12 集、已下 10 集；正确答案是 576121『落语朱音』(12 集·TV)。
    # 后果不只是名字错：total_episodes=1 让完结判定永远算不对，归档目录名也是特典的名字。
    #
    # 判据要数【不同的集号】而不是种子条数：一部真的单集作品会有多个字幕组/多个版本的种子，
    # 那是正常的。全库实测：命中 1 部（就是 #96），另有 3 部真单集作品不受影响，零误报。
    eps = {t.episode for t in rows
           if isinstance(t.episode, (int, float)) and t.episode >= 1}
    return (a.total_episodes or 0) == 1 and len(eps) > 1


def apply_start_date_filter() -> int:
    """按『开始使用日』重算超期忽略。超期忽略 = (rejected=True, confirmed=False)——人工拒绝必是 confirmed=True，
    故此组合唯一表示超期忽略、可与人工决定区分。可逆、只动该动的番：
    · 超期(开播<开始日) 且 仍待确认(未确认未拒) → 判超期忽略(置 rejected，confirmed 保持 False)，不自动下；
    · 已不超期(改早开始日/未知开播日/关闭) 且 当前是超期忽略 → 释放回待确认(清 rejected)。
    人工确认(confirmed=True)、人工拒绝(rejected 且 confirmed=True) 一律不碰——改日期不会掀翻用户的手动决定。返回变更数。"""
    changed = 0
    start = _parse_date(config.ANIME_START_DATE)   # 循环外解析一次（本次调用内 config 值恒定）
    with get_session() as s:
        for a in s.exec(select(Anime)):
            out = _aired_before(a.air_date, start)
            if out and not a.rejected and not a.confirmed:      # 超期 + 待确认 → 判超期忽略
                # 【绑定看着不对的番不判超期】它的 air_date 来自一个多半绑错了的 bgm 条目
                # （绑错季时往往绑到十年前的初代），拿这个日期去判超期就是拿错误的前提做结论，
                # 而结论是"从主列表消失、永久停更"。留在『待确认』等人工看一眼。
                # 只在真要翻状态时才查种子，避免给每部番都多一次查询。
                if binding_looks_wrong(s, a):
                    continue
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
    start = _parse_date(config.ANIME_START_DATE)   # 解析一次：判空 + 循环内复用
    if not start:
        return 0
    changed = 0
    with get_session() as s:
        for a in s.exec(select(Anime).where(Anime.confirmed == True, Anime.rejected.is_not(True))):  # noqa: E712
            if _aired_before(a.air_date, start):
                # 【第五处写 rejected 的地方，同样要让路】判据与 apply_start_date_filter 逐字相同：
                # 绑定看着不对的番，它的 air_date 来自一个多半绑错了的 bgm 条目，
                # 拿这个日期去判超期就是拿错误的前提做结论，而结论是"从主列表消失、永久停更"。
                if binding_looks_wrong(s, a):
                    continue
                a.confirmed, a.rejected = False, True
                s.add(a); changed += 1
        if changed:
            s.commit()
            log.info("一次性：把 %d 部开始日前的已确认老番转为超期忽略", changed)
    return changed


# 版本关键词判据统一在 sources.parse.kw_match（源组的 title_filter 与单番的 pref_keyword 共用一份，
# 免得两处一个大小写敏感、一个不敏感——用户填 1080p、标题写 1080P 时表现完全不同）。
_kw_match = kw_match


def _apply_bgm(a: Anime, info: dict | None, keep_path: bool = False) -> None:
    """把 enrich 结果写进 TV 番（engine 落库 + 按 bgm 规范名纠正季号）。

    写完还要补一次集号回折：回折的判据要同时有 ep_offset 和 total_episodes，而这两个值
    【谁先到不一定】——种子先带来双编号标题(学到 offset)、bgm 后到(补上 total) 是常见顺序。
    只在"学到 offset 那一刻"折一次的话，这种顺序下判据当时不成立、折不动，
    而此后再也没有第二次机会，原来的混编号问题原样存活（同一集两个键各下一份到同一目录）。
    这里是 total 落库的唯一入口，补一次正好补上另一半。判据幂等，白跑一次只是一条查询。
    """
    old_total = a.total_episodes
    engine.apply_bgm_meta(a, info, keep_path)
    if a.finished_at is not None and a.total_episodes != old_total:
        # 【总集数一变，"1..total 全部到手"这个结论就作废】——分割播出的番被 bgm 并成
        # 24 集、或先前少记了一集，都会走到这里。巡检的 stale 分支本来也会撤销，
        # 但那要等下一个周期；而这中间用户看到的是一个【已经知道不对】的完结徽标，
        # 且开了停订的话新集会被一直挡在门外。与合并两部番时的处理同口径（见 _merge_anime）。
        log.info("总集数 %s → %s，撤销完结标记：%s", old_total, a.total_episodes, display_of(a))
        a.finished_at = None
    # 季号以 bgm 规范名为准：ANi 罗马音标题常写 "Season 3" 本地解析不到而回 1，
    # 而 bgm 规范名带『第三季』，能纠正（名字没季标记则保留本地解析值）。
    sn = season_from_name(a.display_name) or season_from_name(a.jp_name)
    if sn:
        a.season = sn
    if a.ep_offset and a.total_episodes and a.id:
        # a 已在调用方的 session 里；用同一个 session 折，随调用方一起提交
        from sqlmodel import Session
        sess = Session.object_session(a)
        if sess is not None:
            _refold_absolute_episodes(sess, a)


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
            AnimeAlias.title == alias_key(item.anime_title),
            AnimeAlias.season == item.season)).first()
        if alias is not None:
            return alias.anime_id

    # 未命中：富集定身份（尽力而为，拿不到就当独立新番）
    info = await enrich.resolve(item.search_names, item.release_time, item.episode, item.info_hash)

    with get_session() as s:
        alias = s.exec(select(AnimeAlias).where(  # 重入保护：再查一次
            AnimeAlias.title == alias_key(item.anime_title),
            AnimeAlias.season == item.season)).first()
        if alias is not None:
            return alias.anime_id

        bgm_id = info.get("bangumi_id") if info else None
        anime = None
        if bgm_id is not None:
            anime = s.exec(select(Anime).where(Anime.bangumi_id == bgm_id)).first()
        if anime is None:
            # 未匹配到 bgm 的番，即使来自自动源也不自动确认/下载——进『富集失败』等人工绑定
            auto = _is_auto(item.policy) and bgm_id is not None
            anime = Anime(
                title=item.anime_title, season=item.season, quarter=item.quarter,
                confirmed=auto,
            )
            _apply_bgm(anime, info)   # 落 air_date 等 bgm 字段（下面判超期要用）
            # 【匹配到了错误的季 → 留给人工，别让它掉进任何一个自动结论】
            # 这一支必须排在超期判定【前面】并且互斥：绑错季时 air_date 往往是十年前的初代
            # （Re:Zero 绑成 2016 年的第一季），超期判定会二话不说把它打成『超期忽略』——
            # 一部正在更新的番就此静默停更，而界面上它和用户手动拒绝的番长得一模一样。
            # 判据见 _episode_cannot_belong：要"集号超出本季"【且】"发布时间远超本季窗口"，
            # 单看前者会误伤 15% 的正常番（它们只是用了全系列绝对编号）。
            if _episode_cannot_belong(anime, item):
                anime.confirmed, anime.rejected = False, False   # → 『待确认』，等人工看一眼
                log.warning("bgm 可能匹配到了错误的季，留待人工确认：%s（绑到 bgm %s『%s』"
                            "首播 %s 共 %s 集，而本条是第 %s 集、发布于 %s）",
                            item.anime_title, anime.bangumi_id, anime.display_name or anime.title,
                            anime.air_date, anime.total_episodes, item.episode,
                            item.release_time.date() if item.release_time else "?")
            elif _aired_before_start(anime.air_date):
                anime.confirmed, anime.rejected = False, True   # 早于开始使用日 → 超期忽略，不自动下（种子照常入库）
            s.add(anime)
            s.commit()          # Anime 无唯一约束，(title,季) 的去重由 AnimeAlias 负责，此处不会撞约束
            s.refresh(anime)
        # 登记番名对照（并发/竞态下可能已存在则忽略）
        akey = alias_key(item.anime_title)
        if not s.exec(select(AnimeAlias).where(
                AnimeAlias.title == akey, AnimeAlias.season == item.season)).first():
            s.add(AnimeAlias(title=akey, season=item.season, anime_id=anime.id))
            try:
                s.commit()
            except DatabaseError:
                # 捕获 DatabaseError 而不只是 IntegrityError（并发下已存在）：MySQL 侧
                # anime_alias.title 是 VARCHAR(191)，超长番名（畸形标题解析出来的）写进去抛的是
                # DataError。只接 IntegrityError 的话它会一路逃到 poll_once 的 per-item except，
                # 该条目每轮复报一次『处理失败』且永远入不了库（SQLite 上不会，只在 MySQL 上）。
                # 对照登记失败不致命：番已经建好了，下次同名条目再试一次即可。
                s.rollback()
                log.warning("番名对照登记失败（跳过，不影响入库）：%s 第%s季", akey[:60], item.season)
        return anime.id


def alias_key(title: str) -> str:
    """番名对照的键：按 anime_alias.title 的列长截断。**查询侧与插入侧必须都用它。**

    【为什么必须截断，而且两侧口径要一致】MySQL 上这一列是 VARCHAR(191)：
      · 插入侧：超长番名在 STRICT_TRANS_TABLES 下【报错】（DataError），别名根本存不进去；
      · 查询侧：拿一个 250 字符的参数去比 VARCHAR(191) 里的值，【永远不可能相等】。
    两者叠加就是一条静默的死路：每来一条该番的种子 → 对照查不到 → 当成新番建一部 →
    别名又插不进去 → 下一条重复一遍。在真实 MySQL 上实测：4 条种子建出 4 部重复番、0 条别名，
    而日志里只有一行"番名对照登记失败（不影响入库）"，看不出它其实每轮都在增番。
    SQLite 上没有这个上限，但两边【一律截断】才能保证同一个库在迁移前后行为一致。

    191 字符对真实番名绰绰有余（生产库实测最长 39）；会超的都是解析畸形标题得到的垃圾串，
    截断之后它们至少能稳定地对应到同一部番，而不是每条种子各建一部。
    """
    return (title or "")[:ALIAS_TITLE_LEN]


def auto_downloadable_ep(ep) -> bool:
    """这个集号能不能被【自动/批量】下载。用户拍板：特别篇(-1) 与 未知集/疑似批量(-2) 一律不自动下。

    理由是这两类都不是"周更正片序列"上的一集：-1 常有多个字幕组的多个版本（剧场版/OVA/总集编都可能
    落这里）、-2 更是一堆解析失败或整季合集包，自动挑一份下往往下错东西。要它们就到详情页对准那一条
    点『下载』（force 路径不受本判据约束）。

    【四条路径必须共用这一个判据】flush 放行 / instant 即时下 / 『下载该源』『补下全部』的挑选
    / download_plan 的『将下载』标记。历史上 -2 就是在 instant 与 flush 之间漂移过——同一条种子
    因为走哪条路而命运不同，而页面标注取自第三条，于是标着『将下载』的东西永远下不来。
    小数集（11.5 这类插入话）仍算正集：它在周更序列上，>=0 天然覆盖。
    """
    return isinstance(ep, (int, float)) and ep >= 0


def _warn_unknown_episode(it) -> None:
    """集号没解析出来（-2）时报一次。

    只在【真正新入库】的种子上调用，所以一个种子一辈子只有一条。曾放在
    sources/nyaa.py 的 _parse 里——那是解析层，每轮抓取对 feed 里每条都跑一遍、
    且在 hash 去重之前，于是同一个种子每 20 分钟复报一次，直到它滑出 RSS 窗口
    （实测一条剧场版刷了 160+ 条）。-2 本身是设计内的一等状态（models 里就是默认值、
    有『未知集』KPI 可点开处理），日志只需留痕、不该当持续告警。
    """
    if it.episode == -2:
        log.warning("集数解析失败 - %s", it.raw_title)


def _orig_episode(raw_title: str) -> float:
    """这一行【标题里原本写的集号】——归一之前的值。

    parse_title 是纯函数、raw_title 原样入库，所以任何时刻都能复算出来；而 episode 列可能已经被
    折算过、或被用户在详情页手工改过。折算要拿它当【幂等锚】：只动"集号还等于标题原值"的行，
    于是重复调用不会折第二次（bgm 改了 total_episodes 触发再次回折时尤其要紧），
    也不会把用户手工改好的集号又推回去。
    """
    return parse_title(raw_title or "")[3]


def _foldable(a) -> bool:
    """这部番能不能安全地做绝对号→季内号折算。

    设前面各季共 O 集（ep_offset）、本季 T 集（total_episodes）：
      · 绝对编号的取值域是 [O+1, O+T]
      · 季内编号的取值域是 [1, T]
    两者【不相交】当且仅当 O+1 > T，即 **O ≥ T**。只有此时 `ep > T` 这个判据才是完备且无误的：
    每个绝对号都 > T、每个季内号都 ≤ T，一条都不会认错（**前提是 bgm 的 T 准确**；
    T 少记一集时那一集会被误折，这是 bgm 数据质量的残留风险，与判据本身无关）。

    **O < T 时一条都不折**，这是本次修正的核心。此前无条件用 `ep > T` 折，在 O < T 时只折得动
    (T, O+T] 那一段、[O+1, T] 这段折不动，于是【同一个源】的前半季（未折）与后半季（已折）
    双双落在键 [O+1, T] 上——两批内容不同的集撞成同一个去重键，flush 每键只放行一份，
    实测 O=12/T=24 的番会静默漏掉整整 12 集。而完全不折时，季内源自己就覆盖了全季，
    绝对源的行只是多出来的重复份：**漏集是不可逆的损失，多下一份只是占盘**，两害相权取后者。

    也考虑过"按源取证"（某源出现过 ep>T 就判定它整源用绝对编号）：对抗验证实测它会被两种常见
    情形骗过——bgm 少记一集（総集編/第0話没算进 eps），以及字幕组中途接手（库里没有它 ≤O 的行）——
    一旦误判，整源被平移 offset，漏得比现在更多，且没有撤销入口。故不采用。
    """
    return bool(a.ep_offset and a.total_episodes and a.ep_offset >= a.total_episodes)


def ambiguous_range(a) -> tuple | None:
    """该番的【集号歧义段】(O, T]，没有则 None。O=ep_offset、T=total_episodes，仅在 O < T 时存在。

    这一段上两套编号的取值范围重叠：绝对号 O+k 与季内号 O+k 写出来一模一样，却是【不同的两集】
    （绝对号 O+k 其实是本季第 k 集）。所以【集去重不能在这一段按集号生效】——
    实测绝对源先把键 13..24 占住之后，季内源真正的第 13..24 集会被 have_eps 永久挡掉，
    flush 跳过、『补下本番』也恒返回 0，用户看到的还是一句『同集已有更优版本』。
    这一段改成按 (集号, 源) 去重：同一集最多每源一份，宁可多下也不漏（漏集不可逆，多下只占盘）。
    """
    if not (a and a.ep_offset and a.total_episodes and a.ep_offset < a.total_episodes):
        return None
    return (a.ep_offset, a.total_episodes)


def dedup_key(amb, aid, ep, source):
    """集去重键：歧义段带上源，其余按 (番, 集)。amb=ambiguous_range(番) 的结果（None=无歧义段）。

    **六条**去重路径必须共用它，否则同一条种子会因走哪条路而命运不同：
    flush 的 have_eps、_download_candidates、download_anime_torrent 的同集闸、
    download_plan 的标注、以及 _revive_orphaned_skipped 的分组
    以及 restore_anime 的"复活 skipped 兄弟"。
    （后两条以前都按 (番,集) 算、不在名单里——歧义段上另一个源的同号集会把真正的那一集
    掩蔽掉，它的 skipped 兄弟永远不会被复活。两次都是同一种广度错误。）
    """
    if amb and isinstance(ep, (int, float)) and amb[0] < ep <= amb[1]:
        return (aid, ep, source or "")
    return (aid, ep)


def _refold_absolute_episodes(s, a) -> int:
    """把该番已入库的【绝对编号】行折回季内集号；不可折的番则把【旧规则误折过的行】展开回去。
    返回改动行数。

    判据与 _learn_and_normalize_episode 的 ② 完全一致：该番可折（见 _foldable）+ 集号在
    绝对域 (T, O+T] 内。不碰特别篇/未知集。改的只是集号这个【去重键】，文件路径按番建目录、
    与集号无关，故已下载的行一并折算是安全的（而且必须折：不折的话它不在 have_eps 的季内键上，
    同一集还会被再下一份）。
    """
    off = a.ep_offset or 0
    if not _foldable(a):
        # 【存量回滚】O < T（判不出来、现在一条都不折）的番，库里可能还留着【旧规则】折出来的行——
        # 旧规则无条件按 ep > T 折，在 O < T 时只折得动 (T, O+T] 那一段，于是同一个源的前后半季
        # 撞成同一个键、静默漏掉半季。升级后若不展开回去，老库的行为与升级前一模一样。
        # 判据是现成的：折过的行满足『标题原值 == 现集号 + offset』。展开后 orig == ep，天然幂等。
        if not (off and a.total_episodes):
            return 0
        n = 0
        for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == a.id)):
            ep = t.episode
            if isinstance(ep, (int, float)) and ep >= 1 and _orig_episode(t.raw_title) == ep + off:
                t.episode = ep + off
                s.add(t)
                n += 1
        if n:
            log.info("集号展开：%s 把 %d 条【旧规则误折】的行还原成标题原值（本番 O=%d < T=%d，判不出编号体系、不折）",
                     a.title, n, off, a.total_episodes)
        return n
    total = a.total_episodes
    n = 0
    for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == a.id)):
        ep = t.episode
        if not (isinstance(ep, (int, float)) and total < ep <= off + total and ep - off >= 1):
            continue
        if _orig_episode(t.raw_title) != ep:
            continue    # 已经折过 / 被人工改过 —— 幂等锚，见 _orig_episode
        if extract_episode_abs(t.raw_title):
            continue    # 标题自带双编号 → 这个 ep 已经是季内号（同 _learn_and_normalize_episode 的守卫）
        t.episode = ep - off
        s.add(t)
        n += 1
    if n:
        log.info("集号回折：%s 把 %d 条绝对编号种子折回季内集号（offset=%d）", a.title, n, a.ep_offset)
    return n


def _learn_and_normalize_episode(s, a, item) -> float:
    """跨源集号归一：返回这条种子该按哪个集号入库（顺带学习该番的 ep_offset）。

    有的源用【全系列绝对集号】（第二季第 4 集写成 16），有的用【季内集号】（写 04）。集去重键是
    (anime_id, episode)，两种写法会被当成两集各下一份到【同一个目录】——库里真的发生过
    （anime 16「地狱模式 第二季」total_episodes=13，ANi 的 16 与 LoliHouse 的 4 是同一集，
    两条 save_path 完全相同）。而且只要两源继续更新就每周复发，删一次挡不住下周。

    归一只在【有确凿证据】时做：
      · 学：标题写成 '16(88)' 的双编号种子直接给出 offset = 88 - 16（LoliHouse 系常带）。
      · 用：该番的两套编号取值域【互不相交】(见 _foldable，要求 O ≥ T)，且这条落在绝对域
        (T, O+T] 内 → 减去 offset。O < T 时两域重叠、判不出来，就一条都不折（宁可重复，不可漏集）。
    推不出 offset、或 O < T 判不出来，就【什么都不做】，绝不按发布时间瞎配对
    （补番/跳周/合集都会误判成同集而漏下）。
    """
    ep = item.episode
    if a is None or not isinstance(ep, (int, float)) or ep < 1:
        return ep                       # 特别篇/未知集不参与
    # ① 学：双编号种子给出确凿偏移（只在还没学到时写，别被个别错标题改掉已定的值）
    if a.ep_offset is None and item.episode_abs and item.episode_abs > ep:
        a.ep_offset = int(item.episode_abs - ep)
        s.add(a)
        log.info("集号偏移：%s 第%s季 → offset=%d（据 %s）", a.title, a.season, a.ep_offset, item.raw_title[:40])
        # 【学到的这一刻要把存量行也回折】否则归一只对【之后】进来的种子生效：
        # 已经以绝对编号入库的那些行（16）与新折算出的季内号（4）在库里并存，
        # 而集去重键是 (anime_id, episode) → flush 认为是两集，各放行一份到【完全相同】的目录。
        # 判据与下面 ② 逐字相同，故天然幂等；只在首次学到 offset 时跑一次，成本可忽略。
        _refold_absolute_episodes(s, a)
    # ② 用：本条落在【绝对域】(T, O+T] 内 → 折回季内集号。
    # 上界 O+T 也是判据的一部分：合法的绝对号不可能超过它，卡住它就顺手挡掉把分辨率/年份/
    # 三位话数误解析成集号的垃圾值（'- 1080'、[201] 之类），免得被减去 offset 后变成一个像样的集号。
    if item.episode_abs:
        return ep     # 标题自己写了『季内(绝对)』两个号（如 '13(25)'），ep 就是季内号，再折就错了
    if _foldable(a) and a.total_episodes < ep <= a.ep_offset + a.total_episodes and ep - a.ep_offset >= 1:
        return ep - a.ep_offset
    return ep


def existing_hashes(hashes) -> set[str]:
    """这批 info_hash 里哪些库里已经有了。一次 IN 查询，供 poll_once 批量预取。

    每条 RSS 条目各开一个 session 查一次的代价并不小：100 条实测 42~45ms（本地 SQLite），
    切到远程 MySQL 时是每轮 0.4~2 秒的裸阻塞（建连接 + 往返 ×100）。而 RSS 一轮的条目
    绝大多数是【上一轮就见过的】，批量预取等于把这 100 次往返压成 1 次。
    """
    hs = [h for h in hashes if h]
    if not hs:
        return set()
    with get_session() as s:
        return set(s.exec(select(AnimeTorrent.info_hash).where(AnimeTorrent.info_hash.in_(hs))))


async def process_item(item, known_hashes: set | None = None,
                       qb_alive: bool | None = None) -> bool:
    """处理一条标准条目。返回 True 表示是新种子（之前没见过）。

    known_hashes：调用方批量预取的"库里已有的 hash"集合（见 existing_hashes）。
    传了就用它做去重、不再单独查库；不传则照旧自己查一次（手动补齐等零散入口走这条）。

    qb_alive：本轮开头【只探一次】的 qB 可达性（poll_once 传进来）。
    【为什么不在这里各探各的】"最高优先级即时下载"默认开着，而种入的 ANi 组 priority=100，
    于是几乎每条新种子都走那条路、一条都不走 flush。qB 没开机时，download_anime_torrent
    会先真去源站把种子取回来（最长 180 秒），才发现 qB 连不上——一轮 7 条新条目 = 7 次无用 GET。
    可原样把 qb_precheck 搬进来也不对：qB 在线时每个新条目都要多打一次 GET /app/version
    （实测 1 → 8 次/轮），拿常态的开销换罕态的浪费，净收益为负。
    每轮探一次、传下来，两头都不亏。None＝调用方没探（零散入口），按老路走不额外拦。
    """
    # 1) 种子级去重：同一 hash 见过就跳过（跨源相等）
    if known_hashes is not None:
        if item.info_hash in known_hashes:
            return False
    else:
        with get_session() as s:
            if s.exec(select(AnimeTorrent).where(
                    AnimeTorrent.info_hash == item.info_hash)).first() is not None:
                return False

    # 2) 定位到唯一的番（对照命中不查 bgm；未命中查一次）
    anime_id = await _resolve_anime(item)

    # 3) 入库种子（带 anime_id）。一般不在这里下：交给 flush_ready_downloads。
    with get_session() as s:
        a = s.get(Anime, anime_id)
        episode = _learn_and_normalize_episode(s, a, item)   # 跨源集号归一（绝对编号 → 季内编号）
        torrent = AnimeTorrent(
            info_hash=item.info_hash,
            anime_id=anime_id,
            source=item.source,
            site=item.site,
            anime_title=item.anime_title,
            raw_title=item.raw_title,
            season=item.season,
            episode=episode,
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
        #
        # 【限定"从没下过任何一集的番"】——它要救的两种情形都还没交付过东西；
        # 而『补齐(backfill)』和『手动绑定 bgm』也用 confirmed=False 表达"这批要你人工审"，
        # 对这些番不能自作主张改回去，否则：
        #   · 番未超期 → 被改回 confirmed=True，补齐进来的待审种子下一轮就被自动下走，
        #     详情页那句"后台将暂停自动下新集，直到你去待确认页重新点确认"当场失效；
        #   · 番超期(早于开始使用日，续季/老番补齐正是这种) → 走下面 rejected=True 分支被判
        #     『超期忽略』，从主列表消失、永久停更，而它压根不在弹窗指引的『待确认』页，用户无从发现。
        # 判据用"有没有 HANDLED 状态的种子"：补齐/重绑的番按定义已下过，待救的两种情形则没有。
        # 【绑错季的番不许被自动升确认】_episode_cannot_belong 原来只在 _resolve_anime 的
        # "新建番"分支里判过一次，而这条升确认分支的入口条件与被降级的番完全吻合：
        # 待确认(0,0) + 有 bangumi_id + auto 源 + 一集都没下过 —— 一条新种子进来就把它升回『追番中』。
        #
        # 【作用域，别记错】下面的函数体还有一道 `not _aired_before_start(a.air_date)`，
        # 所以"绑到十年前的老季"那一类（如 Re:Zero 绑成 2016 年第一季）本来就被超期判据挡住了，
        # 不靠这道闸。这道闸真正管的是**另一半**：绑错的季【不早于】开始使用日时
        # （绑到同年另一个 cour、绑到今年的衍生作），超期判据一点忙都帮不上，
        # 没有它就会被静默升成『追番中』并开始自动下。判据与建番时那次逐字相同，不引入第二套口径。
        if (a is not None and not a.confirmed and not a.rejected
                and a.bangumi_id is not None and _is_auto(item.policy)
                and not binding_looks_wrong(s, a)
                and not s.exec(select(AnimeTorrent).where(
                    AnimeTorrent.anime_id == anime_id,
                    AnimeTorrent.status.in_(HANDLED_STATUSES),
                    AnimeTorrent.id != torrent_id)).first()):
            # 超期的【不在这里替用户做忽略决定】，只是不自动确认、留在『待确认』等人工。
            # 原来这里写 a.rejected=True，会咬到一类用户刚刚亲手操作过的番：
            # 『待识别』番按定义一集都没下过（没有 bangumi_id → 从不自动确认 → 从不下载），
            # 于是上面那道"有没有 HANDLED 种子"的守卫对它恒不成立。用户在详情页点『重新识别』或
            # 『绑定 bgm』拿回 bangumi_id（UI 明说"回到待确认，去点确认下载"）后，只要该番
            # air_date 早于开始使用日（长番/续季必然如此），下一条 auto 源新种子进来就会走到这里
            # 把它判成超期忽略：番当场从『待确认』页消失、掉进『已忽略』、永久不再自动下，
            # 而用户按指引回到『待确认』时那里是空的，只会以为"绑完就再没更新过"。
            # 新入库的番仍在 _resolve_anime 建番处按开始使用日判超期（见上面 auto 分支），这条没丢；
            # 对【已有】的番重算超期本来就有设置页那个带二次确认的显式入口，不该由后台悄悄代劳。
            if not _aired_before_start(a.air_date):
                a.confirmed = True
                s.add(a)
                s.commit()
        should_download = is_subscribed(a)
        lock = a.pref_source if a else None   # 锁定源：入库即下也只放行锁定组
        kw = a.pref_keyword if a else None     # 版本关键词：即时下载也只放行命中该版本的

    log.info("新增 - %s - %s 第%s季 第%s集", item.source, item.anime_title, item.season, episode)
    _warn_unknown_episode(item)
    # 最高优先级即时下载：开关开 + 自动下的番 + 来自最高优先级组 + (未锁源或正是锁定源) → 入库就下，不等缓冲窗口。
    # 只放行正集（见 auto_downloadable_ep）：与 flush 同一判据，别让一条种子是否被下取决于它走了哪条路。
    if (config.ANIME_TOP_PRIORITY_INSTANT and should_download
            and qb_alive is not False          # 本轮开头探过且 qB 不可达 → 别白取一次种
            and auto_downloadable_ep(item.episode)
            and (item.priority or 0) >= _top_priority()
            and (not lock or lock == (item.source or ""))
            and (not kw or _kw_match(kw, item.raw_title))):
        await download_anime_torrent(torrent_id)
    return True


async def download_anime_torrent(torrent_id: int, force: bool = False) -> bool | None:
    """取种子文件并加入 qBittorrent。

    【三态返回】True=已交付 / False=**这一条自己**的毛病（坏种子、源站给不出、被 qB 明确拒）
    / None=**系统性**失败（qB 连不上、保存路径算不出来），与这一条种子无关。

    三态而不是两态，是因为调用方里有"同一集逐条试候选、试到成功为止"的循环
    （见 _download_candidates）：把两类失败揉成一个 False，那个循环就会
      · qB 抖动时拿同一集的【第二个 hash】再发一次 —— 两份可能都进了 qB，其中一条还留在 pending、
        UI 上永远看不见；
      · 遇到共因失败（下载目录配错/磁盘满）时把该集【全部】候选一次烧成 error，
        而 flush 只挑 pending —— 这一集从此再不会被自动放行，且没有任何提示。
    收到 None 的循环应当【当场收手】，本轮不再试同集的其它候选。

    【并发下不会对同一集放行两份】worker flush 与 UI 补下可能同时挑中同一集的不同源。
    真正扛住这件事的是下面那句『原子占位』：它在【任何 await 之前】就把状态落库成 downloading，
    而 downloading ∈ TRACKED_STATUSES，后到的协程一进来就被上面的幂等短路挡掉。
    单线程事件循环 + 该段内无 await ⇒ 这段本身就是原子的。
    （原注释说"tests/test_invariants.py 全仓核过"——**那个文件不存在**，全仓只有这一条引用。这类"引用一个不存在的用例来给自己背书"的注释比没有注释更糟。）
    _download_lock 因此是【冗余的保险】：实测去掉它并发测试仍全绿，而去掉原子占位则当场重复下载
    留着它是为了将来万一有人往这段里加 await。（原注释说"tests/test_concurrency.py 用变异测试量过"——**那个文件同样不存在**。）
    force=True：强制下这一条（无视当前状态、跳过集去重），用于详情页手动指定下载。
    """
    if not config.QB_ENABLED:
        return False  # 无 qB 模式：只采集元数据，不发送种子（保持 pending）

    async with _download_lock:
        with get_session() as s:
            t = s.get(AnimeTorrent, torrent_id)
            if t is None:
                return False
            if t.status in engine.TRACKED_STATUSES and not (force and t.archived_at is not None):
                return False  # 已在下/已下：幂等短路（force 也不例外）。例外：已归档的可 force 重新下（重新交回 qB）
            if not force and t.status not in DOWNLOADABLE_STATUSES:
                return False  # 非 force：只放行 pending/error；skipped/deleted 需 force 才强制下
            anime_id = t.anime_id
            episode = t.episode
            season = t.season
            title = t.anime_title
            orig_status = t.status   # 供失败恢复：force 从终态(deleted/excluded)重下若失败，别降级成会触发复活/重下的 error
            orig_archived = t.archived_at   # 同上：force 重下【已归档】的集若失败，要把归档标记原样放回去
            # 进锁时下面会把这四个 qB 实时态清零（好让它作为"全新在下"重新跟踪）。
            # 失败要恢复原状态时必须连它们一起放回：只还状态不还进度，归档/停滞行会重新满足
            # in-flight（TRACKED 且 progress<1）→ sync 查不到 → 两轮后判 error →
            # 盘上的文件脱离 HAVE_STATUSES，UI 再没有入口能删它，同集还会被自动重下一份。
            orig_qb = (t.qb_progress, t.qb_state, t.qb_synced_at, t.qb_progress_at)
            # 跨表【不】去重：番剧/剧场版各下到各自目录（用户要各归各、重复提交也接受）。qB 按 hash 物理去重、
            # 不会真下两遍；某侧删文件后另一侧由 sync 落 error——不再造 progress=1 的幽灵 pointer（曾致删/下竞态静默丢文件）。
            # 同集去重：同一 (anime_id, 集) 已有别的种子在下/已下 → 跳过（force 时不去重，强制下这条）。
            # 注：deleted 不进去重集——用户删的是"那一条种子"，同集来了新 hash 允许照常自动下（deleted 本身
            # 状态非 pending、flush 不会自动选它，同 hash 也在入库处去重，故被删的那条不会自动回来；force 例外）。
            # 只对【正集】去重：特别篇(-1)/未知集(-2) 现在只可能由 force 路径进来（自动/批量四条路径
            # 都被 auto_downloadable_ep 挡在外面），而 force 本就不走这道闸——同一部番的多个特别篇、
            # 多个批量包彼此不是同一集，逐条点就该逐条下。
            if not force and auto_downloadable_ep(episode) and anime_id:
                # 歧义段((O,T]，见 ambiguous_range)按 (集号,源) 去重：那一段同键的两行不一定是同一集，
                # 只按集号挡会把真·另一集永久压成 skipped。口径必须与 flush/补下逐字一致。
                _amb = ambiguous_range(s.get(Anime, anime_id))
                _same_src = [AnimeTorrent.source == (t.source or "")] if (
                    _amb and _amb[0] < episode <= _amb[1]) else []
                dup = s.exec(select(AnimeTorrent).where(
                    AnimeTorrent.anime_id == anime_id,
                    AnimeTorrent.episode == episode,
                    AnimeTorrent.status.in_(HAVE_STATUSES),   # 含 stalled：instant 路径不经 flush 的 have_eps，
                    AnimeTorrent.id != torrent_id,           # 这里是它唯一的集去重闸，口径必须与 flush 一致
                    *_same_src,
                )).first()
                if dup is not None:
                    t.status = "skipped"
                    s.add(t)
                    s.commit()
                    log.info("跳过重复集 - %s 第%s季 第%s集（已有一份在下/已下）", title, season, episode)
                    return False
            t.status = "downloading"  # 原子占位：先落库再 await，后到的协程一进来就被短路挡掉
            # 重新下：清归档标记 + 重置 qB 实时态，让它作为『全新在下』被重新跟踪、从新完成点重算归档倒计时——
            # 否则重下已归档的种子会带着旧 qb_progress=1/旧完成时间，被下一轮完成归档立刻再归档掉。
            t.archived_at = None
            t.qb_progress, t.qb_state, t.qb_synced_at, t.qb_progress_at = 0.0, "", None, None
            s.add(t)
            s.commit()
            url = t.download_url
            info_hash = t.info_hash
            a = s.get(Anime, anime_id) if anime_id else None
            # 季度/番名/季号统一由 _anime_path_parts 算（与 anime_save_path 同口径，B2）：季度/季号以 bgm 纠正后的
            # Anime 为准（种子行是入库快照、重识别后会过时；有下载时 keep_path 已锁死 a.quarter 与名字）；名字优先 bgm 日文原名。
            quarter, folder_name, season = _anime_path_parts(a, t)

    # 失败落定态：从用户终态(deleted/excluded) force 重下若失败，恢复原终态而非 error——
    # 否则 deleted→error 会让该集不再含任何 _HANDLED 状态，被 _revive_orphaned_skipped 复活 skipped 兄弟、
    # flush 自动把用户删过的集重新下回来（违反 deleted『不重下』）。pending/error/skipped/stalled 仍落 error。
    # 已归档的集 force 重下失败时也恢复原态（连同下面的 archived_at 一起还原）：
    # 归档＝文件还在盘上、只是从 qB 移除了。取种/交付失败什么都没改变，它仍是「已归档·已下」。
    # 若像以前那样降级成 error：① 该集脱离 HAVE_STATUSES → flush 当场挑同集另一个 pending 源，
    # 一个轮询周期内把第二份下到同一目录；② 详情页删除按钮、delete_anime_files 全按 HAVE 门控，
    # 于是那份归档旧文件在 UI 里再也没有任何入口能标记或删除，成了永久孤儿。
    # 【停滞也要保留原状态】停滞＝半成品文件在盘上、已脱离轮询、明确留给人工处理。
    # 人工点『下载』抢救、取种却暂时性失败时，若把它降级成 error/pending，就同时丢掉了：
    # ① 停滞标记（用户再也找不到这条异常）；② HAVE 身份 —— 于是 flush 当场为这一集换源下第二份，
    # 而半成品文件还在盘上。原状态本身就是最准确的落点，失败什么也没改变。
    _keep_orig = (orig_status in MANUAL_TERMINAL_STATUSES or orig_status == "stalled"
                  or orig_archived is not None)
    fail_status = orig_status if _keep_orig else "error"
    # 【qB 连不上】时的落点：pending 而不是 error。error 不会被 flush 自动重试（见那里注释），
    # qB 一次重启就能把这一轮放行的几十集全打成 error、等人工补下。种子本身没毛病，留在待下，
    # 下轮 flush 自然重发。从终态 force 重下的仍恢复原终态（同 fail_status，别把用户的处理抹掉）。
    defer_status = orig_status if _keep_orig else "pending"

    def _fail(status: str = "", reason: str = "") -> None:
        """失败回写：恢复状态，并把 in-lock 清掉的归档标记与进度一起放回去。

        【qb_progress 必须一起还原】进锁时为了"作为全新在下重新跟踪"把 qb_progress 清成了 0。
        归档行本是 sent+progress=1 的已完成态，若失败后只还原 status/archived_at 而进度留在 0，
        这行就重新满足 _inflight_where（TRACKED 且 progress<1）→ 被 sync 当成在下的种子去问 qB，
        而它早已从 qB 移除 → 两轮宽限后落 error：盘上那份归档文件脱离 HAVE_STATUSES，
        UI 上再没有任何入口能删它，同时集去重闸失效、同集会被自动重下一份到同一目录。
        """
        st = status or fail_status
        _set_status(torrent_id, st)
        with get_session() as s2:
            t2 = s2.get(AnimeTorrent, torrent_id)
            if t2 is not None and t2.status == st:
                t2.fail_reason = reason[:300]
                if _keep_orig:      # 恢复原状态的同时，把进锁时清掉的 qB 实时态整组放回
                    t2.archived_at = orig_archived
                    (t2.qb_progress, t2.qb_state,
                     t2.qb_synced_at, t2.qb_progress_at) = orig_qb
                s2.add(t2)
                s2.commit()

    def _retry(reason: str) -> None:
        """暂时性失败 → 排进重试队列：状态回 pending，等 retry_at 到点由 flush 自动重发。
        退避表用满就落 error 留人工。从终态(deleted/excluded/已归档) force 重下的不进队列——
        恢复原终态即可，否则用户特意删过的集会被自动重下回来。"""
        # 【只有 flush 真会重发的行才进队列】否则 UI 会挂出"某时刻后自动重发"，而后台永远不动它：
        # · 终态(deleted/excluded/已归档) force 重下 —— 恢复原终态，不该被自动下回来
        # · 特别篇(-1)/未知集(-2) —— flush 明确不放行它们（见 auto_downloadable_ep），排队等于骗人。
        #   落 error 反而对：详情页会显示失败 + 真实原因，人工对准那条点『下载』即可重试。
        if _keep_orig or not auto_downloadable_ep(episode):
            _fail(reason=reason)
            return
        with get_session() as s2:
            t2 = s2.get(AnimeTorrent, torrent_id)
            if t2 is None:
                return
            nxt = engine.next_retry_at(t2.retry_count)
            if nxt is None:
                t2.status, t2.retry_at = "error", None
                log.error("重试 %d 次仍失败，落失败等人工 - %s：%s", t2.retry_count, title, reason)
                # 【失败通知不在这里发】_fail 是同步嵌套函数，await 不了；更重要的是这条路径是
                # 交付主链路，不该为了一条旁枝通知增加任何风险。失败/停滞由 sweep_alerts 统一报，
                # 那里还能顺带合并（批量补下一次落好几条 error，逐条推送本身就是噪声）。
            else:
                t2.status, t2.retry_at = "pending", nxt
                t2.retry_count += 1
                log.warning("暂时性失败，第 %d 次重试排在 %s - %s：%s",
                            t2.retry_count, nxt.strftime("%H:%M"), title, reason)
            t2.fail_reason = reason[:300]
            s2.add(t2)
            s2.commit()

    # 组装保存路径（含越界校验），TV 按设置可加 Season N 子目录
    save_path = engine.build_save_path(quarter, folder_name, season=season,
                                       sub_dir=config.ANIME_DOWN_PATH)
    if save_path is None:
        log.error("拒绝越界保存路径 - %s -> %s / %s", title, quarter, folder_name)
        _fail(reason=("未配置下载目录，请到设置页填『工作目录』"
                      if not (config.DOWN_PATH or config.ANIME_DOWN_PATH)
                      else "拒绝越界保存路径（检查下载目录设置）"))   # 配置问题，重试无意义
        # None 而不是 False：路径算不出来是【下载目录配置】的毛病，同一集换个源照样算不出来。
        # 返回 False 会让调用方的候选循环把该集剩下的候选全部烧成 error。
        return None
    stage = "取种"
    try:
        data = await engine.fetch_torrent_bytes(url)
        stage = "交付"
        # 分类固定不带后缀，标签只放季度（qB 里按分类归大类、按标签筛季度）
        ok = await engine.add_to_qb(data, save_path, "AutoRSS-Anime", quarter, info_hash=info_hash)
    except asyncio.CancelledError:
        # 被取消（关停等）→ 复位，别永久卡 downloading。走 _retry 而不是写死 pending：
        # 从 deleted/excluded 强制重下时若正好被取消，写 pending 会让该集不再含任何"已处理"状态，
        # 于是被 _revive_orphaned_skipped 复活 + flush 自动重下——用户删掉的集又被下回来。
        # 取种最长 180s，这期间关程序就会中招，窗口并不小；下次启动重发一次即可，不该留个 error。
        _retry(f"关停中断（{stage}中）")
        raise
    except Exception as e:
        if stage == "取种":     # 源站超时/502/DNS…——与种子本身无关，排进重试队列
            _retry(f"取种失败：{e}")
        else:                   # 交付阶段的意外异常：不在约定的重试范围内，落失败留人工看
            log.error("下载失败 - %s - %s", title, e)
            _fail(reason=f"交付异常：{e}")
        return False

    if ok is None:             # qB 连不上：留在待下，下轮 flush 自动重发（别记 error 要人工）
        log.warning("qB 连不上，本集留待下轮重发 - %s 第%s季 第%s集", title, season, episode)
        _fail(defer_status, reason="qB 连不上，等它回来自动重发")
        return None            # 系统性：调用方别拿同集的另一个 hash 再发一次（两份都可能进 qB）
    if not ok:
        # qB 明确拒了这一条。成因里既有"这条自己的毛病"（种子无效），也有共因（路径不可写/磁盘满），
        # 但 qB 不告诉我们是哪种，只能按可换源处理——换源若也拒，各自记各自的 error，语义仍是对的。
        _fail(reason="qB 未接受（种子无效 / 保存路径不可写 / 磁盘满）")
        return False
    with get_session() as s:   # 记实际保存路径：改季度/重绑后据此移动或提醒旧位置
        t = s.get(AnimeTorrent, torrent_id)
        if t is not None:
            t.save_path = save_path
            # 交付成功 → 清空重试痕迹，下次再失败从头退避
            t.retry_count, t.retry_at, t.fail_reason = 0, None, ""
            s.add(t)
            s.commit()
    if config.QB_SYNC_STATUS:
        _set_status(torrent_id, "sent")
        engine.qb_kick.set()   # 唤醒 qB 同步循环，立即开始跟这个新交付的种子
    else:
        engine.settle_sent(AnimeTorrent, torrent_id)  # 关跟踪：发送即已下，落定 qb_progress=1、脱离 in-flight
    log.info("已加入qB - %s 第%s季 第%s集", title, season, episode)
    await notify_event("delivered", f"{title}[{episode}]")
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
            *subscribed_where())))
        if not auto_ids:
            return
        # 只取判断要用的四列：这一段几乎是全表扫（error+skipped+已处理的全部状态），
        # 整行 ORM 装配的开销随 sent 行数线性增长，而下面只用到 (id, 番, 集, 状态)。
        # 真正要改写的那几行在下面按 id 单独取（通常只有个位数）。
        rows = list(s.exec(select(AnimeTorrent.id, AnimeTorrent.anime_id,
                                  AnimeTorrent.episode, AnimeTorrent.status,
                                  AnimeTorrent.source).where(
            AnimeTorrent.anime_id.in_(auto_ids),
            AnimeTorrent.status.in_(("error", "skipped") + HANDLED_STATUSES))))
        # 【分组键必须与 flush 同口径：dedup_key，不是 (番, 集)】歧义段（O<T 的番在 (O,T] 上
        # 两套编号重叠）里，同一个集号在不同源下指的是【不同的集】——flush 正因如此才按
        # (番,集,源) 去重。这里按 (番,集) 分组的话，B 源那条已 sent 的"第 5 集"会把
        # A 源真正失败的"第 5 集"一起算进同一组，组里出现 HANDLED ⇒ 判定"该集已有着落" ⇒
        # A 源的 skipped 兄弟永远不会被复活，那一集永久卡死在唯一失败源上。
        amb_map = {a.id: ambiguous_range(a) for a in s.exec(
            select(Anime).where(Anime.id.in_(auto_ids)))}
        by_ep: dict = {}
        for _tid, aid, ep, st, src in rows:
            by_ep.setdefault(dedup_key(amb_map.get(aid), aid, ep, src), set()).add(st)
        # 目标集：有 error，且无 sent/downloading/deleted/stalled（首选已败、该集尚无可用/已删/停滞的下载）。
        # stalled 也算『已处理』→ 不复活兄弟源：停滞的那条留人工处理，不自动换源（与 flush 阻断口径一致）。
        revive = {k for k, sts in by_ep.items()
                  if "error" in sts and not (set(HANDLED_STATUSES) & sts)}
        if not revive:
            return
        ids = [tid for tid, aid, ep, st, src in rows
               if st == "skipped" and dedup_key(amb_map.get(aid), aid, ep, src) in revive]
        changed = 0
        for tid in ids:                    # 只把真要改的那几行取成 ORM 实例
            t = s.get(AnimeTorrent, tid)
            if t is not None and t.status == "skipped":
                t.status = "pending"
                s.add(t)
                changed += 1
        if changed:
            s.commit()
            log.info("换源兜底：复活 %d 个被去重的 skipped 兄弟（该集首选源已失败）", changed)


async def qb_precheck() -> bool:
    """交付前探一次 qB：连得上返回 True。qB 未启用时恒 True（那是"不下载"，不是"故障"）。

    【三处交付入口共用它】flush / 逐番补下 / 批量补下。一个 GET 的代价，换掉"整轮几十集
    逐个去源站取种、再逐个发给一个根本不在的 qB"。

    顺带发状态型通知：**只在"连上↔连不上"翻转的那一刻**各发一条。电平触发的话，
    qB 关机一夜就是几十条一模一样的推送（这条预检每轮都跑）。
    """
    if not config.QB_ENABLED:
        return True
    alive = await engine.qb.reachable()
    await notify_state("qb_down", not alive,
                       "qB 连不上，暂停放行下载（种子留在待下）", "qB 恢复，继续交付")
    if not alive:
        log.warning("qB 连不上，本轮不放行下载（种子留在待下，qB 恢复后自动继续）")
    return alive


async def flush_ready_downloads(qb_alive: bool | None = None) -> int:
    """缓冲窗口 + 严格优先级：每轮跑一次。

    对『自动下载且已确认』的番，把待下种子按 (anime_id, 集) 归组——因为按番的真实身份
    分组，不同组不同写法的同一集会算作同一集，天然只留一份。每集首次被发现后满
    config.ANIME_DOWNLOAD_GRACE_MIN 分钟才放行，到点从该集所有种子挑优先级最高的下一份（错误的排后，
    留作降级）。特别篇(-1)/未知集(-2) 一律不放行（见 auto_downloadable_ep）。返回实际触发下载的数量。
    """
    # 交付前先探一次 qB：一个 GET 的代价，换掉"整轮几十集逐个去 nyaa 取种、再逐个发给一个根本
    # 不在的 qB"。qB 掉线时本轮一行不动，种子留在待下，等它回来自然继续。
    # （中途才挂掉的由 download_anime_torrent 的 defer_status 兜住，两处配合才没有重试风暴：
    #   掉线期间每轮只多花这一个 GET。）
    # qb_alive 由 poll_once 在本轮开头探过一次并传下来（见 process_item 的说明）——
    # 复用它，别在同一轮里对同一个 qB 探第二次。零散入口不传，照旧自己探。
    if not (qb_alive if qb_alive is not None else await qb_precheck()):
        return 0
    _revive_orphaned_skipped()   # 先把『首选源已失败、该集无其它下载』的 skipped 兄弟放回 pending，本轮即可换源
    grace = timedelta(minutes=max(0, config.ANIME_DOWNLOAD_GRACE_MIN))  # 负值会使门槛永假、废掉多源补齐，钳到 0
    now = datetime.now()
    chosen: list[int] = []
    with get_session() as s:
        auto = list(s.exec(
            select(Anime).where(*subscribed_where())
        ))
        auto_ids = {a.id for a in auto}
        pref_map = {a.id: a.pref_source for a in auto if a.pref_source}
        kw_map = {a.id: a.pref_keyword for a in auto if a.pref_keyword}
        if not auto_ids:
            return 0
        # 『该集已有一份』阻断自动换源的集，统一用 HAVE_STATUSES（sent/downloading/stalled）：
        # 含 downloading（在下的交付，不必再挑同集别的）+ stalled（停滞异常，留人工、不自动换源）。
        # 不含 deleted——删的那条不自动回来，但同集来新 hash 仍允许自动下（非整集拉黑）。
        # 【只取两列】这里只要 (番, 集) 这个去重键。整行 ORM 装配是本函数最贵的一步，而 sent 是只增
        # 不减的终态、行数随挂机线性增长：仓库自测 9700 行时光装配就 150ms（13 条 SQL 本身才 7ms），
        # 这期间事件循环整个停摆（页面/qB 同步/剧场版扫描一起卡）。列投影后同样的活儿只剩几毫秒。
        # 歧义段（O<T 的番在 (O,T] 上两套编号重叠）按 (番,集,源) 去重，其余按 (番,集)——见 dedup_key。
        amb_map = {a.id: ambiguous_range(a) for a in auto}
        have_eps = {dedup_key(amb_map.get(aid), aid, ep, src)
                    for aid, ep, src in s.exec(select(
                        AnimeTorrent.anime_id, AnimeTorrent.episode, AnimeTorrent.source).where(
                        AnimeTorrent.status.in_(HAVE_STATUSES))).all()}
        groups: dict = {}
        # 只自动放行 pending：error 不在这里无限重试（高优先级失败→本组还有 pending 低优先级自然降级；
        # 全 error 则本轮不重试，留给人工补下）。
        # retry_at 未到点的跳过——重试退避全靠这一条落地（人工补下不走这里，不受退避约束）
        for t in s.exec(select(AnimeTorrent).where(
                AnimeTorrent.status == "pending",
                or_(AnimeTorrent.retry_at.is_(None), AnimeTorrent.retry_at <= now))):
            if t.anime_id not in auto_ids:
                continue
            lock = pref_map.get(t.anime_id)
            if lock and lock != (t.source or ""):
                continue  # 锁定源：这部番只收锁定组的种子（硬锁、不兜底）；别的源一律不自动下
            kw = kw_map.get(t.anime_id)
            if kw and not _kw_match(kw, t.raw_title):
                continue  # 版本关键词：只收命中该版本的（硬锁、不兜底）
            if not auto_downloadable_ep(t.episode):
                continue   # 特别篇(-1)/未知集(-2) 不自动下，到详情页对准那条点『下载』（见 auto_downloadable_ep）
            groups.setdefault(dedup_key(amb_map.get(t.anime_id), t.anime_id, t.episode, t.source),
                              []).append(t)

    def _pick(ts, aid):
        # prefer_fresh=True：与标注侧（_download_candidates）、补下侧【同一个口径】。
        # 这里的 status 恒是 pending（上面的 SQL 就这么筛的），所以起作用的只有第二个键
        # "失败过的往后排"——而 pending 里确实有失败过的：暂时性失败会留在 pending 上
        # 排队重试（retry_at + retry_count），详情页把它标成『重试中·第N次』。
        # 不加这个键的话，同集里那条已经失败过 3 次的会因为优先级高而每轮都被再挑一次，
        # 健康的兄弟永远轮不到；而标注侧看到的『将下载』又是另一条——两边分家。
        return engine.pick_best(ts, pref_map.get(aid), prefer_fresh=True)

    for key, ts in groups.items():
        if key in have_eps:
            continue  # 这一集已有一份
        first_seen = min(t.created_at for t in ts)
        if now - first_seen < grace:
            continue  # 缓冲窗口未到，等偏好组
        chosen.append(_pick(ts, key[0]).id)  # key[0] 恒为 anime_id（见 dedup_key）

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
        src_total = {src: c for src, c in s.exec(
            select(AnimeTorrent.source, func.count()).group_by(AnimeTorrent.source))}

    confirmed = [a for a in animes if a.confirmed]
    pending_c = [a for a in animes if not a.confirmed and a.bangumi_id]  # 待确认=已匹配未确认；未匹配的算『富集失败』

    # 各季度总番数（含待确认/待识别/已忽略）——供下面的 3 桶分解用
    total_by_q = Counter((q or "未知") for _, q in all_aq)
    aid_q = {aid: (q or "未知") for aid, q in all_aq}
    # 【按四位年排】季度键的年份只有两位，纯字符串比较下 '99D' > '26C'，
    # 1999 年的番会排到当季前面（仪表盘的季度分布条同样中招，不只是番剧列表）。
    qs = sorted((q for q in total_by_q if q != "未知"), key=quarter_sort_key, reverse=True)
    if "未知" in total_by_q:
        qs.append("未知")

    # 各季度番剧按流水线 3 桶：订阅(已确认)/审核(未确认待处理=待确认+待识别)/忽略(已拒绝)，互斥、和=该季总番数
    nonrej_ids = {a.id for a in animes}
    sub_by_q = Counter((a.quarter or "未知") for a in animes if a.confirmed)
    rev_by_q = Counter((a.quarter or "未知") for a in animes if not a.confirmed)
    ign_by_q = Counter(aid_q[aid] for aid, _ in all_aq if aid not in nonrej_ids)
    by_quarter_state = [(q, sub_by_q.get(q, 0), rev_by_q.get(q, 0), ign_by_q.get(q, 0)) for q in qs]

    # 各来源：种子数 + 已下
    # 只回 (源, 种子数)：第三元『各源已下数』页面从没读过（环图只用前两元），白挂一条 GROUP BY
    by_source = sorted((((src or "?"), cnt) for src, cnt in src_total.items()),
                       key=lambda x: -x[1])

    split = pending_breakdown()   # 待下拆 将下载/备用/待确认/未知，算一次给 KPI 与状态区共用
    return {
        "kpi": {
            "tracking": len(confirmed), "fail": sum(1 for a in animes if not a.bangumi_id),
            "confirm": len(pending_c), "rejected": rejected,
            "done": status.get("sent", 0),
            "torrents": total_torrents,
        },
        # 八种应用侧 status 全列（含 deleted/excluded）：仪表盘『种子数』号称"各状态之和"，
        # 漏列任何一种都会让页面数字对不上——用户用了『已删除/已排除』后尤其明显。
        # 八状态【全集】取自 engine，别再手抄：仪表盘对用户承诺"种子数 = 各状态之和"，
        # 将来加第 9 个状态时漏改这里，页面数字就会静默对不上（且不报错）
        "status": {k: status.get(k, 0) for k in engine.ALL_STATUSES},
        "pending_split": split,
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
    """已拒绝的番（『已忽略』页展示，可恢复）。"""
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
        names = ({a.id: display_of(a) for a in
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
        names = ({a.id: display_of(a) for a in
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


def display_of(a) -> str:
    """番的展示名（中文规范名 → 内部标签）。

    与 pages/layout.name_of 同口径，但 core 层不能 import pages（那会成环，且 core 要能脱离 UI 跑）。
    本文件里原本有三处手抄的 `display_name or title`，一并收到这里。
    """
    return (getattr(a, "display_name", None) or getattr(a, "title", None) or "") if a else ""


# ==================== 完结检测 / 断更提醒（巡检，见 core.worker.run_sweep）====================

def episode_coverage(anime_ids) -> dict:
    """{番 id: 已到手的【正集整数集号】集合}。

    口径【比集去重闸更严】：status ∈ HAVE_STATUSES 且 qb_progress >= 1.0。
    集去重问的是"这一集要不要再下一份"，在下的也算有；而完结问的是"这部番是不是真的下完了"——
    sent 只表示【已交给 qB】，downloading/stalled 更是明摆着没下完。
    qb_progress >= 1.0 是本项目已有的"真下完"信号（归档条件用的就是它），
    且在两种跟踪模式下都成立（关跟踪时 settle_sent 直接写 1.0），不是新发明的口径。
    deleted 不算（用户特意删掉的不该算"已有"，与 restore_anime 同口径）。

    【为什么不用 dedup_key】歧义段的键带上了源，同一个集号会按源拆成多份，拿它计数会虚高、
    把没下完的番判成完结。这里要回答的是"第 k 集到手了没"，就该按集号去重。
    【为什么只取两列 + distinct】sent 是只增不减的终态，跑一年的库里比 pending 多一两个数量级；
    取整行会把整张种子历史装配成 ORM 对象。
    """
    ids = {i for i in anime_ids if i}
    if not ids:
        return {}
    with get_session() as s:
        rows = list(s.exec(select(AnimeTorrent.anime_id, AnimeTorrent.episode).where(
            AnimeTorrent.anime_id.in_(ids),
            AnimeTorrent.status.in_(HAVE_STATUSES),
            AnimeTorrent.qb_progress >= 1.0,
            AnimeTorrent.episode >= 1).distinct()))
    out: dict = {}
    for aid, ep in rows:
        # 小数集（11.5 插入话）不计：bgm 的 total_episodes 不含它，计进去会让 12 集番
        # "凑够 12 个集号"却其实缺一整集。整数判断放 Python 侧：SQLite 的 floor()
        # 要编译期开关才有，不可移植。
        if isinstance(ep, (int, float)) and float(ep).is_integer():
            out.setdefault(aid, set()).add(int(ep))
    return out


def is_finished(a, covered: set) -> bool:
    """这部番是不是【已完整到手】。判据故意保守：宁可不判，绝不误判。

    · 总集数未知/为 0 → 不判（bgm 没给，无从谈起）
    · 用户点过『继续订阅』→ 不判（finish_optout）
    · 有【集号歧义段】(O<T，见 ambiguous_range) → 不判：那一段上绝对编号与季内编号取值域重叠，
      绝对源的第 O+k 集会在集号维度上冒充季内第 O+k 集，coverage 是【假的】，
      会把只下了半季的番判成完结。
    · 其余：要求 1..T 每一集都在手。
      用"覆盖"而不是"计数 ≥ T"：计数会被小数集/特别篇/超界集号灌水。
    """
    if a is None or a.finish_optout:
        return False
    t = a.total_episodes or 0
    if t <= 0 or ambiguous_range(a) is not None:
        return False
    return all(k in covered for k in range(1, t + 1))


_FINISH_BACKFILL_KEY = "_FINISH_BACKFILL_DONE"


# 巡检类功能第一次上线时，历史存量会被【一次性】全部判中——那不是"刚发生"的消息，
# 几十条推送只会把限流额度占满、把真正的新事件挤掉。每种巡检各记一条"回填做过了"，
# 走 meta 库（与 MOVIE_SCAN_LAST 同款）、跨重启有效。
_IDLE_BACKFILL_KEY = "_idle_backfilled"


def _backfilled(key: str) -> bool:
    from db import get_meta_session
    from db.models import Setting
    try:
        with get_meta_session() as s:
            return s.get(Setting, key) is not None
    except Exception:
        return True     # 读不到就当作"做过"——宁可少发一批通知，也别在库有问题时刷屏


def _mark_backfilled(key: str) -> None:
    from db import get_meta_session
    from db.models import Setting
    try:
        with get_meta_session() as s:
            if s.get(Setting, key) is None:
                s.add(Setting(key=key, value="1"))
                s.commit()
    except Exception as e:
        log.warning("记录回填标记 %s 失败（下轮可能重复回填一次）：%s", key, e)


def _finish_backfilled() -> bool:
    return _backfilled(_FINISH_BACKFILL_KEY)


def _mark_finish_backfilled() -> None:
    _mark_backfilled(_FINISH_BACKFILL_KEY)


async def sweep_finished() -> int:
    """维护 finished_at：集齐的打标记、不再集齐的【撤销】标记。返回本轮新判定的部数。

    只打标记；是否据此停止自动下新集由 config.ANIME_FINISH_UNSUB 决定（默认关）。

    【必须能撤销，这是本函数最要紧的性质】判据（episode_coverage）是【瞬时量】——
    删了一集文件、某集被标 error、bgm 重识别后 total_episodes 变大，它都会跟着变；
    而 finished_at 若只增不减，就是拿一个会浮动的判据去写一个永久的结论：
    一次误判（bgm 少记一集是真实存在的）之后，UNSUB=on 时该番【永久停订】、
    UNSUB=off 时它也【永久退出断更巡检】，而用户不点那个按钮就永远好不了。
    所以每轮把两个方向都算一遍。finish_optout 的番两个方向都不碰（那是用户的显式意志）。
    """
    with get_session() as s:
        # 【这里【绝不能】换成 subscribed_where()】看着像是同一个判据的手抄，其实不是：
        # subscribed_where 会在开了停订时附加 `finished_at IS NULL`，而本函数的候选集【必须】
        # 包含【已经标记过完结】的番——撤销那一半正是要在它们身上跑。换过去之后，
        # 已标记的番再也进不了候选，撤销静默死掉、FIN-LATCH 当场复活，而全部用例照绿。
        cands = list(s.exec(select(Anime).where(
            Anime.confirmed == True, Anime.rejected.is_not(True),   # noqa: E712
            Anime.finish_optout.is_not(True))))
    if not cands:
        return 0
    cov = episode_coverage([a.id for a in cands])
    # 【关掉开关只停"判"，不停"撤"】ANIME_FINISH_ENABLED 关掉时若整个函数早退，已经标上的
    # finished_at 就被永久冻结在那儿——而消费侧（订阅闸、仪表盘、徽标）看的是另一个开关
    # ANIME_FINISH_UNSUB。于是"关掉完结判定"反而让一批番永久停订，且再也没有路径能解冻。
    # 撤销是清理动作，任何时候都该跑。
    hits = ([a for a in cands if a.finished_at is None and is_finished(a, cov.get(a.id, set()))]
            if config.ANIME_FINISH_ENABLED else [])
    stale = [a for a in cands if a.finished_at is not None and not is_finished(a, cov.get(a.id, set()))]
    if not hits and not stale:
        return 0
    # 【首轮回填不推送】老库第一次跑到这里时，历史上早已完结的番会被一次性全部判定——
    # 那不是"刚刚完结"的消息，几十条推送只会把限流额度占满、把真正的新事件挤掉。
    # 判据必须是"本库【从来】没判过完结"，而不是"当前一部都没标记"：cands 排除了 finish_optout，
    # 用户对已完结的番点几次『继续订阅』就会让后者重新成立，于是下一批真的完结的番静默无声。
    # 用一条 setting 记"回填做过了"，一次性、跨重启有效。
    # 【闩要在"第一轮有命中"时就落下，不能只在回填那一轮落】原写法是
    # `backfill = len(hits) > 5 and not _finish_backfilled()`，而唯一的落闩点在
    # `if backfill:` 里面——于是闩的语义变成"本库出现过一轮 ≥6 命中"，
    # 而落闩那一轮恰恰就是被静默的那一轮。后果：一个从来没有过 ≥6 命中的库，
    # 闩永远不落；等到季末某一轮真有 6 部同时完结时，那一轮被当成"首轮回填"整批静默，
    # finished_at 照写、停订照生效，用户一条通知都收不到。
    # 兄弟路径 sweep_idle 的同款闩是【无条件】落库的（见那里）。
    first_ever = not _finish_backfilled()
    backfill = first_ever and len(hits) > 5
    now = datetime.now()
    done, undone = [], []
    with get_session() as s:
        for a in hits:
            row = s.get(Anime, a.id)
            # 重取再确认：拿到候选之后要 await 发通知，期间用户随时可能点『继续订阅』。
            if row is None or row.finished_at is not None or row.finish_optout:
                continue
            row.finished_at = now
            s.add(row)
            done.append(a)
        for a in stale:
            row = s.get(Anime, a.id)
            if row is None or row.finished_at is None or row.finish_optout:
                continue
            row.finished_at = None
            s.add(row)
            undone.append(a)
        s.commit()
    if hits and first_ever:
        _mark_finish_backfilled()   # 有命中的第一轮就落闩，与是否静默无关
    if backfill:
        log.info("完结判定：首轮回填 %d 部（不推送，避免把限流额度占满）", len(done))
    else:
        for a in done:
            # 【按番去重 + 长冷却】finished_at 只挡得住"标记还在"的重复，挡不住【反复翻转】：
            # 删掉一集 → 巡检撤销标记 → 补下回来 → 再判完结，每翻一次就多一条"全 N 集已下齐"。
            # qB 里删种、临时 missingFiles、总集数被 bgm 改来改去，都会让它翻。
            # 冷却是进程内的（重启即忘），所以这是【压住抖动】不是【跨重启去重】——
            # 后者的正解是给 Anime 加一列 finish_notified_at，要开 revision，留给下一版。
            await notify_event("finished", f"{display_of(a)} 全 {a.total_episodes} 集已下齐"
                                           + ("，已停止自动下新集" if config.ANIME_FINISH_UNSUB else ""),
                               key=str(a.id), cooldown=7 * 24 * 3600)
            # 完结这条【不】因为通知没发出去就回滚标记：finished_at 是业务状态（它决定停不停订），
            # 不是"通知记账"。通知丢了顶多少一条推送，而详情页的徽标一直在。
    if undone:
        log.info("完结判定：%d 部不再集齐，已撤销完结标记（删过文件/失败/总集数变了）", len(undone))
    if done:
        log.info("完结判定：%d 部集齐（%s）", len(done),
                 "已停订" if config.ANIME_FINISH_UNSUB else "仅标记")
    return len(done)


async def sweep_idle() -> int:
    """追番中的番长期没有新种子 → 提醒一次。返回本轮提醒的部数。

    这是发现"源失效 / 字幕组断更 / feed 地址改了"的唯一自动手段——那类故障不会报错，
    表现只是"好几天没更新了"，而那正是用户最晚才会察觉的一种。

    【已判完结的不提醒】它本来就该没有新种子了。
    【用 created_at 而不是 release_time】release_time 的时区基准按站分裂（每个源类自带 TZ），
    且明确只用于显示层；created_at 是我们自己入库的时刻，口径统一、且正是"多久没收到新东西"要问的。
    """
    days = config.ANIME_IDLE_DAYS
    if days <= 0:
        return 0
    cutoff = datetime.now() - timedelta(days=days)
    with get_session() as s:
        # 【同样不能换成 subscribed_where()】断更巡检要的是"还在追、且没完结"，
        # 而 subscribed_where 的完结那一半是【条件性】的（只在开了停订时才加）。
        # 换过去之后，关着停订开关时已完结的番会重新进入断更巡检、每 N 天报一次"这番没动静"。
        subs = list(s.exec(select(Anime).where(
            Anime.confirmed == True, Anime.rejected.is_not(True),   # noqa: E712
            # 【点过『继续订阅』的不提醒】那是一部用户已经知道情况的番（多半正是"完结了但
            # bgm 少记一集"），每 N 天提醒它断更纯属打扰，而且它永远不可能再被判完结、
            # 所以会一直复发。
            Anime.finish_optout.is_not(True),
            Anime.finished_at.is_(None))))
        # 【注意不要在这里早退】"一部在追的番都没有"恰恰是首轮回填最该跑的场景之一：
        # worker 里 sweep_finished 就在本函数【前面】跑且默认开着，升级当天所有"老且集齐"的番
        # 会先被打上 finished_at、当场掉出 subs——若在这里 return，回填对它们覆盖率是 0，
        # 而它们日后一旦被撤销完结标记回到订阅态，就会各报一条几百天前的假断更。实测踩过。
        latest = {aid: t for aid, t in s.exec(
            select(AnimeTorrent.anime_id, func.max(AnimeTorrent.created_at))
            .where(AnimeTorrent.anime_id.in_([a.id for a in subs]))
            .group_by(AnimeTorrent.anime_id))}
    # 【活跃度上界】"断更"说的是【最近才安静下来】的番。没有上界的话，库里躺着的几十部
    # 两年前的老番会永远满足条件，每 N 天原样重发一次，而真正刚出事的那部只贡献一个计数。
    # 用 4 倍阈值（默认 14~56 天）而不是按 quarter 判：不依赖季度字段填得对不对，语义也更直白。
    #
    # 【代价，必须知道】这是个【窗口】不是【下限】：一部番一旦静默超过 4 倍阈值就再也不提醒了。
    # 如果那段时间里通知一直没送出去（NOTIFY_URL 还没配、被限流吞掉、推送服务挂了），
    # 这部番就悄悄滑出了窗口，用户从头到尾不会收到任何消息。
    # 之所以仍然接受：断更提醒的定位是"及时发现源失效"，一个两个月前就断的番早已不是"及时"，
    # 而详情页与仪表盘一直看得到它的最后更新时间。设置页的说明里写了这个窗口。
    floor = datetime.now() - timedelta(days=days * 4)
    # 【下界只对"已经成功提醒过一次"的番生效】上面那段说的代价是真的：通知在那段时间里
    # 一直没送出去（NOTIFY_URL 还没配、被限流吞掉、推送服务挂了），番就悄悄滑出窗口、
    # 用户从头到尾收不到任何消息——而这正是"源失效"最容易发生的时候（新装、刚改配置）。
    # 豁免掉这种番的下界，等于把承诺从"窗口内提醒"改成"每部番退出前一定成功送达过一次"。
    # 存量老番靠下面的一次性回填挡住，不会在升级后灌一波。
    if not _backfilled(_IDLE_BACKFILL_KEY):
        # 【回填范围必须【大于】subs，不能复用上面那批】豁免判据作用在"所有番"上，
        # 而 subs 只含"在追且未完结"的。两者口径不一致时，被漏掉的番日后一旦回到订阅态
        # （qB 里删一集 → sweep_finished 撤销完结标记 / 恢复订阅 / 改总集数），
        # 就会带着 idle_notified_at=None 直接命中豁免，被当成"从没提醒过"报一条 800 天前的假断更。
        # 更要命的是时序：worker 里 sweep_finished 就跑在 sweep_idle 【前面】且默认开着，
        # 于是升级当天所有"老且集齐"的番先被打上 finished_at、当场掉出 subs——
        # 回填对它最想挡的那批番覆盖率正好是 0（实测 0/5）。所以这里【重新查一遍全表】。
        now0 = datetime.now()
        with get_session() as s:
            all_latest = {aid: t for aid, t in s.exec(
                select(AnimeTorrent.anime_id, func.max(AnimeTorrent.created_at))
                .group_by(AnimeTorrent.anime_id))}
            done_ids = [aid for aid, t in all_latest.items() if t is not None and t < floor]
            n = 0
            for aid in done_ids:
                row = s.get(Anime, aid)
                if row is not None and row.idle_notified_at is None:
                    row.idle_notified_at = now0
                    s.add(row)
                    n += 1
            s.commit()
        if n:
            log.info("断更巡检：首轮回填 %d 部早已静默的番（不推送；含已完结/已忽略的）", n)
        _mark_backfilled(_IDLE_BACKFILL_KEY)
        _done = set(done_ids)
        for a in subs:
            if a.id in _done and a.idle_notified_at is None:
                a.idle_notified_at = now0   # 本轮内存副本同步，免得下面又选中它
    if not subs:
        return 0            # 回填已经做过了（若需要），到这里就没有可提醒的对象了
    stale = []
    for a in subs:
        last = latest.get(a.id)
        if last is None or last >= cutoff:
            continue          # 一条种子都没有的番不算"断更"——它是还没开播/没收到过，另一回事
        if last < floor and a.idle_notified_at is not None:
            continue          # 安静太久【且已经提醒过】：那不是"断更"是"早就完了"
        if a.idle_notified_at and a.idle_notified_at >= cutoff:
            continue          # 提醒过了且还没跨过一个完整的静默期，别每轮重发
        stale.append((a, last))
    if not stale:
        return 0
    now = datetime.now()
    # 【按"最近才出事"排序再取前 3】注意是【降序 last】：last 越大＝最后一条种子越新＝
    # 刚安静下来。取静默最久的那几部正是反的——那些多半是早就完结的老番，
    # 而用户需要立刻知道的是"上周还在更、这周没了"的那一部。
    stale.sort(key=lambda x: x[1], reverse=True)
    head = "、".join(f"{display_of(a)}({(now - last).days}天)" for a, last in stale[:3])
    more = f" 等 {len(stale)} 部" if len(stale) > 3 else ""
    ok = await notify_event("idle", f"{days} 天没有新种子：{head}{more}")
    if not ok:
        # 【没发出去就不记账】idle_notified_at 的唯一用途就是"这条已经说过了"。
        # 通知被限流/未订阅/没配 URL 而丢掉时还把它记上，等于把这批番的提醒永久吃掉一次
        # （下次要等满一个静默期）。而这个提醒本身就是靠它去重的，丢了补不回来。
        log.info("断更提醒未发出（未订阅/限流/未配 NOTIFY_URL），不记账，下轮再试")
        return 0
    with get_session() as s:
        for a, _ in stale:
            row = s.get(Anime, a.id)
            if row is not None:
                row.idle_notified_at = now
                s.add(row)
        s.commit()
    log.info("断更提醒：%d 部超过 %d 天没有新种子", len(stale), days)
    return len(stale)


async def sweep_alerts() -> dict:
    """把"需要人工处理的积压"报一次：失败 / 停滞 / 待识别。返回各自的条数。

    【为什么放在巡检里而不是出错的当场】① 交付主链路是本项目最不该为旁枝功能增加风险的地方；
    ② 批量补下可能一次落好几条 error，逐条推送本身就是噪声；③ 这三件事的价值都在"积压到一定
    程度该去处理了"，而不在"某一条具体失败了"——详情页本来就能看到每一条的 fail_reason。

    去重用 (事件, 当前条数) 做 key + 6 小时冷却：条数没变就不重复打扰，变了立刻再说一次。
    """
    with get_session() as s:
        errs = s.exec(select(func.count()).select_from(AnimeTorrent)
                      .where(AnimeTorrent.status == "error")).one()
        stalls = s.exec(select(func.count()).select_from(AnimeTorrent)
                        .where(AnimeTorrent.status == "stalled")).one()
        backlog = s.exec(select(func.count()).select_from(Anime).where(
            Anime.bangumi_id.is_(None), Anime.rejected.is_not(True))).one()
    six_h = 6 * 3600
    if errs:
        await notify_event("failed", f"{errs} 条种子交付失败，去『失败/异常』页看看",
                           key=str(errs), cooldown=six_h)
    if stalls:
        await notify_event("stalled", f"{stalls} 条种子长期无进度（qB 里可能没源了）",
                           key=str(stalls), cooldown=six_h)
    # 待识别是状态型：积压【上穿】阈值时说一次，清空时再说一次。
    # ok 文案随实际条数生成：阈值下穿≠清零，6 部降到 4 部时说"已清空"是假话。
    await notify_state("backlog", backlog >= max(1, config.NOTIFY_BACKLOG_MIN),
                       f"待识别番已积压 {backlog} 部，去『待识别』页处理",
                       "待识别已清空" if backlog == 0 else f"待识别降到 {backlog} 部")
    return {"error": errs, "stalled": stalls, "backlog": backlog}


def resubscribe(anime_id: int) -> bool:
    """用户点『继续订阅』：清掉完结标记，并记下"别再自动判它完结"。

    没有 finish_optout 这一位的话，下一轮巡检会立刻把它再判一次完结 ——
    那个按钮就成了"点了没用"。
    """
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None:
            return False
        a.finished_at, a.finish_optout = None, True
        # 顺手把断更提醒的时间戳也推到现在：这部番多半正是"其实完结了、只是 bgm 少记一集"，
        # 点完『继续订阅』它仍然不会有新种子——不推的话下一个巡检周期就会送来一条断更提醒，
        # 用户刚做完一个操作立刻收到一条"这番没动静"，观感上像是操作失败了。
        a.idle_notified_at = datetime.now()
        s.add(a)
        s.commit()
    return True


def _finished_where():
    """『没有因完结而停订』的 SQL 判据。标注侧单独要它：那几条路径的 confirmed 闸在调用方
    （契约见 tests/test_plan_equivalence.py），只需补上完结这一半。"""
    return [Anime.finished_at.is_(None)] if config.ANIME_FINISH_UNSUB else []


def is_subscribed_row(a) -> bool:
    """『这一行没有因完结而停订』。同上，只管完结那一半。"""
    return not (config.ANIME_FINISH_UNSUB and getattr(a, "finished_at", None) is not None)


def subscribed_where():
    """『这部番会自动下新集』的 SQL 判据 —— **全项目唯一真源**。

    历史上这个判据被手抄在 6 处（flush / 即时下 / 换源兜底 / 批量补下 / 两处标注），
    而本文件里有三段注释各自警告过同一种病：标着『将下载』却永远不下。加一条新判据时
    只要漏改一处就会重现，所以统一到这里。用法：`select(Anime).where(*subscribed_where())`。

    组成：已确认 且 未忽略 且（开了停订时）未判完结。
    """
    conds = [Anime.confirmed == True, Anime.rejected.is_not(True)]  # noqa: E712
    if config.ANIME_FINISH_UNSUB:
        conds.append(Anime.finished_at.is_(None))
    return conds


def is_subscribed(a) -> bool:
    """subscribed_where 的内存版（拿到 Anime 实例时用）。两者必须同口径。"""
    if a is None or not a.confirmed or a.rejected:
        return False
    return not (config.ANIME_FINISH_UNSUB and a.finished_at is not None)


def confirmed_anime_ids(ids) -> set:
    """给定番 id 集合，返回其中【真的会自动下】的（已确认 且 未忽略）。

    新入库里未确认（待确认）番的待下不显示将下载/备用（那是假的、要点确认才会下），
    而显示『待确认』——故先筛出已确认的。已忽略(rejected)的同样排除：执行侧
    (flush/补下)都要求 `confirmed and not rejected`，只看 confirmed 会把已忽略番的
    待下标成『将下载』甚至『待确认』（可它根本没有确认入口），而它永远不会被下。"""
    ids = {i for i in ids if i}
    if not ids:
        return set()
    with get_session() as s:
        return set(s.exec(select(Anime.id).where(
            Anime.id.in_(ids), *subscribed_where())))


def auto_off_reasons(ids) -> dict:
    """给定番 id，返回 {id: 「为什么后台不会自动下它」}；会自动下的番不出现在结果里。

    值取自 `pending_breakdown` 用的同一套词：『待确认』/『已忽略』/『已完结』。
    **判序也与它一致**（未确认 → 已忽略 → 已完结），这样渲染侧与统计侧不会给出两种说法。

    【为什么需要它】`confirmed_anime_ids` 只回答"会不会下"，回答不了"为什么不下"。
    于是渲染侧只能把三种情况统统显示成『待确认』，并把用户指去『待确认』tab——
    而已忽略/已完结的番根本不在那个 tab 里，用户到那儿只会看到一个空列表。
    """
    ids = {i for i in ids if i}
    if not ids:
        return {}
    out = {}
    with get_session() as s:
        for aid, confirmed, rejected, finished in s.exec(select(
                Anime.id, Anime.confirmed, Anime.rejected, Anime.finished_at).where(
                Anime.id.in_(ids))):
            if rejected:
                out[aid] = "已忽略"
            elif not confirmed:
                out[aid] = "待确认"
            elif finished is not None and config.ANIME_FINISH_UNSUB:
                out[aid] = "已完结"
    return out


def get_anime(anime_id: int) -> Anime | None:
    with get_session() as s:
        return s.get(Anime, anime_id)


def episode_numbering_conflict(anime_id: int) -> list:
    """该番是否存在【疑似同集不同编号】——返回可疑的绝对编号集号列表（空=没问题）。

    判据：番的 bgm 总集数已知、且库里有集号【超过总集数】的种子，同时又存在集号在正常范围内的种子。
    这说明两个源用了不同编号体系（绝对 vs 季内），同一集会被当成两集各下一份到同一目录。
    能从双编号标题推出 ep_offset 的番已在入库时自动折算，走不到这里；这里剩下的是推不出的，
    交给人工判断（详情页提示），绝不按发布时间瞎配对——补番/跳周/合集都会误判成同集而漏下。
    """
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None or not a.total_episodes:
            return []
        # 【不能因为"已知 offset"就短路返回】早先这里写着 `or a.ep_offset is not None: return []`，
        # 理由是"能推出 offset 的番已在入库时自动折算，走不到这里"——那个断言只对【新入库】的行成立。
        # 学到 offset 之前入库的绝对编号行不会自动变，于是"学到 offset"这件事本身
        # 反而把人工兜底提示永久关掉了。现在存量行会被 _refold_absolute_episodes 回折，
        # 而这里继续按【实际集号分布】判，才兜得住回折判据覆盖不到的残留。
        eps = {t.episode for t in s.exec(
            select(AnimeTorrent).where(AnimeTorrent.anime_id == anime_id)) if t.episode and t.episode >= 1}
    over = sorted(e for e in eps if e > a.total_episodes)
    within = [e for e in eps if e <= a.total_episodes]
    return over if (over and within) else []


def list_episodes(anime_id: int) -> list[AnimeTorrent]:
    """某番剧的全部种子（按集数、再按入库时间倒序），供详细页展示分集/来源。"""
    with get_session() as s:
        return list(s.exec(
            select(AnimeTorrent).where(AnimeTorrent.anime_id == anime_id)
            .order_by(AnimeTorrent.episode, AnimeTorrent.created_at.desc())
        ))




def downloaded_count(anime_id: int) -> int:
    """该番【删得掉的】文件数——供 UI 决定要不要显示『删除文件』、以及确认框里报几个。

    含 stalled：半成品文件在盘、delete_anime_files 也会删它（deleted 文件已删，不计）。
    【不含 downloading】：那是交付中的占位，qB 里还没有这个 hash，删除路径会跳过它
    （见 delete_anime_torrent）。口径必须与删除路径逐字一致——否则确认框说"删 3 个"、
    实际只删掉 2 个，或者对一条 downloading 点删除得到『没删成』的假错误提示。
    """
    return downloaded_counts([anime_id]).get(anime_id, 0)


def downloaded_counts(anime_ids) -> dict:
    """一次 SQL 取多部番的可删文件数 {anime_id: n}（口径与 downloaded_count 完全一致，共用同一组判据）。

    给列表页用：『已忽略』面板要为每部番决定显不显示『删除文件』，逐番调 downloaded_count
    等于每番开一个 session 打一条 SQL（N+1），而这个面板会被任意操作和 30 秒定时器整体重建。
    """
    ids = list(anime_ids)
    if not ids:
        return {}
    with get_session() as s:
        rows = s.exec(select(AnimeTorrent.anime_id, func.count()).where(
            AnimeTorrent.anime_id.in_(ids),
            AnimeTorrent.status.in_(HAVE_STATUSES),
            AnimeTorrent.status != "downloading",
        ).group_by(AnimeTorrent.anime_id)).all()
    return {aid: n for aid, n in rows}


# ---------------- 给 UI 的操作 ----------------

def confirm_anime(anime_id: int, pref_source: str | None = None) -> None:
    """确认下载该番；pref_source 语义是【三态】，别退回二态：

      · None（默认）＝不动现有锁定源。详情页的『确认下载』不带这个参数，
        而补齐/绑定 bgm 都会把番打回待确认、UI 明确指引用户回来点确认——
        以前默认值是 ""、这里无条件 `pref_source or None`，等于按指引走一遍正常流程
        就把用户显式设过的锁定源静默清空，此后按全局优先级挑源（最高优先级组会通吃）。
      · ""   ＝显式解锁，恢复多源兜底。
      · 非空 ＝锁定只下这个组，缺集不兜底。
    """
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is not None:
            a.confirmed = True
            if pref_source is not None:
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
    """拒绝某个番：打上 rejected（移出主列表进『已忽略』页）、不下载，积压待下种子标记跳过。"""
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None:
            return
        a.rejected = True
        a.confirmed = True   # 人工拒绝置 confirmed=True → 与『超期忽略(confirmed=False)』区分，改开始日不会掀翻它
        s.add(a)
        for t in s.exec(select(AnimeTorrent).where(
            AnimeTorrent.anime_id == anime_id,
            AnimeTorrent.status.in_(DOWNLOADABLE_STATUSES),
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
        # 【键必须是 dedup_key，不能是裸 episode】这是 dedup_key 的第六条消费路径——
        # 歧义段（O<T 的番在 (O,T] 上两套编号重叠）里，A 源用绝对号发的『13』与 B 源用季内号
        # 发的『13』不是同一集。按裸集号算的话，A 源那条已下的『13』会把 B 源真正的第 13 集
        # 一起挡在外面：那条 skipped 永远不复活，而 flush（只挑 pending）、批量补下、
        # 换源兜底（要组里有 error，而 reject 把 error 也压成了 skipped）三条路都不碰它 ⇒
        # 那一集永久收不到，页面却弹绿色「已恢复到『订阅中』，补下 N 集」。
        # 兄弟路径 _revive_orphaned_skipped 已经改过，这条当时没跟上。
        amb = ambiguous_range(a)
        have_eps = {dedup_key(amb, 0, t.episode, t.source)[1:]
                    for t in all_rows if t.status in HANDLED_STATUSES}
        for t in all_rows:
            # 只放回『该集尚无下载/未被删过』的 skipped（集去重留下的旧版本）；用户主动删过的记为 deleted，
            # 其集已进 have_eps 而被排除——免得恢复订阅时把用户特意删掉的文件又重新下回来。
            if (t.status == "skipped"
                    and dedup_key(amb, 0, t.episode, t.source)[1:] not in have_eps):
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
        # 【两边都要查】这道闸跑在种子搬家【之前】（重指 anime_id 的循环在几十行之后），
        # 而 bind_anime_bgm / enrich_anime 的身份守卫里 keeper 恒是【当前正在操作的那条】——
        # 常常是刚绑定、还没有种子的残条，攒着可疑种子的反而是 loser。
        # 只查 keeper 的话，这道闸在最常见的那条路径上完全看不见证据（实测：keeper 无种子、
        # loser 挂着 ep=78/2026-08 的可疑种子，合并后照样升成追番中）。
        if active and (binding_looks_wrong(s, keeper) or binding_looks_wrong(s, loser)):
            # 【绑定看着不对时，union-active 不能生效】这是决定订阅态的【第四处】。
            # union-active 本身是对的（它防的是"番静默从追番中掉回待确认、从此停更"），
            # 但合并跑在自动路径上时（enrich_anime 末尾的身份守卫，由后台 retry_unmatched /
            # 批量重新识别驱动），它会把刚被降级的番悄悄升回『追番中』——而降级的意思正是
            # "这个 bgm 绑定多半错了，等人看一眼"，没人看过就不该恢复自动下载。
            # 落『待确认』而不是『已忽略』：前者在列表里看得见、一键就能确认，后者是静默的。
            keeper.confirmed, keeper.rejected = False, False
        elif active:
            keeper.confirmed, keeper.rejected = True, False
        else:
            keeper.confirmed = keeper.confirmed or loser.confirmed
            keeper.rejected = keeper.rejected or loser.rejected
        # 【合并时必须随 loser 迁过来的订阅控制字段清单】——以后再加同类字段（锁源/锁版本/
        # 单番开关之类）务必同步加到这里。pref_keyword 之前就是漏在这儿的：keeper 恒是刚绑定的
        # 『待确认』残条、loser 才是带关键词的主番，loser 随即被删，于是用户设的『版本』硬锁
        # （繁日/简日/1080p）被静默清空，下一轮 flush 立刻按无关键词口径挑版本，
        # 把用户明确排除过的版本投给 qB，详情页那个输入框也变空、无任何提示。
        if not keeper.pref_source and loser.pref_source:
            keeper.pref_source = loser.pref_source
        if not keeper.pref_keyword and loser.pref_keyword:
            keeper.pref_keyword = loser.pref_keyword
        # ep_offset 同样必须跟过来。它不是"偏好"而是【学来的事实】：某源用绝对编号（'- 16(88)'）时
        # 靠它把绝对号折回季内集号。loser 才是收过种子、学到 offset 的那一部；随 loser 一起删掉的话，
        # 同一集会以"绝对号 88"和"季内号 16"两个不同的去重键并存 —— 集去重当场失效，两份下进同一目录，
        # 而且 ambiguous_range() 的歧义段判定也一并退化（它同样要 ep_offset）。
        if keeper.ep_offset is None and loser.ep_offset:
            keeper.ep_offset = loser.ep_offset
        # finish_optout 是【用户的显式意志】（"别再自动判它完结"），任一方点过就该保留——
        # 否则用户点完『继续订阅』，再做一次绑定/重新识别就被静默撤销，下一轮巡检立刻重判。
        keeper.finish_optout = bool(keeper.finish_optout or loser.finish_optout)
        # finished_at 反过来：合并会改变种子构成，旧结论作废，清掉让巡检重算（它现在支持撤销了）。
        keeper.finished_at = None
        # 季度：keeper 尚未落盘而 loser 已有在下/已下文件时采用 loser 的季度，
        # 免得合并后新集去了 keeper 的（可能不同）季度目录，与已落盘的旧集散在两处。
        if loser.quarter and not _has_handled_torrents(s, keeper_id) and _has_handled_torrents(s, loser_id):
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
    # 种子行改挂 keeper 之后再回折一次：迁过来的那批可能是按绝对编号入的库（loser 学到 offset
    # 之前收下的），而回折要同时有 ep_offset 和 total_episodes——两个值上面都已按"空则补"迁过来。
    if keeper is not None and _foldable(keeper):
        _refold_absolute_episodes(s, keeper)
    s.commit()


def _has_handled_torrents(s, anime_id: int) -> bool:
    """该番是否已下过（在下/已下/停滞/曾删）——有则季度已落盘/曾落盘，不该被重识别改（避免散目录）。

    用 HANDLED_STATUSES（sent/downloading/stalled/deleted）：deleted=删过也算季度已定；
    stalled=半成品仍在盘（save_path 按旧季度写过），更该锁季度——否则重识别把季度冲掉、停滞文件遗留旧目录、新集散两处。
    """
    return s.exec(select(AnimeTorrent).where(
        AnimeTorrent.anime_id == anime_id,
        AnimeTorrent.status.in_(HANDLED_STATUSES),
    )).first() is not None


def _has_unmovable_files(s, anime_id: int) -> bool:
    """该番是否有【搬不动】的已下文件：盘上有文件(HAVE)但已归档(archived_at 非空)。

    归档=从 qB 移除只留文件，relocate 明确把这类行排除在外（engine.relocate 的选行带
    archived_at.is_(None)，注释写明"已归档的不在 qB，setLocation 移不动"）。
    所以只要存在这种行，改名/改季度就会造成【程序侧再也补救不了】的散目录：新集落新目录、
    这批文件永久留在旧目录，UI 上没有任何按钮能把它们搬过去。此时宁可不改名。
    """
    return s.exec(select(AnimeTorrent).where(
        AnimeTorrent.anime_id == anime_id,
        AnimeTorrent.status.in_(HAVE_STATUSES),
        AnimeTorrent.archived_at.is_not(None),
    )).first() is not None


async def enrich_anime(anime_id: int, freeze_empty_path: bool = False) -> bool:
    """富集某番剧：用它已有的名字 + 最近一条种子回退，重取 bgm 元数据并覆盖。

    freeze_empty_path=True 给【不会 relocate 的调用方】用（后台 retry_unmatched、批量重新识别）。
    apply_bgm_meta 的 keep_path 只冻结**已经有值**的路径字段——空值照样会被填上，而
    「空 → 有值」同样会改归档目录：一部还没识别出 bgm 就被人工点下过的番，
    jp_name 是空的、文件落在 <根>/<季度>/<种子解析名>/，后台识别成功后 jp_name 被填上，
    新集就去了日文原名的目录，已下的集留在旧目录，同一部番裂成两个文件夹、全程无提示。
    带 relocate 的入口（详情页/列表页的单番按钮）不需要它——那两条会把文件一起搬过去。

    【为什么不像 movies._upsert_movie 那样无条件冻】剧场版的后台与人工是两个不同函数，
    这边三个入口共用本函数。无条件冻会把详情页那条唯一的补救路径一起冻死：
    用户点『重新识别』想把目录修正过来，结果名字根本不变、relocate 判 new==old 直接早退。
    """
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
        bgm_before = a.bangumi_id      # 见下面 await 之后的那道 compare-and-set

    info = await enrich.resolve(names, release_time, episode, info_hash)
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None:
            return False
        # 【await 期间别人改了 bangumi_id 就让路】enrich.resolve 的整体预算是 120 秒
        # （services/enrich.py 的 _RESOLVE_BUDGET），这是全项目最长的一个 await 窗口之一。
        # 原来这里只重取了一次 a，挡住的是"番在 await 期间被删了"，没挡"绑定在 await 期间变了"——
        # 而 apply_bgm_meta 对 bangumi_id 是【无条件覆写】的（keep_path 只冻结 jp_name/display_name）。
        #
        # 于是这条时序会静默吃掉用户的人工决定：后台 retry_unmatched 选中一部『待识别』番
        # （bangumi_id IS NULL）→ 进 enrich 等 bgm 最长 120 秒 → 这期间用户在详情页按界面指引
        # 点『绑定 bgm』填了正确的 subject id → 后台回来用自己那次自动匹配的结果整个盖回去。
        # 更大的触发面是设置页的『批量重新识别』(reenrich_scope)：它一次收齐几十个 id 再逐个跑，
        # 窗口是几十个 120 秒之和。而覆写之后还会走下面的身份守卫 _merge_anime —— 那一步会
        # 【删掉】另一条番记录，没有撤销入口。
        #
        # 判据用 compare-and-set 而不是"有值就不覆盖"：详情页的『重新识别』本来就是"我要你重算"，
        # 它读到的 bgm_before 与回来时相同，照常覆写；只有【第三方在这段窗口里改过】才让路。
        if a.bangumi_id != bgm_before:
            log.info("重新识别期间该番的 bgm 绑定已被改动（%s → %s），本次结果作废、以后者为准：%s",
                     bgm_before, a.bangumi_id, display_of(a))
            return a.bangumi_id is not None
        # 无已下集就采用 bgm 季度（纠正种子解析得来的错季度）；有已下集才保留，避免散目录
        handled = _has_handled_torrents(s, anime_id)
        # season 也在快照里：_apply_bgm 会从【刚被填上、还没还原】的新名字反推季号并留下来
        # （它跑在 apply_bgm_meta 之后、下面的还原之前），而 build_save_path 的入参正是
        # (quarter, 名字, season)。漏了它，开着 ANIME_SEASON_SUBFOLDER 时新集会落进
        # Season 3 而已下的集留在 Season 1 —— 同一部番裂成两个目录，而这两条链路都不 relocate。
        snap = ({k: getattr(a, k) for k in ("quarter", "jp_name", "display_name", "season")}
                if (freeze_empty_path and handled) else None)
        _apply_bgm(a, info, keep_path=handled)
        if snap is not None:
            for k, v in snap.items():
                setattr(a, k, v)      # 连"原本为空"的也还原，见本函数 docstring
        s.add(a)
        s.commit()
        # 身份守卫：若该 bgm_id 已被别的番占用，合并过来，杜绝同一部番裂成两条
        if a.bangumi_id is not None:
            for other in list(s.exec(select(Anime).where(
                    Anime.bangumi_id == a.bangumi_id, Anime.id != a.id))):
                _merge_anime(s, other.id, a.id)
    return bool(info)


def bind_preview(anime_id: int, bgm_id: int) -> dict:
    """『绑定 bgm』按下去【之前】会发生什么——供 UI 回显。只读，不改任何数据。

    回显的重点不是"元数据会变成什么"（那是可逆的、也看得见），而是这两件【不可逆】的事：

      ① **另一条番会被删掉**。bind_anime_bgm 末尾有身份守卫：该 bgm_id 已被别的番占用时
         调 _merge_anime 把它并过来，而 _merge_anime 的最后一步是 `s.delete(loser)`。
         对用户来说这是"我只是想改个 ID"，结果一条【追番中、已经下过几集】的番记录没了。
      ② **两边的集号体系可能不兼容**，合并后同一集会以两个去重键并存，各下一份到同一目录。
         真实例子（本项目实测）：Re:Zero 的 LoliHouse 用全系列绝对编号 78/79/80，
         ANi 用季内编号 12/13/14，两组是同一批集；合并后 3 集内容产生 6 个去重键。
         ep_offset 救不了——它只能从 '16(88)' 那种双编号标题自动学，单编号标题学不到，
         而全项目没有任何入口能手工设它。

    返回 {"merge": [每条将被并入的番...], "warn": [人话告警...]}；merge 为空表示不会触发合并。
    """
    out: dict = {"merge": [], "warn": []}
    with get_session() as s:
        me = s.get(Anime, anime_id)
        if me is None:
            return out
        others = list(s.exec(select(Anime).where(
            Anime.bangumi_id == bgm_id, Anime.id != anime_id)))
        if not others:
            return out

        def _eps(aid: int) -> list:
            return sorted({t.episode for t in s.exec(
                select(AnimeTorrent).where(AnimeTorrent.anime_id == aid))
                if isinstance(t.episode, (int, float)) and t.episode >= 1})

        my_eps = _eps(anime_id)
        for o in others:
            rows = list(s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == o.id)))
            o_eps = _eps(o.id)
            out["merge"].append({
                "id": o.id,
                "name": display_of(o),
                "state": ("追番中" if (o.confirmed and not o.rejected)
                          else "已忽略" if o.rejected else "待确认"),
                "torrents": len(rows),
                "handled": sum(1 for t in rows if t.status in HANDLED_STATUSES),
                "aliases": len(list(s.exec(select(AnimeAlias).where(AnimeAlias.anime_id == o.id)))),
                "episodes": o_eps,
            })
            # 【集号区间不重叠】= 两边多半在用不同的编号体系（季内 vs 全系列绝对）。
            # 判据故意保守：只在两边【都有】正片集号、且【一个交集都没有】时才告警——
            # 有交集说明至少共用同一套编号，合并是安全的。
            if my_eps and o_eps and not (set(my_eps) & set(o_eps)):
                keys = len(set(my_eps) | set(o_eps))
                out["warn"].append(
                    f"本番集号 {_ep_span(my_eps)}，对方集号 {_ep_span(o_eps)}，两边【没有一集重合】。"
                    f"若它们其实是同一批集的不同编号写法（如全系列绝对编号 vs 季内编号），"
                    f"合并后会产生 {keys} 个去重键，每集各下一份到同一个目录。")
    return out


def _ep_span(eps: list) -> str:
    """集号列表 → 人话区间。'12–14（3 集）' / '7（1 集）'。"""
    if not eps:
        return "无"
    fmt = lambda e: str(int(e)) if float(e).is_integer() else str(e)   # noqa: E731
    if len(eps) == 1:
        return f"{fmt(eps[0])}（1 集）"
    return f"{fmt(eps[0])}–{fmt(eps[-1])}（{len(eps)} 集）"


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
        # 【显式绑定要能真的纠正名字与季度】原本是 keep_path=_has_handled_torrents(...)，理由写的是
        # "有已下集才保留，避免散目录"。但那个判据太宽：番一旦下过任何一集（连 deleted 都算）就再也
        # 改不动番名/季度，只有 bangumi_id 被换成新番的——身份与名字/目录长期互相矛盾，而这是【全项目
        # 唯一的人工纠名入口】（movies.bind_movie_bgm 早就是 keep_path=False）。
        # 现在只对【真的搬不动】的情形冻结：本函数的两个调用点（详情页『绑定 bgm』、列表页待识别的
        # 『绑定』）成功后都会调 maybe_relocate_anime 把已下的集搬到新目录，散目录本该由它兜住——
        # 唯独【已归档】的行搬不了（不在 qB，relocate 显式排除），那种情况改名就是制造无法补救的散目录。
        # 自动路径 enrich_anime 的冻结保持原样（那是后台自作主张，更不该动目录）。
        _apply_bgm(a, info, keep_path=_has_unmovable_files(s, anime_id))
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


async def reenrich_scope(seasons: int | None = None) -> int:
    """按季度范围重新识别（bgm）：seasons=1 当季 / 2 近半年 / 4 近1年 / None 全部。返回命中数。

    对范围内的番重跑一次识别——顺带把之前『待识别』(未匹配)的重试、已匹配的刷新元数据。
    """
    quarters = None
    if seasons:
        quarters, q = set(), quarter_of(datetime.now())
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
        try:
            # freeze_empty_path：本入口是【批量】的，页面上不问"要不要搬迁"（几十部番逐个弹框
            # 不现实），所以它与后台 retry_unmatched 同类——不能让它改已下番的归档目录。
            if await manual_enrich(aid, freeze_empty_path=True):   # 含清零后台重试计数
                n += 1
        except Exception as e:
            log.warning("重新识别失败 anime=%s: %s", aid, e)
    log.info("重新识别（范围=%s）完成：%d/%d 命中", seasons or "全部", n, len(ids))
    return n


async def manual_enrich(anime_id: int, freeze_empty_path: bool = False) -> bool:
    """**人工触发的一次重新识别**——所有『重试识别 / 重新识别』按钮都该走这里。

    比 enrich_anime 多做一件事：清零 enrich_tries。这一步不是可选的：
    后台重试池的闸是 `enrich_tries < REENRICH_MAX_TRIES`，一部试满 5 次的番已经掉出池子，
    用户点了『重试识别』若不清零，它就只是【当场试一次】，此后照旧一次都不会自动重试——
    而用户的本意恰恰是"我看它一直没识别出来，帮它再试试"。

    【为什么要有这个函数】三个入口曾各写各的：详情页清、『待识别』tab 不清、批量重识别清。
    同一个按钮名在两个页面上行为不同，而差别只有在几小时后"它怎么还没识别出来"时才显形。
    """
    reset_enrich_tries(anime_id)
    return await enrich_anime(anime_id, freeze_empty_path=freeze_empty_path)


def reset_enrich_tries(anime_id: int) -> None:
    """清零某番的 bgm 后台重试计数（manual_enrich 调用；单独用请三思，见那里的说明）。"""
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
    consumed = 0
    reached = 0        # 本轮真正【问到了 bgm】的部数（问到了但没搜到也算）
    for aid in due:
        with get_session() as s:
            a = s.get(Anime, aid)
            if a is None:
                continue
            a.enrich_tries = (a.enrich_tries or 0) + 1
            a.last_enrich_at = datetime.now()
            s.add(a)
            s.commit()
            consumed += 1
        before = enrich.net_failures()
        try:
            # freeze_empty_path：后台重试没有 UI、不会 relocate，绝不能改已下番的归档目录
            if await enrich_anime(aid, freeze_empty_path=True):
                n += 1
        except Exception as e:
            log.warning("延迟重识别失败 anime=%s: %s", aid, e)
        if enrich.net_failures() == before:
            reached += 1     # 这一部的请求都发出去也收到了回应，只是没搜到
    # 【bgm 整体不可达时把这一轮的次数退回去】退避阶梯一共只有 REENRICH_MAX_TRIES 次、
    # 总跨度约 15 小时——正好能被 bgm 的一次限流窗口或一次机房故障吃光，之后这批番就永久停在
    # 『待识别』，只能人工一部部救。而"根本没问成"与"问了但搜不到"在信息量上完全不同。
    #
    # 【判据必须是"一部都没问成"，不能是"一部都没命中"】候选池里常驻的恰恰是 bgm 搜不到的番，
    # "没命中"是【稳态】而不是故障——按那个判据退款等于把退避阶梯整个废掉：
    # enrich_tries 永远回到 0、每个检查节拍都重打一遍 bgm，REENRICH_MAX_TRIES 永不兑现。
    # 所以看的是连接层失败计数（services.enrich.net_failures）：只有【每一部都没问成】才退。
    if consumed >= 3 and reached == 0:
        with get_session() as s:
            for aid in due:
                a = s.get(Anime, aid)
                if a is not None and a.enrich_tries:
                    a.enrich_tries -= 1
                    s.add(a)
            s.commit()
        log.warning("延迟重识别：%d 部到点却一部都没问成 bgm（连接层全失败），本轮不消耗重试次数", consumed)
    if due:
        log.info("延迟重识别：%d 到点，命中 %d", len(due), n)
    return n


def _download_candidates(rows: list, pref: str | None = None, have_eps: set | None = None,
                         amb: tuple | None = None) -> list[list]:
    """从一部番的待下种子里挑要下的：按集号分组，每集给出一串【按该先试哪个排好序】的候选。
    have_eps 里的集（已在下/已下）跳过。只挑【正集】，理由见 auto_downloadable_ep。
    amb=ambiguous_range(番)：歧义段按 (集号,源) 分组与去重，与 flush 同口径（见 dedup_key）。
    调用方传进来的 have_eps 必须用同一个键算，否则两侧对不上。

    【返回的是候选列表的列表，不是"每集一条"】。早先每集只返回优先级最高的那一条，于是同集
    的最高优先级种子一旦是坏的（源下架/磁链失效），补下每次都挑它、每次都失败，健康的兄弟永远
    轮不到，该集永久停滞。执行侧（补下）按这个顺序逐条试到成功为止；标注侧（计划/徽标）取 [0]。
    排序里已把"失败过的往后排"压在最前（见 engine.pick_order 的 prefer_fresh）。

    【三条路径现在同口径】标注(计划) / 补下 / 后台 flush 都用 prefer_fresh 的四键排序。
    以前不是：_PLAN_COLS 的列投影里没有 retry_count，于是 prefer_fresh 的第二个键在标注侧
    恒为 0、退化成两键，而补下侧是四键——同一集，详情页标『将下载』的那条与点补下真正去试的
    那条可以不是同一条。D-08 把 retry_count 补进投影、并让 flush 也开 prefer_fresh，
    【两半必须一起做】：只补列不改 flush，就变成标注与后台自动下载分家，那才是真的回归。
    """
    have = have_eps or set()
    by_ep: dict = {}
    for t in rows:
        if not auto_downloadable_ep(t.episode):
            continue
        k = dedup_key(amb, 0, t.episode, t.source)[1:]   # 单番内不必带 anime_id
        if k in have:
            continue
        by_ep.setdefault(k, []).append(t)
    return [engine.pick_order(ts, pref, prefer_fresh=True) for ts in by_ep.values()]


async def download_pending_for_anime(anime_id: int) -> int:
    """把某番剧下 status=pending/error 的种子补下（人工确认后放行）。返回触发的下载数。

    加番剧级授权闸门：只对『已确认且未拒绝』的番补下。
    只补【正集】、每集一份（见 _download_candidates / auto_downloadable_ep）：特别篇与未知集要人工
    对准那一条点『下载』，这个批量按钮不碰它们。
    """
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if not is_subscribed(a):
            return 0
        pref, kw = a.pref_source, a.pref_keyword
        all_rows = list(s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == anime_id)))
    amb = ambiguous_range(a)     # 歧义段按 (集号,源) 去重，与 flush 同口径（见 dedup_key）
    have_eps = {dedup_key(amb, 0, t.episode, t.source)[1:] for t in all_rows
                if t.status in HAVE_STATUSES}  # 已有一份(downloading/stalled)；deleted 不算，同集新 hash 照常下
    pending = [t for t in all_rows if t.status in DOWNLOADABLE_STATUSES]
    if pref:  # 锁定源：只补锁定组的待下集（硬锁、不兜底）
        pending = [t for t in pending if pref == (t.source or "")]
    if kw:     # 版本关键词：再过滤到命中该版本的（繁日/简日/画质…；硬锁、不兜底）
        pending = [t for t in pending if _kw_match(kw, t.raw_title)]
    if not await qb_precheck():   # 与 flush 同款预检，理由见 qb_precheck
        return 0
    n = 0
    for cands in _download_candidates(pending, pref, have_eps, amb):
        for t in cands:          # 逐条试到成功为止：最高优先级那份坏了还有同集的其它源兜底
            ok = await download_anime_torrent(t.id)
            if ok:
                n += 1
                break
            if ok is None:
                return n         # 系统性失败（qB 掉线/路径配错）：当场收手。
                                 # 换个 hash 再试只会两份都进 qB，或把该集候选全烧成 error。
    return n


# 算下载计划只用得到这八列，故用【列投影】而不是取整行 ORM 对象。
# 仪表盘每刷新一次就要为【全部已确认番】算一遍计划：取整行会把整张种子表实例化成 SQLModel 对象，
# 实测 9700 条种子时光 ORM 装配就吃掉 150ms（13 条 SQL 本身只要 7ms），而对象里除这八列外一个都用不到。
# Row 支持属性访问，所以 _download_candidates / pick_order 里仍旧是 t.episode、t.priority 这样用，无需改动。
# 【下游要用新字段时，必须同步加到这里】——漏了会在运行时抛 AttributeError。
_PLAN_COLS = (AnimeTorrent.id, AnimeTorrent.anime_id, AnimeTorrent.status, AnimeTorrent.episode,
              AnimeTorrent.source, AnimeTorrent.raw_title, AnimeTorrent.priority,
              AnimeTorrent.created_at, AnimeTorrent.retry_count, AnimeTorrent.retry_at)


def _retry_ready(t, now) -> bool:
    """这条 pending 现在【到点】了吗——退避排队中的不算候选。

    **flush 与标注侧必须用同一个判据**，否则同一集里"两条都失败重试过、退避时间不同"时
    （_fail 同时写 status=pending 与 retry_at，所以 pending 且 retry_count>0 必然带 retry_at），
    详情页/新入库把 A 标『将下载』而后台实际下 B ——正是 _download_candidates 的 docstring
    声称已经消灭的那种失效形状。
    补下口径（for_backfill）【刻意不用】它：人工点补下不受退避约束，与 download_pending_for_anime 一致。
    """
    at = getattr(t, "retry_at", None)
    return at is None or at <= now


def _plan_statuses(for_backfill: bool) -> tuple:
    """算计划时【真正会被用到】的状态：候选（待下/含失败）+ 已有一份（用来跳过该集）。
    其余状态（skipped/deleted/excluded、非补下口径下的 error）取回来也是当场丢掉，
    不如在 SQL 里就滤掉——库里积压的 skipped 往往比在下的还多。与下面循环的 if/elif 完全等价。"""
    return tuple(set(DOWNLOADABLE_STATUSES if for_backfill else ("pending",)) | set(HAVE_STATUSES))


def download_plan(anime_id: int, for_backfill: bool = False) -> set[int]:
    """这部番会被挑中下载的种子 id 集合，只算不下。供详情页/仪表盘标注。

    【两种口径，别混用】——混用正是过去把『将下载』标到不会下的那条上的原因：
    · for_backfill=False（默认）＝『后台自动会下哪条』：候选只含 pending，与
      flush_ready_downloads 一致（它有意不自动重试 error，见那里注释）。若把 error 也算候选，
      高优先级的 error 会顶掉同集真正会被自动下的 pending，页面标注就与实际相反。
    · for_backfill=True ＝『点补下会挑哪条』：候选含 pending+error（补下确实重试失败的），
      只用于给 error 行判『失败·可补下』还是『失败』。

    两者走同一套挑选（锁定源/版本关键词过滤 + 跳过已有一份的集 + 只算正集、每集一份）。
    """
    with get_session() as s:
        a = s.get(Anime, anime_id)
        # 已忽略(rejected)的番一条都不会被下——执行侧(flush:446 / 补下:1223)都要求
        # `confirmed and not rejected`。这里若只看 confirmed，就会把它的待下标成
        # 『将下载』而实际点下载返回 0 集（页面撒谎）。
        if a is None or a.rejected or not is_subscribed_row(a):
            return set()
        pref, kw = a.pref_source, a.pref_keyword
        all_rows = list(s.exec(select(*_PLAN_COLS).where(
            AnimeTorrent.anime_id == anime_id,
            AnimeTorrent.status.in_(_plan_statuses(for_backfill)))))
    amb = ambiguous_range(a)     # 同 download_pending_for_anime：标注口径必须与执行口径一致
    have_eps = {dedup_key(amb, 0, t.episode, t.source)[1:] for t in all_rows
                if t.status in HAVE_STATUSES}  # 已有一份(downloading/stalled)；deleted 不算，同集新 hash 照常下
    _now = datetime.now()
    pending = [t for t in all_rows
               if t.status in (DOWNLOADABLE_STATUSES if for_backfill else ("pending",))
               and (for_backfill or _retry_ready(t, _now))]   # 与 flush 同口径，见 _retry_ready
    if pref:
        pending = [t for t in pending if pref == (t.source or "")]
    if kw:
        pending = [t for t in pending if _kw_match(kw, t.raw_title)]
    return {c[0].id for c in _download_candidates(pending, pref, have_eps, amb)}


def download_plans_for_ids(anime_ids) -> tuple[set[int], set[int]]:
    """一次算出【两种口径】的计划：(自动会下的, 补下会挑的)。

    页面上这两个集合恒是成对使用的（pending 行看前者、error 行看后者），
    而它们只差"候选里含不含 error"——分别调两次 download_plan_for_ids 会把
    番元数据、种子行、"已有一份"的键全部重算一遍。合成一次就省掉一半。
    """
    both = _plans_for_ids(anime_ids, (False, True))
    return both[False], both[True]


def download_plan_for_ids(anime_ids, for_backfill: bool = False) -> set[int]:
    """批量版 download_plan（口径同上：默认只算 pending＝后台自动会下的，for_backfill 才含 error）。
    给定一组番 id，返回它们会被挑中的种子 id 并集（供新入库一次性标将下载/备用）。
    与 download_all_pending 同一挑选口径（锁定源过滤 + 跳过已下/在下集 + 只算正集、每集一份），只算不下。
    把逐番 N 次查询压成【3 次】：番元数据一条、候选种子一条、"已有一份"的去重键一条
    （后两条【刻意分开】，理由见 _plans_for_ids 里的注释）。等价于对每个 id 调 download_plan 求并。"""
    return _plans_for_ids(anime_ids, (for_backfill,))[for_backfill]


def _plans_for_ids(anime_ids, modes: tuple) -> dict:
    """download_plan_for_ids / download_plans_for_ids 的共同实现：一次取数，算出 modes 里每种口径的计划。
    modes 是一串 for_backfill 取值（(False,) / (True,) / (False, True)）。返回 {for_backfill: 计划集合}。"""
    empty = {m: set() for m in modes}
    ids = {i for i in anime_ids if i}
    if not ids:
        return empty
    with get_session() as s:
        animes = list(s.exec(select(Anime).where(  # 排除已忽略/已完结停订：与执行侧同判据，见 download_plan
            Anime.id.in_(ids), Anime.rejected.is_not(True), *_finished_where())))
        pref_map = {a.id: a.pref_source for a in animes}
        kw_map = {a.id: a.pref_keyword for a in animes}
        amb_map = {a.id: ambiguous_range(a) for a in animes}   # 歧义段口径，与逐番版一致
        ids = set(pref_map)      # 只保留过滤后仍在的番——否则被排除的番，其种子仍会被下面计划进去
        if not ids:
            return empty
        # 【两条查询，不是一条】候选半边要完整的 8 列（要排序、要过滤、要拿 id），
        # 而"已有一份"半边只用来算去重键，只需要 3 列 —— 而它恰恰是数据量的大头：
        # sent 是只增不减的终态，跑一年的库里它比 pending 多一两个数量级。
        # 一条查询把两边一起取回来，等于每次刷新仪表盘都把整张种子历史用 8 列实例化进内存。
        # 实测 18000 行库：合并取回 174ms，拆开 + distinct 后 115ms；把 have 判据下推成
        # 【不要试图把 have 判据下推成 NOT EXISTS】原注释写着"还能再降到个位数（留作下一步）"，
        # 实测【比现状慢】：24.8ms → 35.4ms（20k 种子库，且已额外建了理想覆盖索引并确认走了它）。
        # 留着那句话会诱导下一轮做一次负优化。
        # 取所有口径用到的候选状态的并集，各口径再在内存里按自己的判据筛。
        cand_statuses = tuple(set().union(*(
            set(DOWNLOADABLE_STATUSES if m else ("pending",)) for m in modes)))
        rows = list(s.exec(select(*_PLAN_COLS).where(
            AnimeTorrent.anime_id.in_(ids), AnimeTorrent.status.in_(cand_statuses))))
        have_rows = list(s.exec(select(
            AnimeTorrent.anime_id, AnimeTorrent.episode, AnimeTorrent.source).where(
            AnimeTorrent.anime_id.in_(ids),
            AnimeTorrent.status.in_(HAVE_STATUSES)).distinct()))
    _plan_now = datetime.now()
    by_anime: dict = {}
    have_by_anime: dict = {}
    for t in rows:
        by_anime.setdefault(t.anime_id, []).append(t)
    for aid_, ep_, src_ in have_rows:     # 已有一份(downloading/stalled)；deleted 不算，同集新 hash 照常下
        have_by_anime.setdefault(aid_, set()).add(dedup_key(amb_map.get(aid_), 0, ep_, src_)[1:])
    out = {m: set() for m in modes}
    for aid, all_cands in by_anime.items():
        lock, kw = pref_map.get(aid), kw_map.get(aid)
        if lock:
            all_cands = [t for t in all_cands if lock == (t.source or "")]
        if kw:
            all_cands = [t for t in all_cands if _kw_match(kw, t.raw_title)]
        for m in modes:
            keep = DOWNLOADABLE_STATUSES if m else ("pending",)
            cands = [t for t in all_cands
                     if t.status in keep and (m or _retry_ready(t, _plan_now))]  # 见 _retry_ready
            out[m] |= {c[0].id for c in _download_candidates(cands, lock, have_by_anime.get(aid),
                                                             amb_map.get(aid))}
    return out


def pending_breakdown() -> dict:
    """把『待下』(pending)拆成 将下载/备用/待确认/未知，供仪表盘种子状态区看清那一大坨到底是什么。
    · 未知   = 特别篇(-1) 与 批量/未知集(-2)——后台一律不自动下（见 auto_downloadable_ep），需人工在详情页
              对准那一条点『下载』。【最先判】不论番确不确认，与 unknown_episode_rows（点开的列表）同口径，
              卡片数=列表条数——否则特别篇会掉进『备用项』，而那张卡的说明是『同集已有更优版本』，纯属误导。
    · 将下载 = 已确认番·本集首选（download_plan 会挑中，会自动下；只有正集会被标）
    · 备用   = 已确认番·同集已有更优版本（不会自动下）；含已拒绝番/孤儿的残留
    · 待确认 = 番还没确认（要点确认才下）；集号 -2 的已归入『未知』，不重复计
    · 已完结 = 番已判完结且开了停订（不会自动下）——【必须单列】，否则它们会掉进『备用项』，
              而那张卡的说明是"同集已有更优版本"，纯属误导。
    五者之和 = 待下总数。复用批量 download_plan_for_ids，仅几条查询、只在仪表盘打开时算。"""
    with get_session() as s:
        conf = {aid: (c, fin) for aid, c, fin in s.exec(
            select(Anime.id, Anime.confirmed, Anime.finished_at).where(Anime.rejected.is_not(True)))}
        pend = list(s.exec(select(AnimeTorrent.id, AnimeTorrent.anime_id, AnimeTorrent.episode)
                           .where(AnimeTorrent.status == "pending")))
    plan = download_plan_for_ids({aid for aid, (c, _) in conf.items() if c})
    will = backup = unconfirmed = unknown = finished = 0
    unsub = config.ANIME_FINISH_UNSUB
    for tid, aid, ep in pend:
        row = conf.get(aid)
        c = row[0] if row else None
        if not auto_downloadable_ep(ep):   # 特别篇/未知集最先判：不论番确不确认，后台都不自动下、都得人工处理。
            unknown += 1         # 放最前才与点开的列表(unknown_episode_rows)同口径，卡片数=列表条数。
        elif row is None:        # 番已拒绝/孤儿 → 不会自动下
            backup += 1
        elif not c:              # 番未确认
            unconfirmed += 1
        elif unsub and row[1] is not None:   # 已完结且开了停订
            finished += 1
        elif tid in plan:        # 已确认·本集首选（只有正集进得了 plan）
            will += 1
        else:                    # 已确认·非首选（同集有更优）
            backup += 1
    return {"will": will, "backup": backup, "unconfirmed": unconfirmed,
            "unknown": unknown, "finished": finished}


def _torrent_rows(*where) -> list[dict]:
    """按条件取 TV 种子并解析番名，供 KPI 卡点开的列表弹窗（未知集/失败等）复用。"""
    with get_session() as s:
        ts = list(s.exec(select(AnimeTorrent).where(*where)
                         .order_by(AnimeTorrent.created_at.desc())))
        ids = {t.anime_id for t in ts if t.anime_id}
        names = ({a.id: display_of(a) for a in
                  s.exec(select(Anime).where(Anime.id.in_(ids)))} if ids else {})
    return [{
        "id": t.id,
        "anime_id": t.anime_id,
        "name": names.get(t.anime_id) or (t.anime_title or "?"),
        "raw": t.raw_title or "",
    } for t in ts]


def unknown_episode_rows() -> list[dict]:
    """待下里 episode==-2（批量/无法解析集号，flush 不自动下）的种子，供 KPI『未知集』点开手动处理。"""
    return _torrent_rows(AnimeTorrent.status == "pending", AnimeTorrent.episode < 0)


def failed_rows() -> list[dict]:
    """status∈{error, stalled}（下载失败过 / 长期停滞的异常）的种子，供 KPI『失败』点开查看 / 进详情处理。"""
    return _torrent_rows(AnimeTorrent.status.in_(["error", "stalled"]))


def _terminal_torrent_rows(status: str) -> list[dict]:
    """某终态(deleted/excluded)的种子行（番名/集号/原名），供『已忽略』页底部折叠展示。"""
    with get_session() as s:
        ts = list(s.exec(select(AnimeTorrent).where(AnimeTorrent.status == status)
                         .order_by(AnimeTorrent.created_at.desc())))
        ids = {t.anime_id for t in ts if t.anime_id}
        names = ({a.id: display_of(a) for a in
                  s.exec(select(Anime).where(Anime.id.in_(ids)))} if ids else {})
    return [{"id": t.id, "anime_id": t.anime_id,
             "name": names.get(t.anime_id) or (t.anime_title or "?"),
             "episode": t.episode, "raw": t.raw_title or ""} for t in ts]


def deleted_torrent_rows() -> list[dict]:
    """已删除(deleted)的种子——『已删除种子』折叠 + 重新下载找回（deleted 是删文件的终态、不自动重下）。"""
    return _terminal_torrent_rows("deleted")


def excluded_torrent_rows() -> list[dict]:
    """已排除(excluded)的种子——『已排除种子』折叠 + 恢复放回待下（excluded 是主动排除的待下终态）。"""
    return _terminal_torrent_rows("excluded")


def set_torrent_episode(torrent_id: int, episode: float) -> bool:
    """手动改一条种子的集号——把 -2 未知集 / 误判集号救回正常集，让它进正常下载+去重流程。
    只动未下载的(pending/error)；改完仍是待下，由 flush / 补下本番按新集号处理。返回是否改了。"""
    with get_session() as s:
        t = s.get(AnimeTorrent, torrent_id)
        if t is None or t.status not in DOWNLOADABLE_STATUSES:
            return False
        t.episode = episode
        s.add(t)
        s.commit()
    return True


def exclude_torrent(torrent_id: int) -> bool:
    """排除一条不想要的待下种子（实现见 engine.exclude_torrent，两条线共用）。
    flush/补下永不再挑、restore 不复活、RSS 再遇到同 hash 也不重收；可用 unexclude_torrent 撤销。"""
    return engine.exclude_torrent(AnimeTorrent, torrent_id)


def unexclude_torrent(torrent_id: int) -> bool:
    """取消排除：放回 pending，重新参与下载/去重。返回是否放回了。"""
    return engine.unexclude_torrent(AnimeTorrent, torrent_id)


async def download_all_pending() -> int:
    """补下所有『已订阅且已确认』番剧的待下/失败种子。返回触发数。

    只补【正集】、每集一份（特别篇/未知集不在其列，见 auto_downloadable_ep）。
    """
    if not await qb_precheck():   # 预检理由同 flush_ready_downloads
        return 0
    with get_session() as s:
        auto = list(s.exec(select(Anime).where(  # noqa: E712
            *subscribed_where())))
        amb_map = {a.id: ambiguous_range(a) for a in auto}   # 歧义段口径，与逐番版一致
        pref_map = {a.id: a.pref_source for a in auto}
        kw_map = {a.id: a.pref_keyword for a in auto}
        auto_ids = set(pref_map)
        rows = list(s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id.in_(auto_ids)))) if auto_ids else []
    by_anime: dict = {}
    have_by_anime: dict = {}
    for t in rows:
        if t.status in DOWNLOADABLE_STATUSES:
            by_anime.setdefault(t.anime_id, []).append(t)
        elif t.status in HAVE_STATUSES:   # 已有一份(downloading/stalled)；deleted 不算，同集新 hash 照常下
            have_by_anime.setdefault(t.anime_id, set()).add(
                dedup_key(amb_map.get(t.anime_id), 0, t.episode, t.source)[1:])
    n = 0
    for aid, pending in by_anime.items():
        lock = pref_map.get(aid)
        if lock:  # 锁定源：只补锁定组
            pending = [t for t in pending if lock == (t.source or "")]
        kw = kw_map.get(aid)
        if kw:  # 版本关键词：再过滤到命中该版本的
            pending = [t for t in pending if _kw_match(kw, t.raw_title)]
        for cands in _download_candidates(pending, lock, have_by_anime.get(aid), amb_map.get(aid)):
            for t in cands:      # 同 download_pending_for_anime：逐条试到成功为止
                ok = await download_anime_torrent(t.id)
                if ok:
                    n += 1
                    break
                if ok is None:
                    return n     # 系统性失败：整个批量收手，理由同 download_pending_for_anime
    return n


def _norm_name(s: str) -> str:
    """归一化番名做集合比对：去所有空白 + 小写（罗马音大小写不敏感）。"""
    return re.sub(r"\s+", "", (s or "")).lower()


async def backfill_source(anime_id: int, name_filter: bool = False) -> dict:
    """『补齐该源』/『自动补齐』：去 nyaa/Mikan 按名搜『该源』的种子，把漏收的补进这部番。

    该源 = 锁定源(pref_source)；没锁则取该番最高优先级的源。搜到的按 hash 去重、【季号过滤】（挡 S1/S2 混淆），
    name_filter=True(『自动补齐』按钮) 再加【番名近似过滤】挡同名衍生作；新的入库 pending 且把番置 confirmed=False（复用待确认
    审核，不自动下——交给用户点『确认下载』）。返回 {found, kept, ingested, sites}。已忽略(rejected)的番不改订阅态。"""
    from sources import SEARCH_URL, SOURCES

    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None:
            return {"found": 0, "kept": 0, "ingested": 0, "sites": [], "error": "番不存在"}
        rows = list(s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == anime_id)))
        pref, quarter = a.pref_source, a.quarter
        jp, dn, title = a.jp_name, a.display_name, a.title

    # 目标源(组名)+站点：锁定源→只补该源；没锁→补最高优先级的源（Mikan 群组同优先级即并列全取）
    tors = [(t.source, t.site, t.priority or 0) for t in rows if t.source]
    if pref:
        targets = [(src, site) for src, site, _ in tors if pref == (src or "")]
        target_pri = max((pr for src, _, pr in tors if pref == (src or "")), default=0)
    else:
        maxpri = max((pr for _, _, pr in tors), default=0)
        targets = [(src, site) for src, site, pr in tors if pr == maxpri]
        target_pri = maxpri
    # 【补进来的种子要带上该源本来的优先级】否则一律落 priority=0：pick_best 按优先级降序挑，
    # 补齐回来的高优先级源（如 ANi=100）会输给库里任何一条已有的低优先级待下，
    # 于是"补齐该源"补是补到了，真正被下的还是别人——功能等于白做。
    site_groups: dict = {}
    for src, site in targets:
        site_groups.setdefault(site, set()).add(src)
    if not site_groups:
        return {"found": 0, "kept": 0, "ingested": 0, "sites": [], "error": "该番还没有任何来源，无法判断去哪搜"}

    # 搜索名：【先用该源自己的种子标题提取】(search_query_names：剥集号、全括号命名走块级兜底)——补齐要找的
    # 正是这个组的发布，用它自己的写法最容易命中；bgm 规范名(jp/dn)常带罗马数字/日文副标题，在种子站往往
    # 一条都搜不到，故退到后面做保底。取该源最近 3 条种子（同组各集名字基本一样，去重后就一两个；多取一点
    # 是为了兜住组中途改名）。
    tset = set(targets)
    src_rows = sorted((t for t in rows if (t.source, t.site) in tset),
                      key=lambda t: t.created_at, reverse=True)
    names: list[str] = []
    for t in src_rows[:3]:
        for n in search_query_names(t.raw_title):
            if n not in names:
                names.append(n)
    for n in (jp, dn):                    # bgm 规范名（先日/中，再罗马音）殿后保底
        if n and n not in names:
            names.append(n)
    # 季号过滤基准：用本番种子【实际解析出的季号】而非 bgm 纠正后的 a.season——否则锁定源的续季番若种子标题
    # 无季标记(解析成 season=1)，会与 a.season=2 全对不上而假阴、补齐永远搜不到。
    existing_seasons = {t.season for t in rows}
    queries = [n for n in names if len(n.replace(" ", "")) >= 2][:5]   # 限 5 个查询(×站点数=请求数)，别打太多
    if not queries:
        return {"found": 0, "kept": 0, "ingested": 0, "sites": [], "error": "没有可搜索的番名"}

    # 抓取：按站构造搜索源，复用 RssSource._parse（含组名白名单/合集过滤/hash 校验）。
    # 各 (site × 查询名) 并发抓，墙钟=最慢一次而非累加，避免最坏 ~8×30s 串行阻塞几分钟。
    # 【查表而不是 if/elif】以前这里是本文件的"唯一分站处"，漏改的后果是静默 return []：
    # 详情页点『补齐』永远返回 0 条，日志里一个字都没有。
    async def _fetch_one(site, groups, q):
        cls, search = SOURCES.get(site), SEARCH_URL.get(site)
        if cls is None or search is None:
            log.warning("补齐：站点 %s 没有搜索入口，跳过", site)
            return []
        try:
            src_obj = cls("补齐", search(q), priority=target_pri,
                          subgroups=list(groups), title_filter=[])
            return await src_obj.fetch()
        except Exception as e:
            log.warning("补齐搜索失败 site=%s q=%s: %s", site, q, e)
            return []

    tasks = [_fetch_one(site, groups, q) for site, groups in site_groups.items() for q in queries]
    found: dict = {}
    for items in await asyncio.gather(*tasks):
        for it in items:
            found.setdefault(it.info_hash, it)

    # 过滤：季号一致（挡 S1/S2 混淆）+ name_filter 番名近似（挡同名衍生作/恶搞）
    # ref_names 仅该分支用到 → 只在开了番名过滤时构建（含逐条 _norm_name 正则），否则不白算
    ref_names = ({_norm_name(x) for x in (jp, dn, title) if x}
                 | {_norm_name(t.anime_title) for t in rows if t.anime_title}) if name_filter else set()
    kept = []
    for it in found.values():
        if it.season not in existing_seasons:
            continue
        if name_filter:
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
        stolen = 0
        for it in kept:
            if s.exec(select(AnimeTorrent).where(AnimeTorrent.info_hash == it.info_hash)).first():
                continue
            # 【绝不抢别的番的种子】站内搜索是词级 AND，同一字幕组的续作/剧场版/衍生作很容易一起命中，
            # 而这里是拿 anime_id 硬挂入库、不查对照表。一旦挂错，这条 hash 就被本番占死：真正属于
            # 它的那部番之后走 process_item 会在 hash 去重处静默 return False，永远收不到它。
            # 对照表已明确指向【另一部番】的条目一律跳过。这与两个补齐按钮的松紧无关（那是"要不要再做
            # 番名近似"的取舍），纯粹是归属正确性，两边都该守。
            owner = s.exec(select(AnimeAlias).where(       # 同 process_item：键要按列长截断
                AnimeAlias.title == alias_key(it.anime_title),
                AnimeAlias.season == it.season)).first()
            if owner is not None and owner.anime_id != anime_id:
                stolen += 1
                continue
            s.add(AnimeTorrent(
                info_hash=it.info_hash, anime_id=anime_id, source=it.source, site=it.site,
                anime_title=it.anime_title, raw_title=it.raw_title, season=it.season,
                # 与 process_item 同口径做跨源集号归一，否则补齐进来的绝对编号种子会绕过它
                episode=_learn_and_normalize_episode(s, a_now, it), quarter=quarter or it.quarter,
                download_url=it.download_url, release_time=it.release_time,
                priority=it.priority, status="pending"))
            try:
                s.commit()
                ingested += 1
                _warn_unknown_episode(it)   # 补齐是第二个入库口，不走 process_item
            except IntegrityError:
                s.rollback()
        if ingested and not a_now.rejected:   # 有新货且未忽略(实时态) → 转待确认，复用审核流、别自动下
            a_now.confirmed = False
            s.add(a_now)
            s.commit()
            to_confirm = True
    log.info("补齐 anime=%s 番名过滤=%s：搜到 %d（站 %s）→ 留 %d → 入库 %d%s",
             anime_id, name_filter, len(found), list(site_groups), len(kept), ingested,
             f"（另跳过 {stolen} 条：对照表显示属于别的番）" if stolen else "")
    return {"found": len(found), "kept": len(kept), "ingested": ingested, "stolen": stolen,
            "sites": list(site_groups), "to_confirm": to_confirm}


async def sync_qb_status(manual: bool = False) -> int:
    """从 qB 同步 TV 种子实时态（剧场版走 movies.sync_qb_status）。"""
    return await engine.sync_qb_status(AnimeTorrent, manual=manual)


def anime_save_path(anime_id: int) -> str | None:
    """该番当前的归档目录（build_save_path 结果：[子目录]/[季度]/番名/[Season N]）；算不出返回 None。

    与 download_anime_torrent 【同口径】：都走 _anime_path_parts(a, 最新种子行)。缺 bgm 元数据(未识别)时
    两处都回退到种子解析名，故显示/relocate 目标与实际落地一致（B2）。
    """
    with get_session() as s:
        a = s.get(Anime, anime_id)
        if a is None:
            return None
        t = s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == anime_id)
                   .order_by(AnimeTorrent.created_at.desc())).first()   # 最新种子行，供缺元数据时回退，同下载口径
        quarter, folder, season = _anime_path_parts(a, t)
    return engine.build_save_path(quarter, folder, season=season,
                                  sub_dir=config.ANIME_DOWN_PATH)


async def relocate_anime(anime_id: int, old_path: str | None = None) -> dict:
    """把该番已下/在下/停滞的种子移到当前归档目录（改季度/重绑后调用；调用方应已落新 a.quarter/名/季号）。
    实现见 engine.relocate（两条线共用）。返回 {new_path, old_path, moved, redownload, untracked,
    failed, stalled_kept, fail_code?, error?}。"""
    return await engine.relocate(AnimeTorrent, AnimeTorrent.anime_id, anime_id,
                                 anime_save_path(anime_id), old_path, noun="集")


async def delete_anime_torrent(torrent_id: int) -> bool:
    """删除单条种子在 qB 里的文件（走 qB 接口），标记为 deleted。详情页按集删用。

    deleted 是用户主动删除的终态：恢复订阅时不会被重新下（区别于集去重落选、可复活的 skipped）。
    若同一 hash 剧场版管线还在用，则只脱手本行、不删 qB/文件，免得毁了对面。

    已归档(archived_at)的：种子早已从 qB 移除，qB 代删不到文件 → 只落 deleted 终态，
    硬盘文件留在 save_path 由用户自行清理（UI 在确认框里把该路径显示出来）。
    """
    with get_session() as s:
        t = s.get(AnimeTorrent, torrent_id)
        # 【交付中(downloading)的行不能删】它只是"已挑中、正在 fetch+add"的瞬时占位，qB 里还没有这个 hash：
        # qb.delete 打在不存在的 hash 上照样回 200（qB 的语义），于是这行被标 deleted、
        # 而交付协程稍后无条件写回 sent + save_path，删除被静默撤销、文件照常落盘；
        # 更糟的时序里会停在 deleted 而种子仍在 qB 下载——脱离 TRACKED 后 sync 再也不看它，
        # 文件在盘上却没有任何 UI 入口能删。与 relocate(engine.py:delivering)、
        # sync_qb_status 对交付中占位行的处理同口径：交付协程独占该行，旁人让路。
        if t is None or t.status not in HAVE_STATUSES or t.status == "downloading":
            return False  # stalled 也允许删；downloading 是交付中的占位，见上
        h = t.info_hash
        if t.archived_at is not None:      # 已归档：不在 qB，删不到文件，只改状态
            t.status = "deleted"
            s.add(t)
            s.commit()
            log.info("删除已归档条目（仅标记，文件留在 %s）- torrent=%s", t.save_path or "?", torrent_id)
            return True
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

    已归档的一并计入并标 deleted（与逐条删同口径），但它们早已不在 qB、代删不到文件——
    只改状态，文件留在各自 save_path 由用户自行清理（UI 负责提示）。
    """
    with get_session() as s:
        rows = list(s.exec(select(AnimeTorrent).where(
            AnimeTorrent.anime_id == anime_id,
            AnimeTorrent.status.in_(HAVE_STATUSES),   # 含停滞异常，一并清
            AnimeTorrent.status != "downloading",      # 交付中的占位行让路（理由见 delete_anime_torrent）
        )))
        pairs = [(t.id, t.info_hash) for t in rows]
        archived_paths = [t.save_path for t in rows if t.archived_at is not None]
        live = [(t.id, t.info_hash) for t in rows if t.archived_at is None]
    if not pairs:
        return 0
    # 只对【还在 qB 里】且未被剧场版共享的 hash 真删文件；已归档的不进这批（qB 里已无，删了也白删）
    exclusive = [h for _, h in live if not engine.hash_owned_elsewhere(h, MovieTorrent)]
    if exclusive and not await engine.qb.delete(exclusive, delete_files=True):
        return 0
    with get_session() as s:
        for tid, _ in pairs:
            t = s.get(AnimeTorrent, tid)
            if t is not None:
                t.status = "deleted"  # 用户主动删除，终态；恢复订阅时不重下（区别集去重的 skipped）
                s.add(t)
        s.commit()
    log.info("删除文件 - anime=%s 共 %d 个种子（独占 %d 个删文件；已归档 %d 个只标状态，文件留 %s）",
             anime_id, len(pairs), len(exclusive), len(archived_paths),
             "/".join(sorted({p for p in archived_paths if p})) or "下载目录")
    return len(pairs)


# ---------------- 源组（字幕组）管理 ----------------

def list_source_groups(enabled_only: bool = False) -> list[SourceGroup]:
    with get_session() as s:
        q = select(SourceGroup)
        if enabled_only:
            q = q.where(SourceGroup.enabled == True)  # noqa: E712
        return list(s.exec(q.order_by(SourceGroup.priority.desc(), SourceGroup.id)))


def add_source_group(name, site, feed, policy, priority, enabled=True,
                     subgroups="", title_filter="") -> bool:
    """新增源组。写不进去返回 False（调用方据此提示）——不捕获的话异常会逃到 NiceGUI 的事件
    处理器里只落日志，用户侧完全静默，看上去就是『点了没反应』。

    捕获 DatabaseError 而不只是 IntegrityError：撞唯一约束（重名）是 IntegrityError，而 MySQL 侧
    把超长字符串写进 VARCHAR(191) 抛的是【DataError】（同为 DatabaseError 的兄弟类）。只接前者的话，
    用户粘一条很长的 feed URL / 一大串字幕组白名单，在 SQLite 上能存、切到 MySQL 就静默失败。
    """
    with get_session() as s:
        s.add(SourceGroup(name=name, site=site, feed=feed, policy=policy,
                          priority=int(priority), enabled=enabled, subgroups=subgroups,
                          title_filter=title_filter))
        try:
            s.commit()
        except DatabaseError as e:
            s.rollback()
            log.warning("新增源组失败 name=%s: %s", name, str(e).splitlines()[0][:160])
            return False
    return True


def update_source_group(gid: int, **fields) -> bool:
    """改源组。写不进去返回 False，捕获范围与理由同 add_source_group。"""
    with get_session() as s:
        g = s.get(SourceGroup, gid)
        if g is None:
            return False
        for k, v in fields.items():
            setattr(g, k, v)
        s.add(g)
        try:
            s.commit()
        except DatabaseError as e:
            s.rollback()
            log.warning("保存源组失败 gid=%s: %s", gid, str(e).splitlines()[0][:160])
            return False
    return True


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
                          subgroups="LoliHouse"))   # 默认给 Mikan 白名单 LoliHouse 字幕组
        s.commit()
