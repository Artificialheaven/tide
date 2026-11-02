# -*- coding: utf-8 -*-
"""deepseek 上下文治理：硬截尾 plan_tail、summary_mode 配置校验、
摘要锚点单调更新（防止旧快照把 covered_up_to_id 写小导致摘要风暴）。"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'plugins', 'deepseek'))

from Core import context, config, storage  # noqa: E402


def _rows(n, start=1):
    return [{'id': i, 'role': 'user', 'content': f'消息{i}',
             'tool_calls': '', 'tool_call_id': '', 'name': ''}
            for i in range(start, start + n)]


# ── plan_tail ──────────────────────────────────────────────────────

def test_plan_tail_no_cut_when_within_keep():
    rows = _rows(10)
    plan = context.plan_tail(rows, 30, 6000)
    assert plan['cut'] == 0
    assert plan['window'] is rows


def test_plan_tail_keeps_last_x_and_window_is_suffix():
    rows = _rows(50)
    plan = context.plan_tail(rows, 30, 10 ** 9)
    assert plan['cut'] == 20
    assert plan['window'] == rows[20:]
    assert plan['window'][0]['id'] == rows[plan['cut']]['id']


def test_plan_tail_char_budget_drops_from_oldest():
    rows = _rows(20)
    plan = context.plan_tail(rows, 30, 30)  # 每条 ~4 字，预算只够几条
    assert plan['cut'] > 0
    ids = [r['id'] for r in rows[plan['cut']:]]
    assert [r['id'] for r in plan['window']] == ids
    assert sum(context._content_len(r) for r in plan['window']) <= 30


def test_plan_tail_never_starts_with_orphan_tool_row():
    rows = _rows(31)
    rows[-30] = {'id': rows[-30]['id'], 'role': 'tool', 'content': '结果',
                 'tool_calls': '', 'tool_call_id': 'x', 'name': 'now_time'}
    plan = context.plan_tail(rows, 30, 10 ** 9)
    assert plan['window'][0]['role'] != 'tool'
    assert plan['window'] == rows[plan['cut']:]


def test_plan_tail_empty():
    assert context.plan_tail([], 30, 6000) == {'cut': 0, 'window': []}


# ── summary_mode 配置 ─────────────────────────────────────────────

def test_merge_config_accepts_tail_mode_and_coerces_keep():
    cfg = config.merge_config(
        {'summary_mode': 'tail', 'tail_keep_msgs': '12'})
    assert cfg['summary_mode'] == 'tail'
    assert cfg['tail_keep_msgs'] == 12


def test_merge_config_rejects_unknown_mode():
    cfg = config.merge_config({'summary_mode': 'bogus'})
    assert cfg['summary_mode'] == 'summary'


def test_resolve_group_overrides_mode_and_rejects_bogus():
    base = config.merge_config({})
    eff = config.resolve_group(base, {'summary_mode': 'tail',
                                      'tail_keep_msgs': 8})
    assert eff['summary_mode'] == 'tail'
    assert eff['tail_keep_msgs'] == 8
    bad = config.resolve_group(base, {'summary_mode': 'nope'})
    assert bad['summary_mode'] == base['summary_mode']


def test_resolve_group_drops_non_overridable():
    base = config.merge_config({})
    eff = config.resolve_group(base, {'summary_force_msgs': 1})
    assert eff['summary_force_msgs'] == base['summary_force_msgs']


# ── 摘要锚点单调更新 ──────────────────────────────────────────────

def _db(tmp_path):
    p = os.path.join(str(tmp_path), 't.db')
    storage.migrate(p)
    return p


def test_advance_summary_anchor_insert_and_monotonic(tmp_path):
    p = _db(tmp_path)
    assert storage.advance_summary_anchor(p, 'g', 10) is True
    row = storage.get_summary(p, 'g')
    assert row['covered_up_to_id'] == 10
    assert row['summary'] == ''
    # 旧快照不得把锚点写小
    assert storage.advance_summary_anchor(p, 'g', 5) is False
    assert storage.get_summary(p, 'g')['covered_up_to_id'] == 10
    assert storage.advance_summary_anchor(p, 'g', 20) is True
    assert storage.get_summary(p, 'g')['covered_up_to_id'] == 20


def test_advance_summary_monotonic_same_persona(tmp_path):
    p = _db(tmp_path)
    assert storage.advance_summary(p, 'g', '摘要A', 100, 'hashA') is True
    assert storage.advance_summary(p, 'g', '旧摘要', 50, 'hashA') is False
    row = storage.get_summary(p, 'g')
    assert row['covered_up_to_id'] == 100
    assert row['summary'] == '摘要A'
    assert storage.advance_summary(p, 'g', '摘要B', 120, 'hashA') is True
    assert storage.get_summary(p, 'g')['summary'] == '摘要B'


def test_advance_summary_allows_persona_change(tmp_path):
    p = _db(tmp_path)
    storage.advance_summary(p, 'g', '摘要A', 100, 'hashA')
    # 人设变化：锚点允许回退重算
    assert storage.advance_summary(p, 'g', '新摘要', 80, 'hashB') is True
    row = storage.get_summary(p, 'g')
    assert row['covered_up_to_id'] == 80
    assert row['persona_hash'] == 'hashB'
