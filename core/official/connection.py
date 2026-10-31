# -*- coding: utf-8 -*-
"""对 main.py 暴露的连接对象：`async with` + `send` / `recv`。

内部把官方网关包成 OneBot WebSocket 的形状。首次建连失败会向上抛，
由 main.py 现有的外层重连处理；连上之后网关断线由 gateway 自己 Resume，
不会打断 main 的 `recv()` 循环。
"""
from __future__ import annotations

from .api import OfficialAPI, OfficialAPIError
from .config import OfficialConfig
from .gateway import OfficialGateway
from .session import OfficialSession


class OfficialConnection:
    def __init__(self, config: OfficialConfig):
        self.config = config
        self._api: OfficialAPI | None = None
        self._session: OfficialSession | None = None
        self._gateway: OfficialGateway | None = None

    @staticmethod
    def _log(message: str) -> None:
        print(f"[官方] {message}")

    async def __aenter__(self) -> "OfficialConnection":
        self.config.validate()
        self._log(f"连接 {self.config.base_url}"
                  f"（sandbox={self.config.sandbox}）")
        self._api = OfficialAPI(self.config)
        # 先换 token：凭据/网络问题在进入主循环前暴露，交给外层重连
        await self._api.token()
        self._session = OfficialSession(self.config, self._api,
                                        logger=self._log)
        self._gateway = OfficialGateway(
            self.config, self._api,
            on_dispatch=self._session.handle_dispatch,
            on_ready=self._session.handle_ready,
            logger=self._log)
        try:
            await self._gateway.start()
        except Exception:
            await self._session.close()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        gateway, session = self._gateway, self._session
        self._gateway = None
        self._session = None
        if gateway is not None:
            try:
                await gateway.stop()
            except Exception:
                pass
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass

    async def send(self, raw: str) -> None:
        if self._session is None:
            raise ConnectionError("官方连接已关闭")
        self._session.submit_action(raw)

    async def recv(self) -> str:
        if self._session is None:
            raise ConnectionError("官方连接已关闭")
        return await self._session.recv()