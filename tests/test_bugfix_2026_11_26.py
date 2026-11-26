"""docs/non-security-bug-audit-2026-11-26.md 中 B-01..B-07 的回归测试。

每个用例对应报告里的一个编号，尽量直接打到「缺陷复现的那条路径」上，
避免只断言实现细节。
"""
import asyncio
import inspect
import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'plugins', 'deepseek'))

import flask  # noqa: E402

from Core import engine as engine_mod  # noqa: E402
from Core import loop_run  # noqa: E402
from Core import permissions as perms  # noqa: E402
from Core import storage  # noqa: E402
from Core import subagent as subagent_mod  # noqa: E402
from Core import tasks as tasks_mod  # noqa: E402
from Core import tool_batch  # noqa: E402
from Core import web as web_mod  # noqa: E402

TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'plugins', 'deepseek', 'templates')


def _tmp_db() -> str:
    db = os.path.join(tempfile.mkdtemp(), 'bugfix.db')
    storage.migrate(db)
    return db


# ── B-01 仪表盘接口必须带 Blueprint 前缀 ─────────────────────────

def test_dashboard_api_paths_use_blueprint_prefix():
    with open(os.path.join(TEMPLATES, 'dashboard.html'),
              encoding='utf-8') as f:
        html = f.read()
    # 根路径 /api/* 会 404（Blueprint 挂在 /deepseek 下）
    assert "fetch('/api/" not in html
    assert "fetch(`/api/" not in html
    assert "BP + '/api/overview'" in html
    assert "BP + '/api/llm-health'" in html
    assert "BP + '/api/reconnect'" in html


# ── B-02 Web 审批必须桥回事件循环 await 异步 decide ─────────────

class _FakeApprovals:
    def __init__(self):
        self.calls = []
        self.ok = True

    async def decide(self, approval_id, *, approved, decided_by='web'):
        self.calls.append((approval_id, approved, decided_by))
        return self.ok


def test_web_approval_decide_awaits_async_decide():
    from core import globals as core_globals

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    old = core_globals.event_loop
    core_globals.event_loop = loop
    try:
        fake = _FakeApprovals()
        plugin = type('P', (), {'engine': type(
            'E', (), {'approvals': fake})()})()
        app = flask.Flask(__name__)
        app.register_blueprint(web_mod.build_blueprint(plugin),
                               url_prefix='/deepseek')
        client = app.test_client()

        r = client.post('/deepseek/api/approvals/decide',
                        json={'id': 7, 'approved': True})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.get_json()['ok'] is True
        # 关键：确实执行了协程，而不是把 coroutine 当 bool(True)
        assert fake.calls == [(7, True, 'web-admin')]

        fake.ok = False
        r = client.post('/deepseek/api/approvals/decide',
                        json={'id': 8, 'approved': False})
        assert r.status_code == 404
        assert r.get_json()['ok'] is False

        r = client.post('/deepseek/api/approvals/decide', json={'id': 'x'})
        assert r.status_code == 400
    finally:
        core_globals.event_loop = old
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)


# ── B-03 批调度组超时不能给同一 tool_call 写两条结果 ────────────

def test_batch_group_timeout_single_result_per_call():
    calls = [{'id': 'fast', 'name': 'fs_read', 'arguments': {}},
             {'id': 'slow', 'name': 'fs_list', 'arguments': {}}]

    async def dispatch(norm):
        if norm['id'] == 'slow':
            await asyncio.sleep(1)
        return f'ok-{norm["id"]}'

    cfg = {'tool_parallel_enabled': True,
           'tool_parallel_group_timeout': 0.05}
    out = asyncio.run(tool_batch.run_batch(dispatch, calls, cfg=cfg, gid='g'))
    ids = [c['id'] for c, _ in out.results]
    assert ids == ['fast', 'slow']
    assert out.results[0][1] == 'ok-fast'
    assert out.results[1][1].startswith('[工具超时]')


def test_chat_loop_fills_missing_results_only_outside_batch():
    """单工具 CancelledError 分支不能再补占位（会与统一 append 重复）。"""
    src = inspect.getsource(engine_mod.Engine._chat_loop)
    # 期望仅两处：插话发送被取消、run_batch 外层被取消
    assert src.count('_fill_missing_tool_results(') == 2
    # run_batch 外层取消分支必须仍然补齐
    tail = src[src.index('run_batch'):]
    assert 'except asyncio.CancelledError' in tail
    assert '_fill_missing_tool_results(' in tail


# ── B-04 权限热更新必须刷新规则快照 ─────────────────────────────

def test_permissions_refresh_applies_new_rules():
    eng = perms.PermissionEngine({'permissions': {'default_action': 'allow'}})
    assert eng.decide(tool_key='fs.read', args={}, actor_role='member',
                      gid='g').action == 'allow'
    eng.refresh({'permissions': {'default_action': 'deny'}})
    assert eng.decide(tool_key='fs.read', args={}, actor_role='member',
                      gid='g').action == 'deny'

    eng.refresh({'permissions': {'default_action': 'allow', 'rules': [
        {'tool': 'fs.write', 'action': 'deny', 'reason': '禁写'}]}})
    d = eng.decide(tool_key='fs.write', args={}, actor_role='owner', gid='g')
    assert d.action == 'deny' and d.reason == '禁写'

    # legacy（无 permissions）也要跟着 cfg 变
    eng.refresh({'risky_owner_only': True})
    assert eng.decide(tool_key='fs.write', args={}, actor_role='member',
                      gid='g').action == 'deny'
    eng.refresh({'risky_owner_only': False})
    assert eng.decide(tool_key='fs.write', args={}, actor_role='member',
                      gid='g').action == 'allow'


# ── B-05 子代理工具名 ↔ 内部 key 映射 ───────────────────────────

def test_subagent_maps_tool_name_to_internal_key():
    assert subagent_mod.tool_key_of('fs_read') == 'fs.read'
    assert subagent_mod.tool_key_of('history_query') == 'history.query'
    assert subagent_mod.tool_key_of('fs.nope') == 'fs.nope'

    runner = subagent_mod.SubagentRunner(engine=None)
    schemas = runner._schemas(('fs_read',))
    names = {(s.get('function') or {}).get('name') for s in schemas}
    assert 'fs_read' in names
    # 白名单外的工具即使写成内部 key 也要被过滤掉
    assert 'fs_write' not in {
        s['function']['name'] for s in runner._schemas(('fs.write',))}


class _FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    async def chat(self, messages, cfg, tools=None):
        self.seen.append([t for t in (tools or [])])
        step = self.script.pop(0)
        return type('R', (), step)()


def test_subagent_loop_dispatches_readonly_tool():
    dispatched = []

    class E:
        p = type('P', (), {'cfg': {}, 'llm': None, 'bot_id': '1'})()

        def __init__(self):
            self.log = lambda *a, **k: None

        async def _dispatch(self, name, args, ctx, cfg, em, ec=None):
            dispatched.append((name, args))
            return '文件内容'

    eng = E()
    eng.p.llm = _FakeLLM([
        {'content': '', 'usage': {}, 'tool_calls': [
            {'id': 'c1', 'name': 'fs_read', 'arguments': {'path': 'a.txt'}}]},
        {'content': '结论：a.txt 内容是文件内容', 'usage': {},
         'tool_calls': []},
    ])
    runner = subagent_mod.SubagentRunner(eng)
    # 2026-11-26：_tool_ctx 增加 allow_tools 形参（子代理额外工具授权）
    runner._tool_ctx = lambda *a, **k: object()  # noqa: ARG005
    spec = subagent_mod.SubagentSpec(task='看看 a.txt', tools=('fs_read',),
                                     max_rounds=3)
    res = asyncio.run(runner.run(spec, gid='g'))
    assert dispatched == [('fs_read', {'path': 'a.txt'})]
    assert '结论' in res.text and not res.error
    # 第二轮发给模型的 tool 结果不能是拒绝文案
    assert res.text and '[拒绝]' not in res.text


# ── B-06 私聊长跑的 chat_type 不能被 resolver 丢掉 ───────────────

def test_loop_chat_type_reads_raw_settings():
    db = _tmp_db()
    storage.upsert_group(db, '20001', settings={'chat_type': 'private'})
    storage.upsert_group(db, '90001', settings={})
    plugin = type('P', (), {'db_path': db,
                            'cfg': {'owner_qq': '10000'}})()
    mgr = loop_run.LoopManager(plugin)
    # 普通好友私聊：不能因为 gid != owner_qq 退化成 group
    assert mgr._chat_type('20001') == 'private'
    assert mgr._chat_type('90001') == 'group'
    # 主人 QQ 仍是兜底 private
    assert mgr._chat_type('10000') == 'private'


# ── B-07 关闭群的到期约定要顺延，不能长期占住 LIMIT 100 ─────────

class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)


def test_disabled_group_commitments_are_snoozed():
    db = _tmp_db()
    now = 1_700_000_000.0
    storage.upsert_group(db, '111', enabled=False, settings={})
    storage.upsert_group(db, '222', enabled=True, settings={})
    # 关闭群的约定到期更早，会排在查询窗口最前面
    stale = storage.add_commitment(db, '111', 'u1', '甲', '旧约定',
                                   due_at=now - 100, due_text='昨天')
    active = storage.add_commitment(db, '222', 'u2', '乙', '新约定',
                                    due_at=now - 10, due_text='刚刚')

    bot = _FakeBot()
    plugin = type('P', (), {'cfg': {'reminder_on': True}, 'bot': bot})()
    mgr = tasks_mod.TaskManager(db, memory=None)
    asyncio.run(mgr._tick(plugin))

    sent_targets = {s['target_id'] for s in bot.sent}
    assert '222' in sent_targets          # 正常群照常提醒
    assert '111' not in sent_targets      # 关闭群不投递

    rows = {r['id']: r for r in storage.list_commitments(db, status='')}
    assert rows[active]['reminded'] is True
    # 关闭群的行既不能标记完成（重开还要提醒），也不能继续卡在原到期时间
    assert rows[stale]['reminded'] is False
    assert rows[stale]['due_at'] > now


def test_due_commitments_window_not_starved_by_disabled_groups():
    """复现报告里的饥饿场景：100 条关闭群旧约定 + 1 条正常群约定。"""
    db = _tmp_db()
    now = 1_700_000_000.0
    storage.upsert_group(db, 'closed', enabled=False, settings={})
    storage.upsert_group(db, 'open', enabled=True, settings={})
    for i in range(100):
        storage.add_commitment(db, 'closed', 'u', '甲', f'旧{i}',
                               due_at=now - 5000 + i, due_text='e')
    storage.add_commitment(db, 'open', 'u2', '乙', '该提醒的',
                           due_at=now - 1, due_text='e')

    bot = _FakeBot()
    plugin = type('P', (), {'cfg': {'reminder_on': True}, 'bot': bot})()
    mgr = tasks_mod.TaskManager(db, memory=None)
    # 第一轮先把 100 条关闭群的顺延掉
    asyncio.run(mgr._tick(plugin))
    asyncio.run(mgr._tick(plugin))
    assert any(s['target_id'] == 'open' for s in bot.sent)
