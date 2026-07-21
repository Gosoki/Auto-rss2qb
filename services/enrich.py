"""Bangumi 富集（P3）——bgm 是番剧身份/季度/规范名的权威。

匹配（拿 bgm subject id）优先级：
  ① 用标题里的候选名（日文原名/罗马音/中文简繁）搜 bgm，被越多名字一致命中越可信；
     用『集数倒推的首播日』校验放送日，挡掉同名老番/别的作品。
  ② 都没命中才退回 Mikan-hash 桥（hash→Mikan剧集页→bgm）当兜底——Mikan 只是下载源+兜底。
拿到 bgm id 后，name_cn=规范名、date=真实放送日→季度、id=跨源去重身份，全出自 bgm。
全程尽力而为，拿不到返回 None，绝不阻断主下载链路。
"""
import logging
import re
from collections import Counter
from datetime import datetime, timedelta

import httpx

import config
from sources.parse import extract_quarter

log = logging.getLogger("autorss")

_MIKAN_BANGUMI_RE = re.compile(r"/Home/Bangumi/(\d+)")
_BGM_SUBJECT_RE = re.compile(r"bgm\.tv/subject/(\d+)")
_CJK_RE = re.compile(r"[一-鿿぀-ヿ]")
_UA = {"User-Agent": "autorss/1.0 (anime rss downloader)"}


def _parse_date(s: str | None) -> datetime | None:
    # bgm 用 ISO(Y-M-D)；不放歧义的 D/M/Y。
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _date_ok(bgm_dt: datetime, est: datetime | None, release: datetime | None) -> bool:
    """放送日是否合理：贴近『集数倒推的首播日』(±35天)，或落在种子发布前后的兜底窗口。"""
    if est is None and release is None:
        return True  # 完全没有时间基准时不卡日期，交给名字重叠+bgm 相关性排序
    if est is not None and abs((est - bgm_dt).days) <= 35:
        return True
    if release is not None and -21 <= (release - bgm_dt).days <= 45:
        return True
    return False


def _name_plausible(query: str, subject: dict) -> bool:
    """中文/日文名做字符重叠校验；纯罗马音交给搜索相关性+日期，不额外卡。"""
    if not _CJK_RE.search(query):
        return True
    cand = f"{subject.get('name_cn', '')} {subject.get('name', '')}"
    return any(query[i:i + 2] in cand for i in range(len(query) - 1) if query[i:i + 2].strip())


async def _search_one(client, name, est, release):
    """用一个名字搜 bgm，返回第一个通过日期+名字校验的 subject（bgm 按相关性排序）。"""
    try:
        r = await client.post(
            f"{config.BGM_API}/v0/search/subjects", headers=_UA,
            json={"keyword": name, "filter": {"type": [2]}},
        )
        if r.status_code != 200:
            return None
        body = r.json()
        # bgm 正常返回 {"data": [...]}；防它返回数组/非对象/data 非列表导致 AttributeError 逃逸
        data = body.get("data") if isinstance(body, dict) else None
        results = data if isinstance(data, list) else []
    except (httpx.HTTPError, ValueError, TypeError):
        return None
    for d in results:
        if not isinstance(d, dict):
            continue
        dt = _parse_date(d.get("date"))
        if dt is None:
            continue
        if _date_ok(dt, est, release) and _name_plausible(name, d):
            return d
    return None


async def _mikan_bridge(client, info_hash):
    """兜底：hash → Mikan 剧集页 → Mikan番组页 → bgm id。"""
    try:
        ep = await client.get(f"{config.MIKAN_BASE}/Home/Episode/{info_hash}")
        if ep.status_code != 200:
            return None
        m = _MIKAN_BANGUMI_RE.search(ep.text)
        if not m:
            return None
        bg = await client.get(f"{config.MIKAN_BASE}/Home/Bangumi/{m.group(1)}")
        if bg.status_code != 200:
            return None
        sm = _BGM_SUBJECT_RE.search(bg.text)
        return int(sm.group(1)) if sm else None
    except httpx.HTTPError:
        return None


def _subject_to_info(bgm_id, meta: dict) -> dict:
    """bgm subject 元数据 → 统一的富集 info 字典（resolve 与手动绑定共用）。"""
    jp_name = meta.get("name") or None                  # 原名（日文）
    display_name = meta.get("name_cn") or jp_name        # 规范名，无中文退日文
    dt = _parse_date(meta.get("date"))
    return {
        "bangumi_id": bgm_id,
        "display_name": display_name,
        "jp_name": jp_name,
        "air_date": dt.strftime("%Y-%m-%d") if dt else None,
        "air_weekday": dt.weekday() if dt else None,     # 0=周一
        "quarter": extract_quarter(dt) if dt else None,
        "total_episodes": meta.get("total_episodes") or meta.get("eps") or None,
        "platform": meta.get("platform") or None,        # TV/剧场版/OVA…
        "cover_url": (meta.get("images") or {}).get("large") or None,
        "rating": (meta.get("rating") or {}).get("score") or None,
        "summary": (meta.get("summary") or "").strip() or None,
    }


async def fetch_by_id(bgm_id: int) -> dict | None:
    """按明确的 bgm subject id 直接取元数据（『富集失败』页手动绑定用）。取不到返回 None。"""
    try:
        async with httpx.AsyncClient(**config.http_client_kwargs(config.ENRICH_TIMEOUT)) as client:
            r = await client.get(f"{config.BGM_API}/v0/subjects/{bgm_id}", headers=_UA)
        if r.status_code != 200:
            return None
        j = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("按 id 取 bgm 失败 %s: %s", bgm_id, e)
        return None
    if not isinstance(j, dict) or not j.get("id"):
        return None
    return _subject_to_info(bgm_id, j)


async def resolve(names, release_time=None, episode=None, info_hash=None) -> dict | None:
    """→ {bangumi_id, air_date, quarter, display_name}；拿不到返回 None。"""
    names = [names] if isinstance(names, str) and names else (names or [])
    if not names and not info_hash:
        return None

    # 集数倒推首播日（周更番第 N 集≈首播后 N-1 周），作为日期校验基准
    est = release_time
    if release_time is not None and isinstance(episode, (int, float)) and 1 <= episode <= 30:
        est = release_time - timedelta(weeks=int(episode) - 1)

    try:
        async with httpx.AsyncClient(**config.http_client_kwargs(config.ENRICH_TIMEOUT)) as client:
            # ① 多名搜 bgm，统计投票（被几个名字命中）+ 记录日期贴合度
            votes: Counter = Counter()
            gap: dict = {}
            for name in names:
                d = await _search_one(client, name, est, release_time)
                if d:
                    bid = d.get("id")
                    if bid is None:
                        continue
                    votes[bid] += 1
                    bdt = _parse_date(d.get("date"))
                    g = abs(((est or release_time) - bdt).days) if (bdt and (est or release_time)) else 999
                    gap[bid] = min(gap.get(bid, 10 ** 9), g)
            bgm_id = None
            if votes:
                # 优先被多个名字一致命中的；其次放送日最贴的
                bgm_id = sorted(votes, key=lambda i: (-votes[i], gap.get(i, 10 ** 9)))[0]

            # ② 兜底：Mikan-hash 桥
            if bgm_id is None and info_hash:
                bgm_id = await _mikan_bridge(client, info_hash)

            # ③ 取 bgm 元数据（规范名/原名/放送日 + 简介/总集数/类型/封面/评分）
            meta = {}
            if bgm_id is not None:
                try:
                    r = await client.get(f"{config.BGM_API}/v0/subjects/{bgm_id}", headers=_UA)
                    if r.status_code == 200:
                        j = r.json()
                        meta = j if isinstance(j, dict) else {}  # 防 bgm 返回数组/非对象
                except (httpx.HTTPError, ValueError):
                    meta = {}

        if _parse_date(meta.get("date")) is None and bgm_id is None:
            return None
        return _subject_to_info(bgm_id, meta)
    except httpx.HTTPError as e:
        log.warning("富集失败 %s: %s", (names[0] if names else info_hash or "")[:16], e)
        return None
