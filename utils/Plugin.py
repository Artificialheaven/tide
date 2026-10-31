import sys
import os
import json
import websockets
import warnings


class Plugin:
    """插件基类"""

    name = "示例插件"
    description = "示例插件，用于测试插件系统和Bot功能"
    version = "1.0.0"

    def __init__(self, websocket: websockets.ClientConnection, bot_instance=None):
        """
        初始化插件
        :param websocket: WebSocket连接
        :param bot_instance: Bot实例（PluginBotWrapper，自动注入插件名）
        """
        self.user_id = None
        self.nickname = None
        self.websocket = websocket
        self.bot = bot_instance
        self.funs = []
        self._plugin_config = {}  # 插件配置缓存
        self._collect_decorated_functions()

    def _collect_decorated_functions(self):
        """收集所有被装饰器标记的函数"""
        for attr_name in dir(self):
            attr = getattr(self, attr_name)
            if hasattr(attr, '_plugin_event'):
                event_info = attr._plugin_event
                self.funs.append({
                    "listen": event_info["event_type"],
                    "function": attr
                })

    @staticmethod
    def on_message(event_type="message"):
        """
        消息事件装饰器
        :param event_type: 事件类型，默认为"message"
        """
        def wrapper(func):
            func._plugin_event = {
                "event_type": event_type
            }
            return func
        return wrapper

    @staticmethod
    def on_notice(event_type="notice"):
        """
        通知事件装饰器
        :param event_type: 事件类型，默认为"notice"
        """
        def wrapper(func):
            func._plugin_event = {
                "event_type": event_type
            }
            return func
        return wrapper

    @staticmethod
    def on_request(event_type="request"):
        """
        请求事件装饰器（好友申请 / 加群申请 / 群邀请）
        :param event_type: 事件类型，默认为"request"
        """
        def wrapper(func):
            func._plugin_event = {
                "event_type": event_type
            }
            return func
        return wrapper

    async def on_start(self):
        """
        插件启动时调用（首次加载时自动执行）
        """
        pass

    async def on_stop(self):
        """插件停止前调用——热重载、连接断开、程序退出时触发。

        可重写为同步或异步方法（框架两种都支持）。
        """
        pass

    def register(self) -> list:
        """
        返回事件监听器列表
        :return: list of {"listen": event_type, "function": callable}
        """
        return self.funs

    def regisiter(self) -> list:
        """
        已弃用：请使用 register()
        """
        warnings.warn(
            "regisiter() is deprecated, use register() instead",
            DeprecationWarning,
            stacklevel=2
        )
        return self.register()

    # ============================================================
    #  插件配置与 RPC
    # ============================================================
    def get_web_blueprint(self):
        """返回插件的 Flask Blueprint，用于注册自定义 Web 路由。

        框架会自动将 Blueprint 挂载到 /<插件文件夹名>/ 路径下，
        并为所有路由自动应用登录认证（login_required）。

        返回 None 表示该插件不需要 Web 路由。

        用法示例::

            import flask
            bp = flask.Blueprint('my_plugin', __name__)

            @bp.route('/api/data')
            def api_data():
                return flask.jsonify({"ok": True})

            return bp
        """
        return None

    def call_function(self, name: str, **kwargs):
        """
        插件间 RPC 调用入口 — 由 Web 管理面板或框架调用。
        子类可重写以响应特定函数名。

        :param name: 函数名（如 "save_config"）
        :param kwargs: 参数
        :return: 任意返回值，默认返回 None
        """
        return None

    def get_config(self) -> dict:
        """获取当前配置（从文件或默认值）"""
        try:
            config_file = os.path.join(
                os.path.dirname(sys.modules[self.__class__.__module__].__file__),
                "config.json"
            )
            if os.path.exists(config_file):
                with open(config_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            if self.bot and hasattr(self.bot, 'Logger'):
                self.bot.Logger.warning("框架", self.name, f"读取配置失败: {e}")
            else:
                print(f"[{self.name}] 读取配置失败: {e}")
        return {}

    def save_config(self, data: dict):
        """保存配置到插件目录的 config.json"""
        try:
            config_file = os.path.join(
                os.path.dirname(sys.modules[self.__class__.__module__].__file__),
                "config.json"
            )
            with open(config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._plugin_config = data
        except Exception:
            print(f"[{self.name}] 保存配置失败")
