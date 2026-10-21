"""LangGraph pipeline: intake -> design -> code -> review, with a human gate after each stage.

The gates are LangGraph interrupts, not polling loops: the graph stops mid-run, the checkpointer
holds state, and `Command(resume=...)` continues from exactly where it stopped. A gate node has no
side effects before `interrupt()` because the node body re-executes on resume (ADR 0002).
"""
from __future__ import annotations

import time

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..agents.base import AgentOutputError
from ..agents.code import CodeAgent
from ..agents.design import DesignAgent
from ..agents.intake import IntakeAgent
from ..agents.review import ReviewAgent
from ..guardrails import injection
from ..guardrails.policy import GateDecision, evaluate, looks_urgent
from ..observability.tracer import Tracer
from .events import Outbox
from .state import PipelineState


def audit(actor: str, action: str, **detail) -> dict:
    return {"ts": time.time(), "actor": actor, "action": action, "detail": detail}


class StageNodes:
    """Node implementations. Agents are injected so tests can script them."""

    def __init__(
        self,
        intake: IntakeAgent,
        design: DesignAgent,
        code: CodeAgent,
        review: ReviewAgent,
        tracer: Tracer,
        outbox: Outbox,
    ) -> None:
        self.intake = intake
        self.design = design
        self.code = code
        self.review = review
        self.tracer = tracer
        self.outbox = outbox

    # ---------- agent stages ----------

    def intake_node(self, state: PipelineState) -> dict:
        try:
            result = self.intake.run(state["title"], state["description"])
        except AgentOutputError as e:
            return self._failed("intake", e)
        span = self.tracer.record(state["run_id"], "intake", "intake", result.meta)
        return {
            "triage": result.to_dict(),
            "stage": "intake",
            "status": "running",
            "audit": [audit("agent:intake", "triaged", **result.to_dict())],
            "spent_usd": span.cost_usd,
        }

    def design_node(self, state: PipelineState) -> dict:
        triage = state.get("triage") or {}
        try:
            result = self.design.run(state["title"], triage.get("requirements", []))
        except (AgentOutputError, ValueError) as e:
            return self._failed("design", e)
        span = self.tracer.record(state["run_id"], "design", "design", result.meta)
        return {
            "design": result.to_dict(),
            "stage": "design",
            "status": "running",
            "audit": [audit("agent:design", "designed", approach=result.approach,
                            citations=result.citations)],
            "spent_usd": span.cost_usd,
        }

    def code_node(self, state: PipelineState) -> dict:
        triage = state.get("triage") or {}
        design = state.get("design") or {}
        try:
            result = self.code.run(
                state["title"], design.get("approach", ""), triage.get("requirements", [])
            )
        except AgentOutputError as e:
            return self._failed("code", e)
        span = self.tracer.record(state["run_id"], "code", "code", result.meta)
        return {
            "patch": result.to_dict(),
            "stage": "code",
            "status": "running",
            "audit": [audit("agent:code", "patch_proposed", files=result.files_touched,
                            lines=result.info.lines if result.info else 0)],
            "spent_usd": span.cost_usd,
        }

    def review_node(self, state: PipelineState) -> dict:
        patch = state.get("patch") or {}
        design = state.get("design") or {}
        try:
            result = self.review.run(patch.get("diff", ""), design.get("approach", ""))
        except AgentOutputError as e:
            return self._failed("review", e)
        span = self.tracer.record(state["run_id"], "review", "review", result.meta)
        return {
            "review": result.to_dict(),
            "stage": "review",
            "status": "running",
            "audit": [audit("agent:review", "reviewed", verdict=result.verdict,
                            findings=len(result.findings))],
            "spent_usd": span.cost_usd,
        }

    # ---------- human gates ----------

    def gate_intake(self, state: PipelineState) -> dict:
        return self._gate(state, "intake")

    def gate_design(self, state: PipelineState) -> dict:
        return self._gate(state, "design")

    def gate_code(self, state: PipelineState) -> dict:
        return self._gate(state, "code")

    def gate_review(self, state: PipelineState) -> dict:
        return self._gate(state, "review")

    def finalize(self, state: PipelineState) -> dict:
        return {
            "status": "approved",
            "stage": "done",
            "audit": [audit("system", "run_approved",
                            spend_usd=round(state.get("spent_usd", 0.0), 6))],
        }

    # ---------- internals ----------

    def _signals(self, state: PipelineState, stage: str) -> dict:
        text = f"{state.get('title', '')} {state.get('description', '')}"
        if stage == "intake":
            triage = state.get("triage") or {}
            return {
                "confidence": triage.get("confidence"),
                "priority": triage.get("priority"),
                "estimate_points": triage.get("estimate_points"),
                "severity_inversion": looks_urgent(text) and triage.get("priority") != "high",
                "injection_hits": injection.scan(text),
                "injection_phrases": injection.matched_phrases(text),
            }
        if stage == "design":
            design = state.get("design") or {}
            return {
                "confidence": design.get("confidence"),
                "open_questions": len(design.get("open_questions") or []),
            }
        if stage == "code":
            patch = state.get("patch") or {}
            return {
                "confidence": patch.get("confidence"),
                "files_touched": patch.get("files_touched") or [],
                "patch_lines": patch.get("patch_lines") or 0,
                "injection_hits": injection.scan(text),
                "injection_phrases": injection.matched_phrases(text),
            }
        review = state.get("review") or {}
        findings = review.get("findings") or []
        return {
            "confidence": review.get("confidence"),
            "verdict": review.get("verdict"),
            "critical_findings": review.get("critical_findings", sum(
                1 for f in findings if f.get("severity") == "critical")),
        }

    def _summary(self, state: PipelineState, stage: str) -> dict:
        if stage == "intake":
            return state.get("triage") or {}
        if stage == "design":
            return state.get("design") or {}
        if stage == "code":
            patch = state.get("patch") or {}
            return {
                "files_touched": patch.get("files_touched"),
                "patch_lines": patch.get("patch_lines"),
                "diff": (patch.get("diff") or "")[:4000],
                "rationale": patch.get("rationale"),
            }
        return state.get("review") or {}

    def _gate(self, state: PipelineState, stage: str) -> dict:
        signals = self._signals(state, stage)
        decision: GateDecision = evaluate(stage, signals)
        payload = {
            "run_id": state["run_id"],
            "stage": stage,
            "risk": str(decision.risk),
            "escalate": decision.escalate,
            "reasons": list(decision.reasons),
            "summary": self._summary(state, stage),
        }
        phrases = signals.get("injection_phrases") or []
        if phrases:
            payload["injection_phrases"] = phrases
        # No side effects above this line: the node body runs again when the gate resumes.
        resume = interrupt(payload)
        if not isinstance(resume, dict):
            resume = {"approve": False, "approver": "unknown", "comment": "no decision supplied"}
        approve = bool(resume.get("approve"))
        entry = {
            "stage": stage,
            "approver": str(resume.get("approver", "unknown")),
            "role": str(resume.get("role", "engineer")),
            "approve": approve,
            "comment": str(resume.get("comment", "")),
            "risk": str(decision.risk),
            "escalate": decision.escalate,
            "reasons": list(decision.reasons),
            "ts": time.time(),
        }
        audit_entry = audit(
            f"human:{entry['approver']}", "approved" if approve else "rejected",
            stage=stage, comment=entry["comment"], risk=entry["risk"],
        )
        updates: dict = {"decisions": [entry], "audit": [audit_entry], "stage": stage}
        if decision.escalate:
            updates["escalations"] = [{
                "stage": stage, "risk": str(decision.risk),
                "reasons": list(decision.reasons), "ts": entry["ts"],
            }]
        updates["status"] = "running" if approve else "rejected"
        return updates

    def _failed(self, stage: str, error: Exception) -> dict:
        return {
            "status": "failed",
            "stage": stage,
            "errors": [{"stage": stage, "error": str(error)}],
            "audit": [audit(f"agent:{stage}", "failed", error=str(error))],
        }


def _after_agent(state: PipelineState) -> str:
    return "failed" if state.get("status") == "failed" else "gate"


def _after_gate(state: PipelineState) -> str:
    return "rejected" if state.get("status") == "rejected" else "continue"


def build_graph(nodes: StageNodes, checkpointer=None):
    """Wire the pipeline. Structure is fixed; behaviour lives in the agents and the policy."""
    g = StateGraph(PipelineState)
    g.add_node("intake", nodes.intake_node)
    g.add_node("gate_intake", nodes.gate_intake)
    g.add_node("design", nodes.design_node)
    g.add_node("gate_design", nodes.gate_design)
    g.add_node("code", nodes.code_node)
    g.add_node("gate_code", nodes.gate_code)
    g.add_node("review", nodes.review_node)
    g.add_node("gate_review", nodes.gate_review)
    g.add_node("finalize", nodes.finalize)

    g.add_edge(START, "intake")
    chain = [
        ("intake", "gate_intake", "design"),
        ("design", "gate_design", "code"),
        ("code", "gate_code", "review"),
        ("review", "gate_review", "finalize"),
    ]
    for agent, gate, nxt in chain:
        g.add_conditional_edges(agent, _after_agent, {"gate": gate, "failed": END})
        g.add_conditional_edges(gate, _after_gate, {"continue": nxt, "rejected": END})
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())


def resume_command(approver: str, approve: bool, comment: str = "", role: str = "engineer"):
    return Command(resume={"approver": approver, "approve": approve, "comment": comment,
                           "role": role})
