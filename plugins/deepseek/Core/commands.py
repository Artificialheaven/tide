"""/ai 主人指令——纯解析逻辑（副作用由插件层执行，便于单测）。

支持：/ai 开启|关闭|清空记忆|状态|人设 <一行>|别名 add|del <词>
      /ai 思考 开|关|继承|/ai 忘记 <QQ或@> |/ai 工具
"""
from __future__ import annotations

import re

# 动词归一：词表 → 规范动作
_VERB_ALIASES = {
    '开启': 'enable', '打开': 'enable', '启用': 'enable', 'on': 'enable',
    'open': 'enable',
    '关闭': 'disable', '关': 'disable', '停用': 'disable', 'off': 'disable',
    'stop': 'disable',
    '清空记忆': 'reset', '重置记忆': 'reset', '清空': 'reset', 'reset': 'reset',
    '状态': 'status', 'status': 'status', '情况': 'status',
    '人设': 'persona', '人格': 'persona', 'persona': 'persona',
    '别名': 'alias', '称呼': 'alias', 'alias': 'alias',
    '思考': 'thinking', '深度思考': 'thinking', 'thinking': 'thinking',
    '忘记': 'forget', '遗忘': 'forget', 'forget': 'forget',
    '工具': 'tools', 'tools': 'tools',
    '待办': 'tasklist', '任务': 'tasklist', '约定': 'tasklist', 'todo': 'tasklist',
    '定时': 'jobs', '定时任务': 'jobs', '日程': 'jobs', 'jobs': 'jobs',
    '风格': 'style', '学说话': 'style', 'style': 'style',
    '循环': 'loop', '长跑': 'loop', 'loop': 'loop',
    '帮助': 'help', 'help': 'help',
}

_LOOP_STOP_WORDS = {'关', '关闭', '停', '停止', '停掉', '结束', 'stop', 'off', 'end'}
_LOOP_STATUS_WORDS = {'状态', '情况', 'status', 'stat'}
_LOOP_APPROVE_WORDS = ('同意', '批准', '允许', 'approve', 'yes', 'y')
_LOOP_REJECT_WORDS = ('拒绝', '不同意', '不批准', 'reject', 'deny', 'no')


def parse_loop_arg(arg: str) -> tuple[str, str]:
    """/ai loop 的子动作解析（纯函数）。

    返回 (sub, payload)，sub ∈ start/stop/status/approve/reject：
    - 空参数              → status
    - 关/停/stop…          → stop
    - 状态/status         → status
    - 同意/批准 [#]N        → approve，payload=编号
    - 拒绝/不同意 [#]N [理由] → reject，payload="编号 理由"
    - 其余非空文本          → start，payload=任务文本
    """
    t = (arg or '').strip()
    if not t:
        return ('status', '')
    head, _, rest = t.partition(' ')
    low = head.lower().lstrip('#＃')
    rest = rest.strip()
    if low in _LOOP_STOP_WORDS:
        return ('stop', '')
    if low in _LOOP_STATUS_WORDS:
        return ('status', '')
    for w in _LOOP_APPROVE_WORDS:
        if low == w:
            m = re.search(r'\d+', rest or t)
            return ('approve', m.group(0) if m else (rest or ''))
    for w in _LOOP_REJECT_WORDS:
        if low == w:
            m = re.search(r'\d+', rest or t)
            if m:
                tail = (rest or t)[m.end():].strip(' ，,：:')
                return ('reject', f'{m.group(0)} {tail}'.strip())
            return ('reject', rest)
    return ('start', t)

_BOOL_WORDS = {
    '开': True, '开启': True, '打开': True, 'on': True, 'true': True, '是': True,
    '关': False, '关闭': False, 'off': False, 'false': False, '否': False,
    '继承': None,
}

_AT_QQ_RE = re.compile(r'\[CQ:at,qq=(\d+|all)\]')


def is_owner(sender_id, owner_qq) -> bool:
    return str(sender_id).strip() == str(owner_qq).strip()


def is_private_allowed(cfg: dict, uid) -> bool:
    """私聊门禁：主人恒允许，其余看 private_allowed_qqs（逗号/空白分隔）。"""
    uid = str(uid).strip()
    if is_owner(uid, cfg.get('owner_qq', '')):
        return True
    raw = str(cfg.get('private_allowed_qqs', '') or '')
    allowed = {t.strip() for t in re.split(r'[,\s，、]+', raw) if t.strip()}
    return uid in allowed


_INTERRUPT_VERBS = ('打断一下', '插一句', '插话', '打断')


def parse_interrupt(text: str, prefix: str):
    """`/ai 打断 <指示>` 解析（任何群成员可用，独立于主人指令门禁）。

    命中动词：返回指示正文（可能为 ''，表示没带内容 → 回用法提示）；
    不是打断指令：返回 None。动词按长度优先匹配（打断/打断一下）。
    """
    t = (text or '').strip()
    p = (prefix or '/ai').strip()
    if not t.startswith(p):
        return None
    body = t[len(p):].strip()
    for verb in _INTERRUPT_VERBS:
        if body == verb:
            return ''
        if body.startswith(verb):
            rest = body[len(verb):]
            if rest[:1] in (' ', ' ', '　'):
                return rest.strip()
    return None


def parse_command(text: str, prefix: str) -> tuple[str, str] | None:
    """命中前缀返回 (规范动作, 参数字符串)，否则 None。"""
    t = (text or '').strip()
    p = (prefix or '/ai').strip()
    if not t.startswith(p):
        return None
    body = t[len(p):].strip()
    if not body:
        return ('help', '')
    verb, _, arg = body.partition(' ')
    verb = verb.lower()
    action = _VERB_ALIASES.get(verb)
    if action is None:
        return None
    return (action, arg.strip())


def parse_thinking_arg(arg: str):
    """思考开关参数 → True/False/None(继承)；无法识别返回 Ellipsis。"""
    key = (arg or '').strip().lower()
    if key in _BOOL_WORDS:
        return _BOOL_WORDS[key]
    return Ellipsis


def extract_target_qq(arg: str) -> str:
    """`/ai 忘记` 参数里的目标 QQ：支持 @段 与纯数字。"""
    m = _AT_QQ_RE.search(arg or '')
    if m and m.group(1) != 'all':
        return m.group(1)
    m2 = re.search(r'\d{5,}', arg or '')
    return m2.group(0) if m2 else ''


def parse_alias_arg(arg: str) -> tuple[str, str] | None:
    """`别名 add|del <词>` → (op, 词)。"""
    op, _, word = (arg or '').partition(' ')
    op = op.strip().lower()
    word = word.strip()
    if op in ('add', 'del', '添加', '删除') and word:
        op = 'add' if op in ('add', '添加') else 'del'
        return (op, word)
    return None
