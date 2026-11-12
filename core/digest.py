"""每日摘要推送：一条消息讲清『这段时间下了什么 / 有什么等你处理 / 系统好不好』。

只在出事时推送的话，"没收到推送"既可能是一切正常，也可能是推送地址或程序自己坏了，两者分不开；
待确认、待识别、仪表盘提示这几类待办又本来就没有即时推送（逐条推会刷屏）。每天固定发一条，
哪天没收到就说明推送或程序出了问题。

【取数一律调仪表盘 / 巡检现成的函数】anime.overview / pending_confirm / locked_out_episodes、alerts.view、
health.*。摘要里的数字因此与仪表盘卡片对得上，改判据时也只有一处要改。只有两项是这里自己查的：
"这段时间新下的集"与"这段时间新入库、等你去点的剧场版版本"——仪表盘上没有按时间窗算的这两个数。
"""
import logging
import time
from datetime import datetime, timedelta
from urllib.parse import quote

from sqlalchemy import func
from sqlmodel import select

import config
import db
from core import alerts, anime, engine, health
from db.models import Anime, AnimeTorrent, MovieTorrent
from services import notify

log = logging.getLogger("autorss")

# 【封顶有两道：字数管可读，字节管送得到】推送走 GET，消息百分号编码后进 URL 路径（一个汉字 9 字节）。
# bark-server（fiber v2）默认读缓冲 4096 字节，请求行加全部请求头超过它就回 431；500 个汉字编码后
# 就有 4.5 KB，只按字数封顶会恰好在积压最多的那几天发不出去。三行字节预算合计 3000，
# 给密钥、图标、"另有 N 条没能送到"的尾巴与请求头留约 1 KB。第二行全是待办，预算给得最宽，
# 各项顶满时约 1.1 KB、截不到；会被截的是第一行末尾的番名与第三行长说明的尾巴。
_LINE_CAP = (140, 179, 179)            # 字数，合计加两个换行 ≤ 500
_LINE_BYTES = (850, 1300, 850)         # 编码后字节，合计 ≤ 3000
_NAMES = 3          # 第一行列几部番名
_PEND_NAMES = 2     # 待确认列几部番名
_BACKOFF_MAX = 3600  # 没送到时重试间隔的上限（秒）

# 没送到时的退避：只靠 notify 的共享熔断不够——别的通知照常送达时连续失败计数一直被清零，
# 熔断永远合不上，摘要就每 30 秒重算一次文本、重发一次；别的通知不多时，摘要每次失败又给熔断续命，
# 把交付通知一起丢掉。送达即清零；设置页改了推送设置也清零（reset_retry）。
_fails = 0
_retry_at: datetime | None = None


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def _clip_bytes(s: str, budget: int) -> str:
    """按百分号编码后的字节数截断，超出补 "…"。逐字符累加，不会截出半个编码。"""
    if len(quote(s, safe="")) <= budget:
        return s
    used, out = len(quote("…", safe="")), []
    for ch in s:
        used += len(quote(ch, safe=""))
        if used > budget:
            break
        out.append(ch)
    return "".join(out) + "…"


def digest_text(since: datetime, now: datetime) -> str:
    """三行文本：下了什么 / 等你处理什么 / 系统状态。每行按 _LINE_CAP 字、_LINE_BYTES 字节封顶。"""
    hours = max(1, round((now - since).total_seconds() / 3600))
    span = f"{round(hours / 24)} 天" if hours >= 48 else f"{hours} 小时"
    with db.get_session() as s:
        # 窗口内入库、且已有着落（HAVE_STATUSES：已交付 / 交付中 / 停滞）的正集，按番归并。
        # 按入库时刻而不是交付时刻算（库里没有交付时刻这一列），所以文案写"约"。
        got: dict = {}
        for aid, ep in s.exec(select(AnimeTorrent.anime_id, AnimeTorrent.episode).where(
                AnimeTorrent.created_at >= since, AnimeTorrent.created_at <= now, AnimeTorrent.episode >= 1,
                AnimeTorrent.status.in_(engine.HAVE_STATUSES))):
            got.setdefault(aid, set()).add(ep)
        top = sorted(got, key=lambda i: (-len(got[i]), i))[:_NAMES]
        names = {a.id: anime.display_of(a) for a in s.exec(
            select(Anime).options(anime._NO_SUMMARY).where(Anime.id.in_(top)))} if top else {}
        # 剧场版没有自动下载，新入库的版本落 pending（已忽略的片落 skipped）——就是等你去点的那些
        new_mv = s.exec(select(func.count()).select_from(MovieTorrent).where(
            MovieTorrent.created_at >= since, MovieTorrent.created_at <= now,
            MovieTorrent.status == "pending")).one()
        # 与巡检 sweep_lock_lag 取同一批番（锁了下载源的），追番中等条件由 locked_out_episodes 自己判
        locked = list(s.exec(select(Anime).options(anime._NO_SUMMARY).where(
            Anime.pref_source.is_not(None), Anime.pref_source != "")))
    n_eps = sum(len(v) for v in got.values())
    if got:
        line1 = (f"近 {span}约下了 {n_eps} 集（{len(got)} 部）："
                 + "、".join(names.get(i, "?") for i in top) + ("…" if len(got) > len(top) else ""))
    else:
        line1 = f"近 {span}没有新下的"

    ov = anime.overview()                 # 仪表盘 KPI 卡片背后的同一份数
    pend = anime.pending_confirm()
    lag = anime.locked_out_episodes(locked)
    live = alerts.view()["live"]
    gaps = health.gaps()                  # 全站横幅上那条『RSS 断档』，点了『知道了』才清
    todo = []
    if pend:
        todo.append(f"待确认 {len(pend)} 部（" + "、".join(
            f"{_clip(anime.display_of(a), 24)}·{anime.confirm_reason_text(a.confirm_reason)[0]}"
            for a in pend[:_PEND_NAMES]) + (" 等" if len(pend) > _PEND_NAMES else "") + "）")
    for label, n, unit in (("待识别", ov["kpi"]["fail"], "部"), ("缺集", ov["kpi"]["gaps"], "部"),
                           ("锁定源落后", len(lag), "部"),
                           ("失败/异常", ov["status"]["error"] + ov["status"]["stalled"], "条"),
                           ("未读提示", len(live), "条"), ("剧场版新版本", new_mv, "个"),
                           ("RSS 断档", len(gaps), "段")):
        if n:
            todo.append(f"{label} {n} {unit}")
    line2 = ("等你处理：" + "、".join(todo)) if todo else "没有要你处理的事"

    # 别的模块后来加的健康判据（推送通道连续失败、后台协程退出）有就带上，没有就不提
    probes = [health.poll_problem, health.sources_problem,
              getattr(notify, "problem", None), getattr(health, "tasks_problem", None)]
    probs = [p for p in (f() for f in probes if callable(f)) if p]
    if not ov["config"]["qb"]:            # 与仪表盘横幅同一个判据
        probs.append("qB 未启用，只采集不下载")
    line3 = "系统：" + ("；".join(probs) if probs else "正常")

    return "\n".join(_clip_bytes(_clip(ln, cap), b)
                     for ln, cap, b in zip((line1, line2, line3), _LINE_CAP, _LINE_BYTES))


def _due(now: datetime) -> bool:
    """到点：当前小时 ≥ NOTIFY_DIGEST_AT，且距起算点已满 NOTIFY_DIGEST_HOURS 小时。

    【起算点】间隔是整天的倍数（24、168…）时，取上次送达那天的 AT 点（送达时已过 AT 点的话）：
    首次启用在晚上、某天推送坏到傍晚才通、把 AT 调小，下一条都回到设定的钟点，而不是钉在那次送达的钟点上；
    同一天内离下一个 AT 点不足一个间隔，不会连发。其余间隔取上次送达的整点——只去掉分秒，
    否则看守节拍 30 秒一次加上推送耗时，每条都比上一条晚几十秒，一年下来会漂两小时。
    LAST 为空或损坏 → 视作到点（自愈，而不是永久不发）；起算点比现在还晚（时钟被回拨一天以上）→ 同样视作到点。
    """
    if now.hour < config.NOTIFY_DIGEST_AT:
        return False
    last = config.NOTIFY_DIGEST_LAST
    if not last:
        return True
    try:
        base = datetime.fromisoformat(last).replace(minute=0, second=0, microsecond=0)
        if config.NOTIFY_DIGEST_HOURS % 24 == 0 and base.hour > config.NOTIFY_DIGEST_AT:
            base = base.replace(hour=config.NOTIFY_DIGEST_AT)
        elapsed = (now - base).total_seconds()
    except (ValueError, TypeError):      # TypeError：手改成了带时区的时间戳，与本地时间不能相减
        return True
    return not 0 <= elapsed < config.NOTIFY_DIGEST_HOURS * 3600


def _since(now: datetime) -> datetime:
    """统计窗口的起点：上次送达的时刻（中间停过机、推送断过几天也不漏算），但最多回看 3 个间隔——
    摘要关了几个月再打开，第一条不该写成"近 2160 小时"；没有 / 损坏 / 比现在晚就往前推 HOURS 小时。"""
    try:
        last = datetime.fromisoformat(config.NOTIFY_DIGEST_LAST)
        if last <= now:
            return max(last, now - timedelta(hours=3 * config.NOTIFY_DIGEST_HOURS))
    except (ValueError, TypeError):
        pass
    return now - timedelta(hours=config.NOTIFY_DIGEST_HOURS)


async def tick(now: datetime | None = None) -> bool:
    """看守协程每 30 秒调一次：到点就发一条摘要。送达返回 True。

    先判到点、再算文本：没到点时只读内存里的配置，一条业务库查询都不发。
    【送达才写 NOTIFY_DIGEST_LAST】没送到就留着，按 30 秒起、逐次翻倍、最长 1 小时退避重试；
    推送熔断期间不试——熔断中每试一次都会被记成一条"没能送到"挂在下一条消息尾巴上，一次摘要会被报成十几条丢失。
    走事件键 digest、绕开每小时限流桶：它一天一条，不该被当天的交付通知挤掉。
    """
    global _fails, _retry_at
    if config.NOTIFY_DIGEST_HOURS <= 0 or not notify.enabled("digest"):
        return False
    if db.is_data_down() or notify.muted():
        return False
    now = now or datetime.now()
    # 上界：时钟被往回拨时，退避不能被拉成好几天
    if _retry_at and now < _retry_at <= now + timedelta(seconds=_BACKOFF_MAX):
        return False
    if not _due(now):
        return False
    text = digest_text(_since(now), now)
    if not await notify.event("digest", text, bypass_rate=True):
        _fails += 1
        _retry_at = now + timedelta(seconds=min(_BACKOFF_MAX, 30 * 2 ** min(_fails - 1, 7)))
        return False
    _fails, _retry_at = 0, None
    log.info("每日摘要已发出")
    stamp = now.isoformat(timespec="seconds")
    try:
        config.set_many({"NOTIFY_DIGEST_LAST": stamp})
    except Exception as e:
        # 已经送达了：落库失败也要记进内存，否则下个节拍又判到点，每 30 秒重发一条
        config._v["NOTIFY_DIGEST_LAST"] = stamp
        log.warning("每日摘要已发出，但记录发送时间失败（本进程内不会重发，重启后可能多发一条）：%s", e)
    return True


def reset_retry() -> None:
    """设置页改了推送设置时调：清掉退避，改完地址下一个节拍就重试，不用等满退避。"""
    global _fails, _retry_at
    _fails, _retry_at = 0, None
