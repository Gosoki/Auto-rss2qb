# autorss

动漫 RSS 自动下载器（重写版）。抓 nyaa 上 ANi 的全量订阅 → 按种子 hash 去重 →
加进 qBittorrent，并提供一个 Web 面板做番剧管理 / 查进度 / 手动补下。
接入 **Mikan 发现非 ANi 番**（人工确认）、**Bangumi 识别**（真实放送日→季度、规范番名），
以及 **剧场版/OVA 按季度发现**（Mikan 季度页发现 + bgm 识别，`/movies` 审批下载）
和 **qBittorrent 实时状态**（下载进度/速度/做种态回显）。

技术栈：**Python + NiceGUI（界面）+ FastAPI（内核，NiceGUI 自带）+ SQLite/SQLModel + asyncio 轮询器**。

## 运行

```bash
pip install -r requirements.txt
cp .env.example .env      # 可选：只放 Web 端口等结构项
python main.py            # 浏览器打开 http://<host>:8080
```

qB 账号、下载目录、代理、面板显示等设置都在启动后的 Web「设置」/「源管理」页里填（存数据库、即时生效）。
一条命令同时跑：后台轮询下载 + Web 面板。数据库在 `data/autorss.db`（首次自动建，加字段会自动迁移，并写入默认设置）。

## 结构

| 文件 | 职责 |
| --- | --- |
| `config.py` | 配置：默认值硬编码，建库时写入数据库 `settings` 表；运行时读、设置页可改（`.env` 只留 WEB_PORT/DB_PATH 结构项） |
| `db.py` | SQLite + SQLModel 引擎（WAL）+ 加列自动迁移 |
| `models.py` | 数据模型：`Setting` / `SourceGroup` / `Anime`（tv/movie）/ `TitleAlias` / `Torrent`（按 info_hash 去重，含 qB 实时态） |
| `sources/base.py` | 源基类 + 标准条目 `ParsedItem`（**新增源在这里继承**） |
| `sources/parse.py` | 共享标题/季度解析 |
| `sources/nyaa.py` | nyaa 源（一个字幕组一个实例，ANi 等） |
| `sources/mikan.py` | Mikan Classic 全站发现源（RSS，非 ANi，待确认） |
| `sources/mikan_catalog.py` | Mikan 季度浏览页发现剧场版/OVA（供 `/movies` 用） |
| `enrich.py` | Bangumi 识别：多名投票搜 bgm / hash→Mikan→bgm 兜底，取真实放送日+规范名+类型 |
| `qbittorrent.py` | qBittorrent 异步客户端（增删种子 + 查实时态） |
| `core.py` | 主流程（去重·识别·下载）+ 剧场版发现 + qB 状态同步 + 给 UI 的查询/操作 |
| `worker.py` | 后台常驻轮询器（采集 + qB 状态同步两条协程） |
| `pages/` | NiceGUI 界面：`dashboard`(主页,含仪表盘/番剧表/待确认/待识别/已忽略/订阅源) / `movies` / `detail` / `settings` / `sources` / `layout` |
| `notify.py` | 可选通知推送 |
| `main.py` | 入口 |

## 设计要点

- **去重键 = 种子 info_hash**（40位hex）。nyaa 从 `<nyaa:infoHash>` 白拿，Mikan 从 `/Home/Episode/<hash>` 链接取——跨源/跨站同一种子精确相等。
- **身份 = bangumi_id**：不同组不同写法经 `TitleAlias` 指到同一部番；未匹配 bgm 的进「待识别」人工绑定，绝不自动下。
- **下不下 = `confirmed` 且未 `rejected`**：自动源默认确认、审核源留人工确认；剧场版/OV（kind=movie）一律人工点下。
- **逻辑集去重**：同一 (番, 集) 只下一份，跨源/跨组去重（缓冲窗口内等更高优先级的源补齐，到点选优先级最高的一份）。
- **季度**：以 **Bangumi 真实放送日** 定季度（首播季）；bgm 拿不到才退回集数倒推。

## 多源与识别

源组（ANi / Mikan / 各字幕组的 feed、策略、优先级、字幕组白名单）都在 Web「订阅源」页配置，改完下一轮生效。
Bangumi 识别恒开（真实放送日定季度 + 规范番名 + 类型），无需配置。

- **发现流**：Mikan 抓到非 ANi 番 → 面板「待确认」里出现 → 你点「确认下载」放行或「忽略」。
- **识别**：登记时多名投票搜 bgm（放送日校验），拿规范名/日文名/放送日/类型；失败进「待识别」可重试或粘 bgm 链接绑定。

## 剧场版 / OVA（`/movies`）

周更番走 RSS，剧场版/OVA 不适合 RSS，改为**按季度从 Mikan 发现**：季度浏览页取「剧场版/OVA」桶
→ 每部详情拿 bgm 精确联动键 → **bgm 识别**（规范名/放送日/类型）→ 入库为 `kind=movie` 的番并抓其种子。
在 `/movies` 页选年份/季度「扫描」，命中后逐条选版本下载（不自动下）。

## qBittorrent 实时状态

交给 qB 的种子，后台每 `QB_SYNC_INTERVAL` 秒回拉一次实时态（下载中/做种/进度/速度），
在仪表盘「种子状态」和详情页逐集回显；进度到 100% 的把应用侧状态收敛为「已下」。

## 路线

- **P1**：ANi 单源 + Web 面板（番剧开关 / 待确认 / 手动补下 / 进度）。✅
- **P2**：Mikan 发现非 ANi 番 + 人工确认队列。✅
- **P3**：Bangumi 识别（真实放送日→季度、规范番名、类型）。✅
- **P4**：剧场版/OVA 按季度发现（Mikan + bgm）+ qBittorrent 实时状态回显。✅
