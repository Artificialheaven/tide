# B 站直播 AI 方案设计

> 目标：实时接收 B 站直播间弹幕，每 X 秒把多个观众的消息**聚合成一批**发给
> deepseek 插件的 AI（减少调用次数）；她的回复默认走 TTS 念出来（不发弹幕），
> 只有播放 voice 素材的窗口内台词才改走弹幕，两条声轨绝不重叠。下播后自动
> 复盘并把本场内容
> 固化进她的记忆库；支持把 B 站 uid 绑定到 QQ 号，实现同一个人在
> QQ 群与直播间的记忆互通。
>
> 状态：**M1–M5 全部落地（2026-11-04）**，仅剩面板真机端到端验收
> （扫码 / 启停 / 心跳 / 出声）。结论：无技术障碍，插件核心代码零改动，
> 新写的只有弹幕聚合循环、出站 FakeBot 路由、一个设置页和 TTS 客户端。

---

## 0. 为什么可行（现有代码的可复用点）

已核实的关键事实：

1. **所有出站消息只汇流到一个方法** `bot.send_message(message=, target_id=,
   message_type=, extra=)`：
   - 终态分条回复走 `Core/reply.py::send_in_chunks`；
   - `qq.text` 工具走 `builtin_tools.dispatch('qq_send_text')`；
   - `qq.voice` / `qq.image` 工具发出的是 `[CQ:record,file=...]` 这样的 CQ 码，
     同样调 `send_message`。

   → 注入一个 FakeBot 即可在**不改插件代码**的前提下接管全部「她说的话」，
   按文本 / CQ 码分流到弹幕发送与（未来的）TTS。
2. **入站只需要一个 dataclass**：`Core/models.py::ParsedMessage`
   （`text/sender_id/sender_name/at_me/chat_type/allow_silent/...`）。
   插件自己的定时苏醒 / 外部触发（`Engine.handle_wake` /
   `handle_trigger`）就是合成 ParsedMessage 后直接调
   `engine._run_turn(gid, cfg, parsed, forced=True, ...)`，属于官方既有用法。
   `forced=True` 跳过硬粗筛和判官，适合直播场景，省 token。
3. **整套大脑白嫖**：config.json 里已配置的服务商/API key、`LLMClient`、
   工具循环、人设、上下文窗口 + 滚动摘要、per-user 长期记忆、约定待办、
   自我记忆、`[nospeak]` / `[静默]`、分条切句净化——全部以 gid（字符串）
   为键。直播间用独立 gid（`bili:<房间号>`）在 `groups` 表建行即可，
   人设/历史/记忆与 QQ 群互不干扰。
4. **记忆抽取管线一次产出三类内容**（`MemoryManager._maybe_extract`）：
   群友记忆（`memory_entries`）+ 约定待办（TaskManager）+
   她自己的第一人称记忆（`SelfMemoryManager`）。下播后调一次
   `force_extract(gid)` 即可全部固化。
5. **`memory_entries` 按 user_id 全局键控**，`source_group_id` 只记出处。
   B 站 uid 绑定到 QQ 后，记忆天然跨场景互通，不需要同步逻辑。
6. **插件可脱离框架裸构造**：`main(None, fake_bot)` 即可（基类
   `Plugin.__init__` 不校验 websocket）。每轮对 bot 的其他调用
   （`call_api('get_version_info')`、群成员信息）全部包了 try/except 并
   优雅降级。脚本形态参照仓库根已有的独立脚本
   `scripts/meme_import_bili.py`（自己插 sys.path、不依赖框架、
   自带 B 站扫码登录实现）。

---

## 1. 进程拓扑

```
┌─ bot 进程（main.py + deepseek 插件）──────────────────────┐
│  新增 /deepseek/live 设置页（Flask 蓝图内加路由+模板）       │
│  只读写 DB：配置/绑定/场次/启停标志/心跳                     │
└───────────────┬───────────────────────────▲──────────────┘
                │ 共享 plugins/deepseek/db/deepseek.db (SQLite WAL)
                ▼                           │
┌─ scripts/bili_live_ai.py（独立 asyncio 进程）──────────────────┐
│  bilibili-api-python：弹幕/礼物/SC 事件 + 发弹幕 + 扫码凭据      │
│  import 插件 Core：裸构造 main(None, FakeBot)，直接调           │
│  Engine._run_turn                                              │
│  TTS/播放：本期只留两个空钩子（打日志），以后只填这两个函数       │
└────────────────────────────────────────────────────────────┘
                │ db/bili_credential.json（与 meme 导入脚本共用一份）
```

约定：

- 插件核心代码零改动：只加 `Core/web.py` 路由、一个新模板、
  `Core/storage.py` 一轮 `PRAGMA user_version` 迁移。
- 两进程通过 **WAL SQLite** 通信。storage 的连接工厂是「每次调用开独立
  连接」，WAL 下多进程读写安全。直播 gid 只有脚本进程在跑对话，面板只写
  配置不写该 gid 的消息，无需跨进程锁。
- 脚本**不调 `on_start()`**（否则会在另一个进程起 trigger/live 两个 HTTP
  服务，造成端口冲突）；构造完直接用，退出时 `await llm.aclose()`。
- 独立 DB 不可行（各 manager 在构造时绑定 `db_path`），所以共用插件
  db、用独立 gid 隔离会话。

---

## 2. 弹幕接入（用 bilibili-api-python，不手搓协议）

- **接收**：库的 `LiveDanmaku` 事件监听（自带 protobuf/zlib/重连），订阅：
  - `DANMU_MSG`：普通弹幕（uid / 昵称 / 粉丝牌 / 文本）；
  - `SEND_GIFT` / `GUARD_BUY`：礼物 / 上舰（合并节流，小心心连击不能
    灌爆上下文）；
  - `SUPER_CHAT_MESSAGE`：SC，走高优先级通道；
  - `LIVE` / `PREPARING` / `ROOM_CHANGE`：开播 / 下播 / 分区标题变更。
- **发送**：库的 `LiveRoom` 发弹幕接口（以安装版本为准），或直接带
  credential 的 `bili_jct` POST `api.live.bilibili.com/msg/send`。
- **登录**：直接搬 `scripts/meme_import_bili.py` 里现成的扫码登录与凭据
  持久化（`db/bili_credential.json`，0600、自动刷新、已 gitignore）。
  扫一次码，梗库脚本与直播脚本共用。
- **自环过滤**：忽略当前登录 uid 自己的弹幕，防自己触发自己。

---

## 3. 聚合 → AI：双通道

每场直播固定会话 id：**gid = `bili:<房间号>`**，在 `groups` 表建行，
人设、per-group 设置、消息历史、摘要、记忆全部自动归属。

| 通道 | 触发条件 | 处理 |
|---|---|---|
| 普通通道 | 每 X 秒，或攒满 N 条（先到先触发） | 弹幕进有界队列；到点拼成**一条** user 消息：`昵称：内容` 多行，上限 N 条 / 字符截断，多余的留到下一轮 |
| 优先通道 | SC / 达阈值礼物 / 开播下播事件 | 立即冲刷普通队列，事件单独插一轮 |

每轮做法与插件自己的 `handle_trigger` 完全同构：

1. 事件类（礼物/SC/上下播）按 `handle_wake` 惯例落库：`sender_id=''`、
   `sender_name='系统'`，文本形如 `[直播事件] 阿凯投喂 SC「……」50 元`；
2. 普通批次先 `storage.upsert_user(...)` +
   `storage.insert_message(...'user'...)`（sender_id 见第 5 节身份映射）；
3. 构造
   `ParsedMessage(text=批次文本, sender_id=批次首个发言人, at_me=True,
   chat_type='group', allow_silent=True, message_id='')`；
4. 脚本进程内一把 `asyncio.Lock`（引擎要求同 gid 串行），调
   `engine._run_turn(gid, cfg, parsed, forced=True, ...)`；
5. `allow_silent=True` + 人设里约定「弹幕接不上话时只回 `[静默]`」，
   引擎原生支持静默轮（不发弹幕、不抽记忆），避免每 X 秒强制刷屏。

---

## 4. 出站路由（FakeBot）

脚本注入一个假 bot，只实现引擎真正会碰的方法：

- `async send_message(message, target_id, message_type, extra=None)`：
  **唯一出站口**。
  - 纯文本 → ① 进 `speak(text)` 钩子（本期 = 日志占位）
    ② 切条成 ≤20 字发 B 站弹幕；
  - `[CQ:record,file=...]`（模型若调了 qq.voice）→ 进 `play_file(path)`
    钩子（本期 = 日志占位）。
  - **以后接 TTS 时只填 `speak()` / `play_file()` 两个 async 函数**
    （HTTP 调 TTS + 串行播放队列），其他一行不用动。
- `async call_api(...)`：直接抛异常 → `passive_turn.detect_transport`
  60 秒缓存降级为 napcat、群成员信息块返回空，都是引擎现成容错路径。
- **工具裁剪（最终态）**：`group_tool_state` 白名单只保留 10 个内置工具
  `qq.text`、`qq.voice`、`time.now`、`memory.recall/remember`、
  `memory.recall_self/remember_self`、`task.add/close/list`，其余内置工具
  （QQ 专属的 image/at/file/member、qzone、request、watcher 等）全部关闭；
  **MCP 整体置空**（远程桌面操控，依赖 on_start 生命周期且属风险操作）；
  **自定义工具保留**（2026-11-04 起）：全局 `custom__*` 工具自动对直播间
  生效——它们在独立子进程执行、只收文本 ctx、拿不到 bot，返回值仅喂给模型。
  限制：直播间内无法发布/管理工具（管理类内置工具不在白名单）；标了「仅主人」
  的工具观众恒为非主人会被拦截；需要写群工作区文件的工具会因
  `workspace/bili:<room>` 路径在 Windows 含冒号而失败，纯联网/计算类正常。
  启动日志会列出本场生效的自定义工具。
- B 站弹幕两个硬限制在发送层处理：
  - 20 字硬顶：直接给该 gid 设 `split_max_len=20`，复用切句器；
  - 短时间重复内容被吞：发送层做轻量去重变换，且绝不改正文入库。
- **提示词口径**：系统提示词里写死了「你在一个 QQ 群里」。不动插件的
  全局提示词（会污染 QQ 群）；脚本进程内对 `Engine._system_prompt`
  做一个仅本进程生效的小包装，替换场景行为直播版（直播间设定、观众弹幕
  格式、`[静默]` 约定、口播风格——句子短、适合念出来）。

---

## 5. B 站 uid ↔ QQ 身份打通（重点）

现有机制：`memory_entries` 按 user_id 全局键控；记忆注入按本轮
`participants=[sender_id, ...]` 召回；记忆抽取时用 `group_roster` 把
LLM 输出的人名解析回 user_id。

### 5.1 直播时的发言者 id 规则

- **已绑定**的 B 站 uid → 直接用 **QQ 号作为 sender_id**，并
  `upsert_user(QQ号, B站昵称, gid)`（昵称并入该 QQ 的 names 列表）。
  - 她在直播间看到这个人的弹幕时，召回的是这个人在 **QQ 群里的全部
    记忆**；直播中新记下的事实也直接写进这个 QQ 的记忆，他回 QQ 群时
    她依然记得。模型主动调 `memory.remember` 时，名字经 roster 也解析
    到 QQ。
- **未绑定**的观众 → 用临时 id **`bili:<uid>`**，名字、发言、记忆照常
  积累，不丢任何东西，将来绑定即回填。

### 5.2 设置页里的绑定 / 回填

- 新表 `live_identity`（见第 8 节），面板列出最近观众
  （昵称 / 发言数 / 是否已绑），支持按 QQ 昵称搜 `users` 表辅助选择。
- 绑定时在**一个事务**里把历史数据整体并入 QQ：
  - `messages.sender_id: bili:<uid> → QQ`；
  - `memory_entries.user_id: bili:<uid> → QQ`（并入后插件自带的三档
    相似度合并会自然去重）；
  - users 行的 names 合并；
  - 脚本进程的记忆缓存在内存，绑定建议在该房间未直播时操作
    （面板按心跳状态禁用按钮）。
- **权限注意**：`ToolContext.is_owner` 按 sender_id 判主人。绑定到主人
  QQ 的 B 账号在直播间拥有主人工具权限，绑定时给确认提示；别把陌生 SC
  大哥误绑成主人。

---

## 6. 下播总结与记忆固化

触发时机：收到 `PREPARING`（主播下播）/ 面板停止 / Ctrl+C，三选一。
收尾顺序：

1. **冲刷残余批次**，可选让她说一句告别语（发不发由 `[静默]` 决定）；
2. **结构化复盘**：按本场时间窗查 `messages`（弹幕 + 她的回复 +
   系统事件都在里面），分块调 `LLMClient`（复用摘要同款便宜参数）生成：
   热点话题、有趣弹幕、礼物 / SC 鸣谢名单、没答上的问题、她做出的承诺、
   本场心情。写入 `live_sessions.summary`，设置页可看历次复盘；
3. **记忆固化——直接复用现成管线**：
   `await plugin.memory.force_extract(gid, cfg, llm)`，它会：
   - 给每个已映射 QQ 的观众产出 / 合并群友记忆（进 QQ 记忆库）；
   - 产出约定待办（TaskManager）；
   - C 段产出经 `SelfMemoryManager.apply_extracted` 写入她自己的
     第一人称记忆（全局、跨群共享）——之后在 QQ 群她会自然记得
     「我前几天在直播间……」；
   - 未绑定观众的记忆进 `bili:<uid>`，将来绑定即回填合并；
4. **可选加强**：把复盘结论用现成的 `self_memory.merge_self(...)`
   固化 1~2 条高重要度记忆（比抽取更可控），并在 summary 里点名下次
   直播该回访的人/事；
5. 上下文滚动摘要（`summaries` 表）随窗口机制自然存在，不用单独处理；
   场次状态置 `ended`，写统计（弹幕数 / SC / 她发了多少条 / 耗时）。

注：直播进行中其实也在持续记忆（每个非静默轮结束引擎会调
`schedule_extraction`），下播的 force_extract 只是兜底最后几轮。

---

## 7. 新增设置页（挂现有蓝图）

`Core/web.py` 加路由 `/live` + `templates/live.html`（自动挂在
`/deepseek/live/`，蓝图自带登录鉴权），API 一组 `/api/live/*`：

1. **房间与节奏**：房间号、启用开关、聚合间隔 X、批量上限 N、
   SC/礼物是否立即触发、本场模型路由（复用现有模型选择口径）；
2. **人设 prompt**：直接编辑该 gid 的 `groups.persona`（群设置页那套
   存储，不造新字段），旁边给直播场景模板；
3. **凭据状态**：显示当前登录账号与 cookie 有效期；「扫码登录」按钮
   ——后端调 `bilibili_api.login` 生成二维码返回图片/key，前端轮询，
   成功落 `bili_credential.json`；脚本监听文件 mtime 热加载，不用重启；
4. **身份绑定**：观众列表、搜索 QQ、绑定/解绑（带历史回填确认文案）；
5. **场次页**：历次直播的时间 / 统计 / 复盘全文，「立即重新总结」按钮；
6. **运行状态与启停**：脚本每 10s 写心跳行（房间号 / 队列长度 /
   最近一条弹幕）。启动方式复用 `watchers.py` 那套**分离式 Popen +
   启动后 reconcile 对账**的成熟范式（on_stop 刻意不杀子进程，监测
   不能中断）；停止用 DB 标志位，脚本轮询到后自己跑完收尾流程再退出。

---

## 8. 数据库迁移（必须走 PRAGMA user_version）

下一个 user_version 新增三张表：

- `live_rooms(room_id PK, enabled, batch_seconds, batch_max,
  immediate_gift_sc, model_route, ..., updated_at)`
  —— 节奏配置；人设不入此表，复用 `groups.persona`；
- `live_identity(bili_uid PK, qq, bili_name, first_seen, last_seen,
  msg_count, linked_at)`；
- `live_sessions(id PK, room_id, started_at, ended_at, status,
  heartbeat_at, stats_json, summary)`。

不做独立事件表：礼物 / SC / 上下播统一进 `messages`（系统消息惯例），
下播总结只查一张表。

---

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| 模型调 QQ 专属工具报错刷屏 | 该 gid 工具开关全关，只留说话/记忆/时间；FakeBot 容错 |
| 礼物连击灌爆上下文 | 礼物事件节流合并（同人同礼物 30s 聚合一行），只有 SC / 大额礼物走立即通道 |
| 弹幕 20 字限长 / 重复被吞 | gid 级 `split_max_len=20`；发送层轻量去重变换 |
| 提示词是 QQ 口径 | 仅脚本进程包装 `_system_prompt`，不碰全局提示词与 QQ 群 |
| 两进程并发 | 直播 gid 仅脚本跑对话；绑定操作要求房间未在播；SQLite WAL + 每调用独立连接 |
| 插件热重载 | 脚本是独立进程，不受影响；下次启动自然加载新代码 |
| cookie 安全 | 落 gitignored 的 db 目录、0600，与梗库脚本同款；面板走现有登录鉴权 |
| TTS 以后才接 | 路由层现在就定好 `speak()/play_file()` 两个 async 钩子，默认打日志，未来零改动接入 |

---

## 10. 实施分期

- **M1 骨架**：迁移 + 脚本裸构造插件 + FakeBot 日志路由 + 手动指定房间，
  X 秒聚合跑通「弹幕 → AI → 控制台打印回复」（不发弹幕、不登录也能收）；
  - ✅ **已完成（2026-11-04，真实直播间实测通过）**。实现与计划的偏差：
    ① 弹幕接收改用 bilibili-api-python 的 `LiveDanmaku`（匿名 WebSocket
    遇到 -352 风控，库内置 buvid 激活/wbi 反爬；另需 aiohttp——httpx
    不支持 WebSocket）；② 库事件载荷在 `ev['data']` 下再嵌套一层
    （弹幕 `ev['data']['info']`，礼物/SC `ev['data']['data']`）；
    ③ 实测存在 uid=0 的匿名游客弹幕，统一归到 `bili:0`。
  - 产出：`storage.py` v17 迁移（三张 live_ 表）、
    `scripts/bili_live_ai.py`（`--room/--interval/--max/--persona`）。
- **M2 双向打通**：扫码凭据 + 发弹幕 + 直播版系统提示词 + 工具裁剪；
  - ✅ **已完成（2026-11-04）**。实现要点：
    ① 登录复用 `scripts/meme_import_bili.py` 的网页扫码流程与凭据文件
    （同一 `db/bili_credential.json`），`CredHolder` 支持文件 mtime
    热加载；接收/发送都走登录凭据，登录 uid 的弹幕自动过滤（自环）；
    ② `DanmakuSender` 串行队列：`Core.reply.split_reply` 切 ≤19 字
    （留 1 字去重后缀）、≥1.1s 间隔、60s 内重复内容追加 ~/～/。 等
    轻量后缀（只影响弹幕显示，正文入库不变）；`--no-send`/`--anonymous`
    可降级回收发；③ 进程内包装 `Engine._system_prompt`（QQ 场景句与
    QQ 号身份段替换为 B 站口径，追加直播规则块），不污染全局提示词；
    ④ 直播 gid 群工具开关只留 `qq.text/time.now/memory.*/task.*`
    九把，插件实例 `mcp/custom` 属性直接置空；gid 强制
    `split_max_len=20` 等弹幕形态设置；
    ⑤ 另应要求在设置里加了 `cheap_thinking_on` 开关（默认关，判官/
    摘要/抽取/心情/风格五处便宜调用生效；llm 的参数错误兜底现在也认
    智谱 1210 中文报错）。
- **M3 面板** ✅ **已完成（2026-11-04）**：`/deepseek/live` 页面（深色风格
  与其他管理页一致）：① 账号二维码登录（`Core/bili_auth.py`，服务端
  渲染 QR PNG，sid 240s TTL；凭据存 `db/bili_credential.json` 0600，
  脚本 mtime 轮询热加载）；② 房间卡片：添加房间（短号→真实号在线解析）、
  聚合节奏 / SC 即时 / 礼物阈值 / 本场模型路由 / 本间人设（写 `groups.persona`
  引擎每轮现取天然热生效）；③ 心跳统计原地刷新（pid/心跳/弹幕/AI 轮/
  已发/队列/最近弹幕），4s 轮询用 structureSig 保输入焦点；④ 启停：
  `Core/bili_live.py::BiliLiveManager` 按 `watchers.py` 范式 Popen
  分离进程（DETACHED|NEW_GROUP|NO_WINDOW，stdout 落 `db/live_logs/`），
  停止走 DB 标志位软停止，不听话由面板 kill_tree + 置 stopped；
  ⑤ 脚本侧新增 `--managed --session-id`：10s 心跳写 stats、5s 轮询
  live_rooms 热生效（间隔/上限/SC/礼物/模型路由）、enabled=0 触发下播
  收尾、露面观众写 live_identity。实现中踩到的坑：`upsert_live_room`
  原本写成裸 INSERT，更新已存在房间会撞唯一约束，已改 INSERT OR IGNORE。
- **M4 身份与记忆** ✅ **已完成（2026-11-04）**：① 页面底部「观众身份
  绑定」表（`GET/POST/DELETE /api/live/identities/*`），绑定为**单事务**
  回填：`messages.sender_id` / `memory_entries.user_id` /
  `commitments.owner_id` 从 `bili:<uid>` 改挂 QQ，users 目录合并昵称与
  时间；同一 QQ 只能绑一个 B 站号（新绑定时旧绑定自动让位）；解绑只清
  关系不回移；绑主人 QQ 有二次确认（后端返回 is_owner）；② SC/上舰/达阈
  值礼物进优先通道立即触发一轮（系统提示行让她当场感谢）；③ 软停止后
  `post_stream_wrapup`：取本场时间窗内 user/assistant 消息，走
  summary_model 写 300–500 字复盘入 `live_sessions.summary`，再
  `memory.force_extract(gid,cfg,llm)` 兜底最后几轮的群友记忆/约定/自我
  记忆；④「历史场次」区：状态/时长/统计/复盘全文 + 「重新复盘」
  （`BiliLiveManager.resummarize`，同样走便宜模型）。与计划的偏差：
  礼物连击节流仍按单条事件入队（由聚合窗口自然合批），没做 30s 去重。
- **M5（TTS）** ✅ **已完成（2026-11-04）**：新增 `scripts/bili_tts.py`
  （火山引擎单向流式 WSS，详见 `docs/volc_ttc.md`）与集成：
  ① 配置读根 `config.json` 的 `tts_apikey/tts_model/tts_speaker`
  （`tts_model` 即 X-Api-Resource-Id，复刻音色 `seed-icl-2.0`；env
  `TTS_API_KEY/TTS_MODEL/TTS_SPEAKER` 可覆盖）；② 首块 PCM 到达即开口
  （实测首包 0.7s，14 字 2.38s 音频），sounddevice RawOutputStream
  回调消费，欠载补零，队列有界（超 60s 丢旧块防堆积）；③ **最终三路路由**
  （单音频 worker 串行，两条声轨绝不重叠）：
  - 她的终态回复文本与 `qq.text` 工具输出**同一路**——只进 TTS 朗读队列，
    **绝不作为弹幕发出**；
  - `qq.voice` 的 CQ 录音进**独立素材队列**：ffmpeg 解码成 24k PCM 后
    本地播放（http(s) URL / `base64://` / 本地路径均可；mp3/wav/m4a/ogg/amr
    靠 ffmpeg，silk 不支持）；
  - 素材播放的**整个窗口**（解码前置位，到真实播完；进度按 fed/played
    字节水位线精确等待，欠载补零不计进度，否则窗口会被前段残音提前关闭）：
    窗口内（含 URL 解码下载期间）到达的台词入队即打 divert 标，播放过程中
    实时经 `DanmakuSender.speak` **改成弹幕发出、不 TTS**；素材开始前已
    排队的台词同样冲刷成弹幕；
  - TTS 未配置/设备打开失败时所有台词自动回退弹幕，voice 素材只 log 丢弃；
    退出时先冲弹幕、再等播完音频才去复盘；`--no-tts` 强制哑火；
  ④ 新增依赖：`pip install sounddevice`（播放）、
  `pip install bilibili-api-python aiohttp`（直播），另需系统 PATH 中有
  **ffmpeg**（素材解码，实测位于 `C:\Windows\system32\ffmpeg`）；
  websockets/qrcode 已在环境内。实测协议要点（文档
  与实际不符处）：audio-only 帧类型是 **0xB**（非 2）、full JSON 是
  **0x9**（非 4）、事件无 EventType 字段而按 body 的键区分（空/text/
  words/usage）；**必须带 `X-Control-Require-Usage-Tokens-Return: *`
  否则永远收不到 SessionFinished**；seed-icl-2.0 的 model 只能由请求头
  选，body 带任何 model 字段都报 InvalidModel；音频帧头里的 payload_size
  只覆盖首个分片，真正长度以内层 TLV 的 body 长度为准（否则只能拿到
  45ms 残片）。

---

## 附：关键代码位置

| 用途 | 位置 |
|---|---|
| 插件主类 / 装配 | `plugins/deepseek/deepseek.py` |
| 对话一轮的入口 | `Core/engine.py::Engine._run_turn`（合成轮参考 `handle_wake` / `handle_trigger`） |
| 入站消息结构 | `Core/models.py::ParsedMessage` |
| 出站唯一口 | `bot.send_message`（终态：`Core/reply.py::send_in_chunks`；工具：`Core/builtin_tools.py::dispatch`） |
| 工具上下文 | `Core/builtin_tools.py::ToolContext` |
| 记忆抽取 / 强制抽取 | `Core/memory.py::_maybe_extract / force_extract` |
| 她的第一人称记忆 | `Core/self_memory.py` |
| 存储 / 迁移 | `Core/storage.py`（`PRAGMA user_version`） |
| 设置页蓝图 | `Core/web.py::build_blueprint`（直播页路由段 `# B 站直播 AI`） |
| 直播页模板 | `plugins/deepseek/templates/bili_live.html` |
| 子进程管理 / 重新复盘 | `Core/bili_live.py::BiliLiveManager` |
| B 站扫码凭据（面板用） | `Core/bili_auth.py`（脚本终端扫码仍在 `scripts/meme_import_bili.py`） |
| 直播脚本（聚合/托管/复盘集成） | `scripts/bili_live_ai.py` |
| 火山流式 TTS + 本地播放 | `scripts/bili_tts.py`（协议文档 `docs/volc_ttc.md`） |
| 独立脚本 + B 站扫码登录范本 | `scripts/meme_import_bili.py` |
| 分离子进程 + 对账范式 | `Core/watchers.py` |
