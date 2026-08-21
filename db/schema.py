"""数据库版本管理：统一走 Alembic。

历史上这里是五个"每次启动都跑一遍"的临时迁移函数（加列、状态值改名、建 partial index、
删冗余索引、拓宽 INT）。它们的问题是：没有版本概念，改了模型只能靠"下次启动时比对补上"，
既看不出库处在哪一版，也没法回退，跨 SQLite/MySQL 两种方言时更要各写一份分支。
现在一律用 Alembic：库里有 alembic_version 表记着版本号，升级=跑 revision，可查可控。

【双引擎】两个引擎各自独立地跑同一条版本链，各有各的版本表（见 alembic/env.py）：
    role="meta" → 本地 SQLite，只管 setting
    role="data" → 业务库（SQLite 或 MySQL），管其余 6 张表

【本项目不做向后迁移】切到 Alembic 那天起老库一律重建，故基线就是起点、没有 downgrade。
以后改表结构：改完模型后跑
    PYTHONPATH=. .venv/bin/alembic -x role=data -x url=<业务库URL> revision --autogenerate -m "说明"
生成的脚本记得按 role 分支（meta 只碰 setting），并检查 SQLite/MySQL 两边都成立。
"""
import logging
import threading
from pathlib import Path

# 【必须在 import alembic 之前】alembic 那堆 "setup plugin …" / "Context impl …" 是 INFO 级，
# 会顺着 logger 继承传播到本项目的 root handler、灌进 data/autorss.log（每次热重载好几行）。
# 其中 "setup plugin" 是在【导入 alembic 时】就打的，等到模块体后面再压级别已经晚了。
# 压到 WARNING：真出错仍看得见，日常升级只由下面 upgrade() 打一行人话。
# 注意这与 alembic.ini 里的 [logger_alembic] 是两回事——那段只在走 CLI 时生效。
logging.getLogger("alembic").setLevel(logging.WARNING)

from alembic import command                       # noqa: E402
from alembic.config import Config                 # noqa: E402
from alembic.runtime.migration import MigrationContext  # noqa: E402
from alembic.script import ScriptDirectory        # noqa: E402

log = logging.getLogger("autorss")

_ROOT = Path(__file__).resolve().parent.parent
_INI = _ROOT / "alembic.ini"

# 【整个 alembic 调用必须串行化】alembic 的 EnvironmentContext 装的是**进程级模块代理**
# （alembic/op.py、alembic/context.py 的 create_module_class_proxy）——两次升级重叠时，
# 后退出的那个 `del globals_[attr]` 会删掉对方的代理。
# 而本项目真的会并发调它：schema.upgrade 有 5 个调用点，其中两个在【非事件循环线程】里
# （看守协程每 30 秒一次的 probe_data_engine、页面『立即重连』按钮），还有一个是
# run.io_bound 里的整库迁移。实测（200 次独立进程）：KeyError: 'script'、
# "index already exists"、**6% 概率整进程 Segmentation fault**，
# 以及 **4.5% 概率收尾是「alembic_version=head 而索引没建上」** —— 而 upgrade() 开头那句
# `if cur == head: return` 会让这一态【永远不会被修复】。
#
# 锁要加在这一层（而不是"把 engine 传下去"）：根因是进程级代理，不是引擎选谁——
# 用两个不同的库、两个不同的 engine 并发跑，8/8 照样复现 KeyError。
_LOCK = threading.Lock()


def _config(engine, role: str) -> Config:
    # attributes 里塞个 configure_logger=False：否则 alembic 会按 alembic.ini 的 [loggers] 段
    # 重配【全局】logging，把自己那堆 "setup plugin …" / "Context impl …" 灌进 data/autorss.log，
    # 还会顺手改掉本项目 core.logsetup 已经配好的处理器。
    cfg = Config(str(_INI), attributes={"configure_logger": False})
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    # URL 与 role 走 -x 参数注入：alembic.ini 里不写连接串（业务库是可切换的，写死没意义），
    # 且 render_as_string 要带密码，否则 MySQL 连不上。
    cfg.cmd_opts = type("O", (), {"x": [f"role={role}",
                                        f"url={engine.url.render_as_string(hide_password=False)}"]})()
    return cfg


def current_revision(engine, role: str) -> str | None:
    """库当前所处的版本号（全新库返回 None）。"""
    with engine.connect() as conn:
        return MigrationContext.configure(
            conn, opts={"version_table": f"alembic_version_{role}"}).get_current_revision()


def head_revision() -> str | None:
    """代码里最新的版本号。"""
    return ScriptDirectory(str(_ROOT / "alembic")).get_current_head()


def upgrade(engine, role: str, target: str = "head") -> None:
    """把 engine 升到 target（默认最新；已经在那一版则什么都不做）。全新库会一路建过去。

    target 不是给生产用的——生产恒是 "head"。它存在是为了让测试能造出
    **真正的旧库**（升到某个中间 revision 就停），而不是"建到 head 再把版本号改回去"：
    后者的表结构其实是新的，测不出"老结构 + 新代码"这一类问题。
    而本项目的缺陷有相当比例只在存量库的升级路径上出现（见 docs/audit-2026-08-r9.md §5）。
    """
    with _LOCK:      # 连"检查当前版本"一起包住：否则两个线程可能都读到旧版本再各跑一遍
        cur = current_revision(engine, role)
        if cur == (head_revision() if target == "head" else target):
            return
        command.upgrade(_config(engine, role), target)
        log.info("数据库版本升级[%s]：%s → %s", role, cur or "空库", current_revision(engine, role))


def stamp_head(engine, role: str) -> None:
    """只打版本戳、不执行任何 DDL。给"表已经建好了但没有版本记录"的库补票用。"""
    with _LOCK:      # 同 upgrade：alembic 的模块代理是进程级的，不能有两个同时在跑
        command.stamp(_config(engine, role), "head")
