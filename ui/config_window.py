import dearpygui.dearpygui as dpg

from core import globals
import configs


def save_config(sender, app_data, user_data):
    """保存配置"""
    show_heartbeat = dpg.get_value("show_heartbeat")
    ws_address = dpg.get_value("ws_address")
    token = dpg.get_value("token")
    transport = str(dpg.get_value("transport")).strip().lower()
    official_app_id = str(dpg.get_value("official_app_id")).strip()
    official_app_secret = str(dpg.get_value("official_app_secret")).strip()

    # 更新全局变量
    globals.show_heart_beat = show_heartbeat

    # 更新配置字典
    configs.conf["show_heartbeat"] = show_heartbeat
    configs.conf["ws_address"] = ws_address
    configs.conf["token"] = token
    configs.conf["transport"] = transport
    configs.conf["official_app_id"] = official_app_id
    configs.conf["official_app_secret"] = official_app_secret

    # 保存到文件
    configs.save_config()

    dpg.set_value("save_status", "配置保存成功！")


def create_config_window():
    """创建配置窗口"""
    with dpg.window(
            label="框架配置",
            width=420,
            height=560,
            tag="config_window",
            pos=[200, 50],
            on_close=lambda: dpg.hide_item("config_window")
    ):
        # 标题
        dpg.add_text("框架配置", color=[255, 255, 0, 255])
        dpg.add_separator()

        # 是否显示心跳选择框
        dpg.add_checkbox(
            label="是否显示心跳",
            default_value=configs.conf.get("show_heartbeat", True),
            tag="show_heartbeat"
        )

        dpg.add_spacer(height=20)

        # WS地址输入框
        dpg.add_text("WS地址:")
        dpg.add_input_text(
            default_value=configs.conf.get("ws_address", "ws://localhost:8080"),
            width=300,
            tag="ws_address",
            hint="请输入WebSocket地址..."
        )

        dpg.add_spacer(height=10)

        # Token令牌输入框
        dpg.add_text("Token令牌:")
        dpg.add_input_text(
            default_value=configs.conf.get("token", ""),
            width=300,
            tag="token",
            password=True,
            hint="请输入访问令牌..."
        )

        dpg.add_spacer(height=10)

        # 连接方式：napcat(OneBot) / 官方 QQ 机器人
        dpg.add_text("连接方式:")
        dpg.add_combo(
            items=["napcat", "official"],
            default_value=configs.conf.get("transport", "napcat"),
            width=300,
            tag="transport"
        )
        dpg.add_text(
            "napcat = OneBot v11；official = QQ 开放平台 v2 群机器人",
            color=[160, 160, 160, 255]
        )

        dpg.add_spacer(height=10)

        dpg.add_text("官方 AppID:")
        dpg.add_input_text(
            default_value=configs.conf.get("official_app_id", ""),
            width=300,
            tag="official_app_id",
            hint="QQ 开放平台机器人 AppID..."
        )

        dpg.add_spacer(height=10)

        dpg.add_text("官方 AppSecret:")
        dpg.add_input_text(
            default_value=configs.conf.get("official_app_secret", ""),
            width=300,
            tag="official_app_secret",
            password=True,
            hint="留空则读取 QQBOT_APP_SECRET 环境变量..."
        )

        dpg.add_spacer(height=30)

        # 保存按钮
        with dpg.group(horizontal=True):
            dpg.add_spacer(width=120)
            dpg.add_button(
                label="保存配置",
                width=100,
                height=40,
                callback=save_config
            )

        dpg.add_spacer(height=10)

        # 保存状态显示
        dpg.add_text("", tag="save_status", color=[0, 255, 0, 255])

    # 默认隐藏配置窗口
    dpg.hide_item("config_window")

    return "config_window"