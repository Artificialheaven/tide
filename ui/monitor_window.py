import dearpygui.dearpygui as dpg
from core import globals


# 监控窗口与各显示元素的 tag
MONITOR_WINDOW = "monitor_window"


def update_monitor_display():
    """更新监控显示（总数 / 当前分钟 / 趋势图全部读自 StatsTracker）"""
    if not globals.bot_instance:
        return

    stats = globals.bot_instance.get_stats()

    # 更新总数
    if dpg.does_item_exist("monitor_recv_total"):
        dpg.set_value("monitor_recv_total", str(stats['received']))
    if dpg.does_item_exist("monitor_sent_total"):
        dpg.set_value("monitor_sent_total", str(stats['sent']))

    # 更新当前分钟
    if dpg.does_item_exist("monitor_recv_current"):
        dpg.set_value("monitor_recv_current", str(stats['received_minute']))
    if dpg.does_item_exist("monitor_sent_current"):
        dpg.set_value("monitor_sent_current", str(stats['sent_minute']))

    # 更新图表
    update_monitor_chart(stats['trend'])


def update_monitor_chart(trend):
    """根据每分钟趋势生成文本柱状图。"""
    if not dpg.does_item_exist("monitor_chart_content"):
        return

    if not trend:
        dpg.set_value("monitor_chart_content", "暂无数据，请等待一分钟...")
        return

    chart_lines = []
    max_value = max(max(s['received'], s['sent']) for s in trend)
    if max_value == 0:
        max_value = 1

    chart_height = 15

    # 显示最近30分钟
    for stat in trend[-30:]:
        recv_bar = int((stat['received'] / max_value) * chart_height)
        sent_bar = int((stat['sent'] / max_value) * chart_height)

        line = f"{stat['time']} | "
        line += "█" * recv_bar + " " * (chart_height - recv_bar)
        line += f" | 收:{stat['received']:3d} "
        line += "█" * sent_bar + " " * (chart_height - sent_bar)
        line += f" | 发:{stat['sent']:3d}"

        chart_lines.append(line)

    # 添加图例
    legend = "\n图例: ███ 接收消息   ███ 发送消息"
    dpg.set_value("monitor_chart_content", "\n".join(chart_lines) + legend)


def _ensure_live_handler():
    """注册全局可见性处理器：监控窗口可见时每帧刷新，实现实时更新。"""
    if dpg.does_item_exist("monitor_visible_handler"):
        return

    def _on_visible():
        if dpg.does_item_exist(MONITOR_WINDOW) and dpg.is_item_shown(MONITOR_WINDOW):
            update_monitor_display()

    with dpg.handler_registry():
        dpg.add_visible_handler(tag="monitor_visible_handler", callback=_on_visible)


def show_monitor_window():
    """显示监控窗口"""
    # 如果窗口已存在，直接显示并更新
    if dpg.does_item_exist(MONITOR_WINDOW):
        dpg.show_item(MONITOR_WINDOW)
        update_monitor_display()
        return MONITOR_WINDOW

    # 创建新窗口
    with dpg.window(label="实时监控", tag=MONITOR_WINDOW, width=800, height=500, pos=[100, 50]):
        dpg.add_text("消息监控 - 实时统计", color=[255, 255, 0, 255])
        dpg.add_separator()

        # 实时统计卡片
        with dpg.group(horizontal=True):
            # 接收消息卡片
            with dpg.child_window(width=380, height=150):
                dpg.add_text("[接收] 接收消息", color=[100, 255, 100, 255])
                dpg.add_spacer(height=10)
                dpg.add_text("本分钟:", color=[200, 200, 200, 255])
                dpg.add_text("0", color=[255, 255, 255, 255], tag="monitor_recv_current")
                dpg.add_spacer(height=10)
                dpg.add_text("总计:", color=[200, 200, 200, 255])
                dpg.add_text("0", color=[255, 255, 255, 255], tag="monitor_recv_total")

            # 发送消息卡片
            with dpg.child_window(width=380, height=150):
                dpg.add_text("[发送] 发送消息", color=[100, 100, 255, 255])
                dpg.add_spacer(height=10)
                dpg.add_text("本分钟:", color=[200, 200, 200, 255])
                dpg.add_text("0", color=[255, 255, 255, 255], tag="monitor_sent_current")
                dpg.add_spacer(height=10)
                dpg.add_text("总计:", color=[200, 200, 200, 255])
                dpg.add_text("0", color=[255, 255, 255, 255], tag="monitor_sent_total")

        dpg.add_spacer(height=20)
        dpg.add_text("最近60分钟趋势", color=[255, 255, 0, 255])
        dpg.add_separator()

        # 简单的文本图表
        with dpg.child_window(width=760, height=250, tag="monitor_chart"):
            dpg.add_text("加载中...", color=[200, 200, 200, 255], tag="monitor_chart_content")

    # 窗口可见期间实时刷新
    _ensure_live_handler()

    # 更新监控数据
    update_monitor_display()

    return MONITOR_WINDOW
