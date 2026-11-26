# 内存安全与性能审查（2026-11-26）

> 范围：`main.py`、`core/`、`utils/`、`ui/`、`plugins/deepseek/`（仓库当前只有 deepseek 一个插件）。
> 方式：3 路只读审计（内存/资源泄漏、性能、并发与取消）＋ 本机实测（`EXPLAIN QUERY PLAN`、计时脚本、`grep` 调用链核对）。
> 状态：**仅审查，未改任何代码**。基线 `pytest tests/ -q` = 133 passed。

## 0. 摘要

没有发现「立即 OOM」式的崩溃点，但存在 6 个 P0 级问题，正好对应三类体感：

| 体感 | 根因 | 量级（本机实测） |
|---|---|---|
| 多群并发时莫名卡顿、心跳超时、全 bot 假死 | 全部 SQLite 在事件循环线程同步执行；`connect()` 每次重设 `journal_mode`；`timeout=10` 写锁忙等 | 单次 `connect()` **0.93 ms**（纯浪费 **0.89 ms**，186 个 DAL 调用点）；触发轮 15–25 次 DAL ⇒ **每轮 15–25 ms 事件循环停摆**；写锁竞争时最长冻结 **10 s** |
| 仪表盘越用越慢 | `usage_log` 无 `created_at` 索引，`overview_stats` 每次请求 15 次全表扫；`/api/groups` 是 N+1 | 5 万行 **109 ms/请求**，随行数线性增长；100 群 `/api/groups` ≈ **330 ms** |
| API 账单比预期高 | 每轮固定 mood 判定 + judge/记忆/风格旁路调用 | 稳态调用次数 **×1.4–1.6** |
| 长期运行内存缓慢上涨 | 3 个只增不删的缓存（昵称热名、成员信息、记忆 BM25）；SSE 订阅/线程泄漏 | 见 P0-5、P1-2、P1-3 |
| 越热重载越乱 | `_call_stop` 超时后吞异常继续重新 import | 旧 on_stop 未结束、新实例已启动，双实例同库 |

## 1. 方法与证据

- 计时：`/tmp` 下独立脚本，用 `storage.migrate()` 建真实库，插入 20k `messages` / 20k `raw_messages` / 50k `usage_log` 后测 `EXPLAIN QUERY PLAN` 与耗时。
- 关键实测值（复现方法见附录 A）：
  - `storage.connect()` + `close()` = **0.931 ms**；裸 `sqlite3.connect()` = **0.039 ms**；常驻连接 `execute` = **0.002 ms**
  - `messages_since()`（20k 行，`LIMIT 2000`）= **13.0 ms**；`trim_raw` = **27.5 ms**；`overview_stats`（50k 行）= **109.4 ms**
- 每条结论都带 `文件:行号`；只有二手推断、未跑通的会标「未确认」。

## 2. P0（会造成卡顿 / 冻结 / 双实例）

### P0-1 SQLite 全部同步跑在事件循环线程，且 `connect()` 每次重设 WAL —— 每轮 15–25 ms 停摆，写锁竞争可冻 10 s

- 证据：`plugins/deepseek/Core/storage.py:16-21`

  ```python
  def connect(db_path: str) -> sqlite3.Connection:
      conn = sqlite3.connect(db_path, timeout=10)
      conn.row_factory = sqlite3.Row
      conn.execute('PRAGMA journal_mode=WAL')     # ← 0.89 ms/次，纯浪费
      conn.execute('PRAGMA foreign_keys=ON')
  ```
  文件头注释写明「全部 DAL 短开短关连接，不缓存 Connection」，`conn = connect(...)` 共 **186 处**。
- 在 loop 线程上直接调用的热路径：`engine.py:160`（`upsert_user`）、`engine.py:185`（用户消息落库）、`engine.py:1703/1710`（`get_summary` / `messages_since`）、`engine.py:1136/1140`（`add_usage`）、`engine.py:1186`（interim 落库）、`engine.py:1292-1298`（工具结果逐条落库）、`deepseek.py:285`（每条群消息 `insert_raw_message`）、`deepseek.py:425`（**持群锁时** `get_group`）、`tasks.py:112`（`open_block`，TaskManager 里唯一没走 `to_thread` 的 DAL）、`qzone_watch.py:194-203`。
- 影响：① 每条语句额外 ~0.9 ms，触发轮 15–25 次 ⇒ 每轮 15–25 ms 事件循环停摆，N 个群同时触发就是 N 倍；② 同步 SQLite 的 `timeout=10` 意味着与 `to_thread` 里的写者（记忆抽取 `memory.py:220/306`、`custom_tools.py:211`、scheduler）抢写锁时，**整个 bot 最长停 10 秒**（所有群、WS 收包、心跳一起停）。
- 修法（按性价比）：
  1. 删掉 `connect()` 里的 `PRAGMA journal_mode=WAL`（WAL 是库文件持久属性，`migrate()` 里设过即可），一行改动，全局省 0.89 ms/次；
  2. `timeout` 降到 0.2–0.5 s 并配合重试，避免一次写锁竞争冻 10 s；
  3. 读路径包 `asyncio.to_thread`，写路径改「一轮一次事务批量写」；
  4. 中长期：单写线程 + 队列（或 aiosqlite）。

### P0-2 入站事件处理器无背压、无取消：长跑持群锁时任务无上限堆积，重连后旧任务继续跑旧实例

- 证据：`core/events.py:16` `plugin_tasks = set()`；`:116-120`

  ```python
  task = asyncio.create_task(_safe_execute_handler(callback, msg_event, plugin_name))
  plugin_tasks.add(task); task.add_done_callback(plugin_tasks.discard)
  ```
  同构于 `:152-156`（notice）、`:180-184`（request）、`:207-211`（meta）。
- 阻塞点：`plugins/deepseek/deepseek.py:423-427` 每条消息 `async with self.group_lock(gid)`，而长跑模式在 `loop_run.py:208-210` 持同一把群锁数小时；期间每个入站消息都变成一个卡在锁上的 task（各自持有完整 `MessageEvent` 与 `raw_message` 原文）。
- 重连不闭环：`main.py:78-96` 的 `_teardown_connection` 只 `ashutdown_all_plugins()` + 清 `globals.bot_instance`，**从不 cancel `plugin_tasks`**；旧 task 恢复后继续用旧 bot/旧 db 写库，而新连接已建新插件实例 → 同群两条回合并发写 SQLite 与上下文。
- 修法：入口做「每群单飞 + 合并」（已有 `interrupts`/引导队列，应在建 task 前判断，而不是先占 task 再阻塞锁）；`_teardown_connection` 里 `for t in list(plugin_tasks): t.cancel()` 后 `gather` 有界等待；handler 加超时。

### P0-3 热重载不闭环：`_call_stop` 超时吞异常继续重新 import → 双实例并存

- 证据：`utils/plugin_loader.py:481-488`

  ```python
  future = asyncio.run_coroutine_threadsafe(method(), loop)
  future.result(timeout=timeout)          # 超时不 cancel future
  ...
  except BaseException:                   # TimeoutError / CancelledError 全吞，只打印
  ```
  `:922-925` 在 loop 线程内 `asyncio.create_task(method())` 后立即返回；`plugins/deepseek/deepseek.py:215-229` 的 on_stop 各段预算合计 ≈ 28 s，**大于 `_call_stop` 默认 10 s**。
- 时序：Web 面板/GUI 点「重载」→ `unload_plugin` → `_call_stop` 等 10 s 超时 → 吞异常 → 立刻 `_clear_plugin_modules` + 重新 import → 旧 on_stop 协程仍在跑（持有旧 llm/mcp/loop/HTTP 服务），新实例 `on_start` 又起一套 → 两套实例对同一库、同一群各自的 `group_lock` 互不知情。
- 修法：on_stop 各段自带超时且整体 < 卸载预算；超时则**拒绝/回滚重载**并报错；loop 线程内不要走「create_task 后立即返回」分支；lifecycle task 纳入 `_bg_stops` 并在卸载时 cancel。

### P0-4 仪表盘：`usage_log` 无 `created_at` 索引，`overview_stats` 每次 15 次全表扫；`/api/groups` 是 N+1

- 证据：`storage.py:2378-2410`

  ```python
  agg = conn.execute("... FROM usage_log WHERE created_at>=?", (today,))...
  for i in range(days - 1, -1, -1):
      r = conn.execute("... FROM usage_log WHERE created_at>=? AND created_at<?", ...)
  ```
  表上只有 `idx_usage_group(group_id, id)` 与 `idx_usage_group_time(group_id, created_at)`（`created_at` 非前导列），`EXPLAIN` 结果为 `SCAN usage_log`（覆盖索引全扫）。入口 `web.py:618`。
- 实测：5 万行 **109 ms/请求**，行数线性增长；100 万行时约 1–2 s。
- 另有 `web.py:572-574` 的 N+1：`for g in groups: storage.usage_summary(db(), g['group_id'], limit_rows=200)`，100 群 ≈ 101 次连接 + 100 次聚合 ≈ **330 ms**。
- 修法：`_DDL` 加 `CREATE INDEX idx_usage_created ON usage_log(created_at)`（写迁移）；`overview_stats` 改成一条 `GROUP BY created_at/86400`；`/api/groups` 改成一条 `WHERE group_id IN (...) GROUP BY group_id`。

### P0-5 SSE 处理线程无 socket 超时、`subscribe()` 在 `try` 之外 → 线程与订阅泄漏，并钉住整个旧插件实例

- 证据：`plugins/deepseek/Core/live.py:307-317`

  ```python
  sub = mgr.bus.subscribe(groups=groups, kinds=kinds, include_prompt=include_prompt)
  self.send_response(200); self.send_header(...); self.end_headers()
  try:                                    # ← try 从这里才开始
  ```
  `live.py:344-351` 写事件只有 `except (BrokenPipeError, ConnectionResetError, OSError)`，**未设 `self.connection.settimeout()`**；`live.py:375-378` 的 `stop()` 只 `shutdown()+server_close()+thread.join(3)`，不等 handler 线程（daemon）。
- 后果：① header 阶段客户端断开 → `finally: unsubscribe` 不执行，`Subscription` 永久留在 `LiveBus._subs`，队列上限 200 条 × 单事件截断后最大约 21k 字符 ⇒ **单订阅最多约 4–12 MB**，且此后每次 `publish` 都白遍历它；② 黑洞/极慢客户端让 `wfile.write` 在 TCP 重传上阻塞（无 SNDTIMEO，最长可十几分钟），handler 线程闭包持有 `LiveManager → self.p`（**整个插件实例**），重连后旧实例无法回收。
- 修法：`subscribe` 与 header 写入都放进 `try`；`self.connection.settimeout(30)` 并在写异常/超时即退出；`LiveManager.start()` 加重入守卫（`if self._server is not None: return`，`live.py:56-73` 目前二次调用遇 OSError 会把 `_server` 置 None 而旧 server 仍在跑）。

### P0-6 每轮固定多打一次 mood LLM，另有 judge/记忆/风格三路旁路

- 证据：`engine.py:1046-1060`（每轮 `mood_mgr.schedule_judge(...)`，`config.py:198` `mood_on` 默认 `True`，`:336/:407` 也在群覆盖允许列表）；`engine.py:711-733`（启发式命中后判官）、`memory.py:246-264`（每 5 轮抽取）、`style.py:70-81`（每 200 条 raw 重建）。
- 量级：以主回复为基准，稳态调用数 **×1.4–1.6**；judge/mood 的 prompt 含触发消息 + 回复 + 上下文脚本，但它不是主对话缓存前缀、全额计费。
- 修法：mood 改每 N 轮或并入主调用（让主模型输出 mood delta）；judge/mood 共用稳定前缀吃 prompt cache；记忆抽取批量低频。

## 3. P1（特定条件出错 / 无界增长 / 资源浪费）

### P1-1 三张表没有保留策略：`messages`、`usage_log`、`qzone_events` 只增不删

- `messages`：`storage.py:962-970` `mark_summarized` 只 `UPDATE ... SET summarized=1`，全仓 `DELETE FROM messages` 只有按群重置（`storage.py:977`）。触发过的轮次越多行数越大（每轮约 2–10 行），一年可达百万行级、数 GB。
- `usage_log`：每次模型调用插一行（`storage.py:2363`），无 prune。
- `qzone_events`：`storage.py:3362` 定义了 `prune_qzone_events`，但**全仓无调用者**；`qzone_state` 里 `ccount:<tid>`（`qzone_watch.py:265-269`）每见过一条说说就多一个键，也不清理。
- 对照：`raw_messages` 有 `trim_raw`（每 200 条，`deepseek.py:294-298`）、aux 表有 `prune_aux_tables`（`engine.py:95`，仅实例化时跑一次）。
- 修法：加一个每日清理任务：`messages` 删除 `summarized=1` 且 id 低于「最近 N 条」的行；`usage_log` 保留 180 天；`qzone_events` 保留 30 天并清 `ccount:` 孤儿键。

### P1-2 人设变更/摘要缺失时同步全量拉历史，内存尖峰

- 证据：`engine.py:1789-1791`（`_rebuild_summary` 内）

  ```python
  old_rows = [r for r in storage.messages_after(self.p.db_path, gid, 0, 10**6) if r['id'] < cutoff and r['content']][-60:]
  ```
  在 `_prepare_context`（群锁内、事件循环线程）里执行 `LIMIT 1000000` 的查询并 `dict()` 全部行；长命群几十万行 ⇒ 数百 ms～数秒卡顿 + 上百 MB 瞬时内存。
- 修法：`to_thread` + 只取最近 N 条（如 200）重建，或加 `ORDER BY id DESC LIMIT` 子查询。

### P1-3 三处只增不删的缓存（长连接下缓慢上涨）

1. `plugins/deepseek/Core/mentions.py:187/198`：`MemberDirectory._hot` 每条群消息 `observe()` 只写不删；成员更名/新人发言不断新增 `昵称→QQ`。
2. `mentions.py:146-147`：`MemberInfoDirectory._cache/_failed` 的 TTL 只决定「要不要重查」，**过期条目不删除**，key 是 `(gid, qq)`，随被查询过的成员数无限增长（每条 info 约 0.5–1.5 KB）。
3. `plugins/deepseek/Core/memory.py:104/125-132`：`MemoryManager._cache` 按 `user_id` 缓存「记忆列表 + BM25 索引」，只有写该用户记忆时才 invalidate，其他用户永不淘汰（单用户几十 KB～几百 KB）。
- 修法：三处统一加 LRU 上限（如 `_hot` 每群 2000、成员信息 8192、BM25 256 个用户）+ 惰性过期清扫。

### P1-4 `llm` 的客户端缓存在非 loop 线程被改：跨线程 `create_task` 静默失败 → aiohttp 会话泄漏，且无锁 dict 可能迭代时被改

- 证据：`llm.py:161-167`

  ```python
  loop = getattr(globals, 'event_loop', None)
  if loop is not None and loop.is_running():
      loop.create_task(self._safe_close(client))      # 非 loop 线程调用会 RuntimeError
  ```
  `llm.py:189-195` `prune_clients` 直接 `pop`；跨线程入口：`ui/web/server.py:27` `SocketIO(..., async_mode="threading")` → 保存配置 → `deepseek.py:344` `self.llm.prune_clients(active)`。`aclose`（`llm.py:178-186`）遍历 `_clients` 时若另一线程 `pop` ⇒ `RuntimeError: dictionary changed size during iteration`。
- 修法：`_clients` 加 `threading.Lock`；关闭动作一律 `asyncio.run_coroutine_threadsafe`；`prune_clients` 投递到 loop 线程执行。

### P1-5 消息处理热路径上的重复劳动

- `resolve_group` 每条消息跑 2 次（`deepseek.py` handle_message 一次 + `engine.py:157` 一次），每次 `dict(cfg)` + 数百键的类型校验（`config.py:653-673`），估算 0.1–0.3 ms/条。
- `_at_map` 每轮最多 3 次 `group_roster`（200 行 + 连接）与 `render_ats` 的正则重编译（`mentions.py:68`，昵称几百个时 0.5–2 ms/次；`engine.py:493/1191/1411/1662`）。
- `_name_of` 每个 @/回复目标一次 `get_user` 连接（`engine.py:431-439/505-522`），一条 @3 人的消息 = 4 次 connect ≈ 3.6 ms，**未触发也付**。
- `trim_raw` 是 `DELETE ... NOT IN (子查询)`，扫该群全部 raw 行，20k 行实测 **27.5 ms**，活跃群每 200 条抖一次（`storage.py:1982`，调用点 `deepseek.py:294` 在同步函数里）。
- 修法：`resolve_group` 按 `(gid, settings 版本)` 缓存；`_at_map` 单轮内复用；`_name_of` 批量 `WHERE user_id IN (...)`；`trim_raw` 换成 `id <= (SELECT ... OFFSET ?)` 单点删除或挪后台。

### P1-6 群锁内等 LLM/审批/发消息：同群队头阻塞

- 证据：`deepseek.py:423-427` 持锁 `await engine.handle(...)`；`engine.py:1111` 持锁等 LLM；`engine.py:1925-1930` 审批等待最长 120 s（`approval.py:56-110` 每 1 s 同步读库）；长跑 `loop_run.py:208-210` 持锁数小时、`stop()` 只写库不 cancel 当前轮（`loop_run.py:154-167`）。
- 后果：同群新消息只能排队；轮间 1 s 窗口里的消息只落库不唤醒（`deepseek.py:587-607`）→ 看起来「丢消息」；长回复还会持锁 sleep 600–1800 ms/条（`reply.py:254-310`）。
- 修法：等待期间释放群锁（挂起/唤醒模型）；长跑改「每轮 acquire/release」；发送延迟移出锁外。

### P1-7 关停/重载时取消被系统性吞掉

- 证据：`plugins/deepseek/Core/asyncutil.py:38-55`（`wait_task_resilient` 注释自述「外层收到 CancelledError 吞掉不重抛」）、`:65-85`；`deepseek.py:201-207` `_seg` 捕获 `BaseException` 继续下一段；`deepseek.py:223-224` 在 loop 线程同步执行 `triggers.stop`/`live.stop`，各自 `join(timeout=3)`（`triggers.py:263-266`、`live.py:375-378`）。
- 后果：on_stop 总时长可达数十秒且不可中断，期间 `main.py` 已 teardown、新实例可能已启动。
- 修法：区分「被等待任务失败」与「本任务被取消」，自身被 cancel 时 re-raise；HTTP 服务 stop 用 `to_thread`；SSE handler 设超时使 join 能及时返回。

### P1-8 指令路径不持群锁改群状态，与进行中的回合竞争

- 证据：`deepseek.py:609-622`（`_maybe_command` 在锁外直接执行）、`:658` `storage.reset_group_memory`、`:689` `style.force_build`、`:652-679` 一串 `upsert_group`。
- 时序：回合进行中 `/ai 重置` 清库 → 回合结束又写回自己的消息/摘要 ⇒ 记忆「清了一半」；`/ai 关闭` 后当前回合仍跑完并发消息。
- 修法：改状态的命令同样取群锁，或投递到引擎的命令队列在回合边界处理。

### P1-9 定时任务与旁路调用

- `proactor.py:222-235` 每 tick 全量 `list_groups`（在 loop 线程），随后每群 4–7 次 DAL（`:257-309`）⇒ 100 群约 400–700 次 connect ≈ 0.4–0.6 s/次（30 min 一次）。
- `_locks`（`deepseek.py:133`）、`_name_tasks`（`:431-442`，on_stop 不 cancel）、`memory._locks`（`memory.py:107-115`）等 per-group dict 无清理；量级小，但属于「只增」。
- `core/official/session.py:88` `asyncio.Queue()` 无界；`core/official/state.py:44/96` 的 `_failed` 只写不清（官方通道，默认不走）。

## 4. P2（微优化 / 有界但宽松）

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| P2-1 | `storage.py:1238-1260` | `list_users` 用 `LIKE '%…%'` 全表扫，`users` 无索引 | 加 `user_id` 索引，`names` 只做前缀匹配 |
| P2-2 | `storage.py:824-833` | `list_groups ORDER BY updated_at` 无索引（实测 1.4 ms/几百群） | `(enabled, updated_at)` 索引 |
| P2-3 | `context.py:73`、`engine.py:1434-1449` | 每丢一行重算全窗字符数；每工具轮 `json.dumps` 全部 tool_calls | 增量维护 token/字符数 |
| P2-4 | `reply.py:96-115` | `any(a <= i < b for a,b in cq_spans)` 每字符遍历区间 | 区间指针/二分 |
| P2-5 | `live_bus.py:44-45`、`live.py:36-42` | ring 1000 条 × 单条最大 ~21k 字符（CJK 峰值可达数十 MB）；`arguments` 未在 `_CLIP` 里 | ring 按总字节裁剪（如 8 MB）；补 `'arguments': 2000` |
| P2-6 | `templates/live/debug.html:381` | `state.events = evs.concat(state.events)` 无上限（DOM 有 3000 上限） | 数组同样裁剪 |
| P2-7 | `video_read.py:62-64/796-808` | 视频宫格图 LRU 6 张 × 1.8 MB ≈ 11 MB 常驻 | 降上限或落临时文件 |
| P2-8 | `ui/report_window.py:13-14` | GUI 模块级缓存整份好友/群列表（300 s 替换） | 可接受 |
| P2-9 | `ui/web/server.py:445/478` | 蓝图只注册一次 → 首代 `Core/web` 模块被永久钉住（一次性） | reload 时按插件名替换蓝图 |
| P2-10 | `main.py:377-378` | GUI 模式 asyncio 线程非 daemon | `daemon=True` 或显式 shutdown |
| P2-11 | `triggers.py:140-142` | `_spawn` 的 task 无引用、无并发上限 | 存集合 + 信号量；on_stop cancel |
| P2-12 | `core/history.py:10-12/33-41` | 20 万条 `raw_message` 原文常驻（按会话数淘汰，不按字节） | 总字节预算或短 TTL |
| P2-13 | `core/sync_bridge.py:39-51`、`Core/web.py:63-71` | 超时后不 cancel future，协程继续跑（Web 接口重复点击会叠加） | `except TimeoutError: future.cancel()` |

## 5. 已核对、确认「有回收 / 非泄漏」的项

避免后续重复排查：

- `bot.pending_requests`：`core/bot.py:155-158/286-289` finally `pop`，`handle_response` 命中即 `del`。
- `plugin_tasks`：有 `add_done_callback(discard)`（问题是无上限/不取消，不是永远不回收）。
- `sys.path`：`utils/plugin_loader.py:286-304/615-617` 有去重与移除；`sys.modules` 按 `__file__` 前缀清理（`:498-508`）；`_on_start_tasks`/`_bg_stops` 有 done 回调。
- `hooks._REGISTRY`：on_start/on_stop 都 `unregister()`（`Core/hooks.py`）。
- `approval._futures`：ttl + finally `pop`；`interrupt.Hub` 的 `end_turn` 在 `engine.py:1034` finally 调用。
- `MCP` 句柄 `shutdown` 清理 `_handles`；`watchers/coderunner/bili_live` 的 `Popen` 与文件句柄均 `finally close()`/`kill_tree`。
- `live_bus.close_all()` 清 `_subs` 并投递 CLOSE（`live_bus.py:143-158`）；`history_payload` 的临时 Subscription 不注册。
- `official` 连接 `__aexit__` 成对 `gateway.stop()+session.close()`；dispatch task 有 done 回调。
- `builtin_tools._SCHEMAS`/`SCHEMAS_BY_NAME` 模块级预构建共享引用（`builtin_tools.py:100/1558`），不是每轮重建。
- `tool_batch.run_batch` 的只读并发/写屏障策略合理（`tool_batch.py:61-141`）；`due_jobs`/`due_commitments` 均走索引。

## 6. 修复顺序建议

| 顺序 | 动作 | 收益 | 风险 |
|---|---|---|---|
| 1 | 删 `connect()` 的 `PRAGMA journal_mode=WAL`；`timeout` 降到 0.5 s | 每轮省 10–25 ms，消除 10 s 级冻结 | 极低（WAL 已由 `migrate` 设置） |
| 2 | `usage_log(created_at)` 索引 + `overview_stats` 单次 `GROUP BY` + `/api/groups` 去 N+1 | 仪表盘 440 ms → ~10 ms | 低（需写迁移 v21） |
| 3 | SSE：`subscribe` 进 `try`、`settimeout(30)`、`start()` 重入守卫 | 消除线程/订阅泄漏与旧实例钉住 | 低 |
| 4 | `core/events.py` 背压 + `_teardown_connection` 取消旧 handler task | 消除任务堆积与双实例并发写 | 中（涉及生命周期） |
| 5 | 热重载闭合：超时 cancel + 拒绝重载；on_stop 预算 < 10 s | 消除「越重载越乱」 | 中 |
| 6 | 每日清理：`messages`/`usage_log`/`qzone_events`/`ccount:` | 控制库体积与仪表盘随行数恶化 | 低 |
| 7 | 三处缓存加 LRU/TTL；`_rebuild_summary` 改分页 | 长跑内存可控 | 低 |
| 8 | mood 改每 N 轮 / 合并进主调用 | API 调用数 −20%～30% | 低（可配置开关） |

## 7. 结论与边界

- 这些结论基于静态调用链核对 + 本机小库实测；线上真实数据量下的绝对值会不同，但「随行数线性增长/随连接时长增长」的方向不变。
- 未做的验证：多群高并发压测、SSE 黑洞客户端复现、热重载真实双实例复现（结论来自代码路径，时序类问题标注了触发条件）。
- 审查阶段（0–7 节）是只读结论；2026-11-26 当天已按第 8 节完成修复，每一项都配了回归测试。

## 8. 修复记录（2026-11-26）

按第 6 节顺序全部落地。验证基线：`cd /home/ubuntu/tide && TMPDIR=/home/ubuntu/tmpdir /tmp/tvenv/bin/python -m pytest tests/ -q` → **238 passed**（修复前 133）；全部改动 `py_compile` 通过、17 个 Jinja 模板可解析、改动模板的内联 JS 过 `node --check`、`git diff --check` 干净。

### 8.1 P0

| 编号 | 问题 | 修复 | 主要位置 | 测试 |
|---|---|---|---|---|
| P0-1 | 同步 SQLite 跑在事件循环上，慢语句可冻结全 bot 10 s | `connect()` 去掉每次重设 `PRAGMA journal_mode=WAL`（只由 `migrate()` 设一次）、`timeout` 10 s → 2 s；热路径 DAL 全部 `asyncio.to_thread`（engine 摘要/历史/用量/中间消息/名字预取、deepseek 原始语料与群资料、tasks `open_block`、proactor `list_groups`） | `storage.py connect/migrate`；`engine.py _prepare_context/_prepare_tail/_rebuild_summary/_run_turn/_judge/add_usage`；`deepseek.py _record_raw/handle_message`；`tasks.py open_block`；`proactor.py _tick` | `test_audit_storage_2026_11_26.py`、`test_audit_engine_2026_11_26.py` |
| P0-2 | 插件 handler task 无上限堆积、重连后旧任务继续跑 | 入站改为「(插件, 会话) 有界队列 50 + 单 worker 串行」，满时丢最旧并告警；新增 `cancel_plugin_tasks()`，`_teardown_connection()` 第一步取消全部在途 handler/worker | `core/events.py`、`main.py` | `test_audit_lifecycle_2026_11_26.py` |
| P0-3 | 热重载超时吞异常 → 新旧实例并存 | `_call_stop` 返回「是否干净结束」；超时取消旧 on_stop future；`unload_plugin`/`reload_plugin` 在 stop 不干净时拒绝卸载/重载并记录错误 | `utils/plugin_loader.py` | `test_audit_lifecycle_2026_11_26.py` |
| P0-4 | 仪表盘 15 次全表扫 + `/api/groups` N+1 | 新增 `usage_log(created_at)` 索引与 `overview_stats()` 单次 `GROUP BY` 聚合；`/api/groups` 改 `usage_summary_many()` 单连接批量；另加 6 个常用索引 | `storage.py`（迁移 v21）、`Core/web.py` | `test_audit_storage_2026_11_26.py` |
| P0-5 | SSE 订阅/线程泄漏，黑洞客户端钉住旧实例 | `subscribe` 与响应头写入移进同一 `try`、`connection.settimeout(30)`、`finally` 退订；`start()` 加重入守卫（活着直接返回、线程死才重建）；ring 加 8 MiB 字节预算；前端事件数组上限 3000 | `Core/live.py`、`Core/live_bus.py`、`templates/live/debug.html` | `test_audit_sse_2026_11_26.py` |
| P0-6 | 每轮固定调 mood 模型（调用数 ×1.4–1.6） | 新增 `mood_every_rounds`（默认 3），`_mood_due()` 按群计数、首轮必跑；配置进 `DEFAULTS` 与 web.json | `engine.py`、`config.py`、`web.json` | `test_audit_engine_2026_11_26.py` |

### 8.2 P1

| 编号 | 问题 | 修复 | 主要位置 |
|---|---|---|---|
| P1-1 | `messages`/`usage_log`/`qzone_events`/`ccount:` 无保留策略 | 新增迁移 v21 索引 + `prune_messages`（只删已摘要的旧行，未摘要一条不删）/`prune_usage_log`/`prune_qzone_state`/`housekeeping()`；`Core/housekeeping.py` 每 6 h 在 `to_thread` 里跑一次（启动延迟 120 s，间隔下限 600 s），配置 `housekeep_on/messages_keep/usage_keep_days/qzone_events_keep_days` | `storage.py`、`Core/housekeeping.py`、`deepseek.py` |
| P1-2 | 人设变更时 `_rebuild_summary` 拉 10⁶ 行全历史 | 改 `messages_before(..., 200, roles=('user','assistant'))` 分页 + `to_thread`，取末 60 条渲染 | `engine.py` |
| P1-3 | 三处只增缓存（`MemberDirectory._hot`、`MemberInfoDirectory._cache/_failed`、memory 记忆缓存） | 全部改 LRU：每群 2000 / 全局 20000 热度表，成员信息 8192、失败表 2048 且 TTL 惰性清扫，记忆缓存 256 用户（淘汰时释放 BM25 索引）；临界区不跨 await | `mentions.py`、`memory.py` |
| P1-4 | `llm._clients` 跨线程 `loop.create_task` + 无锁 dict | `RLock` 保护客户端表与重建；`_schedule_close` 无运行 loop 时走 `run_coroutine_threadsafe`（原来跨线程 create_task 抛错被吞 → 连接泄漏）；`prune_clients` 锁内摘除、锁外关闭 | `llm.py` |
| P1-5 | 热路径重复劳动（每轮重建 schema/提示词、`resolve_group` 每条消息 2 次、`_at_map` 每轮 3 次查 200 行、昵称逐个查、@ 正则重编译） | `cfg_for_group()` 按 settings 指纹缓存（容量 128）；`_prefetch_names` 一次 `IN` 批量预取 + 轮内缓存；`_at_map` 按轮缓存；`render_ats` 的 `re.compile` 按昵称集合 `lru_cache(8)` | `deepseek.py`、`engine.py`、`mentions.py` |
| P1-6 | 群锁内等 LLM/审批、长跑整段持锁、审批读库阻塞 | `loop_run` 改为每轮进出锁（轮间 sleep 在锁外）；`approval.request()` 的读写库改 `to_thread`（轮询仍 1 s，语义不变） | `loop_run.py`、`approval.py` |
| P1-7 | 取消被系统性吞（`_seg` 捕 BaseException、插件内 asyncutil 死代码） | `deepseek._seg/_seg_sync` 对 `BaseException` 记日志后重抛（让上层知道 stop 未完成）；插件内 `Core/asyncutil.py` 补 `_is_self_cancelling`（注：该文件末尾仍 `from utils.asyncutil import *` 覆盖，线上走框架版——框架版刻意吞外层取消，有既有测试保护） | `deepseek.py`、`Core/asyncutil.py` |
| P1-8 | 指令路径不持锁改状态 | `_MUTATING_COMMANDS` + `_command_mutates()`，仅「改状态」子命令包群锁；approve/reject 与 loop 状态类故意免锁（调用点本就在锁外，避免与等待审批的轮死锁） | `deepseek.py` |
| P1-9 | 定时任务/无界队列（`_schedule_group_name` 不取消、official out_queue 无界、`_locks` 不清理） | `_name_tasks` 改 dict[gid→task] 且 on_stop 统一取消；official 队列 `maxsize=1000` 满丢最旧；`_failed` 表加容量与过期清扫；on_stop 清 `_locks`/`_cfg_cache` | `deepseek.py`、`core/official/session.py`、`core/official/state.py` |

### 8.3 P2

| 编号 | 修复 | 位置 |
|---|---|---|
| P2-1/2 | `trim_raw` 改「取游标 + 范围删」；`messages_since` 补 `(group_id, summarized, id)` 索引 | `storage.py` |
| P2-3 | `select_window`/`plan_tail` 增量求和；工具调用称重按轮缓存 | `context.py`、`engine.py` |
| P2-4 | `_split_sentences` 的 CQ 区间判定改有序游标 | `reply.py` |
| P2-5/6 | live ring 单事件字段截断（`arguments` 2000 字符）+ 8 MiB 预算；`/live/api/events` 历史回看沿用同一预算 | `live.py`、`live_bus.py` |
| P2-7 | 视频宫格缓存 6 张 + 16 MB 双上限 | `video_read.py` |
| P2-8 | `ui/report_window.py` 300 s 缓存 —— **评估后不修**（只缓存好友/群列表且每次全量替换，不随时间增长） | — |
| P2-9 | 插件蓝图热重载后旧路由残留 → `_purge_blueprint()` + 自动重注册 | `ui/web/server.py` |
| P2-10 | GUI asyncio 子线程改 daemon | `main.py` |
| P2-11 | `triggers._spawn` 任务强引用 + 并发上限 4 | `triggers.py` |
| P2-12 | `core/history.py` 加 64 MB 总字节预算 + 引用计数 | `core/history.py` |
| P2-13 | `sync_bridge`/`web._run_async` 超时取消底层 future | `core/sync_bridge.py`、`Core/web.py` |

### 8.4 已知残留与取舍

- **P0-1 仍有少量同步 DAL**：`_judge` 的 `recent_messages`、`set_summary/advance_summary/mark_summarized`、每条消息的 `insert_message/upsert_group/upsert_user` 仍在事件循环上（单次毫秒级）。这些调用点分散在多个分支，批量改动风险高于收益，列为后续收口项。
- **P1-7 线上仍走框架版 `wait_task_resilient`**：框架版「外层取消不外传、内层跑完」是刻意设计，有 `tests/test_shutdown_resilience.py` 保护，故只做局部改进不切换。
- **P1-2 语义微调**：新摘要输入不再包含 `tool` 行（原实现会包含），滚动摘要本身仍以旧摘要为输入，信息不会整段丢失。
- **`_seg` 重抛 `CancelledError`**：on_stop 中途被取消时后续清理段会跳过；正常关停路径不受影响，且 `plugin_loader` 会因 stop 不干净拒绝卸载，避免双实例。
- **mood 计数只在内存**：重启归零 → 首轮必跑；这是可接受的有偏。
- **P1-8 锁边界**：以后新增「阻塞型命令」必须重判 `_command_mutates()`，否则可能死锁或绕开锁。
- **P1-6 行为变化**：长跑轮间释放锁后，普通消息轮可与长跑轮交替执行（仍由群锁串行），上下文互相影响比原先更频繁。
- **P1-9 背压语义**：单群积压超过 50 条事件时丢最旧事件并打印 `[背压]` 告警（此前是无上限堆积）。
- **SSE/历史回看仍受进程内 ring 限制**：重启即清空，最多回看 1000 条 / 8 MiB。

## 附录 A：度量复现

```bash
# 环境：/tmp/tvenv（pytest/flask/httpx 等）
python - <<'PY'
import sys, os, tempfile, time
sys.path.insert(0, 'plugins/deepseek')
from Core import storage
db = os.path.join(tempfile.mkdtemp(), 't.db'); storage.migrate(db)
t = time.perf_counter()
for _ in range(200): storage.connect(db).close()
print('connect()', round((time.perf_counter()-t)/200*1000, 3), 'ms')
PY

# EXPLAIN QUERY PLAN（验证 usage_log / messages 是否全表扫）
sqlite3 <db> "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM usage_log WHERE created_at>=0"
```

## 附录 B：证据索引

- 同步 SQLite / connect 开销：`plugins/deepseek/Core/storage.py:16-21`；热路径 `engine.py:160/185/1703/1710/1136/1140/1186/1292-1298`、`deepseek.py:285/425`、`tasks.py:112`、`qzone_watch.py:194-203`。
- 事件任务：`core/events.py:16/116-120/152-156/180-184/207-211`；群锁 `plugins/deepseek/deepseek.py:423-427`、`loop_run.py:208-210`；teardown `main.py:78-96`。
- 热重载：`utils/plugin_loader.py:481-488/922-925`；on_stop 预算 `plugins/deepseek/deepseek.py:215-229`。
- 仪表盘：`storage.py:2378-2410`、`web.py:572-574/618`。
- SSE：`plugins/deepseek/Core/live.py:56-73/307-317/344-351/366/375-378`；总线 `live_bus.py:44-45/143-158`。
- 模型调用：`engine.py:1046-1060`、`config.py:198/336/407`、`mood.py:195`、`memory.py:246-264`、`style.py:70-81`。
- 保留策略：`storage.py:962-970/977/2363/3362`、`qzone_watch.py:265-269`、`deepseek.py:294-298`、`engine.py:95`。
- 缓存：`mentions.py:68/146-147/187/198`、`memory.py:104/125-132`、`deepseek.py:382`。
- LLM 客户端：`llm.py:161-167/178-195`、`deepseek.py:344`、`ui/web/server.py:27/327`。
- 取消/生命周期：`Core/asyncutil.py:38-55/65-85`、`deepseek.py:201-207/223-224`、`triggers.py:263-266`、`live.py:375-378`。
