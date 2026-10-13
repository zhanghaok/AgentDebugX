"""Tests for the local DataAgent diagnosis service (``agentdebug.api``).

Covers the pure layers end to end — sanitizer, OpenCode converter, report/brief
serialization and the FastAPI envelopes — without touching a database or an LLM.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from agentdebug.api.app import create_app
from agentdebug.api.brief import render_brief
from agentdebug.api.db_adapter.v2_adapter import SessionNotFound, SessionRecord
from agentdebug.api.localization import (
    FAILURE_MODE_ZH,
    ZhReflexionSuggestion,
    assert_full_coverage,
    localize_failure_mode,
)
from agentdebug.api.opencode_converter import convert_session, trajectory_to_wire
from agentdebug.api.report import report_to_wire
from agentdebug.api.sanitize import (
    is_synthetic_client_tool_continuation,
    unwrap_user_prompt,
    visible_text,
)
from agentdebug.api.service import LLMNotConfigured, ServiceConfig
from agentdebug.schema import AgentTrajectory, DiagnosticReport, FailureFinding
from agentdebug.schema.taxonomy import SEED_FAILURE_MODES

SESSION_ID = 'ses_testapi0000000000000000000000'


def _session(messages):
    return SessionRecord(
        session_id=SESSION_ID,
        project_id='prj_1',
        title='演示会话',
        slug='demo',
        directory='/tmp/workspace',
        version='local',
        agent='navigator-orchestrator',
        messages=messages,
    )


def _user(message_id, data):
    return {'id': message_id, 'role': 'user', 'data': data}


def _assistant(message_id, parts):
    return {'id': message_id, 'role': 'assistant', 'data': {'agent': 'navigator-orchestrator', 'content': parts}}


def _demo_session():
    return _session([
        _user('msg_1', {'text': '<environment>ctx</environment>\n<user_message>看一下这个数据\n</user_message>', 'content': []}),
        _assistant('msg_2', [
            {'type': 'reasoning', 'text': '我先调用工具。'},
            {'type': 'tool', 'id': 'call_1', 'name': 'knowledge_search',
             'state': {'status': 'error', 'input': {'query': 'x'}, 'error': {'message': 'boom'}}},
            {'type': 'text', 'text': '工具不可用，改为直答。'},
        ]),
        _assistant('msg_3', [{'type': 'text', 'text': '   '}]),
        _user('msg_4', {'text': '[client tool result] {"ok": true}', 'content': []}),
    ])


def test_sanitize_unwraps_user_prompt_and_strips_protocol_blocks():
    raw = ('<environment>env</environment><server_tools>tools</server_tools>'
           '<user_message>  帮我看下销量  </user_message><unclosed_tag>')
    # Pipeline order: unwrap first (peels <environment>, keeps <user_message>
    # inner text), then strip remaining protocol blocks from that text.
    unwrapped = unwrap_user_prompt(raw)
    assert unwrapped == '帮我看下销量'
    assert visible_text(unwrapped) == '帮我看下销量'
    assert visible_text('<environment>e</environment><client_tools>t</client_tools>真实问题') == '真实问题'
    assert visible_text('<server_tools>never closed') == ''
    assert is_synthetic_client_tool_continuation('[client tool result] {"a": 1}')
    assert not is_synthetic_client_tool_continuation('真实的用户问题')


def test_converter_maps_parts_to_events_with_deterministic_ids():
    trajectory = convert_session(_demo_session())
    wire = trajectory_to_wire(trajectory)

    assert wire['trace_id'] == SESSION_ID
    assert wire['goal'] == '演示会话'
    assert wire['framework'] == 'opencode'
    assert wire['task_id'] == 'prj_1'
    assert wire['metadata'] == {
        'source': 'opencode_session',
        'slug': 'demo',
        'directory': '/tmp/workspace',
        'version': 'local',
    }

    events = wire['events']
    assert [event['event_type'] for event in events] == [
        'PLAN', 'REFLECTION', 'TOOL_CALL', 'TOOL_RESULT', 'AGENT_STEP',
    ]
    assert [event['step_index'] for event in events] == [1, 2, 3, 4, 5]
    assert [event['event_id'] for event in events] == [
        '%s_step%d' % (SESSION_ID, step) for step in range(1, 6)
    ]
    assert events[0]['output'] == '看一下这个数据'
    assert events[0]['agent_name'] == 'user'
    assert events[2]['input'] == json.dumps({'query': 'x'}, ensure_ascii=False)
    assert events[2]['output'] == 'call_1'
    assert events[3]['error'] == 'boom'
    assert 'output' not in events[3]

    call_metadata = events[2]['metadata']
    assert call_metadata['message_id'] == 'msg_2'
    assert call_metadata['message_idx'] == 1
    assert call_metadata['part_idx'] == 1
    assert call_metadata['role'] == 'assistant'
    assert call_metadata['tool'] == 'knowledge_search'
    assert call_metadata['tool_status'] == 'error'
    assert call_metadata['callID'] == 'call_1'


def test_converter_keeps_finish_reason_for_step_parts():
    session = _session([_assistant('msg_1', [
        {'type': 'step-finish', 'finishReason': 'stop'},
    ])])
    events = trajectory_to_wire(convert_session(session))['events']
    assert len(events) == 1
    assert events[0]['event_type'] == 'AGENT_STEP'
    assert events[0]['metadata']['finish_reason'] == 'stop'


def test_localization_covers_every_seed_mode():
    assert_full_coverage()
    assert len(FAILURE_MODE_ZH) == len(SEED_FAILURE_MODES)
    for mode_id, mode in SEED_FAILURE_MODES.items():
        wire = localize_failure_mode(mode)
        assert wire['name_zh']
        assert len(wire['suggestion_templates_zh']) == len(mode.suggestion_templates)
    assert localize_failure_mode(SEED_FAILURE_MODES['action.wrong_tool'])['name_zh'] == '工具选择错误'


def _report(failure_mode) -> DiagnosticReport:
    return DiagnosticReport(
        trace_id=SESSION_ID,
        task_id='prj_1',
        summary='工具选择错误导致失败。',
        findings=[FailureFinding(
            failure_mode=failure_mode,
            event_id='%s_step3' % SESSION_ID,
            agent_name='navigator-orchestrator',
            step_index=3,
            confidence=0.9,
            evidence=['tool result: No tool named "render_a2ui"'],
            suggestion='Add tool routing examples.',
        )],
    )


def test_report_wire_matches_intranet_key_order(failure_mode):
    wire = report_to_wire(_report(failure_mode), detector='llm',
                          attributor='all_at_once', elapsed_seconds=1.239)
    assert list(wire) == [
        'report_id', 'trace_id', 'task_id', 'generated_at', 'root_cause_event_id',
        'root_cause_agent', 'root_cause_step_index', 'findings', 'summary',
        'overall_summary', 'suggestions', 'attribution', 'recovery', 'audit', 'metadata',
    ]
    assert wire['summary'] == wire['overall_summary']
    assert wire['generated_at'].endswith('Z')
    assert list(wire['findings'][0]) == [
        'finding_id', 'failure_mode', 'event_id', 'agent_name', 'step_index',
        'confidence', 'evidence', 'suggestion', 'metadata',
    ]
    assert wire['attribution']['elapsed_ms'] == 0
    assert wire['attribution']['method'] == 'all_at_once'
    # No attribution payload on this report -> hypotheses empty, no 归因 data.
    assert wire['attribution']['hypotheses'] == []
    assert wire['attribution']['raw']['corrected_action'] is None
    assert wire['recovery'] == {'proposal_count': 0, 'proposals': []}
    assert wire['metadata']['elapsed_seconds'] == 1.24
    assert wire['metadata']['detector'] == 'llm'


def test_brief_renders_intranet_sections(failure_mode):
    report_wire = report_to_wire(_report(failure_mode), detector='llm',
                                 attributor='all_at_once', elapsed_seconds=2.0)
    trajectory_wire = trajectory_to_wire(convert_session(_demo_session()))
    brief = render_brief(report_wire, trajectory_wire)
    headers = [line for line in brief.splitlines() if line.startswith('#')]
    assert headers == ['# 诊断简报', '## 配置', '## 检测总结', '## 归因总结', '## 恢复建议', '## 相关步骤详情']
    assert '> Trace: `%s` ｜ 生成于 ' % SESSION_ID in brief
    assert '| Task ID | prj_1 |' in brief
    assert '- **发现数**：1' in brief
    assert '| 失败模式 | 次数 |' in brief
    assert '> 本节无数据' in brief


def test_zh_reflexion_proposal_is_chinese(failure_mode):
    trajectory = AgentTrajectory(trace_id=SESSION_ID, goal='演示会话', framework='opencode')
    report = _report(failure_mode)
    proposals = ZhReflexionSuggestion().suggest(trajectory, report)
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.target_event_id == '%s_step3' % SESSION_ID
    assert '任务：演示会话' in proposal.suggestion_text
    assert '（%s）' % failure_mode.name in proposal.suggestion_text
    assert '下次请按以下方式处理：' in proposal.suggestion_text
    assert proposal.side_effects == ['memory.write']
    assert proposal.requires_human_approval is False


class _FakeService:
    llm_configured = True

    def submit(self, session_id, detector, attributor):
        if session_id == 'ses_missing':
            raise SessionNotFound(session_id)
        if session_id == 'ses_no_llm':
            raise LLMNotConfigured('no key')
        return {'status': 'processing', 'session_id': session_id,
                'detector': detector, 'attributor': attributor}

    def status(self, session_id):
        if session_id == SESSION_ID:
            return {'status': 'processing', 'session_id': session_id, 'detector': 'llm',
                    'attributor': 'all_at_once', 'stored_at': None, 'data': None,
                    'error': None, 'elapsed_seconds': None}
        return None


def _client() -> TestClient:
    config = ServiceConfig(db_path='/tmp/none.db', out_dir='/tmp/none')
    return TestClient(create_app(config, service=_FakeService()))


def test_diagnose_endpoints_envelopes_and_errors():
    client = _client()
    payload = {'session_id': SESSION_ID, 'detector': 'llm', 'attributor': 'all_at_once'}

    head = client.post('/api/v1/diagnose', json=payload)
    assert head.status_code == 200
    assert head.json() == payload | {'status': 'processing'}

    poll = client.get('/api/v1/diagnose/%s' % SESSION_ID)
    assert list(poll.json()) == ['status', 'session_id', 'detector', 'attributor',
                                 'stored_at', 'data', 'error', 'elapsed_seconds']
    assert poll.json()['stored_at'] is None

    via_gateway_path = client.post('/api/agentdebug/api/v1/diagnose', json=payload)
    assert via_gateway_path.status_code == 200

    assert client.post('/api/v1/diagnose', json={'session_id': SESSION_ID}).status_code == 400
    assert client.post('/api/v1/diagnose', json=payload | {'detector': 'heuristic'}).status_code == 400
    assert client.post('/api/v1/diagnose', json=payload | {'session_id': 'ses_missing'}).status_code == 404
    assert client.get('/api/v1/diagnose/unknown').status_code == 404

    no_llm = client.post('/api/v1/diagnose', json=payload | {'session_id': 'ses_no_llm'})
    assert no_llm.status_code == 503
    assert client.get('/healthz').json()['status'] == 'ok'
