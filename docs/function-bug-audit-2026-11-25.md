# 功能缺陷审查报告（2026-11-25）

- **仓库**：`F:\PythonProgram\tide`（master，审查时 HEAD `45b8c6d`）
- **范围**：框架核心（main、core、ui/web、utils）与 `plugins/deepseek` 全部 Python 逻辑
- **方式**：逐文件静态审查 + `pyflakes` 未定义名扫描 + 三路并行深读（engine/loop/scheduler、events/loader/main、storage/message/reply/web）+ 逐条对源码复核
- **测试基线**：`python -m pytest tests/ -q` → **78 passed**
- **说明**：按需求，本报告**只列功能缺陷**。鉴权、越权、注入、敏感信息泄露、路径穿越等安全类问题不在本文范围内，另案处理。
- **行号**：基于审查时的工作区；后续改动可能使行号偏移，定位时以函数名/代码片段为准。

---

## 1. 结论摘要

| 级别 | 数量 | 典型影响 |
|---|---|---|
| 高 | 7 | 功能直接不可用、进程卡死、整条消息丢失、重连后插件实例泄漏 |
| 中 | 21 | 特定场景下发送失败/数据丢失/白烧 token/内存或缓存不一致 |
| 低 | 17 | 边界输入、配置项、资源增长、日志与统计偏差 |

**最影响日常使用的五件事**：

1. “立即总结”按钮 100% 报 500（一行未定义变量）。
2. 插件“禁用/启用”后事件处理器不会恢复，且禁用不会停后台服务。
3. 设置页把“单条最大字数”填 0/负数会让整个 bot 死循环卡死。
4. 私聊会话会被定时唤醒/提醒当作群处理，发送必然失败并持续烧 token。
5. 连接建立后的初始化异常会绕过 `on_stop`，重连后旧插件实例及其后台任务永久残留。

---

## 2. 高

### H-01 “立即总结”接口必 500：未定义变量 `coroutine`

- **位置**：`plugins/deepseek/Core/web.py:536-537`
- **代码**：

  ```python
  coro = p.memory.force_extract(gid, p.cfg, p.llm)
  _, err = _run_async(coroutine, timeout=90)   # coroutine 不存在
  ```

- **触发**：群管理页点击“立即总结” → `POST /deepseek/api/groups/<gid>/summarize-now`。
- **后果**：每次调用都抛 `NameError`，Flask 返回 500，按钮永远失败。
- **修复**：改为 `_run_async(coro, timeout=90)`。

### H-02 插件“禁用/启用”是坏的，且禁用不停后台服务

- **位置**：`utils/plugin_loader.py:862-883`（`enable_plugin` / `disable_plugin`）
- **触发**：GUI（`ui/plugin_window.py:15/19`）或 Web 面板（`ui/web/server.py:229-244`）先“禁用”再“启用”。
- **后果**：
  - `disable_plugin()` 把该插件所有事件处理器从 `event_handlers` 删除，但**没有调用 `on_stop`**。deepseek 的 tasks/scheduler/proactor/触发服务/Live 订阅仍在运行并通过 `self.bot` 主动发消息——“已禁用”只对入站消息生效。
  - `enable_plugin()` 只把 `plugin_info[...]['enabled']` 翻回 `True`，**不重新注册处理器、不调用 `on_start`**。界面显示“已启用”，但插件从此收不到 `message/notice/request`，直到热重载。
- **修复**：`disable` 走真正的卸载/`on_stop`；`enable` 重新收集 `register()` 的处理器并调用 `on_start`（或直接实现为重载）。

### H-03 `split_max_len` 设为 0/负数会让 bot 死循环卡死

- **位置**：`plugins/deepseek/Core/reply.py:110-135`（`_hard_split`）、`reply.py:273`；配置校验 `plugins/deepseek/Core/config.py:379-385`；设置页 `plugins/deepseek/templates/settings.html:168`
- **代码**：

  ```python
  while len(rest) > max_len:   # max_len <= 0 时恒真
      window = rest[:max_len]  # ''
      ...
      cut = max_len            # 0
      out.append(rest[:0])     # 追加空串
      rest = rest[cut:]        # 原样不变 → 无限循环
  ```

- **触发**：设置页“单条最大字数”填 `0` 或负数。该输入没有 `min`，`merge_config` 的 `_as_int` 也没有下限。
- **后果**：下一条非空回复进入 `split_reply` → `_hard_split` 在事件循环线程里无限追加空串，内存持续增长，整个 bot 卡死（心跳、消息处理、信号桥全部停摆）。
- **修复**：`split_reply` 入口 `max_len = max(1, int(max_len))`（`min_len` 同样兜底），配置侧增加下限。

### H-04 JSON 卡片解析抛 `UnboundLocalError`，整条消息被丢弃

- **位置**：`plugins/deepseek/Core/message.py:215-232`（`_card_from_json`）
- **代码**：

  ```python
  title, best_rank, best_depth = '', 99, 99
  for path, v in _walk_json(obj):
      ...
      if any(sk in path[:-1] for sk in _TITLE_SKIP_PATHS):
          pass                     # rank 未赋值
      elif key in _TITLE_BEST:  rank = 0
      elif key in _TITLE_FALLBACK: rank = 1
      else: rank = 99
      if rank < 99:                # 首次命中 skip 分支 → UnboundLocalError
  ```

- **触发**：JSON 卡片里第一个非空字符串叶子位于 `preview / button / buttons / action / icon / cover` 之下（`_TITLE_SKIP_PATHS`）。
- **后果**：异常从 `parse_event` 抛到 `handle_message`，被框架的 `_safe_execute_handler` 吞掉，这条消息既不回复也不落库。若此前已赋过 `rank`，skip 分支会沿用上一个叶子的 rank，按钮/预览文案可能覆盖真正的标题。
- **修复**：skip 分支显式 `rank = 99`（或 `continue`）。

### H-05 连接建立后的初始化异常绕过 `on_stop`，旧插件实例泄漏

- **位置**：`main.py:170-189` 初始化段 + `main.py:247-296`；`utils/plugin_loader.py:1116-1124`
- **触发**：
  - 握手成功后连接立刻断开，`await init_bots(websocket)`（`main.py:189`）抛 `ConnectionClosed`；
  - 或插件返回**固定名 Blueprint**，第二次重连时 `register_plugin_blueprints`（`main.py:172`）抛 `ValueError`。
- **后果**：异常命中 `main.py:274-277` 的通用 `except Exception` 后直接 `continue`，**跳过 279-296 的收尾块**（`ashutdown_all_plugins()` + 清空 `globals.bot_instance/websocket`）。但 `main.py:164` 已经创建了新 manager，并给所有插件排了 `on_start`。下一轮 `init_plugin_manager` 用新 manager 覆盖全局，旧 manager 的 `on_stop` 永远不执行，旧实例的 tasks/scheduler/proactor/触发服务/Live 订阅继续运行，且持有已关闭的 socket；`atexit` 只关最新 manager。
- **修复**：把“建连后的初始化”与“连接生命周期”包进 `try/finally`；任何 `continue`/`break` 前先 `await manager.ashutdown_all_plugins()` 并清空 globals。

### H-06 私聊会话被“定时唤醒”当作群，必然发送失败并白烧 token

- **位置**：`plugins/deepseek/Core/proactor.py:235`、`:283-285`、`:309-310`；`plugins/deepseek/Core/engine.py:291-300`
- **触发**：私聊会话在 `groups` 表里是一行以 QQ 号为 `group_id` 的记录，`enabled=1`（私聊对话本身要求它开启）。`proactor._tick` 遍历所有 `enabled=1` 的行，命中唤醒/主动搭话。
- **后果**：`engine.handle_wake` 把 `ParsedMessage` 写死为 `chat_type='group'`，后续 `_emit_final`/`qq_send_text` 用 `message_type='group'` 往 QQ 号发送 → 必失败。每次唤醒都会：
  - 跑一次真实 LLM 调用，然后发送失败（异常被 proactor 的 per-group `except` 吞掉）；
  - 往私聊历史写入一条系统通知行；
  - 在 `bg_runs` 里记错 `target_type`。
  私聊里创建的 `poll_add` 轮询（`_tick_polls`）走同一路径，同样失败。
- **修复**：proactor 只遍历 QQ 群；或把会话类型带进 `handle_wake`，按它构造 `ParsedMessage` 并使用 `message_type='private'`。
- **备注**：`bili:` 虚拟群行默认 `enabled=0`，通常不会进入此路径，除非在 Web 群列表手动打开。

### H-07 私聊里的到期提醒永远发不出去，且每 60 秒无限重试

- **位置**：`plugins/deepseek/Core/tasks.py:190-208`
- **触发**：在私聊里通过 `task_add`（或记忆抽取）登记了带 `due_date` 的约定；或群被临时关闭后又有到期约定。
- **后果**：
  - `enabled=1` 的私聊：`due_commitments` 返回的 `group_id` 是 QQ 号，`_tick` 用 `message_type='group'`、还带 `[CQ:at,qq=...]` 发到 QQ 号 → 失败；失败后不 `mark_reminded`，下一 tick 再取到、再失败，**永久每 60 秒重试一次**。
  - `enabled=0` 的群：`tasks.py:193-194` 直接 `mark_reminded`，提醒被永久吞掉——这与 `:198` 注释“群级关闭不标记，将来重新开启还会提醒”的意图矛盾。
- **修复**：commitments 落库时记录会话类型；私聊用 `message_type='private'` 且不带 CQ at；群被关闭时按注释意图处理，不要直接标记已提醒。

---

## 3. 中

### M-01 报表窗口获取群/好友列表每次卡满 30 秒

- **位置**：`ui/report_window.py:137-152`、`:162-177`
- **触发**：GUI 模式启动后自动拉取，或打开“报表”窗口。
- **原因**：在后台线程里 `asyncio.new_event_loop()`，然后 `run_until_complete(bot.get_group_list())`。但响应由主事件循环线程 `future.set_result()` 回填；跨线程 `set_result` 不会唤醒这个新 loop（`call_soon` 不是线程安全的）。实测每次调用要等满 `wait_for` 的超时（30s）才被处理，两次调用约 60s。`dpg.set_value/configure_item` 也从后台线程直接调用，DearPyGui 不保证线程安全。
- **修复**：用 `asyncio.run_coroutine_threadsafe(..., globals.event_loop)` 把请求调回主循环；UI 更新切回 UI 线程。

### M-02 一条脏数据会让记忆抽取永久卡住

- **位置**：`plugins/deepseek/Core/llm.py:424`
- **代码**：`float(e.get('importance', 0.6) or 0.6)` 未做类型兜底。
- **触发**：模型返回的 `importance` 是 `"高"`、对象等非数字。
- **后果**：`extract_memories` 抛异常，`_maybe_extract` 在最外层捕获后**不推进游标**，于是每轮重试同一批消息、每次都在同一处失败——记忆永远抽不出来，还持续烧 token。同文件 `_clean_self_entries:444-448` 已有正确的 try/except 写法。
- **修复**：照 `_clean_self_entries` 的写法包住 `float` 转换。

### M-03 缺少 `bilibili_api` 时，直播凭据接口返回 500 而不是“缺依赖”

- **位置**：`plugins/deepseek/Core/web.py:368-379`、`:387-398`；`plugins/deepseek/Core/bili_auth.py:52-63`
- **原因**：`load_credential()` 里的 `from bilibili_api import Credential` 在协程内执行，ImportError 被 `_run_async` 转成 `(None, (msg, 500))` 返回；外层 `except ImportError` 不会触发。错误分支又调用 `credential_snapshot()`，它再次 `load_credential()`，直接把 ImportError 抛到 Flask。
- **后果**：直播页的凭据/扫码面板只会显示 500，而不是提示安装依赖。
- **修复**：在 `credential_snapshot()`/`validate()` 内兜住 ImportError，或在 endpoint 先探测依赖。

### M-04 通过框架通用“插件配置”页保存 deepseek 配置，可能永久丢掉未登记键

- **位置**：`ui/web/server.py:286-329`
- **原因**：先把前端提交的 `web.json` schema 子集**整文件覆盖**写入 `plugins/<name>/config.json`，之后再调 `plugin.call_function("load_config", config=data)` 让插件合并内存里的全量配置。
- **触发**：插件未加载（加载失败、重连窗口）、热重载中，或插件没实现 `load_config`。
- **后果**：没有第二次修复，`llm_providers`（含 API key）、`trigger_token` 等不在 schema 里的键永久丢失；`call_function` 抛异常时只 `print`，接口仍返回 `{"ok": true}`，界面显示保存成功，用户无从察觉。
- **修复**：先校验插件存在并调用插件合并/校验，成功后再原子落盘；失败返回 `ok:false`。

### M-05 “0 条发送”仍被落库、计入去重、消耗引用名额

- **位置**：`plugins/deepseek/Core/engine.py:1111-1118`、`:1288-1295`、`:1531-1540`；`plugins/deepseek/Core/reply.py:280-284`
- **触发**：
  - 工具轮插话：`send_in_chunks` 抛异常后 `n=0`，但 `:1116` 仍 `sent_texts.add(interim)`；
  - official 关池模式名额耗尽：`send_in_chunks` 在 `pctx.plan_text()` 返回空列表时**正常返回 0**（不抛错），但调用方仍 `insert_message`、`sent_texts.add`；`quote_id` 参数里的 `take_quote()` 也已经执行，引用名额被消耗。
- **后果**：用户根本没收到，但历史里多了一条“她说过”的 assistant 消息，模型重发同一内容会被去重吞掉。
- **修复**：只有返回条数 > 0 才落库/加去重/消费引用。

### M-06 终态回复不进本轮 `messages`，续轮看不到自己刚说的话；空续轮覆盖 `final_text`

- **位置**：`plugins/deepseek/Core/engine.py:915-938`
- **原因**：`_emit_final` 只发送、落库，不把 assistant 终态 append 进 `messages`。收尾期间来了插队时，`engine.py:931` 用同一个 `messages` 再请求模型，模型看不到自己刚说的话，容易重复汇报。`:936` 又用续轮结果**覆盖** `final_text`；续轮返回空正文时 `final_text` 变成 `''`，导致 `bg_runs` 记成 `empty`、心情判定的 `reply_text` 为空、长跑的 `since_spoke` 不重置。
- **修复**：`_emit_final` 发送成功后把该 assistant 消息 append 进 `messages`；只有非空续轮正文才覆盖 `final_text`。

### M-07 长跑强制汇报发送失败会直接终止整个长跑

- **位置**：`plugins/deepseek/Core/engine.py:1287-1294`
- **原因**：同函数里 LLM 请求失败有兜底（`:1255-1262`，注释明确“跳过本次，继续任务”），但 `reply.send_in_chunks` 没有任何 `try/except`。
- **后果**：网络抖动/限流导致的发送异常沿 `_chat_loop → _run_turn → _run_locked → _worker` 上抛，`_worker` 把长跑 session 置为 error 并通知主人，任务终止。
- **修复**：给强制汇报的发送加同样的兜底。

### M-08 定时任务永久失败会无限重试，每次重试都跑一次 LLM

- **位置**：`plugins/deepseek/Core/scheduler.py:225-231`
- **触发**：机器人被踢出目标群、群被解散、发送接口持续报错。
- **后果**：`_tick` 对 `_fire` 的任何异常都执行 `next_run_at=now+300, enabled=True`，每 5 分钟：`_compose`（一次真实 LLM 调用）→ 发送失败 → 再顺延，无上限。日志与 token 用量持续增长。
- **修复**：区分可重试错误并加次数上限，超限后停用并写 `last_error`。

### M-09 注入表的提示词编号与工具使用的数据库 ID 不一致

- **位置**：`plugins/deepseek/Core/context.py:421-427`；`plugins/deepseek/Core/builtin_tools.py:1757-1761`
- **原因**：每轮注入块用 `enumerate(rows, 1)` 渲染成 `[1][2][3]`，但 `inject_list/inject_toggle/inject_remove` 使用 DB 自增 `id`（`inject_list` 显示 `[r['id']]`）。
- **后果**：模型很可能直接用上下文里的 `[1]` 调 `inject_remove/inject_toggle`，删掉/停用的是另一条，或报“条目不存在”。text 为空的条目被 `continue` 跳过后编号还会跳号。
- **修复**：注入块直接用 `r['id']` 作为方括号编号。

### M-10 Web 删除单条记忆后缓存不失效

- **位置**：`plugins/deepseek/Core/web.py:756-760`
- **原因**：DELETE 分支只 `storage.delete_memory()` 就 return，没有 `p.memory.invalidate()`；而 PUT 分支和 `forget_user` 都有。`MemoryManager._index` 按用户缓存了 `entries + BM25`。
- **后果**：删除后该用户下一次召回仍从缓存里拿到已删除的条目，继续注入模型，直到下一次记忆抽取或其它编辑操作才失效。
- **修复**：删除前取该记忆的 `user_id`，删除后 `invalidate(user_id)`。

### M-11 首次安装插件依赖会冻死整个事件循环（最长无限）

- **位置**：`utils/plugin_loader.py:243-271`
- **原因**：`install_to_plugin_lib` 用同步 `Popen` + `for line in proc.stdout` 读 pip 输出，但**超时检查写在读到一行之后**（`:263`），循环结束后的 `proc.wait()`（`:266`）也没有超时。`--progress-bar off` 下 pip 卡在网络/认证时会一行输出都没有。该函数由 `load_plugin_by_folder` 在事件循环线程里同步调用（`main.py:164`）。
- **后果**：阻塞期间心跳、消息处理和信号桥全部停摆；WebSocket 可能因心跳超时被 NapCat 断开，触发重连。
- **修复**：`proc.communicate(timeout=...)` 或看门狗线程；并把 pip 安装放到 `asyncio.to_thread`/executor。

### M-12 `get_login_info` 失败响应当成成功，导致断线重连循环

- **位置**：`core/events.py:32-39`
- **原因**：`init_bots` 是裸 `websocket.send`，没有走 `bot.call_api` 的 pending future，响应直接进 `parse_event`。这里无条件执行 `event['data']['nickname']`、`event['data']['user_id']`。
- **触发**：NapCat 尚未登录/刚重连，返回 `{"status":"failed","retcode":...,"data":null,"echo":"get_login_info"}`。
- **后果**：`TypeError/KeyError` 从 `parse_event` 抛出 → `main.py:241` 的兜底当成连接错误 break → 重连 → 再失败，直到重连次数耗尽。GUI 模式下该行还是在 asyncio 子线程里直接调 `dpg.set_value`。
- **修复**：先校验 `retcode/data`，或改用 `bot.get_login_info()` 走 pending future；UI 更新投递回 UI 线程。

### M-13 `on_start` 与 `on_stop` 没有顺序约束

- **位置**：`utils/plugin_loader.py:674-700`（`:679` 的 `create_task`）；`main.py:177-183`
- **原因**：异步 `on_start` 是 fire-and-forget，task 没有保存、也没有被 await。
- **触发**：连接在 `on_start` 执行期间断开（deepseek 的 `on_start` 第一步就是 `await self.bot.get_login_info()`，最多等 30s）。
- **后果**：`connect_and_run` 立刻 `await ashutdown_all_plugins()`：`on_stop` 先停掉尚未启动/正在启动的服务，`on_start` 随后继续把这些后台循环**重新启动**；实例已被 `_STOP_MARK` 标记，后续关停和 atexit 都会跳过它。孤儿定时器/Live 订阅永久残留，重连再叠加一份。另外 `create_task` 没有强引用，有被 GC 的理论风险。
- **修复**：把 on_start task 收进 manager 的任务集合，断线时先 await/cancel；`on_start` 入口检查实例是否已标记停止。

### M-14 `loop-notice` 没进历史隐藏名单

- **位置**：`plugins/deepseek/Core/storage.py:788-789`；`plugins/deepseek/Core/context.py:151-157`；`plugins/deepseek/Core/loop_run.py:237`
- **原因**：`_HISTORY_HIDDEN_NAMES` 包含 wake/join/trigger/speakup，但漏了 `loop-notice`；而 `context.BG_NOTICE_NAME` 明确把 `loop-notice` 视为内部通知。
- **后果**：跑过长跑后，模型调用 `history_query` 会把 `[长跑任务通知]…`、`[系统通知] 主人拒绝了长跑申请#…` 这类长指令当“群友”消息读回来，污染上下文，甚至被当成用户指令继续执行。
- **修复**：把 `'loop-notice'` 加入隐藏名单（老数据可按 `BG_NOTICE_PREFIX` 里的 `[长跑任务通知]` 前缀兼容）。

### M-15 硬截尾模式下锚点漂移，会永久丢消息

- **位置**：`plugins/deepseek/Core/context.py:390-407`；`plugins/deepseek/Core/engine.py:1625-1632`
- **原因**：`plan_tail` 用 `cut = len(rows) - len(win)` 并假设 `win` 恒为 `rows` 后缀。`select_window` 里 `_trim_orphans` 开头丢行仍保持后缀性，但 `_repair_tool_chains`（`context.py:113-145`）会删掉**中段/尾部**残缺的 `assistant.tool_calls` 批和无主 tool 行；此时 `win` 长度变小、起点没变，`cut` 就大于窗口真实起点。`_prepare_tail` 随后执行 `new_up_to = rows[cut-1]['id']` 并推进锚点。
- **触发**：某轮被中断/异常，历史里留下残缺的工具调用批（代码注释也承认会出现）。
- **后果**：下一轮 `messages_since` 跳过窗口内这些正常消息；tail 模式又不调摘要，这些消息永久丢失。
- **修复**：不要用长度差，直接用 `win[0]['id'] - 1` 作为锚点。

### M-16 在事件循环线程内卸载插件会跳过 `on_stop`

- **位置**：`utils/plugin_loader.py:479-482` + `:1019-1023`
- **原因**：`unload_plugin` 直接调 `_call_stop`；检测到当前在事件循环线程内时，`_call_stop` 打印“已跳过”并 return，但 `unload_plugin` 随后仍然 `_mark_stopped(inst)` 并删除实例。
- **触发**：在插件自己的 async handler 里调用 `reload_plugin(...)`/`unload_plugin(...)`。Web 面板的重载来自 Flask 线程，不受影响。
- **后果**：async `on_stop` 完全没跑（MCP 连接、LLM client、线程都没关），且 atexit 也不会补调。
- **修复**：循环内改为 `create_task(on_stop())` 并保留引用，或明确禁止循环内卸载。

### M-17 长跑“私聊”判定只看是不是主人 QQ

- **位置**：`plugins/deepseek/Core/loop_run.py:75-77`
- **原因**：`_chat_type` 用 `gid == owner_qq` 反推私聊。
- **触发**：白名单好友的私聊长跑（好友发起请求、主人在自己私聊里 `/ai loop 同意`）。
- **后果**：任务执行在好友 QQ 这个 gid 上，但 `_chat_type` 返回 `'group'`，启动公告、`_run_locked` 的默认发送、收尾通知全部按群消息发到好友 QQ，必然失败。
- **修复**：把 `chat_type` 存进 `loop_sessions`，不要在运行期猜。

### M-18 每次重连都重复注册插件 Blueprint

- **位置**：`main.py:170-172`；`ui/web/server.py:434-445`
- **原因**：重连循环里每次 `register_plugin_blueprints(manager)`，Flask 的 url_map 不会去重。
- **后果**：同一路径规则越堆越多；Werkzeug 按注册顺序匹配，**先注册的旧规则优先**。deepseek 用 `_plugin()` 动态取实例躲过了一部分，但第三方插件若在 Blueprint 里闭包了 `self`，重连后访问的是上一个连接留下的旧实例。若插件返回固定名 Blueprint，第二次注册直接抛 `ValueError`，还会触发 H-05。
- **修复**：Blueprint 只在首次启动注册一次，或维护 folder → 当前实例的间接引用。

### M-19 轮换触发 token 时用磁盘配置当全量配置

- **位置**：`plugins/deepseek/Core/web.py:197-200`
- **原因**：`raw = p.get_config()` 读磁盘 `config.json`，然后 `reload_cfg(raw)` 以它为全量重写。同文件另外两个写配置的端点（`:431`、`:470`）都特意用 `dict(getattr(p, '_raw_config', ...))` 规避子集问题。
- **触发**：磁盘正处于 `web.json` 字段子集状态（框架弹窗保存过，或 `load_config` 曾失败）时点击“重新生成 token”。
- **后果**：`llm_providers/api_key` 等未登记键回退默认，之后模型调用鉴权失败。
- **修复**：改成和另外两个端点一致的 `_raw_config` 取值方式。

### M-20 `Future` 在任务取消时不会被清理

- **位置**：`core/bot.py:114-158`、`:263-288`
- **原因**：`send_message`/`call_api` 只捕获 `asyncio.TimeoutError` 和 `Exception`；`CancelledError` 是 `BaseException`，会直接穿透，`pending_requests[echo]` 留在字典里。
- **触发**：插件用 `asyncio.wait_for(bot.send_message(...), 5)`，或热重载取消 handler。
- **后果**：若服务端始终不回该 action，条目会留到整条连接结束；反复取消会持续累积。
- **修复**：用 `try/finally` 无条件 `pop` 并 `cancel()` future。

### M-21 `on_stop` 超时后的残留任务不会被回收

- **位置**：`main.py:283-285`；`utils/plugin_loader.py:982-994`、`:996-1004`
- **原因**：`ashutdown_all_plugins(timeout=10)` 超时后 `finally` 仍把实例标记为已停止；`_bg_stops` 里的 on_stop 兄弟 task 会继续跑。但 `adrain_background()` 只在进程最终退出时对**最新 manager** 调用（`main.py:318-320`）。
- **触发**：deepseek 的 `on_stop`（`mcp.shutdown(8s) + llm.aclose + live.stop`）超过 10 秒。
- **后果**：旧 manager 的任务直到进程结束都没人 drain，并可能与新实例的后台循环并发操作 DB/MCP/HTTP 服务。
- **修复**：每次断开后先 `await manager.adrain_background()` 再重连。

---

## 4. 低优先级 / 边界问题

| ID | 位置 | 问题 | 触发与后果 | 修复 |
|---|---|---|---|---|
| L-01 | `ui/web/server.py:149` | 布尔配置用 `bool(value)` | 脚本/旧前端传 `"false"`、`"0"` 会被当成 True，静默打开 `official_allow_active` 等开关 | 按 `core/official/config.py:_as_bool` 的语义解析 |
| L-02 | `ui/web/server.py:474-478`、`:460` | `stop_web_server()` 只翻转标志；`_running` 在绑定端口前就置 True | 服务线程不停；端口占用后 `_running` 仍为 True，后续永不重试，面板可能永久起不来 | 真正 `stop()` 并 join；启动成功后再置标志 |
| L-03 | `plugins/deepseek/Core/mentions.py:243-262` | `get_group_member_list` 不检查 `retcode/status` | 失败响应没有 `data` 列表时得到空名单，却写入 `_roster`/`_fetched_at`，600 秒内不重试；昵称转真艾特长期失效 | 仅在 `retcode == 0` 且有列表时写缓存 |
| L-04 | `plugins/deepseek/Core/tasks.py:196-198` + `storage.py:2061-2067` | `reminder_on=false` 的到期行 `continue` 但不推进 | 行一直满足 `due_at<=now`，`due_commitments` 按时间升序 `LIMIT 100`；累积 100 条后其他群提醒永远进不了查询窗口 | 查询侧排除这些行，或顺延 `due_at` |
| L-05 | `plugins/deepseek/Core/scheduler.py:269-270` + `storage.py:2139-2145` | `scheduler_on=false` 的任务不推进也不停用 | 同上，`due_jobs LIMIT 50` 被占满后其他群定时任务停摆 | 临时停用/顺延，或查询侧排除 |
| L-06 | `plugins/deepseek/Core/engine.py:312-316`、`loop_run._worker`、`scheduler._fire/_finish_run` | 只 `except Exception`，不处理 `CancelledError` | 插件重载/关停时后台轮被取消，`bg_runs` 永久留下 `status='' / finished_at=0` 的“运行中”记录 | 收尾放 `try/finally`，或显式捕获后 finish 再重抛 |
| L-07 | `plugins/deepseek/Core/web.py:303` | `force = bool(json.get('force'))` | 客户端传字符串 `"false"` 会被当成强制结束 | 解析布尔字符串 |
| L-08 | `plugins/deepseek/Core/web.py:117`、`:322` | `limit` 没有下限 | 负数传给 SQLite（`LIMIT -1` = 不限量），一次请求可能返回全部历史 | `max(1, min(limit, 上限))` |
| L-09 | `plugins/deepseek/Core/web.py:756-768`、`:789-808` | `importance` 非数字、`tags` 非字符串 | `float()`/SQLite 绑定报错 → 500 | 做类型校验并返回 400 |
| L-10 | `plugins/deepseek/Core/storage.py:672-687` | `get_or_create_group` 先 SELECT 再 INSERT | 并发下第二个请求撞 `UNIQUE constraint failed: groups.group_id` | 改用 `INSERT OR IGNORE` |
| L-11 | `plugins/deepseek/Core/history.py:37` | 8 位以上纯数字当 UNIX 时间戳 | `20261125` 解析成 1970 年 | 先按 `YYYYMMDD` 尝试，或要求带分隔符 |
| L-12 | `plugins/deepseek/Core/triggers.py:60` | `_last_fire` 只增不减 | 长期运行按“群 + source”无限积累 | 定期清理过期键 |
| L-13 | `plugins/deepseek/Core/live.py:108-118` | 停服时订阅者队列满会丢弃 `CLOSE` 哨兵 | 对应 SSE 线程可能多活一段时间，热重载后旧连接悬挂 | 停服时清空队列再放哨兵，或直接关闭 server |
| L-14 | `core/bot.py:127` | `increment_sent()` 在响应前调用 | 发送失败也计入“已发送”统计 | 收到成功响应后再计数 |
| L-15 | `utils/plugin_loader.py:204` | requirements 行判断 `line.startswith('#')` 前未去空格 | `  # comment` 会被当成包名传给 pip | 先 `strip()` 再判断 |
| L-16 | `utils/plugin_loader.py:110-131` | 版本元组混入预发布字符串 | 如 `2.0rc1` 与 `>=2.0` 比较触发 `TypeError`，被兜底成“未安装”，每次加载都重装依赖 | 用 `packaging.version` 比较 |
| L-17 | `main.py:30-51` | 每次重连都新起一个后台数据获取线程 | GUI 模式下多次重连会叠加多个线程，各自等待 2 秒后并发拉取 | 用单例/标志位复用线程 |

---

## 5. 误报与已核对项

以下项目在审查中被怀疑过，但确认**不是** bug，避免后续重复排查：

1. **`web.py:274` 只改 persona 会重置 `batch_seconds`**——不是问题。`fields.get('batch_seconds', 20)` 的默认值 20 已经 ≥ 3，只有显式传入 <3 的值才会被抬到 3。
2. **`scheduler._fire` 把私聊任务当群发**——不是问题。`_fire` 用 `job['target_type']` 区分了 `group` / `private`，私聊分支使用 `message_type='private'`。
3. **`raw_after` / `messages_after` 的游标推进**——不是问题。两者都是 `ORDER BY id ASC`，配合 `max(id)` 推进游标是正确的。
4. **`storage` 里的 f-string SQL**——不是问题。拼接的都是代码内固定的列名/条件，用户输入一律走参数绑定。
5. **`history.get_by_id/get_by_offset` 越界**——目前全仓库无调用方，未计入。
6. **`context.plan_summary` 的 `cut` 偏移**——摘要模式下 `cut` 偏大只会把更多消息提前并入摘要，不会丢消息；真正会丢消息的是 tail 模式（见 M-15）。

---

## 6. 建议的修复顺序

1. **H-01**（一行修复，立即恢复“立即总结”）
2. **H-02**（插件启用/禁用与 on_start/on_stop 生命周期，影响整个插件系统）
3. **H-03 + H-04**（一个能卡死进程，一个能丢整条消息）
4. **H-06 + H-07**（私聊被当群：持续发送失败与 token 浪费）
5. **H-05 + M-13 + M-21**（重连/关停生命周期，避免实例与后台任务泄漏）
6. **M-05 + M-06 + M-07**（对话主链路的发送记账与长跑稳定性）
7. 其余按 M → L 顺序处理。

## 7. 回归验证

修复后至少执行：

```bash
python -m pytest tests/ -q
```

并手工覆盖以下路径：

- 群管理页“立即总结”；
- 面板禁用 → 启用插件后发一条消息；
- 设置页把 `split_max_len` 设为 0 再改回（确认不再卡死）；
- 私聊对话、私聊内创建带 `due_date` 的约定；
- 断网/重启 NapCat 触发重连，确认没有重复的定时任务、Live 订阅或触发器；
- official 关池模式下名额耗尽时，确认历史里不会出现未发出的 assistant 消息。

---

*本报告由静态审查产生，未修改任何代码。行号基于审查时工作区，修复时请以函数名和代码片段定位。*
