# -*- coding: utf-8 -*-
"""2026-11-26 审计杂项修复的回归测试（不依赖网络/真实模型）。

覆盖条目：
- 官方通道事件队列上限（超限丢最旧）与失败标记上限（state.GroupCache/MemberCache）
- P2-12 core/history.py 总字节预算淘汰
- P2-13 同步桥 / Web 桥超时后取消底层 future
- P1-6 审批中心等待改为线程池读库（不阻塞事件循环）且超时 fallback 语义不变
- P2-7 视频宫格缓存总字节上限
- P2-9 插件蓝图热重载后重新注册（新模块函数生效）
"""
import asyncio
import contextlib
import json
import os
import sys
import threading
import time
import types
import uuid

import flask
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, 'plugins', 'deepseek'))

import ui.web.server as web_admin  # noqa: E402
from Core import approval as approval_mod  # noqa: E402
from Core import video_read as video_mod  # noqa: E402
from Core import web as deepseek_web  # noqa: E402
from core import globals as core_globals  # noqa: E402
from core import history as history_mod  # noqa: E402
from core import sync_bridge  # noqa: E402
from core.official import config as official_config_mod  # noqa: E402
from core.official import session as official_session_mod  # noqa: E402
from core.official import state as official_state_mod  # noqa: E402


# ── 官方通道：事件队列 / 失败标记 ──────────────────────────────

def _make_official_session(logs: list):
    cfg = official_config_mod.OfficialConfig(app_id='10000', app_secret='s')
    api = types.SimpleNamespace(compact_json=json.dumps)
    return official_session_mod.OfficialSession(cfg, api, logger=logs.append)


def test_official_out_queue_drops_oldest(monkeypatch):
    """队列满时丢最旧事件并记 warning，消费端拿到的是最新事件。"""
    monkeypatch.setattr(official_session_mod, '_QUEUE_MAX', 2)
    logs: list = []
    session = _make_official_session(logs)
    assert session.out_queue.maxsize == 2
    for seq in range(5):
        session._emit({'seq': seq})
    assert session.out_queue.qsize() == 2
    got = [json.loads(session.out_queue.get_nowait()) for _ in range(2)]
    assert got == [{'seq': 3}, {'seq': 4}]
    assert any('丢弃最旧事件' in msg for msg in logs)


def test_official_group_cache_failed_bounded(monkeypatch):
    """失败标记超上限时按写入顺序淘汰，不会随运行时间无限增长。"""
    clock = {'now': 1000.0}
    monkeypatch.setattr(official_state_mod, '_now', lambda: clock['now'])
    cache = official_state_mod.GroupCache(ttl=600, failed_retry=300,
                                          failed_capacity=8)
    for i in range(50):
        cache.mark_failed(f'g{i}')
    assert len(cache._failed) <= 8
    assert cache._failed and f'g49' in cache._failed

    # 过期标记在惰性清扫时被删除，needs_refresh 重新返回 True
    clock['now'] += 1000
    cache._prune_failed()
    assert not cache._failed
    assert cache.needs_refresh('g49') is True


def test_official_member_cache_failed_bounded(monkeypatch):
    clock = {'now': 2000.0}
    monkeypatch.setattr(official_state_mod, '_now', lambda: clock['now'])
    cache = official_state_mod.MemberCache(ttl=600, failed_retry=300,
                                           failed_capacity=8)
    for i in range(50):
        cache.mark_failed('g', f'u{i}')
    assert len(cache._failed) <= 8

    clock['now'] += 1000
    cache._prune_failed()
    assert not cache._failed


# ── P2-12：消息历史字节预算 ────────────────────────────────────

def _make_message_event(message_id, text, group_id='1', user_id='2'):
    return types.SimpleNamespace(
        message_type='group', group_id=group_id, user_id=user_id,
        message_id=message_id, raw_message=text, time=0, sender=None)


def test_history_byte_budget_evicts_oldest(monkeypatch):
    """条数远未到上限时，按总字节预算淘汰最旧会话的最旧消息。"""
    monkeypatch.setattr(history_mod, '_MAX_BYTES', 4000)
    history_mod.clear()
    try:
        # 每条 = 1000 字节文本 + 160 估算开销 = 1160 字节
        for i in range(10):
            history_mod.record(_make_message_event(
                1000 + i, 'a' * 1000, group_id=str(i % 2)))
        assert history_mod._total_bytes <= 4000
        # 最新消息仍可读，最旧已被淘汰
        assert history_mod.get_by_id(1009) == 'a' * 1000
        assert history_mod.get_by_id(1000) == ''
        assert history_mod.get_by_offset('g', '1', 0) == 'a' * 1000
    finally:
        history_mod.clear()


def test_history_duplicate_message_id_counts_bytes_once(monkeypatch):
    """同一 message_id 重复记录时，旧索引引用要释放，不能重复计费。"""
    monkeypatch.setattr(history_mod, '_MAX_BYTES', 64 * 1024 * 1024)
    history_mod.clear()
    try:
        history_mod.record(_make_message_event(1, 'x' * 100))
        first = history_mod._total_bytes
        history_mod.record(_make_message_event(1, 'y' * 300))
        second = history_mod._total_bytes
        # 旧 entry 从桶里被淘汰（同会话继续写）后总字节不应累加两份
        assert second <= first + 300 + history_mod._ENTRY_OVERHEAD
        assert history_mod.get_by_id(1) == 'y' * 300
        history_mod.clear()
        assert history_mod._total_bytes == 0
    finally:
        history_mod.clear()


# ── P2-13：超时取消底层 future ─────────────────────────────────

class _TimeoutFuture:
    """模拟 concurrent.futures.Future：result 超时，done 为 False。"""

    def __init__(self):
        self.cancelled = False
        self.done_called = 0

    def result(self, timeout=None):
        raise TimeoutError('timed out')

    def done(self):
        self.done_called += 1
        return False

    def cancel(self):
        self.cancelled = True
        return True


def _install_fake_loop_bot(monkeypatch):
    loop = types.SimpleNamespace(is_running=lambda: True)
    bot = types.SimpleNamespace(
        call_api=lambda *a, **k: asyncio.sleep(0),
        send_message=lambda *a, **k: asyncio.sleep(0),
    )
    monkeypatch.setattr(core_globals, 'event_loop', loop)
    monkeypatch.setattr(core_globals, 'bot_instance', bot)
    return loop


def test_sync_bridge_timeout_cancels_future(monkeypatch):
    _install_fake_loop_bot(monkeypatch)
    futures: list = []

    def fake_run_coroutine_threadsafe(coro, loop):
        coro.close()          # 测试里不真正调度，避免未 await 警告
        fut = _TimeoutFuture()
        futures.append(fut)
        return fut

    monkeypatch.setattr(asyncio, 'run_coroutine_threadsafe',
                        fake_run_coroutine_threadsafe)
    with pytest.raises(TimeoutError):
        sync_bridge.call_api_sync('get_group_info')
    with pytest.raises(TimeoutError):
        sync_bridge.send_message_sync('hi', target_id='1')
    assert [f.cancelled for f in futures] == [True, True]


def test_web_run_async_timeout_cancels_future(monkeypatch):
    loop = types.SimpleNamespace(is_running=lambda: True)
    monkeypatch.setattr(core_globals, 'event_loop', loop)
    fut = _TimeoutFuture()

    def fake_run_coroutine_threadsafe(coro, l):
        coro.close()
        return fut

    monkeypatch.setattr(asyncio, 'run_coroutine_threadsafe',
                        fake_run_coroutine_threadsafe)

    async def _slow():
        return 'never'

    result, err = deepseek_web._run_async(_slow(), timeout=0.01)
    assert result is None
    assert err == ('操作超时', 500)
    assert fut.cancelled


def test_web_run_async_success_returns_value(monkeypatch):
    loop = types.SimpleNamespace(is_running=lambda: True)
    monkeypatch.setattr(core_globals, 'event_loop', loop)

    class _OkFuture:
        def result(self, timeout=None):
            return 'ok'

        def done(self):
            return True

        def cancel(self):
            raise AssertionError('成功路径不应取消')

    def fake_run_coroutine_threadsafe(coro, l):
        coro.close()
        return _OkFuture()

    monkeypatch.setattr(asyncio, 'run_coroutine_threadsafe',
                        fake_run_coroutine_threadsafe)

    async def _quick():
        return 'ok'

    result, err = deepseek_web._run_async(_quick())
    assert (result, err) == ('ok', None)


# ── P1-6：审批中心等待不阻塞事件循环 ───────────────────────────

class _FakeApprovalStorage:
    """最小可用的 storage 替身：记录调用线程、决策与读库耗时。"""

    def __init__(self, get_delay: float = 0.0):
        self.get_delay = get_delay
        self.threads: dict = {}
        self.decisions: list = []
        self.created: dict = {}
        self.status = 'pending'

    def create_approval(self, db_path, **kwargs):
        self.threads.setdefault('create', threading.get_ident())
        self.created = kwargs
        return {'id': 7, 'expires_at': 0}

    def get_approval(self, db_path, approval_id):
        self.threads.setdefault('get', threading.get_ident())
        if self.get_delay:
            time.sleep(self.get_delay)
        status = self.status
        if status == 'pending':
            return {'id': approval_id, 'status': 'pending', 'gid': '1',
                    'chat_type': 'group', 'turn_id': ''}
        return {'id': approval_id, 'status': status, 'gid': '1',
                'chat_type': 'group', 'turn_id': ''}

    def decide_approval(self, db_path, approval_id, status,
                        decided_by=''):
        self.threads.setdefault('decide', threading.get_ident())
        self.decisions.append((approval_id, status, decided_by))
        self.status = status
        return {'id': approval_id}


def _install_fake_approval_storage(monkeypatch, fake):
    for name in ('create_approval', 'get_approval', 'decide_approval'):
        monkeypatch.setattr(approval_mod.storage, name, getattr(fake, name))


def _make_approval_center():
    plugin = types.SimpleNamespace(
        cfg={'permissions': {'ask': {'fallback': 'deny'}}},
        bot=None, live=None)
    return approval_mod.ApprovalCenter('/tmp/audit-fake-approval.db', plugin)


def test_approval_timeout_falls_back_to_deny(monkeypatch):
    """超时仍按 fallback=deny 落 rejected/timeout，且读库在事件循环外执行。"""
    fake = _FakeApprovalStorage()
    _install_fake_approval_storage(monkeypatch, fake)
    center = _make_approval_center()
    loop_thread = threading.get_ident()
    approved = asyncio.run(center.request(
        gid='1', chat_type='group', tool_key='fs_write', args={},
        actor='9', ttl=0.05))
    assert approved is False
    assert fake.decisions == [(7, 'rejected', 'timeout')]
    # 读库/写库都走了 asyncio.to_thread，不会卡在事件循环线程上
    assert fake.threads['get'] != loop_thread
    assert fake.threads['decide'] != loop_thread


def test_approval_wait_does_not_block_event_loop(monkeypatch):
    """等待期间事件循环继续调度；正常批准经 future 立即唤醒。"""
    fake = _FakeApprovalStorage(get_delay=0.05)
    _install_fake_approval_storage(monkeypatch, fake)
    center = _make_approval_center()
    ticks = {'n': 0}

    async def main():
        async def ticker():
            while True:
                ticks['n'] += 1
                await asyncio.sleep(0.01)

        tick_task = asyncio.create_task(ticker())
        try:
            req_task = asyncio.create_task(center.request(
                gid='1', chat_type='group', tool_key='fs_write', args={},
                actor='9', ttl=5))
            await asyncio.sleep(0.12)
            assert not req_task.done()
            assert await center.decide(7, approved=True) is True
            assert await asyncio.wait_for(req_task, 2) is True
        finally:
            tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick_task

    asyncio.run(main())
    # 若同步读库阻塞了事件循环，ticker 在 0.12s 内根本跑不了几轮
    assert ticks['n'] >= 3


# ── P2-7：视频宫格缓存字节上限 ─────────────────────────────────

def _reset_video_cache(monkeypatch, *, max_count: int, max_bytes: int):
    monkeypatch.setattr(video_mod, '_CACHE_MAX', max_count)
    monkeypatch.setattr(video_mod, '_CACHE_BYTES_MAX', max_bytes)
    saved = (dict(video_mod._CACHE), video_mod._cache_bytes)
    video_mod._CACHE.clear()
    video_mod._cache_bytes = 0
    return saved


def _restore_video_cache(saved):
    cache, total = saved
    video_mod._CACHE.clear()
    video_mod._CACHE.update(cache)
    video_mod._cache_bytes = total


def test_video_cache_byte_budget_evicts_oldest(monkeypatch):
    saved = _reset_video_cache(monkeypatch, max_count=10, max_bytes=5000)
    try:
        # 每条 sheet 1000 字节 + 2048 估算开销 = 3048，两条必然超 5000
        video_mod._store_cache('k1', {'sheet': b'x' * 1000})
        video_mod._store_cache('k2', {'sheet': b'y' * 1000})
        assert 'k1' not in video_mod._CACHE
        assert 'k2' in video_mod._CACHE
        assert video_mod._cache_bytes <= 5000
        assert video_mod._cached('k2')['sheet'] == b'y' * 1000
        # 同 key 覆盖时旧条目字节先释放，不会重复计费
        video_mod._store_cache('k2', {'sheet': b'z' * 500})
        assert video_mod._cache_bytes == 500 + 2048
    finally:
        _restore_video_cache(saved)


def test_video_cache_count_limit_still_works(monkeypatch):
    saved = _reset_video_cache(monkeypatch, max_count=2, max_bytes=10 ** 9)
    try:
        for key in ('a', 'b', 'c'):
            video_mod._store_cache(key, {'sheet': b''})
        assert list(video_mod._CACHE) == ['b', 'c']
        # 单条超大但只剩它时保留（不把自己淘汰掉）
        video_mod._store_cache('huge', {'sheet': b'x' * (10 ** 6)})
        assert 'huge' in video_mod._CACHE
    finally:
        _restore_video_cache(saved)


# ── P2-9：插件蓝图热重载后重新注册 ─────────────────────────────

def test_plugin_blueprint_reregistered_after_hot_reload():
    """热重载后重注册：旧蓝图的路由/view/请求回调被摘掉，新模块函数生效。"""
    state = {'version': 'v1'}

    class _FakePlugin:
        def get_web_blueprint(self):
            bp = flask.Blueprint(f'auditfix_{uuid.uuid4().hex[:8]}', __name__)

            @bp.route('/ping')
            def ping():
                return state['version']

            return bp

    class _FakeManager:
        def __init__(self):
            self.plugins = {'auditfix': _FakePlugin()}

    manager = _FakeManager()
    saved_registered = dict(web_admin._registered_blueprints)
    saved_generation = web_admin._blueprint_generation
    try:
        web_admin.register_plugin_blueprints(manager)
        name1, gen1 = web_admin._registered_blueprints['auditfix']
        client = web_admin._app.test_client()
        with client.session_transaction() as sess:
            sess['authenticated'] = True
        assert client.get('/auditfix/ping').data == b'v1'

        # 模拟热重载：模块重导入后同一 folder 拿到新蓝图
        state['version'] = 'v2'
        web_admin.register_plugin_blueprints(manager)
        name2, gen2 = web_admin._registered_blueprints['auditfix']
        assert name2 != name1
        assert gen2 > gen1
        assert client.get('/auditfix/ping').data == b'v2'

        # 旧蓝图的路由/view 函数/蓝图注册全部被摘除
        old_prefix = name1 + '.'
        assert not [rule for rule in web_admin._app.url_map.iter_rules()
                    if str(rule.endpoint).startswith(old_prefix)]
        assert old_prefix + 'ping' not in web_admin._app.view_functions
        assert name1 not in web_admin._app.blueprints
        assert any(rule.endpoint == 'static'
                   for rule in web_admin._app.url_map.iter_rules())
        # 登录鉴权仍生效（未被重新注册破坏）
        assert web_admin._app.test_client().get(
            '/auditfix/ping').status_code == 401
    finally:
        entry = web_admin._registered_blueprints.pop('auditfix', None)
        if entry:
            web_admin._purge_blueprint(entry[0])
        web_admin._registered_blueprints.clear()
        web_admin._registered_blueprints.update(saved_registered)
        web_admin._blueprint_generation = saved_generation
