"""2026-11-26 NapCat 适配 + QQ 空间读写/知会 的回归测试。

覆盖四块：
1. 空间客户端：WS 优先、official 摘除、发/评/读图的参数校验与落库；
2. 空间轮询：纯函数（@ 匹配、新评论筛选、知会文案、投递目标）；
3. 扩展通知：资料赞不响应、戳一戳回戳、群名同步；
4. 框架层：message_sent 不进消息分发、storage v20 迁移与事件去重。
"""
import asyncio
import inspect
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'plugins', 'deepseek'))

from Core import backfill  # noqa: E402
from Core import builtin_tools  # noqa: E402
from Core import engine as engine_mod  # noqa: E402
from Core import notices  # noqa: E402
from Core import qzone as qz  # noqa: E402
from Core import qzone_watch  # noqa: E402
from Core import runtime_env  # noqa: E402
from Core import storage  # noqa: E402


def _tmp_db() -> str:
    db = os.path.join(tempfile.mkdtemp(), 'qzone.db')
    storage.migrate(db)
    return db


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeBot:
    """记录 action 调用，按 action 返回预置信封（OneBot 完整响应）。"""

    def __init__(self, data=None):
        self.calls = []
        self.data = data if data is not None else {}

    async def call_api(self, action, params=None):
        self.calls.append((action, dict(params or {})))
        return {'status': 'ok', 'retcode': 0, 'data': self.data}


class _FakePlugin:
    def __init__(self, db=None, bot=None, cfg=None, engine=None):
        self.db_path = db or _tmp_db()
        self.bot = bot if bot is not None else _FakeBot()
        self.cfg = dict(cfg or {})
        self.bot_id = '10001'
        self.bot_name = '大肥鱼'
        self.engine = engine
        self.logs = []

    def log(self, msg, level='info'):
        self.logs.append((level, str(msg)))


def _root_config(tmpdir, **kw):
    path = os.path.join(tmpdir, 'config.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(kw, f)
    runtime_env.ROOT_CONFIG = type(runtime_env.ROOT_CONFIG)(path)
    runtime_env.reset_cache()
    return path


# ── 1. 空间客户端 ────────────────────────────────────────────────

def test_mentions_me_handles_nickname_and_qq():
    assert qz.mentions_me('今天@大肥鱼 你看', '10001', '大肥鱼')
    assert qz.mentions_me('@大肥鱼你看', '10001', '大肥鱼')
    assert qz.mentions_me('@10001 你好', '10001', '大肥鱼')
    assert not qz.mentions_me('@100010 你好', '10001', '大肥鱼')   # QQ 号必须是完整匹配
    assert not qz.mentions_me('@小鱼 你好', '10001', '大肥鱼')
    assert not qz.mentions_me('', '10001', '大肥鱼')


def test_find_mentions_maps_feed_fields():
    feeds = [
        {'tid': 'T1', 'uin': 20002, 'name': '小明', 'time': 1764000000,
         'content': '@大肥鱼 在吗', 'images': ['http://a/1.jpg'],
         'comment_num': 3},
        {'tid': 'T2', 'uin': 20003, 'name': '小红', 'time': 1764000100,
         'content': '今天天气不错', 'images': [], 'comment_num': 0},
    ]
    rows = qz.find_mentions(feeds, '10001', '大肥鱼')
    assert [r['tid'] for r in rows] == ['T1']
    assert rows[0]['uin'] == '20002'
    assert rows[0]['nickname'] == '小明'
    assert rows[0]['images'] == ['http://a/1.jpg']
    assert rows[0]['comment_count'] == 3


def test_new_comments_skips_self_and_already_seen():
    detail = {'comments': [
        {'comment_id': 'c1', 'uin': 30001, 'name': '甲', 'time': 100,
         'content': '旧的'},
        {'comment_id': 'c2', 'uin': 30002, 'name': '乙', 'time': 200,
         'content': '新的'},
        {'comment_id': 'c3', 'uin': 10001, 'name': '我', 'time': 300,
         'content': '自己回的'},
    ]}
    rows = qz.new_comments(detail, since_ts=150, self_uin='10001')
    assert [r['comment_id'] for r in rows] == ['c2']
    # 楼中楼里的新回复也要能被发现
    detail2 = {'comments': [
        {'comment_id': 'c9', 'uin': 30002, 'name': '乙', 'time': 50,
         'content': '一楼', 'replies': [
             {'comment_id': 'r1', 'uin': 30003, 'name': '丙', 'time': 400,
              'content': '楼中楼'}]},
    ]}
    rows2 = qz.new_comments(detail2, since_ts=150, self_uin='10001')
    assert [r['comment_id'] for r in rows2] == ['r1']


def test_official_transport_disables_qzone(monkeypatch=None):
    tmp = tempfile.mkdtemp()
    old = runtime_env.ROOT_CONFIG
    try:
        _root_config(tmp, transport='official')
        db = _tmp_db()
        plugin = _FakePlugin(db=db)
        client = qz.QzoneClient(plugin)
        assert client.available() is False
        cap = _run(client.refresh(force=True))
        assert cap.available is False
        assert 'official' in cap.why
        # 调用直接短路，不会碰 bot（official 通道没有空间 action）
        data, err = _run(client._call('get_qzone_msg_list', {'num': 1}))
        assert data is None and 'official' in err
        assert plugin.bot.calls == []
    finally:
        runtime_env.ROOT_CONFIG = old
        runtime_env.reset_cache()


def test_publish_with_images_uses_ws_and_records(monkeypatch=None):
    tmp = tempfile.mkdtemp()
    old = runtime_env.ROOT_CONFIG
    try:
        _root_config(tmp, transport='napcat')
        db = _tmp_db()
        bot = _FakeBot({'tid': 'T100', 'time': 1764000000})
        plugin = _FakePlugin(db=db, bot=bot)
        client = qz.QzoneClient(plugin)
        client.cap = qz.QzoneCapability(True, 'ws', '', 'NapCat 4.18.42', 9e18)
        msg = _run(client.publish('图文说说', ugc_right=4,
                                  images=['base64://AAAA'],
                                  sender_id='10001', sender_name='大肥鱼'))
        assert 'T100' in msg and '1 张图' in msg
        action, params = bot.calls[-1]
        assert action == 'send_qzone_msg'
        assert params['content'] == '图文说说'
        assert params['images'] == ['base64://AAAA']
        assert params['ugc_right'] == 4
        rows = storage.list_qzone_posts(db, limit=5)
        assert rows and rows[0]['tid'] == 'T100'
        assert json.loads(rows[0]['images']) == ['base64://AAAA']
    finally:
        runtime_env.ROOT_CONFIG = old
        runtime_env.reset_cache()


def test_publish_rejects_too_many_images_and_empty():
    plugin = _FakePlugin()
    client = qz.QzoneClient(plugin)
    client.cap = qz.QzoneCapability(True, 'ws', '', '', 9e18)
    assert '不能为空' in _run(client.publish(''))
    msg = _run(client.publish('x', images=[f'http://a/{i}.jpg'
                                           for i in range(qz.MAX_IMAGES + 1)]))
    assert '最多' in msg
    assert plugin.bot.calls == []


def test_reply_comment_requires_id_and_uin_pair():
    plugin = _FakePlugin()
    client = qz.QzoneClient(plugin)
    client.cap = qz.QzoneCapability(True, 'ws', '', '', 9e18)
    msg = _run(client.comment('T1', '回你', comment_id='c1'))
    assert '成对' in msg
    assert plugin.bot.calls == []
    ok = _run(client.comment('T1', '回你', comment_id='c1',
                             comment_uin='30002', reply_name='乙'))
    assert '已回复' in ok
    action, params = plugin.bot.calls[-1]
    assert action == 'comment_qzone'
    assert params['comment_id'] == 'c1' and params['comment_uin'] == '30002'
    assert params['reply_name'] == '乙'


def test_unavailable_qzone_tools_removed_from_schemas():
    plugin = _FakePlugin()
    plugin.mcp = None
    plugin.custom = None

    class _Qz:
        def available(self):
            return False

    plugin.qzone = _Qz()
    eng = engine_mod.Engine(plugin)
    schemas = eng._build_tools('123', plugin.cfg)[0]
    names = [s['function']['name'] for s in schemas]
    assert names and not [n for n in names if n.startswith('qzone_')]

    plugin.qzone = type('_Q', (), {'available': lambda self: True})()
    names2 = [s['function']['name']
              for s in eng._build_tools('123', plugin.cfg)[0]]
    assert 'qzone_list' in names2 and 'qzone_images' in names2


# ── 2. 空间轮询纯函数 ────────────────────────────────────────────

def test_notify_target_prefers_group_else_owner_private():
    assert qzone_watch.notify_target(
        {'qzone_notify_group': '777', 'owner_qq': '10001'}) == ('777', 'group')
    assert qzone_watch.notify_target(
        {'qzone_notify_group': '', 'owner_qq': '10001'}) == ('10001', 'private')


def test_space_event_text_mention_points_to_comment_tool():
    text = qzone_watch.space_event_text(
        {'kind': 'mention', 'tid': 'T1', 'actor_uin': '20002',
         'actor_name': '小明', 'content': '@大肥鱼 看这个',
         'event_time': 1764000000},
        self_name='大肥鱼', images=2)
    assert 'T1' in text and 'qzone_comment' in text and 'qzone_images' in text
    assert '小明' in text


def test_space_event_text_comment_includes_comment_id():
    text = qzone_watch.space_event_text(
        {'kind': 'comment', 'tid': 'T2', 'comment_id': 'c7',
         'comment_uin': '30002', 'actor_name': '乙', 'content': '说得对',
         'event_time': 1764000000},
        post_preview='说说正文')
    assert 'c7' in text and 'comment_uin' in text


class _FakeQz:
    """轮询用的假空间客户端：feeds/posts/detail 由用例预置。"""

    def __init__(self, feeds=None, posts=None, detail=None):
        self.feeds = feeds or []
        self.posts = posts or []
        self.detail = detail or {}
        self.cap = qz.QzoneCapability(True, 'ws', '', 'NapCat 4.18.42', 9e18)

    def available(self):
        return True

    def status_text(self):
        return 'QQ 空间不可用' if not self.available() else 'QQ 空间可用（ws）'

    async def refresh(self, force=False):
        return self.cap

    async def fetch_feeds(self, limit=10, page=1):
        return self.feeds, False, '', ''

    async def fetch_posts(self, limit=10, target_uin=''):
        return self.posts, ''

    async def fetch_detail(self, tid, pos=0, num=20):
        return self.detail or {'tid': tid, 'content': '说说', 'comments': []}, ''


def test_watcher_tick_queues_mention_and_dispatches():
    db = _tmp_db()
    calls = []

    class _Engine:
        async def handle_trigger(self, gid, cfg, text, **kw):
            calls.append((gid, text, kw))
            return {'status': 'completed'}

    import time as _time
    feed = {'tid': 'T9', 'uin': 20002, 'nickname': '小明',
            'time': int(_time.time()) - 100,
            'content': '@大肥鱼 看这个', 'images': ['x'], 'comment_num': 1}
    plugin = _FakePlugin(db=db, engine=_Engine(), cfg={
        'qzone_watch_on': True, 'owner_qq': '10001',
        'qzone_watch_feeds': True, 'qzone_watch_comments': False,
        'qzone_notify_group': '777', 'bot_name': '大肥鱼'})
    fake = _FakeQz(feeds=[feed], posts=[])
    plugin.qzone = fake
    watcher = qzone_watch.QzoneWatcher(plugin, log=plugin.log)
    res = _run(watcher.tick())          # 首次 tick 只建基线，历史 @ 不补报
    assert res['events'] == 0
    feed['time'] = int(_time.time()) + 5      # 基线之后的新 @
    res2 = _run(watcher.tick())
    assert res2['events'] == 1
    assert len(calls) == 1
    gid, text, kw = calls[0]
    assert gid == '777' and kw['run_kind'] == 'qzone'
    # 委托人是主人：否则 qzone_comment/like/publish 会被风险门禁拒绝
    assert kw['actor_id'] == '10001'
    assert 'T9' in text and '小明' in text
    rows = storage.recent_qzone_events(db, limit=5)
    assert rows and rows[0]['status'] == 'handled'


def test_watcher_skips_when_official():
    db = _tmp_db()
    old = runtime_env.ROOT_CONFIG
    try:
        _root_config(tempfile.mkdtemp(), transport='official')
        plugin = _FakePlugin(db=db, cfg={'qzone_watch_on': True})
        plugin.qzone = _FakeQz()
        watcher = qzone_watch.QzoneWatcher(plugin)
        assert _run(watcher.tick())['disabled'] == 'official'
    finally:
        runtime_env.ROOT_CONFIG = old
        runtime_env.reset_cache()


def test_engine_handle_trigger_private_notice():
    """空间知会走私聊：chat_type/raw_bg_notice/委托人不覆盖发送人。"""
    from Core import engine as engine_mod

    db = _tmp_db()
    plugin = _FakePlugin(db=db, cfg={'owner_qq': '10001', 'bg_runs_keep': 5})
    eng = engine_mod.Engine(plugin)
    captured = {}

    async def fake_run(gid, cfg, parsed, forced, attachments=None, **kw):
        captured.update(gid=gid, parsed=parsed, kw=kw)
        return {'final_text': '', 'silent': True, 'sent_texts': []}

    eng._run_turn = fake_run
    res = _run(eng.handle_trigger(
        '10001', plugin.cfg, '小明 在空间里 @ 了你', source='QQ空间',
        chat_type='private', run_kind='qzone', source_kind='work',
        actor_id='20002', actor_name='小明',
        notice_text='[QQ空间通知] 小明 在空间里 @ 了你，说说 tid=T1'))
    assert res['status'] in ('silent', 'empty', 'ok')
    parsed = captured['parsed']
    assert parsed.chat_type == 'private' and parsed.allow_silent is True
    assert parsed.sender_name == '系统'          # 委托人身份不覆盖发送人
    kw = captured['kw']
    assert kw['background'] is True and kw['raw_bg_notice'] is True
    assert kw['source_kind'] == 'work'
    assert kw['actor_id'] == '20002' and kw['actor_name'] == '小明'
    # 空间知会轮默认禁发/删说说（只许读 + 评论/点赞）
    assert kw['deny_tools'] == frozenset({'qzone.publish', 'qzone.delete'})
    # 私聊会话标记：定时唤醒/提醒不再把它当群
    row = storage.get_group(db, '10001')
    assert (row.get('settings') or {}).get('chat_type') == 'private'
    # 通知原文落历史，且用的是 notice_text 而不是通用触发模板
    msgs = storage.recent_messages(db, '10001', 5)
    notices = [m for m in msgs if m.get('name') == 'trigger-notice']
    assert notices and 'QQ空间通知' in notices[-1]['content']
    # bg_runs 记成 qzone 类别
    runs = storage.list_bg_runs(db, limit=5)
    assert runs and runs[0]['kind'] == 'qzone'


def test_qzone_turn_allows_write_when_switch_on():
    """qzone_event_write_on=True 时不再禁用发/删说说。"""
    db = _tmp_db()
    plugin = _FakePlugin(db=db, cfg={'owner_qq': '10001', 'bg_runs_keep': 5,
                                     'qzone_event_write_on': True})
    eng = engine_mod.Engine(plugin)
    captured = {}

    async def fake_run(gid, cfg, parsed, forced, attachments=None, **kw):
        captured.update(kw=kw)
        return {'final_text': 'ok', 'silent': False, 'sent_texts': []}

    eng._run_turn = fake_run
    _run(eng.handle_trigger(
        '777', plugin.cfg, '有人在空间评论了你', source='QQ空间',
        run_kind='qzone'))
    assert captured['kw']['deny_tools'] == frozenset()


def test_scene_deny_keeps_reads_and_removes_publish():
    plugin = _FakePlugin()
    plugin.mcp = None
    plugin.custom = None
    plugin.qzone = type('_Q', (), {'available': lambda self: True})()
    eng = engine_mod.Engine(plugin)
    deny = frozenset({'qzone.publish', 'qzone.delete'})
    names = [x['function']['name']
             for x in eng._build_tools('123', plugin.cfg, deny)[0]]
    assert 'qzone_list' in names and 'qzone_images' in names
    assert 'qzone_comment' in names and 'qzone_like' in names
    assert 'qzone_publish' not in names and 'qzone_delete' not in names


def test_watcher_dispatch_uses_target_group_overrides():
    """知会轮按投递会话的群级覆盖跑（window_rounds 只是示例键）。"""
    db = _tmp_db()
    plugin = _FakePlugin(db=db, cfg={'owner_qq': '10001',
                                     'window_rounds': 12,
                                     'qzone_notify_group': '777'})
    storage.upsert_group(db, '777', settings={'window_rounds': 5})
    storage.add_qzone_event(
        db, 'mention', tid='T1', actor_uin='20002', actor_name='小明',
        content='@大肥鱼 看这个', comment_id='', comment_uin='',
        parent_id='', event_time=1700000000.0, handle_gid='777')
    got = {}

    class _Eng:
        async def handle_trigger(self, gid, cfg, text, **kw):
            got.update(gid=gid, cfg=cfg, kw=kw)
            return {'status': 'ok'}

    plugin.engine = _Eng()

    class _Qz:
        available = staticmethod(lambda: True)

        async def fetch_detail(self, tid, num=1):
            return {'content': '@大肥鱼 看这个', 'images': []}, ''

    plugin.qzone = _Qz()
    w = qzone_watch.QzoneWatcher(plugin, log=lambda *a, **k: None)
    assert _run(w.dispatch(db, plugin.cfg)) == 1
    assert got['gid'] == '777'
    assert got['cfg']['window_rounds'] == 5          # 群覆盖生效
    assert got['kw']['run_kind'] == 'qzone'


def test_web_qzone_info_surfaces_transport_and_reason():
    from Core import web as web_mod

    class _P:
        pass

    p = _P()
    p.qzone = _FakeQz()
    info = web_mod._qzone_info(p)
    assert info['available'] is True and info['channel'] == 'ws'
    assert info['text']

    assert web_mod._qzone_info(None)['text'] == '未加载'

    p2 = _P()
    p2.qzone = None
    assert web_mod._qzone_info(p2)['text'] == '未加载'


def test_backfill_group_inserts_new_and_skips_duplicates():
    db = _tmp_db()
    ts = 1700000000.0
    storage.insert_raw_message(db, '777', '20002', '小明', 'hello',
                               created_at=ts)
    history = [
        {'sender': {'user_id': 20002, 'nickname': '小明'}, 'time': ts,
         'message': [{'type': 'text', 'data': {'text': 'hello'}}]},
        {'sender': {'user_id': 20003, 'nickname': '小红'}, 'time': ts + 60,
         'message': [{'type': 'text', 'data': {'text': '新消息'}}]},
    ]
    bot = _FakeBot({'messages': history})
    plugin = _FakePlugin(db=db, bot=bot)
    added = _run(backfill.backfill_group(plugin, '777'))
    assert added == 1
    rows = storage.recent_raw_messages(db, '777', 10)
    assert [r['content'] for r in rows] == ['hello', '新消息']
    assert bot.calls[0][0] == 'get_group_msg_history'
    assert bot.calls[0][1]['count'] == backfill.COUNT


def test_backfill_run_respects_official_and_switch():
    old = runtime_env.ROOT_CONFIG
    try:
        _root_config(tempfile.mkdtemp(), transport='official')
        plugin = _FakePlugin(cfg={'history_backfill_on': True})
        assert _run(backfill.run(plugin))['skipped'] == 'official'
        _root_config(tempfile.mkdtemp(), transport='napcat')
        plugin2 = _FakePlugin(cfg={'history_backfill_on': False})
        assert _run(backfill.run(plugin2))['skipped'] == 'off'
    finally:
        runtime_env.ROOT_CONFIG = old
        runtime_env.reset_cache()


def test_backfill_run_only_enabled_groups_with_history():
    db = _tmp_db()
    for gid, enabled, raw in (('111', True, True), ('222', False, True),
                              ('333', True, False)):
        storage.upsert_group(db, gid, enabled=enabled)
        if raw:
            storage.insert_raw_message(db, gid, '20002', '小明', 'x')
    bot = _FakeBot({'messages': []})
    plugin = _FakePlugin(db=db, bot=bot, cfg={'history_backfill_on': True})
    res = _run(backfill.run(plugin))
    # 333 没有本地语料：不发 RPC；222 未启用：不发 RPC
    assert res['groups'] == 2        # 处理了 111 与 333（333 空转）
    assert [c[1]['group_id'] for c in bot.calls] == [111]


def test_builtin_dispatch_routes_all_qzone_tools():
    """9 个空间工具都从 dispatch 正确落到客户端方法（防止 schema 与分派脱节）。"""
    from Core import builtin_tools as bt

    calls = []

    class _Qz:
        cap = qz.QzoneCapability(True, 'ws', '', 'NapCat 4.18.42', 0)

        def available(self):
            return True

        async def list_posts(self, limit, target_uin=''):
            calls.append(('list', limit, target_uin))
            return 'posts'

        async def post_detail(self, tid):
            calls.append(('detail', tid))
            return 'detail'

        async def read_images(self, tid, indexes):
            calls.append(('images', tid, indexes))
            return 'images', []

        async def list_feeds(self, limit):
            calls.append(('feeds', limit))
            return 'feeds'

        async def comment(self, tid, content, **kw):
            calls.append(('comment', tid, content, kw))
            return 'commented'

        async def like(self, tid, on):
            calls.append(('like', tid, on))
            return 'liked'

        async def publish(self, content, ugc_right, target_uins, **kw):
            calls.append(('publish', content, ugc_right, kw))
            return 'published'

        async def delete(self, tid):
            calls.append(('delete', tid))
            return 'deleted'

        def recent(self, limit):
            calls.append(('recent', limit))
            return 'recent'

    ctx = bt.ToolContext(bot=None, group_id='1', sandbox=None, cfg={},
                         db_path='', qzone=_Qz(), sender_id='1',
                         sender_name='主人')
    cases = [
        ('qzone_list', {'limit': 3}, ('list', 3, '')),
        ('qzone_detail', {'tid': 'T1'}, ('detail', 'T1')),
        ('qzone_images', {'tid': 'T1', 'indexes': [0]}, ('images', 'T1', [0])),
        ('qzone_feeds', {'limit': 5}, ('feeds', 5)),
        ('qzone_comment', {'tid': 'T1', 'content': 'hi',
                           'comment_id': 'c1', 'comment_uin': '2'},
         ('comment', 'T1', 'hi')),
        ('qzone_like', {'tid': 'T1'}, ('like', 'T1', True)),
        ('qzone_publish', {'content': 'hi'}, ('publish', 'hi', 1)),
        ('qzone_delete', {'tid': 'T1'}, ('delete', 'T1')),
        ('qzone_recent', {'limit': 4}, ('recent', 4)),
    ]
    for name, args, want in cases:
        out = _run(bt.dispatch(name, args, ctx))
        assert out is not None
        rec = calls[-1]
        assert rec[:len(want)] == want, (name, rec)
    # 回复评论要带成对参数
    kw = calls[4][3]
    assert kw['comment_id'] == 'c1' and kw['comment_uin'] == '2'


# ── 3. 扩展通知策略 ─────────────────────────────────────────────

class _Event:
    def __init__(self, notice_type, sub_type='', **raw):
        self.notice_type = notice_type
        self.sub_type = sub_type
        self._raw = {'notice_type': notice_type, 'sub_type': sub_type, **raw}


def test_profile_like_is_silently_consumed():
    engine_calls = []

    class _Engine:
        async def handle_trigger(self, *a, **kw):
            engine_calls.append((a, kw))
            return {'status': 'ok'}

    plugin = _FakePlugin(cfg={'owner_qq': '10001', 'notice_ext_on': True},
                         engine=_Engine())
    ev = _Event('notify', 'profile_like', user_id=20002,
                operator_id=20002, times=1)
    assert _run(notices.handle(plugin, ev)) is True
    assert engine_calls == []            # 不唤醒模型
    assert plugin.bot.calls == []        # 也不尝试任何回赞动作
    assert not [x for x in plugin.logs if x[0] == 'warn']


def test_poke_backs_only_when_poked_self():
    plugin = _FakePlugin(cfg={'notice_ext_on': True, 'poke_back_on': True,
                              'poke_ai_on': False})
    # 戳的是别人 → 与机器人无关
    assert _run(notices.handle(plugin, _Event(
        'notify', 'poke', group_id='123', user_id='20002',
        target_id='20003'))) is True
    assert plugin.bot.calls == []
    # 戳的是自己 → 回戳
    assert _run(notices.handle(plugin, _Event(
        'notify', 'poke', group_id='123', user_id='20002',
        target_id='10001'))) is True
    action, params = plugin.bot.calls[-1]
    assert action == 'group_poke'
    assert params == {'user_id': 20002, 'group_id': 123}


def test_poke_ai_triggers_turn_only_when_enabled():
    calls = []

    class _Engine:
        async def handle_trigger(self, *a, **kw):
            calls.append(kw)
            return {'status': 'ok'}

    db = _tmp_db()
    storage.upsert_group(db, '123', enabled=True)
    plugin = _FakePlugin(db=db, engine=_Engine(),
                         cfg={'notice_ext_on': True, 'poke_back_on': False,
                              'poke_ai_on': True})
    _run(notices.handle(plugin, _Event(
        'notify', 'poke', group_id='123', user_id='20002',
        target_id='10001')))
    assert len(calls) == 1 and calls[0]['run_kind'] == 'poke'


def test_group_name_notice_syncs_local_name():
    db = _tmp_db()
    storage.get_or_create_group(db, '123')
    plugin = _FakePlugin(db=db, cfg={'notice_ext_on': True,
                                     'group_name_sync_on': True})
    _run(notices.handle(plugin, _Event(
        'notify', 'group_name', group_id='123', name_new='新群名')))
    assert storage.get_group(db, '123')['group_name'] == '新群名'


def test_notice_ext_switch_off_passes_through():
    plugin = _FakePlugin(cfg={'notice_ext_on': False})
    ev = _Event('notify', 'poke', group_id='123', user_id='20002',
                target_id='10001')
    assert _run(notices.handle(plugin, ev)) is False
    assert plugin.bot.calls == []


# ── 4. 框架层 ───────────────────────────────────────────────────

def test_message_sent_is_not_dispatched_as_incoming():
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'core', 'events.py')
    with open(path, encoding='utf-8') as f:
        src = f.read()
    assert "post_type'] == 'message_sent'" in src
    # message_sent 分支必须在 message 分支之前，否则会被当普通消息处理
    assert src.index("'message_sent'") < src.index("post_type'] == 'message'")


def test_storage_v20_tables_and_event_dedupe():
    db = _tmp_db()
    with sqlite3.connect(db) as conn:
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        cols = {r[1] for r in conn.execute('PRAGMA table_info(qzone_posts)')}
    assert version >= 20
    assert {'qzone_events', 'qzone_state'} <= tables
    assert 'images' in cols

    ev = dict(tid='T1', actor_uin='20002', actor_name='小明',
              content='@我', event_time=1764000000)
    assert storage.add_qzone_event(db, 'mention', **ev) is True
    assert storage.add_qzone_event(db, 'mention', **ev) is False   # 去重
    rows = storage.pending_qzone_events(db, limit=10)
    assert len(rows) == 1 and rows[0]['status'] == 'pending'
    storage.mark_qzone_event(db, rows[0]['id'], 'pending', 'boom')
    assert storage.pending_qzone_events(db, limit=10)[0]['attempts'] == 1
    assert storage.get_qzone_state(db, 'baseline_done', '') == ''
    storage.set_qzone_state(db, 'baseline_done', 1)
    assert storage.get_qzone_state(db, 'baseline_done', '') == '1'


def test_qzone_tool_keys_shipped_in_catalog():
    keys = set(builtin_tools.TOOL_KEYS.values())
    for k in ('qzone.list', 'qzone.detail', 'qzone.images', 'qzone.feeds',
              'qzone.comment', 'qzone.like', 'qzone.publish',
              'qzone.delete', 'qzone.recent'):
        assert k in keys
    schemas = builtin_tools.schemas({'qzone.publish', 'qzone.comment'})
    params = {s['function']['name']: s['function']['parameters']
              for s in schemas}
    assert 'images' in params['qzone_publish']['properties']
    assert 'qzone_publish' not in params['qzone_publish'].get('required', [])
    assert 'comment_id' in params['qzone_comment']['properties']
