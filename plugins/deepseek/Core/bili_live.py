# -*- coding: utf-8 -*-
"""B 站直播 AI 子进程管理（M3，docs/bilibili-live-plan.md 第 7 节）。

面板经 DB + 分离进程控制 scripts/bili_live_ai.py：
- 启动：DETACHED_PROCESS 起独立 Python（不随 bot 重连/插件重载被杀），
  stdout 落 db/live_logs/，先建 live_sessions 行再把 session id 传进去；
- 停止：置 live_rooms.enabled=0（软停止），脚本轮询到后自己冲刷队列、
  写复盘占位再退出；进程不听话时面板可 kill_tree 强杀；
- 对账：running 场次结合 pid 存活 + 心跳新鲜度判定，崩溃的置 crashed。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from . import storage
from .watchers import kill_tree, pid_alive

# __file__ = <repo>/plugins/deepseek/Core/bili_live.py
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # plugins/deepseek
_REPO_ROOT = os.path.dirname(os.path.dirname(_PLUGIN_DIR))                 # 仓库根
_SCRIPT = os.path.join(_REPO_ROOT, 'scripts', 'bili_live_ai.py')
_LOG_DIR = os.path.join(_PLUGIN_DIR, 'db', 'live_logs')

_DETACHED = 0x00000008          # DETACHED_PROCESS
_NEW_GROUP = 0x00000200         # CREATE_NEW_PROCESS_GROUP
_NO_WINDOW = 0x08000000         # CREATE_NO_WINDOW
_STALE_SEC = 30.0
_LOG_TAIL_BYTES = 16384


class BiliLiveManager:
    def __init__(self, plugin):
        self.p = plugin

    @property
    def db_path(self) -> str:
        return self.p.db_path if self.p is not None else \
            os.path.join(_PLUGIN_DIR, 'db', 'deepseek.db')

    def log(self, msg, level='info'):
        if self.p is not None:
            self.p.log(msg, level)

    # ── 房间 ───────────────────────────────────────────────────

    async def resolve_room(self, display_id) -> int:
        """短号 → 真实 room_id（面板添加房间时触网校验）。"""
        from bilibili_api import Credential
        from bilibili_api.live import LiveRoom
        from . import bili_auth
        cred = bili_auth.load_credential() or Credential()
        room = LiveRoom(room_display_id=int(display_id), credential=cred)
        return int(await room.get_room_id())

    def add_room(self, real_id: int) -> dict:
        row = storage.upsert_live_room(self.db_path, str(real_id))
        # 人设/历史所在的群行一并建好
        storage.get_or_create_group(self.db_path, f'bili:{real_id}')
        return row

    # ── 对账/状态 ──────────────────────────────────────────────

    def _reconcile(self, sessions: list[dict]) -> None:
        now = time.time()
        for s in sessions:
            if s['status'] != 'running':
                continue
            pid = int((s.get('stats') or {}).get('pid') or 0)
            if pid and not pid_alive(pid):
                storage.end_live_session(self.db_path, s['id'],
                                         status='crashed')
                s['status'] = 'crashed'
                self.log(f'直播脚本 pid={pid} 已死亡，场次 #{s["id"]} 标记 crashed',
                         'warn')

    def snapshot(self) -> list[dict]:
        """每个直播房间 + 最近场次 + 人设 + 实时判定。"""
        rooms = storage.list_live_rooms(self.db_path)
        out = []
        now = time.time()
        for r in rooms:
            sess = storage.list_live_sessions(
                self.db_path, room_id=r['room_id'], limit=1)
            sess = sess[0] if sess else None
            self._reconcile([sess] if sess else [])
            running = bool(sess and sess['status'] == 'running')
            stale = False
            can_force_stop = False
            if running:
                age = now - float(sess.get('heartbeat_at') or 0)
                stale = age > _STALE_SEC
                can_force_stop = (r['enabled'] == 0) or age > _STALE_SEC
            grow = storage.get_group(self.db_path, f'bili:{r["room_id"]}')
            out.append({
                'room': r,
                'session': sess,
                'running': running,
                'stale': stale,
                'can_force_stop': can_force_stop,
                'pid': int((sess or {}).get('stats', {}).get('pid') or 0)
                if sess else 0,
                'persona': (grow or {}).get('persona', ''),
            })
        return out

    # ── 启停 ───────────────────────────────────────────────────

    def start(self, room_id) -> dict:
        room_id = str(room_id)
        row = storage.get_live_room(self.db_path, room_id)
        if row is None:
            raise RuntimeError('房间未添加，请先添加房间')
        if not os.path.isfile(_SCRIPT):
            raise RuntimeError(f'找不到直播脚本 {_SCRIPT}')
        existing = storage.running_live_session(self.db_path, room_id)
        if existing:
            pid = int((existing.get('stats') or {}).get('pid') or 0)
            if pid and pid_alive(pid):
                raise RuntimeError(f'该房间直播脚本已在运行（pid={pid}）')
        storage.upsert_live_room(self.db_path, room_id, enabled=1)
        sid = storage.create_live_session(
            self.db_path, room_id,
            {'pid': 0, 'started_by': 'panel', 'script': _SCRIPT})

        Path(_LOG_DIR).mkdir(parents=True, exist_ok=True)
        log_path = os.path.join(
            _LOG_DIR, f'{room_id}-{int(time.time())}.log')
        argv = [sys.executable, '-X', 'utf8', _SCRIPT,
                '--room', room_id, '--managed', '--session-id', str(sid)]
        f = open(log_path, 'wb')
        try:
            env = dict(os.environ)
            env['PYTHONIOENCODING'] = 'utf-8'
            kwargs = dict(cwd=_REPO_ROOT, env=env,
                          stdin=subprocess.DEVNULL, stdout=f,
                          stderr=subprocess.STDOUT, close_fds=True)
            if os.name == 'nt':
                kwargs['creationflags'] = _DETACHED | _NEW_GROUP | _NO_WINDOW
            else:
                kwargs['start_new_session'] = True
            proc = subprocess.Popen(argv, **kwargs)
        finally:
            f.close()
        storage.heartbeat_live_session(
            self.db_path, sid,
            {'pid': proc.pid, 'log_path': log_path,
             'argv': ' '.join(argv[1:])})
        self.log(f'B 站直播 {room_id} 已启动 pid={proc.pid} sid=#{sid}', 'ok')
        return {'session_id': sid, 'pid': proc.pid, 'log_path': log_path}

    def request_stop(self, room_id) -> None:
        """软停止：置标志位，等脚本自己收尾退出。"""
        storage.upsert_live_room(self.db_path, str(room_id), enabled=0)
        self.log(f'B 站直播 {room_id} 已请求停止（等待脚本收尾）')

    def force_stop(self, room_id) -> bool:
        sess = storage.running_live_session(self.db_path, str(room_id))
        if not sess:
            storage.upsert_live_room(self.db_path, str(room_id), enabled=0)
            return False
        pid = int((sess.get('stats') or {}).get('pid') or 0)
        if pid:
            kill_tree(pid)
        storage.end_live_session(self.db_path, sess['id'], status='stopped')
        storage.upsert_live_room(self.db_path, str(room_id), enabled=0)
        self.log(f'B 站直播 {room_id} 已强制停止 pid={pid}', 'warn')
        return True

    _WRAPUP_SYS = (
        '你是 AI 主播的直播复盘助手。下面是一场 B 站直播的完整弹幕记录'
        '（观众弹幕与主播回复混排）。请写一份 300~500 字的中文复盘，包含：'
        '1）今晚聊了哪些话题、观众反应如何；2）有意思的互动和梗；'
        '3）她答应过观众、下次该记得或兑现的事；4）下次直播可以延续的话题。'
        '只写复盘正文，口语化分点，不要寒暄和标题。')

    async def resummarize(self, session_id: int) -> dict:
        """对历史场次重跑复盘（M4）：取该场次时间窗内的消息，走便宜模型。"""
        from . import config as cfg_mod
        sess = storage.get_live_session(self.db_path, int(session_id))
        if sess is None:
            raise RuntimeError('场次不存在')
        room_id = str(sess['room_id'])
        gid = f'bili:{room_id}'
        end_ts = float(sess.get('ended_at') or 0) or time.time()
        rows, _ = storage.query_messages(
            self.db_path, gid,
            start_ts=float(sess.get('started_at') or 0) - 60,
            end_ts=end_ts + 60, limit=400)
        if not rows:
            raise RuntimeError('该场次没有可复盘的对话')
        lines = [r['content'] if r['role'] == 'user' else f'她：{r["content"]}'
                 for r in rows]
        script = '\n'.join(lines)[-12000:]
        p = self.p
        if p is None:
            raise RuntimeError('插件未运行，无法调 LLM')
        grow = storage.get_group(self.db_path, gid) or {}
        cfg = cfg_mod.resolve_group(p.cfg, grow.get('settings'))
        result = await p.llm.chat(
            [{'role': 'system', 'content': self._WRAPUP_SYS},
             {'role': 'user', 'content': script}],
            cfg, route='summary_model', max_tokens=700,
            temperature=0.3, kind='summary',
            no_thinking=not cfg.get('cheap_thinking_on', False))
        summary = (result.content or '').strip()
        if not summary:
            raise RuntimeError('模型返回了空复盘')
        storage.set_live_session_summary(self.db_path, sess['id'], summary)
        return {'summary': summary}

    def log_tail(self, session_id: int, max_chars: int = 6000) -> str:
        s = storage.get_live_session(self.db_path, int(session_id))
        if not s:
            return ''
        path = (s.get('stats') or {}).get('log_path') or ''
        p = Path(path)
        if not path or not p.is_file():
            return ''
        with open(p, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _LOG_TAIL_BYTES))
            return f.read().decode('utf-8', errors='replace')[-max_chars:]
