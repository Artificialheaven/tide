# -*- coding: utf-8 -*-
"""运行环境探测：框架根 config.json 的接入方式等全局事实。

为什么单独一个模块：插件（plugins/deepseek）与框架（core/）是两个包，插件
拿不到框架的 configs 模块；但有些能力是"当前接线下能不能用"级别的判断
（例如 QQ 空间只在 NapCat 通道存在，official 开放平台没有对应接口），
必须读仓库根 config.json 的 `transport`。这里按 mtime 缓存，配置改了
不用重启也能生效。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT_CONFIG = Path(__file__).resolve().parents[3] / 'config.json'
_cache = {'mtime': -1.0, 'data': {}}


def root_config() -> dict:
    """读框架根 config.json（按 mtime 缓存），缺失/损坏时返回 {}。"""
    try:
        st = ROOT_CONFIG.stat()
        if st.st_mtime != _cache['mtime']:
            data = json.loads(ROOT_CONFIG.read_text(encoding='utf-8'))
            _cache['data'] = data if isinstance(data, dict) else {}
            _cache['mtime'] = st.st_mtime
    except (OSError, ValueError):
        return {}
    return _cache['data']


def reset_cache() -> None:
    """测试用：丢掉缓存，下一次 root_config 重新读盘。"""
    _cache['mtime'] = -1.0
    _cache['data'] = {}


def transport() -> str:
    """接入方式：napcat（OneBot v11 WS）| official（QQ 开放平台 v2）。"""
    return str(root_config().get('transport') or 'napcat').strip().lower()


def is_official() -> bool:
    return transport() == 'official'


def unavailable_reason(feature: str = '该功能') -> str:
    """按接入方式返回不可用原因；返回空串表示当前接入方式支持它。"""
    if is_official():
        return (f'{feature}走的是 NapCat 私有 OneBot 扩展，当前 '
                'transport=official（QQ 官方开放平台）没有对应事件/接口')
    return ''
