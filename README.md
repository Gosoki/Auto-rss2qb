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
| `services/` | 外部服务客户端：`enrich`(bgm 识别) / `qbittorrent`(qB 客户端) / `notify`(通知推送) |
| `sources/` | 订阅源：`base`(源基类+`ParsedItem`) / `parse`(标题季度解析) / `nyaa` / `mikan`(Classic RSS 源 + 季度剧场版/OVA 发现) |
| `pages/` | NiceGUI 界面：`anime`(番剧主页 tab:仪表盘/番剧表/待确认/待识别/已忽略/订阅源) / `anime_detail`(番剧详情组件,渲染进悬浮框) / `movies`(剧场版 tab:仪表盘/列表/待识别/已忽略/订阅源) / `parse_test`(解析测试页 `/parse`) / `settings` / `sources` / `layout` |

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
