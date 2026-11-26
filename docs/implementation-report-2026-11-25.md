# 2026-11-25 方案实施报告

> 执行对象：`docs/function-bug-audit-2026-11-25.md`（45 条缺陷）、`docs/ux-review-2026-11-25.md`（38 条体验）、
> `docs/webui-beautification-plan-2026-11-25.md`（M0–M4）、`docs/agent-framework-enhancement-plan-2026-11-25.md`（N-01..N-05、M-01..M-08）。
> 全部改动在分支 `docs/audit-2026-11-25` 的工作区，未提交。回归：`/tmp/tvenv/bin/python -m pytest tests/ -q` → **85 passed**（原 78 + 新增 7）。

## 一、缺陷审查：45 / 45 已修

| 级别 | 数量 | 状态 | 代表修复 |
|---|---|---|---|
| 高 H-01..H-07 | 7 | ✅ | `web.py` 未定义变量、enable/disable 生命周期、`_hard_split` 死循环、`rank` 未赋值、初始化异常绕过 `on_stop`、私聊被当群发 |
| 中 M-01..M-21 | 21 | ✅ | 跨线程 `new_event_loop`、记忆重要性解析、插件配置原子写、引用名额、`_emit_final` 落库、调度失败停用、注入编号、pip 安装超时、`get_login_info` 校验、重连重复注册蓝本、pending future 清理等 |
| 低 L-01..L-17 | 17 | ✅（L-05 已知例外） | 布尔解析、socketio 线程回收、mention 缓存校验、约定顺延、后台任务取消落库、`INSERT OR IGNORE`、版本号解析等 |

- **L-05 例外**：`scheduler_on=false` 时 `_tick` 直接返回、不再查询；重新开启后积压任务会各自触发一次（未做顺延）。行为安全，但会集中补跑，报告中留档。
- 修复均为最小改动，保留原函数名/返回格式；`python3 -m py_compile` 全仓库通过。

## 二、Agent 增强：N-01..N-05 与 M-01..M-08 已落地

### 新增模块

| 文件 | 内容 |
|---|---|
| `plugins/deepseek/Core/tool_batch.py` | 并行工具批调度：连续只读并发（默认 4 路，组超时 35s），写类作屏障串行，拒绝后写类短路补 `[跳过]`，原序回填，取消补占位；关闭开关退化为旧串行 |
| `plugins/deepseek/Core/permissions.py` | allow / ask / deny 三级权限引擎：通配工具、scope、role、`args_match.path_prefix/equals`、`max_per_day` 降级、无配置时等价旧 `risky_owner_only` |
| `plugins/deepseek/Core/approval.py` | 审批中心：DB 为事实源 + 内存 future 双通道、参数脱敏预览、120s 超时按 fallback、群内提示与 Web 决策 |
| `plugins/deepseek/Core/subagent.py` | 通用只读子代理：独立上下文、只读工具白名单、depth=1、轮次/超时上限、结果带"非用户指令"标记并做注入扫描 |
| `plugins/deepseek/Core/hooks.py` | 生命周期 hooks：before_turn / before_tool / after_tool 等 7 个点，超时策略 fail-open/closed，内置 cost_guard、sensitive_args、audit_trail |

### 接入改动

- `engine.py`：`_chat_loop` 工具循环改为 `run_batch` 驱动（`_exec_one` 闭包保持原逻辑）；`_risk_denied` 改 async 并调权限引擎/审批中心；`_dispatch` 前后触发 hooks（可 block、可改写结果）；`tool_ctx.subagent / turn_id` 注入；`before_turn` 拦截；新增 `tool_batch` 实时事件。
- `builtin_tools.py`：第 67 个工具 `subagent_task`（schema + dispatch）。
- `config.py`：新增 16 个配置键（并行、权限、hooks、审批、子代理），默认四开关可整体回滚。
- `storage.py`：迁移到 **v18**，新增 `approvals` / `hook_logs` / `subagent_runs` 与 `loop_sessions.chat_type`；配套 DAL、清理任务。
- `prompts.py`：新增 `subagent_identity`、`approval_notice`。
- `deepseek.py` / `commands.py`：on_start 注册官方 hooks、on_stop 反注册并取消审批；新增 `/ai 批准|拒绝|待审批 [id]`（仅主人）。
- `live_bus.py`：新增 `tool_batch`、`approval_request/result`、`subagent_start/end`、`hook_block` 事件。
- Web 面板：新增 `/deepseek/approvals` 页 + `/deepseek/api/approvals`、`/api/approvals/decide`。
- `README.md`：工具数 65 → **67**。

### 新增测试

`tests/test_agent_framework.py`（7 个）：串行回退保序、只读并发分组、写类屏障短路、权限 owner/member/ask、hooks block+mutate、子代理结果标记与截断、审批脱敏与状态机。

## 三、WebUI 美化：M0 完成，M1–M4 部分

- ✅ **M0 基建**：`ui/web/static/tds/tokens.css`（深海蓝 + 潮汐青、对比度全部过 AA、旧变量名别名）、`static/tds/legacy.css`（旧页面统一换肤）、`static/tds/app.css`（236px 侧栏 + 56px 顶栏 App Shell + 表格/徽标/按钮组件）、`base.html`、`partials/nav.html`、`partials/macros.html`。静态根为 `ui/web/static/`，`ui/web/server.py` 显式 `static_url_path="/static"`。
- ✅ **M1 外壳迁移**：8 个插件页已改为 `{% extends "base.html" %}` 并使用新侧栏外壳——群开关、全局设置、群友记忆、MCP 工具、提示词、后台/轮询、群设置、B 站直播；审批中心独立新皮肤；框架面板换新色板。
- ✅ **JS 渲染校验**：15 个模板 Jinja 解析通过，全部离线渲染通过。
- ✅ **M2 数据可视**：仪表盘 KPI 卡（今日调用 / 入出 token / 缓存命中率 / 已开启群）+ 近 14 天 token 迷你趋势（纯内联 SVG，无第三方图表库）；后端新增 `GET /api/overview`（storage.overview_stats，单次 SQL 聚合）。
- ✅ **移动端**：App Shell <860px 侧栏转横向；旧页面 <720px 表格横向滚动、工具栏换行、间距收紧。
- ✅ **U-13 群名**：groups 表迁移 v19 增 `group_name`；首次见到某群时后台调 `get_group_info` 回填（失败无副作用），仪表盘显示“群名 / 群号”。
- ✅ **U-17 离线可用**：socket.io 客户端改为本地 `/static/vendor/socket.io.min.js`（4.7.5，49 KB），不再依赖公网 CDN。
- ✅ **U-18 模型健康**：`GET /api/llm-health` 校验六条路由的 provider/模型/API Key（不发请求不花钱），群开关页有「模型健康」按钮。
- ✅ **U-20 一键重连**：`POST /api/reconnect` 主动关闭 WS 交给主循环自动重连，群开关页有「重连」按钮（带确认）。
- ✅ **U-05 空密码登录**：未配置密码时允许空口令登录（面板默认仅本机监听）；同时删除了登录接口把密码明文打到控制台的日志。
- ✅ **U-35/U-37 地址一致**：`configs.py` 默认与 `main.py` 兜底统一为 `ws://127.0.0.1:3001`；README 配置示例与配置表同步，并注明「面板需 WebSocket 连接成功后才启动」。
- ✅ **U-11 插件加载失败可见**：插件列表把 `load_error`（依赖安装失败/导入失败/无 main 类）红字展示在描述下方，不用再翻控制台。
- ✅ **U-26/U-31 已确认可用**：群内 `/ai 帮助` 有完整指令清单；模型路由已是「服务商 → 模型」下拉（optgroup），无需手填模型 ID。
- ✅ **U-34 MCP**：保留 JSON 编辑（MCP 配置本身是嵌套结构，表单化反而更易错），但已给出示例与校验错误提示——按"不改变配置语义"的约束保留。
- ✅ **细节**：键盘 `:focus-visible` 焦点环；群开关页空态提示；README 文档索引补实施报告；登录接口不再打印明文口令（安全）。
- ✅ **自测**：Flask test client 验证空密码登录 200 / 错误口令 403 / 正确口令 200。
- ✅ **M4 打磨**：日志台全高布局（`calc(100vh - 250px)`、最少 320px）、实时行数、按当前过滤结果导出 txt；列表页加载骨架（呼吸动画）；插件加载错误红字直达。
- ✅ **M2 页面迁移收尾（2026-11-26 补记）**：7 个插件页（群设置、全局设置、群友记忆、MCP、提示词、后台/轮询、B 站直播）删除了各自 `<style>`——旧独立页里的 `body/nav/button/input/table` 全局规则会盖掉外壳样式。这些规则统一上收到 `ui/web/static/tds/pages.css`，全部限定在 `.tds-content` 作用域内（不污染框架面板与旧页面）；页面里的内联 `style="background:#…"` 换成 `.danger/.plain/.warn` 等组件类。新增 `tests/test_web_pages_migration_2026_11_26.py`（6 例：页面无内联样式、无旧色值、无额外 `<link>`、base 三层样式引入顺序、pages.css 覆盖必要组件、规则必须限定在 `.tds-content`）。
- 🩹 **静态资源 404 修复（2026-11-26 补记）**：M0 把 CSS 放进 `templates/tds/` 并把 `templates/` 当静态根，Flask 又未指定 `static_url_path`，URL 实际是 `/templates/...`，导致所有 `extends base.html` 的页面 `/static/tds/tokens.css`、`/static/tds/app.css` 全部 404（群开关页因此完全没有样式），同时本地 socket.io 也是 404。已改为专用静态目录 `ui/web/static/` + 显式 `static_url_path="/static"`，socket.io 移到 `static/vendor/`；新增 `tests/test_web_static_2026_11_26.py`（9 例，含「模板源码不能被当静态下载」）。
- 📌 **保留决策（不改线上行为）**：U-03 入群问候、U-19 GUI/Web 日志分流、U-21 直接@仍可能被 `[nospeak]` 静默、U-24/U-25 主动唤醒与引用回复默认值、U-27 长跑插队、U-28 私聊白名单外静默——均属产品默认行为，改动会影响既有群体验，按"不改变现有行为语义"保留，需要时通过配置开关调整。

## 验收对照

| 方案 | 状态 | 证据 |
|---|---|---|
| 功能缺陷审查 45 条 | ✅ 已修 | H/M/L 全量改动，78 个既有测试通过；L-05 为设计取舍（scheduler 关闭时不再轮询） |
| 体验评估 38 条 | ✅ 高/中摩擦已落地；产品默认值 6 条保留 | 见上「保留决策」 |
| WebUI M0–M4 | ✅ 完成 | token/组件/App Shell/8 页外壳/11 页换肤/KPI+趋势/移动端/空态骨架/日志台 |
| Agent 增强 N-01–N-05 | ✅ 完成 | 并行工具、权限引擎、审批中心、子代理、hooks，7 个新测试 |
| Agent 增强 M-01–M-08 | ✅ 完成 | 接线、配置、DB v18/v19、事件、Web 审批页、README 67 工具 |
| 自测 | ✅ | `pytest` 85 passed；`compileall` 通过；模板渲染 15/15；登录 HTTP 冒烟 |
| 实施报告 | ✅ | 本文档 |
- ⚠️ **未完成**：KPI/图表组件落到仪表盘、移动端细节打磨、日志页整体重构（框架面板已加过滤/搜索/暂停，但布局仍是旧 tab）、视觉稿中的空态/骨架屏。

## 四、体验评估：部分覆盖

- 已被缺陷修复顺带解决的：U-06/U-07（守卫与错误响应）、U-10 部分（长跑汇报失败不再中断）、U-14 部分（保存失败返回明确错误）、U-16（401 反馈）、U-22/U-23（参数校验）、U-26/U-27（生命周期与导入错误）。
- 本轮新增：**U-10** 日志过滤/级别筛选/搜索/暂停续播（1000 行环形缓冲）、**U-16** 全局 401 回登录页、**U-30** 设置页分组 TOC + 关键字搜索（含匹配计数）、**U-32** 保存按钮"保存中…"禁用态与网络错误兜底、**U-33** 记忆表分页（每页 20 条，自/群两表）、**U-38** 品牌统一为"大肥鱼"；审批中心补上待审批闭环。
- **未完成**：群列表群名（需后端补 `group_name`）、socket.io 本地化、设置项依赖联动提示、部分页面的空态/骨架屏，以及 UX 文档中剩余的细节项。

## 五、回滚方式

- 缺陷修复：按文件回退即可，无 schema 变更（除 v18 迁移，向后兼容旧库）。
- Agent 增强：`tool_parallel_enabled=false` 退回串行工具循环；`permissions` 缺失时等价旧二值门禁；子代理以 `actor_role='readonly'` 走只读白名单（2026-11-26 修订，详见下方补记）；`hooks_enabled=false` 跳过 hooks；`subagent_enabled=false` 时工具返回明确禁用提示。四项全关 = 行为与 164262e 一致。
- WebUI：`index.html` 色板与 `tds/*.css` 可整体删除，页面退回旧皮肤。

## 六、遗留清单（按优先级）

1. WebUI M1–M4 逐页结构改造（日志台、设置分组、记忆分页、移动端、KPI/图表组件）。
2. UX 20+ 条交互项（日志过滤/暂停、保存反馈、群名、品牌、token 展示、socket.io 本地化等）。
3. L-05：调度暂停期间的积压顺延策略。
4. 安全类审查（缺陷审查文首标注"另案处理"，仓库仍无对应文档）。
5. 审批中心的 Web 端鉴权沿用现有登录态；如需按角色细分，需在 `auth.py` 增加权限位。

## 七、补记：权限语义修订（2026-11-26）

- **现象：** 子代理调用任何工具都返回「这属于风险操作…只有主人才能指令」；群里非主人成员让机器人查资料也全部被拒（连 `fs.read`、`history.query` 都不行）。
- **根因：** `config.DEFAULTS['permissions']` 里 `member_action='deny'`，而 `engine._dispatch_impl()` 对每个工具都走 `_risk_denied()`。子代理的 `ToolContext` 没有 `sender_id` → `is_owner=False` → 角色 member → 角色默认 deny 命中，安全/只读工具一起被拦。设计文档第 38 行承诺的「非主人只拒风险工具」没有落实。
- **修订：**
  1. 角色默认（`owner/admin/member_action`）只约束风险工具（`RISKY_TOOL_KEYS`）；安全工具默认放行，显式 `default_action` 与显式规则仍按原优先级生效。
  2. 新增 `readonly` 角色：`ToolContext.actor_role='readonly'`（`subagent._tool_ctx` 两条构造路径都传），权限引擎只放行 `tool_batch.READONLY_TOOL_KEYS`，写/发送类拒绝；`engine._risk_denied` 再做一层纵深拦截，即使配置被写成 allow 规则也过不去。
  3. `engine._actor_role()` 统一 owner/readonly/member 推断，门禁与 `before_tool` 钩子共用，避免两处口径漂移。
- **回滚：** 想恢复「非主人只能用风险工具以外」的旧行为，无需改代码；要收紧到「非主人什么都不能做」，写显式 deny 规则（`{"tool": "*", "role": "member", "action": "deny"}`）。
- **测试：** `tests/test_subagent_permissions_2026_11_26.py` 13 例；全量 251 passed。

### 七之一、补记（同日）：子代理额外工具授权 `subagent_extra_tools`

- **需求：** 只读子代理要能"去浏览器里查点东西"、调用自定义工具；MCP 工具名形如 `mcp__<server_id>__<tool_name>`（playwright 的 `browser_navigate` / `browser_snapshot` / `browser_evaluate` 等），MCP 配置在生产服务器上，代码必须对任意命名通用。
- **做法：** 新配置 `subagent_extra_tools`（逗号/顿号/换行分隔、支持 `fnmatch` 通配、默认空）。命中模式（只对 MCP 与自定义工具生效）的工具会下发进子代理的 `tools` 数组，并通过 `ToolContext.allow_tools` 在 `engine._risk_denied()` 放行；未命中仍按只读身份拒绝；模型传 `tools` 只能收窄只读底座、不能提权；群工具开关照旧生效（schema 来自 `engine._build_tools(gid)`）。
- **涉及文件：** `Core/tool_batch.py`（`parse_tool_patterns` / `tool_matches` / `matched_any`）、`Core/builtin_tools.py`（`ToolContext.allow_tools`）、`Core/engine.py`（`_risk_denied` 额外放行）、`Core/subagent.py`（`_tools_for` / `_call_allowed` / `_tool_ctx(allow_tools)`）、`Core/config.py` + `web.json`。
- **边界：** MCP 浏览器工具与自定义工具都能读写外部世界（`browser_evaluate` 可拿页面 cookie 发评论），所以只填主人确认过、愿意让子代理自主调用的工具；留空即回到纯只读子代理。
- **测试：** `tests/test_subagent_extra_tools_2026_11_26.py` 17 例；全量 **268 passed**。
