"""剧场版 / OVA 逻辑（与 TV 番剧 anime.py 完全分离，只共用 engine 底层）。

来源仅 Mikan 季度浏览页的『剧场版/OVA 桶』——不碰 TV 那边的订阅源。识别用 bgm。
『是不是电影以 Mikan 桶为准』：桶里的一律当剧场版/OVA 收进 Movie（哪怕 bgm 把类型识别成 TV，
也只是详情页的 bgm 元数据，不改变它在剧场版列表里，也不转去番剧表）。剧场版一部作品逐版本人工点下。

【有意与 TV 侧不同的两点，均为产品决定，勿"对齐"补上】：
· 不受『开始使用日』(ANIME_START_DATE) 约束——那是给自动下载兜底的，剧场版本就不自动下。
· 无 flush 自动放行、也无 _revive_orphaned_skipped 式的自动换源兜底——
  哪一版下失败就停在那里，由用户在详情页自己挑另一版下，不替用户决定。
"""
import asyncio
import logging
import re
from collections import Counter
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import func, select

import config
from core import engine
from db import get_session
from db.models import AnimeTorrent, Movie, MovieTorrent
from services import enrich, fetch
from services.notify import event as notify_event
from sources import mikan
from sources.parse import format_quarter, quarter_sort_key

log = logging.getLogger("autorss")

_dl_lock = asyncio.Lock()  # 串行化剧场版下载，防同一片并发重复交 qB


def _has_handled_torrents(s, movie_id: int) -> bool:
    """该片是否已下过（在下/已下/停滞/曾删）——有则季度已落盘/曾落盘，重识别不该改（与番剧 _has_handled_torrents 同判据）。
    含 deleted（删过也算季度已定）+ stalled（半成品仍在盘、save_path 按旧季度写过），否则重识别把季度冲掉、文件散目录。"""
    return s.exec(select(MovieTorrent).where(
        MovieTorrent.movie_id == movie_id,
        MovieTorrent.status.in_(engine.HANDLED_STATUSES))).first() is not None


def _merge_movie(s, loser_id: int, keeper_id: int) -> None:
    """同一 bgm_id 裂成多条时合并：把 loser 的种子并到 keeper 并删 loser。

    剧场版只有『忽略(rejected)』一个人工状态（逐版本手动下，无审批/首选源），故合并时两方任一被忽略则仍忽略。
    """
    if loser_id == keeper_id:
        return
    keeper = s.get(Movie, keeper_id)
    loser = s.get(Movie, loser_id)
    # 【双方都已下过东西 → 拒绝静默合并】合并会【删行】且不可逆，而触发它的是一次可能出错的
    # 自动识别（bgm 的日期/名字匹配）。同系列的续作、重制版、总集编彼此极像，认错一次的代价
    # 就是把一部【已经下完】的片连同它的版本记录一起删掉，而用户看到的只是一句"识别成功 ✓"。
    # 两边都空着的时候合并是安全的（本来就是同一部片裂成了两行）；两边都有东西时几乎必然是认错了，
    # 那就宁可留下两行让人看见、也不要安静地删掉一行。
    if (keeper is not None and loser is not None
            and _has_handled_torrents(s, keeper_id) and _has_handled_torrents(s, loser_id)):
        log.warning("拒绝合并剧场版 %s → %s：两边都已下过文件，多半是识别认错了片。"
                    "两行都保留，请到 /movies 人工核对（合并会删行且不可逆）", loser_id, keeper_id)
        return
    if keeper is not None and loser is not None:
        keeper.rejected = keeper.rejected or loser.rejected
        s.add(keeper)
    for t in s.exec(select(MovieTorrent).where(MovieTorrent.movie_id == loser_id)):
        t.movie_id = keeper_id
        s.add(t)
    if loser is not None:
        s.delete(loser)
    s.commit()


# ---------------- 发现（Mikan 季度剧场版/OVA 桶 + bgm 识别） ----------------

def _upsert_movie(mikan_id: str, title: str, bgm_id: int | None,
                  info: dict | None, label: str) -> tuple[int, bool]:
    """按 bgm_id（无则 mikan_id）定位/新建 Movie，写入 bgm 元数据。返回 (movie_id, 是否新建)。

    是不是电影以 Mikan 桶为准（mikan_type，列表徽标用）；platform 存 bgm 的类型（详情页展示，跟 bgm）。
    """
    mikan_type = ("剧场版" if ("剧场" in label or "劇場" in label)
                  else "OVA" if ("OVA" in label or "OAD" in label or "OAV" in label) else "剧场版")
    with get_session() as s:
        movie = None
        if bgm_id is not None:
            movie = s.exec(select(Movie).where(Movie.bangumi_id == bgm_id)).first()
        if movie is None:
            movie = s.exec(select(Movie).where(Movie.mikan_id == mikan_id)).first()
        is_new = movie is None
        if movie is None:
            movie = Movie(title=title, mikan_id=mikan_id)
        # 【先把这个 mikan_id 从别的行上摘下来】mikan_id 现在是唯一索引，而下一行是
        # 【无条件】覆写：按 bgm_id 命中的那一行可能原本挂着另一个 mikan_id，
        # 而这个 mikan_id 又可能正挂在一部"早期扫进来、当时没识别出 bgm"的旧行上。
        # 不先摘就是 IntegrityError——加约束之前那只是多一行重复（观感问题），
        # 加约束之后会变成"这部片每一轮扫描都失败"，而日志只有一行『处理剧场版失败』。
        # 摘掉之后怎么收场，分两种：
        # · 被摘的那行有 bgm_id 且与本行相同 → 下面的身份守卫合并掉，没问题。
        # · 被摘的那行【没有 bgm_id】→ 它就此变成"无 mikan_id、无 bgm_id、却挂着种子"的
        #   孤儿：『刷新版本』按钮要 mikan_id，被禁用；重扫该年份也捞不回来——重扫会把这个
        #   mikan_id 认到【keeper】身上，孤儿永远等不到自己的那一轮。
        #   （这里原本写着"重扫该年份会重新补上"，那是假话。）
        #   两行先后引用过同一个 mikan_id，说明 Mikan 的这个番组【现在】被认到了 keeper 身上。
        #   （注意 mikan_id 有唯一约束，两行不可能【同时】持有同一个值，所以"共用"只是先后关系，
        #   "它们是同一部片"这件事并没有被证明——下面因此只合并"还没下过东西"的那种。）
        #   合并在 commit 之后做：新建时 movie.id 此刻还是 None，_merge_movie 需要 keeper id。
        stolen_from: list[int] = []
        for other in list(s.exec(select(Movie).where(Movie.mikan_id == mikan_id))):
            if movie.id is None or other.id != movie.id:
                # 注意不能写进 SQL 的 Movie.id != movie.id：新建时 movie.id 还是 None，
                # `id != NULL` 在 SQL 里恒为 NULL，一行都选不出来。
                other.mikan_id = None
                s.add(other)
                if other.bangumi_id is None:
                    stolen_from.append(other.id)
        s.flush()      # 【先把"摘掉"落下去】否则 flush 顺序不保证，可能先发出本行的 UPDATE
                       # 再发旧行的，中间那一瞬两行同值 —— 唯一索引照样 1062。
        movie.mikan_id = mikan_id
        movie.mikan_type = mikan_type   # Mikan 桶判定（剧场版/OVA），列表徽标用
        keep_path = (not is_new and _has_handled_torrents(s, movie.id))
        # 【快照要在任何写入之前拍】display_name 也决定目录（_movie_folder 取
        # jp_name or display_name，两者皆空才回退种子原名），而下面那句"空就填成 Mikan 展示名"
        # 正是把它从空变成有值的地方 —— 拍晚一格，冻的就是已经被改过的值。
        # 第一版只冻了 quarter/jp_name，实测目录仍从 `.../[组] 早下的片` 变成 `.../早下的片`。
        snap = ({k: getattr(movie, k) for k in ("quarter", "jp_name", "display_name")}
                if keep_path else None)
        if not movie.display_name:
            movie.display_name = title  # 无 bgm 时先用 Mikan 展示名兜底
        # 【连"原本为空"的也要冻】apply_bgm_meta 只冻已有值的字段（理由见那里）。可这条是
        # **后台扫描**链路：它没有 UI、不会 relocate。一部在 bgm 还没识别出来时就下过的片，
        # jp_name/quarter 都是空的、文件落在 `.../unknown/<种子原名>/`；下一轮扫描 bgm 识别成功
        # 就把这两个字段填上，于是归档目录整个换了地方，而盘上的文件没人去搬——
        # 页面显示的目录与实际落地从此分家，删除/重定位都会打空。
        # 想把它挪到正确目录，走详情页的『重新识别』：那条路径带 relocate，会把文件一起搬过去。
        engine.apply_bgm_meta(movie, info, keep_path=keep_path)
        if snap is not None:
            for k, v in snap.items():
                setattr(movie, k, v)
        s.add(movie)
        s.commit()
        s.refresh(movie)
        if movie.bangumi_id is not None:  # 身份守卫：同 bgm_id 合并
            # 【这里的合并是安全的，别照搬番剧侧 E-18 的"自动路径不删行"】——我试着搬过一次，是错的。
            # 两者形状不同：
            #   · 番剧侧 E-18 防的是"自动识别把 X 【绑错】到 bgm B，然后删掉本来正确地占着 B 的 Y"，
            #     错在【绑定】那一步，而删掉的是一部完全不同的番。
            #   · 这里的 keeper 是【按 bgm_id 查出来的】（见本函数开头的查找顺序），
            #     所以守卫命中时两行本来就声称同一个 subject —— 合并是构造上正确的；
            #     而且 _merge_movie 会先把种子搬到 keeper 再删行，盘上文件不受影响，
            #     丢的只是一行重复的元数据。
            # 真正危险的是【摘 mikan_id】那条路（下面几行）：那里两行只是先后引用过同一个
            # mikan_id，"同一部片"从没被证明过 —— 所以那条才需要"loser 下过就不并"。
            for other in list(s.exec(select(Movie).where(
                    Movie.bangumi_id == movie.bangumi_id, Movie.id != movie.id))):
                _merge_movie(s, other.id, movie.id)
        # 【别把被摘走 mikan_id 的无主行丢在原地】理由见上面摘 mikan_id 那段。
        # 【但只并"还没下过东西"的那种】——这道限制不能省，我第一版就漏了：
        # _merge_movie 的保险是"**两边都**下过就拒绝"，而这条新路径上 keeper 的典型情形恰恰是
        # "刚按 bgm 命中、一份都没下过"，于是保险在 `loser 下过 / keeper 没下过` 这一半上恒为假，
        # 而那正是代价最大的一半：_merge_movie 最后一步是 s.delete(loser)，
        # 丢掉的是 Movie 整行（title/quarter/jp_name/display_name 与全部 bgm 元数据），
        # 页面上那部片直接消失、版本挂到另一部名下，不可逆，且日志里一个字都没有。
        # （种子行会跟过去、save_path 保留、盘上文件不打空——丢的是"这是哪部片"这件事。）
        #
        # 【为什么不改 _merge_movie 而是在这里限制】那道保险服务的是"同 bgm_id 裂成两行"的场景，
        # 那里两行【确实】是同一部片，loser 有文件时并过去是正确的修复。而这条路径不一样：
        # 两行只是先后引用过同一个 mikan_id（mikan_id 有唯一约束，不可能同时持有），
        # "它们是同一部片"这件事没有被证明过。作用域不同的两件事，别共用一道闸。
        for oid in stolen_from:
            other = s.get(Movie, oid)
            if other is None or other.bangumi_id is not None:
                continue
            if _has_handled_torrents(s, oid):
                log.warning("剧场版 %s（%s）的 Mikan 链接被 %s 取走，而它已经下过东西——"
                            "不合并、保留该行以免删掉你已有的片；它会以『无 Mikan 链接』的样子"
                            "留在 /movies 上，可到『待识别』手动绑 bgm 后由身份守卫合并",
                            oid, other.display_name or other.title, movie.id)
                continue
            _merge_movie(s, oid, movie.id)
        return movie.id, is_new


def _store_movie_torrents(movie_id: int, items: list) -> int:
    """把某剧场版番组抓来的种子入库（按 hash 去重、逐条提交），全部 pending。返回新增数。

    【入库前先确认这部 Movie 还在】movie_id 是本轮扫描早些时候 _upsert_movie 拿到的，
    而拿到它之后我们又 await 了两次网络（fetch_detail / fetch_bangumi_torrents，各可能几秒）。
    这期间用户完全可能在页面上把这部片绑到别的 bgm 上、于是它被合并掉了——
    此时挂上去的种子会变成孤儿：它占住了 info_hash（本函数按 hash 全局去重），
    而真正那部 Movie 从此【永远收不到这几个版本】，页面上也看不到它们。
    TV 侧早就有同款守卫（core/anime.py 的 backfill：await 之后重取，悬空就放弃本轮）。
    """
    n = 0
    with get_session() as s:
        if s.get(Movie, movie_id) is None:
            log.info("剧场版 %s 在抓种期间已被合并/删除，本轮的 %d 个版本不入库（下轮扫描会重新归位）",
                     movie_id, len(items))
            return 0
        # 【一次 IN 预取，不要逐条查】(R21) 原来这里对每个版本各发一条
        # `SELECT <整行> WHERE info_hash = ?`。站上的版本绝大多数上轮已入库，于是每次重扫
        # 都要把已知的 hash 各查一遍：真库 70 部 / 569 个版本 = 639 条 SQL、657ms 的同步阻塞，
        # 而这跑在事件循环上（页面、下载放行、qB 同步一起卡）。番剧侧的 poll_once 早就是
        # 批量预取了，这一半漏了 —— 第①号形状。
        known = engine.existing_hashes(MovieTorrent, [i.info_hash for i in items])
        for item in items:
            if item.info_hash in known:
                continue
            known.add(item.info_hash)   # 同一批里出现两次同 hash 时也只入一条
            s.add(MovieTorrent(
                info_hash=item.info_hash, movie_id=movie_id, source=item.source,
                site=item.site, raw_title=item.raw_title, download_url=item.download_url,
                release_time=item.release_time, priority=item.priority, status="pending",
            ))
            try:
                s.commit()
                n += 1
            except IntegrityError:
                s.rollback()
    return n


async def discover_movies(year: int, seasons: list[str] | None = None) -> dict:
    """扫描 Mikan 指定年份/季度的剧场版·OVA 桶，识别(bgm)入库为 Movie 并抓其种子。

    seasons：['A','B','C','D'] 子集（冬春夏秋），None=全年四季。
    『是不是电影』以 Mikan 桶为准——桶里的一律当剧场版/OVA 收进 Movie（哪怕 bgm 把类型识别成 TV，
    也只是详情页的 bgm 元数据，不改变它在剧场版列表里）。本函数只碰 Movie/MovieTorrent，不写 TV 表。
    返回 {'movies','torrents','seen','errors'}。
    """
    seasons = seasons or ["A", "B", "C", "D"]
    try:
        async with mikan.make_client() as client:
            return await _discover_loop(client, year, seasons)
    except Exception as e:
        # 【建 client 那一步也会抛，而它在原来的 try 之外】代理填成 '127.0.0.1:7890'（漏 scheme）
        # 抛 ValueError、填 'socks5://…' 而没装 socksio 抛 ImportError —— 两者都不属 httpx 异常族，
        # 且都发生在【建 AsyncClient 时】而不是发请求时。漏出去就一路逃进 /movies 那个
        # 『立即扫描』按钮的 on_click：先弹"扫描中…请稍候"，然后永远没有下文，页面零反馈。
        # refresh_movie_torrents 早就为这件事加了同款守卫（47 行之下），这里没跟上。
        log.error("剧场版扫描失败 %s：%s: %s", year, type(e).__name__, e)
        return {"movies": 0, "torrents": 0, "seen": 0, "errors": 1}


async def _discover_loop(client, year: int, seasons: list) -> dict:
    """discover_movies 的主体（拆出来只是为了让建 client 也能被同一个 try 罩住）。"""
    added_movies = added_torrents = seen = errors = 0
    for letter in seasons:
        try:
            bucket = await mikan.discover_movie_bucket(client, year, letter)
        except Exception as e:
            log.error("发现剧场版失败 %s%s: %s", year, letter, e)
            errors += 1
            continue
        log.info("Mikan %s年%s 剧场版/OVA 桶：%d 部",
                 year, mikan.season_cn(letter), len(bucket))
        for mikan_id, title, mlabel in bucket:
            try:
                bgm_id = await mikan.fetch_detail(client, mikan_id)
                info = await enrich.fetch_by_id(bgm_id) if bgm_id is not None else None
                movie_id, is_new = _upsert_movie(mikan_id, title, bgm_id, info, mlabel)
                seen += 1
                added_movies += 1 if is_new else 0
                items = await mikan.fetch_bangumi_torrents(client, mikan_id)
                added_torrents += _store_movie_torrents(movie_id, items)
            except Exception as e:
                log.error("处理剧场版失败 mikan=%s(%s): %s", mikan_id, title, e)
                errors += 1
    log.info("剧场版发现完成 %s：命中 %d/新增 %d/种子 %d，出错 %d",
             year, seen, added_movies, added_torrents, errors)
    return {"movies": added_movies, "torrents": added_torrents, "seen": seen, "errors": errors}


async def refresh_movie_torrents(movie_id: int) -> dict:
    """按需重拉这一部剧场版的全部版本（Mikan `/RSS/Bangumi?bangumiId=<mikan_id>`）。

    【为什么需要它】剧场版的 BD 普遍在首映后 6~18 个月才出。发现是按季度整年扫的，
    而"每隔几个月人工把整年重扫一遍，只为看某一部有没有出新版本"实践中等于不会发生——
    于是 `Movie.mikan_id` 那句"刷新种子 RSS 用"的注释从上线起就没有兑现过。
    一次只发一个 RSS 请求，比整年重扫便宜三个数量级。

    返回 {"ok": bool, "msg": str, "added": int, "seen": int}。
    """
    with get_session() as s:
        m = s.get(Movie, movie_id)
        if m is None:
            return {"ok": False, "msg": "这部片已不存在（可能刚被合并）", "added": 0, "seen": 0}
        mid = (m.mikan_id or "").strip()
    if not mid:
        # 早期扫描入库的、或被合并时保留了另一侧的行，可能没有 mikan_id。
        return {"ok": False, "msg": "这部片没有 Mikan 番组 id，无法刷新（重新扫描该年份可补上）",
                "added": 0, "seen": 0}
    try:
        async with mikan.make_client() as client:
            items = await mikan.fetch_bangumi_torrents(client, mid)
    except Exception as e:
        # 【必须接住，且不能只接 httpx.HTTPError】本函数 4 行之上的另一条失败支
        # （没有 mikan_id）是老老实实按契约返回的，这条却直接抛——而 NiceGUI 的
        # on_click 里逃出去的异常只进服务端日志，用户看到的是"按钮点了没反应"。
        # 代理配错时抛的是 ImportError / ValueError，都不属 httpx 异常族。
        # 脱敏：这个 msg 会被页面原样弹成红字，而 MIKAN_BASE 是设置页可改的地址
        # （自建镜像可能带凭据），httpx 异常的 str() 带完整 URL。日志那半有过滤器兜着，
        # 返回值这半没有 —— 这一处是 R22 的按出口守卫替我找出来的第 5 个出口。
        emsg = fetch.redact(e)
        log.warning("刷新剧场版版本失败 movie=%s mikan=%s: %s", movie_id, mid, emsg)
        return {"ok": False, "msg": f"Mikan 取不到：{type(e).__name__}: {emsg}",
                "added": 0, "seen": 0}
    # _store_movie_torrents 自带"入库前重取、悬空就放弃"的守卫（我们刚 await 过网络）
    added = _store_movie_torrents(movie_id, items)
    log.info("刷新剧场版版本 movie=%s mikan=%s：站上 %d 个，新增 %d", movie_id, mid, len(items), added)
    return {"ok": True, "msg": "", "added": added, "seen": len(items)}


async def scan_now(year: int, seasons: list[str] | None = None) -> dict:
    """扫描一次并记下扫描时间（手动『立即扫描』与后台自动扫描共用）。只碰剧场版，不涉 TV。"""
    res = await discover_movies(year, seasons)
    # 只有覆盖当年四季的完整扫描才刷新自动扫描时间基准；手动只扫单季(回填历史)不该顶掉它、推迟自动全年扫。
    # 整轮网络故障（一部没命中且有报错）不刷新基准——否则一次抓取失败就要等满一个间隔才重试；留给 5 分钟心跳重扫。
    total_fail = res["seen"] == 0 and res["errors"] > 0
    # 【还必须是"当年"】MOVIE_SCAN_LAST 是自动扫描的节拍基准，而自动扫描扫的恒是【当年】四季
    # （见 auto_scan_tick）。手动去补抓 2023 年的老片同样是"四季全选"，不加年份判断就会把基准
    # 顶到现在 —— 当年的新片最长 MOVIE_SCAN_INTERVAL（默认 7 天）不再被自动抓，
    # 而 /movies 上"上次扫描 刚刚"看起来一切正常，没有任何迹象说明漏了。
    if (year == datetime.now().year
            and (seasons is None or set(seasons) >= {"A", "B", "C", "D"}) and not total_fail):
        config.set_many({"MOVIE_SCAN_LAST": datetime.now().isoformat(timespec="seconds")})
    return res


async def auto_scan_tick() -> bool:
    """后台心跳调用：开了自动扫描且到点（距上次 ≥ MOVIE_SCAN_INTERVAL）就扫当年四季。扫了返回 True。"""
    if not config.MOVIE_SCAN_ENABLED:
        return False
    last = config.MOVIE_SCAN_LAST
    if last:
        try:
            elapsed = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
            if 0 <= elapsed < config.MOVIE_SCAN_INTERVAL:
                return False  # 还没到点（elapsed<0=系统时钟被回拨，视作到点、照扫，自愈不停摆）
        except ValueError:
            pass
    await scan_now(datetime.now().year)
    return True


# ---------------- 查询（给 /movies 页） ----------------

def overview() -> dict:
    """/movies 仪表盘的聚合数据：KPI + 各季度(电影数/已下) + qB 实时态。"""
    with get_session() as s:
        all_m = list(s.exec(select(Movie)))
        # 种子维度用 SQL 聚合（不整表拉进内存）
        status = {st: c for st, c in s.exec(
            select(MovieTorrent.status, func.count()).group_by(MovieTorrent.status))}
        versions = s.exec(select(func.count()).select_from(MovieTorrent)).one()
        dl_ids = set(s.exec(select(MovieTorrent.movie_id)
                            .where(MovieTorrent.status == "sent").distinct()))
    active = [m for m in all_m if not m.rejected]
    # 【剧场版按【年】分布，不按季度】(R31) E-30 已经拍板"剧场版按年归档"、
    # `movie_quarter_of` 从此恒返回 `<yy>A`；而这里一直按**季度键**分组 ——
    # 于是存量那 53 条带着旧规则算出的 B/C/D 季母，真库 6 个年份被劈成 **15 根柱**
    # （例：`('25D',9) ('25C',9) ('25B',5) ('25A',3)` 全是 2025 年），
    # 而今后新入库的片一律落进 `<yy>A`：同一年的新旧片分在不同柱上。
    # 季母对剧场版没有意义（页面上那一栏本来就叫『年份』，编辑框也是），所以这里直接按年聚。
    # 这样 E-47（存量季母回填）就只剩"库里好看一点"，不再是显示正确的前提。
    def _year_of(m):
        y = engine.quarter_year(m.quarter or "")
        return str(y) if y is not None else "未知"

    y_of = {m.id: _year_of(m) for m in active}   # 键域即 active 的 id 集，成员判断复用它
    total_by_q = Counter(y_of[m.id] for m in active)
    dl_by_q = Counter(y_of[mid] for mid in dl_ids if mid in y_of)
    qs = sorted((y for y in total_by_q if y != "未知"), key=lambda y: -int(y))
    if "未知" in total_by_q:
        qs.append("未知")
    return {
        "kpi": {
            "total": len(active),
            "matched": sum(1 for m in active if m.bangumi_id),
            "unmatched": sum(1 for m in active if not m.bangumi_id),
            "rejected": sum(1 for m in all_m if m.rejected),
            "versions": versions,
        },
        # 键名叫 by_year 不叫 by_quarter：它装的是【四位年】，不是季度键。见上面 _year_of。
        "by_year": [(y, total_by_q.get(y, 0), dl_by_q.get(y, 0)) for y in qs],
        # 八种应用侧 status 全列（含 deleted/excluded），与番剧侧同口径：仪表盘『种子数』号称"各状态之和"
        # 八状态【全集】取自 engine，别再手抄：仪表盘对用户承诺"种子数 = 各状态之和"，
        # 将来加第 9 个状态时漏改这里，页面数字就会静默对不上（且不报错）
        "status": {k: status.get(k, 0) for k in engine.ALL_STATUSES},
        "qb": engine.qb_summary(MovieTorrent),
        "config": {"qb": config.QB_ENABLED},
    }


def year_brief() -> list[dict]:
    """列表页顶部小结：今年 + 上年 的剧场版分布（已识别/待识别/已忽略）+ 种子维度（已下/待下/版本）。
    剧场版按年归档（quarter 前两位=年份后两位），故按年而非季度小结。"""
    now_year = datetime.now().year

    yr_of = engine.quarter_year

    with get_session() as s:
        movies = list(s.exec(select(Movie.id, Movie.quarter, Movie.rejected, Movie.bangumi_id)))
        # 种子按 (movie_id,状态) 库内聚合，再按影片年份归拢（MovieTorrent 无 quarter 列）
        tcounts = list(s.exec(select(MovieTorrent.movie_id, MovieTorrent.status, func.count())
                              .group_by(MovieTorrent.movie_id, MovieTorrent.status)))
    yr_by_mid = {mid: yr_of(q) for mid, q, _, _ in movies}
    out = []
    for tag, yr in (("今年", now_year), ("上年", now_year - 1)):
        mv = [(rej, bid) for _, q, rej, bid in movies if yr_of(q) == yr]
        qc: dict[str, int] = {}
        for mid, st, c in tcounts:
            if yr_by_mid.get(mid) == yr:
                qc[st] = qc.get(st, 0) + c
        out.append({
            "tag": tag, "key": yr,
            "total": len(mv),
            "matched": sum(1 for rej, bid in mv if not rej and bid),   # 已识别（有 bgm、未忽略）
            "fail": sum(1 for rej, bid in mv if not rej and not bid),  # 待识别（未匹配 bgm）
            "ignored": sum(1 for rej, bid in mv if rej),               # 已忽略
            "versions": sum(qc.values()),
            "done": qc.get("sent", 0),
            "pending": sum(qc.get(k, 0) for k in engine.DOWNLOADABLE_STATUSES),
        })
    return out


def list_unmatched_movies() -> list[Movie]:
    """未识别（bgm 没匹配上）的剧场版/OVA——供『待识别』tab 手动绑定。"""
    with get_session() as s:
        return list(s.exec(select(Movie).where(
            Movie.bangumi_id.is_(None), Movie.rejected.is_not(True))
            .order_by(Movie.created_at.desc())))


def list_movies() -> list[Movie]:
    """未忽略的剧场版/OVA（/movies 页展示）。"""
    with get_session() as s:
        return list(s.exec(select(Movie).where(Movie.rejected.is_not(True))
                           .order_by(Movie.quarter.desc(), Movie.id)))


def list_rejected_movies() -> list[Movie]:
    """已忽略的剧场版/OVA（/movies 页底部『已忽略』区展示，可恢复）。"""
    with get_session() as s:
        rows = list(s.exec(select(Movie).where(Movie.rejected == True)))  # noqa: E712
    # 【不能用 SQL 的 ORDER BY quarter DESC】季度键的年份只有两位，字符串比较下 '99D' > '26C'，
    # 1999 年的片会排到最前。别处的季度排序都过 quarter_sort_key，唯独这条是【平铺渲染】——
    # pages/movies.py 的『已忽略』页直接按本函数的返回序渲染，没有上层分组重排来兜底，
    # 所以这里排错就是用户直接看到的错。
    return sorted(rows, key=lambda m: (quarter_sort_key(m.quarter), -(m.id or 0)), reverse=True)


def get_movie(movie_id: int) -> Movie | None:
    with get_session() as s:
        return s.get(Movie, movie_id)


def movie_torrents(movie_id: int) -> list[MovieTorrent]:
    with get_session() as s:
        return list(s.exec(select(MovieTorrent).where(MovieTorrent.movie_id == movie_id)
                           .order_by(MovieTorrent.created_at.desc())))


def source_map() -> dict:
    """{片 id: [来源...]}，一次 DISTINCT 查齐（对齐番剧 source_map）。

    列表页/待识别页逐片调 movie_sources 是 N+1：每片一个 session、一条 SQL；
    片数一多（剧场版按年攒）渲染就线性变慢。这里一次查完，行数只与『片×来源』有关。
    """
    from collections import defaultdict
    with get_session() as s:
        pairs = list(s.exec(select(MovieTorrent.movie_id, MovieTorrent.source).distinct()))
    src: dict = defaultdict(set)
    for mid, source in pairs:
        if mid:
            src[mid].add(source or "?")
    return {mid: sorted(v) for mid, v in src.items()}


def movie_sources(movie_id: int) -> list[str]:
    with get_session() as s:
        rows = s.exec(select(MovieTorrent.source).where(MovieTorrent.movie_id == movie_id)).all()
    return sorted({r for r in rows if r})


def torrents_by_movie(movie_ids: list[int]) -> dict[int, list[MovieTorrent]]:
    """一次查出多部片的种子，按 movie_id 归组（列表页批量渲染用，免得每张卡片各查 2 次库=N+1）。"""
    if not movie_ids:
        return {}
    out: dict[int, list[MovieTorrent]] = {}
    with get_session() as s:
        for t in s.exec(select(MovieTorrent).where(MovieTorrent.movie_id.in_(movie_ids))
                        .order_by(MovieTorrent.created_at.desc())):
            out.setdefault(t.movie_id, []).append(t)
    return out


def recent_movie_rows(limit: int = 50) -> list[dict]:
    """新入库列表：剧场版/OVA 种子 + 片的规范名（比原始种子标题可读）+ 原始种子标题。

    MovieTorrent 表只含剧场版/OVA 种子（TV 在 AnimeTorrent），故无需再过滤。
    """
    with get_session() as s:
        ts = list(s.exec(select(MovieTorrent).order_by(MovieTorrent.created_at.desc()).limit(limit)))
        ids = {t.movie_id for t in ts if t.movie_id}
        names = ({m.id: (m.display_name or m.title) for m in
                  s.exec(select(Movie).where(Movie.id.in_(ids)))} if ids else {})
    return [{
        "id": t.id,
        "movie_id": t.movie_id,
        "time": engine.torrent_time(t),
        "name": names.get(t.movie_id) or (t.raw_title or "?"),
        "source": t.source,
        "status": t.status,
        "qb_state": t.qb_state,
        "qb_progress": t.qb_progress,
        "qb_synced_at": t.qb_synced_at,
        "qb_dlspeed": t.qb_dlspeed,
        "raw": t.raw_title or "",
    } for t in ts]


def inflight_movie_rows(limit: int = 50) -> list[dict]:
    """仪表盘『正在下载』区：当前在下的剧场版/OVA 种子（口径同 has_inflight），按完成度降序。"""
    with get_session() as s:
        ts = list(s.exec(
            select(MovieTorrent).where(*engine._inflight_where(MovieTorrent))
            .order_by(MovieTorrent.qb_progress.desc(), MovieTorrent.created_at.desc()).limit(limit)))
        ids = {t.movie_id for t in ts if t.movie_id}
        names = ({m.id: (m.display_name or m.title) for m in
                  s.exec(select(Movie).where(Movie.id.in_(ids)))} if ids else {})
    return [{
        "id": t.id,
        "name": names.get(t.movie_id) or (t.raw_title or "?"),
        "status": t.status,
        "qb_state": t.qb_state,
        "qb_progress": t.qb_progress,
        "qb_synced_at": t.qb_synced_at,
        "qb_dlspeed": t.qb_dlspeed,
    } for t in ts]


# ---------------- 操作（给 /movies 页 + 详情） ----------------

def reject_movie(movie_id: int) -> None:
    with get_session() as s:
        m = s.get(Movie, movie_id)
        if m is None:
            return
        m.rejected = True
        s.add(m)
        for t in s.exec(select(MovieTorrent).where(
                MovieTorrent.movie_id == movie_id,
                MovieTorrent.status.in_(engine.DOWNLOADABLE_STATUSES))):
            t.status = "skipped"
            s.add(t)
        s.commit()


def restore_movie(movie_id: int) -> None:
    with get_session() as s:
        m = s.get(Movie, movie_id)
        if m is None:
            return
        m.rejected = False
        s.add(m)
        rows = list(s.exec(select(MovieTorrent).where(MovieTorrent.movie_id == movie_id)))
        # deleted/stalled 也算『有过一版』：唯一版本被删/停滞后，不该把 skipped 旧版翻出来重下（与番剧 _HANDLED 一致）
        anydl = any(t.status in engine.HANDLED_STATUSES for t in rows)
        for t in rows:  # 剧场版=一部作品：已有一版就别把 skipped 旧版翻出来（deleted 是用户主动删，也不重下）
            if t.status == "skipped" and not anydl:
                t.status = "pending"
                s.add(t)
        s.commit()


def _terminal_torrent_rows(status: str) -> list[dict]:
    """某终态(deleted/excluded)的剧场版种子行（片名/原名），供『已忽略』页底部折叠展示。"""
    with get_session() as s:
        ts = list(s.exec(select(MovieTorrent).where(MovieTorrent.status == status)
                         .order_by(MovieTorrent.created_at.desc())))
        ids = {t.movie_id for t in ts if t.movie_id}
        names = ({m.id: (m.display_name or m.title) for m in
                  s.exec(select(Movie).where(Movie.id.in_(ids)))} if ids else {})
    return [{"id": t.id, "movie_id": t.movie_id,
             "name": names.get(t.movie_id) or "?", "raw": t.raw_title or ""} for t in ts]


def deleted_torrent_rows() -> list[dict]:
    """已删除(deleted)的剧场版种子——『已删除种子』折叠 + 重新下载找回（与番剧对称）。"""
    return _terminal_torrent_rows("deleted")


def excluded_torrent_rows() -> list[dict]:
    """已排除(excluded)的剧场版种子——『已排除种子』折叠 + 恢复放回可下（与番剧对称）。"""
    return _terminal_torrent_rows("excluded")


def exclude_torrent(mt_id: int) -> bool:
    """排除一条不想要的待下版本（实现见 engine.exclude_torrent，两条线共用）。
    不再显示在可下队列、RSS 再遇到同 hash 也不重收；可用 unexclude_torrent 撤销。"""
    return engine.exclude_torrent(MovieTorrent, mt_id)


def unexclude_torrent(mt_id: int) -> bool:
    """取消排除：把 excluded 的版本放回 pending（可下）。返回是否放回了。"""
    return engine.unexclude_torrent(MovieTorrent, mt_id)


async def enrich_movie(movie_id: int) -> bool:
    """手动重识别某剧场版：用已有名字 + 最近一条种子回退，重取 bgm 元数据覆盖。"""
    with get_session() as s:
        m = s.get(Movie, movie_id)
        if m is None:
            return False
        t = s.exec(select(MovieTorrent).where(MovieTorrent.movie_id == movie_id)
                   .order_by(MovieTorrent.created_at.desc())).first()
        names = [n for n in (m.display_name, m.jp_name, m.title) if n]
        info_hash = t.info_hash if t else None
        bgm_before = m.bangumi_id      # 见下面 await 之后的那道 compare-and-set
    # 【不传 release_time】那是【种子上架日】，不是【首映日】。剧场版的 BD/WEB 版普遍在首映后
    # 6~18 个月才发，把上架日当首映日去做日期校验，会把正确的 subject 判成"日期对不上"而排除，
    # 转而命中同系列的【另一部】（续作/重制版往往就在那个年份）。而下面紧接着就是
    # `Movie.bangumi_id == m.bangumi_id → _merge_movie` —— 认错的直接后果是把【正主那一行】
    # 连同它已下好的版本一起删掉，UI 上还弹一句绿色的"识别成功 ✓"。
    # TV 侧 services/enrich.py 早有同一条规矩："基准不可靠就【不设】基准"，这里把它用上。
    info = await enrich.resolve(names, None, None, info_hash)
    with get_session() as s:
        m = s.get(Movie, movie_id)
        if m is None:
            return False
        # 【await 期间别人改了 bangumi_id 就让路】与番剧侧 core.anime.enrich_anime 同一道闸——
        # 那边修好之后【这边漏了】，是本项目最常见的广度错误（同一件事有两处，只改了一处）。
        # enrich.resolve 的整体预算是 120 秒，是全项目最长的 await 窗口之一；原来这里只重取了
        # 一次 m，挡住的是"这条记录在 await 期间被删了"，没挡"绑定在 await 期间变了"——
        # 而 apply_bgm_meta 对 bangumi_id 是【无条件覆写】的，紧接着下面就是 _merge_movie，
        # 它的最后一步是 s.delete(loser)：用户在同一个弹窗里点『重新识别』又点『绑定 bgm』填了
        # 正确的 subject，几十秒后后台回来把它盖掉，还顺手删掉正主那一行（可能是已经下完的那部），
        # UI 上弹的却是绿色的『识别成功 ✓』。
        # 判据用 compare-and-set 而不是"有值就不覆盖"：『重新识别』本来就是"我要你重算"，
        # 它读到的 bgm_before 与回来时相同、照常覆写；只有【第三方在这段窗口里改过】才让路。
        dup_names = ""
        if m.bangumi_id != bgm_before:
            log.info("重新识别期间该片的 bgm 绑定已被改动（%s → %s），本次结果作废、以后者为准：%s",
                     bgm_before, m.bangumi_id, m.display_name or m.title)
            return m.bangumi_id is not None
        # 【撞车时干脆不写 bangumi_id】(R22)
        # R21 只是把这里的"合并删行"去掉了，可**删除动作在 `_upsert_movie` 里原样保留着**，
        # 而那一处的立论是 R20 写的「keeper 是按 bgm_id 查出来的、两行本来就声称同一个 subject」——
        # 一旦这里把一个**未经证明**的 bgm_id 写进去，那个立论就变成循环论证：
        # 下一轮剧场版扫描（自动到点，或用户点一次『扫描』）处理另一部片时调 `_upsert_movie`，
        # 它按这个 bgm_id 查出 keeper、`s.delete` 掉正确的那一行 —— 删除只是被**推迟**了一轮，
        # 而且这一次是全自动发生的，连日志都归在扫描那一侧。
        # 所以撞车时连绑定本身都不写：只记日志 + 推 backlog，等用户走带回显的『绑定 bgm』落定。
        # 那条路有 `bind_preview` + `require_bind_confirm`，"这两行是同一部"由用户明确说出来。
        new_bgm = (info or {}).get("bangumi_id")
        clash = list(s.exec(select(Movie).where(
            Movie.bangumi_id == new_bgm, Movie.id != m.id))) if new_bgm else []
        if clash:
            names = "、".join(f"#{o.id}「{o.display_name or o.title}」" for o in clash)
            log.warning("识别把 #%s「%s」认成了 bgm %s，而 %s 已经占着它 —— "
                        "**本次不写绑定**（写进去会让下一轮扫描的 _upsert_movie 按它删掉正主那一行）。"
                        "要合并请到 /movies 走带回显的『绑定 bgm』",
                        m.id, m.display_name or m.title, new_bgm, names)
            dup_bgm, dup_me, dup_names = new_bgm, (m.display_name or m.title), names
        else:
            # 手动重识别：允许更新季度（哪怕已下过）——季度变了由 UI 层确认后 relocate_movie 搬已下文件
            engine.apply_bgm_meta(m, info, keep_path=False)
            s.add(m)
            s.commit()
        # 【识别路径一律不合并、不删行】(R21，与 core.anime.enrich_anime 同一条规矩)
        # 原来这里是 `Movie.bangumi_id 相同 → _merge_movie`，而 _merge_movie 的最后一步是
        # `s.delete(loser)`，没有撤销入口。合并的前提是"用户明确说这两条是同一部"——
        # 而本函数的入口（详情页/『待识别』的『重新识别』、后台重识别）没有一个满足它：
        # 人点下去之前【根本不知道会绑到哪个 subject】，没法预先回显。
        # 上面那段注释自己就写着这条路的风险：剧场版的续作/重制/总集编彼此极像，
        # 而"上架日当首映日"的日期校验对它们系统性地判错。
        #
        # 【番剧侧 R20 就收口了，这一半漏到 R21 才补】——同一件事有两处、只改了一处，
        # 本项目第①号形状。R20 当时还特意回退过 `_upsert_movie` 那一处的同款改动，
        # 理由是"那里的 keeper 是按 bgm_id 查出来的、合并是构造上正确的"——
        # 那个判断对 `_upsert_movie` 成立，但**这里不是那条路**：这里的 bgm_id 来自一次
        # 全新的 `enrich.resolve`，两行"是同一部"从没被证明过。
        #
        # 留下的两条同 bgm_id 记录靠日志 + backlog 通知报出来，
        # 用户到 /movies 走『绑定 bgm』那条带回显（bind_preview + require_bind_confirm）的路合并。
        if not clash and m.bangumi_id is not None:
            # 兜底：撞车判定与写入之间没有 await，正常到不了这里；
            # 留着是因为"两条同 bgm_id"还可能由别的路径造出来（历史数据、并发的绑定）。
            dup = list(s.exec(select(Movie).where(
                Movie.bangumi_id == m.bangumi_id, Movie.id != m.id)))
            if dup:
                dup_names = "、".join(f"#{o.id}「{o.display_name or o.title}」" for o in dup)
                log.warning("识别把 %s 绑到了 bgm %s，而 %s 已经占着这个 bgm —— "
                            "本路径不合并、不删行（合并只在『绑定 bgm』那条带回显的路上做），"
                            "请人工核对",
                            f"#{m.id}「{m.display_name or m.title}」", m.bangumi_id, dup_names)
                dup_bgm, dup_me = m.bangumi_id, (m.display_name or m.title)
    if dup_names:
        await notify_event(
            "backlog", f"{dup_me} 与 {dup_names} 绑到了同一个 bgm，"
                       "识别没有合并（合并会删记录）。到剧场版页核对一下",
            key=f"dupmovie:{dup_bgm}", cooldown=24 * 3600)
    return bool(info)


def bind_preview(movie_id: int, bgm_id: int) -> dict:
    """剧场版侧的『绑定 bgm』回显 —— 与 core.anime.bind_preview 对称，返回同一个形状。

    番剧侧加了回显闸之后，这一半一直是漏的：`bind_movie_bgm` 末尾同样有身份守卫
    （core/movies.py 的 `_merge_movie`），最后一步同样是 `s.delete(loser)`，
    两个调用点（详情页『绑定 bgm』、列表页待识别的『绑定』）却零预览零确认。
    「番剧侧补了、剧场版侧没补」正是本项目最常见的那种广度错误。

    与番剧侧的差别只有一处：剧场版没有集号，所以不产生「同一集两个编号」那类告警，
    warn 恒为空；要说明的只有"哪一条记录会被删、它下过几个版本"。
    """
    out: dict = {"merge": [], "warn": []}
    with get_session() as s:
        me = s.get(Movie, movie_id)
        if me is None:
            return out
        for o in s.exec(select(Movie).where(Movie.bangumi_id == bgm_id, Movie.id != movie_id)):
            rows = list(s.exec(select(MovieTorrent).where(MovieTorrent.movie_id == o.id)))
            out["merge"].append({
                "id": o.id,
                "name": o.display_name or o.title or "?",
                "state": "已忽略" if o.rejected else "正常",
                "torrents": len(rows),
                "handled": sum(1 for t in rows if t.status in engine.HANDLED_STATUSES),
                "aliases": 0,      # 剧场版没有别名表，形状对齐番剧侧即可
                "episodes": [],
            })
    return out


async def bind_movie_bgm(movie_id: int, bgm_id: int, report: dict | None = None) -> bool:
    """手动把剧场版绑定到指定 bgm subject id：取元数据覆盖 + 身份合并。"""
    info = await enrich.fetch_by_id(bgm_id)
    if not info:
        return False
    with get_session() as s:
        m = s.get(Movie, movie_id)
        if m is None:
            return False
        # 手动纠正绑定：允许更新年份/片名（哪怕已下过）——变了由 UI 层确认后 relocate_movie 搬已下文件。
        # 【唯独"搬不动"的要冻结】已归档的行不在 qB、relocate 移不动它们（R21 补，
        # 番剧侧 bind_anime_bgm 早就是这个写法，剧场版这一半一直是漏的）。
        # 【冻结这件事要能传出去】(R22) 有"盘上有文件但已归档"的版本时，keep_path 会把
        # display_name / jp_name / quarter 全冻住 —— 冻结本身是对的（已归档的行不在 qB，
        # relocate 移不动它们，改名就是制造程序侧补救不了的散目录），
        # 但整条链没有任何一处把这件事告诉用户：页面无条件弹绿色的『已绑定并识别 ✓』，
        # 而紧接着的 relocate 因为路径压根没变直接 return，连"有 N 个版本已归档"那句都到不了。
        # 用户明明是来纠正认错的片名的，绑定报成功，片名却还是错的。
        _frozen = engine.has_unmovable_files(s, MovieTorrent, MovieTorrent.movie_id, movie_id)
        if report is not None:
            report["frozen"] = _frozen
        engine.apply_bgm_meta(m, info, keep_path=_frozen)
        s.add(m)
        s.commit()
        for other in list(s.exec(select(Movie).where(
                Movie.bangumi_id == bgm_id, Movie.id != m.id))):
            _merge_movie(s, other.id, m.id)
        # 【合并会把"已忽略"传染过来，手动绑定要撤销它】_merge_movie 的规则是"两方任一被忽略则仍忽略"
        # ——那对后台的身份归并是对的（用户忽略过的片不该因为被合并而复活）。
        # 但【手动点绑定】是一个明确的"我要这部片"的动作：继承过来之后，UI 上弹一句绿色的
        # "已绑定并识别 ✓"，片却从『列表』和『待识别』同时消失，只能去『已忽略』tab 找。
        # TV 侧同款操作早就在合并后重读 keeper 并复位（见 core/anime.py 的 bind_anime_bgm）。
        m2 = s.get(Movie, movie_id)
        if m2 is not None and m2.rejected:
            m2.rejected = False
            s.add(m2)
            s.commit()
    return True


def _movie_folder(m, t=None):
    """剧场版下载文件夹名——download_movie_torrent 与 movie_save_path 统一口径（B3）：
    bgm 日文/中文名 → 种子原始标题 → m.title → 'movie'。缺 bgm 元数据时两处都回退到同一名字。"""
    return (((m.jp_name or m.display_name) if m else "")
            or (t.raw_title if t else "") or (m.title if m else "") or "movie")


def set_quarter(movie_id: int, quarter: str) -> bool:
    """手动改某部剧场版的归档年份（内部键如 26A；bgm 判错时的最终人工纠错）。

    与番剧侧 `anime.set_quarter` 【同一套校验、同一个返回口径】——两条线的对称实现，
    改行为要两边一起改。成功返回 True，改后由调用方触发 relocate 移动已下文件。

    【为什么剧场版侧此前没有这个入口】(E-33，2026-09-01 拍板)
    番剧侧一直有 `set_quarter` + 详情页的『编辑季度』，剧场版侧一个都没有——
    而剧场版的归档年份【更容易算错】：`quarter_of` 带着番剧的规则「12 月归次年冬季」
    （对季播番是对的：12 月开播实际跨 1–3 月播完），而剧场版是一次性上映，
    真库 70 部里有 5 部因此被归到了次年。在这个入口存在之前，唯一的纠正办法是手改数据库。

    【存的仍是季度键不是年份】列名与类型都不动（改列要迁移），而剧场版实际只用其中的年份——
    页面上那一栏就叫『年份』、归档目录走 MOVIE_QUARTER_FMT（默认 {yyyy}）。
    所以 UI 收的是年份，由调用方拼成 `{yy}A` 交给这里；这里仍按番剧的格式校验，
    免得两条线的键格式分家。
    """
    q = (quarter or "").strip().upper()
    if not re.fullmatch(r"\d{2}[A-D]", q):
        return False
    with get_session() as s:
        m = s.get(Movie, movie_id)
        if m is None:
            return False
        m.quarter = q
        s.add(m)
        s.commit()
    return True


def movie_save_path(movie_id: int) -> str | None:
    """该剧场版当前的归档目录（build_save_path 结果：[子目录]/[季度]/片名）；算不出返回 None。

    与 download_movie_torrent 【同口径】：都走 _movie_folder(m, 最新种子行)。缺 bgm 元数据时都回退种子原始标题，
    故显示/relocate 目标与实际落地一致（B3）。剧场版不建 Season 子目录。
    """
    with get_session() as s:
        m = s.get(Movie, movie_id)
        if m is None:
            return None
        t = s.exec(select(MovieTorrent).where(MovieTorrent.movie_id == movie_id)
                   .order_by(MovieTorrent.created_at.desc())).first()   # 最新种子行，缺元数据时回退，同下载口径
        quarter = (m.quarter or "unknown")
        folder = _movie_folder(m, t)
    return engine.build_save_path(quarter, folder, sub_dir=config.MOVIE_DOWN_PATH,
                                  quarter_fmt=config.MOVIE_QUARTER_FMT)


async def relocate_movie(movie_id: int, old_path: str | None = None) -> dict:
    """把该剧场版已下/在下/停滞的版本移到当前归档目录（改季度/重绑后调用；调用方应已落新 m.quarter/名）。
    实现见 engine.relocate（与番剧共用同一份）。"""
    return await engine.relocate(MovieTorrent, MovieTorrent.movie_id, movie_id,
                                 movie_save_path(movie_id), old_path, noun="个版本")


# ---------------- 下载 ----------------

def _set_status(mt_id: int, status: str) -> None:
    engine.set_torrent_status(MovieTorrent, mt_id, status)


def reset_downloading() -> None:
    """启动时把上次遗留的 downloading 复位为 pending。"""
    engine.reset_downloading(MovieTorrent)


async def download_movie_torrent(mt_id: int) -> bool:
    """交付一条种子。**整段包在 try/finally 里注销交付登记**（见 engine._delivering）。

    包装放在这一层而不是函数体内部：函数体从进锁到最后一次回写有一百多行、
    多条 return 与 raise，任何一条漏掉注销都会让那一行被永久当成"正在交付中" ——
    而 R24 之前它压根没有注销这回事，一次库抖动就把切库/迁移永久拒死。
    """
    try:
        return await _download_movie_torrent_inner(mt_id)
    finally:
        engine._delivering.discard(("MovieTorrent", int(mt_id)))


async def _download_movie_torrent_inner(mt_id: int) -> bool:
    """强制下某一版本到 qB（详情页逐条下用）。剧场版不建 Season 子目录。成功返回 True。"""
    if not config.QB_ENABLED:
        return False
    async with _dl_lock:
        with get_session() as s:
            t = s.get(MovieTorrent, mt_id)
            if t is None or (t.status in engine.TRACKED_STATUSES and t.archived_at is None):
                return False  # 已在下/已下 → 幂等短路，防并发重复交 qB；例外：已归档的可重新下（重新交回 qB）
            # 跨表【不】去重：剧场版/番剧各下到各自目录（用户要各归各、重复提交也接受）。qB 按 hash 物理去重、
            # 不会真下两遍；某侧删文件后另一侧由 sync 落 error——不再造 progress=1 的幽灵 pointer。
            m = s.get(Movie, t.movie_id)
            orig_status = t.status        # 供失败恢复：从终态(deleted/excluded)重下失败别降级成 error
            orig_archived = t.archived_at  # 同上：重下【已归档】的版本失败要把归档标记原样放回
            # 进锁时下面会清零这四个 qB 实时态；恢复原状态时要连它们一起放回（同番剧侧）
            orig_qb = (t.qb_progress, t.qb_state, t.qb_synced_at, t.qb_progress_at)
            t.status = "downloading"
            # 【登记"本协程真的在管这一行"】(R24) 落库的 downloading 只说明"某进程某一刻开始交付"，
            # 不说明"此刻真的有协程在管"。回写撞上库抖动时异常直接冒出去、行永久停在 downloading，
            # 而它既不被 sync 复查、又占着 HAVE_STATUSES、还把切库/迁移永久拒死。
            # 注销在本函数的外层包装的 finally 里（见 download_movie_torrent 那一层）。
            engine._delivering.add(("MovieTorrent", int(mt_id)))
            # 重新下：清归档标记 + 重置 qB 实时态，作『全新在下』重新跟踪、从新完成点重算归档倒计时（否则会被立刻再归档）
            t.archived_at = None
            t.qb_progress, t.qb_state, t.qb_synced_at, t.qb_progress_at = 0.0, "", None, None
            s.add(t)
            s.commit()
            url = t.download_url
            info_hash = t.info_hash
            quarter = (m.quarter if m else "") or "unknown"
            folder = _movie_folder(m, t)   # 与 movie_save_path 同口径（B3）

    # 与番剧侧对齐：失败不无条件写 error。终态(deleted/excluded)重下失败降级成 error 会丢掉
    # 『用户已处理』的语义；已归档重下失败降级则让那份仍在盘上的旧文件脱离 HAVE_STATUSES，
    # UI 上再没有入口能删它。CancelledError 以前写死 pending，同样会抹掉终态。
    # stalled 也保留原状态：半成品文件在盘、已留人工处理，抢救失败不该把这个标记抹掉（同番剧侧）
    _keep_orig = (orig_status in engine.MANUAL_TERMINAL_STATUSES or orig_status == "stalled"
                  or orig_archived is not None)
    fail_status = orig_status if _keep_orig else "error"
    defer_status = orig_status if _keep_orig else "pending"   # qB 连不上时的落点，同番剧侧

    # 剧场版【不做自动重试】：番剧那套退避挂在 flush_ready_downloads 上，而剧场版没有自动放行——
    # 下载一律由用户在页面上点。失败时人就在跟前，给个能看的 fail_reason 比排队重发有用。
    def _fail(status: str = "", reason: str = "") -> None:
        st = status or fail_status
        _set_status(mt_id, st)
        with get_session() as s2:
            t2 = s2.get(MovieTorrent, mt_id)
            if t2 is not None and t2.status == st:
                t2.fail_reason = reason[:300]
                if _keep_orig:      # 恢复原状态时把进锁清掉的 qB 实时态整组放回
                    t2.archived_at = orig_archived
                    (t2.qb_progress, t2.qb_state,
                     t2.qb_synced_at, t2.qb_progress_at) = orig_qb
                s2.add(t2)
                s2.commit()

    save_path = engine.build_save_path(quarter, folder, sub_dir=config.MOVIE_DOWN_PATH,
                                       quarter_fmt=config.MOVIE_QUARTER_FMT)
    if save_path is None:
        log.error("拒绝越界保存路径 - movie torrent %s", mt_id)
        _fail(reason=("未配置下载目录，请到设置页填『工作目录』"
                      if not (config.DOWN_PATH or config.MOVIE_DOWN_PATH)
                      else "拒绝越界保存路径（检查下载目录设置）"))
        return False
    try:
        data = await engine.fetch_torrent_bytes(url)
        # 分类不带后缀（可在设置页改名，见 config.QB_CATEGORY_MOVIE）；标签只放【年份】(不带季度)。
        # format_quarter 解析不出时原样回退（如 unknown）
        ok = await engine.add_to_qb(data, save_path, config.QB_CATEGORY_MOVIE,
                                    format_quarter(quarter, "{yyyy}"), info_hash=info_hash)
    except asyncio.CancelledError:
        _fail(reason="关停中断")
        raise
    except Exception as e:
        # 脱敏，理由同 core/anime.py 的交付异常那一处（fail_reason 会持久化并在详情页展示）
        log.error("剧场版下载失败 - %s", fetch.redact(e))
        _fail(reason=f"下载失败：{fetch.redact(e)}")
        return False
    if ok is None:             # qB 连不上：留在待下，别记 error
        log.warning("qB 连不上，本条留待重发 - movie torrent %s", mt_id)
        _fail(defer_status, reason="qB 连不上，稍后再点一次")
        return False
    if not ok:
        _fail(reason="qB 未接受（种子无效 / 保存路径不可写 / 磁盘满）")
        return False
    with get_session() as s:   # 记实际保存路径：改季度/重绑后据此移动或提醒旧位置
        t = s.get(MovieTorrent, mt_id)
        if t is not None:
            t.save_path = save_path
            t.fail_reason = ""      # 下成功了就把上次的失败原因抹掉
            s.add(t)
            s.commit()
    if config.QB_SYNC_STATUS:
        _set_status(mt_id, "sent")
        engine.qb_kick.set()   # 唤醒 qB 同步循环，立即开始跟这个新交付的种子
    else:
        engine.settle_sent(MovieTorrent, mt_id)  # 关跟踪：发送即已下，落定 qb_progress=1、脱离 in-flight
    log.info("已加入qB（剧场版）- torrent=%s", mt_id)
    # 走事件层：否则设置页那句"留空＝一条都不发"对剧场版是假的，
    # 而且批量下 30 个版本 = 30 条不可关、不限流的推送。
    # 事件键是 'movie' 而不是 'delivered'：见 services/notify.EVENTS 里的说明。
    # 图标由事件表给（🎬），这里不再自己拼。
    await notify_event("movie", folder)
    return True


async def delete_movie_torrent(mt_id: int) -> bool:
    """删除单条剧场版种子在 qB 里的文件（走 qB 接口），标记为 deleted（终态，恢复时不重下）。

    若同一 hash TV 管线还在用，则只脱手本行、不删 qB/文件，免得毁了对面。

    已归档(archived_at)的：种子早已从 qB 移除，qB 代删不到文件 → 只落 deleted 终态，
    硬盘文件留在 save_path 由用户自行清理（UI 在确认框里把该路径显示出来）。
    """
    with get_session() as s:
        t = s.get(MovieTorrent, mt_id)
        # 交付中(downloading)的行让路：qB 里还没这个 hash，删了会被交付协程静默写回（同番剧侧）
        if t is None or t.status not in engine.HAVE_STATUSES or t.status == "downloading":
            return False  # stalled 也允许删；downloading 是交付中的占位
        h = t.info_hash
        if t.archived_at is not None:      # 已归档：不在 qB，删不到文件，只改状态
            t.status = "deleted"
            s.add(t)
            s.commit()
            log.info("删除已归档条目（仅标记，文件留在 %s）- torrent=%s", t.save_path or "?", mt_id)
            return True
    if engine.hash_owned_elsewhere(h, AnimeTorrent):
        _set_status(mt_id, "deleted")  # TV 侧还持有同一种子 → 只脱手，不删文件
        return True
    if not await engine.qb.delete([h], delete_files=True):
        return False
    _set_status(mt_id, "deleted")   # 用户主动删除：终态，恢复时不会被重新下（区别于 skipped）
    log.info("删除文件（剧场版单条）- torrent=%s", mt_id)
    return True


async def sync_qb_status(manual: bool = False) -> int | None:
    """从 qB 同步剧场版种子实时态。"""
    return await engine.sync_qb_status(MovieTorrent, manual=manual)


def failed_rows() -> list[dict]:
    """status∈{error, stalled}（下载失败过 / 长期停滞）的版本，供 KPI『失败』点开查看、进详情页处理。
    对齐番剧的 anime.failed_rows：一次取种子 + 批量取片名，避免逐条查库。"""
    with get_session() as s:
        ts = list(s.exec(select(MovieTorrent)
                         .where(MovieTorrent.status.in_(["error", "stalled"]))
                         .order_by(MovieTorrent.created_at.desc())))
        ids = {t.movie_id for t in ts if t.movie_id}
        names = ({m.id: (m.display_name or m.title) for m in
                  s.exec(select(Movie).where(Movie.id.in_(ids)))} if ids else {})
    return [{"id": t.id, "movie_id": t.movie_id,
             "name": names.get(t.movie_id) or (t.raw_title or "?"),
             "raw": t.raw_title or ""} for t in ts]
