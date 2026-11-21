# -*- coding: utf-8 -*-
"""CQ 码图片工具：read_message_image 回归 + save_message_image 落盘工作区。"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'plugins', 'deepseek'))

from Core import builtin_tools as bt  # noqa: E402
from Core import media_in  # noqa: E402
from Core.builtin_tools import ToolContext, ToolMediaResult  # noqa: E402
from Core.sandbox import Sandbox  # noqa: E402

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeBot:
    def __init__(self, url='https://img.example/a.png'):
        self.url = url
        self.calls = []

    async def call_api(self, action, params):
        self.calls.append((action, params))
        return {'retcode': 0, 'data': {'url': self.url}}


def _ctx(tmp_path, bot=None, cfg=None):
    sb = Sandbox(str(tmp_path), '123')
    return ToolContext(bot=bot or _FakeBot(), group_id='123',
                       cfg=cfg or {'media_download_max_kb': 1024},
                       sandbox=sb)


def _patch_download(monkeypatch, raw=PNG):
    async def fake_get(url, budget):
        return raw

    monkeypatch.setattr(media_in, '_http_get', fake_get)


def test_tool_registered():
    assert bt.TOOL_KEYS['save_message_image'] == 'message.save_image'
    names = {s['function']['name'] for s in bt.schemas()}
    assert 'save_message_image' in names
    assert 'read_message_image' in names


def test_save_default_path_under_mcp(tmp_path, monkeypatch):
    _patch_download(monkeypatch)
    ctx = _ctx(tmp_path)
    out = run(bt.dispatch('save_message_image', {'ref': 'abc123.jpg'}, ctx))
    assert out.startswith('图片已保存到工作区 mcp/msg_image_')
    assert '临时中转' in out
    saved = list((tmp_path / '123' / 'mcp').glob('msg_image_*.png'))
    assert len(saved) == 1
    assert saved[0].read_bytes() == PNG


def test_save_bare_name_goes_to_mcp_and_gets_ext(tmp_path, monkeypatch):
    _patch_download(monkeypatch)
    ctx = _ctx(tmp_path)
    out = run(bt.dispatch('save_message_image',
                          {'ref': 'abc', 'name': 'cat'}, ctx))
    assert 'mcp/cat.png' in out
    assert (tmp_path / '123' / 'mcp' / 'cat.png').read_bytes() == PNG


def test_save_explicit_workspace_path(tmp_path, monkeypatch):
    _patch_download(monkeypatch)
    ctx = _ctx(tmp_path)
    out = run(bt.dispatch('save_message_image',
                          {'ref': 'abc', 'name': 'images/a.png'}, ctx))
    assert 'images/a.png' in out
    assert '临时中转' not in out
    assert (tmp_path / '123' / 'images' / 'a.png').read_bytes() == PNG
    assert 'read_image' in out and 'qq_send_image' in out


def test_save_dedupes_existing_name(tmp_path, monkeypatch):
    _patch_download(monkeypatch)
    ctx = _ctx(tmp_path)
    run(bt.dispatch('save_message_image',
                    {'ref': 'a', 'name': 'x.png'}, ctx))
    out = run(bt.dispatch('save_message_image',
                          {'ref': 'b', 'name': 'x.png'}, ctx))
    assert 'mcp/x_2.png' in out
    assert (tmp_path / '123' / 'mcp' / 'x.png').exists()
    assert (tmp_path / '123' / 'mcp' / 'x_2.png').exists()


def test_save_rejects_bad_ref(tmp_path, monkeypatch):
    _patch_download(monkeypatch)
    ctx = _ctx(tmp_path)
    out = run(bt.dispatch('save_message_image',
                          {'ref': 'https://evil/a.png'}, ctx))
    assert out.startswith('错误: ref 必须是')
    assert not (tmp_path / '123' / 'mcp').exists()


def test_save_rejects_traversal_and_public(tmp_path, monkeypatch):
    _patch_download(monkeypatch)
    ctx = _ctx(tmp_path)
    out = run(bt.dispatch('save_message_image',
                          {'ref': 'abc', 'name': '../evil.png'}, ctx))
    assert '相对路径' in out
    out = run(bt.dispatch('save_message_image',
                          {'ref': 'abc', 'name': 'public/a.png'}, ctx))
    assert '公共素材库' in out
    assert not (tmp_path / 'public' / 'a.png').exists()


def test_save_requires_online_bot(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.bot = None
    out = run(bt.dispatch('save_message_image', {'ref': 'abc'}, ctx))
    assert '机器人不在线' in out


def test_save_rejects_unknown_image_format(tmp_path, monkeypatch):
    _patch_download(monkeypatch, raw=b'not an image')
    ctx = _ctx(tmp_path)
    out = run(bt.dispatch('save_message_image', {'ref': 'abc'}, ctx))
    assert '不是受支持的图片格式' in out


def test_read_message_image_still_works(tmp_path, monkeypatch):
    _patch_download(monkeypatch)
    ctx = _ctx(tmp_path)
    res = run(bt.dispatch('read_message_image', {'ref': 'abc'}, ctx))
    assert isinstance(res, ToolMediaResult)
    assert res.images and res.images[0][0] == 'image/png'
    assert 'abc' in ctx.image_seen
    again = run(bt.dispatch('read_message_image', {'ref': 'abc'}, ctx))
    assert '已经给过你' in again


def test_save_image_file_quota_error_is_reported(tmp_path, monkeypatch):
    _patch_download(monkeypatch)
    ctx = _ctx(tmp_path)
    ctx.sandbox.max_file_bytes = 8   # 非 mcp/ 路径走普通配额通道
    out = run(bt.dispatch('save_message_image',
                          {'ref': 'abc', 'name': 'images/big.png'}, ctx))
    assert out.startswith('错误: 图片保存失败')
