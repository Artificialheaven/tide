"""Windows 下 pywin32 以 pip --target 安装时的引导 shim。

pywin32 的 DLL/pth 在 --target 布局下不会自动生效，stdio MCP 若间接依赖
pywin32 会导入失败。在 import mcp 之前调用 enable_pywin32(lib_dir) 即可；
非 Windows / 未安装 pywin32 时静默跳过。
"""
from __future__ import annotations

import os
import sys


def enable_pywin32(lib_dir: str):
    if not os.path.isdir(lib_dir):
        return
    for sub in ('win32', os.path.join('win32', 'lib'), 'pywin32_system32'):
        p = os.path.join(lib_dir, sub)
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)

    dll_dir = os.path.join(lib_dir, 'pywin32_system32')
    if os.path.isdir(dll_dir):
        try:
            os.add_dll_directory(dll_dir)  # py>=3.8
        except (AttributeError, OSError):
            if dll_dir not in os.environ.get('PATH', ''):
                os.environ['PATH'] = dll_dir + os.pathsep + os.environ.get('PATH', '')
