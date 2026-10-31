"""按 QQ 用户的长期记忆：CJK BM25 召回、相似度合并、异步批量抽取。

参考 flyai 记忆骨架，按群聊改造：
- 记忆主体是 QQ 用户（跨群共享事实），不是 AI 角色
- 零依赖 BM25（unigram+bigram），无 embedding
- 抽取 fire-and-forget，不阻塞回复
本模块不 import openai（LLMClient 注入）。
"""
from __future__ import annotations

import asyncio
import math
import re
import time
from collections import Counter
from difflib import SequenceMatcher

from . import context as ctx_mod
from . import storage

_CJK_RE = re.compile(r'[一-鿿]')
_TOKEN_RE = re.compile(r'[a-z0-9]+')


def tokenize(text: str) -> list[str]:
    """CJK unigram+bigram，拉丁/数字整词，全部小写。"""
    text = (text or '').lower()
    toks = []
    for w in _TOKEN_RE.findall(text):
        toks.append(w)
    cjk = ''.join(ch for ch in text if _CJK_RE.match(ch))
    toks.extend(list(cjk))
    toks.extend(cjk[i:i + 2] for i in range(len(cjk) - 1))
    return toks


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.lens = [len(d) for d in docs]
        self.avgdl = (sum(self.lens) / self.n) if self.n else 0
        self.tf = [Counter(d) for d in docs]
        df = Counter()
        for d in docs:
            for t in set(d):
                df[t] += 1
        self.idf = {t: math.log(1 + (self.n - f + 0.5) / (f + 0.5))
                    for t, f in df.items()}

    def score(self, query: list[str]) -> list[tuple[int, float]]:
        if self.n == 0:
            return []
        out = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            denom_base = 1 - self.b + self.b * (self.lens[i] / (self.avgdl or 1))
            for t in query:
                if t not in tf:
                    continue
                idf = self.idf.get(t, 0.0)
                f = tf[t]
                s += idf * (f * (self.k1 + 1)) / (f + self.k1 * denom_base)
            if s > 0:
                out.append((i, s))
        out.sort(key=lambda x: x[1], reverse=True)
        return out


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a or '', b or '').ratio()


def _merge_one(db_path: str, user_id: str, text: str, tags: str,
               importance: float, op: str, source_group_id: str,
               log=None) -> int:
    """三档合并，返回记忆 id。≥0.8 或显式 update(≥0.5) 就地更新；
    0.5–0.8 旧条软删+新增；否则新增。"""
    entries = storage.list_active_memories(db_path, user_id)
    best, best_ratio = None, 0.0
    for e in entries:
        r = similarity(text, e['text'])
        if r > best_ratio:
            best, best_ratio = e, r

    if best is not None and (best_ratio >= 0.8 or (op == 'update' and best_ratio >= 0.5)):
        storage.update_memory_full(
            db_path, best['id'], text,
            tags or best.get('tags', ''), max(importance, best['importance']),
            best.get('history', []))
        return best['id']
    if best is not None and best_ratio >= 0.5:
        storage.update_memory(db_path, best['id'], superseded=True)
    return storage.add_memory(db_path, user_id, text, tags=tags,
                              importance=importance,
                              source_group_id=source_group_id)


class MemoryManager:
    def __init__(self, db_path: str, log=None):
        self.db_path = db_path
        self._log = log or (lambda msg: None)
        self._cache: dict[str, tuple[list, BM25]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set = set()
        self.tasks = None       # TaskManager，插件装配后注入
        self.self_memory = None  # SelfMemoryManager，插件装配后注入

    def _lock(self, group_id: str) -> asyncio.Lock:
        lk = self._locks.get(group_id)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[group_id] = lk
        return lk

    def invalidate(self, user_id=None):
        if user_id is None:
            self._cache.clear()
        else:
            self._cache.pop(str(user_id), None)

    # ── 召回 ────────────────────────────────────────────────────

    def _index(self, user_id: str):
        user_id = str(user_id)
        cached = self._cache.get(user_id)
        if cached is not None:
            return cached
        entries = storage.list_active_memories(self.db_path, user_id)
        bm = BM25([tokenize(e['text']) for e in entries])
        self._cache[user_id] = (entries, bm)
        return entries, bm

    def _rank(self, user_id: str, query: str, topk: int,
              allow_fallback: bool = False) -> list[dict]:
        entries, bm = self._index(user_id)
        if not entries:
            return []
        now = time.time()
        scored = bm.score(tokenize(query))
        if scored:
            max_s = scored[0][1] or 1.0
            ranked = []
            for rank, (i, s) in enumerate(scored):
                e = entries[i]
                recency = math.exp(-(now - (e['updated_at'] or 0)) / 86400 / 60)
                fused = (s / max_s) * 0.75 + e['importance'] * 0.15 + recency * 0.10
                ranked.append((fused, rank, e))
            ranked.sort(key=lambda x: (-x[0], x[1]))
            return [e for _, _, e in ranked[:topk]]
        # 自动注入（query=当前消息）无命中 → 返回空，不塞无关记忆；
        # 仅模型主动查（无 keyword）时才按重要度兜底
        if allow_fallback:
            return sorted(entries, key=lambda e: e['importance'], reverse=True)[:topk]
        return []

    def _display_name(self, user_id: str) -> str:
        u = storage.get_user(self.db_path, user_id)
        name = (u['names'][0] if u and u.get('names') else '') or '群友'
        return f'{name}(…{str(user_id)[-4:]})'

    def recall_block(self, user_ids: list, query: str, cfg: dict,
                     recent_script: str = '') -> str:
        """组装发言者记忆注入段；无内容返回 ''（0 token）。"""
        if not cfg.get('memory_on', True) or not user_ids:
            return ''
        topk = int(cfg.get('memory_recall_topk', 6))
        budget = int(cfg.get('memory_recall_budget', 400))
        per_user_cap = max(1, topk // 2 + 1)
        lines, used = [], 0
        touched = []
        for uid in dict.fromkeys(str(u) for u in user_ids if u):
            picked = self._rank(uid, query, per_user_cap)
            label = self._display_name(uid)
            for e in picked:
                line = f'- {label}：{e["text"]}'
                if recent_script and (e['text'] in recent_script
                                      or (len(e['text']) > 8 and e['text'][:12] in recent_script)):
                    continue  # 滑窗里已有的事实不重复注入
                if used + len(line) > budget:
                    break
                lines.append(line)
                used += len(line)
                touched.append(e['id'])
            if used >= budget:
                break
        if touched:
            storage.touch_memories(self.db_path, touched)
        if not lines:
            return ''
        return ('【你记得的群友信息】（仅供参考，不合时宜不要主动提起，'
                '也不要逐字复述）\n' + '\n'.join(lines))

    # ── 姓名解析 ────────────────────────────────────────────────

    def resolve_user(self, group_id: str, name: str):
        name = (name or '').strip()
        if not name:
            return None
        if name.isdigit():
            if storage.get_user(self.db_path, name):
                return name
            roster = storage.group_roster(self.db_path, group_id, limit=2000)
            return name if name in roster.values() else None
        roster = storage.group_roster(self.db_path, group_id)
        if name in roster:
            return roster[name]
        # 群名册没有 → 全局用户档案昵称/名片
        for u in storage.list_users(self.db_path, query=name, limit=50):
            if name in u.get('names', []):
                return u['user_id']
        return None

    # ── 模型工具入口 ────────────────────────────────────────────

    async def recall_user_tool(self, group_id, name, keyword, cfg) -> str:
        if not cfg.get('memory_on', True):
            return '记忆功能未启用'
        uid = await asyncio.to_thread(self.resolve_user, group_id, name)
        if not uid:
            return f'未找到群友「{name}」，请使用本群近期出现过的昵称或QQ号'
        topk = self._rank(uid, keyword, 8) if keyword else \
            storage.list_active_memories(self.db_path, uid)[:8]
        if not topk:
            return f'没有关于 {name} 的记忆'
        lines = [f'- {e["text"]}' for e in topk]
        return f'关于 {name} 的记忆：\n' + '\n'.join(lines)

    async def remember_user_tool(self, group_id, name, fact, cfg) -> str:
        if not cfg.get('memory_on', True):
            return '记忆功能未启用'
        uid = await asyncio.to_thread(self.resolve_user, group_id, name)
        if not uid:
            return f'未找到群友「{name}」，无法记录'
        mid = await asyncio.to_thread(
            _merge_one, self.db_path, uid, fact[:200], '其他', 0.6,
            'add', str(group_id), self._log)
        self.invalidate(uid)
        return f'已记住（{mid}）'

    # ── 异步批量抽取 ────────────────────────────────────────────

    async def force_extract(self, group_id, cfg, llm):
        """后台「立即抽取」：把计数推过阈值后走正常流程。"""
        every = max(1, int(cfg.get('memory_extract_every', 5)))
        state = storage.get_memory_state(self.db_path, str(group_id))
        gap = max(0, every - int(state.get('turns_since', 0)))
        if gap:
            storage.bump_memory_turn(self.db_path, str(group_id), gap)
        await self._maybe_extract(str(group_id), cfg, llm)

    def schedule_extraction(self, group_id, cfg, llm):
        """fire-and-forget 调度；同群重入直接跳过，异常静默。"""
        task = asyncio.create_task(
            self._maybe_extract(str(group_id), cfg, llm))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _maybe_extract(self, group_id: str, cfg: dict, llm):
        if not cfg.get('memory_on', True):
            return
        every = int(cfg.get('memory_extract_every', 5))
        if every <= 0:
            return
        lk = self._lock(group_id)
        if lk.locked():
            return
        async with lk:
            try:
                state = storage.bump_memory_turn(self.db_path, group_id)
                if state['turns_since'] < every:
                    return
                cursor = state['extracted_up_to_id']
                rows = storage.messages_after(
                    self.db_path, group_id, cursor,
                    int(cfg.get('memory_extract_max_msgs', 40)))
                if not rows:
                    storage.set_memory_cursor(self.db_path, group_id, cursor)
                    return
                script = ctx_mod.render_script(rows)
                if not script.strip():
                    storage.set_memory_cursor(
                        self.db_path, group_id, max(r['id'] for r in rows))
                    return

                res = await llm.extract_memories(cfg, script)
                # 真实 LLMClient 返回 4 元组（含自我记忆 C 段）；
                # 旧 fake / 旧实现可能仍是 3 元组，宽容解包
                if len(res) == 4:
                    entries, commitments, self_entries, usage = res
                else:
                    entries, commitments, usage = res
                    self_entries = []
                storage.add_usage(self.db_path, {
                    'group_id': group_id, 'kind': 'extract',
                    'tokens_in': usage.get('tokens_in', 0),
                    'cache_hit': usage.get('cache_hit', 0),
                    'tokens_out': usage.get('tokens_out', 0), 'ok': True})

                roster = storage.group_roster(self.db_path, group_id,
                                              limit=2000)
                applied = 0
                for e in entries:
                    uid = await asyncio.to_thread(
                        self._resolve_extract_name, roster, e['name'])
                    if not uid:
                        continue  # 名字映射不上：丢弃，绝不张冠李戴
                    await asyncio.to_thread(
                        _merge_one, self.db_path, uid, e['text'], e['tags'],
                        e['importance'], e['op'], group_id, self._log)
                    self.invalidate(uid)
                    applied += 1
                n_tasks = 0
                if self.tasks is not None and commitments:
                    n_tasks = await self.tasks.apply_extracted(
                        group_id, roster, commitments, self._resolve_extract_name)
                storage.set_memory_cursor(
                    self.db_path, group_id, max(r['id'] for r in rows))
                n_self = 0
                if self.self_memory is not None and self_entries:
                    try:
                        n_self = await self.self_memory.apply_extracted(
                            group_id, self_entries, cfg)
                    except Exception as ex:
                        self._log(f'自我记忆落库失败（不影响其他流程）: {ex}')
                self._log(f'记忆抽取：{len(entries)} 条候选，落库 {applied} 条；'
                          f'约定/话题 {n_tasks} 条；自我记忆 {n_self} 条')
            except Exception as ex:
                self._log(f'记忆抽取失败（不影响回复）: {ex}')

    def _resolve_extract_name(self, roster: dict, name: str):
        name = (name or '').strip()
        if not name:
            return None
        if name in roster:
            return roster[name]
        if name.isdigit() and name in roster.values():
            return name
        # 容错：抽取模型可能带了 @ 或尾号
        clean = name.lstrip('@').strip()
        if clean in roster:
            return roster[clean]
        for u in storage.list_users(self.db_path, query=clean, limit=20):
            if clean in u.get('names', []):
                return u['user_id']
        return None
