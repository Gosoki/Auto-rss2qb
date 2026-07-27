"""qBittorrent Web UI 客户端（异步）。

会话复用：登录一次、缓存已登录 client（cookie），到 TTL / 配置变更 / 403(失效) 才重登——把活跃下载期
『每次 API 调用都重登』(每分钟数次) 降到每 TTL 一次，减小 qB 压力与失败登录风控风险。
"""
import asyncio
import logging
import time

import httpx

import config

log = logging.getLogger("autorss")

# torrents_info 每批最多带多少个 hash（拼进 query string，见该函数注释）。
# 100×41≈4.1KB URL，稳在常见 8K 请求行上限的一半以内（200 就顶到 8.2KB、正好压线）。
# 代价只是多几个 GET：2000 个在下种子＝20 次请求，对 30s 一轮的同步毫无压力。
_INFO_BATCH = 100

_SESSION_TTL = 1800   # 复用已登录 client 的秒数；取小于 qB 默认 cookie 寿命(约 1h)，到点主动重登


class QBittorrent:
    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._auth = None                 # 缓存时的 (url, user, pass)；变了即重登
        self._logged_at = 0.0
        self._lock = asyncio.Lock()       # 串行化登录，防并发重复登录

    async def _login(self) -> httpx.AsyncClient | None:
        """新建并登录一个 AsyncClient；成功返回它（不 aclose，交缓存复用），失败返回 None 并清理。"""
        try:
            client = httpx.AsyncClient(base_url=config.QB_URL, timeout=30)
        except Exception as e:                 # QB_URL 非法等 → 建 client 就抛，别逃逸成未处理异常
            log.error("qBittorrent 客户端创建失败（QB_URL 非法？）: %s", e)
            return None
        ok = False
        try:
            resp = await client.post(
                "/api/v2/auth/login",
                data={"username": config.QB_USERNAME, "password": config.QB_PASSWORD},
                headers={"Referer": config.QB_URL},
            )
            # qB 对 /auth/login 有三种回法：
            #   200 "Ok."    正常登录成功，Set-Cookie 下发 SID
            #   200 "Fails." 账号或密码错（403 则是失败次数过多被临时封 IP）
            #   204 空       该客户端【免鉴权】——qB 里勾了"对本机/白名单网段跳过身份验证"，
            #                压根没有会话可建，后续请求本就直接放行
            # 早先只认第一种，于是内网免鉴权的 qB（如 bypass_auth_subnet_whitelist=10.0.0.0/8）
            # 一律被判成"连不上"，保存设置时还会顺手把『发送到 qB』开关自动关掉。
            if resp.status_code == 204 or (resp.status_code == 200
                                           and resp.text.strip().lower().startswith("ok")):
                ok = True
                return client
            log.error("qBittorrent 登录失败: %s %s", resp.status_code, resp.text[:80])
            return None
        except httpx.HTTPError as e:
            log.error("qBittorrent 登录请求失败: %s", e)
            return None
        except Exception as e:                 # 非 HTTP 类异常(socket/URL 等)也别逃逸；CancelledError 是
            log.error("qBittorrent 登录异常: %s", e)   # BaseException 不被此捕获，仍正常向上传播
            return None
        finally:
            if not ok:
                await client.aclose()

    async def _close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def _ensure(self) -> httpx.AsyncClient | None:
        """返回可用的已登录 client（复用缓存；到 TTL / 配置变更才重登）；登录失败返回 None。"""
        auth = (config.QB_URL, config.QB_USERNAME, config.QB_PASSWORD)
        async with self._lock:
            if (self._client is not None and self._auth == auth
                    and time.monotonic() - self._logged_at < _SESSION_TTL):
                return self._client
            await self._close()
            client = await self._login()
            if client is not None:
                self._client, self._auth, self._logged_at = client, auth, time.monotonic()
            return client

    async def _invalidate(self) -> None:
        async with self._lock:
            await self._close()

    async def _request(self, method: str, path: str, **kw) -> httpx.Response | None:
        """带会话复用 + 一次 403 重登重试。返回 Response；连不上/登录失败/HTTP 传输错误返回 None。

        403 常见成因是 cookie 失效（qB 重启/超时）→ 清缓存重登重试一次即可恢复；若重试后仍 403
        （如 setLocation 无写权限这类【业务级】403）则原样返回，由调用方按状态码处理。"""
        for attempt in (1, 2):
            client = await self._ensure()
            if client is None:
                return None
            try:
                resp = await client.request(method, path, **kw)
            except httpx.TransportError as e:
                # 传输层错误再给一次机会：client 是【全进程共享】的，另一个协程走到会话轮换
                # （TTL 到点 / 认证被改 / 403 重登）时会直接 aclose 掉它，把【本协程此刻在途的】
                # 请求扯断成 ReadError/ClosedResourceError。实测能让一次 add_torrent 返回 False，
                # 而 qB 其实已经收下了种子。重试会走 _ensure 拿到新登录的 client。
                # 只对第一次尝试重试，避免 qB 真离线时每个请求都翻倍。
                if attempt == 1:
                    log.warning("qB 请求传输层失败 %s %s: %s —— 重试一次", method, path, e)
                    await self._invalidate()
                    continue
                log.error("qB 请求失败 %s %s: %s", method, path, e)
                return None
            except Exception as e:
                # 兜宽到 Exception 而不是只 httpx.HTTPError：httpx 有一批异常【不】继承 HTTPError，
                # 典型是 InvalidURL（query 过长等构造期错误）。它一旦漏出去就穿透 _request →
                # torrents_info → sync_qb_status，只在 worker 那层留一行"qB 状态同步异常"，
                # 本轮 0 行更新、in-flight 一条不减，下轮重复同样失败＝状态同步永久停摆。
                # 与同文件 _login 的兜法一致（那里早就用的 except Exception）。
                # 本函数对调用方的契约是"失败返回 None"，这里必须真的兑现。
                log.error("qB 请求失败 %s %s: %s", method, path, e)
                return None
            if resp.status_code == 403 and attempt == 1:
                await self._invalidate()
                continue
            return resp
        return None

    async def add_torrent(self, torrent_bytes: bytes, save_path: str, category: str, tags: str) -> bool:
        files = {"torrents": ("t.torrent", torrent_bytes, "application/x-bittorrent")}
        data = {"savepath": save_path, "autoTMM": "false", "paused": "false",
                "category": category, "tags": tags}
        resp = await self._request("POST", "/api/v2/torrents/add", data=data, files=files)
        if resp is None:
            return False
        # qB 的 add 成功回 "Ok."、失败回 "Fails."；两者皆 200，靠响应体区分。要求响应体【非空且不含 fail】
        # 才算成功——空体（反代/网关在 200 下塞的空响应）不当成功，落失败路径由 add_to_qb 据 info_hash 兜底核实。
        body = resp.text.strip().lower()
        if resp.status_code == 200 and body and "fail" not in body:
            return True
        # 200+Fails 最常见成因是『该 hash 已在 qB』(重复提交/跨表同种)，调用方 engine.add_to_qb 会据
        # info_hash 兜底判为已交付——故按 warning 记，别当 error 惊扰；真失败(坏种子)是 415/非 200 仍记 error。
        # 附上 save_path：远程 qB 若因路径不可写而拒，这行是唯一线索。
        (log.warning if resp.status_code == 200 else log.error)(
            "添加下载任务未接受 %s: %s（save_path=%s）", resp.status_code, resp.text[:80], save_path)
        return False

    async def add_url(self, url: str, save_path: str, category: str, tags: str) -> bool:
        """把 magnet 链接（或 http .torrent 链接）交给 qB 自己抓取下载（urls= 参数）。成功回 True。
        用于手动下载的 magnet：qB 支持 urls 收 magnet/URL；判成功同 add_torrent(200 且响应体不含 fail)。"""
        data = {"urls": url, "savepath": save_path, "autoTMM": "false", "paused": "false",
                "category": category, "tags": tags}
        resp = await self._request("POST", "/api/v2/torrents/add", data=data)
        if resp is None:
            return False
        body = resp.text.strip().lower()
        if resp.status_code == 200 and body and "fail" not in body:
            return True
        (log.warning if resp.status_code == 200 else log.error)(
            "添加下载任务(url)未接受 %s: %s（save_path=%s）", resp.status_code, resp.text[:80], save_path)
        return False

    async def torrents_info(self, hashes: list[str]) -> dict | None:
        """按 info_hash 批量查 qB 实时态，返回 {hash(小写): 种子dict}。连不上返回 None、无结果返回 {}。

        种子 dict 关键字段：state / progress(0..1) / dlspeed / size / eta / num_seeds…
        """
        hashes = [h for h in hashes if h]
        if not hashes:
            return {}
        # 【必须分批】hashes 是拼进 query string 的，条数没有上限：一个 40 位 hash + 分隔符 = 41 字节，
        # 1525 条就把 URL 顶过 httpx 的 MAX_URL_LENGTH(65536) 抛 InvalidURL，socket 都发不出去；
        # 而 qB 自己和中间反代对请求行的限制（常见 8K）比这低得多，才是真实环境里的第一个失效点。
        # 任一批失败就整体返回 None，保住上游"连不上则本轮不动"的语义——不能只把成功的那几批合起来
        # 返回，否则缺席的行会被 sync 当成"qB 里没有它"而走落定分支。
        out: dict = {}
        for i in range(0, len(hashes), _INFO_BATCH):
            resp = await self._request("GET", "/api/v2/torrents/info",
                                       params={"hashes": "|".join(hashes[i:i + _INFO_BATCH])})
            if resp is None:
                return None
            try:
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError) as e:
                log.error("查询种子状态失败: %s", e)
                return None
            if not isinstance(data, list):
                # 正常 qB 该端点恒返回数组。非列表(反代/网关在 200 下塞的错误 JSON/维护页等)按『连不上』处理、
                # 本轮不动——绝不能当成空 {}，否则上游会把在下种子逐个误判为 error（真在下却被标失败）。
                return None
            out.update({str(t.get("hash", "")).lower(): t for t in data if t.get("hash")})
        return out

    async def delete(self, hashes: list[str], delete_files: bool = True) -> bool:
        """按 info_hash 从 qB 删除种子；delete_files=True 连硬盘文件一起删。全空/成功返回 True。"""
        hashes = [h for h in hashes if h]
        if not hashes:
            return True
        resp = await self._request("POST", "/api/v2/torrents/delete", data={
            "hashes": "|".join(hashes),
            "deleteFiles": "true" if delete_files else "false",
        })
        if resp is None:
            return False
        if resp.status_code == 200:
            return True
        log.error("删除下载任务失败 %s: %s", resp.status_code, resp.text[:80])
        return False

    async def set_location(self, hashes: list[str], location: str) -> int | None:
        """把这些种子的保存路径整批移到 location（qB 后台搬文件，autoTMM=false 的不会被分类路径覆盖）。

        返回 HTTP 状态码：200 成功 / 400 空路径 / 403 无写权限 / 409 建目录失败；连不上返回 None、空 hashes 返回 200。
        403/409 是路径级失败（对整批一致）——调用方据此『只提醒、不动状态』。
        """
        hashes = [h for h in hashes if h]
        if not hashes:
            return 200
        resp = await self._request("POST", "/api/v2/torrents/setLocation", data={
            "hashes": "|".join(hashes),
            "location": location,
        })
        if resp is None:
            return None
        if resp.status_code != 200:
            log.error("移动种子位置失败 %s: %s", resp.status_code, resp.text[:80])
        return resp.status_code
