import json

import websockets
import dearpygui.dearpygui as dpg
import asyncio
from core import globals, history
from utils.obj import MessageEvent, MetaEvent, NoticeEvent, RequestEvent

# if not globals.no_gui:
from ui.main_ui import add_log

from utils.plugin_loader import get_plugin_manager


# 存储正在运行的插件任务，用于管理和清理
plugin_tasks = set()


async def init_bots(websocket: websockets.ClientConnection):
    data = {
        "action": "get_login_info",
        "params": {},
        "echo": "get_login_info"
    }
    await websocket.send(json.dumps(data, ensure_ascii=False))


async def parse_event(event: dict):
    """解析并分发 OneBot 事件（新名，推荐使用）"""
    ret = True

    if 'echo' in event:
        if event['echo'] == 'get_login_info':
            if globals.no_gui:
                print(f"登录信息: {event['data']}")
            else:
                dpg.set_value('name', f"{event['data']['nickname']}({event['data']['user_id']})")
            globals.bot_id = event['data']['user_id']
        globals.echo_dict[event['echo']] = event
        ret = False
        return ret

    if event['post_type'] == 'meta_event':
        meta = MetaEvent(event)
        if meta.meta_event_type == 'lifecycle':
            if meta.sub_type == 'connect':
                add_log(
                    '框架',
                    'DearQQ',
                    '已成功链接到NapcatQQ',
                    '黄色'
                )
                await init_bots(globals.websocket)
                ret = False

        if meta.meta_event_type == 'heartbeat':
            if globals.show_heart_beat:
                add_log(
                    'Napcat',
                    '心跳',
                    '心跳包收到√',
                    '黄色'
                )
            ret = False

    if event['post_type'] == 'message':
        js = event
        # 构造结构化消息事件
        msg_event = MessageEvent(event)

        # 记录消息历史（供 【msg[ID]】 等取用）
        history.record(msg_event)

        # 接收计数：群聊/私聊统一在此处只计一次
        if globals.bot_instance:
            globals.bot_instance.increment_received()

        if msg_event.message_type == 'group':
            add_log(
                f'{js["self_id"]}',
                f'{js["group_name"]}({js["group_id"]})',
                f'{js["sender"]["nickname"]}({js["sender"]["user_id"]}) 说:{js["raw_message"]}',
                '绿色'
            )

            # 调用插件的消息处理函数
            plugin_manager = get_plugin_manager()
            if plugin_manager:
                handlers = plugin_manager.get_event_handlers('message')
                for handler_info in handlers:
                    try:
                        callback = handler_info['callback']
                        plugin_name = handler_info['plugin_name']

                        # 注入插件名到事件对象
                        msg_event._plugin_name = plugin_name

                        # 检查是否是协程函数
                        if asyncio.iscoroutinefunction(callback):
                            task = asyncio.create_task(
                                _safe_execute_handler(callback, msg_event, plugin_name)
                            )
                            plugin_tasks.add(task)
                            task.add_done_callback(plugin_tasks.discard)
                        else:
                            loop = asyncio.get_event_loop()
                            loop.run_in_executor(None, _safe_sync_handler, callback, msg_event)
                    except Exception as e:
                        print(f"插件处理消息失败: {e}")
                        import traceback
                        traceback.print_exc()

            ret = False

        if msg_event.message_type == 'private':
            add_log(
                f'{js["self_id"]}',
                f'{js["sender"]["nickname"]}({js["sender"]["user_id"]})',
                f'私聊消息: {js["raw_message"]}',
                '蓝色'
            )

            # 调用插件的消息处理函数
            plugin_manager = get_plugin_manager()
            if plugin_manager:
                handlers = plugin_manager.get_event_handlers('message')
                for handler_info in handlers:
                    try:
                        callback = handler_info['callback']
                        plugin_name = handler_info['plugin_name']

                        # 注入插件名到事件对象
                        msg_event._plugin_name = plugin_name

                        if asyncio.iscoroutinefunction(callback):
                            task = asyncio.create_task(
                                _safe_execute_handler(callback, msg_event, plugin_name)
                            )
                            plugin_tasks.add(task)
                            task.add_done_callback(plugin_tasks.discard)
                        else:
                            loop = asyncio.get_event_loop()
                            loop.run_in_executor(None, _safe_sync_handler, callback, msg_event)
                    except Exception as e:
                        print(f"插件处理消息失败: {e}")
                        import traceback
                        traceback.print_exc()

            ret = False

    # 通知事件也构造结构化对象（为后续扩展准备）
    if event['post_type'] == 'notice':
        notice_event = NoticeEvent(event)
        plugin_manager = get_plugin_manager()
        if plugin_manager:
            handlers = plugin_manager.get_event_handlers('notice')
            for handler_info in handlers:
                try:
                    callback = handler_info['callback']
                    plugin_name = handler_info['plugin_name']
                    notice_event._plugin_name = plugin_name

                    if asyncio.iscoroutinefunction(callback):
                        task = asyncio.create_task(
                            _safe_execute_handler(callback, notice_event, plugin_name)
                        )
                        plugin_tasks.add(task)
                        task.add_done_callback(plugin_tasks.discard)
                    else:
                        loop = asyncio.get_event_loop()
                        loop.run_in_executor(None, _safe_sync_handler, callback, notice_event)
                except Exception as e:
                    print(f"插件处理通知失败: {e}")
                    import traceback
                    traceback.print_exc()
        ret = False

    # 请求事件：好友申请 / 加群申请 / 群邀请（审批走 set_*_add_request）
    if event['post_type'] == 'request':
        request_event = RequestEvent(event)
        plugin_manager = get_plugin_manager()
        if plugin_manager:
            handlers = plugin_manager.get_event_handlers('request')
            for handler_info in handlers:
                try:
                    callback = handler_info['callback']
                    plugin_name = handler_info['plugin_name']
                    request_event._plugin_name = plugin_name

                    if asyncio.iscoroutinefunction(callback):
                        task = asyncio.create_task(
                            _safe_execute_handler(callback, request_event, plugin_name)
                        )
                        plugin_tasks.add(task)
                        task.add_done_callback(plugin_tasks.discard)
                    else:
                        loop = asyncio.get_event_loop()
                        loop.run_in_executor(None, _safe_sync_handler, callback, request_event)
                except Exception as e:
                    print(f"插件处理请求事件失败: {e}")
                    import traceback
                    traceback.print_exc()
        ret = False

    return ret


async def prase_event(event: dict):
    """
    已弃用：请使用 parse_event()
    """
    import warnings
    warnings.warn(
        "prase_event() is deprecated, use parse_event() instead",
        DeprecationWarning,
        stacklevel=2
    )
    return await parse_event(event)


async def _safe_execute_handler(handler, event_obj, plugin_name=None):
    """安全地执行异步插件处理器（接收结构化事件对象）"""
    try:
        if plugin_name and hasattr(event_obj, '_plugin_name'):
            event_obj._plugin_name = plugin_name
        await handler(event_obj)
    except Exception as e:
        print(f"[-] 插件处理失败: {e}")
        import traceback
        traceback.print_exc()


def _safe_sync_handler(handler, event_obj):
    """安全地执行同步插件处理器（接收结构化事件对象）"""
    try:
        handler(event_obj)
    except Exception as e:
        print(f"[-] 同步插件处理失败: {e}")
        import traceback
        traceback.print_exc()