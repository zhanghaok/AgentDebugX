"""Tests for the service-specific P3/P4 pipeline (issue #3)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import agentdebug.api.service as service_module
from agentdebug.api.data_taxonomy import register_data_taxonomy
from agentdebug.api.db_adapter.v2_adapter import SessionRecord
from agentdebug.api.diagnose import (
    DEFAULT_MIN_CONFIDENCE,
    MISSING_CONFIDENCE,
    ServiceAttributor,
    ServiceJudge,
    apply_root_cause,
    build_graded_context_builder,
    select_root_finding,
    should_skip_attribution,
)
from agentdebug.api.outcome import OutcomeSignals, extract_outcome_signals
from agentdebug.api.service import DiagnosisService, ServiceConfig
from agentdebug.schema import (
    SEED_FAILURE_MODES,
    AgentEvent,
    AgentTrajectory,
    DiagnosticReport,
    EventType,
    FailureFinding,
)


@pytest.fixture(autouse=True)
def _register_taxonomy():
    """The service registers data modes; restore the global dict afterwards."""

    saved = dict(SEED_FAILURE_MODES)
    register_data_taxonomy()
    yield
    SEED_FAILURE_MODES.clear()
    SEED_FAILURE_MODES.update(saved)


class _ScriptedLLM:
    model = 'fake-model'

    def __init__(self, judge_text='{"findings": [], "summary": "ok"}',
                 attributor_text=None):
        self.judge_text = judge_text
        self.attributor_text = attributor_text
        self.calls = []

    def complete(self, messages, max_tokens=0, **kwargs):
        self.calls.append({
            'messages': messages,
            'max_tokens': max_tokens,
            **kwargs,
        })
        system = messages[0]['content']
        if 'AgentDebugX-Attributor' in system:
            text = self.attributor_text or (
                '{"span_id": null, "step_index": null, "agent_name": null, '
                '"confidence": 0, "rationale": "no failure", "evidence": []}'
            )
        else:
            text = self.judge_text
        return SimpleNamespace(text=text)


def _event(event_id, step_index, *, agent='agent', event_type=EventType.AGENT_STEP,
           output='内容', metadata=None):
    return AgentEvent(
        event_id=event_id,
        trace_id='trace_unit',
        agent_name=agent,
        event_type=event_type,
        step_index=step_index,
        output=output,
        metadata=metadata or {'role': 'assistant', 'part_type': 'text'},
    )


def _trajectory():
    trajectory = AgentTrajectory(trace_id='trace_unit', goal='查一下销量')
    trajectory.add_event(_event(
        'u1', 1, agent='user', event_type=EventType.PLAN,
        output='查一下昨天的销量',
        metadata={'role': 'user', 'part_type': 'text'},
    ))
    trajectory.add_event(_event('e1', 2, output='昨天销量是 100。'))
    return trajectory


def _finding(mode_id, *, event_id='e1', step=1, confidence=0.8,
             evidence='evidence'):
    return FailureFinding(
        failure_mode=SEED_FAILURE_MODES[mode_id],
        event_id=event_id,
        agent_name='agent',
        step_index=step,
        confidence=confidence,
        evidence=[evidence],
    )


def test_service_judge_uses_chinese_prompt_data_modes_and_signals():
    llm = _ScriptedLLM()
    trajectory = _trajectory()
    signals = extract_outcome_signals(trajectory)
    report = ServiceJudge(llm, outcome_signals=signals).analyze(trajectory)

    system, user = llm.calls[0]['messages']
    assert 'AgentDebugX-Judge' in system['content']
    assert 'data.join_error' in user['content']
    assert 'environment.source_data_issue' in user['content']
    assert '结果信号' in user['content']
    assert '"verdict": "normal"' in user['content']
    assert report.metadata['outcome_signals']['verdict'] == 'normal'
    # The deterministic graded builder is installed for the zh judge.
    assert report.metadata['analyzer'] == 'ServiceJudge'


def test_service_judge_defaults_missing_confidence_to_0_3():
    judge_text = json.dumps({'findings': [{
        'event_id': 'e1',
        'step_index': 2,
        'agent_name': 'agent',
        'failure_mode_id': 'action.wrong_tool',
        'evidence': ['tool mismatch'],
    }]})
    report = ServiceJudge(_ScriptedLLM(judge_text)).analyze(_trajectory())
    assert len(report.findings) == 1
    assert report.findings[0].confidence == MISSING_CONFIDENCE
    assert MISSING_CONFIDENCE < DEFAULT_MIN_CONFIDENCE


def test_service_attributor_sees_only_eligible_findings_and_no_failure_framing():
    below = _finding('action.wrong_tool', event_id='e1', step=1,
                     confidence=0.4, evidence='below-evidence')
    eligible = _finding('data.join_error', event_id='e2', step=2,
                        confidence=0.8, evidence='eligible-evidence')
    llm = _ScriptedLLM()
    attributor = ServiceAttributor(
        llm,
        outcome_signals=OutcomeSignals(verdict='failed'),
        min_confidence=0.5,
    )
    result = attributor.attribute(_trajectory(), [below, eligible])

    system, user = llm.calls[0]['messages']
    assert 'AgentDebugX-Attributor' in system['content']
    assert 'eligible-evidence' in user['content']
    assert 'below-evidence' not in user['content']
    assert 'decisive mistake' not in user['content'].lower()
    assert 'environment.*' in user['content']
    assert result.hypotheses == []
    assert result.raw['no_failure'] is True


def test_select_root_finding_prefers_attribution_then_confidence():
    low = _finding('action.wrong_tool', event_id='e1', step=1, confidence=0.6)
    high = _finding('data.join_error', event_id='e2', step=2, confidence=0.9)

    assert select_root_finding([low, high]) is high
    attribution = {'hypotheses': [{
        'span_id': 'e1', 'step_index': 1, 'agent_name': 'agent',
    }]}
    assert select_root_finding([low, high], attribution) is low
    # Attribution that does not land on an eligible event falls back.
    attribution = {'primary': {
        'span_id': 'missing', 'step_index': 2, 'agent_name': 'agent',
    }}
    assert select_root_finding([low, high], attribution) is high
    assert select_root_finding([low], None, min_confidence=0.8) is None


def test_select_root_finding_stays_empty_when_attribution_declines_failure():
    finding = _finding('action.wrong_tool', event_id='e1', step=1, confidence=0.9)
    attribution = {
        'method': 'all_at_once',
        'hypotheses': [],
        'primary': None,
        'raw': {'no_failure': True, 'no_failure_rationale': '已恢复'},
    }

    assert select_root_finding([finding], attribution) is None

    report = DiagnosticReport(trace_id='trace_unit', findings=[finding])
    assert apply_root_cause(report, attribution, min_confidence=0.5) is None
    assert report.root_cause_event_id is None
    assert report.metadata['root_cause_kind'] == 'none'


def test_apply_root_cause_marks_below_threshold_and_sets_kind():
    below = _finding('environment.source_data_issue', event_id='e1', step=1,
                     confidence=0.4)
    eligible = _finding('data.join_error', event_id='e2', step=2, confidence=0.8)
    report = DiagnosticReport(trace_id='trace_unit', findings=[below, eligible])

    root = apply_root_cause(report, None, min_confidence=0.5)
    assert root is eligible
    assert report.root_cause_event_id == 'e2'
    assert report.root_cause_agent == 'agent'
    assert report.root_cause_step_index == 2
    assert report.metadata['root_cause_kind'] == 'agent'
    assert below.metadata['below_threshold'] is True
    assert 'below_threshold' not in eligible.metadata

    empty = DiagnosticReport(trace_id='trace_unit', findings=[below])
    assert apply_root_cause(empty, None, min_confidence=0.5) is None
    assert empty.root_cause_event_id is None
    assert empty.metadata['root_cause_kind'] == 'none'


def test_should_skip_attribution_only_for_normal_outcome_without_credible_finding():
    normal = OutcomeSignals(verdict='normal', normal_end=True)
    failed = OutcomeSignals(verdict='failed')
    below = _finding('action.wrong_tool', confidence=0.4)
    eligible = _finding('action.wrong_tool', confidence=0.8)

    assert should_skip_attribution(
        DiagnosticReport(trace_id='t', findings=[below]), normal, 0.5
    ) is True
    assert should_skip_attribution(
        DiagnosticReport(trace_id='t', findings=[below, eligible]), normal, 0.5
    ) is False
    assert should_skip_attribution(
        DiagnosticReport(trace_id='t', findings=[below]), failed, 0.5
    ) is False


def test_graded_context_builder_keeps_chunk_detail_and_clips_the_rest():
    first = _event('e1', 1, output='A' * 2000)
    second = _event('e2', 2, output='B' * 2000)
    builder = build_graded_context_builder([first, second])

    assert len(builder.pool[0]['th1']) <= 1024
    assert len(builder.pool[1]['th3']) <= 256
    rendered = builder.render_chunk([first, second], [first])
    assert 'CONTEXT-ONLY' in rendered
    assert 'A' * 300 in rendered
    assert 'B' * 100 in rendered
    # Out-of-chunk text is clipped to the th3 budget (256 chars), while the
    # judged chunk keeps the th1 budget (1024).
    assert rendered.count('A') <= 1024
    assert rendered.count('B') <= 256


def _session_record():
    return SessionRecord(
        session_id='ses_service',
        project_id='prj_1',
        title='自动生成的标题',
        slug='demo',
        directory='/tmp/workspace',
        version='local',
        agent='agent',
        messages=[
            {'id': 'msg_1', 'role': 'user',
             'data': {'text': '查一下昨天的销量', 'content': []}},
            {'id': 'msg_2', 'role': 'assistant', 'data': {'agent': 'agent', 'content': [
                {'type': 'text', 'text': '我先查询。'},
                {'type': 'tool', 'id': 'call_1', 'name': 'sql', 'state': {
                    'status': 'error',
                    'input': {'sql': 'select * from sales'},
                    'error': {'message': 'table not found'},
                }},
                {'type': 'text', 'text': '查询失败。'},
            ]}},
        ],
    )


def _normal_session_record():
    return SessionRecord(
        session_id='ses_normal',
        project_id='prj_1',
        title='自动生成的标题',
        slug='demo',
        directory='/tmp/workspace',
        version='local',
        agent='agent',
        messages=[
            {'id': 'msg_1', 'role': 'user',
             'data': {'text': '查一下昨天的销量', 'content': []}},
            {'id': 'msg_2', 'role': 'assistant', 'data': {'agent': 'agent', 'content': [
                {'type': 'text', 'text': '昨天销量是 100。'},
            ]}},
        ],
    )


def _entry(session_id):
    return {
        'status': 'processing',
        'session_id': session_id,
        'detector': 'llm',
        'attributor': 'all_at_once',
        'stored_at': None,
        'data': None,
        'error': None,
        'elapsed_seconds': None,
    }


def test_service_run_wires_signals_attribution_and_root_cause(
    tmp_path, monkeypatch,
):
    judge_text = json.dumps({'findings': [{
        'event_id': 'ses_service_step3',
        'step_index': 3,
        'agent_name': 'agent',
        'failure_mode_id': 'system.tool_execution_error',
        'confidence': 0.9,
        'evidence': ['table not found'],
        'rationale': 'SQL tool failed',
    }], 'summary': '工具执行失败'})
    attributor_text = json.dumps({
        'span_id': 'ses_service_step3',
        'step_index': 3,
        'agent_name': 'agent',
        'confidence': 0.9,
        'rationale': 'the SQL call failed',
        'evidence': ['table not found'],
    })
    llm = _ScriptedLLM(judge_text, attributor_text)
    record = _session_record()
    monkeypatch.setattr(
        service_module, 'load_session',
        lambda db_path, session_id: record,
    )

    service = DiagnosisService(ServiceConfig(
        db_path='unused', out_dir=str(tmp_path),
    ))
    service._llm = llm
    entry = _entry(record.session_id)
    service._run(entry)

    assert entry['status'] == 'done', entry['error']
    wire = entry['data']['report']
    assert wire['root_cause_event_id'] == 'ses_service_step3'
    assert wire['root_cause_step_index'] == 3
    assert wire['metadata']['root_cause_kind'] == 'environment'
    assert wire['metadata']['outcome_signals']['verdict'] == 'failed'
    assert wire['metadata']['outcome_signals']['trailing_tool_error']['tool'] == 'sql'
    assert wire['attribution']['method'] == 'all_at_once'
    assert wire['attribution']['hypotheses'][0]['span_id'] == 'ses_service_step3'
    assert wire['findings'][0]['failure_mode']['mode_id'] == 'system.tool_execution_error'
    assert wire['findings'][0]['failure_mode']['name_zh'] == '工具执行错误'
    assert wire['recovery']['proposal_count'] == 1
    assert entry['stored_at']['report'].endswith('_report.json')


def test_service_run_skips_attribution_for_normal_outcome(tmp_path, monkeypatch):
    llm = _ScriptedLLM(
        judge_text='{"findings": [], "summary": "正常完成"}',
    )
    record = _normal_session_record()
    monkeypatch.setattr(
        service_module, 'load_session',
        lambda db_path, session_id: record,
    )

    service = DiagnosisService(ServiceConfig(
        db_path='unused', out_dir=str(tmp_path),
    ))
    service._llm = llm
    entry = _entry(record.session_id)
    service._run(entry)

    assert entry['status'] == 'done', entry['error']
    wire = entry['data']['report']
    assert wire['attribution']['method'] == 'skipped'
    assert wire['root_cause_event_id'] is None
    assert wire['metadata']['root_cause_kind'] == 'none'
    assert wire['metadata']['attribution_skipped'] is True
    assert wire['metadata']['outcome_signals']['verdict'] == 'normal'
    assert wire['recovery'] == {'proposal_count': 0, 'proposals': []}
    assert wire['summary'].startswith('未发现明确失败')
    # Only the judge ran; the attributor was never called.
    assert len(llm.calls) == 1
