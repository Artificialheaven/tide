# -*- coding: utf-8 -*-
"""实时事件订阅服务（SSE）+ 弹幕机/调试台静态页（自持轻量 HTTP 服务）。

与 triggers.py 同范式：标准库 ThreadingHTTPServer 起在独立守护线程，默认只绑
127.0.0.1，复用 trigger_token 鉴权（EventSource 不能自定义头，token 走 query）。
不用插件 Flask 蓝图：框架强加 session 登录（OBS/EventSource 拿不到 cookie），
且 GUI 模式下 Web 后台可能没启动。

路由：
  GET /live/healthz          健康检查，不需 token
  GET /live/events           SSE 事件流（?token=&groups=&kinds=&include_prompt=1，
                             支持 Last-Event-ID 请求头断线补播，~20s 心跳）
  GET /live/danmaku          弹幕机页面（全屏透明叠加层，可做 OBS 浏览器源）
  GET /live/debug            实时调试台（完整思考链/工具 IO/本轮 prompt）

安全：接口纯只读；默认 loopback。改成 0.0.0.0 等于让局域网内任何持 token 者
实时读取全部群聊、思考链、工具参数与文件内容。
"""
from __future__ import annotations

import hmac
import json
import os
import queue
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .live_bus import CLOSE, EVENT_KINDS, LiveBus
from .version import VERSION

_DIR = os.path.dirname(os.path.abspath(__file__))
_PAGE_DIR = os.path.join(_DIR, '..', 'templates', 'live')
_PAGES = {'danmaku': 'danmaku.html', 'debug': 'debug.html'}

# 事件长字段截断（字符）：调试台可接受较长正文，但不把整个工作区文件灌进浏览器
_CLIP = {
    'reasoning': 4000,
    'result': 1000,
    'prompt_view': 8000,
    'text': 4000,
    'content': 4000,
    # 工具参数：tool_result 事件里是 dict，emit 里序列化后按此上限截断
    'arguments': 2000,
}


class LiveManager:
    """实时订阅服务生命周期 + 对外发布口。挂在 plugin.live。"""

    def __init__(self, plugin, log=None):
        self.p = plugin
        self._log = log or (lambda msg, level='info': None)
        self.bus = LiveBus()
        self._server: LiveServer | None = None

    # ── 生命周期 ───────────────────────────────────────────────

    def start(self) -> None:
        # 重入守卫：on_start 可能被重复触发（断线重连/热重载）。已有实例
        # 必须原样保留——否则新实例绑定同一端口会 OSError，随后把
        # self._server 置 None，看似已停、实际旧 server 线程仍在跑。
        if self._server is not None:
            thread = getattr(self._server, '_thread', None)
            if thread is None or thread.is_alive():
                self._log('实时订阅服务已在运行，忽略重复启动', 'warn')
                return
            # 线程已退出（极少见）：先关掉残留监听 socket，再重建
            try:
                self._server.stop()
            except Exception:
                pass
            self._server = None
        cfg = self.p.cfg
        if not cfg.get('live_on', True):
            self._log('实时订阅服务已关闭（live_on=False）', 'warn')
            return
        host = str(cfg.get('live_host', '127.0.0.1') or '127.0.0.1')
        port = int(cfg.get('live_port', 8095) or 8095)
        if not cfg.get('trigger_token'):
            self._log('实时订阅服务复用触发 token，token 为空：拒绝启动'
                      '（请在设置页生成触发 token）', 'error')
            return
        try:
            server = LiveServer(self, host, port)
            server.start()
            self._server = server
            self._log(f'实时订阅服务已监听 http://{host}:{server.port()}'
                      f'/live/danmaku', 'ok')
        except OSError as e:
            # 走到这里 _server 只可能是 None（绑定失败在构造阶段抛出），
            # 不会破坏上面守卫保留的已有实例
            self._server = None
            self._log(f'实时订阅服务启动失败（端口可能被占用）: {e}', 'error')

    def stop(self) -> None:
        if self._server is not None:
            try:
                self.bus.close_all()
                self._server.stop()
            except Exception:
                pass
            self._server = None

    def is_running(self) -> bool:
        return self._server is not None

    def port(self) -> int:
        return self._server.port() if self._server is not None else 0

    def page_urls(self) -> dict:
        """设置页展示用：0.0.0.0 替换成 127.0.0.1（浏览器访问本机口）。"""
        cfg = self.p.cfg
        host = str(cfg.get('live_host', '127.0.0.1') or '127.0.0.1')
        shown = '127.0.0.1' if host in ('0.0.0.0', '::', '') else host
        port = self.port() or int(cfg.get('live_port', 8095) or 8095)
        base = f'http://{shown}:{port}/live'
        return {'danmaku': base + '/danmaku', 'debug': base + '/debug'}

    # ── HTTP 侧（工作线程）─────────────────────────────────────

    def check_token(self, presented: str) -> bool:
        expected = str(self.p.cfg.get('trigger_token') or '')
        return bool(expected) and bool(presented) and hmac.compare_digest(
            presented, expected)

    def state_payload(self) -> dict:
        """/live/api/state：页面拉群列表填下拉框（只读，HTTP 线程调用，
        storage 每次调用开独立连接，跨线程安全）。"""
        groups = []
        try:
            from . import storage
            for g in storage.list_groups(self.p.db_path)[:300]:
                groups.append({
                    'group_id': str(g.get('group_id') or ''),
                    'enabled': bool(g.get('enabled')),
                    'persona': str(g.get('persona') or '')[:60],
                    'last_active': g.get('last_active') or 0,
                })
        except Exception:
            pass
        # 「固定投递会话」：QQ 空间知会没有群，投递目标是 qzone_notify_group
        # 或主人私聊。调试台要能直接按它过滤，所以这里显式补进群列表并打标。
        delivery = None
        try:
            from .qzone_watch import notify_target
            dgid, dchat = notify_target(self.p.cfg)
            if dgid:
                delivery = {
                    'group_id': dgid,
                    'chat_type': dchat,
                    'label': ('空间知会 → 主人私聊' if dchat == 'private'
                              else '空间知会 → 投递群'),
                }
                hit = False
                for g in groups:
                    if g['group_id'] == dgid:
                        g['delivery'] = True
                        g['chat_type'] = dchat
                        hit = True
                        break
                if not hit:
                    groups.insert(0, {
                        'group_id': dgid, 'enabled': True, 'persona': '',
                        'last_active': 0, 'delivery': True,
                        'chat_type': dchat,
                    })
        except Exception:
            delivery = None
        return {'ok': True, 'version': VERSION,
                'subscribers': self.bus.count(), 'groups': groups,
                'delivery': delivery}

    def history_payload(self, groups=None, kinds=None, before=0,
                        limit: int = 200, include_prompt: bool = False) -> dict:
        """/live/api/events：按 seq 游标回看 ring 里的历史事件。

        调试台打开时先拉最近一批，滚动到顶再按 before=最早 seq 往前翻。
        事件只在进程内 ring（1000 条）里，重启即清空；持久化归档不在本次
        范围（要的话得落库，那是另一套东西）。
        """
        from .live_bus import Subscription
        g = {str(x).strip() for x in (groups or []) if str(x).strip()}
        k = {str(x).strip() for x in (kinds or []) if str(x).strip()}
        sub = Subscription(g, k, bool(include_prompt))
        limit = max(1, min(int(limit or 200), 500))
        # 多取一条来判断「还有没有更早的」：ring 是环形的，
        # 若只把 seq 和全局 oldest 比，按群/类型过滤时会误报（例如更早的
        # 事件都属别的群，前端仍显示「加载更早」却每次拿到空）
        evs = self.bus.older(before, limit + 1, sub)
        has_more = len(evs) > limit
        if has_more:
            evs = evs[1:]
        return {'ok': True, 'events': evs, 'count': len(evs),
                'oldest_seq': self.bus.oldest_seq(),
                'latest_seq': self.bus.latest_seq(),
                'has_more': has_more}

    # ── 引擎侧（asyncio 线程）──────────────────────────────────

    def emit(self, kind: str, group_id='', chat_type: str = 'group',
             turn_id: str = '', **fields) -> None:
        """发布一个事件。截断长字段并吞掉一切异常（观测不得影响对话）。"""
        try:
            self._clip_fields(fields)
            self.bus.publish(
                kind, group_id=group_id, chat_type=chat_type,
                turn_id=turn_id, **fields)
        except Exception:
            pass

    @staticmethod
    def _clip_fields(fields: dict) -> None:
        """按 `_CLIP` 原地截断长字段。

        字符串按字符数截断；dict/list（如工具调用 arguments）先 JSON
        序列化再截断，避免一个超大 tool_result 把单事件撑到几十 KB。
        """
        for key, limit in _CLIP.items():
            v = fields.get(key)
            if isinstance(v, str):
                text = v
            elif isinstance(v, (dict, list)):
                try:
                    text = json.dumps(v, ensure_ascii=False)
                except (TypeError, ValueError):
                    continue
            else:
                continue
            if len(text) > limit:
                fields[key] = text[:limit] + f'…(+{len(text) - limit}字)'


class LiveServer:
    """标准库 HTTP 服务（守护线程，随进程退出）。"""

    def __init__(self, manager: LiveManager, host: str, port: int):
        mgr = manager

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def _reply_json(self, code: int, payload: dict):
                data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type',
                                 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(data)

            def _reply_html(self, path: str, code: int = 200,
                           msg: str = 'not found'):
                if code != 200:
                    data = msg.encode('utf-8')
                    self.send_response(code)
                    self.send_header('Content-Type', 'text/plain; charset=utf-8')
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(data)
                    return
                try:
                    with open(path, 'rb') as f:
                        data = f.read()
                except OSError:
                    return self._reply_html(path, code=500, msg='page missing')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
                if path == '/live/healthz':
                    return self._reply_json(
                        200, {'ok': True, 'service': 'deepseek-live'})
                if path == '/live/danmaku':
                    return self._reply_html(
                        os.path.join(_PAGE_DIR, _PAGES['danmaku']))
                if path == '/live/debug':
                    return self._reply_html(
                        os.path.join(_PAGE_DIR, _PAGES['debug']))
                if path == '/live/events':
                    return self._sse(qs)
                if path == '/live/api/state':
                    token = (qs.get('token') or [''])[0].strip()
                    if not mgr.check_token(token):
                        return self._reply_json(
                            401, {'ok': False, 'error': 'token 无效或缺失'})
                    return self._reply_json(200, mgr.state_payload())
                if path == '/live/api/events':
                    token = (qs.get('token') or [''])[0].strip()
                    if not mgr.check_token(token):
                        return self._reply_json(
                            401, {'ok': False, 'error': 'token 无效或缺失'})
                    def _csv(name):
                        return [x for x in (qs.get(name) or [''])[0]
                                .split(',') if x.strip()]
                    try:
                        before = int((qs.get('before') or ['0'])[0] or 0)
                    except (TypeError, ValueError):
                        before = 0
                    try:
                        limit = int((qs.get('limit') or ['200'])[0] or 200)
                    except (TypeError, ValueError):
                        limit = 200
                    include_prompt = (qs.get('include_prompt') or [''])[0] \
                        in ('1', 'true', 'yes')
                    return self._reply_json(200, mgr.history_payload(
                        groups=_csv('groups'), kinds=_csv('kinds'),
                        before=before, limit=limit,
                        include_prompt=include_prompt))
                self._reply_json(404, {'ok': False, 'error': 'not found'})

            def _sse(self, qs):
                token = (qs.get('token') or [''])[0].strip()
                # 方便 curl 调试：query 没有时也认 Authorization 头
                if not token:
                    auth = self.headers.get('Authorization', '')
                    if auth.lower().startswith('bearer '):
                        token = auth[7:].strip()
                if not mgr.check_token(token):
                    return self._reply_json(
                        401, {'ok': False, 'error': 'token 无效或缺失'})
                groups = [g for g in (qs.get('groups') or [''])[0].split(',')
                          if g.strip()]
                kinds = [k for k in (qs.get('kinds') or [''])[0].split(',')
                         if k.strip()]
                bad = [k for k in kinds if k not in EVENT_KINDS]
                if bad:
                    return self._reply_json(
                        400, {'ok': False,
                              'error': f'未知事件类型：{",".join(bad)}'})
                include_prompt = (qs.get('include_prompt') or [''])[0] \
                    in ('1', 'true', 'yes')

                # 订阅与 header 写入都放进 try：客户端在 header 阶段就断开时，
                # finally 同样要退订，否则 Subscription 永久留在 LiveBus._subs
                # （队列 200 条 × 单事件最大 ~21k 字符 ⇒ 4–12MB/个），此后每次
                # publish 还会白遍历它。
                sub = None
                try:
                    # wfile 写超时：黑洞/极慢客户端不能把 handler 线程卡在 TCP
                    # 重传上（线程闭包持有 LiveManager → 整个旧插件实例）。
                    self.connection.settimeout(30)
                    sub = mgr.bus.subscribe(
                        groups=groups, kinds=kinds,
                        include_prompt=include_prompt)
                    self.send_response(200)
                    self.send_header('Content-Type',
                                     'text/event-stream; charset=utf-8')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(b'retry: 3000\n\n')
                    # 建连问候帧：回显服务端实际生效的过滤条件，页面据此区分
                    # 「已连接但当前群没动静」与「根本没连上/token 失效」
                    self._write_event(
                        {'kind': 'meta', 'hello': True, 'groups': groups,
                         'include_prompt': include_prompt, 'version': VERSION},
                        with_id=False)
                    # 断线补播（ring 重放）
                    for ev in mgr.bus.replay(
                            self.headers.get('Last-Event-ID'), sub):
                        self._write_event(ev)
                    while True:
                        try:
                            item = sub.queue.get(timeout=20)
                        except queue.Empty:
                            # 注释行心跳，保活兼探活
                            self.wfile.write(b': ping\n\n')
                            self.wfile.flush()
                            continue
                        if item is CLOSE:
                            break
                        if sub.dropped:
                            n, sub.dropped = sub.dropped, 0
                            self._write_event(
                                {'seq': 0, 'kind': 'meta',
                                 'dropped': n,
                                 'note': f'消费者过慢，已丢弃 {n} 个旧事件'},
                                with_id=False)
                        self._write_event(item)
                except (socket.timeout, BrokenPipeError,
                        ConnectionResetError, OSError):
                    # 客户端断开 / 写超时：静默退出，finally 负责退订
                    pass
                finally:
                    if sub is not None:
                        mgr.bus.unsubscribe(sub)

            def _write_event(self, ev: dict, with_id: bool = True):
                seq = ev.get('seq') or 0
                head = f'id: {seq}\n' if with_id and seq else ''
                kind = ev.get('kind') or 'message'
                data = json.dumps(ev, ensure_ascii=False)
                # data 必须单行（JSON 内不会出裸换行，保险替换）
                self.wfile.write(
                    f'{head}event: {kind}\ndata: {data}\n\n'
                    .encode('utf-8'))
                self.wfile.flush()

        self._httpd = ThreadingHTTPServer((host, int(port)), _Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name='deepseek-live',
            daemon=True)

    def start(self):
        self._thread.start()

    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=3)
