"""Shared test doubles: scripted models so tests control exactly what the 'model' says."""
from __future__ import annotations

import json

import pytest

from deliveryops.db.repository import InMemoryRepository
from deliveryops.llm.base import LLMResult
from deliveryops.llm.fake import _kind
from deliveryops.orchestrator.pipeline import Pipeline


class ScriptedLLM:
    """Returns one fixed string for every call. Used to prove failure handling."""

    name = "scripted"

    def __init__(self, text: str) -> None:
        self.text = text

    def complete(self, system, user, *, max_tokens=1000, temperature=0.0):
        return LLMResult(text=self.text, input_tokens=10, output_tokens=10, model=self.name)


class RoutingLLM:
    """Answers per agent kind, so a whole pipeline can run deterministically offline."""

    name = "scripted-router"

    def __init__(self, **payloads: dict) -> None:
        self.payloads = payloads

    def complete(self, system, user, *, max_tokens=1000, temperature=0.0):
        kind = _kind(system)
        payload = self.payloads.get(kind)
        if payload is None:
            raise AssertionError(f"no payload configured for agent kind {kind!r}")
        return LLMResult(
            text=json.dumps(payload), input_tokens=120, output_tokens=45,
            latency_ms=42.0, model=self.name,
        )


def triage_payload(**over) -> dict:
    base = {
        "type": "bug",
        "priority": "medium",
        "estimate_points": 2,
        "requirements": ["Return 200 on login after a password reset"],
        "confidence": 0.9,
    }
    return {**base, **over}


DESIGN_PAYLOAD = {
    "approach": "Reset the session token when the password changes and add a regression test.",
    "components": ["auth/session.py", "tests/test_auth.py"],
    "risks": ["existing sessions are invalidated"],
    "test_plan": ["unit test for the reset path"],
    "open_questions": [],
    "confidence": 0.9,
}

PATCH = """--- a/src/auth/session.py
+++ b/src/auth/session.py
@@ -1,2 +1,2 @@
-def login(user):
+def login(user, reset=False):
     return user
"""

CODE_PAYLOAD = {
    "diff": PATCH,
    "files_touched": ["src/auth/session.py"],
    "rationale": "Reset the session after a password change.",
    "confidence": 0.9,
}

REVIEW_PAYLOAD = {"findings": [], "verdict": "approve", "confidence": 0.9}


def make_pipeline(llm=None, **overrides) -> Pipeline:
    payloads = {
        "intake": triage_payload(**overrides.pop("intake", {})),
        "design": DESIGN_PAYLOAD,
        "code": CODE_PAYLOAD,
        "review": REVIEW_PAYLOAD,
    }
    overrides.setdefault("repo", InMemoryRepository())
    return Pipeline(llm or RoutingLLM(**payloads), **overrides)


@pytest.fixture()
def pipeline() -> Pipeline:
    return make_pipeline()
