"""
Web 管理服务器 — Flask + SocketIO
仅在 -nogui 无头模式下启动
"""

import os
import sys
import threading
import time
import json
from datetime import datetime

import flask
from flask_socketio import SocketIO, emit

from .auth import init_auth, login_required, try_login, logout as do_logout

# ---------- Flask 应用 ----------
_app = flask.Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "templates"),
)
_app.secret_key = os.urandom(24).hex()
_app.config["PERMANENT_SESSION_LIFETIME"] = 3600 * 24  # 24 小时

_socketio = SocketIO(_app, cors_allowed_origins="*", async_mode="threading")
_server_thread = None
_running = False

# ---------- 日志缓冲区（环形） ----------
_log_buffer: list[dict] = []
_log_max = 200


def _add_log_entry(response: str, source: str, content: str, color: str):
    """内部日志记录（同时推送到 WebSocket）"""
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "response": str(response),
        "source": str(source),
        "content": str(content),
        "color": str(color),
    }
    _log_buffer.append(entry)
    if len(_log_buffer) > _log_max:
        _log_buffer.pop(0)

    # 推送到所有 WebSocket 客户端
    try:
        _socketio.emit("log_entry", entry)
    except Exception:
        pass


def web_logger(level: str, source: str, message: str, color: str = "默认"):
    """供 bot 使用的日志回调"""
    _add_log_entry(level, source, message, color)
    print(f"[{level}] [{source}] {message}")


def push_log_entry(response: str, source: str, content: str, color: str = "默认"):
    """供外部模块（events.py 等）直接推送日志到 Web 面板"""
    _add_log_entry(response, source, content, color)


# ============================================================
#  路由：页面
# ============================================================
@_app.route("/")
def index():
    """主页 — 未登录跳转登录页"""
    return flask.render_template("index.html")


@_app.route("/api/login", methods=["POST"])
def api_login():
    data = flask.request.get_json(silent=True) or {}
    password = data.get("password", "")
    from .auth import AUTH_TOKEN
    print(f"[Web管理] 登录尝试 — 输入: '{password}' 期望: '{AUTH_TOKEN}'")
    if try_login(password):
        print("[Web管理] 登录成功")
        return flask.jsonify({"ok": True})
    print("[Web管理] 登录失败: 密码错误")
    return flask.jsonify({"ok": False, "error": "密码错误"}), 403


@_app.route("/api/logout", methods=["POST"])
def api_logout():
    do_logout()
    return flask.jsonify({"ok": True})


@_app.route("/api/check_auth", methods=["GET"])
def api_check_auth():
    from flask import session
    return flask.jsonify({"authenticated": bool(session.get("authenticated"))})


# ============================================================
#  路由：配置
# ============================================================
SECRET_MASK = '********'
_SECRET_KEYS = ('official_app_secret',)


@_app.route("/api/config", methods=["GET"])
@login_required
def api_get_config():
    import configs
    payload = dict(configs.conf)
    for key in _SECRET_KEYS:
        if payload.get(key):
            payload[key] = SECRET_MASK
    return flask.jsonify(payload)


def _validate_config_update(data: dict) -> tuple[dict, str]:
    """校验/规整面板可改的配置，返回 (干净更新, 错误信息)。"""
    import configs
    clean, err = {}, ''
    for key, value in data.items():
        if key not in configs.conf:
            continue
        if key in _SECRET_KEYS and value == SECRET_MASK:
            # 前端回传的是掩码值，表示「不修改」，不能覆盖真实密钥
            continue
        if key == 'official_app_secret':
            clean[key] = str(value or '').strip()
        elif key == 'transport':
            transport = str(value or '').strip().lower()
            if transport not in ('napcat', 'official'):
                return {}, 'transport 只能是 napcat 或 official'
            clean[key] = transport
        elif key == 'web_admin_port':
            try:
                port = int(value)
            except (TypeError, ValueError):
                return {}, 'Web 面板端口必须是整数'
            if not 1 <= port <= 65535:
                return {}, 'Web 面板端口必须在 1-65535 之间'
            clean[key] = port
        elif key == 'web_admin_host':
            host = str(value or '').strip()
            if not host or len(host) > 253 or any(ch.isspace() for ch in host):
                return {}, 'Web 面板监听地址非法（示例：0.0.0.0 或 127.0.0.1）'
            clean[key] = host
        else:
            clean[key] = value
    return clean, err


@_app.route("/api/config", methods=["POST"])
@login_required
def api_update_config():
    import configs
    data = flask.request.get_json() or {}
    clean, err = _validate_config_update(data)
    if err:
        return flask.jsonify({"ok": False, "error": err}), 400
    configs.conf.update(clean)
    configs.save_config()
    return flask.jsonify({"ok": True, "config": configs.conf})


# ============================================================
#  路由：插件
# ============================================================
@_app.route("/api/plugins", methods=["GET"])
@login_required
def api_list_plugins():
    from utils.plugin_loader import get_plugin_manager
    import os as _os
    mgr = get_plugin_manager()
    if not mgr:
        return flask.jsonify({"plugins": {}})
    plugins = mgr.get_plugin_list()
    # 注入 has_web_config 字段
    plugins_dir = _os.path.join(_os.path.dirname(__file__), "..", "..", "plugins")
    for key, info in plugins.items():
        folder = info.get("folder", key)
        web_json = _os.path.join(plugins_dir, folder, "web.json")
        info["has_web_config"] = _os.path.exists(web_json)
        # 检查插件是否提供了 Web 管理页面
        info["has_web_page"] = False
        if key in mgr.plugins:
            try:
                bp = mgr.plugins[key].get_web_blueprint()
                info["has_web_page"] = bp is not None
            except Exception:
                pass
    return flask.jsonify({"plugins": plugins})


@_app.route("/api/plugins/<name>/reload", methods=["POST"])
@login_required
def api_reload_plugin(name):
    from utils.plugin_loader import reload_plugin
    ok = reload_plugin(name)
    return flask.jsonify({"ok": ok})


@_app.route("/api/plugins/<name>/enable", methods=["POST"])
@login_required
def api_enable_plugin(name):
    from utils.plugin_loader import get_plugin_manager
    mgr = get_plugin_manager()
    ok = mgr.enable_plugin(name) if mgr else False
    return flask.jsonify({"ok": ok})


@_app.route("/api/plugins/<name>/disable", methods=["POST"])
@login_required
def api_disable_plugin(name):
    from utils.plugin_loader import get_plugin_manager
    mgr = get_plugin_manager()
    ok = mgr.disable_plugin(name) if mgr else False
    return flask.jsonify({"ok": ok})


@_app.route("/api/plugins/<name>/config", methods=["GET"])
@login_required
def api_get_plugin_config(name):
    """读取插件的 web.json 配置模式 + 当前配置值"""
    from utils.plugin_loader import get_plugin_manager
    import os as _os, json as _json
    mgr = get_plugin_manager()

    # 读取 web.json 获取配置模式
    plugins_dir = os.path.join(os.path.dirname(__file__), "..", "..", "plugins")
    plugin_dir = os.path.join(plugins_dir, name)
    web_json_path = os.path.join(plugin_dir, "web.json")

    schema = {}
    if os.path.exists(web_json_path):
        try:
            with open(web_json_path, "r", encoding="utf-8") as f:
                schema = _json.load(f).get("config", {})
        except Exception:
            pass

    # 读取已保存的配置值
    saved_config = {}
    config_json_path = os.path.join(plugin_dir, "config.json")
    if os.path.exists(config_json_path):
        try:
            with open(config_json_path, "r", encoding="utf-8") as f:
                saved_config = _json.load(f)
        except Exception:
            pass

    # 合并默认值
    current = {}
    for key, field in schema.items():
        current[key] = saved_config.get(key, field.get("default", ""))

    return flask.jsonify({"schema": schema, "config": current})


@_app.route("/api/plugins/<name>/config", methods=["POST"])
@login_required
def api_save_plugin_config(name):
    """保存插件配置并调用插件的 call_function"""
    from utils.plugin_loader import get_plugin_manager
    import os as _os, json as _json
    mgr = get_plugin_manager()

    data = flask.request.get_json() or {}
    if not data:
        return flask.jsonify({"ok": False, "error": "无数据"}), 400

    # 保存到插件目录的 config.json
    plugins_dir = os.path.join(os.path.dirname(__file__), "..", "..", "plugins")
    plugin_dir = os.path.join(plugins_dir, name)
    config_json_path = os.path.join(plugin_dir, "config.json")
    try:
        with open(config_json_path, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return flask.jsonify({"ok": False, "error": str(e)}), 500

    # 从 web.json 读取 call_function 名称
    web_json_path = os.path.join(plugin_dir, "web.json")
    call_func_name = "save_config"  # 默认
    if os.path.exists(web_json_path):
        try:
            with open(web_json_path, "r", encoding="utf-8") as f:
                web_cfg = _json.load(f)
                call_func_name = web_cfg.get("call_function", "save_config")
        except Exception:
            pass

    # 调用插件的 call_function（使用 web.json 中指定的函数名）
    result = None
    if mgr and name in mgr.plugins:
        plugin = mgr.plugins[name]
        try:
            result = plugin.call_function(call_func_name, config=data)
            print(f"[Web管理] 已调用插件 {name}.{call_func_name}(config=...)")
        except Exception as e:
            print(f"[Web管理] 插件 {name}.{call_func_name} 异常: {e}")

    return flask.jsonify({"ok": True, "result": result})


# ============================================================
#  路由：监控
# ============================================================
@_app.route("/api/monitor", methods=["GET"])
@login_required
def api_monitor():
    from core import globals
    bot = globals.bot_instance
    stats = bot.get_stats() if bot else {
        "sent": 0, "received": 0,
        "sent_minute": 0, "received_minute": 0,
        "trend": [],
    }

    return flask.jsonify({
        "sent": stats.get("sent", 0),
        "received": stats.get("received", 0),
        "sent_minute": stats.get("sent_minute", 0),
        "received_minute": stats.get("received_minute", 0),
        "trend": stats.get("trend", []),
        "bot_id": str(globals.bot_id) if globals.bot_id else "—",
        "ws_connected": globals.websocket is not None,
    })


# ============================================================
#  路由：状态
# ============================================================
@_app.route("/api/status", methods=["GET"])
@login_required
def api_status():
    from core import globals
    import psutil

    cpu = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/" if sys.platform != "win32" else "C:\\")
    net = psutil.net_io_counters()

    return flask.jsonify({
        "cpu_percent": cpu,
        "cpu_count": psutil.cpu_count(logical=True),
        "mem_percent": mem.percent,
        "mem_total_gb": round(mem.total / (1024 ** 3), 1),
        "mem_used_gb": round(mem.used / (1024 ** 3), 1),
        "disk_percent": disk.percent,
        "disk_total_gb": round(disk.total / (1024 ** 3), 1),
        "disk_used_gb": round(disk.used / (1024 ** 3), 1),
        "net_sent_mb": round(net.bytes_sent / (1024 ** 2), 1),
        "net_recv_mb": round(net.bytes_recv / (1024 ** 2), 1),
        "ws_connected": globals.websocket is not None,
        "bot_id": str(globals.bot_id) if globals.bot_id else "—",
    })


# ============================================================
#  路由：日志
# ============================================================
@_app.route("/api/logs", methods=["GET"])
@login_required
def api_logs():
    limit = flask.request.args.get("limit", 100, type=int)
    return flask.jsonify({"logs": _log_buffer[-limit:]})


@_app.route("/api/logs/clear", methods=["POST"])
@login_required
def api_clear_logs():
    _log_buffer.clear()
    return flask.jsonify({"ok": True})


# ============================================================
#  SocketIO 事件
# ============================================================
@_socketio.on("connect")
def on_connect():
    print("[Web管理] 客户端已连接")


@_socketio.on("disconnect")
def on_disconnect():
    print("[Web管理] 客户端已断开")


# ============================================================
#  插件 Blueprint 注册
# ============================================================
def register_plugin_blueprints(plugin_manager):
    """遍历所有已加载插件，注册其 Flask Blueprint。

    每个 Blueprint 自动挂载到 /<插件文件夹名>/ 路径下，
    所有路由自动应用登录认证。
    """
    from flask import session as _flask_session, jsonify as _flask_jsonify

    def _check_auth():
        """before_request 处理器：验证登录状态。"""
        if not _flask_session.get("authenticated"):
            return _flask_jsonify({"error": "未登录"}), 401
        return None

    for folder_name, plugin in plugin_manager.plugins.items():
        try:
            bp = plugin.get_web_blueprint()
        except Exception as e:
            print(f"[Web管理] 插件 {folder_name} get_web_blueprint() 异常: {e}")
            continue
        if bp is None:
            continue
        # 自动注入登录认证
        bp.before_request(_check_auth)
        _app.register_blueprint(bp, url_prefix=f"/{folder_name}")
        print(f"[Web管理] 已注册插件 {folder_name} 的 Web 路由: /{folder_name}/")


# ============================================================
#  启动 / 停止
# ============================================================
def start_web_server(host: str = "0.0.0.0", port: int = 8081, token: str = "test"):
    """启动 Web 管理服务器（后台线程）"""
    global _running, _server_thread

    if _running:
        print("[Web管理] 服务器已在运行")
        return

    init_auth(token)
    _running = True

    def _run():
        from .auth import AUTH_TOKEN
        print(f"[Web管理] 启动 Web 管理面板: http://{host}:{port}")
        print(f"[Web管理] Token: {AUTH_TOKEN}")
        _socketio.run(_app, host=host, port=port, debug=False,
                      use_reloader=False, allow_unsafe_werkzeug=True)

    _server_thread = threading.Thread(target=_run, daemon=True)
    _server_thread.start()
    print("[Web管理] 服务器线程已启动")


def stop_web_server():
    """停止 Web 管理服务器"""
    global _running
    _running = False
    print("[Web管理] 服务器已停止")


def web_server_running() -> bool:
    return _running
