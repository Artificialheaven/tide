# -*- coding: utf-8 -*-
"""外部监测程序 → AI 后台任务的触发入口（自持轻量 HTTP 服务）。

为什么不用插件 Web 蓝图：框架给蓝图强加 session 登录鉴权（外部脚本拿不到
cookie），且 GUI 模式下 Web 后台可能根本没启动。这里用标准库
ThreadingHTTPServer 起在独立守护线程，默认只绑 127.0.0.1，Bearer token
鉴权，不依赖框架 Web 管理面。

协议：POST /trigger
    Headers: Authorization: Bearer <trigger_token>  或  X-Trigger-Token
    JSON:    {"group_id": "群号", "text": "事件内容", "source": "程序名(可选)"}
    成功 202 {"ok": true}（AI 后台轮异步执行，不同步等待）
其余：401 鉴权失败 / 400 参数问题 / 404 群不存在 / 403 群未开启 /
      429 冷却中 / 503 事件循环未运行。

GET /trigger 是健康检查，不需 token。
"""
from __future__ import annotations

import datetime as _dt
import hmac
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import storage
from .config import resolve_group

MAX_BODY_BYTES = 65536
WEEK_CN = ('一', '二', '三', '四', '五', '六', '日')
_SOURCE_RE = re.compile(r'^[0-9A-Za-z_一-鿿.\-]{1,40}$')


def trigger_user_text(now: _dt.datetime, source: str, text: str) -> str:
    """合成给模型的外部事件通知（allow_silent 轮，与定时苏醒同契约）。"""
    src = f'监测程序「{source.strip()}」报告：\n' if source and source.strip() else ''
    return (
        f'[外部触发] 现在是 {now.strftime("%Y-%m-%d %H:%M")} '
        f'星期{WEEK_CN[now.weekday()]}，群里暂时没有人 @ 你，'
        f'这是你让外部监测程序盯梢的事件经自动触发接口推送的，'
        '不是任何群友发的消息。\n'
        f'{src}'
        f'事件内容：{text.strip()}\n'
        '结合未结约定、长期记忆和自我提示词注入表判断：'
        '这件事值不值得现在在群里自然开口（通报进展、@相关群友、提醒等）？\n'
        '- 值得就像普通群友一样自然开口，简短，别解释「触发/接口/监测程序」这套机制；\n'
        '- 没必要说话（小事、重复、半夜打扰）时，整条回复只输出 [静默]'
        '（连同方括号四个字符），不要带任何其他内容。')


class TriggerManager:
    """触发服务生命周期 + 事件入队。所有方法对插件无侵入（鸭子类型）。"""

    def __init__(self, plugin, log=None):
        self.p = plugin
        self._log = log or (lambda msg, level='info': None)
        self._server: TriggerServer | None = None
        self._last_fire: dict[tuple, float] = {}
        self._lock = threading.Lock()

    # ── 生命周期 ───────────────────────────────────────────────

    def start(self) -> None:
        cfg = self.p.cfg
        if not cfg.get('trigger_on', True):
            self._log('外部触发服务已关闭（trigger_on=False）', 'warn')
            return
        host = str(cfg.get('trigger_host', '127.0.0.1') or '127.0.0.1')
        port = int(cfg.get('trigger_port', 8094) or 8094)
        if not cfg.get('trigger_token'):
            self._log('外部触发 token 为空：拒绝启动（请在配置中生成）', 'error')
            return
        try:
            self._server = TriggerServer(self, host, port)
            self._server.start()
            self._log(f'外部触发服务已监听 http://{host}:{self._server.port()}/trigger',
                      'ok')
        except OSError as e:
            self._server = None
            self._log(f'外部触发服务启动失败（端口可能被占用）: {e}', 'error')

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.stop()
            except Exception:
                pass
            self._server = None

    def address(self) -> str:
        if self._server is None:
            return ''
        host = str(self.p.cfg.get('trigger_host', '127.0.0.1'))
        return f'http://{host}:{self._server.port()}/trigger'

    def is_running(self) -> bool:
        return self._server is not None

    # ── HTTP 侧调用（工作线程）─────────────────────────────────

    def check_token(self, presented: str) -> bool:
        expected = str(self.p.cfg.get('trigger_token') or '')
        return bool(expected) and bool(presented) and hmac.compare_digest(
            presented, expected)

    def admit(self, gid: str, source: str) -> tuple[bool, str]:
        """群有效性 + 冷却检查。返回 (是否放行, 原因/HTTP 错误短语)。"""
        row = storage.get_group(self.p.db_path, str(gid))
        if row is None:
            return False, 'unknown_group'
        if not row['enabled']:
            return False, 'group_disabled'
        cooldown = max(0, int(self.p.cfg.get('trigger_cooldown_seconds', 30)))
        key = (str(gid), source or '')
        now = time.time()
        with self._lock:
            last = self._last_fire.get(key, 0)
            if cooldown and now - last < cooldown:
                return False, 'cooldown'
            self._last_fire[key] = now
        return True, ''

    def schedule_fire(self, gid: str, text: str, source: str) -> bool:
        """把后台 AI 轮排到 bot 的 asyncio 循环。成功入队返回 True。"""
        from core import globals
        loop = getattr(globals, 'event_loop', None)
        if loop is None or not loop.is_running():
            return False
        loop.call_soon_threadsafe(self._spawn, gid, text, source)
        return True

    def _spawn(self, gid: str, text: str, source: str) -> None:
        import asyncio
        asyncio.create_task(self.fire(str(gid), text, source))

    async def fire(self, gid: str, text: str, source: str = '') -> None:
        """在 asyncio 线程执行：锁内重检后跑后台轮。异常只记录不外泄。"""
        try:
            row = storage.get_group(self.p.db_path, gid)
            if row is None or not row['enabled']:
                return
            cfg = resolve_group(self.p.cfg, row.get('settings'))
            async with self.p.group_lock(gid):
                await self.p.engine.handle_trigger(gid, cfg, text, source)
        except Exception:
            import traceback
            self._log('外部触发后台轮异常:\n' + traceback.format_exc(), 'error')

    # ── 请求处理（供 TriggerServer 调用）──────────────────────

    def handle_request(self, body: bytes, headers) -> tuple[int, dict]:
        """HTTP 层无关的纯处理：返回 (HTTP 状态码, JSON dict)。"""
        token = headers.get('Authorization', '')
        if token.lower().startswith('bearer '):
            token = token[7:].strip()
        else:
            token = headers.get('X-Trigger-Token', '')
        if not self.check_token(token):
            return 401, {'ok': False, 'error': 'token 无效或缺失'}
        try:
            payload = json.loads(body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return 400, {'ok': False, 'error': '请求体不是合法 JSON'}
        if not isinstance(payload, dict):
            return 400, {'ok': False, 'error': '请求体必须是 JSON 对象'}

        gid = str(payload.get('group_id') or '').strip()
        text = str(payload.get('text') or '').strip()
        source = str(payload.get('source') or '').strip()
        if not gid or not gid.isdigit():
            return 400, {'ok': False, 'error': 'group_id 必填且必须是数字群号'}
        if not text:
            return 400, {'ok': False, 'error': 'text 必填（监测到的事件内容）'}
        cap = int(self.p.cfg.get('trigger_text_max_chars', 2000))
        if len(text) > cap:
            return 400, {'ok': False, 'error': f'text 最长 {cap} 字'}
        if source and not _SOURCE_RE.match(source):
            return 400, {'ok': False,
                         'error': 'source 限 40 字符内的中英文/数字/_-.'}

        ok, reason = self.admit(gid, source)
        if not ok:
            code = {'unknown_group': 404, 'group_disabled': 403,
                    'cooldown': 429}.get(reason, 403)
            msg = {'unknown_group': '该群不存在或机器人未被启用',
                   'group_disabled': '该群尚未开启机器人',
                   'cooldown': '冷却中：同类事件刚触发过，请稍后再试'}.get(
                reason, reason)
            return code, {'ok': False, 'error': msg}

        if not self.schedule_fire(gid, text, source):
            # 冷却已记，入队失败要让出，避免下条正常事件被冷却挡掉
            with self._lock:
                self._last_fire.pop((gid, source or ''), None)
            return 503, {'ok': False, 'error': 'bot 事件循环未运行'}
        return 202, {'ok': True, 'queued': True}


class TriggerServer:
    """标准库 HTTP 服务（守护线程，随进程退出）。"""

    def __init__(self, manager: TriggerManager, host: str, port: int):
        mgr = manager

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def _reply(self, code: int, payload: dict):
                data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type',
                                 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.split('?')[0] == '/trigger':
                    self._reply(200, {'ok': True,
                                      'service': 'deepseek-trigger'})
                else:
                    self._reply(404, {'ok': False, 'error': 'not found'})

            def do_POST(self):
                if self.path.split('?')[0] != '/trigger':
                    self._reply(404, {'ok': False, 'error': 'not found'})
                    return
                try:
                    length = int(self.headers.get('Content-Length', 0))
                except ValueError:
                    length = 0
                if length <= 0 or length > MAX_BODY_BYTES:
                    self._reply(400, {'ok': False,
                                      'error': '请求体为空或超过 64KiB 上限'})
                    return
                body = self.rfile.read(length)
                code, payload = mgr.handle_request(body, self.headers)
                self._reply(code, payload)

        self._httpd = ThreadingHTTPServer((host, int(port)), _Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name='deepseek-trigger',
            daemon=True)

    def start(self):
        self._thread.start()

    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=3)
