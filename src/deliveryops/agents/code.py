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


MAX_CONTEXT_FILES = 3
MAX_CONTEXT_CHARS = 12_000


def _repo_context(repo, hint: str) -> str:
    """Read-only grounding: the target repo's listing plus the files the design names."""
    try:
        listing = repo.list_files()
    except Exception:  # an unreachable repo must not fail the stage; note it and go on
        return "Target repository unreachable; the diff could not be grounded in real files."
    named = [p for p in listing if p in hint or ("/" in p and p.split("/")[-1] in hint)]
    blocks = ["Files in the target repository:", *[f"- {p}" for p in listing[:200]]]
    budget = MAX_CONTEXT_CHARS
    for path in named[:MAX_CONTEXT_FILES]:
        got = repo.read_file(path)
        text = got.get("text", "")
        if "error" in got or not text:
            continue
        chunk = text[:budget]
        budget -= len(chunk)
        blocks.append(f"--- {path} ---\n{chunk}")
        if budget <= 0:
            break
    return "\n".join(blocks)


class CodeAgent:
    name = "code"

    def __init__(self, llm: LLM, repo=None) -> None:
        self.llm = llm
        self.repo = repo  # read-only repo reader (integrations.github); None = ungrounded

    def run(self, title: str, design_approach: str, requirements: list[str]) -> CodeResult:
        user = (
            f"Ticket: {title}\nDesign: {design_approach}\n"
            f"Requirements:\n- " + "\n- ".join(requirements)
        )
        if self.repo is not None:
            hint = design_approach + "\n" + "\n".join(requirements)
            user += ("\n\nRepository context (read-only; the diff must apply against the "
                     "real target repo):\n" + _repo_context(self.repo, hint))
        res = self.llm.complete(SYSTEM, user, max_tokens=2000)
        result = validate(parse_json(res, "code"))
        result.meta = meta_of(res)
        return result
