"""启发式粗筛（纯本地、0 token）。

命中任一：称呼词（全局/群别名）、疑问语气、呼唤语。
@我/回复我在更上层直接判 forced，不经过这里。
"""
from __future__ import annotations

import re

# 句尾/句中疑问标记
_QUESTION_MARKS = ('?', '？')
# 常见呼唤前缀/词（短，宁少勿滥，避免灌水误触发）
_HINTS = ('请问', '问一下', '你知道', '你觉得', '你认为', '帮我', '帮忙',
          '告诉', '解释', '为什么', '怎么', '如何', '吗', '嘛')

# 粗筛前剥掉的噪声：CQ 码（图片/转发 id）与 URL（卡片链接里的 ?/怎么 会误触发）
_CQ_RE = re.compile(r'\[CQ:[^\]]*\]')
_URL_RE = re.compile(r'https?://\S+|www\.\S+', re.I)


def _normalize(s: str) -> str:
    return (s or '').strip().lower()


def strip_for_heuristic(text: str) -> str:
    """剥掉 CQ 码与链接后的纯文本（粗筛专用，不改入库内容）。"""
    t = _CQ_RE.sub(' ', text or '')
    t = _URL_RE.sub(' ', t)
    return ' '.join(t.split())


def heuristic_hit(text: str, aliases: list[str], bot_names: list[str] | None = None) -> bool:
    """纯启发式：文本命中称呼词/疑问/呼唤词之一即 True。"""
    t = strip_for_heuristic(text)
    if not t:
        return False
    low = _normalize(t)

    for alias in (*(aliases or []), *(bot_names or [])):
        a = _normalize(alias)
        if a and a in low:
            return True

    if t.endswith(_QUESTION_MARKS):
        return True
    if any(h in t for h in _HINTS):
        return True
    return False


def should_use_judge(parsed_forced: bool, cfg: dict) -> bool:
    """是否需要走判官：非强制触发 且 配置启用判官。"""
    return (not parsed_forced) and bool(cfg.get('judge_on', True))
