"""Startup config validation: bad combinations fail at boot with every problem
named, good ones produce a summary in which secrets appear only as set/unset.
"""
from __future__ import annotations

import pytest

from deliveryops.config import ConfigError, validate_config


def test_the_empty_environment_is_the_offline_default():
    summary = validate_config({})
    assert summary["llm"] == "fake"
    assert summary["retriever"] == "lexical"
    assert summary["checkpointer"] == "memory"
    assert summary["database_url"] == "unset"


def test_a_consistent_production_shaped_env_passes():
    summary = validate_config({
        "DELIVERYOPS_LLM": "chat",
        "DELIVERYOPS_PROVIDER": "anthropic",
        "DELIVERYOPS_MODEL": "claude-sonnet-4-6",
        "DATABASE_URL": "postgresql://u:p@db/deliveryops",
        "DELIVERYOPS_RETRIEVER": "pgvector",
        "DELIVERYOPS_CHECKPOINTER": "postgres",
        "DELIVERYOPS_MAX_RUN_COST_USD": "2.50",
    })
    assert summary["llm"] == "chat:anthropic/claude-sonnet-4-6"
    assert summary["checkpointer"] == "postgres"
    assert summary["database_url"] == "set"


def test_pgvector_without_a_database_fails_at_validation():
    with pytest.raises(ConfigError, match="DELIVERYOPS_RETRIEVER=pgvector "
                                           "requires DATABASE_URL"):
        validate_config({"DELIVERYOPS_RETRIEVER": "pgvector"})


def test_postgres_checkpointer_without_a_database_fails():
    with pytest.raises(ConfigError, match="CHECKPOINTER=postgres requires DATABASE_URL"):
        validate_config({"DELIVERYOPS_CHECKPOINTER": "postgres"})


def test_an_unknown_llm_mode_fails():
    with pytest.raises(ConfigError, match="DELIVERYOPS_LLM 'bogus'"):
        validate_config({"DELIVERYOPS_LLM": "bogus"})


def test_a_non_numeric_cap_fails_at_validation_not_mid_run():
    with pytest.raises(ConfigError, match="DELIVERYOPS_MAX_RUN_COST_USD"):
        validate_config({"DELIVERYOPS_MAX_RUN_COST_USD": "a dollar"})


def test_partial_jira_config_fails_via_the_adapters_own_rule():
    with pytest.raises(ConfigError, match="jira intake is partially configured"):
        validate_config({"JIRA_SITE": "https://example.atlassian.net"})


def test_all_problems_are_reported_together():
    with pytest.raises(ConfigError) as excinfo:
        validate_config({
            "DELIVERYOPS_LLM": "bogus",
            "DELIVERYOPS_RETRIEVER": "hybrid",
            "DELIVERYOPS_DB_POOL_MAX": "lots",
        })
    message = str(excinfo.value)
    assert "DELIVERYOPS_LLM" in message
    assert "DELIVERYOPS_RETRIEVER=hybrid" in message
    assert "DELIVERYOPS_DB_POOL_MAX" in message


def test_the_summary_never_contains_a_secret_value(caplog):
    secret = "sk-ant-must-not-appear"
    dsn = "postgresql://user:hunter2@db/deliveryops"
    with caplog.at_level("INFO", logger="deliveryops.config"):
        summary = validate_config({"ANTHROPIC_API_KEY": secret, "DATABASE_URL": dsn})
    assert summary["anthropic_api_key"] == "set"
    assert summary["database_url"] == "set"
    assert secret not in str(summary)
    assert "hunter2" not in str(summary)
    assert secret not in caplog.text
    assert "hunter2" not in caplog.text
