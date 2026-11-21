# -*- coding: utf-8 -*-
"""关停韧性：外部 CancelledError 不再腰斩 on_stop 清理链。

覆盖五组：
A. utils.asyncutil 原语（关键回归：外层 cancel 不穿透 wait_task_resilient）
B. PluginManager.ashutdown_all_plugins 兄弟 task 隔离与实例级关停标记
C. 热重载后旧实例标记不污染同名新实例
D. deepseek MCP/LLM 关闭路径在 CancelledError 下仍重置状态
E. ShutdownController 信号桥（exiter 注入 fake，不真退进程）

无 pytest-asyncio：每个用例手工 new_event_loop/run_until_complete/close。
从仓库根运行：python -m pytest tests/test_shutdown_resilience.py
"""
import asyncio
import os
import signal
import sys

# 仓库根（python -m pytest 从根跑时已在 sys.path，这里显式兜底）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
# deepseek 插件内模块按 Core.xxx 导入
_DS = os.path.join(_ROOT, 'plugins', 'deepseek')
if _DS not in sys.path:
    sys.path.insert(0, _DS)

from utils.asyncutil import (  # noqa: E402
    cancel_task, drain_pending, wait_resilient, wait_task_resilient)
from utils.plugin_loader import (  # noqa: E402
    PluginManager, _STOP_MARK, _mark_stopped)
from core.signals import ShutdownController  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── A. asyncutil 原语 ──────────────────────────────────────────────

def test_cancel_task_none_and_done_are_noop():
    async def case():
        done = asyncio.ensure_future(asyncio.sleep(0))
        await done
        await cancel_task(None)
        await cancel_task(done)          # 已结束：不报错、不抛
        assert done.done()
    _run(case())


def test_cancel_task_stops_pending_without_raising():
    async def case():
        task = asyncio.ensure_future(asyncio.sleep(10))
        await cancel_task(task, timeout=2.0)
        assert task.cancelled()
    _run(case())


def test_wait_task_resilient_timeout_returns_false():
    async def case():
        task = asyncio.ensure_future(asyncio.sleep(10))
        ok = await wait_task_resilient(task, timeout=0.05)
        assert ok is False
        assert not task.done()
        await cancel_task(task, timeout=2.0)
    _run(case())


def test_outer_cancel_does_not_propagate_and_inner_finishes():
    """关键回归：外层任务被 cancel，CancelledError 在等待边界被吞掉，
    被等待的兄弟 task 无感跑完，外层任务正常返回而不是被取消。"""
    async def inner():
        await asyncio.sleep(0.05)
        return 'done'

    async def outer():
        task = asyncio.ensure_future(inner())
        ok = await wait_task_resilient(task, timeout=5.0)
        return ok, task

    loop = asyncio.new_event_loop()
    try:
        outer_t = loop.create_task(outer())
        loop.call_soon(outer_t.cancel)       # 模拟 asyncio.run 关停注入
        ok, inner_t = loop.run_until_complete(outer_t)
        assert not outer_t.cancelled()       # 取消被消化，外层活下来
        assert ok is True
        assert inner_t.done() and not inner_t.cancelled()
        assert inner_t.result() == 'done'
    finally:
        loop.close()


def test_wait_resilient_timeout_then_drain_reclaims():
    async def case():
        ok, task = await wait_resilient(asyncio.sleep(0.3), timeout=0.05)
        assert ok is False
        assert not task.done()
        await drain_pending(timeout=2.0)     # 残余 task 被有界回收
        assert task.done()
    _run(case())


def test_drain_pending_empty_is_safe():
    _run(drain_pending())


# ── B. ashutdown_all_plugins 隔离 ─────────────────────────────────

class FakePlugin:
    def __init__(self, name, mode='ok'):
        self.name = name
        self.mode = mode
        self.calls = 0

    async def on_stop(self):
        self.calls += 1
        if self.mode == 'cancel':
            raise asyncio.CancelledError()
        if self.mode == 'sleep':
            await asyncio.sleep(0.05)


def _manager_with(*plugins):
    mgr = PluginManager(None)
    for p in plugins:
        mgr.plugins[p.name] = p
    return mgr


def test_ashutdown_cancelled_plugin_does_not_block_others():
    async def case():
        a = FakePlugin('a', mode='cancel')
        b = FakePlugin('b')
        mgr = _manager_with(a, b)
        await mgr.ashutdown_all_plugins(timeout=2.0)   # 不冒泡
        assert a.calls == 1 and b.calls == 1
        assert getattr(a, _STOP_MARK, False)
        assert getattr(b, _STOP_MARK, False)
        # 第二次关停：已标记实例全部跳过
        await mgr.ashutdown_all_plugins(timeout=2.0)
        assert a.calls == 1 and b.calls == 1
    _run(case())


def test_ashutdown_survives_outer_cancel_and_runs_everything():
    loop = asyncio.new_event_loop()
    try:
        a = FakePlugin('a', mode='sleep')
        b = FakePlugin('b', mode='sleep')
        mgr = _manager_with(a, b)
        outer = loop.create_task(mgr.ashutdown_all_plugins(timeout=5.0))
        loop.call_soon(outer.cancel)
        loop.run_until_complete(outer)        # 不抛 CancelledError
        assert not outer.cancelled()
        assert a.calls == 1 and b.calls == 1  # 两段 on_stop 都在兄弟 task 里跑完
        assert getattr(a, _STOP_MARK, False)
        assert getattr(b, _STOP_MARK, False)
    finally:
        loop.close()


def test_adrain_background_reclaims_slow_on_stop():
    async def case():
        slow = FakePlugin('slow', mode='sleep')   # 0.05s
        mgr = _manager_with(slow)
        await mgr.ashutdown_all_plugins(timeout=0.01)  # 超时但不报错
        assert slow.calls == 1                          # 兄弟 task 已在跑
        assert getattr(slow, _STOP_MARK, False)
        await mgr.adrain_background(timeout=2.0)
        assert mgr._bg_stops == set()
    _run(case())


# ── C. 热重载标记不污染新实例 ─────────────────────────────────────

class SyncFakePlugin:
    def __init__(self):
        self.calls = 0

    def on_stop(self):
        self.calls += 1


def test_reload_marker_does_not_poison_same_name_new_instance():
    old = SyncFakePlugin()
    new = SyncFakePlugin()
    mgr = PluginManager(None)
    mgr.plugins['p'] = old

    mgr.unload_plugin('p')                    # 旧实例停一次并打标记、移除
    assert old.calls == 1
    assert getattr(old, _STOP_MARK, False)

    mgr.plugins['p'] = new                   # 同名新实例（模拟热重载）
    mgr.shutdown_all_plugins()               # atexit 同步关停
    assert new.calls == 1                    # 新实例必须能被停到
    assert old.calls == 1                    # 旧标记实例不重复停
    assert getattr(new, _STOP_MARK, False)


def test_mark_stopped_tolerates_slots():
    class Slotted:
        __slots__ = ()

    inst = Slotted()
    _mark_stopped(inst)                       # 不抛：__slots__ 兜底
    assert not getattr(inst, _STOP_MARK, False)


# ── D. deepseek MCP / LLM 关闭路径 ────────────────────────────────

def test_mcp_close_resets_handle_even_when_aclose_cancelled():
    from Core.mcp_manager import McpHandle, McpManager

    class FakeStack:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True
            raise asyncio.CancelledError()

    async def case():
        mgr = McpManager('dummy_dir', 'dummy.db')
        h = McpHandle('s1')
        stack = FakeStack()
        h.stack, h.session, h.status = stack, object(), 'running'
        mgr._handles['s1'] = h

        await mgr._close(h)                  # BaseException 也兜在 finally 外
        assert stack.closed
        assert h.status == 'stopped'
        assert h.stack is None and h.session is None
    _run(case())


def test_mcp_shutdown_clears_handles_under_cancel():
    from Core.mcp_manager import McpHandle, McpManager

    class FakeStack:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True
            raise asyncio.CancelledError()

    async def case():
        mgr = McpManager('dummy_dir', 'dummy.db')
        h = McpHandle('s2')
        stack = FakeStack()
        h.stack, h.session, h.status = stack, object(), 'running'
        mgr._handles['s2'] = h

        await mgr.shutdown(total_budget=2.0)
        assert stack.closed
        assert mgr._handles == {}            # finally 必达，不留半连接登记

        await mgr.shutdown()                 # 空 handles 早退不报错
    _run(case())


def test_llm_aclose_resets_state_under_cancel():
    from Core.llm import LLMClient

    class FakeClient:
        def __init__(self, exc=None):
            self.exc = exc
            self.closed = False

        async def close(self):
            self.closed = True
            if self.exc is not None:
                raise self.exc

    async def case():
        llm = LLMClient()
        good = FakeClient()
        bad = FakeClient(asyncio.CancelledError())
        llm._clients = {'a': good, 'b': bad}
        llm._sigs = {'a': ('k', 'u'), 'b': ('k2', 'u2')}

        await llm.aclose()                   # 坏 client 不连累好 client 与状态重置
        assert good.closed and bad.closed
        assert llm._clients == {} and llm._sigs == {}
    _run(case())


# ── E. ShutdownController 信号桥 ──────────────────────────────────

def test_signal_sets_async_event_and_second_signal_exits():
    exits = []
    ctrl = ShutdownController(exiter=lambda code: exits.append(code),
                              poll_interval=0.02)

    async def case():
        await ctrl.attach()
        ctrl._on_signal(signal.SIGINT, None)         # 第一次：优雅关停
        await asyncio.sleep(0.08)                    # 等轮询桥（0.02s）
        assert ctrl.event.is_set()
        assert ctrl.signalled is True
        ctrl._on_signal(signal.SIGINT, None)         # 第二次：强退
        assert exits == [1]
        assert await ctrl.wait_interruptible(0.1) is True
        await ctrl.detach()
    _run(case())


def test_wait_interruptible_timeout_branch_and_park():
    ctrl = ShutdownController(poll_interval=0.02)

    async def case():
        await ctrl.attach()
        assert await ctrl.wait_interruptible(0.05) is False   # 睡满
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, ctrl._on_signal, signal.SIGINT, None)
        await ctrl.park()                                     # 信号后返回
        assert ctrl.signalled is True
        await ctrl.detach()
    _run(case())


def test_install_restore_reinstates_old_handlers():
    ctrl = ShutdownController()
    old = signal.getsignal(signal.SIGINT)
    try:
        ctrl.install()
        assert signal.getsignal(signal.SIGINT) == ctrl._on_signal
        ctrl.restore()
        assert signal.getsignal(signal.SIGINT) == old
    finally:
        signal.signal(signal.SIGINT, old)
