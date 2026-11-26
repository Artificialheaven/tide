import _imp
import importlib
import inspect
import threading
import concurrent.futures
import websockets
from core import globals
import sys
import os
import subprocess
import time
import asyncio
import atexit
import re
import shutil
import traceback

from utils.asyncutil import wait_task_resilient


# 热重载串行锁：防止两个重载并发清理/重建 sys.modules
_reload_lock = threading.RLock()

# 实例级关停标记：ashutdown/_call_stop 调过 on_stop 后打上，atexit 不再重复调。
# 必须挂实例而不是插件名——热重载后的同名新实例不能被旧标记污染
_STOP_MARK = '_tide_on_stop_done'

# _call_stop 在事件循环线程内只能异步调度 on_stop：强引用这些 task，
# 由 adrain_background / ashutdown 统一有界回收
_PENDING_STOPS: set = set()


def _mark_stopped(inst) -> None:
    try:
        setattr(inst, _STOP_MARK, True)
    except Exception:
        # 极端情况（__slots__ 等）：标记打不上就静默退回每次都尝试
        pass


def get_plugins_path():
    """获取 plugins 目录的绝对路径（兼容打包后的环境）。"""
    if getattr(sys, 'frozen', False):
        base_path = os.path.dirname(sys.executable)
    else:
        # 本文件位于 <根>/utils/plugin_loader.py，根目录即上一级
        base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, 'plugins')


def find_python_interpreter():
    """查找系统中的 Python 解释器"""
    # 优先使用当前运行的 Python
    python_exe = sys.executable
    
    # 验证是否可用
    try:
        result = subprocess.run(
            [python_exe, '--version'],
            capture_output=True,
            timeout=5
        )
        if result.returncode == 0:
            print(f"找到 Python 解释器: {python_exe}")
            return python_exe
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    
    # 尝试其他常见路径
    python_paths = [
        'python',
        'python3',
        'py',
    ]
    
    if sys.platform == 'win32':
        python_paths.extend([
            'C:/Python39/python.exe',
            'C:/Python310/python.exe',
            'C:/Python311/python.exe',
            'C:/Python312/python.exe',
        ])
    
    for python_path in python_paths:
        try:
            result = subprocess.run(
                [python_path, '--version'],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0:
                print(f"找到 Python 解释器: {python_path}")
                return python_path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    
    return None


def _parse_requirement(requirement: str):
    """解析 '包名>=1.2' 形式的需求为 (规范名, [(操作符, 版本)])。"""
    import re
    m = re.match(r'\s*([A-Za-z0-9._-]+)\s*(.*)', requirement)
    if not m:
        return requirement.strip(), []
    name = re.sub(r'[-_.]+', '-', m.group(1)).lower()
    specs = []
    for op, ver in re.findall(r'(~=|===|==|!=|>=|<=|>|<)\s*([0-9][A-Za-z0-9._!+*-]*)',
                              m.group(2)):
        specs.append((op, tuple(int(x) for x in re.findall(r'\d+', ver)) or (0,)))
    return name, specs


def _version_satisfied(version: tuple, specs: list) -> bool:
    for op, wanted in specs:
        if op in ('==', '==='):
            if version[:len(wanted)] != wanted:
                return False
        elif op == '!=':
            if version[:len(wanted)] == wanted:
                return False
        elif op == '>=' and not version >= wanted:
            return False
        elif op == '<=' and not version <= wanted:
            return False
        elif op == '>' and not version > wanted:
            return False
        elif op == '<' and not version < wanted:
            return False
        elif op == '~=':
            # 兼容版本：>=wanted 且前 len-1 段相同
            if not version >= wanted:
                return False
            if version[:len(wanted) - 1] != wanted[:len(wanted) - 1]:
                return False
    return True


def check_package_in_lib(lib_path: str, package_name: str) -> bool:
    """
    检查包是否已在 lib 目录安装，且版本满足 requirements 约束。

    优先读取 *.dist-info 元数据（规范 PEP 503 名称），
    找不到元数据时回退到目录/文件存在性检测。

    :param lib_path: lib 目录路径
    :param package_name: 需求（可带版本约束，如 'requests>=2.28'）
    :return: 是否已安装且满足版本
    """
    if not os.path.exists(lib_path):
        return False

    try:
        name, specs = _parse_requirement(package_name)
        for item in os.listdir(lib_path):
            if item.endswith('.dist-info'):
                # 目录名形如 requests-2.34.2.dist-info
                stem = item[:-len('.dist-info')]
                parts = stem.split('-', 1)
                if len(parts) != 2:
                    continue
                dist_name = re.sub(r'[-_.]+', '-', parts[0]).lower()
                if dist_name != name:
                    continue
                if not specs:
                    return True
                # 只保留数字段：2.0rc1 这类预发布混入字符串后与 int
                # 比较会抛 TypeError，被兜底成“未安装”导致每次加载重装
                ver_parts = tuple(int(x) for x in re.findall(r'\d+', parts[1])) or (0,)
                return _version_satisfied(ver_parts, specs)

        # 无 dist-info：按导入名（- 与 _ 互换）粗略判断
        import_name = name.replace('-', '_')
        for item in os.listdir(lib_path):
            item_lower = item.lower()
            if item_lower == import_name \
                    or item_lower == import_name + '.py' \
                    or item_lower.startswith(import_name + '-'):
                return True
        return False
    except Exception:
        return False


def install_to_plugin_lib(requirements_file: str, plugin_name: str, plugin_dir: str) -> bool:
    """
    将依赖安装到插件的独立 lib 目录
    
    :param requirements_file: requirements.txt 文件路径
    :param plugin_name: 插件名称
    :param plugin_dir: 插件目录路径
    :return: 是否安装成功
    """
    if not os.path.exists(requirements_file):
        return True
    
    print(f"[{plugin_name}] 检测到 requirements.txt，开始检查依赖...")
    
    # 创建插件的 lib 目录
    lib_path = os.path.join(plugin_dir, 'lib')
    os.makedirs(lib_path, exist_ok=True)
    
    # 使用当前 Python 解释器
    python_exe = sys.executable
    
    try:
        # 读取 requirements.txt
        with open(requirements_file, 'r', encoding='utf-8') as f:
            requirements = [line.strip() for line in f
                            if line.strip() and not line.strip().startswith('#')]
        
        if not requirements:
            print(f"[{plugin_name}] requirements.txt 为空，跳过安装")
            return True
        
        # 检查哪些包需要安装
        missing_packages = []
        installed_packages = []
        
        for req in requirements:
            if check_package_in_lib(lib_path, req):
                installed_packages.append(req)
                print(f"[{plugin_name}] ✓ 已安装: {req}")
            else:
                missing_packages.append(req)
                print(f"[{plugin_name}] ✗ 未安装: {req}")
        
        # 如果所有依赖都已安装，跳过
        if not missing_packages:
            print(f"[{plugin_name}] 所有依赖已安装，跳过安装")
            return True
        
        # 安装缺失的依赖到 lib 目录
        print(f"[{plugin_name}] 需要安装的依赖: {', '.join(missing_packages)}")
        
        cmd = [
            python_exe,
            '-m', 'pip', 'install',
            '--target', lib_path,  # 安装到指定目录
            *missing_packages,
            '--progress-bar', 'off',          # 关掉 \r 进度条，逐行日志才不刷屏
            '--disable-pip-version-check',
            '--no-warn-script-location'
        ]

        print(f"[{plugin_name}] 正在安装依赖到 lib 目录（pip 输出在结束时汇总显示）...")
        # 有界等待：pip 卡在认证/网络时会一行输出都没有，逐行读 + 循环后置
        # 超时检查（旧实现）等于无限期冻住事件循环。communicate 带超时兜底。
        try:
            out, _ = proc.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            print(f"[{plugin_name}] 依赖安装超时（180s），已终止 pip", flush=True)
            return False
        tail_lines = [ln.rstrip() for ln in (out or '').splitlines() if ln.strip()]
        for line in tail_lines[-40:]:
            print(f"[{plugin_name}|pip] {line}", flush=True)

        if proc.returncode == 0:
            print(f"[{plugin_name}] 依赖安装成功到: {lib_path}")

            # 创建 .pth 文件以便 Python 能找到这些包
            pth_file = os.path.join(plugin_dir, f'{plugin_name}_lib.pth')
            with open(pth_file, 'w', encoding='utf-8') as f:
                f.write(lib_path + '\n')

            print(f"[{plugin_name}] 已创建路径配置文件: {pth_file}")
            return True
        else:
            print(f"[{plugin_name}] 依赖安装失败（pip 退出码 {proc.returncode}），末尾日志:")
            for line in tail_lines:
                print(f"  {line}")
            return False

    except subprocess.TimeoutExpired:
        print(f"[{plugin_name}] 依赖安装超时（超过5分钟）")
        return False
    except Exception as e:
        print(f"[{plugin_name}] 安装依赖时出错: {e}")
        import traceback
        traceback.print_exc()
        return False


def add_plugin_lib_to_path(plugin_dir: str, plugin_name: str):
    """
    将插件的 lib 目录添加到 sys.path
    
    :param plugin_dir: 插件目录路径
    :param plugin_name: 插件名称
    """
    lib_path = os.path.join(plugin_dir, 'lib')
    
    if os.path.exists(lib_path) and lib_path not in sys.path:
        # 将插件 lib 目录添加到 sys.path 的前面，优先使用
        sys.path.insert(0, lib_path)
        print(f"[{plugin_name}] 已添加 lib 目录到路径: {lib_path}")


def remove_plugin_lib_from_path(plugin_dir: str, plugin_name: str):
    """卸载插件时把其 lib 目录移出 sys.path，避免依赖影子长期残留。"""
    lib_path = os.path.join(plugin_dir, 'lib')
    if lib_path in sys.path:
        sys.path.remove(lib_path)
        print(f"[{plugin_name}] 已从 sys.path 移除 lib 目录: {lib_path}")


class PluginBotWrapper:
    """Bot包装器，自动注入插件名称"""
    
    def __init__(self, bot_instance, plugin_name):
        self._bot = bot_instance
        self._plugin_name = plugin_name
        # 创建专属Logger，固定插件名为来源
        self.Logger = PluginLogger(bot_instance.Logger.add_log, plugin_name)

    def __getattr__(self, name):
        """自动委托未显式定义的属性到底层 bot 实例。

        需要注入 plugin_name 的方法（send_message、call_api 等）
        仍由显式方法转发，不会经过此通路。
        """
        # 防止 _bot 未初始化时无限递归
        if name == '_bot':
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '_bot'")
        if '_bot' in self.__dict__:
            return getattr(self._bot, name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")
    
    async def send_message(self, message, target_id=None, message_type="private",
                           extra=None, **kwargs):
        """发送消息（自动注入plugin_name）

        extra：附加 action 参数（如 official 通道的
        {'_official_msg_id': ..., '_official_user_id': ...}），
        NapCat 通道会忽略未知参数。其余未知 kwargs 同样并入 extra。
        """
        merged = dict(extra or {})
        merged.update(kwargs)
        return await self._bot.send_message(
            message, target_id, message_type,
            plugin_name=self._plugin_name,
            extra=merged or None
        )
    
    async def get_group_list(self, no_cache=False):
        """获取群列表"""
        return await self._bot.get_group_list(
            plugin_name=self._plugin_name, 
            no_cache=no_cache
        )
    
    async def get_friend_list(self):
        """获取好友列表"""
        return await self._bot.get_friend_list(
            plugin_name=self._plugin_name
        )
    
    async def get_group_info(self, group_id, no_cache=False):
        """获取群信息"""
        return await self._bot.get_group_info(
            group_id, 
            plugin_name=self._plugin_name, 
            no_cache=no_cache
        )
    
    async def get_group_member_list(self, group_id, no_cache=False):
        """获取群成员列表"""
        return await self._bot.get_group_member_list(
            group_id, 
            plugin_name=self._plugin_name, 
            no_cache=no_cache
        )
    
    async def get_group_member_info(self, group_id, user_id, no_cache=False):
        """获取群成员信息"""
        return await self._bot.get_group_member_info(
            group_id, user_id, 
            plugin_name=self._plugin_name, 
            no_cache=no_cache
        )
    
    async def get_login_info(self):
        """获取登录信息"""
        return await self._bot.get_login_info(
            plugin_name=self._plugin_name
        )
    
    async def call_api(self, action, params=None):
        """通用API调用"""
        return await self._bot.call_api(
            action, params, 
            plugin_name=self._plugin_name
        )
    
    async def call_api_parallel(self, api_calls):
        """并行调用API"""
        return await self._bot.call_api_parallel(
            api_calls, 
            plugin_name=self._plugin_name
        )
    
    async def call_api_batch(self, action, params_list):
        """批量调用API"""
        return await self._bot.call_api_batch(
            action, params_list, 
            plugin_name=self._plugin_name
        )
    
    @property
    def websocket(self):
        return self._bot.websocket
    
    @property
    def pending_requests(self):
        return self._bot.pending_requests
    
    @property
    def bot_qq(self):
        return self._bot.bot_qq


class PluginLogger:
    """插件专属Logger，固定来源为插件名"""
    
    def __init__(self, add_log_callback, plugin_name):
        self.add_log = add_log_callback
        self.plugin_name = plugin_name

    def info(self, response, source, content):
        """普通日志 - 来源强制为插件名"""
        self.add_log(response, self.plugin_name, content, '默认')

    def warning(self, response, source, content):
        """警告日志 - 来源强制为插件名"""
        self.add_log(response, self.plugin_name, content, '黄色')

    def error(self, response, source, content):
        """错误日志 - 来源强制为插件名"""
        self.add_log(response, self.plugin_name, content, '红色')

    def custom(self, response, source, content, color):
        """自定义日志 - 来源强制为插件名"""
        self.add_log(response, self.plugin_name, content, color)

    def msg(self, content):
        self.info("框架", self.plugin_name, content)


def _wait_stop_future(future, timeout: float) -> bool:
    """有界等待 on_stop 的 concurrent.futures.Future，返回是否干净结束。

    超时时显式 cancel 旧实例的 on_stop 协程，并把「未干净结束」作为结果返回。
    以前 TimeoutError 被 _call_stop 的兜底 except 吞掉，旧 on_stop 继续跑、
    新实例已经 import，形成新旧实例并存（审计 P0-3）。
    """
    try:
        future.result(timeout=timeout)
        return True
    except (TimeoutError, concurrent.futures.TimeoutError):
        print(f'[卸载] on_stop 超过 {timeout:.1f}s 未结束，取消旧实例的 on_stop 协程')
        try:
            future.cancel()
        except BaseException:
            print('[卸载] 取消 on_stop future 失败:')
            traceback.print_exc()
        return False
    except BaseException:
        # 含 CancelledError（BaseException）：循环关闭期 future 可能被取消。
        # 异常同样意味着 stop 不干净，交给调用方拒绝重载。
        print('[卸载] on_stop 执行异常:')
        traceback.print_exc()
        return False


def _call_stop(inst, timeout: float = 10.0) -> bool:
    """调用插件实例的 on_stop（支持同步/异步），返回 on_stop 是否干净结束。

    事件循环线程外：异步 on_stop 经 run_coroutine_threadsafe 有界等待；
    超时会 cancel 旧协程并返回 False。
    事件循环线程内：不能阻塞等待，只能异步调度并返回 False。
    返回 False 时上层（unload_plugin / reload_plugin）必须拒绝继续卸载/重载，
    否则旧实例的 on_stop 协程会和新实例并存。
    """
    method = getattr(inst, 'on_stop', None)
    if method is None:
        return True
    if not inspect.iscoroutinefunction(method):
        try:
            method()
            return True
        except BaseException:
            print('[卸载] on_stop 执行异常:')
            traceback.print_exc()
            return False
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False
    if in_loop:
        # 不能在此阻塞，但也绝不能直接跳过：把 on_stop 作为独立 task
        # 保住强引用，unload 完成后再跑清理（否则 MCP/线程全泄漏）
        task = asyncio.get_running_loop().create_task(method())
        _PENDING_STOPS.add(task)
        task.add_done_callback(_PENDING_STOPS.discard)
        print('[卸载] 在事件循环线程内，on_stop 已改为异步调度（本次卸载判定为未完成）')
        return False
    loop = globals.event_loop
    if loop is not None and loop.is_running():
        try:
            future = asyncio.run_coroutine_threadsafe(method(), loop)
        except BaseException:
            print('[卸载] 提交 on_stop 到事件循环失败:')
            traceback.print_exc()
            return False
        return _wait_stop_future(future, timeout)
    print('[卸载] 事件循环不可用，跳过异步 on_stop')
    return False


def _clear_plugin_modules(plugin_dir: str) -> list:
    """删除 __file__ 位于插件目录内的所有已导入模块。

    相比只删顶层模块名，这能一并清除 Core.*、api.*、regs 等子模块缓存。
    """
    target = os.path.abspath(plugin_dir).lower()
    prefix = target + os.sep.lower()
    removed = []
    for mod_name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        file_path = getattr(mod, '__file__', None)
        if not file_path:
            continue
        mod_dir = os.path.dirname(os.path.abspath(file_path)).lower()
        if mod_dir == target or mod_dir.startswith(prefix):
            removed.append(mod_name)
    for mod_name in removed:
        del sys.modules[mod_name]
    return removed


def _clear_pycaches(plugin_dir: str):
    """递归删除插件目录下所有 __pycache__。"""
    count = 0
    for root, dirs, _files in os.walk(plugin_dir):
        for d in list(dirs):
            if d == '__pycache__':
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                dirs.remove(d)
                count += 1
    return count


class PluginManager:
    """插件管理器"""
    
    def __init__(self, websocket: websockets.ClientConnection):
        self.websocket = websocket
        self.plugins = {}
        self.plugin_info = {}
        self.event_handlers = {}
        # 在跑的 on_stop 兄弟 task（外层被取消时它们仍在自己的 task 上下文里跑完）
        self._bg_stops: set = set()
    
    def load_all_plugins(self):
        """加载 plugins 目录下的所有插件（基于文件夹）"""
        try:
            plugins_path = get_plugins_path()
            
            if not os.path.exists(plugins_path):
                print(f"插件目录不存在: {plugins_path}")
                return
            
            # 遍历 plugins 目录下的所有子文件夹
            for item in os.listdir(plugins_path):
                item_path = os.path.join(plugins_path, item)
                
                # 跳过非目录、__pycache__、以下划线开头的目录
                if not os.path.isdir(item_path):
                    continue
                if item.startswith('__') or item.startswith('.'):
                    continue
                
                try:
                    self.load_plugin_by_folder(item, plugins_path)
                except Exception as e:
                    print(f"加载插件 {item} 失败: {e}")
                    import traceback
                    traceback.print_exc()
            
            print(f"共加载 {len(self.plugins)} 个插件")
        except Exception as e:
            print(f"加载插件时出错: {e}")
            import traceback
            traceback.print_exc()
    
    def load_plugin_by_folder(self, folder_name: str, plugins_path: str):
        """
        通过文件夹加载插件
        
        :param folder_name: 插件文件夹名称（例如 hello）
        :param plugins_path: plugins 目录路径
        """
        plugin_dir = os.path.join(plugins_path, folder_name)
        main_file = os.path.join(plugin_dir, f"{folder_name}.py")
        
        # 检查主文件是否存在
        if not os.path.exists(main_file):
            print(f"[{folder_name}] 警告: 找不到主文件 {folder_name}.py，跳过")
            return
        
        # 检查并安装依赖到独立的 lib 目录
        requirements_file = os.path.join(plugin_dir, 'requirements.txt')
        if os.path.exists(requirements_file):
            print(f"[{folder_name}] 发现 requirements.txt")
            success = install_to_plugin_lib(requirements_file, folder_name, plugin_dir)
            
            if not success:
                print(f"[{folder_name}] 依赖安装失败，跳过此插件")
                self.plugin_info[folder_name] = {
                    'name': folder_name,
                    'description': '依赖安装失败',
                    'version': '未知',
                    'enabled': False,
                    'module_name': f"plugins.{folder_name}.{folder_name}",
                    'error': '依赖安装失败，请查看控制台输出'
                }
                return
        
        # 将插件的 lib 目录添加到 sys.path
        add_plugin_lib_to_path(plugin_dir, folder_name)
        
        # 确保插件目录有 __init__.py
        init_file = os.path.join(plugin_dir, '__init__.py')
        if not os.path.exists(init_file):
            # 自动创建空的 __init__.py
            try:
                with open(init_file, 'w', encoding='utf-8') as f:
                    f.write('# Auto-generated by plugin loader\n')
                print(f"[{folder_name}] 已自动创建 __init__.py")
            except Exception as e:
                print(f"[{folder_name}] 创建 __init__.py 失败: {e}")
                return
        
        # 将 plugins 目录添加到 sys.path（如果还没有）
        if plugins_path not in sys.path:
            sys.path.insert(0, plugins_path)
        
        # 导入模块：plugins.folder_name.folder_name
        module_name = f"plugins.{folder_name}.{folder_name}"
        
        try:
            module = importlib.import_module(module_name)
        except ImportError as e:
            print(f"导入插件 {module_name} 失败: {e}")
            self.plugin_info[folder_name] = {
                'name': folder_name,
                'description': '插件加载失败',
                'version': '未知',
                'enabled': False,
                'module_name': module_name,
                'error': f'导入失败: {str(e)}'
            }
            return
        
        # 查找模块中的 main 类
        for name, obj in inspect.getmembers(module):
            if name == 'main' and inspect.isclass(obj):
                # 为插件创建专属的 Bot 包装器（自动注入插件名）
                plugin_bot_wrapper = PluginBotWrapper(globals.bot_instance, folder_name)
                
                # 实例化插件（传入 websocket 和包装后的bot）
                plugin_instance = obj(self.websocket, plugin_bot_wrapper)
                
                # 存储插件实例
                self.plugins[folder_name] = plugin_instance
                
                # 获取插件信息
                plugin_name = getattr(plugin_instance, 'name', folder_name)
                # 更新包装器中的插件名为实际名称
                plugin_bot_wrapper._plugin_name = plugin_name
                plugin_bot_wrapper.Logger.plugin_name = plugin_name
                
                plugin_info = {
                    'name': plugin_name,
                    'description': getattr(plugin_instance, 'description', '暂无描述'),
                    'version': getattr(plugin_instance, 'version', '1.0.0'),
                    'enabled': True,
                    'module_name': module_name,
                    'folder': folder_name
                }
                self.plugin_info[folder_name] = plugin_info
                
                # 调用插件的 on_start 方法（如果存在）
                if hasattr(plugin_instance, 'on_start'):
                    try:
                        on_start_method = getattr(plugin_instance, 'on_start')
                        if inspect.iscoroutinefunction(on_start_method):
                            # 异步方法：尝试在当前线程或通过线程安全调度执行
                            import asyncio
                            try:
                                loop = asyncio.get_running_loop()
                                task = asyncio.create_task(self._safe_call_on_start(on_start_method, plugin_name))
                                # 强引用保留：否则 task 可能被 GC；断开重连时
                                # ashutdown 先等这些 on_start 跑完，避免 on_stop
                                # 先停、on_start 后把后台循环重新拉起来
                                if not hasattr(self, '_on_start_tasks'):
                                    self._on_start_tasks = set()
                                self._on_start_tasks.add(task)
                                task.add_done_callback(self._on_start_tasks.discard)
                                print(f"[{plugin_name}] on_start 异步任务已创建")
                            except RuntimeError:
                                # 当前线程无运行中的事件循环 → 线程安全调度到主循环
                                if hasattr(globals, 'event_loop') and globals.event_loop is not None and globals.event_loop.is_running():
                                    asyncio.run_coroutine_threadsafe(
                                        self._safe_call_on_start(on_start_method, plugin_name),
                                        globals.event_loop
                                    )
                                    print(f"[{plugin_name}] on_start 已通过线程安全方式调度到主事件循环")
                                else:
                                    # 主循环尚未就绪，存储待执行（仅初始加载时触发）
                                    if not hasattr(globals, 'pending_on_start_tasks'):
                                        globals.pending_on_start_tasks = []
                                    globals.pending_on_start_tasks.append((on_start_method, plugin_name))
                                    print(f"[{plugin_name}] 事件循环未就绪，on_start 将延迟执行")
                        else:
                            # 同步方法，直接调用
                            on_start_method()
                            print(f"[{plugin_name}] on_start 已调用")
                    except Exception as e:
                        print(f"[{plugin_name}] on_start 调用失败: {e}")
                        import traceback
                        traceback.print_exc()
                
                # 注册插件的事件监听器
                if hasattr(plugin_instance, 'regisiter'):
                    handlers = plugin_instance.regisiter()
                    for handler in handlers:
                        event_type = handler['listen']
                        callback = handler['function']
                        
                        if event_type not in self.event_handlers:
                            self.event_handlers[event_type] = []
                        
                        # 保存插件名称和处理器的映射
                        self.event_handlers[event_type].append({
                            'callback': callback,
                            'plugin_name': plugin_name,
                            'plugin_key': folder_name
                        })
                
                print(f"[{folder_name}] 插件加载成功")
                break
        else:
            print(f"[{folder_name}] 警告: 未找到 main 类")
            self.plugin_info[folder_name] = {
                'name': folder_name,
                'description': '未找到 main 类',
                'version': '未知',
                'enabled': False,
                'module_name': module_name,
                'error': '插件文件中未定义 main 类'
            }
    
    def load_plugin(self, module_name: str):
        """
        旧的加载方法（保留兼容性）
        推荐使用 load_plugin_by_folder
        """
        plugin_key = module_name.split('.')[-1]
        plugins_path = get_plugins_path()
        
        # 导入模块
        try:
            module = importlib.import_module(module_name)
        except ImportError as e:
            print(f"导入插件 {module_name} 失败: {e}")
            self.plugin_info[plugin_key] = {
                'name': plugin_key,
                'description': '插件加载失败',
                'version': '未知',
                'enabled': False,
                'module_name': module_name,
                'error': f'导入失败: {str(e)}'
            }
            return
        
        # 查找模块中的 main 类
        for name, obj in inspect.getmembers(module):
            if name == 'main' and inspect.isclass(obj):
                # 为插件创建专属的 Bot 包装器（自动注入插件名）
                plugin_bot_wrapper = PluginBotWrapper(globals.bot_instance, plugin_key)
                
                # 实例化插件（传入 websocket 和包装后的bot）
                plugin_instance = obj(self.websocket, plugin_bot_wrapper)
                
                # 存储插件实例
                self.plugins[plugin_key] = plugin_instance
                
                # 获取插件信息
                plugin_name = getattr(plugin_instance, 'name', plugin_key)
                # 更新包装器中的插件名为实际名称
                plugin_bot_wrapper._plugin_name = plugin_name
                plugin_bot_wrapper.Logger.plugin_name = plugin_name
                
                plugin_info = {
                    'name': plugin_name,
                    'description': getattr(plugin_instance, 'description', '暂无描述'),
                    'version': getattr(plugin_instance, 'version', '1.0.0'),
                    'enabled': True,
                    'module_name': module_name
                }
                self.plugin_info[plugin_key] = plugin_info
                
                # 调用插件的 on_start 方法（如果存在）
                if hasattr(plugin_instance, 'on_start'):
                    try:
                        on_start_method = getattr(plugin_instance, 'on_start')
                        if inspect.iscoroutinefunction(on_start_method):
                            # 异步方法：尝试在当前线程或通过线程安全调度执行
                            import asyncio
                            try:
                                loop = asyncio.get_running_loop()
                                task = asyncio.create_task(self._safe_call_on_start(on_start_method, plugin_name))
                                # 强引用保留：否则 task 可能被 GC；断开重连时
                                # ashutdown 先等这些 on_start 跑完，避免 on_stop
                                # 先停、on_start 后把后台循环重新拉起来
                                if not hasattr(self, '_on_start_tasks'):
                                    self._on_start_tasks = set()
                                self._on_start_tasks.add(task)
                                task.add_done_callback(self._on_start_tasks.discard)
                                print(f"[{plugin_name}] on_start 异步任务已创建")
                            except RuntimeError:
                                # 当前线程无运行中的事件循环 → 线程安全调度到主循环
                                if hasattr(globals, 'event_loop') and globals.event_loop is not None and globals.event_loop.is_running():
                                    asyncio.run_coroutine_threadsafe(
                                        self._safe_call_on_start(on_start_method, plugin_name),
                                        globals.event_loop
                                    )
                                    print(f"[{plugin_name}] on_start 已通过线程安全方式调度到主事件循环")
                                else:
                                    # 主循环尚未就绪，存储待执行（仅初始加载时触发）
                                    if not hasattr(globals, 'pending_on_start_tasks'):
                                        globals.pending_on_start_tasks = []
                                    globals.pending_on_start_tasks.append((on_start_method, plugin_name))
                                    print(f"[{plugin_name}] 事件循环未就绪，on_start 将延迟执行")
                        else:
                            # 同步方法，直接调用
                            on_start_method()
                            print(f"[{plugin_name}] on_start 已调用")
                    except Exception as e:
                        print(f"[{plugin_name}] on_start 调用失败: {e}")
                        import traceback
                        traceback.print_exc()

                # 注册插件的事件监听器
                # 优先使用 register()，回退到已弃用的 regisiter()
                if hasattr(plugin_instance, 'register'):
                    handlers = plugin_instance.register()
                elif hasattr(plugin_instance, 'regisiter'):
                    handlers = plugin_instance.regisiter()
                else:
                    handlers = []

                if handlers:
                    for handler in handlers:
                        event_type = handler['listen']
                        callback = handler['function']

                        if event_type not in self.event_handlers:
                            self.event_handlers[event_type] = []
                        
                        # 保存插件名称和处理器的映射
                        self.event_handlers[event_type].append({
                            'callback': callback,
                            'plugin_name': plugin_name,
                            'plugin_key': plugin_key
                        })

                break
    
    async def _safe_call_on_start(self, on_start_method, plugin_name):
        """安全地调用异步 on_start 方法"""
        try:
            await on_start_method()
            print(f"[{plugin_name}] on_start 执行完成")
        except Exception as e:
            print(f"[{plugin_name}] on_start 执行失败: {e}")
            import traceback
            traceback.print_exc()
    
    def get_event_handlers(self, event_type: str) -> list:
        """获取指定事件类型的所有处理函数"""
        return self.event_handlers.get(event_type, [])
    
    def get_plugin_list(self) -> dict:
        """获取所有插件信息"""
        return self.plugin_info
    
    def _plugin_key_of(self, plugin_name: str):
        """把插件名（显示名或文件夹名）解析成 plugins/plugin_info 里的键。"""
        if plugin_name in self.plugins or plugin_name in self.plugin_info:
            return plugin_name
        for key, info in self.plugin_info.items():
            if info.get('name') == plugin_name:
                return key
        return None

    def _remove_handlers(self, plugin_key: str):
        for event_type in list(self.event_handlers.keys()):
            self.event_handlers[event_type] = [
                h for h in self.event_handlers[event_type]
                if h.get('plugin_key') != plugin_key
            ]
            if not self.event_handlers[event_type]:
                del self.event_handlers[event_type]

    def _register_handlers(self, plugin_key: str, inst) -> int:
        """（重新）注册插件实例的事件处理器；重复调用会先清掉旧条目。"""
        self._remove_handlers(plugin_key)
        if hasattr(inst, 'register'):
            handlers = inst.register()
        elif hasattr(inst, 'regisiter'):
            handlers = inst.regisiter()
        else:
            handlers = []
        name = getattr(inst, 'name', plugin_key)
        count = 0
        for handler in handlers or []:
            event_type = handler['listen']
            self.event_handlers.setdefault(event_type, []).append({
                'callback': handler['function'],
                'plugin_name': name,
                'plugin_key': plugin_key,
            })
            count += 1
        return count

    def _call_lifecycle(self, inst, method_name: str):
        """线程安全地触发 on_start / on_stop（异步方法经主循环调度）。"""
        method = getattr(inst, method_name, None)
        if method is None:
            return
        try:
            if inspect.iscoroutinefunction(method):
                try:
                    asyncio.get_running_loop()
                    in_loop = True
                except RuntimeError:
                    in_loop = False
                loop = globals.event_loop
                if in_loop:
                    # 强引用保住 task（否则可能被 GC），并纳入 _bg_stops 让
                    # adrain_background 能在关停时 gather 收尾（审计 P0-3）
                    task = asyncio.create_task(method())
                    self._bg_stops.add(task)
                    task.add_done_callback(self._bg_stops.discard)
                elif loop is not None and loop.is_running():
                    asyncio.run_coroutine_threadsafe(method(), loop)
                else:
                    print(f'[插件] 事件循环不可用，跳过 {method_name}')
            else:
                method()
        except Exception as e:
            print(f'[插件] {method_name} 执行失败: {e}')
            traceback.print_exc()

    def enable_plugin(self, plugin_name: str):
        """启用插件：恢复事件处理器并重新触发 on_start。"""
        key = self._plugin_key_of(plugin_name)
        if key is None:
            return False
        info = self.plugin_info[key]
        if info.get('enabled'):
            return True
        info['enabled'] = True
        inst = self.plugins.get(key)
        if inst is not None:
            self._register_handlers(key, inst)
            self._call_lifecycle(inst, 'on_start')
        return True

    def disable_plugin(self, plugin_name: str):
        """禁用插件：摘掉事件处理器并触发 on_stop（停掉后台服务）。"""
        key = self._plugin_key_of(plugin_name)
        if key is None:
            return False
        info = self.plugin_info[key]
        info['enabled'] = False
        self._remove_handlers(key)
        inst = self.plugins.get(key)
        if inst is not None:
            self._call_lifecycle(inst, 'on_stop')
        return True
    
    def reload_plugin(self, plugin_name: str):
        """
        重新加载指定插件（热重载）
        
        :param plugin_name: 插件文件夹名称
        :return: 是否成功
        """
        if plugin_name not in self.plugins and plugin_name not in self.plugin_info:
            print(f"[热重载] 插件 {plugin_name} 不存在")
            return False

        # 串行化热重载；模块清除与重新导入期间持有解释器 import 锁，
        # 防止其他线程的惰性 import（from Core import ...）拿到新旧混杂的模块集
        with _reload_lock:
            print(f"[热重载] 开始重新加载插件: {plugin_name}")

            try:
                # 1. 卸载旧插件；on_stop 未干净结束时拒绝继续（否则新旧实例并存）
                if not self.unload_plugin(plugin_name):
                    print(f"[热重载] 插件 {plugin_name} 的 on_stop 未干净结束，"
                          f"拒绝重新加载（避免新旧实例并存）")
                    if plugin_name in self.plugin_info:
                        self.plugin_info[plugin_name]['error'] = 'on_stop 未干净结束，已拒绝重载'
                    return False

                # 2~4. 清模块缓存 / 清 __pycache__ / 重新导入（同一把 import 锁内）
                plugin_dir = os.path.join(get_plugins_path(), plugin_name)
                _imp.acquire_lock()
                try:
                    removed = _clear_plugin_modules(plugin_dir)
                    if removed:
                        preview = ', '.join(removed[:8])
                        print(f"[热重载] 已清除 {len(removed)} 个模块缓存: {preview}")

                    if _clear_pycaches(plugin_dir):
                        print(f"[热重载] 已清除 __pycache__")

                    # 4. 重新加载插件
                    plugins_path = get_plugins_path()
                    self.load_plugin_by_folder(plugin_name, plugins_path)
                finally:
                    _imp.release_lock()

                print(f"[热重载] 插件 {plugin_name} 重新加载成功")
                return True

            except Exception as e:
                print(f"[热重载] 重新加载插件 {plugin_name} 失败: {e}")
                import traceback
                traceback.print_exc()

                # 更新插件状态为错误
                if plugin_name in self.plugin_info:
                    self.plugin_info[plugin_name]['enabled'] = False
                    self.plugin_info[plugin_name]['error'] = f'热重载失败: {str(e)}'

                return False
    
    def reload_all_plugins(self):
        """
        重新加载所有插件
        
        :return: 成功和失败的插件列表
        """
        print("[热重载] 开始重新加载所有插件")
        
        success_list = []
        failed_list = []
        
        # 获取所有插件名称列表
        plugin_names = list(self.plugin_info.keys())
        
        for plugin_name in plugin_names:
            try:
                result = self.reload_plugin(plugin_name)
                if result:
                    success_list.append(plugin_name)
                else:
                    failed_list.append(plugin_name)
            except Exception as e:
                print(f"[热重载] 插件 {plugin_name} 重载异常: {e}")
                failed_list.append(plugin_name)
        
        print(f"[热重载] 完成: 成功 {len(success_list)} 个, 失败 {len(failed_list)} 个")
        return success_list, failed_list
    
    async def ashutdown_all_plugins(self, timeout: float = 10.0):
        """事件循环线程内（连接断开时）关闭全部插件。

        每个异步 on_stop 跑在独立兄弟 task 里，本函数只 shield 等待、从不 cancel 它：
        外层任务被 CancelledError 打断时，on_stop 协程本身收不到取消，八段清理与
        MCP 生成器 aclose 全部在自己的 task 上下文里跑完（避免 anyio 跨 task 退出
        cancel scope 报错）。一个插件失败不连累其余插件。
        """
        # 先等异步 on_start 收尾：否则 on_stop 先执行、on_start 随后才把
        # 定时器/Live 订阅等后台循环重新拉起来，形成关不掉的孤儿任务
        starts = [t for t in list(getattr(self, '_on_start_tasks', set()))
                  if not t.done()]
        if starts:
            try:
                await asyncio.wait(starts, timeout=min(timeout, 5.0))
            except BaseException:
                pass
            for t in starts:
                if not t.done():
                    t.cancel()
        for key, inst in list(self.plugins.items()):
            if getattr(inst, _STOP_MARK, False):
                continue
            method = getattr(inst, 'on_stop', None)
            if method is None:
                _mark_stopped(inst)
                continue
            try:
                if inspect.iscoroutinefunction(method):
                    task = asyncio.create_task(method())
                    self._bg_stops.add(task)
                    task.add_done_callback(self._bg_stops.discard)
                    await wait_task_resilient(task, timeout)
                else:
                    method()
            except BaseException:
                # helper 理论上不抛，兜底保证后续插件不被连坐
                print(f'[关闭] 插件 {key} on_stop 异常:')
                traceback.print_exc()
            finally:
                _mark_stopped(inst)

    async def adrain_background(self, timeout: float = 5.0):
        """有界回收超时残留的 on_stop 兄弟 task（start() 收尾时调）。"""
        pending = [t for t in list(self._bg_stops) if not t.done()]
        # _call_stop 在循环线程内异步调度的 on_stop 也一并回收
        pending += [t for t in list(_PENDING_STOPS)
                    if not t.done() and t not in pending]
        if not pending:
            return
        # gather 返回 Future，ensure_future 原样接受（create_task 只收协程）
        gathered = asyncio.ensure_future(
            asyncio.gather(*pending, return_exceptions=True))
        await wait_task_resilient(gathered, timeout)

    def shutdown_all_plugins(self, timeout: float = 10.0):
        """事件循环线程外（atexit 等）关闭全部插件。"""
        for key, inst in list(self.plugins.items()):
            # ashutdown 已停过的实例静默跳过，消除 atexit 的重复 on_stop 噪音
            if getattr(inst, _STOP_MARK, False):
                continue
            _call_stop(inst, timeout=timeout)
            _mark_stopped(inst)

    def unload_plugin(self, plugin_name: str):
        """卸载指定插件。

        :return: True=卸载完成；False=on_stop 未干净结束，拒绝卸载（调用方不应继续重载）
        """
        if plugin_name in self.plugins:
            # 先触发 on_stop；超时/失败时不能删实例，否则旧 on_stop 协程会和新实例并存
            inst = self.plugins[plugin_name]
            if not _call_stop(inst):
                print(f"[卸载] 插件 {plugin_name} 的 on_stop 未干净结束，拒绝卸载（避免新旧实例并存）")
                return False
            # 标记打在旧实例上；同名新实例不受污染，atexit 仍会停新实例
            _mark_stopped(inst)
            del self.plugins[plugin_name]
            print(f"[卸载] 插件实例已移除: {plugin_name}")

        # 移出该插件的 lib 目录，避免依赖影子残留/多版本冲突
        remove_plugin_lib_from_path(
            os.path.join(get_plugins_path(), plugin_name), plugin_name)
        
        # 清理事件处理器
        removed_count = 0
        for event_type in list(self.event_handlers.keys()):
            original_count = len(self.event_handlers[event_type])
            self.event_handlers[event_type] = [
                h for h in self.event_handlers[event_type]
                if h['plugin_key'] != plugin_name
            ]
            removed_count += original_count - len(self.event_handlers[event_type])
            if not self.event_handlers[event_type]:
                del self.event_handlers[event_type]
        
        if removed_count > 0:
            print(f"[卸载] 已移除 {removed_count} 个事件处理器")
        
        # 更新插件信息状态
        if plugin_name in self.plugin_info:
            self.plugin_info[plugin_name]['enabled'] = False
        
        print(f"插件 {plugin_name} 已卸载")
        return True
    
    def scan_new_plugins(self):
        """
        扫描并加载新插件（不会重新加载已有插件）
        
        :return: 新加载的插件列表
        """
        print("[扫描] 开始扫描新插件")
        
        new_plugins = []
        plugins_path = get_plugins_path()
        
        if not os.path.exists(plugins_path):
            print("[扫描] 插件目录不存在")
            return new_plugins
        
        # 遍历 plugins 目录
        for item in os.listdir(plugins_path):
            # 跳过已加载的插件
            if item in self.plugins or item in self.plugin_info:
                continue
            
            item_path = os.path.join(plugins_path, item)
            
            # 跳过非目录、__pycache__、以下划线开头的目录
            if not os.path.isdir(item_path):
                continue
            if item.startswith('__') or item.startswith('.'):
                continue
            
            # 尝试加载新插件
            try:
                print(f"[扫描] 发现新插件: {item}")
                self.load_plugin_by_folder(item, plugins_path)
                if item in self.plugins:
                    new_plugins.append(item)
            except Exception as e:
                print(f"[扫描] 加载插件 {item} 失败: {e}")
        
        print(f"[扫描] 完成: 新加载 {len(new_plugins)} 个插件")
        return new_plugins


# 创建全局插件管理器实例
_plugin_manager = None


def get_plugin_manager() -> PluginManager:
    """获取插件管理器实例"""
    return _plugin_manager


_atexit_registered = False


def _atexit_shutdown():
    manager = get_plugin_manager()
    if manager is None:
        return
    try:
        manager.shutdown_all_plugins(timeout=3.0)
    except Exception:
        pass


def init_plugin_manager(websocket: websockets.ClientConnection):
    """初始化插件管理器并加载所有插件"""
    global _plugin_manager, _atexit_registered
    _plugin_manager = PluginManager(websocket)
    _plugin_manager.load_all_plugins()
    if not _atexit_registered:
        atexit.register(_atexit_shutdown)
        _atexit_registered = True
    return _plugin_manager


def reload_plugin(plugin_name: str) -> bool:
    """
    重新加载指定插件（模块级热重载入口）

    :param plugin_name: 插件文件夹名称
    :return: 是否成功
    """
    manager = get_plugin_manager()
    if manager is None:
        print("[热重载] 插件管理器未初始化")
        return False
    return manager.reload_plugin(plugin_name)


def reload_all_plugins():
    """
    重新加载所有插件（模块级入口）

    :return: (成功列表, 失败列表)
    """
    manager = get_plugin_manager()
    if manager is None:
        print("[热重载] 插件管理器未初始化")
        return [], []
    return manager.reload_all_plugins()


def unload_plugin(plugin_name: str) -> bool:
    """
    卸载指定插件（模块级入口）

    :param plugin_name: 插件文件夹名称
    :return: 是否成功
    """
    manager = get_plugin_manager()
    if manager is None:
        print("[卸载] 插件管理器未初始化")
        return False
    return manager.unload_plugin(plugin_name)