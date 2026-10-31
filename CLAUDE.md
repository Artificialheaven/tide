# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt

python main.py          # GUI 模式（DearPyGui，asyncio 主循环跑在子线程）
python main.py -nogui   # 无头模式（纯控制台日志）

python -m pytest tests/                      # 全量测试
python -m pytest tests/test_loops.py         # 单个文件
python -m pytest tests/test_loops.py::test_break   # 单个用例
```

没有配置 linter / formatter。运行配置在被 gitignore 的 `config.json`（缺失时 `configs.py` 自动生成默认值）。

## 架构总览

OneBot v11 QQ 机器人框架：一个 asyncio WebSocket **客户端**，外加插件系统和两套管理界面（DearPyGui 桌面端、Flask :8081 Web 面板，二者互斥，由 `-nogui` 和 `enable_web_admin` 决定）。

### 启动、重连与线程模型（main.py / core/）

- `connect_and_run` 是一个**重连外循环**：每次连接都重新创建 `bot` 实例、调用 `init_plugin_manager` 重新加载全部插件；断开时先 `await ashutdown_all_plugins()`（触发各插件 `on_stop`），再指数退避重连。写插件生命周期代码时必须假设 on_start/on_stop 会反复执行。
- 入站消息先按 `echo` 匹配 `bot.pending_requests` 里的 Future（OneBot 请求/响应 RPC，30s 超时），匹配不上才交给 `events.parse_event`。
- `events.parse_event` 把 dict 包成结构化事件（`utils/obj.py` 的 MessageEvent / NoticeEvent / MetaEvent），遍历插件处理器：async handler 建 task，sync handler 丢默认 executor。因此**阻塞式同步代码只能出现在 executor 线程或自建线程里，绝不能在 async handler 中直接迭代阻塞生成器**。
- `core/globals.py` 是进程级单例（`event_loop` / `bot_instance` / `websocket`），跨线程调度依赖它。
- GUI 模式下 asyncio 循环运行在子线程，UI 回调在主线程——跨线程一律经 `globals.event_loop`。

### 插件系统（utils/Plugin.py, utils/plugin_loader.py）

约定（不是配置）：

- 插件 = `plugins/<name>/<name>.py`，模块内必须有 `class main(Plugin)`；按 `plugins.<name>.<name>` 导入。
- 处理器靠装饰器收集：`@Plugin.on_message("message")` / `@Plugin.on_notice("notice")`，装饰器在函数对象上打 `_plugin_event` 标记，`Plugin.__init__` 反射收集；`on_start`/`on_stop` 可以是同步或 async（框架两种都支持，线程外经 `run_coroutine_threadsafe` 调度）。
- 第三方依赖写在插件自己的 `requirements.txt`，加载时 `pip install --target plugins/<name>/lib`，该目录动态插入/移出 `sys.path`（卸载时必须清理，见 `remove_plugin_lib_from_path`）。
- `plugins/<name>/config.json` 是运行期配置（**已 gitignore**，用 `self.get_config()/save_config()`，不要硬编码）；`web.json` 是 Web 面板的配置表单 schema，改配置走 `call_function("load_config", ...)` RPC。
- `get_web_blueprint()` 返回的 Flask Blueprint 会自动挂到 `/<插件文件夹名>/` 并套登录鉴权。
- 热重载：`reload_plugin` 在 `_reload_lock` 内、并持有解释器 import 锁，完成「卸载 → 清除插件目录下所有 sys.modules（含 Core.*、api.* 子模块）→ 清 __pycache__ → 重新导入」。模块级单例（如 Slang 的 register）热重载后会重建，不要在插件外缓存对它们的引用。
- 插件间互不知道彼此；需要调 OneBot API 就用注入的 `self.bot`（实际是带插件名注入的 wrapper）。

### Slang 插件（plugins/slang/，框架里最大的插件）

中文关键字文本 DSL：文本中的 `【函数名 参数1>=<参数2>=<...】` 块被解析成 AST（`Core/ast.py`，TextNode/FunctionNode，按已知函数名最长匹配），嵌套块先求值为字符串再当参数。

- **控制流不在函数表里执行**：`判断 / 比较- / 循环- / 循环 / Fori` 通过 `reg_not_callable` 注册、由 AST 求值器直接处理（比较语义、循环上限 10 万次等都在 ast.py）。普通函数才走 `register.call`。
- 函数注册是装饰器路由：各 `api/*.py` 定义自己的 `Router`，`regs.py` 用 `set_router` 合并，**先注册者胜出**（同名函数不会被后加载的覆盖）。注册函数签名统一为 `fn(parma: list, _dict: dict)`（`parma` 是项目里既有的拼写），`_dict` 是本次执行的消息上下文变量。
- 新增 DSL 函数：在合适的 `api/<域>.py` 里 `@router.reg('中文名', 参数数, '说明')`，并在 `regs.py` 挂上新 router。
- 求值语义的关键陷阱：`Slang.run_room` 吞掉所有异常返回 `''`；但 AST 层对单个函数调用也做了异常隔离（一个坏参数只让该函数变空，外围文本保留）。**直接 `register.call(...)` 不经隔离、会抛异常**（测试依赖这一点）。
- DSL 规则在有界守护线程池（`Core/thread_pool.py` 的 DaemonThreadPoolExecutor）中执行，不能阻塞事件循环；DSL 内要调 OneBot API 时走 `core/sync_bridge.py`（`run_coroutine_threadsafe` 桥到主循环，前提是 bot 在线）。
- 正则一律走 `Core/safe_re.py`（长度限制 + 2 秒超时 + 隔离小线程池，超时后池子会被整体替换）；不要在 api 函数里直接用裸 `re` 处理来自消息的 pattern。
- 规则与积分数据在 `plugins/slang/db/rules.db`（SQLite WAL，`Core/storage.py` 用 PRAGMA user_version 做迁移，改表必须写迁移）；Web 规则编辑器是该插件的 Flask Blueprint（`templates/rules.html`）。
- DSL 完整参考见 `docs/slang/slang-guide.md`；插件开发见 `docs/plugin-guide.md`；OneBot action 见 `docs/api-reference.md`。

### AI 插件（plugins/ai/）

- 每个 QQ 一个 `ConversationManager` + 一个 SQLite 库（`db/<qq>.db`）；OpenAI SDK 是**同步**的，流式 `chat_stream` 是同步生成器。`ai.py` 用「asyncio.Queue + 守护泵线程」桥接成 `async for`，改这块时不要退化成同步迭代（会冻住事件循环）。
- 自带 :5000 的对话展示 Web 服务（Flask threading 模式，无鉴权，绑定地址可在 config 的 `web_host/web_port` 改）；Werkzeug 线程无法可靠关闭，实例挂在 `sys._ai_plugin_web_server` 跨热重载复用。
- API key 只从 `config.json` 或环境变量 `SILICONFLOW_API_KEY` 读取；灰度 QQ 用 config 的 `allowed_users` 或 `AI_ALLOWED_QQS`。

## 测试约定

- `tests/conftest.py` 把仓库根和 `plugins/slang` 注入 `sys.path`，必须从仓库根运行。核心 fixtures：`call(name, *args)` 直接调 DSL 函数、`run(code)` 执行整段 Slang 文本、`ctx`/`db_path` 提供隔离的临时 SQLite。
- 测 DSL 行为优先走 `run(...)`（与线上同路径，含异常隔离）；测单个函数的抛错行为才用 `call(...)`。
- 涉及正则超时的测试用 `isolated_re_pool` fixture 换独立线程池，且灾难性回溯样本要短（约 1 秒自熄），worker 是守护线程，不能让卡死任务污染全局池或拖住 pytest 退出。
