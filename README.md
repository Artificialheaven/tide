# NapCatBot — QQ 机器人框架

基于 **OneBot 协议** 的现代化 QQ 机器人框架，支持插件化扩展、图形化管理界面、Web 远程管理和自动断线重连。

## 特性

- **GUI + Web 双面板** — DearPyGui 桌面端 + Flask Web 管理面板，`-nogui` 切换
- **插件系统** — 热加载/热卸载，独立依赖目录，5 种配置字段类型
- **自动重连** — WebSocket 断线自动重连，指数退避算法
- **事件驱动** — 结构化事件对象（MessageEvent / NoticeEvent / MetaEvent），异步并行处理
- **实时监控** — 消息统计、群/好友列表、系统资源（CPU/内存/磁盘/GPU/网络）
- **Token 认证** — Web 管理面板基于 session 的密码登录

## 环境要求

- Python 3.12+
- Windows / Linux / macOS

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 编辑 config.json（配置 OneBot WebSocket 地址和 Token）
# 3. 启动
python main.py          # GUI 模式
python main.py -nogui   # 无头模式

# 启用 Web 管理面板：config.json 中设置 "enable_web_admin": true
# 访问 http://localhost:8081，使用 config.json 中的 token 登录
```

## 项目结构

```
napcatbot/
├── main.py                 # 入口
├── configs.py              # 配置管理
├── config.json             # 用户配置
├── requirements.txt        # 依赖
│
├── core/
│   ├── bot.py              # Bot 实例（OneBot API 封装）
│   ├── events.py           # 事件解析与分发
│   └── globals.py          # 全局共享状态
│
├── utils/
│   ├── Plugin.py           # 插件基类
│   ├── plugin_loader.py    # 插件管理器（加载/卸载/热重载）
│   └── obj.py              # 数据模型 + 事件类
│
├── ui/
│   ├── main_ui.py          # 主 GUI 窗口
│   ├── system_window.py    # 系统资源监控
│   ├── monitor_window.py   # 消息监控
│   ├── plugin_window.py    # 插件管理
│   └── web/                # Web 管理面板
│       ├── server.py       # Flask + SocketIO
│       ├── auth.py         # Token 认证
│       └── templates/      # HTML 模板
│
├── plugins/
│   ├── hello/              # 示例插件
│   ├── ai/                 # AI 对话插件
│   └── mcs/                # Minecraft RCON 插件
│
└── docs/                   # 文档
```

## 配置项

| 键 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `ws_address` | string | `ws://localhost:8080` | OneBot WebSocket 地址 |
| `token` | string | — | OneBot 认证 Token / Web 面板密码 |
| `show_heartbeat` | bool | `true` | 是否显示心跳日志 |
| `max_reconnect_attempts` | int | `0` | 最大重连次数（0=无限） |
| `reconnect_base_delay` | int | `2` | 重连初始延迟（秒） |
| `reconnect_max_delay` | int | `60` | 重连最大延迟（秒） |
| `enable_web_admin` | bool | `false` | 启用 Web 管理面板 |
| `web_admin_port` | int | `8081` | Web 面板端口 |

## 文档

- [插件编写教程](docs/plugin-guide.md)
- [Bot API 参考](docs/api-reference.md)
- [Web 管理面板](docs/web-admin.md)
