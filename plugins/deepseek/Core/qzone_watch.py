# -*- coding: utf-8 -*-
"""QQ 空间被动知会：轮询 → 落 qzone_events → 合成一轮对话触发。

## 为什么是轮询

NapCat 有 28 个空间 action，但**没有任何空间事件上报**（napcat-onebot 的
event 只有 message/meta/notice/request 四类，notice 也没有空间子类型）。
所以"空间里被 @ 了""说说收到评论"只能主动去拉：

- 被 @：`get_qzone_feeds`（好友动态）里筛 content 含 "@自己昵称/QQ号" 的条目；
- 新评论：`get_qzone_msg_list` 取自己最近的说说，逐条 `get_qzone_msg_detail`
  看评论树里有没有晚于游标的新评论/回复。

两条扫描都以"首次扫描时的时间"为基线，只报之后发生的事；解析出的通知先写入
`qzone_events`（`UNIQUE(tid, kind, comment_id, actor_uin)` 天然去重），再逐条
喂给 `Engine.handle_trigger` 跑一轮后台对话。模型看到的是空间上下文，可以：

- `qzone_comment(tid, content)` 直接在那条说说下回评（被 @ 的回应）；
- `qzone_comment(tid, content, comment_id, comment_uin)` 回复某条评论；
- `qzone_detail` / `qzone_images` 先看正文和配图；
- 不想理会就输出 [静默]。

游标存 `qzone_state`（键值表），重启不会把老评论再报一遍。
"""
from __future__ import annotations

import asyncio
import time
import traceback

from . import qzone as qz
from . import runtime_env, storage

# 每轮最多处理几条知会（避免一次积压把主循环占满）
MAX_EVENTS_PER_TICK = 3
# 轮询间隔下限（秒），防止配置写 0 变成死循环
MIN_INTERVAL = 30.0
# 每多少轮顺手清一次 `ccount:<tid>` 状态键（P1-1：已不存在的说说）
PRUNE_STATE_EVERY = 24


# ── 纯函数（可单测）────────────────────────────────────────────

def notify_target(cfg: dict) -> tuple:
    """知会落到哪个会话：配置了群就用群，否则发给主人私聊。"""
    gid = str((cfg or {}).get('qzone_notify_group') or '').strip()
    if gid.isdigit():
        return gid, 'group'
    return str((cfg or {}).get('owner_qq') or '').strip(), 'private'


def space_event_text(event: dict, *, self_name: str = '',
                     post_preview: str = '', images: int = 0) -> str:
    """一条 qzone_events 行 → 给模型看的事件描述（不含通知模板）。"""
    kind = str(event.get('kind') or '')
    tid = str(event.get('tid') or '')
    who = str(event.get('actor_name') or event.get('actor_uin') or '某人')
    uin = str(event.get('actor_uin') or '')
    content = str(event.get('content') or '').strip()
    when = qz.fmt_time(event.get('event_time'))
    if kind == 'mention':
        lines = [f'有人在 QQ 空间发说说时 @ 了你（{self_name or "你"}）。',
                 f'说说 tid={tid}，作者：{who}'
                 + (f'（QQ {uin}）' if uin else '') + f'，{when}']
        if content:
            lines.append(f'正文：{qz.clip(content, 400)}')
        if images:
            lines.append(f'配图 {images} 张（可用 qzone_images 查看）')
        if post_preview and post_preview.strip() != content.strip():
            lines.append(f'摘要：{qz.clip(post_preview, 200)}')
        lines.append('这是空间里的 @，不是群聊消息。想回应就在这条说说下评论：'
                     'qzone_comment，tid 用上面的值。')
        return '\n'.join(lines)
    if kind == 'comment':
        cid = str(event.get('comment_id') or '')
        lines = [f'你在 QQ 空间发的说说收到了新评论。',
                 f'说说 tid={tid}，{when}']
        if post_preview:
            lines.append(f'说说正文：{qz.clip(post_preview, 300)}')
        lines.append(f'评论人：{who}' + (f'（QQ {uin}）' if uin else '')
                     + (f'，comment_id={cid}' if cid else ''))
        if content:
            lines.append(f'评论内容：{qz.clip(content, 400)}')
        lines.append('想回复这条评论：qzone_comment，tid + comment_id + '
                     'comment_uin 一起提供（comment_uin 用上面的 QQ 号）；'
                     '只想在说说下说一句就只传 tid + content。')
        return '\n'.join(lines)
    return f'QQ 空间有新动静（{kind}，tid={tid}）：{qz.clip(content, 300)}'


def event_summary(rows) -> str:
    parts = []
    for r in rows or []:
        kind = {'mention': '@你', 'comment': '评论'}.get(
            str(r.get('kind') or ''), str(r.get('kind') or ''))
        parts.append(f'{r.get("actor_name") or r.get("actor_uin") or "某人"}'
                     f'{kind}:{qz.clip(r.get("content"), 40)}')
    return '；'.join(parts)


class QzoneWatcher:
    """轮询空间动态并把知会喂给引擎。所有方法对插件无侵入（鸭子类型）。"""

    def __init__(self, plugin, log=None):
        self.p = plugin
        self._log = log or (lambda msg, level='info': None)
        self._task: asyncio.Task | None = None
        self._stopped = False
        self.last_tick: dict = {}
        self._ticks = 0        # 已完成轮询次数，用于低频状态清理

    # ── 生命周期 ──────────────────────────────────────────────

    def start(self, plugin=None) -> None:
        if plugin is not None:
            self.p = plugin
        self._stopped = False
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:      # 生命周期回调在非 loop 线程里被调用时兜底
            loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._loop())

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ── 主循环 ────────────────────────────────────────────────

    def _interval(self) -> float:
        """当前配置的轮询间隔（秒）。每轮重读 self.p.cfg，改配置下轮生效。"""
        cfg = getattr(self.p, 'cfg', None) or {}
        try:
            interval = float(cfg.get('qzone_watch_interval', 300) or 300)
        except (TypeError, ValueError):
            interval = 300.0
        return max(MIN_INTERVAL, interval)

    async def _sleep(self, seconds: float) -> None:
        """等待 seconds 秒；单独抽出来便于单测替换，避免真实等待。"""
        await asyncio.sleep(seconds)

    async def _loop(self) -> None:
        try:
            # 启动后先等一个间隔：on_start 阶段本体还在初始化
            await self._sleep(min(30.0, self._interval()))
            while not self._stopped:
                try:
                    res = await self.tick()
                    if res.get('events') or res.get('handled'):
                        self._log(
                            f'空间知会：新 {res.get("events", 0)} 条，'
                            f'处理 {res.get("handled", 0)} 条')
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._log('空间轮询异常:\n' + traceback.format_exc(), 'error')
                # 每轮重新读配置：调大/调小 qzone_watch_interval 无需重启
                await self._sleep(self._interval())
        except asyncio.CancelledError:
            pass

    # ── 单次扫描 + 分发 ───────────────────────────────────────

    async def tick(self) -> dict:
        """跑一次「扫描 → 落库 → 分发」，返回统计。单测可直接 await。"""
        db = self.p.db_path
        cfg = self.p.cfg
        if runtime_env.is_official():
            return {'disabled': 'official', 'events': 0, 'handled': 0}
        if not cfg.get('qzone_watch_on', True):
            return {'disabled': 'off', 'events': 0, 'handled': 0}
        qz_client = getattr(self.p, 'qzone', None)
        if qz_client is None:
            return {'disabled': 'no-client', 'events': 0, 'handled': 0}
        if not qz_client.available():
            await qz_client.refresh()
            if not qz_client.available():
                return {'disabled': qz_client.cap.why or 'unavailable',
                        'events': 0, 'handled': 0}
        self._ensure_baseline(db)
        found = 0
        try:
            found += await self._scan_feeds(db, cfg)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log('扫好友动态失败:\n' + traceback.format_exc(), 'warn')
        try:
            found += await self._scan_comments(db, cfg)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log('扫说说评论失败:\n' + traceback.format_exc(), 'warn')
        handled = await self.dispatch(db, cfg, limit=MAX_EVENTS_PER_TICK)
        self._ticks += 1
        # 低频清 `ccount:<tid>` 孤儿键（P1-1）：`_scan_comments` 每见过一条
        # 说说就写一个键，只增不删；具体清理规则在 storage.prune_qzone_state。
        if self._ticks % PRUNE_STATE_EVERY == 0:
            try:
                await asyncio.to_thread(storage.prune_qzone_state, db)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log('空间状态清理失败:\n' + traceback.format_exc(), 'warn')
        self.last_tick = {'at': time.time(), 'events': found, 'handled': handled}
        return {'events': found, 'handled': handled}

    def _ensure_baseline(self, db: str) -> None:
        """首次扫描把"现在"定为基线：只报之后发生的动静。"""
        if storage.get_qzone_state(db, 'baseline_done') == '1':
            return
        now = time.time()
        storage.set_qzone_state(db, 'mention_since', now)
        storage.set_qzone_state(db, 'comment_since', now)
        storage.set_qzone_state(db, 'baseline_done', 1)
        self._log('空间监听基线已建立：只报此后的 @ 与新评论')

    async def _scan_feeds(self, db: str, cfg: dict) -> int:
        if not cfg.get('qzone_watch_feeds', True):
            return 0
        qz_client = self.p.qzone
        count = int(cfg.get('qzone_watch_feeds_count', 10) or 10)
        self_uin = str(storage.get_qzone_state(db, 'self_uin', '')
                       or getattr(self.p, 'bot_id', '') or '')
        self_name = str(storage.get_qzone_state(db, 'self_name', '')
                        or getattr(self.p, 'bot_name', '') or '')
        if self_uin and not storage.get_qzone_state(db, 'self_uin'):
            storage.set_qzone_state(db, 'self_uin', self_uin)
        if self_name and not storage.get_qzone_state(db, 'self_name'):
            storage.set_qzone_state(db, 'self_name', self_name)
        feeds, _more, _cursor, err = await qz_client.fetch_feeds(limit=count)
        if err:
            self._log(f'好友动态读取失败：{err}', 'warn')
            return 0
        mentions = qz.find_mentions(feeds, self_uin, self_name)
        since = float(storage.get_qzone_state(db, 'mention_since', 0) or 0)
        gid, _ct = notify_target(cfg)
        n = 0
        newest = since
        for m in mentions:
            ev_time = float(m.get('time') or 0) / (1000.0 if float(m.get('time') or 0) > 1e11 else 1.0)
            if ev_time and ev_time <= since:
                continue
            newest = max(newest, ev_time)
            if storage.add_qzone_event(
                    db, 'mention', tid=m['tid'], actor_uin=m['uin'],
                    actor_name=m['nickname'], content=m['content'],
                    event_time=ev_time, handle_gid=gid):
                n += 1
        if n:
            storage.set_qzone_state(db, 'mention_since',
                                    newest or time.time())
        return n

    async def _scan_comments(self, db: str, cfg: dict) -> int:
        if not cfg.get('qzone_watch_comments', True):
            return 0
        qz_client = self.p.qzone
        posts_limit = int(cfg.get('qzone_watch_posts', 10) or 10)
        posts, err = await qz_client.fetch_posts(limit=posts_limit)
        if err:
            self._log(f'说说列表读取失败：{err}', 'warn')
            return 0
        since = float(storage.get_qzone_state(db, 'comment_since', 0) or 0)
        self_uin = str(storage.get_qzone_state(db, 'self_uin', '')
                       or getattr(self.p, 'bot_id', '') or '')
        gid, _ct = notify_target(cfg)
        n = 0
        newest = since
        for post in posts:
            tid = str(post.get('tid') or '').strip()
            if not tid:
                continue
            try:
                cnum = int(post.get('comment_num') or 0)
            except (TypeError, ValueError):
                cnum = 0
            # 评论数没变就没必要拉详情（省一次请求）
            key = f'ccount:{tid}'
            last_num = storage.get_qzone_state(db, key, '')
            if last_num == str(cnum):
                continue
            storage.set_qzone_state(db, key, cnum)
            if cnum <= 0:
                continue
            detail, derr = await qz_client.fetch_detail(tid, num=30)
            if derr:
                self._log(f'说说 {tid} 详情读取失败：{derr}', 'warn')
                continue
            for c in qz.new_comments(detail, since, self_uin):
                ev_time = float(c.get('time') or 0)
                newest = max(newest, ev_time)
                if storage.add_qzone_event(
                        db, 'comment', tid=tid,
                        actor_uin=c.get('comment_uin', ''),
                        actor_name=c.get('name', ''),
                        content=c.get('content', ''),
                        comment_id=c.get('comment_id', ''),
                        comment_uin=c.get('comment_uin', ''),
                        parent_id=c.get('parent_comment_id', ''),
                        event_time=ev_time, handle_gid=gid):
                    n += 1
        if n:
            storage.set_qzone_state(db, 'comment_since',
                                    newest or time.time())
        return n

    # ── 分发 ─────────────────────────────────────────────────

    async def _fire(self, engine, gid: str, cfg: dict, text: str,
                    chat_type: str) -> dict:
        """派发一轮空间知会（调用方负责群锁）。"""
        return await engine.handle_trigger(
            gid, cfg, text, source='QQ空间',
            chat_type=chat_type, run_kind='qzone',
            source_kind='work',
            notice_text=_space_notice_text(cfg, text),
            # 委托人用主人：空间知会是主人配置的自动化，评论/点赞/发说说
            # 这些写工具要靠主人身份才能过风险门禁；事件里的真实当事人
            # 已经写进 text 了，不用 actor 字段覆盖发送人。
            actor_id=str(cfg.get('owner_qq') or ''),
            actor_name='')

    async def dispatch(self, db: str, cfg: dict, limit: int = 3) -> int:
        """把待处理知会逐条喂给引擎（每条一轮后台对话）。"""
        rows = storage.pending_qzone_events(db, limit=limit)
        if not rows:
            return 0
        engine = getattr(self.p, 'engine', None)
        if engine is None or not hasattr(engine, 'handle_trigger'):
            return 0
        qz_client = getattr(self.p, 'qzone', None)
        self_name = str(getattr(self.p, 'bot_name', '') or '')
        handled = 0
        for ev in rows:
            gid = str(ev.get('handle_gid') or '')
            if not gid:
                gid, _ct = notify_target(cfg)
            chat_type = 'private' if gid == str(cfg.get('owner_qq') or '') \
                else 'group'
            if not gid.isdigit():
                storage.mark_qzone_event(db, ev['id'], 'skipped',
                                         '没有可投递的会话')
                continue
            # 投递会话可能有自己的群级覆盖（模型/阈值/开关）：知会轮按
            # 「收通知的那个会话」的配置跑，别用全局配置顶替
            cfg_g = cfg
            try:
                from .config import resolve_group
                row = storage.get_group(db, gid) or {}
                cfg_g = resolve_group(cfg, row.get('settings') or {})
            except Exception:
                cfg_g = cfg
            preview, images = '', 0
            if qz_client is not None and ev.get('tid'):
                try:
                    detail, _err = await qz_client.fetch_detail(
                        ev['tid'], num=1)
                    if detail:
                        preview = str(detail.get('content') or '')
                        images = len(detail.get('images') or [])
                except Exception:
                    pass
            text = space_event_text(ev, self_name=self_name,
                                    post_preview=preview, images=images)
            try:
                lock_get = getattr(self.p, 'group_lock', None)
                if callable(lock_get):
                    # 与外部触发服务同构：同一会话的轮次必须串行，否则会和
                    # 群里的真人对话/定时苏醒抢同一条历史
                    async with lock_get(gid):
                        res = await self._fire(
                            engine, gid, cfg_g, text, chat_type)
                else:
                    res = await self._fire(
                        engine, gid, cfg_g, text, chat_type)
                status = 'handled' if res.get('status') != 'error' else 'pending'
                storage.mark_qzone_event(
                    db, ev['id'], status,
                    '' if status == 'handled' else str(res.get('error') or '运行异常'))
                if status == 'handled':
                    handled += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log(f'空间知会处理失败（event={ev["id"]}）：{e}', 'warn')
                storage.mark_qzone_event(db, ev['id'], 'pending', str(e)[:200])
        return handled


def _space_notice_text(cfg: dict, text: str) -> str:
    """空间知会的通知全文（不走群里那套"值不值得开口"的措辞）。"""
    return (
        '[QQ空间通知] 这是空间里的动静，不是群聊消息，也不是群友在群里 @ 你。\n'
        f'{text}\n'
        '处理建议：\n'
        '- 想回应就在那条说说下评论（qzone_comment），需要先看内容/评论树用 '
        'qzone_detail，需要看配图用 qzone_images；\n'
        '- 被 @ 时没有必须回复的义务，看内容值不值得；评论同理，不想理会就整条'
        '只输出 [静默]，不要解释这套机制；\n'
        '- 已经处理完也输出 [静默]（工具调用本身不需要再向群里汇报）。'
    )
