"""Checkpointer selection: durable Postgres in deployment, in-memory for tests and demos.

DELIVERYOPS_CHECKPOINTER=postgres|memory; the default is postgres whenever DATABASE_URL is
set, memory otherwise. With the Postgres checkpointer an in-flight run survives an API
restart: the graph state lives in the same database as the run rows, and the resumed process
continues from the interrupt (this was the top known limitation - see docs/STATUS.md).

Asking for postgres without a database is a loud error, never a silent in-memory fall back.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

from langgraph.checkpoint.memory import InMemorySaver

# The Postgres saver's connection context must live as long as the process does.
_LIVE_CONTEXTS: list = []


def get_checkpointer(env: Mapping[str, str] | None = None):
    env = os.environ if env is None else env
    mode = (env.get("DELIVERYOPS_CHECKPOINTER") or "").lower()
    dsn = env.get("DATABASE_URL", "")
    if mode == "memory" or (not mode and not dsn):
        return InMemorySaver()
    if mode not in ("", "postgres"):
        raise RuntimeError(f"unknown DELIVERYOPS_CHECKPOINTER {mode!r} (memory|postgres)")
    if not dsn:
        raise RuntimeError("DELIVERYOPS_CHECKPOINTER=postgres requires DATABASE_URL")
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError as e:
        raise RuntimeError(
            "postgres checkpointer needs the 'postgres' extra "
            "(uv sync --extra postgres)"
        ) from e
    ctx = PostgresSaver.from_conn_string(dsn)
    saver = ctx.__enter__()
    _LIVE_CONTEXTS.append(ctx)
    saver.setup()
    return saver
