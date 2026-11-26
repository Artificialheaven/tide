"""2026-11-26 内存/性能审计 P0-5、P2-5、P2-6 的回归测试。

覆盖：
1. LiveBus ring 的字节预算（P2-5）：大 payload 时按总字节从最旧驱逐、
   oldest/latest 游标正确、至少保留最近 1 条、CJK 按 UTF-8 字节计；
2. live.py 的 SSE 资源回收（P0-5）：subscribe 与 header 写入在同一个 try
   内、socket 写超时、finally 兜底退订；`arguments` 字段纳入截断；
3. LiveManager.start() 重入守卫（P0-5）：重复启动不销毁已有 server，
   绑定失败也不误伤已有实例；
4. debug.html（P2-6）：state.events 与 DOM 一样按 3000 条裁剪，且裁剪
   最旧时 minSeq 同步到剩余最早 seq（「加载更早」游标不漏页）。

JS 无法在 pytest 里直接跑，P2-6 用源码片段断言防回归；断言只依赖稳定
的结构性片段与常量，不依赖局部变量名细节。
"""
import inspect
import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, 'plugins', 'deepseek'))

from Core import live as live_mod  # noqa: E402
from Core import live_bus  # noqa: E402
from Core.live import LiveManager, LiveServer  # noqa: E402
from Core.live_bus import LiveBus, Subscription  # noqa: E402

_DEBUG_HTML = os.path.join(_ROOT, 'plugins', 'deepseek', 'templates',
                           'live', 'debug.html')


class _FakePlugin:
    def __init__(self, cfg=None):
        self.cfg = dict(cfg or {})
        self.logs = []

    def log(self, msg, level='info'):
        self.logs.append((level, str(msg)))


class _FakeLiveServer:
    """替身 server：只实现 LiveManager.start()/stop() 用到的那点接口。"""

    def __init__(self, port=18095, alive=True):
        self._port = port
        self._alive = alive
        self.stopped = 0
        self._thread = types.SimpleNamespace(
            is_alive=lambda: self._alive)

    def port(self):
        return self._port

    def start(self):
        pass

    def stop(self):
        self.stopped += 1


def _all_events(bus: LiveBus) -> list:
    return bus.older(0, 500, Subscription(set(), set(), False))


# ── 1. LiveBus ring 字节预算（P2-5）────────────────────────────

def test_bus_ring_byte_budget_evicts_oldest(monkeypatch):
    monkeypatch.setattr(live_bus, '_RING_MAX_BYTES', 10_000)
    bus = LiveBus()
    published = [bus.publish('chat', group_id='g', text='x' * 2000)
                 for _ in range(20)]

    evs = _all_events(bus)
    seqs = [e['seq'] for e in evs]
    # latest_seq 不受驱逐影响；保留的是「最新的连续一段」
    assert bus.latest_seq() == 20
    assert seqs and seqs == list(range(seqs[0], 21))
    assert bus.oldest_seq() == seqs[0]
    assert bus.ring_len() == len(evs)
    assert 1 <= len(evs) < 20

    # 内存估算与实际保留事件一致，且不超预算
    sizes = [LiveBus._event_size(e) for e in evs]
    assert bus.ring_bytes() == sum(sizes)
    assert bus.ring_bytes() <= 10_000
    # 紧致性：再多留一条最旧的被驱逐事件就会超预算
    evicted = published[seqs[0] - 2]      # seq = seqs[0] - 1
    assert bus.ring_bytes() + LiveBus._event_size(evicted) > 10_000


def test_bus_ring_keeps_latest_even_if_over_budget(monkeypatch):
    monkeypatch.setattr(live_bus, '_RING_MAX_BYTES', 1000)
    bus = LiveBus()
    ev = bus.publish('chat', group_id='g', text='y' * 5000)
    # 单事件本身超预算：仍保留最近 1 条，不把唯一事件丢掉
    assert bus.ring_len() == 1
    assert bus.oldest_seq() == bus.latest_seq() == 1
    assert bus.ring_bytes() == LiveBus._event_size(ev)
    assert bus.ring_bytes() > 1000

    bus.publish('chat', group_id='g', text='z' * 5000)
    assert bus.ring_len() == 1
    assert bus.oldest_seq() == bus.latest_seq() == 2
    assert _all_events(bus)[0]['text'].startswith('z')


def test_bus_ring_budget_counts_utf8_bytes(monkeypatch):
    # 1000 个 CJK 字符 ≈ 3000 字节：按字节只能留 1 条，按字符会留 4 条
    monkeypatch.setattr(live_bus, '_RING_MAX_BYTES', 5_000)
    bus = LiveBus()
    for _ in range(5):
        bus.publish('chat', group_id='g', text='中' * 1000)
    assert bus.ring_len() == 1
    assert bus.ring_bytes() > 3_000


def test_bus_ring_count_cap_still_enforced(monkeypatch):
    monkeypatch.setattr(live_bus, '_RING_MAX', 5)
    bus = LiveBus()
    for i in range(12):
        bus.publish('chat', group_id='g', text=str(i))
    assert bus.ring_len() == 5
    assert bus.oldest_seq() == 8
    assert bus.latest_seq() == 12


# ── 2. 事件字段截断（P2-5）与 SSE 资源回收（P0-5）──────────────

def test_emit_clips_arguments_and_result_fields():
    assert 'arguments' in live_mod._CLIP
    mgr = LiveManager(types.SimpleNamespace(cfg={}),
                      log=lambda *a, **k: None)
    mgr.emit('tool_result', group_id='777', name='read_file',
             arguments={'path': 'a.txt', 'content': 'x' * 5000},
             result='r' * 3000)
    ev = _all_events(mgr.bus)[-1]
    # 大 dict 参数序列化后截断，单事件不会撑到几十 KB
    assert isinstance(ev['arguments'], str)
    assert len(ev['arguments']) <= 2100
    assert '…(+' in ev['arguments']
    assert len(ev['result']) <= 1100

    # 小参数保持 dict：调试台仍按结构渲染
    mgr.emit('tool_result', group_id='777', arguments={'a': 1})
    ev2 = _all_events(mgr.bus)[-1]
    assert ev2['arguments'] == {'a': 1}


def _sse_source() -> str:
    src = inspect.getsource(LiveServer)
    start = src.index('def _sse')
    end = src.index('def _write_event', start)
    return src[start:end]


def test_sse_subscribe_and_headers_inside_try():
    sse = _sse_source()
    assert 'mgr.bus.subscribe(' in sse
    assert 'self.connection.settimeout(' in sse
    assert 'socket.timeout' in sse
    # 订阅与 header 写入都在 try 内：header 阶段断开也会走 finally 退订
    assert sse.index('try:') < sse.index('mgr.bus.subscribe(')
    assert sse.index('mgr.bus.subscribe(') < sse.index(
        'self.send_response(200)')
    # sub 先置 None，subscribe 本身抛错时 finally 不会 NameError
    assert 'sub = None' in sse


def test_sse_finally_always_unsubscribes():
    sse = _sse_source()
    assert 'finally:' in sse
    finally_tail = sse[sse.rindex('finally:'):]
    assert 'mgr.bus.unsubscribe(sub)' in finally_tail
    # 写循环的异常面覆盖断开/超时
    assert 'BrokenPipeError' in sse
    assert 'OSError' in sse


# ── 3. LiveManager.start() 重入守卫（P0-5）─────────────────────

def _start_cfg() -> dict:
    return {'live_on': True, 'trigger_token': 't',
            'live_host': '127.0.0.1', 'live_port': 18095}


def test_start_twice_keeps_existing_server(monkeypatch):
    plugin = _FakePlugin(cfg=_start_cfg())
    mgr = LiveManager(plugin, log=plugin.log)
    fake = _FakeLiveServer()
    mgr._server = fake
    constructed = []
    monkeypatch.setattr(live_mod, 'LiveServer',
                        lambda *a, **k: constructed.append(1))

    mgr.start()

    # 二次启动直接返回：不新建、不停旧、不把 _server 置 None
    assert mgr._server is fake
    assert constructed == []
    assert fake.stopped == 0
    assert any('已在运行' in msg for _, msg in plugin.logs)


def test_start_after_dead_thread_replaces_server(monkeypatch):
    plugin = _FakePlugin(cfg=_start_cfg())
    mgr = LiveManager(plugin, log=plugin.log)
    dead = _FakeLiveServer(port=1, alive=False)
    fresh = _FakeLiveServer(port=2)
    mgr._server = dead
    monkeypatch.setattr(live_mod, 'LiveServer', lambda *a, **k: fresh)

    mgr.start()

    assert dead.stopped == 1
    assert mgr._server is fresh


def test_start_bind_failure_keeps_state_clean(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError('address already in use')

    monkeypatch.setattr(live_mod, 'LiveServer', boom)
    plugin = _FakePlugin(cfg=_start_cfg())
    mgr = LiveManager(plugin, log=plugin.log)

    mgr.start()

    assert mgr._server is None
    assert any('启动失败' in msg for _, msg in plugin.logs)


# ── 4. debug.html 事件数组上限（P2-6）──────────────────────────

def _html() -> str:
    with open(_DEBUG_HTML, encoding='utf-8') as f:
        return f.read()


def _js_function(html: str, name: str) -> str:
    start = html.index(f'function {name}(')
    end = html.index('\n}', start)
    return html[start:end + 2]


def test_debug_html_events_array_capped():
    html = _html()
    assert 'const EVENTS_MAX = 3000' in html
    trim = _js_function(html, 'trimEvents')
    assert 'slice(-EVENTS_MAX)' in trim
    add = _js_function(html, 'addEvent')
    assert 'trimEvents()' in add
    # 追加历史事件后也必须裁剪，否则「加载更早」会无限增长
    concat = html.index('state.events = evs.concat(state.events)')
    assert 'trimEvents()' in html[concat:concat + 300]


def test_debug_html_trim_syncs_minseq_cursor():
    trim = _js_function(_html(), 'trimEvents')
    # minSeq 是「加载更早」的 before 游标：裁剪最旧后必须指向剩余最早 seq
    assert 'minSeq' in trim
    assert 'state.events.find(' in trim
    # 时间线 DOM 已有上限逻辑，仍在（防回归）
    assert 'feed.insertBefore' in _html()
