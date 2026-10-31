# Bot API 参考

通过 `self.bot` 调用，所有方法为 `async`，返回 OneBot 协议响应。

## 消息

### send_message

```python
await self.bot.send_message(
    message="消息内容",
    target_id=123456789,      # 目标 QQ 或群号
    message_type="private"    # "private" 私聊 / "group" 群聊
)
```

| 参数 | 类型 | 说明 |
|------|------|------|
| `message` | str | 消息内容 |
| `target_id` | int | 目标 ID（私聊为 QQ 号，群聊为群号） |
| `message_type` | str | `"private"` 或 `"group"` |

返回：OneBot `send_msg` API 响应。

---

## 账号信息

### get_login_info

```python
info = await self.bot.get_login_info()
# info["data"]["nickname"] → 昵称
# info["data"]["user_id"]  → QQ 号
```

### bot_qq（属性）

```python
qq = self.bot.bot_qq  # → "123456789"
```

---

## 群相关

### get_group_list

```python
groups = await self.bot.get_group_list(no_cache=False)
# groups["data"] → [{"group_id": ..., "group_name": ...}, ...]
```

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `no_cache` | bool | `False` | 是否绕过缓存 |

### get_group_info

```python
info = await self.bot.get_group_info(group_id=123456, no_cache=False)
```

### get_group_member_list

```python
members = await self.bot.get_group_member_list(group_id=123456)
# members["data"] → [{"user_id": ..., "nickname": ..., "role": ...}, ...]
```

### get_group_member_info

```python
member = await self.bot.get_group_member_info(group_id=123456, user_id=789)
```

---

## 好友

### get_friend_list

```python
friends = await self.bot.get_friend_list()
# friends["data"] → [{"user_id": ..., "nickname": ..., "remark": ...}, ...]
```

---

## 通用 API

### call_api

直接调用 OneBot 协议中的任意 API：

```python
resp = await self.bot.call_api("get_stranger_info", {"user_id": 123456})
```

| 参数 | 类型 | 说明 |
|------|------|------|
| `action` | str | OneBot API 动作名 |
| `params` | dict | 参数 |

### call_api_parallel

并行调用多个 API：

```python
results = await self.bot.call_api_parallel([
    {"action": "get_group_list", "params": {}},
    {"action": "get_friend_list", "params": {}},
])
```

### call_api_batch

对同一 API 批量调用不同参数：

```python
results = await self.bot.call_api_batch(
    "get_group_member_info",
    [{"group_id": 123, "user_id": 1}, {"group_id": 123, "user_id": 2}]
)
```

---

## 消息统计

```python
stats = self.bot.get_stats()
# {"sent": 42, "received": 128}
```

---

## 事件对象

### MessageEvent

| 属性 | 类型 | 说明 |
|------|------|------|
| `message_type` | str | `"group"` / `"private"` |
| `sub_type` | str | `"friend"` / `"normal"` / ... |
| `message_id` | int | 消息 ID |
| `user_id` | int | 发送者 QQ |
| `group_id` | int\|None | 群号 |
| `raw_message` | str | 纯文本消息 |
| `message` | str | 结构化消息（含 CQ 码） |
| `font` | int | 字体 |
| `sender` | sender | 发送者对象 |
| `_plugin_name` | str | 插件名（框架注入） |

### sender

| 属性 | 类型 | 说明 |
|------|------|------|
| `user_id` | int | QQ 号 |
| `nickname` | str | 昵称 |

### NoticeEvent

| 属性 | 类型 | 说明 |
|------|------|------|
| `notice_type` | str | `group_increase` / `group_decrease` / ... |
| `sub_type` | str | 子类型 |
| `group_id` | int\|None | 群号 |
| `user_id` | int | 被操作者 |
| `operator_id` | int | 操作者 |

### MetaEvent

| 属性 | 类型 | 说明 |
|------|------|------|
| `meta_event_type` | str | `lifecycle` / `heartbeat` |
| `sub_type` | str | `connect` / ... |

所有事件对象均保留 `_raw` 属性——原始 JSON dict，用于访问 OneBot 协议中未被结构化的字段。
