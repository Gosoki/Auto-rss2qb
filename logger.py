"""日志。

`log` 是全局唯一的日志对象：按天写入 `LOG_PATH/YYYY-MM-DD.log`，
WARNING 及以上额外汇总到 `LOG_PATH/warnings.log`，并触发一条通知。
其它模块统一 `from logger import log` 使用，全程共用同一实例。
"""
import logging
import os
from datetime import datetime

from config import LOG_PATH
from notify import notify


def now_time():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def today():
    return datetime.now().strftime("%Y-%m-%d")


class Log:
    def __init__(self):
        self.is_sleep = False       # 休眠期间 info 只打印控制台，不写文件，避免刷屏
        self._day = today()
        self._logger = self._build()

    def _build(self):
        os.makedirs(LOG_PATH, exist_ok=True)
        logger = logging.getLogger("autorss")
        logger.setLevel(logging.DEBUG)
        # 清除旧处理器，避免跨天重建时累积
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)

        fmt = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )

        file_handler = logging.FileHandler(f"{LOG_PATH}/{self._day}.log", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(fmt)
        logger.addHandler(console_handler)

        warning_handler = logging.FileHandler(f"{LOG_PATH}/warnings.log", encoding="utf-8")
        warning_handler.setLevel(logging.WARNING)
        warning_handler.setFormatter(fmt)
        logger.addHandler(warning_handler)

        return logger

    def _rollover(self):
        day = today()
        if day != self._day:
            self._day = day
            self._logger = self._build()

    def info(self, message):
        self._rollover()
        if self.is_sleep:
            print(f"{now_time()} - INFO - {message}")  # 休眠期：只打印，不写文件
        else:
            self._logger.info(message)

    def warning(self, message):
        self._rollover()
        self._logger.warning(message)
        notify(f"WARNING - {message}")

    def error(self, message):
        self._rollover()
        self._logger.error(message)
        notify(f"ERROR - {message}")

    def critical(self, message):
        self._rollover()
        self._logger.critical(message)


# 全局唯一实例
log = Log()
