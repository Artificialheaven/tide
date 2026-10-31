# -*- coding: utf-8 -*-
"""官方 WebSocket 网关客户端（协议自实现）。

生命周期：`start()` 建连并等到 READY 才返回；之后由内部 supervisor
负责断线重连/Resume，对外不暴露断线。只有首次建连失败会抛给 main.py
的外层重连（此时插件尚未开始处理事件，重启无副作用）。
"""
from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable, Optional

import websockets

from .api import OfficialAPI, OfficialAPIError
from .config import OfficialConfig

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11


class GatewayError(Exception):
    pass


class GatewayReconnect(GatewayError):
    """保留会话，尝试 Resume。"""


class GatewayReidentify(GatewayError):
    """会话失效，清空 session 后重新 Identify。"""


class OfficialGateway:
    def __init__(self, config: OfficialConfig, api: OfficialAPI,
                 on_dispatch: Callable[[str, dict], Awaitable[None]],
                 on_ready: Optional[Callable[[dict], Awaitable[None]]] = None,
                 logger: Optional[Callable[[str], None]] = None):
        self.config = config
        self.api = api
        self.on_dispatch = on_dispatch
        self.on_ready = on_ready
        self._log = logger or (lambda msg: print(f"[官方] {msg}"))

        self._ws = None
        self._gateway_url = ""
        self._session_id = ""
        self._last_seq: Optional[int] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._heartbeat_interval = 41.25
        self._supervisor: Optional[asyncio.Task] = None
        self._closed = False
        self._tasks: set[asyncio.Task] = set()

    # ── 对外 ──────────────────────────────────────────────

    async def start(self) -> None:
        self._closed = False
        self._gateway_url = await self._fetch_gateway_url()
        await self._open(resume=False)
        self._supervisor = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        self._closed = True
        supervisor = self._supervisor
        self._supervisor = None
        if supervisor:
            supervisor.cancel()
            try:
                await supervisor
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        await self._cleanup_connection()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

    # ── 建连 ──────────────────────────────────────────────

    async def _fetch_gateway_url(self) -> str:
        body = await self.api.get_gateway()
        url = str(body.get("url") or "").strip()
        if not url:
            raise GatewayError("网关地址为空")
        return url

    async def _open(self, resume: bool) -> None:
        ws = await websockets.connect(
            self._gateway_url, max_size=None, ping_interval=None,
            close_timeout=5, open_timeout=15)
        self._ws = ws
        try:
            hello_raw = await asyncio.wait_for(ws.recv(), timeout=15)
            hello = json.loads(hello_raw)
            if hello.get("op") != OP_HELLO:
                raise GatewayReconnect(
                    f"网关首包不是 Hello（op={hello.get('op')}）")
            interval_ms = (hello.get("d") or {}).get("heartbeat_interval")
            self._set_heartbeat_interval(interval_ms)

            token = await self.api.token()
            if resume and self._session_id:
                await ws.send(json.dumps({
                    "op": OP_RESUME,
                    "d": {"token": f"QQBot {token}",
                          "session_id": self._session_id,
                          "seq": self._last_seq if self._last_seq else 0},
                }))
            else:
                await ws.send(json.dumps({
                    "op": OP_IDENTIFY,
                    "d": {"token": f"QQBot {token}",
                          "intents": self.config.intents,
                          "shard": [0, 1],
                          "properties": {}},
                }))
            self._start_heartbeat()

            loop = asyncio.get_running_loop()
            deadline = loop.time() + 30
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise GatewayReconnect("等待 READY 超时")
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                message = json.loads(raw)
                op = message.get("op")
                if message.get("s") is not None:
                    self._last_seq = message.get("s")
                if op == OP_HELLO:
                    self._set_heartbeat_interval(
                        (message.get("d") or {}).get("heartbeat_interval"))
                    continue
                if op == OP_DISPATCH:
                    t = str(message.get("t") or "")
                    d = message.get("d") or {}
                    if t == "READY":
                        self._session_id = str(d.get("session_id") or "")
                        await self._emit_ready(d)
                        return
                    if t == "RESUMED":
                        return
                    self._spawn_dispatch(t, d)
                    continue
                if op == OP_HEARTBEAT_ACK:
                    continue
                if op == OP_RECONNECT:
                    raise GatewayReconnect("服务端要求重连")
                if op == OP_INVALID_SESSION:
                    self._invalidate_session()
                    raise GatewayReidentify("会话失效，将重新鉴权")
                raise GatewayReconnect(f"未知 op={op}")
        except Exception:
            await self._cleanup_connection()
            raise

    def _set_heartbeat_interval(self, interval_ms) -> None:
        try:
            value = float(interval_ms or 41250) / 1000.0
        except (TypeError, ValueError):
            value = 41.25
        self._heartbeat_interval = max(1.0, value)

    async def _emit_ready(self, d: dict) -> None:
        if not self.on_ready:
            return
        try:
            await self.on_ready(d)
        except Exception as exc:
            self._log(f"READY 处理失败: {exc.__class__.__name__}: {exc}")

    # ── 主循环 ────────────────────────────────────────────

    async def _read_loop(self) -> None:
        ws = self._ws
        if ws is None:
            raise GatewayReconnect("连接已关闭")
        async for raw in ws:
            message = json.loads(raw)
            op = message.get("op")
            if message.get("s") is not None:
                self._last_seq = message.get("s")
            if op == OP_DISPATCH:
                t = str(message.get("t") or "")
                d = message.get("d") or {}
                if t == "READY":
                    self._session_id = str(d.get("session_id") or "")
                    await self._emit_ready(d)
                elif t == "RESUMED":
                    self._log("网关会话已恢复")
                else:
                    self._spawn_dispatch(t, d)
            elif op == OP_HEARTBEAT_ACK:
                continue
            elif op == OP_HELLO:
                self._set_heartbeat_interval(
                    (message.get("d") or {}).get("heartbeat_interval"))
            elif op == OP_RECONNECT:
                raise GatewayReconnect("服务端要求重连")
            elif op == OP_INVALID_SESSION:
                self._invalidate_session()
                raise GatewayReidentify("会话失效，将重新鉴权")

    async def _supervise(self) -> None:
        backoff = max(1.0, float(self.config.ws_reconnect_base_delay))
        max_delay = max(backoff, float(self.config.ws_reconnect_max_delay))
        # start() 已建好首条连接，第一轮直接读它；之后才需要重新建连
        has_connection = True
        while not self._closed:
            try:
                if not has_connection:
                    self._gateway_url = await self._fetch_gateway_url()
                    await self._open(resume=bool(self._session_id))
                    backoff = max(1.0,
                                  float(self.config.ws_reconnect_base_delay))
                has_connection = False
                await self._read_loop()
                self._log("网关连接关闭，准备重连")
            except asyncio.CancelledError:
                raise
            except GatewayReidentify as exc:
                self._log(str(exc))
                await self._cleanup_connection()
                if self._closed:
                    break
                await asyncio.sleep(1)
                continue
            except OfficialAPIError as exc:
                self._log(f"网关鉴权失败: {exc}")
            except GatewayError as exc:
                self._log(f"网关错误: {exc}")
            except Exception as exc:
                self._log(f"网关断开: {exc.__class__.__name__}: {exc}")
            finally:
                await self._cleanup_connection()
            if self._closed:
                break
            await asyncio.sleep(backoff)
            backoff = min(max_delay, max(1.0, backoff * 2))

    def _invalidate_session(self) -> None:
        self._session_id = ""
        self._last_seq = None

    # ── 心跳 ──────────────────────────────────────────────

    def _start_heartbeat(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                ws = self._ws
                if ws is None:
                    return
                payload = {"op": OP_HEARTBEAT,
                           "d": self._last_seq}
                await ws.send(json.dumps(payload))
        except asyncio.CancelledError:
            raise
        except Exception:
            # 发送失败交给读循环/网关状态发现
            return

    # ── 清理与分发 ────────────────────────────────────────

    async def _cleanup_connection(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    def _spawn_dispatch(self, t: str, d: dict) -> None:
        if not t:
            return
        task = asyncio.create_task(self._dispatch(t, d))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _dispatch(self, t: str, d: dict) -> None:
        try:
            await self.on_dispatch(t, d)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log(f"事件处理失败 {t}: {exc.__class__.__name__}: {exc}")