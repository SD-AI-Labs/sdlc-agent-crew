"""Scorers turn agent output + expectation into a 0..1 score. Boring and explicit on purpose.

Two rules learned the hard way (ADR 0004):
  * subjective fields accept a *set* of correct answers, not one label
  * a dimension the naive baseline can max out for free is not a dimension
"""
from __future__ import annotations

from ..agents.intake import TriageResult


def exact_set(actual: str, acceptable: str | list[str]) -> float:
    options = [acceptable] if isinstance(acceptable, str) else list(acceptable)
    return 1.0 if actual in {o.lower() for o in options} else 0.0


def within_one_step(actual: int, expected: int) -> float:
    """Story points on a Fibonacci-ish scale: exact = 1.0, adjacent bucket = 0.5."""
    scale = [1, 2, 3, 5, 8]
    if actual == expected:
        return 1.0
    try:
        return 0.5 if abs(scale.index(actual) - scale.index(expected)) == 1 else 0.0
    except ValueError:
        return 0.0


def requirement_coverage(requirements: list[str], must_mention: list[str]) -> float:
    """Fraction of required terms present. Empty expectation scores 1.0 by definition."""
    if not must_mention:
        return 1.0
    blob = " ".join(requirements).lower()
    return sum(1 for k in must_mention if k.lower() in blob) / len(must_mention)


def score_triage(result: TriageResult, expected: dict) -> dict[str, float]:
    return {
        "type": exact_set(result.type, expected["type"]),
        "priority": exact_set(result.priority, expected["priority"]),
        "estimate": within_one_step(result.estimate_points, expected["estimate_points"]),
        "requirements": requirement_coverage(result.requirements,
                                             expected.get("must_mention", [])),
    }


JUDGE_SYSTEM = """You are grading requirement quality for an engineering ticket.
Score the requirements from 1 to 5:
 5 = each requirement is independently testable, specific, and covers the ticket's outcome
 3 = mostly relevant but vague or partly untestable
 1 = restates the ticket or names no verifiable behaviour
Return ONLY a JSON object: {"score": <1-5>, "reason": "<one sentence>"}"""


def judge_requirements(judge, requirements: list[str], ticket: str) -> dict:
    """LLM-as-judge for requirement quality. Returns {'score': 0..1, 'reason': str}."""
    prompt = f"Ticket:\n{ticket}\n\nRequirements:\n- " + "\n- ".join(requirements)
    try:
        raw = judge.complete(JUDGE_SYSTEM, prompt).json()
        score = float(raw.get("score", 0))
    except (ValueError, TypeError, AttributeError):
        return {"score": 0.0, "reason": "judge output unparseable"}
    score = max(1.0, min(5.0, score))
    return {"score": (score - 1) / 4, "reason": str(raw.get("reason", ""))}
