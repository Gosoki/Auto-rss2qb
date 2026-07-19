"""qBittorrent Web UI 客户端（异步）。"""
import logging

import httpx

from config import QB_PASSWORD, QB_URL, QB_USERNAME

log = logging.getLogger("autorss")


class QBittorrent:
    async def _login(self) -> httpx.AsyncClient | None:
        """返回已登录的 AsyncClient；失败返回 None（成功时由调用方负责 aclose）。"""
        client = httpx.AsyncClient(base_url=QB_URL, timeout=30)
        ok = False
        try:
            resp = await client.post(
                "/api/v2/auth/login",
                data={"username": QB_USERNAME, "password": QB_PASSWORD},
                headers={"Referer": QB_URL},
            )
            if resp.status_code == 200 and resp.text.strip().lower().startswith("ok"):
                ok = True
                return client
            log.error("qBittorrent 登录失败: %s %s", resp.status_code, resp.text[:80])
            return None
        except httpx.HTTPError as e:
            log.error("qBittorrent 登录请求失败: %s", e)
            return None
        finally:
            # 任何非成功路径（含 CancelledError 等向上抛的异常）都关掉连接，防泄漏
            if not ok:
                await client.aclose()

    async def add_torrent(self, torrent_bytes: bytes, save_path: str, category: str, tags: str) -> bool:
        client = await self._login()
        if client is None:
            return False
        try:
            files = {"torrents": ("t.torrent", torrent_bytes, "application/x-bittorrent")}
            data = {
                "savepath": save_path,
                "autoTMM": "false",
                "paused": "false",
                "category": category,
                "tags": tags,
            }
            resp = await client.post("/api/v2/torrents/add", data=data, files=files)
        except httpx.HTTPError as e:
            log.error("添加下载任务失败: %s", e)
            return False
        finally:
            await client.aclose()

        # qB 的 add 失败时也返回 200，靠响应体区分（成功 "Ok."，失败 "Fails."）
        if resp.status_code == 200 and "fail" not in resp.text.strip().lower():
            return True
        log.error("添加下载任务失败 %s: %s", resp.status_code, resp.text[:80])
        return False
