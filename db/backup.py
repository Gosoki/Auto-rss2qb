"""SQLite 备份：整库快照 + 保留最近 N 份。

【为什么不能用 shutil.copy2】本项目的 SQLite 开着 WAL（见 db/__init__ 的 _sqlite_pragmas）。
WAL 模式下，最近的写入躺在 `-wal` 文件里、还没合并进主文件——直接拷主文件会拿到一份
【陈旧甚至根本打不开】的库，而且它长得完全正常：文件在、大小合理、天天"备份成功"，
只有真去恢复的那一天才发现缺了最近的数据。备份最坏的失败方式就是这一种。

`VACUUM INTO` 由 SQLite 自己完成一致性快照（读事务内导出，含 WAL 里未合并的内容），
产物是一个已整理过的独立库文件，且【不需要停写】。SQLite ≥ 3.27 支持，
Python 3.11 自带的版本远高于它。

业务库切到 MySQL 时这里只备得到本地的配置库（setting 表）——那不是"备份失败"，
而是范围就到这；调用方要把这件事明确告诉用户，不能让人以为番剧数据也备了。

【恢复的顺序不能乱】停服务 → **删掉 autorss.db-wal 与 autorss.db-shm** → 把备份复制成
autorss.db → 启服务。第二步不能省：只覆盖主文件的话，SQLite 启动时会把【事故现场那份 -wal】
重放到刚恢复的库上，拿回的是出事【后】的数据，而 `pragma quick_check` 照样返回 ok。
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
    same = _sqlite_path(engine) == meta_path
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
    note = ("本次备份含【配置 + 全部业务数据】（两者同库）" if same else
            "业务库当前是 MySQL，本次【只备了配置】（源组/设置），番剧与种子数据不在其中")
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
        out.append({"name": p.name, "path": str(p), "bytes": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime),
                    "scope": p.name.split("-")[1]})
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
    doomed = [d for items in by_scope.values() for d in items[keep:]
              if protect is None or Path(d["path"]) != protect]
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
    return True, f"可用（{len(tables)} 张表）"


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
