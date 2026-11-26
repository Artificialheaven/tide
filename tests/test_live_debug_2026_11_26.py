"""2026-11-26 实时调试台：投递会话 + 历史事件回看的回归测试。

覆盖：
1. LiveBus.older 的游标/过滤语义（「加载更早」的底座）；
2. /live/api/state 里把固定投递会话（空间知会）补进群列表并打标；
3. /live/api/events 的分页（before 游标、has_more、按群/类型过滤）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'plugins', 'deepseek'))

from Core import storage  # noqa: E402
from Core.live import LiveManager  # noqa: E402
from Core.live_bus import LiveBus, Subscription  # noqa: E402


def _tmp_db() -> str:
    db = os.path.join(tempfile.mkdtemp(), 'live.db')
    storage.migrate(db)
    return db


class _FakePlugin:
    def __init__(self, db=None, cfg=None):
        self.db_path = db or _tmp_db()
        self.cfg = dict(cfg or {})
        self.bot_id = '10001'
        self.bot_name = '大肥鱼'
        self.logs = []

    def log(self, msg, level='info'):
        self.logs.append((level, str(msg)))


def _mgr(cfg=None) -> LiveManager:
    p = _FakePlugin(cfg=cfg)
    return LiveManager(p, log=lambda *a, **k: None)


# ── 1. 总线回看 ─────────────────────────────────────────────────

def test_bus_older_ascending_and_group_filter():
    bus = LiveBus()
    for i in range(6):
        bus.publish('chat', group_id='a' if i % 2 == 0 else 'b', text=f'm{i}')
    sub = Subscription({'a'}, set(), False)
    evs = bus.older(0, 10, sub)                  # 最新一批（只看 a）
    assert [e['text'] for e in evs] == ['m0', 'm2', 'm4']
    assert bus.oldest_seq() == 1 and bus.latest_seq() == 6
    # before 游标：取 seq < 5 的
    older = bus.older(5, 10, sub)
    assert all(e['seq'] < 5 for e in older)
    assert [e['text'] for e in older] == ['m0', 'm2']
    # limit 生效，且保留的是离游标最近的一批
    assert [e['text'] for e in bus.older(0, 2, sub)] == ['m2', 'm4']


def test_bus_older_kind_filter_and_empty_ring():
    bus = LiveBus()
    sub = Subscription(set(), {'gate'}, False)
    assert bus.older(0, 10, sub) == []
    bus.publish('chat', group_id='a')
    bus.publish('gate', group_id='a', stage='judge', reason='不相关')
    evs = bus.older(0, 10, sub)
    assert len(evs) == 1 and evs[0]['kind'] == 'gate'


# ── 2. 投递会话进群列表 ─────────────────────────────────────────

def test_state_payload_adds_private_delivery_session():
    plugin = _FakePlugin(cfg={'owner_qq': '10001', 'qzone_notify_group': ''})
    mgr = LiveManager(plugin, log=lambda *a, **k: None)
    data = mgr.state_payload()
    assert data['delivery']['group_id'] == '10001'
    assert data['delivery']['chat_type'] == 'private'
    gids = [g['group_id'] for g in data['groups']]
    assert '10001' in gids
    entry = [g for g in data['groups'] if g['group_id'] == '10001'][0]
    assert entry['delivery'] is True and entry['chat_type'] == 'private'


def test_state_payload_marks_group_delivery_session():
    db = _tmp_db()
    storage.get_or_create_group(db, '777')
    plugin = _FakePlugin(db=db, cfg={'owner_qq': '10001',
                                     'qzone_notify_group': '777'})
    mgr = LiveManager(plugin, log=lambda *a, **k: None)
    data = mgr.state_payload()
    assert data['delivery']['group_id'] == '777'
    assert data['delivery']['chat_type'] == 'group'
    entry = [g for g in data['groups'] if g['group_id'] == '777'][0]
    assert entry['delivery'] is True
    # 已有群行不被重复追加
    assert len([g for g in data['groups'] if g['group_id'] == '777']) == 1


def test_state_payload_without_delivery_target():
    plugin = _FakePlugin(cfg={'owner_qq': '', 'qzone_notify_group': ''})
    mgr = LiveManager(plugin, log=lambda *a, **k: None)
    assert mgr.state_payload()['delivery'] is None


# ── 3. 历史事件分页 ─────────────────────────────────────────────

def test_history_payload_paging_by_before_cursor():
    mgr = _mgr()
    for i in range(10):
        mgr.bus.publish('chat', group_id='777', text=f'm{i}')
    page1 = mgr.history_payload(groups=['777'], limit=4)
    assert [e['text'] for e in page1['events']] == ['m6', 'm7', 'm8', 'm9']
    assert page1['has_more'] is True

    page2 = mgr.history_payload(groups=['777'], limit=4,
                                before=page1['events'][0]['seq'])
    assert [e['text'] for e in page2['events']] == ['m2', 'm3', 'm4', 'm5']
    assert page2['has_more'] is True

    page3 = mgr.history_payload(groups=['777'], limit=4,
                                before=page2['events'][0]['seq'])
    assert [e['text'] for e in page3['events']] == ['m0', 'm1']
    assert page3['has_more'] is False          # 已到 ring 最早


def test_history_payload_group_filter_and_limit_clamp():
    mgr = _mgr()
    for i in range(5):
        mgr.bus.publish('chat', group_id='777' if i < 3 else '888',
                        text=f'm{i}')
    only = mgr.history_payload(groups=['888'], limit=999)
    assert [e['text'] for e in only['events']] == ['m3', 'm4']
    assert only['has_more'] is False


def test_history_payload_empty_ring():
    mgr = _mgr()
    data = mgr.history_payload(before=0, limit=200)
    assert data['events'] == [] and data['has_more'] is False
    assert data['oldest_seq'] == 0 and data['latest_seq'] == 0
