"""可选通知推送（NOTIFY_URL 留空即静默关闭）。失败只记录，不影响主流程。

两层：
· notify()  —— 底层发送。最保守的旁枝处理，见它自己的 docstring。
· event() / state() —— 事件层。用户在设置页勾选想收哪些事件（NOTIFY_EVENTS）；
  带冷却与限流，且【状态型事件只在翻转的那一刻发】——那是"qB 掉线时每 30 秒一条通知"的根治法。
"""
import logging
import time
from urllib.parse import quote

import httpx

import config
from services import fetch
from services.fetch import TooLarge, get_bytes

# 事件表：键 → (中文名, 图标)。
# 【键是存进 settings 表的字面量，改名等于改用户配置】——要改名得同时写迁移，别顺手改。
EVENTS: dict[str, tuple[str, str]] = {
    "delivered": ("交付成功", "📥"),      # 每集下发成功（这是本项目原本唯一的那条通知）
    # 【剧场版单列一键，不复用 delivered】番剧是周更、剧场版是一年几部且【每一部都要人工点】，
    # 两者的推送节奏差一个数量级。合在一起时想关掉剧场版那几条就必须连番剧的一起关，
    # 而"番剧每集都要通知、剧场版不用"正是最常见的组合。
    "movie": ("剧场版交付", "🎬"),        # 剧场版/OVA 下发成功
    "failed": ("下载失败", "❌"),         # 落 error（含重试用尽）
    "stalled": ("停滞异常", "🐌"),        # 进度长期零推进
    "finished": ("完结", "🎊"),           # 1..总集数 全部到手
    "idle": ("断更/摸鱼", "🐟"),          # 长期没有新种子
    "qb_down": ("qB 连不上", "🔌"),       # 状态型：进/出各一条
    "db_down": ("数据库停摆", "💥"),      # 状态型
    "backlog": ("待识别积压", "❓"),      # 状态型（阈值上穿/下穿）
}

# (事件, key) → 上次发送时刻（monotonic）。进程内，重启即忘——这是【有意】的：
# 需要跨重启去重的那几种都各自有落库的列（Anime.finished_at / idle_notified_at）。
_last_sent: dict[tuple[str, str], float] = {}
# 状态型事件的当前认知：None=未知（首次观测到"坏"也算翻转，否则"启动时就坏着"永远不会被告知）
_state_now: dict[str, bool | None] = {}
# 限流令牌：最近一小时内已发出的时刻列表 + 被丢弃的条数
_sent_times: list[float] = []
_dropped = 0


def _safe_host() -> str:
    """NOTIFY_URL 里可以安全写进日志的那一段：只留主机名。

    Bark / Server酱 / ntfy 这类服务把推送密钥放在 URL 【路径】里
    （如 https://api.day.app/<密钥>/消息），所以日志里绝不能出现完整 URL。

    【netloc 不行，要用 hostname】netloc **含 userinfo**：
    `https://alice:hunter2@ntfy.homelab/topic` 的 netloc 是 `alice:hunter2@ntfy.homelab`，
    于是任何一次推送失败都会把 basic-auth 口令写进 data/autorss.log（滚动 5 份）
    与 /logs 页的「下载完整日志」。而 config 里没有 NOTIFY_USER/NOTIFY_PASS——
    URL 内嵌凭据是给推送端加 basic-auth 的**唯一方式**，所以这不是个边角情形。
    （core/ssrf.py 用的是 httpx.URL.netloc，那个【不含】userinfo，所以那边抄对了；
    这里抄的是 urlsplit 的 netloc，语义不同。）
    """
    from urllib.parse import urlsplit
    try:
        p = urlsplit(config.NOTIFY_URL or "")
        host = p.hostname or ""
        return (f"{host}:{p.port}" if p.port else host) or "(未配置)"
    except ValueError:
        return "(地址无法解析)"

log = logging.getLogger("autorss")

_BODY_CAP = 64 * 1024      # 只关心"送到没送到"，响应体一概不用；给 64KB 已远超任何推送服务的回执


async def notify(message: str) -> bool:
    """推一条通知。**返回它有没有真的送出去**（异常仍然一律吞掉，不外抛）。

    【为什么要有返回值】上层的冷却与"已通知"标记都靠它判断该不该记账。早先这里返回 None、
    异常又被就地吞掉，于是 event() 只能无条件当成"发出去了"——一条根本没送到的 qb_down
    照样把状态记成"坏"、把 6 小时冷却烧掉，而那正是最需要重发的时候。
    "送出去了"的口径 = 请求发出且未收到 4xx/5xx（对端有没有真的推给用户我们无从得知）。

    这是【最外圈的旁枝】：调用点在 download_*_torrent 的末尾（交付成功后），
    它一旦挂住或抛出，整条采集/放行链路就跟着停——故这里对超时与异常都取最保守的做法。

    · 走 fetch.get_bytes：httpx 的 timeout 是【每次读】的、逐块重置，涓流响应能永不触发；
      NOTIFY_URL 是用户自己填的第三方地址，必须有【总时长】上限与响应体上限（理由详见 fetch 模块）。
    · except Exception 而不是 httpx.HTTPError：URL 写坏时抛的是 httpx.InvalidURL（不继承 HTTPError）、
      端口越界时抛的是 ExceptionGroup，两者都会从这里逃出去打断本轮交付。通知失败不该有这种权力。
      （CancelledError 继承 BaseException，不在此列，关服仍能正常打断。）
    """
    if not config.NOTIFY_URL:
        return False
    url = f"{config.NOTIFY_URL}/💡{quote(message, safe='')}"  # safe='' 连 '/' 也编码，防可控番名注入额外路径段
    timeout = max(1, config.NOTIFY_TIMEOUT)
    try:
        # 走 config.http_client_kwargs：这里曾是全仓唯一【绕过代理设置】的出站请求。
        # NOTIFY_URL 指向墙外服务（Telegram/Pushover 等）时，通知会永远发不出去，
        # 而日志里只有一行"通知发送失败"，没人会想到是它没走代理。
        async with httpx.AsyncClient(**config.http_client_kwargs(timeout, url=url)) as client:
            await get_bytes(client, url, cap=_BODY_CAP, timeout=timeout)
        return True
    except TooLarge:
        return True   # 回执体太大 = 请求已经发出去、通知已经推了，只是我们不读它。不是失败。
    except httpx.HTTPStatusError as e:
        # get_bytes 里的 raise_for_status 对 3xx 也抛，而有些推送服务受理后就是 302 到状态页——
        # 那同样是"送到了"。只有 4xx/5xx 才是真没送到，值得记一条。
        # 【只记状态码与主机名】httpx 的异常串里带完整 URL，而 Bark/Server酱 这类服务把
        # 推送密钥放在 URL【路径】里——原样记下去就落进 data/autorss.log 与 /logs 页，
        # 用户用一键下载把日志贴进 issue 时密钥就公开了。
        if e.response.status_code >= 400:
            log.warning("通知发送失败：HTTP %s（%s）", e.response.status_code, _safe_host())
            return False
        return True   # 3xx：有些推送服务受理后就是 302 到状态页，那也是送到了
    except Exception as e:
        # 【str(e) 要脱敏再截断】上面 HTTPStatusError 那一支的注释写着"原样记下去密钥就公开了"，
        # 而这条兜底以前是裸 str(e)[:80]：services/fetch.py 造的异常消息正是以完整 URL 结尾，
        # 而 notify 的 url 含 Bark/Server酱 的路径密钥。截断也救不了——22 位密钥能塞进 80 字符。
        log.warning("通知发送失败：%s: %s（%s）", type(e).__name__,
                    fetch.redact(e)[:120], _safe_host())
        return False


def enabled(kind: str) -> bool:
    """这个事件用户订阅了吗。

    【空列表 = 全关】不是全开。设置页必须把这句写出来——"留空=不限"在本项目别的地方
    （字幕组白名单、标题关键词）恰好是相反的意思，最容易想当然。
    """
    return bool(config.NOTIFY_URL) and kind in set(config.NOTIFY_EVENTS or [])


_STATE_CAP_PER_HOUR = 12          # 状态型自己的小桶：6 组"坏+好"，足够表达真实故障，挡得住抖动风暴
# 【按 kind 分账，不是一个全局桶】桶的立论是"单一 kind 反复抖动时要有上界"——
# 那用每个 kind 各一个桶就够了，而共用一个桶会让一个抖动的 kind 把别的饿死：
# qb_down 抖起来能把 db_down 与 backlog 的额度一起吃光，而后两者恰恰是最需要送达的。
# 上界从 12/小时 变成 12×kind 数，仍然有界。
_state_times: dict[str, list[float]] = {}


def _state_rate_ok(kind: str) -> bool:
    """状态型事件的独立限流（每个 kind 各一个桶）。见 state() 里的说明。"""
    now = time.monotonic()
    times = [t for t in _state_times.get(kind, ()) if t > now - 3600]
    if len(times) >= _STATE_CAP_PER_HOUR:
        _state_times[kind] = times
        return False
    times.append(now)
    _state_times[kind] = times
    return True


def _rate_ok() -> bool:
    """限流：一小时内最多 NOTIFY_MAX_PER_HOUR 条（0=不限）。超了就丢弃并记账。"""
    global _dropped
    cap = config.NOTIFY_MAX_PER_HOUR
    if cap <= 0:
        return True
    now = time.monotonic()
    cutoff = now - 3600
    _sent_times[:] = [t for t in _sent_times if t > cutoff]
    if len(_sent_times) >= cap:
        _dropped += 1
        return False
    _sent_times.append(now)
    return True


async def event(kind: str, message: str, *, key: str = "", cooldown: int = 0,
                bypass_rate: bool = False) -> bool:
    """推一条事件通知。未订阅 / 冷却中 / 被限流 → 静默丢弃（通知永远不该影响主流程）。
    **返回这条消息有没有真的发出去**——调用方据此决定要不要落"已通知"的标记。

    key + cooldown：同一 (kind, key) 在 cooldown 秒内只发一条。用来压住"同一件事反复触发"。

    【记账必须晚于发送】早先的写法是先写冷却、再判限流：一条被限流丢掉的通知照样把这个
    (kind,key) 的冷却窗口烧掉 6 小时，于是那条告警在窗口内【永远不会重发】。
    落库的那几个标记（finished_at / idle_notified_at）同理，都得等这个返回值。
    """
    global _dropped
    if not enabled(kind):
        return False
    slot = (kind, key)
    now = time.monotonic()
    if cooldown > 0:
        last = _last_sent.get(slot, 0.0)
        if last and now - last < cooldown:
            return False
    if not bypass_rate and not _rate_ok():
        return False
    icon = EVENTS.get(kind, ("", "💡"))[1]
    tail = ""
    if _dropped:
        # 把"你有 N 条没收到"挂在下一条放行的消息尾巴上——否则限流本身是不可见的，
        # 用户只会觉得"通知时灵时不灵"。
        tail = f"（另有 {_dropped} 条被限流丢弃）"
        _dropped = 0
    ok = await notify(f"{icon}{message}{tail}")
    if not ok:
        # 没送出去：把刚才吞掉的"另有 N 条被限流丢弃"还回去，否则这条提示会随本次失败一起消失。
        if tail:
            _dropped += 1
        return False
    if cooldown > 0:
        _last_sent[slot] = now      # 送出去了才起算冷却
    return True


async def state(kind: str, bad: bool, bad_msg: str, ok_msg: str = "") -> None:
    """状态型事件：只在【翻转】的那一刻发一条。

    这是"qB 掉线时每 30 秒一条通知"的根治法——边沿触发，不是电平触发。
    首次观测到 bad=True 也算翻转（`None → True`），否则"启动时就已经坏着"永远不会被告知；
    而首次观测到 bad=False 【不】发（一切正常时不该有通知）。
    """
    prev = _state_now.get(kind)
    if prev == bad:
        return
    # 【订阅判定要在扣费之前】enabled() 原本在 event() 内部才判，于是一个【没订阅】的 kind
    # 每一轮都判成翻转、每一轮扣一枚令牌，而它永远发不出去、_state_now 也永远不落记忆。
    # db_watch 每 30 秒一轮 = 120 次/小时，足够把 12 枚一小时的额度打光，
    # 把另外两个【已订阅】的状态型事件一起饿死。实测：未订阅的 qb_down 调 12 轮 → 桶 12/12。
    if not enabled(kind):
        return
    # 【状态记忆也要等发送成功】早先是先写记忆再发：一条被限流吞掉的 qb_down / db_down
    # 就此【永远不会重发】——因为状态已经记成"坏"，后续每一轮都判为"没翻转"而静默返回，
    # 用户最后只会收到一条孤零零的"已恢复"。而这两个恰恰是全项目唯一的带外故障信号。
    # 【状态型走自己的桶，不占 delivered 的额度】共用一个桶时，桶满会把边沿推迟到下一轮，
    # 而 qb_down 的下一轮可能是 20 分钟后——那正是最需要立刻知道的时候。
    # 但也【不能完全不限流】："单次翻转最多两条"是真的，"每小时的翻转次数有上界"却不是：
    # qB 在掉线边缘反复抖动时，判据每轮翻转一次，两小时能打出几百条。
    # 独立小桶两头都占住：不被 delivered 挤掉，自己也失控不了。
    if not _state_rate_ok(kind):
        return                          # 抖动风暴：本轮不记也不发，等它稳定下来
    # 【先占位再发】边沿判定（上面那个 prev）与写回之间隔着 await（最长 NOTIFY_TIMEOUT）。
    # qB 掉线时两条路径会同时进来（qB 同步轮 与 flush 的 qb_precheck，分属不同锁；
    # 或页面『补下全部』被双击——它没有防重入），两个协程都读到同一个 prev、都判成翻转，
    # 于是同一次翻转推【两条】、占两枚令牌。对称交错还能把一次真实掉线告警吞掉一轮。
    # 占位之后失败再还原，"没发出去就不落记忆"这条性质保持不变。
    _state_now[kind] = bad
    try:
        if bad:
            ok = await event(kind, bad_msg, bypass_rate=True)
        elif prev is None:
            ok = True                   # 首次观测到"好"：只记不发（一切正常时不该有通知）
        else:
            ok = (not ok_msg) or await event(kind, ok_msg, bypass_rate=True)
    except Exception:
        _state_now[kind] = prev         # 【异常也要还原】否则那次翻转被永久吃掉
        raise
    if not ok:
        _state_now[kind] = prev


def reset_state() -> None:
    """清空进程内的事件记忆。用户改了通知设置后调它——否则改完开关还要等冷却过期才看得到效果。"""
    _last_sent.clear()
    _state_now.clear()
    _sent_times.clear()
    _state_times.clear()
    global _dropped
    _dropped = 0
