# -*- coding: utf-8 -*-
"""官方机器人配置：从 configs.conf 解析，环境变量优先。

凭据只存内存，不写回文件；任何日志/响应里都不出现 app_secret。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


class OfficialConfigError(ValueError):
    """配置不可用（缺凭据、模式非法等），属于不可恢复错误。"""


_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def _as_int(value: Any, default: int, minimum: int | None = None,
            maximum: int | None = None) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def _as_float(value: Any, default: float, minimum: float | None = None,
              maximum: float | None = None) -> float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def _as_mode(value: Any, default: str, allowed: set[str]) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def mask_secret(value: str) -> str:
    """给 Web/GUI 展示用的掩码；空值仍为空。"""
    if not value:
        return ""
    return "********"


@dataclass
class OfficialConfig:
    app_id: str = ""
    app_secret: str = field(default="", repr=False)
    sandbox: bool = False
    self_id: str = ""
    bot_name: str = "QQ官方机器人"

    allow_active: bool = False
    at_all_mode: str = "native"          # native | text | drop
    passive_window_seconds: int = 295
    max_passive_seq: int = 5
    passive_pool_size: int = 64
    max_upload_bytes: int = 5 * 1024 * 1024

    member_cache_ttl: int = 600
    member_failed_retry: int = 300
    enable_member_enrich: bool = True

    request_timeout: int = 20
    ws_reconnect_base_delay: float = 2.0
    ws_reconnect_max_delay: float = 60.0
    intents: int = 1 << 25               # GROUP_AT_MESSAGE_CREATE

    # 仅供测试覆盖
    api_base: str = ""
    token_url: str = ""

    extra: dict = field(default_factory=dict, repr=False)

    # ── 派生值 ──────────────────────────────────────────────

    @property
    def effective_self_id(self) -> str:
        return str(self.self_id or self.app_id or "")

    @property
    def base_url(self) -> str:
        if self.api_base:
            return self.api_base.rstrip("/")
        host = ("https://sandbox.api.sgroup.qq.com" if self.sandbox
                else "https://api.sgroup.qq.com")
        return host

    @property
    def effective_token_url(self) -> str:
        return self.token_url or "https://bots.qq.com/app/getAppAccessToken"

    # ── 构造 ────────────────────────────────────────────────

    @classmethod
    def from_conf(cls, conf: dict) -> "OfficialConfig":
        conf = conf or {}
        app_id = str(os.environ.get("QQBOT_APP_ID") or
                     conf.get("official_app_id") or "").strip()
        app_secret = str(os.environ.get("QQBOT_APP_SECRET") or
                         conf.get("official_app_secret") or "").strip()
        self_id = str(conf.get("official_self_id") or "").strip()
        if not self_id:
            self_id = app_id
        cfg = cls(
            app_id=app_id,
            app_secret=app_secret,
            sandbox=_as_bool(conf.get("official_sandbox"), False),
            self_id=self_id,
            bot_name=str(conf.get("official_bot_name") or
                         "QQ官方机器人").strip() or "QQ官方机器人",
            allow_active=_as_bool(conf.get("official_allow_active"), False),
            at_all_mode=_as_mode(conf.get("official_at_all_mode"), "native",
                                 {"native", "text", "drop"}),
            passive_window_seconds=_as_int(
                conf.get("official_passive_window_seconds"), 295, 30, 295),
            max_passive_seq=_as_int(
                conf.get("official_max_passive_seq"), 5, 1, 5),
            passive_pool_size=_as_int(
                conf.get("official_passive_pool_size"), 64, 1, 512),
            max_upload_bytes=_as_int(
                conf.get("official_max_upload_bytes"), 5 * 1024 * 1024,
                1024, 64 * 1024 * 1024),
            member_cache_ttl=_as_int(
                conf.get("official_member_cache_ttl"), 600, 0, 86400),
            member_failed_retry=_as_int(
                conf.get("official_member_failed_retry"), 300, 30, 86400),
            enable_member_enrich=_as_bool(
                conf.get("official_enable_member_enrich"), True),
            request_timeout=_as_int(
                conf.get("official_request_timeout"), 20, 5, 120),
            ws_reconnect_base_delay=_as_float(
                conf.get("official_ws_reconnect_base_delay"), 2.0, 1.0, 60.0),
            ws_reconnect_max_delay=_as_float(
                conf.get("official_ws_reconnect_max_delay"), 60.0, 5.0, 600.0),
            intents=_as_int(
                conf.get("official_intents"), 1 << 25, 1, 1 << 31),
            api_base=str(conf.get("official_api_base") or "").strip(),
            token_url=str(conf.get("official_token_url") or "").strip(),
        )
        return cfg

    def validate(self) -> None:
        if not self.app_id:
            raise OfficialConfigError(
                "official 模式缺少 app_id（official_app_id 或 QQBOT_APP_ID）")
        if not self.app_secret:
            raise OfficialConfigError(
                "official 模式缺少 app_secret（official_app_secret 或 "
                "QQBOT_APP_SECRET）")

    def redacted(self) -> dict:
        """可安全输出的配置快照。"""
        return {
            "app_id": self.app_id,
            "app_secret": mask_secret(self.app_secret),
            "sandbox": self.sandbox,
            "self_id": self.self_id,
            "bot_name": self.bot_name,
            "at_all_mode": self.at_all_mode,
            "allow_active": self.allow_active,
            "base_url": self.base_url,
        }