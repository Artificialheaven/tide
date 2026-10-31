"""AI 定时任务：模型可登记「到点让我写一段话发到某群/某私聊」。

- 三种周期：once（单次，YYYY-MM-DD HH:MM）/ daily（每天 HH:MM）/ weekly（每周几 HH:MM）
- brief 是登记时模型写好的「任务简介/创作提示」，到点作为唯一输入再让模型现写正文
- 循环跑在 asyncio 里，on_start 启动 / on_stop 取消；触发后立即推进 next_run_at
- SQLite 操作全部 to_thread；发群消息走该群 group_lock，避免与对话轮互相插队
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import time

from . import reply, storage
from .config import resolve_group, resolve_persona
from .tasks import parse_due

WEEK_CN = ('一', '二', '三', '四', '五', '六', '日')


# ── 时间纯逻辑 ──────────────────────────────────────────────────

def parse_hhmm(text: str) -> tuple[int, int] | None:
    """'HH:MM'（24h）→ (h, m)；兼容 H:MM / 点半之类不收。"""
    s = (text or '').strip()
    parts = s.split(':')
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def _datetime_next(weekday_py: int, h: int, m: int, now: _dt.datetime) -> _dt.datetime:
    """从今天起，下一个 weekday_py(0=周一..6=周日) 的 h:m（时刻已过则顺延到下周）。"""
    days = (weekday_py - now.weekday()) % 7
    cand = (now + _dt.timedelta(days=days)).replace(hour=h, minute=m,
                                                    second=0, microsecond=0)
    if cand <= now:
        cand += _dt.timedelta(days=7)
    return cand


def plan_schedule(mode: str, at_text: str, time_text: str, weekday,
                  now_ts: float | None = None) -> tuple[dict | None, str]:
    """校验工具入参并算出首次触发时间。

    返回 (fields, '') 或 (None, 给模型的中文错误)。
    fields: {mode, once_at, run_hhmm, weekday(1..7 外部约定，-1=不适用), next_run_at}
    """
    now_ts = time.time() if now_ts is None else now_ts
    mode = (mode or 'once').strip().lower()
    if mode not in ('once', 'daily', 'weekly'):
        return None, '错误: mode 只支持 once/daily/weekly'
    if mode == 'once':
        ts = parse_due(at_text)
        if ts is None:
            return None, '错误: 单次任务请用 at 填 "YYYY-MM-DD HH:MM"'
        if ts < now_ts - 60:
            return None, '错误: 触发时间已过去，请检查日期'
        return ({'mode': 'once', 'once_at': ts, 'run_hhmm': '',
                 'weekday': -1, 'next_run_at': ts}, '')
    hm = parse_hhmm(time_text)
    if hm is None:
        return None, '错误: 请用 time 填 "HH:MM"（24小时制）'
    h, m = hm
    now = _dt.datetime.fromtimestamp(now_ts)
    if mode == 'daily':
        cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand <= now:
            cand += _dt.timedelta(days=1)
        return ({'mode': 'daily', 'once_at': None,
                 'run_hhmm': f'{h:02d}:{m:02d}', 'weekday': -1,
                 'next_run_at': cand.timestamp()}, '')
    # weekly：外部约定 1=周一 … 7=周日
    try:
        wd = int(weekday)
    except (TypeError, ValueError):
        return None, '错误: 每周任务请用 weekday 指明星期几（1=周一…7=周日）'
    if not 1 <= wd <= 7:
        return None, '错误: weekday 取 1~7（1=周一，7=周日）'
    cand = _datetime_next(wd - 1, h, m, now)
    return ({'mode': 'weekly', 'once_at': None,
             'run_hhmm': f'{h:02d}:{m:02d}', 'weekday': wd,
             'next_run_at': cand.timestamp()}, '')


def next_recurring(job: dict, now_ts: float | None = None) -> float:
    """循环任务本次触发后的下一次时刻（严格晚于现在，防止宕机漏轮后连发）。"""
    now_ts = time.time() if now_ts is None else now_ts
    now = _dt.datetime.fromtimestamp(now_ts)
    h, m = parse_hhmm(job.get('run_hhmm') or '') or (9, 0)
    if job.get('mode') == 'weekly':
        wd = int(job.get('weekday') or 1)
        return _datetime_next(max(0, wd - 1), h, m, now).timestamp()
    cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if cand <= now:
        cand += _dt.timedelta(days=1)
    return cand.timestamp()


def describe_schedule(job: dict) -> str:
    """人话周期描述（给群列表/确认文本用）。"""
    mode = job.get('mode')
    if mode == 'once':
        ts = job.get('once_at') or job.get('next_run_at')
        return _dt.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
    if mode == 'weekly':
        wd = int(job.get('weekday') or 1)
        return f'每周{WEEK_CN[wd - 1]} {job.get("run_hhmm")}'
    return f'每天 {job.get("run_hhmm")}'


def describe_target(job: dict) -> str:
    if job.get('target_type') == 'private':
        return f'QQ {job.get("target_id")} 私聊'
    return f'群 {job.get("target_id")}'


class JobManager:
    def __init__(self, db_path: str, log=None):
        self.db_path = db_path
        self._log = log or (lambda msg: None)
        self._task: asyncio.Task | None = None

    # ── 模型工具入口 ────────────────────────────────────────────

    async def add_job_tool(self, group_id, sender_id, sender_name, *,
                           target_type: str = 'group', target_id: str = '',
                           mode: str = 'once', at: str = '', time_text: str = '',
                           weekday=0, brief: str = '') -> str:
        brief = (brief or '').strip()
        if not brief:
            return '错误: brief 必填（到点要你写什么、发给谁、什么语气，写清楚）'
        fields, err = plan_schedule(mode, at, time_text, weekday)
        if err:
            return err
        target_type = 'private' if target_type == 'private' else 'group'
        tid = str(target_id or '').strip()
        if target_type == 'private':
            if not tid:
                tid = str(sender_id or '')
            if not tid.isdigit():
                return '错误: 私聊目标必须是 QQ 号'
        else:
            tid = tid or str(group_id)
            if tid != str(group_id):
                # 只许登记到机器人已在、且已开启 AI 的群
                g = await asyncio.to_thread(storage.get_group, self.db_path, tid)
                if not g or not g['enabled']:
                    return f'错误: 群 {tid} 未开启 AI，不能往那里定时发消息'
        jid = await asyncio.to_thread(
            storage.add_job, self.db_path,
            {'creator_id': sender_id, 'creator_name': sender_name,
             'group_id': group_id, 'target_type': target_type, 'target_id': tid,
             'brief': brief, **fields})
        first = _dt.datetime.fromtimestamp(fields['next_run_at']) \
            .strftime('%Y-%m-%d %H:%M')
        when = first if fields['mode'] == 'once' \
            else f'{describe_schedule(fields)}（首次 {first}）'
        who = f'QQ {tid} 私聊' if target_type == 'private' else (
            '本群' if tid == str(group_id) else f'群 {tid}')
        return f'已登记定时任务（{jid}）：{when} 发到{who}，内容要求：{brief[:80]}'

    async def list_jobs_tool(self, group_id, sender_id='') -> str:
        rows = await asyncio.to_thread(
            storage.list_jobs, self.db_path, str(group_id), str(sender_id))
        if not rows:
            return '没有定时任务'
        lines = ['定时任务：']
        for r in rows:
            state = '' if r['enabled'] else '（已停用）'
            lines.append(
                f'- [{r["id"]}]{state} {describe_schedule(r)} → '
                f'{describe_target(r)}：{r["brief"][:50]}')
        return '\n'.join(lines)[:2000]

    async def cancel_job_tool(self, job_id, sender_id, is_owner: bool = False) -> str:
        try:
            jid = int(job_id)
        except (TypeError, ValueError):
            return '错误: id 必须是数字'
        row = await asyncio.to_thread(storage.get_job, self.db_path, jid)
        if not row:
            return '未找到该定时任务'
        if not is_owner and str(row.get('creator_id')) != str(sender_id):
            return '错误: 只有任务创建者或主人可以取消'
        await asyncio.to_thread(storage.delete_job, self.db_path, jid)
        return f'已取消定时任务 {jid}'

    # ── 触发循环 ────────────────────────────────────────────────

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
                await asyncio.sleep(
                    max(15, int(plugin.cfg.get('scheduler_check_seconds', 30))))
                await self._tick(plugin)
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                self._log(f'定时任务循环异常: {ex}')
                await asyncio.sleep(30)

    async def _tick(self, plugin):
        if not plugin.cfg.get('scheduler_on', True) or not getattr(plugin, 'bot', None):
            return
        rows = await asyncio.to_thread(storage.due_jobs, self.db_path, time.time())
        for job in rows:
            try:
                await self._fire(plugin, job)
            except Exception as ex:
                self._log(f'定时任务 {job["id"]} 触发失败: {ex}')
                await asyncio.to_thread(
                    storage.job_mark_run, self.db_path, job['id'],
                    time.time() + 300, True, f'触发失败: {ex}')

    async def _fire(self, plugin, job: dict):
        jid = job['id']
        tid = str(job['target_id'])
        if job['target_type'] == 'group':
            g = await asyncio.to_thread(storage.get_group, self.db_path, tid)
            if not g or not g['enabled']:
                # 目标群关了：单次任务直接停用；循环任务顺延，等重新开启
                if job['mode'] == 'once':
                    await asyncio.to_thread(
                        storage.job_mark_run, self.db_path, jid, None, False,
                        '目标群未开启 AI')
                else:
                    await asyncio.to_thread(
                        storage.job_mark_run, self.db_path, jid,
                        next_recurring(job), True, '目标群未开启，已顺延')
                return
            cfg = resolve_group(plugin.cfg, g.get('settings'))
            persona = resolve_persona(cfg, g.get('persona', ''))
        else:
            cfg = plugin.cfg
            persona = resolve_persona(cfg, '')
        if not cfg.get('scheduler_on', True):
            return  # 群级关闭：不推进，重新开启后本轮仍可触发

        run_id = await asyncio.to_thread(
            storage.bg_run_start, self.db_path, 'scheduler',
            job['target_type'], tid,
            f'【定时任务 {jid}】{job["brief"]}', time.time())
        trace: list = []
        try:
            text, raw = await self._compose(plugin, cfg, persona, job)
            trace.append({'k': 'compose', 'raw': (raw or '')[:2000]})
            if not text:
                note = '模型未产出正文'
                if job['mode'] == 'once':
                    await asyncio.to_thread(
                        storage.job_mark_run, self.db_path, jid, None, False, note)
                else:
                    await asyncio.to_thread(
                        storage.job_mark_run, self.db_path, jid,
                        next_recurring(job), True, note)
                self._log(f'定时任务 {jid} 模型输出为空，本次未发送')
                # 原始输出只有 [nospeak] 视为模型主动跳过；真空输出算异常空轮
                status = 'nospeak' if (raw or '').strip() else 'empty'
                await self._finish_run(plugin, run_id, status, '', trace, cfg)
                return

            mtype = 'private' if job['target_type'] == 'private' else 'group'
            if mtype == 'group':
                at_map = None
                if '@' in text and cfg.get('real_at_on', True):
                    directory = getattr(plugin, 'members', None)
                    if directory is not None:
                        try:
                            at_map = await directory.resolve(
                                tid, plugin.bot, self.db_path)
                        except Exception:
                            at_map = None
                async with plugin.group_lock(tid):
                    n = await reply.send_in_chunks(
                        plugin.bot, tid, text, cfg, message_type='group',
                        name_map=at_map)
                    # name='scheduled'：前台上下文渲染时标注这是定时任务主动发的
                    storage.insert_message(self.db_path, tid, 'assistant',
                                            text, name='scheduled')
            else:
                n = await reply.send_in_chunks(
                    plugin.bot, tid, text, cfg, message_type='private')
                # 私聊同样入历史：后续私聊对话要能看到自己定时发过什么
                storage.insert_message(self.db_path, tid, 'assistant', text,
                                        name='scheduled')
            await asyncio.to_thread(
                storage.add_usage, self.db_path,
                {'group_id': tid if mtype == 'group' else '', 'kind': 'scheduled'})
            self._log(f'定时任务 {jid} 已发到{describe_target(job)}（{n} 条）：{text[:40]}')
            trace.append({'k': 'send', 'chunks': n, 'text': text[:2000]})
            if job['mode'] == 'once':
                await asyncio.to_thread(
                    storage.job_mark_run, self.db_path, jid, None, False)
            else:
                await asyncio.to_thread(
                    storage.job_mark_run, self.db_path, jid, next_recurring(job),
                    True)
            await self._finish_run(plugin, run_id, 'ok', text, trace, cfg)
        except Exception as e:
            await self._finish_run(plugin, run_id, 'error', '', trace, cfg, str(e))
            raise

    async def _finish_run(self, plugin, run_id: int, status: str, final_text: str,
                          trace: list, cfg: dict, error: str = ''):
        await asyncio.to_thread(
            storage.bg_run_finish, self.db_path, run_id, status, final_text,
            trace, error, int(cfg.get('bg_runs_keep', 200)))

    async def _compose(self, plugin, cfg: dict, persona: str,
                       job: dict) -> tuple[str, str]:
        """到点让模型按 brief 现写正文；无工具、无对话上下文。

        返回 (去 nospeak 后的正文, 模型原始输出)。"""
        now = _dt.datetime.now()
        week = WEEK_CN[now.weekday()]
        sys = (
            f'{persona.strip()}\n\n'
            '你在一个QQ群里扮演聊天陪伴角色。现在是一个预先登记的定时任务到点触发，'
            '没有人和你实时对话。请按任务要求写出这次要主动发出的消息正文：\n'
            '- 直接输出要发的话，简短自然，像真人主动开口，不要解释任务、不要标题前缀；\n'
            '- 结合当前时间（星期、早中晚）让内容应景；不要每次都用一样的模板；\n'
            '- 不想发的内容可以用 [nospeak]理由[/nospeak] 包住且标签外留空，本次就会跳过。'
        )
        usr = (f'【定时任务】{job["brief"]}\n'
               f'当前时间：{now.strftime("%Y-%m-%d %H:%M")} 星期{week}\n'
               f'发送目标：{describe_target(job)}\n'
               '请写出这次要发送的内容。')
        result = await plugin.llm.chat(
            [{'role': 'system', 'content': sys},
             {'role': 'user', 'content': usr}],
            cfg, max_tokens=500, temperature=0.8)
        raw = result.content or ''
        spoken, _hidden = reply.split_nospeak(raw)
        return spoken.strip(), raw
