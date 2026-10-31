# 2026-10-27 更新汇总（v2026.10.27.1 / v2026.10.27.2）

三批更新：**记忆回忆链第一期**（自我记忆的召回/改写留痕，主人只读）、
**好友申请 / 群邀请审批**（框架补上 request 事件通道，AI 暂存通知、主人拍板）、
**MCP 分群开关修复 + 前缀缓存重排 + 多服务商模型路由**。
数据库迁移到 **v15**（第二批不动表结构）。

## 第二批：v2026.10.27.2

| # | 更新 | 入口 | 默认 |
|---|---|---|---|
| 3 | MCP 工具分群开关修复（旧版勾选实际不生效的 bug） | Web `deepseek` 群设置 →「工具勾选」 | 默认全开 |
| 4 | 系统提示词重排：稳定块在前、易变块（风格/记忆/约定/注入）压尾 | 无 | 自动 |
| 5 | 心情判定 system 常量化（口径/幅度/当前心情值挪进 user 消息） | 无 | 自动 |
| 6 | 多服务商模型路由：服务商共享 Base URL + Key，六个路由各自选模型 | Web 设置页「账号 / 模型」 | 迁移旧配置 |

### MCP 分群开关修复

群设置页的 MCP 勾选之前有双层前缀 bug：存进库的 key 是
`mcp__sid__mcp__sid__tool`，和引擎实际使用的 `mcp__sid__tool` 对不上，
勾选不生效；且禁用的服务器工具也会列出。修复后：

- 只列**已启用** MCP 服务器的缓存工具，按服务器分组显示；
- 关掉的工具既不进模型的工具 schema，**dispatch 映射里也同步摘除**——
  模型靠幻觉/上一轮缓存硬调被关工具时返回「工具不可用」，不会执行；
- `/ai 工具` 在每个 MCP 服务器下展开每个工具的本群 ✅/❌。

### 前缀缓存重排

`engine._system_prompt` 零件顺序从
`人设→身份→nospeak→媒体→CQ禁令→风格→记忆→约定→注入→工具目录→MCP规范`
改为
`人设→身份→nospeak→媒体→CQ禁令→工具目录→MCP规范→风格→记忆→约定→注入`。

依据见 [`缓存命中率诊断.md`](缓存命中率诊断.md)：chat 命中率 92.8%，
失血点是风格/记忆/约定/注入四个每轮易变块排在 1.4 万 token 的工具目录之前，
它们一动后面全部重算。时钟/在场者等独立 system 消息的顺序不动。
近因效应取舍：工具目录离最后一条用户消息变远、记忆/注入变近，观察 1-2 天命中率。

心情判定（mood 命中率 0.5%）的 system 也改成常量公共前缀：
本轮口径、幅度上限、当前心情值/七绪现状全部挪到 user 消息开头；
解析后的幅度钳制（±cap）仍在代码侧兜底，模型乱报不会越界。

### 多服务商模型路由

设置页「账号 / 模型」改造：

- **模型服务商**：每家一个显示名、ID、API 地址、API Key，下面挂任意多个模型
  （模型 ID + 可选显示名）。同一家共享一个 Base URL 和 Key。
- **模型路由**六个下拉，按服务商 optgroup 分组：
  对话 `model`、判官 `judge_model`、摘要 `summary_model`、
  记忆抽取 `extract_model`、心情 `mood_model`、风格 `style_model`。
  对话模型仍可在各群设置里覆盖，其余五个全局一套；定时文案跟随对话模型、
  ASR 模型名仍自由填写（可写 `服务商ID/模型名` 指定走哪家）。
- 路由值格式 `服务商ID/模型ID`（按第一个 `/` 切分，模型 ID 自身可含 `/`）；
  裸模型名只对 `default` 服务商有效。
- Key 保存后不回显（只露首尾 4 位提示），留空/`********` 表示不修改；
  `default` 服务商 Key 留空时读环境变量 `DEEPSEEK_API_KEY`。
- 删除正在被全局路由或某群模型引用的服务商/模型时，后端 **400 拒绝整次保存**，
  页面列出需要先改的引用。
- 旧 `api_key / base_url / model / judge_model` 配置首次加载自动迁移成
  `default` 服务商；框架弹窗（web.json）不再能写模型/账号，弹窗保存也不会
  再冲掉未登记的配置键（改为与内存全量配置合并）。

---

## 第一批：v2026.10.27.1

| # | 更新 | 入口 | 默认 |
|---|---|---|---|
| 1 | 记忆回忆链第一期：召回/改写/作废/网页改只追加留痕 | Web 面板 `deepseek/memory` →「回忆链」 | 开（`self_memory_trace_on`，可按群关） |
| 2 | 好友申请 / 拉群邀请：暂存 + 私聊通知主人 + 主人对话拍板 | 私聊通知 + 3 个工具 | 开（`request_notify_on`，全局） |

---

## 1. 记忆回忆链第一期

**背景**：自我记忆有三档自动合并（原地改写 / 补版作废旧条目 / 新增），
之前只能看到当前文本和最近 5 版旧文本，回答不了"她是**什么时候、在哪个群、
因为哪句话**想起或改掉这条记忆的"。

本期新增只追加表 `self_memory_traces`（DB v15）：

- **召回即留痕**：每轮自我记忆召回命中时，为每条被采用的记忆记一条 `recall`：
  触发语（截断 300）、来源群、来源场景（前台/后台苏醒/主动搭话/入群/抽取/工具）、
  当时的心情标签与心情值、命中时的融合分、当时文本。同轮同条只记一次，
  与 access_count 的 touch 合并在**同一笔事务**。
- **事实层任何改写强制留痕**：三档合并的原地改写 → `revise`（记 `旧:… | 新:…` 对照）；
  相似条目补版作废 → `supersede`；Web 记忆页编辑 → `web_edit`。写入口只有三处，
  全部在 DAL 层同事务闭环，绕不过去。
- **主人只读视图**：Web「群友记忆」页的自我记忆表格每行显示回忆链条数，
  点「回忆链」懒加载展开，倒序看时间/类型/来源/群/心情/命中分/触发语/改写对照。

关键安全约定：**trace 永远不进模型上下文**——没有任何召回工具会读到它，
防止她"照着痕迹表演"。这是只给主人看的审计视图。

本期**刻意不做**（先攒真实数据）：LLM 压缩/digest、周报式矛盾发现、
心情加权召回排序。开关在全局设置（也可按群覆盖），关闭后退化为只 touch 不留痕。

---

## 2. 好友申请 / 群邀请审批

**背景**：框架之前只有 meta/message/notice 三类事件分支，
OneBot 的 `post_type=request` 事件（好友申请、加群申请、群邀请）被静默丢弃，
机器人面对好友申请无能为力。

### 框架层（纯加法）

- `utils/obj.py` 新增 `RequestEvent`（`request_type / sub_type / user_id /
  group_id / comment / flag`，原始 dict 仍完整保留）。
- `core/events.py` 新增 request 分支，与 notice 同路径派发
  （async handler 建 task，sync handler 丢 executor）。
- `utils/Plugin.py` 新增 `@Plugin.on_request("request")` 装饰器。
- 没有插件监听时事件被安全忽略。插件开发见
  [`plugin-guide.md`](plugin-guide.md)「请求事件」一节。

### deepseek 插件行为

详见 [`request-approval-guide.md`](request-approval-guide.md)：

- 收到**好友申请**或**拉机器人进群的邀请**（`sub_type=invite`）→ best-effort
  补昵称/群名后落库（`pending_requests` 表，flag 去重）→ 私聊主人带编号通知；
  别人申请入群（`sub_type=add`）忽略。
- 主人对话拍板：「同意申请#1」「拒绝申请#1，理由…」，
  由 `request_list / request_approve / request_reject` 三个工具执行；
  后两个在风险工具名单里，**只有主人 QQ 能指令**（认号不认昵称），
  非主人指令在引擎层直接拦截，不会调平台 API。
- flag 过期/平台报错 → 标记 `failed`，不自动重试；已处理不可重复操作。
- NapCat action 约定：`set_friend_add_request {flag, approve, remark?}`
  （remark 仅同意时有意义）、`set_group_add_request {flag, approve, reason?}`
  （不需要 sub_type）；正向 WS 不支持快速操作。

---

## 测试

- 新增：`test_framework_request_events.py`（5）、
  `test_deepseek_migration_v15.py`（7）、
  `test_deepseek_memory_traces.py`（5）、
  `test_deepseek_requests.py`（21）。
- 第二批：重写 `test_deepseek_config.py`（16，旧配置迁移/六路由/裸 id/悬空重置/群覆盖）、
  `test_deepseek_llm.py`（14，多服务商路由/中文错误/mood 常量 system 与钳制/客户端缓存淘汰）、
  新增 `test_deepseek_providers_web.py`（9，脱敏/密钥对账/悬空 400/弹窗子集保护）、
  `test_deepseek_engine.py` 增补 MCP 分群关停与提示词顺序 2 例。
- 既有 v12/v14 迁移测试与提示词目录测试的版本号/目录断言同步更新。
- 全量 `python -m pytest tests/ -q` 通过。
