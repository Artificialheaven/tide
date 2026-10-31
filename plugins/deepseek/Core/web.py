"""Web 后台 Blueprint：5 页 + REST。

蓝图名每次实例化取 uuid（框架每次重连都向同一 Flask app 重复注册）；
路由内不闭包插件实例，运行期经 get_plugin_manager 取活实例。
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid

import flask

from . import prompts as prompts_mod
from . import storage
from .version import VERSION

_PROMPT_MAX_CHARS = 8000

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB = os.path.join(_PLUGIN_DIR, 'db', 'deepseek.db')
_MASK = '********'


def _plugin():
    from utils.plugin_loader import get_plugin_manager
    pm = get_plugin_manager()
    return pm.plugins.get('deepseek') if pm is not None else None


def _eff_cfg(plugin, group_row):
    from .config import resolve_group
    return resolve_group(plugin.cfg, (group_row or {}).get('settings'))


def _run_async(coro, timeout: float = 30.0):
    from core import globals
    loop = getattr(globals, 'event_loop', None)
    if loop is None or not loop.is_running():
        return None, ('事件循环未运行', 503)
    try:
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout), None
    except Exception as e:
        return None, (str(e), 500)


def build_blueprint(plugin) -> flask.Blueprint:
    bp = flask.Blueprint(f'deepseek_{uuid.uuid4().hex[:8]}', __name__,
                         template_folder=os.path.join(_PLUGIN_DIR, 'templates'))

    # 框架的 Flask app 是模块级长生命周期单例，debug 关闭时 Jinja 默认不热重载，
    # 插件模板更新后不重启主进程就会一直渲染旧页面（旧 JS 与新 DOM 对不上会报错）。
    # 这里按 mtime 打开自动重载；HTML 页面同时禁浏览器缓存。
    @bp.before_request
    def _enable_template_auto_reload():
        flask.current_app.jinja_env.auto_reload = True

    @bp.context_processor
    def _inject_version():
        return {'PLUGIN_VERSION': VERSION}

    @bp.after_request
    def _no_cache_html(resp):
        resp.headers['X-Deepseek-Version'] = VERSION
        ctype = resp.headers.get('Content-Type', '')
        if 'text/html' in ctype:
            # 页面 URL 另挂 ?v=VERSION 双保险：旧版 HTML 被浏览器缓存时，
            # 新版导航链接的 v 不同也会强制重新拉取
            resp.headers['Cache-Control'] = 'no-store'
        return resp

    def db():
        return plugin.db_path if plugin else _DB

    # ── 页面 ───────────────────────────────────────────────────

    @bp.route('/')
    def index():
        return flask.redirect(
            flask.url_for(f'{bp.name}.dashboard', v=VERSION))

    @bp.route('/dashboard')
    def dashboard():
        return flask.render_template('dashboard.html')

    @bp.route('/settings')
    def settings_page():
        return flask.render_template('settings.html')

    @bp.route('/group')
    def group_page():
        return flask.render_template('group.html')

    @bp.route('/mcp')
    def mcp_page():
        return flask.render_template('mcp.html')

    @bp.route('/memory')
    def memory_page():
        return flask.render_template('memory.html')

    @bp.route('/runs')
    def runs_page():
        return flask.render_template('runs.html')

    @bp.route('/prompts')
    def prompts_page():
        return flask.render_template('prompts.html')

    # ── 后台运行记录（定时苏醒 / 定时任务）────────────────────

    @bp.route('/api/runs', methods=['GET'])
    def api_runs():
        try:
            limit = int(flask.request.args.get('limit', 50))
        except (TypeError, ValueError):
            limit = 50
        target = (flask.request.args.get('target') or '').strip()
        rows = storage.list_bg_runs(db(), limit=limit, target_id=target)
        return flask.jsonify({'ok': True, 'runs': rows})

    @bp.route('/api/runs/<int:rid>', methods=['GET'])
    def api_run_detail(rid):
        row = storage.get_bg_run(db(), rid)
        if not row:
            return flask.jsonify({'ok': False, 'error': '记录不存在'}), 404
        return flask.jsonify({'ok': True, 'run': row})

    # ── AI 自管轮询设定列表（查询用；增删改由 AI 在群里用工具完成）──

    @bp.route('/api/polls', methods=['GET'])
    def api_polls():
        target = (flask.request.args.get('target') or '').strip()
        rows = storage.list_polls(db(), group_id=target)
        return flask.jsonify({'ok': True, 'polls': rows})

    # ── 外部监测程序 watcher ──────────────────────────────────

    @bp.route('/api/watchers', methods=['GET'])
    def api_watchers():
        p = _plugin()
        if p is None:
            return flask.jsonify({'ok': False, 'error': '插件未加载'}), 503
        target = (flask.request.args.get('target') or '').strip()
        rows, err = _run_async(p.watchers.list_watchers(target))
        if err:
            return flask.jsonify({'ok': False, 'error': err[0]}), err[1]
        return flask.jsonify({'ok': True, 'watchers': rows})

    @bp.route('/api/watchers/<int:wid>/stop', methods=['POST'])
    def api_watcher_stop(wid):
        p = _plugin()
        if p is None:
            return flask.jsonify({'ok': False, 'error': '插件未加载'}), 503
        msg, err = _run_async(p.watchers.stop_watcher(wid))
        if err:
            return flask.jsonify({'ok': False, 'error': err[0]}), err[1]
        return flask.jsonify({'ok': True, 'message': msg})

    @bp.route('/api/watchers/<int:wid>/logs', methods=['GET'])
    def api_watcher_logs(wid):
        p = _plugin()
        if p is None:
            return flask.jsonify({'ok': False, 'error': '插件未加载'}), 503
        text, err = _run_async(p.watchers.logs(wid))
        if err:
            return flask.jsonify({'ok': False, 'error': err[0]}), err[1]
        return flask.jsonify({'ok': True, 'logs': text})

    # ── 外部触发服务信息 / token 轮换 ─────────────────────────

    @bp.route('/api/trigger-info', methods=['GET'])
    def api_trigger_info():
        p = _plugin()
        if p is None:
            return flask.jsonify({'ok': False, 'error': '插件未加载'}), 503
        cfg = p.cfg
        return flask.jsonify({'ok': True, 'info': {
            'on': bool(cfg.get('trigger_on', True)),
            'host': cfg.get('trigger_host', '127.0.0.1'),
            'port': cfg.get('trigger_port', 8094),
            'url': p.triggers.address(),
            'running': p.triggers.is_running(),
            # 与 MCP 头/token 同口径：本页已有登录鉴权，明文展示便于配置外部程序
            'token': cfg.get('trigger_token', ''),
            'cooldown': cfg.get('trigger_cooldown_seconds', 30),
        }})

    @bp.route('/api/trigger-token/regenerate', methods=['POST'])
    def api_trigger_token_regen():
        import secrets
        p = _plugin()
        if p is None:
            return flask.jsonify({'ok': False, 'error': '插件未加载'}), 503
        token = secrets.token_hex(16)
        raw = p.get_config()
        raw['trigger_token'] = token
        p.reload_cfg(raw)              # 触发服务每请求实时读 cfg，无需重启
        return flask.jsonify({'ok': True, 'token': token})

    # ── 实时订阅 / 弹幕机服务信息 ─────────────────────────────

    @bp.route('/api/live-info', methods=['GET'])
    def api_live_info():
        p = _plugin()
        if p is None:
            return flask.jsonify({'ok': False, 'error': '插件未加载'}), 503
        cfg = p.cfg
        live = getattr(p, 'live', None)
        running = bool(live and live.is_running())
        urls = live.page_urls() if live else {}
        return flask.jsonify({'ok': True, 'info': {
            'on': bool(cfg.get('live_on', True)),
            'host': cfg.get('live_host', '127.0.0.1'),
            'port': cfg.get('live_port', 8095),
            'running': running,
            # 本页已有登录鉴权，明文展示便于直接拼 URL（SSE token 走 query）
            'token': cfg.get('trigger_token', ''),
            'danmaku': urls.get('danmaku', ''),
            'debug': urls.get('debug', ''),
            'subscribers': live.bus.count() if live else 0,
        }})

    # ── 全局设置 ───────────────────────────────────────────────

    @bp.route('/api/settings', methods=['GET'])
    def api_get_settings():
        p = _plugin()
        cfg = dict(p.cfg if p else storage_placeholder_cfg())
        # 密钥只存在于服务商条目内，出 API 一律脱敏
        cfg['llm_providers'] = _masked_providers(cfg)
        cfg.pop('api_key', None)
        return flask.jsonify({'ok': True, 'settings': cfg})

    @bp.route('/api/settings', methods=['PUT'])
    def api_put_settings():
        p = _plugin()
        if p is None:
            return flask.jsonify({'ok': False, 'error': '插件未加载'}), 503
        data = flask.request.get_json(silent=True) or {}
        # 服务商/密钥只走专用端点；旧字段 api_key/base_url 不再接受
        data.pop('llm_providers', None)
        data.pop('api_key', None)
        data.pop('base_url', None)
        # 以内存全量配置为底（磁盘可能刚被框架弹窗覆写成字段子集）
        raw = dict(getattr(p, '_raw_config', p.get_config()))
        raw.update(data)
        p.reload_cfg(raw)
        return flask.jsonify({'ok': True})

    # ── 模型服务商（共享 base_url + api_key 的多模型路由）────────

    @bp.route('/api/llm-providers', methods=['GET'])
    def api_get_providers():
        p = _plugin()
        cfg = p.cfg if p else storage_placeholder_cfg()
        return flask.jsonify(
            {'ok': True, 'providers': _masked_providers(cfg)})

    @bp.route('/api/llm-providers', methods=['PUT'])
    def api_put_providers():
        p = _plugin()
        if p is None:
            return flask.jsonify({'ok': False, 'error': '插件未加载'}), 503
        from . import config as cfg_mod
        payload = flask.request.get_json(silent=True) or {}
        incoming = payload.get('providers')
        if not isinstance(incoming, list):
            return flask.jsonify(
                {'ok': False, 'error': 'providers 必须是列表'}), 400
        old = {pr.get('id'): pr for pr in (p.cfg.get('llm_providers') or [])
               if isinstance(pr, dict)}
        reconciled = []
        for e in incoming:
            if not isinstance(e, dict):
                continue
            e = dict(e)
            pid = str(e.get('id') or '').strip()
            key = str(e.get('api_key') or '')
            prev = old.get(pid) if pid else None
            # 空值/掩码 = 沿用已存储密钥（id 改名视为新服务商，不带密钥）
            if (not key.strip() or _MASK in key) and prev is not None:
                e['api_key'] = prev.get('api_key', '')
            reconciled.append(e)
        raw = dict(getattr(p, '_raw_config', p.get_config()))
        raw['llm_providers'] = reconciled
        merged = cfg_mod.merge_config(raw)
        # merge_config 会把失效路由静默重置，所以拿「当前在用的路由值」对照
        # 新服务商集做悬空检测，命中就拒绝整次保存
        probe = dict(merged)
        for k in cfg_mod.ROUTE_KEYS:
            probe[k] = p.cfg.get(k)
        dangling = cfg_mod.find_dangling_routes(
            probe, storage.list_groups(db()))
        if dangling:
            detail = '；'.join(f'{label}={v}' for label, _k, v in dangling[:8])
            return flask.jsonify(
                {'ok': False,
                 'error': f'保存后这些模型引用会失效，请先改路由：{detail}'}), 400
        p.reload_cfg(raw)
        return flask.jsonify(
            {'ok': True, 'providers': _masked_providers(p.cfg)})

    # ── 群 ─────────────────────────────────────────────────────

    @bp.route('/api/groups', methods=['GET'])
    def api_groups():
        groups = storage.list_groups(db())
        for g in groups:
            g['usage'] = storage.usage_summary(db(), g['group_id'], limit_rows=200)
        return flask.jsonify({'ok': True, 'groups': groups})

    @bp.route('/api/groups/<gid>', methods=['GET'])
    def api_get_group(gid):
        g = storage.get_group(db(), gid)
        if not g:
            return flask.jsonify({'ok': False, 'error': '群不存在'}), 404
        g['usage'] = storage.usage_summary(db(), gid, limit_rows=500)
        g['tools'] = storage.get_tool_overrides(db(), gid)
        return flask.jsonify({'ok': True, 'group': g})

    @bp.route('/api/groups/<gid>', methods=['PUT'])
    def api_put_group(gid):
        data = flask.request.get_json(silent=True) or {}
        storage.get_or_create_group(db(), gid)
        kwargs = {}
        for k in ('enabled', 'persona', 'aliases'):
            if k in data:
                kwargs[k] = data[k]
        if 'settings' in data and isinstance(data['settings'], dict):
            kwargs['settings'] = data['settings']
        g = storage.upsert_group(db(), gid, **kwargs)
        return flask.jsonify({'ok': True, 'group': g})

    @bp.route('/api/groups/<gid>/toggle', methods=['POST'])
    def api_toggle(gid):
        g = storage.get_or_create_group(db(), gid)
        g = storage.upsert_group(db(), gid, enabled=not g['enabled'])
        return flask.jsonify({'ok': True, 'enabled': g['enabled']})

    @bp.route('/api/groups/<gid>/reset-memory', methods=['POST'])
    def api_reset(gid):
        storage.reset_group_memory(db(), gid)
        return flask.jsonify({'ok': True})

    @bp.route('/api/groups/<gid>/summarize-now', methods=['POST'])
    def api_summarize_now(gid):
        p = _plugin()
        if p is None:
            return flask.jsonify({'ok': False, 'error': '插件未加载'}), 503
        coro = p.memory.force_extract(gid, p.cfg, p.llm)
        _, err = _run_async(coroutine, timeout=90)
        if err:
            return flask.jsonify({'ok': False, 'error': err[0]}), err[1]
        return flask.jsonify({'ok': True})

    # ── 群体风格画像 ─────────────────────────────────────────

    @bp.route('/api/groups/<gid>/style', methods=['GET', 'PUT', 'DELETE'])
    def api_group_style(gid):
        if flask.request.method == 'GET':
            row = storage.get_style(db(), gid) or {}
            n = storage.count_raw_after(db(), gid, row.get('up_to_id', 0))
            return flask.jsonify(
                {'ok': True, 'style': row.get('style_text', ''),
                 'sample_count': row.get('sample_count', 0),
                 'built_at': row.get('built_at', 0), 'new_msgs': n})
        p = _plugin()
        if p is None:
            return flask.jsonify({'ok': False, 'error': '插件未加载'}), 503
        if flask.request.method == 'DELETE':
            storage.clear_style(db(), gid)
            p.style.invalidate(gid)
            return flask.jsonify({'ok': True})
        data = flask.request.get_json(silent=True) or {}
        text = str(data.get('style_text', '') or '').strip()
        row = storage.get_style(db(), gid) or {}
        storage.set_style(db(), gid, text, int(row.get('up_to_id', 0) or 0),
                          int(row.get('sample_count', 0) or 0))
        p.style.invalidate(gid)
        return flask.jsonify({'ok': True})

    @bp.route('/api/groups/<gid>/style/build-now', methods=['POST'])
    def api_style_build(gid):
        p = _plugin()
        if p is None:
            return flask.jsonify({'ok': False, 'error': '插件未加载'}), 503
        g = storage.get_or_create_group(db(), gid)
        cfg = _eff_cfg(p, g)
        _, err = _run_async(p.style.force_build(gid, cfg, p.llm), timeout=120)
        if err:
            return flask.jsonify({'ok': False, 'error': err[0]}), err[1]
        return flask.jsonify({'ok': True, 'style': p.style.get_text(gid)})

    @bp.route('/api/groups/<gid>/raw-corpus', methods=['DELETE'])
    def api_raw_clear(gid):
        storage.clear_raw(db(), gid)
        return flask.jsonify({'ok': True})

    # ── 约定/待办 ─────────────────────────────────────────────

    @bp.route('/api/groups/<gid>/commitments', methods=['GET', 'POST'])
    def api_group_commitments(gid):
        if flask.request.method == 'GET':
            include_done = flask.request.args.get('all') == '1'
            rows = storage.list_commitments(
                db(), gid, '' if include_done else 'open', 200)
            return flask.jsonify({'ok': True, 'commitments': rows})
        d = flask.request.get_json(silent=True) or {}
        content = str(d.get('content', '')).strip()
        owner = str(d.get('owner_name', '')).strip()
        if not content or not owner:
            return flask.jsonify({'ok': False,
                'error': 'content 和 owner_name 必填'}), 400
        from .tasks import parse_due
        due_date = str(d.get('due_date', '') or '').strip()
        cid = storage.add_commitment(
            db(), gid, str(d.get('owner_id', '') or ''), owner, content,
            'topic' if d.get('kind') == 'topic' else 'task',
            str(d.get('due_text', '') or '')[:40], parse_due(due_date), 'web')
        return flask.jsonify({'ok': True, 'id': cid})

    @bp.route('/api/commitments/<int:cid>', methods=['PUT', 'DELETE'])
    def api_commitment_item(cid):
        if flask.request.method == 'DELETE':
            return flask.jsonify(
                {'ok': storage.set_commitment_status(db(), cid, 'cancelled')})
        d = flask.request.get_json(silent=True) or {}
        status = str(d.get('status', '')).strip()
        if status not in ('open', 'done', 'cancelled'):
            return flask.jsonify({'ok': False, 'error': 'status 非法'}), 400
        return flask.jsonify(
            {'ok': storage.set_commitment_status(db(), cid, status)})

    @bp.route('/api/groups/<gid>/tools', methods=['GET', 'PUT'])
    def api_group_tools(gid):
        if flask.request.method == 'GET':
            return flask.jsonify(
                {'ok': True, 'tools': storage.get_tool_overrides(db(), gid)})
        data = flask.request.get_json(silent=True) or {}
        items = data.get('tools') or {}
        for key, on in items.items():
            # 自由 key（内置点号 / custom__ / mcp__sid__tool），只挡畸形输入
            if not isinstance(key, str) or not key or len(key) > 128:
                continue
            storage.set_tool_override(db(), gid, key, bool(on))
        return flask.jsonify({'ok': True})

    @bp.route('/api/groups/<gid>/usage', methods=['GET'])
    def api_usage(gid):
        # days=0 表示全部历史；默认近 30 天（SQL 侧全量聚合）
        try:
            days = float(flask.request.args.get('days', 30))
        except (TypeError, ValueError):
            days = 30.0
        days = max(0.0, min(days, 3650.0))
        since = time.time() - days * 86400 if days else 0
        return flask.jsonify({
            'ok': True,
            'usage': storage.usage_summary(db(), gid),
            'days': days,
            'breakdown': storage.usage_breakdown(db(), gid, since)})

    # ── 心情（每群一份）────────────────────────────────────────

    @bp.route('/api/groups/<gid>/mood', methods=['GET'])
    def api_group_mood(gid):
        p = _plugin()
        if p is None:
            return flask.jsonify({'ok': False, 'error': '插件未加载'}), 503
        if not storage.get_group(db(), gid):
            return flask.jsonify({'ok': False, 'error': '群不存在'}), 404
        try:
            limit = int(flask.request.args.get('limit', 30))
        except (TypeError, ValueError):
            limit = 30
        cfg = _eff_cfg(p, storage.get_group(db(), gid))
        data = p.mood.view(gid, cfg, limit=limit)
        return flask.jsonify({'ok': True, **data})

    @bp.route('/api/groups/<gid>/mood/reset', methods=['POST'])
    def api_group_mood_reset(gid):
        p = _plugin()
        if p is None:
            return flask.jsonify({'ok': False, 'error': '插件未加载'}), 503
        cfg = _eff_cfg(p, storage.get_or_create_group(db(), gid))
        state = p.mood.reset(gid, cfg)
        return flask.jsonify({'ok': True, 'state': state})

    # ── MCP ────────────────────────────────────────────────────

    @bp.route('/api/mcp', methods=['GET', 'POST'])
    def api_mcp_list_create():
        if flask.request.method == 'GET':
            return flask.jsonify(
                {'ok': True, 'servers': storage.list_mcp_servers(db())})
        d = flask.request.get_json(silent=True) or {}
        sid = str(d.get('id', '')).strip()
        if not sid or not sid.replace('-', '').replace('_', '').isalnum():
            return flask.jsonify({'ok': False,
                'error': 'id 仅允许字母数字-_'}), 400
        row = storage.upsert_mcp_server(
            db(), sid, str(d.get('name', sid)), str(d.get('kind', 'stdio')),
            d.get('config') or {}, bool(d.get('enabled', False)))
        return flask.jsonify({'ok': True, 'server': row})

    @bp.route('/api/mcp/<sid>', methods=['PUT', 'DELETE'])
    def api_mcp_update(sid):
        if flask.request.method == 'DELETE':
            p = _plugin()
            if p:
                _, _ = _run_async(p.mcp.stop(sid), timeout=8)
            ok = storage.delete_mcp_server(db(), sid)
            return flask.jsonify({'ok': ok})
        d = flask.request.get_json(silent=True) or {}
        row = storage.upsert_mcp_server(
            db(), sid, str(d.get('name', sid)), str(d.get('kind', 'stdio')),
            d.get('config') or {}, bool(d.get('enabled', False)))
        return flask.jsonify({'ok': True, 'server': row})

    @bp.route('/api/mcp/<sid>/test', methods=['POST'])
    def api_mcp_test(sid):
        p = _plugin()
        if p is None:
            return flask.jsonify({'ok': False, 'error': '插件未加载'}), 503
        res, err = _run_async(p.mcp.test_server(sid), timeout=35)
        if err:
            return flask.jsonify({'ok': False, 'error': err[0]}), err[1]
        return flask.jsonify({'ok': True, 'result': res})

    @bp.route('/api/mcp/<sid>/restart', methods=['POST'])
    def api_mcp_restart(sid):
        p = _plugin()
        if p is None:
            return flask.jsonify({'ok': False, 'error': '插件未加载'}), 503
        async def _do():
            await p.mcp.restart(sid)
            specs = await p.mcp.refresh_tools(sid)
            return len(specs)
        n, err = _run_async(_do(), timeout=40)
        if err:
            return flask.jsonify({'ok': False, 'error': err[0]}), err[1]
        return flask.jsonify({'ok': True, 'tools': n})

    @bp.route('/api/mcp/<sid>/tools', methods=['GET'])
    def api_mcp_tools(sid):
        row = storage.get_mcp_server(db(), sid)
        if not row:
            return flask.jsonify({'ok': False, 'error': '不存在'}), 404
        return flask.jsonify({'ok': True, 'tools': row['tools'],
                              'status': row['status'],
                              'last_error': row['last_error']})

    # ── 用户 / 记忆 ────────────────────────────────────────────

    @bp.route('/api/users', methods=['GET'])
    def api_users():
        q = flask.request.args.get('q', '')
        return flask.jsonify(
            {'ok': True, 'users': storage.list_users(db(), q)})

    @bp.route('/api/users/<qq>/memories', methods=['GET'])
    def api_user_memories(qq):
        include = flask.request.args.get('all') == '1'
        return flask.jsonify({
            'ok': True,
            'user': storage.get_user(db(), qq),
            'memories': storage.list_memories(db(), qq, include),
        })

    @bp.route('/api/memories/<int:mid>', methods=['PUT', 'DELETE'])
    def api_memory_item(mid):
        if flask.request.method == 'DELETE':
            ok = storage.delete_memory(db(), mid)
            return flask.jsonify({'ok': ok})
        d = flask.request.get_json(silent=True) or {}
        storage.update_memory(
            db(), mid, text=d.get('text'), tags=d.get('tags'),
            importance=d.get('importance'), superseded=d.get('superseded'))
        p = _plugin()
        if p:
            p.memory.invalidate()
        return flask.jsonify({'ok': True})

    @bp.route('/api/users/<qq>/memories', methods=['DELETE'])
    def api_forget_user(qq):
        n = storage.forget_user(db(), qq)
        p = _plugin()
        if p:
            p.memory.invalidate(qq)
        return flask.jsonify({'ok': True, 'forgot': n})

    # ── 全局第一人称自我记忆 ───────────────────────────────────

    @bp.route('/api/self-memories', methods=['GET'])
    def api_self_memories():
        include = flask.request.args.get('all') == '1'
        return flask.jsonify({
            'ok': True,
            'memories': storage.list_self_memories(db(), include),
            'stats': storage.self_memory_stats(db()),
        })

    @bp.route('/api/self-memories/<int:mid>', methods=['PUT', 'DELETE'])
    def api_self_memory_item(mid):
        if flask.request.method == 'DELETE':
            ok = storage.delete_self_memory(db(), mid)
            p = _plugin()
            if p:
                p.self_memory.invalidate()
            return flask.jsonify({'ok': ok})
        d = flask.request.get_json(silent=True) or {}
        # 网页改事实层同样强制留痕（web_edit / supersede）
        trace = None
        if d.get('text') is not None:
            trace = {'kind': 'web_edit', 'group_id': 'web', 'source': 'web'}
        elif d.get('superseded') is not None:
            trace = {'kind': 'supersede', 'group_id': 'web',
                     'source': 'web'}
        storage.update_self_memory(
            db(), mid, text=d.get('text'), tags=d.get('tags'),
            importance=d.get('importance'), superseded=d.get('superseded'),
            trace=trace)
        p = _plugin()
        if p:
            p.self_memory.invalidate()
        return flask.jsonify({'ok': True})

    @bp.route('/api/self-memories/<int:mid>/traces', methods=['GET'])
    def api_self_memory_traces(mid):
        # 回忆链主人只读：模型上下文里绝不出现
        try:
            limit = max(1, min(int(flask.request.args.get('limit', 20)), 100))
        except ValueError:
            limit = 20
        return flask.jsonify({
            'ok': True,
            'traces': storage.list_self_memory_traces(db(), mid, limit)})

    @bp.route('/api/memory/stats', methods=['GET'])
    def api_memory_stats():
        stats = storage.memory_stats(db())
        stats.update(storage.self_memory_stats(db()))
        return flask.jsonify({'ok': True, 'stats': stats})

    # ── 系统提示词（注册表在 prompts.py，覆盖存 v13）────────────

    @bp.route('/api/prompts', methods=['GET'])
    def api_prompts_list():
        rows = {r['key']: r
                for r in storage.list_prompt_override_rows(db())}
        items = []
        for key, (section, label, tokens, default, hint) \
                in prompts_mod.PROMPTS.items():
            row = rows.get(key)
            items.append({
                'key': key,
                'section': section,
                'label': label,
                'tokens': list(tokens),
                'hint': hint,
                'default': default,
                'current': row['content'] if row else '',
                'updated_at': row['updated_at'] if row else 0,
                'overridden': row is not None,
            })
        return flask.jsonify({
            'ok': True,
            'sections': list(prompts_mod.SECTION_ORDER),
            'items': items})

    @bp.route('/api/prompts/<key>', methods=['PUT', 'DELETE'])
    def api_prompts_item(key):
        if key not in prompts_mod.PROMPTS:
            return flask.jsonify({'ok': False,
                                  'error': '未知的提示词 key'}), 404
        if flask.request.method == 'DELETE':
            storage.delete_prompt_override(db(), key)
        else:
            d = flask.request.get_json(silent=True) or {}
            content = d.get('content')
            if not isinstance(content, str):
                return flask.jsonify({'ok': False,
                                      'error': 'content 必须是字符串'}), 400
            content = content.strip()
            if not content:
                storage.delete_prompt_override(db(), key)   # 空白=恢复默认
            else:
                if len(content) > _PROMPT_MAX_CHARS:
                    return flask.jsonify(
                        {'ok': False,
                         'error': f'文案最长 {_PROMPT_MAX_CHARS} 字'}), 400
                storage.set_prompt_override(db(), key, content)
        p = _plugin()
        if p is not None and getattr(p, 'prompts', None) is not None:
            p.prompts.invalidate()
        return flask.jsonify({'ok': True})

    return bp


def _key_hint(key: str) -> str:
    return (key[:4] + _MASK + key[-4:]) if len(key) > 8 else _MASK


def _masked_providers(cfg: dict) -> list:
    """服务商列表的出 API 脱敏版：密钥永不返回，只给是否已配置 + 首尾提示。"""
    out = []
    for pr in (cfg or {}).get('llm_providers') or []:
        if not isinstance(pr, dict):
            continue
        key = str(pr.get('api_key') or '')
        e = {k: v for k, v in pr.items() if k != 'api_key'}
        e['api_key'] = ''
        e['key_set'] = bool(key)
        e['key_hint'] = _key_hint(key) if key else ''
        out.append(e)
    return out


def storage_placeholder_cfg():
    """插件未加载时（测试）的最小视图。"""
    return {'llm_providers': []}
