"""TV 番剧与剧场版/OVA 共用的底层引擎：下载原语 / qB 客户端与状态同步 / bgm 元数据落库 / 路径季度。

anime.py(TV) 与 movies.py(剧场版) 都依赖这里；本模块不含任何 TV/movie 业务分支，纯共用，
两条线因此互不相干又不重复造轮子。
"""
import asyncio
import logging
import os
import re
from datetime import datetime

import httpx
from sqlmodel import select

import config
from db import get_session
from services.qbittorrent import QBittorrent
from sources.parse import format_quarter

log = logging.getLogger("autorss")

qb = QBittorrent()

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_QUARTER_KEY_RE = re.compile(r"(\d{2})([A-D])")
_TORRENT_CAP = 32 * 1024 * 1024  # .torrent 通常 < 1MB，32MB 已是极宽松上限

# 写回 Anime/Movie 的 bgm 字段（两者同名）；season/kind 等各自专属，不在此
_BGM_FIELDS = ("bangumi_id", "display_name", "jp_name", "air_date", "air_weekday",
               "total_episodes", "platform", "cover_url", "rating", "summary")


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
    if season is not None and config.SEASON_SUBFOLDER:
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

async def fetch_torrent_bytes(url: str) -> bytes:
    """流式下载 .torrent，封顶 32MB + 整体 180s 超时（download_url 源自 RSS 可被投毒 + 跟随重定向）。

    httpx 的 timeout=60 只是每次读的超时、逐块重置，慢速 trickle 连接能让它无限挂起并堵死整个下载/
    采集循环；故再套一层 asyncio.timeout 对总传输时长封顶。取到返回 bytes；HTTP/超限/超时失败抛异常，
    由调用方回写 error。
    """
    kwargs = config.http_client_kwargs(60)
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
        cands = [t for t in torrents if pref in (t.source or "")] or torrents
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

# 含 qB 5.x 改名后的状态（forcedMetaDL / stoppedDL / stoppedUP）
_QB_DOWNLOADING = {"downloading", "forcedDL", "metaDL", "forcedMetaDL", "stalledDL",
                   "queuedDL", "checkingDL", "allocating"}
_QB_SEEDING = {"uploading", "forcedUP", "stalledUP", "queuedUP", "checkingUP",
               "pausedUP", "stoppedUP"}  # 含已完成（暂停做种）


def qb_is_downloading(state: str) -> bool:
    return state in _QB_DOWNLOADING


def qb_is_seeding(state: str) -> bool:
    return state in _QB_SEEDING


async def sync_qb_status(model_cls) -> int:
    """从 qB 拉取已交付种子的实时态写回某表（AnimeTorrent 或 MovieTorrent，qb_* 字段同名）。返回更新数。

    只查 downloaded/downloading（已交给 qB 的）；qB 里没有的清实时态；进度到 1 的把 downloading 收敛为
    downloaded；同步期间被删/忽略（→非跟踪态）的跳过，免得已删种子在面板复活。连不上 qB 安静返回 0。
    """
    if not config.QB_ENABLED:
        return 0
    with get_session() as s:
        rows = [(t.id, t.info_hash) for t in s.exec(select(model_cls).where(
            model_cls.status.in_(["downloaded", "downloading"]))) if t.info_hash]
    if not rows:
        return 0
    info = await qb.torrents_info([h for _, h in rows])
    if info is None:
        return 0
    now = datetime.now()
    updated = 0
    with get_session() as s:
        for tid, h in rows:
            t = s.get(model_cls, tid)
            if t is None or t.status not in ("downloaded", "downloading"):
                continue
            d = info.get(h)
            if d is None:
                if t.qb_state:
                    t.qb_state = ""
                    t.qb_synced_at = now
                    s.add(t)
                    updated += 1
                continue
            t.qb_state = d.get("state", "") or ""
            t.qb_progress = float(d.get("progress", 0) or 0)
            t.qb_dlspeed = int(d.get("dlspeed", 0) or 0)
            t.qb_size = int(d.get("size", 0) or 0)
            t.qb_synced_at = now
            if t.status == "downloading" and t.qb_progress >= 1.0:
                t.status = "downloaded"
            s.add(t)
            updated += 1
        s.commit()
    return updated


def qb_summary(model_cls) -> dict:
    """某表已交付种子的 qB 实时态聚合：跟踪数 / 下载中 / 做种 / 下速 / 平均进度。"""
    with get_session() as s:
        rows = [(t.qb_state, t.qb_progress or 0, t.qb_dlspeed or 0) for t in s.exec(
            select(model_cls).where(model_cls.status.in_(["downloaded", "downloading"]))) if t.qb_state]
    dl = [r for r in rows if qb_is_downloading(r[0])]
    return {
        "tracked": len(rows),
        "downloading": len(dl),
        "seeding": sum(1 for st, _, _ in rows if qb_is_seeding(st)),
        "dlspeed": sum(sp for _, _, sp in dl),
        "avg_progress": (sum(pr for _, pr, _ in rows) / len(rows)) if rows else 0.0,
    }
