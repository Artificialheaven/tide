"""终态回复切条与分条发送（模拟真人）。

split_reply 是纯函数；send_in_chunks 做异步逐条发送。
切分不消耗任何模型 token，只是多次 send_msg。
"""
from __future__ import annotations

import asyncio
import random
import re

_CQ_RE = re.compile(r'\[CQ:[a-zA-Z0-9_]+(?:,[^\]]*)?\]')
_FENCE_RE = re.compile(r'```[\s\S]*?```')
# 引用回复的 message_id 只允许有限字符集，防止把任意内容拼进 CQ 码
_QUOTE_ID_RE = re.compile(r'[0-9A-Za-z_-]{1,64}')


def reply_prefix(quote_id: str) -> str:
    """引用回复的 CQ 头；id 非法或为空返回 ''。必须加在净化之后再拼。"""
    qid = str(quote_id or '').strip()
    if not qid or not _QUOTE_ID_RE.fullmatch(qid):
        return ''
    return f'[CQ:reply,id={qid}]'


def take_quote(state: dict | None) -> str:
    """本轮引用状态：返回一次引用 id（每轮只有第一条文本消息能引用）。

    state = {'id': 触发消息 id, 'used': False}，由 _run_turn 每轮新建；
    文本发送出口（终态/插话/qq_send_text）共用，先到先得。图片/语音
    不调用这里，免得先发的媒体把唯一的引用名额用掉。
    """
    if not state or state.get('used'):
        return ''
    qid = str(state.get('id') or '')
    if not qid:
        return ''
    state['used'] = True
    return qid


def sanitize_model_text(text: str) -> str:
    """模型终态文本出站前净化：半角 [CQ: 一律转全角。

    模型文本里出现 CQ 码只可能是复读入站内容（历史/背景/转发里看到的）；
    合法真 @ 由 mentions.render_ats 在发送时生成、图片走段 API，都不经过模型文本，
    不净化会被 NapCat 当成真 CQ 码（如复读图片哈希会重发缓存图）。
    """
    return (text or '').replace('[CQ:', '［CQ：')
# 她偶尔照抄历史行首的档案时间戳当回复前缀（如 "[10-26 14:30] 在的"），
# 出站剥掉；只认时间标签样式，误伤面极小（裸 "14:30 开会" 这种正常内容不动）
_LEAD_TIME_PATTERNS = (
    re.compile(r'^\s*\[\s*\d{4}[-/.]\d{1,2}[-/.]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?\s*\]\s*'),
    re.compile(r'^\s*\[\s*\d{1,2}[-/.]\d{1,2}\s+\d{1,2}:\d{2}\s*\]\s*'),
    re.compile(r'^\s*\[\s*\d{1,2}:\d{2}(?::\d{2})?\s*\]\s*'),
    re.compile(r'^\s*\d{4}[-/.]\d{1,2}[-/.]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?[，,、\s]+'),
    re.compile(r'^\s*\d{1,2}[-/.]\d{1,2}\s+\d{1,2}:\d{2}[，,、\s]+'),
)


def strip_leading_time(text: str, max_rounds: int = 3) -> str:
    """剥离开头的时间戳前缀；剥完会变空（整条就一个时间）则原样返回。"""
    if not text:
        return text or ''
    for _ in range(max_rounds):
        for pat in _LEAD_TIME_PATTERNS:
            m = pat.match(text)
            if m:
                rest = text[m.end():]
                if rest.strip():
                    text = rest
                    break
                return text
        else:
            return text
    return text


_SENT_END = '。！？!?；…'
# 收尾符号：切句时要跟在句号后面，不能被甩到下一条句首
_CLOSING = '”’」』）)】》》'
_SOFT_SEP = '，,、 　'


def _split_sentences(s: str) -> list[str]:
    """按句末标点切；CQ 码与收尾引号整体保留。"""
    if not s:
        return []
    cq_spans = [m.span() for m in _CQ_RE.finditer(s)]
    out, start = [], 0
    i = 0
    while i < len(s):
        ch = s[i]
        in_cq = any(a <= i < b for a, b in cq_spans)
        if not in_cq and ch in _SENT_END:
            # 吞掉连续标点（！？…）与收尾引号/括号
            j = i + 1
            while j < len(s) and (s[j] in _SENT_END or s[j] in _CLOSING):
                j += 1
            out.append(s[start:j])
            start = j
            i = j
            continue
        i += 1
    if start < len(s):
        out.append(s[start:])
    return [x for x in out if x]


def _hard_split(s: str, max_len: int) -> list[str]:
    """超长句在逗号/空格处二次切；实在切不动才硬截。"""
    if len(s) <= max_len:
        return [s]
    out = []
    rest = s
    while len(rest) > max_len:
        window = rest[:max_len]
        cut = -1
        for i in range(max_len - 1, max_len // 2, -1):
            if window[i] in _SOFT_SEP:
                cut = i + 1
                break
        if cut <= 0:
            cut = max_len
        # CQ 段（如 [CQ:at,qq=...]）不可拆：切点落在段内时顺延到段尾，
        # 宁可该条略微超 max_len 也不能发出半个段
        for m in _CQ_RE.finditer(rest):
            if m.start() < cut < m.end():
                cut = m.end()
                break
        out.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        out.append(rest)
    return out


def split_reply(text: str, max_len: int = 100, min_len: int = 6) -> list[str]:
    """把长文本切成适合 QQ 逐条发送的句子列表。

    规则：代码块原子不拆；按句末标点切；过短句向后合并到 ≥min_len；
    超长句在逗号/空格二次切并受 max_len 硬顶；CQ 码原子。
    """
    text = (text or '').strip()
    if not text:
        return []

    # 1) 拆成 普通文本块 / 代码块（代码块原子）
    blocks: list[tuple[str, bool]] = []
    pos = 0
    for m in _FENCE_RE.finditer(text):
        if m.start() > pos:
            blocks.append((text[pos:m.start()], False))
        blocks.append((m.group(0), True))
        pos = m.end()
    if pos < len(text):
        tail = text[pos:]
        blocks.append((tail, True) if tail.lstrip().startswith('```') else (tail, False))

    # 2) 普通块先按换行、再按句切
    units: list[tuple[str, bool]] = []
    for chunk, atomic in blocks:
        if atomic:
            units.append((chunk.strip(), True))
            continue
        for line in chunk.split('\n'):
            line = line.strip()
            if line:
                for sent in _split_sentences(line):
                    units.append((sent, False))

    # 3) 过短句合并（原子块不参与合并）
    merged: list[tuple[str, bool]] = []
    buf = ''
    for chunk, atomic in units:
        if atomic:
            if buf:
                merged.append((buf, False))
                buf = ''
            merged.append((chunk, True))
            continue
        if not buf:
            buf = chunk
        elif len(buf) < min_len and len(buf) + len(chunk) <= max_len:
            buf += chunk
        else:
            merged.append((buf, False))
            buf = chunk
    if buf:
        merged.append((buf, False))

    # 4) 超长二次切
    result: list[str] = []
    for chunk, atomic in merged:
        if atomic or len(chunk) <= max_len:
            result.append(chunk)
        else:
            result.extend(_hard_split(chunk, max_len))
    return [c for c in result if c.strip()]


# [nospeak]…[/nospeak]：模型的内心戏，永不发送、不入历史
_NOSPEAK_CLOSED_RE = re.compile(
    r'\[\s*nospeak\s*\].*?\[\s*/\s*nospeak\s*\]', re.IGNORECASE | re.DOTALL)
_NOSPEAK_OPEN_RE = re.compile(r'\[\s*nospeak\s*\]', re.IGNORECASE)
_NOSPEAK_ANYTAG_RE = re.compile(r'\[\s*/?\s*nospeak\s*\]', re.IGNORECASE)


def split_nospeak(text: str) -> tuple[str, str]:
    """拆出 (要说的正文, [nospeak] 内的内心戏)。

    - 标签外内容才会发送；标签内多段用换行拼接到 hidden
    - 只有开标签没闭合时，开标签之后全部视为内心戏（容错模型漏写闭合）
    - 标签外为空 → 调用方本轮不发任何消息
    """
    text = text or ''
    hidden_parts = []
    for m in _NOSPEAK_CLOSED_RE.finditer(text):
        body = _NOSPEAK_ANYTAG_RE.split(m.group(0))
        # 整段形如 [nospeak] 内容 [/nospeak]，split 后中间即正文
        inner = body[1] if len(body) >= 3 else ''
        if inner.strip():
            hidden_parts.append(inner.strip())
    spoken = _NOSPEAK_CLOSED_RE.sub('', text)

    m = _NOSPEAK_OPEN_RE.search(spoken)
    if m:
        tail = spoken[m.end():]
        if tail.strip():
            hidden_parts.append(tail.strip())
        spoken = spoken[:m.start()]

    # 清掉残留的孤立闭合标签，压缩多余空行
    spoken = _NOSPEAK_ANYTAG_RE.sub('', spoken)
    spoken = re.sub(r'\n{3,}', '\n\n', spoken).strip()
    return spoken, '\n'.join(hidden_parts).strip()


def _next_delay(cfg: dict, prev_len: int, rng) -> float:
    dmin = max(0, int(cfg.get('split_delay_min_ms', 600)))
    dmax = max(dmin, int(cfg.get('split_delay_max_ms', 1800)))
    delay = rng.uniform(dmin, dmax) / 1000.0
    # 上一条越长，"打字"越久：每 100 字最多再加 0.4s
    delay += min(prev_len, 100) / 100.0 * 0.4
    return delay


async def send_in_chunks(bot, target_id, text: str, cfg: dict,
                         rng=None, message_type: str = 'group',
                         name_map: dict | None = None,
                         quote_id: str = '') -> int:
    """按配置分条发送，返回实际发出的条数。群消息全程应在群锁内调用。

    name_map 非空时，先把文本里的 @昵称 换成真 CQ:at 段再切条
    （CQ 码在 split_reply 里是原子，不会被切散）；调用方落库仍用原文。
    quote_id 非空时，第一条前面拼 [CQ:reply,id=…]（引用触发消息）。
    """
    if not (text or '').strip():
        return 0
    rng = rng or random
    # 防御性净化：任何调用方漏净化，模型复读的 CQ 码都不能原样出群
    # （必须在 render_ats 之前——后者自己生成的真 CQ:at 是合法的）
    text = sanitize_model_text(text)
    if name_map and '@' in text:
        from .mentions import render_ats
        text = render_ats(text, name_map)
    if cfg.get('split_reply', True):
        chunks = split_reply(
            text,
            max_len=int(cfg.get('split_max_len', 100)),
            min_len=int(cfg.get('split_min_len', 6)),
        )
    else:
        chunks = [text.strip()]
    if not chunks:
        return 0
    prefix = reply_prefix(quote_id)
    if prefix:
        chunks[0] = prefix + chunks[0]

    sent = 0
    for i, chunk in enumerate(chunks):
        await bot.send_message(message=chunk, target_id=target_id,
                               message_type=message_type)
        sent += 1
        if i + 1 < len(chunks):
            await asyncio.sleep(_next_delay(cfg, len(chunk), rng))
    return sent
