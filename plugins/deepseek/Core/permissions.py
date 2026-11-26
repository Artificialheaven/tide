# -*- coding: utf-8 -*-
"""N-02 权限引擎：allow / ask / deny 三级决策。

设计对齐 docs/agent-framework-enhancement-plan-2026-11-25.md §3：
- 规则粒度：tool(+glob) / scope / role / args_match，同粒度 deny 优先；
- 角色默认（owner/admin/member_action）：只约束**风险工具**。安全/只读工具
  在非主人手里仍默认放行，与 N-02 升级前的 risky_owner_only 语义一致
  （否则普通成员连 fs.read / history.query 都会被 member_action=deny 拦掉）；
- 角色 readonly：子代理等"只读身份"，只有只读工具白名单放行，写/发送一律拒绝；
- max_per_day 超限时 allow 降级为 ask；
- 无 permissions 配置时行为等价于旧的 risky_owner_only 二值门禁。

本模块不做 IO：计数通过 count_today 回调注入，便于单测与解耦。
"""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch

ALLOW, ASK, DENY = 'allow', 'ask', 'deny'


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str = ''
    rule: str = ''


class PermissionEngine:
    def __init__(self, cfg: dict | None = None, count_today=None):
        self.cfg = cfg or {}
        self.permissions = dict(self.cfg.get('permissions') or {})
        self.count_today = count_today

    def refresh(self, cfg: dict):
        """热更新：除了换 cfg 引用，必须重建规则快照。

        decide() 读的是 self.permissions；只换 self.cfg 会让运行中修改的
        权限规则/默认动作在插件重建前全部失效。
        """
        self.cfg = cfg or {}
        self.permissions = dict(self.cfg.get('permissions') or {})

    # ── 规则匹配 ──────────────────────────────────────────────

    @staticmethod
    def _tool_match(pattern: str, tool_key: str) -> bool:
        pattern = str(pattern or '')
        if not pattern:
            return True
        if any(ch in pattern for ch in '*?['):
            return fnmatch(tool_key, pattern)
        return tool_key == pattern

    def _args_match(self, rule: dict, args: dict) -> bool:
        match = rule.get('args_match') or {}
        if not match:
            return True
        for key, expected in match.items():
            if key == 'path_prefix':
                val = ''
                for k in ('path', 'file', 'target'):
                    if args.get(k):
                        val = str(args.get(k))
                        break
                if not val or not val.strip().replace('\\', '/').startswith(
                        str(expected).strip().replace('\\', '/')):
                    return False
            elif key == 'equals':
                for ak, av in (expected or {}).items():
                    if str(args.get(ak)) != str(av):
                        return False
            else:
                # 未知匹配键按不匹配处理（保守）
                return False
        return True

    def _rule_hit(self, rule: dict, *, tool_key: str, args: dict,
                  role: str, gid: str) -> bool:
        if not self._tool_match(rule.get('tool'), tool_key):
            return False
        scope = str(rule.get('scope') or '')
        if scope and scope not in ('all', '*') and str(gid) != scope:
            return False
        rrole = str(rule.get('role') or '')
        if rrole and rrole != role:
            return False
        return self._args_match(rule, args or {})

    # ── 决策 ─────────────────────────────────────────────────

    def decide(self, *, tool_key: str, args: dict, actor_role: str,
               gid: str, chat_type: str = 'group') -> Decision:
        perm = self.permissions
        rules = list(perm.get('rules') or [])
        role = actor_role or 'member'
        if role not in ('owner', 'admin', 'member', 'readonly'):
            role = 'member'

        if not perm:
            # 旧配置：完全沿用 risky_owner_only 语义
            return self._legacy(tool_key, role)

        # 1) deny
        for rule in rules:
            action = str(rule.get('action') or '').lower()
            if action not in (ALLOW, ASK, DENY):
                continue
            if action != DENY:
                continue
            if self._rule_hit(rule, tool_key=tool_key, args=args,
                              role=role, gid=gid):
                return Decision(DENY, str(rule.get('reason') or '命中拒绝规则'),
                                self._rule_name(rule))

        # 2) allow（max_per_day 超限 → 降级 ask）
        for rule in rules:
            action = str(rule.get('action') or '').lower()
            if action != ALLOW:
                continue
            if self._rule_hit(rule, tool_key=tool_key, args=args,
                              role=role, gid=gid):
                limit = int(rule.get('max_per_day') or 0)
                if limit > 0 and self.count_today is not None:
                    try:
                        used = int(self.count_today(tool_key, args) or 0)
                    except Exception:
                        used = 0
                    if used >= limit:
                        return Decision(ASK, '超出每日配额，转人工审批',
                                        self._rule_name(rule))
                return Decision(ALLOW, '', self._rule_name(rule))

        # 3) ask
        for rule in rules:
            action = str(rule.get('action') or '').lower()
            if action != ASK:
                continue
            if self._rule_hit(rule, tool_key=tool_key, args=args,
                              role=role, gid=gid):
                if not (perm.get('ask') or {}).get('enabled', False):
                    return Decision(DENY, '该操作需要审批，但审批中心未开启',
                                    self._rule_name(rule))
                return Decision(ASK, str(rule.get('reason') or '需要主人审批'),
                                self._rule_name(rule))

        # 3.5) 只读身份（子代理）：白名单内放行，其余一律拒绝。
        #      显式 deny/allow/ask 规则已在上面的步骤里命中，仍然优先。
        #      注意：AI 自主执行模式（3.6）也不能让子代理变成写手——子代理
        #      的额外工具由 subagent_extra_tools 单独授权。
        if role == 'readonly':
            if self.is_readonly_tool(tool_key):
                return Decision(ALLOW, '只读身份，允许只读工具')
            return Decision(DENY, '只读身份只能调用只读工具')

        # 3.6) AI 自主执行模式（privileged_tools_on）：风险工具不再要求
        #      "主人本人指令"。显式 deny 与 ask 规则已在上面的步骤命中，仍然
        #      优先；只读身份仍走 3.5，不受影响。
        if self.cfg.get('privileged_tools_on'):
            return Decision(ALLOW, 'AI 自主执行模式已开启')

        # 4) 角色默认（仅风险工具）
        #    安全工具不按角色默认拒绝：member_action=deny 的本意是"非主人
        #    不许动会写/外发的工具"，把它套到 fs.read 上会让普通成员与
        #    子代理彻底没有工具可用（N-02 升级后的真实回归）。要拦安全
        #    工具请写显式 deny 规则。
        key = f'{role}_action'
        default_action = str(perm.get(key) or '')
        if default_action in (ALLOW, ASK, DENY):
            if default_action == DENY and role != 'owner' and \
                    not self._is_risky(tool_key):
                return Decision(ALLOW, '角色默认只约束风险工具')
            if default_action == ASK:
                if not (perm.get('ask') or {}).get('enabled', False):
                    return Decision(DENY, '需要审批，但审批中心未开启')
                return Decision(ASK, '按角色策略需要审批')
            return Decision(default_action)

        # 5) 工具风险度 / default_action 兜底
        default_action = str(perm.get('default_action') or '')
        if default_action in (ALLOW, ASK, DENY):
            if default_action == ASK and \
                    not (perm.get('ask') or {}).get('enabled', False):
                return Decision(DENY, '需要审批，但审批中心未开启')
            return Decision(default_action, '按默认策略')
        if self._is_risky(tool_key):
            return Decision(DENY, '风险工具默认拒绝')
        return Decision(ALLOW)

    def _legacy(self, tool_key: str, role: str) -> Decision:
        if role == 'owner':
            return Decision(ALLOW)
        if not self.cfg.get('risky_owner_only', True):
            return Decision(ALLOW)
        if self._is_risky(tool_key):
            return Decision(DENY, '该操作仅限主人')
        return Decision(ALLOW)

    @staticmethod
    def is_readonly_tool(tool_key: str) -> bool:
        """是否只读工具（内部 key 或模型函数名都接受）。"""
        key = str(tool_key or '')
        try:
            from . import builtin_tools
            from .tool_batch import READONLY_TOOL_KEYS
            key = str(builtin_tools.TOOL_KEYS.get(key, key))
            return key in READONLY_TOOL_KEYS
        except Exception:
            return False

    @staticmethod
    def _is_risky(tool_key: str) -> bool:
        try:
            from . import builtin_tools
            key = tool_key
            if tool_key in builtin_tools.TOOL_KEYS:
                key = builtin_tools.TOOL_KEYS[tool_key]
            if key in set(builtin_tools.TOOL_KEYS.values()):
                return key in builtin_tools.RISKY_TOOL_KEYS
        except Exception:
            pass
        return str(tool_key).startswith('mcp__')

    @staticmethod
    def _rule_name(rule: dict) -> str:
        return str(rule.get('name') or rule.get('tool') or 'rule')
