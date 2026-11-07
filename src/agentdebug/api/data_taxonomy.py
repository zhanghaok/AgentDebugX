"""Data-scenario failure modes and root-cause kind mapping.

The OSS taxonomy in :mod:`agentdebug.schema.taxonomy` covers agent-internal
failures. DataAgent sessions can also fail because the requirement was
misread, the metric definition was wrong, or the source data itself is the
problem. The intranet service adds those modes at startup so the LLM judge
sees them in its allowed list (issue #3, P2).

Registration is explicit and idempotent: importing this module does not mutate
the global taxonomy, ``register_data_taxonomy()`` does. That keeps the OSS
defaults untouched for callers that do not run the local service.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from agentdebug.schema import FailureMode
from agentdebug.schema.taxonomy import SEED_FAILURE_MODES

#: Failure modes added by the data-scenario taxonomy.
#:
#: Each description carries one positive and one negative example. The judge
#: prompt enumerates ``SEED_FAILURE_MODES`` directly, so these definitions are
#: the only place a near-miss distinction has to live.
DATA_FAILURE_MODES: Dict[str, FailureMode] = {
    'data.requirement_misread': FailureMode(
        mode_id='data.requirement_misread',
        name='Requirement misread',
        family='data',
        description=(
            '指标、维度、范围或交付形式与用户原话或后续修改不符。'
            '正例：用户要求“最近 7 天”，SQL 却查了自然月。'
            '反例：用户没有给出时间范围，agent 选择了一个合理默认值并明确说明。'
        ),
        signals=['requirement mismatch', 'wrong dimension', 'wrong scope'],
        suggestion_templates=[
            'Restate the requirement and the chosen interpretation before running the query.',
            'Add a pre-execution checklist that compares each requested dimension, metric and range.',
        ],
        source='DataAgent (intranet)',
    ),
    'data.metric_definition_error': FailureMode(
        mode_id='data.metric_definition_error',
        name='Metric definition error',
        family='data',
        description=(
            '计算公式、分子分母、去重口径与项目知识或 spec 不一致。'
            '正例：用订单数代替去重用户数计算转化率。'
            '反例：按 spec 的公式计算，结果与预期不同只是因为数据本身异常。'
        ),
        signals=['metric formula mismatch', 'wrong numerator/denominator', 'wrong dedup grain'],
        suggestion_templates=[
            'Look up the metric definition in project knowledge before writing the expression.',
            'Record the formula used next to the result so it can be checked against the spec.',
        ],
        source='DataAgent (intranet)',
    ),
    'data.wrong_table_or_field': FailureMode(
        mode_id='data.wrong_table_or_field',
        name='Wrong table or field',
        family='data',
        description=(
            '使用了含义不符的表或字段。'
            '正例：用订单表的金额字段代替支付表的实收金额。'
            '反例：使用了同名字段，但确实来自正确的表。'
        ),
        signals=['wrong table', 'wrong column', 'semantic field mismatch'],
        suggestion_templates=[
            'Resolve each table and field against the catalog before using it.',
            'Carry table/field descriptions into the query planning context.',
        ],
        source='DataAgent (intranet)',
    ),
    'data.join_error': FailureMode(
        mode_id='data.join_error',
        name='Join error',
        family='data',
        description=(
            '关联键或关联方式错误，导致行数重复或数据丢失。'
            '正例：用非唯一键关联导致结果行数膨胀。'
            '反例：使用唯一键关联，结果行数与预期一致。'
        ),
        signals=['wrong join key', 'row explosion', 'lost rows'],
        suggestion_templates=[
            'Verify join-key uniqueness before joining.',
            'Compare row counts before and after each join.',
        ],
        source='DataAgent (intranet)',
    ),
    'data.filter_or_time_range_error': FailureMode(
        mode_id='data.filter_or_time_range_error',
        name='Filter or time-range error',
        family='data',
        description=(
            '过滤条件、时间起止与要求不符。'
            '正例：缺少上界导致数据延伸到未来。'
            '反例：按用户要求只过滤了有效记录。'
        ),
        signals=['missing filter', 'wrong date bound', 'wrong time window'],
        suggestion_templates=[
            'Enumerate the required filters and time bounds before executing the query.',
            'Echo the effective time range next to the result.',
        ],
        source='DataAgent (intranet)',
    ),
    'data.aggregation_grain_error': FailureMode(
        mode_id='data.aggregation_grain_error',
        name='Aggregation grain error',
        family='data',
        description=(
            '分组粒度不对，或对比率、均值再次求和。'
            '正例：对已经算好的比率列求和。'
            '反例：先按公式求分子和分母，再相除得到比率。'
        ),
        signals=['wrong group grain', 'ratio summed', 'average of averages'],
        suggestion_templates=[
            'State the target grain before aggregating and check the GROUP BY against it.',
            'Keep numerators and denominators separate until the final ratio.',
        ],
        source='DataAgent (intranet)',
    ),
    'data.knowledge_unused': FailureMode(
        mode_id='data.knowledge_unused',
        name='Project knowledge unused',
        family='data',
        description=(
            '项目知识里有口径或表说明，但没有查阅或没有遵循。'
            '正例：知识库已经说明口径，agent 未检索就自行定义。'
            '反例：检索并遵循了知识库中的口径。'
        ),
        signals=['knowledge not retrieved', 'knowledge ignored', 'self-defined metric'],
        suggestion_templates=[
            'Search project knowledge for metric and table definitions before planning.',
            'Cite the knowledge entry that supports the chosen definition.',
        ],
        source='DataAgent (intranet)',
    ),
    'data.result_unchecked': FailureMode(
        mode_id='data.result_unchecked',
        name='Result not self-checked',
        family='data',
        description=(
            '结果明显异常（为空、量级不对）却没有检查就交付。'
            '正例：SQL 返回空集仍直接给出结论。'
            '反例：发现空结果后检查了过滤条件并说明原因。'
        ),
        signals=['empty result delivered', 'implausible magnitude', 'no sanity check'],
        suggestion_templates=[
            'Run a sanity check on row count and magnitude before presenting a result.',
            'When a result is empty, inspect the filters and time range before answering.',
        ],
        source='DataAgent (intranet)',
    ),
    'data.artifact_incomplete': FailureMode(
        mode_id='data.artifact_incomplete',
        name='Incomplete artifact',
        family='data',
        description=(
            '要求的结果数据、报告或文件缺失。'
            '正例：要求导出 CSV，最终只给了文字说明。'
            '反例：交付了全部要求的产物。'
        ),
        signals=['missing artifact', 'partial deliverable', 'report not produced'],
        suggestion_templates=[
            'Enumerate the requested deliverables and check each one before finishing.',
            'Surface missing artifacts explicitly instead of silently omitting them.',
        ],
        source='DataAgent (intranet)',
    ),
    'environment.source_data_issue': FailureMode(
        mode_id='environment.source_data_issue',
        name='Source data issue',
        family='environment',
        description=(
            '源表数据本身缺失、重复或构造有误，SQL 正确也得不到正确结果。'
            '正例：源表缺少某天分区，正确 SQL 也查不到数据。'
            '反例：SQL 写错导致漏数据。'
        ),
        signals=['missing partition', 'duplicate source rows', 'corrupt source data'],
        suggestion_templates=[
            'Check source-table freshness, partition coverage and duplicate rate before blaming the query.',
            'Record data-quality checks alongside the query result.',
        ],
        source='DataAgent (intranet)',
    ),
    'environment.data_range_insufficient': FailureMode(
        mode_id='environment.data_range_insufficient',
        name='Insufficient data coverage',
        family='environment',
        description=(
            '源数据只覆盖部分时间或对象，不足以满足要求。'
            '正例：源表只到 W43，却要求分析到 W52。'
            '反例：源数据完整，只是 SQL 过滤掉了部分范围。'
        ),
        signals=['partial time coverage', 'missing entities', 'range too short'],
        suggestion_templates=[
            'Compare the requested range with the source-table coverage before analyzing.',
            'Report coverage gaps explicitly instead of extrapolating.',
        ],
        source='DataAgent (intranet)',
    ),
}

#: Root-cause kind for every mode the service can emit.
#:
#: ``data.*`` failures are agent-side (the data existed, the agent used it
#: wrongly). ``environment.*`` failures are environmental. The three
#: ``system.*`` modes are also environmental: the tool, model limit or runtime
#: environment failed, not the agent's reasoning (issue #1).
ROOT_CAUSE_KIND: Dict[str, str] = {
    'data.requirement_misread': 'agent',
    'data.metric_definition_error': 'agent',
    'data.wrong_table_or_field': 'agent',
    'data.join_error': 'agent',
    'data.filter_or_time_range_error': 'agent',
    'data.aggregation_grain_error': 'agent',
    'data.knowledge_unused': 'agent',
    'data.result_unchecked': 'agent',
    'data.artifact_incomplete': 'agent',
    'environment.source_data_issue': 'environment',
    'environment.data_range_insufficient': 'environment',
    'system.tool_execution_error': 'environment',
    'system.llm_limit': 'environment',
    'system.environment_error': 'environment',
}

_DEFAULT_KIND = 'agent'


def register_data_taxonomy() -> List[str]:
    """Merge the data-scenario modes into ``SEED_FAILURE_MODES``.

    Idempotent: modes already present are left untouched, so repeated service
    starts or test fixtures cannot duplicate or overwrite a mode. Returns the
    IDs that were actually added.
    """

    added: List[str] = []
    for mode_id, mode in DATA_FAILURE_MODES.items():
        if mode_id not in SEED_FAILURE_MODES:
            SEED_FAILURE_MODES[mode_id] = mode
            added.append(mode_id)
    return added


def root_cause_kind(mode_id: Optional[str]) -> str:
    """Return ``agent``, ``environment`` or ``none`` for a failure mode ID."""

    if not mode_id:
        return 'none'
    return ROOT_CAUSE_KIND.get(str(mode_id), _DEFAULT_KIND)


__all__ = [
    'DATA_FAILURE_MODES',
    'ROOT_CAUSE_KIND',
    'register_data_taxonomy',
    'root_cause_kind',
]
