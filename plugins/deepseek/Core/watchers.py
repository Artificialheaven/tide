# -*- coding: utf-8 -*-
"""AI 托管的外部监测进程（watcher）。

AI 先用 fs_write 把监测脚本写进群沙箱，再用 watcher_start 工具以**分离进程**
启动：脚本不随 bot 重连/插件重载被杀（监测不能中断），通过环境变量拿到回调
地址与 token（脚本里不应硬编码密钥），检测到事件就 POST 回 Core/triggers.py
的触发服务，由后者拉起一次后台 AI 轮。

安全边界：
- 启动/停止属于风险操作，仅主人（引擎 _risk_denied + RISKY_TOOL_KEYS 门禁）；
- 脚本必须位于群沙箱内（Sandbox.resolve 拒绝越界，public 区不许跑）；
- 子进程环境走 coderunner._clean_env()，不带走任何 API key；
- 每群存活进程数有上限；进程不做崩溃重启/开机自启（v1 明确不做）。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

from . import storage
from .coderunner import _clean_env

_DETACHED = 0x00000008          # DETACHED_PROCESS
_NEW_GROUP = 0x00000200         # CREATE_NEW_PROCESS_GROUP
_NO_WINDOW = 0x08000000         # CREATE_NO_WINDOW
_STILL_ACTIVE = 259
_LOG_TAIL_BYTES = 8192
_MAX_ARGS = 10
_ARG_MAX_CHARS = 200


def pid_alive(pid: int) -> bool:
    """进程是否仍在运行。仅探活，不抛异常。"""
    pid = int(pid or 0)
    if pid <= 0:
        return False
    if os.name == 'nt':
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # 回收僵尸（同进程生的子进程才收得到，ECHILD 忽略）
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    return True


def kill_tree(pid: int) -> None:
    """杀整棵进程树。同步调用，可能阻塞数秒（taskkill）。"""
    pid = int(pid or 0)
    if pid <= 0:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                       capture_output=True, timeout=15)
        return
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, getattr(__import__('signal'), 'SIGTERM', 15))
        time.sleep(0.5)
        os.killpg(pgid, getattr(__import__('signal'), 'SIGKILL', 9))
    except ProcessLookupError:
        pass


class WatcherManager:
    def __init__(self, plugin):
        self.p = plugin

    def _log(self, msg, level='info'):
        self.p.log(msg, level)

    # ── 生命周期对账 ───────────────────────────────────────────

    def reconcile(self) -> int:
        """启动期：把残留登记里实际已死的进程标 stopped。返回对账条数。"""
        n = 0
        try:
            rows = storage.list_watchers(self.p.db_path, status='running')
        except Exception:
            return 0
        for r in rows:
            if not pid_alive(r['pid']):
                storage.set_watcher_status(self.p.db_path, r['id'], 'stopped')
                n += 1
        return n

    # ── AI 工具后端 ────────────────────────────────────────────

    async def start_watcher(self, group_id, sender_id, sender_name, *,
                            script: str, args=None, name: str = '') -> str:
        import asyncio
        triggers = getattr(self.p, 'triggers', None)
        if triggers is None or not triggers.is_running():
            return ('错误: 外部触发服务未运行（trigger_on 关闭或端口启动失败），'
                    '监测程序启动了也无法回调')
        gid = str(group_id)
        script = (script or '').strip().replace('\\', '/')
        if not script:
            return '错误: script 必填（先用 fs_write 把 .py 脚本写进群沙箱）'
        if not script.lower().endswith('.py'):
            return '错误: v1 仅支持 Python 脚本（.py）'
        try:
            sb = self.p.sandbox(gid)
            script_abs, is_public = sb.resolve(script)
            log_dir, log_public = sb.resolve('.watchers', for_write=True)
        except Exception as e:
            return f'错误: 脚本路径不合法：{e}'
        if is_public or log_public:
            return '错误: 监测脚本只能放在本群沙箱，不能放 public 共享区'
        if not script_abs.is_file():
            return f'错误: 沙箱里找不到脚本 {script}（先 fs_write 写入）'

        argv_args = []
        if args:
            if isinstance(args, str):
                argv_args = args.split()
            elif isinstance(args, list):
                argv_args = [str(a) for a in args]
            else:
                return '错误: args 必须是字符串数组'
        if len(argv_args) > _MAX_ARGS:
            return f'错误: 参数最多 {_MAX_ARGS} 个'
        if any(len(a) > _ARG_MAX_CHARS for a in argv_args):
            return f'错误: 单个参数最长 {_ARG_MAX_CHARS} 字符'

        name = (name or '').strip()[:40]
        cap = int(self.p.cfg.get('watchers_max_per_group', 5))
        if await asyncio.to_thread(
                storage.count_watchers, self.p.db_path, gid, 'running') >= cap:
            return f'错误: 本群最多 {cap} 个存活监测程序，先 watcher_stop 旧的'

        url = triggers.address()
        token = str(self.p.cfg.get('trigger_token') or '')
        log_dir.mkdir(parents=True, exist_ok=True)

        def _spawn() -> tuple[int, object]:
            ts = int(time.time())
            log_path = log_dir / f'{ts}.log'
            env = _clean_env()
            env.update({
                'TRIGGER_URL': url,
                'TRIGGER_TOKEN': token,
                'TRIGGER_GROUP': gid,
                'TRIGGER_SOURCE': name or f'watcher-{ts}',
                'PYTHONIOENCODING': 'utf-8',
            })
            f = open(log_path, 'wb')
            try:
                kwargs = dict(cwd=str(sb.group_root), env=env,
                              stdin=subprocess.DEVNULL, stdout=f,
                              stderr=subprocess.STDOUT, close_fds=True)
                if os.name == 'nt':
                    kwargs['creationflags'] = _DETACHED | _NEW_GROUP | _NO_WINDOW
                else:
                    kwargs['start_new_session'] = True
                proc = subprocess.Popen(
                    [sys.executable, '-I', '-X', 'utf8',
                     str(script_abs), *argv_args], **kwargs)
            finally:
                f.close()
            return proc.pid, log_path

        try:
            pid, log_path = await asyncio.to_thread(_spawn)
        except Exception as e:
            return f'错误: 监测程序启动失败：{e}'

        wid = await asyncio.to_thread(
            storage.add_watcher, self.p.db_path,
            {'group_id': gid, 'name': name, 'script': script,
             'args_json': __import__('json').dumps(argv_args,
                                                   ensure_ascii=False),
             'pid': pid, 'creator_id': str(sender_id or ''),
             'creator_name': str(sender_name or ''),
             'log_path': str(log_path), 'started_at': time.time()})
        self._log(f'群 {gid} 监测程序 #{wid} 已启动 pid={pid}: {script}', 'ok')
        return (
            f'监测程序已启动（{wid}），pid={pid}。\n'
            f'回调地址已在环境变量 TRIGGER_URL，token 在 TRIGGER_TOKEN（不要写进代码/日志），'
            f'本群号在 TRIGGER_GROUP。\n'
            '脚本里监测到事件后这样回调：\n'
            'requests.post(os.environ["TRIGGER_URL"], timeout=10,\n'
            '  headers={"Authorization": "Bearer "+os.environ["TRIGGER_TOKEN"]},\n'
            '  json={"group_id": os.environ["TRIGGER_GROUP"],\n'
            '        "source": "bili-reply", "text": "事件说明"})\n'
            'watcher_list 查看状态，watcher_logs 看输出，watcher_stop 停止。')

    async def list_watchers(self, group_id='') -> list:
        import asyncio
        rows = await asyncio.to_thread(
            storage.list_watchers, self.p.db_path, str(group_id or ''))
        for r in rows:
            alive = r['status'] == 'running' and await asyncio.to_thread(
                pid_alive, r['pid'])
            if r['status'] == 'running' and not alive:
                await asyncio.to_thread(
                    storage.set_watcher_status, self.p.db_path, r['id'],
                    'stopped')
                r['status'] = 'stopped'
            r['alive'] = alive if r['status'] == 'running' else False
        return rows

    async def stop_watcher(self, watcher_id) -> str:
        import asyncio
        try:
            wid = int(watcher_id)
        except (TypeError, ValueError):
            return '错误: id 必须是数字'
        row = await asyncio.to_thread(storage.get_watcher, self.p.db_path, wid)
        if not row:
            return '未找到该监测程序'
        if row['status'] == 'running':
            await asyncio.to_thread(kill_tree, row['pid'])
            await asyncio.to_thread(
                storage.set_watcher_status, self.p.db_path, wid, 'stopped',
                time.time())
        self._log(f'监测程序 #{wid}（pid={row["pid"]}）已停止', 'ok')
        return f'监测程序 {wid} 已停止'

    async def logs(self, watcher_id, max_chars: int = 4000) -> str:
        import asyncio
        try:
            wid = int(watcher_id)
        except (TypeError, ValueError):
            return '错误: id 必须是数字'
        row = await asyncio.to_thread(storage.get_watcher, self.p.db_path, wid)
        if not row:
            return '未找到该监测程序'
        log_path = row.get('log_path') or ''

        def _read() -> str:
            p = Path(log_path)
            if not log_path or not p.is_file():
                return ''
            with open(p, 'rb') as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - _LOG_TAIL_BYTES))
                return f.read().decode('utf-8', errors='replace')

        text = await asyncio.to_thread(_read)
        text = text.strip()
        if not text:
            return f'监测程序 {wid} 还没有任何输出'
        return text[-max(200, int(max_chars)):]
