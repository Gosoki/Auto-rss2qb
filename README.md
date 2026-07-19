# autorss

动漫 RSS 自动下载器（重写版）。抓 nyaa 上 ANi 的全量订阅 → 按种子 hash 去重 →
加进 qBittorrent，并提供一个 Web 面板做番剧管理 / 查进度 / 手动补下。

技术栈：**Python + NiceGUI（界面）+ FastAPI（内核，NiceGUI 自带）+ SQLite/SQLModel + asyncio 轮询器**。

## 运行

```bash
pip install -r requirements.txt
cp .env.example .env      # 填 qB 账号等
python main.py            # 浏览器打开 http://<host>:8080
```

一条命令同时跑：后台轮询下载 + Web 面板。数据库在 `data/autorss.db`（首次自动建）。

## 结构

| 文件 | 职责 |
| --- | --- |
| `config.py` | 从 `.env` 读配置 |
| `db.py` | SQLite + SQLModel 引擎（WAL） |
| `models.py` | 数据模型：`Anime`（番剧管理单位）/ `Torrent`（按 info_hash 去重） |
| `sources/base.py` | 源基类 + 标准条目 `ParsedItem`（**新增源在这里继承**） |
| `sources/ani.py` | nyaa ANi 源：抓取 + 标题/季/集/季度解析 |
| `qbittorrent.py` | qBittorrent 异步客户端 |
| `core.py` | 主流程（去重·登记·下载）+ 给 UI 的查询/操作函数 |
| `worker.py` | 后台常驻轮询器 |
| `pages.py` | NiceGUI 界面 |
| `notify.py` | 可选通知推送 |
| `main.py` | 入口 |

## 设计要点

- **去重键 = 种子 info_hash**（40位hex，从 nyaa RSS 的 `<nyaa:infoHash>` 白拿）。跨源/跨站同一种子精确相等，为后续接 Mikan 打底。
- **番剧管理单位 = (标题, 季)**：`if_down` 控制是否自动下、`confirmed` 给非 ANi 来源留人工确认。
- **季度**：第一季且一个 cour 内用集数倒推首播季度，否则用当集时间（避免分割番/连续编号 S2 落错季度）。

## 路线

- **P1（当前）**：ANi 单源跑通 + Web 面板（番剧开关 / 待确认 / 手动补下 / 进度）。
- **P2**：接 Mikan 发现非 ANi 番、非 ANi 走人工确认队列。
- **P3**：Bangumi 富集（真实放送日→季度、规范番名），做成可选旁路。
