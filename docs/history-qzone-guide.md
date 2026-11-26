# 历史查询 / Token 用量 / QQ 空间（2026.10.26.1）

三个能力都以 **AI 内置工具**形式提供，默认在所有开启 AI 的群生效；群管理页「工具」区可按群关闭（`history.query` / `usage.stats` / `qzone.*`）。`qzone.publish`、`qzone.delete` 是风险操作，**只有主人 QQ 能指令**（与 fs.write、code.python 同一套门禁）。

## 1. history_query — 查群历史消息

滑窗之外的旧消息也能查。两种方式二选一：

| 方式 | 参数 | 说明 |
|---|---|---|
| 时间段 | `start`、`end`（任一或都给） | `YYYY-MM-DD`、`YYYY-MM-DD HH:MM[:SS]`、`MM-DD HH:MM`（月日简写补今年，若落在 1 天以后的未来自动回退一年）、UNIX 秒 |
| 相对条数 | `offset` + `limit`（不给时间即为该模式） | `offset` 相对最新消息往回跳过几条（默认 0）；`limit` 默认 30、上限 100 |

可选 `include_tools=true` 连工具调用/结果行一起返回。规则：

- 结果按时间正序返回，带说话人昵称+QQ 与时间标签；超量时有翻页提示。
- 系统内部通知行（定时苏醒 / 入群通知 / 外部触发）**永远不返回**——它们装的是内部长指令。
- 群之间数据隔离，AI 只能查当前群。

数据来自既有 `messages` 表（v10 加了 `(group_id, created_at)` 索引），不增加额外落库。

## 2. token_stats 与 Web 用量展示

每次 DeepSeek 调用都在 `usage_log` 留一行（`kind` ∈ chat/judge/summary/extract/style/scheduled，带 `tokens_in/cache_hit/tokens_out/ok/created_at`）。

工具 `token_stats(days=30)`（`days=0` 全部历史）输出：

- **输入命中缓存 / 输入未命中 / 输出**三类总量 + 输入缓存命中率；
- 按调用类型拆分（对话、判官、摘要、记忆抽取、风格学习…）和调用次数、失败次数。

口径：`cache_hit` 是 `tokens_in` 的子集（DeepSeek 官方 usage 的 `prompt_cache_hit_tokens`，兼容硅基流动字段名），所以 **未命中 = tokens_in − cache_hit**，不是另一个独立计数。

Web 侧：

- 群列表页列改为 **命中 / 未命中 / 输出**（近 200 次调用的近似值，与旧「in/out」同源）。
- 群设置页新增「Token 用量（近30天）」卡片：三类总量、命中率、按类型小表。
- 接口：`GET /deepseek/api/groups/<gid>/usage?days=30`（`days=0` 全部），响应在旧 `usage` 之外新增 `breakdown`（`totals` + `by_kind`），旧字段保留兼容。

## 3. QQ 空间（发 / 读 / 评论 / 被 @ 知会）

走 **NapCat 的 OneBot 扩展 action**（不是 cookie 逆向、也不是独立 HTTP 接口）。NapCat 在标准 OneBot v11 之上加了 28 个 `*_qzone*` action，标准规范和官方手写文档里都没有登记，只能以源码为准（见 `docs/napcat-adaptation-review-2026-11-26.md`）。

### 3.1 通道：WS 优先，HTTP 兜底

| 通道 | 条件 | 说明 |
|---|---|---|
| WS（默认） | 机器人 WS 在线 | 复用同一条长连接 `call_api('get_qzone_msg_list', …)`；不需要额外配置，也不要求开 NapCat 的 HTTP 服务端 |
| HTTP 兜底 | WS 不可用且配了 `http_address`/`napcat_http_url` | `POST {http_address}/{action}`，`Authorization: Bearer <token>`，20s 超时 |
| 不可用 | `transport=official` | QQ 官方开放平台没有空间接口；所有 `qzone.*` 工具会从工具表和提示词里摘除，并在 Web/日志里给出原因 |

启动时做一次能力探测：`get_version_info` 拿版本 → `get_qzone_msg_list {num:1}` 探一次真实可用性，结果缓存 `qzone_probe_ttl` 秒（默认 600）；一次调用失败会自动换另一条通道重试一次。

### 3.2 工具（9 个）

只读（并发批调度白名单内，可和其他只读工具并行）：

- `qzone_list(limit=10, target_uin?)`：读说说列表（tid / 时间 / 正文 / 评论数 / 是否私密 / 配图数）。
- `qzone_detail(tid, pos=0, num=20)`：读某条说说的正文与评论树（含 comment_id / 楼中楼）。
- `qzone_images(tid, indexes?)`：下载说说配图并作为**图片**交给视觉模型（默认前 4 张，单张上限 `qzone_image_max_kb`）。
- `qzone_feeds(limit=10, page=1)`：读好友动态（"空友爱看"公共流）。
- `qzone_recent(limit=10)`：查本机器人发布留痕。

写（**风险操作，只听主人**）：

- `qzone_publish(content?, ugc_right=1, target_uins?, images?)`：发说说，支持**图文混排**（`images` ≤9 张，支持 `file://` `http(s)://` `base64://`，纯图片时 content 可为空）。成功登记到本地 `qzone_posts`。
- `qzone_delete(tid)`：按 tid 删除（NapCat 会校验归属，删别人的会报错）。
- `qzone_comment(tid, content, comment_id?, comment_uin?, reply_uin?, reply_name?, images?)`：评论说说；**回复某条评论** = `tid` + `comment_id` + `comment_uin`（两者必须成对，来自 `qzone_detail`）；`reply_uin`/`reply_name` 指定接收者；楼中楼只支持文字。
- `qzone_like(tid, on=true)`：点赞 / 取消点赞。

`ugc_right`：**1 所有人（默认）/ 4 好友 / 16 部分好友可见 / 64 仅自己 / 128 部分好友不可见**；16/128 必须带 `target_uins`。

### 3.3 被 @ 与新评论知会（轮询）

**NapCat 没有任何空间事件**（`event/` 只有 message/meta/notice/request 四类），所以"空间里被 @"只能轮询发现：

- 由 `Core/qzone_watch.py` 每 `qzone_watch_interval` 秒（默认 300，最小 30）跑一次；
- `_scan_feeds`：拉好友动态，用 `mentions_me()` 匹配 `@昵称` / `@QQ号`，命中写 `qzone_events(kind='mention')`；
- `_scan_comments`：拉自己的说说列表，评论数变化时才拉 `qzone_detail`，用 `new_comments()`（排除自己、按 comment_id 去重）写 `kind='comment'`；
- 派发：一条事件唤醒一轮对话（`engine.handle_trigger(run_kind='qzone', notice_text=…)`），投递到 `qzone_notify_group`（留空=主人私聊）；模型处理完输出 `[静默]` 就什么都不发；
- 首次启动只记基线（`qzone_state.baseline_done`），**不补历史**，避免一上来刷出几十条旧事件；
- 失败事件 `attempts` 上限 3 次，之后跳过；`prune_qzone_events` 清理 30 天前记录。

### 3.4 配置

| 来源 | 键 | 说明 |
|---|---|---|
| 框架根 `config.json` | `http_address`、`http_token` | HTTP 兜底通道（可选）；Bearer 鉴权 |
| 插件 `config.json`（覆盖） | `napcat_http_url`、`napcat_http_token` | 优先于根配置 |
| 插件 `config.json` | `qzone_watch_on` / `qzone_watch_interval` / `qzone_watch_feeds` / `qzone_watch_comments` / `qzone_notify_group` / `qzone_image_max_kb` / `qzone_probe_ttl` | 知会与读图行为 |

### 3.5 留痕与边界

- 本地只留**本机器人成功发表**的说说（`qzone_posts`，tid 唯一）；手动在 QQ 里发的说说用 `qzone_list`/`qzone_detail` 能读到，但删不掉（不是自己的）。
- 发表/评论/点赞失败会把 NapCat 的 `wording`/`msg` 透传回模型，且不留本地记录。
- 空间接口是 NapCat 私有扩展，NapCat 上游已于 2026-11-12 宣布停止维护（README 只剩 "Ending"）；现役 4.18.x 仍可用，但换框架（LLOneBot 等）就没有这些 action —— 这也是工具表要按 transport 摘除的原因。

## 存储迁移

- v10：新建 `qzone_posts` 表 + `idx_qzone_time`、`idx_usage_group_time` 索引。
- v20：`qzone_posts` 补 `images` 列；新建 `qzone_events`（轮询发现的空间事件队列，`UNIQUE(tid,kind,comment_id,actor_uin)` 去重）与 `qzone_state`（mention_since / comment_since / ccount:* / baseline_done 等键值）。
- 历史用量无需回填——分类统计直接基于既有 `usage_log`。
