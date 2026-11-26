# -*- coding: utf-8 -*-
"""审计 2026-11-26 生命周期修复回归：事件背压/取消、热重载闭环、取消语义、触发并发上限。

覆盖六组：
A. core.events.cancel_plugin_tasks 取消在途任务（P0-2a）
B. 会话维度有界队列背压 + 同会话保序/丢最旧 + 不同会话并行 + worker 空闲退出（P0-2b/2c）
C. utils.plugin_loader._call_stop 超时 cancel、reload 拒绝（P0-3）
D. plugins/deepseek/Core/asyncutil 自身取消重抛、内部任务失败按原语义返回（P1-7）
E. plugins/deepseek/Core/triggers 并发上限与 stop 取消（P2-11）
F. main.py 关停顺序 / GUI 线程 daemon（P2-10，源码级断言）

无 pytest-asyncio：每个用例手工 new_event_loop/run_until_complete/close。
从仓库根运行：python -m pytest tests/test_audit_lifecycle_2026_11_26.py
"""
import asyncio
import importlib.util
import inspect
import os
import sys

import pytest

# 仓库根（python -m pytest 从根跑时已在 sys.path，这里显式兜底）
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
# deepseek 插件内模块按 Core.xxx 导入
_DS = os.path.join(_REPO, 'plugins', 'deepseek')
if _DS not in sys.path:
    sys.path.insert(0, _DS)

from core import events  # noqa: E402
import utils.plugin_loader as plugin_loader  # noqa: E402
from utils.plugin_loader import PluginManager  # noqa: E402

_ORIG_QUEUE_MAXSIZE = events.SESSION_QUEUE_MAXSIZE
_ORIG_IDLE_TIMEOUT = events.SESSION_WORKER_IDLE_TIMEOUT


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _wait_until(predicate, timeout=1.0, interval=0.005):
    """轮询等待条件成立（避免固定 sleep 让用例变慢/变脆）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError('等待条件超时')
        await asyncio.sleep(interval)


class _FakeEvent:
    """事件对象最小替身：_enqueue_handler 只读 group_id/user_id/post_type。"""

    def __init__(self, session, marker):
        self.group_id = session
        self.user_id = None
        self.post_type = marker
        self._plugin_name = ''


@pytest.fixture(autouse=True)
def _reset_events_module():
    """隔离模块级登记表与可 monkeypatch 的常量，避免用例间串味。"""
    events.plugin_tasks.clear()
    events._session_workers.clear()
    yield
    events.plugin_tasks.clear()
    events._session_workers.clear()
    events.SESSION_QUEUE_MAXSIZE = _ORIG_QUEUE_MAXSIZE
    events.SESSION_WORKER_IDLE_TIMEOUT = _ORIG_IDLE_TIMEOUT


# ── A. cancel_plugin_tasks ────────────────────────────────────────

def test_cancel_plugin_tasks_cancels_all_and_counts():
    """全部未完成任务被 cancel、返回数量正确，已失败任务不影响其它任务。"""

    async def case():
        hang1 = asyncio.ensure_future(asyncio.sleep(30))
        hang2 = asyncio.ensure_future(asyncio.sleep(30))

        async def boom():
            raise RuntimeError('boom')

        failed = asyncio.ensure_future(boom())
        events.plugin_tasks.update({hang1, hang2, failed})
        await asyncio.sleep(0)                 # 让 failed 先结束
        failed.exception()                     # 取回，避免 "never retrieved" 噪音

        n = await events.cancel_plugin_tasks(timeout=1.0)
        assert n == 2
        assert hang1.cancelled() and hang2.cancelled()
        assert failed.done() and not failed.cancelled()
        assert not events.plugin_tasks

    _run(case())


def test_cancel_plugin_tasks_cancels_session_workers():
    """worker 登记项也在取消范围内，且登记表被清空（P0-2b）。"""

    async def case():
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def handler(ev):
            entered.set()
            await gate.wait()

        events._enqueue_handler(handler, _FakeEvent('gW', 'm1'), 'p')
        await entered.wait()
        n = await events.cancel_plugin_tasks(timeout=1.0)
        assert n == 1
        assert events._session_workers == {}
        assert events.plugin_tasks == set()
        gate.set()                             # 释放被取消 handler 的等待

    _run(case())


# ── B. 会话队列背压 / 并行 / 空闲退出 ─────────────────────────────

def test_session_queue_drops_oldest_and_keeps_order(monkeypatch, capsys):
    """同会话投递 > maxsize 时丢最旧，处理数有上限且保持顺序（P0-2b）。"""
    monkeypatch.setattr(events, 'SESSION_QUEUE_MAXSIZE', 3)

    async def case():
        seen = []
        started = asyncio.Event()
        gate = asyncio.Event()

        async def handler(ev):
            seen.append(ev.post_type)
            if ev.post_type == 'msg-0':        # 首条占住 worker，后续事件只能排队
                started.set()
                await gate.wait()

        try:
            events._enqueue_handler(handler, _FakeEvent('g1', 'msg-0'), 'p')
            await started.wait()
            for i in range(1, 10):
                events._enqueue_handler(
                    handler, _FakeEvent('g1', f'msg-{i}'), 'p')

            key = ('p', 'g1')
            assert key in events._session_workers
            queue = events._session_workers[key][0]
            assert queue.qsize() == 3          # 有界，不会无限堆积

            gate.set()
            await _wait_until(lambda: len(seen) == 4)
            # 首条在跑，队列最后保留 msg-7/8/9（最旧 msg-1..6 被丢）
            assert seen == ['msg-0', 'msg-7', 'msg-8', 'msg-9']
        finally:
            gate.set()
            await events.cancel_plugin_tasks(timeout=1.0)

    _run(case())
    out = capsys.readouterr().out
    assert '背压' in out and '队列已满' in out


def test_different_sessions_run_in_parallel():
    """不同会话各自并发：A 阻塞时 B 已开始处理，不退化成一个全局队列。"""

    async def case():
        gate = asyncio.Event()
        a_started = asyncio.Event()
        b_started = asyncio.Event()
        order = []

        async def handler(ev):
            if ev.group_id == 'gA':
                order.append('A-start')
                a_started.set()
                await gate.wait()
                order.append('A-end')
            else:
                order.append('B-start')
                b_started.set()

        try:
            events._enqueue_handler(handler, _FakeEvent('gA', 'a'), 'p')
            await a_started.wait()
            events._enqueue_handler(handler, _FakeEvent('gB', 'b'), 'p')
            await asyncio.wait_for(b_started.wait(), timeout=1.0)
            assert 'A-end' not in order        # A 仍阻塞，B 已并行开始
            gate.set()
            await _wait_until(lambda: 'A-end' in order)
        finally:
            gate.set()
            await events.cancel_plugin_tasks(timeout=1.0)

    _run(case())


def test_handler_cancelled_error_does_not_kill_worker():
    """单个 handler 冒出的 CancelledError 不能拖垮会话队列（P0-2c 兼容）。"""

    async def case():
        seen = []

        async def handler(ev):
            if ev.post_type == 'bad':
                raise asyncio.CancelledError()
            seen.append(ev.post_type)

        try:
            events._enqueue_handler(handler, _FakeEvent('gCE', 'bad'), 'p')
            events._enqueue_handler(handler, _FakeEvent('gCE', 'good'), 'p')
            await _wait_until(lambda: 'good' in seen)
            assert ('p', 'gCE') in events._session_workers   # worker 仍存活
        finally:
            await events.cancel_plugin_tasks(timeout=1.0)

    _run(case())


def test_session_worker_exits_when_idle(monkeypatch):
    """worker 空闲超过阈值自行退出并清理登记项（P0-2b）。"""
    monkeypatch.setattr(events, 'SESSION_WORKER_IDLE_TIMEOUT', 0.05)

    async def case():
        ran = []

        async def handler(ev):
            ran.append(ev.post_type)

        try:
            events._enqueue_handler(handler, _FakeEvent('gIdle', 'x'), 'p')
            await _wait_until(lambda: ran == ['x'])
            assert ('p', 'gIdle') in events._session_workers
            # 空闲 0.05s 后 worker 自行退出并 pop 登记项
            await _wait_until(
                lambda: ('p', 'gIdle') not in events._session_workers,
                timeout=1.0)
            assert not events.plugin_tasks
        finally:
            await events.cancel_plugin_tasks(timeout=1.0)

    _run(case())


# ── C. plugin_loader：on_stop 超时必须 cancel，重载必须拒绝 ────────

class _TimeoutFuture:
    """concurrent.futures.Future 替身：result 超时抛 TimeoutError。"""

    def __init__(self):
        self.cancelled_calls = 0

    def result(self, timeout=None):
        raise TimeoutError('等待 on_stop 超时')

    def cancel(self):
        self.cancelled_calls += 1
        return True


def test_wait_stop_future_timeout_cancels_future():
    fut = _TimeoutFuture()
    ok = plugin_loader._wait_stop_future(fut, 0.01)
    assert ok is False
    assert fut.cancelled_calls == 1        # 旧 on_stop 协程被真正取消


def test_wait_stop_future_exception_is_unclean():
    class _BoomFuture:
        def result(self, timeout=None):
            raise RuntimeError('on_stop 抛异常')

        def cancel(self):
            return True

    assert plugin_loader._wait_stop_future(_BoomFuture(), 0.01) is False


def test_call_stop_in_loop_schedules_and_reports_unclean():
    """循环线程内不能阻塞：异步调度 on_stop 并返回 False（判定未完成）。"""

    async def case():
        class Inst:
            async def on_stop(self):
                await asyncio.sleep(30)

        before = set(plugin_loader._PENDING_STOPS)
        assert plugin_loader._call_stop(Inst(), timeout=0.2) is False
        scheduled = plugin_loader._PENDING_STOPS - before
        assert len(scheduled) == 1
        for task in scheduled:
            task.cancel()
        await asyncio.gather(*scheduled, return_exceptions=True)
        await asyncio.sleep(0)             # 让 done 回调清理强引用
        assert not (plugin_loader._PENDING_STOPS - before)

    _run(case())


def test_unload_plugin_refused_when_stop_unclean(monkeypatch):
    mgr = PluginManager(None)
    inst = object()
    mgr.plugins['p'] = inst
    monkeypatch.setattr(
        plugin_loader, '_call_stop', lambda inst, timeout=10.0: False)

    assert mgr.unload_plugin('p') is False
    assert 'p' in mgr.plugins              # 旧实例保留，避免新旧并存


def test_reload_plugin_refused_when_stop_unclean(monkeypatch):
    mgr = PluginManager(None)
    mgr.plugin_info['p'] = {'enabled': True, 'error': None}
    monkeypatch.setattr(mgr, 'unload_plugin', lambda name: False)
    loads = []

    def _load(*args, **kwargs):
        loads.append(args)

    monkeypatch.setattr(mgr, 'load_plugin_by_folder', _load)

    assert mgr.reload_plugin('p') is False
    assert loads == []                     # stop 未干净时禁止重新 import
    assert 'on_stop' in mgr.plugin_info['p']['error']


# ── D. 插件 asyncutil：区分自身取消与内部 task 失败 ───────────────

def _load_local_asyncutil(monkeypatch):
    """模拟旧框架（无 utils.asyncutil）加载插件本地实现。"""
    path = os.path.join(_DS, 'Core', 'asyncutil.py')
    monkeypatch.setitem(sys.modules, 'utils.asyncutil', None)
    spec = importlib.util.spec_from_file_location('_ds_asyncutil_local', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_local_asyncutil_self_cancel_propagates(monkeypatch):
    """自己所在任务被 cancel 时，CancelledError 必须继续向上抛（P1-7）。"""
    mod = _load_local_asyncutil(monkeypatch)

    async def case():
        inner = asyncio.ensure_future(asyncio.sleep(30))

        async def waiter():
            return await mod.wait_task_resilient(inner, timeout=5.0)

        outer = asyncio.create_task(waiter())
        await asyncio.sleep(0)             # 让 waiter 跑到等待边界
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        assert not inner.done()            # shield 不把取消转发给被等待 task
        inner.cancel()
        await asyncio.gather(inner, return_exceptions=True)

    _run(case())


def test_local_asyncutil_inner_failure_keeps_original_semantics(monkeypatch):
    """内部 task 失败/被取消时按原语义返回，不向调用方抛异常（P1-7）。"""
    mod = _load_local_asyncutil(monkeypatch)

    async def case():
        async def boom():
            raise ValueError('inner boom')

        failed = asyncio.ensure_future(boom())
        assert await mod.wait_task_resilient(failed, timeout=1.0) is True
        assert failed.done()

        victim = asyncio.ensure_future(asyncio.sleep(30))
        asyncio.get_running_loop().call_soon(victim.cancel)
        assert await mod.wait_task_resilient(victim, timeout=1.0) is True
        assert victim.cancelled()

    _run(case())


# ── E. triggers：并发上限 + stop 取消 ─────────────────────────────

def _make_trigger_manager():
    from Core import triggers as tr

    class _FakePlugin:
        cfg = {}
        db_path = ''
        engine = None

    logs = []
    mgr = tr.TriggerManager(
        _FakePlugin(), log=lambda msg, level='info': logs.append((level, msg)))
    return tr, mgr, logs


def test_trigger_spawn_skips_over_concurrency_limit():
    """超过并发上限的 spawn 直接跳过并记日志（P2-11）。"""
    tr, mgr, logs = _make_trigger_manager()
    old_limit = tr.MAX_CONCURRENT_FIRES
    tr.MAX_CONCURRENT_FIRES = 2
    try:
        async def case():
            started = []

            async def fake_fire(gid, text, source=''):
                started.append(gid)
                await asyncio.sleep(30)

            mgr.fire = fake_fire
            try:
                mgr._spawn('1', 'a', 's1')
                mgr._spawn('2', 'b', 's2')
                mgr._spawn('3', 'c', 's3')
                await asyncio.sleep(0.01)
                assert len(mgr._tasks) == 2
                assert '3' not in started      # 超限被跳过，没有起 task
                assert any('并发已达上限' in msg for _lv, msg in logs)
            finally:
                snapshot = list(mgr._tasks)
                mgr.stop()
                await _wait_until(lambda: not tr._REAPERS, timeout=1.0)
                assert all(t.done() for t in snapshot)
                assert all(t.cancelled() for t in snapshot)

        _run(case())
    finally:
        tr.MAX_CONCURRENT_FIRES = old_limit


def test_trigger_stop_cancels_pending_fires():
    """stop() 取消全部在途触发任务并 gather 回收（P2-11）。"""
    tr, mgr, _logs = _make_trigger_manager()

    async def case():
        async def fake_fire(gid, text, source=''):
            await asyncio.sleep(30)

        mgr.fire = fake_fire
        try:
            mgr._spawn('1', 'a', 's')
            mgr._spawn('2', 'b', 's')
            await asyncio.sleep(0.01)
            snapshot = list(mgr._tasks)
            assert len(snapshot) == 2
            mgr.stop()
            await _wait_until(
                lambda: all(t.done() for t in snapshot), timeout=1.0)
            await _wait_until(lambda: not tr._REAPERS, timeout=1.0)
            assert all(t.cancelled() for t in snapshot)
        finally:
            await _wait_until(lambda: not tr._REAPERS, timeout=1.0)

    _run(case())


# ── F. main.py：关停顺序与 GUI 线程 daemon ────────────────────────

def test_main_teardown_cancels_events_before_plugin_shutdown():
    """关停顺序：先取消入站任务，再 ashutdown_all_plugins（P0-2d）。"""
    import main

    src = inspect.getsource(main._teardown_connection)
    assert 'cancel_plugin_tasks' in src
    assert src.index('cancel_plugin_tasks') < src.index('ashutdown_all_plugins')


def test_main_gui_thread_is_daemon():
    """GUI 模式下 asyncio 子线程必须 daemon=True（P2-10）。"""
    import main

    src = inspect.getsource(main)
    # GUI 分支的 asyncio.run(...) 线程调用了 shutdown=None 且 daemon=True
    assert 'start(no_gui=False, shutdown=None)), daemon=True).start()' in src
