# -*- coding: utf-8 -*-
"""Core.asyncutil 旧框架兼容：没有 utils.asyncutil 时插件仍能加载与关停。"""
import asyncio
import importlib.util
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'plugins', 'deepseek'))

_MOD_PATH = os.path.join(REPO, 'plugins', 'deepseek', 'Core', 'asyncutil.py')


def _load_without_framework(monkeypatch):
    # 模拟旧框架：utils 存在但没有 asyncutil 子模块
    monkeypatch.setitem(sys.modules, 'utils.asyncutil', None)
    spec = importlib.util.spec_from_file_location(
        '_ds_asyncutil_fallback', _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_fallback_defines_all_primitives(monkeypatch):
    mod = _load_without_framework(monkeypatch)
    for name in ('wait_task_resilient', 'wait_resilient', 'cancel_task',
                 'drain_pending'):
        assert callable(getattr(mod, name)), name


def test_fallback_wait_and_cancel(monkeypatch):
    mod = _load_without_framework(monkeypatch)

    async def main():
        async def quick():
            return 7

        ok, task = await mod.wait_resilient(quick(), 1.0)
        assert ok and task.result() == 7

        started = asyncio.Event()

        async def spin():
            started.set()
            await asyncio.sleep(30)

        slow = asyncio.ensure_future(spin())
        await started.wait()
        await mod.cancel_task(slow, timeout=1.0)
        assert slow.done()
        await mod.cancel_task(None)
        await mod.drain_pending(0.2)

    _run(main())


def test_uses_framework_impl_when_available():
    from Core import asyncutil as ds_util
    from utils import asyncutil as fw_util
    # 新框架下插件直接复用框架实现（共享 _PENDING 回收）
    assert ds_util.cancel_task is fw_util.cancel_task
    assert ds_util.wait_resilient is fw_util.wait_resilient
