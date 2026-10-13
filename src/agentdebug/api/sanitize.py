"""Strip DataAgent prompt envelopes from OpenCode histories.

Python port of the Gateway's ``InternalProtocolSanitizer`` and
``ThreadMessagesService#unwrapUserPrompt`` so the diagnosis service sees the
same visible text the user saw, not the transport instructions around it.
"""

from __future__ import annotations

import re

TAGS = (
    'client_tools',
    'server_tools',
    'environment',
    'attachments',
    'user_message',
    'delivery_execution',
    'forked_context',
    'project_resources_json',
    'project_asset_operation_json',
)

_TAG_ALTERNATION = '|'.join(TAGS)
_COMPLETE_BLOCK = re.compile(
    r'<(%s)\b[^>]*>.*?</\1\s*>' % _TAG_ALTERNATION,
    re.IGNORECASE | re.DOTALL,
)
_UNCLOSED_BLOCK = re.compile(
    r'<(%s)\b[^>]*>.*$' % _TAG_ALTERNATION,
    re.IGNORECASE | re.DOTALL,
)
_ENVIRONMENT_BLOCK = re.compile(r'(?s)<environment>.*?</environment>', re.IGNORECASE)
_USER_MESSAGE_BLOCK = re.compile(r'(?s)<user_message>\s*(.*?)\s*</user_message>', re.IGNORECASE)
_BLANK_RUN = re.compile(r'(?m)^[ \t]+$')

_CLIENT_TOOL_CONTINUATION_MARKER = '[client tool result]'


def visible_text(text: str) -> str:
    """Remove internal protocol blocks that are not part of the visible chat."""
    if not text:
        return ''
    if not _might_contain_internal_tag(text):
        return text
    sanitized = _COMPLETE_BLOCK.sub('', text)
    sanitized = _UNCLOSED_BLOCK.sub('', sanitized)
    return _BLANK_RUN.sub('', sanitized).strip()


def unwrap_user_prompt(text: str) -> str:
    """Peel the ``<environment>`` wrapper and keep only ``<user_message>``."""
    if not text:
        return ''
    unwrapped = _ENVIRONMENT_BLOCK.sub('', text).strip()
    match = _USER_MESSAGE_BLOCK.search(unwrapped)
    if match:
        unwrapped = match.group(1)
    return unwrapped.strip()


def is_synthetic_client_tool_continuation(text: str) -> bool:
    """Detect client-tool relay messages that are not real user turns."""
    if not text:
        return False
    normalized = text.lstrip().lower()
    if normalized.startswith(_CLIENT_TOOL_CONTINUATION_MARKER):
        return True
    offset = normalized.find(_CLIENT_TOOL_CONTINUATION_MARKER)
    return offset >= 0 and normalized.startswith('<client_tools')


def _might_contain_internal_tag(text: str) -> bool:
    lowered = text.lower()
    return any('<' + tag in lowered for tag in TAGS)
