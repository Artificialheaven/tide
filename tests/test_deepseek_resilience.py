# -*- coding: utf-8 -*-
"""deepseek 工具调用容错：残缺 tool_calls 链修复、派发异常不中断对话、
取消时补齐工具结果、LLM 瞬时失败重试。

背景：assistant.tool_calls 落库后、tool 结果落库前若被异常/取消打断，
历史里会留下残缺调用链；OpenAI 兼容接口遇到这种历史每次请求都 400，
对外表现是「对话突然永远不回」。
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'plugins', 'deepseek'))

from Core import context, engine, llm  # noqa: E402


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _row(role, content='', **kw):
    r = {'id': kw.pop('id', 0), 'role': role, 'content': content,
         'tool_calls': '', 'tool_call_id': '', 'name': '',
         'created_at': 0, 'sender_id': '', 'sender_name': ''}
    r.update(kw)
    return r


def _tc_json(*ids):
    return json.dumps([{'id': i, 'type': 'function',
                        'function': {'name': f'fn_{i}', 'arguments': '{}'}}
                       for i in ids], ensure_ascii=False)


def _tool(cid, content='ok', id_=0):
    return _row('tool', content, id=id_, tool_call_id=cid)


# ── _repair_tool_chains ────────────────────────────────────────────

def test_repair_keeps_complete_batch():
    rows = [
        _row('user', 'hi'),
        _row('assistant', '', tool_calls=_tc_json('a', 'b')),
        _tool('a'),
        _tool('b'),
        _row('assistant', 'done'),
    ]
    out = context._repair_tool_chains(rows)
    assert out == rows


def test_repair_drops_incomplete_batch_mid_window():
    # 窗口一开始是完整前缀，但中段 assistant 只拿到一半工具结果
    rows = [
        _row('user', 'hi'),
        _row('assistant', 'earlier'),
        _row('assistant', '', tool_calls=_tc_json('a', 'b')),
        _tool('a'),
        _row('user', 'next'),
        _row('assistant', 'reply'),
    ]
    out = context._repair_tool_chains(rows)
    roles = [r['role'] for r in out]
    assert roles == ['user', 'assistant', 'user', 'assistant']
    assert all(r.get('tool_calls') in (None, '') for r in out)


def test_repair_drops_orphan_and_duplicate_tool_rows():
    rows = [
        _row('user', 'hi'),
        _tool('ghost'),                       # 无主 tool 行
        _row('assistant', '', tool_calls=_tc_json('a')),
        _tool('a'),
        _tool('a'),                           # 重复结果
        _tool('b'),                           # 批外 tool 行
        _row('assistant', 'done'),
    ]
    out = context._repair_tool_chains(rows)
    assert [r['role'] for r in out] == [
        'user', 'assistant', 'tool', 'assistant']
    assert out[2]['tool_call_id'] == 'a'


def test_repair_drops_trailing_incomplete_batch():
    rows = [
        _row('user', 'hi'),
        _row('assistant', '', tool_calls=_tc_json('a', 'b')),
        _tool('a'),
    ]
    out = context._repair_tool_chains(rows)
    assert [r['role'] for r in out] == ['user']


def test_plan_summary_window_rows_still_valid_without_cut():
    # 不触发摘要时 window 原样返回 rows；render_openai 必须兜底修复
    rows = [
        _row('user', 'hi'),
        _row('assistant', '', tool_calls=_tc_json('a', 'b')),
        _tool('a'),
        _row('user', 'next'),
    ]
    plan = context.plan_summary(rows, 30, 10 ** 9)
    assert plan['window'] is rows
    rendered = context.render_openai(plan['window'])
    assert [m['role'] for m in rendered] == ['user', 'user']


def test_render_openai_repairs_incomplete_tool_chain():
    rows = [
        _row('user', 'hi'),
        _row('assistant', '', tool_calls=_tc_json('a', 'b')),
        _tool('a'),
        _row('user', 'next'),
    ]
    out = context.render_openai(rows)
    assert [m['role'] for m in out] == ['user', 'user']
    assert not any('tool_calls' in m for m in out)


def test_render_openai_keeps_valid_tool_chain():
    rows = [
        _row('user', 'hi'),
        _row('assistant', '', tool_calls=_tc_json('a')),
        _tool('a', '结果'),
    ]
    out = context.render_openai(rows)
    assert [m['role'] for m in out] == ['user', 'assistant', 'tool']
    assert out[1]['tool_calls'][0]['id'] == 'a'
    assert out[2]['tool_call_id'] == 'a'


# ── Engine._dispatch / _fill_missing_tool_results ─────────────────

class _FakePlugin:
    def __init__(self):
        self.db_path = 'fake.db'
        self.logged = []

    def log(self, msg, level='info'):
        self.logged.append((level, msg))


class _FakeCtx:
    group_id = 123
    sender_id = '42'


def _new_engine():
    p = _FakePlugin()
    return engine.Engine(p), p


def test_dispatch_wraps_unexpected_exception():
    eng, p = _new_engine()

    async def boom(*a, **k):
        raise RuntimeError('炸了')

    eng._dispatch_impl = boom
    text = run(eng._dispatch('now_time', {}, _FakeCtx(), {}, []))
    assert text.startswith('错误: 工具 now_time 执行异常')
    assert '炸了' in text
    assert p.logged and p.logged[0][0] == 'error'


def test_dispatch_reraises_cancelled_error():
    eng, _ = _new_engine()

    async def boom(*a, **k):
        raise asyncio.CancelledError()

    eng._dispatch_impl = boom
    try:
        run(eng._dispatch('now_time', {}, _FakeCtx(), {}, []))
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError('CancelledError 应继续上抛')


def test_fill_missing_tool_results_only_unanswered(monkeypatch):
    stored = []
    monkeypatch.setattr(
        engine.storage, 'insert_message',
        lambda db, gid, role, content, **kw: stored.append(
            (role, content, kw)))

    eng, _ = _new_engine()
    messages = []
    answered = {'a'}
    wire = [
        {'id': 'a', 'function': {'name': 'fn_a'}},
        {'id': 'b', 'function': {'name': 'fn_b'}},
        {'id': '', 'function': {'name': 'fn_x'}},   # 无 id 的调用跳过
    ]
    eng._fill_missing_tool_results(123, messages, wire, answered, '本轮被中止')

    assert answered == {'a', 'b'}
    assert messages == [{'role': 'tool', 'tool_call_id': 'b',
                         'content': '错误: 工具调用未完成（本轮被中止）'}]
    assert stored == [('tool', messages[0]['content'],
                       {'tool_call_id': 'b', 'name': 'fn_b'})]


# ── LLM 瞬时失败重试 ───────────────────────────────────────────────

class _FakeMessage:
    content = 'ok'
    tool_calls = None
    reasoning_content = ''


class _FakeChoice:
    message = _FakeMessage()
    finish_reason = 'stop'


class _FakeResp:
    choices = [_FakeChoice()]
    usage = None


class _FakeCreate:
    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    async def __call__(self, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return _FakeResp()


class _FakeClient:
    def __init__(self, errors):
        self.calls = _FakeCreate(errors)
        create = self.calls

        class _Completions:
            pass

        comp = _Completions()
        comp.create = create

        class _Chat:
            pass

        chat = _Chat()
        chat.completions = comp
        self.chat = chat


def _new_llm(errors):
    client = llm.LLMClient()
    client._resolve = lambda cfg, route, model: ({'id': 'p'}, 'm')
    fake = _FakeClient(errors)
    client._get_client = lambda provider: fake
    return client, fake


def _cfg(**over):
    cfg = {'max_tokens': 100, 'request_timeout': 5}
    cfg.update(over)
    return cfg


def _chat(client, cfg):
    return run(client.chat([{'role': 'user', 'content': 'hi'}], cfg,
                           kind='judge'))


def test_chat_retries_transient_error(monkeypatch):
    client, fake = _new_llm([ConnectionError('reset')])
    logs = []
    client.log = lambda msg, level='info': logs.append((level, msg))

    result = _chat(client, _cfg(llm_retry_max=2, llm_retry_base_ms=0))
    assert result.content == 'ok'
    assert fake.calls.calls == 2
    assert logs and logs[0][0] == 'warn'


def test_chat_exponential_backoff_delays(monkeypatch):
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(llm.asyncio, 'sleep', fake_sleep)
    monkeypatch.setattr(llm.random, 'uniform', lambda a, b: 1.0)
    client, fake = _new_llm([ConnectionError('a'), ConnectionError('b')])

    _chat(client, _cfg(llm_retry_max=2, llm_retry_base_ms=1000,
                       llm_retry_max_ms=20000))
    assert fake.calls.calls == 3
    assert slept == [1.0, 2.0]


def test_chat_backoff_capped(monkeypatch):
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(llm.asyncio, 'sleep', fake_sleep)
    monkeypatch.setattr(llm.random, 'uniform', lambda a, b: 1.0)
    client, _ = _new_llm([ConnectionError('a'), ConnectionError('b'),
                          ConnectionError('c')])

    _chat(client, _cfg(llm_retry_max=3, llm_retry_base_ms=1000,
                       llm_retry_max_ms=1500))
    # 1s → 2s(封顶 1.5s) → 4s(封顶 1.5s)
    assert slept == [1.0, 1.5, 1.5]


def test_chat_backoff_has_jitter(monkeypatch):
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(llm.asyncio, 'sleep', fake_sleep)
    client, _ = _new_llm([ConnectionError('a')])

    _chat(client, _cfg(llm_retry_max=1, llm_retry_base_ms=1000))
    assert slept and 0.8 <= slept[0] <= 1.2


def test_chat_gives_up_after_max_retries(monkeypatch):
    async def fake_sleep(sec):
        pass

    monkeypatch.setattr(llm.asyncio, 'sleep', fake_sleep)
    client, fake = _new_llm([ConnectionError(str(i)) for i in range(5)])
    try:
        _chat(client, _cfg(llm_retry_max=2, llm_retry_base_ms=0))
    except ConnectionError:
        pass
    else:
        raise AssertionError('重试耗尽后应上抛原异常')
    assert fake.calls.calls == 3


def test_chat_does_not_retry_business_error(monkeypatch):
    client, fake = _new_llm([ValueError('bad param')])
    try:
        _chat(client, _cfg(llm_retry_max=3, llm_retry_base_ms=0))
    except ValueError:
        pass
    else:
        raise AssertionError('业务错误应立即上抛')
    assert fake.calls.calls == 1


def test_transient_error_detection():
    assert llm._is_transient_error(ConnectionError('reset'))
    assert llm._is_transient_error(TimeoutError())
    assert llm._is_transient_error(RuntimeError('upstream 502 Bad Gateway'))
    assert llm._is_transient_error(RuntimeError('Too many requests'))
    assert not llm._is_transient_error(ValueError('bad param'))
    assert not llm._is_transient_error(RuntimeError('invalid api key'))
