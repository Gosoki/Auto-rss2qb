"""qBittorrent Web UI 客户端。"""
import os

import requests

from config import QB_PASSWORD, QB_URL, QB_USERNAME
from logger import log


class QBittorrent:
    def _login(self):
        """登录并返回带会话的 requests.Session；失败返回 None。"""
        session = requests.Session()
        try:
            resp = session.post(
                f"{QB_URL}/api/v2/auth/login",
                data={"username": QB_USERNAME, "password": QB_PASSWORD},
                timeout=30,
            )
        except requests.RequestException as e:
            log.error(f"qBittorrent 登录请求失败: {e}")
            return None
        if resp.status_code != 200:
            log.error(f"qBittorrent 登录失败: {resp.status_code} {resp.text}")
            return None
        return session

    def add_torrent(self, save_path, torrent_file, quarter):
        """把种子加入 qBittorrent。成功返回 True。"""
        if not os.path.exists(save_path):
            os.makedirs(save_path, exist_ok=True)
            # 保留原行为：放开权限，便于以其它用户运行的 qB 写入（如需收紧可自行调整）
            os.chmod(save_path, 0o777)

        session = self._login()
        if session is None:
            return False

        try:
            with open(torrent_file, "rb") as f:
                resp = session.post(
                    f"{QB_URL}/api/v2/torrents/add",
                    data={
                        "savepath": save_path,
                        "autoTMM": "false",   # 禁用自动分类
                        "paused": "false",     # 立即开始下载
                        "category": f"autoRSS {quarter}",
                        "tags": quarter,
                    },
                    files={"torrents": f},
                    timeout=60,
                )
        except requests.RequestException as e:
            log.error(f"添加下载任务请求失败: {e}")
            return False

        if resp.status_code == 200:
            return True
        log.error(f"添加下载任务失败: {resp.text}")
        return False
