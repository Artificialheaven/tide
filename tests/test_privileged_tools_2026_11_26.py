# -*- coding: utf-8 -*-
"""AI 自主执行模式（privileged_tools_on，2026-11-26）。

需求（用户原话）：「我希望 AI 可以在对其他各种工具具备较高权限，包括在每个
群的工作区内的读写操作和代码运行操作。」

语义：
- 默认关闭：风险工具（写/删工作区文件、代码执行、发布说说、批准好友申请、
  MCP 远程桌面……）仍然只有主人本人指令才能触发；
- 打开后（全局或按群）：风险工具不再要求「主人本人在场」，群里任何成员触发
  的对话轮都能让 AI 自己写文件 / 跑代码 / 发说说；
- 显式 deny / ask 规则依旧优先（规则是主人的显式意图），ask 未开启时仍然拒绝；
- 子代理（readonly 身份）不受影响，仍是只读；它的额外工具由
  subagent_extra_tools 单独授权；
- 每次「非主人 + 风险工具 + 自主模式」放行都会打一条 warn 审计日志。

全部 < 2s，不碰网络、不执行真实代码（只走权限门禁）。
"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'plugins', 'deepseek'))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio

from Core import builtin_tools, config, engine, permissions, storage


RISKY_SAMPLES = ('fs.write', 'fs.append', 'fs.delete', 'code.python',
                 'code.shell', 'qzone.publish', 'qq.at_all', 'request.approve',
                 'mcp__playwright__browser_evaluate')
SAFE_SAMPLES = ('fs.read', 'fs.list', 'history.query', 'memory.recall',
                'qq.member_info', 'time.now')


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakePlugin:
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


def _cfg(**kw):
    cfg = dict(config.DEFAULTS)
    cfg.update(kw)
    return cfg


def _new_engine(tmp_path, cfg=None):
    db = str(tmp_path / 'priv.db')
    storage.migrate(db)
    return engine.Engine(_FakePlugin(db, cfg or _cfg()))


def _ctx(eng, *, sender_id='20002', role=''):
    return builtin_tools.ToolContext(
        bot=None, group_id='123', sandbox=None, db_path=eng.p.db_path,
        cfg=eng.p.cfg, sender_id=sender_id, actor_role=role)


# ── 配置层 ──────────────────────────────────────────────

def test_config_key_defaults_off_and_group_overridable():
    assert config.DEFAULTS['privileged_tools_on'] is False
    assert 'privileged_tools_on' in config._BOOL_KEYS
    assert 'privileged_tools_on' in config.GROUP_OVERRIDABLE
    assert config.merge_config({'privileged_tools_on': 'true'})[
        'privileged_tools_on'] is True
    merged = config.merge_config({'privileged_tools_on': True})
    assert config.resolve_group(merged, {'privileged_tools_on': 'false'})[
        'privileged_tools_on'] is False


def test_housekeeping_keys_are_ints_not_bools():
    """回归：五个整数键曾被误挂 _BOOL_KEYS，用户配的数字被吞掉回默认。"""
    for key in ('mood_every_rounds', 'housekeep_interval', 'messages_keep',
                'usage_keep_days', 'qzone_events_keep_days'):
        assert key in config._INT_KEYS, key
        assert key not in config._BOOL_KEYS, key
    merged = config.merge_config({'housekeep_interval': 3600,
                                  'messages_keep': '500',
                                  'mood_every_rounds': '2',
                                  'usage_keep_days': '30'})
    assert merged['housekeep_interval'] == 3600
    assert merged['messages_keep'] == 500
    assert merged['mood_every_rounds'] == 2
    assert merged['usage_keep_days'] == 30


# ── 权限引擎 ────────────────────────────────────────────

def test_default_off_keeps_risky_tools_owner_only():
    perm = permissions.PermissionEngine(_cfg())
    for tool in RISKY_SAMPLES:
        assert perm.decide(tool_key=tool, args={}, actor_role='member',
                           gid='1').action == 'deny', tool
    for tool in SAFE_SAMPLES:
        assert perm.decide(tool_key=tool, args={}, actor_role='member',
                           gid='1').action == 'allow', tool


def test_on_allows_member_risky_and_safe_tools():
    perm = permissions.PermissionEngine(_cfg(privileged_tools_on=True))
    for tool in RISKY_SAMPLES + SAFE_SAMPLES:
        d = perm.decide(tool_key=tool, args={}, actor_role='member', gid='1')
        assert d.action == 'allow', (tool, d)
    assert '自主执行' in d.reason


def test_on_still_denies_explicit_deny_rule():
    cfg = _cfg(privileged_tools_on=True)
    cfg['permissions'] = dict(cfg['permissions'])
    cfg['permissions']['rules'] = [
        {'tool': 'code.*', 'action': 'deny', 'reason': '这台机器不许跑代码'},
    ]
    perm = permissions.PermissionEngine(cfg)
    d = perm.decide(tool_key='code.python', args={}, actor_role='member',
                    gid='1')
    assert d.action == 'deny' and d.reason == '这台机器不许跑代码'
    # 其它风险工具仍放行
    assert perm.decide(tool_key='fs.write', args={}, actor_role='member',
                       gid='1').action == 'allow'


def test_on_ask_rule_still_applies():
    cfg = _cfg(privileged_tools_on=True)
    cfg['permissions'] = dict(cfg['permissions'])
    ask_on = dict(cfg['permissions'].get('ask') or {})
    ask_on['enabled'] = True
    cfg['permissions']['ask'] = ask_on
    cfg['permissions']['rules'] = [
        {'tool': 'qzone.publish', 'action': 'ask', 'reason': '发说说要确认'},
    ]
    perm = permissions.PermissionEngine(cfg)
    d = perm.decide(tool_key='qzone.publish', args={}, actor_role='member',
                    gid='1')
    assert d.action == 'ask' and d.reason == '发说说要确认'
    # 审批中心没开：ask 降级为拒绝（不会被自主模式绕过）
    cfg2 = _cfg(privileged_tools_on=True)
    cfg2['permissions'] = dict(cfg2['permissions'])
    ask_off = dict(cfg2['permissions'].get('ask') or {})
    ask_off['enabled'] = False
    cfg2['permissions']['ask'] = ask_off
    cfg2['permissions']['rules'] = [{'tool': 'qzone.publish', 'action': 'ask'}]
    perm2 = permissions.PermissionEngine(cfg2)
    assert perm2.decide(tool_key='qzone.publish', args={},
                        actor_role='member', gid='1').action == 'deny'


def test_on_does_not_grant_readonly_subagent_write():
    perm = permissions.PermissionEngine(_cfg(privileged_tools_on=True))
    for tool in ('fs.write', 'code.python', 'qzone.publish'):
        d = perm.decide(tool_key=tool, args={}, actor_role='readonly', gid='1')
        assert d.action == 'deny', tool
    # 只读工具照常
    assert perm.decide(tool_key='fs.read', args={}, actor_role='readonly',
                       gid='1').action == 'allow'


# ── engine 门禁 + 审计日志 ──────────────────────────────

def test_engine_allows_workspace_write_and_code_for_member(tmp_path):
    eng = _new_engine(tmp_path, _cfg(privileged_tools_on=True))
    ctx = _ctx(eng)
    assert ctx.is_owner is False

    async def go(name):
        return await eng._risk_denied(name, ctx, eng.p.cfg)

    for name in ('fs_write', 'fs_append', 'fs_delete', 'code_run_python',
                 'code_run_shell', 'qzone_publish'):
        assert run(go(name)) is None, name
    # 审计日志留痕
    warns = [m for lvl, m in eng.p.logged if lvl == 'warn' and '[权限]' in m]
    assert warns and all('AI 自主执行' in w for w in warns)
    assert any('fs.write' in w for w in warns)


def test_engine_off_still_denies_and_logs_nothing(tmp_path):
    eng = _new_engine(tmp_path, _cfg())
    ctx = _ctx(eng)

    async def go(name):
        return await eng._risk_denied(name, ctx, eng.p.cfg)

    for name in ('fs_write', 'code_run_python'):
        out = run(go(name))
        assert out and '只有主人' in out, (name, out)
    assert not [m for lvl, m in eng.p.logged if '[权限]' in m]


def test_engine_no_audit_log_for_owner(tmp_path):
    eng = _new_engine(tmp_path, _cfg(privileged_tools_on=True))
    owner = str(eng.p.cfg.get('owner_qq') or '')
    ctx = _ctx(eng, sender_id=owner)
    assert ctx.is_owner is True
    assert run(eng._risk_denied('fs_write', ctx, eng.p.cfg)) is None
    assert not [m for lvl, m in eng.p.logged if '[权限]' in m]


def test_engine_group_override_off_keeps_owner_only(tmp_path):
    """群级覆盖关掉时，该群的风险工具仍然只认主人。"""
    eng = _new_engine(tmp_path, _cfg(privileged_tools_on=True))
    group_cfg = config.resolve_group(eng.p.cfg, {'privileged_tools_on': 'false'})
    ctx = _ctx(eng)
    out = run(eng._risk_denied('fs_write', ctx, group_cfg))
    assert out and '只有主人' in out
    # 全局仍开着
    assert run(eng._risk_denied('fs_write', ctx, eng.p.cfg)) is None


def test_engine_custom_owner_only_tool_respects_privileged(tmp_path):
    """自定义工具的 owner_only_call 在自主模式下放开（源码级约束校验）。"""
    src = open(engine.__file__, encoding='utf-8').read()
    assert "and not cfg.get('privileged_tools_on')" in src


# ── 提示词：模型也要知道门禁放开了 ────────────────────────

def test_system_prompt_mentions_autonomy_mode_only_when_on():
    from Core import prompts as prompts_mod
    catalog = '· fs_write 写文件'
    on = engine.Engine._system_prompt(
        '人设', _cfg(privileged_tools_on=True), has_tools=True,
        catalog=catalog)
    assert 'AI 自主执行' in on['content']
    off = engine.Engine._system_prompt(
        '人设', _cfg(), has_tools=True, catalog=catalog)
    assert 'AI 自主执行' not in off['content']


def test_catalog_drops_owner_only_tag_in_privileged_mode():
    from Core import prompts as prompts_mod
    items = [('generic_add', '加法', True)]
    normal = prompts_mod.build_catalog({'now_time'}, items, False)
    assert '仅主人可触发' in normal
    priv = prompts_mod.build_catalog({'now_time'}, items, False,
                                     privileged=True)
    assert '仅主人可触发' not in priv
    assert 'generic_add' in priv


def test_web_schema_and_group_page_expose_switch():
    import json
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    web = json.load(open(os.path.join(root, 'plugins', 'deepseek', 'web.json'),
                         encoding='utf-8'))
    entry = web['config']['privileged_tools_on']
    assert entry['type'] == 'checkbox' and entry['default'] is False
    page = open(os.path.join(root, 'plugins', 'deepseek', 'templates',
                             'group.html'), encoding='utf-8').read()
    assert 'name="privileged_tools_on"' in page
    assert "'privileged_tools_on'" in page
