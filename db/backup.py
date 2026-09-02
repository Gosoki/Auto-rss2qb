"""SQLite 备份：整库快照 + 保留最近 N 份。

【为什么不能用 shutil.copy2】本项目的 SQLite 开着 WAL（见 db/__init__ 的 _sqlite_pragmas）。
WAL 模式下，最近的写入躺在 `-wal` 文件里、还没合并进主文件——直接拷主文件会拿到一份
【陈旧甚至根本打不开】的库，而且它长得完全正常：文件在、大小合理、天天"备份成功"，
只有真去恢复的那一天才发现缺了最近的数据。备份最坏的失败方式就是这一种。

`VACUUM INTO` 由 SQLite 自己完成一致性快照（读事务内导出，含 WAL 里未合并的内容），
产物是一个已整理过的独立库文件，且【不需要停写】。SQLite ≥ 3.27 支持，
Python 3.11 自带的版本远高于它。

【scope 标签只表达"这一刻业务数据在不在这个文件里"，不表达"文件里有几张表"】
备份动作恒为对 meta_path 整个文件做 VACUUM INTO——切到 MySQL 【不会】删掉本地文件里
原有的业务表（switch_data_engine 明写"只切连接，不搬数据"）。所以一份标着 meta 的备份
里通常还躺着切换前的整套番剧数据，只是【旧的】。
早先这里写的是"只备得到本地的配置库（setting 表）"，那是假话，而且是危险的假话：
用户照着它以为番剧数据没备到，于是把这份文件当垃圾删掉——那可能是 MySQL 迁移出问题后
唯一剩下的一份番剧数据。真实内容一律以 _peek() 现场数出来的行数为准，别信标签。

【恢复的顺序不能乱】停服务 → 把现役的 autorss.db 连同 -wal/-shm 一起【改名留底】→
把备份复制成 autorss.db → 启服务。
· 第二步不能只覆盖主文件：SQLite 启动时会把【事故现场那份 -wal】重放到刚恢复的库上，
  拿回的是出事【后】的数据，而 `pragma quick_check` 照样返回 ok。
· 但也【不能 rm】：改名留底之后这一步就是可逆的。恢复用的备份可能选错（meta 恢到 full 上
  是最容易犯的那种），而删掉之后现役库就没了——那是全流程唯一不可逆的一步，
  换成 mv 不多花一分力气，却把"选错了还能退回去"变成了可能。
"""
import logging
import os
import re
import sqlite3
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from config import DATA_DIR

log = logging.getLogger("autorss")

BACKUP_DIR = DATA_DIR / "backups"
# 文件名里带来源标签，一眼能看出这份备份含什么：meta=只有配置，full=配置+业务同库
_NAME_RE = re.compile(r"^autorss-(meta|full)-\d{8}-\d{6}\.db$")
# 同进程内串行化 backup_now：后台协程与页面按钮打的是同一个函数（见 backup_now 里的 TOCTOU 说明）
_backup_lock = threading.Lock()


def _sqlite_path(eng) -> str | None:
    """引擎背后的 SQLite 文件路径；不是 SQLite（或是内存库）则 None。"""
    url = eng.url
    if url.get_backend_name() != "sqlite" or not url.database:
        return None
    return url.database


# 业务表（判断一份备份里到底有没有番剧数据）。与 db.transfer.TABLE_ORDER 同一批表，
# 但这里【故意不 import 它】：那边的顺序服务于外键，改动频繁；这里只需要"有没有"。
_BUSINESS_TABLES = ("anime", "animetorrent", "movie", "movietorrent",
                    "sourcegroup", "anime_alias")


def _peek(path: str) -> dict:
    """打开一份备份，数一数里面到底有什么。失败返回 {}。

    【为什么不能靠文件名里的 scope 判断】scope 是【导出那一刻的配置】推出来的标签，
    而导出动作恒为整文件快照。切到 MySQL 之后本地文件里的业务表【不会被删】，
    于是一份标着 meta 的备份里往往还有整套（旧的）番剧数据。反过来，
    一个刚 upgrade 出来、还没跑过业务的库标成 full，里面却一行数据都没有。
    要回答"这份能不能救回我的番"，只能现场数。

    【它挂在页面渲染路径上，所以量过】52 MB / 10.5 万行种子的库上 _peek 耗时 **2 ms**
    （6 个 count(*)），5 份备份的 list_backups 整体 6~10 ms。不需要 to_thread。
    对比：同一个文件上 verify() 要 113 ms，那是 quick_check 的开销，与本函数无关。
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except Exception:
        return {}
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        rows = {}
        for t in _BUSINESS_TABLES:
            if t in names:
                try:
                    rows[t] = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                except sqlite3.DatabaseError:
                    rows[t] = None
        # 【顺手把"这份备份把业务库指向哪"读出来】(R28) 备份是对 meta_path 整文件做
        # VACUUM INTO，`setting` 表原样进快照 —— **包括 DB_BACKEND 与 DB_MYSQL_***。
        # 恢复一份【切到 MySQL 之前】做的备份，重启后 `apply_configured_backend()`
        # 读到的就是 `DB_BACKEND=sqlite`：系统静默跑在**另一份数据集**上，
        # 页面显示切换前的旧番剧，后台采集/交付往本地 SQLite 写，MySQL 上的真数据
        # 既看不见也不再更新，全程零告警。这正是 db/__init__.py 的 `_data_down` 注释
        # 判定为"比直接停摆危险得多"的那种静默回退。
        # 而页面上那个绿色徽标只数**本地文件里**的业务表行数，切到 MySQL 并不会删掉
        # 本地那套旧表 —— 于是 MySQL 用户的每一份备份都显示"配置+业务"，
        # 恰恰把人往那份旧备份上引。所以这里必须把指向也读出来、写到页面上。
        backend = mysql_db = None
        if "setting" in names:
            try:
                got = dict(conn.execute(
                    "SELECT key, value FROM setting WHERE key IN "
                    "('DB_BACKEND','DB_MYSQL_HOST','DB_MYSQL_NAME')").fetchall())
                backend = (got.get("DB_BACKEND") or "sqlite").strip().lower()
                if backend == "mysql":
                    mysql_db = f"{(got.get('DB_MYSQL_HOST') or '?').strip()}/" \
                               f"{(got.get('DB_MYSQL_NAME') or '?').strip()}"
            except sqlite3.DatabaseError:
                pass
        return {"tables": sorted(names), "rows": rows,
                "anime": rows.get("anime") or 0, "movie": rows.get("movie") or 0,
                "torrents": (rows.get("animetorrent") or 0) + (rows.get("movietorrent") or 0),
                "backend": backend, "mysql_db": mysql_db}
    except Exception:
        return {}
    finally:
        conn.close()


def _summarize(pk: dict) -> tuple[str, bool]:
    """把一次 _peek 的结果翻译成 (一句话, 有没有业务数据)。

    【只此一份，且只 peek 一次】describe_content 与 has_business_data 早先各自调一次 _peek，
    而 list_backups 会把两者对每份备份都调一遍——同一个文件被打开两次、6 个 count(*) 数两遍，
    而它就挂在页面渲染路径上（_peek 的 docstring 特意写了"量过"，却量的是单次）。
    实测 7 份 × 206MB 时冷启 121ms。两个结果本来就来自同一个字典。
    """
    if not pk:
        return "内容读不出来", False
    has = bool(pk["anime"] or pk["movie"] or pk["torrents"])
    if not has:
        return f"{len(pk['tables'])} 张表，【无业务数据】（只有全局设置）", False
    return (f"{len(pk['tables'])} 张表，番剧 {pk['anime']} 部、剧场版 {pk['movie']} 部、"
            f"种子 {pk['torrents']} 条"), True


def describe_content(path: str) -> str:
    """一句话说清这份备份【里面有什么】（不是文件名/大小/时间，那些 list_backups 已经给了）。给 note / 页面徽标 / verify 共用，口径只有这一处。"""
    return _summarize(_peek(path))[0]


def has_business_data(path: str) -> bool:
    """这份备份里有没有番剧/剧场版数据（决定页面徽标的颜色与文案，以及 prune 的救生艇）。"""
    return _summarize(_peek(path))[1]


def backup_now(keep: int = 7) -> dict:
    """立刻做一次备份，并把 backups/ 里最旧的删到只剩 keep 份。

    返回 {"path": str|None, "scope": "full"|"meta", "bytes": int, "pruned": [文件名],
          "note": 给用户看的一句话}。失败抛异常（调用方决定怎么呈现）。
    """
    from db import engine, meta_engine    # 延迟导入：本模块被 db 包内部引用，避免环导入

    meta_path = _sqlite_path(meta_engine)
    if meta_path is None:                 # 理论上不会——配置库恒为本地 SQLite
        raise RuntimeError("配置库不是本地 SQLite，无法备份")

    # 业务库与配置库同一个文件时，一份快照就把两边都备了；切到 MySQL 时只备得到配置。
    # 【不能只看"两个引擎是不是同一个 SQLite 文件"】DB_BACKEND=mysql 但连接参数不全时，
    # apply_configured_backend 走 mark_data_fatal，engine 仍指着本地 SQLite ——
    # 于是 same 恒真、标成 full、note 说"含全部业务数据"，而真数据在够不着的 MySQL 上。
    # 用户拿着这份"完整备份"，实际一条番剧数据都没有。配置说了算，engine 只是当下的状态。
    import config
    same = (_sqlite_path(engine) == meta_path
            and (config.DB_BACKEND or "sqlite").lower() != "mysql")
    scope = "full" if same else "meta"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        BACKUP_DIR.chmod(0o700)           # 与 data/ 同口径：库里有 qB 明文密码
    except OSError:
        pass

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = BACKUP_DIR / f"autorss-{scope}-{stamp}.db"
    # 【先导出到临时名，成功了再改名】文件名精确到秒，后台协程与页面按钮同秒撞车是真实可能
    # （worker.run_backup 与 settings 的『立刻备份』）。早先的写法是 `if out.exists(): raise` +
    # 失败时无条件 unlink(out) —— 那是个 TOCTOU：输家从 VACUUM 拿到 "table already exists"，
    # 然后把【赢家刚做好的那份】删掉，目录里一份都不剩，而自动路径还照样写下 BACKUP_LAST 报成功。
    # 临时名 + os.replace 之后，两者各写各的，最坏结果只是同一秒的两份互相覆盖，绝不会两败俱伤。
    with _backup_lock:                    # 同进程内再串行化一层，省掉大多数撞车
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".db", dir=str(BACKUP_DIR))
        os.close(fd)
        tmp = Path(tmp_name)
        tmp.unlink()                      # VACUUM INTO 要求目标【不存在】；mkstemp 只是用来抢一个唯一名字
        try:
            # 【单独开一个连接，不走 SQLAlchemy 的池】VACUUM INTO 不能在事务里跑，而池里的连接
            # 可能正处于一个隐式事务中。另开一个 autocommit 连接最省心，也不会占住业务用的池。
            conn = sqlite3.connect(meta_path, isolation_level=None)
            try:
                # 目标路径参数化，避免路径里的引号把 SQL 拼坏
                conn.execute("VACUUM INTO ?", (str(tmp),))
            finally:
                conn.close()
            try:
                tmp.chmod(0o600)
            except OSError:
                pass
            os.replace(tmp, out)          # 同目录内原子改名
        except Exception:
            # 只清自己造的那份（含 VACUUM 失败时可能留下的 -journal 之类残骸）——
            # 满盘时它们每 600 秒攒一个，而 _NAME_RE 不匹配，prune 永远不会清理。
            for sfx in ("", "-journal", "-wal", "-shm"):
                Path(str(tmp) + sfx).unlink(missing_ok=True)
            raise

    pruned = prune(keep, protect=out)
    # 【note 说的是文件里【实际】有什么，不是 scope 标签的字面意思】
    # 导出动作恒为对本地 meta_path 整文件 VACUUM INTO，而切到 MySQL 不会删掉本地的业务表，
    # 所以 scope=meta 的那份里通常还有整套【旧的】番剧数据。
    # 早先这里写死"只备了 settings 一张表"——用户照着删掉它，等于删掉 MySQL 出问题后
    # 唯一剩下的番剧数据副本。
    detail = describe_content(str(out))
    if same:
        note = f"本次备份含【配置 + 全部业务数据】（两者同库）：{detail}"
    elif has_business_data(str(out)):
        note = ("业务数据在 MySQL 上，【本次备的是本地库】：" + detail +
                " —— 这些是切到 MySQL【之前】留在本地的旧数据，不是 MySQL 上的现状。"
                "MySQL 那边要自己做备份（mysqldump）")
    else:
        note = ("业务数据在 MySQL 上，本次【只备到了全局设置】：" + detail +
                " —— 番剧、剧场版、种子、源组、番名对照都不在其中，"
                "那些要自己给 MySQL 做备份（mysqldump）")
    log.info("备份完成：%s（%.1f KB）%s", out.name, out.stat().st_size / 1024,
             f"，清理 {len(pruned)} 份旧备份" if pruned else "")
    return {"path": str(out), "scope": scope, "bytes": out.stat().st_size,
            "pruned": pruned, "note": note}


def list_backups() -> list[dict]:
    """现有备份，新的在前。只认本模块自己的命名，不碰用户手动放进来的别的文件。"""
    if not BACKUP_DIR.exists():
        return []
    out = []
    for p in BACKUP_DIR.iterdir():
        if not p.is_file() or not _NAME_RE.match(p.name):
            continue
        st = p.stat()
        _pk = _peek(str(p))                        # 每份只 peek 一次，见 _summarize
        _detail, _has = _summarize(_pk)
        out.append({"name": p.name, "path": str(p), "bytes": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime),
                    "scope": p.name.split("-")[1],
                    # 这份备份里的『业务库指向』——恢复它就会把系统切到这个库上，见 _peek
                    "backend": _pk.get("backend"), "mysql_db": _pk.get("mysql_db"),
                    # 【页面上的徽标要按这个走，不能按 scope】见 _peek 的说明：
                    # scope 是导出那一刻的配置，回答不了"这份救不救得回我的番"。
                    "has_data": _has, "detail": _detail})
    # 【按 mtime 排，不能按文件名】文件名是 autorss-{scope}-{stamp}.db —— scope 段排在时间戳【前面】，
    # 而 'm'(meta) > 'f'(full)。于是一份刚做的 full 备份会排在所有旧的 meta 备份【后面】，
    # 被自己这一次 prune 当成"最旧的"删掉，紧接着 backup_now 里的 out.stat() 抛 FileNotFoundError：
    # 自动备份就此进入"每 10 分钟整库导出一次再删掉、BACKUP_LAST 永不前进"的死循环。
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out


def prune(keep: int, protect: Path | None = None) -> list[str]:
    """每种 scope 各留最近 keep 份，返回被删的文件名。keep<=0 视作不清理（别把用户的备份全删了）。

    protect：这一份【绝不删】。刚做好的备份必须传进来——排序键再怎么改也不该让
    "做一份备份"这个动作有把自己删掉的可能，这是最后一道兜底。
    """
    if keep <= 0:
        return []
    # 【两种 scope 各留 keep 份，不混在一起数】一份"仅配置"的 meta 备份不该顶掉一份
    # "配置+业务"的 full 备份：用户试过一阵 MySQL 又切回来，中间那批 meta 会把 full 全挤走，
    # 而 full 才是真正救得回番剧数据的那种。
    by_scope: dict = {}
    for d in list_backups():                 # 已按 mtime 降序
        by_scope.setdefault(d["scope"], []).append(d)
    # 【每种 scope 里"最新的那份真有业务数据的"永不删】——与 protect= 同一个性质的兜底。
    # R17 把"别信文件名里的 scope、现场数行数"推到了三处【读】的地方（页面徽标、verify 的说明、
    # backup_now 的 note），却没推到这里——而这里是整个模块唯一会删【已经存在、之前验过】
    # 的备份的地方，用的偏偏还是 R17 亲自判定为不可信的那个判据。
    # 后果链是真的：库被误恢复/误清空之后，auto_tick 每天照常产出一份
    # "可用、quick_check ok、scope=full"的【空】备份并记 BACKUP_LAST，
    # BACKUP_KEEP 天之后 prune 把唯一那份还救得回数据的当成"最旧的"删掉，留下一整排空的。
    # （用户机器上此刻就躺着 4 份标着 full、业务数据为 0 的备份，所以这不是假想。）
    # 保住的只有【一份】：不改 keep 的语义，也不会让备份目录无限增长。
    lifeboats = set()
    for items in by_scope.values():
        for d in items:                      # 已按 mtime 降序，第一份命中的就是最新的
            if d.get("has_data"):
                lifeboats.add(d["path"])
                break
    doomed = [d for items in by_scope.values() for d in items[keep:]
              if (protect is None or Path(d["path"]) != protect)
              and d["path"] not in lifeboats]
    gone = []
    for d in doomed:
        try:
            Path(d["path"]).unlink()
            gone.append(d["name"])
        except OSError as e:
            log.warning("删除旧备份失败 %s: %s", d["name"], e)
    return gone


def verify(path: str) -> tuple[bool, str]:
    """打开这份备份做一次快速自检。返回 (是否可用, 说明)。

    备份最要命的失败方式是"文件在、大小正常、其实打不开"，所以做完/恢复前都该验一次。
    """
    if not os.path.exists(path):
        return False, "文件不存在"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            ok = conn.execute("PRAGMA quick_check").fetchone()
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
        finally:
            conn.close()
    except Exception as e:
        return False, f"打不开：{type(e).__name__}: {e}"
    if not ok or ok[0] != "ok":
        return False, f"完整性检查未通过：{ok}"
    if "setting" not in tables:
        return False, "缺少 setting 表（不像是本程序的库）"
    # 【"可用"必须连内容一起说】只报"可用（N 张表）"时，把一份【没有业务数据】的备份
    # 恢复到一个有 99 部番的库上，这里照样绿灯、quick_check 照样 ok、启动日志照样正常，
    # 而番全没了。表数量对用户没有任何意义，"番剧 0 部"才有。
    return True, f"可用（{describe_content(path)}）"


def auto_tick() -> bool:
    """后台心跳调用：开了自动备份且距上次 ≥ BACKUP_INTERVAL_HOURS 就备一次。备了返回 True。

    判据与剧场版自动扫描同款：把"上次什么时候做的"存进 settings 表而不是内存计时器，
    这样【跨重启也不会误重做】——否则每次重启都会立刻再备一份，几次重启就把保留份数刷没了。
    """
    import config
    if not config.BACKUP_ENABLED:
        return False
    last = config.BACKUP_LAST
    if last:
        try:
            elapsed = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
        except ValueError:
            elapsed = float("inf")       # 时间戳坏了（手改过库？）→ 视作到点，自愈而不是永久卡住
        # elapsed < 0 = 系统时钟被回拨：视作到点照备（多一份备份无害，少一份可能致命）
        if 0 <= elapsed < max(1, config.BACKUP_INTERVAL_HOURS) * 3600:
            return False
    try:
        res = backup_now(keep=config.BACKUP_KEEP)
    except Exception as e:
        log.error("自动备份失败：%s: %s", type(e).__name__, e)
        return False
    # 【做完就验一次】verify 的 docstring 说的就是"做完/恢复前都该验"，而自动这条路径是
    # 唯一没人盯着的：验不过还记下 BACKUP_LAST，就会安静地等满一个间隔再做下一份，
    # 中间这段时间用户手里的"最新备份"其实打不开。
    ok, why = verify(res["path"])
    if not ok:
        log.error("自动备份做完了但自检未通过（不记时间，下次心跳重做）：%s —— %s", res["path"], why)
        try:
            Path(res["path"]).unlink(missing_ok=True)   # 可疑文件比没有更危险
        except OSError:
            pass
        return False
    # 【只有成功才记时间】失败不记 → 下次心跳还会再试，而不是等满一个间隔
    config.set_many({"BACKUP_LAST": datetime.now().isoformat(timespec="seconds")})
    log.info("自动备份：%s", res["note"])
    return True
