"""The pipeline facade the API talks to: start a run, answer a gate, read a run.

Everything LangGraph-specific (thread ids, interrupts, checkpoint state) stays in here.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field

from ..agents.code import CodeAgent
from ..agents.design import DesignAgent
from ..agents.intake import IntakeAgent
from ..agents.review import ReviewAgent
from ..db.repository import (
    AuditEntry,
    Escalation,
    InMemoryRepository,
    Repository,
    RunRecord,
)
from ..llm.base import LLM
from ..observability.tracer import Tracer
from .events import Outbox
from .graph import StageNodes, build_graph, resume_command


class RunNotFound(KeyError):
    pass


class GateClosed(RuntimeError):
    """Raised when a decision arrives for a run that is not waiting at a gate."""


class InsufficientAuthority(RuntimeError):
    """Raised when an escalated run is approved by someone without the required role."""


SENIOR_ROLES = {"senior", "staff", "principal", "security", "director"}


@dataclass
class RunView:
    run_id: str
    status: str
    stage: str
    ticket: dict
    triage: dict | None = None
    design: dict | None = None
    patch: dict | None = None
    review: dict | None = None
    awaiting: dict | None = None
    decisions: list[dict] = field(default_factory=list)
    escalations: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    spend_usd: float = 0.0
    audit: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class Pipeline:
    def __init__(
        self,
        llm: LLM,
        repo: Repository | None = None,
        outbox: Outbox | None = None,
        tracer: Tracer | None = None,
        search=None,
        checkpointer=None,
        repo_reader=None,
    ) -> None:
        self.repo = repo or InMemoryRepository()
        self.outbox = outbox or Outbox()
        self.tracer = tracer or Tracer(sink=self.repo.record_call)
        self.nodes = StageNodes(
            intake=IntakeAgent(llm),
            design=DesignAgent(llm, search=search),
            code=CodeAgent(llm, repo=repo_reader),
            review=ReviewAgent(llm),
            tracer=self.tracer,
            outbox=self.outbox,
        )
        self.graph = build_graph(self.nodes, checkpointer)
        self._persisted_audit: dict[str, int] = {}

    # ---------- public API ----------

    def start(self, title: str, description: str, run_id: str | None = None) -> RunView:
        rid = run_id or str(uuid.uuid4())
        cfg = self._cfg(rid)
        # The run row must exist before any span is written: agent_calls has a foreign key to it.
        self.repo.save(
            RunRecord(id=rid, title=title, description=description, status="submitted",
                      stage="intake")
        )
        self.outbox.publish(rid, "ticket.submitted", "intake", {"title": title})
        self.graph.invoke(
            {
                "run_id": rid,
                "title": title,
                "description": description,
                "status": "submitted",
                "audit": [{"ts": time.time(), "actor": "system", "action": "submitted",
                           "detail": {}}],
                "spent_usd": 0.0,
            },
            cfg,
        )
        return self.view(rid)

    def resume(self, run_id: str, approver: str, approve: bool, comment: str = "",
               role: str = "engineer") -> RunView:
        view = self.view(run_id)
        if view.awaiting is None:
            raise GateClosed(f"run {run_id} is '{view.status}', not waiting at a gate")
        escalated = bool((view.awaiting or {}).get("escalate"))
        if approve and escalated and role.lower() not in SENIOR_ROLES:
            raise InsufficientAuthority(
                f"stage '{view.stage}' is escalated; approver role '{role}' cannot clear it"
            )
        self.graph.invoke(resume_command(approver, approve, comment, role), self._cfg(run_id))
        return self.view(run_id)

    def view(self, run_id: str) -> RunView:
        cfg = self._cfg(run_id)
        snapshot = self.graph.get_state(cfg)
        values = dict(snapshot.values or {})
        if not values:
            raise RunNotFound(run_id)
        awaiting = None
        if snapshot.interrupts:
            awaiting = dict(snapshot.interrupts[0].value)
        ticket = {"id": run_id, "title": values.get("title", ""),
                  "description": values.get("description", "")}
        if snapshot.next:
            status = "awaiting_approval"
        else:
            status = values.get("status", "submitted")
        view = RunView(
            run_id=run_id,
            status=status,
            stage=values.get("stage", ""),
            ticket=ticket,
            triage=values.get("triage"),
            design=values.get("design"),
            patch=values.get("patch"),
            review=values.get("review"),
            awaiting=awaiting,
            decisions=values.get("decisions", []),
            escalations=values.get("escalations", []),
            errors=values.get("errors", []),
            spend_usd=round(values.get("spent_usd", 0.0), 6),
            audit=values.get("audit", []),
        )
        self._persist(run_id, view, values)
        return view

    def apply_patch(self, run_id: str) -> str:
        """Return the approved patch text for a human to apply. Never writes to a repo."""
        view = self.view(run_id)
        if view.status != "approved":
            raise GateClosed(
                f"run {run_id} is '{view.status}'; only approved patches can be applied"
            )
        return (view.patch or {}).get("diff", "")

    # ---------- internals ----------

    def _cfg(self, run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}}

    def _persist(self, run_id: str, view: RunView, values: dict) -> None:
        record = self.repo.get(run_id) or RunRecord(
            id=run_id, title=view.ticket["title"], description=view.ticket["description"]
        )
        record.status = view.status
        record.stage = view.stage
        record.state = {
            "triage": view.triage,
            "design": view.design,
            "patch": view.patch,
            "review": view.review,
            "awaiting": view.awaiting,
            "decisions": view.decisions,
            "spend_usd": view.spend_usd,
        }
        record.outcome = view.status
        self.repo.save(record)

        already = self._persisted_audit.get(run_id, 0)
        for item in view.audit[already:]:
            self.repo.append_audit(
                AuditEntry(run_id, float(item.get("ts", 0.0)), item.get("actor", "system"),
                           item.get("action", ""), item.get("detail", {}))
            )
        self._persisted_audit[run_id] = len(view.audit)

        # Escalations are recorded when the gate opens (above), which is the only place they can
        # appear; recording them again on decision produced duplicate queue rows.

        if view.awaiting is not None and view.awaiting.get("escalate"):
            stage = str(view.awaiting.get("stage", ""))
            if not any(e.stage == stage for e in self.repo.escalations(run_id)):
                self.repo.record_escalation(
                    Escalation(run_id, stage, str(view.awaiting.get("risk", "")),
                               tuple(view.awaiting.get("reasons", [])))
                )
        if view.awaiting is not None:
            stage = str(view.awaiting.get("stage", ""))
            if not self.outbox.has(run_id, "gate.opened", stage):
                self.outbox.publish(run_id, "gate.opened", stage, view.awaiting)
        for d in view.decisions:
            if not self.outbox.has(run_id, "gate.decided", str(d.get("stage"))):
                self.outbox.publish(run_id, "gate.decided", str(d.get("stage")), d)
