# -*- coding: utf-8 -*-
"""OneBot 消息（CQ 字符串/段数组）→ 官方发送项。

一条 OneBot 消息会被拆成若干「发送项」：纯文本合并成一条，
每个媒体单独上传后发一条。每一发送项都要消耗一个被动回复名额。
"""
from __future__ import annotations

import base64
import binascii
import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote, urlparse

_CQ_RE = re.compile(r"\[CQ:([A-Za-z0-9_.-]+)((?:,[^\]]*)?)\]")
_CQ_KV_RE = re.compile(r"([A-Za-z0-9_.-]+)=([^,\]]*)")


class ConvertError(ValueError):
    """消息无法转换成官方发送项（媒体来源无效、超限等）。"""


# 官方 file_type
MEDIA_FILE_TYPES = {"image": 1, "video": 2, "record": 3, "file": 4}

# 非媒体 CQ 的文本占位
_CQ_PLACEHOLDERS = {
    "face": "[表情]", "mface": "[表情]", "sface": "[表情]",
    "json": "[卡片]", "xml": "[卡片]",
    "forward": "[合并转发]", "node": "[合并转发]",
    "contact": "[名片]", "share": "[链接]", "music": "[音乐]",
    "location": "[位置]", "gift": "[礼物]", "dice": "[骰子]",
    "rps": "[猜拳]", "poke": "[戳一戳]", "reply": "",
}


@dataclass
class MediaSpec:
    kind: str                 # image / record / video / file
    file_type: int
    url: str = ""
    data: Optional[bytes] = None
    filename: str = ""


@dataclass
class SendItem:
    kind: str                 # text / media
    content: str = ""
    media: Optional[MediaSpec] = None


def _cq_unescape(text: str) -> str:
    return (str(text).replace("&amp;", "&")
            .replace("&#91;", "[").replace("&#93;", "]")
            .replace("&#44;", ","))


def _parse_cq_params(raw: str) -> dict:
    data = {}
    for key, value in _CQ_KV_RE.findall(raw or ""):
        data[key] = _cq_unescape(value)
    return data


def parse_cq(text: str) -> list:
    """CQ 字符串 → 段数组；无 CQ 时返回单 text 段。"""
    segments: list = []
    pos = 0
    for match in _CQ_RE.finditer(str(text or "")):
        if match.start() > pos:
            segments.append({"type": "text",
                             "data": {"text": text[pos:match.start()]}})
        segments.append({"type": match.group(1).lower(),
                         "data": _parse_cq_params(match.group(2))})
        pos = match.end()
    if pos < len(text or ""):
        segments.append({"type": "text", "data": {"text": text[pos:]}})
    return segments


def normalize_segments(message) -> list:
    """统一成 [{type, data}]；空段丢弃。"""
    if isinstance(message, str):
        return parse_cq(message)
    if not isinstance(message, list):
        return []
    out: list = []
    for item in message:
        if not isinstance(item, dict):
            continue
        stype = str(item.get("type") or "").strip().lower()
        if not stype:
            continue
        data = item.get("data")
        if not isinstance(data, dict):
            data = {}
        out.append({"type": stype, "data": dict(data)})
    return out


def extract_reply_id(segments: list) -> str:
    for seg in segments:
        if seg.get("type") == "reply":
            return str(seg.get("data", {}).get("id") or "").strip()
    return ""


# ── 媒体来源解析 ────────────────────────────────────────────

def _guess_filename(source: str, fallback: str = "") -> str:
    name = str(fallback or "").strip()
    if name:
        return name
    if source.startswith("http://") or source.startswith("https://"):
        path = unquote(urlparse(source).path)
        base = os.path.basename(path)
        if base:
            return base
    if source.startswith("file://"):
        source = source[7:]
    base = os.path.basename(unquote(source))
    return base or "file"


def _decode_base64(source: str) -> bytes:
    body = source[len("base64://"):] if source.startswith("base64://") else source
    if "," in body and body.split(",", 1)[0].startswith("data:"):
        body = body.split(",", 1)[1]
    try:
        return base64.b64decode(body, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ConvertError("base64 媒体数据无效") from exc


def _resolve_media(seg: dict, max_upload_bytes: int) -> MediaSpec:
    stype = seg.get("type") or ""
    data = seg.get("data") or {}
    file_type = MEDIA_FILE_TYPES.get(stype)
    if not file_type:
        raise ConvertError(f"不支持的媒体类型: {stype}")
    url = str(data.get("url") or "").strip()
    source = str(data.get("file") or data.get("path") or "").strip()
    filename = _guess_filename(url or source, data.get("name")
                               or data.get("filename") or "")

    if url.startswith(("http://", "https://")):
        return MediaSpec(kind=stype, file_type=file_type, url=url,
                         filename=filename)
    if source.startswith(("http://", "https://")):
        return MediaSpec(kind=stype, file_type=file_type, url=source,
                         filename=filename)
    if not source:
        raise ConvertError(f"{stype} 段缺少媒体来源（file/url）")

    if source.startswith("base64://") or (
            source.startswith("data:") and ";base64," in source):
        payload = _decode_base64(source)
        if not payload:
            raise ConvertError("base64 媒体数据为空")
        if len(payload) > max_upload_bytes:
            raise ConvertError(
                f"媒体超出上传上限（{len(payload)} > {max_upload_bytes} 字节）")
        return MediaSpec(kind=stype, file_type=file_type, data=payload,
                         filename=filename)

    path = source
    if path.startswith("file://"):
        path = path[len("file://"):]
    path = unquote(path)
    try:
        if not os.path.isfile(path):
            raise ConvertError(f"本地媒体不存在: {os.path.basename(path)}")
        size = os.path.getsize(path)
        if size > max_upload_bytes:
            raise ConvertError(
                f"媒体超出上传上限（{size} > {max_upload_bytes} 字节）")
        with open(path, "rb") as handle:
            payload = handle.read()
    except OSError as exc:
        raise ConvertError("读取本地媒体失败") from exc
    if not payload:
        raise ConvertError("本地媒体为空")
    return MediaSpec(kind=stype, file_type=file_type, data=payload,
                     filename=filename)


# ── 发送计划 ────────────────────────────────────────────────

def at_tag(qq: str, at_all_mode: str = "native") -> str:
    qq = str(qq or "").strip()
    if not qq:
        return ""
    if qq.lower() == "all":
        if at_all_mode == "native":
            return "<qqbot-at-everyone/>"
        if at_all_mode == "text":
            return "@全体成员"
        return ""
    return f'<qqbot-at-user id="{qq}"/>'


def build_plan(segments: list, at_all_mode: str = "native",
               max_upload_bytes: int = 5 * 1024 * 1024,
               resolve_media: bool = True) -> tuple[list, str]:
    """返回 (发送项列表, reply_id)。媒体来源无效时抛出 ConvertError。"""
    items: list[SendItem] = []
    buffer: list[str] = []
    reply_id = ""

    def flush() -> None:
        content = "".join(buffer).strip()
        buffer.clear()
        if content:
            items.append(SendItem(kind="text", content=content))

    for seg in segments:
        stype = str(seg.get("type") or "")
        data = seg.get("data") or {}
        if stype == "text":
            text = str(data.get("text") or "")
            if text:
                buffer.append(text)
            continue
        if stype == "reply":
            if not reply_id:
                reply_id = str(data.get("id") or "").strip()
            continue
        if stype == "at":
            tag = at_tag(data.get("qq", ""), at_all_mode)
            if tag:
                buffer.append(tag)
            continue
        if stype in MEDIA_FILE_TYPES:
            flush()
            spec = (_resolve_media(seg, max_upload_bytes) if resolve_media
                    else MediaSpec(kind=stype,
                                   file_type=MEDIA_FILE_TYPES[stype],
                                   url=str(data.get("url") or
                                           data.get("file") or ""),
                                   filename=str(data.get("name") or "")))
            items.append(SendItem(kind="media", media=spec))
            continue
        placeholder = _CQ_PLACEHOLDERS.get(stype)
        if placeholder:
            buffer.append(placeholder)
        # 其余类型（如 unsupported 的 redbag/shake）直接忽略，不阻断整条消息

    flush()
    return items, reply_id