"""Tracing: one span per agent call, rolled up into cost, latency and quality metrics.

Deliberately not OpenTelemetry yet. The span shape here is what an exporter would carry, so
wiring OTel later is an adapter, not a rewrite; today the sink is the repository (agent_calls).
"""
from __future__ import annotations

import statistics
import time
from dataclasses import asdict, dataclass, field

from ..llm.pricing import cost_usd


@dataclass(frozen=True)
class Span:
    run_id: str
    stage: str
    agent: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cost_usd: float
    ts: float = field(default_factory=time.time)
    ok: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class Tracer:
    """Collects spans and computes the aggregates the dashboard and exec summary report."""

    def __init__(self, sink=None) -> None:
        self._spans: list[Span] = []
        self._sink = sink  # callable(Span) -> None, e.g. repository.record_call

    def record(self, run_id: str, stage: str, agent: str, meta: dict, ok: bool = True,
               error: str = "") -> Span:
        model = str(meta.get("model", "unknown"))
        input_tokens = int(meta.get("input_tokens", 0))
        output_tokens = int(meta.get("output_tokens", 0))
        span = Span(
            run_id=run_id,
            stage=stage,
            agent=agent,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=float(meta.get("latency_ms", 0.0)),
            cost_usd=float(meta.get("cost_usd") or cost_usd(model, input_tokens, output_tokens)),
            ok=ok,
            error=error,
        )
        self._spans.append(span)
        if self._sink:
            self._sink(span)
        return span

    def spans(self, run_id: str | None = None) -> list[Span]:
        return [s for s in self._spans if run_id is None or s.run_id == run_id]

    def metrics(self) -> dict:
        spans = self._spans
        if not spans:
            return {"calls": 0, "cost_usd": 0.0, "tokens": 0, "latency": {}, "by_stage": {}}
        latencies = [s.latency_ms for s in spans]
        by_stage: dict[str, dict] = {}
        for stage in sorted({s.stage for s in spans}):
            group = [s for s in spans if s.stage == stage]
            by_stage[stage] = {
                "calls": len(group),
                "cost_usd": round(sum(s.cost_usd for s in group), 6),
                "tokens": sum(s.input_tokens + s.output_tokens for s in group),
                "avg_latency_ms": round(statistics.mean(s.latency_ms for s in group), 2),
                "error_rate": round(sum(0 if s.ok else 1 for s in group) / len(group), 3),
            }
        return {
            "calls": len(spans),
            "cost_usd": round(sum(s.cost_usd for s in spans), 6),
            "tokens": sum(s.input_tokens + s.output_tokens for s in spans),
            "latency": {
                "mean_ms": round(statistics.mean(latencies), 2),
                "p95_ms": round(_p95(latencies), 2),
            },
            "by_stage": by_stage,
        }


def _p95(values: list[float]) -> float:
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[idx]
