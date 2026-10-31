"""同步线程 → 主事件循环的 OneBot 调用桥。

Slang 脚本在 executor 线程中执行，而 OneBot API（``bot.call_api``）是协程。
本模块用 ``asyncio.run_coroutine_threadsafe`` 把协程提交到主事件循环并阻塞等待，
让同步的 Slang 函数可以调用任意 OneBot v11 action（群管动作等）。

前提：``core.globals.event_loop`` 指向正在运行的主循环、``bot_instance`` 已连接。
"""
import asyncio

from core import globals


class BotNotConnectedError(RuntimeError):
    """Bot 未连接 / 事件循环未运行时抛出。"""


def _get_running_loop():
    loop = globals.event_loop
    if loop is None or not loop.is_running():
        raise BotNotConnectedError('事件循环未运行，无法调用 OneBot API')
    if globals.bot_instance is None:
        raise BotNotConnectedError('Bot 实例未连接，无法调用 OneBot API')
    return loop


def call_api_sync(action: str, params: dict = None, timeout: float = 30.0,
                  plugin_name: str = None) -> dict:
    """同步调用任意 OneBot v11 API，返回服务端响应 dict。

    :param action: OneBot action 名（如 'set_group_ban'）
    :param params: action 参数
    :param timeout: 等待超时（秒）
    """
    loop = _get_running_loop()
    coro = globals.bot_instance.call_api(
        action, params if params is not None else {},
        plugin_name=plugin_name)
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


def send_message_sync(message, target_id=None, message_type: str = 'private',
                      timeout: float = 30.0, plugin_name: str = None) -> dict:
    """同步发送消息，返回服务端响应 dict。"""
    loop = _get_running_loop()
    coro = globals.bot_instance.send_message(
        message, target_id=target_id, message_type=message_type,
        plugin_name=plugin_name)
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)
