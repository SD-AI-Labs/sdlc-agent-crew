"""HTTP API: submit tickets, answer gates, read runs, dashboard, metrics, per-run report."""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..console.dashboard import render_dashboard
from ..db.repository import get_repository
from ..integrations.github import get_repo_reader
from ..integrations.jira import JiraError, get_ticket_store
from ..llm.factory import get_llm
from ..observability.report import executive_summary
from ..orchestrator.checkpoint import get_checkpointer
from ..orchestrator.pipeline import (
    GateClosed,
    InsufficientAuthority,
    Pipeline,
    RunNotFound,
)
from ..orchestrator.worker import build_outbox

_repo = get_repository()
_pipeline: Pipeline | None = None
_tickets = None


def get_ticket_provider():
    """Lazy: env is read when the first ticket arrives, not at import time."""
    global _tickets
    if _tickets is None:
        _tickets = get_ticket_store()
    return _tickets


def get_pipeline() -> Pipeline:
    global _pipeline
    if _pipeline is None:
        # Same outbox backend as the worker: with a database, gate events are durable rows.
        # The checkpointer follows the database too: durable state, resumable after restart.
        _pipeline = Pipeline(get_llm(), repo=_repo, outbox=build_outbox(),
                             checkpointer=get_checkpointer(),
                             repo_reader=get_repo_reader())
    return _pipeline


class TicketIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    ticket_id: str | None = None


class DecisionIn(BaseModel):
    approver: str = Field(min_length=1)
    approve: bool
    comment: str = ""
    role: str = "engineer"
    # Optional: the gate the caller believes it is answering. A mismatch with the gate
    # actually open is a 409, so a slow or duplicated request cannot land on the next gate.
    stage: str | None = None


def create_app(pipeline: Pipeline | None = None) -> FastAPI:
    app = FastAPI(title="DeliveryOps Agents", version="0.2.0")
    pipe = pipeline or get_pipeline()

    def _load_eval_report() -> dict | None:
        path = Path(os.getenv("DELIVERYOPS_EVAL_REPORT", "eval-report.json"))
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except ValueError:
            return None

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "llm": pipe.nodes.intake.llm.name, "runs": len(pipe.repo.list(500))}

    @app.post("/tickets", status_code=201)
    def submit(ticket: TicketIn) -> dict:
        title, description = ticket.title, ticket.description
        if ticket.ticket_id:
            try:
                found = get_ticket_provider().get(ticket.ticket_id)
            except JiraError as e:
                raise HTTPException(502, f"tracker unreachable: {e}") from e
            if not found:
                raise HTTPException(404, "ticket not found")
            title, description = found.title, found.description
        if not title or not description or len(title) < 3 or len(description) < 3:
            raise HTTPException(422, "title and description are required (min 3 chars)")
        try:
            view = pipe.start(title, description)
        except Exception as e:
            # Stage failures are handled inside the pipeline (the run goes to
            # 'failed'); anything escaping to here is infrastructure, reported as a
            # structured 502 rather than a bare 500.
            raise HTTPException(502, f"pipeline failed to start the run: {e}") from e
        return view.to_dict()

    @app.get("/runs")
    def list_runs(limit: int = 50) -> list[dict]:
        out = []
        for record in pipe.repo.list(limit):
            out.append({
                "run_id": record.id,
                "title": record.title,
                "status": record.status,
                "stage": record.stage,
                "created_at": record.created_at,
                "spend_usd": (record.state or {}).get("spend_usd", 0.0),
            })
        return out

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        try:
            return pipe.view(run_id).to_dict()
        except RunNotFound as e:
            raise HTTPException(404, "run not found") from e

    @app.post("/runs/{run_id}/decision")
    def decide(run_id: str, body: DecisionIn,
               idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
        try:
            return pipe.resume(run_id, body.approver, body.approve, body.comment,
                               body.role, stage=body.stage,
                               idempotency_key=idempotency_key).to_dict()
        except RunNotFound as e:
            raise HTTPException(404, "run not found") from e
        except GateClosed as e:
            raise HTTPException(409, str(e)) from e
        except InsufficientAuthority as e:
            raise HTTPException(403, str(e)) from e
        except Exception as e:
            raise HTTPException(502, f"pipeline failed to resume the run: {e}") from e

    @app.get("/runs/{run_id}/patch", response_class=PlainTextResponse)
    def get_patch(run_id: str) -> str:
        try:
            return pipe.apply_patch(run_id)
        except RunNotFound as e:
            raise HTTPException(404, "run not found") from e
        except GateClosed as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/runs/{run_id}/summary", response_class=PlainTextResponse)
    def get_summary(run_id: str) -> str:
        try:
            view = pipe.view(run_id)
        except RunNotFound as e:
            raise HTTPException(404, "run not found") from e
        return executive_summary(view, pipe.tracer, _load_eval_report())

    @app.get("/metrics")
    def metrics() -> dict:
        base = pipe.tracer.metrics()
        runs = pipe.repo.list(500)
        base["runs"] = {
            "total": len(runs),
            "awaiting_approval": sum(1 for r in runs if r.status == "awaiting_approval"),
            "approved": sum(1 for r in runs if r.status == "approved"),
            "rejected": sum(1 for r in runs if r.status == "rejected"),
            "failed": sum(1 for r in runs if r.status == "failed"),
        }
        base["escalations"] = len(pipe.repo.escalations())
        persisted = pipe.repo.calls()
        base["persisted"] = {
            "calls": len(persisted),
            "tokens": sum(s.input_tokens + s.output_tokens for s in persisted),
            "cost_usd": round(sum(s.cost_usd for s in persisted), 6),
        }
        base["eval_gate"] = (_load_eval_report() or {}).get("passed")
        return base

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard() -> str:
        return render_dashboard(pipe.repo.list(50), pipe.tracer.metrics(),
                                pipe.repo.escalations())

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("deliveryops.api.main:app", host="0.0.0.0",
                port=int(os.getenv("PORT", "8000")), reload=False)
