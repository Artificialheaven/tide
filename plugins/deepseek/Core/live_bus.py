# -*- coding: utf-8 -*-
"""实时事件总线（进程内 pub/sub，跨 asyncio 线程 ↔ HTTP 工作线程）。

设计约束：
- 引擎侧（asyncio 线程）publish 必须零负担、零异常：无订阅者时仅一次加锁空转，
  绝不能影响对话主链路；
- 每个 SSE 订阅者一条有界 queue：慢消费者丢最旧事件（并累加 dropped 计数，
  由 SSE 线程补发 meta 事件通知页面），不能让一个卡住的浏览器反压机器人；
- ring buffer 保存最近事件供 EventSource 断线重连时按 Last-Event-ID 补播。

事件就是普通 dict，必有 seq/ts/kind/group_id/chat_type/turn_id 字段
（group_id 私聊时为对方 QQ）。发布者只有 bot 的 asyncio 线程一个，
订阅者在 HTTP 线程消费，queue 本身线程安全；Subscription.dropped 同样只有
publish 侧写、消费侧读完清零，靠「单生产者」事实保证。
"""
from __future__ import annotations

import queue
import threading
import time
from collections import deque

# 事件类型白名单（页面过滤参数合法性校验用）
EVENT_KINDS = (
    'chat',          # 群/私聊入站消息（过 enabled 后、触发闸门前；被拦的也有）
    'gate',          # 触发闸门拦截（粗筛/判官判否）
    'turn_start',    # 一轮对话开始
    'prompt',        # 任务执行中的插队插话（实际进入模型的话）
    'model_round',   # 一次 LLM 调用完成（正文/思考链/工具调用/token）
    'tool_result',   # 一次工具派发完成
    'assistant_send',  # 真正发进群/私聊的正文（interim/final/loop_speech）
    'compact',       # 长跑上下文自动压缩
    'notice',        # 长跑生命周期通知
    'turn_end',      # 一轮对话结束
    'meta',          # 传输层元信息（丢帧计数等，由 SSE 线程直发，不进 ring）
)

_RING_MAX = 1000
_QUEUE_MAX = 200
_REPLAY_MAX = 200

# SSE 关闭哨兵
CLOSE = object()


class Subscription:
    __slots__ = ('queue', 'groups', 'kinds', 'include_prompt', 'dropped')

    def __init__(self, groups: set, kinds: set, include_prompt: bool):
        self.queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self.groups = groups          # 空集 = 全部群
        self.kinds = kinds            # 空集 = 全部类型
        self.include_prompt = include_prompt
        self.dropped = 0

    def wants(self, ev: dict) -> bool:
        if self.groups and str(ev.get('group_id') or '') not in self.groups:
            return False
        if self.kinds and ev.get('kind') not in self.kinds:
            return False
        return True


class LiveBus:
    def __init__(self):
        self._lock = threading.Lock()
        self._seq = 0
        self._ring: deque = deque(maxlen=_RING_MAX)
        self._subs: set[Subscription] = set()

    # ── 订阅侧（HTTP 线程）──────────────────────────────────────

    def subscribe(self, groups=None, kinds=None,
                  include_prompt: bool = False) -> Subscription:
        g = {str(g).strip() for g in (groups or []) if str(g).strip()}
        k = {str(k).strip() for k in (kinds or []) if str(k).strip()}
        sub = Subscription(g, k, bool(include_prompt))
        with self._lock:
            self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            self._subs.discard(sub)

    def replay(self, last_id, sub: Subscription) -> list:
        """Last-Event-ID 补播：ring 中 seq 更大且本订阅者关心的事件。"""
        try:
            lid = int(last_id or 0)
        except (TypeError, ValueError):
            lid = 0
        with self._lock:
            if lid <= 0:
                return []
            out = [ev for ev in self._ring
                   if ev['seq'] > lid and sub.wants(ev)]
        return out[-_REPLAY_MAX:]

    def count(self) -> int:
        with self._lock:
            return len(self._subs)

    def has_flag(self, name: str) -> bool:
        """是否有订阅者开了某重负载开关（如 include_prompt，懒构造依据）。"""
        with self._lock:
            return any(getattr(s, name, False) for s in self._subs)

    def close_all(self) -> None:
        """服务停止：向所有 SSE 连接投哨兵，让 handler 线程退出
        （热重载/断线重连后浏览器连上新 bus，旧线程不能悬挂）。"""
        with self._lock:
            subs = list(self._subs)
            self._subs.clear()
        for s in subs:
            try:
                s.queue.put_nowait(CLOSE)
            except queue.Full:
                pass

    # ── 发布侧（asyncio 线程）───────────────────────────────────

    def publish(self, kind: str, **fields) -> dict | None:
        """发一个事件。返回事件 dict（含分配的 seq），无订阅者也写 ring
        （供短暂断线补播）。任何异常由上层 LiveManager 吞掉。"""
        with self._lock:
            self._seq += 1
            ev = {'seq': self._seq,
                  'ts': fields.pop('ts', None) or time.time(),
                  'kind': kind,
                  'group_id': str(fields.pop('group_id', '') or ''),
                  'chat_type': fields.pop('chat_type', '') or 'group',
                  'turn_id': fields.pop('turn_id', '') or '',
                  **fields}
            self._ring.append(ev)
            subs = list(self._subs)
        for sub in subs:
            if not sub.wants(ev):
                continue
            try:
                sub.queue.put_nowait(ev)
            except queue.Full:
                # 慢消费者：丢最旧腾位，dropped 由 SSE 线程在下一帧前补 meta
                try:
                    sub.queue.get_nowait()
                except queue.Empty:
                    pass
                sub.dropped += 1
                try:
                    sub.queue.put_nowait(ev)
                except queue.Full:
                    pass
        return ev
