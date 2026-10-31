class group_info:
    id = 100001
    name = 'Group'
    memo = 'Memo'
    create_time = '1672502400'
    level = '1'
    member_count = '1'
    max_member_count = '200'

    def __init__(self,
                 id=100001,
                 name='Group',
                 memo='Memo',
                 create_time='1672502400',
                 level='1',
                 member_count='1',
                 max_member_count='200'
                 ):
        self.id = id
        self.name = name
        self.memo = memo
        self.create_time = create_time
        self.level = level
        self.member_count = member_count
        self.max_member_count = max_member_count


class friend_info:
    id = 10001
    name = 'name'
    remark = 'remark'

    def __init__(self,
                 id=10001,
                 name='name',
                 remark='remark'
                 ):
        self.id = id
        self.name = name
        self.remark = remark


class sender:
    id = 10001
    nickname = 'nickname'
    sex = 'unknown'
    age = 105
    card = 'card'
    area = 'China'
    level = '0'
    role = 'member'
    title = 'title'

    def __init__(self,
                 user_id=10001,
                 nickname='nickname',
                 sex='unknown',
                 age=105,
                 # card='card',
                 # area='China',
                 # level='0',
                 # role='member',
                 # title='title'
                 ):
        self.user_id = user_id
        self.nickname = nickname
        self.sex = sex
        self.age = age
        # self.card = card
        # self.area = area
        # self.level = level
        # self.role = role
        # self.title = title


class Message:
    send_time = '1672502400'
    self_id = 10001
    group_id = 100001
    message_type = 'private'
    sub_type = 'friend'
    message_id = 1
    user_id = 10001
    message = 'message'
    raw_message: str = 'message'
    font = 0
    temp_source = 0

    def __init__(self,
                 send_time='1672502400',
                 self_id=10001,
                 group_id=100001,
                 message_type='private',
                 sub_type='friend',
                 message_id=1,
                 user_id=10001,
                 message='message',
                 raw_message: str = 'message',
                 font=0,
                 temp_source=0,
                 sender=None,
                 # reply=None
                 ):
        self.sender = sender
        self.send_time = send_time
        self.self_id = self_id
        self.group_id = group_id
        self.message_type = message_type
        self.sub_type = sub_type
        self.message_id = message_id
        self.user_id = user_id
        self.message = message
        self.raw_message = raw_message
        self.font = font
        self.temp_source = temp_source
        # self._reply = reply

    def _reply(self, message):
        pass
        # if self.message_type == 'private':
        #     return self._reply(self.user_id, message)
        # else:
        #     return self._reply(self.group_id, message)


class Notice:
    js = {}  # 原始json
    send_time = 1672502400  # 操作时间戳
    group_id = 100001  # 统一来源群ID
    self_id = 10001  # 统一机器人ID
    notice_type = 'None'
    sub_type = 'None'
    operator_id = 10002  # 统一操作者ID
    user_id = 10001  # 统一被操作者ID
    message_id = 0  # 撤回消息ID
    # file = file()             # file对象
    duration = 1
    sender_id = 10002  # 原则上与operator_id一致
    target_id = 10001  # 原则上与user_id一致
    honor_type = 'talkative'  # 荣誉称号，龙王之类的
    title = 'Title'  # 群头衔
    card_old = 'card_old'  # 原群名片
    card_new = 'card_new'  # 新群名片
    # client = Device()     # 客户端信息
    online = True
    comment = 'comment'  # 验证消息
    flag = 'flag'  # 请求 flag, 在调用处理请求的 API 时需要传入

    def __init__(self,
                 js,
                 send_time=1672502400,
                 group_id=100001,
                 self_id=10001,
                 notice_type='None',
                 sub_type='None',
                 operator_id=10002,
                 user_id=10001,
                 message_id=0,
                 duration=1,
                 sender_id=10002,
                 target_id=10001,
                 honor_type='talkative',
                 title='Title',
                 card_old='card_old',
                 card_new='card_new',
                 online=True,
                 comment='comment',
                 flag='flag'
                 ):
        self.js = js
        self.send_time = send_time
        self.group_id = group_id
        self.self_id = self_id
        self.notice_type = notice_type
        self.sub_type = sub_type
        self.operator_id = operator_id
        self.user_id = user_id
        self.message_id = message_id
        if sub_type == 'group_upload':
            self.file = file(id=js['file']['id'], name=js['file']['name'], size=js['file']['size'],
                             busid=js['file']['busid'])
        if sub_type == 'offline_file':
            self.file = file(id=js['file']['id'], name=js['file']['name'], size=js['file']['size'],
                             url=js['file']['url'])
        self.duration = duration
        self.sender_id = sender_id
        self.target_id = target_id
        self.honor_type = honor_type
        self.title = title
        self.card_old = card_old
        self.card_new = card_new
        if sub_type == 'client_status':
            self.client = Device(appid=js['client'], device_kind=js['client']['device_kind'],
                                 device_name=js['client']['device_name'])
            self.online = online
        self.comment = comment
        self.flag = flag


class Device:
    app_id = 1001
    device_name = 'device_name'  # 设备名称
    device_kind = 'device_kind'  # 类型

    def __init__(self, appid=1001, device_name='device_name', device_kind='device_kine'):
        self.app_id = appid
        self.device_kind = device_kind
        self.device_name = device_name


class file:
    id = 'FileID'
    name = 'File'
    size = 1
    busid = 1
    url = 'http://File.Url'

    def __init__(self,
                 id='FileID',
                 name='File',
                 size=1,
                 busid=1,
                 url='http://File.Url'
                 ):
        self.id = id
        self.name = name
        self.size = size
        self.busid = busid
        self.url = url


class Request:
    user_id = 10001
    group_id = 100001
    sub_type = 'unknown'
    comment = 'comment'
    flag = 'flag'
    n_type = 'friend'

    def __init__(self,
                 user_id=10001,
                 group_id=100001,
                 sub_type='unknown',
                 comment='comment',
                 flag='flag',
                 reply=None,
                 n_type='friend'
                 ):
        self.user_id = user_id
        self.group_id = group_id
        self.sub_type = sub_type
        self.comment = comment
        self.flag = flag
        self._reply = reply
        self.n_type = n_type

    def reply(self, approve: bool, remark="", reason=""):
        """
        快速处理request
        :param remark: 如果是好友申请，在同意时可以备注好友
        :param reason: 如果拒绝群申请，这里为拒绝原因，默认空
        :param approve: 是否通过/允许
        :return:
        """
        if self.n_type == 'friend':
            self._reply(self.flag, approve, remark=remark)
        elif self.n_type == 'group':
            self._reply(self.flag, self.sub_type, approve, reason=reason)


# ============================================================
#  结构化事件类 — 替代原始 dict 传递给插件
# ============================================================

class Event:
    """事件基类 — 所有 OneBot 事件的父类"""

    def __init__(self, raw_event: dict):
        self._raw = raw_event           # 保留原始 JSON，兼容旧代码
        self.post_type = raw_event.get('post_type', '')
        self.self_id = raw_event.get('self_id', 0)
        self.time = raw_event.get('time', 0)
        self._plugin_name = ''          # 由事件分发器注入


class MessageEvent(Event):
    """消息事件（群聊 / 私聊）"""

    def __init__(self, raw_event: dict):
        super().__init__(raw_event)
        self.message_type = raw_event.get('message_type', '')
        self.sub_type = raw_event.get('sub_type', '')
        self.message_id = raw_event.get('message_id', 0)
        self.user_id = raw_event.get('user_id', 0)
        self.group_id = raw_event.get('group_id', None)
        self.raw_message = raw_event.get('raw_message', '')
        self.message = raw_event.get('message', '')
        self.font = raw_event.get('font', 0)
        sender_data = raw_event.get('sender', {})
        self.sender = sender(
            user_id=sender_data.get('user_id', 0),
            nickname=sender_data.get('nickname', '')
        )


class NoticeEvent(Event):
    """通知事件（群成员变动、文件上传、禁言等）"""

    def __init__(self, raw_event: dict):
        super().__init__(raw_event)
        self.notice_type = raw_event.get('notice_type', '')
        self.sub_type = raw_event.get('sub_type', '')
        self.group_id = raw_event.get('group_id', None)
        self.user_id = raw_event.get('user_id', 0)
        self.operator_id = raw_event.get('operator_id', 0)


class RequestEvent(Event):
    """请求事件（好友申请、加群申请/群邀请）

    审批不走进站回复（正向 WebSocket 不支持快速操作），而是事后调
    set_friend_add_request / set_group_add_request，flag 原样回传。
    """

    def __init__(self, raw_event: dict):
        super().__init__(raw_event)
        self.request_type = str(raw_event.get('request_type', '') or '')
        # friend 事件没有 sub_type；group 事件为 'add'（申请入群）或 'invite'（被邀请）
        self.sub_type = str(raw_event.get('sub_type', '') or '')
        self.user_id = raw_event.get('user_id', 0)
        # 好友申请没有 group_id
        self.group_id = raw_event.get('group_id', None)
        self.comment = str(raw_event.get('comment', '') or '')
        self.flag = str(raw_event.get('flag', '') or '')


class MetaEvent(Event):
    """元事件（心跳、生命周期）"""

    def __init__(self, raw_event: dict):
        super().__init__(raw_event)
        self.meta_event_type = raw_event.get('meta_event_type', '')
        self.sub_type = raw_event.get('sub_type', '')
