# -*- coding: utf-8 -*-
"""B 站凭据与网页扫码登录（M3）。

凭据文件与 scripts/meme_import_bili.py、scripts/bili_live_ai.py 共用：
plugins/deepseek/db/bili_credential.json（0600、gitignored）。

本模块所有 bilibili_api / qrcode 依赖一律延迟导入——没装库时插件与 Web
面板照常工作，只有直播页的扫码功能报「缺依赖」。

注意：网页扫码的 SESSDATA 兜底逻辑复制自 scripts/meme_import_bili.py
（17.4.2 的 poll 解析在部分账号拿不到 SESSDATA，需从会话 CookieJar 捞），
两边改的时候要同步。
"""
from __future__ import annotations

import base64
import io
import json
import os
import secrets
import time

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRED_PATH = os.path.join(_PLUGIN_DIR, 'db', 'bili_credential.json')

# sid -> {'login': obj, 'ts': float, 'state': str}
_QR_SESSIONS: dict[str, dict] = {}
_QR_TTL = 240.0

# check_valid 结果缓存（避免面板每几秒刷新就打一次 B 站接口）
_valid_cache: dict = {'ts': 0.0, 'uid': '', 'uname': '', 'ok': False}
_VALID_CACHE_SEC = 60.0


# ── 凭据文件 ───────────────────────────────────────────────────

def save_credential(cred) -> None:
    os.makedirs(os.path.dirname(CRED_PATH), exist_ok=True)
    data = {k: getattr(cred, k, None) or ''
            for k in ('sessdata', 'bili_jct', 'buvid3', 'buvid4',
                      'dedeuserid', 'ac_time_value')}
    tmp = CRED_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CRED_PATH)
    try:
        os.chmod(CRED_PATH, 0o600)
    except OSError:
        pass


def load_credential():
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


def delete_credential() -> bool:
    try:
        os.remove(CRED_PATH)
        return True
    except FileNotFoundError:
        return False


async def ensure_buvid(cred) -> None:
    """wbi 签名接口需要 buvid3；扫码凭据不带，缺了就现取并落盘。"""
    if getattr(cred, 'buvid3', None):
        return
    from bilibili_api import get_buvid
    cred.buvid3, cred.buvid4 = await get_buvid()
    save_credential(cred)


def credential_mtime() -> float:
    try:
        return os.path.getmtime(CRED_PATH)
    except OSError:
        return 0.0


def credential_snapshot() -> dict:
    """不触网的文件级状态（面板轮询用）。"""
    cred = load_credential()
    if cred is None:
        return {'logged_in': False}
    return {
        'logged_in': True,
        'uid': str(getattr(cred, 'dedeuserid', '') or ''),
        'mtime': credential_mtime(),
    }


async def validate(force: bool = False) -> dict:
    """触网校验 + 取昵称，60s 缓存；凭据文件更新后缓存自动失效。"""
    mt = credential_mtime()
    now = time.time()
    if (not force and _valid_cache['ts']
            and now - _valid_cache['ts'] < _VALID_CACHE_SEC
            and _valid_cache.get('mtime') == mt
            and _valid_cache.get('uid')):
        return dict(_valid_cache)
    out = {'logged_in': False, 'uid': '', 'uname': '',
           'valid': False, 'mtime': mt}
    cred = load_credential()
    if cred is not None:
        try:
            ok = bool(await cred.check_valid())
        except Exception:
            ok = False
        out['logged_in'] = True
        out['valid'] = ok
        out['uid'] = str(getattr(cred, 'dedeuserid', '') or '')
        if ok:
            try:
                from bilibili_api.user import User
                info = await User(uid=int(cred.dedeuserid or 0),
                                  credential=cred).get_user_info()
                out['uname'] = info.get('uname') or info.get('name') or ''
            except Exception:
                pass
    _valid_cache.clear()
    _valid_cache.update({'ts': now, 'mtime': mt, **out})
    return out


# ── 网页扫码（复制并修正自 scripts/meme_import_bili.py）────────────

def _cookies_from_client() -> dict:
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
    sessdata = cookies.get('SESSDATA') or cookies.get('sessdata') or ''
    if not sessdata:
        return cred
    from bilibili_api import Credential
    return Credential(
        sessdata=sessdata,
        bili_jct=cookies.get('bili_jct') or getattr(cred, 'bili_jct', None),
        buvid3=cookies.get('buvid3') or getattr(cred, 'buvid3', None),
        buvid4=cookies.get('buvid4') or getattr(cred, 'buvid4', None),
        dedeuserid=(cookies.get('DedeUserID') or cookies.get('dedeuserid')
                    or getattr(cred, 'dedeuserid', None)),
        ac_time_value=getattr(cred, 'ac_time_value', None),
    )


def _make_web_login():
    """网页扫码登录（修正 bilibili-api 17.4.2 的 SESSDATA 解析）。"""
    from bilibili_api import Credential
    from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginChannel
    from bilibili_api.login_v2 import API, QrCodeLoginEvents
    from bilibili_api.utils.network import Api, get_session
    from urllib.parse import parse_qs, unquote, urlsplit

    class WebQrLogin(QrCodeLogin):
        def __init__(self):
            super().__init__(QrCodeLoginChannel.WEB)

        async def check_state(self):
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
                url = unquote(url)
            fields: dict[str, str] = {}
            if '?' in url:
                fields = {k: v[0] for k, v in
                          parse_qs(urlsplit(url).query).items() if v}
            if not fields.get('SESSDATA'):
                if url:
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


def _gc_sessions() -> None:
    now = time.time()
    for sid in [s for s, v in _QR_SESSIONS.items()
                if now - v['ts'] > _QR_TTL]:
        _QR_SESSIONS.pop(sid, None)


async def qr_start() -> dict:
    """生成扫码会话：{sid, url, png_b64}。二维码 PNG 服务端现画。"""
    login = _make_web_login()
    await login.generate_qrcode()
    url = login._QrCodeLogin__qr_link
    if not url:
        raise RuntimeError('B 站未返回二维码内容')
    import qrcode
    buf = io.BytesIO()
    qrcode.make(url).save(buf, format='PNG')
    png = base64.b64encode(buf.getvalue()).decode('ascii')
    sid = secrets.token_hex(8)
    _gc_sessions()
    _QR_SESSIONS[sid] = {'login': login, 'ts': time.time(), 'state': 'new'}
    return {'sid': sid, 'url': url, 'png_b64': png}


async def qr_poll(sid: str) -> dict:
    """轮询扫码状态：waiting/scan/conf/timeout/done。done 即落凭据文件。"""
    _gc_sessions()
    sess = _QR_SESSIONS.get(sid)
    if sess is None:
        return {'state': 'expired'}
    if time.time() - sess['ts'] > _QR_TTL:
        _QR_SESSIONS.pop(sid, None)
        return {'state': 'expired'}
    login = sess['login']
    ev = await login.check_state()
    state = getattr(ev, 'name', str(ev)).lower()
    # 库里枚举名 SCAN/CONF/TIMEOUT/DONE
    state = {'scan': 'scan', 'conf': 'conf',
             'timeout': 'timeout', 'done': 'done'}.get(state, 'waiting')
    sess['state'] = state
    if state == 'done':
        _QR_SESSIONS.pop(sid, None)
        cred = _fill_credential(login.get_credential(),
                                _cookies_from_client())
        if not getattr(cred, 'sessdata', None):
            return {'state': 'error', 'error': '登录成功但没拿到 SESSDATA'}
        try:
            await ensure_buvid(cred)
        except Exception:
            pass
        save_credential(cred)
        _valid_cache['ts'] = 0.0
        uname, uid = '', str(getattr(cred, 'dedeuserid', '') or '')
        try:
            from bilibili_api.user import User
            info = await User(uid=int(uid or 0),
                              credential=cred).get_user_info()
            uname = info.get('uname') or info.get('name') or ''
        except Exception:
            pass
        return {'state': 'done', 'uid': uid, 'uname': uname}
    if state == 'timeout':
        _QR_SESSIONS.pop(sid, None)
    return {'state': state}
