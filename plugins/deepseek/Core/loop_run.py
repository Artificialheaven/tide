"""loop 长跑模式：主人授权后，AI 在一个群里围绕任务连续自主工作。

- 无普通对话的工具轮上限（引擎在 loop_ctx 非 None 时摘掉上限）；
- 每 N 个工具轮必须在群里说一次进展（引擎侧强制）；
- 上下文将满时引擎自动压缩（loop_ctx 滚动纪要）；
- 结束：AI 调 loop_finish / 主人 /ai loop 关 / 连续空转自动停 / 异常；
  结束私聊汇报主人。

授权入口有两个：主人 /ai loop <任务> 直接开；AI 调 loop.request 落申请 +
私聊主人，主人 /ai loop 同意 N 后启动（解耦审批，不阻塞工具协程）。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import traceback

from . import storage
from .config import resolve_group
from .models import ParsedMessage


def _start_notice(task: str) -> str:
    return (
        '[长跑任务通知]\n'
        '主人已授权你进入长跑模式，连续完成下面这个任务，普通对话的工具步数'
        '上限对你不生效：\n'
        f'任务：{task}\n'
        '规则：\n'
        '1. 自主连续推进，不要等任何人说「继续」；一个阶段说完进展话后你会立刻'
        '收到续跑通知，接着做直到全部完成。\n'
        '2. 每做几个工具动作就要在群里用一句话说说进展，不能长时间闷头干活。\n'
        '3. 上下文快满时会自动压缩，放心继续，不要重复已完成的步骤。\n'
        '4. 全部完成后【必须调用 loop_finish】汇报结果；主人随时可以叫停。\n'
        '不要对群友提及「系统/通知/模式/长跑」这类内部字眼，自然地干活说话。')


def _continue_notice(task: str, idle: int) -> str:
    tail = ''
    if idle:
        tail = ('\n注意：你上一轮没有进行任何工具操作。任务若已完成就立刻调用 '
                'loop_finish；不要用闲聊空转。')
    return (
        '[长跑续跑通知]\n'
        f'长跑任务还没收尾：{task}\n'
        '如果已经全部完成，立刻调用 loop_finish 并附上结果摘要；'
        '否则继续下一步，必要时先用一句话在群里说说进展。' + tail)


class LoopManager:
    def __init__(self, plugin, log=None):
        self.p = plugin
        self._log = log or (lambda msg, level='info': None)
        # gid → asyncio.Task（只在事件循环线程内增删，无需加锁）
        self._tasks: dict[str, asyncio.Task] = {}

    # ── 查询 ────────────────────────────────────────────────────

    def _notice(self, gid, chat_type: str, lifecycle: str, text: str,
                session_id: int = 0):
        """实时订阅：长跑生命周期通知（best-effort，不影响主流程）。"""
        live = getattr(self.p, 'live', None)
        if live is None:
            return
        try:
            live.emit('notice', group_id=gid, chat_type=chat_type,
                      lifecycle=lifecycle, session_id=session_id, text=text)
        except Exception:
            pass

    def is_running(self, gid) -> bool:
        return str(gid) in self._tasks

    def _chat_type(self, gid: str) -> str:
        return ('private' if str(gid) == str(self.p.cfg.get('owner_qq', ''))
                else 'group')

    def status_text(self, gid: str) -> str:
        if str(gid) in self._tasks:
            row = storage.get_running_loop(self.p.db_path, gid)
            if row:
                return (f'🏃 长跑进行中（任务#{row["id"]}）：{row["task"]}\n'
                        f'已执行 {row["rounds"]} 个工具轮，压缩 {row["compacts"]} 次\n'
                        '主人可发 /ai loop 关 随时停止')
            return '🏃 长跑进行中（主人可发 /ai loop 关 停止）'
        row = storage.get_running_loop(self.p.db_path, gid)
        if row:
            # DB 残留 running：插件重启遗留，worker 已不存在
            storage.update_loop_session(self.p.db_path, row['id'],
                                        status='interrupted')
        return '本群没有进行中的长跑'

    # ── 启停 ────────────────────────────────────────────────────

    async def start(self, gid, task: str, started_by: str = 'owner',
                    request_id: int = 0, announce: bool = True) -> str:
        """启动长跑。返回发到命令会话的结果文案。

        announce=True 时另向任务会话发一条启动通知（审批来自私聊等跨会话
        场景）；命令会话就是任务会话时必须传 False，由调用方把返回文案发
        出去，否则同一条 🚀 通知会发两遍。
        """
        gid = str(gid)
        task = (task or '').strip()
        if not task:
            return '用法：/ai loop <任务内容>'
        if gid in self._tasks:
            return '⚠️ 本群已有长跑任务在执行，先 /ai loop 关 再开新的'
        row = storage.get_group(self.p.db_path, gid)
        cfg = resolve_group(self.p.cfg, (row or {}).get('settings'))
        if not cfg.get('loop_on', True):
            return '本群已关闭长跑模式（loop_on）'
        session = storage.insert_loop_session(
            self.p.db_path, gid, task, started_by, request_id)
        sid = session['id']
        self._tasks[gid] = asyncio.create_task(self._worker(gid, sid, task))
        note = f'🚀 长跑已启动（任务#{sid}）：{task}'
        if announce:
            try:
                await self.p.bot.send_message(
                    message=note, target_id=gid,
                    message_type=self._chat_type(gid))
            except Exception:
                self._log('长跑启动通知发送失败:\n' + traceback.format_exc(),
                          'error')
        self._notice(gid, self._chat_type(gid), 'start', note, sid)
        if started_by == 'ai':
            # 审批发生在任务会话之外（主人私聊批准）：任务群已另发通知，
            # 命令会话回确认；审批就在任务群里（announce=False）只回启动文案
            return note if not announce else \
                f'✅ 已批准申请#{request_id}，{note}'
        return note

    async def stop(self, gid) -> str:
        gid = str(gid)
        row = storage.get_running_loop(self.p.db_path, gid)
        if not row:
            return '本群没有进行中的长跑'
        storage.update_loop_session(self.p.db_path, row['id'],
                                    status='stopped',
                                    ended_at=_dt.datetime.now().timestamp())
        if gid in self._tasks:
            # 不 cancel：worker 在当前对话轮边界自察状态位后干净退出
            return '⏹️ 已请求停止长跑，她会在当前这一轮做完后退出'
        # DB 有 running 但本进程无 worker：重启遗留，直接落 stopped
        return '⏹️ 已标记长跑停止（执行任务不在本进程，可能已随重启中断）'

    async def stop_all(self):
        """on_stop：取消所有 worker（不发通知，残留 running 由下次对账清理）。"""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for t in tasks:
            t.cancel()
        if tasks:
            # 外部 cancel 会让被取消任务内部重抛 CancelledError（BaseException），
            # 必须用 gather(return_exceptions=True) 兜住，不能裸 except Exception
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=3)

    def reconcile(self) -> int:
        """插件启动：内存 worker 全没了，残留 running 置 interrupted。"""
        try:
            return storage.interrupt_running_loops(self.p.db_path)
        except Exception:
            return 0

    # ── worker ──────────────────────────────────────────────────

    async def _worker(self, gid: str, session_id: int, task: str):
        loop_ctx = {
            'session_id': session_id,
            'finished': False,
            'finish_text': '',
            'since_spoke': 0,
            'total_rounds': 0,
            'compacts': 0,
            'compact_note': '',
            # 真实 token 水位采样（上次成功请求的 prompt_tokens 与当时体量），
            # 以及从超长报错/配置学到的模型上下文总 token 上限
            'last_prompt_tokens': 0,
            'last_weight': 0,
            'context_limit': 0,
        }
        chat_type = self._chat_type(gid)
        try:
            # 整个长跑期间一次持有群锁：普通消息排队、/ai 指令不持锁照常处理、
            # 群友插话走 InterruptHub 锁外插队通道
            async with self.p.group_lock(gid):
                await self._run_locked(gid, session_id, task,
                                       chat_type, loop_ctx)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log(f'群 {gid} 长跑 worker 异常: {e}\n'
                      + traceback.format_exc(), 'error')
            try:
                storage.update_loop_session(
                    self.p.db_path, session_id, status='error',
                    error=str(e)[:500],
                    ended_at=_dt.datetime.now().timestamp())
                await self._notify_owner(
                    f'⚠️ 群 {gid} 的长跑任务#{session_id} 异常停止：\n'
                    f'任务：{task[:100]}\n错误：{str(e)[:300]}')
                self._notice(gid, chat_type, 'error',
                             f'长跑异常停止：{str(e)[:200]}', session_id)
            except Exception:
                pass
        finally:
            self._tasks.pop(gid, None)

    async def _run_locked(self, gid: str, session_id: int, task: str,
                          chat_type: str, loop_ctx: dict):
        """持有群锁的长跑主循环（调用方已持锁）。"""
        turn = 0
        idle_turns = 0
        while True:
            # 状态位检查（主人 /ai loop 关）
            sess = storage.get_loop_session(self.p.db_path, session_id)
            if not sess or sess['status'] != 'running':
                self._log(f'群 {gid} 长跑#{session_id} 状态变为 '
                          f'{sess and sess["status"]}，worker 退出', 'warn')
                return
            row = storage.get_group(self.p.db_path, gid)
            cfg = resolve_group(self.p.cfg, (row or {}).get('settings'))
            if not cfg.get('loop_on', True):
                storage.update_loop_session(
                    self.p.db_path, session_id, status='stopped',
                    ended_at=_dt.datetime.now().timestamp())
                return

            turn += 1
            notice = _start_notice(task) if turn == 1 \
                else _continue_notice(task, idle_turns)
            storage.insert_message(
                self.p.db_path, gid, 'user', notice,
                sender_id='', sender_name='系统', name='loop-notice')
            storage.upsert_group(self.p.db_path, gid, touch=True)
            parsed = ParsedMessage(
                text=notice, sender_id='', sender_name='系统',
                at_me=True, chat_type=chat_type, allow_silent=False)

            run_id = storage.bg_run_start(
                self.p.db_path, 'loop', chat_type, gid,
                f'[长跑#{session_id}] {task}'[:4000],
                started_at=_dt.datetime.now().timestamp())
            trace: list = []
            loop_ctx['tools_this_turn'] = 0
            try:
                res = await self.p.engine._run_turn(
                    gid, cfg, parsed, True, None, background=True,
                    trace=trace, raw_bg_notice=True, source_kind='work',
                    loop_ctx=loop_ctx)
            except Exception as e:
                storage.bg_run_finish(
                    self.p.db_path, run_id, 'error', '', trace, str(e),
                    keep=int(cfg.get('bg_runs_keep', 200)))
                raise

            storage.bump_loop_rounds(
                self.p.db_path, session_id, loop_ctx['total_rounds'])
            # 每个续跑轮收尾时她都会在群里留下终态正文，汇报节奏按轮重新计数
            if res['final_text']:
                loop_ctx['since_spoke'] = 0
            status = ('silent' if res['silent']
                      else ('empty' if not res['final_text'] else 'ok'))
            storage.bg_run_finish(
                self.p.db_path, run_id, status, res['final_text'], trace,
                keep=int(cfg.get('bg_runs_keep', 200)))

            # 收尾判定
            sess = storage.get_loop_session(self.p.db_path, session_id)
            if not sess or sess['status'] != 'running':
                return
            if loop_ctx['finished']:
                result = loop_ctx.get('finish_text', '') or res['final_text']
                storage.update_loop_session(
                    self.p.db_path, session_id, status='done',
                    result=result[:2000],
                    ended_at=_dt.datetime.now().timestamp())
                await self._finish_announce(gid, chat_type, session_id,
                                            task, result, 'done')
                return
            if loop_ctx.get('tools_this_turn', 0) == 0:
                idle_turns += 1
            else:
                idle_turns = 0
            idle_limit = max(1, int(cfg.get('loop_idle_turn_limit', 3)))
            if idle_turns >= idle_limit:
                storage.update_loop_session(
                    self.p.db_path, session_id, status='done',
                    result='连续多轮未进行工具操作，判定任务已结束（空转保护）',
                    ended_at=_dt.datetime.now().timestamp())
                await self._finish_announce(
                    gid, chat_type, session_id, task,
                    '她连续几轮没有继续操作，长跑已自动结束。'
                    '如任务其实没完，可再次 /ai loop 开启。', 'idle')
                return
            # 让出事件循环一圈（插队投递/通知等得以处理）
            await asyncio.sleep(1)

    async def _finish_announce(self, gid, chat_type, session_id, task,
                               result, kind):
        # 群里一句简短收尾；详细摘要私聊主人
        try:
            if kind == 'done':
                await self.p.bot.send_message(
                    message=f'✅ 长跑任务#{session_id}已完成',
                    target_id=gid, message_type=chat_type)
        except Exception:
            pass
        await self._notify_owner(
            f'✅ 长跑任务#{session_id}已完成（群 {gid}）\n'
            f'任务：{task[:200]}\n结果：{str(result)[:1000]}')
        self._notice(gid, chat_type, kind,
                     f'{task[:80]}\n{str(result)[:500]}', session_id)

    async def _notify_owner(self, text: str):
        owner = self.p.cfg.get('owner_qq')
        if not owner or not getattr(self.p, 'bot', None):
            return
        try:
            await self.p.bot.send_message(
                message=text, target_id=owner, message_type='private')
        except Exception:
            self._log('长跑私聊通知主人失败:\n' + traceback.format_exc(),
                      'warn')

    # ── AI 申请 / 主人审批 ──────────────────────────────────────

    async def request_tool(self, gid, task: str, reason: str,
                           requester_id, requester_name, bot, cfg) -> str:
        """loop.request 工具：落申请 + 私聊主人。返回给模型的提示。"""
        if str(gid) in self._tasks or storage.get_running_loop(
                self.p.db_path, gid):
            return '本群长跑已经在进行中，你直接继续干活即可，无需重复申请。'
        row = storage.insert_loop_request(self.p.db_path, {
            'group_id': str(gid), 'task': task, 'reason': reason,
            'requester_id': str(requester_id or ''),
            'requester_name': str(requester_name or ''),
        })
        rid = row['id']
        owner = cfg.get('owner_qq')
        if owner and bot is not None:
            text = (
                f'[长跑申请#{rid}] 群 {gid}'
                + (f'，{requester_name}(QQ:{requester_id})' if requester_id else '')
                + ' 让我申请进入长跑模式\n'
                f'任务：{task[:300]}\n'
                + (f'理由：{reason[:200]}\n' if reason else '')
                + f'批准请发：/ai loop 同意 {rid}\n'
                f'拒绝请发：/ai loop 拒绝 {rid}（群里或私聊发给我都行）')
            try:
                await bot.send_message(message=text, target_id=owner,
                                       message_type='private')
            except Exception:
                self._log('长跑申请私聊通知失败:\n' + traceback.format_exc(),
                          'warn')
        return (f'长跑申请#{rid}已记录并私聊通知主人，不会在本轮启动。'
                '你可以先结束本轮回复；主人批准后你会收到一条明确的长跑任务'
                '通知，届时连续完成任务并用 loop_finish 收尾。')

    async def approve(self, req_id, owner_qq, cmd_gid: str = '') -> str:
        """主人批准申请。cmd_gid 是命令所在会话：与任务群相同说明批准就在
        任务群里发出，启动通知只发一遍；否则（私聊批准）任务群另发 🚀，
        命令会话回 ✅。"""
        req = storage.get_loop_request(self.p.db_path, int(req_id)) \
            if str(req_id).isdigit() else None
        if not req:
            return f'找不到长跑申请#{req_id}'
        if req['status'] != 'pending':
            return f'申请#{req_id}已经处理过（{req["status"]}）'
        if not storage.mark_loop_request(self.p.db_path, req['id'],
                                         'approved', str(owner_qq)):
            return f'申请#{req_id}已经处理过'
        announce = str(cmd_gid) != str(req['group_id'])
        return await self.start(req['group_id'], req['task'],
                                started_by='ai', request_id=req['id'],
                                announce=announce)

    async def reject(self, req_id, reason: str, owner_qq) -> str:
        if not str(req_id).isdigit():
            return '用法：/ai loop 拒绝 <编号> [理由]'
        req = storage.get_loop_request(self.p.db_path, int(req_id))
        if not req:
            return f'找不到长跑申请#{req_id}'
        if req['status'] != 'pending':
            return f'申请#{req_id}已经处理过（{req["status"]}）'
        if not storage.mark_loop_request(self.p.db_path, req['id'],
                                         'rejected', str(owner_qq)):
            return f'申请#{req_id}已经处理过'
        # 让群里的她下次被触发时能看到申请已被拒（不要傻等批准）
        try:
            storage.insert_message(
                self.p.db_path, req['group_id'], 'user',
                f'[系统通知] 主人拒绝了长跑申请#{req["id"]}'
                + (f'：{reason}' if reason else ''),
                sender_id='', sender_name='系统', name='loop-notice')
        except Exception:
            pass
        return f'已拒绝长跑申请#{req["id"]}'
