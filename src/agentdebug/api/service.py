"""Orchestration: lookup session -> convert -> run pipeline -> persist.

Wraps the upstream diagnose pipeline with the OpenCode data source and the
browser-facing response envelope (issues #266 / #299). Runs each diagnosis in
a background thread; state lives in memory, artifacts on disk under
``diagnosis/``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from agentdebug.api.brief import render_brief
from agentdebug.api.data_taxonomy import register_data_taxonomy
from agentdebug.api.db_adapter.v2_adapter import (
    SessionNotFound,
    load_session,
    session_exists,
)
from agentdebug.api.diagnose import (
    DEFAULT_MIN_CONFIDENCE,
    ServiceAttributor,
    ServiceJudge,
    apply_root_cause,
    normal_outcome_summary,
    should_skip_attribution,
)
from agentdebug.api.localization import ZhReflexionSuggestion
from agentdebug.api.opencode_converter import convert_session, trajectory_to_wire
from agentdebug.api.outcome import extract_outcome_signals
from agentdebug.api.report import report_to_wire
from agentdebug.diagnose.pipeline import DiagnosePipeline
from agentdebug.runtime import OpenAICompatClient

LOGGER = logging.getLogger('agentdebug.api')

DETECTORS = ('llm',)
ATTRIBUTORS = ('all_at_once',)
DEFAULT_MODEL = 'deepseek-flash'


class LLMNotConfigured(Exception):
    """Raised when no LLM endpoint is configured for the detector stage."""


@dataclass
class ServiceConfig:
    """Runtime configuration, resolved from environment variables."""

    db_path: str
    out_dir: str
    base_url: str = ''
    api_key: str = ''
    model: str = DEFAULT_MODEL
    timeout: float = 180.0
    #: Findings below this confidence stay in the report but never become the
    #: root cause (issue #3, P4).
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    #: ``zh`` selects the service prompts; ``en`` keeps the upstream ones.
    language: str = 'zh'


def config_from_env(environ: Optional[Dict[str, str]] = None) -> ServiceConfig:
    env = environ if environ is not None else os.environ
    default_db = os.path.join(os.path.expanduser('~'), '.dataagent', 'opencode.db')
    default_out = os.path.join(repo_root(), 'diagnosis')
    return ServiceConfig(
        db_path=env.get('OPENCODE_DB') or default_db,
        out_dir=env.get('DIAGNOSIS_OUT_DIR') or default_out,
        base_url=env.get('AGENTDEBUG_LLM_BASE_URL') or '',
        api_key=env.get('AGENTDEBUG_LLM_API_KEY') or '',
        model=env.get('AGENTDEBUG_LLM_MODEL') or DEFAULT_MODEL,
        timeout=float(env.get('AGENTDEBUG_LLM_TIMEOUT') or 180.0),
        min_confidence=_confidence_from_env(env),
        language=(env.get('AGENTDEBUG_LANGUAGE') or 'zh').strip().lower() or 'zh',
    )


def _confidence_from_env(env: Dict[str, str]) -> float:
    raw = env.get('AGENTDEBUG_MIN_CONFIDENCE')
    if raw is None or str(raw).strip() == '':
        return DEFAULT_MIN_CONFIDENCE
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MIN_CONFIDENCE
    return min(1.0, max(0.0, value))


def repo_root() -> str:
    """Repository root (``<root>/src/agentdebug/api/service.py``)."""
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )


class DiagnosisService:
    """Submit/poll diagnosis runs for OpenCode sessions."""

    def __init__(self, config: ServiceConfig):
        self.config = config
        # The data-scenario taxonomy is a service concern: make the extra
        # failure modes visible to the judge (and translatable) only when the
        # local service actually runs. Idempotent, so repeated construction is
        # safe.
        register_data_taxonomy()
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._llm = self._build_llm()

    @property
    def llm_configured(self) -> bool:
        return self._llm is not None

    def submit(self, session_id: str, detector: str, attributor: str) -> Dict[str, Any]:
        """Start a run (or reuse the in-flight one); raise for unknown sessions."""
        if not session_exists(self.config.db_path, session_id):
            raise SessionNotFound(session_id)
        with self._lock:
            entry = self._entries.get(session_id)
            if (
                entry is not None
                and entry['status'] == 'processing'
                and entry['detector'] == detector
                and entry['attributor'] == attributor
            ):
                return _processing_envelope(session_id, detector, attributor)
            if self._llm is None:
                raise LLMNotConfigured('AGENTDEBUG_LLM_BASE_URL/API_KEY are not set')
            entry = {
                'status': 'processing',
                'session_id': session_id,
                'detector': detector,
                'attributor': attributor,
                'stored_at': None,
                'data': None,
                'error': None,
                'elapsed_seconds': None,
            }
            self._entries[session_id] = entry
        worker = threading.Thread(
            target=self._run,
            args=(entry,),
            name='diagnose-%s' % session_id,
            daemon=True,
        )
        worker.start()
        return _processing_envelope(session_id, detector, attributor)

    def status(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Polling response; ``None`` when the session was never submitted."""
        with self._lock:
            entry = self._entries.get(session_id)
            if entry is None:
                return None
            snapshot = dict(entry)
        envelope = {
            'status': snapshot['status'],
            'session_id': session_id,
            'detector': snapshot['detector'],
            'attributor': snapshot['attributor'],
        }
        if snapshot['status'] == 'processing':
            envelope.update({
                'stored_at': None,
                'data': None,
                'error': None,
                'elapsed_seconds': None,
            })
        elif snapshot['status'] == 'done':
            envelope.update({
                'stored_at': snapshot['stored_at'],
                'data': snapshot['data'],
                'error': None,
                'elapsed_seconds': snapshot['elapsed_seconds'],
            })
        else:
            envelope.update({
                'stored_at': None,
                'data': None,
                'error': snapshot['error'],
                'elapsed_seconds': snapshot['elapsed_seconds'],
            })
        return envelope

    def _run(self, entry: Dict[str, Any]) -> None:
        session_id = entry['session_id']
        started = time.monotonic()
        try:
            record = load_session(self.config.db_path, session_id)
            trajectory = convert_session(record)
            signals = extract_outcome_signals(trajectory)
            report = ServiceJudge(
                self._llm,
                language=self.config.language,
                outcome_signals=signals,
                max_tokens=8192,
                request_json=True,
            ).analyze(trajectory)

            if should_skip_attribution(report, signals, self.config.min_confidence):
                # A normal outcome with nothing credible to blame: skip the
                # attributor call entirely instead of asking it to explain a
                # failure that did not happen.
                apply_root_cause(report, None, self.config.min_confidence)
                report.summary = normal_outcome_summary(signals)
                report.attribution = {
                    'method': 'skipped',
                    'elapsed_ms': 0,
                    'hypotheses': [],
                    'primary': None,
                    'raw': {
                        'skipped': True,
                        'skip_reason': 'normal_outcome_no_credible_findings',
                    },
                }
                report.recovery = {'proposal_count': 0, 'proposals': []}
                report.metadata['attribution_skipped'] = True
                report.metadata['skip_reason'] = 'normal_outcome_no_credible_findings'
            else:
                pipeline = DiagnosePipeline(
                    detector=_PrecomputedDetector(report),
                    attributor=ServiceAttributor(
                        self._llm,
                        language=self.config.language,
                        outcome_signals=signals,
                        min_confidence=self.config.min_confidence,
                        max_tokens=16000,
                    ),
                    recoverer=ZhReflexionSuggestion(),
                )
                result = pipeline.run(trajectory)
                report = result.report
                # Must run after the pipeline: DiagnoseContext.build writes the
                # attribution's blame into root_cause_* first, and P4 overrides
                # that with the eligible-finding selector.
                apply_root_cause(report, result.attribution, self.config.min_confidence)

            elapsed = time.monotonic() - started
            trajectory_wire = trajectory_to_wire(trajectory)
            report_wire = report_to_wire(
                report,
                detector=entry['detector'],
                attributor=entry['attributor'],
                elapsed_seconds=elapsed,
            )
            brief = render_brief(report_wire, trajectory_wire)
            stored_at = self._persist(session_id, trajectory_wire, report_wire, brief)
            self._finish(entry, status='done', stored_at=stored_at, data={
                'trajectory': trajectory_wire,
                'report': report_wire,
                'brief': brief,
            }, error=None, elapsed_seconds=round(elapsed, 2))
        except Exception as exc:  # noqa: BLE001 - surfaced to the poller
            LOGGER.exception('diagnosis failed for %s', session_id)
            self._finish(
                entry,
                status='failed',
                stored_at=None,
                data=None,
                error='%s: %s' % (type(exc).__name__, exc),
                elapsed_seconds=round(time.monotonic() - started, 2),
            )

    def _finish(self, entry: Dict[str, Any], **fields: Any) -> None:
        with self._lock:
            entry.update(fields)

    def _persist(
        self,
        session_id: str,
        trajectory_wire: Dict[str, Any],
        report_wire: Dict[str, Any],
        brief: str,
    ) -> Dict[str, str]:
        out_dir = self.config.out_dir
        os.makedirs(out_dir, exist_ok=True)
        payloads = {
            'trajectory': trajectory_wire,
            'report': report_wire,
            'brief': brief,
        }
        parent = os.path.basename(os.path.normpath(out_dir))
        stored_at: Dict[str, str] = {}
        for kind, payload in payloads.items():
            filename = 'session_%s_%s%s' % (
                session_id,
                kind,
                '.md' if kind == 'brief' else '.json',
            )
            path = os.path.join(out_dir, filename)
            with open(path, 'w', encoding='utf-8') as handle:
                if kind == 'brief':
                    handle.write(payload)
                else:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
            stored_at[kind] = '%s/%s' % (parent, filename)
        return stored_at

    def _build_llm(self) -> Optional[OpenAICompatClient]:
        if not (self.config.base_url and self.config.api_key):
            return None
        return OpenAICompatClient(
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            model=self.config.model,
            default_max_tokens=8192,
            timeout=self.config.timeout,
            # The DeepSeek endpoint occasionally drops TLS handshakes/records;
            # retry transport errors before the attributor's own fallback kicks in.
            max_retries=3,
        )


class _PrecomputedDetector:
    """Detector stage that replays an already-computed report.

    The service runs the judge first because P3 decides whether to skip
    attribution based on the findings; the second pipeline pass then supplies
    the report to attribute/recover without paying for detection twice.
    """

    def __init__(self, report: Any) -> None:
        self.report = report

    def analyze(self, trajectory: Any) -> Any:
        return self.report


def _processing_envelope(session_id: str, detector: str, attributor: str) -> Dict[str, Any]:
    return {
        'status': 'processing',
        'session_id': session_id,
        'detector': detector,
        'attributor': attributor,
    }
