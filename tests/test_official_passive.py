# -*- coding: utf-8 -*-
"""被动回复池：粘住 msg_id、同用户优先、关池不借用。

从仓库根运行：python -m pytest tests/test_official_passive.py
"""
from core.official.passive import PassivePool


def _fill(pool, gid, specs):
    """specs: [(msg_id, user_id)] 顺序入池。"""
    for mid, uid in specs:
        pool.add(gid, mid, user_id=uid)


def test_single_slot_exhausted_after_max_seq():
    pool = PassivePool(window_seconds=295, max_seq=5)
    pool.add('g', 'm1', user_id='u1')
    seqs = [pool.reserve('g').seq for _ in range(5)]
    assert seqs == [1, 2, 3, 4, 5]
    assert pool.reserve('g') is None


def test_sticky_does_not_roam_until_exhausted():
    # 池内有两条消息时，前 5 次必须粘在同一条上，不能 FIFO 轮换
    pool = PassivePool(max_seq=5)
    _fill(pool, 'g', [('m1', 'u1'), ('m2', 'u2')])
    used = [pool.reserve('g').slot.msg_id for _ in range(5)]
    assert used == ['m1'] * 5 or used == ['m2'] * 5
    # 第 6 次才换另一条
    sixth = pool.reserve('g')
    assert sixth is not None
    assert sixth.slot.msg_id != used[0]


def test_default_anchor_is_newest_sender():
    pool = PassivePool(max_seq=5)
    _fill(pool, 'g', [('m1', 'u1'), ('m2', 'u2')])
    # 无偏好：锚定最新入站消息（= 本轮触发者）
    assert pool.reserve('g').slot.msg_id == 'm2'


def test_prefer_same_user_oldest_expiring_first():
    pool = PassivePool(max_seq=5)
    _fill(pool, 'g', [
        ('m1', 'u1'), ('m2', 'u2'), ('m3', 'u1'), ('m4', 'u2')])
    # 显式锚定 u1：先烧 u1 自己最快过期的（m1），再 m3
    r = pool.reserve('g', prefer_user_id='u1')
    assert r.slot.msg_id == 'm1'
    for _ in range(4):
        pool.reserve('g', prefer_user_id='u1')
    r = pool.reserve('g', prefer_user_id='u1')
    assert r.slot.msg_id == 'm3'


def test_explicit_msg_id_preferred_and_sticky_after():
    pool = PassivePool(max_seq=5)
    _fill(pool, 'g', [('m1', 'u1'), ('m2', 'u2'), ('m3', 'u1')])
    r = pool.reserve('g', prefer_msg_id='m3')
    assert r.slot.msg_id == 'm3'
    # 后续不带偏好也粘在 m3
    assert pool.reserve('g').slot.msg_id == 'm3'


def test_unknown_explicit_msg_id_never_borrows():
    pool = PassivePool(max_seq=5, pool_on=True)
    pool.add('g', 'm1', user_id='u1')
    assert pool.reserve('g', prefer_msg_id='nope') is None


def test_pool_off_no_borrowing_other_messages():
    pool = PassivePool(max_seq=5, pool_on=False)
    _fill(pool, 'g', [('m1', 'u1'), ('m2', 'u2'), ('m3', 'u3')])
    # 显式锚定 m3：5 条用完后即使别人有名额也不借
    used = [pool.reserve('g', prefer_msg_id='m3',
                         prefer_user_id='u3').slot.msg_id
            for _ in range(5)]
    assert used == ['m3'] * 5
    assert pool.reserve('g', prefer_msg_id='m3',
                        prefer_user_id='u3') is None


def test_pool_off_proactive_reanchors_newest_once():
    pool = PassivePool(max_seq=5, pool_on=False)
    _fill(pool, 'g', [('m1', 'u1'), ('m2', 'u2')])
    # 无触发消息的发送（如入群问候）：锚定最新一条，之后粘住
    first = pool.reserve('g')
    assert first.slot.msg_id == 'm2'
    assert pool.reserve('g').slot.msg_id == 'm2'


def test_invalidate_clears_sticky_and_falls_back_pool_on():
    pool = PassivePool(max_seq=5, pool_on=True)
    _fill(pool, 'g', [('m1', 'u1'), ('m2', 'u2')])
    r = pool.reserve('g', prefer_msg_id='m1')
    assert r.slot.msg_id == 'm1'
    pool.invalidate('g', 'm1')
    fallback = pool.reserve('g')
    assert fallback is not None
    assert fallback.slot.msg_id == 'm2'


def test_expired_anchor_switches_after_prune():
    t = {'now': 1000.0}
    pool = PassivePool(window_seconds=295, max_seq=5,
                       clock=lambda: t['now'])
    pool.add('g', 'm1', user_id='u1')
    assert pool.reserve('g').slot.msg_id == 'm1'
    t['now'] += 300
    pool.add('g', 'm2', user_id='u1')
    r = pool.reserve('g')
    assert r.slot.msg_id == 'm2'


def test_user_id_backfilled_on_duplicate_add():
    pool = PassivePool(max_seq=5)
    pool.add('g', 'm1')
    pool.add('g', 'm1', user_id='u1')
    slots = pool.snapshot('g')
    assert len(slots) == 1
    assert slots[0].user_id == 'u1'


def test_available_counts_remaining_quota():
    pool = PassivePool(max_seq=5)
    pool.add('g', 'm1', user_id='u1')
    pool.add('g', 'm2', user_id='u2')
    assert pool.available('g') == 10
    pool.reserve('g', prefer_msg_id='m1')
    assert pool.available('g') == 9


def test_lock_serializes_per_group():
    pool = PassivePool(max_seq=5)
    lock = pool.lock('g')
    assert pool.lock('g') is lock
    assert pool.lock('h') is not lock
