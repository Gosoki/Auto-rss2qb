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

    handlers = [logging.StreamHandler()]
    try:
        handlers.append(RotatingFileHandler(
            LOG_PATH, maxBytes=_FILE_MAXBYTES, backupCount=_FILE_BACKUPS, encoding="utf-8"))
    except OSError as e:              # 落不了盘（目录只读等）也别拖垮启动，退化成控制台+内存
        root.warning("日志文件无法创建（%s），仅用控制台与内存缓冲", e)
    for h in handlers:
        h.setFormatter(_FMT)
        h.addFilter(filt)
        root.addHandler(h)

    ring.setFormatter(_FMT)
    logging.getLogger("autorss").addHandler(ring)  # 只收本应用日志（各模块都用这个 logger 名）
    _configured = True
