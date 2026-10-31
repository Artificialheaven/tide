"""轻量数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ParsedMessage:
    """归一化后的入站群消息（只保留纯文本 + 判定标志，省 token）。"""
    text: str
    sender_id: str = ""
    sender_name: str = ""
    sender_card: str = ""
    at_me: bool = False
    reply_to_bot: bool = False
    reply_id: str = ""
    reply_to_id: str = ""                          # 被回复者 QQ（回复别人时）
    reply_to_name: str = ""                        # 被回复者昵称/名片（来自 reply 段，可能为空）
    reply_excerpt: str = ""                        # 被回复消息的纯文本摘要（截断）
    has_media: bool = False
    at_users: list = field(default_factory=list)   # 消息中 @ 到的 QQ 号（str）
    at_names: dict = field(default_factory=dict)   # qq → 段内自带名字（通常为空，待 users 表补全）
    # 媒体引用（只存段里的 file/url/name 等引用，触发通过后才下载）：
    # [{'kind': 'image|record|file|video', 'file': '', 'url': '', 'name': '', ...}]
    media: list = field(default_factory=list)
    chat_type: str = "group"                       # group | private
    message_id: str = ""                           # 本条消息 id（引用回复用；合成消息为空）
    event_time: int = 0                            # 消息时间戳（登记私聊文件用）
    allow_silent: bool = False                     # 定时苏醒轮：模型可只回 [静默] 不发群
    raw_id: int = 0                                # 本条在 raw_messages 的行 id（未记录=0）

    @property
    def forced(self) -> bool:
        return self.at_me or self.reply_to_bot

    @property
    def display_name(self) -> str:
        """对外展示名：群聊优先群名片（没设名片再退 QQ 昵称），私聊用 QQ 昵称。"""
        if self.chat_type == 'private':
            return self.sender_name
        return self.sender_card or self.sender_name


@dataclass
class UsageRow:
    group_id: str
    kind: str                  # chat / judge / summary / extract
    tokens_in: int = 0
    cache_hit: int = 0
    tokens_out: int = 0
    tool_rounds: int = 0
    ok: bool = True
