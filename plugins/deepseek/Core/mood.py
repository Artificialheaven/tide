"""每群一份的心情：心情值 + 七绪，判定走便宜模型，本地半衰期回落。

设计要点（参考 feiyu-mood）：
- 无后台定时器：读时按半衰期纯计算衰减，判定落库时重新锚定 updated_at；
- 一次显著变化只对「她」宣布一次：render_injection 先占后用，下一轮不重复；
- 低信心/小波动只记时间线，不进系统提示词；每天每群有注入次数上限；
- 判定 fire-and-forget（同群重入跳过），异常只记日志，不影响回复。
本模块不 import openai（LLMClient 注入）。
"""
from __future__ import annotations

import asyncio
import time

from . import storage

# 半衰期：心情值约 3 小时回落到基线，七绪约 90 分钟
MOOD_HALF_LIFE = 3 * 3600
DIM_HALF_LIFE = 90 * 60

# 七绪（强度 0-100）
MOOD_DIMS = ('开心', '踏实', '甜蜜', '心疼', '担心', '难过', '吃醋')

# 不同轮次来源的单轮变化上限；未知来源 fail-open 按真人话（human）
SOURCE_CAPS = {'human': 10, 'talk': 5, 'work': 3, 'greet': 5}

_REASON_MAX_CHARS = 60


# ── 纯函数 ──────────────────────────────────────────────────────

def decayed_mood(mood: float, updated_at: float, baseline: float,
                 now: float) -> float:
    """心情值按半衰期向基线回落；从未锚定（updated_at=0）时就是基线。"""
    if not updated_at:
        return float(baseline)
    dt = max(0.0, float(now) - float(updated_at))
    return float(baseline) + (float(mood) - float(baseline)) * (
        0.5 ** (dt / MOOD_HALF_LIFE))


def decayed_dims(dims: dict, updated_at: float, now: float) -> dict:
    """七绪按各自半衰期向 0 衰减。"""
    if not updated_at or not dims:
        return {}
    dt = max(0.0, float(now) - float(updated_at))
    factor = 0.5 ** (dt / DIM_HALF_LIFE)
    out = {}
    for k, v in dims.items():
        try:
            nv = float(v) * factor
        except (TypeError, ValueError):
            continue
        if nv >= 0.5:  # 太小的直接抹掉
            out[k] = nv
    return out


def source_cap(source_kind: str) -> int:
    return SOURCE_CAPS.get(source_kind, SOURCE_CAPS['human'])


def clamp_delta(delta, source_kind: str) -> int:
    cap = source_cap(source_kind)
    try:
        d = int(round(float(delta)))
    except (TypeError, ValueError):
        return 0
    return max(-cap, min(cap, d))


def clamp_dim_increments(raw: dict, source_kind: str) -> dict:
    """只收七绪里的键，逐维增量按来源上限钳制。"""
    cap = source_cap(source_kind)
    out = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if k not in MOOD_DIMS:
            continue
        try:
            inc = int(round(float(v)))
        except (TypeError, ValueError):
            continue
        if inc:
            out[k] = max(-cap, min(cap, inc))
    return out


def apply_dims(dims_now: dict, increments: dict) -> dict:
    out = dict(dims_now or {})
    for k, inc in increments.items():
        out[k] = max(0.0, min(100.0, float(out.get(k, 0.0)) + inc))
    return {k: v for k, v in out.items() if v >= 0.5}


def _clamp01(x, lo=0.0, hi=100.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return lo


def local_day(now: float) -> str:
    return time.strftime('%Y-%m-%d', time.localtime(now))


# ── 管理器 ──────────────────────────────────────────────────────

class MoodManager:
    def __init__(self, db_path: str, log=None, prompts=None):
        self.db_path = db_path
        self._log = log or (lambda msg, level='info': None)
        self.prompts = prompts
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set = set()

    def _lock(self, group_id: str) -> asyncio.Lock:
        lk = self._locks.get(group_id)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[group_id] = lk
        return lk

    # ── 读取 ────────────────────────────────────────────────────

    def effective(self, group_id, cfg: dict, now: float | None = None) -> dict:
        """当前状态 + 实时衰减后的心情值/七绪 + 是否有未宣布变化。"""
        now = now if now is not None else time.time()
        baseline = float(cfg.get('mood_baseline', 70))
        st = storage.mood_get_state(self.db_path, str(group_id), baseline)
        st['mood_now'] = decayed_mood(
            st['mood'], st['updated_at'], baseline, now)
        st['dims_now'] = decayed_dims(st['dims'], st['updated_at'], now)
        st['has_pending'] = bool(st['pending_tid']) and (
            st['injected_for'] + 1e-6 < st['updated_at'])
        return st

    # ── 注入（engine 组装期同步调用）────────────────────────────

    def render_injection(self, group_id, cfg: dict,
                         now: float | None = None) -> str:
        """有待宣布的显著变化时返回一句心情背景，并立即占用（只说一次）。"""
        if not cfg.get('mood_on', True):
            return ''
        now = now if now is not None else time.time()
        st = self.effective(group_id, cfg, now)
        if not st['has_pending']:
            return ''
        if abs(int(st['pending_delta'])) < int(
                cfg.get('mood_delta_threshold', 5)):
            return ''
        if int(st['pending_confidence']) < int(
                cfg.get('mood_min_confidence', 60)):
            return ''
        day = local_day(now)
        count_today = int(st['inject_count']) if st['inject_day'] == day else 0
        if count_today >= int(cfg.get('mood_daily_cap', 6)):
            return ''
        reason = (st['pending_reason'] or '').strip()[:_REASON_MAX_CHARS]
        if not reason:
            return ''
        storage.mood_mark_injected(
            self.db_path, str(group_id), day=day,
            count=count_today + 1, now=now)
        from . import prompts as prompts_mod
        return prompts_mod.render('mood_wrap', self.prompts, reason=reason)

    # ── 判定（fire-and-forget）──────────────────────────────────

    def schedule_judge(self, group_id, cfg: dict, llm, *,
                       source_kind: str, trigger_text: str, reply_text: str,
                       context_script: str = ''):
        if not cfg.get('mood_on', True) or llm is None:
            return
        gid = str(group_id)
        lk = self._lock(gid)
        if lk.locked():
            return
        task = asyncio.create_task(self._judge(
            gid, cfg, llm, source_kind, trigger_text, reply_text,
            context_script))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _judge(self, gid: str, cfg: dict, llm, source_kind: str,
                     trigger_text: str, reply_text: str,
                     context_script: str):
        async with self._lock(gid):
            usage = {}
            try:
                now = time.time()
                baseline = float(cfg.get('mood_baseline', 70))
                st = storage.mood_get_state(self.db_path, gid, baseline)
                mood_now = decayed_mood(
                    st['mood'], st['updated_at'], baseline, now)
                dims_now = decayed_dims(st['dims'], st['updated_at'], now)
                res = await llm.judge_mood(
                    cfg, mood_now=mood_now, dims_now=dims_now,
                    trigger_text=trigger_text or '',
                    reply_text=reply_text or '',
                    context_script=context_script or '',
                    source_kind=source_kind)
                usage = res.get('usage', {}) or {}
                delta = clamp_delta(res.get('delta'), source_kind)
                incs = clamp_dim_increments(res.get('dims'), source_kind)
                dims_after = apply_dims(dims_now, incs)
                mood_after = _clamp01(mood_now + delta, 0.0, 100.0)
                reason = str(res.get('reason', '') or '').strip()[
                    :_REASON_MAX_CHARS]
                confidence = int(_clamp01(res.get('confidence', 0), 0, 100))
                await asyncio.to_thread(
                    storage.mood_save_judge, self.db_path, gid,
                    mood=mood_after, dims=dims_after, delta=delta,
                    confidence=confidence, reason=reason,
                    source=source_kind, now=now,
                    keep=int(cfg.get('mood_timeline_keep', 200)))
                self._log_usage(gid, usage, True)
            except Exception as ex:
                self._log(f'心情判定失败（不影响回复）: {ex}', 'warning')
                self._log_usage(gid, usage, False)

    def _log_usage(self, gid: str, usage: dict, ok: bool):
        try:
            storage.add_usage(self.db_path, {
                'group_id': gid, 'kind': 'mood',
                'tokens_in': int(usage.get('tokens_in', 0) or 0),
                'cache_hit': int(usage.get('cache_hit', 0) or 0),
                'tokens_out': int(usage.get('tokens_out', 0) or 0),
                'ok': ok})
        except Exception:
            pass

    # ── Web / 管理 ──────────────────────────────────────────────

    def view(self, group_id, cfg: dict, limit: int = 20,
             now: float | None = None) -> dict:
        st = self.effective(group_id, cfg, now)
        tl = storage.mood_timeline(self.db_path, str(group_id), limit)
        return {'state': st, 'timeline': tl}

    def reset(self, group_id, cfg: dict, now: float | None = None) -> dict:
        return storage.mood_reset(
            self.db_path, str(group_id),
            baseline=float(cfg.get('mood_baseline', 70)), now=now)
