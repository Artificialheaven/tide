"""AI 自定义工具：dry-run 验证 → 发布 → 隔离调用。

信任模型：
- validate/publish/toggle/remove 是 owner-only（builtin_tools.RISKY_TOOL_KEYS
  + engine._risk_denied 统一门禁）；自定义工具代码只有主人能上线。
- 任何 AI 代码都只在短生子进程（tool_worker.py，python -I）里 exec：
  语法/加载/运行期崩溃只影响那次子进程；超时 taskkill 整棵进程树；
  主进程永不 import AI 代码。
- 发布 = 子进程加载验证通过后才原子 upsert；失败则库里旧版原样保留（回滚）。
- 已上线工具默认全员可触发（owner 发布即担责），可标 owner_only_call。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import storage

_NAME_RE = re.compile(r'^[a-z][a-z0-9_]{0,31}$')
_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
_WORKER = str(Path(__file__).resolve().parent / 'tool_worker.py')

CONTRACT_HINT = (
    '工具代码契约：定义 def tool(args: dict, ctx: dict) -> str；'
    'args 是模型按 params_schema 传的参数，'
    'ctx 提供 group_id / workspace(本群沙箱绝对路径) / now；返回字符串。'
)


def _lib_dir() -> str:
    return str(Path(__file__).resolve().parent.parent / 'lib')


# ── 子进程执行（同步阻塞，调用方 to_thread）──────────────────────

def _run_worker(req: dict, timeout: float) -> dict:
    """短生子进程跑一次 validate/call。任何崩溃都转成 {ok:False,error}。"""
    with tempfile.TemporaryDirectory(prefix='dstool_') as tmp:
        result_path = os.path.join(tmp, 'result.json')
        argv = [sys.executable, '-I', '-X', 'utf8', _WORKER, result_path]
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
        try:
            out, err = proc.communicate(
                json.dumps(req, ensure_ascii=False).encode('utf-8'),
                timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                                   capture_output=True, timeout=10)
                else:
                    proc.kill()
            except Exception:
                proc.kill()
            proc.communicate()
            return {'ok': False, 'stage': 'timeout',
                    'error': f'执行超过 {timeout:g}s，已强杀进程树'}
        if not os.path.exists(result_path):
            tail = (err or out or b'').decode('utf-8', 'replace')[-1200:]
            return {'ok': False, 'stage': 'crash',
                    'error': f'工具进程异常退出（code={proc.returncode}）：\n{tail}'}
        try:
            with open(result_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            return {'ok': False, 'stage': 'crash',
                    'error': f'结果读取失败: {e}'}


# ── 静态校验（不执行代码）────────────────────────────────────────

def validate_schema_text(text: str) -> tuple[dict | None, str]:
    try:
        schema = json.loads(text)
    except (ValueError, TypeError):
        return None, 'params_schema 不是合法 JSON 字符串'
    if not isinstance(schema, dict) or schema.get('type') != 'object':
        return None, 'params_schema 必须是 {"type":"object", "properties":{...}}'
    if 'properties' in schema and not isinstance(schema['properties'], dict):
        return None, 'params_schema.properties 必须是对象'
    return schema, ''


def _static_check(name: str, description: str, schema_text: str,
                  code: str, code_max: int) -> str:
    if not _NAME_RE.match(name or ''):
        return 'name 必须是 ^[a-z][a-z0-9_]{0,31}$（小写字母开头）'
    if not (description or '').strip():
        return 'description 必填（告诉模型什么时候调用）'
    if len(description) > 500:
        return 'description 最多 500 字'
    _, err = validate_schema_text(schema_text)
    if err:
        return err
    if not (code or '').strip():
        return 'code 为空'
    if len(code) > code_max:
        return f'代码超过 {code_max} 字符上限'
    try:
        compile(code, '<custom_tool>', 'exec')
    except SyntaxError as e:
        return f'语法错误（未上线）：{e.msg}（第 {e.lineno} 行）'
    return ''


# ── 管理器 ───────────────────────────────────────────────────────

class CustomToolManager:
    def __init__(self, db_path: str, log=None):
        self.db_path = db_path
        self._log = log or (lambda msg, level='info': None)

    # ── AI 工具：管理类 ────────────────────────────────────────

    async def dispatch_manage(self, name: str, args: dict, ctx) -> str:
        if name == 'custom_tool_validate':
            return await self._validate(args, ctx, publish=False)
        if name == 'custom_tool_publish':
            return await self._publish(args, ctx)
        if name == 'custom_tool_list':
            return await asyncio.to_thread(self._list_text, ctx.group_id)
        if name == 'custom_tool_toggle':
            return await self._toggle(args, ctx)
        if name == 'custom_tool_remove':
            return await self._remove(args, ctx)
        return '错误: 未知自定义工具管理操作'

    def _call_ctx(self, ctx) -> dict:
        return {
            'group_id': str(ctx.group_id),
            'workspace': str(ctx.sandbox.group_root),
            'now': time.strftime('%Y-%m-%d %H:%M:%S'),
            'dry_run': False,
        }

    async def _validate(self, args: dict, ctx, publish: bool) -> str:
        name = str(args.get('name', '') or '').strip()
        desc = str(args.get('description', '') or '').strip()
        schema_text = str(args.get('params_schema', '') or '')
        code = str(args.get('code', '') or '')
        code_max = int(ctx.cfg.get('custom_tool_code_max_chars', 8000))
        err = _static_check(name, desc, schema_text, code, code_max)
        if err:
            return '❌ 校验失败：' + err

        test_args = None
        if args.get('test_args') is not None:
            try:
                test_args = json.loads(str(args['test_args']))
                if not isinstance(test_args, dict):
                    return '❌ 校验失败：test_args 必须是 JSON 对象'
            except ValueError:
                return '❌ 校验失败：test_args 不是合法 JSON'

        timeout = float(ctx.cfg.get('custom_validate_timeout', 10))
        req = {'code': code, 'mode': 'validate', 'test_args': test_args,
               'ctx': self._call_ctx(ctx), 'lib_dirs': [_lib_dir()]}
        res = await asyncio.to_thread(_run_worker, req, timeout)
        if not res.get('ok'):
            return ('❌ dry-run 失败，未上线（框架与现有工具不受影响）：\n'
                    + str(res.get('error', '未知错误'))[-1500:])
        lines = ['✅ dry-run 通过：语法/加载正常，tool(args, ctx) 契约成立。']
        if res.get('load_print'):
            lines.append('加载期输出（建议删掉，加载应无副作用）：'
                         + res['load_print'][:300])
        if test_args is not None:
            lines.append('样例调用返回：' + str(res.get('test', ''))[:800])
            if res.get('test_print'):
                lines.append('样例 stdout：' + res['test_print'][:400])
        if publish:
            lines.append('（发布校验通过）')
        else:
            lines.append('确认无误后调用 custom_tool_publish 正式上线。')
        return '\n'.join(lines)

    async def _publish(self, args: dict, ctx) -> str:
        name = str(args.get('name', '') or '').strip()
        is_global = bool(args.get('global'))
        if is_global and not ctx.is_owner:
            return '错误: 全局工具只有主人能发布'
        # 发布前再做一次仅加载验证（不试跑），保证坏代码不进库
        args2 = dict(args)
        args2.pop('test_args', None)
        report = await self._validate(args2, ctx, publish=True)
        if not report.startswith('✅'):
            return report.replace('custom_tool_publish', 'custom_tool_publish')
        scope = storage.GLOBAL_CUSTOM_SCOPE if is_global else str(ctx.group_id)
        existing = [r for r in storage.list_custom_tools(self.db_path)
                    if r['scope'] == scope and r['name'] == name]
        cap = int(ctx.cfg.get('custom_tools_max_per_scope', 20))
        if not existing and storage.count_custom_tools(self.db_path, scope) >= cap:
            return f'错误: 该作用域工具数已达上限 {cap}，先 custom_tool_remove 几个'
        fields = {
            'scope': scope, 'name': name,
            'title': str(args.get('title', '') or '')[:20],
            'description': str(args.get('description', '') or '')[:500],
            'params_schema': str(args.get('params_schema', '') or ''),
            'code': str(args.get('code', '') or ''),
            'owner_only_call': bool(args.get('owner_only_call')),
            'created_by': str(ctx.sender_id or ''),
        }
        tool_id = await asyncio.to_thread(
            storage.upsert_custom_tool, self.db_path, fields)
        where = '全部群/私聊' if is_global else f'群 {ctx.group_id}'
        self._log(f'自定义工具上线：{name}（id={tool_id}，{where}，'
                  f'发布人 {ctx.sender_id}）', 'ok')
        return (f'✅ 工具 custom__{name}（id={tool_id}）已上线，{where}生效。'
                f'之后你可以直接按它的参数调用；'
                f'{"仅主人可触发；" if fields["owner_only_call"] else ""}'
                f'更新代码用同名重新发布（旧版验证失败会自动保留）。')

    def _list_text(self, gid: str) -> str:
        rows = storage.list_custom_tools(self.db_path, str(gid))
        if not rows:
            return '本群没有自定义工具（用 custom_tool_validate 试写，再 custom_tool_publish 上线）'
        lines = ['自定义工具（本群 + 全局）：']
        for r in rows:
            scope = '全局' if r['global'] else '本群'
            state = '启用' if r['enabled'] else '停用'
            owner = '，仅主人触发' if r['owner_only_call'] else ''
            lines.append(
                f'[{r["id"]}] custom__{r["name"]}（{scope}/{state}{owner}）'
                f' {r["title"] or r["description"][:30]}')
        return '\n'.join(lines)

    async def _toggle(self, args: dict, ctx) -> str:
        row = await self._owned_row(args, ctx)
        if isinstance(row, str):
            return row
        enabled = bool(args.get('enabled'))
        await asyncio.to_thread(storage.set_custom_tool_enabled,
                                self.db_path, row['id'], enabled)
        return f'已{"启用" if enabled else "停用"} custom__{row["name"]}'

    async def _remove(self, args: dict, ctx) -> str:
        row = await self._owned_row(args, ctx)
        if isinstance(row, str):
            return row
        await asyncio.to_thread(storage.delete_custom_tool,
                                self.db_path, row['id'])
        return f'已删除 custom__{row["name"]}'

    async def _owned_row(self, args: dict, ctx):
        """取目标行并做作用域/权限校验，返回 row 或错误字符串。"""
        try:
            tool_id = int(args.get('id'))
        except (TypeError, ValueError):
            return '错误: id 必须是整数（custom_tool_list 里方括号编号）'
        row = await asyncio.to_thread(storage.get_custom_tool, self.db_path,
                                      tool_id)
        if not row:
            return '未找到该自定义工具'
        if row['global'] and not ctx.is_owner:
            return '错误: 全局自定义工具只有主人能修改'
        if not row['global'] and row['scope'] != str(ctx.group_id):
            return '错误: 不能管理其他群/私聊的自定义工具'
        return row

    # ── 模型调用已上线工具 ─────────────────────────────────────

    def call_tool(self, row: dict, args: dict, call_ctx: dict,
                  timeout: float) -> str:
        """同步（engine 用 to_thread 包裹）。任何异常都文本化，绝不抛出到工具循环。"""
        try:
            req = {'code': row['code'], 'mode': 'call', 'args': args or {},
                   'ctx': call_ctx, 'lib_dirs': [_lib_dir()]}
            res = _run_worker(req, timeout)
        except Exception as e:
            return f'错误: 自定义工具执行器异常: {e}'
        if not res.get('ok'):
            self._log(f'自定义工具 {row.get("name", "?")} 执行失败: '
                      + str(res.get('error', ''))[-300:], 'warn')
            return '错误: 自定义工具执行失败（不影响其他工具）：\n' \
                + str(res.get('error', '未知错误'))[-1000:]
        result = res.get('result') or ''
        printed = res.get('print') or ''
        if not result and printed:
            return printed
        if printed:
            return str(result) + '\n（工具 stdout）\n' + printed[:1000]
        return str(result) or '（工具无返回值）'
