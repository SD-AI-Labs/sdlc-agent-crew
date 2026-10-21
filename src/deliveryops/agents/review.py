"""Review agent: proposed diff -> findings + verdict.

Local implementation of the specialist review that GraphForge exposes as MCP tools. If
DELIVERYOPS_REVIEW=mcp is set, the same shape is fetched from the graphforge MCP server instead
(see mcp_server/client.py), so the pipeline does not care which reviewer answered.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..llm.base import LLM
from .base import AgentOutputError, confidence, meta_of, parse_json, require

SYSTEM = """You are a code reviewer. Return ONLY a JSON object with keys:
  findings: list of {"severity": "low"|"medium"|"high"|"critical",
                      "area": "security"|"performance"|"style"|"correctness",
                      "detail": string}
  verdict: "approve" | "request_changes"
  confidence: number between 0 and 1
No prose, no markdown."""

SEVERITIES = {"low", "medium", "high", "critical"}
VERDICTS = {"approve", "request_changes"}

_SECRET = re.compile(r"(api[_-]?key|secret|token)\s*=\s*['\"][^'\"]{8,}", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    severity: str
    area: str
    detail: str


@dataclass
class ReviewResult:
    findings: list[Finding]
    verdict: str
    confidence: float
    meta: dict = field(default_factory=dict)

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "critical")

    def to_dict(self) -> dict:
        return {
            "findings": [f.__dict__ for f in self.findings],
            "critical_findings": self.critical_count,
            "verdict": self.verdict,
            "confidence": self.confidence,
        }


def validate(raw: dict) -> ReviewResult:
    require(raw, "review", "findings", "verdict", "confidence")
    raw_findings = raw["findings"]
    if not isinstance(raw_findings, list):
        raise AgentOutputError("review: findings must be a list")
    findings: list[Finding] = []
    for f in raw_findings:
        if not isinstance(f, dict) or "severity" not in f or "detail" not in f:
            raise AgentOutputError("review: each finding needs severity and detail")
        sev = str(f["severity"]).lower()
        if sev not in SEVERITIES:
            raise AgentOutputError(f"review: invalid severity {sev!r}")
        findings.append(Finding(sev, str(f.get("area", "correctness")), str(f["detail"])))
    verdict = str(raw["verdict"]).lower()
    if verdict not in VERDICTS:
        raise AgentOutputError(f"review: invalid verdict {verdict!r}")
    return ReviewResult(findings, verdict, confidence(raw["confidence"], "review"))


def static_scan(diff: str) -> list[Finding]:
    """Deterministic checks that run even when the model review is optimistic."""
    out: list[Finding] = []
    for line in diff.splitlines():
        if line.startswith("+") and _SECRET.search(line):
            out.append(Finding("critical", "security", "possible secret added in diff"))
        if line.startswith("+") and "eval(" in line:
            out.append(Finding("high", "security", "unsafe eval() added"))
    return out


class ReviewAgent:
    name = "review"

    def __init__(self, llm: LLM) -> None:
        self.llm = llm

    def run(self, diff: str, design_approach: str) -> ReviewResult:
        user = f"Design: {design_approach}\n\nProposed diff:\n{diff}"
        res = self.llm.complete(SYSTEM, user)
        result = validate(parse_json(res, "review"))
        result.meta = meta_of(res)
        scanner = static_scan(diff)
        if scanner:
            result.findings = result.findings + scanner
            if any(f.severity == "critical" for f in scanner):
                result.verdict = "request_changes"
        return result
