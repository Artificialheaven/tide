"""PDF/Word 文档栅格化成页面 JPEG，送多模态视觉阅读。

用途：扫描版 PDF（抽不出文本）、排版复杂的 Word（表格/图文混排）——
文本提取不够用时，直接让模型"看"页面。

- PDF：PyMuPDF（import pymupdf，老版本包名 fitz）按 DPI 栅格化，Pillow 压 JPEG
- Word（.docx / 老 .doc）：本机 LibreOffice（soffice）无头转 PDF 后走同一管线
依赖全部延迟导入、外部程序按 PATH + 常见安装目录探测；
缺依赖/转换失败统一抛 DocRenderError（人话说明），由调用方转成工具错误文本。
全部是同步重活，调用方必须放在 to_thread 里跑。
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SUPPORTED_EXTS = ('pdf', 'docx', 'doc')


class DocRenderError(Exception):
    """渲染不了（缺依赖、文件坏、超上限等），args[0] 是可直接给模型看的中文说明。"""


def _fitz():
    """延迟导入 PyMuPDF，新旧包名都兜住。"""
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        pass
    try:
        import fitz  # PyMuPDF < 1.24 的顶层包名
        return fitz
    except ImportError:
        raise DocRenderError(
            '文档渲染依赖 PyMuPDF（未安装），无法把 PDF 转成页面图片')


def find_soffice() -> str:
    """定位 LibreOffice 可执行文件；找不到返回 ''。

    Windows 优先 soffice.com（控制台子系统，转换结束才返回），
    soffice.exe 可能 detach 导致拿不到转换结果。
    """
    found = shutil.which('soffice') or shutil.which('soffice.exe')
    if found:
        if found.lower().endswith('.exe'):
            com = found[:-4] + '.com'
            if os.path.exists(com):
                return com
        return found
    if sys.platform == 'win32':
        roots = [os.environ.get('ProgramFiles'),
                 os.environ.get('ProgramFiles(x86)'),
                 os.environ.get('ProgramW6432')]
        for root in roots:
            if not root:
                continue
            pdir = os.path.join(root, 'LibreOffice', 'program')
            for cand in ('soffice.com', 'soffice.exe'):
                p = os.path.join(pdir, cand)
                if os.path.exists(p):
                    return p
    return ''


def _office_to_pdf(raw: bytes, ext: str, timeout: int = 90) -> bytes:
    """LibreOffice 无头把 Word 文档转成 PDF（临时目录隔离，带独立用户配置）。"""
    soffice = find_soffice()
    if not soffice:
        raise DocRenderError(
            'Word 渲染需要运行机器上安装 LibreOffice（未找到 soffice），'
            '无法转成页面图片；可以把文档另存为 PDF 后再发')
    tmp = tempfile.mkdtemp(prefix='dsdoc_')
    try:
        src = os.path.join(tmp, f'doc.{ext}')
        with open(src, 'wb') as f:
            f.write(raw)
        # 独立 UserInstallation：用户正开着 LibreOffice 时不会因配置目录锁死失败
        profile_uri = Path(tmp).as_uri() + '/lo_profile'
        cmd = [soffice, '--headless', '--norestore', '--nologo',
               f'-env:UserInstallation={profile_uri}',
               '--convert-to', 'pdf', '--outdir', tmp, src]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise DocRenderError('LibreOffice 转 PDF 超时（超过 %s 秒）' % timeout)
        out_pdf = os.path.join(tmp, 'doc.pdf')
        if not os.path.exists(out_pdf):
            detail = (p.stderr or p.stdout or b'').decode('utf-8', 'replace')[:200]
            raise DocRenderError(
                f'LibreOffice 转 PDF 失败（{ext} 文档可能已损坏）。{detail}'.strip())
        return Path(out_pdf).read_bytes()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _encode_jpeg(png_bytes: bytes, max_kb: int) -> bytes:
    """Pixmap 出来的 PNG 转 RGB JPEG，质量级联；仍超限时缩 0.8 再压。"""
    try:
        from PIL import Image
    except ImportError:
        raise DocRenderError('图片压缩依赖 Pillow（未安装），无法渲染页面')
    img = Image.open(io.BytesIO(png_bytes))
    if img.mode != 'RGB':
        img = img.convert('RGB')
    for quality in (88, 75, 62, 50):
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=quality)
        if buf.tell() <= max_kb * 1024:
            return buf.getvalue()
    img = img.resize((max(1, int(img.width * 0.8)),
                      max(1, int(img.height * 0.8))))
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=50)
    return buf.getvalue()


def _render_pdf(raw: bytes, want_pages, max_pages: int,
                dpi: int, page_max_kb: int) -> list:
    """PDF → [(1 基页码, jpeg_bytes), ...]。want_pages 为 None 时取前 max_pages 页。"""
    fz = _fitz()
    try:
        doc = fz.open(stream=io.BytesIO(raw), filetype='pdf')
    except Exception as e:
        raise DocRenderError(f'PDF 打不开（可能已损坏）：{e}')
    total = doc.page_count
    if want_pages is None:
        idxs = list(range(min(total, max_pages)))
    else:
        idxs = sorted({p - 1 for p in want_pages if 1 <= p <= total})
        if not idxs:
            raise DocRenderError(
                f'页码无效（文档共 {total} 页，页码从 1 开始）')
        if len(idxs) > max_pages:
            raise DocRenderError(f'一次最多渲染 {max_pages} 页，请减少 pages')
    pages = []
    for i in idxs:
        pix = doc.load_page(i).get_pixmap(dpi=dpi, alpha=False)
        pages.append((i + 1, _encode_jpeg(pix.tobytes('png'), page_max_kb)))
    return pages


def render_document(filename: str, raw: bytes, *, want_pages=None,
                    max_pages: int = 3, dpi: int = 130,
                    page_max_kb: int = 1536,
                    src_max_kb: int = 20480) -> list:
    """把文档渲染成页面图。返回 [(1 基页码, jpeg_bytes), ...]；失败抛 DocRenderError。"""
    ext = (filename or '').rsplit('.', 1)[-1].lower()
    if ext not in SUPPORTED_EXTS:
        raise DocRenderError(
            f'只支持渲染 PDF / Word（.pdf/.docx/.doc），收到的是 .{ext or "?"}')
    if len(raw) > src_max_kb * 1024:
        raise DocRenderError(
            f'文档 {len(raw)} 字节，超过 {src_max_kb}KiB 的渲染上限')
    if want_pages is not None:
        try:
            want_pages = [int(p) for p in want_pages]
        except (TypeError, ValueError):
            raise DocRenderError('pages 必须是 1 开始的整数页码数组')
    if ext in ('docx', 'doc'):
        raw = _office_to_pdf(raw, ext)
    return _render_pdf(raw, want_pages, max_pages, dpi, page_max_kb)
