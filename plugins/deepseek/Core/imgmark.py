# -*- coding: utf-8 -*-
"""截图视觉辅助与坐标映射（纯逻辑不依赖 Pillow；渲染部分懒加载）。

为什么需要这层：
- 模型在截图上估出的坐标是「图像内像素」，MCP 的 Click 吃的是「Windows 虚拟
  桌面坐标」，两者在多屏 / 窗口偏移 / 降采样时并不重合。
- MCP 截图落盘时写一个同名 sidecar（.meta/<图名>.json）记录图像尺寸、monitor、
  max_width、原点假设；click_at 据此做确定性换算，拒绝一切无法保证 1:1 的情况。
- 网格叠加 + 裁剪放大让模型把「图像内像素」估准：网格标签始终标注**原图坐标**，
  裁剪放大后也一样，模型直接把标签数字喂给 click_at，无需自己加偏移。
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass

META_VERSION = 1


class ImgMarkError(Exception):
    pass


# ── sidecar 纯逻辑 ────────────────────────────────────────────

def sidecar_rel(image_rel: str) -> str:
    """截图 rel（mcp/xx.jpg）→ sidecar rel（mcp/.meta/xx.jpg.json）。"""
    name = image_rel.replace('\\', '/').rstrip('/').split('/')[-1]
    return f'mcp/.meta/{name}.json'


def is_snapshot_tool(tool_name: str) -> bool:
    n = (tool_name or '').lower()
    return 'snapshot' in n or 'screenshot' in n or 'screencapture' in n


def build_meta(*, image_w: int, image_h: int, tool_name: str,
               args: dict | None) -> dict | None:
    """根据截图工具名/参数构造 sidecar；无法保证坐标映射时返回 None。

    约定（已写进系统提示词，模型必须遵守）：
    - Snapshot 只允许 monitor=1（主屏，原点 0,0）、max_width=0（不降采样）；
    - monitor=0（含没有 monitor 参数的 AnnotatedSnapshot）在单屏机器上原点
      也是 0,0，标 origin_assumed=True，click_at 照常工作但会在返回里警告；
    - max_width 把图缩放过（0 < max_width <= 实际图宽）时映射不可靠，不写
      sidecar，click_at 会要求模型用 max_width=0 重截。
    DPI 缩放无法从客户端探测，服务端按物理像素截图时映射才精确。
    """
    if not image_w or not image_h or not is_snapshot_tool(tool_name):
        return None
    args = args or {}
    monitor = 0
    try:
        monitor = int(args.get('monitor', 0) or 0)
    except (TypeError, ValueError):
        monitor = 0
    try:
        mw = int(args.get('max_width', 0) or 0)
    except (TypeError, ValueError):
        mw = 0
    if 0 < mw <= image_w:
        # 输出宽度被钉在 cap 上（或更窄）：原生宽度未知，缩放比不可知
        return None
    if monitor not in (0, 1):
        # 副屏原点可能为负，客户端无从得知
        return None
    return {
        'version': META_VERSION,
        'source_tool': tool_name,
        'image_w': int(image_w), 'image_h': int(image_h),
        'monitor': monitor, 'max_width': mw,
        'scale': 1.0,                 # 图像像素 → 桌面像素的乘数
        'origin_x': 0, 'origin_y': 0,
        'origin_assumed': monitor == 0,
    }


def map_point(meta: dict, x: int, y: int) -> tuple[int, int, str, str]:
    """截图内坐标 → (桌面 x, 桌面 y, 警告, 错误)。错误非空时前两项无意义。"""
    w, h = int(meta.get('image_w', 0)), int(meta.get('image_h', 0))
    if not w or not h:
        return 0, 0, '', '截图元数据缺少尺寸信息'
    if not (0 <= x <= w and 0 <= y <= h):
        return 0, 0, '', f'坐标 ({x},{y}) 超出截图范围（{w}x{h}）'
    scale = float(meta.get('scale', 1.0) or 1.0)
    if scale <= 0 or scale != scale:  # NaN 防御
        return 0, 0, '', '截图元数据缩放比异常'
    dx = int(round(meta.get('origin_x', 0) + x * scale))
    dy = int(round(meta.get('origin_y', 0) + y * scale))
    warn = '该截图是全屏拼接（monitor=0），已按单屏原点(0,0)换算；多屏环境可能点偏' \
        if meta.get('origin_assumed') else ''
    return dx, dy, warn, ''


def dump_meta(meta: dict) -> str:
    return json.dumps(meta, ensure_ascii=False)


def load_meta(text: str) -> dict:
    meta = json.loads(text)
    if int(meta.get('version', 0)) != META_VERSION:
        raise ImgMarkError(f'不支持的截图元数据版本: {meta.get("version")}')
    return meta


# ── 尺寸探测（不依赖 Pillow；WEBP 复杂格式返回 None）────────────

def image_size(raw: bytes) -> tuple[int, int] | None:
    if raw[:8] == b'\x89PNG\r\n\x1a\n' and len(raw) >= 24:
        return int.from_bytes(raw[16:20], 'big'), int.from_bytes(raw[20:24], 'big')
    if raw[:3] == b'\xff\xd8\xff':
        i = 2
        while i < len(raw) - 9:
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            seglen = int.from_bytes(raw[i + 2:i + 4], 'big')
            if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                h = int.from_bytes(raw[i + 5:i + 7], 'big')
                w = int.from_bytes(raw[i + 7:i + 9], 'big')
                return w, h
            i += 2 + seglen
    if raw[:6] in (b'GIF87a', b'GIF89a') and len(raw) >= 10:
        return int.from_bytes(raw[6:8], 'little'), int.from_bytes(raw[8:10], 'little')
    if raw[:2] == b'BM' and len(raw) >= 22:
        return (int.from_bytes(raw[18:22], 'little', signed=True),
                int.from_bytes(raw[22:26], 'little', signed=True))
    return None


# ── Pillow 渲染（懒依赖）──────────────────────────────────────

def pillow_available() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except Exception:
        return False


def _font(size: int):
    from PIL import ImageFont
    for name in ('consola.ttf', 'arial.ttf', 'DejaVuSans.ttf'):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


@dataclass
class View:
    raw: bytes
    mime: str
    crop_box: tuple | None = None   # (x0,y0,w,h) 原图坐标
    zoom: int = 1


def render_view(raw: bytes, *, step: int = 100, grid: bool = True,
                box: list | None = None, zoom: int = 1,
                max_kb: int = 2048) -> View:
    """裁剪 + 放大 + 坐标网格，返回 JPEG bytes。

    - box=[x,y,w,h] 为**原图**像素矩形，自动夹到图内；
    - 网格标签始终标注原图坐标（裁剪后也不需要模型自己加偏移）；
    - 输出经质量级联压到 max_kb 以内，仍超限抛 ImgMarkError。
    """
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        raise ImgMarkError(f'未安装 Pillow，无法裁剪/打网格: {e}')
    step = max(20, int(step or 100))
    zoom = min(4, max(1, int(zoom or 1)))
    im = Image.open(io.BytesIO(raw))
    im.load()
    W, H = im.size

    x0 = y0 = 0
    cw, ch = W, H
    if box:
        try:
            bx, by, bw, bh = (int(v) for v in box[:4])
        except (TypeError, ValueError):
            raise ImgMarkError('crop 必须是 [x,y,w,h] 四个整数')
        if bw <= 0 or bh <= 0:
            raise ImgMarkError('crop 的宽高必须为正数')
        x0, y0 = max(0, bx), max(0, by)
        x1, y1 = min(W, bx + bw), min(H, by + bh)
        if x1 <= x0 or y1 <= y0:
            raise ImgMarkError(f'crop 区域不在截图内（截图 {W}x{H}）')
        im = im.crop((x0, y0, x1, y1))
        cw, ch = x1 - x0, y1 - y0

    if zoom > 1:
        im = im.resize((cw * zoom, ch * zoom))  # 默认 BICUBIC，UI 文字放大清晰
    if im.mode != 'RGB':
        bg = Image.new('RGB', im.size, (255, 255, 255))
        rgba = im.convert('RGBA')
        bg.paste(rgba, mask=rgba.split()[-1] if rgba.mode == 'RGBA' else None)
        im = bg

    note_box = None
    if grid:
        draw = ImageDraw.Draw(im)
        fs = max(12, min(22, round(im.size[0] / 110)))
        font = _font(fs)
        line = (40, 40, 40)
        minor_line = (150, 150, 150)
        bold = (0, 0, 0)
        # 放大视图主刻度自动加密：放大后同样屏距对应更少原图像素，
        # 不加密模型会去够最近的百线（实测裁剪条件因此出现 60px 误差）
        major_step = step if zoom <= 1 else max(25, (step // 2 // 25) * 25 or 25)
        half = max(10, major_step // 2)

        def _grid_axis(vertical: bool):
            span = cw if vertical else ch
            origin = x0 if vertical else y0
            size_along = im.size[0] if vertical else im.size[1]
            size_cross = im.size[1] if vertical else im.size[0]
            # 浅色半距辅助线（不带数字，给格内插值参照）
            k = origin // half + 1
            while k * half < origin + span:
                p = (k * half - origin) * zoom
                if (k * half) % major_step:
                    if vertical:
                        draw.line([(p, 0), (p, size_cross)],
                                  fill=minor_line, width=1)
                    else:
                        draw.line([(0, p), (size_cross, p)],
                                  fill=minor_line, width=1)
                k += 1
            # 主刻度线 + 原图坐标数字
            k = origin // major_step + 1
            while k * major_step < origin + span:
                p = (k * major_step - origin) * zoom
                strong = (k * major_step) % (step * 5) == 0
                t = str(k * major_step)
                # 标签居中压在自己的刻度线上（左对齐时模型会把数字错配给
                # 相邻刻度，加密刻度后错一档就是 50px）
                tb = draw.textbbox((0, 0), t, font=font)
                tw, th = tb[2] - tb[0], tb[3] - tb[1]
                if vertical:
                    draw.line([(p, 0), (p, size_cross)],
                              fill=bold if strong else line,
                              width=2 if strong else 1)
                    draw.rectangle([p - tw // 2 - 4, 2, p + tw // 2 + 4,
                                    th + 8], fill=(255, 255, 255))
                    draw.text((p - tw // 2, 4), t, fill=(200, 0, 0),
                              font=font)
                else:
                    draw.line([(0, p), (size_along, p)],
                              fill=bold if strong else line,
                              width=2 if strong else 1)
                    draw.rectangle([2, p - th // 2 - 3, tw + 10,
                                    p + th // 2 + 3], fill=(255, 255, 255))
                    draw.text((5, p - th // 2 - 1), t,
                              fill=(200, 0, 0), font=font)
                k += 1

        _grid_axis(True)
        _grid_axis(False)
        note_box = (x0, y0, cw, ch)

    cap = max(1, int(max_kb)) * 1024
    for quality in (90, 82, 72, 60, 48):
        buf = io.BytesIO()
        im.save(buf, format='JPEG', quality=quality)
        out = buf.getvalue()
        if len(out) <= cap:
            break
    else:
        raise ImgMarkError(f'加网格/放大后仍超过 {max_kb}KiB 上限，缩小 crop 或 zoom')
    return View(raw=out, mime='image/jpeg',
                crop_box=(x0, y0, cw, ch) if box else None, zoom=zoom)
