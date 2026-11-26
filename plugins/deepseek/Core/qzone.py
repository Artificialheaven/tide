# -*- coding: utf-8 -*-
"""QQ 空间：优先走机器人已有的 OneBot WebSocket（call_api），HTTP 兜底。

## 为什么改成 WS 优先

NapCat 的 QQ 空间能力是**私有扩展**（28 个 action），既没写进手写文档，也
没有任何空间事件上报。这些 action 挂在同一个 OneBot router 上，所以既能用
NapCat 的 HTTP 服务端调，也能用现有 WebSocket 长连接以普通 action 调。

旧实现只走 HTTP：要求用户额外启用一个**默认关闭**的 HTTP 服务端（默认
3000 端口）并填地址/token。而机器人本体用的是 WS 服务端（默认 3001），
绝大多数部署根本没开 HTTP —— 于是三个空间工具全部报"未配置地址"。

现在默认走 WS，HTTP 仅作兜底。是否可用由 `refresh()` 探测：

1. `transport=official`（QQ 开放平台）→ 直接判定不可用；
2. WS：调 `get_qzone_msg_list` 探测，成功即用 WS；
3. HTTP：配置了地址时同样探测，成功即用 HTTP；
4. 都不行 → 记下原因（工具不再暴露，提示词里也不出现）。

探测结果按 `qzone_probe_ttl` 秒缓存；调用失败时缓存作废，下次自动重探。

## 参数契约（来自 NapCat 源码 action schema，非文档）

- send_qzone_msg：content / images（file:// http(s):// base64://）/ ugc_right
  （1 所有人 4 好友 16 部分好友可见 64 仅自己 128 部分好友不可见）/
  target_uins（16、128 必填）→ {tid, time}
- get_qzone_msg_list：format(snowluma|native) / target_uin / pos / num /
  cursor → snowluma {total, msglist:[{tid, content, time, comment_num,
  is_private, images}]}
- get_qzone_msg_detail：tid / feed_ref → {tid, content, comment_num, comments:
  [{uin, name, time, content, comment_id, replies:[...]}]}
- get_qzone_feeds：page_num / count / cursor → {feeds:[{uin, nickname, time,
  tid, content, images, like_count, comment_count}], has_more, next_cursor}
- comment_qzone：tid + content，带 comment_id + comment_uin 即回复某条评论，
  再带 reply_uin + reply_name 指定接收者；楼中楼只能纯文字
- like_qzone / unlike_qzone：tid
"""
from __future__ import annotations

import datetime
import json
import re
import time

try:  # openai SDK 自带 httpx；缺它时只失去 HTTP 兜底与图片下载，WS 通道照常
    import httpx
except ModuleNotFoundError:  # pragma: no cover - 正常安装不会走到
    httpx = None

from . import runtime_env, storage

# 查看权限码（NapCat 源码 UGC_RIGHTS）
UGC_RIGHTS = {
    1: '所有人可见',
    4: '好友可见',
    16: '部分好友可见',
    64: '仅自己可见',
    128: '部分好友不可见',
}
MAX_CONTENT = 5000
MAX_IMAGES = 9
MAX_READ_IMAGES = 4
_TIMEOUT = 20.0
PROBE_TTL = 600.0
_ROOT_CFG = runtime_env.ROOT_CONFIG


# ── 纯函数（可单测）────────────────────────────────────────────

def resolve_endpoint(plugin_cfg: dict) -> tuple:
    """返回 HTTP 兜底通道的 (base_url, token)；插件配置优先，回落根 config。"""
    root = runtime_env.root_config()
    url = str((plugin_cfg or {}).get('napcat_http_url')
              or root.get('http_address') or '').strip().rstrip('/')
    token = str((plugin_cfg or {}).get('napcat_http_token')
                or root.get('http_token') or '').strip()
    return url, token


def fmt_time(ts) -> str:
    try:
        ts = float(ts or 0)
    except (TypeError, ValueError):
        ts = 0.0
    if ts <= 0:
        return '时间未知'
    if ts > 1e11:  # 毫秒时间戳
        ts /= 1000.0
    return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')


def clip(text, limit: int = 200) -> str:
    s = str(text or '').replace('\r', ' ').replace('\n', ' ⏎ ').strip()
    return s if len(s) <= limit else s[:limit] + '…'


def ugc_text(code) -> str:
    try:
        return UGC_RIGHTS.get(int(code), f'权限码 {code}')
    except (TypeError, ValueError):
        return f'权限码 {code}'


def mentions_me(text: str, self_uin, self_name: str) -> bool:
    """说说/评论文本里是否 @ 了自己（昵称或 QQ 号）。"""
    text = str(text or '')
    if not text:
        return False
    name = str(self_name or '').strip()
    uin = str(self_uin or '').strip()
    if name:
        # @昵称 允许中间有空格/零宽字符；昵称内部空格不敏感
        parts = [re.escape(p) for p in name.split()] or [re.escape(name)]
        if re.search(r'@\s*' + r'\s*'.join(parts), text):
            return True
    if uin and re.search(r'@\s*' + re.escape(uin) + r'(?!\d)', text):
        return True
    return False


def find_mentions(feeds, self_uin, self_name: str) -> list:
    """好友动态 → @ 了自己的说说列表（去重、无 tid 的跳过）。"""
    out, seen = [], set()
    for f in feeds or []:
        if not isinstance(f, dict):
            continue
        content = str(f.get('content') or '')
        if str(self_uin or '') and str(f.get('uin') or '') == str(self_uin):
            continue                      # 自己发的说说不用「有人 @ 你」
        if not mentions_me(content, self_uin, self_name):
            continue
        tid = str(f.get('tid') or '').strip()
        key = tid or f'{f.get("uin")}:{content[:80]}'
        if not tid or key in seen:
            continue
        seen.add(key)
        out.append({
            'tid': tid,
            'uin': str(f.get('uin') or ''),
            'nickname': str(f.get('nickname') or f.get('name') or ''),
            'time': f.get('time') or 0,
            'content': content,
            'images': [str(u) for u in (f.get('images') or []) if u],
            'comment_count': (f.get('comment_count')
                              if f.get('comment_count') is not None
                              else f.get('comment_num')),
        })
    return out


def new_comments(detail: dict, since_ts: float, self_uin) -> list:
    """说说详情 → since_ts 之后的新评论/回复（排除自己发的）。"""
    out = []
    me = str(self_uin or '')
    for c in (detail.get('comments') or []):
        if not isinstance(c, dict):
            continue
        uin = str(c.get('uin') or '')
        if uin and uin == me:
            continue
        try:
            ct = float(c.get('time') or 0)
        except (TypeError, ValueError):
            ct = 0.0
        # 父评论可能是旧的（since 之前），但它下面的楼中楼可能刚有人回：
        # 不能因为父评论旧就整条跳过，否则"评论才有人回复"会漏掉
        if not ct or ct > float(since_ts or 0):
            out.append({
                'comment_id': str(c.get('comment_id') or ''),
                'comment_uin': uin,
                'name': str(c.get('name') or ''),
                'content': str(c.get('content') or ''),
                'time': ct,
                'reply_to': '',
            })
        for r in (c.get('replies') or []):
            if not isinstance(r, dict):
                continue
            ruin = str(r.get('uin') or '')
            if ruin and ruin == me:
                continue
            try:
                rt = float(r.get('time') or 0)
            except (TypeError, ValueError):
                rt = 0.0
            if rt and rt <= float(since_ts or 0):
                continue
            out.append({
                'comment_id': str(r.get('comment_id') or ''),
                'comment_uin': ruin,
                'name': str(r.get('name') or ''),
                'content': str(r.get('content') or ''),
                'time': rt,
                'reply_to': str(r.get('name') or '') or uin,
                'parent_comment_id': str(r.get('parent_comment_id') or ''),
                'parent_comment_uin': str(r.get('parent_comment_uin') or ''),
            })
    return out


def _unwrap_response(resp, action: str):
    """OneBot 响应信封 → (data_dict, 错误文案)。"""
    if not isinstance(resp, dict):
        return None, f'{action} 返回格式异常（{type(resp).__name__}）'
    if 'retcode' not in resp and 'data' not in resp:
        return None, f'{action} 返回缺少 data 字段'
    rc = resp.get('retcode')
    status = str(resp.get('status') or '')
    if rc not in (0, None) or status in ('failed', 'error'):
        why = (resp.get('wording') or resp.get('msg')
               or resp.get('message') or str(resp.get('data'))[:200])
        return None, f'NapCat 拒绝 {action}：{why}'
    data = resp.get('data')
    return (data if isinstance(data, dict) else {}), ''


class QzoneCapability:
    """一次通道探测结果；available=None 表示还没探测过（乐观放行）。"""

    __slots__ = ('available', 'channel', 'why', 'version', 'at')

    def __init__(self, available=None, channel: str = '', why: str = '',
                 version: str = '', at: float = 0.0):
        self.available = available
        self.channel = channel
        self.why = why
        self.version = version
        self.at = at

    def as_dict(self) -> dict:
        return {'available': self.available, 'channel': self.channel,
                'why': self.why, 'version': self.version, 'at': self.at}

    def __repr__(self):
        return (f'<QzoneCapability available={self.available} '
                f'channel={self.channel!r} why={self.why!r}>')


class QzoneClient:
    def __init__(self, plugin):
        self.p = plugin
        self.cap = QzoneCapability()

    # ── 基础 ──────────────────────────────────────────────────

    @property
    def cfg(self) -> dict:
        return getattr(self.p, 'cfg', {}) or {}

    @property
    def bot(self):
        return getattr(self.p, 'bot', None)

    def available(self) -> bool:
        """同步查询（工具/提示词过滤用）；未探测/官方通道之外默认乐观 True。"""
        if runtime_env.is_official():
            return False
        if self.cap.available is None:
            return True
        return bool(self.cap.available)

    def status_text(self) -> str:
        cap = self.cap
        if not self.available():
            return 'QQ 空间不可用：' + (cap.why or runtime_env.unavailable_reason('QQ 空间'))
        ch = {'ws': 'WebSocket 动作通道', 'http': 'NapCat HTTP 服务端'}.get(
            cap.channel, cap.channel or '未探测')
        return f'QQ 空间可用（{ch}{"，NapCat " + cap.version if cap.version else ""}）'

    # ── 通道探测 ──────────────────────────────────────────────

    async def refresh(self, force: bool = False) -> QzoneCapability:
        """探测/刷新可用性，返回当前能力对象。"""
        if runtime_env.is_official():
            self.cap = QzoneCapability(
                False, '', runtime_env.unavailable_reason('QQ 空间'),
                '', time.time())
            return self.cap
        ttl = float(self.cfg.get('qzone_probe_ttl', PROBE_TTL) or PROBE_TTL)
        if (not force and self.cap.available is not None
                and time.time() - float(self.cap.at or 0) < ttl):
            return self.cap

        version = ''
        bot = self.bot
        ws_err = '机器人不在线（WebSocket 未连接）'
        if bot is not None and hasattr(bot, 'call_api'):
            try:
                info, _err = _unwrap_response(
                    await bot.call_api('get_version_info', {}), 'get_version_info')
                if isinstance(info, dict):
                    version = str(info.get('app_name') or info.get('version') or '')
                    if info.get('version') and info.get('app_name'):
                        version = f'{info.get("app_name")} {info.get("version")}'
            except Exception:
                pass
            data, ws_err = await self._ws('get_qzone_msg_list', {'num': 1})
            if data is not None:
                self.cap = QzoneCapability(True, 'ws', '', version, time.time())
                return self.cap
            ws_err = ws_err or '未知错误'
        url, _token = resolve_endpoint(self.cfg)
        http_err = '未配置 NapCat HTTP 服务端地址（插件 napcat_http_url 或根 config.json 的 http_address）'
        if url:
            data, http_err = await self._http('get_qzone_msg_list', {'num': 1})
            if data is not None:
                self.cap = QzoneCapability(True, 'http', '', version, time.time())
                return self.cap
        why = (f'WS 通道探测失败（{ws_err}）；'
               f'HTTP 兜底不可用（{http_err}）。'
               '若 NapCat 版本低于 4.18.41 / 不是含空间扩展的构建，'
               '则没有 get_qzone_msg_list 等空间 action。')
        self.cap = QzoneCapability(False, '', why, version, time.time())
        return self.cap

    async def _call(self, action: str, payload: dict):
        """调一个空间 action：WS 优先，WS 失败且配置了 HTTP 时兜底一次。"""
        if runtime_env.is_official():
            return None, runtime_env.unavailable_reason('QQ 空间')
        if self.cap.available is None:
            await self.refresh()
        if not self.available():
            return None, self.cap.why or 'QQ 空间通道不可用'
        if self.cap.channel == 'ws':
            data, err = await self._ws(action, payload)
            if data is not None:
                return data, ''
            url, _token = resolve_endpoint(self.cfg)
            if url:
                data2, err2 = await self._http(action, payload)
                if data2 is not None:
                    self.cap = QzoneCapability(
                        True, 'http', '', self.cap.version, time.time())
                    return data2, ''
                return None, f'WS 调用失败（{err}）；HTTP 兜底也失败（{err2}）'
            # 通道可能坏了：缓存作废，下次重探
            self.cap = QzoneCapability(None, '', '', self.cap.version, 0.0)
            return None, err
        return await self._http(action, payload)

    async def _ws(self, action: str, payload: dict):
        bot = self.bot
        if bot is None or not hasattr(bot, 'call_api'):
            return None, '机器人不在线（WebSocket 未连接）'
        try:
            resp = await bot.call_api(action, payload)
        except Exception as e:
            return None, f'WS 调用 {action} 异常：{e}'
        return _unwrap_response(resp, action)

    def _http_client(self):
        """复用 AsyncClient（原实现每次调用新建连接池，HTTP 兜底与配图下载
        每次都要重新建连/握手；性能审计里属于无谓开销）。"""
        cli = getattr(self, '_cli', None)
        if cli is None or getattr(cli, 'is_closed', False):
            cli = httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True)
            self._cli = cli
        return cli

    async def aclose(self):
        """关闭复用的 HTTP 客户端（on_stop / 热重载时调用）。"""
        cli = getattr(self, '_cli', None)
        self._cli = None
        if cli is None:
            return
        try:
            await cli.aclose()
        except Exception:
            pass

    async def _http(self, action: str, payload: dict):
        """单次 NapCat OneBot HTTP action（兜底通道）。返回 (data, 错误文案)。"""
        if httpx is None:
            return None, ('缺少 httpx，HTTP 兜底通道不可用'
                          '（pip install httpx，或改用 WS 通道）')
        url, token = resolve_endpoint(self.cfg)
        if not url:
            return None, ('未配置 NapCat HTTP 地址（根 config.json 的 '
                          'http_address，或插件配置 napcat_http_url）')
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        try:
            resp = await self._http_client().post(
                f'{url}/{action}',
                content=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                headers=headers)
        except httpx.HTTPError as e:
            return None, f'无法连接 NapCat HTTP 服务：{e}'
        try:
            raw = resp.json()
        except ValueError:
            return None, f'NapCat 返回非 JSON（HTTP {resp.status_code}）'
        if not isinstance(raw, dict) or raw.get('retcode') != 0 \
                or raw.get('status') not in ('ok', 'async'):
            why = (raw.get('wording') or raw.get('msg') or raw.get('message')
                   or str(raw)) if isinstance(raw, dict) else str(raw)
            return None, f'NapCat 拒绝请求：{why}'
        d = raw.get('data')
        return (d if isinstance(d, dict) else {}), ''

    # ── 读：列表 / 详情 / 动态 ────────────────────────────────

    async def fetch_posts(self, limit: int = 10, target_uin: str = '') -> tuple:
        """自己的说说列表。返回 (msglist, 错误文案)。"""
        try:
            limit = max(1, min(int(limit or 10), 100))
        except (TypeError, ValueError):
            limit = 10
        payload = {'num': limit, 'pos': 0, 'format': 'snowluma'}
        if str(target_uin or '').strip().isdigit():
            payload['target_uin'] = str(target_uin).strip()
        data, err = await self._call('get_qzone_msg_list', payload)
        if err:
            return [], err
        msglist = data.get('msglist')
        if not isinstance(msglist, list):
            return [], ('NapCat 返回的不是 snowluma 列表格式（可能是旧版本只支持 '
                        'native H5 格式）')
        return [m for m in msglist if isinstance(m, dict)], ''

    async def fetch_feeds(self, limit: int = 10, page: int = 1) -> tuple:
        """好友动态。返回 (feeds, has_more, next_cursor, 错误文案)。"""
        try:
            limit = max(1, min(int(limit or 10), 30))
            page = max(1, int(page or 1))
        except (TypeError, ValueError):
            limit, page = 10, 1
        data, err = await self._call(
            'get_qzone_feeds',
            {'format': 'snowluma', 'page_num': page, 'count': limit})
        if err:
            return [], False, None, err
        feeds = data.get('feeds')
        if not isinstance(feeds, list):
            return [], False, None, (
                'NapCat 返回的不是 snowluma 动态格式（可能是旧版本只支持 native）')
        return ([f for f in feeds if isinstance(f, dict)],
                bool(data.get('has_more')), data.get('next_cursor'), '')

    async def fetch_detail(self, tid: str, pos: int = 0, num: int = 20) -> tuple:
        """单条说说详情（含评论树）。返回 (detail, 错误文案)。"""
        tid = str(tid or '').strip()
        if not tid:
            return {}, '错误: tid 必填（来自列表或发布结果）'
        try:
            num = max(1, min(int(num or 20), 100))
        except (TypeError, ValueError):
            num = 20
        data, err = await self._call(
            'get_qzone_msg_detail',
            {'format': 'snowluma', 'tid': tid, 'pos': max(0, int(pos or 0)),
             'num': num})
        if err:
            return {}, err
        return data, ''

    # ── 写：发布 / 删除 / 评论 / 点赞 ─────────────────────────

    async def publish(self, content: str, ugc_right: int = 1,
                      target_uins=None, images=None, sender_id: str = '',
                      sender_name: str = '') -> str:
        content = str(content or '').strip()
        imgs = [str(u).strip() for u in (images or []) if str(u).strip()]
        if not content and not imgs:
            return '错误: 说说正文不能为空（纯图片说说请提供 images）'
        if len(content) > MAX_CONTENT:
            return f'错误: 说说正文最长 {MAX_CONTENT} 字（当前 {len(content)}）'
        if len(imgs) > MAX_IMAGES:
            return f'错误: 一条说说最多 {MAX_IMAGES} 张图片（当前 {len(imgs)}）'
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
        payload = {}
        if content:
            payload['content'] = content
        if imgs:
            payload['images'] = imgs
        if ugc_right != 1:
            payload['ugc_right'] = ugc_right
            payload['target_uins'] = uins
        data, err = await self._call('send_qzone_msg', payload)
        if err:
            return '错误: ' + err
        tid = str((data or {}).get('tid') or '').strip()
        if not tid:
            return '错误: 发送成功但响应里没有 tid'
        storage.add_qzone_post(
            self.p.db_path, tid, content, ugc_right,
            creator_id=str(sender_id or ''),
            creator_name=str(sender_name or ''),
            images=imgs)
        scope = UGC_RIGHTS[ugc_right]
        extra = f'，含 {len(imgs)} 张图' if imgs else ''
        return (f'说说已发布（{scope}{extra}），tid={tid}。\n'
                '删除请用 qzone_delete 并提供这个 tid；最近发布的也能用 '
                'qzone_recent 查到。')

    async def delete(self, tid: str) -> str:
        tid = str(tid or '').strip()
        if not tid:
            return '错误: tid 必填（来自发布结果或 qzone_recent）'
        _, err = await self._call('delete_qzone_msg', {'tid': tid})
        if err:
            return '错误: ' + err
        storage.delete_qzone_post(self.p.db_path, tid)
        return f'说说 {tid} 已删除'

    async def comment(self, tid: str, content: str,
                      comment_id: str = '', comment_uin: str = '',
                      reply_uin: str = '', reply_name: str = '',
                      images=None, target_uin: str = '') -> str:
        """评论说说；带 comment_id + comment_uin 即回复某条评论。"""
        tid = str(tid or '').strip()
        content = str(content or '').strip()
        imgs = [str(u).strip() for u in (images or []) if str(u).strip()]
        if not tid and not str(target_uin or '').strip():
            return '错误: tid 必填（要评论的说说 id，来自 qzone_list/qzone_detail）'
        if not content and not imgs:
            return '错误: 评论内容不能为空'
        payload = {}
        if tid:
            payload['tid'] = tid
        if str(target_uin or '').strip().isdigit():
            payload['target_uin'] = str(target_uin).strip()
        payload['content'] = content
        cid, cuin = str(comment_id or '').strip(), str(comment_uin or '').strip()
        if bool(cid) != bool(cuin):
            return '错误: 回复某条评论时 comment_id 与 comment_uin 必须成对提供'
        if cid:
            payload['comment_id'] = cid
            payload['comment_uin'] = cuin
            if str(reply_uin or '').strip():
                payload['reply_uin'] = str(reply_uin).strip()
            if str(reply_name or '').strip():
                payload['reply_name'] = str(reply_name).strip()
        elif imgs:
            payload['images'] = imgs[:MAX_IMAGES]
        data, err = await self._call('comment_qzone', payload)
        if err:
            return '错误: ' + err
        cid2 = str((data or {}).get('comment_id') or '').strip()
        if cid:
            who = str(reply_name or comment_uin or '').strip()
            return (f'已回复 tid={tid} 的评论{("（回复 " + who + "）") if who else ""}'
                    f'，comment_id={cid2 or "未知"}')
        return (f'已在说说 {tid} 下发表评论，comment_id={cid2 or "未知"}')

    async def like(self, tid: str, on: bool = True) -> str:
        tid = str(tid or '').strip()
        if not tid:
            return '错误: tid 必填'
        action = 'like_qzone' if on else 'unlike_qzone'
        _, err = await self._call(action, {'tid': tid})
        if err:
            return '错误: ' + err
        return f'已{"点赞" if on else "取消点赞"}说说 {tid}'

    # ── 读图 ──────────────────────────────────────────────────

    async def read_images(self, tid: str, indexes=None) -> tuple:
        """取说说配图给模型看。返回 (说明文本, [(mime, base64)])。"""
        detail, err = await self.fetch_detail(tid, num=1)
        if err:
            return '错误: ' + err, []
        images = [str(u) for u in (detail.get('images') or []) if u]
        if not images:
            return f'说说 {tid} 没有配图', []
        picks = list(range(len(images)))
        if indexes:
            picked = []
            for i in indexes:
                try:
                    n = int(i)
                except (TypeError, ValueError):
                    continue
                if 0 <= n < len(images):
                    picked.append(n)
            if picked:
                picks = picked[:MAX_READ_IMAGES]
        else:
            picks = picks[:MAX_READ_IMAGES]
        cap = int(self.cfg.get('qzone_image_max_kb', 10240) or 10240) * 1024
        got = []
        notes = []
        if httpx is None:
            return '错误: 缺少 httpx，无法下载空间配图', []
        cli = self._http_client()
        for i in picks:
            url = images[i]
            try:
                resp = await cli.get(url)
                raw = resp.content or b''
            except httpx.HTTPError as e:
                notes.append(f'第 {i + 1} 张下载失败：{e}')
                continue
            if not raw:
                notes.append(f'第 {i + 1} 张内容为空')
                continue
            if len(raw) > cap:
                notes.append(f'第 {i + 1} 张超过 {cap // 1024}KB 上限，已跳过')
                continue
            mime = _sniff_image(raw)
            if not mime:
                notes.append(f'第 {i + 1} 张不是受支持的图片格式')
                continue
            import base64 as _b64
            got.append((mime, _b64.b64encode(raw).decode('ascii')))
        text = (f'说说 {tid} 共 {len(images)} 张图，本次取回 {len(got)} 张'
                + ('（其余：' + '；'.join(notes) + '）' if notes else ''))
        if len(images) > len(picks):
            text += f'；可用 index 指定其余图片（0-based，最多 {MAX_READ_IMAGES} 张）'
        return text, got

    # ── 工具文本格式化 ────────────────────────────────────────

    async def list_posts(self, limit: int = 10, target_uin: str = '') -> str:
        rows, err = await self.fetch_posts(limit, target_uin)
        if err:
            return '错误: ' + err
        if not rows:
            return '空间里还没有说说'
        who = f'QQ {target_uin}' if str(target_uin or '').strip() else '你'
        lines = [f'{who}最近的说说（{len(rows)} 条，较新在前）：']
        for m in rows:
            tid = str(m.get('tid') or '')
            imgs = m.get('images') or []
            lines.append(
                f'- tid={tid or "(无)"} 〔{fmt_time(m.get("time"))}〕'
                f' 评论{m.get("comment_num", 0)} 图{len(imgs)}'
                f'{" 仅自己可见" if m.get("is_private") else ""}\n'
                f'  {clip(m.get("content"), 160)}')
        lines.append('要读某条的完整评论/配图：qzone_detail、qzone_images；'
                     '要评论或回复：qzone_comment。')
        return '\n'.join(lines)

    async def post_detail(self, tid: str) -> str:
        d, err = await self.fetch_detail(tid)
        if err:
            return '错误: ' + err
        if not d:
            return f'说说 {tid} 读不到详情（可能已删除或非好友可见）'
        imgs = [str(u) for u in (d.get('images') or []) if u]
        lines = [f'tid={tid} 〔时间未知〕 {clip(d.get("content"), 300)}']
        if imgs:
            lines.append(f'配图 {len(imgs)} 张：' + ' '.join(
                f'[{i}]' for i in range(len(imgs)))
                + '（要看图用 qzone_images）')
        comments = d.get('comments') or []
        if not comments:
            lines.append('还没有评论。')
            return '\n'.join(lines)
        lines.append(f'评论（{d.get("comment_num") or len(comments)} 条）：')
        for c in comments:
            lines.append(
                f'- {c.get("name") or c.get("uin")}（uin={c.get("uin")}，'
                f'comment_id={c.get("comment_id")}）：{clip(c.get("content"), 200)}')
            for r in (c.get('replies') or []):
                lines.append(
                    f'    ↳ {r.get("name") or r.get("uin")}'
                    f'（uin={r.get("uin")}，comment_id={r.get("comment_id")}）：'
                    f'{clip(r.get("content"), 160)}')
        lines.append('回复某条评论用 qzone_comment（tid + comment_id + '
                     'comment_uin 成对提供）。')
        return '\n'.join(lines)

    async def list_feeds(self, limit: int = 10) -> str:
        feeds, more, _cur, err = await self.fetch_feeds(limit)
        if err:
            return '错误: ' + err
        if not feeds:
            return '好友动态是空的（可能没有好友/权限不足）'
        lines = [f'好友动态（{len(feeds)} 条）：']
        for f in feeds:
            imgs = f.get('images') or []
            lines.append(
                f'- {f.get("nickname") or f.get("uin")}'
                f'（uin={f.get("uin")}，tid={f.get("tid") or "无"}）'
                f'〔{fmt_time(f.get("time"))}〕赞{f.get("like_count") or 0} '
                f'评论{f.get("comment_count") or 0} 图{len(imgs)}\n'
                f'  {clip(f.get("content"), 160)}')
        if more:
            lines.append('还有更多（本工具暂不分页）')
        return '\n'.join(lines)

    def recent(self, limit: int = 10) -> str:
        """本地发布留痕（含图片数、权限），不依赖网络。"""
        try:
            limit = max(1, min(int(limit or 10), 30))
        except (TypeError, ValueError):
            limit = 10
        rows = storage.list_qzone_posts(self.p.db_path, limit)
        if not rows:
            return '本地还没有发布过说说（tid 只在发布成功后登记）'
        lines = ['最近发布的说说（仅本机器人发布留痕）：']
        for r in rows:
            t = fmt_time(r['created_at'])
            scope = ugc_text(r['ugc_right'])
            try:
                n = len(json.loads(r.get('images') or '[]'))
            except (TypeError, ValueError):
                n = 0
            preview = clip(r['content'], 60)
            lines.append(f'- [{t}] tid={r["tid"]}（{scope}，图{n}）{preview}')
        return '\n'.join(lines)


_VISION_MAGIC = (
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
    (b'BM', 'image/bmp'),
)


def _sniff_image(raw: bytes):
    for magic, mime in _VISION_MAGIC:
        if raw.startswith(magic):
            return mime
    if len(raw) > 12 and raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        return 'image/webp'
    return ''
