"""Wire serialization of ``DiagnosticReport`` in the diagnosis API shape.

Field order and nesting follow the intranet samples (issues #266 / #299) so
the browser contract stays byte-compatible where it matters.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from agentdebug.api.localization import localize_failure_mode

_HYPOTHESIS_FIELDS = (
    'span_id',
    'step_index',
    'agent_name',
    'confidence',
    'rationale',
    'evidence',
    'sources',
    'corrected_action',
)
_CORRECTED_ACTION_FIELDS = (
    'requested',
    'emitted',
    'reason',
    'source',
    'differs_from_original',
)
_PROPOSAL_FIELDS = (
    'proposal_id',
    'recoverer_id',
    'target_event_id',
    'summary',
    'rationale',
    'confidence',
    'suggestion_text',
    'side_effects',
    'requires_human_approval',
)


def report_to_wire(
    report: Any,
    *,
    detector: str,
    attributor: str,
    elapsed_seconds: float,
) -> Dict[str, Any]:
    metadata = dict(report.metadata or {})
    wire_metadata: Dict[str, Any] = {
        'analyzer': metadata.get('analyzer'),
        'model': metadata.get('model'),
    }
    if metadata.get('recovery_target') is not None:
        wire_metadata['recovery_target'] = metadata['recovery_target']
    wire_metadata['elapsed_seconds'] = round(float(elapsed_seconds), 2)
    wire_metadata['detector'] = detector
    wire_metadata['attributor'] = attributor

    return {
        'report_id': report.report_id,
        'trace_id': report.trace_id,
        'task_id': report.task_id,
        'generated_at': format_generated_at(report.generated_at),
        'root_cause_event_id': report.root_cause_event_id,
        'root_cause_agent': report.root_cause_agent,
        'root_cause_step_index': report.root_cause_step_index,
        'findings': [finding_to_wire(finding) for finding in report.findings],
        'summary': report.summary,
        'overall_summary': report.summary,
        'suggestions': list(report.suggestions or []),
        'attribution': attribution_to_wire(report.attribution),
        'recovery': recovery_to_wire(report.recovery),
        'audit': [_to_plain_dict(entry) for entry in (report.audit or [])],
        'metadata': wire_metadata,
    }


def finding_to_wire(finding: Any) -> Dict[str, Any]:
    return {
        'finding_id': finding.finding_id,
        'failure_mode': localize_failure_mode(finding.failure_mode),
        'event_id': finding.event_id,
        'agent_name': finding.agent_name,
        'step_index': finding.step_index,
        'confidence': finding.confidence,
        'evidence': list(finding.evidence or []),
        'suggestion': finding.suggestion,
        'metadata': dict(finding.metadata or {}),
    }


def attribution_to_wire(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    payload = payload or {}
    raw = payload.get('raw') or {}
    return {
        'method': payload.get('method') or 'all_at_once',
        # Both intranet samples always report 0 here; keep the shape stable.
        'elapsed_ms': 0,
        'hypotheses': [
            _hypothesis_to_wire(item) for item in payload.get('hypotheses') or []
        ],
        'primary': _hypothesis_to_wire(payload.get('primary')),
        'raw': {
            'finding_count': raw.get('finding_count', 0),
            'ground_truth_context_used': bool(raw.get('ground_truth_context_used', False)),
            'eval': raw.get('eval'),
            'corrected_action': _raw_corrected_action(raw.get('corrected_action')),
        },
    }


def recovery_to_wire(recovery: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    recovery = recovery or {}
    proposals = [
        _pick_fields(proposal, _PROPOSAL_FIELDS)
        for proposal in recovery.get('proposals') or []
    ]
    return {
        'proposal_count': recovery.get('proposal_count', len(proposals)),
        'proposals': proposals,
    }


def format_generated_at(value: Any) -> Optional[str]:
    """Format as ``2026-10-10T03:54:45.416742Z`` (UTC, microsecond precision)."""
    if isinstance(value, str) or value is None:
        return value
    if not isinstance(value, datetime):
        return None
    moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'


def _hypothesis_to_wire(hypothesis: Any) -> Optional[Dict[str, Any]]:
    if not hypothesis:
        return None
    wire = {}
    for key in _HYPOTHESIS_FIELDS:
        value = _field(hypothesis, key)
        if key == 'corrected_action':
            value = _hypothesis_corrected_action(value)
        wire[key] = value
    return wire


def _hypothesis_corrected_action(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {key: value.get(key) for key in _CORRECTED_ACTION_FIELDS}
    return _to_plain_dict(value)


def _raw_corrected_action(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return {key: value.get(key) for key in _CORRECTED_ACTION_FIELDS}
    return None


def _pick_fields(source: Any, fields: tuple) -> Dict[str, Any]:
    return {key: _field(source, key) for key in fields}


def _field(source: Any, key: str) -> Any:
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def _to_plain_dict(value: Any) -> Any:
    if value is None or isinstance(value, dict):
        return value
    for attribute in ('model_dump', 'dict'):
        method = getattr(value, attribute, None)
        if callable(method):
            try:
                return method()
            except TypeError:
                continue
    if hasattr(value, '__dict__'):
        return dict(vars(value))
    return value
