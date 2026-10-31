import _imp
import importlib
import inspect
import threading
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


# 热重载串行锁：防止两个重载并发清理/重建 sys.modules
_reload_lock = threading.RLock()


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
        specs.append((op, tuple(int(x) if x.isdigit() else x
                                for x in re.split(r'[.]', ver))))
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
                ver_parts = tuple(int(x) if x.isdigit() else x
                                  for x in re.split(r'[.]', parts[1]))
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
            requirements = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        
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

        print(f"[{plugin_name}] 正在安装依赖到 lib 目录（实时输出 pip 日志）...")
        # 实时流式输出：逐行读 pip 的 stdout/stderr 转发到控制台/日志窗口，
        # 同时缓存末尾若干行用于失败诊断（不再 --quiet 黑箱等待）
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
        )
        tail_lines = []
        deadline = time.time() + 300
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                print(f"[{plugin_name}|pip] {line}", flush=True)
                tail_lines.append(line)
                if len(tail_lines) > 40:
                    tail_lines.pop(0)
                if time.time() > deadline:
                    proc.kill()
                    raise subprocess.TimeoutExpired(cmd, 300)
            proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()

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
    
    async def send_message(self, message, target_id=None, message_type="private", **kwargs):
        """发送消息（自动注入plugin_name）"""
        return await self._bot.send_message(
            message, target_id, message_type, 
            plugin_name=self._plugin_name
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


def _call_stop(inst, timeout: float = 10.0):
    """调用插件实例的 on_stop（支持同步/异步）。

    事件循环线程外：异步 on_stop 经 run_coroutine_threadsafe 阻塞等待。
    事件循环线程内：无法阻塞等待，跳过（在循环线程内请用
    PluginManager.ashutdown_all_plugins 直接 await）。
    """
    method = getattr(inst, 'on_stop', None)
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
                print('[卸载] 警告: 在事件循环线程内无法同步等待 on_stop，已跳过')
                return
            if loop is not None and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(method(), loop)
                future.result(timeout=timeout)
            else:
                print('[卸载] 事件循环不可用，跳过异步 on_stop')
        else:
            method()
    except Exception:
        print('[卸载] on_stop 执行异常:')
        traceback.print_exc()


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
    
    def enable_plugin(self, plugin_name: str):
        """启用插件"""
        if plugin_name in self.plugin_info:
            self.plugin_info[plugin_name]['enabled'] = True
            return True
        return False
    
    def disable_plugin(self, plugin_name: str):
        """禁用插件"""
        if plugin_name in self.plugin_info:
            self.plugin_info[plugin_name]['enabled'] = False
            # 清理事件处理器（使用 plugin_key 精确匹配）
            for event_type in list(self.event_handlers.keys()):
                original_count = len(self.event_handlers[event_type])
                self.event_handlers[event_type] = [
                    h for h in self.event_handlers[event_type]
                    if h['plugin_key'] != plugin_name
                ]
                if not self.event_handlers[event_type]:
                    del self.event_handlers[event_type]
            return True
        return False
    
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
                # 1. 卸载旧插件
                self.unload_plugin(plugin_name)

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
        """事件循环线程内（连接断开时）关闭全部插件——直接 await 异步 on_stop。"""
        for key, inst in list(self.plugins.items()):
            method = getattr(inst, 'on_stop', None)
            if method is None:
                continue
            try:
                if inspect.iscoroutinefunction(method):
                    await asyncio.wait_for(method(), timeout=timeout)
                else:
                    method()
            except Exception:
                print(f'[关闭] 插件 {key} on_stop 异常:')
                traceback.print_exc()

    def shutdown_all_plugins(self, timeout: float = 10.0):
        """事件循环线程外（atexit 等）关闭全部插件。"""
        for key, inst in list(self.plugins.items()):
            _call_stop(inst, timeout=timeout)

    def unload_plugin(self, plugin_name: str):
        """卸载指定插件"""
        if plugin_name in self.plugins:
            # 先触发 on_stop
            _call_stop(self.plugins[plugin_name])
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