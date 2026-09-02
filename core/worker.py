"""后台轮询器：常驻协程，每 ANIME_POLL_INTERVAL 秒抓一次所有源并处理。

源不再写死——每轮从 DB 的 SourceGroup 表重建（在 UI 改了组，下一轮就生效）。
抓取入库后，统一由 flush_ready_downloads 按『缓冲窗口 + 优先级』决定下哪些。
"""
import asyncio
import logging
import time

from core import anime
import config
from core import engine, movies
import db
from db.models import AnimeTorrent
from core.anime import flush_ready_downloads, list_source_groups, process_item
from services import fetch
from services.notify import state as notify_state
from sources import SOURCES
from sources.nyaa import nyaa_feed_url

log = logging.getLogger("autorss")

# 【两把独立的轮次锁】常态下后台协程各自定时跑；页面上还有几个随时可点的手动入口（设置页『重新激活
# 全部任务』、/movies『立即扫描』），它们做的是同一件事，并发跑就是把同一批请求重打一遍：
#   · 采集轮：同一批 RSS 被抓两遍、同一批条目走两遍 process_item；
#   · 剧场版整年扫描：MOVIE_SCAN_LAST 要等整轮跑完才写，扫描期间 auto_scan_tick 恒判『到点』，
#     于是两轮 Mikan+bgm 的整年请求并发打出去（有被限流的风险），且 _upsert_movie 是"先查后插"、
#     mikan_id 上没有唯一约束——两轮交错时未识别的番组会留下两行重复 Movie。
# 【必须是两把、不能合成一把】采集与剧场版扫描本就互不相干（run_movie_scan 的 docstring 明写"只碰
# 剧场版"）。合成一把的话，一次十几分钟的整年扫描会把 TV 采集连同 flush_ready_downloads 整段憋住。
# 后台协程【等】自己那把锁（本来就在定时循环里，晚一点无所谓）；手动入口【不等】，直接回报"已有一轮在跑"。
_poll_lock = asyncio.Lock()    # 采集一轮：抓所有源 + 放行到点的下载
_scan_lock = asyncio.Lock()    # 剧场版整年扫描
# 【下面这两把是 R27 补的，补的不是并发、是"在途闸看得见"】
# `engine.maintenance_blockers()` 逐条列出"跨 await 持业务库主键"的后台线，
# 而 R23 那条守卫的判据是"每一把模块级轮次锁都必须被闸看见" ——
# 判据只看得见【已经有锁】的线。归档与巡检这两条**一把锁都没有**，
# 于是它们既不在闸里、也不会被守卫报出来：约束的作用域比验证的作用域小（第②种形状）。
# 加锁之后，那条现成的守卫自动把它们纳进来，以后新增一条也一样。
#
# · 归档：`archive_old_completed` 把 (id, info_hash) 读成列表 → `await qb.delete()`
#   （单次上限 _QB_TOTAL_TIMEOUT=45 秒）→ 出 await 后 `s.get(model_cls, tid)` 按主键写回。
# · 巡检：`sweep_finished` / `sweep_idle` 拿着 `Anime.id` 去 `await notify_event()`
#   （每条上限 NOTIFY_TIMEOUT）→ 回来 `s.get(Anime, a.id)` 写 finished_at / 通知记账。
# 两条都在 `db/transfer.py` 保留主键的前提下会把回写落进【另一个库的另一行】。
_archive_lock = asyncio.Lock()   # 完成归档一轮
_sweep_lock = asyncio.Lock()     # 完结/断更/积压巡检一轮（含交付残骸清扫）


def build_sources() -> list:
    """据 DB 里启用的源组构建本轮的源实例（按优先级从高到低）。"""
    srcs = []
    for g in list_source_groups(enabled_only=True):
        subs = [x.strip() for x in (g.subgroups or "").split(",") if x.strip()]
        tfilter = [x.strip() for x in (g.title_filter or "").split(",") if x.strip()]
        cls = SOURCES.get(g.site)
        if cls is None:
            log.warning("未知源类型 %s（组 %s），跳过", g.site, g.name)
            continue
        # 【每个组各自兜住】build_sources 是在 poll_once 的 for 语句【之外】求值的，
        # 所以下面那个 per-source 的 try 够不着这里：一个组构造失败（feed 为空让
        # nyaa_feed_url raise、或将来别的校验）就会掀翻【整轮采集】——连同 flush_ready_downloads
        # 一起不跑，而日志里只有一行不含组名的"本轮异常"。
        # 同一份数据在上面那个未知 site 的分支里只是 warning 跳过，两边口径本就该一致。
        try:
            # nyaa 的 feed 可以只填用户名，要先拼成 RSS URL；mikan 的 feed 本来就是 URL。
            # 这是两个源之间唯一还需要分情况的地方，所以留在这儿而不是塞进源类。
            url = nyaa_feed_url(g.feed) if g.site == "nyaa" else g.feed
            srcs.append(cls(g.name, url, g.policy, g.priority, subs, tfilter))
        except Exception as e:
            log.error("源组『%s』配置有问题，本轮跳过它（其余源照抓）：%s", g.name, e)
    return srcs


def loop_error(what: str, e: BaseException, **kw) -> None:
    """后台循环的统一异常记账。(R21/R22)

    【为什么要收成一处】维护期（切库 / 整库迁移）里 `db.get_session()` 会抛 `DatabaseBusy`，
    而它可能落在任意一条后台循环的轮次中间 —— 那不是"异常"，是**计划内的**、
    用户自己点出来的、秒级的暂停。逐条报成 `ERROR ...异常` 会在日志页顶出一片红，
    掩盖同一时间段里真正的错误。
    本项目有 13 处这样的记账点，逐处判必然漏一处（第①号形状），所以收成一个函数。
    """
    if isinstance(e, db.DatabaseBusy):
        log.info("%s：%s —— 整库维护中，这一轮跳过，维护结束后自动继续", what, e)
        return
    log.error("%s异常: %s", what, e, **kw)


async def poll_once() -> None:
    # 【每轮只探一次 qB】传给本轮所有 process_item，让"最高优先级即时下载"在 qB 不可达时
    # 别去白取种（详见 anime.process_item 的 qb_alive 说明）。探测本身就是 flush 前那一次，
    # 顺带把状态型通知的翻转也发了，所以不额外增加请求。
    qb_alive = await anime.qb_precheck()
    for source in build_sources():
        try:
            items = await source.fetch()
        except Exception as e:
            # 【必须脱敏】httpx 自己抛的 HTTPStatusError/ConnectError 的 str() 带完整 URL，
            # 而 Mikan『我的番组』订阅地址把 token 放在 query 里 ——
            # 随便一次 403/500 就把 ?token=… 整条写进 data/autorss.log（滚动 5 份）
            # 与 /logs 页的「下载完整日志」。这里以前既不截断也不脱敏。
            log.error("抓取失败 %s: %s: %s", source.name, type(e).__name__,
                      fetch.redact(e)[:200])
            continue
        # 【本源这一批的 hash 一次查完】RSS 一轮的条目绝大多数是上一轮就见过的，
        # 逐条各开一个 session 去查等于把 100 次数据库往返摊在采集主链路上
        # （本地 SQLite 约 42ms/100 条；远程 MySQL 是每轮 0.4~2 秒的裸阻塞）。
        # 预取是在【本源开抓之前】做的，所以本轮里前一个源刚入库的 hash 已经在里面了
        # （每个源各预取一次）。缺口只有一个：同一个 feed 里出现【重复条目】——
        # 那条 hash 预取时不存在、第一次处理后才存在，不补进集合的话第二条会再走一遍
        # 识别与入库（唯一约束会挡下写入，但 bgm 请求已经白打出去了）。
        # 所以处理完就补进集合：无论它是被入库还是被过滤掉，同一轮里再见到都该是同样的结局。
        known = engine.existing_hashes(
            AnimeTorrent, [getattr(i, "info_hash", "") for i in items])
        new = 0
        for item in items:
            try:
                if await process_item(item, known_hashes=known, qb_alive=qb_alive):
                    new += 1
            except db.DatabaseBusy:
                # 【整轮早退，不要逐条记】(R22) 维护一开，后面每一条的 get_session() 都会一样失败。
                # 逐条落成 ERROR 的量是致命的：一轮 feed 几十到几千条（真实 Mikan 番组 feed 4193 条），
                # 而 /logs 的环形缓冲只有 200 条 —— 一次切库就把实时视图整块冲掉，
                # 同一时间段里真正的错误全被挤出去。冒到 run_worker 的 loop_error，整轮只留一行 INFO。
                raise
            except Exception as e:
                loop_error(f"处理 {getattr(item, 'anime_title', '?')}", e)
            finally:
                if getattr(item, "info_hash", ""):
                    known.add(item.info_hash)   # 同一 feed 内的重复条目不再重复识别（理由见上）
        log.info("源 %s：%d 条，新增 %d", source.name, len(items), new)

    try:
        n = await flush_ready_downloads(qb_alive=qb_alive)
        if n:
            log.info("缓冲窗口放行下载 %d 集", n)
    except Exception as e:
        loop_error("放行下载", e)


# 【本进程是否还欠一次"启动复位"】只有 main.py 在【启动时业务库就不可用】的那一支会置 True：
# 那时 init_business_state 被跳过，而库从进程启动起就一直 down、所有后台循环都按 is_data_down() 空转，
# 所以此刻库里的 downloading 必定是【上个进程】留下的残骸，复位它们是安全且必须的。
# 默认 False：运行中掉线再恢复、以及切库，都可能有交付协程正卡在 await（取种最长 180s），
# 打回 pending 会当场解除集去重 → 同一集被两个源各下一份到同一目录。
_startup_reset_pending = False


def init_business_state(reset_leftovers: bool = True) -> None:
    """业务库可用之后必做的初始化。三个操作都幂等，重复调用无害。

    reset_leftovers 的取值规则见上面 _startup_reset_pending 的说明：
    只有"进程启动时库就不可用、现在才首次可用"这一种情形才该复位遗留的 downloading。
    这些残骸没有第二条出路——sync_qb_status 显式跳过 downloading 行（交付协程独占），
    而 downloading ∈ HAVE_STATUSES，集去重闸会认定该集"已有一份"，flush/补下都不会再碰它。
    """
    global _startup_reset_pending
    anime.seed_source_groups()          # 首启种入 ANi/Mikan 两个源组
    if reset_leftovers:
        anime.reset_downloading()       # 复位上次遗留的 downloading（TV）
        movies.reset_downloading()      # 复位上次遗留的 downloading（剧场版）
        _startup_reset_pending = False  # 复位成功才清；失败则下一次探测继续尝试
    engine.backfill_legacy_progress_once()   # 一次性：历史 sent 标记为已完成


async def run_db_watch() -> None:
    """业务库健康看守：每 30s 探一次。

    连不上 → 标停摆，各后台循环自己跳过本轮、页面显示明确提示（不回退本地库，理由见 db 模块）。
    恢复   → 自动补跑迁移 + 补跑 init_business_state，不必重启。
    探测本身极便宜（一条 SELECT 1），日志只在状态变化时由 probe_data_engine 记一行。
    """
    was_down = db.is_data_down()
    while True:
        # 【整库维护期间整轮跳过，且【不动 was_down】】(R22)
        # 维护（切库 / 整库迁移）会让 `is_data_down()` 为真 —— 那是有意的（后台循环的把门
        # 判据只此一条）。但看守协程把"真/假"喂给了 `notify_state("db_down", …)`，
        # 而那是**边沿触发**的：维护窗口只要撞上这条 30 秒节拍，用户就会收到一条
        # 『数据库停摆，采集/下载/同步已暂停』的推送，维护结束再收一条『数据库恢复』——
        # 为一次他自己刚点下去的、几秒钟的操作。
        # 更糟的是随后 `was_down and not now_down` 这条恢复边沿会成立，
        # 白跑一次 init_business_state。
        # 跳过时【不能碰 was_down】：它记的是"维护之前库到底是好是坏"，维护结束后照那个判。
        if db.maintenance_reason():
            await asyncio.sleep(30)
            continue
        try:
            # 【必须丢到线程里】建连接是同步调用：目标主机关机或被防火墙 DROP 时，
            # TCP 连接要挂到超时才返回（已用 db.MYSQL_CONNECT_TIMEOUT 压到 5 秒，但仍是 5 秒），
            # 直接 await 不了的同步调用会把整个事件循环卡死——页面、下载、qB 同步一起停。
            now_down = bool(await asyncio.to_thread(db.probe_data_engine))
            # 【await 之后必须再查一次】(R22) 上面 while 顶端那一查在 `to_thread` **之前**，
            # 而 `to_thread` 是真实的挂起点（MySQL 后端上还要走 SELECT 1，重连时最长 5 秒）。
            # 用户点『切库/迁移』的处理器在事件循环里同步置 `_maintenance`，正好落进这个窗口时，
            # 线程里的 `probe_data_engine` 命中它自己那句 `if _maintenance: return _maintenance`，
            # now_down 变 True → 照样推一条假的『数据库停摆』。同样不动 was_down。
            if db.maintenance_reason():
                await asyncio.sleep(30)
                continue
            # 【状态型通知挂在这里】看守协程是全项目唯一知道"停摆↔恢复"何时翻转的地方；
            # 挂进 db.mark_data_down 反而不对——那是同步函数，且页面撞上异常时也会调它。
            await notify_state("db_down", now_down,
                               f"数据库停摆，采集/下载/同步已暂停：{db.data_down_reason()}",
                               "数据库恢复，后台已自动接上")
            # 【补读配置放在边沿【之外】】它曾被写在下面 `was_down and not now_down` 的边沿里，
            # 而那个分支在它最该起作用的场景下【不可达】：建表/迁移失败走的是 mark_data_fatal，
            # 此后 probe_data_engine 恒返回 fatal，边沿永不成立 —— 于是"进程到死都用着默认配置"
            # 这件它本来要防的事照旧发生。
            # 配置库恒是本地 SQLite（见 db 的双引擎说明），本就不必等业务库回来，每轮试一次即可。
            if not config.loaded_from_db:
                try:
                    config.load_from_db()
                    log.info("已补读数据库里的配置（启动时那次没跑成）")
                except Exception as e:
                    log.error("补读配置失败（下次探测再试）: %s", e)
            if was_down and not now_down:
                # 补跑停摆期间漏掉的初始化。是否复位遗留的 downloading 看本进程欠不欠那一次：
                # 【启动时就停摆】→ 欠（main.py 跳过了初始化，且此刻不可能有交付协程在跑）；
                # 【运行中掉线又回来】→ 不欠（可能有协程正卡在 await，打回 pending 会重复下载）。
                #
                # 【必须上线程】(R28) 这个函数做的全是同步库往返（seed_source_groups /
                # reset_downloading×2 / backfill_legacy_progress_once），而此刻 db.engine
                # 可能指向 MySQL。上一行的 `probe_data_engine` 早就 `to_thread` 了，
                # 另外两个调用点（设置页『切到 MySQL』、顶栏『立即重连』）也都上了线程 ——
                # **只有这一条是裸的**：R27 修的是同一件事，只落到 pages/settings.py 一处，
                # 而守卫也只读那一个文件（第②种形状：约束的作用域比验证的小）。
                # 这条路径还偏偏跑在"MySQL 刚回来、可能立刻又抖回去"那一刻：
                # pool_pre_ping 重连 5 秒、慢查询 15 秒才切断，事件循环整段冻住，
                # 而且它是**无人值守**的（另外两处是用户点了按钮在等）。
                await asyncio.to_thread(init_business_state, _startup_reset_pending)
            was_down = now_down
        except Exception as e:          # 探测自己出岔子也别让看守协程死掉，否则永远发现不了恢复
            loop_error("数据库看守", e)
        await asyncio.sleep(30)


async def run_worker() -> None:
    log.info("轮询器启动（采集%s），每 %d 秒一轮",
             "开" if config.ANIME_POLL_ENABLED else "关·在设置页开启", config.ANIME_POLL_INTERVAL)
    while True:
        if not config.ANIME_POLL_ENABLED or db.is_data_down():
            await asyncio.sleep(15)  # 暂停中/数据库停摆：短睡轮询开关，恢复后约 15s 内继续
            continue
        try:
            async with _poll_lock:       # 与手动『重新激活』互斥，别把同一批源抓两遍
                await poll_once()
        except Exception as e:
            loop_error("本轮", e)
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
        if not config.ANIME_POLL_ENABLED or db.is_data_down():
            continue                # 采集暂停 / 数据库停摆 → 重试也暂停
        try:
            await anime.retry_unmatched()
        except Exception as e:
            loop_error("延迟重识别", e)


async def run_backup() -> None:
    """独立协程：按 BACKUP_INTERVAL_HOURS 自动备份整库（开关在设置页）。

    刻意独立而不是挂在别的循环上：备份要在【采集/下载都可能正忙】的时候照做，
    而且它不该被采集暂停或 qB 掉线连累——那两种情形恰恰是最需要有备份的时候。
    每 10 分钟心跳一次，是否真备由 backup.auto_tick 按"距上次多久"判（跨重启不会误重备）。

    【业务库停摆时【照备】】这里原来有一道 `if not db.is_data_down()`，理由写的是
    "此时业务库连接本身就不可信"。那道门的作用域比它保护的东西大：backup_now 的快照源
    恒为 meta_engine（db/__init__ 里写死本地 SQLite），VACUUM INTO 全程不碰 data 引擎——
    业务库连不上，与本地配置库能不能备份【无关】。
    而后果是反的：MySQL 掉线期间一份备份都不做，且 mark_data_fatal 是不自愈的，
    于是"业务库出事"这个最需要有备份的时刻，恰好是备份彻底停摆的时刻。
    """
    from db import backup
    log.info("自动备份协程启动（%s，每 %d 小时，保留 %d 份）",
             "开" if config.BACKUP_ENABLED else "关·在设置页开启",
             config.BACKUP_INTERVAL_HOURS, config.BACKUP_KEEP)
    while True:
        await asyncio.sleep(600)          # 先睡后做：启动瞬间不抢资源，且刚起来备一份意义不大
        try:
            await asyncio.to_thread(backup.auto_tick)   # VACUUM INTO 是同步 IO，别卡事件循环
        except Exception as e:
            loop_error("自动备份", e)


async def archive_round() -> None:
    """跑一轮完成归档，全程持 `_archive_lock`。

    【锁必须盖住整个区间】`archive_old_completed` 是『读 (id, hash) → await qb.delete()
    → 出 await 后按主键写回 archived_at』，而在途闸只挡"开始维护"——
    锁没盖住的那一段等于不设防。抽成模块级函数（原来是 `run_qb_sync` 里的闭包）
    正是为了让用例能在**函数内部**问一次闸：光断言"闸认识这把锁"是测不到
    "这条线真的拿着它跑"的，把 `async with` 删掉那种断言照样绿。
    """
    async with _archive_lock:
        await engine.archive_old_completed()


async def sweep_round() -> None:
    """跑一轮完结/断更/积压巡检（含交付残骸清扫），全程持 `_sweep_lock`。

    【四段各自 try】它们互不相干：sweep_finished 抛一次异常不该让断更提醒与
    失败/停滞/积压告警整轮不跑——而后两者恰恰是"出事了要有人知道"的那一类。

    【交付残骸的清扫挂在这里】(R24) `status=downloading` 但本进程并没有协程在管的行
    是交付途中库抖了一下留下的残骸：sync 显式跳过它、集去重认定该集已有一份、
    看守协程的恢复边沿也不复位它 —— 以前只有重启进程才清得掉，
    而它还会把设置页的切库/迁移永久拒死。放在巡检轮上：有界、周期性、不碰在途的那些。

    【锁盖整轮】(R27) 完结判定与断更提醒都是"拿着 Anime.id 去 await 通知、
    回来按同一个主键写回"，锁只盖住其中一段等于没盖。整轮持锁的代价是维护要多等一轮巡检
    （默认 3 小时一轮、一轮几十秒），而闸只挡"开始维护"、不打断已经在跑的，用户重试即可。
    """
    async with _sweep_lock:
        for name, fn in (("交付残骸清扫", engine.sweep_stale_delivering),
                         ("完结判定", anime.sweep_finished),
                         ("断更提醒", anime.sweep_idle),
                         ("积压告警", anime.sweep_alerts)):
            try:
                r = fn()
                if asyncio.iscoroutine(r):    # 清扫是同步的（一条 SQL），其余三段是协程
                    await r
            except Exception as e:
                loop_error(f"巡检·{name} ", e, exc_info=True)


async def run_sweep() -> None:
    """独立协程：定期巡检『完结』与『断更』（开关在设置页）。

    刻意独立于采集轮询：这两件事回答的是"有没有【没发生】的事"，而采集循环只在有东西可抓时才动。
    尤其是断更检测——源失效/字幕组停更时采集轮看起来一切正常（抓到 0 条不是错误），
    只有这条巡检会说话。数据库停摆时跳过。
    """
    log.info("完结/断更巡检协程启动（%s，每 %d 分钟；断更阈值 %d 天）",
             "完结判定开" if config.ANIME_FINISH_ENABLED else "完结判定关",
             config.SWEEP_INTERVAL_MIN, config.ANIME_IDLE_DAYS)
    # 先睡后做：启动瞬间不抢资源，也避免"每次重启都立刻重判一遍"。
    await asyncio.sleep(max(60, config.SWEEP_INTERVAL_MIN * 60))
    while True:
        if db.is_data_down():
            # 【短睡重试，而不是睡满一整个巡检周期】巡检间隔默认 3 小时，库恢复后再等 3 小时
            # 才做第一次巡检，那段时间里完结/断更/积压全是瞎的。
            # 【长睡必须移出循环体】早先它是 while 的第一条语句，于是这里的 continue 回到的
            # 正是那个 3 小时长睡——"短睡重试"是个彻头彻尾的空操作，实测 sleep 序列
            # [10800, 30, 10800, 10800]：库在 3 小时时恢复，第一次巡检落在 6 小时。
            await asyncio.sleep(30)
            continue
        await sweep_round()          # 一轮的内容与"为什么四段各自 try"写在它的 docstring 里
        await asyncio.sleep(max(300, config.SWEEP_INTERVAL_MIN * 60))   # 做完再睡到下一轮


async def run_movie_scan() -> None:
    """独立协程：按 MOVIE_SCAN_INTERVAL 自动扫描 Mikan 当年剧场版/OVA（开关在 /movies 订阅源）。

    每 5 分钟心跳一次，是否真扫由 movies.auto_scan_tick 按『距上次扫描的间隔』判（跨重启也不会误重扫）。
    只碰剧场版，与 TV 采集互不相干。
    """
    log.info("剧场版自动扫描协程启动（%s，每 %d 秒）",
             "开" if config.MOVIE_SCAN_ENABLED else "关·在 /movies 订阅源开启", config.MOVIE_SCAN_INTERVAL)
    while True:
        try:
            if not db.is_data_down():
                # 与 /movies『立即扫描』和手动『重新激活』互斥（同一把 _scan_lock），别并发跑两轮整年扫描
                async with _scan_lock:
                    if await movies.auto_scan_tick():
                        log.info("剧场版自动扫描完成")
        except Exception as e:
            loop_error("剧场版自动扫描", e)
        await asyncio.sleep(300)  # 5 分钟心跳，到点才真扫


# 完成归档的墙钟节流间隔（秒）。内层高频轮询期间也要能跑到它，见 run_qb_sync 里的说明。
_ARCHIVE_EVERY = 600


async def run_qb_sync() -> None:
    """qB 状态同步：事件驱动 + 保底自查。

    平时停在 qb_kick 上休眠（0 开销）；有种子交付给 qB 时被 kick 立即醒来，按活跃间隔轮询这批『在下的』，
    全下完就回去休眠。另设保底超时（QB_SYNC_BACKSTOP_MIN 分钟）——即便漏了 kick / 重启 / qB 开关切换，也每隔
    这么久醒来自查一次、兜住漏网的在下种子。快路径管跟手、慢路径管最终一致，且种子在 qB 里照下不受影响。
    """
    log.info("qB 状态同步启动（事件驱动，活跃间隔 %ds，保底 %d 分钟）",
             config.QB_SYNC_INTERVAL, config.QB_SYNC_BACKSTOP_MIN)
    try:
        if not db.is_data_down() and engine.needs_qb_poll():
            engine.qb_kick.set()      # 启动即自查：接上重启前遗留的『在下的』种子
    except Exception as e:            # 停摆时直接跳过，别白查一次库再把异常记成噪声
        loop_error("qB 同步启动自查（忽略，靠保底兜住）", e)
    while True:
        # 三档节奏：① 高频轮询在下面内层 while（有活跃下载，每 QB_SYNC_INTERVAL 秒）；② 还有没下完的在下种子
        # 但都不活跃(慢/stalled/暂停) → 每 QB_IDLE_RECHECK_MIN 分钟自查一次，别等一个保底周期才发现完成；
        # ③ 全无在下 → 睡到保底 QB_SYNC_BACKSTOP_MIN。任一 kick 立即打断醒来。
        try:
            has_unfinished = engine.needs_qb_poll()
        except Exception:
            has_unfinished = True      # 拿不准(DB 锁等) → 用中档短超时，宁可多查一次
        wait_min = config.QB_IDLE_RECHECK_MIN if has_unfinished else config.QB_SYNC_BACKSTOP_MIN
        try:
            await asyncio.wait_for(engine.qb_kick.wait(), timeout=max(60, wait_min * 60))
        except asyncio.TimeoutError:
            pass                       # 到点：没人 kick 也醒来自查一遍
        engine.qb_kick.clear()
        # 【停摆闸放在这里，不能放在上面 wait_for 之前】本循环里其余每一处都按 is_data_down() 把门，
        # 唯独【醒来之后】的 archive_old_completed() 与内层循环是裸的同步库调用。而这一觉最长睡
        # QB_SYNC_BACKSTOP_MIN（默认 120 分钟）——放在睡前判等于用两小时前的结论决定现在做不做，
        # 库正是在这段睡眠里挂掉的话照样会冻住事件循环（MYSQL_CONNECT_TIMEOUT，5 秒）。
        if db.is_data_down():
            await asyncio.sleep(30)
            continue
        if config.QB_ENABLED and config.QB_SYNC_STATUS:
            # 【qB 掉线的告警不该只挂在采集轮上】那条预检在 flush 里，而采集可以被用户关掉
            # （全新库默认就是关的），于是"开箱即用"的配置下 qb_down 事件恒不触发。
            # 这个循环是唯一一条与采集开关无关、又必然要碰 qB 的路径。
            try:
                await notify_state("qb_down", not await engine.qb.reachable(),
                                   "qB 连不上，下载状态无法同步", "qB 恢复，继续同步")
            except Exception as e:
                loop_error("qB 可达性探测（忽略）", e)
        # 【归档不能只挂在"内层循环退出"这个事件上】(R22)
        # 它原来只在这里跑一次，而内层 while 的四个出口里有一个是 `has_active_downloading()` ——
        # 只要每轮都有一条在真下，idle 恒被清零、内层永不退出，外层这一句一次都不做。
        # 判据是 `qb_dlspeed >= max(1, QB_ACTIVE_FLOOR_KBPS*1024)`，而设置页写着"0=只要有速度就算"，
        # 于是 `QB_ACTIVE_FLOOR_KBPS=0` + 一条涓流种子就能把循环永久钉住：
        # **`QB_ARCHIVE_AFTER_DAYS` 对它的目标用户（长期挂着下载的人）恒不生效**
        # （R22 实测：跑满 200 个内层同步轮，archive 调用 0 次）。
        # 所以改成"墙钟节流的心跳"：外层醒来一次，内层每隔 _ARCHIVE_EVERY 秒也顺手做一次。
        last_archive = 0.0

        async def _maybe_archive(force: bool = False) -> None:
            nonlocal last_archive
            now = time.monotonic()
            if not force and now - last_archive < _ARCHIVE_EVERY:
                return
            last_archive = now
            try:
                await archive_round()
            except Exception as e:
                loop_error("完成归档（忽略，下轮再来）", e)

        await _maybe_archive(force=True)
        idle = 0                            # 连续几轮没在真下（局部计数，本次唤醒周期内累加、下次唤醒清零，无需入库）
        try:
            while (config.QB_ENABLED and config.QB_SYNC_STATUS
                   and not db.is_data_down() and engine.needs_qb_poll()):
                try:
                    await anime.sync_qb_status()   # 每轮批量刷新所有在下的：有活种子时慢的/stalled 的也顺便一起更新
                    await movies.sync_qb_status()
                except Exception as e:
                    loop_error("qB 状态同步", e)
                await _maybe_archive()             # 墙钟节流，理由见上面 _maybe_archive
                if engine.has_active_downloading():
                    idle = 0
                else:
                    idle += 1
                    if idle >= max(1, config.QB_SLOW_ROUNDS):
                        break   # 连续 N 轮没一个在真下(全 stalled/排队/慢速爬行) → 退出高频轮询，回等 kick/保底、休眠
                await asyncio.sleep(max(5, config.QB_SYNC_INTERVAL))
        except Exception as e:
            # needs_qb_poll()/has_active_downloading() 若因 DB 锁等抛错，别让它掀翻 while True（否则 qB 同步永久死掉）
            loop_error("qB 同步内层循环（回退休眠，等下次 kick/保底）", e)


async def scan_movies_now(year: int, letters: list) -> dict | None:
    """剧场版手动扫描（/movies 的『扫描』按钮走这里）：与后台整年扫描共用 _scan_lock。

    页面自己的防抖只在单个浏览器标签里有效，挡不住后台那一轮、也挡不住第二个标签页，
    而这正是最容易并发跑两轮整年扫描的入口。返回 movies.scan_now 的结果；已有一轮在跑则返回 None。
    """
    if _scan_lock.locked():
        return None
    async with _scan_lock:
        return await movies.scan_now(year, letters)


async def run_all_once() -> tuple[bool, str]:
    """手动『重新激活全部任务』（设置页按钮）：立刻跑一轮 = 抓所有源入库 → 放行到点的下载发往 qB
    → 按需扫剧场版 → 唤醒 qB 状态检查（顺带完成归档）。等价于重启服务后各协程立刻做的那一轮，但不重启进程。
    返回 (是否全部照做, 给用户看的结果)——有任何一段被跳过就是 False，页面据此用警告色，
    别把『其实什么都没做』显示成绿色的成功。

    采集暂停时跳过抓源（与 run_worker 同口径，暂停就是暂停）；数据库停摆时整个跳过（各后台循环都按
    db.is_data_down() 把门，这里不该是唯一的例外——那只会撞一串写库异常）。
    两段轮次各自看自己的锁：后台正在跑哪一段就跳过哪一段，另一段照做，不会因为剧场版在扫描就连采集也不跑。
    刻意【不】做 reset_downloading：那是启动时清上次异常退出的残留，运行中 status=downloading 的都是真在下的
    种子，复位成 pending 会让 flush 认为该集『还没有』而另挑一个源重下一份。
    """
    if db.is_data_down():
        return False, "数据库停摆中，未执行（先到设置页『数据库』修好连接）"
    # qB 检查【无条件先做】：qb_kick 只是置一个 Event，与别的轮在不在跑毫无关系，
    # 而"刷新在下种子的进度 + 完成归档"正是用户点这个按钮最常见的诉求，不该被别的段的跳过连累。
    engine.qb_kick.set()
    notes = []
    if not config.ANIME_POLL_ENABLED:
        log.info("重新激活：采集处于暂停，跳过抓源")
        notes.append("采集处于暂停、未抓源")
    elif _poll_lock.locked():
        notes.append("已有一轮采集在跑、未重复抓源")
    else:
        async with _poll_lock:
            await poll_once()
    if _scan_lock.locked():
        notes.append("已有一轮剧场版扫描在跑")
    else:
        try:
            async with _scan_lock:
                if await movies.auto_scan_tick():
                    log.info("重新激活：剧场版自动扫描完成")
        except Exception as e:
            loop_error("重新激活：剧场版扫描", e)
            notes.append("剧场版扫描出错（详见日志）")
    log.info("重新激活全部任务：完成%s", ("（" + "；".join(notes) + "）") if notes else "")
    tail = ("（" + "；".join(notes) + "）") if notes else ""
    return not notes, f"已重新激活{tail}，详情见日志页"
