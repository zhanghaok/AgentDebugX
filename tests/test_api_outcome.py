"""Tests for deterministic outcome signals (issue #3, P3)."""

from __future__ import annotations

import json

from agentdebug.api.outcome import (
    extract_outcome_signals,
    is_empty_sql_result,
    sql_text_from_call,
)
from agentdebug.schema import AgentEvent, AgentTrajectory, EventType

TRACE = 'trace_outcome'


def _event(
    event_id,
    step_index,
    event_type,
    *,
    agent='agent',
    output=None,
    input=None,
    error=None,
    metadata=None,
):
    return AgentEvent(
        event_id=event_id,
        trace_id=TRACE,
        agent_name=agent,
        event_type=event_type,
        step_index=step_index,
        input=input,
        output=output,
        error=error,
        metadata=metadata or {},
    )


def _user(event_id, step_index, text):
    return _event(
        event_id, step_index, EventType.PLAN,
        agent='user', output=text,
        metadata={'role': 'user', 'part_type': 'text'},
    )


def _assistant_text(event_id, step_index, text):
    return _event(
        event_id, step_index, EventType.AGENT_STEP,
        output=text,
        metadata={'role': 'assistant', 'part_type': 'text'},
    )


def _tool_call(event_id, step_index, call_id, tool_input, status='completed'):
    return _event(
        event_id, step_index, EventType.TOOL_CALL,
        output=call_id, input=json.dumps(tool_input, ensure_ascii=False),
        metadata={
            'role': 'assistant', 'part_type': 'tool', 'tool': 'sql',
            'tool_status': status, 'callID': call_id,
        },
    )


def _tool_result(event_id, step_index, call_id, *, output=None, error=None,
                 status='completed'):
    return _event(
        event_id, step_index, EventType.TOOL_RESULT,
        output=output, error=error,
        metadata={
            'role': 'assistant', 'part_type': 'tool', 'tool': 'sql',
            'tool_status': status, 'callID': call_id,
        },
    )


def _trajectory(*events):
    trajectory = AgentTrajectory(trace_id=TRACE, goal='查销量')
    for event in events:
        trajectory.add_event(event)
    return trajectory


def test_normal_end_is_normal():
    signals = extract_outcome_signals(_trajectory(
        _user('u1', 1, '查一下昨天的销量'),
        _assistant_text('a1', 2, '昨天销量是 100。'),
    ))
    assert signals.verdict == 'normal'
    assert signals.normal_end is True
    assert signals.user_rejection == []
    assert signals.trailing_tool_error is None
    assert signals.no_wrapup is None
    assert signals.to_wire()['basis'] == ['normal_end']


def test_user_rejection_marks_run_failed():
    signals = extract_outcome_signals(_trajectory(
        _user('u1', 1, '查一下昨天的销量'),
        _assistant_text('a1', 2, '昨天销量是 100。'),
        _user('u2', 3, '这个结果不对，重新查'),
        _assistant_text('a2', 4, '好的，我再查一次。'),
    ))
    assert signals.verdict == 'failed'
    assert signals.normal_end is False
    assert signals.user_rejection[0]['term'] == '不对'
    assert signals.user_rejection[0]['event_id'] == 'u2'


def test_initial_request_with_command_word_is_not_rejection():
    signals = extract_outcome_signals(_trajectory(
        _user('u1', 1, '重新跑一下昨天的销量'),
        _assistant_text('a1', 2, '昨天销量是 100。'),
    ))
    assert signals.verdict == 'normal'
    assert signals.user_rejection == []


def test_trailing_tool_error_before_final_answer_marks_failed():
    signals = extract_outcome_signals(_trajectory(
        _user('u1', 1, '查一下销量'),
        _assistant_text('a1', 2, '我先查一下。'),
        _tool_call('c1', 3, 'call_1', {'sql': 'select * from sales'}),
        _tool_result('r1', 4, 'call_1', error='table not found', status='error'),
        _assistant_text('a2', 5, '抱歉，查询失败了。'),
    ))
    assert signals.verdict == 'failed'
    assert signals.trailing_tool_error['event_id'] == 'r1'
    assert signals.trailing_tool_error['error'] == 'table not found'
    assert signals.empty_result is None


def test_failed_tool_call_followed_by_success_is_recovered_not_failed():
    signals = extract_outcome_signals(_trajectory(
        _user('u1', 1, '查一下销量'),
        _assistant_text('a1', 2, '我先查一下。'),
        _tool_call('c1', 3, 'call_1', {'sql': 'select * from sales'}),
        _tool_result('r1', 4, 'call_1', error='table not found', status='error'),
        _assistant_text('a2', 5, '表名不对，我换一种写法重试。'),
        _tool_call('c2', 6, 'call_2', {'sql': 'select * from sales_v2'}),
        _tool_result('r2', 7, 'call_2', output='3 rows'),
        _assistant_text('a3', 8, '昨天销量是 100。'),
    ))
    assert signals.verdict == 'normal'
    assert signals.normal_end is True
    assert signals.trailing_tool_error is None
    assert signals.recovered_tool_error['event_id'] == 'r1'
    assert signals.recovered_tool_error['tool'] == 'sql'
    assert signals.recovered_tool_error['error'] == 'table not found'
    assert signals.recovered_tool_error['recovered_by_event_id'] == 'r2'
    assert signals.recovered_tool_error['recovered_by_tool'] == 'sql'
    assert signals.to_wire()['basis'] == ['recovered_tool_error', 'normal_end']


def test_failed_tool_call_after_a_successful_call_still_marks_failed():
    signals = extract_outcome_signals(_trajectory(
        _user('u1', 1, '查一下销量'),
        _assistant_text('a1', 2, '我先查一下。'),
        _tool_call('c1', 3, 'call_1', {'sql': 'select * from sales'}),
        _tool_result('r1', 4, 'call_1', output='3 rows'),
        _tool_call('c2', 5, 'call_2', {'sql': 'select * from missing'}),
        _tool_result('r2', 6, 'call_2', error='table not found', status='error'),
        _assistant_text('a2', 7, '抱歉，查询失败了。'),
    ))
    assert signals.verdict == 'failed'
    assert signals.trailing_tool_error['event_id'] == 'r2'
    assert signals.recovered_tool_error is None


def test_run_ending_on_tool_result_has_no_wrapup():
    signals = extract_outcome_signals(_trajectory(
        _user('u1', 1, '查一下销量'),
        _assistant_text('a1', 2, '我先查一下。'),
        _tool_call('c1', 3, 'call_1', {'sql': 'select * from sales'}),
        _tool_result('r1', 4, 'call_1', output='3 rows'),
    ))
    assert signals.verdict == 'failed'
    assert signals.no_wrapup['reason'] == 'last_event_not_assistant_text'
    assert signals.no_wrapup['event_id'] == 'r1'
    assert signals.trailing_tool_error is None


def test_aborted_finish_reason_has_no_wrapup():
    finish = _event(
        'f1', 3, EventType.AGENT_STEP,
        metadata={'role': 'assistant', 'part_type': 'step-finish',
                  'finish_reason': 'error'},
    )
    signals = extract_outcome_signals(_trajectory(
        _user('u1', 1, '查一下销量'),
        _assistant_text('a1', 2, '查询中……'),
        finish,
    ))
    assert signals.verdict == 'failed'
    assert signals.no_wrapup == {
        'reason': 'aborted_finish_reason', 'finish_reason': 'error',
    }


def test_earlier_abort_followed_by_a_later_success_is_not_no_wrapup():
    aborted = _event(
        'f1', 3, EventType.AGENT_STEP,
        metadata={'role': 'assistant', 'part_type': 'step-finish',
                  'finish_reason': 'error', 'message_id': 'msg_1'},
    )
    first_answer = _assistant_text('a1', 2, '第一轮失败了。')
    first_answer.metadata['message_id'] = 'msg_1'
    second_answer = _assistant_text('a3', 5, '第二轮成功了。')
    second_answer.metadata['message_id'] = 'msg_2'
    signals = extract_outcome_signals(_trajectory(
        _user('u1', 1, '先查一下销量'),
        first_answer,
        aborted,
        _user('u2', 4, '再试一次'),
        second_answer,
    ))
    assert signals.verdict == 'normal'
    assert signals.no_wrapup is None


def test_empty_sql_result_is_ambiguous_not_normal():
    signals = extract_outcome_signals(_trajectory(
        _user('u1', 1, '查一下销量'),
        _assistant_text('a1', 2, '我先查一下。'),
        _tool_call('c1', 3, 'call_1', {'query': 'select * from sales'}),
        _tool_result('r1', 4, 'call_1', output='0 rows'),
        _assistant_text('a2', 5, '结果为空。'),
    ))
    assert signals.verdict == 'ambiguous'
    assert signals.normal_end is False
    assert signals.empty_result['event_id'] == 'r1'
    assert signals.empty_result['sql'] == 'select * from sales'


def test_empty_trajectory_is_ambiguous():
    signals = extract_outcome_signals(AgentTrajectory(trace_id=TRACE))
    assert signals.verdict == 'ambiguous'
    assert signals.no_wrapup == {'reason': 'empty_trajectory'}


def test_sql_text_from_call():
    assert sql_text_from_call(
        _event('c1', 1, EventType.TOOL_CALL, input='select 1')
    ) == 'select 1'
    assert sql_text_from_call(
        _event('c2', 1, EventType.TOOL_CALL,
               input=json.dumps({'sql': 'select 2'}))
    ) == 'select 2'
    assert sql_text_from_call(
        _event('c3', 1, EventType.TOOL_CALL,
               input=json.dumps({'query': 'with x as (select 1) select * from x'}))
    ) is not None
    assert sql_text_from_call(
        _event('c4', 1, EventType.TOOL_CALL, input=json.dumps({'foo': 'bar'}))
    ) is None


def test_is_empty_sql_result():
    assert is_empty_sql_result('[]') is True
    assert is_empty_sql_result('{}') is True
    assert is_empty_sql_result('null') is True
    assert is_empty_sql_result('0 rows') is True
    assert is_empty_sql_result('共 0 条') is True
    assert is_empty_sql_result('no data') is True
    assert is_empty_sql_result('') is False
    assert is_empty_sql_result('[{"sales": 1}]') is False
    assert is_empty_sql_result('3 rows') is False
