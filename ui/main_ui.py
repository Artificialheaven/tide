import dearpygui.dearpygui as dpg
import sys
from ui.system_window import show_system_window
from ui.config_window import create_config_window
from ui.plugin_window import create_plugin_window, refresh_plugin_list
from ui.monitor_window import show_monitor_window
from ui.report_window import show_report_window
from core import globals

if globals.no_gui:
    print("[系统] 无GUI模式，跳过UI初始化")

    def add_log(response, source, content, color):
        """无GUI模式下的日志输出 — 同时推送到 Web 管理面板"""
        print(f"[{response}] [{source}] {content}")
        try:
            from ui.web.server import push_log_entry
            push_log_entry(str(response), str(source), str(content), color)
        except Exception:
            pass

else:
    # 初始化DVP
    dpg.create_context()
    dpg.create_viewport(title='Napcat', width=1050, height=650)
    dpg.setup_dearpygui()

    looping = False

    # 设置中文字体
    def setup_chinese_font():
        """设置中文字体"""
        with dpg.font_registry():
            # 尝试加载系统中文字体，Windows系统
            font_paths = [
                "C:/Windows/Fonts/simsun.ttc",  # Windows宋体
                "C:/Windows/Fonts/msyh.ttc",  # Windows微软雅黑
                "C:/Windows/Fonts/simhei.ttf",  # Windows黑体
                "/System/Library/Fonts/PingFang.ttc",  # macOS苹方
                "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",  # Linux文泉驿
                "./NotoSansSC-Regular.ttf"  # Google Noto Sans
            ]

            font_added = False
            for font_path in font_paths:
                try:
                    # 添加中文字体
                    with dpg.font(font_path, 16) as default_font:
                        # 添加中文字符范围
                        dpg.add_font_range_hint(dpg.mvFontRangeHint_Chinese_Full)
                        # 设置默认字体
                        dpg.bind_font(default_font)
                        print(f"已成功加载字体: {font_path}")
                        font_added = True
                        break
                except:
                    print(f"无法加载字体: {font_path}")
                    continue

            # 如果所有系统字体都失败，尝试使用基本字体并打印警告
            if not font_added:
                print("警告: 未找到中文字体，中文可能显示为乱码")
                print("请下载 NotoSansSC-Regular.ttf 到项目文件夹")

                # 尝试使用默认字体
                with dpg.font("./NotoSansSC-Regular.ttf", 16) as default_font:
                    dpg.bind_font(default_font)


    # 调用字体设置函数
    setup_chinese_font()

    # 存储窗口ID
    config_window = None
    plugin_window = None

    # 日志数据 - 现在包含颜色信息
    log_data = [
        {
            "id": 1,
            "time": "-",
            "response": "框架",
            "source": "DearQQ",
            "content": "系统正在载入中",
            "color": "绿色"
        },
        {
            "id": 2,
            "time": "-",
            "response": "框架",
            "source": "DearQQ",
            "content": "广告位招商",
            "color": "紫色"
        },
        {
            "id": 3,
            "time": "-",
            "response": "框架",
            "source": "DearQQ",
            "content": "广告位招商",
            "color": "橙色"
        },
        {
            "id": 4,
            "time": "-",
            "response": "框架",
            "source": "DearQQ",
            "content": "广告位招商",
            "color": "橙色"
        },
        {
            "id": 5,
            "time": "-",
            "response": "框架",
            "source": "DearQQ",
            "content": "广告位招商",
            "color": "橙色"
        }
    ]

    log_length = 5

    # 颜色映射
    color_map = {
        "默认": [255, 255, 255, 255],
        "白色": [255, 255, 255, 255],
        "红色": [255, 100, 100, 255],
        "绿色": [100, 255, 100, 255],
        "蓝色": [100, 100, 255, 255],
        "黄色": [255, 255, 100, 255],
        "紫色": [255, 100, 255, 255],
        "橙色": [255, 165, 0, 255],
        "青色": [0, 255, 255, 255]
    }


    # 添加日志功能
    def add_log_log():
        """添加新的日志"""
        with dpg.window(label="添加日志", width=400, height=350, tag="add_log_window", pos=[300, 150]):
            dpg.add_text("添加新日志")
            dpg.add_separator()

            # 响应类型选择
            response_types = ["200 OK", "404", "500", "403", "401", "302"]
            status_color_map = {
                "200 OK": [100, 255, 100, 255],
                "404": [255, 255, 100, 255],
                "500": [255, 100, 100, 255],
                "403": [255, 165, 0, 255],
                "401": [255, 165, 0, 255],
                "302": [100, 100, 255, 255]
            }

            # 响应类型选择
            dpg.add_text("响应类型:")
            response_combo = dpg.add_combo(
                items=response_types,
                default_value="200 OK",
                width=200
            )

            dpg.add_spacer(height=10)

            # 来源选择
            sources = ["服务器", "客户端", "API", "数据库", "中间件", "网络"]
            dpg.add_text("来源:")
            source_combo = dpg.add_combo(
                items=sources,
                default_value="服务器",
                width=200
            )

            dpg.add_spacer(height=10)

            # 内容输入
            dpg.add_text("日志内容:")
            content_input = dpg.add_input_text(
                default_value="",
                width=300,
                height=100,
                multiline=True,
                hint="请输入日志内容..."
            )

            dpg.add_spacer(height=20)

            # 颜色标签选择
            dpg.add_text("颜色标记:")
            color_combo = dpg.add_combo(
                items=list(color_map.keys()),
                default_value="绿色",
                width=200
            )

            dpg.add_spacer(height=20)

            # 按钮
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="添加日志",
                    width=120,
                    height=40,
                    callback=lambda: add_new_log(
                        response_combo,
                        source_combo,
                        content_input,
                        color_combo
                    )
                )
                dpg.add_button(
                    label="取消",
                    width=80,
                    height=40,
                    callback=lambda: dpg.delete_item("add_log_window")
                )


    def add_new_log(response_combo, source_combo, content_input, color_combo):
        """添加新的日志到列表"""
        response = dpg.get_value(response_combo)
        source = dpg.get_value(source_combo)
        content = dpg.get_value(content_input)
        color = dpg.get_value(color_combo)

        if not content.strip():
            dpg.set_value("add_log_status", "日志内容不能为空")
            return

        # 模拟插入新日志（实际应该添加到数据库或文件）
        import datetime
        global log_length
        new_id = log_length + 1
        log_length = log_length + 1
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        new_log = {
            "id": new_id,
            "time": current_time,
            "response": response,
            "source": source,
            "content": content,
            "color": color
        }

        # 添加到日志数据显示
        color_value = color_map.get(color, color_map["默认"])

        with dpg.table_row(parent="log_table"):
            dpg.add_text(str(new_id))
            dpg.add_text(current_time)
            dpg.add_text(response, color=color_value)
            dpg.add_text(source)
            dpg.add_text(content, color=color_value)

        print(f"添加日志成功: ID={new_id}, 响应={response}, 来源={source}, 颜色={color}")

        # 关闭添加日志窗口
        dpg.delete_item("add_log_window")


    def add_log(response, source, content, color):

        if globals.no_gui:
            # 无GUI模式下直接打印日志
            print(f"[{response}] [{source}] {content}")
            return

        """添加新的日志到列表"""
        if not content.strip():
            return

        # 模拟插入新日志（实际应该添加到数据库或文件）
        import datetime
        global log_length
        new_id = log_length + 1
        log_length = log_length + 1
        current_time = datetime.datetime.now().strftime("%H:%M:%S")

        new_log = {
            "id": new_id,
            "time": current_time,
            "response": response,
            "source": source,
            "content": content,
            "color": color
        }

        # 添加到日志数据显示
        color_value = color_map.get(color, color_map["默认"])

        with dpg.table_row(parent="log_table"):
            dpg.add_text(str(new_id))
            dpg.add_text(current_time)
            dpg.add_text(response, color=color_value)
            dpg.add_text(source)
            dpg.add_text(content, color=color_value)

        print(f"添加日志成功: ID={new_id}, 响应={response}, 来源={source}, 颜色={color}")
        dpg.set_value('log_num', f"日志数量: {new_id}")


    def clear_logs():
        """清除所有日志"""
        global log_length
        # 删除表格中的所有行
        if dpg.does_item_exist("log_table"):
            children = dpg.get_item_children("log_table", 1)
            for child in children:
                dpg.delete_item(child)

        # 重置日志计数器
        log_length = 0
        dpg.set_value('log_num', f"日志数量: 0")
        print("日志已清除")


    # 窗口显示函数
    def show_config_window():
        """显示配置窗口"""
        global config_window
        if config_window is None:
            config_window = create_config_window()
        else:
            dpg.show_item(config_window)


    def show_plugin_window():
        """显示插件窗口"""
        global plugin_window
        if plugin_window is None:
            plugin_window = create_plugin_window()
        else:
            # 刷新插件列表
            refresh_plugin_list()
            dpg.show_item(plugin_window)


    def on_close(sender, app_data, user_data):
        """窗口关闭回调函数"""
        global looping
        print("\n[系统] 检测到窗口关闭，正在退出程序...")

        # 1. 设置循环标志为 False，停止主循环
        looping = False

        # 2. 清理 Bot 实例和 WebSocket 连接
        try:
            from core import globals
            if globals.bot_instance:
                print("[系统] 正在关闭 Bot 实例...")
                globals.bot_instance = None

            if globals.websocket:
                print("[系统] 正在关闭 WebSocket 连接...")
                # 注意：这里不直接关闭 websocket，让 asyncio 任务自然结束
                globals.websocket = None
        except Exception as e:
            print(f"[系统] 清理资源时出错: {e}")

        # 3. 停止 DearPyGui
        print("[系统] 正在停止 UI...")
        dpg.stop_dearpygui()

        # 4. 强制退出程序
        print("[系统] 程序已退出")
        import os
        os._exit(0)  # 使用 os._exit 强制退出，不等待其他线程


    class UI:
        def __init__(self):
            global looping
            looping = True

            # 创建主窗口，并设置关闭回调
            with dpg.window(label="NatCat", tag="main_window", width=1000, height=600, on_close=on_close):
                # 主窗口不显示标题栏
                dpg.set_item_pos("main_window", [0, 0])

                with dpg.group(horizontal=True):
                    # 左侧表格区域 (800x600)
                    with dpg.child_window(width=800, height=600):
                        # 表格工具栏
                        with dpg.group(horizontal=True):
                            dpg.add_text("日志列表 (800x600)", tag="name")
                            dpg.add_spacer(width=530)
                            # 隐藏添加日志按钮（创建但不显示）
                            add_log_btn = dpg.add_button(
                                label="添加日志",
                                width=100,
                                height=30,
                                callback=add_log_log
                            )
                            dpg.hide_item(add_log_btn)  # 隐藏按钮

                            # 清除按钮（正常使用）
                            dpg.add_button(
                                label="清除",
                                width=80,
                                height=30,
                                callback=clear_logs
                            )

                        dpg.add_separator()

                        # 创建日志表格（表头默认固定在滚动时可见）
                        with dpg.table(
                                header_row=True,
                                resizable=True,
                                reorderable=False,
                                hideable=False,
                                tag="log_table",
                                policy=dpg.mvTable_SizingFixedFit,
                                height=520,
                                scrollY=False
                        ):
                            # 设置表格列
                            dpg.add_table_column(label="序号", width_fixed=True, init_width_or_weight=30)
                            dpg.add_table_column(label="时间", width_fixed=True, init_width_or_weight=80)
                            dpg.add_table_column(label="响应", width_fixed=True, init_width_or_weight=100)
                            dpg.add_table_column(label="来源", width_fixed=True, init_width_or_weight=100)
                            dpg.add_table_column(label="内容", width_stretch=True)

                            # 添加现有日志数据
                            for log in log_data:
                                color_value = color_map.get(log["color"], color_map["默认"])

                                with dpg.table_row():
                                    dpg.add_text(str(log["id"]))
                                    dpg.add_text(log["time"])
                                    dpg.add_text(log["response"], color=color_value)
                                    dpg.add_text(log["source"])
                                    dpg.add_text(log["content"], color=color_value)


                    # 右侧按钮区域 (200x600)
                    with dpg.child_window(width=200, height=600):

                        # 系统信息
                        dpg.add_separator()
                        dpg.add_text("系统状态:", color=[150, 150, 255, 255])
                        dpg.add_text("运行正常", color=[0, 255, 0, 255])
                        dpg.add_text(f"日志数量: {len(log_data)}", color=[200, 200, 200, 255], tag='log_num')
                        dpg.add_text("更新时间: 实时", color=[200, 200, 200, 255])

                        # 系统标题
                        dpg.add_text("系统功能", color=[255, 255, 0, 255])
                        dpg.add_separator()

                        # 功能按钮
                        functions = [
                            ("配置", show_config_window, "系统配置管理"),
                            ("插件", show_plugin_window, "插件管理"),
                            ("监控", show_monitor_window, "实时监控面板"),
                            ("报表", show_report_window, "统计报表"),
                            ("状态", show_system_window, "系统运行状态"),
                            ("刷新", lambda: print("刷新日志"), "刷新日志内容")
                        ]

                        for btn_text, callback, tooltip in functions:
                            dpg.add_button(
                                label=btn_text,
                                width=180,
                                height=50,
                                callback=callback
                            )
                            dpg.add_text(tooltip, color=[200, 200, 200, 255], indent=10)
                            dpg.add_spacer(height=10)

                        dpg.add_spacer(height=20)

        def setup(self):
            global looping
            # 设置主窗口
            dpg.set_primary_window("main_window", True)

            # 显示主视口
            dpg.show_viewport()
            dpg.start_dearpygui()
            dpg.destroy_context()


