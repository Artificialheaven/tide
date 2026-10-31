"""主人专属代码执行：独立子进程跑 Python / Shell。

安全边界（刻意从简，靠流程而非 OS 沙箱）：
- 引擎层 owner-only 门禁（builtin_tools.RISKY_TOOL_KEYS），非主人无法触发；
- 每次执行起独立子进程：sys.exit / 段错误 / 死循环只杀子进程，框架不受影响；
- Python 用 -I 隔离模式（无 PYTHONPATH、无用户 site、cwd 不自动入 sys.path），
  需要的第三方库通过 preamble 显式 sys.path.insert 插件 lib；
- 超时杀整棵进程树（Windows taskkill /T）；环境变量清掉已知密钥；
- cwd 钉死在群沙箱 code/ 目录。
同机同用户权限，不做网络隔离——owner-only 是唯一硬门禁，别在群设置里关掉它。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

# 子进程环境里抹掉的密钥/路径（大写匹配）
_STRIP_ENV = {
    'DEEPSEEK_API_KEY', 'SILICONFLOW_API_KEY', 'AI_API_KEY',
    'AI_ALLOWED_QQS', 'BILI_SESSDATA', 'BILI_BILI_JCT', 'PYTHONPATH',
}


def plugin_lib_dir() -> str:
    """插件自带 lib（框架 pip install --target 的目录）。"""
    return str(Path(__file__).resolve().parent.parent / 'lib')


def _clean_env() -> dict:
    env = dict(os.environ)
    for k in list(env):
        if k.upper() in _STRIP_ENV:
            env.pop(k, None)
    return env


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                           capture_output=True, timeout=10)
        else:
            proc.kill()
    except Exception:
        with __import__('contextlib').suppress(Exception):
            proc.kill()


def run(code: str, kind: str, cwd: str, timeout: float = 30,
        extra_paths: list[str] | None = None) -> dict:
    """执行一段代码/命令。返回 {rc, stdout(bytes), stderr(bytes), timed_out, elapsed}。"""
    Path(cwd).mkdir(parents=True, exist_ok=True)
    env = _clean_env()
    if kind == 'python':
        paths = [p.replace('\\', '\\\\') for p in (extra_paths or [])
                 if p and Path(p).exists()]
        preamble = 'import sys as _sys\n'
        for p in paths:
            preamble += f'_sys.path.insert(0, r"{p}")\n'
        argv = [sys.executable, '-I', '-X', 'utf8', '-c', preamble + code]
    elif kind == 'shell':
        if os.name == 'nt':
            comspec = os.environ.get('COMSPEC') or 'cmd.exe'
            argv = [comspec, '/c', code]
        else:
            argv = ['/bin/sh', '-c', code]
    else:
        raise ValueError(f'未知执行类型: {kind}')

    t0 = time.time()
    proc = subprocess.Popen(
        argv, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        out, err = proc.communicate()
        rc = proc.returncode if proc.returncode is not None else -1
    return {'rc': rc, 'stdout': out or b'', 'stderr': err or b'',
            'timed_out': timed_out, 'timeout_s': timeout,
            'elapsed': round(time.time() - t0, 2)}


def format_result(res: dict, cap_chars: int = 1800) -> str:
    """执行结果 → 给模型的文本；stdout/stderr 各自有预算，总长度受控。"""
    half = max(200, cap_chars // 2 - 100)

    def _dec(b: bytes) -> str:
        return (b or b'').decode('utf-8', 'replace')

    def _cap(s: str) -> str:
        if len(s) <= half:
            return s
        return s[:half] + f'\n…(输出过长，已截断 {len(s) - half} 字符)'

    lines = []
    if res['timed_out']:
        lines.append(f'⏱ 执行超时（>{res.get("timeout_s", "?")}s），'
                     f'进程树已被强杀')
    lines.append(f'退出码 {res["rc"]}，用时 {res["elapsed"]}s')
    out, err = _cap(_dec(res['stdout'])), _cap(_dec(res['stderr']))
    if out.strip():
        lines.append('── stdout ──\n' + out)
    if err.strip():
        lines.append('── stderr ──\n' + err)
    if not out.strip() and not err.strip():
        lines.append('（无输出；Python 要用 print() 才能看到结果）')
    return '\n'.join(lines)
