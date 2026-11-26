# -*- coding: utf-8 -*-
"""N-03 审批中心：ask 决策落 pending，等主人批准/拒绝/超时。

- DB 是事实来源（进程重启/Web 线程都看它），内存 future 只负责快速唤醒；
- 等待用 1s 轮询 + future 双通道，兼容 Flask 线程直接改库；
- 默认超时按 fallback 处理（deny），并允许群内提示。
"""
from __future__ import annotations

import asyncio
import json
import time

from . import storage


_SECRET_KEYS = ('api_key', 'apikey', 'token', 'password', 'secret',
                'authorization', 'cookie')


def _preview(args: dict, limit: int = 500) -> str:
    def scrub(value):
        if isinstance(value, dict):
            return {k: ('***' if str(k).lower() in _SECRET_KEYS
                        else scrub(v)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [scrub(v) for v in value]
        return value
    try:
        text = json.dumps(scrub(args or {}), ensure_ascii=False)
    except Exception:
        text = str(args)
    return text[:limit]


class ApprovalCenter:
    def __init__(self, db_path: str, plugin):
        self.db_path = db_path
        self.p = plugin
        self._futures: dict[int, asyncio.Future] = {}

    # ── 申请 ─────────────────────────────────────────────────

    async def request(self, *, gid: str, chat_type: str, tool_key: str,
                      args: dict, actor: str, turn_id: str = '',
                      ttl: float | None = None, source: str = 'tool') -> bool:
        cfg = getattr(self.p, 'cfg', {}) or {}
        ask_cfg = (cfg.get('permissions') or {}).get('ask') or {}
        if ttl is None:
            ttl = float(ask_cfg.get('timeout_sec', 120) or 120)
        fallback = str(ask_cfg.get('fallback', 'deny'))
        preview = _preview(args)
        # 落库/读库都放线程池：SQLite 写锁或慢查询会卡住整个事件循环
        row = await asyncio.to_thread(
            storage.create_approval,
            self.db_path, gid=str(gid), chat_type=chat_type,
            actor_id=str(actor or ''), actor_name='', tool_key=tool_key,
            args_json=preview, args_preview=preview, ttl=ttl,
            turn_id=turn_id, source=source)
        aid = int(row['id'])
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._futures[aid] = fut
        self._live('approval_request', gid, chat_type, turn_id,
                   approval_id=aid, tool_key=tool_key, args_preview=preview,
                   expires_at=row['expires_at'])
        await self._notify(gid, chat_type, aid, tool_key, preview)
        deadline = time.time() + float(ttl)
        try:
            while True:
                if fut.done():
                    return bool(fut.result())
                status = await asyncio.to_thread(
                    storage.get_approval, self.db_path, aid)
                if status and status['status'] != 'pending':
                    return status['status'] == 'approved'
                if time.time() >= deadline:
                    approved = fallback == 'allow'
                    await asyncio.to_thread(
                        storage.decide_approval, self.db_path, aid,
                        'approved' if approved else 'rejected',
                        decided_by='timeout')
                    self._live('approval_result', gid, chat_type, turn_id,
                               approval_id=aid, approved=approved,
                               decided_by='timeout')
                    return approved
                try:
                    # 1s 轮询保底（兼容直接改库的 Flask 线程），正常批准走 future 立即唤醒
                    await asyncio.wait({fut}, timeout=1.0)
                except asyncio.CancelledError:
                    # 取消路径保持同步落库：此时事件循环可能正在关闭，
                    # 再排线程池任务可能失败，而取消标记必须写进去
                    storage.decide_approval(self.db_path, aid, 'cancelled',
                                            decided_by='cancel')
                    raise
        finally:
            self._futures.pop(aid, None)

    async def decide(self, approval_id: int, *, approved: bool,
                     decided_by: str = 'web') -> bool:
        status = storage.decide_approval(
            self.db_path, approval_id,
            'approved' if approved else 'rejected', decided_by=decided_by)
        ok = bool(status)
        fut = self._futures.get(int(approval_id))
        if fut is not None and not fut.done():
            try:
                asyncio.get_running_loop().call_soon_threadsafe(
                    fut.set_result, bool(approved))
            except RuntimeError:
                pass
        row = storage.get_approval(self.db_path, int(approval_id)) or {}
        self._live('approval_result', row.get('gid', ''), row.get('chat_type', ''),
                   row.get('turn_id', ''), approval_id=approval_id,
                   approved=bool(approved), decided_by=decided_by)
        return ok

    def pending(self, gid: str | None = None) -> list[dict]:
        return storage.pending_approvals(self.db_path, gid)

    def cancel_all(self) -> None:
        for aid, fut in list(self._futures.items()):
            if not fut.done():
                fut.cancel()
            try:
                storage.decide_approval(self.db_path, aid, 'cancelled',
                                        decided_by='shutdown')
            except Exception:
                pass
        self._futures.clear()

    # ── 辅助 ─────────────────────────────────────────────────

    async def _notify(self, gid: str, chat_type: str, aid: int,
                      tool_key: str, preview: str) -> None:
        cfg = getattr(self.p, 'cfg', {}) or {}
        if not cfg.get('approval_notify_group', True):
            return
        bot = getattr(self.p, 'bot', None)
        if bot is None:
            return
        text = (f'【审批】#{aid} 工具 {tool_key} 请求执行：'
                f'{preview[:200]}\n主人可回复「批准 {aid}」或「拒绝 {aid}」。')
        try:
            await bot.send_message(message=text, target_id=str(gid),
                                   message_type=chat_type)
        except Exception:
            pass

    def _live(self, kind: str, gid: str, chat_type: str, turn_id: str,
              **fields) -> None:
        live = getattr(self.p, 'live', None)
        if live is None:
            return
        try:
            fn = getattr(live, 'emit', None) or getattr(live, 'publish')
            fn(kind, group_id=str(gid), chat_type=chat_type,
               turn_id=turn_id, **fields)
        except Exception:
            pass
