# auto-rss 番剧自动下载

抓取 RSS 订阅（当前为 nyaa 上的 ANi），把新番/新集入库（SQLite 或 MySQL），并把
种子加入 qBittorrent 下载，同时可选地推送通知。

## 运行

```bash
pip install -r requirements.txt
cp .env.example .env      # 首次使用，然后填入自己的配置
python auto-rss.py        # 或 python main.py
```

程序会常驻运行：启动时先补下数据库里未下载的种子，之后每 `STOP_TIME` 秒抓取一次订阅。

默认用 **SQLite**（本地文件、自动建表、无需装 pymysql），第一次运行会在脚本目录生成
`autorss.db`。想用 MySQL 就在 `.env` 把 `DB_TYPE=mysql` 并填好 `MYSQL_*`（建表见 `schema.sql`）。

## 配置

所有可变项都在 `.env`（模板见 `.env.example`），改配置不需要动代码。
关键项：`DB_TYPE`（sqlite/mysql）、qBittorrent 账号、`DOWN_PATH` 保存路径、
`STOP_TIME` 抓取间隔、`NOTIFY_URL` 通知地址（留空即关闭通知）、`OPEN_PROXY` 是否走代理。

## 代码结构

| 文件 | 职责 |
| --- | --- |
| `config.py` | 从 `.env` 读取全部配置 |
| `logger.py` | 日志（按天写文件） |
| `notify.py` | robot 通知推送（可选功能，`NOTIFY_URL` 留空即关闭） |
| `db.py` | 数据库连接与参数化执行，**SQLite / MySQL 双后端**（通用 `insert/update/query`，SQL 用 `?` 占位符） |
| `repo.py` | 数据访问层：**所有业务 SQL 集中在这里**（查/改数据看这个文件） |
| `qbittorrent.py` | qBittorrent Web UI 客户端 |
| `rss.py` | RSS 订阅源与标题/季数/集数解析（**新增源在这里**） |
| `main.py` | 主流程：抓取 → 登记 → 入库 → 下载 |
| `manage.py` | 命令行查库/管理工具（看进度、开关某番、手动下载） |
| `auto-rss.py` | 入口壳，调用 `main.run()` |
| `smoke_test.py` | 本地烟雾测试，不连真实 qB/MySQL，验证整体流程 |
| `migrate_mysql_to_sqlite.py` | 一次性迁移脚本：旧 MySQL → 新 SQLite |
| `schema.sql` | 参考建表语句（SQLite 自动建表，此文件给 MySQL 用） |

`db.py` 只负责“怎么连、怎么安全执行”；`repo.py` 负责“查什么、改什么”。
想调整或新增一条查询，改 `repo.py` 即可，`main.py` / `manage.py` / 以后的 UI 都复用它。

数据库三张表（SQLite 首启自动建）：

- `anime(anime_name, if_down)`：一部番要不要下载（订阅开关，按番名整体控制）。
- `anime_season(anime_name, season, quarter)`：某番的**某一季**属于哪个季度 —— 决定下载文件夹。
  季度绑在 (番, 季) 上，所以同名番的第二季会进它自己的季度，不会和第一季混。
- `rss_torrent(torrent_url, rss_group, anime_title, number_of_words, status, season, release_time, torrent_from)`：
  每一集的种子记录，`status` 0=未下载 1=已加入 qB。

**登记跟集数无关**：第一次见到某 `(番, 季)` 就登记它（用当集发布时间定季度），
所以第 0 话、特别篇、第二季从第 15 话开始，都能被正确登记并下载。

## 查库 / 管理（manage.py）

不用手写 SQL，命令行就能看数据、控制下载：

```bash
python manage.py stats              # 概览：番数、开关状态、已下/待下集数
python manage.py anime              # 已登记番剧（季度 / 是否下载 / 番名）
python manage.py progress           # 每部番进度（已下 / 总集数）
python manage.py pending [N]        # 待下载的集（可选上限 N）
python manage.py off <番名>         # 不再自动下这部番（if_down=0）
python manage.py on  <番名>         # 恢复自动下载
python manage.py get <torrent_id>   # 手动下载某个种子（忽略开关）
```

这些命令背后都是 `repo.py` 里的函数，以后做 UI 直接调同一批函数即可。

## 从旧 MySQL 迁移到 SQLite

在能连到 MySQL 的内网机器上跑一次（`DB_TYPE` 保持 sqlite）：

```bash
python migrate_mysql_to_sqlite.py
```

它会把旧库导入新的 SQLite：每个 (番, 季) 的季度按该季最早一集重新推算，
种子的已下/未下状态原样保留，避免迁完又重下一遍。建议先在空 SQLite 上跑。

## 本地验证

改完解析或新增源后，先跑烟雾测试确认流程通畅（不会写库、不会加种子、不会发通知）：

```bash
python smoke_test.py
```

## 新增一个 RSS 源

`rss.py` 已内置一个例子：`AniSource` 和 `LilithSource` 都继承自 `NyaaSource`，
因为它们的标题格式一致，各自只多了三行属性。想启用 Lilith-Raws，把 `rss.py`
末尾 `# register(LilithSource())` 的注释去掉即可。

新增一个 **同为 nyaa 格式** 的上传者：

```python
class MySource(NyaaSource):
    name = "示例"
    rss_group = "MINE"
    RSS_URL = "https://nyaa.si/?page=rss&u=SomeUploader"

register(MySource())
```

新增一个 **完全不同站点** 的源：继承 `RssSource`，自己实现
`download_url()` 和 `fetch_items()`（解析时可复用 `extract_season` /
`extract_episode` / `extract_quarter`），返回 `List[RssItem]` 即可。

```python
class OtherSiteSource(RssSource):
    name = "别的站"
    rss_group = "OTHER"
    torrent_from = "othersite"   # 决定下载地址拼法

    def download_url(self, torrent_id):
        return f"https://othersite/download/{torrent_id}.torrent"

    def fetch_items(self):
        ...   # 抓取 + 解析，返回 List[RssItem]

register(OtherSiteSource())
```

`RssItem` 是标准化后的条目（`torrent_id / anime_title / episode / season /
release_time / quarter / rss_group / torrent_from`），主流程只认这个结构，
因此不同源的解析差异都被隔离在各自的 `fetch_items()` 里。
