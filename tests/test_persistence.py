"""Persistence ordering: the run row must exist before telemetry and audit rows reference it.

This is the bug the Docker/Postgres run caught: an in-memory repository hides foreign-key ordering,
so the ordering is asserted explicitly with a spy that records call order.
"""
from __future__ import annotations

from deliveryops.db.repository import AuditEntry, Escalation, InMemoryRepository, RunRecord
from deliveryops.observability.tracer import Span

from .conftest import make_pipeline


class OrderSpyRepository(InMemoryRepository):
    def __init__(self) -> None:
        super().__init__()
        self.order: list[str] = []

    def save(self, record: RunRecord) -> None:
        self.order.append(f"save:{record.status}")
        super().save(record)

    def append_audit(self, entry: AuditEntry) -> None:
        self.order.append("audit")
        super().append_audit(entry)

    def record_call(self, span: Span) -> None:
        self.order.append(f"call:{span.stage}")
        super().record_call(span)

    def record_escalation(self, esc: Escalation) -> None:
        self.order.append("escalation")
        super().record_escalation(esc)


def test_run_row_is_saved_before_the_first_agent_call():
    repo = OrderSpyRepository()
    p = make_pipeline(repo=repo)
    p.start("Login page returns 500", "Users get a 500 after a password reset.")
    assert repo.order[0].startswith("save:"), repo.order
    assert repo.order.index("call:intake") > 0


def test_every_persisted_row_belongs_to_a_known_run():
    repo = OrderSpyRepository()
    p = make_pipeline(repo=repo)
    view = p.start("Login page returns 500", "Users get a 500 after a password reset.")
    assert repo.get(view.run_id) is not None
    assert all(span.run_id == view.run_id for span in repo.calls(view.run_id))
    assert all(entry.run_id == view.run_id for entry in repo.audit(view.run_id))


def test_committed_record_carries_status_stage_and_state():
    p = make_pipeline()
    view = p.start("Login page returns 500", "Users get a 500 after a password reset.")
    record = p.repo.get(view.run_id)
    assert record.status == "awaiting_approval"
    assert record.stage == "intake"
    assert record.state["triage"]["type"] == "bug"
    assert record.state["awaiting"]["stage"] == "intake"
