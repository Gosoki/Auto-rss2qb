"""TV 番剧与剧场版/OVA 共用的底层引擎：下载原语 / qB 客户端与状态同步 / bgm 元数据落库 / 路径季度。

anime.py(TV) 与 movies.py(剧场版) 都依赖这里；本模块不含任何 TV/movie 业务分支，纯共用，
两条线因此互不相干又不重复造轮子。
"""
import asyncio
import ipaddress
import logging
import ntpath
import os
import posixpath
import re
import socket
from datetime import datetime, timedelta, timezone

import httpx
from sqlmodel import func, or_, select

import config
from db import get_meta_session, get_session
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

# ==== 种子状态词表：全项目唯一真相（两条线 + 本模块 + pages 都从这里取，别再各处手抄）====
#
# 应用侧 status 全集。加第 9 个状态时，下面每个子集都要重新想一遍该不该含它——
# 这正是过去出事的地方：同一个集合被手抄十几份，加状态时漏改一处就静默失效（不报错、只是永远不匹配）。
ALL_STATUSES = ("pending", "downloading", "sent", "error",
                "skipped", "deleted", "excluded", "stalled")

# 三个判据是【逐层包含】的，写成累加形式让关系一目了然，也防止某一层被单独改歪：
#
# ① TRACKED：『已交付给 qB，且仍归轮询跟踪』——只有这两个态会被 sync 拉取/计入 qB 实时统计。
#    stalled 有意【不在】此列：它已被判停滞、脱离轮询、交人工处理。
#    ⚠️ download_*_torrent 的幂等短路用它：stalled 若混进来，详情页对停滞种子点『下载』会静默无反应
#      （那是人工抢救停滞种子的唯一入口）。
TRACKED_STATUSES = ("sent", "downloading")
# ② HAVE：『这一集盘上已经有一份』——在 ① 基础上 +stalled（停滞种子的半成品文件确实在盘上）。
#    用于：集去重(flush/plan/instant 去重闸/补下)、跨表持有判断、删除、搬迁、详情页 covered。
#    【不含 deleted】——用户删的那条不会自己回来（它自身是终态，flush/plan 只挑 pending/error 永不选它），
#    但同集来了新 hash 照常自动下（该集此时不含本集合任一状态 → 不被挡）。
HAVE_STATUSES = TRACKED_STATUSES + ("stalled",)
# ③ HANDLED：『这一集处理过』——在 ② 基础上 +deleted。用于 restore / 换源兜底：
#    用户特意删过的集，其被去重压成 skipped 的兄弟不该被复活重下。
HANDLED_STATUSES = HAVE_STATUSES + ("deleted",)

# 『用户手工置的终态』：不是流程跑出来的，是人明确表达过"不要这条"。两处判据共用：
#   · force 重下失败时恢复原态（core.anime 的 fail_status）——落成 error 会让该集不再含任何
#     HANDLED 状态，进而被换源兜底复活 + flush 自动重下，等于把用户的删除决定悄悄撤销；
#   · UI 显示时优先于 qB 残留态（pages.layout.qb_live_text）——删除/排除只改 status 不清 qb_state，
#     不先拦住就会把已删的显示成『已归档』或『已完成 100%』。
# 加第 9 个人工终态时，这两处会同时跟着走，不必再各改一遍。
MANUAL_TERMINAL_STATUSES = ("deleted", "excluded")

# 与上面三者互斥的另一维：『还没落盘、仍在待下队列』——可被下载选中，也可改集号/排除。
# 不叫 RETRYABLE：failed_rows 用的 ("error","stalled") 才是直觉上的"可重试"，重名会诱使后人往这里加 stalled，
# 而 stalled 一旦进来就会被自动重下/换源（正是 ② 要挡住的）。
DOWNLOADABLE_STATUSES = ("pending", "error")

# 写回 Anime/Movie 的 bgm 字段；多数两者同名，个别（duration=片长）仅剧场版有，靠下方 hasattr 跳过番剧
_BGM_FIELDS = ("bangumi_id", "display_name", "jp_name", "air_date", "air_weekday",
               "total_episodes", "platform", "cover_url", "rating", "summary",
               "author", "director", "music", "cast", "duration")
# _BGM_FIELDS 里【决定归档目录名】的那几个：有已下文件时不许被重识别改写（见 apply_bgm_meta）。
# 加新的路径来源字段时记得同步这里。
_PATH_FIELDS = ("display_name", "jp_name")


# ---------------- 文件名 / 季度 ----------------

# 单段文件/目录名的字节上限：ext4/NTFS/APFS 都是 255 字节，留点余量给 qB 可能追加的后缀。
# 番名来自 RSS，长标题（尤其中文/日文，1 字 3 字节）很容易越界——越界会让该番【所有集】
# 在 qB 侧 ENAMETOOLONG 永久失败，且报错在 qB 那边、本工具只看到"加种子失败"，极难排查。
_NAME_MAX_BYTES = 200


def safe_name(name: str) -> str:
    """清洗成安全的单段文件夹名：去非法字符/控制符，挡掉 '.'/'..' 路径穿越，并按字节截断。"""
    cleaned = _ILLEGAL.sub("_", name or "").strip().strip(".").strip()
    if len(cleaned.encode("utf-8")) > _NAME_MAX_BYTES:
        # 按【字节】截断但不切碎多字节字符：逐字符累加，超了就停
        out, used = [], 0
        for ch in cleaned:
            w = len(ch.encode("utf-8"))
            if used + w > _NAME_MAX_BYTES:
                break
            out.append(ch)
            used += w
        cleaned = "".join(out).strip().strip(".").strip()
    return cleaned or "unknown"


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


_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _path_module(base: str):
    """按 base 的样子选路径规则：Windows 盘符(E:\\ / E:/)或 UNC(\\\\NAS\\share) → ntpath，其余 → posixpath。

    路径是【给 qB 主机用的】，而本程序可能跑在另一个系统上，所以不能用 os.path——
    在 macOS/Linux 上 os.sep 是 '/'，拼出来的 Windows 路径会是
    'E:\\Anime\\AutoRSS/26C/番名' 这种混合分隔符。

    混合分隔符本身不影响下载（已在真机 qB v5.2.3 上验过：它照收，并规范化成全反斜杠），
    但 qB 回报的 save_path 就与我们库里存的【逐字不同】了，而 relocate（改季度搬文件）
    正是拿这两个字符串比对的——不一致会让它误判该不该搬、以及把"旧文件在 X"提示指到
    一个字面上不存在的路径。所以要从源头生成与 qB 一致的分隔符。

    判据无歧义（盘符/UNC 前缀），不需要用户去设开关——开关多一个就多一处能设错的地方，
    而且填了 Windows 路径却忘了拨开关时，症状与现在完全一样、更难查。
    """
    b = (base or "").strip()
    return ntpath if (_WIN_DRIVE_RE.match(b) or b.startswith("\\\\")) else posixpath


def build_save_path(quarter: str, folder_name: str, season: int | None = None,
                    sub_dir: str = "", quarter_fmt: str | None = None) -> str | None:
    """下载保存路径：根/[季度目录]/名字[/Season N]。做包含校验，越界返回 None。

    据工作目录(config.DOWN_PATH)与该侧目录 sub_dir(ANIME_DOWN_PATH/MOVIE_DOWN_PATH) 定根：
      有工作目录：sub_dir 作『相对』路径拼其下（sub_dir 空=直接落工作目录，不额外分类）；
      无工作目录：sub_dir 即『绝对』路径（须非空；两者皆空=无处下载，返回 None）。
    quarter_fmt=季度/年份目录模板（None=用 config.QUARTER_FMT；留空 ''=不建季度/年份目录，直接放名字）。

    分隔符按 base 的样子走（见 _path_module）：Windows 路径用反斜杠、POSIX 用正斜杠。
    """
    work = config.DOWN_PATH
    pm = _path_module(work or sub_dir)
    if work:
        base = pm.join(work, sub_dir.lstrip("/\\")) if sub_dir else work
    else:
        base = sub_dir
    if not base:            # 工作目录与该侧都空——无处下载
        return None
    parts = [base]
    fmt = config.QUARTER_FMT if quarter_fmt is None else quarter_fmt
    if fmt:   # 模板非空才建季度/年份目录；留空=不分类（format_quarter 对空模板有回退，故此处先判空）
        parts.append(safe_name(format_quarter(quarter or "unknown", fmt)))
    parts.append(safe_name(folder_name))
    if season is not None and config.ANIME_SEASON_SUBFOLDER:
        parts.append(f"Season {int(season)}")
    save_path = pm.join(*parts)
    if pm is ntpath:
        # Windows 路径用【词法】规范化比对，而不是 realpath——后者会拿本机（macOS/Linux）
        # 文件系统去解析一个 Windows 路径，纯属空转。大小写按 Windows 语义忽略。
        #
        # 注意这道闸的实际覆盖面与 POSIX 分支【一样窄】：sub_dir 在上面已经被折进 base，
        # 两边一起 normpath 后自然相等，所以 sub_dir 里写 '..' 是拦不住的（POSIX 分支同理，
        # 这是审计记录的待裁项，与本次 Windows 支持无关）。真正被它守住的只有季度/番名两段，
        # 而那两段已先过 safe_name（剥掉 . 与路径分隔符）。留着是纵深防御，不是主要防线。
        base_n = ntpath.normpath(base).rstrip("\\").lower()
        real_n = ntpath.normpath(save_path).lower()
        if real_n != base_n and not real_n.startswith(base_n + "\\"):
            return None
        return save_path
    # POSIX 分支保持原样（含 realpath 解析软链）：本机就是 qB 主机时它是真在校验，别动。
    base_real = os.path.realpath(base)
    real = os.path.realpath(save_path)
    if real != base_real and not real.startswith(base_real + os.sep):
        return None
    return save_path


# ---------------- bgm 元数据落库 ----------------

def apply_bgm_meta(obj, info: dict | None, keep_path: bool = False) -> None:
    """把 enrich 结果写进 obj（Anime 或 Movie，bgm 字段同名）——只覆盖非空值。

    keep_path=True（已有已下文件时）冻结【所有决定归档路径的字段】：季度 + jp_name/display_name。
    路径由 (quarter, jp_name or display_name, season) 拼成（见 anime._anime_path_parts），
    这几个字段一变，新集就落到另一个目录、已下的分集留在旧目录，同一部番裂成两个文件夹，
    而 qB 那边没人去搬。此前只冻结了 quarter，名字是漏的——重识别走的是【全新 bgm 搜索】而非
    按 bangumi_id 取，命中同系列另一 cour/衍生作时 jp_name 会被整体改写（实测：猫と竜 → 猫と竜 ふたたび）。
    改名只留给显式『绑定 bgm』流程——那条路径本来就带 relocate，会把文件一起搬过去。
    注：季号 season 不在这里冻结，但 anime._apply_bgm 是从 display_name/jp_name 反推它的，
    名字冻住了季号自然也稳；kind 等专属字段由各线自理。
    """
    if not info:
        return
    for k in _BGM_FIELDS:
        if keep_path and k in _PATH_FIELDS and getattr(obj, k, None):
            continue                            # 已有值且要保路径 → 不覆盖
        v = info.get(k)
        if v is not None and hasattr(obj, k):   # hasattr 跳过一方没有的列（番剧无 duration）
            setattr(obj, k, v)
    if info.get("quarter") and not (keep_path and obj.quarter):
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


# release_time 存的是 naive datetime，但【基准随来源站不同】，入库时没归一：
#   · nyaa  ：pubDate 带 -0000，feedparser 归一后是【真 UTC】
#   · mikan ：pubDate 不带时区（实为北京时间 UTC+8），feedparser 按 GMT 解释 → 存的是北京墙钟
# 存量数据用的都是旧基准，所以【只在显示期换算、不改库】——改入库那头会让新旧数据混在同一列、更难看。
# 用真实时区做 astimezone() 而不是写死小时差：本机换个时区（或夏令时地区）也照样对。
_SITE_TZ = {"nyaa": timezone.utc, "mikan": timezone(timedelta(hours=8))}


def torrent_time(t) -> str:
    """种子入库/发布时间的统一短显示：优先放送时间，退回创建时间，截到分钟。

    release_time 按来源站的时区换算到本地再显示。不换算的话 nyaa 那批【日期会整天错】——
    本机 JST 时，次日凌晨 00:30 上传的深夜番会显示成前一天 15:30，用户据此判断"这集什么时候
    放出来的"直接错一天；同一集跨源并列时两条时间差出近两天；『新入库』表按 created_at 排序
    却显示 release_time，时间列出现小时级倒挂、看起来像表坏了。
    回退用的 created_at 本来就是 datetime.now() 的本地时间，不换算。
    注意 release_time 不进任何下载/门禁判定（开始使用日比的是 bgm air_date），这纯粹是显示层。
    """
    if t.release_time is not None:
        tz = _SITE_TZ.get(getattr(t, "site", "") or "")
        if tz is not None:
            return str(t.release_time.replace(tzinfo=tz).astimezone().replace(tzinfo=None))[:16]
        return str(t.release_time)[:16]
    return str(t.created_at)[:16]


def set_torrent_status(model_cls, tid: int, status: str) -> None:
    """把某条种子（AnimeTorrent/MovieTorrent 任一）的状态置为 status。"""
    with get_session() as s:
        t = s.get(model_cls, tid)
        if t is not None:
            t.status = status
            s.add(t)
            s.commit()


def settle_sent(model_cls, tid: int) -> None:
    """把交付成功的种子直接落定为『已下完』(status=sent, qb_progress=1，脱离 in-flight)。
    关状态跟踪(QB_SYNC_STATUS=off)时用：发送即已下、不轮询 qB——若不落定 qb_progress，它会永久满足
    _inflight_where(progress<1 且 state 空未落定)，永远挂在『正在下载』区、且 has_inflight 恒真。"""
    with get_session() as s:
        t = s.get(model_cls, tid)
        if t is not None:
            t.status, t.qb_progress, t.qb_state, t.qb_synced_at = "sent", 1.0, "", datetime.now()
            s.add(t)
            s.commit()


def settle_inflight_off() -> int:
    """关闭 qB 状态跟踪(QB_SYNC_STATUS→off)或发送(QB_ENABLED→off)时，一次性把当前所有『在下的』种子
    落定为已下完(status=sent、qb_progress=1、脱离 in-flight)。返回落定数。

    off 之后 sync 内层循环不再运行(见 worker.run_qb_sync 的 while 条件)，这批『on 模式交付、进度未满』的行
    再无路径推进 → 会永久满足 _inflight_where、恒挂『正在下载』区、has_inflight 恒真。此处一次性落定，语义与
    settle_sent/『off=发送即已下』一致。settle_sent 只对【新交付】单条生效，故切换时刻的旧行须靠这里兜。"""
    n = 0
    now = datetime.now()
    with get_session() as s:
        for model_cls in (AnimeTorrent, MovieTorrent):
            for t in s.exec(select(model_cls).where(*_inflight_where(model_cls))):
                t.status, t.qb_progress, t.qb_state, t.qb_synced_at = "sent", 1.0, "", now
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


# 归档每轮处理上限 + 连续删除失败熔断阈值。归档幂等，分轮做不丢东西。
_ARCHIVE_BATCH = 200
_ARCHIVE_MAX_FAILS = 5


async def archive_old_completed() -> int:
    """完成归档：把『下载完成已超过 QB_ARCHIVE_AFTER_DAYS 天』的种子从 qB 移除【只删种子、留文件】，
    盖 archived_at、清 qb_state（不再跟踪）。status 保持 sent 不变——归档的仍是已下的一集，去重/统计
    /不重下等一切沿用 sent 语义，只是从 qB 列表清出、UI 显示『已归档』。

    完成时间以 qb_synced_at 为准：种子完成即脱离轮询、该时间冻结在完成点。跨表同 hash 安全：只删种子不删文件，
    另一侧下轮各自归档（qb.delete 对已不在的 hash 幂等）。qB 连不上/删失败则本轮跳过、下轮再来。返回归档数。"""
    days = config.QB_ARCHIVE_AFTER_DAYS
    if days <= 0 or not config.QB_ENABLED:
        return 0
    cutoff = datetime.now() - timedelta(days=days)
    n = 0
    fails = 0            # 连续删除失败数：连挂 _ARCHIVE_MAX_FAILS 次就整轮收手（见下）
    for model_cls in (AnimeTorrent, MovieTorrent):
        with get_session() as s:
            rows = [(t.id, t.info_hash) for t in s.exec(select(model_cls).where(
                model_cls.status == "sent",
                model_cls.qb_progress >= 1.0,
                model_cls.archived_at.is_(None),
                model_cls.qb_synced_at.is_not(None),
                model_cls.qb_synced_at < cutoff,
            ).limit(_ARCHIVE_BATCH)) if t.info_hash]   # 每轮封顶：归档幂等，剩下的下轮接着来
        for tid, h in rows:
            if not await qb.delete([h], delete_files=False):  # 只删种子、保留硬盘文件
                # 【连续失败熔断】qB 挂了的时候，_ensure 登录失败不缓存 client，于是每一条都会
                # 重新登录、重新建 AsyncClient、各刷一行错误日志。上千条待归档时一次唤醒就能刷出
                # 上万行，把 /logs 的 200 条环形缓冲和 2MB×5 的日志文件全冲掉——用户排查
                # 『qB 为何连不上』时反而什么线索都看不到。连挂几次就认定 qB 不可用、本轮收手，
                # 这也才对得上本函数 docstring 承诺的『qB 连不上则本轮跳过、下轮再来』。
                fails += 1
                if fails >= _ARCHIVE_MAX_FAILS:
                    log.warning("完成归档：连续 %d 次删除失败（qB 不可用？），本轮跳过，下轮再试", fails)
                    return n
                continue
            fails = 0    # 成功一次就清零，只熔断【连续】失败
            now = datetime.now()
            with get_session() as s:
                t = s.get(model_cls, tid)
                if t is not None and t.status == "sent" and t.archived_at is None:
                    t.archived_at, t.qb_state = now, ""    # 标已归档 + 清实时态（脱离 qB 跟踪与做种统计）
                    s.add(t)
                    s.commit()
                    n += 1
    if n:
        log.info("完成归档：%d 个完成超 %d 天的种子已从 qB 移除(留文件)、标已归档", n, days)
    return n


def pick_best(torrents, pref=None):
    """从候选种子里挑一份：钉了首选源就优先它（没有才退回全部），再按（优先级降序, 入库时间升序）取第一。

    调用方保证 torrents 非空（TV 选集 / 剧场版审批下载都先筛过 pending）。
    """
    cands = torrents
    if pref:
        cands = [t for t in torrents if pref == (t.source or "")] or torrents
    return sorted(cands, key=lambda t: (-(t.priority or 0), t.created_at))[0]


def hash_owned_elsewhere(info_hash: str, other_model) -> bool:
    """该 info_hash 在另一张表里是否仍被持有(downloading/sent)。

    TV 与剧场版两条独立管线偶有同一物理种子（同 hash，如某剧场版也被 ANi 按集发）。删文件前查一下：
    对面还在用就别 qB-delete(deleteFiles) 把共享的种子/硬盘文件一起端了，只在本表脱手即可。
    持有判据须与『可删状态』(sent/downloading/stalled) 对齐：stalled 的行文件仍在盘、仍指向该 hash，
    若不算持有会被本侧 deleteFiles 误删共享文件、留下永久悬空指针（deleted 是文件已删的终态，不计入）。
    """
    with get_session() as s:
        return s.exec(select(other_model).where(
            other_model.info_hash == info_hash,
            other_model.status.in_(HAVE_STATUSES))).first() is not None


def exclude_torrent(model_cls, tid: int) -> bool:
    """把一条【还没落盘】的种子置终态 excluded：不删文件、不碰 qB，只改状态。

    效果：不再出现在待下队列、自动放行/补下永不再挑、RSS 再遇到同 hash 也不重收；
    可用 unexclude_torrent 撤销。只动 DOWNLOADABLE 的行（已下/在下的请走删除，不是排除）。
    两条线逻辑完全一致，故实现放这里，anime/movies 各留一层同名薄包装供 pages 调用。
    """
    with get_session() as s:
        t = s.get(model_cls, tid)
        if t is None or t.status not in DOWNLOADABLE_STATUSES:
            return False
        t.status = "excluded"
        s.add(t)
        s.commit()
    return True


def unexclude_torrent(model_cls, tid: int) -> bool:
    """取消排除：把 excluded 的种子放回 pending，重新参与挑选。返回是否放回了。"""
    with get_session() as s:
        t = s.get(model_cls, tid)
        if t is None or t.status != "excluded":
            return False
        t.status = "pending"
        s.add(t)
        s.commit()
    return True


async def relocate(model_cls, owner_col, owner_id: int, new_path: str | None,
                   old_path: str | None = None, noun: str = "集") -> dict:
    """把某番/某片『盘上有文件』的种子移到新的归档目录（改季度/重绑后调用，调用方须已落新季度/名）。

    两条线逻辑完全一致（此前是同一段 67 行抄两份），故实现放这里；差异只有名词与错误文案，走参数。
    · qB 跟踪该种子 → setLocation 原地搬 + 更新 save_path
    · qB 关 / 连不上 / 不认识该 hash(remove-on-complete) → 清状态待重下到新目录
    · setLocation 报 403/409(新目录不可写) → 只报告、不动状态
    返回 {new_path, old_path, moved, redownload, untracked, failed, stalled_kept, delivering, fail_code?, error?}。
    """
    rep = {"new_path": new_path, "old_path": old_path, "moved": 0,
           "redownload": 0, "untracked": 0, "failed": 0,
           "stalled_kept": 0,   # 停滞行不降级也不重下，文件留在旧目录，需提示用户
           "delivering": 0}     # 交付中的占位行：本次不动它，交付完仍会落旧目录，需提示用户再点一次
    if new_path is None:
        rep["error"] = f"算不出新路径（越界或无{'番' if noun == '集' else '片'}）"
        return rep
    with get_session() as s:
        rows = s.exec(select(model_cls).where(
            owner_col == owner_id,
            model_cls.status.in_(HAVE_STATUSES),   # 含 stalled：半成品也在盘上，同样要搬
            model_cls.archived_at.is_(None))).all()  # 已归档的不在 qB，setLocation 移不动、别误清成 pending 触发重下
        # 排除【交付中】的占位行——与 sync_qb_status 同口径。downloading 是"已置位、还没 add 进 qB"的占位：
        # 交付协程在锁外 fetch(最长 180s)+add，此刻 qB 里没有它，问 qB 必然查不到 → 会被下面当成
        # untracked 清成 pending，而交付协程返回时又无条件写回 sent + 旧路径，净效果是：
        # 文件落旧目录、库里记 sent 不会重下、报告却谎称"已清状态待重下"并让用户去删
        # ——那正是 qB 此刻在写的文件。这里不碰它，只在报告里单独计数提示用户稍后再点一次。
        pairs = [(t.id, t.info_hash) for t in rows if t.status != "downloading"]
        rep["delivering"] = sum(1 for t in rows if t.status == "downloading")
    if not pairs:
        return rep

    def _clear(ids) -> int:
        """搬不动时把行清成 pending，等重下到新目录。返回【实际改了几行】。

        只降级仍被 qB 跟踪的行(TRACKED)。stalled 有意保持原样——它是『等人工处理』的标记，
        降成 pending 会：① 让停滞集重回自动队列，flush 还可能挑中同集别的源＝对停滞集自动换源
        （状态词表明令禁止）；② 抹掉『⚠️停滞』提示；③ 详情页删除按钮门槛是 HAVE，
        变 pending 后按钮消失，旧目录的半成品成了 UI 删不掉的孤儿。
        返回真实改动数是为了让报告不说谎（说要重下 N 条，就得真有 N 条被清）。
        """
        changed = 0
        with get_session() as s:
            for tid in ids:
                t = s.get(model_cls, tid)
                if t is not None and t.status in TRACKED_STATUSES:
                    # 连 qB 实时态一起清：否则这行虽已是『待重下』，UI 仍按残留的 qb_state/进度
                    # 渲染成『已完成 100%』(qb_live_text 优先于 status)，用户看不出它需要重下。
                    t.status = "pending"
                    t.qb_state, t.qb_progress = "", 0.0
                    t.qb_synced_at, t.qb_progress_at = None, None
                    s.add(t)
                    changed += 1
            s.commit()
        return changed

    def _stalled_of(ids) -> int:
        with get_session() as s:
            return sum(1 for tid in ids
                       if (t := s.get(model_cls, tid)) is not None and t.status == "stalled")

    def _mark_moved(ids):
        with get_session() as s:
            for tid in ids:
                t = s.get(model_cls, tid)
                if t is not None:
                    t.save_path = new_path
                    s.add(t)
            s.commit()

    all_ids = [tid for tid, _ in pairs]
    if not config.QB_ENABLED:   # qB 关：只能清状态待重下 + 提醒
        rep["stalled_kept"] = _stalled_of(all_ids)
        rep["redownload"] = _clear(all_ids)
        return rep
    info = await qb.torrents_info([h for _, h in pairs])
    if info is None:            # qB 连不上：同上
        rep["stalled_kept"] = _stalled_of(all_ids)
        rep["redownload"] = _clear(all_ids)
        return rep
    tracked = [(tid, h) for tid, h in pairs if h in info]
    untracked = [tid for tid, h in pairs if h not in info]
    if untracked:               # remove-on-complete 等：qB 已不认识 → 清状态待重下
        rep["stalled_kept"] += _stalled_of(untracked)
        rep["untracked"] = rep["redownload"] = _clear(untracked)
    if tracked:
        code = await qb.set_location([h for _, h in tracked], new_path)
        if code == 200:
            _mark_moved([tid for tid, _ in tracked])
            rep["moved"] = len(tracked)
        elif code is None:      # 中途连不上：退回清状态待重下
            rep["stalled_kept"] += _stalled_of([tid for tid, _ in tracked])
            rep["redownload"] += _clear([tid for tid, _ in tracked])
        else:                   # 403/409：新目录不可写/建不了 → 只报告，不动状态
            rep["failed"] = len(tracked)
            rep["fail_code"] = code
    return rep


def qb_is_local() -> bool:
    """qB 是否与本工具同机（QB_URL 指向 loopback）——决定要不要在本地预建保存目录。
    远程 qB（LAN IP / 域名）在本地建目录既无意义（是另一台机器的文件系统）又会落一堆空目录，故只对 loopback 建。"""
    try:
        host = (httpx.URL(config.QB_URL).host or "").strip("[]")
    except Exception:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def add_to_qb(data: bytes, save_path: str, category: str, tags: str,
                    info_hash: str = "") -> bool:
    """把种子加入 qB。返回是否成功。

    qB 同机(loopback)时本地预建目录 + chmod（跨用户 qB 需要）；qB 远程时跳过——真正建目录的是 qB 自己
    （实测 qB add 会按 savepath 建目录），本地建只会在错的机器上落空目录。

    幂等兜底：qB 对【已存在的 hash】的 add 会返回失败(200 'Fails.')——跨表同 hash / 重复提交 / 重下一个
    qB 里仍在的种子都会撞上。此时若 info_hash 已在 qB，视作交付成功（物理种子确实在，标 error 反而误伤，
    并使各下载路径注释所称『重复提交也接受』成立）。仅当 qB 在线且确认 hash 存在才兜底；连不上不误判。"""
    if qb_is_local():
        try:
            os.makedirs(save_path, exist_ok=True)
            os.chmod(save_path, 0o777)
        except OSError:
            pass
    if await qb.add_torrent(data, save_path, category, tags):
        return True
    if info_hash:
        h = info_hash.lower()   # torrents_info 返回小写键；命中判定与日志统一归一（防上游传大写）
        info = await qb.torrents_info([h])   # None=连不上(真失败)；dict 里有该 hash=已在 qB
        if info and h in info:
            log.info("add 被 qB 拒但该 hash 已在 qB（重复提交/跨表同种）→ 视作已交付 - %s", h[:12])
            return True
    return False


# ---------------- qB 实时态（对 AnimeTorrent / MovieTorrent 通用） ----------------

# ==== qB 原始态词表：全项目唯一真相（分类 + 中文名都在这一张表里）====
#
# 以前分类集合在这里、中文名在 pages/layout.py 各记一份 → 两边漂移无人察觉：
# qB 5.x 的 moving / checkingResumeData 早就写进了 UI 中文表，engine 的集合却没收，
# 结果『已完成』长期少记、校验期快轮询提前退出（详见 AUDIT.md B2/B3）。
# 现在新增一个 qB 状态只需在这里加一行，分类与中文名同时到位，漏不掉。
#
# 分类标记（可组合，一个状态可同时属多类）：
#   D = 下载态（计入『下载中』统计）
#   S = 做种/已完成态（下载已完成；含 5.x 改名后的 stoppedUP=完成后暂停）
#   T = 短暂工作态（在动但速度可能为 0：取元数据/校验/分配/搬运）——按『在真下』算，
#       免得刚开始那几秒或校验/搬运期间被速度地板误判成慢而提前退出快轮询
#   X = 落定态（不再需要轮询跟踪）——做种(已完成) + 文件缺失(终态、不会再变)
#   W = qB【没在推进】它：排队等额度(queuedDL) / 被要求停下(pausedDL/stoppedDL)。
#       停滞计时对这些态不该走——它们不是"卡住"，是"还没轮到 / 你让它停的"。
#       漏了这条会出事：qB 的 max_active_downloads 一小（本项目部署是 3），批量补番时几十条
#       全在 queuedDL 排队，超过 QB_STALL_TIMEOUT_MIN(默认 24h) 就会被整批误标『停滞异常』，
#       从而脱离轮询、不自动换源、在 UI 上报一堆假告警。
# 不带任何标记 = 既非在下也非已完成（error、unknown）
# 『qB 本轮查不到它』的记号态。不是 qB 的词，故用下划线开头，永不会与真实态撞。
_QB_ABSENT = "_absent"

_QB_STATES: dict[str, tuple[str, str]] = {
    # ---- 下载中 ----
    "downloading":        ("D",  "下载中"),
    "forcedDL":           ("D",  "下载中"),
    "stalledDL":          ("D",  "等待下载"),      # 无源、0 速：算下载态但不算『在真下』
    "queuedDL":           ("DW", "排队下载"),    # qB 排队等额度，不是卡住
    "metaDL":             ("DT", "取元数据"),
    "forcedMetaDL":       ("DT", "取元数据"),
    "checkingDL":         ("DT", "校验中"),
    "allocating":         ("DT", "分配空间"),
    # ---- 在动，但不属下载态 ----
    "checkingResumeData": ("T",  "校验中"),        # qB 重启后校验续传数据
    "moving":             ("T",  "移动中"),        # 开 temp_path 时完成后搬运必经
    # ---- 下载已完成（做种 / 完成后暂停）----
    "uploading":          ("SX", "已完成"),
    "forcedUP":           ("SX", "已完成"),
    "stalledUP":          ("SX", "已完成"),
    "queuedUP":           ("SX", "已完成"),
    "checkingUP":         ("SX", "校验中"),
    "pausedUP":           ("SX", "已完成"),
    "stoppedUP":          ("SX", "已完成"),
    # ---- 落定但不是完成 ----
    "missingFiles":       ("X",  "文件缺失"),      # 文件没了：终态，不再轮询，也不算已完成
    # ---- 既非在下也非已完成 ----
    "pausedDL":           ("W",  "已暂停"),
    "stoppedDL":          ("W",  "已暂停"),
    "error":              ("",   "错误"),
    "unknown":            ("",   "未知"),
    # ---- 我们自造的过渡态（不是 qB 会返回的值）----
    # sync 本轮在 qB 里没查到这个在下的种子时先打这个记号、宽限一轮，下轮仍查不到才落 error。
    # 带 W：qB 都看不到它，停滞计时自然不该继续走。只活一轮——下轮 qB 认回来就被真实态覆写。
    _QB_ABSENT:           ("W",  "确认中"),
}


def _states_with(flag: str) -> set:
    return {s for s, (flags, _) in _QB_STATES.items() if flag in flags}


_QB_DOWNLOADING = _states_with("D")
_QB_SEEDING = _states_with("S")
_QB_SETTLED = _states_with("X")
_QB_TRANSIENT = _states_with("T")
_QB_NOT_ADVANCING = _states_with("W")   # qB 没在推进它（排队/暂停）——停滞计时跳过这些
# qB 原始态 → 中文（UI 只从这里取，不再自己维护一份）
QB_STATE_CN = {s: cn for s, (_, cn) in _QB_STATES.items()}
# 预算成 list 供 SQL in_/not_in 复用（集合恒定、只读；避免热路径每次调用重新 list()）
_QB_SETTLED_LIST = list(_QB_SETTLED)
_QB_TRANSIENT_LIST = list(_QB_TRANSIENT)


def qb_is_downloading(state: str) -> bool:
    return state in _QB_DOWNLOADING


def _inflight_where(model_cls):
    """『在下的种子』筛选条件（sync 查询与 has_inflight 共用，口径一致）：
    已交付(sent/downloading) 且 进度<100% 且 qB 态未落定(非做种/非文件缺失)。
    进度满/做种(已完成)/文件缺失 都算落定 → 不再轮询，qB 压力只随『当前在下数』走。"""
    return (
        model_cls.status.in_(TRACKED_STATUSES),
        model_cls.qb_progress < 1.0,
        func.coalesce(model_cls.qb_state, "").not_in(_QB_SETTLED_LIST),
    )


def inflight_count(model_cls) -> int:
    """『在下的』种子总数（口径同 _inflight_where）。列表页只取前 N 条展示，
    标题要显示真实总数——否则被 limit 截断时标题少报，还会和上方按 qb_summary 算的
    『下载中』对不上（实测过：标题写 50、同屏 chip 写 64）。"""
    with get_session() as s:
        return s.exec(select(func.count()).select_from(model_cls)
                      .where(*_inflight_where(model_cls))).one()


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
                        model_cls.qb_state.in_(_QB_TRANSIENT_LIST))).limit(1)).first():
                return True
    return False


def mark_done_by_hash(info_hash: str) -> bool:
    """把某 info_hash 的种子标记为『已下完』(qb_progress=1、脱离 in-flight)——供 qB『完成时回调』精确兜底。
    认我们表里 sent/downloading/stalled 的种子；非法 hash / 非我们的 / 已终态(deleted/skipped/excluded) 返回 False。
    含 stalled：停滞种子被 sync 脱离轮询后再不复查，若它其实爬完了，只能靠此回调恢复为已下完。"""
    h = (info_hash or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", h):
        return False
    with get_session() as s:
        for model_cls in (AnimeTorrent, MovieTorrent):
            t = s.exec(select(model_cls).where(model_cls.info_hash == h)).first()
            if t is None:
                continue
            if t.status not in HAVE_STATUSES:
                continue   # 这张表里是终态 → 跨表同 hash 可能另一表还在下/停滞，继续查下一张，别提前 return
            t.status, t.qb_progress, t.qb_state, t.qb_synced_at = "sent", 1.0, "", datetime.now()
            s.add(t)
            s.commit()
            log.info("qB 完成回调：标记已下完 - %s", h[:12])
            return True
    return False   # 不是我们的种子 → 忽略


def backfill_legacy_progress_once() -> None:
    """一次性迁移：本功能上线前 status='sent' 语义=已交付（历史行都早已下完），但 qb_progress 可能为 0/未满。
    新模型以 qb_progress>=1 判『已完成、停止监听』，故上线时把现存 sent 行的 qb_progress 补成 1.0，免得它们
    被误判成『在下』而永久滞留 in-flight、每活跃间隔空打一次 qB。用 Setting 标记，只跑一次（后续新交付照常跟踪）。

    【标记位必须走 meta 会话】Setting 属 META_TABLES，只存在于本地 SQLite；业务库切到 MySQL 后
    那边【永远】不会有 setting 表（create_all 不建它、transfer 也不搬它）。以前这里用 get_session()
    读 Setting，切 MySQL 后每次启动必抛 1146，而它在 main.py 里排在四个 create_task 之前——
    异常一抛，采集/qB同步/剧场版扫描/待识别重试【一个都起不来】，页面却照常 200，
    用户只会觉得"好几天没更新了"。标记位读写走 meta，业务行更新走 data，两段分开。
    """
    flag = "_QB_PROGRESS_BACKFILLED"
    n = 0
    with get_meta_session() as ms:            # 标记位：本地 SQLite
        if ms.get(Setting, flag) is not None:
            return
    with get_session() as s:                  # 业务行：当前业务库（可能是 MySQL）
        for model_cls in (AnimeTorrent, MovieTorrent):
            for t in s.exec(select(model_cls).where(
                    model_cls.status == "sent", model_cls.qb_progress < 1.0)):
                t.qb_progress = 1.0
                s.add(t)
                n += 1
        s.commit()
    with get_meta_session() as ms:
        ms.add(Setting(key=flag, value="1"))
        ms.commit()
    if n:
        log.info("一次性迁移：%d 条历史 sent 种子标记为已完成（qb_progress=1，脱离 in-flight）", n)


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
                # 跳过【交付中】的占位行：download_*_torrent 在锁内先置 downloading 提交，
                # 出锁后才 fetch(最长 180s)+add，此刻 qB 里根本还没有它。若把它当"在下的"去问 qB，
                # 第一轮 d is None 走宽限【并自己写上 qb_synced_at】，第二轮就凭这个自造的证据判成
                # error——而交付其实还在进行。行一落 error 就掉出 HAVE_STATUSES，集去重闸失效，
                # flush 会自动为同一集再下一份到同一目录（实测无需任何用户操作即可复现）。
                # 交付协程独占这条行，它成功/失败都会自己写回状态，这里不该插手。
                if t.info_hash and t.status != "downloading"]
    if not rows:
        return 0
    info = await qb.torrents_info([h for _, h, _ in rows])
    if info is None:
        return 0   # 只在『连不上/出错』(None) 本轮不动。空 dict {} 是『qB 在线但这批一个都不在』——
                   # 须落到下面逐行走 d is None 落定(全被删/移除时)，否则它们永久 in-flight、循环永不休眠。
    if not config.QB_SYNC_STATUS:
        return 0   # await 期间用户关了跟踪（这批已由 settle_inflight_off 落定）——别用陈旧 qB 数据把它们覆写回在下态
    now = datetime.now()
    updated = 0
    with get_session() as s:
        for tid, h, was_synced in rows:
            t = s.get(model_cls, tid)
            if t is None or t.status not in TRACKED_STATUSES:
                continue
            d = info.get(h)
            if d is None:
                # qB 查不到这个在下的种子——必须在有限轮内落定，否则它恒满足 in-flight、循环永不休眠。
                # 用【重读后】的实时进度判定（await 期间该行可能被完成回调 mark_done_by_hash/新交付推进到满）：
                # 若仍用 await 前的陈旧快照，会把刚被 /api/qb/done 回调标『已下完』的行覆写回 error、使回调形同虚设。
                if (t.qb_progress or 0.0) >= 0.999:  # 已满(含完成回调刚落定) → 下完被 qB 移除，落定已下
                    t.qb_progress, t.qb_state, t.qb_synced_at = 1.0, "", now
                elif not was_synced:        # 从未被 qB 确认(刚交付未登记?) → 给一轮宽限，下轮仍无则→error
                    t.qb_synced_at = now
                elif t.qb_state != _QB_ABSENT:
                    # 曾在下、这一轮从 qB 消失 —— 【先记号、宽限一轮，不立刻判死】。
                    # qB 重启时 WebUI 先绑好端口、resume-data 还在异步装载，此期间 torrents/info 会
                    # 合法返回 200 + 【不完整】列表（不是空 dict，挡不住上面那个 info is None 闸）。
                    # 单轮 miss 即写 error 会把一批在下种子误杀成永久假失败：error 掉出 TRACKED_STATUSES
                    # → sync 不再复查、flush 按设计不重试 error、mark_done_by_hash 只认 HAVE 也救不回；
                    # 同时该集失去 HAVE 状态，flush 的 have_eps 闸当场放行同集另一个 pending 源，
                    # 而原种子在 qB 里其实装载完毕照常下完 → 同一集两份落进同一目录。
                    # 用 qb_state 记号而不是比时间：轮询间隔在 30s~保底 120 分之间浮动，
                    # 任何基于 now-qb_synced_at 的阈值要么误判、要么（若每轮都刷新）永远进不了 error 分支，
                    # 那会让查不到的种子永久滞留 in-flight、循环永不休眠——正是本段原注释要防的事。
                    t.qb_state, t.qb_synced_at = _QB_ABSENT, now
                else:                       # 连续第二轮仍查不到 → 落定 error（可补/重下）。
                    # 注：慢速种子被降级停跟后、在休眠里下完又被 qB 删（remove-on-complete）也会走这里被标 error——
                    # 我们看不到它爬到 100%。要精确标『已下』就在 qB 配『完成回调』(/api/qb/done，可选，见设置页)。
                    t.status, t.qb_state, t.qb_synced_at = "error", "", now
                s.add(t)
                updated += 1
                continue
            # 新鲜度守卫：await 期间该行可能被完成回调(/api/qb/done→mark_done_by_hash)或新交付落定为已下完
            # (qb_progress=1、qb_state 清空)。此时 d 是 await 前发出的【陈旧】快照(progress<1、downloading)，
            # 若无条件覆写会把它回退到在下态、UI 从『已下完』倒退，甚至下轮 d is None 被误标 error。
            # 与 d is None 分支同款：已落定(进度满且态已清)的行不被陈旧快照回退。
            if (t.qb_progress or 0.0) >= 0.999 and not t.qb_state:
                t.qb_synced_at = now
                s.add(t)
                updated += 1
                continue
            state = d.get("state", "") or ""
            prev_progress = t.qb_progress or 0.0
            t.qb_state = state
            t.qb_progress = float(d.get("progress", 0) or 0)
            t.qb_dlspeed = int(d.get("dlspeed", 0) or 0)
            t.qb_size = int(d.get("size", 0) or 0)
            t.qb_synced_at = now
            if t.qb_progress > prev_progress or t.qb_progress_at is None:
                t.qb_progress_at = now      # 进度推进(或首见)→ 刷新『上次推进时间』，作停滞判定基准
            if state == "error":
                t.status = "error"          # qB 侧真错误 → 回传；missingFiles 有意不回传（只镜像显示）
            elif t.status == "downloading" and t.qb_progress >= 1.0:
                t.status = "sent"     # 兼容旧的 downloading 占位（正常已在交付时置 sent）
            elif state in _QB_NOT_ADVANCING:
                # qB 排队等额度 / 被要求停下：这段时间不算"卡住"，重置停滞计时基准。
                # 否则 max_active_downloads 一小、批量补番时整批会被误标停滞异常
                # （脱离轮询 + 不自动换源 + UI 一堆假告警）。
                t.qb_progress_at = now
            elif (config.QB_STALL_TIMEOUT_MIN > 0 and t.qb_progress < 1.0
                  and t.qb_progress_at is not None
                  and now - t.qb_progress_at > timedelta(minutes=config.QB_STALL_TIMEOUT_MIN)):
                # 已交付但进度长期(默认1天)零推进 → 标『停滞(异常)』：脱离 in-flight、【不自动换源】、供人工处理。
                # 只看进度是否推进：慢但在动的种子 qb_progress_at 持续刷新→不会误杀；真卡死(无源0速)才中招。
                t.status = "stalled"
                log.warning("种子停滞标为异常（%d 分钟进度无推进，%.1f%%）- %s",
                            config.QB_STALL_TIMEOUT_MIN, t.qb_progress * 100, h[:12])
            s.add(t)
            updated += 1
        s.commit()
    return updated


def qb_summary(model_cls) -> dict:
    """某表已交付种子的 qB 实时态聚合：跟踪数 / 下载中 / 已完成 / 下速 / 平均进度。
    completed = 进度已满(qb_progress>=1)的数——统一叫『已完成』而非『做种』，
    避免把下完停着的种子说成在上传做种（B6）；为何按进度而非 qB 态判，见下方实现处注释。

    SQL 侧按 qb_state 分组聚合，只回十几种 state 的汇总行，不把整表已下种子整行拉进内存
    （已下是常态终态、只增不减，随挂机可累积到几千上万条）。qb_state='' 即未被 qB 跟踪，排除。"""
    with get_session() as s:
        grp = s.exec(
            select(model_cls.qb_state, func.count(), func.sum(model_cls.qb_dlspeed),
                   func.sum(model_cls.qb_progress))
            .where(model_cls.status.in_(TRACKED_STATUSES), model_cls.qb_state != "")
            .group_by(model_cls.qb_state)).all()
        # 『已完成』按【进度满】判，不按 qB 态判：种子一到 100% 就脱离 in-flight、此后不再同步，
        # 其 qb_state 会永久冻结在最后一次同步到的值。开了 temp_path 时完成瞬间是 moving(搬运中)，
        # 若按做种态判就会把这些行永久算作『跟踪中但未完成』，仪表盘已完成数长期少记。进度是唯一可靠依据。
        # 唯一例外 missingFiles：进度虽记着 1.0，但文件已从盘上消失，不该报成『已完成』（只在跟踪数里体现）。
        completed = s.exec(select(func.count()).select_from(model_cls).where(
            model_cls.status.in_(TRACKED_STATUSES),
            model_cls.qb_state != "", model_cls.qb_state != "missingFiles",
            model_cls.qb_progress >= 1.0)).one()
    tracked = downloading = dlspeed = 0
    prog_sum = 0.0
    for state, cnt, speed, psum in grp:
        cnt = cnt or 0
        tracked += cnt
        prog_sum += float(psum or 0)
        if qb_is_downloading(state):
            downloading += cnt
            dlspeed += int(speed or 0)
    return {
        "tracked": tracked,
        "downloading": downloading,
        "completed": completed,
        "dlspeed": dlspeed,
        "avg_progress": (prog_sum / tracked) if tracked else 0.0,
    }
