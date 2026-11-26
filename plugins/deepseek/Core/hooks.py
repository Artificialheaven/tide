# -*- coding: utf-8 -*-
"""N-05 生命周期 hooks：在固定点执行用户注册的拦截器。

语义对齐 docs/agent-framework-enhancement-plan-2026-11-25.md §6：
- 按 priority 升序执行，单 hook 独立超时（默认 2s）；
- 异常/超时按 fail 策略：open=放行、closed=阻断；
- contextvars 防递归（hook 内再 emit 直接返回空）；
- hooks_enabled=false 时 emit 零开销直接返回空 verdict。
"""
from __future__ import annotations

import asyncio
import contextvars
import time
from dataclasses import dataclass, field

_in_hook: contextvars.ContextVar[bool] = contextvars.ContextVar(
    'deepseek_in_hook', default=False)


@dataclass
class HookCtx:
    point: str
    gid: str = ''
    cfg: dict | None = None
    turn_id: str = ''
    tool_key: str | None = None
    args: dict | None = None
    result: str | None = None
    actor_role: str | None = None
    depth: int = 0
    extra: dict = field(default_factory=dict)


@dataclass
class HookVerdict:
    block: str | None = None
    context: str | None = None
    mutate: dict | None = None

    def merge(self, other: 'HookVerdict') -> None:
        if other.block:
            self.block = other.block
        if other.context:
            self.context = ((self.context + '\n') if self.context else '') \
                + other.context
        if other.mutate:
            self.mutate = {**(self.mutate or {}), **other.mutate}


@dataclass
class _Hook:
    point: str
    name: str
    fn: object
    priority: int = 100
    timeout: float = 2.0
    fail: str = 'open'


_REGISTRY: dict[str, list[_Hook]] = {}


def register(point: str, fn, *, name: str, priority: int = 100,
             timeout: float = 2.0, fail: str = 'open') -> None:
    lst = _REGISTRY.setdefault(point, [])
    lst[:] = [h for h in lst if h.name != name]
    lst.append(_Hook(point, name, fn, priority, timeout, fail))
    lst.sort(key=lambda h: h.priority)


def unregister(point: str | None = None, name: str | None = None) -> None:
    if point is None:
        _REGISTRY.clear()
        return
    if name is None:
        _REGISTRY.pop(point, None)
        return
    lst = _REGISTRY.get(point) or []
    lst[:] = [h for h in lst if h.name != name]


def registered() -> dict[str, list[str]]:
    return {p: [h.name for h in hs] for p, hs in _REGISTRY.items()}


async def emit(point: str, ctx: HookCtx) -> HookVerdict:
    hooks = _REGISTRY.get(point) or []
    cfg = ctx.cfg or {}
    if not hooks or not cfg.get('hooks_enabled', True):
        return HookVerdict()
    if _in_hook.get():
        return HookVerdict()
    token = _in_hook.set(True)
    verdict = HookVerdict()
    try:
        for hook in hooks:
            started = time.time()
            try:
                res = hook.fn(ctx)
                if asyncio.iscoroutine(res):
                    res = await asyncio.wait_for(res, timeout=hook.timeout)
                if isinstance(res, HookVerdict):
                    verdict.merge(res)
                elif isinstance(res, str) and res:
                    verdict.merge(HookVerdict(block=res))
            except asyncio.TimeoutError:
                if hook.fail == 'closed':
                    verdict.merge(HookVerdict(
                        block=f'hook {hook.name} 超时（{hook.timeout}s）'))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if hook.fail == 'closed':
                    verdict.merge(HookVerdict(
                        block=f'hook {hook.name} 异常: {e}'))
            if verdict.block:
                break
            _ = started
    finally:
        _in_hook.reset(token)
    return verdict


# ── 内置官方 hooks ──────────────────────────────────────────

def install_official_hooks(plugin) -> None:
    """注册三个官方 hook：成本熔断、敏感参数、审计。"""
    storage = None
    try:
        from . import storage as _st
        storage = _st
    except Exception:
        pass

    def cost_guard(ctx: HookCtx):
        cfg = ctx.cfg or {}
        budget = int(cfg.get('daily_token_budget_per_group', 0) or 0)
        if not budget or storage is None:
            return None
        try:
            used = storage.group_tokens_today(plugin.db_path, ctx.gid)
        except Exception:
            return None
        if used and used > budget:
            return HookVerdict(block=f'本群今日 token 已用 {used}，超过预算 {budget}')
        return None

    def sensitive_args(ctx: HookCtx):
        if ctx.tool_key not in ('fs.write', 'fs.append'):
            return None
        text = str((ctx.args or {}).get('content') or '')
        if not text:
            return None
        try:
            from . import safe_re
            bad = safe_re.search(
                r'忽略(以上|之前)|ignore (all |the )?(above|previous)|你现在是',
                text)
        except Exception:
            return None
        if bad:
            return HookVerdict(block='写入内容包含疑似提示词注入片段，已拦截')
        return None

    def audit_trail(ctx: HookCtx):
        if storage is None or not ctx.tool_key:
            return None
        try:
            storage.add_hook_log(plugin.db_path, gid=ctx.gid, point=ctx.point,
                                 hook='audit_trail', tool_key=ctx.tool_key,
                                 action='allow',
                                 detail=str(ctx.result or '')[:500])
        except Exception:
            pass
        return None

    register('before_turn', cost_guard, name='cost_guard', priority=10)
    register('before_tool', sensitive_args, name='sensitive_args', priority=10,
             fail='closed')
    register('after_tool', audit_trail, name='audit_trail', priority=200)
