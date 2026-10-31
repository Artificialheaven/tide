# -*- coding: utf-8 -*-
"""视频/GIF 分镜阅读（参考 dsh-video-storyboard 的思路，独立实现）。

主模型是纯文本的，看不了视频。这里把视频拆成一张可寻址的宫格图，再交给
一个**视觉子代理**阅读，最后只把文字总结返回主模型。

链路：
  1. ffprobe 读时长/分辨率/音轨；按显示方向处理手机竖拍旋转；
  2. 低分辨率逐帧解码，RGB 帧差找剪辑点（阈值按「约 1.5 秒一刀」的速率
     反推，而不是固定值或分数倍数）；
  3. 剪辑密度（有台词时再叠加字/秒）积分等分格位，边界吸附到真实剪辑点，
     位移超过理想格长 30% 就放弃吸附；
  4. 每格取 3 个候选帧，比 Laplacian 方差挑最清晰的那帧（模糊不改变
     直方图，所以 ffmpeg thumbnail 会挑到糊掉的文字画面）；
  5. 拼成带格号、时间码、剪辑点刻度和音频包络的宫格图；
  6. 视觉子代理读宫格图 + 逐格清单，回答主模型的问题。

解码/拼图都在调用方的 asyncio.to_thread 里执行；只有子代理请求是 async。
"""
from __future__ import annotations

import asyncio
import base64
import bisect
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .sandbox import SandboxError

try:  # Pillow 是插件依赖；缺失时给出明确错误
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

    _PIL_OK = True
except Exception:  # pragma: no cover - 环境缺 Pillow
    _PIL_OK = False

_CELL_W, _CELL_H = 320, 180
_COLS = 4
_GAP = 4
_PAD = 8
_STRIP_H = 30
_SCAN_W, _SCAN_H = 48, 27
_MAX_SCAN_FRAMES = 9000
_SUPPORTED_EXT = {
    '.mp4', '.m4v', '.mov', '.mkv', '.webm', '.avi', '.flv', '.wmv',
    '.mpg', '.mpeg', '.ts', '.3gp', '.gif',
}
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# 进程内小 LRU：键 = 路径/size/mtime/窗口/扫描参数。不 hash 文件内容。
_CACHE: 'OrderedDict[str, dict]' = OrderedDict()
_CACHE_MAX = 6
_CACHE_LOCK = threading.Lock()


class VideoError(Exception):
    """可直接展示给用户的中文错误。"""


# ── 外部进程 ────────────────────────────────────────────────────


def _bin(name: str) -> str:
    return shutil.which(name) or ''


def _require_ffmpeg() -> str:
    exe = _bin('ffmpeg')
    if not exe:
        raise VideoError('本机未安装 ffmpeg，无法拆分视频（read_video 需要 ffmpeg/ffprobe）')
    return exe


def _exec(cmd: list, timeout: float):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=max(1.0, timeout),
                           creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise VideoError('ffmpeg 处理超时（视频可能过长或编码异常）')
    except OSError as e:
        raise VideoError(f'无法启动 {cmd[0]}：{e}')
    return p.returncode, p.stdout, p.stderr


def _run(cmd: list, timeout: float) -> bytes:
    rc, out, err = _exec(cmd, timeout)
    if rc != 0:
        tail = (err or b'')[-500:].decode('utf-8', 'replace').strip()
        raise VideoError(f'ffmpeg 处理失败：{tail or "未知错误"}')
    return out


# ── 探测 ────────────────────────────────────────────────────────


def _rate(stream: dict) -> float:
    for key in ('avg_frame_rate', 'r_frame_rate'):
        s = str(stream.get(key) or '')
        if '/' in s:
            a, b = s.split('/', 1)
            try:
                x = float(a) / float(b)
            except (ValueError, ZeroDivisionError):
                continue
            if x > 0:
                return x
    return 0.0


def probe(path: str, timeout: float = 20.0) -> dict:
    ffprobe = _bin('ffprobe')
    if not ffprobe:
        raise VideoError('本机未安装 ffprobe，无法读取视频信息（需要 ffmpeg）')
    out = _run([ffprobe, '-v', 'error', '-print_format', 'json',
                '-show_format', '-show_streams', path], timeout)
    try:
        info = json.loads(out.decode('utf-8', 'replace'))
    except (ValueError, UnicodeDecodeError):
        raise VideoError('ffprobe 输出无法解析，文件可能已损坏')
    streams = info.get('streams') or []
    v = next((s for s in streams if s.get('codec_type') == 'video'), None)
    if v is None:
        raise VideoError('文件里没有视频画面')
    has_audio = any(s.get('codec_type') == 'audio' for s in streams)

    rotation = 0
    for sd in v.get('side_data_list') or []:
        try:
            rotation = int(float(sd.get('rotation', 0)))
            break
        except (TypeError, ValueError):
            continue
    if not rotation:
        try:
            rotation = int(float((v.get('tags') or {}).get('rotate', 0)))
        except (TypeError, ValueError):
            rotation = 0

    width, height = int(v.get('width') or 0), int(v.get('height') or 0)
    if abs(rotation) % 180 == 90:
        width, height = height, width

    def _f(x) -> float:
        try:
            return float(x or 0)
        except (TypeError, ValueError):
            return 0.0

    duration = _f((info.get('format') or {}).get('duration')) or _f(
        v.get('duration'))
    fps = _rate(v)
    if duration <= 0 and fps:
        duration = _f(v.get('nb_frames')) / fps
    if duration <= 0:
        raise VideoError('读不出视频时长（可能是流式文件或损坏），请换成本地完整文件')
    return {'duration': duration, 'width': width, 'height': height,
            'fps': fps, 'rotation': rotation, 'has_audio': has_audio,
            'codec': str(v.get('codec_name') or '')}


# ── 场景扫描与剪辑点 ────────────────────────────────────────────


def _decode_lowres(ff: str, path: str, start: float, end: float,
                   fps: float, deadline: float) -> bytes:
    """解码成 48×27 rgb24。"""
    dur = max(0.1, end - start)
    cmd = [ff, '-v', 'error', '-nostdin',
           '-ss', f'{start:.3f}', '-t', f'{dur:.3f}', '-i', path,
           '-vf', f'fps={fps:.6f},scale={_SCAN_W}:{_SCAN_H}:flags=area,'
                  'format=rgb24',
           '-an', '-sn', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
    return _run(cmd, max(5.0, deadline - time.monotonic()))


def _frame_diffs(raw: bytes, w: int, h: int, deadline: float) -> list:
    """相邻帧 RGB 平均绝对差（0–255）。

    用 RGB 三通道而不是亮度：同亮度换色（红→蓝）在亮度域几乎为零。
    """
    frame_len = w * h * 3
    n_pix = w * h
    scores = []
    prev_img = None
    prev_raw = None
    for off in range(0, len(raw) - frame_len + 1, frame_len):
        if time.monotonic() > deadline:
            raise VideoError('视频扫描超时，请缩小 from/to 范围或降低扫描帧率')
        frame = raw[off:off + frame_len]
        if _PIL_OK:
            im = Image.frombuffer('RGB', (w, h), frame, 'raw', 'RGB', 0, 1)
            if prev_img is not None:
                st = ImageStat.Stat(ImageChops.difference(prev_img, im))
                scores.append(sum(st.sum) / (n_pix * 3))
            prev_img = im
        else:
            if prev_raw is not None:
                total = 0
                for a, b in zip(prev_raw, frame):
                    total += abs(a - b)
                scores.append(total / (n_pix * 3))
            prev_raw = frame
    return scores


def _detect_cuts(scores: list, fps: float, dur: float,
                 min_gap: float = 0.45) -> list:
    """速率法阈值 + 局部峰值抑制 + 近邻合并，返回相对窗口的切点秒数。"""
    n = len(scores)
    if n < 2:
        return []
    # 目标切点数 ≈ 1 刀 / 1.5 秒（短片收紧），并最多占帧数 25%
    gap = 0.5 if dur < 8 else 0.8 if dur < 30 else 1.5
    target = min(int(n * 0.25), max(1, int(dur / gap)))
    ranked = sorted(scores, reverse=True)
    threshold = max(12.0, ranked[min(target, n) - 1])

    cuts = []
    for i, s in enumerate(scores):
        if s < threshold:
            continue
        if i > 0 and scores[i - 1] > s:
            continue
        if i + 1 < n and scores[i + 1] >= s:
            continue
        cuts.append(((i + 1) / fps, s))
    merged = []
    for t, s in cuts:
        if merged and t - merged[-1][0] < min_gap:
            if s > merged[-1][1]:
                merged[-1] = (t, s)
        else:
            merged.append((t, s))
    return [t for t, _ in merged]


def _segments(cuts: list, start: float, end: float) -> list:
    bounds = [start] + [c for c in cuts if start < c < end] + [end]
    segs = [(a, b) for a, b in zip(bounds, bounds[1:]) if b - a > 0.01]
    if not segs:
        return [(start, end)]
    # 最小间隔不超过素材自身粒度：六个 1 秒镜头不能并成三个
    while len(segs) > 1:
        lens = sorted(b - a for a, b in segs)
        med = lens[len(lens) // 2]
        min_gap = max(0.35, min(1.2, med * 0.9))
        idx = next((i for i, (a, b) in enumerate(segs) if b - a < min_gap),
                   None)
        if idx is None:
            break
        if idx == 0:
            segs[0] = (segs[0][0], segs[1][1])
            del segs[1]
        else:
            segs[idx - 1] = (segs[idx - 1][0], segs[idx][1])
            del segs[idx]
    return segs


# ── 密度与格位 ──────────────────────────────────────────────────


def _pct(values: list, p: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    i = min(len(xs) - 1, max(0, int(round((len(xs) - 1) * p / 100.0))))
    return xs[i]


def auto_cells(window: float, segs: list, scores: list) -> int:
    """格数由画面运动 p90 与片段数决定，不由时长单独决定。"""
    base = 4 if window < 8 else 6 if window < 20 else 9 if window < 60 \
        else 12 if window < 180 else 16
    p90 = _pct(scores, 90)
    if p90 < 6.0:
        # 静态单镜头：再多格也是同一张图
        base = min(base, 4 if window >= 30 else 6)
    elif p90 > 30.0:
        base = min(16, base + 2)
    if segs:
        base = min(base, max(4, len(segs)))
    return max(4, min(16, int(base)))


def _density(cuts: list, start: float, end: float,
             speech: list | None = None) -> tuple[list, float]:
    """0.25s 粒度密度曲线：1 + 局部剪辑率 + 台词字/秒。"""
    binw = 0.25
    n = max(1, int(math.ceil((end - start) / binw)))
    dens = [1.0] * n
    cut_bins = [0] * n
    for c in cuts:
        j = int((c - start) / binw)
        if 0 <= j < n:
            cut_bins[j] += 1
    global_rate = len(cuts) / max(0.1, end - start)
    if global_rate > 0:
        half = max(1, int(2.0 / binw))
        pref = [0]
        for x in cut_bins:
            pref.append(pref[-1] + x)
        for i in range(n):
            lo, hi = max(0, i - half), min(n, i + half + 1)
            local = (pref[hi] - pref[lo]) / (2.0 * 2.0)
            dens[i] += 1.6 * min(local / global_rate, 4.0)
    if speech:
        char_bins = [0.0] * n
        for a, b, txt in speech:
            if not txt or b <= a:
                continue
            a2, b2 = max(a, start), min(b, end)
            if b2 <= a2:
                continue
            rate = len(txt) / (b - a)
            j0, j1 = int((a2 - start) / binw), int((b2 - start) / binw)
            for j in range(max(0, j0), min(n, j1 + 1)):
                char_bins[j] += rate * binw
        avg = sum(char_bins) / max(1, n)
        if avg > 0.01:
            for i in range(n):
                dens[i] += 1.0 * min(char_bins[i] / avg, 4.0)
    # 箱型平滑，别让每个切点都鼓成一个假爆发
    sm = []
    for i in range(n):
        lo, hi = max(0, i - 2), min(n, i + 3)
        sm.append(sum(dens[lo:hi]) / (hi - lo))
    return sm, binw


def _allocate(start: float, end: float, dens: list, binw: float,
              cells: int, cuts: list) -> list:
    """密度积分等分 + 吸收到真实剪辑点（位移 ≤ 理想格长 30%）。"""
    cells = max(1, int(cells))
    total = sum(dens) * binw
    if total <= 0 or len(dens) < cells:
        return [start + (end - start) * i / cells
                for i in range(cells)] + [end]
    cum, acc = [], 0.0
    for d in dens:
        acc += d * binw
        cum.append(acc)
    ideal = (end - start) / cells
    bounds = [start]
    prev = start
    for k in range(1, cells):
        target = total * k / cells
        j = bisect.bisect_left(cum, target)
        t = min(end, max(start, start + (j + 1) * binw))
        if cuts:
            near = min(cuts, key=lambda c: abs(c - t))
            if abs(near - t) <= 0.3 * ideal:
                t = near
        lo = prev + 0.12
        hi = end - 0.12 * (cells - k)
        if hi <= lo:
            # 吸附把格位挤死：退回均匀切，别给出零长度格
            return [start + (end - start) * i / cells
                    for i in range(cells)] + [end]
        bounds.append(min(hi, max(lo, t)))
        prev = bounds[-1]
    bounds.append(end)
    return bounds


# ── 取帧、拼图 ──────────────────────────────────────────────────


_SCALE_VF = (f'scale={_CELL_W}:{_CELL_H}:'
             'force_original_aspect_ratio=increase,'
             f'crop={_CELL_W}:{_CELL_H},format=rgb24')


def _grab_candidates(ff: str, path: str, t0: float, t1: float,
                     deadline: float) -> list:
    dur = max(0.05, t1 - t0)
    n = 3 if dur >= 0.35 else 1
    fps = max(0.2, min(15.0, n / dur))
    cmd = [ff, '-v', 'error', '-nostdin',
           '-ss', f'{t0:.3f}', '-t', f'{dur:.3f}', '-i', path,
           '-vf', f'fps={fps:.6f},{_SCALE_VF}', '-frames:v', str(n),
           '-an', '-sn', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
    raw = _run(cmd, max(5.0, deadline - time.monotonic()))
    frame_len = _CELL_W * _CELL_H * 3
    out = [raw[off:off + frame_len]
           for off in range(0, len(raw) - frame_len + 1, frame_len)]
    if not out:
        # 极短片段的 fps 过滤可能一帧都吐不出来：退回中点单帧
        mid = (t0 + t1) / 2.0
        raw = _run([ff, '-v', 'error', '-nostdin', '-ss', f'{mid:.3f}',
                    '-i', path, '-vf', _SCALE_VF, '-frames:v', '1',
                    '-an', '-sn', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
                    '-'], max(5.0, deadline - time.monotonic()))
        out = [raw[off:off + frame_len]
               for off in range(0, len(raw) - frame_len + 1, frame_len)]
    if not out:
        raise VideoError('取帧失败（这一格可能是损坏帧）')
    return out


def _sharpness(raw: bytes) -> float:
    if not _PIL_OK:
        return 0.0
    im = Image.frombuffer('RGB', (_CELL_W, _CELL_H), raw, 'raw', 'RGB', 0, 1)
    try:
        edges = im.convert('L').filter(ImageFilter.FIND_EDGES)
        return float(sum(ImageStat.Stat(edges).var))
    except Exception:
        return 0.0


def _to_image(raw: bytes):
    return Image.frombuffer('RGB', (_CELL_W, _CELL_H), raw, 'raw', 'RGB', 0, 1)


_FONT = {
    '0': ('01110', '10001', '10011', '10101', '11001', '10001', '01110'),
    '1': ('00100', '01100', '00100', '00100', '00100', '00100', '01110'),
    '2': ('01110', '10001', '00001', '00010', '00100', '01000', '11111'),
    '3': ('11110', '00001', '00001', '01110', '00001', '00001', '11110'),
    '4': ('00010', '00110', '01010', '10010', '11111', '00010', '00010'),
    '5': ('11111', '10000', '11110', '00001', '00001', '10001', '01110'),
    '6': ('00110', '01000', '10000', '11110', '10001', '10001', '01110'),
    '7': ('11111', '00001', '00010', '00100', '01000', '01000', '01000'),
    '8': ('01110', '10001', '10001', '01110', '10001', '10001', '01110'),
    '9': ('01110', '10001', '10001', '01111', '00001', '00010', '01100'),
    ':': ('00000', '00100', '00100', '00000', '00100', '00100', '00000'),
    '.': ('00000', '00000', '00000', '00000', '00000', '00110', '00110'),
    '-': ('00000', '00000', '00000', '11111', '00000', '00000', '00000'),
}


def _text_width(text: str, scale: int) -> int:
    return max(0, len(text) * 6 * scale - scale)


def _draw_text(draw, x: int, y: int, text: str, scale: int,
               fg=(255, 255, 255), bg=(0, 0, 0), pad: int = 2) -> None:
    """内置 5×7 点阵字：不依赖 fontconfig（Windows 上常常是坏的）。"""
    tw = _text_width(text, scale)
    th = 7 * scale
    draw.rectangle([x - pad, y - pad, x + tw + pad, y + th + pad], fill=bg)
    for ch in text:
        pattern = _FONT.get(ch)
        if pattern:
            for ry, row in enumerate(pattern):
                for rx, bit in enumerate(row):
                    if bit == '1':
                        draw.rectangle(
                            [x + rx * scale, y + ry * scale,
                             x + rx * scale + scale - 1,
                             y + ry * scale + scale - 1], fill=fg)
        x += 6 * scale


def _fmt_t(t: float) -> str:
    t = max(0.0, float(t))
    if t >= 3600:
        return (f'{int(t // 3600)}:'
                f'{int((t % 3600) // 60):02d}:{t % 60:04.1f}')
    return f'{int(t // 60)}:{t % 60:04.1f}'


def _fmt_plain(t: float) -> str:
    t = max(0.0, float(t))
    if t >= 3600:
        return f'{int(t // 3600)}:{int((t % 3600) // 60):02d}:{int(t % 60):02d}'
    return f'{int(t // 60)}:{int(t % 60):02d}'


def _waveform(ff: str, path: str, start: float, dur: float,
              width: int, deadline: float) -> bytes:
    vf = (f'aformat=channel_layouts=mono,'
          f'showwavespic=s={max(32, width)}x{_STRIP_H}:colors=0x38bdf8')
    cmd = [ff, '-v', 'error', '-nostdin',
           '-ss', f'{start:.3f}', '-t', f'{dur:.3f}', '-i', path,
           '-filter_complex', vf, '-frames:v', '1',
           '-f', 'image2pipe', '-vcodec', 'png', '-']
    try:
        return _run(cmd, max(4.0, deadline - time.monotonic()))
    except VideoError:
        return b''


_SILENCE_RE = re.compile(r'silence_(start|end):\s*([\d.]+)')


def _silences(ff: str, path: str, start: float, dur: float,
              deadline: float) -> list:
    cmd = [ff, '-v', 'info', '-nostdin',
           '-ss', f'{start:.3f}', '-t', f'{dur:.3f}', '-i', path,
           '-af', 'silencedetect=noise=-35dB:d=0.6', '-f', 'null', '-']
    try:
        rc, _, err = _exec(cmd, max(4.0, deadline - time.monotonic()))
    except VideoError:
        return []
    if rc != 0:
        return []
    marks = _SILENCE_RE.findall(err.decode('utf-8', 'replace'))
    out, cur = [], None
    for kind, val in marks:
        try:
            t = float(val)
        except ValueError:
            continue
        if kind == 'start':
            cur = t
        elif cur is not None:
            if t > cur:
                out.append((start + cur, start + t))
            cur = None
    return out


def _compose_sheet(images: list, cells: list, cuts: list, start: float,
                   end: float, wave: bytes, silences: list) -> bytes:
    if not _PIL_OK:
        raise VideoError('缺少 Pillow，无法生成分镜宫格图（插件依赖未安装完整）')
    n = len(images)
    rows = max(1, math.ceil(n / _COLS))
    width = _PAD * 2 + _COLS * _CELL_W + (_COLS - 1) * _GAP
    body_h = rows * _CELL_H + (rows - 1) * _GAP
    height = _PAD * 2 + body_h + 6 + _STRIP_H
    sheet = Image.new('RGB', (width, height), (20, 22, 27))
    draw = ImageDraw.Draw(sheet)

    for i, im in enumerate(images):
        col, row = i % _COLS, i // _COLS
        x = _PAD + col * (_CELL_W + _GAP)
        y = _PAD + row * (_CELL_H + _GAP)
        sheet.paste(im, (x, y))
        # 格号烧进像素（小模型数格子会数错，读数字很准）
        _draw_text(draw, x + 5, y + 5, str(i + 1), 4)
        a, b = cells[i]
        _draw_text(draw, x + 5, y + _CELL_H - 25, _fmt_t(a), 2,
                   fg=(235, 235, 235))
        cut_n = sum(1 for c in cuts if a <= c < b)
        if cut_n:
            label = str(cut_n)
            tw = _text_width(label, 2)
            bx = x + _CELL_W - tw - 12
            by = y + _CELL_H - 25
            draw.rectangle([bx - 6, by + 4, bx - 2, by + 12],
                           fill=(250, 204, 21))
            _draw_text(draw, bx, by, label, 2, fg=(250, 204, 21),
                       bg=(0, 0, 0))

    # 底部条带：音频包络 + 静音块 + 剪辑点刻度，与画面共用一条时间轴
    sy = _PAD + body_h + 6
    strip = Image.new('RGB', (width, _STRIP_H), (28, 30, 36))
    if wave:
        try:
            wim = Image.open(io.BytesIO(wave)).convert('RGB')
            strip.paste(wim.resize((width, _STRIP_H)), (0, 0))
        except Exception:
            pass
    span = max(0.001, end - start)
    sdraw = ImageDraw.Draw(strip)
    for a, b in silences or []:
        x0 = int((max(start, a) - start) / span * (width - 1))
        x1 = int((min(end, b) - start) / span * (width - 1))
        if x1 > x0:
            sdraw.rectangle([x0, 0, max(x0, x1 - 1), _STRIP_H - 1],
                            fill=(38, 38, 46))
    for c in cuts:
        if start <= c <= end:
            x = int((c - start) / span * (width - 1))
            sdraw.rectangle([x, 0, min(width - 1, x + 1), _STRIP_H - 1],
                            fill=(250, 204, 21))
    # 格边界：只画底部一小截，避免和黄色切点混淆
    for a, _b in cells[1:]:
        x = int((a - start) / span * (width - 1))
        sdraw.rectangle([x, _STRIP_H - 5, min(width - 1, x + 1),
                         _STRIP_H - 1], fill=(180, 180, 190))
    sheet.paste(strip, (0, sy))
    buf = io.BytesIO()
    sheet.save(buf, format='PNG', optimize=True)
    return buf.getvalue()


def _encode_sheet(png: bytes, max_kb: int) -> tuple[str, bytes]:
    if len(png) <= max_kb * 1024:
        return 'image/png', png
    im = Image.open(io.BytesIO(png)).convert('RGB')
    buf = io.BytesIO()
    im.save(buf, format='JPEG', quality=88, optimize=True)
    return 'image/jpeg', buf.getvalue()


# ── 台词（可选 ASR） ────────────────────────────────────────────


def _chunk_bounds(start: float, end: float, silences: list,
                  chunk: float = 40.0) -> list:
    total = end - start
    if total <= chunk:
        return [start, end]
    bounds = [start]
    k = 1
    while start + k * chunk < end:
        nominal = start + k * chunk
        midpoints = [(a + b) / 2 for a, b in silences
                     if abs((a + b) / 2 - nominal) <= 8
                     and a > start + 5 and b < end - 5]
        bounds.append(min(midpoints, key=lambda t: abs(t - nominal))
                      if midpoints else nominal)
        k += 1
    bounds.append(end)
    out = [bounds[0]]
    for t in bounds[1:]:
        if t - out[-1] >= 3.0:
            out.append(t)
    if out[-1] < end:
        out.append(end)
    return out


def _extract_chunks(ff: str, path: str, bounds: list,
                    deadline: float) -> list:
    chunks = []
    for i, (a, b) in enumerate(zip(bounds, bounds[1:])):
        cmd = [ff, '-v', 'error', '-nostdin',
               '-ss', f'{a:.3f}', '-t', f'{b - a:.3f}', '-i', path,
               '-vn', '-ac', '1', '-ar', '16000',
               '-c:a', 'libmp3lame', '-b:a', '32k', '-f', 'mp3', '-']
        raw = _run(cmd, max(5.0, deadline - time.monotonic()))
        if raw:
            chunks.append((a, b, f'video_{i:02d}.mp3', raw))
    return chunks


async def _transcribe_chunks(ctx, chunks: list, timeout: float,
                             log) -> tuple[list, str]:
    model = str(ctx.cfg.get('voice_asr_model') or '').strip()
    if not model:
        return [], '未配置 voice_asr_model，跳过台词转写'
    if ctx.llm is None or not hasattr(ctx.llm, 'transcribe'):
        return [], '当前模型客户端不支持音频转写，跳过台词'
    sem = asyncio.Semaphore(4)

    async def one(a, b, name, raw):
        async with sem:
            txt = await ctx.llm.transcribe(ctx.cfg, name, raw, model)
        return (a, b, str(txt or '').strip())

    tasks = [asyncio.create_task(one(*c)) for c in chunks]
    done, pending = await asyncio.wait(tasks, timeout=max(5.0, timeout))
    for t in pending:
        t.cancel()
    out = []
    for t in done:
        try:
            item = t.result()
        except Exception as e:
            if callable(log):
                log(f'read_video 台词转写失败: {e}', 'warn')
            continue
        if item[2]:
            out.append(item)
    out.sort(key=lambda x: x[0])
    return out, ''


def _cut_sentence(text: str, cap: int) -> str:
    text = ' '.join(text.split())
    if len(text) <= cap:
        return text
    head = text[:cap]
    for mark in ('。', '！', '？', '；', '.', '!', '?', ';'):
        j = head.rfind(mark)
        if j >= cap * 0.5:
            return head[:j + 1]
    return head + '…'


# ── 分析主流程（同步，调用方 to_thread） ─────────────────────────


def _cache_dir(db_path: str):
    if not db_path:
        return None
    d = Path(db_path).parent / 'video_cache'
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return d


def _cleanup_cache(d: Path, keep: int) -> None:
    try:
        files = [p for p in d.glob('sheet_*') if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime)
        for p in files[:max(0, len(files) - keep)]:
            p.unlink(missing_ok=True)
    except OSError:
        pass


def analyze(path: str, real_path: str, start: float | None,
            end: float | None, cells: int, cfg: dict,
            deadline: float) -> dict:
    """探测 + 扫描 + 分格 + 取帧 + 拼图，返回宫格图与逐格结构。"""
    if not _PIL_OK:
        raise VideoError('缺少 Pillow，无法生成分镜宫格图（插件依赖未安装完整）')
    ff = _require_ffmpeg()
    info = probe(real_path, max(5.0, min(20.0, deadline - time.monotonic())))
    dur = float(info['duration'])
    max_minutes = max(1, int(cfg.get('read_video_max_minutes', 15)))
    start = 0.0 if start is None else max(0.0, float(start))
    if start >= dur - 0.2:
        raise VideoError(f'起点 {_fmt_plain(start)} 超出视频时长 '
                         f'{_fmt_plain(dur)}')
    if end is None:
        end = min(dur, start + max_minutes * 60.0)
    else:
        end = min(dur, max(start + 0.3, float(end)))
    truncated = end < dur - 0.2
    window = end - start

    scan_fps = max(2, min(10, int(cfg.get('read_video_scan_fps', 4))))
    if window * scan_fps > _MAX_SCAN_FRAMES:
        scan_fps = max(1.0, _MAX_SCAN_FRAMES / window)
    raw = _decode_lowres(ff, real_path, start, end, scan_fps, deadline)
    scores = _frame_diffs(raw, _SCAN_W, _SCAN_H, deadline)
    cuts = [start + c for c in _detect_cuts(scores, scan_fps, window)]
    segs = _segments(cuts, start, end)

    n_cells = int(cells or 0)
    if n_cells <= 0:
        n_cells = auto_cells(window, segs, scores)
    n_cells = max(4, min(16, n_cells))
    dens, binw = _density(cuts, start, end)
    bounds = _allocate(start, end, dens, binw, n_cells, cuts)
    cell_ranges = list(zip(bounds, bounds[1:]))

    # 每格 3 候选，比锐度取最清晰的一帧；最多 4 路并行解码
    raws = [None] * len(cell_ranges)

    def _one(i):
        a, b = cell_ranges[i]
        return i, max(_grab_candidates(ff, real_path, a, b, deadline),
                      key=_sharpness)

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(cell_ranges)))) as pool:
        for i, best in pool.map(_one, range(len(cell_ranges))):
            raws[i] = best

    images = [_to_image(r) for r in raws]
    width = _PAD * 2 + _COLS * _CELL_W + (_COLS - 1) * _GAP
    wave, silences = b'', []
    if info['has_audio']:
        wave = _waveform(ff, real_path, start, window, width, deadline)
        silences = _silences(ff, real_path, start, window, deadline)
    png = _compose_sheet(images, cell_ranges, cuts, start, end, wave,
                         silences)
    mime, sheet = _encode_sheet(
        png, int(cfg.get('read_video_sheet_max_kb', 1800)))

    key = hashlib.sha1(
        f'{real_path}|{os.path.getsize(real_path)}|'
        f'{os.path.getmtime(real_path)}|{start:.3f}|{end:.3f}|'
        f'{scan_fps:.2f}|{n_cells}'.encode('utf-8')).hexdigest()[:20]
    sheet_path = ''
    cache_dir = _cache_dir(str(cfg.get('_db_path') or ''))
    if cache_dir is not None:
        ext = 'png' if mime == 'image/png' else 'jpg'
        sheet_path = str(cache_dir / f'sheet_{key}.{ext}')
        try:
            Path(sheet_path).write_bytes(sheet)
            _cleanup_cache(cache_dir,
                           max(4, int(cfg.get('read_video_keep_cache', 12))))
        except OSError:
            sheet_path = ''
    return {
        'key': key, 'name': Path(path).name, 'path': path,
        'info': info, 'start': start, 'end': end, 'duration': dur,
        'truncated': truncated, 'cells': cell_ranges, 'cuts': cuts,
        'scan_fps': scan_fps, 'mime': mime, 'sheet': sheet,
        'sheet_path': sheet_path, 'silences': silences, 'asr': None,
    }


def _cached(key: str):
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is not None:
            _CACHE.move_to_end(key)
        return entry


def _store_cache(key: str, entry: dict) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = entry
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)


# ── 子代理 ──────────────────────────────────────────────────────

_SUBAGENT_SYSTEM = (
    '你是一个视频分镜阅读助手。用户看不到视频，只能依靠你读图后写的文字。'
    '你会收到一段视频按时间顺序拆成的分镜宫格图：每格左上角的数字是格号，'
    '左下角是该格代表的时间码（mm:ss.d），右下角的黄色数字是该格内的剪辑点数；'
    '整张图底部是音频包络条带，青色是响度、深灰块是静音、黄色竖线是剪辑点。'
    '请只根据宫格图和随附的逐格清单回答：先用一两句话概括这段视频整体讲了'
    '什么/发生了什么，再分点描述关键镜头、人物、动作、画面文字和节奏变化。'
    '引用信息时带上格号和时间码（如「第 5 格 0:42」）。'
    '看不清、被遮挡或无法判断的地方直接说不确定，不要编造台词或画面内容。'
    '不要提图片、宫格、token 等技术细节，除非用户明确问你是怎么看的。'
)


def _manifest(data: dict, transcript: list, detail: str) -> str:
    cap = {'low': 60, 'med': 120, 'high': 400}.get(detail, 120)
    lines = []
    for i, (a, b) in enumerate(data['cells']):
        cut_n = sum(1 for c in data['cuts'] if a <= c < b)
        line = f'[{i + 1}] {_fmt_t(a)}–{_fmt_t(b)} · 切点 {cut_n}'
        txt = _cut_sentence(
            ' '.join(t for x, y, t in transcript if x < b and y > a), cap)
        if txt:
            line += f' · 台词：{txt}'
        lines.append(line)
    return '\n'.join(lines)


def _header(data: dict) -> str:
    info = data['info']
    return (f'文件 {data["name"]}，视频 {_fmt_plain(data["duration"])}，'
            f'{info["width"]}x{info["height"]}，{info["fps"]:.1f}fps，'
            f'{"有" if info["has_audio"] else "无"}音轨；'
            f'本次阅读 {_fmt_plain(data["start"])}–{_fmt_plain(data["end"])}，'
            f'{len(data["cells"])} 格，{len(data["cuts"])} 个剪辑点。')


async def _ask_subagent(ctx, data: dict, manifest: str, question: str,
                        timeout: float) -> str:
    from . import config as cfg_mod
    model = str(ctx.cfg.get('read_video_model') or '').strip() or None
    if model and not cfg_mod.is_valid_route_value(ctx.cfg, model):
        raise VideoError(
            f'read_video_model 配置无效（{model}）：必须写成“服务商id/模型id”，'
            '且模型已在该服务商的模型列表里')
    intro = _header(data)
    if data.get('truncated'):
        intro += '（视频更长，本次只读了这一段；需要后面内容请带 from/to 再读）'
    q = question.strip() or '请完整总结这段视频'
    text = (f'{intro}\n\n用户的问题：{q}\n\n逐格清单：\n{manifest}\n\n'
            '请据此作答。')
    b64 = base64.b64encode(data['sheet']).decode('ascii')
    parts = [
        {'type': 'text', 'text': text},
        {'type': 'image_url',
         'image_url': {'url': f'data:{data["mime"]};base64,{b64}'}},
    ]
    messages = [{'role': 'system', 'content': _SUBAGENT_SYSTEM},
                {'role': 'user', 'content': parts}]
    res = await ctx.llm.chat(messages, ctx.cfg, model=model, kind='video',
                             max_tokens=1800, no_thinking=True,
                             timeout=max(10.0, timeout))
    return str(getattr(res, 'content', '') or '').strip()


# ── 工具入口 ────────────────────────────────────────────────────


def _parse_time(value) -> float | None:
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise VideoError('时间参数不能是布尔值')
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace('，', '.').replace(',', '.')
    if not s:
        return None
    try:
        if ':' in s:
            parts = [float(x) for x in s.split(':')]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            if len(parts) == 2:
                return parts[0] * 60 + parts[1]
            raise ValueError
        return float(s)
    except ValueError:
        raise VideoError(f'时间格式不对：{value!r}（用秒数或 mm:ss）')


async def handle(args: dict, ctx) -> str:
    """read_video 主体：解析路径 → 分镜 → 子代理 → 返回总结。"""
    args = args or {}
    path = str(args.get('path') or '').strip()
    if not path:
        return '错误: path 为空（填工作区里的视频/GIF 相对路径）'
    cfg = ctx.cfg
    if not cfg.get('read_video_on', True):
        return '错误: 本群已关闭 read_video（read_video_on）'
    if ctx.llm is None:
        return '错误: 当前没有可用的模型客户端，无法启动视频阅读子代理'

    try:
        real, _is_public = ctx.sandbox.resolve(path)
    except SandboxError as e:
        return f'错误: {e}'
    if not real.exists() or real.is_dir():
        return f'错误: 视频不存在: {path}'
    if real.suffix.lower() not in _SUPPORTED_EXT:
        return ('错误: 只支持常见视频/GIF 格式（mp4/mov/mkv/webm/avi/gif 等），'
                f'当前文件是 {real.suffix or "无扩展名"}')
    size = real.stat().st_size
    max_mb = max(1, int(cfg.get('read_video_max_mb', 512)))
    if size > max_mb * 1024 * 1024:
        return f'错误: 视频 {size / 1048576:.0f}MiB，超过 {max_mb}MiB 上限'

    try:
        start = _parse_time(args.get('from'))
        end = _parse_time(args.get('to'))
        if end is not None and start is not None and end <= start:
            return '错误: to 必须大于 from'
        cells = int(args.get('cells') or 0)
    except VideoError as e:
        return f'错误: {e}'
    except (TypeError, ValueError):
        return '错误: from/to/cells 必须是数字或 mm:ss'
    detail = str(args.get('detail') or 'med').lower()
    if detail not in ('low', 'med', 'high'):
        detail = 'med'
    question = str(args.get('question') or '').strip()

    timeout = max(30.0, float(cfg.get('read_video_timeout', 180)))
    overall = time.monotonic() + timeout
    asr_on = bool(cfg.get('read_video_asr_on', False))
    cells_cfg = max(0, min(16, int(cfg.get('read_video_cells', 0) or 0)))
    use_cells = max(4, min(16, cells)) if cells else cells_cfg

    stat_key = (f'{real}|{size}|{real.stat().st_mtime_ns}|'
                f'{start if start is not None else ""}|'
                f'{end if end is not None else ""}|{use_cells}|'
                f'{cfg.get("read_video_scan_fps", 4)}')
    key = hashlib.sha1(stat_key.encode('utf-8')).hexdigest()[:24]
    data = _cached(key)
    if data is None:
        cfg2 = dict(cfg)
        cfg2['_db_path'] = getattr(ctx, 'db_path', '') or ''
        try:
            data = await asyncio.to_thread(
                analyze, path, str(real), start, end, use_cells, cfg2,
                overall - timeout * 0.38)
        except SandboxError as e:
            return f'错误: {e}'
        except VideoError as e:
            return f'错误: {e}'
        _store_cache(key, data)

    transcript = data.get('asr')
    asr_note = ''
    if asr_on and transcript is None:
        if not data['info']['has_audio']:
            transcript, asr_note = [], '视频没有音轨，跳过台词转写'
        else:
            try:
                ff = _require_ffmpeg()
                max_chunks = max(1, int(cfg.get('read_video_asr_max_chunks',
                                                24)))
                bounds = _chunk_bounds(data['start'], data['end'],
                                       data['silences'])[:max_chunks + 1]
                chunks = await asyncio.to_thread(
                    _extract_chunks, ff, str(real), bounds,
                    min(overall, time.monotonic() + timeout * 0.15))
                transcript, asr_note = await _transcribe_chunks(
                    ctx, chunks, timeout * 0.18, getattr(ctx, 'log', None))
            except VideoError as e:
                transcript, asr_note = [], f'台词转写失败：{e}'
        data['asr'] = transcript
    if transcript is None:
        transcript = []

    manifest = _manifest(data, transcript, detail)
    remaining = overall - time.monotonic()
    try:
        summary = await _ask_subagent(
            ctx, data, manifest, question,
            timeout=max(15.0, min(timeout * 0.45, remaining - 3)))
    except VideoError as e:
        return f'错误: {e}'
    except Exception as e:
        log = getattr(ctx, 'log', None)
        if callable(log):
            log(f'read_video 子代理失败: {e!r}', 'warn')
        return f'错误: 视频阅读子代理调用失败: {e}'
    if not summary:
        summary = '（视觉子代理没有返回内容）'

    cap = max(800, int(cfg.get('tool_result_max_chars', 2000)))
    head = (f'已让视频阅读子代理看完 {path}（{_fmt_plain(data["start"])}'
            f'–{_fmt_plain(data["end"])}，{len(data["cells"])} 格）。\n')
    if data.get('sheet_path'):
        head += f'分镜图：{data["sheet_path"]}\n'
    if asr_note:
        head += f'（{asr_note}）\n'
    out = f'{head}【子代理总结】\n{summary}\n\n【逐格时间轴】\n{manifest}\n'
    if data.get('truncated'):
        out += ('\n视频还有后续内容。需要时带 from/to（如 '
                f'from="{_fmt_plain(data["end"])}"）再读下一段。')
    if len(out) > cap:
        out = out[:cap] + '\n…(已截断，需要细节可带 from/to 再读)'
    return out
