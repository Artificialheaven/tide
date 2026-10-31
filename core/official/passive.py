# -*- coding: utf-8 -*-
"""按群的被动回复名额池。

官方限制：同一个 msg_id 最多 5 条被动回复、约 5 分钟内有效。
这里把「群内近期每条入站消息剩下的名额」攒成一个池，发满一条再换下一条，
把单条消息的 5 条上限扩展成群内近期消息的配额总和。

所有取号都在调用方持有的群锁内完成（`lock(group)`），避免并发抢到
同一个 msg_id+msg_seq。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class PassiveSlot:
    group_id: str
    msg_id: str
    received_at: float
    expires_at: float
    next_seq: int = 1

    def usable(self, now: float, max_seq: int) -> bool:
        return self.next_seq <= max_seq and now < self.expires_at


@dataclass
class Reservation:
    slot: PassiveSlot
    seq: int


class PassivePool:
    def __init__(self, window_seconds: float = 295.0, max_seq: int = 5,
                 max_per_group: int = 64,
                 clock: Optional[Callable[[], float]] = None):
        self.window = max(1.0, float(window_seconds))
        self.max_seq = max(1, min(5, int(max_seq)))
        self.max_per_group = max(1, int(max_per_group))
        self._clock = clock or time.monotonic
        # 按入池顺序保存，主动清理时不打乱 FIFO
        self._pools: dict[str, list[PassiveSlot]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ── 锁 ────────────────────────────────────────────────

    def lock(self, group_id: str) -> asyncio.Lock:
        gid = str(group_id)
        lock = self._locks.get(gid)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[gid] = lock
        return lock

    # ── 入池 ──────────────────────────────────────────────

    def add(self, group_id: str, msg_id: str) -> Optional[PassiveSlot]:
        gid, mid = str(group_id), str(msg_id)
        if not gid or not mid:
            return None
        now = self._clock()
        pool = self._pools.setdefault(gid, [])
        self._prune(pool, now)
        for slot in pool:
            if slot.msg_id == mid:
                return slot
        slot = PassiveSlot(group_id=gid, msg_id=mid, received_at=now,
                           expires_at=now + self.window)
        pool.append(slot)
        if len(pool) > self.max_per_group:
            # 丢最早的（最接近过期的）槽位
            pool.pop(0)
        return slot

    def _prune(self, pool: list[PassiveSlot], now: float) -> None:
        pool[:] = [slot for slot in pool
                   if slot.usable(now, self.max_seq)]

    # ── 取号 ──────────────────────────────────────────────

    def reserve(self, group_id: str,
                prefer_msg_id: str = "") -> Optional[Reservation]:
        """在已持有 `lock(group_id)` 的前提下取一个名额。

        优先用 prefer_msg_id（CQ reply 指向的入站消息），否则选最早到期的
        可用槽位（FIFO 消耗快过期的名额）。取号即递增 seq。
        """
        gid = str(group_id)
        now = self._clock()
        pool = self._pools.get(gid)
        if not pool:
            return None
        self._prune(pool, now)
        if not pool:
            self._pools.pop(gid, None)
            return None

        slot = None
        preferred = str(prefer_msg_id or "")
        if preferred:
            slot = next((s for s in pool if s.msg_id == preferred), None)
            if slot is not None and not slot.usable(now, self.max_seq):
                slot = None
        if slot is None:
            usable = [s for s in pool if s.usable(now, self.max_seq)]
            if not usable:
                return None
            slot = min(usable, key=lambda s: (s.expires_at, s.received_at))

        seq = slot.next_seq
        slot.next_seq += 1
        self._prune(pool, now)
        if not pool:
            self._pools.pop(gid, None)
        return Reservation(slot=slot, seq=seq)

    async def reserve_async(self, group_id: str,
                            prefer_msg_id: str = "") -> Optional[Reservation]:
        async with self.lock(group_id):
            return self.reserve(group_id, prefer_msg_id)

    # ── 失效 ──────────────────────────────────────────────

    def invalidate(self, group_id: str, msg_id: str) -> None:
        """官方拒绝该 msg_id 时整条槽位作废（其余名额也不可信）。"""
        gid, mid = str(group_id), str(msg_id)
        pool = self._pools.get(gid)
        if not pool:
            return
        self._pools[gid] = [s for s in pool if s.msg_id != mid]
        if not self._pools[gid]:
            self._pools.pop(gid, None)

    # ── 观察 ──────────────────────────────────────────────

    def snapshot(self, group_id: str) -> list[PassiveSlot]:
        gid = str(group_id)
        pool = self._pools.get(gid)
        if not pool:
            return []
        now = self._clock()
        self._prune(pool, now)
        return list(pool)

    def available(self, group_id: str) -> int:
        return sum(self.max_seq - slot.next_seq + 1
                   for slot in self.snapshot(group_id))

    def clear(self) -> None:
        self._pools.clear()