"""定期维护：messages / usage_log / qzone_events 的保留策略。

性能审计 P1-1：这三张表只增不删（messages 只标 summarized、usage_log 每次
模型调用一行、qzone_events 定义了 prune 却没人调），长跑一年能到百万行级，
仪表盘与上下文查询都随之变慢。

这里只做「删旧行」：
- messages 只删 summarized=1 且不在最近 keep 条窗口里的行，未摘要的活跃
  窗口一条不动；
- 全部在线程池里跑（asyncio.to_thread），不占事件循环；
- 间隔与保留天数走配置，改配置下个周期生效（循环每轮重读 self.p.cfg）。
"""
from __future__ import annotations

import asyncio

from . import storage

MIN_INTERVAL = 600          # 最小维护间隔（秒），防止配置写 0 变成死循环
STARTUP_DELAY = 120.0       # 启动后延迟，避开 on_start/补历史高峰


def _as_int(value, default: int, lo: int = 1) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n >= lo else default


def run_once(db_path: str, cfg: dict) -> dict:
    """跑一次保留策略（同步，调用方负责丢线程池）。"""
    cfg = cfg or {}
    return storage.housekeeping(
        db_path,
        keep_per_group=_as_int(cfg.get('messages_keep'), 2000, 200),
        usage_days=_as_int(cfg.get('usage_keep_days'), 180, 7),
        qzone_days=_as_int(cfg.get('qzone_events_keep_days'), 30, 1),
    )


class Housekeeper:
    """插件级维护任务。start()/stop() 幂等，可被重连/热重载反复调用。"""

    def __init__(self, plugin, log=None):
        self.p = plugin
        self.log = log or print
        self._task = None

    # ── 生命周期 ───────────────────────────────────────────────

    def running(self) -> bool:
        t = self._task
        return bool(t and not t.done())

    def start(self, cfg: dict | None = None) -> bool:
        """启动后台维护任务；已有任务/开关关闭时返回 False。"""
        cfg = cfg if cfg is not None else (getattr(self.p, 'cfg', None) or {})
        if not cfg.get('housekeep_on', True):
            return False
        if self.running():
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._task = loop.create_task(self._loop())
        return True

    async def stop(self):
        task = self._task
        self._task = None
        if not task or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:          # 收尾阶段不再向上抛
            self.log(f'数据清理任务停止异常：{e}')

    # ── 循环 ───────────────────────────────────────────────────

    async def _loop(self):
        await asyncio.sleep(STARTUP_DELAY)
        while True:
            cfg = getattr(self.p, 'cfg', None) or {}
            if cfg.get('housekeep_on', True):
                try:
                    res = await asyncio.to_thread(
                        run_once, getattr(self.p, 'db_path', ''), cfg)
                    if any(int(v or 0) for v in res.values()):
                        self.log('数据清理：' + '，'.join(
                            f'{k} -{v}' for k, v in res.items() if v))
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.log(f'数据清理失败：{e}')
            interval = _as_int(cfg.get('housekeep_interval'), 21600, MIN_INTERVAL)
            await asyncio.sleep(interval)
