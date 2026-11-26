"""消息历史缓存。

- 按会话（群 ``g:<id>`` / 私聊 ``p:<id>``）保存最近 ``_BUCKET_MAX`` 条消息
- ``message_id → entry`` 的 LRU 索引，供按消息 ID 取历史消息
- 全局字节预算 ``_MAX_BYTES``：raw_message 原文是最大的常驻内存来源，
  仅按会话条数淘汰不够（100 条长文本也会占几十 MB），因此同时按总字节数
  从最旧会话的最旧消息开始淘汰

记录时机：``core/events.py`` 构造 MessageEvent 之后、插件分发之前。
仅供本进程内存使用，重启即清空。
"""
import time
from collections import OrderedDict, deque

_BUCKET_MAX = 100
_INDEX_MAX = 4096
# 会话数量上限，防止群/私聊极多时 key 无限增长
_BUCKETS_MAX = 2000
# 全部缓存文本的总字节预算（按 UTF-8 编码后的长度粗略估算）
_MAX_BYTES = 64 * 1024 * 1024
# 每条记录除文本外的估算开销（message_id/nickname/time/dict 头部等）
_ENTRY_OVERHEAD = 160

# 会话 key → deque[entry]（OrderedDict：超限时淘汰最久未写入的会话）
_buckets: 'OrderedDict[str, deque]' = OrderedDict()
# message_id → entry（LRU）
_id_index: OrderedDict = OrderedDict()
# 当前所有「仍在缓存中」的 entry 文本总字节数（entry 被桶/索引引用计数去重）
_total_bytes = 0


def _bucket_key(event) -> str:
    if event.message_type == 'group':
        return f'g:{event.group_id}'
    return f'p:{event.user_id}'


def _retain(entry) -> None:
    """新增一处引用；只在 0→1 时计入总字节，避免同一 entry 被桶和索引重复计。"""
    global _total_bytes
    refs = entry.get('_refs', 0) + 1
    entry['_refs'] = refs
    if refs == 1:
        _total_bytes += entry['_size']


def _release(entry) -> None:
    """释放一处引用；引用归零时从总字节中扣除。"""
    global _total_bytes
    refs = entry.get('_refs', 0) - 1
    entry['_refs'] = refs if refs > 0 else 0
    if refs <= 0:
        _total_bytes -= entry['_size']


def _unlink_index(message_id, entry) -> None:
    """索引仍指向该 entry 时摘掉并释放索引这一份引用。"""
    if message_id is not None and _id_index.get(message_id) is entry:
        _id_index.pop(message_id, None)
        _release(entry)


def _evict_bucket(bucket) -> None:
    """整桶淘汰：逐条释放桶与索引的引用。"""
    while bucket:
        entry = bucket.popleft()
        _release(entry)
        _unlink_index(entry.get('message_id'), entry)


def record(event):
    """记录一条 MessageEvent。"""
    nickname = ''
    if event.sender is not None:
        nickname = getattr(event.sender, 'nickname', '') or ''
    text = event.raw_message or ''
    entry = {
        'message_id': event.message_id,
        'user_id': event.user_id,
        'nickname': nickname,
        'text': text,
        'time': event.time or int(time.time()),
        '_size': len(str(text).encode('utf-8', 'replace')) + _ENTRY_OVERHEAD,
        '_refs': 0,
    }
    key = _bucket_key(event)
    bucket = _buckets.get(key)
    if bucket is None:
        bucket = deque()
        _buckets[key] = bucket
        while len(_buckets) > _BUCKETS_MAX:
            _, old_bucket = _buckets.popitem(last=False)
            _evict_bucket(old_bucket)
    else:
        _buckets.move_to_end(key)

    _retain(entry)
    bucket.append(entry)
    while len(bucket) > _BUCKET_MAX:
        old_entry = bucket.popleft()
        _release(old_entry)
        _unlink_index(old_entry.get('message_id'), old_entry)

    if event.message_id is not None:
        # 同一个 message_id 重复记录时先摘掉旧索引项，避免索引无限增长
        old_indexed = _id_index.pop(event.message_id, None)
        if old_indexed is not None:
            _release(old_indexed)
        _id_index[event.message_id] = entry
        _retain(entry)
        while len(_id_index) > _INDEX_MAX:
            _, old_entry = _id_index.popitem(last=False)
            _release(old_entry)

    _enforce_budget(entry)


def _enforce_budget(keep=None) -> None:
    """按总字节预算淘汰：从最旧会话的最旧消息开始删。

    :param keep: 刚写入的 entry，绝不淘汰它（单条就超预算时缓存退化为只留最新一条）。
    """
    global _total_bytes
    while _total_bytes > _MAX_BYTES and _buckets:
        key = next(iter(_buckets))
        bucket = _buckets[key]
        if not bucket:
            _buckets.pop(key, None)
            continue
        old_entry = bucket[0]
        if old_entry is keep:
            # 只剩刚写入的这条：再删就会丢最新消息，宁可暂时超出预算
            break
        bucket.popleft()
        _release(old_entry)
        _unlink_index(old_entry.get('message_id'), old_entry)
        if not bucket:
            _buckets.pop(key, None)


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
    global _total_bytes
    _buckets.clear()
    _id_index.clear()
    _total_bytes = 0
