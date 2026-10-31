import dearpygui.dearpygui as dpg
from core import globals
import time
import asyncio
import threading
import json
import os

# 启动时间
start_time = time.time()

# 缓存数据
group_list_cache = []
friend_list_cache = []
last_update_time = 0
CACHE_DURATION = 300  # 缓存5分钟
is_loading = False  # 防止重复加载
data_fetch_started = False  # 标记数据获取是否已启动

# 临时文件路径
TEMP_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'temp')
REPORT_DATA_FILE = os.path.join(TEMP_DIR, 'report_data.json')


def ensure_temp_dir():
    """确保临时目录存在"""
    if not os.path.exists(TEMP_DIR):
        os.makedirs(TEMP_DIR)


def save_report_data():
    """保存报表数据到临时文件"""
    try:
        ensure_temp_dir()
        data = {
            'group_list': group_list_cache,
            'friend_list': friend_list_cache,
            'last_update_time': last_update_time,
            'timestamp': time.time()
        }
        with open(REPORT_DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print("[报表窗口] 数据已保存到临时文件")
    except Exception as e:
        print(f"[报表窗口] 保存数据失败: {e}")


def load_report_data():
    """从临时文件加载报表数据"""
    global group_list_cache, friend_list_cache, last_update_time
    
    if not os.path.exists(REPORT_DATA_FILE):
        print("[报表窗口] 未找到历史数据文件")
        return False
    
    try:
        with open(REPORT_DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        group_list_cache = data.get('group_list', [])
        friend_list_cache = data.get('friend_list', [])
        last_update_time = data.get('last_update_time', 0)
        
        print(f"[报表窗口] 已从临时文件加载数据: {len(group_list_cache)}个群, {len(friend_list_cache)}个好友")
        return True
    except Exception as e:
        print(f"[报表窗口] 加载数据失败: {e}")
        return False


def update_report_display():
    """更新报表显示"""
    if not globals.bot_instance:
        return
    
    stats = globals.bot_instance.get_stats()
    elapsed = time.time() - start_time
    
    # 更新运行时间
    if dpg.does_item_exist("report_runtime"):
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        seconds = int(elapsed % 60)
        dpg.set_value("report_runtime", f"运行时间: {hours}小时 {minutes}分钟 {seconds}秒")
    
    # 更新统计数据
    if dpg.does_item_exist("report_recv_total"):
        dpg.set_value("report_recv_total", str(stats['received']))
    if dpg.does_item_exist("report_sent_total"):
        dpg.set_value("report_sent_total", str(stats['sent']))
    if dpg.does_item_exist("report_total"):
        dpg.set_value("report_total", str(stats['received'] + stats['sent']))
    
    # 更新速率
    if elapsed > 0:
        recv_per_min = stats['received'] / (elapsed / 60)
        sent_per_min = stats['sent'] / (elapsed / 60)
        
        if dpg.does_item_exist("report_recv_rate"):
            dpg.set_value("report_recv_rate", f"{recv_per_min:.2f} 条/分钟")
        if dpg.does_item_exist("report_sent_rate"):
            dpg.set_value("report_sent_rate", f"{sent_per_min:.2f} 条/分钟")


def fetch_and_update_data():
    """在后台线程中获取并更新数据"""
    global group_list_cache, friend_list_cache, last_update_time, is_loading, data_fetch_started
    
    if is_loading:
        return
    
    is_loading = True
    data_fetch_started = True
    
    try:
        current_time = time.time()
        
        # 检查缓存是否有效
        cache_valid = current_time - last_update_time < CACHE_DURATION
        groups_cached = len(group_list_cache) > 0
        friends_cached = len(friend_list_cache) > 0
        
        # 如果缓存有效，直接使用缓存数据更新UI
        if cache_valid and groups_cached and friends_cached:
            # 在主线程中更新UI
            if dpg.does_item_exist("loading_indicator"):
                dpg.configure_item("loading_indicator", show=False)
            if dpg.does_item_exist("data_status"):
                dpg.set_value("data_status", "✓ 数据已缓存")
            update_ui_with_cache()
            return
        
        # 获取群列表
        if not cache_valid or not groups_cached:
            try:
                if globals.bot_instance:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        print("[报表窗口] 正在获取群列表...")
                        if dpg.does_item_exist("data_status"):
                            dpg.set_value("data_status", "⟳ 正在获取群列表...")
                        result = loop.run_until_complete(
                            globals.bot_instance.get_group_list(plugin_name="报表窗口")
                        )
                        if result and 'data' in result:
                            group_list_cache = result['data']
                            print(f"[报表窗口] 获取到 {len(group_list_cache)} 个群")
                            # 保存数据到临时文件
                            save_report_data()
                    finally:
                        loop.close()
            except Exception as e:
                print(f"[报表窗口] 获取群列表失败: {e}")
                if dpg.does_item_exist("data_status"):
                    dpg.set_value("data_status", "✗ 群列表获取失败")
        
        # 获取好友列表
        if not cache_valid or not friends_cached:
            try:
                if globals.bot_instance:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        print("[报表窗口] 正在获取好友列表...")
                        if dpg.does_item_exist("data_status"):
                            dpg.set_value("data_status", "⟳ 正在获取好友列表...")
                        result = loop.run_until_complete(
                            globals.bot_instance.get_friend_list(plugin_name="报表窗口")
                        )
                        if result and 'data' in result:
                            friend_list_cache = result['data']
                            print(f"[报表窗口] 获取到 {len(friend_list_cache)} 个好友")
                            # 保存数据到临时文件
                            save_report_data()
                    finally:
                        loop.close()
            except Exception as e:
                print(f"[报表窗口] 获取好友列表失败: {e}")
                if dpg.does_item_exist("data_status"):
                    dpg.set_value("data_status", "✗ 好友列表获取失败")
        
        # 更新最后更新时间
        last_update_time = time.time()
        
        # 在主线程中更新UI
        if dpg.does_item_exist("loading_indicator"):
            dpg.configure_item("loading_indicator", show=False)
        if dpg.does_item_exist("data_status"):
            dpg.set_value("data_status", "✓ 数据已更新")
        update_ui_with_cache()
        
    except Exception as e:
        print(f"[报表窗口] 数据获取异常: {e}")
        if dpg.does_item_exist("data_status"):
            dpg.set_value("data_status", "✗ 数据获取异常")
    finally:
        is_loading = False


def update_ui_with_cache():
    """使用缓存数据更新UI（在主线程中调用）"""
    # 清空现有数据
    if dpg.does_item_exist("group_table"):
        children = dpg.get_item_children("group_table", 1)
        for child in children:
            dpg.delete_item(child)
    
    if dpg.does_item_exist("friend_table"):
        children = dpg.get_item_children("friend_table", 1)
        for child in children:
            dpg.delete_item(child)
    
    # 更新群列表显示
    if dpg.does_item_exist("group_table"):
        for group in group_list_cache:
            group_id = group.get('group_id', '未知')
            group_name = group.get('group_name', '未知群')
            member_count = group.get('member_count', 0)
            
            with dpg.table_row(parent="group_table"):
                dpg.add_text(str(group_id))
                dpg.add_text(group_name)
                dpg.add_text(str(member_count))
        
        # 更新群数量统计
        if dpg.does_item_exist("group_count"):
            dpg.set_value("group_count", f"群数量: {len(group_list_cache)}")
    
    # 更新好友列表显示
    if dpg.does_item_exist("friend_table"):
        for friend in friend_list_cache:
            user_id = friend.get('user_id', '未知')
            nickname = friend.get('nickname', '未知')
            remark = friend.get('remark', '')
            
            with dpg.table_row(parent="friend_table"):
                dpg.add_text(str(user_id))
                dpg.add_text(nickname)
                dpg.add_text(remark if remark else '-')
        
        # 更新好友数量统计
        if dpg.does_item_exist("friend_count"):
            dpg.set_value("friend_count", f"好友数量: {len(friend_list_cache)}")


def refresh_contact_data():
    """刷新联系人数据（群和好友）- 非阻塞版本"""
    if not globals.bot_instance:
        return
    
    # 显示加载提示
    if dpg.does_item_exist("loading_indicator"):
        dpg.configure_item("loading_indicator", show=True)
    if dpg.does_item_exist("data_status"):
        dpg.set_value("data_status", "⟳ 正在刷新...")
    
    # 在后台线程中获取数据，避免阻塞UI
    thread = threading.Thread(target=fetch_and_update_data, daemon=True)
    thread.start()


def show_report_window():
    """显示报表窗口"""
    report_window = "report_window"
    
    # 如果窗口已存在，直接显示并更新
    if dpg.does_item_exist(report_window):
        update_report_display()
        # 如果数据还未加载，触发加载
        if not data_fetch_started or len(group_list_cache) == 0:
            refresh_contact_data()
        dpg.show_item(report_window)
        return report_window
    
    # 尝试从临时文件加载历史数据
    if not data_fetch_started:
        load_report_data()
    
    # 创建新窗口
    with dpg.window(label="统计报表", tag=report_window, width=900, height=550, pos=[100, 50]):
        dpg.add_text("消息统计报表", color=[255, 255, 0, 255])
        dpg.add_separator()
        
        # 运行时间和数据状态
        with dpg.group(horizontal=True):
            elapsed = time.time() - start_time
            hours = int(elapsed // 3600)
            minutes = int((elapsed % 3600) // 60)
            seconds = int(elapsed % 60)
            
            dpg.add_text(f"运行时间: {hours}小时 {minutes}分钟 {seconds}秒", 
                        color=[200, 200, 200, 255], tag="report_runtime")
            dpg.add_spacer(width=20)
            dpg.add_text("", color=[100, 255, 100, 255], tag="data_status")
        
        dpg.add_spacer(height=10)
        
        # 总体统计
        with dpg.child_window(width=860, height=200):
            dpg.add_text("总体统计", color=[255, 255, 0, 255])
            dpg.add_separator()
            
            if globals.bot_instance:
                stats = globals.bot_instance.get_stats()
                
                with dpg.group(horizontal=True):
                    dpg.add_text("[接收] 总接收消息:", color=[100, 255, 100, 255])
                    dpg.add_text(f"{stats['received']}", color=[255, 255, 255, 255], tag="report_recv_total")
                
                dpg.add_spacer(height=10)
                
                with dpg.group(horizontal=True):
                    dpg.add_text("[发送] 总发送消息:", color=[100, 100, 255, 255])
                    dpg.add_text(f"{stats['sent']}", color=[255, 255, 255, 255], tag="report_sent_total")
                
                dpg.add_spacer(height=10)
                
                total = stats['received'] + stats['sent']
                with dpg.group(horizontal=True):
                    dpg.add_text("[总计] 消息总量:", color=[255, 255, 100, 255])
                    dpg.add_text(f"{total}", color=[255, 255, 255, 255], tag="report_total")
                
                dpg.add_spacer(height=20)
                
                # 平均速率
                if elapsed > 0:
                    recv_per_min = stats['received'] / (elapsed / 60)
                    sent_per_min = stats['sent'] / (elapsed / 60)
                    
                    with dpg.group(horizontal=True):
                        dpg.add_text("[速率] 平均接收速率:", color=[200, 200, 200, 255])
                        dpg.add_text(f"{recv_per_min:.2f} 条/分钟", color=[255, 255, 255, 255], tag="report_recv_rate")
                    
                    with dpg.group(horizontal=True):
                        dpg.add_text("[速率] 平均发送速率:", color=[200, 200, 200, 255])
                        dpg.add_text(f"{sent_per_min:.2f} 条/分钟", color=[255, 255, 255, 255], tag="report_sent_rate")
            else:
                dpg.add_text("Bot未初始化", color=[255, 100, 100, 255])
        
        dpg.add_spacer(height=20)
        
        # 群列表
        with dpg.child_window(width=860, height=220):
            with dpg.group(horizontal=True):
                dpg.add_text("[群组] 已加入的群列表", color=[255, 255, 0, 255])
                dpg.add_spacer(width=450)
                dpg.add_text("加载中...", color=[255, 255, 100, 255], tag="loading_indicator", show=False)
            
            dpg.add_separator()
            
            with dpg.group(horizontal=True):
                dpg.add_text("群数量: 0", color=[200, 200, 200, 255], tag="group_count")
                dpg.add_spacer(width=600)
            
            dpg.add_spacer(height=5)
            
            # 创建群列表表格
            with dpg.table(
                header_row=True,
                resizable=True,
                reorderable=False,
                hideable=False,
                tag="group_table",
                policy=dpg.mvTable_SizingFixedFit,
                height=150,
                scrollY=True
            ):
                dpg.add_table_column(label="群号", width_fixed=True, init_width_or_weight=120)
                dpg.add_table_column(label="群名称", width_stretch=True)
                dpg.add_table_column(label="成员数", width_fixed=True, init_width_or_weight=80)
        
        dpg.add_spacer(height=10)
        
        # 好友列表
        with dpg.child_window(width=860, height=220):
            dpg.add_text("[好友] 好友列表", color=[255, 255, 0, 255])
            dpg.add_separator()
            
            with dpg.group(horizontal=True):
                dpg.add_text("好友数量: 0", color=[200, 200, 200, 255], tag="friend_count")
                dpg.add_spacer(width=600)
            
            dpg.add_spacer(height=5)
            
            # 创建好友列表表格
            with dpg.table(
                header_row=True,
                resizable=True,
                reorderable=False,
                hideable=False,
                tag="friend_table",
                policy=dpg.mvTable_SizingFixedFit,
                height=150,
                scrollY=True
            ):
                dpg.add_table_column(label="QQ号", width_fixed=True, init_width_or_weight=120)
                dpg.add_table_column(label="昵称", width_stretch=True)
                dpg.add_table_column(label="备注", width_fixed=True, init_width_or_weight=120)
        
        dpg.add_spacer(height=20)
        
        # 刷新按钮
        with dpg.group(horizontal=True):
            dpg.add_button(
                label="刷新数据",
                width=100,
                height=30,
                callback=lambda: [update_report_display(), refresh_contact_data()]
            )
            dpg.add_button(
                label="关闭",
                width=80,
                height=30,
                callback=lambda: dpg.hide_item("report_window")
            )
    
    # 如果已有缓存数据，立即显示
    if len(group_list_cache) > 0 or len(friend_list_cache) > 0:
        update_ui_with_cache()
        if dpg.does_item_exist("data_status"):
            dpg.set_value("data_status", "✓ 已加载历史数据")
    
    # 如果数据还未开始获取，立即启动
    if not data_fetch_started:
        refresh_contact_data()
    
    return report_window


def get_start_time():
    """获取启动时间"""
    return start_time

