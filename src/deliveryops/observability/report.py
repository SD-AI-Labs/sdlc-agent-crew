"""The one-page "what the agent did and how we know" report for a run.

Written for a reader who will not look at the code: what happened, what it cost, what a human
decided and why, and which claims are checked rather than asserted.
"""
from __future__ import annotations

from .tracer import Tracer

STAGE_LABEL = {
    "intake": "Intake triage",
    "design": "Technical design",
    "code": "Code change",
    "review": "Review",
    "done": "Completed",
}


def _money(v: float) -> str:
    return f"${v:.4f}"


def executive_summary(view, tracer: Tracer, eval_report: dict | None = None) -> str:
    """Render the per-run report. `view` is an orchestrator RunView."""
    ticket = view.ticket
    spans = tracer.spans(view.run_id)
    total_cost = sum(s.cost_usd for s in spans) or view.spend_usd
    total_ms = sum(s.latency_ms for s in spans)
    lines: list[str] = []
    lines.append(f"# Run report: {ticket.get('title', '')}")
    lines.append("")
    lines.append(f"- Run id: `{view.run_id}`")
    lines.append(f"- Ticket: {ticket.get('title', '')}")
    stage_label = STAGE_LABEL.get(view.stage, view.stage)
    lines.append(f"- Outcome: **{view.status}** (current stage: {stage_label})")
    lines.append(f"- Agent spend: {_money(total_cost)} across {len(spans)} model calls")
    lines.append(f"- Agent latency: {total_ms / 1000:.1f}s")
    lines.append(f"- Human decisions recorded: {len(view.decisions)}")
    lines.append("")
    lines.append("## What the agent did")
    lines.append("")
    if view.triage:
        t = view.triage
        lines.append(
            f"- **Triage**: {t['type']}, priority {t['priority']}, {t['estimate_points']} points, "
            f"confidence {t['confidence']:.2f}. Requirements: "
            + "; ".join(t["requirements"])
        )
    if view.design:
        d = view.design
        cite = ", ".join(d.get("citations") or []) or "no process docs cited"
        lines.append(f"- **Design**: {d['approach']} (grounded in: {cite})")
        if d.get("risks"):
            lines.append(f"  - Risks: {'; '.join(d['risks'])}")
    if view.patch:
        p = view.patch
        lines.append(
            f"- **Change**: {p.get('patch_lines', 0)} diff lines across "
            f"{', '.join(p.get('files_touched') or [])} - proposed only, not applied"
        )
    if view.review:
        r = view.review
        count = len(r.get("findings") or [])
        lines.append(f"- **Review**: verdict `{r['verdict']}` with {count} finding(s)")
        for f in r.get("findings") or []:
            lines.append(f"  - [{f['severity']}] {f['area']}: {f['detail']}")
    if not any((view.triage, view.design, view.patch, view.review)):
        lines.append("- No stage completed yet.")
    lines.append("")
    lines.append("## What a human decided, and why")
    lines.append("")
    if not view.decisions:
        lines.append("- No gates cleared yet." if view.awaiting is None else
                     "- Waiting at the gate below; nothing has been approved by a human yet.")
    for d in view.decisions:
        verdict = "approved" if d["approve"] else "rejected"
        lines.append(
            f"- {STAGE_LABEL.get(d['stage'], d['stage'])}: {verdict} by {d['approver']} "
            f"({d.get('role', 'engineer')}), risk {d['risk']}"
            + (f" - \"{d['comment']}\"" if d.get("comment") else "")
        )
    if view.awaiting:
        a = view.awaiting
        lines.append("")
        waiting = STAGE_LABEL.get(a.get("stage", ""), a.get("stage", ""))
        lines.append(f"## Waiting now: {waiting} gate")
        lines.append("")
        lines.append(f"- Risk: {a.get('risk')}" + (" (escalated)" if a.get("escalate") else ""))
        for reason in a.get("reasons") or ["routine review"]:
            lines.append(f"- Reason: {reason}")
    escalations = list(view.escalations)
    if view.awaiting and view.awaiting.get("escalate"):
        escalations.append({
            "stage": view.awaiting.get("stage", ""),
            "risk": view.awaiting.get("risk", ""),
            "reasons": view.awaiting.get("reasons") or [],
        })
    if escalations:
        lines.append("")
        lines.append("## Escalations")
        lines.append("")
        for e in escalations:
            lines.append(f"- {STAGE_LABEL.get(e.get('stage', ''), e.get('stage', ''))}: "
                         f"{e.get('risk')} - {'; '.join(e.get('reasons') or [])}")
    lines.append("")
    lines.append("## How we know")
    lines.append("")
    lines.append("- Every stage ended in a human approval gate; the decision, approver and role "
                 "are in the append-only audit log.")
    lines.append("- Malformed agent output fails the run instead of reaching a gate.")
    lines.append("- Ticket text is screened for prompt injection before it is trusted.")
    if eval_report:
        means = eval_report.get("means", {})
        lines.append(
            "- The agent version in this run passed its eval gate: "
            + ", ".join(f"{k} {v:.2f}" for k, v in means.items())
            + f" (model `{eval_report.get('model')}`, {eval_report.get('cases')} cases)."
            if eval_report.get("passed")
            else "- Eval gate status: **failing** - this build is not releasable."
        )
    else:
        lines.append("- Eval gate status for this build: not supplied (run `deliveryops-eval`).")
    lines.append("")
    return "\n".join(lines)
