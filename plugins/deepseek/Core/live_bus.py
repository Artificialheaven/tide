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

import json
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
    'tool_batch',    # 一批工具调度（并行组/串行结果）
    'approval_request',  # 审批请求（N-03）
    'approval_result',   # 审批结果
    'subagent_start',    # 子代理开始（N-04）
    'subagent_end',      # 子代理结束
    'hook_block',        # hook 拦截（N-05）
    'meta',          # 传输层元信息（丢帧计数等，由 SSE 线程直发，不进 ring）
)

_RING_MAX = 1000
# ring 总字节预算：单事件最长约 21k 字符，纯按条数限制时 1000 条在 CJK
# 峰值下可达数十 MB；按 JSON 序列化后的 UTF-8 字节数从最旧开始驱逐。
_RING_MAX_BYTES = 8 * 1024 * 1024
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
        # ring 与逐条字节数平行保存：逐出时直接减，不必重新序列化旧事件
        self._ring: deque = deque()
        self._ring_sizes: deque = deque()
        self._ring_bytes = 0
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

    @staticmethod
    def _event_size(ev: dict) -> int:
        """单事件内存估算：JSON 序列化后的 UTF-8 字节数（CJK 按 3 字节）。

        序列化失败时退化为一个保守的固定值，避免预算统计因单个坏事件失真。
        """
        try:
            return len(json.dumps(ev, ensure_ascii=False).encode('utf-8'))
        except (TypeError, ValueError):
            return 1024

    def _append_ring(self, ev: dict) -> None:
        """写 ring，并按「条数 + 总字节」双预算从最旧开始驱逐（调用方持锁）。

        字节预算见 `_RING_MAX_BYTES`；至少保留最近 1 条——单个事件本身超预算
        时宁可略超，也不丢掉唯一有效事件。
        """
        size = self._event_size(ev)
        self._ring.append(ev)
        self._ring_sizes.append(size)
        self._ring_bytes += size
        while self._ring and (
                len(self._ring) > _RING_MAX
                or (self._ring_bytes > _RING_MAX_BYTES
                    and len(self._ring) > 1)):
            self._ring.popleft()
            self._ring_bytes -= self._ring_sizes.popleft()

    def ring_len(self) -> int:
        """ring 当前事件条数。"""
        with self._lock:
            return len(self._ring)

    def ring_bytes(self) -> int:
        """ring 当前字节占用（按 JSON UTF-8 序列化估算）。"""
        with self._lock:
            return self._ring_bytes

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

    def older(self, before_seq, limit: int, sub: Subscription) -> list:
        """加载更早的事件：ring 里 seq 小于 before_seq（<=0 表示最新）且本
        订阅者关心的那批，按 seq 升序返回。给调试台的「加载更早」用。

        ring 是进程内内存缓冲（_RING_MAX 条），进程重启即清空——所以这只
        是「本次运行内的历史」，不是持久化归档。
        """
        try:
            before = int(before_seq or 0)
        except (TypeError, ValueError):
            before = 0
        try:
            n = max(1, min(int(limit or 200), 500))
        except (TypeError, ValueError):
            n = 200
        with self._lock:
            out = [ev for ev in self._ring
                   if (before <= 0 or ev['seq'] < before) and sub.wants(ev)]
            oldest = self._ring[0]['seq'] if self._ring else 0
        return out[-n:]

    def oldest_seq(self) -> int:
        with self._lock:
            return self._ring[0]['seq'] if self._ring else 0

    def latest_seq(self) -> int:
        with self._lock:
            return self._seq

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
            # 队列满时 put_nowait(CLOSE) 会被丢弃导致 SSE 线程悬挂：
            # 先清空旧事件再放哨兵，保证关闭信号一定送达
            while True:
                try:
                    s.queue.get_nowait()
                except queue.Empty:
                    break
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
            self._append_ring(ev)
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
