"""Guardrails: autonomy is earned, not assumed.

Every stage ends in a human gate. This module decides *why* the gate matters, whether the run
should be escalated (a stronger form of review: risk, not routine approval), and records the
reasons that flow into the audit log, the dashboard and the executive summary.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


RISK_ORDER = {Risk.LOW: 0, Risk.MEDIUM: 1, Risk.HIGH: 2, Risk.CRITICAL: 3}


@dataclass(frozen=True)
class StagePolicy:
    """Thresholds for one pipeline stage. Tightening these is an eval-backed decision."""

    min_confidence: float = 0.7
    max_auto_points: int = 3
    review_priorities: tuple[str, ...] = ("high",)
    max_patch_files: int = 5
    max_patch_lines: int = 300
    forbidden_paths: tuple[str, ...] = (
        ".github/",
        ".env",
        "Dockerfile",
        "docker-compose.yml",
        "evals/golden/",
        "pyproject.toml",
    )


POLICIES: dict[str, StagePolicy] = {
    "intake": StagePolicy(min_confidence=0.70, max_auto_points=3),
    "design": StagePolicy(min_confidence=0.65),
    "code": StagePolicy(min_confidence=0.65, max_patch_files=5, max_patch_lines=300),
    "review": StagePolicy(min_confidence=0.60),
}

DEFAULT_POLICY = StagePolicy()


@dataclass(frozen=True)
class GateDecision:
    """The outcome of grading one stage. A human gate is structural, so it is not a field here."""

    stage: str
    escalate: bool
    risk: Risk
    reasons: tuple[str, ...]


def _raise_risk(current: Risk, new: Risk) -> Risk:
    return new if RISK_ORDER[new] > RISK_ORDER[current] else current


def evaluate(stage: str, signals: dict, policy: StagePolicy | None = None) -> GateDecision:
    """Map raw stage signals to a gate decision.

    `signals` is a plain dict so that adding a signal never changes a function signature; the
    keys understood today are documented in docs/adr/0005.
    """
    policy = policy or POLICIES.get(stage, DEFAULT_POLICY)
    reasons: list[str] = []
    escalate = False
    risk = Risk.LOW

    confidence = signals.get("confidence")
    if confidence is not None and confidence < policy.min_confidence:
        reasons.append(f"low confidence ({confidence:.2f} < {policy.min_confidence})")
        escalate = True
        risk = _raise_risk(risk, Risk.HIGH)

    points = signals.get("estimate_points")
    if points is not None and points > policy.max_auto_points:
        reasons.append(f"estimate {points} exceeds auto limit {policy.max_auto_points}")
        risk = _raise_risk(risk, Risk.MEDIUM)

    priority = signals.get("priority")
    if priority in policy.review_priorities:
        reasons.append(f"priority '{priority}' always needs review")
        risk = _raise_risk(risk, Risk.MEDIUM)

    if signals.get("severity_inversion"):
        reasons.append("possible severity inversion: urgent language with non-urgent priority")
        escalate = True
        risk = _raise_risk(risk, Risk.HIGH)

    open_questions = signals.get("open_questions") or 0
    if open_questions:
        reasons.append(f"design left {open_questions} open question(s) for a human")
        risk = _raise_risk(risk, Risk.MEDIUM)

    hits = signals.get("injection_hits") or []
    if hits:
        reasons.append(f"prompt-injection signals ({', '.join(sorted(hits))})")
        escalate = True
        risk = _raise_risk(risk, Risk.CRITICAL)

    files = signals.get("files_touched") or []
    if len(files) > policy.max_patch_files:
        reasons.append(f"patch touches {len(files)} files (limit {policy.max_patch_files})")
        escalate = True
        risk = _raise_risk(risk, Risk.HIGH)

    patch_lines = signals.get("patch_lines") or 0
    if patch_lines > policy.max_patch_lines:
        reasons.append(f"patch is {patch_lines} lines (limit {policy.max_patch_lines})")
        escalate = True
        risk = _raise_risk(risk, Risk.HIGH)

    forbidden = [f for f in files if any(f.startswith(p) for p in policy.forbidden_paths)]
    if forbidden:
        reasons.append(f"patch touches gated paths: {', '.join(sorted(forbidden))}")
        escalate = True
        risk = _raise_risk(risk, Risk.CRITICAL)

    critical_findings = signals.get("critical_findings") or 0
    if critical_findings:
        reasons.append(f"{critical_findings} critical review finding(s)")
        escalate = True
        risk = _raise_risk(risk, Risk.CRITICAL)

    verdict = signals.get("verdict")
    if verdict == "request_changes":
        reasons.append("reviewer agent requests changes")
        risk = _raise_risk(risk, Risk.MEDIUM)

    return GateDecision(stage=stage, escalate=escalate, risk=risk, reasons=tuple(reasons))


URGENT_TERMS = (
    "outage",
    "data loss",
    "security",
    "breach",
    "production down",
    "sev1",
    "sev-1",
    "p1",
    "customer impact",
)


def looks_urgent(text: str) -> bool:
    t = text.lower()
    return any(term in t for term in URGENT_TERMS)
