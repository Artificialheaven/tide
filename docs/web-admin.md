# Web 管理面板

## 启动

`config.json` 中设置：

```json
{
    "enable_web_admin": true,
    "web_admin_port": 8081,
    "token": "你的密码"
}
```

然后正常启动 `python main.py`（GUI 或无头均可）。访问 `http://localhost:8081`。

## 登录

输入 `config.json` 中 `token` 的值作为密码，无需用户名。Session 有效期 24 小时。

## 功能页面

### 📊 仪表盘

Bot ID、消息收发统计、WebSocket 连接状态，每 3 秒自动刷新。

### ⚙️ 配置

修改全局配置（WebSocket 地址、Token、心跳显示、重连参数、Web 面板开关等），保存后需重启生效。

### 🔌 插件

| 操作 | 说明 |
|------|------|
| 🔄 重载 | 卸载并重新加载插件 |
| 启用/禁用 | 开关插件（禁用后消息不再路由到该插件） |
| ⚙️ 配置 | 仅当插件目录存在 `web.json` 时显示，打开模态表单 |

### 💻 状态

CPU / 内存 / 磁盘使用率（含进度条）、网络累计流量，每 3 秒自动刷新。

### 📜 日志

实时日志流（WebSocket 推送），支持颜色标注、自动滚动、清空。

## 插件配置

插件在目录中放置 `web.json` 即可在 Web 面板中获得配置按钮。

### web.json 格式

```json
{
    "config": {
        "字段名": {
            "type": "string|int|checkbox|radiobutton|dropdown",
            "label": "显示标签",
            "default": "默认值",
            "options": ["选项1", "选项2"]  // radiobutton / dropdown 需要
        }
    },
    "call_function": "load_config"
}
```

保存时 Web 面板会：
1. 写入 `plugins/<插件名>/config.json`
2. 调用 `plugin.call_function("<call_function>", config={...})`

## API 列表

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/login` | 登录 `{"password":"..."}` |
| POST | `/api/logout` | 登出 |
| GET | `/api/check_auth` | 检查登录状态 |
| GET | `/api/config` | 读取全局配置 |
| POST | `/api/config` | 保存全局配置 |
| GET | `/api/plugins` | 插件列表 |
| POST | `/api/plugins/<name>/reload` | 重载插件 |
| POST | `/api/plugins/<name>/enable` | 启用插件 |
| POST | `/api/plugins/<name>/disable` | 禁用插件 |
| GET | `/api/plugins/<name>/config` | 读取插件配置模式 |
| POST | `/api/plugins/<name>/config` | 保存插件配置 |
| GET | `/api/monitor` | 消息监控数据 |
| GET | `/api/status` | 系统状态（CPU/内存/磁盘） |
| GET | `/api/logs` | 日志列表 |
| POST | `/api/logs/clear` | 清空日志 |

所有 `/api/*` 接口（除 login/logout/check_auth）均需登录认证。
