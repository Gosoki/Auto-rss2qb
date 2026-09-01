"""手动下载（一次性工具，不建 Anime 记录、不进追踪）。

把用户给的 magnet / .torrent（链接或上传文件）交给 qB：默认下到『工作目录/Temp』。
可选填 bgm 链接/ID → 识别算出正常番剧目录（季度/日文名/Season），可在下载前定向、或下完再 setLocation 移过去。
"""
import base64
import hashlib
import logging
import re

import config
from core import engine
from services import enrich
from sources.parse import season_from_name

log = logging.getLogger("autorss")

_BTIH_RE = re.compile(r"xt=urn:btih:([0-9A-Fa-f]{40}|[A-Za-z2-7]{32})")
_BGM_ID_RE = re.compile(r"subject/(\d+)")

MANUAL_CATEGORY = "AutoRSS-Manual"   # qB 分类：与番剧 AutoRSS-Anime / 剧场版 AutoRSS-Movie 并列，均不带后缀
MANUAL_TAG = "Manual"                # qB 标签：只放后缀（番剧放季度、剧场版放年份，手动固定 Manual）


def temp_path() -> str:
    """默认保存位置：工作目录下的 Temp（工作目录未设时退到动漫目录/空）。"""
    base = (config.DOWN_PATH or config.ANIME_DOWN_PATH or "").rstrip("/\\")
    return (base + "/Temp") if base else "Temp"


def parse_bgm_id(text: str):
    """从 bgm 链接/文本抠 subject id：优先 subject/<id>，退而取任意数字。取不到返回 None。"""
    m = _BGM_ID_RE.search(text or "") or re.search(r"(\d+)", text or "")
    return int(m.group(1)) if m else None


def magnet_hash(magnet: str):
    """从 magnet 取 v1 info-hash（40hex 或 32 位 base32 → 归一成 40hex 小写）；取不到返回 None。"""
    m = _BTIH_RE.search(magnet or "")
    if not m:
        return None
    h = m.group(1)
    if len(h) == 40:
        return h.lower()
    try:                                    # base32(32 char) → 20 字节 → 40hex
        return base64.b32decode(h.upper()).hex()
    except Exception:
        return None


def info_hash_from_torrent(data: bytes):
    """从 .torrent 字节算 v1 info-hash（sha1 of bencoded info 段）；解析失败返回 None。"""
    try:
        pos = 0

        def dec():
            nonlocal pos
            c = data[pos:pos + 1]
            if c == b"i":
                j = data.index(b"e", pos); v = int(data[pos + 1:j]); pos = j + 1; return v
            if c == b"l":
                pos += 1; out = []
                while data[pos:pos + 1] != b"e":
                    out.append(dec())
                pos += 1; return out
            if c == b"d":
                pos += 1; out = {}
                while data[pos:pos + 1] != b"e":
                    k = dec(); vs = pos; dec(); out[k] = (vs, pos)   # 记 value 字节区间
                pos += 1; return out
            j = data.index(b":", pos); n = int(data[pos:j]); s = data[j + 1:j + 1 + n]; pos = j + 1 + n; return s

        top = dec()
        span = top.get(b"info") if isinstance(top, dict) else None
        if not span:
            return None
        return hashlib.sha1(data[span[0]:span[1]]).hexdigest()
    except Exception:
        return None


async def add_manual(torrent_input: str, torrent_bytes, save_path: str) -> dict:
    """把手动种子交给 qB。torrent_input=magnet/http链接（有上传文件时可空），torrent_bytes=上传的 .torrent 字节。
    返回 {ok, info_hash?, save_path?, error?}。info_hash 供之后『移到正常目录』用。"""
    if not config.QB_ENABLED:
        return {"ok": False, "error": "qB 未启用（去设置页开启『发送种子到 qB』）"}
    save_path = (save_path or temp_path()).strip()
    if not save_path:
        return {"ok": False, "error": "没有保存位置（工作目录未设时请手填绝对路径）"}
    inp = (torrent_input or "").strip()
    try:
        # 【一律走 engine.add_to_qb，不要直接调 qb.add_*】这里原本是三条直连，
        # 于是番剧/剧场版两条线都有、而手动这条没有的两件事：
        # ① qB 同机时预建 save_path（手动的默认位置『工作目录/Temp』谁也不会先去建）；
        # ② 『这个 hash 已经在 qB 里』的幂等兜底——同一条磁力点第二次，
        #    本该是"它已经在下了"，实际给的是红色『qB 未接受（种子无效/路径不可写/重复）』。
        if torrent_bytes:                                   # 上传的 .torrent 文件
            ih = info_hash_from_torrent(torrent_bytes)
            ok = await engine.add_to_qb(torrent_bytes, save_path, MANUAL_CATEGORY, MANUAL_TAG,
                                        info_hash=ih or "")
        elif inp.lower().startswith("magnet:"):             # magnet：交给 qB 抓
            ih = magnet_hash(inp)
            ok = await engine.add_to_qb(None, save_path, MANUAL_CATEGORY, MANUAL_TAG,
                                        info_hash=ih or "", magnet=inp)
        elif inp.lower().startswith(("http://", "https://")):   # .torrent 链接：本地抓字节(带 SSRF 守卫)再交 qB
            data = await engine.fetch_torrent_bytes(inp)
            ih = info_hash_from_torrent(data)
            ok = await engine.add_to_qb(data, save_path, MANUAL_CATEGORY, MANUAL_TAG,
                                        info_hash=ih or "")
        else:
            return {"ok": False, "error": "请填 magnet: / http(s) .torrent 链接，或上传 .torrent 文件"}
    except Exception as e:
        log.warning("手动下载失败: %s", e)
        return {"ok": False, "error": str(e)}
    if ok:
        log.info("手动下载已交 qB: %s → %s", (ih or inp or "上传文件")[:16], save_path)
    # add_torrent/add_url 是三态：None=连不上 qB（暂时性，重试就好），False=qB 明确拒了这一条
    #（种子坏/路径不可写/重复）。把 None 也说成"未接受"是误诊，会让用户去查种子和路径，
    # 而真正该做的只是等 qB 起来再点一次。
    err = None if ok else ("连不上 qB（在重启/网络不通？稍后再点一次）" if ok is None
                           else "qB 未接受（种子无效/路径不可写/重复）")
    return {"ok": bool(ok), "info_hash": ih, "save_path": save_path, "error": err}


async def identify_folder(bgm_input: str) -> dict:
    """用 bgm 链接/ID 识别 → 算正常番剧目录（季度/日文名/Season）。返回 {ok, name, quarter, season, path, error?}。
    一次性：只取 bgm 元数据算路径，不建 Anime、不入库。"""
    bid = parse_bgm_id(bgm_input)
    if bid is None:
        return {"ok": False, "error": "请填 bgm 链接或数字 ID"}
    info = await enrich.fetch_by_id(bid)
    if not info:
        return {"ok": False, "error": "bgm 取不到（ID 不存在 / 网络问题）"}
    quarter = info.get("quarter") or "unknown"
    folder = info.get("jp_name") or info.get("display_name") or "unknown"
    season = (season_from_name(info.get("jp_name") or "")
              or season_from_name(info.get("display_name") or "") or 1)
    path = engine.build_save_path(quarter, folder, season=season, sub_dir=config.ANIME_DOWN_PATH)
    if path is None:
        return {"ok": False, "error": "算不出保存目录（越界 / 未配置下载目录）"}
    return {"ok": True, "name": info.get("display_name") or folder,
            "quarter": quarter, "season": season, "path": path}
