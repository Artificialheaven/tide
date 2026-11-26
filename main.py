import asyncio
import websockets
import threading
import json
import time
import sys

from core import globals

no_gui = '-nogui' in sys.argv
# 设置全局 no_gui 标志
globals.no_gui = no_gui

from core import events
import configs
from core.official.config import OfficialConfigError
from utils.plugin_loader import init_plugin_manager, get_plugin_manager
from core.bot import bot, logger


async def init_bots(websocket: websockets.ClientConnection):
    data = {
        "action": "get_login_info",
        "params": {},
        "echo": "get_login_info"
    }
    await websocket.send(json.dumps(data, ensure_ascii=False))


_bg_fetch_thread = None


def start_background_data_fetch(no_gui=False):
    """在后台启动数据获取任务（每次重连只保留一个线程）"""
    global _bg_fetch_thread
    if no_gui:
        print("[系统] 无GUI模式，跳过数据获取")
        return
    if _bg_fetch_thread is not None and _bg_fetch_thread.is_alive():
        return
    
    from ui.report_window import fetch_and_update_data
    
    def fetch_task():
        # 等待 Bot 初始化完成
        time.sleep(2)
        
        if globals.bot_instance:
            print("[系统] 开始在后台获取群列表和好友列表...")
            fetch_and_update_data()
        else:
            print("[系统] Bot 未初始化，跳过数据获取")
    
    # 启动后台线程
    thread = threading.Thread(target=fetch_task, daemon=True)
    _bg_fetch_thread = thread
    thread.start()
    print("[系统] 后台数据获取任务已启动")


def create_connector():
    """按配置选择 NapCat（OneBot WebSocket 客户端）或官方机器人适配连接。"""
    transport = str(configs.conf.get("transport", "napcat") or "napcat").lower()
    if transport == "official":
        from core.official.config import OfficialConfig
        from core.official.connection import OfficialConnection
        return OfficialConnection(OfficialConfig.from_conf(configs.conf))
    uri = configs.conf.get("ws_address", "ws://127.0.0.1:3001")
    headers = {"Authorization": "Bearer " + configs.conf.get("token", "test")}
    return websockets.connect(uri, additional_headers=headers)


async def _teardown_connection():
    """一次连接会话的收尾：先取消入站事件任务，再保证插件 on_stop 执行、全局单例清理干净。"""
    # 先取消入站事件任务与会话 worker：避免旧实例的 handler 在插件重载后继续处理事件
    try:
        _cancelled = await events.cancel_plugin_tasks()
        if _cancelled:
            print(f'[系统] 已取消 {_cancelled} 个在途事件任务')
    except BaseException:
        print('[系统] 取消在途事件任务时出错:')
        import traceback
        traceback.print_exc()
    _manager = get_plugin_manager()
    if _manager is not None:
        try:
            await _manager.ashutdown_all_plugins()
        except BaseException:
            print('[系统] 关闭插件时出错:')
            import traceback
            traceback.print_exc()
        # 有界回收 on_stop 超时残留的兄弟 task，避免它们与下一次连接的
        # 新实例并发操作 DB/MCP/HTTP 服务
        try:
            await _manager.adrain_background()
        except BaseException:
            pass
    globals.bot_instance = None
    globals.websocket = None


async def connect_and_run(no_gui=False, shutdown=None):
    """
    连接WebSocket并运行主循环
    使用循环而非递归来避免栈溢出

    shutdown：nogui 模式传入的 ShutdownController（信号桥），GUI 模式为 None。
    返回值：'give_up'=重连次数耗尽、'signal'=收到退出信号、'fatal'=不可恢复配置错误。
    """
    transport = str(configs.conf.get("transport", "napcat") or "napcat").lower()
    if transport == "official":
        import os as _os
        target = (configs.conf.get("official_app_id")
                  or _os.environ.get("QQBOT_APP_ID") or "QQ官方机器人")
    else:
        target = configs.conf.get("ws_address", "ws://127.0.0.1:3001")

    # 从配置中读取重连参数（0 表示无限重试；缺省兜底 10 次）
    max_attempts = configs.conf.get("max_reconnect_attempts", 10)
    base_delay = configs.conf.get("reconnect_base_delay", 2)
    max_delay = configs.conf.get("reconnect_max_delay", 60)

    reconnect_count = 0

    while True:
        # 检查是否超过最大重连次数
        if max_attempts > 0 and reconnect_count >= max_attempts:
            print(f'[系统] 已达到最大重连次数 ({max_attempts})，停止重连')
            return 'give_up'

        # 计算重连延迟（指数退避）
        if reconnect_count > 0:
            delay = min(base_delay * (2 ** (reconnect_count - 1)), max_delay)
            print(f'[系统] 将在 {delay:.1f} 秒后尝试第 {reconnect_count + 1} 次重连...')
            if shutdown is not None:
                # 退避期可被信号立即唤醒，不等满 delay
                if await shutdown.wait_interruptible(delay):
                    print('[系统] 退避期间收到退出信号，停止重连')
                    return 'signal'
            else:
                await asyncio.sleep(delay)

        try:
            print(f'[系统] 正在连接到 {target}...')

            async with create_connector() as websocket:
                try:
                    globals.websocket = websocket

                    # 重置重连计数
                    reconnect_count = 0

                    print('[系统] WebSocket连接成功')

                    # 保存事件循环引用，供热重载线程安全调度
                    globals.event_loop = asyncio.get_running_loop()

                    # 初始化日志记录器
                    # 检查是否启用 Web 管理面板
                    enable_web = configs.conf.get("enable_web_admin", False)
                    web_port = configs.conf.get("web_admin_port", 8081)
                    web_host = str(configs.conf.get("web_admin_host", "0.0.0.0")
                                   or "0.0.0.0").strip()

                    if enable_web:
                        # 启动 Web 管理面板（host/port 都可在 config.json 或面板「配置」页改，重启生效）
                        from ui.web.server import start_web_server, web_logger
                        # 面板密码优先用 web_admin_token；未配置时回退到 WS token（旧配置兼容）
                        web_token = (configs.conf.get("web_admin_token")
                                     or configs.conf.get("token", "test"))
                        start_web_server(
                            host=web_host, port=web_port,
                            token=web_token
                        )
                        print(f"[系统] Web 管理面板已启动: http://{web_host}:{web_port}")
                        bot_logger = logger(web_logger)
                    elif not no_gui:
                        from ui import main_ui
                        bot_logger = logger(main_ui.add_log)
                    else:
                        # 无GUI且未启用Web → 纯控制台日志
                        def console_logger(level, source, message, color=None):
                            print(f"[{level}] [{source}] {message}")
                        bot_logger = logger(console_logger)

                    # 初始化Bot实例
                    bot_instance = bot(websocket, bot_logger)
                    globals.bot_instance = bot_instance

                    print('[系统] Bot实例已创建')

                    # 确保事件循环已经就绪
                    try:
                        loop = asyncio.get_running_loop()
                        print(f'[系统] 事件循环已就绪: {loop}')
                    except RuntimeError as e:
                        print(f'[系统] 警告: 事件循环未就绪: {e}')

                    # 初始化插件管理器（此时bot_instance已经设置到globals中）
                    print("开始初始化插件管理器...")
                    manager = init_plugin_manager(websocket)
                    print(f"插件管理器初始化完成: {manager}")

                    print(f'[系统] 插件管理器已初始化，加载了 {len(manager.plugins)} 个插件')

                    # 注册插件 Web Blueprint（在 Web 管理已启用时）
                    if enable_web:
                        from ui.web.server import register_plugin_blueprints
                        register_plugin_blueprints(manager)

                    # 执行待处理的 on_start 任务
                    if hasattr(globals, 'pending_on_start_tasks') and globals.pending_on_start_tasks:
                        print(f"开始执行 {len(globals.pending_on_start_tasks)} 个待处理的 on_start 任务")
                        for on_start_method, plugin_name in globals.pending_on_start_tasks:
                            try:
                                # 在当前事件循环中创建任务
                                asyncio.create_task(manager._safe_call_on_start(on_start_method, plugin_name))
                                print(f"[{plugin_name}] on_start 任务已创建")
                            except Exception as e:
                                print(f"[{plugin_name}] 创建 on_start 任务失败: {e}")

                        # 清空待处理列表
                        globals.pending_on_start_tasks.clear()

                    # 发送登录信息请求
                    await init_bots(websocket)

                    # 启动后台数据获取（在插件加载完成后）
                    start_background_data_fetch(no_gui=no_gui)

                    # 主循环：接收和处理消息
                    while True:
                        try:
                            if shutdown is not None:
                                # recv 与信号竞速：official 传输的 recv 阻塞在队列上、
                                # 连接对象也没有 close()，关连接唤不醒，只能靠竞速统一处理
                                recv_task = asyncio.create_task(websocket.recv())
                                sig_task = asyncio.create_task(shutdown.event.wait())
                                try:
                                    done, _pending = await asyncio.wait(
                                        {recv_task, sig_task},
                                        return_when=asyncio.FIRST_COMPLETED)
                                finally:
                                    if not sig_task.done():
                                        sig_task.cancel()
                                if sig_task in done:
                                    recv_task.cancel()
                                    await asyncio.gather(recv_task,
                                                         return_exceptions=True)
                                    print('[系统] 收到退出信号，关闭当前连接')
                                    break
                                _response = recv_task.result()
                            else:
                                _response = await websocket.recv()
                            if '"meta_event_type":"heartbeat"' not in _response:
                                print(f"Received response: {_response}")
                            response = json.loads(_response)
                            # print(response)

                            # 检查是否是某个请求的响应（通过echo字段匹配）
                            if 'echo' in response and response['echo']:
                                if globals.bot_instance and globals.bot_instance.handle_response(response['echo'], response):
                                    # 这是一个API调用的响应，已经被Bot处理了
                                    continue

                            # 处理其他事件
                            if await events.parse_event(response):
                                print(f'Received: {_response}')

                        except websockets.exceptions.ConnectionClosed as e:
                            print(f'[系统] WebSocket连接关闭: {e.code} - {e.reason}')
                            break

                        except websockets.exceptions.ConnectionClosedError as e:
                            print(f'[系统] WebSocket连接错误: {e}')
                            break

                        except Exception as e:
                            print(f'[系统] 处理消息时出错: {type(e).__name__}: {e}')
                            import traceback
                            traceback.print_exc()
                            break
                finally:
                    await _teardown_connection()

        
        except websockets.exceptions.InvalidStatusCode as e:
            print(f'[系统] 连接失败: HTTP {e.status_code}')
            reconnect_count += 1
            continue
        
        except websockets.exceptions.InvalidURI:
            print(f'[系统] 无效的连接地址: {target}')
            return 'fatal'

        except OfficialConfigError as e:
            # 配置缺失/非法重试也不会变好，直接停下并提示怎么修
            print(f'[系统] 官方机器人配置错误: {e}')
            print('[系统] 请在 config.json 填写 official_app_id / '
                  'official_app_secret（GUI「框架配置」窗口或 Web 面板也可改），'
                  '或设置环境变量 QQBOT_APP_ID / QQBOT_APP_SECRET；改完重启程序')
            return 'fatal'
        
        except ConnectionRefusedError:
            print('[系统] 连接被拒绝，请检查服务器是否运行')
            reconnect_count += 1
            continue
        
        except OSError as e:
            print(f'[系统] 网络错误: {e}')
            reconnect_count += 1
            continue
        
        except Exception as e:
            print(f'[系统] 连接错误: {type(e).__name__}: {e}')
            reconnect_count += 1
            continue
        
        # 如果到达这里，说明内层while循环break了（连接断开）
        print('[系统] 检测到连接断开，准备重连...')

        # 信号触发的断线：收尾已完成，不再重连
        if shutdown is not None and shutdown.signalled:
            return 'signal'

        # 增加重连计数
        reconnect_count += 1


async def start(no_gui=False, shutdown=None):
    """启动函数。shutdown 为 nogui 信号桥，GUI 模式传 None。"""
    print('[系统] 框架正在启动中...')

    if shutdown is not None:
        await shutdown.attach()
    try:
        reason = await connect_and_run(no_gui=no_gui, shutdown=shutdown)
        if reason == 'give_up' and shutdown is not None:
            # 进程留存不退出：事件循环停车，Web 面板等线程继续服务，等信号收尾
            print('[系统] 已放弃重连，进程保持存活，等待退出信号（Ctrl+C 退出）')
            await shutdown.park()
    except Exception as e:
        print(f'[系统] 启动失败: {type(e).__name__}: {e}')
        import traceback
        traceback.print_exc()
    finally:
        # 有界回收 on_stop 残余任务，保证循环关闭前没有悬挂 task/生成器
        try:
            _manager = get_plugin_manager()
            if _manager is not None:
                await _manager.adrain_background(timeout=5.0)
        except Exception:
            import traceback
            traceback.print_exc()
        try:
            from utils.asyncutil import drain_pending
            await drain_pending(timeout=3.0)
        except Exception:
            import traceback
            traceback.print_exc()
        if shutdown is not None:
            await shutdown.detach()


if __name__ == "__main__":
    print("DearQQ启动中...")
    
    # 检查是否有 -nogui 参数

    
    if no_gui:
        print("[系统] 无GUI模式启动")
        # 信号处理只能在主线程装：signal.signal → threading.Event → 桥接 task
        from core.signals import ShutdownController
        shutdown = ShutdownController()
        shutdown.install()
        try:
            asyncio.run(start(no_gui=True, shutdown=shutdown))
        finally:
            shutdown.restore()
    else:
        # GUI模式（asyncio 在子线程，不装信号桥，shutdown=None 时新分支全部 no-op）
        from ui import main_ui
        keep_loop = True
        ui = main_ui.UI()
        threading.Thread(target=lambda: asyncio.run(
            start(no_gui=False, shutdown=None)), daemon=True).start()
        ui.setup()