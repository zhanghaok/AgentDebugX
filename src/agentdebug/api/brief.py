"""Markdown diagnosis brief (``brief`` field of the done response).

Renders the report/trajectory wire payloads into the exact template used by
the intranet service (see issue #299 for the populated sample and #266 for
the zero-finding sample).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_DASH = '—'
_DETAIL_SEPARATOR = ' · '


def render_brief(report: Dict[str, Any], trajectory: Dict[str, Any]) -> str:
    blocks: List[str] = [
        _header_block(report),
        _config_block(report),
        _detection_block(report),
        _attribution_block(report),
        _recovery_block(report),
    ]
    details = _details_block(report, trajectory)
    if details:
        blocks.append(details)
    return '\n\n'.join(blocks) + '\n'


def _header_block(report: Dict[str, Any]) -> str:
    return '# 诊断简报\n\n> Trace: `%s` ｜ 生成于 %s' % (
        report.get('trace_id') or _DASH,
        _display_time(report.get('generated_at')),
    )


def _config_block(report: Dict[str, Any]) -> str:
    metadata = report.get('metadata') or {}
    elapsed = metadata.get('elapsed_seconds')
    elapsed_text = '%.2f s' % elapsed if isinstance(elapsed, (int, float)) else _DASH
    rows = (
        ('Task ID', report.get('task_id') or _DASH),
        ('Detector', metadata.get('detector') or _DASH),
        ('Attributor', metadata.get('attributor') or _DASH),
        ('Analyzer', metadata.get('analyzer') or _DASH),
        ('Model', metadata.get('model') or _DASH),
        ('耗时', elapsed_text),
    )
    lines = ['## 配置', '', '| 项 | 值 |', '|---|---|']
    lines.extend('| %s | %s |' % row for row in rows)
    return '\n'.join(lines)


def _detection_block(report: Dict[str, Any]) -> str:
    findings = report.get('findings') or []
    lines = [
        '## 检测总结',
        '',
        '> %s' % (report.get('summary') or _DASH),
        '',
        '- **发现数**：%d' % len(findings),
    ]
    if report.get('root_cause_step_index') is not None:
        lines.append('- **根因定位**：`%s` @ step %s（`%s`）' % (
            report.get('root_cause_agent') or _DASH,
            report.get('root_cause_step_index'),
            report.get('root_cause_event_id') or _DASH,
        ))
    if findings:
        lines.extend(['', '**失败模式分布**', '', '| 失败模式 | 次数 |', '|---|---|'])
        for mode_id, count in _mode_distribution(findings):
            lines.append('| %s | %d |' % (mode_id, count))
        lines.extend([
            '',
            '**Top 发现（按置信度）**',
            '',
            '| # | Step | Agent | 失败模式 | 置信度 |',
            '|---|---|---|---|---|',
        ])
        ranked = sorted(
            findings, key=lambda item: item.get('confidence') or 0.0, reverse=True
        )
        for index, finding in enumerate(ranked, 1):
            lines.append('| %d | %s | %s | %s | %.2f |' % (
                index,
                finding.get('step_index'),
                finding.get('agent_name') or _DASH,
                (finding.get('failure_mode') or {}).get('mode_id') or _DASH,
                finding.get('confidence') or 0.0,
            ))
    return '\n'.join(lines)


def _attribution_block(report: Dict[str, Any]) -> str:
    attribution = report.get('attribution') or {}
    primary = attribution.get('primary') or {}
    lines = ['## 归因总结', '', '- **方法**：%s' % (attribution.get('method') or _DASH)]
    has_primary = (
        primary.get('agent_name') is not None
        and primary.get('step_index') is not None
    )
    if has_primary:
        lines.append('- **主归因**：`%s` @ step %s，置信度 %.2f' % (
            primary.get('agent_name'),
            primary.get('step_index'),
            primary.get('confidence') or 0.0,
        ))
    else:
        lines.append('- **主归因**：`%s` @ step %s，置信度 0.00' % (_DASH, _DASH))
    lines.extend(['', '> %s' % (primary.get('rationale') or _DASH)])
    lines.extend(['', '**假设列表**', '', '| # | Step | Agent | 置信度 | 依据摘要 |', '|---|---|---|---|---|'])
    hypotheses = attribution.get('hypotheses') or []
    for index, hypothesis in enumerate(hypotheses, 1):
        hypothesis = hypothesis or {}
        step = hypothesis.get('step_index')
        lines.append('| %d | %s | %s | %.2f | %s |' % (
            index,
            step if step is not None else _DASH,
            hypothesis.get('agent_name') or _DASH,
            hypothesis.get('confidence') or 0.0,
            (hypothesis.get('rationale') or _DASH).replace('\n', ' '),
        ))
    return '\n'.join(lines)


def _recovery_block(report: Dict[str, Any]) -> str:
    recovery = report.get('recovery') or {}
    proposals = recovery.get('proposals') or []
    lines = ['## 恢复建议']
    if not proposals:
        lines.extend(['', '> 本节无数据'])
        return '\n'.join(lines)
    lines.extend(['', '共 %d 条建议。' % len(proposals)])
    for index, proposal in enumerate(proposals, 1):
        lines.extend([
            '',
            '### %d. %s' % (index, proposal.get('summary') or _DASH),
            '',
            '- **目标事件**：`%s`' % (proposal.get('target_event_id') or _DASH),
            '- **置信度**：%.2f' % (proposal.get('confidence') or 0.0),
            '- **副作用**：%s' % _side_effects_text(proposal.get('side_effects')),
            '- **需人工审批**：%s' % ('是' if proposal.get('requires_human_approval') else '否'),
            '',
            '```',
            str(proposal.get('suggestion_text') or '').rstrip('\n'),
            '```',
        ])
    return '\n'.join(lines)


def _details_block(
    report: Dict[str, Any], trajectory: Dict[str, Any]
) -> Optional[str]:
    events_by_id = {
        event.get('event_id'): event for event in trajectory.get('events') or []
    }
    wanted_ids: List[str] = [
        finding.get('event_id') for finding in report.get('findings') or []
    ]
    wanted_ids.append(report.get('root_cause_event_id'))

    selected: List[Dict[str, Any]] = []
    seen = set()
    for event_id in wanted_ids:
        if not event_id or event_id in seen or event_id not in events_by_id:
            continue
        seen.add(event_id)
        selected.append(events_by_id[event_id])
    if not selected:
        return None
    selected.sort(key=lambda event: event.get('step_index') or 0)

    lines = ['## 相关步骤详情']
    for event in selected:
        lines.extend(['', '**Step %s** · `%s` · `%s`' % (
            event.get('step_index'),
            event.get('event_type') or _DASH,
            event.get('agent_name') or _DASH,
        ), ''])
        metadata = event.get('metadata') or {}
        if metadata.get('tool'):
            lines.append('- **工具**：工具 `%s`' % metadata.get('tool'))
            lines.append('- **状态**：`%s`' % metadata.get('tool_status'))
        blocks = [_content_lines('输入', event.get('input'))]
        if event.get('output') is not None:
            blocks.append(_content_lines('输出', event.get('output')))
        elif event.get('error') is not None:
            blocks.append(_content_lines('错误', event.get('error')))
        for index, block in enumerate([item for item in blocks if item]):
            if index:
                lines.append('')
            lines.extend(block)
    return '\n'.join(lines)


def _content_lines(label: str, value: Any) -> List[str]:
    if value is None or value == '':
        return []
    return ['- **%s**：' % label, '```', str(value), '```']


def _mode_distribution(findings: List[Dict[str, Any]]) -> List[tuple]:
    counts: Dict[str, int] = {}
    for finding in findings:
        mode_id = (finding.get('failure_mode') or {}).get('mode_id') or _DASH
        counts[mode_id] = counts.get(mode_id, 0) + 1
    return list(counts.items())


def _side_effects_text(side_effects: Any) -> str:
    if not side_effects:
        return '无'
    return ', '.join('`%s`' % effect for effect in side_effects)


def _display_time(value: Any) -> str:
    if not value:
        return _DASH
    try:
        moment = datetime.strptime(str(value), '%Y-%m-%dT%H:%M:%S.%fZ')
    except ValueError:
        return str(value)
    return moment.replace(tzinfo=timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S')
