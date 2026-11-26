"""N-01..N-05 增强模块的单元测试（不依赖网络/真实模型）。"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'plugins', 'deepseek'))

from Core import tool_batch  # noqa: E402
from Core import permissions as perms  # noqa: E402
from Core import hooks  # noqa: E402
from Core import approval as approval_mod  # noqa: E402
from Core import subagent as subagent_mod  # noqa: E402


def test_batch_serial_fallback_preserves_order():
    calls = [{'id': 'a', 'name': 'fs_read', 'arguments': {}},
             {'id': 'b', 'name': 'fs_list', 'arguments': {}}]
    seen = []

    async def dispatch(norm):
        seen.append(norm['id'])
        await asyncio.sleep(0.01)
        return f'r-{norm["id"]}'

    cfg = {'tool_parallel_enabled': False}
    out = asyncio.run(tool_batch.run_batch(dispatch, calls, cfg=cfg, gid='g'))
    assert [r[0]['id'] for r in out.results] == ['a', 'b']
    assert [r[1] for r in out.results] == ['r-a', 'r-b']


def test_batch_parallel_readonly_groups():
    calls = [{'id': str(i), 'name': 'fs_read', 'arguments': {}}
             for i in range(4)]
    live = {'now': 0, 'peak': 0}

    async def dispatch(norm):
        live['now'] += 1
        live['peak'] = max(live['peak'], live['now'])
        await asyncio.sleep(0.02)
        live['now'] -= 1
        return f'r-{norm["id"]}'

    cfg = {'tool_parallel_enabled': True, 'tool_parallel_max': 4}
    out = asyncio.run(tool_batch.run_batch(dispatch, calls, cfg=cfg, gid='g'))
    assert live['peak'] >= 2
    assert [r[0]['id'] for r in out.results] == ['0', '1', '2', '3']


def test_batch_write_barrier_short_circuits():
    calls = [{'id': 'w', 'name': 'fs_write', 'arguments': {}},
             {'id': 'w2', 'name': 'fs_append', 'arguments': {}}]
    ran = []

    async def dispatch(norm):
        ran.append(norm['id'])
        if norm['id'] == 'w':
            return '[拒绝] nope'
        return 'ok'

    out = asyncio.run(tool_batch.run_batch(dispatch, calls, cfg={}, gid='g'))
    assert ran == ['w']
    assert out.results[1][1].startswith('[跳过]')


def test_permissions_owner_member_ask():
    eng = perms.PermissionEngine({'owner_qq': '1', 'risky_owner_only': True})
    assert eng.decide(tool_key='fs.write', args={}, actor_role='owner',
                      gid='g', chat_type='group').action == 'allow'
    assert eng.decide(tool_key='fs.write', args={}, actor_role='member',
                      gid='g', chat_type='group').action == 'deny'
    cfg = {'permissions': {'default_action': 'ask',
                           'ask': {'enabled': True}, 'rules': []}}
    eng2 = perms.PermissionEngine(cfg)
    assert eng2.decide(tool_key='fs.read', args={}, actor_role='member',
                       gid='g', chat_type='group').action == 'ask'


def test_hooks_block_mutate_priority():
    hooks.unregister()

    async def blocker(ctx):
        return hooks.HookVerdict(block='no')

    async def rewrite(ctx):
        return hooks.HookVerdict(mutate={'result': 'x'})

    hooks.register('before_tool', blocker, name='b')
    hooks.register('after_tool', rewrite, name='a')
    ctx = hooks.HookCtx(point='before_tool', gid='g', cfg={})
    v = asyncio.run(hooks.emit('before_tool', ctx))
    assert v.block == 'no'
    ctx2 = hooks.HookCtx(point='after_tool', gid='g', cfg={}, result='y')
    v2 = asyncio.run(hooks.emit('after_tool', ctx2))
    assert v2.mutate['result'] == 'x'
    hooks.unregister()


def test_subagent_result_marker_and_truncate():
    res = subagent_mod.SubagentResult(text='原' * 3000)
    out = subagent_mod.format_result(res)
    assert out.startswith(subagent_mod.RESULT_MARK)
    assert len(out) < 2200


def test_approval_preview_masks_and_decide():
    prev = approval_mod._preview({'api_key': 'secret', 'x': 1})
    assert 'secret' not in prev and '***' in prev
    from Core import storage
    db = os.path.join(tempfile.mkdtemp(), 't.db')
    storage.migrate(db)
    aid = storage.create_approval(
        db, gid='g', chat_type='group', actor_id='u', actor_name='U',
        tool_key='fs.write', args_json='{}', args_preview='{}')['id']
    assert aid
    assert storage.decide_approval(db, aid, 'approved', 'owner')
    assert not storage.decide_approval(db, aid, 'rejected', 'owner')
    row = storage.get_approval(db, aid)
    assert row['status'] == 'approved'
