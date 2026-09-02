"""日志装配：控制台 + 滚动文件(data/autorss.log) + 内存环形缓冲(供 /logs 页实时看)。

main.py 导入时调用 setup_logging() 一次。控制台/文件挂根 logger（全量，含框架报错，
经 _SuppressDeletedSlot 滤掉 NiceGUI 断连噪声）；环形缓冲只挂 'autorss' logger——本工具各模块
都用这个名，故缓冲里只有采集/下载/重识别/qB 同步这些真正想看的日志，不掺 HTTP 访问等框架噪声。
"""
import logging
from collections import deque
from logging.handlers import RotatingFileHandler

from config import DATA_DIR

LOG_PATH = DATA_DIR / "autorss.log"
_RING_CAPACITY = 200            # /logs 页最多回看的最近条数（更早的翻日志文件）
_FILE_MAXBYTES = 2_000_000      # 单个日志文件上限≈2MB，滚动保留 5 份 → 最多约 10MB
_FILE_BACKUPS = 5

_FMT = logging.Formatter("%(asctime)s %(levelname)s %(message)s")


class _SuppressDeletedSlot(logging.Filter):
    """滤掉 NiceGUI 在客户端断开瞬间偶发的一族良性报错——面板自动刷新的 ui.timer、或断连后 async
    处理器回来时 ui.notify/refresh 访问已删元素/客户端，都会抛这几条兄弟消息。客户端已走、不影响功能，
    只是刷屏并掩盖真错，故按消息精确过滤。三条 needle 都足够特指，不会误吞真正的错误。"""
    _NEEDLES = (
        "parent slot of the element has been deleted",
        "The client this element belongs to has been deleted",
        "The client this outbox belongs to has been deleted",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        text = record.getMessage() + " " + str(exc or "")
        return not any(n in text for n in self._NEEDLES)


class _RedactUrls(logging.Filter):
    """把每条日志里的 URL 换成"只留 scheme+host"的形式。**装在这里而不是各调用点。**

    【为什么必须是结构性的】(R21) 泄漏的形状是：`except Exception as e: log.warning("… %s", e)`
    —— 而 httpx 的 `HTTPStatusError`/`ConnectError` 的 `str()` 原样带着完整 URL。
    私有站的 .torrent 直链把 **passkey 放在 query 里**（手动下载那条路的输入就是它），
    Mikan『我的番组』订阅地址把 token 放在 query 里。一次 403 就把密钥写进三个地方：
    `data/autorss.log`（滚动 5 份，/logs 页有『下载完整日志』按钮）、
    /logs 页的实时视图、以及页面上的红字提示。

    仓库里早有解药（`services.fetch.redact`），可 R21 之前**生产代码只有 2 处在用**。
    全仓扫下来有 **77 个** `except … as e` 把异常写进日志 —— 逐处加必然漏，
    而"漏一处就等于没做"正是本项目反复栽的那种形状。装成过滤器之后，
    调用点写什么都不会漏，将来新增的 except 也自动被盖住。

    非日志的出口（返回给页面的 error、持久化进库的 fail_reason）过滤器盖不到，
    那几处仍然显式调 `fetch.redact`，见 core/manual.py、core/anime.py、core/movies.py。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        from services.fetch import redact          # 延迟导入：logsetup 在很早的启动期就被导入
        try:
            text = record.getMessage()
        except Exception:
            return True                            # 格式化不了就别拦，交给 handler 自己报
        safe = redact(text)
        if safe != text:
            # 【连 args 一起清掉】只改 msg 而留着 args，handler 会拿 msg % args 再格式化一次，
            # 原文又回来了（第一版就是这么假绿的）。
            record.msg, record.args = safe, ()
        if record.exc_info and not record.exc_text:
            # 异常栈里同样会出现 URL（httpx 的异常消息就在 traceback 末行）。
            # 先渲染成字符串再脱敏；`Formatter.format` 见到 exc_text 非空就跳过 formatException，
            # 所以栈照样是脱敏后的那一份。
            #
            # 【绝不能把 exc_info 清成 None】(R22) 本过滤器挂在三个 handler 上，其中 ring 挂在
            # 'autorss' logger 上，而 `logging.callHandlers` 先走本 logger 的 handler 再往 root 传
            # —— ring 上这一个**总是第一个**跑。清掉 exc_info 之后，root 那两个 handler 上的
            # `_SuppressDeletedSlot.filter` 里 `record.exc_info[1] if record.exc_info else None`
            # 恒为 None，它要滤的那一族 NiceGUI 断连噪声（消息体是通用的
            # `按钮操作失败：%s`，特征全在异常里）**整个失效**，日志被刷屏并掩盖真错。
            import logging as _l
            record.exc_text = redact(_l.Formatter().formatException(record.exc_info))
        return True


class RingHandler(logging.Handler):
    """把最近 capacity 条日志留在内存里，供 /logs 页读取。每条存 {levelno, level, line}。"""

    def __init__(self, capacity: int):
        super().__init__()
        self.buf: deque = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            item = {"levelno": record.levelno, "level": record.levelname,
                    "line": self.format(record)}
        except Exception:
            self.handleError(record)
            return
        self.buf.append(item)      # deque(maxlen) 满则挤掉最老的，天然限长

    def snapshot(self) -> list:
        """线程安全地取当前缓冲快照（后台协程在写、页面在读）。"""
        with self.lock:
            return list(self.buf)


ring = RingHandler(_RING_CAPACITY)
_configured = False


def setup_logging() -> None:
    """装配根日志器（控制台+滚动文件）与 'autorss' 环形缓冲。重复调用只装一次。"""
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    filt = _SuppressDeletedSlot()
    redact_filt = _RedactUrls()

    handlers = [logging.StreamHandler()]
    try:
        handlers.append(RotatingFileHandler(
            LOG_PATH, maxBytes=_FILE_MAXBYTES, backupCount=_FILE_BACKUPS, encoding="utf-8"))
    except OSError as e:              # 落不了盘（目录只读等）也别拖垮启动，退化成控制台+内存
        root.warning("日志文件无法创建（%s），仅用控制台与内存缓冲", e)
    for h in handlers:
        h.setFormatter(_FMT)
        h.addFilter(filt)
        h.addFilter(redact_filt)
        root.addHandler(h)

    # 【httpx 的 INFO 访问日志压到 WARNING】它对每个请求记一行完整 URL，而 qB 的
    # torrents/info 是把几十上百个 hash 拼进 query 的——单条就 4KB 以上，每 QB_SYNC_INTERVAL
    # 一次，一天能写十几 MB，远超本文件总共 12MB 的保留量：真正想看的采集/下载日志会被
    # 挤出滚动窗口，而下载下来的日志 99% 是 URL。warning 及以上照常保留。
    # （同款处理见 db/schema.py 对 alembic 的静音。）
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    ring.setFormatter(_FMT)
    # 【环形缓冲也要挂】(R21) 它挂在 'autorss' logger 上，不经过 root 的那几个 handler ——
    # 只给 root 挂过滤器的话，/logs 页的实时视图仍然明文显示 passkey。
    ring.addFilter(redact_filt)
    logging.getLogger("autorss").addHandler(ring)  # 只收本应用日志（各模块都用这个 logger 名）
    _configured = True
