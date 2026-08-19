"""Alembic 运行环境 —— 适配本项目的【双引擎】布局。

两个引擎各自独立地跑同一条版本链，各有各的 alembic_version 表：

    role=meta  → 本地 SQLite，只管 setting 表（配置恒在本地，不随业务库走）
    role=data  → 业务库（SQLite 或 MySQL），管其余 6 张业务表

同一个 revision 在两边都会被执行，脚本内部按 role 决定自己要做哪部分（见 versions/ 下的脚本）。
版本表也按 role 分名，免得两个引擎指向同一个 SQLite 文件时（默认布局就是如此）互相覆盖版本号。

url 与 role 由 db/schema.py 通过 -x 参数注入；直接用 CLI 时也得自己带上，例如：
    alembic -x role=data -x url=sqlite:///data/autorss.db upgrade head
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

import db.models  # noqa: F401  注册表结构
from db import META_TABLES
from db.dialect import adapt_metadata

config = context.config
# 只有从 CLI 跑时才让 alembic 配 logging。程序内调用（db/schema.py）会传
# configure_logger=False —— 否则它会重配全局 logging，把 "setup plugin …" 之类
# 灌进 data/autorss.log，还会覆盖掉 core.logsetup 已经装好的处理器。
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

# 必须先定型再拿 metadata：无长度 VARCHAR 在 MySQL 上根本编译不出 DDL，
# DateTime 精度与 BIGINT 也靠它（见 db/dialect.py）。autogenerate 比对的也得是定型后的模型。
adapt_metadata(SQLModel.metadata)
target_metadata = SQLModel.metadata

_x = context.get_x_argument(as_dictionary=True)
ROLE = _x.get("role", "data")
VERSION_TABLE = f"alembic_version_{ROLE}"


def _url() -> str:
    return _x.get("url") or config.get_main_option("sqlalchemy.url") or ""


def include_object(obj, name, type_, reflected, compare_to):
    """只让本 role 该管的表进入比对与建表范围。"""
    if type_ == "table":
        # 【两条版本链的版本表要互相排除】meta 与 data 各有一张 alembic_version_*，
        # 而它们【不在】任何一个 role 的 metadata 里。不排除的话，按 db/schema.py 文档化的流程
        # 跑 autogenerate 会生成 `op.drop_table('alembic_version_meta')` —— 应用之后第二次启动
        # 就会因为版本号丢失而重跑 baseline，撞上 "table setting already exists" 变成永久 fatal。
        if name.startswith("alembic_version"):
            return False
        return (name in META_TABLES) is (ROLE == "meta")
    if type_ == "index" and name and name.endswith("_inflight"):
        # in-flight 的 partial index 是 db/dialect.py 按方言手工建的（带 WHERE 子句），
        # 不在 SQLModel 的 metadata 里；不排除会被 autogenerate 当成"多余索引"生成 drop_index。
        return False
    return True


def _configure(connection=None, url=None):
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        include_object=include_object,
        version_table=VERSION_TABLE,
        # SQLite 不支持大多数 ALTER（改列类型/加约束都得重建表），batch 模式让
        # 后续 op.alter_column 之类在 SQLite 上自动走"建新表→拷数据→换名"。
        render_as_batch=True,
        compare_type=True,
        literal_binds=url is not None,
        dialect_opts={"paramstyle": "named"} if url is not None else {},
    )


def run_migrations_offline() -> None:
    _configure(url=_url())
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _url()
    connectable = config.attributes.get("connection", None)
    if connectable is None:
        connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
