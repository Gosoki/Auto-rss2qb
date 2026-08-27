# autorss

动漫 RSS 自动下载器（重写版）。抓 nyaa 上 ANi 的全量订阅 → 按种子 hash 去重 →
加进 qBittorrent，并提供一个 Web 面板做番剧管理 / 查进度 / 手动补下。
接入 **Mikan 发现非 ANi 番**（人工确认）、**Bangumi 识别**（真实放送日→季度、规范番名），
以及 **剧场版/OVA 按季度发现**（Mikan 季度页发现 + bgm 识别，`/movies` 审批下载）
和 **qBittorrent 实时状态**（下载进度/速度/做种态回显）。

技术栈：**Python + NiceGUI（界面）+ FastAPI（内核，NiceGUI 自带）+ SQLite/SQLModel + asyncio 轮询器**。

## 运行（本地开发）

```bash
pip install -r requirements.txt
cp .env.example .env      # 可选：只放 Web 端口等结构项
python main.py            # 浏览器打开 http://<host>:2333
```

需要 **Python ≥ 3.11**（网络层用了 3.11 才有的 `asyncio.timeout`——所有抓取/识别/取种都靠它做总超时；
3.10 上界面能开，但采集、bgm 识别、下载会全部静默失败）。`deploy.sh` 走 uv 独立 Python 3.12，不受系统版本影响。

qB 账号、下载目录、代理、面板显示等设置都在启动后的 Web「设置」/「源管理」页里填（存数据库、即时生效）。
一条命令同时跑：后台轮询下载 + Web 面板。数据库在 `data/autorss.db`（首次自动建，加字段会自动迁移，并写入默认设置）。

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
> 填成你的网段（如 `192.168.1.0/24`）收窄访问——该项走数据库、改完即时生效，回环地址恒放行不会把自己锁在外面。
> 要暴露到公网请在前面套反向代理做鉴权（此时白名单应留空，因为对端 IP 会变成代理）。

## 结构

**TV 番剧与剧场版/OVA 分表、分模块、互不相干,只共用 `engine` 底层。**

| 文件 | 职责 |
| --- | --- |
根目录只留入口 + 基础层，逻辑收进 `core/`，外部客户端/源/界面各成一包：

| 路径 | 职责 |
| --- | --- |
| `main.py` | 入口 |
| `config.py` | 配置：默认值硬编码，建库时写入数据库 `settings` 表；运行时读、设置页可改（`.env` 只留 WEB_PORT/DB_PATH 结构项） |
| `db/` | 数据层：`__init__`(SQLite/WAL 引擎 + 会话 + 开发期加列自动迁移) / `models`(数据模型:`Setting`/`SourceGroup`;TV=`Anime`+`AnimeTorrent`+`AnimeAlias`;剧场版=`Movie`+`MovieTorrent`) |
| `core/` | **核心逻辑**：`engine`(TV/剧场版共用底层:qB客户端+实时态同步+下载原语+bgm落库+路径季度) / `anime`(TV 番剧线) / `movies`(剧场版/OVA 线,对 anime 零依赖) / `worker`(后台三协程:采集/qB同步/剧场版扫描) |
| `core/ssrf.py` | 出站请求的内网守卫：取种【每一跳都判】，其余出站【首跳与同主机跳转放行、其余跳判】（config.http_client_kwargs 默认装上） |
| `services/` | 外部服务客户端：`enrich`(bgm 识别) / `qbittorrent`(qB 客户端) / `notify`(通知推送) |
| `sources/` | 订阅源：`base`(`ParsedItem` + `RssSource` —— 取回/校验/过滤/解析/构造【全部】共用逻辑) / `parse`(标题季度解析) / `nyaa`、`mikan`(各只剩 `site`/`TZ`/`_hash_of`/`_url_of` 四个覆写点) / `__init__` 的 `SOURCES` 表(站点→源类，加源只改这一处) |
| `pages/` | NiceGUI 界面：`anime`(番剧主页 tab:仪表盘/番剧表/待确认/待识别/已忽略/订阅源) / `anime_detail`(番剧详情组件,渲染进悬浮框) / `movies`(剧场版 tab:仪表盘/列表/待识别/已忽略/订阅源) / `parse_test`(解析测试页 `/parse`) / `settings` / `sources` / `layout` |

## 文档

| 文件 | 内容 |
| --- | --- |
| [docs/audit-2026-08.md](docs/audit-2026-08.md) | 第 1 轮审计：已修缺陷、未修清单、插件架构裁决、命名待判项、**需要拍板的 9 个问题** |
| [docs/audit-2026-08-r2.md](docs/audit-2026-08-r2.md) | 第 2 轮审计：**第 1 轮引入的 2 个 P0 回归**及修法、上轮盲区（手动线/白名单/Alembic/qB 客户端/取回层）、**新增 8 个待拍板** |
| [docs/audit-2026-08-r3.md](docs/audit-2026-08-r3.md) | 第 3 轮审计：性能/备份/取回层三块新东西的成色（**备份模块自己带着 2 条 critical 上线**）、测试套件自身的质量问题、**新增 9 个待拍板** |
| [docs/audit-2026-08-r4.md](docs/audit-2026-08-r4.md) | 第 4 轮审计：ani-rss「三件套」落地 + 它引入的 3 条 P0（归纳成两条结构性根因）、完结判据的风险专节 |
| [docs/audit-2026-08-r5.md](docs/audit-2026-08-r5.md) | 第 5 轮（收官）：交付状态、五轮修复的交叉验证、**这五轮的复盘**（为什么每轮的改动都要靠下一轮才发现 P0） |
| [docs/audit-2026-08-r6.md](docs/audit-2026-08-r6.md) | 第 6 轮：**MySQL 四条缺陷在真库上修复并验证**、`sources/` 重复合并（前提更正：本项目无插件系统） |
| [docs/audit-2026-08-r7.md](docs/audit-2026-08-r7.md) | 第 7 轮：第 6 轮那三块改动各自引入的回归（含**一条就写在自己警示注释下面 3 行**的）、**ani-rss 对标收官** |
| [docs/GLOSSARY.md](docs/GLOSSARY.md) | **术语表**：两条线哪些用词分歧是有意的、哪些字段容易读反 |
| [docs/audit-2026-08-r8.md](docs/audit-2026-08-r8.md) | 第 8 轮（收官）：**DECISIONS 24 条全部清空**、三条「比描述要大」的改动（SSRF 同主机放行 / 唯一约束差点变成每轮崩 / batch 迁移静默失效） |
| [docs/audit-2026-08-r9.md](docs/audit-2026-08-r9.md) | 第 9 轮（回顾）：45 agent 对抗复查上一轮的 12 项改动——**没有一项做错，漏的全是覆盖面**；归纳出一种新病「约束的作用域比验证的作用域大」 |
| [docs/audit-2026-08-r10.md](docs/audit-2026-08-r10.md) | 第 10 轮：**仓库 HEAD 曾经起不来**、双引擎设计评估（结论：骨架都对，不要重构）、4 条运行时缺陷、**3 条守卫回退掉照样全绿** |
| [docs/audit-2026-08-r11.md](docs/audit-2026-08-r11.md) | 第 11 轮：**上一轮的修复引入了一个 P0**（全括号标题的首块被当番名）、通知的三条缺陷、以及「已应用的 revision 不可变」这条教训 |
| [docs/audit-2026-08-r12.md](docs/audit-2026-08-r12.md) | 第 12 轮（收官）：**又一条 P0 来自上一轮的修复**（搜索词塌成季名）、alembic 并发实测 4/6 留下「版本号到了而 DDL 没跑」+ 2/6 段错误、设计语言第二层落地 |
| [docs/audit-2026-08-r13.md](docs/audit-2026-08-r13.md) | 第 13 轮：停机时间被当成「种子停滞」、missingFiles 行被中间态吃掉三道闸、三条凭据泄露、以及**一条我自己刚写下的假用例** |
| **[docs/DECISIONS.md](docs/DECISIONS.md)** | 拍板清单（**24/24 已全部实施**）——保留每条的**为什么**，回退时要用 |
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
- **下不下 = `confirmed` 且未 `rejected`**：自动源默认确认、待确认源留人工确认；剧场版/OVA 一律人工逐版本点下（独立 `Movie` 表，与番剧分离）。
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

交给 qB 的种子，后台每 `QB_SYNC_INTERVAL` 秒回拉一次实时态（下载中/做种/进度/速度），
在仪表盘「种子状态」和详情页逐集回显；进度到 100% 的把应用侧状态收敛为「已下」。

## 路线

- **P1**：ANi 单源 + Web 面板（番剧开关 / 待确认 / 手动补下 / 进度）。✅
- **P2**：Mikan 发现非 ANi 番 + 人工确认队列。✅
- **P3**：Bangumi 识别（真实放送日→季度、规范番名、类型）。✅
- **P4**：剧场版/OVA 按季度发现（Mikan + bgm）+ qBittorrent 实时状态回显。✅
