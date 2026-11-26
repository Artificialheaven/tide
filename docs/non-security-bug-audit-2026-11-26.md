# 非安全类 Bug 审查报告

- **审查日期：** 2026-11-26
- **审查对象：** `/home/ubuntu/tide` 当前工作树（包含未提交代码改动）
- **范围：** 功能、稳定性、并发、异步调用、数据/队列处理及 UI/API 契约；按要求不检查或报告安全问题。
- **结论：** 确认 **9 项**非安全类缺陷：4 项高、5 项中。主要集中在最近增加的 Web 审批与仪表盘、Web 面板静态资源、权限热更新、子代理工具调度与权限、私聊长跑、批调度超时与提醒队列。
- **修复状态：** 9 项已于 2026-11-26 全部修复，新增 49 个回归用例；全量测试 291 passed（原 85）。

## 摘要

| ID | 严重度 | 位置 | 简述 |
|---|---|---|---|
| B-01 | 高 | `plugins/deepseek/templates/dashboard.html:36,56,66` | 仪表盘三个 API 使用错误的根路径，KPI/健康检查/重连不可用 |
| B-02 | 高 | `plugins/deepseek/Core/web.py:126-140` | Web 审批路由以错误参数调用异步 `decide`，批准/拒绝请求失败 |
| B-03 | 高 | `plugins/deepseek/Core/engine.py:1215-1218,1228-1255` | 只读批调组超时会写入重复 tool 结果，污染消息历史 |
| B-04 | 高 | `plugins/deepseek/Core/permissions.py:29-35,84-87` | 权限配置热更新后仍使用初始化时的旧规则 |
| B-05 | 中 | `plugins/deepseek/Core/subagent.py:149-178` | 子代理把模型工具名与内部工具 key 直接比较，所有工具调用都被拒绝 |
| B-06 | 中 | `plugins/deepseek/Core/loop_run.py:75-90` | 非主人私聊长跑被识别成群聊，启动/完成通知走错消息类型 |
| B-07 | 中 | `plugins/deepseek/Core/tasks.py:190-204`、`plugins/deepseek/Core/storage.py:2127-2135` | 关闭群的到期提醒长期占据 LIMIT 100，后续提醒饥饿 |
| B-08 | 高 | `plugins/deepseek/Core/permissions.py:150-170`、`Core/engine.py:2170-2190`、`Core/subagent.py:_tool_ctx` | 角色默认 `member_action=deny` 套在所有工具上：子代理（无 sender_id → member）连 `fs.read` 都被拒，普通成员的只读工具同样全灭 |
| B-09 | 高 | `ui/web/server.py:22-24`、`ui/web/templates/index.html:244` | 静态根指向 `templates/` 且未设 `static_url_path`，`/static/…` 全 404：所有继承 base 的页面（含群开关）无 CSS、本地 socket.io 加载失败 |

## 修复记录（2026-11-26）

| ID | 修复内容 | 改动位置 | 回归测试 |
|---|---|---|---|
| B-01 | 新增 `const BP = '/deepseek'`，三个接口改用 `BP + '/api/...'`，与同页 `/deepseek/api/groups` 一致 | `plugins/deepseek/templates/dashboard.html:35-38,42,62,72` | `test_dashboard_api_paths_use_blueprint_prefix` |
| B-02 | 改关键字传参并用 `_run_async` 桥回主事件循环等待结果；插件未加载返回 503 | `plugins/deepseek/Core/web.py:127-145` | `test_web_approval_decide_awaits_async_decide` |
| B-03 | 删掉 `_exec_one` 的 CancelledError 补占位（组超时会 cancel 单个 task 但批调仍正常返回）；整轮取消仍由 `run_batch` 外层统一补齐 | `plugins/deepseek/Core/engine.py:1215-1221` | `test_batch_group_timeout_single_result_per_call`、`test_chat_loop_fills_missing_results_only_outside_batch` |
| B-04 | `refresh()` 同时重建 `self.permissions` 规则快照 | `plugins/deepseek/Core/permissions.py:33-40` | `test_permissions_refresh_applies_new_rules` |
| B-05 | 新增 `tool_key_of()` 做函数名→内部 key 映射；`_schemas` 与执行期白名单判断都走映射 | `plugins/deepseek/Core/subagent.py:33-40,124-136,168-173` | `test_subagent_maps_tool_name_to_internal_key`、`test_subagent_loop_dispatches_readonly_tool` |
| B-06 | `LoopManager._chat_type()` 直接读原始 `settings.chat_type`，不再经 `resolve_group()`（其白名单不含该键） | `plugins/deepseek/Core/loop_run.py:76-97` | `test_loop_chat_type_reads_raw_settings` |
| B-07 | 关闭群分支也调用 `snooze_commitment(now + 3600)`：不标记完成（重开仍提醒），但不再占住查询窗口 | `plugins/deepseek/Core/tasks.py:198-209` | `test_disabled_group_commitments_are_snoozed`、`test_due_commitments_window_not_starved_by_disabled_groups` |
| B-08 | 角色默认（owner/admin/member_action）改为只约束风险工具，安全/只读工具对非主人恢复放行；新增 `readonly` 角色（`ToolContext.actor_role`），子代理只放行 `READONLY_TOOL_KEYS`，写/发送在 permissions 与 engine 两层拦截 | `plugins/deepseek/Core/permissions.py:96-190`、`Core/engine.py:2024-2070`、`Core/builtin_tools.py:1529-1535`、`Core/subagent.py:_tool_ctx` | `tests/test_subagent_permissions_2026_11_26.py`（13 例）、`tests/test_subagent_extra_tools_2026_11_26.py`（17 例） |
| B-09 | 静态资源独立到 `ui/web/static/`，Flask 显式 `static_folder=ui/web/static` + `static_url_path="/static"`；socket.io 移到 `static/vendor/socket.io.min.js` 并同步 `index.html` | `ui/web/server.py:22-27`、`ui/web/templates/index.html:244`、`ui/web/static/{tds,vendor}/` | `tests/test_web_static_2026_11_26.py`（9 例） |

验证：`/tmp/tvenv/bin/python -m pytest tests/ -q` → **95 passed**（B-01..B-07 修复时）；B-08 修复后全量 **251 passed**，额外工具授权补充后全量 **268 passed**；改动文件 `py_compile` 通过；全部 Jinja 模板解析通过；`dashboard.html` 内联 JS 通过 `node --check`。

## 详细发现

### B-01 仪表盘 API 请求路径未包含插件 Blueprint 前缀

- **严重度：高；置信度：高。**
- `plugins/deepseek/templates/dashboard.html:36` 请求 `/api/overview`；第 56 行请求 `/api/llm-health`；第 66 行请求 `/api/reconnect`。
- 这些路由定义在 `plugins/deepseek/Core/web.py:546-589` 的 DeepSeek Blueprint 下。框架注册 Blueprint 时会增加 `/deepseek` 前缀，因此实际端点为 `/deepseek/api/overview`、`/deepseek/api/llm-health`、`/deepseek/api/reconnect`。同一模板第 71 行群列表请求正确使用 `/deepseek/api/groups`，也印证了实际路径约定。
- **触发/影响：** 打开仪表盘时 KPI 和趋势无法加载；点击“模型健康”或“重连”会请求不存在的根路径并失败。
- **建议：** 使用带 `/deepseek` 前缀的 URL 或由模板生成 Blueprint 路径，并补 Flask 路由集成测试，断言仪表盘引用地址均可命中。

### B-02 Web 审批路由调用 `decide` 的参数契约不符

- **严重度：高；置信度：高。**
- `plugins/deepseek/Core/web.py:135-136` 调用 `plugin.engine.approvals.decide(aid, approved, 'web-admin')`。
- 实际方法 `plugins/deepseek/Core/approval.py:93-98` 定义为 `async def decide(self, approval_id, *, approved, decided_by='web')`：`approved` 和 `decided_by` 是仅限关键字参数。因此当前位置参数调用直接抛 `TypeError`，路由捕获后返回 HTTP 500。即使改为关键字传参，视图仍须在线程安全地调度并等待协程，不能在普通同步 Flask view 中直接调用异步函数。
- **触发/影响：** 审批页面点击批准或拒绝均无法完成决策，审批保持 pending，直到超时/其他决策路径处理。
- **建议：** 在 Flask view 中把 coroutine 提交到 `core.globals.event_loop` 并等待结果，或将同步桥封装为专用审批函数；测试 HTTP 状态码、DB 状态及等待中的 future 是否唤醒。

### B-03 批调组超时会为同一 tool_call 写入重复结果

- **严重度：高；置信度：高。**
- `plugins/deepseek/Core/tool_batch.py:103-117` 对并发只读组使用 `wait_for(gather(...), timeout=...)`；超时后取消未完成 task，再把超时文本放入 `BatchOutcome` 并正常返回。
- 但 `plugins/deepseek/Core/engine.py:1215-1218` 每个被取消的 `_exec_one` 都调用 `_fill_missing_tool_results(gid, messages, wire, answered, ...)`。此时本批结果尚未在第 1240-1255 行统一追加，`answered` 也还没有更新，因此 helper 可能先给整批 wire calls（包括已经完成的调用）写入占位 tool 行。随后批调正常返回，engine 又把每项实际/超时结果追加一次。
- **触发/影响：** 只读工具执行超过 `tool_parallel_group_timeout`（默认 35 秒）且该批含多个调用时，历史中同一 `tool_call_id` 可出现“本轮被中止”和正常/超时结果两条记录；下一轮模型请求可能收到不完整或重复的工具调用链。
- **建议：** 区分“整轮取消”和“单组超时”：仅在整轮被取消时调用 `_fill_missing_tool_results`；批组超时应由 `BatchOutcome` 统一生成唯一占位结果。增加一个快任务+一个超时任务的回归用例，检查每个 call ID 恰有一条 tool 消息且 DB/内存一致。

### B-04 权限引擎 `refresh()` 没有刷新规则快照

- **严重度：高；置信度：高。**
- `plugins/deepseek/Core/permissions.py:29-30` 在构造时把 `cfg['permissions']` 复制进 `self.permissions`；第 33-35 行的 `refresh()` 只改 `self.cfg`，未更新 `self.permissions`。`decide()` 第 86 行之后读取的仍是旧字典。`engine.py:1854` 每次工具派发虽然调用 `refresh(cfg)`，但不能因此更新规则。
- **复现：** 初始化 `default_action=allow`，调用 `refresh(default_action=deny)` 后再 `decide()`，结果仍为 `allow`（已通过 Python 小脚本复现）。
- **触发/影响：** 运行中通过设置/配置重载修改权限规则、默认动作、审批策略后，新配置不会生效，直到重建 PermissionEngine/插件。
- **建议：** `refresh()` 同步重建 `self.permissions = dict(self.cfg.get('permissions') or {})`；补充同实例 allow→deny、rules 变更及启停 ask 的热更新测试。

### B-05 子代理的工具名与白名单 key 使用不同命名空间

- **严重度：中；置信度：高。**
- 子代理模型收到的函数名来自 `plugins/deepseek/Core/builtin_tools.py`，如 `fs_read`、`qq_member_info`；只读白名单 `plugins/deepseek/Core/tool_batch.py:18-26` 存的是 `fs.read`、`qq.member_info` 等内部 key。
- `plugins/deepseek/Core/subagent.py:149-167` 从模型结果取得函数名后，直接用 `name not in READONLY_TOOL_KEYS` 判断，没有通过 `builtin_tools.TOOL_KEYS` 映射。因此所有模型返回的内置只读工具名都会进入拒绝分支。
- **触发/影响：** 子代理可在无工具时直接回答；但凡它尝试调用任何只读工具，都会收到“无权调用”，无法完成需检索/读取的任务。schema 仍暴露这些工具，表现为模型不断尝试后空答或达到轮数上限。
- **建议：** 执行前将模型工具名映射成内部 key 后再比白名单；工具 dispatch 仍使用模型名。增加 fs_read/history_query 等工具的端到端子代理用例。

### B-06 私聊长跑的 `chat_type` 在配置解析时丢失

- **严重度：中；置信度：高。**
- `plugins/deepseek/deepseek.py` 私聊入口将 `settings.chat_type='private'` 写入群记录；但 `plugins/deepseek/Core/loop_run.py:81-85` 读取记录后调用 `resolve_group()`，再从结果读取 `chat_type`。`plugins/deepseek/Core/config.py:298-318` 的 `GROUP_OVERRIDABLE` 不包含 `chat_type`，而 `resolve_group()` 只复制白名单键（第 625-644 行），所以标记被丢弃。`loop_run.py:89-90` 随后对非主人 QQ 回退为 `group`。
- **触发/影响：** 普通好友私聊中的长跑任务按群聊类型发启动/完成通知，目标好友不是群时投递失败；任务结果通知不可达。非主人私聊的错误路径已通过实际解析代码核对。
- **建议：** `chat_type` 属于会话传输元数据，不应经群级配置白名单解析；直接从持久化 row/settings 读取，或使用 `loop_sessions.chat_type` 并保证启动任务时写入。补普通好友私聊长跑测试。

### B-07 关闭群的到期 commitment 会挤占提醒查询窗口

- **严重度：中；置信度：高。**
- `plugins/deepseek/Core/storage.py:2127-2133` 的 `due_commitments()` 按 `due_at` 排序且固定 `LIMIT 100`。
- `plugins/deepseek/Core/tasks.py:198-204` 对 disabled 群只 `continue`，既不标记也不顺延；群级 reminder 关闭分支在 206-211 行已使用 `snooze_commitment()`，但群本身关闭的分支漏了处理。
- **触发/影响：** 至少 100 条更早到期且属于关闭群的 commitment 长期保持未提醒，会反复占据每次查询的前 100 条；随后启用群或私聊的到期提醒不再进入批次，形成饥饿。已用 100 条旧记录 + 1 条目标记录的队列场景确认目标不在查询结果中。
- **建议：** 对 disabled 群采用可恢复的顺延/分群批处理，或从 due 查询中过滤并公平轮转；避免直接标记完成，以保留重开后提醒的现有意图。补超过 100 条积压的测试。

### B-08 角色默认 deny 套到所有工具上：子代理与普通成员连只读工具都用不了

- **严重度：高；置信度：高。**
- `plugins/deepseek/Core/config.py:266-274` 的 `DEFAULTS['permissions']` 非空（`owner_action=allow`、`admin_action=deny`、`member_action=deny`、`default_action=deny`），`permissions.py` 因此不会走 `_legacy` 分支（旧语义「只拒风险工具」），而是逐级落到角色默认。
- `engine.py` 的 `_dispatch_impl()` 对**每一个**工具调用都先执行 `_risk_denied()`；其中 `role = 'owner' if tool_ctx.is_owner else 'member'`。子代理的 `ToolContext`（`subagent.py:_tool_ctx`）没有 `sender_id`，`is_owner=False`，于是全部落到 `member_action='deny'`——`fs.read`、`history.query`、`memory.recall` 一个都过不去；普通群里非主人成员同样被全拒。
- 与设计文档冲突：`docs/agent-framework-enhancement-plan-2026-11-25.md:38` 承诺「ask 关时默认行为不变：主人放行、非主人拒**风险**工具」，实现却按 §3.3 第 5 步把角色默认套到了安全工具上。
- **修复：**
  1. `permissions.decide()` 的角色默认分支：`member/admin_action=deny` 只在工具属于 `RISKY_TOOL_KEYS` 时生效；安全/只读工具直接放行（显式 `default_action` 与显式规则不受影响，B-04 的用例仍按预期拒绝）。
  2. 新增 `readonly` 角色：`decide()` 在显式规则之后、角色默认之前处理——只读白名单（`tool_batch.READONLY_TOOL_KEYS`）放行，其余拒绝并给出「只读身份只能调用只读工具」。
  3. `builtin_tools.ToolContext` 新增 `actor_role` 字段；`subagent._tool_ctx` 两条构造路径都传 `actor_role='readonly'`。
  4. `engine._actor_role()` 统一角色推断（owner / readonly / member），`_risk_denied` 与 `before_tool` 钩子都走它；`_risk_denied` 对 readonly 再做一次纵深拦截，即使配置里被写了 allow 规则，非只读工具也过不去。
- **行为变化（有意）：** 非主人成员可以继续用只读工具与发送类工具（与 N-02 升级前一致）；子代理只能读；写/外发/风险工具仍仅主人可用。
- **后续补充（2026-11-26，同日）：子代理额外工具授权。**
  「子代理只能读」对「让它去浏览器里查点东西」这类需求过严，而 MCP 工具
  （`mcp__<server>__<tool>`）在权限引擎里一律按风险处理、自定义工具也任意
  代码。新增主人显式开关 `subagent_extra_tools`（逗号分隔、支持 `fnmatch`
  通配，默认空）：命中的工具会真正下发到子代理 `tools` 数组，并通过
  `ToolContext.allow_tools`（`builtin_tools.py:1533`）在
  `engine._risk_denied()` 放行；未命中的仍按只读身份拒绝，模型传的 `tools`
  参数只能收窄、不能提权。默认空 = 行为与纯只读子代理完全一致。
  extras 只对 MCP（`mcp__*`）与自定义（`custom__*`）工具生效，内置写/执行
  工具即使写进配置也不会下发。
  涉及：`Core/tool_batch.py`（`parse_tool_patterns` / `tool_matches` /
  `matched_any`）、`Core/subagent.py`（`_tools_for` / `_call_allowed` /
  `_tool_ctx(allow_tools)`）、`Core/engine.py:2040-2050`、
  `Core/config.py` DEFAULTS + `plugins/deepseek/web.json`。
  用例：`tests/test_subagent_extra_tools_2026_11_26.py`（17 例）。

### B-09 Web 面板静态资源全部 404：继承 base.html 的页面没有 CSS

- **严重度：高；置信度：高（Flask test client 复现）。**
- 现象：群开关页（`/deepseek/dashboard`）在浏览器里完全没有样式；DevTools 里 `tokens.css`、`app.css` 两个请求都是 404，本地 socket.io 同样 404。
- 根因：`ui/web/server.py` 建 Flask app 时 `static_folder=…/templates`，但**没有设置 `static_url_path`**。Flask 在只给 `static_folder` 时会按目录基名自动推导 URL 前缀，于是静态路由变成 `/templates/<path:filename>`；而 `ui/web/templates/base.html:7-8`、`index.html:7`、`live/debug.html`、`live/danmaku.html`、`approvals.html` 统一引用 `/static/tds/*.css`，全部 404。
- 复现：`ui.web.server._app.test_client()` 下 `GET /static/tds/tokens.css` → 404、`/static/tds/app.css` → 404、`/static/tds/legacy.css` → 404、`/static/socket.io.min.js` → 404；`url_map` 里唯一静态规则是 `/templates/<path:filename>`。
- 次生问题：`index.html` 为离线化把 CDN 换成本地 `<script src="/static/socket.io.min.js">`，但文件落在 `ui/web/templates/static/`（在 templates 目录里），即使前缀修对 URL 也对不上。
- **修复：**
  1. 静态资源独立成 `ui/web/static/{tds,vendor}/`（`tokens.css`/`app.css`/`legacy.css`/`vendor/socket.io.min.js`），Flask 改用 `static_folder=ui/web/static` 并显式 `static_url_path="/static"`。
  2. 这样也顺手消除了「templates 当静态根 → `/static/base.html` 可直接下载模板源码」的暴露面。
  3. `index.html` 的 socket.io 引用统一为 `/static/vendor/socket.io.min.js`（与 `docs/webui-beautification-plan-2026-11-25.md` §7.3 第 7 条一致）。
- **回归测试：** `tests/test_web_static_2026_11_26.py`（9 例）——`static_url_path` 必须是 `/static`、静态路由存在、模板里每个 `/static/…` 引用都 200 且 MIME 正确、`/templates/…` 前缀不再出现、base/群开关页两份 CSS 可访问、socket.io 本地文件可访问且体积正常、不存在的静态资源仍 404、`/static/base.html` 等模板源码不可下载。

## 验证与限制

- 审查阶段全量测试：`/tmp/tvenv/bin/python -m pytest tests/ -q`，**85 passed in 35.61s**（当时基线）。
- 修复后全量测试：**95 passed in 3.85s**（新增 `tests/test_bugfix_2026_11_26.py` 的 10 个用例）；B-08 追加后 **251 passed in 37.74s**（新增 `tests/test_subagent_permissions_2026_11_26.py` 的 13 个用例）；额外工具授权补充后 **268 passed**（新增 `tests/test_subagent_extra_tools_2026_11_26.py` 的 17 个用例）。；B-09 修复后 **291 passed**（新增 `tests/test_web_static_2026_11_26.py` 的 9 个用例）；插件页样式迁移收尾后 **298 passed**（新增 `tests/test_web_pages_migration_2026_11_26.py` 的 6 个用例）。
- `git diff --check` 通过；改动文件 `py_compile` 通过；全部 Jinja 模板解析通过；`dashboard.html` 内联 JS 通过 `node --check`。
- 复现验证（审查阶段）：权限热更新输出仍为旧动作 `allow`；子代理首批 schema 函数名（例如 `fs_read`）均不在内部只读 key 集；Web 审批调用因 keyword-only 参数触发 `TypeError`。
- 对子代理、Web/API、数据/生命周期分工进行了并行只读审查；上述报告项由代码交叉核实。
- 本轮修复只改动了上表列出的 7 处产品代码，未调整既有 API 语义与配置默认值。
- 仍属静态与定向核查 + 单元测试，不是 OneBot 真机/QQ 端到端验证；未提交 git。

## 修复顺序（已完成）

1. ~~先修 B-02（审批不可用）与 B-03（可能破坏后续上下文工具链）。~~ 已完成。
2. ~~修 B-04（配置热更不能及时落实权限）和 B-05（子代理核心工具能力失效）。~~ 已完成。
3. ~~修 B-01、B-06、B-07，并分别补 HTTP、消息类型、积压公平性回归用例。~~ 已完成。

