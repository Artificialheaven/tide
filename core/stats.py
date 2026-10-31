"""
StatsTracker — 消息统计追踪器（独立模块）
===========================================
框架中消息统计的唯一数据源，组合在 bot 实例内部：
- 累计发送/接收总数
- 当前自然分钟的发送/接收数
- 最近 60 分钟的每分钟趋势（供 GUI / Web 图表使用）

计数与读取均加锁；分钟桶采用惰性滚动——只有在新增计数或读取时
才把已结束的分钟归档，因此不需要后台定时器。
"""
import threading
import time


class StatsTracker:
    """消息统计追踪器。线程安全，单实例组合到 bot 内部。"""

    # 趋势保留的最大分钟数
    TREND_MAX = 60

    def __init__(self):
        self._lock = threading.Lock()

        # 累计总数
        self.sent_count = 0
        self.received_count = 0

        # 当前分钟桶（边界对齐到自然分钟）
        now = time.time()
        self._bucket_start = now - now % 60
        self._bucket_sent = 0
        self._bucket_received = 0

        # 每分钟趋势：[{"time": "HH:MM", "received": n, "sent": n}, ...]
        self.minute_trend = []

    # ---- 内部：分钟桶惰性滚动（调用方须持有 _lock） ----

    def _archive_bucket_locked(self, received, sent):
        """归档一个刚结束的分钟桶并裁剪趋势长度。"""
        self.minute_trend.append({
            'time': time.strftime('%H:%M', time.localtime(self._bucket_start)),
            'received': received,
            'sent': sent,
        })
        if len(self.minute_trend) > self.TREND_MAX:
            self.minute_trend.pop(0)

    def _rollover_locked(self):
        """把当前时间之前已结束的分钟桶全部归档。"""
        now = time.time()
        elapsed_min = int((now - self._bucket_start) // 60)
        if elapsed_min <= 0:
            return

        if elapsed_min > self.TREND_MAX:
            # 中断太久，历史桶已无参考价值，直接丢弃
            self.minute_trend.clear()
        else:
            # 中间没有消息的分钟按 0 归档
            for _ in range(elapsed_min - 1):
                self._bucket_start += 60
                self._archive_bucket_locked(0, 0)
            # 最后一个桶携带实际计数
            self._bucket_start += 60
            self._archive_bucket_locked(self._bucket_received, self._bucket_sent)

        self._bucket_sent = 0
        self._bucket_received = 0
        self._bucket_start = now - now % 60

    # ---- 计数入口 ----

    def increment_sent(self):
        """增加发送消息计数。"""
        with self._lock:
            self._rollover_locked()
            self.sent_count += 1
            self._bucket_sent += 1

    def increment_received(self):
        """增加接收消息计数。"""
        with self._lock:
            self._rollover_locked()
            self.received_count += 1
            self._bucket_received += 1

    # ---- 读取 ----

    def get_stats(self) -> dict:
        """获取统计快照。

        :return: {
            "sent": 累计发送, "received": 累计接收,
            "sent_minute": 当前分钟发送, "received_minute": 当前分钟接收,
            "trend": 最近每分钟趋势列表,
        }
        """
        with self._lock:
            self._rollover_locked()
            return {
                "sent": self.sent_count,
                "received": self.received_count,
                "sent_minute": self._bucket_sent,
                "received_minute": self._bucket_received,
                "trend": list(self.minute_trend),
            }


# 全局唯一实例：跨重连复用，累计数与趋势不因 bot 实例重建而清零
stats_tracker = StatsTracker()
