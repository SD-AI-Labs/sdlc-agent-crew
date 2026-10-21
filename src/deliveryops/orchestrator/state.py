"""Shared pipeline state. LangGraph merges node return values into this."""
from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class PipelineState(TypedDict, total=False):
    run_id: str
    title: str
    description: str
    triage: dict
    design: dict
    patch: dict
    review: dict
    stage: str            # stage that just ran / is awaiting a decision
    status: str           # submitted | awaiting_approval | approved | rejected | failed
    decisions: Annotated[list[dict], operator.add]
    escalations: Annotated[list[dict], operator.add]
    audit: Annotated[list[dict], operator.add]
    errors: Annotated[list[dict], operator.add]
    spent_usd: Annotated[float, operator.add]
