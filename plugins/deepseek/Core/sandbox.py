"""群工作区文件沙箱（同步核心，纯逻辑可单测）。

- 每群一个目录 workspace/<group_id>/，另有跨群共享 workspace/public/
  （公共素材/技能库：默认可写，独立配额，写/删属风险操作，引擎层限主人）
- 禁绝对路径、盘符、`..`；resolve 后做双根校验（含 symlink realpath）
- 群目录与 public 各自独立的单文件大小 / 总配额
调用方（builtin_tools）用 asyncio.to_thread 包裹，避免阻塞事件循环。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

_DRIVE_RE = re.compile(r'^[a-zA-Z]:[\\/]')


def purge_all_mcp_media(workspace_root: str, ttl_seconds: float) -> int:
    """启动期清扫：遍历所有群工作区删除过期 MCP 媒体（public 不动）。"""
    if ttl_seconds <= 0:
        return 0
    root = Path(workspace_root)
    if not root.exists():
        return 0
    total = 0
    for gd in root.iterdir():
        if not gd.is_dir() or gd.name == 'public':
            continue
        try:
            total += Sandbox(str(root), gd.name).purge_mcp_media(ttl_seconds)
        except OSError:
            pass
    return total


class SandboxError(PermissionError):
    pass


def _parts(rel: str) -> list[str]:
    rel = (rel or '.').strip().replace('\\', '/')
    if rel.startswith('/') or _DRIVE_RE.match(rel):
        raise SandboxError('禁止使用绝对路径')
    parts = []
    for p in rel.split('/'):
        if p in ('', '.'):
            continue
        if p == '..':
            raise SandboxError('禁止使用 .. 越界')
        parts.append(p)
    return parts


class Sandbox:
    def __init__(self, workspace_root: str, group_id,
                 max_file_kb: int = 512, quota_mb: int = 10,
                 public_writable: bool = True,
                 public_max_file_kb: int = 4096, public_quota_mb: int = 50,
                 mcp_media_max_kb: int = 15360):
        self.root = Path(workspace_root)
        self.group_root = (self.root / str(group_id)).resolve()
        self.public_root = (self.root / 'public').resolve()
        self.max_file_bytes = int(max_file_kb) * 1024
        self.quota_bytes = int(quota_mb) * 1024 * 1024
        self.public_writable = bool(public_writable)
        self.public_max_file_bytes = int(public_max_file_kb) * 1024
        self.public_quota_bytes = int(public_quota_mb) * 1024 * 1024
        # MCP 截图/录音是中转大文件（全屏 PNG 常达数 MiB），给独立上限；
        # 不计入群工作区配额，磁盘占用由 mcp_media_ttl_hours 清扫兜底
        self.mcp_media_max_bytes = int(mcp_media_max_kb) * 1024

    # ── 路径校验 ────────────────────────────────────────────────

    def resolve(self, rel: str, for_write: bool = False) -> tuple[Path, bool]:
        """返回 (绝对路径, 是否 public 共享区)。越界拒绝；public 关闭写入时拒绝。"""
        parts = _parts(rel)
        is_public = bool(parts and parts[0] == 'public')
        if is_public:
            base, sub = self.public_root, parts[1:]
        else:
            base, sub = self.group_root, parts
        target = base.joinpath(*sub) if sub else base
        real = target.resolve()
        if base != real and base not in real.parents:
            raise SandboxError('路径越界')
        # symlink 二次校验：realpath 必须仍在允许的根内
        if real != target and not self._under(real, self.group_root) \
                and not self._under(real, self.public_root):
            raise SandboxError('链接路径越界')
        if for_write and is_public and not self.public_writable:
            raise SandboxError('public 公共素材库当前为只读，禁止写入')
        return real, is_public

    @staticmethod
    def _under(path: Path, root: Path) -> bool:
        return path == root or root in path.parents

    # ── 配额（群目录 / public 各自独立记账）─────────────────────

    @staticmethod
    def _dir_used(root: Path) -> int:
        total = 0
        if not root.exists():
            return 0
        for p in root.rglob('*'):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        return total

    def _group_used(self) -> int:
        # mcp/ 走独立上限 + TTL 清扫，不占群工作区配额，否则两张大图就把配额顶满
        return self._dir_used(self.group_root) - \
            self._dir_used(self.group_root / 'mcp')

    def _check_write(self, target: Path, content: bytes, overwrite: bool,
                     is_public: bool):
        if is_public:
            cap, quota, used_root = (
                self.public_max_file_bytes, self.public_quota_bytes,
                self.public_root)
        else:
            cap, quota, used_root = (
                self.max_file_bytes, self.quota_bytes, self.group_root)
        if len(content) > cap:
            kind = 'public 素材' if is_public else '文件'
            raise SandboxError(f'{kind}单文件不得超过 {cap // 1024}KiB')
        old = target.stat().st_size if overwrite and target.exists() else 0
        if self._dir_used(used_root) - old + len(content) > quota:
            area = 'public 公共库' if is_public else '群工作区'
            raise SandboxError(f'{area}总配额 {quota // 1024 // 1024}MiB 已满')

    # ── 操作 ────────────────────────────────────────────────────

    def list_dir(self, rel: str = '.') -> list[dict]:
        target, _ = self.resolve(rel)
        if not target.exists():
            return []
        if not target.is_dir():
            raise SandboxError('不是目录')
        items = []
        for p in sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name))[:200]:
            items.append({
                'name': p.name + ('/' if p.is_dir() else ''),
                'type': 'dir' if p.is_dir() else 'file',
                'size': p.stat().st_size if p.is_file() else 0,
            })
        return items

    def read_file(self, rel: str) -> str:
        target, _ = self.resolve(rel)
        if target.is_dir():
            raise SandboxError('目标是目录')
        data = target.read_bytes()
        if len(data) > self.max_file_bytes:
            raise SandboxError('文件过大')
        try:
            return data.decode('utf-8')
        except UnicodeDecodeError:
            return data[:64].hex() + '…(二进制文件)'

    def read_bytes(self, rel: str) -> bytes:
        target, _ = self.resolve(rel)
        if not target.exists() or target.is_dir():
            raise SandboxError('文件不存在')
        data = target.read_bytes()
        if len(data) > self.max_file_bytes:
            raise SandboxError('文件过大')
        return data

    def write_bytes(self, rel: str, data: bytes) -> int:
        """二进制写入（MCP 图片/语音块、fs_publish 落盘走这里，过配额校验）。"""
        target, is_public = self.resolve(rel, for_write=True)
        self._check_write(target, data, target.exists(), is_public)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target.stat().st_size

    def write_mcp_media(self, rel: str, data: bytes,
                        limit_bytes: int | None = None) -> int:
        """MCP 截图/录音/入站视频专用落盘：必须写在本群 mcp/ 下。

        走独立的大文件单上限（mcp_media_max_kb，默认 15MiB），
        不占群工作区配额；TTL 清扫负责回收磁盘。limit_bytes 可按调用方
        放宽（入站视频另有 inbound_video_max_kb 上限）。
        """
        target, is_public = self.resolve(rel, for_write=True)
        if is_public:
            raise SandboxError('MCP 媒体只能写到本群 mcp/ 下')
        try:
            sub = target.relative_to(self.group_root)
        except ValueError:
            raise SandboxError('MCP 媒体只能写到本群 mcp/ 下')
        if not sub.parts or sub.parts[0] != 'mcp':
            raise SandboxError('MCP 媒体只能写到 mcp/ 目录下')
        cap = int(limit_bytes) if limit_bytes else self.mcp_media_max_bytes
        if len(data) > cap:
            raise SandboxError(f'MCP 媒体单文件不得超过 {cap // 1024}KiB')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target.stat().st_size

    def write_public(self, rel: str, data: bytes) -> int:
        """把素材写进跨群共享的 public/（rel 必须以 public/ 开头）。"""
        target, is_public = self.resolve(rel, for_write=True)
        if not is_public:
            raise SandboxError('公共素材只能写到 public/ 下')
        self._check_write(target, data, target.exists(), True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target.stat().st_size

    def write_file(self, rel: str, content: str, append: bool = False) -> int:
        target, is_public = self.resolve(rel, for_write=True)
        data = (content or '').encode('utf-8')
        if append and target.exists():
            self._check_write(target, target.read_bytes() + data, True,
                              is_public)
            with target.open('ab') as f:
                f.write(data)
        else:
            self._check_write(target, data, target.exists(), is_public)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return target.stat().st_size

    def purge_mcp_media(self, ttl_seconds: float) -> int:
        """删除本群 mcp/ 目录里 mtime 早于 ttl 的媒体文件，返回删除条数。

        MCP 截图/录音只是发给群的中转文件，无需长期保留。
        """
        mcp_dir = self.group_root / 'mcp'
        if ttl_seconds <= 0 or not mcp_dir.exists():
            return 0
        cutoff = time.time() - ttl_seconds
        n = 0
        for p in mcp_dir.rglob('*'):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    n += 1
            except OSError:
                pass
        return n

    def delete(self, rel: str) -> bool:
        target, is_public = self.resolve(rel, for_write=True)
        # 禁止删除两个根目录本身（一条 delete('.') 清空全群的兜底）
        if target in (self.group_root, self.public_root):
            raise SandboxError('禁止删除工作区根目录')
        if not target.exists():
            return False
        if target.is_dir():
            import shutil
            shutil.rmtree(target)
        else:
            target.unlink()
        return True
