"""入口脚本（保留原文件名，沿用 `python auto-rss.py` 运行）。

实际逻辑已拆分到各模块，见 README.md：
    config.py / logger.py / db.py / qbittorrent.py / rss.py / main.py
"""
from main import run

if __name__ == "__main__":
    run()
