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
    # 静态资源独立放在 ui/web/static/（不要把 templates/ 当静态根：那样模板源码
    # 会以 /static/*.html 直接暴露）。页面里统一引用 /static/...，因此显式指定
    # static_url_path，避免 Flask 用目录基名推导出别的前缀导致全站 CSS/JS 404。
    static_folder=os.path.join(os.path.dirname(__file__), "static"),
    static_url_path="/static",
)
_app.secret_key = os.urandom(24).hex()
_app.config["PERMANENT_SESSION_LIFETIME"] = 3600 * 24  # 24 小时

_socketio = SocketIO(_app, cors_allowed_origins="*", async_mode="threading")
_server_thread = None
_running = False
# 已注册的 Blueprint：plugin_key → (蓝图名, 注册代数)。
# 热重载会重新导入插件模块，蓝图里闭包的是旧模块函数，
# 因此每次注册都要先摘掉上一代蓝图再挂新的（url_prefix 不变，页面 URL 不变）。
_registered_blueprints: dict[str, tuple[str, int]] = {}
_blueprint_generation = 0
_blueprint_lock = threading.Lock()

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
_SECRET_KEYS = ('official_app_secret', 'web_admin_token')

# 面板可改的布尔/整型键及范围（其余官方细项不在面板暴露）
_BOOL_KEYS = frozenset({
    'show_heartbeat', 'use_plugin_venv', 'enable_web_admin',
    'official_sandbox', 'official_allow_active',
    'official_enable_member_enrich', 'official_passive_pool_on',
})
_INT_RANGES = {
    'web_admin_port': (1, 65535),
    'max_reconnect_attempts': (0, 10 ** 9),
    'reconnect_base_delay': (0, 86400),
    'reconnect_max_delay': (0, 7 * 86400),
    'official_passive_window_seconds': (30, 295),
    'official_max_passive_seq': (1, 5),
    'official_passive_pool_size': (1, 512),
    'official_request_timeout': (5, 120),
}


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
        if key in _SECRET_KEYS:
            clean[key] = str(value or '').strip()
        elif key in _BOOL_KEYS:
            # 脚本/旧前端会把 false 传成字符串，bool("false") == True 会静默打开开关
            if isinstance(value, str):
                clean[key] = value.strip().lower() in ('1', 'true', 'yes', 'on')
            else:
                clean[key] = bool(value)
        elif key in _INT_RANGES:
            try:
                number = int(value)
            except (TypeError, ValueError):
                return {}, f'{key} 必须是整数'
            low, high = _INT_RANGES[key]
            if not low <= number <= high:
                return {}, f'{key} 必须在 {low}-{high} 之间'
            clean[key] = number
        elif key == 'transport':
            transport = str(value or '').strip().lower()
            if transport not in ('napcat', 'official'):
                return {}, 'transport 只能是 napcat 或 official'
            clean[key] = transport
        elif key == 'official_at_all_mode':
            mode = str(value or '').strip().lower()
            if mode not in ('native', 'text', 'drop'):
                return {}, 'official_at_all_mode 只能是 native/text/drop'
            clean[key] = mode
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
    if ok:
        # 热重载后必须重新挂蓝图：否则 Flask 里留着的还是首代 Core/web 模块函数
        try:
            from utils.plugin_loader import get_plugin_manager
            mgr = get_plugin_manager()
            if mgr is not None:
                register_plugin_blueprints(mgr)
        except Exception as e:
            print(f"[Web管理] 插件 {name} 热重载后重新注册 Web 路由失败: {e}")
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

    # 先确认插件已加载：未加载时前端提交的只是 web.json 子集，
    # 整文件覆盖会永久丢掉 llm_providers/api_key 等未登记键
    if not mgr or name not in mgr.plugins:
        return flask.jsonify(
            {"ok": False, "error": "插件未加载，配置未保存（请先重载插件）"}), 409
    plugin = mgr.plugins[name]

    plugins_dir = os.path.join(os.path.dirname(__file__), "..", "..", "plugins")
    plugin_dir = os.path.join(plugins_dir, name)
    config_json_path = os.path.join(plugin_dir, "config.json")

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

    # 先让插件合并/校验（内存态）；失败则完全不落盘
    result = None
    try:
        result = plugin.call_function(call_func_name, config=data)
    except Exception as e:
        print(f"[Web管理] 插件 {name}.{call_func_name} 异常: {e}")
        return flask.jsonify({"ok": False,
                              "error": f"插件保存失败: {e}"}), 500
    if isinstance(result, dict) and result.get("ok") is False:
        return flask.jsonify(result), 400

    # 与磁盘旧配置合并后再原子写：保留 web.json 未登记的键
    merged = {}
    try:
        with open(config_json_path, "r", encoding="utf-8") as f:
            merged = _json.load(f)
    except Exception:
        merged = {}
    if not isinstance(merged, dict):
        merged = {}
    merged.update(data)
    try:
        tmp_path = config_json_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            _json.dump(merged, f, ensure_ascii=False, indent=2)
        _os.replace(tmp_path, config_json_path)
    except Exception as e:
        return flask.jsonify({"ok": False, "error": str(e)}), 500

    print(f"[Web管理] 已调用插件 {name}.{call_func_name}(config=...)")
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
def _purge_blueprint(bp_name: str) -> None:
    """把某个已注册的 Blueprint 从 Flask app 上摘掉。

    Flask 没有 unregister_blueprint，只能手动清理它登记过的 url rule、
    view 函数和各类请求回调，再重建路由匹配器。仅用于插件热重载后的替换。
    """
    if not bp_name:
        return
    prefix = bp_name + '.'
    removed_rule = False
    for rule in list(_app.url_map.iter_rules()):
        endpoint = str(getattr(rule, 'endpoint', '') or '')
        if endpoint != bp_name and not endpoint.startswith(prefix):
            continue
        try:
            _app.url_map._rules.remove(rule)
        except ValueError:
            continue
        removed_rule = True
        rules = _app.url_map._rules_by_endpoint.get(endpoint)
        if rules:
            try:
                rules.remove(rule)
            except ValueError:
                pass
            if not rules:
                _app.url_map._rules_by_endpoint.pop(endpoint, None)
        _app.view_functions.pop(endpoint, None)
    if removed_rule:
        # 直接改 _rules 不会置 Map._remap，重建匹配器让旧路径立刻不可匹配
        from werkzeug.routing.matcher import StateMachineMatcher
        matcher = StateMachineMatcher(_app.url_map.merge_slashes)
        for rule in _app.url_map._rules:
            matcher.add(rule)
        matcher.update()
        _app.url_map._matcher = matcher

    # 蓝图级 before/after/teardown/context/error 回调：注册时按蓝图名合并进 app
    for mapping in (_app.before_request_funcs, _app.after_request_funcs,
                    _app.teardown_request_funcs, _app.url_default_functions,
                    _app.url_value_preprocessors,
                    _app.template_context_processors):
        for key in list(mapping.keys()):
            if isinstance(key, str) and (key == bp_name
                                         or key.startswith(prefix)):
                mapping.pop(key, None)
    for key in list(_app.error_handler_spec.keys()):
        if isinstance(key, str) and (key == bp_name
                                     or key.startswith(prefix)):
            _app.error_handler_spec.pop(key, None)
    # Jinja 模板加载器在渲染时遍历 app.blueprints，摘掉即不再加载旧模板目录
    _app.blueprints.pop(bp_name, None)


def register_plugin_blueprints(plugin_manager):
    """遍历所有已加载插件，注册（已注册过的则替换成当前这一代）Flask Blueprint。

    每个 Blueprint 自动挂载到 /<插件文件夹名>/ 路径下，
    所有路由自动应用登录认证。

    热重载后插件模块被重新导入，旧蓝图里闭包的是被替换掉的旧模块函数；
    因此这里记录 plugin_key → (蓝图名, 代数)，每次调用先摘旧蓝图再挂新蓝图。
    """
    global _blueprint_generation
    from flask import session as _flask_session, jsonify as _flask_jsonify

    def _check_auth():
        """before_request 处理器：验证登录状态。"""
        if not _flask_session.get("authenticated"):
            return _flask_jsonify({"error": "未登录"}), 401
        return None

    with _blueprint_lock:
        # Flask 3.x 处理过首个请求后会禁止再调用 register_blueprint；
        # 热重载是运行期行为，这里临时复位该标志（它只用于 setup 校验），
        # 注册完成后恢复原值，不影响请求分发。
        first_request_done = getattr(_app, '_got_first_request', False)
        _app._got_first_request = False
        try:
            plugins = dict(getattr(plugin_manager, 'plugins', None) or {})
            # 已卸载/禁用的插件：摘掉它残留的旧蓝图，避免继续挂旧页面
            for folder_name in list(_registered_blueprints.keys()):
                if folder_name in plugins:
                    continue
                old_name = _registered_blueprints.pop(folder_name)[0]
                _purge_blueprint(old_name)
                print(f"[Web管理] 已移除插件 {folder_name} 的 Web 路由: "
                      f"/{folder_name}/")

            for folder_name, plugin in plugins.items():
                entry = _registered_blueprints.pop(folder_name, None)
                old_name, old_gen = entry if entry else ('', 0)
                try:
                    bp = plugin.get_web_blueprint()
                except Exception as e:
                    # 取新蓝图失败时保留旧蓝图：可能只是临时异常，总比页面 404 好
                    print(f"[Web管理] 插件 {folder_name} "
                          f"get_web_blueprint() 异常: {e}")
                    if old_name:
                        _registered_blueprints[folder_name] = (old_name,
                                                               old_gen)
                    continue
                _purge_blueprint(old_name)
                if bp is None:
                    print(f"[Web管理] 插件 {folder_name} 未提供 Web 页面，"
                          f"已移除旧路由: /{folder_name}/")
                    continue
                # 自动注入登录认证（必须在 register 前，注册后蓝图会拒绝再改）
                bp.before_request(_check_auth)
                try:
                    _app.register_blueprint(bp, url_prefix=f"/{folder_name}")
                except ValueError as e:
                    print(f"[Web管理] 插件 {folder_name} "
                          f"Blueprint 名称冲突: {e}")
                    continue
                _blueprint_generation += 1
                _registered_blueprints[folder_name] = (bp.name,
                                                       _blueprint_generation)
                print(f"[Web管理] 已注册插件 {folder_name} 的 Web 路由: "
                      f"/{folder_name}/")
        finally:
            _app._got_first_request = first_request_done


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

    def _run():
        from .auth import AUTH_TOKEN
        print(f"[Web管理] 启动 Web 管理面板: http://{host}:{port}")
        print(f"[Web管理] Token: {AUTH_TOKEN}")
        _socketio.run(_app, host=host, port=port, debug=False,
                      use_reloader=False, allow_unsafe_werkzeug=True)

    _server_thread = threading.Thread(target=_run, daemon=True)
    _server_thread.start()
    _running = True
    print("[Web管理] 服务器线程已启动")


def stop_web_server():
    """停止 Web 管理服务器（真正停 socketio 并 join 线程）"""
    global _running, _server_thread
    _running = False
    try:
        _socketio.stop()
    except Exception:
        pass
    t = _server_thread
    if t is not None and t.is_alive():
        t.join(timeout=5)
    _server_thread = None
    print("[Web管理] 服务器已停止")


def web_server_running() -> bool:
    return _running
