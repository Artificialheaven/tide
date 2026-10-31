# -*- coding: utf-8 -*-
"""历史消息查询工具的纯逻辑：时间串解析、结果渲染。

支持两种查法（由 builtin_tools.history_query 调用、storage.query_messages 落地）：
- 时间段：start/end 时间串；
- 相对条数：offset（距当前消息跳过几条）+ limit。
"""
from __future__ import annotations

import datetime as _dt
import time

from . import context as _ctx

_TIME_FORMATS = (
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%d %H:%M',
    '%Y-%m-%d',
    '%m-%d %H:%M:%S',
    '%m-%d %H:%M',
    '%m-%d',
)

MAX_LIMIT = 100
DEFAULT_LIMIT = 30


def parse_time(s: str, now=None) -> float:
    """宽松时间串 → UNIX 秒。

    接受：UNIX 秒数字串、完整/简写日期（月日简写补当前年）。
    无法解析抛 ValueError（调用方转中文错误给模型）。
    """
    s = str(s or '').strip()
    if not s:
        raise ValueError('空时间')
    if s.isdigit() and len(s) >= 8:
        return float(s)
    now = now or _dt.datetime.now()
    for fmt in _TIME_FORMATS:
        try:
            dt = _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
        if '%Y' not in fmt:
            dt = dt.replace(year=now.year)
            # 「12-31」在 1 月查询时显然指去年
            if dt - now > _dt.timedelta(days=1):
                dt = dt.replace(year=now.year - 1)
        return dt.timestamp()
    raise ValueError(f'无法解析时间：{s}')


def render_history(rows: list, total: int, *, shown_range: bool,
                   offset: int = 0, truncated: bool = False) -> str:
    """storage 行 → 给模型看的历史文本。"""
    if not rows:
        return '该范围内没有消息记录。'
    kind = '时间段' if shown_range else '相对条数'
    head = f'历史消息（{kind}查询，命中 {total} 条，以下 {len(rows)} 条，按时间正序）：'
    lines = [head]
    for r in rows:
        tag = _ctx.time_tag(r.get('created_at'))
        role = r.get('role')
        if role == 'user':
            who = _ctx.speaker_label(r)
            lines.append(f'{tag}{who}: {r.get("content", "")}')
        elif role == 'assistant':
            label = '助手[定时]' if (r.get('name') or '') == 'scheduled' else '助手'
            content = r.get('content') or '（工具操作，无正文）'
            lines.append(f'{tag}{label}: {content}')
        else:
            lines.append(f'{tag}[工具 {r.get("name", "")}] '
                         f'{(r.get("content") or "")[:200]}')
    if truncated:
        lines.append('…(结果过长已截断：缩小时间范围或减小 limit 再查)')
    elif total > len(rows) and not shown_range:
        lines.append(f'…(更早还有 {total - len(rows) - offset} 条，'
                     f'加大 offset 或 limit 翻页)')
    elif total > len(rows):
        lines.append(f'…(范围内共 {total} 条，已按 limit 截断，缩小时间范围再查)')
    return '\n'.join(lines)
