# 心情 · 自我记忆 · 主动搭话（v2026.10.26.3）

参考 `docs/DSH四插件协作原理.md`、`docs/多群聊多用户下的人格一致与记忆连续.md`，
在 deepseek 插件内新增三件互相独立、默认全开的能力：

| 能力 | 粒度 | 开关（均可每群覆盖） | 额外模型调用 |
|---|---|---|---|
| 心情 mood | **每群一份**（私聊以对方 QQ 为群号，天然独立） | `mood_on` | 每次非静默回复后 1 次便宜模型（设置页「心情模型」mood_model，v2026.10.27.2 前共用 judge_model） |
| 自我记忆 self memory | **全局一份**，跨所有群/私聊共享 | `self_memory_on` | 0 次（搭车记忆抽取同一次调用） |
| 主动搭话 speak-up | 每群计数，挂在定时苏醒同一次 tick | `speakup_on` | 命中才产生 1 次正常对话 |

三项都不碰群友长期记忆（`memory_entries`，那是"关于别人"的档案）。

---

## 一、心情（每群一份）

### 状态

- **心情值** 0–100，基线默认 70。没有事件时按 **3 小时半衰期** 向基线回落
  （纯读取时计算，没有后台定时器）。
- **七绪**：开心 / 踏实 / 甜蜜 / 心疼 / 担心 / 难过 / 吃醋，各 0–100，
  **90 分钟半衰期**，更快平复。
- 每群存在 `mood_state` 一行当前状态，`mood_timeline` 记流水
  （每群默认保留最近 200 条，可配 `mood_timeline_keep`）。

### 什么时候判

每轮**她实际发出了非 `[静默]` 回复之后**，异步跑一次便宜模型判定（不阻塞回复、
不阻塞事件循环；判定任务异常只记日志，不影响任何主流程）。判定带来源口径，
单次变化有上限，防止一句话心情崩盘：

| source_kind | 场景 | 心情值变化上限 |
|---|---|---|
| `human` | 普通群聊/私聊回复（含未知来源 fail-open） | ±10 |
| `talk` | 主动搭话轮 | ±5 |
| `work` | 定时苏醒 / 外部触发等后台干活轮 | ±3 |
| `greet` | 新成员进群欢迎 | ±5 |

判官铁律（见 `Core/llm.py::judge_mood`）：

- 只允许依据本轮真实出现的信息，禁止脑补情节；
- **心情下降只能来自"心疼/担心群友"或"她自己难过"，绝不允许"对方惹她生气"**；
- 理由必须是两句过去式、带具体昵称，禁出现"此刻 / 一直 / 用户"；
- 没变化就老实返回 delta=0。

### 怎么影响对话

判定结果**不会立刻**塞进提示词，而是作为"待宣布变化"挂起，下一轮组装消息时
由 `MoodManager.render_injection` 决定是否注入。要同时满足：

1. `mood_on` 开着；
2. 确有未宣布的新判定（`pending_tid` 且尚未注入）；
3. `|delta| ≥ mood_delta_threshold`（默认 5）——小变化只记账，不说；
4. `confidence ≥ mood_min_confidence`（默认 60）——没把握不说；
5. 本群今日注入次数 < `mood_daily_cap`（默认 6，按本地自然日滚动）；
6. 理由非空。

注入文案是一句过去式的**背景**，显式告诉模型"这不是这一轮必须有的情绪，
不用主动提起，也别重复它"：

```
（你刚才那一下的心情背景：{理由}这是背景，不是这一轮必须有的情绪，不用主动提起，也别重复它。）
```

位置在"触发前背景消息（ambient）"块之后、对话窗之前；**前台轮和后台轮都生效**。
采用"先占后用"：决定注入的瞬间就落库标记（`mood_mark_injected`），
所以即使该轮之后失败也不会重复注入——**一次变化最多说一次**。

### 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `mood_on` | true | 总开关，可在群设置页每群覆盖 |
| `mood_baseline` | 70 | 心情基线 |
| `mood_delta_threshold` | 5 | 低于此幅度只进时间线 |
| `mood_min_confidence` | 60 | 判官信心门槛 |
| `mood_daily_cap` | 6 | 每群每天最多注入几次 |
| `mood_timeline_keep` | 200 | 时间线每群保留条数 |

半衰期（心情 3h、七绪 90min）是代码常量（`Core/mood.py`）。

### Web

群设置页底部"本群心情"面板：当前心情值（实时衰减后）、尚存的七绪、
待宣布变化、时间线（时间 / 来源 / Δ→结果 / 理由 / 是否已注入），
以及"重置为基线"按钮（写一条 `kind=reset` 流水，清空七绪与待宣布项，历史保留）。

接口：`GET /deepseek/api/groups/<gid>/mood?limit=`、
`POST /deepseek/api/groups/<gid>/mood/reset`。

---

## 二、她自己的第一人称记忆（全局一份）

`memory_entries` 记的是"关于某个 QQ 的事实"（per-user，跨群合并）；
新表 `self_memories` 记的是**"我"的记忆**，主语必须是机器人自己：

- 我亲口答应过但还没做的小事（"我说过要把那张表整理给阿明"）；
- 我和某个群友关系的进展（"我和小林开始互相吐槽老板了"）；
- 我亲身经历、跨群也该记得的事。

**全库一张表**：在 A 群记下的自我记忆，在 B 群和所有私聊里都能召回——
这就是"换群不换人"的人格连续。日常拌嘴、一次性寒暄不记。

### 写入（两条路径，共享每日配额）

1. **搭车抽取（不另花钱）**：记忆抽取提示词新增 C 段 self_entries，
   `extract_memories` 真实返回 4 元组；消费端宽容解包（`len(res)==4`，
   旧的 3 元组 fake 照常工作）。只在每群记忆抽取游标推进成功后落库。
2. **工具**：`memory.remember_self`（记）/ `memory.recall_self`（查），
   非风险工具，所有群友可用。单条上限 200 字。

每日配额 `self_memory_daily_quota`（默认 8）按本地日计数，
**工具写入与抽取写入共享**；**就地改写/合并旧条目不耗配额**，只有真正新建才计数。
配额满后剩余抽取条目丢弃并记日志，不报错。

### 合并 / 召回

- 与群友记忆同款 BM25（中日韩 unigram/bigram，无第三方依赖）+
  SequenceMatcher 三档合并（≥0.8 就地改写；op=update 且 ≥0.5 就地改写；
  ≥0.5 旧条作废再新建；否则新建），旧文本入 history（留 5 版）。
- 每轮用本轮文本作 query 做融合排序（0.75 BM25 / 0.15 重要度 / 0.10 时效），
  取 top `self_memory_recall_topk`（默认 5）、总预算
  `self_memory_recall_budget`（默认 300 字），滑窗内与上轮重复的条目自动去重并
  touch（更新最近访问）。
- 召回块是独立 system 消息（在摘要块之后），头部明确"跨所有群/私聊同一份；
  不合时宜不要主动提起，也不要逐字复述"。
- 后台轮（苏醒/搭话/触发）的 query 取窗口内**最后一条真人消息**，
  绝不会拿通知长指令自身去检索。
- `self_memory_on=false` 时召回块、抽取落库与两个工具全部摘除
  （工具 schema 直接不发给模型）。

### Web / 接口

「群友记忆」页下半部"她的自我记忆"区：列表、就地编辑、作废/恢复、彻底删除、
"显示已作废"。接口：

- `GET /deepseek/api/self-memories?all=1`
- `PUT /deepseek/api/self-memories/<id>`（text/tags/importance/superseded 部分更新）
- `DELETE /deepseek/api/self-memories/<id>`
- `/deepseek/api/memory/stats` 增补 `active_self_memories` / `total_self_memories`。

---

## 三、主动搭话（speak-up）

区别于"防假死苏醒"（`[定时苏醒]` 闹钟口吻、检查有没有事要干），
搭话是**她自己想说话**：冷场时主动起个话头。与苏醒共用 30 分钟一次的
proactor tick，分层掷骰，**每 tick 每群至多一次主动轮**。

### 概率线（纯函数，可注入 roll 测试）

```
基础概率 = is_peak(now) ? speakup_peak_percent(20) : speakup_valley_percent(60)
峰时     = 工作日 9:00–12:00、14:00–18:00（周末全天为谷）
时机系数 = 距最后一条真人消息 < speakup_recent_minutes(20min) → 0.15（刚聊过不黏人）
           距最后一条真人消息 > speakup_cold_hours(6h)        → 1.5（冷场更想说话）
           否则 1.0
连空加成 = min(speakup_miss_cap(40), 连空次数 × speakup_miss_bonus(10))
搭话线   = clamp(基础概率 × 时机系数 + 连空加成, 0, 100)
```

判定顺序（`speakup_decision`）：

1. **安静时段** `speakup_quiet`（默认 `01:00-08:00`，支持跨午夜；留空=全天；
   格式非法 fail-open 按关闭并记警告）→ skip，不动任何计数；
2. **自我冷却**：距她自己上次发言 < `speakup_self_cooldown_minutes`（20min）
   → skip，不计数；
3. 掷骰 ≤ 搭话线 → **hit**；否则 miss，连空 +1。

### tick 分层（`Core/proactor.py`）

1. 有 AI 自建轮询（polls）的群：轮询独占本次 tick，**不掷搭话骰**（避免双重主动）；
2. 活跃门（`wake_active_hours`，默认 12h 无真人消息不打扰）由苏醒与搭话共享，冷群直接 return；
3. `speakup_on`：先掷搭话——
   - hit → `proactor_record(fired=True)`（**搭话连空与苏醒保底计数同时清零**）
     → group_lock 内跑 `handle_wake(speakup=True)` → 本 tick 结束；
   - miss → `proactor_bump_speakup_miss`（只动搭话计数，不碰苏醒 ticks）→ 继续往下；
   - skip → 静默继续；
4. `wake_on`：旧苏醒逻辑**字节级不变**（保底/分时段/10%）。苏醒命中同样清两个计数。

`speakup_on=false` 时整个搭话层不存在，行为与旧版完全一致。
两个总开关都关的群 tick 直接返回。

### 防错位框（关键）

命中后塞入窗口的是一条隐藏通知行（`name='speakup-notice'`，
`render_script`/`render_openai` 均不显示，前缀 `[主动搭话]`），
核心文案（`speakup_user_text`）：

> 现在是 …，群里已经大约 N 没有人说话了。这不是任何人发的消息，也不是定时任务或闹钟。
>（下面这句是你自己心里冒出来的念头，不是别人对你说的话；顺着它自己起个话头去做，
> 别回答它、别把它当成谁在叫你、也别解释这个念头：也许现在可以主动去群里说点什么——
> 一句应景的问候、一个新话题、或者提起你答应过但还没做的小事。）

并保留 `[静默]` 契约：确实没什么可说时允许静默。后台记录 kind=`speakup`
（Web「后台/轮询」页显示为"主动搭话"标签），该轮心情判定 source=`talk`（±5）。

通知行隐藏三处必须同步：`context.BG_NOTICE_NAME/_PREFIX/_bg_notice`、
`storage._HISTORY_HIDDEN_NAMES`、`runs.html` 标签——新增通知类型时照此办理。

### 配置

| 键 | 默认 |
|---|---|
| `speakup_on` | true（可每群覆盖） |
| `speakup_peak_percent` / `speakup_valley_percent` | 20 / 60 |
| `speakup_recent_minutes` / `speakup_cold_hours` | 20 / 6 |
| `speakup_self_cooldown_minutes` | 20 |
| `speakup_miss_bonus` / `speakup_miss_cap` | 10 / 40 |
| `speakup_quiet` | `"01:00-08:00"`（字符串，不做整数转换） |

tick 间隔沿用 `wake_check_minutes`。设计取向：谷时 + 冷场 + 连空后搭话线接近
100%——"她迟早会开口"，频率由安静时段、20 分钟自我冷却、12h 活跃门与
`[静默]` 共同收口；bg_runs 表可观测实际频率。

---

## 四、存储与升级

`PRAGMA user_version` 升到 **12**（`Core/storage.py::_DDL_V12`）：

- 新表 `mood_state` / `mood_timeline`(+索引) / `self_memories`(+索引) /
  `self_memory_state`；
- `proactor_state` 加列 `speakup_miss`（ALTER 带 `table_info` 守卫，可重复执行）；
- v11 → v12 旧行原样保留，`speakup_miss` 默认 0。

心情与自我记忆管理器**没有循环线程**，不需要 start/stop；判定/抽取都是
fire-and-forget 自弃任务（与记忆抽取同姿态），重连重建插件实例时在飞任务
自然随旧实例消亡。

## 五、测试

- `tests/test_deepseek_migration_v12.py` —— 全新迁移/幂等/v11 升级保留旧行；
- `tests/test_deepseek_mood.py` —— 衰减、钳制、来源上限、注入门槛/日上限/
  只注入一次/隔日重置、引擎接线、静默不判、speakup 通知隐藏、join→greet；
- `tests/test_deepseek_self_memory.py` —— 三档合并、跨群去重、配额共享、
  召回排序/预算/去重、工具摘除、4 元组/3 元组兼容；
- `tests/test_deepseek_speakup.py` —— 峰谷表、安静时段、时机系数、概率线钳制、
  decision 计数效应、tick 分层（hit 跳过苏醒 / miss 后苏醒仍可中并清连空 /
  polls 群不掷 / 冷群不掷 / 单 wake_on 逐字一致 / 双关总门）、文案与隐藏、Web API。
