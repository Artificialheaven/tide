"""
系统运行状态监控窗口
- CPU / 内存饼图
- CPU / 内存历史折线图
- 硬盘、显卡、网络信息
- 每秒自动刷新
"""

import dearpygui.dearpygui as dpg
import math
import threading
import time
from collections import deque

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


# ============================================================
#  数据存储（线程安全：主线程只读，采集线程写入）
# ============================================================
cpu_percent = 0.0
mem_percent = 0.0
cpu_history = deque(maxlen=120)     # 保留 120 个数据点（2 分钟）
mem_history = deque(maxlen=120)
time_labels = deque(maxlen=120)

disk_info: list[dict] = []
gpu_info: list[dict] = []
net_info = {"sent": 0, "recv": 0, "sent_speed": 0, "recv_speed": 0}
cpu_logical = 0
cpu_physical = 0
cpu_freq = 0
mem_total_gb = 0.0
_time_idx = 0                     # 秒级时间轴索引

_monitor_running = False
_monitor_thread = None


# ============================================================
#  饼图绘制（基于 draw_triangle 的扇形填充）
# ============================================================
def _draw_pie(drawlist: str, center: tuple, radius: float,
              usage_pct: float, color_used: tuple, color_free: tuple):
    """
    在指定 drawlist 中绘制饼图。
    DPG 坐标系：y 轴向下，角度从右侧 (1,0) 顺时针（弧度）。
    """
    segments_per_full = 80
    used_segments = max(int(segments_per_full * usage_pct), 0 if usage_pct == 0 else 1)
    free_segments = segments_per_full - used_segments
    cx, cy = center

    def _draw_segment(start_angle, num_seg, color):
        if num_seg == 0:
            return
        step = (2 * math.pi) / segments_per_full
        for i in range(num_seg):
            a1 = start_angle + i * step
            a2 = start_angle + (i + 1) * step
            p1 = (cx, cy)
            p2 = (cx + radius * math.cos(a1), cy + radius * math.sin(a1))
            p3 = (cx + radius * math.cos(a2), cy + radius * math.sin(a2))
            dpg.draw_triangle(p1, p2, p3, color=color, fill=color,
                              parent=drawlist)

    # 从顶部 (-π/2) 开始，先绘制已用部分
    start = -math.pi / 2
    _draw_segment(start, used_segments, color_used)
    _draw_segment(start + used_segments * (2 * math.pi) / segments_per_full,
                  free_segments, color_free)


def _draw_ring(drawlist: str, center: tuple, radius: float, thickness: float,
               color: tuple):
    """绘制中心装饰圆环"""
    dpg.draw_circle(center, radius + thickness / 2, color=color,
                    thickness=thickness, parent=drawlist)


# ============================================================
#  数据采集
# ============================================================
def _collect_system_info():
    """后台线程：每秒采集一次系统信息"""
    global cpu_percent, mem_percent, _time_idx, disk_info, gpu_info, net_info
    global cpu_logical, cpu_physical, cpu_freq, mem_total_gb, _monitor_running

    if not HAS_PSUTIL:
        return

    _prev_net = psutil.net_io_counters()
    _prev_time = time.time()

    # ---------- 一次性信息 ----------
    cpu_logical = psutil.cpu_count(logical=True)
    cpu_physical = psutil.cpu_count(logical=False)
    cpu_freq = psutil.cpu_freq().current if psutil.cpu_freq() else 0
    mem_total_gb = psutil.virtual_memory().total / (1024 ** 3)

    # 磁盘
    disk_info.clear()
    for part in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disk_info.append({
                "device": part.device,
                "mount": part.mountpoint,
                "total": usage.total / (1024 ** 3),
                "used": usage.used / (1024 ** 3),
                "percent": usage.percent,
            })
        except Exception:
            continue

    # GPU
    gpu_info.clear()
    try:
        # 尝试通过 WMI 或 nvidia-smi 获取，psutil 不直接支持
        import subprocess
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,temperature.gpu,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                parts = [p.strip() for p in line.split(',')]
                if len(parts) >= 5:
                    gpu_info.append({
                        "name": parts[0],
                        "mem_total": float(parts[1]) / 1024,
                        "mem_used": float(parts[2]) / 1024,
                        "temp": float(parts[3]),
                        "util": float(parts[4]),
                    })
    except Exception:
        pass

    # ---------- 持续监控 ----------
    while _monitor_running:
        try:
            cpu_percent = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            mem_percent = mem.percent

            _time_idx += 1
            cpu_history.append(cpu_percent)
            mem_history.append(mem_percent)
            time_labels.append(_time_idx)

            # 网络速率
            net = psutil.net_io_counters()
            now = time.time()
            delta = now - _prev_time
            if delta > 0:
                net_info["sent_speed"] = (net.bytes_sent - _prev_net.bytes_sent) / delta / 1024
                net_info["recv_speed"] = (net.bytes_recv - _prev_net.bytes_recv) / delta / 1024
            net_info["sent"] = net.bytes_sent / (1024 ** 3)
            net_info["recv"] = net.bytes_recv / (1024 ** 3)
            _prev_net = net
            _prev_time = now

            # 更新 DPG 显示（在主线程之外调用 set_value 在实践中可行）
            try:
                _update_display()
            except Exception:
                pass

            time.sleep(1)
        except Exception:
            time.sleep(1)


# ============================================================
#  UI 更新
# ============================================================
def _update_display():
    """主线程：把最新数据写入 DPG 控件"""
    if not dpg.does_item_exist("sysmon_window"):
        return

    # CPU
    dpg.set_value("sysmon_cpu_val", f"{cpu_percent:.1f}%")
    dpg.configure_item("sysmon_cpu_bar", overlay=f"{cpu_percent:.1f}%")
    dpg.set_value("sysmon_cpu_bar", cpu_percent / 100.0)
    dpg.set_value("sysmon_cpu_info",
                  f"物理核心: {cpu_physical}  逻辑核心: {cpu_logical}\n"
                  f"频率: {cpu_freq:.0f} MHz")

    # 更新 CPU 饼图
    if dpg.does_item_exist("sysmon_cpu_pie"):
        dpg.delete_item("sysmon_cpu_pie", children_only=True)
        _draw_pie("sysmon_cpu_pie", (50, 50), 42, cpu_percent / 100.0,
                  (255, 80, 80, 255), (50, 50, 50, 255))

    # 内存
    dpg.set_value("sysmon_mem_val", f"{mem_percent:.1f}%")
    dpg.configure_item("sysmon_mem_bar", overlay=f"{mem_percent:.1f}%")
    dpg.set_value("sysmon_mem_bar", mem_percent / 100.0)
    dpg.set_value("sysmon_mem_info",
                  f"总内存: {mem_total_gb:.1f} GB\n"
                  f"已用: {mem_total_gb * mem_percent / 100:.1f} GB")

    # 更新内存饼图
    if dpg.does_item_exist("sysmon_mem_pie"):
        dpg.delete_item("sysmon_mem_pie", children_only=True)
        _draw_pie("sysmon_mem_pie", (50, 50), 42, mem_percent / 100.0,
                  (80, 180, 255, 255), (50, 50, 50, 255))

    # 折线图
    if dpg.does_item_exist("sysmon_cpu_line"):
        xs = list(time_labels)
        cpu_ys = list(cpu_history)
        mem_ys = list(mem_history)
        if xs:
            dpg.set_value("sysmon_cpu_line", [xs, cpu_ys])
            dpg.set_value("sysmon_mem_line", [xs, mem_ys])
            xmin, xmax = max(0, xs[-1] - 120), xs[-1] + 5
            dpg.set_axis_limits("sysmon_x_axis", xmin, xmax)
            dpg.set_axis_limits("sysmon_mem_x_axis", xmin, xmax)
            dpg.set_axis_limits("sysmon_cpu_y", 0, 105)
            dpg.set_axis_limits("sysmon_mem_y", 0, 105)

    # 磁盘
    if dpg.does_item_exist("sysmon_disk_text"):
        lines = []
        for d in disk_info:
            lines.append(f"{d['device']} ({d['mount']}): "
                         f"{d['used']:.0f}/{d['total']:.0f} GB ({d['percent']:.1f}%)")
        dpg.set_value("sysmon_disk_text", "\n".join(lines) if lines else "无数据")

    # GPU
    if dpg.does_item_exist("sysmon_gpu_text"):
        lines = []
        for g in gpu_info:
            lines.append(f"{g['name']}")
            lines.append(f"  显存: {g['mem_used']:.1f}/{g['mem_total']:.1f} GB")
            lines.append(f"  温度: {g['temp']:.0f}°C  利用率: {g['util']:.0f}%")
        dpg.set_value("sysmon_gpu_text", "\n".join(lines) if lines else "未检测到 GPU")

    # 网络
    if dpg.does_item_exist("sysmon_net_text"):
        dpg.set_value("sysmon_net_text",
                      f"↓ 接收: {net_info['recv_speed']:.1f} KB/s\n"
                      f"↑ 发送: {net_info['sent_speed']:.1f} KB/s\n"
                      f"累计接收: {net_info['recv']:.2f} GB\n"
                      f"累计发送: {net_info['sent']:.2f} GB")


# ============================================================
#  窗口入口
# ============================================================
def show_system_window():
    """显示系统运行状态窗口"""
    global _monitor_running, _monitor_thread
    tag = "sysmon_window"

    # 窗口已存在 → 直接显示
    if dpg.does_item_exist(tag):
        dpg.show_item(tag)
        return

    # ---------- 启动数据采集线程 ----------
    if HAS_PSUTIL and not _monitor_running:
        _monitor_running = True
        _monitor_thread = threading.Thread(target=_collect_system_info, daemon=True)
        _monitor_thread.start()
        time.sleep(0.5)  # 等待首次采集完成

    # ---------- 构建 UI ----------
    with dpg.window(label="系统运行状态", tag=tag, width=960, height=580,
                    pos=[50, 30], on_close=lambda: dpg.hide_item(tag)):
        dpg.add_text("系统资源监控", color=[255, 255, 0, 255])
        dpg.add_separator()

        # ---- 第一行：CPU 饼图 + 内存饼图 + 文本信息 ----
        with dpg.group(horizontal=True):
            # CPU 左侧
            with dpg.child_window(width=220, height=170):
                dpg.add_text("CPU 使用率", color=[255, 255, 255, 255])
                with dpg.drawlist(width=100, height=100, tag="sysmon_cpu_pie"):
                    _draw_pie("sysmon_cpu_pie", (50, 50), 42, cpu_percent / 100.0,
                              (255, 80, 80, 255), (50, 50, 50, 255))
                dpg.add_text(f"{cpu_percent:.1f}%", tag="sysmon_cpu_val",
                             color=[255, 100, 100, 255])
                dpg.add_progress_bar(tag="sysmon_cpu_bar", width=200,
                                     overlay=f"{cpu_percent:.1f}%")
                dpg.set_value("sysmon_cpu_bar", cpu_percent / 100.0)

            dpg.add_spacer(width=10)

            # 内存中间
            with dpg.child_window(width=220, height=170):
                dpg.add_text("内存 使用率", color=[255, 255, 255, 255])
                with dpg.drawlist(width=100, height=100, tag="sysmon_mem_pie"):
                    _draw_pie("sysmon_mem_pie", (50, 50), 42, mem_percent / 100.0,
                              (80, 180, 255, 255), (50, 50, 50, 255))
                dpg.add_text(f"{mem_percent:.1f}%", tag="sysmon_mem_val",
                             color=[100, 180, 255, 255])
                dpg.add_progress_bar(tag="sysmon_mem_bar", width=200,
                                     overlay=f"{mem_percent:.1f}%")
                dpg.set_value("sysmon_mem_bar", mem_percent / 100.0)

            dpg.add_spacer(width=10)

            # 详细信息右侧
            with dpg.child_window(width=280, height=170):
                dpg.add_text("CPU 信息", color=[255, 200, 100, 255])
                dpg.add_text(f"物理核心: {cpu_physical}  逻辑核心: {cpu_logical}\n"
                             f"频率: {cpu_freq:.0f} MHz",
                             tag="sysmon_cpu_info", color=[200, 200, 200, 255])
                dpg.add_spacer(height=10)
                dpg.add_text("内存信息", color=[255, 200, 100, 255])
                dpg.add_text(f"总内存: {mem_total_gb:.1f} GB\n"
                             f"已用: {mem_total_gb * mem_percent / 100:.1f} GB",
                             tag="sysmon_mem_info", color=[200, 200, 200, 255])

        dpg.add_spacer(height=10)

        # ---- 第二行：折线图 ----
        with dpg.group(horizontal=True):
            # CPU 折线图
            with dpg.child_window(width=470, height=170):
                dpg.add_text("CPU 使用率历史 (%)", color=[100, 255, 100, 255])
                with dpg.plot(label="CPU", height=130, width=450):
                    dpg.add_plot_axis(dpg.mvXAxis, label="秒", tag="sysmon_x_axis")
                    with dpg.plot_axis(dpg.mvYAxis, label="%", tag="sysmon_cpu_y"):
                        dpg.add_line_series([], [], tag="sysmon_cpu_line",
                                            label="CPU %")
                    dpg.add_plot_legend()

            # 内存折线图
            with dpg.child_window(width=470, height=170):
                dpg.add_text("内存使用率历史 (%)", color=[100, 180, 255, 255])
                with dpg.plot(label="Memory", height=130, width=450):
                    dpg.add_plot_axis(dpg.mvXAxis, label="秒", tag="sysmon_mem_x_axis")
                    with dpg.plot_axis(dpg.mvYAxis, label="%", tag="sysmon_mem_y"):
                        dpg.add_line_series([], [], tag="sysmon_mem_line",
                                            label="Mem %")
                    dpg.add_plot_legend()

        dpg.add_spacer(height=10)

        # ---- 第三行：磁盘 / GPU / 网络 ----
        with dpg.group(horizontal=True):
            # 磁盘
            with dpg.child_window(width=300, height=120):
                dpg.add_text("磁盘", color=[255, 200, 100, 255])
                lines = []
                for d in disk_info:
                    lines.append(f"{d['device']} ({d['mount']}): "
                                 f"{d['used']:.0f}/{d['total']:.0f} GB "
                                 f"({d['percent']:.1f}%)")
                dpg.add_text("\n".join(lines) if lines else "无数据",
                             tag="sysmon_disk_text", color=[200, 200, 200, 255])

            # GPU
            with dpg.child_window(width=300, height=120):
                dpg.add_text("显卡", color=[255, 200, 100, 255])
                lines = []
                for g in gpu_info:
                    lines.append(f"{g['name']}")
                    lines.append(f"  显存: {g['mem_used']:.1f}/{g['mem_total']:.1f} GB")
                    lines.append(f"  温度: {g['temp']:.0f}°C  利用率: {g['util']:.0f}%")
                dpg.add_text("\n".join(lines) if lines else "未检测到 GPU",
                             tag="sysmon_gpu_text", color=[200, 200, 200, 255])

            # 网络
            with dpg.child_window(width=320, height=120):
                dpg.add_text("网络", color=[255, 200, 100, 255])
                dpg.add_text(f"↓ 接收: {net_info['recv_speed']:.1f} KB/s\n"
                             f"↑ 发送: {net_info['sent_speed']:.1f} KB/s\n"
                             f"累计接收: {net_info['recv']:.2f} GB\n"
                             f"累计发送: {net_info['sent']:.2f} GB",
                             tag="sysmon_net_text", color=[200, 200, 200, 255])


# ============================================================
#  定时刷新器（由外部帧循环驱动）
# ============================================================
_refresh_handler = None


def register_refresh_callback():
    """注册每秒刷新回调到 DPG 帧循环"""
    global _refresh_handler
    if _refresh_handler is not None:
        return
    with dpg.handler_registry():
        _refresh_handler = dpg.add_key_release_handler(
            key=-1,  # 不存在的虚拟键，用作帧钩子
            callback=lambda: None
        )


def poll_display():
    """供外部定时调用的刷新函数"""
    if dpg.does_item_exist("sysmon_window") and dpg.is_item_visible("sysmon_window"):
        _update_display()