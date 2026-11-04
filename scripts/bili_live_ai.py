# -*- coding: utf-8 -*-
"""B 站直播 AI —— M2/M3/M5（弹幕接收 → 聚合 → deepseek 大脑 → 弹幕 + TTS）
=====================================================================
设计文档：docs/bilibili-live-plan.md

双向打通（M2）：
  - 扫码登录：复用 scripts/meme_import_bili.py 的网页扫码流程，凭据落在
    plugins/deepseek/db/bili_credential.json（0600、gitignored，与梗库
    脚本共用一份）；首次运行终端打印二维码，之后自动复用；脚本运行中
    检测到文件被外部更新会热加载；
  - 真实发弹幕：FakeBot.speak 把她的话经 Core.reply.split_reply 切成
    ≤19 字的条（留 1 字给去重后缀），串行发 B 站弹幕（≥1.1s 间隔、
    60s 内重复内容加轻量后缀绕过去重，绝不改正文落库）；
  - 直播版系统提示词：进程内包装 Engine._system_prompt，替换 QQ 群
    场景句并追加直播规则，不动插件全局提示词（QQ 群不受污染）；
  - 工具裁剪：直播 gid 的群工具开关只留 qq.text / qq.voice / time.now /
    memory.* / task.*，其余内置工具全部关闭；插件实例的 MCP 与
    自定义工具源直接置空（模型看不到、硬调也会落「工具不可用」）。

面板托管（M3，--managed --session-id N）：
  - 配置全部读 live_rooms 行（面板可改、5s 内热生效）：聚合间隔、单批
    上限、SC/礼物即时触发、模型路由；人设读 groups.persona（引擎每轮
    现取，天然热生效）；
  - 每 10s 心跳写 live_sessions.stats（弹幕数/AI 轮/已发/队列/最近弹幕
    等），面板据此显示运行态；enabled=0 为软停止：冲刷弹幕与 TTS、
    跑下播复盘（摘要 + 强制记忆抽取）、end 场次后退出；
  - 露面观众写 live_identity（M4 绑定面板的数据源）。

流式语音（M5）：
  - 出站三通道：她的台词（终态文本与 qq.text 同路）只进 TTS 队列念出来、
    默认不发弹幕；qq.voice 素材进独立播放队列（ffmpeg 解码直放）；素材
    播放窗口内排队的台词自动改走弹幕（Speaker divert 回调），两条声轨
    不打架；TTS 不可用时台词回退纯弹幕。详见 scripts/bili_tts.py；
    --no-tts 关语音。

会话隔离：gid = bili:<真实房间号>，人设/历史/摘要/记忆与 QQ 群互不
干扰；观众身份 bili:<uid>（M4 做 QQ 绑定）。登录后自动过滤自己发的
弹幕，防自环。

依赖：
    pip install bilibili-api-python aiohttp   # 直播连接（aiohttp 必需）
    pip install sounddevice                    # TTS 本地播放（不装则自动哑火）

用法（仓库根）：
    python scripts/bili_live_ai.py --room 123456            # 首次会弹扫码
    python scripts/bili_live_ai.py --room 123456 --interval 20 --max 30
    python scripts/bili_live_ai.py --room 123456 --no-send  # 只收不发（测试）
    python scripts/bili_live_ai.py --room 123456 --anonymous  # 匿名（不登不收自己）
    python scripts/bili_live_ai.py --room 123456 --no-tts  # 只发弹幕不出声
    （--managed/--session-id 由 Web 面板启动时自动传入，不要手填）

注意：直播中每轮结束后插件会照常调度记忆抽取/心情判定等后台 LLM
任务（与线上同路径），会产生 token 费用。
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import logging
import os
import re
import sys
import time
import traceback

# ====== 路径：仓库根（utils/core 包）+ 插件目录（Core 包、deepseek.py）=========
#          + scripts 目录（复用 meme_import_bili 的扫码登录 + bili_tts）=======
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)
_PLUGIN_DIR = os.path.join(_REPO_ROOT, 'plugins', 'deepseek')
for p in (_REPO_ROOT, _PLUGIN_DIR, _SCRIPT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from bilibili_api import Credential  # noqa: E402
from bilibili_api.live import Danmaku, LiveDanmaku, LiveRoom  # noqa: E402

from Core import config as cfg_mod  # noqa: E402
from Core import storage  # noqa: E402
from Core.models import ParsedMessage  # noqa: E402
from Core.reply import sanitize_model_text, split_reply  # noqa: E402

import meme_import_bili as mi  # noqa: E402  只复用其纯函数/扫码流程
import bili_tts  # noqa: E402     火山流式 TTS + 本地播放（sounddevice 懒加载）


# ═══════════════════════════════════════════════════════════════════════
# 登录凭据：扫码复用梗库脚本那份 bili_credential.json，支持热加载
# ═══════════════════════════════════════════════════════════════════════

class CredHolder:
    """直播脚本唯一的凭据出口。接收重连/发弹幕都从这里取最新 Credential。"""

    def __init__(self, log):
        self.log = log
        self.cred: Credential | None = None
        self.uid = 0
        self._mtime = -1.0

    async def ensure_login(self):
        """有效凭据直接复用；无效/缺失走终端扫码（流程与梗库脚本完全一致）。"""
        cred = await mi.ensure_login()
        self.cred = cred
        self.uid = int(getattr(cred, 'dedeuserid', 0) or 0)
        try:
            self._mtime = os.path.getmtime(mi.CRED_PATH)
        except OSError:
            pass
        if self.uid:
            self.log(f'🔑 直播账号 uid={self.uid}（自环过滤已开启）')

    def use_anonymous(self):
        self.cred = Credential()
        self.uid = 0
        self.log('⚠ 匿名模式：不登录、不发弹幕（或 --no-send），自己的弹幕无法过滤')

    def maybe_reload(self):
        """文件被外部（面板/重新扫码）更新时热加载，不打断现有连接。"""
        try:
            mt = os.path.getmtime(mi.CRED_PATH)
        except OSError:
            return
        if mt == self._mtime:
            return
        cred = mi._load_credential()
        if cred is not None and getattr(cred, 'sessdata', None):
            self.cred = cred
            self.uid = int(getattr(cred, 'dedeuserid', 0) or 0)
            self.log('🔑 检测到凭据文件更新，已热加载新登录')
        self._mtime = mt


# ═══════════════════════════════════════════════════════════════════════
# 运行态统计（心跳/面板共用）
# ═══════════════════════════════════════════════════════════════════════

class LiveStats:
    def __init__(self):
        self.danmaku = 0       # 收到的观众弹幕（自环过滤后）
        self.sc = 0
        self.gift = 0
        self.turns = 0         # AI 轮次
        self.silent = 0
        self.sent = 0          # 成功发出的弹幕条数
        self.send_fail = 0
        self.last_dm = ''
        self.started_at = time.time()

    def snapshot(self, extra_qsize: int = 0) -> dict:
        return {
            'pid': os.getpid(),
            'danmaku': self.danmaku,
            'sc': self.sc,
            'gift': self.gift,
            'turns': self.turns,
            'silent': self.silent,
            'sent': self.sent,
            'send_fail': self.send_fail,
            'queue': extra_qsize,
            'last_dm': (self.last_dm or '')[:40],
        }


# ═══════════════════════════════════════════════════════════════════════
# 弹幕发送：切条 + 限频 + 重复内容轻量变换
# ═══════════════════════════════════════════════════════════════════════

# 直播 gid 允许她保留的工具（其余内置工具在群工具开关里全部关闭）
_KEEP_TOOLS = frozenset({
    'qq.text', 'qq.voice', 'time.now',
    'memory.recall', 'memory.remember',
    'memory.recall_self', 'memory.remember_self',
    'task.add', 'task.close', 'task.list',
})

# B 站弹幕单条 20 字硬顶；切到 19 留 1 字给去重后缀
_DM_MAX_LEN = 19
_DM_HARD_LIMIT = 20
_SEND_GAP = 1.1          # 同房间两条弹幕的最小间隔（秒）
_RECENT_WIN = 60.0       # 去重窗口
_JITTER_SUFFIXES = ('~', '～', '。', '.', '…')


class DanmakuSender:
    """串行弹幕发送队列：AI 轮只入队，发送节奏不反压对话。"""

    def __init__(self, room_id: int, holder: CredHolder, log,
                 dry_run: bool = False, stats: LiveStats | None = None):
        self.room_id = room_id
        self.holder = holder
        self.log = log
        self.dry_run = dry_run
        self.stats = stats
        self._q: asyncio.Queue = asyncio.Queue()
        self._recent: collections.deque = collections.deque()
        self._task: asyncio.Task | None = None

    @property
    def qsize(self) -> int:
        return self._q.qsize()

    def start(self):
        self._task = asyncio.get_event_loop().create_task(self._worker())

    async def speak(self, text: str):
        """她的一段出站文本：净化 → 按 19 字切条 → 入队。可被多条 chunk 调用。"""
        text = sanitize_model_text(text)
        for line in text.splitlines() or ['']:
            line = line.strip()
            if not line:
                continue
            for ch in split_reply(line, max_len=_DM_MAX_LEN, min_len=4):
                ch = ch.strip()
                if ch:
                    await self._q.put(ch)

    async def close(self, timeout: float = 12.0):
        """等队列排空（给最后一批回复留时间），再收工人。"""
        if self._task is None:
            return
        deadline = time.monotonic() + timeout
        while not self._q.empty() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        left = self._q.qsize()
        if left:
            self.log(f'⚠ 退出时还有 {left} 条弹幕未发出')

    async def _worker(self):
        while True:
            text = await self._q.get()
            try:
                text = self._dedupe(text)
                await self._send_once(text)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log('弹幕发送异常:\n' + traceback.format_exc(), 'error')
            finally:
                self._q.task_done()
            await asyncio.sleep(_SEND_GAP)

    def _dedupe(self, text: str) -> str:
        """60s 内发过相同内容则追加一个自然短后缀（仅影响弹幕显示，不落库）。"""
        now = time.time()
        while self._recent and now - self._recent[0][1] > _RECENT_WIN:
            self._recent.popleft()
        recent = {t for t, _ in self._recent}
        if text not in recent:
            self._recent.append((text, now))
            return text
        for suf in _JITTER_SUFFIXES:
            cand = text + suf
            if cand not in recent and len(cand) <= _DM_HARD_LIMIT:
                self._recent.append((cand, now))
                return cand
        self._recent.append((text, now))
        return text   # 已满 20 字没法加后缀，硬发（由服务端裁决）

    async def _send_once(self, text: str):
        if self.dry_run:
            self.log(f'📤 [dry-run] {text}')
            if self.stats:
                self.stats.sent += 1
            return
        # 每次现建 LiveRoom：凭据热加载后不必同步替换持有对象
        room = LiveRoom(room_display_id=self.room_id,
                        credential=self.holder.cred)
        res = await room.send_danmaku(Danmaku(text=text), room_id=self.room_id)
        code = res.get('code', 0) if isinstance(res, dict) else 0
        if code:
            self.log(f'⚠ 弹幕被拒 code={code} '
                     f'msg={res.get("message") or res.get("msg", "")}：{text}',
                     'warn')
            if self.stats:
                self.stats.send_fail += 1
            return
        self.log(f'📤 已发弹幕：{text}')
        if self.stats:
            self.stats.sent += 1


# ═══════════════════════════════════════════════════════════════════════
# FakeBot：接管出站（文本 → 弹幕 + TTS；record/video → 播放钩子）
# ═══════════════════════════════════════════════════════════════════════

_CQ_RE = re.compile(r'^\s*\[CQ:([a-zA-Z0-9_]+)')


class FakeBot:
    """引擎对话链路里唯一真正需要的 bot 接口。出站三通道各司其职：

    - 台词（终态分条回复与 qq.text 工具同等待遇）→ 只进 TTS 队列念出来，
      不发弹幕；Speaker 在播放语音素材期间会把排队台词经 divert 回调
      改成弹幕（避免两条声轨打架）；
    - qq.voice/qq.video（[CQ:record/video,file=...]）→ 进 Speaker 的
      素材播放队列，ffmpeg 解码后直接本地播放；
    - TTS 不可用（未配置/无声卡/--no-tts）时台词回退为纯弹幕。
    - call_api：get_version_info / 群成员信息等查询一律失败，
      引擎各处都包了容错（detect_transport 60s 缓存降级 napcat）。
    """

    def __init__(self, log, sender: DanmakuSender | None = None,
                 speaker: bili_tts.Speaker | None = None):
        self._log = log
        self._sender = sender
        self._speaker = speaker
        # 素材播放窗口内，Speaker 用它把排队中的台词改走弹幕通道
        if speaker is not None and sender is not None:
            speaker.set_divert(sender.speak)

    async def send_message(self, message='', target_id=None,
                           message_type='group', extra=None, **kwargs):
        raw = str(message or '')
        m = _CQ_RE.match(raw)
        kind = m.group(1).lower() if m else 'text'
        if kind == 'text':
            text = sanitize_model_text(raw)
            for line in text.splitlines() or ['']:
                if line.strip():
                    self._log(f'🤖 她：{line}')
            voice_ok = self._speaker is not None and self._speaker.enabled
            if voice_ok:
                # 台词只念不发；素材播放期间由 Speaker divert 成弹幕
                self._speaker.speak_soon(text)
            elif self._sender is not None:
                # 没有语音能力时回退为纯弹幕，不能让她彻底闭嘴
                await self._sender.speak(text)
        elif kind in ('record', 'video'):
            await self.play_file(_cq_field(raw, 'file') or raw)
        elif kind == 'image':
            self._log(f'🖼 [图片出站，直播不处理] {_cq_field(raw, "file") or raw[:80]}')
        else:
            self._log(f'📎 [{kind} 出站，直播不处理] {raw[:120]}')
        return {'message_id': 0}

    async def call_api(self, action: str, params=None, **kwargs):
        raise RuntimeError(f'FakeBot 不支持 OneBot API：{action}')

    async def play_file(self, path: str):
        """qq.voice/qq.video：成品素材进独立播放队列（ffmpeg 解码直放）。

        file 形态：http(s) 链接 / base64://（引擎对本地文件已内联成
        base64）/ 裸路径。绝不能塞给 TTS——那是「放录音」不是「念台词」。"""
        if self._speaker is not None and self._speaker.enabled:
            self._speaker.play_soon(str(path or ''))
            self._log('🎵 语音素材已入播放队列')
            return
        clip = str(path or '')
        if len(clip) > 80:
            clip = clip[:77] + '...'
        self._log(f'🔇 [语音素材但本地播放不可用，已丢弃] {clip}')


def _cq_field(cq: str, field: str) -> str:
    m = re.search(rf'(?:^|,){re.escape(field)}=([^,\]]*)', cq)
    return m.group(1) if m else ''


# ═══════════════════════════════════════════════════════════════════════
# 直播版系统提示词（仅本进程生效，不碰插件全局提示词）
# ═══════════════════════════════════════════════════════════════════════

_LIVE_SCENE = ('你正在 B 站直播间里当 AI 主播，眼前滚动的是观众实时发来的弹幕，'
               '不是 QQ 群聊。')

# 默认 identity 模板里 QQ 身份段的原文（用户在面板自定义过 identity 时
# replace 会静默落空，不影响追加的直播规则块生效）
_QQ_IDENTITY_SEG = ('对话记录每行格式为「昵称(QQ:号): 内容」，括号里的 QQ 号是每个人的'
                    '唯一身份——昵称会重复、会改，认人一律以 QQ 号为准；'
                    '记录长期记忆、约定责任人、判断谁在说话时都用 QQ 号，不要只凭昵称。')
_LIVE_IDENTITY_SEG = ('弹幕记录每行格式为「昵称(QQ:bili:UID): 内容」，括号里形如 '
                      'bili:123456 的编号是该观众的 B 站 UID（不是真 QQ 号，'
                      '未登录游客显示 bili:0）——昵称会重复、会改，认人、记长期记忆、'
                      '约定责任人一律以 bili:UID 为准，不要只凭昵称。')

_LIVE_RULES = """
【直播场景规则（覆盖上文一切与 QQ 群有关的说法）】
- 你是 B 站直播间的 AI 主播，观众发的是实时滚动弹幕，来去自由、话题跳得快。
- 记录行首括号里的 bili:数字 是观众的 B 站 UID（不是 QQ 号；游客是 bili:0），认人以它为准。
- 观众是「听」你说话的：你正常输出的每句话都会用你的声音 TTS 念出来，默认不显示成弹幕。
  所以要说短句、说人话、口语化，像真人接话；不要列表、标题、Markdown、长段落，
  一件事拆成两三句短的分别说。
- 想播放现成的语音素材（音效、录音梗）时才调用 qq.voice 工具；素材播放期间
  你说的话系统会自动改成文字弹幕发出（避免和素材声音撞在一起），你无需特意安排。
- 醒目留言（SC）和礼物是观众真金白银的支持，被点名时必须热情、明确地当场感谢。
- 接不上话、没必要开口时照常只回 [静默]；没人喜欢自说自话。
- 不要提 QQ、群聊、CQ 码、工具或内部流程，也不要用 @ 符号。""".strip()


def install_live_prompt() -> None:
    """进程内包装 Engine._system_prompt：QQ 场景句 → 直播场景 + 追加规则。"""
    from Core.engine import Engine
    orig = Engine._system_prompt

    def _patched(persona: str, cfg: dict, has_tools: bool,
                 has_mcp: bool = False, bot_id=0, bot_name: str = '',
                 chat_type: str = 'group', store=None, catalog: str = ''):
        msg = orig(persona, cfg, has_tools, has_mcp, bot_id, bot_name,
                   chat_type, store, catalog)
        content = msg['content']
        content = content.replace('你在一个 QQ 群里。', _LIVE_SCENE)
        content = content.replace('这是一对一 QQ 私聊。', _LIVE_SCENE)
        content = content.replace(_QQ_IDENTITY_SEG, _LIVE_IDENTITY_SEG)
        if _LIVE_RULES not in content:
            content = content + '\n\n' + _LIVE_RULES
        return {'role': 'system', 'content': content}

    Engine._system_prompt = staticmethod(_patched)


# ═══════════════════════════════════════════════════════════════════════
# 弹幕接收（bilibili-api-python，登录连接；自带心跳/重连/反爬）
# ═══════════════════════════════════════════════════════════════════════

async def resolve_room(display_id: int, cred: Credential) -> int:
    """短房间号 → 真实 room_id（启动前确定 gid，并对房间不存在快速失败）。"""
    room = LiveRoom(room_display_id=int(display_id), credential=cred)
    return int(await room.get_room_id())


async def danmaku_receiver(room_display: int, log, stop: asyncio.Event,
                           out: asyncio.Queue, holder: CredHolder,
                           brain: 'LiveBrain', stats: LiveStats):
    """断线整体重连的弹幕接收循环；只把规范化事件塞进 out。

    每轮新建 LiveDanmaku：重新取 danmu token 与主机列表，并重读凭据
    （热加载的新登录在下一轮重连生效）。回调只做 put_nowait，绝不
    在回调里 await AI。SC/礼物按 brain 上的面板配置决定是否立优先。
    """
    backoff = 2
    while not stop.is_set():
        holder.maybe_reload()
        dm = LiveDanmaku(room_display, credential=holder.cred,
                         max_retry=5, retry_after=2)
        # 库自己的 INFO 日志太吵（心跳/重连），只留警告以上
        dm.logger.setLevel(logging.WARNING)

        def _push(item: dict):
            # 有界队列：满了丢最旧，绝不让慢的 AI 轮反压弹幕接收
            try:
                out.put_nowait(item)
            except asyncio.QueueFull:
                try:
                    out.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    out.put_nowait(item)
                except asyncio.QueueFull:
                    pass

        @dm.on('DANMU_MSG')
        async def _on_danmu(ev: dict):
            # 库事件结构：ev['data'] 是完整 cmd payload，弹幕在其 ['info']
            payload = ev.get('data') or {}
            info = payload.get('info') or []
            if len(info) < 3 or not isinstance(info[2], list):
                return
            text = str(info[1] or '').strip()
            uid, name = info[2][0], str(info[2][1] or '').strip()
            if not text:
                return
            if holder.uid and int(uid or 0) == int(holder.uid):
                return  # 自环过滤：登录账号自己发的弹幕
            stats.danmaku += 1
            stats.last_dm = f'{name}：{text}'
            _push({'kind': 'danmaku', 'uid': str(uid or ''),
                   'name': name, 'text': text,
                   'line': f'{name}：{text}', 'ts': time.time()})

        @dm.on('SUPER_CHAT_MESSAGE')
        async def _on_sc(ev: dict):
            data = (ev.get('data') or {}).get('data') or {}
            uinfo = data.get('user_info') or {}
            name = str(uinfo.get('uname') or '').strip()
            uid = str(uinfo.get('uid') or '')
            price = float(data.get('price') or 0)
            text = str(data.get('message') or '').strip()
            stats.sc += 1
            log(f'💎 SC {name} ¥{price:g}：{text}')
            if not text:
                return
            _push({'kind': 'sc', 'uid': uid, 'name': name, 'text': text,
                   'line': f'{name}：[醒目留言 ¥{price:g}] {text}',
                   'priority': bool(brain.immediate_sc), 'ts': time.time()})

        @dm.on('SEND_GIFT')
        async def _on_gift(ev: dict):
            data = (ev.get('data') or {}).get('data') or {}
            name = str(data.get('uname') or '').strip()
            uid = str(data.get('uid') or '')
            gift_name = str(data.get('giftName') or '礼物')
            num = int(data.get('num') or 1)
            # gold=金瓜子（1000=1 元），silver=免费银瓜子，不计价
            yuan = 0.0
            if str(data.get('coin_type') or '') == 'gold':
                yuan = float(data.get('total_coin') or 0) / 1000.0
            stats.gift += 1
            log(f'🎁 {name} {data.get("action", "投喂")} '
                f'{gift_name} x{num}' + (f'（约 ¥{yuan:g}）' if yuan else ''))
            if not brain.immediate_gift:
                return
            if brain.gift_threshold > 0 and yuan < brain.gift_threshold:
                return
            price_txt = f'，约合 {yuan:g} 元' if yuan else ''
            _push({'kind': 'gift', 'uid': uid, 'name': name,
                   'text': f'投喂了{gift_name}x{num}',
                   'line': f'（系统提示：{name} 刚刚投喂 {gift_name} x{num}'
                           f'{price_txt}，请像真人主播一样当场口头感谢）',
                   'priority': True, 'ts': time.time()})

        @dm.on('GUARD_BUY')
        async def _on_guard(ev: dict):
            data = (ev.get('data') or {}).get('data') or {}
            name = str(data.get('username') or '').strip()
            stats.gift += 1
            log(f'🛡 {name} 上舰：{data.get("gift_name", "")}')
            _push({'kind': 'guard', 'uid': '', 'name': name,
                   'text': '上舰了',
                   'line': f'（系统提示：{name} 刚刚开通了舰长'
                           f'（{data.get("gift_name", "")}），这是重金支持，'
                           '必须热情地当场郑重感谢）',
                   'priority': bool(brain.immediate_gift), 'ts': time.time()})

        @dm.on('LIVE')
        async def _on_live(_ev):
            log('🔴 开播事件')

        @dm.on('PREPARING')
        async def _on_preparing(_ev):
            log('⚪ 下播事件')

        try:
            log(f'🔌 连接房间 {room_display} 的弹幕服务器……')
            await dm.connect()
            # connect 返回 = 连接彻底结束（主机全部失败或被关闭）
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not stop.is_set():
                log(f'⚠ 弹幕连接异常（{backoff}s 后重连）：{e}', 'warn')
        finally:
            try:
                await dm.disconnect()
            except Exception:
                pass
        if stop.is_set():
            break
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


# ═══════════════════════════════════════════════════════════════════════
# 聚合 → 插件大脑
# ═══════════════════════════════════════════════════════════════════════

class LiveBrain:
    def __init__(self, room_id: int, interval: int, batch_max: int, log,
                 stats: LiveStats):
        self.gid = f'bili:{room_id}'
        self.interval = max(3, int(interval))
        self.batch_max = max(1, int(batch_max))
        self.immediate_sc = True
        self.immediate_gift = False
        self.gift_threshold = 0
        self.log = log
        self.stats = stats
        self.lock = asyncio.Lock()
        self._plugin = None
        self.cfg = None

    def setup(self, sender: DanmakuSender | None = None,
              speaker: bili_tts.Speaker | None = None):
        """裸构造插件：不跑 on_start（会起 trigger/live HTTP 服务）。"""
        import deepseek as deepseek_plugin
        from Core import builtin_tools
        plugin = deepseek_plugin.main(None, FakeBot(self.log, sender, speaker))
        # MCP（远程桌面读写操控）与直播无关且依赖 on_start 生命周期，继续置空；
        # 自定义工具保留：它是 SQLite 里的代码、独立子进程执行，只收文本 ctx、
        # 拿不到 bot，返回值仅喂给模型。全局工具自动对直播间生效
        # （bili:<room> 作用域工具无任何发布入口，实践中只会有全局工具）。
        # 注意：标了「仅主人」的工具观众触发会被引擎拦截；需要写群工作区文件的
        # 工具会因 workspace/bili:<room> 路径在 Windows 含冒号而失败。
        plugin.mcp = None

        storage.get_or_create_group(plugin.db_path, self.gid)
        self._harden_group(plugin)
        self._prune_tools(plugin, builtin_tools)
        self._plugin = plugin

        self.rebuild_cfg()
        self.log(f'🧠 大脑就绪，会话 gid={self.gid}（模型：'
                 f'{cfg_mod.route_label(self.cfg, "model")}）')
        return plugin

    @property
    def db_path(self) -> str:
        return self._plugin.db_path

    def rebuild_cfg(self, model_route: str = ''):
        """从群 settings 重算有效 cfg；model_route 非空时面板路由覆盖。"""
        row = storage.get_group(self._plugin.db_path, self.gid)
        s = dict(row.get('settings') or {})
        if model_route:
            s['model'] = model_route
        self.cfg = cfg_mod.resolve_group(self._plugin.cfg, s)

    def _harden_group(self, plugin):
        """直播 gid 的发言形态：分条 ≤20 字、条间短延迟（真正的限频由
        DanmakuSender 保证）。每次启动强制写回，M3 面板再做可视化编辑。"""
        row = storage.get_group(plugin.db_path, self.gid)
        s = dict(row.get('settings') or {})
        s.update({
            'split_reply': True,
            'split_max_len': 20,
            'split_min_len': 4,
            'split_delay_min_ms': 300,
            'split_delay_max_ms': 800,
        })
        storage.upsert_group(plugin.db_path, self.gid, settings=s)

    def _prune_tools(self, plugin, builtin_tools):
        """只保留说话/语音素材/时间/记忆/约定十把内置工具，其余内置工具在该
        gid 关闭；自定义工具（custom__*）不经过这张白名单，另行保留。

        写 group_tool_state（与面板工具勾选同一张表、同一套 override 语义），
        upsert 幂等；直播 gid 由本脚本托管，覆盖用户旧勾选是预期行为。"""
        for key in sorted(set(builtin_tools.TOOL_KEYS.values())):
            if key not in _KEEP_TOOLS:
                storage.set_tool_override(plugin.db_path, self.gid,
                                          key, False)
        try:
            rows = storage.list_custom_tools(plugin.db_path, self.gid,
                                             enabled_only=True)
        except Exception:
            rows = []
        if rows:
            names = '、'.join(f'custom__{r["name"]}' for r in rows)
            self.log(f'🛠 本场可用自定义工具 {len(rows)} 个：{names}')

    async def run_turn(self, items: list[dict]):
        """一批弹幕 → 一条 user 消息落库 → 一轮 _run_turn（群锁串行）。"""
        p = self._plugin
        first = items[0]
        # 本批出现的观众入目录（记忆按 user_id 键控）+ 直播露面计数
        seen = set()
        for it in items:
            uid_raw = it.get('uid') or ''
            uid = f'bili:{uid_raw}' if uid_raw else 'bili:0'
            if uid in seen:
                continue
            seen.add(uid)
            storage.upsert_user(p.db_path, uid, it['name'], self.gid)
            # 游客（bili:0）不进绑定候选表
            if uid_raw and uid_raw != '0':
                storage.upsert_live_identity(
                    p.db_path, uid_raw, it['name'], inc_msg=1)
        batch_text = '\n'.join(it.get('line') or
                               f"{it['name']}：{it['text']}" for it in items)
        first_uid = (f"bili:{first['uid']}" if first.get('uid')
                     else 'bili:0')
        storage.insert_message(p.db_path, self.gid, 'user', batch_text,
                               sender_id=first_uid, sender_name=first['name'])
        storage.upsert_group(p.db_path, self.gid, touch=True)

        parsed = ParsedMessage(
            text=batch_text, sender_id=first_uid,
            sender_name=first['name'], at_me=True,
            chat_type='group', allow_silent=True)
        kinds = '/'.join(sorted({it.get('kind', 'danmaku') for it in items}))
        self.log(f'━━━ 聚合 {len(items)} 条（{kinds}），送 AI ━━━')
        self.stats.turns += 1
        async with self.lock:
            try:
                res = await p.engine._run_turn(
                    self.gid, self.cfg, parsed, True)
                if res.get('silent'):
                    self.stats.silent += 1
                    self.log('（她选择静默，本轮未发言）')
            except Exception:
                self.log('对话轮异常:\n' + traceback.format_exc(), 'error')

    async def aclose(self):
        p = self._plugin
        if p is None:
            return
        try:
            await p.llm.aclose()
        except Exception:
            pass


async def aggregator(brain: LiveBrain, queue: asyncio.Queue,
                     stop: asyncio.Event, log):
    while not stop.is_set():
        try:
            first = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        items = [first]
        if first.get('kind') == 'danmaku':
            log(f"📨 {first['name']}：{first['text']}")
        # SC/礼物优先事件：立即成批，不等聚合窗口
        deadline = (time.monotonic() if first.get('priority')
                    else time.monotonic() + brain.interval)
        while len(items) < brain.batch_max:
            left = deadline - time.monotonic()
            if left <= 0 or stop.is_set():
                break
            try:
                it = await asyncio.wait_for(queue.get(), timeout=min(left, 1.0))
            except asyncio.TimeoutError:
                continue
            items.append(it)
            if it.get('kind') == 'danmaku':
                log(f"📨 {it['name']}：{it['text']}")
            # 窗口里又进来优先事件：收完队列里已排队的立刻发车
            if it.get('priority'):
                while not queue.empty() and len(items) < brain.batch_max:
                    items.append(queue.get_nowait())
                break
        await brain.run_turn(items)


# ═══════════════════════════════════════════════════════════════════════
# 面板托管：配置轮询 + 心跳 + 下播复盘（M3/M4）
# ═══════════════════════════════════════════════════════════════════════

async def managed_config_poll(db_path: str, room_id: int, brain: LiveBrain,
                              stop: asyncio.Event, log):
    """5s 一轮读 live_rooms：enabled=0 软停止；其余参数热生效。"""
    warned = False
    last_route = None
    while not stop.is_set():
        await asyncio.sleep(5.0)
        row = storage.get_live_room(db_path, str(room_id))
        if row is None:
            if not warned:
                log('⚠ live_rooms 没有本房间配置行，沿用启动参数', 'warn')
                warned = True
            continue
        if not row['enabled']:
            log('🛑 面板请求停止（enabled=0），进入收尾……')
            stop.set()
            return
        brain.interval = max(3, row['batch_seconds'])
        brain.batch_max = max(1, row['batch_max'])
        brain.immediate_sc = bool(row['immediate_sc'])
        brain.immediate_gift = bool(row['immediate_gift'])
        brain.gift_threshold = max(0, row['gift_threshold'])
        route = (row.get('model_route') or '').strip()
        if route != last_route:
            last_route = route
            try:
                brain.rebuild_cfg(route)
                label = cfg_mod.route_label(brain.cfg, 'model')
                log(f'🔁 模型路由{("热切换：" + label) if route else "恢复跟随群设置"}')
            except Exception:
                log('模型路由热切换失败:\n' + traceback.format_exc(), 'error')


async def heartbeat_loop(db_path: str, sid: int, stats: LiveStats,
                         stop: asyncio.Event, sender: DanmakuSender,
                         speaker: bili_tts.Speaker | None):
    while not stop.is_set():
        await asyncio.sleep(10.0)
        qsize = sender.qsize
        if speaker is not None and getattr(speaker, '_jobs', None) is not None:
            qsize += speaker._jobs.qsize()
        try:
            storage.heartbeat_live_session(db_path, sid,
                                           stats.snapshot(qsize))
        except Exception:
            pass   # 心跳偶发失败不能拖死直播


_WRAPUP_SYS = (
    '你是 AI 主播的直播复盘助手。下面是一场 B 站直播的完整弹幕记录'
    '（观众弹幕与主播回复混排）。请写一份 300~500 字的中文复盘，包含：'
    '1）今晚聊了哪些话题、观众反应如何；2）有意思的互动和梗；'
    '3）她答应过观众、下次该记得或兑现的事；4）下次直播可以延续的话题。'
    '只写复盘正文，口语化分点，不要寒暄和标题。')


async def post_stream_wrapup(brain: LiveBrain, started_at: float,
                             sid: int | None, log):
    """下播复盘：场次摘要入库 + 强制记忆抽取（含 self 记忆，同线上路径）。

    面板历史场次（M4）展示 summary；force_extract 把最后一批弹幕的
    per-viewer 记忆与第一人称自我记忆全部落库。失败不影响退出。"""
    p = brain._plugin
    # 1) 场次摘要：取本场开始后的消息
    try:
        rows = storage.recent_messages(p.db_path, brain.gid, 400)
        script_lines = []
        for r in rows:
            if r['role'] not in ('user', 'assistant'):
                continue  # 跳过 tool 行等噪声
            ts = float(r.get('created_at') or 0)
            if ts and ts < started_at - 60:
                continue
            if r['role'] == 'user':
                script_lines.append(r['content'])
            else:
                script_lines.append(f'她：{r["content"]}')
        script = '\n'.join(script_lines)[-12000:]
        if script.strip() and sid:
            result = await p.llm.chat(
                [{'role': 'system', 'content': _WRAPUP_SYS},
                 {'role': 'user', 'content': script}],
                brain.cfg, route='summary_model', max_tokens=700,
                temperature=0.3, kind='summary',
                no_thinking=not brain.cfg.get('cheap_thinking_on', False))
            summary = (result.content or '').strip()
            if summary:
                storage.set_live_session_summary(p.db_path, sid, summary)
                log(f'📝 场次复盘已写入（{len(summary)} 字）')
    except asyncio.CancelledError:
        raise
    except Exception:
        log('场次复盘失败:\n' + traceback.format_exc(), 'warn')

    # 2) 强制记忆抽取（per-viewer + commitments + self 记忆，与线上同路径）
    try:
        log('🧠 下场记忆抽取中……')
        await p.memory.force_extract(brain.gid, brain.cfg, p.llm)
        log('🧠 记忆抽取完成')
    except asyncio.CancelledError:
        raise
    except Exception:
        log('下场记忆抽取失败:\n' + traceback.format_exc(), 'warn')


# ═══════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description='B 站直播 AI（M2/M3/M5）')
    ap.add_argument('--room', required=True, help='直播间号（短号/真实号均可）')
    ap.add_argument('--interval', type=int, default=20,
                    help='聚合间隔秒数（默认 20；托管模式被面板配置覆盖）')
    ap.add_argument('--max', dest='batch_max', type=int, default=30,
                    help='单批弹幕上限（默认 30，先到先触发）')
    ap.add_argument('--persona', default='',
                    help='设置/覆盖本直播间 gid 的人设后启动')
    ap.add_argument('--no-send', action='store_true',
                    help='只收不发：她的回复只打印不发弹幕（测试用）')
    ap.add_argument('--anonymous', action='store_true',
                    help='匿名连接：不扫码登录（无法发弹幕、无法过滤自己）')
    ap.add_argument('--no-tts', action='store_true',
                    help='关闭语音播放（默认她的回复会流式 TTS 念出来）')
    # 以下两个由 Web 面板启动时传入（BiliLiveManager.start）
    ap.add_argument('--managed', action='store_true',
                    help=argparse.SUPPRESS)
    ap.add_argument('--session-id', type=int, default=0,
                    help=argparse.SUPPRESS)
    args = ap.parse_args()

    def log(msg: str, level: str = 'info'):
        ts = time.strftime('%H:%M:%S')
        print(f'[{ts}] {msg}', flush=True)

    import signal as _sig

    async def _run():
        # Windows 的 Proactor 事件循环不支持 add_signal_handler；
        # signal.signal 的回调在主线程执行，用线程事件 + 轮询桥进 asyncio
        import threading
        kb = threading.Event()
        old_handlers = {}
        for sname in ('SIGINT', 'SIGTERM'):
            if hasattr(_sig, sname):
                old_handlers[sname] = _sig.getsignal(getattr(_sig, sname))
                _sig.signal(getattr(_sig, sname), lambda *_: kb.set())

        stats = LiveStats()

        # 1) 登录（与梗库脚本共用凭据文件）
        holder = CredHolder(log)
        if args.anonymous:
            holder.use_anonymous()
        else:
            await holder.ensure_login()

        # 2) 先解析真实房间号，失败直接退出
        real_id = await resolve_room(args.room, holder.cred)
        gid = f'bili:{real_id}'
        log(f'🚪 房间 {args.room} → 真实房间 {real_id}')

        # 3) 弹幕发送队列（登录且未显式 --no-send 才真发）
        dry_run = args.no_send or args.anonymous
        sender = DanmakuSender(real_id, holder, log, dry_run=dry_run,
                               stats=stats)
        sender.start()
        if dry_run:
            log('🧪 弹幕发送已禁用（dry-run），她的回复只打印到控制台')

        # 4) TTS 流式播放（缺 sounddevice / 缺配置会自己降级为哑火）
        speaker = None
        if not args.no_tts:
            speaker = bili_tts.Speaker(log)
            await speaker.start()

        # 5) 裸构造大脑 + 直播场景提示词
        install_live_prompt()
        brain = LiveBrain(real_id, args.interval, args.batch_max, log, stats)
        plugin = brain.setup(sender, speaker)
        # 注意：不要把虚拟群置 enabled=1——forced 轮不看群开关，而 enabled=1
        # 会让主 bot 进程的定时任务把它当真实 QQ 群投递（必然报错/并发跑轮）。
        storage.upsert_group(plugin.db_path, gid, touch=True)
        if args.persona:
            storage.upsert_group(plugin.db_path, gid, persona=args.persona)
            log('📝 人设已更新')

        # 6) 托管模式：读面板配置作为启动参数（之后 5s 轮询热生效）
        db_path = plugin.db_path
        managed_sid = args.session_id if args.managed else 0
        poll_task = hb_task = None
        if args.managed:
            row = storage.get_live_room(db_path, str(real_id))
            if row is not None:
                brain.interval = max(3, row['batch_seconds'])
                brain.batch_max = max(1, row['batch_max'])
                brain.immediate_sc = bool(row['immediate_sc'])
                brain.immediate_gift = bool(row['immediate_gift'])
                brain.gift_threshold = max(0, row['gift_threshold'])
                brain.rebuild_cfg((row.get('model_route') or '').strip())
                log(f'🎛 托管配置：间隔 {brain.interval}s / 上限 '
                    f'{brain.batch_max} / SC 即时 {brain.immediate_sc} / '
                    f'礼物即时 {brain.immediate_gift}（阈值 '
                    f'{brain.gift_threshold}）')
            else:
                log('⚠ 托管模式但 live_rooms 无配置，沿用命令行参数', 'warn')
            storage.heartbeat_live_session(
                db_path, managed_sid,
                stats.snapshot(sender.qsize))

        stop = asyncio.Event()
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

        async def watch_keyboard():
            while not kb.is_set():
                await asyncio.sleep(0.2)
            if not stop.is_set():
                log('🛑 收到退出信号，正在收尾……')
                stop.set()

        recv_task = asyncio.create_task(
            danmaku_receiver(real_id, log, stop, queue, holder, brain, stats))
        agg_task = asyncio.create_task(aggregator(brain, queue, stop, log))
        kb_task = asyncio.create_task(watch_keyboard())
        if args.managed:
            poll_task = asyncio.create_task(
                managed_config_poll(db_path, real_id, brain, stop, log))
            hb_task = asyncio.create_task(
                heartbeat_loop(db_path, managed_sid, stats, stop,
                               sender, speaker))
        try:
            await stop.wait()
        finally:
            for t in (recv_task, agg_task, kb_task, poll_task, hb_task):
                if t is not None:
                    t.cancel()
            await asyncio.gather(
                recv_task, agg_task, kb_task,
                *([poll_task, hb_task] if args.managed else []),
                return_exceptions=True)

            # 先把最后一轮已入队的弹幕发完
            await sender.close(timeout=12.0)
            # 再把已入队的语音播完（复盘 LLM 期间不必让观众听机器沉默）
            if speaker is not None:
                await speaker.aclose(wait=20.0)

            # 托管模式：下播复盘（场次摘要 + 强制记忆抽取）后结束场次
            if args.managed and managed_sid:
                await post_stream_wrapup(brain, stats.started_at,
                                         managed_sid, log)
                final = stats.snapshot(0)
                final['ended_by'] = 'script'
                storage.end_live_session(db_path, managed_sid,
                                         status='ended', stats=final)
                log(f'🏁 场次 #{managed_sid} 已结束')

            # 虚拟群复位为 disabled：主 bot 进程的定时任务/被动逻辑不会再
            # 把它当真实 QQ 群投递（同时治愈早期版本遗留的 enabled=1）
            try:
                storage.upsert_group(db_path, gid, enabled=0)
            except Exception:
                pass

            for sname, h in old_handlers.items():
                try:
                    _sig.signal(getattr(_sig, sname), h)
                except (ValueError, OSError):
                    pass
            # 引擎每轮结束后调度的后台 LLM 任务（记忆抽取等）给 8s 收尾
            pending = [t for t in asyncio.all_tasks()
                       if t is not asyncio.current_task()]
            if pending:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),
                        timeout=8.0)
                except asyncio.TimeoutError:
                    pass
            await brain.aclose()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    log('已退出')


if __name__ == '__main__':
    main()
