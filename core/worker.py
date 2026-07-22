"""后台轮询器：常驻协程，每 ANIME_POLL_INTERVAL 秒抓一次所有源并处理。

源不再写死——每轮从 DB 的 SourceGroup 表重建（在 UI 改了组，下一轮就生效）。
抓取入库后，统一由 flush_ready_downloads 按『缓冲窗口 + 优先级』决定下哪些。
"""
import asyncio
import logging

from core import anime
import config
from core import engine, movies
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


async def run_worker() -> None:
    log.info("轮询器启动（采集%s），每 %d 秒一轮",
             "开" if config.ANIME_POLL_ENABLED else "关·在设置页开启", config.ANIME_POLL_INTERVAL)
    while True:
        if not config.ANIME_POLL_ENABLED:
            await asyncio.sleep(15)  # 暂停中：短睡轮询采集开关，打开约 15s 内生效
            continue
        try:
            await poll_once()
        except Exception as e:
            log.error("本轮异常: %s", e)
        await asyncio.sleep(max(60, config.ANIME_POLL_INTERVAL))  # 每轮读当前值；下限 60s 兜底，防坏值(0/负)忙循环


async def run_movie_scan() -> None:
    """独立协程：按 MOVIE_SCAN_INTERVAL 自动扫描 Mikan 当年剧场版/OVA（开关在 /movies 订阅源）。

    每 5 分钟心跳一次，是否真扫由 movies.auto_scan_tick 按『距上次扫描的间隔』判（跨重启也不会误重扫）。
    只碰剧场版，与 TV 采集互不相干。
    """
    log.info("剧场版自动扫描协程启动（%s，每 %d 秒）",
             "开" if config.MOVIE_SCAN_ENABLED else "关·在 /movies 订阅源开启", config.MOVIE_SCAN_INTERVAL)
    while True:
        try:
            if await movies.auto_scan_tick():
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
    if engine.has_inflight():
        engine.qb_kick.set()          # 启动即自查：接上重启前遗留的『在下的』种子
    while True:
        try:
            await asyncio.wait_for(engine.qb_kick.wait(),
                                   timeout=max(60, config.QB_SYNC_BACKSTOP_MIN * 60))
        except asyncio.TimeoutError:
            pass                       # 保底到点：没人 kick 也醒来自查一遍
        engine.qb_kick.clear()
        while config.QB_ENABLED and engine.has_inflight():
            try:
                await anime.sync_qb_status()
                await movies.sync_qb_status()
            except Exception as e:
                log.error("qB 状态同步异常: %s", e)
            await asyncio.sleep(max(5, config.QB_SYNC_INTERVAL))
