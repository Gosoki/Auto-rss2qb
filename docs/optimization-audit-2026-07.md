# 全项目优化审查报告（2026-07-25）

多智能体并行审查（15 agent，逐组"发现→对抗验证"+ 跨文件口径专审）产出 33 条，已人工复核归类。
分两部分：**A. 已落地的行为保持型优化**（不改变任何对外效果）；**B. 口径不一致 / 需裁决项**（改动会变行为，仅记录，待用户判断）。

---

## A. 已落地优化（behavior-preserving，已应用）

| # | 文件 | 位置 | 优化 | 收益 |
|---|------|------|------|------|
| A1 | core/engine.py | `_QB_SETTLED`/`_QB_TRANSIENT` | 预算模块级 `_QB_SETTLED_LIST`/`_QB_TRANSIENT_LIST`，`_inflight_where`/`has_active_downloading` 复用，不再每次 `list()` | 热路径省重复分配 |
| A2 | core/anime.py | `backfill_source` | `ref_names` 全量构建（含逐条 `_norm_name` 正则）移进 `if strict:`；非 strict 补齐不再白算 | 默认『补齐该源』省一轮正则归一 |
| A3 | core/anime.py | `apply_start_date_filter`/`ignore_confirmed_before_start` | 循环外解析一次 `ANIME_START_DATE`，不再逐番重复 `_parse_date` 常量 | 省 N 次 strptime |
| A4 | core/movies.py | `overview` | 删冗余 `active_ids`，用 `q_of` 键判成员（键域相同） | 省一次集合构建 |
| A5 | services/enrich.py | `resolve` | 复用 `_search_one` 已解析的 date，不再二次 `_parse_date` 同串 | 省重复解析 |
| A6 | services/enrich.py | `resolve` L265 | 短路条件改 `bgm_id is None and _parse_date(...) is None`，命中 bgm_id 时跳过解析 | 省一次解析 |
| A7 | sources/parse.py | `parse_title`/`candidate_names` | 抽 `_group_and_body` 共享 `_GROUP_RE.match`+body（不共享 slash 切分，语义不变） | 每条种子省一次组名解析 |
| A8 | sources/parse.py | `_clean_name`/`_clean_for_search`/`parse_multibracket` | 去标签块正则提为模块级 `_TAG_BLK_RE` | 预编译 |
| A9 | sources/parse.py | `_clean_name`/`_clean_for_search` | `_STRIP_PATTERNS`/`_EP_TAIL` 预编译成正则对象 | 预编译 |
| A10 | sources/nyaa.py, mikan.py | info_hash 校验 | 40 位 hex 正则提为模块级 `_HEX40_RE`（三处复用） | 预编译，每条种子热路径 |
| A11 | sources/mikan.py | `fetch_bangumi_torrents` | 同 A7 共享 group/body | 剧场版路径省重复解析 |
| A12 | pages/anime.py | `confirm_panel`/`reject_panel`/`fail_panel` | 循环前取一次 `source_map()`，消 N+1 的逐番 `anime_sources` 查询 | 三处 N+1→1 |
| A13 | pages/anime.py | `charts_panel` | 每季度 `quarter_label(q)` 算一次给 label+tooltip 复用 | 省重复格式化 |
| A14 | pages/layout.py | `frame()` | 4 段恒定 `add_head_html`（CSS/preload/referrer/徽标配色）提为模块级常量 | 每次渲染省字符串拼接 |
| A15 | core/engine.py | `add_to_qb` 兜底分支 | `h = info_hash.lower()` 统一命中判定与日志口径（防御性） | 口径一致 |
| A16 | db/models.py | `AnimeTorrent`/`MovieTorrent.info_hash` | 去掉冗余 `index=True`（唯一约束索引已覆盖等值查找）；`db/__init__` 加迁移 `DROP INDEX ix_*_info_hash` 清老库重复索引 | 省一份索引、写入更快 |

以上均经审查对抗验证 `behavior_preserving=true`，且由逻辑对抗测试 + 真实 qB 生命周期回归覆盖。

---

## B. 口径不一致 / 需裁决项（未改，改动会变行为）

> 任务要求"记录口径不一样的地方"。以下为跨文件同一概念口径不同处，多数只在**元数据缺失/边角场景**触发；改动会改变落盘目录或下载决策，属需你裁决的项。按重要性排序。

### B1【重要】"该集已有一份、不再下"三套口径（core/anime.py）
`flush_ready_downloads` / `download_plan`·`_select_downloads` / `download_anime_torrent` 锁内去重，三处对"这一集已经有了"的定义各异：
- flush 的 `downloaded` 集 = `status in (downloaded, stalled)`，键 `(anime_id, episode)`；**含 stalled、不含 downloading/deleted**。
- plan/补下的 `have_eps` = `status in (downloaded, downloading, deleted)`，键仅 `episode`；**含 downloading+deleted、不含 stalled**。
- download 锁内去重 = `status in (downloading, downloaded)`；**不含 stalled/deleted**。

可观察后果：
- **stalled**：flush 视作"已有一份"不自动换源（有意），但详情页 `download_plan`/`covered` 不含 stalled → 会把该集 pending 兄弟标"将下载"、`download_pending_for_anime` 手动补下会真的下 → "详情页说会下、后台 flush 不下"打架。
- **downloading**：某集只有一条在下还没 downloaded 时，flush 不认它为"已有"→ 会再挑一条 pending 放行，进 download 才被锁内去重压 skipped（多一次无谓占位，靠锁兜底）。
- **deleted**：have_eps 含 deleted（删过不重下）、flush 不含 → 同集来新 hash 时 flush 会下（有意），但 plan/covered 把该集算 covered、把新 hash 标"备用"而非"将下载"，两处结论相反。

**建议**：定义单一权威 `_have_episodes(rows)`，明确 downloaded/downloading/stalled/deleted 各是否计入，四处统一。重点定夺 stalled（flush 计入 vs plan 不计入）与 downloading（flush 不计入 vs plan 计入）。**需你确认是否符合"stalled 不自动换源""deleted 不重下"两条承诺后再统一。**

### B2 番剧保存路径回退链：下载入口 vs 显示/relocate 入口不一致（core/anime.py）
`download_anime_torrent`：quarter=`a.quarter or t.quarter or 'unknown'`，folder=`(a.jp_name or a.display_name) or t.anime_title`。
`anime_save_path`（详情页『↓保存目录』、relocate 算 new_path）：quarter=`a.quarter or 'unknown'`（不回退 t.quarter），folder=`(a.jp_name or a.display_name) or a.title or 'unknown'`。
→ a.quarter 空但 t.quarter 非空、或 a 三名皆空但种子有 anime_title 时，**详情页显示/relocate 目标 ≠ 实际落地目录**，可能误导或 relocate 搬到错目录。日常（已确认+已富集，a.quarter/jp_name 都有值）一致，仅元数据缺失边角触发。
**建议**：抽共用 `(quarter, folder, season)` 计算函数，两入口统一回退链。

### B3 剧场版 folder 回退链不一致（core/movies.py）
`download_movie_torrent` 末档退 `t.raw_title`，`movie_save_path`（relocate 依赖）末档退 `m.title`。jp_name/display_name 皆空时分叉（`_upsert_movie` 通常兜底填 display_name，故日常一致）。同 B2，建议抽 `_movie_folder(m, t)` 共用。

### B4 详情页 `covered` 漏 stalled（pages/anime_detail.py）
`covered` 未含 stalled，与 flush"stalled 算已有一份"不一致 → 某集唯一下载 stalled 时详情页误报橙色"缺集"。**低风险纯显示修复**：`covered` 加 `stalled`。（可直接改，等你点头。）

### B5 仪表盘状态桶口径（core/anime.py / movies.py overview）
overview status 汇总只列 6 态，遗漏 **excluded**（用户主动排除的待下终态在仪表盘完全不可见）与 deleted。`failed_rows` 把 error+stalled 并作"失败"，pending_breakdown 不涉 stalled。建议 overview status 补 excluded，且 anime/movies 两侧共用同一份状态常量。

### B6 `qb_summary` 做种计数含"已完成暂停"（core/engine.py）
`_QB_SEEDING` 含 pausedUP/stoppedUP（已完成暂停），`qb_summary` 把它们计入"做种 N"，与详情页把它们显示为"已完成"文案对不上（做种数偏大）。建议 qb_summary 单列"已完成"或改标签语义。

### B7 "已交付" vs "已完成"两套判据（core/anime.py vs engine.archive）
业务侧（downloaded_count/_has_downloads/have_eps/relocate 选取）只看 `status`，归档侧额外要 `qb_progress>=1`。开 QB_SYNC_STATUS 时，status=downloaded 但 progress<1 的种子在"去重/统计"算已下、在"完成/显示"算在下。建议 docstring 明确区分 delivered/completed，或 UI"已下"计数改用 progress>=1。

### B8 声优抓取不走重试（services/enrich.py）
`_fetch_cast` 是唯一未包 `_retryable` 的 bgm 调用，一次瞬时超时即丢 cast，其它字段会重试。**需确认是否有意让 cast"瞬时失败即放弃"**；若否，包上 `_retryable` 即可（会改失败耗时，故未擅改）。

### B9 其它（低优先，可不改）
- `extract_season(raw_title)` 可改用 body（与集数抽取一致），但若字幕组把季号写进 `[组]` 会变——罕见，未改。
- nyaa/mikan `_parse` 中"空名短路"与"白名单过滤"先后相反（最终入库集合相同，仅短路路径不同）。
- 仪表盘 `overview()` 在一次刷新里被 head/charts/tail 三个独立 refreshable 各算一次（2~3 次全量聚合）；合并会破坏独立刷新交互态，建议在刷新层算一次传入，属需谨慎实现的建议项。
- `config._v` 跨线程无锁读写（后台协程读、UI 线程写），撕裂窗口极小、影响近零；如需彻底消除可在 `http_client_kwargs` 内 `dict(_v)` 快照。
- overview 与 pending_breakdown 重复查一次非拒绝 Anime.confirmed（可传参复用，收益小、跨函数签名，暂缓）。

---

## C. 测试结论（Phase 3/4）

优化后跑了 **170 条断言，全部通过、零回归**：

| 套件 | 断言 | 覆盖 |
|---|---|---|
| 逻辑对抗 | 104 | parse(番名/集数/合集/季度/中文数字) · engine(safe_name/build_save_path/prev_quarter/pick_best/qB态集合/SSRF/qb_is_local) · in-flight 语义 · 选集去重/plan · reject/restore · config · netguard |
| mock-sync 状态机 | 14 | sync_qb_status 全分支：镜像/进度推进/停滞/error/missingFiles/做种落定/d-None 三分支(进度满→已下、曾同步→error、未同步→宽限)/连不上/关跟踪 |
| **真实 qB 全生命周期** | 37 | 真实 qB(10.0.0.230)：入队 add(save_path/category/落库) → 完成落定 → 重复add幂等 → **移动 setLocation** → **归档(留文件)** → **重下已归档** → **删除** → 跨表同hash只脱手 |
| flush 下载决策管线 | 15 | 分组去重/优先级/缓冲窗口 · deleted不挡新hash · stalled挡换源 · 特别篇每番一份 · 锁源 · 关键词过滤 · 待确认不下 · 换源兜底(error复活skipped、downloaded/deleted不复活) |

**真实 qB 验证到的**：add 传参/save_path/category/本地落库、setLocation 移动、archive_old_completed 的 qb.delete(留文件)、重下已归档的 qb 重新入队、delete_files 删除、跨表同 hash 的 hash_owned_elsewhere 保护——全部与设计一致。

**未做真·端到端下载完成**：archive.org 测试种子在 qB 主机上因外网 webseed 不通而 `stalledDL`（**环境网络问题，非代码 bug**）；LAN webseed 方案被安全策略拦下。改以「模拟完成态(写 DB) + 真实 qb.delete/add」覆盖归档/重下，「mock torrents_info」确定性覆盖 sync 状态机——逻辑覆盖等价且更稳定。

**Bug 结论**：测试未发现任何代码 bug。此前基线跑出的 16 个"失败"经核实全部是"外网下不完"的级联，非代码问题。B 部分的口径不一致是既存的边角一致性问题（非崩溃/丢数据），已记录待裁。

## D. 前瞻优化建议（未做，供参考）

1. **统一路径/去重口径（对应 B1/B2/B3）**：抽 `_anime_path_parts(a,t)` / `_movie_folder(m,t)` / `_have_episodes(rows)` 三个权威函数收口回退链与"已有一份"判据，是本项目最值得做的一致性收敛（消除元数据缺失边角的搬迁/换源分歧）。因会改变边角行为，建议你确认语义后我再统一。
2. **状态桶共用常量**：`overview()` 的 status 元组在 anime/movies 各写一份且都漏 excluded；抽一份共享常量并决定 excluded 是否上仪表盘。
3. **仪表盘 overview() 单轮算一次**：head/charts/tail 三个 refreshable 各算一遍(2~3×全量聚合)；在刷新层算一次传入可省 1~2 次重聚合（需保住三者可独立刷新的交互态）。
4. **`status` 列索引**：flush 的 `where status=='pending'`、归档的 `status=='downloaded'` 走全表扫；数据量大后可加 `status` 索引（当前量级无感，故未加）。
5. **config `_v` 快照**：如要彻底消除跨线程读配置的撕裂窗口，可在 `http_client_kwargs` 内 `dict(_v)` 一次性快照（当前撕裂概率与影响近零）。
6. **`_fetch_cast` 是否加重试（B8）**：目前声优抓取瞬时失败即放弃、与其它 bgm 字段不一致；若希望一致，包 `_retryable` 即可（会改失败耗时）。
