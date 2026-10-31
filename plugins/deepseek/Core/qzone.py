# -*- coding: utf-8 -*-
"""QQ 空间说说：经 NapCat OneBot HTTP「扩展接口」发表 / 删除。

NapCat 4.18.x 提供（文档 /api 版本页）：
  POST {base}/send_qzone_msg
       {"content": str, "images"?: str[], "ugc_right"?: 1|4|16|64|128,
        "target_uins"?: str[]}  ->  {"data": {"tid": "..."}}
  POST {base}/delete_qzone_msg {"tid": str}
鉴权与普通 OneBot HTTP action 一致：Authorization: Bearer <access_token>。

地址/token 解析顺序：插件配置 napcat_http_url / napcat_http_token →
框架根 config.json 的 http_address / http_token。
该 HTTP 通道与机器人 WebSocket 长连接相互独立，全部为 async httpx
调用（发表是低频主人操作，不进事件循环线程池）。
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

import httpx

from . import storage

# 查看权限码（NapCat 文档原文）
UGC_RIGHTS = {
    1: '所有人可见',
    4: '好友可见',
    16: '部分好友可见',
    64: '仅自己可见',
    128: '部分好友不可见',
}
MAX_CONTENT = 5000
_TIMEOUT = 20.0
_ROOT_CFG = Path(__file__).resolve().parents[3] / 'config.json'
_root_cache = {'mtime': -1.0, 'data': {}}


def _root_config() -> dict:
    """读框架根 config.json（按 mtime 缓存），找不到时返回 {}。"""
    try:
        st = _ROOT_CFG.stat()
        if st.st_mtime != _root_cache['mtime']:
            _root_cache['data'] = json.loads(
                _ROOT_CFG.read_text(encoding='utf-8'))
            _root_cache['mtime'] = st.st_mtime
    except (OSError, ValueError):
        return {}
    return _root_cache['data']


def resolve_endpoint(plugin_cfg: dict) -> tuple:
    """返回 (base_url, token)；插件配置优先，回落根 config。"""
    root = _root_config()
    url = str(plugin_cfg.get('napcat_http_url')
              or root.get('http_address') or '').strip().rstrip('/')
    token = str(plugin_cfg.get('napcat_http_token')
                or root.get('http_token') or '').strip()
    return url, token


class QzoneClient:
    def __init__(self, plugin):
        self.p = plugin

    def _endpoint(self) -> tuple:
        return resolve_endpoint(getattr(self.p, 'cfg', {}) or {})

    async def _post(self, action: str, payload: dict):
        """单次 OneBot HTTP action。返回 (data_dict, 错误文案)。"""
        url, token = self._endpoint()
        if not url:
            return None, ('未配置 NapCat HTTP 地址（根 config.json 的 '
                          'http_address，或插件配置 napcat_http_url）')
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as cli:
                resp = await cli.post(
                    f'{url}/{action}',
                    content=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                    headers=headers)
        except httpx.HTTPError as e:
            return None, f'无法连接 NapCat HTTP 服务：{e}'
        try:
            data = resp.json()
        except ValueError:
            return None, f'NapCat 返回非 JSON（HTTP {resp.status_code}）'
        if not isinstance(data, dict) or data.get('retcode') != 0 \
                or data.get('status') not in ('ok', 'async'):
            why = (data.get('wording') or data.get('msg')
                   or data.get('message') or str(data)) if isinstance(data, dict) \
                else str(data)
            return None, f'NapCat 拒绝请求：{why}'
        d = data.get('data')
        return d if isinstance(d, dict) else {}, ''

    async def publish(self, content: str, ugc_right: int = 1,
                      target_uins=None, sender_id: str = '',
                      sender_name: str = '') -> str:
        content = str(content or '').strip()
        if not content:
            return '错误: 说说正文不能为空'
        if len(content) > MAX_CONTENT:
            return f'错误: 说说正文最长 {MAX_CONTENT} 字（当前 {len(content)}）'
        try:
            ugc_right = int(ugc_right)
        except (TypeError, ValueError):
            return '错误: ugc_right 必须是数字权限码'
        if ugc_right not in UGC_RIGHTS:
            return ('错误: ugc_right 仅支持 1 所有人 / 4 好友 / '
                    '16 部分好友可见 / 64 仅自己 / 128 部分好友不可见')
        uins = []
        if ugc_right in (16, 128):
            uins = [str(u).strip() for u in (target_uins or [])
                    if str(u).strip().isdigit()]
            if not uins:
                return f'错误: 权限码 {ugc_right} 必须在 target_uins 提供 QQ 号数组'
        payload = {'content': content}
        if ugc_right != 1:
            payload['ugc_right'] = ugc_right
            payload['target_uins'] = uins
        data, err = await self._post('send_qzone_msg', payload)
        if err:
            return '错误: ' + err
        tid = str((data or {}).get('tid') or '').strip()
        if not tid:
            return '错误: 发送成功但响应里没有 tid'
        storage.add_qzone_post(
            self.p.db_path, tid, content, ugc_right,
            creator_id=str(sender_id or ''),
            creator_name=str(sender_name or ''))
        return (f'说说已发布（{UGC_RIGHTS[ugc_right]}），tid={tid}。\n'
                '删除请用 qzone_delete 并提供这个 tid；最近发布的也能用 '
                'qzone_recent 查到。')

    async def delete(self, tid: str) -> str:
        tid = str(tid or '').strip()
        if not tid:
            return '错误: tid 必填（来自发布结果或 qzone_recent）'
        _, err = await self._post('delete_qzone_msg', {'tid': tid})
        if err:
            return '错误: ' + err
        return f'说说 {tid} 已删除'

    def recent(self, limit: int = 10) -> str:
        """本地发布留痕（NapCat 此版本无空间列表接口）。"""
        limit = max(1, min(int(limit or 10), 30))
        rows = storage.list_qzone_posts(self.p.db_path, limit)
        if not rows:
            return '本地还没有发布过说说（tid 只在发布成功后登记）'
        lines = ['最近发布的说说（仅本机器人发布留痕）：']
        for r in rows:
            t = datetime.datetime.fromtimestamp(
                float(r['created_at'])).strftime('%m-%d %H:%M')
            scope = UGC_RIGHTS.get(int(r['ugc_right']), str(r['ugc_right']))
            preview = (r['content'] or '').replace('\n', ' ')[:50]
            lines.append(
                f'- [{t}] tid={r["tid"]}（{scope}）{preview}')
        return '\n'.join(lines)
