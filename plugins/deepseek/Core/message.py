"""入站消息归一化：段数组优先，CQ 码串兜底。

落库文本里：
- 图片保留可操作的紧凑 CQ 码 `[CQ:image,file=<哈希>]`（可用 read_message_image
  回看）；
- 文件保留下载参数 `[文件 name="..." file_id=... busid=... size=...]`
  （参数可直接喂 qq_download_file）；语音仍是 `[语音]` 占位；
- 视频保留下载参数 + 工作区路径 `[视频 name="..." ... path=mcp/...]`
  （触发后由 media_in 存到工作区，可把 path 喂 read_video）；
- json/xml 分享卡片提取「标题 + 跳转链接」（B 站/小黑盒/小程序/音乐等）；
- 合并转发保留 `[CQ:forward,id=...]`（可用 read_forward 展开）；
- 名片给 `[名片] QQ:/群:`。
媒体引用同时收在 media 列表里，触发通过后由 media_in 下载，图片走视觉输入、
文件解析为文本（不入库）。不 import openai/mcp，纯逻辑可单测
（parse_event 之外全部可测）。
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from urllib.parse import unquote

from .models import ParsedMessage

# 只出占位、不给可操作码的类型（图片走紧凑 CQ 码，单独处理）
_MEDIA_PLACEHOLDER = {
    'record': '[语音]', 'face': '[表情]',
    'video': '[视频]', 'file': '[文件]',
}

# CQ 码：[CQ:type,key=value,...]，value 可能含转义
_CQ_RE = re.compile(r'\[CQ:[a-zA-Z0-9_]+(?:,[^\]]*)?\]')
_CQ_PARSE_RE = re.compile(r'\[CQ:([a-zA-Z0-9_]+)(?:,(.*))?\]')

# 卡片标题候选键（按优先级）；URL 候选键
_TITLE_BEST = ('title',)
_TITLE_FALLBACK = ('desc', 'description', 'prompt')
_URL_KEYS = ('url', 'jumpurl', 'qqdocurl', 'link', 'source_url')
# 这些祖先节点下的标题/描述多为按钮文案/预览冗余，不卡片标题
_TITLE_SKIP_PATHS = ('preview', 'button', 'buttons', 'action', 'icon', 'cover')


def _cq_unescape(s: str) -> str:
    return (s.replace('&#91;', '[').replace('&#93;', ']')
             .replace('&#44;', ',').replace('&amp;', '&'))


def _same_id(a, b) -> bool:
    """QQ 号 int/str 双比对（'all' 不算 at 机器人）。"""
    if a is None or b is None:
        return False
    return str(a).strip() == str(b).strip()


def _collapse(s: str, limit: int = 60) -> str:
    s = ' '.join((s or '').split())
    return s if len(s) <= limit else s[:limit] + '…'


# ── 富文本片段：图片码 / 卡片 / 名片 / 转发 ─────────────────────

def _image_code(data: dict, *, human: bool = False) -> str:
    """图片段 → 紧凑可操作 CQ 码；human 模式给回复摘要用的 [图片]。"""
    if human:
        return '[图片]'
    f = str(data.get('file') or '').strip()
    if f:
        return f'[CQ:image,file={f}]'
    u = str(data.get('url') or '').strip()
    if u.startswith(('http://', 'https://')):
        return f'[CQ:image,url={u}]'
    return '[图片]'


def _param(value, limit: int = 120) -> str:
    """参数值压成单行、去掉会破坏占位符结构的字符。"""
    s = ' '.join(str(value or '').split()).replace('"', "'").replace(']', '）')
    return s if len(s) <= limit else s[:limit] + '…'


def _media_kv(data: dict, *, size: bool = True) -> list:
    """文件/视频段 → [name="…", file_id=…, busid=…, size=…]，缺项省略。"""
    kv = []
    name = _param(data.get('name') or data.get('file') or '')
    if name:
        kv.append(f'name="{name}"')
    fid = _param(data.get('file_id') or '', 200)
    if fid:
        kv.append(f'file_id={fid}')
    busid = data.get('busid')
    if busid not in (None, ''):
        kv.append(f'busid={_param(busid, 32)}')
    raw_size = data.get('file_size') if data.get('file_size') not in (
        None, '') else data.get('size')
    if size and raw_size not in (None, ''):
        kv.append(f'size={_param(raw_size, 32)}')
    return kv


def _file_code(data: dict, *, human: bool = False) -> str:
    """文件段 → 带下载参数的占位符（参数可直接喂 qq_download_file）。"""
    if human:
        return '[文件]'
    kv = _media_kv(data)
    return f'[文件 {" ".join(kv)}]' if kv else '[文件]'


_VIDEO_EXTS = ('.mp4', '.m4v', '.mov', '.mkv', '.webm', '.avi', '.flv',
               '.wmv', '.mpg', '.mpeg', '.ts', '.3gp', '.gif')


def inbound_video_path(ref: dict) -> str:
    """入站视频在工作区里的确定性落盘路径（media_in 写、占位符里写同一个）。

    以 file_id/file/url 做种子，同一段反复出现得到同一路径；扩展名取文件名
    里 read_video 支持的视频后缀，识别不了按 .mp4。
    """
    seed = str(ref.get('file_id') or ref.get('file') or ref.get('url') or '')
    if not seed:
        return ''
    key = hashlib.sha1(seed.encode('utf-8', 'ignore')).hexdigest()[:16]
    ext = ''
    for src in (ref.get('file'), ref.get('name'), ref.get('url')):
        path = str(src or '').split('?', 1)[0]
        if '.' in path.rsplit('/', 1)[-1]:
            cand = '.' + path.rsplit('.', 1)[-1].lower()
            if cand in _VIDEO_EXTS:
                ext = cand
                break
    return f'mcp/inbound_video_{key}{ext or ".mp4"}'


def _video_code(data: dict, *, human: bool = False,
                inbound: bool = False) -> str:
    """视频段 → 带下载参数（入站消息再加工作区 path）的占位符。"""
    if human:
        return '[视频]'
    kv = _media_kv(data)
    if inbound:
        rel = inbound_video_path(data)
        if rel:
            kv.append(f'path={rel}')
    return f'[视频 {" ".join(kv)}]' if kv else '[视频]'


def _contact_text(data: dict) -> str:
    t = str(data.get('type') or '').lower()
    cid = str(data.get('id') or '').strip()
    if not cid:
        return '[名片]'
    kind = '群' if t == 'group' else 'QQ'
    return f'[名片] {kind}:{cid}'


def _forward_text(data: dict) -> str:
    fid = str(data.get('id') or data.get('resid') or '').strip()
    return f'[CQ:forward,id={fid}]' if fid else '[合并转发]'


def _walk_json(obj, path=()):
    """深度优先产出 (lowercase key 路径, 叶子值)。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_json(v, path + (str(k).lower(),))
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_json(v, path)
    else:
        yield path, obj


def _assemble_card(title: str, url: str) -> str:
    title = ' '.join((title or '').split())
    if len(title) > 80:
        title = title[:80]
    if not url.startswith(('http://', 'https://')):
        url = ''
    if len(url) > 300:
        url = ''  # 截断的链接不可用，宁可不放
    if title and url:
        s = f'[卡片] {title} {url}'
        return s if len(s) <= 200 else f'[卡片] {title}'
    if title:
        return f'[卡片] {title}'
    if url:
        s = f'[卡片] {url}'
        return s if len(s) <= 200 else '[卡片]'
    return '[卡片]'


def _card_from_json(raw: str) -> str:
    """json 卡片段（structmsg/detail_1/miniapp/音乐等形态）→ 标题 + 链接。

    各形态结构差异大，统一在整棵 JSON 树里按优先级找标题键、找首个 http URL。
    """
    obj = None
    if isinstance(raw, (dict, list)):
        obj = raw
    else:
        # 入参已是原始 JSON 串：段通道本就未转义；CQ 串通道由 _parse_kv 解过
        # 一次实体（不能再 _cq_unescape，否则双重解码）。unquote 兜底老式
        # go-cqhttp 的百分号编码。
        text = str(raw or '')
        for candidate in (text, unquote(text)):
            try:
                obj = json.loads(candidate)
                break
            except (json.JSONDecodeError, ValueError):
                continue
    if obj is None:
        return '[卡片]'

    title, best_rank, best_depth = '', 99, 99
    url = ''
    for path, v in _walk_json(obj):
        if not isinstance(v, str) or not path:
            continue
        val = v.strip()
        if not val:
            continue
        key = path[-1]
        if any(sk in path[:-1] for sk in _TITLE_SKIP_PATHS):
            pass
        elif key in _TITLE_BEST:
            rank = 0
        elif key in _TITLE_FALLBACK:
            rank = 1
        else:
            rank = 99
        if rank < 99:
            cand = ' '.join(val.split())
            # prompt 常见值是 "[QQ小程序]" 这类壳子文案，剥掉方括号再用
            cand = cand.strip('[]【】')
            if cand and not cand.startswith('http') and len(cand) <= 80:
                depth = len(path)
                if rank < best_rank or (rank == best_rank and depth < best_depth):
                    title, best_rank, best_depth = cand, rank, depth
        if not url and key in _URL_KEYS and val.startswith(
                ('http://', 'https://')) and len(val) <= 500:
            url = val
    return _assemble_card(title, url)


def _card_from_xml(raw: str) -> str:
    """xml 卡片段：<title> 文本 + <url>/url 属性里的首个 http 链接。"""
    text = html.unescape(_cq_unescape(str(raw or '')))
    title = ''
    m = re.search(r'<title[^>]*>(.*?)</title>', text, re.S | re.I)
    if m:
        title = re.sub(r'<[^>]+>', '', m.group(1))
    url = ''
    m = re.search(r'<url[^>]*>\s*(https?://[^\s<]+)', text, re.I)
    if not m:
        m = re.search(r'\burl=["\']?(https?://[^\s"\'<>]+)', text, re.I)
    if m:
        url = m.group(1)
    return _assemble_card(title, url)


def _rich_other(stype: str, data: dict, *, human: bool,
                inbound: bool = False) -> str:
    """非 text/at/reply 段 → 富文本（'' 表示无文本）。

    human=True 用于被回复消息摘要：图片回 [图片]、卡片不带链接。
    inbound=True 只用于当前入站正文：文件/视频带下载参数与工作区路径；
    历史转发节点里没有实际下载，保持朴素占位避免误导。
    """
    if stype in ('image', 'flash'):
        return _image_code(data, human=human)
    if stype == 'file':
        return _file_code(data, human=human or not inbound)
    if stype == 'video':
        return _video_code(data, human=human, inbound=inbound)
    if stype == 'json':
        # human 模式由 _plain_from_segments 改走 _human_card（去链接）
        return _card_from_json(data.get('data', ''))
    if stype == 'xml':
        return _card_from_xml(data.get('data', ''))
    if stype == 'forward':
        return '[合并转发]' if human else _forward_text(data)
    if stype == 'contact':
        return _contact_text(data)
    if stype == 'node':
        return '[合并转发]'
    return _MEDIA_PLACEHOLDER.get(stype, '')


def _human_card(stype: str, data: dict) -> str:
    """回复摘要里的卡片：[卡片] 标题（去掉链接，省 60 字预算）。"""
    if stype == 'json':
        card = _card_from_json(data.get('data', ''))
    elif stype == 'xml':
        card = _card_from_xml(data.get('data', ''))
    else:
        return ''
    # _assemble_card 输出形如 '[卡片] 标题 URL' / '[卡片] 标题' / '[卡片] URL'
    if card == '[卡片]':
        return card
    body = card[len('[卡片] '):]
    http = body.find('http')
    if http > 0:
        body = body[:http].strip()
    return f'[卡片] {body}' if body and not body.startswith('http') else '[卡片]'


def _plain_from_segments(segments) -> str:
    """段数组 → 人话纯文本（@保留为 @名字/QQ，媒体占位、卡片不带链接）。

    用于被回复消息摘要；入站正文走 parse_segments 保留可操作码。
    """
    out = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        stype, data = seg.get('type', ''), seg.get('data') or {}
        if stype == 'text':
            out.append(data.get('text', ''))
        elif stype == 'at':
            qq = data.get('qq', '')
            out.append('@全体成员' if str(qq).lower() == 'all'
                       else f'@{data.get("name") or qq}')
        elif stype in ('json', 'xml'):
            out.append(_human_card(stype, data))
        else:
            out.append(_rich_other(stype, data, human=True))
    return ''.join(out)


def _extract_reply(reply_info: dict | None, bot_id) -> dict:
    """从 event._raw['reply'] 提取 id/被回复者/被回复内容摘要。"""
    out = {'reply_id': '', 'reply_to_bot': False, 'reply_to_id': '',
           'reply_to_name': '', 'reply_excerpt': ''}
    if not isinstance(reply_info, dict):
        return out
    rid = reply_info.get('id') or reply_info.get('message_id') or ''
    out['reply_id'] = str(rid) if rid else ''
    rsender = reply_info.get('sender') or {}
    to_id = rsender.get('user_id')
    if to_id is not None:
        out['reply_to_id'] = str(to_id)
        out['reply_to_bot'] = _same_id(to_id, bot_id)
    out['reply_to_name'] = str(rsender.get('card') or rsender.get('nickname') or '')
    msg = reply_info.get('message')
    if isinstance(msg, list):
        excerpt = _plain_from_segments(msg)
    else:
        # 字符串消息或仅有 raw_message 时，统一剥 CQ 码
        excerpt = msg if isinstance(msg, str) else (reply_info.get('raw_message') or '')
        excerpt = _CQ_RE.sub(_cq_to_plain, excerpt)
    out['reply_excerpt'] = _collapse(excerpt)
    return out


def _cq_to_plain(m: re.Match) -> str:
    """CQ 码 → 纯文本占位（用于被回复消息字符串）。"""
    cm = _CQ_PARSE_RE.match(m.group(0))
    stype, kv = cm.group(1), cm.group(2) or ''
    data = dict(_parse_kv(kv))
    if stype == 'at':
        qq = data.get('qq', '')
        return '@全体成员' if str(qq).lower() == 'all' \
            else f'@{_cq_unescape(data.get("name", qq))}'
    if stype == 'reply':
        return ''
    if stype in ('image', 'flash'):
        return '[图片]'
    if stype == 'forward':
        return '[合并转发]'
    if stype in ('json', 'xml'):
        return _human_card(stype, data)
    if stype == 'contact':
        return _contact_text(data)
    return _MEDIA_PLACEHOLDER.get(stype, '')


def parse_segments(segments: list, bot_id, reply_info: dict | None = None,
                   sender: dict | None = None) -> ParsedMessage:
    """解析 OneBot 段数组。reply_info 是 event._raw.get('reply')。"""
    parts, at_me, at_users, at_names, has_media, media = \
        [], False, [], {}, False, []
    rep = _extract_reply(reply_info, bot_id)
    reply_id = rep['reply_id']

    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        stype = seg.get('type', '')
        data = seg.get('data') or {}
        if stype == 'text':
            parts.append(data.get('text', ''))
        elif stype == 'at':
            qq = data.get('qq', '')
            if str(qq).lower() == 'all':
                parts.append('@全体成员')
            else:
                qq = str(qq)
                at_users.append(qq)
                if _same_id(qq, bot_id):
                    at_me = True
                nm = str(data.get('name') or '').strip()
                if nm:
                    at_names[qq] = nm
                parts.append(f'@{nm or qq}')
        elif stype == 'reply':
            if not reply_id:
                reply_id = str(data.get('id', ''))
        else:
            parts.append(_rich_other(stype, data, human=False, inbound=True))
            # 媒体下载引用：图片（含闪照）/语音/文件/视频
            if stype in ('image', 'flash'):
                has_media = True
                ref = {k: str(v) for k, v in (data or {}).items()
                       if v is not None}
                ref['kind'] = 'image'
                media.append(ref)
            elif stype in _MEDIA_PLACEHOLDER:
                has_media = True
                if stype in ('record', 'file', 'video'):
                    ref = {k: str(v) for k, v in (data or {}).items()
                           if v is not None}
                    ref['kind'] = stype
                    media.append(ref)

    text = ''.join(parts).strip()
    sd = sender or {}
    return ParsedMessage(
        text=text,
        sender_id=str(sd.get('user_id', '')),
        sender_name=str(sd.get('nickname', '') or ''),
        sender_card=str(sd.get('card', '') or ''),
        at_me=at_me,
        reply_to_bot=rep['reply_to_bot'],
        reply_id=reply_id,
        reply_to_id=rep['reply_to_id'],
        reply_to_name=rep['reply_to_name'],
        reply_excerpt=rep['reply_excerpt'],
        has_media=has_media,
        at_users=at_users,
        at_names=at_names,
        media=media,
    )


def parse_cq(raw_message: str, bot_id, sender: dict | None = None,
             reply_info: dict | None = None) -> ParsedMessage:
    """段数组缺失时从 raw_message 的 CQ 码串解析。"""
    parts, at_me, at_users, at_names, has_media, media = \
        [], False, [], {}, False, []
    rep = _extract_reply(reply_info, bot_id)
    reply_id = rep['reply_id']

    def _sub(m: re.Match) -> str:
        nonlocal at_me, has_media, reply_id
        cm = _CQ_PARSE_RE.match(m.group(0))
        stype, kv = cm.group(1), cm.group(2) or ''
        data = dict(_parse_kv(kv))
        if stype == 'at':
            qq = data.get('qq', '')
            if str(qq).lower() == 'all':
                return '@全体成员'
            qq = str(qq)
            at_users.append(qq)
            if _same_id(qq, bot_id):
                at_me = True
            nm = _cq_unescape(data.get('name', '')).strip()
            if nm:
                at_names[qq] = nm
            return f'@{nm or qq}'
        if stype == 'reply':
            reply_id = reply_id or data.get('id', '')
            return ''
        if stype in ('image', 'flash'):
            has_media = True
            ref = dict(data)
            ref['kind'] = 'image'
            media.append(ref)
            return _image_code(data)
        if stype in ('record', 'file', 'video'):
            has_media = True
            ref = dict(data)
            ref['kind'] = stype
            media.append(ref)
            if stype == 'file':
                return _file_code(data)
            if stype == 'video':
                return _video_code(data, inbound=True)
            return _MEDIA_PLACEHOLDER[stype]
        if stype == 'face':
            has_media = True
            return _MEDIA_PLACEHOLDER[stype]
        if stype == 'json':
            return _card_from_json(data.get('data', ''))
        if stype == 'xml':
            return _card_from_xml(data.get('data', ''))
        if stype == 'forward':
            return _forward_text(data)
        if stype == 'contact':
            return _contact_text(data)
        return ''

    text = _CQ_RE.sub(_sub, raw_message or '').strip()
    sd = sender or {}
    return ParsedMessage(
        text=text,
        sender_id=str(sd.get('user_id', '')),
        sender_name=str(sd.get('nickname', '') or ''),
        sender_card=str(sd.get('card', '') or ''),
        at_me=at_me,
        reply_to_bot=rep['reply_to_bot'],
        reply_id=str(reply_id or ''),
        reply_to_id=rep['reply_to_id'],
        reply_to_name=rep['reply_to_name'],
        reply_excerpt=rep['reply_excerpt'],
        has_media=has_media,
        at_users=at_users,
        at_names=at_names,
        media=media,
    )


def _parse_kv(kv: str):
    # 朴素切分：CQ 码 value 内逗号已转义为 &#44;
    for pair in kv.split(','):
        if '=' in pair:
            k, v = pair.split('=', 1)
            yield k.strip(), _cq_unescape(v)


def parse_event(event, bot_id) -> ParsedMessage:
    """从框架 MessageEvent 构造 ParsedMessage。"""
    raw = getattr(event, '_raw', {}) or {}
    sender = raw.get('sender') or {}
    reply_info = raw.get('reply')
    segments = getattr(event, 'message', None)

    if isinstance(segments, list) and segments:
        parsed = parse_segments(segments, bot_id, reply_info, sender)
    else:
        parsed = parse_cq(getattr(event, 'raw_message', '') or '',
                          bot_id, sender, reply_info)

    # 兜底：event 属性比段解析更可靠时补上身份
    if not parsed.sender_id:
        parsed.sender_id = str(getattr(event, 'user_id', '') or '')
    if not parsed.sender_name and getattr(event, 'sender', None):
        parsed.sender_name = str(getattr(event.sender, 'nickname', '') or '')

    mtype = str(raw.get('message_type')
                or getattr(event, 'message_type', '') or 'group')
    parsed.chat_type = 'private' if mtype == 'private' else 'group'
    parsed.message_id = str(raw.get('message_id')
                            or getattr(event, 'message_id', '') or '')
    try:
        parsed.event_time = int(
            raw.get('time') or getattr(event, 'time', 0) or 0)
    except (TypeError, ValueError):
        parsed.event_time = 0
    return parsed


def self_group_join_notice(event, bot_id) -> tuple | None:
    """识别「机器人自己被拉进群」的 group_increase 通知。

    返回 (group_id, operator_id)；不是入群事件、入群者不是自己、缺群号时返回 None。
    event 是框架的 NoticeEvent（notice_type/sub_type/group_id/user_id/
    operator_id/self_id 都是属性）；纯函数可单测。
    """
    if getattr(event, 'notice_type', '') != 'group_increase':
        return None
    gid = getattr(event, 'group_id', None)
    joined = str(getattr(event, 'user_id', 0) or '')
    self_id = str(getattr(event, 'self_id', 0) or bot_id or '')
    if not gid or not joined or not self_id or joined != self_id:
        return None
    op = str(getattr(event, 'operator_id', 0) or '')
    return str(gid), (op if op != '0' else '')


# ── 合并转发节点归一（read_forward 工具用）─────────────────────

def node_body_text(node_data: dict) -> str:
    """get_forward_msg 单个 node 的消息体 → 富文本。

    message/content 可能是段数组，也可能是 CQ 字符串；嵌套 node 给 [合并转发]。
    """
    body = node_data.get('message')
    if body is None:
        body = node_data.get('content')
    if isinstance(body, list):
        return _segments_rich(body)
    if isinstance(body, str):
        return _CQ_RE.sub(_cq_forward_sub, body).strip()
    return ''


def normalize_forward_nodes(nodes) -> list:
    """get_forward_msg 的 data.messages → 正序归一列表。

    每项 {user_id, nickname, time(int), text}；节点段外层可能包一层 data，
    用户信息也可能直接挂在段顶层，两种都兼容。
    """
    out = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        d = node.get('data')
        if not isinstance(d, dict):
            d = node
        uid = str(d.get('user_id') or node.get('user_id') or '')
        nick = str(d.get('nickname') or d.get('name')
                   or node.get('nickname') or '')
        try:
            ts = int(d.get('time') or node.get('time') or 0)
        except (TypeError, ValueError):
            ts = 0
        out.append({'user_id': uid, 'nickname': nick, 'time': ts,
                    'text': node_body_text(d)})
    return out


def _segments_rich(segments) -> str:
    """转发节点里的段数组 → 与入站正文同级的富文本（图片码/卡片链接保留）。"""
    out = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        stype, data = seg.get('type', ''), seg.get('data') or {}
        if stype == 'text':
            out.append(data.get('text', ''))
        elif stype == 'at':
            qq = data.get('qq', '')
            out.append('@全体成员' if str(qq).lower() == 'all'
                       else f'@{data.get("name") or qq}')
        elif stype == 'reply':
            continue
        else:
            out.append(_rich_other(stype, data, human=False))
    return ''.join(out).strip()


def _cq_forward_sub(m: re.Match) -> str:
    """转发节点 CQ 串 → 富文本（保留图片码/卡片链接，与段数组通道一致）。"""
    cm = _CQ_PARSE_RE.match(m.group(0))
    stype, kv = cm.group(1), cm.group(2) or ''
    data = dict(_parse_kv(kv))
    if stype == 'at':
        qq = data.get('qq', '')
        return '@全体成员' if str(qq).lower() == 'all' \
            else f'@{_cq_unescape(data.get("name", qq))}'
    if stype == 'reply':
        return ''
    if stype in ('image', 'flash'):
        return _image_code(data)
    if stype == 'json':
        return _card_from_json(data.get('data', ''))
    if stype == 'xml':
        return _card_from_xml(data.get('data', ''))
    if stype == 'forward' or stype == 'node':
        return '[合并转发]'
    if stype == 'contact':
        return _contact_text(data)
    return _MEDIA_PLACEHOLDER.get(stype, '')
