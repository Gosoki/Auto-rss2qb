"""采集健康：停摆 / 暂停 / 源连续失败 / RSS 窗口溢出。(r37 #1 #7)

【为什么要有它】2026-09-27 宿主机关机，容器停了 15 天，10-12 才开回来 —— 采集半个月一条没进，
全程零提醒；开回来之后也没人知道漏了哪段（ANi 的 nyaa RSS 只留最近约 9 天，Mikan Classic 约 1 天，
09-27~10-03 的发布永久看不到了，5 部 10 月新番缺第 1 集）。那时页面上唯一的信号是仪表盘 tab 里
『上次采集』的红徽标，恢复后第一轮跑完就变回灰色。

这里只放【状态与判据】，不碰业务库、不 import worker：worker 记账，看守协程（run_db_watch）每 30 秒读一次
推状态型通知，frame() 读它挂全站横幅。所以 pages/layout 可以放心 import 它（不会把整条采集链拖进页面模块）。

  · 停摆：采集开着，却已经很久没有任何动静（一轮卡死在某个 await 上 / 协程死了）。
          进程【整个不在】的那 15 天它当然发现不了 —— 那要靠外部心跳（HEARTBEAT_URL）与下面的窗口溢出。
  · 暂停：采集开关关着超过 ALERT_POLL_PAUSED_HOURS。暂停本身是用户的决定，但 RSS 只留最近一段，
          关久了就是在静默漏集，这件事用户未必知道。
  · 源连续失败：同一个源组连着 N 轮抓不到（网络、403、站点返回维护页）。只看"上一轮"的话，
          一个源坏了三天，页面上和偶尔抖一下没有区别。
  · 窗口溢出：每个 feed 记下 seen ——"这之前的发布都已处理过"（正常等于上次抓取时刻）；这一轮成功抓到时，
          若 feed 里最老一条都比它晚，中间那段就已经滚出 RSS、永远看不到了。这是零误报的：说"可能漏了"的
          那段时间里我们确实一条都没处理到。入库撞上连接错误时，断档也可能落在抓到但没入库的那段（r39 A13）。
"""
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

import config

log = logging.getLogger("autorss")

# 最近一次"采集在动"的时刻：一轮开始、每个源抓完、每条处理完、一轮结束。进程启动算一次。
_last_activity: datetime = datetime.now()
# 源组名 → (连续失败轮数, 最近一次失败原因)。进程内；重启后从 0 数起（重启本身就是一次重来）。
_fail: dict[str, tuple[int, str]] = {}
_alive: set = set()                   # 上一轮启用着的源组名（forget_sources 时记下，判"全挂了"要用分母）
# 一看就是"这台机器连不上网"的失败原因（DNS 解析不了、连接被拒/超时、路由不可达）
_NET_HINTS = ("name resolution", "Name or service not known", "nodename nor servname",
              "ConnectError", "ConnectTimeout", "Network is unreachable", "No route to host")
# feed 键 → seen：这之前的发布都已处理过（正常等于上次抓取时刻；UTC ISO）。
# 持久化在运行时配置 SOURCE_FEED_SEEN 里（重启之后正要用它）。
_seen: dict | None = None
_GAP_MIN = timedelta(minutes=10)      # 小于这个的"断档"只是两边时钟差，不报
_GAPS_KEEP = 10


def touch() -> None:
    global _last_activity
    _last_activity = datetime.now()


def record_source(name: str, ok: bool, reason: str = "") -> None:
    if ok:
        _fail.pop(name, None)
    else:
        n = _fail.get(name, (0, ""))[0]
        _fail[name] = (n + 1, reason[:120])


def forget_sources(alive: set) -> None:
    """删掉已经不在启用列表里的源组的计数（被删 / 被停用的组不该一直挂着"连续失败"）。"""
    global _alive
    _alive = set(alive)
    for n in list(_fail):
        if n not in alive:
            _fail.pop(n, None)


def failing_sources() -> list[tuple[str, int, str]]:
    """连续失败达到阈值的源组：[(组名, 连续轮数, 最近原因)]。"""
    th = max(1, config.ALERT_SOURCE_FAIL_ROUNDS)
    return sorted(((n, c, r) for n, (c, r) in _fail.items() if c >= th), key=lambda x: -x[1])


def network_down() -> bool:
    """所有启用的源都连续失败到了阈值、而且失败原因全是连接/DNS 类 —— 多半是这台机器断网了。(r37)
    2026-10-14 06:00 ~ 10-15 12:00 容器断网约 30 小时：6 个源每轮各报一条"抓取失败"，qB 也连不上，
    页面上看起来像是六个源同时坏了，其实是一件事。"""
    bad = failing_sources()
    return (len(_alive) >= 2 and len(bad) == len(_alive)
            and all(any(h in r for h in _NET_HINTS) for _, _, r in bad))


def sources_problem() -> str | None:
    """源抓取连续失败的人话描述（横幅与推送共用）；没问题返回 None。全挂且都是网络错时合成一句"多半断网"。"""
    bad = failing_sources()
    if not bad:
        return None
    if network_down():
        return (f"所有 {len(bad)} 个源都连续 {min(c for _, c, _ in bad)} 轮以上连不上（DNS 解析失败 / 连接失败）"
                "——多半是这台机器断网了，qB 很可能也连不上。看一下容器 / 宿主机的网络。")
    return ("源抓取连续失败：" + "；".join(f"{n} 连续 {c} 轮（{r}）" for n, c, r in bad)
            + "。去『订阅源』看看地址还对不对、站点是不是挂了。")


def fail_streak(name: str) -> int:
    return _fail.get(name, (0, ""))[0]


def _feed_key(url: str) -> str:
    """feed 的持久化键。用 URL 的摘要而不是 URL 本身：Mikan『我的番组』的 token 在 query 里。"""
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:12]


def _load_seen() -> dict:
    global _seen
    if _seen is None:
        try:
            _seen = json.loads(config.SOURCE_FEED_SEEN or "{}")
            if not isinstance(_seen, dict):
                _seen = {}
        except (ValueError, TypeError):
            _seen = {}
    return _seen


def seen_at(url: str) -> datetime | None:
    """这个 feed 记着的 seen：这之前的发布都已处理过（正常等于上次抓取时刻；带时区的 UTC）。
    没记过 / 记坏了返回 None。"""
    seen = _load_seen()
    k = _feed_key(url)
    try:
        return datetime.fromisoformat(seen[k]) if k in seen else None
    except (ValueError, TypeError):
        return None


def mark_seen(url: str, t: datetime | None) -> None:
    """把这个 feed 的 seen 写成 t：这之前的发布都已处理过（正常等于上次抓取时刻；
    撞上业务库连接错误时 worker 会写得更早，规则在 poll_once）。t 为 None 时不写。(r39 A13)"""
    if t is not None:
        _load_seen()[_feed_key(url)] = t.isoformat(timespec="seconds")


def check_window(url: str, oldest: datetime | None, now: datetime | None = None, *,
                 commit: bool = True):
    """这个 feed 这一轮成功抓到了：记下时刻，并判断上次到这次之间有没有一段滚出了 RSS。
    返回 (seen, 这次最老一条的时刻)（都是带时区的 UTC），没有断档返回 None。
    同一个 URL 被两个源组共用时，第二个组读到的"上次"就是第一个组刚写的，不会重复报。

    commit=False：只算断档、不写时刻 (r39 A13)。"抓到了"不等于"入库了"——worker 要等这一组条目
    处理完、看有没有撞上业务库连接错误，再用 mark_seen 决定推进到哪（规则写在 poll_once 里）。"""
    now = now or datetime.now(timezone.utc)
    prev = seen_at(url)
    if commit:
        mark_seen(url, now)
    if prev is None or oldest is None:
        return None
    if oldest - prev > _GAP_MIN:
        return prev, oldest
    return None


def seen_snapshot() -> str:
    """给 worker 轮末写回 SOURCE_FEED_SEEN 用。"""
    return json.dumps(_load_seen(), sort_keys=True)


def gaps() -> list[dict]:
    """记下来、用户还没点『知道了』的断档：[{src, from, to, at}]（时间是本地时间的 ISO 串）。"""
    try:
        v = json.loads(config.FEED_GAPS or "[]")
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def gap_record(src: str, frm: datetime, to: datetime) -> dict:
    def _local(d):
        return d.astimezone().replace(tzinfo=None).isoformat(timespec="minutes")
    return {"src": src, "from": _local(frm), "to": _local(to),
            "at": datetime.now().isoformat(timespec="minutes")}


def gaps_with(new: list[dict]) -> str:
    """把新断档并进已有的（最多留 _GAPS_KEEP 条），返回要写回 FEED_GAPS 的串。"""
    return json.dumps((gaps() + new)[-_GAPS_KEEP:], ensure_ascii=False)


def clear_gaps() -> None:
    config.set_many({"FEED_GAPS": ""})


def gap_text(g: dict) -> str:
    def _short(s):
        return (s or "")[5:16].replace("T", " ")
    return f"{g.get('src', '?')} {_short(g.get('from'))} ~ {_short(g.get('to'))}"


def _ago(td: timedelta) -> str:
    s = int(td.total_seconds())
    if s >= 86400:
        return f"{s // 86400} 天{(s % 86400) // 3600} 小时"
    if s >= 3600:
        return f"{s // 3600} 小时{(s % 3600) // 60} 分"
    return f"{max(1, s // 60)} 分钟"


def _parse_local(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def stall_threshold() -> timedelta:
    """多久没动静算停摆：ALERT_POLL_STALL_MIN 与 3 个轮询间隔取大者（间隔调到 2 小时的人不该被误报）。"""
    return timedelta(minutes=max(config.ALERT_POLL_STALL_MIN, 3 * max(60, config.ANIME_POLL_INTERVAL) // 60))


def poll_problem(now: datetime | None = None) -> str | None:
    """采集停摆 / 暂停太久的人话描述；没问题返回 None。业务库停摆时不判（那边有自己的全站提示）。"""
    import db
    if db.is_data_down():
        return None
    now = now or datetime.now()
    last = _parse_local(config.ANIME_POLL_LAST)
    if not config.ANIME_POLL_ENABLED:
        hours = max(0, config.ALERT_POLL_PAUSED_HOURS)
        if hours <= 0 or last is None:       # 全新库从没采过：那是还没配好，不是"暂停太久"
            return None
        if now - last < timedelta(hours=hours):
            return None
        return (f"采集已暂停 {_ago(now - last)}（上一轮结束于 {last:%m-%d %H:%M}）。RSS 只保留最近一段"
                "（Mikan 约 1 天、nyaa 约一周），关久了中间的发布会滚出去、再也收不到。不是有意关的就去设置页打开。")
    idle = now - _last_activity
    if idle < stall_threshold():
        return None
    tail = f"，上一轮结束于 {last:%m-%d %H:%M}" if last else ""
    return (f"采集开着，但已经 {_ago(idle)} 没有任何动静{tail}。多半是某一轮卡住了或后台协程退出了，"
            "去 /logs 看最近的 ERROR；重启服务可以恢复。")
