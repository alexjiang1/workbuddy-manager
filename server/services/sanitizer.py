"""Prompt 脱敏与指纹净化服务。

职责：
  在出站请求发往上游前，对消息内容进行安全脱敏与客户端指纹净化：
  1. 剥离 Codex / Agent 特征标签与系统元数据（去皮留肉，保留实际任务目标与内容）；
  2. 伪装多模型交接与上下文压缩提示词（转换为标准中性提示词）；
  3. 净化技能文件的物理绝对路径（抹除本地用户名与隐藏目录）；
  4. 遮蔽高危生产凭据（App Store Connect ID 等）；
  5. 兼容上游 11128 及旧版 Claude Code 反探测规则。

性能设计：
  - 常驻预编译正则；
  - 基于 C 级别 substring 快速预检（未命中特征即零拷贝直接返回）；
  - 单次大文本清洗耗时 < 1ms，对整体中转反代速度零影响。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger('workbuddy.sanitizer')

# ── 快速预检特征词表（任一命中才进入深度正则改写）──
_FAST_TRIGGER_KEYWORDS: tuple[str, ...] = (
    '<codex_internal_context',
    '</codex_internal_context>',
    'CONTEXT CHECKPOINT COMPACTION',
    'Another language model started to solve this problem',
    'x-anthropic-billing-header',
    'cc_entrypoint=',
    'You are Claude Code',
    'Main branch (',
    'You are a coding agent running in the Codex CLI',
    'github.com/anthropics/',
    '11128',
    'ASC App ID',
    '.codex/skills',
)

# ── 正则预编译 ──
# 1. 标签伪装（剥离外壳，保留里面核心任务目标）
_RE_CODEX_TAG_START = re.compile(r'<codex_internal_context[^>]*>')
_RE_CODEX_TAG_END = re.compile(r'</codex_internal_context>')
_RE_OBJECTIVE_TAG_START = re.compile(r'<objective>')
_RE_OBJECTIVE_TAG_END = re.compile(r'</objective>')

# 2. 技能绝对路径伪装：[$name](/Users/xxx/.codex/skills/name/SKILL.md) -> [$name](skills/name/SKILL.md)
_RE_SKILL_PATH = re.compile(r'\[\$([a-zA-Z0-9_\-]+)\]\(/Users/[^/]+/\.codex/skills/([^/]+)/SKILL\.md\)')

# 3. 多模型交接与压缩提示词中性化
_RE_ANOTHER_LLM = re.compile(
    r'Another language model started to solve this problem and produced a summary of its thinking process[^\n]*\n*'
    r'(?:You also have access to the state of the tools that were used by that language model[^\n]*\n*)?'
    r'(?:Use this to build on the work that has already been done and avoid duplicating work[^\n]*\n*)?'
    r'(?:Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:)?',
    re.IGNORECASE
)

_RE_CHECKPOINT_COMPACTION = re.compile(
    r'You are performing a CONTEXT CHECKPOINT COMPACTION\.',
    re.IGNORECASE
)

_RE_HANDOFF_SUMMARY_PROMPT = re.compile(
    r'Create a handoff summary for another LLM that will resume the task\.',
    re.IGNORECASE
)

# 4. 高危开发者生产凭据遮蔽（如 App Store Connect ID，防止后台日志撞库）
_RE_ASC_APP_ID = re.compile(r'(ASC App ID\s*=\s*)\d{6,12}')

# 5. 上游 11128 绕过与旧版 Claude Code 指纹剥离（与上游保持兼容）
_RE_ANTHROPIC_BILLING = re.compile(r'(?i)x-anthropic-billing-header:[^;\n]*;?\s*')
_RE_ANTHROPIC_BARE_HDR = re.compile(r'(?i)x-anthropic-billing-header')
_RE_CC_KV = re.compile(r'(?i)\bcc_[a-z0-9_]+=[^;\n]*;?\s*')

_EXACT_REWRITES: tuple[tuple[str, str], ...] = (
    ("You are Claude Code, Anthropic's official CLI for Claude", "You are Claude Code, Anthropic's official CLI tool for Claude"),
    ("Main branch (you will usually use this for PRs)", "Default branch (you will usually use this for PRs)"),
    ("You are a coding agent running in the Codex CLI, a terminal-based coding assistant.", "You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant."),
    ("To give feedback, users should report the issue at https://github.com/anthropics/claude-code/issues", "To provide feedback, users should report the issue at https://github.com/anthropics/claude-code/issues"),
    ("11128", "11-128"),
)


def has_sensitive_fingerprint(text: str) -> bool:
    """快速特征预检：C 级别子串匹配，命中任意一项才进入清洗流程。"""
    for kw in _FAST_TRIGGER_KEYWORDS:
        if kw in text:
            return True
    return bool(_RE_ANTHROPIC_BARE_HDR.search(text))


def sanitize_text(text: str) -> str:
    """清洗单段文本。

    遵循「去皮留肉」与「语义保留型伪装」原则：
    - 不删除任务正文与用户指令，仅将平台特定的 XML 壳子和固定模板句中性化；
    - 不篡改项目的代码相对路径；
    - 快速路径下零分配返回原串。
    """
    if not text or not has_sensitive_fingerprint(text):
        return text

    # 1. 剥离 Codex 专有系统标签壳（保留其内部任务与目标正文）
    if '<codex_internal_context' in text or '</codex_internal_context>' in text:
        text = _RE_CODEX_TAG_START.sub('### Task Context & Objective\n', text)
        text = _RE_CODEX_TAG_END.sub('', text)

    if '<objective>' in text or '</objective>' in text:
        text = _RE_OBJECTIVE_TAG_START.sub('\n**Task Goal:**\n', text)
        text = _RE_OBJECTIVE_TAG_END.sub('\n', text)

    # 2. 技能引用绝对路径中性化（抹除 /Users/<username>/.codex/skills/ 物理路径）
    if '.codex/skills' in text:
        text = _RE_SKILL_PATH.sub(r'[$\1](skills/\2/SKILL.md)', text)

    # 3. 多模型交接与压缩提示词中性化
    if 'Another language model' in text:
        text = _RE_ANOTHER_LLM.sub(
            'The previous phase of this task produced the following summary and context. '
            'Use this information to continue the work seamlessly:\n',
            text
        )

    if 'CONTEXT CHECKPOINT COMPACTION' in text:
        text = _RE_CHECKPOINT_COMPACTION.sub(
            'Please create a structured task summary and context checkpoint.',
            text
        )

    if 'Create a handoff summary for another LLM' in text:
        text = _RE_HANDOFF_SUMMARY_PROMPT.sub(
            'Create a concise handoff summary for resuming the task.',
            text
        )

    # 4. 生产敏感 ID 遮蔽
    if 'ASC App ID' in text:
        text = _RE_ASC_APP_ID.sub(r'\1[REDACTED_APP_ID]', text)

    # 5. 上游 11128 与 Claude Code 逐字匹配兼容层
    for src, dst in _EXACT_REWRITES:
        if src in text:
            text = text.replace(src, dst)

    if 'x-anthropic-billing-header' in text.lower():
        text = _RE_ANTHROPIC_BILLING.sub('', text)
        text = _RE_ANTHROPIC_BARE_HDR.sub('x-anthropic-billing-hdr', text)

    if 'cc_' in text:
        text = _RE_CC_KV.sub('', text)

    return text


def sanitize_content_field(content: Any) -> tuple[Any, bool]:
    """清洗 message.content 字段（支持纯文本与多模态数组）。"""
    if isinstance(content, str):
        cleaned = sanitize_text(content)
        return cleaned, cleaned != content

    if isinstance(content, list):
        changed = False
        new_list = []
        for part in content:
            if isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str):
                orig_text = part['text']
                cleaned_text = sanitize_text(orig_text)
                if cleaned_text != orig_text:
                    new_list.append({**part, 'text': cleaned_text})
                    changed = True
                else:
                    new_list.append(part)
            else:
                new_list.append(part)
        return new_list, changed

    return content, False


def sanitize_tool_calls(tool_calls: Any) -> bool:
    """清洗 assistant.tool_calls 内部参数字符串（JSON 文本）。"""
    if not isinstance(tool_calls, list):
        return False

    changed = False
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        fn = call.get('function')
        if not isinstance(fn, dict):
            continue
        args = fn.get('arguments')
        if isinstance(args, str) and has_sensitive_fingerprint(args):
            fn['arguments'] = sanitize_text(args)
            changed = True

    return changed


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """对整个请求 Payload（messages、system、prompt 等）进行安全净化。"""
    if not isinstance(payload, dict):
        return payload

    messages = payload.get('messages')
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue

            # content
            if 'content' in msg:
                new_content, changed = sanitize_content_field(msg['content'])
                if changed:
                    msg['content'] = new_content

            # reasoning_content
            if 'reasoning_content' in msg and isinstance(msg['reasoning_content'], str):
                msg['reasoning_content'] = sanitize_text(msg['reasoning_content'])

            # tool_calls
            if 'tool_calls' in msg:
                sanitize_tool_calls(msg['tool_calls'])

    # 兼容部分客户端放在顶层的 prompt 或 system 字段
    if 'prompt' in payload and isinstance(payload['prompt'], str):
        payload['prompt'] = sanitize_text(payload['prompt'])

    if 'system' in payload and isinstance(payload['system'], str):
        payload['system'] = sanitize_text(payload['system'])

    return payload
