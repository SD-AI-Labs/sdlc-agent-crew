"""Intake triage agent: ticket text -> type, priority, estimate, requirements, confidence."""
from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel

from ..llm.base import LLM
from .base import (
    AgentOutputError,
    complete_structured,
    confidence,
    require,
    str_list,
)

SYSTEM = """You are an intake triage agent for a software delivery team.
Return ONLY a JSON object with keys:
  type: one of "bug" | "feature" | "task"
  priority: one of "low" | "medium" | "high"
  estimate_points: integer from {1,2,3,5,8}
  requirements: list of 1-5 short, testable requirement strings
  confidence: number between 0 and 1 (your confidence in this triage)
No prose, no markdown."""

VALID_TYPES = {"bug", "feature", "task"}
VALID_PRIORITIES = {"low", "medium", "high"}
VALID_POINTS = {1, 2, 3, 5, 8}


@dataclass
class TriageResult:
    type: str
    priority: str
    estimate_points: int
    requirements: list[str]
    confidence: float
    meta: dict = field(default_factory=dict)  # tokens, latency, model, cost: feeds observability

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "priority": self.priority,
            "estimate_points": self.estimate_points,
            "requirements": self.requirements,
            "confidence": self.confidence,
        }


def validate(raw: dict) -> TriageResult:
    require(raw, "intake", "type", "priority", "estimate_points", "requirements", "confidence")
    try:
        ttype = str(raw["type"]).lower()
        priority = str(raw["priority"]).lower()
        estimate = int(raw["estimate_points"])
    except (TypeError, ValueError) as e:
        raise AgentOutputError(f"intake: malformed field types ({e})") from e
    reqs = str_list(raw["requirements"], "intake", "requirements")
    conf = confidence(raw["confidence"], "intake")
    if ttype not in VALID_TYPES:
        raise AgentOutputError(f"intake: invalid type {ttype!r}")
    if priority not in VALID_PRIORITIES:
        raise AgentOutputError(f"intake: invalid priority {priority!r}")
    if estimate not in VALID_POINTS:
        raise AgentOutputError(f"intake: invalid estimate {estimate}")
    if not 1 <= len(reqs) <= 5:
        raise AgentOutputError("intake: requirements must have 1-5 items")
    return TriageResult(ttype, priority, estimate, reqs, conf)


class TriageSchema(BaseModel):
    """Structured-output schema; validate() below remains the contract enforcement."""

    type: str
    priority: str
    estimate_points: int
    requirements: list[str]
    confidence: float


class IntakeAgent:
    name = "intake"

    def __init__(self, llm: LLM) -> None:
        self.llm = llm

    def run(self, title: str, description: str) -> TriageResult:
        raw, meta = complete_structured(self.llm, SYSTEM, f"Title: {title}\n\n{description}",
                                        TriageSchema, "intake")
        result = validate(raw)
        result.meta = meta
        return result
