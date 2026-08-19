# 本地测试套件

**已入库**（原本按要求不上传，第 6 轮起改为入库——理由见 `docs/DECISIONS.md` 的 D-28）。跑法：

```bash
.venv/bin/python -m pytest          # 全量，约 10 秒
.venv/bin/python -m pytest tests/test_parse.py -v
```

## 约定

- **只测纯函数与内存内逻辑**：不打网络、不连 qB、不碰开发用的 `data/autorss.db`
  （`conftest.py` 在 import config 之前把 `DB_PATH` 指到临时目录）。
- **每条用例都对应一次真实修过的 bug 或一条明确的设计承诺**。标了 `(R1)` 的是
  2026-08 第 1 轮审计修的，注释里写着修前的实际错误值。
- **新修 bug 时先在这里加一行，再改代码**。本项目历史上的回归有相当比例是
  "改了一处判据、另一处手抄的同款判据没跟上"，那类问题只要有表驱动用例就能当场兜住。
- 有些用例断言的是**故意为之的怪异行为**（如 12 月归次年冬档、`not_blocked_by("")` 返回 True），
  注释里写明了"别改它"和理由——那些不是 bug，是被文档化过的口径。

## 覆盖

| 文件 | 守住什么 |
| --- | --- |
| `test_parse.py` (47) | 标题解析：集号/季号/组名/番名，含全括号命名的回退捕获。解析错 = 下错文件或整部番进"待识别" |
| `test_dedup.py` (25) | 集去重键与绝对号折算的不变量。错了 = 漏整季，或同一集下两份进同一目录 |
| `test_paths.py` (42) | 保存路径：越界穿越、跨平台分隔符、按字节截断、季度模板容错 |
| `test_select.py` (14) | 下载候选的挑选顺序与每集分组。错了 = 某集永久停滞，或同一集下两份 |
| `test_qb_sync.py` (10) | qB 实时态同步的状态机（何时才允许判死一条种子）。错了 = 整批种子被误标失败 → 自动重复下载 |
| `test_netguard.py` (29) | 网段白名单。放宽 = 无鉴权面板暴露给内网；收紧 = 用户被挡在自己的面板外 |
| `test_engine_lifecycle.py` (25) | 完成回调、归档前置条件、跨表同 hash |
| `test_status_tables.py` (26) | 状态词表的分层与一致性。本项目自己立过「同一份知识记两处会漂移」的规矩，这组把它变成可执行断言 |
| `test_qb_client.py` (11) | qB 客户端的三态返回（受理/被拒/连不上）。揉成两态会让 qB 掉线时整批种子被打成 error |
| `test_plan_equivalence.py` | 批量/逐番/合并三种计划口径必须逐字相等；含一条把「confirmed 闸在调用方」这个**契约**写死的用例 |
| `test_poll_dedup.py` | 采集轮的批量查重（预取集合的盲区：预取之后才入库的 hash） |
| `test_backup.py` | 备份：WAL 快照、自检、保留策略、并发、残骸清理 |
| `test_fetch_caps.py` | 取回层上限：压缩炸弹的**内存峰值**、多值 Content-Encoding、解不开的编码要报错而非返回二进制垃圾 |
| `test_notify_style.py` | toast 前景色必须写驼峰 `textColor`（蛇形会被 Quasar 静默忽略——本项目错过一次） |
| `test_notify_events.py` | 事件层：订阅过滤/边沿触发/冷却/限流/**记账晚于发送**/list 型配置的前向兼容 |
| `test_finish_idle.py` | 完结判定与断更提醒。完结判据的失败方向不对称：误判 = 最后一集永远下不下来且不报错 |
| `test_enrich_refund.py` | 退避阶梯与「bgm 不可达退款」。退款判据写错会把整个阶梯废掉 |
| `test_movies_scan.py` / `test_movies_identity.py` | 剧场版入库守卫与身份归并（合并会**删行**且不可逆） |
| `test_dashboard_invariants.py` | 仪表盘拆桶：五者之和 = 待下总数 |
| `test_qb_precheck.py` | 三个交付入口共用的 qB 预检 |

共 **425+** 条。

**没覆盖**（有意）：真实网络抓取、真实 qB、NiceGUI 页面渲染、Alembic 迁移、MySQL 后端。
这些要么依赖外部环境，要么值得单独一套集成测试。
