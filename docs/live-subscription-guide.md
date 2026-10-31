# 实时事件订阅 / 弹幕机 / 调试台指南

DeepSeek 插件在对话过程中有一条实时事件流：群里谁发了什么、哪些话进了大模型、
她每一轮怎么思考、调用了什么工具、工具返回了什么、最后哪些正文真正发进了群——
这些事件以前只有控制台日志和事后落库的运行记录可看。现在通过一个独立的只读
HTTP 服务（默认 `http://127.0.0.1:8095`）对外实时推送，附带两个现成的浏览器页面：

| 出口 | 地址 | 用途 |
|---|---|---|
| SSE 事件流 | `GET /live/events` | 程序订阅原始事件（浏览器 EventSource / curl / 任意语言） |
| 状态接口 | `GET /live/api/state` | 鉴权 JSON：群列表（填下拉框）、在线订阅者数、版本；兼作 token/连通性诊断 |
| 弹幕机 | `GET /live/danmaku` | 两种视图可切换：飘浮弹幕（全屏透明叠加层，OBS 浏览器源，对标 chat.vrp.moe）/ 信息流（调试台样式的左侧滚动时间线） |
| 实时调试台 | `GET /live/debug` | 时间线视图：按群下拉切换、完整思考链、工具参数/结果、token 用量、本轮完整 prompt |
| 健康检查 | `GET /live/healthz` | 无需 token |

## 架构

```
asyncio 线程（引擎/插件）                     HTTP 工作线程
engine / deepseek / loop_run 各挂点             GET /live/events（每连接一个线程）
   │  plugin.live.emit(kind, ...)                │  token 校验 → 订阅总线
   ▼                                              ▼
LiveBus（Core/live_bus.py）── 每订阅者一条有界 Queue(200)
   ├─ ring buffer(1000)：断线重连按 Last-Event-ID 补播
     ├─ 慢消费者丢最旧事件，补发 meta 丢帧通知
     └─ 无订阅者时发布只是一次加锁空转，绝不影响对话主链路
```

与外部触发服务（8094）同范式：标准库 `ThreadingHTTPServer` 起在守护线程，默认只绑
`127.0.0.1`，**复用 `trigger_token` 鉴权**。不用插件 Flask 蓝图的原因：框架给蓝图
强加 session 登录（OBS/EventSource 拿不到 cookie），且 GUI 模式下 Web 管理面可能
没启动。

**粒度限制**：当前 `llm.chat` 是非流式整段调用，事件粒度是「每个工具轮」，不是逐字
token 流——思考链和正文在该轮调用返回时整段出现。逐字流式需改造 LLM 层，列为二期。

## 配置（全局设置页「实时订阅 / 弹幕机」fieldset）

| 键 | 默认值 | 说明 |
|---|---|---|
| `live_on` | `true` | 总开关，**不支持每群覆盖** |
| `live_host` | `127.0.0.1` | 监听地址，默认仅本机 |
| `live_port` | `8095` | 监听端口 |
| `trigger_token` | 首次启动自动生成 | 复用触发服务的 token（SSE 只能走 query 参数） |

改开关/host/port 需要重连（插件重启）生效；token 实时读取，轮换后新连接立即用新
token。设置页上有弹幕机/调试台的直达链接（自动带 token）。

## SSE 协议

```
GET /live/events?token=<trigger_token>
                &groups=123,456        # 可选，群号白名单（逗号分隔），空=全部
                &kinds=chat,model_round,assistant_send   # 可选，事件类型白名单
                &include_prompt=1      # 可选，附带每次模型调用前的完整 messages
```

- 鉴权：`token` query 参数（EventSource 无法自定义请求头）；curl 调试也可用
  `Authorization: Bearer <token>`。失败返回 401，未知 kind 返回 400。
- 响应 `text/event-stream`，帧格式：

  ```
  id: 42
  event: model_round
  data: {"seq":42,"ts":1777000000.12,"kind":"model_round",...}

  ```
- 握手后先发一帧不带 `id` 的问候帧 `event: meta`（`{"hello":true,"groups":[...],
  "include_prompt":false,"version":...}`），回显服务端**实际生效**的群过滤；页面
  据此显示「已连接 · 监听 xxx」，区分「连着但该群没动静」和「根本没连上」。
- 约每 20 秒发一帧 `: ping` 注释心跳。
- **断线补播**：浏览器 EventSource 重连时自动带 `Last-Event-ID` 请求头，服务端从
  ring buffer 补发之后的事件（最多 200 帧；再旧的没有）。
- 慢消费者：浏览器卡住时服务端丢最旧事件保最新，并在恢复后发一帧
  `event: meta`（`{"dropped": N, ...}`）提示弹幕不完整。

curl 试一条：

```bash
curl -N "http://127.0.0.1:8095/live/events?token=你的token&kinds=chat,assistant_send"
```

## 事件目录

公共字段：`seq`（递增）、`ts`（Unix 秒）、`kind`、`group_id`（私聊时为对方 QQ）、
`chat_type`（group/private）、`turn_id`（同一轮对话共享的 12 位 id）。

| kind | 时机 | 主要载荷 |
|---|---|---|
| `chat` | 已开启群/私聊的每条入站消息，**触发闸门前**（被判官拦掉的也有） | sender_id/sender_name、text、at_me、reply、has_media、media[] |
| `gate` | 粗筛/判官判定不回复 | stage（heuristic/judge）、allowed=false、reason |
| `turn_start` | 一轮对话开始 | source（human/work/greet…）、background、forced、触发人、触发 text |
| `prompt` | 任务执行中群友「/ai 打断」插话进入模型 | sender_name、text、how=interrupt |
| `model_round` | 每次 LLM 调用返回（含强制收尾、长跑强制汇报，见 note） | round（0 起）、content、**reasoning 思考链**、tool_calls[{id,name,arguments}]、usage、note（forced_close/loop_speech）、prompt_view（仅订阅 include_prompt 时） |
| `tool_result` | 一次工具派发完成 | round、call_id、name、arguments、ok、result |
| `assistant_send` | 正文**真正发出**时 | text、phase（interim 边干边说 / final 终态 / loop_speech 长跑汇报）、n_chunks |
| `compact` | 长跑上下文自动压缩 | head_msgs、tail_msgs、chars、forced |
| `notice` | 长跑启动/完成/空转自停/异常 | lifecycle（start/done/idle/error）、session_id、text |
| `turn_end` | 一轮对话结束 | silent、final_text |
| `meta` | 传输层（不进补播 ring、无 seq） | 握手问候 `hello:true`+回显 groups/include_prompt；或 dropped 丢帧计数+note |

长字段服务端截断：reasoning 4000 字、prompt_view 8000 字、result/text/content
1000–4000 字，超长带 `…(+N字)` 后缀。

## 弹幕机（飘浮弹幕 / 信息流）

打开 `http://127.0.0.1:8095/live/danmaku`，粘贴一次 trigger_token（存浏览器
localStorage）。顶部有**两种视图可切换**，选择与所有面板设置都记住在 localStorage：

**① 飘浮弹幕**（默认）：全屏透明叠加层，群聊与 AI 输出从右往左飘，做 OBS 浏览器源用。

- 显示类别：群聊消息 / AI 发言 / 工具动作（暗色小字）/ 长跑通知 / 💭思考标记（每轮
  模型调用只飘一条「💭 思考中…」，不泄露思考内容）/ 系统事件（闸门、压缩、插队）；
  默认全开除系统事件；
- 群号过滤、字号、速度（px/s）、不透明度、轨道数；
- 防刷屏：同人同文 3 秒内合并显示 ×N。

**② 信息流**：调试台风格的左侧滚动时间线，按时间顺序列群消息、AI 发言（带
interim/final 阶段标签）、工具调用结果（✓/✗ + 参数摘要）、长跑通知与系统事件，
贴底自动滚动（向上翻则暂停跟随）。**默认不显示思考信息**——模型调用轮次整轮隐藏，
AI 实际输出仍由 `assistant_send` 事件完整呈现；勾选面板上的「显示思考信息」后，
每个 `model_round` 就地展开思考链全文、工具名与本轮正文。

URL 参数：`view=feed` 直接打开信息流（默认/缺省为飘浮），另有 `token`、`groups`、
`bg=transparent`。信息流即使带 `bg=transparent` 也保留半透明深色底条（保证可读）；
`bg=transparent` 主要服务于飘浮弹幕的 OBS 场景。

OBS：添加「浏览器」源，URL 用

```
http://127.0.0.1:8095/live/danmaku?token=你的token&bg=transparent&groups=可选群号
```

不想手拼 URL：在调试台选好群后点「复制弹幕链接」，token 和 groups 已带好，追加
`&bg=transparent` 即可用于 OBS。

`bg=transparent` 时页面全透明且控件自动隐藏（动鼠标可唤出）。OBS 与机器人不在
同一台机器时，把 `live_host` 改成 `0.0.0.0` 并用机器人主机的局域网 IP 访问。

## 实时调试台

`http://127.0.0.1:8095/live/debug`：按回合（turn_id）组织的时间线，支持类型过滤、
暂停/清屏/自动滚动/导出当前事件 JSON。

- **选群**：顶栏「调试群」下拉（来自 `/live/api/state`，已开启/未开启群都列出），
  选定后点「💾 保存并刷新」——群号写进 URL（`?token=...&groups=<群号>`，选「全部群」
  则去掉该参数）整页刷新，所见即所得；URL 可收藏/分享。
- **弹幕机连接**：「🎬 弹幕机」新窗口打开当前群的弹幕机；「复制弹幕链接」复制带
  token 与群号的完整 URL，直接贴进 OBS 浏览器源即可，不用再手填群号。
- **连不上的诊断**：页面加载和 SSE 断线时都会探测 state 接口——token 失效会重新
  弹输入框提示重新粘贴（而不是让 EventSource 无限静默重连），服务没开会明确显示
  「连不上实时服务（8095 未启动？）」；连上后状态栏显示服务端实际监听的群与已收
  事件数，避免「连着但群过滤错了」被误判成订阅坏掉。

勾选「本轮完整 prompt」后重连，
每个 `model_round` 可展开看到**发送前的完整 messages 快照**（系统提示词、记忆注入、
对话历史、工具调用，多模态部分以 `[多模态内容]` 占位）。思考链在调试台默认完整展示；
弹幕机的飘浮视图只有 💭 标记，信息流视图默认也隐藏、需手动勾选「显示思考信息」。

## 安全边界

- 默认绑 `127.0.0.1`：只有本机能连。改成 `0.0.0.0` 意味着**同一局域网内任何拿到
  token 的人**都能实时读取全部群聊、她的思考链、工具参数（含文件路径/内容片段）和
  私聊消息——只在可信网络里这么做。
- 接口纯只读，没有任何能影响机器人行为的端点。
- token 出现在 URL query（SSE 的固有限制）：会留在浏览器历史/反代日志里；分享
  OBS 配置或截图前注意抹掉。token 随时可在设置页重新生成（触发服务同步换用）。
- 弹幕页/调试页 HTML 本身不含数据，token 只存访问者自己的浏览器 localStorage。
