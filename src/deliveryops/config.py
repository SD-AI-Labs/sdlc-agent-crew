"""Startup configuration validation: one pass, at API and worker boot, that
checks the whole known env surface and logs a redacted summary.

The individual factories already fail loudly on their own slice (an unknown
DELIVERYOPS_LLM, pgvector without DATABASE_URL, a partial Jira config). This
module's job is timing and completeness: all of those rules run at startup, in
one place, before the first request or poll - not lazily on first use - plus
the cross-field rules no single factory owns (checkpointer vs DATABASE_URL)
and the numeric knobs that are otherwise only parsed mid-run, where a typo
would surface as a crashed run instead of a refused boot.

Every problem found is reported together. Secret values are never logged or
returned: the summary records presence/absence for them, values for the rest.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping

log = logging.getLogger("deliveryops.config")

# Env vars whose values are secrets. The summary reports set/unset for these.
SECRET_VARS = (
    "DATABASE_URL",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "JIRA_API_TOKEN",
    "GITHUB_TOKEN",
    "DELIVERYOPS_API_TOKENS",
    "DELIVERYOPS_SLACK_WEBHOOK_URL",
)

# Mirrors of the factories' accepted values (llm/factory.py, knowledge/embedder.py).
# The factories still raise on their own; these exist so boot reports every
# problem at once instead of one per restart.
_LLM_MODES = {"fake", "chat", "anthropic", "openai", "google", "google_genai"}
_PROVIDERS = {"anthropic", "openai", "google", "google_genai"}
_EMBEDDERS = {"hash", "langchain"}
_RETRIEVERS = {"lexical", "pgvector", "hybrid"}
_CHECKPOINTERS = {"", "memory", "postgres"}


class ConfigError(RuntimeError):
    """The environment is inconsistent; the message names every problem found."""


def _check_number(env: Mapping[str, str], name: str, kind: type,
                  errors: list[str]) -> None:
    raw = env.get(name, "")
    if not raw:
        return
    try:
        kind(raw)
    except ValueError:
        errors.append(f"{name} must be a number, got {raw!r}")


def validate_config(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Validate the env surface; raise ConfigError listing every problem.

    Returns (and logs) a redacted summary when the configuration is usable.
    """
    env = os.environ if env is None else env
    errors: list[str] = []

    mode = env.get("DELIVERYOPS_LLM", "fake").lower()
    if mode not in _LLM_MODES:
        errors.append(
            f"DELIVERYOPS_LLM {mode!r} is not one of {sorted(_LLM_MODES)}")
    provider = env.get("DELIVERYOPS_PROVIDER", "anthropic").lower()
    if provider not in _PROVIDERS:
        errors.append(
            f"DELIVERYOPS_PROVIDER {provider!r} is not one of {sorted(_PROVIDERS)}")

    embedder = env.get("DELIVERYOPS_EMBEDDER", "hash").lower()
    if embedder not in _EMBEDDERS:
        errors.append(
            f"DELIVERYOPS_EMBEDDER {embedder!r} is not one of {sorted(_EMBEDDERS)}")

    dsn = env.get("DATABASE_URL", "")
    retriever = env.get("DELIVERYOPS_RETRIEVER", "lexical").lower()
    if retriever not in _RETRIEVERS:
        errors.append(
            f"DELIVERYOPS_RETRIEVER {retriever!r} is not one of {sorted(_RETRIEVERS)}")
    elif retriever in ("pgvector", "hybrid") and not dsn:
        errors.append(f"DELIVERYOPS_RETRIEVER={retriever} requires DATABASE_URL")

    checkpointer = env.get("DELIVERYOPS_CHECKPOINTER", "").lower()
    if checkpointer not in _CHECKPOINTERS:
        errors.append(
            f"DELIVERYOPS_CHECKPOINTER {checkpointer!r} is not memory|postgres")
    elif checkpointer == "postgres" and not dsn:
        errors.append("DELIVERYOPS_CHECKPOINTER=postgres requires DATABASE_URL")

    # Jira's rule lives in the adapter (partial config is an error); constructing
    # the store is side-effect free, so reuse it rather than duplicating the rule.
    try:
        from .integrations.jira import get_ticket_store

        get_ticket_store(env)
    except Exception as e:
        errors.append(str(e))

    # Numeric knobs other code parses lazily, mid-run or mid-poll.
    _check_number(env, "DELIVERYOPS_MAX_RUN_COST_USD", float, errors)
    _check_number(env, "DELIVERYOPS_WORKER_INTERVAL", float, errors)
    _check_number(env, "DELIVERYOPS_RETRIEVAL_MIN_SCORE", float, errors)
    _check_number(env, "DELIVERYOPS_DB_POOL_MIN", int, errors)
    _check_number(env, "DELIVERYOPS_DB_POOL_MAX", int, errors)

    if errors:
        raise ConfigError("invalid configuration: " + "; ".join(errors))

    summary = {
        "llm": mode if mode == "fake" else (
            f"{mode}:{provider}/{env.get('DELIVERYOPS_MODEL', 'claude-sonnet-4-6')}"),
        "retriever": retriever,
        "checkpointer": checkpointer or ("postgres" if dsn else "memory"),
        "embedder": embedder,
    }
    for name in SECRET_VARS:
        summary[name.lower()] = "set" if env.get(name) else "unset"
    log.info("configuration ok: %s", summary)
    return summary
