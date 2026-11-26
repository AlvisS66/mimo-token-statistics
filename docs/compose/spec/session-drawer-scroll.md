---
feature: session-drawer-scroll
status: in-progress
updated: 2026-11-26
source: feat/session-drawer-scroll
---

# 会话抽屉：消息列表滚动加载 + 模型/服务商/思考强度

## Report

[Not finalized yet]

## [S1] Problem

会话详情抽屉的「每次请求」只画最近 50 条，底部提示写死 `显示最近 50 / 596 条`——第 51 条以后的请求在界面上没有任何入口。50 是前端 `all.slice().reverse().slice(0, 50)` 的渲染上限，而 `/api/session` 里还有一处 `turns[-200:]`（实测这条把上限压在 200：滚到底后提示停在 `已显示 200 / 995 条`，见图与 T3 的用例），所以「全部」还要先把这个截断去掉。本机最大的会话有 **1,268 条**消息，一次全画会明显卡顿，所以需要「先画一批、滚到底再追加」。

每张卡片现在只有模型名（`glm-5.4`）。而 `message.data` 里还有两个对排查很有用的字段没被用上：`providerID`（服务商，实测四类：`kg-newapi` 2235 条 / `knox-chat` 1382 / `zhipuai-coding-plan` 355 / `gemini` 28）与 `variant`（模型变体，对应 MiMo-Code 的 `reasoningEffort`，实测只有 `max` 或无该字段）。同一个会话里模型相同、服务商或思考强度不同的请求现在看不出区别。

## [S2] Design

### S2.1 服务端：每条请求带服务商与思考强度

`parse_tokens()` 的返回值增加两个键（不改既有键）：

- `provider` ← `data.providerID`，缺失为 `None`
- `variant` ← `data.variant`，缺失为 `None`

`session_detail()` 把这两个键写进每条 `turns[]`，字段名 `provider` / `variant`（前端按它渲染）。其余接口（`aggregate` / `export_csv` 等）不消费这两个键，行为不变；`/api/session` 的顶层与 `totals` 键集合也不变（只加 turn 内的键）。

接口新增字段 ⇒ 版本串升 `MiMoTokenStats/1.0.5` + `RELEASE_NOTES_v1.0.5.md`（不并入 1.0.4）。

### S2.2 卡片上的三项信息

每张请求卡片的三项信息放在**同一行**（2026-11-26 用户追加要求「服务商/思考强度与模型放一行」）：

```
glm-5.4  [kg-newapi]  [思考 max]            23.43万 · 命中 100%
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
入/出/缓存 754 / 1 / 233.33万
11-26 22:45:55
```

- 模型名保持原样（`.t-model`，已有省略规则）；模型名与两个 chip 包在 `.t-head` 里（`display:flex; gap:6px; min-width:0; flex-wrap:wrap`），窄屏真放不下时 chip 折到下一行，不横向溢出。
- 服务商用中性 chip：`background: var(--surface-2)`、`border: 1px solid var(--border-subtle)`、`color: var(--text-2)`、`border-radius: 999px`、`padding: 1px 8px`、`font-size: 10.5px`，`title="服务商 kg-newapi"`。
- 思考强度用紫色调 chip：`color: var(--violet)`、`background: rgba(167,139,250,0.14)`、`border: 1px solid rgba(167,139,250,0.34)`，文案 `思考 max`，`title="思考强度 max（模型的 variant）"`；`variant` 缺失时文案 `思考 默认`，用中性 chip 样式（与「没有配置思考强度」一致，不冒充 `max`）。
- 两项都缺失（老数据）时整行 meta 不渲染，卡片高度与现在一致。
- 深色 / 浅色两套主题都要可读（用已有变量与固定 alpha，不新造颜色变量）。

### S2.3 滚动加载

- 数据源：`/api/session` 一次返回全部 `turns`（删掉 `turns[-200:]` 这处静默截断；聚合与 `totals` 本来就按全量算，删掉不增加扫描成本，最大的会话约 1,268 条约 270 KB）。前端按**倒序**（最新在上）保留在 `state.drawerTurns`，只渲染前 `state.drawerShown = 50` 条。
- 滚动容器是抽屉的 `.modal-body`（`overflow-y: auto`）。监听它的 `scroll`（passive），满足 `scrollTop + clientHeight >= scrollHeight - 240` 时追加下一批 50 条。
- 追加只拼接新卡片（`insertAdjacentHTML`），**不重写已渲染节点**——避免滚动位置跳动，也避免每追加一批就重排全部卡片。
- 追加后若容器仍不可滚动（内容不足一屏）且还有剩余，继续追加，直到可滚动或加载完（防「永远触不到底」）。
- 顶部提示从 `显示最近 50 / N 条` 改成 `已显示 50 / 1,268 条`，每次追加更新；全部加载完改成 `共 1,268 条`。图例（入/出/缓存）只在首次渲染时插入一次。
- 打开抽屉时重置 `drawerTurns` / `drawerShown`；关闭抽屉时清空并移除滚动监听。
- 空态与失败态文案不变。

### S2.4 模型分布也要显示服务商（2026-11-26 用户追加）

抽屉里的「模型分布」每行现在是 `模型名` + 右侧统计，看不出这个模型的请求走的是哪家服务商。同一个模型可能经不同服务商（实测 `deepseek-v4.1-flash` 既有 `knox-chat` 也有 `kg-newapi`），所以：

- `session_detail()` 的 `models[]` 每行新增 `providers`：该模型用过的服务商，按条数降序去重（没有服务商的老数据为空数组）。
- 前端在模型名右边按顺序渲染同款 `t-chip-prov` 小胶囊；名字太长时 chip 换行，模型名自己省略。
- 只加服务商，不加思考强度（`variant` 是逐条请求的，按模型聚合会误导）。

## [S3] Out of Scope

- 模型分布那几行不加思考强度（`variant` 逐条不同，聚合成一个值会误导）；服务商见 S2.4。
- 不改成服务端分页（`/api/session` 本来就全量，1,268 条约 230 KB，缓存命中是一次列表查询）。
- 不做虚拟列表（只做增量渲染；50 条一批、最多 26 批）。
- 抽屉内不加搜索 / 筛选；不改 `/api/session` 的缓存策略。

## Tasks

- [x] T1: 服务端 turn 加 `provider` / `variant` — acceptance: `parse_tokens` 返回这两个键（缺字段为 `None`），`session_detail` 的每条 `turns[]` 带上它们；测试 fixture 的 `_assistant()` 支持传入 provider/variant，新增用例断言值正确且缺省为 `null`；其余既有用例仍全绿 (covers: S2.1)
- [x] T2: 卡片渲染三项信息与样式 — acceptance: 有头场景里卡片在**同一行**显示模型名、服务商 chip、`思考 max` chip；`variant` 缺失时显示 `思考 默认` 并用中性样式；两项都缺时这一行只有模型名；深色与浅色主题都清晰可读，390 px 宽不横向溢出 (covers: S2.2)
- [x] T3: 滚动加载 — acceptance: 服务端不再截断 `turns`（删 `turns[-200:]`，用 210 条请求的用例锁住）；打开一个上千条消息的会话，首屏只渲染 50 条且提示 `已显示 50 / N 条`；滚到底自动追加到 100、150…，滚动位置不跳、已渲染卡片不重排；全部加载完提示变 `共 N 条`；关闭再打开计数归零 (covers: S2.3)
- [ ] T4: 版本串 1.0.5 + 发布说明 + README — acceptance: `Server` 头为 `MiMoTokenStats/1.0.5`，`RELEASE_NOTES_v1.0.5.md` 写明两项改动，README 的 `/api/session` 行提到 `provider` / `variant` (covers: S2.1)
- [x] T5: 模型分布行显示服务商（用户追加）— acceptance: `session_detail()["models"][]` 带 `providers`（按条数降序、无服务商为空数组），前端模型名右边显示同款 chip；测试 fixture 里同一模型有 `prov-a` 与缺失两种请求时用例能区分 (covers: S2.4)

## Journey log

- 2026-11-26 先查数据再设计：`message.data` 里 `providerID` / `variant` 都存在且分布合理（`variant` 实测只有 `max` 或缺失），所以「思考强度」就用 `variant`，不做推断；`/api/session` 也已返回全部消息（最大会话 1,268 条），因此只改前端渲染策略 + 给 turn 补字段，不动分页。
- 2026-11-26 实现 T3 时发现 `session_detail()` 里还有一处 `turns[-200:]`：有头场景滚到底停在 `已显示 200 / 995 条`，与 S1 原先「接口返回全部」的判断不符 ⇒ 先补失败用例（fixture 里插 210 条请求，旧实现回 200）再删掉截断，用例全绿后场景才通过。
- 2026-11-26 用户追加两条 UI 要求（截图指认）：① 服务商 / 思考强度与模型名放**同一行**（原设计是另起一行 meta）；② 抽屉的「模型分布」每行也要显示服务商。已原地扩写 S2.2、新增 S2.4 与任务 T5（不重编号）。
- 2026-11-26 T2/T3/T5 实现与验收（同一份有头场景 `drawer.js` / `models.js` / `theme.js`，放在系统临时目录、不进仓库）：995 条的会话首屏 50 条（提示 `已显示 50 / 995 条`）→ 滚到底逐批追加 → `共 995 条`，全程 1.1 s、滚动位置不跳；卡片 `lineOffsets` 实测 `[0,0]`（chip 与模型名中线零偏差）；`模型分布` 每行带服务商，`ses_ffe5e5a6c…` 的 `deepseek-flash` 列出 `kg-newapi` + `deepseek` 两家；两项都缺的老数据（用 `page.route` 抹掉最后一条请求的两个字段构造）只显示模型名；深/浅主题对比度实测 服务商 5.59 / 3.93、思考强度 6.57 / 4.33；390 px 宽卡片与模型行均无横向溢出。
