"""自定义工具/初筛脚本一次性子进程执行器（不被主进程 import，由管理器拉起）。

协议：
- argv[1] = 结果文件路径（避免工具 print 污染协议通道）
- stdin = 一个 JSON：{code, args, ctx, mode, test_args, lib_dirs}
  mode:
    'call'/'validate'       自定义工具，必须定义
                            def tool(args: dict, ctx: dict) -> str
    'screen'/'screen_validate'  初筛脚本，必须定义
                            def screen(group_id, user_id, message_raw) -> bool
                            （可选第 4 参 ctx，框架检测到会传上下文字典）
- 代码在独立 namespace 里 exec；结果 JSON 写 argv[1]；
  进程崩溃/段错误只死这个子进程。
"""
import inspect
import io
import json
import sys
import traceback


class _ContractError(Exception):
    pass


def _coerce(rv):
    if isinstance(rv, str):
        return rv
    if rv is None:
        return ''
    if isinstance(rv, (dict, list)):
        return json.dumps(rv, ensure_ascii=False)
    return str(rv)


_SCREEN_TRUE = {'true', '1', 'yes', 'y', '是', '允许', '命中'}
_SCREEN_FALSE = {'false', '0', 'no', 'n', '否', '', '不允许', '未命中'}


def _coerce_bool(rv) -> bool:
    """screen 返回值归一化。脚本应直接 return True/False；
    容错字符串 "true"/"false"（Python 里非空字符串恒真，不能直接 bool()）。"""
    if isinstance(rv, bool):
        return rv
    if isinstance(rv, (int, float)):
        return rv != 0
    if rv is None:
        return False
    s = str(rv).strip().lower()
    if s in _SCREEN_TRUE:
        return True
    if s in _SCREEN_FALSE:
        return False
    return bool(s)


def _screen_payload(src: dict) -> dict:
    src = src or {}
    return {
        'group_id': str(src.get('group_id', '') or ''),
        'user_id': str(src.get('user_id', '') or ''),
        'message_raw': str(src.get('message_raw', '') or ''),
    }


def _call_screen(fn, payload: dict, ctx: dict | None):
    args = _screen_payload(payload)
    try:
        n = len(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        n = 3
    if n >= 4 and ctx is not None:
        return fn(args['group_id'], args['user_id'], args['message_raw'], ctx)
    return fn(args['group_id'], args['user_id'], args['message_raw'])


def _write(path: str, payload: dict):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)


def main() -> int:
    result_path = sys.argv[1]
    try:
        req = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    except Exception as e:
        _write(result_path, {'ok': False, 'stage': 'protocol',
                             'error': f'请求解析失败: {e}'})
        return 1

    for p in req.get('lib_dirs') or []:
        if p and p not in sys.path:
            sys.path.insert(0, p)

    mode = req.get('mode', 'call')
    is_screen = mode in ('screen', 'screen_validate')
    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
    try:
        ns: dict = {}
        # exec 期的 print 也收进报告（上线代码不应在加载期输出）
        with _redirect(stdout_buf, stderr_buf):
            exec(compile(req.get('code', ''),
                         '<screen_script>' if is_screen else '<custom_tool>',
                         'exec'), ns)
        info = {'ok': True, 'loaded': True,
                'load_print': stdout_buf.getvalue()[-1000:]}
        if is_screen:
            fn = ns.get('screen')
            if not callable(fn):
                raise _ContractError(
                    '代码必须定义可调用函数：'
                    'def screen(group_id, user_id, message_raw) -> bool')
            if mode == 'screen_validate':
                if req.get('test_args') is not None:
                    call_out = io.StringIO()
                    ctx = dict(req.get('ctx') or {})
                    ctx['dry_run'] = True
                    with _redirect(call_out, stderr_buf):
                        rv = _call_screen(fn, req['test_args'], ctx)
                    info['test'] = _coerce(rv)
                    info['test_allow'] = _coerce_bool(rv)
                    info['test_print'] = call_out.getvalue()[-2000:]
                else:
                    info['test'] = None
            else:
                call_out = io.StringIO()
                with _redirect(call_out, stderr_buf):
                    rv = _call_screen(
                        fn, req.get('args') or {},
                        dict(req.get('ctx') or {}))
                info['allow'] = _coerce_bool(rv)
                info['raw'] = _coerce(rv)
                info['print'] = call_out.getvalue()[-2000:]
            _write(result_path, info)
            return 0
        fn = ns.get('tool')
        if not callable(fn):
            raise _ContractError(
                '代码必须定义可调用函数：def tool(args: dict, ctx: dict) -> str')
        if mode == 'validate':
            if req.get('test_args') is not None:
                call_out = io.StringIO()
                ctx = dict(req.get('ctx') or {})
                ctx['dry_run'] = True
                with _redirect(call_out, stderr_buf):
                    rv = fn(dict(req['test_args']), ctx)
                info['test'] = _coerce(rv)
                info['test_print'] = call_out.getvalue()[-2000:]
            else:
                info['test'] = None
        else:
            call_out = io.StringIO()
            with _redirect(call_out, stderr_buf):
                rv = fn(dict(req.get('args') or {}), dict(req.get('ctx') or {}))
            info['result'] = _coerce(rv)
            info['print'] = call_out.getvalue()[-2000:]
        _write(result_path, info)
        return 0
    except Exception:
        _write(result_path, {
            'ok': False, 'stage': req.get('mode', 'call'),
            'error': traceback.format_exc(limit=8),
            'print': stdout_buf.getvalue()[-1000:]})
        return 1


class _redirect:
    """同时重定向 stdout/stderr 的上下文（contextlib 版的本地写法）。"""

    def __init__(self, out, err):
        self._out, self._err = out, err
        self._old = (None, None)

    def __enter__(self):
        self._old = (sys.stdout, sys.stderr)
        sys.stdout, sys.stderr = self._out, self._err
        return self

    def __exit__(self, *exc):
        sys.stdout, sys.stderr = self._old
        return False


if __name__ == '__main__':
    sys.exit(main())
