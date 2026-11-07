"""Tests for the native OpenCode session browser (``GET /`` and sessions API)."""

from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

from agentdebug.api.app import create_app
from agentdebug.api.report import attribution_to_wire
from agentdebug.api.service import ServiceConfig

SESSION_NEW = 'ses_new000000000000000000000000000'
SESSION_OLD = 'ses_old000000000000000000000000000'


class _FakeService:
    llm_configured = True

    def submit(self, session_id, detector, attributor):  # pragma: no cover
        raise AssertionError('the UI tests do not submit diagnoses')

    def status(self, session_id):  # pragma: no cover
        raise AssertionError('the UI tests do not poll diagnoses')


def _create_db(tmp_path):
    db_path = tmp_path / 'opencode.db'
    connection = sqlite3.connect(db_path)
    connection.execute(
        'CREATE TABLE session_v2 ('
        ' id TEXT PRIMARY KEY, project_id TEXT, title TEXT, slug TEXT,'
        ' directory TEXT, version TEXT, agent TEXT,'
        ' time_created INTEGER, time_updated INTEGER)'
    )
    connection.execute(
        'CREATE TABLE session_message ('
        ' id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER,'
        ' time_created INTEGER, time_updated INTEGER, data TEXT)'
    )
    sessions = [
        (SESSION_NEW, 'prj_1', '新会话', 'new-session', '/tmp/new', 'local',
         'navigator-orchestrator', 1000, 2000),
        (SESSION_OLD, 'prj_1', '旧会话', 'old-session', '/tmp/old', 'local',
         None, 500, 1000),
    ]
    connection.executemany(
        'INSERT INTO session_v2 (id, project_id, title, slug, directory, version,'
        ' agent, time_created, time_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        sessions,
    )
    messages = [
        ('msg_new_1', SESSION_NEW, 'user', 1,
         {'text': '查一下昨天的销量', 'content': []}),
        ('msg_new_2', SESSION_NEW, 'assistant', 2,
         {'agent': 'navigator-orchestrator',
          'content': [{'type': 'text', 'text': '昨天销量是 100。'}]}),
        ('msg_old_1', SESSION_OLD, 'user', 1,
         {'text': '旧问题', 'content': []}),
    ]
    connection.executemany(
        'INSERT INTO session_message (id, session_id, type, seq, data)'
        ' VALUES (?, ?, ?, ?, ?)',
        [(mid, sid, kind, seq, json.dumps(data, ensure_ascii=False))
         for mid, sid, kind, seq, data in messages],
    )
    # Message types the adapter ignores must not affect the picker count.
    connection.execute(
        'INSERT INTO session_message (id, session_id, type, seq, data)'
        ' VALUES (?, ?, ?, ?, ?)',
        ('msg_idle', SESSION_NEW, 'idle', 3, '{}'),
    )
    connection.commit()
    connection.close()
    return db_path


def _client(tmp_path):
    config = ServiceConfig(
        db_path=str(_create_db(tmp_path)),
        out_dir=str(tmp_path / 'out'),
    )
    return TestClient(create_app(config=config, service=_FakeService()))


def test_index_serves_the_native_page(tmp_path):
    client = _client(tmp_path)
    for path in ('/', '/ui', '/api/agentdebug', '/api/agentdebug/ui'):
        response = client.get(path)
        assert response.status_code == 200
        assert 'text/html' in response.headers['content-type']
        assert 'Agent 轨迹诊断' in response.text
        assert 'all_at_once' in response.text
        # Long trajectory payloads render collapsed with a bulk toggle.
        assert 'content-details' in response.text
        assert '全部展开' in response.text
        # The trajectory map marks failures and diagnosis highlights.
        assert '轨迹地图' in response.text
        assert 'function renderTraceMap' in response.text
        assert 'trace-node' in response.text
        # Diagnosed sessions open on the report pane, with the trajectory as
        # a separate view, so the report does not sit below a long timeline.
        assert 'id="view-tabs"' in response.text
        assert 'data-view="report"' in response.text
        assert 'report-placeholder' in response.text
        # The console is product-neutral: no AgentDebugX branding in the page.
        assert 'AgentDebugX' not in response.text



def test_sessions_list_is_newest_first_and_counted(tmp_path):
    client = _client(tmp_path)
    response = client.get('/api/v1/sessions')
    assert response.status_code == 200
    sessions = response.json()['sessions']
    assert [item['session_id'] for item in sessions] == [SESSION_NEW, SESSION_OLD]
    assert sessions[0]['title'] == '新会话'
    assert sessions[0]['message_count'] == 2
    assert sessions[1]['message_count'] == 1
    assert 'messages' not in sessions[0]


def test_sessions_list_honours_the_browser_prefix(tmp_path):
    client = _client(tmp_path)
    response = client.get('/api/agentdebug/api/v1/sessions?limit=1')
    assert response.status_code == 200
    assert [item['session_id'] for item in response.json()['sessions']] == [SESSION_NEW]
    assert client.get('/api/v1/sessions?limit=0').status_code == 422
    assert client.get('/api/agentdebug/healthz').json()['status'] == 'ok'
    assert client.get(
        '/api/agentdebug/api/v1/sessions/' + SESSION_NEW
    ).json()['session']['session_id'] == SESSION_NEW


def test_session_detail_returns_converted_trajectory(tmp_path):
    client = _client(tmp_path)
    response = client.get('/api/v1/sessions/' + SESSION_NEW)
    assert response.status_code == 200
    payload = response.json()
    assert payload['session']['session_id'] == SESSION_NEW
    assert payload['session']['message_count'] == 2
    assert payload['trajectory']['goal'] == '查一下昨天的销量'
    assert [event['event_type'] for event in payload['trajectory']['events']] == [
        'PLAN',
        'AGENT_STEP',
    ]


def test_unknown_session_detail_is_404(tmp_path):
    client = _client(tmp_path)
    response = client.get('/api/v1/sessions/ses_missing')
    assert response.status_code == 404
    assert response.json()['error'] == 'session_not_found'


def test_missing_database_returns_a_clear_error(tmp_path):
    config = ServiceConfig(
        db_path=str(tmp_path / 'missing.db'),
        out_dir=str(tmp_path / 'out'),
    )
    client = TestClient(create_app(config=config, service=_FakeService()))
    response = client.get('/api/v1/sessions')
    assert response.status_code == 500
    assert response.json()['error'] == 'session_list_failed'


def test_attribution_wire_keeps_no_failure_rationale():
    wire = attribution_to_wire({
        'method': 'all_at_once',
        'raw': {
            'finding_count': 0,
            'no_failure': True,
            'no_failure_rationale': '工具报错后已恢复，最终回答正确。',
        },
    })
    assert wire['raw']['no_failure'] is True
    assert wire['raw']['no_failure_rationale'] == '工具报错后已恢复，最终回答正确。'
