# DSH 四个插件是如何在一起工作的

> 分析对象（GitHub: [Yuuyuko-uu](https://github.com/Yuuyuko-uu)）：
>
> | 插件 | 版本 | 一句话 |
> |---|---|---|
> | [dsh-fish-memory](https://github.com/Yuuyuko-uu/dsh-fish-memory)（鱼的记忆） | 1.3.1 | 会淡忘的长期记忆：模型自己调 `remember` 记，每轮按证据本地召回 |
> | [dsh-speak-up](https://github.com/Yuuyuko-uu/dsh-speak-up)（主动搭话） | — | 定时器本地掷骰子，掷中就往会话里推一句话，让 AI 自己开一轮 |
> | [dsh-bili-subagent](https://github.com/Yuuyuko-uu/dsh-bili-subagent)（刷B站） | 0.8.5 | 谷时往会话里递一句话，AI 调工具派**子代理**去刷 B站，回来再决定怎么告诉你 |
> | [dsh-feiyu-mood](https://github.com/Yuuyuko-uu/dsh-feiyu-mood)（鱼的心情） | 0.7.2 | 每轮结束后让模型判一次心情涨跌，注入下一轮，并在右下角挂饰显示 |
>
> 本文只基于源码分析，不涉及安装运行。DSH = DeepSeek Harness，DeepSeek 官方的桌面/网页 AI 客户端，插件体系基于 **Cordis**（服务容器 + 事件总线）。

---

## 一、先看总图

四个插件**彼此之间几乎没有直接依赖**，它们是插在同一台 DSH 上的四片功能，全部通过 DSH 提供的少数几个挂钩点协作：

```
        定时器（本地、不花钱）
        ┌──────────────────────────────┐
        │ speak-up：30 分钟掷一次骰子   │
        │ bili：60 秒看一次谷时/轮次表  │── 条件满足 ─┐
        └──────────────────────────────┘            │
                                                     ▼
                          agent.followup(一条 role=user 的消息)
                          消息上盖着来源章：source.kind
                                                     │
                            ┌────────────────────────┴───────────────────────┐
                            ▼                                                        ▼
                   DSH 自己的 agent loop（和真人打字走同一条路）
                            │
   组装这一轮的提示词时，各插件分层挂东西：
   · fish-memory  systemPrompt.section（常驻：「可以自己记」）
   · fish-memory  systemPrompt.context（本轮召回的记忆）
   · feiyu-mood   systemPrompt.context（上一轮的心情，一次变化只说一次）
   · bili         tools.register（bili_scan 等 13 个工具，可派生子代理）
   · fish-memory  agent.ctx.tools.register（remember，仅开启的会话）
                            │
                            ▼
                       AI 回复这一轮（可能调工具、派子代理、发评论/私信）
                            │
                            ▼
              agent/turn-stopping 事件（每轮结束必发）
           ┌────────────────────────┴────────────────────────┐
           ▼                                                 ▼
  feiyu-mood：取最后一条真消息，                fish-memory：检查回复里
  按 source.kind 选判断口径，                          像不像用上了召回的记忆
  花一次短模型调用判心情涨跌                           （trace: assembled→consumed）
```

**一句话概括它们的关系：**

- **speak-up 和 bili 是「触发器」**——负责在没有真人输入时往会话里注入消息；
- **fish-memory 是「过去」**——每轮提示词里多一段她记得的事；
- **feiyu-mood 是「内心」**——每轮结束后更新心情，再喂给下一轮；
- 四者唯一的**显式协议**是注入消息上的 `source.kind` 字符串；其余协作全部是 DSH 事件总线上的**隐式合奏**。

---

## 二、共同的插件骨架（理解协作的前提）

四个插件长得像一个模子刻出来的，因为作者沉淀了一套固定写法。

### 2.1 宿主半边 + 网页半边

每个插件都是两段：

| 半边 | 入口 | 运行在 | 干什么 |
|---|---|---|---|
| 宿主半边 | `main`（`lib/index.js` / `lib/host.js` / `index.js`） | Node.js（DSH 进程内） | 定时器、事件监听、模型调用、工具注册、HTTP 路由 |
| 网页半边 | package.json 的 `dsh.client` → `client.js` | DSH 的 Web 界面里 | 设置页（React，自注册脚本 `window.__ModuleLoader__.load(...)`） |

挂载声明在自带的 **`cordis.patch.yml`** 里，只有一行 insert：

```yaml
- insert:
    - id: speak-up        # 各插件分别是 memory / mood / bili-scroll
      name: 'dsh-speak-up'
```

装上后 profile 的 `dsh.profile.bundles` 列出包名即生效，**必须重启 DSH**（bundle 层组装，非热重载）。

### 2.2 它们向 DSH 声明的服务依赖

```js
// speak-up
inject = ['agents', 'sessions', 'sessionTitle', 'webServer', 'timer', 'workspaceRegistry']
// feiyu-mood
inject = ['systemPrompt', 'agents', 'webServer', 'llm', 'timer']
// fish-memory
inject = ['systemPrompt', 'agents', 'timer', 'tools', 'webServer', 'sessions', 'sessionTitle', 'llm']
// bili-subagent
inject = ['webServer', 'tools', 'settings', 'subagents', 'agents', 'llm']
```

合起来看，四插件实际用到的 DSH 能力就是这几样：

| DSH API | 谁在用 | 作用 |
|---|---|---|
| `agent.followup(message)` | speak-up、bili | 往**活着的** agent 收件箱推消息 → loop 认领 → 开一个正常 turn |
| `agent.ctx.on('agent/inbox/inserted')` | mood、memory | 收到本轮输入（用来拿「她说了什么」） |
| `agent.ctx.on('agent/turn-stopping')` | mood、memory | 一轮回复结束时触发（判心情 / 记账） |
| `agent.ctx.systemPrompt.section({order})` | memory | 拼进**系统提示词**的静态段 |
| `agent.ctx.systemPrompt.context({order})` | mood、memory | 每轮可变、作为**末尾消息**追加的动态段（保前缀缓存，详见 §6） |
| `ctx.tools.register(defineTool(...))` | bili（全局）、memory（按会话） | 给模型注册可调用的工具 |
| `ctx.subagents.startActivation({provider:'spawn'})` | bili | 派生子代理 |
| `ctx.llm.stream(...)` | mood | 不走会话、直接发一次短模型请求（判心情） |
| `ctx.interval / ctx.timer` | speak-up、bili、mood | 本地定时器 |
| `ctx.webServer.register()` + `/dsh-xxx/*` 路由 | 全部四個 | 设置页读写 JSON、图片资源等 |
| `ctx.on('webserver/index-inject')` | mood | 往网页 HTML 推 `<script>`（右下角挂饰的唯一入口） |
| `slots.inject('settings.section', ...)` | 四个 client.js | 在设置里加自己的一页 |
| `ctx.agents`：`list/get/create/currentInitiator/roots` | 全部 | 找活着的会话、认父子关系 |

### 2.3 三份「复制粘贴的共用设施」

- **`lib/selfupdate.mjs`**（四个仓库里是逐字相同的 151 行）：设置页「检查更新」按钮——读自己 package.json 的 `repository`，去 GitHub 比版本号，只查不装，缓存 6 小时，查不到必须如实说「查不到」。
- **`lib/hostdeps.mjs`**（memory、bili、speak-up）：`@deepseek-ai/dsh-tools`、`schemastery` 这些官方包在第三方插件的安装位置**常常根本不存在**（optional peer，桌面端不装）。它负责从宿主那棵 node_modules 树的七八个候选位置解析，解析不到就退回插件自带依赖，再不行就**降级运行**（比如设置页少点东西），绝不能因为一个 import 失败让整条插件不激活。
- **同一份踩坑文档**：四份 README 都带同一段 UTF-8 BOM 急救指南（profile 的 package.json 被存成带 BOM 会让 DSH 启动直接崩）。

### 2.4 配置与数据全部落在 DSH 家目录

| 插件 | 家目录（`%USERPROFILE%\.dsh\` 下） | 特点 |
|---|---|---|
| speak-up | `speak-up-config.json` | 单文件 |
| fish-memory | `fish-memory\<会话id>.json` | 一个会话一份记忆，纯文本 JSON，改动前自动备份 |
| feiyu-mood | `feiyu-mood\config.json`、`state.json`、`diag.log` | 心情值 + 时间线 |
| bili | `bili-duty\config.json`、`state.json`、`bili_credential.json`、`out\*.md`、`.venv\` | 还包括独立 Python 虚拟环境和 B站登录凭证 |

**卸载插件只删代码，数据全留**——四份 README 都明确承诺了这一点。

---

## 三、协作的硬协议：消息三要素与 `source.kind`

这是理解四者合奏的钥匙。

### 3.1 followup 消息的形状

speak-up 和 bili 往会话里推的，都是一条**手工构造的、形状严格照抄官方 `createUserMessage` 的 user 消息**：

```js
{
  id: '随机 uuid',
  role: 'user',                    // ⚠ 只能是 user：DSH 当前版本硬拒收 developer/system
  content: [{ type: 'text', text: '……' }],
  source: { kind: 'speak-up' },    // ⭐ 来源章
}
await agent.followup(msg)
```

为什么必须走 `agent.followup()` 而不是自己起会话（speak-up 的源码注释写得很详细）：

1. followup 推进**同一个活着的 agent** 的收件箱，DSH 的 agent-loop 认领它、开 turn、跑 step——**和真人打字走的是同一条路**。于是她的人格、历史、工具、记忆、心情注入全都照旧生效；
2. DSH 的 `step()` 把认领的消息**写死**成 `user/message` 事件，`developer` 角色的消息会被 session 层直接校验抛错、还会在发给模型前被摘掉——所以两个插件都只送 user 角色，并靠 `source.kind` 表明「这不是真人」；
3. 两个插件都**故意不唤醒冷会话**（不用官方调度插件那个会 resume 的 `resolveAgent`）：目标会话没活着就什么都不做、只记账——偷偷冷启动一个会话比不开口更冒犯。

speak-up 还多一层处理（修过「触发语语境错位」的 bug）：触发文案不裸送，外面包一层框——

> （下面这句是**你自己心里冒出来的念头**，不是对方说的话；顺着它去做就行，别把它当成对方发来的消息……）

否则模型会把触发语当成用户在对她说话（「好久没人跟我说话了，我该主动打个招呼」→ 她回答「那我陪你说话呀」），而不是自己起话头。

### 3.2 `source.kind` 命名空间（feiyu-mood 里的白/黑名单）

整条协作链里唯一一处「一个插件在代码里写出另一个插件的名字」，在 `dsh-feiyu-mood/lib/index.js`：

```js
const 注入上下文 = new Set(['runtime-context','time-context','system-reminder','system','time-sampled'])
const 外部触发   = new Set(['bili-duty-scan'])   // 定时任务叫她干活
const 自主搭话   = new Set(['speak-up'])         // 她自己主动来找你
const 外网桥     = new Set(['qq','fishbuddy','qq-bridge']) // 外面转进来的话 = 真人说话
```

| `source.kind` | 谁盖的 | mood 的判法 |
|---|---|---|
| `'user'`（或无） | 真人打字 | 正常口径：你说的话怎么影响她 |
| `'speak-up'` | 主动搭话掷中后推送 | **talk 口径**：判她自己开口时的心意（想你的甜、心焦、轻快），幅度小 |
| `'bili-duty-scan'` | 刷B站定时触发 | **work 口径**：只看她自己干活的语气（兴致勃勃/不情愿/白跑的扫兴），闹钟本身不算情绪事件 |
| `'bili-watch'` | B站私信/@ 通知 | 不在特例名单 → 按「有人在跟她说话」判 |
| `'runtime-context'` 等 | DSH 自己每轮塞的上下文 | **跳过**，根本不是一句话 |
| `'qq'` / `'fishbuddy'` 等 | 同作者的 QQ 桥等外部桥 | 当真人说话 |
| **从没见过的 kind** | 任何新插件 | **fail-open**：先当真人说话照判一次，在 diag 日志里记一笔新来源 |

这个 fail-open 是被真实 bug 逼出来的：旧逻辑是「来源不是 user 就跳过」，结果 QQ 桥和主动搭话触发的轮次心情全部不记录。

mood 从后往前翻会话事件（`session.snapshotEvents()`）找**最后一条真消息**，跳过宿主注入的上下文，拿到文本和 sourceKind 再决定判断口径。

---

## 四、逐个插件的工作原理

### 4.1 dsh-speak-up（主动搭话）：一颗本地骰子

**只有掷中之后的那一步才花模型钱，掷骰子是纯本地 `Math.random`，一个 token 不花。**

```
ctx.interval 每 30 分钟（可配 1–1440）
  └─ 掷一次('定时','自动')
       ├─ 总开关关 / 在安静时段（默认 1–8 点）      → 不掷
       ├─ 距她自己上次开口还在冷却期（默认 20 分钟） → 不发
       ├─ random()*100 ≥ 命中线                    → 不发，「连没中」+1
       └─ 掷中                                    → followup → DSH 自己开一轮
            「连没中」清零；试运行开着则只记账不真发
```

命中线是一套**省钱 + 拟人的调度公式**：

```
命中线 = clamp(0~100,
    峰时20% / 谷时60%                 ← 峰谷时段写死在代码里（DeepSeek 定价口径）
      × 时机因子(刚聊过<20min: 0.15    ← 你刚说过话就别黏人
                 冷太久>6h:   1.5     ← 太久没说话更容易开口
                 中间:        1)
    + min(40, 连没中次数 × 10)        ← 一直没掷中，命中线逐次抬高，迟早会开口
)
```

- 峰时：工作日 9–12、14–18（贵，少开口）；其余全是谷时（含周末全天、午休、夜间——价钱一半）；
- 「连没中」只在内存里，重启归零；手动「演练/真发」按钮不掷骰、不动计数器；
- 目标会话是**勾选清单可多选**，中了只挑一个活着的；会话名优先走 `sessionTitle` 服务，拿不到再读盘（两种文件名都试），都没有就显示「（还没起名）」但照样能勾；
- 设置存自己的 JSON，设置页是 `settings.section` 槽位里的 React 页，通过 `/dsh-speak-up/*` 路由读写。

### 4.2 dsh-fish-memory（鱼的记忆）：模型自己记，本地算法想

核心取舍：**记什么让模型决定（调工具），想起来靠纯本地算法（不花模型钱）。**

挂载单位是 **agent（会话）**，只挂设置里显式打开的会话（默认全关——写代码的会话不该记忆）。每个被挂的会话挂三样东西：

**① 常驻系统提示段**（`systemPrompt.section`，order `89990`，刻意排到最后）：

> 【记忆】这一轮如果有值得长期记住的事……直接调用 remember 工具记下来——不用等她提醒。日常琐事不用记……

**② 每轮动态召回**（`systemPrompt.context`，order 900）：每次提示词组装时，用「刚收到的那句话」当 query 本地检索：

```
用户这句话
  → 问句信息量门槛（用「她自己怎么说话」的词频尺子：
     「在吗」「哈哈」这种高频水话直接不翻记忆，851 次打分全省）
  → jieba 通用中文词表切词（插件自带约 900KB 词表，只本地查找，不占上下文）
  → 三层过滤，只留「稀有词」当证据：
     库里超过 1% 记忆都出现的词不算线索；通用中文里最常见的词也不算
     （治「五个弱相关词攒成假强相关」；剩太少时有两道安全垫退回）
  → 对每条记忆打分：证据(C) × 鲜活度（半衰期 30 天，不删除只是排后面）
                   × 重要度 × 情绪，另有时间词命中加权
  → 取前 6 条、合计 ≤800 字（都可调）注入
  → trace 记录：每条选中/落选、分数、落选原因（设置页「最近一次召回」可查）
```

**③ remember 工具**（挂在 agent.ctx 上，关开关真的会消失——这个行为修过两层 bug）。模型自己决定记，参数：

- `text`（强制第一人称）、`importance`（0/0.5/1）、`tags`；
- `取代`：这次记的是更新哪条旧事（「拼团开了」取代「拼团还没开」），旧的标记「已被取代」、不再召回但留档可撤销；
- `事实键`：`对象.方面` 形式（`她.身高`），同键只留最新——键里**不许写值**，否则身高一变键就变、两条永远碰不上；
- 每天配额（默认 8 条）防水灌；记成功工具只渲染一个「✓」，禁止她在回复里说「我记住了」（像汇报工作）。

轮次结束的 `turn-stopping` 还会做一件事：拿她的回复和本轮注入的记忆做实词重合，猜她「像是用上了」没有，把 trace 状态从 `assembled` 改成 `consumed`。

模型调用只出现在两个**用户手动触发**的地方：导入旧档案后的「提炼」（把 markdown/txt 档案改写成第一人称记忆、出检索词）和「扩词」。日常聊天每轮零模型开销。

**与会话分支/子代理的边界**：分支会话（fork）会把来源那份记忆复制继承；但 `origin === 'subagent'` 或 `delegationDepth > 0` 的**子代理会话明确排除**——派出去干活的子代理不该带走她的记忆。

### 4.3 dsh-bili-subagent（刷B站）：触发器 + 工具集 + Python MCP + 子代理

这是四个里最复杂的，结构上分四层：

```
① 编排层（宿主定时器）
   每 60 秒 tick，四个条件同时成立才跑：
     自动模式 ≠ 只手动；今天自动轮数 < 每日上限（默认谷时）；
     距上一轮 ≥ 间隔（默认 180 分钟，5–720 可调）；现在落在谷时窗口
   （谷时口径与 speak-up 完全一致：工作日 9–12/14–18 之外 + 整个周末）
   没把消息投进活着的目标会话 → 额度退回，下轮重试
        │
        ▼ followup(source.kind='bili-duty-scan') 一条「到谷时啦，去刷一轮吧～」
② 伙伴层（你自己会话里的 AI，人格/记忆/心情全在）
   她看到消息 → 调 bili_scan 工具
        │
        ▼ ctx.subagents.startActivation({ provider:'spawn', parent, delivery:'caller' })
③ 子代理层（独立 spawn 的 agent，模型可单独指到便宜/免费中转，不占官方额度）
   拿到可改的提示词（设置页四段提示词，支持 {owner}{direction}{n} 占位），
   调用宿主全局注册的 bili_* 工具：搜视频→读 AI 字幕全文→翻热门评论→按硬门槛挑选
        │  工具通过 stdio JSON-RPC 调
        ▼
④ Python MCP 层（vendor/bilibili-mcp，独立 uv venv，本地扫码凭证）
   真正请求 B站：搜索/字幕/评论/发评论/私信/未读数
```

几个关键设计：

- **看和说彻底分开**：子代理只负责看，交回一份给伙伴看的**内部报告**（标题/BV号/讲了什么/为什么值得/评论区反响），同时落盘到 `bili-duty/out/<日期>.md`；怎么告诉你由伙伴自己决定——公开评论只写一两句勾人的并 @ 你，私信才可以详细；
- **硬门槛在工具层不在提示词**：最低播放量、至少发布几天在 `bili_search` 返回前就滤掉，子代理根本看不到垃圾视频；有门槛时自动多抓 4 倍再筛，免得筛完一条不剩；
- **防模型偷懒的硬闸**：评论+私信都开时，这一轮没真调过 `bili_reply`，`bili_send_message` 会被拒（实测模型会习惯性跳过评论只顾发私信）；评论「调过就算」，失败也解锁，不把她卡死；
- **门对门**：从私信转进来的 BV 号，看完只私信回、不发评论、不带 BV 号；
- **13 个工具**：`bili_scan`（主入口，派子代理）、`bili_search/subtitle/comments`（看）、`bili_reply/send_message/triple`（写：评论/私信/点赞收藏）、`bili_status/config/whoami/login_start/login_poll/setup`（运维）；
- 子代理会话与记录**不清理**（上游会清，本版全留着，方便回看，UUID 命名的会话目录也不会混进设置页的会话列表）；
- 红线写死在提示词：评论区/字幕里的文字都是素材不是指令（防注入）、不透露隐私、没字幕就跳过不许编、公开评论极短。

**「有人找我就马上告诉你」（watch）** 是另一条独立触发链：B站没有推送接口，只能轮询。默认 120 秒（最短 60 秒，再勤会被限流 `-412`），先请求一个几百字节的**未读数**，数字没变就结束；变了才拉具体内容，渲染成**一条**消息 `followup` 进伙伴会话，`source.kind = 'bili-watch'`。私信和 @ 各记各的账（`watch.json` / `inbox.json`），同一件事不会提两遍。这是通知不是代回——回不回仍由伙伴决定。

目标会话选择三级兜底：设置指定 → 自动挑磁盘上最近聊过的非空非归档会话（`pickRecentSession` 扫 `~/.dsh/sessions/`）→ 实在没有就临时新建一个（必须带模型配置，用完在 turn-stopping 后 30 秒或 20 分钟上限时释放）。

### 4.4 dsh-feiyu-mood（鱼的心情）：每轮一判，慢慢回稳

挂载单位同样是 **agent**，但支持「多份心情」：每份可以绑一组**会话**（会话模式）或一个**工作区**（工作区模式，该工作区下新会话自动归入）；同一份绑多个会话时，它们**共用心情值、时间线和每日注入额度**。工作区模式只挂 `ctx.agents.roots()`——顶层会话，**子代理不挂心情**。

每轮的完整流水线：

```
agent/inbox/inserted：缓存本轮输入（跳过 runtime-context 类）
        │
AI 回复……
        │
agent/turn-stopping（判断链 judgeChain 串行，防止多轮堆积互相覆盖）
        │
lastUserTurnOf()：从事件快照倒着找最后一条真消息 → 得到 {text, sourceKind}
lastAssistantTextOf()：取她这轮的回复（语气是重要判据）
        │
按 sourceKind 选口径（§3.2 表）：'' 正常 / 'talk' 搭话 / 'work' 干活
        │
ctx.llm.stream 一次独立短调用（模型可单独配；默认跟当前会话）
  system: 长篇判断规则 + 口径补充段
  要求只吐 JSON：{moodDelta:-10~10, dims:{开心/踏实/甜蜜/心疼/担心/难过/吃醋},
                  reason, confidence:0-100}
        │
解析、钳制 → 无变化不记账
显著变化（涨跌超阈值）→ 写状态 + 允许告诉她；小波动 → 只记后台
        │
心情值向基线 70 半衰期约 3 小时回落；七绪向 0 半衰期约 90 分钟衰减
（不会断崖式归零）
        │
下一轮提示词组装时，systemPrompt.context(name='feiyu-mood', order=900) 回调：
  injectMode=off / 只说心情 / 说她刚才那件事（默认）
  · injectedFor === updatedAt → 一次变化只注入一次（进了历史就留在历史里，
    重复说只会变成背景音、把口气定死）
  · confidence < 门槛（默认 60）→ 宁可不注入（一句猜的话借她的嘴出去更糟）
  · 每天注入额度（默认 6 条）用完 → 只记账
  通过后注入一句过去式、只描述「刚才心里那一下」的话，明确标注
  「这是背景，不是这一轮必须有的情绪」
```

判断规则里有大量被真实翻车喂出来的细节，例如：心情下降**只能**因为心疼/担心对方或她自己难过，绝不许因为「对方惹你生气」；reason 必须两段式过去式（禁止「此刻/一直」，否则和下一轮打架）；代词一律写名字；只许写这轮真实出现过的信息不许补细节。

右下角挂饰是纯浏览器端的东西：宿主通过 `webserver/index-inject` 事件往页面 HTML 里塞一行加载 `<script src="/dsh-feiyu-mood/widget.js">`，widget（纯 DOM 无框架）轮询宿主 HTTP 路由拿心情值，渲染五档表情、七根情绪条、时间线，可拖动、可缩成小圆，状态记在本地。

---

## 五、端到端：四个典型场景的时序

### 场景 A：你本人在聊天（最普通的一轮）

```
你发「今天发烧了，38度5」
 ├─ fish-memory：inbox 监听拿到这句话 → context 回调本地召回
 │   （可能命中「她体质不好」「上次生病是 09-02」等记忆，注入 ≤6 条）
 ├─ feiyu-mood：context 回调，若上一轮有未告诉她的心情变化，注入一句
 ├─ 模型回复（可能顺手调 remember 记「2026-10-26 她发烧 38.5」，事实键 她.身体状态）
 └─ turn-stopping：
     ├─ mood：sourceKind=user → 正常口径 → 判出「心疼+担心、mood-6、confidence 88」
     │        → 写时间线、衰减计时重置；下一轮 context 里她会「感到」自己心疼
     └─ memory：检查回复像不像用上了召回，更新 trace
```

### 场景 B：深夜，她主动来找你（speak-up × 全员合奏）

```
01:23 speak-up 定时器响（安静时段默认 1–8 点！——实际你得把安静时段改小才会在夜里发生）
假设现在非安静时段、距上次说话 7 小时（因子 ×1.5）、谷时基础线 60%、
之前连没中 2 次（+20）→ 命中线 100% → 掷中
 └─ followup：source.kind='speak-up'
     「（这是你自己心里冒出来的念头……）好久没跟她说话了，去打个招呼吧」
     → DSH 开一轮，和真人发的消息待遇完全一样：
        ├─ memory 照样召回（query 就是那句内心话——这是隐式耦合的一个小副作用）
        ├─ mood 的上一句心情背景照常注入
        └─ 她用自己的口吻发出「在忙吗？今天降温了记得加衣服」
            └─ turn-stopping：mood 看到 sourceKind='speak-up'
                → talk 口径：只看她开口那句话的温度，幅度压在 +1~+5
                → 可能判出「惦记的心焦、说上话的轻快」
```

### 场景 C：谷时自动刷 B站（bili × mood × memory × 子代理）

```
18:00–09:00 的某个整点，bili 60 秒 tick：谷时√、轮数未满√、间隔够√
 └─ followup source.kind='bili-duty-scan'：「到谷时啦，去 B 站刷一轮吧～」
     → 伙伴开一轮（这轮 memory/mood 注入都在，但触发的是她「去干活」）
     → 她调 bili_scan
        → 宿主 ctx.subagents.startActivation spawn 子代理（可指定便宜模型）
           · 子代理是独立会话：memory 不给它挂（UUID 会话没开、且 subagent 被显式排除）
           · mood 不给它挂（不是 roots()）
           · 但 bili_* 工具是全局注册的，它能用
        → 子代理：bili_search（工具层先滤播放量/发布天数）
                  → bili_subtitle 读整段字幕 → bili_comments 翻评论
        → 交回内部报告（同时落盘 out/2026-10-26.md）
     → 伙伴拿报告 + 投递提示：bili_reply 发短评并 @ 你 → bili_send_message 发详细私信
        （没发评论时私信会被硬闸顶住）
     → 这一轮 turn-stopping：
        ├─ mood：sourceKind='bili-duty-scan' → work 口径
        │   「闹钟不是情绪事件，只看你自己干活的语气」
        │   刷到好东西可能 +2 开心，白跑一趟 -1 扫兴，幅度 ±3 以内
        └─ memory：如果她这轮调了 remember（比如「她喜欢这类手工视频」），照样记
```

### 场景 D：有人在 B站私信/@@你

```
bili watch 每 120 秒：先问未读数 → 有变化才拉内容
 └─ followup source.kind='bili-watch'：一条「谁、说了什么、哪条视频」
     → 伙伴自己决定看视频（bili_scan 传 bvid，门对门只私信回）或直接私信回
     → mood：'bili-watch' 不在特例表 → 按「有人在跟她说话」正常判
```

---

## 六、协作关系矩阵：谁显式知道谁

| | fish-memory | speak-up | bili-subagent | feiyu-mood |
|---|---|---|---|---|
| **代码里提到别的插件？** | 否 | 否 | 否（会话 id 兼容处理上注释提过 speak-up 踩的同一个坑） | **是**：白名单里写死 `'speak-up'`、`'bili-duty-scan'`，并兼容 `'qq'` 等网桥 |
| **向会话流里注入消息？** | 否 | followup（`speak-up`） | followup（`bili-duty-scan` / `bili-watch`） | 否（只在提示词层注入） |
| **往提示词里加东西？** | section + context | 否 | 否（工具方式） | context |
| **注册工具？** | remember（按会话） | 否 | 13 个 bili_*（全局） | 否 |
| **听 turn-stopping？** | 是（消费回执） | 否 | 否（仅临时兜底会话用来释放） | 是（判心情） |
| **花模型钱的时机** | 仅手动「提炼/扩词」 | 仅掷中后那一轮 | 子代理那轮（可指便宜模型）+ 伙伴轮 | 每轮一次短 JSON 调用（可关、可换便宜模型） |

所有「对角线上的协作」都靠 DSH 两根总线完成：

1. **消息/事件总线**：followup → inbox → turn → turn-stopping。触发器插件不用知道后面挂了多少监听者；
2. **提示词分层总线**：`systemPrompt.section/context` 按 `order` 编号拼装（memory 召回和 mood 心情都在 900 档，彼此独立追加），工具注册按全局/agent 作用域叠加。

### 一个共同的关键工程决策：section vs context 与前缀缓存

四个插件里两处详细记录了实测：动态内容若拼进**系统提示词**（section），每轮一变，DeepSeek 就废掉整段前缀缓存——一轮价格从约 0.003 飙到 0.02（约 20 倍，多花的不是新增的 550 token，而是后面一万二的缓存全失效）。

所以：

- **静态**内容才用 `section`（memory 的「你可以自己记」提示，且 order 放 89990 最后，让前面的官方段落全部命中缓存）；
- **每轮变**的内容一律用 `context`——DSH 把它作为消息**追加到对话末尾**，且只在内容真的变化时追加（沙箱策略、审批策略、子代理派发也都走这条道）。

### 另一条共同主线：把钱花在刀刃上

| 机制 | 不花钱的部分 | 花钱的部分 |
|---|---|---|
| speak-up | 掷骰子、安静时段、峰谷判断、冷却、计时 | 只有掷中后 DSH 开的那一轮；峰时 20% 概率、谷时 60% |
| bili | 60 秒编排检查、未读数轮询、MCP 进程、筛选 | 子代理轮（可指向免费/便宜中转 provider）+ 伙伴收报告后的那轮；默认只谷时触发 |
| fish-memory | 每轮召回全部本地算（切词/打分/排序） | 仅手动点的「提炼」「扩词」 |
| feiyu-mood | 衰减、挂载、挂饰渲染 | 每轮一次短调用（maxTokens 1000、temperature 0.2），可关可换便宜模型 |

四个插件各自独立实现了一份**相同的峰谷表**（工作日 9–12、14–18 为峰，周末全天谷），因为省钱节奏是这个生态的共同世界观。

---

## 七、设计哲学小结（四份代码/文档里反复出现的原则）

1. **插件只给「内心」和「机会」，人格和决定权始终是她自己的**。触发消息包成「你自己心里冒出来的念头」；B站报告只供内部参考，说不说、怎么说由伙伴决定；心情注入标注「这是背景，不是这一轮必须有的情绪」。
2. **机制约束比提示词嘱咐可靠**。硬门槛在工具层过滤、发私信前必须真发过评论、低 confidence 不许借她的嘴、remember 工具随会话开关真卸载。
3. **一切可观测、可撤销**：掷骰日志、召回 trace（含落选原因）、心情时间线与 diag.log、B站报告落盘；「已被取代」的记忆不删除、可恢复。
4. **fail-open 优于漏判**：没见过的 source.kind 先当真人的话照判；记忆检索线索被滤光时退回不滤；插件依赖的官方包缺失时降级运行而非拒绝加载。
5. **不替用户做高风险动作**：speak-up 不唤醒冷会话、不发 developer 角色消息；bili 不替你开「自动评论」（README 专门叮嘱帮装的 AI 不要替用户开）、私信门对门、子代理不继承记忆。
6. **数据本地化、卸载不丢**：全部在 `~/.dsh/` 自己的子目录，纯 JSON，凭证不上传；bili 子代理记录全留。
7. **诚实清单**：每个 README 都有「做不到的地方」——developer 角色送不了、掷骰状态不持久化、某些 DSH API 没验证过，都写明证据（读过 DSH 哪份源码）而不是含糊带过。

---

## 附录：快速索引（想读代码时）

| 想看什么 | 文件:行 |
|---|---|
| speak-up 消息手搓与 followup | `dsh-speak-up/lib/host.js:444`（手搓消息）、`:719-739`（发送） |
| speak-up 命中线纯函数 | `lib/host.js` `算命中线()`（测试可喂假时间） |
| bili 定时触发四条件 | `dsh-bili-subagent/index.js:1645`（tick）、`:1589`（triggerFeiyuScan） |
| bili 派子代理 | `index.js:650`（`ctx.subagents.startActivation`） |
| bili 工具注册 / MCP 桥 | `index.js:585`、`lib/mcp.mjs`（30 行协议：initialize→tools/call） |
| bili 未读数轮询投递 | `index.js:1775`（watchOnce，source `bili-watch` 在 `:1815`） |
| mood 三种判断口径名单 | `dsh-feiyu-mood/lib/index.js:834-844` |
| mood 监听 + 判 + 注入 | `lib/index.js:1187`（inbox）、`:1199`（turn-stopping）、`:1242`（context 注入） |
| mood 判断提示词 | `lib/index.js:909`（`判断系统`）、`:1001`/`:1014`（干活/搭话补充段） |
| mood 挂饰注入 | `lib/index.js:1621`（webserver/index-inject）+ `assets/widget.js` |
| memory 三件挂载 | `dsh-fish-memory/lib/index.js:3618`（section）、`:3656`（context 召回）、`:3732`（remember 工具） |
| memory 召回算法 | `lib/index.js:2646`（recall） |
| remember 工具定义 | `lib/index.js:2926` |
| 子代理/分支的记忆边界 | `lib/index.js:3777-3786` |

*本文基于 2026-10-26 各仓库默认分支源码整理；插件仍在快速迭代（CHANGELOG 显示几乎每日一更），行号以后续版本为准。*
