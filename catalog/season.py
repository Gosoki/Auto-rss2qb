"""番单目录层（重写第 1 步，只读）。

给定 (年, 季)，产出这一季的番单：番名 + bgm_id + 首播季 + 字幕组 + 有无 ANi。

来源：
- Mikan 季度页 BangumiCoverFlowByDayOfWeek（浏览入口，保证有资源）→ 每部的 mikan_id/番名
- Mikan 番组详情页 → bgm.tv/subject/<id> 精确联动键 + 字幕组列表（ANi = 组 359）
- Bangumi subjects/<id> → 规范 air_date（首播季一律由它算，见 [[auto-rss2qb-rewrite-plan]] 决策）

纯 stdlib、同步、带磁盘缓存；接进异步 feeds/ 是后面的事。季度归属只有一个含义 = bgm 首播季。
"""
from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

UA = "Auto-rss2qb/catalog (https://github.com/rnmuds)"
ANI_GROUP_ID = 359  # Mikan 上 ANi 字幕组的 PublishGroup id
CACHE_DIR = Path(os.environ.get("CATALOG_CACHE") or Path(__file__).resolve().parent / ".cache")
_MIKAN = "https://mikanani.me"
_BGM = "https://api.bgm.tv"


@dataclass
class ShowCard:
    mikan_id: str
    title: str                      # Mikan 展示名（中文）
    bgm_id: int | None
    air_date: str                   # bgm 规范首播日 YYYY-MM-DD（缺失则回退 Mikan 放送开始）
    premiere_quarter: str           # 首播季，如 26C（一律由 air_date 算）
    groups: list[str] = field(default_factory=list)   # 字幕组名（含 ANi）
    group_ids: list[int] = field(default_factory=list)

    @property
    def has_ani(self) -> bool:
        return ANI_GROUP_ID in self.group_ids

    @property
    def other_groups(self) -> list[str]:
        return [g for g, i in zip(self.groups, self.group_ids) if i != ANI_GROUP_ID]


# ---------- 底层：带缓存的 HTTP ----------
def _get(url: str, cache_key: str, rate: float = 0.3) -> str:
    CACHE_DIR.mkdir(exist_ok=True)
    fp = CACHE_DIR / cache_key
    if fp.exists() and fp.stat().st_size > 0:
        return fp.read_text(encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        text = r.read().decode("utf-8", "replace")
    fp.write_text(text, encoding="utf-8")
    time.sleep(rate)  # 礼貌限速（stdlib time.sleep，非 shell sleep）
    return text


# ---------- 解析 ----------
def _season_ids(year: int, season_cn: str) -> list[tuple[str, str]]:
    q = urllib.parse.quote(season_cn)
    url = f"{_MIKAN}/Home/BangumiCoverFlowByDayOfWeek?year={year}&seasonStr={q}"
    htm = _get(url, f"mikan_season_{year}_{season_cn}.html")
    out, seen = [], set()
    for m in re.finditer(r'/Home/Bangumi/(\d+)"[^>]*?title="([^"]*)"', htm):
        mid, title = m.group(1), html.unescape(m.group(2))
        if mid not in seen:
            seen.add(mid)
            out.append((mid, title))
    return out


def _detail(mikan_id: str) -> tuple[int | None, list[tuple[int, str]], str]:
    """返回 (bgm_id, [(group_id, group_name)], mikan 放送开始 YYYY-MM-DD 或 '')。"""
    htm = _get(f"{_MIKAN}/Home/Bangumi/{mikan_id}", f"mikan_detail_{mikan_id}.html")
    bm = re.search(r"bgm\.tv/subject/(\d+)", htm)
    bgm_id = int(bm.group(1)) if bm else None
    # 只认真实种子区块 <div class="subgroup-text" id="..."> 内的组，
    # 避免侧栏/下拉/兜底标签（如 id 180 生肉/不明字幕、73 漫猫）污染覆盖率。
    groups, seen = [], set()
    for seg in re.split(r'<div class="subgroup-text"\s+id="\d+">', htm)[1:]:
        gm = re.search(r'/Home/PublishGroup/(\d+)"[^>]*>([^<]+)', seg)
        if not gm:
            continue
        gid, gname = int(gm.group(1)), html.unescape(gm.group(2)).strip()
        if gid not in seen:
            seen.add(gid)
            groups.append((gid, gname))
    dm = re.search(r"放送开始[：:]\s*</[^>]+>\s*<[^>]+>\s*(\d{4}[/-]\d{1,2}[/-]\d{1,2})", htm)
    if not dm:
        dm = re.search(r"(\d{4}/\d{1,2}/\d{1,2})", htm)
    mikan_air = _norm_date(dm.group(1)) if dm else ""
    return bgm_id, groups, mikan_air


def _bgm_air(bgm_id: int) -> str:
    raw = _get(f"{_BGM}/v0/subjects/{bgm_id}", f"bgm_subject_{bgm_id}.json")
    try:
        return (json.loads(raw).get("date") or "").strip()
    except json.JSONDecodeError:
        return ""


def _norm_date(s: str) -> str:
    s = s.replace("/", "-")
    y, m, d = s.split("-")
    return f"{y}-{int(m):02d}-{int(d):02d}"


def quarter_of(date_str: str) -> str:
    """YYYY-MM-DD -> 首播季码，如 26C（A冬 B春 C夏 D秋）。"""
    if not date_str:
        return ""
    y, m, _ = date_str.split("-")
    letter = "A" if int(m) <= 3 else "B" if int(m) <= 6 else "C" if int(m) <= 9 else "D"
    return f"{y[2:]}{letter}"


# ---------- 对外入口 ----------
def fetch_season(year: int, season_cn: str) -> list[ShowCard]:
    cards: list[ShowCard] = []
    for mid, title in _season_ids(year, season_cn):
        bgm_id, groups, mikan_air = _detail(mid)
        air = (_bgm_air(bgm_id) if bgm_id else "") or mikan_air
        cards.append(ShowCard(
            mikan_id=mid, title=title, bgm_id=bgm_id, air_date=air,
            premiere_quarter=quarter_of(air),
            groups=[g for _, g in groups], group_ids=[i for i, _ in groups],
        ))
    return cards


# ---------- 演示：打印 2026 夏番单 + ANi 标注 + 字幕组覆盖率 ----------
if __name__ == "__main__":
    import sys
    from collections import Counter

    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
    season = sys.argv[2] if len(sys.argv) > 2 else "夏"
    cards = fetch_season(year, season)
    this_q = quarter_of(f"{year}-{ {'冬':1,'春':4,'夏':7,'秋':10}[season] :02d}-01")

    with_ani = [c for c in cards if c.has_ani]
    no_ani = [c for c in cards if not c.has_ani]
    new_only = [c for c in cards if c.premiere_quarter == this_q]   # 真·本季首播新番

    print(f"\n{'='*90}\n{year} {season}季番组 —— 共 {len(cards)} 部"
          f"（其中首播={this_q} 的真·本季新番 {len(new_only)} 部；余为跨季续播/长番）\n{'='*90}")
    print(f"{'ANi':<5}{'首播季':<7}{'bgm_id':<9}番名 / 字幕组")
    print("-" * 90)
    for c in sorted(cards, key=lambda x: (not x.has_ani, x.air_date)):
        flag = "✓" if c.has_ani else "·"
        newtag = "🆕" if c.premiere_quarter == this_q else "  "
        if c.has_ani:
            line = f"ANi + {len(c.other_groups)}组" if c.other_groups else "仅 ANi"
        else:
            line = "、".join(c.other_groups[:6]) or "（无字幕组？）"
        print(f"{flag:<5}{c.premiere_quarter:<7}{str(c.bgm_id):<9}{newtag} {c.title}  →  {line}")

    print(f"\n{'='*90}\nANi 覆盖：{len(with_ani)}/{len(cards)} 部有 ANi；{len(no_ani)} 部无 ANi\n{'='*90}")

    # ---- 非 ANi 字幕组覆盖率 ----
    def rank(pool, label):
        cnt = Counter()
        for c in pool:
            for gid, gname in zip(c.group_ids, c.groups):
                if gid != ANI_GROUP_ID:
                    cnt[gname] += 1
        n = len(pool)
        print(f"\n【{label}】(共 {n} 部) 非 ANi 字幕组覆盖率 TOP15：")
        for gname, k in cnt.most_common(15):
            print(f"  {k:>3}/{n}  ({k/n*100:4.1f}%)  {gname}")

    rank(cards, "全部 84 部（含续播）")
    rank(new_only, f"仅真·本季新番（首播={this_q}）")
