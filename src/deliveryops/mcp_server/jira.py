"""Mock Jira ticket store behind the MCP tool layer.

Replace with a real Jira/GitHub adapter by implementing the same two methods; nothing above this
layer knows which tracker it is talking to.
"""
from __future__ import annotations

import itertools
import time
from dataclasses import asdict, dataclass, field


@dataclass
class Ticket:
    id: str
    title: str
    description: str
    reporter: str = "unknown"
    labels: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class TicketStore:
    def __init__(self, seed: bool = True) -> None:
        self._tickets: dict[str, Ticket] = {}
        self._counter = itertools.count(101)
        if seed:
            self.create(
                "Checkout returns 500 on submit",
                "Payments fail for all users. Production outage.",
                "oncall", ["payments"],
            )
            self.create(
                "Add CSV export to reports",
                "Teams want to export the monthly report as CSV.",
                "pm", ["reporting"],
            )
            self.create(
                "Deprecate legacy /v1/ping",
                "Unused endpoint; remove after confirming no traffic.",
                "platform", ["cleanup"],
            )

    def create(self, title: str, description: str, reporter: str = "unknown",
               labels: list[str] | None = None) -> Ticket:
        tid = f"OPS-{next(self._counter)}"
        t = Ticket(tid, title, description, reporter, labels or [])
        self._tickets[tid] = t
        return t

    def get(self, ticket_id: str) -> Ticket | None:
        return self._tickets.get(ticket_id)

    def list(self) -> list[Ticket]:
        return list(self._tickets.values())
