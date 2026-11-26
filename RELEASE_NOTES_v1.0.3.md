## MiMo Token Statistics v1.0.3

v1.0.2 的复审后续：把三个新面板接上日历联动，修掉一批口径与安全问题，并把回归测试沉淀进仓库。命中率算法没有改：仍然是 `cache.read / (cache.read + cache.write + input)`，三个数全部取服务商上报值。

### 新增

- **日历联动**：在热力图上选中某一天后，Agent 用量 / 首轮命中 / 会话归并三个面板按该日重新汇总，副标题显示选中日期；取消选中后恢复为区间汇总。此前这三块只按顶部时间范围汇总
- **`dates` 参数**：`GET /api/agents`、`/api/first-round`、`/api/sessions-root` 接受可选的 `dates`（逗号分隔 `YYYY-MM-DD`，最多 32 个）
  - 给出时忽略 `range`，按消息的本地日期筛选
  - 响应新增 `dates` 字段（未传时为 `null`）；该模式下 `range_label` 为选中日期，`since` 为最早选中日的本地零点
  - 格式错误回 `400`
  - 首轮口径不变：`dates` 只筛首轮发生的时间，不改变哪条是首轮；`pending` 同样按 `dates` 筛
- `GET /api/overview` 的 `by_model` 每行新增 `pct`（相对第一名的百分比），与 `by_project` / `by_session` 对齐
- `GET /api/export.csv` 在末尾新增 `agent` 列，既有列顺序不变
- 仓库内新增回归测试 `tests/test_api_shape.py`（39 条，纯标准库 `unittest`，运行 `python -m unittest discover -s tests`）：锁定各接口的响应字段集合、首轮口径、`dates` 筛选与缓存隔离、静态文件路径边界

### 修复

- **排行条宽**：模型 / 项目 / 会话 / Agent / 会话归并五处的条形图宽度改用 `pct`（相对最大值）。此前用的是 `share`（占总量的比例），第一名的条只有它那份占比那么宽
- **首轮分桶**：改为命中率先取整再分桶。此前命中率在 0 与 0.5% 之间的首轮会显示 0%，却落进 1–50% 桶；现在显示 0% 的一定在 0% 桶，与 v1.0.2 发布说明写明的口径一致
- **会话归并表**：「子会话」一列改为子会话数量。此前填的是归并后的会话总数（含根本身），没有子会话的行显示 1，与同一行的「无子会话」自相矛盾
- **Agent 展开状态**：切换时间范围时一并收起；展开的 agent 记在 `sessionStorage`，刷新页面后保留
- **静态文件路径检查**：从字符串前缀匹配改为按路径段判断。此前 `foo-x/` 这样的同名前缀兄弟目录会被当成 `foo/` 的子路径放行，越界路径现在回 `403`
- **移除多余的 CORS 头与 OPTIONS**：所有响应不再带 `Access-Control-Allow-Origin: *`，`OPTIONS` 请求回 `501`（页面与接口同源，不存在预检）
- `get_user_quota` 的 Cookie 临时副本改用 `NamedTemporaryFile` 并在 `finally` 中清理。此前在「副本已落盘、随后读库失败」时会留下临时文件
- 删除从未被调用的 `_parse_message_rows`，以及 `aggregate()` 里一段什么都不做的空分支

### 兼容性

- 接口字段**只增不改**：没有删除或改名任何响应字段
- 不传 `dates` 时，三个维度接口的返回与 v1.0.2 一致，只多一个 `dates: null`；`range` 的行为不变
- **唯一的破坏性变更是去掉 CORS 头**：若此前有其它站点跨源直连这些接口，浏览器会拦截。面板本身同源，不受影响
- 改动涉及 `server.py`，需要重启进程才能生效（聚合缓存是进程内全局，`/api/refresh` 不会重载模块）

### 安装

1. 下载 Source code zip 或 `git clone`
2. 需要 Python 3.9+
3. `python server.py` 或双击 `启动统计服务.bat`
4. 打开 http://127.0.0.1:8765，默认密码 `root`

### 命令行

```bash
python server.py
python server.py --password 强口令
python server.py --no-auth
python server.py --host 127.0.0.1
```
