"""出站图片压缩：把工作区里的大图压到 QQ/NapCat 可发送的尺寸。

MCP 截图为了坐标映射必须保留未采样原图（经常数 MiB 的 PNG），直接 base64
内联发送既受工作区读取上限限制，也可能撞 QQ 消息大小；所以发送/送视觉前
在内存里重编码，绝不改原文件。纯同步重活，调用方用 asyncio.to_thread 包裹。
"""
from __future__ import annotations

import io

# 与 builtin_tools._IMG_SNIFF 保持同一组魔数；本模块独立可读，自带一份
_SNIFF = (
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
    (b'BM', 'image/bmp'),
)


class MediaOutError(Exception):
    pass


def sniff_image(raw: bytes) -> str:
    for magic, mime in _SNIFF:
        if raw.startswith(magic):
            return mime
    if raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        return 'image/webp'
    return ''


def _flatten_rgb(img):
    """透明通道压到白底上（直接 convert('RGB') 会把透明区填黑）。"""
    if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
        from PIL import Image
        rgba = img.convert('RGBA')
        bg = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
        bg.alpha_composite(rgba)
        return bg.convert('RGB')
    return img.convert('RGB')


def _jpeg_cascade(img, max_bytes: int) -> bytes | None:
    for quality in (88, 78, 68, 58, 48, 38, 28):
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=quality, optimize=True)
        if buf.tell() <= max_bytes:
            return buf.getvalue()
    return None


def compress_for_send(raw: bytes, suffix: str = '',
                      max_kb: int = 3072) -> tuple[bytes, str, bool]:
    """返回 (可能重编码后的字节, mime, 是否发生了压缩)。

    - 未超 max_kb：原样返回（PNG 截图清晰度/透明通道都保留）
    - GIF 动图：不压（压了丢动画），超限直接报错让调用方换格式
    - 其余（png/jpg/webp/bmp）：Pillow 开图 → 白底 RGB → JPEG 质量级联，
      仍超限则 0.85 倍逐级降采样直到短边 480px
    """
    max_bytes = max(1, int(max_kb)) * 1024
    if len(raw) <= max_bytes:
        return raw, sniff_image(raw) or '', False
    mime = sniff_image(raw)
    if not mime and suffix:
        mime = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
                'gif': 'image/gif', 'webp': 'image/webp',
                'bmp': 'image/bmp'}.get(suffix.lower().lstrip('.'), '')
    if mime == 'image/gif':
        raise MediaOutError(
            f'GIF 动图 {len(raw)} 字节超过 {max_kb}KiB 发送上限，'
            '动图无法在保留动画的前提下压缩，请改发截图（PNG/JPG）')
    try:
        from PIL import Image
    except ImportError:
        raise MediaOutError(
            f'图片 {len(raw)} 字节超过 {max_kb}KiB，且本机未安装 Pillow，'
            '无法自动压缩后发送')
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:
        raise MediaOutError(f'图片无法解码，压缩发送失败: {e}')
    try:
        img = _flatten_rgb(img)
    except Exception:
        img = img.convert('RGB')

    out = _jpeg_cascade(img, max_bytes)
    if out is not None:
        return out, 'image/jpeg', True

    # 质量压到 28 仍超限：降采样（截图信息密度高时需要），再走一轮质量级联
    while min(img.size) > 480:
        img = img.resize((max(1, int(img.width * 0.85)),
                          max(1, int(img.height * 0.85))), Image.LANCZOS)
        out = _jpeg_cascade(img, max_bytes)
        if out is not None:
            return out, 'image/jpeg', True

    # 已缩到短边 480：取最低质量结果兜底（此时必然远小于常见上限）
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=20, optimize=True)
    return buf.getvalue(), 'image/jpeg', True
