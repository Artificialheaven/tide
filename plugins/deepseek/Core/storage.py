"""DeepSeek 插件 SQLite 存储：连接工厂 + PRAGMA user_version 迁移 + DAL。

全部 DAL 短开短关连接（WAL 下跨线程安全），不缓存 Connection。
时间统一 time.time() 秒；group_id/user_id 一律存 TEXT。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time


# ── 连接 ─────────────────────────────────────────────────────────

def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


# ── DDL（user_version = 1）──────────────────────────────────────

# 单独提常量：v10 迁移要给比 usage_log 更早的 v1 老库补建这张表
_DDL_USAGE_LOG = """CREATE TABLE IF NOT EXISTS usage_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id    TEXT NOT NULL DEFAULT '',
        kind        TEXT NOT NULL,
        tokens_in   INTEGER NOT NULL DEFAULT 0,
        cache_hit   INTEGER NOT NULL DEFAULT 0,
        tokens_out  INTEGER NOT NULL DEFAULT 0,
        tool_rounds INTEGER NOT NULL DEFAULT 0,
        ok          INTEGER NOT NULL DEFAULT 1,
        created_at  REAL NOT NULL
    )"""

_DDL = [
    """CREATE TABLE IF NOT EXISTS groups (
        group_id      TEXT PRIMARY KEY,
        enabled       INTEGER NOT NULL DEFAULT 0,
        persona       TEXT NOT NULL DEFAULT '',
        aliases       TEXT NOT NULL DEFAULT '',
        settings_json TEXT NOT NULL DEFAULT '{}',
        last_active   REAL NOT NULL DEFAULT 0,
        created_at    REAL NOT NULL,
        updated_at    REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS messages (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id     TEXT NOT NULL,
        role         TEXT NOT NULL,
        sender_id    TEXT NOT NULL DEFAULT '',
        sender_name  TEXT NOT NULL DEFAULT '',
        content      TEXT NOT NULL DEFAULT '',
        tool_calls   TEXT NOT NULL DEFAULT '',
        tool_call_id TEXT NOT NULL DEFAULT '',
        name         TEXT NOT NULL DEFAULT '',
        summarized   INTEGER NOT NULL DEFAULT 0,
        created_at   REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_messages_group ON messages(group_id, id)",
    """CREATE TABLE IF NOT EXISTS summaries (
        group_id         TEXT PRIMARY KEY,
        summary          TEXT NOT NULL DEFAULT '',
        covered_up_to_id INTEGER NOT NULL DEFAULT 0,
        persona_hash     TEXT NOT NULL DEFAULT '',
        updated_at       REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS mcp_servers (
        id             TEXT PRIMARY KEY,
        name           TEXT NOT NULL DEFAULT '',
        kind           TEXT NOT NULL DEFAULT 'stdio',
        config_json    TEXT NOT NULL DEFAULT '{}',
        enabled        INTEGER NOT NULL DEFAULT 0,
        tools_json     TEXT NOT NULL DEFAULT '[]',
        tools_cached_at REAL,
        status         TEXT NOT NULL DEFAULT 'stopped',
        last_error     TEXT NOT NULL DEFAULT '',
        created_at     REAL NOT NULL,
        updated_at     REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS group_tool_state (
        group_id TEXT NOT NULL,
        tool_key TEXT NOT NULL,
        enabled  INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (group_id, tool_key)
    )""",
    _DDL_USAGE_LOG,
    "CREATE INDEX IF NOT EXISTS idx_usage_group ON usage_log(group_id, id)",
    """CREATE TABLE IF NOT EXISTS users (
        user_id       TEXT PRIMARY KEY,
        names         TEXT NOT NULL DEFAULT '[]',
        first_seen    REAL NOT NULL,
        last_seen     REAL NOT NULL,
        last_group_id TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS memory_entries (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         TEXT NOT NULL,
        text            TEXT NOT NULL,
        tags            TEXT NOT NULL DEFAULT '',
        importance      REAL NOT NULL DEFAULT 0.6,
        superseded      INTEGER NOT NULL DEFAULT 0,
        history         TEXT NOT NULL DEFAULT '[]',
        source_group_id TEXT NOT NULL DEFAULT '',
        created_at      REAL NOT NULL,
        updated_at      REAL NOT NULL,
        last_accessed   REAL NOT NULL DEFAULT 0,
        access_count    INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_memory_user ON memory_entries(user_id, superseded)",
    """CREATE TABLE IF NOT EXISTS memory_state (
        group_id            TEXT PRIMARY KEY,
        extracted_up_to_id  INTEGER NOT NULL DEFAULT 0,
        turns_since         INTEGER NOT NULL DEFAULT 0,
        updated_at          REAL NOT NULL
    )""",
]


# v2 增量：群聊原始语料 / 风格画像 / 约定待办
_DDL_V2 = [
    """CREATE TABLE IF NOT EXISTS raw_messages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id    TEXT NOT NULL,
        sender_id   TEXT NOT NULL DEFAULT '',
        sender_name TEXT NOT NULL DEFAULT '',
        content     TEXT NOT NULL DEFAULT '',
        created_at  REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_raw_group ON raw_messages(group_id, id)",
    """CREATE TABLE IF NOT EXISTS group_style (
        group_id     TEXT PRIMARY KEY,
        style_text   TEXT NOT NULL DEFAULT '',
        up_to_id     INTEGER NOT NULL DEFAULT 0,
        sample_count INTEGER NOT NULL DEFAULT 0,
        built_at     REAL NOT NULL DEFAULT 0,
        updated_at   REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS commitments (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id    TEXT NOT NULL,
        owner_id    TEXT NOT NULL DEFAULT '',
        owner_name  TEXT NOT NULL DEFAULT '',
        content     TEXT NOT NULL,
        kind        TEXT NOT NULL DEFAULT 'task',
        due_text    TEXT NOT NULL DEFAULT '',
        due_at      REAL,
        status      TEXT NOT NULL DEFAULT 'open',
        reminded    INTEGER NOT NULL DEFAULT 0,
        source      TEXT NOT NULL DEFAULT 'extract',
        created_at  REAL NOT NULL,
        updated_at  REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_commit_owner ON commitments(owner_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_commit_group ON commitments(group_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_commit_due ON commitments(due_at, reminded)",
]


# v3 增量：AI 定时任务（到点让模型按 brief 写一段话发群/私聊）
_DDL_V3 = [
    """CREATE TABLE IF NOT EXISTS scheduled_jobs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        creator_id   TEXT NOT NULL DEFAULT '',
        creator_name TEXT NOT NULL DEFAULT '',
        group_id     TEXT NOT NULL DEFAULT '',
        target_type  TEXT NOT NULL DEFAULT 'group',
        target_id    TEXT NOT NULL DEFAULT '',
        mode         TEXT NOT NULL DEFAULT 'once',
        once_at      REAL,
        run_hhmm     TEXT NOT NULL DEFAULT '',
        weekday      INTEGER NOT NULL DEFAULT -1,
        brief        TEXT NOT NULL DEFAULT '',
        next_run_at  REAL NOT NULL,
        enabled      INTEGER NOT NULL DEFAULT 1,
        last_run_at  REAL,
        last_error   TEXT NOT NULL DEFAULT '',
        created_at   REAL NOT NULL,
        updated_at   REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_jobs_due ON scheduled_jobs(enabled, next_run_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_group ON scheduled_jobs(group_id)",
]

# v4 增量：私聊文件清单（OneBot 没有"列举私聊文件"接口，机器人自己登记）
_DDL_V4 = [
    """CREATE TABLE IF NOT EXISTS private_files (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    TEXT NOT NULL,
        file_id    TEXT NOT NULL,
        busid      TEXT NOT NULL DEFAULT '',
        name       TEXT NOT NULL DEFAULT '',
        file_size  INTEGER NOT NULL DEFAULT 0,
        msg_time   INTEGER NOT NULL DEFAULT 0,
        ref_json   TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        UNIQUE(user_id, file_id, busid)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pf_user ON private_files(user_id, msg_time DESC)",
]


# v5 增量：AI 自我提示词注入表（学到技能/沉淀素材后给自己写的每轮提醒）
_DDL_V5 = [
    """CREATE TABLE IF NOT EXISTS prompt_injections (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        scope      TEXT NOT NULL,
        title      TEXT NOT NULL,
        text       TEXT NOT NULL,
        enabled    INTEGER NOT NULL DEFAULT 1,
        created_by TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pi_scope ON prompt_injections(scope, enabled)",
]

# scope='*' = 全局注入（所有群/私聊生效，仅主人可建）
GLOBAL_INJECTION_SCOPE = '*'


# v6 增量：定时概率唤醒状态（防重启丢计数）+ AI 自定义工具
_DDL_V6 = [
    """CREATE TABLE IF NOT EXISTS proactor_state (
        group_id          TEXT PRIMARY KEY,
        ticks_since_fire  INTEGER NOT NULL DEFAULT 0,
        last_tick_at      REAL NOT NULL DEFAULT 0,
        last_fire_at      REAL NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS custom_tools (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        scope           TEXT NOT NULL,
        name            TEXT NOT NULL,
        title           TEXT NOT NULL DEFAULT '',
        description     TEXT NOT NULL,
        params_schema   TEXT NOT NULL,
        code            TEXT NOT NULL,
        enabled         INTEGER NOT NULL DEFAULT 1,
        owner_only_call INTEGER NOT NULL DEFAULT 0,
        status          TEXT NOT NULL DEFAULT 'ok',
        created_by      TEXT NOT NULL DEFAULT '',
        created_at      REAL NOT NULL,
        updated_at      REAL NOT NULL,
        UNIQUE(scope, name)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ct_scope ON custom_tools(scope, enabled)",
]

GLOBAL_CUSTOM_SCOPE = '*'


# v7 增量：后台运行记录（定时苏醒 / 定时任务触发的完整工作流留痕）
_DDL_V7 = [
    """CREATE TABLE IF NOT EXISTS bg_runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        kind        TEXT NOT NULL,
        target_type TEXT NOT NULL DEFAULT 'group',
        target_id   TEXT NOT NULL DEFAULT '',
        prompt      TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT '',
        final_text  TEXT NOT NULL DEFAULT '',
        steps       TEXT NOT NULL DEFAULT '[]',
        error       TEXT NOT NULL DEFAULT '',
        started_at  REAL NOT NULL,
        finished_at REAL NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_bg_started ON bg_runs(started_at)",
]


# v8 增量：多条 AI 自管轮询设定（每条带独立任务/概率/保底/活跃门/分时段）
_DDL_V8 = [
    """CREATE TABLE IF NOT EXISTS polls (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id         TEXT NOT NULL,
        task             TEXT NOT NULL,
        prob_percent     INTEGER NOT NULL DEFAULT 10,
        every_n          INTEGER NOT NULL DEFAULT 0,
        active_hours     REAL NOT NULL DEFAULT 12,
        schedule_json    TEXT NOT NULL DEFAULT '[]',
        enabled          INTEGER NOT NULL DEFAULT 1,
        creator_id       TEXT NOT NULL DEFAULT '',
        creator_name     TEXT NOT NULL DEFAULT '',
        ticks_since_fire INTEGER NOT NULL DEFAULT 0,
        last_tick_at     REAL NOT NULL DEFAULT 0,
        last_fire_at     REAL NOT NULL DEFAULT 0,
        created_at       REAL NOT NULL,
        updated_at       REAL NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_polls_group ON polls(group_id, enabled)",
    # bg_runs 关联到具体哪条轮询触发（0=旧版无任务的通用苏醒/定时任务）
    "ALTER TABLE bg_runs ADD COLUMN poll_id INTEGER NOT NULL DEFAULT 0",
]

# v9 增量：AI 托管的外部监测进程（watcher）登记
_DDL_V9 = [
    """CREATE TABLE IF NOT EXISTS watchers (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id     TEXT NOT NULL,
        name         TEXT NOT NULL DEFAULT '',
        script       TEXT NOT NULL,
        args_json    TEXT NOT NULL DEFAULT '[]',
        pid          INTEGER NOT NULL DEFAULT 0,
        status       TEXT NOT NULL DEFAULT 'running',
        creator_id   TEXT NOT NULL DEFAULT '',
        creator_name TEXT NOT NULL DEFAULT '',
        log_path     TEXT NOT NULL DEFAULT '',
        started_at   REAL NOT NULL,
        stopped_at   REAL NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_watchers_group ON watchers(group_id, status)",
]

# v10：QQ 空间说说发布留痕（tid 用于日后删除）+ usage_log 时间范围索引
_DDL_V10 = [
    """CREATE TABLE IF NOT EXISTS qzone_posts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        tid          TEXT NOT NULL UNIQUE,
        content      TEXT NOT NULL DEFAULT '',
        ugc_right    INTEGER NOT NULL DEFAULT 1,
        creator_id   TEXT NOT NULL DEFAULT '',
        creator_name TEXT NOT NULL DEFAULT '',
        created_at   REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_qzone_time ON qzone_posts(created_at)",
]
# idx_usage_group_time 单独处理：比 usage_log 更早的 v1 老库没有这张表，
# 直接建索引会 OperationalError，需先补建表
_DDL_V10_USAGE_INDEX = \
    "CREATE INDEX IF NOT EXISTS idx_usage_group_time ON usage_log(group_id, created_at)"

# v11：每群「背景闲聊」游标——上次已注入前台对话轮的 raw_messages 最大 id。
# 下一轮只注入游标之后、触发消息之前的未触发群消息。
_DDL_V11 = [
    """CREATE TABLE IF NOT EXISTS raw_cursors (
        group_id TEXT PRIMARY KEY,
        last_id  INTEGER NOT NULL DEFAULT 0
    )""",
]

# v12：心情（每群一份）+ 全局第一人称自我记忆 + 主动搭话连没中计数。
_DDL_V12 = [
    """CREATE TABLE IF NOT EXISTS mood_state (
        group_id           TEXT PRIMARY KEY,
        mood               REAL NOT NULL DEFAULT 70,
        dims_json          TEXT NOT NULL DEFAULT '{}',
        updated_at         REAL NOT NULL DEFAULT 0,
        last_judge_at      REAL NOT NULL DEFAULT 0,
        pending_delta      INTEGER NOT NULL DEFAULT 0,
        pending_confidence INTEGER NOT NULL DEFAULT 0,
        pending_reason     TEXT NOT NULL DEFAULT '',
        pending_tid        INTEGER NOT NULL DEFAULT 0,
        injected_for       REAL NOT NULL DEFAULT 0,
        inject_day         TEXT NOT NULL DEFAULT '',
        inject_count       INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS mood_timeline (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id    TEXT NOT NULL,
        ts          REAL NOT NULL,
        kind        TEXT NOT NULL DEFAULT 'judge',
        source      TEXT NOT NULL DEFAULT '',
        delta       INTEGER NOT NULL DEFAULT 0,
        mood_after  REAL NOT NULL DEFAULT 70,
        dims_json   TEXT NOT NULL DEFAULT '{}',
        reason      TEXT NOT NULL DEFAULT '',
        confidence  INTEGER NOT NULL DEFAULT 0,
        injected    INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_mood_tl_group ON mood_timeline(group_id, id)",
    """CREATE TABLE IF NOT EXISTS self_memories (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        text             TEXT NOT NULL,
        tags             TEXT NOT NULL DEFAULT '',
        importance       REAL NOT NULL DEFAULT 0.6,
        superseded       INTEGER NOT NULL DEFAULT 0,
        history          TEXT NOT NULL DEFAULT '[]',
        created_by_group TEXT NOT NULL DEFAULT '',
        created_at       REAL NOT NULL,
        updated_at       REAL NOT NULL,
        last_accessed    REAL NOT NULL DEFAULT 0,
        access_count     INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_self_active ON self_memories(superseded, id)",
    """CREATE TABLE IF NOT EXISTS self_memory_state (
        day        TEXT PRIMARY KEY,
        writes     INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL
    )""",
    # ALTER（需 table_info 守卫，见 migrate）
    "ALTER TABLE proactor_state ADD COLUMN speakup_miss INTEGER NOT NULL DEFAULT 0",
]

# v13：系统提示词的人工覆盖（key → content；缺省走 prompts.py 里的默认值）。
_DDL_V13 = [
    """CREATE TABLE IF NOT EXISTS prompt_overrides (
        key        TEXT PRIMARY KEY,
        content    TEXT NOT NULL,
        updated_at REAL NOT NULL
    )""",
]

# v14：AI 自制初筛脚本（多判官机制的第一层：任一脚本/启发式命中才拉起 LLM 判官）。
# 结构与 custom_tools 同构（scope=群号 或 GLOBAL_CUSTOM_SCOPE），但没有 params_schema，
# 入口契约固定为 screen(group_id, user_id, message_raw)。
_DDL_V14 = [
    """CREATE TABLE IF NOT EXISTS screen_scripts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        scope        TEXT NOT NULL,
        name         TEXT NOT NULL,
        title        TEXT NOT NULL DEFAULT '',
        description  TEXT NOT NULL DEFAULT '',
        code         TEXT NOT NULL,
        enabled      INTEGER NOT NULL DEFAULT 1,
        hit_count    INTEGER NOT NULL DEFAULT 0,
        last_hit_at  REAL NOT NULL DEFAULT 0,
        created_by   TEXT NOT NULL DEFAULT '',
        created_at   REAL NOT NULL,
        updated_at   REAL NOT NULL,
        UNIQUE(scope, name)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ss_scope ON screen_scripts(scope, enabled)",
]

# v15：两件事——
# 1) 自我记忆「回忆链」：召回/改写/作废/网页编辑都只追加一条 trace，
#    绝不进对话上下文；self_memories 加冗余 trace_count。
# 2) 好友申请/群邀请暂存：request 事件落库等主人私聊拍板，flag 唯一去重。
_DDL_V15 = [
    """CREATE TABLE IF NOT EXISTS self_memory_traces (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        memory_id   INTEGER NOT NULL,
        kind        TEXT NOT NULL DEFAULT 'recall',
        context     TEXT NOT NULL DEFAULT '',
        group_id    TEXT NOT NULL DEFAULT '',
        source      TEXT NOT NULL DEFAULT '',
        mood_tag    TEXT NOT NULL DEFAULT '',
        mood_level  INTEGER NOT NULL DEFAULT 0,
        seen_text   TEXT NOT NULL DEFAULT '',
        revision    TEXT NOT NULL DEFAULT '',
        match_score REAL NOT NULL DEFAULT 0,
        created_at  REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_smt_mem ON self_memory_traces(memory_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_smt_time ON self_memory_traces(created_at)",
    """CREATE TABLE IF NOT EXISTS pending_requests (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        flag        TEXT NOT NULL UNIQUE,
        request_type TEXT NOT NULL,
        sub_type    TEXT NOT NULL DEFAULT '',
        user_id     TEXT NOT NULL,
        group_id    TEXT NOT NULL DEFAULT '',
        comment     TEXT NOT NULL DEFAULT '',
        nickname    TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT 'pending',
        decided_by  TEXT NOT NULL DEFAULT '',
        decided_at  REAL NOT NULL DEFAULT 0,
        result      TEXT NOT NULL DEFAULT '',
        created_at  REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pr_status ON pending_requests(status, created_at)",
]

# v16：loop 长跑模式——AI 的长跑申请（主人批准后启动）与会话状态。
_DDL_V16 = [
    """CREATE TABLE IF NOT EXISTS loop_requests (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id       TEXT NOT NULL,
        task           TEXT NOT NULL,
        reason         TEXT NOT NULL DEFAULT '',
        requester_id   TEXT NOT NULL DEFAULT '',
        requester_name TEXT NOT NULL DEFAULT '',
        status         TEXT NOT NULL DEFAULT 'pending',
        decided_by     TEXT NOT NULL DEFAULT '',
        decided_at     REAL NOT NULL DEFAULT 0,
        created_at     REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_lr_status ON loop_requests(status, created_at)",
    """CREATE TABLE IF NOT EXISTS loop_sessions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id    TEXT NOT NULL,
        task        TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'running',
        started_by  TEXT NOT NULL DEFAULT 'owner',
        request_id  INTEGER NOT NULL DEFAULT 0,
        rounds      INTEGER NOT NULL DEFAULT 0,
        compacts    INTEGER NOT NULL DEFAULT 0,
        result      TEXT NOT NULL DEFAULT '',
        error       TEXT NOT NULL DEFAULT '',
        created_at  REAL NOT NULL,
        ended_at    REAL NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ls_status ON loop_sessions(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_ls_group ON loop_sessions(group_id, id DESC)",
]


def migrate(db_path: str):
    """初始化/升级数据库。幂等。"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = connect(db_path)
    try:
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version < 1:
            for ddl in _DDL:
                conn.execute(ddl)
            conn.execute('PRAGMA user_version=1')
            version = 1
        if version < 2:
            for ddl in _DDL_V2:
                conn.execute(ddl)
            conn.execute('PRAGMA user_version=2')
        if version < 3:
            for ddl in _DDL_V3:
                conn.execute(ddl)
            conn.execute('PRAGMA user_version=3')
        if version < 4:
            for ddl in _DDL_V4:
                conn.execute(ddl)
            conn.execute('PRAGMA user_version=4')
        if version < 5:
            for ddl in _DDL_V5:
                conn.execute(ddl)
            conn.execute('PRAGMA user_version=5')
        if version < 6:
            for ddl in _DDL_V6:
                conn.execute(ddl)
            conn.execute('PRAGMA user_version=6')
        if version < 7:
            for ddl in _DDL_V7:
                conn.execute(ddl)
            conn.execute('PRAGMA user_version=7')
        if version < 8:
            for ddl in _DDL_V8:
                # ALTER TABLE 不能 IF NOT EXISTS：先查列，避免中途失败后
                # 永久卡在 v7（其余 DDL 本身幂等）
                if ddl.startswith('ALTER TABLE'):
                    cols = {r[1] for r in conn.execute('PRAGMA table_info(bg_runs)')}
                    if 'poll_id' in cols:
                        continue
                conn.execute(ddl)
            conn.execute('PRAGMA user_version=8')
        if version < 9:
            for ddl in _DDL_V9:
                conn.execute(ddl)
            conn.execute('PRAGMA user_version=9')
        if version < 10:
            for ddl in _DDL_V10:
                conn.execute(ddl)
            has_usage = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='usage_log'").fetchone()
            if not has_usage:
                # 比 usage_log 更早的 v1 老库：补建表与旧索引
                conn.execute(_DDL_USAGE_LOG)
                conn.execute(
                    'CREATE INDEX IF NOT EXISTS idx_usage_group '
                    'ON usage_log(group_id, id)')
            conn.execute(_DDL_V10_USAGE_INDEX)
            conn.execute('PRAGMA user_version=10')
        if version < 11:
            for ddl in _DDL_V11:
                conn.execute(ddl)
            conn.execute('PRAGMA user_version=11')
        if version < 12:
            for ddl in _DDL_V12:
                if ddl.startswith('ALTER TABLE'):
                    cols = {r[1] for r in
                            conn.execute('PRAGMA table_info(proactor_state)')}
                    if 'speakup_miss' in cols:
                        continue
                conn.execute(ddl)
            conn.execute('PRAGMA user_version=12')
        if version < 13:
            for ddl in _DDL_V13:
                conn.execute(ddl)
            conn.execute('PRAGMA user_version=13')
        if version < 14:
            for ddl in _DDL_V14:
                conn.execute(ddl)
            conn.execute('PRAGMA user_version=14')
        if version < 15:
            for ddl in _DDL_V15:
                conn.execute(ddl)
            cols = {r[1] for r in
                    conn.execute('PRAGMA table_info(self_memories)')}
            if 'trace_count' not in cols:
                conn.execute(
                    'ALTER TABLE self_memories '
                    'ADD COLUMN trace_count INTEGER NOT NULL DEFAULT 0')
            conn.execute('PRAGMA user_version=15')
        if version < 16:
            for ddl in _DDL_V16:
                conn.execute(ddl)
            conn.execute('PRAGMA user_version=16')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── 群 DAL ───────────────────────────────────────────────────────

def _row_to_group(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d['settings'] = json.loads(d.pop('settings_json') or '{}')
    except json.JSONDecodeError:
        d['settings'] = {}
    d['enabled'] = bool(d['enabled'])
    return d


def get_group(db_path: str, group_id) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT * FROM groups WHERE group_id=?',
                         (str(group_id),)).fetchone()
        return _row_to_group(r) if r else None
    finally:
        conn.close()


def get_or_create_group(db_path: str, group_id) -> dict:
    g = get_group(db_path, group_id)
    if g is None:
        now = time.time()
        conn = connect(db_path)
        try:
            conn.execute(
                'INSERT INTO groups (group_id, enabled, persona, aliases, '
                'settings_json, last_active, created_at, updated_at) '
                "VALUES (?, 0, '', '', '{}', ?, ?, ?)",
                (str(group_id), now, now, now))
            conn.commit()
        finally:
            conn.close()
        g = get_group(db_path, group_id)
    return g


def upsert_group(db_path: str, group_id, *, enabled=None, persona=None,
                 aliases=None, settings=None, touch=True) -> dict:
    get_or_create_group(db_path, group_id)
    fields, vals = [], []
    if enabled is not None:
        fields.append('enabled=?'); vals.append(1 if enabled else 0)
    if persona is not None:
        fields.append('persona=?'); vals.append(persona)
    if aliases is not None:
        fields.append('aliases=?'); vals.append(aliases)
    if settings is not None:
        fields.append('settings_json=?'); vals.append(json.dumps(settings, ensure_ascii=False))
    if touch:
        fields.append('last_active=?'); vals.append(time.time())
    fields.append('updated_at=?'); vals.append(time.time())
    vals.append(str(group_id))
    conn = connect(db_path)
    try:
        conn.execute(f"UPDATE groups SET {', '.join(fields)} WHERE group_id=?", vals)
        conn.commit()
    finally:
        conn.close()
    return get_group(db_path, group_id)


def list_groups(db_path: str) -> list:
    conn = connect(db_path)
    try:
        rows = conn.execute('SELECT * FROM groups ORDER BY updated_at DESC').fetchall()
        return [_row_to_group(r) for r in rows]
    finally:
        conn.close()


# ── 消息 DAL ─────────────────────────────────────────────────────

def insert_message(db_path: str, group_id, role: str, content: str,
                   sender_id: str = '', sender_name: str = '',
                   tool_calls: list | None = None, tool_call_id: str = '',
                   name: str = '', created_at: float | None = None) -> int:
    conn = connect(db_path)
    try:
        cur = conn.execute(
            'INSERT INTO messages (group_id, role, sender_id, sender_name, '
            'content, tool_calls, tool_call_id, name, summarized, created_at) '
            'VALUES (?,?,?,?,?,?,?,?,0,?)',
            (str(group_id), role, str(sender_id), sender_name, content,
             json.dumps(tool_calls or [], ensure_ascii=False), tool_call_id,
             name, created_at if created_at is not None else time.time()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def recent_messages(db_path: str, group_id, limit: int) -> list:
    """返回最近 limit 条，按时间正序。"""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            'SELECT * FROM messages WHERE group_id=? ORDER BY id DESC LIMIT ?',
            (str(group_id), int(limit))).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


def messages_since(db_path: str, group_id, after_id: int,
                   limit: int = 2000) -> list:
    """锚点之后的全部消息（正序，含 tool 行）。

    和 messages_after 的区别：保留 tool 行（窗口渲染与工具链完整性要用），
    且 limit 截的是「最近」N 条，不是最早的 N 条。"""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            'SELECT * FROM messages WHERE group_id=? AND id>? '
            'ORDER BY id DESC LIMIT ?',
            (str(group_id), int(after_id), int(limit))).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


def messages_after(db_path: str, group_id, after_id: int, limit: int) -> list:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            'SELECT id, role, sender_id, sender_name, content FROM messages '
            'WHERE group_id=? AND id>? AND role IN ("user","assistant") '
            'ORDER BY id ASC LIMIT ?',
            (str(group_id), int(after_id), int(limit))).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# 历史查询工具的背景通知 name（这些 user 行装的是内部指令，不进历史）
_HISTORY_HIDDEN_NAMES = ('wake-notice', 'join-notice', 'trigger-notice',
                         'speakup-notice')


def query_messages(db_path: str, group_id, *, start_ts: float = 0,
                   end_ts: float = 0, offset: int = 0, limit: int = 30,
                   include_tools: bool = False) -> tuple:
    """两种模式查群消息，正序返回 (rows, total)。

    - 时间段：start_ts/end_ts（秒，0 端点开区间表示不限制），按 created_at 过滤；
    - 相对条数：两端点都为 0，OFFSET 跳过最近 offset 条、再取 limit 条。
    背景通知内部行（wake/join/trigger-notice）与 tool 行默认不返回。
    """
    gid = str(group_id)
    where = ['group_id=?']
    args: list = [gid]
    roles = ('user', 'assistant') if not include_tools else (
        'user', 'assistant', 'tool')
    where.append('role IN (' + ','.join('?' * len(roles)) + ')')
    args.extend(roles)
    where.append("(name IS NULL OR name NOT IN ('"
                 + "','".join(_HISTORY_HIDDEN_NAMES) + "'))")
    if start_ts:
        where.append('created_at>=?')
        args.append(float(start_ts))
    if end_ts:
        where.append('created_at<=?')
        args.append(float(end_ts))
    sql_where = ' WHERE ' + ' AND '.join(where)
    conn = connect(db_path)
    try:
        total = conn.execute(
            f'SELECT COUNT(*) FROM messages{sql_where}', args).fetchone()[0]
        if start_ts or end_ts:
            rows = conn.execute(
                f'SELECT * FROM messages{sql_where} ORDER BY id ASC LIMIT ?',
                (*args, int(limit))).fetchall()
        else:
            # 相对条数：倒序取窗口再反转为正序
            rows = conn.execute(
                f'SELECT * FROM messages{sql_where} ORDER BY id DESC '
                'LIMIT ? OFFSET ?',
                (*args, int(limit), int(offset))).fetchall()
            rows = reversed(rows)
        return [dict(r) for r in rows], int(total)
    finally:
        conn.close()


def unsummarized_old(db_path: str, group_id, keep_window_ids: set) -> list:
    """窗口之外、尚未摘要的旧消息（正序）。"""
    if not keep_window_ids:
        return []
    conn = connect(db_path)
    try:
        ph = ','.join('?' * len(keep_window_ids))
        rows = conn.execute(
            f'SELECT * FROM messages WHERE group_id=? AND summarized=0 '
            f'AND id NOT IN ({ph}) ORDER BY id ASC',
            (str(group_id), *keep_window_ids)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_summarized(db_path: str, group_id, up_to_id: int):
    conn = connect(db_path)
    try:
        conn.execute(
            'UPDATE messages SET summarized=1 WHERE group_id=? AND id<=?',
            (str(group_id), int(up_to_id)))
        conn.commit()
    finally:
        conn.close()


def reset_group_memory(db_path: str, group_id):
    """清空某群对话记忆（消息+摘要），不动群设置。"""
    conn = connect(db_path)
    try:
        conn.execute('DELETE FROM messages WHERE group_id=?', (str(group_id),))
        conn.execute('DELETE FROM summaries WHERE group_id=?', (str(group_id),))
        conn.execute('DELETE FROM memory_state WHERE group_id=?', (str(group_id),))
        conn.commit()
    finally:
        conn.close()


# ── 摘要 DAL ─────────────────────────────────────────────────────

def get_summary(db_path: str, group_id) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT * FROM summaries WHERE group_id=?',
                         (str(group_id),)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def set_summary(db_path: str, group_id, summary: str, covered_up_to_id: int,
                persona_hash: str):
    conn = connect(db_path)
    try:
        conn.execute(
            'INSERT INTO summaries (group_id, summary, covered_up_to_id, '
            'persona_hash, updated_at) VALUES (?,?,?,?,?) '
            'ON CONFLICT(group_id) DO UPDATE SET summary=excluded.summary, '
            'covered_up_to_id=excluded.covered_up_to_id, '
            'persona_hash=excluded.persona_hash, updated_at=excluded.updated_at',
            (str(group_id), summary, int(covered_up_to_id), persona_hash,
             time.time()))
        conn.commit()
    finally:
        conn.close()


# ── 工具勾选 DAL ─────────────────────────────────────────────────

def get_tool_overrides(db_path: str, group_id) -> dict:
    """{tool_key: bool}；缺位=用默认。"""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            'SELECT tool_key, enabled FROM group_tool_state WHERE group_id=?',
            (str(group_id),)).fetchall()
        return {r['tool_key']: bool(r['enabled']) for r in rows}
    finally:
        conn.close()


def set_tool_override(db_path: str, group_id, tool_key: str, enabled: bool):
    conn = connect(db_path)
    try:
        conn.execute(
            'INSERT INTO group_tool_state (group_id, tool_key, enabled) '
            'VALUES (?,?,?) ON CONFLICT(group_id, tool_key) '
            'DO UPDATE SET enabled=excluded.enabled',
            (str(group_id), tool_key, 1 if enabled else 0))
        conn.commit()
    finally:
        conn.close()


# ── MCP DAL ──────────────────────────────────────────────────────

def list_mcp_servers(db_path: str, only_enabled=False) -> list:
    conn = connect(db_path)
    try:
        sql = 'SELECT * FROM mcp_servers'
        if only_enabled:
            sql += ' WHERE enabled=1'
        sql += ' ORDER BY id ASC'
        rows = conn.execute(sql).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d['config'] = json.loads(d.pop('config_json') or '{}')
            except json.JSONDecodeError:
                d['config'] = {}
            try:
                d['tools'] = json.loads(d.pop('tools_json') or '[]')
            except json.JSONDecodeError:
                d['tools'] = []
            d['enabled'] = bool(d['enabled'])
            out.append(d)
        return out
    finally:
        conn.close()


def get_mcp_server(db_path: str, server_id: str) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT * FROM mcp_servers WHERE id=?',
                         (server_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d['config'] = json.loads(d.pop('config_json') or '{}')
        d['tools'] = json.loads(d.pop('tools_json') or '[]')
        d['enabled'] = bool(d['enabled'])
        return d
    finally:
        conn.close()


def upsert_mcp_server(db_path: str, server_id: str, name: str, kind: str,
                      config: dict, enabled: bool) -> dict:
    now = time.time()
    conn = connect(db_path)
    try:
        conn.execute(
            'INSERT INTO mcp_servers (id, name, kind, config_json, enabled, '
            'created_at, updated_at) VALUES (?,?,?,?,?,?,?) '
            'ON CONFLICT(id) DO UPDATE SET name=excluded.name, kind=excluded.kind, '
            'config_json=excluded.config_json, enabled=excluded.enabled, '
            'updated_at=excluded.updated_at',
            (server_id, name, kind, json.dumps(config, ensure_ascii=False),
             1 if enabled else 0, now, now))
        conn.commit()
    finally:
        conn.close()
    return get_mcp_server(db_path, server_id)


def delete_mcp_server(db_path: str, server_id: str) -> bool:
    conn = connect(db_path)
    try:
        cur = conn.execute('DELETE FROM mcp_servers WHERE id=?', (server_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_mcp_tools_cache(db_path: str, server_id: str, tools: list):
    conn = connect(db_path)
    try:
        conn.execute(
            'UPDATE mcp_servers SET tools_json=?, tools_cached_at=?, '
            'updated_at=? WHERE id=?',
            (json.dumps(tools, ensure_ascii=False), time.time(), time.time(),
             server_id))
        conn.commit()
    finally:
        conn.close()


def set_mcp_status(db_path: str, server_id: str, status: str, error: str = ''):
    conn = connect(db_path)
    try:
        conn.execute(
            'UPDATE mcp_servers SET status=?, last_error=?, updated_at=? WHERE id=?',
            (status, error[:500], time.time(), server_id))
        conn.commit()
    finally:
        conn.close()


# ── 用户档案 DAL ─────────────────────────────────────────────────

def upsert_user(db_path: str, user_id, name: str = '', group_id='') -> dict:
    """累积昵称/名片（最近使用在前，去重），更新最后出现时间与群。"""
    now = time.time()
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT * FROM users WHERE user_id=?',
                         (str(user_id),)).fetchone()
        name = (name or '').strip()
        if r is None:
            names = [name] if name else []
            conn.execute(
                'INSERT INTO users (user_id, names, first_seen, last_seen, '
                'last_group_id) VALUES (?,?,?,?,?)',
                (str(user_id), json.dumps(names, ensure_ascii=False), now, now,
                 str(group_id)))
        else:
            try:
                names = json.loads(r['names'] or '[]')
            except json.JSONDecodeError:
                names = []
            if name:
                names = [n for n in names if n != name]
                names.insert(0, name)
                names = names[:10]
            conn.execute(
                'UPDATE users SET names=?, last_seen=?, last_group_id=? '
                'WHERE user_id=?',
                (json.dumps(names, ensure_ascii=False), now, str(group_id),
                 str(user_id)))
        conn.commit()
        r2 = conn.execute('SELECT * FROM users WHERE user_id=?',
                          (str(user_id),)).fetchone()
        d = dict(r2)
        d['names'] = json.loads(d['names'] or '[]')
        return d
    finally:
        conn.close()


def get_user(db_path: str, user_id) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT * FROM users WHERE user_id=?',
                         (str(user_id),)).fetchone()
        if not r:
            return None
        d = dict(r)
        d['names'] = json.loads(d['names'] or '[]')
        return d
    finally:
        conn.close()


def list_users(db_path: str, query: str = '', limit: int = 100) -> list:
    conn = connect(db_path)
    try:
        if query:
            like = f'%{query}%'
            rows = conn.execute(
                "SELECT * FROM users WHERE user_id LIKE ? OR names LIKE ? "
                "ORDER BY last_seen DESC LIMIT ?",
                (like, like, int(limit))).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM users ORDER BY last_seen DESC LIMIT ?',
                (int(limit),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d['names'] = json.loads(d['names'] or '[]')
            out.append(d)
        return out
    finally:
        conn.close()


def group_roster(db_path: str, group_id, limit: int = 200) -> dict:
    """本群近期发言人 {规范化名字: user_id}，用于记忆抽取姓名映射。"""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            'SELECT sender_id, sender_name FROM messages WHERE group_id=? '
            'AND sender_id != "" ORDER BY id DESC LIMIT ?',
            (str(group_id), int(limit))).fetchall()
        roster = {}
        for r in rows:
            nm = (r['sender_name'] or '').strip()
            if nm and nm not in roster:
                roster[nm] = str(r['sender_id'])
            roster[str(r['sender_id'])] = str(r['sender_id'])
        return roster
    finally:
        conn.close()


# ── 记忆条目 DAL ─────────────────────────────────────────────────

def add_memory(db_path: str, user_id, text: str, tags: str = '',
               importance: float = 0.6, source_group_id: str = '',
               history: list | None = None, superseded: bool = False) -> int:
    now = time.time()
    conn = connect(db_path)
    try:
        cur = conn.execute(
            'INSERT INTO memory_entries (user_id, text, tags, importance, '
            'superseded, history, source_group_id, created_at, updated_at, '
            'last_accessed) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (str(user_id), text, tags, float(importance),
             1 if superseded else 0,
             json.dumps(history or [], ensure_ascii=False), str(source_group_id),
             now, now, 0))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_active_memories(db_path: str, user_id) -> list:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            'SELECT * FROM memory_entries WHERE user_id=? AND superseded=0 '
            'ORDER BY id ASC', (str(user_id),)).fetchall()
        return [_memory_row(r) for r in rows]
    finally:
        conn.close()


def list_memories(db_path: str, user_id, include_superseded=False) -> list:
    conn = connect(db_path)
    try:
        sql = 'SELECT * FROM memory_entries WHERE user_id=?'
        if not include_superseded:
            sql += ' AND superseded=0'
        sql += ' ORDER BY id DESC'
        rows = conn.execute(sql, (str(user_id),)).fetchall()
        return [_memory_row(r) for r in rows]
    finally:
        conn.close()


def get_memory(db_path: str, memory_id: int) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT * FROM memory_entries WHERE id=?',
                         (int(memory_id),)).fetchone()
        return _memory_row(r) if r else None
    finally:
        conn.close()


def update_memory(db_path: str, memory_id: int, *, text=None, tags=None,
                  importance=None, superseded=None) -> bool:
    fields, vals = [], []
    if text is not None:
        fields.append('text=?'); vals.append(text)
    if tags is not None:
        fields.append('tags=?'); vals.append(tags)
    if importance is not None:
        fields.append('importance=?'); vals.append(float(importance))
    if superseded is not None:
        fields.append('superseded=?'); vals.append(1 if superseded else 0)
    if not fields:
        return False
    fields.append('updated_at=?'); vals.append(time.time())
    vals.append(int(memory_id))
    conn = connect(db_path)
    try:
        cur = conn.execute(
            f'UPDATE memory_entries SET {", ".join(fields)} WHERE id=?', vals)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_memory_full(db_path: str, memory_id: int, text: str, tags: str,
                       importance: float, history: list):
    """抽取合并时用：就地更新文本，旧文入 history（保留 5 版）。"""
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT text, history FROM memory_entries WHERE id=?',
                         (int(memory_id),)).fetchone()
        if not r:
            return
        try:
            hist = json.loads(r['history'] or '[]')
        except json.JSONDecodeError:
            hist = []
        hist.append(r['text'])
        hist = hist[-5:]
        conn.execute(
            'UPDATE memory_entries SET text=?, tags=?, importance=?, history=?, '
            'updated_at=? WHERE id=?',
            (text, tags, float(importance),
             json.dumps(hist, ensure_ascii=False), time.time(), int(memory_id)))
        conn.commit()
    finally:
        conn.close()


def touch_memories(db_path: str, memory_ids: list):
    if not memory_ids:
        return
    now = time.time()
    conn = connect(db_path)
    try:
        conn.executemany(
            'UPDATE memory_entries SET last_accessed=?, access_count=access_count+1 '
            'WHERE id=?', [(now, int(i)) for i in memory_ids])
        conn.commit()
    finally:
        conn.close()


def delete_memory(db_path: str, memory_id: int) -> bool:
    conn = connect(db_path)
    try:
        cur = conn.execute('DELETE FROM memory_entries WHERE id=?',
                           (int(memory_id),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def forget_user(db_path: str, user_id) -> int:
    """软删除某人全部记忆（标 superseded），返回条数。"""
    conn = connect(db_path)
    try:
        cur = conn.execute(
            'UPDATE memory_entries SET superseded=1, updated_at=? '
            'WHERE user_id=? AND superseded=0', (time.time(), str(user_id)))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def memory_stats(db_path: str) -> dict:
    conn = connect(db_path)
    try:
        users = conn.execute('SELECT COUNT(*) c FROM users').fetchone()['c']
        active = conn.execute(
            'SELECT COUNT(*) c FROM memory_entries WHERE superseded=0'
        ).fetchone()['c']
        total = conn.execute('SELECT COUNT(*) c FROM memory_entries').fetchone()['c']
        return {'users': users, 'active_memories': active, 'total_memories': total}
    finally:
        conn.close()


def _memory_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d['history'] = json.loads(d.pop('history') or '[]')
    d['superseded'] = bool(d['superseded'])
    return d


# ── 抽取游标 DAL ─────────────────────────────────────────────────

def get_memory_state(db_path: str, group_id) -> dict:
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT * FROM memory_state WHERE group_id=?',
                         (str(group_id),)).fetchone()
        if r:
            return dict(r)
        return {'group_id': str(group_id), 'extracted_up_to_id': 0,
                'turns_since': 0, 'updated_at': 0.0}
    finally:
        conn.close()


def bump_memory_turn(db_path: str, group_id, n: int = 1) -> dict:
    get_or_create_group(db_path, group_id)
    conn = connect(db_path)
    try:
        conn.execute(
            'INSERT INTO memory_state (group_id, extracted_up_to_id, '
            'turns_since, updated_at) VALUES (?,0,?,?) '
            'ON CONFLICT(group_id) DO UPDATE SET turns_since=turns_since+?, '
            'updated_at=excluded.updated_at',
            (str(group_id), int(n), time.time(), int(n)))
        conn.commit()
    finally:
        conn.close()
    return get_memory_state(db_path, group_id)


def set_memory_cursor(db_path: str, group_id, up_to_id: int):
    """一轮抽取完成：推进游标并清零计数。"""
    conn = connect(db_path)
    try:
        conn.execute(
            'INSERT INTO memory_state (group_id, extracted_up_to_id, '
            'turns_since, updated_at) VALUES (?,?,0,?) '
            'ON CONFLICT(group_id) DO UPDATE SET extracted_up_to_id=?, '
            'turns_since=0, updated_at=excluded.updated_at',
            (str(group_id), int(up_to_id), time.time(), int(up_to_id)))
        conn.commit()
    finally:
        conn.close()


# ── 心情 DAL（每群一份：当前状态 + 时间线）─────────────────────

def _mood_state_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d.pop('dims_json', None)
    try:
        d['dims'] = json.loads(r['dims_json'] or '{}')
    except json.JSONDecodeError:
        d['dims'] = {}
    d['mood'] = float(r['mood'])
    for k in ('updated_at', 'last_judge_at', 'injected_for'):
        d[k] = float(r[k] or 0.0)
    for k in ('pending_delta', 'pending_confidence', 'pending_tid',
              'inject_count'):
        d[k] = int(r[k] or 0)
    return d


def mood_get_state(db_path: str, group_id, baseline: float = 70.0) -> dict:
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT * FROM mood_state WHERE group_id=?',
                         (str(group_id),)).fetchone()
        if not r:
            return {'group_id': str(group_id), 'mood': float(baseline),
                    'dims': {}, 'updated_at': 0.0, 'last_judge_at': 0.0,
                    'pending_delta': 0, 'pending_confidence': 0,
                    'pending_reason': '', 'pending_tid': 0,
                    'injected_for': 0.0, 'inject_day': '', 'inject_count': 0}
        return _mood_state_row(r)
    finally:
        conn.close()


def mood_save_judge(db_path: str, group_id, *, mood: float, dims: dict,
                    delta: int, confidence: int, reason: str,
                    source: str, now: float, keep: int = 200) -> int:
    """一次判定落库：写时间线 + 更新当前状态（pending 指向新时间线条目）。

    返回时间线 id。injected_for 不动——由 mood_mark_injected 标记。
    """
    gid = str(group_id)
    conn = connect(db_path)
    try:
        cur = conn.execute(
            'INSERT INTO mood_timeline (group_id, ts, kind, source, delta, '
            'mood_after, dims_json, reason, confidence, injected) '
            'VALUES (?,?,?,?,?,?,?,?,?,0)',
            (gid, now, 'judge', source, int(delta), float(mood),
             json.dumps(dims or {}, ensure_ascii=False), reason or '',
             int(confidence)))
        tid = cur.lastrowid
        if keep and keep > 0:
            conn.execute(
                'DELETE FROM mood_timeline WHERE group_id=? AND id NOT IN '
                '(SELECT id FROM mood_timeline WHERE group_id=? '
                'ORDER BY id DESC LIMIT ?)', (gid, gid, int(keep)))
        conn.execute(
            """INSERT INTO mood_state(group_id, mood, dims_json, updated_at,
                   last_judge_at, pending_delta, pending_confidence,
                   pending_reason, pending_tid)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(group_id) DO UPDATE SET
                   mood=excluded.mood, dims_json=excluded.dims_json,
                   updated_at=excluded.updated_at,
                   last_judge_at=excluded.last_judge_at,
                   pending_delta=excluded.pending_delta,
                   pending_confidence=excluded.pending_confidence,
                   pending_reason=excluded.pending_reason,
                   pending_tid=excluded.pending_tid""",
            (gid, float(mood), json.dumps(dims or {}, ensure_ascii=False),
             now, now, int(delta), int(confidence), reason or '', tid))
        conn.commit()
        return tid
    finally:
        conn.close()


def mood_mark_injected(db_path: str, group_id, *, day: str, count: int,
                       now: float):
    """宣布占用：injected_for 锚到 updated_at，记当日注入次数。"""
    gid = str(group_id)
    conn = connect(db_path)
    try:
        conn.execute(
            'UPDATE mood_state SET injected_for=updated_at, inject_day=?, '
            'inject_count=? WHERE group_id=?', (day, int(count), gid))
        conn.execute(
            'UPDATE mood_timeline SET injected=1 WHERE id='
            '(SELECT pending_tid FROM mood_state WHERE group_id=?)', (gid,))
        conn.commit()
    finally:
        conn.close()


def mood_reset(db_path: str, group_id, *, baseline: float = 70.0,
               now: float | None = None) -> dict:
    """手动重置：回到基线、清空七维与待宣布变化，时间线留一条 reset。"""
    gid = str(group_id)
    ts = now if now is not None else time.time()
    conn = connect(db_path)
    try:
        conn.execute(
            'INSERT INTO mood_timeline (group_id, ts, kind, source, delta, '
            'mood_after, dims_json, reason, confidence, injected) '
            'VALUES (?,?,?,?,?,?,?,?,?,0)',
            (gid, ts, 'reset', '', 0, float(baseline), '{}', '手动重置', 0))
        conn.execute(
            """INSERT INTO mood_state(group_id, mood, dims_json, updated_at,
                   last_judge_at, pending_delta, pending_confidence,
                   pending_reason, pending_tid, injected_for, inject_day,
                   inject_count)
               VALUES(?,?,'{}',?,0,0,0,'',0,?,'',0)
               ON CONFLICT(group_id) DO UPDATE SET
                   mood=excluded.mood, dims_json='{}',
                   updated_at=excluded.updated_at, last_judge_at=0,
                   pending_delta=0, pending_confidence=0,
                   pending_reason='', pending_tid=0,
                   injected_for=excluded.injected_for,
                   inject_day='', inject_count=0""",
            (gid, float(baseline), ts, ts))
        conn.commit()
    finally:
        conn.close()
    return mood_get_state(db_path, gid, baseline)


def mood_timeline(db_path: str, group_id, limit: int = 30) -> list:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            'SELECT * FROM mood_timeline WHERE group_id=? '
            'ORDER BY id DESC LIMIT ?',
            (str(group_id), int(limit))).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d['dims'] = json.loads(d.pop('dims_json') or '{}')
            except json.JSONDecodeError:
                d['dims'] = {}
            d['mood_after'] = float(r['mood_after'])
            d['injected'] = bool(r['injected'])
            out.append(d)
        return out
    finally:
        conn.close()


# ── 全局第一人称自我记忆 DAL（无 user_id，跨群/私聊同一份）──────

def _self_memory_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d['history'] = json.loads(d.pop('history') or '[]')
    d['superseded'] = bool(d['superseded'])
    return d


def add_self_memory(db_path: str, text: str, tags: str = '',
                    importance: float = 0.6, created_by_group: str = '',
                    history: list | None = None,
                    superseded: bool = False) -> int:
    now = time.time()
    conn = connect(db_path)
    try:
        cur = conn.execute(
            'INSERT INTO self_memories (text, tags, importance, superseded, '
            'history, created_by_group, created_at, updated_at, '
            'last_accessed) VALUES (?,?,?,?,?,?,?,?,0)',
            (text, tags, float(importance), 1 if superseded else 0,
             json.dumps(history or [], ensure_ascii=False),
             str(created_by_group), now, now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_active_self_memories(db_path: str) -> list:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            'SELECT * FROM self_memories WHERE superseded=0 ORDER BY id ASC'
        ).fetchall()
        return [_self_memory_row(r) for r in rows]
    finally:
        conn.close()


def list_self_memories(db_path: str, include_superseded: bool = False) -> list:
    conn = connect(db_path)
    try:
        sql = 'SELECT * FROM self_memories'
        if not include_superseded:
            sql += ' WHERE superseded=0'
        sql += ' ORDER BY id DESC'
        rows = conn.execute(sql).fetchall()
        return [_self_memory_row(r) for r in rows]
    finally:
        conn.close()


def get_self_memory(db_path: str, memory_id: int) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT * FROM self_memories WHERE id=?',
                         (int(memory_id),)).fetchone()
        return _self_memory_row(r) if r else None
    finally:
        conn.close()


def _revision_note(old: str, new: str, clip: int = 150) -> str:
    """revise 类 trace 的改动摘要（不做真 diff，截断够用）。"""
    return f'旧:{(old or "")[:clip]} | 新:{(new or "")[:clip]}'


def _insert_self_memory_trace(conn, memory_id: int, kind: str, now: float,
                              **kw):
    """在已开事务的 conn 上插一条回忆链并维护冗余计数。"""
    conn.execute(
        'INSERT INTO self_memory_traces (memory_id, kind, context, group_id, '
        'source, mood_tag, mood_level, seen_text, revision, match_score, '
        'created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (int(memory_id), str(kind), str(kw.get('context', '') or '')[:300],
         str(kw.get('group_id', '') or ''), str(kw.get('source', '') or ''),
         str(kw.get('mood_tag', '') or ''), int(kw.get('mood_level', 0) or 0),
         str(kw.get('seen_text', '') or '')[:400],
         str(kw.get('revision', '') or ''),
         float(kw.get('match_score', 0) or 0.0), now))
    conn.execute(
        'UPDATE self_memories SET trace_count=trace_count+1 WHERE id=?',
        (int(memory_id),))


def update_self_memory(db_path: str, memory_id: int, *, text=None, tags=None,
                       importance=None, superseded=None,
                       trace: dict | None = None) -> bool:
    """改事实层。trace 给定时同事务强制留痕：

    - superseded=True → kind='supersede'；
    - 带 text 变更    → kind=trace['kind']（web_edit/revise），revision 自动填。
    """
    fields, vals = [], []
    if text is not None:
        fields.append('text=?'); vals.append(text)
    if tags is not None:
        fields.append('tags=?'); vals.append(tags)
    if importance is not None:
        fields.append('importance=?'); vals.append(float(importance))
    if superseded is not None:
        fields.append('superseded=?'); vals.append(1 if superseded else 0)
    if not fields:
        return False
    now = time.time()
    fields.append('updated_at=?'); vals.append(now)
    vals.append(int(memory_id))
    conn = connect(db_path)
    try:
        old = conn.execute(
            'SELECT text FROM self_memories WHERE id=?',
            (int(memory_id),)).fetchone() if trace is not None else None
        cur = conn.execute(
            f'UPDATE self_memories SET {", ".join(fields)} WHERE id=?', vals)
        if cur.rowcount > 0 and trace is not None:
            kind = trace.get('kind') or (
                'supersede' if superseded else 'revise')
            kw = {k: trace.get(k, '') for k in
                  ('context', 'group_id', 'source', 'mood_tag')}
            kw['mood_level'] = trace.get('mood_level', 0)
            kw['seen_text'] = text if text is not None else \
                (old['text'] if old else '')
            if text is not None and old is not None:
                kw['revision'] = _revision_note(old['text'], text)
            _insert_self_memory_trace(conn, memory_id, kind, now, **kw)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_self_memory_full(db_path: str, memory_id: int, text: str,
                            tags: str, importance: float, history: list,
                            *, trace_group_id: str = '',
                            trace_context: str = '',
                            trace_source: str = ''):
    """抽取合并：就地改写文本，旧文入 history（保留 5 版）+ 强制 revise 留痕。"""
    conn = connect(db_path)
    try:
        r = conn.execute(
            'SELECT text, history FROM self_memories WHERE id=?',
            (int(memory_id),)).fetchone()
        if not r:
            return
        now = time.time()
        try:
            hist = json.loads(r['history'] or '[]')
        except json.JSONDecodeError:
            hist = []
        hist.append(r['text'])
        hist = hist[-5:]
        conn.execute(
            'UPDATE self_memories SET text=?, tags=?, importance=?, history=?, '
            'updated_at=? WHERE id=?',
            (text, tags, float(importance),
             json.dumps(hist, ensure_ascii=False), now, int(memory_id)))
        if r['text'] != text:
            _insert_self_memory_trace(
                conn, memory_id, 'revise', now,
                context=trace_context, group_id=trace_group_id,
                source=trace_source, seen_text=text,
                revision=_revision_note(r['text'], text))
        conn.commit()
    finally:
        conn.close()


def touch_self_memories(db_path: str, memory_ids: list):
    if not memory_ids:
        return
    now = time.time()
    conn = connect(db_path)
    try:
        conn.executemany(
            'UPDATE self_memories SET last_accessed=?, '
            'access_count=access_count+1 WHERE id=?',
            [(now, int(i)) for i in memory_ids])
        conn.commit()
    finally:
        conn.close()


def touch_and_trace_self_memories(db_path: str, memory_ids: list,
                                  trace_rows: list | None = None):
    """召回收尾：touch 与回忆链写入合并成单事务（trace_rows 可空）。

    trace_rows 每项 {'memory_id','kind','context','group_id','source',
    'mood_tag','mood_level','seen_text','revision','match_score'}。
    """
    if not memory_ids:
        return
    now = time.time()
    conn = connect(db_path)
    try:
        conn.executemany(
            'UPDATE self_memories SET last_accessed=?, '
            'access_count=access_count+1 WHERE id=?',
            [(now, int(i)) for i in memory_ids])
        if trace_rows:
            for tr in trace_rows:
                _insert_self_memory_trace(
                    conn, tr['memory_id'], str(tr.get('kind', 'recall')),
                    now,
                    context=tr.get('context', ''),
                    group_id=tr.get('group_id', ''),
                    source=tr.get('source', ''),
                    mood_tag=tr.get('mood_tag', ''),
                    mood_level=tr.get('mood_level', 0),
                    seen_text=tr.get('seen_text', ''),
                    revision=tr.get('revision', ''),
                    match_score=tr.get('match_score', 0.0))
        conn.commit()
    finally:
        conn.close()


def list_self_memory_traces(db_path: str, memory_id: int,
                            limit: int = 20) -> list:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            'SELECT * FROM self_memory_traces WHERE memory_id=? '
            'ORDER BY id DESC LIMIT ?',
            (int(memory_id), int(limit))).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_self_memory(db_path: str, memory_id: int) -> bool:
    conn = connect(db_path)
    try:
        cur = conn.execute('DELETE FROM self_memories WHERE id=?',
                           (int(memory_id),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def self_memory_stats(db_path: str) -> dict:
    conn = connect(db_path)
    try:
        active = conn.execute(
            'SELECT COUNT(*) c FROM self_memories WHERE superseded=0'
        ).fetchone()['c']
        total = conn.execute(
            'SELECT COUNT(*) c FROM self_memories').fetchone()['c']
        return {'active_self_memories': active, 'total_self_memories': total}
    finally:
        conn.close()


# ── 自我记忆每日写配额 DAL ─────────────────────────────────────

def self_quota_get(db_path: str, day: str) -> int:
    conn = connect(db_path)
    try:
        r = conn.execute(
            'SELECT writes FROM self_memory_state WHERE day=?',
            (day,)).fetchone()
        return int(r['writes']) if r else 0
    finally:
        conn.close()


def self_quota_bump(db_path: str, day: str, n: int = 1):
    conn = connect(db_path)
    try:
        conn.execute(
            'INSERT INTO self_memory_state (day, writes, updated_at) '
            'VALUES (?,?,?) ON CONFLICT(day) DO UPDATE SET '
            'writes=writes+excluded.writes, updated_at=excluded.updated_at',
            (day, int(n), time.time()))
        conn.commit()
    finally:
        conn.close()


# ── 群聊原始语料 DAL（风格学习用，与对话窗互不影响）────────────

def insert_raw_message(db_path: str, group_id, sender_id, sender_name,
                       content: str, created_at: float | None = None) -> int:
    conn = connect(db_path)
    try:
        cur = conn.execute(
            'INSERT INTO raw_messages (group_id, sender_id, sender_name, '
            'content, created_at) VALUES (?,?,?,?,?)',
            (str(group_id), str(sender_id or ''), sender_name or '',
             content or '', created_at if created_at is not None else time.time()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def count_raw_after(db_path: str, group_id, after_id: int) -> int:
    conn = connect(db_path)
    try:
        return conn.execute(
            'SELECT COUNT(*) c FROM raw_messages WHERE group_id=? AND id>?',
            (str(group_id), int(after_id))).fetchone()['c']
    finally:
        conn.close()


def raw_after(db_path: str, group_id, after_id: int, limit: int) -> list:
    """游标之后最早的一批原始语料（正序）。"""
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute(
            'SELECT id, sender_id, sender_name, content FROM raw_messages '
            'WHERE group_id=? AND id>? ORDER BY id ASC LIMIT ?',
            (str(group_id), int(after_id), int(limit))).fetchall()]
    finally:
        conn.close()


def trim_raw(db_path: str, group_id, cap: int) -> int:
    """每群滚动只留最近 cap 条，返回删除条数。"""
    cap = int(cap)
    if cap <= 0:
        return 0
    conn = connect(db_path)
    try:
        cur = conn.execute(
            'DELETE FROM raw_messages WHERE group_id=? AND id NOT IN '
            '(SELECT id FROM raw_messages WHERE group_id=? ORDER BY id DESC LIMIT ?)',
            (str(group_id), str(group_id), cap))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def clear_raw(db_path: str, group_id):
    conn = connect(db_path)
    try:
        conn.execute('DELETE FROM raw_messages WHERE group_id=?', (str(group_id),))
        conn.commit()
    finally:
        conn.close()


# ── 背景闲聊游标 DAL（前台对话轮的未触发消息注入）──────────────

def get_raw_cursor(db_path: str, group_id) -> int:
    """上轮已注入背景消息的最大 raw id；从未注入过返回 0。"""
    conn = connect(db_path)
    try:
        r = conn.execute(
            'SELECT last_id FROM raw_cursors WHERE group_id=?',
            (str(group_id),)).fetchone()
        return int(r['last_id']) if r else 0
    finally:
        conn.close()


def set_raw_cursor(db_path: str, group_id, last_id: int):
    conn = connect(db_path)
    try:
        conn.execute(
            'INSERT INTO raw_cursors (group_id, last_id) VALUES (?,?) '
            'ON CONFLICT(group_id) DO UPDATE SET last_id=excluded.last_id',
            (str(group_id), int(last_id)))
        conn.commit()
    finally:
        conn.close()


def ambient_raw_rows(db_path: str, group_id, after_id: int, before_id: int,
                     n: int) -> list:
    """取 (after_id, before_id) 区间内最近 n 条原始群消息，按时间正序返回。

    after_id=0（首轮）即取触发消息之前最近 n 条；行已被 trim 清掉时自然少几条，
    游标超前（语料被清空）时返回空列表，不报错。
    """
    n = max(1, int(n))
    conn = connect(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            'SELECT id, sender_id, sender_name, content, created_at '
            'FROM raw_messages WHERE group_id=? AND id>? AND id<? '
            'ORDER BY id DESC LIMIT ?',
            (str(group_id), int(after_id), int(before_id), n)).fetchall()]
        rows.reverse()
        return rows
    finally:
        conn.close()


# ── 群体风格画像 DAL ───────────────────────────────────────────

def get_style(db_path: str, group_id) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT * FROM group_style WHERE group_id=?',
                         (str(group_id),)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def set_style(db_path: str, group_id, style_text: str, up_to_id: int,
              sample_count: int):
    conn = connect(db_path)
    try:
        conn.execute(
            'INSERT INTO group_style (group_id, style_text, up_to_id, '
            'sample_count, built_at, updated_at) VALUES (?,?,?,?,?,?) '
            'ON CONFLICT(group_id) DO UPDATE SET style_text=excluded.style_text, '
            'up_to_id=excluded.up_to_id, sample_count=excluded.sample_count, '
            'built_at=excluded.built_at, updated_at=excluded.updated_at',
            (str(group_id), style_text, int(up_to_id), int(sample_count),
             time.time(), time.time()))
        conn.commit()
    finally:
        conn.close()


def clear_style(db_path: str, group_id):
    conn = connect(db_path)
    try:
        conn.execute('DELETE FROM group_style WHERE group_id=?', (str(group_id),))
        conn.commit()
    finally:
        conn.close()


# ── 约定/待办 DAL ──────────────────────────────────────────────

def add_commitment(db_path: str, group_id, owner_id, owner_name, content: str,
                   kind: str = 'task', due_text: str = '',
                   due_at: float | None = None, source: str = 'extract') -> int:
    now = time.time()
    conn = connect(db_path)
    try:
        cur = conn.execute(
            'INSERT INTO commitments (group_id, owner_id, owner_name, content, '
            'kind, due_text, due_at, status, reminded, source, created_at, '
            'updated_at) VALUES (?,?,?,?,?,?,?,"open",0,?,?,?)',
            (str(group_id), str(owner_id or ''), owner_name or '', content,
             kind if kind in ('task', 'topic') else 'task', due_text,
             due_at, source, now, now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _commitment_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d['reminded'] = bool(d['reminded'])
    return d


def list_commitments(db_path: str, group_id='', status: str = 'open',
                     limit: int = 200) -> list:
    conn = connect(db_path)
    try:
        where, params = [], []
        if group_id != '':
            where.append('group_id=?'); params.append(str(group_id))
        if status:
            where.append('status=?'); params.append(status)
        sql = 'SELECT * FROM commitments'
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY COALESCE(due_at, 9e18) ASC, id DESC LIMIT ?'
        params.append(int(limit))
        return [_commitment_row(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def open_commitments_for(db_path: str, group_id, user_ids, limit: int = 10) -> list:
    """某群里、归属给定用户的未结约定/话题（到期的排前）。"""
    ids = [str(u) for u in user_ids if u]
    if not ids:
        return []
    conn = connect(db_path)
    try:
        ph = ','.join('?' * len(ids))
        return [_commitment_row(r) for r in conn.execute(
            f'SELECT * FROM commitments WHERE group_id=? AND status="open" '
            f'AND owner_id IN ({ph}) '
            'ORDER BY COALESCE(due_at, 9e18) ASC, id DESC LIMIT ?',
            (str(group_id), *ids, int(limit))).fetchall()]
    finally:
        conn.close()


def get_commitment(db_path: str, commitment_id: int) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT * FROM commitments WHERE id=?',
                         (int(commitment_id),)).fetchone()
        return _commitment_row(r) if r else None
    finally:
        conn.close()


def set_commitment_status(db_path: str, commitment_id: int, status: str) -> bool:
    if status not in ('open', 'done', 'cancelled'):
        return False
    conn = connect(db_path)
    try:
        cur = conn.execute(
            'UPDATE commitments SET status=?, updated_at=? WHERE id=?',
            (status, time.time(), int(commitment_id)))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def find_open_commitment(db_path: str, group_id, owner_id, content: str,
                         min_ratio: float = 0.55) -> dict | None:
    """按文本相似度找某人的未结事项（抽取 op=done 时用，纯 SQL+difflib 粗配）。"""
    from difflib import SequenceMatcher
    best, best_r = None, 0.0
    for c in list_commitments(db_path, group_id, 'open', 200):
        if owner_id and c['owner_id'] != str(owner_id):
            continue
        r = SequenceMatcher(None, content or '', c['content']).ratio()
        if r > best_r:
            best, best_r = c, r
    return best if best_r >= min_ratio else None


def due_commitments(db_path: str, now_ts: float) -> list:
    conn = connect(db_path)
    try:
        return [_commitment_row(r) for r in conn.execute(
            'SELECT * FROM commitments WHERE status="open" AND reminded=0 '
            'AND due_at IS NOT NULL AND due_at<=? ORDER BY due_at ASC LIMIT 100',
            (float(now_ts),)).fetchall()]
    finally:
        conn.close()


def mark_reminded(db_path: str, commitment_id: int):
    conn = connect(db_path)
    try:
        conn.execute('UPDATE commitments SET reminded=1, updated_at=? WHERE id=?',
                     (time.time(), int(commitment_id)))
        conn.commit()
    finally:
        conn.close()


def cancel_user_commitments(db_path: str, user_id) -> int:
    """/ai 忘记 时顺带取消某人全部未结事项。"""
    conn = connect(db_path)
    try:
        cur = conn.execute(
            'UPDATE commitments SET status="cancelled", updated_at=? '
            'WHERE owner_id=? AND status="open"',
            (time.time(), str(user_id)))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ── AI 定时任务 DAL ─────────────────────────────────────────────

def add_job(db_path: str, row: dict) -> int:
    now = time.time()
    conn = connect(db_path)
    try:
        cur = conn.execute(
            'INSERT INTO scheduled_jobs (creator_id, creator_name, group_id, '
            'target_type, target_id, mode, once_at, run_hhmm, weekday, brief, '
            'next_run_at, enabled, last_run_at, last_error, created_at, updated_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,1,NULL,"",?,?)',
            (str(row.get('creator_id', '')), row.get('creator_name', ''),
             str(row.get('group_id', '')),
             row.get('target_type', 'group') if row.get('target_type') in
             ('group', 'private') else 'group',
             str(row.get('target_id', '')),
             row.get('mode', 'once') if row.get('mode') in
             ('once', 'daily', 'weekly') else 'once',
             row.get('once_at'), row.get('run_hhmm', ''),
             int(row.get('weekday', -1)), row.get('brief', '')[:500],
             float(row['next_run_at']), now, now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _job_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d['enabled'] = bool(d['enabled'])
    return d


def get_job(db_path: str, job_id: int) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT * FROM scheduled_jobs WHERE id=?',
                         (int(job_id),)).fetchone()
        return _job_row(r) if r else None
    finally:
        conn.close()


def due_jobs(db_path: str, now_ts: float, limit: int = 50) -> list:
    conn = connect(db_path)
    try:
        return [_job_row(r) for r in conn.execute(
            'SELECT * FROM scheduled_jobs WHERE enabled=1 AND next_run_at<=? '
            'ORDER BY next_run_at ASC LIMIT ?',
            (float(now_ts), int(limit))).fetchall()]
    finally:
        conn.close()


def list_jobs(db_path: str, group_id: str = '', creator_id: str = '',
              include_disabled: bool = True, limit: int = 100) -> list:
    """群内可见：发往该群的，或该群友创建的（含私发任务）。"""
    scopes, params = [], []
    if group_id != '':
        scopes.append('(group_id=? OR target_id=?)')
        params.extend([str(group_id), str(group_id)])
    if creator_id != '':
        scopes.append('creator_id=?')
        params.append(str(creator_id))
    where = ' OR '.join(scopes)
    if not include_disabled:
        where = f'({where}) AND enabled=1' if where else 'enabled=1'
    sql = 'SELECT * FROM scheduled_jobs'
    if where:
        sql += ' WHERE ' + where
    sql += ' ORDER BY id DESC LIMIT ?'
    params.append(int(limit))
    conn = connect(db_path)
    try:
        return [_job_row(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def list_all_enabled_jobs(db_path: str) -> list:
    conn = connect(db_path)
    try:
        return [_job_row(r) for r in conn.execute(
            'SELECT * FROM scheduled_jobs WHERE enabled=1 ORDER BY id').fetchall()]
    finally:
        conn.close()


def delete_job(db_path: str, job_id: int) -> bool:
    conn = connect(db_path)
    try:
        cur = conn.execute('DELETE FROM scheduled_jobs WHERE id=?',
                           (int(job_id),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def job_mark_run(db_path: str, job_id: int, next_run_at: float | None,
                 enabled: bool, error: str = ''):
    """触发后推进：循环任务给下次时间；单次任务停用时 next_run_at 保留原值。"""
    conn = connect(db_path)
    try:
        if next_run_at is None:
            conn.execute(
                'UPDATE scheduled_jobs SET enabled=?, last_run_at=?, '
                'last_error=?, updated_at=? WHERE id=?',
                (1 if enabled else 0, time.time(), error[:300], time.time(),
                 int(job_id)))
        else:
            conn.execute(
                'UPDATE scheduled_jobs SET enabled=1, next_run_at=?, '
                'last_run_at=?, last_error=?, updated_at=? WHERE id=?',
                (float(next_run_at), time.time(), error[:300], time.time(),
                 int(job_id)))
        conn.commit()
    finally:
        conn.close()


# ── 用量 DAL ─────────────────────────────────────────────────────

def add_usage(db_path: str, row: dict):
    conn = connect(db_path)
    try:
        conn.execute(
            'INSERT INTO usage_log (group_id, kind, tokens_in, cache_hit, '
            'tokens_out, tool_rounds, ok, created_at) VALUES (?,?,?,?,?,?,?,?)',
            (str(row.get('group_id', '')), row.get('kind', 'chat'),
             int(row.get('tokens_in', 0)), int(row.get('cache_hit', 0)),
             int(row.get('tokens_out', 0)), int(row.get('tool_rounds', 0)),
             1 if row.get('ok', True) else 0, time.time()))
        conn.commit()
    finally:
        conn.close()


def usage_summary(db_path: str, group_id=None, limit_rows: int = 500) -> dict:
    conn = connect(db_path)
    try:
        where, params = '', []
        if group_id is not None:
            where, params = 'WHERE group_id=?', [str(group_id)]
        rows = conn.execute(
            f'SELECT * FROM usage_log {where} ORDER BY id DESC LIMIT ?',
            (*params, int(limit_rows))).fetchall()
        agg = {'calls': len(rows), 'tokens_in': 0, 'cache_hit': 0,
               'tokens_out': 0, 'by_kind': {}}
        for r in reversed(rows):
            agg['tokens_in'] += r['tokens_in']
            agg['cache_hit'] += r['cache_hit']
            agg['tokens_out'] += r['tokens_out']
            k = r['kind']
            agg['by_kind'].setdefault(k, 0)
            agg['by_kind'][k] += 1
        return agg
    finally:
        conn.close()


def usage_breakdown(db_path: str, group_id: str = '', since: float = 0) -> dict:
    """SQL 侧全量聚合：返回三类 token（输入命中/输入未命中/输出）总量与按
    调用种类（chat/judge/summary/extract/style/...）的拆分。

    cache_hit 是 tokens_in 的子集，故「输入未命中 = tokens_in - cache_hit」。
    """
    where, args = [], []
    if str(group_id or ''):
        where.append('group_id=?')
        args.append(str(group_id))
    if since:
        where.append('created_at>=?')
        args.append(float(since))
    sql_where = (' WHERE ' + ' AND '.join(where)) if where else ''
    conn = connect(db_path)
    try:
        rows = conn.execute(
            'SELECT kind, '
            'COALESCE(SUM(cache_hit),0) AS cached, '
            'COALESCE(SUM(tokens_in),0) AS tin, '
            'COALESCE(SUM(tokens_out),0) AS tout, '
            'COALESCE(SUM(ok),0) AS ok_n, COUNT(*) AS calls '
            f'FROM usage_log{sql_where} GROUP BY kind',
            args).fetchall()
        by_kind, totals = [], {
            'cached': 0, 'uncached': 0, 'out': 0, 'calls': 0, 'ok_calls': 0}
        for r in rows:
            cached, tin, tout = int(r['cached']), int(r['tin']), int(r['tout'])
            item = {'kind': r['kind'], 'cached': cached,
                    'uncached': max(0, tin - cached), 'out': tout,
                    'calls': int(r['calls']), 'ok_calls': int(r['ok_n'])}
            by_kind.append(item)
            for k in ('cached', 'uncached', 'out'):
                totals[k] += item[k]
            totals['calls'] += item['calls']
            totals['ok_calls'] += item['ok_calls']
        by_kind.sort(key=lambda x: -(x['cached'] + x['uncached'] + x['out']))
        return {'totals': totals, 'by_kind': by_kind}
    finally:
        conn.close()


# ── 私聊文件清单 DAL ──────────────────────────────────────────────

def upsert_private_file(db_path: str, user_id, file_id: str, busid='',
                        name='', file_size=0, msg_time=0,
                        ref_json: str = '') -> bool:
    """登记/刷新一条私聊文件引用。返回是否为新记录。"""
    if not file_id:
        return False
    conn = connect(db_path)
    try:
        exists = conn.execute(
            'SELECT 1 FROM private_files WHERE user_id=? AND file_id=? AND busid=?',
            (str(user_id), str(file_id), str(busid or ''))).fetchone()
        if exists:
            # 刷新时不带 ref_json（空串）就保留旧引用：补登记的消息可能更简略
            conn.execute(
                'UPDATE private_files SET name=?, file_size=?, msg_time=?, '
                'ref_json=COALESCE(NULLIF(?, \'\'), ref_json) '
                'WHERE user_id=? AND file_id=? AND busid=?',
                (name, int(file_size or 0), int(msg_time or 0), ref_json,
                 str(user_id), str(file_id), str(busid or '')))
            conn.commit()
            return False
        conn.execute(
            'INSERT INTO private_files (user_id, file_id, busid, name, '
            'file_size, msg_time, ref_json, created_at) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (str(user_id), str(file_id), str(busid or ''), name,
             int(file_size or 0), int(msg_time or 0), ref_json, time.time()))
        conn.commit()
        return True
    finally:
        conn.close()


def list_private_files(db_path: str, user_id, limit: int = 30) -> list:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            'SELECT * FROM private_files WHERE user_id=? '
            'ORDER BY msg_time DESC, id DESC LIMIT ?',
            (str(user_id), int(limit))).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_private_file(db_path: str, user_id, file_id: str) -> dict | None:
    """按 file_id 取该用户清单里的文件（不允许跨用户取）。"""
    conn = connect(db_path)
    try:
        r = conn.execute(
            'SELECT * FROM private_files WHERE user_id=? AND file_id=?',
            (str(user_id), str(file_id))).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


# ── 自我提示词注入 DAL ────────────────────────────────────────────

def add_injection(db_path: str, scope: str, title: str, text: str,
                  created_by: str = '') -> int:
    """新增一条注入。scope=群号（私聊=对方 QQ）或 GLOBAL_INJECTION_SCOPE。"""
    now = time.time()
    conn = connect(db_path)
    try:
        cur = conn.execute(
            'INSERT INTO prompt_injections (scope, title, text, enabled, '
            'created_by, created_at, updated_at) VALUES (?,?,?,1,?,?,?)',
            (str(scope), title[:40], text, str(created_by or ''), now, now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _injection_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d['enabled'] = bool(d['enabled'])
    d['global'] = (d['scope'] == GLOBAL_INJECTION_SCOPE)
    return d


def get_injection(db_path: str, injection_id: int) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT * FROM prompt_injections WHERE id=?',
                         (int(injection_id),)).fetchone()
        return _injection_row(r) if r else None
    finally:
        conn.close()


def list_active_injections(db_path: str, chat_id, limit: int = 50) -> list:
    """本群/私聊生效的启用条目：全局在前、本群在后，各自按 id 正序。"""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            'SELECT * FROM prompt_injections WHERE enabled=1 '
            "AND scope IN (?, '*') ORDER BY "
            "CASE scope WHEN '*' THEN 0 ELSE 1 END, id ASC LIMIT ?",
            (str(chat_id), int(limit))).fetchall()
        return [_injection_row(r) for r in rows]
    finally:
        conn.close()


def list_injections(db_path: str, chat_id='') -> list:
    """管理用清单：含停用条目。chat_id 给定时只列全局 + 该会话的。"""
    conn = connect(db_path)
    try:
        if chat_id == '':
            rows = conn.execute(
                'SELECT * FROM prompt_injections ORDER BY id DESC'
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM prompt_injections WHERE scope IN (?, '*') "
                'ORDER BY CASE scope '
                "WHEN '*' THEN 0 ELSE 1 END, id DESC",
                (str(chat_id),)).fetchall()
        return [_injection_row(r) for r in rows]
    finally:
        conn.close()


def set_injection_enabled(db_path: str, injection_id: int, enabled: bool) -> bool:
    conn = connect(db_path)
    try:
        cur = conn.execute(
            'UPDATE prompt_injections SET enabled=?, updated_at=? WHERE id=?',
            (1 if enabled else 0, time.time(), int(injection_id)))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_injection(db_path: str, injection_id: int) -> bool:
    conn = connect(db_path)
    try:
        cur = conn.execute('DELETE FROM prompt_injections WHERE id=?',
                           (int(injection_id),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def count_injections(db_path: str, scope: str) -> int:
    conn = connect(db_path)
    try:
        return conn.execute(
            'SELECT COUNT(*) c FROM prompt_injections WHERE scope=?',
            (str(scope),)).fetchone()['c']
    finally:
        conn.close()


# ── 定时概率唤醒（proactor）DAL ──────────────────────────────────

def proactor_get(db_path: str, group_id) -> dict:
    conn = connect(db_path)
    try:
        r = conn.execute(
            'SELECT * FROM proactor_state WHERE group_id=?',
            (str(group_id),)).fetchone()
        keys = set(r.keys()) if r else set()
        if not r:
            return {'ticks_since_fire': 0, 'last_tick_at': 0.0,
                    'last_fire_at': 0.0, 'speakup_miss': 0}
        d = {'ticks_since_fire': int(r['ticks_since_fire']),
             'last_tick_at': float(r['last_tick_at'] or 0.0),
             'last_fire_at': float(r['last_fire_at'] or 0.0)}
        # v12 之前的库刚读到时理论上已经迁移过；保留防御
        d['speakup_miss'] = int(r['speakup_miss']) if 'speakup_miss' in keys else 0
        return d
    finally:
        conn.close()


def proactor_record(db_path: str, group_id, fired: bool, now_ts: float):
    """一次 tick 记账：触发则计数清零并记 last_fire；未触发计数 +1。

    触发（任何一种主动轮）同时清零主动搭话连没中计数。
    """
    conn = connect(db_path)
    try:
        if fired:
            conn.execute(
                """INSERT INTO proactor_state(group_id,ticks_since_fire,
                       last_tick_at,last_fire_at,speakup_miss)
                   VALUES(?,0,?,?,0)
                   ON CONFLICT(group_id) DO UPDATE SET
                       ticks_since_fire=0, last_tick_at=excluded.last_tick_at,
                       last_fire_at=excluded.last_fire_at,
                       speakup_miss=0""",
                (str(group_id), now_ts, now_ts))
        else:
            conn.execute(
                """INSERT INTO proactor_state(group_id,ticks_since_fire,
                       last_tick_at,last_fire_at)
                   VALUES(?,1,?,0)
                   ON CONFLICT(group_id) DO UPDATE SET
                       ticks_since_fire=ticks_since_fire+1,
                       last_tick_at=excluded.last_tick_at""",
                (str(group_id), now_ts))
        conn.commit()
    finally:
        conn.close()


def proactor_bump_speakup_miss(db_path: str, group_id, now_ts: float):
    """主动搭话掷了但没中：连没中 +1（不动 ticks_since_fire）。"""
    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT INTO proactor_state(group_id,ticks_since_fire,
                   last_tick_at,last_fire_at,speakup_miss)
               VALUES(?,0,?,0,1)
               ON CONFLICT(group_id) DO UPDATE SET
                   speakup_miss=speakup_miss+1,
                   last_tick_at=excluded.last_tick_at""",
            (str(group_id), now_ts))
        conn.commit()
    finally:
        conn.close()


def last_human_message_ts(db_path: str, group_id) -> float:
    """最近一条真人消息时间（排除系统合成消息/机器人消息）。"""
    conn = connect(db_path)
    try:
        r = conn.execute(
            """SELECT MAX(created_at) m FROM messages
               WHERE group_id=? AND role='user' AND sender_id != ''""",
            (str(group_id),)).fetchone()
        return float(r['m'] or 0.0)
    finally:
        conn.close()


def last_assistant_message_ts(db_path: str, group_id) -> float:
    """机器人最近一次发言时间（主动搭话自我冷却用）。"""
    conn = connect(db_path)
    try:
        r = conn.execute(
            "SELECT MAX(created_at) m FROM messages "
            "WHERE group_id=? AND role='assistant'",
            (str(group_id),)).fetchone()
        return float(r['m'] or 0.0)
    finally:
        conn.close()


# ── AI 自定义工具 DAL ────────────────────────────────────────────

def _custom_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d['enabled'] = bool(r['enabled'])
    d['owner_only_call'] = bool(r['owner_only_call'])
    d['global'] = d['scope'] == GLOBAL_CUSTOM_SCOPE
    try:
        d['schema'] = json.loads(r['params_schema'] or '{}')
    except (ValueError, TypeError):
        d['schema'] = {}
    return d


def upsert_custom_tool(db_path: str, fields: dict) -> int:
    """按 (scope,name) 插入或整体覆盖（调用方须先在 worker 验证通过）。"""
    now = time.time()
    conn = connect(db_path)
    try:
        r = conn.execute(
            'SELECT id FROM custom_tools WHERE scope=? AND name=?',
            (fields['scope'], fields['name'])).fetchone()
        if r:
            conn.execute(
                """UPDATE custom_tools SET title=?,description=?,params_schema=?,
                       code=?,enabled=1,owner_only_call=?,status='ok',
                       created_by=?,updated_at=? WHERE id=?""",
                (fields.get('title', ''), fields['description'],
                 fields['params_schema'], fields['code'],
                 1 if fields.get('owner_only_call') else 0,
                 fields.get('created_by', ''), now, r['id']))
            conn.commit()
            return int(r['id'])
        cur = conn.execute(
            """INSERT INTO custom_tools(scope,name,title,description,params_schema,
                   code,enabled,owner_only_call,status,created_by,
                   created_at,updated_at)
               VALUES(?,?,?,?,?,?,1,?,'ok',?,?,?)""",
            (fields['scope'], fields['name'], fields.get('title', ''),
             fields['description'], fields['params_schema'], fields['code'],
             1 if fields.get('owner_only_call') else 0,
             fields.get('created_by', ''), now, now))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_custom_tools(db_path: str, chat_id: str = '',
                      enabled_only: bool = False) ->[dict]:
    """列工具：给 chat_id 时含本群 + 全局；不给则全表（管理用）。"""
    conn = connect(db_path)
    try:
        if chat_id:
            sql = 'SELECT * FROM custom_tools WHERE scope IN (?, ?)'
            args = [str(chat_id), GLOBAL_CUSTOM_SCOPE]
            if enabled_only:
                sql += ' AND enabled=1'
            rows = conn.execute(
                sql + ' ORDER BY CASE scope WHEN ? THEN 0 ELSE 1 END, id',
                (*args, GLOBAL_CUSTOM_SCOPE)).fetchall()
        else:
            sql = 'SELECT * FROM custom_tools'
            if enabled_only:
                sql += ' WHERE enabled=1'
            rows = conn.execute(sql + ' ORDER BY scope, id').fetchall()
        return [_custom_row(r) for r in rows]
    finally:
        conn.close()


def get_custom_tool(db_path: str, tool_id: int) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT * FROM custom_tools WHERE id=?',
                         (int(tool_id),)).fetchone()
        return _custom_row(r) if r else None
    finally:
        conn.close()


def delete_custom_tool(db_path: str, tool_id: int) -> bool:
    conn = connect(db_path)
    try:
        cur = conn.execute('DELETE FROM custom_tools WHERE id=?',
                           (int(tool_id),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_custom_tool_enabled(db_path: str, tool_id: int, enabled: bool) -> bool:
    conn = connect(db_path)
    try:
        cur = conn.execute(
            'UPDATE custom_tools SET enabled=?, updated_at=? WHERE id=?',
            (1 if enabled else 0, time.time(), int(tool_id)))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def count_custom_tools(db_path: str, scope: str) -> int:
    conn = connect(db_path)
    try:
        return conn.execute(
            'SELECT COUNT(*) c FROM custom_tools WHERE scope=?',
            (str(scope),)).fetchone()['c']
    finally:
        conn.close()


# ── AI 初筛脚本（screen_scripts，v14）DAL ─────────────────────────

def _screen_row(r) -> dict:
    d = dict(r)
    d['enabled'] = bool(r['enabled'])
    d['global'] = d['scope'] == GLOBAL_CUSTOM_SCOPE
    return d


def upsert_screen_script(db_path: str, fields: dict) -> int:
    """按 (scope,name) 插入或整体覆盖（调用方须先在 worker 验证通过）。"""
    now = time.time()
    conn = connect(db_path)
    try:
        r = conn.execute(
            'SELECT id, hit_count FROM screen_scripts WHERE scope=? AND name=?',
            (fields['scope'], fields['name'])).fetchone()
        if r:
            conn.execute(
                """UPDATE screen_scripts SET title=?,description=?,code=?,
                       enabled=1,created_by=?,updated_at=? WHERE id=?""",
                (fields.get('title', ''), fields.get('description', ''),
                 fields['code'], fields.get('created_by', ''), now, r['id']))
            conn.commit()
            return int(r['id'])
        cur = conn.execute(
            """INSERT INTO screen_scripts(scope,name,title,description,code,
                   enabled,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,1,?,?,?)""",
            (fields['scope'], fields['name'], fields.get('title', ''),
             fields.get('description', ''), fields['code'],
             fields.get('created_by', ''), now, now))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_screen_scripts(db_path: str, chat_id: str = '',
                        enabled_only: bool = False) -> [dict]:
    """列初筛脚本：给 chat_id 时含本群 + 全局；不给则全表（管理用）。"""
    conn = connect(db_path)
    try:
        if chat_id:
            sql = 'SELECT * FROM screen_scripts WHERE scope IN (?, ?)'
            args = [str(chat_id), GLOBAL_CUSTOM_SCOPE]
            if enabled_only:
                sql += ' AND enabled=1'
            rows = conn.execute(
                sql + ' ORDER BY CASE scope WHEN ? THEN 0 ELSE 1 END, id',
                (*args, GLOBAL_CUSTOM_SCOPE)).fetchall()
        else:
            sql = 'SELECT * FROM screen_scripts'
            if enabled_only:
                sql += ' WHERE enabled=1'
            rows = conn.execute(sql + ' ORDER BY scope, id').fetchall()
        return [_screen_row(r) for r in rows]
    finally:
        conn.close()


def get_screen_script(db_path: str, script_id: int) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT * FROM screen_scripts WHERE id=?',
                         (int(script_id),)).fetchone()
        return _screen_row(r) if r else None
    finally:
        conn.close()


def delete_screen_script(db_path: str, script_id: int) -> bool:
    conn = connect(db_path)
    try:
        cur = conn.execute('DELETE FROM screen_scripts WHERE id=?',
                           (int(script_id),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_screen_script_enabled(db_path: str, script_id: int,
                              enabled: bool) -> bool:
    conn = connect(db_path)
    try:
        cur = conn.execute(
            'UPDATE screen_scripts SET enabled=?, updated_at=? WHERE id=?',
            (1 if enabled else 0, time.time(), int(script_id)))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def count_screen_scripts(db_path: str, scope: str) -> int:
    conn = connect(db_path)
    try:
        return conn.execute(
            'SELECT COUNT(*) c FROM screen_scripts WHERE scope=?',
            (str(scope),)).fetchone()['c']
    finally:
        conn.close()


def bump_screen_hit(db_path: str, script_id: int) -> None:
    """初筛命中一次：累计命中数 + 最近命中时间（判官是否放行无关）。"""
    conn = connect(db_path)
    try:
        conn.execute(
            'UPDATE screen_scripts SET hit_count=hit_count+1, last_hit_at=? '
            'WHERE id=?', (time.time(), int(script_id)))
        conn.commit()
    finally:
        conn.close()


# ── 后台运行记录（bg_runs）DAL ───────────────────────────────────

def bg_run_start(db_path: str, kind: str, target_type: str, target_id,
                 prompt: str, started_at: float | None = None,
                 poll_id: int = 0) -> int:
    """开一条后台运行记录，返回 id。kind: wake(定时苏醒)/scheduler(定时任务)。

    poll_id：由哪条轮询设定触发（0=通用苏醒/定时任务）。
    """
    started_at = time.time() if started_at is None else started_at
    conn = connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO bg_runs(kind,target_type,target_id,prompt,
                   poll_id,started_at) VALUES(?,?,?,?,?,?)""",
            (str(kind), str(target_type or 'group'), str(target_id or ''),
             str(prompt or '')[:4000], int(poll_id or 0), started_at))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _bg_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d['steps'] = json.loads(r['steps'] or '[]')
    except (ValueError, TypeError):
        d['steps'] = []
    return d


def bg_run_finish(db_path: str, run_id: int, status: str, final_text: str,
                  steps: list | None = None, error: str = '',
                  keep: int = 200) -> None:
    """收尾一条运行记录，并按 keep 裁剪最旧记录（0=不裁剪）。"""
    conn = connect(db_path)
    try:
        conn.execute(
            """UPDATE bg_runs SET status=?, final_text=?, steps=?, error=?,
                   finished_at=? WHERE id=?""",
            (str(status), str(final_text or '')[:8000],
             json.dumps(steps or [], ensure_ascii=False)[:200000],
             str(error or '')[:4000], time.time(), int(run_id)))
        if keep and keep > 0:
            conn.execute(
                """DELETE FROM bg_runs WHERE id NOT IN (
                       SELECT id FROM bg_runs ORDER BY id DESC LIMIT ?)""",
                (int(keep),))
        conn.commit()
    finally:
        conn.close()


def list_bg_runs(db_path: str, limit: int = 50, target_id: str = '') -> list[dict]:
    """后台运行列表（不含 steps 大字段），新的在前。"""
    conn = connect(db_path)
    try:
        sql = ('SELECT id,kind,target_type,target_id,prompt,status,final_text,'
               'error,started_at,finished_at,poll_id FROM bg_runs')
        args: list = []
        if target_id:
            sql += ' WHERE target_id=?'
            args.append(str(target_id))
        sql += ' ORDER BY id DESC LIMIT ?'
        args.append(max(1, min(int(limit), 200)))
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def get_bg_run(db_path: str, run_id: int) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT * FROM bg_runs WHERE id=?',
                         (int(run_id),)).fetchone()
        return _bg_row(r) if r else None
    finally:
        conn.close()

# ── 多条轮询设定（AI 自管；空列表的群走旧版全局概率配置）────────

def add_poll(db_path: str, fields: dict) -> int:
    now = time.time()
    conn = connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO polls(group_id,task,prob_percent,every_n,active_hours,
                   schedule_json,enabled,creator_id,creator_name,created_at,updated_at)
               VALUES(?,?,?,?,?,?,1,?,?,?,?)""",
            (str(fields['group_id']), str(fields['task']),
             int(fields.get('prob_percent', 10)),
             int(fields.get('every_n', 0)),
             float(fields.get('active_hours', 12)),
             str(fields.get('schedule', '') or '[]'),
             str(fields.get('creator_id', '')),
             str(fields.get('creator_name', '')), now, now))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def count_polls(db_path: str, group_id) -> int:
    conn = connect(db_path)
    try:
        return conn.execute(
            'SELECT COUNT(*) FROM polls WHERE group_id=?',
            (str(group_id),)).fetchone()[0]
    finally:
        conn.close()


def get_poll(db_path: str, poll_id: int) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT * FROM polls WHERE id=?',
                         (int(poll_id),)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def list_polls(db_path: str, group_id: str = '', enabled_only: bool = False) -> list:
    """轮询设定列表（id 正序）。group_id 非空只看该群；''=后台查全部群。"""
    sql = 'SELECT * FROM polls'
    where, args = [], []
    if group_id:
        where.append('group_id=?')
        args.append(str(group_id))
    if enabled_only:
        where.append('enabled=1')
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY group_id, id'
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def set_poll_enabled(db_path: str, poll_id: int, on: bool) -> None:
    conn = connect(db_path)
    try:
        conn.execute('UPDATE polls SET enabled=?, updated_at=? WHERE id=?',
                     (1 if on else 0, time.time(), int(poll_id)))
        conn.commit()
    finally:
        conn.close()


def delete_poll(db_path: str, poll_id: int) -> None:
    conn = connect(db_path)
    try:
        conn.execute('DELETE FROM polls WHERE id=?', (int(poll_id),))
        conn.commit()
    finally:
        conn.close()


def poll_record(db_path: str, poll_id: int, fired: bool, now_ts: float) -> None:
    """记录一次 tick：触发则清零保底计数并记 last_fire；未触发连续计数 +1。"""
    conn = connect(db_path)
    try:
        if fired:
            conn.execute(
                """UPDATE polls SET ticks_since_fire=0,last_tick_at=?,
                       last_fire_at=? WHERE id=?""",
                (float(now_ts), float(now_ts), int(poll_id)))
        else:
            conn.execute(
                """UPDATE polls SET ticks_since_fire=ticks_since_fire+1,
                       last_tick_at=? WHERE id=?""",
                (float(now_ts), int(poll_id)))
        conn.commit()
    finally:
        conn.close()

# ── 外部监测进程 watcher DAL ────────────────────────────────────

def add_watcher(db_path: str, fields: dict) -> int:
    conn = connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO watchers (group_id, name, script, args_json, pid,
                   status, creator_id, creator_name, log_path, started_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (str(fields['group_id']), str(fields.get('name', '')),
             str(fields['script']), str(fields.get('args_json', '[]')),
             int(fields.get('pid', 0)), str(fields.get('status', 'running')),
             str(fields.get('creator_id', '')),
             str(fields.get('creator_name', '')),
             str(fields.get('log_path', '')), float(fields['started_at'])))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _watcher_row(r):
    if r is None:
        return None
    d = dict(r)
    try:
        d['args'] = json.loads(d.pop('args_json', '[]') or '[]')
    except (ValueError, TypeError):
        d['args'] = []
    return d


def get_watcher(db_path: str, watcher_id: int):
    conn = connect(db_path)
    try:
        return _watcher_row(conn.execute(
            'SELECT * FROM watchers WHERE id=?', (int(watcher_id),)).fetchone())
    finally:
        conn.close()


def list_watchers(db_path: str, group_id: str = '',
                  status: str = '') -> list:
    sql, args = 'SELECT * FROM watchers', []
    where = []
    if group_id:
        where.append('group_id=?')
        args.append(str(group_id))
    if status:
        where.append('status=?')
        args.append(status)
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY id DESC'
    conn = connect(db_path)
    try:
        return [_watcher_row(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def count_watchers(db_path: str, group_id: str, status: str = 'running') -> int:
    conn = connect(db_path)
    try:
        return conn.execute(
            'SELECT COUNT(*) FROM watchers WHERE group_id=? AND status=?',
            (str(group_id), status)).fetchone()[0]
    finally:
        conn.close()


def set_watcher_status(db_path: str, watcher_id: int, status: str,
                       stopped_at: float = 0) -> None:
    conn = connect(db_path)
    try:
        conn.execute('UPDATE watchers SET status=?, stopped_at=? WHERE id=?',
                     (str(status), float(stopped_at or 0), int(watcher_id)))
        conn.commit()
    finally:
        conn.close()


# ── QQ 空间说说 DAL（发布留痕：NapCat 无列表接口，tid 只来自发布响应）────

def add_qzone_post(db_path: str, tid: str, content: str, ugc_right: int = 1,
                   creator_id: str = '', creator_name: str = '') -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            'INSERT OR IGNORE INTO qzone_posts (tid, content, ugc_right, '
            'creator_id, creator_name, created_at) VALUES (?,?,?,?,?,?)',
            (str(tid), str(content or '')[:5000], int(ugc_right or 1),
             str(creator_id or ''), str(creator_name or ''), time.time()))
        conn.commit()
    finally:
        conn.close()


def list_qzone_posts(db_path: str, limit: int = 10) -> list:
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute(
            'SELECT * FROM qzone_posts ORDER BY id DESC LIMIT ?',
            (int(limit),)).fetchall()]
    finally:
        conn.close()


# ── 系统提示词覆盖（v13）─────────────────────────────────────────

def list_prompt_override_rows(db_path: str) -> list:
    """所有提示词覆盖行（含 updated_at，Web 页用）。"""
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute(
            'SELECT * FROM prompt_overrides ORDER BY key').fetchall()]
    finally:
        conn.close()


def list_prompt_overrides(db_path: str) -> dict:
    """key → content 映射（PromptStore 缓存用）。"""
    return {r['key']: r['content']
            for r in list_prompt_override_rows(db_path)}


def set_prompt_override(db_path: str, key: str, content: str,
                        now: float | None = None) -> None:
    ts = now if now is not None else time.time()
    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT INTO prompt_overrides(key, content, updated_at)
               VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                   content=excluded.content, updated_at=excluded.updated_at""",
            (str(key), str(content), ts))
        conn.commit()
    finally:
        conn.close()


def delete_prompt_override(db_path: str, key: str) -> bool:
    """删掉覆盖（= 恢复默认）。返回是否真有一行被删。"""
    conn = connect(db_path)
    try:
        cur = conn.execute('DELETE FROM prompt_overrides WHERE key=?',
                           (str(key),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── 好友申请 / 群邀请暂存 DAL（v15）──────────────────────────────

def _pending_request_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d['decided_at'] = float(d['decided_at'] or 0)
    d['created_at'] = float(d['created_at'] or 0)
    return d


def upsert_pending_request(db_path: str, fields: dict) -> tuple:
    """flag 唯一去重落库。返回 (row, created)；已存在返回旧行与 False。"""
    now = time.time()
    conn = connect(db_path)
    try:
        cur = conn.execute(
            'INSERT OR IGNORE INTO pending_requests (flag, request_type, '
            'sub_type, user_id, group_id, comment, nickname, created_at) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (str(fields['flag']), str(fields['request_type']),
             str(fields.get('sub_type', '') or ''), str(fields['user_id']),
             str(fields.get('group_id', '') or ''),
             str(fields.get('comment', '') or ''),
             str(fields.get('nickname', '') or ''), now))
        conn.commit()
        created = cur.rowcount > 0
        row = conn.execute(
            'SELECT * FROM pending_requests WHERE flag=?',
            (str(fields['flag']),)).fetchone()
        return _pending_request_row(row), created
    finally:
        conn.close()


def get_pending_request(db_path: str, req_id: int) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT * FROM pending_requests WHERE id=?',
                         (int(req_id),)).fetchone()
        return _pending_request_row(r) if r else None
    finally:
        conn.close()


def list_pending_requests(db_path: str, status: str | None = None,
                          limit: int = 20) -> list:
    conn = connect(db_path)
    try:
        if status:
            rows = conn.execute(
                'SELECT * FROM pending_requests WHERE status=? '
                'ORDER BY id DESC LIMIT ?',
                (str(status), int(limit))).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM pending_requests ORDER BY id DESC LIMIT ?',
                (int(limit),)).fetchall()
        return [_pending_request_row(r) for r in rows]
    finally:
        conn.close()


def mark_pending_request(db_path: str, req_id: int, status: str,
                         decided_by: str = '', result: str = '') -> bool:
    conn = connect(db_path)
    try:
        cur = conn.execute(
            'UPDATE pending_requests SET status=?, decided_by=?, '
            'decided_at=?, result=? WHERE id=?',
            (str(status), str(decided_by or ''), time.time(),
             str(result or '')[:500], int(req_id)))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()

# ── loop 长跑模式 DAL ────────────────────────────────────────────

def _loop_request_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d['decided_at'] = float(d['decided_at'] or 0)
    d['created_at'] = float(d['created_at'] or 0)
    return d


def _loop_session_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d['created_at'] = float(d['created_at'] or 0)
    d['ended_at'] = float(d['ended_at'] or 0)
    d['rounds'] = int(d['rounds'] or 0)
    d['compacts'] = int(d['compacts'] or 0)
    d['request_id'] = int(d['request_id'] or 0)
    return d


def insert_loop_request(db_path: str, fields: dict) -> dict:
    now = time.time()
    conn = connect(db_path)
    try:
        cur = conn.execute(
            'INSERT INTO loop_requests (group_id, task, reason, requester_id, '
            'requester_name, status, created_at) VALUES (?,?,?,?,?,?,?)',
            (str(fields['group_id']), str(fields['task']),
             str(fields.get('reason', '') or ''),
             str(fields.get('requester_id', '') or ''),
             str(fields.get('requester_name', '') or ''),
             'pending', now))
        conn.commit()
        rid = cur.lastrowid
        r = conn.execute('SELECT * FROM loop_requests WHERE id=?',
                         (rid,)).fetchone()
        return _loop_request_row(r)
    finally:
        conn.close()


def get_loop_request(db_path: str, req_id: int) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT * FROM loop_requests WHERE id=?',
                         (int(req_id),)).fetchone()
        return _loop_request_row(r) if r else None
    finally:
        conn.close()


def list_loop_requests(db_path: str, status: str | None = None,
                       limit: int = 20) -> list:
    conn = connect(db_path)
    try:
        if status:
            rows = conn.execute(
                'SELECT * FROM loop_requests WHERE status=? '
                'ORDER BY id DESC LIMIT ?',
                (str(status), int(limit))).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM loop_requests ORDER BY id DESC LIMIT ?',
                (int(limit),)).fetchall()
        return [_loop_request_row(r) for r in rows]
    finally:
        conn.close()


def mark_loop_request(db_path: str, req_id: int, status: str,
                      decided_by: str = '') -> bool:
    conn = connect(db_path)
    try:
        cur = conn.execute(
            'UPDATE loop_requests SET status=?, decided_by=?, decided_at=? '
            'WHERE id=? AND status=?',
            (str(status), str(decided_by or ''), time.time(),
             int(req_id), 'pending'))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def insert_loop_session(db_path: str, group_id, task: str,
                        started_by: str = 'owner', request_id: int = 0) -> dict:
    now = time.time()
    conn = connect(db_path)
    try:
        cur = conn.execute(
            'INSERT INTO loop_sessions (group_id, task, status, started_by, '
            'request_id, created_at) VALUES (?,?,?,?,?,?)',
            (str(group_id), str(task), 'running', str(started_by),
             int(request_id or 0), now))
        conn.commit()
        r = conn.execute('SELECT * FROM loop_sessions WHERE id=?',
                         (cur.lastrowid,)).fetchone()
        return _loop_session_row(r)
    finally:
        conn.close()


def get_loop_session(db_path: str, session_id: int) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute('SELECT * FROM loop_sessions WHERE id=?',
                         (int(session_id),)).fetchone()
        return _loop_session_row(r) if r else None
    finally:
        conn.close()


def get_running_loop(db_path: str, group_id) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute(
            'SELECT * FROM loop_sessions WHERE group_id=? AND status=? '
            'ORDER BY id DESC LIMIT 1',
            (str(group_id), 'running')).fetchone()
        return _loop_session_row(r) if r else None
    finally:
        conn.close()


def update_loop_session(db_path: str, session_id: int, **fields) -> bool:
    """只允许更新 status/result/error/rounds/compacts/ended_at。"""
    allowed = {'status', 'result', 'error', 'rounds', 'compacts', 'ended_at'}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in {'result', 'error', 'status'}:
            v = str(v or '')
        sets.append(f'{k}=?')
        vals.append(v)
    if not sets:
        return False
    vals.append(int(session_id))
    conn = connect(db_path)
    try:
        cur = conn.execute(
            f'UPDATE loop_sessions SET {", ".join(sets)} WHERE id=?', vals)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def bump_loop_rounds(db_path: str, session_id: int, rounds: int) -> None:
    """worker 每轮回写累计工具轮数（状态查询用，失败静默）。"""
    try:
        update_loop_session(db_path, session_id, rounds=int(rounds))
    except Exception:
        pass


def interrupt_running_loops(db_path: str) -> int:
    """插件重启对账：内存 worker 已不存在，残留 running 全部置 interrupted。"""
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE loop_sessions SET status='interrupted', "
            "ended_at=?, error='插件重启，长跑中断' WHERE status='running'",
            (time.time(),))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
