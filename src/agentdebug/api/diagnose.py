"""Service-specific diagnosis components for issue #3 (P3/P4).

The OSS detectors and attributors default to English prompts and to an
"assume the run failed" framing. The local DataAgent service needs:

* a judge that sees deterministic result signals and knows the source data
  may be at fault,
* an attributor that decides *whether* the run failed before naming a root
  cause, and sees the detector's findings,
* a confidence threshold and a root-cause selector that never promotes a
  below-threshold finding,
* graded context (the judged chunk in detail, the rest as a gist) without
  paying for per-step compression.

Everything here lives in ``agentdebug.api`` so the upstream defaults stay
byte-identical for existing callers.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from agentdebug.api.data_taxonomy import root_cause_kind
from agentdebug.api.outcome import OutcomeSignals
from agentdebug.diagnose.attribute.attribution import (
    AllAtOnceAttributor,
    AttributionResult,
    Blame,
    _step_semantics_note,
)
from agentdebug.diagnose.detect.compression import (
    DEFAULT_TH1_CHARS,
    DEFAULT_TH2_CHARS,
    DEFAULT_TH3_CHARS,
    GradedContextBuilder,
    clip_middle,
    event_text,
)
from agentdebug.diagnose.detect.judge import LLMJudgeAnalyzer
from agentdebug.schema import (
    AgentEvent,
    AgentTrajectory,
    DiagnosticReport,
    FailureFinding,
    SEED_FAILURE_MODES,
)

DEFAULT_MIN_CONFIDENCE = 0.5
#: The judge's P3 default: a model that omits confidence is not neutral
#: evidence, and treating the omission as 0.5 would let it clear the threshold.
MISSING_CONFIDENCE = 0.3
MAX_FINDINGS_FOR_ATTRIBUTION = 20


SERVICE_JUDGE_SYSTEM_PROMPT = """你是 AgentDebugX-Judge，专门诊断 LLM agent 轨迹中的失败。

你会看到：任务目标、确定性结果信号、允许的失败类型、按时间排列的事件。

你的任务：找出每一步的失败，并从允许的失败类型中选择一个。只标记事件证据支持的问题；如果会话没有失败，返回空 findings 列表。

必须遵守：
1. 结果异常可能源于源数据本身；没有证据表明 SQL 或步骤有误时，使用 environment.* 类型，不要归咎 SQL。
2. 只描述看到的内容。SQL 或结果被截断（以“…”结尾）时，不得推断被截掉部分写了什么。
3. evidence 必须逐字摘自事件内容，不要改写或编造。
4. 输出 ONLY 一个 JSON 对象，不要 prose 或 markdown 围栏。
5. findings 最多 {max_findings} 条；字符串值内不要换行；JSON 必须完整。

输出格式：
{{"findings":[{{"event_id":"...", "step_index":N|null, "agent_name":"...",
  "failure_mode_id":"...", "confidence":0..1, "evidence":["..."], "rationale":"<short>"}}],
  "summary":"<short>"}}
"""


SERVICE_ATTR_SYSTEM_PROMPT = """你是 AgentDebugX-Attributor，负责在 agent 会话中指出失败根因步骤。

只输出一个 JSON 对象（不要 prose，不要 markdown 围栏）：
{
  "span_id": "<输入中的 event_id 或 null>",
  "step_index": <整数或 null>,
  "agent_name": "<输入中的 agent_name 或 null>",
  "confidence": <0 到 1 的浮点数>,
  "rationale": "<一到两句理由>",
  "evidence": ["<简短引用>", ...]
}

如果会话没有失败，所有字段返回 null，confidence 返回 0。

遵守：
1. 结果异常可能源于源数据本身；没有证据表明 SQL 或步骤有误时，考虑 environment.* 类问题，不要强行归咎 SQL。
2. 只描述看到的内容。SQL 或结果被截断（以“…”结尾）时，不要推断被截掉部分写了什么。
3. evidence 必须逐字摘自会话内容，不要改写或编造。
"""


class ServiceJudge(LLMJudgeAnalyzer):
    """Judge with Chinese prompts, result signals and a 0.3 missing-confidence default."""

    def __init__(
        self,
        llm: Any,
        *,
        language: str = 'zh',
        outcome_signals: Optional[OutcomeSignals] = None,
        **kwargs: Any,
    ) -> None:
        self.language = (language or 'zh').lower()
        self.outcome_signals = outcome_signals
        kwargs.setdefault('missing_confidence', MISSING_CONFIDENCE)
        if self.language == 'zh':
            kwargs.setdefault('system_prompt', SERVICE_JUDGE_SYSTEM_PROMPT)
        super().__init__(llm, **kwargs)

    def analyze(self, trajectory: AgentTrajectory) -> DiagnosticReport:
        if self.language == 'zh':
            # Deterministic graded context: the judged chunk keeps up to 1024
            # chars, everything outside it is clipped to 256. No extra LLM
            # calls -- StepCompressor is deliberately not used here (P4 only
            # asks for GradedContextBuilder).
            self.context_builder = build_graded_context_builder(trajectory.events)
        report = super().analyze(trajectory)
        if self.outcome_signals is not None:
            report.metadata['outcome_signals'] = self.outcome_signals.to_wire()
        return report

    def _render_user_prompt(
        self,
        trajectory: AgentTrajectory,
        chunk: List[AgentEvent],
    ) -> str:
        if self.language != 'zh':
            return super()._render_user_prompt(trajectory, chunk)
        modes_doc = '\n'.join(
            f'- {mode_id}: {mode.description}'
            for mode_id, mode in SEED_FAILURE_MODES.items()
        )
        if self.context_builder is not None:
            events_doc = self.context_builder.render_chunk(trajectory.events, chunk)
        else:
            events_doc = '\n'.join(self._render_event(event) for event in chunk)
        return (
            f'任务目标：{trajectory.goal!r}\n'
            f'框架：{trajectory.framework!r}\n\n'
            f'结果信号（确定性提取）：\n{_signals_text(self.outcome_signals)}\n\n'
            f'允许的失败类型：\n{modes_doc}\n\n'
            f'事件（当前段完整显示，其余段为摘要）：\n{events_doc}\n'
        )


class ServiceAttributor(AllAtOnceAttributor):
    """Attributor that sees result signals and only eligible detector findings."""

    def __init__(
        self,
        llm: Any,
        *,
        outcome_signals: Optional[OutcomeSignals] = None,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        language: str = 'zh',
        **kwargs: Any,
    ) -> None:
        self.outcome_signals = outcome_signals
        self.min_confidence = float(min_confidence)
        self.language = (language or 'zh').lower()
        if self.language == 'zh':
            kwargs.setdefault('system_prompt', SERVICE_ATTR_SYSTEM_PROMPT)
        super().__init__(llm, **kwargs)

    def attribute(
        self,
        trajectory: AgentTrajectory,
        findings: Optional[List[FailureFinding]] = None,
    ) -> AttributionResult:
        eligible = [
            finding for finding in (findings or [])
            if _confidence(finding) >= self.min_confidence
        ]
        eligible.sort(key=_confidence_key)
        result = super().attribute(
            trajectory, eligible[:MAX_FINDINGS_FOR_ATTRIBUTION]
        )
        primary = result.hypotheses[0] if result.hypotheses else None
        if primary is not None and _declined(primary):
            raw = dict(result.raw or {})
            raw['no_failure'] = True
            raw['no_failure_rationale'] = primary.rationale
            return AttributionResult(
                method=result.method,
                hypotheses=[],
                elapsed_ms=result.elapsed_ms,
                raw=raw,
                usage=result.usage,
            )
        return result

    def _render_prompt(
        self,
        trajectory: AgentTrajectory,
        findings: List[FailureFinding],
    ) -> str:
        if self.language != 'zh':
            return super()._render_prompt(trajectory, findings)
        events = self._attributable_events(list(trajectory.events))
        if not events:
            events = list(trajectory.events)
        events_doc = '\n'.join(
            f'Step {event.step_index if event.step_index is not None else "?"} '
            f'[{event.event_id}] {event.agent_name}: '
            f'{(str(event.output) if event.output is not None else str(event.input or ""))}'
            f'{(" | ERROR: " + str(event.error)) if event.error else ""}'
            for event in events
        ) or '(empty conversation)'
        findings_doc = '\n'.join(
            _render_finding(finding) for finding in findings[:MAX_FINDINGS_FOR_ATTRIBUTION]
        ) or '（无）'
        return (
            '你正在分析一个 agent 会话，判断任务是否失败；如果失败，指出根因步骤。\n'
            f'任务目标：{trajectory.goal or "(未记录)"}\n'
            f'{_step_semantics_note(trajectory)}'
            f'结果信号（确定性提取）：\n{_signals_text(self.outcome_signals)}\n\n'
            f'已发现的问题（可信度不低于 {self.min_confidence:.2f}，最多 '
            f'{MAX_FINDINGS_FOR_ATTRIBUTION} 条）：\n{findings_doc}\n\n'
            '请先根据结果信号和会话判断任务是否失败：\n'
            '- 如果未失败，所有字段返回 null，confidence 返回 0。\n'
            '- 如果失败，指出最早且真正导致失败的一步，并说明理由。\n\n'
            '注意：\n'
            '1. 结果异常可能源于源数据本身；没有证据表明 SQL 或步骤有误时，'
            '考虑 environment.* 类型。\n'
            '2. 只描述看到的内容。SQL 或结果被截断（以“…”结尾）时，'
            '不要推断被截掉部分写了什么。\n'
            '3. evidence 必须逐字摘自事件内容，不要改写或编造。\n\n'
            f'会话：\n\n{events_doc}\n'
        )


def build_graded_context_builder(
    events: List[AgentEvent],
    *,
    caps: Optional[Dict[str, int]] = None,
) -> GradedContextBuilder:
    """Deterministic graded pool: no StepCompressor calls.

    ``GradedContextBuilder`` degrades to ``clip_middle`` when a pool entry is
    missing; filling the pool explicitly makes that behaviour predictable for
    every event and keeps per-run cost at zero extra model calls.
    """

    caps = caps or {
        'th1': DEFAULT_TH1_CHARS,
        'th2': DEFAULT_TH2_CHARS,
        'th3': DEFAULT_TH3_CHARS,
    }
    pool: Dict[int, Dict[str, str]] = {}
    for position, event in enumerate(events):
        text = event_text(event)
        pool[position] = {
            tier: clip_middle(text, cap) for tier, cap in caps.items()
        }
    return GradedContextBuilder(
        pool,
        caps=caps,
        in_chunk_tier='th1',
        out_of_chunk_tier='th3',
        scope_to_chunk=True,
    )


# -- root cause selection (P4) -------------------------------------------

def mark_below_threshold(
    report: DiagnosticReport,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> None:
    """Keep every finding in the report, but mark the ones that cannot win."""

    for finding in report.findings:
        if _confidence(finding) < min_confidence:
            finding.metadata['below_threshold'] = True
        else:
            finding.metadata.pop('below_threshold', None)


def should_skip_attribution(
    report: DiagnosticReport,
    signals: Optional[OutcomeSignals],
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> bool:
    """P3: a normal outcome plus no credible finding means nothing to attribute."""

    if signals is None or signals.verdict != 'normal':
        return False
    return not any(
        _confidence(finding) >= min_confidence for finding in report.findings
    )


def normal_outcome_summary(signals: Optional[OutcomeSignals]) -> str:
    # Keep this one line: the brief renders ``summary`` inline, and the full
    # signal provenance is already in ``metadata.outcome_signals``.
    basis = signals.to_wire()['basis'] if signals is not None else []
    suffix = '（依据：%s）' % ', '.join(basis) if basis else ''
    return (
        '未发现明确失败：结果信号判定为正常结束%s，'
        '且没有可信度达到阈值的发现。' % suffix
    )


def select_root_finding(
    findings: List[FailureFinding],
    attribution: Optional[Any] = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> Optional[FailureFinding]:
    """Pick the root cause using the P4 order.

    1. no root cause when the attributor explicitly says the run did not fail;
    2. the attribution conclusion, when it lands on an eligible finding;
    3. otherwise the most confident eligible finding (ties -> earlier step);
    4. otherwise ``None``.
    """

    eligible = [
        finding for finding in findings
        if _confidence(finding) >= min_confidence
    ]
    if not eligible:
        return None
    # An explicit no-failure conclusion means the run recovered. Publishing a
    # root cause in that case would contradict the attribution panel.
    if _attribution_declines_failure(attribution):
        return None
    primary = _primary_hypothesis(attribution)
    if primary is not None:
        matched = _match_finding(primary, eligible)
        if matched is not None:
            return matched
    return sorted(eligible, key=_confidence_key)[0]


def apply_root_cause(
    report: DiagnosticReport,
    attribution: Optional[Any] = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> Optional[FailureFinding]:
    """Apply the P4 selector and write ``metadata.root_cause_kind``."""

    mark_below_threshold(report, min_confidence)
    root = select_root_finding(report.findings, attribution, min_confidence)
    report.root_cause_event_id = root.event_id if root is not None else None
    report.root_cause_agent = root.agent_name if root is not None else None
    report.root_cause_step_index = root.step_index if root is not None else None
    report.metadata['root_cause_kind'] = root_cause_kind(
        root.failure_mode.mode_id if root is not None else None
    )
    return root


# -- helpers --------------------------------------------------------------

def _confidence(finding: FailureFinding) -> float:
    return MISSING_CONFIDENCE if finding.confidence is None else float(finding.confidence)


def _confidence_key(finding: FailureFinding) -> tuple:
    step = finding.step_index if finding.step_index is not None else 10**9
    return (-_confidence(finding), step)


def _declined(blame: Blame) -> bool:
    return (
        blame.span_id is None
        and blame.step_index is None
        and blame.agent_name is None
    )


def _attribution_declines_failure(attribution: Optional[Any]) -> bool:
    if attribution is None:
        return False
    raw = _field(attribution, 'raw')
    return bool(isinstance(raw, dict) and raw.get('no_failure'))


def _primary_hypothesis(attribution: Optional[Any]) -> Optional[Any]:
    if attribution is None:
        return None
    hypotheses = getattr(attribution, 'hypotheses', None)
    if hypotheses:
        return hypotheses[0]
    if isinstance(attribution, dict):
        primary = attribution.get('primary')
        if isinstance(primary, dict):
            return primary
        items = attribution.get('hypotheses')
        if isinstance(items, list) and items:
            return items[0]
    return None


def _field(source: Any, key: str) -> Any:
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def _match_finding(primary: Any, eligible: List[FailureFinding]) -> Optional[FailureFinding]:
    event_id = _field(primary, 'span_id') or _field(primary, 'event_id')
    if event_id:
        for finding in eligible:
            if finding.event_id == event_id:
                return finding
    step = _field(primary, 'step_index')
    if step is None:
        return None
    agent = _field(primary, 'agent_name')
    candidates = [
        finding for finding in eligible
        if finding.step_index == step
        and (not agent or not finding.agent_name or finding.agent_name == agent)
    ]
    if not candidates:
        return None
    return sorted(candidates, key=_confidence_key)[0]


def _render_finding(finding: FailureFinding) -> str:
    source = (finding.metadata or {}).get('finding_source_label') or 'model'
    evidence = '; '.join(finding.evidence)[:200]
    return (
        f'- 来源={source} 类型={finding.failure_mode.mode_id} '
        f'agent={finding.agent_name} step={finding.step_index} '
        f'可信度={_confidence(finding):.2f} 证据={evidence}'
    )


def _signals_text(signals: Optional[OutcomeSignals]) -> str:
    if signals is None:
        return '（未提取）'
    return json.dumps(signals.to_wire(), ensure_ascii=False, indent=2)


__all__ = [
    'DEFAULT_MIN_CONFIDENCE',
    'MAX_FINDINGS_FOR_ATTRIBUTION',
    'MISSING_CONFIDENCE',
    'SERVICE_ATTR_SYSTEM_PROMPT',
    'SERVICE_JUDGE_SYSTEM_PROMPT',
    'ServiceAttributor',
    'ServiceJudge',
    'apply_root_cause',
    'build_graded_context_builder',
    'mark_below_threshold',
    'normal_outcome_summary',
    'select_root_finding',
    'should_skip_attribution',
]
