# -*- coding: utf-8 -*-
"""火山引擎单向流式 TTS（豆包语音 2.0）+ 本地流式播放（M5）。

接口文档：docs/volc_ttc.md
WSS: wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream

协议帧（火山 openspeech v3 二进制头，8 字节，实测）：
  byte0 = (version<<4)|header_size        0x11
  byte1 = (msg_type<<4)|flags（低 4 位固定 4）
  byte2 = (serialization<<4)|compression  JSON=0x10，裸音频=0x00
  byte3 保留 0x00；byte4-7 payload_size 大端 uint32
消息类型：1=客户端完整请求 0xB=音频-only 响应 0x9=完整 JSON 响应 0xF=错误
JSON/音频 payload 都是 TLV：[4B 长度][session_id][4B 长度][body]
事件由 JSON body 的键区分：空=SentenceStart，text=SentenceEnd，
words=Subtitle，usage=SessionFinished。

配置来自根 config.json（框架配置）：
  tts_apikey / tts_model(=X-Api-Resource-Id，seed-tts-2.0|seed-icl-2.0)
  / tts_speaker(音色 ID)；环境变量 TTS_API_KEY/TTS_MODEL/TTS_SPEAKER 覆盖。

运行依赖：sounddevice（本地播放）；ffmpeg 在 PATH 中（qq.voice 素材解码，
  mp3/wav/m4a/ogg/amr → 24k s16le PCM；silk 不支持）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import queue
import re
import struct
import subprocess
import threading
import time
import uuid

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT_CONFIG = os.path.join(_REPO_ROOT, 'config.json')

_WSS = 'wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream'

# 帧头常量
_VERSION = 1
_HEADER_SIZE = 1
_MSG_FULL_CLIENT = 1
_MSG_AUDIO_ONLY = 0xB
_MSG_FULL_SERVER = 0x9
_MSG_ERROR = 0xF
_SERIAL_JSON = 1
_COMPRESS_NONE = 0

# 播放参数（PCM s16le 24k 单声道）
_SAMPLE_RATE = 24000
_FRAME_BYTES = 2400          # 每次回调 50ms（1200 samples * 2 bytes）
_MAX_PENDING_BYTES = 24000 * 2 * 60   # 队列最多积压 60s，超出丢弃旧音频防越积越多
_TTS_TIMEOUT = 20.0


def build_frame(payload: bytes, msg_type: int = _MSG_FULL_CLIENT) -> bytes:
    header = bytes([
        (_VERSION << 4) | _HEADER_SIZE,
        (msg_type << 4) | 0,
        (_SERIAL_JSON << 4) | _COMPRESS_NONE,
        0,
    ]) + struct.pack('>I', len(payload))
    return header + payload


def parse_frames(data: bytes):
    """拆分一条 WS 消息里的所有协议帧，yield (msg_type, body)。

    v3 payload 是两段 TLV：[sid 长度][sid][body 长度][body]。
    实测服务端头里的 payload_size 只算第一块分片（严重偏小），真正的
    body 长度以内层 TLV 为准；一条 WS 消息也可能拼接多帧，故循环切。
    """
    off = 0
    total = len(data)
    while off + 8 <= total:
        msg_type = (data[off + 1] >> 4) & 0x0F
        if off + 12 > total:
            break
        n1 = struct.unpack('>I', data[off + 8:off + 12])[0]
        p = off + 12 + n1 + 4
        if p > total:
            break
        n2 = struct.unpack('>I', data[off + 12 + n1:p])[0]
        body = data[p:p + n2]
        yield msg_type, body
        off = p + n2


def parse_frame(data: bytes) -> tuple[int, bytes]:
    """返回第一帧的 (msg_type, body)。"""
    return next(iter(parse_frames(data)))


def load_tts_config() -> dict:
    """读根 config.json 的 TTS 三件套，env 可覆盖；文件每次重读（面板改后热生效）。"""
    out = {'api_key': '', 'resource_id': 'seed-tts-2.0',
           'speaker': '', 'speed': 0}
    try:
        with open(_ROOT_CONFIG, encoding='utf-8') as f:
            raw = json.load(f)
        out['api_key'] = str(raw.get('tts_apikey') or '')
        out['resource_id'] = str(raw.get('tts_model') or out['resource_id'])
        out['speaker'] = str(raw.get('tts_speaker') or '')
        if raw.get('tts_speed') is not None:
            try:
                out['speed'] = max(-50, min(100, int(raw.get('tts_speed'))))
            except (TypeError, ValueError):
                pass
    except (OSError, json.JSONDecodeError):
        pass
    out['api_key'] = os.environ.get('TTS_API_KEY') or out['api_key']
    out['resource_id'] = os.environ.get('TTS_MODEL') or out['resource_id']
    out['speaker'] = os.environ.get('TTS_SPEAKER') or out['speaker']
    return out


# 句切：按中英文句读切，太长再按逗号二切，单句上限 60 字（TTS 延时与自然度平衡）
_SENT_SPLIT = re.compile(r'(?<=[。！？!?…\n])')


def split_sentences(text: str, max_len: int = 60) -> list[str]:
    text = re.sub(r'\s+', ' ', (text or '').replace('\r', '')).strip()
    out: list[str] = []
    for part in _SENT_SPLIT.split(text):
        part = part.strip()
        if not part:
            continue
        while len(part) > max_len:
            cut = part.rfind('，', 0, max_len)
            if cut < max_len // 2:
                cut = part.rfind(',', 0, max_len)
            if cut < max_len // 2:
                cut = max_len - 1   # 没有逗号就硬切
            head, part = part[:cut + 1].strip(), part[cut + 1:].strip()
            if head:
                out.append(head)
        if part:
            out.append(part)
    return out


class VolcTTS:
    """一次合成 = 一个 WebSocket：发一帧完整请求，流式收 PCM 音频块。"""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_tts_config()

    def _body(self, text: str) -> bytes:
        rp = {
            'speaker': self.cfg['speaker'],
            'text': text,
            'audio_params': {
                'format': 'pcm',
                'sample_rate': _SAMPLE_RATE,
                'speech_rate': int(self.cfg.get('speed') or 0),
            },
            # 去掉 Markdown / Emoji 符号（她的话里偶尔带 ** 或表情）
            'additions': json.dumps({
                'disable_markdown_filter': True,
                'disable_emoji_filter': True,
            }, ensure_ascii=False),
        }
        # 实测 seed-icl-2.0（复刻音色）由请求头 X-Api-Resource-Id 选模型，
        # body 里再带 model（任何值）都会报 InvalidModel，故不带。
        return json.dumps({'req_params': rp}, ensure_ascii=False).encode('utf-8')

    def _headers(self) -> dict:
        return {
            'X-Api-Key': self.cfg['api_key'],
            'X-Api-Resource-Id': self.cfg['resource_id'],
            'X-Api-Request-Id': str(uuid.uuid4()),
            'X-Control-Require-Usage-Tokens-Return': '*',
        }

    async def synth(self, text: str, on_audio) -> dict:
        """合成 text，每收到一块 PCM 调 await on_audio(bytes)。返回 {bytes,words}。"""
        import websockets
        headers = self._headers()
        try:
            ws = await websockets.connect(
                _WSS, additional_headers=headers, max_size=10 * 1024 * 1024,
                open_timeout=_TTS_TIMEOUT, close_timeout=5)
        except TypeError:
            ws = await websockets.connect(
                _WSS, extra_headers=headers, max_size=10 * 1024 * 1024,
                open_timeout=_TTS_TIMEOUT, close_timeout=5)
        total = 0
        words = 0
        try:
            await ws.send(build_frame(self._body(text)))
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=_TTS_TIMEOUT)
                except asyncio.TimeoutError:
                    raise RuntimeError('TTS 收音频超时')
                if isinstance(msg, str):
                    continue
                for mtype, body in parse_frames(msg):
                    if mtype == _MSG_AUDIO_ONLY and body:
                        total += len(body)
                        await on_audio(body)
                    elif mtype == _MSG_FULL_SERVER:
                        try:
                            d = json.loads(body.decode('utf-8'))
                        except Exception:
                            continue
                        # v3 事件靠 body 的键区分（无 EventType 字段）：
                        # usage=SessionFinished，words=字幕，text=句结束
                        if 'usage' in d:
                            words = int((d.get('usage') or {})
                                        .get('text_words') or 0)
                            return {'bytes': total, 'words': words}
                    elif mtype == _MSG_ERROR:
                        detail = body.decode('utf-8', errors='replace').strip()
                        if not detail:
                            i = msg.find(b'{')
                            detail = msg[i:].decode('utf-8', errors='replace') \
                                if i >= 0 else '未知错误'
                        raise RuntimeError(f'TTS 服务端错误：{detail}')
        finally:
            try:
                await ws.close()
            except Exception:
                pass
        if total <= 0:
            raise RuntimeError('TTS 未返回任何音频')
        return {'bytes': total, 'words': words}


def decode_audio(source: str, timeout: float = 60.0) -> bytes:
    """任意音频 → 24k 单声道 s16le PCM（阻塞，调用方自己 to_thread）。

    source：http(s) 链接交给 ffmpeg 自己拉；base64:// 解码后喂管道；
    其余按本地路径。QQ 录音 silk 系格式 ffmpeg 解不了时返回 b''。
    """
    raw = None
    if source.startswith('base64://'):
        raw = base64.b64decode(source[len('base64://'):])
    if raw is not None:
        # 管道输入没有扩展名，按魔数给容器提示（猜不出就交给 ffmpeg 自检）
        fmt = _sniff_audio_fmt(raw)
        args = ['ffmpeg', '-hide_banner', '-loglevel', 'error']
        if fmt:
            args += ['-f', fmt]
        args += ['-i', 'pipe:0', '-f', 's16le', '-ac', '1',
                 '-ar', str(_SAMPLE_RATE), 'pipe:1']
        proc = subprocess.run(args, input=raw, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)
    else:
        args = ['ffmpeg', '-hide_banner', '-loglevel', 'error',
                '-i', source, '-f', 's16le', '-ac', '1',
                '-ar', str(_SAMPLE_RATE), 'pipe:1']
        proc = subprocess.run(args, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(
            f'ffmpeg 解码失败：{proc.stderr.decode("utf-8", "replace")[-200:]}')
    return proc.stdout


def _sniff_audio_fmt(raw: bytes) -> str:
    if raw[:4] == b'fLaC':
        return 'flac'
    if raw[:3] == b'ID3' or raw[:2] == b'\xff\xfb' or raw[:2] == b'\xff\xf3':
        return 'mp3'
    if raw[:4] == b'OggS':
        return 'ogg'
    if raw[:8] == b'ftypM4A' or raw[4:8] == b'ftyp':
        return 'mp4'
    if raw[:4] == b'RIFF':
        return 'wav'
    if raw[:6] in (b'#!AMR\n', b'#!AMR-W'):
        return 'amr'
    return ''


class Speaker:
    """两路音频的串行调度（共用同一块声卡，绝不重叠出声）：

    - 文本队列 _jobs：她的台词（终态回复与 qq.text 同路），逐句流式 TTS，
      第一块 PCM 到达即开口；
    - 素材队列 _media_jobs：qq.voice 触发的成品音频（ffmpeg 解码）。
      素材开始播放的瞬间，文本队列里积压的台词整体改走弹幕（divert 回调），
      播放窗口内新到的台词同样转弹幕，避免和素材抢声轨。

    播放用常驻 RawOutputStream（PCM s16le 24k 单声道），PortAudio 回调在
    音频线程消费，欠载补零。播放进度用 fed/played 字节计数精确等待。
    """

    _MAX_JOBS = 20
    _MEDIA_SENTINEL = object()

    def __init__(self, log, cfg: dict | None = None,
                 device=None, enabled: bool = True):
        self.log = log
        self.cfg = cfg
        self.device = device
        self.enabled = enabled
        self._q: queue.Queue = queue.Queue()   # PCM 块，音频线程消费
        self._jobs: asyncio.Queue | None = None  # 待朗读台词，worker 串行消费
        self._media_jobs: asyncio.Queue | None = None  # 待播放素材（voice）
        self._divert = None                    # async fn(text)：台词改走弹幕
        self._voice_active = False             # 素材播放窗口（仅 worker 协程读写）
        self._fed_bytes = 0                    # 已喂给音频队列的字节
        self._played_bytes = 0                 # 回调已实际播放的字节
        self._pcm_tail = b''                   # 音频线程回调的零头，仅音频线程读写
        self._stream = None
        self._tts: VolcTTS | None = None
        self._bg: asyncio.Task | None = None
        self._dropped = 0

    def set_divert(self, divert):
        """注册 async fn(text)：voice 播放窗口内的台词由它改成发弹幕。"""
        self._divert = divert

    def _ensure_stream(self):
        if self._stream is not None:
            return
        import sounddevice as sd
        self._stream = sd.RawOutputStream(
            samplerate=_SAMPLE_RATE, blocksize=_FRAME_BYTES // 2,
            dtype='int16', channels=1, device=self.device,
            callback=self._callback)
        self._stream.start()

    def _callback(self, outdata, frames, time_info, status):  # 输出流只有 4 个参数
        # underflow 等状态不打日志，直播中太吵
        need = frames * 2
        buf = bytearray(self._pcm_tail)
        self._pcm_tail = b''
        while len(buf) < need:
            try:
                buf.extend(self._q.get_nowait())
            except queue.Empty:
                break
        content = len(buf)                    # 真实内容字节（欠载补零不算）
        if buf and len(buf) >= need:
            self._pcm_tail = bytes(buf[need:])
        if content < need:
            buf.extend(b'\x00' * (need - content))
        outdata[:] = bytes(buf[:need])
        # 只计实际输出的内容，欠载静音不推进进度（否则素材播放窗口会提前结束）
        self._played_bytes += min(need, content)

    async def start(self):
        if not self.enabled:
            return
        cfg = self.cfg or load_tts_config()
        if not cfg.get('api_key') or not cfg.get('speaker'):
            self.log('⚠ TTS 未配置（config.json 缺 tts_apikey/tts_speaker），本场只发弹幕不说话')
            self.enabled = False
            return
        try:
            await asyncio.to_thread(self._ensure_stream)
        except Exception as e:
            self.log(f'⚠ 音频输出设备打开失败，本场禁用语音：{e}', 'warn')
            self.enabled = False
            return
        self._tts = VolcTTS(cfg)
        self._jobs = asyncio.Queue(maxsize=self._MAX_JOBS)
        self._media_jobs = asyncio.Queue(maxsize=self._MAX_JOBS)
        self._bg = asyncio.ensure_future(self._runner())
        self.log(f'🔊 TTS 就绪（{cfg["resource_id"]} / 音色 {cfg["speaker"]}，'
                 f'24k PCM 流式播放）')

    async def _runner(self):
        """单 worker 串行调度两条队列：素材优先（要开口的音效不能等），
        其余台词按 TTS 念。语音素材播放窗口内的台词全部转弹幕。"""
        while True:
            kind, item = await self._next_job()
            if item is None and kind == 'text':
                # 收尾哨兵：把残余素材也播完再退
                while not self._media_jobs.empty():
                    src = self._media_jobs.get_nowait()
                    self._media_jobs.task_done()
                    await self._play_media(src)
                break
            if kind == 'media':
                self._media_jobs.task_done()
                if item is self._MEDIA_SENTINEL:
                    break
                try:
                    await self._play_media(item)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                continue
            # text：入队时打了 divert 标的（素材播放窗口内到达）直接走弹幕
            self._jobs.task_done()
            force_divert = isinstance(item, tuple) and item[0] == 'divert'
            text = item[1] if force_divert else item
            try:
                if force_divert:
                    if self._divert is not None:
                        await self._divert(text)
                else:
                    await self.say(text)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    async def _next_job(self):
        """素材队列非空优先；否则等台词（200ms 粒度回看素材队列）。"""
        while True:
            if self._media_jobs is not None and not self._media_jobs.empty():
                return 'media', self._media_jobs.get_nowait()
            try:
                item = await asyncio.wait_for(self._jobs.get(), timeout=0.2)
                return 'text', item
            except asyncio.TimeoutError:
                continue

    def speak_soon(self, text: str):
        """台词入 TTS 队列；素材解码/播放窗口内到达的打 divert 标转弹幕。
        队列满丢最旧防延迟堆积。"""
        if not self.enabled or self._jobs is None or not (text or '').strip():
            return
        item = ('divert', text) if self._voice_active else text
        try:
            self._jobs.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self._jobs.get_nowait()
                self._jobs.task_done()
            except asyncio.QueueEmpty:
                pass
            try:
                self._jobs.put_nowait(item)
            except asyncio.QueueFull:
                pass

    def play_soon(self, source: str):
        """qq.voice 素材入独立播放队列（满了丢最旧素材）。"""
        if not self.enabled or self._media_jobs is None or not source:
            return
        try:
            self._media_jobs.put_nowait(source)
        except asyncio.QueueFull:
            try:
                self._media_jobs.get_nowait()
                self._media_jobs.task_done()
            except asyncio.QueueEmpty:
                pass
            try:
                self._media_jobs.put_nowait(source)
            except asyncio.QueueFull:
                pass

    async def _play_media(self, source: str):
        """解码素材 → 播放 → 等整块播完；窗口（含解码耗时）内台词转弹幕。"""
        # 窗口必须先于解码置位：URL 下载可能耗时数秒，期间新台词要打 divert 标
        self._voice_active = True
        try:
            # 已排队的台词在开口前冲刷成弹幕（它们本会和素材同时响）
            await self._flush_texts_to_divert()
            try:
                pcm = await asyncio.to_thread(decode_audio, source)
            except Exception as e:
                self.log(f'🎵 语音素材解码失败（跳过）：{e}', 'warn')
                return
            if not pcm:
                return
            secs = len(pcm) / (_SAMPLE_RATE * 2)
            self.log(f'🎵 播放语音素材（{secs:.1f}s），期间她的话改走弹幕')
            # 分块喂，保持和 TTS 一样的有界/欠载行为
            step = _FRAME_BYTES * 4
            for i in range(0, len(pcm), step):
                await self._feed(pcm[i:i + step])
            # 水位线必须取喂完后的 _fed_bytes：队列里可能还有前段 TTS 残音，
            # 用「当前 played + 素材长度」会被残音提前满足，导致窗口提前关闭。
            # FIFO 下 played 追到这个水位才说明素材自己的字节真播完了。
            target = self._fed_bytes
            deadline = time.monotonic() + max(5.0, secs * 2 + 5)
            while self._played_bytes < target and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
                # 播放期间持续冲刷：台词一到就发弹幕，不等素材播完
                await self._flush_texts_to_divert(quiet=True)
        finally:
            # 关窗之前再冲一次，把最后这批转成弹幕
            await self._flush_texts_to_divert(quiet=True)
            self._voice_active = False

    async def _flush_texts_to_divert(self, quiet: bool = False):
        if self._divert is None or self._jobs is None:
            return
        n = 0
        while not self._jobs.empty():
            item = self._jobs.get_nowait()
            self._jobs.task_done()
            # 窗口内入队的是 ('divert', text) 元组，窗口前的是裸字符串
            text = item[1] if isinstance(item, tuple) else item
            n += 1
            try:
                await self._divert(text)
            except Exception:
                pass
        if n and not quiet:
            self.log(f'📝 语音素材开播，{n} 段排队中的台词改走弹幕')

    async def _enqueue_silence(self, ms: int):
        await self._feed(b'\x00' * (_SAMPLE_RATE * 2 * ms // 1000))

    async def say(self, text: str):
        """直接朗读一段（调用方自己排队/加锁）。"""
        if not self.enabled or not (text or '').strip():
            return
        sentences = split_sentences(text)
        for sent in sentences:
            # 注意：单 worker 串行，say() 执行期间不可能有素材在播
            # （素材优先只会在两句/两段的作业之间插队，入队时已打标）
            try:
                info = await self._tts.synth(sent, self._feed)
                # 句间 180ms 停顿，更像真人说话
                await self._enqueue_silence(180)
                secs = info['bytes'] / (_SAMPLE_RATE * 2)
                self.log(f'🔊 朗读 {info["words"] or len(sent)} 字 '
                         f'（约 {secs:.1f}s 音频）')
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(f'TTS 合成失败（跳过这句）：{e}', 'warn')

    async def _feed(self, pcm: bytes):
        # 有界保护：积压超过 60s 丢最旧的一块 PCM，防止越堵越延迟；
        # 同步推进 played 计数，避免等待方永久阻塞
        self._fed_bytes += len(pcm)
        if self._fed_bytes - self._played_bytes > _MAX_PENDING_BYTES:
            try:
                old = self._q.get_nowait()
                self._played_bytes += len(old)
                self._dropped += 1
            except queue.Empty:
                pass
        await asyncio.to_thread(self._q.put, pcm)

    async def aclose(self, wait: float = 20.0):
        """等已入队的台词/素材处理完（有上限），再关音频流。"""
        if self._jobs is not None and self._bg is not None:
            if not self._bg.done():
                await self._jobs.put(None)   # 哨兵：排空（含残余素材）后退出
                try:
                    await asyncio.wait_for(self._bg, timeout=wait)
                except asyncio.TimeoutError:
                    self._bg.cancel()
            # 等队列里剩余 PCM 真正播完
            left = (self._fed_bytes - self._played_bytes) / (_SAMPLE_RATE * 2)
            await asyncio.sleep(min(max(left + 0.3, 0.3), wait))
        if self._stream is not None:
            try:
                await asyncio.to_thread(self._stream.stop)
                await asyncio.to_thread(self._stream.close)
            except Exception:
                pass
            self._stream = None
