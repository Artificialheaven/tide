# -*- coding: utf-8 -*-
"""梗库生成工具：B 站视频字幕 → AI 提取梗 → meme.db
=====================================================
独立脚本，不依赖机器人框架或插件代码；只依赖第三方库。

从指定 UP 主（默认 uid=94510621）的投稿列表里，按发布时间从新到旧逐个：
  视频标题 + 字幕 → AI 提取梗（名称/拼音/别名/简要）→ 写入 meme.db

依赖：
    pip install bilibili-api-python aiohttp openai tqdm
可选（无字幕视频的本地语音识别）：
    pip install faster-whisper        # 需要 ffmpeg 在 PATH；首次运行自动下载模型

首次运行：终端打印 B 站登录二维码，扫码后凭据存入 DB 同目录的
bili_credential.json（权限 0600），之后自动复用。

默认输出 plugins/deepseek/db/meme.db（插件直接读取）；用 --db 可写到任意路径，
首次写入时自动建表（与插件 Core/meme_guide.py 的 schema v2 完全一致）。
登录凭据、缓存列表、ASR 模型都放在 DB 所在目录，便于整体搬迁。

用法（在任意目录均可运行）：
    python scripts/meme_import_bili.py
    python scripts/meme_import_bili.py --from BV1xxxx --end BV1yyyy
    python scripts/meme_import_bili.py --from BV1xxxx   # 从该 BV 向旧扫，遇到库里已有 BV 停
    python scripts/meme_import_bili.py --end BV1yyyy    # 从最新投稿扫到该 BV（含）
    # 都不指定：从最新视频开始，遇到 memes.source_bv 已存在的 BV 停止（该条不处理）

    python scripts/meme_import_bili.py --db /path/to/meme.db   # 指定输出库

缓存列表（每次规划出的视频清单落盘，中断可续）：
    python scripts/meme_import_bili.py list             # 查看全部缓存列表
    python scripts/meme_import_bili.py check 3          # 查看 3 号列表起点/完成数/总数
    python scripts/meme_import_bili.py --re 3           # 从 3 号列表的结束位置继续
    python scripts/meme_import_bili.py --re 3 --asr-skipped  # 补跑此前「无字幕」的条目
    python scripts/meme_import_bili.py --no-asr          # 关闭本地语音识别

API Key 优先级：--api-key > OPENAI_API_KEY > DEEPSEEK_API_KEY > SILICONFLOW_API_KEY。
默认走 opencode 网关（--base-url/--model 可换）。

AI 总结出的梗若同名已在库中则跳过（不覆盖旧内容）。

进度：启动先翻页算出待处理总数，tqdm 进度条固定底部；
     AI 总结出的梗名/内容用 tqdm.write 在上方滚动输出。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import tempfile
import time
import uuid

# ====== 路径 =====================================================================
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)
_DEFAULT_DB = os.path.join(_REPO_ROOT, 'plugins', 'deepseek', 'db', 'meme.db')

# 由 use_db() 按实际 DB 路径填充：凭据/缓存/ASR 模型都跟着 DB 走
DB_PATH = ''
CRED_PATH = ''
CACHE_DIR = ''
ASR_MODEL_DIR = ''


def use_db(path: str) -> None:
    """切换输出库，并把它同目录作为凭据/缓存/模型的存放位置。"""
    global DB_PATH, CRED_PATH, CACHE_DIR, ASR_MODEL_DIR
    DB_PATH = os.path.abspath(os.path.expanduser(str(path)))
    base = os.path.dirname(DB_PATH) or '.'
    CRED_PATH = os.path.join(base, 'bili_credential.json')
    CACHE_DIR = os.path.join(base, 'meme_import_runs')
    ASR_MODEL_DIR = os.path.join(base, 'asr_models')


use_db(_DEFAULT_DB)

# ====== 配置（可用命令行 / 环境变量覆盖）============================================
OPENAI_BASE_URL = 'https://opencode.ai/zen/go/v1'
OPENAI_API_KEY = ''            # 留空则读环境变量；不要把真实 key 提交进仓库
OPENAI_MODEL = 'deepseek-v4.1-flash'      # 网关当前可用型号（-free 后缀已下线）
UP_UID = 94510621
# ==================================================================================

PAGE_SIZE = 30                  # 投稿列表每页条数
SUBTITLE_CHAR_LIMIT = 6000      # 喂给 AI 的字幕上限（超出截尾）
POLITE_SLEEP = 0.8              # 每个视频之间的礼貌间隔（秒）

# ── 无字幕兜底：faster-whisper 本地语音识别 ─────────────────────
# 视频没有 CC/AI 字幕时，下载音频轨到临时文件，用本地 whisper 转写后同样喂给 AI。
# 首次运行会从 HuggingFace 下载模型（small 约 460MB），缓存到 DB 同目录的 asr_models。
# 不需要时命令行加 --no-asr 关闭。
ASR_ENABLED = True
ASR_MODEL = 'small'             # tiny/base/small/medium/large-v3，或本地模型目录路径
ASR_DEVICE = 'cpu'              # 有 NVIDIA 显卡可改 'cuda'
ASR_COMPUTE_TYPE = 'int8'       # cuda 建议 'float16'
# HuggingFace 直连不通时用镜像；能直连可设 '' 走官方源。
# 必须在 huggingface_hub 首次 import 前设置（它在 import 时读环境变量）。
ASR_HF_ENDPOINT = 'https://hf-mirror.com'
if ASR_HF_ENDPOINT:
    os.environ.setdefault('HF_ENDPOINT', ASR_HF_ENDPOINT)

# opencode 系中转要求带会话头；进程内固定一个 id，便于在网关侧串联同一次导入
COPENCODE_SESSION = uuid.uuid4().hex


# ── meme.db 结构（与 plugins/deepseek/Core/meme_guide.py 的 schema v2 一致）────
# 这里内嵌一份，保证本工具脱离仓库/插件也能独立生成可被插件读取的库。
_DDL_V1 = [
    """CREATE TABLE IF NOT EXISTS memes (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL UNIQUE,
        pinyin     TEXT NOT NULL DEFAULT '',
        content    TEXT NOT NULL DEFAULT '',
        enabled    INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS meme_aliases (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        meme_id INTEGER NOT NULL REFERENCES memes(id) ON DELETE CASCADE,
        alias   TEXT NOT NULL,
        kind    TEXT NOT NULL DEFAULT 'cn',   -- cn=中文别名/缩写；py=拼音/英文等拉丁写法
        UNIQUE(meme_id, alias, kind)
    )""",
    'CREATE INDEX IF NOT EXISTS idx_meme_alias_kind ON meme_aliases(kind, alias)',
]


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def migrate(db_path: str | None = None) -> None:
    """建库/升级到 v2（v1 无 source_bv，v2 增加来源 BV）。幂等。"""
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    conn = _connect(path)
    try:
        ver = conn.execute('PRAGMA user_version').fetchone()[0]
        if ver == 0:
            for ddl in _DDL_V1:
                conn.execute(ddl)
            ver = 1
            conn.execute('PRAGMA user_version=1')
            conn.commit()
        if ver < 2:
            conn.execute(
                "ALTER TABLE memes ADD COLUMN source_bv TEXT NOT NULL DEFAULT ''")
            conn.execute('CREATE INDEX IF NOT EXISTS idx_memes_bv ON memes(source_bv)')
            conn.execute('PRAGMA user_version=2')
            conn.commit()
    finally:
        conn.close()


def _has_cjk(s: str) -> bool:
    return any('一' <= c <= '鿿' for c in (s or ''))


def upsert_meme(db_path: str, name: str, content: str, pinyin: str = '',
                aliases=None, enabled: bool = True, source_bv: str = '') -> int:
    """新增或按主名更新一条梗；别名整表重写（含 CJK 的归 cn，其余归 py）。"""
    name = str(name or '').strip()
    if not name:
        raise ValueError('梗名不能为空')
    migrate(db_path)
    now = time.time()
    conn = _connect(db_path)
    try:
        conn.execute(
            """INSERT INTO memes(name, pinyin, content, enabled, created_at, updated_at, source_bv)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   pinyin=excluded.pinyin, content=excluded.content,
                   enabled=excluded.enabled, source_bv=excluded.source_bv,
                   updated_at=excluded.updated_at""",
            (name, str(pinyin or ''), str(content or ''),
             1 if enabled else 0, now, now, str(source_bv or '').strip()))
        mid = conn.execute(
            'SELECT id FROM memes WHERE name=?', (name,)).fetchone()[0]
        conn.execute('DELETE FROM meme_aliases WHERE meme_id=?', (mid,))
        seen = {name}
        for alias in aliases or []:
            alias = str(alias or '').strip()
            if not alias or alias in seen:
                continue
            seen.add(alias)
            kind = 'cn' if _has_cjk(alias) else 'py'
            conn.execute(
                'INSERT OR IGNORE INTO meme_aliases(meme_id, alias, kind)'
                ' VALUES (?, ?, ?)', (mid, alias, kind))
        conn.commit()
        return mid
    finally:
        conn.close()


def meme_name_exists(db_path: str, name: str) -> bool:
    name = str(name or '').strip()
    if not name:
        return False
    conn = _connect(db_path)
    try:
        return conn.execute(
            'SELECT 1 FROM memes WHERE name=? LIMIT 1', (name,)
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


_SYSTEM_PROMPT = (
    '你是中文网络流行梗百科编辑。用户会给你一个 B 站视频的标题和字幕，'
    '该视频通常在讲解一个网络流行梗/黑话。请从中提取视频实际讲解的梗（通常 1 个，'
    '若同集讲了多个相互独立的梗可给多个；纯新闻/日常/与梗无关则返回空数组）。\n'
    '严格只输出 JSON 对象，不要输出任何解释或代码块标记，格式：\n'
    '{"memes":[{"name":"梗的标准中文名称","pinyin":"无声调小写拼音，音节空格分隔，'
    '如 nei juan；无把握给空字符串","aliases":["别名/缩写/英文名/常见错写"]，'
    '没有就给空数组,"content":"50到200字简要：含义、出处来源、典型用法，客观陈述"}]}\n'
    '要求：name 用群友最常用的写法；content 不要照抄字幕原文、不要加「视频里说」这类措辞；'
    'aliases 里不要重复 name。'
)


def log(msg: str) -> None:
    """走 tqdm.write，避免冲掉底部进度条。"""
    from tqdm import tqdm
    tqdm.write(msg)


# ── 登录 / 凭据持久化 ─────────────────────────────────────────────

def _save_credential(cred) -> None:
    os.makedirs(os.path.dirname(CRED_PATH), exist_ok=True)
    data = {k: getattr(cred, k, None) or ''
            for k in ('sessdata', 'bili_jct', 'buvid3', 'buvid4',
                      'dedeuserid', 'ac_time_value')}
    with open(CRED_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    try:
        os.chmod(CRED_PATH, 0o600)
    except OSError:
        pass


def _load_credential():
    if not os.path.exists(CRED_PATH):
        return None
    try:
        with open(CRED_PATH, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not data.get('sessdata'):
        return None
    from bilibili_api import Credential
    return Credential(**{k: v for k, v in data.items() if v})


async def _ensure_buvid(cred) -> None:
    """wbi 签名接口需要 buvid3；扫码凭据不带，缺了就现取并落盘。"""
    if getattr(cred, 'buvid3', None):
        return
    try:
        from bilibili_api import get_buvid
        cred.buvid3, cred.buvid4 = await get_buvid()
        _save_credential(cred)
    except Exception as e:
        log(f'⚠ 获取 buvid3 失败（不影响登录，部分接口可能 412）：{e}')


def _cookies_from_client() -> dict:
    """从 bilibili-api 使用的 HTTP 会话 CookieJar 里取登录下发的 Cookie。

    bilibili-api 17.4.2 的网页扫码在部分账号上解析不到 poll 返回 url 里的
    SESSDATA（get_credential() 拿到空值），但登录成功的 Set-Cookie 已经落在
    客户端会话的空 CookieJar 里，这里兜底捞一次。
    """
    try:
        from yarl import URL
        from bilibili_api.utils.network import get_client
        session = get_client().get_wrapped_session()
        jar = getattr(session, 'cookie_jar', None)
        if jar is None:
            return {}
        out: dict[str, str] = {}
        for host in ('https://www.bilibili.com',
                     'https://bilibili.com',
                     'https://passport.bilibili.com',
                     'https://passport.biligame.com'):
            for name, morsel in jar.filter_cookies(URL(host)).items():
                out.setdefault(name, morsel.value)
        return out
    except Exception:
        return {}


def _fill_credential(cred, cookies: dict):
    """会话 Cookie 里能补出 SESSDATA 就重建凭据，否则原样返回。"""
    sessdata = cookies.get('SESSDATA') or cookies.get('sessdata') or ''
    if not sessdata:
        return cred
    had_sessdata = bool(getattr(cred, 'sessdata', None))
    from bilibili_api import Credential
    cred = Credential(
        sessdata=sessdata,
        bili_jct=cookies.get('bili_jct') or getattr(cred, 'bili_jct', None),
        buvid3=cookies.get('buvid3') or getattr(cred, 'buvid3', None),
        buvid4=cookies.get('buvid4') or getattr(cred, 'buvid4', None),
        dedeuserid=(cookies.get('DedeUserID') or cookies.get('dedeuserid')
                    or getattr(cred, 'dedeuserid', None)),
        ac_time_value=getattr(cred, 'ac_time_value', None),
    )
    if not had_sessdata:
        log('✔ 已从登录会话 Cookie 补全 SESSDATA')
    return cred


def _make_web_login():
    """网页扫码登录（修正 bilibili-api 17.4.2 的 SESSDATA 解析）。

    库里的 WEB 分支把 poll 返回 url 的 query 按 `cookie[:9]` 硬切；部分账号
    返回的 url 根本不含 cookie 参数（或参数名大小写/编码不同），于是凭据里
    SESSDATA 是空串，后续 get_subtitle 抛 CredentialNoSessdataException。
    这里改用 parse_qs 解析，解析不出来就把 crossDomain url 真正访问一次，
    让服务端用 Set-Cookie 下发，再从会话 CookieJar 取。
    依赖库的私有属性 `__qr_key` / `__credential`，升级库后需复核。
    """
    from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginChannel

    class WebQrLogin(QrCodeLogin):
        def __init__(self):
            super().__init__(QrCodeLoginChannel.WEB)

        async def check_state(self):
            from urllib.parse import parse_qs, unquote, urlsplit
            from bilibili_api import Credential
            from bilibili_api.login_v2 import API, QrCodeLoginEvents
            from bilibili_api.utils.network import Api, get_session

            events = (await Api(credential=Credential(),
                                **API['qrcode']['web']['get_events'])
                      .update_params(qrcode_key=self._QrCodeLogin__qr_key)
                      .result)
            code = events['code']
            if code == 86101:
                return QrCodeLoginEvents.SCAN
            if code == 86090:
                return QrCodeLoginEvents.CONF
            if code == 86038:
                return QrCodeLoginEvents.TIMEOUT

            url = events.get('url') or ''
            if url and not url.startswith('http'):
                url = unquote(url)          # 少数情况下整个 url 被百分号编码
            fields: dict[str, str] = {}
            if '?' in url:
                fields = {k: v[0] for k, v in
                          parse_qs(urlsplit(url).query).items() if v}
            if not fields.get('SESSDATA'):
                if url:
                    # 访问 crossDomain，触发 Set-Cookie（不跟随跳转，只看响应头）
                    try:
                        async with get_session().get(
                                url, allow_redirects=False) as resp:
                            await resp.read()
                    except Exception:
                        pass
                fields.update({k: v for k, v in _cookies_from_client().items()
                               if k not in fields})

            cred = Credential(
                sessdata=fields.get('SESSDATA') or '',
                bili_jct=fields.get('bili_jct') or '',
                dedeuserid=fields.get('DedeUserID') or '',
                ac_time_value=events.get('refresh_token') or '',
                buvid3=fields.get('buvid3') or '',
                buvid4=fields.get('buvid4') or '',
            )
            self._QrCodeLogin__credential = cred
            return QrCodeLoginEvents.DONE

    return WebQrLogin()


async def _qr_login(channel: str):
    """终端二维码登录；channel = 'web' | 'tv'。"""
    from bilibili_api.login_v2 import (QrCodeLogin, QrCodeLoginChannel,
                                       QrCodeLoginEvents)
    qr = (_make_web_login() if channel == 'web'
          else QrCodeLogin(QrCodeLoginChannel.TV))
    await qr.generate_qrcode()
    print()
    print(qr.get_qrcode_terminal())
    log('请用「哔哩哔哩 App → 我的 → 右上角扫一扫」扫码登录'
        '（二维码图片另存于系统临时目录 qrcode.png）')
    last_state = None
    while not qr.has_done():
        state = await qr.check_state()
        if state != last_state:
            if state == QrCodeLoginEvents.SCAN:
                log('… 已扫码，请在手机上确认')
            elif state == QrCodeLoginEvents.CONF:
                log('… 已确认，登录中')
            elif state == QrCodeLoginEvents.TIMEOUT:
                log('⚠ 二维码超时，重新生成…')
                await qr.generate_qrcode()
                print(qr.get_qrcode_terminal())
            last_state = state
        await asyncio.sleep(2)
    return _fill_credential(qr.get_credential(), _cookies_from_client())


async def ensure_login():
    """有效凭据直接复用；否则终端打印二维码走扫码登录。"""
    from bilibili_api.user import User

    cred = _load_credential()
    if cred is not None:
        try:
            if await cred.check_valid():
                await _ensure_buvid(cred)
                try:
                    info = await User(uid=int(cred.dedeuserid or 0),
                                      credential=cred).get_user_info()
                    log(f'✔ 已登录 B 站：{info.get("uname") or info.get("name", "")}')
                except Exception:
                    log('✔ 凭据有效，已登录 B 站')
                return cred
        except Exception:
            pass
        log('⚠ 保存的登录凭据已失效，重新扫码登录…')

    cred = await _qr_login('web')
    if not getattr(cred, 'sessdata', None):
        log('⚠ 网页扫码没拿到 SESSDATA（bilibili-api 解析 poll 返回异常），改用 TV 扫码…')
        cred = await _qr_login('tv')
    if not getattr(cred, 'sessdata', None):
        raise SystemExit(
            '扫码登录成功，但拿不到 SESSDATA（字幕接口必须登录凭据）。\n'
            '请重新运行脚本再扫一次；若仍失败，检查账号是否被风控。')
    _save_credential(cred)
    await _ensure_buvid(cred)
    log(f'✔ 登录成功，凭据已保存到 {CRED_PATH}')
    return cred


# ── 投稿列表与区间规划 ────────────────────────────────────────────

def existing_bvs() -> set[str]:
    if not os.path.exists(DB_PATH):
        return set()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT source_bv FROM memes WHERE source_bv != ''").fetchall()
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()
    return {r[0] for r in rows}


async def plan_videos(cred, uid: int, start_bv: str | None,
                      end_bv: str | None) -> list[tuple[str, str]]:
    """翻页定位区间，返回待处理列表（含 start/end 两端），顺序新→旧。

    - start/end 都空：最新 → 遇到库内已有 BV 停（已有那条不处理）
    - 只给 start：从 start 起向旧扫，遇到库内已有 BV 停
    - 只给 end：最新 → end（含）
    - 都给：闭区间，不看数据库
    """
    db_stop = (start_bv is None and end_bv is None) or end_bv is None
    known = existing_bvs() if db_stop else set()

    collected: list[tuple[str, str]] = []
    from bilibili_api.user import User
    user = User(uid=uid, credential=cred)
    pn = 1
    started = start_bv is None
    hit_end = False
    while not hit_end:
        data = await user.get_videos(pn=pn, ps=PAGE_SIZE)
        vlist = (data.get('list') or {}).get('vlist') or []
        if not vlist:
            break
        for item in vlist:
            bvid, title = item.get('bvid') or '', item.get('title') or ''
            if not started:
                if bvid == start_bv:
                    started = True
                else:
                    continue
            if end_bv and bvid == end_bv:
                collected.append((bvid, title))
                hit_end = True
                break
            if bvid in known:
                log(f'⏸ 遇到库中已存在的视频 {bvid}，停止向前扫描')
                hit_end = True
                break
            collected.append((bvid, title))
        if not hit_end and len(vlist) < PAGE_SIZE:
            break
        pn += 1
        await asyncio.sleep(0.3)

    if start_bv and not started:
        raise SystemExit(f'起点 BV {start_bv} 不在 uid={uid} 的投稿列表中')
    if end_bv and not any(bv == end_bv for bv, _ in collected):
        raise SystemExit(f'终点 BV {end_bv} 不在 uid={uid} 的投稿列表中'
                         f'（应位于起点 {start_bv or "最新投稿"} 的更旧一侧）')
    return collected


# ── 字幕拉取 ──────────────────────────────────────────────────────

async def fetch_subtitle_text(cred, bvid: str) -> str:
    """取视频首选中文字幕（人工 CC 优先，其次 AI 自动字幕）的纯文本。"""
    import aiohttp
    from bilibili_api.video import Video

    video = Video(bvid=bvid, credential=cred)
    info = await video.get_info()
    cid = info.get('cid')
    if not cid:
        pages = info.get('pages') or await video.get_pages()
        if not pages:
            return ''
        cid = pages[0].get('cid')
    sub = await video.get_subtitle(cid=cid)
    subs = (sub or {}).get('subtitles') or []
    if not subs:
        return ''
    preference = ('zh-CN', 'zh-Hans', 'ai-zh', 'zh')
    # 注意：sort 期间 list 对 key 函数表现成空列表（CPython 就地排序的实现细节），
    # key 里写 len(subs) 会恒为 0，等于没排序，所以长度要先存下来。
    n_subs = len(subs)
    subs.sort(key=lambda s: next(
        (i for i, p in enumerate(preference) if (s.get('lan') or '').startswith(p)),
        n_subs))
    url = subs[0].get('subtitle_url') or ''
    if url.startswith('//'):
        url = 'https:' + url
    elif url.startswith('http'):
        pass
    else:
        url = 'https://' + url.lstrip('/')
    headers = {
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/124.0 Safari/537.36'),
        'Referer': f'https://www.bilibili.com/video/{bvid}/',
    }
    cookies = {'SESSDATA': cred.sessdata} if getattr(cred, 'sessdata', None) else {}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers, cookies=cookies) as resp:
            resp.raise_for_status()
            data = json.loads(await resp.text())
    lines = []
    for seg in data.get('body') or []:
        text = (seg.get('content') or '').strip()
        if text and (not lines or lines[-1] != text):
            lines.append(text)
    return '\n'.join(lines)


# ── 无字幕兜底：音频轨下载 + 本地 whisper ─────────────────────────

async def fetch_audio_file(cred, bvid: str) -> str:
    """把视频音频轨下载到临时文件，返回路径（调用方负责删除）。"""
    import aiohttp
    from bilibili_api.video import Video

    video = Video(bvid=bvid, credential=cred)
    info = await video.get_info()
    cid = info.get('cid')
    if not cid:
        pages = info.get('pages') or await video.get_pages()
        cid = pages[0].get('cid') if pages else None
    if not cid:
        raise RuntimeError('取不到 cid')

    data = await video.get_download_url(cid=cid)
    dash = data.get('dash') or {}
    tracks = list(dash.get('audio') or [])
    if not tracks and (dash.get('dolby') or {}).get('audio'):
        tracks = [dash['dolby']['audio']]
    if not tracks and (dash.get('flac') or {}).get('audio'):
        tracks = [dash['flac']['audio']]
    if tracks:
        # 普通 m4a 轨里带宽最高的一条；不用无损 flac（体积大、转写无收益）
        track = max(tracks, key=lambda a: a.get('bandwidth') or 0)
        url = track.get('baseUrl') or track.get('base_url') or track.get('url') or ''
        suffix = '.m4a'
    else:
        durl = data.get('durl') or []
        if not durl:
            raise RuntimeError('没有可下载的音轨（可能是充电专属/付费视频）')
        url = durl[0].get('url') or ''
        suffix = '.mp4'
    if not url:
        raise RuntimeError('音轨地址为空')

    headers = {
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/124.0 Safari/537.36'),
        'Referer': f'https://www.bilibili.com/video/{bvid}/',
    }
    fd, path = tempfile.mkstemp(prefix='meme_asr_', suffix=suffix)
    os.close(fd)
    try:
        timeout = aiohttp.ClientTimeout(total=600)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                with open(path, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        f.write(chunk)
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return path


_asr_model = None


def _get_asr_model():
    """懒加载并复用 whisper 模型（faster-whisper 未安装时抛 ImportError）。"""
    global _asr_model
    if _asr_model is None:
        from faster_whisper import WhisperModel
        os.makedirs(ASR_MODEL_DIR, exist_ok=True)
        log(f'⏳ 加载 ASR 模型 {ASR_MODEL}（首次运行会从 HuggingFace 下载）…')
        _asr_model = WhisperModel(ASR_MODEL, device=ASR_DEVICE,
                                  compute_type=ASR_COMPUTE_TYPE,
                                  download_root=ASR_MODEL_DIR)
    return _asr_model


def _decode_pcm(path: str):
    """ffmpeg 解码成 16kHz 单声道 float32 PCM。

    faster-whisper 自带的 PyAV 解码在 PyAV 19 上会因 metadata_errors 参数被移除而
    报 TypeError，所以优先自己解码、直接把 ndarray 交给模型（模型接受 ndarray）。
    返回 None 表示系统里没有 ffmpeg，退回让 faster-whisper 自己读文件。
    """
    import shutil
    import subprocess

    import numpy as np

    exe = shutil.which('ffmpeg')
    if not exe:
        return None
    proc = subprocess.run(
        [exe, '-nostdin', '-loglevel', 'error', '-i', path,
         '-f', 's16le', '-ac', '1', '-ar', '16000', 'pipe:1'],
        capture_output=True, check=True)
    return np.frombuffer(proc.stdout, np.int16).astype(np.float32) / 32768.0


def _asr_transcribe(path: str) -> str:
    """同步转写（在工作线程里跑）。segments 是惰性生成器，必须同线程消费完。"""
    audio = _decode_pcm(path)
    if audio is None:
        log('⚠ PATH 中找不到 ffmpeg，改用 PyAV 解码（PyAV 版本过新时可能失败）')
    segments, info = _get_asr_model().transcribe(
        path if audio is None else audio,
        language='zh', beam_size=5, vad_filter=True,
        initial_prompt='以下是B站「梗指南」视频的口播，包含网络流行语和梗。')
    log(f'   🎧 音频 {info.duration:.0f}s 转写中…')
    lines = []
    for seg in segments:
        text = (seg.text or '').strip()
        if text and (not lines or lines[-1] != text):
            lines.append(text)
    return '\n'.join(lines)


async def _asr_fallback(cred, bvid: str) -> str:
    """字幕缺失时的兜底：下载音频轨 → 本地 whisper 转写。任何失败返回 ''。"""
    path = ''
    try:
        path = await fetch_audio_file(cred, bvid)
        return await asyncio.to_thread(_asr_transcribe, path)
    except ImportError:
        log('⚠ 未安装 faster-whisper，跳过 ASR（pip install faster-whisper）')
        return ''
    except Exception as e:
        log(f'⚠ ASR 失败：{type(e).__name__}: {e}')
        return ''
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


# ── AI 提取梗 ─────────────────────────────────────────────────────

def _ai_extract(title: str, subtitle: str) -> list[dict]:
    """同步 OpenAI 调用（在工作线程里跑）；返回 [{name,pinyin,aliases,content}]。"""
    from openai import OpenAI

    api_key = (OPENAI_API_KEY
               or os.environ.get('OPENAI_API_KEY')
               or os.environ.get('DEEPSEEK_API_KEY')
               or os.environ.get('SILICONFLOW_API_KEY', ''))
    if not api_key:
        raise SystemExit(
            '未配置 API Key：用 --api-key 或设置环境变量 '
            'OPENAI_API_KEY / DEEPSEEK_API_KEY / SILICONFLOW_API_KEY')

    # 网关要求 x-opencode-session（无 c；与插件 Core/llm.py 一致）。
    # x-copencode-session 是早期笔误遗留，网关忽略未知头，保留以兼容旧文档。
    client = OpenAI(
        api_key=api_key, base_url=OPENAI_BASE_URL, timeout=120.0,
        default_headers={
            'x-opencode-session': COPENCODE_SESSION,
            'x-copencode-session': COPENCODE_SESSION,
        })
    user_prompt = f'视频标题：{title}\n\n视频字幕：\n{subtitle[:SUBTITLE_CHAR_LIMIT]}'
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0.3,
        messages=[{'role': 'system', 'content': _SYSTEM_PROMPT},
                  {'role': 'user', 'content': user_prompt}],
    )
    raw = (resp.choices[0].message.content or '').strip()
    if raw.startswith('```'):
        raw = raw.strip('`')
        raw = raw[raw.find('\n') + 1:] if '\n' in raw else raw
    data = json.loads(raw)
    memes = data.get('memes') if isinstance(data, dict) else data
    if not isinstance(memes, list):
        return []
    clean = []
    for m in memes:
        if not isinstance(m, dict):
            continue
        name = str(m.get('name') or '').strip()
        content = str(m.get('content') or '').strip()
        if not name or not content:
            continue
        pinyin = str(m.get('pinyin') or '').strip()
        aliases = m.get('aliases') or []
        if not isinstance(aliases, list):
            aliases = [aliases]
        aliases = [str(a).strip() for a in aliases if str(a).strip()]
        aliases = list(dict.fromkeys(a for a in aliases if a != name))
        clean.append({'name': name, 'content': content,
                      'pinyin': pinyin, 'aliases': aliases})
    return clean


# ── 缓存列表（规划结果落盘，中断可续跑）────────────────────────────

def _run_path(rid: int) -> str:
    return os.path.join(CACHE_DIR, f'run_{rid:03d}.json')


def list_run_ids() -> list[int]:
    if not os.path.isdir(CACHE_DIR):
        return []
    ids = []
    for fn in os.listdir(CACHE_DIR):
        if fn.startswith('run_') and fn.endswith('.json'):
            try:
                ids.append(int(fn[4:-5]))
            except ValueError:
                pass
    return sorted(ids)


def load_run(rid: int) -> dict:
    path = _run_path(rid)
    if not os.path.exists(path):
        raise SystemExit(f'缓存列表 #{rid} 不存在（用 list 查看现有列表）')
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def save_run(run: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = _run_path(run['id']) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(run, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _run_path(run['id']))


def new_run(uid: int, start_bv: str | None, end_bv: str | None,
            videos: list[tuple[str, str]]) -> dict:
    rid = (list_run_ids()[-1] + 1) if list_run_ids() else 1
    now = time.time()
    return {
        'id': rid,
        'uid': uid,
        'start_bv': start_bv or '',
        'end_bv': end_bv or '',
        'created_at': now,
        'updated_at': now,
        'finished_at': None,
        'videos': [{'bvid': bv, 'title': t, 'status': 'pending',
                    'note': '', 'saved': [], 'skipped_existing': [], 'ts': 0}
                   for bv, t in videos],
    }


def _fmt_ts(ts: float) -> str:
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(ts)) if ts else '-'


def _run_state(run: dict) -> tuple[int, int, int, int]:
    """返回 (已完成, 失败, 待处理, 总数)。done=成功/无字幕/无梗都算完成。"""
    vids = run['videos']
    done = sum(1 for v in vids if v.get('status') == 'done')
    failed = sum(1 for v in vids if v.get('status') == 'failed')
    return done, failed, len(vids) - done - failed, len(vids)


def cmd_list() -> None:
    ids = list_run_ids()
    if not ids:
        print('（没有缓存列表）')
        return
    print(f'共 {len(ids)} 个缓存列表（目录 {CACHE_DIR}）：')
    for rid in ids:
        run = load_run(rid)
        done, failed, pending, total = _run_state(run)
        state = '已完成' if run.get('finished_at') else ('进行中' if pending or failed else '已完成')
        head, tail = run['videos'][0], run['videos'][-1]
        print(f'  #{rid:<3} [{state}] {done}/{total}'
              f'{f" 失败{failed}" if failed else ""}  {_fmt_ts(run["created_at"])}')
        print(f'        {head["bvid"]}《{head["title"][:22]}》'
              f' → {tail["bvid"]}《{tail["title"][:22]}》  uid={run["uid"]}')


def cmd_check(rid: int) -> None:
    run = load_run(rid)
    done, failed, pending, total = _run_state(run)
    head, tail = run['videos'][0], run['videos'][-1]
    pct = (done * 100 // total) if total else 100
    saved_total = sum(len(v.get('saved') or []) for v in run['videos'])
    asr_n = sum(1 for v in run['videos'] if v.get('note') == 'ASR')
    nosub_n = sum(1 for v in run['videos']
                  if str(v.get('note') or '').startswith('无字幕'))
    print(f'缓存列表 #{rid}   uid={run["uid"]}')
    print(f'  创建时间：{_fmt_ts(run["created_at"])}    最后更新：{_fmt_ts(run["updated_at"])}')
    print(f'  区间参数：--from {run["start_bv"] or "（最新）"}  --end {run["end_bv"] or "（遇库停）"}')
    print(f'  起始位置：{head["bvid"]} 《{head["title"]}》')
    print(f'  结束位置：{tail["bvid"]} 《{tail["title"]}》')
    print(f'  完成数量：{done}/{total}（{pct}%）  失败 {failed}  待处理 {pending}'
          f'   累计入库梗 {saved_total} 条')
    print(f'  其中 ASR 转写 {asr_n} 条，无字幕跳过 {nosub_n} 条')
    print(f'  数据库：{DB_PATH}')
    last_done = None
    for v in run['videos']:
        if v.get('status') == 'done':
            last_done = v
    if last_done:
        print(f'  最后完成：{last_done["bvid"]} 《{last_done["title"]}》'
              f'（{_fmt_ts(last_done.get("ts", 0))}）')
        cont = next((i for i, v in enumerate(run['videos'])
                     if v.get('status') != 'done'), total)
        if cont < total:
            nxt = run['videos'][cont]
            print(f'  续跑起点：{nxt["bvid"]} 《{nxt["title"]}》（--re {rid}）')
    if run.get('finished_at'):
        print('  状态：已全部完成')


# ── 主流程 ────────────────────────────────────────────────────────

async def process_run(cred, run: dict) -> None:
    """按缓存列表从第一个未完成项开始处理，每个视频处理完即落盘。"""
    from tqdm import tqdm

    migrate(DB_PATH)
    vids = run['videos']
    start_idx = next((i for i, v in enumerate(vids) if v.get('status') != 'done'),
                     len(vids))
    if start_idx >= len(vids):
        log(f'缓存列表 #{run["id"]} 已全部完成，无需续跑。')
        return

    saved = skipped = failed = 0
    interrupted = False
    log(f'缓存列表 #{run["id"]}：从第 {start_idx + 1}/{len(vids)} 个继续 → '
        f'{vids[start_idx]["bvid"]}《{vids[start_idx]["title"][:24]}》')
    try:
        with tqdm(total=len(vids), initial=start_idx, desc='梗导入',
                  unit='video', ncols=88) as bar:
            for idx in range(start_idx, len(vids)):
                entry = vids[idx]
                bvid, title = entry['bvid'], entry['title']
                if entry.get('status') == 'done':
                    bar.update(1)      # 前有失败条目时，其后可能已完成：不重复处理
                    continue
                bar.set_postfix_str(bvid)
                try:
                    subtitle = await fetch_subtitle_text(cred, bvid)
                    src_note = ''
                    if not subtitle and ASR_ENABLED:
                        log(f'🎧 [{bvid}] 《{title}》无字幕，改走本地语音识别…')
                        subtitle = await _asr_fallback(cred, bvid)
                        src_note = 'ASR'
                    if not subtitle:
                        entry.update(status='done',
                                     note='无字幕' + ('且ASR失败' if ASR_ENABLED else ''),
                                     ts=time.time())
                        skipped += 1
                        log(f'⏭ [{bvid}] 《{title}》无字幕，跳过')
                    else:
                        memes = await asyncio.to_thread(_ai_extract, title, subtitle)
                        if not memes:
                            entry.update(status='done', note='未提取到梗', ts=time.time())
                            skipped += 1
                            log(f'∅ [{bvid}] 《{title}》未提取到梗')
                        else:
                            log('')
                            log(f'🎬 [{bvid}] 《{title}》')
                            names_saved, names_existing = [], []
                            for m in memes:
                                if meme_name_exists(DB_PATH, m['name']):
                                    names_existing.append(m['name'])
                                    log(f'↷ 梗「{m["name"]}」已在库中，跳过')
                                    continue
                                upsert_meme(DB_PATH, m['name'], m['content'],
                                            pinyin=m['pinyin'], aliases=m['aliases'],
                                            source_bv=bvid)
                                names_saved.append(m['name'])
                                saved += 1
                                alias_txt = (f'（别名：{"、".join(m["aliases"])}）'
                                             if m['aliases'] else '')
                                py_txt = f' [{m["pinyin"]}]' if m['pinyin'] else ''
                                log(f'   📌 {m["name"]}{py_txt}{alias_txt}')
                                log(f'      {m["content"]}')
                            entry.update(status='done', note=src_note,
                                         saved=names_saved,
                                         skipped_existing=names_existing,
                                         ts=time.time())
                except SystemExit:
                    raise
                except Exception as e:
                    failed += 1
                    entry.update(status='failed',
                                 note=f'{type(e).__name__}: {e}'[:300], ts=time.time())
                    log(f'⚠ [{bvid}] 《{title}》处理失败：{type(e).__name__}: {e}')
                finally:
                    run['updated_at'] = time.time()
                    if all(v.get('status') == 'done' for v in vids):
                        run['finished_at'] = time.time()
                    save_run(run)
                    bar.update(1)
                await asyncio.sleep(POLITE_SLEEP)
    except KeyboardInterrupt:
        interrupted = True
        log(f'\n⏹ 已中断，进度已保存；用 --re {run["id"]} 从断点继续。')

    done_n, failed_n, pending_n, total_n = _run_state(run)
    tail = '（中断）' if interrupted else ''
    log(f'\n本次：新入库梗 {saved} 条，跳过视频 {skipped} 个，失败 {failed} 个{tail}')
    log(f'列表 #{run["id"]} 总进度：完成 {done_n}/{total_n}'
        f'{f"，失败 {failed_n}" if failed_n else ""}{f"，待处理 {pending_n}" if pending_n else ""}')
    log(f'数据库：{DB_PATH}')


async def cmd_run(start_bv: str | None, end_bv: str | None, uid: int,
                  resume_id: int | None, asr_skipped: bool = False) -> None:
    cred = await ensure_login()
    if resume_id is not None:
        run = load_run(resume_id)
        log(f'续跑缓存列表 #{resume_id}（uid={run["uid"]}）')
        if asr_skipped:
            # 把此前因「无字幕」跳过的条目重新排队，用 ASR 再试一次
            n = 0
            for v in run.get('videos') or []:
                if v.get('status') == 'done' and \
                        str(v.get('note') or '').startswith('无字幕'):
                    v.update(status='pending', note='', ts=None)
                    n += 1
            if n:
                run['updated_at'] = time.time()
                run.pop('finished_at', None)
                save_run(run)
                log(f'已把 {n} 个「无字幕」条目重新排队（本次走 ASR 兜底）')
            else:
                log('没有需要补跑的「无字幕」条目。')
    else:
        log(f'正在拉取 uid={uid} 的投稿列表并计算待处理数量…')
        videos = await plan_videos(cred, uid, start_bv, end_bv)
        if not videos:
            log('没有需要导入的视频（区间为空，或最新视频之前都已入库）。')
            return
        run = new_run(uid, start_bv, end_bv, videos)
        save_run(run)
        log(f'已建立缓存列表 #{run["id"]}，共 {len(videos)} 个视频：'
            f'{videos[0][0]} → {videos[-1][0]}'
            f'（{videos[0][1][:24]} … {videos[-1][1][:24]}）')
    await process_run(cred, run)


def _check_deps() -> None:
    missing = []
    for mod, pip_name in (('bilibili_api', 'bilibili-api-python'),
                          ('aiohttp', 'aiohttp'),
                          ('openai', 'openai'),
                          ('tqdm', 'tqdm')):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pip_name)
    if missing:
        raise SystemExit('缺少依赖，请先安装：pip install ' + ' '.join(missing))


def main() -> None:
    global ASR_ENABLED, OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL
    ap = argparse.ArgumentParser(
        description='从 B 站 UP 主视频字幕提取梗，生成/更新 meme.db（独立工具）')
    ap.add_argument('command', nargs='?', choices=('list', 'check'),
                    help='list=查看缓存列表；check N=查看某列表进度')
    ap.add_argument('index', nargs='?', type=int, help='缓存列表编号（check 用）')
    ap.add_argument('--db', default=_DEFAULT_DB, metavar='PATH',
                    help=f'输出的 meme.db 路径（默认 {_DEFAULT_DB}）')
    ap.add_argument('--from', dest='start_bv', metavar='BV',
                    help='从该 BV 开始（含）；不填则从最新投稿开始')
    ap.add_argument('--end', dest='end_bv', metavar='BV',
                    help='到该 BV 结束（含）；不填且未给 --from 时，遇到库中已有 BV 停')
    ap.add_argument('--re', dest='resume', type=int, metavar='N',
                    help='从第 N 个缓存列表的结束位置继续获取')
    ap.add_argument('--uid', type=int, default=UP_UID, help=f'UP 主 uid（默认 {UP_UID}）')
    ap.add_argument('--api-key', default='', help='AI 接口 key（也可用环境变量）')
    ap.add_argument('--base-url', default=OPENAI_BASE_URL, help='AI 接口 base_url')
    ap.add_argument('--model', default=OPENAI_MODEL, help='AI 模型名')
    ap.add_argument('--no-asr', action='store_true',
                    help='无字幕时不走本地语音识别（faster-whisper）')
    ap.add_argument('--asr-skipped', action='store_true',
                    help='配合 --re：把缓存里此前「无字幕」跳过的条目重新跑 ASR')
    args = ap.parse_args()

    use_db(args.db)
    if args.api_key:
        OPENAI_API_KEY = args.api_key
    OPENAI_BASE_URL = args.base_url
    OPENAI_MODEL = args.model
    if args.no_asr:
        ASR_ENABLED = False

    if args.command == 'list':
        cmd_list()
        return
    if args.command == 'check':
        if args.index is None:
            ap.error('check 需要指定列表编号，例如：check 3')
        cmd_check(args.index)
        return
    _check_deps()
    asyncio.run(cmd_run(args.start_bv, args.end_bv, args.uid, args.resume,
                        asr_skipped=args.asr_skipped))


if __name__ == '__main__':
    main()
