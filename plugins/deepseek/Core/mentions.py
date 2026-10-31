# -*- coding: utf-8 -*-
"""出站真艾特：模型回复里的「@昵称」在发送出口转成真 [CQ:at,qq=...] 段。

为什么需要：入站消息喂给模型时 @ 已被渲染成纯文本 @昵称，模型回复里写的
@昵称 自然也是假艾特（不会通知到人）。本模块在**发送时**按 昵称→QQ 映射
把文本艾特替换成真 CQ 段；落库历史始终保留模型原文。

名字→QQ 三级来源（任何一级失败都不影响正常回复）：
1. 入站消息即时观察到的发言人（热名，0 RPC，活跃群友立刻可解析）；
2. messages 表近期发言人 storage.group_roster（本地 SQLite）；
3. OneBot get_group_member_list 全群名单（内存 TTL 缓存，默认 10 分钟）。

安全取舍：名字后必须紧跟非文字字符（标点/空格/句尾）才转换，避免「张」
误伤「@张三丰」；同名不同号视为歧义，该名字不转换；@所有人 不转换
（机器人通常无权限，整段 at-all 失败可能拖垮整条消息）。
"""
from __future__ import annotations

import re
import time

from . import storage

ROSTER_TTL = 600          # 群名单 RPC 缓存秒数
ROSTER_FAIL_BACKOFF = 60  # RPC 失败后的退避秒数
MAX_NAME_LEN = 30
# 这些名字永远不转：机器人通常没有 at-all 权限，整段失败会拖垮整条消息
_RESERVED_AT_NAMES = {'所有人', '全体成员', 'everyone', 'all'}

_FENCE_RE = re.compile(r'```[\s\S]*?```')
_CQ_RE = re.compile(r'\[CQ:[a-zA-Z0-9_]+(?:,[^\]]*)?\]')
# 名字后不能继续粘着文字（中英文/数字/下划线），否则可能只是长名字前缀
_NOT_WORD_AFTER = r'(?![0-9A-Za-z_一-鿿])'
_CQ_TRANS = str.maketrans({'&': '&amp;', '[': '&#91;', ']': '&#93;'})


def cq_escape(text: str) -> str:
    return (text or '').translate(_CQ_TRANS)


def render_ats(text: str, name_map: dict | None) -> str:
    """把文本中的 @昵称 / @QQ 替换为真 CQ:at 段；代码块与已有 CQ 段不动。

    name_map: {昵称或QQ字符串: QQ}，允许混入数字键（group_roster 自带 qq→qq）。
    """
    if not text or '@' not in text or not name_map:
        return text
    qq_set = {str(q) for q in name_map.values() if q}
    names = sorted(
        {n for n in name_map
         if n and not str(n).isdigit() and '@' not in n
         and n not in _RESERVED_AT_NAMES
         and 1 <= len(n) <= MAX_NAME_LEN},
        key=len, reverse=True)
    if not names:
        alt = r'\d{5,12}'  # 没有名字表时仍允许裸 QQ（需在 qq_set 内）
    else:
        alt = r'\d{5,12}|' + '|'.join(re.escape(n) for n in names)
    at_re = re.compile('@(' + alt + ')' + _NOT_WORD_AFTER)

    out: list[str] = []
    pos = 0

    def convert_plain(seg: str) -> str:
        if '@' not in seg:
            return seg
        parts, last = [], 0
        for m in at_re.finditer(seg):
            token = m.group(1)
            qq = token if token.isdigit() else str(name_map.get(token, ''))
            if not qq or (token.isdigit() and token not in qq_set):
                continue  # 裸 QQ 必须是本群近期发言人，防模型瞎编号码
            parts.append(cq_escape(seg[last:m.start()]))
            parts.append(f'[CQ:at,qq={qq}]')
            last = m.end()
        parts.append(cq_escape(seg[last:]))
        return ''.join(parts)

    # 代码块原子：里面的 @ 是代码不是艾特
    for fm in _FENCE_RE.finditer(text):
        pre = text[pos:fm.start()]
        out.append(_protect_cq_spans(pre, convert_plain))
        out.append(text[fm.start():fm.end()])
        pos = fm.end()
    out.append(_protect_cq_spans(text[pos:], convert_plain))
    return ''.join(out)


def _protect_cq_spans(seg: str, plain_fn) -> str:
    """已有 CQ 段原样保留（不转义不替换），其余走普通转换。"""
    if '[CQ:' not in seg:
        return plain_fn(seg)
    out, pos = [], 0
    for cm in _CQ_RE.finditer(seg):
        out.append(plain_fn(seg[pos:cm.start()]))
        out.append(seg[cm.start():cm.end()])
        pos = cm.end()
    out.append(plain_fn(seg[pos:]))
    return ''.join(out)


ROLE_CN = {'owner': '群主', 'admin': '管理员', 'member': '普通成员'}
MEMBER_INFO_TTL = 600  # 单个成员信息 RPC 缓存秒数（角色/等级/头衔变化很慢）


def normalize_member_info(data: dict) -> dict | None:
    """OneBot get_group_member_info 的 data → 精简结构；非法返回 None。"""
    if not isinstance(data, dict) or not data.get('user_id'):
        return None
    role = str(data.get('role') or 'member').lower()
    if role not in ROLE_CN:
        role = 'member'
    return {
        'qq': str(data.get('user_id')),
        'role': role,
        'level': str(data.get('level') or '').strip(),
        'title': str(data.get('title') or '').strip(),
        'card': str(data.get('card') or '').strip(),
        'nickname': str(data.get('nickname') or '').strip(),
        'join_time': int(data.get('join_time') or 0),
        'last_sent_time': int(data.get('last_sent_time') or 0),
    }


def describe_member(info: dict) -> str:
    """群主/管理员/普通成员 + 群等级 + 群头衔的一句话描述。"""
    if not info:
        return ''
    parts = [ROLE_CN.get(info.get('role'), '普通成员')]
    if info.get('level'):
        parts.append(f"群等级 Lv.{info['level']}")
    if info.get('title'):
        parts.append(f"群头衔「{info['title']}」")
    return '，'.join(parts)


class MemberInfoDirectory:
    """群成员身份目录：(群, QQ) → 角色/等级/头衔，单成员 RPC + TTL 缓存。

    任何一级失败都返回 None（不影响正常对话）；RPC 失败有退避，
    避免接口持续报错时每轮重试。
    """

    def __init__(self, ttl: float = MEMBER_INFO_TTL):
        self.ttl = ttl
        self._cache: dict[tuple, tuple] = {}   # key → (ts, info|None)
        self._failed: dict[tuple, float] = {}

    async def info(self, gid, qq, bot, refresh: bool = False) -> dict | None:
        gid, qq = str(gid or ''), str(qq or '')
        if not gid or not qq or bot is None:
            return None
        key = (gid, qq)
        now = time.time()
        if not refresh:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
            if now - self._failed.get(key, 0) < ROSTER_FAIL_BACKOFF:
                return None
        getter = getattr(bot, 'get_group_member_info', None)
        if getter is None:
            return None
        try:
            resp = await getter(gid, qq, no_cache=refresh)
        except Exception:
            self._failed[key] = now
            return None
        data = resp.get('data', resp) if isinstance(resp, dict) else resp
        if isinstance(resp, dict) and resp.get('retcode') not in (None, 0):
            self._failed[key] = now
            return None
        info = normalize_member_info(data or {})
        if info is None:
            self._failed[key] = now
        else:
            self._cache[key] = (now, info)
            self._failed.pop(key, None)
        return info


class MemberDirectory:
    """群成员 昵称→QQ 目录：热名 + 本地近期发言人 + RPC 全名单（带缓存）。"""

    def __init__(self, ttl: float = ROSTER_TTL):
        self.ttl = ttl
        self._hot: dict[str, dict[str, str]] = {}
        self._roster: dict[str, dict[str, str]] = {}
        self._fetched_at: dict[str, float] = {}
        self._failed_at: dict[str, float] = {}
        self._fetching: set[str] = set()

    def observe(self, gid, qq, *names) -> None:
        """入站消息即时登记发言人（名片/昵称），活跃群友免 RPC 可艾特。"""
        qq = str(qq or '')
        if not qq:
            return
        m = self._hot.setdefault(str(gid), {})
        for n in names:
            n = (n or '').strip()
            if n and n != qq and n not in m:
                m[n] = qq

    async def resolve(self, gid, bot, db_path: str) -> dict:
        """返回合并后的 {名字/QQ: QQ}；同名冲突的条目剔除。永不抛错。"""
        gid = str(gid)
        await self._maybe_fetch(gid, bot)

        sources: list[dict] = []
        try:
            sources.append(storage.group_roster(db_path, gid))
        except Exception:
            pass
        sources.append(self._hot.get(gid, {}))
        sources.append(self._roster.get(gid, {}))

        merged: dict[str, str] = {}
        for src in sources:
            for name, qq in src.items():
                name, qq = str(name), str(qq or '')
                if not name or not qq:
                    continue
                if name in merged:
                    if merged[name] != qq:
                        merged[name] = ''  # 歧义：稍后剔除
                else:
                    merged[name] = qq
        return {n: q for n, q in merged.items() if q}

    async def _maybe_fetch(self, gid: str, bot) -> None:
        now = time.time()
        if now - self._fetched_at.get(gid, 0) < self.ttl:
            return
        if now - self._failed_at.get(gid, 0) < ROSTER_FAIL_BACKOFF:
            return
        if gid in self._fetching or bot is None:
            return
        getter = getattr(bot, 'get_group_member_list', None)
        if getter is None:
            return
        self._fetching.add(gid)
        try:
            resp = await getter(gid)
            data = resp.get('data', resp) if isinstance(resp, dict) else resp
            m: dict[str, str] = {}
            for u in data or []:
                if not isinstance(u, dict):
                    continue
                qq = str(u.get('user_id') or '')
                if not qq:
                    continue
                for raw in (u.get('card'), u.get('nickname')):
                    nm = (raw or '').strip()
                    if not nm or nm == qq:
                        continue
                    if nm in m:
                        if m[nm] != qq:
                            m[nm] = ''  # 同名歧义标记
                    else:
                        m[nm] = qq
            self._roster[gid] = {n: q for n, q in m.items() if q}
            self._fetched_at[gid] = now
        except Exception:
            self._failed_at[gid] = now
        finally:
            self._fetching.discard(gid)
