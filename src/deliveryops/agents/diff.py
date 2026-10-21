"""Parse a proposed unified diff.

The code agent never writes to a working tree: it proposes a patch, this module proves the patch
is well formed, and a human applies it after approving the run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
_MINUS_FILE = re.compile(r"^--- (?:a/)?(\S+)")
_PLUS_FILE = re.compile(r"^\+\+\+ (?:b/)?(\S+)")


class InvalidPatch(ValueError):
    pass


@dataclass(frozen=True)
class DiffInfo:
    files: tuple[str, ...]
    added: int
    removed: int
    hunks: int
    lines: int
    binary: bool = False


def parse(text: str) -> DiffInfo:
    if not text.strip():
        raise InvalidPatch("empty patch")
    files: list[str] = []
    added = removed = hunks = 0
    binary = False
    for line in text.splitlines():
        if line.startswith("Binary files") or "GIT binary patch" in line:
            binary = True
        if _HUNK.match(line):
            hunks += 1
        elif line.startswith("--- "):
            m = _MINUS_FILE.match(line)
            if m and m.group(1) != "/dev/null" and m.group(1) not in files:
                files.append(m.group(1))
        elif line.startswith("+++ "):
            m = _PLUS_FILE.match(line)
            if m and m.group(1) != "/dev/null" and m.group(1) not in files:
                files.append(m.group(1))
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    if not files:
        raise InvalidPatch("no file headers found in patch")
    if hunks == 0 and not binary:
        raise InvalidPatch("no hunks found in patch")
    return DiffInfo(tuple(files), added, removed, hunks, len(text.splitlines()), binary)
