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
import re
import sys
import traceback

import websockets
from utils.Plugin import Plugin

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from Core import config as cfg_mod
from Core import backfill
from Core import hooks
from Core import storage
from Core.commands import (extract_target_qq, is_owner, is_private_allowed,
                           parse_alias_arg, parse_command, parse_interrupt,
                           parse_loop_arg, parse_thinking_arg)
from Core.loop_run import LoopManager
from Core.config import merge_config, parse_aliases, resolve_group
from Core.custom_tools import CustomToolManager
from Core.engine import Engine
from Core.housekeeping import Housekeeper
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
from Core import notices
from Core.qzone import QzoneClient
from Core.qzone_watch import QzoneWatcher
from Core.tasks import TaskManager
from Core.version import VERSION

try:
    from core import globals
except Exception:  # 单测环境
    globals = None


# P1-8：会改群状态的指令（执行时持群锁，避免与对话轮并发读旧配置）
_MUTATING_COMMANDS = frozenset({
    'enable', 'disable', 'reset', 'persona', 'forget', 'style'})
# 长跑子动作里的只读/必须免锁项：status 只读；approve/reject 要解掉
# 正在等审批的对话轮，而那一轮持着群锁，加锁会直接死锁
_LOOP_UNLOCKED_SUBS = frozenset({'status', 'approve', 'reject'})


def _command_mutates(action: str, arg: str) -> bool:
    """该指令是否会写群状态/长跑会话（决定要不要持群锁执行）。"""
    if action in _MUTATING_COMMANDS:
        return True
    if action == 'loop':
        try:
            sub = parse_loop_arg(arg)[0]
        except Exception:
            return False
        return sub not in _LOOP_UNLOCKED_SUBS
    # 称呼词/深度思考：带参数才是写，裸命令是查询
    if action in ('alias', 'thinking'):
        return bool((arg or '').strip())
    return False


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
        # 瞬时失败重试日志走插件日志（跨线程 print 也一致）
        self.llm.log = self.log
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
        self.qzone_watch = QzoneWatcher(self, log=self.log)
        # 后台整理（裁剪消息/用量/空间事件），随插件生命周期起停
        self.housekeeper = Housekeeper(self, log=self.log)
        self._backfill_task = None
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
        # 补群名任务（gid -> task），on_stop 统一取消
        self._name_tasks: dict[str, asyncio.Task] = {}
        # P1-5：群级生效配置缓存，(gid, settings 内容) -> eff
        self._cfg_cache: dict[tuple, dict] = {}
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
        try:
            hooks.unregister()
            hooks.install_official_hooks(self)
            self.log(f'hooks 已注册：{len(hooks.registered())} 个')
        except Exception:
            self.log('hooks 注册异常:\n' + traceback.format_exc(), 'error')
        # QQ 空间：先探测通道（WS 优先，HTTP 兜底；official 直接不可用），
        # 可用才启动"被 @ / 新评论"轮询——没有通道时轮询只会白刷日志
        try:
            cap = await self.qzone.refresh(force=True)
            if cap.available:
                if self.cfg.get('qzone_watch_on', True):
                    self.qzone_watch.start(self)
                self.log(f'QQ 空间：{self.qzone.status_text()}')
            else:
                self.log(f'QQ 空间不可用：{cap.why}', 'warn')
        except Exception:
            self.log('QQ 空间初始化异常:\n' + traceback.format_exc(), 'error')
        # 断线/重启补拉群历史：只补本地语料（不进对话窗、不触发回复），
        # 后台任务跑，启动不被 RPC 拖住；official 通道没有这个 action 会直接跳过
        self._start_backfill()
        # 后台整理：晚于补拉启动，避免启动瞬间和补拉抢库
        try:
            self.housekeeper.start()
        except Exception:
            self.log('后台整理启动异常:\n' + traceback.format_exc(), 'error')
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

    async def _seg(self, name: str, coro):
        """on_stop 单段隔离：普通异常记录后继续下一段；CancelledError 记录后
        重新抛出，让上层知道清理被取消（不能悄悄吞掉取消信号）。"""
        try:
            await coro
        except Exception as e:
            self.log(f'on_stop {name} 失败: {type(e).__name__}: {e}', 'warn')
        except BaseException as e:
            self.log(f'on_stop {name} 被取消: {type(e).__name__}: {e}', 'warn')
            raise

    def _seg_sync(self, name: str, fn):
        try:
            fn()
        except Exception as e:
            self.log(f'on_stop {name} 失败: {type(e).__name__}: {e}', 'warn')
        except BaseException as e:
            self.log(f'on_stop {name} 被取消: {type(e).__name__}: {e}', 'warn')
            raise

    async def on_stop(self):
        await self._seg('name_tasks.cancel', self._cancel_name_tasks())
        await self._seg('loop.stop_all', self.loop.stop_all())
        await self._seg('tasks.stop', self.tasks.stop())
        await self._seg('scheduler.stop', self.scheduler.stop())
        await self._seg('proactor.stop', self.proactor.stop())
        await self._seg('qzone_watch.stop', self.qzone_watch.stop())
        # 关闭 QQ 空间复用的 httpx 连接池（HTTP 兜底/配图下载）
        await self._seg('qzone.aclose', self.qzone.aclose())
        await self._seg('backfill.stop', self._stop_backfill())
        await self._seg('housekeeper.stop', self.housekeeper.stop())
        # 只停 HTTP 服务；监测子进程是分离的，刻意不杀（监测不能中断）
        self._seg_sync('triggers.stop', self.triggers.stop)
        self._seg_sync('live.stop', self.live.stop)
        self._seg_sync('hooks.unregister', hooks.unregister)
        self._seg_sync('approvals.cancel_all',
                      self.engine.approvals.cancel_all)
        await self._seg('mcp.shutdown', self.mcp.shutdown(total_budget=8.0))
        await self._seg('llm.aclose', self.llm.aclose())
        # 群锁/配置缓存都是进程内临时状态，下次 on_start 重建
        self._locks.clear()
        self._cfg_cache.clear()
        self.log('on_stop 完成')

    def _start_backfill(self):
        """启动一次群历史补拉（后台任务，不阻塞 on_start）。"""
        try:
            if self._backfill_task is not None and not self._backfill_task.done():
                return
            self._backfill_task = asyncio.create_task(self._run_backfill())
        except Exception:
            self.log('群历史补拉启动异常:\n' + traceback.format_exc(), 'warn')

    async def _run_backfill(self):
        try:
            r = await backfill.run(self, self.cfg)
            if r.get('added'):
                self.log(f'群历史补拉：{r["groups"]} 个群补 {r["added"]} 条')
        except asyncio.CancelledError:
            raise
        except Exception:
            self.log('群历史补拉异常:\n' + traceback.format_exc(), 'warn')

    async def _stop_backfill(self):
        task, self._backfill_task = self._backfill_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

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

    @staticmethod
    def _settings_key(settings) -> str:
        """群 settings 的稳定指纹（P1-5 缓存 key 的一半）。"""
        try:
            return json.dumps(settings or {}, sort_keys=True,
                              ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return repr(settings)

    def cfg_for_group(self, gid: str, settings) -> dict:
        """群级生效配置（缓存命中直接复用）。

        P1-5：deepseek.handle_message 与 engine.handle 原本各算一次
        resolve_group；settings 内容不变时第二次起命中缓存。容量 128，
        满了整体清空；reload_cfg 时清空。返回的 dict 只读，不要原地改。
        """
        key = (str(gid), self._settings_key(settings))
        hit = self._cfg_cache.get(key)
        if hit is not None:
            return hit
        eff = resolve_group(self.cfg, settings)
        if len(self._cfg_cache) >= 128:
            self._cfg_cache.clear()
        self._cfg_cache[key] = eff
        return eff

    async def _record_raw(self, gid: str, parsed, eff: dict):
        """原始语料落库 + 每 200 条裁剪。P0-1：sqlite 写丢线程池。"""
        try:
            rid = await asyncio.to_thread(
                storage.insert_raw_message,
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
                await asyncio.to_thread(
                    storage.trim_raw,
                    self.db_path, gid,
                    int(eff.get('raw_corpus_cap', 3000)))
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
        # P1-5：配置变了，群级生效配置缓存立即失效
        self._cfg_cache.clear()
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
        if not group_row.get('group_name'):
            self._schedule_group_name(gid)

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
        # P1-5：群级配置走缓存（与 engine.handle 共用同一份，避免算两遍）
        eff = self.cfg_for_group(gid, group_row.get('settings'))
        if (eff.get('ambient_context_on', True)
                or eff.get('learn_style_on', True)):
            await self._record_raw(gid, parsed, eff)

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
            # 锁内重新读取（指令可能刚改过设置）；P0-1：同步查询丢线程池
            row = await asyncio.to_thread(storage.get_group, self.db_path, gid)
            try:
                await self.engine.handle(event, parsed, row)
            except Exception:
                self.log('对话处理异常:\n' + traceback.format_exc())

    def _schedule_group_name(self, gid: str):
        """首次见到某群时异步补一次群名（失败无副作用，下条消息再试）。

        P1-9：task 存进 self._name_tasks，on_stop 时统一 cancel+gather，
        避免关停后后台任务还在跑 RPC / 写库。
        """
        if gid in self._name_tasks:
            return
        try:
            task = asyncio.get_running_loop().create_task(
                self._fill_group_name(gid))
        except RuntimeError:
            return
        self._name_tasks[gid] = task

    async def _cancel_name_tasks(self):
        """on_stop：取消未完成的补群名任务并回收（不让它们跨关停存活）。"""
        tasks = [t for t in self._name_tasks.values() if not t.done()]
        self._name_tasks.clear()
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _fill_group_name(self, gid: str):
        try:
            bot = self.bot
            data = None
            if bot is not None and gid.isdigit():
                data = await bot.call_api('get_group_info', {'group_id': int(gid)})
            name = (data or {}).get('group_name') if isinstance(data, dict) else ''
            if name:
                storage.upsert_group(self.db_path, gid, group_name=name)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.log('补群名失败 %s:\n%s' % (gid, traceback.format_exc()))
        finally:
            if self._name_tasks.get(gid) is asyncio.current_task():
                self._name_tasks.pop(gid, None)

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
                # NapCat 扩展通知（戳一戳/资料赞/群名变更/表情回应…）走策略表：
                # 该回的回、该忽略的明确忽略，不让它们"看起来坏了"
                await notices.handle(self, event)
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
        # 私聊会话与群共用 groups 表：打上 chat_type 标记，
        # 提醒/定时唤醒等后台任务据此选择 private 发送通道
        if (row.get('settings') or {}).get('chat_type') != 'private':
            _settings = dict(row.get('settings') or {})
            _settings['chat_type'] = 'private'
            storage.upsert_group(self.db_path, uid, settings=_settings)
            row = storage.get_group(self.db_path, uid) or row
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
            row = await asyncio.to_thread(storage.get_group, self.db_path, uid)
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
        eff = self.cfg_for_group(gid, group_row.get('settings'))
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
        name_cache: dict = {}
        display_text = self.engine._user_display_text(parsed, name_cache)
        item = self.engine.build_interrupt_item(parsed, display_text, name_cache)
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
        if _command_mutates(action, arg):
            # P1-8：会改群状态的指令与对话轮共用群锁，避免一边读旧配置
            # 一边被改；两个调用点（handle_message/_handle_private）都在
            # 锁外，这里加锁不会重入死锁。长跑的 approve/reject 例外：
            # 正在等审批的那一轮持着群锁，加锁必死锁，故走无锁分支。
            async with self.group_lock(gid):
                reply = await self._run_command(action, arg, group_row, gid)
        else:
            reply = await self._run_command(action, arg, group_row, gid)
        if reply:
            await self.bot.send_message(message=reply, target_id=gid,
                                        message_type=chat_type)
        return True

    async def _approval_cmd(self, arg: str, approved: bool) -> str:
        m = re.search(r'\d+', arg or '')
        if not m:
            return '用法：/ai 批准 <id> 或 /ai 拒绝 <id>'
        aid = int(m.group(0))
        ok = await self.engine.approvals.decide(
            aid, approved, f'{self.bot_id}')
        if not ok:
            return f'#{aid} 不存在或已处理'
        return f'#{aid} 已{"批准" if approved else "拒绝"}'

    async def _run_command(self, action: str, arg: str, group_row: dict,
                           gid: str) -> str:
        if action in ('approve', 'reject'):
            return await self._approval_cmd(arg, action == 'approve')
        if action == 'approvals':
            try:
                rows = self.engine.approvals.pending()
            except Exception:
                rows = []
            if not rows:
                return '当前没有待审批的请求'
            lines = ['待审批：']
            for r in rows[:10]:
                lines.append(f"#{r.get('id')} {r.get('tool_key')} "
                             f"{r.get('args_preview') or ''}")
            return '\n'.join(lines)
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
            f'上下文模式：{"截尾保留尾部 " + str(eff.get("tail_keep_msgs")) + " 条" if eff.get("summary_mode") == "tail" else "LLM 滚动摘要"}\n'
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
                 'qzone.list': '读空间说说列表',
                 'qzone.detail': '看说说正文和评论树',
                 'qzone.images': '看空间配图',
                 'qzone.feeds': '看好友动态',
                 'qzone.comment': '评论/回复空间评论（主人）',
                 'qzone.like': '空间点赞（主人）',
                 'message.read_image': '回看消息里的图片',
                 'message.save_image': '保存消息图片到工作区',
                 'message.read_forward': '展开合并转发',
                 'meme.lookup': '梗指南查梗'}
        # 空间工具只在通道可用时列出来：official / 老版 NapCat 下它们本来
        # 就被引擎摘掉了，这里再列一遍会让人以为「明明没开却显示有」
        qz_ok = bool(getattr(self, 'qzone', None)
                     and self.qzone.available())
        for key in _ALL_TOOL_KEYS:
            if not qz_ok and key.startswith('qzone.'):
                continue
            on = overrides.get(key, True)
            lines.append(f'{"✅" if on else "❌"} {names.get(key, key)}')
        if not qz_ok:
            lines.append('（QQ 空间工具未列出：当前通道没有空间接口）')
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
