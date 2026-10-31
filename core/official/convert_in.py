# -*- coding: utf-8 -*-
"""官方事件 → OneBot v11 事件 dict。

只处理群聊：GROUP_AT_MESSAGE_CREATE（消息）、GROUP_ADD_ROBOT（入群通知）。
不伪造私聊/好友/频道事件。

入站内容里的 `<qqbot-at-user id="..."/>` / `<qqbot-at-everyone/>` /
`<qqbot-face id="..."/>` 会被转成 OneBot 段；attachments 按 content_type
转成 image/record/video/file。官方群机器人只会收到 @ 消息，若内容里没有
指向机器人的 at（或无法识别机器人 openid），会强制注入一个 at self_id，
让 deepseek 的 at_me / 仅@门禁 / @ 回复逻辑照常工作。
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

from .config import OfficialConfig
from .state import GroupCache, MemberCache, MessageIndex, MessageRecord

_TAG_RE = re.compile(r"<qqbot-([a-zA-Z0-9_-]+)((?:\s+[^<>]*?)?)\s*/?>", re.S)
_ATTR_ID_RE = re.compile(r"""id\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.S)

_IMAGE_TYPES = ("image", "jpg", "jpeg", "png", "gif", "webp")
_VIDEO_TYPES = ("video", "mp4", "mov", "m4v")
_VOICE_TYPES = ("voice", "audio", "silk", "amr", "mp3", "wav", "ogg")


# ── 基础工具 ────────────────────────────────────────────────

def _tail(value: str, length: int = 6) -> str:
    value = str(value or "")
    if not value:
        return "未知"
    return value[-length:]


def fallback_group_name(group_id: str) -> str:
    return f"QQ群({_tail(group_id)})"


def fallback_user_name(user_id: str) -> str:
    return f"用户{_tail(user_id)}"


def parse_timestamp(value) -> int:
    text = str(value or "").strip()
    if not text:
        return int(time.time())
    if text.isdigit():
        number = int(text)
        return number // 1000 if number > 10 ** 12 else number
    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (TypeError, ValueError):
        return int(time.time())


def _cq_escape_text(text: str) -> str:
    return (str(text).replace("&", "&amp;")
            .replace("[", "&#91;").replace("]", "&#93;"))


def _cq_escape_param(text: str) -> str:
    return _cq_escape_text(text).replace(",", "&#44;")


def _append_text(segments: list, text: str) -> None:
    if not text:
        return
    if segments and segments[-1].get("type") == "text":
        segments[-1]["data"]["text"] = (
            str(segments[-1]["data"].get("text", "")) + text)
        return
    segments.append({"type": "text", "data": {"text": text}})


def _append_at(segments: list, qq: str) -> None:
    qq = str(qq or "").strip()
    if not qq:
        return
    segments.append({"type": "at", "data": {"qq": qq}})


# ── 官方 content → OneBot 段 ─────────────────────────────────

def parse_content(content) -> tuple[list, list]:
    """返回 (segments, at_user_ids)。at_user_ids 保留原始 openid 顺序。"""
    text = str(content or "")
    segments: list = []
    at_ids: list = []
    pos = 0
    for match in _TAG_RE.finditer(text):
        if match.start() > pos:
            _append_text(segments, text[pos:match.start()])
        name = match.group(1).lower()
        attrs = match.group(2) or ""
        if name == "at-user":
            id_match = _ATTR_ID_RE.search(attrs)
            raw_id = (id_match.group(1) or id_match.group(2) or "").strip() \
                if id_match else ""
            if raw_id:
                _append_at(segments, raw_id)
                at_ids.append(raw_id)
        elif name == "at-everyone":
            _append_at(segments, "all")
        elif name in ("face", "emoji"):
            _append_text(segments, "[表情]")
        # 其余开放标签（markdown 等）直接丢弃正文，避免污染文本
        pos = match.end()
    if pos < len(text):
        _append_text(segments, text[pos:])
    return segments, at_ids


def resolve_at_targets(segments: list, cfg: OfficialConfig,
                       bot_openid: str) -> tuple[list, bool]:
    """把 {"type":"at"} 里的机器人 openid 换成 self_id，返回 (segments, at_me)。"""
    self_id = cfg.effective_self_id
    at_me = False
    out: list = []
    for seg in segments:
        if seg.get("type") != "at":
            out.append(seg)
            continue
        qq = str(seg.get("data", {}).get("qq") or "")
        if qq.lower() == "all":
            out.append(seg)
            continue
        if bot_openid and qq == str(bot_openid):
            qq = self_id
        if self_id and qq == self_id:
            at_me = True
        _append_at(out, qq)
    return out, at_me


# ── 附件 ────────────────────────────────────────────────────

def _attachment_kind(content_type: str) -> str:
    ctype = str(content_type or "").strip().lower()
    base = ctype.split("/", 1)[-1].split(";", 1)[0]
    if any(token in ctype for token in _IMAGE_TYPES):
        return "image"
    if any(token in ctype for token in _VIDEO_TYPES):
        return "video"
    if any(token in ctype for token in _VOICE_TYPES):
        return "record"
    if base in ("octet-stream", "application", ""):
        # content_type 缺失时按文件名猜
        return "file"
    return "file"


def attachment_filename(att: dict, url: str) -> str:
    name = str(att.get("filename") or att.get("file_name") or "").strip()
    if name:
        return name
    if url:
        path = unquote(urlparse(url).path)
        base = os.path.basename(path)
        if base:
            return base
    return ""


def attachment_segment(att: dict) -> tuple[dict | None, str, str, str]:
    """返回 (segment, kind, url, filename)。无 url 的附件退回文本占位。"""
    if not isinstance(att, dict):
        return None, "", "", ""
    url = str(att.get("url") or "").strip()
    filename = attachment_filename(att, url)
    kind = _attachment_kind(str(att.get("content_type") or ""))
    if not url:
        return None, kind, url, filename
    size = str(att.get("size") or "")
    if kind == "image":
        seg = {"type": "image", "data": {"file": url, "url": url,
                                         "name": filename}}
    elif kind == "record":
        seg = {"type": "record", "data": {"file": url, "url": url,
                                          "name": filename}}
    elif kind == "video":
        seg = {"type": "video", "data": {"file": url, "url": url,
                                         "name": filename}}
    else:
        seg = {"type": "file", "data": {"file": url, "file_url": url,
                                        "url": url, "name": filename,
                                        "size": size}}
    return seg, kind, url, filename


def attachments_to_segments(attachments) -> list:
    segments: list = []
    for att in attachments or []:
        seg, kind, url, filename = attachment_segment(att)
        if seg is None:
            placeholder = {
                "image": "[图片]", "record": "[语音]",
                "video": "[视频]", "file": "[文件]",
            }.get(kind, "[附件]")
            _append_text(segments, placeholder)
            continue
        segments.append(seg)
    return segments


def attachments_to_media(attachments) -> tuple[list, dict]:
    """供 MessageIndex 缓存：附件列表 + 各种引用 → 附件 映射。"""
    from .state import MediaAttachment

    media: list = []
    by_ref: dict = {}
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        seg, kind, url, filename = attachment_segment(att)
        if seg is None:
            continue
        item = MediaAttachment(kind=kind, url=url, filename=filename,
                               content_type=str(att.get("content_type") or ""),
                               raw=dict(att))
        media.append(item)
        for ref in (url, filename, str(att.get("file_id") or "")):
            if ref:
                by_ref[ref] = item
    return media, by_ref


# ── raw_message 序列化 ──────────────────────────────────────

def segments_to_cq(segments: list) -> str:
    parts: list = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        stype = str(seg.get("type") or "")
        data = seg.get("data") or {}
        if stype == "text":
            parts.append(_cq_escape_text(data.get("text", "")))
        elif stype == "at":
            qq = str(data.get("qq", "") or "")
            if not qq:
                continue
            extra = ""
            name = str(data.get("name") or "")
            if name:
                extra = f",name={_cq_escape_param(name)}"
            parts.append(f"[CQ:at,qq={_cq_escape_param(qq)}{extra}]")
        elif stype == "reply":
            mid = str(data.get("id", "") or "")
            if mid:
                parts.append(f"[CQ:reply,id={_cq_escape_param(mid)}]")
        elif stype in ("image", "record", "video"):
            source = str(data.get("url") or data.get("file") or "")
            if source:
                parts.append(f"[CQ:{stype},file={_cq_escape_param(source)}]")
        elif stype == "file":
            source = str(data.get("url") or data.get("file") or "")
            name = str(data.get("name") or "")
            if source:
                params = f"file={_cq_escape_param(source)}"
                if name:
                    params += f",name={_cq_escape_param(name)}"
                parts.append(f"[CQ:file,{params}]")
        elif stype == "face":
            face_id = str(data.get("id", "") or "0")
            parts.append(f"[CQ:face,id={_cq_escape_param(face_id)}]")
        # 未知段不进 raw_message；插件优先读段数组
    return "".join(parts)


# ── 引用信息 ────────────────────────────────────────────────

def _reply_info(d: dict, members: MemberCache,
                messages: MessageIndex) -> dict | None:
    ref = d.get("message_reference") or d.get("message_ref") or {}
    if not isinstance(ref, dict):
        return None
    ref_id = str(ref.get("message_id") or ref.get("id") or "").strip()
    if not ref_id:
        return None
    record = messages.get(ref_id)
    info: dict = {"id": ref_id, "message_id": ref_id}
    if record:
        name = members.display_name(record.group_id, record.user_id) or \
            fallback_user_name(record.user_id)
        info["sender"] = {"user_id": record.user_id, "nickname": name,
                          "card": ""}
        if record.segments:
            info["message"] = list(record.segments)
        if record.raw_message:
            info["raw_message"] = record.raw_message
    else:
        # 未缓存（如重启后）时保留 id；正文摘要留空
        sender = d.get("author") or {}
        uid = str(sender.get("member_openid") or "")
        if uid:
            info["sender"] = {"user_id": uid, "nickname": "", "card": ""}
    return info


# ── 事件构造 ────────────────────────────────────────────────

def _message_event(d: dict, cfg: OfficialConfig, bot_openid: str,
                   groups: GroupCache, members: MemberCache,
                   messages: MessageIndex) -> dict:
    group_id = str(d.get("group_openid") or "").strip()
    author = d.get("author") or {}
    user_id = str(author.get("member_openid") or author.get("id") or "").strip()
    message_id = str(d.get("id") or "").strip()
    timestamp = parse_timestamp(d.get("timestamp"))

    parsed, at_ids = parse_content(d.get("content"))
    parsed, at_me = resolve_at_targets(parsed, cfg, bot_openid)
    self_id = cfg.effective_self_id
    if not at_me and self_id:
        parsed.insert(0, {"type": "at", "data": {"qq": self_id}})
        at_me = True

    reply = _reply_info(d, members, messages)
    attachments = d.get("attachments") or []
    media_segments = attachments_to_segments(attachments)

    segments: list = []
    if reply:
        segments.append({"type": "reply",
                         "data": {"id": reply["id"]}})
    segments.extend(parsed)
    segments.extend(media_segments)

    group_name = groups.name(group_id) or fallback_group_name(group_id)
    member = members.get(group_id, user_id)
    nickname = ""
    card = ""
    role = "member"
    if member:
        nickname = member.nickname or ""
        card = member.card or ""
        role = member.role or "member"
    if not nickname:
        nickname = card or fallback_user_name(user_id)

    event = {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "self_id": self_id,
        "group_id": group_id,
        "group_name": group_name,
        "user_id": user_id,
        "message_id": message_id,
        "time": timestamp,
        "font": 0,
        "raw_message": segments_to_cq(segments),
        "message": segments,
        "sender": {
            "user_id": user_id,
            "nickname": nickname,
            "card": card,
            "role": role,
        },
        "_official": d,
    }
    if reply:
        event["reply"] = reply
        event["_official_reply_id"] = reply["id"]
    event["_official_at_me"] = at_me
    event["_official_at_ids"] = list(at_ids)
    return event


def to_onebot_event(t, d, cfg: OfficialConfig, bot_openid: str,
                    groups: GroupCache, members: MemberCache,
                    messages: MessageIndex) -> dict | None:
    """单个网关 dispatch → OneBot 事件 dict（或 None 表示不关心）。"""
    d = d if isinstance(d, dict) else {}
    if t == "GROUP_AT_MESSAGE_CREATE":
        if not d.get("group_openid"):
            return None
        return _message_event(d, cfg, bot_openid, groups, members, messages)
    if t == "GROUP_ADD_ROBOT":
        group_id = str(d.get("group_openid") or d.get("group_id") or "")
        if not group_id:
            return None
        op = str(d.get("op_member_openid") or
                 (d.get("op_member") or {}).get("member_openid") or "")
        return {
            "post_type": "notice",
            "notice_type": "group_increase",
            "sub_type": "approve",
            "self_id": cfg.effective_self_id,
            "group_id": group_id,
            "group_name": groups.name(group_id) or fallback_group_name(group_id),
            "user_id": cfg.effective_self_id,
            "operator_id": op,
            "time": parse_timestamp(d.get("timestamp")),
            "_official": d,
        }
    return None


def build_message_record(event: dict, attachments) -> MessageRecord:
    """事件转完后再登记消息索引（引用/撤回/媒体查询用）。"""
    media, by_ref = attachments_to_media(attachments)
    return MessageRecord(
        message_id=str(event.get("message_id") or ""),
        group_id=str(event.get("group_id") or ""),
        user_id=str(event.get("user_id") or ""),
        raw=dict(event.get("_official") or {}),
        attachments=media,
        media_by_ref=by_ref,
        segments=list(event.get("message") or []),
        raw_message=str(event.get("raw_message") or ""),
        timestamp=int(event.get("time") or 0),
    )