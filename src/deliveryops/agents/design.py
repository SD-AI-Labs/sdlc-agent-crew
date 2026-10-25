"""Design agent: approved requirements + retrieved process docs -> a technical design."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from ..llm.base import LLM
from .base import complete_structured, confidence, require, str_list

SYSTEM = """You are a senior engineer producing a technical design for a small change.
Return ONLY a JSON object with keys:
  approach: 2-4 sentences describing the change
  components: list of 1-5 components or files touched
  risks: list of 0-4 concrete risks
  test_plan: list of 1-4 test steps
  open_questions: list of 0-3 questions that must be answered by a human
  confidence: number between 0 and 1
No prose, no markdown."""


@dataclass
class DesignResult:
    approach: str
    components: list[str]
    risks: list[str]
    test_plan: list[str]
    open_questions: list[str]
    confidence: float
    citations: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "approach": self.approach,
            "components": self.components,
            "risks": self.risks,
            "test_plan": self.test_plan,
            "open_questions": self.open_questions,
            "confidence": self.confidence,
            "citations": self.citations,
        }


def validate(raw: dict, citations: list[str]) -> DesignResult:
    require(raw, "design", "approach", "components", "test_plan", "confidence")
    approach = raw["approach"]
    if not isinstance(approach, str) or len(approach.strip()) < 10:
        raise ValueError("design: approach must be a sentence or more")
    return DesignResult(
        approach=approach.strip(),
        components=str_list(raw["components"], "design", "components"),
        risks=str_list(raw.get("risks", []), "design", "risks"),
        test_plan=str_list(raw["test_plan"], "design", "test_plan"),
        open_questions=str_list(raw.get("open_questions", []), "design", "open_questions"),
        confidence=confidence(raw["confidence"], "design"),
        citations=citations,
    )


def _default_search(query: str, k: int = 3) -> list[dict]:
    """Knowledge arrives through the MCP tool layer, not from a direct retriever import."""
    from ..mcp_server import client

    return client.search_knowledge(query, k=k)


KnowledgeSearch = Callable[[str, int], list[dict]]


class DesignSchema(BaseModel):
    """Structured-output schema; validate() below remains the contract enforcement."""

    approach: str
    components: list[str]
    risks: list[str] = Field(default_factory=list)
    test_plan: list[str]
    open_questions: list[str] = Field(default_factory=list)
    confidence: float


class DesignAgent:
    name = "design"

    def __init__(self, llm: LLM, search: KnowledgeSearch | None = None) -> None:
        self.llm = llm
        self.search: KnowledgeSearch = search or _default_search

    def run(self, title: str, requirements: list[str],
            knowledge_query: str | None = None) -> DesignResult:
        query = knowledge_query or f"{title} {' '.join(requirements)}"
        hits = self.search(query, 3)
        context = "\n\n".join(f"[{h['citation']}] {h['title']}\n{h['content']}" for h in hits)
        user = (
            f"Ticket: {title}\n"
            f"Approved requirements:\n- " + "\n- ".join(requirements) + "\n\n"
            f"Process knowledge (cite by id when you rely on it):\n{context}"
        )
        raw, meta = complete_structured(self.llm, SYSTEM, user, DesignSchema, "design")
        result = validate(raw, [h["citation"] for h in hits])
        result.meta = meta
        return result
