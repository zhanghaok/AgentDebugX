"""Chinese localization layer for the diagnosis output.

The intranet fork adds ``name_zh`` / ``suggestion_templates_zh`` to every
failure mode and renders Reflexion proposals in Chinese; the upstream OSS
package only ships English. This module restores that behaviour:

* :data:`FAILURE_MODE_ZH` — translations for all seeded failure modes
  (``name_zh`` plus suggestion templates kept index-aligned with the
  English ones in ``agentdebug.schema.taxonomy``).
* :func:`localize_failure_mode` — wire serializer adding the ``_zh`` fields.
* :class:`ZhReflexionSuggestion` — ``ReflexionSuggestion`` subclass whose
  proposal text is Chinese. Only the wrapper labels are translated; the
  suggestion directive itself stays whatever the detector emitted, exactly
  like the intranet sample.
"""

from __future__ import annotations

from typing import Any, Dict, List

from agentdebug.diagnose.recover.recovery import FixProposal, ReflexionSuggestion
from agentdebug.schema import (
    AgentTrajectory,
    DiagnosticReport,
    FailureFinding,
    FailureMode,
    confidence_or_default,
    new_id,
)

FAILURE_MODE_ZH: Dict[str, Dict[str, Any]] = {
    'memory.retrieval_failure': {
        'name_zh': '记忆检索失败',
        'suggestion_templates_zh': [
            '将缺失状态持久化为结构化记忆，并附加到下一轮规划中。',
            '在依据检索到的上下文行动前，增加检索质量校验。',
        ],
    },
    'memory.hallucination': {
        'name_zh': '记忆幻觉',
        'suggestion_templates_zh': [
            '要求记忆读取在使用前标注来源事件或工件。',
            '在 agent 提示中将观测到的状态与推断出的状态分开。',
        ],
    },
    'reflection.progress_misjudge': {
        'name_zh': '进度误判',
        'suggestion_templates_zh': [
            '在终止前加入外部任务校验器。',
            '跟踪明确的成功标准，并将当前状态与每条标准逐一比对。',
        ],
    },
    'reflection.causal_misattribution': {
        'name_zh': '因果误归因',
        'suggestion_templates_zh': [
            '用反事实重放或消融实验验证被归责的步骤是否真的导致失败。',
            '在反思中为每个因果论断保留证据链接。',
        ],
    },
    'planning.constraint_ignorance': {
        'name_zh': '约束忽视',
        'suggestion_templates_zh': [
            '将任务与工具约束编译为动作前检查。',
            '当必要约束无法验证时，直接失败（fail closed）。',
        ],
    },
    'planning.impossible_action': {
        'name_zh': '不可行动作',
        'suggestion_templates_zh': [
            '将环境可执行能力暴露为机器可读的动作空间。',
            '检测到不可行动作后，从上一个有效状态重新规划。',
        ],
    },
    'planning.inefficient_plan': {
        'name_zh': '低效规划',
        'suggestion_templates_zh': [
            '对工具调用与状态变化增加循环检测。',
            '当相同的「状态-动作」模式重复出现时触发重新规划。',
        ],
    },
    'action.wrong_tool': {
        'name_zh': '工具选择错误',
        'suggestion_templates_zh': [
            '为相似工具补充路由示例与反例。',
            '在执行高影响动作前，增加工具选择校验器。',
        ],
    },
    'action.invalid_action': {
        'name_zh': '无效动作',
        'suggestion_templates_zh': [
            '执行前校验动作 Schema，并返回可操作的修复提示。',
            '记录被拒绝的动作，用于改进工具描述与示例。',
        ],
    },
    'action.format_error': {
        'name_zh': '格式错误',
        'suggestion_templates_zh': [
            '强制结构化输出，并利用校验错误信息重试。',
            '在可行时优先使用类型化工具 API，而不是自由格式命令串。',
        ],
    },
    'action.parameter_error': {
        'name_zh': '参数错误',
        'suggestion_templates_zh': [
            '按工具 Schema 校验参数，并主动索取缺失的用户/上下文字段。',
            '记录参数来源，使编造的参数在轨迹中可见。',
        ],
    },
    'system.tool_execution_error': {
        'name_zh': '工具执行错误',
        'suggestion_templates_zh': [
            '采集工具 stderr/状态/耗时，区分可重试与不可重试的失败。',
            '为有副作用的工具增加幂等键与回滚。',
        ],
    },
    'system.llm_limit': {
        'name_zh': '模型限制',
        'suggestion_templates_zh': [
            '对超长上下文做摘要或分片，并显式记录信息损失。',
            '将超限失败路由到备用模型或更小的提示。',
        ],
    },
    'system.environment_error': {
        'name_zh': '环境错误',
        'suggestion_templates_zh': [
            '记录环境快照与动作前后的状态差异。',
            '当视觉/浏览器观测不稳定时，回退到稳定 API 或可访问性树。',
        ],
    },
    'multiagent.handoff_loss': {
        'name_zh': '交接上下文丢失',
        'suggestion_templates_zh': [
            '让交接载荷类型化，包含目标、约束、证据、置信度与未决问题。',
            '在继续之前，验证接收方能够复述关键约束。',
        ],
    },
    'multiagent.role_drift': {
        'name_zh': '角色漂移',
        'suggestion_templates_zh': [
            '为每个 agent 定义职责边界与允许的动作。',
            '增加监督者检查角色越界与重复工作。',
        ],
    },
    'verification.missing_task_validation': {
        'name_zh': '缺失任务校验',
        'suggestion_templates_zh': [
            '增加独立于执行 agent 的最终状态校验器。',
            '要求在执行开始前，将任务特定的成功标准写入轨迹。',
        ],
    },
    'verification.premature_stop': {
        'name_zh': '提前停止',
        'suggestion_templates_zh': [
            '在运行 Schema 中区分成功、放弃与达到步数上限等停止条件。',
            '当终止并非经校验的成功时，触发恢复规划。',
        ],
    },
    'multimodal.perception_error': {
        'name_zh': '多模态感知错误',
        'suggestion_templates_zh': [
            '在保存提取文本的同时，保存原始多模态工件与置信度。',
            'UI 任务使用冗余感知通道（可访问性树 + 截图）。',
        ],
    },
    'observation.misread': {
        'name_zh': '观测误读',
        'suggestion_templates_zh': [
            '在行动前，将解析出的值与原始响应一并回显。',
            '在解析边界而非下游断言单位与类型。',
        ],
    },
    'observation.ignored': {
        'name_zh': '观测忽略',
        'suggestion_templates_zh': [
            '要求下一步显式引用最近的观测。',
            '将未被使用的诊断信号呈现到规划上下文中。',
        ],
    },
    'observation.grounding_fail': {
        'name_zh': '观测接地失败',
        'suggestion_templates_zh': [
            '跨步骤传递实体标识符（而非显示名称）。',
            '在从目标提取信息前，先确认已解析到正确的目标。',
        ],
    },
    'observation.timing': {
        'name_zh': '观测时序错误',
        'suggestion_templates_zh': [
            '在消费响应前检查就绪/状态字段。',
            '在工具契约中区分空结果与未完成结果。',
        ],
    },
    # Data-scenario modes (P2, issue #3). Kept aligned with
    # ``api/data_taxonomy.py``; ``assert_full_coverage()`` checks the whole set
    # once the service has registered the data taxonomy.
    'data.requirement_misread': {
        'name_zh': '需求理解偏差',
        'suggestion_templates_zh': [
            '在执行前复述需求，并写明对指标、维度和范围的解释。',
            '增加执行前检查清单，逐项核对用户要求的维度、指标和时间范围。',
        ],
    },
    'data.metric_definition_error': {
        'name_zh': '指标口径错误',
        'suggestion_templates_zh': [
            '编写表达式前先查阅项目知识中的指标定义。',
            '把实际使用的公式与结果一并记录，便于和 spec 对照。',
        ],
    },
    'data.wrong_table_or_field': {
        'name_zh': '表或字段选错',
        'suggestion_templates_zh': [
            '使用表和字段前，先在数据目录中确认其含义。',
            '把表/字段说明带入查询规划上下文。',
        ],
    },
    'data.join_error': {
        'name_zh': '关联条件错误',
        'suggestion_templates_zh': [
            '关联前先校验关联键的唯一性。',
            '每次关联前后对比行数，发现膨胀或丢失立即停下检查。',
        ],
    },
    'data.filter_or_time_range_error': {
        'name_zh': '过滤或时间范围错误',
        'suggestion_templates_zh': [
            '执行前列出必需的过滤条件和时间边界。',
            '在结果旁回显实际生效的时间范围。',
        ],
    },
    'data.aggregation_grain_error': {
        'name_zh': '聚合粒度错误',
        'suggestion_templates_zh': [
            '聚合前先声明目标粒度，并检查 GROUP BY 是否匹配。',
            '把分子和分母分开计算，最后一步再做除法得到比率。',
        ],
    },
    'data.knowledge_unused': {
        'name_zh': '未使用已有知识',
        'suggestion_templates_zh': [
            '规划前先检索项目知识中的指标和表说明。',
            '引用支撑当前口径的知识条目。',
        ],
    },
    'data.result_unchecked': {
        'name_zh': '结果未自查',
        'suggestion_templates_zh': [
            '交付前检查行数和量级是否合理。',
            '结果为空时先检查过滤条件和时间范围，再给出结论。',
        ],
    },
    'data.artifact_incomplete': {
        'name_zh': '产物不完整',
        'suggestion_templates_zh': [
            '结束前逐项核对要求的产物是否齐全。',
            '缺失的产物要显式说明，不要静默省略。',
        ],
    },
    'environment.source_data_issue': {
        'name_zh': '源数据缺失或异常',
        'suggestion_templates_zh': [
            '归咎查询前，先检查源表的新鲜度、分区覆盖和重复率。',
            '把数据质量检查结果和查询结果一起记录。',
        ],
    },
    'environment.data_range_insufficient': {
        'name_zh': '数据覆盖范围不足',
        'suggestion_templates_zh': [
            '分析前对比用户要求的时间范围和源表实际覆盖范围。',
            '显式报告覆盖缺口，不要外推。',
        ],
    },
}


def localize_failure_mode(mode: FailureMode) -> Dict[str, Any]:
    """Serialize a failure mode, appending the ``_zh`` fields."""
    english_templates: List[str] = list(mode.suggestion_templates or [])
    translation = FAILURE_MODE_ZH.get(mode.mode_id)
    return {
        'mode_id': mode.mode_id,
        'name': mode.name,
        'family': mode.family,
        'description': mode.description,
        'signals': list(mode.signals or []),
        'suggestion_templates': english_templates,
        'source': mode.source,
        'name_zh': translation['name_zh'] if translation else mode.name,
        'suggestion_templates_zh': (
            list(translation['suggestion_templates_zh']) if translation else english_templates
        ),
    }


class ZhReflexionSuggestion(ReflexionSuggestion):
    """Reflexion proposals with Chinese wrapper text (intranet behaviour)."""

    def _build_proposal(
        self, trajectory: AgentTrajectory, finding: FailureFinding
    ) -> Any:
        goal = trajectory.goal or '(no goal recorded)'
        framework = trajectory.framework or '(framework not declared)'
        evidence_block = '\n'.join('  - %s' % e for e in finding.evidence) or '  (none)'
        suggestion_template = (
            finding.suggestion
            or (finding.failure_mode.suggestion_templates[0]
                if finding.failure_mode.suggestion_templates
                else 'Inspect the offending step and constrain the agent at that point.')
        )
        translation = FAILURE_MODE_ZH.get(finding.failure_mode.mode_id) or {}
        name_zh = translation.get('name_zh') or finding.failure_mode.name
        reflection = (
            f'任务：{goal}\n'
            f'框架：{framework}\n'
            f'观测到的失败模式：{finding.failure_mode.mode_id}'
            f'（{name_zh}）\n'
            f'定位：agent={finding.agent_name}，step={finding.step_index}，'
            f'event_id={finding.event_id}\n'
            f'证据：\n{evidence_block}\n'
            f'下次请按以下方式处理：\n  {suggestion_template}\n'
        )
        return FixProposal(
            proposal_id=new_id('fix'),
            recoverer_id=self.id,
            target_event_id=finding.event_id,
            summary=(
                f'{finding.failure_mode.mode_id} 在 step {finding.step_index} '
                f'的 Reflexion 重试提示'
            ),
            rationale=(
                'Reflexion（Shinn 等，NeurIPS 2023）将一次失败转化为一段'
                '口头提示，追加到下一次尝试中。'
            ),
            confidence=min(0.9, max(0.1, confidence_or_default(finding.confidence))),
            suggestion_text=reflection,
            side_effects=['memory.write'],
            requires_human_approval=False,
        )


def assert_full_coverage() -> None:
    """Fail loudly if a seeded failure mode is missing a translation."""
    from agentdebug.schema.taxonomy import SEED_FAILURE_MODES

    missing = sorted(set(SEED_FAILURE_MODES) - set(FAILURE_MODE_ZH))
    if missing:
        raise AssertionError('missing zh translations for: %s' % ', '.join(missing))
