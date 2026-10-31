# -*- coding: utf-8 -*-
"""DeepSeek DSML 工具调用标记的兜底解析。

DSML（<｜｜DSML｜｜ invoke ...>）是 deepseek 系模型原生的工具调用格式，
正常情况下服务端会把它解析成 OpenAI 协议的 tool_calls 字段；但在标记轻微
畸形/生成被截断等情况下会原样泄漏到正文 content 里。不做兜底时，这坨标签
会被当成普通回复直接发到群里。

本模块只做纯文本解析，不依赖 openai/mcp，方便单测。
"""
from __future__ import annotations

import html
import json
import re

# 全角 ｜｜（U+FF5C）为模型实际输出，兼容半角 ||
_BAR = r'[｜|]{2}'
MARKER_RE = re.compile(_BAR + r'DSML' + _BAR)

_INVOKE_RE = re.compile(
    r'<' + _BAR + r'DSML' + _BAR + r'\s*invoke([^>]*)>'
    r'(.*?)'
    r'</' + _BAR + r'DSML' + _BAR + r'\s*invoke[^>]*>',
    re.IGNORECASE | re.DOTALL)
_PARAM_RE = re.compile(
    r'<' + _BAR + r'DSML' + _BAR + r'\s*parameter([^>]*)>'
    r'(.*?)'
    r'</' + _BAR + r'DSML' + _BAR + r'\s*parameter[^>]*>',
    re.IGNORECASE | re.DOTALL)
# 残留标签（未闭合块、calls 包裹等）：标签 + 同一行尾巴一起切掉，
# 避免半截参数值（如截断的 "1600"）被当成正文发进群
_RESIDUAL_RE = re.compile(
    r'</?' + _BAR + r'DSML' + _BAR + r'[^>]*>[^\n]*',
    re.IGNORECASE)
_NAME_RE = re.compile(r'name\s*=\s*"([^"]+)"')
_STRING_RE = re.compile(r'string\s*=\s*"(true|false)"', re.IGNORECASE)


def _convert_value(raw: str, is_string: bool):
    if is_string:
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw  # 解析不了就按字符串透传给工具，工具自行报错


def _parse_invoke(attrs: str, body: str) -> dict | None:
    m = _NAME_RE.search(attrs or '')
    if not m:
        return None
    name = m.group(1).strip()
    if not name:
        return None

    arguments: dict = {}
    for pattrs, pbody in _PARAM_RE.findall(body):
        pm = _NAME_RE.search(pattrs)
        if not pm:
            continue
        pname = pm.group(1).strip()
        sm = _STRING_RE.search(pattrs)
        is_string = bool(sm and sm.group(1).lower() == 'true')
        arguments[pname] = _convert_value(pbody.strip(), is_string)

    if not arguments:
        # 个别变体把整个参数 JSON 直接放在 invoke 体里
        stripped = body.strip()
        if stripped.startswith('{'):
            try:
                data = json.loads(stripped)
                if isinstance(data, dict):
                    arguments = data
            except json.JSONDecodeError:
                pass

    return {'id': '', 'name': name, 'arguments': arguments}


def parse_dsml(content: str) -> tuple:
    """把正文里泄漏的 DSML 调用还原成工具调用。

    返回 (tool_calls, spoken, found)：
    - tool_calls: [{'id','name','arguments'}]，id 形如 dsml-1
    - spoken: 剥掉所有 DSML 标签/调用块后的剩余正文（模型的口语部分）
    - found: 正文里是否出现过 DSML 标记（哪怕畸形）
    """
    if not content or not MARKER_RE.search(content):
        return [], content or '', False

    text = html.unescape(content)
    tool_calls = []

    def _take(m):
        parsed = _parse_invoke(m.group(1), m.group(2))
        if parsed:
            parsed['id'] = f'dsml-{len(tool_calls) + 1}'
            tool_calls.append(parsed)
        return ''

    text = _INVOKE_RE.sub(_take, text)
    # 残片（未闭合的块、calls 包裹标签等）全部剥掉，绝不允许发进群
    text = _RESIDUAL_RE.sub('', text)
    spoken = '\n'.join(line.strip() for line in text.splitlines()
                       if line.strip())
    return tool_calls, spoken, True
