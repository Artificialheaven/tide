"""2026-11-26 内存审计 P1-3 / P1-1 的回归测试：只增缓存加 LRU / 惰性 TTL。

覆盖：
1. `mentions.MemberDirectory._hot`：每群/全局 LRU 淘汰、最近使用的保留；
2. `mentions.MemberInfoDirectory._cache/_failed`：惰性过期清扫 + 总条数上限；
3. `memory.MemoryManager._cache`：按 user_id 的 LRU（超上限淘汰并释放索引引用）；
4. `qzone_watch.QzoneWatcher`：每轮重读配置（间隔改配置立即生效）、
   低频调用 `storage.prune_qzone_state` 清 `ccount:<tid>` 孤儿键。

全部用假 Bot / monkeypatch，不碰网络、不依赖真实 WS。
"""
import asyncio
import os
import sys
import tempfile
import threading
import time
import weakref

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'plugins', 'deepseek'))

from Core import memory as memory_mod  # noqa: E402
from Core import mentions  # noqa: E402
from Core import qzone_watch  # noqa: E402
from Core import storage  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _tmp_db() -> str:
    db = os.path.join(tempfile.mkdtemp(), 'audit.db')
    storage.migrate(db)
    return db


# ── 1. MemberDirectory._hot LRU ─────────────────────────────────


def test_member_hot_per_group_lru_evicts_oldest_keeps_recent():
    d = mentions.MemberDirectory(hot_per_group=3, hot_global=100)
    d.observe('g1', '10001', '甲')
    d.observe('g1', '10002', '乙')
    d.observe('g1', '10003', '丙')
    # 重复观察不改 QQ，只刷新最近使用序：乙最旧、甲最新
    d.observe('g1', '10001', '甲')
    d.observe('g1', '10004', '丁')
    assert set(d._hot['g1']) == {'甲', '丙', '丁'}
    assert d._hot['g1']['甲'] == '10001'
    assert d._hot_count == 3
    # 全局序里也不残留被淘汰的键
    assert ('g1', '乙') not in d._hot_order


def test_member_hot_global_cap_evicts_across_groups():
    d = mentions.MemberDirectory(hot_per_group=10, hot_global=4)
    d.observe('g1', '10001', 'a')
    d.observe('g1', '10002', 'b')
    d.observe('g2', '10003', 'c')
    d.observe('g2', '10004', 'd')
    assert d._hot_count == 4
    d.observe('g1', '10001', 'a')      # a 变最近使用 → b 成全局最旧
    d.observe('g3', '10005', 'e')      # 触发全局淘汰 b
    assert d._hot_count == 4
    assert 'b' not in d._hot.get('g1', {})
    assert d._hot['g1']['a'] == '10001'
    assert d._hot['g3']['e'] == '10005'
    # 被清空的群条目整体移除，不留下空 dict
    d2 = mentions.MemberDirectory(hot_per_group=10, hot_global=2)
    d2.observe('g1', '10001', 'a')
    d2.observe('g2', '10002', 'b')
    d2.observe('g3', '10003', 'c')
    assert 'g1' not in d2._hot
    assert d2._hot_count == 2


def test_member_hot_resolve_keeps_observed_names():
    db = _tmp_db()
    d = mentions.MemberDirectory(hot_per_group=10, hot_global=100)
    d.observe('777', '20002', '小明')
    out = _run(d.resolve('777', object(), db))   # 假 bot：无 RPC 方法
    assert out.get('小明') == '20002'


def test_member_hot_clear_and_concurrent_observe():
    d = mentions.MemberDirectory(hot_per_group=1000, hot_global=1000)

    def worker(base):
        for i in range(200):
            d.observe('g', str(base + i), f'n{base + i}')

    t1 = threading.Thread(target=worker, args=(1,))
    t2 = threading.Thread(target=worker, args=(10001,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert d._hot_count == len(d._hot['g']) == 400
    d.clear()
    assert d._hot_count == 0 and not d._hot and not d._hot_order


# ── 2. MemberInfoDirectory._cache/_failed ───────────────────────


class _OkMemberBot:
    def __init__(self):
        self.calls = []

    async def get_group_member_info(self, gid, qq, no_cache=False):
        self.calls.append((str(gid), str(qq), bool(no_cache)))
        return {'retcode': 0, 'data': {
            'user_id': int(qq), 'role': 'admin', 'level': '3',
            'nickname': f'n{qq}'}}


class _FailMemberBot:
    def __init__(self):
        self.calls = 0

    async def get_group_member_info(self, gid, qq, no_cache=False):
        self.calls += 1
        raise RuntimeError('rpc down')


def test_member_info_lru_上限淘汰最久未用():
    bot = _OkMemberBot()
    d = mentions.MemberInfoDirectory(ttl=60, max_entries=2, max_failed=8)
    assert _run(d.info('1', '10001', bot))['qq'] == '10001'
    assert _run(d.info('1', '10002', bot))['qq'] == '10002'
    # 命中 10001 → 最近使用序变成 [10002, 10001]，且不重新 RPC
    assert _run(d.info('1', '10001', bot))['qq'] == '10001'
    assert len(bot.calls) == 2
    _run(d.info('1', '10003', bot))     # 超上限：淘汰最旧 10002
    assert ('1', '10002') not in d._cache
    assert ('1', '10001') in d._cache
    assert len(d._cache) == 2


def test_member_info_lazy_expiry_and_failed_sweep():
    bot = _OkMemberBot()
    d = mentions.MemberInfoDirectory(ttl=60, max_entries=16)
    d._sweep_every = 1                  # 每次访问都清扫，便于确定性断言
    _run(d.info('1', '10001', bot))
    key = ('1', '10001')
    ts, info = d._cache[key]
    d._cache[key] = (ts - 120, info)    # 伪造成已过期（TTL 60s）
    d._failed[('1', '10003')] = time.time() - mentions.ROSTER_FAIL_BACKOFF - 1
    _run(d.info('1', '10002', bot))     # 本次访问触发惰性清扫
    assert key not in d._cache
    assert ('1', '10003') not in d._failed


def test_member_info_failed_cap_and_backoff():
    bot = _FailMemberBot()
    d = mentions.MemberInfoDirectory(ttl=60, max_entries=8, max_failed=2)
    for qq in ('10001', '10002', '10003'):
        assert _run(d.info('1', qq, bot)) is None
    assert len(d._failed) == 2
    assert ('1', '10001') not in d._failed     # 最旧的被淘汰
    assert bot.calls == 3
    # 仍在退避窗口内：再查不重新 RPC
    assert _run(d.info('1', '10003', bot)) is None
    assert bot.calls == 3


# ── 3. MemoryManager._cache LRU ─────────────────────────────────


def test_memory_cache_lru_evicts_and_reloads(monkeypatch):
    loads = []

    def fake_list(db, uid):
        loads.append(uid)
        return []

    monkeypatch.setattr(memory_mod.storage, 'list_active_memories', fake_list)
    mm = memory_mod.MemoryManager('unused.db', cache_max_users=2)
    mm._index('u1')
    mm._index('u2')
    mm._index('u1')                     # 命中 → u2 变最久未用
    assert list(mm._cache) == ['u2', 'u1']
    mm._index('u3')                     # 超上限：淘汰 u2
    assert list(mm._cache) == ['u1', 'u3']
    assert 'u2' not in mm._cache
    mm._index('u2')                     # 再访问：重新加载，淘汰最旧 u1
    assert loads.count('u2') == 2
    assert 'u1' not in mm._cache
    # invalidate 语义不变：单用户删除 / 全清
    mm.invalidate('u3')
    assert 'u3' not in mm._cache
    mm.invalidate()
    assert not mm._cache


def test_memory_cache_eviction_releases_index(monkeypatch):
    monkeypatch.setattr(memory_mod.storage, 'list_active_memories',
                        lambda db, uid: [])
    mm = memory_mod.MemoryManager('unused.db', cache_max_users=1)
    res = mm._index('u1')
    ref = weakref.ref(res[1])
    del res
    mm._index('u2')                     # 淘汰 u1：元组从缓存移除
    assert 'u1' not in mm._cache
    assert ref() is None                # BM25 索引不再被持有


# ── 4. QzoneWatcher ─────────────────────────────────────────────


class _FakeQz:
    def __init__(self, available: bool = True):
        self._available = available

    def available(self) -> bool:
        return self._available

    async def refresh(self):
        pass

    async def fetch_feeds(self, limit=10):
        return [], False, None, ''

    async def fetch_posts(self, limit=10):
        return [], ''


class _FakePlugin:
    def __init__(self, db: str, cfg: dict):
        self.db_path = db
        self.cfg = dict(cfg)
        self.qzone = _FakeQz()
        self.bot_id = '10001'
        self.bot_name = '大肥鱼'


def _watcher_cfg(**kw) -> dict:
    cfg = {'qzone_watch_on': True, 'owner_qq': '10001',
           'qzone_watch_feeds': False, 'qzone_watch_comments': False}
    cfg.update(kw)
    return cfg


def test_qzone_interval_reread_each_round():
    plugin = _FakePlugin(_tmp_db(), _watcher_cfg(qzone_watch_interval=60))
    w = qzone_watch.QzoneWatcher(plugin, log=lambda *a, **k: None)
    sleeps, ticks = [], []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:            # 跑完第一轮后在线改配置
            plugin.cfg['qzone_watch_interval'] = 120
        if len(sleeps) >= 3:
            w._stopped = True

    async def fake_tick():
        ticks.append(1)
        return {'events': 0, 'handled': 0}

    w._sleep = fake_sleep
    w.tick = fake_tick
    _run(w._loop())
    assert sleeps[0] == 30.0            # 启动延迟 min(30, 间隔)
    assert sleeps[1] == 60.0            # 第一轮沿用启动时的配置
    assert sleeps[2] == 120.0          # 改配置后下一轮立即生效
    assert len(ticks) == 2


def test_qzone_interval_validation():
    plugin = _FakePlugin(_tmp_db(), _watcher_cfg(qzone_watch_interval=5))
    w = qzone_watch.QzoneWatcher(plugin)
    assert w._interval() == 30.0        # 不低于下限
    plugin.cfg['qzone_watch_interval'] = 'bad'
    assert w._interval() == 300.0       # 非法值回默认
    plugin.cfg['qzone_watch_interval'] = 90
    assert w._interval() == 90.0


def test_qzone_tick_prunes_state_every_24_rounds(monkeypatch):
    db = _tmp_db()
    plugin = _FakePlugin(db, _watcher_cfg())
    w = qzone_watch.QzoneWatcher(plugin, log=lambda *a, **k: None)
    pruned = []
    monkeypatch.setattr(qzone_watch.runtime_env, 'is_official', lambda: False)
    monkeypatch.setattr(qzone_watch.storage, 'prune_qzone_state',
                        lambda path: pruned.append(path) or 0)

    async def run_ticks():
        for _ in range(23):
            await w.tick()
        assert pruned == []             # 未到频次不清理
        await w.tick()                  # 第 24 轮
        assert pruned == [db]

    _run(run_ticks())
