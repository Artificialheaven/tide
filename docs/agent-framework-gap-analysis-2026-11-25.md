# Agent 框架差异书：大肥鱼（Tide + DeepSeek 插件）vs 主流 Agent 框架

> 版本：2026-11-25 ｜ 对象：`/home/ubuntu/tide` 当前分支 `docs/audit-2026-11-25`（164262e）
> 主参照：Claude Code；横向参照：OpenAI Agents SDK、LangGraph、AutoGen、CrewAI、OpenHands
> 性质：机制层差异分析 + 演进建议。**不评价模型能力，不给"谁更强"的总排名**——物种不同，比较的是器官。

---

## 0. 摘要

**一句话结论：大肥鱼不是"落后一代的 coding agent"，而是一个"社会性群聊 agent"，它的上下文工程和多套记忆比多数开源框架更细，差距集中在 agent 工程学的四件套——扩展点（hooks）、子代理、权限分级、断点恢复。**

按 agent 工程学的通用分层打分（●完善 / ◐部分 / ○缺失，非精确评分）：

| 层级 | 能力 | 大肥鱼 | 说明 |
|---|---|---|---|
| L1 | 基础 agent 循环 | ● | 触发→判官→上下文→工具循环→落库，闭环完整 |
| L2 | 工具治理 | ◐ | 66 个工具 + MCP + 自造，但 schema 全量注入、同轮串行 |
| L3 | 上下文工程 | ●/◐ | 前缀缓存布局是亮点；压缩是"滚动摘要"，非结构化 |
| L4 | 扩展性 | ○ | 无 hooks、无技能、无通用子代理；只有插件级热重载 |
| L5 | 可靠性 | ◐ | 每轮落库可回放；长跑无断点续跑 |
| L6 | 可观测/评测 | ◐/○ | SSE 事件流 + 11 页控制台；无评测集、无 OTel |
| L7 | 多代理协作 | ○ | 只有一个硬编码视觉子代理 |
| L8 | 社会性（人设/记忆/主动） | ● | **独有优势**，其他框架基本空白 |

**最值得补的 4 项（按性价比排序）**：同轮并行工具（1 人日）→ 权限分级与逐次审批（3 人日）→ 通用子代理（4 人日）→ 结构化 hooks（2 人日）。**最不该照搬的 4 项**：plan mode 强制、文件编辑工具集、CLI 交互形态、图编排 DSL——它们服务于"单用户完成代码任务"，与"多群常驻陪聊"的约束相反。

---

## 1. 比较对象与方法

### 1.1 当前框架到底是什么

仓库是两层结构：

| 层 | 位置 | 职责 |
|---|---|---|
| 宿主框架 | `main.py`、`core/`、`utils/` | OneBot v11 WebSocket 客户端、重连、插件加载/热重载、DearPyGui 桌面端与 Flask Web 面板 |
| Agent 运行时 | `plugins/deepseek/` | 群聊 AI 的完整 agent：门禁、判官、上下文、工具、记忆、心情、长跑、多模态 |

本文的比较对象是**后者**，必要时说明宿主的约束。证据文件：`plugins/deepseek/Core/engine.py`（1949 行）、`builtin_tools.py`（2473 行）、`storage.py`（3683 行）、`loop_run.py`（401 行）、`context.py`（434 行）、`deepseek.py`（767 行）。

一轮完整对话的调用链（均可在源码核对）：

```
QQ 消息 → deepseek.py 插件分发（每群 asyncio.Lock，deepseek.py:216-220）
        → Engine._run_turn（engine.py:672）
        → 触发门禁 _pre_judge_gate（engine.py:568-593）
            启发式命中 or AI 初筛脚本放行；脚本异常/超时 fail-closed
        → 便宜模型判官 _judge（engine.py:648-668）
            judge_fail_open 默认 False：判官失败 = 不回复
        → 上下文组装（engine.py:697-849）
            摘要锚点窗口 + 群友/自我记忆 + 本群身份 + 风格 + 约定 + 注入 + 心情 + 梗 + 时钟
        → 工具循环 _chat_loop（engine.py:981-1244）
            最多 max_tool_rounds=50（config.py:80），同轮多工具串行
        → 中途插话 / 最终 split 发送（reply.py）
        → 异步侧写：记忆抽取、心情推进、风格学习、用量落库
```

### 1.2 参照对象与选型理由

| 框架 | 形态 | 为什么选它 |
|---|---|---|
| **Claude Code** | 单用户 CLI/IDE/桌面/云的 coding agent | 用户点名的参照；agent 工程学的"事实标准"，hooks/子代理/权限/检查点都最完整 |
| **OpenAI Agents SDK** | 库（Python/TS） | 原语最少：Agent / handoff / guardrail / session；代表"轻框架"路线 |
| **LangGraph** | 图编排库 | 持久化执行、检查点、interrupt 的标杆 |
| **AutoGen** | 多代理框架（AgentChat + Core） | 群聊式多代理的老牌实现，与本框架"群"的形态最像 |
| **CrewAI** | 角色化多代理 | 角色/任务/流程抽象，产品化程度高 |
| **OpenHands** | 软件工程 agent（沙箱运行时） | 长时任务 + 沙箱 + 评测（SWE-bench）的代表 |

方法：读代码（当前框架）+ 读官方文档（参照框架），**只比较机制，不做性能 benchmark**。外部事实均附文档链接，见附录 A；当前框架事实均附 `文件:行`，见附录 B。

### 1.3 评分口径

- ●：有完整、可配置、有测试/文档的实现
- ◐：有实现但能力受限（硬编码、单一路径、缺配置）
- ○：没有
- 评分只描述"该维度是否存在成熟机制"，不表示综合优劣

---

## 2. 参照框架速写

### 2.1 Claude Code

核心是"一个主循环 + 一组工具 + 一套权限 + 一圈生命周期钩子"：

- **主循环**：模型输出工具调用 → 执行 → 结果回灌，直到模型给出终答；支持**同轮多个工具调用并行**（`PostToolBatch` 事件即在一批工具全部完成后触发）。
- **工具**：十余个内置工具（Read/Write/Edit/Bash/Grep/Glob/WebFetch/WebSearch/Task/TodoWrite 等），MCP 动态扩展；工具按需授权，不是全量塞进提示词。
- **权限**：`allow / deny / ask` 规则 + 权限模式（如 plan、acceptEdits、bypassPermissions）+ hooks 可强制 allow/deny/ask，形成"规则—模式—钩子"三层。
- **子代理**：Markdown + frontmatter 定义，**独立上下文窗口、独立系统提示词、受限工具集、独立权限**，只把总结回传主线；内置 Explore（只读搜索）、Plan（计划研究）、general-purpose；支持并行、嵌套（默认最多 3 层）、并发上限 20、按 ID 续跑、独立 transcript。
- **Hooks**：30+ 生命周期事件（SessionStart/UserPromptSubmit/PreToolUse/PermissionRequest/PostToolUse/PostToolBatch/PreCompact/Stop/SubagentStart/SubagentStop…），handler 可以是 shell、HTTP、MCP 工具、LLM 提示或子代理；退出码 2 阻止，或输出 JSON 决策（含 `additionalContext` 注入、`permissionDecision`）。
- **其他**：CLAUDE.md 记忆、skills、checkpoints/rewind、auto-compact、background agents、worktree 隔离。

### 2.2 OpenAI Agents SDK

刻意做小：`Agent`（指令+工具）、`handoff`/agents-as-tools（委派）、`guardrail`（输入输出校验，可与模型并行、失败即停）、内置运行循环。外围能力靠可选层：Sessions（SQLite/Redis/Mongo 等）、Tracing、Human-in-the-loop（审批/中断/恢复，`RunState` 可序列化续跑）、MCP、Sandbox agents、Realtime/Voice。

设计哲学是"够用但原语少"——不提供图、不提供角色，把编排留给开发者。

### 2.3 LangGraph

`StateGraph`：节点是函数，边是控制流，共享一个带 reducer 的 typed state。杀手锏是**持久化执行**：checkpointer 在每个 super-step 落盘，可中断（interrupt）等人工审批、可"时间旅行"回到任意检查点、可把子图当节点组合。适合长流程、需要恢复与人工介入的场景；代价是你要自己画图。

### 2.4 AutoGen

两层：AgentChat（高层：AssistantAgent、UserProxyAgent、GroupChat、Swarm、Magentic-One）与 Core（事件驱动的 actor 运行时）。支持代码执行器（本地/Docker）、human-in-the-loop、`save_state/load_state`。它的"群聊"是**多个 LLM agent 互相发言**，与本框架"一个 agent 面对一群人类"方向相反——这点后文会展开。

### 2.5 CrewAI

角色化抽象：Agent（role/goal/backstory）+ Task + Crew（sequential 或 hierarchical 流程）+ Flow（事件驱动）。自带短期/长期/实体记忆、工具、guardrails、知识库。产品化最好，但抽象较重，定制控制流不如 LangGraph。

### 2.6 OpenHands

面向软件工程：事件流（event stream）驱动、Docker 沙箱运行时、CodeAct 风格（用代码表达动作）、浏览器/终端/编辑器工具、microagents、SWE-bench 评测体系。是"长时任务 + 强沙箱 + 可复现评测"的样板。

---

## 3. 差异矩阵

> 记号：● 完善 ｜ ◐ 部分 ｜ ○ 缺失 ｜ — 不适用/无此概念

| # | 维度 | 大肥鱼 / Tide | Claude Code | OpenAI Agents SDK | LangGraph | 判断 |
|---|---|---|---|---|---|---|
| 1 | 定位 | 多群常驻陪聊 agent | 单用户编码 agent | 通用 agent 库 | 流程编排库 | 物种不同 |
| 2 | 运行形态 | 长驻进程，事件驱动 | 会话式 CLI/IDE | 库调用 | 图执行 | 各有约束 |
| 3 | 并发模型 | **每群串行 + 锁**，群间并行 | 单会话串行，子代理并行 | 单 run，可多 run 并行 | 图内并行分支 | 合理 |
| 4 | 触发决策 | **门禁 + 便宜判官**（省钱） | 用户输入即触发 | 调用即触发 | 图入口触发 | **独有** |
| 5 | 工具循环上限 | 硬上限 50 轮，到限强制收尾 | 有 maxTurns，可配置 | 循环至完成 | 图决定 | 接近 |
| 6 | 同轮多工具 | **串行**（engine.py:1126-1135） | **并行** | 同轮多调用（并行性未确认） | ToolNode 并行 | **差距** |
| 7 | 工具规模 | 66 内置 + MCP + 自造 | ~15 内置 + MCP | 自定义 + MCP | 自定义节点 | 规模不同 |
| 8 | 工具 schema 注入 | **全量常驻**（约 1.2–1.4 万 token） | 按需/渐进 | 全量（用户自控） | 开发者自控 | **差距** |
| 9 | 工具发现 | 提示词目录 + 固定顺序 | 工具名 + 描述 | 工具名 + 描述 | — | 接近 |
| 10 | 权限模型 | **二值主人门禁**（engine.py:1753-1769） | allow/deny/ask + 模式 + hooks | guardrail + HITL 审批 | interrupt | **差距** |
| 11 | 逐次审批 | 无（非主人直接拒） | 有（PermissionRequest） | 有（approval） | 有（interrupt） | **差距** |
| 12 | 沙箱 | 自研路径白名单 + 子进程 | 平台级沙箱 | Sandbox agents | 无（自接） | 接近 |
| 13 | 子代理 | 仅硬编码视觉子代理 | **通用、可配置、隔离上下文** | agents-as-tools | 子图 | **差距** |
| 14 | 多代理协作 | 无 | agent teams/background | handoff | 多节点 | ○ |
| 15 | 生命周期扩展 | 无 hooks；插件热重载 | **30+ hooks** | RunHooks/AgentHooks | 回调 | **差距** |
| 16 | 上下文压缩 | 滚动摘要 + 尾部易变块 | auto-compact + microcompact | Sessions/裁剪交由用户 | 状态自管 | 各有千秋 |
| 17 | 前缀缓存优化 | **显式设计**（LIVE_CTX 尾部） | 依赖供应商缓存 | 依赖供应商缓存 | 无 | **领先** |
| 18 | 记忆 | **多套**（群友/自我/风格/约定/心情） | CLAUDE.md + 文件 | Sessions 消息史 | checkpointer | **领先** |
| 19 | 状态恢复 | 每轮落库可回放；长跑无断点 | checkpoints/rewind | RunState 序列化 | **checkpointer 级** | **差距** |
| 20 | 可观测 | SSE 事件流 + 11 页控制台 + 用量表 | OTel/调试日志 | Tracing | LangSmith | ◐ |
| 21 | 评测 | **无** | 无公开评测集 | 平台 eval | LangSmith eval | **差距** |
| 22 | 人设/主动性/社会性 | **强**（人格、心情、梗、主动、长跑） | 无 | 无 | 无 | **独有** |

---

## 4. 逐维度差异

### 4.1 运行形态与会话模型：不是落后，是约束不同

**当前框架**：长驻 WebSocket 客户端，一个 QQ 号服务多个群；每群一把 `asyncio.Lock`（`deepseek.py:216-220`），同一群同一时刻只跑一轮，避免两个轮次交错写历史。群与群之间并行。会话不是"一次任务"，而是**无限流**：消息表只增，摘要锚点前移，靠"窗口 + 摘要 + 尾部易变块"维持上下文。

**对照**：Claude Code / OpenAI SDK 都以"一次会话/一次 run"为单位，生命周期短，上下文有明确终点，结束时可以整体落盘。LangGraph 用状态机建模，天然有开始和结束。

**判断**：这是产品形态差异，不是缺陷。真正可借鉴的是**"会话有终点"带来的状态管理**：既然长驻，就更需要显式的轮次状态（见 4.9 检查点）。

**若照搬的代价**：把群聊改成"一次任务一次会话"会丢掉上下文连续性，也会让判官/记忆失去意义。不采纳。

### 4.2 Agent 循环与并行工具：最直接的一处工程差距

**现状**（`engine.py:1126-1135`）：

```python
for norm in result.tool_calls:
    content = await self._dispatch(gid, cfg, norm, ...)
```

模型在一轮里返回 3 个互不依赖的调用（比如同时查天气、查日程、搜梗），框架**逐个 await**。每个工具可能是一次网络往返（MCP、QQ 文件下载、网页抓取），串行把延迟叠成 N 倍。

到 `max_tool_rounds=50` 时摘掉工具、追加 system 指令强制一句话收尾（`engine.py:1195-1240`）——上限本身合理（防死循环），但 50 轮 × 串行工具意味着单轮最坏情况非常长。

**对照**：Claude Code 在一批工具全部完成后才触发 `PostToolBatch`，明确按批并行；LangGraph 的 `ToolNode` 对同一消息里的多个 tool call 并发执行；OpenAI SDK 至少把输入 guardrail 与模型调用并行（`run.py` 中 `asyncio.create_task` + `gather`，1059-1061、1793-1871），工具级并行未在源码中确认，不写死。

**差距判断**：值得补，且成本最低。改法：

1. 把 `for` 循环改成"分组 + `asyncio.gather`"：只读工具（查询、读文件、搜索）可并发；写类/有副作用工具（`fs.write`、`fs.delete`、`qq_send_*`、`loop_finish`）保持串行或按模型给出的顺序执行。
2. 并发上限（如 4–8）与单工具超时（已有 `request_timeout`）组合使用。
3. 结果仍按 `tool_calls` 原始顺序回填 `tool` 消息，避免模型看到乱序。
4. 回归：现有 `_fill_missing_tool_results`（1771-1792）要能处理"部分并发失败"。

工作量约 **1 人日**，收益是长工具链场景的墙钟时间直接下降。

### 4.3 工具生态：数量领先，治理落后

**现状**：`builtin_tools.py:24-91` 定义 **66** 个工具（README 写 65，差 1 个——建议顺手修文档），`_SCHEMAS` 是 66 个完整 JSON schema，序列化后 27,736 字符，按中英混排粗估 **1.2–1.4 万 token 的固定成本**，每一轮请求都要带。工具分四类来源：内置、MCP（`mcp__<server>__<tool>`）、AI 自造（`custom_tools`）、预置脚本（`screen`）。按群可开关（`group_tool_state`）。

**对照**：Claude Code 的内置工具是"少而通用"，靠 MCP 与子代理扩展；工具按权限逐次授权，不需要的一次也不进上下文。OpenAI SDK 把工具定义交给调用方，框架不预设。OpenHands 用"代码即动作"把大量工具收敛成 Bash/Python 两个入口。

**差距判断**：66 个工具在"陪聊"场景是合理的产品选择（发图、发表情、查群成员、记约定……都是真实需求），但**注入方式**可以更省：

- 方案 A（轻）：`tools_guide` 只列"工具目录 + 一句话"，模型需要时用 `tool_search` 拉取完整 schema（两阶段披露）。可把常驻成本从 1.2 万 token 降到 2–3 千。
- 方案 B（中）：按"当前群是否启用 + 最近 N 轮用过"动态拼 schema，未用过的工具不进请求。风险是模型"想不起来"某个工具。
- 方案 C（重）：工具分组 + 子代理（探索类工具只给子代理，主线只拿摘要）。与 4.6 联动。

建议先做 A 的灰度：保留全量模式开关，A/B 对比回复质量与成本。

### 4.4 权限：二值门禁 → 三级决策

**现状**（`builtin_tools.py:1334+`、`engine.py:1753-1769`）：

- `RISKY_TOOL_KEYS` 标记危险工具：文件写/删/发布、@全体、代码执行、自造工具注册、初筛脚本。
- `_risk_denied`：**不是主人 → 直接返回拒绝文案**。MCP 工具整体视为风险。
- 没有 allow/deny/ask 规则，没有"这一次允许"的临时授权，没有审批队列，没有按群/按工具/按参数的细粒度策略。

**对照**：Claude Code 的权限是三层——规则（allow/deny/ask，支持工具名与参数模式）、模式（plan/acceptEdits/bypassPermissions…）、hooks（`PermissionRequest` 可返回 allow/deny/ask，`PreToolUse` 可 deny）。OpenAI SDK 的 HITL 允许在工具执行前中断、等审批、再恢复（`RunState` 可序列化）。LangGraph 用 `interrupt()` 暂停图、人工批准后继续。

**差距判断**：这是**安全与体验的双重缺口**。当前设计把"群成员"整体当作不可信（合理），但把"主人"整体当作可信（过粗）：主人也可能误触发 `fs.delete`、也可能想让某个管理员临时用一次 @全体。建议：

1. 引入三态 `allow / deny / ask`，`ask` 走 Web 面板审批队列（复用 `pending_requests` 表结构，加 `kind='approval'`）。
2. 审批超时默认拒绝，并把"谁批准、批了什么参数"落 `usage_log`/审计表。
3. 规则支持 `tool` + `arg_pattern` + `scope`（群/私聊/主人/管理员），配置进 `config.json` 的 `permissions` 段，Web 设置页可视化。
4. MCP 工具不再"整体风险"，按 server 配置默认策略。

工作量约 **3 人日**（含 Web 审批面板）。

### 4.5 子代理：只有一个硬编码的"视觉助手"

**现状**：`video_read.py:849` 的 `_ask_subagent` 是唯一的子代理调用——把视频关键帧交给视觉模型描述，超时 180s，模型路由 `read_video_model`。它没有独立工具集、没有独立上下文、不能并行、不能嵌套。

**对照**：Claude Code 的子代理是**一等公民**：frontmatter 定义（name/description/tools/model/permissionMode/maxTurns/isolation），独立上下文窗口与系统提示词，只回传总结，支持并行、嵌套 3 层、并发 20、按 ID 续跑、独立 transcript，回传前还做 prompt-injection 扫描（子代理的话不具授权）。OpenAI SDK 的 agents-as-tools 是同一思想的轻量版。

**差距判断**：对陪聊 agent，子代理的价值不是"并行写完一个仓库"，而是**上下文隔离**：

- 长文/长视频/大文件分析：把 10 万 token 的原始材料交给子代理，主线只收 500 字结论；
- 多步检索（翻聊天记录、查多个群文件）：子代理多轮工具调用，主线不被中间结果污染；
- 高风险操作预演：子代理在只读工具集里先跑一遍，主线决定是否执行。

建议把 `_ask_subagent` 抽象为通用能力：

1. `spawn_subagent(task, tools=[...], max_rounds=N, model=...)`：内部起一个受限 `Engine` 实例（复用 `_chat_loop`，工具白名单、独立消息数组、无群记忆注入）。
2. 只回传 `final_text` + `usage`，中间 tool 消息不落主线历史。
3. 超时/失败降级为主线自己跑，保证可用性。
4. 先接 2 个场景：`read_video` 改造 + "深度检索历史"。

工作量约 **4 人日**。

### 4.6 上下文工程：这是当前框架最被低估的强项

**现状**（`engine.py:766-849`）：系统提示词只放**跨轮稳定**的块（人格、工具目录、礼仪）；每轮都在变的东西（群风格、群友记忆、约定、注入、自我记忆、本群身份、背景消息、心情、时钟、梗）**全部拼成一条 LIVE_CTX 尾部 system 块，放在历史之后**。原因写在注释里：放在提示词中段会让供应商的前缀缓存在几千 token 处断裂，后面全部按未命中计费。

摘要侧（`engine.py:1554-1615`）：锚点 `covered_up_to_id` 之后**只追加、不滑动**，两轮之间请求前缀字节一致；摘要重写按"积压阈值 + 最短间隔"节流（12 轮/6000 字符起步，12 条/2000 字符触发，48 条/12000 字符强制，间隔 ≥600s）。这实际上是一套"缓存感知的滚动摘要"。

**对照**：Claude Code 用 auto-compact（接近上限时压缩）+ microcompact（细粒度裁剪），不强调"前缀字节一致"。OpenAI SDK 的 Sessions 只负责持久化消息，压缩策略留给用户。LangGraph 的 checkpointer 保存状态，不处理模型侧缓存。

**判断**：**这一维度大肥鱼领先**，尤其是"易变块后置"的设计，比多数开源框架更懂供应商缓存。可补的是"压缩质量"：

- 现在摘要是自由文本，缺结构。建议固定 schema：`已完成 / 未完成 / 关键事实 / 未决问题 / 人物关系`，便于后续检索与回放。
- `_maybe_compact`（长跑）与 `_prepare_context`（群聊）是两套压缩逻辑，建议共享一个"摘要器"，避免风格漂移。
- 增加"压缩前保留最近 N 轮完整工具链"的显式配置（已有 `_compact_cut` 的 `keep_rounds`，但群聊侧没有同等能力）。

### 4.7 记忆：产品级领先，检索还粗

**现状**：`storage.py` 40+ 张表，记忆相关至少 8 套：群友记忆（`memory_entries`）、自我记忆（`self_memories`，跨群共享）、群风格（`group_style`）、约定（`commitments`）、心情（`mood_state`/`mood_timeline`）、提示词注入（`prompt_injections`）、梗（`meme.db`）、原始消息游标（`raw_cursors`）。召回按发言人 + 被 @ 者 + 当前文本做关键词/相似度，`recall_block` 拼进 LIVE_CTX。

**对照**：Claude Code 只有 CLAUDE.md 与文件系统；OpenAI SDK 的 Sessions 是消息史；LangGraph checkpointer 是执行状态。**开源框架普遍没有"群友记忆/自我记忆/风格/心情"这种社会学分层**——这是大肥鱼真正的差异化资产，值得在产品叙事里强调。

**差距**：检索是单轮的"关键词 + 最近性"，没有"这条记忆上次被用是什么时候、用了之后对话是否变好"的反馈环；记忆写入也没有冲突消解（同一事实两种说法都存）。可借鉴 RAG 的成熟做法（混合检索 + 重排 + 时效衰减），但不必上向量库——SQLite + FTS5 足够。

### 4.8 扩展性：没有 hooks，只有插件热重载

**现状**：宿主有插件系统（`plugin_loader.py`）与热重载，但**agent 内部没有生命周期钩子**。想在"工具执行前拦截""每轮开始前注入""摘要完成后通知"做定制，只能改 `engine.py`。对比 Claude Code 的 30+ 事件与 5 种 handler 类型（shell/HTTP/MCP/prompt/agent），差距明显。

**判断**：不需要照搬 shell handler（安全面太大），但**进程内 Python hook 点**成本低、收益高：

1. 定义 `HOOK_POINTS`：`before_turn / before_tool / after_tool / after_turn / before_compact / after_compact / on_gate`。
2. 插件通过 `register_hook(point, fn)` 注册，同步/异步皆可，返回 `None`/`{"block": reason}`/`{"context": text}`。
3. 内置几个官方 hook 作为示例：敏感词拦截、成本熔断（单群日 token 超限则降级）、审计日志。
4. 失败默认放行（fail-open），但要能配置 fail-closed——与 Claude Code 的 `onFailure` 语义对齐。

工作量约 **2 人日**。

### 4.9 状态与恢复：能回放，不能续跑

**现状**：每轮消息、工具结果、用量都落 SQLite，理论上可以回放整段对话；但**长跑任务**（`loop_run.py`）的 `_tasks` 是进程内 `dict[str, asyncio.Task]`（`loop_run.py:117`），进程重启后 `status_text`（86-92）把残留 `running` 标成 `interrupted`——**没有断点续跑**。机器重启、插件热重载、OOM 都会让一个跑了 3 小时的任务消失，只能靠 `loop_sessions` 里的人工可读进展重新发起。

**对照**：LangGraph 的 checkpointer 在每个 super-step 落盘，重启后从最近检查点继续；OpenAI SDK 的 `RunState` 可序列化/恢复；Claude Code 有 checkpoints/rewind（回退到某个点重来）；AutoGen 有 `save_state/load_state`。

**判断**：长跑是产品卖点（README 的 7 大能力之一），恢复能力却是短板。建议：

1. 每完成一个工具轮，把 `{round, messages_delta, last_tool_results, plan_note}` 写 `loop_checkpoints` 表（新增，走 `user_version` 迁移）。
2. 重启时 `LoopManager.startup_recover()`：把 `interrupted` 且 `resume_on_boot=true` 的任务重新入队，从最近 checkpoint 重建消息。
3. 幂等：副作用工具（发送、写文件）在恢复时按 `tool_call_id` 去重，避免重复发消息。

工作量约 **5 人日**（含测试）。

### 4.10 可观测与评测：事件流有了，评测没有

**现状**：`live.py` 提供 SSE `/live/events`（`Last-Event-ID` 补播、20s 心跳、1000 条环形缓冲），事件覆盖 `turn_start / gate / prompt / model_round / tool_result / assistant_send / compact / notice / turn_end`；Web 控制台 11 页；`usage_log` 记录每轮 token 与成本。

**对照**：Claude Code 支持 OpenTelemetry 指标/日志；OpenAI SDK 内置 Tracing（span 树 + 可视化 + 评测联动）；LangSmith 提供评测数据集、回归对比、在线打分；OpenHands 有 SWE-bench 级评测。

**差距**：有"运行时可观测"，缺"变更时可回归"。现在改一句提示词、换一个模型路由，没有任何客观指标能回答"变好了还是变差了"。建议最小评测体系：

1. 30 条群聊场景回归集（`tests/evals/cases.yaml`）：每条 = 群上下文 + 用户消息 + 期望行为（回复/不回复/调某工具/不调某工具）。
2. 判官打分 + 规则断言（是否触发门禁、是否越权、工具序列是否包含某调用）。
3. CI 跑 50 条，报告"通过率 / 平均 token / 平均轮次"三项，与基线对比。
4. 把 SSE 事件映射为 OTel span（可选，2 人日），接入现有监控。

### 4.11 多代理：不该照搬"群聊多代理"

**现状**：无多代理。AutoGen/CrewAI 的"多代理"是多个 LLM agent 互相发言；本框架是**一个 agent 面对多个人类群成员**。两者都叫"群聊"，但社会结构相反。

**判断**：明确不采纳"多个 agent 在群里互相对话"的形态——成本翻倍、延迟不可控、且会破坏"她是一个群友"的人格设定。真正需要的是**主从式**子代理（4.5），而不是对等式多代理。AutoGen 的 GroupChat/Magentic-One 可以借鉴其"任务分解 + 投票/仲裁"思想，但不引入其运行时。

---

## 5. 关键差距清单

按"影响 × 成本"排序。P0 = 两周内可做；P1 = 一个迭代；P2 = 有明确需求再做。

| 编号 | 差距 | 证据 | 对照 | 建议 | 优先级 | 人日 |
|---|---|---|---|---|---|---|
| G-01 | 同轮多工具串行 | `engine.py:1126-1135` | Claude Code / LangGraph 并行 | 只读并发、写类串行，上限 4–8 | P0 | 1 |
| G-02 | 权限只有二值 | `builtin_tools.py:1334`、`engine.py:1753-1769` | allow/deny/ask + HITL | 三态 + Web 审批队列 | P0 | 3 |
| G-03 | 无通用子代理 | `video_read.py:849` 唯一 | Claude Code Task/子代理 | `spawn_subagent` 受限实例 | P0 | 4 |
| G-04 | 无 hooks | agent 内部无扩展点 | 30+ 事件 | 进程内 6 个 hook 点 | P0 | 2 |
| G-05 | 长跑无断点恢复 | `loop_run.py:117`、86-92 | LangGraph checkpointer | `loop_checkpoints` + 启动恢复 | P1 | 5 |
| G-06 | 工具 schema 全量注入 | 27,736 字符 ≈1.2–1.4 万 token | Claude Code 按需授权 | 两阶段披露（目录 + 按需 schema） | P1 | 3 |
| G-07 | 摘要无结构 | `ctx_mod.plan_summary` 自由文本 | — | 固定五段 schema | P1 | 2 |
| G-08 | 无评测集 | 仓库无 evals | LangSmith / SWE-bench | 30 条场景回归 + 判官 | P1 | 4 |
| G-09 | 无 OTel/trace 标准 | 自研 SSE 事件 | Claude Code OTel | 事件映射 span | P2 | 2 |
| G-10 | 记忆检索无反馈环 | `memory.recall_block` | RAG 实践 | 命中率埋点 + 时效衰减 | P2 | 3 |

> 合计 P0 约 10 人日、P1 约 14 人日、P2 约 5 人日。

---

## 6. 不该照搬的

差异书最容易被误读成"缺什么补什么"。以下四项是**明确不建议**的：

1. **plan mode 强制**：Claude Code 的 plan mode 服务于"改代码前先给方案"。群聊里没人想看计划文档，模型多一轮输出就是多一轮延迟和成本。当前"先判官后回复"的门禁已经承担了类似职责（决定要不要动手）。
2. **文件编辑工具集**（Read/Write/Edit/MultiEdit/NotebookEdit）：群聊场景的"文件"是群文件与多媒体，已由 `fs.*` + `read_image/read_video/view_document` 覆盖。照搬编码工具会把工具表从 66 扩到 80+，进一步推高 schema 成本。
3. **CLI 交互形态**：斜杠命令、终端 UI、diff 预览都假设"一个用户在终端前"。本产品的交互面是 QQ 消息 + Web 控制台，形态已经对。
4. **图编排 DSL**：LangGraph 的 StateGraph 适合"步骤确定的流水线"。群聊是事件驱动、步骤不确定的，引入图只会把 `_chat_loop` 的灵活性锁死。需要确定性时用"子代理 + 受限工具集"更轻。

另外两点**需要谨慎**：

- **多代理互聊**（4.11）：成本与人格风险都高。
- **全量 schema 动态裁剪**（G-06 方案 B）：模型"想不起来某工具"的退化比省 token 更难排查，建议 A 方案灰度。

---

## 7. 演进路线

### 阶段一（P0，约 10 人日，两周）

1. **并行工具**（G-01）：`engine.py` 的 `_chat_loop` 改造 + 并发上限配置 + 顺序回填 + 失败占位测试。
2. **权限三态**（G-02）：`permissions` 配置段 + `ask` 审批队列 + Web 面板"待审批"页 + 审计日志。
3. **通用子代理**（G-03）：抽 `spawn_subagent`，先接 `read_video` 与"历史深度检索"两个场景。
4. **hooks**（G-04）：6 个 hook 点 + 插件注册 API + 两个官方示例（成本熔断、敏感词）。

验收：现有 78 个测试全绿；新增 ≥12 个测试（并发顺序、审批超时、子代理超时降级、hook 阻断/放行）；SSE 事件新增 `approval_request/approval_result`。

### 阶段二（P1，约 14 人日，一个迭代）

5. **断点恢复**（G-05）：`loop_checkpoints` 表 + 启动恢复 + 副作用去重。
6. **工具两阶段披露**（G-06）：`tool_search` + `tools_guide` 瘦身，灰度开关。
7. **结构化摘要**（G-07）：五段 schema + 两个压缩入口共享。
8. **评测基线**（G-08）：30 条场景 + 判官 + CI 报告。

### 阶段三（P2，按需）

9. OTel 导出（G-09）。
10. 记忆检索反馈环（G-10）。

---

## 8. 结论

**能力画像**：

```
                    编码任务      群聊陪聊
Claude Code            ●●●●●         ○
OpenAI Agents SDK      ●●●◐○         ◐
LangGraph              ●●●●○         ◐
大肥鱼                 ◐◐○○○         ●●●●◐
```

两者几乎正交。把 Claude Code 的"器官"逐个移植过来，不会让大肥鱼变成更好的群友；但**四个通用器官**——并行工具、权限分级、子代理、hooks——是任何"会动手的 agent"都需要的，而且成本可控（10 人日）。

**战略建议**：把差异化继续押在"社会性"（人格、多套记忆、心情、主动、长跑、群友感），把工程学补齐到"不拖后腿"。差异书的用途不是让大肥鱼像 Claude Code，而是回答一个问题：**当模型能力继续上涨时，框架里哪些部分会成为瓶颈？** 答案是：工具循环的并发、权限的粒度、上下文隔离的能力、以及出问题后能不能恢复——恰好就是 G-01 到 G-05。

---

## 附录 A：参照框架文档

- Claude Code Hooks：https://code.claude.com/docs/en/hooks
- Claude Code Subagents：https://code.claude.com/docs/en/sub-agents
- Claude Code 权限与设置：https://code.claude.com/docs/en/settings
- OpenAI Agents SDK：https://openai.github.io/openai-agents-python/
- OpenAI Agents SDK 源码：https://github.com/openai/openai-agents-python
- LangGraph 持久化与 interrupt：https://langchain-ai.github.io/langgraph/
- AutoGen：https://microsoft.github.io/autogen/
- CrewAI：https://docs.crewai.com/
- OpenHands：https://docs.all-hands.dev/

## 附录 B：当前框架证据索引

| 结论 | 位置 |
|---|---|
| 每群串行锁 | `plugins/deepseek/deepseek.py:216-220` |
| 一轮主流程 | `plugins/deepseek/Core/engine.py:672-980` |
| 触发门禁 | `engine.py:568-593` |
| 判官 fail-closed | `engine.py:648-668` |
| 上下文组装（稳定/易变分离） | `engine.py:766-849` |
| 摘要锚点与节流 | `engine.py:1554-1615` |
| 工具循环串行 | `engine.py:1126-1135` |
| 轮次上限收尾 | `engine.py:1195-1240` |
| 工具异常隔离 | `engine.py:1794-1809` |
| 工具结果补齐 | `engine.py:1771-1792` |
| 工具定义（66 个） | `builtin_tools.py:24-91` |
| 风险工具集合 | `builtin_tools.py:1334+` |
| 二值主人门禁 | `engine.py:1753-1769` |
| 默认配置 | `Core/config.py:79-81,190,260` |
| 长跑进程内任务表 | `Core/loop_run.py:117` |
| 重启标记 interrupted | `Core/loop_run.py:86-92` |
| 视觉子代理 | `Core/video_read.py:849` |
| SSE 事件流 | `Core/live.py` |
| 多套记忆表 | `Core/storage.py`（40+ 表） |

## 附录 C：术语

- **门禁（gate）**：决定"这条消息要不要唤醒她"的轻量判定，分启发式与初筛脚本两级。
- **判官（judge）**：用便宜模型判断"要不要回复、怎么回"的中间层。
- **LIVE_CTX**：每轮易变上下文的尾部 system 块，为前缀缓存而放在历史之后。
- **摘要锚点（covered_up_to_id）**：已摘要到的消息 id，之后的消息只追加不滑动。
- **长跑（loop）**：跨多轮工具调用、可能持续数小时的任务，期间按轮汇报进展。
