"""群体说话风格学习：攒够语料 → 便宜调用产出风格画像 → 注入 system prompt。

与记忆模块同构（fire-and-forget、每群锁重入跳过、游标推进），
但语料来自 raw_messages（全量群聊），不是触发后的对话。
本模块不 import openai（LLMClient 注入）。
"""
from __future__ import annotations

import asyncio
import re

from . import storage
from .context import script_clean

_URL_RE = re.compile(r'https?://\S+|www\.\S+', re.I)


def clean_raw_content(text: str) -> str:
    """raw 语料入风格学习前的轻清洗：富消息码转占位、剥链接、折叠空白。"""
    t = script_clean(text or '')
    t = _URL_RE.sub(' ', t)
    return ' '.join(t.split())


class StyleManager:
    def __init__(self, db_path: str, log=None):
        self.db_path = db_path
        self._log = log or (lambda msg: None)
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set = set()
        self._cache: dict[str, str] = {}

    def invalidate(self, group_id=None):
        if group_id is None:
            self._cache.clear()
        else:
            self._cache.pop(str(group_id), None)

    def get_text(self, group_id) -> str:
        gid = str(group_id)
        if gid in self._cache:
            return self._cache[gid]
        row = storage.get_style(self.db_path, gid)
        text = (row or {}).get('style_text') or ''
        self._cache[gid] = text
        return text

    def _lock(self, group_id: str) -> asyncio.Lock:
        lk = self._locks.get(group_id)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[group_id] = lk
        return lk

    async def force_build(self, group_id, cfg, llm):
        """后台「立即学习」：无视语料门槛，直接重建。"""
        if not cfg.get('learn_style_on', True):
            return
        await self._build(str(group_id), cfg, llm, force=True)

    def schedule_check(self, group_id, cfg, llm):
        """fire-and-forget 调度；同群重入直接跳过，异常静默。返回 task（可空）。"""
        if not cfg.get('learn_style_on', True):
            return None
        task = asyncio.create_task(self._build(str(group_id), cfg, llm))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _build(self, group_id: str, cfg: dict, llm, force: bool = False):
        every = max(1, int(cfg.get('style_rebuild_every', 200)))
        lk = self._lock(group_id)
        if lk.locked():
            return
        async with lk:
            try:
                row = storage.get_style(self.db_path, group_id)
                up_to = (row or {}).get('up_to_id', 0)
                n_new = storage.count_raw_after(self.db_path, group_id, up_to)
                if not force and n_new < every:
                    return
                rows = storage.raw_after(
                    self.db_path, group_id, up_to,
                    int(cfg.get('style_sample_msgs', 300)))
                if not rows:
                    return
                lines = []
                for r in rows:
                    name = r.get('sender_name') or r.get('sender_id') or '群友'
                    content = clean_raw_content(r['content'])
                    if content:
                        lines.append(f'{name}: {content}')
                if not lines:
                    return
                script = '\n'.join(lines)
                text, usage = await llm.build_style(cfg, script)
                if not text:
                    return
                storage.add_usage(self.db_path, {
                    'group_id': group_id, 'kind': 'style',
                    'tokens_in': usage.get('tokens_in', 0),
                    'cache_hit': usage.get('cache_hit', 0),
                    'tokens_out': usage.get('tokens_out', 0), 'ok': True})
                storage.set_style(self.db_path, group_id, text,
                                  max(r['id'] for r in rows), len(rows))
                self._cache[group_id] = text
                self._log(f'风格画像已更新（{len(rows)} 条语料）')
            except Exception as ex:
                self._log(f'风格学习失败（不影响回复）: {ex}')
