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

- 静态文件路径解析抽为 `_resolve_static(rel) -> Optional[Path]`，用 `Path.is_relative_to(FRONTEND_ROOT)`（Python 3.9+）替代 `str.startswith`；越界返回 `None`，路由回 403。评审后收紧为目录白名单，见 S2.8。
- `_json()` 删除 `Access-Control-Allow-Origin: *`。删除 `do_OPTIONS()`：前端与 API 同源，不存在预检请求。

### S2.6 零散修复

- 会话归并表"子会话"列改为 `children.length`，与同行副标题一致。
- `state.openAgent` 读写 `sessionStorage`（键 `mimo-ts-open-agent`），`setRange()` 中重置。
- `export_csv` 在末尾追加 `agent` 列，不改变既有列序。
- `get_user_quota` 用 `tempfile.NamedTemporaryFile(delete=False)` 替代 `mktemp`，`finally` 中清理。
- 删除未引用的 `_parse_message_rows` 与 `aggregate()` 中的空 `pass` 块。
- `_first_round_bucket` 改为先取整再分桶：命中率取整为 0 即 `zero`，与 README 口径一致；签名简化为 `_first_round_bucket(hit)`。

### S2.7 版本与发布说明

Implement 完成后由用户确认追加（spec 原先未含，但项目惯例要求接口变更同步升版本）。版本串升为 `MiMoTokenStats/1.0.3`（`Handler.server_version`，即 `Server` 响应头）；补 `RELEASE_NOTES_v1.0.3.md`，沿用既有格式归纳本次全部接口变更。

### S2.8 静态路由只放行三个目录（评审跟进，合并前追加）

独立评审发现：静态路由排在鉴权之前，按原始路径前缀（`/assets/`、`/css/`、`/js/`）放行后，`_resolve_static` 只校验解析结果没出仓库根，于是 `/css/../server.py` 免登录返回 200 与服务端源码；同一路数还能读到 `index.html`、`README.md`、`.git/config`。

改为解析后的路径必须落在 `assets/`、`css/`、`js/` 三个目录之一，其余一律 403（`STATIC_DIRS` 一处定义，路由前缀判断与解析校验共用）。`index.html` 仍只经 `/` 与 `/index.html` 路由、过鉴权后提供。

### S2.9 首轮加载的竞态（评审跟进，合并前追加）

`#firstround-agent` 的 change 处理独立调用 `loadFirstRound()`，原先只受 `dimsReqId` 约束——连改两次 agent 时，先发的响应后到会覆盖后发的结果：下拉显示 B，表格却是 A。

`loadFirstRound` 在响应回来时按当时的范围 / 日期 / agent 重新对一遍请求口径，对不上即丢弃。按口径而不是按先后次序判过期：维度加载进行中切换 agent 时，独立的首次请求与维度加载随后发出的请求口径相同，谁先到都能直接用，不必等后一次。

### S2.10 日期模式下保留 agent 筛选（评审跟进，合并前追加）

`populateFirstRoundAgentFilter` 以 `/api/agents` 的列表为准「先校正、再取首轮」：本地记下的 agent 不在列表里就清空，让失效的筛选能自愈。但带 `dates` 时列表只有这些天有数据的 agent，同一规则会把「当天没数据」的筛选也清掉。

区分两种列表：响应自带 `dates`（按日期筛过）时，当前 agent 不在列表里则保留下拉选项并标注「所选日期无数据」，表格照常显示空状态；不带 `dates`（区间）时沿用原规则校正。回到区间后该 agent 若仍无数据，筛选照旧被清空。

### S2.11 两处清理（评审跟进，合并前追加）

- `get_days_hourly` 四处裸写 `[:32]`，而 `MAX_DATES` 的注释自称与它一致：改为在入口截断一次，缓存键也随之按截断后的日期计算（超过上限时，键与实际计算的范围才对得上）。
- 有选中日期时，每次刷新都会从日详情同步（`syncDaysDetail`）和总览加载（`loadOverview`）各调一次 `loadDimensions()`，同窗口发两套请求、一套被 `dimsReqId` 丢弃。`loadDimensions` 改为按「范围 + 选中日期」记住进行中的加载，同窗口的再次调用共用同一个 Promise。代价：同窗口已有请求在途时，紧随其后的刷新沿用那次请求的数据，下一次刷新再更新；相比服务端 180 秒的缓存，这点滞后可以忽略。

## [S3] Out of Scope

- 密码持久化（需单独决定存储位置与哈希方案）。
- 登录速率限制、默认监听地址。
- `/api/quota` 的 Cookie 解密可行性。
- `aggregate()` 拆分、三处"其他"桶逻辑去重。
- 旧版 schema（无 `agent_id` / `parent_id`）的降级路径。
- README 中开发日志段落的位置调整。

## Tasks

- [x] T1: 建测试骨架——`tests/test_api_shape.py`、最小 fixture DB、对当前代码的键集合断言 — acceptance: `python -m unittest discover -s tests -v` 在未改源码时全绿；断言覆盖六个接口函数的顶层与 `totals` 键集合 (covers: S2.3)
- [x] T2: `aggregate()` 窗口起点改用 `range_since_ms()` — acceptance: `aggregate` 函数体不再含 `replace(hour=0` 分支；测试断言 `range_since_ms("today")` 等于本地零点 (covers: S2.4) (depends: T1)
- [x] T3: `by_model` 加 `pct`，前端五处条宽改 `pct` — acceptance: 测试断言 `by_model[0].pct == 100`；五个渲染函数中条宽表达式均引用 `pct` (covers: S2.2) (depends: T1)
- [x] T4: 抽 `_resolve_static()` + `is_relative_to`，删 ACAO 头与 `do_OPTIONS` — acceptance: 测试断言 `../server.py` 解析为 `None`、`css/style.css` 正常；`server.py` 中不再出现 `Access-Control-Allow-Origin` (covers: S2.5) (depends: T1)
- [x] T5: 零散修复六项 — acceptance: 测试断言 `export_csv` 表头末列为 `agent`、`_first_round_bucket(0) == "zero"`；`server.py` 中不再出现 `mktemp` 与 `_parse_message_rows`；`renderRoots` 子会话列引用 `children` (covers: S2.6) (depends: T1)
- [x] T6: 三接口加 `dates` 参数与 `select_items()` helper — acceptance: 测试断言 `aggregate_agents(range, dates=[某天]).totals.tokens` 等于 fixture 中该日条目之和，且三个响应均含 `dates` 字段 (covers: S2.1) (depends: T2)
- [x] T7: 前端 `loadDimensions` 带 `dates`、副标题用 `selectionLabel`、`closeDayDetail` 回区间 — acceptance: 浏览器选中一天后三面板副标题显示该日期，取消后恢复区间标签 (covers: S2.1) (depends: T6)
- [x] T8: README API 表补 `dates` 参数说明 — acceptance: README 三个接口行均提到 `dates` (depends: T6)
- [x] T9: 版本串升 `1.0.3` + `RELEASE_NOTES_v1.0.3.md` — acceptance: `server.py` 中不再出现 `1.0.2`；发布说明覆盖本次全部接口变更 (covers: S2.7) (depends: T8)
- [x] T10: 静态路由只放行 `assets` / `css` / `js` 三个目录 — acceptance: 测试断言 `_resolve_static("css/../server.py")` 为 `None` 而 `css/style.css` 正常；未鉴权请求 `/css/../server.py`、`/assets/../index.html`、`/js/../README.md` 均回 403，`/css/style.css` 回 200 (covers: S2.8)
- [x] T11: `loadFirstRound` 按请求口径判过期，覆盖 agent 下拉连改的竞态 — acceptance: 浏览器把 `agent=checkpoint-writer-1` 的首轮响应拖慢 1.5 s、随即改选 `main`，慢响应到达后表格仍只含 `main`（改动前会变成 `checkpoint-writer-1`）；维度加载卡在 `/api/agents` 时切换到 `main`，首个 `main` 响应到达即更新表格，不等待后一次同口径请求 (covers: S2.9)
- [x] T12: 日期模式下保留当日无数据的 agent 筛选 — acceptance: 选 `checkpoint-writer-1` 后点 2026-11-23（当天只有 `main` 有数据），下拉仍为 `checkpoint-writer-1` 并带「所选日期无数据」标注、表格显示空状态；取消日期后筛选仍是 `checkpoint-writer-1`（改动前两处都被清成「全部 agent」） (covers: S2.10)
- [x] T13: `get_days_hourly` 复用 `MAX_DATES`；同窗口 `loadDimensions` 去重 — acceptance: 测试断言 `get_days_hourly` 传 40 个日期时只处理 `MAX_DATES` 个（测试内把 `MAX_DATES` 改成 3 再断言，写死 32 的旧实现会失败），`server.py` 中不再出现 `[:32]`；浏览器选中日期后点一次刷新，`/api/agents` 只发 1 次（改动前 2 次），日期连点（11-25 的响应拖慢）三面板仍停在 11-26 (covers: S2.11)

## Journey log

- **T1 `6f052de`**：19 条键集合断言在未改源码时一次通过。唯一修正是 `_root_session_id` 的环用例——`a↔b` 环里「根」没有良定义，契约收窄为「必终止且只返回环内节点」。
- **T2 `00b5b07`**：`aggregate()` 的内联窗口分支替换为 `range_since_ms()`；`replace(hour=0` 剩余 4 处均在 `aggregate` 之外。
- **T3 `0afcf64`**：日期模式下 `renderModels` 吃的是 `aggregate_dates_ranks` 的 `by_model`，所以服务端改了两处（`aggregate()` 与 `aggregate_dates_ranks`）而非一处，否则日期模式下条宽为 NaN。
  - 范围外遗留：`pack_mstat`（`server.py:237`）不返回 `pct`，经它供数的两条模型条仍用 `share`——热力图日详情面板（`js/app.js:623`）与会话抽屉（`js/app.js:1822`）。修它需要给 `pack_mstat` 加 `pct`，会同时影响 `get_day_detail` 与 `session_detail` 两个响应，未纳入本次范围。
- **T4 `fab0403`**：HTTP 层「同名前缀兄弟目录」用例最初少写一层 `..`，路径实际落在根目录内，回 404 才是正确行为；修正后新旧实现才有区分度（旧 404 / 新 403）。
- **T5**：六项全部落地，测试 30 条。
  - `mktemp` 的失败路径用例第一版把失败注入在「复制阶段」，实测**区分不了新旧实现**：`mktemp` 只给文件名、不建文件，复制一失败磁盘上什么都没有，新旧泄漏数均为 0。改为「副本落盘后读库失败」后，旧实现泄漏 1 个、新实现 0 个，用例才真正锁住这次修复。
- **T6**：三接口 `dates` 参数落地，测试 39 条。
  - spec 写的 helper `select_items(range_key, dates)` 实际落成 `selection_window()` + `_time_in_window()`：首轮接口还要用同一窗口筛 `pending` 的时间戳（不是条目），只返回条目的 helper 覆盖不了；三个函数共用同一处窗口定义。
  - 缓存键原样只含 `range`，必须掺入 `dates`，否则带日期与不带日期的请求会静默串台——`test_cache_keeps_dates_apart_from_range` 专门锁这一点。
  - `dates` 模式下 `range_label` 回选中日期（与前端 `selectionLabel()` 同格式）、`since` 回最早选中日的本地零点；`range` 保持回显请求参数。格式错误的日期回 400，不带病算出一份空数据。
  - 真实库实测（31,218 条，只读）：单日筛选 65 ms，三接口未命中缓存时 72 / 4 / 58 ms，无需优化。
- **T7**：前端联动落地，headed 浏览器验收通过（真实库，`--no-auth`，`127.0.0.1:8798`）。
  - 超出 spec 字面的一条：给 `loadDimensions` / `loadFirstRound` 加了 `dimsReqId` 守门（与现有 `hourlyReqId` 同一套路）。日历每次点击都会触发一次加载，快速连点时先发的响应后到会把面板覆盖成错的日期；守门让过期响应连 `state` 都不进。验收时用路由把 11-25 的响应延迟 1.5 秒，先点 11-25 再点 11-26，最终三面板停在 11-26，且日志里没有 11-25 的 first-round 请求（过期的那次在第一道守门处就被丢弃）。
  - 验收读数：区间「近 7 天 · 首轮 96 条 · 19 个根会话」→ 选中 11-26「2026-11-26 · 首轮 11 条 · 6 个根会话」→ 取消后与初始逐字一致；三个维度接口的请求均带 `dates=`；控制台 0 报错。
  - 工具层踩坑（已绕过，见项目 MEMORY）：`npx` 在本机解析到 `npx.ps1`，它在 `-Command` 路径下会重新解析调用处的源码文本来拼参数，参数是变量或 `@args` 展开时 CLI 一个参数都收不到，须改用 `npx.cmd`；`run-code` 需要传函数 `async (page) => { … }`，复杂片段用 `--filename` 落文件以避开 `cmd.exe` 转义。
- **T8**：README API 表三行加 `dates`，表后补一段三个接口共用的口径说明（给出时忽略 `range`、最多 32 个、回显 `dates`、格式错误 400、首轮口径不变）。
- **T9**：Implement 完成后由用户确认追加（spec 原先未含）。版本串只有 `Handler.server_version` 一处；发布说明如实把首轮分桶写成修复——v1.0.2 的说明已经写了「先取整再分桶」，但代码到本版才真正做到。运行时实测 `Server` 响应头为 `MiMoTokenStats/1.0.3`。
- **Review**：独立评审结论 PASS（无 critical、无 major），6 条 minor。已修的 3 条都是本次交付自己写出来的：
  - 缓存键用 `|` 与 `,` 裸拼接用户输入，`range=7|2026-11-26` 能命中 `dates` 模式的缓存，反过来还能污染正常请求（评审实测复现）。三处改用 `json.dumps`，`test_cache_key_cannot_be_forged` 先红后绿。
  - 三个面板的副标题按「选中了什么」而非「展示的是哪份数据」来写，请求失败时会写着选中日期、数字却是区间。改为跟随数据自带的 `dates`；浏览器里掐断三个接口的请求后实测，副标题留在区间标签。
  - 发布说明里「返回与 v1.0.2 一致」与同文的分桶修复自相矛盾，已勘误；补记 `days-hourly` 的 `by_model` 也带 `pct`。
  - 另补 `agent + dates` 组合测试、修正未导入的类型注解与照抄生产表达式的期望值，测试 41 条。
  - 未处理的评审项（既有问题，原超出本 spec；用户已确认追加为 T10–T13）：agent 下拉连改的竞态（`app.js` 的 `firstround-agent` change 处理不经过守门）；选中日期时该 agent 当日无数据会被清空筛选（与「先校正再取首轮」的既有设计决定相关）；`/css/../server.py` 等仓库根目录内的文件可经静态路由未登录读取（`main` 上同样存在）。
- **评审跟进（合并前追加，用户确认）**：评审留下的既有问题里，`main` 上同样存在的四项由用户确认一并修掉（T10–T13），作为本 spec 的修订。
- **T10**：先用未鉴权的 HTTP 用例复现——`/css/../server.py` 在旧实现上回 200。修复后回 403，`StaticPathTest` 之外的放行目录文件仍免鉴权可取，测试 43 条。
- **T11–T13 的基线**：三项都写不了单测（仓库没有 JS 测试框架），证据改成有头浏览器对照——先在未改动的前端（`e1291cd`）上跑一遍留底：拖慢先发的响应后，表格变成 `checkpoint-writer-1` 而下拉是 `main`；选 `checkpoint-writer-1` 再点 2026-11-23，筛选被清空；选中日期后点一次刷新，`/api/agents` 发了 2 次。改完后跑同一个脚本对照。
- **T11**：加 `frReqId` 后，慢响应到达时表格仍是 `main`；T12 / T13 的读数与基线相同，说明这项改动没有波及它们。
- **T12**：是否保留下拉选项，看的是响应自带的 `dates`，而不是「当前有没有选中日期」——维度接口请求失败时 `state.agents` 还是区间的那份，按后者判断会给它错贴「所选日期无数据」的标注。
- **T11 补遗**：第一版用 `frReqId` 按「先后次序」判过期，推演时发现过严——维度加载正卡在 `/api/agents` 上时切换 agent，独立的首次请求与维度加载随后发出的请求口径相同，先到的正确响应会被后发的同口径请求挤掉，表格得多等一次往返（浏览器实测：切换后 2.5 s 表格仍是上一个 agent）。改为响应回来时按当时的范围 / 日期 / agent 重新对一遍口径，同口径的响应不再丢弃；`frReqId` 随之删除，不新增状态。
- **T13**：`MAX_DATES` 的测试在默认值下新旧实现都过（纯重构），是否有牙是用「把常量临时改成 20」验证的——旧实现写死 32 会红；评审指出这一步靠人工、CI 复现不了，已把「改小常量」挪进测试本身。同窗口去重后，单次刷新的 `/api/agents` 由 2 次降到 1 次；去重包装动的是 `loadDimensions` 本身，所以把 T7 的日期连点竞态（11-25 的响应拖慢 1.5 s）重新跑了一遍，三面板仍停在 11-26。
- **Review（第二轮，增量 `e07866a..83e8702`）**：独立评审结论 PASS WITH NOTES（无 critical、无 major），四条 acceptance 均有证据。6 条 minor / nit 的处置：
  - 已修：`MAX_DATES` 的测试在默认值下区分不了新旧实现，而「改小常量」的验证靠人工、CI 复现不了——把改小常量挪进测试本身，期望值用字面量；发布说明里「不在放行目录内的路径现在回 403」说过头了，勘正为只对三个前缀下的路径成立。
  - 记录、不改：同窗口去重会让紧随在途请求之后的刷新沿用那次请求的数据（方案固有的取舍，下一次刷新即更新）；维度请求失败、`state.agents` 仍是旧的日期模式响应时，下拉可能残留「所选日期无数据」标注（此时面板展示的也还是那份数据，标注与所展示的一致）；`_resolve_static` 在 Windows 上大小写不敏感而路由前缀判断敏感（不构成绕过）；`get_days_hourly` 收到重复日期时首次请求会重复计算（既有问题，前端不会发重复日期）。
