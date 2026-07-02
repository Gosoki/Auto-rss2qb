"""命令行查库 / 管理工具。

    python manage.py stats              概览：番数、开关状态、已下/待下集数
    python manage.py anime              已登记番剧（季度 / 是否下载 / 番名）
    python manage.py progress           每部番的进度（已下 / 总集数）
    python manage.py pending [N]        待下载的集（默认全部，可给上限 N）
    python manage.py on  <番名>         开启某番自动下载
    python manage.py off <番名>         关闭某番自动下载（选择不下这部）
    python manage.py get <torrent_id>   手动下载某个种子（忽略开关）

以后要做 UI，直接复用 repo.py 里的这些函数即可。
"""
import sys

import repo

USAGE = __doc__


def cmd_stats(args):
    total, enabled, done, pending = repo.stats()
    print(f"登记番剧: {total} 部（自动下载开启 {enabled} 部）")
    print(f"剧集: 已下 {done} 集，待下 {pending} 集")


def cmd_anime(args):
    rows = repo.list_anime()
    if not rows:
        print("（无记录）")
        return
    for quarter, season, name, if_down in rows:
        flag = "✓下载" if int(if_down) == 1 else "✗跳过"
        print(f"{quarter}  {flag}  {name}  第{season}季")


def cmd_progress(args):
    rows = repo.episode_counts()
    if not rows:
        print("（无记录）")
        return
    for title, downloaded, total in rows:
        print(f"{int(downloaded)}/{int(total)}  {title}")


def cmd_pending(args):
    limit = int(args[0]) if args else None
    rows = repo.pending(limit)
    if not rows:
        print("（没有待下载的集）")
        return
    for torrent_id, _from, title, episode, season, release_time in rows:
        print(f"{release_time}  {title}  第{season}季 第{episode}集  [{torrent_id}]")
    print(f"共 {len(rows)} 集待下")


def cmd_on(args):
    _set_download(args, True)


def cmd_off(args):
    _set_download(args, False)


def _set_download(args, enabled):
    if not args:
        print("请提供番名，如：python manage.py off 我推的孩子")
        return
    name = args[0]
    repo.set_download(name, enabled)
    print(f"{'已开启' if enabled else '已关闭'}自动下载: {name}")


def cmd_get(args):
    if not args:
        print("请提供 torrent_id，如：python manage.py get 1789456")
        return
    torrent_id = args[0]
    row = repo.get_torrent(torrent_id)
    if not row:
        print(f"未找到种子记录: {torrent_id}")
        return
    tid, torrent_from, title, episode, season = row
    import main  # 延迟导入：用到时才初始化 qBittorrent 客户端
    main.download(tid, torrent_from, title, episode, season, force=True)


COMMANDS = {
    "stats": cmd_stats,
    "anime": cmd_anime,
    "progress": cmd_progress,
    "pending": cmd_pending,
    "on": cmd_on,
    "off": cmd_off,
    "get": cmd_get,
}


def main_cli():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(USAGE)
        return
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main_cli()
