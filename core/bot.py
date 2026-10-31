import uuid
import asyncio
import json
from core import globals
from core.stats import stats_tracker


class logger:
    """日志记录器"""
    
    def __init__(self, add_log_callback, plugin_name=None):
        """
        初始化日志记录器
        :param add_log_callback: 添加日志的回调函数，签名为 add_log(response, source, content, color)
        :param plugin_name: 插件名称（可选），如果提供则固定为来源
        """
        self.add_log = add_log_callback
        self.plugin_name = plugin_name
        print("[Logger] Logger已初始化")

    def info(self, response, source, content):
        """普通日志（白色）"""
        # 如果是插件调用，强制使用插件名作为来源
        if self.plugin_name:
            source = self.plugin_name
        self.add_log(response, source, content, '默认')

    def warning(self, response, source, content):
        """警告日志（黄色）"""
        # 如果是插件调用，强制使用插件名作为来源
        if self.plugin_name:
            source = self.plugin_name
        self.add_log(response, source, content, '黄色')

    def error(self, response, source, content):
        """错误日志（红色）"""
        # 如果是插件调用，强制使用插件名作为来源
        if self.plugin_name:
            source = self.plugin_name
        self.add_log(response, source, content, '红色')

    def custom(self, response, source, content, color):
        """自定义颜色日志"""
        # 如果是插件调用，强制使用插件名作为来源
        if self.plugin_name:
            source = self.plugin_name
        self.add_log(response, source, content, color)


class bot:
    """Bot实例"""
    
    def __init__(self, websocket, Logger):
        """
        初始化Bot
        :param websocket: WebSocket连接
        :param Logger: 日志记录器实例
        """
        self.websocket = websocket
        self.Logger = Logger
        self.pending_requests = {}
        self.api_semaphore = asyncio.Semaphore(10)  # 限制同时进行的API调用数量为10
        self._stats = stats_tracker  # 全局单例：重连后累计数延续
        
        print("[Bot] Bot已初始化")
        self.Logger.info("框架", "Bot", "Bot已初始化")

    def _generate_echo(self):
        """生成唯一的echo标识"""
        return str(uuid.uuid4())

    @property
    def bot_qq(self):
        """获取Bot的QQ号"""
        return str(globals.bot_id) if globals.bot_id else "未知"

    def increment_sent(self):
        """增加发送消息计数"""
        self._stats.increment_sent()

    def increment_received(self):
        """增加接收消息计数"""
        self._stats.increment_received()

    def get_stats(self):
        """获取消息统计信息"""
        return self._stats.get_stats()

    async def send_message(self, message, target_id=None, message_type="private", plugin_name=None):
        """
        发送消息
        
        :param message: 发送的消息内容
        :param target_id: 发送的目标ID（用户ID或群ID）
        :param message_type: private（私聊）或 group（群聊）
        :param plugin_name: 插件名称（可选），用于在日志中显示来源
        :return: 服务端的响应数据
        """
        echo = self._generate_echo()
        
        # 来源是插件名，如果没有则显示为Bot
        source = plugin_name if plugin_name else "Bot"
        # 响应是Bot的QQ号（从globals获取）
        response = self.bot_qq
        
        self.Logger.info(response, source, f"发送消息到 {target_id}: {message}")
        
        # 创建Future用于等待响应
        future = asyncio.Future()
        self.pending_requests[echo] = future
        
        try:
            # 构建参数
            params = {
                "message": message
            }
            
            if message_type == "private":
                params["user_id"] = target_id
            elif message_type == "group":
                params["group_id"] = target_id
            
            # 发送消息 - 使用正确的JSON格式
            data = {
                "action": "send_msg",
                "params": params,
                "echo": echo
            }
            
            # 使用 json.dumps 而不是 str() 来确保正确的JSON格式
            await self.websocket.send(json.dumps(data, ensure_ascii=False))
            # self.Logger.info(response, source, f"消息已发送 (echo: {echo})")
            
            # 增加发送计数
            self.increment_sent()
            
            # 等待响应（设置超时时间30秒）
            resp = await asyncio.wait_for(future, timeout=30.0)
            self.Logger.warning(response, source, f"收到响应 (echo: {echo})")
            
            return resp
            
        except asyncio.TimeoutError:
            self.Logger.error(response, source, f"请求超时 (echo: {echo})")
            self.pending_requests.pop(echo, None)
            raise Exception("请求超时")
        except Exception as e:
            self.Logger.error(response, source, f"发送消息失败: {e}")
            if echo in self.pending_requests:
                del self.pending_requests[echo]
            raise

    async def get_group_list(self, plugin_name=None, no_cache=False):
        """
        获取群列表
        
        :param plugin_name: 插件名称（可选）
        :param no_cache: 是否不使用缓存
        :return: 群列表数组
        """
        params = {"no_cache": no_cache}
        return await self.call_api("get_group_list", params, plugin_name)

    async def get_friend_list(self, plugin_name=None):
        """
        获取好友列表
        
        :param plugin_name: 插件名称（可选）
        :return: 好友列表数组
        """
        return await self.call_api("get_friend_list", {}, plugin_name)

    async def get_group_info(self, group_id, plugin_name=None, no_cache=False):
        """
        获取群信息
        
        :param group_id: 群号
        :param plugin_name: 插件名称（可选）
        :param no_cache: 是否不使用缓存
        :return: 群信息
        """
        params = {
            "group_id": group_id,
            "no_cache": no_cache
        }
        return await self.call_api("get_group_info", params, plugin_name)

    async def get_group_member_list(self, group_id, plugin_name=None, no_cache=False):
        """
        获取群成员列表
        
        :param group_id: 群号
        :param plugin_name: 插件名称（可选）
        :param no_cache: 是否不使用缓存
        :return: 群成员列表数组
        """
        params = {
            "group_id": group_id,
            "no_cache": no_cache
        }
        return await self.call_api("get_group_member_list", params, plugin_name)

    async def get_group_member_info(self, group_id, user_id, plugin_name=None, no_cache=False):
        """
        获取群成员信息
        
        :param group_id: 群号
        :param user_id: QQ号
        :param plugin_name: 插件名称（可选）
        :param no_cache: 是否不使用缓存
        :return: 群成员信息
        """
        params = {
            "group_id": group_id,
            "user_id": user_id,
            "no_cache": no_cache
        }
        return await self.call_api("get_group_member_info", params, plugin_name)

    async def get_login_info(self, plugin_name=None):
        """
        获取登录号信息
        
        :param plugin_name: 插件名称（可选）
        :return: 登录号信息
        """
        return await self.call_api("get_login_info", {}, plugin_name)

    async def call_api(self, action, params=None, plugin_name=None):
        """
        通用API调用方法（带并发控制）
        
        :param action: API动作名称
        :param params: 参数字典
        :param plugin_name: 插件名称（可选），用于在日志中显示来源
        :return: 服务端的响应数据
        """
        if params is None:
            params = {}
        
        # 使用信号量限制并发数量，防止过多API调用堵塞
        async with self.api_semaphore:
            echo = self._generate_echo()
            
            # 来源是插件名，如果没有则显示为Bot
            source = plugin_name if plugin_name else "Bot"
            # 响应是Bot的QQ号（从globals获取）
            response = self.bot_qq
            
            # self.Logger.info(response, source, f"调用API: {action}")
            
            # 创建Future用于等待响应
            future = asyncio.Future()
            self.pending_requests[echo] = future
            
            try:
                # 使用正确的JSON格式
                data = {
                    "action": action,
                    "params": params,
                    "echo": echo
                }
                
                await self.websocket.send(json.dumps(data, ensure_ascii=False))
                # self.Logger.info(response, source, f"API调用已发送 (echo: {echo})")
                
                # 等待响应
                resp = await asyncio.wait_for(future, timeout=30.0)
                # self.Logger.info(response, source, f"收到API响应 (echo: {echo})")
                
                return resp
                
            except asyncio.TimeoutError:
                self.Logger.error(response, source, f"API调用超时: {action} (echo: {echo})")
                self.pending_requests.pop(echo, None)
                raise Exception(f"API调用超时: {action}")
            except Exception as e:
                self.Logger.error(response, source, f"API调用失败: {e}")
                if echo in self.pending_requests:
                    del self.pending_requests[echo]
                raise

    async def call_api_parallel(self, api_calls, plugin_name=None):
        """
        并行调用多个API（非阻塞）
        
        :param api_calls: API调用列表，每个元素是 (action, params) 元组
        :param plugin_name: 插件名称（可选）
        :return: 响应列表
        :example:
            results = await bot.call_api_parallel([
                ("get_group_info", {"group_id": 123}),
                ("get_group_info", {"group_id": 456}),
            ], plugin_name="我的插件")
        """
        tasks = []
        for action, params in api_calls:
            task = asyncio.create_task(self.call_api(action, params, plugin_name))
            tasks.append(task)
        
        # 并行执行所有任务
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return results

    async def call_api_batch(self, action, params_list, plugin_name=None):
        """
        批量调用同一个API（非阻塞）
        
        :param action: API动作名称
        :param params_list: 参数列表
        :param plugin_name: 插件名称（可选）
        :return: 响应列表
        :example:
            results = await bot.call_api_batch(
                "get_group_info",
                [{"group_id": 123}, {"group_id": 456}],
                plugin_name="我的插件"
            )
        """
        tasks = []
        for params in params_list:
            task = asyncio.create_task(self.call_api(action, params, plugin_name))
            tasks.append(task)
        
        # 并行执行所有任务
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return results

    def handle_response(self, echo, response):
        """
        处理服务端响应
        
        :param echo: 响应标识
        :param response: 响应数据
        """
        if echo in self.pending_requests:
            future = self.pending_requests[echo]
            if not future.done():
                future.set_result(response)
            del self.pending_requests[echo]
            return True
        return False
