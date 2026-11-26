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

会话详情抽屉的「每次请求」只画最近 50 条，底部提示写死 `显示最近 50 / 596 条`——第 51 条以后的请求在界面上没有任何入口。而 `/api/session` 其实**返回该会话的全部消息**（`session_detail()` 里没有截断），50 是前端 `all.slice().reverse().slice(0, 50)` 的渲染上限。本机最大的会话有 **1,268 条**消息，一次全画会明显卡顿，所以需要「先画一批、滚到底再追加」。

每张卡片现在只有模型名（`glm-5.4`）。而 `message.data` 里还有两个对排查很有用的字段没被用上：`providerID`（服务商，实测四类：`kg-newapi` 2235 条 / `knox-chat` 1382 / `zhipuai-coding-plan` 355 / `gemini` 28）与 `variant`（模型变体，对应 MiMo-Code 的 `reasoningEffort`，实测只有 `max` 或无该字段）。同一个会话里模型相同、服务商或思考强度不同的请求现在看不出区别。

## [S2] Design

### S2.1 服务端：每条请求带服务商与思考强度

`parse_tokens()` 的返回值增加两个键（不改既有键）：

- `provider` ← `data.providerID`，缺失为 `None`
- `variant` ← `data.variant`，缺失为 `None`

`session_detail()` 把这两个键写进每条 `turns[]`，字段名 `provider` / `variant`（前端按它渲染）。其余接口（`aggregate` / `export_csv` 等）不消费这两个键，行为不变；`/api/session` 的顶层与 `totals` 键集合也不变（只加 turn 内的键）。

接口新增字段 ⇒ 版本串升 `MiMoTokenStats/1.0.5` + `RELEASE_NOTES_v1.0.5.md`（不并入 1.0.4）。

### S2.2 卡片上的三项信息

每张请求卡片在「模型名 + 消耗」这一行下面加一行 meta，三项都显示：

```
glm-5.4                                    23.43万 · 命中 100%
[kg-newapi]  [思考 max]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
入/出/缓存 754 / 1 / 233.33万
11-26 22:45:55
```

- 模型名保持原样（`.t-model`，已有样式与省略规则不动）。
- 服务商用中性 chip：`background: var(--surface-2)`、`border: 1px solid var(--border-subtle)`、`color: var(--text-2)`、`border-radius: 999px`、`padding: 1px 8px`、`font-size: 10.5px`，`title="服务商 kg-newapi"`。
- 思考强度用紫色调 chip：`color: var(--violet)`、`background: rgba(167,139,250,0.14)`、`border: 1px solid rgba(167,139,250,0.34)`，文案 `思考 max`，`title="思考强度 max（模型的 variant）"`；`variant` 缺失时文案 `思考 默认`，用中性 chip 样式（与「没有配置思考强度」一致，不冒充 `max`）。
- 两项都缺失（老数据）时整行 meta 不渲染，卡片高度与现在一致。
- 深色 / 浅色两套主题都要可读（用已有变量与固定 alpha，不新造颜色变量）。

### S2.3 滚动加载

- 数据源不变：`/api/session` 一次返回全部 `turns`。前端按**倒序**（最新在上）保留在 `state.drawerTurns`，只渲染前 `state.drawerShown = 50` 条。
- 滚动容器是抽屉的 `.modal-body`（`overflow-y: auto`）。监听它的 `scroll`（passive），满足 `scrollTop + clientHeight >= scrollHeight - 240` 时追加下一批 50 条。
- 追加只拼接新卡片（`insertAdjacentHTML`），**不重写已渲染节点**——避免滚动位置跳动，也避免每追加一批就重排全部卡片。
- 追加后若容器仍不可滚动（内容不足一屏）且还有剩余，继续追加，直到可滚动或加载完（防「永远触不到底」）。
- 顶部提示从 `显示最近 50 / N 条` 改成 `已显示 50 / 1,268 条`，每次追加更新；全部加载完改成 `共 1,268 条`。图例（入/出/缓存）只在首次渲染时插入一次。
- 打开抽屉时重置 `drawerTurns` / `drawerShown`；关闭抽屉时清空并移除滚动监听。
- 空态与失败态文案不变。

## [S3] Out of Scope

- 模型分布那几行不加服务商 / 思考强度（本次只改「每次请求」列表）。
- 不改成服务端分页（`/api/session` 本来就全量，1,268 条约 230 KB，缓存命中是一次列表查询）。
- 不做虚拟列表（只做增量渲染；50 条一批、最多 26 批）。
- 抽屉内不加搜索 / 筛选；不改 `/api/session` 的缓存策略。

## Tasks

- [x] T1: 服务端 turn 加 `provider` / `variant` — acceptance: `parse_tokens` 返回这两个键（缺字段为 `None`），`session_detail` 的每条 `turns[]` 带上它们；测试 fixture 的 `_assistant()` 支持传入 provider/variant，新增用例断言值正确且缺省为 `null`；其余既有用例仍全绿 (covers: S2.1)
- [ ] T2: 卡片渲染三项信息与样式 — acceptance: 有头场景里卡片同时显示模型名、服务商 chip、`思考 max` chip；`variant` 缺失时显示 `思考 默认` 并用中性样式；两项都缺时不出 meta 行；深色与浅色主题都清晰可读 (covers: S2.2)
- [ ] T3: 滚动加载 — acceptance: 打开一个上千条消息的会话，首屏只渲染 50 条且提示 `已显示 50 / N 条`；滚到底自动追加到 100、150…，滚动位置不跳、已渲染卡片不重排；全部加载完提示变 `共 N 条`；关闭再打开计数归零 (covers: S2.3)
- [ ] T4: 版本串 1.0.5 + 发布说明 + README — acceptance: `Server` 头为 `MiMoTokenStats/1.0.5`，`RELEASE_NOTES_v1.0.5.md` 写明两项改动，README 的 `/api/session` 行提到 `provider` / `variant` (covers: S2.1)

## Journey log

- 2026-11-26 先查数据再设计：`message.data` 里 `providerID` / `variant` 都存在且分布合理（`variant` 实测只有 `max` 或缺失），所以「思考强度」就用 `variant`，不做推断；`/api/session` 也已返回全部消息（最大会话 1,268 条），因此只改前端渲染策略 + 给 turn 补字段，不动分页。
