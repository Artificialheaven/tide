# -*- coding: utf-8 -*-
"""官方事件的本地缓存：群、成员、消息索引。

官方群事件只带 openid，昵称/群名要靠 REST 补全；补全结果缓存起来，
后续入站映射直接使用，避免每条消息都打接口。缓存 TTL 只影响「是否刷新」，
不丢旧值（旧名字仍比降级名好）。
"""
from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Optional


def _now() -> float:
    return time.monotonic()


@dataclass
class GroupRecord:
    group_id: str
    name: str = ""
    raw: dict = field(default_factory=dict)
    updated_at: float = 0.0


@dataclass
class MemberRecord:
    group_id: str
    user_id: str
    nickname: str = ""
    card: str = ""
    role: str = "member"
    raw: dict = field(default_factory=dict)
    updated_at: float = 0.0


class GroupCache:
    def __init__(self, ttl: int = 600, failed_retry: int = 300):
        self.ttl = max(0, int(ttl))
        self.failed_retry = max(0, int(failed_retry))
        self._data: "OrderedDict[str, GroupRecord]" = OrderedDict()
        self._failed: dict[str, float] = {}

    def get(self, group_id: str) -> Optional[GroupRecord]:
        return self._data.get(str(group_id))

    def name(self, group_id: str) -> str:
        record = self.get(group_id)
        return record.name if record else ""

    def upsert(self, group_id: str, name: str = "",
               raw: Optional[dict] = None) -> GroupRecord:
        gid = str(group_id)
        record = self._data.get(gid) or GroupRecord(group_id=gid)
        if name:
            record.name = str(name)
        if raw:
            record.raw = dict(raw)
            if not record.name:
                record.name = str(raw.get("group_name") or
                                  raw.get("name") or "")
        record.updated_at = _now()
        self._data[gid] = record
        self._data.move_to_end(gid)
        self._failed.pop(gid, None)
        while len(self._data) > 512:
            self._data.popitem(last=False)
        return record

    def mark_failed(self, group_id: str) -> None:
        self._failed[str(group_id)] = _now()

    def needs_refresh(self, group_id: str) -> bool:
        gid = str(group_id)
        record = self._data.get(gid)
        now = _now()
        if record and record.name:
            if self.ttl == 0 or now - record.updated_at < self.ttl:
                return False
        failed_at = self._failed.get(gid)
        if failed_at is not None and now - failed_at < self.failed_retry:
            return False
        return True

    def all(self) -> list[GroupRecord]:
        return list(self._data.values())


class MemberCache:
    def __init__(self, ttl: int = 600, failed_retry: int = 300):
        self.ttl = max(0, int(ttl))
        self.failed_retry = max(0, int(failed_retry))
        self._data: "OrderedDict[tuple[str, str], MemberRecord]" = OrderedDict()
        self._failed: dict[tuple[str, str], float] = {}

    def get(self, group_id: str, user_id: str) -> Optional[MemberRecord]:
        return self._data.get((str(group_id), str(user_id)))

    def display_name(self, group_id: str, user_id: str) -> str:
        record = self.get(group_id, user_id)
        if not record:
            return ""
        return record.card or record.nickname or ""

    def upsert(self, group_id: str, user_id: str, nickname: str = "",
               card: str = "", role: str = "member",
               raw: Optional[dict] = None) -> MemberRecord:
        key = (str(group_id), str(user_id))
        record = self._data.get(key) or MemberRecord(group_id=key[0],
                                                     user_id=key[1])
        if nickname:
            record.nickname = str(nickname)
        if card:
            record.card = str(card)
        if role:
            record.role = str(role)
        if raw:
            record.raw = dict(raw)
            record.nickname = record.nickname or str(
                raw.get("nickname") or raw.get("username") or "")
            record.card = record.card or str(raw.get("card") or "")
        record.updated_at = _now()
        self._data[key] = record
        self._data.move_to_end(key)
        self._failed.pop(key, None)
        while len(self._data) > 4096:
            self._data.popitem(last=False)
        return record

    def mark_failed(self, group_id: str, user_id: str) -> None:
        self._failed[(str(group_id), str(user_id))] = _now()

    def needs_refresh(self, group_id: str, user_id: str) -> bool:
        key = (str(group_id), str(user_id))
        record = self._data.get(key)
        now = _now()
        if record and (record.nickname or record.card):
            if self.ttl == 0 or now - record.updated_at < self.ttl:
                return False
        failed_at = self._failed.get(key)
        if failed_at is not None and now - failed_at < self.failed_retry:
            return False
        return True

    def all(self, group_id: str | None = None) -> list[MemberRecord]:
        if group_id is None:
            return list(self._data.values())
        gid = str(group_id)
        return [rec for rec in self._data.values() if rec.group_id == gid]


@dataclass
class MediaAttachment:
    kind: str           # image / record / video / file
    url: str = ""
    filename: str = ""
    content_type: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class MessageRecord:
    message_id: str
    group_id: str
    user_id: str = ""
    from_bot: bool = False
    raw: dict = field(default_factory=dict)
    attachments: list[MediaAttachment] = field(default_factory=list)
    media_by_ref: dict[str, MediaAttachment] = field(default_factory=dict)
    segments: list = field(default_factory=list)
    raw_message: str = ""
    timestamp: int = 0


class MessageIndex:
    """message_id → 群/发送者/附件，供引用、撤回、媒体查询使用。"""

    def __init__(self, capacity: int = 2000):
        self.capacity = max(32, int(capacity))
        self._data: "OrderedDict[str, MessageRecord]" = OrderedDict()

    def add(self, record: MessageRecord) -> None:
        if not record.message_id:
            return
        self._data[record.message_id] = record
        self._data.move_to_end(record.message_id)
        while len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def get(self, message_id: str) -> Optional[MessageRecord]:
        return self._data.get(str(message_id))

    def group_of(self, message_id: str) -> str:
        record = self.get(message_id)
        return record.group_id if record else ""

    def find_media(self, ref: str) -> Optional[MediaAttachment]:
        ref = str(ref or "").strip()
        if not ref:
            return None
        for record in reversed(self._data.values()):
            found = record.media_by_ref.get(ref)
            if found:
                return found
            for att in record.attachments:
                if ref in (att.url, att.filename, str(att.raw.get("file_id") or "")):
                    return att
        return None