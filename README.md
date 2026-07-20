# autorss

动漫 RSS 自动下载器（重写版）。抓 nyaa 上 ANi 的全量订阅 → 按种子 hash 去重 →
加进 qBittorrent，并提供一个 Web 面板做番剧管理 / 查进度 / 手动补下。
可选接入 **Mikan 发现非 ANi 番**（人工确认）和 **Bangumi 富集**（真实放送日→季度、规范番名）。

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
| `models.py` | 数据模型：`Anime`（番剧管理单位）/ `Torrent`（按 info_hash 去重） |
| `sources/base.py` | 源基类 + 标准条目 `ParsedItem`（**新增源在这里继承**） |
| `sources/parse.py` | 共享标题/季度解析 |
| `sources/ani.py` | nyaa ANi 源（自动下载） |
| `sources/mikan.py` | Mikan Classic 全站发现源（非 ANi，待确认） |
| `enrich.py` | Bangumi 富集：hash→Mikan→bgm，取真实放送日+规范名 |
| `qbittorrent.py` | qBittorrent 异步客户端 |
| `core.py` | 主流程（去重·登记·富集·下载）+ 给 UI 的查询/操作 |
| `worker.py` | 后台常驻轮询器 |
| `pages.py` | NiceGUI 界面 |
| `notify.py` | 可选通知推送 |
| `main.py` | 入口 |

## 设计要点

- **去重键 = 种子 info_hash**（40位hex）。nyaa 从 `<nyaa:infoHash>` 白拿，Mikan 从 `/Home/Episode/<hash>` 链接取——跨源/跨站同一种子精确相等。
- **番剧管理单位 = (标题, 季)**：`if_down` 控制是否自动下、`confirmed` 给非 ANi 来源留人工确认。
- **逻辑集去重**：同一 (番, 季, 集) 只下一次，跨源/跨组去重（ANi 更快先占，Mikan 版自动跳过）。
- **季度**：默认第一季且一个 cour 内用集数倒推首播季度；开了富集则用 **Bangumi 真实放送日** 直接定季度。

## 多源与富集

源组（ANi / Mikan / 各字幕组的 feed、策略、优先级、字幕组白名单）都在 Web「源管理」页配置，改完下一轮生效。
Bangumi 富集恒开（用真实放送日定季度 + 规范番名），无需配置。

- **发现流**：Mikan 抓到非 ANi 番 → 面板"待确认"里出现 → 你点「确认下载」放行或「拒绝」。
- **富集**：开了之后，新番登记时自动 hash→Mikan→bgm 拿真实放送日，季度更准，并显示规范名 + bgm 链接。失败不影响下载（退回集数倒推的季度）。

## 路线

- **P1**：ANi 单源 + Web 面板（番剧开关 / 待确认 / 手动补下 / 进度）。✅
- **P2**：Mikan 发现非 ANi 番 + 人工确认队列。✅
- **P3**：Bangumi 富集（真实放送日→季度、规范番名），可选旁路。✅
