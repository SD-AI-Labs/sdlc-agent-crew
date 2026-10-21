"""Prompt-injection screening for untrusted ticket text.

Ticket bodies are attacker-controlled input in the real world (anyone can file a ticket). The
first line of defence is detection: if the text looks like it is trying to steer the agent, the
run is escalated to a human with the matched phrases recorded, instead of being trusted.
"""
from __future__ import annotations

import re

PATTERNS: tuple[tuple[str, str], ...] = (
    (r"ignore (all )?(previous|prior|above) instructions", "instruction override"),
    (r"disregard (the )?(system|previous) prompt", "instruction override"),
    (r"you are now", "persona hijack"),
    (r"\bsystem\s*:", "fake system prompt"),
    (r"set (the )?(priority|confidence|estimate)[^.]{0,20}to", "output steering"),
    (r"confidence\s*(=|:)\s*1(\.0+)?", "output steering"),
    (r"do not (escalate|ask|require approval)", "gate bypass"),
    (r"approve (this|the) (ticket|change|run) automatically", "gate bypass"),
    (r"\bapi[_ ]?key\b", "credential fishing"),
    (r"\b(exfiltrate|leak|send) (the )?(secrets?|credentials?|env)", "credential fishing"),
)

_COMPILED = tuple((re.compile(p, re.IGNORECASE), reason) for p, reason in PATTERNS)


def scan(text: str) -> list[str]:
    """Return the distinct reasons the text looks like an injection attempt."""
    hits: list[str] = []
    for rx, reason in _COMPILED:
        if rx.search(text) and reason not in hits:
            hits.append(reason)
    return hits


def matched_phrases(text: str) -> list[str]:
    """Return the raw matched spans, for the audit log and the escalation note."""
    out: list[str] = []
    for rx, _ in _COMPILED:
        out.extend(m.group(0) for m in rx.finditer(text))
    return out
