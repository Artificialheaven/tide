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

## 3. QQ 空间（仅文字说说）

走 **NapCat 自带 OneBot HTTP 扩展接口**，不做 cookie/扫码逆向：

- `POST {http_address}/send_qzone_msg` → `data.tid`
- `POST {http_address}/delete_qzone_msg` `{tid}`

### 配置

| 来源 | 键 | 说明 |
|---|---|---|
| 框架根 `config.json` | `http_address`、`http_token` | 首选，与 WebSocket 长连接互相独立；Bearer 鉴权与普通 OneBot HTTP action 相同 |
| 插件 `config.json`（可选覆盖） | `napcat_http_url`、`napcat_http_token` | 插件配置优先，缺项回落根配置 |

只在发表/删除时发起 async httpx 请求（20s 超时），低频主人操作不占线程池。

### 工具

- `qzone_publish(content, ugc_right=1, target_uins?)`：发表纯文字说说（≤5000 字）。成功返回 `tid` 并在本地 `qzone_posts` 表留痕（内容、可见范围、登记人、时间）。
  - `ugc_right`：**1 所有人（默认）/ 4 好友 / 16 部分好友可见 / 64 仅自己 / 128 部分好友不可见**；16/128 必须带 `target_uins`（QQ 号数组）。
  - v1 不发图（NapCat 的 `images` 参数未接，后续可扩展）。
- `qzone_delete(tid)`：删除自己发的说说。
- `qzone_recent(limit=10)`：查本机器人最近发布留痕，用于拿 tid。

### 留痕与边界

- NapCat 4.18.41 **没有空间列表接口**，`qzone_recent` 只查得到本机器人成功发表的记录（`qzone_posts`，v10 新建，tid 唯一去重）；手动在 QQ 里发的说说不可见、也无法用本工具删。
- 删除走 tid 精确删除；发表接口失败（retcode≠0、连不上、返回非 JSON）会把 NapCat 的 wording 透传回模型，且**不留本地记录**。
- 发表与删除属外发不可逆操作：主人门禁 + 群工具开关双重限制；留痕表可在 Web 后续版本做管理页（v1 仅 AI 工具）。

## 存储迁移

v10（PRAGMA user_version=10，幂等）：新建 `qzone_posts` 表 + `idx_qzone_time`、`idx_usage_group_time` 索引。历史用量无需回填——分类统计直接基于既有 `usage_log`。
