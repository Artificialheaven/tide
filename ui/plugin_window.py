import dearpygui.dearpygui as dpg


def enable_plugin(sender, app_data, user_data):
    """启用/禁用插件"""
    from utils.plugin_loader import get_plugin_manager
    
    plugin_name = user_data
    enabled = dpg.get_value(f"plugin_enabled_{plugin_name}")
    status_tag = f"plugin_status_{plugin_name}"
    
    plugin_manager = get_plugin_manager()
    if plugin_manager:
        if enabled:
            plugin_manager.enable_plugin(plugin_name)
            status = "已启用"
            color = [0, 255, 0, 255]
        else:
            plugin_manager.disable_plugin(plugin_name)
            status = "已禁用"
            color = [255, 0, 0, 255]
        
        dpg.set_value(status_tag, f"状态: {status}")
        dpg.configure_item(status_tag, color=color)
        print(f"插件 {plugin_name} {status}")
    else:
        print("插件管理器未初始化")


def reload_single_plugin(sender, app_data, user_data):
    """重新加载单个插件"""
    from utils.plugin_loader import get_plugin_manager
    
    plugin_name = user_data
    plugin_manager = get_plugin_manager()
    
    if not plugin_manager:
        print("插件管理器未初始化")
        return
    
    print(f"[GUI] 请求重新加载插件: {plugin_name}")
    
    # 执行热重载
    success = plugin_manager.reload_plugin(plugin_name)
    
    if success:
        print(f"[GUI] 插件 {plugin_name} 热重载成功")
        # 刷新显示
        refresh_plugin_list()
    else:
        print(f"[GUI] 插件 {plugin_name} 热重载失败")
        # 仍然刷新显示以显示错误状态
        refresh_plugin_list()


def reload_all_plugins():
    """重新加载所有插件"""
    from utils.plugin_loader import get_plugin_manager
    
    plugin_manager = get_plugin_manager()
    
    if not plugin_manager:
        print("插件管理器未初始化")
        return
    
    print("[GUI] 请求重新加载所有插件")
    
    # 执行全部热重载
    success_list, failed_list = plugin_manager.reload_all_plugins()
    
    print(f"[GUI] 热重载完成: 成功 {len(success_list)} 个, 失败 {len(failed_list)} 个")
    
    if success_list:
        print(f"[GUI] 成功的插件: {', '.join(success_list)}")
    if failed_list:
        print(f"[GUI] 失败的插件: {', '.join(failed_list)}")
    
    # 刷新显示
    refresh_plugin_list()


def scan_new_plugins():
    """扫描并加载新插件"""
    from utils.plugin_loader import get_plugin_manager
    
    plugin_manager = get_plugin_manager()
    
    if not plugin_manager:
        print("插件管理器未初始化")
        return
    
    print("[GUI] 扫描新插件")
    
    # 扫描新插件
    new_plugins = plugin_manager.scan_new_plugins()
    
    if new_plugins:
        print(f"[GUI] 发现并加载了新插件: {', '.join(new_plugins)}")
    else:
        print("[GUI] 没有发现新插件")
    
    # 刷新显示
    refresh_plugin_list()


def refresh_plugin_list():
    """刷新插件列表显示"""
    from utils.plugin_loader import get_plugin_manager
    
    plugin_manager = get_plugin_manager()
    
    # 清除旧的插件列表容器
    if dpg.does_item_exist("plugin_list_container"):
        dpg.delete_item("plugin_list_container")
    
    # 创建新的容器
    with dpg.child_window(height=280, tag="plugin_list_container", parent="plugin_window"):
        if not plugin_manager or not plugin_manager.get_plugin_list():
            dpg.add_text("暂无插件", color=[150, 150, 150, 255])
            return
        
        plugin_list = plugin_manager.get_plugin_list()
        for i, (plugin_key, info) in enumerate(plugin_list.items()):
            with dpg.group(tag=f"plugin_group_{plugin_key}"):
                with dpg.group(horizontal=True):
                    dpg.add_text(f"{i + 1}. {info['name']} (v{info['version']})")
                    dpg.add_checkbox(
                        label="启用",
                        tag=f"plugin_enabled_{plugin_key}",
                        default_value=info['enabled'],
                        callback=enable_plugin,
                        user_data=plugin_key
                    )
                
                dpg.add_text(f"描述: {info['description']}", color=[200, 200, 200, 255])
                
                # 显示错误信息（如果有）
                if 'error' in info and info['error']:
                    dpg.add_text(f"错误: {info['error']}", color=[255, 100, 100, 255])
                
                status_tag = f"plugin_status_{plugin_key}"
                status_color = [0, 255, 0, 255] if info['enabled'] else [255, 0, 0, 255]
                status_text = "状态: 已启用" if info['enabled'] else "状态: 已禁用"
                dpg.add_text(status_text, tag=status_tag, color=status_color)
                
                # 添加热重载按钮
                with dpg.group(horizontal=True):
                    dpg.add_button(
                        label="重载",
                        width=60,
                        height=25,
                        callback=reload_single_plugin,
                        user_data=plugin_key
                    )
                
                dpg.add_separator()


def create_plugin_window():
    """创建插件窗口"""
    from utils.plugin_loader import get_plugin_manager
    
    plugin_manager = get_plugin_manager()
    
    with dpg.window(
            label="插件管理",
            width=500,
            height=450,
            tag="plugin_window",
            pos=[250, 50],
            on_close=lambda: dpg.hide_item("plugin_window")
    ):
        dpg.add_text("插件管理", color=[255, 255, 0, 255])
        dpg.add_separator()
        
        # 插件列表容器（动态填充）
        with dpg.child_window(height=280, tag="plugin_list_container"):
            if not plugin_manager or not plugin_manager.get_plugin_list():
                dpg.add_text("暂无插件，请将插件放入 plugins 目录", color=[150, 150, 150, 255])
            else:
                plugin_list = plugin_manager.get_plugin_list()
                for i, (plugin_key, info) in enumerate(plugin_list.items()):
                    with dpg.group(tag=f"plugin_group_{plugin_key}"):
                        with dpg.group(horizontal=True):
                            dpg.add_text(f"{i + 1}. {info['name']} (v{info['version']})")
                            dpg.add_checkbox(
                                label="启用",
                                tag=f"plugin_enabled_{plugin_key}",
                                default_value=info['enabled'],
                                callback=enable_plugin,
                                user_data=plugin_key
                            )
                        
                        dpg.add_text(f"描述: {info['description']}", color=[200, 200, 200, 255])
                        
                        # 显示错误信息（如果有）
                        if 'error' in info and info['error']:
                            dpg.add_text(f"错误: {info['error']}", color=[255, 100, 100, 255])
                        
                        status_tag = f"plugin_status_{plugin_key}"
                        status_color = [0, 255, 0, 255] if info['enabled'] else [255, 0, 0, 255]
                        status_text = "状态: 已启用" if info['enabled'] else "状态: 已禁用"
                        dpg.add_text(status_text, tag=status_tag, color=status_color)
                        
                        # 添加热重载按钮
                        with dpg.group(horizontal=True):
                            dpg.add_button(
                                label="重载",
                                width=60,
                                height=25,
                                callback=reload_single_plugin,
                                user_data=plugin_key
                            )
                        
                        dpg.add_separator()

        dpg.add_spacer(height=10)

        # 插件管理按钮
        with dpg.group(horizontal=True):
            dpg.add_button(label="刷新列表", width=120, height=35, callback=refresh_plugin_list)
            dpg.add_button(label="重载全部", width=120, height=35, callback=reload_all_plugins)
            dpg.add_button(label="扫描新插件", width=120, height=35, callback=scan_new_plugins)

        dpg.add_spacer(height=10)

        # 插件目录显示
        dpg.add_text("插件目录: ./plugins", color=[150, 150, 255, 255])

    # 默认隐藏插件窗口
    dpg.hide_item("plugin_window")

    return "plugin_window"