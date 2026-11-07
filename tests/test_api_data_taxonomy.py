"""Tests for the P2 data-scenario taxonomy (issue #3)."""

from __future__ import annotations

import pytest

from agentdebug.api.data_taxonomy import (
    DATA_FAILURE_MODES,
    ROOT_CAUSE_KIND,
    register_data_taxonomy,
    root_cause_kind,
)
from agentdebug.api.localization import FAILURE_MODE_ZH, assert_full_coverage
from agentdebug.schema.taxonomy import SEED_FAILURE_MODES


@pytest.fixture
def isolated_taxonomy():
    """Remove data modes for the test, then restore whatever was there."""

    saved = dict(SEED_FAILURE_MODES)
    for mode_id in DATA_FAILURE_MODES:
        SEED_FAILURE_MODES.pop(mode_id, None)
    yield
    SEED_FAILURE_MODES.clear()
    SEED_FAILURE_MODES.update(saved)


def test_register_data_taxonomy_is_explicit_and_idempotent(isolated_taxonomy):
    before = len(SEED_FAILURE_MODES)
    added = register_data_taxonomy()
    assert added == list(DATA_FAILURE_MODES)
    assert len(SEED_FAILURE_MODES) == before + len(DATA_FAILURE_MODES)
    assert register_data_taxonomy() == []
    assert len(SEED_FAILURE_MODES) == before + len(DATA_FAILURE_MODES)


def test_registered_modes_have_chinese_translations(isolated_taxonomy):
    register_data_taxonomy()
    assert_full_coverage()
    assert set(SEED_FAILURE_MODES) <= set(FAILURE_MODE_ZH)
    for mode_id in DATA_FAILURE_MODES:
        translation = FAILURE_MODE_ZH[mode_id]
        assert translation['name_zh']
        assert len(translation['suggestion_templates_zh']) == len(
            DATA_FAILURE_MODES[mode_id].suggestion_templates
        )


def test_each_data_mode_has_positive_and_negative_examples():
    for mode_id, mode in DATA_FAILURE_MODES.items():
        assert mode.mode_id == mode_id
        assert mode.family in ('data', 'environment')
        assert '正例：' in mode.description, mode_id
        assert '反例：' in mode.description, mode_id
        assert mode.suggestion_templates, mode_id


def test_root_cause_kind_mapping():
    assert root_cause_kind('data.join_error') == 'agent'
    assert root_cause_kind('environment.source_data_issue') == 'environment'
    assert root_cause_kind('environment.data_range_insufficient') == 'environment'
    assert root_cause_kind('system.tool_execution_error') == 'environment'
    assert root_cause_kind('action.wrong_tool') == 'agent'
    assert root_cause_kind(None) == 'none'
    # Every mode in the mapping is one the service can actually emit.
    assert set(ROOT_CAUSE_KIND) <= set(DATA_FAILURE_MODES) | {
        'system.tool_execution_error',
        'system.llm_limit',
        'system.environment_error',
    }
