# -*- coding: utf-8 -*-
"""子代理额外工具授权（2026-11-26）：MCP 浏览器操作 / 自定义工具。

背景：B-08 之后子代理是 `readonly` 身份，只读白名单（READONLY_TOOL_KEYS）
之外一律拒绝——包括 MCP 工具（`mcp__<server>__<tool>`，权限引擎一律按风险
处理）与自定义工具（`custom__<name>`）。但「让子代理去浏览器里查点东西」
是真实需求，所以加一个**主人显式授权**的开关：

- 新配置 `subagent_extra_tools`（逗号分隔、支持 fnmatch 通配、默认空）；
- 命中的工具会真正下发到子代理的 `tools` 数组，并通过
  `ToolContext.allow_tools` 交给 engine 门禁放行；
- 未命中的仍按只读身份拒绝；模型传 `tools` 参数只能收窄、不能提权；
- 默认空 = 行为与「纯只读子代理」完全一致。

全部 <2s、不碰网络。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'plugins', 'deepseek'))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Core import builtin_tools, config, engine, storage, subagent, tool_batch
from Core.tool_batch import READONLY_TOOL_KEYS


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _ChatResult:
    def __init__(self, content='', tool_calls=None, usage=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.usage = usage or {}


class _FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.tool_names_seen = []

    async def chat(self, messages, cfg, tools=None):
        self.tool_names_seen.append(
            [str((t.get('function') or {}).get('name') or '')
             for t in (tools or [])])
        return self.script.pop(0)


class _FakePlugin:
    def __init__(self, db_path, cfg, llm=None):
        self.db_path = db_path
        self.cfg = dict(cfg)
        self.bot = None
        self.live = None
        self.llm = llm
        self.bot_id = 10000
        self.bot_name = '机器人'
        self.logged = []

    def log(self, msg, level='info'):
        self.logged.append((level, msg))

    def sandbox(self, gid):
        return None


class _FakeEngine:
    """只实现子代理用到的几个入口，避免真 Engine 的全部接线。"""

    def __init__(self, plugin, schemas):
        self.p = plugin
        self.schemas = list(schemas)
        self.calls = []
        self.notes = []

    def _build_tools(self, gid, cfg, deny=frozenset()):
        return list(self.schemas), {}, {}

    async def _dispatch(self, name, args, ctx, cfg, _a=None, _b=None):
        self.calls.append({'name': name, 'args': args,
                           'allow_tools': frozenset(
                               getattr(ctx, 'allow_tools', None) or ()),
                           'role': getattr(ctx, 'actor_role', '')})
        return f'ok:{name}'

    def log(self, msg, level='info'):
        self.notes.append((level, msg))


def _schema(name):
    return {'type': 'function',
            'function': {'name': name, 'description': name,
                         'parameters': {'type': 'object', 'properties': {}}}}


def _all_schemas():
    """内置只读 + 一个写工具 + 两个 MCP + 一个自定义工具。"""
    return [*builtin_tools.schemas(set(READONLY_TOOL_KEYS)),
            _schema('fs_write'), _schema('qq_send_text'),
            _schema('mcp__pw__browser_snapshot'),
            _schema('mcp__pw__browser_click'),
            _schema('custom__bili_hot')]


def _runner(tmp_path, extra='', script=None):
    db = str(tmp_path / 'sub.db')
    storage.migrate(db)
    cfg = dict(config.DEFAULTS)
    cfg['subagent_extra_tools'] = extra
    plugin = _FakePlugin(db, cfg, _FakeLLM(script or [_ChatResult('结论')]))
    eng = _FakeEngine(plugin, _all_schemas())
    return subagent.SubagentRunner(eng), plugin, eng


# ── 配置解析与通配匹配 ───────────────────────────────────────

def test_parse_tool_patterns_accepts_list_and_separators():
    assert tool_batch.parse_tool_patterns('a, b，c、d\ne  f') == (
        'a', 'b', 'c', 'd', 'e', 'f')
    assert tool_batch.parse_tool_patterns(['x', ' x ', '', None]) == ('x',)
    assert tool_batch.parse_tool_patterns('') == ()


def test_matched_any_wildcard_and_key_forms():
    pats = tool_batch.parse_tool_patterns(
        'mcp__pw__browser_*, custom__bili_hot, fs_read')
    assert tool_batch.matched_any(pats, 'mcp__pw__browser_snapshot',
                                  'mcp__pw__browser_snapshot')
    assert tool_batch.matched_any(pats, 'mcp__pw__browser_click',
                                  'mcp__pw__browser_click')
    # 内部 key 与函数名两种写法都认
    assert tool_batch.matched_any(pats, 'fs_read', 'fs.read')
    assert not tool_batch.matched_any(pats, 'mcp__other__x', 'mcp__other__x')
    assert not tool_batch.matched_any(pats, 'fs_write', 'fs.write')


def test_default_config_is_pure_readonly():
    assert config.DEFAULTS['subagent_extra_tools'] == ''


# ── 工具集组装：只读底座 + 主人授权的 extras ─────────────────

def test_tools_for_adds_matching_extras_only(tmp_path):
    runner, _p, _e = _runner(tmp_path, extra='mcp__pw__browser_*, custom__bili_hot')
    spec = subagent.SubagentSpec(task='查一下')
    schemas, allowed = runner._tools_for(spec, config.merge_config(
        {'subagent_extra_tools': 'mcp__pw__browser_*, custom__bili_hot'}), '1')
    names = {s['function']['name'] for s in schemas}
    # 命中的额外工具在
    assert {'mcp__pw__browser_snapshot', 'mcp__pw__browser_click',
            'custom__bili_hot'} <= names
    assert 'mcp__pw__browser_snapshot' in allowed
    # 没命中的写/发送类不在
    assert 'fs_write' not in names and 'qq_send_text' not in names
    # 只读底座仍在
    assert 'fs_read' in names
    assert {'fs.read', 'fs_read'} & set(allowed)


def test_tools_for_empty_config_keeps_readonly_only(tmp_path):
    runner, _p, _e = _runner(tmp_path)
    schemas, allowed = runner._tools_for(
        subagent.SubagentSpec(task='t'), dict(config.DEFAULTS), '1')
    names = {s['function']['name'] for s in schemas}
    assert 'mcp__pw__browser_snapshot' not in names
    assert 'custom__bili_hot' not in names
    assert set(allowed) <= set(READONLY_TOOL_KEYS)


def test_model_cannot_escalate_via_tools_param(tmp_path):
    runner, _p, _e = _runner(tmp_path)      # 没配 extras
    spec = subagent.SubagentSpec(
        task='t', tools=('fs_write', 'mcp__pw__browser_snapshot',
                         'custom__bili_hot'))
    schemas, allowed = runner._tools_for(spec, dict(config.DEFAULTS), '1')
    assert schemas == []
    assert allowed == set()


def test_wanted_narrows_readonly_but_extras_still_added(tmp_path):
    cfg = dict(config.DEFAULTS)
    cfg['subagent_extra_tools'] = 'mcp__pw__browser_snapshot'
    runner, _p, _e = _runner(tmp_path, extra='mcp__pw__browser_snapshot')
    schemas, _allowed = runner._tools_for(
        subagent.SubagentSpec(task='t', tools=('fs_read',)), cfg, '1')
    names = {s['function']['name'] for s in schemas}
    assert 'fs_read' in names and 'fs_list' not in names
    assert 'mcp__pw__browser_snapshot' in names


# ── 端到端：下发 / 放行 / 拒绝 ───────────────────────────────

def test_run_lets_subagent_call_mcp_extra(tmp_path):
    script = [
        _ChatResult(tool_calls=[{'id': 'c1', 'name': 'mcp__pw__browser_snapshot',
                                 'arguments': {'url': 'https://example.com'}}]),
        _ChatResult('页面上有 3 条结果'),
    ]
    runner, plugin, eng = _runner(
        tmp_path, extra='mcp__pw__browser_*', script=script)
    out = run(runner.run(subagent.SubagentSpec(task='查页面'), gid='1'))
    assert out.error is None
    assert out.text == '页面上有 3 条结果'
    assert eng.calls and eng.calls[0]['name'] == 'mcp__pw__browser_snapshot'
    assert eng.calls[0]['role'] == 'readonly'
    assert 'mcp__pw__browser_snapshot' in eng.calls[0]['allow_tools']
    # 模型真的看到了这个工具
    assert 'mcp__pw__browser_snapshot' in plugin.llm.tool_names_seen[0]


def test_run_denies_unlisted_mcp_tool(tmp_path):
    script = [
        _ChatResult(tool_calls=[{'id': 'c1', 'name': 'mcp__pw__browser_click',
                                 'arguments': {}}]),
        _ChatResult('做不了'),
    ]
    runner, _p, eng = _runner(
        tmp_path, extra='mcp__pw__browser_snapshot', script=script)
    out = run(runner.run(subagent.SubagentSpec(task='点一下'), gid='1'))
    assert eng.calls == []                # 没派发出去
    assert out.error is None


def test_run_without_config_denies_mcp_and_custom(tmp_path):
    script = [
        _ChatResult(tool_calls=[
            {'id': 'c1', 'name': 'mcp__pw__browser_snapshot', 'arguments': {}},
            {'id': 'c2', 'name': 'custom__bili_hot', 'arguments': {}},
        ]),
        _ChatResult('都不行'),
    ]
    runner, plugin, eng = _runner(tmp_path, script=script)
    out = run(runner.run(subagent.SubagentSpec(task='x'), gid='1'))
    assert eng.calls == []
    assert out.error is None
    # 只读工具仍然是下发的
    assert 'fs_read' in plugin.llm.tool_names_seen[0]
    assert 'mcp__pw__browser_snapshot' not in plugin.llm.tool_names_seen[0]


# ── engine 门禁：readonly + allow_tools ─────────────────────

def _real_engine(tmp_path):
    db = str(tmp_path / 'eng.db')
    storage.migrate(db)
    plugin = _FakePlugin(db, dict(config.DEFAULTS))
    return engine.Engine(plugin)


def _ctx(eng, **kw):
    return builtin_tools.ToolContext(
        bot=None, group_id='123', sandbox=None, db_path=eng.p.db_path,
        cfg=eng.p.cfg, actor_role='readonly', **kw)


def test_engine_gate_allows_allowlisted_extra_tool(tmp_path):
    eng = _real_engine(tmp_path)
    ctx = _ctx(eng, allow_tools=frozenset({'mcp__pw__browser_snapshot'}))
    assert run(eng._risk_denied('mcp__pw__browser_snapshot', ctx,
                               eng.p.cfg)) is None
    # 未列的 MCP 工具仍被只读身份拦住
    denied = run(eng._risk_denied('mcp__pw__browser_click', ctx, eng.p.cfg))
    assert denied and '只读' in denied


def test_engine_gate_wildcard_allow_tools(tmp_path):
    eng = _real_engine(tmp_path)
    patterns = tool_batch.parse_tool_patterns('mcp__pw__browser_*')
    ctx = _ctx(eng, allow_tools=frozenset(patterns))
    # 允许名单里放的是通配模式时也一样命中
    assert run(eng._risk_denied('mcp__pw__browser_snapshot', ctx,
                               eng.p.cfg)) is None


def test_engine_gate_extra_tools_do_not_apply_to_members(tmp_path):
    eng = _real_engine(tmp_path)
    ctx = builtin_tools.ToolContext(
        bot=None, group_id='123', sandbox=None, db_path=eng.p.db_path,
        cfg=eng.p.cfg, sender_id='20002',
        allow_tools=frozenset({'mcp__pw__browser_snapshot'}))
    denied = run(eng._risk_denied('mcp__pw__browser_snapshot', ctx,
                                 eng.p.cfg))
    assert denied and '只有主人' in denied


def test_engine_gate_empty_allow_tools_is_readonly(tmp_path):
    eng = _real_engine(tmp_path)
    ctx = _ctx(eng)
    assert run(eng._risk_denied('fs_read', ctx, eng.p.cfg)) is None
    denied = run(eng._risk_denied('mcp__pw__browser_snapshot', ctx,
                                 eng.p.cfg))
    assert denied and '只读' in denied


def test_tool_ctx_carries_allow_tools(tmp_path):
    runner, _p, _e = _runner(tmp_path, extra='mcp__pw__browser_*')
    ctx = runner._tool_ctx('1', dict(config.DEFAULTS),
                           {'mcp__pw__browser_snapshot', 'mcp__pw__browser_snapshot'})
    assert ctx.actor_role == 'readonly'
    assert 'mcp__pw__browser_snapshot' in ctx.allow_tools
    # 旧调用（不传）仍是空集
    assert runner._tool_ctx('1', dict(config.DEFAULTS)).allow_tools == frozenset()


def test_schema_follows_group_overrides(tmp_path):
    """群开关关掉的工具不会因为 extras 命中而复活（schema 来自 _build_tools）。"""
    runner, plugin, eng = _runner(tmp_path, extra='mcp__pw__browser_*')
    eng.schemas = [s for s in _all_schemas()
                   if s['function']['name'] != 'mcp__pw__browser_click']
    schemas, _allowed = runner._tools_for(
        subagent.SubagentSpec(task='t'),
        config.merge_config({'subagent_extra_tools': 'mcp__pw__browser_*'}),
        '1')
    names = {s['function']['name'] for s in schemas}
    assert 'mcp__pw__browser_snapshot' in names
    assert 'mcp__pw__browser_click' not in names


def test_extras_cannot_open_builtin_write_tools(tmp_path):
    """extras 只放开 MCP/自定义工具；内置写/执行工具仍仅主人可用。"""
    cfg = dict(config.DEFAULTS)
    cfg['subagent_extra_tools'] = 'fs_write, code_python, qq_send_text, fs_write'
    runner, _p, _e = _runner(tmp_path, extra=cfg['subagent_extra_tools'])
    schemas, allowed = runner._tools_for(
        subagent.SubagentSpec(task='t'), cfg, '1')
    names = {s['function']['name'] for s in schemas}
    assert 'fs_write' not in names
    assert 'code_python' not in names
    assert 'qq_send_text' not in names
    assert 'fs.write' not in allowed and 'code.python' not in allowed
