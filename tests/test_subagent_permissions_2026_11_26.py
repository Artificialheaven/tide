# -*- coding: utf-8 -*-
"""子代理权限修复（2026-11-26）：至少保证只读工具可用。

背景（真实回归）：`config.DEFAULTS['permissions']` 里
`member_action='deny'` + `default_action='deny'` 非空，`engine._dispatch_impl`
对**每个**工具都会走 `_risk_denied()`；子代理的 ToolContext 没有 sender_id
→ `is_owner=False` → 角色 member → 角色默认 deny 命中，于是连 `fs.read` /
`history.query` 这种只读工具都被拒，表现为「子代理没有任何权限」。

修复语义：
- 角色默认（owner/admin/member_action）只约束**风险工具**，安全/只读工具
  对非主人默认放行（与 N-02 升级前的 risky_owner_only 语义一致）；
- 新增 `readonly` 角色：`ToolContext.actor_role='readonly'`（子代理填），
  只放行 `tool_batch.READONLY_TOOL_KEYS` 里的工具，写/发送一律拒绝；
- 显式 deny/allow/ask 规则仍然优先（规则是主人的显式意图）；
- engine `_risk_denied` 里对 readonly 再做一次纵深拦截，即使规则被写坏，
  非只读工具也过不去。

全部 <2s、不碰网络。
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'plugins', 'deepseek'))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Core import builtin_tools, config, engine, permissions, storage, subagent
from Core.tool_batch import READONLY_TOOL_KEYS


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakePlugin:
    """够 Engine / SubagentRunner 构造的最小 fake。"""

    def __init__(self, db_path=':memory:', cfg=None):
        self.db_path = db_path
        self.cfg = dict(cfg or {})
        self.bot = None
        self.live = None
        self.bot_id = 10000
        self.bot_name = '机器人'
        self.llm = None
        self.memory = None
        self.logged = []

    def log(self, msg, level='info'):
        self.logged.append((level, msg))

    def sandbox(self, gid):
        return None


def _new_engine(tmp_path, cfg=None):
    db = str(tmp_path / 'sub.db')
    storage.migrate(db)
    return engine.Engine(_FakePlugin(db, cfg))


def _defaults():
    # 真实默认配置：permissions 非空（member_action/default_action 都是 deny）
    return dict(config.DEFAULTS)


def _ctx(eng, *, sender_id='', role=''):
    return builtin_tools.ToolContext(
        bot=None, group_id='123', sandbox=None, db_path=eng.p.db_path,
        cfg=eng.p.cfg, sender_id=sender_id, actor_role=role)


# ── 权限引擎：角色默认只约束风险工具 ──────────────────────────

def test_default_config_member_keeps_safe_tools():
    perm = permissions.PermissionEngine(_defaults())

    def act(tool, role='member'):
        return perm.decide(tool_key=tool, args={}, actor_role=role,
                           gid='1').action

    # 安全/只读工具：普通成员依然可用（升级前行为）
    for tool in ('fs.read', 'fs.list', 'history.query', 'memory.recall',
                 'qq.member_info', 'time.now', 'message.read_image'):
        assert act(tool) == 'allow', tool
    # 风险工具：非主人仍然拒绝
    for tool in ('fs.write', 'fs.delete', 'code.python', 'qzone.publish',
                 'qzone.comment', 'qq.at_all', 'request.approve'):
        assert act(tool) == 'deny', tool
    # 未知 MCP 工具按风险处理
    assert act('mcp__srv__do') == 'deny'
    # 主人不受限
    assert act('fs.write', 'owner') == 'allow'


def test_role_default_deny_not_applied_to_safe_tools_only():
    """member_action=deny 只拦风险工具；显式 default_action 仍生效。"""
    cfg = {'permissions': {'member_action': 'deny', 'rules': []}}
    perm = permissions.PermissionEngine(cfg)
    assert perm.decide(tool_key='fs.read', args={}, actor_role='member',
                       gid='g').action == 'allow'
    assert perm.decide(tool_key='fs.write', args={}, actor_role='member',
                       gid='g').action == 'deny'
    # 没有 member_action、只有 default_action=deny：安全工具仍按显式默认拒绝
    cfg2 = {'permissions': {'default_action': 'deny', 'rules': []}}
    perm2 = permissions.PermissionEngine(cfg2)
    assert perm2.decide(tool_key='fs.read', args={}, actor_role='member',
                        gid='g').action == 'deny'


# ── 权限引擎：readonly 角色（子代理） ────────────────────────

def test_readonly_role_allows_only_readonly_tools():
    perm = permissions.PermissionEngine(_defaults())
    for tool in sorted(READONLY_TOOL_KEYS):
        assert perm.decide(tool_key=tool, args={}, actor_role='readonly',
                           gid='1').action == 'allow', tool
    # 模型函数名（fs_read）也要能判定
    assert perm.decide(tool_key='fs_read', args={},
                       actor_role='readonly', gid='1').action == 'allow'
    # 写/发送/外发类：拒绝（包括不属风险表但会外发的 qq.text）
    for tool in ('qq.text', 'qq.image', 'fs.write', 'fs.append', 'fs.delete',
                 'code.python', 'qzone.comment', 'qzone.publish',
                 'inject.add', 'custom.publish', 'mcp__srv__do'):
        assert perm.decide(tool_key=tool, args={}, actor_role='readonly',
                           gid='1').action == 'deny', tool


def test_readonly_role_deny_reason_mentions_readonly():
    perm = permissions.PermissionEngine(_defaults())
    d = perm.decide(tool_key='fs.write', args={}, actor_role='readonly',
                    gid='1')
    assert d.action == 'deny' and '只读' in d.reason


def test_readonly_and_member_honour_explicit_deny_rules():
    cfg = {'permissions': {
        'member_action': 'deny',
        'rules': [{'tool': 'fs.read', 'action': 'deny', 'reason': '禁读'}],
    }}
    perm = permissions.PermissionEngine(cfg)
    for role in ('member', 'readonly'):
        d = perm.decide(tool_key='fs.read', args={}, actor_role=role, gid='g')
        assert d.action == 'deny' and d.reason == '禁读'


# ── engine._risk_denied：真实门禁 ───────────────────────────

def test_engine_readonly_ctx_can_use_readonly_tools(tmp_path):
    eng = _new_engine(tmp_path, _defaults())
    ctx = _ctx(eng, role='readonly')

    async def go():
        return [await eng._risk_denied(name, ctx, eng.p.cfg)
                for name in ('fs_read', 'fs_list', 'history_query',
                             'recall_user', 'qzone_list', 'now_time')]

    assert run(go()) == [None] * 6


def test_engine_readonly_ctx_cannot_write_or_send(tmp_path):
    eng = _new_engine(tmp_path, _defaults())
    ctx = _ctx(eng, role='readonly')

    async def go(name):
        return await eng._risk_denied(name, ctx, eng.p.cfg)

    for name in ('fs_write', 'fs_delete', 'code_python', 'qq_send_text',
                 'qq_send_image', 'qzone_comment'):
        out = run(go(name))
        assert out and '只读' in out, (name, out)


def test_engine_readonly_guard_survives_broken_allow_rule(tmp_path):
    """即使配置给 readonly 写了 allow 规则，engine 也拦非只读工具。"""
    cfg = _defaults()
    cfg['permissions'] = dict(cfg['permissions'])
    cfg['permissions']['rules'] = [{'tool': 'qq.text', 'role': 'readonly',
                                    'action': 'allow'}]
    eng = _new_engine(tmp_path, cfg)
    ctx = _ctx(eng, role='readonly')
    out = run(eng._risk_denied('qq_send_text', ctx, eng.p.cfg))
    assert out and '只读' in out
    # 规则允许的只读工具照常放行
    assert run(eng._risk_denied('fs_read', ctx, eng.p.cfg)) is None


def test_engine_member_safe_tool_regression(tmp_path):
    """非主人（普通成员）的只读工具也要放行——之前被 member_action 全拒。"""
    eng = _new_engine(tmp_path, _defaults())
    ctx = _ctx(eng, sender_id='20002')
    assert ctx.is_owner is False
    assert run(eng._risk_denied('fs_read', ctx, eng.p.cfg)) is None
    assert run(eng._risk_denied('history_query', ctx, eng.p.cfg)) is None
    denied = run(eng._risk_denied('fs_write', ctx, eng.p.cfg))
    assert denied and '只有主人' in denied


def test_engine_owner_unaffected(tmp_path):
    eng = _new_engine(tmp_path, _defaults())
    owner = str(eng.p.cfg.get('owner_qq') or '3399752707')
    ctx = _ctx(eng, sender_id=owner)
    assert ctx.is_owner is True
    assert run(eng._risk_denied('fs_write', ctx, eng.p.cfg)) is None


# ── 子代理侧：ToolContext 必须标只读 ────────────────────────

def test_subagent_tool_ctx_marks_readonly(tmp_path):
    eng = _new_engine(tmp_path, _defaults())
    runner = subagent.SubagentRunner(eng)
    ctx = runner._tool_ctx('123', eng.p.cfg)
    assert ctx.actor_role == 'readonly'
    assert ctx.is_owner is False
    # 兜底构造路径（正常构造抛异常时）也必须带只读标记
    real = builtin_tools.ToolContext
    calls = {'n': 0}

    def boom_once(*a, **kw):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('boom')
        return real(*a, **kw)

    builtin_tools.ToolContext = boom_once
    try:
        ctx2 = runner._tool_ctx('123', eng.p.cfg)
    finally:
        builtin_tools.ToolContext = real
    assert calls['n'] == 2 and ctx2.actor_role == 'readonly'


def test_subagent_allowlist_still_readonly_only(tmp_path):
    eng = _new_engine(tmp_path, _defaults())
    runner = subagent.SubagentRunner(eng)
    names = {(s.get('function') or {}).get('name')
             for s in runner._schemas(())}
    keys = {subagent.tool_key_of(n) for n in names}
    assert keys and keys <= set(READONLY_TOOL_KEYS)
    assert 'fs.read' in keys
    # 显式要求写工具会被白名单过滤掉
    got = {(s.get('function') or {}).get('name')
           for s in runner._schemas(('fs_write', 'qzone_comment'))}
    assert got == set()


def test_engine_dispatch_uses_actor_role_helper(tmp_path):
    """HookCtx / 门禁都必须走统一的角色推断，避免 readonly 被当成 member。"""
    eng = _new_engine(tmp_path, _defaults())
    src = open(engine.__file__, encoding='utf-8').read()
    assert 'def _actor_role(' in src
    assert "actor_role=self._actor_role(tool_ctx)" in src
    ctx = _ctx(eng, role='readonly')
    assert eng._actor_role(ctx) == 'readonly'
    ctx2 = _ctx(eng, sender_id='20002')
    assert eng._actor_role(ctx2) == 'member'
    owner = str(eng.p.cfg.get('owner_qq') or '')
    assert eng._actor_role(_ctx(eng, sender_id=owner)) == 'owner'
