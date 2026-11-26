# -*- coding: utf-8 -*-
"""断线/重启后补拉群历史（NapCat `get_group_msg_history`）。

## 为什么要有这个

框架的 OneBot 连接是 WebSocket 客户端：断线期间群里发生的事不会重放，
本地 `raw_messages`（背景闲聊语料、history_query 的来源）就出现空档；
模型事后回忆「刚才大家在聊什么」会缺一段。NapCat 提供了
`get_group_msg_history`，可以在连上之后把最近一段补回来。

## 边界（刻意的保守）

- **只补原始语料**（`raw_messages`），不进对话窗、不触发 AI 回复——
  补历史绝不能演变成"对旧消息挨个回复"。
- 只处理**已启用、且本地已有语料**的群：没有历史的新群不去凭空拉。
- 去重靠「发送人 + 正文 + 时间差 ≤ 3s」比对最近 50 条；拿不准的宁可少补。
- 默认只补最近 30 条、最多 20 个群、每个群 5 秒超时：启动阶段不拖慢。
- official 通道没有这个 action，直接跳过（与空间能力同一套 `runtime_env` 判定）。
"""
from __future__ import annotations

import asyncio
import time

from . import runtime_env, storage
from .message import _plain_from_segments

MAX_GROUPS = 20
COUNT = 30
PER_GROUP_TIMEOUT = 5.0
DEDUPE_WINDOW = 3.0
RECENT_FOR_DEDUPE = 50


def _sender_of(msg: dict) -> tuple:
    sender = msg.get('sender') or {}
    uid = str(sender.get('user_id') or msg.get('user_id') or '')
    name = str(sender.get('card') or sender.get('nickname')
               or msg.get('sender_name') or '')
    return uid, name


def _text_of(msg: dict) -> str:
    segs = msg.get('message')
    text = _plain_from_segments(segs) if isinstance(segs, list) else ''
    if not text:
        text = str(msg.get('raw_message') or '')
    return text.strip()[:4000]


def _time_of(msg: dict) -> float:
    try:
        return float(msg.get('time') or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_dup(rows: list, uid: str, name: str, text: str, ts: float) -> bool:
    for r in rows:
        if str(r.get('sender_id') or '') != uid:
            continue
        if str(r.get('content') or '') != text:
            continue
        try:
            rt = float(r.get('created_at') or 0)
        except (TypeError, ValueError):
            continue
        if abs(rt - ts) <= DEDUPE_WINDOW:
            return True
    return False


def _history_of(resp):
    """get_group_msg_history 响应 → messages 列表。"""
    if not isinstance(resp, dict):
        return [], '返回格式异常'
    if resp.get('retcode') not in (0, None) or resp.get('status') in (
            'failed', 'error'):
        return [], str(resp.get('wording') or resp.get('msg')
                       or resp.get('message') or 'NapCat 拒绝请求')
    data = resp.get('data')
    if isinstance(data, dict):
        msgs = data.get('messages')
        if isinstance(msgs, list):
            return msgs, ''
    return [], '响应里没有 messages'


async def backfill_group(plugin, gid: str) -> int:
    """补一个群，返回写入条数。异常由调用方兜。"""
    bot = getattr(plugin, 'bot', None)
    if bot is None or not hasattr(bot, 'call_api'):
        return 0
    db = plugin.db_path
    if storage.last_raw_time(db, gid) <= 0:
        return 0                      # 本地没历史：不凭空空造
    resp = await asyncio.wait_for(
        bot.call_api('get_group_msg_history',
                     {'group_id': int(gid) if str(gid).isdigit()
                      else gid, 'count': COUNT}),
        timeout=PER_GROUP_TIMEOUT)
    msgs, _err = _history_of(resp)
    if not msgs:
        return 0
    recent = storage.recent_raw_messages(db, gid, RECENT_FOR_DEDUPE)
    added = 0
    for m in msgs:
        if not isinstance(m, dict):
            continue
        uid, name = _sender_of(m)
        text = _text_of(m)
        if not text or not uid:
            continue
        ts = _time_of(m) or time.time()
        if _is_dup(recent, uid, name, text, ts):
            continue
        storage.insert_raw_message(db, gid, uid, name, text, created_at=ts)
        recent.append({'sender_id': uid, 'sender_name': name,
                       'content': text, 'created_at': ts})
        added += 1
    return added


async def run(plugin, cfg: dict | None = None) -> dict:
    """对所有有历史的启用群补一次；返回统计（测试/日志用）。"""
    cfg = cfg or getattr(plugin, 'cfg', {}) or {}
    if runtime_env.is_official():
        return {'skipped': 'official', 'groups': 0, 'added': 0}
    if not cfg.get('history_backfill_on', True):
        return {'skipped': 'off', 'groups': 0, 'added': 0}
    done = added = 0
    errors = []
    try:
        groups = storage.list_groups(plugin.db_path)
    except Exception:
        return {'skipped': 'no-db', 'groups': 0, 'added': 0}
    for row in groups[:MAX_GROUPS]:
        if not row.get('enabled'):
            continue
        if (row.get('settings') or {}).get('chat_type') == 'private':
            continue
        gid = str(row.get('group_id') or '')
        if not gid.isdigit():
            continue
        try:
            n = await backfill_group(plugin, gid)
            done += 1
            added += n
        except asyncio.CancelledError:
            raise
        except Exception as e:
            errors.append(f'{gid}:{e}')
    if errors:
        try:
            plugin.log('补拉群历史部分失败：' + '；'.join(errors[:3]), 'warn')
        except Exception:
            pass
    return {'groups': done, 'added': added, 'errors': errors}
