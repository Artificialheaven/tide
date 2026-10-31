"""10 个内置工具：QQ 三件套 + 文件五件套 + 记忆两件套。

schema 顺序固定（缓存友好）；dispatch 全部 async，文件操作走 to_thread。
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import re
import time
from dataclasses import dataclass, field

from . import context as ctx_mod, history, imgmark, media_in, media_out
from . import message as message_mod, storage, video_read
from .sandbox import SandboxError

# read_message_image 的 file 白名单（NapCat 哈希名/带括号的老式文件名）
_SAFE_IMG_REF = re.compile(r'^[A-Za-z0-9._(){}\-]{1,160}$')
# read_forward 的 id 白名单（hex/resId，可能含 _ = . -）
_SAFE_FORWARD_ID = re.compile(r'^[A-Za-z0-9_.=\-]{1,200}$')

# 工具名 → 群勾选 key
TOOL_KEYS = {
    'qq_send_text': 'qq.text',
    'qq_send_image': 'qq.image',
    'qq_send_voice': 'qq.voice',
    'qq_at_all': 'qq.at_all',
    'qq_member_info': 'qq.member_info',
    'qq_list_files': 'qq.file_list',
    'qq_download_file': 'qq.file_fetch',
    'fs_list': 'fs.list',
    'fs_read': 'fs.read',
    'fs_write': 'fs.write',
    'fs_append': 'fs.append',
    'fs_delete': 'fs.delete',
    'fs_publish': 'fs.publish',
    'inject_add': 'inject.add',
    'inject_list': 'inject.list',
    'inject_toggle': 'inject.toggle',
    'inject_remove': 'inject.remove',
    'recall_user': 'memory.recall',
    'remember_user': 'memory.remember',
    'recall_self': 'memory.recall_self',
    'remember_self': 'memory.remember_self',
    'task_add': 'task.add',
    'task_list': 'task.list',
    'task_close': 'task.close',
    'schedule_add': 'schedule.add',
    'schedule_list': 'schedule.list',
    'schedule_cancel': 'schedule.cancel',
    'poll_add': 'poll.add',
    'poll_list': 'poll.list',
    'poll_toggle': 'poll.toggle',
    'poll_remove': 'poll.remove',
    'watcher_start': 'watcher.start',
    'watcher_list': 'watcher.list',
    'watcher_stop': 'watcher.stop',
    'watcher_logs': 'watcher.logs',
    'read_image': 'fs.read_image',
    'read_video': 'fs.read_video',
    'click_at': 'ui.click_at',
    'view_document': 'fs.doc_view',
    'now_time': 'time.now',
    'code_run_python': 'code.python',
    'code_run_shell': 'code.shell',
    'custom_tool_validate': 'custom.validate',
    'custom_tool_publish': 'custom.publish',
    'custom_tool_list': 'custom.list',
    'custom_tool_toggle': 'custom.toggle',
    'custom_tool_remove': 'custom.remove',
    'screen_validate': 'screen.validate',
    'screen_publish': 'screen.publish',
    'screen_list': 'screen.list',
    'screen_toggle': 'screen.toggle',
    'screen_remove': 'screen.remove',
    'request_list': 'request.list',
    'request_approve': 'request.approve',
    'request_reject': 'request.reject',
    'loop_request': 'loop.request',
    'loop_finish': 'loop.finish',
    'history_query': 'history.query',
    'token_stats': 'usage.stats',
    'qzone_publish': 'qzone.publish',
    'qzone_delete': 'qzone.delete',
    'qzone_recent': 'qzone.recent',
    'read_message_image': 'message.read_image',
    'read_forward': 'message.read_forward',
    'meme_lookup': 'meme.lookup',
}

_SCHEMAS = [
    {
        'type': 'function',
        'function': {
            'name': 'qq_send_text',
            'description': '立即向当前群发送一条文本消息（用于需要中途插话时；正常回复不要调用）',
            'parameters': {
                'type': 'object',
                'properties': {'text': {'type': 'string', 'description': '消息文本'}},
                'required': ['text'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'qq_send_image',
            'description': '向当前群发送图片。source 为 http(s) 链接或本群工作区内的相对路径',
            'parameters': {
                'type': 'object',
                'properties': {'source': {'type': 'string'}},
                'required': ['source'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'qq_send_voice',
            'description': '向当前群发送语音。source 为 http(s) 链接或工作区内的音频相对路径',
            'parameters': {
                'type': 'object',
                'properties': {'source': {'type': 'string'}},
                'required': ['source'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'qq_at_all',
            'description': (
                '@全体成员（QQ 每天次数有限；你自己在本群必须是管理员或群主，'
                '且只有主人能指令；正常聊天、没有正经通知需求时不要用）。'
                '附带的文本会跟在 @全体成员 后面同一条发出。'),
            'parameters': {
                'type': 'object',
                'properties': {'text': {'type': 'string',
                                       'description': '通知正文（可留空，只 @ 一下）'}},
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'qq_member_info',
            'description': (
                '查某个群成员的身份：是不是群主/管理员、群等级、群头衔、群名片等。'
                'user_id 填对方 QQ 号；不填则返回你自己在本群的身份（判断能否 @全体成员 等）。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'user_id': {'type': 'integer',
                                'description': '要查的 QQ 号；不填查自己'},
                    'no_cache': {'type': 'boolean',
                                 'description': 'true=强制刷新（刚变更时用），默认 false'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'qq_list_files',
            'description': (
                '查询 QQ 群文件目录，或当前私聊里机器人登记过的文件清单。'
                '群里实时列 QQ 服务器上的群文件（根目录或某个文件夹内）；'
                '私聊返回本地登记的清单（OneBot 协议无法列举历史私聊文件，'
                '对方把文件发到私聊后自动登记）。'
                '用户提到"群文件里的X、之前发过的X"而工作区里没有时先列目录，'
                '再按文件名和日期自己判断要下载哪个，不要问用户。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'folder_id': {'type': 'string',
                                  'description': '群文件文件夹 ID（结果里 D 开头条目的 folder_id）；留空=根目录。私聊忽略'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'qq_download_file',
            'description': (
                '从群文件目录或私聊清单里下载一个文件并读取内容（纯文本/PDF/Word/'
                'Excel 抽文字，扫描版 PDF/老 .doc 自动渲染成图给你看）。'
                'file_id、busid 严格照抄 qq_list_files 结果；name 也照抄文件名'
                '（决定怎么解析）。用户消息里 [文件 name="…" file_id=… busid=…] '
                '占位符带的参数同样可直接照抄。下载量受配置上限约束（默认 20MiB）。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'file_id': {'type': 'string',
                                'description': 'qq_list_files 结果里的 file_id'},
                    'busid': {'type': 'integer',
                              'description': '群文件结果里的 busid；私聊文件没有就填 0'},
                    'name': {'type': 'string',
                             'description': '文件名（含扩展名），照抄列表里的名字'},
                },
                'required': ['file_id', 'name'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'fs_list',
            'description': '列出本群工作区目录下的文件；public/ 为跨群共享的公共素材/技能库（主人可写）',
            'parameters': {
                'type': 'object',
                'properties': {'path': {'type': 'string', 'description': '相对路径，默认根目录'}},
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'fs_read',
            'description': (
                '读取本群工作区中的文本文件（含 pdf/docx/xlsx 自动抽文本）。'
                '结果按字符分页：每次最多返回 limit 字符，开头声明全文长度和本次区间，'
                '没读完时结尾给出下一次的 offset——必须带着 offset 继续读，'
                '不要假设剩余内容。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': '工作区相对路径'},
                    'offset': {
                        'type': 'integer',
                        'description': '起始字符偏移（从 0 开始；上次返回结尾提示里的数字）',
                    },
                    'limit': {
                        'type': 'integer',
                        'description': '本次最多读取字符数，默认 2000，最大 8000',
                    },
                },
                'required': ['path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'fs_write',
            'description': '写入/覆盖文本文件；默认在本群工作区，以 public/ 开头则写入跨群公共素材库（风险操作，仅主人可指令）',
            'parameters': {
                'type': 'object',
                'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}},
                'required': ['path', 'content'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'fs_append',
            'description': '向文本文件追加内容；以 public/ 开头则写入跨群公共素材库（风险操作，仅主人可指令）',
            'parameters': {
                'type': 'object',
                'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}},
                'required': ['path', 'content'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'fs_delete',
            'description': '删除本群工作区或 public 公共库中的文件/目录（风险操作，仅主人可指令；禁止删根目录）',
            'parameters': {
                'type': 'object',
                'properties': {'path': {'type': 'string'}},
                'required': ['path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'fs_publish',
            'description': (
                '把本群工作区里的文件（如 MCP 截图、下载的图片素材、整理好的资料）'
                '复制沉淀到跨群共享的 public/ 公共素材库，以后任何群都能读取，长期保留'
                '（不受本群配额和 MCP 媒体清理影响）。'
                'source 是本群工作区相对路径，dest 必须以 public/ 开头，'
                '如 public/memes/猫.jpg、public/skills/电脑操作/笔记.md。'
                '只沉淀确实有长期复用价值的东西，临时文件不要放。'
                '沉淀后用 inject_add 写一条注入提醒自己资源在哪、什么场景用。'
                '风险操作，仅主人可指令。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'source': {'type': 'string',
                               'description': '本群工作区内的源文件相对路径，如 mcp/Snapshot_xxx.jpg'},
                    'dest': {'type': 'string',
                             'description': 'public/ 下的目标相对路径，含文件名，如 public/memes/猫.jpg'},
                },
                'required': ['source', 'dest'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'inject_add',
            'description': (
                '往你自己的「提示词注入表」里加一条自我提醒，之后每次对话系统都会自动'
                '注入它。典型用法：你在 public/ 沉淀了素材、或学会了一套可复用的技能'
                '（如某软件的操作流程、某类问题的处理套路、收藏的梗图位置）后，'
                '写一条简短提醒说明【有什么资源/技能、放在哪、什么场景该用】。'
                'text 用第二人称对自己下指令，具体可执行，不要写空话。'
                '默认只在当前群/私聊生效；global=true 对所有会话生效（只有主人能建）。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'title': {'type': 'string',
                              'description': '短标题（最多 40 字），如 梗图库、电脑操作技能'},
                    'text': {'type': 'string',
                             'description': '注入正文（最多 500 字）：资源路径 + 适用场景 + 该怎么做'},
                    'global': {'type': 'boolean',
                               'description': '是否全局生效（所有群/私聊）。默认 false=仅本群；true 仅主人可建'},
                },
                'required': ['title', 'text'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'inject_list',
            'description': '列出当前会话生效、以及你在这里可管理的提示词注入条目（含已停用的），返回编号供 toggle/remove',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'inject_toggle',
            'description': '启用或停用一条提示词注入（停用后不再注入，但保留内容）。只能管本群的条目；全局条目只有主人能管',
            'parameters': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer',
                           'description': '条目编号（inject_list 返回的方括号编号）'},
                    'enabled': {'type': 'boolean',
                                'description': 'true=启用，false=停用'},
                },
                'required': ['id', 'enabled'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'inject_remove',
            'description': '彻底删除一条提示词注入。只能删本群的条目；全局条目只有主人能删',
            'parameters': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer', 'description': '条目编号（inject_list 返回的方括号编号）'},
                },
                'required': ['id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'recall_user',
            'description': '按昵称/名片/QQ号查询某群友的长期记忆，可加 keyword 关键词过滤',
            'parameters': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string', 'description': '群友昵称、群名片或QQ号'},
                    'keyword': {'type': 'string', 'description': '可选关键词'},
                },
                'required': ['name'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'remember_user',
            'description': '把一条关于某群友的稳定事实记入长期记忆（只记明确事实，不要记闲聊）',
            'parameters': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string'},
                    'fact': {'type': 'string', 'description': '一句完整的第三人称事实陈述'},
                },
                'required': ['name', 'fact'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'recall_self',
            'description': ('查询你自己的第一人称记忆（你经历过的事、你答应过的事、'
                            '你和群友的关系），跨所有群共享同一份。可加 keyword 关键词'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'keyword': {'type': 'string', 'description': '可选关键词；留空返回重要的几条'},
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'remember_self',
            'description': ('记下一条关于「我自己」的第一人称记忆：我亲身经历的事、'
                            '我亲口答应的事、我和某人关系的进展。只记以后还接得上的，'
                            '日常拌嘴和一次性寒暄不要记'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'statement': {'type': 'string',
                                  'description': '一句完整的第一人称陈述，如「我答应阿凯周五帮他看 bug」'},
                },
                'required': ['statement'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'read_image',
            'description': (
                '读取工作区里的图片（如 MCP 截图工具保存的截图），把图片本身送进你的'
                '视觉输入，你就能真正"看见"它的内容。想看图就调这个，不要猜。'
                'path 必须严格照抄工具结果提示里给的完整文件名（含 mcp/ 前缀）。'
                '对截图默认叠加红色坐标网格（每 100px 一条，边缘红色数字就是该线'
                '相对原图的像素坐标，直接读数字定位控件；小控件先 crop=[x,y,w,h] '
                '裁一小块再 zoom=2 放大看，放大后网格数字仍是原图坐标，无需换算。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': '工作区相对路径，如 mcp/Snapshot_20261024_090000.jpg'},
                    'question': {'type': 'string', 'description': '可选：你想从图里确认什么，会一并交给你自己看图时参考'},
                    'grid': {'type': 'boolean',
                             'description': '是否叠加坐标网格（截图定位用，默认开；看照片等非截图可关）'},
                    'crop': {'type': 'array', 'items': {'type': 'integer'},
                             'description': '可选：只看原图某区域，[x,y,w,h] 均为原图像素坐标'},
                    'zoom': {'type': 'integer',
                             'description': '可选：查看放大倍数（1-4），小控件配合 crop 使用'},
                },
                'required': ['path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'read_video',
            'description': (
                '观看工作区里的视频或 GIF。你不能直接看视频：本工具会按画面剪辑点'
                '自动拆成分镜宫格图，交给一个视觉子代理阅读，再把总结和逐格时间轴'
                '返回给你。用户发来视频、或需要了解工作区里视频的内容时调用，'
                '不要凭文件名/大小猜内容。入站视频的占位符 [视频 … path=…] '
                '里的 path 就是已存好的工作区路径，原样传入即可。'
                '长视频默认只读开头一段，结果里会注明'
                '怎么用 from/to 续读；只想看某一段就带 from/to。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string',
                             'description': '工作区相对路径，如 uploads/xxx.mp4'},
                    'question': {'type': 'string',
                                 'description': '可选：你希望子代理重点看什么（如"主角在干什么""画面里出现了哪些字"）'},
                    'from': {'type': 'string',
                             'description': '可选：起始时间，秒数或 mm:ss，默认从头'},
                    'to': {'type': 'string',
                           'description': '可选：结束时间，秒数或 mm:ss；与 from 成对使用可续读长视频'},
                    'cells': {'type': 'integer',
                              'description': '可选：分镜格数 4-16，省略则按画面运动自动决定'},
                    'detail': {'type': 'string', 'enum': ['low', 'med', 'high'],
                               'description': '可选：台词/清单详细度，默认 med'},
                },
                'required': ['path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'click_at',
            'description': (
                '按【截图内像素坐标】点击远程桌面。系统读截图的坐标元数据，自动把'
                '图像坐标换算成 Windows 虚拟桌面坐标后再调用 MCP 鼠标工具——'
                '这是唯一可靠的点击方式，因为图像坐标和桌面坐标在多屏/偏移时不重合。'
                '流程：Snapshot(monitor=1, max_width=0) 保存截图 → read_image '
                '看网格读坐标 → 用本工具点击 → Wait 后重新 Snapshot 验证。'
                '若 Snapshot 的元素列表直接给出了目标的 center，直接调 MCP Click '
                '传 center 即可，不必走本工具。x/y 是相对 path 那张【原图】的'
                '像素坐标（网格/裁剪图标注的都是原图坐标）。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string',
                             'description': '截图的工作区相对路径（read_image 用的同一张，含 mcp/ 前缀）'},
                    'x': {'type': 'integer', 'description': '目标相对截图左上角的像素 x'},
                    'y': {'type': 'integer', 'description': '目标相对截图左上角的像素 y'},
                    'button': {'type': 'string', 'enum': ['left', 'right', 'middle'],
                               'description': '鼠标键，默认 left'},
                    'action': {'type': 'string', 'enum': ['click', 'double', 'hover'],
                               'description': '动作，默认 click'},
                },
                'required': ['path', 'x', 'y'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'view_document',
            'description': (
                '把工作区里的 PDF / Word（.pdf/.docx/.doc）逐页渲染成图片送进'
                '你的视觉输入，你就能直接"看见"并阅读页面上的文字、表格和排版。'
                '适用：扫描版 PDF（fs_read 提示没提取到文本）、图文/表格排版复杂、'
                'fs_read 结果明显缺漏时。普通可复制文本的 PDF/Word 用 fs_read 更省。'
                'PDF 直接渲染；Word 需要运行机器装有免费的 LibreOffice，'
                '没装时结果会说明。默认返回前 3 页，可传 pages 指定页码（从 1 开始）。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string',
                             'description': '工作区相对路径，如 报表.pdf、docs/a.docx'},
                    'pages': {'type': 'array', 'items': {'type': 'integer'},
                              'description': '可选：要渲染的页码（从 1 开始），默认前 3 页'},
                },
                'required': ['path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'task_add',
            'description': ('记下一条明确的约定/待办/需要后续跟进的话题。只记明确说出口的承诺，'
                            '闲聊假设不要记。owner 是责任人昵称，留空记给说话人本人；'
                            'due_date 仅在能可靠换算时填 YYYY-MM-DD 或 YYYY-MM-DD HH:MM'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'owner': {'type': 'string', 'description': '责任人昵称/QQ号，留空=说话人'},
                    'content': {'type': 'string', 'description': '事项内容'},
                    'kind': {'type': 'string',
                             'description': 'task=要做的事，topic=等后续的话题',
                             'enum': ['task', 'topic']},
                    'due_text': {'type': 'string', 'description': '人话时间，如 明天晚上'},
                    'due_date': {'type': 'string', 'description': 'YYYY-MM-DD 或 YYYY-MM-DD HH:MM，不确定留空'},
                },
                'required': ['content'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'task_list',
            'description': '查询本群未结的约定/待办；owner 留空列出全群的',
            'parameters': {
                'type': 'object',
                'properties': {'owner': {'type': 'string', 'description': '责任人昵称/QQ号，留空=全群'}},
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'task_close',
            'description': '把某条约定/待办标记为已完成',
            'parameters': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer', 'description': '事项 id（task_list 返回的方括号编号）'},
                },
                'required': ['id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'schedule_add',
            'description': (
                '登记一个定时任务：到点后系统会唤醒你，按 brief 现写一段消息，'
                '自动发到指定群或 QQ 私聊。支持三种模式：'
                'once=某时刻触发一次（填 at）；daily=每天循环（填 time）；'
                'weekly=每周循环（填 weekday+time）。'
                'brief 要写清楚：发什么内容、什么语气、给谁看；时间要按群友的原话换算准确，'
                '拿不准日期就不要登记。登记后用自然语言告诉群友已设好及触发时间。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'target_type': {'type': 'string', 'enum': ['group', 'private'],
                                    'description': '发群里还是私聊，默认 group'},
                    'target_id': {'type': 'string',
                                  'description': '群号（留空=当前群）或私聊对象 QQ 号'},
                    'mode': {'type': 'string', 'enum': ['once', 'daily', 'weekly'],
                             'description': 'once=单次 / daily=每天 / weekly=每周'},
                    'at': {'type': 'string',
                           'description': 'mode=once 时填：YYYY-MM-DD HH:MM'},
                    'time': {'type': 'string',
                             'description': 'mode=daily/weekly 时填：HH:MM（24小时制）'},
                    'weekday': {'type': 'integer',
                                'description': 'mode=weekly 时必填：1=周一 … 7=周日'},
                    'brief': {'type': 'string',
                              'description': '任务简介/创作提示：到点要写什么、发给谁、什么语气'},
                },
                'required': ['mode', 'brief'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'schedule_list',
            'description': '列出本群相关的定时任务（发往本群的、或你创建的私发任务）',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'schedule_cancel',
            'description': '取消一个定时任务（只有创建者或主人能取消）',
            'parameters': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer', 'description': '定时任务 id（schedule_list 返回的方括号编号）'},
                },
                'required': ['id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'poll_add',
            'description': (
                '给本群登记一条「轮询设定」：系统每隔一段时间（全局约 30 分钟）'
                '按概率把你唤醒，让你围绕指定任务判断要不要主动开口；没事就回 [静默]。'
                '可以登记多条，各带不同任务。参数全部由你按群友要求换算：'
                'task=这次轮询要你干什么（一句话）；'
                'prob_percent=每次检查时唤醒你的概率 0-100；'
                'every_n=保底连续检查 N 次必唤醒 1 次（0=不要保底）；'
                'active_hours=群里多少小时没人说话就跳过不唤醒（0=不限）；'
                'schedule=分时段概率 JSON 数组，形如 '
                '[{"start":"23:00","end":"08:00","percent":2}]，跨午夜自动识别，'
                '命中时段就覆盖 prob_percent，不用填 []。'
                '群里没有任何轮询时走全局默认苏醒配置；登记后用自然语言告诉群友。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'task': {'type': 'string',
                             'description': '这条轮询要主动干什么，如「提醒大家喝水」'
                                            '「看看有没有人 @ 我问的问题还没答」'},
                    'prob_percent': {'type': 'integer',
                                     'description': '每次检查的唤醒概率百分数 0-100，默认 10'},
                    'every_n': {'type': 'integer',
                                'description': '保底：连续 N 次没唤醒则必唤醒 1 次，0=关闭，默认 0'},
                    'active_hours': {'type': 'number',
                                     'description': '群里多少小时无真人发言就不唤醒，0=不限，默认 12'},
                    'schedule': {'type': 'string',
                                 'description': '分时段概率 JSON（[] 或留空=不分时段）'},
                },
                'required': ['task'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'poll_list',
            'description': '列出本群的全部轮询设定（任务、概率、保底、活跃门、分时段、连续未触发次数）',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'poll_toggle',
            'description': '启用/停用一条轮询设定（只有登记人或主人能改）',
            'parameters': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer',
                           'description': '轮询 id（poll_list 返回的方括号编号）'},
                    'enabled': {'type': 'boolean', 'description': 'true=启用，false=停用'},
                },
                'required': ['id', 'enabled'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'poll_remove',
            'description': '删除一条轮询设定（只有登记人或主人能删）',
            'parameters': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer', 'description': '轮询 id（poll_list 返回的方括号编号）'},
                },
                'required': ['id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'watcher_start',
            'description': (
                '启动一个长驻的外部监测程序（风险操作，仅主人可指令）。'
                '典型用途：监测 B 站评论/回复、网页变化等，发现事件后由程序'
                '通过自动触发接口回调，拉起你的一次后台任务（你可选择在群里开口或静默）。'
                '用法：先用 fs_write 把 Python 脚本写进本群沙箱（如 watchers/bili.py），'
                '脚本里循环检测、sleep 轮询；检测到事件后用 requests 回调（不要硬编码 token）：\n'
                'import os, requests\n'
                'requests.post(os.environ["TRIGGER_URL"], timeout=10,\n'
                '  headers={"Authorization": "Bearer "+os.environ["TRIGGER_TOKEN"]},\n'
                '  json={"group_id": os.environ["TRIGGER_GROUP"],\n'
                '        "source": "bili-reply", "text": "事件说明（2000字内）"})\n'
                '回调成功返回 202；同一 source 默认有 30 秒冷却，脚本应自行去重避免刷屏。'
                '进程分离运行，bot 重连不会被杀；机器重启后需要主人让你重新启动。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'script': {'type': 'string',
                               'description': '脚本在本群沙箱内的相对路径（.py），须先用 fs_write 写入'},
                    'args': {'type': 'array', 'items': {'type': 'string'},
                             'description': '命令行参数（可选，最多10个）'},
                    'name': {'type': 'string',
                             'description': '监测程序名（≤40字，用于辨认，也作为默认 source）'},
                },
                'required': ['script'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'watcher_list',
            'description': '列出本群（或全部）监测程序及存活状态、pid、启动时间。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'scope': {'type': 'string',
                              'description': '留空=本群；all=全部群（仅查询用）'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'watcher_stop',
            'description': '停止一个监测程序（树杀整个进程组；风险操作，仅主人可指令）。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer', 'description': '监测程序 id（watcher_list 返回的编号）'},
                },
                'required': ['id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'watcher_logs',
            'description': '查看监测程序 stdout/stderr 的最新约 4000 字（排查脚本报错用）。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer', 'description': '监测程序 id'},
                },
                'required': ['id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'now_time',
            'description': (
                '获取运行机器的当前本地时间（公历日期、时分、星期几、UNIX 时间戳）。'
                '用户说"明天/周末/每天几点"等相对时间、或登记 schedule_add 之前，'
                '先调用本工具确认今天日期和星期，不要凭印象换算；'
                '深夜跨零点场景尤其要先查。'),
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'code_run_python',
            'description': (
                '在运行机器人的这台电脑上执行 Python 3 代码（风险操作，仅主人可指令）。'
                '独立子进程运行，崩溃/死循环不影响机器人；隔离模式启动，'
                '工作目录是本群沙箱的 code/ 文件夹（生成的文件都放这里），'
                '插件 lib 里已装的第三方包可直接 import；默认 30 秒超时强杀。'
                '只有 print 到 stdout 的内容能看到；用它做计算、数据处理、'
                '批量改文件、跑脚本等。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'code': {'type': 'string', 'description': '完整可运行的 Python 源码'},
                },
                'required': ['code'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'code_run_shell',
            'description': (
                '在运行机器人的电脑上执行 Shell 命令（Windows 为 cmd /c）。'
                '风险操作，等同远程控制本机，仅主人可指令；'
                '工作目录为本群沙箱 code/，30 秒超时杀整棵进程树。'
                '返回退出码、stdout、stderr。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'command': {'type': 'string', 'description': '一条 shell 命令行'},
                },
                'required': ['command'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'custom_tool_validate',
            'description': (
                '试运行你自己写的自定义工具代码（dry-run，不落库、不上线）。'
                '风险操作，仅主人可指令。代码必须是 Python，且定义：\n'
                'def tool(args: dict, ctx: dict) -> str\n'
                '  args：模型按 params_schema 传入的参数；\n'
                '  ctx：{group_id, workspace(本群沙箱绝对路径), now, dry_run}；\n'
                '  返回字符串作为工具结果。禁止依赖模块级残留状态。\n'
                '流程：先本工具校验（给了 test_args 就真调一次），通过后再 '
                'custom_tool_publish 上线。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'name': {
                        'type': 'string',
                        'description': '工具标识，^[a-z][a-z0-9_]{1,31}$，如 bili_hot'},
                    'description': {
                        'type': 'string',
                        'description': '给模型看的功能说明（什么时候该调用、参数含义）'},
                    'params_schema': {
                        'type': 'string',
                        'description': 'JSON 字符串，标准 JSON Schema 的 parameters 段，'
                                       '如 {"type":"object","properties":{"q":{"type":"string"}}}'},
                    'code': {'type': 'string', 'description': '工具 Python 源码'},
                    'test_args': {
                        'type': 'string',
                        'description': '可选，JSON 字符串样例参数；给了就真跑一次（10s 超时）'},
                },
                'required': ['name', 'description', 'params_schema', 'code'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'custom_tool_publish',
            'description': (
                '把自定义工具正式上线（默认仅本群可用；global=true 发布到全部群/私聊，'
                '仅主人可发布）。风险操作，仅主人可指令。'
                '上线前会在隔离子进程里再次加载验证：失败则整体回滚，'
                '已有同名旧版本继续生效、框架不受影响。'
                '同名重复发布即升级该工具；owner_only_call=true 时该工具只有主人能触发。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string'},
                    'title': {'type': 'string', 'description': '短中文名，最多 20 字'},
                    'description': {'type': 'string'},
                    'params_schema': {'type': 'string', 'description': 'JSON Schema parameters 段（JSON 字符串）'},
                    'code': {'type': 'string'},
                    'global': {'type': 'boolean', 'description': 'true=全群共享；默认 false=仅本群'},
                    'owner_only_call': {'type': 'boolean', 'description': '上线后是否仅主人可触发，默认 false'},
                },
                'required': ['name', 'description', 'params_schema', 'code'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'custom_tool_list',
            'description': '列出本群可用的自定义工具（含全局共享的）及其编号、作用域、启停状态',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'custom_tool_toggle',
            'description': '启用/停用一个自定义工具（风险操作，仅主人可指令；全局工具只有主人能改）',
            'parameters': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer', 'description': 'custom_tool_list 里的编号'},
                    'enabled': {'type': 'boolean'},
                },
                'required': ['id', 'enabled'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'custom_tool_remove',
            'description': '删除一个自定义工具（风险操作，仅主人可指令；全局工具只有主人能删）',
            'parameters': {
                'type': 'object',
                'properties': {'id': {'type': 'integer'}},
                'required': ['id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'screen_validate',
            'description': (
                '试运行你自己写的「初筛脚本」（dry-run，不落库、不上线）。'
                '风险操作，仅主人可指令。初筛脚本决定一条群消息要不要进一步'
                '让判官大模型判断：每条没直接 @ 你、没回复你的群消息都会跑它，'
                '它和内置启发式粗筛、其他初筛脚本是「或」关系——任一返回 True '
                '才会花钱拉起判官。代码必须是 Python，且定义：\n'
                'def screen(group_id: str, user_id: str, message_raw: str) -> bool\n'
                '  三个参数都是字符串；返回 True=这条值得拉起判官，False=交给下一道。\n'
                '  可选第 4 参 ctx（{group_id, now, dry_run}）。\n'
                '要求：纯字符串判断（关键词/正则/发言人 QQ 等），5 秒内返回，'
                '不要发网络请求、不要写文件。拿不准就返回 False（宁可漏判官，'
                '不要刷屏），抓不准的模糊语义留给判官。\n'
                '流程：先本工具校验（给了 test_message 就用当前群和你的 QQ '
                '真跑一次，也可用 test_args 传完整 JSON），通过后再 screen_publish。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'name': {
                        'type': 'string',
                        'description': '脚本标识，^[a-z][a-z0-9_]{1,31}$，如 catch_loong'},
                    'description': {
                        'type': 'string',
                        'description': '一句话说明这个脚本在留意什么消息'},
                    'title': {'type': 'string', 'description': '短中文名，最多 20 字'},
                    'code': {'type': 'string', 'description': '初筛脚本 Python 源码'},
                    'test_message': {
                        'type': 'string',
                        'description': '可选样例消息文本；给了就以当前群和你的 QQ 真跑一次'},
                    'test_args': {
                        'type': 'string',
                        'description': '可选，完整 JSON 样例：'
                                       '{"group_id":"..","user_id":"..","message_raw":".."}'},
                },
                'required': ['name', 'description', 'code'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'screen_publish',
            'description': (
                '把初筛脚本正式上线（默认仅本群生效；global=true 发布到全部群，'
                '仅主人可发布）。风险操作，仅主人可指令。'
                '上线前会在隔离子进程里再次加载验证：失败则整体回滚，'
                '已有同名旧版本继续生效。同名重复发布即升级。'
                '上线后每条未强制触发的群消息都会执行，务必保持脚本便宜、返回克制。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string'},
                    'title': {'type': 'string', 'description': '短中文名，最多 20 字'},
                    'description': {'type': 'string'},
                    'code': {'type': 'string'},
                    'global': {'type': 'boolean',
                               'description': 'true=全群共享；默认 false=仅本群'},
                },
                'required': ['name', 'description', 'code'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'screen_list',
            'description': '列出本群生效的初筛脚本（含全局共享的）：编号、作用域、'
                           '启停状态、累计命中次数与最近命中时间',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'screen_toggle',
            'description': '启用/停用一个初筛脚本（风险操作，仅主人可指令；全局脚本只有主人能改）',
            'parameters': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer', 'description': 'screen_list 里的编号'},
                    'enabled': {'type': 'boolean'},
                },
                'required': ['id', 'enabled'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'screen_remove',
            'description': '删除一个初筛脚本（风险操作，仅主人可指令；全局脚本只有主人能删）',
            'parameters': {
                'type': 'object',
                'properties': {'id': {'type': 'integer'}},
                'required': ['id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'request_list',
            'description': (
                '查看机器人收到的好友申请/拉群邀请（你不会被自动通知，主人交代时先查这个）。\n'
                '- status：pending（待处理，默认）/ approved / rejected / failed；\n'
                '- 申请只有主人能拍板；查到后用 request_approve / request_reject 处理。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'status': {'type': 'string',
                               'description': 'pending|approved|rejected|failed',
                               'default': 'pending'},
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'request_approve',
            'description': (
                '同意一条好友申请或拉群邀请。【风险操作，仅主人可指令】\n'
                '- id：申请编号（来自 request_list 或主人私聊通知里的「申请#编号」）；\n'
                '- remark：仅好友申请生效，加为好友后的备注名（可空）；\n'
                '- flag 可能已过期，失败会明确报错，不要换参数重试。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer', 'description': '申请编号'},
                    'remark': {'type': 'string',
                               'description': '好友备注名（仅好友申请；可空）'},
                },
                'required': ['id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'request_reject',
            'description': (
                '拒绝一条好友申请或拉群邀请。【风险操作，仅主人可指令】\n'
                '- id：申请编号（来自 request_list 或主人私聊通知里的「申请#编号」）；\n'
                '- reason：拒绝理由（群邀请会带给平台，可空）。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer', 'description': '申请编号'},
                    'reason': {'type': 'string', 'description': '拒绝理由（可空）'},
                },
                'required': ['id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'loop_request',
            'description': (
                '申请进入「长跑模式」：某个任务你预估需要连续很多步工具操作、'
                '可能超过普通对话的工具轮上限时调用。申请会私聊发给主人，'
                '主人批准后才会启动，你不会在本轮立刻进入长跑；'
                '批准后你会收到一条明确的长跑任务通知，届时做完必须调用 loop_finish 收尾。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'task': {'type': 'string',
                             'description': '长跑要完成的任务，一句话说清目标与完成标准'},
                    'reason': {'type': 'string',
                               'description': '为什么需要长跑（预计多少步、在做什么），给主人判断'},
                },
                'required': ['task'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'loop_finish',
            'description': (
                '【仅长跑模式中可用】宣告长跑任务完成并结束长跑，机器人会把结果私聊汇报给主人。'
                '任务做完后必须调用它，不要自顾自停下；summary 写最终结果/产出位置。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'summary': {'type': 'string',
                                'description': '任务完成情况与结果摘要（会私聊发给主人）'},
                },
                'required': ['summary'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'history_query',
            'description': (
                '查询本群历史消息（你当前上下文滑窗之外的旧消息也能查），两种方式二选一：\n'
                '1) 时间段查询：提供 start 和/或 end，如 "2026-10-20"、'
                '"2026-10-25 09:00"；也接受 "10-25 21:30"（月日简写补今年）和 UNIX 秒；\n'
                '2) 相对条数查询：不给时间，用 offset（相对最新消息往回跳过几条，默认0）'
                '+ limit（取回条数，默认30，最多100）。\n'
                '结果按时间正序返回，含说话人昵称/QQ 和时间标签；不含系统内部通知；'
                '默认不含工具调用，需要排查工具过程时 include_tools=true。'
                '历史里的图片显示为 [CQ:image,file=…]，可用 read_message_image 回看；'
                '合并转发显示为 [CQ:forward,id=…]，可用 read_forward 展开。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'start': {'type': 'string',
                              'description': '起始时间（含），YYYY-MM-DD 或 YYYY-MM-DD HH:MM[:SS] 或 UNIX 秒'},
                    'end': {'type': 'string',
                            'description': '结束时间（含），格式同 start'},
                    'offset': {'type': 'integer',
                               'description': '相对条数模式：相对最新消息往回跳过的条数，默认 0'},
                    'limit': {'type': 'integer',
                              'description': '最多返回条数，默认 30，上限 100'},
                    'include_tools': {'type': 'boolean',
                                      'description': '是否包含工具调用/结果行，默认 false'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'token_stats',
            'description': (
                '统计本群 DeepSeek token 用量，按 输入命中缓存 / 输入未命中 / 输出 '
                '三类汇总，并细分调用类型（对话/判官/摘要/记忆抽取/风格）。'
                '用户问"用了多少 token、花了多少、缓存命中率"时调用。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'days': {'type': 'number',
                             'description': '统计最近多少天，默认 30；0 表示全部历史'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'qzone_publish',
            'description': (
                '在机器人账号的 QQ 空间发布一条纯文字说说（风险操作，仅主人可指令）。'
                '通过 NapCat 自带接口发表，发表成功会返回 tid 并在本地留痕，'
                '之后可用 qzone_delete 删除、qzone_recent 查最近发布。'
                '默认所有人可见；ugc_right 可见范围：1 所有人（默认）、4 好友、'
                '16 部分好友可见（必须给 target_uins）、64 仅自己、'
                '128 部分好友不可见（必须给 target_uins）。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'content': {'type': 'string', 'description': '说说正文（纯文字，最多 5000 字）'},
                    'ugc_right': {'type': 'integer', 'enum': [1, 4, 16, 64, 128],
                                  'description': '可见范围，默认 1 所有人可见'},
                    'target_uins': {'type': 'array', 'items': {'type': 'string'},
                                    'description': '部分好友 QQ 号数组，仅 ugc_right=16/128 时需要'},
                },
                'required': ['content'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'qzone_delete',
            'description': '按 tid 删除机器人账号 QQ 空间里自己发的说说'
                           '（风险操作，仅主人可指令；tid 来自发表结果或 qzone_recent）。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'tid': {'type': 'string', 'description': '说说 tid'},
                },
                'required': ['tid'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'qzone_recent',
            'description': '查最近由本机器人发布的说说（本地留痕：tid、可见范围、'
                           '发布时间、正文预览），用于拿 tid 做删除。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'limit': {'type': 'integer', 'description': '条数，默认 10，最多 30'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'read_message_image',
            'description': (
                '回看群消息里以 [CQ:image,file=...] 出现的图片：历史消息、'
                '触发前背景消息、合并转发节点里的图片都能用。把码里的 file 值'
                '原样作为 ref 传入，图片会作为多模态内容返回给你。'
                '当前这条消息自带的图片你已经能直接看到，不要重复调用；'
                '同一张图本轮只调一次。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'ref': {'type': 'string',
                            'description': '[CQ:image,file=<这里的值>]，不要填 URL 或文件路径'},
                    'question': {'type': 'string',
                                 'description': '可选：你想在这张图里确认什么，会随图一起标注'},
                },
                'required': ['ref'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'read_forward',
            'description': (
                '展开合并转发消息。id 取自消息文本里的 [CQ:forward,id=...]。'
                '每页最多 20 条，用 offset 翻页；节点中的图片显示为 '
                '[CQ:image,file=…]，需要看图再调 read_message_image。'
                '若返回过期提示，请群友重新转发一次。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'string', 'description': '[CQ:forward,id=<这里>] 的 id'},
                    'offset': {'type': 'integer',
                               'description': '跳过前几条（翻页用），默认 0'},
                    'limit': {'type': 'integer',
                              'description': '本页条数，默认 20，最大 20'},
                },
                'required': ['id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'meme_lookup',
            'description': (
                '查网络梗/黑话/流行语的含义与出处。可以用中文全名、中文别名缩写、'
                '或拼音查询：三字梗允许错一个汉字，拼音允许差一个音节或一两个字母，'
                '你听到群友说听不懂的词、或想确认某个梗的准确含义/用法时调用。'
                '消息里提到的梗系统通常已自动注入释义，只有需要进一步确认或'
                '查询没被注入的词时才调。'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string',
                              'description': '要查的梗：中文名/别名/拼音，如 小黑子、xhz、neijuan'},
                },
                'required': ['query'],
            },
        },
    },
]

SCHEMAS_BY_NAME = {s['function']['name']: s for s in _SCHEMAS}

# 只有主人能指令的风险内置工具（MCP 远程桌面工具整体另算，见 engine._dispatch）
RISKY_TOOL_KEYS = frozenset({
    'fs.write', 'fs.append', 'fs.delete', 'fs.publish',
    # @全体成员打扰全群且每日次数有限：仅主人可指令
    'qq.at_all',
    # 在本机执行代码/命令、注册可执行的自定义工具：等同远程操控本机
    'code.python', 'code.shell',
    'custom.validate', 'custom.publish', 'custom.toggle', 'custom.remove',
    # AI 自制初筛脚本：每条未触发群消息都会执行它放行的判定代码
    'screen.validate', 'screen.publish', 'screen.toggle', 'screen.remove',
    # 批准/拒绝好友申请与拉群邀请：外发不可逆，只有主人能拍板
    'request.approve', 'request.reject',
    # 启动/停止长驻监测进程：等同在本机长期执行任意代码
    'watcher.start', 'watcher.stop',
    # 公开发布 / 删除 QQ 空间说说：外发不可逆操作
    'qzone.publish', 'qzone.delete',
})


@dataclass
class ToolContext:
    bot: object
    group_id: str
    cfg: dict
    sandbox: object
    db_path: str = ''
    chat_type: str = 'group'
    memory: object = None
    self_memory: object = None
    tasks: object = None
    scheduler: object = None
    proactor: object = None
    watchers: object = None
    qzone: object = None
    mcp: object = None
    custom: object = None
    screens: object = None
    requests: object = None
    member_info: object = None
    loop: object = None
    meme_guide: object = None
    # LLMClient：read_video 要起视觉子代理、台词转写也要用它
    llm: object = None
    # loop 长跑轮的运行态 dict（非 loop 轮为 None）；loop.finish 靠它标记收尾
    loop_ctx: object = None
    bot_id: str = ''
    sent_texts: set = field(default_factory=set)
    # 引用回复状态 {'id': 触发消息 id, 'used': bool}；每轮一份，
    # 第一个真正发出的消息（文本/图片/语音）引用触发消息
    quote_state: dict | None = None
    log: object = None
    sender_id: str = ''
    sender_name: str = ''
    # 本轮状态：已回看的图片哈希去重；合并转发全量节点缓存（翻页不再请求）
    image_seen: set = field(default_factory=set)
    fw_cache: dict = field(default_factory=dict)

    @property
    def is_owner(self) -> bool:
        return bool(self.sender_id) and str(self.sender_id) == str(
            self.cfg.get('owner_qq', ''))


@dataclass
class ToolMediaResult:
    """需要以多模态 content 回传给模型的工具结果（read_image 看图用）。

    落库/日志只存 text；真正的图片 parts 只活在当轮 messages 里。
    """
    text: str
    images: list = field(default_factory=list)  # [(mime, base64_str)]

    def to_parts(self) -> list:
        parts = [{'type': 'text', 'text': self.text}]
        for mime, b64 in self.images:
            parts.append({'type': 'image_url',
                          'image_url': {'url': f'data:{mime};base64,{b64}'}})
        return parts


def schemas(enabled_keys: set | None = None) -> list:
    """按固定顺序返回工具 schema；enabled_keys 为 None 表示全开。"""
    out = []
    for s in _SCHEMAS:
        key = TOOL_KEYS[s['function']['name']]
        if enabled_keys is None or enabled_keys.intersection({key, '__all__'}):
            out.append(s)
    return out


def is_builtin(name: str) -> bool:
    return name in SCHEMAS_BY_NAME


def _truncate(s: str, cfg: dict) -> str:
    n = int(cfg.get('tool_result_max_chars', 2000))
    return s if len(s) <= n else s[:n] + '\n…(已截断)'


_OFFICE_EXT = ('pdf', 'docx', 'xlsx', 'xlsm', 'xls')


def _fs_read_paged(rel: str, offset: int, limit: int, ctx: ToolContext) -> str:
    """读文件并按字符分页（同步，调用方 to_thread）。

    走 resolve 直读而非 read_bytes/read_file：源文件上限用更宽松的
    read_file_max_kb（默认 4MiB），办公文档抽全文后同样支持 offset/limit。
    """
    real, _ = ctx.sandbox.resolve(rel)
    if not real.exists() or real.is_dir():
        raise SandboxError(f'文件不存在: {rel}')
    src_cap_kb = int(ctx.cfg.get('read_file_max_kb', 4096))
    raw = real.read_bytes()
    if len(raw) > src_cap_kb * 1024:
        raise SandboxError(
            f'文件 {len(raw)} 字节，超过 {src_cap_kb}KiB 读取上限')
    ext = real.suffix.lower().lstrip('.')
    if ext in _OFFICE_EXT:
        from . import docparse
        budget = int(ctx.cfg.get('read_file_max_chars', 30000))
        text = docparse.extract_text(real.name, raw, budget)
    else:
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            return (f'二进制文件（{len(raw)} 字节，非文本）。前 64 字节：'
                    f'{raw[:64].hex()}')
    return _page_slice(rel, text, offset, limit)


def _page_slice(rel: str, text: str, offset: int, limit: int) -> str:
    """全文切片成一页：头部长度声明 + 切片 + 尾部续读提示（显式、不静默截断）。"""
    total = len(text)
    if offset < 0:
        offset = 0
    if offset >= total:
        return (f'📄 {rel}｜全文 {total} 字符｜offset={offset} 已到文件末尾，'
                f'没有更多内容（要重读从 offset=0 开始）。')
    page = text[offset:offset + limit]
    end = offset + len(page)
    out = [f'📄 {rel}｜全文 {total} 字符｜显示 {offset}–{end}']
    out.append(page)
    if end < total:
        out.append(
            f'…还有 {total - end} 字符未读（显式分页，未静默截断）。'
            f'继续读：fs_read(path="{rel}", offset={end}, limit={limit})')
    return '\n'.join(out)


def _local_cq(kind: str, source: str, ctx: ToolContext) -> str:
    """URL 直接透传；本地相对路径读字节内联 base64（NapCat 可能不在本机，
    绝对路径对它不可见——base64:// 是 OneBot v11 跨机发送的标准方式）。

    本地图片走 resolve 直读而非 read_bytes：截图原图普遍超过工作区通用
    512KiB 文件上限；超出 send_image_max_kb 时在内存里压成 JPEG，
    绝不动 mcp/ 原图（click_at 坐标映射依赖未采样原图）。
    """
    if source.startswith(('http://', 'https://', 'base64://')):
        return f'[CQ:{kind},file={source}]'
    real, _ = ctx.sandbox.resolve(source)
    if not real.exists() or real.is_dir():
        raise SandboxError(f'文件不存在: {source}')
    src_cap_kb = int(ctx.cfg.get('mcp_media_max_kb', 15360))
    raw = real.read_bytes()
    if len(raw) > src_cap_kb * 1024:
        raise SandboxError(
            f'{kind} 源文件 {len(raw)} 字节，超过 {src_cap_kb}KiB 发送上限')
    if kind == 'image':
        raw, _mime, _changed = media_out.compress_for_send(
            raw, real.suffix, int(ctx.cfg.get('send_image_max_kb', 3072)))
    return f'[CQ:{kind},file=base64://{base64.b64encode(raw).decode("ascii")}]'


_IMG_SNIFF = (
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
    (b'BM', 'image/bmp'),
)


def _sniff_image(raw: bytes) -> str:
    """魔数识别图片真实类型；WEBP 查 RIFF 头。"""
    for magic, mime in _IMG_SNIFF:
        if raw.startswith(magic):
            return mime
    if raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        return 'image/webp'
    return ''


def _load_image(path: str, question: str, ctx: ToolContext,
                want_grid: bool, box, zoom: int) -> ToolMediaResult:
    """读图、校验类型与大小，按需裁剪/放大/打网格，打包成视觉 tool 结果。

    同步函数（调用方 to_thread）；Pillow 缺失时纯看图可用，显式 crop 才报错。
    """
    real, _ = ctx.sandbox.resolve(path)
    if not real.exists() or real.is_dir():
        raise SandboxError(f'图片不存在: {path}')
    # 源文件上限跟 MCP 媒体落盘一致（截图原图常达数 MiB）；送视觉前再压回
    # view_cap_kb 以内，所以这里不能拿读图输出上限直接挡源文件
    src_cap_kb = int(ctx.cfg.get('mcp_media_max_kb', 15360))
    raw = real.read_bytes()
    if len(raw) > src_cap_kb * 1024:
        raise SandboxError(f'图片 {len(raw)} 字节，超过 {src_cap_kb}KiB 读图源上限')
    cap_kb = int(ctx.cfg.get('read_image_max_kb', 2048))
    mime = _sniff_image(raw)
    if not mime:
        ext = real.suffix.lower().lstrip('.')
        mime = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
                'gif': 'image/gif', 'webp': 'image/webp',
                'bmp': 'image/bmp'}.get(ext, '')
    if not mime:
        raise SandboxError('不是受支持的图片（png/jpg/gif/webp/bmp）')

    dims = imgmark.image_size(raw)
    size_str = f'{dims[0]}x{dims[1]}' if dims else '尺寸未知'
    notes = [f'已打开图片 {path}（{len(raw)} 字节，{mime}，原图 {size_str}），'
             f'下面就是这张图的画面。']
    view_mime, view_raw = mime, raw
    if not (want_grid or box or (zoom and zoom > 1)) \
            and len(raw) > cap_kb * 1024:
        # 无网格/裁剪的纯看图：原图超视觉输出预算时内存里压成 JPEG，
        # 不动磁盘原图（click_at 仍按原图像素坐标映射）
        view_raw, view_mime, _ = media_out.compress_for_send(
            raw, real.suffix, cap_kb)
        if view_mime == 'image/jpeg':
            notes[0] = (f'已打开图片 {path}（原图 {len(raw)} 字节，{mime}，'
                        f'{size_str}；为控制体积已转 JPEG 压缩视图），'
                        f'下面就是这张图的画面。')
    if want_grid or box or (zoom and zoom > 1):
        try:
            step = int(ctx.cfg.get('vision_grid_step', 100))
            view = imgmark.render_view(
                raw, step=step, grid=want_grid, box=box, zoom=zoom,
                max_kb=cap_kb)
            view_mime, view_raw = view.mime, view.raw
            if view.crop_box:
                x0, y0, w, h = view.crop_box
                notes.append(
                    f'当前画面是原图 ({x0},{y0}) 起 {w}x{h} 区域放大 '
                    f'{view.zoom} 倍的结果。')
            if want_grid:
                dense = '放大视图主刻度自动加密为 50px，' if view.zoom > 1 else ''
                notes.append(
                    f'画面上有红色坐标网格（{dense}每 {step}px 一条主刻度，'
                    f'{step * 5}px 处为粗线，另有浅色半距辅助线；'
                    f'白色标签居中压在对应刻度线上），上沿数字是 x、左沿是 y，'
                    f'都是相对原图左上角的原图像素坐标。'
                    f'目标不压在线上时，按它在相邻两刻度间的位置比例估任意整数，'
                    f'不要把坐标凑整到刻度数字上。')
            notes.append('要点击就把目标处的坐标数字（裁剪放大也不用自己加偏移）'
                         '传给 click_at。')
        except imgmark.ImgMarkError as e:
            if box or (zoom and zoom > 1):
                raise SandboxError(f'裁剪/放大失败: {e}')
            notes.append(f'（本环境未启用网格渲染：{e}；坐标需按整图自行估算）')
    if question.strip():
        notes.append(f'你想确认的问题：{question.strip()}')
    b64 = base64.b64encode(view_raw).decode('ascii')
    return ToolMediaResult(text=''.join(notes), images=[(view_mime, b64)])


def _click_prepare(path: str, x: int, y: int, ctx: ToolContext) -> tuple[int, int, str]:
    """读截图 sidecar 并把图像坐标映射成桌面坐标（同步，调用方 to_thread）。"""
    real, _ = ctx.sandbox.resolve(path)
    if not real.exists() or real.is_dir():
        raise SandboxError(f'截图不存在: {path}')
    meta_real, _ = ctx.sandbox.resolve(imgmark.sidecar_rel(path))
    if not meta_real.exists():
        raise SandboxError(
            '这张图没有坐标元数据，无法可靠映射（可能不是 Snapshot 截图、'
            '截图前给 max_width 降了采样，或是旧版本保存的图）。'
            '请重新调用截图工具，参数用 monitor=1、max_width=0，'
            '再对新截图 read_image + click_at。')
    meta = imgmark.load_meta(meta_real.read_text('utf-8'))
    dx, dy, warn, err = imgmark.map_point(meta, x, y)
    if err:
        raise SandboxError(err)
    return dx, dy, warn


def _view_document(path: str, want_pages, ctx: ToolContext) -> ToolMediaResult:
    """PDF/Word 渲染成页面 JPEG（同步重活，调用方 to_thread）。

    走 sandbox.resolve 而不是 read_bytes：文档源文件上限（doc_render_max_kb）
    比工作区通用文件配额（max_file_size_kb）宽松，resolve 仍做越界校验。
    """
    from . import docrender
    real, _ = ctx.sandbox.resolve(path)
    if not real.exists() or real.is_dir():
        raise SandboxError(f'文档不存在: {path}')
    src_cap = int(ctx.cfg.get('doc_render_max_kb', 20480))
    raw = real.read_bytes()
    if len(raw) > src_cap * 1024:
        raise SandboxError(f'文档 {len(raw)} 字节，超过 {src_cap}KiB 渲染上限')
    pages = docrender.render_document(
        real.name, raw, want_pages=want_pages,
        max_pages=int(ctx.cfg.get('doc_render_max_pages', 3)),
        dpi=int(ctx.cfg.get('doc_render_dpi', 130)),
        page_max_kb=int(ctx.cfg.get('read_image_max_kb', 2048)),
        src_max_kb=src_cap)
    pnos = '、'.join(str(n) for n, _ in pages)
    fname = path.rsplit('/', 1)[-1]
    text = (f'已把文档 {fname} 的第 {pnos} 页渲染成 {len(pages)} 张页面图'
            f'（按页码顺序附在下面），直接阅读画面里的文字、表格和排版即可。')
    images = [('image/jpeg', base64.b64encode(jpg).decode('ascii'))
              for _, jpg in pages]
    return ToolMediaResult(text=text, images=images)


async def _dispatch_inject(name: str, args: dict, ctx: ToolContext) -> str:
    """提示词注入表的增/查/启停/删（DB 短操作走 to_thread，不阻塞事件循环）。"""
    if not ctx.db_path:
        return '错误: 注入功能不可用（数据库未初始化）'
    db = ctx.db_path

    if name == 'inject_add':
        title = str(args.get('title', '') or '').strip()
        text = str(args.get('text', '') or '').strip()
        if not title or not text:
            return '错误: title 和 text 必填'
        if len(title) > 40:
            return '错误: title 最多 40 字'
        if len(text) > 500:
            return '错误: text 最多 500 字，写最关键的路径和用法'
        is_global = bool(args.get('global'))
        if is_global and not ctx.is_owner:
            return ('错误: 全局注入（对所有群/私聊生效）只有主人能创建。'
                    '不传 global 只给本群写即可。')
        scope = (storage.GLOBAL_INJECTION_SCOPE if is_global
                 else str(ctx.group_id))
        cap = int(ctx.cfg.get('inject_max_per_scope', 30))
        n = await asyncio.to_thread(storage.count_injections, db, scope)
        if n >= cap:
            return (f'错误: 该范围已达 {cap} 条上限，请先用 inject_list 查编号、'
                    f'inject_remove 删旧条目或 inject_toggle 停用')
        iid = await asyncio.to_thread(
            storage.add_injection, db, scope, title, text, ctx.sender_id)
        where = '全局（所有群/私聊）' if is_global else '仅本群/私聊'
        return (f'已加入你的自我注入表 [id={iid}]（{where}）：'
                f'以后每次对话都会自动带上这条提醒「{title}」。')

    if name == 'inject_list':
        rows = await asyncio.to_thread(
            storage.list_injections, db, str(ctx.group_id))
        if not rows:
            return '（你的注入表为空——学到可复用技能、或往 public/ 沉淀素材后，用 inject_add 记一条）'
        lines = ['你的提示词注入表（方括号内是管理用编号）：']
        for r in rows:
            tag = '全局' if r['global'] else '本群'
            mark = '✅' if r['enabled'] else '⏸停用'
            lines.append(f"[{r['id']}]{mark}（{tag}）{r['title']}：{r['text']}")
        return _truncate('\n'.join(lines), ctx.cfg)

    # toggle / remove：归属与权限校验
    try:
        iid = int(args.get('id'))
    except (TypeError, ValueError):
        return '错误: id 必须是 inject_list 返回的整数编号'
    row = await asyncio.to_thread(storage.get_injection, db, iid)
    if row is None:
        return '错误: 注入条目不存在（编号照抄 inject_list 的结果）'
    if row['global']:
        if not ctx.is_owner:
            return '错误: 全局注入条目只有主人能管理'
    elif row['scope'] != str(ctx.group_id):
        return '错误: 不能管理其他群/私聊的注入条目'

    if name == 'inject_toggle':
        enabled = bool(args.get('enabled'))
        await asyncio.to_thread(storage.set_injection_enabled, db, iid, enabled)
        return f'条目 [{iid}]「{row["title"]}」已{"启用" if enabled else "停用"}'
    ok = await asyncio.to_thread(storage.delete_injection, db, iid)
    return f'已删除条目 [{iid}]「{row["title"]}」' if ok else '错误: 删除失败'


_USAGE_KIND_CN = {
    'chat': '对话',
    'judge': '判官',
    'summary': '摘要',
    'extract': '记忆抽取',
    'style': '风格学习',
    'scheduled': '定时任务',
    'mood': '心情判定',
    'video': '视频阅读',
}


def _render_usage_breakdown(bd: dict, days: float) -> str:
    t = bd.get('totals', {})
    cached, uncached, out = (int(t.get(k, 0))
                             for k in ('cached', 'uncached', 'out'))
    calls, ok_calls = int(t.get('calls', 0)), int(t.get('ok_calls', 0))
    span = '全部历史' if not days else f'近 {days:g} 天'
    if not calls:
        return f'本群{span}没有 API 调用记录'
    tin = cached + uncached
    hit_rate = f'{cached / tin * 100:.1f}%' if tin else '—'
    lines = [
        f'本群 token 用量（{span}，共 {calls} 次调用'
        f'，失败 {max(0, calls - ok_calls)} 次）：',
        f'输入命中缓存 {cached:,}｜输入未命中 {uncached:,}｜输出 {out:,}'
        f'（输入缓存命中率 {hit_rate}）',
    ]
    by = bd.get('by_kind') or []
    if by:
        lines.append('按调用类型：')
        for k in by:
            kn = _USAGE_KIND_CN.get(k['kind'], k['kind'])
            lines.append(
                f'- {kn} ×{k["calls"]}：命中 {k["cached"]:,} / '
                f'未命中 {k["uncached"]:,} / 输出 {k["out"]:,}')
    return '\n'.join(lines)


async def dispatch(name: str, args: dict, ctx: ToolContext) -> str:
    """执行内置工具，返回给模型的文本结果。异常被转成文本，不抛出工具循环。

    多数工具返回 str；read_image 返回 ToolMediaResult（多模态 tool 消息）。
    """
    args = args or {}
    try:
        if name == 'read_image':
            path = str(args.get('path', '')).strip()
            if not path:
                return '错误: path 为空'
            if 'grid' in args:
                want_grid = bool(args.get('grid'))
            else:
                want_grid = bool(ctx.cfg.get('vision_grid_on', True))
            box = args.get('crop')
            if box is not None and (not isinstance(box, list)
                                    or len(box) != 4):
                return '错误: crop 必须是 [x,y,w,h] 四个整数'
            try:
                zoom = int(args.get('zoom', 1) or 1)
            except (TypeError, ValueError):
                return '错误: zoom 必须是 1-4 的整数'
            if not 1 <= zoom <= 4:
                return '错误: zoom 只能取 1-4'
            return await asyncio.to_thread(
                _load_image, path, str(args.get('question', '') or ''), ctx,
                want_grid, box, zoom)

        if name == 'read_video':
            # 耗时长（解码 + 子代理），超时由 engine 单独放宽
            return await video_read.handle(args, ctx)

        if name == 'click_at':
            path = str(args.get('path', '')).strip()
            if not path:
                return '错误: path 为空'
            try:
                x, y = int(args.get('x')), int(args.get('y'))
            except (TypeError, ValueError):
                return '错误: x/y 必须是截图内像素整数'
            dx, dy, warn = await asyncio.to_thread(
                _click_prepare, path, x, y, ctx)
            if ctx.mcp is None:
                return '错误: 没有可用的 MCP 远程桌面连接，无法点击'
            mcp_args = {'x': dx, 'y': dy}
            button = str(args.get('button', 'left') or 'left')
            if button in ('left', 'right', 'middle'):
                mcp_args['button'] = button
            action = str(args.get('action', 'click') or 'click')
            if action in ('click', 'double', 'hover'):
                mcp_args['action'] = action
            res = await ctx.mcp.call_short_tool(
                'Click', mcp_args, ctx.cfg, sandbox=ctx.sandbox)
            tail = (f'已把截图坐标 ({x},{y}) 映射为桌面坐标 ({dx},{dy}) 并执行'
                    f'{mcp_args["button"]}/{mcp_args["action"]}。'
                    f'下一步必须 Wait 约 0.5 秒后重新 Snapshot 验证是否点中；'
                    f'没点中按新截图的网格修正一次，连续两次失败就停下告诉用户。')
            if warn:
                tail = f'注意：{warn}\n' + tail
            return tail + '\nMCP 返回：' + str(res)

        if name == 'view_document':
            path = str(args.get('path', '')).strip()
            if not path:
                return '错误: path 为空'
            pages_arg = args.get('pages')
            want_pages = None
            if pages_arg is not None:
                if not isinstance(pages_arg, list) or not pages_arg:
                    return '错误: pages 必须是非空整数数组（页码从 1 开始）'
                try:
                    want_pages = [int(p) for p in pages_arg]
                except (TypeError, ValueError):
                    return '错误: pages 必须是整数页码'
                if any(p < 1 for p in want_pages):
                    return '错误: 页码从 1 开始'
            return await asyncio.to_thread(
                _view_document, path, want_pages, ctx)

        if name == 'qq_send_text':
            text = str(args.get('text', '')).strip()
            if not text:
                return '错误: text 为空'
            from .reply import sanitize_model_text, take_quote, reply_prefix
            text = sanitize_model_text(text)
            # 引用前缀只加在真正发出的消息上；去重集合仍记净文本，
            # 否则终态复读同一句时会被当成新内容再发一遍
            send_text = reply_prefix(take_quote(ctx.quote_state)) + text
            await ctx.bot.send_message(message=send_text,
                                       target_id=ctx.group_id,
                                       message_type=ctx.chat_type)
            ctx.sent_texts.add(text)
            return '已发送'

        if name == 'qq_at_all':
            if ctx.chat_type != 'group':
                return '错误: @全体成员只能在群聊使用'
            if ctx.member_info is None or not ctx.bot_id:
                return '错误: 群成员信息不可用，无法确认你的管理员身份'
            me = await ctx.member_info.info(
                ctx.group_id, ctx.bot_id, ctx.bot)
            if not me or me.get('role') not in ('owner', 'admin'):
                return ('错误: 你在本群是普通成员，QQ 不允许普通成员 @全体成员。'
                        '不要重试，也不要改在正文里写 @所有人')
            text = str(args.get('text', '') or '').strip()
            if len(text) > 500:
                return '错误: 通知正文最多 500 字'
            from .reply import sanitize_model_text, strip_leading_time
            text = strip_leading_time(sanitize_model_text(text))
            msg = '[CQ:at,qq=all]' + ((' ' + text) if text else '')
            await ctx.bot.send_message(message=msg, target_id=ctx.group_id,
                                       message_type='group')
            ctx.sent_texts.add(msg)
            return '已发送 @全体成员'

        if name == 'qq_member_info':
            if ctx.chat_type != 'group':
                return '错误: 群成员身份只在群聊可查'
            if ctx.member_info is None:
                return '错误: 群成员信息不可用'
            uid = args.get('user_id')
            if uid in (None, ''):
                uid = ctx.bot_id
            else:
                try:
                    uid = str(int(uid))
                except (TypeError, ValueError):
                    return '错误: user_id 必须是 QQ 号整数'
            info = await ctx.member_info.info(
                ctx.group_id, uid, ctx.bot,
                refresh=bool(args.get('no_cache', False)))
            if not info:
                return f'没查到 QQ {uid} 在本群的信息（可能已退群或接口暂时失败）'
            from .mentions import describe_member
            lines = [f'QQ {uid} 在本群的身份：',
                     f"身份：{describe_member(info)}"]
            if info.get('card'):
                lines.append(f"群名片：{info['card']}")
            if info.get('nickname'):
                lines.append(f"昵称：{info['nickname']}")
            if info.get('join_time'):
                lines.append('入群时间：'
                             + datetime.datetime.fromtimestamp(
                                 info['join_time']).strftime('%Y-%m-%d'))
            if info.get('last_sent_time'):
                lines.append('最近发言：'
                             + datetime.datetime.fromtimestamp(
                                 info['last_sent_time']).strftime('%Y-%m-%d %H:%M'))
            return '\n'.join(lines)

        if name in ('qq_send_image', 'qq_send_voice'):
            source = str(args.get('source', '')).strip()
            if not source:
                return '错误: source 为空'
            kind = 'image' if name == 'qq_send_image' else 'record'
            cq = await asyncio.to_thread(_local_cq, kind, source, ctx)
            # 图片/语音不占用本轮引用：引用只给文本（qq_send_text 或终态），
            # 否则先发一张图/一条语音就把「第一句引用」用掉了，真正的文字
            # 回复反而引用不上；媒体带 [CQ:reply] 头也容易被客户端显示成怪样
            await ctx.bot.send_message(message=cq, target_id=ctx.group_id,
                                       message_type=ctx.chat_type)
            return '已发送'

        if name == 'qq_list_files':
            from . import remote_files
            folder_id = str(args.get('folder_id', '') or '').strip()
            if ctx.chat_type == 'private':
                rows = await asyncio.to_thread(
                    storage.list_private_files, ctx.db_path, ctx.group_id)
                return remote_files.list_private_files(rows)
            return await remote_files.list_group_files(
                ctx.bot, ctx.group_id, folder_id)

        if name == 'qq_download_file':
            from . import remote_files
            file_id = str(args.get('file_id', '') or '').strip()
            name = str(args.get('name', '') or '').strip()
            if not file_id:
                return '错误: file_id 为空（照抄 qq_list_files 的结果）'
            try:
                busid = int(args.get('busid') or 0)
            except (TypeError, ValueError):
                return '错误: busid 必须是整数'
            try:
                text, images = await remote_files.fetch_remote_file(
                    ctx.bot, ctx.chat_type, ctx.group_id, file_id, busid,
                    name, db_path=ctx.db_path, cfg=ctx.cfg, log=ctx.log)
            except remote_files.RemoteFileError as e:
                return f'错误: {e}'
            if images:
                return ToolMediaResult(
                    text=text or f'已下载 {name}，页面图见下', images=images)
            return text or f'已下载 {name}，但没提取到可读内容'

        if name == 'fs_list':
            items = await asyncio.to_thread(ctx.sandbox.list_dir,
                                            str(args.get('path', '.') or '.'))
            if not items:
                return '(空目录)'
            lines = [f"{'[目录]' if i['type'] == 'dir' else '      '} {i['name']}"
                     + ('' if i['type'] == 'dir' else f" ({i['size']}B)")
                     for i in items]
            return _truncate('\n'.join(lines), ctx.cfg)

        if name == 'fs_read':
            rel = str(args.get('path', ''))
            if not rel.strip():
                return '错误: path 为空'
            try:
                offset = int(args.get('offset') or 0)
            except (TypeError, ValueError):
                return '错误: offset 必须是非负整数'
            try:
                page = int(args.get('limit') or 0)
            except (TypeError, ValueError):
                return '错误: limit 必须是正整数'
            page_max = int(ctx.cfg.get('read_page_max_chars', 8000))
            if page <= 0:
                page = int(ctx.cfg.get('tool_result_max_chars', 2000))
            page = min(page, page_max)
            return await asyncio.to_thread(
                _fs_read_paged, rel, max(0, offset), page, ctx)

        if name in ('fs_write', 'fs_append'):
            size = await asyncio.to_thread(
                ctx.sandbox.write_file, str(args.get('path', '')),
                str(args.get('content', '')), name == 'fs_append')
            return f'已写入 {size} 字节'

        if name == 'fs_delete':
            ok = await asyncio.to_thread(ctx.sandbox.delete,
                                         str(args.get('path', '')))
            return '已删除' if ok else '文件不存在'

        if name == 'fs_publish':
            source = str(args.get('source', '') or '').strip()
            dest = str(args.get('dest', '') or '').strip()
            if not source or not dest:
                return '错误: source 和 dest 必填'

            def _publish():
                src_real, _ = ctx.sandbox.resolve(source)
                if not src_real.exists() or src_real.is_dir():
                    raise SandboxError(f'源文件不存在: {source}')
                return ctx.sandbox.write_public(dest, src_real.read_bytes())

            size = await asyncio.to_thread(_publish)
            return (f'已沉淀到 {dest}（{size} 字节），所有群/私聊都能读取，'
                    f'且不随群工作区清理；记得用 inject_add 写一条提醒，'
                    f'说明这个资源什么场景下该用。')

        if name in ('inject_add', 'inject_list', 'inject_toggle',
                    'inject_remove'):
            return await _dispatch_inject(name, args, ctx)

        if name in ('code_run_python', 'code_run_shell'):
            from . import coderunner
            is_py = name == 'code_run_python'
            key = 'code' if is_py else 'command'
            src = str(args.get(key, '') or '')
            if not src.strip():
                return f'错误: {key} 为空'
            code_dir, _ = ctx.sandbox.resolve('code', for_write=True)
            timeout = min(120, max(5, int(ctx.cfg.get('code_run_timeout', 30))))

            def _run_code():
                extra = [coderunner.plugin_lib_dir()] if is_py else None
                res = coderunner.run(
                    src, 'python' if is_py else 'shell', str(code_dir),
                    timeout=timeout, extra_paths=extra)
                return coderunner.format_result(
                    res, int(ctx.cfg.get('tool_result_max_chars', 2000)))

            return await asyncio.to_thread(_run_code)

        if name in ('custom_tool_validate', 'custom_tool_publish',
                    'custom_tool_list', 'custom_tool_toggle',
                    'custom_tool_remove'):
            if ctx.custom is None:
                return '错误: 自定义工具功能不可用'
            return await ctx.custom.dispatch_manage(name, args, ctx)

        if name in ('screen_validate', 'screen_publish',
                    'screen_list', 'screen_toggle', 'screen_remove'):
            if ctx.screens is None:
                return '错误: 初筛脚本功能不可用'
            return await ctx.screens.dispatch_manage(name, args, ctx)

        if name == 'request_list':
            if ctx.requests is None:
                return '错误: 申请审批功能不可用'
            return await asyncio.to_thread(
                ctx.requests.list_text, str(args.get('status', 'pending')))

        if name in ('request_approve', 'request_reject'):
            if ctx.requests is None:
                return '错误: 申请审批功能不可用'
            req_id = args.get('id')
            note = str(args.get('remark', '') or args.get('reason', '') or '')
            return await ctx.requests.decide(
                ctx.bot, req_id, name == 'request_approve', note,
                ctx.sender_id)

        if name == 'loop_request':
            if ctx.loop is None:
                return '错误: 长跑功能不可用'
            if not ctx.cfg.get('loop_on', True):
                return '错误: 本群已关闭长跑模式'
            task = str(args.get('task', '') or '').strip()
            if not task:
                return '错误: task 为空'
            return await ctx.loop.request_tool(
                ctx.group_id, task, str(args.get('reason', '') or ''),
                ctx.sender_id, ctx.sender_name, ctx.bot, ctx.cfg)

        if name == 'loop_finish':
            if ctx.loop_ctx is None:
                return ('错误: 当前不在长跑模式中。只有收到长跑任务通知后的'
                        '连续工作期间才能用它收尾；普通对话直接正常回复即可。')
            summary = str(args.get('summary', '') or '').strip()
            if not summary:
                return '错误: summary 为空，说说你完成了什么'
            ctx.loop_ctx['finished'] = True
            ctx.loop_ctx['finish_text'] = summary[:2000]
            return '已收到你的收尾汇报，长跑将在本轮结束后停止。'

        if name == 'recall_user':
            if ctx.memory is None:
                return '记忆功能未启用'
            return await ctx.memory.recall_user_tool(
                ctx.group_id, str(args.get('name', '')),
                str(args.get('keyword', '') or ''), ctx.cfg)

        if name == 'remember_user':
            if ctx.memory is None:
                return '记忆功能未启用'
            fact = str(args.get('fact', '')).strip()
            nm = str(args.get('name', '')).strip()
            if not nm or not fact:
                return '错误: name 和 fact 必填'
            return await ctx.memory.remember_user_tool(
                ctx.group_id, nm, fact, ctx.cfg)

        if name == 'recall_self':
            if ctx.self_memory is None:
                return '自我记忆功能未启用'
            return await ctx.self_memory.recall_self_tool(
                str(args.get('keyword', '') or ''), ctx.cfg)

        if name == 'remember_self':
            if ctx.self_memory is None:
                return '自我记忆功能未启用'
            statement = str(args.get('statement', '')).strip()
            if not statement:
                return '错误: statement 必填'
            return await ctx.self_memory.remember_self_tool(
                ctx.group_id, statement, ctx.cfg)

        if name in ('task_add', 'task_list', 'task_close'):
            if ctx.tasks is None:
                return '约定/待办功能未启用'
            if name == 'task_add':
                return await ctx.tasks.add_task_tool(
                    ctx.group_id, str(args.get('owner', '')),
                    str(args.get('content', '')),
                    kind='topic' if str(args.get('kind')) == 'topic' else 'task',
                    due_text=str(args.get('due_text', '')),
                    due_date=str(args.get('due_date', '')),
                    sender_id=ctx.sender_id, sender_name=ctx.sender_name)
            if name == 'task_list':
                return await ctx.tasks.list_tasks_tool(
                    ctx.group_id, str(args.get('owner', '')),
                    sender_id=ctx.sender_id)
            return await ctx.tasks.close_task_tool(args.get('id'))

        if name in ('schedule_add', 'schedule_list', 'schedule_cancel'):
            if ctx.scheduler is None:
                return '定时任务功能未启用'
            if name == 'schedule_add':
                return await ctx.scheduler.add_job_tool(
                    ctx.group_id, ctx.sender_id, ctx.sender_name,
                    target_type=str(args.get('target_type', 'group')),
                    target_id=str(args.get('target_id', '')),
                    mode=str(args.get('mode', 'once')),
                    at=str(args.get('at', '')),
                    time_text=str(args.get('time', '')),
                    weekday=args.get('weekday', 0),
                    brief=str(args.get('brief', '')))
            if name == 'schedule_list':
                return await ctx.scheduler.list_jobs_tool(
                    ctx.group_id, ctx.sender_id)
            return await ctx.scheduler.cancel_job_tool(
                args.get('id'), ctx.sender_id, is_owner=ctx.is_owner)

        if name in ('poll_add', 'poll_list', 'poll_toggle', 'poll_remove'):
            if ctx.proactor is None:
                return '定时轮询功能未启用'
            if name == 'poll_add':
                return await ctx.proactor.add_poll_tool(
                    ctx.group_id, ctx.sender_id, ctx.sender_name,
                    task=str(args.get('task', '')),
                    prob_percent=args.get('prob_percent', 10),
                    every_n=args.get('every_n', 0),
                    active_hours=args.get('active_hours', 12),
                    schedule=str(args.get('schedule', '')),
                    max_polls=int(ctx.cfg.get('polls_max_per_group', 10)))
            if name == 'poll_list':
                return await ctx.proactor.list_polls_tool(ctx.group_id)
            on = bool(args.get('enabled', True))
            if name == 'poll_toggle':
                return await ctx.proactor.toggle_poll_tool(
                    args.get('id'), ctx.sender_id, on,
                    is_owner=ctx.is_owner)
            return await ctx.proactor.remove_poll_tool(
                args.get('id'), ctx.sender_id, is_owner=ctx.is_owner)

        if name in ('watcher_start', 'watcher_list', 'watcher_stop',
                    'watcher_logs'):
            if ctx.watchers is None:
                return '外部监测功能未启用'
            if name == 'watcher_start':
                return await ctx.watchers.start_watcher(
                    ctx.group_id, ctx.sender_id, ctx.sender_name,
                    script=str(args.get('script', '')),
                    args=args.get('args'),
                    name=str(args.get('name', '')))
            if name == 'watcher_list':
                scope = str(args.get('scope', '') or '').strip().lower()
                rows = await ctx.watchers.list_watchers(
                    '' if scope == 'all' else ctx.group_id)
                if not rows:
                    return '当前没有监测程序'
                lines = ['监测程序：']
                for r in rows:
                    state = '运行中' if r.get('alive') else '已停止'
                    nm = f' {r["name"]}' if r.get('name') else ''
                    lines.append(
                        f'- [{r["id"]}]{nm}（{state}，pid={r["pid"]}）'
                        f'群{r["group_id"]}｜{r["script"][:60]}')
                return '\n'.join(lines)[:2000]
            if name == 'watcher_stop':
                return await ctx.watchers.stop_watcher(args.get('id'))
            return await ctx.watchers.logs(
                args.get('id'),
                max_chars=int(ctx.cfg.get('tool_result_max_chars', 2000)))

        if name == 'history_query':
            start_s = str(args.get('start') or '').strip()
            end_s = str(args.get('end') or '').strip()
            try:
                start_ts = history.parse_time(start_s) if start_s else 0
                end_ts = history.parse_time(end_s) if end_s else 0
            except ValueError as e:
                return f'错误: {e}'
            if start_ts and end_ts and start_ts > end_ts:
                return '错误: start 不能晚于 end'
            try:
                limit = int(args.get('limit') or history.DEFAULT_LIMIT)
                offset = max(0, int(args.get('offset') or 0))
            except (TypeError, ValueError):
                return '错误: offset/limit 必须是整数'
            limit = max(1, min(limit, history.MAX_LIMIT))
            rows, total = await asyncio.to_thread(
                storage.query_messages, ctx.db_path, ctx.group_id,
                start_ts=start_ts, end_ts=end_ts, offset=offset,
                limit=limit, include_tools=bool(args.get('include_tools')))
            return history.render_history(
                rows, total, shown_range=bool(start_ts or end_ts),
                offset=offset)

        if name == 'token_stats':
            try:
                days = float(args.get('days', 30) if args.get('days') is not None
                             else 30)
            except (TypeError, ValueError):
                return '错误: days 必须是数字'
            days = max(0.0, min(days, 3650))
            since = time.time() - days * 86400 if days else 0
            bd = await asyncio.to_thread(
                storage.usage_breakdown, ctx.db_path, ctx.group_id, since)
            return _render_usage_breakdown(bd, days)

        if name in ('qzone_publish', 'qzone_delete', 'qzone_recent'):
            if ctx.qzone is None:
                return 'QQ 空间功能未启用'
            if name == 'qzone_publish':
                try:
                    ugc_right = int(args.get('ugc_right', 1) or 1)
                except (TypeError, ValueError):
                    return '错误: ugc_right 必须是数字权限码'
                return await ctx.qzone.publish(
                    args.get('content', ''), ugc_right,
                    args.get('target_uins'),
                    sender_id=ctx.sender_id, sender_name=ctx.sender_name)
            if name == 'qzone_delete':
                return await ctx.qzone.delete(args.get('tid', ''))
            try:
                limit = int(args.get('limit', 10) or 10)
            except (TypeError, ValueError):
                limit = 10
            return ctx.qzone.recent(limit)

        if name == 'read_message_image':
            ref = str(args.get('ref') or args.get('file') or '').strip()
            if (not ref or '..' in ref
                    or not _SAFE_IMG_REF.fullmatch(ref)):
                return ('错误: ref 必须是消息里 [CQ:image,file=...] 中的 file '
                        '值（图片哈希文件名），不要填 URL 或文件路径')
            if ref in ctx.image_seen:
                return '这张图本轮已经给过你了，直接看上面的图即可，不要重复取'
            if ctx.bot is None or not hasattr(ctx.bot, 'call_api'):
                return '错误: 机器人不在线，无法读取消息图片'
            d = await media_in._api_data(ctx.bot, 'get_image', {'file': ref})
            url = d.get('url') or ''
            if not media_in._is_url(url):
                f2 = d.get('file') or ''
                url = f2 if media_in._is_url(f2) else ''
            if not url:
                return '错误: NapCat 取不到该图片地址（图片可能已过期），可请群友重发'
            budget = int(ctx.cfg.get('media_download_max_kb', 10240)) * 1024
            try:
                raw = await media_in._http_get(url, budget)
            except Exception as e:
                return f'错误: 图片下载失败：{e}'
            mime = media_in.sniff_image(raw)
            if mime not in media_in._VISION_OK:
                return f'错误: 不是受支持的图片格式（{mime or "未知"}）'
            ctx.image_seen.add(ref)
            note = f'消息图片 {ref}（{len(raw)} 字节）已取回，下面就是这张图的画面。'
            q = str(args.get('question') or '').strip()
            if q:
                note += f'\n你想确认的问题：{q}'
            return ToolMediaResult(
                text=note,
                images=[(mime, base64.b64encode(raw).decode('ascii'))])

        if name == 'read_forward':
            fid = str(args.get('id') or '').strip()
            if not fid or not _SAFE_FORWARD_ID.fullmatch(fid):
                return '错误: id 必须是消息里 [CQ:forward,id=...] 中的 id，不要填 URL'
            try:
                offset = max(0, int(args.get('offset') or 0))
                limit = int(args.get('limit') or 20)
            except (TypeError, ValueError):
                return '错误: offset/limit 必须是整数'
            limit = max(1, min(limit, 20))

            nodes = ctx.fw_cache.get(fid)
            if nodes is None:
                if ctx.bot is None or not hasattr(ctx.bot, 'call_api'):
                    return '错误: 机器人不在线，无法读取合并转发'
                try:
                    resp = await ctx.bot.call_api(
                        'get_forward_msg', {'message_id': fid})
                except Exception as e:
                    return f'错误: 读取合并转发失败：{e}'
                if (not isinstance(resp, dict) or resp.get('retcode') != 0
                        or resp.get('status') not in ('ok', 'async')):
                    why = ''
                    if isinstance(resp, dict):
                        why = str(resp.get('wording') or resp.get('msg')
                                  or resp.get('message') or '')[:120]
                    return (f'错误: 合并转发读取失败{"：" + why if why else ""}。'
                            '通常是转发已过期或被撤回，请群友重新转发一次。')
                raw_nodes = (resp.get('data') or {}).get('messages')
                nodes = message_mod.normalize_forward_nodes(raw_nodes)
                ctx.fw_cache[fid] = nodes

            total = len(nodes)
            if total == 0:
                return '合并转发里没有可展示的消息（可能已过期），请群友重新转发一次。'
            if offset >= total:
                return f'错误: offset={offset} 超出总条数 {total}'
            lines = [f'合并转发共 {total} 条：']
            used, shown = len(lines[0]), 0
            for nd in nodes[offset:offset + limit]:
                name = nd['nickname'] or (
                    f'QQ:{nd["user_id"]}' if nd['user_id'] else '群友')
                body = nd['text'] or '[空消息]'
                if len(body) > 400:
                    body = body[:400] + '…'
                line = (f'{offset + shown + 1}. '
                        f'{ctx_mod.time_tag(nd["time"])}{name}: {body}')
                if shown and used + len(line) + 1 > 1800:
                    break
                lines.append(line)
                used += len(line) + 1
                shown += 1
            a, b = offset + 1, offset + shown
            if b < total:
                lines.append(
                    f'第 {a}-{b} 条 / 共 {total} 条；继续看请用 '
                    f'read_forward(id={fid}, offset={b})')
            else:
                lines.append(f'第 {a}-{b} 条 / 共 {total} 条（已全部展示）')
            return '\n'.join(lines)

        if name == 'meme_lookup':
            if ctx.meme_guide is None:
                return '错误: 梗指南功能不可用'
            query = str(args.get('query', '') or '').strip()
            if not query:
                return '错误: query 为空'
            return await asyncio.to_thread(
                ctx.meme_guide.lookup_text, query, ctx.cfg)

        if name == 'now_time':
            now = datetime.datetime.now()
            week = '一二三四五六日'[now.weekday()]
            return (f'当前本地时间：{now.strftime("%Y-%m-%d %H:%M:%S")} '
                    f'星期{week}（UNIX {int(now.timestamp())}）')

        return f'错误: 未知内置工具 {name}'
    except Exception as e:
        if ctx.log:
            ctx.log(f'内置工具 {name} 异常: {e}')
        return f'错误: {e}'
