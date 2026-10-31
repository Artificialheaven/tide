# -*- coding: utf-8 -*-
"""`#sl` 状态查询：版本 / 平台 / 系统在线时间。

全员可用、无需求机器人已在该群开启，因此不走触发判官也不抢群锁。
运行时间取的是**操作系统开机以来的在线时间**（不是 bot 进程时长）：
Windows 用 GetTickCount64，Linux 读 /proc/uptime，macOS 退到 sysctl。
"""
from __future__ import annotations

import ctypes
import platform
import re
import struct
import subprocess
import sys
import time

from .version import VERSION

STATUS_TRIGGER = '#sl'


def is_status_trigger(text: str | None) -> bool:
    return (text or '').strip() == STATUS_TRIGGER


def system_uptime_seconds() -> int:
    """系统自开机以来的在线秒数；取不到返回 0。"""
    if sys.platform == 'win32':
        try:
            return int(ctypes.windll.kernel32.GetTickCount64() // 1000)
        except Exception:
            return 0
    try:
        with open('/proc/uptime', 'r', encoding='ascii') as f:
            return int(float(f.read().split()[0]))
    except OSError:
        pass
    try:  # macOS / BSD：sysctl kern.boottime 返回 "{ sec = N, usec = 0 } ..."
        out = subprocess.run(['sysctl', '-n', 'kern.boottime'],
                             capture_output=True, text=True, timeout=2).stdout
        m = re.search(r'sec\s*=\s*(\d+)', out)
        if m:
            return max(0, int(time.time()) - int(m.group(1)))
    except Exception:
        pass
    return 0


def format_uptime(seconds: int) -> str:
    """格式：[x年x月]x天 x小时 x分钟（年/月仅在非零时出现；按12月/年、30天/月折算）。"""
    seconds = max(0, int(seconds))
    minutes = seconds // 60
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    months, days = divmod(days, 30)
    years, months = divmod(months, 12)
    head = ''
    if years:
        head += f'{years}年'
    if months:
        head += f'{months}月'
    head += f'{days}天'
    return f'{head} {hours}小时 {minutes}分钟'


def platform_name() -> str:
    """平台名-位数，如 ubuntu-x64 / win32-x64 / linux-x32 / macos-arm64 不区分架构名，
    位数统一按解释器指针宽度报 x64/x32（32 位 Python 跑在 64 位系统上也报 x32，符合实际）。"""
    if sys.platform == 'win32':
        name = 'win32'
    elif sys.platform == 'darwin':
        name = 'macos'
    elif sys.platform.startswith('linux'):
        name = 'linux'
        try:  # 优先报发行版 id（ubuntu/debian/...），老内核/容器取不到就退回 linux
            name = platform.freedesktop_os_release().get('ID') or 'linux'
        except Exception:
            pass
    else:
        name = sys.platform
    bits = 'x64' if struct.calcsize('P') * 8 == 64 else 'x32'
    return f'{name}-{bits}'


def status_text() -> str:
    return (
        'Tide 状态\n'
        f'版本:{VERSION}\n'
        f'平台:{platform_name()}\n'
        f'运行时间: {format_uptime(system_uptime_seconds())}'
    )


async def send_status(bot, target_id, message_type: str) -> None:
    await bot.send_message(message=status_text(), target_id=target_id,
                           message_type=message_type)
