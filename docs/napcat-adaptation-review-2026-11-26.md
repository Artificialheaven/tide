# NapCat 适配程度核对（2026-11-26）

> 核对对象：`/home/ubuntu/tide`（OneBot v11 框架 + `plugins/deepseek` 群聊插件）。
> 外部依据：NapCat 官方文档站 2026-11-25/26 抓取、GitHub 上游状态、fork 源码。
> 代码基线：提交 `164262e`（`docs: 补充功能缺陷审查与体验评估`）。§2–§5 描述**本轮空间改造之前**的基线，基线里已不存在的文件内容按 `git show HEAD:<path>` 取；其余行号对应当前工作区。

## 零、摘要

结论：本框架对 NapCat 的适配是「标准 OneBot v11 正向 WebSocket 客户端」范式——连接、鉴权、事件分发、消息段解析、CQ 出站这条主链路完整可用，日常群聊/私聊/审批/媒体读取都能跑；但接入面很窄：出站只用一个 `send_msg`，固定使用的 action 仅 18 个（约为 NapCat 文档 130 个基础 action 的 14%），18 类 notice 中只有「机器人自己入群」一种有应用逻辑。QQ 空间走自行拼的 HTTP 扩展通道，只用了 send/delete 两个 action，读取、评论、点赞全缺，且依赖默认关闭的 HTTP 服务端，现网实际不可用。更大的背景是 NapCat 上游已于 2026-11-12 停止维护并清空源码，适配工作从此进入「锁定版本 + 自行兜底」阶段。

要点：

- **主链路好、扩展面窄。** 瓶颈不在框架抽象，而在「能力探测」与「事件消费」缺位：能力探测只有一次 `get_version_info`（`plugins/deepseek/Core/passive_turn.py:40`），事件消费只覆盖入群一条。
- **上游已停维护（战略结论）。** `NapNeko/NapCatQQ` 于 2026-11-12 清空源码，README 只剩 48 字节的 Ending 声明；完整源码仅存于 fork。适配策略应从「跟进上游」改为「锁定可用版本 / 自持 fork」。
- **空间是最大短板。** 上游 28 个 qzone action 只用了 2 个；空间没有任何事件上报，只能轮询，而当前连轮询都没有；HTTP 通道默认关闭，导致功能默认不可用。
- **事件侧还有一批低成本增量。** poke、emoji_like、input_status、group_name、message_sent、群灰条、bot_offline 全仓零消费，补起来不改变架构。
- **文档与代码有历史漂移。** 基线 README 写 65 个工具、实际 `TOOL_KEYS` 66 个（偏差 1 个）；`history-qzone-guide` 的「v1 不发图」「4.18.41 无列表接口」两项结论已被上游 PR #1965/#2124 推翻。

## 一、目的、范围与方法

### 1.1 目的

回答三个问题：现在接住了 NapCat 的哪些能力；哪些能力被浪费或误用；下一步按什么优先级改。文档面向项目作者，既要覆盖矩阵，也要给出可执行的分档建议。

### 1.2 范围

- **连接与协议层**：`main.py`、`configs.py`、`core/events.py`、`core/bot.py`、`core/official/`、`utils/obj.py`。
- **消息解析与出站**：`plugins/deepseek/Core/message.py`、`reply.py`、`mentions.py`。
- **action 使用面**：全仓 `call_api(` 调用点、插件通知/请求处理器。
- **空间与工具**：`plugins/deepseek/Core/qzone.py`、`builtin_tools.py`、`storage.py`、`prompts.py`、`engine.py`。
- **文档**：`docs/history-qzone-guide.md`、`README.md`。
- 不含 GUI 细节、Slang 插件内部逻辑与 AI 插件会话算法（除非直接决定 NapCat 能力调用）。

另外把 README、`history-qzone-guide` 与代码实现的一致性作为佐证纳入核对范围：适配问题经常先表现为「文档说支持、代码没接」。

### 1.3 文档抓取方式与限制

NapCat 文档站 `https://napcat.napneko.icu` 是 VitePress 构建：裸路径全部 404，必须带 `.html`。本次抓取 `/onebot/index.html`、`/onebot/api.html`、`/onebot/event.html`、`/onebot/napcat.html`、`/onebot/basic_event.html`、`/onebot/segment.html`、`/onebot/network.html`、`/config/basic.html`，以及版本索引 `/api/`。

限制有三：

1. `/api/` 只是版本索引（4.12.0 → 4.18.42），`/api/4.18.42.html` 是纯前端壳；OpenAPI spec 的多种常见 json 地址均返回 404，**按 action 自动生成的详细文档无法离线获取**，只能用版本号确认接口面存在。
2. 手写页 `onebot/api`、`onebot/event`、`onebot/napcat` 里 grep `qzone` 均为 0 次——**QQ 空间扩展从未写进官方文档**，本文空间部分只能以 fork 源码为准，也意味着它对使用者没有文档契约。
3. 文档页对消息段的标题列了 15 种；材料（含 OneBot 标准与 NapCat 实现）按 16 种计。本文消息段表列 16 行：前 15 行对齐文档页，第 16 行按实现层的 `node`（合并转发节点）补入，脚注说明差异。

### 1.4 代码取证方法

- **action 面**：`grep -rn "call_api("` 统计固定调用，再排除动态拼接（`media_in.py`、`remote_files.py` 的 `_api_data`）；共 21 处调用点，归并为 18 个 action。
- **事件面**：`grep -rn "@Plugin.on_notice\|@Plugin.on_request"` 定位应用层消费点；对照 `core/events.py` 的分发分支。
- **工具数**：解析 `builtin_tools.py` 的 `TOOL_KEYS` 字典，与 `README.md:59` 交叉核对。
- **空间基线**：`git show HEAD:plugins/deepseek/Core/qzone.py`、`storage.py`、`builtin_tools.py`，避免本轮并行改造造成行号漂移。

### 1.5 行号与基线约定

本文一般引用当前工作区行号。§5 涉及空间改造前状态的证据统一标 `HEAD`，指提交 `164262e`，可用 `git show HEAD:<path>` 原样复现；基线 README 的工具数为 65，与基线 `TOOL_KEYS` 的 66 不一致（工作区已随本轮改造更新，见 §5.4）。

## 二、NapCat 上游与文档现状

### 2.1 上游终结

| 时间（2026） | 事实                                                                                        |
| ------------ | ------------------------------------------------------------------------------------------- |
| 11-12 02:58  | 最后一个提交 `Keep README only`，源码被清空                                                 |
| 11-12 03:26  | 提交 `docs: ending`，README 只剩 48 字节                                                    |
| 之后         | 仓库 `size=1`、`open_issues=12`；README 内容为「# NapCatQQ / ## Ending / 到这里告一段落。」 |
| 现状         | 完整源码仅存于 fork，如 `Creep-debug/NapCatQQ`、`MingTianSang/NapCatQQ`                     |

判断：**上游已停止维护**。这带来三个直接后果：QQ 客户端协议变化后没有人再做适配补丁；发现的安全问题不会修；文档站虽然还能访问，但已冻结在 4.18.42。项目若继续依赖 NapCat，必须把「可用的最后一个版本」和「fork 来源」当作资产管理，而不是当成会持续进化的上游。

停维护不等于「明天就不可用」：已登录的旧版本在 QQ 协议不变时仍可长期运行，真正的风险是「一旦坏掉无法自行修复」。因此适配重心应从「追新」转为「可观测 + 可降级 + 可替换」——能力探测、错误分类、通道兜底比新增 action 更重要。

### 2.2 fork 与生态可用性

- fork 是唯一源码来源，但 fork 的维护意愿、同步节奏、许可证与合规义务都需要单独确认；fork 也可能随时被删，建议对锁定版本做内部镜像。
- LLOneBot 已改名 LuckyLilliaBot（`LuckyLilliaDev`，2026-11-24 仍活跃），但全仓 0 个 qzone 文件；OneBot v11 标准公开 API 只有 38 个，也不含 qzone。**空间能力是 NapCat 独家扩展**，换实现等于放弃空间功能，不能作为规避停维护的迁移路径。

### 2.3 文档覆盖度

| 页面                       | 覆盖内容                                                         | 对本文的价值                                                        | 局限                                                       |
| -------------------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------- | ---------------------------------------------------------- |
| `/onebot/index.html`       | OneBot 定位声明                                                  | 原话「OneBot 是已经脱轨且停滞的协议规范，对框架本身没有实质性约束」 | 说明 NapCat 以自行扩展为主，标准只是底线                   |
| `/onebot/api.html`         | 8 类约 130 个基础 action                                         | 出站能力的全集清单                                                  | 无 qzone；无逐字段 schema（详表在打不开的 `/api/` 壳里）   |
| `/onebot/event.html`       | meta / notice / request / message 及扩展通知                     | 入站事件全集                                                        | qzone 无事件；`message_sent` 等需 `reportSelfMessage`      |
| `/onebot/segment.html`     | 15 种消息段                                                      | 消息段覆盖矩阵                                                      | 未单列 `node`；无 `contact` 说明                           |
| `/onebot/napcat.html`      | 差异实现说明                                                     | LRU 缓存、哈希 message_id、资源 URL 形态                            | 只说「无数据库、缓存会过期」，没给容量与过期时间的精确契约 |
| `/onebot/basic_event.html` | 基础事件机制（心跳/生命周期语义）                                | 理解 `heartbeat`/`lifecycle` 的正确用法                             | 不含扩展通知                                               |
| `/onebot/network.html`     | 网络配置说明（WS/HTTP 角色与地址）                               | 连接角色核对                                                        | 以示例配置为准                                             |
| `/config/basic.html`       | 四种网络角色、默认端口、`messagePostFormat`、`reportSelfMessage` | 连接角色核对                                                        | 默认值以示例配置为准，实际以部署为准                       |
| `/api/`                    | 版本索引 4.12.0 → 4.18.42                                        | 版本面确认                                                          | OpenAPI spec 取不到，无法自动生成覆盖表                    |

### 2.4 停维护风险评估

- **协议漂移**：空间 28 个 action 是 fork 里逆向后加的，QQ 客户端升级即可失效；即使不升级 NapCat，QQ 侧协议变化也会让旧版本登录失败。
- **安全修复缺失**：WebUI/HTTP/WS 三类入口都不会再有安全补丁，部署侧必须自行收敛暴露面与 token。
- **文档冻结**：新能力、字段变更不会再写进文档，运行时探测与失败降级成为必需品，而不是可选项。

## 三、框架侧连接与协议现状

### 3.1 连接角色

NapCat 支持四种网络角色，本框架只用了其中一种：

| NapCat 角色                    | 默认端口                  | 框架支持        | 证据 / 说明                                                                    |
| ------------------------------ | ------------------------- | --------------- | ------------------------------------------------------------------------------ |
| WS 服务端（正向）              | 3001                      | ✅ 唯一长期通道 | `main.py:61-70`：`websockets.connect(ws_address, Authorization: Bearer token)` |
| HTTP 服务端                    | 3000，默认 `enable:false` | ⚠️ 仅空间兜底   | `configs.py:31-33` 的 `http_address`/`http_token`，由 `Core/qzone.py` 使用     |
| HTTP 客户端（NapCat 回调）     | —                         | ❌ 未实现       | 无回调服务端                                                                   |
| WS 客户端（反向，NapCat 连出） | —                         | ❌ 未实现       | 无监听端                                                                       |

补充事实：`configs.py:35-36` 用 `transport` 在 `napcat`/`official` 间切换；`main.py:61-70` 的 `create_connector` 负责选择连接实现；工作区默认 `ws_address` 已改为 `ws://127.0.0.1:3001`（`configs.py:19`），与 NapCat 正向 WS 默认端口一致。本机实际 `config.json` 仍是 `ws://localhost:8080`（未入库），属于部署漂移，升级时需手工对齐。

连接层没有握手或订阅，鉴权完全依赖 `Authorization: Bearer <token>`（`main.py:69`）。NapCat 侧的 `heartInterval`（默认 30000）、`messagePostFormat`、`reportSelfMessage` 都是 NapCat 配置字段，框架无法从协议层覆盖。建议 `messagePostFormat` 保持 `array`：框架两条解析路径都支持，但段数组信息最全，CQ 兜底会丢掉部分结构。

### 3.2 启动与事件分发

入站链路是：`connect_and_run` 重连外循环 → `create_connector` → WebSocket → 接收循环。接收循环先按 `echo` 匹配 `bot.pending_requests` 里的 RPC Future（`main.py:252-255`），匹配不上才交给 `events.parse_event`（`main.py:258`）。

`parse_event` 分四支：

- **meta**（`core/events.py:52-73`）：`lifecycle.connect` 调 `init_bots` 发 `get_login_info`；`heartbeat` 只打日志。meta 事件不派发给插件。
- **message**（`core/events.py:75-158`）：群/私聊分别解析，异步处理器建 task、同步处理器丢 executor。
- **notice**（`core/events.py:161-185`）：所有 notice 统一分发给插件的 notice handler；`NoticeEvent` 只抽 `notice_type/sub_type/group_id/user_id/operator_id` 五个字段（`utils/obj.py:306-316`），扩展开关字段要回 `_raw` 取。
- **request**（`core/events.py:188-212`）：加好友/加群请求统一分发，应用侧暂存并私聊主人审批（`deepseek.py:450+`、`requests.py:147-157`）。

两个与 NapCat 扩展直接相关的缺口：`message_sent` 的 `post_type` 是 `message_sent`，不匹配 `'message'` 分支，会被静默忽略（主循环只打印 `Received:`）；`meta` 没有插件分发，`bot_offline` 之类的通知在框架层无处消费。

插件生命周期要与重连语义对齐：`connect_and_run` 是重连外循环，每次重连都会重建 bot、重新加载插件并再次触发 `on_start`。空间轮询、能力探测缓存这类状态如果挂在实例上，重连后必须重建；否则会出现「连接正常但工具不可用」的假象。

### 3.3 消息段解析

`Core/message.py` 有两条解析路径：优先段数组（`parse_segments`，`:378-444`），段数组缺失时用 CQ 字符串兜底（`parse_cq`，`:446-520`）。`parse_event`（`:531-560`）统一封装成 `ParsedMessage`，并补 `sender/message_id/time/chat_type`。

- 图片走紧凑 CQ 码并登记 media ref，供后续 `get_image` 回看（`media_in.py:84`）。
- 语音/视频/文件/表情只出占位（`_MEDIA_PLACEHOLDER`，`:28-31`）：`record→[语音]`、`face→[表情]`、`video→[视频]`、`file→[文件]`。
- `mface/dice/rps/poke/music` 没有映射，直接丢弃——这是消息段覆盖表里信息损失最明显的部分。
- `self_group_join_notice`（`:564-581`）只认「`group_increase` 且 `user_id == self_id`」，即只有机器人自己入群能触发应用逻辑。

### 3.4 出站

出站唯一入口是 `bot.send_message`（`core/bot.py:89-158`），统一发 `send_msg`（`:132`），参数是 `message` + `user_id`/`group_id`。`message` 是 CQ 字符串：

- 引用回复：`reply_prefix`（`reply.py:18-23`）拼 `[CQ:reply,id=...]`，只接受 `[0-9A-Za-z_-]{1,64}`。
- @：`render_ats`（`mentions.py:41`）把 `@昵称` 映射成 `[CQ:at,qq=...]`，依赖 `get_group_member_info`/`get_group_member_list` 的名单缓存（`mentions.py:161`、`:238`）。
- 图片/语音：`_local_cq`（基线 `builtin_tools.py:1484`）接受 URL 或 `base64://`。
- @全体：`qq_at_all`（基线 `builtin_tools.py:1930/1949`）拼 `[CQ:at,qq=all]`。

实际出站段只有 text/at/reply/image/record 五种；file/video/json/music/forward 没有专用构造。理论上可以手写 CQ 透传，但没有任何工具或提示词引导模型这样做，因此视为「未覆盖」。

### 3.5 official 兼容层

`core/official/` 是 QQ 官方开放平台 v2 的兼容层，把官方事件映射成 OneBot 形状：

- `convert_in.py:380-407` 只处理 `GROUP_AT_MESSAGE_CREATE`（→message）与 `GROUP_ADD_ROBOT`（→notice/group_increase），不伪造私聊/好友/频道。
- `session.py:220-230` 的 `dispatch_action`：`send_msg`/`send_group_msg` 走 `_action_send_msg`；`send_private_msg` 明确失败（「官方机器人仅支持群聊消息」）；未实现的 action 返回「官方机器人暂不支持该操作」。
- 已实现 14 个 action：`get_login_info`（`:369`）、`get_group_list`（`:373`）、`get_group_info`（`:386`）、`get_group_member_list`（`:414`）、`get_group_member_info`（`:443`）、`get_stranger_info`（`:503`）、`get_friend_list`（`:506`）、`delete_msg`（`:509`）、`get_image`（`:522`）、`get_record`（`:525`）、`get_file`（`:528`）、`get_group_file_url`（`:531`）、`get_forward_msg`（`:555`）、`get_version_info`（`:577-583`）。
- official 通道不发 meta lifecycle，所以 `init_bots` 由框架显式调用（`main.py:21-28`）；`passive_turn.py:32-59` 通过 `get_version_info` 识别通道并缓存（`engine.py:919-921` 每轮取用）。

结论：兼容层让上层代码协议无关，但能力天花板很低（无空间、无私聊、无多数 action），只能当备用通道，不能替代 NapCat。

## 四、覆盖矩阵

统计口径：action 以固定调用点为准（不含动态字符串拼接）；事件按 NapCat 文档列出的类型逐项核对；消息段按 16 种核对（15 种文档段 + `node`）。

总体覆盖率：连接角色 1/4；有应用消费的入站事件 5/25（message 两类、request 两类、自己入群一类）；出站 action 18/约 130（约 14%）；消息段入站可解析 11/16、出站可构造 5/16。

### 4.1 入站事件覆盖

| 事件                          | 框架是否处理                   | 处理位置                                            | 建议                                  |
| ----------------------------- | ------------------------------ | --------------------------------------------------- | ------------------------------------- |
| meta / heartbeat              | ⚠️ 仅日志                      | `core/events.py:65-73`                              | 用最后心跳时间做健康探测（P2）        |
| meta / lifecycle              | ⚠️ 仅 connect 触发 `init_bots` | `core/events.py:54-63`                              | 可扩展 enable/disable 子类型（P2）    |
| message / private             | ✅ 解析 + 应用消费             | `core/events.py:124-158`；`Core/message.py:531-560` | 保持                                  |
| message / group               | ✅ 解析 + 应用消费             | `core/events.py:87-122`                             | 保持                                  |
| message_sent                  | ❌ 静默忽略                    | `core/events.py:75`（只认 `post_type=='message'`）  | 需开 `reportSelfMessage` 才有（P2）   |
| notice / friend_add           | ⚠️ 分发，无消费                | `core/events.py:161-185`；`deepseek.py:424-449`     | 欢迎语/好友登记（P2）                 |
| notice / friend_recall        | ⚠️ 分发，无消费                | 同上                                                | 撤回感知（P2）                        |
| notice / group_recall         | ⚠️ 分发，无消费                | 同上                                                | 撤回感知（P2）                        |
| notice / group_increase       | ✅ 仅机器人自己入群            | `deepseek.py:424-449`；`message.py:564-581`         | 他人入群欢迎/白名单（P1）             |
| notice / group_decrease       | ⚠️ 分发，无消费                | 同上                                                | 退群清理本地缓存（P1）                |
| notice / group_admin          | ⚠️ 分发，无消费                | 同上                                                | 权限变化提示（P2）                    |
| notice / group_ban            | ⚠️ 分发，无消费                | 同上                                                | 禁言提醒（P2）                        |
| notice / group_upload         | ⚠️ 分发，无消费                | 同上                                                | 群文件入库（P1，配合 `remote_files`） |
| notice / group_card           | ⚠️ 分发，无消费                | 同上                                                | 名片变更（P2）                        |
| notify / group_name           | ❌ 全仓 0 命中                 | —                                                   | 群名缓存失效（P1）                    |
| notify / title                | ⚠️ 分发，无消费                | `core/events.py:161-185`                            | 头衔变更（P2）                        |
| notify / essence              | ⚠️ 分发，无消费                | 同上                                                | 精华消息（P2）                        |
| notice / group_gray_tip       | ❌ 全仓 0 命中                 | —                                                   | 灰条提示（P2）                        |
| notice / group_msg_emoji_like | ❌ 全仓 0 命中                 | —                                                   | 回赞（P1，`likes[]` 在 `_raw`）       |
| notify / poke                 | ❌ 事件无消费                  | 仅 `core/official/convert_out.py:35` 占位           | 被戳回应（P1）                        |
| notify / profile_like         | ❌ 全仓 0 命中                 | —                                                   | 资料赞（P2）                          |
| notify / input_status         | ❌ 全仓 0 命中                 | —                                                   | 「正在输入」提示（P2）                |
| notice / bot_offline          | ❌ 全仓 0 命中                 | —                                                   | 主动重连/告警（P1）                   |
| request / friend              | ✅ 暂存 + 主人审批             | `deepseek.py:450+`；`requests.py:147`               | 保持                                  |
| request / group               | ✅ 暂存 + 主人审批             | 同上；`requests.py:152-157`                         | 保持                                  |

### 4.2 出站 action 覆盖

| 分组 | action                                                                                                                                                                   | 框架是否使用 | 用途 / 缺位与优先级                                                 |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------ | ------------------------------------------------------------------- |
| 账号 | get_login_info                                                                                                                                                           | ✅           | 启动探活、取昵称（`events.py:21`、`deepseek.py:138`）               |
| 账号 | get_version_info                                                                                                                                                         | ✅           | 通道识别（`passive_turn.py:40`；官方通道返回 `transport=official`） |
| 账号 | get_status                                                                                                                                                               | ❌           | 健康检查（P2）                                                      |
| 账号 | set_self_longnick / set_qq_profile / set_qq_avatar                                                                                                                       | ❌           | 改资料，仅主人显式指令（P2）                                        |
| 账号 | clean_cache / get_clientkey / bot_exit                                                                                                                                   | ❌           | 运维/危险，不建议接                                                 |
| 好友 | get_friend_list                                                                                                                                                          | ✅           | 管理面板报表（`ui/report_window.py:172`）                           |
| 好友 | get_stranger_info                                                                                                                                                        | ✅           | 加好友审批时补资料（`requests.py:95`）                              |
| 好友 | set_friend_add_request                                                                                                                                                   | ✅           | 审批（`requests.py:147`）                                           |
| 好友 | get_friend_msg_history                                                                                                                                                   | ❌           | 补私聊断线断层（P1）                                                |
| 好友 | friend_poke                                                                                                                                                              | ❌           | 主动戳一戳（P1，与 poke 事件成对）                                  |
| 好友 | get_unidirectional_friend_list / set_friend_remark / delete_friend / get_profile_like / fetch_emoji_like                                                                 | ❌           | 低价值或不建议（P2）                                                |
| 群   | get_group_list                                                                                                                                                           | ✅           | 报表（`report_window.py:145`）                                      |
| 群   | get_group_info                                                                                                                                                           | ✅           | 群名补全（`requests.py:106`、`deepseek.py:399`）                    |
| 群   | get_group_member_list                                                                                                                                                    | ✅           | @名单（`mentions.py:238`）                                          |
| 群   | get_group_member_info                                                                                                                                                    | ✅           | @单人查询（`mentions.py:161`）                                      |
| 群   | set_group_add_request                                                                                                                                                    | ✅           | 审批（`requests.py:152-157`）                                       |
| 群   | get_group_msg_history                                                                                                                                                    | ❌           | 断线补历史（P1）                                                    |
| 群   | group_poke                                                                                                                                                               | ❌           | 主动戳一戳（P1）                                                    |
| 群   | get_group_at_all_remain / get_group_shut_list / get_group_honor_info / get_group_system_msg                                                                              | ❌           | @全体配额、禁言名单等群状态（P2）                                   |
| 群   | get_essence_msg_list / set_essence_msg / delete_essence_msg                                                                                                              | ❌           | 精华消息（P2）                                                      |
| 群   | _get_group_notice / _send_group_notice / _del_group_notice                                                                                                               | ❌           | 群公告（P2）                                                        |
| 群   | set_group_kick / ban / whole_ban / admin / card / name / leave / special_title / portrait / remark / sign                                                                | ❌           | 群管理 11 个，仅主人显式指令才考虑（P2）                            |
| 消息 | send_msg                                                                                                                                                                 | ✅           | 唯一出站（`core/bot.py:132`）                                       |
| 消息 | get_image                                                                                                                                                                | ✅           | 图片回看/视觉输入（`media_in.py:84`）                               |
| 消息 | get_record                                                                                                                                                               | ✅           | 语音转写输入（`media_in.py:87`）                                    |
| 消息 | get_file                                                                                                                                                                 | ✅           | 文件下载（`media_in.py:100/125`）                                   |
| 消息 | get_forward_msg                                                                                                                                                          | ✅           | 合并转发展开（`builtin_tools.py` 工作区）                           |
| 消息 | send_poke                                                                                                                                                                | ❌           | 主动戳（P1）                                                        |
| 消息 | ocr_image                                                                                                                                                                | ❌           | 图片 OCR（P2）                                                      |
| 消息 | get_msg                                                                                                                                                                  | ❌           | 按 id 取消息，注意 LRU 过期（P2）                                   |
| 消息 | can_send_image / can_send_record                                                                                                                                         | ❌           | 发送前能力探测（P2）                                                |
| 消息 | get_recent_contact / _mark_all_as_read / mark_msg_as_read                                                                                                                | ❌           | 低价值（P2）                                                        |
| 文件 | get_group_root_files                                                                                                                                                     | ✅           | 列群根目录（`remote_files.py:112`）                                 |
| 文件 | get_group_files_by_folder                                                                                                                                                | ✅           | 列子目录（`remote_files.py:108`）                                   |
| 文件 | get_group_file_url                                                                                                                                                       | ✅           | 换临时直链（`remote_files.py:158`）                                 |
| 文件 | upload_group_file                                                                                                                                                        | ❌           | 主动发文件（P1/P2，需本地路径）                                     |
| 文件 | download_file / move_group_file / trans_group_file / rename_group_file / delete_group_file / create_group_file_folder / delete_group_folder / get_group_file_system_info | ❌           | 与已有读取链路重叠或属管理（P2）                                    |
| 文件 | upload_private_file / get_private_file_url                                                                                                                               | ❌           | 私聊文件（P2）                                                      |
| AI   | get_ai_characters / get_ai_record / send_group_ai_record                                                                                                                 | ❌           | 与插件自有 LLM 重复，不建议接                                       |
| 转发 | send_group_forward_msg / send_private_forward_msg / send_forward_msg                                                                                                     | ❌           | 发合并转发（P1）                                                    |
| 转发 | ArkSharePeer / ArkShareGroup / get_mini_app_ark                                                                                                                          | ❌           | 卡片分享，不建议（P2）                                              |
| 其他 | set_msg_emoji_like                                                                                                                                                       | ❌           | 回赞（P1，与 emoji_like 通知成对）                                  |
| 其他 | get_cookies / get_csrf_token / get_credentials                                                                                                                           | ❌           | 敏感凭据，明确不接                                                  |
| 其他 | set_doubt_friends_add_request / get_rkey / get_robot_uin_range / get_online_clients                                                                                      | ❌           | 低频，按需（P2）                                                    |
| 其他 | create_collection / get_collection_list / fetch_custom_face / check_url_safely                                                                                           | ❌           | 收藏、表情、链接检测（P2）                                          |
| 其他 | translate_en2zh / click_inline_keyboard_button / .handle_quick_operation / send_packet                                                                                   | ❌           | 本地可替代、依赖官方卡片或底层裸包，不建议                          |

注：表格覆盖了文档里的主要 action，不是 130 个的全量逐项；未列出的孤立 action 多为聊天记录、收藏、频道等与当前产品无关的能力。

### 4.3 消息段覆盖

| 消息段  | 入站解析      | 出站发送 | 说明                                                                   |
| ------- | ------------- | -------- | ---------------------------------------------------------------------- |
| text    | ✅            | ✅       | 主路径                                                                 |
| at      | ✅            | ✅       | `at_me` 归一化为 `qq=self_id`；发送走 `render_ats`（`mentions.py:41`） |
| reply   | ✅            | ✅       | 入站带被回复摘要；出站 `reply.py:18-23`                                |
| face    | ✅ 占位       | ❌       | 入站 `[表情]`（`message.py:29`）；无构造                               |
| mface   | ❌ 丢弃       | ❌       | 商城表情，无映射                                                       |
| dice    | ❌ 丢弃       | ❌       | 骰子，无映射                                                           |
| rps     | ❌ 丢弃       | ❌       | 猜拳，无映射                                                           |
| poke    | ❌ 丢弃       | ❌       | 戳一戳，段与事件都没接                                                 |
| image   | ✅            | ✅       | 入站登记 media ref；出站 `_local_cq`（基线 `builtin_tools.py:1484`）   |
| record  | ✅ 占位       | ✅       | 入站 `[语音]`；出站 `_local_cq` 支持 `base64://`                       |
| video   | ✅ 占位       | ❌       | 入站 `[视频]`；无上传/发送构造                                         |
| file    | ✅ 占位       | ❌       | 入站 `[文件]` 并可下载；发送需 `upload_group_file`                     |
| json    | ✅ 卡片       | ❌       | 只取标题；无卡片构造                                                   |
| music   | ❌ 丢弃       | ❌       | 音乐分享，无映射                                                       |
| forward | ✅ 只取 id    | ❌       | 入站可 `get_forward_msg` 展开；无发送 action                           |
| node    | ✅ 节点内占位 | ❌       | 段可解析为 `[合并转发]`；发送依赖 `send_*_forward_msg`                 |

脚注：`/onebot/segment.html` 实际按四组列出 15 种段，未单列 `node`、也没有官方的 `contact` 段说明；材料中的「16 种」按实现层把合并转发节点计为一种，本文据此列出第 16 行。

## 五、QQ 空间专项

### 5.1 基线实现（HEAD `164262e`）

基线 `plugins/deepseek/Core/qzone.py` 只有 162 行，纯 HTTP 扩展通道：

- `resolve_endpoint`（`:53-60`）从插件配置 `napcat_http_url`/`napcat_http_token` 取地址，缺项回落框架 `http_address`/`http_token`（`configs.py:31-33`），两套配置入口。
- `_post`（`:70-98`）用 httpx `POST {base}/<action>`，`Authorization: Bearer`，校验 HTTP 状态与 `retcode`/`status=failed`。
- `publish`（`:100-137`）只发文字：payload 为 `content`/`ugc_right`/`target_uins`，签名里**没有 images 参数**；成功后 `storage.add_qzone_post`（`:131-134`）留痕。
- `delete`（`:139-146`）按 tid 删除。
- `recent`（`:148-162`）只读本地表，docstring 明确写「NapCat 此版本无空间列表接口」（`:149`）。
- 全文 grep `transport`/`official` 为 0：**没有官方通道守卫**。

工具面只有 3 个（`HEAD builtin_tools.py:84-86`）：`qzone_publish`/`qzone_delete`/`qzone_recent`。schema 在 `:1186`、`:1210`、`:1225`；`RISKY_TOOL_KEYS` 在 `:1348` 把 publish/delete 划为主人专属；`ToolContext.qzone` 在 `:1366`；dispatch 在 `:2336-2354`。接线位置：`deepseek.py:51/115/697-699`、`prompts.py:472-474`、`engine.py:884`。存储表 `qzone_posts` 在 `storage.py:319-328`，写入 `:3049`，读取 `:3063`。

### 5.2 NapCat 的 28 个空间 action

自有空间 14 个（来自 fork `packages/napcat-onebot/action/extends/`）：

| action                                | 能力                                                                                                      | 基线是否使用 |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------- | ------------ |
| send_qzone_msg                        | 发说说：文字 + 图片（`file://`/`http(s)://`/`base64://`）+ `ugc_right` + `target_uins`，返回 `{tid,time}` | ✅（仅文字） |
| delete_qzone_msg                      | 按 tid 删说说                                                                                             | ✅           |
| get_qzone_feeds                       | 好友动态流，返回 feeds + `has_more`/`next_cursor`                                                         | ❌           |
| get_qzone_msg_list                    | 自己的说说列表，SnowLuma 格式 `{total,msglist[]}`                                                         | ❌           |
| get_qzone_msg_detail                  | 单条说说详情 + 评论树（`comment_id`/`uin`/`name`/`replies`）                                              | ❌           |
| get_qzone_comment_list                | 评论列表                                                                                                  | ❌           |
| get_qzone_comment_replies             | 某条评论的楼中楼                                                                                          | ❌           |
| like_qzone / unlike_qzone             | 点赞/取消赞                                                                                               | ❌           |
| comment_qzone                         | 评论或回复评论（`tid`+`content`，回复需 `comment_id`+`comment_uin` 成对）                                 | ❌           |
| get_qzone_profile / set_qzone_profile | 空间资料读/写                                                                                             | ❌           |
| set_qzone_msg_right                   | 改说说可见范围                                                                                            | ❌           |
| set_qzone_ban                         | 空间封禁相关                                                                                              | ❌           |

公共流「空友爱看」14 个：`get_qzone_public_capabilities`、`get_qzone_public_feeds`、`get_qzone_public_feed_detail`、`get_qzone_public_comments`、`get_qzone_public_replies`、`set_qzone_public_upvote`、`set_qzone_public_like`、`set_qzone_public_follow`、`set_qzone_public_comment_like`、`comment_qzone_public_feed`、`reply_qzone_public_feed`、`delete_qzone_public_comment`、`delete_qzone_public_reply`、`forward_qzone_public_feed`——基线全部未用，且这一组依赖腾讯侧公共流接口，稳定性低于自有空间，优先级应排在自有空间之后。

时间线：2026-07-20 PR #1965 首次加入空间发布与图片上传；2026-10-29 PR #2124 完善空间 API 并兼容 SnowLuma 格式。基线文档「v1 不发图」与「4.18.41 无列表接口」都停留在 PR #1965 之前的认知。

使用优先级：自有空间 14 个 action 是已落地能力，应先接读取与互动；公共流 14 个属于「空友爱看」式公共内容，接口稳定性与内容合规都更弱，适合作为 P2 探索，不应作为空间功能的主路径。

### 5.3 空间没有事件上报

fork 的 `napcat-onebot/event/` 只有 message/meta/notice/request 四类，notice 的 23 个实现里没有 qzone。直接后果：

- 点赞、评论、@ 提及、好友发说说都**不会实时到达**，只能在轮询里发现。
- 轮询必须自带状态：按 `tid` 去重、记录上次游标/时间，否则会重复喂给模型；`qzone_state`/`qzone_events` 这类本地表是必需品。
- 实时性上限由轮询间隔决定，做不出「秒回空间评论」的体验。

轮询设计约束：空间 action 没有游标契约，`get_qzone_msg_list` 只有 SnowLuma 的 `msglist`，好友动态才有 `next_cursor`；新评论/@ 只能在详情里做集合差。若要做「知会」，本地至少要存 `tid → 已见 comment_id / 点赞用户` 的映射，并对轮询失败做退避。

### 5.4 仓库文档的过时点

| 旧陈述                              | 位置（HEAD `164262e`）           | 事实                                                                       | 处理                                                 |
| ----------------------------------- | -------------------------------- | -------------------------------------------------------------------------- | ---------------------------------------------------- |
| 「QQ 空间（仅文字说说）」           | `docs/history-qzone-guide.md:39` | `send_qzone_msg` 已支持图片（PR #1965）                                    | 本轮已改                                             |
| 「v1 不发图（images 参数未接）」    | 同上 `:59`                       | 同上，且图片支持多来源 URL                                                 | 本轮已改                                             |
| 「NapCat 4.18.41 没有空间列表接口」 | 同上 `:65`                       | 4.18.41+ 已有 `get_qzone_msg_list`/`get_qzone_feeds`/详情/评论（PR #2124） | 本轮已改                                             |
| 「仅 HTTP 扩展、需开 HTTP 服务端」  | 同上 `:41-51`                    | WS 已连通，`call_api` 可直接调 qzone action                                | 本轮已改                                             |
| 「README 65 个工具」                | `README.md:59`（HEAD）           | 基线 `TOOL_KEYS` 66 个，偏差 1 个                                          | 本轮已订正为 73，与当前 `TOOL_KEYS` 一致；发布前重核 |

### 5.5 具体缺陷清单（基线）

- **D1 通道默认不可用**：空间只走 HTTP 服务端，而 NapCat 的 HTTP 服务端默认 `enable:false`、端口 3000；框架 `http_address` 默认空（`configs.py:31-33`）。现网不额外配置时，空间功能 100% 失败。
- **D2 工具却常驻**：`deepseek.py:115` 无条件创建 `QzoneClient`，`engine.py:884` 恒注入，导致 `builtin_tools.py:2336-2338` 的「`ctx.qzone is None` → QQ 空间功能未启用」是**不可达分支**；用户实际看到的是 HTTP 连接错误。
- **D3 无 official 守卫**：`HEAD qzone.py` 全文无 `transport` 判断。官方通道下空间工具仍会出现并被调用，结果是打到不存在的 HTTP 端口。
- **D4 能力面残缺**：只实现 send/delete，列表、详情、好友动态、评论、点赞、取消赞、图片全部缺失；28 个 action 用了 2 个。
- **D5 发不了图**：`publish` 签名无 `images`，上游已支持的图片能力被浪费。
- **D6 没有轮询**：空间无事件，基线也没有定时任务读取新评论/点赞/@，功能只能「写」，不能「读」。
- **D7 配置不闭环**：插件配置 `napcat_http_url`/`napcat_http_token` 不在 `DEFAULTS`，也不在 `web.json`（HEAD 0 命中），只能手改 `config.json`，Web 面板无法配置。
- **D8 文档过时**：§5.4 五条旧结论会误导后续维护者。
- **D9 错误文案不可诊断**：HTTP 失败时把底层异常文案直接透传，未区分「通道未启用」「token 错误」「接口不存在」。
- **D10 无能力探测**：不同 NapCat 版本/分支的 action 集合不同，基线没有运行时探测，也没有降级路径。

## 六、差距与改造分档

分档原则：P0 是「现状不可用或随时会坏」；P1 是「能力明显缺失、改动可控、收益直接」；P2 是「锦上添花或依赖产品决策」。「成本」按单人日粗估。

P0 决定「现在能不能用」，P1 决定空间能否从「只能写」变成「能读、能互动、有知会」，P2 则多是产品选择题。建议按档推进，不要把 P1 和 P2 混在一个大版本里。

### 6.1 P0（必须做）

| 项         | 动作                                                                               | 理由                                           | 影响面                       | 成本             |
| ---------- | ---------------------------------------------------------------------------------- | ---------------------------------------------- | ---------------------------- | ---------------- |
| 空间通道   | 优先复用已有 WS `call_api` 调 28 个 action，HTTP 仅作兜底                          | HTTP 默认关闭、多一套端口/token；WS 已鉴权连通 | 空间全部工具与部署配置       | 中（0.5–1 人日） |
| 能力探测   | 启动时 `get_version_info` + `get_qzone_msg_list {num:1}` 探活，结果缓存（如 600s） | 空间接口无文档契约、版本差异大，只能运行时判断 | 工具可用性、错误提示、降级   | 低（0.2 人日）   |
| 可用性门控 | 探测失败或 `transport=official` 时不注入空间工具、提示词中摘除并给出原因           | 现在工具常驻且报连接错误，污染 prompt          | 工具表、提示词、Web/日志提示 | 低（0.2 人日）   |
| 文档同步   | 更新 `history-qzone-guide`、README 工具数                                          | 旧结论已被上游推翻，会误导维护                 | 文档                         | 低（0.2 人日）   |

### 6.2 P1（建议做）

| 项               | 动作                                                                                          | 理由                                     | 影响面                  | 成本           |
| ---------------- | --------------------------------------------------------------------------------------------- | ---------------------------------------- | ----------------------- | -------------- |
| 空间读取         | 接 `get_qzone_msg_list`/`get_qzone_msg_detail`/`get_qzone_feeds`，新增 list/detail/feeds 工具 | 只写不读没有产品价值                     | 空间工具、prompt        | 中（1 人日）   |
| 空间配图         | 详情里带 `images`，下载后作为图片输入视觉模型                                                 | 图片是空间内容主体                       | 媒体链路                | 低（0.3 人日） |
| 空间互动         | 接 `comment_qzone`/`like_qzone`/`unlike_qzone`                                                | 评论/点赞是空间的主要交互                | 空间工具、风控          | 中（0.5 人日） |
| 空间知会轮询     | 定时拉新评论/赞/@，按 tid+评论 id 去重，命中后走主模型或知会主人                              | 无事件上报，只能轮询                     | 定时任务、本地状态表    | 中（1 人日）   |
| poke 双向        | 消费 `notify(poke)` 事件；接 `send_poke`/`group_poke`                                         | 社交互动里高频，改动小                   | 事件分发、工具          | 低（0.3 人日） |
| emoji_like       | 消费 `group_msg_emoji_like`；接 `set_msg_emoji_like` 回赞                                     | 通知里有完整 `likes[]`，回赞成本低       | 事件分发、工具          | 低（0.2 人日） |
| 群名/群事件缓存  | 消费 `notify(group_name)`、`group_upload`、`group_decrease`，维护群名与成员缓存               | 现在靠 `get_group_info` 补名，事件更实时 | `mentions` 缓存、群管理 | 低（0.3 人日） |
| 断线补历史       | 接 `get_group_msg_history`/`get_friend_msg_history` 在重连后补断层                            | 重连后上下文丢失是现有体验问题           | 消息链路                | 中（0.5 人日） |
| 后台任务生命周期 | 轮询/探测按 `on_start`/`on_stop` 幂等注册，状态落盘                                           | 重连会重建 bot 与插件，实例内状态会丢    | 空间轮询、通知缓存      | 低（0.2 人日） |

### 6.3 P2（按需）

| 项               | 动作                                                                 | 理由                        | 影响面         | 成本 |
| ---------------- | -------------------------------------------------------------------- | --------------------------- | -------------- | ---- |
| message_sent     | 开启 `reportSelfMessage`，消费 `post_type='message_sent'`            | 多端同步/审计需要；默认关闭 | 事件分发、配置 | 低   |
| ocr_image        | 接图片 OCR                                                           | 视觉模型已能看图，收益边际  | 工具           | 低   |
| 精华/公告        | 接 essence、`_get_group_notice`/`_send_group_notice`                 | 群运营能力                  | 工具           | 低   |
| 合并转发发送     | 接 `send_group_forward_msg`/`send_private_forward_msg`               | 长内容整理场景              | 出站           | 中   |
| 文件上传         | 接 `upload_group_file`                                               | 需本地路径/权限策略         | 出站、文件管理 | 中   |
| 公共流 14 action | 空友爱看读取/互动                                                    | 稳定性存疑，依赖腾讯侧接口  | 工具面         | 高   |
| 群状态           | `get_group_at_all_remain`/`get_group_shut_list`/`get_online_clients` | 群运营辅助                  | 工具           | 低   |
| 群管理 11 action | 踢/禁言/改名片等                                                     | 风险高，仅主人显式指令      | 权限、审计     | 中   |

## 七、本次实施记录

2026-11-26 按本文 P0/P1 分档落地了第一批：**空间读写全通 + 空间知会轮询 + 扩展通知策略 + official 门控**。
改动全部可回退（配置开关），没有改数据库既有列语义，也没有新增第三方依赖。

### 7.1 新增文件

| 文件 | 行数 | 作用 |
|---|---|---|
| `plugins/deepseek/Core/runtime_env.py` | 52 | 读根 `config.json`（mtime 缓存）：`transport()` / `is_official()` / `unavailable_reason()` | 
| `plugins/deepseek/Core/qzone.py`（重写） | 716 | WS 优先 + HTTP 兜底的空间客户端：能力探测、读/写/评论/点赞、读配图、文本格式化 | 
| `plugins/deepseek/Core/qzone_watch.py` | 379 | 空间轮询：扫好友动态里的 @、扫自己说说的新评论，落库后派发给引擎 | 
| `plugins/deepseek/Core/backfill.py` | 158 | 断线/重启后用 `get_group_msg_history` 补本地群语料（只进 `raw_messages`，不触发回复） |
| `plugins/deepseek/Core/notices.py` | 170 | NapCat 扩展通知策略表（戳一戳/资料赞/群名/表情回应/精华/离线…） | 
| `tests/test_napcat_qzone_2026_11_26.py` | 709 | 30 个回归用例（纯函数、通道、工具表、通知、迁移、message_sent、空间知会派发、群级覆盖、场景禁用、9 工具分派、断线补历史） |
| `tests/test_live_debug_2026_11_26.py` | 142 | 8 个回归用例：调试台历史回看的游标/过滤/分页，投递会话在状态接口里打标 | 

### 7.2 空间能力（P0 + P1 主体）

| 项 | 实现位置 | 说明 |
|---|---|---|
| 通道改走 WS | `Core/qzone.py:315` `_call()` / `:340` `_ws()` / `:350` `_http()` | 优先复用机器人已有 WS 的 `bot.call_api(action,…)`，HTTP 仅在 WS 失败且配置了地址时兜底一次，成功后切频道 |
| 能力探测 | `Core/qzone.py:271` `refresh()` | `get_version_info` 取版本 → `get_qzone_msg_list {num:1}` 探活；结果缓存 `qzone_probe_ttl`（默认 600s），失败自动换通道重试 |
| 可用性门控 | `Core/engine.py:1846-1850` | 不可用时从 `enabled_keys` 摘除全部 `qzone.*`；`prompts.build_catalog` 只列已启用工具，提示词里的空间行随之消失 |
| official 门控 | `Core/runtime_env.py:38-52` + `Core/qzone.py:253` | `transport=official` → `available()` 恒 False，工具被摘、调用返回明确原因，不产生任何无效 API 调用 |
| 读说说/评论树 | `Core/qzone.py:384` `fetch_posts` / `:421` `fetch_detail` / `:608` `list_posts` / `:628` `post_detail` | `format='snowluma'`，评论树含 `comment_id` / `comment_uin` / 楼中楼 |
| 读好友动态 | `Core/qzone.py:402` `fetch_feeds` / `:658` `list_feeds` | 供 @ 扫描与 `qzone_feeds` 工具 |
| 读配图 | `Core/qzone.py:551` `read_images()` | 下载图片（`qzone_image_max_kb` 单张上限、默认前 4 张）→ 魔数嗅探 → 以图片输入交给视觉模型 |
| 发说说（图文） | `Core/qzone.py:440` `publish()` | `images` ≤9 张，支持 `file://` / `http(s)://` / `base64://`；纯图片可不写正文；成功后登记 `qzone_posts` |
| 评论/回复评论 | `Core/qzone.py:499` `comment()` | `comment_id` + `comment_uin` 必须成对（回复某条评论），可再带 `reply_uin` / `reply_name`；楼中楼只文字 |
| 点赞 | `Core/qzone.py:539` `like()` | `like_qzone` / `unlike_qzone` |
| 工具表 | `Core/builtin_tools.py:87-89`（keys）、`:1249-1330`（schemas）、`:2502-2560`（dispatch） | 6 个新工具：`qzone_list` / `detail` / `images` / `feeds` / `comment` / `like`；`qzone_images` 返回多模态 `ToolMediaResult` |
| 风险门禁 | `Core/builtin_tools.py` `RISKY_TOOL_KEYS` | `qzone.publish` / `delete` / `comment` / `like` 四个写操作都走「仅主人」门禁 |
| 并发批调 | `Core/tool_batch.py` `READONLY_TOOL_KEYS` | `qzone.list/detail/images/feeds/recent` 只读，可与其他只读工具并行 |

### 7.3 空间知会（NapCat 没有空间事件，只能轮询）

| 项 | 实现位置 | 说明 |
|---|---|---|
| 轮询器 | `Core/qzone_watch.py:97` `QzoneWatcher` | `start/stop/_loop/tick`；间隔 `qzone_watch_interval`（默认 300s，最小 30s）；启动先等 ≤30s 再跑第一轮 |
| 被 @ 扫描 | `Core/qzone_watch.py:200` `_scan_feeds()` | `find_mentions()` 匹配 `@昵称` / `@QQ号`（`Core/qzone.py:105`），按 tid 去重 |
| 新评论扫描 | `Core/qzone_watch.py:237` `_scan_comments()` | 先用说说列表的 `comment_num` 判断有没有新评论，变了才拉详情（省一次请求）；`new_comments()`（`Core/qzone.py:150`）排除自己、含楼中楼 |
| 事件队列 | `Core/storage.py:593` `_DDL_V20` + `:3245-3345` | 新表 `qzone_events`（`UNIQUE(tid,kind,comment_id,actor_uin)` 去重、attempts ≤3）与 `qzone_state`（基线/游标/评论数/自身 uid 昵称） |
| 基线 | `Core/qzone_watch.py:190` `_ensure_baseline()` | 首次扫描把「现在」定为基线，**不补历史**，避免启动刷出几十条旧事件 |
| 派发 | `Core/qzone_watch.py:292` `dispatch()` | 每条事件唤醒一轮后台对话：`engine.handle_trigger(..., run_kind='qzone', notice_text=…)`，可 `[静默]`；失败回 pending 重试。**委托身份传主人 QQ**（不是空间里的当事人）：否则 `qzone_comment`/`qzone_like`/`qzone_publish` 会被风险门禁按「非主人」拒绝，等于只能报告不能回应；当事人信息写在通知正文里。派发在 `plugin.group_lock(gid)` 内串行，避免和群内真人轮次/定时苏醒抢同一条历史 |
| 投递目标 | `Core/qzone_watch.py:40` `notify_target()` | `qzone_notify_group` 有值 → 该群；留空 → 主人私聊 |
| 引擎支持 | `Core/engine.py:347` `handle_trigger()` | 新增 `chat_type` / `run_kind` / `source_kind` / `actor_id` / `actor_name` / `notice_text` 关键字参数；私聊知会自动补 `settings.chat_type='private'` |
| 启动接线 | `plugins/deepseek/deepseek.py:176-183`、`:120` | `on_start` 探测成功才 `qzone_watch.start()`；`on_stop` 停轮询（`_seg` 隔离） |

### 7.4 扩展通知（P1 的一部分）

| 事件 | 处理 |
|---|---|
| `notify/poke` | 被戳的是自己才处理：`poke_back_on`（默认开）→ `group_poke` / `friend_poke` 回戳；`poke_ai_on`（默认关）→ 额外触发一轮对话（`run_kind='poke'`） |
| `notify/profile_like` | **明确不响应**：不落库、不回赞、不唤醒模型（`Core/notices.py` 策略表首行即此条） |
| `notify/input_status` | 静默忽略（高频、无信息量） |
| `notify/group_name` | 同步 `groups.group_name`（`group_name_sync_on`），不唤醒 |
| `group_msg_emoji_like` / `essence` / `group_gray_tip` | 消费掉不唤醒（避免点赞刷屏） |
| `bot_offline` | 记 warn，连接层自行重连 |
| 开关 | `notice_ext_on`（总开关，默认开）；关闭后全部回到「静默丢弃」的旧行为 |

### 7.5 框架层

| 项 | 位置 | 说明 |
|---|---|---|
| `message_sent` | `core/events.py:75-81` | 显式丢弃：机器人自己发的消息不进历史、不触发闸门，避免 `reportSelfMessage=true` 时自问自答 |
| Web 状态 | `Core/web.py:32-59`（`_qzone_info`）、`:615`（overview 附带）、`:620`（`POST /api/qzone/probe`） | 仪表盘新增「QQ 空间通道」KPI：可用/不可用 + 悬停看原因，点击重新探测 |
| 模板 | `templates/dashboard.html:15,58-63,74-88` | 同上；`templates/group.html` 工具开关补 6 个新 key |
| 配置 | `Core/config.py:301-310`（DEFAULTS）、`:405-406`（类型集合） | 新增 13 个键：`qzone_probe_ttl` / `qzone_image_max_kb` / `qzone_watch_*` / `qzone_notify_group` / `notice_ext_on` / `poke_back_on` / `poke_ai_on` / `group_name_sync_on` |
| 设置页 | `web.json` | 新增 7 个高频键的表单描述（其余留 config.json 高级项） |
| 提示词 | `Core/prompts.py:485-497` | 空间工具行扩成 9 个，标注「发/删/评论仅主人可指令」 |
| 迁移 | `Core/storage.py:593`、`:741` | `user_version 19 → 20`：建两表 + `qzone_posts.images` 列；幂等（已用 v19 库模拟升级验证） |
| 依赖 | `plugins/deepseek/requirements.txt` | 显式声明 `httpx`（空间配图下载 + HTTP 兜底通道）；代码里仍做了「没有 httpx 就降级到 WS/文本」的保护 |
| 断线补历史 | `Core/backfill.py`（`run()`/`backfill_group()`）、`deepseek.py` `_start_backfill()`/`_run_backfill()`/`_stop_backfill()` | 连接建立后后台补最近历史：最多 20 群 × 30 条 × 5s 超时；只写 `raw_messages`（背景语料/history_query 用），**不进对话窗、不触发 AI 回复**；去重按发送人+正文+±3s；默认开 `history_backfill_on`，official 通道跳过 |
| 原始语料 DAL | `Core/storage.py` `recent_raw_messages()` / `last_raw_time()` | 补拉去重与「本地无历史就不空造」的判断依据 |
| 场景级工具禁用 | `Core/engine.py` `_build_tools(gid, cfg, deny)` / `_run_turn(deny_tools=)` / `_chat_loop(deny_tools=)` / `_exec_one` / `handle_trigger` | 空间知会轮默认 `deny_tools={qzone.publish, qzone.delete}`：从工具表移除、提示词自动消失，且模型硬调时执行期直接返回拒绝文案（提示词 + 执行期双保险）；`qzone_event_write_on` 打开才解禁 |
| 知会轮群级覆盖 | `Core/qzone_watch.py` `dispatch`、`Core/notices.py` `_handle_poke` | 派发前按投递会话叠 `resolve_group()`：模型路由、人设、`window_rounds` 等群级覆盖生效，不再拿全局 cfg 跑 |
| 调试台：投递会话 | `Core/live.py` `state_payload()`、`templates/live/debug.html` `fillGroupSel()` | `/live/api/state` 里固定插入「◆ 空间知会」会话（投递群或主人私聊，标「（私聊）」），选中即按该会话的 group_id 过滤事件流 |
| 调试台：历史回看 | `Core/live_bus.py` `older()`/`oldest_seq()`/`latest_seq()`、`Core/live.py` `history_payload()`、`LiveServer` `GET /live/api/events`、`debug.html` `loadHistory()` | 页面打开先回填最近 200 条（刷新不再空白），顶部「⏮ 加载更早」按 `before` 游标向前翻页；`has_more` 按过滤后的实际剩余判定；范围仅进程内 ring（1000 条），重启清空 |
| 配置（续） | `Core/config.py`（196 键）、`web.json` | 新增 `qzone_event_write_on`（默认 false，慎开：陌生人一次 @ 就能驱动主人权限发说说） |
| 文档 | `docs/history-qzone-guide.md` 第 3 节重写、`README.md` 工具数 73 + 文档索引 | 删掉「v1 不发图 / NapCat 无列表接口」两处过时结论 |

### 7.6 验证

- `python -m pytest tests/ -q` → **133 passed**（基线 95 + 本次 38）。
- 全部改动文件 `py_compile` 通过；Jinja 模板全部可解析；`dashboard.html` 内联 JS 过 `node --check`。
- 迁移：新库 `migrate` 两次幂等；模拟 v19 旧库（无 `images` 列、无两新表）升级到 20 正常。
- 行为用例覆盖：official 下客户端不碰 bot、工具表摘除 `qzone_*`、图文发布走 `send_qzone_msg` 并落库、回复评论必须成对参数、轮询首轮只建基线且历史 @ 不补报、资料赞零副作用、戳一戳只回戳自己、群名同步、`message_sent` 不被当入站消息、空间知会走私聊时 `chat_type=private` 且 `bg_runs.kind=qzone`、9 个空间工具分派、断线补历史只补新消息且 official/关开关时跳过。
- 追加覆盖：知会轮 `deny_tools` 默认禁发/删、`qzone_event_write_on` 解禁、场景禁用后只读工具仍在表里、派发使用投递会话的群级覆盖；调试台 `older()` 游标/群过滤/类型过滤、投递会话在 `state_payload` 打标（私聊与群两种、无目标时 `None`）、`history_payload` 三页翻到底后 `has_more=false`。

### 7.7 本次未做（保留在 P1/P2）

- `group_msg_emoji_like` 的**回赞**（`set_msg_emoji_like`）：只消费不动作，等确认风控与产品口径。
- 群文件上传 / 群成员增减事件缓存、`ocr_image`、精华/公告、合并转发、公共流 14 个 action。
- 断线补历史目前只补 `raw_messages`：不重建对话窗、不补 tool 消息；`message_seq` 游标未持久化，所以每次最多回看 30 条（够覆盖短断线，长断线要靠部署侧降低 `qzone_watch_interval`/提高补拉条数）。
- 空间知会目前只覆盖「被 @」与「新评论」；点赞/删除等动静没有事件也没有轮询价值，未做。
- 空间轮询频率与账号风控的实测：默认 300s，部署后按实际体感调 `qzone_watch_interval`。
- 调试台历史回看只覆盖本次运行的进程内缓冲（1000 条），重启即清空；要跨重启回看需要把事件落库，本次未做。

## 八、风险与注意事项

- **上游已停维护**：NapCat 不再有官方修复，QQ 协议变化随时可能让当前版本失效。对策：锁定 commit/版本、内部镜像 fork、记录来源与许可证；不要假设「升级到最新版」能解决问题。
- **fork 不是长期保障**：fork 可能滞后、失修或被删；空间 action 是逆向产物，与 QQ 客户端强耦合。建议把关键 action 的请求/响应 schema 写成本仓文档或契约测试。
- **LRU 与 message_id**：NapCat 无数据库，`message_id` 是哈希得到的非连续正整数，约 5000 条消息后被清理；`file` 标识同受 LRU 影响。不要把 `message_id` 当长期主键，历史归档必须在入站时落本地库。
- **撤回不可恢复**：已撤回消息无法再次获取，`get_msg` 也拿不到。需要审计/回看的场景必须在消息到达时落库。
- **空间无事件**：点赞/评论/@ 只能轮询；频率过高有账号风控风险，轮询状态要持久化并做去重。
- **空间接口无版本契约**：28 个 action 从未写进官方文档，SnowLuma 格式是 fork 的兼容产物；必须保留能力探测与失败降级，不能把接口当成稳定 API。
- **official 通道无空间能力**：官方开放平台没有空间接口，且不支持私聊与多数 action；空间工具必须在 official 下隐藏。
- **message_sent 依赖配置**：NapCat 的 WS 服务端默认 `reportSelfMessage:false`，不开启永远收不到自己发的消息；开启后注意自回环（模型可能把自己的消息当新输入）。
- **HTTP 服务端默认关闭**：`config/onebot11_<qq>.json` 里 HTTP 服务端 `enable:false`、port 3000；WebUI 默认 6099 + token。基线的空间功能依赖它，是「现网不可用」的直接原因。
- **端口与配置漂移**：NapCat 默认 HTTP 3000 / 正向 WS 3001 / WebUI 6099；框架默认 WS 3001，但本机 `config.json` 仍是 8080。升级或迁移时逐项对齐，避免出现「代码改了但连的还是旧端口」。
- **重连语义**：断线重连会重建 bot 并重载插件；后台轮询、能力探测缓存若挂在实例上，重连后需重新注册或重建，否则表现为「在线但空间工具不可用」。
- **消息段信息损失**：`mface/dice/rps/poke/music` 入站即丢弃，若后续要做表情语义或互动统计，需要补映射。
- **安全与合规**：自动点赞/评论/发说说属于自动化行为，注意平台条款与账号风控；`get_cookies`/`get_credentials` 等凭据类 action 不应接入。

## 九、附录

### 9.1 文档 URL

| 页面            | URL                                                |
| --------------- | -------------------------------------------------- |
| OneBot 总览     | https://napcat.napneko.icu/onebot/index.html       |
| API 列表        | https://napcat.napneko.icu/onebot/api.html         |
| 事件列表        | https://napcat.napneko.icu/onebot/event.html       |
| NapCat 差异实现 | https://napcat.napneko.icu/onebot/napcat.html      |
| 基础事件        | https://napcat.napneko.icu/onebot/basic_event.html |
| 消息段          | https://napcat.napneko.icu/onebot/segment.html     |
| 网络配置        | https://napcat.napneko.icu/onebot/network.html     |
| 基础配置        | https://napcat.napneko.icu/config/basic.html       |
| 版本索引        | https://napcat.napneko.icu/api/                    |

裸路径（不带 `.html`）在 VitePress 下会 404；表中均为核对时可直接打开的地址，版本索引 `/api/` 除外。

### 9.2 源码与 fork

| 对象             | 地址 / 说明                                                   |
| ---------------- | ------------------------------------------------------------- |
| 上游（已清空）   | https://github.com/NapNeko/NapCatQQ（2026-11-12 后仅 README） |
| fork A           | https://github.com/Creep-debug/NapCatQQ                       |
| fork B           | https://github.com/MingTianSang/NapCatQQ                      |
| 空间 action 路径 | `packages/napcat-onebot/action/extends/`（fork 内）           |
| 空间事件目录     | `napcat-onebot/event/`（仅 message/meta/notice/request）      |

迁移或自建镜像时，先核对这两个目录的 action/event 注册表；空间能力只存在于 fork，不在 OneBot 标准或 LuckyLilliaBot 中。

### 9.3 证据索引

| 证据                                  | 结论                                               |
| ------------------------------------- | -------------------------------------------------- |
| `core/events.py:52-73`                | meta 只处理 lifecycle/heartbeat，不派发插件        |
| `core/events.py:75-158`               | message 仅识别 `post_type=='message'`，群/私聊两支 |
| `core/events.py:161-185`              | notice 通用分发，无按类型消费                      |
| `core/events.py:188-212`              | request 通用分发                                   |
| `main.py:252-258`                     | 先按 echo 匹配 RPC，再进 `parse_event`             |
| `main.py:61-70`                       | 正向 WS + Bearer；`official` 走兼容层              |
| `configs.py:31-33`                    | `http_address`/`http_token` 默认空，空间 HTTP 兜底 |
| `configs.py:35-36`                    | `transport` 双通道开关                             |
| `core/bot.py:89-158`                  | 出站统一 `send_msg`                                |
| `core/bot.py:236-289`                 | 通用 `call_api`，Semaphore(10)、30s 超时           |
| `Core/message.py:378-444`             | 段数组解析                                         |
| `Core/message.py:446-520`             | CQ 字符串兜底解析                                  |
| `Core/message.py:28-31`               | 语音/表情/视频/文件占位                            |
| `reply.py:18-23`                      | 引用回复 CQ 头与 id 白名单                         |
| `mentions.py:41`                      | @昵称 → `[CQ:at]` 映射                             |
| `deepseek.py:424-449`                 | 唯一 notice 处理器，仅机器人自己入群               |
| `deepseek.py:450+`                    | 请求审批处理器                                     |
| `HEAD qzone.py:70-98`                 | 空间 HTTP `_post`，Bearer + retcode 校验           |
| `HEAD qzone.py:100-137`               | 仅文字发布，无 images 参数                         |
| `HEAD qzone.py:148-162`               | `recent` 只读本地表                                |
| `HEAD builtin_tools.py:84-86`         | 基线 3 个空间工具映射                              |
| `HEAD builtin_tools.py:2336-2354`     | 空间工具 dispatch；`ctx.qzone is None` 分支不可达  |
| `HEAD storage.py:319-328`             | `qzone_posts` 本地留痕表                           |
| `HEAD README.md:59`                   | 基线工具数 65，与 `TOOL_KEYS` 66 不一致            |
| `core/official/convert_in.py:380-407` | official 只映射两类事件                            |
| `core/official/session.py:220-230`    | official action 白名单与失败文案                   |
| `passive_turn.py:32-59`               | 通道探测与缓存                                     |
| `configs.py:19`                       | 正向 WS 默认端口 3001，与 NapCat 默认一致          |
| `HEAD qzone.py:53-60`                 | 空间 HTTP 地址解析：插件配置优先、根配置回落       |
| `deepseek.py:115`                     | 无条件创建空间客户端，导致工具常驻                 |
| `engine.py:884`                       | 工具上下文注入 `qzone`                             |
| `HEAD prompts.py:472-474`             | 基线空间工具提示词 3 条                            |
