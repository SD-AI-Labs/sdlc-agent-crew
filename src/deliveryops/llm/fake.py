"""Deterministic keyword LLM for tests, CI and offline development.

It exists so the harness (agents, gates, evals) is testable without network access. Its quality
is deliberately poor: the eval gate is supposed to reject it, and a test asserts that it does.
"""
from __future__ import annotations

import json

from .base import LLMResult

_BUG = ("bug", "error", "crash", "fails", "broken", "exception", "500", "regression")
_FEATURE = ("add", "support", "implement", "new", "enable", "allow", "build")
_URGENT = ("outage", "production", "down", "data loss", "security", "breach", "urgent")


def _kind(system: str) -> str:
    s = system.lower()
    if "intake triage" in s:
        return "intake"
    if "code change" in s or "diff" in s and "review" not in s:
        return "code"
    if "technical design" in s:
        return "design"
    if "code review" in s:
        return "review"
    return "generic"


class FakeLLM:
    name = "fake-keyword-llm"

    def complete(
        self, system: str, user: str, *, max_tokens: int = 1000, temperature: float = 0.0
    ) -> LLMResult:
        text = user.lower()
        kind = _kind(system)
        if kind == "intake":
            payload = self._intake(text)
        elif kind == "design":
            payload = self._design(text)
        elif kind == "code":
            payload = self._code(text)
        elif kind == "review":
            payload = self._review(text)
        else:
            payload = {"ok": True}
        words = len(text.split())
        return LLMResult(
            text=json.dumps(payload), input_tokens=words, output_tokens=40, model=self.name
        )

    def _intake(self, text: str) -> dict:
        if any(k in text for k in _BUG):
            ttype = "bug"
        elif any(k in text for k in _FEATURE):
            ttype = "feature"
        else:
            ttype = "task"
        priority = "high" if any(k in text for k in _URGENT) else "medium"
        words = len(text.split())
        points = 1 if words < 12 else 3 if words < 30 else 5
        reqs = [s.strip() for s in text.replace("\n", ". ").split(".") if len(s.split()) > 3][:3]
        return {
            "type": ttype,
            "priority": priority,
            "estimate_points": points,
            "requirements": reqs or ["Clarify scope with the requester"],
            "confidence": 0.55 if ttype == "task" else 0.85,
        }

    def _design(self, text: str) -> dict:
        return {
            "approach": "Follow the existing pattern; change the smallest surface that fixes it.",
            "components": ["api", "service"],
            "risks": ["regression in adjacent flow"],
            "test_plan": ["unit test for the changed branch"],
            "open_questions": [] if "?" in text else ["Confirm expected behaviour with requester"],
            "confidence": 0.6,
        }

    def _code(self, text: str) -> dict:
        return {
            "diff": (
                "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,3 +1,3 @@\n"
                "-def handle():\n+def handle(request=None):\n"
            ),
            "files_touched": ["src/app.py"],
            "rationale": "Smallest change consistent with the design.",
            "confidence": 0.5,
        }

    def _review(self, text: str) -> dict:
        return {
            "findings": [{"severity": "low", "area": "style", "detail": "Add a docstring."}],
            "verdict": "approve",
            "confidence": 0.6,
        }
