"""烟雾测试：不连真实 qBittorrent / MySQL，跑通 抓取 -> 入库 -> 下载 全流程。

- qb.add_torrent 恒返回 True（忽略 qB 结果）
- 数据库用内存假实现（忽略 DB 结果）
- 通知与种子下载都被替换为打印，不真的发请求
缺少第三方库时会自动打最小桩，因此在开发机上也能直接：`python smoke_test.py`

用途：改完解析逻辑或新增 RSS 源后，本地快速确认整体流程仍然顺畅。
"""
import os
import sys
import types


def _stub(name, attrs=None):
    """第三方库缺失时用最小桩顶替；已安装则用真库。"""
    try:
        return __import__(name)
    except ImportError:
        module = types.ModuleType(name)
        for key, value in (attrs or {}).items():
            setattr(module, key, value)
        sys.modules[name] = module
        return module


def _load_env(path=None, *args, **kwargs):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


class _OpenCC:
    def __init__(self, *_):
        pass

    def convert(self, text):
        return text  # 桩环境不做简繁转换


_stub("requests", {"RequestException": Exception})
_stub("feedparser")
_stub("opencc", {"OpenCC": _OpenCC})
_stub("pymysql", {
    "connect": lambda *a, **k: None,
    "err": types.SimpleNamespace(IntegrityError=type("IntegrityError", (Exception,), {})),
})
_stub("dotenv", {"load_dotenv": _load_env})

import db      # noqa: E402
import logger  # noqa: E402
import main    # noqa: E402
import repo    # noqa: E402
import rss     # noqa: E402


# ---- 把会碰真实资源的地方全部替换掉 ----
class FakeDB:
    """内存假库：insert/update 只打印，query 按 SQL 关键字返回假数据。"""

    def __init__(self):
        self._seen = set()

    def insert(self, sql, params=None):
        if "rss_torrent" in sql:
            key = ("torrent", params[0])
        elif "anime_season" in sql:
            key = ("season", (params[0], params[1]))
        else:  # anime
            key = ("anime", params[0])
        if key in self._seen:
            print(f"    [db] 已存在，跳过: {params}")
            return False
        self._seen.add(key)
        print(f"    [db] 写入: {params}")
        return True

    def update(self, sql, params=None):
        print(f"    [db] 更新: {params}")
        return True

    def query(self, sql, params=None):
        # 按 SQL 特征返回对应形状的假数据（测试替身，和 repo.py 里的查询一一对应）
        if sql.strip().startswith("SELECT (SELECT COUNT"):        # repo.stats
            return [(5, 4, 12, 3)]
        if "SUM(status = 1)" in sql:                              # repo.episode_counts
            return [("我推的孩子", 7, 12), ("怪兽8号", 1, 12)]
        if "JOIN anime a" in sql:                                 # repo.list_anime
            return [("24C", 2, "我推的孩子", 1), ("24B", 1, "怪兽8号", 0)]
        if "SELECT quarter FROM anime_season" in sql:             # repo.get_quarter
            return [("24C",)]
        if "SELECT if_down FROM anime" in sql:                    # repo.is_subscribed
            return [(1,)]
        if "FROM rss_torrent WHERE status = 0" in sql:            # repo.pending
            return [("1801234", "nyaa", "怪兽8号", 1, 1, "2024-04-13 10:00:00")]
        if "WHERE torrent_url = ?" in sql:                        # repo.get_torrent
            return [("1801234", "nyaa", "怪兽8号", 1, 1)]
        return []


class _Resp:
    status_code = 200
    content = b"fake-torrent-bytes"

    def raise_for_status(self):
        pass


# 关键：patch db._db 单例（repo/main 都通过 db.get_db() 拿它），而不是各模块里的引用
db._db = FakeDB()
main.qb.add_torrent = lambda save_path, *a, **k: (print(f"    [qb] 加入下载: {save_path}") or True)
main.notify = lambda msg: print(f"    [notify] {msg}")
logger.notify = lambda msg: print(f"    [notify] {msg}")
main.requests = types.SimpleNamespace(get=lambda *a, **k: _Resp(), RequestException=Exception)


def _entry(title, link, published):
    e = types.SimpleNamespace(title=title, link=link)
    e.get = lambda k, d=None: {"published": published, "title": title}.get(k, d)
    return e


print("=== 1) 解析验证：ANi 与 示例源 Lilith-Raws 复用同一套解析 ===")
samples = [
    (rss.AniSource(), _entry(
        "[ANi] Oshi no Ko / 我推的孩子 第二季 - 07 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]",
        "https://nyaa.si/download/1789456.torrent",
        "Wed, 10 Jul 2024 12:30:00 +0000")),
    (rss.LilithSource(), _entry(
        "[Lilith-Raws] Kaijuu 8-gou / 怪兽8号 - 01 [Baha][WEB-DL][1080p][AVC AAC][CHT][MP4]",
        "https://nyaa.si/download/1801234.torrent",
        "Sat, 13 Apr 2024 10:00:00 +0000")),
]
items = []
for source, entry in samples:
    item = source._parse_entry(entry)
    items.append(item)
    print(f"    [{source.name}] {item.anime_title} | 第{item.season}季 第{item.episode}集 "
          f"| 季度{item.quarter} | from={item.torrent_from}")
assert rss.extract_episode("[ANi] X / 某番 - 11.5 [1080P][Baha][CHT]") == 11.5
print("    (.5 集解析 OK：11.5)")


print("\n=== 2) 全流程：入库 -> 下载（qB / DB / 通知 结果均被忽略）===")


class MockSource(rss.RssSource):
    name = "mock"

    def fetch_items(self):
        return items


print("-- 第一次运行（全部为新）--")
main.process_source(MockSource())
print("-- 第二次运行（全部已存在，应进入休眠）--")
main.process_source(MockSource())


print("\n=== 3) 命令行工具 manage.py 演示（数据为假，仅验证输出）===")
import manage  # noqa: E402

for label, func, cmd_args in [
    ("stats", manage.cmd_stats, []),
    ("anime", manage.cmd_anime, []),
    ("progress", manage.cmd_progress, []),
    ("pending", manage.cmd_pending, []),
    ("off 我推的孩子", manage.cmd_off, ["我推的孩子"]),
    ("get 1801234", manage.cmd_get, ["1801234"]),
]:
    print(f"$ python manage.py {label}")
    func(cmd_args)
    print()

print("烟雾测试结束（未触碰真实 qB / MySQL / 通知）")
