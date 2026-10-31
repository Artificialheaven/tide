"""AI 自制初筛脚本：dry-run 验证 → 发布 → 隔离子进程判定。

多判官机制的第一层（便宜层）：每条未强制触发的群消息，启发式粗筛与所有
启用中的初筛脚本是「或」关系——任一放行才拉起 LLM 判官（贵的那层）。

信任模型与 custom_tools 完全一致：
- validate/publish/toggle/remove 是 owner-only（builtin_tools.RISKY_TOOL_KEYS
  + engine._risk_denied 统一门禁）；脚本只有主人能上线。
- 代码只在短生子进程（tool_worker.py，python -I）里 exec；
  语法/加载/运行期崩溃只影响那次子进程；超时 taskkill 整棵进程树；
  主进程永不 import AI 代码。
- 发布 = 子进程加载验证通过后才原子 upsert；失败旧版原样保留。
- 脚本判定 fail-closed：任何异常/超时/返回值无法识别都按「未命中」，
  绝不因脚本故障跳过启发式之外的所有消息（也不会因故障放过所有消息）。

契约（用户指定的三参数形态）：
    def screen(group_id: str, user_id: str, message_raw: str) -> bool
可选第 4 参 ctx（框架检测到形参≥4 才传）：{group_id, now, dry_run}。
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from . import storage
from .custom_tools import _lib_dir, _run_worker

_NAME_RE = re.compile(r'^[a-z][a-z0-9_]{0,31}$')

CONTRACT_HINT = (
    '初筛脚本契约：定义 def screen(group_id: str, user_id: str, '
    'message_raw: str) -> bool；三个参数都是字符串，'
    '返回 True=这条值得拉起判官再看看，False=放行给下一道初筛。'
    '脚本要便宜（默认 5 秒超时），不要发起网络请求或做重计算。'
)


def screen_static_check(name: str, description: str, code: str,
                        code_max: int) -> str:
    """静态校验（不执行代码）。返回 ''=通过，否则错误文案。"""
    if not _NAME_RE.match(name or ''):
        return 'name 必须是 ^[a-z][a-z0-9_]{0,31}$（小写字母开头）'
    if not (description or '').strip():
        return 'description 必填（一句话说明这个脚本在替你留意什么）'
    if len(description) > 500:
        return 'description 最多 500 字'
    if not (code or '').strip():
        return 'code 为空'
    if len(code) > code_max:
        return f'代码超过 {code_max} 字符上限'
    try:
        compile(code, '<screen_script>', 'exec')
    except SyntaxError as e:
        return f'语法错误（未上线）：{e.msg}（第 {e.lineno} 行）'
    return ''


class ScreenScriptManager:
    def __init__(self, db_path: str, log=None):
        self.db_path = db_path
        self._log = log or (lambda msg, level='info': None)

    # ── AI 工具：管理类 ────────────────────────────────────────

    async def dispatch_manage(self, name: str, args: dict, ctx) -> str:
        if name == 'screen_validate':
            return await self._validate(args, ctx, publish=False)
        if name == 'screen_publish':
            return await self._publish(args, ctx)
        if name == 'screen_list':
            return await asyncio.to_thread(self._list_text, ctx.group_id)
        if name == 'screen_toggle':
            return await self._toggle(args, ctx)
        if name == 'screen_remove':
            return await self._remove(args, ctx)
        return '错误: 未知初筛脚本管理操作'

    def _call_ctx(self, ctx) -> dict:
        return {
            'group_id': str(ctx.group_id),
            'now': time.strftime('%Y-%m-%d %H:%M:%S'),
            'dry_run': False,
        }

    async def _validate(self, args: dict, ctx, publish: bool) -> str:
        name = str(args.get('name', '') or '').strip()
        desc = str(args.get('description', '') or '').strip()
        code = str(args.get('code', '') or '')
        code_max = int(ctx.cfg.get('screen_code_max_chars', 8000))
        err = screen_static_check(name, desc, code, code_max)
        if err:
            return '❌ 校验失败：' + err

        test_args = None
        if args.get('test_message') is not None or args.get('test_args') is not None:
            raw = args.get('test_args')
            if raw is None:
                raw = json.dumps({
                    'group_id': str(ctx.group_id),
                    'user_id': str(ctx.sender_id or ''),
                    'message_raw': str(args.get('test_message', '') or ''),
                }, ensure_ascii=False)
            try:
                test_args = json.loads(str(raw))
                if not isinstance(test_args, dict):
                    return '❌ 校验失败：test_args 必须是 JSON 对象'
            except ValueError:
                return '❌ 校验失败：test_args 不是合法 JSON'

        timeout = float(ctx.cfg.get('screen_validate_timeout', 10))
        req = {'code': code, 'mode': 'screen_validate',
               'test_args': test_args, 'ctx': self._call_ctx(ctx),
               'lib_dirs': [_lib_dir()]}
        res = await asyncio.to_thread(_run_worker, req, timeout)
        if not res.get('ok'):
            return ('❌ dry-run 失败，未上线（框架与现有脚本不受影响）：\n'
                    + str(res.get('error', '未知错误'))[-1500:])
        lines = ['✅ dry-run 通过：语法/加载正常，screen(group_id, user_id, '
                 'message_raw) 契约成立。']
        if res.get('load_print'):
            lines.append('加载期输出（建议删掉，加载应无副作用）：'
                         + res['load_print'][:300])
        if test_args is not None:
            verdict = '允许（拉起判官）' if res.get('test_allow') else '不允许'
            lines.append(f'样例判定返回：{verdict}（原始返回 {str(res.get("test", ""))[:200]}）')
            if res.get('test_print'):
                lines.append('样例 stdout：' + res['test_print'][:400])
        if publish:
            lines.append('（发布校验通过）')
        else:
            lines.append('确认无误后调用 screen_publish 正式上线。')
        return '\n'.join(lines)

    async def _publish(self, args: dict, ctx) -> str:
        name = str(args.get('name', '') or '').strip()
        is_global = bool(args.get('global'))
        if is_global and not ctx.is_owner:
            return '错误: 全局初筛脚本只有主人能发布'
        # 发布前再做一次仅加载验证（不试跑），保证坏代码不进库
        args2 = dict(args)
        args2.pop('test_args', None)
        args2.pop('test_message', None)
        report = await self._validate(args2, ctx, publish=True)
        if not report.startswith('✅'):
            return report
        scope = storage.GLOBAL_CUSTOM_SCOPE if is_global else str(ctx.group_id)
        existing = [r for r in storage.list_screen_scripts(self.db_path)
                    if r['scope'] == scope and r['name'] == name]
        cap = int(ctx.cfg.get('screen_max_per_scope', 10))
        if not existing and storage.count_screen_scripts(
                self.db_path, scope) >= cap:
            return f'错误: 该作用域初筛脚本数已达上限 {cap}，先 screen_remove 几个'
        fields = {
            'scope': scope, 'name': name,
            'title': str(args.get('title', '') or '')[:20],
            'description': str(args.get('description', '') or '')[:500],
            'code': str(args.get('code', '') or ''),
            'created_by': str(ctx.sender_id or ''),
        }
        script_id = await asyncio.to_thread(
            storage.upsert_screen_script, self.db_path, fields)
        where = '全部群/私聊' if is_global else f'群 {ctx.group_id}'
        self._log(f'初筛脚本上线：{name}（id={script_id}，{where}，'
                  f'发布人 {ctx.sender_id}）', 'ok')
        return (f'✅ 初筛脚本 {name}（id={script_id}）已上线，{where}生效。'
                f'之后每条未直接 @ 你/回复你的群消息都会跑它；'
                f'它与内置启发式、其他脚本是「或」关系，任一放行才拉起判官。'
                f'同名重新发布即升级（旧版验证失败会自动保留）。')

    def _list_text(self, gid: str) -> str:
        rows = storage.list_screen_scripts(self.db_path, str(gid))
        if not rows:
            return ('本群没有初筛脚本（用 screen_validate 试写，再 '
                    'screen_publish 上线；契约：screen(group_id, user_id, '
                    'message_raw) -> bool）')
        lines = ['初筛脚本（本群 + 全局；任一返回允许就拉起判官）：']
        for r in rows:
            scope = '全局' if r['global'] else '本群'
            state = '启用' if r['enabled'] else '停用'
            import time as _t
            last = (_t.strftime('%m-%d %H:%M', _t.localtime(r['last_hit_at']))
                    if r['last_hit_at'] else '从未命中')
            lines.append(
                f'[{r["id"]}] {r["name"]}（{scope}/{state}，'
                f'命中 {r["hit_count"]} 次，最近 {last}）'
                f' {r["title"] or r["description"][:30]}')
        return '\n'.join(lines)

    async def _toggle(self, args: dict, ctx) -> str:
        row = await self._owned_row(args, ctx)
        if isinstance(row, str):
            return row
        enabled = bool(args.get('enabled'))
        await asyncio.to_thread(storage.set_screen_script_enabled,
                                self.db_path, row['id'], enabled)
        return f'已{"启用" if enabled else "停用"}初筛脚本 {row["name"]}'

    async def _remove(self, args: dict, ctx) -> str:
        row = await self._owned_row(args, ctx)
        if isinstance(row, str):
            return row
        await asyncio.to_thread(storage.delete_screen_script,
                                self.db_path, row['id'])
        return f'已删除初筛脚本 {row["name"]}'

    async def _owned_row(self, args: dict, ctx):
        """取目标行并做作用域/权限校验，返回 row 或错误字符串。"""
        try:
            script_id = int(args.get('id'))
        except (TypeError, ValueError):
            return '错误: id 必须是整数（screen_list 里方括号编号）'
        row = await asyncio.to_thread(storage.get_screen_script,
                                      self.db_path, script_id)
        if not row:
            return '未找到该初筛脚本'
        if row['global'] and not ctx.is_owner:
            return '错误: 全局初筛脚本只有主人能修改'
        if not row['global'] and row['scope'] != str(ctx.group_id):
            return '错误: 不能管理其他群/私聊的初筛脚本'
        return row

    # ── 引擎期：同步跑一条脚本（engine 用 to_thread 包裹）────────

    def run_screen(self, row: dict, payload: dict,
                   timeout: float) -> dict:
        """任何异常都文本化并按未命中处理，绝不抛到事件循环。"""
        try:
            req = {'code': row['code'], 'mode': 'screen',
                   'args': payload, 'ctx': {
                       'group_id': str(payload.get('group_id', '')),
                       'now': time.strftime('%Y-%m-%d %H:%M:%S'),
                       'dry_run': False},
                   'lib_dirs': [_lib_dir()]}
            res = _run_worker(req, timeout)
        except Exception as e:
            return {'allow': False, 'error': f'执行器异常: {e}'}
        if not res.get('ok'):
            self._log(f'初筛脚本 {row.get("name", "?")} 执行失败（按未命中）: '
                      + str(res.get('error', ''))[-300:], 'warn')
            return {'allow': False,
                    'error': str(res.get('error', '未知错误'))[-800:]}
        return {'allow': bool(res.get('allow')),
                'printed': res.get('print') or ''}
