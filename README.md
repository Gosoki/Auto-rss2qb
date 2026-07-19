# autorss

动漫 RSS 自动下载器（重写版）。抓 nyaa 上 ANi 的全量订阅 → 按种子 hash 去重 →
加进 qBittorrent，并提供一个 Web 面板做番剧管理 / 查进度 / 手动补下。
可选接入 **Mikan 发现非 ANi 番**（人工确认）和 **Bangumi 富集**（真实放送日→季度、规范番名）。

技术栈：**Python + NiceGUI（界面）+ FastAPI（内核，NiceGUI 自带）+ SQLite/SQLModel + asyncio 轮询器**。

## 运行

```bash
pip install -r requirements.txt
cp .env.example .env      # 填 qB 账号等
python main.py            # 浏览器打开 http://<host>:8080
```

一条命令同时跑：后台轮询下载 + Web 面板。数据库在 `data/autorss.db`（首次自动建，加字段会自动迁移）。

## 结构

| 文件 | 职责 |
| --- | --- |
| `config.py` | 从 `.env` 读配置 |
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

## 多源与富集（默认关，按需开）

在 `.env` 里：

```bash
# Mikan 发现非 ANi 番（发现的番默认不下，进 Web 面板"待确认"等你放行）
MIKAN_ENABLED=true
# 只保留这些字幕组的发现（逗号分隔，留空=全部；用于压 Mikan 全站噪声）
MIKAN_SUBGROUPS=Nekomoe kissaten,Lilith-Raws

# Bangumi 富集（用真实放送日定季度 + 规范番名；每部番自动做一次，失败可在 UI 手动重来）
ENRICH_ENABLED=true
```

- **发现流**：Mikan 抓到非 ANi 番 → 面板"待确认新番"里出现 → 你点「确认下载」放行或「拒绝」。
- **富集**：开了之后，新番登记时自动 hash→Mikan→bgm 拿真实放送日，季度更准，并显示规范名 + bgm 链接。失败不影响下载（退回集数倒推的季度）。

## 路线

- **P1**：ANi 单源 + Web 面板（番剧开关 / 待确认 / 手动补下 / 进度）。✅
- **P2**：Mikan 发现非 ANi 番 + 人工确认队列。✅
- **P3**：Bangumi 富集（真实放送日→季度、规范番名），可选旁路。✅
