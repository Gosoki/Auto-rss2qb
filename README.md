# autorss

动漫 RSS 自动下载器（重写版）。抓 nyaa 上 ANi 的全量订阅 → 按种子 hash 去重 →
加进 qBittorrent，并提供一个 Web 面板做番剧管理 / 查进度 / 手动补下。
接入 **Mikan 发现非 ANi 番**（人工确认）、**Bangumi 识别**（真实放送日→季度、规范番名），
以及 **剧场版/OVA 按季度发现**（Mikan 季度页发现 + bgm 识别，`/movies` 审批下载）
和 **qBittorrent 实时状态**（下载进度/速度/做种态回显）。
后来陆续加了：后台巡检（完结 / 断更 / 缺集 / 锁定源落后）与推送、采集健康横幅、qB 的 IP 过滤、业务库可切 MySQL。

技术栈：**Python + NiceGUI（界面）+ FastAPI（内核，NiceGUI 自带）+ SQLite/SQLModel + asyncio 轮询器**。

## 运行（本地开发）

```bash
pip install -r requirements.txt
cp .env.example .env      # 可选：只放 Web 端口等结构项
python main.py            # 浏览器打开 http://127.0.0.1:2333（启动横幅会打印确切地址）
```

> ⚠️ **用 `127.0.0.1` 而不是 `localhost`。** `WEB_HOST` 默认 `127.0.0.1`，uvicorn 只监听
> **IPv4** 回环；而不少系统的 `localhost` 会先解析成 IPv6 的 `::1`。curl 会自动回落到 IPv4，
> **Chrome 不一定会**——它连 `[::1]:2333` 被拒之后直接显示自己的错误页，看起来就是一片白屏，
> 而后端日志里【一个字都没有】（请求根本没到）。控制台里的迹象是
> `Unsafe attempt to load URL … from frame with URL chrome-error://chromewebdata/`。
> 要让两个协议栈都能进，把 `.env` 里的 `WEB_HOST` 设成 `0.0.0.0`（⚠️ 那会对局域网开放，见下方警告）。

需要 **Python ≥ 3.11**（网络层用了 3.11 才有的 `asyncio.timeout`——所有抓取/识别/取种都靠它做总超时；
3.10 上界面能开，但采集、bgm 识别、下载会全部静默失败）。`deploy.sh` 走 uv 独立 Python 3.12，不受系统版本影响。

qB 账号、下载目录、代理、面板显示等设置都在启动后的 Web『全局设置』页里填（存数据库、即时生效）；源组在番剧页的『订阅源』标签里配。
一条命令同时跑：后台轮询下载 + Web 面板。数据库在 `data/autorss.db`（首次自动建并写入默认设置；建表与升级走 Alembic，启动时自动迁移）。

## 部署（Debian/Ubuntu 服务器、PVE LXC）

```bash
apt update && apt install -y git curl ca-certificates
git clone <本仓库> && cd <仓库目录>
bash deploy.sh            # root 运行
```

`deploy.sh` 自动完成：装 [uv](https://docs.astral.sh/uv/)（自带独立 Python，**不依赖系统 python**，
Debian 11 的 3.9 也无所谓）→ 建 `.venv` 装依赖 → 写 `.env`（`WEB_HOST=0.0.0.0`）
→ 生成 systemd 服务并启动。路径由脚本自身位置推导，仓库克隆到哪都行；每步失败即停并报错，
**幂等可重复跑**（已有可用 `.venv` 就复用，版本不符才停服务重建），升级也是 `git pull && bash deploy.sh`。

```bash
journalctl -u autorss -f          # 看日志
systemctl restart autorss         # 重启
```

> ⚠️ **本工具没有鉴权**，设置页还存着 qB 密码。绑 `0.0.0.0` 后请进设置页把 `WEB_ALLOW_CIDRS`
> （页面上是「站点 / Web 访问 / 高级」里的『允许网段(CIDR)』）填成你的网段（如 `192.168.1.0/24`）收窄访问——该项走数据库、改完即时生效，回环地址恒放行不会把自己锁在外面。
> 要暴露到公网请在前面套反向代理做鉴权（此时白名单应留空，因为对端 IP 会变成代理）。

## 装好之后的 5 步

全新库的后台采集默认是**关着**的，按顺序在设置页 / 订阅源页填完再打开：

1. **qB 连接与下载目录**：qB 地址、账号密码、工作目录（下载根）。
2. **网段白名单**：『允许网段(CIDR)』（`WEB_ALLOW_CIDRS`）填你的网段（见上面的警告）。
3. **开始使用日**：填本季第一天之类。**空着＝不限**——默认的 ANi 是自动下载，
   第一轮会把它 feed 里的几十部番、几十 GB 全当新番下下来；早于这天开播的番判『超期忽略』，不自动下。
4. **订阅源与策略**：『订阅源』页看一遍源组，自动下载还是人工审核（先进『待确认』，你点头才下）、优先级、字幕组白名单。
5. **推送与心跳**：通知 URL 收告警与交付；外部心跳地址让机器整台停了也有人知道。

最后回设置页打开『启用后台采集』。开始使用日还空着时会再问你一次。

## 升级、回退与备份

- **升级不可回退**：数据库只往前迁移。库比代码新时程序拒绝启动，所以回滚代码前要先有一份旧库备份。业务库是本地 SQLite 时，真要迁移的那次启动会先自动留一份（备份列表里标『升级前』）；用 MySQL 的请自己先备份。
- **备份与恢复**：设置页『备份』一栏，恢复步骤在那里的 ⓘ 说明里（只记这一份，别处不抄）。

## 测试

`.venv/bin/python -m pytest`（零网络）；分组、夹具与注意事项见 [tests/README.md](tests/README.md)。

## 结构

**TV 番剧与剧场版/OVA 分表、分模块、互不相干,只共用 `engine` 底层。**

根目录只留入口 + 基础层，逻辑收进 `core/`，外部客户端/源/界面各成一包：

| 路径 | 职责 |
| --- | --- |
| `main.py` | 入口 |
| `config.py` | 配置：默认值硬编码，建库时写入数据库 `settings` 表；运行时读、设置页可改（`.env` 只留 WEB_HOST/WEB_PORT/DB_PATH 结构项） |
| `db/` | 数据层：`__init__`(SQLite/WAL 引擎 + 会话) / `schema`(建表与升级走 Alembic，迁移脚本在 `alembic/`) / `models`(数据模型:`Setting`/`SourceGroup`/`AlertAck`;TV=`Anime`+`AnimeTorrent`+`AnimeAlias`;剧场版=`Movie`+`MovieTorrent`) / `backup`(整库快照) / `transfer`(SQLite ⇄ MySQL 整体搬运) / `dialect`(跨方言适配) |
| `core/` | **核心逻辑**：`engine`(TV/剧场版共用底层:qB客户端+实时态同步+下载原语+bgm落库+路径季度) / `anime`(TV 番剧线) / `movies`(剧场版/OVA 线,对 anime 零依赖) / `worker`(后台协程:采集 / qB 同步 / 剧场版扫描 / 待识别重试 / 备份 / 巡检 / IP 过滤 / 库看守，见 `main._startup`) / `alerts`(只报不改的巡检发现与『知道了』) / `health`(采集健康) / `digest`(每日摘要) / `ipfilter`(qB 的 IP 过滤) / `netguard`(网段白名单) / `manual`(手动下载) / `logsetup`(日志) |
| `core/ssrf.py` | 出站请求的内网守卫：取种【每一跳都判】，其余出站【首跳与同主机跳转放行、其余跳判】（config.http_client_kwargs 默认装上） |
| `services/` | 外部服务客户端：`enrich`(bgm 识别) / `qbittorrent`(qB 客户端) / `notify`(通知推送) / `fetch`(带字节上限与总超时的取回) |
| `sources/` | 订阅源：`base`(`ParsedItem` + `RssSource` —— 取回/校验/过滤/解析/构造【全部】共用逻辑) / `parse`(标题季度解析) / `nyaa`、`mikan`(各只剩 `site`/`TZ`/`_hash_of`/`_url_of` 四个覆写点) / `__init__` 的 `SOURCES` 表(站点→源类，加源只改这一处) |
| `pages/` | NiceGUI 界面：`anime`(番剧主页 tab:仪表盘/番剧表/待确认/待识别/已忽略/订阅源) / `anime_detail`(番剧详情组件,渲染进悬浮框) / `movies`(剧场版 tab:仪表盘/列表/待识别/已忽略/订阅源) / `parse_test`(解析测试页 `/parse`) / `settings` / `sources` / `manual`(手动下载) / `logs`(运行日志) / `api`(qB 完成回调) / `layout` |
| `alembic/` | 数据库迁移脚本 |
| `tools/` | 命令行小工具（手动生成 IP 过滤规则） |
| `tests/` | 回归用例，见 [tests/README.md](tests/README.md) |

## 文档

| 文件 | 内容 |
| --- | --- |
| `docs/audit-*.md` | 各轮审计记录，按轮次存档（文件名带年月与轮次；第 1 轮是 [docs/audit-2026-08.md](docs/audit-2026-08.md)） |
| [docs/GLOSSARY.md](docs/GLOSSARY.md) | **术语表**：两条线哪些用词分歧是有意的、哪些字段容易读反 |
| **[docs/DECISIONS.md](docs/DECISIONS.md)** | 拍板与待拍板清单——保留每条的**为什么**，回退时要用 |
| [docs/benchmark-ani-rss.md](docs/benchmark-ani-rss.md) | 与 [ani-rss](https://github.com/wushuo894/ani-rss) 的逐维度差距分析、值得借鉴的 Top 5、明确不做的 |

## 加一个订阅源要做什么

**本项目没有插件系统**——`sources/` 就是几个普通模块，共用一个 `RssSource` 基类。加一个站：

1. 新建 `sources/<站>.py`，继承 `RssSource`，给出四样东西：
   `site`（站点标识）、`TZ`（该站 pubDate 的时区基准）、`_hash_of(entry)`、`_url_of(entry)`。
2. 在 `sources/__init__.py` 的 `SOURCES` 表里加一行；有搜索接口的再往 `SEARCH_URL` 加一行（可选，缺了只是不支持『补齐』）。

就这两步。取回、bozo 告警、40hex 校验、合集过滤、标题关键词、番名解析、多括号回退、
字幕组白名单、发布时间、季度推算、`ParsedItem` 构造、异常兜底 —— 全在基类里。
（一个例外：`sources/mikan.fetch_bangumi_torrents` 是服务于剧场版的第三条解析路径，
语义不同故未合并 —— 改基类的通用行为时记得看一眼那边。）
UI 的类型下拉、`engine` 的时区查询、`anime` 的补齐分派都从 `SOURCES` 取，不必再改。

> 这套结构是审计推着做的：合并之前那些逻辑在 nyaa 与 mikan 里各有一份逐字相同的拷贝，
> 源层的改动**有 9 次是「同一件事改两遍」**，其中最近一次只改了一半（字幕组白名单的大小写），
> 表现是那个源组每轮抓 0 条、日志里却看不出为什么。

## 设计要点

- **去重键 = 种子 info_hash**（40位hex）。nyaa 从 `<nyaa:infoHash>` 白拿，Mikan 从 `/Home/Episode/<hash>` 链接取——跨源/跨站同一种子精确相等。
- **身份 = bangumi_id**：不同组不同写法经 `AnimeAlias` 指到同一部番；未匹配 bgm 的进「待识别」人工绑定，绝不自动下。
- **下不下 = `confirmed` 且未 `rejected`**：自动源默认确认、人工审核源留人工确认；剧场版/OVA 一律人工逐版本点下（独立 `Movie` 表，与番剧分离）。
- **逻辑集去重**：同一 (番, 集) 只下一份，跨源/跨组去重（缓冲窗口内等更高优先级的源补齐，到点选优先级最高的一份）。
- **季度**：以 **Bangumi 真实放送日** 定季度（首播季）；bgm 拿不到才退回集数倒推。

## 多源与识别

源组（ANi / Mikan / 各字幕组的 feed、策略、优先级、字幕组白名单）都在 Web「订阅源」页配置，改完下一轮生效。
Bangumi 识别恒开（真实放送日定季度 + 规范番名 + 类型），无需配置。

- **发现流**：Mikan 抓到非 ANi 番 → 面板「待确认」里出现 → 你点「确认下载」放行或「忽略」。
- **识别**：登记时多名投票搜 bgm（放送日校验），拿规范名/日文名/放送日/类型；失败进「待识别」可重试或粘 bgm 链接绑定。

## 剧场版 / OVA（`/movies`）

周更番走 RSS，剧场版/OVA 不适合 RSS，改为**按季度从 Mikan 发现**：季度浏览页取「剧场版/OVA」桶
→ 每部详情拿 bgm 精确联动键 → **bgm 识别**（规范名/放送日/类型）→ 入库为独立 `Movie` 表记录并抓其种子。
在 `/movies` 页选年份/季度「扫描」，命中后逐条选版本下载（不自动下）。

## qBittorrent 实时状态

交给 qB 的种子，后台按三档节奏回拉实时态（下载中/做种/进度/速度）：有种子在活跃下载时每 `QB_SYNC_INTERVAL` 秒；
还有没下完的但都不活跃（慢 / 停滞 / 暂停）时每 `QB_IDLE_RECHECK_MIN` 分钟；全无在下时每 `QB_SYNC_BACKSTOP_MIN` 分钟保底。
在仪表盘「种子状态」和详情页逐集回显，下完的显示『已完成』（关了 qB 跟踪时发送即算下完，显示『已下』）。

## 路线

- **P1**：ANi 单源 + Web 面板（番剧开关 / 待确认 / 手动补下 / 进度）。✅
- **P2**：Mikan 发现非 ANi 番 + 人工确认队列。✅
- **P3**：Bangumi 识别（真实放送日→季度、规范番名、类型）。✅
- **P4**：剧场版/OVA 按季度发现（Mikan + bgm）+ qBittorrent 实时状态回显。✅
