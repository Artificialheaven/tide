# -*- coding: utf-8 -*-
"""兼容入口：脚本已迁到仓库根 scripts/meme_import_bili.py（可独立运行）。

这里只做转发，避免出现两份实现。新脚本不依赖框架/插件，API Key 从
--api-key 或环境变量读取；用法见仓库根脚本开头的说明。
"""
import os
import runpy

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_TARGET = os.path.join(_REPO_ROOT, 'scripts', 'meme_import_bili.py')

if not os.path.exists(_TARGET):
    raise SystemExit(f'找不到 {_TARGET}（脚本可能已被移动）')

runpy.run_path(_TARGET, run_name='__main__')
