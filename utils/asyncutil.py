"""关停期通用 asyncio 原语：cancel + 有界等待 + 隔离外部 CancelledError。

背景：Python 3.8+ CancelledError 继承 BaseException，关停链上常见的
``except Exception`` 挡不住它，一个取消会腰斩后续全部清理。本模块的原语把
取消封在等待边界：外层任务被 cancel 时，被等待的兄弟 task 不受影响，
在自己的 task 上下文里跑完（MCP anyio 生成器等资源才能优雅 aclose）。
"""
from __future__ import annotations

import asyncio

# 超时后仍未结束的残余 task：保活防 GC 报 "Task was destroyed but it is pending"，
# 进程退出前由 drain_pending 做最后一次有界回收
_PENDING: set[asyncio.Task] = set()


def _retrieve(task: asyncio.Task) -> None:
    """取回异常，消除 "exception was never retrieved" 警告。"""
    if task.done() and not task.cancelled():
        try:
            task.exception()
        except Exception:
            pass


async def wait_task_resilient(task: asyncio.Task, timeout: float) -> bool:
    """有界等待一个**已存在**的 task，返回 True=已结束（正常/异常/被取消），False=超时残留。

    - ``wait_for(shield(task))`` 保证本函数从不向被等待 task 转发取消；
    - 外层任务收到 CancelledError 时吞掉不重抛，只要 task 未结束就继续等到
      deadline（3.10 下 CancelledError 捕获即消费，无 3.11 的 cancel 计数问题）；
    - task 自身抛异常时函数正常返回 True，异常经 _retrieve 取回不外冒。
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
            # 两种来源：被等待 task 自身被我方 cancel（done → 退出）；
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
    """便利封装：自行 ensure_future 后走 resilient 等待，返回 (是否结束, task)。

    超时残留的 task 进 _PENDING 保活，等待 drain_pending 统一回收。
    """
    task = asyncio.ensure_future(coro)
    ok = await wait_task_resilient(task, timeout)
    if not ok:
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)
    return ok, task


async def cancel_task(task: asyncio.Task | None, timeout: float = 3.0) -> None:
    """替换各管理器 ``cancel + wait_for(shield) + except Exception`` 旧模板。

    None/已结束的 task 直接返回；任何异常（含外层 CancelledError）都不冒泡。
    """
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
