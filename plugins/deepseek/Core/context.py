"""滑窗裁剪、消息渲染、人设哈希——全部纯函数。"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import time

# 每轮易变块（自我记忆/本群身份/背景/心情/时钟/梗指南）统一装进这一条
# system 消息，放在对话历史之后：前缀（系统提示词→摘要→历史）保持稳定，
# 每轮只让尾部一小段重新计费。
LIVE_CTX_HEADER = '【本轮实时上下文】'


def time_tag(ts, now=None) -> str:
    """消息行首时间标签 [MM-DD HH:MM]（跨年带年份）。无/非法时间戳返回 ''。"""
    if not ts:
        return ''
    try:
        dt = datetime.datetime.fromtimestamp(float(ts))
    except (TypeError, ValueError, OSError, OverflowError):
        return ''
    now = now or datetime.datetime.now()
    fmt = '%Y-%m-%d %H:%M' if dt.year != now.year else '%m-%d %H:%M'
    return '[' + dt.strftime(fmt) + '] '


def persona_hash(text: str) -> str:
    return hashlib.sha1((text or '').strip().encode('utf-8')).hexdigest()[:12]


def _content_len(row: dict) -> int:
    n = len(row.get('content') or '')
    if row.get('tool_calls'):
        try:
            n += sum(len(json.dumps(tc, ensure_ascii=False))
                     for tc in json.loads(row['tool_calls']))
        except (json.JSONDecodeError, TypeError):
            pass
    return n


def _assistant_tool_ids(row: dict) -> set:
    if row.get('role') != 'assistant' or not row.get('tool_calls'):
        return set()
    try:
        return {tc.get('id') for tc in json.loads(row['tool_calls']) if tc.get('id')}
    except (json.JSONDecodeError, TypeError):
        return set()


def select_window(rows: list, rounds: int, char_budget: int) -> list:
    """从按 id 正序的消息里选滑窗：先按条数（一轮≈user+assistant 两条），
    再按字符预算从最旧处丢弃；丢弃边界必须切在完整的工具调用链之前。"""
    if not rows:
        return []
    hard_limit = max(2, int(rounds) * 2 + 8)
    win = rows[-hard_limit:]

    # 字符预算：从最旧开始丢（工具行不能打头、assistant.tool_calls 必须带齐 tool 结果）
    while win and sum(_content_len(r) for r in win) > char_budget and len(win) > 1:
        win = win[1:]
        win = _trim_orphans(win)
    return _trim_orphans(win)


def _trim_orphans(win: list) -> list:
    """切掉窗口开头不合法的 tool 行 / 工具结果不全的 assistant 行。"""
    while win:
        first = win[0]
        if first.get('role') == 'tool':
            win = win[1:]
            continue
        ids = _assistant_tool_ids(first)
        if ids:
            present = set()
            for r in win[1:]:
                if r.get('role') == 'assistant':
                    break
                if r.get('role') == 'tool':
                    present.add(r.get('tool_call_id'))
            if not ids.issubset(present):
                # 丢掉该 assistant 及其残缺 tool 行
                win = win[1:]
                while win and win[0].get('role') == 'tool':
                    win = win[1:]
                continue
        break
    return win


# 后台触发在 messages.name 上的来源标（免迁移复用现有列）。
# 这些「系统通知 user 行」装的是给后台轮看的长指令，绝不能原样进前台上下文，
# 只渲染成一句中性的 system 说明；静默轮（后面没有 assistant 发言）整行丢弃。
BG_NOTICE_NAME = {
    'wake-notice': '定时苏醒',
    'join-notice': '入群通知',
    'trigger-notice': '外部触发',
    'speakup-notice': '主动搭话',
    'loop-notice': '长跑通知',
}
# 兼容旧数据：name 列没打标时，按内容头识别
BG_NOTICE_PREFIX = ('[定时苏醒]', '[系统通知]', '[外部触发]', '[主动搭话]',
                    '[长跑任务通知]', '[长跑续跑通知]')


def _bg_notice(row: dict) -> str:
    """后台通知行返回其种类名，普通行返回 ''。"""
    name = (row.get('name') or '').strip()
    if name in BG_NOTICE_NAME:
        return BG_NOTICE_NAME[name]
    if row.get('role') == 'user' and not (row.get('sender_id') or ''):
        head = (row.get('content') or '').lstrip()
        if head.startswith('[定时苏醒]'):
            return BG_NOTICE_NAME['wake-notice']
        if head.startswith('[系统通知]'):
            return BG_NOTICE_NAME['join-notice']
        if head.startswith('[外部触发]'):
            return BG_NOTICE_NAME['trigger-notice']
        if head.startswith('[主动搭话]'):
            return BG_NOTICE_NAME['speakup-notice']
        if (head.startswith('[长跑任务通知]')
                or head.startswith('[长跑续跑通知]')):
            return BG_NOTICE_NAME['loop-notice']
    return ''


def _row_has_following_speech(rows: list, i: int) -> bool:
    """后台通知后面是否紧跟着 assistant 的实质发言（静默轮没有）。"""
    for r in rows[i + 1:]:
        role = r.get('role')
        if role == 'assistant':
            # 工具轮 assistant 可能 content 为空（后面跟 tool 行），继续找
            if (r.get('content') or '').strip():
                return True
            continue
        if role == 'user':
            return False
    return False


def speaker_label(row: dict) -> str:
    """说话人展示名：昵称后挂 QQ 号（昵称会重名/会改，QQ 是唯一身份）。"""
    qq = str(row.get('sender_id') or '').strip()
    name = (row.get('sender_name') or '').strip()
    if qq and name and name != qq:
        return f'{name}(QQ:{qq})'
    return name or (qq or '群友')


def render_openai(rows: list, raw_bg_notice: bool = False) -> list:
    """DB 行 → OpenAI chat messages（用户行带 `昵称(QQ:号): ` 前缀）。

    后台触发（定时苏醒/入群通知/定时任务）的内部提示词不进前台上下文：
    通知 user 行只在随后确有 assistant 发言时渲染成一句中性 system 说明，
    静默轮整行丢弃；定时任务主动发的 assistant 行前面补 system 出处说明。
    raw_bg_notice=True（后台轮自己渲染时）则把通知原文当普通 user 行给出。
    """
    out = []
    for i, r in enumerate(rows):
        role = r.get('role')
        tag = time_tag(r.get('created_at'))
        if role == 'user':
            bg = _bg_notice(r)
            if bg and not raw_bg_notice:
                if not _row_has_following_speech(rows, i):
                    continue
                out.append({'role': 'system',
                            'content': f'{tag}（{bg}触发，下面是你当时主动说的话，'
                                       '不是群友发的消息）'})
                continue
            out.append({'role': 'user',
                        'content': f'{tag}{speaker_label(r)}: {r.get("content", "")}'})
        elif role == 'assistant':
            bg = (r.get('name') or '') == 'scheduled'
            if bg and (not out or out[-1]['role'] != 'user'):
                out.append({'role': 'system',
                            'content': f'{tag}（定时任务触发，下面是你按预先登记的'
                                       '任务主动发出的消息，不是回复任何人）'})
            # 她自己的历史回复不加行首时间标签：那会成为每轮的"输出示范"，
            # 导致她照抄时间戳前缀（用户可见的发送时间由 QQ 自己盖）
            content = r.get('content') or None
            msg = {'role': 'assistant', 'content': content}
            if r.get('tool_calls'):
                try:
                    tcs = json.loads(r['tool_calls'])
                    if tcs:
                        msg['tool_calls'] = tcs
                except (json.JSONDecodeError, TypeError):
                    pass
            if msg['content'] is None and 'tool_calls' not in msg:
                continue
            out.append(msg)
        elif role == 'tool':
            out.append({'role': 'tool',
                        'tool_call_id': r.get('tool_call_id') or '',
                        'content': r.get('content', '')[:2000]})
    return out


# 正文里可操作的富消息码进判官/摘要语料前先退化成人话（省 token、防链接误导）
_CQ_CODE_RE = re.compile(r'\[CQ:[^\]]*\]')
_CARD_URL_RE = re.compile(r'\[卡片\]\s*(.*?)\s*https?://\S+')


def script_clean(text: str) -> str:
    """对话窗富文本 → 判官/摘要/记忆语料用的轻量文本。

    图片码 → [图片]，转发码 → [合并转发]，卡片去掉链接只留标题。
    render_openai（对话窗/历史查询结果）保留可操作码，不用本函数。
    """
    if not text:
        return text or ''

    def _cq(m):
        s = m.group(0)
        if s.startswith('[CQ:forward'):
            return '[合并转发]'
        if s.startswith('[CQ:image'):
            return '[图片]'
        return ''

    t = _CQ_CODE_RE.sub(_cq, text)
    t = _CARD_URL_RE.sub(
        lambda m: ('[卡片] ' + m.group(1).strip()) if m.group(1).strip()
        else '[卡片]', t)
    return t


def render_script(rows: list) -> str:
    """摘要/记忆抽取用的对话脚本：`昵称: 内容`，跳过工具行。

    后台内部通知不进脚本（长指令与记忆无关）；定时任务主动发言标注出处。
    """
    lines = []
    for i, r in enumerate(rows):
        role = r.get('role')
        tag = time_tag(r.get('created_at'))
        if role == 'user':
            bg = _bg_notice(r)
            if bg:
                # 静默轮无下文就完全跳过；有发言只留一行中性占位
                if _row_has_following_speech(rows, i):
                    lines.append(f'{tag}（{bg}，助手主动发言）')
                continue
            lines.append(
                f'{tag}{speaker_label(r)}: {script_clean(r.get("content", ""))}')
        elif role == 'assistant' and r.get('content'):
            prefix = '助手[定时任务]' if (r.get('name') or '') == 'scheduled' else '助手'
            lines.append(f'{tag}{prefix}: {script_clean(r["content"])}')
    return '\n'.join(lines)


# ── 触发前背景闲聊注入（仅前台对话轮）──────────────────────────

AMBIENT_HEADER = (
    '【群内背景消息】以下是本轮对话触发前群里最近的群友闲聊，你全程没参与、'
    '没人 @ 你、也不是回复你的消息。它们只用于帮你理解当前气氛与话题上下文：'
    '不要主动回答这些消息里的问题、不要逐条回应、也不要自己提起它们；'
    '仅当群友本轮消息与此直接相关时，才自然地利用其中信息。')


def render_ambient(rows: list, n: int = 8, char_cap: int = 1200,
                   header: str = AMBIENT_HEADER) -> str:
    """raw_messages 行 → 一条 system 背景块；无实质内容返回 ''。

    超字符预算时从最旧行丢（保留离触发最近的）；行格式与对话窗一致。
    header 可由提示词覆盖注入（prompts.py）。
    """
    rows = [r for r in (rows or []) if (r.get('content') or '').strip()]
    rows = rows[-max(1, int(n)):]
    if not rows:
        return ''
    lines, used = [], 0
    for r in reversed(rows):
        line = (f"{time_tag(r.get('created_at'))}"
                f"{speaker_label(r)}: {(r.get('content') or '').strip()}")
        if lines and used + len(line) + 1 > char_cap:
            break
        lines.append(line)
        used += len(line) + 1
    lines.reverse()
    return header + '\n' + '\n'.join(lines)



def need_summary(overflow_rows: list) -> bool:
    """窗口外有未摘要的实质消息才调摘要（省 token）。"""
    return any((r.get('role') in ('user', 'assistant') and r.get('content'))
               for r in overflow_rows)


def plan_summary(rows: list, rounds: int, char_budget: int, *,
                 min_new_msgs: int = 12, min_new_chars: int = 2000,
                 force_msgs: int = 48, force_chars: int = 12000,
                 min_interval_sec: int = 600, last_updated_at: float = 0,
                 now: float | None = None, force: bool = False) -> dict:
    """锚定窗口下判断是否把积压滚进摘要（纯函数）。

    rows 是摘要锚点之后的全部消息（正序，含 tool 行）。窗口不再每轮滑动：
    摘要不改写时历史只追加，供应商的前缀缓存才能跨轮命中；只有摘要真正
    重写（锚点前移）才是计划内的前缀断点，因此重写要节流。

    返回 {'due': 是否摘要, 'cut': rows[:cut] 并入摘要, 'window': 本轮历史}。
    due=False 时 window=rows（只追加）；触发但摘要失败时调用方可退回
    window=select_window(...) 兜底，避免提示词无限增长。
    """
    if not rows:
        return {'due': False, 'cut': 0, 'window': []}
    tail = select_window(rows, rounds, char_budget)
    cut = len(rows) - len(tail)   # select_window 结果恒为 rows 的后缀
    if cut <= 0:
        return {'due': False, 'cut': 0, 'window': rows}
    overflow = rows[:cut]
    if not need_summary(overflow):
        # 积压里只有工具行/空内容，不值得调摘要；但仍要防前缀无限增长
        if len(overflow) >= force_msgs:
            return {'due': False, 'cut': 0, 'window': tail}
        return {'due': False, 'cut': 0, 'window': rows}
    n = len(overflow)
    chars = sum(_content_len(r) for r in overflow)
    now = now if now is not None else time.time()
    forced = force or n >= force_msgs or chars >= force_chars
    elapsed = now - float(last_updated_at or 0)
    soft = ((n >= min_new_msgs or chars >= min_new_chars)
            and elapsed >= min_interval_sec)
    due = forced or soft
    return {'due': due, 'cut': cut if due else 0,
            'window': tail if due else rows}


def render_injections(rows: list, max_chars: int = 1500) -> str:
    """把启用的自我注入条目渲染成系统提示词块；超预算从最旧/最后的丢弃。

    入参顺序即生效顺序（DAL 已排：全局在前、本群在后）。
    """
    if not rows:
        return ''
    head = ('【你的自我提示词注入表】（以下都是你过去用 inject_add 给自己写下的'
            '经验与资源索引，每轮对话自动注入，按此行事；过时条目用 inject_list '
            '查编号、inject_toggle 停用或 inject_remove 删除）')
    blocks, used = [], len(head)
    for i, r in enumerate(rows, 1):
        title = (r.get('title') or '').strip() or '未命名'
        text = (r.get('text') or '').strip()
        if not text:
            continue
        scope = '全局' if r.get('global') else '本群'
        block = f'[{i}]（{scope}）{title}\n{text}'
        if used + len(block) + 1 > max_chars:
            break
        blocks.append(block)
        used += len(block) + 1
    if not blocks:
        return ''
    return head + '\n' + '\n'.join(blocks)
