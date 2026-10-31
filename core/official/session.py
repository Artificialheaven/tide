# -*- coding: utf-8 -*-
"""会话层：官方事件 → OneBot JSON 队列；OneBot action → 官方 REST。

对上层（core/bot.bot）表现得像一个 OneBot WebSocket：
- `recv()` 只返回紧凑 JSON 字符串；
- `submit_action(raw)` 处理 action，并且**必定**往队列塞一条带 echo 的响应。
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Optional

from .api import OfficialAPI, OfficialAPIError
from .config import OfficialConfig
from .convert_in import (build_message_record, fallback_group_name,
                         fallback_user_name, to_onebot_event)
from .convert_out import (ConvertError, SendItem, build_plan,
                          normalize_segments)
from .passive import PassivePool
from .state import GroupCache, MemberCache, MessageIndex, MessageRecord

MAX_TEXT_CHARS = 4000


class SendExhausted(Exception):
    """被动回复名额耗尽且不允许主动发送。"""


def ok_envelope(data=None) -> dict:
    return {"status": "ok", "retcode": 0,
            "data": data if data is not None else {}}


def fail_envelope(message: str, data=None) -> dict:
    return {"status": "failed", "retcode": -1, "data": data,
            "message": str(message), "wording": str(message)}


def _with_flat(envelope: dict, flat: dict) -> dict:
    """Slang 读顶层字段、deepseek 读 data，两边都放。"""
    for key, value in (flat or {}).items():
        envelope[key] = value
        if isinstance(envelope.get("data"), dict):
            envelope["data"].setdefault(key, value)
    return envelope


def _split_text(content: str, limit: int = MAX_TEXT_CHARS) -> list:
    text = str(content or "")
    chunks: list = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


class OfficialSession:
    def __init__(self, config: OfficialConfig, api: OfficialAPI,
                 logger=None):
        self.config = config
        self.api = api
        self._log = logger or (lambda msg: print(f"[官方] {msg}"))

        self.self_id = config.effective_self_id
        self.bot_openid = ""
        self.bot_name = config.bot_name

        self.groups = GroupCache(config.member_cache_ttl,
                                 config.member_failed_retry)
        self.members = MemberCache(config.member_cache_ttl,
                                   config.member_failed_retry)
        self.messages = MessageIndex()
        self.passive = PassivePool(config.passive_window_seconds,
                                   config.max_passive_seq,
                                   config.passive_pool_size)

        self.out_queue: asyncio.Queue = asyncio.Queue()
        self._tasks: set[asyncio.Task] = set()
        self._enrich_inflight: set[tuple] = set()
        self._closed = False

    # ── 网关回调 ──────────────────────────────────────────

    async def handle_ready(self, d: dict) -> None:
        user = d.get("user") or {}
        self.bot_openid = str(user.get("id") or "").strip()
        name = str(user.get("username") or "").strip()
        if name:
            self.bot_name = name
        self._log(f"已就绪：{self.bot_name}（self_id={self.self_id}）")

    async def handle_dispatch(self, t: str, d: dict) -> None:
        try:
            event = to_onebot_event(t, d, self.config, self.bot_openid,
                                    self.groups, self.members, self.messages)
        except Exception as exc:
            self._log(f"事件转换失败 {t}: {exc.__class__.__name__}: {exc}")
            return
        if not event:
            return
        if event.get("post_type") == "message":
            group_id = str(event.get("group_id") or "")
            self.groups.upsert(group_id)
            self.passive.add(group_id, event.get("message_id", ""))
            self.messages.add(build_message_record(
                event, (d or {}).get("attachments") or []))
            self._schedule_enrich(group_id,
                                  str(event.get("user_id") or ""))
        self._emit(event)

    def _emit(self, event: dict) -> None:
        try:
            payload = self.api.compact_json(event)
        except (TypeError, ValueError):
            return
        if not self._closed:
            self.out_queue.put_nowait(payload)

    # ── 缓存补全 ──────────────────────────────────────────

    def _schedule_enrich(self, group_id: str, user_id: str) -> None:
        if not self.config.enable_member_enrich:
            return
        need_group = self.groups.needs_refresh(group_id)
        need_user = bool(user_id) and self.members.needs_refresh(
            group_id, user_id)
        if not need_group and not need_user:
            return
        key = (group_id, user_id)
        if key in self._enrich_inflight:
            return
        self._enrich_inflight.add(key)
        task = asyncio.create_task(self._enrich(group_id, user_id))
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    async def _enrich(self, group_id: str, user_id: str) -> None:
        try:
            if self.groups.needs_refresh(group_id):
                try:
                    raw = await self.api.get_group_info(group_id)
                    self.groups.upsert(group_id, raw=raw)
                except OfficialAPIError:
                    self.groups.mark_failed(group_id)
                except Exception:
                    self.groups.mark_failed(group_id)
            if user_id and self.members.needs_refresh(group_id, user_id):
                try:
                    raw = await self.api.get_group_member_info(group_id,
                                                               user_id)
                    self.members.upsert(group_id, user_id, raw=raw)
                except OfficialAPIError:
                    self.members.mark_failed(group_id, user_id)
                except Exception:
                    self.members.mark_failed(group_id, user_id)
        finally:
            self._enrich_inflight.discard((group_id, user_id))

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            self._log(f"后台任务失败: {exc.__class__.__name__}: {exc}")

    # ── 队列接口 ──────────────────────────────────────────

    async def recv(self) -> str:
        return await self.out_queue.get()

    def submit_action(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            self._log("收到无法解析的 action JSON")
            return
        if not isinstance(payload, dict):
            return
        action = str(payload.get("action") or "")
        params = payload.get("params")
        if not isinstance(params, dict):
            params = {}
        echo = payload.get("echo")
        task = asyncio.create_task(
            self._handle_action(action, params, echo))
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    async def _handle_action(self, action: str, params: dict,
                             echo=None) -> None:
        try:
            envelope = await self.dispatch_action(action, params)
        except asyncio.CancelledError:
            raise
        except OfficialAPIError as exc:
            envelope = fail_envelope(f"官方接口错误: {exc}")
        except Exception as exc:
            envelope = fail_envelope(
                f"适配层内部错误: {exc.__class__.__name__}")
        envelope = dict(envelope)
        if echo not in (None, ""):
            envelope["echo"] = echo
        if not self._closed:
            self.out_queue.put_nowait(self.api.compact_json(envelope))

    async def dispatch_action(self, action: str, params: dict) -> dict:
        action = str(action or "")
        params = params or {}
        if action in ("send_msg", "send_group_msg"):
            return await self._action_send_msg(params)
        if action == "send_private_msg":
            return fail_envelope("官方机器人仅支持群聊消息")
        handler = getattr(self, f"_action_{action}", None)
        if handler is not None:
            return await handler(params)
        return fail_envelope(f"官方机器人暂不支持该操作: {action}")

    # ── 发送 ──────────────────────────────────────────────

    async def _action_send_msg(self, params: dict) -> dict:
        group_id = str(params.get("group_id") or "").strip()
        if not group_id:
            if params.get("user_id"):
                return fail_envelope("官方机器人仅支持群聊消息")
            return fail_envelope("缺少 group_id")
        return await self.send_group(group_id, params.get("message"))

    async def send_group(self, group_id: str, message) -> dict:
        segments = normalize_segments(message)
        try:
            items, reply_id = build_plan(
                segments, self.config.at_all_mode,
                self.config.max_upload_bytes)
        except ConvertError as exc:
            return fail_envelope(str(exc))
        if not items:
            return fail_envelope("消息内容为空")

        items = self._expand_text_items(items)
        sent_count = 0
        last_id = ""
        async with self.passive.lock(group_id):
            for item in items:
                try:
                    response = await self._deliver(group_id, item, reply_id)
                except SendExhausted:
                    return fail_envelope(
                        "被动回复名额已用完（官方限制每条消息 5 条、约 5 分钟）；"
                        "可等群内新消息或开启 official_allow_active",
                        data={"sent_count": sent_count})
                except OfficialAPIError as exc:
                    return fail_envelope(
                        f"官方发送失败: {exc}",
                        data={"sent_count": sent_count})
                sent_count += 1
                last_id = self._response_id(response) or last_id
                if last_id:
                    self.messages.add(MessageRecord(
                        message_id=last_id, group_id=group_id, from_bot=True,
                        timestamp=int(time.time())))
        envelope = ok_envelope({"message_id": last_id,
                                "sent_count": sent_count})
        envelope["message_id"] = last_id
        return envelope

    @staticmethod
    def _expand_text_items(items: list) -> list:
        out: list = []
        for item in items:
            if item.kind == "text" and len(item.content) > MAX_TEXT_CHARS:
                for chunk in _split_text(item.content):
                    out.append(SendItem(kind="text", content=chunk))
            else:
                out.append(item)
        return out

    async def _deliver(self, group_id: str, item: SendItem,
                       prefer_msg_id: str) -> dict:
        media_obj = None
        if item.kind == "media" and item.media is not None:
            media_obj = await self._upload_media(group_id, item.media)

        last_error: Optional[OfficialAPIError] = None
        for attempt in range(2):
            reserve = self.passive.reserve(
                group_id, prefer_msg_id if attempt == 0 else "")
            if reserve is None:
                if self.config.allow_active:
                    body = self._message_body(item, media_obj, None)
                    return await self.api.send_group_message(group_id, body)
                raise SendExhausted()
            body = self._message_body(item, media_obj, reserve)
            try:
                return await self.api.send_group_message(group_id, body)
            except OfficialAPIError as exc:
                # 官方拒绝了该 msg_id：整条槽位作废，换下一条再试一次
                self.passive.invalidate(group_id, reserve.slot.msg_id)
                last_error = exc
        raise last_error or OfficialAPIError("发送失败")

    @staticmethod
    def _message_body(item: SendItem, media_obj: Optional[dict],
                      reserve) -> dict:
        if item.kind == "media":
            body = {"content": "", "msg_type": 7, "media": media_obj or {}}
        else:
            body = {"content": item.content, "msg_type": 0}
        if reserve is not None:
            body["msg_id"] = reserve.slot.msg_id
            body["msg_seq"] = reserve.seq
        return body

    async def _upload_media(self, group_id: str, spec) -> dict:
        body = {"file_type": spec.file_type, "srv_send_msg": False}
        if spec.url:
            body["url"] = spec.url
        elif spec.data is not None:
            body["file_data"] = base64.b64encode(spec.data).decode("ascii")
        else:
            raise OfficialAPIError("媒体来源为空")
        if spec.file_type == 4 and spec.filename:
            body["file_name"] = spec.filename
        response = await self.api.upload_group_file(group_id, body)
        file_info = str(response.get("file_info") or "").strip()
        if not file_info:
            raise OfficialAPIError("媒体上传未返回 file_info")
        media = {"file_info": file_info}
        if spec.file_type == 4 and spec.filename:
            media["file_name"] = spec.filename
        return media

    @staticmethod
    def _response_id(response: dict) -> str:
        if not isinstance(response, dict):
            return ""
        return str(response.get("id") or response.get("message_id") or "")

    # ── 查询 ──────────────────────────────────────────────

    async def _action_get_login_info(self, params: dict) -> dict:
        data = {"user_id": self.self_id, "nickname": self.bot_name}
        return _with_flat(ok_envelope(data), data)

    async def _action_get_group_list(self, params: dict) -> dict:
        groups = []
        for record in self.groups.all():
            raw = record.raw or {}
            groups.append({
                "group_id": record.group_id,
                "group_name": record.name or fallback_group_name(
                    record.group_id),
                "member_count": _int_or(raw.get("member_count"), 0),
                "max_member_count": _int_or(raw.get("max_member_count"), 0),
            })
        return ok_envelope(groups)

    async def _action_get_group_info(self, params: dict) -> dict:
        group_id = str(params.get("group_id") or "").strip()
        if not group_id:
            return fail_envelope("缺少 group_id")
        record = self.groups.get(group_id)
        raw: dict = dict(record.raw) if record and record.raw else {}
        try:
            fresh = await self.api.get_group_info(group_id)
            if isinstance(fresh, dict) and fresh:
                raw.update(fresh)
                self.groups.upsert(group_id, raw=fresh)
                record = self.groups.get(group_id)
        except OfficialAPIError:
            pass
        except Exception:
            pass
        name = (record.name if record else "") or str(
            raw.get("group_name") or "") or fallback_group_name(group_id)
        data = {
            "group_id": group_id,
            "group_name": name,
            "member_count": _int_or(raw.get("member_count"), 0),
            "max_member_count": _int_or(raw.get("max_member_count"), 0),
            "group_create_time": _int_or(raw.get("group_create_time"), 0),
            "group_level": str(raw.get("group_level") or ""),
        }
        return _with_flat(ok_envelope(data), data)

    async def _action_get_group_member_list(self, params: dict) -> dict:
        group_id = str(params.get("group_id") or "").strip()
        if not group_id:
            return fail_envelope("缺少 group_id")
        items: list = []
        try:
            raw = await self.api.get_group_member_list(group_id)
            items = _normalize_member_items(raw)
            for item in items:
                uid = _member_id(item)
                if uid:
                    self.members.upsert(group_id, uid, raw=item)
        except OfficialAPIError:
            pass
        except Exception:
            pass

        members = [self._member_payload(group_id, rec.user_id, rec)
                   for rec in self.members.all(group_id)]
        seen = {m["user_id"] for m in members}
        for item in items:
            uid = _member_id(item)
            if uid and uid not in seen:
                members.append(self._member_payload(group_id, uid))
                seen.add(uid)
        if self.self_id and self.self_id not in seen:
            members.insert(0, self._member_payload(group_id, self.self_id))
        return ok_envelope(members)

    async def _action_get_group_member_info(self, params: dict) -> dict:
        group_id = str(params.get("group_id") or "").strip()
        user_id = str(params.get("user_id") or params.get("user_openid")
                      or "").strip()
        if not group_id or not user_id:
            return fail_envelope("缺少 group_id 或 user_id")
        if self._is_self(user_id):
            payload = self._member_payload(group_id, self.self_id)
            return _with_flat(ok_envelope(payload), payload)
        try:
            raw = await self.api.get_group_member_info(group_id, user_id)
            if isinstance(raw, dict) and raw:
                self.members.upsert(group_id, user_id, raw=raw)
        except OfficialAPIError:
            pass
        except Exception:
            pass
        record = self.members.get(group_id, user_id)
        payload = self._member_payload(group_id, user_id, record)
        return _with_flat(ok_envelope(payload), payload)

    def _is_self(self, user_id: str) -> bool:
        return bool(user_id) and (
            user_id == self.self_id or
            (self.bot_openid and user_id == self.bot_openid))

    def _member_payload(self, group_id: str, user_id: str,
                        record=None) -> dict:
        record = record or self.members.get(group_id, user_id)
        is_self = self._is_self(user_id)
        if is_self:
            role = ("admin" if self.config.at_all_mode == "native"
                    else "member")
            return {
                "group_id": group_id,
                "user_id": self.self_id,
                "nickname": self.bot_name,
                "card": self.bot_name,
                "role": role,
                "join_time": 0,
                "last_sent_time": 0,
                "level": "",
                "title": "",
            }
        nickname = (record.nickname if record else "") or fallback_user_name(
            user_id)
        card = record.card if record else ""
        role = (record.role if record else "") or "member"
        return {
            "group_id": group_id,
            "user_id": user_id,
            "nickname": nickname,
            "card": card,
            "role": role,
            "join_time": 0,
            "last_sent_time": 0,
            "level": "",
            "title": "",
        }

    async def _action_get_stranger_info(self, params: dict) -> dict:
        return fail_envelope("官方机器人没有陌生人资料接口")

    async def _action_get_friend_list(self, params: dict) -> dict:
        return fail_envelope("官方群机器人没有好友列表")

    async def _action_delete_msg(self, params: dict) -> dict:
        message_id = str(params.get("message_id") or "").strip()
        if not message_id:
            return fail_envelope("缺少 message_id")
        group_id = self.messages.group_of(message_id)
        if not group_id:
            return fail_envelope("无法定位该消息所在群（仅支持近期消息）")
        try:
            await self.api.delete_group_message(group_id, message_id)
        except OfficialAPIError as exc:
            return fail_envelope(f"撤回失败: {exc}")
        return ok_envelope({})

    async def _action_get_image(self, params: dict) -> dict:
        return self._media_lookup(params, "image")

    async def _action_get_record(self, params: dict) -> dict:
        return self._media_lookup(params, "record")

    async def _action_get_file(self, params: dict) -> dict:
        return self._media_lookup(params, "file")

    async def _action_get_group_file_url(self, params: dict) -> dict:
        return self._media_lookup(params, "file")

    def _media_lookup(self, params: dict, kind: str) -> dict:
        refs = [params.get("file"), params.get("file_id"),
                params.get("file_uri"), params.get("url")]
        for ref in refs:
            ref = str(ref or "").strip()
            if not ref:
                continue
            if kind != "file" and ref.startswith(("http://", "https://")):
                data = {"url": ref, "file": ref}
                return _with_flat(ok_envelope(data), data)
            found = self.messages.find_media(ref)
            if found:
                data = {
                    "file": found.filename or ref,
                    "url": found.url,
                    "file_name": found.filename,
                    "file_size": str((found.raw or {}).get("size") or 0),
                }
                return _with_flat(ok_envelope(data), data)
        return fail_envelope(f"未在近期入站附件里找到该{kind}")

    async def _action_get_forward_msg(self, params: dict) -> dict:
        return fail_envelope("官方机器人不支持合并转发")

    async def _action_get_msg(self, params: dict) -> dict:
        message_id = str(params.get("message_id") or "").strip()
        record = self.messages.get(message_id)
        if not record:
            return fail_envelope("消息不在本地缓存")
        data = {
            "message_id": record.message_id,
            "group_id": record.group_id,
            "user_id": record.user_id,
            "time": record.timestamp,
            "raw_message": record.raw_message,
            "message": record.segments,
            "sender": {"user_id": record.user_id,
                       "nickname": self.members.display_name(
                           record.group_id, record.user_id) or
                       fallback_user_name(record.user_id)},
        }
        return ok_envelope(data)

    async def _action_get_version_info(self, params: dict) -> dict:
        data = {"app_name": "QQOfficial", "app_version": "1.0.0",
                "protocol_version": "v11"}
        return _with_flat(ok_envelope(data), data)

    async def _action_get_status(self, params: dict) -> dict:
        data = {"online": True, "good": True}
        return _with_flat(ok_envelope(data), data)

    async def _action_can_send_image(self, params: dict) -> dict:
        return _with_flat(ok_envelope({"yes": True}), {"yes": True})

    async def _action_can_send_record(self, params: dict) -> dict:
        return _with_flat(ok_envelope({"yes": True}), {"yes": True})

    # ── 关闭 ──────────────────────────────────────────────

    async def close(self) -> None:
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        self.passive.clear()


# ── 模块级小工具 ────────────────────────────────────────────

def _int_or(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _member_id(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("member_openid") or item.get("openid") or
               item.get("user_id") or "").strip()


def _normalize_member_items(raw) -> list:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if not isinstance(raw, dict):
        return []
    for key in ("items", "members", "list", "data"):
        value = raw.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    # 单成员信息直接返回 dict 的情况
    if _member_id(raw):
        return [raw]
    return []