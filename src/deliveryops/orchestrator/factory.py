"""Compiled-graph entry point for the LangGraph CLI and Studio (`langgraph dev`).

langgraph.json points here. Same building blocks as the API: model selection via env
(DELIVERYOPS_LLM/PROVIDER/MODEL), durable checkpointer when DATABASE_URL is set, in-memory
otherwise. Studio then shows the live graph and lets you time-travel through checkpoints.
"""
from __future__ import annotations

from ..agents.code import CodeAgent
from ..agents.design import DesignAgent
from ..agents.intake import IntakeAgent
from ..agents.review import ReviewAgent
from ..llm.factory import get_llm
from ..observability.tracer import Tracer
from .checkpoint import get_checkpointer
from .events import Outbox
from .graph import StageNodes, build_graph


def make_graph():
    llm = get_llm()
    nodes = StageNodes(
        intake=IntakeAgent(llm),
        design=DesignAgent(llm),
        code=CodeAgent(llm),
        review=ReviewAgent(llm),
        tracer=Tracer(),
        outbox=Outbox(),
    )
    return build_graph(nodes, get_checkpointer())


graph = make_graph()
