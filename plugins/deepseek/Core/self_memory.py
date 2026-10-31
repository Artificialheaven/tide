"""全局第一人称自我记忆：关于「我」的经历/承诺/关系，跨所有群与私聊同一份。

与 memory.py（按 QQ 用户的群友画像）互补：
- 无 user_id：全库一张表、一个 BM25 索引；
- 复用 memory 的 tokenize / BM25 / similarity 与三档合并；
- 写配额按天（就地改写不耗配额），工具写与抽取写共用；
- 召回块是独立 system 消息（动态内容不进前缀缓存的静态人设）。
本模块不 import openai。
"""
from __future__ import annotations

import asyncio
import math
import time

from . import storage
from .memory import BM25, similarity, tokenize

def _block_header(store=None) -> str:
    # 默认文案在 prompts.py 登记（Web「提示词」页可覆盖）
    from . import prompts
    return prompts.render('self_memory_header', store)


def _local_day(now: float | None = None) -> str:
    return time.strftime('%Y-%m-%d', time.localtime(
        now if now is not None else time.time()))


def merge_self(db_path: str, text: str, tags: str, importance: float,
               op: str, source_group_id: str = '', *,
               source: str = '', context: str = '') -> tuple:
    """三档合并（同 per-user 记忆口径），返回 (记忆 id, 是否新建)。"""
    entries = storage.list_active_self_memories(db_path)
    best, best_ratio = None, 0.0
    for e in entries:
        r = similarity(text, e['text'])
        if r > best_ratio:
            best, best_ratio = e, r
    if best is not None and (
            best_ratio >= 0.8 or (op == 'update' and best_ratio >= 0.5)):
        storage.update_self_memory_full(
            db_path, best['id'], text, tags or best.get('tags', ''),
            max(importance, best['importance']), best.get('history', []),
            trace_group_id=str(source_group_id or ''),
            trace_context=context or '', trace_source=source or '')
        return best['id'], False
    if best is not None and best_ratio >= 0.5:
        storage.update_self_memory(
            db_path, best['id'], superseded=True,
            trace={'kind': 'supersede', 'group_id': source_group_id,
                   'source': source, 'context': context,
                   'seen_text': best.get('text', '')})
    mid = storage.add_self_memory(
        db_path, text, tags=tags, importance=importance,
        created_by_group=str(source_group_id))
    return mid, True


class SelfMemoryManager:
    def __init__(self, db_path: str, log=None, prompts=None):
        self.db_path = db_path
        self._log = log or (lambda msg, level='info': None)
        self.prompts = prompts
        self._cache: tuple | None = None

    def invalidate(self):
        self._cache = None

    # ── 召回 ────────────────────────────────────────────────────

    def _index(self):
        if self._cache is not None:
            return self._cache
        entries = storage.list_active_self_memories(self.db_path)
        bm = BM25([tokenize(e['text']) for e in entries])
        self._cache = (entries, bm)
        return entries, bm

    def _rank_scored(self, query: str, topk: int,
                     allow_fallback: bool = False) -> list:
        """返回 [(entry, fused 融合分 0~1), ...]（相关 .75 / 重要 .15 / 新近 .10）。"""
        entries, bm = self._index()
        if not entries:
            return []
        now = time.time()
        scored = bm.score(tokenize(query))
        if scored:
            max_s = scored[0][1] or 1.0
            ranked = []
            for rank, (i, s) in enumerate(scored):
                e = entries[i]
                recency = math.exp(
                    -(now - (e['updated_at'] or 0)) / 86400 / 60)
                fused = ((s / max_s) * 0.75 + e['importance'] * 0.15
                         + recency * 0.10)
                ranked.append((fused, rank, e))
            ranked.sort(key=lambda x: (-x[0], x[1]))
            return [(e, f) for f, _, e in ranked[:topk]]
        if allow_fallback:
            top = sorted(entries, key=lambda e: e['importance'],
                         reverse=True)[:topk]
            return [(e, 0.0) for e in top]
        return []

    def _rank(self, query: str, topk: int,
              allow_fallback: bool = False) -> list:
        return [e for e, _ in self._rank_scored(
            query, topk, allow_fallback=allow_fallback)]

    def recall_block(self, query: str, cfg: dict,
                     recent_script: str = '', *, group_id: str = '',
                     source: str = 'human', mood_tag: str = '',
                     mood_level: int = 0) -> str:
        """本轮自我记忆注入段；无内容返回 ''（0 token）。

        被采用的记忆同步写一条 recall 回忆链（self_memory_trace_on=False
        时只 touch 不写链）；trace 永不进本函数的输出。
        """
        if not cfg.get('self_memory_on', True):
            return ''
        query = (query or '').strip()
        if not query:
            return ''
        topk = int(cfg.get('self_memory_recall_topk', 5))
        budget = int(cfg.get('self_memory_recall_budget', 300))
        picked = self._rank_scored(query, topk)
        lines, used, touched, trace_rows = [], 0, [], []
        ctx = query[:300]
        trace_on = cfg.get('self_memory_trace_on', True)
        for e, score in picked:
            line = f'- {e["text"]}'
            if recent_script and (
                    e['text'] in recent_script
                    or (len(e['text']) > 8
                        and e['text'][:12] in recent_script)):
                continue  # 对话窗里刚说过的不重复
            if used + len(line) > budget:
                break
            lines.append(line)
            used += len(line)
            touched.append(e['id'])
            if trace_on:
                trace_rows.append({
                    'memory_id': e['id'], 'kind': 'recall',
                    'context': ctx, 'group_id': str(group_id or ''),
                    'source': str(source or ''),
                    'mood_tag': str(mood_tag or ''),
                    'mood_level': int(mood_level or 0),
                    'seen_text': e['text'][:400], 'revision': '',
                    'match_score': round(float(score), 4)})
        if touched:
            try:
                storage.touch_and_trace_self_memories(
                    self.db_path, touched, trace_rows)
            except Exception as ex:
                self._log(f'回忆链写入失败（不影响对话）: {ex}', 'warning')
        if not lines:
            return ''
        return _block_header(getattr(self, 'prompts', None)) \
            + '\n' + '\n'.join(lines)

    # ── 模型工具入口 ────────────────────────────────────────────

    async def recall_self_tool(self, keyword: str, cfg: dict) -> str:
        if not cfg.get('self_memory_on', True):
            return '自我记忆功能未启用'
        topk = await asyncio.to_thread(
            self._rank, keyword, 8) if keyword else \
            storage.list_active_self_memories(self.db_path)[:8]
        if not topk:
            return '你还没有关于自己的记忆'
        lines = [f'- {e["text"]}' for e in topk]
        return '你自己的记忆：\n' + '\n'.join(lines)

    async def remember_self_tool(self, group_id, statement: str,
                                 cfg: dict) -> str:
        if not cfg.get('self_memory_on', True):
            return '自我记忆功能未启用'
        statement = (statement or '').strip()[:200]
        if not statement:
            return '没什么可记的'
        day = _local_day()
        quota = int(cfg.get('self_memory_daily_quota', 8))
        used = await asyncio.to_thread(storage.self_quota_get,
                                       self.db_path, day)
        # 只有「新建」才耗配额：先判断本轮会不会走新增
        will_create = await asyncio.to_thread(
            self._will_create, statement)
        if will_create and used >= quota:
            return (f'今天关于自己的新记忆已经记了 {used} 条，先消化一下；'
                    '真要记可以等明天，或者先在群里继续聊。')
        mid, created = await asyncio.to_thread(
            merge_self, self.db_path, statement, '自我', 0.65,
            'add', str(group_id or ''), source='tool',
            context=statement[:300])
        if created:
            await asyncio.to_thread(storage.self_quota_bump,
                                    self.db_path, day, 1)
        self.invalidate()
        return f'已记住（{mid}）'

    def _will_create(self, text: str) -> bool:
        entries, _ = self._index()
        best = 0.0
        for e in entries:
            r = similarity(text, e['text'])
            if r > best:
                best = r
        return best < 0.5

    # ── 周期抽取落库 ────────────────────────────────────────────

    async def apply_extracted(self, group_id, items: list,
                              cfg: dict) -> int:
        """记忆抽取 C 段落库。返回新建条数。配额耗尽后剩下的丢弃。"""
        if not cfg.get('self_memory_on', True) or not items:
            return 0
        day = _local_day()
        quota = int(cfg.get('self_memory_daily_quota', 8))
        used = await asyncio.to_thread(storage.self_quota_get,
                                       self.db_path, day)
        created_n = 0
        for it in items:
            text = (it.get('text') or '').strip()[:200]
            if not text:
                continue
            will_create = await asyncio.to_thread(self._will_create, text)
            if will_create and used + created_n >= quota:
                self._log('自我记忆今日写配额已满，剩余抽取条目丢弃')
                break
            try:
                _, created = await asyncio.to_thread(
                    merge_self, self.db_path, text,
                    str(it.get('tags') or '自我')[:20],
                    float(it.get('importance', 0.65)),
                    'update' if it.get('op') == 'update' else 'add',
                    str(group_id or ''), source='extract',
                    context=text[:300])
            except Exception as ex:
                self._log(f'自我记忆落库失败（不影响其他流程）: {ex}',
                          'warning')
                continue
            if created:
                created_n += 1
        if created_n:
            await asyncio.to_thread(storage.self_quota_bump,
                                    self.db_path, day, created_n)
            self.invalidate()
        return created_n
