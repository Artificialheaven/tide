# 好友申请 / 群邀请审批指南

机器人收到**好友申请**或**被人拉进群的邀请**时，AI 不会自行决定，而是：

1. 申请落库暂存（`pending_requests` 表，DB v15 起）；
2. best-effort 补一下对方昵称（群邀请补群名），然后**私聊通知主人**，通知里带申请编号；
3. 主人在对话里拍板，AI 经风险工具执行同意 / 拒绝。

```
NapCat 推送 request 事件
   │  框架 core/events.py 新增 request 分支
   ▼
@Plugin.on_request → RequestManager.handle()
   ├─ 好友申请 / sub_type=invite 的群邀请：落库 + 私聊主人
   └─ sub_type=add（别人申请入群）：直接忽略
   ▼
主人私聊/群里对她说「同意申请#1」
   ▼
request_approve / request_reject（风险工具，仅主人 QQ 可指令）
   ├─ set_friend_add_request {flag, approve, remark?}
   └─ set_group_add_request  {flag, approve, reason?}
```

## 主人怎么用

机器人私聊会发来这样的通知：

```
[申请审批] 收到一条好友申请（申请#1）
发起人：123456（小明）
附言：我是群友
我不会自行决定。要批准/拒绝，直接对我说「同意申请#1」或「拒绝申请#1」（拒绝可附理由）。
```

直接对机器人说即可（私聊、群里都行，**只认主人 QQ 号**，不认昵称）：

- 「同意申请#1」——加好友；也可以让她加备注，如「同意申请#1，备注老王」。
- 「拒绝申请#1，理由不认识」——群邀请的理由会带给平台；好友拒绝没有备注/理由概念，附言会被忽略。
- 不确定编号时让她「看看待批申请」，她会调 `request_list` 列出最近的待处理申请。

## 行为约定

- **只处理两类**：好友申请；别人**拉机器人进群**的邀请（`sub_type=invite`）。别人申请加群（`sub_type=add`）忽略——那是群管的事，机器人不替群管批人。
- **flag 去重**：NapCat 可能重复投递同一事件，按 flag 去重，重复事件只落库、不重复通知。
- **flag 有时效**：同意/拒绝时若平台报错（flag 过期等），该申请标记为 `failed`，不会自动重试；主人可重新联系申请人再发起。
- **已处理不可重复操作**：同一申请第二次拍板会回复「已处理过」。
- 通知发送失败只记日志，申请仍已落库；主人随时可用 `request_list` 查到。

## 配置

在 Web 全局设置（或 `config.json`）：

| 键 | 默认 | 说明 |
|---|---|---|
| `request_notify_on` | `true` | 收到申请是否私聊通知主人；关闭后只落库不通知 |
| `owner_qq` | — | 接收通知、唯一能审批的 QQ |

审批不提供 Web 按钮：设计上只在对话里发生，所有决定都经过主人门禁（与删文件、MCP 远程桌面等风险操作同一套 `RISKY_TOOL_KEYS` 机制）。

## 给插件开发者的备注

- 框架新增了 request 事件通道：`@Plugin.on_request("request")`，事件对象 `utils.obj.RequestEvent`（字段 `request_type / sub_type / user_id / group_id / comment / flag`）。详见 [plugin-guide.md](plugin-guide.md) 「请求事件」一节。
- 正向 WebSocket 不能用 OneBot 快速操作，只能显式调 `set_friend_add_request` / `set_group_add_request`；NapCat 约定**不需要**传 `sub_type`（已编码在复合 flag 里）。
- 相关代码：插件侧 `plugins/deepseek/Core/requests.py`，框架侧 `core/events.py`、`utils/obj.py`、`utils/Plugin.py`。
