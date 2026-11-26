---
feature: review-followups
status: in-progress
updated: 2026-11-26
source: fix/review-followups
---

# 审查后续修复：日历联动、条宽、安全卫生与回归测试

## Report
[Not finalized yet]

## [S1] Problem

v1.0.2 代码审查发现六类问题，彼此独立但同属一份审查的后续：

1. **新面板不跟日历**：选中热力图日期后，Agent 用量 / 首轮命中 / 会话归并三个面板仍显示区间数据，与上方模型 / 项目 / 会话面板的联动行为割裂。
2. **排行条宽用错字段**：五处条形图宽度用 `share`（相对总量）而非服务端已算好的 `pct`（相对最大值），条数多时全是短线；`by_model` 干脆没提供 `pct`。
3. **回归纪律未沉淀**：发布说明称"已用字段集合断言做过回归"，但仓库没有任何测试文件。
4. **窗口起点逻辑两份**：`aggregate()` 内联计算与新抽出的 `range_since_ms()` 重复。
5. **两处多余的攻击面**：静态文件路径检查用字符串前缀匹配；所有 JSON 响应带 `Access-Control-Allow-Origin: *`；`do_OPTIONS` 声明的方法集与实际不符。
6. **零散小项**：会话归并表"子会话"列数字含根自身；`openAgent` 不持久化、切范围不重置；CSV 缺 agent 列；`tempfile.mktemp` 已废弃；两处死代码；首轮分桶边界与文档"先取整再分桶"不符。

## [S2] Design

### S2.1 新面板接日历联动

- `GET /api/agents`、`/api/first-round`、`/api/sessions-root` 新增可选查询参数 `dates`（逗号分隔 `YYYY-MM-DD`，最多取前 32 个，与 `/api/days-hourly` 上限一致）。
- 传 `dates` 时忽略 `range`，条目筛选改为「消息本地日期 ∈ dates」。首轮仍遵守既有口径「全历史定位、窗口只筛结果」——`dates` 只筛首轮发生的时间，不改变哪条是首轮。`pending` 计数同样按 `dates` 筛。
- 三个响应新增 `dates` 字段（未传时为 `null`），供前端与测试核对口径。
- 服务端抽 helper `select_items(range_key, dates)`：返回按 range 或 dates 筛好的 items，三函数共用；日期判定复用现有 `day_key()`。
- 前端：`loadDimensions()` 在 `state.selectedDayDates.length > 0` 时带 `&dates=…`，三面板副标题用现有 `selectionLabel()`；`closeDayDetail()` 恢复区间时同步调用 `loadDimensions()`。

### S2.2 排行条宽使用 pct

- `aggregate()` 的 `by_model` 增加 `pct`（相对 `by_model[0].tokens`），与 `by_project` / `by_session` 对齐。
- 前端五处条宽改用 `pct`：`renderSessions`（`s.pct`）、`renderModels`（`m.pct`）、`renderProjects`（`p.pct`）、`renderAgents`（`a.pct`）、`renderRoots`（`r.pct`）。`meta` 文案中的 `share` 百分比不变。

### S2.3 回归测试

- 新增 `tests/test_api_shape.py`，仅用标准库 `unittest`，不引入依赖。
- fixture：`setUp` 用 `tempfile` 建最小 SQLite（`session` / `project` / `message` 三表，列名与真实 schema 一致），插入构造消息（含正常 assistant、全 0 assistant、user、带 `agent_id`、带 `parent_id` 的子会话），并把 `server.MIMO_DB_PATH` 指向它；`tearDown` 恢复并清空 `server` 模块级缓存与 `_FULL_*`。
- 断言范围：六个接口函数的顶层键集合与 `totals` 键集合；`parse_tokens` 的 zero / keep_zero 行为；`range_since_ms` 与 `aggregate` 窗口一致；`export_csv` 表头；静态路径解析；首轮分桶边界。
- 运行方式：仓库根 `python -m unittest discover -s tests -v`。

### S2.4 窗口起点统一

- `aggregate()` 内联的 `range_since` 计算替换为 `range_since_ms(range_key)`。`collect_rows()` 因带 `min_days` 放宽逻辑，不在本次范围。

### S2.5 路径检查与 CORS

- 静态文件路径解析抽为 `_resolve_static(rel) -> Optional[Path]`，用 `Path.is_relative_to(FRONTEND_ROOT)`（Python 3.9+）替代 `str.startswith`；越界返回 `None`，路由回 403。
- `_json()` 删除 `Access-Control-Allow-Origin: *`。删除 `do_OPTIONS()`：前端与 API 同源，不存在预检请求。

### S2.6 零散修复

- 会话归并表"子会话"列改为 `children.length`，与同行副标题一致。
- `state.openAgent` 读写 `sessionStorage`（键 `mimo-ts-open-agent`），`setRange()` 中重置。
- `export_csv` 在末尾追加 `agent` 列，不改变既有列序。
- `get_user_quota` 用 `tempfile.NamedTemporaryFile(delete=False)` 替代 `mktemp`，`finally` 中清理。
- 删除未引用的 `_parse_message_rows` 与 `aggregate()` 中的空 `pass` 块。
- `_first_round_bucket` 改为先取整再分桶：命中率取整为 0 即 `zero`，与 README 口径一致；签名简化为 `_first_round_bucket(hit)`。

## [S3] Out of Scope

- 密码持久化（需单独决定存储位置与哈希方案）。
- 登录速率限制、默认监听地址。
- `/api/quota` 的 Cookie 解密可行性。
- `aggregate()` 拆分、三处"其他"桶逻辑去重。
- 旧版 schema（无 `agent_id` / `parent_id`）的降级路径。
- README 中开发日志段落的位置调整。

## Tasks

- [ ] T1: 建测试骨架——`tests/test_api_shape.py`、最小 fixture DB、对当前代码的键集合断言 — acceptance: `python -m unittest discover -s tests -v` 在未改源码时全绿；断言覆盖六个接口函数的顶层与 `totals` 键集合 (covers: S2.3)
- [ ] T2: `aggregate()` 窗口起点改用 `range_since_ms()` — acceptance: `aggregate` 函数体不再含 `replace(hour=0` 分支；测试断言 `range_since_ms("today")` 等于本地零点 (covers: S2.4) (depends: T1)
- [ ] T3: `by_model` 加 `pct`，前端五处条宽改 `pct` — acceptance: 测试断言 `by_model[0].pct == 100`；五个渲染函数中条宽表达式均引用 `pct` (covers: S2.2) (depends: T1)
- [ ] T4: 抽 `_resolve_static()` + `is_relative_to`，删 ACAO 头与 `do_OPTIONS` — acceptance: 测试断言 `../server.py` 解析为 `None`、`css/style.css` 正常；`server.py` 中不再出现 `Access-Control-Allow-Origin` (covers: S2.5) (depends: T1)
- [ ] T5: 零散修复六项 — acceptance: 测试断言 `export_csv` 表头末列为 `agent`、`_first_round_bucket(0) == "zero"`；`server.py` 中不再出现 `mktemp` 与 `_parse_message_rows`；`renderRoots` 子会话列引用 `children` (covers: S2.6) (depends: T1)
- [ ] T6: 三接口加 `dates` 参数与 `select_items()` helper — acceptance: 测试断言 `aggregate_agents(range, dates=[某天]).totals.tokens` 等于 fixture 中该日条目之和，且三个响应均含 `dates` 字段 (covers: S2.1) (depends: T2)
- [ ] T7: 前端 `loadDimensions` 带 `dates`、副标题用 `selectionLabel`、`closeDayDetail` 回区间 — acceptance: 浏览器选中一天后三面板副标题显示该日期，取消后恢复区间标签 (covers: S2.1) (depends: T6)
- [ ] T8: README API 表补 `dates` 参数说明 — acceptance: README 三个接口行均提到 `dates` (depends: T6)

## Journey log
None
