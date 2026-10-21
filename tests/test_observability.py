"""Tracing, cost accounting and the per-run report."""
from __future__ import annotations

import pytest

from deliveryops.llm.pricing import cost_usd, is_priced, price_for
from deliveryops.observability.report import executive_summary
from deliveryops.observability.tracer import Tracer

from .conftest import make_pipeline


def test_cost_is_computed_from_published_prices():
    # 1M input + 1M output tokens on sonnet pricing
    assert cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000) == pytest.approx(18.0)
    assert cost_usd("claude-sonnet-4-6", 1000, 0) == pytest.approx(0.003)


def test_dated_snapshot_models_inherit_the_family_price():
    assert is_priced("claude-sonnet-4-6-20260101")
    assert price_for("claude-sonnet-4-6-20260101").output_per_mtok == 15.0


def test_unknown_model_is_unpriced_not_silently_wrong():
    assert cost_usd("mystery-model", 1000, 1000) == 0.0
    assert not is_priced("mystery-model")


def test_tracer_aggregates_by_stage():
    t = Tracer()
    t.record("r1", "intake", "intake", {"model": "claude-sonnet-4-6", "input_tokens": 1000,
                                        "output_tokens": 500, "latency_ms": 100})
    t.record("r1", "design", "design", {"model": "claude-sonnet-4-6", "input_tokens": 2000,
                                        "output_tokens": 900, "latency_ms": 300})
    m = t.metrics()
    assert m["calls"] == 2
    assert m["by_stage"]["intake"]["cost_usd"] > 0
    assert m["latency"]["mean_ms"] == 200.0
    assert sum(s.input_tokens + s.output_tokens for s in t.spans("r1")) == 4400


def test_tracer_records_errors_for_the_dashboard():
    t = Tracer()
    t.record("r1", "intake", "intake", {"model": "x"}, ok=False, error="bad json")
    assert t.metrics()["by_stage"]["intake"]["error_rate"] == 1.0


def test_exec_summary_for_a_finished_run_reports_what_and_how():
    p = make_pipeline()
    view = p.start("Login page returns 500", "Users get a 500 after a password reset.")
    while view.awaiting is not None:
        view = p.resume(view.run_id, "sukhdev", True, "ship it", role="senior")
    report = executive_summary(view, p.tracer, {"passed": True, "model": "oracle",
                                               "cases": 20, "means": {"type": 0.95}})
    assert "Outcome: **approved**" in report
    assert "What the agent did" in report
    assert "proposed only, not applied" in report
    assert "passed its eval gate" in report
    assert "ship it" in report


def test_exec_summary_flags_a_failing_eval_gate():
    p = make_pipeline()
    view = p.start("Login page returns 500", "Users get a 500 after a password reset.")
    report = executive_summary(view, p.tracer, {"passed": False, "means": {}})
    assert "not releasable" in report


def test_exec_summary_without_an_eval_report_says_so():
    p = make_pipeline()
    view = p.start("Login page returns 500", "Users get a 500 after a password reset.")
    assert "not supplied" in executive_summary(view, p.tracer, None)


def test_exec_summary_lists_escalations():
    p = make_pipeline(intake={"confidence": 0.1})
    view = p.start("Login page returns 500", "Users get a 500 after a password reset.")
    report = executive_summary(view, p.tracer)
    assert "## Escalations" in report and "(escalated)" in report
