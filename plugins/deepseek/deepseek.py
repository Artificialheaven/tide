"""DeepSeek 群管 AI 插件
======================
- 主人指令开关各群 AI；未开启群零 token
- @我/回复我必答，其余消息 启发式 → 判官 两级过滤
- 分条发送模拟真人；每群人设/工具/思考开关可后台配置
- QQ 三件套 + 工作区文件五件套 + per-user 长期记忆 + MCP 工具
"""
import asyncio
import json
import os
import sys
import traceback

import websockets
from utils.Plugin import Plugin

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from Core import config as cfg_mod
from Core import storage
from Core.commands import (extract_target_qq, is_owner, is_private_allowed,
                           parse_alias_arg, parse_command, parse_interrupt,
                           parse_loop_arg, parse_thinking_arg)
from Core.loop_run import LoopManager
from Core.config import merge_config, parse_aliases, resolve_group
from Core.custom_tools import CustomToolManager
from Core.engine import Engine
from Core.interrupt import InterruptHub
from Core.live import LiveManager
from Core.proactor import ProactorManager
from Core.llm import LLMClient
from Core.mcp_manager import McpManager
from Core.mentions import MemberDirectory, MemberInfoDirectory
from Core.memory import MemoryManager
from Core.meme_guide import MemeGuide
from Core.mood import MoodManager
from Core.prompts import PromptStore
from Core.screen_scripts import ScreenScriptManager
from Core.self_memory import SelfMemoryManager
from Core.requests import RequestManager
from Core.message import parse_event, self_group_join_notice
from Core import remote_files
from Core.sandbox import Sandbox, purge_all_mcp_media
from Core.scheduler import JobManager, describe_schedule, describe_target
from Core.status import is_status_trigger, send_status
from Core.style import StyleManager
from Core.triggers import TriggerManager
from Core.watchers import WatcherManager
from Core.qzone import QzoneClient
from Core.tasks import TaskManager
from Core.version import VERSION

try:
    from core import globals
except Exception:  # 单测环境
    globals = None


class main(Plugin):
    name = "DeepSeek群AI"
    description = "DeepSeek 群聊 AI：MCP 工具、工作区、分条发送、按人长期记忆"
    version = VERSION

    def __init__(self, websocket: websockets.ClientConnection, bot_instance=None):
        super().__init__(websocket, bot_instance)
        self._plugin_dir = _DIR
        os.makedirs(os.path.join(_DIR, 'db'), exist_ok=True)
        os.makedirs(os.path.join(_DIR, 'workspace', 'public'), exist_ok=True)
        self.db_path = os.path.join(_DIR, 'db', 'deepseek.db')
        self.meme_db_path = os.path.join(_DIR, 'db', 'meme.db')
        self.workspace_root = os.path.join(_DIR, 'workspace')
        storage.migrate(self.db_path)

        # _raw_config 是最近一次已知的完整原始配置：框架弹窗保存会用 web.json
        # 字段子集整文件覆写 config.json，load_config 必须与内存里的全量配置
        # 合并，否则未登记键（含 llm_providers 密钥）会被冲掉
        self._raw_config = self.get_config() or {}
        self.cfg = merge_config(self._raw_config)
        # 触发 token 首次自动生成（只落 gitignored config.json）
        if self.cfg.get('trigger_on', True) and not self.cfg.get('trigger_token'):
            import secrets
            self.cfg['trigger_token'] = secrets.token_hex(16)
            self.save_config(self.cfg)
            self._raw_config = dict(self.cfg)
        self.llm = LLMClient()
        self.prompts = PromptStore(self.db_path, log=self.log)
        # 判官/摘要/抽取提示词走提示词页注册表，缺省=内置默认
        self.llm.prompts = self.prompts
        self.memory = MemoryManager(self.db_path, log=self.log)
        self.self_memory = SelfMemoryManager(
            self.db_path, log=self.log, prompts=self.prompts)
        self.mood = MoodManager(
            self.db_path, log=self.log, prompts=self.prompts)
        self.tasks = TaskManager(self.db_path, self.memory, log=self.log)
        self.memory.tasks = self.tasks
        self.memory.self_memory = self.self_memory
        self.scheduler = JobManager(self.db_path, log=self.log)
        self.style = StyleManager(self.db_path, log=self.log)
        self.mcp = McpManager(_DIR, self.db_path, log=self.log)
        self.custom = CustomToolManager(self.db_path, log=self.log)
        self.screens = ScreenScriptManager(self.db_path, log=self.log)
        self.requests = RequestManager(self.db_path, log=self.log)
        self.proactor = ProactorManager(self.db_path, log=self.log)
        self.meme_guide = MemeGuide(self.meme_db_path, log=self.log)
        self.interrupts = InterruptHub()
        self.members = MemberDirectory()
        self.member_info = MemberInfoDirectory()
        self.triggers = TriggerManager(self, log=self.log)
        self.live = LiveManager(self, log=self.log)
        self.watchers = WatcherManager(self)
        self.qzone = QzoneClient(self)
        self.loop = LoopManager(self, log=self.log)
        self.engine = Engine(self)
        # 上次运行残留的监测进程登记：死了的标 stopped（活着的继续跑，不杀）
        try:
            n = self.watchers.reconcile()
            if n:
                self.log(f'监测进程对账：{n} 条登记已失效')
        except Exception:
            pass

        self._locks: dict[str, asyncio.Lock] = {}
        self._raw_counts: dict[str, int] = {}
        self.bot_id = getattr(globals, 'bot_id', 0) if globals else 0
        self.bot_name = ''
        self.log(f'插件已初始化 v{VERSION}')

    # ── 生命周期 ────────────────────────────────────────────────

    async def on_start(self):
        try:
            info = await self.bot.get_login_info()
            data = (info or {}).get('data') or {}
            if data.get('user_id'):
                self.bot_id = data['user_id']
            self.bot_name = data.get('nickname', '') or ''
        except Exception:
            pass
        # 上次运行（或热重载）残留的 running 长跑：worker 已随旧实例消失，
        # 不做自动复活，统一标记 interrupted
        try:
            n = self.loop.reconcile()
            if n:
                self.log(f'长跑对账：{n} 个未结束会话因重启标记为中断', 'warn')
        except Exception:
            pass
        self.tasks.start(self)
        self.scheduler.start(self)
        self.proactor.start(self)
        try:
            self.triggers.start()
        except Exception:
            self.log('外部触发服务启动异常:\n' + traceback.format_exc(), 'error')
        try:
            self.live.start()
        except Exception:
            self.log('实时订阅服务启动异常:\n' + traceback.format_exc(), 'error')
        ttl = float(self.cfg.get('mcp_media_ttl_hours', 6))
        if ttl > 0:
            try:
                n = await asyncio.to_thread(
                    purge_all_mcp_media, self.workspace_root, ttl * 3600)
                if n:
                    self.log(f'启动清扫：删除 {n} 个过期 MCP 媒体文件')
            except Exception:
                pass
        self.log(f'on_start 完成，bot={self.bot_name}({self.bot_id})')

    async def on_stop(self):
        try:
            await self.loop.stop_all()
        except Exception:
            pass
        try:
            await self.tasks.stop()
        except Exception:
            pass
        try:
            await self.scheduler.stop()
        except Exception:
            pass
        try:
            await self.proactor.stop()
        except Exception:
            pass
        try:
            # 只停 HTTP 服务；监测子进程是分离的，刻意不杀（监测不能中断）
            self.triggers.stop()
        except Exception:
            pass
        try:
            self.live.stop()
        except Exception:
            pass
        try:
            await self.mcp.shutdown(total_budget=8.0)
        except Exception:
            pass
        try:
            await self.llm.aclose()
        except Exception:
            pass
        self.log('on_stop 完成')

    # ── 工具方法（供 Engine/Web 使用）──────────────────────────

    # level → 框架日志颜色（GUI 控制台 / Web 日志面板 / nogui 控制台三处同源）
    _LOG_COLORS = {'info': '默认', 'ok': '绿色', 'warn': '黄色', 'error': '红色'}

    def log(self, msg: str, level: str = 'info'):
        print(f'[{self.name}] {msg}')
        # 转发到框架日志（self.bot 是 PluginBotWrapper，带插件专属 Logger；
        # 单测环境的 fake bot 没有 Logger，静默退回 print）
        lg = getattr(getattr(self, 'bot', None), 'Logger', None)
        if lg is not None:
            try:
                lg.custom(str(self.bot_id or 'DeepSeek'), self.name,
                          str(msg), self._LOG_COLORS.get(level, '默认'))
            except Exception:
                pass

    def group_lock(self, gid: str) -> asyncio.Lock:
        lk = self._locks.get(gid)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[gid] = lk
        return lk

    def _record_raw(self, gid: str, parsed, eff: dict):
        try:
            rid = storage.insert_raw_message(
                self.db_path, gid, parsed.sender_id,
                parsed.sender_card or parsed.sender_name, parsed.text)
            # 回填给引擎：背景消息游标以它为上界（异常/未记录时保持 0）
            try:
                parsed.raw_id = int(rid or 0)
            except (TypeError, ValueError):
                pass
            n = self._raw_counts.get(gid, 0) + 1
            self._raw_counts[gid] = n
            if n % 200 == 0:
                storage.trim_raw(
                    self.db_path, gid, int(eff.get('raw_corpus_cap', 3000)))
        except Exception:
            self.log('原始语料落库异常:\n' + traceback.format_exc())

    def _emit_chat_live(self, gid: str, parsed, chat_type: str):
        """实时订阅：已开启群/私聊的每条入站消息（触发闸门前，被拦的也推）。"""
        try:
            self.live.emit(
                'chat', group_id=gid, chat_type=chat_type,
                sender_id=str(parsed.sender_id or ''),
                sender_name=parsed.display_name,
                text=self.engine._user_display_text(parsed),
                at_me=bool(parsed.at_me),
                reply=bool(parsed.reply_id),
                has_media=bool(parsed.has_media),
                media=[str(m.get('kind') or '') for m in parsed.media][:8])
        except Exception:
            pass

    def sandbox(self, gid: str) -> Sandbox:
        return Sandbox(
            self.workspace_root, gid,
            max_file_kb=self.cfg.get('max_file_size_kb', 512),
            quota_mb=self.cfg.get('workspace_quota_mb', 10),
            public_writable=self.cfg.get('public_writable_on', True),
            public_max_file_kb=self.cfg.get('public_max_file_kb', 4096),
            public_quota_mb=self.cfg.get('public_quota_mb', 50),
            mcp_media_max_kb=self.cfg.get('mcp_media_max_kb', 15360))

    def reload_cfg(self, raw: dict | None = None) -> dict:
        """合并默认值并持久化。

        raw=None：重新规范化内存中的 _raw_config（磁盘可能刚被框架弹窗覆写成
        字段子集，不能从盘读）；raw 非空：以它作为新的全量原始配置。
        落盘的是规范化后的 cfg（未知键由 merge_config 原样保留）。
        """
        if raw is not None:
            self._raw_config = raw
        merged = merge_config(self._raw_config)
        self.cfg = merged
        self.save_config(merged)
        self._raw_config = dict(merged)
        # 服务商列表可能已删除条目，顺手关掉对应缓存客户端
        try:
            active = {p.get('id') for p in merged.get('llm_providers', [])
                      if isinstance(p, dict)}
            self.llm.prune_clients(active)
        except Exception:
            pass
        return self.cfg

    # ── 消息入口 ────────────────────────────────────────────────

    @Plugin.on_message("message")
    async def handle_message(self, event):
        if not self.bot:
            return
        mtype = getattr(event, 'message_type', '')
        if mtype not in ('group', 'private'):
            return
        bot_id = getattr(event, 'self_id', None) or self.bot_id
        parsed = parse_event(event, bot_id)
        if not parsed.text and not parsed.has_media:
            return

        # `#sl` 状态查询：全员可用、不开群也响应、不抢群锁
        if is_status_trigger(parsed.text):
            target = event.user_id if mtype == 'private' else event.group_id
            try:
                await send_status(self.bot, target, mtype)
            except Exception:
                self.log('状态查询处理异常:\n' + traceback.format_exc())
            return

        if mtype == 'private':
            await self._handle_private(event, parsed)
            return

        gid = str(event.group_id)
        group_row = storage.get_or_create_group(self.db_path, gid)

        # 群友名片/昵称即时入目录：回复里的 @昵称 才能发成真艾特
        self.members.observe(gid, parsed.sender_id,
                             parsed.sender_card, parsed.sender_name)

        # 打断指令最优先：不抢群锁，直接投进正在执行的对话轮（全员可用）
        if await self._maybe_interrupt(parsed, group_row, 'group'):
            return

        # 主人指令优先（未开启群也能用）
        try:
            handled = await self._maybe_command(event, parsed, group_row)
            if handled:
                return
        except Exception:
            self.log('指令处理异常:\n' + traceback.format_exc())
            return

        if not group_row['enabled']:
            return

        self._emit_chat_live(gid, parsed, 'group')

        # 全量群聊语料（仅已开启群、0 token）：风格学习原料 + 触发前背景消息来源
        eff = resolve_group(self.cfg, group_row.get('settings'))
        if (eff.get('ambient_context_on', True)
                or eff.get('learn_style_on', True)):
            self._record_raw(gid, parsed, eff)

        # 仅@触发：没 @ 她、没回复她、也没叫她名字的群消息不进 AI，
        # 也不投长跑插队；语料已先落库（风格学习仍需要全量聊天）
        if self.engine.at_only_blocks(eff, parsed, group_row):
            self.engine._live('gate', gid, 'group', stage='at_only',
                              allowed=False,
                              reason='仅@触发：本条未@她、不是回复她、也没叫名字')
            return

        # 长跑进行中：普通群友消息不排队等群锁（worker 整段持锁，排队要等到
        # 长跑结束），改投插队通道在安全点进她的上下文；正赶上两个长跑轮之间
        # 的窄窗（无活动轮）就直接落库，下一个长跑轮重建窗口时看得到
        if self.loop.is_running(gid) and self._divert_to_loop(gid, parsed):
            return

        async with self.group_lock(gid):
            # 锁内重新读取（指令可能刚改过设置）
            row = storage.get_group(self.db_path, gid)
            try:
                await self.engine.handle(event, parsed, row)
            except Exception:
                self.log('对话处理异常:\n' + traceback.format_exc())

    @Plugin.on_notice("notice")
    async def handle_notice(self, event):
        """入群侦查：机器人自己被拉进群时，送一条系统通知给 AI 并让它打招呼。

        别人入群/退群等通知一律忽略；不受群 enabled 与触发闸门约束
        （进群第一面总要打个招呼），但受全局 join_greet_on 开关控制。
        """
        if not self.bot:
            return
        try:
            found = self_group_join_notice(event, self.bot_id)
            if found is None:
                return
            gid, _operator = found
            if not self.cfg.get('join_greet_on', True):
                self.log(f'群 {gid} 机器人入群，但入群打招呼已关闭', 'warn')
                return
            storage.get_or_create_group(self.db_path, gid)
            async with self.group_lock(gid):
                await self.engine.handle_group_join(gid, event)
        except Exception:
            self.log('入群通知处理异常:\n' + traceback.format_exc())

    @Plugin.on_request("request")
    async def handle_request_event(self, event):
        """好友申请 / 拉群邀请：只暂存并私聊通知主人，AI 不自主拍板。

        审批走 request_approve / request_reject 主人专属工具；
        加群申请(sub_type=add)等其他 request 类型在解析层忽略。
        """
        if not self.bot:
            return
        try:
            await self.requests.handle(event, self.cfg, self.bot)
        except Exception:
            self.log('请求事件处理异常:\n' + traceback.format_exc())

    def _private_allowed(self, uid: str) -> bool:
        """私聊白名单：主人恒允许，其余看 private_allowed_qqs。"""
        return is_private_allowed(self.cfg, uid)

    def _register_private_files(self, uid: str, parsed):
        """私聊文件引用落本地清单（OneBot 没有列举私聊文件的接口）。"""
        try:
            remote_files.register_private_files(self.db_path, uid, parsed,
                                                 self.log)
        except Exception:
            self.log('私聊文件登记异常:\n' + traceback.format_exc())

    async def _handle_private(self, event, parsed):
        uid = str(getattr(event, 'user_id', '') or parsed.sender_id or '')
        if not uid:
            return
        parsed.sender_id = uid
        if not self.cfg.get('private_on', True) or not self._private_allowed(uid):
            self.log(f'私聊来自 {uid}（未开启或不在白名单），忽略', 'warn')
            return

        # 文件引用先登记：即使本轮 AI 不回复，清单也保持完整
        self._register_private_files(uid, parsed)

        # 以 QQ 号为键复用群会话的全部设施（人设/上下文/工具/沙箱）
        row = storage.get_or_create_group(self.db_path, uid)
        if await self._maybe_interrupt(parsed, row, 'private'):
            return
        try:
            handled = await self._maybe_command(
                event, parsed, row, chat_type='private')
            if handled:
                return
        except Exception:
            self.log('私聊指令处理异常:\n' + traceback.format_exc())
            return
        if not row['enabled']:
            return

        self._emit_chat_live(uid, parsed, 'private')

        # 私聊每句话都算直接对机器人说：跳过粗筛/判官
        parsed.at_me = True
        # 私聊长跑进行中：同样走插队通道，不持锁排队
        if self.loop.is_running(uid) and self._divert_to_loop(uid, parsed):
            return
        async with self.group_lock(uid):
            row = storage.get_group(self.db_path, uid)
            try:
                await self.engine.handle(event, parsed, row)
            except Exception:
                self.log('私聊对话处理异常:\n' + traceback.format_exc())

    # ── 主人指令 ────────────────────────────────────────────────

    async def _maybe_interrupt(self, parsed, group_row,
                               chat_type='group') -> bool:
        """`/ai 打断 <指示>`：投进正在执行的对话轮，不抢群锁。全员可用。"""
        directive = parse_interrupt(
            parsed.text, self.cfg.get('command_prefix', '/ai'))
        if directive is None:
            return False
        gid = str(group_row['group_id'])
        eff = resolve_group(self.cfg, group_row.get('settings'))
        if not eff.get('interrupt_on', True):
            return True  # 本群关闭：吞掉，不进 AI
        if not directive:
            await self.bot.send_message(
                message='用法：/ai 打断 <临时指示>（我正在干活时，会在当前任务'
                        '里立刻看到并调整做法）',
                target_id=gid, message_type=chat_type)
            return True
        item = self.engine.build_interrupt_item(parsed, directive)
        if not self.interrupts.push(gid, item):
            await self.bot.send_message(
                message='当前没有正在执行的任务，直接发消息我就能看到～',
                target_id=gid, message_type=chat_type)
            return True
        self.log(f'{gid} 收到打断投递：{item["sender_name"]} → '
                 f'{directive[:60]}', 'warn')
        return True

    def _divert_to_loop(self, gid: str, parsed) -> bool:
        """长跑进行中的普通消息：投插队通道；窄窗则落库等下一个长跑轮。

        返回 True 表示已分流（调用方不要再走群锁路径）。
        """
        display_text = self.engine._user_display_text(parsed)
        item = self.engine.build_interrupt_item(parsed, display_text)
        if self.interrupts.push(gid, item):
            self.log(f'{gid} 长跑中收到消息，已投插队通道：'
                     f'{item["sender_name"]} → {display_text[:60]}', 'warn')
            return True
        try:
            storage.upsert_user(self.db_path, parsed.sender_id,
                                parsed.display_name, gid)
            storage.insert_message(
                self.db_path, gid, 'user', display_text,
                parsed.sender_id, parsed.display_name)
            storage.upsert_group(self.db_path, gid, touch=True)
        except Exception:
            self.log('长跑窄窗消息落库异常:\n' + traceback.format_exc())
        return True

    async def _maybe_command(self, event, parsed, group_row,
                             chat_type='group') -> bool:
        parsed_cmd = parse_command(parsed.text, self.cfg.get('command_prefix', '/ai'))
        if parsed_cmd is None:
            return False
        if not is_owner(parsed.sender_id, self.cfg.get('owner_qq')):
            return True  # 命中前缀但非主人：吞掉，不进 AI
        action, arg = parsed_cmd
        gid = str(group_row['group_id'])
        reply = await self._run_command(action, arg, group_row, gid)
        if reply:
            await self.bot.send_message(message=reply, target_id=gid,
                                        message_type=chat_type)
        return True

    async def _run_command(self, action: str, arg: str, group_row: dict,
                           gid: str) -> str:
        if action == 'enable':
            storage.upsert_group(self.db_path, gid, enabled=True)
            return '✅ 本群 AI 已开启'
        if action == 'disable':
            storage.upsert_group(self.db_path, gid, enabled=False)
            return '💤 本群 AI 已关闭（记忆保留）'
        if action == 'reset':
            storage.reset_group_memory(self.db_path, gid)
            return '🗑 本群对话记忆已清空'
        if action == 'status':
            return self._status_text(group_row, gid)
        if action == 'persona':
            if not arg:
                return '用法：/ai 人设 <一句话人设>'
            storage.upsert_group(self.db_path, gid, persona=arg)
            return f'✅ 本群人设已更新：{arg}'
        if action == 'alias':
            return self._alias_cmd(arg, group_row, gid)
        if action == 'thinking':
            return self._thinking_cmd(arg, group_row, gid)
        if action == 'forget':
            qq = extract_target_qq(arg)
            if not qq:
                return '用法：/ai 忘记 <QQ号 或 @某人>'
            n = storage.forget_user(self.db_path, qq)
            nc = storage.cancel_user_commitments(self.db_path, qq)
            self.memory.invalidate(qq)
            tail = f'，取消 {nc} 条未结约定' if nc else ''
            return f'🗑 已软删除 {qq} 的 {n} 条记忆{tail}'
        if action == 'tools':
            return self._tools_text(group_row, gid)
        if action == 'tasklist':
            return self._tasklist_text(gid)
        if action == 'jobs':
            return self._jobs_text(gid)
        if action == 'style':
            if not group_row['enabled']:
                return '请先 /ai 开启 后再学习群风格'
            await self.style.force_build(
                gid, resolve_group(self.cfg, group_row.get('settings')), self.llm)
            text = self.style.get_text(gid)
            return '✅ 风格画像已重建' if text else '⚠️ 可学习的群语料不足'
        if action == 'loop':
            return await self._loop_cmd(arg, group_row, gid)
        return self._help_text()

    async def _loop_cmd(self, arg: str, group_row: dict, gid: str) -> str:
        sub, payload = parse_loop_arg(arg)
        owner = self.cfg.get('owner_qq')
        if sub == 'status':
            return self.loop.status_text(gid)
        if sub == 'stop':
            return await self.loop.stop(gid)
        if sub == 'approve':
            if not str(payload).isdigit():
                return '用法：/ai loop 同意 <编号>'
            return await self.loop.approve(payload, owner, cmd_gid=gid)
        if sub == 'reject':
            rid, _, reason = str(payload).partition(' ')
            if not rid.isdigit():
                return '用法：/ai loop 拒绝 <编号> [理由]'
            return await self.loop.reject(rid, reason.strip(), owner)
        # start：命令会话即任务会话，start 不再自发通知，返回文案走统一回复
        eff = resolve_group(self.cfg, group_row.get('settings'))
        if not eff.get('loop_on', True):
            return '本群已关闭长跑模式（loop_on）'
        return await self.loop.start(gid, payload, announce=False)

    def _status_text(self, group_row, gid: str) -> str:
        eff = resolve_group(self.cfg, group_row.get('settings'))
        think = eff.get('enable_thinking')
        think_src = '（本群覆盖）' if 'enable_thinking' in (group_row.get('settings') or {}) \
            else '（继承全局）'
        sm = storage.get_summary(self.db_path, gid)
        msgs = storage.recent_messages(self.db_path, gid, 500)
        return (
            f'📊 群 {gid} 状态（v{self.version}）\n'
            f'AI：{"开启" if group_row["enabled"] else "关闭"}\n'
            f'模型：{cfg_mod.route_label(eff, "model")}\n'
            f'深度思考：{"开" if think else "关"}{think_src}\n'
            f'分条发送：{"开" if eff.get("split_reply") else "关"}\n'
            f'长期记忆：{"开" if eff.get("memory_on") else "关"}\n'
            f'风格学习：{"开" if eff.get("learn_style_on") else "关"}\n'
            f'主动提醒：{"开" if eff.get("reminder_on") else "关"}\n'
            f'定时任务：{"开" if eff.get("scheduler_on") else "关"}\n'
            f'长跑模式：{"🏃 进行中" if self.loop.is_running(gid) else ("开" if eff.get("loop_on", True) else "关")}\n'
            f'图片视觉：{"开" if eff.get("vision_on") else "关"}；'
            f'附件解析：{"开" if eff.get("file_parse_on") else "关"}；'
            f'入站视频：{"开" if eff.get("inbound_video_on") else "关"}'
            f'（上限 {eff.get("inbound_video_max_kb")}KB）\n'
            f'窗口轮数：{eff.get("window_rounds")}\n'
            f'历史消息：{len(msgs)} 条；摘要：{"有" if sm and sm.get("summary") else "无"}'
        )

    def _alias_cmd(self, arg, group_row, gid) -> str:
        parsed_a = parse_alias_arg(arg)
        if not parsed_a:
            aliases = parse_aliases(group_row.get('aliases'))
            return '本群称呼词：' + ('、'.join(aliases) if aliases else '(无)')
        op, word = parsed_a
        aliases = parse_aliases(group_row.get('aliases'))
        if op == 'add':
            if word not in aliases:
                aliases.append(word)
        elif word in aliases:
            aliases.remove(word)
        storage.upsert_group(self.db_path, gid,
                             aliases=','.join(aliases))
        return f'✅ 称呼词已更新：{"、".join(aliases) or "(无)"}'

    def _thinking_cmd(self, arg, group_row, gid) -> str:
        val = parse_thinking_arg(arg)
        if val is Ellipsis:
            return '用法：/ai 思考 开|关|继承'
        settings = dict(group_row.get('settings') or {})
        if val is None:
            settings.pop('enable_thinking', None)
            note = '已恢复继承全局'
        else:
            settings['enable_thinking'] = val
            note = '已开启' if val else '已关闭'
        storage.upsert_group(self.db_path, gid, settings=settings)
        return f'🧠 本群深度思考{note}'

    def _tools_text(self, group_row, gid) -> str:
        from Core.engine import _ALL_TOOL_KEYS
        overrides = storage.get_tool_overrides(self.db_path, gid)
        lines = ['🛠 本群生效工具（内置）：']
        names = {'qq.text': '群发文本', 'qq.image': '群发图片', 'qq.voice': '群发语音',
                 'qq.at_all': '@全体成员（主人）', 'qq.member_info': '查群成员身份',
                 'qq.file_list': '查QQ文件列表', 'qq.file_fetch': '下载QQ文件',
                 'fs.list': '列文件', 'fs.read': '读文件', 'fs.write': '写文件',
                 'fs.append': '追加文件', 'fs.delete': '删文件',
                 'fs.publish': '沉淀素材到公共库',
                 'inject.add': '加自我提示词注入', 'inject.list': '查注入表',
                 'inject.toggle': '启停注入', 'inject.remove': '删注入',
                 'fs.read_image': '查看工作区图片',
                 'fs.read_video': '看视频/动图（分镜+视觉子代理）',
                 'ui.click_at': '截图坐标点击',
                 'fs.doc_view': '文档渲染看图（PDF/Word）',
                 'code.python': '执行Python（主人）',
                 'code.shell': '执行Shell命令（主人）',
                 'custom.validate': '试运行自定义工具（主人）',
                 'custom.publish': '上线自定义工具（主人）',
                 'custom.list': '查自定义工具',
                 'custom.toggle': '启停自定义工具（主人）',
                 'custom.remove': '删自定义工具（主人）',
                 'screen.validate': '试运行初筛脚本（主人）',
                 'screen.publish': '上线初筛脚本（主人）',
                 'screen.list': '查初筛脚本',
                 'screen.toggle': '启停初筛脚本（主人）',
                 'screen.remove': '删初筛脚本（主人）',
                 'request.list': '查好友申请/群邀请',
                 'request.approve': '同意好友申请/群邀请（主人）',
                 'request.reject': '拒绝好友申请/群邀请（主人）',
                 'time.now': '查询当前时间',
                 'memory.recall': '查群友记忆', 'memory.remember': '记群友信息',
                 'memory.recall_self': '查我自己的记忆',
                 'memory.remember_self': '记我自己的记忆',
                 'task.add': '登记约定', 'task.list': '查约定', 'task.close': '结约定',
                 'schedule.add': '登记定时任务', 'schedule.list': '查定时任务',
                 'schedule.cancel': '取消定时任务',
                 'poll.add': '登记轮询设定', 'poll.list': '查轮询设定',
                 'poll.toggle': '启停轮询', 'poll.remove': '删轮询',
                 'watcher.start': '启动监测程序（主人）',
                 'watcher.list': '查监测程序',
                 'watcher.stop': '停止监测程序（主人）',
                 'watcher.logs': '看监测程序日志',
                 'history.query': '查历史消息',
                 'usage.stats': '查token用量',
                 'qzone.publish': '发QQ空间说说（主人）',
                 'qzone.delete': '删QQ空间说说（主人）',
                 'qzone.recent': '查本机器人空间留痕',
                 'message.read_image': '回看消息里的图片',
                 'message.read_forward': '展开合并转发',
                 'meme.lookup': '梗指南查梗'}
        for key in _ALL_TOOL_KEYS:
            on = overrides.get(key, True)
            lines.append(f'{"✅" if on else "❌"} {names.get(key, key)}')
        for s in self.mcp.enabled_servers():
            lines.append(f'🔌 MCP {s["id"]}（{s["status"]}）：'
                         f'{len(s["tools"])} 个工具已缓存')
            # 每个缓存工具的本群开关（与引擎 _build_tools 同口径：名称已带前缀）
            for spec in self.mcp.cached_tool_specs(s['id']):
                full = spec['function']['name']
                on = overrides.get(full, True)
                parts = full.split('__', 2)
                tool_name = parts[2] if len(parts) == 3 else full
                lines.append(f'    {"✅" if on else "❌"} {tool_name}')
        return '\n'.join(lines)

    def _tasklist_text(self, gid: str) -> str:
        rows = storage.list_commitments(self.db_path, gid, 'open', 30)
        if not rows:
            return '本群没有未结的约定/待办'
        lines = [f'📌 本群未结事项（{len(rows)}）：']
        for r in rows:
            due = f' ⏰{r["due_text"]}' if r['due_text'] else ''
            tag = '话题' if r['kind'] == 'topic' else '待办'
            lines.append(f'[{r["id"]}][{tag}] {r["owner_name"]}：{r["content"]}{due}')
        return '\n'.join(lines)

    def _jobs_text(self, gid: str) -> str:
        rows = storage.list_jobs(self.db_path, gid, include_disabled=True, limit=50)
        active = [r for r in rows if r['enabled']]
        if not active:
            return '本群相关的定时任务：无'
        lines = [f'⏰ 定时任务（{len(active)}）：']
        for r in active:
            lines.append(f'[{r["id"]}] {describe_schedule(r)} → '
                         f'{describe_target(r)}：{r["brief"][:40]}'
                         f'（{r.get("creator_name") or r.get("creator_id")}）')
        return '\n'.join(lines)

    def _help_text(self) -> str:
        p = self.cfg.get('command_prefix', '/ai')
        return (f'指令：{p} 开启|关闭|清空记忆|状态|人设 <内容>|'
                f'别名 add|del <词>|思考 开|关|继承|忘记 <QQ>|'
                f'工具|待办|定时|风格|循环\n'
                f'长跑：{p} 循环 <任务> 开启 / {p} 循环 关 停止 / '
                f'{p} 循环 同意|拒绝 <编号>\n'
                f'全员可用：{p} 打断 <临时指示>（我执行长任务时插话调整）\n'
                f'状态查询：#sl（版本/平台/系统在线时长）')

    # ── Web ─────────────────────────────────────────────────────

    def get_web_blueprint(self):
        from Core.web import build_blueprint
        return build_blueprint(self)

    def call_function(self, name: str, **kwargs):
        if name == 'load_config':
            # 框架弹窗在 RPC 前已用 web.json 字段子集整文件覆写 config.json，
            # 不能读盘合并（会丢未登记键，含 llm_providers 密钥）；
            # 以内存里最近一次全量配置为底，再叠加弹窗子集
            raw = dict(self._raw_config)
            raw.update(kwargs.get('config') or {})
            self.reload_cfg(raw)
            return True
        return super().call_function(name, **kwargs)
