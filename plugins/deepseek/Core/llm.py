"""AsyncOpenAI 薄封装：chat / judge / summarize / extract_memories。

openai 延迟导入（插件 lib 未安装时本模块仍可被导入，单测可注入 fake client）。
judge/summary/extract 恒关深度思考，用便宜参数省钱。
"""
from __future__ import annotations

import json
import os
import re
import uuid

from . import config as cfg_mod, context as ctx_mod, prompts as prompts_mod
from .config import ROUTE_LABELS
from .dsml import parse_dsml
from .mood import MOOD_DIMS


class LLMError(RuntimeError):
    pass


class ChatResult:
    def __init__(self, content='', tool_calls=None, usage=None, raw=None,
                 finish_reason='', reasoning=''):
        self.content = content or ''
        self.tool_calls = tool_calls or []
        self.usage = usage or {}
        self.raw = raw
        self.finish_reason = finish_reason or ''
        # 推理模型（火山方舟 deepseek 系 / deepseek-reasoner）的思考链在单独字段
        self.reasoning = reasoning or ''


def extract_cache_hit(usage) -> int:
    """兼容 prompt_cache_hit_tokens 与 prompt_tokens_details.cached_tokens。"""
    if usage is None:
        return 0
    get = getattr(usage, 'get', None)
    if callable(get):
        d = usage
    else:
        d = getattr(usage, 'model_dump', lambda: {})() or vars(usage)
    hit = d.get('prompt_cache_hit_tokens')
    if hit:
        return int(hit)
    details = d.get('prompt_tokens_details') or {}
    if isinstance(details, dict):
        return int(details.get('cached_tokens') or 0)
    return int(getattr(details, 'cached_tokens', 0) or 0)


_JSON_OBJ_RE = re.compile(r'\{[\s\S]*\}')

# 心情判定的固定系统前缀：每轮变化的口径/幅度/当前心情值全部放在 user 消息里，
# 让这段成为可缓存的公共前缀
_MOOD_SYSTEM = (
    '你在判断一个 QQ 群聊陪伴机器人（一个有自己情绪的「她」）'
    '刚刚这一轮之后的心情变化。\n'
    '心情值 0-100（越高越轻快），另有七绪：开心/踏实/甜蜜/心疼/担心/'
    '难过/吃醋，强度 0-100。\n'
    '铁律：\n'
    '1. 只能依据这一轮真实发生的信息，禁止编造没有的情节；'
    '没有值得动情绪的事就给 delta=0；\n'
    '2. 心情下降只能因为她心疼/担心某个群友，或她自己真的难过；'
    '绝不允许「对方惹她生气、对方冒犯了她」这类写法——'
    '群友的普通玩笑/拌嘴/催促不是攻击；\n'
    '3. delta 与七绪增量必须在本轮给定的「幅度上限」以内，'
    'delta 为整数；没有变化的绪不要写；\n'
    '4. reason 用两小句过去式中文，必须点到具体昵称/具体事，'
    '禁止使用「此刻」「一直」「用户」这些词，例：'
    '「阿凯说发烧了，她听着有点担心。他语气还轻松，又没太沉下去。」；\n'
    '5. confidence 0-100：信息含糊、拿不准就给低分。\n'
    '只输出 JSON：{"delta": -3, "dims": {"担心": 4}, '
    '"reason": "……", "confidence": 75}'
)


_JUDGE_TRUE = {'true', '1', 'yes', 'y', '是', '回复', '要'}
_JUDGE_FALSE = {'false', '0', 'no', 'n', '否', '不回复', '不要'}


def _parse_judge(content: str) -> tuple:
    """宽松解析判官输出，返回 (should|None, reason)；None=无法判定（交给 fail_open）。"""
    data = _loose_json(content) or {}
    raw = data.get('reply')
    if isinstance(raw, bool):
        should = raw
    elif isinstance(raw, str):
        s = raw.strip().lower()
        if s in _JUDGE_TRUE:
            should = True
        elif s in _JUDGE_FALSE:
            should = False
        else:
            should = None
    else:
        should = None
    reason = str(data.get('reason') or '').strip()[:30]
    return should, reason


def _loose_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        m = _JSON_OBJ_RE.search(text or '')
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


class LLMClient:
    def __init__(self):
        # provider id -> (AsyncOpenAI, 签名(api_key, base_url))
        self._clients = {}
        self._sigs = {}
        # opencode 系中转要求 x-opencode-session 头；进程内固定一个会话 id
        self._opencode_session = uuid.uuid4().hex
        # 插件构造完 PromptStore 后挂上来；None 时全部用 prompts.py 默认文案
        self.prompts = None

    def _prompt(self, key: str) -> str:
        """后台模型系统提示词：提示词页覆盖优先，缺省走注册表默认值。"""
        store = getattr(self, 'prompts', None)
        return prompts_mod.render(key, store=store)

    def _get_client(self, provider):
        """按服务商缓存 AsyncOpenAI（单参；测试可整体替换此方法注入 fake）。"""
        pid = provider.get('id') or 'default'
        api_key = (provider.get('api_key') or '').strip()
        if not api_key and pid == cfg_mod.DEFAULT_PROVIDER_ID:
            api_key = os.environ.get('DEEPSEEK_API_KEY', '')
        base_url = (provider.get('base_url') or '').strip() or 'https://api.deepseek.com'
        sig = (api_key, base_url)
        existing = self._clients.get(pid)
        if existing is not None and self._sigs.get(pid) == sig:
            return existing
        from openai import AsyncOpenAI  # 延迟导入
        kw = dict(api_key=api_key or 'missing-key',
                  base_url=base_url, timeout=60.0)
        # opencode 系中转：服务商 id 或 base_url 带 opencode 即补会话头
        if pid == 'opencode' or 'opencode' in base_url.lower():
            kw['default_headers'] = {
                'x-opencode-session': self._opencode_session}
        client = AsyncOpenAI(**kw)
        if existing is not None:
            self._schedule_close(existing)
        self._clients[pid] = client
        self._sigs[pid] = sig
        return client

    def _schedule_close(self, client) -> None:
        """best-effort 关闭旧客户端：框架主循环在跑就调度，否则放弃（GC 兜底）。"""
        try:
            from core import globals
            loop = getattr(globals, 'event_loop', None)
            if loop is not None and loop.is_running():
                loop.create_task(self._safe_close(client))
        except Exception:
            pass

    @staticmethod
    async def _safe_close(client) -> None:
        try:
            await client.close()
        except Exception:
            pass

    async def aclose(self):
        for client in list(self._clients.values()):
            try:
                await client.close()
            except Exception:
                pass
        self._clients = {}
        self._sigs = {}

    def prune_clients(self, active_ids) -> None:
        """配置里删掉服务商后，清掉对应缓存客户端（best-effort）。"""
        active = set(active_ids or ())
        for pid in list(self._clients):
            if pid not in active:
                self._schedule_close(self._clients.pop(pid, None))
                self._sigs.pop(pid, None)

    def _resolve(self, cfg: dict, route: str, model):
        """把 chat 的 route/model 入参解析成 (provider, model_id)。

        ValueError 统一转中文 LLMError，调用方按既有异常路径处理。
        """
        value = model if model is not None else cfg.get(route)
        label = ROUTE_LABELS.get(route, route)
        try:
            provider, model_id = cfg_mod.resolve_model_value(cfg, value)
        except ValueError as e:
            msg = str(e)
            if '没有模型' in msg or '不在 default' in msg:
                raise LLMError(f'{label}选择的模型不在该服务商的模型列表里：{value}')
            if '服务商' in msg:
                raise LLMError(f'{label}的服务商不存在或已删除：{value}，请在设置页重新选择模型')
            raise LLMError(f'{label}的路由值非法：{value}（应为 服务商ID/模型ID）')
        return provider, model_id

    # ── 基础对话 ────────────────────────────────────────────────

    async def chat(self, messages, cfg: dict, *, model=None, tools=None,
                   max_tokens=None, temperature=None, kind='chat',
                   timeout=None, no_thinking=False, route='model') -> ChatResult:
        provider, model = self._resolve(cfg, route, model)
        client = self._get_client(provider)
        kwargs = {
            'model': model,
            'messages': messages,
            'max_tokens': max_tokens or int(cfg.get('max_tokens', 30000)),
            'temperature': 0.7 if temperature is None else temperature,
        }
        if tools:
            kwargs['tools'] = tools
            kwargs['tool_choice'] = 'auto'
        # 判官/摘要/抽取恒关思考：火山方舟 deepseek 系模型默认可能开启推理，
        # 思考链计入 max_tokens，小配额调用会被吃光导致正文为空。
        if no_thinking:
            kwargs['extra_body'] = {'thinking': {'type': 'disabled'}}
        elif kind == 'chat' and cfg.get('enable_thinking') and model == 'deepseek-chat':
            kwargs['extra_body'] = {'thinking': {'type': 'enabled'}}

        async def _call():
            return await client.chat.completions.create(
                timeout=timeout or float(cfg.get('request_timeout', 60)), **kwargs)

        try:
            resp = await _call()
        except Exception as e:
            # 思考参数不被服务端接受时，摘掉重式一次
            if 'extra_body' in kwargs and _is_param_error(e):
                kwargs.pop('extra_body', None)
                resp = await _call()
            else:
                raise

        choice = resp.choices[0]
        msg = choice.message
        tool_calls = []
        for tc in (msg.tool_calls or []):
            args = {}
            try:
                args = json.loads(tc.function.arguments or '{}')
            except json.JSONDecodeError:
                args = {'_raw': tc.function.arguments}
            tool_calls.append({'id': tc.id, 'name': tc.function.name,
                               'arguments': args})
        # DSML 泄漏兜底：服务端没把模型的工具调用标记解析成 tool_calls 时，
        # 标记会混在正文里，这里还原成标准工具调用（残片一律剥掉，绝不发群）
        content = msg.content or ''
        # content 可能是多模态 parts（视觉/工具消息后的回复罕见但要防），
        # DSML 只在纯文本里找
        if not tool_calls and isinstance(content, str) and content:
            dsml_calls, content, _found = parse_dsml(content)
            if dsml_calls:
                tool_calls = dsml_calls
        u = resp.usage
        usage = {
            'tokens_in': getattr(u, 'prompt_tokens', 0) or 0,
            'tokens_out': getattr(u, 'completion_tokens', 0) or 0,
            'cache_hit': extract_cache_hit(u),
        }
        return ChatResult(content=content, tool_calls=tool_calls,
                          usage=usage, raw=resp,
                          finish_reason=getattr(choice, 'finish_reason', '') or '',
                          reasoning=getattr(msg, 'reasoning_content', '') or '')

    async def transcribe(self, cfg: dict, filename: str, audio: bytes,
                         model: str) -> str:
        """OpenAI 兼容音频转写（audio.transcriptions）。服务端不支持时异常上浮。

        ASR 模型名是自由填写（whisper-1 等），不在聊天模型列表里做归属校验：
        限定写法 ``服务商id/模型名`` 选服务商，裸模型名走 default 服务商。
        """
        try:
            pid, mid = cfg_mod.split_route(model)
        except (ValueError, TypeError):
            raise LLMError(f'ASR 模型名非法：{model!r}')
        if pid:
            provider = cfg_mod.get_provider(cfg, pid)
            if provider is None:
                raise LLMError(f'ASR 指定的服务商不存在：{pid}')
        else:
            provider = (cfg_mod.get_provider(cfg, cfg_mod.DEFAULT_PROVIDER_ID)
                        or (cfg.get('llm_providers') or [None])[0])
            if provider is None:
                raise LLMError('没有可用的模型服务商，请先在设置页配置')
        client = self._get_client(provider)
        resp = await client.audio.transcriptions.create(
            model=mid,
            file=(filename, audio, 'audio/mpeg'),
        )
        if isinstance(resp, str):
            return resp
        text = getattr(resp, 'text', None)
        if isinstance(text, str):
            return text
        if isinstance(resp, dict):
            return str(resp.get('text') or '')
        return ''

    # ── 判官：要不要回 ──────────────────────────────────────────

    async def judge(self, cfg: dict, history_script: str, user_text: str) -> tuple:
        """返回 (是否回复, 理由, usage dict)。max_tokens=80，无工具。

        解析不出合法判定时抛 LLMError —— 调用方按 judge_fail_open 决定
        放行/拦截，并把原始输出写进日志，绝不静默判否。
        """
        sys = self._prompt('judge_prompt')
        recent = history_script.strip()
        usr = f'{recent}\n最后一条：{user_text}' if recent else f'最后一条：{user_text}'
        messages = [{'role': 'system', 'content': sys},
                    {'role': 'user', 'content': usr}]
        result = await self.chat(messages, cfg, route='judge_model',
                                 max_tokens=80, temperature=0.0, kind='judge', no_thinking=True)
        should, reason = _parse_judge(result.content)
        if should is None:
            if not result.content.strip():
                detail = (f'空回复(finish_reason={result.finish_reason or "?"}'
                          f'，out={result.usage.get("tokens_out", 0)}'
                          f'{"，思考链耗尽配额" if result.reasoning else ""})')
            else:
                detail = repr(result.content[:100])
            raise LLMError(f'判官输出无法解析: {detail}')
        return should, reason, result.usage

    # ── 摘要 ────────────────────────────────────────────────────

    async def summarize(self, cfg: dict, persona: str, old_summary: str,
                        script: str) -> tuple:
        """增量摘要旧对话，返回 (摘要文本, usage)。"""
        sys = self._prompt('summary_prompt')
        parts = [f'机器人当前人设：{persona}',
                 f'新发生的对话：\n{script}']
        if old_summary:
            parts.insert(1, f'已有摘要（请融合而非照抄）：\n{old_summary}')
        messages = [{'role': 'system', 'content': sys},
                    {'role': 'user', 'content': '\n\n'.join(parts)}]
        result = await self.chat(messages, cfg, route='summary_model',
                                 max_tokens=600, temperature=0.2,
                                 kind='summary', no_thinking=True)
        return result.content.strip(), result.usage

    # ── 记忆抽取 ────────────────────────────────────────────────

    async def extract_memories(self, cfg: dict, script: str) -> tuple:
        """从群聊片段抽取 per-user 事实、明确约定与第一人称自我记忆。

        返回 (entries, commitments, self_entries, usage)：
        entries:      [{name, text, tags, importance, op}]
        commitments:  [{owner, content, kind, due_text, due_date, op}]
        self_entries: [{text, tags, importance, op}]（关于机器人"我"的）
        """
        sys = self._prompt('extract_prompt')
        messages = [{'role': 'system', 'content': sys},
                    {'role': 'user', 'content': script}]
        result = await self.chat(messages, cfg, route='extract_model',
                                 max_tokens=1100, temperature=0.1,
                                 kind='extract', no_thinking=True)
        data = _loose_json(result.content) or {}
        entries = data.get('entries') if isinstance(data, dict) else None
        if not isinstance(entries, list):
            entries = []
        clean = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            text = str(e.get('text', '')).strip()
            name = str(e.get('name', '')).strip()
            if not text or not name:
                continue
            clean.append({
                'name': name,
                'text': text[:200],
                'tags': str(e.get('tags', '其他'))[:20],
                'importance': min(1.0, max(0.0, float(e.get('importance', 0.6) or 0.6))),
                'op': 'update' if str(e.get('op', 'add')).lower() == 'update' else 'add',
            })
        commitments = self._clean_commitments(
            data.get('commitments') if isinstance(data, dict) else None)
        self_entries = self._clean_self_entries(
            data.get('self_entries') if isinstance(data, dict) else None)
        return clean, commitments, self_entries, result.usage

    @staticmethod
    def _clean_self_entries(raw) -> list:
        if not isinstance(raw, list):
            return []
        out = []
        for e in raw:
            if not isinstance(e, dict):
                continue
            text = str(e.get('text', '')).strip()
            if not text:
                continue
            try:
                importance = min(1.0, max(0.0,
                                 float(e.get('importance', 0.65) or 0.65)))
            except (TypeError, ValueError):
                importance = 0.65
            out.append({
                'text': text[:200],
                'tags': str(e.get('tags', '自我'))[:20],
                'importance': importance,
                'op': 'update' if str(e.get('op', 'add')).lower() == 'update'
                else 'add',
            })
        return out

    # ── 心情判定 ────────────────────────────────────────────────

    async def judge_mood(self, cfg: dict, *, mood_now: float,
                         dims_now: dict, trigger_text: str, reply_text: str,
                         context_script: str, source_kind: str) -> dict:
        """判这一轮后她的心情变化。返回 {delta, dims, reason, confidence, usage}。

        delta：心情值增量（带符号整数）；dims：七绪增量。
        输出无法解析为 JSON 时抛 LLMError，调用方跳过本轮判定。
        """
        source_desc = {
            'human': ('这是群友/私聊对象跟她说话的真实一轮，按正常的人际互动判。',
                      10),
            'talk': ('这是她自己主动搭话的一轮，判她开口时的心意，不要把触发语'
                     '当成别人对她说的话。', 5),
            'work': ('这是定时任务/闹钟驱动她干了一轮活。闹钟本身不是情绪事件，'
                     '只看她自己干活时的语气，幅度必须很小。', 3),
            'greet': ('这是她刚进群/新见面打招呼的一轮。', 5),
        }.get(source_kind, ('这是真实的人际互动一轮，按正常口径判。', 10))
        desc, cap = source_desc
        dim_lines = '\n'.join(f'- {k}：{int(dims_now.get(k, 0))}'
                              for k in MOOD_DIMS)
        # system 为常量公共前缀（利于前缀缓存）；口径/幅度/当前心情值每轮都变，
        # 全部放进 user 消息。幅度钳制在解析后仍由代码兜底（见下方 clamp）。
        state = (
            f'本轮口径：{desc}（幅度上限 ±{cap}）\n'
            f'当前心情值约 {round(float(mood_now), 1)}，七绪现状：\n'
            f'{dim_lines}'
        )
        parts = [state]
        if context_script and context_script.strip():
            parts.append('最近的对话背景：\n' + context_script.strip()[-1500:])
        parts.append(f'这一轮对方的话/触发内容：\n{trigger_text.strip()[:800]}')
        parts.append(f'她这一轮的回复：\n{reply_text.strip()[:800]}')
        messages = [{'role': 'system', 'content': _MOOD_SYSTEM},
                    {'role': 'user', 'content': '\n\n'.join(parts)}]
        result = await self.chat(messages, cfg, route='mood_model',
                                 max_tokens=200, temperature=0.2,
                                 kind='mood', no_thinking=True)
        data = _loose_json(result.content)
        if not isinstance(data, dict):
            raise LLMError(
                f'心情判定输出无法解析: {repr(result.content[:100])}')
        try:
            delta = int(round(float(data.get('delta', 0) or 0)))
        except (TypeError, ValueError):
            raise LLMError('心情判定 delta 非法')
        if not -cap <= delta <= cap:
            delta = max(-cap, min(cap, delta))
        dims = {}
        raw_dims = data.get('dims')
        if isinstance(raw_dims, dict):
            for k, v in raw_dims.items():
                if k not in MOOD_DIMS:
                    continue
                try:
                    inc = int(round(float(v)))
                except (TypeError, ValueError):
                    continue
                if inc:
                    dims[k] = max(-cap, min(cap, inc))
        reason = str(data.get('reason', '') or '').strip()
        try:
            confidence = int(round(float(data.get('confidence', 0) or 0)))
        except (TypeError, ValueError):
            confidence = 0
        confidence = max(0, min(100, confidence))
        if not reason and (delta or dims):
            raise LLMError('心情判定缺少 reason')
        return {'delta': delta, 'dims': dims, 'reason': reason,
                'confidence': confidence, 'usage': result.usage}

    @staticmethod
    def _clean_commitments(raw) -> list:
        if not isinstance(raw, list):
            return []
        out = []
        for c in raw:
            if not isinstance(c, dict):
                continue
            owner = str(c.get('owner', '')).strip()
            content = str(c.get('content', '')).strip()
            if not owner or not content:
                continue
            kind = str(c.get('kind', 'task')).strip().lower()
            out.append({
                'owner': owner,
                'content': content[:200],
                'kind': 'topic' if kind == 'topic' else 'task',
                'due_text': str(c.get('due_text', ''))[:40],
                'due_date': str(c.get('due_date', '')).strip()[:20],
                'op': 'done' if str(c.get('op', 'add')).lower() == 'done' else 'add',
            })
        return out

    async def build_style(self, cfg: dict, script: str) -> tuple:
        """从群聊样本学习群体说话风格，返回 (风格画像文本, usage)。只学表达，不学事实。"""
        sys = (
            '你是群聊语言风格分析师。阅读这群人的聊天记录，提炼一段 200~400 字的'
            '「群体说话风格画像」，供群聊机器人模仿语气。内容包括：\n'
            '- 典型句长与断句（短句连珠还是长句）、高频语气词和口癖（举真实例子，如"哈哈""绷""嘛""呗"）；\n'
            '- emoji/颜文字/拼音缩写/网络梗的使用方式与密度；\n'
            '- 典型玩笑、吐槽、互怼方式，互相之间的称呼习惯；\n'
            '- 打招呼、接话、结束话题的常见方式。\n'
            '严格只写可迁移的表达方式：不要记录任何具体人名、事件、隐私事实；'
            '脏话、人身攻击、歧视性表达列入「不要模仿」；不要编造样本里没有的特征。\n'
            '直接输出画像正文，用分点短句，不要标题和寒暄。'
        )
        messages = [{'role': 'system', 'content': sys},
                    {'role': 'user', 'content': script[-6000:]}]
        result = await self.chat(messages, cfg, route='style_model',
                                 max_tokens=500, temperature=0.3,
                                 kind='style', no_thinking=True)
        return result.content.strip(), result.usage


def _is_param_error(exc) -> bool:
    s = str(exc).lower()
    return 'thinking' in s or 'unknown parameter' in s or 'unrecognized' in s
