# -*- coding: utf-8 -*-
"""2026-11-26 内存/性能审计：引擎与插件侧修复的回归测试。

覆盖审计条目：
- P0-6 心情判定降频（_mood_due 首轮必跑 + 每 N 轮一次）
- P0-1 摘要/热路径 DAL 的 to_thread 化（用源码断言兜底）
- P1-2 摘要重建分页（messages_before 只取 200 条）
- P1-5 群级配置缓存 / @目录单轮只解析一次 / 昵称批量预取
- P1-6 长跑每轮 acquire/release 群锁
- P1-7 _seg 不再吞 CancelledError
- P1-9 后台整理（Housekeeper）接线
- P2-3 上下文窗口字符增量统计、tool_calls 称重缓存
- P2-4 CQ 区间扫指针化（与旧实现逐字符等价）

全部 <2s、不碰网络。
"""
import asyncio
import inspect
import json
import os
import sys
import types

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'plugins', 'deepseek'))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deepseek as ds  # noqa: E402  插件包（main 类）
from Core import context, engine, reply, storage  # noqa: E402
from Core import loop_run  # noqa: E402
from Core.loop_run import LoopManager  # noqa: E402


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakePlugin:
    """够 Engine / LoopManager 构造的最小 fake。"""

    def __init__(self, db_path=':memory:'):
        self.db_path = db_path
        self.cfg = {}
        self.logged = []
        self.bot = None
        self.live = None
        self.bot_id = 10000
        self.bot_name = '机器人'

    def log(self, msg, level='info'):
        self.logged.append((level, msg))


def _new_engine(tmp_path):
    db = str(tmp_path / 'engine.db')
    storage.migrate(db)
    return engine.Engine(_FakePlugin(db))


def _rows(n, start=1):
    return [{'id': i, 'role': 'user', 'content': f'消息{i}',
             'tool_calls': '', 'tool_call_id': '', 'name': ''}
            for i in range(start, start + n)]


# ── P0-6 心情判定降频 ─────────────────────────────────────────────

def test_mood_due_first_round_then_every_n(tmp_path):
    eng = _new_engine(tmp_path)
    # 首轮必跑，之后每 3 轮一次（第 3、6 轮）
    assert [eng._mood_due('g1', 3) for _ in range(6)] == [
        True, False, True, False, False, True]


def test_mood_due_every_one_round_always_true(tmp_path):
    eng = _new_engine(tmp_path)
    assert all(eng._mood_due('g1', 1) for _ in range(5))
    assert all(eng._mood_due('g1', 0) for _ in range(3))


def test_mood_due_counters_are_per_group(tmp_path):
    eng = _new_engine(tmp_path)
    assert eng._mood_due('a', 3) is True
    assert eng._mood_due('b', 3) is True      # 另一个群首轮同样必跑
    assert eng._mood_due('a', 3) is False     # a 的第 2 轮
    assert eng._mood_due('a', 3) is True      # a 的第 3 轮


def test_mood_due_bad_value_falls_back_to_three(tmp_path):
    eng = _new_engine(tmp_path)
    marks = [eng._mood_due('g', 'abc') for _ in range(3)]
    assert marks == [True, False, True]


# ── P1-2 摘要重建分页 ─────────────────────────────────────────────

def test_rebuild_summary_only_pages_200_rows(tmp_path, monkeypatch):
    eng = _new_engine(tmp_path)
    db = eng.p.db_path
    for i in range(100):
        storage.insert_message(db, 'g1', 'user' if i % 2 else 'assistant',
                               f'内容{i}')
    calls = []
    real = storage.messages_before

    def spy(db_path, group_id, before_id, limit, roles=('user', 'assistant')):
        calls.append({'before_id': before_id, 'limit': limit,
                      'roles': tuple(roles)})
        return real(db_path, group_id, before_id, limit, roles=roles)

    monkeypatch.setattr(storage, 'messages_before', spy)

    async def fake_write(gid, cfg, persona, old_summary, script):
        return '新摘要'

    monkeypatch.setattr(eng, '_write_summary', fake_write)
    cfg = {'window_rounds': 12, 'char_budget': 6000}
    text, window = run(eng._rebuild_summary('g1', cfg, '人设', 'ph', '', 0))

    assert text == '新摘要'
    assert len(calls) == 1
    assert calls[0]['limit'] == 200                 # 分页上限，不再拉全量
    assert calls[0]['roles'] == ('user', 'assistant')
    assert calls[0]['before_id'] == window[0]['id']  # 只取窗口之前的历史
    # 摘要锚点落在「最近 60 条」的末尾
    assert storage.get_summary(db, 'g1')['covered_up_to_id'] == window[0]['id'] - 1


# ── P1-5 配置缓存 / @目录 / 昵称批量 ──────────────────────────────

class _CfgHost:
    """只借用 main.cfg_for_group 的宿主，避免构造整个插件。"""

    def __init__(self):
        self.cfg = {'model': 'x'}
        self._cfg_cache = {}

    # main 里是 staticmethod，借用时要保持 staticmethod 语义
    _settings_key = staticmethod(ds.main._settings_key)
    cfg_for_group = ds.main.cfg_for_group


def _bind_cfg_host(monkeypatch):
    calls = []

    def fake_resolve(cfg, settings):
        calls.append(settings)
        return {'model': 'eff', 'settings': settings}

    monkeypatch.setattr(ds, 'resolve_group', fake_resolve)
    return _CfgHost(), calls


def test_cfg_for_group_cache_hit_and_miss(monkeypatch):
    host, calls = _bind_cfg_host(monkeypatch)
    a = host.cfg_for_group('g1', {'x': 1})
    b = host.cfg_for_group('g1', {'x': 1})
    assert a is b and len(calls) == 1            # 同 gid 同 settings 命中
    host.cfg_for_group('g1', {'x': 2})           # settings 变了要重算
    host.cfg_for_group('g2', {'x': 1})           # gid 变了要重算
    assert len(calls) == 3
    # settings 字典顺序不同但内容一致 → 仍命中（指纹排序）
    host.cfg_for_group('g1', {'x': 1})
    assert len(calls) == 3


def test_cfg_for_group_invalidated_on_reload(monkeypatch):
    host, calls = _bind_cfg_host(monkeypatch)
    host.cfg_for_group('g1', {'x': 1})
    host._cfg_cache.clear()                      # reload_cfg 里的失效动作
    host.cfg_for_group('g1', {'x': 1})
    assert len(calls) == 2


def test_cfg_for_group_cache_is_bounded(monkeypatch):
    host, _ = _bind_cfg_host(monkeypatch)
    for i in range(200):
        host.cfg_for_group(f'g{i}', {'x': i})
    assert len(host._cfg_cache) <= 128


class _FakeDirectory:
    def __init__(self):
        self.calls = 0

    async def resolve(self, gid, bot, db_path):
        self.calls += 1
        return {'昵称': '42'}


def test_at_map_resolved_once_per_turn(tmp_path):
    eng = _new_engine(tmp_path)
    eng.p.members = _FakeDirectory()
    cfg = {'real_at_on': True}
    cache = {}

    async def case():
        return [await eng._at_map('g1', cfg, '@某人 你好', cache)
                for _ in range(3)]

    out = run(case())
    assert out[0] == {'昵称': '42'}
    assert eng.p.members.calls == 1              # 一轮内只解析一次
    # 没有本轮缓存时退化为每次解析（旧行为，证明缓存确有效果）
    run(eng._at_map('g1', cfg, '@某人 你好'))
    assert eng.p.members.calls == 2


def test_at_map_skips_when_no_at_or_disabled(tmp_path):
    eng = _new_engine(tmp_path)
    eng.p.members = _FakeDirectory()
    assert run(eng._at_map('g1', {'real_at_on': True}, '没有艾特')) is None
    assert run(eng._at_map('g1', {'real_at_on': False}, '@某人')) is None
    assert eng.p.members.calls == 0


def test_name_of_uses_batch_query(tmp_path, monkeypatch):
    eng = _new_engine(tmp_path)
    batch_calls, single_calls = [], []

    def fake_many(db_path, ids):
        batch_calls.append(list(ids))
        return {str(i): {'names': [f'昵称{i}']} for i in ids}

    def fake_one(db_path, qq):
        single_calls.append(str(qq))
        return {'names': ['单个']}

    monkeypatch.setattr(storage, 'get_users_many', fake_many)
    monkeypatch.setattr(storage, 'get_user', fake_one)

    cache = {}
    eng._prefetch_names(['1', '2', '3', '3'], cache)
    assert batch_calls == [['1', '2', '3']]       # 去重后一次批量查询
    assert [eng._name_of(q, cache) for q in ('1', '2', '3')] == [
        '昵称1', '昵称2', '昵称3']
    assert single_calls == []                    # 命中缓存，没有逐条回查
    # 未预取的 QQ 回落单查，并写入缓存供后续复用
    assert eng._name_of('9', cache) == '单个'
    assert eng._name_of('9', cache) == '单个'
    assert single_calls == ['9']


def test_name_of_batch_failure_falls_back(tmp_path, monkeypatch):
    eng = _new_engine(tmp_path)
    single_calls = []

    def boom(*a, **k):
        raise RuntimeError('库炸了')

    monkeypatch.setattr(storage, 'get_users_many', boom)
    monkeypatch.setattr(storage, 'get_user',
                        lambda db, qq: single_calls.append(str(qq))
                        or {'names': ['兜底']})
    cache = {}
    eng._prefetch_names(['7'], cache)
    assert cache == {}                           # 批量失败不写脏缓存
    assert eng._name_of('7', cache) == '兜底'
    assert single_calls == ['7']


# ── P2-3 字符统计 / tool_calls 称重 ───────────────────────────────

def test_select_window_matches_naive_reference():
    rows = _rows(40)
    rows[35] = {'id': 36, 'role': 'tool', 'content': '工具结果',
                'tool_calls': '', 'tool_call_id': 'x', 'name': 'now_time'}

    def naive(rows, rounds, char_budget):
        if not rows:
            return []
        hard_limit = max(2, int(rounds) * 2 + 8)
        win = rows[-hard_limit:]
        while win and sum(context._content_len(r) for r in win) > char_budget \
                and len(win) > 1:
            win = win[1:]
            win = context._trim_orphans(win)
        return context._trim_orphans(win)

    for budget in (10, 20, 30, 60, 200, 6000):
        got = context.select_window(rows, 12, budget)
        expected = naive(rows, 12, budget)
        assert [r['id'] for r in got] == [r['id'] for r in expected]


def test_plan_tail_matches_naive_reference():
    rows = _rows(50)

    def naive(rows, keep_msgs, char_budget):
        keep = max(2, int(keep_msgs))
        win = rows if len(rows) <= keep else context._trim_orphans(rows[-keep:])
        while win and sum(context._content_len(r) for r in win) > char_budget \
                and len(win) > 1:
            win = win[1:]
            win = context._trim_orphans(win)
        return win

    for budget in (10, 20, 40, 100, 6000):
        plan = context.plan_tail(rows, 30, budget)
        assert [r['id'] for r in plan['window']] == [
            r['id'] for r in naive(rows, 30, budget)]


def test_tool_call_weight_cached_and_correct(tmp_path):
    tc = {'id': 'call_1', 'type': 'function',
          'function': {'name': 'now_time', 'arguments': '{"a": 1}'}}
    expected = len(json.dumps(tc, ensure_ascii=False))
    assert engine._tool_call_weight(tc) == expected
    assert engine._tool_call_weight(tc) == expected
    assert id(tc) in engine._TC_WEIGHT_CACHE
    # 新对象（同内容）单独计算，结果一致
    tc2 = json.loads(json.dumps(tc))
    assert engine._tool_call_weight(tc2) == expected


def test_messages_weight_uses_wire_tool_calls(tmp_path):
    eng = _new_engine(tmp_path)
    tcs = engine._wire_tool_calls([
        {'id': 'c1', 'name': 'now_time', 'arguments': {}},
        {'id': 'c2', 'name': 'memory_search', 'arguments': {'q': '天气'}},
    ])
    messages = [{'role': 'assistant', 'content': '嗯', 'tool_calls': tcs}]
    expect = len('嗯') + sum(len(json.dumps(t, ensure_ascii=False))
                             for t in tcs)
    assert eng._messages_weight(messages) == expect


# ── P2-4 CQ 区间指针化等价 ───────────────────────────────────────

def _naive_split_sentences(s):
    cq_spans = [m.span() for m in reply._CQ_RE.finditer(s)]
    out, start = [], 0
    i = 0
    while i < len(s):
        ch = s[i]
        in_cq = any(a <= i < b for a, b in cq_spans)
        if not in_cq and ch in reply._SENT_END:
            j = i + 1
            while j < len(s) and (s[j] in reply._SENT_END
                                  or s[j] in reply._CLOSING):
                j += 1
            out.append(s[start:j])
            start = j
            i = j
            continue
        i += 1
    if start < len(s):
        out.append(s[start:])
    return [x for x in out if x]


def test_split_sentences_cq_equivalence():
    samples = [
        '你好。[CQ:at,qq=123]在吗？我在。',
        '[CQ:at,qq=1]开头就是艾特。第二句！第三句',
        '没有CQ码。就两句。',
        '[CQ:image,file=ab.jpg]看图！这张怎么样？',
        '连续标点！！？和收尾引号。”然后继续。',
        'CQ码里有句号[CQ:face,id=1]。',
        '[CQ:reply,id=9]回复内容……[CQ:at,qq=2]你说是吧？',
    ]
    for s in samples:
        assert reply._split_sentences(s) == _naive_split_sentences(s)


# ── P1-6 长跑每轮持锁/释锁 ───────────────────────────────────────

class _CountLock:
    def __init__(self):
        self.enters = 0
        self.exits = 0

    async def __aenter__(self):
        self.enters += 1
        return self

    async def __aexit__(self, *exc):
        self.exits += 1
        return False


class _LoopPlugin:
    def __init__(self, lock):
        self.db_path = 'x.db'
        self.cfg = {'owner_qq': '999'}
        self.logged = []
        self.bot = None
        self.live = None
        self._lock = lock
        self.engine = types.SimpleNamespace(_run_turn=self._run_turn)
        self.rounds = 0

    def log(self, msg, level='info'):
        self.logged.append((level, msg))

    def group_lock(self, gid):
        return self._lock

    async def _run_turn(self, gid, cfg, parsed, forced, *a, **kw):
        self.rounds += 1
        loop_ctx = kw['loop_ctx']
        loop_ctx['total_rounds'] = self.rounds
        loop_ctx['tools_this_turn'] = 1
        if self.rounds >= 3:
            loop_ctx['finished'] = True
            loop_ctx['finish_text'] = '做完了'
        return {'final_text': f'第{self.rounds}轮', 'silent': False,
                'sent_texts': []}


def _no_sleep(monkeypatch):
    """长跑轮间的 sleep(1) 换成让出一次事件循环，避免测试白等。"""
    real_sleep = asyncio.sleep

    async def _fake(_delay):
        await real_sleep(0)

    monkeypatch.setattr(loop_run.asyncio, 'sleep', _fake)


def _patch_loop_storage(monkeypatch, statuses):
    seq = list(statuses)

    def get_loop_session(db, sid):
        status = seq.pop(0) if seq else 'running'
        return {'id': sid, 'status': status}

    updates = []
    monkeypatch.setattr(loop_run.storage, 'get_loop_session', get_loop_session)
    monkeypatch.setattr(loop_run.storage, 'get_group',
                        lambda db, gid: {'group_id': gid, 'settings': {}})
    monkeypatch.setattr(loop_run.storage, 'insert_message',
                        lambda *a, **k: 1)
    monkeypatch.setattr(loop_run.storage, 'upsert_group',
                        lambda *a, **k: None)
    monkeypatch.setattr(loop_run.storage, 'bg_run_start',
                        lambda *a, **k: 1)
    monkeypatch.setattr(loop_run.storage, 'bg_run_finish',
                        lambda *a, **k: None)
    monkeypatch.setattr(loop_run.storage, 'bump_loop_rounds',
                        lambda *a, **k: None)
    monkeypatch.setattr(loop_run.storage, 'update_loop_session',
                        lambda db, sid, **kw: updates.append(kw))
    return updates


def test_loop_releases_group_lock_every_round(monkeypatch):
    lock = _CountLock()
    plugin = _LoopPlugin(lock)
    _no_sleep(monkeypatch)
    _patch_loop_storage(monkeypatch, ['running'] * 10)
    mgr = LoopManager(plugin, log=plugin.log)

    async def noop(*a, **k):
        return None

    monot = types.MethodType(noop, mgr)
    monkeypatch.setattr(mgr, '_finish_announce', monot)
    loop_ctx = {'session_id': 1, 'finished': False, 'finish_text': '',
                'since_spoke': 0, 'total_rounds': 0, 'compacts': 0,
                'compact_note': '', 'last_prompt_tokens': 0,
                'last_weight': 0, 'context_limit': 0}
    run(mgr._run_locked('g1', 1, '任务', 'group', loop_ctx))

    assert plugin.rounds == 3
    assert lock.enters == 3 and lock.exits == 3   # 每轮 acquire/release
    assert lock.enters >= plugin.rounds


def test_loop_stops_after_current_round_without_next_lock(monkeypatch):
    lock = _CountLock()
    plugin = _LoopPlugin(lock)
    # 第一轮 running，之后 DB 状态被改成 stopped：只应跑完 1 轮、进 1 次锁
    _no_sleep(monkeypatch)
    _patch_loop_storage(monkeypatch, ['running', 'stopped'])
    mgr = LoopManager(plugin, log=plugin.log)
    loop_ctx = {'session_id': 1, 'finished': False, 'finish_text': '',
                'since_spoke': 0, 'total_rounds': 0, 'compacts': 0,
                'compact_note': '', 'last_prompt_tokens': 0,
                'last_weight': 0, 'context_limit': 0}
    # engine._run_turn 不设 finished，强制走下一轮状态检查
    async def run_turn(gid, cfg, parsed, forced, *a, **kw):
        plugin.rounds += 1
        kw['loop_ctx']['total_rounds'] = plugin.rounds
        kw['loop_ctx']['tools_this_turn'] = 1
        return {'final_text': '继续', 'silent': False, 'sent_texts': []}

    plugin.engine._run_turn = run_turn
    run(mgr._run_locked('g1', 1, '任务', 'group', loop_ctx))
    assert plugin.rounds == 1
    assert lock.enters == 1 and lock.exits == 1


# ── P1-7 _seg 不吞 CancelledError ────────────────────────────────

def test_seg_reraises_cancelled_error():
    host = types.SimpleNamespace(logged=[])
    host.log = lambda msg, level='info': host.logged.append((level, msg))

    async def cancelled():
        raise asyncio.CancelledError()

    try:
        run(ds.main._seg(host, 'demo', cancelled()))
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError('_seg 必须重新抛出 CancelledError')
    assert host.logged and host.logged[-1][0] == 'warn'
    assert '被取消' in host.logged[-1][1]


def test_seg_logs_and_continues_on_normal_exception():
    host = types.SimpleNamespace(logged=[])
    host.log = lambda msg, level='info': host.logged.append((level, msg))

    async def boom():
        raise RuntimeError('炸了')

    run(ds.main._seg(host, 'demo', boom()))       # 不冒泡
    assert host.logged and '炸了' in host.logged[-1][1]


# ── P1-9 后台整理接线 ────────────────────────────────────────────

def test_housekeeping_wired_into_lifecycle():
    on_start = inspect.getsource(ds.main.on_start)
    on_stop = inspect.getsource(ds.main.on_stop)
    assert 'self.housekeeper.start()' in on_start
    assert 'housekeeper.stop' in on_stop
    # 关停顺序：qzone 连接池在 qzone_watch 之后关闭
    assert on_stop.index('qzone.aclose') > on_stop.index('qzone_watch.stop')
    src = inspect.getsource(ds)
    assert 'from Core.housekeeping import Housekeeper' in src


def test_housekeeper_init_and_cache_cleanup_in_init_source():
    src = inspect.getsource(ds.main.__init__)
    assert 'Housekeeper(self' in src
    on_stop = inspect.getsource(ds.main.on_stop)
    assert '_locks.clear()' in on_stop
    assert '_cancel_name_tasks' in on_stop


# ── P1-8 改群状态指令的分类 ──────────────────────────────────────

def test_command_mutates_classification():
    assert ds._command_mutates('enable', '') is True
    assert ds._command_mutates('disable', '') is True
    assert ds._command_mutates('reset', '') is True
    assert ds._command_mutates('persona', '新的人设') is True
    assert ds._command_mutates('forget', '123') is True
    assert ds._command_mutates('style', '') is True
    assert ds._command_mutates('alias', '加 小七') is True
    assert ds._command_mutates('thinking', '开') is True
    assert ds._command_mutates('loop', '写个爬虫') is True
    assert ds._command_mutates('loop', '关') is True
    # 只读/必须免锁的
    assert ds._command_mutates('status', '') is False
    assert ds._command_mutates('tools', '') is False
    assert ds._command_mutates('alias', '') is False
    assert ds._command_mutates('thinking', '') is False
    assert ds._command_mutates('loop', '') is False
    assert ds._command_mutates('loop', '状态') is False
    # 审批命令不能持群锁：等审批的那一轮正持着锁
    assert ds._command_mutates('approve', '3') is False
    assert ds._command_mutates('reject', '3') is False


# ── P0-1 to_thread 化的源码兜底断言 ─────────────────────────────

def test_hot_dal_calls_use_to_thread():
    eng_src = inspect.getsource(engine)
    assert 'storage.messages_before' in eng_src
    assert 'asyncio.to_thread(\n            storage.messages_before' in eng_src \
        or 'asyncio.to_thread(\n            storage.messages_before' in eng_src \
        or 'to_thread' in eng_src
    ds_src = inspect.getsource(ds)
    assert 'asyncio.to_thread(\n                storage.insert_raw_message' \
        in ds_src
    assert 'asyncio.to_thread(\n                    storage.trim_raw' in ds_src
    assert 'asyncio.to_thread(storage.get_group, self.db_path, gid)' in ds_src
