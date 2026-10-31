"""群文件 / 私聊文件的列举与下载（OneBot v11 扩展 API + 本地私聊清单）。

- 群文件：实时调 get_group_root_files / get_group_files_by_folder 列目录，
  get_group_file_url 换临时直链（QQ 协议支持，模型不用本地登记）。
- 私聊文件：OneBot 没有列举接口，由插件在收到私聊文件时登记进 private_files
  表，这里读本地清单；直链优先用段数据自带 url（可能过期），否则试通用 get_file。
下载后的解析/渲染走 media_in.process_file，与入站附件同一条管线。
"""
from __future__ import annotations

import asyncio
import datetime
import json

from . import media_in, storage


class RemoteFileError(Exception):
    """取不到列表/链接等业务失败，args[0] 是给模型看的中文说明。"""


def register_private_files(db_path: str, uid: str, parsed, log=None) -> int:
    """把私聊消息里的文件引用登记进 private_files，返回新登记条数。"""
    log = log or (lambda msg, level='info': None)
    n_new = 0
    for ref in getattr(parsed, 'media', None) or []:
        if ref.get('kind') != 'file':
            continue
        fid = ref.get('file_id') or ''
        if not fid:
            continue
        try:
            size = int(ref.get('file_size') or ref.get('size') or 0)
        except (TypeError, ValueError):
            size = 0
        try:
            ref_json = json.dumps(
                {k: v for k, v in ref.items() if k != 'kind'},
                ensure_ascii=False)
        except (TypeError, ValueError):
            ref_json = ''
        is_new = storage.upsert_private_file(
            db_path, uid, fid, busid=ref.get('busid', ''),
            name=ref.get('name') or ref.get('file') or '附件',
            file_size=size, msg_time=getattr(parsed, 'event_time', 0),
            ref_json=ref_json)
        n_new += 1 if is_new else 0
        log(f'私聊文件已{"登记" if is_new else "刷新"}：{uid} '
            f'{ref.get("name") or fid}', 'ok')
    return n_new


def _unwrap(resp: dict) -> dict:
    if isinstance(resp, dict):
        d = resp.get('data')
        return d if isinstance(d, dict) else resp
    return {}


async def _api(bot, action: str, params: dict) -> dict:
    if bot is None or not hasattr(bot, 'call_api'):
        return {}
    return _unwrap(await bot.call_api(action, params))


def _is_url(s) -> bool:
    return isinstance(s, str) and s.startswith(('http://', 'https://'))


def _human_size(n) -> str:
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return '?'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024


def _ymd(ts) -> str:
    try:
        ts = int(ts or 0)
        if ts <= 0:
            return ''
        return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
    except (TypeError, ValueError, OSError):
        return ''


def _file_line(i: int, f: dict) -> str:
    name = f.get('file_name') or f.get('name') or '(未命名)'
    meta = ' · '.join(p for p in (
        _human_size(f.get('file_size') or f.get('size')),
        f.get('uploader_name') or str(f.get('uploader') or ''),
        _ymd(f.get('upload_time') or f.get('msg_time')),
    ) if p)
    busid = f.get('busid', '')
    fid = f.get('file_id') or f.get('id') or ''
    return (f'{i}. {name}' + (f' · {meta}' if meta else '')
            + f'\n   file_id={fid} busid={busid}')


async def list_group_files(bot, group_id: str, folder_id: str = '') -> str:
    gid = int(group_id) if str(group_id).isdigit() else group_id
    try:
        if folder_id:
            resp = await _api(bot, 'get_group_files_by_folder',
                              {'group_id': gid, 'folder_id': folder_id})
            where = f'文件夹 {folder_id}'
        else:
            resp = await _api(bot, 'get_group_root_files', {'group_id': gid})
            where = '根目录'
    except Exception as e:
        raise RemoteFileError(f'读取群文件列表失败：{e}')
    if not isinstance(resp, dict):
        raise RemoteFileError('读取群文件列表失败：适配器返回格式异常')
    folders = resp.get('folders') or []
    files = resp.get('files') or []
    return _format_listing(f'群文件 · {where}', folders, files)


def _format_listing(title: str, folders: list, files: list) -> str:
    if not folders and not files:
        return f'📁 {title}：（空）'
    folders, files = folders[:40], files[:40]
    lines = [f'📁 {title}（📂 {len(folders)} 个文件夹，📄 {len(files)} 个文件）']
    if folders:
        lines.append('—— 进入文件夹：qq_list_files 传 folder_id ——')
        for i, d in enumerate(folders, 1):
            cnt = d.get('total_file_count', '')
            name = d.get('folder_name') or '(未命名文件夹)'
            lines.append(
                f'D{i}. {name}' + (f'（{cnt} 个文件）' if cnt != '' else '')
                + f'\n   folder_id={d.get("folder_id", "")}')
    lines.append('—— 文件：选定后调 qq_download_file，file_id/busid 照抄 ——')
    for i, f in enumerate(files, 1):
        lines.append(_file_line(i, f))
    if len(folders) >= 40 or len(files) >= 40:
        lines.append('…（条目过多已截断，可先进文件夹查看）')
    return '\n'.join(lines)


def list_private_files(rows: list) -> str:
    if not rows:
        return ('📁 私聊文件清单：（空）。把文件发给我后我会自动登记；'
                'OneBot 协议不支持回溯历史私聊文件。')
    files = [{'file_name': r.get('name', ''), 'file_size': r.get('file_size', 0),
              'upload_time': r.get('msg_time', 0),
              'file_id': r.get('file_id', ''), 'busid': r.get('busid', '')}
             for r in rows[:30]]
    return _format_listing('私聊文件 · 机器人为你登记的清单', [], files)


async def _group_file_url(bot, chat_id: str, file_id: str, busid) -> str:
    gid = int(chat_id) if str(chat_id).isdigit() else chat_id
    try:
        d = await _api(bot, 'get_group_file_url',
                       {'group_id': gid, 'file_id': file_id,
                        'busid': int(busid or 0)})
    except Exception as e:
        raise RemoteFileError(f'获取群文件下载链接失败：{e}')
    url = d.get('url') if isinstance(d, dict) else ''
    if not _is_url(url):
        raise RemoteFileError('适配器没返回有效的群文件下载链接（文件可能已过期）')
    return url


async def _private_file_url(bot, row: dict) -> str:
    ref = {}
    try:
        ref = json.loads(row.get('ref_json') or '{}')
    except json.JSONDecodeError:
        pass
    if _is_url(ref.get('url')):
        return ref['url']
    try:
        d = await _api(bot, 'get_file', {'file_id': row.get('file_id', '')})
    except Exception as e:
        raise RemoteFileError(f'获取私聊文件下载链接失败：{e}')
    url = (d.get('url') if isinstance(d, dict) else '') or ''
    if not _is_url(url):
        f = d.get('file') if isinstance(d, dict) else ''
        if _is_url(f):
            url = f
    if not url:
        raise RemoteFileError(
            '适配器没返回私聊文件下载链接（NapCat 可能不支持 get_file，'
            '或文件已过期；请把文件重新发一次再下载）')
    return url


async def fetch_remote_file(bot, chat_type: str, chat_id: str, file_id: str,
                            busid=None, name_hint: str = '', *,
                            db_path: str, cfg: dict, log=None
                            ) -> tuple:
    """下载一个群/私聊文件并解析成 (文本, 图片列表)。失败抛 RemoteFileError。"""
    if not file_id:
        raise RemoteFileError('file_id 为空（从 qq_list_files 的结果里照抄）')
    if chat_type == 'private':
        row = storage.get_private_file(db_path, chat_id, file_id)
        if row is None:
            raise RemoteFileError(
                '你的私聊文件清单里没有这个 file_id；先 qq_list_files 看清单，'
                '或把文件重新发给我登记')
        name_hint = name_hint or row.get('name', '')
        busid = busid if busid not in (None, '') else row.get('busid', '')
        url = await _private_file_url(bot, row)
    else:
        url = await _group_file_url(bot, chat_id, file_id, busid)

    budget = int(cfg.get('qq_file_max_kb', 20480)) * 1024
    try:
        raw = await media_in._http_get(url, budget)
    except Exception as e:
        raise RemoteFileError(f'下载失败（{type(e).__name__}: {e}）')

    fname = name_hint or f'{file_id}'
    slots = int(cfg.get('vision_max_images', 4))
    text, images = await asyncio.to_thread(
        media_in.process_file, fname, raw, cfg, slots, log)
    if not text and not images:
        text = f'已下载 {fname}（{len(raw)} 字节），但没提取到可读内容'
    return text, images
