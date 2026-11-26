"""约定/待办/未完结话题：模型工具、开口时被动注入、到点主动提醒。

- 责任人必须能解析到真实 QQ（复用记忆模块的名册解析，防张冠李戴）
- 主动提醒循环跑在 asyncio 里，on_start 启动 / on_stop 取消；
  发过即置 reminded，重连不重复提醒
- SQLite 操作全部 to_thread，不阻塞事件循环
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import time

from .asyncutil import cancel_task
from . import storage

_DUE_FORMATS = ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d')


def parse_due(text: str) -> float | None:
    """把 LLM/工具给的 YYYY-MM-DD[ HH:MM] 转成本地时间戳；解析不了返回 None。"""
    s = (text or '').strip()
    if not s:
        return None
    for i, fmt in enumerate(_DUE_FORMATS):
        try:
            dt = _dt.datetime.strptime(s, fmt)
            if fmt == '%Y-%m-%d':
                dt = dt.replace(hour=9)  # 只有日期 → 当天早上提醒
            return dt.timestamp()
        except ValueError:
            continue
    return None


class TaskManager:
    def __init__(self, db_path: str, memory, log=None):
        self.db_path = db_path
        self.memory = memory
        self._log = log or (lambda msg: None)
        self._reminder_task: asyncio.Task | None = None

    # ── 模型工具入口 ────────────────────────────────────────────

    async def add_task_tool(self, group_id, owner_name: str, content: str,
                            kind: str = 'task', due_text: str = '',
                            due_date: str = '', sender_id: str = '',
                            sender_name: str = '') -> str:
        content = (content or '').strip()
        if not content:
            return '错误: content 必填'
        owner_name = (owner_name or '').strip()
        uid, label = await self._resolve_owner(
            group_id, owner_name, sender_id, sender_name)
        if not uid:
            return f'未找到群友「{owner_name}」，请用本群近期出现过的昵称或QQ号'
        due_text = (due_text or '').strip()
        due_date = (due_date or '').strip()
        due_at = parse_due(due_date)
        if due_date and due_at is None:
            due_text = f'{due_text} {due_date}'.strip()  # 解析不了只留文字，不编造
        cid = await asyncio.to_thread(
            storage.add_commitment, self.db_path, group_id, uid, label,
            content[:200], kind, due_text[:40], due_at, 'tool')
        return f'已记下（{cid}）'

    async def list_tasks_tool(self, group_id, owner_name: str = '',
                              sender_id: str = '') -> str:
        if owner_name.strip():
            uid, label = await self._resolve_owner(
                group_id, owner_name, sender_id, '')
            if not uid:
                return f'未找到群友「{owner_name}」'
            rows = await asyncio.to_thread(
                storage.open_commitments_for, self.db_path, group_id, [uid], 10)
            title = f'{label} 的未结事项'
        else:
            rows = await asyncio.to_thread(
                storage.list_commitments, self.db_path, group_id, 'open', 15)
            title = '本群未结事项'
        if not rows:
            return '没有未结事项'
        lines = [title + '：']
        for r in rows:
            due = f'（{r["due_text"]}）' if r['due_text'] else ''
            tag = '话题' if r['kind'] == 'topic' else '待办'
            lines.append(f'- [{tag}]{r["id"]} {r["owner_name"]}：{r["content"]}{due}')
        return '\n'.join(lines)[:2000]

    async def close_task_tool(self, commitment_id, result: str = '') -> str:
        try:
            cid = int(commitment_id)
        except (TypeError, ValueError):
            return '错误: id 必须是数字'
        ok = await asyncio.to_thread(storage.set_commitment_status,
                                     self.db_path, cid, 'done')
        return '已标记完成' if ok else '未找到该事项'

    async def _resolve_owner(self, group_id, owner_name, sender_id, sender_name):
        owner_name = (owner_name or '').strip()
        if not owner_name:
            # 默认记在说话人自己头上
            return (str(sender_id), sender_name) if sender_id else ('', '')
        uid = await asyncio.to_thread(self.memory.resolve_user, group_id, owner_name)
        return (uid, owner_name) if uid else ('', owner_name)

    # ── 开口时被动注入 ──────────────────────────────────────────

    async def open_block(self, group_id, user_ids, budget: int = 120,
                         limit: int = 3) -> str:
        """当事人有未结事项时给模型的提醒段；无内容返回 ''。

        P0-1：open_commitments_for 是同步 DAL，本轮对话里每轮都调，
        放线程池跑，避免阻塞事件循环（调用方需 await）。
        """
        rows = await asyncio.to_thread(
            storage.open_commitments_for,
            self.db_path, group_id, user_ids, limit + 3)
        lines, used = [], 0
        now = time.time()
        for r in rows:
            due = ''
            if r['due_at']:
                overdue = r['due_at'] <= now
                due = '（已到约定时间，可主动问起）' if overdue else \
                      f'（约期：{r["due_text"] or "近期"}）'
            tag = '话题' if r['kind'] == 'topic' else '待办'
            line = f'- {r["owner_name"]}的{tag}：{r["content"]}{due}'
            if used + len(line) > budget and lines:
                break
            lines.append(line)
            used += len(line)
            if len(lines) >= limit:
                break
        if not lines:
            return ''
        return ('【未完结的约定/话题】（合适时自然提起，已完成的事项不要再提）\n'
                + '\n'.join(lines))

    # ── 抽取结果落库 ────────────────────────────────────────────

    async def apply_extracted(self, group_id, roster: dict, commitments: list,
                              resolve_name) -> int:
        """记忆抽取附带的约定：责任人映射到 QQ 后落库；done 则关闭旧事项。"""
        applied = 0
        for c in commitments:
            uid = await asyncio.to_thread(resolve_name, roster, c['owner'])
            if not uid:
                continue
            if c['op'] == 'done':
                old = await asyncio.to_thread(
                    storage.find_open_commitment, self.db_path, group_id,
                    uid, c['content'])
                if old:
                    await asyncio.to_thread(storage.set_commitment_status,
                                            self.db_path, old['id'], 'done')
                    applied += 1
                continue
            due_at = parse_due(c['due_date'])
            await asyncio.to_thread(
                storage.add_commitment, self.db_path, group_id, uid, c['owner'],
                c['content'], c['kind'], c['due_text'], due_at, 'extract')
            applied += 1
        return applied

    # ── 主动提醒循环 ────────────────────────────────────────────

    def start(self, plugin):
        if self._reminder_task is not None:
            return
        self._reminder_task = asyncio.create_task(self._loop(plugin))

    async def stop(self):
        task = self._reminder_task
        self._reminder_task = None
        # cancel + 有界等待；外层 CancelledError 也不冒泡，保证 on_stop 后续段可达
        await cancel_task(task, timeout=3.0)

    async def _loop(self, plugin):
        while True:
            try:
                await asyncio.sleep(
                    max(15, int(plugin.cfg.get('reminder_check_seconds', 60))))
                await self._tick(plugin)
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                self._log(f'提醒循环异常: {ex}')
                await asyncio.sleep(30)

    async def _tick(self, plugin):
        if not plugin.cfg.get('reminder_on', True) or not getattr(plugin, 'bot', None):
            return
        now = time.time()
        rows = await asyncio.to_thread(storage.due_commitments, self.db_path, now)
        for r in rows:
            g = await asyncio.to_thread(storage.get_group, self.db_path, r['group_id'])
            if not g:
                await asyncio.to_thread(storage.mark_reminded, self.db_path, r['id'])
                continue
            settings = g.get('settings') or {}
            chat_type = 'private' if settings.get('chat_type') == 'private' else 'group'
            if not g['enabled']:
                if chat_type == 'private':
                    # 私聊会话被关闭：这条约定不再有投递通道，标记作废
                    await asyncio.to_thread(storage.mark_reminded,
                                             self.db_path, r['id'])
                else:
                    # 群被关闭：不标记（重新开启后还会提醒），但必须顺延到期
                    # 时间——否则长期关闭的群每轮都占满 due_commitments 的
                    # LIMIT 100 窗口，别的群的提醒排不进来（饥饿）。
                    await asyncio.to_thread(storage.snooze_commitment,
                                             self.db_path, r['id'], now + 3600)
                continue
            eff_cfg = {**plugin.cfg, **settings}
            if not eff_cfg.get('reminder_on', True):
                # 群级关闭：不标记（将来重开还会提醒），但必须顺延到期时间，
                # 否则这些行会一直占住 due_commitments 的 LIMIT 100 窗口，
                # 其它群的提醒永远进不了查询
                await asyncio.to_thread(storage.snooze_commitment,
                                         self.db_path, r['id'], now + 3600)
                continue
            kind = '你之前提到的话题' if r['kind'] == 'topic' else '你有个约定到时间了'
            body = f'{kind}：{r["content"]}' + (
                f'（{r["due_text"]}）' if r['due_text'] else '')
            target = str(r['group_id'])
            message_type = 'group' if chat_type == 'group' else 'private'
            text = (f'[CQ:at,qq={r["owner_id"]}] {body}'
                    if message_type == 'group' else body)
            try:
                await plugin.bot.send_message(
                    message=text, target_id=target, message_type=message_type)
                await asyncio.to_thread(storage.mark_reminded,
                                         self.db_path, r['id'])
            except Exception as ex:
                self._log(f'提醒发送失败 {r["id"]}: {ex}')
