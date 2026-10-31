# 外部触发 / 监测程序指南

DeepSeek 插件可以让 AI 在群里**托管一个监测程序**（例如盯 B 站评论有没有被回复、盯网页变化、盯 RSS 更新）。程序发现事件后，通过一个本机 HTTP「触发接口」回调机器人，机器人就在**对应群**自动跑一轮后台 AI 对话——和定时苏醒完全同构：AI 结合记忆/约定/注入表判断该不该开口，该说就像普通群友一样自然说话，不该说就回复 `[静默]`。

整个功能由两部分组成：

| 部分 | 文件 | 作用 |
|---|---|---|
| 触发服务 | `Core/triggers.py` | 独立轻量 HTTP 服务（标准库，不依赖 Web 管理面），接收外部事件 |
| 监测进程管理 | `Core/watchers.py` | AI 用 `watcher_start` 等工具启动/查询/停止分离式监测进程 |

## 架构与数据流

```
监测脚本（分离子进程，群沙箱内）
   │  检测到事件
   │  POST http://127.0.0.1:8094/trigger  (Bearer token)
   ▼
TriggerServer（守护线程，HTTP 工作线程）
   │  校验 token / 群有效性 / 冷却 → 立即返回 202
   │  call_soon_threadsafe 投递到 bot asyncio 循环
   ▼
TriggerManager.fire()（asyncio 线程）
   │  group_lock 内 → Engine.handle_trigger()
   ▼
合成「系统·外部触发通知」→ 一轮完整后台对话（可调用全部工具）
   ├─ AI 自然开口 → 消息发进群（@ 昵称照样转真 CQ at）
   └─ AI 输出 [静默] → 什么都不发
```

为什么触发服务不用插件自带的 Flask 蓝图：框架给所有插件蓝图强加 session 登录鉴权（外部脚本没有 cookie），而且 GUI 模式下 Web 管理面可能根本没启动。所以触发服务用标准库 `ThreadingHTTPServer` 单独监听。

## 配置（config.json，均在插件配置页可改）

| 键 | 默认值 | 说明 |
|---|---|---|
| `trigger_on` | `true` | 总开关。**全局项，不支持每群覆盖** |
| `trigger_host` | `127.0.0.1` | 监听地址，默认仅本机。只有确实需要别的机器回调时才改 `0.0.0.0`（务必先确认网络可信） |
| `trigger_port` | `8094` | 监听端口 |
| `trigger_token` | 首次启动自动生成 | 32 字符随机 token，只存 gitignored 的 config.json |
| `trigger_cooldown_seconds` | `30` | 同一个 (群, source) 两次触发的最小间隔，防刷屏 |
| `trigger_text_max_chars` | `2000` | 单条事件文本上限 |
| `watchers_max_per_group` | `5` | 每群存活监测进程上限 |

token 可在 Web「全局设置 → 外部触发 / 监测程序」里看到明文并随时重新生成；服务端每次请求实时读配置，**换 token 不用重启**。改 host/port 需要重启插件（重连即生效）。

## HTTP 协议

### `POST /trigger`

请求头（二选一）：

```
Authorization: Bearer <trigger_token>
X-Trigger-Token: <trigger_token>
```

JSON 请求体：

```json
{
  "group_id": "123456789",
  "text": "你在 B 站的评论「xxx」收到回复：……",
  "source": "bili-reply"
}
```

- `group_id`：必填，数字群号字符串，且该群必须已开启机器⼈。
- `text`：必填，事件内容（≤ `trigger_text_max_chars` 字）。
- `source`：可选，监测程序名，40 字符内（中英文/数字/`_-.`）。用于冷却区分和给 AI 的溯源标注。

响应：

| 状态码 | 含义 |
|---|---|
| `202` | 已受理，后台 AI 轮异步执行（不会同步等 AI 说完） |
| `400` | 参数问题（非 JSON / 群号非数字 / text 空或超长 / source 字符合法性） |
| `401` | token 缺失或错误 |
| `403` | 群存在但未开启机器人 |
| `404` | 群不存在（机器人从未在该群登记） |
| `429` | 同 (群, source) 冷却中 |
| `503` | bot 事件循环未运行（离线中） |

### `GET /trigger`

健康检查，不要 token，返回 `{"ok": true, "service": "deepseek-trigger"}`。请求体上限 64KiB。

## 写一个监测脚本

约定：**脚本由 AI 事先用 `fs_write` 写进本群沙箱**（如 `watchers/bili.py`），只能放在群沙箱，不能放 public 共享区。启动时下列环境变量自动注入，脚本里**不要硬编码 token**：

| 环境变量 | 内容 |
|---|---|
| `TRIGGER_URL` | 完整回调地址（如 `http://127.0.0.1:8094/trigger`） |
| `TRIGGER_TOKEN` | Bearer token |
| `TRIGGER_GROUP` | 所属群号 |
| `TRIGGER_SOURCE` | 启动时给的程序名（未命名则为 `watcher-<时间戳>`） |

脚本以 `python -I -X utf8 <script> [args...]` 启动（隔离模式，无 site-packages，无当前目录隐式入路径），stdin 接 DEVNULL，stdout/stderr 合并写入沙箱 `.watchers/<时间戳>.log`（可用 `watcher_logs` 看尾部 8KiB）。子进程环境经过 `_clean_env()` 清洗，**不含任何 API key**。

最小完整示例（轮询型，自带节流，每 60 秒看一次）：

```python
# -*- coding: utf-8 -*-
"""监测示例：盯某条件，命中就回调机器人。"""
import json
import os
import time

import requests

URL = os.environ["TRIGGER_URL"]
TOKEN = os.environ["TRIGGER_TOKEN"]
GROUP = os.environ["TRIGGER_GROUP"]
SOURCE = os.environ.get("TRIGGER_SOURCE", "demo-watcher")

seen = set()


def fetch_new_events():
    """实际使用时替换成 B 站 API / 网页抓取 / RSS 等。"""
    return []


def notify(text: str) -> None:
    resp = requests.post(
        URL,
        timeout=10,
        headers={"Authorization": "Bearer " + TOKEN},
        json={"group_id": GROUP, "source": SOURCE, "text": text},
    )
    print(f"notify -> {resp.status_code}: {text[:60]}", flush=True)


def main():
    print(f"{SOURCE} started, group={GROUP}", flush=True)
    while True:
        try:
            for ev in fetch_new_events():
                key = ev.get("id") or json.dumps(ev, ensure_ascii=False)
                if key in seen:
                    continue
                seen.add(key)
                notify(f"监测到新事件：{ev.get('summary')}")
        except Exception as e:
            # 异常要吞掉并打日志，不能让监测循环直接死掉
            print(f"poll error: {e}", flush=True)
        time.sleep(60)


if __name__ == "__main__":
    main()
```

注意：`python -I` 隔离模式下第三方包（如 `requests`）必须装在解释器的标准 site-packages 里；不想依赖时直接用标准库 `urllib.request` 回调即可。

## AI 侧用法

主人在群里直接用自然语言指挥即可，AI 有四个工具（`watcher.start` / `watcher.stop` 是风险操作，**只有主人 QQ 能指令**；list/logs 群内可用）：

- `watcher_start`：启动沙箱内脚本，可带参数（≤10 个，每个 ≤200 字符）和程序名。返回 id、pid 和回调示例。
- `watcher_list`：查本群（或全部群）监测程序及存活状态。
- `watcher_logs`：看某 id 的日志尾部。
- `watcher_stop`：树杀进程（Windows `taskkill /T /F`，POSIX `killpg`）并标记停止。

Web 面板「后台/轮询 → 监测程序」页也可以查列表、停止、看日志。触发产生的后台轮在「后台运行记录」里以 kind=`trigger`（徽标「外部触发」）留痕，可查看 trace 里的 source 与事件原文。

## 生命周期边界（重要）

- 监测进程是**分离进程**：bot 断线重连、插件热重载都**不会**杀掉它，监测不中断。
- 反过来，v1 **不做开机自启、不做崩溃自动重启**。机器重启或进程自己崩了之后，需要时让主人在群里说一声，AI 用 `watcher_start` 重新拉起即可。
- 插件启动时会对登记为 running 的记录做一次探活对账（进程已死的行自动标 stopped）。
- 触发服务是守护线程，随插件进程退出而关闭。

## 安全说明

- 默认只绑 `127.0.0.1`，同机以外访问不到；改成 `0.0.0.0` 前请确认网络可信，token 是唯一防线。
- token 随机生成、仅存 config.json / 注入子进程环境变量，不入库不进日志；怀疑泄露就在设置页重新生成（即时生效）。
- 服务端有 per-(群, source) 冷却、64KiB 请求体上限、文本长度上限，防止脚本故障刷爆 AI 轮。
- 启动/停止监测进程等同执行代码，走与代码执行相同的主人门禁。
