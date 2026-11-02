# -*- coding: utf-8 -*-
"""按群的被动回复名额池。

官方限制：同一个 msg_id 最多 5 条被动回复、约 5 分钟内有效。
这里把「群内近期每条入站消息剩下的名额」攒成一个池。

取号策略（所有取号都在调用方持有的群锁 `lock(group)` 内完成）：

1. 显式指定（CQ reply / 发送参数 _official_msg_id）且仍可用 → 直接用；
2. 群内「粘住」的当前 msg_id 仍有名额 → 继续用，**不用完不换 id**；
3. 需要重新挑 id 时：
   - 优先用「目标用户」（发送参数 _official_user_id，否则取池内最新一条
     消息的发送人）自己的入站消息（多条时先烧最快过期的）；
   - 开池（pool_on=True，默认）：再不够才按 FIFO 用池内任何人的 id；
   - 关池（pool_on=False）：除显式指定/粘住的 id 外不借用其他消息，
     无 id 可抛给调用方（无显式偏好时允许重新锚定池内最新一条，
     供入群问候这类没有触发消息的主动发送兜底）。
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
    user_id: str = ""
    next_seq: int = 1

    def usable(self, now: float, max_seq: int) -> bool:
        return self.next_seq <= max_seq and now < self.expires_at


@dataclass
class Reservation:
    slot: PassiveSlot
    seq: int


class PassivePool:
    def __init__(self, window_seconds: float = 295.0, max_seq: int = 5,
                 max_per_group: int = 64, pool_on: bool = True,
                 clock: Optional[Callable[[], float]] = None):
        self.window = max(1.0, float(window_seconds))
        self.max_seq = max(1, min(5, int(max_seq)))
        self.max_per_group = max(1, int(max_per_group))
        self.pool_on = bool(pool_on)
        self._clock = clock or time.monotonic
        # 按入池顺序保存，主动清理时不打乱 FIFO
        self._pools: dict[str, list[PassiveSlot]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # 每群当前粘住的 msg_id：该 id 名额没用完就不换
        self._sticky: dict[str, str] = {}

    # ── 锁 ────────────────────────────────────────────────

    def lock(self, group_id: str) -> asyncio.Lock:
        gid = str(group_id)
        lock = self._locks.get(gid)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[gid] = lock
        return lock

    # ── 入池 ──────────────────────────────────────────────

    def add(self, group_id: str, msg_id: str,
            user_id: str = "") -> Optional[PassiveSlot]:
        gid, mid = str(group_id), str(msg_id)
        if not gid or not mid:
            return None
        now = self._clock()
        pool = self._pools.setdefault(gid, [])
        self._prune(pool, now)
        for slot in pool:
            if slot.msg_id == mid:
                # 入站时 user_id 可能缺失，后补全
                if not slot.user_id and user_id:
                    slot.user_id = str(user_id)
                return slot
        slot = PassiveSlot(group_id=gid, msg_id=mid, received_at=now,
                           expires_at=now + self.window,
                           user_id=str(user_id or ""))
        pool.append(slot)
        if len(pool) > self.max_per_group:
            # 丢最早的（最接近过期的）槽位；若丢的是粘住的，解除粘住
            dropped = pool.pop(0)
            if self._sticky.get(gid) == dropped.msg_id:
                self._sticky.pop(gid, None)
        return slot

    def _prune(self, pool: list[PassiveSlot], now: float) -> None:
        pool[:] = [slot for slot in pool
                   if slot.usable(now, self.max_seq)]

    # ── 取号 ──────────────────────────────────────────────

    def reserve(self, group_id: str,
                prefer_msg_id: str = "",
                prefer_user_id: str = "") -> Optional[Reservation]:
        """在已持有 `lock(group_id)` 的前提下取一个名额。

        取号即递增 seq；选槽规则见模块文档字符串。
        """
        gid = str(group_id)
        now = self._clock()
        pool = self._pools.get(gid)
        if not pool:
            return None
        self._prune(pool, now)
        if not pool:
            self._pools.pop(gid, None)
            self._sticky.pop(gid, None)
            return None

        usable = [s for s in pool if s.usable(now, self.max_seq)]
        if not usable:
            return None

        preferred = str(prefer_msg_id or "")
        slot: Optional[PassiveSlot] = None
        if preferred:
            # 显式指定的 id 不可用时不偷偷换别的（尤其关池模式）
            slot = next((s for s in usable if s.msg_id == preferred), None)
            if slot is None:
                return None

        if slot is None:
            sticky_id = self._sticky.get(gid)
            if sticky_id:
                slot = next((s for s in usable if s.msg_id == sticky_id), None)

        if slot is None:
            target_user = str(prefer_user_id or "").strip()
            if not target_user and pool:
                # 默认锚定最新入站消息的发送人（通常就是本轮触发者）
                target_user = pool[-1].user_id
            if self.pool_on:
                user_slots = [s for s in usable
                              if target_user and s.user_id == target_user]
                candidates = user_slots or usable
                # 先烧最快过期的名额
                slot = min(candidates,
                           key=lambda s: (s.expires_at, s.received_at))
            else:
                # 关池：没有粘住的 id 时，只允许整体重新锚定最新一条
                # （入群问候等无触发消息的发送走这里）；不按用户翻旧账
                slot = usable[-1]

        seq = slot.next_seq
        slot.next_seq += 1
        self._sticky[gid] = slot.msg_id
        self._prune(pool, now)
        if not pool:
            self._pools.pop(gid, None)
            self._sticky.pop(gid, None)
        return Reservation(slot=slot, seq=seq)

    async def reserve_async(self, group_id: str,
                            prefer_msg_id: str = "",
                            prefer_user_id: str = "") -> Optional[Reservation]:
        async with self.lock(group_id):
            return self.reserve(group_id, prefer_msg_id, prefer_user_id)

    # ── 失效 ──────────────────────────────────────────────

    def invalidate(self, group_id: str, msg_id: str) -> None:
        """官方拒绝该 msg_id 时整条槽位作废（其余名额也不可信）。"""
        gid, mid = str(group_id), str(msg_id)
        pool = self._pools.get(gid)
        if not pool:
            return
        self._pools[gid] = [s for s in pool if s.msg_id != mid]
        if self._sticky.get(gid) == mid:
            self._sticky.pop(gid, None)
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
        self._sticky.clear()
