"""MCP 长驻会话管理（stdio / streamable-http / sse）。

设计要点：
- 懒启动：某群工具循环第一次需要某 server 时才拉起，多群共享一个 handle
- 手动 AsyncExitStack 持有长连接，initialize 失败也必须 aclose 防孤儿
- call 传输异常 → 关旧连接 → 重连重试一次
- on_stop 8s 总预算并发关闭
mcp 全程延迟导入（lib 未装时插件其余功能仍可用）。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import os
import re
import time
from datetime import timedelta

from . import imgmark, storage
from .mcp_winboot import enable_pywin32

_MEDIA_EXT = {
    'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif',
    'image/webp': 'webp', 'image/bmp': 'bmp',
    'audio/wav': 'wav', 'audio/x-wav': 'wav', 'audio/mpeg': 'mp3',
    'audio/ogg': 'ogg', 'audio/amr': 'amr', 'audio/silk': 'silk',
}


def _safe_name(text: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_-]+', '_', text or '').strip('_')[:40] or 'media'


def tool_fullname(server_id: str, tool_name: str) -> str:
    """MCP 工具在 OpenAI tools 里的命名空间名（与 engine 解析保持一致）。"""
    return f'mcp__{server_id}__{tool_name}'


class McpHandle:
    def __init__(self, server_id: str):
        self.server_id = server_id
        self.stack = None
        self.session = None
        self.status = 'stopped'       # stopped/starting/running/failed
        self.last_error = ''
        self.lock = asyncio.Lock()


class McpManager:
    def __init__(self, plugin_dir: str, db_path: str, log=None):
        self.plugin_dir = plugin_dir
        self.db_path = db_path
        self._log = log or (lambda msg: None)
        self._handles: dict[str, McpHandle] = {}
        self._lib_booted = False

    def log(self, msg: str):
        self._log(msg)

    # ── 配置视图 ────────────────────────────────────────────────

    def enabled_servers(self) -> list[dict]:
        try:
            return storage.list_mcp_servers(self.db_path, only_enabled=True)
        except Exception:
            return []

    def cached_tool_specs(self, server_id: str) -> list[dict]:
        """从 SQLite 缓存取 OpenAI 格式 schema（停机时后台仍可展示/勾选）。"""
        row = storage.get_mcp_server(self.db_path, server_id)
        return row['tools'] if row else []

    # ── 连接生命周期 ────────────────────────────────────────────

    def _boot_lib(self):
        if self._lib_booted:
            return
        enable_pywin32(os.path.join(self.plugin_dir, 'lib'))
        self._lib_booted = True

    async def _connect(self, h: McpHandle, row: dict):
        self._boot_lib()
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamablehttp_client
        from mcp.client.sse import sse_client

        cfg = row['config']
        kind = row['kind']
        h.status, h.last_error = 'starting', ''
        stack = contextlib.AsyncExitStack()
        try:
            if kind == 'stdio':
                params = StdioServerParameters(
                    command=cfg['command'],
                    args=list(cfg.get('args', [])),
                    env={**os.environ, **(cfg.get('env') or {})} if cfg.get('env') else None,
                    cwd=cfg.get('cwd') or None,
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            elif kind == 'http':
                read, write, _ = await stack.enter_async_context(
                    streamablehttp_client(
                        cfg['url'],
                        headers=cfg.get('headers') or None,
                        timeout=timedelta(seconds=float(cfg.get('timeout', 30))),
                    ))
            elif kind == 'sse':
                read, write = await stack.enter_async_context(
                    sse_client(cfg['url'], headers=cfg.get('headers') or None))
            else:
                raise ValueError(f'未知 MCP 类型: {kind}')

            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            h.stack, h.session = stack, session
            h.status = 'running'
            storage.set_mcp_status(self.db_path, h.server_id, 'running')
        except Exception as e:
            with contextlib.suppress(Exception):
                await stack.aclose()
            h.status, h.last_error = 'failed', str(e)
            storage.set_mcp_status(self.db_path, h.server_id, 'failed', str(e))
            raise

    async def ensure(self, server_id: str) -> McpHandle:
        h = self._handles.get(server_id)
        if h is None:
            h = McpHandle(server_id)
            self._handles[server_id] = h
        if h.status == 'running' and h.session is not None:
            return h
        async with h.lock:
            if h.status == 'running' and h.session is not None:
                return h
            row = storage.get_mcp_server(self.db_path, server_id)
            if row is None or not row['enabled']:
                raise RuntimeError(f'MCP server {server_id} 不存在或未启用')
            await self._connect(h, row)
        return h

    async def _close(self, h: McpHandle):
        if h.stack is not None:
            with contextlib.suppress(Exception):
                await h.stack.aclose()
        h.stack, h.session, h.status = None, None, 'stopped'

    async def restart(self, server_id: str):
        h = self._handles.get(server_id)
        if h:
            await self._close(h)
        await self.ensure(server_id)
        await self.refresh_tools(server_id)

    async def stop(self, server_id: str):
        h = self._handles.get(server_id)
        if h:
            await self._close(h)
        storage.set_mcp_status(self.db_path, server_id, 'stopped')

    # ── 工具 ────────────────────────────────────────────────────

    def _convert_tool(self, server_id: str, tool) -> dict:
        schema = getattr(tool, 'inputSchema', None) or {'type': 'object', 'properties': {}}
        desc = (getattr(tool, 'description', '') or '')[:512]
        return {
            'type': 'function',
            'function': {
                'name': tool_fullname(server_id, tool.name),
                'description': f'[MCP:{server_id}] {desc}',
                'parameters': schema,
            },
        }

    async def refresh_tools(self, server_id: str) -> list[dict]:
        h = await self.ensure(server_id)
        result = await h.session.list_tools()
        specs = [self._convert_tool(server_id, t) for t in result.tools]
        storage.set_mcp_tools_cache(self.db_path, server_id, specs)
        return specs

    async def call_tool(self, server_id: str, tool_name: str,
                        args: dict, cfg: dict | None = None,
                        sandbox=None, ttl_hours: float = 6) -> str:
        """调用 MCP 工具；传输层异常时重连重试一次。

        sandbox：群 Sandbox。MCP 返回的图片/语音块会落进该群工作区
        （mcp/ 子目录），模型可拿返回的相对路径调 qq_send_image/voice 发群。
        """
        timeout = float((cfg or {}).get('mcp_timeout', 30))
        try:
            return await self._call_once(server_id, tool_name, args, timeout,
                                         sandbox, ttl_hours)
        except (ConnectionError, RuntimeError, EOFError, asyncio.TimeoutError) as e:
            self.log(f'MCP {server_id} 连接异常，重连重试: {e}')
            h = self._handles.get(server_id)
            if h:
                await self._close(h)
            return await self._call_once(server_id, tool_name, args, timeout,
                                         sandbox, ttl_hours)

    async def call_short_tool(self, short_name: str, args: dict,
                              cfg: dict | None = None, sandbox=None,
                              ttl_hours: float = 6) -> str:
        """跨已启用 server 找短名匹配（忽略 mcp__sid__ 前缀与大小写）的工具。

        click_at 用它找各远程桌面 server 的 Click：插件不绑死 winuse 一个 id。
        """
        target = short_name.lower()
        for s in self.enabled_servers():
            for spec in self.cached_tool_specs(s['id']):
                full = spec['function']['name']
                parts = full.split('__', 2)
                if len(parts) == 3 and parts[2].lower() == target:
                    return await self.call_tool(
                        s['id'], parts[2], args, cfg,
                        sandbox=sandbox, ttl_hours=ttl_hours)
        raise RuntimeError(f'没有已启用的 MCP 服务器提供 {short_name} 工具')

    async def _call_once(self, server_id, tool_name, args, timeout,
                         sandbox=None, ttl_hours: float = 6) -> str:
        h = await self.ensure(server_id)
        result = await asyncio.wait_for(
            h.session.call_tool(
                tool_name, args or {},
                read_timeout_seconds=timedelta(seconds=timeout)),
            timeout=timeout + 5)
        texts, media_hints = [], []
        for block in result.content:
            kind = getattr(block, 'type', '') or ''
            if kind == 'text':
                texts.append(getattr(block, 'text', '') or '')
            elif kind in ('image', 'audio'):
                hint = await asyncio.to_thread(
                    self._save_media, block, kind, sandbox, tool_name,
                    ttl_hours, args)
                if hint:
                    media_hints.append(hint)
            elif kind == 'resource':
                # EmbeddedResource：只接二进制 blob（截图类服务可能这么回）
                res = getattr(block, 'resource', None)
                blob = getattr(res, 'blob', None)
                mime = ((getattr(res, 'mimeType', '')
                         or getattr(res, 'mime_type', '')) if res else '')
                if blob is not None and (mime.startswith('image/')
                                         or mime.startswith('audio/')):
                    kind2 = 'audio' if mime.startswith('audio/') else 'image'
                    fake = type('B', (), {'data': blob, 'mimeType': mime})()
                    hint = await asyncio.to_thread(
                        self._save_media, fake, kind2, sandbox, tool_name,
                        ttl_hours, args)
                    if hint:
                        media_hints.append(hint)
                else:
                    texts.append(f'[{type(block).__name__}]')
            else:
                texts.append(f'[{type(block).__name__}]')
        body = '\n'.join(t for t in (texts + media_hints) if t) or '(无返回内容)'
        if getattr(result, 'isError', False):
            body = f'工具返回错误: {body}'
        return body[:2000]

    def _save_media(self, block, kind: str, sandbox, tool_name: str,
                    ttl_hours: float = 6, args: dict | None = None) -> str:
        """把 MCP 图片/语音块 base64 解码落进群工作区，返回给模型的操作提示。

        截图类图片（Snapshot/AnnotatedSnapshot）额外写坐标 sidecar，
        click_at 靠它把「图像内坐标」映射成 Windows 桌面坐标。
        """
        if sandbox is None:
            return f'[{kind} 未保存：群工作区不可用]'
        # 落盘前顺手清掉本群过期截图/录音（TTL，0=永久保留）
        with contextlib.suppress(Exception):
            sandbox.purge_mcp_media(float(ttl_hours) * 3600)
        b64 = getattr(block, 'data', '') or ''
        mime = (getattr(block, 'mimeType', '')
                or getattr(block, 'mime_type', '') or '').lower()
        ext = _MEDIA_EXT.get(mime, 'png' if kind == 'image' else 'amr')
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return f'[{kind} 保存失败：base64 解码错误]'
        stamp = time.strftime('%Y%m%d_%H%M%S')
        base = f'mcp/{_safe_name(tool_name)}_{stamp}'
        rel = f'{base}.{ext}'
        n = 1
        try:
            real, _ = sandbox.resolve(rel)
            while real.exists():
                n += 1
                rel = f'{base}_{n}.{ext}'
                real, _ = sandbox.resolve(rel)
            # 独立大文件上限 + 不占群配额（全屏 PNG 截图常达数 MiB）
            sandbox.write_mcp_media(rel, raw)
        except Exception as e:
            return f'[{kind} 保存失败：{e}]'
        if kind == 'audio':
            return (f'[语音已保存到工作区 {rel}（{len(raw)} 字节）。'
                    f'你听不到它；如需发到群里，调用 qq_send_voice 且 '
                    f'source 填 "{rel}"]')

        # 截图类：探测尺寸 + 写坐标 sidecar（失败不影响图片本身）
        clickable = False
        try:
            size = imgmark.image_size(raw)
            if size:
                meta = imgmark.build_meta(
                    image_w=size[0], image_h=size[1],
                    tool_name=tool_name, args=args)
                if meta is not None:
                    sandbox.write_bytes(
                        imgmark.sidecar_rel(rel),
                        (imgmark.dump_meta(meta) + '\n').encode('utf-8'))
                    clickable = True
        except Exception:
            clickable = False

        if clickable:
            return (
                f'[图片已保存到工作区 {rel}（{len(raw)} 字节），可点击定位。'
                f'你现在还看不到画面：先调用 read_image（path 填 "{rel}"，'
                f'默认带每 100px 坐标网格；小按钮可以 crop 裁剪后 zoom 放大再看），'
                f'看清目标的网格坐标后，调用内置工具 click_at（path 填 "{rel}"，'
                f'x/y 填网格标出的原图坐标）由系统换算到桌面坐标点击；'
                f'不要凭目测坐标直接调 MCP Click。'
                f'确认要发到群里才调 qq_send_image（source 填 "{rel}"）]')
        return (f'[图片已保存到工作区 {rel}（{len(raw)} 字节）。'
                f'你现在还看不到画面：要知道图里有什么就调用 read_image 且 '
                f'path 填 "{rel}"；确认要发到群里就调用 qq_send_image 且 '
                f'source 填 "{rel}"]')

    # ── Web「测试连接」：短栈 ───────────────────────────────────

    async def test_server(self, server_id: str) -> dict:
        row = storage.get_mcp_server(self.db_path, server_id)
        if row is None:
            return {'ok': False, 'error': 'server 不存在'}
        h = McpHandle(f'test-{server_id}')
        try:
            await asyncio.wait_for(self._connect(h, row), timeout=20)
            result = await asyncio.wait_for(h.session.list_tools(), timeout=10)
            names = [t.name for t in result.tools]
            return {'ok': True, 'tools': names}
        except Exception as e:
            return {'ok': False, 'error': str(e)}
        finally:
            await self._close(h)

    # ── 关闭 ────────────────────────────────────────────────────

    async def shutdown(self, total_budget: float = 8.0):
        handles = list(self._handles.values())
        if not handles:
            return

        async def _one(h: McpHandle):
            try:
                await asyncio.wait_for(self._close(h), timeout=5.0)
            except Exception:
                pass

        await asyncio.wait_for(
            asyncio.gather(*(_one(h) for h in handles), return_exceptions=True),
            timeout=total_budget)
        self._handles.clear()
