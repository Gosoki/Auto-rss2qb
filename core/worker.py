"""后台轮询器：常驻协程，每 ANIME_POLL_INTERVAL 秒抓一次所有源并处理。

源不再写死——每轮从 DB 的 SourceGroup 表重建（在 UI 改了组，下一轮就生效）。
抓取入库后，统一由 flush_ready_downloads 按『缓冲窗口 + 优先级』决定下哪些。
"""
import asyncio
import logging

from core import anime
import config
from core import engine, movies
import db
from core.anime import flush_ready_downloads, list_source_groups, process_item
from sources.mikan import MikanSource
from sources.nyaa import NyaaSource, nyaa_feed_url

log = logging.getLogger("autorss")


def build_sources() -> list:
    """据 DB 里启用的源组构建本轮的源实例（按优先级从高到低）。"""
    srcs = []
    for g in list_source_groups(enabled_only=True):
        subs = [x.strip() for x in (g.subgroups or "").split(",") if x.strip()]
        tfilter = [x.strip() for x in (g.title_filter or "").split(",") if x.strip()]
        if g.site == "nyaa":
            srcs.append(NyaaSource(g.name, nyaa_feed_url(g.feed), g.policy, g.priority, subs, tfilter))
        elif g.site == "mikan":
            srcs.append(MikanSource(g.name, g.feed, g.policy, g.priority, subs, tfilter))
        else:
            log.warning("未知源类型 %s（组 %s），跳过", g.site, g.name)
    return srcs


async def poll_once() -> None:
    for source in build_sources():
        try:
            items = await source.fetch()
        except Exception as e:
            log.error("抓取失败 %s: %s", source.name, e)
            continue
        new = 0
        for item in items:
            try:
                if await process_item(item):
                    new += 1
            except Exception as e:
                log.error("处理失败 %s: %s", getattr(item, "anime_title", "?"), e)
        log.info("源 %s：%d 条，新增 %d", source.name, len(items), new)

    try:
        n = await flush_ready_downloads()
        if n:
            log.info("缓冲窗口放行下载 %d 集", n)
    except Exception as e:
        log.error("放行下载异常: %s", e)


def init_business_state() -> None:
    """业务库可用之后必做的一次性初始化。三个操作都幂等，重复调用无害。

    启动时库是通的就直接调；【停摆着启动】则由 run_db_watch 探到库回来后补调——
    否则这些复位永远不做，上次遗留的 downloading 会一直卡着。
    """
    anime.seed_source_groups()          # 首启种入 ANi/Mikan 两个源组
    anime.reset_downloading()           # 复位上次遗留的 downloading（TV）
    movies.reset_downloading()          # 复位上次遗留的 downloading（剧场版）
    engine.backfill_legacy_progress_once()   # 一次性：历史 sent 标记为已完成


async def run_db_watch() -> None:
    """业务库健康看守：每 30s 探一次。

    连不上 → 标停摆，各后台循环自己跳过本轮、页面显示明确提示（不回退本地库，理由见 db 模块）。
    恢复   → 自动补跑迁移 + 补跑 init_business_state，不必重启。
    探测本身极便宜（一条 SELECT 1），日志只在状态变化时由 probe_data_engine 记一行。
    """
    was_down = bool(db.data_down())
    while True:
        try:
            now_down = bool(db.probe_data_engine())
            if was_down and not now_down:
                init_business_state()   # 停摆期间漏掉的初始化，这会儿补上
            was_down = now_down
        except Exception as e:          # 探测自己出岔子也别让看守协程死掉，否则永远发现不了恢复
            log.error("数据库看守异常: %s", e)
        await asyncio.sleep(30)


async def run_worker() -> None:
    log.info("轮询器启动（采集%s），每 %d 秒一轮",
             "开" if config.ANIME_POLL_ENABLED else "关·在设置页开启", config.ANIME_POLL_INTERVAL)
    while True:
        if not config.ANIME_POLL_ENABLED or db.data_down():
            await asyncio.sleep(15)  # 暂停中/数据库停摆：短睡轮询开关，恢复后约 15s 内继续
            continue
        try:
            await poll_once()
        except Exception as e:
            log.error("本轮异常: %s", e)
        await asyncio.sleep(max(60, config.ANIME_POLL_INTERVAL))  # 每轮读当前值；下限 60s 兜底，防坏值(0/负)忙循环


async def run_reenrich_retry() -> None:
    """独立协程：按指数退避对『待识别』番重跑 bgm 识别（到点判定与翻倍在 anime.retry_unmatched）。

    刻意独立于主采集轮询——重试是串行 bgm 请求，放主循环里会拖住 poll_once/下载放行。采集暂停时也暂停重试。
    以『检查节拍』周期性醒来看谁到点（节拍 ≤ 基准等待、封顶 10 分钟），到点的才真打 bgm。
    """
    log.info("待识别重试协程启动（指数退避：基准 %d 分、翻倍封顶 %d 分、最多 %d 次）",
             config.REENRICH_RETRY_BASE, config.REENRICH_RETRY_MAX, config.REENRICH_MAX_TRIES)
    while True:
        await asyncio.sleep(max(60, min(config.REENRICH_RETRY_BASE * 60, 600)))  # 检查节拍(秒)：≤基准、封顶10分；先睡后查
        if not config.ANIME_POLL_ENABLED or db.data_down():
            continue                # 采集暂停 / 数据库停摆 → 重试也暂停
        try:
            await anime.retry_unmatched()
        except Exception as e:
            log.error("延迟重识别异常: %s", e)


async def run_movie_scan() -> None:
    """独立协程：按 MOVIE_SCAN_INTERVAL 自动扫描 Mikan 当年剧场版/OVA（开关在 /movies 订阅源）。

    每 5 分钟心跳一次，是否真扫由 movies.auto_scan_tick 按『距上次扫描的间隔』判（跨重启也不会误重扫）。
    只碰剧场版，与 TV 采集互不相干。
    """
    log.info("剧场版自动扫描协程启动（%s，每 %d 秒）",
             "开" if config.MOVIE_SCAN_ENABLED else "关·在 /movies 订阅源开启", config.MOVIE_SCAN_INTERVAL)
    while True:
        try:
            if not db.data_down() and await movies.auto_scan_tick():
                log.info("剧场版自动扫描完成")
        except Exception as e:
            log.error("剧场版自动扫描异常: %s", e)
        await asyncio.sleep(300)  # 5 分钟心跳，到点才真扫


async def run_qb_sync() -> None:
    """qB 状态同步：事件驱动 + 保底自查。

    平时停在 qb_kick 上休眠（0 开销）；有种子交付给 qB 时被 kick 立即醒来，按活跃间隔轮询这批『在下的』，
    全下完就回去休眠。另设保底超时（QB_SYNC_BACKSTOP_MIN 分钟）——即便漏了 kick / 重启 / qB 开关切换，也每隔
    这么久醒来自查一次、兜住漏网的在下种子。快路径管跟手、慢路径管最终一致，且种子在 qB 里照下不受影响。
    """
    log.info("qB 状态同步启动（事件驱动，活跃间隔 %ds，保底 %d 分钟）",
             config.QB_SYNC_INTERVAL, config.QB_SYNC_BACKSTOP_MIN)
    try:
        if not db.data_down() and engine.has_inflight():
            engine.qb_kick.set()      # 启动即自查：接上重启前遗留的『在下的』种子
    except Exception as e:            # 停摆时直接跳过，别白查一次库再把异常记成噪声
        log.error("qB 同步启动自查异常（忽略，靠保底兜住）: %s", e)
    while True:
        # 三档节奏：① 高频轮询在下面内层 while（有活跃下载，每 QB_SYNC_INTERVAL 秒）；② 还有没下完的在下种子
        # 但都不活跃(慢/stalled/暂停) → 每 QB_IDLE_RECHECK_MIN 分钟自查一次，别等一个保底周期才发现完成；
        # ③ 全无在下 → 睡到保底 QB_SYNC_BACKSTOP_MIN。任一 kick 立即打断醒来。
        try:
            has_unfinished = engine.has_inflight()
        except Exception:
            has_unfinished = True      # 拿不准(DB 锁等) → 用中档短超时，宁可多查一次
        wait_min = config.QB_IDLE_RECHECK_MIN if has_unfinished else config.QB_SYNC_BACKSTOP_MIN
        try:
            await asyncio.wait_for(engine.qb_kick.wait(), timeout=max(60, wait_min * 60))
        except asyncio.TimeoutError:
            pass                       # 到点：没人 kick 也醒来自查一遍
        engine.qb_kick.clear()
        try:
            await engine.archive_old_completed()   # 顺手做完成归档（完成超 N 天→从 qB 移除留文件、标已归档；关则空转）
        except Exception as e:
            log.error("完成归档异常（忽略，下轮再来）: %s", e)
        idle = 0                            # 连续几轮没在真下（局部计数，本次唤醒周期内累加、下次唤醒清零，无需入库）
        try:
            while (config.QB_ENABLED and config.QB_SYNC_STATUS
                   and not db.data_down() and engine.has_inflight()):
                try:
                    await anime.sync_qb_status()   # 每轮批量刷新所有在下的：有活种子时慢的/stalled 的也顺便一起更新
                    await movies.sync_qb_status()
                except Exception as e:
                    log.error("qB 状态同步异常: %s", e)
                if engine.has_active_downloading():
                    idle = 0
                else:
                    idle += 1
                    if idle >= max(1, config.QB_SLOW_ROUNDS):
                        break   # 连续 N 轮没一个在真下(全 stalled/排队/慢速爬行) → 退出高频轮询，回等 kick/保底、休眠
                await asyncio.sleep(max(5, config.QB_SYNC_INTERVAL))
        except Exception as e:
            # has_inflight()/has_active_downloading() 若因 DB 锁等抛错，别让它掀翻 while True（否则 qB 同步永久死掉）
            log.error("qB 同步内层循环异常（回退休眠，等下次 kick/保底）: %s", e)


async def reactivate_all() -> None:
    """手动『重新激活全部任务』（设置页按钮）：立刻跑一轮 = 抓所有源入库 → 放行到点的下载发往 qB
    → 按需扫剧场版 → 唤醒 qB 状态检查（顺带完成归档）。等价于重启服务后各协程立刻做的那一轮，但不重启进程。

    采集暂停时跳过抓源（与 run_worker 同口径，暂停就是暂停），qB 检查照做。
    刻意【不】做 reset_downloading：那是启动时清上次异常退出的残留，运行中 status=downloading 的都是真在下的
    种子，复位成 pending 会让 flush 认为该集『还没有』而另挑一个源重下一份。
    """
    if config.ANIME_POLL_ENABLED:
        await poll_once()
    else:
        log.info("重新激活：采集处于暂停，跳过抓源")
    try:
        if await movies.auto_scan_tick():
            log.info("重新激活：剧场版自动扫描完成")
    except Exception as e:
        log.error("重新激活：剧场版扫描异常: %s", e)
    engine.qb_kick.set()      # 唤醒 qB 状态同步：刷新在下种子的实时态 + 完成归档
    log.info("重新激活全部任务：完成")
