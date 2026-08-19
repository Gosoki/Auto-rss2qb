"""qBittorrent Web UI 客户端（异步）。

会话复用：登录一次、缓存已登录 client（cookie），到 TTL / 配置变更 / 403(失效) 才重登——把活跃下载期
『每次 API 调用都重登』(每分钟数次) 降到每 TTL 一次，减小 qB 压力与失败登录风控风险。
"""
import asyncio
import json
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

# 【登录失败的负缓存】qB 自带失败登录封禁（默认 5 次失败 → 封该 IP 3600 秒）。
# 而本程序的两条后台线（anime + movies 的状态同步）每 QB_SYNC_INTERVAL（默认 30s）各要一次会话，
# 用户改了密码或 qB 重启后，约 75 秒就能凑够 5 次失败把自己封进去——之后即使把密码改对，
# 设置页的连接探测也会失败，还会顺手把『发送到 qB』开关自动关掉，看起来像"密码明明是对的却连不上"。
# 凭据类失败退避得久（改密码是人工动作，不必抢），网络类失败退避得短（qB 重启很快回来）。
_LOGIN_COOLDOWN_AUTH = 300    # 200 'Fails.'（账号密码错）/ 403（已被封）
_LOGIN_COOLDOWN_NET = 30      # 连不上/超时


def _add_accepted(resp: httpx.Response) -> bool:
    """/torrents/add 是否被受理。两代 qB 的回法完全不同，都要认。

    · qB ≤5.0：200 + 纯文本 "Ok." / "Fails."
    · qB 5.1+：200 + JSON，实测 5.2.3 回
          {"added_torrent_ids":["<hash>"],"failure_count":0,"pending_count":0,"success_count":1}
      重复提交、种子/磁链非法则一律 409 Conflict（纯文本）。

    【这里踩过的坑】原先只按文本判，且拿"响应体里不含 fail"当成功标记——新版 JSON 里
    "failure_count" 这个字段名正好含 fail，于是【每一次成功的 add 都被判成失败】：
    先记一条 WARNING，再走 add_to_qb 的兜底去查 hash 在不在 qB。而 qB 是异步入库的，
    刚 add 完立刻查 torrents/info 有时还没列出来 → 兜底扑空 → 好端端下上了的种子被标 error。
    表现就是"失败数莫名其妙地多"，且时好时坏（取决于那一下有没有赶上）。
    """
    if resp.status_code != 200:
        return False
    body = resp.text.strip()
    if body.startswith("{"):
        try:
            d = json.loads(body)
        except ValueError:
            return False
        return bool(d.get("added_torrent_ids") or d.get("success_count"))
    # 空体（反代/网关在 200 下塞的空响应）不当成功：落失败路径，由 add_to_qb 据 info_hash 兜底核实。
    return bool(body) and "fail" not in body.lower()


class QBittorrent:
    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._auth = None                 # 缓存时的 (url, user, pass)；变了即重登
        self._logged_at = 0.0
        self._lock = asyncio.Lock()       # 串行化登录，防并发重复登录
        self._fail_until = 0.0            # 负缓存：在这个时刻之前不再尝试登录（见 _LOGIN_COOLDOWN_*）

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
            # 凭据错 / 已被封 → 长冷却。再撞下去只会把封禁时间续上，且用户改对密码后仍连不上。
            self._fail_until = time.monotonic() + _LOGIN_COOLDOWN_AUTH
            log.error("qBittorrent 登录失败: %s %s（%d 秒内不再重试；改完账号密码点『保存』可立即重试）",
                      resp.status_code, resp.text[:80], _LOGIN_COOLDOWN_AUTH)
            return None
        except httpx.HTTPError as e:
            self._fail_until = time.monotonic() + _LOGIN_COOLDOWN_NET   # 网络类：短冷却，qB 重启很快回来
            log.error("qBittorrent 登录请求失败: %s", e)
            return None
        except Exception as e:                 # 非 HTTP 类异常(socket/URL 等)也别逃逸；CancelledError 是
            self._fail_until = time.monotonic() + _LOGIN_COOLDOWN_NET
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
            if time.monotonic() < self._fail_until:
                return None            # 冷却中：直接当"连不上"，调用方本就按三态处理（None=留待下轮）
            client = await self._login()
            if client is not None:
                self._client, self._auth, self._logged_at = client, auth, time.monotonic()
                self._fail_until = 0.0                      # 登录成功即解除冷却
            return client

    async def _invalidate(self) -> None:
        async with self._lock:
            await self._close()

    async def reset_cooldown(self) -> None:
        """解除登录失败的负缓存。设置页保存 qB 配置后调它——用户刚改完账号密码，
        不该还被自己上一次的失败冷却挡着（冷却是为了别把 qB 的封禁计数撞满，不是惩罚用户）。"""
        async with self._lock:
            self._fail_until = 0.0
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

    async def reachable(self) -> bool:
        """qB 现在连得上且登得进吗。给交付前的预检用：一次 GET，比试着发一整批种子便宜得多。"""
        return await self._request("GET", "/api/v2/app/version") is not None

    async def add_torrent(self, torrent_bytes: bytes, save_path: str,
                          category: str, tags: str) -> bool | None:
        """True=已受理，False=qB 明确拒了这一条，【None=根本没连上 qB】。

        三态而不是两态：连不上是【暂时性】的（qB 重启/网络抖动），种子本身没问题，
        调用方该把它留在待下、下轮再来；而『被拒』多半是这一条自己的毛病（坏种子、路径不可写），
        重试多少次都一样。两者都揉成 False，就会在 qB 掉线时把整批种子打成 error 要人工补下。
        """
        files = {"torrents": ("t.torrent", torrent_bytes, "application/x-bittorrent")}
        # 【paused 与 stopped 都发】qB 5.0 把这个参数改名成 stopped 且【没有保留别名】。
        # 只发 paused 的话，用户在 qB 里开了 AddTorrentStopped 时种子会以 stoppedDL 入库——
        # 那是 W 档（不计停滞），于是该行永久 in-flight、永不下完也永不报错。
        # 两个都带是安全的：老版忽略未知的 stopped，新版忽略未知的 paused。
        data = {"savepath": save_path, "autoTMM": "false",
                "paused": "false", "stopped": "false",
                "category": category, "tags": tags}
        resp = await self._request("POST", "/api/v2/torrents/add", data=data, files=files)
        if resp is None:
            return None
        if _add_accepted(resp):
            return True
        # 被拒最常见的成因是『该 hash 已在 qB』(重复提交/跨表同种；新版回 409 Conflict)，调用方
        # engine.add_to_qb 会据 info_hash 兜底判为已交付——故 200 按 warning 记，别当 error 惊扰。
        # 响应体留 200 字：新版失败时的 failure_reasons 才不会被截掉（那是唯一的失败原因线索）。
        # 附上 save_path：远程 qB 若因路径不可写而拒，这行是另一条线索。
        # 200('Fails.') 与 409(Conflict) 都是"该 hash 已在 qB"的正常表达（新旧版本各一种），
        # 调用方 engine.add_to_qb 会据 info_hash 兜底判为已交付——按 warning 记，别让"仅错误"
        # 日志档里出现一堆正常事件。
        (log.warning if resp.status_code in (200, 409) else log.error)(
            "添加下载任务未接受 %s: %s（save_path=%s）", resp.status_code, resp.text[:200], save_path)
        return False

    async def add_url(self, url: str, save_path: str, category: str, tags: str) -> bool | None:
        """把 magnet 链接（或 http .torrent 链接）交给 qB 自己抓取下载（urls= 参数）。
        三态与 add_torrent 完全一致：True=已受理 / False=qB 明确拒了 / None=根本没连上
        （理由见 add_torrent 的说明；两者揉成 False 会让调用方把『qB 掉线』误诊成『种子有问题』）。"""
        data = {"urls": url, "savepath": save_path, "autoTMM": "false",
                "paused": "false", "stopped": "false",   # 理由同 add_torrent
                "category": category, "tags": tags}
        resp = await self._request("POST", "/api/v2/torrents/add", data=data)
        if resp is None:
            return None
        if _add_accepted(resp):
            return True
        (log.warning if resp.status_code in (200, 409) else log.error)(
            "添加下载任务(url)未接受 %s: %s（save_path=%s）", resp.status_code, resp.text[:200], save_path)
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
                # 【只记异常类型与状态码】httpx 的异常串里带完整 URL，而本端点的 query 是把
                # 几十上百个 hash 用 | 拼起来的——原样记下去单条日志就能上 4KB，一天十几 MB，
                # 把真正想看的采集/下载日志挤出滚动窗口。
                code = getattr(getattr(e, "response", None), "status_code", "—")
                log.error("查询种子状态失败：%s（HTTP %s，%d 个 hash）",
                          type(e).__name__, code, len(hashes[i:i + _INFO_BATCH]))
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
