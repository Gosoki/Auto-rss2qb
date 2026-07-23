"""TV 番剧与剧场版/OVA 共用的底层引擎：下载原语 / qB 客户端与状态同步 / bgm 元数据落库 / 路径季度。

anime.py(TV) 与 movies.py(剧场版) 都依赖这里；本模块不含任何 TV/movie 业务分支，纯共用，
两条线因此互不相干又不重复造轮子。
"""
import asyncio
import ipaddress
import logging
import os
import re
import socket
from datetime import datetime, timedelta

import httpx
from sqlmodel import func, or_, select

import config
from db import get_session
from db.models import AnimeTorrent, MovieTorrent, Setting
from services.qbittorrent import QBittorrent
from sources.parse import format_quarter

log = logging.getLogger("autorss")

qb = QBittorrent()

# 有种子交付给 qB 时 set()，唤醒 qB 同步循环立即开始跟；平时循环停在这上面休眠（见 worker.run_qb_sync）
qb_kick = asyncio.Event()

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_QUARTER_KEY_RE = re.compile(r"(\d{2})([A-D])")
_TORRENT_CAP = 32 * 1024 * 1024  # .torrent 通常 < 1MB，32MB 已是极宽松上限

# 写回 Anime/Movie 的 bgm 字段（两者同名）；season/kind 等各自专属，不在此
_BGM_FIELDS = ("bangumi_id", "display_name", "jp_name", "air_date", "air_weekday",
               "total_episodes", "platform", "cover_url", "rating", "summary",
               "author", "director", "music", "cast")


# ---------------- 文件名 / 季度 ----------------

def safe_name(name: str) -> str:
    """清洗成安全的单段文件夹名：去非法字符/控制符，并挡掉 '.'/'..' 路径穿越。"""
    cleaned = _ILLEGAL.sub("_", name or "").strip().strip(".").strip()
    return cleaned or "unknown"


def quarter_folder(quarter: str) -> str:
    """内部季度键(26C) → 下载文件夹用的季度目录名（config.QUARTER_FMT）。"""
    return format_quarter(quarter, config.QUARTER_FMT)


def quarter_label(quarter: str) -> str:
    """内部季度键(26C) → 页面显示用的季度名（config.QUARTER_FMT_UI）。"""
    return format_quarter(quarter, config.QUARTER_FMT_UI)


def prev_quarter(q: str) -> str:
    """上一个季度键：26C→26B，26A→25D（A 是年内第一季）。解析不出回空串。"""
    m = _QUARTER_KEY_RE.fullmatch(q or "")
    if not m:
        return ""
    yy, letter = int(m.group(1)), m.group(2)
    if letter == "A":
        return f"{yy - 1:02d}D"
    return f"{yy}{chr(ord(letter) - 1)}"


def build_save_path(quarter: str, folder_name: str, season: int | None = None,
                    top: str = "", root: str = "") -> str | None:
    """下载保存路径：根/[分类]/季度目录/番名[/Season N]。做 realpath 包含校验，越界返回 None。

    root=下载根（空=config.DOWN_PATH）。top=分类顶层目录（番剧/剧场版）——仅在用默认根时加；
    若给了独立 root（如电影专属目录），该 root 本身就是专属目录，不再套分类层。越界校验按实际根来。
    """
    base = root or config.DOWN_PATH
    parts = [base]
    if top and not root:
        parts.append(safe_name(top))
    parts += [safe_name(quarter_folder(quarter or "unknown")), safe_name(folder_name)]
    if season is not None and config.ANIME_SEASON_SUBFOLDER:
        parts.append(f"Season {int(season)}")
    save_path = os.path.join(*parts)
    base_real = os.path.realpath(base)
    real = os.path.realpath(save_path)
    if real != base_real and not real.startswith(base_real + os.sep):
        return None
    return save_path


# ---------------- bgm 元数据落库 ----------------

def apply_bgm_meta(obj, info: dict | None, keep_quarter: bool = False) -> None:
    """把 enrich 结果写进 obj（Anime 或 Movie，bgm 字段同名）——只覆盖非空值。

    keep_quarter=True（手动重识别、且已有季度）时不动季度——季度是归档路径的一部分，
    确定后应保持稳定，否则已下分集会散落到另一个季度目录。season/kind 等专属字段由各线自理。
    """
    if not info:
        return
    for k in _BGM_FIELDS:
        v = info.get(k)
        if v is not None:
            setattr(obj, k, v)
    if info.get("quarter") and not (keep_quarter and obj.quarter):
        obj.quarter = info["quarter"]


# ---------------- 下载原语（取种子 + 交 qB） ----------------

def _ip_is_internal(ip) -> bool:
    """ipaddress 对象是否属内网/环回/链路本地/保留等不可路由到公网的范围。
    IPv4-mapped IPv6（::ffff:127.0.0.1）先归一到内嵌 IPv4 再判，防映射写法绕过。"""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


async def _host_is_internal(host: str) -> bool:
    """host（字面 IP 或域名）会不会连到内网/环回地址。

    字面 IP 直接判；其余（域名，以及十进制 2130706433 / 0x7f000001 / 0177.0.0.1 等非点分整数写法）
    交给 getaddrinfo 实际解析、对每个解析地址逐一判——这些花式写法会被解析成真实内网 IP 从而被拦，
    指向内网的域名同样被拦（弥补『只拦字面 IP』的绕过面）。解析失败/无结果保守视作内网并拒（反正也连不上）。
    """
    host = (host or "").strip("[]")            # 去 IPv6 字面量方括号
    try:
        return _ip_is_internal(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError, ValueError):
        return True
    if not infos:
        return True
    for info in infos:
        try:
            if _ip_is_internal(ipaddress.ip_address(info[4][0])):
                return True
        except ValueError:
            return True                        # 解析出无法识别的地址形态 → 保守拒
    return False


async def _block_internal_request(request: httpx.Request) -> None:
    """请求级钩子：种子下载不许打到内网/环回地址（含重定向后的每一跳）——挡住 RSS 里的 SSRF 载荷。
    已配代理时目标由代理侧解析、本地判定既无意义又会误伤，跳过。"""
    if config.PROXY:
        return
    if await _host_is_internal(request.url.host or ""):
        raise ValueError(f"拒绝下载到内网/环回地址（防 SSRF）：{request.url.host}")


async def fetch_torrent_bytes(url: str) -> bytes:
    """流式下载 .torrent，封顶 32MB + 整体 180s 超时（download_url 源自 RSS 可被投毒 + 跟随重定向）。

    httpx 的 timeout=60 只是每次读的超时、逐块重置，慢速 trickle 连接能让它无限挂起并堵死整个下载/
    采集循环；故再套一层 asyncio.timeout 对总传输时长封顶。取到返回 bytes；HTTP/超限/超时失败抛异常，
    由调用方回写 error。请求级钩子额外挡内网/环回字面 IP（防 SSRF，含重定向每一跳）。
    """
    if not (url or "").lower().startswith(("http://", "https://")):
        raise ValueError(f"拒绝非 http(s) 下载地址（防 SSRF）：{(url or '')[:80]}")
    kwargs = config.http_client_kwargs(60)
    kwargs["event_hooks"] = {"request": [_block_internal_request]}
    async with asyncio.timeout(180):
        async with httpx.AsyncClient(**kwargs) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf += chunk
                    if len(buf) > _TORRENT_CAP:
                        raise ValueError(f"种子文件超过 {_TORRENT_CAP} 字节，疑似非法下载地址")
                return bytes(buf)


def torrent_time(t) -> str:
    """种子入库/发布时间的统一短显示：优先放送时间，退回创建时间，截到分钟。"""
    return str(t.release_time or t.created_at)[:16]


def set_torrent_status(model_cls, tid: int, status: str) -> None:
    """把某条种子（AnimeTorrent/MovieTorrent 任一）的状态置为 status。"""
    with get_session() as s:
        t = s.get(model_cls, tid)
        if t is not None:
            t.status = status
            s.add(t)
            s.commit()


def settle_downloaded(model_cls, tid: int) -> None:
    """把交付成功的种子直接落定为『已下完』(status=downloaded, qb_progress=1，脱离 in-flight)。
    关状态跟踪(QB_SYNC_STATUS=off)时用：发送即已下、不轮询 qB——若不落定 qb_progress，它会永久满足
    _inflight_where(progress<1 且 state 空未落定)，永远挂在『正在下载』区、且 has_inflight 恒真。"""
    with get_session() as s:
        t = s.get(model_cls, tid)
        if t is not None:
            t.status, t.qb_progress, t.qb_state, t.qb_synced_at = "downloaded", 1.0, "", datetime.now()
            s.add(t)
            s.commit()


def settle_inflight_off() -> int:
    """关闭 qB 状态跟踪(QB_SYNC_STATUS→off)或发送(QB_ENABLED→off)时，一次性把当前所有『在下的』种子
    落定为已下完(status=downloaded、qb_progress=1、脱离 in-flight)。返回落定数。

    off 之后 sync 内层循环不再运行(见 worker.run_qb_sync 的 while 条件)，这批『on 模式交付、进度未满』的行
    再无路径推进 → 会永久满足 _inflight_where、恒挂『正在下载』区、has_inflight 恒真。此处一次性落定，语义与
    settle_downloaded/『off=发送即已下』一致。settle_downloaded 只对【新交付】单条生效，故切换时刻的旧行须靠这里兜。"""
    n = 0
    now = datetime.now()
    with get_session() as s:
        for model_cls in (AnimeTorrent, MovieTorrent):
            for t in s.exec(select(model_cls).where(*_inflight_where(model_cls))):
                t.status, t.qb_progress, t.qb_state, t.qb_synced_at = "downloaded", 1.0, "", now
                s.add(t)
                n += 1
        s.commit()
    if n:
        log.info("关闭 qB 跟踪/发送：落定 %d 条在下种子为已下完（脱离 in-flight）", n)
    return n


def reset_downloading(model_cls) -> None:
    """启动时把某种子表上次异常退出遗留的 downloading 复位为 pending，好被重新下。"""
    with get_session() as s:
        for t in s.exec(select(model_cls).where(model_cls.status == "downloading")):
            t.status = "pending"
            s.add(t)
        s.commit()


def pick_best(torrents, pref=None):
    """从候选种子里挑一份：钉了首选源就优先它（没有才退回全部），再按（优先级降序, 入库时间升序）取第一。

    调用方保证 torrents 非空（TV 选集 / 剧场版审批下载都先筛过 pending）。
    """
    cands = torrents
    if pref:
        cands = [t for t in torrents if pref == (t.source or "")] or torrents
    return sorted(cands, key=lambda t: (-(t.priority or 0), t.created_at))[0]


def hash_owned_elsewhere(info_hash: str, other_model) -> bool:
    """该 info_hash 在另一张表里是否仍被持有(downloading/downloaded)。

    TV 与剧场版两条独立管线偶有同一物理种子（同 hash，如某剧场版也被 ANi 按集发）。删文件前查一下：
    对面还在用就别 qB-delete(deleteFiles) 把共享的种子/硬盘文件一起端了，只在本表脱手即可。
    """
    with get_session() as s:
        return s.exec(select(other_model).where(
            other_model.info_hash == info_hash,
            other_model.status.in_(["downloaded", "downloading"]))).first() is not None


async def add_to_qb(data: bytes, save_path: str, category: str, tags: str) -> bool:
    """尽力建目录（跨用户的 qB 需要，失败不阻断）+ 把种子加入 qB。返回是否成功。"""
    try:
        os.makedirs(save_path, exist_ok=True)
        os.chmod(save_path, 0o777)
    except OSError:
        pass
    return await qb.add_torrent(data, save_path, category, tags)


# ---------------- qB 实时态（对 AnimeTorrent / MovieTorrent 通用） ----------------

# 下载态含 qB 5.x 新增的 forcedMetaDL；做种态含 5.x 改名后的 stoppedUP（=已完成暂停做种）。
# 暂停未完成的 pausedDL/stoppedDL 有意不计入下载、也不计入做种（既非在下也非已完成）。
_QB_DOWNLOADING = {"downloading", "forcedDL", "metaDL", "forcedMetaDL", "stalledDL",
                   "queuedDL", "checkingDL", "allocating"}
_QB_SEEDING = {"uploading", "forcedUP", "stalledUP", "queuedUP", "checkingUP",
               "pausedUP", "stoppedUP"}  # 含已完成（暂停做种）
# 『落定』态：不再需要轮询跟踪的种子 qB 态——做种(=下载已完成) + 文件缺失(终态、不会再变)。
# in-flight 判定与 sync 查询都据此把它们排除，使『停止监听』对做种/缺文件都生效。
_QB_SETTLED = _QB_SEEDING | {"missingFiles"}
# 短暂『工作中』态（在动但速度可能为 0：取元数据/校验/分配磁盘）——这些也算『在真下』，
# 免得刚开始那几秒被速度地板误判成慢。真正的下载态(downloading/forcedDL)则改用速度地板判快慢。
_QB_TRANSIENT = {"metaDL", "forcedMetaDL", "checkingDL", "allocating"}


def qb_is_downloading(state: str) -> bool:
    return state in _QB_DOWNLOADING


def qb_is_seeding(state: str) -> bool:
    return state in _QB_SEEDING


def _inflight_where(model_cls):
    """『在下的种子』筛选条件（sync 查询与 has_inflight 共用，口径一致）：
    已交付(downloaded/downloading) 且 进度<100% 且 qB 态未落定(非做种/非文件缺失)。
    进度满/做种(已完成)/文件缺失 都算落定 → 不再轮询，qB 压力只随『当前在下数』走。"""
    return (
        model_cls.status.in_(["downloaded", "downloading"]),
        model_cls.qb_progress < 1.0,
        func.coalesce(model_cls.qb_state, "").not_in(list(_QB_SETTLED)),
    )


def has_inflight() -> bool:
    """还有没有『在下的』种子（TV 或剧场版任一）——供 worker 决定要不要继续轮询、还是休眠。"""
    with get_session() as s:
        for model_cls in (AnimeTorrent, MovieTorrent):
            if s.exec(select(model_cls.id).where(*_inflight_where(model_cls)).limit(1)).first():
                return True
    return False


def has_active_downloading() -> bool:
    """在下的种子里有没有『正在真下』的——决定要不要维持高频轮询。
    判据：下载速度 ≥ 慢速地板(QB_ACTIVE_FLOOR_KBPS)，或处于短暂工作态(取元数据/校验/分配)。
    stalled(无源,0速)/排队/慢速爬行 都不算 → 只剩这些时快循环退回休眠、交给保底，别空转钉住循环。
    （注意：只要还有一个『在真下』，sync 每轮批量更新会顺便把慢的/stalled 的也一起刷新，不会漏。）"""
    thr = max(1, config.QB_ACTIVE_FLOOR_KBPS * 1024)   # KB/s→B/s；地板设 0 时=至少要有速度(≥1B/s)才算在真下
    # 新鲜度闸：只认『最近一次同步够新』的种子为在真下。qB 掉线时 sync 走 None 分支不刷新 qb_synced_at、
    # 速度值变陈旧——若不设闸，陈旧的高速值会让 has_active 恒真、内层循环永不退出、对着死掉的 qB 每轮空打。
    # qB 在线的正常路径每轮都刷新 qb_synced_at(≤QB_SYNC_INTERVAL 秒)，远在窗口内、此闸永不误伤，行为等价。
    cutoff = datetime.now() - timedelta(seconds=max(120, config.QB_SYNC_INTERVAL * 3))
    with get_session() as s:
        for model_cls in (AnimeTorrent, MovieTorrent):
            if s.exec(select(model_cls.id).where(
                    *_inflight_where(model_cls),
                    model_cls.qb_synced_at >= cutoff,
                    or_(model_cls.qb_dlspeed >= thr,
                        model_cls.qb_state.in_(list(_QB_TRANSIENT)))).limit(1)).first():
                return True
    return False


def mark_done_by_hash(info_hash: str) -> bool:
    """把某 info_hash 的种子标记为『已下完』(qb_progress=1、脱离 in-flight)——供 qB『完成时回调』精确兜底。
    只认我们自己表里已交付(downloaded/downloading)的种子；非法 hash / 非我们的 / 已终态 返回 False。"""
    h = (info_hash or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", h):
        return False
    with get_session() as s:
        for model_cls in (AnimeTorrent, MovieTorrent):
            t = s.exec(select(model_cls).where(model_cls.info_hash == h)).first()
            if t is None:
                continue
            if t.status not in ("downloaded", "downloading"):
                continue   # 这张表里是终态 → 跨表同 hash 可能另一表还在下，继续查下一张，别提前 return
            t.status, t.qb_progress, t.qb_state, t.qb_synced_at = "downloaded", 1.0, "", datetime.now()
            s.add(t)
            s.commit()
            log.info("qB 完成回调：标记已下完 - %s", h[:12])
            return True
    return False   # 不是我们的种子 → 忽略


def backfill_legacy_downloaded_once() -> None:
    """一次性迁移：本功能上线前 status='downloaded' 语义=已交付（历史行都早已下完），但 qb_progress 可能为 0/未满。
    新模型以 qb_progress>=1 判『已完成、停止监听』，故上线时把现存 downloaded 行的 qb_progress 补成 1.0，免得它们
    被误判成『在下』而永久滞留 in-flight、每活跃间隔空打一次 qB。用 Setting 标记，只跑一次（后续新交付照常跟踪）。"""
    flag = "_QB_PROGRESS_BACKFILLED"
    n = 0
    with get_session() as s:
        if s.get(Setting, flag) is not None:
            return
        for model_cls in (AnimeTorrent, MovieTorrent):
            for t in s.exec(select(model_cls).where(
                    model_cls.status == "downloaded", model_cls.qb_progress < 1.0)):
                t.qb_progress = 1.0
                s.add(t)
                n += 1
        s.add(Setting(key=flag, value="1"))
        s.commit()
    if n:
        log.info("一次性迁移：%d 条历史 downloaded 种子标记为已完成（qb_progress=1，脱离 in-flight）", n)


async def sync_qb_status(model_cls) -> int:
    """从 qB 拉『在下的』种子实时态写回某表（AnimeTorrent/MovieTorrent，qb_* 字段同名）。返回更新数。

    一次 hashes= 拿全状态，客户端按 qB 态分桶：
    · 下载态          → 镜像进度/速度（显示『下载中』）；
    · 进度满/做种态    → 镜像后本轮起落定，下轮不再拉（『停止监听』）；
    · error           → 回传 status=error（该集脱离已下，可被别的源补/手动重下）；
    · missingFiles    → 不回传 status，只镜像『文件缺失』，下轮因落定被排除；
    · qB 已无此种子    → 保证有限轮内落定(见下)，绝不让它永久滞留 in-flight、把循环钉住不休眠。
    只拉『在下的』(见 _inflight_where)，全下完时查询为空、直接返回，不打 qB。连不上(None)安静返回 0；
    qB 在线但这批一个都不在(空{})则逐行走 d is None 落定，保证被删/移除的种子有限轮内脱离 in-flight。
    """
    if not config.QB_ENABLED:
        return 0
    with get_session() as s:
        rows = [(t.id, t.info_hash, t.qb_synced_at is not None)
                for t in s.exec(select(model_cls).where(*_inflight_where(model_cls)))
                if t.info_hash]
    if not rows:
        return 0
    info = await qb.torrents_info([h for _, h, _ in rows])
    if info is None:
        return 0   # 只在『连不上/出错』(None) 本轮不动。空 dict {} 是『qB 在线但这批一个都不在』——
                   # 须落到下面逐行走 d is None 落定(全被删/移除时)，否则它们永久 in-flight、循环永不休眠。
    now = datetime.now()
    updated = 0
    with get_session() as s:
        for tid, h, was_synced in rows:
            t = s.get(model_cls, tid)
            if t is None or t.status not in ("downloaded", "downloading"):
                continue
            d = info.get(h)
            if d is None:
                # qB 查不到这个在下的种子——必须在有限轮内落定，否则它恒满足 in-flight、循环永不休眠。
                # 用【重读后】的实时进度判定（await 期间该行可能被完成回调 mark_done_by_hash/新交付推进到满）：
                # 若仍用 await 前的陈旧快照，会把刚被 /api/qb/done 回调标『已下完』的行覆写回 error、使回调形同虚设。
                if (t.qb_progress or 0.0) >= 0.999:  # 已满(含完成回调刚落定) → 下完被 qB 移除，落定已下
                    t.qb_progress, t.qb_state, t.qb_synced_at = 1.0, "", now
                elif was_synced:            # 曾在下、还没下完就从 qB 消失 → 落定 error（可补/重下）。
                    # 注：慢速种子被降级停跟后、在休眠里下完又被 qB 删（remove-on-complete）也会走这里被标 error——
                    # 我们看不到它爬到 100%。要精确标『已下』就在 qB 配『完成回调』(/api/qb/done，可选，见设置页)。
                    t.status, t.qb_state, t.qb_synced_at = "error", "", now
                else:                       # 从未被 qB 确认(刚交付未登记?) → 给一轮宽限，下轮仍无则上面→error
                    t.qb_synced_at = now
                s.add(t)
                updated += 1
                continue
            state = d.get("state", "") or ""
            t.qb_state = state
            t.qb_progress = float(d.get("progress", 0) or 0)
            t.qb_dlspeed = int(d.get("dlspeed", 0) or 0)
            t.qb_size = int(d.get("size", 0) or 0)
            t.qb_synced_at = now
            if state == "error":
                t.status = "error"          # qB 侧真错误 → 回传；missingFiles 有意不回传（只镜像显示）
            elif t.status == "downloading" and t.qb_progress >= 1.0:
                t.status = "downloaded"     # 兼容旧的 downloading 占位（正常已在交付时置 downloaded）
            s.add(t)
            updated += 1
        s.commit()
    return updated


def qb_summary(model_cls) -> dict:
    """某表已交付种子的 qB 实时态聚合：跟踪数 / 下载中 / 做种 / 下速 / 平均进度。

    SQL 侧按 qb_state 分组聚合，只回十几种 state 的汇总行，不把整表已下种子整行拉进内存
    （已下是常态终态、只增不减，随挂机可累积到几千上万条）。qb_state='' 即未被 qB 跟踪，排除。"""
    with get_session() as s:
        grp = s.exec(
            select(model_cls.qb_state, func.count(), func.sum(model_cls.qb_dlspeed),
                   func.sum(model_cls.qb_progress))
            .where(model_cls.status.in_(["downloaded", "downloading"]), model_cls.qb_state != "")
            .group_by(model_cls.qb_state)).all()
    tracked = downloading = seeding = dlspeed = 0
    prog_sum = 0.0
    for state, cnt, speed, psum in grp:
        cnt = cnt or 0
        tracked += cnt
        prog_sum += float(psum or 0)
        if qb_is_downloading(state):
            downloading += cnt
            dlspeed += int(speed or 0)
        if qb_is_seeding(state):
            seeding += cnt
    return {
        "tracked": tracked,
        "downloading": downloading,
        "seeding": seeding,
        "dlspeed": dlspeed,
        "avg_progress": (prog_sum / tracked) if tracked else 0.0,
    }
