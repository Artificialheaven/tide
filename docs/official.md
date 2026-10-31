# QQ 官方机器人接入（official transport）

框架默认通过 NapCat（OneBot v11 WebSocket 客户端）连 QQ。设置 `transport=official`
后，`core/official/` 会以 **QQ 开放平台 v2 群机器人** 身份直连官方网关，并把官方事件/
接口翻译成 NapCat 给插件的那套 OneBot v11 字段。`core/bot.py`、事件分发和所有插件
都不需要改。

```
main.py
  └─ transport == "official"
        └─ core/official/
              config.py      配置解析（env 优先）
              api.py         Token 管理 + REST 调用
              gateway.py     WS 网关：hello / identify / heartbeat / resume
              convert_in.py  官方事件 → OneBot v11 事件
              convert_out.py OneBot 消息 → 官方发送项
              passive.py     按群的被动回复 msg_id 池
              session.py     recv 队列 + action 分发
              connection.py  对 main.py 暴露的适配器
```

## 启用

1. 在 [QQ 开放平台](https://q.qq.com/) 创建群机器人，拿到 AppID / AppSecret。
2. 配置（`config.json` 已 gitignore，不会进仓库）：

   ```json
   {
     "transport": "official",
     "official_app_id": "123456",
     "official_app_secret": "***",
     "official_sandbox": false
   }
   ```

   环境变量 `QQBOT_APP_ID` / `QQBOT_APP_SECRET` 优先于配置文件，适合部署时注入。

   GUI 的「框架配置」窗口和 `-nogui` 模式的 Web 面板（`:8081`）都能改；Web 面板
   回传的 `official_app_secret` 是掩码，原样保存不会覆盖真实密钥。

3. 启动：

   ```bash
   python main.py          # GUI
   python main.py -nogui   # 无头
   ```

4. 把机器人拉进群，@ 它说话即可。官方群机器人**只会收到 @ 自己的消息**。

## 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `transport` | `napcat` | `napcat` 或 `official`，单进程只跑一种 |
| `official_app_id` / `official_app_secret` | `""` | 开放平台凭据；env `QQBOT_APP_ID` / `QQBOT_APP_SECRET` 优先 |
| `official_sandbox` | `false` | 使用 `sandbox.api.sgroup.qq.com` |
| `official_self_id` | `""` | 空则用 AppID 作为 OneBot `self_id` |
| `official_bot_name` | `QQ官方机器人` | `get_login_info` 拿不到昵称时的降级名 |
| `official_allow_active` | `false` | 被动名额耗尽后是否允许发主动消息 |
| `official_at_all_mode` | `native` | `[CQ:at,qq=all]`：`native` 用 `<qqbot-at-everyone/>`，`text` 降级为文本，`drop` 丢弃 |
| `official_passive_window_seconds` | `295` | 被动回复有效期（官方约 5 分钟） |
| `official_max_passive_seq` | `5` | 每个 `msg_id` 的最大被动回复条数 |
| `official_passive_pool_size` | `64` | 每群保留的入站 `msg_id` 槽位数 |
| `official_max_upload_bytes` | `5242880` | 本地/base64 媒体上传上限 |
| `official_member_cache_ttl` | `600` | 群/成员信息缓存秒数 |
| `official_member_failed_retry` | `300` | 拉取失败后的重试间隔 |
| `official_enable_member_enrich` | `true` | 是否调用群/成员接口补全昵称、群名 |
| `official_request_timeout` | `20` | 单次 REST 请求超时 |
| `official_ws_reconnect_base_delay` / `official_ws_reconnect_max_delay` | `2` / `60` | 网关重连退避 |
| `official_intents` | `33554432` | 默认 `1<<25`：群内 @ 消息事件 |

## 入站映射

| OneBot 字段 | 官方来源 |
|---|---|
| `self_id` | `official_self_id` 或 AppID（字符串） |
| `group_id` | `group_openid`（字符串） |
| `user_id` | `author.member_openid`（字符串） |
| `message_id` | `id` |
| `time` | `timestamp`，解析失败用当前时间 |
| `sender.nickname` | 成员缓存，缺失时 `用户(<openid 后 6 位>)` |
| `group_name` | 群信息缓存，缺失时 `QQ群(<openid 后 6 位>)` |
| `message` | `content` 中的 `<qqbot-at-user>` / `<qqbot-at-everyone>` → `at` 段；`attachments` → image/record/video/file 段；`message_reference` → `reply` 段 |
| `raw_message` | 由段数组合成的 CQ 串 |

补充：

- 转换后若没有指向机器人自身的 `at` 段，会在开头强制注入一个，保证插件里基于
  `at_me` / 「仅 @ 触发」的逻辑成立。
- 原始官方 payload 放在 `event["_official"]`，`_raw` 仍是 OneBot 形状。
- `GROUP_ADD_ROBOT` 会合成 `notice_type="group_increase"`（`user_id=self_id`），
  用于入群问候；其余官方 notice 不伪造。
- 私聊（`C2C_MESSAGE_CREATE`）、频道、好友事件不处理。

## 出站映射

`send_msg` 只支持 `group_id`，消息可以是 CQ 字符串或段数组：

- 文本直接拼进 `content`；`[CQ:at]` → `<qqbot-at-user id="..."/>`。
- 图片/语音/视频/文件：先 `POST /v2/groups/{gid}/files` 上传，再用 `msg_type=7`
  发送。http(s) URL 直接交给官方拉取；`base64://` 和本地文件走 `file_data`。
- 文本与媒体会被拆成多条官方消息，每条消耗一个被动回复名额。
- 表情/卡片/合并转发等转成文本占位或丢弃，不会让整条消息失败。

### 被动回复池

官方限制是「每条入站消息最多 **5 条**被动回复，约 5 分钟有效」。适配层按群维护一个
近期入站 `msg_id` 池：

1. 每条 `GROUP_AT_MESSAGE_CREATE` 入池（去重，按过期时间清理，每群上限
   `official_passive_pool_size`）。
2. 发送时优先使用 CQ 里 `[CQ:reply,id=X]` 指定的消息；否则挑**最早到期且未用完**
   的槽位（FIFO），先把快过期的名额用掉。
3. 槽位内 `msg_seq` 从 1 递增到 `official_max_passive_seq`；用完后自动换池内下一条
   消息，所以群里有近期发言时，单轮回复可以明显超过 5 条。
4. 同群发送在同一把 `asyncio.Lock` 内完成，避免并发抢同一个 `msg_seq`；跨群绝不
   混用槽位。
5. 池耗尽默认返回 `failed` 并带 `data.sent_count`（已发条数）；开启
   `official_allow_active=true` 后改用主动消息继续发。

安静群（近期只有本次触发的那一条 @）仍会撞到 5 条上限——这是官方能力边界。

## 支持的 action

| 类别 | action |
|---|---|
| 查询 | `get_login_info`、`get_group_info`、`get_group_list`、`get_group_member_info`、`get_group_member_list`、`get_version_info`、`get_status`、`can_send_image`、`can_send_record` |
| 消息 | `send_msg` / `send_group_msg`、`delete_msg`、`get_msg`、`get_image`、`get_record`、`get_file` |
| 不支持 | 私聊、好友、群管理（踢人/禁言/名片/管理员/头衔）、群文件列表、合并转发、戳一戳、点赞、加好友请求等 |

不支持的操作返回统一失败 envelope：

```json
{"status": "failed", "retcode": -1, "data": null,
 "message": "official 模式不支持 xxx", "wording": "...", "echo": ...}
```

查询类响应会把关键字段同时平铺到顶层（`user_id`、`group_name` 等），兼容 Slang
读顶层字段的习惯。

## 与插件的关系

- **deepseek**：群 @ 问答、文本/图片/语音/视频/文件入站、引用回复、分段回复可用；
  QZone 相关功能不可用。
- **slang**：文本规则可用；依赖数字 QQ 号的功能（成员精确匹配、群管理命令）会
  退化——openid 是非数字字符串，适配层不会伪造成数字。
- 定时/主动推送：默认关闭，且受官方主动消息配额限制。
- 网关断线由适配层内部 Resume/重连，**不会**触发插件重启；只有首次连接失败、
  凭据错误等不可恢复错误才会走 `main.py` 的外层重连。

## 排查

- 连不上：确认 AppID/AppSecret 正确，`official_sandbox` 与实际环境一致；日志里
  的 `trace_id` 可拿到开放平台对照。
- 收不到消息：确认机器人已在群里，且消息是 **@ 机器人** 发的；`official_intents`
  是否被改错。
- 发送失败：看 envelope 的 `message`。被动名额耗尽会带 `data.sent_count`；媒体
  超限（默认 5MB）会直接失败。
- 名字显示成 `用户(abcdef)`：成员/群信息接口失败或缓存未建立，通常是权限或频率
  限制，稍后会自动重试。

## 安全

- `official_app_secret` 不会出现在日志、异常信息和 dataclass repr 中。
- Web 面板 GET 返回掩码；回传掩码不会覆盖真实值。
- 不要把 `config.json`、`plugins/*/config.json`、`*.db` 提交进仓库。