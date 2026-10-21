"""Agent output contracts: what the model must produce, and what happens when it does not."""
from __future__ import annotations

import json

import pytest

from deliveryops.agents.base import AgentOutputError
from deliveryops.agents.code import validate as validate_code
from deliveryops.agents.design import DesignAgent
from deliveryops.agents.diff import InvalidPatch, parse
from deliveryops.agents.intake import IntakeAgent, validate
from deliveryops.agents.review import ReviewAgent, static_scan
from deliveryops.llm.base import LLMResult
from deliveryops.llm.fake import FakeLLM

from .conftest import (
    CODE_PAYLOAD,
    DESIGN_PAYLOAD,
    PATCH,
    REVIEW_PAYLOAD,
    ScriptedLLM,
    triage_payload,
)


def test_validate_accepts_good_output():
    assert validate(triage_payload()).type == "bug"


@pytest.mark.parametrize("bad", [
    triage_payload(type="epic"),
    triage_payload(priority="urgent"),
    triage_payload(estimate_points=4),
    triage_payload(confidence=1.5),
    triage_payload(requirements=[]),
    triage_payload(requirements=["a"] * 6),
])
def test_validate_rejects_contract_violations(bad):
    with pytest.raises(AgentOutputError):
        validate(bad)


def test_missing_field_is_a_contract_violation():
    broken = triage_payload()
    del broken["requirements"]
    with pytest.raises(AgentOutputError, match="missing keys"):
        validate(broken)


def test_agent_rejects_non_json():
    with pytest.raises(AgentOutputError, match="not JSON"):
        IntakeAgent(ScriptedLLM("sure! here is your triage")).run("t", "d")


def test_json_in_code_fence_is_tolerated():
    text = "```json\n" + json.dumps(triage_payload()) + "\n```"
    assert IntakeAgent(ScriptedLLM(text)).run("t", "d").type == "bug"


def test_meta_carries_tokens_latency_and_cost():
    class Metered(ScriptedLLM):
        def complete(self, system, user, *, max_tokens=1000, temperature=0.0):
            return LLMResult(text=json.dumps(triage_payload()), input_tokens=100,
                             output_tokens=50, latency_ms=12.5, model="fake-keyword-llm")

    r = IntakeAgent(Metered("x")).run("t", "d")
    assert r.meta["input_tokens"] == 100 and r.meta["latency_ms"] == 12.5
    assert r.meta["cost_usd"] == 0.0  # fake model is priced at zero


def test_code_agent_rejects_patch_for_undeclared_file():
    payload = {**CODE_PAYLOAD, "files_touched": ["somewhere/else.py"]}
    with pytest.raises(AgentOutputError, match="undeclared"):
        validate_code(payload)


def test_code_agent_rejects_patch_without_hunks():
    payload = {**CODE_PAYLOAD, "diff": "--- a/x.py\n+++ b/x.py\n"}
    with pytest.raises(AgentOutputError, match="no hunks"):
        validate_code(payload)


def test_code_agent_reports_files_from_the_diff_not_the_claim():
    payload = {**CODE_PAYLOAD, "files_touched": ["src/auth/session.py", "extra.py"]}
    result = validate_code(payload)
    assert result.files_touched == ["src/auth/session.py"]
    assert result.info.hunks == 1


def test_diff_parser_counts_added_and_removed_lines():
    info = parse(PATCH)
    assert (info.added, info.removed) == (1, 1)
    assert info.files == ("src/auth/session.py",)


def test_diff_parser_rejects_empty_and_garbage():
    with pytest.raises(InvalidPatch):
        parse("")
    with pytest.raises(InvalidPatch):
        parse("just some text")


def test_review_static_scan_flags_added_secret():
    findings = static_scan('+api_key = "sk-live-abcdef123456"')
    assert findings and findings[0].severity == "critical"


def test_review_agent_overrides_optimistic_verdict_on_secret():
    bad_diff = (
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n x = 1\n"
        "+api_key = \"sk-live-abcdef123456\"\n"
    )
    result = ReviewAgent(ScriptedLLM(json.dumps(REVIEW_PAYLOAD))).run(bad_diff, "design")
    assert result.verdict == "request_changes"
    assert result.critical_count >= 1


def test_design_agent_cites_retrieved_documents():
    # Default search goes through the MCP tool layer, which uses the lexical retriever offline.
    r = DesignAgent(ScriptedLLM(json.dumps(DESIGN_PAYLOAD))).run(
        "Checkout fails", ["restore deleted records", "add rollback"], "data loss rollback"
    )
    assert r.citations and all("#kb-" in c for c in r.citations)
    assert r.confidence == 0.9


def test_design_agent_accepts_an_injected_search():
    calls = []

    def fake_search(query: str, k: int = 3) -> list[dict]:
        calls.append((query, k))
        return [{"citation": "docs/policy.md#kb-999", "title": "Policy", "content": "text",
                 "score": 1.0}]

    r = DesignAgent(ScriptedLLM(json.dumps(DESIGN_PAYLOAD)), search=fake_search).run("t", ["r"])
    assert r.citations == ["docs/policy.md#kb-999"] and calls == [("t r", 3)]


def test_design_agent_rejects_too_short_approach():
    payload = {**DESIGN_PAYLOAD, "approach": "fix"}
    with pytest.raises(ValueError, match="approach"):
        DesignAgent(ScriptedLLM(json.dumps(payload))).run("t", ["r"])


def test_fake_llm_answers_every_agent_kind():
    from deliveryops.agents.code import SYSTEM as CODE_SYSTEM
    from deliveryops.agents.design import SYSTEM as DESIGN_SYSTEM
    from deliveryops.agents.intake import SYSTEM as INTAKE_SYSTEM
    from deliveryops.agents.review import SYSTEM as REVIEW_SYSTEM

    fake = FakeLLM()
    for system, key in [(INTAKE_SYSTEM, "type"), (DESIGN_SYSTEM, "approach"),
                        (CODE_SYSTEM, "diff"), (REVIEW_SYSTEM, "verdict")]:
        payload = fake.complete(system, "Title: t\n\nbody").json()
        assert key in payload, (system[:40], payload)
