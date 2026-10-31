# 插件编写教程

## 目录

1. [插件结构](#插件结构)
2. [最小示例](#最小示例)
3. [生命周期](#生命周期)
4. [消息处理](#消息处理)
5. [通知事件](#通知事件)
6. [Bot API](#bot-api)
7. [日志记录](#日志记录)
8. [Web 配置](#web-配置)
9. [热重载](#热重载)
10. [完整示例](#完整示例)

---

## 插件结构

每个插件是一个 `plugins/` 下的独立文件夹：

```
plugins/mine/
├── __init__.py          # Python 包标记（自动生成）
├── mine.py              # 主入口（文件名 = 文件夹名，必需）
├── requirements.txt     # 依赖声明（可选）
├── web.json             # Web 面板配置模式（可选）
└── lib/                 # 独立依赖目录（自动安装）
```

## 最小示例

```python
# plugins/mine/mine.py
import websockets
from utils.Plugin import Plugin

class main(Plugin):
    name = "我的插件"
    description = "一个示例插件"
    version = "1.0.0"

    def __init__(self, websocket: websockets.ClientConnection, bot_instance=None):
        super().__init__(websocket, bot_instance)

    @Plugin.on_message("message")
    async def on_msg(self, event):
        """收到消息时触发"""
        if event.raw_message.strip() == "ping":
            await self.bot.send_message(
                message="pong!",
                target_id=event.user_id,
                message_type=event.message_type
            )
```

**要点**：
- 类名必须是 `main`，继承 `Plugin`
- 文件名 = 文件夹名
- `@Plugin.on_message("message")` 装饰器注册消息处理器
- `self.bot.send_message(...)` 发送消息

## 生命周期

```python
class main(Plugin):
    def __init__(self, websocket, bot_instance=None):
        super().__init__(websocket, bot_instance)
        # ✅ 在这里初始化插件状态
        # ⚠️ bot_instance 可能为 None（插件先于 Bot 加载时）

    async def on_start(self):
        """首次加载 / 热重载后自动调用"""
        if not self.bot:
            return
        info = await self.bot.get_login_info()
        groups = await self.bot.get_group_list()
```

## 消息处理

处理器接收 `MessageEvent` 结构化对象：

```python
@Plugin.on_message("message")
async def handle(self, event):
    # event.message_type → "group" / "private"
    # event.raw_message  → 纯文本消息
    # event.user_id      → 发送者 QQ
    # event.group_id     → 群号（群聊时）
    # event.sender       → sender 对象（.nickname, .user_id）

    if event.message_type == "group":
        await self.bot.send_message(
            message="收到群消息",
            target_id=event.group_id,
            message_type="group"
        )
    elif event.message_type == "private":
        await self.bot.send_message(
            message="收到私聊",
            target_id=event.user_id,
            message_type="private"
        )
```

## 通知事件

使用 `@Plugin.on_notice` 处理群通知：

```python
@Plugin.on_notice("notice")
async def on_notice(self, notice):
    # notice.notice_type → "group_increase" / "group_decrease" / "group_ban" ...
    # notice.group_id   → 群号
    # notice.user_id    → 被操作者

    if notice.notice_type == "group_increase":
        await self.bot.send_message(
            message=f"欢迎 {notice.user_id} 入群！",
            target_id=notice.group_id,
            message_type="group"
        )
```

支持的 `notice_type`：`group_increase`、`group_decrease`、`group_ban`、`group_admin`、`group_upload`、`friend_add` 等（完整列表见 OneBot 协议文档）。

## 请求事件（好友申请 / 加群申请 / 群邀请）

`post_type == "request"` 的事件用 `@Plugin.on_request` 处理，事件对象是 `utils.obj.RequestEvent`：

| 字段 | 说明 |
|------|------|
| `event.request_type` | `"friend"`（好友申请）或 `"group"`（加群相关） |
| `event.sub_type` | group 时为 `"add"`（别人申请入群）或 `"invite"`（有人拉机器人进群）；friend 时为 `""` |
| `event.user_id` | 申请人 / 邀请人 QQ |
| `event.group_id` | 群号；好友申请时为 `None` |
| `event.comment` | 申请附言 |
| `event.flag` | 审批凭据，**原样回传**给审批 API，有时效 |

```python
@Plugin.on_request("request")
async def on_request(self, event):
    if event.request_type == "friend":
        # 审批：flag 原样传；approve=True 时可带 remark（加好友后的备注名）
        await self.bot.call_api("set_friend_add_request",
                                {"flag": event.flag, "approve": True})
    elif event.request_type == "group" and event.sub_type == "invite":
        # 群邀请/加群申请同一个 API；拒绝时可带 reason；不需要传 sub_type
        await self.bot.call_api("set_group_add_request",
                                {"flag": event.flag, "approve": False,
                                 "reason": "不认识"})
```

注意：

- 正向 WebSocket 不能用 OneBot 的快速操作（`.handle_quick_operation`），只能显式调上面两个 API。
- `flag` 会过期；平台返回非 0 `retcode` 就是没处理成，不要自动重试，提醒主人即可。
- 没有插件监听 request 事件时，事件被安全忽略（不报错）。

## Bot API

通过 `self.bot` 调用，所有方法均为 `async`：

| 方法 | 参数 | 说明 |
|------|------|------|
| `send_message(message, target_id, message_type)` | message, target_id, type="private"/"group" | 发送消息 |
| `get_login_info()` | — | 获取 Bot 登录信息 |
| `get_group_list(no_cache=False)` | no_cache | 获取群列表 |
| `get_friend_list()` | — | 获取好友列表 |
| `get_group_info(group_id, no_cache=False)` | group_id | 获取群信息 |
| `get_group_member_list(group_id, no_cache=False)` | group_id | 获取群成员 |
| `get_group_member_info(group_id, user_id)` | group_id, user_id | 获取群成员信息 |
| `call_api(action, params)` | action, params | 通用 API 调用 |

```python
# 示例
info = await self.bot.get_login_info()
groups = await self.bot.get_group_list(no_cache=True)
await self.bot.send_message("你好", target_id=123456, message_type="private")
```

## 日志记录

Logger 自动注入插件名作为来源：

```python
self.bot.Logger.info("框架", "任意", "普通日志")    # 白色
self.bot.Logger.warning("框架", "任意", "警告")    # 黄色
self.bot.Logger.error("框架", "任意", "错误")      # 红色
self.bot.Logger.custom("框架", "任意", "消息", "蓝色")  # 自定义颜色
```

## Web 配置

创建 `web.json` 声明配置模式，Web 管理面板自动生成表单：

```json
{
    "config": {
        "api_key": {
            "type": "string",
            "label": "API Key",
            "default": ""
        },
        "max_tokens": {
            "type": "int",
            "label": "最大 Token",
            "default": 2048
        },
        "enable_cache": {
            "type": "checkbox",
            "label": "启用缓存",
            "default": true
        },
        "model": {
            "type": "dropdown",
            "label": "模型",
            "default": "gpt-4",
            "options": ["gpt-4", "gpt-3.5", "claude-3"]
        },
        "mode": {
            "type": "radiobutton",
            "label": "模式",
            "default": "chat",
            "options": ["chat", "complete"]
        }
    },
    "call_function": "load_config"
}
```

插件中实现配置接收：

```python
def call_function(self, name: str, **kwargs):
    if name == "load_config":
        config = kwargs.get("config", {})
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "")
        return True
    return super().call_function(name, **kwargs)
```

支持的字段类型：

| type | 渲染 | 需 options |
|------|------|-----------|
| `string` | 文本输入框 | 否 |
| `int` | 数字输入框 | 否 |
| `checkbox` | 复选框 | 否 |
| `radiobutton` | 单选按钮组 | 是 |
| `dropdown` | 下拉框 | 是 |

## 热重载

插件修改后无需重启整个框架：

- **Web 面板**：插件页 → 点击 🔄 重载
- **代码调用**：`from utils.plugin_loader import reload_plugin; reload_plugin("插件文件夹名")`

热重载流程：卸载旧实例 → 清除模块缓存 → 重新导入 → 调用 `on_start()`。

## 完整示例

参见 `plugins/hello/hello.py`，涵盖了：
- `on_message` 消息处理（多命令）
- `on_notice` 通知处理（入群欢迎）
- `call_function` Web 配置热加载
- `on_start` 启动生命周期
- Logger 日志
- Bot API 调用（群聊/私聊发送、查询信息）
