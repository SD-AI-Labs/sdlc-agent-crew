"""A small, hand-written process library: the knowledge the agents retrieve from.

Deliberately tiny and explicit. In a real org this is Confluence/Notion; the point of the
project is the retrieval + grounding path, not the corpus size.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Doc:
    id: str
    source: str
    title: str
    content: str
    tags: tuple[str, ...] = ()


DOCS: tuple[Doc, ...] = (
    Doc(
        "kb-001",
        "engineering-handbook/definition-of-done.md",
        "Definition of Done",
        "A change ships when: tests cover the new branch, the eval gate passes, a human has "
        "approved the design, an audit entry exists for every transition, and rollback is "
        "documented for anything touching payments or auth.",
        ("done", "quality", "release"),
    ),
    Doc(
        "kb-002",
        "engineering-handbook/estimation-guide.md",
        "Estimating work in story points",
        "1 point: config or copy change. 2: single-file change with one test. 3: multi-file "
        "change, existing pattern. 5: new integration or schema change. 8: anything needing a "
        "design review or an external dependency. Split anything above 8.",
        ("estimate", "planning"),
    ),
    Doc(
        "kb-003",
        "engineering-handbook/severity-guide.md",
        "Priority and severity",
        "High: customer-facing outage, data loss, security issue, or a broken primary flow. "
        "Medium: a supported flow is degraded or has a workaround. Low: cosmetic, internal-only, "
        "or speculative. Urgent language in the ticket body must be reflected in the priority.",
        ("priority", "triage", "severity"),
    ),
    Doc(
        "kb-004",
        "security/review-checklist.md",
        "Security review checklist",
        "Check for: secrets in code or logs, unvalidated input crossing a trust boundary, authz "
        "checks on new endpoints, SQL built by string concatenation, unsafe deserialisation, and "
        "dependency additions without a version pin.",
        ("security", "review"),
    ),
    Doc(
        "kb-005",
        "platform/change-policy.md",
        "Change and approval policy",
        "Every agent stage ends in a human approval gate. Escalate when confidence is below "
        "threshold, when the patch touches CI, evals, secrets or infrastructure, or when ticket "
        "text shows signs of prompt injection. Escalation is not failure; it is the system "
        "working.",
        ("governance", "approval", "escalation"),
    ),
    Doc(
        "kb-006",
        "platform/rollout-policy.md",
        "Rollout and rollback",
        "Ship behind a flag when the change is user-visible. Keep the flag for one release. "
        "Rollback must be a redeploy of the previous image, not a data migration reversal. "
        "Database migrations are forward-only and must be backward compatible for one release.",
        ("release", "rollback", "database"),
    ),
    Doc(
        "kb-007",
        "quality/test-strategy.md",
        "Test strategy",
        "Unit tests for contracts and edge cases, one integration test per external boundary, "
        "and no test that depends on network access. Agents must emit tests with the patch, not "
        "after it.",
        ("testing", "quality"),
    ),
    Doc(
        "kb-008",
        "quality/eval-guide.md",
        "Writing evals for agents",
        "Start from real tickets that went wrong. Score each dimension separately. Accept a set "
        "of correct answers for subjective fields. Treat malformed output as a failure. Never "
        "lower a threshold to make a run pass; fix the agent or add a case.",
        ("evals", "quality", "agents"),
    ),
)
