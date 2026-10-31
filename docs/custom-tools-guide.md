# 自定义工具（AI 自写工具）开发须知

DeepSeek 插件可以让 AI 把重复性能力沉淀成**自定义工具**：用 `custom_tool_validate`
dry-run 试写、`custom_tool_publish` 上线，之后每轮对话都能直接调用。

## 安全模型（先读）

- validate / publish / toggle / remove 都是**风险操作，只有主人 QQ 能指令**；
- 工具代码**永远不在机器人主进程里执行**：每次调用都在一个短生子进程
  （`python -I` 隔离模式）里运行，崩溃、`sys.exit`、死循环只死那个子进程，
  超时会被 `taskkill /T` 杀整棵进程树，不影响机器人和其他工具；
- 发布是「子进程加载验证通过 → 原子写库」。验证失败时库里的旧版本原样保留，
  不存在"上线了一个坏工具导致全挂"的中间态；
- 默认本群可见；主人发布时 `global=true` 才对所有群/私聊生效；
- 已上线工具默认群内任何人的对话都可能间接触发；不想这样就
  `owner_only_call=true`，只有主人能触发。

## 代码契约

```python
# 只有一个硬性要求：定义可调用的 tool
def tool(args: dict, ctx: dict) -> str:
    """
    args  模型按你声明的 params_schema 传入的参数
    ctx   {"group_id": "...", "workspace": "<本群沙箱绝对路径>",
           "now": "2026-10-25 10:00:00", "dry_run": false}
    返回   str（非 str 会被自动转换：dict/list 走 JSON，其余 str()）
    """
    q = args.get("q", "")
    return f"你好，{q}"
```

规则：

1. 加载期（模块级代码）不应有副作用、不应 print——每次调用都重新 exec；
2. 想看结果就 `print()`，stdout 会附加在工具结果里；
3. 第三方包只能用插件 `lib/` 里已安装的（标准库随便用）；
4. 每次调用是全新进程，**不要依赖模块级变量保存状态**——要持久化就写文件
   （`ctx["workspace"]` 下）或让调用方把状态放在参数里；
5. 拿不到 bot 实例：工具不能自己发 QQ 消息，要发消息让模型用内置 qq_send_text。

## 发布流程（AI 操作）

1. `custom_tool_validate(name, description, params_schema, code, test_args?)`
   - 静态检查（命名 `^[a-z][a-z0-9_]{0,31}$`、JSON Schema、语法）在主进程做；
   - 加载 + 可选样例试跑在子进程做（默认 10s 超时）；
   - **不落库、不上线**，可以反复试。
2. 报告显示 ✅ 后，`custom_tool_publish(...)` 再验一次加载，通过才原子上线；
   同名重发即升级，失败自动保留旧版。
3. 上线后工具名是 `custom__<name>`，模型按 schema 直接调用。
4. `custom_tool_list / custom_tool_toggle / custom_tool_remove` 管理。

## params_schema 示例

```json
{
  "type": "object",
  "properties": {
    "q": {"type": "string", "description": "查询词"},
    "n": {"type": "integer", "description": "条数，默认3"}
  },
  "required": ["q"]
}
```

## 最小完整示例

代码（code 参数）：

```python
import json, urllib.parse, urllib.request

def tool(args, ctx):
    q = urllib.parse.quote(args.get("q", ""))
    url = f"https://httpbin.org/get?q={q}"
    with urllib.request.urlopen(url, timeout=8) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["args"]["q"]
```

`description` 写清「什么时候该调用」，例如：*查询某个词的回显，用于演示
自定义工具的网络能力。*

---

# 初筛脚本（多判官第一层）

除了自定义工具，AI 还能沉淀**初筛脚本**：决定一条没直接 @ 机器人、没回复机器人的
群消息，要不要进一步花钱拉起 LLM 判官。触发链路是：

```
群消息 → @我/回复我？──是──→ 直接进对话
                  │否
                  └→ 内置启发式粗筛 ──命中─→ 判官
                         │未命中
                         └→ 逐个跑启用中的初筛脚本（并发）
                                └任一返回 True─→ 判官 ──判是──→ 进对话
                                └全部 False/报错─→ 本条忽略
```

即启发式与所有初筛脚本是**「或」**：任意一个放行才调判官；判官判否仍然不回复。
强制触发（@、回复）跳过全部闸门。

## 与自定义工具相同的安全模型

- `screen_validate / screen_publish / screen_toggle / screen_remove`
  同样是**主人专属风险操作**（`screen_list` 人人可问）；
- 代码只在 `python -I` 短生子进程里 exec，超时默认 **5 秒**杀整棵进程树，
  崩溃/死循环不影响主进程；发布前必须 dry-run 加载验证通过，坏代码旧版保留；
- 分本群 / 全局（`global=true`）两个作用域，每作用域默认上限 10 个；
- 脚本异常、超时、返回值无法识别一律**按「未命中」处理（fail-closed）**，
  不会因为脚本故障放过所有消息，也不会跳过其他脚本。

## 代码契约（三个参数，不是 args/ctx）

```python
def screen(group_id: str, user_id: str, message_raw: str) -> bool:
    # group_id    群号（字符串）
    # user_id     发言人 QQ（字符串）
    # message_raw 本条消息纯文本（已剥掉 CQ 码；@ 变成 @昵称）
    # 返回 True = 这条值得拉起判官；False = 交给下一道初筛
```

可选第 4 参 `ctx`（声明了才传）：`{"group_id", "now", "dry_run"}`。

注意返回值要直接给布尔值；字符串 `"false"` 在 Python 里恒真，框架会把
`"true"/"false"/"1"/"0"/"是"/"否"` 等归一化，但不要依赖它。

## 编写要点

1. **要便宜**：每条未触发消息都会跑全部启用脚本。只做字符串/正则/QQ 号判断，
   不要发网络请求、不要读写文件、不要重计算（默认 5s 超时）；
2. **宁可漏判，不要刷屏**：脚本只负责「这条也许值得理」，模糊语义交给判官；
3. 第三方包只能用插件 `lib/` 里已有的，标准库随便用；
4. 每次执行都是全新进程，不要靠模块级变量存状态。

## 发布流程

1. `screen_validate`（给 `test_message` 或 `test_args` 真跑一次样例）；
2. `screen_publish` 上线（默认本群；`global=true` 全群生效，仅主人）；
3. `screen_list` 看编号/作用域/启停/累计命中次数与最近命中时间；
4. `screen_toggle` 临时停用、`screen_remove` 删除；同名重新发布即升级，
   历史命中计数保留、失败自动回滚旧版。

群设置里有「AI 初筛脚本」总开关（`screen_on`，可每群关闭）；
相关配置项：`screen_max_per_scope`、`screen_code_max_chars`、
`screen_call_timeout`、`screen_validate_timeout`。
