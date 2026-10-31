# -*- coding: utf-8 -*-
"""任务打断：把群友消息直接插进正在执行的对话轮，而不是排队等下一轮。

同群对话被 group_lock 串行：一条消息到达时若该群已有一轮对话在跑（可能
正在连续调工具），普通路径只能阻塞等锁、任务结束后再开新轮。打断指令不
抢锁，直接投递到当前活动轮的插队队列；对话轮只在安全点（一轮工具执行完、
下一次请求模型前）取走，保证不会在半截工具调用中间改消息列表。

全部操作都在事件循环线程里发生（push 来自消息协程，drain 来自对话协程，
协作式调度不会并发改写），不需要加锁。
"""
from __future__ import annotations


class InterruptHub:
    def __init__(self):
        self._token: dict[str, object] = {}   # 会话 key -> 活动轮令牌
        self._queued: dict[str, list] = {}    # 会话 key -> 待插队消息

    def begin_turn(self, key) -> object:
        """对话轮开始：登记为活动轮，返回令牌。"""
        key = str(key)
        token = object()
        self._token[key] = token
        self._queued.setdefault(key, [])
        return token

    def end_turn(self, key, token) -> list:
        """对话轮结束：注销活动轮，返回仍没消费的插队消息（极窄竞态兜底）。"""
        key = str(key)
        if self._token.get(key) is token:
            self._token.pop(key, None)
        return self._queued.pop(key, [])

    def is_active(self, key) -> bool:
        return str(key) in self._token

    def push(self, key, item: dict) -> bool:
        """投递一条插队消息。没有活动轮返回 False（调用方提示用户直接说话）。"""
        key = str(key)
        if key not in self._token:
            return False
        self._queued.setdefault(key, []).append(item)
        return True

    def pending(self, key, token) -> int:
        if self._token.get(str(key)) is not token:
            return 0
        return len(self._queued.get(str(key)) or [])

    def drain(self, key, token) -> list:
        """活动轮在安全点取走全部插队消息（非活动轮/过期令牌拿不到）。"""
        key = str(key)
        if self._token.get(key) is not token:
            return []
        items = self._queued.get(key) or []
        self._queued[key] = []
        return items
