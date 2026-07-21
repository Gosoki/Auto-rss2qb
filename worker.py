"""后台轮询器：常驻协程，每 POLL_INTERVAL 秒抓一次所有源并处理。

源不再写死——每轮从 DB 的 SourceGroup 表重建（在 UI 改了组，下一轮就生效）。
抓取入库后，统一由 flush_ready_downloads 按『缓冲窗口 + 优先级』决定下哪些。
"""
import asyncio
import logging

import config
import core
import movies
from core import flush_ready_downloads, list_source_groups, process_item
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
             "开" if config.POLL_ENABLED else "关·在设置页开启", config.POLL_INTERVAL)
    while True:
        if not config.POLL_ENABLED:
            await asyncio.sleep(15)  # 暂停中：短睡轮询采集开关，打开约 15s 内生效
            continue
        try:
            await poll_once()
        except Exception as e:
            log.error("本轮异常: %s", e)
        await asyncio.sleep(config.POLL_INTERVAL)  # 每轮读当前值，改了下一轮生效


async def run_qb_sync() -> None:
    """独立协程：定期从 qB 拉取种子实时态（TV + 剧场版两张表）。比采集频率高，接近实时。"""
    log.info("qB 状态同步启动，每 %d 秒一次（QB_ENABLED 关时空转）", config.QB_SYNC_INTERVAL)
    while True:
        try:
            if config.QB_ENABLED:
                await core.sync_qb_status()
                await movies.sync_qb_status()
        except Exception as e:
            log.error("qB 状态同步异常: %s", e)
        await asyncio.sleep(max(5, config.QB_SYNC_INTERVAL))
