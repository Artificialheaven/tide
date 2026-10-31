"""定时概率唤醒（防假死）：每隔若干分钟按概率主动唤醒 AI 一次。

- 每 tick 遍历所有已开启群：活跃门（近期有真人说话）→ 分时段概率掷骰；
- 保底：连续 wake_every_n 次未触发则第 N 次必触发（计数器持久化，重启不丢）；
- 触发 = 合成一条「定时苏醒」系统消息跑一轮完整对话（带工具/记忆/注入），
  模型可选择只回 [静默]，engine 不发群、不入库。
- 生命周期仿 scheduler.JobManager：on_start 启动 / on_stop 取消。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import random
import time

from . import storage

WEEK_CN = ('一', '二', '三', '四', '五', '六', '日')
SILENT_MARK = '[静默]'


# ── 纯逻辑（可单测）──────────────────────────────────────────────

def _hhmm_to_min(text) -> int | None:
    try:
        h, m = str(text).strip().split(':')
        h, m = int(h), int(m)
    except (ValueError, AttributeError):
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h * 60 + m
    return None


def parse_prob_schedule(text: str) -> tuple[list | None, str]:
    """解析分时段概率配置 JSON。

    形如 [{"start":"23:00","end":"02:00","percent":2}]；end<=start 视为跨午夜。
    返回 (规则列表, '') 或 (None, 错误说明)。空字符串 = 不分时段。
    """
    text = (text or '').strip()
    if not text:
        return [], ''
    try:
        raw = json.loads(text)
    except ValueError:
        return None, '分时段配置不是合法 JSON'
    if not isinstance(raw, list):
        return None, '分时段配置必须是数组'
    out = []
    for i, seg in enumerate(raw):
        if not isinstance(seg, dict):
            return None, f'第 {i + 1} 段不是对象'
        lo, hi = _hhmm_to_min(seg.get('start')), _hhmm_to_min(seg.get('end'))
        if lo is None or hi is None:
            return None, f'第 {i + 1} 段 start/end 必须是 HH:MM'
        try:
            pct = int(seg.get('percent', seg.get('p')))
        except (TypeError, ValueError):
            return None, f'第 {i + 1} 段 percent 必须是 0-100 的整数'
        if not 0 <= pct <= 100:
            return None, f'第 {i + 1} 段 percent 超出 0-100'
        out.append({'lo': lo, 'hi': hi, 'percent': pct})
    return out, ''


def prob_for_minute(minute: int, schedule: list, default_percent: int) -> int:
    """当前分钟命中的概率（百分数）。跨午夜段按 lo>hi 处理；不命中走默认。"""
    for seg in schedule:
        lo, hi = seg['lo'], seg['hi']
        if lo <= hi:
            if lo <= minute < hi:
                return seg['percent']
        elif minute >= lo or minute < hi:
            return seg['percent']
    return max(0, min(100, int(default_percent)))


def should_wake(now: _dt.datetime, ticks_since_fire: int, every_n: int,
                schedule: list, default_percent: int,
                roll: float | None = None) -> tuple[bool, str]:
    """本次 tick 是否唤醒：保底优先，其次分时段概率。

    ticks_since_fire 是距上次触发已经历的 tick 数；当前是第 ticks+1 次。
    """
    n = int(every_n or 0)
    if n > 0 and ticks_since_fire + 1 >= n:
        return True, f'保底（每 {n} 次必唤醒 1 次）'
    minute = now.hour * 60 + now.minute
    pct = prob_for_minute(minute, schedule, default_percent)
    if pct <= 0:
        return False, ''
    if roll is None:
        roll = random.random()
    if roll * 100 < pct:
        return True, f'概率命中（{pct}%）'
    return False, ''


# ── 主动搭话掷骰（叠加在 tick 上的早退层）────────────────────────

def is_peak(now: _dt.datetime) -> bool:
    """高峰段：工作日 9:00-11:59、14:00-17:59；午休/晚间/周末全天低谷。

    高峰（贵、人在忙）少开口；低谷（便宜、人在闲）更敢开口。
    """
    if now.weekday() >= 5:
        return False
    return (9 <= now.hour < 12) or (14 <= now.hour < 18)


def parse_quiet(text) -> tuple:
    """安静时段 "HH:MM-HH:MM" → ([(lo,hi)...], '')。

    跨午夜拆成两段；空串/空配置 = ([], '')（关闭）；
    非法 = (None, 错误说明)，调用方按关闭处理（fail-open）。
    """
    text = (text or '').strip()
    if not text:
        return [], ''
    parts = text.split('-')
    if len(parts) != 2:
        return None, '安静时段格式应为 HH:MM-HH:MM'
    lo, hi = _hhmm_to_min(parts[0]), _hhmm_to_min(parts[1])
    if lo is None or hi is None or lo == hi:
        return None, '安静时段的时间非法'
    if lo < hi:
        return [(lo, hi)], ''
    return [(lo, 24 * 60), (0, hi)], ''


def in_quiet(now: _dt.datetime, ranges: list) -> bool:
    if not ranges:
        return False
    minute = now.hour * 60 + now.minute
    return any(lo <= minute < hi for lo, hi in ranges)


def speakup_timing(age_sec: float, recent_min: int = 20,
                   cold_hours: int = 6) -> float:
    """冷场时长乘子：刚聊过不黏人（0.15）、正常（1.0）、冷太久更易开口（1.5）。"""
    age = float(age_sec or 0)
    if age < max(0, int(recent_min)) * 60:
        return 0.15
    if age > max(0, int(cold_hours)) * 3600:
        return 1.5
    return 1.0


def speakup_line(now: _dt.datetime, age_sec: float, miss_streak: int,
                 cfg: dict) -> float:
    """本次主动搭话概率线（0-100）：峰谷基础率 × 冷场乘子 + 连没中累进。"""
    base = int(cfg.get('speakup_peak_percent' if is_peak(now)
                       else 'speakup_valley_percent', 20))
    timing = speakup_timing(
        age_sec, int(cfg.get('speakup_recent_minutes', 20)),
        int(cfg.get('speakup_cold_hours', 6)))
    bonus = min(int(cfg.get('speakup_miss_cap', 40)),
                int(miss_streak or 0) * int(cfg.get('speakup_miss_bonus', 10)))
    return max(0.0, min(100.0, base * timing + bonus))


def speakup_decision(now: _dt.datetime, *, last_human_ts: float,
                     last_bot_ts: float, miss_streak: int, cfg: dict,
                     roll: float | None = None) -> tuple:
    """主动搭话裁决：('skip'|'miss'|'hit', 理由)。

    skip：安静时段 / 机器人自我冷却内（不掷骰、不计连没中）；
    miss：掷了没中（连没中 +1）；hit：该开口。
    """
    ranges, err = parse_quiet(cfg.get('speakup_quiet', ''))
    if err:
        ranges = []  # 配置非法 fail-open：当作没设安静段
    if in_quiet(now, ranges):
        return 'skip', '安静时段'
    cooldown = int(cfg.get('speakup_self_cooldown_minutes', 20)) * 60
    if last_bot_ts and now.timestamp() - float(last_bot_ts) < cooldown:
        return 'skip', '自我冷却中'
    age = now.timestamp() - float(last_human_ts or 0)
    line = speakup_line(now, age, miss_streak, cfg)
    if line <= 0:
        return 'miss', '概率 0'
    if roll is None:
        roll = random.random()
    if roll * 100 < line:
        return 'hit', f'主动搭话概率命中（{line:.0f}%）'
    return 'miss', f'未中（概率 {line:.0f}%）'


# ── 轮询循环 ─────────────────────────────────────────────────────

class ProactorManager:
    def __init__(self, db_path: str, log=None):
        self.db_path = db_path
        self._log = log or (lambda msg, level='info': None)
        self._task: asyncio.Task | None = None

    def start(self, plugin):
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(plugin))

    async def stop(self):
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=3)
            except Exception:
                pass

    async def _loop(self, plugin):
        while True:
            try:
                minutes = max(1, int(plugin.cfg.get('wake_check_minutes', 30)))
                await asyncio.sleep(minutes * 60)
                await self._tick(plugin)
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                self._log(f'定时唤醒循环异常: {ex}', 'warn')
                await asyncio.sleep(60)

    async def _tick(self, plugin):
        if not (plugin.cfg.get('wake_on', True)
                or plugin.cfg.get('speakup_on', True)) \
                or not getattr(plugin, 'bot', None):
            return
        schedule, err = parse_prob_schedule(plugin.cfg.get('wake_schedule', ''))
        if err:
            self._log(f'唤醒分时段配置有误（本轮全用默认概率）: {err}', 'warn')
            schedule = []
        _, qerr = parse_quiet(plugin.cfg.get('speakup_quiet', ''))
        if qerr:
            self._log(f'主动搭话安静时段配置有误（本轮按未设置处理）: {qerr}',
                      'warn')
        groups = [g for g in storage.list_groups(self.db_path) if g['enabled']]
        now = _dt.datetime.now()
        for g in groups:
            gid = str(g['group_id'])
            try:
                await self._tick_one(plugin, gid, g, schedule, now)
            except Exception as ex:
                self._log(f'群 {gid} 唤醒 tick 失败: {ex}', 'warn')

    async def _tick_one(self, plugin, gid, group_row, schedule, now):
        from .config import resolve_group
        cfg = resolve_group(plugin.cfg, group_row.get('settings'))
        wake_on = cfg.get('wake_on', True)
        speakup_on = cfg.get('speakup_on', True)
        if not wake_on and not speakup_on:
            return
        # AI 自建轮询：群里有任意一条设定就完全走自管模式（旧版全局概率
        # 配置只对没有任何轮询的群生效，避免双重唤醒）。
        # 轮询群同样不掷主动搭话，避免一 tick 双开口。
        polls = await asyncio.to_thread(
            storage.list_polls, self.db_path, gid, True)
        if polls:
            if wake_on:
                await self._tick_polls(plugin, gid, cfg, polls, now)
            return
        # 活跃门：冷群不自言自语（主动搭话与旧唤醒共享）
        active_hours = float(cfg.get('wake_active_hours', 12))
        last_human = await asyncio.to_thread(
            storage.last_human_message_ts, self.db_path, gid)
        if active_hours > 0 and (
                not last_human or now.timestamp() - last_human > active_hours * 3600):
            return
        state = None
        # ── 主动搭话层（先掷；命中即早退，本 tick 不再走旧唤醒）──
        if speakup_on:
            last_bot = await asyncio.to_thread(
                storage.last_assistant_message_ts, self.db_path, gid)
            state = await asyncio.to_thread(
                storage.proactor_get, self.db_path, gid)
            verdict, vreason = speakup_decision(
                now, last_human_ts=last_human, last_bot_ts=last_bot,
                miss_streak=state['speakup_miss'], cfg=cfg)
            if verdict == 'hit':
                await asyncio.to_thread(
                    storage.proactor_record, self.db_path, gid, True,
                    now.timestamp())
                self._log(f'群 {gid} 主动搭话（{vreason}）', 'ok')
                async with plugin.group_lock(gid):
                    await plugin.engine.handle_wake(
                        gid, cfg, now, speakup=True,
                        idle_seconds=now.timestamp() - (last_human or 0))
                return
            if verdict == 'miss':
                # 没中：连没中 +1（旧唤醒的 ticks 计数不动），继续走旧唤醒
                await asyncio.to_thread(
                    storage.proactor_bump_speakup_miss,
                    self.db_path, gid, now.timestamp())
        if not wake_on:
            return
        # ── 旧版概率唤醒（语义逐字不变）──
        if state is None:
            state = await asyncio.to_thread(
                storage.proactor_get, self.db_path, gid)
        wake, reason = should_wake(
            now, state['ticks_since_fire'],
            int(cfg.get('wake_every_n', 20)), schedule,
            int(cfg.get('wake_prob_percent', 10)))
        if not wake:
            await asyncio.to_thread(
                storage.proactor_record, self.db_path, gid, False, now.timestamp())
            return
        await asyncio.to_thread(
            storage.proactor_record, self.db_path, gid, True, now.timestamp())
        self._log(f'群 {gid} 定时唤醒（{reason}）', 'ok')
        async with plugin.group_lock(gid):
            await plugin.engine.handle_wake(gid, cfg, now)

    async def _tick_polls(self, plugin, gid, cfg, polls: list, now):
        """逐条评估 AI 自建轮询；同一 tick 至多触发一条（先命中先触发），

        其余条目照记未触发（保底计数照走）。每条有独立的活跃门/概率/保底/分时段。
        """
        # 群级活跃门只查一次真人发言时间，各条轮询再按自己的 active_hours 判
        last_human = await asyncio.to_thread(
            storage.last_human_message_ts, self.db_path, gid)
        chosen, chosen_reason = None, ''
        for poll in polls:
            ah = float(poll.get('active_hours') or 0)
            if ah > 0 and (
                    not last_human or now.timestamp() - last_human > ah * 3600):
                continue  # 这条轮询的活跃门没过：不计 tick（冷群时长本来就不该掷骰）
            segs, err = parse_prob_schedule(poll.get('schedule_json', ''))
            if err:
                segs = []
            wake, reason = should_wake(
                now, int(poll.get('ticks_since_fire') or 0),
                int(poll.get('every_n') or 0), segs,
                int(poll.get('prob_percent') if poll.get('prob_percent') is not None
                    else 10))
            if wake and chosen is None:
                chosen, chosen_reason = poll, reason
                await asyncio.to_thread(
                    storage.poll_record, self.db_path, poll['id'], True,
                    now.timestamp())
            else:
                await asyncio.to_thread(
                    storage.poll_record, self.db_path, poll['id'], False,
                    now.timestamp())
        if chosen is None:
            return
        self._log(f'群 {gid} 轮询 #{chosen["id"]} 唤醒（{chosen_reason}）：'
                  f'{chosen["task"][:40]}', 'ok')
        async with plugin.group_lock(gid):
            await plugin.engine.handle_wake(gid, cfg, now, poll=chosen)

    # ── 模型工具：AI 自管轮询设定 ───────────────────────────────

    async def add_poll_tool(self, group_id, sender_id, sender_name, *,
                            task: str, prob_percent: int = 10,
                            every_n: int = 0, active_hours: float = 12,
                            schedule: str = '', max_polls: int = 10) -> str:
        task = (task or '').strip()
        if not task:
            return '错误: task 必填，写清这条轮询要你主动干什么'
        if len(task) > 300:
            return '错误: task 最长 300 字，写一句话任务即可'
        try:
            pct = int(prob_percent)
        except (TypeError, ValueError):
            return '错误: prob_percent 必须是 0-100 的整数'
        if not 0 <= pct <= 100:
            return '错误: prob_percent 必须在 0-100 之间'
        try:
            n = int(every_n)
        except (TypeError, ValueError):
            return '错误: every_n 必须是 >=0 的整数（0=关保底）'
        if n < 0:
            return '错误: every_n 不能为负'
        try:
            ah = float(active_hours)
        except (TypeError, ValueError):
            return '错误: active_hours 必须是 >=0 的数字（小时，0=不限）'
        if ah < 0:
            return '错误: active_hours 不能为负'
        segs, err = parse_prob_schedule(schedule or '')
        if err:
            # 重抛前给模型的中文错误由 parse_prob_schedule 决定
            return f'错误: 分时段概率 {err}'
        # 落库保留 start/end 原文（parse_prob_schedule 已转成分钟数，
        # 后台列表页要显示 HH:MM；tick 时再解析一遍）
        sched_store = json.dumps(
            json.loads(schedule.strip()), ensure_ascii=False) \
            if schedule and schedule.strip() else '[]'
        cap = max(1, int(max_polls or 10))
        if await asyncio.to_thread(storage.count_polls, self.db_path, group_id) >= cap:
            return f'错误: 本群最多 {cap} 条轮询，先 poll_remove 删掉旧的'
        pid = await asyncio.to_thread(
            storage.add_poll, self.db_path,
            {'group_id': str(group_id), 'task': task, 'prob_percent': pct,
             'every_n': n, 'active_hours': ah,
             'schedule': sched_store,
             'creator_id': str(sender_id or ''),
             'creator_name': str(sender_name or '')})
        return self._poll_added_text(pid, task, pct, n, ah, bool(segs))

    @staticmethod
    def _poll_added_text(pid, task, pct, n, ah, has_segs) -> str:
        parts = [f'已登记轮询设定（{pid}）：任务「{task[:80]}」',
                 f'每次 {pct}% 概率']
        if n > 0:
            parts.append(f'保底每 {n} 次必唤醒 1 次')
        parts.append('群里%.0f小时没人说话就不唤醒' % ah if ah > 0 else '不限活跃时间')
        if has_segs:
            parts.append('已设分时段概率')
        parts.append('可用 poll_list 查看、poll_remove 删除')
        return '，'.join(parts)

    async def list_polls_tool(self, group_id) -> str:
        rows = await asyncio.to_thread(storage.list_polls, self.db_path, group_id)
        if not rows:
            return '本群还没有轮询设定（没有时我按全局定时苏醒配置随机看看群）'
        lines = ['本群轮询设定：']
        for r in rows:
            state = '' if r['enabled'] else '（已停用）'
            sched = '，分时段' if (r.get('schedule_json') or '[]') not in ('[]', '') else ''
            guarantee = f'，保底{r["every_n"]}次' if r.get('every_n') else ''
            ah = float(r.get('active_hours') or 0)
            gate = ('，%.0f小时无人不唤醒' % ah) if ah > 0 else '，不限活跃'
            miss = int(r.get('ticks_since_fire') or 0)
            lines.append(
                f'- [{r["id"]}]{state} {r["prob_percent"]}%{guarantee}{gate}{sched}'
                f'｜连续未触发 {miss} 次｜{r["task"][:60]}')
        return '\n'.join(lines)[:2000]

    async def _get_owned(self, poll_id, sender_id, is_owner):
        try:
            pid = int(poll_id)
        except (TypeError, ValueError):
            return None, '错误: id 必须是数字'
        row = await asyncio.to_thread(storage.get_poll, self.db_path, pid)
        if not row:
            return None, '未找到该轮询设定'
        if not is_owner and str(row.get('creator_id')) != str(sender_id):
            return None, '错误: 只有登记人或主人可以改这条轮询'
        return row, ''

    async def toggle_poll_tool(self, poll_id, sender_id, on: bool,
                               is_owner: bool = False) -> str:
        row, err = await self._get_owned(poll_id, sender_id, is_owner)
        if err:
            return err
        await asyncio.to_thread(storage.set_poll_enabled, self.db_path,
                                row['id'], bool(on))
        return f'轮询 {row["id"]} 已{"启用" if on else "停用"}'

    async def remove_poll_tool(self, poll_id, sender_id,
                               is_owner: bool = False) -> str:
        row, err = await self._get_owned(poll_id, sender_id, is_owner)
        if err:
            return err
        await asyncio.to_thread(storage.delete_poll, self.db_path, row['id'])
        return f'轮询 {row["id"]} 已删除'


def _idle_cn(age_sec: float) -> str:
    age = max(0, int(age_sec or 0))
    if age >= 3600:
        return f'{age // 3600} 小时'
    if age >= 60:
        return f'{age // 60} 分钟'
    return '一小会儿'


def speakup_user_text(now: _dt.datetime, idle_seconds: float = 0,
                      store=None) -> str:
    """主动搭话的合成触发语（allow_silent 轮）。

    核心是防错位框：明确告诉她这不是任何人发的消息、也不是定时任务，
    而是她自己心里冒出来的念头——否则她会把「你可以主动说点什么」
    误读成别人在对她说话，回答「好呀」而不是自己起话头。
    不内联心情/自我记忆（那些走各自的标准注入通道）。
    store 非空时用 Web「提示词」页里可能存在的人工覆盖。
    """
    from . import prompts
    return prompts.render(
        'speakup_notice', store,
        datetime=now.strftime('%Y-%m-%d %H:%M'),
        weekday=WEEK_CN[now.weekday()],
        idle=_idle_cn(idle_seconds))


def wake_user_text(now: _dt.datetime, task: str | None = None,
                   store=None) -> str:
    """合成给模型的唤醒通知（allow_silent 轮）。

    task 非空（AI 自建轮询）时，通知围绕这条具体任务；否则是旧版通用苏醒。
    store 非空时用 Web「提示词」页里可能存在的人工覆盖。
    """
    from . import prompts
    if task and task.strip():
        task_line = (
            f'你给自己登记的轮询任务是：{task.strip()}\n'
            '结合未结约定、长期记忆和自我提示词注入表判断：'
            '现在是不是为这件事主动开口的合适时机？')
    else:
        task_line = (
            '请结合未结约定、长期记忆和自我提示词注入表判断：'
            '现在有没有值得主动开口的事——到点该提醒的、之前答应过的、'
            '有人悬而未决的问题、或者一句应景的真人式插话？')
    return prompts.render(
        'wake_notice', store,
        datetime=now.strftime('%Y-%m-%d %H:%M'),
        weekday=WEEK_CN[now.weekday()],
        task_line=task_line)
