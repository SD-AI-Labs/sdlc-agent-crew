"""The eval gate: scoring, critical cases, and the proof that the gate has teeth."""
from __future__ import annotations

import json
from pathlib import Path

from deliveryops.agents.intake import IntakeAgent
from deliveryops.evals import runner
from deliveryops.evals.gates import THRESHOLDS, check
from deliveryops.evals.scorers import exact_set, requirement_coverage, score_triage, within_one_step
from deliveryops.llm.base import LLMResult
from deliveryops.llm.fake import FakeLLM

from .conftest import ScriptedLLM, triage_payload

GOLDEN = Path(__file__).parent.parent / "evals" / "golden" / "intake.json"


class OracleLLM:
    """Answers every golden case with its expectation: proves the gate *can* pass."""

    name = "oracle"

    def __init__(self, mutate=None) -> None:
        self.cases = {c["title"]: c for c in json.loads(GOLDEN.read_text())}
        self.mutate = mutate

    def complete(self, system, user, *, max_tokens=1000, temperature=0.0):
        title = user.split("\n", 1)[0].removeprefix("Title: ")
        e = self.cases[title]["expected"]
        payload = {
            "type": e["type"][0] if isinstance(e["type"], list) else e["type"],
            "priority": e["priority"][0] if isinstance(e["priority"], list) else e["priority"],
            "estimate_points": e["estimate_points"],
            "requirements": e["must_mention"] or ["x"],
            "confidence": 0.9,
        }
        if self.mutate:
            payload = self.mutate(title, payload)
        return LLMResult(text=json.dumps(payload), input_tokens=100, output_tokens=40,
                         latency_ms=5.0, model=self.name)


def test_exact_set_accepts_any_listed_answer():
    assert exact_set("high", ["medium", "high"]) == 1.0
    assert exact_set("low", ["medium", "high"]) == 0.0
    assert exact_set("bug", "bug") == 1.0


def test_estimate_scale_gives_partial_credit_for_adjacent_bucket():
    assert within_one_step(3, 3) == 1.0
    assert within_one_step(2, 3) == 0.5
    assert within_one_step(1, 8) == 0.0


def test_coverage_requires_the_prescriptive_terms():
    assert requirement_coverage(["add a regression test"], ["regression"]) == 1.0
    assert requirement_coverage(["fix the login error"], ["regression"]) == 0.0


def test_score_triage_has_four_dimensions():
    r = IntakeAgent(ScriptedLLM(json.dumps(triage_payload()))).run("t", "d")
    assert set(score_triage(r, {"type": ["bug"], "priority": ["medium"],
                                "estimate_points": 2, "must_mention": ["x"]})) == {
        "type", "priority", "estimate", "requirements"
    }


def test_gate_passes_for_a_correct_agent():
    report = runner.run_eval(GOLDEN, IntakeAgent(OracleLLM()))
    assert report["passed"], report["gate_failures"]
    assert report["means"]["safety"] == 1.0


def test_gate_blocks_the_keyword_baseline():
    report = runner.run_eval(GOLDEN, IntakeAgent(FakeLLM()))
    assert not report["passed"]
    assert report["means"]["requirements"] < THRESHOLDS["requirements"]


def test_report_records_reproducibility_metadata():
    report = runner.run_eval(GOLDEN, IntakeAgent(OracleLLM()))
    assert report["golden_sha"] and report["prompt_sha"]
    assert report["cases"] == len(json.loads(GOLDEN.read_text()))
    assert report["cost_usd"] == 0.0
    assert report["priced"] is True  # test doubles are explicitly priced at zero


def test_critical_case_failure_blocks_the_gate_even_when_means_pass():
    """T-002 is a production outage; getting its priority wrong must fail the whole run."""

    def mutate(title, payload):
        if title.startswith("Checkout fails for all users"):
            payload = {**payload, "priority": "low"}
        return payload

    report = runner.run_eval(GOLDEN, IntakeAgent(OracleLLM(mutate)))
    assert not report["passed"]
    assert any("critical case failed: T-002" in f for f in report["gate_failures"])


def test_malformed_output_counts_as_failure_not_skip():
    report = runner.run_eval(GOLDEN, IntakeAgent(ScriptedLLM("nonsense")))
    assert report["failure_rate"] == 1.0 and not report["passed"]


def test_injection_case_must_escalate():
    def mutate(title, payload):
        if "instructions" in title:  # pragma: no cover - keeps the helper honest
            return payload
        return payload

    report = runner.run_eval(GOLDEN, IntakeAgent(OracleLLM(mutate)))
    assert report["means"]["safety"] == 1.0


def test_judge_dimension_is_scored_when_a_judge_is_supplied():
    judge = ScriptedLLM(json.dumps({"score": 4, "reason": "specific and testable"}))
    report = runner.run_eval(GOLDEN, IntakeAgent(OracleLLM()), judge=judge)
    assert report["judge"] == "scripted"
    assert 0.7 < report["means"]["requirements_judge"] <= 1.0


def test_cost_budget_is_enforced():
    failures = check({"type": 1.0}, 0.0, 5.0, [])
    assert any("cost_usd" in f for f in failures)


def test_baseline_check_mode_exits_zero_when_the_baseline_fails():
    assert runner.main(["--baseline-check"]) == 0


def test_trials_take_the_median_across_calls():
    report = runner.run_eval(GOLDEN, IntakeAgent(OracleLLM()), trials=2)
    assert report["trials"] == 2 and report["passed"]
