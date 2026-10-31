# -*- coding: utf-8 -*-
"""好友申请 / 群邀请（request 事件）暂存与主人审批。

安全模型：AI 不自主决定任何申请——request 事件到达只落库并私聊通知主人，
批准/拒绝必须由主人在对话里经 request_approve/request_reject 风险工具拍板。
群相关只处理 sub_type='invite'（别人拉机器人进群）；'add'（别人申请入群）忽略。

审批动作（NapCat 实测约定）：
- set_friend_add_request: {flag, approve, remark?}，拒绝无 remark；
- set_group_add_request:  {flag, approve, reason?}，不需要 sub_type。
flag 有时效，过期/已处理的 API 报错按 failed 落库，不自动重试。
"""
from __future__ import annotations

import asyncio
import time

from . import storage


def parse_request_event(event) -> dict | None:
    """RequestEvent → 归一 dict；不关心的类型/缺字段返回 None（纯函数可单测）。"""
    rtype = str(getattr(event, 'request_type', '') or '')
    flag = str(getattr(event, 'flag', '') or '')
    uid = str(getattr(event, 'user_id', '') or '')
    if not flag or not uid:
        return None
    if rtype == 'friend':
        return {'request_type': 'friend', 'sub_type': '', 'user_id': uid,
                'group_id': '', 'flag': flag,
                'comment': str(getattr(event, 'comment', '') or '')}
    if rtype == 'group':
        sub = str(getattr(event, 'sub_type', '') or '')
        if sub != 'invite':
            return None  # 别人申请入群（add）：本插件不处理
        gid = str(getattr(event, 'group_id', '') or '')
        if not gid:
            return None
        return {'request_type': 'group', 'sub_type': sub, 'user_id': uid,
                'group_id': gid, 'flag': flag,
                'comment': str(getattr(event, 'comment', '') or '')}
    return None


def _age_text(ts: float) -> str:
    mins = int((time.time() - ts) / 60)
    if mins < 1:
        return '刚刚'
    if mins < 60:
        return f'{mins} 分钟前'
    hours = mins // 60
    if hours < 48:
        return f'{hours} 小时前'
    return f'{hours // 24} 天前'


class RequestManager:
    def __init__(self, db_path: str, log=None):
        self.db_path = db_path
        self._log = log or (lambda msg, level='info': None)

    # ── 入站：落库 + 通知主人 ──────────────────────────────────

    async def handle(self, event, cfg: dict, bot) -> dict | None:
        info = parse_request_event(event)
        if info is None:
            return None
        # best-effort 补昵称/群名（失败不影响落库）
        info['nickname'] = await self._stranger_nick(bot, info['user_id'])
        group_name = ''
        if info['request_type'] == 'group':
            group_name = await self._group_name(bot, info['group_id'])
        row, created = storage.upsert_pending_request(self.db_path, info)
        if not created:
            self._log(f"申请 flag 重复（{info['request_type']} "
                      f"qq={info['user_id']}），不重复通知", 'warn')
            return row
        self._log(f"收到{('群邀请' if info['request_type'] == 'group' else '好友申请')}"
                  f"：{info['user_id']} → 申请#{row['id']} 已暂存待主人审批")
        if cfg.get('request_notify_on', True):
            owner = str(cfg.get('owner_qq', '') or '')
            if owner and bot:
                try:
                    await bot.send_message(
                        message=self._notify_text(
                            row, info.get('nickname', ''), group_name),
                        target_id=owner, message_type='private')
                except Exception as ex:
                    self._log(f'申请审批通知发送失败: {ex}', 'warn')
        return row

    async def _stranger_nick(self, bot, uid: str) -> str:
        try:
            resp = await asyncio.wait_for(
                bot.call_api('get_stranger_info', {'user_id': int(uid)}),
                timeout=5)
            if isinstance(resp, dict) and resp.get('retcode', 0) == 0:
                return str((resp.get('data') or {}).get('nickname', '') or '')
        except Exception:
            return ''
        return ''

    async def _group_name(self, bot, gid: str) -> str:
        try:
            resp = await asyncio.wait_for(
                bot.call_api('get_group_info', {'group_id': int(gid)}),
                timeout=5)
            if isinstance(resp, dict) and resp.get('retcode', 0) == 0:
                return str((resp.get('data') or {}).get('group_name', '') or '')
        except Exception:
            return ''
        return ''

    def _notify_text(self, row: dict, nickname: str = '',
                     group_name: str = '') -> str:
        if row['request_type'] == 'friend':
            kind, target = '好友申请', ''
        else:
            kind = '群邀请（有人拉我进群）'
            target = f'\n目标群：{group_name or row["group_id"]}'
        who = f'{row["user_id"]}' + (f'（{nickname or row["nickname"]}）'
                                     if nickname or row['nickname'] else '')
        comment = f'\n附言：{row["comment"]}' if row['comment'] else ''
        return (f'[申请审批] 收到一条{kind}（申请#{row["id"]}）\n'
                f'发起人：{who}{target}{comment}\n'
                f'我不会自行决定。要批准/拒绝，直接对我说'
                f'「同意申请#{row["id"]}」或「拒绝申请#{row["id"]}」'
                '（拒绝可附理由）。')

    # ── 主人拍板（工具调用）────────────────────────────────────

    async def decide(self, bot, req_id: int, approve: bool,
                     note: str = '', decided_by: str = '') -> str:
        try:
            req_id = int(req_id)
        except (TypeError, ValueError):
            return '错误: 申请编号必须是数字（先用 request_list 查看）'
        row = storage.get_pending_request(self.db_path, req_id)
        if row is None:
            return f'错误: 找不到申请#{req_id}'
        if row['status'] != 'pending':
            cn = {'approved': '已同意', 'rejected': '已拒绝',
                  'failed': '处理失败'}.get(row['status'], row['status'])
            return f'申请#{req_id}已处理过（{cn}），不能重复操作'
        note = (note or '').strip()[:100]
        if row['request_type'] == 'friend':
            action = 'set_friend_add_request'
            params = {'flag': row['flag'], 'approve': bool(approve)}
            if approve and note:
                params['remark'] = note
        else:
            action = 'set_group_add_request'
            params = {'flag': row['flag'], 'approve': bool(approve)}
            if note:
                params['reason'] = note
        try:
            resp = await bot.call_api(action, params)
        except Exception as ex:
            storage.mark_pending_request(
                self.db_path, req_id, 'failed', decided_by, str(ex))
            return (f'调用 {action} 失败（申请#{req_id} 标记为失败，'
                    f'flag 可能已过期）：{str(ex)[:120]}')
        if isinstance(resp, dict) and resp.get('retcode', 0) not in (0, None):
            why = str(resp.get('msg') or resp.get('wording') or '')[:120]
            storage.mark_pending_request(
                self.db_path, req_id, 'failed', decided_by, why)
            return f'平台拒绝处理申请#{req_id}（已标记失败）：{why or resp}'
        status = 'approved' if approve else 'rejected'
        storage.mark_pending_request(
            self.db_path, req_id, status, decided_by, '')
        verb = '同意' if approve else '拒绝'
        if row['request_type'] == 'friend':
            target = f'好友 {row["user_id"]}'
        else:
            target = f'入群邀请（群 {row["group_id"]}，邀请人 {row["user_id"]}）'
        extra = f'，备注/理由：{note}' if note else ''
        return f'已{verb}申请#{req_id}（{target}）{extra}'.strip()

    # ── 列表（工具 + 通知兜底）─────────────────────────────────

    def list_text(self, status: str = 'pending', limit: int = 10) -> str:
        rows = storage.list_pending_requests(
            self.db_path, status if status in
            ('pending', 'approved', 'rejected', 'failed') else 'pending',
            max(1, min(int(limit or 10), 20)))
        if not rows:
            return ('当前没有待处理申请。' if status == 'pending'
                    else f'没有{status}状态的申请记录。')
        head = {'pending': '待处理', 'approved': '已同意',
                'rejected': '已拒绝', 'failed': '处理失败'}.get(status, status)
        lines = [f'【{head}的申请】（最近 {len(rows)} 条）']
        for r in rows:
            if r['request_type'] == 'friend':
                kind = '好友申请'
                target = ''
            else:
                kind = '群邀请'
                target = f' → 群{r["group_id"]}'
            who = r['nickname'] or ''
            who = f' {r["user_id"]}' + (f'（{who}）' if who else '')
            comment = f' 附言:{r["comment"]}' if r['comment'] else ''
            extra = ''
            if status != 'pending':
                extra = f' [{_age_text(r["decided_at"] or r["created_at"])}处理]'
            lines.append(
                f'#{r["id"]} {kind}{who}{target} · {_age_text(r["created_at"])}'
                f'{comment}{extra}')
        if status == 'pending':
            lines.append('对她说「同意/拒绝 申请#编号」即可拍板。')
        return '\n'.join(lines)
