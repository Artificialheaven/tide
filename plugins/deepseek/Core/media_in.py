"""入站媒体：触发通过后才下载群友发的图片/文件/语音（省流量、不白花钱）。

- 图片：下载 → 魔数校验 → data URI，作为视觉输入塞进最后一条 user 消息
- 文件：下载 → docparse 抽文本（txt/md/代码/pdf/docx），以文本块附在消息后
- 语音：默认不处理；voice_asr_on 开启后下载，必要时 ffmpeg 转 mp3，走
  OpenAI 兼容的 audio.transcriptions（ASR 服务可用性取决于 base_url）
- 视频：inbound_video_on + read_video_on 开启后下载，存进本群 mcp/ 工作区，
  在消息后附上路径，模型可把它传给 read_video 看画面
全部网络/解码有界：总下载量、图片张数、附件文本长度、视频大小都受 cfg 限制。
"""
from __future__ import annotations

import asyncio
import base64
import shutil
import subprocess
from dataclasses import dataclass, field

from . import docparse, docrender, message

_IMG_MIME = {
    b'\x89PNG\r\n\x1a\n': 'image/png',
    b'\xff\xd8\xff': 'image/jpeg',
    b'GIF87a': 'image/gif',
    b'GIF89a': 'image/gif',
}
_VISION_OK = {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}


@dataclass
class Attachments:
    images: list = field(default_factory=list)   # [(mime, base64_str)]
    extra_text: str = ''                         # 附件文本 / 语音转写


def sniff_image(raw: bytes) -> str:
    for magic, mime in _IMG_MIME.items():
        if raw.startswith(magic):
            return mime
    if raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        return 'image/webp'
    return ''


def _is_url(s: str) -> bool:
    return isinstance(s, str) and s.startswith(('http://', 'https://'))


async def _api_data(bot, action: str, params: dict) -> dict:
    """调 OneBot action，容错 {data:...} 信封与裸 dict 两种返回。"""
    if bot is None or not hasattr(bot, 'call_api'):
        return {}
    resp = await bot.call_api(action, params)
    if isinstance(resp, dict):
        d = resp.get('data')
        return d if isinstance(d, dict) else resp
    return {}


async def _http_get(url: str, budget_left: int) -> bytes:
    import httpx  # 插件 lib 里随 openai/mcp 已装
    async with httpx.AsyncClient(follow_redirects=True,
                                 timeout=15.0) as client:
        async with client.stream('GET', url) as r:
            r.raise_for_status()
            chunks = []
            used = 0
            async for chunk in r.aiter_bytes(65536):
                used += len(chunk)
                if used > budget_left:
                    raise ValueError('媒体超过下载总预算')
                chunks.append(chunk)
            return b''.join(chunks)


async def _resolve_url(bot, ref: dict, kind: str, group_id: str,
                       chat_type: str = 'group') -> str:
    """群附件走 get_group_file_url；私聊没有该接口，走通用 get_file。"""
    url = ref.get('url') or ''
    if _is_url(url):
        return url
    file_ = ref.get('file') or ''
    if kind == 'image' and file_:
        d = await _api_data(bot, 'get_image', {'file': file_})
        return d.get('url') or (d.get('file') if _is_url(d.get('file', '')) else '')
    if kind == 'record' and file_:
        d = await _api_data(bot, 'get_record',
                            {'file': file_, 'out_format': 'mp3'})
        return d.get('url') or (d.get('file') if _is_url(d.get('file', '')) else '')
    if kind == 'video':
        # 视频段一般只带 file（或 url），走通用 get_file；有的实现要 file_id
        probes = []
        fid = ref.get('file_id') or ''
        if fid:
            probes.append({'file_id': fid})
        if file_ and file_ != fid:
            probes.append({'file': file_})
        for params in probes:
            try:
                d = await _api_data(bot, 'get_file', params)
            except Exception:
                continue
            if _is_url(d.get('url', '')):
                return d['url']
            if _is_url(d.get('file', '')):
                return d['file']
            # 部分实现把地址塞在 data.file 的 path/url 里
            for key in ('path', 'file_path'):
                if _is_url(d.get(key, '')):
                    return d[key]
        return ''
    if kind == 'file':
        fid = ref.get('file_id') or ''
        if fid and chat_type != 'private':
            try:
                d = await _api_data(bot, 'get_group_file_url', {
                    'group_id': int(group_id) if str(group_id).isdigit() else group_id,
                    'file_id': fid, 'busid': int(ref.get('busid') or 0)})
                if _is_url(d.get('url', '')):
                    return d['url']
            except Exception:
                pass
        if fid:
            try:
                d = await _api_data(bot, 'get_file', {'file_id': fid})
                if _is_url(d.get('url', '')):
                    return d['url']
                if _is_url(d.get('file', '')):
                    return d['file']
            except Exception:
                pass
    return ''


def _ffmpeg_to_mp3(raw: bytes, ext: str) -> bytes:
    """丝量语音转 mp3（喂给 ASR）。本机没装 ffmpeg 时抛 FileNotFoundError。"""
    # silk/amr 直接让 ffmpeg 自己识别（部分构建支持 amr；silk 可能失败）
    p = subprocess.run(
        ['ffmpeg', '-loglevel', 'error', '-f', ext or 's16le',
         '-i', 'pipe:0', '-f', 'mp3', '-b:a', '64k', 'pipe:1'],
        input=raw, capture_output=True)
    if p.returncode != 0 or not p.stdout:
        raise ValueError('ffmpeg 转码失败（QQ silk 语音可能需要专用解码器）')
    return p.stdout


def process_file(fname: str, raw: bytes, cfg: dict, slots: int,
                 log=None) -> tuple:
    """下载完成后的文件统一处理（同步重活，调用方放 to_thread）。

    先 docparse 抽文本；扫描版 PDF / 老 .doc 抽不出字时 docrender 渲染页面图。
    入站附件和 qq_download_file 工具共用这一条管线。
    返回 (给模型的文本, [('image/jpeg', b64), ...])。
    """
    log = log or (lambda msg, level='info': None)
    images = []
    text = docparse.extract_text(
        fname, raw, int(cfg.get('file_text_max_chars', 4000)))
    ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
    scanned_pdf = (ext == 'pdf'
                   and ('没有提取到文本' in text or '扫描' in text))
    rendered_pages = []
    if ((scanned_pdf or ext == 'doc') and cfg.get('vision_on', True)
            and cfg.get('doc_render_on', True)):
        if slots <= 0:
            log('文档可渲染但图片名额已满，只保留文字说明', 'warn')
        else:
            try:
                rendered_pages = docrender.render_document(
                    fname, raw,
                    max_pages=min(int(cfg.get('doc_render_max_pages', 3)),
                                  slots),
                    dpi=int(cfg.get('doc_render_dpi', 130)),
                    page_max_kb=int(cfg.get('read_image_max_kb', 2048)),
                    src_max_kb=int(cfg.get('media_download_max_kb', 10240)))
            except docrender.DocRenderError as e:
                log(f'文档渲染失败，回退文字说明：{e}', 'warn')
    if rendered_pages:
        for _, jpg in rendered_pages[:slots]:
            images.append(
                ('image/jpeg', base64.b64encode(jpg).decode('ascii')))
        n = min(len(rendered_pages), slots)
        text = (f'[附件 {fname}（{len(raw)} 字节）：文本提取为空，'
                f'已把前 {n} 页渲染成图片附在下方，直接看图阅读]')
        log(f'文档已渲染 {n} 页送视觉：{fname}', 'ok')
    elif text:
        log(f'文件已解析为文本：{fname}（{len(raw)} 字节）', 'ok')
    return text, images


async def fetch_attachments(bot, parsed, cfg: dict, group_id: str,
                            llm=None, log=None, chat_type: str = 'group',
                            sandbox=None) -> Attachments:
    """下载并解析本条消息的全部媒体；单项失败只加说明/跳过，不影响对话。

    sandbox 只用于入站视频落盘（写 mcp/ 下，交给 read_video）；不传时跳过视频。
    """
    out = Attachments()
    budget = int(cfg.get('media_download_max_kb', 10240)) * 1024
    video_budget = int(cfg.get('inbound_video_max_kb', 51200)) * 1024
    used = video_used = 0
    log = log or (lambda msg, level='info': None)
    for ref in getattr(parsed, 'media', None) or []:
        kind = ref.get('kind')
        # 功能关闭时连地址都不解析、不下载（语音默认关，绝不白拿文件）
        if kind == 'image':
            if not cfg.get('vision_on', True):
                continue
            if len(out.images) >= int(cfg.get('vision_max_images', 4)):
                log('入站图片超过张数上限，后续图片跳过', 'warn')
                continue
        elif kind == 'file' and not cfg.get('file_parse_on', True):
            continue
        elif kind == 'record' and not cfg.get('voice_asr_on', False):
            continue
        elif kind == 'video':
            if not cfg.get('inbound_video_on', True):
                continue
            if not cfg.get('read_video_on', True):
                continue
            if sandbox is None:
                log('入站视频需要工作区才能交给 read_video，跳过', 'warn')
                continue
        try:
            url = await _resolve_url(bot, ref, kind, str(group_id), chat_type)
            if not _is_url(url):
                log(f'入站 {kind} 拿不到下载地址，跳过（段数据：'
                    + str({k: v for k, v in ref.items() if k != 'kind'})[:120]
                    + '）', 'warn')
                continue
            if kind == 'video':
                left = video_budget - video_used
                if left <= 0:
                    log(f'入站视频超过下载上限（{video_budget // 1024} KiB），'
                        '跳过', 'warn')
                    break
            else:
                left = budget - used
                if left <= 0:
                    log(f'入站 {kind} 处理失败：媒体下载总预算已用尽'
                        f'（上限 {budget // 1024} KiB），后续附件跳过', 'warn')
                    break
            raw = await _http_get(url, left)
            if kind == 'video':
                video_used += len(raw)
            else:
                used += len(raw)

            if kind == 'image':
                mime = sniff_image(raw)
                if mime not in _VISION_OK:
                    log(f'入站图片类型不受视觉支持（{mime or "未知"}），跳过', 'warn')
                    continue
                out.images.append((mime, base64.b64encode(raw).decode('ascii')))
                log(f'入站图片 {len(raw)} 字节（{mime}）已送视觉输入', 'ok')

            elif kind == 'file':
                fname = ref.get('name') or ref.get('file') or '附件'
                slots = int(cfg.get('vision_max_images', 4)) - len(out.images)
                text, imgs = await asyncio.to_thread(
                    process_file, fname, raw, cfg, slots, log)
                out.images.extend(imgs)
                if text:
                    out.extra_text = (out.extra_text + '\n' + text).strip()

            elif kind == 'video':
                rel = message.inbound_video_path(ref)
                if not rel:
                    log('入站视频缺少可用的下载引用，跳过', 'warn')
                    continue
                await asyncio.to_thread(
                    sandbox.write_mcp_media, rel, raw, video_budget)
                out.extra_text = (
                    out.extra_text
                    + f'\n[视频已保存到工作区：{rel}（{len(raw)} 字节）。'
                      f'要看画面请调用 read_video，path 填 {rel}]').strip()
                log(f'入站视频已存工作区：{rel}（{len(raw)} 字节）', 'ok')

            elif kind == 'record':
                text = await _transcribe(llm, cfg, raw, ref, log)
                if text:
                    tag = ref.get('name') or '语音'
                    out.extra_text = (
                        out.extra_text + f'\n[语音转写（{tag}）]\n{text}').strip()
                    log(f'入站语音已转写：{text[:40]}', 'ok')
        except Exception as e:
            log(f'入站 {kind} 处理失败: {e}', 'warn')
    return out


async def _transcribe(llm, cfg: dict, raw: bytes, ref: dict, log) -> str:
    """下载后转写：非标格式先用 ffmpeg 转 mp3，再走 OpenAI 兼容 ASR 接口。"""
    model = (cfg.get('voice_asr_model') or '').strip()
    if not model:
        log('语音转写已开但未配置 voice_asr_model，跳过', 'warn')
        return ''
    if llm is None or not hasattr(llm, 'transcribe'):
        log('当前模型客户端不支持语音转写，跳过', 'warn')
        return ''
    fname = (ref.get('file') or 'voice.mp3').split('/')[-1] or 'voice.mp3'
    ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
    audio = raw
    if ext not in ('mp3', 'wav', 'm4a', 'webm', 'ogg', 'flac'):
        if not shutil.which('ffmpeg'):
            log('语音不是可直传格式且本机无 ffmpeg，无法转写', 'warn')
            return ''
        audio = await asyncio.to_thread(_ffmpeg_to_mp3, raw, ext)
        fname = 'voice.mp3'
    return await llm.transcribe(cfg, fname, audio, model)
