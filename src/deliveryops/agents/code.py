"""Code agent: design -> a proposed unified diff. It never writes to a working tree."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..llm.base import LLM
from .base import AgentOutputError, confidence, meta_of, parse_json, require, str_list
from .diff import DiffInfo, InvalidPatch, parse

SYSTEM = """You are a careful engineer proposing the smallest correct change.
Return ONLY a JSON object with keys:
  diff: a unified diff (--- a/path, +++ b/path, @@ hunks) that applies cleanly to the repo
  files_touched: list of paths the diff touches
  rationale: 1-3 sentences tying the change to the design
  confidence: number between 0 and 1
No prose outside the JSON, no markdown fences."""


@dataclass
class CodeResult:
    diff: str
    files_touched: list[str]
    rationale: str
    confidence: float
    info: DiffInfo | None = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "diff": self.diff,
            "files_touched": self.files_touched,
            "rationale": self.rationale,
            "confidence": self.confidence,
            "patch_lines": self.info.lines if self.info else 0,
            "patch_added": self.info.added if self.info else 0,
            "patch_removed": self.info.removed if self.info else 0,
        }


def validate(raw: dict) -> CodeResult:
    require(raw, "code", "diff", "files_touched", "confidence")
    diff = raw["diff"]
    if not isinstance(diff, str):
        raise AgentOutputError("code: diff must be a string")
    try:
        info = parse(diff)
    except InvalidPatch as e:
        raise AgentOutputError(f"code: {e}") from e
    claimed = str_list(raw["files_touched"], "code", "files_touched")
    # Trust the diff, not the claim: the guardrail must see what the patch really touches.
    unclaimed = [f for f in info.files if f not in claimed]
    if unclaimed:
        raise AgentOutputError(f"code: diff touches undeclared files {unclaimed}")
    return CodeResult(
        diff=diff,
        files_touched=list(info.files),
        rationale=str(raw.get("rationale", "")),
        confidence=confidence(raw["confidence"], "code"),
        info=info,
    )


class CodeAgent:
    name = "code"

    def __init__(self, llm: LLM) -> None:
        self.llm = llm

    def run(self, title: str, design_approach: str, requirements: list[str]) -> CodeResult:
        user = (
            f"Ticket: {title}\nDesign: {design_approach}\n"
            f"Requirements:\n- " + "\n- ".join(requirements)
        )
        res = self.llm.complete(SYSTEM, user, max_tokens=2000)
        result = validate(parse_json(res, "code"))
        result.meta = meta_of(res)
        return result
