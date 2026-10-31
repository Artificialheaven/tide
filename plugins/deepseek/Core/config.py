"""默认配置、配置合并、群覆盖解析、人设三级解析。

所有纯逻辑，不 import openai/mcp，可直接单测。
配置热加载：插件 call_function("load_config") 时重新 merge_config。
"""
from __future__ import annotations

import re

# ── 多提供商模型路由 ──────────────────────────────────────────────
# 路由值统一写 "服务商id/模型id"（按第一个 '/' 切分，模型 id 自身可含 '/'）。
DEFAULT_PROVIDER_ID = "deepseek"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL_ID = "deepseek-flash"
_DEFAULT_ROUTE = f"{DEFAULT_PROVIDER_ID}/{DEFAULT_MODEL_ID}"
# 六个可独立选模型的路由：主对话 + 五个后台任务（全部全局；仅 model 可按群覆盖）
ROUTE_KEYS = ("model", "judge_model", "summary_model", "extract_model",
              "mood_model", "style_model")
ROUTE_LABELS = {
    "model": "对话模型",
    "judge_model": "判官模型",
    "summary_model": "摘要模型",
    "extract_model": "记忆抽取模型",
    "mood_model": "心情模型",
    "style_model": "风格模型",
}
_PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# ── 默认配置 ──────────────────────────────────────────────────────

DEFAULTS = {
    # 账号 / 模型
    "owner_qq": 3399752707,
    # api_key/base_url 仅作旧配置迁移输入：合并后的规范配置里它们落在
    # llm_providers 的 default 服务商内，顶层不再保留（密钥禁止写入源码）
    "api_key": "",
    "base_url": DEFAULT_BASE_URL,
    "llm_providers": [],                 # [{id,label,base_url,api_key,models:[{id,label}]}]；空=从旧字段合成 default
    "model": _DEFAULT_ROUTE,
    "judge_model": _DEFAULT_ROUTE,
    "summary_model": _DEFAULT_ROUTE,
    "extract_model": _DEFAULT_ROUTE,
    "mood_model": _DEFAULT_ROUTE,
    "style_model": _DEFAULT_ROUTE,
    "enable_thinking": False,            # 深度思考总开关（judge/summary/extract 恒关）
    "default_persona": "",               # 全局默认人设（后台 textarea）
    "command_prefix": "/ai",
    "aliases": "",                       # 全局称呼词，逗号分隔

    # 上下文 / 摘要
    "window_rounds": 12,
    "char_budget": 6000,
    "summary_char_budget": 1500,
    # 摘要重写节流：锚点之外的积压达到阈值且距上次摘要够久才重写，
    # 否则每轮都改 messages[1]，前缀缓存全部失效（输入未命中飙升的主因）。
    "summary_min_new_msgs": 12,          # 软阈值：窗口外积压条数
    "summary_min_new_chars": 2000,       # 软阈值：窗口外积压字符数（与条数取或）
    "summary_min_interval_sec": 600,     # 软阈值：距上次摘要的最短间隔
    "summary_force_msgs": 48,            # 硬阈值：积压超过就无视间隔立即摘要
    "summary_force_chars": 12000,        # 硬阈值：积压字符超过就立即摘要
    "ambient_context_on": True,           # 触发对话时把触发前几条未触发群聊作为背景注入
    "ambient_context_n": 8,                # 背景消息最多注入条数

    # 触发
    "heuristic_on": True,
    "judge_on": True,
    "judge_fail_open": False,            # 判官异常时是否放行（默认不放，省钱）
    "judge_history": 4,
    "join_greet_on": True,               # 机器人被拉进群时通知 AI 主动打招呼（入群侦查）

    # 生成
    "max_tokens": 30000,                 # 推理模型思考链也占输出配额；上限只影响可用额度，不按上限计费
    "max_tool_rounds": 50,               # 桌面远控类任务天然长串行（截图/点击/输入/等待）
    "request_timeout": 60,
    "tool_timeout": 30,
    "mcp_timeout": 30,

    # 沙箱
    "max_file_size_kb": 512,
    "workspace_quota_mb": 10,
    "tool_result_max_chars": 2000,
    "mcp_media_ttl_hours": 6,            # MCP 截图/录音在群工作区的存活时长；0=永久
    "mcp_media_max_kb": 15360,           # MCP 截图/录音落盘 + 本地媒体发送/读图的源文件上限（15MiB）
    "send_image_max_kb": 3072,           # qq_send_image 出站图片目标体积，超限自动转 JPEG 压缩
    "read_file_max_kb": 4096,            # fs_read 源文件字节上限（4MiB；512KiB 是写配额不是读上限）
    "read_file_max_chars": 30000,        # fs_read 办公文档抽取全文的字符预算
    "read_page_max_chars": 8000,         # fs_read 单次分页返回的硬上限

    "mcp_watchdog": False,

    # 代码执行（owner-only）
    "code_run_timeout": 30,               # code_run_python/shell 子进程超时秒数

    # 后台触发（定时苏醒）与前台分开的工具轮次
    "background_max_tool_rounds": 8,

    # 定时概率唤醒（防假死）
    "wake_on": True,                      # 总开关（每群可覆盖）
    "wake_check_minutes": 30,             # 轮询间隔
    "wake_prob_percent": 10,              # 每次 tick 的基础唤醒概率（百分数）
    "wake_every_n": 20,                   # 连续 N 次未触发必唤醒 1 次（0=关保底）
    "wake_active_hours": 12,              # 群里最近多久没真人说话就不唤醒（0=不限）
    "wake_schedule": "",                  # 分时段概率 JSON，如 [{"start":"23:00","end":"08:00","percent":2}]
    "polls_max_per_group": 10,            # 每群最多几条 AI 自建轮询设定

    # 主动搭话（叠加在 30 分钟 tick 上的掷骰层；与 wake 共用活跃门，每 tick 至多一次主动轮）
    "speakup_on": True,                   # 总开关（每群可覆盖）
    "speakup_peak_percent": 20,           # 高峰段（工作日 9-12/14-18）开口概率
    "speakup_valley_percent": 60,         # 低谷/夜间/周末开口概率（便宜时段更敢开口）
    "speakup_recent_minutes": 20,         # 刚有人说话 N 分钟内不黏人（概率乘子 0.15）
    "speakup_cold_hours": 6,             # 冷场超过 N 小时更易开口（乘子 1.5）
    "speakup_self_cooldown_minutes": 20,  # 机器人自己发言后 N 分钟内不再主动
    "speakup_miss_bonus": 10,             # 每连没中一次累加的概率
    "speakup_miss_cap": 40,              # 连没中累进上限
    "speakup_quiet": "01:00-08:00",       # 安静时段（本地时间，可跨午夜；空串=关）

    # 任务打断：/ai 打断 <指示> 插进正在执行的对话轮（全员可用）
    "interrupt_on": True,
    "interrupt_grace_rounds": 2,          # 收尾后才到的插队消息，同轮内最多再处理几波

    # 外部触发：监测程序 POST 回调 → 后台 AI 轮（watcher_start 仅主人）
    "trigger_on": True,                   # 触发 HTTP 服务总开关
    "trigger_host": "127.0.0.1",          # 默认仅本机；远程回调需自行改 0.0.0.0
    "trigger_port": 8094,
    "trigger_token": "",                  # 留空首次启动自动生成（只存 config.json）
    "trigger_cooldown_seconds": 30,       # 同一(群,source)两次回调最小间隔
    "trigger_text_max_chars": 2000,       # 单次事件文本上限
    "watchers_max_per_group": 5,          # 每群存活监测进程上限

    # 实时订阅 / 弹幕机 / 调试台（独立只读 SSE 服务，复用 trigger_token 鉴权）
    "live_on": True,                     # 总开关（改 host/port/开关需重连生效）
    "live_host": "127.0.0.1",            # 默认仅本机；给局域网/OBS 机用才改 0.0.0.0
    "live_port": 8095,

    # AI 自定义工具
    "custom_tools_max_per_scope": 20,
    "custom_tool_code_max_chars": 8000,
    "custom_tool_call_timeout": 20,       # 调用已上线自定义工具的超时（超时杀 worker）
    "custom_validate_timeout": 10,        # dry-run 试跑超时
    "bg_runs_keep": 200,                  # 后台运行记录保留条数（定时苏醒/定时任务）

    # AI 初筛脚本（多判官机制第一层：启发式 OR 任一脚本放行才调 LLM 判官）
    "screen_on": True,                    # 总开关（每群可覆盖）
    "screen_max_per_scope": 10,           # 每群/全局脚本数上限
    "screen_code_max_chars": 8000,
    "screen_call_timeout": 5,             # 每条群消息跑脚本的单脚本超时（必须便宜）
    "screen_validate_timeout": 10,        # dry-run 试跑超时

    # 触发与回复外观（每群可覆盖）
    "at_only_on": False,                 # 仅@触发：群里只响应 @ 她/回复她/叫她名字的消息
    "quote_reply_on": False,             # 引用回复：本轮第一条文本消息引用触发消息（图片/语音不占用）

    # 分条发送
    "split_reply": True,
    "real_at_on": True,                  # 回复里的 @昵称 发送时转真 CQ:at
    "split_max_len": 100,
    "split_min_len": 6,
    "split_delay_min_ms": 600,
    "split_delay_max_ms": 1800,

    # per-user 记忆
    "memory_on": True,
    "memory_extract_every": 5,           # 每群每 N 轮回复抽一次；0=关自动抽取
    "memory_recall_topk": 6,
    "memory_recall_budget": 400,
    "memory_extract_max_msgs": 40,

    # 她自己的第一人称记忆（全局一份，跨所有群/私聊共享）
    "self_memory_on": True,              # 总开关（每群可覆盖）；关后工具摘除、不抽取不召回
    "self_memory_daily_quota": 8,        # 每天新建条目上限（就地改写不耗配额）
    "self_memory_recall_topk": 5,
    "self_memory_recall_budget": 300,
    "self_memory_trace_on": True,        # 回忆链：每次召回/改写留只追加足迹（Web 记忆页主人只读）

    # 好友申请 / 群邀请：只暂存 + 私聊通知主人，由主人对话拍板（全局项，不按群覆盖）
    "request_notify_on": True,

    # loop 长跑模式：主人授权后无工具轮上限，直到 AI 调 loop.finish 或主人叫停
    "loop_on": True,                     # 总开关（每群可覆盖）
    "loop_every_rounds": 5,              # 连续 N 个工具轮没在群里说话，就强制汇报一次进展
    "loop_compact_threshold_chars": 40000,  # 兜底：单轮上下文估算超过该字符数就自动压缩
    "loop_context_limit_tokens": 0,      # 模型上下文总 token 数；0=自动（从超长报错学习，学到前按字符阈值）
    "loop_compact_percent": 80,          # 真实 prompt token 用到输入预算的百分比就压缩（余量留给输出）
    "loop_keep_rounds": 4,               # 压缩时保留最近几个工具轮的完整消息
    "loop_idle_turn_limit": 3,           # 连续 N 个续跑轮没调任何工具且未收尾，判定空转自动结束

    # 心情（每群一份；私聊以对方 QQ 为群号各自独立）
    "mood_on": True,                     # 总开关（每群可覆盖）
    "mood_baseline": 70,                 # 心情基线，波动按半衰期向它回落
    "mood_delta_threshold": 5,           # |delta| 低于此值只记账、不注入
    "mood_min_confidence": 60,           # 判定信心低于此值不进她的嘴
    "mood_daily_cap": 6,                 # 每群每天最多注入几次心情背景
    "mood_timeline_keep": 200,           # 每群心情时间线保留条数

    # 群体说话风格学习
    "learn_style_on": True,              # 已开启群全量落库群聊语料（0 token）并周期学习风格
    "style_rebuild_every": 200,          # 每攒 N 条新群语料重建一次风格画像
    "style_sample_msgs": 300,            # 单次学习采样上限
    "raw_corpus_cap": 3000,              # 每群滚动保留的原始群聊条数

    # 约定/待办 + 主动提醒
    "reminder_on": True,                 # 到点主动在群里 @ 提醒
    "reminder_check_seconds": 60,

    # AI 定时任务（到点模型按 brief 现写内容发群/私聊）
    "scheduler_on": True,
    "scheduler_check_seconds": 30,

    # 入站多模态：用户发的图片/文件/语音
    "vision_on": True,                   # 图片直接作为视觉输入送给多模态模型
    "vision_max_images": 4,              # 单条消息最多送几张图
    "media_download_max_kb": 10240,      # 图片/附件下载总上限（10MiB）
    "read_image_max_kb": 2048,           # read_image 回看工作区截图的单图上限
    "file_parse_on": True,               # txt/md/代码/pdf/docx 等附件解析为文本
    "file_text_max_chars": 4000,         # 单个附件注入正文的字符上限
    "voice_asr_on": False,               # 语音转文字（需兼容 OpenAI 音频接口的 ASR 服务 + ffmpeg）
    "voice_asr_model": "",               # 如 whisper-1 / 服务端支持的 ASR 模型名
    "inbound_video_on": True,            # 群友发的视频存进工作区，附带路径供 read_video
    "inbound_video_max_kb": 51200,       # 入站视频单条下载上限（50MiB，独立于文档预算）

    # 截图坐标辅助
    "vision_grid_on": True,              # read_image 对截图默认叠加坐标网格
    "vision_grid_step": 100,             # 网格间距（原图像素）

    # PDF/Word 渲染成图送视觉
    "doc_render_on": True,               # 扫描版 PDF 等自动渲染成图；view_document 总可用
    "doc_render_max_pages": 3,           # 单次/单附件渲染页数上限
    "doc_render_dpi": 130,               # 页面栅格化 DPI
    "doc_render_max_kb": 20480,          # 被渲染文档的源文件大小上限（KiB）

    # read_video：视频/GIF 拆成宫格图，交给视觉子代理读，只把文字总结回主模型
    "read_video_on": True,               # 总开关（每群可覆盖）；关后工具摘除
    "read_video_model": "",              # 视觉子代理模型（留空沿用对话模型，必须支持看图）
    "read_video_timeout": 180,           # 单次 read_video 总超时（秒）：解码 + 子代理
    "read_video_max_minutes": 15,        # 单次最多读多少分钟（窗口从 from 开始）
    "read_video_max_mb": 512,            # 源文件大小上限（MiB）
    "read_video_cells": 0,               # 固定格数 4-16；0=按画面运动自动
    "read_video_scan_fps": 4,            # 低分辨率扫描帧率（切点/密度检测）
    "read_video_asr_on": False,          # 顺带转写台词贴到格子上（需 voice_asr_model）
    "read_video_asr_max_chunks": 24,     # 单次最多转写多少个 ≤40s 音频块
    "read_video_sheet_max_kb": 1800,     # 宫格图上限（超出转 JPEG）
    "read_video_keep_cache": 12,         # 保留最近多少张宫格图

    # 私聊 + QQ 文件工具
    "private_on": True,                  # 是否在私聊里对话（默认只接主人和白名单）
    "private_allowed_qqs": "",           # 允许私聊的 QQ（逗号分隔）；主人恒允许
    "qq_file_max_kb": 20480,             # qq_download_file 单文件下载上限（KiB）

    # 权限
    "risky_owner_only": True,            # 写删文件/MCP 远程桌面等风险操作只听主人

    # public 公共素材/技能库（跨群共享，写删仍走主人风险门禁）
    "public_writable_on": True,          # public/ 是否允许写入（关闭=只读）
    "public_max_file_kb": 4096,          # public 单文件上限（KiB，图片素材比文本大）
    "public_quota_mb": 50,               # public 总配额（MiB，全群共享一份）

    # AI 自我提示词注入表
    "inject_on": True,                   # 每轮把启用条目注入系统提示词 + 提供管理工具
    "inject_max_chars": 1500,            # 每轮注入块总字符预算
    "inject_max_per_scope": 30,          # 每个群/全局的条目数上限

    # 梗指南（独立 meme.db：中文名/别名/拼音容错匹配，命中自动注入 + meme_lookup 工具）
    "meme_guide_on": True,               # 总开关（每群可覆盖）；关后不注入、工具摘除
    "meme_inject_max": 2,                # 单轮自动注入梗条数上限（多候选歧义时）
    "meme_entry_max_chars": 600,         # 单条梗释义注入字符上限
}

# 群 settings_json 允许覆盖的键白名单
GROUP_OVERRIDABLE = frozenset({
    "model", "enable_thinking",
    "heuristic_on", "judge_on",
    "window_rounds", "char_budget",
    "summary_min_new_msgs", "summary_min_new_chars",
    "summary_min_interval_sec",
    "ambient_context_on", "ambient_context_n",
    "split_reply", "split_max_len", "split_min_len",
    "split_delay_min_ms", "split_delay_max_ms",
    "memory_on", "learn_style_on", "reminder_on",
    "scheduler_on", "vision_on", "file_parse_on", "voice_asr_on",
    "inbound_video_on", "inbound_video_max_kb",
    "doc_render_on", "wake_on", "interrupt_on",
    "mood_on", "self_memory_on", "speakup_on",
    "screen_on", "self_memory_trace_on",
    "loop_on", "loop_every_rounds",
    "meme_guide_on", "read_video_on",
    "at_only_on", "quote_reply_on",
})

BUILTIN_PERSONA = "你是一个QQ群聊助手，说话简短自然，用中文交流。"

_INT_KEYS = {
    "owner_qq", "window_rounds", "char_budget", "summary_char_budget",
    "summary_min_new_msgs", "summary_min_new_chars",
    "summary_min_interval_sec", "summary_force_msgs", "summary_force_chars",
    "ambient_context_n",
    "judge_history", "max_tokens", "max_tool_rounds", "request_timeout",
    "tool_timeout", "mcp_timeout", "mcp_media_ttl_hours",
    "mcp_media_max_kb", "send_image_max_kb",
    "read_file_max_kb", "read_file_max_chars", "read_page_max_chars",
    "max_file_size_kb", "workspace_quota_mb",
    "tool_result_max_chars", "split_max_len", "split_min_len",
    "code_run_timeout", "background_max_tool_rounds",
    "wake_check_minutes", "wake_prob_percent", "wake_every_n",
    "wake_active_hours", "polls_max_per_group",
    "custom_tools_max_per_scope", "custom_tool_code_max_chars",
    "custom_tool_call_timeout", "custom_validate_timeout", "bg_runs_keep",
    "screen_max_per_scope", "screen_code_max_chars",
    "screen_call_timeout", "screen_validate_timeout",
    "interrupt_grace_rounds",
    "trigger_port", "trigger_cooldown_seconds", "trigger_text_max_chars",
    "watchers_max_per_group", "live_port",
    "split_delay_min_ms", "split_delay_max_ms", "memory_extract_every",
    "memory_recall_topk", "memory_recall_budget", "memory_extract_max_msgs",
    "self_memory_daily_quota", "self_memory_recall_topk",
    "self_memory_recall_budget",
    "mood_baseline", "mood_delta_threshold", "mood_min_confidence",
    "mood_daily_cap", "mood_timeline_keep",
    "speakup_peak_percent", "speakup_valley_percent",
    "speakup_recent_minutes", "speakup_cold_hours",
    "speakup_self_cooldown_minutes", "speakup_miss_bonus",
    "speakup_miss_cap",
    "style_rebuild_every", "style_sample_msgs", "raw_corpus_cap",
    "reminder_check_seconds",
    "scheduler_check_seconds", "read_image_max_kb", "media_download_max_kb",
    "vision_max_images", "file_text_max_chars", "inbound_video_max_kb",
    "vision_grid_step",
    "doc_render_max_pages", "doc_render_dpi", "doc_render_max_kb",
    "read_video_timeout", "read_video_max_minutes", "read_video_max_mb",
    "read_video_cells", "read_video_scan_fps", "read_video_asr_max_chunks",
    "read_video_sheet_max_kb", "read_video_keep_cache",
    "qq_file_max_kb",
    "public_max_file_kb", "public_quota_mb",
    "inject_max_chars", "inject_max_per_scope",
    "meme_inject_max", "meme_entry_max_chars",
    "loop_every_rounds", "loop_compact_threshold_chars",
    "loop_context_limit_tokens", "loop_compact_percent",
    "loop_keep_rounds", "loop_idle_turn_limit",
}
_BOOL_KEYS = {
    "enable_thinking", "heuristic_on", "judge_on", "judge_fail_open",
    "mcp_watchdog", "split_reply", "real_at_on", "memory_on", "learn_style_on",
    "reminder_on", "scheduler_on", "vision_on", "file_parse_on",
    "voice_asr_on", "inbound_video_on",
    "vision_grid_on", "doc_render_on", "private_on",
    "risky_owner_only", "public_writable_on", "inject_on",
    "meme_guide_on", "read_video_on", "read_video_asr_on",
    "join_greet_on", "wake_on", "interrupt_on", "trigger_on", "live_on",
    "ambient_context_on", "mood_on", "self_memory_on", "speakup_on",
    "screen_on", "self_memory_trace_on", "request_notify_on",
    "loop_on", "at_only_on", "quote_reply_on",
}
def _as_int(v, default):
    try:
        if isinstance(v, bool):
            return int(v)
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _as_bool(v, default):
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on", "开", "是"):
        return True
    if s in ("0", "false", "no", "off", "关", "否", ""):
        return False
    return default


# ── 多提供商解析（纯函数，可直接单测）────────────────────────────

def slugify_provider_id(value, used: set) -> str:
    """任意输入 → 合法服务商 id（小写字母数字-_，1-32 位）；与 used 去重。"""
    s = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-_")
    if not s or not _PROVIDER_ID_RE.match(s):
        s = "provider"
    s = s[:32].rstrip("-_") or "provider"
    pid, i = s, 2
    while pid in used:
        suf = f"-{i}"
        pid = (s[:32 - len(suf)].rstrip("-_") or "p") + suf
        i += 1
    used.add(pid)
    return pid


def normalize_model_entry(m) -> dict | None:
    """模型条目 → {'id','label'}；dict/裸字符串都收，空值返回 None。"""
    if isinstance(m, dict):
        mid = str(m.get("id") or "").strip()
        label = str(m.get("label") or "").strip()
    else:
        mid, label = str(m or "").strip(), ""
    if not mid:
        return None
    return {"id": mid, "label": label}


def synthesize_legacy_provider(raw: dict | None) -> dict:
    """没有 llm_providers 时：从旧 api_key/base_url/model/judge_model 造 default。"""
    raw = raw or {}
    ids = []
    for k in ("model", "judge_model"):
        v = str(raw.get(k) or "").strip()
        if "/" in v:                       # 兼容已经限定化的值
            v = v.split("/", 1)[1]
        if v:
            ids.append(v)
    ids.append(DEFAULT_MODEL_ID)
    models, seen = [], set()
    for mid in ids:
        if mid not in seen:
            seen.add(mid)
            models.append({"id": mid, "label": ""})
    return {
        "id": DEFAULT_PROVIDER_ID,
        "label": "默认（旧配置迁移）",
        "base_url": str(raw.get("base_url") or "").strip() or DEFAULT_BASE_URL,
        "api_key": str(raw.get("api_key") or ""),
        "models": models,
    }


def normalize_providers(providers, *, legacy: dict | None = None) -> list:
    """清洗服务商列表；空/全废时回退到从旧配置合成 default。"""
    if not isinstance(providers, list) or not providers:
        return [synthesize_legacy_provider(legacy)]
    out, used = [], set()
    for e in providers:
        if not isinstance(e, dict):
            continue
        # Web 新建卡片允许只填显示名：id 留空时从显示名生成
        pid = slugify_provider_id(e.get("id") or e.get("label"), used)
        models, seen = [], set()
        for m in (e.get("models") if isinstance(e.get("models"), list) else []):
            ent = normalize_model_entry(m)
            if ent and ent["id"] not in seen:
                seen.add(ent["id"])
                models.append(ent)
        if not models:
            continue
        out.append({
            "id": pid,
            "label": str(e.get("label") or "").strip(),
            "base_url": str(e.get("base_url") or "").strip() or DEFAULT_BASE_URL,
            "api_key": str(e.get("api_key") or ""),
            "models": models,
        })
    return out or [synthesize_legacy_provider(legacy)]


def split_route(value) -> tuple[str, str]:
    """'pid/mid' → ('pid','mid')；裸 'mid' → ('', 'mid')。非法抛 ValueError。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("空路由值")
    value = value.strip()
    if "/" not in value:
        return "", value
    pid, mid = value.split("/", 1)
    if not pid or not mid:
        raise ValueError(f"路由值非法：{value!r}")
    return pid, mid


def get_provider(cfg: dict, pid: str) -> dict | None:
    for p in cfg.get("llm_providers") or []:
        if isinstance(p, dict) and p.get("id") == pid:
            return p
    return None


def _model_ids(provider: dict) -> set:
    return {str(m.get("id")) for m in provider.get("models", [])
            if isinstance(m, dict) and m.get("id") is not None}


def resolve_model_value(cfg: dict, value) -> tuple[dict, str]:
    """路由值 → (provider dict, 模型id)。裸 id 只在 default 服务商声明过才合法。"""
    pid, mid = split_route(value)
    if not pid:
        p = get_provider(cfg, DEFAULT_PROVIDER_ID)
        if p is not None and mid in _model_ids(p):
            return p, mid
        raise ValueError(f"裸模型名 {mid!r} 不在 default 服务商的模型列表里")
    p = get_provider(cfg, pid)
    if p is None:
        raise ValueError(f"服务商不存在：{pid}")
    if mid not in _model_ids(p):
        raise ValueError(f"服务商 {pid} 没有模型 {mid}")
    return p, mid


def resolve_route(cfg: dict, route_key: str = "model") -> tuple[dict, str]:
    return resolve_model_value(cfg, cfg.get(route_key))


def is_valid_route_value(cfg: dict, value) -> bool:
    try:
        resolve_model_value(cfg, value)
        return True
    except (ValueError, TypeError):
        return False


def _qualified(value) -> str:
    pid, mid = split_route(value)
    return f"{pid or DEFAULT_PROVIDER_ID}/{mid}"


def route_label(cfg: dict, route_key: str) -> str:
    """状态文案用：服务商显示名 / 模型显示名（或 id）；解析失败回退原值。"""
    v = cfg.get(route_key, "")
    try:
        p, mid = resolve_route(cfg, route_key)
    except (ValueError, TypeError):
        return str(v or "")
    ml = next((str(m.get("label") or "") for m in p.get("models", [])
               if isinstance(m, dict) and m.get("id") == mid), "")
    return f'{p.get("label") or p["id"]} / {ml or mid}'


def find_dangling_routes(cfg: dict, group_rows=None) -> list:
    """找出引用了不存在服务商/模型的全局路由与各群 model 覆盖（删服务商前校验）。"""
    bad = []
    for k in ROUTE_KEYS:
        v = cfg.get(k)
        if isinstance(v, str) and v.strip() and not is_valid_route_value(cfg, v):
            bad.append((ROUTE_LABELS.get(k, k), k, v))
    for row in (group_rows or []):
        if not isinstance(row, dict):
            continue
        st = row.get("settings") or {}
        v = st.get("model")
        if isinstance(v, str) and v.strip() and not is_valid_route_value(cfg, v):
            bad.append((f"群 {row.get('group_id')} 模型", "model", v))
    return bad


def merge_config(raw: dict | None) -> dict:
    """用户 config.json 与 DEFAULTS 合并，做类型修正；未知键保留但不校验。"""
    raw = raw or {}
    cfg = dict(DEFAULTS)
    for k, v in raw.items():
        if k in _INT_KEYS:
            cfg[k] = _as_int(v, DEFAULTS[k])
        elif k in _BOOL_KEYS:
            cfg[k] = _as_bool(v, DEFAULTS[k])
        else:
            cfg[k] = v
    # 多提供商：先规范化服务商，再校验六个路由（非法重置；裸 id 限定化）
    providers = normalize_providers(raw.get("llm_providers"), legacy=raw)
    cfg["llm_providers"] = providers
    # 默认路由不在任何服务商里时（如用户只配了 GLM），退到第一个服务商的第一个模型
    fallback_route = DEFAULTS["model"]
    if not is_valid_route_value(cfg, fallback_route):
        p0 = providers[0]
        fallback_route = f'{p0["id"]}/{p0["models"][0]["id"]}'
    for k in ROUTE_KEYS:
        v = cfg.get(k)
        if isinstance(v, str) and is_valid_route_value(cfg, v):
            cfg[k] = _qualified(v)
        else:
            cfg[k] = fallback_route
    # 规范配置里密钥/base_url 只存在于服务商条目内
    cfg.pop("api_key", None)
    cfg.pop("base_url", None)
    cfg["owner_qq"] = _as_int(cfg["owner_qq"], DEFAULTS["owner_qq"])
    return cfg


def resolve_group(cfg: dict, group_settings: dict | None) -> dict:
    """把群级覆盖叠到全局配置上，返回新 dict。"""
    eff = dict(cfg)
    if group_settings:
        for k, v in group_settings.items():
            if k not in GROUP_OVERRIDABLE or v is None:
                continue
            if k in _INT_KEYS:
                eff[k] = _as_int(v, cfg[k])
            elif k in _BOOL_KEYS:
                eff[k] = _as_bool(v, cfg[k])
            elif k == "model":
                # qualified 或 default 服务商声明过的裸 id 才生效，否则回退全局
                if isinstance(v, str) and is_valid_route_value(eff, v):
                    eff[k] = _qualified(v)
            else:
                eff[k] = v
    return eff


def resolve_persona(cfg: dict, group_persona: str | None) -> str:
    """群 persona → 全局 default_persona → 内置精简人设。"""
    p = (group_persona or "").strip() or (cfg.get("default_persona") or "").strip()
    return p or BUILTIN_PERSONA


def parse_aliases(value) -> list[str]:
    """配置里的逗号分隔称呼词 → 去空去重列表。"""
    if isinstance(value, list):
        items = value
    else:
        items = str(value or "").replace("，", ",").replace("、", ",").split(",")
    out, seen = [], set()
    for it in items:
        w = str(it).strip()
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out
