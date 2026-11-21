"""无头模式信号桥：signal.signal（主线程）→ threading.Event → 轮询 task → asyncio.Event。

Windows 的 ProactorEventLoop 不支持 ``loop.add_signal_handler``；signal 回调在主线程
执行且只允许做极简操作，因此用 threading.Event 中转，再由事件循环里的桥接 task 轮询
（范式同 scripts/bili_live_ai.py）。第一次信号请求优雅关停，第二次信号直接强退。
"""
from __future__ import annotations

import asyncio
import os
import signal
import threading


class ShutdownController:
    def __init__(self, exiter=os._exit, poll_interval: float = 0.2):
        self._thread_event = threading.Event()   # 信号回调（主线程）写
        self._async_event: asyncio.Event | None = None  # 桥接 task（循环线程）置
        self._bridge_task: asyncio.Task | None = None
        self._old_handlers: dict = {}
        self._exiter = exiter            # 默认 os._exit；测试可注入 fake
        self._poll = poll_interval
        self.signalled = False

    # ── 主线程，asyncio.run 之前调用 ──────────────────────────────

    def install(self) -> None:
        for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                self._old_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):
                # 非主线程或平台不支持：跳过该信号
                pass

    def _on_signal(self, signum, frame):
        # 第二次信号：放弃收尾，立即强退
        if self._thread_event.is_set():
            self._exiter(1)
        self._thread_event.set()

    def restore(self) -> None:
        for sig, handler in self._old_handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        self._old_handlers.clear()

    # ── 事件循环线程内调用 ───────────────────────────────────────

    async def attach(self) -> None:
        if self._async_event is not None:
            return
        self._async_event = asyncio.Event()
        self._bridge_task = asyncio.create_task(self._bridge())

    async def _bridge(self) -> None:
        while not self._thread_event.is_set():
            await asyncio.sleep(self._poll)
        self.signalled = True
        self._async_event.set()

    @property
    def event(self) -> asyncio.Event:
        if self._async_event is None:
            raise RuntimeError('ShutdownController.attach() 尚未执行')
        return self._async_event

    async def wait_interruptible(self, delay: float) -> bool:
        """退避等待：信号先到返回 True（未睡满），睡满返回 False。"""
        try:
            await asyncio.wait_for(self.event.wait(), timeout=delay)
            return True
        except asyncio.TimeoutError:
            return False

    async def park(self) -> None:
        """放弃重连后停车，直到信号到达（进程留存、Web 面板等线程继续服务）。"""
        await self.event.wait()

    async def detach(self) -> None:
        if self._bridge_task is not None:
            self._bridge_task.cancel()
            await asyncio.gather(self._bridge_task, return_exceptions=True)
            self._bridge_task = None
