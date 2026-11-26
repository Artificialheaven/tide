# -*- coding: utf-8 -*-
"""N-01 工具批调度：同轮多个 tool_call 的并发/屏障执行。

规则（与 docs/agent-framework-enhancement-plan-2026-11-25.md §2 一致）：
- 连续的只读调用合并成一个并发组（上限 tool_parallel_max，默认 4）；
- 非只读调用作为屏障，严格按模型给出的顺序串行执行；
- 写类返回拒绝/拦截时，后续写类短路为 skipped，避免“删了 A 再删 B”的半完成；
- 结果按 calls 原序回填，模型看到的 tool 顺序与它请求的一致；
- 外层取消时取消未完成任务，交给 engine._fill_missing_tool_results 补占位；
- tool_parallel_enabled=false 时完全退化为串行 for，等价回滚到旧行为。
"""
from __future__ import annotations

import asyncio
import fnmatch
import time
from dataclasses import dataclass

READONLY_TOOL_KEYS = frozenset({
    'fs.list', 'fs.read', 'fs.doc_view', 'fs.read_image', 'fs.read_video',
    'qq.member_info', 'qq.file_list',
    'memory.recall', 'memory.recall_self', 'history.query',
    'task.list', 'schedule.list', 'poll.list', 'watcher.list', 'watcher.logs',
    'inject.list', 'request.list', 'meme.lookup', 'time.now',
    'usage.stats', 'message.read_image', 'message.read_forward',
    'qzone.recent', 'qzone.list', 'qzone.detail', 'qzone.images',
    'qzone.feeds',
})

# 写类/发送类被拒绝后，后续写类不再执行
_DENY_PREFIXES = ('[拒绝]', '[审批未通过]', '[工具异常]')


def parse_tool_patterns(value) -> tuple[str, ...]:
    """配置里的工具名模式 → 去空去重元组。

    兼容列表与字符串两种写法；字符串按 `,` / `，` / `、` / 换行 / 空白分隔，
    例如 `mcp__playwright__browser_*, custom__my_tool`。
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        text = str(value or '')
        for ch in (',', '，', '、', '\n', ';', '；'):
            text = text.replace(ch, ' ')
        items = text.split()
    out, seen = [], set()
    for it in items:
        pat = str(it or '').strip()
        if pat and pat not in seen:
            seen.add(pat)
            out.append(pat)
    return tuple(out)


def tool_matches(pattern: str, name: str, tool_key: str = '') -> bool:
    """单个模式是否命中工具（模型函数名或内部 key，支持 fnmatch 通配）。

    `mcp__playwright__browser_*` 既能匹配函数名也能匹配内部 key；
    `fs_read` 与 `fs.read` 两种写法都认。
    """
    pat = str(pattern or '').strip()
    if not pat:
        return False
    name = str(name or '')
    key = str(tool_key or name)
    if pat in (name, key):
        return True
    try:
        return fnmatch.fnmatchcase(name, pat) or fnmatch.fnmatchcase(key, pat)
    except Exception:
        return False


def matched_any(patterns, name: str, tool_key: str = '') -> bool:
    """任一模式命中即为真。"""
    for pat in patterns or ():
        if tool_matches(pat, name, tool_key):
            return True
    return False


@dataclass
class BatchOutcome:
    results: list            # [(call, content), ...] 与 calls 同长同序
    parallel_groups: int = 0
    wall_ms: int = 0


def tool_key_of(call) -> str:
    """兼容 dict 原始 tool_call 与归一化后的 dict。"""
    if not isinstance(call, dict):
        return ''
    fn = call.get('function') or {}
    name = fn.get('name') if isinstance(fn, dict) else ''
    key = str(name or call.get('name') or '')
    try:
        from .builtin_tools import TOOL_KEYS
        return str(TOOL_KEYS.get(key, key))
    except Exception:
        return key


def _is_denial(content) -> bool:
    if not isinstance(content, str):
        return False
    head = content.lstrip()[:20]
    return any(head.startswith(p) for p in _DENY_PREFIXES)


async def run_batch(dispatch, calls, *, cfg: dict | None = None,
                    gid: str = '') -> BatchOutcome:
    """dispatch(call) 是 async 函数；calls 是模型的 tool_calls 列表。"""
    cfg = cfg or {}
    calls = list(calls or [])
    started = time.time()
    if not calls:
        return BatchOutcome(results=[], parallel_groups=0, wall_ms=0)

    if not cfg.get('tool_parallel_enabled', True):
        out = []
        for call in calls:
            out.append((call, await _safe_dispatch(dispatch, call)))
        return BatchOutcome(results=out, parallel_groups=0,
                            wall_ms=int((time.time() - started) * 1000))

    extra_serial = set(cfg.get('tool_parallel_serial') or [])
    max_conc = max(1, int(cfg.get('tool_parallel_max', 4) or 4))
    group_timeout = float(cfg.get('tool_parallel_group_timeout', 35) or 35)

    def readonly(call) -> bool:
        key = tool_key_of(call)
        return bool(key) and key in READONLY_TOOL_KEYS and key not in extra_serial

    results = [None] * len(calls)
    parallel_groups = 0
    sem = asyncio.Semaphore(max_conc)
    blocked = False

    async def run_one(idx: int, call):
        async with sem:
            results[idx] = (call, await _safe_dispatch(dispatch, call))

    i = 0
    n = len(calls)
    try:
        while i < n:
            if readonly(calls[i]):
                group = []
                while i < n and readonly(calls[i]):
                    group.append(i)
                    i += 1
                parallel_groups += 1
                tasks = [asyncio.create_task(run_one(idx, calls[idx]))
                         for idx in group]
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True),
                        timeout=group_timeout)
                except asyncio.TimeoutError:
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    for idx in group:
                        if results[idx] is None:
                            results[idx] = (calls[idx], '[工具超时] 批调度超时')
                continue

            call = calls[i]
            if blocked:
                results[i] = (call, '[跳过] 前序写操作被拒绝，本次未执行')
                i += 1
                continue
            out = await _safe_dispatch(dispatch, call)
            results[i] = (call, out)
            if _is_denial(out):
                blocked = True
            i += 1
    except asyncio.CancelledError:
        for idx in range(n):
            if results[idx] is None:
                results[idx] = (calls[idx], '[已取消]')
        raise

    for idx in range(n):
        if results[idx] is None:
            results[idx] = (calls[idx], '[已取消]')
    return BatchOutcome(results=results, parallel_groups=parallel_groups,
                        wall_ms=int((time.time() - started) * 1000))


async def _safe_dispatch(dispatch, call):
    try:
        return await dispatch(call)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # 单工具异常不能打断整轮
        return f'[工具异常] {type(e).__name__}: {e}'
