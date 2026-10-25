"""Eval runner and release gate. Exit code 1 means: do not ship this agent change.

Usage:
  deliveryops-eval                     # eval the configured model
  deliveryops-eval --baseline-check    # assert the naive baseline FAILS the gate
  deliveryops-eval --judge             # add the LLM-as-judge requirements dimension

The report records the model, a hash of the golden set and a hash of the agent's system prompt, so
two runs can be compared honestly (ADR 0004).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path

from ..agents.intake import SYSTEM as INTAKE_SYSTEM
from ..agents.intake import AgentOutputError, IntakeAgent
from ..guardrails import injection
from ..guardrails.policy import evaluate
from ..llm.base import LLM
from ..llm.factory import get_llm
from ..llm.fake import FakeLLM
from ..llm.pricing import is_priced
from .gates import (
    CRITICAL_DIMENSIONS,
    MAX_COST_USD,
    RETRIEVAL_THRESHOLDS,
    THRESHOLDS,
    check,
    retrieval_check,
)
from .scorers import judge_requirements, score_triage

DIMENSIONS = ("type", "priority", "estimate", "requirements")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _signals(case: dict, triage) -> dict:
    text = f"{case['title']} {case['description']}"
    from ..guardrails.policy import looks_urgent

    return {
        "confidence": triage.confidence,
        "priority": triage.priority,
        "estimate_points": triage.estimate_points,
        "severity_inversion": looks_urgent(text) and triage.priority != "high",
        "injection_hits": injection.scan(text),
    }


def run_eval(golden_path: Path, agent: IntakeAgent | None = None, judge: LLM | None = None,
             trials: int = 1) -> dict:
    golden_text = golden_path.read_text()
    cases = json.loads(golden_text)
    agent = agent or IntakeAgent(get_llm())
    per_case: list[dict] = []
    failures = critical_failures = 0
    tokens = latency_ms = cost = 0.0
    judge_scores: list[float] = []
    safety_scores: list[float] = []

    for case in cases:
        text = f"{case['title']} {case['description']}"
        expected = case["expected"]
        scores: list[dict[str, float]] = []
        error = ""
        for _ in range(max(1, trials)):
            try:
                r = agent.run(case["title"], case["description"])
            except AgentOutputError as e:
                error = str(e)
                continue
            s = score_triage(r, expected)
            if expected.get("injection"):
                decision = evaluate("intake", _signals(case, r))
                s["safety"] = 1.0 if decision.escalate else 0.0
                safety_scores.append(s["safety"])
            scores.append(s)
            tokens += r.meta["input_tokens"] + r.meta["output_tokens"]
            latency_ms += r.meta["latency_ms"]
            cost += r.meta.get("cost_usd", 0.0)
            if judge is not None:
                j = judge_requirements(judge, r.requirements, text)
                judge_scores.append(j["score"])
        if not scores:
            failures += 1
            per_case.append({"id": case["id"], "error": error or "no successful trial"})
            continue
        # Median across trials: a gate that flips on sampling noise is not a gate.
        agg = {k: statistics.median([s[k] for s in scores]) for k in scores[0]}
        entry = {"id": case["id"], "scores": agg}
        if expected.get("critical"):
            entry["critical"] = True
            if any(v < 1.0 for k, v in agg.items() if k in CRITICAL_DIMENSIONS):
                critical_failures += 1
                entry["critical_failure"] = True
        per_case.append(entry)

    scored = [p["scores"] for p in per_case if "scores" in p]
    means = {k: statistics.mean(s[k] for s in scored) if scored else 0.0 for k in DIMENSIONS}
    if safety_scores:
        means["safety"] = statistics.mean(safety_scores)
    if judge_scores:
        means["requirements_judge"] = statistics.mean(judge_scores)

    failure_rate = failures / len(cases)
    failures_list = check(
        means,
        failure_rate,
        cost,
        [f"critical case failed: {p['id']}" for p in per_case if p.get("critical_failure")],
    )
    return {
        "model": agent.llm.name,
        "priced": is_priced(agent.llm.name),
        "judge": judge.name if judge else None,
        "trials": max(1, trials),
        "cases": len(cases),
        "golden_sha": _sha(golden_text),
        "prompt_sha": _sha(INTAKE_SYSTEM),
        "means": {k: round(v, 4) for k, v in means.items()},
        "failure_rate": round(failure_rate, 4),
        "total_tokens": int(tokens),
        "cost_usd": round(cost, 6),
        "max_cost_usd": MAX_COST_USD,
        "avg_latency_ms": round(latency_ms / max(len(scored) * max(1, trials), 1), 2),
        "gate_failures": failures_list,
        "passed": not failures_list,
        "per_case": per_case,
    }


def run_retrieval_eval(golden_path: Path, retriever=None) -> dict:
    """Golden query -> expected kb-id pairs; recall@1 and recall@3 (ADR 0010).

    Works with any retriever - lexical offline in CI, hybrid against a live database in a
    staging check. A miss is reported with what came back instead, so tuning is evidence-based.
    """
    from ..knowledge.retriever import get_retriever

    golden_text = golden_path.read_text()
    pairs = json.loads(golden_text)
    retriever = retriever or get_retriever()
    per_case: list[dict] = []
    hit1 = hit3 = 0
    for pair in pairs:
        ids = [h.doc_id for h in retriever.search(pair["query"], k=3)]
        ok1 = bool(ids) and ids[0] == pair["expected"]
        ok3 = pair["expected"] in ids[:3]
        hit1 += ok1
        hit3 += ok3
        per_case.append({"id": pair["id"], "expected": pair["expected"], "got": ids,
                         "recall@1": ok1, "recall@3": ok3})
    recalls = {"recall@1": round(hit1 / len(pairs), 4), "recall@3": round(hit3 / len(pairs), 4)}
    failures = retrieval_check(recalls)
    return {
        "retriever": getattr(retriever, "name", type(retriever).__name__),
        "cases": len(pairs),
        "golden_sha": _sha(golden_text),
        "recalls": recalls,
        "gate_failures": failures,
        "passed": not failures,
        "per_case": per_case,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DeliveryOps eval gate")
    ap.add_argument("--golden", default="evals/golden/intake.json")
    ap.add_argument("--report", default="eval-report.json")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--judge", action="store_true", help="add the LLM-as-judge dimension")
    ap.add_argument("--baseline-check", action="store_true",
                    help="assert the naive keyword baseline FAILS the gate (CI self-check)")
    ap.add_argument("--retrieval", action="store_true",
                    help="eval retrieval recall on the golden query->kb-id pairs")
    ap.add_argument("--retrieval-golden", default="evals/golden/retrieval.json")
    args = ap.parse_args(argv)

    if args.baseline_check:
        report = run_eval(Path(args.golden), IntakeAgent(FakeLLM()))
        print(f"baseline model={report['model']} passed={report['passed']}")
        for k, v in report["means"].items():
            print(f"  {k:13s} {v:.2f}  (gate {THRESHOLDS.get(k, '-')})")
        if report["passed"]:
            print("FAIL: the naive baseline passed the gate; the gate has no teeth")
            return 1
        print("OK: the gate rejects the naive baseline")
        return 0

    if args.retrieval:
        report = run_retrieval_eval(Path(args.retrieval_golden))
        print(f"retriever={report['retriever']} cases={report['cases']} "
              f"passed={report['passed']}")
        for k, v in report["recalls"].items():
            print(f"  {k:10s} {v:.2f}  (gate {RETRIEVAL_THRESHOLDS.get(k, '-')})")
        for case in report["per_case"]:
            if not case["recall@3"]:
                print(f"  MISS  {case['id']}: expected {case['expected']}, got {case['got']}")
        for f in report["gate_failures"]:
            print(f"  GATE FAIL  {f}")
        return 0 if report["passed"] else 1

    llm = get_llm()
    judge = llm if args.judge and not isinstance(llm, FakeLLM) else None
    report = run_eval(Path(args.golden), IntakeAgent(llm), judge=judge, trials=args.trials)
    Path(args.report).write_text(json.dumps(report, indent=2))
    print(
        f"model={report['model']} cases={report['cases']} trials={report['trials']} "
        f"passed={report['passed']} cost=${report['cost_usd']:.4f}"
    )
    for k, v in report["means"].items():
        print(f"  {k:18s} {v:.2f}  (gate {THRESHOLDS.get(k, '-')})")
    for f in report["gate_failures"]:
        print(f"  GATE FAIL  {f}")
    if not report["priced"]:
        print(f"  note: model '{report['model']}' has no price entry; cost shown as 0.0")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
