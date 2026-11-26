"""2026-11-26 性能审计：storage 层修复的回归测试。

覆盖：
1. connect() 不再每次重设 WAL（只由 migrate 设一次）；
2. v21 迁移补的查询索引（usage_log/messages/groups/users/raw_messages）；
3. overview_stats 单条 GROUP BY 聚合与原按天逐查结果一致；
4. usage_summary_many 与逐群 usage_summary 语义一致；
5. trim_raw 索引区间删除保持「留最近 cap 条」；
6. 保留策略（messages 只删已摘要旧行 / usage_log 按天 / qzone_state 孤儿键）；
7. 新增批量 DAL（get_users_many / messages_before）。
"""
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'plugins', 'deepseek'))

from Core import storage  # noqa: E402


def _tmp_db() -> str:
    db = os.path.join(tempfile.mkdtemp(), 'perf.db')
    storage.migrate(db)
    return db


def _add_usage(db, rows):
    conn = storage.connect(db)
    try:
        conn.executemany(
            'INSERT INTO usage_log (group_id, kind, tokens_in, cache_hit,'
            ' tokens_out, tool_rounds, ok, created_at) VALUES (?,?,?,?,?,?,?,?)',
            rows)
        conn.commit()
    finally:
        conn.close()


# ── 1. 连接开销 ──────────────────────────────────────────────────

def test_connect_does_not_reset_journal_mode_every_time():
    """connect() 里不能再有 PRAGMA journal_mode（每次 0.9ms × 186 调用点）。"""
    import inspect
    src = inspect.getsource(storage.connect)
    # 只禁止「执行 PRAGMA journal_mode」，注释里提到该 PRAGMA 不算
    assert "PRAGMA journal_mode" not in src
    # WAL 仍由 migrate 设好，并成为库文件持久属性
    db = _tmp_db()
    conn = storage.connect(db)
    try:
        assert conn.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal'
    finally:
        conn.close()


def test_busy_timeout_is_short():
    """忙等上限从 10s 收紧到 2s，避免一把写锁冻住整个事件循环 10 秒。"""
    db = _tmp_db()
    conn = storage.connect(db)
    try:
        ms = conn.execute('PRAGMA busy_timeout').fetchone()[0]
    finally:
        conn.close()
    assert 0 < ms <= 3000


# ── 2. v21 索引迁移 ──────────────────────────────────────────────

def test_migrate_v21_creates_indexes():
    db = _tmp_db()
    conn = storage.connect(db)
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 21
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        conn.close()
    for idx in ('idx_usage_created', 'idx_messages_group_summarized',
                'idx_groups_updated', 'idx_groups_enabled_updated',
                'idx_users_last_seen', 'idx_raw_group_time'):
        assert idx in names, idx


def test_migrate_v20_to_v21_is_idempotent():
    db = _tmp_db()
    conn = storage.connect(db)
    try:
        conn.execute('DROP INDEX IF EXISTS idx_usage_created')
        conn.execute('PRAGMA user_version=20')
        conn.commit()
    finally:
        conn.close()
    storage.migrate(db)
    storage.migrate(db)          # 幂等
    conn = storage.connect(db)
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 21
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_usage_created'").fetchone()
    finally:
        conn.close()


def test_usage_created_query_uses_new_index():
    db = _tmp_db()
    now = time.time()
    _add_usage(db, [('1', 'chat', 100, 50, 20, 1, 1, now - 3600)] * 50)
    conn = storage.connect(db)
    try:
        plan = ' '.join(
            str(r[-1]) for r in conn.execute(
                'EXPLAIN QUERY PLAN SELECT COUNT(*) FROM usage_log '
                'WHERE created_at>=?', (now - 86400,)).fetchall())
    finally:
        conn.close()
    assert 'idx_usage_created' in plan or 'USING INDEX' in plan


# ── 3. overview_stats 单次聚合 ──────────────────────────────────

def test_overview_stats_buckets_match_expected():
    db = _tmp_db()
    now = int(time.time())
    today = now - now % 86400
    rows = [
        ('1', 'chat', 100, 40, 10, 1, 1, today + 60),          # 今天
        ('1', 'chat', 200, 80, 20, 1, 1, today + 120),         # 今天
        ('2', 'judge', 50, 10, 5, 0, 1, today - 86400 + 60),   # 昨天
        ('2', 'mood', 30, 5, 3, 0, 1, today - 2 * 86400 + 60), # 前天
    ]
    _add_usage(db, rows)
    st = storage.overview_stats(db, days=7)
    assert st['today_calls'] == 2
    assert st['tokens_in'] == 300
    assert st['cache_hit'] == 120
    assert st['tokens_out'] == 30
    assert len(st['series']) == 7
    by_day = {s['day']: s for s in st['series']}
    assert by_day[today]['calls'] == 2
    assert by_day[today]['tokens'] == 300 + 30
    assert by_day[today - 86400]['calls'] == 1
    assert by_day[today - 86400]['tokens'] == 55
    assert by_day[today - 2 * 86400]['calls'] == 1
    assert by_day[today - 3 * 86400]['calls'] == 0


def test_overview_stats_days_clamped():
    db = _tmp_db()
    st = storage.overview_stats(db, days=0)
    assert len(st['series']) == 1


# ── 4. usage_summary_many 与单群版一致 ──────────────────────────

def test_usage_summary_many_matches_single():
    db = _tmp_db()
    now = time.time()
    _add_usage(db, [
        ('111', 'chat', 100, 50, 20, 1, 1, now),
        ('111', 'judge', 10, 0, 2, 0, 1, now),
        ('222', 'chat', 300, 100, 30, 1, 1, now),
    ])
    many = storage.usage_summary_many(db, ['111', '222', '333'], limit_rows=200)
    for gid in ('111', '222'):
        single = storage.usage_summary(db, gid, limit_rows=200)
        assert many[gid] == single
    assert many['333']['calls'] == 0


def test_usage_summary_many_single_connection(monkeypatch):
    """批量版只建一次连接（原 /api/groups 每群一次 ≈ 100 次）。"""
    db = _tmp_db()
    _add_usage(db, [('111', 'chat', 1, 0, 1, 0, 1, time.time())])
    calls = []
    real_connect = storage.connect

    def counting_connect(path):
        calls.append(path)
        return real_connect(path)

    monkeypatch.setattr(storage, 'connect', counting_connect)
    storage.usage_summary_many(db, ['111', '222', '333'], limit_rows=10)
    assert len(calls) == 1


# ── 5. trim_raw ─────────────────────────────────────────────────

def test_trim_raw_keeps_recent_rows():
    db = _tmp_db()
    for i in range(10):
        storage.insert_raw_message(db, '777', f'u{i}', f'n{i}', f'm{i}')
    removed = storage.trim_raw(db, '777', 5)
    assert removed == 5
    conn = storage.connect(db)
    try:
        ids = [r[0] for r in conn.execute(
            'SELECT id FROM raw_messages WHERE group_id=? ORDER BY id',
            ('777',))]
        contents = [r[0] for r in conn.execute(
            'SELECT content FROM raw_messages WHERE group_id=? ORDER BY id',
            ('777',))]
    finally:
        conn.close()
    assert len(ids) == 5
    assert contents == ['m5', 'm6', 'm7', 'm8', 'm9']


def test_trim_raw_noop_when_under_cap():
    db = _tmp_db()
    storage.insert_raw_message(db, '777', 'u', 'n', 'm')
    assert storage.trim_raw(db, '777', 5) == 0


# ── 6. 保留策略 ─────────────────────────────────────────────────

def _insert_messages(db, gid, count, summarized_upto):
    conn = storage.connect(db)
    try:
        now = time.time()
        for i in range(1, count + 1):
            conn.execute(
                'INSERT INTO messages (group_id, role, sender_id, sender_name,'
                ' content, tool_calls, tool_call_id, name, summarized, created_at)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?)',
                (gid, 'user', '1', 'n', f'm{i}', '', '', '',
                 1 if i <= summarized_upto else 0, now + i))
        conn.commit()
    finally:
        conn.close()


def test_prune_messages_only_deletes_summarized_old_rows():
    db = _tmp_db()
    _insert_messages(db, '111', 300, summarized_upto=250)
    removed = storage.prune_messages(db, keep_per_group=200)
    # 窗口 = 最近 200 条（id 101..300），只删已摘要且 id<101 的 100 条
    assert removed == 100
    conn = storage.connect(db)
    try:
        min_id = conn.execute(
            'SELECT MIN(id) FROM messages WHERE group_id=?', ('111',)).fetchone()[0]
        left = conn.execute(
            'SELECT COUNT(*) FROM messages WHERE group_id=?', ('111',)).fetchone()[0]
        unsummarized_left = conn.execute(
            'SELECT COUNT(*) FROM messages WHERE group_id=? AND summarized=0',
            ('111',)).fetchone()[0]
    finally:
        conn.close()
    assert left == 200
    assert min_id == 101
    assert unsummarized_left == 50


def test_prune_messages_never_drops_unsummarized_rows():
    """只删已摘要的旧行：未摘要消息无论多少条都不动。"""
    db = _tmp_db()
    _insert_messages(db, '111', 260, summarized_upto=10)
    removed = storage.prune_messages(db, keep_per_group=200)
    assert removed == 10
    conn = storage.connect(db)
    try:
        left = conn.execute(
            'SELECT COUNT(*) FROM messages WHERE group_id=?', ('111',)).fetchone()[0]
        unsummarized = conn.execute(
            'SELECT COUNT(*) FROM messages WHERE group_id=? AND summarized=0',
            ('111',)).fetchone()[0]
    finally:
        conn.close()
    assert left == 250 and unsummarized == 250


def test_prune_usage_log_by_days():
    db = _tmp_db()
    now = time.time()
    _add_usage(db, [
        ('1', 'chat', 1, 0, 1, 0, 1, now - 200 * 86400),
        ('1', 'chat', 1, 0, 1, 0, 1, now - 30 * 86400),
    ])
    removed = storage.prune_usage_log(db, days=180)
    assert removed == 1
    conn = storage.connect(db)
    try:
        left = conn.execute('SELECT COUNT(*) FROM usage_log').fetchone()[0]
    finally:
        conn.close()
    assert left == 1


def test_prune_qzone_state_drops_orphan_ccount():
    db = _tmp_db()
    storage.add_qzone_post(db, 'tid-keep', 'hi')
    storage.set_qzone_state(db, 'ccount:tid-keep', '3')
    storage.set_qzone_state(db, 'ccount:tid-gone', '5')
    storage.set_qzone_state(db, 'comment_since', '123')
    removed = storage.prune_qzone_state(db)
    assert removed == 1
    assert storage.get_qzone_state(db, 'ccount:tid-keep') == '3'
    assert storage.get_qzone_state(db, 'ccount:tid-gone') in (None, '')
    assert storage.get_qzone_state(db, 'comment_since') == '123'


def test_housekeeping_runs_all_prunes():
    db = _tmp_db()
    _insert_messages(db, '111', 300, summarized_upto=250)
    now = time.time()
    _add_usage(db, [('1', 'chat', 1, 0, 1, 0, 1, now - 200 * 86400)])
    storage.set_qzone_state(db, 'ccount:gone', '1')
    res = storage.housekeeping(db, keep_per_group=200, usage_days=180,
                               qzone_days=30)
    assert set(res) == {'messages', 'usage_log', 'qzone_events', 'qzone_state'}
    assert res['messages'] == 100
    assert res['usage_log'] == 1
    assert res['qzone_state'] == 1


# ── 7. 新增批量 DAL ─────────────────────────────────────────────

def test_get_users_many_batches_and_tolerates_missing():
    db = _tmp_db()
    storage.upsert_user(db, '10001', '小明', '111')
    storage.upsert_user(db, '10002', '小红', '111')
    got = storage.get_users_many(db, ['10001', '10002', '99999', ''])
    assert set(got) == {'10001', '10002'}
    assert '小明' in got['10001']['names']


def test_get_users_many_empty_input():
    db = _tmp_db()
    assert storage.get_users_many(db, []) == {}
    assert storage.get_users_many(db, None) == {}


def test_messages_before_pagination():
    db = _tmp_db()
    ids = [storage.insert_message(db, '111', 'user', f'm{i}') for i in range(10)]
    rows = storage.messages_before(db, '111', ids[7], limit=3)
    assert [r['id'] for r in rows] == ids[4:7]
    assert [r['content'] for r in rows] == ['m4', 'm5', 'm6']


def test_messages_before_includes_tool_rows_only_when_requested():
    db = _tmp_db()
    storage.insert_message(db, '111', 'user', 'u1')
    storage.insert_message(db, '111', 'tool', 't1')
    storage.insert_message(db, '111', 'assistant', 'a1', tool_calls=[{'id': 'x'}])
    all_rows = storage.messages_before(db, '111', 10**9, limit=10, roles=())
    assert sorted(r['role'] for r in all_rows) == ['assistant', 'tool', 'user']
    no_tool = storage.messages_before(db, '111', 10**9, limit=10,
                                      roles=('user', 'assistant'))
    assert sorted(r['role'] for r in no_tool) == ['assistant', 'user']


def test_list_users_digit_query_uses_primary_key(monkeypatch):
    db = _tmp_db()
    storage.upsert_user(db, '10001', '小明', '111')
    got = storage.list_users(db, query='10001')
    assert len(got) == 1 and got[0]['user_id'] == '10001'
    # 昵称模糊匹配仍然可用
    got2 = storage.list_users(db, query='小明')
    assert len(got2) == 1 and got2[0]['user_id'] == '10001'
