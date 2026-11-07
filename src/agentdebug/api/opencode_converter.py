"""OpenCode 2 session -> AgentDebugX ``AgentTrajectory`` conversion.

Mirrors the intranet converter (see issue #333): the two-level
message/part structure is flattened into one event stream, each part becoming
one event (a ``tool`` part becomes ``TOOL_CALL`` + ``TOOL_RESULT``), with a
deterministic ``event_id`` of ``{trace_id}_step{step_index}`` so reports can be
traced back to the trajectory across files.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from agentdebug.api.db_adapter.v2_adapter import SessionRecord
from agentdebug.api.sanitize import (
    is_synthetic_client_tool_continuation,
    unwrap_user_prompt,
    visible_text,
)
from agentdebug.schema import AgentEvent, AgentTrajectory, EventType

WIRE_EVENT_TYPES = {
    EventType.PLAN.value: 'PLAN',
    EventType.AGENT_STEP.value: 'AGENT_STEP',
    EventType.REFLECTION.value: 'REFLECTION',
    EventType.TOOL_CALL.value: 'TOOL_CALL',
    EventType.TOOL_RESULT.value: 'TOOL_RESULT',
}

_TOOL_RESULT_STATUSES = ('completed', 'error')

_USER = 'user'
_ASSISTANT = 'assistant'
_AGENT_STEP_PARTS = ('step-start', 'step-finish')
_GOAL_MAX_CHARS = 2000
_GOAL_FOLLOWUP_CHARS = 300


def convert_session(session: SessionRecord) -> AgentTrajectory:
    """Flatten a session record into a trajectory (issue #333 sections 3-5)."""
    trajectory = AgentTrajectory(
        trace_id=session.session_id,
        task_id=session.project_id,
        goal=_build_goal(session.messages),
        framework='opencode',
        metadata={
            'source': 'opencode_session',
            'slug': session.slug,
            'directory': session.directory,
            'version': session.version,
        },
    )
    step_index = 0
    for message_idx, message in enumerate(session.messages):
        step_index = _convert_message(trajectory, session, message, message_idx, step_index)
    return trajectory


def _build_goal(messages: list) -> Optional[str]:
    """Use the user's own words as the goal (issue #3, P3).

    The session title is often auto-generated and loses constraints. The first
    real user message is kept in full; later user turns are appended as
    ``补充或修改要求 #n`` using their first 300 characters. Synthetic client
    tool continuations are skipped. Returns ``None`` when the session has no
    real user message.
    """

    user_texts = []
    for message in messages:
        if message.get('role') != _USER:
            continue
        text = _user_visible_text(message.get('data') or {})
        if not text or is_synthetic_client_tool_continuation(text):
            continue
        user_texts.append(text)
    if not user_texts:
        return None
    parts = [user_texts[0]]
    for index, text in enumerate(user_texts[1:], start=1):
        parts.append(
            '补充或修改要求 #%d：%s' % (index, text[:_GOAL_FOLLOWUP_CHARS])
        )
    goal = '\n'.join(parts)
    if len(goal) > _GOAL_MAX_CHARS:
        goal = goal[:_GOAL_MAX_CHARS - 1] + '…'
    return goal


def trajectory_to_wire(trajectory: AgentTrajectory) -> Dict[str, Any]:
    """Serialize a trajectory into the diagnosis API response shape."""
    return {
        'trace_id': trajectory.trace_id,
        'goal': trajectory.goal,
        'framework': trajectory.framework,
        'task_id': trajectory.task_id,
        'metadata': dict(trajectory.metadata or {}),
        'events': [_event_to_wire(event) for event in trajectory.events],
    }


def _convert_message(
    trajectory: AgentTrajectory,
    session: SessionRecord,
    message: Dict[str, Any],
    message_idx: int,
    step_index: int,
) -> int:
    if message.get('role') == _USER:
        return _convert_user_message(trajectory, message, message_idx, step_index)
    return _convert_assistant_message(trajectory, session, message, message_idx, step_index)


def _convert_user_message(
    trajectory: AgentTrajectory,
    message: Dict[str, Any],
    message_idx: int,
    step_index: int,
) -> int:
    text = _user_visible_text(message.get('data') or {})
    if not text or is_synthetic_client_tool_continuation(text):
        return step_index
    step_index += 1
    _append_event(
        trajectory,
        agent_name=_USER,
        event_type=EventType.PLAN,
        step_index=step_index,
        output=text,
        metadata=_event_metadata('text', message['id'], message_idx, 0, _USER),
    )
    return step_index


def _convert_assistant_message(
    trajectory: AgentTrajectory,
    session: SessionRecord,
    message: Dict[str, Any],
    message_idx: int,
    step_index: int,
) -> int:
    data = message.get('data') or {}
    agent_name = str(data.get('agent') or session.agent or 'agent')
    parts = data.get('content')
    if not isinstance(parts, list):
        return step_index
    for part_idx, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        part_type = part.get('type')
        if part_type in ('reasoning', 'text'):
            step_index = _append_text_event(
                trajectory, agent_name, message, message_idx, part, part_idx, step_index
            )
        elif part_type == 'tool':
            step_index = _append_tool_events(
                trajectory, agent_name, message, message_idx, part, part_idx, step_index
            )
        elif part_type in _AGENT_STEP_PARTS:
            step_index = _append_step_event(
                trajectory, agent_name, message, message_idx, part, part_idx, step_index
            )
    return step_index


def _append_text_event(
    trajectory: AgentTrajectory,
    agent_name: str,
    message: Dict[str, Any],
    message_idx: int,
    part: Dict[str, Any],
    part_idx: int,
    step_index: int,
) -> int:
    text = visible_text(str(part.get('text') or ''))
    if not text.strip():
        return step_index
    is_reasoning = part.get('type') == 'reasoning'
    step_index += 1
    _append_event(
        trajectory,
        agent_name=agent_name,
        event_type=EventType.REFLECTION if is_reasoning else EventType.AGENT_STEP,
        step_index=step_index,
        output=text,
        metadata=_event_metadata(
            str(part.get('type')), message['id'], message_idx, part_idx, _ASSISTANT
        ),
    )
    return step_index


def _append_tool_events(
    trajectory: AgentTrajectory,
    agent_name: str,
    message: Dict[str, Any],
    message_idx: int,
    part: Dict[str, Any],
    part_idx: int,
    step_index: int,
) -> int:
    state = part.get('state') if isinstance(part.get('state'), dict) else {}
    status = str(state.get('status') or '')
    call_id = str(part.get('id') or '')
    metadata = _event_metadata('tool', message['id'], message_idx, part_idx, _ASSISTANT)
    metadata.update({
        'tool': str(part.get('name') or ''),
        'tool_status': status,
        'callID': call_id,
    })

    tool_input = state.get('input')
    step_index += 1
    _append_event(
        trajectory,
        agent_name=agent_name,
        event_type=EventType.TOOL_CALL,
        step_index=step_index,
        input=json.dumps(tool_input, ensure_ascii=False) if tool_input is not None else None,
        # The intranet sample carries the tool call id in ``output``.
        output=call_id or None,
        metadata=dict(metadata),
    )
    if status not in _TOOL_RESULT_STATUSES:
        return step_index

    output, error = _tool_result(state)
    step_index += 1
    _append_event(
        trajectory,
        agent_name=agent_name,
        event_type=EventType.TOOL_RESULT,
        step_index=step_index,
        output=output,
        error=error,
        metadata=dict(metadata),
    )
    return step_index


def _append_step_event(
    trajectory: AgentTrajectory,
    agent_name: str,
    message: Dict[str, Any],
    message_idx: int,
    part: Dict[str, Any],
    part_idx: int,
    step_index: int,
) -> int:
    metadata = _event_metadata(
        str(part.get('type')), message['id'], message_idx, part_idx, _ASSISTANT
    )
    finish_reason = part.get('finishReason') or part.get('finish_reason') or part.get('reason')
    if finish_reason:
        metadata['finish_reason'] = str(finish_reason)
    step_index += 1
    _append_event(
        trajectory,
        agent_name=agent_name,
        event_type=EventType.AGENT_STEP,
        step_index=step_index,
        metadata=metadata,
    )
    return step_index


def _append_event(
    trajectory: AgentTrajectory,
    *,
    agent_name: str,
    event_type: EventType,
    step_index: int,
    metadata: Dict[str, Any],
    input: Any = None,
    output: Any = None,
    error: Optional[str] = None,
) -> None:
    trajectory.add_event(AgentEvent(
        event_id='%s_step%d' % (trajectory.trace_id, step_index),
        trace_id=trajectory.trace_id,
        agent_name=agent_name,
        event_type=event_type,
        step_index=step_index,
        input=input,
        output=output,
        error=error,
        metadata=metadata,
    ))


def _user_visible_text(data: Dict[str, Any]) -> str:
    texts = []
    parts = data.get('content')
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and part.get('type') == 'text':
                candidate = str(part.get('text') or '')
                if candidate:
                    texts.append(candidate)
    if texts:
        return visible_text('\n'.join(texts))
    return visible_text(unwrap_user_prompt(str(data.get('text') or '')))


def _tool_result(state: Dict[str, Any]) -> tuple:
    """Return ``(output, error)`` for a finished tool call."""
    error = state.get('error')
    if isinstance(error, dict):
        message = visible_text(str(error.get('message') or ''))
        return None, message or None
    texts = [
        str(item.get('text') or '')
        for item in state.get('content') or []
        if isinstance(item, dict) and item.get('type') == 'text'
    ]
    if texts:
        return '\n'.join(texts), None
    structured = state.get('structured')
    if isinstance(structured, dict) and structured:
        return json.dumps(structured, ensure_ascii=False), None
    return None, None


def _event_metadata(
    part_type: str,
    message_id: str,
    message_idx: int,
    part_idx: int,
    role: str,
) -> Dict[str, Any]:
    return {
        'part_type': part_type,
        'message_id': message_id,
        'message_idx': message_idx,
        'part_idx': part_idx,
        'role': role,
    }


def _event_to_wire(event: AgentEvent) -> Dict[str, Any]:
    wire = {
        'event_id': event.event_id,
        'trace_id': event.trace_id,
        'agent_name': event.agent_name,
        'event_type': _wire_event_type(event.event_type),
        'step_index': event.step_index,
    }
    if event.input is not None:
        wire['input'] = event.input
    if event.output is not None:
        wire['output'] = event.output
    if event.error is not None:
        wire['error'] = event.error
    wire['metadata'] = dict(event.metadata or {})
    return wire


def _wire_event_type(event_type: str) -> str:
    return WIRE_EVENT_TYPES.get(str(event_type), str(event_type).upper().replace('.', '_'))
