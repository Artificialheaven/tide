"""2026-11-26 性能审计：LLM 客户端缓存的跨线程安全（P1-4）回归测试。

原问题：`_clients` 会被主循环线程与 Flask/SocketIO 线程同时读写；
`_schedule_close` 无条件 `loop.create_task`，跨线程调用抛 RuntimeError 被
静默吞掉 → 旧 AsyncOpenAI 永不关闭、aiohttp 会话泄漏；`aclose` 遍历时被
另一线程 pop 会 RuntimeError: dictionary changed size during iteration。
"""
import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'plugins', 'deepseek'))

from Core import llm as llm_mod  # noqa: E402


class _FakeClient:
    def __init__(self, name='fake'):
        self.name = name
        self.closed = 0

    async def close(self):
        self.closed += 1


def _client() -> llm_mod.LLMClient:
    c = llm_mod.LLMClient()
    c.log = None
    return c


def test_prune_clients_concurrent_with_aclose_does_not_raise():
    """多线程 prune + aclose 并发不应抛 dictionary changed size。"""
    client = _client()
    for i in range(50):
        client._clients[f'p{i}'] = _FakeClient(f'c{i}')
        client._sigs[f'p{i}'] = ('k', 'b')
    errors = []

    def pruner():
        try:
            for _ in range(200):
                client.prune_clients([f'p{i}' for i in range(25)])
        except Exception as e:      # noqa: BLE001
            errors.append(e)

    def reader():
        try:
            for _ in range(200):
                with client._lock:
                    list(client._clients.values())
        except Exception as e:      # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=pruner), threading.Thread(target=reader)]
    t0 = time.time()
    for t in ts:
        t.start()
    # aclose 在主线程跑（不 await 完成也行，这里给个 loop）
    try:
        asyncio.run(client.aclose())
    finally:
        for t in ts:
            t.join(timeout=5)
    assert not errors, errors
    assert time.time() - t0 < 10


def test_prune_clients_removes_and_closes():
    client = _client()
    fake = _FakeClient()
    client._clients['gone'] = fake
    client._sigs['gone'] = ('k', 'b')
    closed = []
    client._schedule_close = lambda c: closed.append(c)
    client.prune_clients(['kept'])
    assert 'gone' not in client._clients
    assert 'gone' not in client._sigs
    assert closed == [fake]


def test_schedule_close_from_worker_thread_uses_threadsafe():
    """跨线程（无运行 loop）时应通过事件循环线程安全调度，而不是静默失败。"""
    client = _client()
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    th = threading.Thread(target=run_loop, daemon=True)
    th.start()
    ready.wait(2)
    try:
        from core import globals as core_globals
        core_globals.event_loop = loop
        fake = _FakeClient()
        # 当前线程没有 running loop → 应走 run_coroutine_threadsafe
        client._schedule_close(fake)
        deadline = time.time() + 3
        while fake.closed == 0 and time.time() < deadline:
            time.sleep(0.02)
        assert fake.closed == 1
    finally:
        core_globals.event_loop = None
        loop.call_soon_threadsafe(loop.stop)
        th.join(timeout=3)


def test_schedule_close_noop_without_loop():
    """没有任何 loop 时不应抛异常（GC 兜底即可）。"""
    client = _client()
    from core import globals as core_globals
    old, core_globals.event_loop = getattr(core_globals, 'event_loop', None), None
    try:
        client._schedule_close(_FakeClient())
    finally:
        core_globals.event_loop = old


def test_get_client_caches_by_signature(monkeypatch):
    """同一 provider+签名复用同一 client；签名变化才重建。"""
    client = _client()
    made = []

    class _FakeAsyncOpenAI:
        def __init__(self, **kw):
            self.kw = kw
            made.append(self)

    import types
    fake_openai = types.SimpleNamespace(AsyncOpenAI=_FakeAsyncOpenAI)
    monkeypatch.setitem(sys.modules, 'openai', fake_openai)
    provider = {'id': 'deepseek', 'api_key': 'k1', 'base_url': 'https://x'}
    c1 = client._get_client(provider)
    c2 = client._get_client(provider)
    assert c1 is c2 and len(made) == 1
    c3 = client._get_client({**provider, 'api_key': 'k2'})
    assert c3 is not c1 and len(made) == 2
