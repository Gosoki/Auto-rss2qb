"""Bangumi 富集（P3）——bgm 是番剧身份/季度/规范名的权威。

匹配（拿 bgm subject id）优先级：
  ① 用标题里的候选名（日文原名/罗马音/中文简繁）搜 bgm，被越多名字一致命中越可信；
     用『集数倒推的首播日』校验放送日，挡掉同名老番/别的作品。
  ② 都没命中才退回 Mikan-hash 桥（hash→Mikan剧集页→bgm）当兜底——Mikan 只是下载源+兜底。
拿到 bgm id 后，name_cn=规范名、date=真实放送日→季度、id=跨源去重身份，全出自 bgm。
全程尽力而为，拿不到返回 None，绝不阻断主下载链路。
"""
import asyncio
import logging
import re
from collections import Counter
from datetime import datetime, timedelta

import httpx

import config
from sources.parse import movie_quarter_of, quarter_of

log = logging.getLogger("autorss")


# 单次尝试的总时长上限（秒）。比 ENRICH_TIMEOUT（逐块超时，默认十几秒）宽松得多，
# 只用来兜住"涓流响应把识别协程挂到天荒地老"这一种情形，正常请求碰不到它。
_ATTEMPT_TIMEOUT = 90
# resolve() 的【整体】预算（秒）与候选名上限。理由见 resolve 里的注释：
# 它串在采集主链路上，单番拖久了会连累整轮采集与下载放行。
# 120 秒对"搜 3 个名字 + 取一次详情 + 取一次声优"绰绰有余（正常一次 1~3 秒）。
_RESOLVE_BUDGET = 120
# 【截断前先按繁简去重】candidate_names 会为中文名额外产出一个 t2s 简体孪生，两者紧挨着排在
# 最前面。直接切前 3 个的话，3 个槽位实际只装得下约 2 个【不同】的名字，日文原名必然被切掉。
# 用真实标题实测过 2900 条：截断本身不改变最终 bangumi_id（bgm 的 name_cn 就是简体，
# 繁体名单独搜也命中），但"3 个名字"这个说法是不准的——去重之后才是。
_MAX_CANDIDATE_NAMES = 3
# 声优抓取自己的上限：它是【可选】字段，不该占用整体预算，更不该让一次已成功的识别作废。
_CAST_TIMEOUT = 20


async def _retryable(make_request):
    """执行一次 HTTP 请求；遇瞬时错误(超时/连接/读)按 config.ENRICH_RETRY_TIMES 重试(指数退避)。

    make_request：无参 async，返回 httpx.Response。重试用尽后把最后一次异常抛出（交由各调用点的
    try/except httpx.HTTPError 收成 None/{}）。非瞬时错误（如 404 正常返回）不在此重试。
    """
    times = max(1, config.ENRICH_RETRY_TIMES)
    for i in range(times):
        try:
            # 【总超时】httpx 的 timeout 是每次读的、逐块重置，服务端涓流发送就能永久挂住，
            # 而这是常驻的识别协程。给每次尝试套一个硬上限，和 sources/ 那边同一思路
            # （那边走 services.fetch；这里调用方要的是 Response 对象，只补总超时即可）。
            async with asyncio.timeout(_ATTEMPT_TIMEOUT):
                return await make_request()
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError,
                httpx.RemoteProtocolError, TimeoutError) as e:
            if i + 1 >= times:
                # 【必须换成 httpx 的异常类型再抛】本模块所有调用点接的都是 except httpx.HTTPError
                # （_search_one / _mikan_bridge / _fetch_cast / fetch_by_id / resolve），
                # 而 asyncio.timeout 抛的 TimeoutError 是 OSError 的子类、不在那一支里，
                # 直接抛会穿透【全部】兜底，把"尽力而为、拿不到返回 None"的契约变成崩溃，
                # 进而掀翻整条识别链路（本模块 docstring 明写"绝不阻断主下载链路"）。
                if isinstance(e, TimeoutError) and not isinstance(e, httpx.TimeoutException):
                    raise httpx.TimeoutException(f"整体超时（{_ATTEMPT_TIMEOUT}s）") from e
                raise
            await asyncio.sleep(0.5 * (2 ** i))   # 0.5s → 1s → 2s …

# 【"没问成" vs "问了但搜不到"】两者的返回值都是 None，而上层必须能区分：
# retry_unmatched 的退避阶梯一共只有 REENRICH_MAX_TRIES 次、总跨度约 15 小时，正好能被
# bgm 的一次限流窗口或机房故障吃光——而"根本没问成"不该消耗那几次机会。
#
# 【只统计 bgm 这一个对端】计数器的唯一消费者是 retry_unmatched，它问的是"bgm 到底可不可达"。
# 早先这里把 Mikan 桥的失败也记进同一个数：于是"bgm 一切正常、只是 Mikan 打不开"会被
# 判成"bgm 整体不可达"，退避阶梯被无限退款、每个节拍重打一遍 bgm（实测 bgm 恒 200 也照样如此）。
# 同理，纯本地的解析异常（JSON 坏了、字段类型不对）也不算"没问成"——问是问到了。
_bgm_fail = 0


def _note_bgm_fail() -> None:
    """记一次【bgm 没问成】。只在"请求没能拿到一个可信答复"时调：连接层失败、429、5xx。"""
    global _bgm_fail
    _bgm_fail += 1


def net_failures() -> int:
    """本进程累计的【bgm 没问成】次数。上层取调用前后的差值，判断这一次到底问成没有。"""
    return _bgm_fail


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


def _name_not_contradicted(query: str, subject: dict) -> bool:
    """中文/日文名做字符重叠校验；纯罗马音交给搜索相关性+日期，不额外卡。"""
    if not _CJK_RE.search(query):
        return True
    cand = f"{subject.get('name_cn', '')} {subject.get('name', '')}"
    if len(query) < 2:                       # 单字 CJK 名：2-gram 循环为空会恒 False，退回子串包含判断
        return query in cand
    return any(query[i:i + 2] in cand for i in range(len(query) - 1) if query[i:i + 2].strip())


async def _search_one(client, name, est, release):
    """用一个名字搜 bgm，返回第一个通过日期+名字校验的 subject（bgm 按相关性排序）。"""
    try:
        r = await _retryable(lambda: client.post(
            f"{config.BGM_API}/v0/search/subjects", headers=_UA,
            json={"keyword": name, "filter": {"type": [2]}},
        ))
        if r.status_code != 200:
            # 429（限流）与 5xx 同样是【没问成】——而它们恰恰是最可能整批发生、
            # 最该退款的一类。4xx 里除 429 之外算"问到了但对方说不行"，不计。
            if r.status_code == 429 or r.status_code >= 500:
                _note_bgm_fail()
            return None
        body = r.json()
        # bgm 正常返回 {"data": [...]}；防它返回数组/非对象/data 非列表导致 AttributeError 逃逸
        data = body.get("data") if isinstance(body, dict) else None
        results = data if isinstance(data, list) else []
    except httpx.HTTPError:
        _note_bgm_fail()     # 连接层失败：这次【没问成】，与"问了但搜不到"要分开
        return None
    except (ValueError, TypeError):
        return None          # 对端回了东西但格式不对——问是问到了
    for d in results:
        if not isinstance(d, dict):
            continue
        dt = _parse_date(d.get("date"))
        if dt is None:
            continue
        if _date_ok(dt, est, release) and _name_not_contradicted(name, d):
            return d, dt   # 连同已解析的放送日返回，供 resolve 复用、免二次解析
    return None


async def _mikan_bridge(client, info_hash):
    """兜底：hash → Mikan 剧集页 → Mikan番组页 → bgm id。"""
    if not re.fullmatch(r"[0-9a-f]{40}", info_hash or ""):
        return None  # 只把 40 位 hex 拼进 URL：防非法 hash 造成路径穿越/请求注入
    try:
        ep = await _retryable(lambda: client.get(f"{config.MIKAN_BASE}/Home/Episode/{info_hash}"))
        if ep.status_code != 200:
            return None
        m = _MIKAN_BANGUMI_RE.search(ep.text)
        if not m:
            return None
        bg = await _retryable(lambda: client.get(f"{config.MIKAN_BASE}/Home/Bangumi/{m.group(1)}"))
        if bg.status_code != 200:
            return None
        sm = _BGM_SUBJECT_RE.search(bg.text)
        return int(sm.group(1)) if sm else None
    except httpx.HTTPError:
        # 【不记 bgm 的账】这是 Mikan，不是 bgm。混在一起会让"Mikan 打不开"被判成
        # "bgm 整体不可达"，把退避阶梯无限退款。
        return None


def _infobox_get(infobox, *keys) -> str | None:
    """从 bgm infobox 取某键的文本值（值可能是字符串或 [{v:..}] 列表）；按给定键顺序取第一个命中的。"""
    idx = {it.get("key"): it.get("value") for it in (infobox or [])
           if isinstance(it, dict) and it.get("key")}
    for k in keys:
        v = idx.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, list):
            parts = [str(x.get("v") if isinstance(x, dict) else x).strip() for x in v]
            parts = [p for p in parts if p]
            if parts:
                return "、".join(parts)
    return None


async def _fetch_cast(client, bgm_id, limit=8) -> str | None:
    """取『主角』的声优名（去重、只要 CV 名不要角色名）→ '声优、声优…' 文本；失败/无主角返回 None。
    只抓主角、不存人物 URL（要全部演员表点 bgm 链接）。"""
    try:
        # 与其它 bgm 调用一致走 _retryable（瞬时超时/连接/读错误按 ENRICH_RETRY_TIMES 指数退避）——
        # 否则声优抓取遇一次瞬时超时即丢 cast，而规范名/放送日等字段会重试，口径不一（B8）。
        r = await _retryable(lambda: client.get(
            f"{config.BGM_API}/v0/subjects/{bgm_id}/characters", headers=_UA))
        if r.status_code != 200:
            return None
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    names: list[str] = []
    for ch in data:
        if not isinstance(ch, dict) or ch.get("relation") != "主角":
            continue
        for a in (ch.get("actors") or []):
            nm = a.get("name") if isinstance(a, dict) else None
            if nm and nm not in names:
                names.append(nm)
    return "、".join(names[:limit]) or None


def _clean_summary(s: str | None) -> str | None:
    """bgm 简介：统一换行、去行尾空白、把连续 2 行以上空白压成 1 行（避免大段空白）。"""
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not s:
        return None
    s = re.sub(r"[ \t　]+(?=\n)", "", s)   # 去每行尾部空白（含全角空格）
    s = re.sub(r"\n{3,}", "\n\n", s)           # 连续 2 行以上空白 → 压成 1 行
    return s


def _subject_to_info(bgm_id, meta: dict) -> dict:
    """bgm subject 元数据 → 统一的富集 info 字典（resolve 与手动绑定共用）。cast 由调用方另调 /characters 填。"""
    jp_name = meta.get("name") or None                  # 原名（日文）
    display_name = meta.get("name_cn") or jp_name        # 规范名，无中文退日文
    dt = _parse_date(meta.get("date"))
    ib = meta.get("infobox")
    return {
        "bangumi_id": bgm_id,
        "display_name": display_name,
        "jp_name": jp_name,
        "air_date": dt.strftime("%Y-%m-%d") if dt else None,
        "air_weekday": dt.weekday() if dt else None,     # 0=周一
        "quarter": quarter_of(dt) if dt else None,
        # 剧场版用它，理由见 sources.parse.movie_quarter_of（12 月首映的片不该归到次年）。
        # 两个都给出来，由调用方按自己是哪条线选——别在这里判，enrich 不知道自己在为谁工作。
        "movie_quarter": movie_quarter_of(dt) if dt else None,
        "total_episodes": meta.get("total_episodes") or meta.get("eps") or None,
        "platform": meta.get("platform") or None,        # TV/剧场版/OVA…
        "cover_url": (meta.get("images") or {}).get("large") or None,
        "rating": (meta.get("rating") or {}).get("score") or None,
        "summary": _clean_summary(meta.get("summary")),
        "author": _infobox_get(ib, "原作"),
        "director": _infobox_get(ib, "导演", "監督", "总导演"),
        "music": _infobox_get(ib, "音乐", "音楽"),
        "duration": _infobox_get(ib, "片长", "时长"),   # 剧场版展示用；番剧无 duration 列，写回时跳过
    }


async def fetch_by_id(bgm_id: int) -> dict | None:
    """按明确的 bgm subject id 直接取元数据（『富集失败』页手动绑定用）。取不到返回 None。

    【同样要有整体预算】它串在【UI 同步等待】的路径上（详情页点『绑定 bgm』的 on_click，
    外层没有任何 timeout），而内部是两次串行的 _retryable —— 最坏
    2 × ENRICH_RETRY_TIMES × _ATTEMPT_TIMEOUT ≈ 9 分钟，期间按钮一直转圈。
    剧场版扫描也在 per-movie 循环里调它，还被 _scan_lock 罩着。
    """
    try:
        async with asyncio.timeout(_RESOLVE_BUDGET):
            return await _fetch_by_id_inner(bgm_id)
    except TimeoutError:
        log.warning("按 id 取 bgm 超时（整体 %ds）：%s", _RESOLVE_BUDGET, bgm_id)
        return None


async def _fetch_by_id_inner(bgm_id: int) -> dict | None:
    try:
        async with httpx.AsyncClient(**config.http_client_kwargs(max(1, config.ENRICH_TIMEOUT))) as client:
            r = await _retryable(lambda: client.get(f"{config.BGM_API}/v0/subjects/{bgm_id}", headers=_UA))
            if r.status_code != 200:
                return None
            j = r.json()
            # 【声优要自己的小超时，不能裸调】与 _resolve_inner 里那段【同一个理由】：
            # 一个涓流的 /characters 会把 3 次重试各拖满 _ATTEMPT_TIMEOUT(90s)≈270s，
            # 撑破外层 _RESOLVE_BUDGET(120s)，于是一次【已经拿到 subject 的成功取数】被整份丢掉。
            # 那三行守卫加在 resolve 那条路径上时，这条漏了——而这条更糟：
            # discover_movies 走的就是它，info=None → 新片以 bangumi_id 为空落库、归档进 unknown/，
            # 而剧场版线没有 retry_unmatched 那样的后台重识别，只能人工再点。
            try:
                async with asyncio.timeout(_CAST_TIMEOUT):
                    cast = await _fetch_cast(client, bgm_id)
            except Exception as e:
                log.info("声优信息取不到（不影响识别）：%s: %s", type(e).__name__, e)
                cast = None
    except Exception as e:
        # 【口径与兄弟路径 _resolve_inner 一致（那边是 except Exception）】只接
        # (httpx.HTTPError, ValueError) 漏掉一整族：代理填成 'socks5://…' 而没装 socksio 时，
        # httpx.AsyncClient 是在【建 client 那一步】抛 ImportError（不是发请求时），
        # 而它既不是 HTTPError 也不是 ValueError。漏出去之后一路穿透 bind_anime_bgm /
        # bind_movie_bgm / manual.identify_folder（三者都没有 try）逃进 NiceGUI 的 on_click——
        # 而 NiceGUI 默认只 log.exception，用户侧连一条 notify 都没有：按钮点了没反应。
        # 同一个弹窗里的『重新识别』走 resolve、那条是 except Exception，同样的故障它会正常弹提示：
        # 同一页同一故障，一个按钮报错、旁边那个装死。
        log.warning("按 id 取 bgm 失败 %s: %s: %s", bgm_id, type(e).__name__, e)
        return None
    if not isinstance(j, dict) or not j.get("id"):
        return None
    try:
        info = _subject_to_info(bgm_id, j)
    except Exception as e:
        # 纯本地转换也要兜——与 _resolve_inner 里给同一句加的独立 try 同款理由：
        # bgm 换一次字段形状（rating 从 dict 变成字符串之类）就是 AttributeError/TypeError，
        # 而它同样会从本函数逃出去变成一个死键。实测 _subject_to_info(1, {"id":1,"rating":"8.5"}) 抛 AttributeError。
        log.warning("bgm 详情字段形状异常 %s: %s: %s", bgm_id, type(e).__name__, e)
        return None
    info["cast"] = cast
    return info


def _dedup_names(names: list) -> list:
    """按"转简体后是否相同"去重，保序。见 _MAX_CANDIDATE_NAMES 的说明。"""
    from sources.parse import t2s
    out, seen = [], set()
    for n in names:
        k = t2s(n or "").strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(n)
    return out


async def resolve(names, release_time=None, episode=None, info_hash=None) -> dict | None:
    """→ {bangumi_id, air_date, quarter, display_name}；拿不到返回 None。"""
    names = [names] if isinstance(names, str) and names else (names or [])
    if not names and not info_hash:
        return None

    # 集数倒推首播日（周更番第 N 集≈首播后 N-1 周），作为日期校验基准。
    #
    # 集号落在 1..30 之外时【连同 release 一起放弃日期校验】，不能像以前那样退回拿 release_time
    # 当首播日：那样 _date_ok 的两条判据会塌缩成同一个 [bgm_air-35, bgm_air+45] 窗口，而长番
    # 第 40 集的发布时间距首播已 280 天，窗口必然落空 → _search_one 把【包括正确答案在内的】
    # 全部候选 continue 掉，名字投票整条路径失效，识别退化成只剩 Mikan-hash 桥一个单点。
    # 也不能无脑把倒推外推到更大集号：跨季绝对编号（S2 第 4 集被写成 16）倒推会指到【上一季】的
    # 首播日，反而把正确的续季 subject 判否。宁可不设基准，交给名字重叠 + bgm 相关性排序。
    est = release_time
    date_ref = release_time      # 传给 _date_ok 的 release 基准；放弃校验时与 est 一起置 None
    if isinstance(episode, (int, float)):
        if 1 <= episode <= 30:
            if release_time is not None:
                est = release_time - timedelta(weeks=int(episode) - 1)
        else:
            est = date_ref = None    # 长番 / 绝对编号 / -1 特别篇 / -2 未识别

    # 【整体时间预算】识别是【串在采集主链路上】的：process_item 在种子落库【之前】调它，
    # 而 poll_once 又要等所有条目处理完才轮到 flush 放行下载。
    # 没有这层封顶时，最坏情况是 候选名数 × 重试次数 × _ATTEMPT_TIMEOUT ——
    # 实测单番可达 ~22 分钟，二十个新番就能把整轮采集与下载放行堵住数小时；
    # 而 nyaa 的 RSS 是滑动窗口，被堵期间滚出去的条目【永远不会再被采到】。
    # 超时就当"这次没识别成"，番留在『待识别』，由 run_reenrich_retry 按退避重来。
    try:
        async with asyncio.timeout(_RESOLVE_BUDGET):
            return await _resolve_inner(_dedup_names(names)[:_MAX_CANDIDATE_NAMES],
                                        est, date_ref, info_hash)
    except TimeoutError:
        # 【这里不能再自己记一次 bgm 失败】预算罩住的不只是 bgm，还有 Mikan 桥
        # （_mikan_bridge）——而它自己的注释明写着"不记 bgm 的账：混在一起会让
        # 'Mikan 打不开' 被判成 'bgm 整体不可达'，把退避阶梯无限退款"。
        # 无条件记的话，Mikan 涓流就能让每一轮都退款：enrich_tries 永远回到 0、
        # 每个检查节拍重打一遍 bgm，而 bgm 从头到尾都是 200。实测复现过。
        # 内层已经在每个真实的 bgm 失败点各记了一笔，这里【什么都不做】才是准确的。
        log.warning("富集超时（整体 %ds），按未识别处理：%s",
                    _RESOLVE_BUDGET, (names[0] if names else info_hash or "")[:40])
        return None


async def _resolve_inner(names, est, date_ref, info_hash) -> dict | None:
    """resolve 的主体。拆出来只为让上面那层 asyncio.timeout 包得干净。"""
    try:
        async with httpx.AsyncClient(**config.http_client_kwargs(max(1, config.ENRICH_TIMEOUT))) as client:
            # ① 多名搜 bgm，统计投票（被几个名字命中）+ 记录日期贴合度
            votes: Counter = Counter()
            gap: dict = {}
            for name in names:
                hit = await _search_one(client, name, est, date_ref)
                if hit:
                    d, bdt = hit   # bdt 已由 _search_one 解析（且必非 None，命中前已校验）
                    bid = d.get("id")
                    if bid is None:
                        continue
                    votes[bid] += 1
                    g = abs(((est or date_ref) - bdt).days) if (bdt and (est or date_ref)) else 999
                    gap[bid] = min(gap.get(bid, 10 ** 9), g)
            bgm_id = None
            if votes:
                # 优先被多个名字一致命中的；其次放送日最贴的
                ranked = sorted(votes, key=lambda i: (-votes[i], gap.get(i, 10 ** 9)))
                best = ranked[0]
                bkey = (votes[best], gap.get(best, 10 ** 9))
                tied = [i for i in ranked if (votes[i], gap.get(i, 10 ** 9)) == bkey]
                if len(tied) > 1:
                    # 【平票不绑，退『待识别』】以前这里直接取 sorted 的第一个——而 Python 的
                    # sorted 是稳定排序，全平局时"第一个"就是【候选名的书写顺序】，等于
                    # 拿种子标题里哪个名字写在前面来决定绑哪部番。
                    #
                    # 这不是理论风险：长番（集号 > 30）会走进 est=date_ref=None 那一支，
                    # 放送日校验整个关闭 → gap 恒为 999 → 只要几个名字各命中一个不同的 subject
                    # 就是全平局。真库 anime#99 实测：
                    #   '海贼王' → 311310「海贼王女」(2021-10-02，2-gram「海贼」误命中)
                    #   'ワンピース' → 90795     'One Piece S01E1174' → 975「航海王」(正确)
                    # 三票全平 → 取第一个 → 绑成「海贼王女」，随后 air_date=2021 早于开始使用日，
                    # 整部番被判「超期忽略」，一集都不会下。而且详情页的『重新识别』同样平局、
                    # 会把人工绑好的 975 再覆盖成另一部（实测 90390「航海王：奈美之章」）。
                    #
                    # 平局时【什么都不绑】更好：bgm_id 留 None 会落到下面的 Mikan-hash 桥——
                    # 那是按 info_hash 精确查番组，比"按名字书写顺序猜"可靠得多；桥也不通就留在
                    # 『待识别』等人工绑定，由 run_reenrich_retry 按退避重试。宁可多一条待办，
                    # 不要一个静默绑错的番（绑错之后没有任何告警，而它会连带决定归档目录与超期判定）。
                    log.info("bgm 识别平票（%d 个候选各 %d 票、放送日贴合度相同），不绑定、留待人工：%s",
                             len(tied), votes[best], "/".join(names)[:60])
                else:
                    bgm_id = best

            # ② 兜底：Mikan-hash 桥
            if bgm_id is None and info_hash:
                bgm_id = await _mikan_bridge(client, info_hash)

            # ③ 取 bgm 元数据（规范名/原名/放送日 + 简介/总集数/类型/封面/评分 + 原作/导演/音乐）
            meta = {}
            cast = None
            if bgm_id is not None:
                try:
                    r = await _retryable(lambda: client.get(f"{config.BGM_API}/v0/subjects/{bgm_id}", headers=_UA))
                    if r.status_code == 200:
                        j = r.json()
                        meta = j if isinstance(j, dict) else {}  # 防 bgm 返回数组/非对象
                except httpx.HTTPError:
                    _note_bgm_fail()   # 详情取不到（502/限流/超时）同样是【没问成 bgm】
                    meta = {}
                except ValueError:
                    meta = {}          # JSON 坏了——问到了，只是对方给的东西不对
                # 【声优单独限时，且失败不影响识别】走到这里 bgm_id 与 meta 都已到手，
                # 只差这一个【可选】字段（_fetch_cast 自己就返回 None 容错）。
                # 把它罩在整体预算里的话，一个挂死的 /characters 会让一次【已经成功】的识别
                # 整个作废、番退回『待识别』——而那正是本函数下面几行明写"只有【详情】取不到
                # 才算识别失败"要排除的情形。给它自己的小超时，兜住就好。
                try:
                    async with asyncio.timeout(_CAST_TIMEOUT):
                        cast = await _fetch_cast(client, bgm_id)
                except (TimeoutError, Exception) as e:   # noqa: B014  （TimeoutError 只为可读性）
                    log.info("声优信息取不到（不影响识别）：%s: %s", type(e).__name__, e)
                    cast = None

        if bgm_id is not None and not meta:
            # 【搜到了 id、却没取到详情】(bgm 502/限流/超时，或 subject 被删) 不能算识别成功：
            # _subject_to_info 会给出一个只有 bangumi_id、其余字段全空的 info，调用方据此把番记成
            # 『已识别』——从此 display_name/原名/放送日/总集数 永久为空，而 retry_unmatched 只捞
            # 『没有 bangumi_id』的番，这一部再也轮不到自动重试，只能人工进详情页点重新识别。
            # 当作本次识别失败：番停在『待识别』，下一次退避重试会连搜带取重来一遍。
            log.warning("富集：搜到 bgm id=%s 但详情取不到，按未识别处理（等下次重试）", bgm_id)
            return None
        if bgm_id is None and _parse_date(meta.get("date")) is None:   # bgm_id 命中即短路，跳过多余解析
            return None
        try:
            # 【纯本地转换单独兜住】到这里网络部分已经全部结束。把它留在下面那个 catch-all 里，
            # bgm 改一次响应形状（字段换类型）就会被记成"没问成 bgm"，进而每轮退款、
            # 每个节拍重打一遍 bgm——而三个端点其实全是 200。
            info = _subject_to_info(bgm_id, meta)
            info["cast"] = cast
        except Exception as e:
            log.warning("bgm 元数据解析失败（问到了，但对方给的形状不对）%s: %s", bgm_id, e)
            return None
        return info
    except Exception as e:
        # 走到这里的都是【请求机制本身】出了问题：建 client 失败（代理配错/缺 socksio）、
        # 传输层错误。纯本地的解析异常已经被上面那个 try 拦住，不会落进这里。
        _note_bgm_fail()
        # 【不能只接 httpx.HTTPError】代理配错时抛的不是它：socks5:// 少装 socksio 抛 ImportError、
        # 代理 URL 少写 scheme 抛 ValueError，两者都发生在【建 client】那一步、不继承 HTTPError。
        # 从这里逃出去只会被上游 worker 的 per-item except 收成一行"处理失败"，看不出是代理的问题。
        # 口径与 services/qbittorrent._request 对齐：识别失败就是识别失败，不该有掀翻调用方的权力。
        log.warning("富集失败 %s: %s: %s", (names[0] if names else info_hash or "")[:16],
                    type(e).__name__, e)
        return None
