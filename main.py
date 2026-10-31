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


def start_background_data_fetch(no_gui=False):
    """在后台启动数据获取任务"""
    if no_gui:
        print("[系统] 无GUI模式，跳过数据获取")
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
    thread.start()
    print("[系统] 后台数据获取任务已启动")


def create_connector():
    """按配置选择 NapCat（OneBot WebSocket 客户端）或官方机器人适配连接。"""
    transport = str(configs.conf.get("transport", "napcat") or "napcat").lower()
    if transport == "official":
        from core.official.config import OfficialConfig
        from core.official.connection import OfficialConnection
        return OfficialConnection(OfficialConfig.from_conf(configs.conf))
    uri = configs.conf.get("ws_address", "ws://47.93.160.124:3001")
    headers = {"Authorization": configs.conf.get("token", "test")}
    return websockets.connect(uri, additional_headers=headers)


async def connect_and_run(no_gui=False):
    """
    连接WebSocket并运行主循环
    使用循环而非递归来避免栈溢出
    """
    transport = str(configs.conf.get("transport", "napcat") or "napcat").lower()
    if transport == "official":
        import os as _os
        target = (configs.conf.get("official_app_id")
                  or _os.environ.get("QQBOT_APP_ID") or "QQ官方机器人")
    else:
        target = configs.conf.get("ws_address", "ws://47.93.160.124:3001")

    # 从配置中读取重连参数
    max_attempts = configs.conf.get("max_reconnect_attempts", 0)  # 0表示无限重试
    base_delay = configs.conf.get("reconnect_base_delay", 2)
    max_delay = configs.conf.get("reconnect_max_delay", 60)

    reconnect_count = 0

    while True:
        # 检查是否超过最大重连次数
        if max_attempts > 0 and reconnect_count >= max_attempts:
            print(f'[系统] 已达到最大重连次数 ({max_attempts})，停止重连')
            break

        # 计算重连延迟（指数退避）
        if reconnect_count > 0:
            delay = min(base_delay * (2 ** (reconnect_count - 1)), max_delay)
            print(f'[系统] 将在 {delay:.1f} 秒后尝试第 {reconnect_count + 1} 次重连...')
            await asyncio.sleep(delay)

        try:
            print(f'[系统] 正在连接到 {target}...')

            async with create_connector() as websocket:
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
                    start_web_server(
                        host=web_host, port=web_port,
                        token=configs.conf.get("token", "test")
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
        
        except websockets.exceptions.InvalidStatusCode as e:
            print(f'[系统] 连接失败: HTTP {e.status_code}')
            reconnect_count += 1
            continue
        
        except websockets.exceptions.InvalidURI:
            print(f'[系统] 无效的连接地址: {target}')
            break

        except OfficialConfigError as e:
            # 配置缺失/非法重试也不会变好，直接停下并提示怎么修
            print(f'[系统] 官方机器人配置错误: {e}')
            print('[系统] 请在 config.json 填写 official_app_id / '
                  'official_app_secret（GUI「框架配置」窗口或 Web 面板也可改），'
                  '或设置环境变量 QQBOT_APP_ID / QQBOT_APP_SECRET；改完重启程序')
            break
        
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

        # 通知全部插件执行 on_stop（exit.* 事件规则等）
        _manager = get_plugin_manager()
        if _manager is not None:
            await _manager.ashutdown_all_plugins()

        # 清空当前的bot实例和websocket
        globals.bot_instance = None
        globals.websocket = None
        
        # 增加重连计数
        reconnect_count += 1


async def start(no_gui=False):
    """启动函数"""
    print('[系统] 框架正在启动中...')
    
    try:
        await connect_and_run(no_gui=no_gui)
    except Exception as e:
        print(f'[系统] 启动失败: {type(e).__name__}: {e}')
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    print("DearQQ启动中...")
    
    # 检查是否有 -nogui 参数

    
    if no_gui:
        print("[系统] 无GUI模式启动")
        # 无GUI模式，直接运行
        asyncio.run(start(no_gui=True))
    else:
        # GUI模式
        from ui import main_ui
        keep_loop = True
        ui = main_ui.UI()
        threading.Thread(target=lambda: asyncio.run(start(no_gui=False))).start()
        ui.setup()