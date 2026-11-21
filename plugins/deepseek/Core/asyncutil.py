"""插件内 asyncio 关停原语（自包含，兼容没有 utils.asyncutil 的旧框架）。

新版框架在 ``utils.asyncutil`` 提供同一组实现；但插件源码包常被单独部署到
旧框架里，直接 ``from utils.asyncutil import ...`` 会抛
``No module named 'utils.asyncutil'``，导致整个插件加载失败。

这里先定义本地等价实现，模块末尾再尝试用框架版本覆盖：
- 新框架：与框架共用同一套函数和 _PENDING 回收；
- 旧框架：自动留在本地实现，插件仍可正常加载/卸载。
"""
from __future__ import annotations

import asyncio

# 超时后仍未结束的残余 task：保活防 GC 报 "Task was destroyed but it is pending"
_PENDING: set[asyncio.Task] = set()


def _retrieve(task: asyncio.Task) -> None:
    """取回异常，消除 "exception was never retrieved" 警告。"""
    if task.done() and not task.cancelled():
        try:
            task.exception()
        except Exception:
            pass


async def wait_task_resilient(task: asyncio.Task, timeout: float) -> bool:
    """有界等待一个已存在的 task，返回 True=已结束，False=超时残留。

    ``wait_for(shield(task))`` 保证不向被等待 task 转发取消；外层任务收到
    CancelledError 时吞掉不重抛，继续等到 deadline。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            # 被等待 task 自身被我方 cancel（done → 退出）；
            # 外层任务被外部 cancel（未 done → 吞掉继续有界等待）
            if task.done():
                break
            continue
        except Exception:
            # task 抛异常结束：wait_for 把异常重抛到这里，取回后正常返回
            break
    _retrieve(task)
    return True


async def wait_resilient(coro, timeout: float) -> tuple[bool, asyncio.Task]:
    """自行 ensure_future 后有界等待，返回 (是否结束, task)。"""
    task = asyncio.ensure_future(coro)
    ok = await wait_task_resilient(task, timeout)
    if not ok:
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)
    return ok, task


async def cancel_task(task: asyncio.Task | None, timeout: float = 3.0) -> None:
    """cancel + 有界等待；None/已结束直接返回，任何异常都不冒泡。"""
    if task is None:
        return
    if task.done():
        _retrieve(task)
        return
    task.cancel()
    ok = await wait_task_resilient(task, timeout)
    if not ok:
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)


async def drain_pending(timeout: float = 3.0) -> None:
    """进程级最终回收：有界等待所有超时残余 task。"""
    pending = [t for t in list(_PENDING) if not t.done()]
    if not pending:
        return
    # gather 返回的是 Future 不是协程，create_task 会 TypeError，用 ensure_future
    gathered = asyncio.ensure_future(
        asyncio.gather(*pending, return_exceptions=True))
    await wait_task_resilient(gathered, timeout)


try:  # 框架较新：复用框架实现（drain_pending 才能统一回收插件任务）
    from utils.asyncutil import (  # type: ignore  # noqa: F401
        cancel_task, drain_pending, wait_resilient, wait_task_resilient)
except Exception:
    # 旧框架没有该模块：保留上面的本地实现
    pass
