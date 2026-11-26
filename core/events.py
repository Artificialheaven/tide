import json

import websockets
import asyncio
from core import globals, history
from utils.obj import MessageEvent, MetaEvent, NoticeEvent, RequestEvent

try:
    import dearpygui.dearpygui as dpg
except ImportError:  # 无头/CI 环境没有 GUI 依赖时，事件分发仍应可用
    dpg = None

try:
    # if not globals.no_gui:
    from ui.main_ui import add_log
except Exception:  # GUI 日志模块不可用（无 GUI 依赖/无头模式）时降级为控制台输出
    def add_log(response, source, content, color=None):
        print(f"[{response}] [{source}] {content}")

from utils.plugin_loader import get_plugin_manager


# 存储正在运行的插件任务，用于管理和清理
plugin_tasks = set()

# 事件分发背压参数：
# 每条消息/通知/请求/申请都单独建 task 会让长跑 handler（例如持群锁的模型调用）
# 无限堆积事件对象与消息原文。这里改成「（插件，会话）维度有界队列 + 单 worker
# 串行」，同一会话内保证顺序、内存有上限；不同会话仍各自并发。
SESSION_QUEUE_MAXSIZE = 50          # 单会话队列上限，满时丢最旧事件
SESSION_WORKER_IDLE_TIMEOUT = 60.0  # worker 空闲超过该秒数自行退出

# (plugin_name, session_id) -> (asyncio.Queue, worker_task)
_session_workers: dict = {}


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
            data = event.get('data') or {}
            if event.get('retcode') not in (0, None) or not data:
                print(f"[系统] 获取登录信息失败（retcode={event.get('retcode')}）")
                globals.echo_dict[event['echo']] = event
                return False
            if globals.no_gui:
                print(f"登录信息: {data}")
            else:
                try:
                    dpg.set_value('name', f"{data.get('nickname')}({data.get('user_id')})")
                except Exception:
                    pass
            if data.get('user_id'):
                globals.bot_id = data['user_id']
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

    if event['post_type'] == 'message_sent':
        # 机器人自己发出的消息（NapCat 配置 reportSelfMessage=true 时上报的
        # message_sent 事件）。它不是"别人说的话"，既不能进消息历史（否则
        # 会把自己的发言当成群友发言喂回模型），也不能触发闸门/回复逻辑——
        # 否则会出现"自己回自己"。日志里已经由出站路径记过一次，这里直接丢弃。
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

                        # 协程 handler 投递到会话队列串行执行（有界背压），同步 handler 仍走线程池
                        if asyncio.iscoroutinefunction(callback):
                            _enqueue_handler(callback, msg_event, plugin_name)
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

                        # 协程 handler 投递到会话队列串行执行（有界背压），同步 handler 仍走线程池
                        if asyncio.iscoroutinefunction(callback):
                            _enqueue_handler(callback, msg_event, plugin_name)
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

                    # 协程 handler 投递到会话队列串行执行（有界背压），同步 handler 仍走线程池
                    if asyncio.iscoroutinefunction(callback):
                        _enqueue_handler(callback, notice_event, plugin_name)
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

                    # 协程 handler 投递到会话队列串行执行（有界背压），同步 handler 仍走线程池
                    if asyncio.iscoroutinefunction(callback):
                        _enqueue_handler(callback, request_event, plugin_name)
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


def _event_session_id(event_obj) -> str:
    """取事件所属会话 id：群聊用群号，私聊/通知/请求用 QQ 号，取不到时用空字符串。"""
    for attr in ('group_id', 'user_id'):
        value = getattr(event_obj, attr, None)
        if value:
            return str(value)
    return ''


def _event_type_name(event_obj) -> str:
    """事件类型名（仅用于背压日志）。"""
    for attr in ('post_type', 'message_type', 'notice_type', 'request_type', 'meta_event_type'):
        value = getattr(event_obj, attr, None)
        if value:
            return str(value)
    return type(event_obj).__name__


def _enqueue_handler(callback, event_obj, plugin_name):
    """把异步插件 handler 投递到（插件，会话）维度的有界队列。

    同一会话内由单个 worker 串行执行，保证顺序；队列满时丢弃最旧事件并告警，
    避免长跑 handler 导致事件对象与消息原文无上限堆积。
    """
    session = _event_session_id(event_obj)
    key = (plugin_name, session)
    entry = _session_workers.get(key)
    if entry is None or entry[1].done():
        queue = asyncio.Queue(maxsize=SESSION_QUEUE_MAXSIZE)
        worker = asyncio.create_task(_session_worker(key, queue))
        entry = (queue, worker)
        _session_workers[key] = entry
        plugin_tasks.add(worker)
        worker.add_done_callback(plugin_tasks.discard)
    queue = entry[0]
    try:
        queue.put_nowait((callback, event_obj))
    except asyncio.QueueFull:
        dropped = queue.get_nowait()
        queue.put_nowait((callback, event_obj))
        print(
            f"[背压] 插件 {plugin_name} 会话 {session or '<未知>'} 队列已满"
            f"（上限 {SESSION_QUEUE_MAXSIZE}），丢弃最旧事件（{_event_type_name(dropped[1])}）"
        )


async def _next_session_event(queue):
    """从会话队列取下一项；空闲超过阈值返回 None。

    用 asyncio.wait 而不是 wait_for 包裹 queue.get()，避免超时瞬间把刚取出的事件
    一起取消掉（Queue.get 只在拿到事件后才真正出队，被取消时事件仍在队列里）。
    """
    getter = asyncio.ensure_future(queue.get())
    try:
        done, _ = await asyncio.wait({getter}, timeout=SESSION_WORKER_IDLE_TIMEOUT)
    except asyncio.CancelledError:
        getter.cancel()
        raise
    if getter in done:
        return getter.result()
    getter.cancel()
    try:
        return await getter
    except asyncio.CancelledError:
        return None


async def _session_worker(key, queue):
    """单会话串行 worker：按序执行事件，空闲超时自动退出并清理登记项。"""
    plugin_name, _session = key
    worker = asyncio.current_task()
    try:
        while True:
            item = await _next_session_event(queue)
            if item is None:
                return
            callback, event_obj = item
            try:
                await _safe_execute_handler(callback, event_obj, plugin_name)
            except asyncio.CancelledError:
                # worker 自身被取消（关停）→ 退出；单个 handler 内部冒出的
                # CancelledError 不应拖垮整个会话队列、丢掉后续事件
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
            finally:
                queue.task_done()
    finally:
        entry = _session_workers.get(key)
        if entry is not None and entry[1] is worker:
            _session_workers.pop(key, None)


async def cancel_plugin_tasks(timeout: float = 2.0) -> int:
    """取消全部在途插件任务与会话 worker，返回被取消的任务数。

    入站任务在重连/退出时必须先于插件 shutdown 取消，否则旧实例的 handler 会在
    新实例导入后继续处理事件。使用 gather(return_exceptions=True) 做有界等待，
    单条任务异常或取消不会影响其它任务；同时清空会话 worker 登记表。
    """
    tasks = [task for task in plugin_tasks if not task.done()]
    for _queue, worker in list(_session_workers.values()):
        if not worker.done() and worker not in tasks:
            tasks.append(worker)
    # 先清空登记，避免取消过程中又往旧 worker 里投递
    _session_workers.clear()
    plugin_tasks.clear()
    if not tasks:
        return 0
    for task in tasks:
        try:
            task.cancel()
        except Exception:
            pass
    gatherer = asyncio.gather(*tasks, return_exceptions=True)
    try:
        await asyncio.wait_for(gatherer, timeout=timeout)
    except TimeoutError:
        print(f"[关闭] 等待插件任务退出超时（{timeout}s），仍有任务未结束")
    except asyncio.CancelledError:
        gatherer.cancel()
        raise
    except Exception as e:  # 跨 loop 等异常不应阻断关停流程
        print(f"[关闭] 等待插件任务退出异常: {e}")
    return len(tasks)


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