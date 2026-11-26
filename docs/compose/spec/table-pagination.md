---
feature: table-pagination
status: in-progress
updated: 2026-11-26
source: feat/table-pagination
---

# 三张列表的服务端分页

## Report

[Not finalized yet]

## [S1] Problem

会话用量、项目榜、会话归并三张列表都在服务端被静默截断，UI 却提供「全部」：

- `by_session[:50]`（`aggregate()` 与 `aggregate_dates_ranks()` 各一处）、`by_project[:30]`（同两处）、`by_root[:100]`。
- 2026-11-26 实测（只读库）：全部历史有 **862** 个有消耗的会话（服务端只给 50）、**355** 个根会话（只给 100）、19 个项目（30 的上限没生效，但同样是硬截断）。
- 会话用量还有一个「显示更多（还有 N 条）」按钮：它把「前 N 名」变成「全部」，但「全部」也只是那 50 条。

上一轮给首轮明细做的是前端分页（`/api/first-round` 本来就不截断）。这三张表不能照抄：它们的搜索、项目 / 模型筛选、排序都在浏览器里做（`filteredSessions()` / `sortRankItems()`），一旦只把当前页交给前端，搜索就退化成「搜当前页」。用户选定：**服务端真分页（`limit` / `offset`）**，同时把这三张表的搜索 / 筛选 / 排序搬到服务端，前端拿到的每一页就是最终结果。

## [S2] Design

### S2.1 服务端分页契约

**分页发生在缓存之后。** `aggregate(range_key)`、`aggregate_dates_ranks(dates)`、`sessions_by_root(range_key, dates)` 的缓存键保持原样（只含 `range` / `dates`），但返回值改为**全量**行（删掉 `[:50]` / `[:30]` / `[:100]`）。HTTP 处理器在缓存对象上做分页：浅拷贝响应 dict、替换被分页的列表、补总数。这样翻页命中缓存即分片，不会因为 `limit` / `offset` 进缓存键而让每一页都触发一次全库扫描。

**新增查询参数**（全部可选，缺省时行为与现在一致）：

| 接口 | 参数 | 默认 | 说明 |
| --- | --- | --- | --- |
| `/api/overview`、`/api/days-hourly` | `session_limit` | 50 | 每页条数；`0` = 不分页（一次给全部） |
| 同上 | `session_offset` | 0 | 跳过多少条（= `(page-1) * limit`，由前端算） |
| 同上 | `session_q` | 空 | 搜索：小写子串匹配 `title` / `project_name` / `directory` / `id` / `models[].model` |
| 同上 | `session_project` | 空 | `project_name` 精确相等 |
| 同上 | `session_model` | 空 | `models[].model` 精确相等（含该模型的会话） |
| 同上 | `session_sort` | `last` | `last` / `tokens` / `turns` / `avg` / `input` / `output` / `cache` / `hit` |
| 同上 | `project_limit` | 30 | 每页条数；`0` = 全部 |
| 同上 | `project_offset` | 0 | 同上 |
| 同上 | `project_sort` | `tokens` | 同 `session_sort` 的键集合 |
| `/api/sessions-root` | `limit` / `offset` | 100 / 0 | 每页条数 / 偏移；`0` = 全部 |
| 同上 | `q` | 空 | 匹配根会话 `title`、`project_name`、子会话 `children[].title` |
| 同上 | `sort` | `tokens` | 同上的键集合 |

**响应新增字段（只加不改）**：`/api/overview` 与 `/api/days-hourly` 加 `sessions_total`、`projects_total`；`/api/sessions-root` 加 `total`。三者都是**过滤后、分页前**的行数，前端用它算页数。既有字段、`definition`、`range_label`、`since` 一律不动。

**过滤与排序语义必须与现有前端逐条对齐**（否则切换当天表格里的数字会跳）：过滤规则照 `filteredSessions()`（app.js:1382）与 `renderRoots()` 的搜索分支；排序照 `sortRankItems()`（全部按数值降序）。

**排序稳定性**：所有排序键按数值降序，并列时二级键 `tokens` 降序、三级键 `id` 升序。现有前端是稳定排序（V8）且服务端本来就按 tokens 降序，所以二级键与现状等价；显式写出来是为了让分页不串行——没有二级键时，同分的两行可能在两页里各出现一次或都不出现。

**`pct` 语义不变**：仍在 `aggregate()` 里按全量、排序后计算（相对第一名），分页前算完，所以翻页后条宽不跳变、筛选后仍显示全局占比（与现状一致）。

**边界行为**：`offset` 超过总数 → 该列表返回空数组、`*_total` 仍给真实总数（不算错误）；`limit` / `offset` 非整数或负数 → 400 `{"ok": false, "error": "bad limit/offset"}`（与 `dates` 坏格式的处理一致）；未知 `sort` 值 → 回落到该接口的默认键，不报错。

**`/api/day-hourly`（单日）不带分页参数**：它的 `by_session` / `by_project` 只用于小时图，前端不渲染成表。

### S2.2 前端通用翻页控件

把首轮明细的翻页控件抽成通用渲染函数（信息行 + 上一页 / 页码 / 下一页 + 每页条数 + 跳至），四张列表共用同一套 DOM 结构与文案，仅容器 id 不同：`#firstround-*`（已有）、`#session-*`、`#project-*`、`#roots-*`。首轮明细保持前端分页（数据本来就全量），三张新表是服务端分页——差别只在「翻页动作」：前者本地重排，后者带着新 `offset` 重新请求。

每张表一份页码状态与口径签名（范围 / 选中日期 / 该表的筛选 + 排序 + 页大小）；签名变化回第 1 页，页大小或筛选变化同时把 `offset` 归零。行数变少导致页码越界时收到最后一页（再请求一次）。

### S2.3 会话用量表接入

- `renderSessions()` 不再 `slice`，渲染服务端返回的这一页；请求带 `session_limit` / `session_offset` / `session_q` / `session_project` / `session_model` / `session_sort`。
- 区间模式拼在 `/api/overview` 上，选中日期模式拼在 `/api/days-hourly` 上（`applySelectedDayRanks` 那条路径）。
- 搜索输入沿用 160 ms 防抖；搜索 / 筛选 / 排序 / 页大小变化 → 回第 1 页并只重取这张表的数据源（不连带重取维度接口）。
- 移除「显示更多」按钮与 `#table-foot`（分页接管；`#btn-more` 的处理器一并删）。
- 空状态 `#session-empty`、行点击开抽屉、`pct` 条宽都不变。

### S2.4 项目榜接入

- `renderProjects()` 不再 `slice`；请求带 `project_limit` / `project_offset` / `project_sort`。
- 项目榜是 `rank-list`（不是表格），翻页控件插在 `#project-list` 之后，形态与表格那几张一致。
- 展开状态 `state.openProject` 保留：翻页后若它不在当前页就不显示细节，翻回来仍在。

### S2.5 会话归并接入

- 请求带 `limit` / `offset` / `q` / `sort`；`renderRoots()` 不再 `slice`，搜索改为服务端过滤（防抖同会话表）。
- `#roots-sub` 的文案（根会话数 / 并入子会话数）用响应的 `totals`，与分页无关，翻页不跳。

### S2.6 版本与文档

接口新增参数与响应字段 ⇒ 版本串升到 `MiMoTokenStats/1.0.4`，新增 `RELEASE_NOTES_v1.0.4.md`，README 的 API 表补参数说明。

## [S3] Out of Scope

- 首轮明细的分页机制（保持前端分页，只把控件换成共用实现）。
- 模型榜、agent 榜的分页（天然只有二十来项）。
- URL 深链接到某一页、页码写入 `localStorage`。
- 缓存策略调整（180 秒 TTL 与 `/api/refresh` 不动）。
- 服务端分页的游标（cursor）形式；只用 `limit` / `offset`。

## Tasks

- [x] T1: 分页契约的失败测试 — acceptance: `tests/test_api_shape.py` 新增用例覆盖 `session_limit/offset/sort`、`session_q/project/model` 过滤、`project_*`、`/api/sessions-root` 的 `limit/offset/q`、`*_total` 字段、越界 `offset` 返回空数组、坏参数 400；在当前实现上运行这些新用例必须**失败**（旧实现没有这些参数，`total` 字段缺失） (covers: S2.1)
- [ ] T2: 服务端实现 — acceptance: T1 的用例全部通过；`aggregate()` / `aggregate_dates_ranks()` / `sessions_by_root()` 返回全量行；缓存键仍只含 `range` / `dates`（同 range 连续两次不同 `limit` 只有一次全量计算）；原有 44 条测试仍全绿 (covers: S2.1) (depends: T1)
- [ ] T3: 前端通用翻页控件 — acceptance: 首轮明细的翻页行外观 / 顺序 / 文案与改动前一致（`共 N 条 · 第 x / y 页`、上一页 / 页码 / 下一页、`N 条/页`、跳至），有头场景复跑上一轮的 T1 / T2 / T4 / T5 断言全绿 (covers: S2.2)
- [ ] T4: 会话用量表接入服务端分页 — acceptance: 全部历史下选 10 条/页只显示 10 行、翻到第 2 页与第 1 页无重复行（`session_id` 唯一）、页数 = `ceil(sessions_total / 10)`；搜索词 / 项目筛选 / 模型筛选 / 排序 / 页大小变化回第 1 页并改变结果；「显示更多」按钮消失；选中日期模式同样可分页 (covers: S2.3)
- [ ] T5: 项目榜接入 — acceptance: 全部历史有 19 个项目、选 10 条/页显示 10 个并出现第 2 页；改排序刷新为对应顺序；展开某个项目后翻页再翻回，展开状态仍在 (covers: S2.4)
- [ ] T6: 会话归并接入 — acceptance: 全部历史 `total` 为 355 附近、选 10 条/页只显示 10 行；搜索只回匹配行且页数随之变化；翻页后 `#roots-sub` 的根会话数不变 (covers: S2.5)
- [ ] T7: 版本串与发布说明 — acceptance: `Server` 响应头为 `MiMoTokenStats/1.0.4`、`RELEASE_NOTES_v1.0.4.md` 存在且写明新增参数与字段 (covers: S2.6)
- [ ] T8: README API 表补参数 — acceptance: `/api/overview`、`/api/days-hourly`、`/api/sessions-root` 三行写明分页 / 过滤 / 排序参数与新增的总数字段 (covers: S2.6) (depends: T2)

## Journey log

- 2026-11-26 用户定下形态：**服务端 `limit` / `offset` 真分页**（不是「服务端不再截断 + 前端分页」），范围 = 会话用量 + 项目榜 + 会话归并，每页条数统一 `10 条/页`（10 / 20 / 50 / 全部）。已知代价（选择时已说明）：搜索 / 项目 / 模型筛选 / 排序必须一起搬到服务端，回归面比首轮明细的前端分页大得多。
- 2026-11-26 实测数据（只读库）：全部历史 862 个有消耗会话 / 355 个根会话 / 19 个项目；7 天 75 个会话、30 天 166 个。`by_project[:30]` 在本机从未生效，但它与另两处同样是硬截断，一并交给分页。
- 2026-11-26 分层结论：分页必须在**缓存之后**做——`aggregate` 的缓存键是 `agg:{range}`（只含 range），若把 `limit` / `offset` 拼进键，每翻一页都要重扫全库；改为「缓存全量行、处理器分片」后，翻页只做一次列表切片。
