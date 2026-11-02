# -*- coding: utf-8 -*-
"""official 通道感知 + 关池模式下单轮被动回复预算。

框架 official 通道在 ``get_version_info`` 里暴露：
``transport == 'official'``、``passive_pool_on``、``max_passive_seq``；
napcat 通道没有这些字段，一律按 napcat 处理，行为与旧版完全一致。

**关池**（passive_pool_on=False）时，一轮回复只允许使用触发消息的被动
名额（官方上限 5 条/msg_id）：

- ``TurnPassive.used`` 记录本轮已发条数，qq_send_text / 图片 / 语音 /
  @全体等工具每发一条自动 -1；
- 文本发送按「剩余条数」均分切段：每条字数 ``ceil(总字数/余量)``，
  保证整条回复占用的条数不超过余量。若拟真人切段本来就不超过余量，
  沿用拟真人切段，避免把一两个短句硬拆成碎条。
"""
from __future__ import annotations

import math
import re
import time

_CQ_RE = re.compile(r'\[CQ:[a-zA-Z0-9_]+(?:,[^\]]*)?\]')

_NAPCAT = {'official': False, 'pool_on': True, 'max_seq': 5}

# transport 不会运行期切换：成功检测后进程内永久缓存；
# 失败/非 official 短缓存，避免每轮都打一次 RPC（30s 超时很贵）
_cache: dict = {'info': None, 'retry_at': 0.0}


async def detect_transport(bot) -> dict:
    """返回 {'official','pool_on','max_seq'}；任何异常都退化为 napcat。"""
    if _cache['info'] is not None:
        return _cache['info']
    now = time.monotonic()
    if now < _cache['retry_at']:
        return _NAPCAT
    try:
        resp = await bot.call_api('get_version_info', {})
    except Exception:
        _cache['retry_at'] = now + 60
        return _NAPCAT
    data = resp if isinstance(resp, dict) else {}
    payload = data.get('data') if isinstance(data.get('data'), dict) else data
    if not isinstance(payload, dict) or payload.get('transport') != 'official':
        _cache['retry_at'] = now + 60
        return _NAPCAT
    try:
        max_seq = max(1, min(5, int(payload.get('max_passive_seq') or 5)))
    except (TypeError, ValueError):
        max_seq = 5
    info = {
        'official': True,
        'pool_on': bool(payload.get('passive_pool_on', True)),
        'max_seq': max_seq,
    }
    _cache['info'] = info
    return info


class TurnPassive:
    """一轮对话的 official 发送态：锚定消息 + 已用名额计数。"""

    # 单条官方文本硬上限；超出会话适配层还会再切，白白多吃名额
    HARD_LIMIT = 4000

    def __init__(self, info: dict | None = None,
                 anchor_msg_id: str = '', anchor_user_id: str = ''):
        info = info or _NAPCAT
        self.official = bool(info.get('official'))
        self.pool_on = bool(info.get('pool_on', True))
        self.max_seq = int(info.get('max_seq') or 5)
        self.anchor_msg_id = str(anchor_msg_id or '')
        self.anchor_user_id = str(anchor_user_id or '')
        self.used = 0

    @property
    def budgeted(self) -> bool:
        """关池模式：发送条数受单 msg_id 名额约束。"""
        return self.official and not self.pool_on

    @property
    def remaining(self) -> int:
        return max(0, self.max_seq - self.used)

    def consume(self, n: int = 1) -> None:
        self.used += max(0, int(n))

    def extra(self) -> dict | None:
        """send_message 的扩展参数：让适配层粘住本轮触发消息/用户。"""
        if not self.official:
            return None
        out = {}
        if self.anchor_msg_id:
            out['_official_msg_id'] = self.anchor_msg_id
        if self.anchor_user_id:
            out['_official_user_id'] = self.anchor_user_id
        return out or None

    def plan_text(self, text: str, natural: list[str]) -> list[str]:
        """决定文本实际切条；非关池或余量充足时沿用 natural。"""
        if not self.budgeted:
            return natural
        remaining = self.remaining
        if remaining <= 0:
            return []
        if len(natural) <= remaining:
            return natural
        pieces = even_split(text, remaining, self.HARD_LIMIT)
        if len(pieces) > remaining:
            # CQ 段顺延等原因导致超条：尾部并回最后允许的一条
            pieces = pieces[:remaining - 1] + [''.join(pieces[remaining - 1:])]
        return pieces


def even_split(text: str, budget: int, hard_limit: int = 4000) -> list[str]:
    """均分成 ≤budget 段，每段字数 ceil(L/budget)（受 hard_limit 封顶）。

    CQ 码不可拆：切点落在段内时顺延到段尾。
    """
    text = text or ''
    budget = max(1, int(budget))
    if budget == 1:
        return [text] if text else []
    size = min(hard_limit, max(1, math.ceil(len(text) / budget)))
    chunks: list[str] = []
    rest = text
    while len(rest) > size:
        cut = size
        for m in _CQ_RE.finditer(rest):
            if m.start() < cut < m.end():
                cut = m.end()
                break
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        chunks.append(rest)
    return chunks
