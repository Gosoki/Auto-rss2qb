"""TV 番剧与剧场版/OVA 共用的底层引擎：下载原语 / qB 客户端与状态同步 / bgm 元数据落库 / 路径季度。

anime.py(TV) 与 movies.py(剧场版) 都依赖这里；本模块不含任何 TV/movie 业务分支，纯共用，
两条线因此互不相干又不重复造轮子。
"""
import asyncio
from contextlib import contextmanager
import ipaddress
import logging
import ntpath
import os
import posixpath
import re
from datetime import datetime, timedelta, timezone

import httpx
from sqlmodel import func, or_, select

import config
from db import get_meta_session, get_session
from db.models import AnimeTorrent, MovieTorrent, Setting
from core import ssrf
from services import fetch
from services.qbittorrent import QBittorrent
from sources.parse import format_quarter
from sources.parse import quarter_year as _quarter_year

log = logging.getLogger("autorss")

qb = QBittorrent()

# 有种子交付给 qB 时 set()，唤醒 qB 同步循环立即开始跟；平时循环停在这上面休眠（见 worker.run_qb_sync）
qb_kick = asyncio.Event()

# 【每张表一把同步锁】sync_qb_status 是"快照 rows → await 打 qB → 逐行写回"的读改写序列，本身不是原子的。
# 后台轮询协程与页面上的『立刻刷新』（pages/anime.py、pages/movies.py）打的是同一个函数，并发跑时
# 两轮会交错：第一轮刚给某行写下 _QB_ABSENT 记号（"宽限一轮"），第二轮立刻读到这个记号就判死成 error。
# 于是那句"宽限一轮"在墙钟上塌成了零秒——正是 sync_qb_status 里那段长注释拼命要防的事。
# 按 model_cls 分两把而不是一把全局锁：TV 与剧场版各查各表、互不相干，合成一把只会让两条线互相排队。
_sync_locks: dict[type, asyncio.Lock] = {}


def sync_busy(model_cls) -> bool:
    """该表的 qB 同步是否正有一轮在跑。页面『立刻刷新』据此早退，别把后台那轮的宽限窗口压掉。"""
    lk = _sync_locks.get(model_cls)
    return bool(lk and lk.locked())


# 【本进程此刻真的在交付哪些行】(R24)
# `status == "downloading"` 是**落库的**占位，它只说明"某个进程某一刻开始交付这一行"，
# 不说明"此刻真的有协程在管它"。两者分家的后果实测过：
# 交付成功后的那次回写撞上 OperationalError（MySQL 重启 / 连接被切 / MYSQL_READ_TIMEOUT
# 切断慢查询 / 锁等待），异常直接冒出函数，行就**永久**停在 downloading ——
#   · `_sync_qb_status` 显式跳过 downloading 行（"交付协程独占"）；
#   · `downloading ∈ HAVE_STATUSES` ⇒ 集去重认定该集已有一份，flush/补下永不再挑；
#   · `run_db_watch` 的恢复边沿调 `init_business_state(reset_leftovers=_startup_reset_pending)`，
#     而运行中掉线那一支该标志恒为 False（有意为之，防止打断真在途的交付）⇒ 不复位。
# 只有重启进程才清得掉。而 R22 把 downloading 收进 `maintenance_blockers()` 之后又多一条：
# 设置页的『切库』『迁移』从此**永久**被拒，提示还写着"等它跑完（最多几分钟）再来" ——
# 它永远不会跑完。
#
# 这个集合把两者分开：进锁时登记、`finally` 里注销。于是
#   · 在途闸只数"真的有协程在管"的那些（残骸不再挡住维护）；
#   · `sweep_stale_delivering()` 能安全地把残骸复位（不在集合里 ⇒ 没人会再写它）。
# 键带表名：两张表的整数主键各自独立，只用 id 会串。
_delivering: set = set()


@contextmanager
def delivering(model_cls, torrent_id: int):
    """声明"本协程正在交付这一行"。见 `_delivering` 处的说明。"""
    key = (model_cls.__name__, int(torrent_id))
    _delivering.add(key)
    try:
        yield
    finally:
        _delivering.discard(key)


def is_delivering(model_cls, torrent_id: int) -> bool:
    return (model_cls.__name__, int(torrent_id)) in _delivering


def sweep_stale_delivering() -> int:
    """把"落库是 downloading、但本进程并没有协程在管"的残骸复位成 pending。返回复位数。

    只有本进程知道自己在交付什么，所以这件事只能在本进程做，且必须靠 `_delivering` 判 ——
    单看状态列是分不出"在途"与"残骸"的，那正是这些行以前永远清不掉的原因。
    """
    from db.models import AnimeTorrent, MovieTorrent

    n = 0
    with get_session() as s:
        for model_cls in (AnimeTorrent, MovieTorrent):
            for t in s.exec(select(model_cls).where(model_cls.status == "downloading")):
                if is_delivering(model_cls, t.id):
                    continue
                t.status = "pending"
                s.add(t)
                n += 1
        if n:
            s.commit()
    if n:
        log.warning("复位 %d 条僵死的『交付中』占位（多半是交付途中库抖了一下，"
                    "回写没成功）——它们回到待下，下一轮 flush 会重发", n)
    return n


def maintenance_blockers() -> list[str]:
    """现在【不能】做整库维护（切库 / 迁移）的理由清单；空列表＝可以做。(R21)

    维护会把整个业务库换掉或清空重写，而下面这几件事**跨 await 持着业务库的整数主键**：
    交付协程在锁内先把状态置成 `downloading`、出锁后 `await` 取种(最长 180s)+加 qB，
    回来再按 `torrent_id` 回写。而 `db/transfer.py` 明确保留主键 ——
    两个库里 id=501 是**两条毫不相干的种子**。维护窗口横在这中间时，
    回写就落进了另一个库的另一行：那一集被静默标成"已交付"（∈HAVE_STATUSES，
    集去重从此永远挡着），而盘上什么都没有，全程零告警。

    `db.maintenance()` 那把闸挡得住维护【期间】的读写，挡不住"await 跨过整个维护窗口、
    维护结束之后才回写"这一种。所以维护开始【之前】必须先确认没有这类协程在半途 ——
    这就是本函数。判据全部复用现成的、本来就在维护的信号，不新造状态：
      · `status == "downloading"` 的行 ＝ 交付协程正在半途（进锁即置、成败都会自己写回）
      · `sync_busy(...)`           ＝ qB 同步有一轮在 await 中
      · 两把轮次锁被持有 ＝ 采集轮 / 剧场版扫描轮正在半途 —— **它们同样跨 await 持主键**：
        `process_item` 里 `anime_id = await _resolve_anime(item)`（enrich 预算 120 秒）
        出 await 之后新开会话按那个 id 写种子行。R21 把这条留给了调用方"记得拿锁"，
        而 `_migrate` 拿了、`_switch_backend` 一把都没拿 —— 标准的第①号形状（R22 实测复现：
        采集轮卡在 enrich 的 await 上时闸放行，切库之后种子带着旧库的主键写进了新库）。
        收进判据之后，调用方不必再记得任何事。

    【库连不上时返回空表，不抛】(R22) `switch_data_engine` 的 docstring 明写
    "切回本地 SQLite"是 fatal/停摆时的**唯一自救出口**（"至少要让连接层回到本地，
    好让用户看得到设置页"）。而本函数要对着 `db.engine` 发两条 COUNT ——
    正是那台连不上的 MySQL。第一版没接异常：MySQL 一挂，这个新加的预检就把自救出口堵死了
    （实测弹"切换失败"，engine 一步都没动）。
    库都连不上，本来也不可能有在途交付；真正的把关在 `db.maintenance(blocked_by=...)` 那一层。
    """
    from sqlalchemy.exc import DBAPIError, SQLAlchemyError

    from db.models import AnimeTorrent, MovieTorrent

    reasons = []
    try:
        with get_session() as s:
            for model_cls, what in ((AnimeTorrent, "番剧"), (MovieTorrent, "剧场版")):
                # 【只数"真的有协程在管"的】落库的 downloading 里可能混着残骸
                # （交付途中库抖了一下、回写没成功），而残骸永远不会消失 ——
                # 只按状态列数的话，一条残骸就把切库/迁移永久拒死。见 _delivering 处的说明。
                n = sum(1 for t in s.exec(select(model_cls.id).where(
                    model_cls.status == "downloading")) if is_delivering(model_cls, t))
                if n:
                    reasons.append(f"有 {n} 条{what}种子正在交付中（取种最长 180 秒）")
    except (SQLAlchemyError, DBAPIError) as e:
        log.info("在途预检读不了业务库（%s: %.80s）—— 视作没有在途交付，"
                 "别把『切回本地 SQLite』这条自救出口堵死", type(e).__name__, e)
        return []
    for model_cls, what in ((AnimeTorrent, "番剧"), (MovieTorrent, "剧场版")):
        if sync_busy(model_cls):
            reasons.append(f"{what}的 qB 状态同步正在跑")
    # 延迟导入：这两个模块都 import engine，模块级会成环
    from core import anime as _a
    from core import worker as _w
    # 【五条后台线，一条都不能漏】(R22) R21 只列了前两条（downloading 行 / qB 同步），
    # R22 补了采集轮与剧场版扫描轮，而**第五条（延迟重识别）又漏了** ——
    # 它单轮最多 50 部、每部 `await enrich.resolve()`（预算 120 秒，全项目最长的 await 之一），
    # 出 await 后按同一个整数主键写回。第①号形状在同一个函数上连中两次，
    # 所以这里按"有几条跨 await 持主键的线"逐条列全，新增一条就往这儿加一行。
    for lock, what in ((_w._poll_lock, "采集轮"),
                       (_w._scan_lock, "剧场版扫描轮"),
                       (_a._enrich_lock, "延迟重识别/批量刷新资料")):
        if lock.locked():
            reasons.append(f"{what}正在跑（它跨 await 持着业务库主键）")
    return reasons

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

# 【暂时性失败】的重试退避（分钟）。第 n 次失败后等 RETRY_BACKOFF_MIN[n] 分钟再发，用满就落 error 等人工。
# 只给两类用：取种失败（nyaa/Mikan 超时、502、DNS…）和关停中断（重启时正在交付的那几条）——
# 它们与种子本身无关，重发大概率就好了。其余一律不进队列，重试没有意义甚至有害：
#   · 越界保存路径 → 配置问题，重试多少次都一样
#   · qB 明确拒收   → 种子文件坏了 / 路径不可写，同上
#   · 在 qB 里消失  → 多半是你自己在 qB 里删的，自动重下＝违背意图（与 deleted『不重下』一个道理）
# 注意实际间隔会被采集轮询周期【量化】：flush 每轮才跑一次（ANIME_POLL_INTERVAL，默认 1200s），
# 所以 1 分钟这一档实质是"下一轮"。这是有意的——不为重试单开一条更快的循环。
RETRY_BACKOFF_MIN = (1, 5, 30, 180, 720, 1440)


def next_retry_at(retry_count: int):
    """已重试 retry_count 次之后，下一次重发的时刻；退避表用满返回 None（＝放弃，落 error）。"""
    if retry_count >= len(RETRY_BACKOFF_MIN):
        return None
    return datetime.now() + timedelta(minutes=RETRY_BACKOFF_MIN[retry_count])

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


# 【quarter_year 只此一份，实现在 sources/parse.py】那里才是季度键的定义处
# （_QUARTER_KEY_RE、_CENTURY_PIVOT、format_quarter 都在那）。本模块只是把它转出来，
# 让 `engine.quarter_year(...)` 这个既有调用形态继续可用（pages/movies.py 与两处 overview 在用）。
#
# 历史：这里曾经自己写着 `2000 + int(q[:2])`，docstring 还宣称「从季度取年份只此一份」——
# 而 format_quarter 的 {yyyy} 占位另写了一份 f"20{yy}"。两份共享同一个「两位年一律当 20xx」的错，
# 于是 '99D' 在排序、显示、归档三条路径上都给出 2099。
# 现在换成再导出而不是「薄包装函数」：包装函数留着，下一个人往里加两行逻辑就又分家了。
quarter_year = _quarter_year


def prev_quarter(q: str) -> str:
    """上一个季度键：26C→26B，26A→25D（A 是年内第一季）。解析不出回空串。"""
    m = _QUARTER_KEY_RE.fullmatch(q or "")
    if not m:
        return ""
    yy, letter = int(m.group(1)), m.group(2)
    if letter == "A":
        # % 100 是必须的：'00A'(2000冬) 的上一季是 '99D'(1999秋)，不取模会拼出 '-1D'——
        # 一个谁都解析不出的键，quarter_brief 的"上季"卡会当场空掉。
        return f"{(yy - 1) % 100:02d}D"
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

    keep_path=True（已有已下文件时）冻结决定归档路径的字段（季度 + jp_name/display_name），
    但**只冻结已经有值的那些**——空值照样会被填上。这是有意的：一律 `continue` 的话，
    bgm 第一次没识别出来的片会带着空 quarter/jp_name 永久钉死在 `.../unknown/<种子原名>/`，
    把"目录名不好看"换成"永远修不好"。代价是"空→有值"这一次仍会改路径，
    带 UI 的那几条链路（绑定 / 详情页重新识别）本来就跟着 relocate 会把文件搬过去；
    **无 UI 的后台链路要自己快照回写**（见 movies._upsert_movie）。
    路径由 (quarter, jp_name or display_name, season) 拼成（见 anime._anime_path_parts），
    这几个字段一变，新集就落到另一个目录、已下的分集留在旧目录，同一部番裂成两个文件夹，
    而 qB 那边没人去搬。此前只冻结了 quarter，名字是漏的——重识别走的是【全新 bgm 搜索】而非
    按 bangumi_id 取，命中同系列另一 cour/衍生作时 jp_name 会被整体改写（实测：猫と竜 → 猫と竜 ふたたび）。
    改名只留给显式『绑定 bgm』流程——那条路径本来就带 relocate，会把文件一起搬过去。
    注：季号 season 不在这里冻结。**这不等于它稳**——原注释说"名字冻住了季号自然也稳"，
    那只在冻的是【非空】名字时成立；anime.enrich_anime 的 freeze_empty_path 那条路径是
    "名字先被填上、_apply_bgm 从新名字反推季号、然后名字才被还原"，季号会漏出去，
    所以那边的快照必须自己带上 season（已带）。原文如下，供理解 _apply_bgm 的意图：
    anime._apply_bgm 是从 display_name/jp_name 反推它的，
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
    # 【两条线取不同的键】番剧用 quarter（12 月开播归次年冬季，因为季播番跨 1–3 月播完），
    # 剧场版用 movie_quarter（按上映那一年，因为它是一次性上映、归档目录只用年份）。
    # 判据用"这个对象有没有 mikan_type 这一列"——那是 Movie 独有的，比传参更不容易漏：
    # 新增调用点时不用记得多传一个 flag。理由见 sources.parse.movie_quarter_of（E-30）。
    qk = "movie_quarter" if hasattr(obj, "mikan_type") else "quarter"
    if info.get(qk) and not (keep_path and obj.quarter):
        obj.quarter = info[qk]


# ---------------- 下载原语（取种子 + 交 qB） ----------------

# SSRF 守卫已挪到 core/ssrf.py —— config.http_client_kwargs 要用它装默认钩子，
# 而 config 是最底层，不能反过来 import 本模块。取种这条路径用【每一跳都判】的严格口径：
# download_url 整个来自 RSS 正文，不存在"用户自填所以可信"。

async def fetch_torrent_bytes(url: str, strict: bool = True) -> bytes:
    """流式下载 .torrent，封顶 32MB + 整体 180s 超时（download_url 源自 RSS 可被投毒 + 跟随重定向）。

    httpx 的 timeout=60 只是每次读的超时、逐块重置，慢速 trickle 连接能让它无限挂起并堵死整个下载/
    采集循环；故再套一层 asyncio.timeout 对总传输时长封顶。取到返回 bytes；HTTP/超限/超时失败抛异常，
    由调用方回写 error。

    strict=True（默认，采集链路用）：**每一跳都判内网**，含首跳。
    download_url 整个来自 RSS 正文，没有"用户自填"这回事，所以首跳也不能信。

    strict=False（手动下载页的『.torrent 链接』用）：**首跳放行、重定向后的每一跳仍强制判**，
    与 docs/DECISIONS.md 的 **D-05** 同一口径。(E-21，2026-09-01 拍板)
    两处口径原本分家，只是因为本函数当初只服务 RSS 来源、后来被手动下载复用了 ——
    而手动那条的地址是【用户自己在输入框里打的】，"局域网自建镜像 / 内网私站取种"是正当用法，
    按严格口径一律拒掉等于把这个功能对内网用户关掉；而被攻陷源站的 302 仍然拦得住，
    因为守卫拦的从来就是"第三方替用户改写了目的地"那一半。
    """
    if not (url or "").lower().startswith(("http://", "https://")):
        raise ValueError(f"拒绝非 http(s) 下载地址（防 SSRF）：{(url or '')[:80]}")
    kwargs = config.http_client_kwargs(60, url=url)
    kwargs["event_hooks"] = {
        "request": [ssrf.block_internal_request if strict else ssrf.guard_redirect_request]}
    async with httpx.AsyncClient(**kwargs) as client:
        # 复用 services.fetch 的封顶实现，别再手写一份：那份已经处理了"上限只作用在解压后"
        # 这个坑（一个几百 KB 的 gzip 压缩体能在单个块里解出几十 MB，等发现超限内存早就上去了）。
        # 总超时仍是 180s（取种可能要走代理、跨境），比 feed 的 120s 宽。
        try:
            return await fetch.get_bytes(client, url, cap=_TORRENT_CAP, timeout=180)
        except fetch.TooLarge as e:
            raise ValueError(f"种子文件超过 {_TORRENT_CAP} 字节，疑似非法下载地址") from e


# release_time 存的是 naive datetime，但【基准随来源站不同】，入库时没归一：
#   · nyaa  ：pubDate 带 -0000，feedparser 归一后是【真 UTC】
#   · mikan ：pubDate 不带时区（实为北京时间 UTC+8），feedparser 按 GMT 解释 → 存的是北京墙钟
# 存量数据用的都是旧基准，所以【只在显示期换算、不改库】——改入库那头会让新旧数据混在同一列、更难看。
# 用真实时区做 astimezone() 而不是写死小时差：本机换个时区（或夏令时地区）也照样对。
#
# 【时区基准存在源自己身上，不在这里维护一张表】这里曾经是一张 {site: tz} 的字典，
# 而它是全项目唯一一处"新增一个源时漏改了【不会报错】、只是发布时间整天错"的地方。
# 现在每个源类自带 TZ 属性（见 sources/base.RssSource），这里按 site 去源里问。
def _site_tz(site: str):
    from sources import SOURCES
    cls = SOURCES.get(site or "")
    return getattr(cls, "TZ", None) if cls else None


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
        tz = _site_tz(getattr(t, "site", "") or "")
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
    _inflight_where(progress<1 且 state 空未落定)，永远挂在『正在下载』区、且 has_inflight 恒真。

    【qb_synced_at 留空】它是【完成归档】的倒计时基准，语义是"看到这条真的下完的时刻"。
    这条路径根本没看 qB，写 now() 就等于谎称"刚刚确认下完了"：等用户把跟踪重新打开，
    archive_old_completed 第一次跑就会把整个关闭窗口里交付的种子成批 qb.delete 出去——
    其中可能有还在下的，下载当场中断、盘上留半成品、UI 却显示『已归档』、集去重认定该集已有一份、
    永不补下。留 None 则它天然不满足归档的 `qb_synced_at is not null`，而 qB 完成回调
    (mark_done_by_hash) 写的【真实完成时刻】仍然有效、仍可归档。"""
    with get_session() as s:
        t = s.get(model_cls, tid)
        if t is not None:
            t.status, t.qb_progress, t.qb_state, t.qb_synced_at = "sent", 1.0, "", None
            s.add(t)
            s.commit()


def settle_inflight_off() -> int:
    """关闭 qB 状态跟踪(QB_SYNC_STATUS→off)或发送(QB_ENABLED→off)时，一次性把当前所有『在下的』种子
    落定为已下完(status=sent、qb_progress=1、脱离 in-flight)。返回落定数。

    off 之后 sync 内层循环不再运行(见 worker.run_qb_sync 的 while 条件)，这批『on 模式交付、进度未满』的行
    再无路径推进 → 会永久满足 _inflight_where、恒挂『正在下载』区、has_inflight 恒真。此处一次性落定，语义与
    settle_sent/『off=发送即已下』一致。settle_sent 只对【新交付】单条生效，故切换时刻的旧行须靠这里兜。"""
    n = 0
    with get_session() as s:
        for model_cls in (AnimeTorrent, MovieTorrent):
            for t in s.exec(select(model_cls).where(*_inflight_where(model_cls))):
                # qb_synced_at 留空，理由同 settle_sent：它是归档倒计时的基准，而这里并没有
                # 真的看到"下完"——写 now() 会让这批行在跟踪重新打开后被追溯归档、砍掉还在下的种子。
                t.status, t.qb_progress, t.qb_state, t.qb_synced_at = "sent", 1.0, "", None
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
    另一侧下轮各自归档（qb.delete 对已不在的 hash 幂等）。qB 连不上/删失败则本轮跳过、下轮再来。返回归档数。

    【关掉状态跟踪(QB_SYNC_STATUS=off)时整个不做】上面那句"qb_synced_at 冻结在完成点"只在开跟踪时成立：
    关跟踪时 settle_sent / settle_inflight_off 在【交付那一刻】就写死 sent + qb_progress=1 + qb_synced_at=now，
    此后没有任何路径会再更新它（sync 内层循环被开关挡住）。于是归档倒计时从交付开始算，而 qB 那边可能还在下——
    N 天后这里会把【没下完】的种子从 qB 移除：下载当场中断、盘上留半成品、库里仍是 sent、UI 显示『已归档』、
    集去重认定该集已有一份、永不补下。关跟踪时我们根本不知道有没有下完，也就没有归档的语义基础。"""
    days = config.QB_ARCHIVE_AFTER_DAYS
    if days <= 0 or not config.QB_ENABLED or not config.QB_SYNC_STATUS:
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
                # 【文件缺失的不归档】missingFiles 是"qB 找不到盘上的文件了"（盘掉了/被挪走/被删）。
                # 它的 qb_progress 停在最后一次同步的值，可能正好是 1.0，于是会被当成"完成很久了"
                # 归掉——而归档=从 qB 移除种子，恰恰把唯一的修复入口（在 qB 里恢复文件后重新校验）
                # 一起端掉了，UI 上那条醒目的告警也被改写成温和的『已归档』。
                # 同模块的 qb_summary 早就显式把它排除在"已完成"之外，这里补齐同一口径。
                func.coalesce(model_cls.qb_state, "").not_in(_QB_NEEDS_RECHECK_LIST),
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


def pick_order(torrents, pref=None, prefer_fresh: bool = False):
    """把候选种子排成『该先试哪一个』的顺序：钉了首选源就先只看它（没有才退回全部），
    再按（优先级降序, 入库时间升序）排。返回排好序的列表（可能为空，调用方自理）。

    prefer_fresh=True 时在最前面再压两个键：【没失败过的排在失败过的前面】。
    否则会出现这样的死结：某集优先级最高的那一份种子是坏的（源下架/磁链失效），
    每次挑都还是它、每次都失败，而同集另一个源的健康 pending 兄弟永远轮不到——
    该集就此永久停滞，用户按仪表盘指引点『补下』永远返回 0，详情页还把『将下载』标在坏的那条上。
    【三条路径现在都用它】补下 / 计划 / 后台 flush 同口径（D-08）。这里曾写着"后台 flush
    只从 pending 里挑、不受影响"——那句话在 D-08 之后就不成立了：flush 也开了 prefer_fresh，
    因为 pending 里确实有失败过的（暂时性失败留在 pending 上排队重试）。
    """
    cands = torrents
    if pref:
        cands = [t for t in torrents if pref == (t.source or "")] or torrents
    if prefer_fresh:
        # retry_count 用 getattr 兜底：算下载计划的路径喂进来的是列投影出来的 Row（见 _PLAN_COLS），
        # 上面没有这一列。取不到就当 0（等价于"没失败过"），排序自然退化成原来的两键。
        return sorted(cands, key=lambda t: (t.status == "error", (getattr(t, "retry_count", 0) or 0) > 0,
                                            -(t.priority or 0), t.created_at))
    return sorted(cands, key=lambda t: (-(t.priority or 0), t.created_at))


def pick_best(torrents, pref=None, prefer_fresh: bool = False):
    """pick_order 的取第一个。调用方保证 torrents 非空（TV 选集 / 剧场版审批下载都先筛过 pending）。"""
    return pick_order(torrents, pref, prefer_fresh)[0]


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


def existing_hashes(model_cls, hashes) -> set[str]:
    """这批 info_hash 里哪些库里已经有了。**一次 IN 查询**，供入库前批量预取。

    逐条查的代价并不小：真库上 569 个 hash 单会话逐条 252ms、一条 IN 只要 2.8ms（90×）。
    而入库路径的条目绝大多数是【上一轮就见过的】，批量预取等于把 N 次往返压成 1 次；
    切到远程 MySQL 时省的是每轮 0.4~2 秒的裸阻塞（建连接 + 往返 ×N）。

    【两条线共用一份】(R21) 原本只有番剧侧有（`core.anime.existing_hashes`，只给 poll_once 用），
    剧场版的 `_store_movie_torrents` 仍是逐条 `WHERE info_hash = ?`：
    真库 70 部 / 569 个版本重扫一遍 = 639 条 SQL、657ms 的同步阻塞，而它跑在事件循环上
    （页面、下载放行、qB 同步一起卡）。同一件事有两处、只做了一处 —— 第①号形状。
    """
    hs = [h for h in hashes if h]
    if not hs:
        return set()
    with get_session() as s:
        return set(s.exec(select(model_cls.info_hash).where(model_cls.info_hash.in_(hs))))


def has_unmovable_files(s, model_cls, owner_col, owner_id: int) -> bool:
    """该番/该片是否有【搬不动】的已下文件：盘上有文件(HAVE)但已归档(archived_at 非空)。

    判据必须与下面 `relocate` 的选行条件**互为反面**：relocate 明确排除
    `archived_at.is_not(None)`（已归档＝从 qB 移除只留文件，setLocation 移不动）。
    所以只要存在这种行，改名/改季度就会造成【程序侧再也补救不了】的散目录：
    新集落新目录、这批文件永久留在旧目录，UI 上没有任何按钮能把它们搬过去。此时宁可不改名。

    【放在 engine 而不是两条线各写一份】(R21) R21 之前只有 `core/anime.py` 有它，
    剧场版侧漏了 —— 而两条线走的是同一个 relocate、同一个 archive_old_completed，
    约束在两边都成立。补的时候第一版是在 movies 里又抄了一份，
    被 `tests/test_single_definition.py` 当场判红：抄出来的第二份，下次只会被改掉一半。
    收成参数化的一份之后，`relocate` 与它天然对齐（同一个文件、相邻两个函数）。

    s 收成参数：两个调用点都在自己的会话里做完读改写，不该为这一问再开一个会话。
    """
    return s.exec(select(model_cls).where(
        owner_col == owner_id,
        model_cls.status.in_(HAVE_STATUSES),
        model_cls.archived_at.is_not(None),
    )).first() is not None


async def relocate(model_cls, owner_col, owner_id: int, new_path: str | None,
                   old_path: str | None = None, noun: str = "集") -> dict:
    """把某番/某片『盘上有文件』的种子移到新的归档目录（改季度/重绑后调用，调用方须已落新季度/名）。

    两条线逻辑完全一致（此前是同一段 67 行抄两份），故实现放这里；差异只有名词与错误文案，走参数。
    · qB 跟踪该种子 → setLocation 原地搬 + 更新 save_path
    · qB 关(用户显式关掉发送) / 不认识该 hash(remove-on-complete) → 清状态待重下到新目录
    · 【qB 连不上】→ 一行都不动，只回 error 让用户稍后重试（与"qB 说没有"是两码事，见下方长注释）
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
        （状态词表明令禁止）；② 抹掉『停滞』提示；③ 详情页删除按钮门槛是 HAVE，
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
    if not config.QB_ENABLED:
        # 【qB 关着时一行都不动】原写法是 _clear 成 pending「待重下」，但那个前提不成立：
        # qB 关着时 download_anime_torrent / download_movie_torrent / manual 三条下载入口
        # 第一行就 return False，flush 与两个批量补下实测全部返回 0 ——
        # **没有任何路径能把它们下回来**。而 _clear 的代价是三重的：
        #   ① 页面照着 rep["redownload"] 提示"旧文件在 X 需你手动清理"，用户照做就删光了唯一一份；
        #   ② "哪些集已到手"的记录被永久抹掉；
        #   ③ 清成 pending 后掉出 HAVE_STATUSES，而两个删除按钮的门槛正是 HAVE ——
        #      那份旧文件连 UI 入口都没有了。
        # 与本函数另外两条分支同口径：已归档行"不在 qB，setLocation 移不动、别误清成 pending"，
        # qB 连不上"一行都不动"。qB 关着与那两种是同一类：我们动不了文件，那就什么都别改。
        rep["error"] = ("qB 未启用，无法代为移动文件：一行都没有改动，"
                        f"这些集的文件仍在旧目录 {rep.get('old_path') or '（原处）'}。"
                        "到设置页打开『发送种子到 qB』后再点一次即可。")
        return rep
    info = await qb.torrents_info([h for _, h in pairs])
    if info is None:
        # 【qB 连不上：一行都不动】——绝不能与上面"qB 关"或下面"qB 说不认识这个 hash"混为一谈。
        # 连不上时那些种子好端端地在 qB 里做种，若照样 _clear 成 pending：下一轮 flush 会全部重发，
        # qB 对已有 hash 回 409 → add_to_qb 的幂等兜底查到 hash 在 qB、判『已交付』并把 save_path
        # 写成【新目录】，可 qB 侧的保存路径从头到尾没变过，文件一个都没动。
        # 于是库/UI 声称文件在新目录、实际仍在旧目录，而移动报告还会提示『旧文件需你手动清理』——
        # 用户照做就把这部番唯一的一份文件删光了。
        # 与全项目对该信号的统一口径一致：sync_qb_status 连不上就本轮不动、flush 预检不过就整轮不放行。
        rep["error"] = "qB 连不上，未做任何改动。等 qB 恢复后重试即可（文件与状态都原样保留）"
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
        elif code is None:
            # 中途连不上：同样【一行都不动】（理由见上面 info is None 处）。
            # 这一批的 save_path 还指着旧目录、文件也确实在旧目录，保持原样才是自洽的。
            # 前面若已有 untracked 被清，那部分是"qB 确实不认识"，与本分支无关，照旧计入报告。
            _part = (f"（此前已有 {rep['untracked']} 集因 qB 确实不认识而清状态待重下）"
                     if rep.get("untracked") else "")
            rep["error"] = ("qB 中途连不上，这批文件未移动，状态原样保留。等 qB 恢复后重试" + _part)
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


def needs_relocate(rows, new_path: str, old_path) -> bool:
    """要不要弹"搬迁已下文件"的确认框。

    两条都算：
      · 这次操作把归档目录改了（new_path != old_path），或
      · 盘上还有文件不在当前归档目录里（rows_in_wrong_dir 非空）。

    【为什么单独抽成纯函数】两侧的页面代码里各写一遍时，守卫只能去 grep 源码字符串，
    而那种守卫【注释就能满足】：第 19 轮实测把番剧侧的闸整段回退回 R18 之前，
    只要保留上面那句提到 rows_in_wrong_dir 的注释，全套用例一条都不红。
    判据抽出来之后就能表驱动地测，页面侧只剩一行调用，回退它会当场变红。
    """
    if not new_path:
        return False
    return new_path != old_path or bool(rows_in_wrong_dir(rows, new_path))


def rows_in_wrong_dir(rows, new_path: str) -> list:
    """这些行里，【盘上文件实际所在的目录】与当前归档目录对不上的那些。

    rows 传 AnimeTorrent / MovieTorrent 列表（本函数只用 status / archived_at / save_path，
    两张表同名，所以两条线共用这一份）。

    【为什么需要它：搬迁闸问错了问题】番剧与剧场版两侧的 maybe_relocate_* 原本都写着
    `if new_path == old_path: return`，而四个调用点拿到的 old_path **也是**
    `*_save_path(id)` —— 同一个纯函数、同一条记录算出来的。于是这道闸问的是
    「我这次操作把记录改了没有」，而不是「盘上的文件跟当前归档目录对得上没有」。
    后果：搬迁只要失败或被拒绝一次，之后**再没有任何入口**能补搬 ——
    后续每一次绑定/重识别/改季度都会算出 old == new 而直接返回，
    而 engine.relocate 的提示里那个 old_path 还会指向一个根本没有文件的目录。
    真库实证：anime#96『落语朱音』10 集躺在 `26C\\AKANE On My Mind〜饅頭こわい`（旧的错绑名），
    记录早已改成 `26B\あかね噺`，界面上没有任何地方还会提出搬它。

    真相就记在行上的 save_path 里，直接问它。空 save_path 不算（老行/没交付过的行）。
    """
    return [t for t in rows
            if t.status in HAVE_STATUSES and not t.archived_at
            and (t.save_path or "") and t.save_path != new_path]


async def add_to_qb(data: bytes | None, save_path: str, category: str, tags: str,
                    info_hash: str = "", *, magnet: str = "") -> bool | None:
    """把种子加入 qB。True=已交付，False=qB 拒了这一条，【None=连不上 qB（暂时性，别当失败）】。

    传 data=种子字节，或传 magnet=磁力链（二选一，magnet 优先）。两种投递方式的【策略完全相同】，
    差别只在最后那一下调哪个 qB 接口，所以【共用这一个函数】——
    早先 core/manual.add_manual 自己直接调 qb.add_torrent/qb.add_url，于是下面两件事它一件都没有：

    qB 同机(loopback)时本地预建目录 + chmod（跨用户 qB 需要）；qB 远程时跳过——真正建目录的是 qB 自己
    （实测 qB add 会按 savepath 建目录），本地建只会在错的机器上落空目录。
    【手动下载最需要这一条】它的默认保存位置是『工作目录/Temp』，那是个谁也不会预先去建的目录。

    幂等兜底：qB 对【已存在的 hash】的 add 会返回失败(200 'Fails.')——跨表同 hash / 重复提交 / 重下一个
    qB 里仍在的种子都会撞上。此时若 info_hash 已在 qB，视作交付成功（物理种子确实在，标 error 反而误伤，
    并使各下载路径注释所称『重复提交也接受』成立）。仅当 qB 在线且确认 hash 存在才兜底；连不上不误判。
    【手动下载同样需要】同一条磁力点两次，第二次本该是"它已经在下了"，
    而绕过兜底时给用户的是红色的『qB 未接受（种子无效/路径不可写/重复）』。"""
    if qb_is_local():
        try:
            # 【chmod 只作用在【我们刚建出来的】那个目录上】它的用途是跨用户 qB 写得进去，
            # 而对象本该是 build_save_path 生成的、位于下载根之下的叶子目录。
            # 但手动下载的 save_path 是用户在输入框里自由填的（pages/manual.py），
            # 完全可能指向一个已经存在的媒体库目录——R17 让手动路径统一走本函数时，
            # 把 chmod 一起带了过去，于是一次手动下载会把那个已有目录改成全局可写。
            # exists 先探一次，就能把"新建的叶子目录"和"用户已有的目录"分开对待。
            fresh = not os.path.isdir(save_path)
            os.makedirs(save_path, exist_ok=True)
            if fresh:
                os.chmod(save_path, 0o777)
        except OSError:
            pass
    res = (await qb.add_url(magnet, save_path, category, tags) if magnet
           else await qb.add_torrent(data, save_path, category, tags))
    if res:
        return True
    if not info_hash:
        return None if res is None else False   # 无从核实：连不上就报连不上
    h = info_hash.lower()   # torrents_info 返回小写键；命中判定与日志统一归一（防上游传大写）
    info = await qb.torrents_info([h])   # None=连不上；dict 里有该 hash=已在 qB
    if info is None:
        # 连不上 qB（add 那一下可能是没连上，也可能是连上后被拒但这会儿断了）——
        # 一律按暂时性处理：宁可下轮重发一次（重复提交会被 409/兜底认成已交付），
        # 也不要把没问题的种子打成 error 等人工。
        return None
    if h in info:
        log.info("add 被 qB 拒但该 hash 已在 qB（重复提交/跨表同种）→ 视作已交付 - %s", h[:12])
        return True
    # 【幂等兜底只能把 False 升级成 True，绝不能把 None 降级成 False】与 11 行之上那句
    # "无从核实：连不上就报连不上"同款。res is None 的含义是【add 那一下根本没连上】
    # （两次 ReadTimeout：大种子入库校验 / 磁盘忙 / qB 正在 recheck 一大批）；
    # 而紧跟着的 torrents_info 是个便宜的 GET，秒回 200 很正常，此刻 qB 只是还没把该 hash 列出来。
    # 无条件 return False 的后果不是"少下一集"：调用方把 False 当成"这一条自己的毛病"，
    # 写 status=error + "qB 未接受"，而 error ∉ HAVE_STATUSES ⇒ 下一轮 flush 认为这一集还缺，
    # 把同集另一个源放行到【同一个 save_path】——而 qB 其实已经收下了第一份。
    # 同一集两份文件落进同一文件夹，正是 tests/test_qb_sync.py 开篇声明要守的那条故障链。
    return None if res is None else False


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
    # 【校验中不是落定】原来带 X（不再轮询）。可 qB 对"曾经下完的种子强制重新校验"报的正是它，
    # 而那恰恰是用户修复 missingFiles 的标准动作：把文件放回去 → 强制校验 → 点『立刻刷新』。
    # 带 X 时这一行同时掉出 in-flight 与补查名单，从此再没有任何路径问过 qB 它怎么样了。
    "checkingUP":         ("ST", "校验中"),
    "pausedUP":           ("SX", "已完成"),
    "stoppedUP":          ("SX", "已完成"),
    # ---- 落定但不是完成 ----
    # 带 W：qB 确实没在推进它（文件都找不到了），所以【停滞计时也不该走】——
    # 否则默认 24 小时后这一行会从精确的『文件缺失』变成含糊的『停滞』，既丢诊断信息，
    # 又因为 stalled ∉ TRACKED_STATUSES 而永久退出上面那条补查（用户在 qB 修好文件也再无出口）。
    "missingFiles":       ("XW", "文件缺失"),      # 文件没了：终态，不再轮询，也不算已完成
    # ---- 既非在下也非已完成 ----
    "pausedDL":           ("W",  "已暂停"),
    "stoppedDL":          ("W",  "已暂停"),
    "error":              ("",   "错误"),
    "unknown":            ("",   "未知"),
    # ---- 我们自造的过渡态（不是 qB 会返回的值）----
    # sync 本轮在 qB 里没查到这个在下的种子时先打这个记号，【记号 + 墙钟下限两个条件都满足】才落 error
    # （下限 max(600, QB_SYNC_INTERVAL*10) 秒；整批一起消失时用更长的那档，见 _sync_qb_status）。
    # 所以它会存活好几轮、至少 10 分钟，不是"只活一轮"。
    # 带 W：qB 都看不到它，停滞计时自然不该继续走。qB 下一轮认回来就被真实态覆写。
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
# 【"还不能当它已经完事"的那些态】——三处消费者共用一份，别再各比各的字面量。
#   · 补查名单：这些态要继续问 qB（否则被采样到中间态的行就此永久冻结）
#   · 归档闸：归档＝从 qB 移除种子，对这些态做等于端掉唯一的修复入口
#   · qb_summary 的"已完成"：进度可能记着 1.0，但盘上未必真有那份文件
# 以前三处都在比字面量 "missingFiles"，于是 missingFiles 行只要被采样到一次 checkingUP/
# moving/checkingResumeData，三道闸【同时】失效：既不再跟踪、又被归掉、还在仪表盘算成已完成。
_QB_NEEDS_RECHECK = {"missingFiles"} | _QB_TRANSIENT
_QB_NEEDS_RECHECK_LIST = list(_QB_NEEDS_RECHECK)
_QB_TRANSIENT_LIST = list(_QB_TRANSIENT)


def qb_is_downloading(state: str) -> bool:
    return state in _QB_DOWNLOADING


def _inflight_where(model_cls):
    """『在下的种子』筛选条件（sync 查询与 has_inflight 共用，口径一致）：
    已交付(sent/downloading) 且 进度<100% 且 qB 态未落定(非做种/非文件缺失)。
    进度满/做种(已完成)/文件缺失 都算落定 → 不再轮询，qB 压力只随『当前在下数』走。

    【别以为 ix_*_inflight 那个 partial index 在起作用】它建出来了，但运行时用不上：
    SQLAlchemy 把 status 列表发成绑定参数(?)，而 SQLite 要能【静态证明】查询条件蕴含索引谓词
    才会选 partial index，占位符做不到这个证明。实测（9672 行）：
        绑定参数 → SCAN animetorrent，0.79ms
        字面量   → SEARCH ... USING COVERING INDEX，0.02ms
    但整个 has_inflight() 也才 1.26ms，而它每 30 秒~2 小时才调一次，
    所以【有意保持现状】：把状态内联成字面量能快 40 倍，却要在 SQL 里拼字符串、
    给后人留一个看着像注入的写法，不值当。真到了几十万行再说。"""
    return (
        model_cls.status.in_(TRACKED_STATUSES),
        model_cls.qb_progress < 1.0,
        func.coalesce(model_cls.qb_state, "").not_in(_QB_SETTLED_LIST),
    )


def _recheck_where(model_cls, states=None):
    """『被 _inflight_where 排除掉、但仍要额外问一遍 qB』的行 —— 与 _inflight_where **严格互斥**。

    最后那个 or_ 就是 `NOT _inflight_where`（在 status 已经是 TRACKED 的前提下）：
    进度满了、或者 qB 态已落定。少了它，两条 WHERE 会重叠 ——
    `_QB_NEEDS_RECHECK = {missingFiles} | _QB_TRANSIENT`，而 _QB_TRANSIENT 的 7 个态
    （checkingDL / allocating / metaDL / forcedMetaDL / checkingResumeData / moving / checkingUP）
    【带 T 不带 X】，progress<1 时两边同时命中，同一行既进 rows 又进 recheck。
    `_sync_qb_status` 里那句 `if tid in recheck_ids: continue` 是按 tid 判的、
    认不出手上这一份来自哪一半，于是属于 rows 的那一份也被当成补查行跳过 ——
    这一行从此【永远落不了定】：恒满足 in-flight（同步循环永不休眠）、
    又恒在 HAVE_STATUSES 里（集去重认定这一集已有一份，而盘上什么都没有 → 该集永久漏投且零告警）。
    触发条件很日常：qB 重启让所有未完成种子经过 checkingResumeData/checkingDL，
    开着预分配时每个新种子经过 allocating，磁链交付经过 metaDL。（R21 修，用例钉在 test_qb_sync.py）

    【为什么不改成"给这 7 个态补上 X"】那能让集合互斥，却会把它们踢出 _inflight_where；
    而下面那条 recheck 查询带着 `if (rows or manual)` 的搭车条件 ——
    在下的只剩这一条时 rows 为空、recheck 压根不跑，那才是真正的死胡同
    （checkingUP 当初从 SX 改成 ST 正是为了躲开它）。
    """
    return (
        model_cls.status.in_(TRACKED_STATUSES),
        model_cls.qb_state.in_(list(states if states is not None else _QB_NEEDS_RECHECK)),
        or_(model_cls.qb_progress >= 1.0,
            func.coalesce(model_cls.qb_state, "").in_(_QB_SETTLED_LIST)),
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


def needs_qb_poll() -> bool:
    """还该不该问 qB —— 供 worker 决定继续轮询还是休眠。(R22)

    = 有在下的种子（`has_inflight()`）**或** 有停在过渡态、等着被补查的种子。

    【为什么不能只用 has_inflight()】R21 给 `_sync_qb_status` 加了"过渡态无条件补查"
    （`_QB_TRANSIENT` 的 7 个态是 qB 自己几秒内就走完的，不该等"有别的在下种子"才搭车）。
    可它唯一的后台调用方是 `run_qb_sync` 的内层 `while … and has_inflight()` ——
    而触发那条新分支的行恰恰是 `progress=1.0 + qb_state=moving/checkingUP` 这种，
    **正好让 `has_inflight()` 返回 False**（`_inflight_where` 要求 progress<1）。
    于是当它是最后一条时循环体一次都不执行，那段新代码是**死代码**
    （R22 两个视角各自跑真实的 `run_qb_sync` 复现：torrents_info 被调 0 次）。
    改动的作用域是"补查这件事"，验证却停在 `sync_qb_status` 那一层 —— 第②号形状。
    """
    if has_inflight():
        return True
    with get_session() as s:
        for model_cls in (AnimeTorrent, MovieTorrent):
            if s.exec(select(model_cls.id).where(
                    *_recheck_where(model_cls, _QB_TRANSIENT)).limit(1)).first():
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
    hit = False
    with get_session() as s:
        for model_cls in (AnimeTorrent, MovieTorrent):
            t = s.exec(select(model_cls).where(model_cls.info_hash == h)).first()
            if t is None:
                continue
            if t.status not in HAVE_STATUSES:
                continue   # 这张表里是终态 → 跨表同 hash 可能另一表还在下/停滞，继续查下一张，别提前 return
            if (t.status == "sent" and (t.qb_progress or 0) >= 1.0 and t.qb_synced_at is not None
                    and (not t.qb_state or t.qb_state in _QB_SEEDING)):
                # 已经落定过了（qB 重启后可能重放一次回调，或跨表同 hash 的另一侧本就已完成）。
                # 【算命中但不写】：写一次会把 qb_synced_at 推到现在，而它正是归档倒计时的起点——
                # 一条早就下完的种子会因为一次重放回调而白白推迟 N 天归档；顺带还会把 qb_state 抹空，
                # 让做种统计凭空掉数。
                # 判据的两头都要卡准：
                #  · 必须 qb_synced_at 非空——settle_sent / settle_inflight_off 写的是 ("sent",1.0,"",None)，
                #    它们【正等着这个回调来补上真实的完成时刻】，跳过就等于让那些行永不归档。
                #  · qb_state 允许为空【或】是做种态——正常下完的行会被 sync 写上 uploading/stalledUP 之类，
                #    只认空串会让绝大多数已完成的行漏出这个短路、每次重放回调都被重写一遍。
                hit = True
                continue
            t.status, t.qb_progress, t.qb_state, t.qb_synced_at = "sent", 1.0, "", datetime.now()
            s.add(t)
            hit = True
            log.info("qB 完成回调：标记已下完 - %s（%s）", h[:12], model_cls.__name__)
        # 【两张表都要走完，不能命中一张就 return】上面那句注释说的正是这件事，可命中分支却当场
        # return 了：TV 与剧场版偶有同一物理种子（同 hash），先命中的那张表标完就走，另一张表里
        # 那条可能正卡在 stalled——而 stalled 已被 sync 脱离轮询，这个回调是它唯一的翻身机会。
        if hit:
            s.commit()
    return hit   # 两张表都不是我们的种子 → 忽略


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
    # 【标记要带业务库身份】(R26) 它判的是"**这个业务库**回填过没有"，而标记存在 meta 库、
    # 不随业务库走。第三处同款标记，理由与 core/anime.py 的 `_scoped` 逐字相同 ——
    # 三处标记同一个决定，之前只有零处落到（第①号形状）。
    from db import data_identity
    flag = f"_QB_PROGRESS_BACKFILLED@{data_identity()}"
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


def observation_gap_seconds() -> int:
    """"这段空白不能归因于种子"的判定窗口（秒）。

    【必须按 worker 的【最慢】那一档正常节拍来算，不是活跃节拍】(R21 修)
    原来窗口是 `max(600, QB_SYNC_INTERVAL*10)` —— 那是按**活跃**间隔（默认 30 秒）算的，
    可真正决定"两次同步之间隔多久"的是 `run_qb_sync` 的**中档**节拍：
    一条无源 0 速的种子让 `has_active_downloading()` 恒为 False，内层快循环跑满
    `QB_SLOW_ROUNDS` 就退出，外层 `wait_for` 睡 `QB_IDLE_RECHECK_MIN*60` = 默认 **600 秒**
    —— 恰好等于旧窗口。于是每次唤醒的第一轮都被判成"观测断档"、把 `qb_progress_at` 重置回 now，
    而同一突发内后面两轮只隔 30 秒：`now - qb_progress_at` 最大 90 秒，
    离 `QB_STALL_TIMEOUT_MIN`（默认 1440 分钟）差三个数量级。
    **判据用错了节拍，于是停滞检测在默认配置下从未生效过**（R21 的用例复现了 26.7 小时零推进仍不触发）。

    加 300 秒余量：那一觉醒来之后还要跑 `qb.reachable()` + `archive_old_completed()` + 本轮同步，
    真实的 `now - prev_synced` 会比 `QB_IDLE_RECHECK_MIN*60` 略大一点。
    """
    return max(600, config.QB_SYNC_INTERVAL * 10, config.QB_IDLE_RECHECK_MIN * 60 + 300)


def _observation_gap(prev_synced, now) -> bool:
    """距上一次成功同步是不是隔了太久——久到这段空白【不能归因于种子】。

    窗口见 observation_gap_seconds()：正常在线时（含中档自查那一档）sync 都在窗口内刷新
    qb_synced_at，此闸不误伤；真正的断档（关机、qB 挂掉）才会命中。
    prev_synced 为空＝这一行还没被同步过，同样不该拿它去判停滞。
    """
    if prev_synced is None:
        return True
    return (now - prev_synced) > timedelta(seconds=observation_gap_seconds())


async def sync_qb_status(model_cls, manual: bool = False) -> int | None:
    """从 qB 拉某表『在下的』种子实时态并写回。整轮串行化，实现见 _sync_qb_status。

    【三态返回】int=实际更新了几行（0 也是有效答案：没有在下的种子）/ **None=没能问到 qB**
    （连不上、超时、被总超时切断）。调用方必须把 None 与 0 分开 ——
    否则 qB 关机时页面会弹一句绿色的『没有正在下载的种子』，而同屏正写着『正在下载（5）』。

    manual=True 是【页面上人点了『立刻刷新』】。差别只有一处：没有在下种子时，
    后台轮次直接收工，而人工这一次仍会去补查『文件缺失』的行——理由见 _sync_qb_status 里
    那段"只在已有在下种子时搭车"。

    锁在这一层而不是在调用方：调用方有四个（后台轮询两条线 + 两个页面按钮），任何一个漏加都会
    把下面那套"宽限一轮"的墙钟语义打回原形（理由见 _sync_locks）。页面想避开排队等待可以先问 sync_busy()。
    """
    lock = _sync_locks.setdefault(model_cls, asyncio.Lock())
    async with lock:
        return await _sync_qb_status(model_cls, manual)


async def _sync_qb_status(model_cls, manual: bool = False) -> int | None:
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
        # 【文件缺失的行也一起问一遍 qB】missingFiles 属落定态（_QB_SETTLED），本来会被
        # 上面的 _inflight_where 排除——那是对的：它不该把同步循环钉醒、也不该被判成失败。
        # 但排除得太干净就成了死胡同：用户在 qB 里把文件放回去、重新校验之后，
        # 我们再也不去看它一眼，UI 上那条醒目的『文件缺失』告警【永不消失】，该行也永不归档。
        # 所以额外查一遍它们、跟着本轮一起刷新：qB 侧一恢复，真实态就把记号覆写掉、告警自动消失。
        # 【后台轮次只在已有在下种子时搭车】没别的事时不为它单独唤醒 qB —— 恢复文件是人工动作，
        # 不急这一轮。但【人工点『立刻刷新』时不搭车、单独查】：那一下正是"我刚把文件放回去了"
        # 的时机，而搭车口径下它恰好不成立（都下完了才会去修文件，此时没有在下种子），
        # 于是那条『文件缺失』告警要挂到下一次有新种子在下时才会消失——休播期可能是几周。
        # 【搭车条件只对 missingFiles 成立，对过渡态不成立】(R21)
        # 上面那段"没别的事时不为它单独唤醒 qB"的立论是"恢复文件是人工动作，不急这一轮" ——
        # 那只说得通于 missingFiles。`_QB_TRANSIENT` 的 7 个态是 qB 自己**几秒内就会走完**的
        # 过渡态：开了 temp_path 时每个种子完成瞬间必经 moving；qB 重启时未完成种子必经
        # checkingResumeData；用户强制校验必经 checkingUP；磁链交付必经 metaDL。
        # 只要 sync 恰好在那几秒里采样到一次，该行就写成 progress=1.0 + qb_state=moving ——
        # 它同时掉出 `_inflight_where`（progress≥1）与 `has_inflight()`，
        # 于是当它是**最后一条**在下的种子时 rows 为空、recheck 整个不跑，后台再也不问 qB：
        # 永久停在中间态（不归档、qb_summary 不算它已完成、UI 永远显示『移动中』）。
        # 所以拆成两段：过渡态无条件补查，missingFiles 保持搭车。
        _states = set(_QB_TRANSIENT) if not (rows or manual) else set(_QB_NEEDS_RECHECK)
        recheck = [(t.id, t.info_hash, True)
                   for t in s.exec(select(model_cls).where(
                       *_recheck_where(model_cls, _states)))
                   if t.info_hash]
    if not rows and not recheck:
        return 0
    # 【补查行必须与在下行分开】它们【不参与】下面那套"qB 查不到就在有限轮内落定"的判据：
    #  · 文件缺失的种子 qb_progress 常常正是 1.0（归档闸那段注释就是为此写的），一进 d is None
    #    的第一个分支就会被清成"已下完"——UI 从『文件缺失』变『已交付』、随后被归档、
    #    集去重认定该集已有一份，而盘上根本没有文件，全程不报错。
    #  · qB 查不到一条【本就报文件缺失】的种子，唯一正确的结论是"维持原状"：它可能已被用户
    #    从 qB 删掉，也可能 qB 还没扫到——两种都不该由我们改写状态。
    # 它们也不进 absent 的分母：那个比例判据算的是"在下的种子里有多少不见了"。
    # 【两个名单严格互斥】由 _recheck_where 的最后一个 or_ 保证（那里写着为什么必须如此）。
    # 下面 `if tid in recheck_ids: continue` 依赖的正是这个互斥：它按 tid 判、认不出来源，
    # 一旦两边重叠，属于 rows 的那一份也会被跳过、这一行永远落不了定。
    recheck_ids = {tid for tid, _, _ in recheck}
    rows_all = rows + recheck
    info = await qb.torrents_info([h for _, h, _ in rows_all])
    if info is None:
        # 【返回 None 而不是 0】(R21) 三种完全不同的情形本来都塌成 `0`：
        #   ① 真的没有在下的种子；② qB 连不上；③ await 期间用户关了跟踪。
        # 而页面上的『立刻刷新』按 `n` 是不是 0 出文案，于是 qB 关机时弹的是
        # **绿色**的『没有正在下载的种子』—— 页面同屏还写着『正在下载（5）』，自相矛盾。
        # 上面那个 `except Exception → 同步失败` 接不住它：整条链
        # （_login 的 `except httpx.HTTPError → None` → torrents_info 的 `if resp is None: return None`
        # → 这里）从头到尾不抛异常，注释里点名要接的那一种恰恰是唯一接不到的。
        # 与本项目已有的三态约定同形（add_to_qb / download_*_torrent 的 True/False/None）。
        return None   # 只在『连不上/出错』(None) 本轮不动。空 dict {} 是『qB 在线但这批一个都不在』——
                   # 须落到下面逐行走 d is None 落定(全被删/移除时)，否则它们永久 in-flight、循环永不休眠。
    if not config.QB_SYNC_STATUS or not config.QB_ENABLED:
        return 0   # await 期间用户关了跟踪/关了 qB（这批已由 settle_inflight_off 落定）——
                   # 别用陈旧 qB 数据把它们覆写回在下态，也别给刚落定的行盖上新的 qb_synced_at：
                   # 那个时间戳是归档判据的一半，盖上去会让"其实没下完"的种子在 N 天后被 delete 掉。
    # 【批量缺席：只抑制"判失败"，不跳过整轮】qB 重启时 WebUI 先绑好端口、resume-data 还在异步装载，
    # 此期间 torrents/info 会合法返回 200 + 【不完整】列表——不是 None、也不是空 dict，上面两个闸都
    # 挡不住它。单行判据（下面的 _QB_ABSENT 记号 + 墙钟下限）只能拖慢误杀，挡不住"整批一起消失"。
    # 判据用【比例】而不是绝对数：正常收敛是零星的（下完被 remove-on-complete 删掉一两条），
    # 而装载期是成批的。≥3 行才启用，避免只跟 1~2 条种子时被正常收敛误伤。
    #
    # 【这里【不能】整轮 return】——那正是本函数曾经的写法，而它有个致命缺口：判据只看【本轮】比例，
    # 于是"用户在 qB 里一次性删掉大半在下的种子"这种【稳态】缺席会每一轮都命中闸门，永远 return，
    # 结果是：缺席行永不落定 → 恒满足 in-flight → 同步循环永不休眠、归档永不发生，
    # 而【在场的健康行】也被连坐、进度冻结在最后一次成功同步的值（UI 上是一个不动的"下载中 42%"）。
    # 那与本函数上面两处注释承诺的"保证有限轮内落定"直接矛盾。
    # 正确形态是：照常进循环（在场的照常镜像进度，缺席的照常打记号），只在【落 error 那一步】
    # 多给一段【有界】的额外宽限。有界 = 批量抑制自己也会到点，行最终一定会落定。
    absent = sum(1 for _, h, _ in rows if h not in info)   # 只数在下的，不含补查行
    batch_absent = len(rows) >= 3 and absent > len(rows) / 2
    if batch_absent:
        log.warning("qB 回报的在下种子缺了 %d/%d（多半是 qB 正在重启装载 resume-data）："
                    "本轮只抑制『判失败』，在场种子的进度照常同步", absent, len(rows))
    now = datetime.now()
    updated = 0
    with get_session() as s:
        # 【一次 IN 取回，别逐行 s.get()】s.get() 在新 session 里对每一行各发一条 SELECT——
        # 200 条在下的种子实测 116ms，换成一条 IN 查询是 11ms（10×）。语义等价：
        # 两种写法都是"await 之后用新 session 重读"，取不到 == 原来的 `t is None`
        # （行在 await 期间被删了）。
        ids = [tid for tid, _, _ in rows_all]
        fresh = {t.id: t for t in s.exec(select(model_cls).where(model_cls.id.in_(ids)))}
        for tid, h, was_synced in rows_all:
            t = fresh.get(tid)
            if t is None or t.status not in TRACKED_STATUSES:
                continue
            d = info.get(h)
            if d is None:
                if tid in recheck_ids:
                    # 【补查行也要有个终点，不能一律"维持原状"】(R24)
                    # "维持原状"这条规则是为 `missingFiles` 写的（用户可能把文件放回去、重新校验，
                    # 那时 qB 会重新认得它）。可 R21/R23 把补查集合扩成了
                    # `{missingFiles} ∪ _QB_TRANSIENT` —— 而对那 7 个**过渡态**，
                    # "维持原状"就是**永久悬空**：再没有任何路径能改写它。
                    # 触发序列很日常：① 开 temp_path 时每个种子完成瞬间必经 `moving`、
                    # qB 重启时必经 `checkingResumeData`、强制校验必经 `checkingUP`、
                    # 磁链交付必经 `metaDL`，sync 在那几秒采样到一次就写成
                    # `sent / progress=1.0 / qb_state=moving`；② 此后该种子离开 qB
                    # （用户删掉、remove-on-complete、跨表同 hash 被另一侧归档时 qb.delete 摘走）。
                    # 后果三条：`archive_old_completed` 把 `_QB_NEEDS_RECHECK` 永久排除
                    # （用户配的 QB_ARCHIVE_AFTER_DAYS 对它恒不生效）；`needs_qb_poll()` 恒为真，
                    # run_qb_sync 再也回不到保底那一档、每 10 分钟醒来空打一轮 qB，永远；
                    # UI 恒显示『移动中 100%』。
                    # 这正是本函数上面那句不变式（"必须在有限轮内落定，否则循环永不休眠"）
                    # 被补查这一半绕开了。
                    #
                    # 分两类收敛：missingFiles 维持原状（那条立论成立）；
                    # 过渡态 —— qB 都查不到了，说明那几秒早就走完，把记号清掉即可
                    # （progress 已是 1.0，等价于"下完后被 qB 移除"，与上面 in-flight 分支第一条同款）。
                    if t.qb_state in _QB_TRANSIENT:
                        t.qb_state, t.qb_synced_at = "", now
                        s.add(t)
                        updated += 1
                    continue
                # qB 查不到这个在下的种子——必须在有限轮内落定，否则它恒满足 in-flight、循环永不休眠。
                # 用【重读后】的实时进度判定（await 期间该行可能被完成回调 mark_done_by_hash/新交付推进到满）：
                # 若仍用 await 前的陈旧快照，会把刚被 /api/qb/done 回调标『已下完』的行覆写回 error、使回调形同虚设。
                # 【判据必须是"确实被落定过的满"，不是"接近满"】(R24 收紧)
                # 这一条的立论是"已满(含完成回调刚落定)"，而"已满"的两个来源
                # （`mark_done_by_hash` / `settle_sent`）写的都是**精确的 1.0 且 qb_state 清空**。
                # 旧判据 `>= 0.999` 会把**从 qB 镜像来的真实进度**一起吃进去：
                # 一条 1.5GB 的种子下到 99.95%（还差 750KB）时从 qB 消失，本轮就被写成
                # `progress=1.0, qb_state="", qb_synced_at=now` —— 它**不经过** `_QB_ABSENT` 记号、
                # **不经过** `max(600, QB_SYNC_INTERVAL*10)` 的墙钟宽限，
                # **也不受 `batch_absent` 抑制**（那个闸只挡最下面的 else 分支）。
                # 于是 qB 每重启一次，所有卡在最后 0.1% 的种子都被判成下完：
                # `sent ∈ HAVE_STATUSES` ⇒ flush/补下/换源永不再碰这一集，
                # `qb_synced_at=now` 还给归档倒计时上了发条，N 天后 UI 显示『已归档』——
                # 而盘上是个残缺文件（开 temp_path 时最终目录里干脆什么都没有），全程零告警。
                # 对照：同一函数下面为浮点噪声定的 epsilon 是 **1e-9**，这里却用了 1e-3，差 6 个数量级。
                # 镜像来的 0.999x 应当落到下面的 _QB_ABSENT + 墙钟宽限路径，最终判 error ——
                # 那才是可换源、可补下、可见的结局。
                if (t.qb_progress or 0.0) >= 1.0 - 1e-9 and not t.qb_state:
                    t.qb_progress, t.qb_state, t.qb_synced_at = 1.0, "", now
                elif not was_synced:        # 从未被 qB 确认(刚交付未登记?) → 给一轮宽限，下轮仍无则→error
                    t.qb_synced_at = now
                elif t.qb_state != _QB_ABSENT:
                    # 曾在下、这一轮从 qB 消失 —— 【先记号，不立刻判死】；何时判死见下面的 else 分支
                    # （记号 + 墙钟下限两个条件都满足才落 error），本行只负责记下"从这一刻起消失的"。
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
                else:                       # 记号已在（至少连续第二轮查不到）→ 再过墙钟下限才落定 error。
                    # 【记号 + 墙钟，两个条件都要满足】只有记号是不够的：轮询节奏由 QB_SYNC_INTERVAL 决定
                    # （默认 30s），"宽限一轮"在墙钟上可能只有半分钟，而 qB 重启装载 resume-data 动辄几分钟。
                    # 上面那段注释担心的"基于时间的阈值要么误判、要么永远进不了 error"，症结在【每轮都刷新
                    # 时间戳】；这里只在首次 miss 写一次（下面的分支不写），时间戳就成了"从哪一刻起消失的"，
                    # 于是既有确定的下限、也一定会在有限时间内到点，不会永久滞留 in-flight。
                    # 批量缺席期用更长的那档（qB 装载一整批 resume-data 比丢一条慢得多），
                    # 但【仍然有上限】：稳态缺席（用户一次性删光）会在这段时间后照常落定。
                    grace = (max(1800, config.QB_SYNC_INTERVAL * 20) if batch_absent
                             else max(600, config.QB_SYNC_INTERVAL * 10))
                    if (now - (t.qb_synced_at or now)).total_seconds() < grace:
                        continue            # 时间没到：这一轮什么都不写，让 qb_synced_at 停在首次 miss 那刻
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
            # 【这段空白是"种子没动"还是"我们没在看"？】上一次同步的时刻，覆写之前先留下来。
            prev_synced = t.qb_synced_at
            t.qb_state = state
            t.qb_progress = float(d.get("progress", 0) or 0)
            t.qb_dlspeed = int(d.get("dlspeed", 0) or 0)
            t.qb_size = int(d.get("size", 0) or 0)
            t.qb_synced_at = now
            # 【比较要带 epsilon】这是第二道保险：即便列类型退回窄浮点（MySQL 的 4 字节 FLOAT
            # 会把 0.4212345 存成 0.42123448848724365），也不会因 round-trip 噪声把"没动"
            # 误判成"推进了"——而那个误判会让 status 永远走不到 stalled，停滞检测整个功能失效。
            # 1e-9 远小于一次真实推进（qB 的 progress 至少按块跳），也远大于 float32 的噪声。
            if t.qb_progress > prev_progress + 1e-9 or t.qb_progress_at is None:
                t.qb_progress_at = now      # 进度推进(或首见)→ 刷新『上次推进时间』，作停滞判定基准
            if state == "error":
                # 【落 stalled 而不是 error】(E-19，2026-09-01 拍板)
                # 这一行回传的是"qB 侧出错了"，而盘上【有半成品】：种子还在 qB 里占着 save_path。
                # 落 error 的后果不是"少下一集"：error ∉ HAVE_STATUSES ⇒ 下一轮 flush 认为
                # 这一集还缺，把同集另一个源的第二份放行到【同一个 save_path】，
                # 而 qB 那边第一份还在。两份文件落进同一文件夹，而原行【再没有任何 UI 入口能删】
                #（详情页的删除按钮只对 HAVE 里的行出现）。
                # stalled 同时满足三件事，正是为这种情形准备的：
                #   · ∈ HAVE_STATUSES  → 集去重挡着，不会自动下第二份
                #   · ∉ TRACKED_STATUSES → 同步循环能休眠，不再每轮空转
                #   · mark_done_by_hash 认它 → qB 侧恢复（重新校验/换源做种）后回调救得回
                # 【error 这个词此前被两条路径写成了两种语义】"从没交付出去"（取种失败/qB 拒收）
                # 与"交付过、盘上有半成品"。前者留给 error，后者归 stalled——这也让
                # failed_rows 的 ("error","stalled") 两栏各自名副其实。
                # missingFiles 有意不回传（只镜像显示），见上面 _QB_NEEDS_RECHECK 的说明。
                t.status = "stalled"
            elif t.status == "downloading" and t.qb_progress >= 1.0:
                t.status = "sent"     # 兼容旧的 downloading 占位（正常已在交付时置 sent）
            elif state in _QB_NOT_ADVANCING:
                # qB 排队等额度 / 被要求停下：这段时间不算"卡住"，重置停滞计时基准。
                # 否则 max_active_downloads 一小、批量补番时整批会被误标停滞异常
                # （脱离轮询 + 不自动换源 + UI 一堆假告警）。
                t.qb_progress_at = now
            elif _observation_gap(prev_synced, now):
                # 【观测断档：这段时间我们根本没在看】qB 不可达时 sync 走 `info is None` 分支、
                # 一个字段都不写；autorss 自己停机时更是一轮都没跑。两种情况下 qb_progress_at
                # 都冻在断档之前，恢复后第一轮 `now - qb_progress_at` 当场超过停滞阈值（默认 1 天）。
                # 后果是批量的：关机一天后开机，进程做的第一件事就是把【所有】在下种子标成 stalled，
                # 而 stalled ∉ TRACKED_STATUSES ⇒ sync 再也不看它们（哪怕 qB 已经 3MB/s 在下），
                # stalled ∈ HAVE_STATUSES ⇒ 集去重挡着不换源，还推一条内容是错的告警。
                # 判据与 has_active_downloads 的"新鲜度闸"同源：那条注释写的正是
                # 「qB 掉线时 sync 走 None 分支不刷新 qb_synced_at」——两个消费者共用一个判据，别再分家。
                t.qb_progress_at = now      # 重新起算，而不是把断档算到种子头上
            elif (config.QB_STALL_TIMEOUT_MIN > 0 and t.qb_progress < 1.0
                  and t.qb_progress_at is not None
                  and not _observation_gap(prev_synced, now)
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
            model_cls.qb_state != "",
            func.coalesce(model_cls.qb_state, "").not_in(_QB_NEEDS_RECHECK_LIST),
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
