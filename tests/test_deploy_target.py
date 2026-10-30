"""DELIVERYOPS_DEPLOY_TARGET: cloud targets are strict, local is untouched.

A cloud deploy that silently falls back to in-memory state or the offline
keyword model is worse than one that refuses to boot - these are the rules
config.py enforces for target gcp/aws, and the absence of rules for local.
"""
from __future__ import annotations

import logging

import pytest

from deliveryops.config import ConfigError, validate_config

_DSN = "postgresql://u:p@db/deliveryops"
_CLOUD_ENV = {
    "DATABASE_URL": _DSN,
    "DELIVERYOPS_LLM": "chat",
    "DELIVERYOPS_API_TOKENS": '{"tok": {"name": "a", "role": "senior"}}',
}


def test_an_unknown_deploy_target_fails():
    with pytest.raises(ConfigError, match="DELIVERYOPS_DEPLOY_TARGET 'azure'"):
        validate_config({"DELIVERYOPS_DEPLOY_TARGET": "azure"})


@pytest.mark.parametrize("target", ["gcp", "aws"])
def test_a_cloud_target_without_a_database_fails(target):
    with pytest.raises(ConfigError, match="requires DATABASE_URL"):
        validate_config({"DELIVERYOPS_DEPLOY_TARGET": target,
                         "DELIVERYOPS_LLM": "chat"})


@pytest.mark.parametrize("target", ["gcp", "aws"])
def test_a_cloud_target_rejects_the_fake_model(target):
    with pytest.raises(ConfigError, match="DELIVERYOPS_ALLOW_FAKE_LLM=1"):
        validate_config({"DELIVERYOPS_DEPLOY_TARGET": target,
                         "DATABASE_URL": _DSN})  # DELIVERYOPS_LLM defaults to fake


@pytest.mark.parametrize("target", ["gcp", "aws"])
def test_the_fake_model_override_is_explicit_and_works(target):
    summary = validate_config({
        "DELIVERYOPS_DEPLOY_TARGET": target,
        "DATABASE_URL": _DSN,
        "DELIVERYOPS_ALLOW_FAKE_LLM": "1",
        "DELIVERYOPS_API_TOKENS": _CLOUD_ENV["DELIVERYOPS_API_TOKENS"],
    })
    assert summary["deploy_target"] == target
    assert summary["llm"] == "fake"


@pytest.mark.parametrize("target", ["gcp", "aws"])
def test_a_cloud_target_without_api_tokens_warns_but_boots(target, caplog):
    env = dict(_CLOUD_ENV, DELIVERYOPS_DEPLOY_TARGET=target)
    del env["DELIVERYOPS_API_TOKENS"]
    with caplog.at_level(logging.WARNING, logger="deliveryops.config"):
        summary = validate_config(env)
    assert summary["deploy_target"] == target
    assert "DELIVERYOPS_API_TOKENS is unset" in caplog.text


@pytest.mark.parametrize("target", ["gcp", "aws"])
def test_a_cloud_target_with_api_tokens_does_not_warn(target, caplog):
    env = dict(_CLOUD_ENV, DELIVERYOPS_DEPLOY_TARGET=target)
    with caplog.at_level(logging.WARNING, logger="deliveryops.config"):
        validate_config(env)
    assert "DELIVERYOPS_API_TOKENS is unset" not in caplog.text


def test_a_fully_specified_cloud_env_passes():
    summary = validate_config(dict(_CLOUD_ENV, DELIVERYOPS_DEPLOY_TARGET="aws"))
    assert summary["deploy_target"] == "aws"
    assert summary["database_url"] == "set"


def test_local_is_unaffected_by_the_cloud_rules():
    # No DATABASE_URL, the fake model, no tokens: all fine locally.
    summary = validate_config({"DELIVERYOPS_DEPLOY_TARGET": "local"})
    assert summary["deploy_target"] == "local"
    assert summary["llm"] == "fake"


def test_local_does_not_warn_about_tokens(caplog):
    with caplog.at_level(logging.WARNING, logger="deliveryops.config"):
        validate_config({"DELIVERYOPS_DEPLOY_TARGET": "local"})
    assert "DELIVERYOPS_API_TOKENS" not in caplog.text


def test_the_default_target_is_local():
    assert validate_config({})["deploy_target"] == "local"
