# -*- coding: utf-8 -*-
"""官方开放平台 REST + Token 管理。

Token 由 bots.qq.com 换取，缓存到过期前 60 秒；401 时强制刷新并重试一次。
所有请求走 asyncio.to_thread，避免阻塞事件循环。日志/异常里绝不出现
app_secret 和 access_token。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional
from urllib.parse import quote

import requests

from .config import OfficialConfig


class OfficialAPIError(Exception):
    def __init__(self, message: str, status: Optional[int] = None,
                 code: Optional[int] = None, trace_id: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.trace_id = trace_id

    def __str__(self) -> str:
        suffix = []
        if self.code not in (None, 0):
            suffix.append(f"code={self.code}")
        if self.trace_id:
            suffix.append(f"trace_id={self.trace_id}")
        return f"{self.message}（{'，'.join(suffix)}）" if suffix else self.message


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class OfficialAPI:
    def __init__(self, config: OfficialConfig):
        self.config = config
        self._token = ""
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()

    # ── Token ──────────────────────────────────────────────

    async def token(self, force: bool = False) -> str:
        async with self._token_lock:
            now = time.monotonic()
            if not force and self._token and now < self._expires_at - 60:
                return self._token
            body = await asyncio.to_thread(self._fetch_token_sync)
            token = str(body.get("access_token") or "").strip()
            if not token:
                raise OfficialAPIError("开放平台未返回 access_token")
            expires_in = _as_int(body.get("expires_in")) or 7200
            self._token = token
            self._expires_at = time.monotonic() + max(120, expires_in)
            return self._token

    def _fetch_token_sync(self) -> dict:
        try:
            response = requests.post(
                self.config.effective_token_url,
                json={"appId": self.config.app_id,
                      "clientSecret": self.config.app_secret},
                timeout=self.config.request_timeout,
            )
        except requests.RequestException as exc:
            raise OfficialAPIError(
                f"获取 access_token 网络错误: {exc.__class__.__name__}") from exc
        try:
            body = response.json() if response.text.strip() else {}
        except ValueError:
            body = {}
        if response.status_code >= 400 or not isinstance(body, dict):
            code = _as_int(body.get("code")) if isinstance(body, dict) else None
            message = ""
            if isinstance(body, dict):
                message = str(body.get("message") or body.get("msg") or "")
            raise OfficialAPIError(
                f"获取 access_token 失败: {message or 'HTTP ' + str(response.status_code)}",
                status=response.status_code, code=code)
        return body

    # ── 通用请求 ───────────────────────────────────────────

    async def request(self, method: str, path: str,
                      params: Optional[dict] = None,
                      json_body: Optional[dict] = None) -> dict:
        token = await self.token()
        try:
            return await self._request_with_token(method, path, token,
                                                  params, json_body)
        except OfficialAPIError as exc:
            if exc.status != 401:
                raise
        token = await self.token(force=True)
        return await self._request_with_token(method, path, token,
                                              params, json_body)

    async def _request_with_token(self, method: str, path: str, token: str,
                                  params: Optional[dict],
                                  json_body: Optional[dict]) -> dict:
        return await asyncio.to_thread(
            self._request_sync, method, path, token, params, json_body)

    def _request_sync(self, method: str, path: str, token: str,
                      params: Optional[dict],
                      json_body: Optional[dict]) -> dict:
        url = path if path.startswith("http") else self.config.base_url + path
        headers = {"Authorization": f"QQBot {token}",
                   "Content-Type": "application/json"}
        try:
            response = requests.request(
                method, url, params=params, json=json_body, headers=headers,
                timeout=self.config.request_timeout)
        except requests.RequestException as exc:
            raise OfficialAPIError(
                f"官方接口网络错误: {exc.__class__.__name__}") from exc

        text = response.text or ""
        try:
            body = response.json() if text.strip() else {}
        except ValueError:
            body = {"raw": text[:300]}
        if not isinstance(body, dict):
            body = {"raw": body}
        trace_id = (response.headers.get("X-Tps-Trace-ID") or
                    str(body.get("trace_id") or ""))
        code = _as_int(body.get("code"))
        if response.status_code >= 400 or (code not in (None, 0)):
            message = str(body.get("message") or body.get("msg") or
                          f"HTTP {response.status_code}")
            raise OfficialAPIError(message, status=response.status_code,
                                   code=code, trace_id=trace_id)
        return body

    # ── 具体接口 ───────────────────────────────────────────

    async def get_gateway(self) -> dict:
        body = await self.request("GET", "/gateway/bot")
        if not body.get("url"):
            raise OfficialAPIError("网关地址为空")
        return body

    async def get_self_info(self) -> dict:
        return await self.request("GET", "/users/@me")

    async def send_group_message(self, group_id: str, body: dict) -> dict:
        gid = quote(str(group_id), safe="")
        return await self.request("POST", f"/v2/groups/{gid}/messages",
                                  json_body=body)

    async def upload_group_file(self, group_id: str, body: dict) -> dict:
        gid = quote(str(group_id), safe="")
        return await self.request("POST", f"/v2/groups/{gid}/files",
                                  json_body=body)

    async def get_group_info(self, group_id: str) -> dict:
        gid = quote(str(group_id), safe="")
        return await self.request("GET", f"/v2/groups/{gid}/info")

    async def get_group_member_list(self, group_id: str) -> dict:
        gid = quote(str(group_id), safe="")
        return await self.request("GET", f"/v2/groups/{gid}/members")

    async def get_group_member_info(self, group_id: str,
                                    user_id: str) -> dict:
        gid = quote(str(group_id), safe="")
        uid = quote(str(user_id), safe="")
        return await self.request("GET", f"/v2/groups/{gid}/members/{uid}")

    async def delete_group_message(self, group_id: str,
                                   message_id: str) -> dict:
        gid = quote(str(group_id), safe="")
        mid = quote(str(message_id), safe="")
        return await self.request("DELETE",
                                  f"/v2/groups/{gid}/messages/{mid}")

    @staticmethod
    def compact_json(payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))