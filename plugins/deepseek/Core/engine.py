"""对话编排：触发后落库 → 摘要 → 记忆注入 → 工具循环 → 去重 → 分条发送 → 抽取调度。

调用方（插件）必须已持有该群的 asyncio.Lock，保证同群串行。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

from . import builtin_tools, context as ctx_mod, media_in, prompts as prompts_mod, reply, storage
from .builtin_tools import ToolContext
from .config import parse_aliases, resolve_group, resolve_persona
from .interrupt import InterruptHub
from .models import ParsedMessage

_ALL_TOOL_KEYS = set(builtin_tools.TOOL_KEYS.values())

_SILENT_MARK = '[静默]'


def _is_silent_mark(text: str) -> bool:
    """定时苏醒轮模型选择不说话：整条只回 [静默]（可带内部理由）。"""
    t = (text or '').strip()
    return bool(t) and (t == _SILENT_MARK or t.startswith(_SILENT_MARK))


def _is_context_overflow(exc) -> bool:
    """模型 API 报错是否为上下文超长（各服务商文案不一，宽松匹配）。"""
    msg = str(exc).lower()
    return any(s in msg for s in (
        'context_length', 'context length', 'maximum context',
        'context window', 'too long', 'input token', 'tokens in the'))


# 各服务商超长报错里「模型上下文上限」的文案（OpenAI 系/部分中转/月之暗面等）
_CTX_LIMIT_RES = (
    re.compile(r"maximum context length is\s+(\d+)", re.I),
    re.compile(r"context window[^\d]{0,24}(\d{4,})", re.I),
    re.compile(r"context[ _]length[^\d]{0,24}(\d{4,})", re.I),
    re.compile(r"tokens?\s*>\s*(\d{4,})", re.I),
    re.compile(r"(\d{4,})\s*tokens?\s*maximum", re.I),
)


def _parse_context_limit(exc) -> int:
    """从上下文超长报错里解析模型上下文总 token 数；解析不到返回 0。

    只取 8192~2_000_000 之间的数字，避免把「本次请求 token 数」误当上限。
    """
    msg = str(exc)
    for rx in _CTX_LIMIT_RES:
        m = rx.search(msg)
        if m:
            try:
                n = int(m.group(1))
            except (ValueError, IndexError):
                continue
            if 8192 <= n <= 2_000_000:
                return n
    return 0


def _wire_tool_calls(tool_calls: list) -> list:
    """内部归一化结构 → OpenAI wire 格式（落库/回传都用 wire）。"""
    return [{
        'id': tc['id'], 'type': 'function',
        'function': {'name': tc['name'],
                     'arguments': json.dumps(tc.get('arguments') or {},
                                             ensure_ascii=False)},
    } for tc in tool_calls]




class Engine:
    def __init__(self, plugin):
        self.p = plugin
        # 任务打断队列；单测 fake plugin 没挂 InterruptHub 时用独立实例兜底
        self.hub = getattr(plugin, 'interrupts', None) or InterruptHub()

    def log(self, msg: str, level: str = 'info'):
        self.p.log(msg, level)

    def _live(self, kind: str, gid='', chat_type: str = 'group',
              turn_id: str = '', **fields) -> None:
        """实时事件发布口：插件没挂 LiveManager（单测）时零成本，异常不外泄。"""
        live = getattr(self.p, 'live', None)
        if live is None:
            return
        try:
            live.emit(kind, group_id=gid, chat_type=chat_type,
                      turn_id=turn_id, **fields)
        except Exception:
            pass

    def _prompt_view(self, messages: list) -> str:
        """调试台用：把本轮完整 OpenAI 消息渲染成纯文本（多模态仅留文字）。"""
        lines = []
        for m in messages:
            content = m.get('content')
            if isinstance(content, list):
                parts = []
                for part in content:
                    if isinstance(part, dict) and part.get('type') == 'text':
                        parts.append(str(part.get('text') or ''))
                    else:
                        parts.append('[多模态内容]')
                content = ' '.join(p for p in parts if p)
            if m.get('tool_calls'):
                content = ((content or '') + ' '
                           + json.dumps(m['tool_calls'], ensure_ascii=False))
            lines.append(f"[{m.get('role')}] {content or ''}")
        return '\n'.join(lines)

    # ── 主入口 ──────────────────────────────────────────────────

    def at_only_blocks(self, cfg: dict, parsed: ParsedMessage,
                       group_row: dict | None = None) -> bool:
        """仅@触发：群里只响应 @ 她、回复（引用）她、或叫了她名字的消息。

        「名字」= 全局称呼词 + 本群称呼词 + 机器人昵称（只认名字，不含
        疑问语气/呼唤词——那些属于粗筛，不能让仅@开关形同虚设）。
        私聊不受影响（私聊每句都是对她说）；后台合成轮 at_me=True 也不受影响。
        群消息入口（handle_message）在投长跑插队之前也要用同一判断。
        """
        if not cfg.get('at_only_on', False):
            return False
        if parsed.chat_type != 'group':
            return False
        if parsed.forced:
            return False
        row = group_row or {}
        aliases = (parse_aliases(self.p.cfg.get('aliases'))
                   + parse_aliases(row.get('aliases')))
        return not _called_by_name(parsed.text, aliases, self.p.bot_name)

    async def handle(self, event, parsed: ParsedMessage, group_row: dict):
        gid = str(group_row['group_id'])
        cfg = resolve_group(self.p.cfg, group_row.get('settings'))

        # 用户档案：无论是否触发都积累昵称/名片（群聊优先群名片）
        storage.upsert_user(self.p.db_path, parsed.sender_id,
                            parsed.display_name, gid)

        if self.at_only_blocks(cfg, parsed, group_row):
            self._live('gate', gid, parsed.chat_type,
                       stage='at_only', allowed=False,
                       reason='仅@触发：本条未@她、也不是回复她')
            return

        forced = parsed.forced
        if not forced:
            if not await self._pre_judge_gate(cfg, parsed, gid, group_row):
                self._live('gate', gid, parsed.chat_type,
                           stage='heuristic', allowed=False,
                           reason='粗筛/初筛脚本未命中，未调判官')
                return
            if cfg.get('judge_on', True):
                if not await self._judge(cfg, parsed, gid):
                    self._live('gate', gid, parsed.chat_type,
                               stage='judge', allowed=False,
                               reason='判官判定不值得回复')
                    return

        # 触发通过：用户消息落库（@ 的 QQ 号解析成群友昵称、回复加引用前缀）
        display_text = self._user_display_text(parsed)
        storage.insert_message(
            self.p.db_path, gid, 'user', display_text,
            parsed.sender_id, parsed.display_name)
        storage.upsert_group(self.p.db_path, gid, touch=True)

        # 触发通过后才下载媒体（图片直传视觉、附件解析文本、语音按需 ASR）
        attachments = media_in.Attachments()
        if parsed.media:
            sandbox = None
            try:
                make_sandbox = getattr(self.p, 'sandbox', None)
                if callable(make_sandbox):
                    sandbox = make_sandbox(gid)
            except Exception as e:
                self.log(f'入站媒体取工作区失败（视频将跳过）: {e}', 'warn')
            try:
                attachments = await media_in.fetch_attachments(
                    self.p.bot, parsed, cfg, gid, llm=self.p.llm, log=self.log,
                    chat_type=parsed.chat_type, sandbox=sandbox)
            except Exception as e:
                self.log(f'入站媒体处理异常（不影响对话）: {e}', 'warn')

        try:
            await self._run_turn(gid, cfg, parsed, forced, attachments)
            # 成功收尾才推进背景游标：本轮失败时下轮重新看到这些背景（自愈性重复，
            # 优于永久跳过）；后台轮走 handle_wake/trigger，不经过这里
            if getattr(parsed, 'raw_id', 0):
                try:
                    storage.set_raw_cursor(
                        self.p.db_path, gid, int(parsed.raw_id))
                except Exception as e:
                    self.log(f'背景游标推进失败: {e}', 'warn')
        except Exception as e:
            self.log(f'对话轮异常: {e}', 'error')
            if forced:
                try:
                    await self.p.bot.send_message(
                        message='（响应失败，请稍后再试）', target_id=gid,
                        message_type=parsed.chat_type)
                except Exception:
                    pass

    # ── 入群侦查：机器人自己被拉进群 → 通知 AI 并打招呼 ─────────

    async def handle_group_join(self, gid: str, notice) -> None:
        """group_increase 且入群者是机器人自己：把入群事件作为系统通知落库，
        强制触发一轮对话让 AI 在群里开口（不受群 enabled/触发闸门约束）。"""
        row = storage.get_or_create_group(self.p.db_path, gid)
        cfg = resolve_group(self.p.cfg, row.get('settings'))
        if not cfg.get('join_greet_on', True):
            self.log(f'群 {gid} 入群打招呼已关闭，忽略入群事件', 'warn')
            return

        event_time = int(getattr(notice, 'time', 0) or time.time())
        sub = str(getattr(notice, 'sub_type', '') or '')
        op_id = str(getattr(notice, 'operator_id', 0) or '')
        op_part = ''
        if op_id and op_id != '0':
            kind = '邀请人' if sub == 'invite' else '审批管理员'
            op_part = f'，{kind} QQ {op_id}'
        notice_text = (
            f'[系统通知] 你（机器人 QQ {self.p.bot_id}'
            + (f'，昵称「{self.p.bot_name}」' if self.p.bot_name else '')
            + f'）刚刚被邀请加入了本群（群号 {gid}{op_part}），'
            '这是你入群后看到的第一条消息。请像真人刚进群一样自然地和大家打个招呼：'
            '结合你的人设，用一两句话告诉大家你能陪他们做什么'
            '（比如陪聊、答疑、读文档/图片、定时提醒，不要罗列功能清单），'
            '语气轻松别像客服；不要提「系统通知 / 事件 / 触发」这类内部字眼。')

        storage.insert_message(
            self.p.db_path, gid, 'user', notice_text,
            sender_id='', sender_name='系统', name='join-notice',
            created_at=float(event_time))
        storage.upsert_group(self.p.db_path, gid, touch=True)
        self.log(f'群 {gid} 机器人入群通知已送 AI（{sub or "group_increase"}）', 'ok')

        parsed = ParsedMessage(
            text=notice_text, sender_id='', sender_name='系统',
            at_me=True, chat_type='group', event_time=event_time)
        try:
            await self._run_turn(gid, cfg, parsed, True, None,
                                 raw_bg_notice=True, source_kind='greet')
        except Exception as e:
            self.log(f'入群打招呼失败: {e}', 'error')
            try:
                await self.p.bot.send_message(
                    message='大家好呀，我是新来的 AI 助手，有事 @ 我就行～',
                    target_id=gid, message_type='group')
            except Exception:
                pass

    # ── 定时概率唤醒（proactor）────────────────────────────────

    async def handle_wake(self, gid: str, cfg: dict,
                          now=None, poll: dict | None = None,
                          speakup: bool = False,
                          idle_seconds: float = 0) -> dict:
        """定时苏醒：合成系统消息跑一轮后台对话，模型可回 [静默] 不发群。

        poll 非空时是某条 AI 自建轮询触发（带具体任务）；否则是旧版通用苏醒。
        poll 触发时继承登记人的身份（sender_id/sender_name），让这一轮的工具
        调用按登记人权限判定（主人登记 → is_owner=True），而不是当作陌生人。
        speaker 仍是「系统」：通知的渲染/历史语义不变，只有工具上下文拿身份。
        speakup=True 时是主动搭话：通知用「自己心里冒出来的念头」防错位框，
        bg_runs 记 speakup，心情按 talk 口径判。
        全程写 bg_runs 留痕（后台任务查看页）；返回运行摘要。
        """
        from .proactor import wake_user_text, speakup_user_text
        import datetime as _dt
        now = now or _dt.datetime.now()
        store = getattr(self.p, 'prompts', None)
        if speakup:
            notice_text = speakup_user_text(now, idle_seconds, store)
            notice_name, run_kind, source_kind = (
                'speakup-notice', 'speakup', 'talk')
        else:
            notice_text = wake_user_text(
                now, poll['task'] if poll else None, store)
            notice_name, run_kind, source_kind = (
                'wake-notice', 'wake', 'work')
        storage.insert_message(
            self.p.db_path, gid, 'user', notice_text,
            sender_id='', sender_name='系统', name=notice_name)
        storage.upsert_group(self.p.db_path, gid, touch=True)
        parsed = ParsedMessage(
            text=notice_text, sender_id='', sender_name='系统',
            at_me=True, chat_type='group', allow_silent=True)
        run_id = storage.bg_run_start(
            self.p.db_path, run_kind, 'group', gid, notice_text,
            started_at=now.timestamp(),
            poll_id=int(poll['id']) if poll else 0)
        trace: list = []
        # 轮询继承登记人身份：登记人可能是主人，也可能是普通群友；空值
        #（旧数据）保持非主人。主动搭话/通用苏醒没有登记人，不继承。
        actor_id = str(poll.get('creator_id') or '') if poll else ''
        actor_name = str(poll.get('creator_name') or '') if poll else ''
        try:
            res = await self._run_turn(
                gid, cfg, parsed, True, None, background=True, trace=trace,
                raw_bg_notice=True, source_kind=source_kind,
                actor_id=actor_id, actor_name=actor_name)
        except Exception as e:
            storage.bg_run_finish(
                self.p.db_path, run_id, 'error', '', trace, str(e),
                keep=int(cfg.get('bg_runs_keep', 200)))
            raise
        status = ('silent' if res['silent']
                  else ('empty' if not res['final_text'] else 'ok'))
        storage.bg_run_finish(
            self.p.db_path, run_id, status, res['final_text'], trace,
            keep=int(cfg.get('bg_runs_keep', 200)))
        return {'run_id': run_id, 'status': status, **res}

    async def handle_trigger(self, gid: str, cfg: dict, text: str,
                             source: str = '', now=None) -> dict:
        """外部监测程序回调：把事件作为系统通知跑一轮后台对话。

        与 handle_wake 同构（可 [静默]、bg_runs 留痕、群锁内调用），
        区别只在通知来源：这里是 watcher 经触发接口推送的具体事件。
        """
        from .triggers import trigger_user_text
        import datetime as _dt
        now = now or _dt.datetime.now()
        text = (text or '').strip()
        source = (source or '').strip()
        notice_text = trigger_user_text(now, source, text)
        storage.insert_message(
            self.p.db_path, gid, 'user', notice_text,
            sender_id='', sender_name='系统', name='trigger-notice')
        storage.upsert_group(self.p.db_path, gid, touch=True)
        parsed = ParsedMessage(
            text=notice_text, sender_id='', sender_name='系统',
            at_me=True, chat_type='group', allow_silent=True)
        prompt = f'[{source or "watcher"}] {text}'[:4000]
        run_id = storage.bg_run_start(
            self.p.db_path, 'trigger', 'group', gid, prompt,
            started_at=now.timestamp())
        trace: list = [{'k': 'trigger', 'source': source[:40],
                        'text': text[:500]}]
        try:
            res = await self._run_turn(
                gid, cfg, parsed, True, None, background=True, trace=trace,
                raw_bg_notice=True, source_kind='work')
        except Exception as e:
            storage.bg_run_finish(
                self.p.db_path, run_id, 'error', '', trace, str(e),
                keep=int(cfg.get('bg_runs_keep', 200)))
            raise
        status = ('silent' if res['silent']
                  else ('empty' if not res['final_text'] else 'ok'))
        storage.bg_run_finish(
            self.p.db_path, run_id, status, res['final_text'], trace,
            keep=int(cfg.get('bg_runs_keep', 200)))
        return {'run_id': run_id, 'status': status, **res}

    # ── @ / 回复感知 ────────────────────────────────────────────

    def _name_of(self, qq: str) -> str:
        """QQ 号 → 群友最新昵称/名片（机器人优先用自身昵称）。"""
        if not qq:
            return ''
        if str(qq) == str(self.p.bot_id):
            return self.p.bot_name or '机器人'
        row = storage.get_user(self.p.db_path, qq)
        names = (row or {}).get('names') or []
        return str(names[0]) if names else ''

    async def _roles_block(self, gid: str, parsed, store) -> str:
        """拼本轮在场者的本群身份块；RPC 全部失败/私聊返回 ''。

        发言人 + 被 @ 者各一行，她自己一行（带 @全体成员 权限提示）。
        """
        if parsed.chat_type != 'group':
            return ''
        from .mentions import describe_member
        mgr = getattr(self.p, 'member_info', None)
        if mgr is None:
            return ''
        bot = self.p.bot
        bot_id = str(self.p.bot_id or '')

        # (QQ, 展示名)，去重保序；她自己单列
        targets: list[tuple[str, str]] = []
        seen = set()

        def _add(qq, name):
            qq = str(qq or '')
            if not qq or qq == bot_id or qq in seen:
                return
            seen.add(qq)
            targets.append((qq, (name or '').strip()))

        _add(parsed.sender_id, parsed.display_name)
        for qq in parsed.at_users:
            _add(qq, (parsed.at_names or {}).get(qq))

        async def _line(qq: str, name: str, is_self: bool) -> str:
            info = await mgr.info(gid, qq, bot)
            if not info:
                return ''
            if is_self:
                tail = ('——可以调用 qq_at_all @全体成员'
                        if info['role'] in ('owner', 'admin')
                        else '——普通成员，没有 @全体成员 权限，别调 qq_at_all')
                return f'你自己(QQ:{qq})：{describe_member(info)}{tail}'
            shown = name or info.get('card') or info.get('nickname') or qq
            return f'{shown}(QQ:{qq})：{describe_member(info)}'

        coros = [_line(qq, nm, False) for qq, nm in targets]
        if bot_id:
            coros.append(_line(bot_id, '', True))
        if not coros:
            return ''
        lines = [ln for ln in await asyncio.gather(*coros) if ln]
        if not lines:
            return ''
        return prompts_mod.render('roles_header', store,
                                  content='\n'.join(lines))

    async def _at_map(self, gid: str, cfg: dict, text: str):
        """发送出口用的 昵称→QQ 映射；无需艾特/未开启/取目录失败时返回 None。"""
        if not text or '@' not in text or not cfg.get('real_at_on', True):
            return None
        directory = getattr(self.p, 'members', None)
        if directory is None:
            return None
        try:
            return await directory.resolve(gid, self.p.bot, self.p.db_path)
        except Exception:
            return None

    def _user_display_text(self, parsed) -> str:
        """把 @裸QQ 替换成 @昵称；回复消息加 [回复 某人：原内容] 前缀。"""
        text = parsed.text or ''
        for qq in parsed.at_users:
            name = (parsed.at_names or {}).get(qq) or self._name_of(qq)
            if name:
                text = text.replace(f'@{qq}', f'@{name}')

        if parsed.reply_id:
            if parsed.reply_to_bot:
                who = self.p.bot_name or '机器人'
            else:
                who = (parsed.reply_to_name
                       or self._name_of(parsed.reply_to_id)
                       or parsed.reply_to_id)
            quote = f'：{parsed.reply_excerpt}' if parsed.reply_excerpt else ''
            text = f'[回复 {who}{quote}] {text}'.strip()
        return text

    def _mood_snapshot(self, gid: str, cfg: dict) -> tuple:
        """回忆链用的心情快照：(最强且≥20 的七维标签, 心情值取整)；失败返回 ('', 0)。"""
        if getattr(self.p, 'mood', None) is None:
            return '', 0
        try:
            st = storage.mood_get_state(
                self.p.db_path, gid, float(cfg.get('mood_baseline', 70)))
            dims = st.get('dims') or {}
            tag, val = '', 0
            for k, v in dims.items():
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                if v >= 20 and v > val:
                    tag, val = k, v
            return tag, int(round(float(st.get('mood', 0) or 0)))
        except Exception:
            return '', 0

    @staticmethod
    def _last_human_text(window: list) -> str:
        """窗口里最后一条真人（非系统合成通知）消息文本；没有则 ''。"""
        for r in reversed(window):
            if r.get('role') != 'user' or not (r.get('sender_id') or ''):
                continue
            if (r.get('name') or '') in storage._HISTORY_HIDDEN_NAMES:
                continue
            return r.get('content') or ''
        return ''

    def build_interrupt_item(self, parsed, content: str) -> dict:
        """打断指令正文 → 插队消息（@裸QQ 同样解析成 @昵称，与正常落库口径一致）。"""
        text = content or ''
        for qq in parsed.at_users:
            name = (parsed.at_names or {}).get(qq) or self._name_of(qq)
            if name:
                text = text.replace(f'@{qq}', f'@{name}')
        return {'sender_id': str(parsed.sender_id or ''),
                'sender_name': (parsed.display_name
                                or str(parsed.sender_id or '')),
                'text': text.strip()}

    def _inject_interrupts(self, gid: str, messages: list, items: list,
                           trace: list | None = None,
                           turn_id: str = '',
                           chat_type: str = 'group') -> None:
        """安全点：把插队群友消息落库并追加到进行中的 LLM 消息列表。

        不直接改已完成的工具链，只在「工具结果之后、下一次请求模型之前」追加，
        消息顺序仍然合法。
        """
        if not items:
            return
        now_ts = time.time()
        tag = ctx_mod.time_tag(now_ts)
        messages.append({'role': 'system', 'content':
            f'{tag}（任务执行中收到群友临时插话打断：请优先采纳下面的新指示'
            '调整当前做法；新指示没说取消，就继续把原任务做完）'})
        for it in items:
            storage.insert_message(
                self.p.db_path, gid, 'user', it['text'],
                it['sender_id'], it['sender_name'], created_at=now_ts)
            who = ctx_mod.speaker_label(
                {'sender_id': it['sender_id'], 'sender_name': it['sender_name']})
            messages.append({'role': 'user',
                             'content': f'{tag}{who}: {it["text"]}'})
            self.log(f'群 {gid} 执行中被 {who} 插话打断：'
                     f'{self._snippet(it["text"])}', 'warn')
            if trace is not None:
                trace.append({'k': 'interrupt', 'from': who,
                              'qq': str(it['sender_id']), 'text': it['text'][:500]})
            self._live('prompt', gid, chat_type, turn_id,
                       sender_id=str(it['sender_id'] or ''),
                       sender_name=it['sender_name'], text=it['text'],
                       how='interrupt')

    # ── 判官 ────────────────────────────────────────────────────

    @staticmethod
    def _who(parsed) -> str:
        return parsed.display_name or str(parsed.sender_id)

    @staticmethod
    def _snippet(text: str) -> str:
        text = ' '.join((text or '').split())
        return text if len(text) <= 40 else text[:40] + '…'

    @staticmethod
    def _clip(text, limit: int = 300) -> str:
        """日志用：空白折叠 + 截断，超长标明剩余字数。"""
        text = ' '.join(str(text or '').split())
        return text if len(text) <= limit else text[:limit] + f'…(+{len(text) - limit}字)'

    def _log_model_output(self, gid: str, result, rounds: int):
        tag = f'群 {gid} AI 输出' + (f'（工具轮{rounds}）' if rounds else '')
        if result.reasoning:
            self.log(f'{tag} 思考：{self._clip(result.reasoning, 150)}')
        for tc in result.tool_calls:
            args = json.dumps(tc.get('arguments') or {}, ensure_ascii=False)
            self.log(f'{tag} 调用工具 {tc["name"]}，参数：{self._clip(args)}', 'ok')
        if result.content:
            self.log(f'{tag} 正文：{self._clip(result.content, 500)}')
        if not result.tool_calls and not result.content:
            self.log(f'{tag} 空输出（finish_reason={result.finish_reason or "?"}）',
                     'warn')

    async def _pre_judge_gate(self, cfg, parsed, gid: str,
                              group_row: dict) -> bool:
        """便宜初筛层：内置启发式 OR AI 初筛脚本，任一放行才拉起 LLM 判官。

        两层都不存在（启发式被关且没有任何启用脚本）时视为无门控，
        保持旧行为：消息直通判官。脚本异常/超时一律 fail-closed（按未命中）。
        """
        head = (f'群 {gid} {self._who(parsed)}：'
                f'「{self._snippet(parsed.text)}」')
        heur_on = cfg.get('heuristic_on', True)
        if heur_on:
            aliases = parse_aliases(self.p.cfg.get('aliases'))
            aliases += parse_aliases(group_row.get('aliases'))
            if _hit_heuristic(parsed.text, aliases, self.p.bot_name):
                return True
        screen_state = 'none'
        if cfg.get('screen_on', True):
            screen_state = await self._screen_gate(cfg, parsed, gid)
            if screen_state == 'hit':
                return True
        # 没有任何启用的便宜初筛层时不拦（旧行为：启发式关=判官看全部消息）
        if not heur_on and screen_state == 'none':
            return True
        self.log(f'{head} → 粗筛未命中（初筛脚本也未放行；未调判官，不回复）',
                 'warn')
        return False

    async def _screen_gate(self, cfg, parsed, gid: str) -> str:
        """跑本群+全局启用中的初筛脚本。返回 'hit' | 'miss' | 'none'。

        所有脚本并发、各自短超时；任何失败按未命中并记日志，绝不抛出。"""
        mgr = getattr(self.p, 'screens', None)
        if mgr is None:
            return 'none'
        try:
            rows = await asyncio.to_thread(
                storage.list_screen_scripts, self.p.db_path, gid, True)
        except Exception as e:
            self.log(f'初筛脚本读取失败（本层跳过）: {e}', 'warn')
            return 'none'
        if not rows:
            return 'none'
        timeout = float(cfg.get('screen_call_timeout', 5))
        payload = {
            'group_id': str(gid),
            'user_id': str(parsed.sender_id or ''),
            'message_raw': parsed.text or '',
        }

        async def _one(row):
            try:
                return row, await asyncio.wait_for(
                    asyncio.to_thread(mgr.run_screen, row, payload, timeout),
                    timeout=timeout + 15)
            except asyncio.TimeoutError:
                return row, {'allow': False, 'error': f'外层超时 {timeout + 15:g}s'}
            except Exception as e:
                return row, {'allow': False, 'error': f'执行器异常: {e}'}

        results = await asyncio.gather(*[_one(r) for r in rows])
        hit_names, errs = [], []
        for row, res in results:
            if res.get('allow'):
                hit_names.append(row['name'])
                try:
                    await asyncio.to_thread(
                        storage.bump_screen_hit, self.p.db_path, row['id'])
                except Exception:
                    pass
            elif res.get('error'):
                errs.append(f'{row["name"]}: {str(res["error"])[:160]}')
        for e in errs:
            self.log(f'初筛脚本失败（按未命中）：{e}', 'warn')
        if hit_names:
            self.log(f'群 {gid} {self._who(parsed)}：'
                     f'「{self._snippet(parsed.text)}」'
                     f'→ 初筛脚本命中：{"、".join(hit_names)}，拉起判官', 'ok')
            return 'hit'
        return 'miss'

    async def _judge(self, cfg, parsed, gid: str) -> bool:
        user_text = parsed.text
        rows = storage.recent_messages(
            self.p.db_path, gid, int(cfg.get('judge_history', 4)) * 2)
        script = ctx_mod.render_script(rows)
        head = f'群 {gid} {self._who(parsed)}：「{self._snippet(user_text)}」'
        try:
            should, reason, usage = await self.p.llm.judge(cfg, script, user_text)
            storage.add_usage(self.p.db_path, {
                'group_id': gid, 'kind': 'judge', 'ok': True, **usage})
            verdict = '回复' if should else '不回复'
            why = f'（{reason}）' if reason else ''
            self.log(f'判官 {head} → {verdict}{why}', 'ok' if should else 'warn')
            return should
        except Exception as e:
            fail_open = bool(cfg.get('judge_fail_open', False))
            self.log(f'判官 {head} → 调用失败: {e}，'
                     f'{"放行回复" if fail_open else "拦截不回复"}', 'error')
            storage.add_usage(self.p.db_path, {
                'group_id': gid, 'kind': 'judge', 'ok': False})
            return fail_open

    # ── 一轮完整对话 ────────────────────────────────────────────

    async def _run_turn(self, gid: str, cfg: dict, parsed: ParsedMessage,
                        forced: bool, attachments=None, background: bool = False,
                        trace: list | None = None,
                        raw_bg_notice: bool = False,
                        source_kind: str = 'human',
                        loop_ctx: dict | None = None,
                        actor_id: str = '', actor_name: str = '') -> dict:
        """一轮完整对话。返回 {final_text, silent, sent_texts}；
        trace 非 None 时往里面追加后台留痕步骤（interim/tool/final）。
        raw_bg_notice=True 时后台触发轮看到系统通知原文（苏醒/入群指令）。
        source_kind 标记本轮来源（human/talk/work/greet），心情判定按它钳幅度。
        actor_id/actor_name 是合成通知轮的「委托人」身份（如轮询登记人）：
        只用于工具上下文（主人门禁/归属判定），不覆盖 parsed 的发送人，
        通知在历史里仍按「系统」渲染。"""
        turn_id = uuid.uuid4().hex[:12]
        self._live('turn_start', gid, parsed.chat_type, turn_id,
                   source=source_kind, background=bool(background),
                   forced=bool(forced),
                   sender_id=str(parsed.sender_id or ''),
                   sender_name=parsed.display_name,
                   text=self._user_display_text(parsed))
        persona = resolve_persona(cfg, storage.get_group(self.p.db_path, gid)
                                  .get('persona', ''))
        phash = ctx_mod.persona_hash(persona)

        # 1) 上下文：锚定窗口 + 节流摘要（前缀缓存友好）
        summary_text, window = await self._prepare_context(
            gid, cfg, persona, phash)

        # 2) 记忆注入（发言者 + 被 @ 的人）
        participants = [u for u in [parsed.sender_id, *parsed.at_users]
                        if u and str(u) != str(self.p.bot_id)]
        window_script = ctx_mod.render_script(window)
        mem_block = ''
        if self.p.memory and cfg.get('memory_on', True):
            mem_block = self.p.memory.recall_block(
                participants, parsed.text, cfg, window_script)

        # 2a) 她自己的第一人称记忆（全局一份，跨群/私聊共享）。
        # 后台轮的触发语是内部通知，不能拿它当查询；用窗口里最后一条真人消息。
        self_block = ''
        self_mgr = getattr(self.p, 'self_memory', None)
        if self_mgr is not None and cfg.get('self_memory_on', True):
            try:
                if background or raw_bg_notice:
                    sq = self._last_human_text(window)
                else:
                    sq = parsed.text or ''
                mood_tag, mood_level = self._mood_snapshot(gid, cfg)
                self_block = self_mgr.recall_block(
                    sq, cfg, window_script, group_id=gid,
                    source=source_kind, mood_tag=mood_tag,
                    mood_level=mood_level)
            except Exception as e:
                self.log(f'自我记忆召回失败（本轮跳过）: {e}', 'warn')

        store = getattr(self.p, 'prompts', None)
        # 2a2) 本群身份：发言人/被 @ 者/她自己的群主·管理员·等级·头衔（仅群聊）
        roles_block = ''
        try:
            roles_block = await self._roles_block(gid, parsed, store)
        except Exception as e:
            self.log(f'本群身份读取失败（本轮跳过）: {e}', 'warn')
        # 2b) 群体说话风格 + 当事人未结约定
        style_block = ''
        if cfg.get('learn_style_on', True) and self.p.style is not None:
            style_text = self.p.style.get_text(gid)
            if style_text:
                style_block = prompts_mod.render(
                    'style_header', store, content=style_text)
        task_block = ''
        if self.p.tasks is not None:
            task_block = self.p.tasks.open_block(gid, participants)

        # 2c) AI 自我提示词注入（学到技能/沉淀素材后自己写下的每轮提醒）
        inject_block = ''
        if cfg.get('inject_on', True):
            try:
                inj_rows = storage.list_active_injections(self.p.db_path, gid)
                inject_block = ctx_mod.render_injections(
                    inj_rows, int(cfg.get('inject_max_chars', 1500)))
            except Exception as e:
                self.log(f'提示词注入读取失败（本轮跳过）: {e}', 'warn')

        # 3) 工具集（先于系统提示词：has_mcp 决定是否注入媒体发送说明）
        tools, enabled_mcp, enabled_custom = self._build_tools(gid, cfg)
        # 工具目录只按"本群实际启用"的工具名生成（随群工具开关/自定义工具增减）
        custom_items = [
            (k, r.get('description', ''), bool(r.get('owner_only_call')))
            for k, r in enabled_custom.items()]
        catalog = prompts_mod.build_catalog(
            {t['function']['name'] for t in tools},
            custom_items, bool(enabled_mcp))

        # 4) 组装消息
        system_msg = self._system_prompt(
            persona, mem_block, style_block, task_block, cfg,
            has_tools=bool(tools), has_mcp=bool(enabled_mcp),
            bot_id=self.p.bot_id, bot_name=self.p.bot_name,
            chat_type=parsed.chat_type, inject_block=inject_block,
            store=store, catalog=catalog)
        messages = [system_msg]
        if summary_text:
            messages.append({'role': 'system',
                             'content': prompts_mod.render(
                                 'summary_header', store,
                                 content=summary_text)})
        # PHI（历史前指令）：对话历史之前的自定义 system 指令。
        # 只组装进本轮请求，绝不写消息表（不落库）；留空不注入
        phi = prompts_mod.render('phi', store).strip()
        if phi:
            messages.append({'role': 'system', 'content': phi})
        # 长跑：上一轮/前几轮压出来的滚动进展纪要跨续跑轮带在对话窗正前方
        # （本块不在 DB；_maybe_compact 认得此前缀，下次压缩会把它并入纪要区）
        if loop_ctx is not None:
            # 消息按 DB 窗口重建过，上个长跑轮末尾的 token 采样不代表当前体量，
            # 清掉防轮首误压（学到的 context_limit 保留）
            loop_ctx['last_prompt_tokens'] = 0
            loop_ctx['last_weight'] = 0
            if loop_ctx.get('compact_note'):
                messages.append({'role': 'system',
                                 'content': '【长跑任务进展纪要（上下文自动压缩）】\n'
                                            + loop_ctx['compact_note']})
        messages.extend(ctx_mod.render_openai(
            window, raw_bg_notice=raw_bg_notice))

        # 每轮易变块（自我记忆/本群身份/背景/心情/时钟/梗指南）全部挪到
        # 对话历史之后合成一条：它们每轮都在变，放在历史前会让前缀在几千
        # token 处断裂，后面的历史全部按未命中计费；放到尾部后只重算尾段。
        live_parts = [self_block, roles_block]
        # 触发本轮前的未触发群聊（仅前台真人轮；后台苏醒/入群/外部触发不注入）
        if (not background and not raw_bg_notice
                and cfg.get('ambient_context_on', True)
                and getattr(parsed, 'raw_id', 0)):
            try:
                cur = storage.get_raw_cursor(self.p.db_path, gid)
                amb_rows = storage.ambient_raw_rows(
                    self.p.db_path, gid, cur, parsed.raw_id,
                    int(cfg.get('ambient_context_n', 8)))
                live_parts.append(ctx_mod.render_ambient(
                    amb_rows, int(cfg.get('ambient_context_n', 8)),
                    header=prompts_mod.render('ambient_header', store)))
            except Exception as e:
                self.log(f'背景消息读取失败（本轮跳过）: {e}', 'warn')
        # 上一轮判出、还没对她说过的心情背景（前台/后台都注入；一次变化只注入一次）
        mood_mgr = getattr(self.p, 'mood', None)
        if mood_mgr is not None and cfg.get('mood_on', True):
            try:
                live_parts.append(mood_mgr.render_injection(gid, cfg))
            except Exception as e:
                self.log(f'心情背景注入失败（本轮跳过）: {e}', 'warn')
        # 当前时间行放在尾部块末端：既给她准确时间，也借近因效应压住
        # "回复开头盖时间戳"的模仿；分钟级变化只影响尾部缓存，不碰前缀
        lt = time.localtime()
        live_parts.append(prompts_mod.render(
            'clock_line', store,
            datetime=time.strftime('%Y-%m-%d %H:%M', lt),
            weekday='一二三四五六日'[lt.tm_wday]))
        # 梗指南：前台真人轮扫描本轮消息，命中把梗释义注入尾部块（不落库）；
        # 后台苏醒/触发轮与判官不扫。meme.db 更新后索引按文件签名自动刷新。
        meme_guide = getattr(self.p, 'meme_guide', None)
        if (meme_guide is not None and not background and not raw_bg_notice
                and cfg.get('meme_guide_on', True)):
            try:
                live_parts.append(meme_guide.injection_block(
                    self._user_display_text(parsed), cfg, store=store))
            except Exception as e:
                self.log(f'梗指南匹配失败（本轮跳过）: {e}', 'warn')
        live_text = '\n\n'.join(p.strip() for p in live_parts
                                if p and p.strip())
        if live_text:
            messages.append({'role': 'system',
                             'content': ctx_mod.LIVE_CTX_HEADER
                                        + '\n' + live_text})
        _attach_inbound_media(messages, attachments)

        # 5) 工具循环
        sent_texts: set[str] = set()
        # 引用回复：本轮所有发送出口共用一份状态，只有第一条消息带引用
        # （后台合成轮 message_id 为空，即使开关开着也不会引用）
        quote_state = {
            'id': (str(getattr(parsed, 'message_id', '') or '')
                   if cfg.get('quote_reply_on', False) else ''),
            'used': False,
        }
        # 委托人身份：真人轮用 parsed 的发送人；合成通知轮（发送人为空，
        # 如轮询触发）回落到 actor_id/actor_name，仅影响工具上下文的权限
        # 与归属判定（is_owner、任务登记人等），不影响历史里的「系统」渲染。
        # parsed.display_name 对空发送人返回「系统」，不能直接拿来当名字。
        ctx_sender_id = (str(parsed.sender_id or '')
                         or str(actor_id or ''))
        ctx_sender_name = ((parsed.display_name if parsed.sender_id else '')
                           or str(actor_name or ''))
        tool_ctx = ToolContext(
            bot=self.p.bot, group_id=gid, cfg=cfg,
            sandbox=self.p.sandbox(gid), db_path=self.p.db_path,
            chat_type=parsed.chat_type, memory=self.p.memory,
            self_memory=getattr(self.p, 'self_memory', None),
            tasks=self.p.tasks, scheduler=getattr(self.p, 'scheduler', None),
            proactor=getattr(self.p, 'proactor', None),
            watchers=getattr(self.p, 'watchers', None),
            qzone=getattr(self.p, 'qzone', None),
            mcp=getattr(self.p, 'mcp', None),
            custom=getattr(self.p, 'custom', None),
            screens=getattr(self.p, 'screens', None),
            requests=getattr(self.p, 'requests', None),
            member_info=getattr(self.p, 'member_info', None),
            loop=getattr(self.p, 'loop', None),
            meme_guide=getattr(self.p, 'meme_guide', None),
            llm=self.p.llm,
            loop_ctx=loop_ctx,
            bot_id=str(self.p.bot_id or ''),
            sent_texts=sent_texts, log=self.log,
            quote_state=quote_state,
            sender_id=ctx_sender_id, sender_name=ctx_sender_name)

        # 5) 工具循环。本轮注册为「可打断活动轮」：/ai 打断 投来的群友消息
        # 在每次请求模型前的安全点注入，可临时调整执行中的任务
        # 后台触发（定时苏醒等）工具预算独立，避免无人值守时长链失控烧 token
        # loop 长跑轮没有工具轮上限（节奏由 loop_every_rounds 的强制汇报保证）
        max_rounds = None if loop_ctx is not None else int(cfg.get(
            'background_max_tool_rounds' if background else 'max_tool_rounds',
            8 if background else 6))
        hub, silent = self.hub, False
        turn_token = hub.begin_turn(gid)
        try:
            final_text = await self._chat_loop(
                gid, cfg, parsed, messages, tool_ctx, tools,
                enabled_mcp, enabled_custom, max_rounds,
                trace=trace, token=turn_token, sent_texts=sent_texts,
                persona=persona, loop_ctx=loop_ctx, turn_id=turn_id)
            final_text, silent = await self._emit_final(
                gid, cfg, parsed, final_text, sent_texts, turn_id=turn_id,
                quote_state=quote_state)

            # 收尾回复期间才到达的插队：同轮内再处理几波，让临时调整立即生效，
            # 而不是等下一轮对话才被看到
            grace_left = max(0, int(cfg.get('interrupt_grace_rounds', 2)))
            while (not silent and grace_left > 0
                   and hub.pending(gid, turn_token)):
                items = hub.drain(gid, turn_token)
                if not items:
                    break
                self._inject_interrupts(gid, messages, items, trace,
                                        turn_id=turn_id,
                                        chat_type=parsed.chat_type)
                grace_left -= 1
                extra = await self._chat_loop(
                    gid, cfg, parsed, messages, tool_ctx, tools,
                    enabled_mcp, enabled_custom, max_rounds,
                    trace=trace, token=turn_token, sent_texts=sent_texts,
                    persona=persona, loop_ctx=loop_ctx, turn_id=turn_id)
                final_text, silent = await self._emit_final(
                    gid, cfg, parsed, extra, sent_texts, turn_id=turn_id,
                    quote_state=quote_state)
        finally:
            # 极窄竞态：最终收尾后又有插队到达、宽限波次已用尽。落库保留，
            # 下一轮前台对话能看到这条真人消息（不会凭空丢失）
            leftover = hub.end_turn(gid, turn_token)
            for it in leftover:
                try:
                    storage.insert_message(
                        self.p.db_path, gid, 'user', it['text'],
                        it['sender_id'], it['sender_name'])
                except Exception:
                    pass
            if leftover:
                self.log(f'群 {gid} {len(leftover)} 条插队消息晚到一步，'
                         '已落入历史，未在本轮处理', 'warn')

        # 7) 记忆异步抽取（不阻塞；静默轮没有新发言，跳过省 token）
        if not silent:
            if self.p.memory and cfg.get('memory_on', True):
                self.p.memory.schedule_extraction(gid, cfg, self.p.llm)
            if self.p.style and cfg.get('learn_style_on', True):
                self.p.style.schedule_check(gid, cfg, self.p.llm)
            # 心情判定：每轮一次便宜模型调用，结果只影响下一轮
            if mood_mgr is not None and cfg.get('mood_on', True):
                trigger_text = (parsed.text if (background or raw_bg_notice)
                                else self._user_display_text(parsed))
                try:
                    mood_mgr.schedule_judge(
                        gid, cfg, self.p.llm, source_kind=source_kind,
                        trigger_text=trigger_text or '',
                        reply_text=final_text or '',
                        context_script=window_script)
                except Exception as e:
                    self.log(f'心情判定调度失败（不影响回复）: {e}', 'warn')

        if trace is not None:
            trace.append({'k': 'final', 'silent': silent,
                          'text': final_text[:2000]})
        self._live('turn_end', gid, parsed.chat_type, turn_id,
                   silent=bool(silent), final_text=final_text or '')
        return {'final_text': final_text, 'silent': silent,
                'sent_texts': sorted(sent_texts)}

    async def _chat_loop(self, gid, cfg, parsed, messages, tool_ctx, tools,
                         enabled_mcp, enabled_custom, max_rounds, *,
                         trace, token, sent_texts,
                         persona: str = '', loop_ctx: dict | None = None,
                         turn_id: str = '') -> str:
        """请求模型 → 派发工具的循环，直到模型给出无工具调用的终态文案。

        每轮请求模型前在安全点取走插队消息（/ai 打断），让群友能临时调整
        正在执行的任务；返回终态正文（未剥 nospeak、未发送）。
        loop_ctx 非 None 为长跑轮：无工具轮上限、自动压缩上下文、按
        loop_every_rounds 强制汇报进展。
        """
        rounds, current_tools = 0, tools
        compact_retries = 0
        while True:
            items = self.hub.drain(gid, token)
            if items:
                self._inject_interrupts(gid, messages, items, trace,
                                        turn_id=turn_id,
                                        chat_type=parsed.chat_type)
                # 新指示可能要重新动手：恢复被轮次上限摘掉的工具
                current_tools = tools
            req_weight = 0
            if loop_ctx is not None:
                req_weight = self._messages_weight(messages)
                try:
                    await self._maybe_compact(
                        gid, cfg, messages, persona, loop_ctx, trace,
                        turn_id=turn_id, chat_type=parsed.chat_type,
                        weight=req_weight,
                        est_tokens=self._estimate_prompt_tokens(
                            loop_ctx, req_weight))
                    # 压缩会改写 messages，请求前重量以压缩后的为准
                    req_weight = self._messages_weight(messages)
                except Exception as e:
                    self.log(f'群 {gid} 长跑上下文压缩失败（本轮不压缩）: {e}',
                             'warn')
            try:
                result = await self.p.llm.chat(
                    messages, cfg, tools=current_tools or None)
            except Exception as e:
                # 长跑轮撞上模型上下文上限：学到实际上限 + 强制压缩一次再重试
                # （每个模型请求至多 3 次）
                if (loop_ctx is not None and compact_retries < 3
                        and _is_context_overflow(e)):
                    learned = _parse_context_limit(e)
                    if learned and learned != loop_ctx.get('context_limit'):
                        loop_ctx['context_limit'] = learned
                        self.log(f'群 {gid} 从超长报错学到模型上下文上限 '
                                 f'{learned} tokens，后续将按真实 token 水位'
                                 '提前压缩', 'ok')
                    compact_retries += 1
                    try:
                        ok = await self._maybe_compact(
                            gid, cfg, messages, persona, loop_ctx, trace,
                            force=True, turn_id=turn_id,
                            chat_type=parsed.chat_type, weight=req_weight)
                    except Exception:
                        ok = False
                    if ok:
                        self.log(f'群 {gid} 请求撞上上下文上限，已强制压缩后'
                                 f'重试（第 {compact_retries} 次）', 'warn')
                        continue
                storage.add_usage(self.p.db_path,
                                  {'group_id': gid, 'kind': 'chat', 'ok': False})
                raise
            compact_retries = 0
            storage.add_usage(self.p.db_path, {
                'group_id': gid, 'kind': 'chat', 'tool_rounds': rounds,
                'ok': True, **result.usage})
            # 记住这次请求的真实 prompt token + 当时体量，供下一轮安全点外推
            # （系统提示词/工具 schema 等固定成本都在真实值里，比字符估算准）
            if loop_ctx is not None:
                tokens_in = int((result.usage or {}).get('tokens_in') or 0)
                if tokens_in > 0:
                    loop_ctx['last_prompt_tokens'] = tokens_in
                    loop_ctx['last_weight'] = req_weight
            self._log_model_output(gid, result, rounds)
            # 实时订阅：本轮模型输出（思考链/正文/工具调用/token）；
            # prompt_view 较重，仅在调试台显式订阅时构造
            prompt_view = ''
            live = getattr(self.p, 'live', None)
            if live is not None and live.bus.has_flag('include_prompt'):
                prompt_view = self._prompt_view(messages)
            self._live(
                'model_round', gid, parsed.chat_type, turn_id,
                round=rounds, content=result.content or '',
                reasoning=result.reasoning or '',
                tool_calls=[{'id': tc.get('id', ''), 'name': tc.get('name', ''),
                             'arguments': tc.get('arguments') or {}}
                            for tc in result.tool_calls],
                usage=result.usage or {}, prompt_view=prompt_view)

            if not result.tool_calls:
                return result.content or ''

            if any(tc['id'].startswith('dsml-') for tc in result.tool_calls):
                names = '、'.join(tc['name'] for tc in result.tool_calls)
                self.log(f'群 {gid} 模型把工具调用写成了 DSML 标签，已兜底还原: {names}',
                         'warn')

            # 模型边干活边说的话：与工具调用同轮返回的正文，剥掉内心戏后
            # 立即分条发群（不等全部工具做完），并计入去重集合
            interim, hidden = reply.split_nospeak(result.content or '')
            if hidden:
                self.log(f'群 {gid} 模型内心戏（不发送）：{self._snippet(hidden)}')

            rounds += 1
            wire = _wire_tool_calls(result.tool_calls)
            assistant_msg = {'role': 'assistant',
                             'content': interim or None,
                             'tool_calls': wire}
            messages.append(assistant_msg)
            storage.insert_message(self.p.db_path, gid, 'assistant', interim,
                                   tool_calls=wire)

            if interim:
                at_map = await self._at_map(gid, cfg, interim)
                n = await reply.send_in_chunks(
                    self.p.bot, gid, interim, cfg,
                    message_type=parsed.chat_type, name_map=at_map,
                    quote_id=reply.take_quote(tool_ctx.quote_state))
                sent_texts.add(interim)
                self.log(f'群 {gid} 工具轮{rounds} 模型插话 {n} 条：'
                         f'{self._clip(interim, 120)}', 'ok')
                if trace is not None:
                    trace.append({'k': 'interim', 'round': rounds,
                                  'text': interim[:500]})
                self._live('assistant_send', gid, parsed.chat_type, turn_id,
                           text=interim, phase='interim', n_chunks=n)

            for norm, w in zip(result.tool_calls, wire):
                tool_name = norm['name']
                tool_args = self._clip(
                    json.dumps(norm.get('arguments') or {},
                               ensure_ascii=False), 300)
                content = await self._dispatch(
                    tool_name, norm.get('arguments') or {}, tool_ctx,
                    cfg, enabled_mcp, enabled_custom)
                # read_image 返回多模态结果：当轮 tool 消息带图片回传视觉，
                # 落库/日志只存文字说明，历史窗不携带 base64
                if isinstance(content, builtin_tools.ToolMediaResult):
                    note, tool_content = content.text, content.to_parts()
                else:
                    note, tool_content = str(content), str(content)
                self.log(f'群 {gid} 工具 {tool_name} 返回：{self._clip(note)}',
                         'warn' if str(note).startswith('错误') else 'info')
                if trace is not None:
                    trace.append({'k': 'tool', 'round': rounds,
                                  'name': tool_name, 'args': tool_args,
                                  'ok': not str(note).startswith('错误'),
                                  'result': self._clip(str(note), 600)})
                self._live('tool_result', gid, parsed.chat_type, turn_id,
                           round=rounds, call_id=norm['id'], name=tool_name,
                           arguments=norm.get('arguments') or {},
                           ok=not str(note).startswith('错误'),
                           result=str(note))
                messages.append({'role': 'tool', 'tool_call_id': norm['id'],
                                 'content': tool_content})
                storage.insert_message(
                    self.p.db_path, gid, 'tool', note,
                    tool_call_id=norm['id'], name=tool_name)

            if loop_ctx is not None:
                # 长跑轮：无上限；但每 N 个工具轮必须说一次进展，说完继续干
                if interim:
                    loop_ctx['since_spoke'] = 0
                else:
                    loop_ctx['since_spoke'] = \
                        loop_ctx.get('since_spoke', 0) + 1
                loop_ctx['total_rounds'] = \
                    loop_ctx.get('total_rounds', 0) + 1
                loop_ctx['tools_this_turn'] = \
                    loop_ctx.get('tools_this_turn', 0) + 1
                every = max(1, int(cfg.get('loop_every_rounds', 5)))
                if loop_ctx['since_spoke'] >= every:
                    await self._force_loop_speech(
                        gid, cfg, parsed, messages, tool_ctx, trace, loop_ctx,
                        turn_id=turn_id)
                    loop_ctx['since_spoke'] = 0
            elif rounds >= max_rounds:
                # 硬上限：摘掉工具，明令只用一句话收尾，避免群里静默
                current_tools = None
                messages.append({'role': 'system', 'content':
                    '工具调用轮次已达上限，本轮你不能再调用任何工具。'
                    '请用一两句简短群聊口语告诉大家当前进展或结果；'
                    '如果事情还没做完，让对方再说一句「继续」你就接着做。'
                    '只输出要说的话。'})
                final_close = await self.p.llm.chat(messages, cfg)
                storage.add_usage(self.p.db_path, {
                    'group_id': gid, 'kind': 'chat', 'tool_rounds': rounds,
                    'ok': True, **final_close.usage})
                self._log_model_output(gid, final_close, rounds)
                self._live(
                    'model_round', gid, parsed.chat_type, turn_id,
                    round=rounds, content=final_close.content or '',
                    reasoning=final_close.reasoning or '',
                    tool_calls=[{'id': tc.get('id', ''),
                                 'name': tc.get('name', ''),
                                 'arguments': tc.get('arguments') or {}}
                                for tc in final_close.tool_calls],
                    usage=final_close.usage or {}, note='forced_close')
                final_text = final_close.content or ''
                if final_close.tool_calls:
                    self.log(f'群 {gid} 已达 {max_rounds} 轮工具上限，'
                             f'收尾时模型仍在请求工具（'
                             + '、'.join(t['name'] for t in final_close.tool_calls)
                             + '），已强制转为文字收尾', 'warn')
                self.log(f'群 {gid} 工具轮次达上限 {max_rounds}，强制收尾',
                         'warn')
                if not final_text.strip():
                    final_text = '（这轮操作步数到上限了，要我继续就说一声）'
                return final_text

    # ── loop 长跑：强制汇报 + 上下文压缩 ────────────────────────

    async def _force_loop_speech(self, gid, cfg, parsed, messages, tool_ctx,
                                 trace, loop_ctx, turn_id: str = '') -> None:
        """连续 N 个工具轮没在群里说话：摘工具强制她汇报一句，说完继续循环。"""
        n = loop_ctx.get('since_spoke', 0)
        directive = (
            f'你已经连续做了 {n} 个工具动作但没在群里说话。现在禁止调用任何工具：'
            '立刻用一两句群聊口语告诉大家当前进展或阶段结果（像真人汇报，'
            '别提「工具/轮次/系统/模式」）。只输出要说的话。')
        messages.append({'role': 'system', 'content': directive})
        try:
            close = await self.p.llm.chat(messages, cfg)
            storage.add_usage(self.p.db_path, {
                'group_id': gid, 'kind': 'chat',
                'tool_rounds': loop_ctx.get('total_rounds', 0),
                'ok': True, **close.usage})
            self._live(
                'model_round', gid, parsed.chat_type, turn_id,
                round=loop_ctx.get('total_rounds', 0),
                content=close.content or '', reasoning=close.reasoning or '',
                tool_calls=[{'id': tc.get('id', ''), 'name': tc.get('name', ''),
                             'arguments': tc.get('arguments') or {}}
                            for tc in close.tool_calls],
                usage=close.usage or {}, note='loop_speech')
            if close.tool_calls:
                self.log(f'群 {gid} 长跑强制汇报时模型仍请求工具（已忽略）：'
                         + '、'.join(t['name'] for t in close.tool_calls), 'warn')
            text, _hidden = reply.split_nospeak(close.content or '')
            text = reply.strip_leading_time(text)
            text = reply.sanitize_model_text(text)
        finally:
            # 指令只是催场，不留在上下文里
            messages.pop()
        if not text.strip():
            self.log(f'群 {gid} 长跑强制汇报为空，本次跳过', 'warn')
            return
        at_map = await self._at_map(gid, cfg, text)
        n_sent = await reply.send_in_chunks(
            self.p.bot, gid, text, cfg,
            message_type=parsed.chat_type, name_map=at_map,
            quote_id=reply.take_quote(tool_ctx.quote_state))
        messages.append({'role': 'assistant', 'content': text})
        storage.insert_message(self.p.db_path, gid, 'assistant', text)
        tool_ctx.sent_texts.add(text)
        self.log(f'群 {gid} 长跑强制汇报 {n_sent} 条：{self._clip(text, 120)}',
                 'ok')
        if trace is not None:
            trace.append({'k': 'loop_speech', 'text': text[:500]})
        self._live('assistant_send', gid, parsed.chat_type, turn_id,
                   text=text, phase='loop_speech', n_chunks=n_sent)

    @staticmethod
    def _messages_weight(messages) -> int:
        """粗估上下文体量（字符）；多模态图片按固定成本计。"""
        total = 0
        for m in messages:
            c = m.get('content')
            if isinstance(c, str):
                total += len(c)
            elif isinstance(c, list):
                for p in c:
                    if isinstance(p, dict):
                        total += 1500 if p.get('type') == 'image_url' \
                            else len(str(p.get('text', '')))
            for tc in (m.get('tool_calls') or []):
                total += len(json.dumps(tc, ensure_ascii=False))
        return total

    @staticmethod
    def _estimate_prompt_tokens(loop_ctx: dict, weight: int) -> int:
        """按上次请求的真实 prompt_tokens 与当时体量，外推当前 prompt token。

        之后只增不减（压缩会令采样失效）；等比放大把系统提示词/工具 schema
        的固定成本也一起放大，因此只会高估、提前压缩，不会顶爆才压。
        没有真实采样前（长跑首轮）返回 0。
        """
        last_tok = int(loop_ctx.get('last_prompt_tokens') or 0)
        last_w = int(loop_ctx.get('last_weight') or 0)
        if last_tok <= 0 or last_w <= 0:
            return 0
        return int(last_tok * max(1.0, weight / last_w))

    @staticmethod
    def _compact_due(cfg: dict, loop_ctx: dict, weight: int,
                     est_tokens: int) -> bool:
        """压缩是否到期：真实 token 水位优先，字符阈值兜底。"""
        if weight >= int(cfg.get('loop_compact_threshold_chars', 40000)):
            return True
        limit = int(cfg.get('loop_context_limit_tokens', 0) or 0) \
            or int(loop_ctx.get('context_limit', 0) or 0)
        if limit > 0 and est_tokens > 0:
            # 总上下文要给回复输出留出 max_tokens，输入预算是两者之差
            budget = limit - int(cfg.get('max_tokens', 30000))
            pct = max(20, min(95, int(cfg.get('loop_compact_percent', 80))))
            if budget > 0 and est_tokens >= budget * pct // 100:
                return True
        return False

    @staticmethod
    def _compact_cut(dialog: list, keep_rounds: int, force: bool) -> int:
        """压缩切点：保留尾部最近 keep_rounds 个工具轮（含完整工具链）。

        返回 dialog 中的切点索引（head=[:cut] 压缩，tail=[cut:] 原样保留）；
        0 表示没有安全的切法（本轮不压）。
        """
        cut = 0
        seen = 0
        for i in range(len(dialog) - 1, -1, -1):
            m = dialog[i]
            if m.get('role') == 'assistant' and m.get('tool_calls'):
                seen += 1
                if seen == keep_rounds:
                    cut = i
                    break
        if cut == 0:
            if not force:
                return 0
            cut = max(1, len(dialog) // 2)
        # tail 不能以孤立 tool 行开头（其 assistant 在 head 里）：切点前移
        while 0 < cut < len(dialog) and dialog[cut].get('role') == 'tool':
            cut -= 1
        # head 不能以「工具结果不全的 assistant(tool_calls)」结尾
        while cut > 0:
            m = dialog[cut - 1]
            if not (m.get('role') == 'assistant' and m.get('tool_calls')):
                break
            ids = {tc['id'] for tc in m['tool_calls']}
            present = {x.get('tool_call_id') for x in dialog[:cut]
                       if x.get('role') == 'tool'}
            if ids.issubset(present):
                break
            cut -= 1
        return cut

    @staticmethod
    def _compact_script(head: list) -> str:
        """把待压缩消息渲染成便宜模型可读的脚本；旧图片直接丢弃。"""
        lines = []
        for m in head:
            role = m.get('role')
            c = m.get('content')
            if isinstance(c, list):
                c = '\n'.join(str(p.get('text', '')) for p in c
                              if isinstance(p, dict) and p.get('type') == 'text')
            c = str(c or '').strip()
            if role == 'system':
                # 只压任务执行中注入的系统内容（插话/旧纪要），跳过其他系统块
                if c.startswith('【长跑') or '插话打断' in c \
                        or '没在群里说话' in c:
                    lines.append(f'[系统] {c[:800]}')
            elif role == 'user':
                lines.append(f'[指示] {c[:1500]}')
            elif role == 'assistant':
                if c:
                    lines.append(f'[我说] {c[:1500]}')
                for tc in (m.get('tool_calls') or []):
                    fn = tc.get('function', {})
                    lines.append(
                        f'  → 工具 {fn.get("name", "?")} '
                        f'参数 {str(fn.get("arguments", ""))[:300]}')
            elif role == 'tool':
                lines.append(f'  ← 结果 {c[:500]}')
        return '\n'.join(lines)

    async def _maybe_compact(self, gid, cfg, messages, persona, loop_ctx,
                             trace=None, force: bool = False,
                             turn_id: str = '',
                             chat_type: str = 'group',
                             weight: int = 0,
                             est_tokens: int = 0) -> bool:
        """长跑轮上下文将满时，把前面的工具过程压成滚动进展纪要。

        只动内存中的 messages（DB 全量历史不受影响，下个对话轮照常重建窗口，
        但纪要本身经 loop_ctx 跨续跑轮注入，见 _run_turn）。
        到期判定：真实 token 水位（配置/自学到的模型上限）优先，字符阈值兜底。
        force=True 用于请求撞上上下文上限后的自救（忽略阈值）。
        """
        weight = weight or self._messages_weight(messages)
        if not force and not self._compact_due(cfg, loop_ctx, weight,
                                               est_tokens):
            return False
        # 本轮实时上下文块常驻消息末尾；它不属于对话过程，先摘出来，
        # 压完再原样放回（否则会被当成对话内容滚进纪要或落在中段）
        live_msg = None
        if (messages and messages[-1].get('role') == 'system'
                and str(messages[-1].get('content') or '').startswith(
                    ctx_mod.LIVE_CTX_HEADER)):
            live_msg = messages.pop()
        try:
            # 开头连续 system 块（系统提示词/摘要/PHI…）整体保留；
            # 但上一轮压缩留下的「进展纪要」要并入压缩区（其内容已随 summarize
            # 滚进新纪要），否则每次压缩都会在前置块里堆积一条过期纪要
            k = 0
            while k < len(messages) and messages[k].get('role') == 'system':
                k += 1
            if (k > 0 and isinstance(messages[k - 1].get('content'), str)
                    and messages[k - 1]['content'].startswith(
                        '【长跑任务进展纪要')):
                k -= 1
            dialog = messages[k:]
            if len(dialog) < 4:
                return False
            cut = self._compact_cut(
                dialog, max(1, int(cfg.get('loop_keep_rounds', 4))),
                force=force)
            if cut <= 0:
                return False
            head, tail = dialog[:cut], dialog[cut:]
            script = self._compact_script(head)
            if not script.strip():
                return False
            script += ('\n\n请在摘要中明确保留：长跑任务目标、已完成步骤与关键结果、'
                       '未完成事项、下一步计划、关键 ID/文件名/群友 QQ。')
            note, usage = await self.p.llm.summarize(
                cfg, persona, loop_ctx.get('compact_note', ''), script[-12000:])
            storage.add_usage(self.p.db_path,
                              {'group_id': gid, 'kind': 'summary',
                               'ok': True, **usage})
            note = (note or '').strip()
            if not note:
                return False
            del messages[k:]
            messages.append({'role': 'system',
                             'content': '【长跑任务进展纪要（上下文自动压缩）】\n'
                                        + note})
            messages.extend(tail)
        finally:
            if live_msg is not None:
                messages.append(live_msg)
        loop_ctx['compact_note'] = note
        loop_ctx['compacts'] = loop_ctx.get('compacts', 0) + 1
        # 采样是压缩前的体量对应的，已失效；等下一个请求回新 usage 再外推
        loop_ctx['last_prompt_tokens'] = 0
        loop_ctx['last_weight'] = 0
        sid = loop_ctx.get('session_id')
        if sid:
            try:
                storage.update_loop_session(
                    self.p.db_path, sid,
                    compacts=loop_ctx['compacts'])
            except Exception:
                pass
        self.log(f'群 {gid} 长跑上下文已压缩（{len(head)} 条 → 纪要 '
                 f'{len(note)} 字，尾部保留 {len(tail)} 条）', 'ok')
        if trace is not None:
            trace.append({'k': 'compact', 'head_msgs': len(head),
                          'tail_msgs': len(tail), 'chars': len(note),
                          'est_tokens': est_tokens, 'forced': force})
        self._live('compact', gid, chat_type, turn_id,
                   head_msgs=len(head), tail_msgs=len(tail),
                   chars=len(note), forced=bool(force))
        return True

    async def _emit_final(self, gid, cfg, parsed, final_text: str,
                          sent_texts: set, turn_id: str = '',
                          quote_state: dict | None = None) -> tuple[str, bool]:
        """[nospeak] 内心戏剥离 + 终态去重 + 分条发送 + 落库。

        返回 (实际正文, 是否静默)。插队触发的续轮共用同一套终态处理；
        quote_state 与插话/工具发送共用，保证一轮只引用一次触发消息。
        """
        final_text, hidden = reply.split_nospeak(final_text)
        # 她偶尔照抄档案时间戳当回复前缀（"[10-26 14:30] 在的"），发群前剥掉；
        # 历史里她自己的行已不再打时间标签，这里只兜底旧习惯漏网的情况
        final_text = reply.strip_leading_time(final_text)
        # 出站净化：模型复读入站 CQ 码会被 NapCat 当成真码（重发图片等），
        # 在发群与落库之前统一转全角
        final_text = reply.sanitize_model_text(final_text)
        if hidden:
            self.log(f'群 {gid} 模型内心戏（不发送）：{self._snippet(hidden)}')
        silent = bool(getattr(parsed, 'allow_silent', False)) \
            and _is_silent_mark(final_text)
        if silent:
            self.log(f'群 {gid} 定时苏醒轮模型选择静默，不发群不入库', 'ok')
        elif final_text and final_text not in sent_texts:
            at_map = await self._at_map(gid, cfg, final_text)
            n = await reply.send_in_chunks(
                self.p.bot, gid, final_text, cfg,
                message_type=parsed.chat_type, name_map=at_map,
                quote_id=reply.take_quote(quote_state))
            storage.insert_message(self.p.db_path, gid, 'assistant',
                                   final_text)
            sent_texts.add(final_text)
            self.log(f'群 {gid} 回复 {n} 条')
            self._live('assistant_send', gid, parsed.chat_type, turn_id,
                       text=final_text, phase='final', n_chunks=n)
        elif not final_text and sent_texts:
            self.log(f'群 {gid} 工具已发言，终态为空，跳过')
        elif hidden:
            self.log(f'群 {gid} 模型选择本轮不说话')
        else:
            self.log(f'群 {gid} 终态为空或重复，未发送')
        return final_text, silent

    # ── 摘要 ────────────────────────────────────────────────────

    async def _prepare_context(self, gid, cfg, persona, phash):
        """锚定窗口 + 节流摘要，返回 (summary_text, window)。

        摘要锚点（summaries.covered_up_to_id）之后的历史只追加、不逐轮滑动：
        两轮之间 messages 的公共前缀（系统提示词 → 摘要 → 历史开头）保持
        字节一致，供应商的前缀缓存才能命中。只有摘要真正重写（锚点前移）
        才会改变前缀，因此重写按「积压阈值 + 最短间隔」节流。
        """
        summary_row = storage.get_summary(self.p.db_path, gid)
        old_summary = (summary_row or {}).get('summary') or ''
        up_to = int((summary_row or {}).get('covered_up_to_id') or 0)
        if summary_row and summary_row.get('persona_hash') != phash:
            return await self._rebuild_summary(
                gid, cfg, persona, phash, old_summary, up_to)

        rows = storage.messages_since(self.p.db_path, gid, up_to)
        plan = ctx_mod.plan_summary(
            rows,
            int(cfg.get('window_rounds', 12)),
            int(cfg.get('char_budget', 6000)),
            min_new_msgs=int(cfg.get('summary_min_new_msgs', 12)),
            min_new_chars=int(cfg.get('summary_min_new_chars', 2000)),
            force_msgs=int(cfg.get('summary_force_msgs', 48)),
            force_chars=int(cfg.get('summary_force_chars', 12000)),
            min_interval_sec=int(cfg.get('summary_min_interval_sec', 600)),
            last_updated_at=float((summary_row or {}).get('updated_at') or 0))
        if not plan['due']:
            return old_summary, plan['window']

        cut = plan['cut']
        overflow = rows[:cut]
        text = await self._write_summary(
            gid, cfg, persona, old_summary,
            ctx_mod.render_script(overflow))
        if text is None:
            # 摘要失败：退回裁剪后的窗口，宁可这一轮前缀断一次，
            # 也不让未摘要历史无限堆进提示词
            return old_summary, plan['window']
        new_up_to = int(overflow[-1]['id'])
        storage.set_summary(self.p.db_path, gid, text, new_up_to, phash)
        storage.mark_summarized(self.p.db_path, gid, new_up_to)
        return text, rows[cut:]

    async def _rebuild_summary(self, gid, cfg, persona, phash, old_summary,
                               up_to):
        """人设变更：旧摘要作废，用新人口吻重算历史（输入封顶 6000 字）。

        摘要失败时不更新人设哈希，下一轮仍会重试。
        """
        rows = storage.messages_since(self.p.db_path, gid, up_to)
        force_msgs = int(cfg.get('summary_force_msgs', 48))
        force_chars = int(cfg.get('summary_force_chars', 12000))
        window = rows
        covered = up_to
        if (len(rows) > force_msgs
                or sum(len(r.get('content') or '') for r in rows) > force_chars):
            window = ctx_mod.select_window(
                rows, int(cfg.get('window_rounds', 12)),
                int(cfg.get('char_budget', 6000)))
            if window:
                covered = int(window[0]['id']) - 1
        cutoff = int(window[0]['id']) if window else 10**18
        old_rows = [r for r in storage.messages_after(
            self.p.db_path, gid, 0, 10**6)
            if r['id'] < cutoff and r['content']][-60:]
        script = ctx_mod.render_script(old_rows)
        if not script and not old_summary:
            storage.set_summary(self.p.db_path, gid, '', covered, phash)
            return '', window
        text = await self._write_summary(
            gid, cfg, persona, old_summary, script)
        if text is None:
            return old_summary, window
        new_up_to = max([int(r['id']) for r in old_rows] + [covered])
        storage.set_summary(self.p.db_path, gid, text, new_up_to, phash)
        if new_up_to:
            storage.mark_summarized(self.p.db_path, gid, new_up_to)
        return text, window

    async def _write_summary(self, gid, cfg, persona, old_summary, script):
        """调摘要模型重写滚动摘要；失败返回 None（调用方保留旧摘要）。"""
        script = (script or '').strip()
        if len(script) > 6000:
            script = script[-6000:]
        try:
            text, usage = await self.p.llm.summarize(
                cfg, persona, old_summary, script)
            storage.add_usage(self.p.db_path, {
                'group_id': gid, 'kind': 'summary', 'ok': True, **usage})
        except Exception as e:
            self.log(f'摘要失败（本轮跳过）: {e}', 'warn')
            storage.add_usage(self.p.db_path,
                              {'group_id': gid, 'kind': 'summary', 'ok': False})
            return None
        return text

    # ── 工具 ────────────────────────────────────────────────────

    def _build_tools(self, gid: str, cfg: dict):
        overrides = storage.get_tool_overrides(self.p.db_path, gid)
        enabled_keys = set(_ALL_TOOL_KEYS)
        enabled_mcp = {}
        if self.p.mcp is not None:
            for s in self.p.mcp.enabled_servers():
                for spec in self.p.mcp.cached_tool_specs(s['id']):
                    # spec 名在缓存时已带 mcp__<sid>__ 前缀，直接使用；
                    # 再包一层会变成 mcp__sid__mcp__sid__tool，导致模型调得到、
                    # dispatch 查不到（曾表现为「工具不可用」）
                    full = spec['function']['name']
                    enabled_mcp[full] = (s['id'], spec)
                    enabled_keys.add(full)
        # 群覆盖
        for key, on in overrides.items():
            if on:
                enabled_keys.add(key)
            else:
                enabled_keys.discard(key)
        # 被群开关关掉的 MCP 工具同时从 schema 与 dispatch 映射里摘除：
        # 模型既看不到，硬调（幻觉/上一轮缓存的工具名）也会落到「工具不可用」
        enabled_mcp = {k: v for k, v in enabled_mcp.items() if k in enabled_keys}
        if not cfg.get('memory_on', True):
            enabled_keys.discard('memory.recall')
            enabled_keys.discard('memory.remember')
        if not cfg.get('self_memory_on', True):
            enabled_keys.discard('memory.recall_self')
            enabled_keys.discard('memory.remember_self')
        if not cfg.get('inject_on', True):
            enabled_keys.discard('inject.add')
            enabled_keys.discard('inject.list')
            enabled_keys.discard('inject.toggle')
            enabled_keys.discard('inject.remove')
        if not cfg.get('meme_guide_on', True):
            enabled_keys.discard('meme.lookup')
        if not cfg.get('read_video_on', True):
            enabled_keys.discard('fs.read_video')

        # AI 自定义工具：本群 + 全局，本群同名覆盖全局（SQL 已按全局在前排序）；
        # 群工具开关（overrides）也能按 custom__<name> 关闭
        enabled_custom = {}
        if getattr(self.p, 'custom', None) is not None:
            rows = storage.list_custom_tools(self.p.db_path, gid,
                                              enabled_only=True)
            for r in rows:
                key = f'custom__{r["name"]}'
                if overrides.get(key, True):
                    enabled_custom[key] = r

        schemas = builtin_tools.schemas(enabled_keys)
        for full, (_, spec) in enabled_mcp.items():
            if full in enabled_keys:
                schemas.append(spec)
        for key, r in enabled_custom.items():
            params = r.get('schema') if isinstance(r.get('schema'), dict) \
                else {'type': 'object', 'properties': {}}
            schemas.append({'type': 'function', 'function': {
                'name': key,
                'description': f'[自定义工具·{"仅主人" if r["owner_only_call"] else "群内可用"}] '
                               + r['description'],
                'parameters': params}})
        return schemas, enabled_mcp, enabled_custom

    def _risk_denied(self, name: str, tool_ctx, cfg: dict):
        """非主人指令风险操作时返回拒绝文案；允许则返回 None。"""
        if not cfg.get('risky_owner_only', True) or tool_ctx.is_owner:
            return None
        risky = False
        if builtin_tools.is_builtin(name):
            risky = builtin_tools.TOOL_KEYS.get(name) in builtin_tools.RISKY_TOOL_KEYS
        elif name.startswith('mcp__'):
            risky = True   # MCP 工具 = 远程桌面读写/操控，整体视为风险
        if not risky:
            return None
        owner = str(cfg.get('owner_qq', '') or '')
        return (f'错误: 这属于风险操作（写/追加/删除工作区或 public 公共素材库'
                f'文件、沉淀素材 fs_publish、在本机执行代码/命令、'
                f'发布/修改自定义工具或初筛脚本，或操控远程桌面），'
                f'只有主人（QQ {owner}）才能指令。请拒绝对方并说明：'
                f'需要主人本人确认后我才能执行，不要尝试绕过。')

    async def _dispatch(self, name: str, args: dict, tool_ctx, cfg,
                        enabled_mcp, enabled_custom=None) -> str:
        denied = self._risk_denied(name, tool_ctx, cfg)
        if denied:
            self.log(f'群 {tool_ctx.group_id} 非主人 {tool_ctx.sender_id} '
                     f'尝试风险工具 {name} 已拦截', 'warn')
            return denied
        if builtin_tools.is_builtin(name):
            # read_video 要解码整段视频再等视觉子代理，套用普通工具超时必死
            timeout = float(cfg.get(
                'read_video_timeout' if name == 'read_video'
                else 'tool_timeout', 180 if name == 'read_video' else 30))
            try:
                return await asyncio.wait_for(
                    builtin_tools.dispatch(name, args, tool_ctx),
                    timeout=timeout)
            except asyncio.TimeoutError:
                return '错误: 工具执行超时'
        if name.startswith('mcp__') and name in enabled_mcp:
            server_id, tool_name = enabled_mcp[name][0], None
            parts = name.split('__', 2)
            tool_name = parts[2]
            try:
                return await asyncio.wait_for(
                    self.p.mcp.call_tool(
                        server_id, tool_name, args, cfg,
                        sandbox=tool_ctx.sandbox,
                        ttl_hours=float(cfg.get('mcp_media_ttl_hours', 6))),
                    timeout=float(cfg.get('mcp_timeout', 30)))
            except asyncio.TimeoutError:
                return '错误: MCP 工具执行超时'
            except Exception as e:
                return f'错误: MCP 工具异常: {e}'
        if name.startswith('custom__') and enabled_custom and \
                name in enabled_custom:
            row = enabled_custom[name]
            if row.get('owner_only_call') and not tool_ctx.is_owner:
                self.log(f'群 {tool_ctx.group_id} 非主人 {tool_ctx.sender_id} '
                         f'尝试主人专属自定义工具 {name}，已拦截', 'warn')
                return '错误: 这个自定义工具被发布者标记为仅主人可触发'
            if self.p.custom is None:
                return '错误: 自定义工具功能不可用'
            timeout = float(cfg.get('custom_tool_call_timeout', 20))
            try:
                # 子进程内部自带 timeout 会自杀；外层 wait_for 只是兜底
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        self.p.custom.call_tool, row, args,
                        self.p.custom._call_ctx(tool_ctx), timeout),
                    timeout=timeout + 15)
            except asyncio.TimeoutError:
                return '错误: 自定义工具执行超时'
        return f'错误: 工具 {name} 不可用'

    # ── prompt ──────────────────────────────────────────────────

    @staticmethod
    def _system_prompt(persona: str, mem_block: str, style_block: str,
                       task_block: str, cfg: dict, has_tools: bool,
                       has_mcp: bool = False, bot_id=0, bot_name: str = '',
                       chat_type: str = 'group', inject_block: str = '',
                       store=None, catalog: str = '') -> dict:
        scene = ('这是一对一 QQ 私聊。' if chat_type == 'private'
                 else '你在一个 QQ 群里。')
        bot_line = ''
        if bot_id:
            bot_line = (f'你自己的 QQ 号是 {bot_id}'
                        + (f'，昵称「{bot_name}」' if bot_name else '')
                        + '。有人 @ 这个号/昵称、或消息带 [回复 你] 前缀，'
                          '就是在跟你说话；群友之间互相 @、互相回复时别会错意。')
        # 前缀缓存顺序：稳定块在前、每轮易变块（风格/记忆/约定/注入）压到最后。
        # tools_guide 的 catalog 每轮工具集固定时是稳定大段，排在易变块之前，
        # 避免记忆/注入一更新就打断后面所有内容的缓存。
        parts = [
            persona.strip(),
            prompts_mod.render('identity', store,
                               scene=scene, bot_line=bot_line),
            prompts_mod.render('nospeak', store),
            prompts_mod.render('media', store),
            prompts_mod.render('cq_ban', store),
        ]
        if has_tools:
            parts.append(prompts_mod.render(
                'tools_guide', store,
                catalog=catalog, owner_qq=cfg.get('owner_qq', '')))
        if has_tools and has_mcp:
            parts.append(prompts_mod.render('mcp_rules', store))
        if style_block:
            parts.append(style_block)
        if mem_block:
            parts.append(mem_block)
        if task_block:
            parts.append(task_block)
        if inject_block:
            parts.append(inject_block)
        return {'role': 'system', 'content': '\n\n'.join(p for p in parts if p)}


def _attach_inbound_media(messages: list, att) -> None:
    """把入站图片/附件文本/语音转写挂到最后一条 user 消息上（就地修改）。

    图片走多模态 content parts；附件/转写是纯文本，直接追加。
    这些内容只活在当轮请求里，DB 历史仍只有占位符。
    """
    extra = (getattr(att, 'extra_text', '') or '').strip()
    images = getattr(att, 'images', None) or []
    if not extra and not images:
        return
    idx = -1
    for i, m in enumerate(messages):
        if m.get('role') == 'user':
            idx = i
    if idx < 0:
        return
    um = messages[idx]
    base = um.get('content')
    base = base if isinstance(base, str) else ''
    text = (base + ('\n' + extra if extra else '')).strip()
    if images:
        parts = [{'type': 'text', 'text': text or '（对方发来了图片）'}]
        for mime, b64 in images:
            parts.append({'type': 'image_url',
                          'image_url': {'url': f'data:{mime};base64,{b64}'}})
        um['content'] = parts
    elif extra:
        um['content'] = text


def _hit_heuristic(text: str, aliases: list, bot_name: str) -> bool:
    from .trigger import heuristic_hit
    names = [bot_name] if bot_name else []
    return heuristic_hit(text, aliases, names)


def _called_by_name(text: str, aliases: list, bot_name: str) -> bool:
    """消息里叫了她的名字/称呼词（剥 CQ 码与链接后包含匹配）。

    只认名字，不含 question/hint——仅@触发时叫名字算直接点名。
    """
    from .trigger import strip_for_heuristic
    t = strip_for_heuristic(text).lower()
    if not t:
        return False
    for name in [*(aliases or []), bot_name]:
        n = (name or '').strip().lower()
        if n and n in t:
            return True
    return False
