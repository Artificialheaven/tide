"""消息历史缓存。

- 按会话（群 ``g:<id>`` / 私聊 ``p:<id>``）保存最近 ``_BUCKET_MAX`` 条消息
- ``message_id → entry`` 的 LRU 索引，供按消息 ID 取历史消息

记录时机：``core/events.py`` 构造 MessageEvent 之后、插件分发之前。
仅供本进程内存使用，重启即清空。
"""
import time
from collections import OrderedDict, deque

_BUCKET_MAX = 100
_INDEX_MAX = 4096
# 会话数量上限，防止群/私聊极多时 key 无限增长
_BUCKETS_MAX = 2000

# 会话 key → deque[entry]（OrderedDict：超限时淘汰最久未写入的会话）
_buckets: 'OrderedDict[str, deque]' = OrderedDict()
# message_id → entry（LRU）
_id_index: OrderedDict = OrderedDict()


def _bucket_key(event) -> str:
    if event.message_type == 'group':
        return f'g:{event.group_id}'
    return f'p:{event.user_id}'


def record(event):
    """记录一条 MessageEvent。"""
    nickname = ''
    if event.sender is not None:
        nickname = getattr(event.sender, 'nickname', '') or ''
    entry = {
        'message_id': event.message_id,
        'user_id': event.user_id,
        'nickname': nickname,
        'text': event.raw_message or '',
        'time': event.time or int(time.time()),
    }
    key = _bucket_key(event)
    bucket = _buckets.get(key)
    if bucket is None:
        bucket = deque(maxlen=_BUCKET_MAX)
        _buckets[key] = bucket
        while len(_buckets) > _BUCKETS_MAX:
            _buckets.popitem(last=False)
    else:
        _buckets.move_to_end(key)
    bucket.append(entry)

    if event.message_id is not None:
        _id_index[event.message_id] = entry
        _id_index.move_to_end(event.message_id)
        while len(_id_index) > _INDEX_MAX:
            _id_index.popitem(last=False)


def get_by_id(message_id) -> str:
    """按 message_id 取消息文本，无缓存返回 ''。"""
    entry = _id_index.get(message_id)
    if entry is None:
        return ''
    return entry['text']


def get_by_offset(kind: str, target, offset: int) -> str:
    """按相对偏移取会话历史消息文本。

    :param kind: 'g'（群）/ 'p'（私聊）
    :param target: 群号 / QQ 号
    :param offset: ≤0；0=最新一条（触发消息本身），-1=上一条，-2=上两条……
    """
    bucket = _buckets.get(f'{kind}:{target}')
    if not bucket:
        return ''
    idx = len(bucket) - 1 + offset
    if idx < 0:
        return ''
    return bucket[idx]['text']


def clear():
    """清空全部历史（测试用）。"""
    _buckets.clear()
    _id_index.clear()
