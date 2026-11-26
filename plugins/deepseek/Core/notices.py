# -*- coding: utf-8 -*-
"""NapCat 扩展通知的处理策略（OneBot 标准之外的那批 notice）。

## 背景

NapCat 在标准 OneBot v11 的 notice 之外加了一批 `notice_type='notify'` 的扩展
（戳一戳、个人资料点赞、输入状态、群名变更、表情回应、灰条等）。这些事件没有
标准定义，但 NapCat 会照实上报；框架原本只有一个 group_increase 处理器，
其余全部静默丢弃。

本模块把它们收拢成一张明确的策略表，避免"悄悄不响应"变成"看起来坏了"：

| 事件 | 处理 |
| --- | --- |
| 戳一戳 `notify/poke` | 被戳的是自己时回戳一下；`poke_ai_on` 开启时再合成一轮对话 |
| 个人资料点赞 `notify/profile_like` | **不响应**（用户明确要求；也不落库、不唤醒） |
| 输入状态 `notify/input_status` | 忽略（高频、无信息量） |
| 群名变更 `notify/group_name` | 同步本地 groups.group_name，不唤醒 |
| 群表情回应 `group_msg_emoji_like` | 忽略（不唤醒，避免刷屏） |
| 群精华 / 灰条 `essence` / `group_gray_tip` | 忽略 |
| 机器人离线 `bot_offline` | 只记日志（连接层会自己重连） |
| 其他 | 返回 False，交回上层（入群侦查等） |

策略都是"消费掉但不回话"，不会因为一个点赞就把模型叫起来烧 token。
"""
from __future__ import annotations

from . import runtime_env, storage

# 被戳一戳后的回戳动作（NapCat 群/私聊分开两个 action）
_POKE_ACTIONS = {'group': 'group_poke', 'private': 'friend_poke'}


def _raw(event) -> dict:
    return getattr(event, '_raw', {}) or {}


def notice_of(event) -> tuple:
    """(notice_type, sub_type)；NapCat 的扩展通知 sub_type 就是 poke 等。"""
    raw = _raw(event)
    nt = str(getattr(event, 'notice_type', '')
             or raw.get('notice_type') or '')
    st = str(getattr(event, 'sub_type', '') or raw.get('sub_type') or '')
    return nt, st


def group_name_of(event) -> tuple:
    """群名变更 → (gid, name_new)；不是这类事件返回 ('','')。"""
    nt, st = notice_of(event)
    if nt not in ('notify', 'group_name') or st not in ('group_name', 'name'):
        return '', ''
    raw = _raw(event)
    gid = str(raw.get('group_id') or getattr(event, 'group_id', '') or '')
    name = str(raw.get('name_new') or raw.get('name') or raw.get('group_name')
               or getattr(event, 'name_new', '') or '')
    return (gid, name) if gid and name else ('', '')


def poke_of(event) -> dict | None:
    """戳一戳 → {group_id,user_id,target_id}；不是戳一戳/戳的不是自己返回 None。"""
    nt, st = notice_of(event)
    if nt != 'notify' or st != 'poke':
        return None
    raw = _raw(event)
    uid = str(raw.get('user_id') or getattr(event, 'user_id', '') or '')
    tid = str(raw.get('target_id') or raw.get('target') or '')
    gid = str(raw.get('group_id') or getattr(event, 'group_id', '') or '')
    return {'group_id': gid, 'user_id': uid, 'target_id': tid}


async def handle(plugin, event) -> bool:
    """处理一条扩展通知。返回 True 表示已消费（上层不必再处理）。"""
    nt, st = notice_of(event)
    raw = _raw(event)
    cfg = plugin.cfg
    if not cfg.get('notice_ext_on', True):
        return False

    # 群名变更：与 AI 无关，但群列表/提示词里的群名要跟着变
    gid, name = group_name_of(event)
    if gid:
        if cfg.get('group_name_sync_on', True):
            try:
                storage.upsert_group(plugin.db_path, gid, group_name=name)
                plugin.log(f'群名变更：{gid} → {name}')
            except Exception:
                plugin.log('同步群名失败:\n' + _tb())
        return True

    sub = st or str(raw.get('sub_type') or '')
    if nt == 'group_msg_emoji_like':
        return True          # 表情回应：不唤醒、不回话
    if nt in ('essence', 'group_gray_tip'):
        return True
    if nt == 'bot_offline':
        plugin.log('NapCat 上报 bot_offline（连接层会自动重连）', 'warn')
        return True
    if nt != 'notify':
        return False

    if sub == 'profile_like':
        # 个人资料点赞：明确不响应（用户要求）。NapCat 只在有人赞资料时上报，
        # 没有可回的动作，叫醒模型也只是白烧 token。
        return True
    if sub == 'input_status':
        return True
    if sub == 'poke':
        return await _handle_poke(plugin, event, cfg)
    return False


async def _handle_poke(plugin, event, cfg) -> bool:
    info = poke_of(event)
    if not info:
        return True
    self_id = str(getattr(plugin, 'bot_id', '') or '')
    target = info['target_id']
    if self_id and target and target != self_id:
        return True          # 别人互戳，与机器人无关
    uid = info['user_id']
    gid = info['group_id']
    if not uid or uid == self_id:
        return True
    chat_type = 'group' if gid else 'private'

    # 回戳：低打扰、符合"戳一戳"的社交语义
    if cfg.get('poke_back_on', True) and runtime_env.transport() == 'napcat':
        action = _POKE_ACTIONS[chat_type]
        params = {'user_id': int(uid)} if uid.isdigit() else {'user_id': uid}
        if gid and gid.isdigit():
            params['group_id'] = int(gid)
        try:
            await plugin.bot.call_api(action, params)
        except Exception:
            plugin.log('回戳失败（对方可能已不在群/被限制）:\n' + _tb(), 'warn')

    # 可选：把戳一戳也当作一次互动触发（默认关，避免被戳爆刷 token）
    if not cfg.get('poke_ai_on', False):
        return True
    engine = getattr(plugin, 'engine', None)
    if engine is None or not uid or not gid:
        return True
    row = storage.get_group(plugin.db_path, gid) or {}
    if not row.get('enabled'):
        return True
    text = (f'群友（QQ {uid}）戳了你一下（戳一戳）。你可以自然回一句，'
            '也可以只回戳（已经自动回戳过了）。')
    cfg_g = cfg
    try:
        from .config import resolve_group
        cfg_g = resolve_group(cfg, row.get('settings') or {})
    except Exception:
        cfg_g = cfg
    try:
        lock_get = getattr(plugin, 'group_lock', None)
        if callable(lock_get):
            async with lock_get(gid):      # 与群内真人轮次串行
                await _fire_poke(engine, gid, cfg_g, text, uid)
        else:
            await _fire_poke(engine, gid, cfg_g, text, uid)
    except Exception:
        plugin.log('戳一戳触发对话失败:\n' + _tb(), 'warn')
    return True


async def _fire_poke(engine, gid: str, cfg: dict, text: str, uid: str):
    await engine.handle_trigger(
        gid, cfg, text, source='戳一戳', run_kind='poke',
        actor_id=uid, actor_name='',
        notice_text=(f'[群内互动] {text}\n'
                     '这是一次轻互动，不必长篇回应；不想说话就只输出 [静默]。'))


def _tb() -> str:
    import traceback
    return traceback.format_exc()
