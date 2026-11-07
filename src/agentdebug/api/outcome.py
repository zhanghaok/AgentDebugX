"""Deterministic result signals for an OpenCode session (issue #3, P3).

The judge and attributor must not assume a session failed. This module reads
the trajectory and extracts the signals the intranet service uses before any
model call:

* user rejection (``不对``, ``重新``, ... in a user turn),
* an unrecovered failed tool call among the last five before the final
  assistant answer,
* a failed tool call that was followed by a successful tool call before the
  final answer (recovered, so it does not mark the run failed),
* an empty SQL result,
* no wrap-up (the run ends on a tool result, an error or an aborted finish),
* normal end (an assistant text answer and none of the above).

The output is deliberately mechanical: every field points at an event and a
quoted span, so a report can show *why* it concluded the task failed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from agentdebug.schema import AgentEvent, AgentTrajectory, EventType

DEFAULT_REJECTION_TERMS = (
    '不对',
    '错了',
    '有问题',
    '重新',
    '不是我要的',
    '重跑',
)
DEFAULT_EMPTY_MARKERS = (
    '0 rows',
    '0 row',
    '无数据',
    '共 0 条',
    '空结果',
    'empty result',
    'no data',
    'no rows',
    '没有数据',
)
FAILED_TOOL_STATUSES = frozenset({
    'error',
    'failed',
    'failure',
    'aborted',
    'cancelled',
    'canceled',
    'timeout',
    'timed_out',
})
ABORTED_FINISH_REASONS = frozenset({
    'error',
    'aborted',
    'cancelled',
    'canceled',
    'interrupted',
    'length',
})
SQL_PREFIXES = (
    'select',
    'with',
    'insert',
    'create',
    'update',
    'delete',
    'alter',
    'drop',
    'replace',
    'merge',
)
_LIFECYCLE_PARTS = frozenset({'step-start', 'step-finish'})
_MAX_QUOTE_CHARS = 160


@dataclass
class OutcomeSignals:
    """Deterministic result signals extracted from one trajectory."""

    verdict: str
    user_rejection: List[Dict[str, Any]] = field(default_factory=list)
    trailing_tool_error: Optional[Dict[str, Any]] = None
    recovered_tool_error: Optional[Dict[str, Any]] = None
    empty_result: Optional[Dict[str, Any]] = None
    no_wrapup: Optional[Dict[str, Any]] = None
    normal_end: bool = False

    def to_wire(self) -> Dict[str, Any]:
        basis = [
            name
            for name, present in (
                ('user_rejection', bool(self.user_rejection)),
                ('trailing_tool_error', self.trailing_tool_error is not None),
                ('recovered_tool_error', self.recovered_tool_error is not None),
                ('empty_result', self.empty_result is not None),
                ('no_wrapup', self.no_wrapup is not None),
                ('normal_end', self.normal_end),
            )
            if present
        ]
        return {
            'verdict': self.verdict,
            'basis': basis,
            'user_rejection': {
                'detected': bool(self.user_rejection),
                'positions': list(self.user_rejection),
            },
            'trailing_tool_error': self.trailing_tool_error,
            'recovered_tool_error': self.recovered_tool_error,
            'empty_result': self.empty_result,
            'no_wrapup': self.no_wrapup,
            'normal_end': self.normal_end,
        }


def extract_outcome_signals(trajectory: AgentTrajectory) -> OutcomeSignals:
    """Extract the P3 signals from ``trajectory`` without calling a model."""

    events = list(trajectory.events)
    if not events:
        return OutcomeSignals(
            verdict='ambiguous',
            no_wrapup={'reason': 'empty_trajectory'},
        )

    rejections = _user_rejections(events)
    pairs = _tool_pairs(events)
    last_answer = _last_assistant_text_index(events)
    trailing, recovered = _tool_error_signals(pairs, last_answer)
    empty = _empty_sql_result(pairs)
    no_wrapup = _no_wrapup(events, last_answer)
    normal_end = (
        not rejections
        and trailing is None
        and empty is None
        and no_wrapup is None
        and last_answer is not None
    )
    if rejections or trailing is not None or no_wrapup is not None:
        verdict = 'failed'
    elif normal_end:
        verdict = 'normal'
    else:
        verdict = 'ambiguous'
    return OutcomeSignals(
        verdict=verdict,
        user_rejection=rejections,
        trailing_tool_error=trailing,
        recovered_tool_error=recovered,
        empty_result=empty,
        no_wrapup=no_wrapup,
        normal_end=normal_end,
    )


# -- visible text ---------------------------------------------------------

def _event_type_value(event: AgentEvent) -> str:
    return str(getattr(event.event_type, 'value', event.event_type))


def _metadata(event: AgentEvent) -> Dict[str, Any]:
    value = event.metadata
    return value if isinstance(value, dict) else {}


def _role(event: AgentEvent) -> str:
    return str(_metadata(event).get('role') or '')


def _part_type(event: AgentEvent) -> str:
    return str(_metadata(event).get('part_type') or '')


def _text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _quote(value: Any) -> str:
    text = ' '.join(_text(value).split())
    if len(text) <= _MAX_QUOTE_CHARS:
        return text
    return text[:_MAX_QUOTE_CHARS] + '…'


def _is_user_message(event: AgentEvent) -> bool:
    if _role(event) == 'user' or event.agent_name == 'user':
        return bool(_text(event.output).strip())
    return False


def _is_assistant_text(event: AgentEvent) -> bool:
    role = _role(event)
    if role and role != 'assistant':
        return False
    if _part_type(event) not in ('', 'text'):
        return False
    if _event_type_value(event) not in (
        EventType.AGENT_STEP.value,
        EventType.LLM_RESPONSE.value,
    ):
        return False
    return bool(_text(event.output).strip())


def _is_lifecycle(event: AgentEvent) -> bool:
    return _part_type(event) in _LIFECYCLE_PARTS


def _last_assistant_text_index(events: Sequence[AgentEvent]) -> Optional[int]:
    for index in range(len(events) - 1, -1, -1):
        if _is_assistant_text(events[index]):
            return index
    return None


def _is_assistant_event(event: AgentEvent) -> bool:
    role = _role(event)
    if role:
        return role == 'assistant'
    return _event_type_value(event) in (
        EventType.AGENT_STEP.value,
        EventType.REFLECTION.value,
        EventType.TOOL_CALL.value,
        EventType.TOOL_RESULT.value,
    )


def _user_rejections(events: Sequence[AgentEvent]) -> List[Dict[str, Any]]:
    terms = _configured_terms('AGENTDEBUG_OUTCOME_REJECTION_TERMS', DEFAULT_REJECTION_TERMS)
    found: List[Dict[str, Any]] = []
    assistant_seen = False
    for event in events:
        if _is_assistant_event(event):
            assistant_seen = True
            continue
        # A first user request can contain words like ``重新`` ("rerun this
        # report") without rejecting anything. Only a user turn that follows
        # an assistant turn can be a rejection.
        if not assistant_seen or not _is_user_message(event):
            continue
        text = _text(event.output)
        for term in terms:
            if term and term in text:
                found.append({
                    'event_id': event.event_id,
                    'step_index': event.step_index,
                    'message_id': _metadata(event).get('message_id'),
                    'term': term,
                    'text': _quote(text),
                })
                break
    return found


# -- tool calls -----------------------------------------------------------

def _tool_key(event: AgentEvent) -> Tuple[Any, ...]:
    metadata = _metadata(event)
    call_id = metadata.get('callID')
    if call_id:
        return ('id', str(call_id))
    return ('pos', str(metadata.get('message_id') or ''), metadata.get('part_idx'))


ToolPair = Tuple[int, AgentEvent, Optional[AgentEvent]]


def _tool_pairs(events: Sequence[AgentEvent]) -> List[ToolPair]:
    calls: List[Tuple[int, Tuple[Any, ...], AgentEvent]] = []
    results: Dict[Tuple[Any, ...], AgentEvent] = {}
    for index, event in enumerate(events):
        event_type = _event_type_value(event)
        if event_type == EventType.TOOL_CALL.value:
            calls.append((index, _tool_key(event), event))
        elif event_type == EventType.TOOL_RESULT.value:
            results[_tool_key(event)] = event
    return [(index, call, results.get(key)) for index, key, call in calls]


def _tool_name(pair: ToolPair) -> str:
    _, call, result = pair
    for event in (call, result):
        if event is not None:
            name = _metadata(event).get('tool')
            if name:
                return str(name)
    return ''


def _tool_failed(pair: ToolPair) -> bool:
    _, call, result = pair
    if result is not None and result.error:
        return True
    for event in (result, call):
        if event is None:
            continue
        status = str(_metadata(event).get('tool_status') or '').lower()
        if status in FAILED_TOOL_STATUSES:
            return True
    return False


def _tool_error_signals(
    pairs: Sequence[ToolPair],
    last_answer: Optional[int],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Return ``(unrecovered, recovered)`` tool-error signals.

    Only calls before the final assistant text can explain why the run ended
    there. Event ids are not ordered, so use the event-list position recorded
    when the pairs were built. A failed call followed by any successful tool
    call before the final answer is treated as recovered: the agent retried
    and went on to finish, so the run is not marked failed.
    """

    if last_answer is None:
        return None, None
    before_answer = [pair for pair in pairs if pair[0] < last_answer][-5:]
    failed = [pair for pair in before_answer if _tool_failed(pair)]
    if not failed:
        return None, None
    culprit_pair = failed[-1]
    culprit_info = _tool_error_info(culprit_pair)
    recovery_pair = next(
        (
            pair for pair in before_answer
            if pair[0] > culprit_pair[0] and not _tool_failed(pair)
        ),
        None,
    )
    if recovery_pair is not None:
        recovered_at = recovery_pair[2] or recovery_pair[1]
        culprit_info['recovered_by_event_id'] = recovered_at.event_id
        culprit_info['recovered_by_step_index'] = recovered_at.step_index
        culprit_info['recovered_by_tool'] = _tool_name(recovery_pair)
        return None, culprit_info
    return culprit_info, None


def _tool_error_info(pair: ToolPair) -> Dict[str, Any]:
    call, result = pair[1], pair[2]
    culprit = result if result is not None and result.error else call
    return {
        'event_id': culprit.event_id,
        'step_index': culprit.step_index,
        'tool': _tool_name(pair),
        'status': _metadata(culprit).get('tool_status') or 'error',
        'error': _quote(culprit.error),
    }


def _parse_tool_input(call: AgentEvent) -> Any:
    value = call.input
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return json.loads(stripped)
        except (TypeError, ValueError):
            return stripped
    return value


def sql_text_from_call(call: AgentEvent) -> Optional[str]:
    """Return the SQL string carried by a tool call, if it is a SQL call.

    Mirrors the intranet helper: a parameter key containing ``sql`` or
    ``query`` is a SQL call, as is a string value that starts with a SQL
    statement keyword.
    """

    value = _parse_tool_input(call)
    candidates: List[str] = []
    if isinstance(value, str):
        candidates.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(item, str):
                continue
            lowered = str(key).lower()
            if 'sql' in lowered or 'query' in lowered or _starts_with_sql(item):
                candidates.append(item)
    for candidate in candidates:
        if candidate.strip():
            return candidate
    return None


def _starts_with_sql(value: str) -> bool:
    first = value.lstrip().split(None, 1)[0].lower() if value.strip() else ''
    return first in SQL_PREFIXES


def _result_text(result: Optional[AgentEvent]) -> str:
    if result is None or result.error:
        return ''
    return _text(result.output)


def is_empty_sql_result(text: str) -> bool:
    """Whether a SQL tool result is an empty set."""

    if text is None:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if stripped in ('[]', '{}', 'null', 'None'):
        return True
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, (list, dict)) and not parsed:
        return True
    lowered = stripped.lower()
    markers = _configured_terms('AGENTDEBUG_OUTCOME_EMPTY_MARKERS', DEFAULT_EMPTY_MARKERS)
    return any(marker and marker.lower() in lowered for marker in markers)


def _empty_sql_result(pairs: Sequence[ToolPair]) -> Optional[Dict[str, Any]]:
    latest: Optional[Dict[str, Any]] = None
    for pair in pairs:
        call, result = pair[1], pair[2]
        if result is None or result.error:
            continue
        sql = sql_text_from_call(call)
        if sql is None:
            continue
        text = _result_text(result)
        if not is_empty_sql_result(text):
            continue
        latest = {
            'event_id': result.event_id,
            'call_event_id': call.event_id,
            'step_index': result.step_index,
            'tool': _tool_name(pair),
            'sql': _quote(sql),
            'result': _quote(text),
        }
    return latest


# -- endings --------------------------------------------------------------

def _last_finish_reason(events: Sequence[AgentEvent]) -> Optional[str]:
    # Scope to the final assistant message. An early turn that aborted but was
    # followed by a successful one must not make the whole session look
    # unfinished.
    last_message_id = None
    for event in reversed(events):
        if _is_assistant_event(event):
            last_message_id = _metadata(event).get('message_id')
            break
    for event in reversed(events):
        metadata = _metadata(event)
        if last_message_id is not None and metadata.get('message_id') != last_message_id:
            continue
        reason = metadata.get('finish_reason')
        if reason:
            return str(reason).lower()
    return None


def _no_wrapup(
    events: Sequence[AgentEvent],
    last_answer: Optional[int],
) -> Optional[Dict[str, Any]]:
    reason = _last_finish_reason(events)
    if reason and reason in ABORTED_FINISH_REASONS:
        return {'reason': 'aborted_finish_reason', 'finish_reason': reason}
    meaningful = [event for event in events if not _is_lifecycle(event)]
    if not meaningful:
        return {'reason': 'empty_trajectory'}
    last = meaningful[-1]
    if _is_assistant_text(last):
        return None
    return {
        'reason': 'last_event_not_assistant_text',
        'event_id': last.event_id,
        'step_index': last.step_index,
        'event_type': _event_type_value(last),
    }


# -- config ---------------------------------------------------------------

def _configured_terms(env_name: str, defaults: Iterable[str]) -> List[str]:
    raw = os.environ.get(env_name)
    if not raw:
        return list(defaults)
    terms = [item.strip() for item in raw.split(',') if item.strip()]
    return terms or list(defaults)


__all__ = [
    'ABORTED_FINISH_REASONS',
    'DEFAULT_EMPTY_MARKERS',
    'DEFAULT_REJECTION_TERMS',
    'FAILED_TOOL_STATUSES',
    'OutcomeSignals',
    'extract_outcome_signals',
    'is_empty_sql_result',
    'sql_text_from_call',
]
